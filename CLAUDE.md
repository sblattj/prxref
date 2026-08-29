# prxref

Fast automated AI code review for Bitbucket, GitLab, and GitHub — Cloud and self-hosted.

## What This Is

A Python CLI + webhook service that reviews PRs/MRs on any of the three major
forges by: parsing one unified diff, chunking it, running parallel single-shot
LLM worker reviews with a fallback model chain, gating findings through
deterministic quality passes, and posting inline comments + a summary.

`prxref review --pr-url https://<any-forge>/<owner>/<repo>/pull|pullrequest|merge_requests/<n>`
auto-detects the forge.

## Tech

- Python 3.12+, `uv` for env/lock, hatchling packaging
- One `Forge` Protocol (src/prxref/forges/base.py), four adapters
- Bitbucket needs two of them: Cloud speaks `/2.0` on `bitbucket.org` only,
  Server / Data Center speaks `/rest/api/1.0` on any host, so the adapter is
  picked from the URL. `detect_forge` asks Cloud first, but that order is
  defensive, not load-bearing: Cloud pins `bitbucket.org` and a bare
  `owner/repo/pull-requests/N` path while Server requires a
  `/projects|users/KEY/repos/REPO/` prefix, so the two patterns are disjoint and
  every URL resolves identically under either order. Asking the narrower parser
  first means a later loosening degrades into a shadowed forge rather than a
  silently mis-routed one. GitHub and GitLab stay one adapter each, because
  their self-hosted products differ only in base URL.
- LLM access via a fallback chain (llm-ferry preferred, litellm optional,
  plain-HTTP client as zero-dependency default) — provider-agnostic, no
  Anthropic key by design

## Commands

- `uv run pytest` — tests (pytest lives in the `dev` dependency group, which
  uv installs by default; it is not a project extra, so no flag is needed)
- `uv run ruff check src tests` — lint
- `uv run prxref review --pr-url ...` — one-shot review

## Conventions

- stdlib + requests only in core; LLM backends are optional extras
- docstrings on public API, no inline commentary
- all LLM calls single-shot with pre-gathered context (no agent loops)
- non-blocking by default: `review` exits 0 on every review error — empty diff,
  network failure, LLM timeout, bad credentials, a totally failed review
  (advisor, not gate). Exit 2 is reserved for a configuration error: a required
  value missing, one malformed or out of range, or one outside its allowed
  vocabulary, reported naming the env var or the CLI flag that supplied it.
  `PRXREF_FAIL_ON` is the one opt-in: `never` (the default) is the doctrine;
  `error`/`any` exit 1 on findings and on a failed review, for CI lanes that
  explicitly want the gate. Do not widen that knob by accident.
- config lives in one place: `config._DEFAULTS` plus the `_INT_KEYS` /
  `_FLOAT_KEYS` / `_RANGES` / `_CHOICE_KEYS` tables. A new key needs all four
  surfaces — those tables, the `config.py` docstring, `.env.example`, and
  `docs/env-vars.md`.
- every posted comment carries model attribution
