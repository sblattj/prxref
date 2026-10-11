# sdlc review gate: design

Issue: #102 "Local GitHub App review gate with durable recovery and source-pinning proof".
Date: 2026-10-08. Base: prxref 0.34.0 (`0d7d2e1`).

## Goal

A GitHub App (default name `prxref-sdlc`) that reviews every pull request on an installed repository
with prxref, publishes an app-owned check run named `sdlc-review`, and is pinned as a
required status check in a ruleset so that a PR cannot merge until the app's own check
succeeds and one fresh human approval exists. The receiver, durable queue and worker run
on one Mac behind an HTTPS tunnel. Inference comes from Amazon Bedrock.

The existing advisory automation (`.github/workflows/prxref-review.yml`, the `prxref
serve` daemon, the composite action) stays intact and unchanged. Its check context is the
Actions job "prxref review"; the gate's is the app check `sdlc-review`.

## Decisions and their evidence

| Decision | Choice | Evidence |
|---|---|---|
| Where the gate lives | Inside prxref as `prxref gate …`, new package `src/prxref/gate/` | User choice 2026-10-08. Reuses `webhooks.py` verification, the orchestrator, tests, release machinery. |
| "sdlc as CODEOWNER" | Not possible; replaced by an app-pinned required check | GitHub docs: CODEOWNERS entries must be users or teams with write access. A ruleset `required_status_checks` entry carries an `integration_id`, which pins the check to the app. |
| Model | `us.anthropic.claude-opus-4-6-v1`, one model, no fallback, medium effort | Opus 5.5, 5, 4.8, 4.7, Sonnet 5.x, Haiku 5.5 and Fable return "not available for this account, contact AWS Sales" on this account. Opus 4.6 answered after the Anthropic use-case form and agreement landed (2026-10-08 16:19 PT). |
| Bedrock route | prxref's existing `litellm` backend, `bedrock/us.anthropic.claude-opus-4-6-v1`, `AWS_BEARER_TOKEN_BEDROCK`, region `us-west-2` | Bedrock's OpenAI-compatible endpoint returns 404 `model_not_found` for every Claude id, so the `http` backend cannot be used. litellm authenticated with the bearer key. `seed` is rejected by Bedrock, so `PRXREF_LLM_SEED=off`. Thinking needs temperature 1, so `PRXREF_LLM_TEMPERATURE=1`; prxref sends 0 by default. Verified 2026-10-08: `reasoning_effort=medium` through litellm returned a reasoning block (367 chars) and 415 completion tokens where the plain call returned 240, so the effort setting is honoured, not merely accepted. |
| Request budget | `PRXREF_LLM_TIMEOUT=900` and `PRXREF_LLM_MAX_TOKENS=32000` set explicitly in the gate env | The prompt-scaled deadline covers the openai-compat family; the gate does not rely on it for litellm. The advisory workflow already hit `finish_reason=length` at the 4096 default on a reasoning model. |
| Webhook loss | A reconciler that lists open PRs is load-bearing, not a backstop | GitHub docs: "GitHub does not automatically redeliver failed deliveries." A push during sleep, shutdown or tunnel loss produces no delivery at all. |
| Version pin | The release that ships the gate (0.35.0); the sandbox pins it | Issue text says 0.31.0, which has none of the gate code. |
| Sandbox | New public repo `sblattj/sdlc-sandbox`; one identity only: the repo owner plus the app, no second account | Free plan: rulesets work on public repos only. A PR author cannot approve their own PR, so the app is the only possible approver in a solo repo. |
| Tunnel | `cloudflared` named tunnel as a launchd service | Installed; stable hostname; survives restarts. |
| Rules source | Base SHA for `.prxref.toml`, rulepacks, standards; head SHA only for inert file context | Today `orchestrator` builds the forge reader at `pr.source_sha` (head). The issue requires base. prxref never executes repository content, so "no head code execution" already holds. |
| Spec vs plan | Seven points above follow the plan's "Spec questions" section | docs/design/2026-10-08-sdlc-review-gate-plan.md, "## Spec questions" items 1, 2, 3, 8, 14, 21, 26 |

