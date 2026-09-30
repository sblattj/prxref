# HANDOFF — v0.29.0 shipped: partial reviews name what they skipped (#72)

**Repo:** `sblattj/prxref` (public) · **Released:** 2026-09-30 · **Supersedes** the
v0.28.0 handoff.

0.29.0 fixes #72. A run where some review units fail used to report
`verdict: Approved` with only a `chunks_failed` count, so a consumer
reading the verdict or a gate built on it could not tell which files got
no review. The run record now carries `failed_chunks`, and the per-model
deadline default rises from 45 s to 120 s, because reasoning models
routinely need 50 s or more for a large chunk and the old default
dropped normal chunks on every run. The user-facing account is the
`[0.29.0]` section of `CHANGELOG.md`.

## What landed

- **`failed_chunks` on the record.** `orchestrator._failed_chunks` builds
  one `{unit, kind, files, error}` per failed unit in review order:
  `kind` is `chunk` (its files, in chunk order) or `sweep` (the
  cross-file sweep, no files). Both the partial-success exit and the
  total-failure exit set it. `_run_record` defaults it to `None`, so an
  exit that never reached review (empty diff, config error) still has the
  key.
- **JSON.** `cli._build_json_result` adds `failed_chunks` after
  `config_file` (before `sampling`/`replay`), so every existing key keeps
  its position and the 0.22.0 golden hashes only need the new key popped.
- **Text.** `_print_summary` prints `not reviewed: <files>` after
  `coverage:`, `not reviewed: cross-file sweep` when the sweep failed, and
  `hint: a review unit hit the model deadline; raise --timeout or
  PRXREF_LLM_TIMEOUT` when any failure was a timeout.
- **Default deadline.** `config._DEFAULTS["llm_timeout"]` and
  `llm_backends.DEFAULT_TIMEOUT` are 120.0; `.env.example`,
  `docs/env-vars.md` and `docs/llm.md` follow.
- **Unchanged on purpose.** The verdict vocabulary and `degraded` (post
  failures only, #48) keep their meaning, so gates and CI fallbacks built
  on them behave as before.

## Tests

`tests/test_issue_72_failed_chunks.py` covers a clean run (`[]`), a failed
chunk with its files, a failed sweep, a total failure naming every chunk,
the JSON key, the text lines and the timeout hint, and the 120 s default.
Pinned record-key sets, JSON key-order assertions and the 45 s default
assertions were updated. Full suite: 10034 passed; ruff clean.

## Live check

The same real 566-line diff through `--diff-file` on the `claude-cli`
backend: at `--timeout 40`, v0.28.0 lost 1 of 4 units with no file list.
At `--timeout 20` the fix reported the failed chunk's 5 files, the
timeout error and the hint. Evidence is on PR #76.

## Next

- Consumers that gate on partial reviews can read `failed_chunks` directly
  instead of inferring from `chunks_failed`.
- A per-chunk adaptive deadline (scaling with input size) is still open
  as a possible follow-up to #72's second expectation. The raised default
  covers the observed cases.
