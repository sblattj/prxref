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