## Non-goals

- No AWS or cloud deployment. No Docker image changes.
- No change to the advisory lane, its workflow, or the composite action.
- No change to the existing `prxref serve` daemon beyond sharing verification helpers.
- No multi-forge gate: GitHub only. Other forges keep the advisory daemon.
- No all-threads-resolution rule, no bypass actors, no code-owner review requirement.
- No claim of review independence: one person operates both write identities.

## Architecture

```
GitHub ──webhook──▶ cloudflared ──▶ receiver (HTTP, <10 s) ──▶ SQLite (WAL)
                                                                   ▲   │
                                      watchdog ◀───────────────────┘   ▼
GitHub ◀──checks/reviews (installation token)── publisher ◀── worker (lease) ──▶ orchestrate_review(CapturingForge) ──▶ litellm ──▶ Bedrock
```

One process, `prxref gate serve`, hosts three threads: the receiver's HTTP server, the
worker, and the watchdog. A second launchd service runs `cloudflared`. All durable state
is one SQLite file. The worker never runs more than one review at a time; publication is
serialized through the worker thread and fenced by the lease token.

## Components

Each component is one module in `src/prxref/gate/` with one concern, a public function
or class, and a test file under `tests/gate/`.

### `app_auth.py`: installation tokens

- Does: builds the app JWT (RS256, 10-minute expiry, issued 60 s in the past for clock
  skew), exchanges it for an installation token scoped at mint to the one repository and
  to `checks: write`, `pull_requests: write`, `contents: read` (the token asks for read even though the app *installation* must hold `contents: write`; a read-scoped token on a write installation still approves), and caches it until 5
  minutes before its one-hour expiry.
- Used by: the publisher and the binding guard; exposes `token_for(repo_full_name)`.
- Depends on: `pyjwt[crypto]` from the new optional extra `github-app`; `requests`.
  `prxref gate serve` exits 2 naming the `github-app` extra when PyJWT is missing;
  `render-manifest`, `render-ruleset`, `pause`, `resume`, `status` and `--help` work
  without it.
- Inputs: `PRXREF_GATE_APP_ID`, `PRXREF_GATE_PRIVATE_KEY_FILE` (PEM path, mode 0600
  enforced at start), `PRXREF_GATE_INSTALLATION_IDS` (allowlist),
  `PRXREF_GATE_REPOS` (required repository allowlist, an `owner/name` list).

### `receiver.py`: webhook intake

- Does: `POST /webhook` reads the raw body, verifies `X-Hub-Signature-256` with
  `PRXREF_GATE_WEBHOOK_SECRET` (reusing `webhooks._check_hmac`), rejects any delivery
  whose `installation.id` is outside `PRXREF_GATE_INSTALLATION_IDS` or whose
  `repository.full_name` is outside `PRXREF_GATE_REPOS`, dedups on
  `X-GitHub-Delivery`, maps the event to an intent, writes one row, and answers 202 with
  the delivery id. Every reply is sent within the receiver's own 5 s budget; no GitHub
  call and no inference happens here. `GET /health` reports queue depth, lease holder,
  last publish time and paused state.
- Event mapping:
  - `pull_request` `opened`, `synchronize`, `reopened`, `ready_for_review`: new generation.
  - `pull_request` `edited` with a `changes.base` entry: new generation (base retarget).
  - `pull_request` `closed`: cancel open generations for that PR; nothing is published.
  - `check_run` `rerequested` and `check_suite` `rerequested` for this app: new generation
    with reason `rerequest`, reusing the current head and base.
  - `ping`, `installation`, everything else: 202 and ignored, logged at INFO.
