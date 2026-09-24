# Deployment Guide for prxref

`prxref` runs in two modes:
1. **Daemon Mode (`prxref serve`)**: An HTTP webhook listener running on port 8080.
2. **CLI / One-shot Review (`prxref review`)**: Run directly in CI/CD pipelines or manually against a PR/MR URL.

For the full list of supported environment variables and credentials, refer to `docs/env-vars.md`. If you are wiring `prxref review` into a pipeline, read [Exit Codes in CI/CD](#5-exit-codes-in-cicd) first — the exit code does not mean what a linter's exit code means.

---

## 1. Docker Deployment (Recommended)

### Build the Image

```bash
docker build -t prxref:latest .
```

### Run with Docker Compose

1. Prepare your environment file with forge tokens, webhook secrets, and LLM configuration (referencing `docs/env-vars.md` or `.env.example`).
2. Start the service:

```bash
docker compose up -d
```

### Run with Docker CLI

```bash
docker run -d \
  --name prxref \
  --restart unless-stopped \
  -p 8080:8080 \
  --env-file .env \
  prxref:latest
```

---

## 2. Webhook Registration

Point your forge webhooks to:
`http://<host>:8080/webhook`

Configure secret tokens and match the events accepted by `prxref`:

| Forge | Secret Env Var | Reviewable Events | Notes |
|---|---|---|---|
| **GitHub** | `PRXREF_GITHUB_WEBHOOK_SECRET` | `Pull request` (actions: `opened`, `synchronize`) | HMAC-SHA256 in `X-Hub-Signature-256` |
| **Bitbucket Cloud** | `PRXREF_BITBUCKET_WEBHOOK_SECRET` | `Pull Request: Created` (`pullrequest:created`), `Pull Request: Updated` (`pullrequest:updated`) | HMAC-SHA256 in `X-Hub-Signature` |
| **Bitbucket Server / Data Center** | `PRXREF_BITBUCKET_WEBHOOK_SECRET` (the same secret as Cloud) | `pr:opened`, `pr:modified`, `pr:from_ref_updated` (the source branch moved) | HMAC-SHA256 in `X-Hub-Signature`; the same `X-Event-Key` header as Cloud, told apart by event name and payload shape |
| **GitLab** | `PRXREF_GITLAB_WEBHOOK_SECRET` | `Merge request events` (actions: `open`, `update`) | Secret token in `X-Gitlab-Token` header |
| **Azure DevOps** | `PRXREF_AZURE_DEVOPS_WEBHOOK_SECRET` | `git.pullrequest.created`, `git.pullrequest.updated`, on a PR whose status is `active` | No event header and no signature: recognized by `publisherId: "tfs"` in the body, authenticated by the HTTP Basic auth password (the user name is ignored). Subscriptions: see [Azure DevOps service hooks](#azure-devops-service-hooks) |

*Note on Insecure Development Bypass:* Setting `PRXREF_ALLOW_UNSIGNED=1` allows unsigned payloads for local testing. Do not use in production.

*First deployment:* set `PRXREF_DRY_RUN=1` before pointing webhooks at a busy repository. The daemon then runs every review in full — fetch, chunk, LLM calls, quality gate — and writes nothing back to the forge, so you can read the logs and confirm the review is sane before it starts commenting. Unset it when you are satisfied. This is the only way to observe the daemon against real traffic: `--no-post` covers a single CLI invocation, and `serve` takes only `--host`/`--port`, so the daemon has no flag-based equivalent.

### Azure DevOps service hooks

Azure DevOps sends no event header and no signature. prxref recognizes its service hooks by the JSON body (`publisherId: "tfs"`) and authenticates them with HTTP Basic auth: the password must equal `PRXREF_AZURE_DEVOPS_WEBHOOK_SECRET` (compared in constant time), and the user name is ignored. With the secret unset, every Azure DevOps webhook gets `401` unless `PRXREF_ALLOW_UNSIGNED=1`.

In **Project settings → Service hooks**, create two **Web Hooks** subscriptions:

| Subscription | Trigger | Filters |
|---|---|---|
| Pull request created (`git.pullrequest.created`) | a PR is opened | repository and target branch, as you like |
| Pull request updated (`git.pullrequest.updated`) | a PR changes | **Change: Source branch updated** |

Set the **Change** filter on the updated subscription. Without it, every reviewer vote, status change and description edit triggers a full re-review: the receiver cannot tell those updates from a push, so it relies on the subscription to filter them.

On the **Action** page of each subscription:
- **URL:** `https://<host>/webhook`, with TLS in front of `prxref serve`. Basic auth carries the secret itself, as GitLab's token header does, rather than a signature of the body, so over plain HTTP anyone on the path can read it.
- **Basic authentication username:** anything, e.g. `prxref`.
- **Basic authentication password:** the value of `PRXREF_AZURE_DEVOPS_WEBHOOK_SECRET`.
- **Resource details to send:** **All**. prxref builds the PR URL from `resource.repository` and `resource.pullRequestId`, and reads `resource.status`; a smaller setting can leave them out, and the PR is then not reviewed.

prxref reviews only a PR whose status is `active`. An update that completes or abandons a PR, and every other event type, is acknowledged with `202` and not reviewed. The daemon posts with `PRXREF_AZURE_DEVOPS_TOKEN`: a PAT with **Code (Read & write)**. Posting to Azure DevOps and the service-hook payload are verified against recorded shapes only, not a live server; see [Azure DevOps](forges.md#5-azure-devops-services--server).

---

## 3. Non-Docker Deployment (Systemd / Bare Metal)

### Installation via `uv`

Install `prxref` directly as a standalone CLI tool or in a virtual environment:

```bash
# Standalone tool installation
uv tool install prxref

# Or in a local environment
uv pip install prxref
```

### Systemd Unit Example

Create `/etc/systemd/system/prxref.service`:

```ini
[Unit]
Description=prxref Webhook Review Service
After=network.target

[Service]
Type=simple
User=prxref
Group=prxref
WorkingDirectory=/opt/prxref
EnvironmentFile=/etc/prxref/prxref.env
ExecStart=/usr/local/bin/prxref serve --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now prxref
```

---

## 4. Health Checks and Monitoring

`prxref serve` provides a lightweight GET endpoint at `/health` for liveness probes:

```bash
curl -f http://localhost:8080/health
# Response: {"ok": true} (HTTP 200)
```

The container includes a built-in curl-free health check using Python standard library `urllib.request`.

---

## 5. Exit Codes in CI/CD

`prxref` is an advisor, not a merge gate, and its exit code reflects that:

| Code | Meaning | Pipeline effect |
|---|---|---|
| `0` | The run finished. This **includes every review error**: an empty diff, a network failure, an LLM timeout, bad forge credentials, an unrecognized PR URL, or a review in which every chunk failed. Diagnostics are printed to stderr. With `PRXREF_FAIL_ON` set to `error` or `any`, a review whose findings trip that policy, or one that fails to complete, exits `1` instead (next row). | Step stays green. |
| `1` | A **gated review outcome**, only when `PRXREF_FAIL_ON` is set: `error` exits `1` when the completed review carries an active error-severity finding, `any` exits `1` on any active finding, and under either value a review that fails to complete also exits `1`. The reason is printed to stderr. An unrecognized PR URL still exits `0`. | Step fails, because the lane opted in. |
| `2` | A **usage or configuration error**: no subcommand, invalid arguments, or a required value missing, malformed, or out of range. The message names the source that supplied it — the environment variable, or the CLI flag when a flag was what the operator typed. | Step fails. This is the intended failure: it means prxref was invoked wrong or is misconfigured, not that your code is bad. |

```bash
# A URL prxref cannot review — still exit 0
$ prxref review --pr-url https://github.com/org/repo/issues/42
unrecognized PR URL 'https://github.com/org/repo/issues/42' — expected a Bitbucket pull-requests, GitHub pull, or GitLab merge_requests link (bitbucket.org, github.com, gitlab.com, or a self-hosted Bitbucket Data Center, GitHub Enterprise Server, or GitLab host), or an Azure DevOps pullrequest link (dev.azure.com, *.visualstudio.com, or an Azure DevOps Server host); the URL must keep the forge's own path shape.
$ echo $?
0

# Every chunk failed — still exit 0, and the forge gets an error notice
$ prxref review --pr-url https://github.com/org/repo/pull/1
verdict: Error
coverage: 0/3 chunks reviewed
$ echo $?
0

# Misconfiguration — exit 2
$ prxref review --pr-url https://github.com/org/repo/pull/1 --max-chunks 0
configuration error: --max-chunks: must be a finite number greater than 0, got 0
$ echo $?
2
```

Practical consequences for a pipeline:

- **Do not add `continue-on-error` to hide review failures.** They already exit `0`. Suppressing errors instead hides the `2` that tells you the deployment is misconfigured — and a review step that can never fail is a review step nobody notices has stopped running.
- **Do not gate a merge on the exit code.** By default it never gates: `PRXREF_FAIL_ON` defaults to `never`, under which no finding moves the exit code, and `PRXREF_FAIL_ON=error` or `PRXREF_FAIL_ON=any` is the explicit opt-in for a lane that wants the gate (the `1` row above). Think hard before you set it. A probabilistic reviewer used as a gate is worse than no gate: the first false positive teaches the team to bypass it. Read the verdict from the posted summary comment instead.
- **Watch for the partial-review banner.** A run where some chunks failed still exits `0` and still posts a summary; the banner in that summary (and the `coverage: N/M chunks reviewed` line on stdout) is the only signal that the review was incomplete. The most common cause is a starved completion budget — see [Reasoning models and the token budget](env-vars.md#reasoning-models-and-the-token-budget).

---

## 6. CLI Model Backends in Docker and CI

The `claude-cli` and `kiro-cli` backends run the Claude Code CLI or the Kiro CLI that a developer has installed and logged in to on their own machine, on that developer's own subscription. They are not for the Docker image, CI, or the webhook daemon:

- **The Docker image ships neither CLI.** It is `python:3.12-slim` plus the prxref wheel, so `claude` and `kiro-cli` are not on its `PATH`.
- **Do not install or log in to one in CI or on the daemon.** A pipeline or a shared webhook server reviews for a whole team, and a personal subscription login is for your own use; Anthropic's terms do not allow a third-party product to offer claude.ai login or subscription rate limits without approval. Use an API key through `openai-compat` or `litellm` there, or Workload Identity Federation or Bedrock/Vertex/Foundry. The policy and everything else about these backends is in [Subscription CLI backends](llm.md#subscription-cli-backends-claude-cli-and-kiro-cli).

A CLI backend whose binary cannot be found is a configuration error, so a lane that selects one by mistake fails loudly with exit `2` before any network call instead of posting nothing:

```
$ PRXREF_LLM_BACKEND=claude-cli PRXREF_LLM_MODELS=sonnet prxref review --pr-url https://github.com/org/repo/pull/1
configuration error: PRXREF_LLM_BACKEND: claude-cli needs the 'claude' CLI, which was not found on PATH; install it and log in, or set PRXREF_LLM_CLI_PATH to its absolute path
$ echo $?
2
```

---

## 7. Spec Sources in CI and on the Daemon

Spec grounding fetches the sources the operator names in `--spec` or `PRXREF_SPEC_SOURCES` on every review; see the README's [Review Against a Spec or Ticket](../README.md#review-against-a-spec-or-ticket). A pull request cannot add a source. The CLI reads sources only from its flags and environment, and the daemon only from its own environment, never from a webhook payload or a PR description.

### Trust the path, not only the flag

A local spec path inside the PR's own checkout is content the PR controls: the PR can rewrite the constraints it is reviewed against. In CI, point `--spec` at a path outside the workspace, or at a URL.

A relative source is confined to the working directory. A path under the working directory that resolves outside it once its symlinks are followed fails its source (`resolves outside the working directory`), and a directory source skips every symlinked entry in it. An absolute path outside the working directory is the operator's own choice and is read as given.

### On the webhook daemon

- **One list grounds every repository.** `PRXREF_SPEC_SOURCES` applies to every review the daemon runs, and the prompts tell the model to quote a violated constraint verbatim. Private spec text can therefore appear in a comment on any repository the daemon serves. Give the daemon only sources that every one of those repositories may see.
- **Run it from a directory no PR can change.** Relative sources resolve against the daemon's working directory, so never start it inside a checkout. The systemd unit above uses `WorkingDirectory=/opt/prxref`.
- **A slow spec host delays the queue.** The daemon reviews one PR at a time, so every source's fetch time is added to every review, and to every review queued behind it. The bounds below cap that cost per source.

### Fetch time bounds

Two module constants in `prxref.specs` bound a fetch. They are not configuration keys, and neither depends on `--timeout` or `PRXREF_LLM_TIMEOUT`:

| Constant | Value | Bounds |
|---|---|---|
| `SPEC_FETCH_TIMEOUT_S` | `15` | Each HTTP attempt's connect timeout and its timeout per read. |
| `SPEC_FETCH_BUDGET_S` | `30` | One source's body, in wall-clock seconds counted from before the request. |

A spec fetch is retried once, with no backoff sleep, and `Retry-After` is ignored: a host that is down or asks for time is skipped, not waited for. The body is read one socket read at a time, so a host that trickles bytes cannot outlast the budget by more than one read timeout. Worst cases per source:

- A host that accepts the connection and never answers costs about 30 s: two attempts of 15 s each.
- A host that answers and then trickles its body costs about 45 s: the 30 s budget plus one 15 s read timeout.

A web page longer than its byte cap, `4 × PRXREF_SPEC_MAX_CHARS + 4`, is cut with a `[source truncated at N chars]` marker. A Jira response over that cap fails its source.

### What the logs, the run record and the trace say

A failed source never fails the review. The posted grounding note names a failed source only by its position and kind. The operator gets more detail:

- **One WARNING per failed source:** `spec source N/T (kind, origin) failed (best-effort): reason`. `kind` is `file`, `dir`, `url` or `jira`, or `unknown` when the source failed before its kind was known. The reason is redacted the same way as in the posted note. The origin is made safe to log:
  - a local path is logged verbatim, since it names what to fix;
  - a URL is cut to `scheme://host[:port]/path`, with no userinfo, query, fragment or `;params`;
  - a URL that cannot be parsed is logged as `[unparseable origin]`.
- **One INFO line per run with spec sources:** `spec grounding: OK/T source(s) ok, N constraint(s) injected`. N is 0 when the digest held no constraint and was not injected. If the spec stage itself crashes, one ERROR line replaces it (`spec grounding failed (best-effort): …`), and the run proceeds ungrounded.
- **The run record's `spec_grounding` key,** also printed by `--format json`: `{sources, ok, failed, constraints, digest_sha256}`.
  - `failed` lists `source N (kind): <redacted reason>`, or `source N: …` when the kind is unknown. A crashed spec stage records `spec stage crashed: <exception class>` instead.
  - `digest_sha256` is the SHA-256 of the injected digest, or `null` when nothing was injected.
  - The whole key is `null` on a run without spec sources.
  - `prxref review -v` prints it as `spec: OK/T source(s) ok, N constraint(s)`.
- **The run trace (`PRXREF_TRACE_FILE`):**
  - one `specs ok` event (at least one source fetched) or `specs fail` event (none did), with `sources`, `ok` and `constraints`; a `fail` event also carries the raw, unredacted `reasons`;
  - a `specs relabel` event with `findings` when ungrounded `spec` findings were relabelled `warning` (see [docs/quality.md](quality.md#spec-grounding)).

**Known limitation.** A file skipped inside a spec directory, whether a symlink or a file that cannot be read or decoded, is only logged at WARNING (`spec directory <origin>: skipped …`) while the other files are read. It does not appear in the posted note, the run record or the trace. A directory source fails, and shows up everywhere, only when none of its files could be read.
