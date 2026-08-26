# Environment Variables

All environment variables used by `prxref` are prefixed with `PRXREF_`.

Configuration is loaded from built-in defaults, overridden by environment variables, and further overridden by explicit programmatic keyword arguments.

## Variable Reference

### LLM & Pipeline

| Variable | Default | Purpose |
|---|---|---|
| `PRXREF_LLM_BACKEND` | `openai-compat` | LLM backend selector: `openai-compat`, `ferry`, or `http` (aliases for plain-HTTP OpenAI-compatible endpoint), or `litellm` (in-process LiteLLM router). |
| `PRXREF_LLM_BASE_URL` | `http://127.0.0.1:8090/v1` | Base URL for the OpenAI-compatible endpoint (e.g. `llm-ferry` daemon). |
| `PRXREF_LLM_API_KEY` | `local` | API key / Bearer token sent to the OpenAI-compatible endpoint. |
| `PRXREF_LLM_MODELS` | `flash,orch` | Comma-separated model fallback chain evaluated in order (e.g. `flash,orch`). First model that answers successfully wins. |
| `PRXREF_LLM_REASONING_EFFORT` | *(empty)* | Reasoning effort for models that cannot disable reasoning (e.g. `low`\|`high`\|`max` for GLM-5.3-Flash). Empty omits the parameter entirely from the request. Provider-specific vocabulary; not validated client-side. |
| `PRXREF_CONFIDENCE_FLOOR` | `0.6` | Minimum confidence score (float `0.0`–`1.0`). Findings below this threshold are dropped. |
| `PRXREF_MAX_ERROR_FINDINGS` | `10` | Maximum number of error-severity findings reported per review. Excess errors are dropped lowest-confidence-first. (Legacy alias: `PRXREF_MAX_ERRORS`.) |
| `PRXREF_MAX_CHUNKS` | `8` | Maximum number of diff chunks reviewed per PR. |

### Per-Forge Authentication

| Variable | Default | Purpose |
|---|---|---|
| `PRXREF_BITBUCKET_TOKEN` | *(empty)* | Bitbucket Cloud workspace/repository Bearer access token. Preferred over basic authentication. |
| `PRXREF_BITBUCKET_USER` | *(empty)* | Bitbucket Cloud username for HTTP Basic authentication (used with `PRXREF_BITBUCKET_APP_PASSWORD`). |
| `PRXREF_BITBUCKET_APP_PASSWORD` | *(empty)* | Bitbucket Cloud app password for HTTP Basic authentication. |
| `PRXREF_GITHUB_TOKEN` | *(empty)* | GitHub Personal Access Token or GitHub App token for `github.com`. |
| `PRXREF_GITHUB_ENTERPRISE_TOKEN` | *(empty)* | GitHub Enterprise token for custom/self-hosted GitHub Enterprise Server domains. Falls back to `PRXREF_GITHUB_TOKEN` if unset. |
| `PRXREF_GITLAB_TOKEN` | *(empty)* | GitLab Personal, Project, or Group Access Token (sent via `PRIVATE-TOKEN` header) for `gitlab.com` or self-hosted GitLab. |

### Webhook Receiver

| Variable | Default | Purpose |
|---|---|---|
| `PRXREF_BITBUCKET_WEBHOOK_SECRET` | *(empty)* | HMAC secret for Bitbucket webhooks (verified against `X-Hub-Signature` via HMAC-SHA256). |
| `PRXREF_GITHUB_WEBHOOK_SECRET` | *(empty)* | HMAC secret for GitHub webhooks (verified against `X-Hub-Signature-256` via HMAC-SHA256). |
| `PRXREF_GITLAB_WEBHOOK_SECRET` | *(empty)* | Secret token for GitLab webhooks (verified against `X-Gitlab-Token`). |
| `PRXREF_ALLOW_UNSIGNED` | `False` | Boolean (`1`, `true`, `yes`, `on`). Accepts webhooks without valid HMAC/token signatures (dev/testing only; logs a warning). |

## Environment Cross-Check & Defaults

The table above defines all 17 configuration keys supported in `src/prxref/config.py` and `.env.example`:

- **LLM / Pipeline (8):** `PRXREF_LLM_BACKEND`, `PRXREF_LLM_BASE_URL`, `PRXREF_LLM_API_KEY`, `PRXREF_LLM_MODELS`, `PRXREF_LLM_REASONING_EFFORT`, `PRXREF_CONFIDENCE_FLOOR`, `PRXREF_MAX_ERROR_FINDINGS`, `PRXREF_MAX_CHUNKS`
- **Per-Forge Auth (6):** `PRXREF_BITBUCKET_TOKEN`, `PRXREF_BITBUCKET_USER`, `PRXREF_BITBUCKET_APP_PASSWORD`, `PRXREF_GITHUB_TOKEN`, `PRXREF_GITHUB_ENTERPRISE_TOKEN`, `PRXREF_GITLAB_TOKEN`
- **Webhooks (4):** `PRXREF_BITBUCKET_WEBHOOK_SECRET`, `PRXREF_GITHUB_WEBHOOK_SECRET`, `PRXREF_GITLAB_WEBHOOK_SECRET`, `PRXREF_ALLOW_UNSIGNED`

*(Total unique variables: 17)*