- Secret rotation: `PRXREF_GATE_WEBHOOK_SECRET_PREVIOUS` is accepted for a bounded window
  (`PRXREF_GATE_SECRET_GRACE_MINUTES`, default 30) so the app's secret can be rotated
  without dropped deliveries. A delivery that only matches the previous secret is logged.

### `store.py`: durable state

SQLite, WAL mode, `synchronous=FULL`, one file at `PRXREF_GATE_STATE_FILE`.

```
deliveries(delivery_id PK, received_at, event, action, repo, pr_number, handled)
generations(id PK, repo, pr_number, head_sha, base_sha, base_ref, gen INTEGER,
            reason, state, created_at, started_at, finished_at, check_run_id,
            external_id, conclusion, error, attempts)
leases(name PK, holder, token INTEGER, expires_at)
publications(generation_id PK, kind, github_id, posted_at)
```

- `gen` is monotonic per `(repo, pr_number)`. Inserting a generation marks every older
  `queued` generation for the same PR `superseded` in the same transaction.
- `state` is one of `queued`, `in_progress`, `completed`, `failed`, `superseded`,
  `stale`, `cancelled`.
- The worker lease is a row with a fencing `token`; every publish carries the token the
  worker holds, and the store refuses a state transition from a holder whose token is
  not current. A second `prxref gate serve` on the same file cannot publish.
- Restart recovery is a query: on start, a fresh worker that breaks a dead holder's
  lease re-queues every `in_progress` generation under its own fencing token
  (`Store.adopt_orphans`), because breaking the lease makes it live again and an
  "expired lease" test would never fire. The re-run reuses the stored `check_run_id`,
  so the same check moves from `in_progress` to its conclusion instead of leaving an
  orphaned in-progress check beside a new one.

### `worker.py`: review execution

- Does: loops on the oldest `queued` generation that is the newest for its PR, marks it
  `in_progress`, creates the check (`queued` then `in_progress`), runs the review, and
  hands the result to the publisher. One review at a time; a `PRXREF_GATE_REVIEW_DEADLINE`
  (default 15 min) wraps the review, and a deadline hit is a failure.
- Review call: the review runs in-process with posting ON over a capturing forge adapter
  (`CapturingForge`) that records the summary and inline-comment post calls and writes
  nothing to GitHub; the publisher then posts from the captured bodies, so every GitHub
  write still belongs to the publisher. The forge adapter is built on the installation
  token and the LLM client by the existing factory from the gate's environment. The
  result dict and `_run_record` are persisted under `PRXREF_GATE_RUNS_DIR/<generation
  id>/` with token counts.
- `prxref gate pause` writes a `paused` flag row; the worker finishes the current
  generation and stops dequeuing. `resume` clears it. This is the kill switch.

### `rules_source.py`: base-pinned rules (orchestrator change)

- Does: a second `forge_reader` at `pr.target_sha` used for `.prxref.toml`, rulepacks
  (`PRXREF_RULES_FILE` in-repo paths), standards documents and learnings. The head reader
  stays for out-of-hunk context and file content.
- `.prxref.toml` and every in-repo path it names (`config._FILE_PATH_KEYS`, which
  includes `learnings_file`) are fetched at the base SHA into a temporary directory,
  because `load_config` reads only local paths; `prompts_dir` is forced off in the gate.
