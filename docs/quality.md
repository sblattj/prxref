# Deterministic Checks, Quality Passes, and Drop Reasons

Most findings come from the LLM fallback chain. Everything on this page is
computed from the parsed diff and the PR's own discussion — no model call, no
non-determinism, no knob unless one is named below.

A finding that fails a pass is **never deleted silently**. It keeps its
identity and gains a `drop_reason`, so a run store, a `--no-post` dry run, or
`--format json` can explain every filter decision. `active(findings)` is the
subset that actually posts.

## Deterministic checks (findings prxref computes itself)

**Release-shaped PRs.** When a PR changes at least 2 files and at least 80% of
them are release machinery — version manifests, `CHANGELOG` / `HISTORY` /
`RELEASE_NOTES` files, lockfiles, `.changeset/` entries, the release-please
manifest — every remaining non-machinery file is flagged with a `warning`
naming each offending path. The body ends with `(deterministic check, no
model)` so it reads distinctly from an LLM finding in the posted summary. The
ratio is exact rational arithmetic, never a float. This catches a release cut
from an unmerged branch, or a hand edit smuggled into a release.

The finding is folded into the raw results **before** the passes below, so it
is validated, aligned, deduplicated and gated exactly like a model finding.
It is spliced in at the chunk/sweep boundary — before the systemic sweep's
own findings, never after — so `apply_sweep_dedup` always treats it as a
CHUNK-side finding and can never drop it as a duplicate of a chunk worker's
own restatement of the same file and title. It also runs on a diff that
yields zero chunks (every file binary, or an empty diff): the heuristic
needs no chunk to fire on, so it is computed and gated on that path too,
not only when at least one chunk survives `build_chunks`.

## The passes, in the order they run

`orchestrate_review` applies these in a fixed order; several of them depend on
it (noted in the table).

| # | Pass | What it does |
|---|---|---|
| 1 | `apply_location_validation` | Drops a finding whose `file` names no path of the parsed diff. |
| 2 | `apply_manifest_claim_check` | `package.json` only. Runs **before** line align, deliberately, so it reads the model's raw anchor. |
| 3 | `apply_line_align` | Re-anchors a cited line to a real added line of that file, or demotes it to file-level. |
| 4 | `apply_thread_dedup` | Drops a finding an existing PR thread already makes (path + line window + shared distinctive tokens). |
| 5 | `apply_settled_thread_suppression` | Drops a finding that re-litigates a subject a thread already argued out. Line-independent by design. |
| 6 | `apply_severity_consistency` | Rewrites only: findings sharing a normalized title are all raised to the group's maximum severity. |
| 7 | `apply_removal_claim_check` | Drops a claim that a **named** path was removed when the post-image still carries it. |
| 8 | `apply_hedge_gate` | Drops a finding whose own text conditions the defect on a precondition never established from the diff. |
| 9 | `apply_quality_gate` | Severity vocabulary, confidence floor, per-review error cap. Returns its findings in content order. |
| 10 | `apply_sweep_dedup` | Drops a sweep finding that restates a chunk finding which **survived** the gate. |
| 11 | `apply_containment_note` | Decoration only: suffixes a throw/panic/crash finding that never named its containment boundary. |

Threads are fetched once per review, **before** the workers run and **after**
the stale-inline-comment prune — reading threads first would let a run suppress
its own findings against prxref's own stale comments and then delete them.

## Drop reasons

| `drop_reason` | Pass | Meaning |
| --- | --- | --- |
| `malformed location: '<file>'` | `apply_location_validation` | The finding names a path the diff never touches — empty, non-path, or invented. |
| `anchor mismatch: claims <pkg> but line <n> is <key>` | `apply_manifest_claim_check` | A `package.json` finding names one dependency but is anchored on a different entry. |
| `section mismatch: claims <section> but <pkg> is under <actual>` | `apply_manifest_claim_check` | A `package.json` finding calls an entry a runtime dependency when it lives under `devDependencies`, or the reverse. |
| `duplicate of existing thread` | `apply_thread_dedup` | An open thread on the PR already says this. |
| `settled in thread: <author>` | `apply_settled_thread_suppression` | A thread on the same path already argued this subject out. A **resolved** thread still settles it — resolution is a decision, not an expiry. |
| `claims removal of a path present in the post-image: <path>` | `apply_removal_claim_check` | Every path the claim names is still present after the PR lands. |
| `hedged: "<matched phrase>"` | `apply_hedge_gate` | The finding's own text conditions the defect on something the model never established. |
| `invalid severity: '<sev>'` | `apply_quality_gate` | Severity outside {`error`, `warning`, `outofscope`}. |
| `confidence <x> below floor <y>` | `apply_quality_gate` | Below `PRXREF_CONFIDENCE_FLOOR`. |
| `error cap exceeded (max <n>)` | `apply_quality_gate` | Beyond `PRXREF_MAX_ERROR_FINDINGS`. Ties break on finding content, not arrival order, so the cap is reproducible. |
| `duplicate of chunk finding` | `apply_sweep_dedup` | A whole-diff sweep finding restates a chunk finding that already survived the gate. |

One more marker is **not** a drop reason. `apply_containment_note` appends
`" [containment boundary not stated]"` to the body of a finding that asserts a
throw, panic, crash, or unhandled rejection without naming the enclosing catch
or the caller it propagates to. The finding still posts; the suffix stops a
correct-but-underscoped "this throws" from reading as a smaller bug than it is.
It runs last, so the posted comment and the dropped-audit copy carry the same
text.

## What is and is not tunable

- `PRXREF_CONFIDENCE_FLOOR` and `PRXREF_MAX_ERROR_FINDINGS` are the only knobs
  here, and they only move `apply_quality_gate`. See
  [Tuning for Your Team](env-vars.md#tuning-for-your-team).
- The hedge gate, the manifest checks, and the removal-claim check have **no
  knob**. They are correctness checks against the diff itself, not noise
  levers. A hedged finding is unverified by its own admission; the escape hatch
  is at the prompt level, where the worker is told to file such a thing as a
  question at confidence ≤ 0.5 and let the floor handle it.
- A hedged finding never consumes an error-cap slot ahead of a proven one.

## File statuses

Diff sections are classified `added`, `modified`, `removed`, `renamed`, or
`copied`. A `copied` section — a git `copy from` / `copy to` header, or the
Bitbucket Server `src://` / `dst://` form — leaves its **source file present**,
which is why `apply_removal_claim_check` exists: a worker that reads the copy as
a move and reports the source "deleted" is contradicted by the diff itself. The
copy headers are rendered back into the worker prompt so the model can tell a
copy from a move in the first place.

## See also

- [docs/systemic-sweep.md](systemic-sweep.md) — the whole-PR sweep's digest
  classes, its existing-discussion block, and its caps.
- [docs/llm.md](llm.md) — the worker prompt's context blocks, failover, and
  what determinism does and does not buy you.
