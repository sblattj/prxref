
# prxref issues — ready to file

Eleven issues against `sblattj/prxref` (**0.11.0**, plus #11, #14, and the addenda to
#4, #7, and #11 against later releases), one file each, paste-ready as GitHub issues (first
line is the title, then labels, then body). GitHub is unreachable from this
machine, hence the folder.

Evidence corpus: two review passes over `GAIENG/syf-mcp` (Bitbucket Server),
ten PRs total, backend GitHub Copilot Business, models
`claude-opus-5,claude-sonnet-5`. Every claim below was verified by hand — the
Effect ones by running the pattern against the exact pinned version
(`effect@4.0.0-rc.110`), the rest against the branch contents.

| # | File | Type | Severity | One line |
|---|---|---|---|---|
| 1 | `01-library-version-blind-false-positive.md` | bug | **high** | Asserts Effect 3 semantics on an Effect 4 repo; its suggested fix creates the bug it alleges |
| 2 | `02-identifier-name-reasoning.md` | bug | high | Trusts the name `PositiveInt` over its definition 78 lines up in the same file |
| 3 | `03-rename-copy-header-read-as-deletion.md` | bug | high | Git `copy from` header reported as "package.json removed" |
| 4 | `04-nondeterminism-high-severity-findings-vanish.md` | bug | **high** | Same commit, 7 findings then 2; the one that vanished was the security finding |
| 5 | `05-quality-gate-admits-self-hedged-findings.md` | bug | medium | Posts findings whose own body says "if X still..." |
| 6 | `06-sweep-blind-to-deletions-and-settled-discussion.md` | recall | medium | Sweep: 0 findings on 6 PRs, blind to guard removal, ignores settled threads |
| 7 | `07-understated-containment-boundary.md` | accuracy | medium | "Throws" findings never name the catch; total outage described as partial |
| 8 | `08-dry-run-prints-no-findings.md` | ux | low | `--no-post` prints a verdict and nothing else |
| 9 | `09-model-unavailable-400-retried-per-chunk.md` | perf | low | Deprovisioned model retried on every chunk and the sweep |
| 10 | `10-release-shaped-pr-touching-source.md` | enhancement | medium | Release PR carrying an unmerged branch's commits drew zero findings |
| 11 | `11-cross-file-evidence-in-same-diff-ignored.md` | bug | **high** | Two false positives on one PR; both refuted by another file in the same diff |
| 13 | `13-settled-thread-crash-on-pathless-comment.md` | bug, crash | **critical** | 0.12.0 regression: review aborts on any PR with a general (unanchored) comment |
| 14 | `14-lockfile-block-boundary-misread-false-positive.md` | bug | **high** | Reads `devDependencies` as runtime `dependencies` and anchors to a phantom line; confident FP on a lockfile |

`00-full-report.md` is the original long-form write-up the issues were split
from; keep it for context or discard it.

## Status against 0.12.0 (released 2026-09-04)

0.12.0 bundles fixes for inbox issues **01–10 and 12**, each with an eval. Retested
on the same PRs:

| Issue | 0.12.0 result |
|---|---|
| 06 settled threads | **Fixed and observed working** — a previously-retracted false positive was auto-dropped as `settled in thread: 503033832` |
| 07 containment | Gate present; the overstated-containment finding no longer posted |
| 04 determinism | Posted output now stable across runs (0 and 0), though **raw candidate count still varies** (4 vs 3 on identical commits) |
| 11 cross-file | **Not fixed** — same wrong conclusions still generated, now suppressed by the confidence floor rather than resolved. No eval exists. |
| 13 pathless thread | **New regression introduced by the 06 fix** — blocks reviews outright |

## Status against 0.12.1 (released 2026-09-10)

Issue **#11 remains open**. PR #52 produced a confidence-0.70 warning claiming
that a deliberate build-time `define` broke a documented runtime override. The
same diff explicitly documents the variable as build-time, explains why the
replacement is required, and uses the resolved value consistently in generated
artifacts. Unlike the 0.12.0 retest, this cross-file false positive cleared the
confidence floor and became the only active finding.

New evidence added from `syf-mcp` #57 (Nexus IQ tools, head `db2044b7`):

- **#4 sharpest reproduction yet (addendum).** At one commit, minutes apart, the
  posting run dropped the *two most severe* findings the dry run had — both
  verified-true blockers (`acquire.ts` whole-run refresh abort; `--force` cannot
  rotate a healthy token). The kept set still read as a competent NEEDS_WORK, so a
  single-run operator gets no signal the two worst defects vanished. Contradicts
  the 0.12.0 "posted output now stable" note — stability held only when the true
  count was zero.
- **#14 new — lockfile block-boundary misread (high).** A confident warning said
  `vitest`/`tsup` were declared under runtime `dependencies` for `@syf-mcp/mcp`;
  they are in the adjacent `devDependencies` block, and the cited anchor line was
  an unrelated correct dependency. Structured-file FP that cleared the floor.

Version numbering caveat: the 0.12.0 sdist ships `PKG-INFO: 0.12.0` but
`pyproject.toml` and `src/prxref/__init__.py` still read `0.11.1`, so
`prxref --version` under-reports and makes a successful update look like a no-op.
Confirm the upgrade by the presence of `src/prxref/chunk_context.py` instead.

## If only two get filed

**#1** and **#4**. Together they are the case that prxref can block a correct PR,
hand the author a patch that breaks it, and silently drop the one finding that
mattered — and that running it twice is the only way to notice.

**#13 now outranks both** — it is a 0.12.0 crash that aborts the review on
essentially any real PR, and it is a two-line fix.

**#11** remains the strongest single-PR accuracy evidence in the folder: two
findings, both false, both refuted by a file the tool had already been given, and
0.12.0 suppresses them by confidence rather than fixing the reasoning.

## Measured precision

| Corpus | Version | Findings verified | True | False |
|---|---|---|---|---|
| Figma lifecycle PR (2 runs) | 0.11.0 | 3 | 1 | 2 |
| Six-PR pass | 0.11.0 | 8 | 6 | 2 |
| syf-mcp #49 / #48 / #45 pass | 0.11.1 | 4 | 2 | 2 |
| syf-mcp #57 (Nexus IQ, posting run) | 0.12.1 | 7 | 6 | 1 |

In the 0.11.1 pass, #49 drew zero findings (correct — the diff is a two-line docs
link change), #48 drew two and **both were false** (issue #11), and #45 drew two
that were **both true** but one of which materially overstated its blast radius
(addendum to issue #7). Running precision across all three corpora: 9 true of 15
verified findings.

Ten of eleven distinct findings were reproducible claims about code; four were
wrong. The true ones were genuinely useful — one caught an uncaught throw that
would have taken down MCP handler construction, and it was fixed before merge.


