# The systemic sweep

Per-chunk workers each see one slice of the diff, so a defect that only looks
wrong in aggregate has no seat that sees enough to name it. The sweep spends
ONE extra single-shot review call over a deterministic digest of the whole PR
(`prxref/systemic.py`), then feeds its findings into the same quality passes as
every chunk finding.

## Digest classes

`match_class(text, *, kind="+")` labels one diff line with the first class it
matches, in this order. `kind` is the line's diff marker; only
`guard-removal` reads it.

| Class | Fires on | Kind |
|---|---|---|
| `entry-point` | exported/handler functions, route and AJAX registrations | any |
| `secret` | `process.env`, `VITE_*`, `NEXT_PUBLIC_*`, `*_API_KEY`, `*SECRET*`, `*TOKEN*` | any |
| `auth-check` | nonce / JWT / bearer / `verify_*` / `authenticate` | any |
| `guard-removal` | a DELETED numeric limit constant (`MAX_*_LENGTH`, `*_SIZE`, `*_BYTES`, `*_TIMEOUT`, `*_CAP`, `*_LIMIT`) or a DELETED validator definition (`is*`, `isValid*`, `validate*`, `sanitize*`, `check*`, `assert*`, `escape*`, `guard*`) | `-` only |
| `error-swallow` | `catch`/`except`/`finally`, empty `.catch()` | any |
| `migration-ddl` | `CREATE TABLE`, `ALTER TABLE`, RLS statements | any |
| `console-log` | `console.*`, `logger.*`, `logging.*` | any |
| `loop-timer` | `setInterval`, `setTimeout`, `while (true)` | any |
| `repo-config` | a `packageManager` pin | any |

`guard-removal` is removal-only by design: the same text ADDED is a guard being
put in place, which is not a finding. It sits in `_MUST_SEE_CLASSES` alongside
entry points, secrets and auth checks, so it survives the per-file matched-line
cap ahead of noisier classes; and because a removed line only reaches the digest
when it carries a class, a PR that deletes nothing but guards is no longer
digested as an empty file. Removed lines render with their `-` marker and their
OLD-file line number (`-2| const MAX_TOOL_NAME_LENGTH = 128;`).

## Existing discussion

Threads already open on the PR are fetched once, before the review units are
dispatched, and the ones whose path is in the digest are appended to the sweep's
user prompt as an `### Existing discussion` block — one
`- <path>: <author>: <snippet>` line per thread. Snippets are truncated to 200
characters and the block is capped at 15 threads / 1200 characters (roughly 300
tokens against a sweep input of 1878–3585), with a final
`… N more threads omitted` line when the cap bites. With no threads, no header
is printed at all. The prompt tells the model not to raise a subject already
argued out there, and to say why the discussion's conclusion is wrong if it
raises it anyway.

The fetch is best-effort: a forge that cannot list threads still gets a full
review, with an empty discussion block. It is deliberately ordered AFTER the
stale-inline-comment prune, or the run would suppress its own findings against
comments it is about to delete.

## Drop reasons

Every dropped finding keeps its identity and states why:

| Reason | Pass |
|---|---|
| `malformed location: '<file>'` | `apply_location_validation` |
| `duplicate of existing thread` | `apply_thread_dedup` — same path, line window, shared tokens |
| `settled in thread: <author>` | `apply_settled_thread_suppression` — same path and 4+ shared tokens, NO line test |
| `severity '<x>' not in vocabulary` / confidence floor / error cap | `apply_quality_gate` |
| duplicate of a chunk finding | `apply_sweep_dedup` |

`settled in thread` is line-independent on purpose: `apply_line_align` has
already demoted a file-level finding to line 0 by the time it runs, so a
distance test could never fire. A resolved thread still settles its subject —
resolution is a decision, not an expiry.
