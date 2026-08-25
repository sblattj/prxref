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
- non-blocking: `review` exits 0 even when the review fails (advisor, not gate)
- every posted comment carries model attribution
