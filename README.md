# prxref

Fast automated AI code review for Bitbucket, GitLab, and GitHub.

prxref inspects pull and merge requests across the three major code hosting forges in sub-minute review cycles. It parses unified diffs, partitions changes into risk-ranked chunks, fans out parallel single-shot LLM reviews across a cheap-first model fallback chain, filters findings through deterministic quality gates, and publishes inline comments alongside an executive summary.

```
                  ┌──────────────────────┐
                  │    Pull / MR URL     │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │     detect_forge     │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │    Forge Adapter     │
                  │ (BB / GitHub / GL)   │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │     Unified Diff     │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ Risk-Ranked Chunking │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ Parallel LLM Workers │
                  │  (Fallback Chain)    │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │     Quality Gate     │
                  │ Line Align / Dedup   │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ Post Inline Comments │
                  │     + Summary        │
                  └──────────────────────┘
```

## Quickstart

Run reviews instantly without local installation using `uvx`, or install the CLI globally:

```bash
# Run one-shot review via uvx
uvx prxref review --pr-url https://github.com/org/repo/pull/123

# Or install tool globally
uv tool install prxref
prxref review --pr-url https://github.com/org/repo/pull/123

# Or install straight from source (works before the first PyPI release)
uv tool install git+https://github.com/sblattj/prxref
```

### Review Any Forge

Pass any PR or MR URL directly. Forge type, repository namespace, and pull request ID are detected automatically:

```bash
# Bitbucket Cloud
prxref review --pr-url https://bitbucket.org/workspace/repo/pull-requests/42

# GitHub & GitHub Enterprise
prxref review --pr-url https://github.com/owner/repository/pull/108

# GitLab & Self-Hosted GitLab (including nested subgroups)
prxref review --pr-url https://gitlab.com/group/subgroup/project/-/merge_requests/15
```

## LLM Configuration

prxref operates without direct cloud provider SDK keys (no Anthropic API keys). It ships with **no default endpoint and no default model chain**: point it at any OpenAI-compatible `/chat/completions` server (OpenRouter, Together, Groq, vLLM, Ollama, a self-hosted gateway), or install the optional `litellm` extra. `PRXREF_LLM_BASE_URL` and `PRXREF_LLM_MODELS` are required — leaving either unset exits `2` with an error naming the variable.

```bash
# Default backend: plain HTTP to any OpenAI-compatible endpoint
export PRXREF_LLM_BACKEND=http
export PRXREF_LLM_BASE_URL="https://openrouter.ai/api/v1"
export PRXREF_LLM_API_KEY="$OPENROUTER_API_KEY"
export PRXREF_LLM_MODELS="z-ai/glm-5.3-flash"
export PRXREF_LLM_REASONING_EFFORT=low

# Optional: in-process litellm extra
# pip install 'prxref[litellm]'
export PRXREF_LLM_BACKEND=litellm
export PRXREF_LLM_MODELS="openrouter/meta-llama/llama-3.3-70b-instruct,bedrock/anthropic.claude-3-7-sonnet-20250219-v1:0"
```

See [docs/llm.md](docs/llm.md) for architecture, failover behavior, and backend setup.

## Forge Authentication

Configure the authentication token matching your forge:

| Forge | Environment Variable | Notes |
|---|---|---|
| **Bitbucket** | `PRXREF_BITBUCKET_TOKEN` | Bearer token (workspace or repo access token) |
| **Bitbucket (Basic)** | `PRXREF_BITBUCKET_USER` + `PRXREF_BITBUCKET_APP_PASSWORD` | App password fallback |
| **GitHub** | `PRXREF_GITHUB_TOKEN` | Personal Access Token (PAT) or GitHub App token |
| **GitHub Enterprise** | `PRXREF_GITHUB_ENTERPRISE_TOKEN` | Used when host is not `github.com` (falls back to `PRXREF_GITHUB_TOKEN`) |
| **GitLab** | `PRXREF_GITLAB_TOKEN` | Personal, project, or group access token (`PRIVATE-TOKEN`) |

See [docs/env-vars.md](docs/env-vars.md) for the full configuration reference and [docs/forges.md](docs/forges.md) for forge specifics.

## Webhook Server

Run prxref as a persistent daemon to handle webhook events from GitHub, Bitbucket, and GitLab:

```bash
prxref serve --port 8080 --host 0.0.0.0
```

The service exposes:
- `POST /webhook` — verifies HMAC or token signatures per forge, enqueues incoming PR events, and responds immediately with `202 Accepted`. A background worker processes reviews serially.
- `GET /health` — liveness probe returning `{"ok": true}`.

## CLI Flags

- `--pr-url URL` — full web URL of the PR or MR (required for `review`).
- `--no-post` — dry run; run review analysis and quality passes without writing comments to the forge.
- `--max-chunks N` — override maximum diff chunks evaluated (default `8`).
- `-v, --verbose` — output run timing, token counts, and finding breakdowns to stdout.
