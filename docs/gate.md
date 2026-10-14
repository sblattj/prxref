# sdlc review gate (GitHub App)

The gate turns a prxref review into a **required, merge-blocking check**. A GitHub
App (default name `prxref-sdlc`; operators may pick another, and the bot login is `<slug>[bot]`) owns a check run named `sdlc-review`; a repository ruleset requires
that check *from that app* (by `integration_id`), so a status or a check with the
same name from anyone else does not satisfy it. The advisory lane (`prxref serve`,
`prxref review`, `action.yml`) is unchanged and never blocks.

One process (`prxref gate serve`) runs three threads on one SQLite file:

- the **receiver** (`POST /webhook`, `GET /health`): verifies the signature, checks
  the installation and repository allowlists, de-duplicates deliveries and queues a
  *generation* (one review of one head against one base);
- the **worker**: one review at a time under a fenced lease, with a hard deadline;
  it reads `.prxref.toml`, rule packs and standards from the PR's **base** commit
  (`PRXREF_RULES_SOURCE=base`), so a PR cannot weaken the rules that judge it;
- the **watchdog**: re-queues work a dead worker left behind and reconciles open
  PRs against the queue every `PRXREF_GATE_RECONCILE_MINUTES`.

Before every write the worker re-reads the PR. If the head, the base branch or the
base commit moved, or the PR closed, the result is dropped and a fresh generation
reviews what is live.

## Verdicts

| Review outcome | Check conclusion |
|---|---|
| Approved | `success` |
| Request-Changes | `failure` |
| Incomplete (a chunk, the sweep or a model failed) | `failure` |
| Error, deadline hit, crash | `failure` |
| Superseded or stale (the PR moved) | nothing published; a newer generation owns the head |
| PR closed | `cancelled`, only if a check was already created |

Every published outcome posts one review plus the summary comment: a pass posts
`APPROVE`, any other completed outcome posts `REQUEST_CHANGES`. The failure and
cancel paths (deadline, crash, publish error, PR closed) post no review. The required
check, pinned by `integration_id`, is the gate nobody can dismiss; the app's approval
is the second lock. An owner or admin can dismiss the app's review (the ruleset
`dismissal_restriction` is unavailable on personal free-plan repositories). Dismissing
an `APPROVE` returns the PR to review-required, while the required check still blocks
a failing PR. The installation needs `contents: write`, because an app's approval counts
toward required reviews only then; the token the gate mints asks only for
`contents: read`. A review that cannot finish fails closed: the check concludes
`failure`, never `neutral` or `skipped`.

## Prerequisites

1. **Install the extra:** `uv tool install 'prxref[github-app,litellm]==0.36.0'`. Without
   `github-app`, `prxref gate serve` exits 2 naming the extra.
2. **Tunnel:** a Cloudflare zone, `cloudflared tunnel login`, `cloudflared tunnel create sdlc`,
   and `cloudflared tunnel route dns sdlc <webhook-host>`. The webhook URL is
   `https://<webhook-host>/webhook`. The named tunnel needs an ingress rule that sends
   that hostname to the gate; write `~/.cloudflared/config.yml` (`<tunnel-uuid>` is the
   id `cloudflared tunnel create` printed, and its credentials file sits beside it):

   ```yaml
   tunnel: sdlc
   credentials-file: /Users/<you>/.cloudflared/<tunnel-uuid>.json
   ingress:
     - hostname: <webhook-host>
       path: ^/webhook$
       service: http://127.0.0.1:8787
     - service: http_status:404
   ```

   Only `/webhook` goes through the tunnel; everything else on the hostname, `/health`
   included, falls to the 404 rule. Check it with `cloudflared tunnel ingress validate` and
   `cloudflared tunnel ingress rule https://<webhook-host>/webhook` (prints the
   `http://127.0.0.1:8787` rule).
