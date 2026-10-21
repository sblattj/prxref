# The review gate on AWS Lambda

Status: in progress, 2026-10-21. Builds on `2026-10-08-sdlc-review-gate.md`.

The gate (`prxref gate serve`) runs as one long-lived process with a SQLite store. That works on
a laptop, but the gate goes down whenever the laptop sleeps. This design runs the same gate code
per PR event on AWS: nothing polls, and nothing stays up between PRs except a one-minute watchdog.

## Flow

1. **GitHub** sends a signed webhook when a PR is opened, pushed to, reopened or marked ready.
2. **Receiver** (API Gateway HTTP API + Lambda, 10 s timeout): verifies the HMAC on the raw body,
   maps the event with the existing `receiver.map_event`, records the generation in DynamoDB and
   sends `{generation_id}` to SQS. It never fetches a diff or calls a model.
3. **Queue** (SQS FIFO): `MessageGroupId = repo#pr`, `MessageDeduplicationId = generation id`.
   After 3 failed receives a message goes to a DLQ.
4. **Worker** (Lambda container image, 4 GB, 15 min, reserved concurrency 1): takes the store
   lease, then runs the existing `run_generation` (binding check, check run, `prxref` review of
   the diff fetched through the API, publish). It never checks out the branch or runs PR code.
   The review deadline is the smaller of `PRXREF_GATE_REVIEW_DEADLINE` and the invocation's
   remaining time minus 2 min (about 13 min), so a timed-out review still gets a red check.
   The lease TTL is 14 min. A message for a generation that is no longer queued is a no-op. A
   held lease raises, so SQS redelivers after the visibility timeout. While the gate is paused
   the worker consumes the message and leaves the row queued; the watchdog resends it.
5. **Model** (Bedrock): litellm uses the worker's execution role. That role can call
   `bedrock:InvokeModel*` and read its two secrets, nothing else. No bearer token on Lambda.
6. **GitHub**: the `sdlc-review` check run, plus inline findings.

**Watchdog** (EventBridge `rate(1 minute)` + Lambda): runs the existing `requeue_expired`, and
`reconcile` every `PRXREF_GATE_RECONCILE_MINUTES` per warm container, then sends one SQS message
per generation that is queued, not only the ones it re-queued. That also recovers a row the
receiver stored but failed to send. A first send dedupes on the generation id, a re-queued one
on `<id>-<attempts>`, so the FIFO 5-minute dedupe window cannot swallow a re-queue. It only
enqueues; the worker does every repair.

**Secrets**: the app key and the webhook secret are plain `SecretString`s, read once per cold
start and registered for redaction. The previous-secret grace window is measured from process
start, which on Lambda is every cold start, so it is off: rotate by updating the secret and the
app's webhook secret together, then redeliver any delivery that failed in between.

## Store

`DynamoStore` implements the same methods as the SQLite `Store` and passes the same contract
tests. One table, single-table layout, numbers zero-padded so they sort:

| pk | sk | holds |
|---|---|---|
| `GEN` | `<id>` | one generation row, the only copy of its mutable fields |
| `OPEN` | `<id>` | marker while a generation is queued or in progress, with its state |
| `PR#<repo>#<n>` | `GEN#<gen>` | immutable pointer: latest per PR, and unique gen per PR |
| `HEAD#<repo>#<sha>` | `<id>` | immutable pointer for rerequest lookups |
| `DELIVERY#<id>` | `DELIVERY` | webhook dedupe, 7-day TTL |
| `PUB#<id>` | `PUB` | the review posted for a generation |
| `LEASE` | `worker` | the single lease with its fencing token |
| `META` | `COUNTER` / `PAUSED` / `PUBLISH` | id counter, pause flag, last publish |

Every state change is one `TransactWriteItems` whose first item is a condition check on the
lease's fencing token, as in SQLite. A generation row lives in one partition (`GEN`), so
`recent` and the status views are strongly consistent queries with no index. Queue depth, the
expiry sweep and supersede read only the small `OPEN` partition. Lease liveness is always
`expires_at` against the clock, never DynamoDB's TTL feature: the lease item has no TTL
attribute, because deleting it would restart the fencing token at 1. Concurrent receivers on
one PR cannot both take a gen number: the `PR#` pointer is written with `attribute_not_exists`
and the loser retries. One lease plus worker reserved concurrency 1 keeps reviews serial. That
fits a personal install. Per-PR leases are the scale-up path.

## What a PR can and cannot change

The PR controls the diff, its title and body, and any file in the repo. The worker reads these
as text. The review rules come from the base branch (`rules_source`), so a PR cannot loosen its
own review. The app key and webhook secret live in Secrets Manager. The app has
`contents: read`, so its review never counts as an approval. The ruleset requires the
`sdlc-review` check, pinned to the app id, and no approvals.

## Rollout

- Phase 0: deploy, send signed sample webhooks to the API, then point the app's webhook URL at
  the API. The local gate is stopped but left installed.
- Rollback: point the webhook URL back at the tunnel and restart the local gate.
- Turning it off: remove the required check from the ruleset first, then disable the queue
  mapping and the webhook.

## Deploy

`deploy/aws/template.yaml` is an AWS SAM template: HTTP API, three functions, SQS FIFO + DLQ,
DynamoDB (on-demand, TTL), the EventBridge rule, IAM roles and Secrets Manager references.
`deploy/aws/deploy.sh` deploys it: it builds the image with buildx and Docker v2 media types
(a containerd-store build from `sam build` fails on Lambda with `Runtime.InvalidEntrypoint`),
pushes it, and passes the digest as `ImageUri`. The watchdog has reserved concurrency 1 and no
async retries, so two reconciles never race on one head.
