# Deployment Guide for prxref

`prxref` runs in two modes:
1. **Daemon Mode (`prxref serve`)**: An HTTP webhook listener running on port 8080.
2. **CLI / One-shot Review (`prxref review`)**: Run directly in CI/CD pipelines or manually against a PR/MR URL.

For the full list of supported environment variables and credentials, refer to `docs/env-vars.md`.

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