3. **Model key:** a *long-term* Amazon Bedrock API key (an IAM user with
   `AmazonBedrockLimitedAccess`, created in the Bedrock console). A 12-hour short-term key
   expires under the daemon and every review after that fails closed.
4. **Register the app:** `prxref gate render-manifest --webhook-url https://<webhook-host>/webhook > manifest.json`,
   then create the app from that manifest (GitHub → Settings → Developer settings →
   GitHub Apps → New, or the manifest flow). Install it on the target repositories
   only, and confirm the installation shows **Repository contents: Read and write**
   (accept the permission if GitHub asks). Without it the app's `APPROVE` does not count
   toward the approval rule and every passing PR stays at review-required. Download its private key to `~/.config/prxref-gate/sdlc.pem` and run
   `chmod 600` on it. Note the app id and the installation id.
5. **Apply the ruleset:** `prxref gate render-ruleset --integration-id <app-id>` shows it;
   `prxref gate apply-ruleset --repo <owner/name> --integration-id <app-id>` creates or
   replaces the ruleset named `sdlc-review` through `gh api` (your `gh` login needs admin on
   the repository). `prxref gate apply-ruleset --repo <owner/name> --integration-id <app-id> --diff`
   prints any drift and exits 1 when there is some. With more than one `gh` account
   configured, run it as
   `GH_TOKEN=$(gh auth token --user <account that owns the repo>) prxref gate apply-ruleset …`,
   because `gh auth switch` is process-global.

## Install

Copy `examples/launchd/prxref-gate.env.example` to `~/.config/prxref-gate/env`, fill it
in, and `chmod 600` it; the gate refuses an env file anyone else can read. It holds:

| Variable | Meaning |
|---|---|
| `PRXREF_GATE_APP_ID` | the app's numeric id (also the ruleset's `integration_id`) |
| `PRXREF_GATE_PRIVATE_KEY_FILE` | path to the app's `.pem`, mode 0600 |
| `PRXREF_GATE_INSTALLATION_IDS` | comma-separated installation ids the gate accepts |
| `PRXREF_GATE_REPOS` | comma-separated `owner/name` repositories the gate accepts |
| `PRXREF_GATE_WEBHOOK_SECRET` | the app's webhook secret |
| `PRXREF_GATE_WEBHOOK_SECRET_PREVIOUS` | the previous secret, during a rotation only |
| `PRXREF_GATE_SECRET_GRACE_MINUTES` | how long after start the previous secret is honored (30) |
| `PRXREF_GATE_STATE_FILE` | the SQLite state file |
| `PRXREF_GATE_RUNS_DIR` | where each review's run record is written (mode 0700); the check summary names it as `runs/<generation id>` relative to this directory |
| `PRXREF_GATE_REVIEW_DEADLINE` | seconds a review may take before it fails closed (900) |
| `PRXREF_GATE_WATCHDOG_SECONDS` | watchdog interval (60) |
| `PRXREF_GATE_RECONCILE_MINUTES` | open-PR reconciliation interval (10) |

plus the model settings (`PRXREF_LLM_BACKEND=litellm`, `PRXREF_LLM_MODELS`,
`PRXREF_LLM_TIMEOUT=900`, `AWS_BEARER_TOKEN_BEDROCK`, `AWS_REGION_NAME`).
`PRXREF_RULES_SOURCE` defaults to `base` under `prxref gate serve`; leave it unset.