- Surface: a new config key `PRXREF_RULES_SOURCE` with values `head` (today's behavior,
  default for `review` and `serve`) and `base` (the gate's default). Added to
  `config._DEFAULTS`, `_CHOICE_KEYS`, the docstring, `.env.example`, `docs/env-vars.md`,
  and classified `ENV_ONLY_KEYS`.
- Proof: a test where head edits `.prxref.toml` to disable a rule; under `base` the rule
  still fires, under `head` it does not.

### `checks.py`: publisher

- Does: creates and updates the `sdlc-review` check run with `external_id = generation
  id`, `details_url` set to the PR's checks tab URL (GitHub requires http(s); the local
  run record path goes in the summary text), and a summary that names the model id,
  effort, token counts, verdict, chunk counts and the base and head SHAs reviewed.
  The app submits a pull request review from `<slug>[bot]` (default `prxref-sdlc[bot]`,
  derived at runtime from the app slug): event `APPROVE` when the review conclusion is
  `success`, `REQUEST_CHANGES` on any other completed outcome, with the actionable inline
  findings riding on that review. If GitHub refuses the anchors (422), the same event is
  re-posted with no comments and the comments are demoted to check-run annotations. The
  `fail()` and `cancel()` paths post no review; the failing or cancelled required check
  blocks. The app's APPROVE is what satisfies the ruleset's one-approval rule. File-level findings become check annotations (max 50
  per update, batched).
- Comment bodies come from prxref's own `formatter` and `markers`, so the next run's
  thread dedup and "Previously raised" logic recognise the gate's comments exactly as
  they recognise the advisory lane's. The executive summary is posted as the check
  output and also as the usual prxref summary comment (same marker), so a reader sees
  one summary per head, authored by `<slug>[bot]` (`prxref-sdlc[bot]` by default).
- Uncertain replies: on a timeout or 5xx while creating a check or review, the publisher
  first lists check runs for the head SHA filtered by `app_id` and `external_id`, and
  reviews by the bot login, before retrying, so a duplicate is never created.
- Every publish passes the lease token to the store; a mismatch aborts before any
  GitHub write.

### `binding.py`: live binding guard

- Does: immediately before every GitHub write, re-reads the PR. The generation publishes
  only if the live head SHA, base SHA and base ref equal the generation's, the PR is
  open, and no newer generation exists for the PR. Otherwise the generation becomes
  `stale` and the check is left as is (a newer generation owns the new head).
- Retarget window: a base-only change arrives as `pull_request.edited`; the receiver
  has already queued a new generation for the same head with the new base, so the guard
  marks the old generation `stale` and the new one publishes a fresh check on that head
  (GitHub shows the latest check run of a name per SHA). The ruleset's strict policy
  also demands the head be up to date with the new base, so in practice a retarget needs
  a fresh head before merge; the runbook says so. The window after a concluded check is
  closed by the ruleset's strict policy (a moved base forces an update push, which
  starts a new generation) together with the receiver's `edited` handling; the watchdog
  and reconciler compare heads only and do not notice a moved base after a check
  concluded.

### `watchdog.py`: repair and reconciliation

- Does, every `PRXREF_GATE_WATCHDOG_SECONDS` (default 60): re-queues `in_progress`
  generations past the deadline whose lease is not live; and, every
  `PRXREF_GATE_RECONCILE_MINUTES` (default 10) and on start, lists open PRs on every
  allowlisted repository (from `PRXREF_GATE_REPOS`) and enqueues a generation for any
  PR whose live head has no generation in the store. This is the recovery path for a push that happened while the
  Mac slept, was shut down, or the tunnel was down, because GitHub never redelivers on
  its own. Polling is bounded to one list call per repo per interval plus one PR read
  per discrepancy. No webhook-redelivery walker: the reconciler covers every missed
  delivery, and the manual "Redeliver" button in the App's delivery log covers the rest.

### `rulesets.py`: manifest and ruleset renderer

- `prxref gate render-manifest` prints the GitHub App manifest JSON for the manifest flow:
  name `prxref-sdlc`, webhook URL, events `pull_request`, `check_run`, `check_suite`,
  permissions `checks: write`, `pull_requests: write`, `contents: write`,
  `metadata: read`.
