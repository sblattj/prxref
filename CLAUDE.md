# prxref

Fast automated AI code review for Bitbucket, GitLab, GitHub, Gitea/Forgejo, and Azure DevOps — Cloud and self-hosted.

## What This Is

A Python CLI + webhook service that reviews PRs/MRs on any of five forges
(Bitbucket, GitHub, GitLab, Gitea/Forgejo, Azure DevOps) by: parsing one
unified diff, chunking it, running parallel single-shot LLM worker reviews
with a fallback model chain, gating findings through deterministic quality
passes, and posting inline comments + a summary.

`prxref review --pr-url https://<any-forge>/<owner>/<repo>/pull|pulls|pullrequest|merge_requests/<n>`
auto-detects the forge.

## Tech

- Python 3.12+, `uv` for env/lock, hatchling packaging
- One `Forge` Protocol (src/prxref/forges/base.py), six adapters for five
  forges
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
- Gitea/Forgejo is one adapter for every host (Codeberg, gitea.com,
  self-hosted, optionally under a sub-path), because Forgejo keeps Gitea's
  `/api/v1`. Its `/pulls/N` path is disjoint from the other forges' patterns;
  `detect_forge` asks it after GitLab and before Azure DevOps. Since the
  pattern accepts any host, the parser refuses the other forges' cloud hosts
  and any URL with an `/api/` segment ahead of the owner, which would
  otherwise capture API URLs that resolve to nothing. The API has no
  compare-diff endpoint, so the pinned-range diff is rebuilt locally from the
  compare file listing plus whole files at the merge base and the head.
- Azure DevOps is one adapter for Services and Server, asked last by
  `detect_forge`; it has no unified-diff endpoint, so it rebuilds the diff
  locally from the Diffs API change list plus blob contents.
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
- all LLM calls single-shot with pre-gathered context (no agent loops). One
  bounded exception: with PRXREF_CONTEXT_FOLLOWUP=on (off by default, repo
  level only), a chunk whose first reply asks about a symbol it was not shown
  is re-sent once with that symbol's definition appended. The lookup is
  deterministic, the model calls no tools, and a follow-up never triggers
  another.
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
  `docs/env-vars.md` — plus a classification into `FILE_KEYS` or
  `ENV_ONLY_KEYS` (and its row in `docs/config-file.md`).
- config file: `.prxref.toml` sits between defaults and env; a new key must
  be classified into `FILE_KEYS` or `ENV_ONLY_KEYS` (a test enforces the
  partition), and anything that names a host, runs a program, writes a file
  or reads outside the repo is env-only.
- every posted comment carries model attribution