Copy `examples/launchd/dev.prxref.gate.plist` and `examples/launchd/dev.prxref.cloudflared.plist`
to `~/Library/LaunchAgents/`, replace `__HOME__` with your home directory, then:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.prxref.gate.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.prxref.cloudflared.plist
curl -s http://127.0.0.1:8787/health
```

Operate it with `prxref gate status --env-file ~/.config/prxref-gate/env` (pause state,
queue depth, lease, last publish, last authentication failure, the last ten
generations), `prxref gate pause --env-file …` (webhooks still queue; nothing is
dequeued) and `prxref gate resume --env-file …`. A paused gate leaves PRs blocked.

## Rotation

- **Webhook secret:** generate a new secret; in the env file set
  `PRXREF_GATE_WEBHOOK_SECRET_PREVIOUS` to the old value and `PRXREF_GATE_WEBHOOK_SECRET`
  to the new one; restart (`launchctl kickstart -k gui/$(id -u)/dev.prxref.gate`); change
  the secret in the app settings. Deliveries signed with the old secret are accepted for
  `PRXREF_GATE_SECRET_GRACE_MINUTES` after the restart. Then remove the previous line and
  restart again.
- **App private key:** generate a second key in the app settings, replace the `.pem`
  (keep mode 0600), restart, confirm `/health`, then delete the old key in the settings.
- **Bedrock key:** replace the `AWS_BEARER_TOKEN_BEDROCK` line and restart.

## Local limits

The gate runs on one Mac. Sleep, shutdown and tunnel loss pause intake. GitHub
retries a failed delivery for a bounded time, and the app's "Recent Deliveries" page
has a **Redeliver** button for the rest. While the gate is away, the required check
is missing, so merges stay blocked. When the gate returns, the watchdog reconciles
every open PR and queues a review for any head without one.

A verdict already published describes the head against the base it saw. If the base
branch moves afterwards, the ruleset's strict policy ("require branches to be up to
date") refuses the merge until the PR is updated, and that update is a new push, so a
new generation reviews the new head against the new base. A base retarget
(`pull_request.edited`) queues a new generation at once; if that delivery is lost, the
next reconcile (`PRXREF_GATE_RECONCILE_MINUTES`) or push repairs it.

A clean stop (`launchctl bootout`, SIGTERM, Ctrl-C) while the worker is idle releases
the worker lease at once. A stop during a review waits up to 5 s for the worker, then
exits with the lease still held and the generation `in_progress`: the worker releases
the lease only when it reaches the end of its loop, which a running review prevents.
The next start on the same host adopts a lease whose holder pid is dead at once and
re-queues the in-progress generation, the same as after a crash (kill -9, power loss).
The lease TTL (review deadline + 120 s) matters only when the holder process is alive
but stuck. A review cut off by a stop is wasted work; run `prxref gate pause` before a
planned restart to avoid it, and `prxref gate resume` afterwards.

## Troubleshooting

- **Key expired.** Every review fails with an authentication error in the check summary,
  and `prxref gate status` shows it under "last auth failure". Replace the Bedrock key
  (see Rotation).
- **401 on every delivery.** The webhook secret in the env file and the app differ.
- **403 on a delivery.** The installation id or the repository is not in
  `PRXREF_GATE_INSTALLATION_IDS` / `PRXREF_GATE_REPOS`.
- **Check stuck `in_progress`.** A stop during a review leaves the lease held and the
  generation `in_progress`, but a restart on the same host adopts the dead holder's
  lease (same host, dead pid) at once and re-queues the work. Only a process that is
  alive but stuck holds the lease until it expires (review deadline + 120 s), and only
  then does the watchdog re-queue. Re-run from the PR's Checks tab ("Re-run") to queue
  it immediately.
- **`/health` answers 404 through the tunnel.** By design: it is local-only because it
  shows the lease holder and the last authentication failure. Use
  `curl http://127.0.0.1:8787/health` on the Mac.

## Rollback

Order matters: remove the ruleset first, or every merge stays blocked.

1. `gh api -X DELETE repos/<owner>/<name>/rulesets/<id>` (find the id with
   `gh api repos/<owner>/<name>/rulesets`).
2. `launchctl bootout gui/$(id -u)/dev.prxref.gate` and
   `launchctl bootout gui/$(id -u)/dev.prxref.cloudflared`.
3. Uninstall the app from the repository: GitHub → Settings → Applications →
   Installed GitHub Apps → the app's name → Configure, or
   `gh api -X DELETE /app/installations/<installation-id>` with an app JWT.