- `prxref gate render-ruleset --integration-id N` prints the ruleset JSON:
  - target `branch`, include `~DEFAULT_BRANCH`, enforcement `active`, `bypass_actors: []`
  - `required_status_checks`: `[{context: "sdlc-review", integration_id: N}]`,
    `strict_required_status_checks_policy: true`
  - `pull_request`: `required_approving_review_count: 1`,
    `dismiss_stale_reviews_on_push: true`, `require_last_push_approval: true`,
    `require_code_owner_review: false`, `required_review_thread_resolution: false`
  - The installation needs `contents: write`: probes showed that with installation
    `contents: read` an App APPROVE left `reviewDecision=REVIEW_REQUIRED`, and with
    `contents: write` it became `APPROVED` / `CLEAN`.
  - Accepted limitation: the repo owner or an admin can dismiss the app's review. Ruleset
    `dismissal_restriction` is not available on a personal free-plan repository (the PUT
    is silently dropped). Dismissing the APPROVE returns the PR to `REVIEW_REQUIRED`; a
    dismissed REQUEST_CHANGES unblocks the review requirement, but the failing required
    check still blocks the merge.
- `prxref gate apply-ruleset --repo owner/name` posts it with the operator's own `gh`
  token and prints the ruleset id; `--diff` shows the live ruleset against the rendered
  one for the bypass audit.

### `cli.py` additions

`prxref gate serve|pause|resume|status|render-manifest|render-ruleset|apply-ruleset`.
`status` prints queue depth, lease, last publish and the last ten generations.

## Verdict mapping

| Review outcome | Check conclusion | Review event | Notes |
|---|---|---|---|
| `Approved` | `success` | `APPROVE` | Warnings attached as comments, non-blocking. |
| `Request-Changes` (error-severity findings) | `failure` | `REQUEST_CHANGES` | Same rule as `PRXREF_FAIL_ON=error`. |
| `Incomplete`, `degraded`, failed chunks | `failure` | `REQUEST_CHANGES` | Partial review is not a review. |
| `Error` (LLM, forge, config) | `failure` | none (`fail()` path) | Summary carries the error class. |
| Deadline hit, worker crash, malformed result | `failure` | none (`fail()` path) | Watchdog or restart publishes it. |
| Generation superseded or stale | no publish | none | Newer generation owns the head. |
| PR closed | `cancelled` | none (`cancel()` path) | Only if a check was already created. |

## Credentials and secrets

- Model credential (`AWS_BEARER_TOKEN_BEDROCK`) and app credentials (private key file,
  webhook secret) are separate variables, loaded from `~/.config/prxref-gate/env`
  (mode 0600, checked at start) by the launchd plist. Nothing is read from the repo.
- The SQLite file, run records and logs never contain tokens: the publisher logs
  request ids and status codes only; the run record's `sampling` block has no key.
- Rotation (runbook): webhook secret via the previous-secret grace window; app private
  key by generating a second key in the App settings, swapping the file, restarting, and
  deleting the old key; Bedrock key by replacing the env line and restarting.
- Bedrock key lifetime: the key used for this design's probes is a 12-hour short-term
  key. The daemon needs a long-term Bedrock API key (an IAM user with
  `AmazonBedrockLimitedAccess`, created in the Bedrock console) as an operator
  prerequisite. An expired key surfaces as an `Error` verdict and a failed check, which
  fails closed; `/health` and `prxref gate status` also report the last Bedrock
  authentication failure so the runbook's "key expired" entry is observable before the
  next PR.

## Operator prerequisites (user-owned, before phase 1)

- Cloudflare: a zone on a Cloudflare account and `cloudflared tunnel login` on this Mac
  (`~/.cloudflared/cert.pem` does not exist today, and `cloudflared tunnel list` fails on
  it). Then `cloudflared tunnel create sdlc` and a DNS route for the webhook hostname.
- A long-term Bedrock API key for the daemon, as above.
- No second GitHub account: the repo owner's own `gh` login posts the foreign status in
  phase 1, and the app is the approver.
- Registering the `prxref-sdlc` GitHub App from the rendered manifest and installing it on the
  sandbox only; downloading its private key to the mode-0600 path.

