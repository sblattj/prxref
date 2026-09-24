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
| **Bitbucket Cloud** | `PRXREF_BITBUCKET_WEBHOOK_SECRET` | `Pull Request: Created` (`pr:opened`), `Pull Request: Updated` (`pr:modified`) | HMAC-SHA256 in `X-Hub-Signature` |
| **GitLab** | `PRXREF_GITLAB_WEBHOOK_SECRET` | `Merge request events` (actions: `open`, `update`) | Secret token in `X-Gitlab-Token` header |

*Note on Insecure Development Bypass:* Setting `PRXREF_ALLOW_UNSIGNED=1` allows unsigned payloads for local testing. Do not use in production.

*First deployment:* set `PRXREF_DRY_RUN=1` before pointing webhooks at a busy repository. The daemon then runs every review in full — fetch, chunk, LLM calls, quality gate — and writes nothing back to the forge, so you can read the logs and confirm the review is sane before it starts commenting. Unset it when you are satisfied. This is the only way to observe the daemon against real traffic: `--no-post` covers a single CLI invocation, and `serve` takes only `--host`/`--port`, so the daemon has no flag-based equivalent.

### Azure DevOps service hooks

<!-- 0.14 placeholder: W62B -->

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
| `0` | The run finished. This **includes every review error**: an empty diff, a network failure, an LLM timeout, bad forge credentials, an unrecognized PR URL, or a review in which every chunk failed. Diagnostics are printed to stderr. | Step stays green. |
| `2` | A **usage or configuration error**: no subcommand, invalid arguments, or a required value missing, malformed, or out of range. The message names the source that supplied it — the environment variable, or the CLI flag when a flag was what the operator typed. | Step fails. This is the intended failure: it means prxref was invoked wrong or is misconfigured, not that your code is bad. |

```bash
# A URL prxref cannot review — still exit 0
$ prxref review --pr-url https://bitbucket.example.com/projects/P/repos/r/pull-requests/42
unrecognized PR URL '...' — expected bitbucket.org, github.com, or gitlab.com PR/MR link
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
- **Do not gate a merge on the exit code.** There is deliberately no `PRXREF_FAIL_ON`. A probabilistic reviewer used as a gate is worse than no gate: the first false positive teaches the team to bypass it. Read the verdict from the posted summary comment instead.
- **Watch for the partial-review banner.** A run where some chunks failed still exits `0` and still posts a summary; the banner in that summary (and the `coverage: N/M chunks reviewed` line on stdout) is the only signal that the review was incomplete. The most common cause is a starved completion budget — see [Reasoning models and the token budget](env-vars.md#reasoning-models-and-the-token-budget).

---

## 6. CLI Model Backends in Docker and CI

<!-- 0.14 placeholder: W66B -->

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
