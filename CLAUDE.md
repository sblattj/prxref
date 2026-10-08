# prxref

Fast automated AI code review for Bitbucket, GitLab, GitHub, Gitea/Forgejo, and Azure DevOps — Cloud and self-hosted.

## What This Is

A Python CLI + webhook service that reviews PRs/MRs on any of those forges by:
parsing one unified diff, chunking it, running parallel single-shot LLM worker
reviews with a fallback model chain, gating findings through deterministic
quality passes, and posting inline comments + a summary.

`prxref review --pr-url https://<any-forge>/<owner>/<repo>/pull|pulls|pullrequest|merge_requests/<n>`
auto-detects the forge.

## Tech

- Python 3.12+, `uv` for env/lock, hatchling packaging
- One `Forge` Protocol (src/prxref/forges/base.py), six adapters for five
  forges (Bitbucket Cloud and Server/Data Center are separate). `detect_forge`
  asks Bitbucket Cloud, Bitbucket Server, GitHub, GitLab, Gitea/Forgejo, then
  Azure DevOps last; the URL patterns must stay disjoint, and Gitea's any-host
  parser must keep refusing the other forges' hosts and `/api/` URLs. Gitea
  and Azure DevOps rebuild the diff locally. Per-forge detail:
  `docs/forges.md`.
- LLM access via a fallback chain (llm-ferry preferred, litellm optional,
  plain-HTTP client as zero-dependency default) — provider-agnostic, no
  Anthropic key by design

## Commands

- `uv run pytest` — tests (pytest is in the default `dev` dependency group)
- `uv run ruff check src tests` — lint
- `uv run prxref review --pr-url ...` — one-shot review
- `uv run prxref config check` — validate config (bare `prxref config` only
  prints help, rc 0)

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
  `_FLOAT_KEYS` / `_RANGES` / `_CHOICE_KEYS` tables. A new key needs those
  tables, the `config.py` docstring, `.env.example`, `docs/env-vars.md`, and a
  classification into `FILE_KEYS` (`.prxref.toml`, between defaults and env)
  or `ENV_ONLY_KEYS` with its row in `docs/config-file.md`; a test enforces
  the partition. Anything that names a host, runs a program, writes a file or
  reads outside the repo is env-only. Renaming a file key in a release needs
  an alias, or existing `.prxref.toml` files exit 2.
- every posted comment carries model attribution

## Testing gotchas

- Editing `src/prxref/prompts/worker.md` moves pinned prompt hashes: re-record
  them in `tests/test_rule_prompt_slot.py`, `test_orchestrator_grouping.py`,
  `test_orchestrator_rule_cap.py`, `test_issue_17_acceptance.py`,
  `test_issue_20_acceptance.py`, `test_context_followup_off_identity.py` and
  the golden JSON under `tests/fixtures/issue17/` and `issue20/`
  (`make_golden.py`). Take the new values from the failing assertions.
- Output key sets (run config, run record, result JSON) are pinned in several
  test files under different names; `grep -rl RUN_CONFIG_KEYS tests` lists the
  run-config ones. The doc-count tests (pass counts, key-word counts) live in
  `tests/test_rule_cap_config.py`.
- A new file in the review trace dir (`orchestrate_review(trace_dir=...)`)
  moves the pinned trace hashes built by `_units()` in
  `tests/test_orchestrator_grouping.py` (imported by
  `test_orchestrator_rule_cap.py`) and the file sets in
  `tests/test_trace_dir.py`; skip it in `_units()` as `diff.patch` is. Only
  the full suite shows this.
- The numbered quality-pass list is the `quality.py` module docstring.
- `cli.py` reaches `orchestrate_review` through `importlib`, so
  `grep 'orchestrate_review('` finds no CLI caller.
- `tests/evals/` is a spec-grounded, recall-only dataset; the structural tests
  in `tests/evals/test_evals.py` reject cases that break that contract. Read
  them before adding a case. Eval runs pin `ci_wiring`, `routing_probe` and
  standards discovery off (`evals.EVAL_PINS`).
- Ad-hoc probes: run through `uv run` (plain python3 lacks `requests`).
  `Finding` and `parse_unified_diff` live in `prxref.triage`; `Finding` needs
  `confidence`. `load_config` never finds `.prxref.toml` on its own: pass
  `config_file=`. An orchestrator probe needs the `contract_stubs` fixture
  (`tests/conftest.py`), or `FakeLLM` sees a non-JSON prompt; append the probe
  to a copy of the owning test module instead of importing from it.
  `contract_stubs` replaces the real prompts, so request it per test: a
  module-wide `pytestmark` also stubs that module's real-prompt tests.
- A red-proof on a `/tmp` copy of the tree needs `PYTHONPATH=<copy>/src`, or
  the editable install keeps importing the worktree. Copy `README.md` too:
  `uv run --directory <copy>` builds the package, and hatchling fails
  without it.
- `docs/env-vars.md` rows are single long lines; read diffs with
  `git diff -U0 -- docs/env-vars.md | cut -c1-300`.
