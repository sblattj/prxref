# The review gate on AWS Lambda

The same gate as `prxref gate serve` ([docs/gate.md](../../docs/gate.md)), run per event on AWS
instead of as one process on a laptop. Design: [the AWS design](../../docs/design/2026-10-21-gate-on-aws.md).

```
GitHub -> HTTP API POST /webhook -> receiver (10 s) -> SQS FIFO (group repo#pr) -> worker (15 min, 1 at a time)
                                         |                    ^                        |
                                         v                    |                        v
                                     DynamoDB  <---------- watchdog (every minute)   Bedrock (execution role)
```

All three functions share one container image (`Dockerfile`, Python 3.12 Lambda base,
`prxref[aws]`); each picks its handler with `ImageConfig.Command`. litellm alone is past the
250 MB zip limit, so the worker must be an image, and one image means one build and one copy of
the code. The cost is a receiver cold start of a second or two, well inside GitHub's 10 s.

## Prerequisites

1. **AWS CLI and SAM CLI**, credentials for the target account, and a local Docker engine
   (OrbStack works) for `sam build`.
2. **Bedrock model access** for the model in `ModelId` (default
   `bedrock/us.anthropic.claude-opus-4-6-v1`, a US cross-region inference profile) in the stack's
   region. Check it before deploying:
   `aws bedrock-runtime converse --model-id us.anthropic.claude-opus-4-6-v1 --messages '[{"role":"user","content":[{"text":"ping"}]}]'`.
   The worker role may call the `us.anthropic.*` inference profiles and the `anthropic.*`
   foundation models in every region the profile routes to; nothing else.
3. **The GitHub App** registered and installed as in [docs/gate.md](../../docs/gate.md)
   (Prerequisites 4 and 5), with its app id, installation id, PEM key and webhook secret.
4. **Two secrets**, created from files so no secret value is ever on a command line:

   ```bash
   aws secretsmanager create-secret --name prxref-gate/app-key \
     --secret-string file://path/to/app.pem
   aws secretsmanager create-secret --name prxref-gate/webhook-secret \
     --secret-string file://path/to/webhook-secret.txt
   ```

   The webhook secret file holds the secret alone, with no trailing newline
   (write it with `printf '%s'`, not `echo`). Note both ARNs. Delete the local
   copies you no longer need.

## Deploy

```bash
cd deploy/aws
sam build
sam deploy --guided
```

`--guided` asks for the parameters (`AppId`, `InstallationIds`, `Repos`, `AppKeySecretArn`,
`WebhookSecretArn`, `ModelId`, `ReasoningEffort`, `ReviewDeadline`), lets SAM create an ECR
repository for the image, and saves the answers to `samconfig.toml` so later deploys are
`sam build && sam deploy`. The stack outputs `WebhookUrl`, `QueueUrl`, `DeadLetterQueueUrl` and
`TableName`. The table is retained if the stack is deleted.

## Send a signed sample webhook

Before GitHub points at the API, sign a request yourself. The signature is computed from the
secret file by Python, so the secret never reaches argv:

```bash
URL=$(aws cloudformation describe-stacks --stack-name <stack> \
  --query "Stacks[0].Outputs[?OutputKey=='WebhookUrl'].OutputValue" --output text)
printf '%s' '{"zen":"ping","hook_id":1}' > ping.json
SIG=$(python3 -c 'import hmac,hashlib,sys; k=open(sys.argv[1],"rb").read(); b=open(sys.argv[2],"rb").read(); print("sha256="+hmac.new(k,b,hashlib.sha256).hexdigest())' path/to/webhook-secret.txt ping.json)
curl -s -X POST "$URL" -H "X-GitHub-Event: ping" -H "X-GitHub-Delivery: sample-1" \
  -H "X-Hub-Signature-256: $SIG" -H "Content-Type: application/json" --data-binary @ping.json
```

`{"status": "ignored", ...}` with 202 proves the route, the secret and the HMAC; a wrong secret
gives 401. To exercise the whole path, sign a `pull_request` `opened` payload for a real open PR
in an allowlisted repo (`number`, `pull_request.head.sha`, `pull_request.base.sha`/`ref`,
`repository.full_name`, `installation.id`) with `X-GitHub-Event: pull_request`, then watch the
worker's log group and the PR's `sdlc-review` check.

## Point the app at the API

GitHub → Settings → Developer settings → GitHub Apps → the app → General → Webhook URL: set it
to `WebhookUrl` and save. Stop the local gate but leave it installed:
`launchctl bootout gui/$(id -u)/dev.prxref.gate` (and the tunnel). Push to a test PR and check
the app's Advanced tab: recent deliveries should show 202.

## Operate

- **Logs:** one log group per function, 14 days.
- **Pause:** `aws dynamodb put-item --table-name <table> --item '{"pk":{"S":"META"},"sk":{"S":"PAUSED"},"paused":{"N":"1"}}'`
  (`"0"` resumes). Deliveries still queue; the worker leaves them queued and the watchdog
  resends them after resume. A paused gate leaves PRs blocked.
- **Dead letters:** a message lands in the DLQ after three failed receives (a crash, or the
  lease held by a dead invocation; redelivery waits the 90-minute visibility timeout). The row
  stays `queued` in the table, so the watchdog sends it again; the DLQ is a record, not the only
  copy.
- **Secret rotation:** put the new value with `aws secretsmanager put-secret-value --secret-id <arn>
  --secret-string file://...`, change it in the app settings, then force new containers with
  `aws lambda update-function-configuration` (any environment change) or wait for them to recycle.
  There is no previous-secret grace on Lambda; redeliver any delivery that failed in between
  from the app's Advanced tab.

## Rollback

1. Point the app's webhook URL back at the tunnel (`https://<webhook-host>/webhook`) and restart
   the local gate: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.prxref.gate.plist`
   and the tunnel plist. Generations queued on AWS are not carried over; the local watchdog's
   reconcile picks up any open PR whose head it has not reviewed.
2. To turn the AWS gate off entirely: remove the required `sdlc-review` check from the ruleset
   first (docs/gate.md, Rollback), then disable the worker's SQS trigger
   (`aws lambda update-event-source-mapping --uuid <id> --no-enabled`) and the webhook, then
   `sam delete`. The table is retained; delete it by hand when you no longer want the history.
