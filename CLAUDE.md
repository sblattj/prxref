# prxref

Fast automated AI code review for Bitbucket, GitLab, and GitHub.

## What This Is

A Python CLI + webhook service that reviews PRs/MRs on any of the three major
forges by: parsing one unified diff, chunking it, running parallel single-shot
LLM worker reviews with a fallback model chain, gating findings through
deterministic quality passes, and posting inline comments + a summary.

`prxref review --pr-url https://<any-forge>/<owner>/<repo>/pull|pullrequest|merge_requests/<n>`
auto-detects the forge.

## Tech

- Python 3.12+, `uv` for env/lock, hatchling packaging
- One `Forge` Protocol (src/prxref/forges/base.py), three adapters
- Forge coverage is asymmetric, on purpose: GitHub Enterprise Server and
  self-hosted GitLab work on any host (same REST API, different base URL), but
  **Bitbucket is Cloud only**. `bitbucket.ForgeImpl.parse_pr_url` returns None
  unless the host is `bitbucket.org`, so a Server/Data Center URL is rejected
  before any credential is read — a token is never the explanation. Server
  speaks `/rest/api/1.0` against different resource shapes, so supporting it is
  a fourth adapter, not a base-URL setting.
- LLM access via a fallback chain (llm-ferry preferred, litellm optional,
  plain-HTTP client as zero-dependency default) — provider-agnostic, no
  Anthropic key by design

## Commands

- `uv run pytest` — tests
- `uv run ruff check src tests` — lint
- `uv run prxref review --pr-url ...` — one-shot review

## Conventions

- stdlib + requests only in core; LLM backends are optional extras
- docstrings on public API, no inline commentary
- all LLM calls single-shot with pre-gathered context (no agent loops)
- non-blocking: `review` exits 0 on every review error — empty diff, network
  failure, LLM timeout, bad credentials, a totally failed review (advisor, not
  gate). Exit 2 is reserved for a configuration error: a required value missing,
  or one malformed or out of range, reported naming the env var or the CLI flag
  that supplied it. No `PRXREF_FAIL_ON`, deliberately. Do not add an exit code.
- config lives in one place: `config._DEFAULTS` plus the `_INT_KEYS` /
  `_FLOAT_KEYS` / `_RANGES` tables. A new key needs all four surfaces — those
  tables, the `config.py` docstring, `.env.example`, and `docs/env-vars.md`.
- every posted comment carries model attribution