## Operations

- `launchd`: `dev.prxref.gate.plist` (`prxref gate serve --env-file ~/.config/prxref-gate/env`)
  and `dev.prxref.cloudflared.plist` (`cloudflared tunnel run sdlc`). Both `KeepAlive`.
  Every `PRXREF_GATE_*` key is env-only, so a `.prxref.toml` cannot carry them.
- Local limits documented in the runbook: sleep, shutdown and tunnel loss pause intake;
  GitHub retries deliveries for a bounded time and the "Redeliver" button covers the
  rest; a missing required check blocks merge until the worker returns; the watchdog
  reconciles on return.
- Rollback: delete the ruleset (`gh api -X DELETE …/rulesets/ID`), unload the plists,
  uninstall the app from the repo. Order matters: ruleset first, or merges block.

## Testing

Unit tests, fake GitHub and fake LLM, under `tests/gate/`, one file per component:

- signature valid, invalid, missing, previous-secret within and outside grace
- allowlist: unknown installation, unknown repo
- duplicate delivery id, out-of-order delivery, supersede on second push
- rerequest creates a generation with the same head
- closed PR cancels; reopened creates
- retarget (`edited` with base change) makes the current generation stale
- worker: deadline, crash mid-review, malformed result, degraded result
- publisher: 5xx on create then found-by-external-id, lease token mismatch aborts
- restart: `in_progress` with expired lease re-queues; with live lease it does not
- rules source: base vs head test described above
- renderer: golden files for manifest and ruleset JSON

## Live proof (sandbox)

Repository `sblattj/sdlc-sandbox`, public, with the ruleset applied and the app installed
only there. Identities: the repo owner (`sblattj`) and the app. Keep the
repo, PR, base, head, approval and ruleset constant across phases 1 and 2.

1. Post a success *status* named `sdlc-review` from the repo owner's own `gh` login (an
   account that is not the app) on the head. Capture: the ruleset, the status, the PR
   `mergeable_state` showing `blocked`, proving the `integration_id` pin rejects a
   same-named status from a non-app source.
2. Let the app review the same head. Capture: the check run with `app.id` equal to the
   ruleset `integration_id`, `conclusion: success`, and `mergeable_state: clean`.
3. Push a new commit. Capture: the previous approval dismissed, a new check run on the
   new head, `mergeable_state: blocked` until re-approval and the new check.
4. Failure cases, scripted against the live receiver: bad signature (401), an induced
   incomplete review (`failure`), deadline, duplicate delivery, rerequest, stale head,
   close and reopen, a simulated 5xx on publish, a worker restart mid-review, and a
   webhook secret rotation.

Each capture is a `gh api` JSON file in `docs/live-instance-verification/sdlc-gate/`
with the commit SHAs, ruleset id, app id and check run id.

## Delivery

- Branch `sdlc-review-gate`, draft PR to `sblattj/prxref` before any ruleset is applied
  to prxref itself. The prxref repository gets the gate only after the sandbox proof.
- Release 0.35.0 after merge; changelog links #102; the sandbox pins 0.35.0.
- Issue #102 gets a comment recording the Opus 5.5 Sales gate, the Opus 4.6
  substitution, and the 0.35.0 pin.
- Docs: `docs/gate.md` (runbook), `docs/env-vars.md` rows for every `PRXREF_GATE_*`
  key, `docs/config-file.md` classification, `README.md` section.

## Open assumptions

- The litellm backend's per-request deadline is taken from `PRXREF_LLM_TIMEOUT` as set;
  the implementation plan includes reading `LiteLLMClient` to confirm no lower internal
  cap applies, and a test that a 900 s setting reaches the litellm call.
- Opus 4.6 remains the model until Opus 5.5 access is granted by AWS Sales; the swap is
  a one-variable change and a re-run of the live proof's phase 2.
