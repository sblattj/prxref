# Deterministic Checks, Quality Passes, and Drop Reasons

Most findings come from the LLM fallback chain. Everything on this page is
computed from the parsed diff and the changed files, the PR's own
discussion, the prompt templates the run used, and any review rules or spec
it was given — no model call, no non-determinism, no knob unless
one is named below.

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
own restatement of the same file and title. Being file-level (line 0), it is
also out of reach of the reworded-duplicate tier (`PRXREF_DEDUP_SIMILARITY`),
which never compares line 0. It also runs on a diff that
yields zero chunks (every file binary, or an empty diff): the heuristic
needs no chunk to fire on, so it is computed and gated on that path too,
not only when at least one chunk survives `build_chunks`.

## The passes, in the order they run

`orchestrate_review` applies these in a fixed order; several of them depend on
it (noted in the table).

| # | Pass | What it does |
|---|---|---|
| 1 | `apply_example_echo_check` | Drops a finding whose title, normalized, equals the title of the example finding in the worker or sweep prompt template the run used. See [Example echoes](#example-echoes). The first pass that drops, deliberately, so an echo never reaches the thread, consistency or grouping comparisons, a cap, or sweep dedup, and its audit copy keeps the model's own anchor. |
| 2 | `apply_location_validation` | Drops a finding whose `file` names no path of the parsed diff. |
| 3 | `apply_manifest_claim_check` | Manifests and npm-family lockfiles (`package.json`, `bun.lock`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `bun.lockb`). When the anchor's hunk holds no section header, the served full-file lines decide the enclosing section. Runs **before** line align, deliberately, so it reads the model's raw anchor. |
| 4 | `apply_line_align` | Re-anchors a cited line to a real added line of that file, or demotes it to file-level. |
| 5 | `apply_thread_dedup` | Drops a finding an existing PR thread already makes (path + line window + shared distinctive tokens). |
| 6 | `apply_settled_thread_suppression` | Drops a finding that re-litigates a subject a thread already argued out. Line-independent by design. A thread with no path — a general, unanchored PR comment — is ignored by this pass, since it cannot be "same path" as any finding. |
| 7 | `apply_severity_consistency` | Rewrites only: findings sharing a normalized title are all raised to the group's maximum severity. |
| 8 | `apply_removal_claim_check` | Drops a claim that a **named** path was removed when the post-image still carries it. The removal verb must **govern** that path (`removed src/app.py`, `src/app.py was removed`); a bare "removed" elsewhere in the body is not a removal claim. |
| 9 | `apply_hedge_gate` | Drops a finding whose own text conditions the defect on a precondition never established from the diff. One part of the body is not read, in any finding whatever its severity: after a `Spec:` marker (that exact spelling; the opening quote is optional), the text the finding copies verbatim from the injected spec digest, compared case-insensitively, up to a closing quote. A condition inside a real constraint belongs to the spec, not the model. Everything else is read: text the digest does not hold, so a made-up `Spec: "…"` hides nothing; every quote when no digest was injected (no spec sources, or an ungrounded run); and the title. Known limitation: a quote with no closing quote after its verbatim text, or one that departs from the digest before its closing quote, is exempt only up to the last quote mark inside its verbatim part (an apostrophe counts), and not at all when there is none. |
| 10 | `apply_rule_grouping` | Opt-in: runs only with `PRXREF_GROUP_FINDINGS` set to `1`. Folds chunk findings in one file that name the same rule (compared case-insensitively), or that name no rule and share a normalized title, into one finding. The finding with the smallest positive line anchors the group; a file-level (line 0) finding anchors only when no member has a line. The anchor takes the group's highest severity and highest confidence, and its body gains `Also at:` followed by each other line of the group as a backticked `<file>:<line>`. Every other member is dropped as `grouped into <file>:<line>`. Only active findings at or above the confidence floor are grouped, and whole-PR sweep findings are never grouped. The same rule in two files makes two groups. Runs **after** the thread, removal and hedge passes, so a dropped finding is never listed as a location, and **before** the gate, so the error cap counts groups, not lines. |
| 11 | `apply_rule_cap` | Runs only when a review rules file is loaded (`PRXREF_REVIEW_RULES` or `PRXREF_SCOPED_RULES`) and `PRXREF_MAX_FINDINGS_PER_RULE` is above 0, which it is by default (2); the model is then asked to name the rule it applied, as with grouping. Caps how many findings one rule produces across the whole review. It considers the findings grouping would (active chunk findings with a valid severity, at or above the confidence floor) and keys them on their rule (compared case-insensitively, with whitespace collapsed) or, when they name none, on their normalized title; the two kinds never mix, and unlike grouping the file is not part of the key. A representative from pass 10 counts once. Within one key the findings are ranked by severity, then confidence, then content, and the first `<n>` (the cap) are kept with their own severity and confidence. Every other one is dropped as `rule cap exceeded (max <n>): listed at <file>:<line>`, naming the best (first-ranked) kept finding. That finding's `locations` gains each folded finding's location and each folded finding's own `locations`, deduplicated, without its own location, and sorted by file and line; its body's last paragraph becomes `Also at:` followed by at most five of them as backticked `<file>:<line>` (a file-level one as its bare `<file>`), then `(+<k> more)` for the other `<k>`, replacing a paragraph pass 10 wrote rather than adding a second. Whole-PR sweep findings are never counted, capped or folded. Runs **after** grouping and **before** the gate, so the error, warning and outofscope caps count what it kept. |
| 12 | `apply_quality_gate` | Severity vocabulary, confidence floor, per-review error cap, and the optional per-review warning and outofscope caps (`PRXREF_MAX_WARNING_FINDINGS`, `PRXREF_MAX_OUTOFSCOPE_FINDINGS`; unset caps nothing, and `spec` is never capped). Returns its findings in content order. |
| 13 | `apply_sweep_dedup` | Drops a sweep finding that restates a chunk finding which **survived** the gate, on file plus normalized title. A chunk finding that grouping folded away (`grouped into <file>:<line>`) or the per-rule cap folded away (`rule cap exceeded (max <n>): listed at <file>:<line>`) still counts here, because the finding it was folded into lists its location. With `PRXREF_DEDUP_SIMILARITY` set, a second tier also drops a **reworded** duplicate: two active findings in the same file and on the same line (never line 0) whose titles reach that Jaccard similarity over a dedicated title tokenizer and share at least 3 title tokens. A chunk copy is kept over a sweep copy of equal or lower severity, and a more severe sweep copy is kept alongside it, so the tier never lowers a review's worst severity. Within one side the more severe, then the more confident, copy is kept. Unset, only the exact tier runs. |
| 14 | `apply_containment_note` | Decoration only: suffixes a throw/panic/crash finding that never named its containment boundary. |

Threads are fetched once per review, **before** the workers run and **after**
the stale-inline-comment prune — reading threads first would let a run suppress
its own findings against prxref's own stale comments and then delete them.

## Severity map from team review rules

When the team review-rules file declares a severity map
(`PRXREF_REVIEW_RULES` / `--rules-file`; see
[docs/review-rules.md](review-rules.md)), `apply_severity_map` runs **before
pass 1**. It rewrites a team severity word the model wrote (`blocker`) to the
prxref tier the map gives it (`error`), matching case-insensitively and with
runs of whitespace collapsed. It runs first because every later pass reads the
severity: consistency groups by it, the sweep boundary is re-derived from it,
and the quality gate would drop `blocker` as `invalid severity: 'blocker'`.

- It **drops nothing**, so it has no row in the drop-reason table below. A
  word the map does not name passes through and still dies at the gate as
  `invalid severity`.
- It never rewrites a finding that already carries one of prxref's own
  severities or a `drop_reason`. It keeps every other field, `scope`
  included, and the list's length and order.
- Without rules, or with rules that map nothing, the pass is not called.
- When it rewrites any finding, prxref logs `severity map: rewrote N
  finding(s) from team severity words` at INFO and the JSONL trace gets a
  `rules remap` event with `findings=N`.

The map never targets `spec`, so the pass never mints a spec finding.

## Spec grounding

A run is **grounded** when the spec digest it built holds at least one
constraint line (`specs.constraint_count` above 0). Only a grounded digest is
injected into the prompts. A digest with no constraint line is not injected at
all: no spec sources, every source failed, nothing extracted, or a
`PRXREF_SPEC_DIGEST_TOKENS` budget too small for one line. Every review unit
then sees the no-specs text `(no specs provided for this review)`, under which
the prompts make `spec` an illegal severity.

`apply_spec_grounding` runs right after the severity map and before
`apply_example_echo_check` (pass 1 above), over chunk and sweep findings
alike:

- On an ungrounded run it relabels every `spec` finding as `warning`. The
  severity is compared trimmed and lower-cased, so `SPEC` counts. It never
  drops a finding and never raises one to `spec`, and because it runs before
  `apply_severity_consistency`, an ungrounded `spec` finding can never lift a
  same-title sibling to `spec`.
- When it relabels anything, one INFO line gives the count (`spec grounding:
  relabelled N spec finding(s) as warning (no spec constraint was
  injected)`), and the run trace gets one `specs relabel` event with
  `findings: N`. This can happen on a run with no spec sources at all, when a
  model emits `spec` unasked.
- On a grounded run it changes nothing.

A relabel is not a drop, so it has no `drop_reason`. After this pass a `spec`
finding is filtered like any other. `apply_severity_consistency` ranks
`error` > `warning` > `spec` > `outofscope`, so a same-title `warning` or
`error` raises it. The confidence floor applies to it. It never counts toward
`PRXREF_MAX_ERROR_FINDINGS` and never moves the verdict. The hedge gate's
`Spec: "…"` exemption (pass 9) reads the injected digest only, so an
ungrounded run exempts nothing.

Since 0.14.0 the worker and sweep prompts carry spec text on every run, with
spec sources or without. Their system half carries the `spec` severity and the
spec-grounded rules. Their user half carries a `### Spec constraints` block
that reads `(no specs provided for this review)` when nothing is injected.

## Example echoes

The worker and sweep prompts each show the model one example finding in their
`## Output Format` section. A model that copies that example into its answer
reports a defect nobody found. `apply_example_echo_check` (pass 1) drops it.

- **The titles come from the templates the run used.** Each run reads the
  example titles out of the worker and sweep templates its review units were
  shown: the packaged `worker.md` and `systemic.md`, or the override a
  `--prompts-dir` supplied (see [docs/prompt-templates.md](prompt-templates.md)).
  An overridden template's example title replaces the packaged one, so the
  packaged title stops dropping anything once its template is overridden.
- **Only fenced JSON blocks are read.** A fenced block tagged `json` (in any
  case) or untagged counts, and so does every `"title": "<text>"` pair in it.
  A block in any other language, such as the worker's `diff` block, is never
  read. prxref scans those blocks rather than parsing them, because a packaged
  example is not valid JSON before it is rendered: the `{scope_example}` and
  `{rule_example}` slots follow its last value.
- **The match is exact after normalization.** A finding's title is compared
  with every harvested title after `normalize_title`: lower-cased, backticks,
  quotes and emphasis marks removed, edge punctuation trimmed, whitespace runs
  collapsed. A title that only resembles an example stays. Chunk and sweep
  findings are checked alike, each against the titles of both templates.
- **Every echo is visible.** A dropped echo gains `drop_reason`
  `echoes the prompt's example: "<title>"`, quoting the example as its
  template writes it. When the pass drops anything, prxref logs `example echo:
  dropped N finding(s) titled like a prompt template's example finding` at INFO
  and the JSONL trace gets one `prompts echo` event with `findings: N`. A run
  with no echo logs and traces nothing extra.
- **It runs first among the passes that drop.** It reads only the title, so
  it needs no aligned anchor, and it keeps the list's length and order, so the
  chunk/sweep boundary holds. Running first keeps an echo out of the thread,
  consistency and grouping comparisons, the caps and sweep dedup. An echo never
  anchors a group, never raises a same-title sibling's severity, and never
  takes an error-cap slot, and its audit copy keeps the model's own anchor.
- If the packaged `systemic.md` cannot be read, the run logs a WARNING and
  checks against the worker's example alone.

## Ticket scope

With a ticket context configured (`--context-file` / `PRXREF_TICKET_CONTEXT_FILE`,
see [Ticket Context and Scope](../README.md#ticket-context-and-scope)), every
finding carries a `scope` of `in`, `out`, or `unknown` relative to that ticket.
Scope is **orthogonal to every pass on this page**: no pass reads it, it never
changes a severity or a confidence, and it never feeds the verdict, the
confidence floor, the error cap or its tie-break, or `PRXREF_FAIL_ON`. It is
not the `outofscope` severity either, which only means minor. A finding never
gains a `drop_reason` for its scope.

- **Only an active ticket can set it.** The model is asked for a scope only
  when the ticket has text. Raw chunk and sweep findings go through
  `_enforce_scope` before the first pass: without a ticket, or with an empty
  one, every finding is `unknown` whatever the model returned.
- **The vocabulary is strict.** `triage.normalize_scope` keeps a value only
  when it is exactly `in`, `out`, or `unknown` after trimming and case-folding.
  `"In scope"`, `"yes"`, a boolean, or a missing key is `unknown`. There is no
  synonym table, because a lenient mapping would turn a malformed answer into
  a confident one.
- **Sweep dedup ignores it.** `apply_sweep_dedup` matches on file and
  normalized title, and its reworded tier (`PRXREF_DEDUP_SIMILARITY`) on file,
  line and title similarity, so a restatement is still dropped when the two
  copies disagree on scope, and the kept copy keeps its own. The identity used to
  re-derive the chunk/sweep boundary across the gate includes `scope`, so
  neither copy's scope ends up on the other.
- **Grouping ignores it.** `apply_rule_grouping` (`PRXREF_GROUP_FINDINGS`)
  groups on file and rule, or file and normalized title, so findings that
  disagree on scope still fold into one. The anchor keeps its own scope, and
  each other member keeps its own on its `grouped into` audit copy. The
  per-rule cap (`apply_rule_cap`) keys on the rule or title alone, so it
  ignores scope the same way.
- **It orders the inline batch within a severity.** When
  `PRXREF_MAX_INLINE_COMMENTS` leaves room for only some findings, severity
  decides first. Within one severity, an `out` finding yields its inline slot
  to `in` and `unknown` ones, and confidence and content break the rest of the
  ties. With no active ticket every scope is `unknown`, so the order is exactly
  the severity-only one.
- **Truncation can change the state.** Only the first
  `PRXREF_TICKET_CONTEXT_MAX_CHARS` characters reach the model, and acceptance
  criteria are detected on that kept text. A long ticket whose criteria come
  after the cap therefore reads as a ticket without criteria, and the summary
  says scope was judged from its description alone.

## Grouped findings in the output

With `PRXREF_GROUP_FINDINGS` set to `1`, a group (pass 10) reaches the output
as one active finding, its representative, plus a dropped audit copy of every
other member.

- **`--format json`.** Every finding row carries `rule` and `locations`, after
  `scope`. The representative's `locations` lists one
  `{"file": ..., "line": ...}` object for each location its `Also at:`
  paragraph names, in the same order. It never repeats the row's own `file`
  and `line`, and it is `null` when `Also at:` names nothing, because every
  other member is file-level or sits on the anchor's line. Each other member
  stays in `findings` as a dropped row with `drop_reason`
  `grouped into <file>:<line>`, `locations` `null`, and its own `rule`, which
  may differ in case from the representative's. `prxref eval` credits a group
  at the row's own location and at each entry of `locations`, never at a
  member row. The per-rule cap (pass 11) fills both keys too: while it is
  active, `rule` is filled as it is under grouping, and the best finding of
  a rule it folds carries `locations` that can span files, listing every
  folded location and not only the five its `Also at:` paragraph shows. Each
  folded finding stays in `findings` as a dropped row with `drop_reason`
  `rule cap exceeded (max <n>): listed at <file>:<line>`, and `prxref eval`
  reads those `locations` exactly as it reads a group's. With grouping off
  and the per-rule cap inactive (no review rules file, or
  `PRXREF_MAX_FINDINGS_PER_RULE` set to `0`), `rule` and `locations` are
  `null` on every row.
- **Text output** (`--no-post` or `-v`). Every active finding that names a
  rule, a representative included, ends its line in ` [rule: <rule>]`, after
  any ` [scope: in]` or ` [scope: out]` tag. A representative's body carries
  the `Also at:` paragraph. Each member is listed under
  `dropped:` with its `grouped into <file>:<line>` reason. A group formed by
  the title fallback names no rule, so its line gains no tag and its `rule` is
  `null`.

## Replay runs and the thread passes

A `--no-threads` replay gives passes 5 and 6 (`apply_thread_dedup` and
`apply_settled_thread_suppression`) an empty thread list, so they drop nothing,
and a `--diff-file` replay with no `--pr-url` has no threads to start with. A
replay at pinned SHAs WITHOUT `--no-threads` still dedups against the PR's
*current* threads, which may postdate the pinned head; the CLI logs a warning
saying so. The stale-inline-comment prune never runs on a replay, because a
replay never posts. See the README's "Replay Mode (Evaluation)".

## Drop reasons

| `drop_reason` | Pass | Meaning |
| --- | --- | --- |
| `echoes the prompt's example: "<title>"` | `apply_example_echo_check` | The finding's title, normalized, is the title of the example finding in the worker or sweep template the run used. `<title>` is the example's title as the template writes it. |
| `malformed location: '<file>'` | `apply_location_validation` | The finding names a path the diff never touches — empty, non-path, or invented. |
| `anchor mismatch: claims <pkg> but line <n> is <key>` | `apply_manifest_claim_check` | A manifest/lockfile finding names one dependency but is anchored on a different entry. |
| `section mismatch: claims <section> but <pkg> is under <actual>` | `apply_manifest_claim_check` | A manifest/lockfile finding calls an entry a runtime dependency when it lives under `devDependencies`, or the reverse. |
| `duplicate of existing thread` | `apply_thread_dedup` | An open thread on the PR already says this. |
| `settled in thread: <author>` | `apply_settled_thread_suppression` | A thread on the same path already argued this subject out. A **resolved** thread still settles it — resolution is a decision, not an expiry. |
| `claims removal of a path present in the post-image: <path>` | `apply_removal_claim_check` | A removal verb governs this path, and every path the claim names is still present after the PR lands. |
| `hedged: "<matched phrase>"` | `apply_hedge_gate` | The finding's own text conditions the defect on something the model never established. |
| `grouped into <file>:<line>` | `apply_rule_grouping` | Only with `PRXREF_GROUP_FINDINGS` set to `1`. Another chunk finding in the same file breaks the same rule (or, with no rule named, has the same normalized title), and this one was folded into it. `<file>:<line>` is where the group's anchor sits: the member on the smallest positive line, which carries the group's highest severity and confidence and lists this finding's line under `Also at:`, unless this finding is file-level or on the anchor's own line. Sweep findings never carry it. |
| `rule cap exceeded (max <n>): listed at <file>:<line>` | `apply_rule_cap` | Only with a review rules file loaded and `PRXREF_MAX_FINDINGS_PER_RULE` above 0 (default 2). The same rule (or, with no rule named, the same normalized title) already has `<n>` better-ranked findings in the review, in any file, and this one was folded into the best of them. `<file>:<line>` is where that best finding sits; its `locations` carries this finding's location, and its `Also at:` paragraph shows it unless five others come first. Sweep findings never carry it. |
| `invalid severity: '<sev>'` | `apply_quality_gate` | Severity outside {`error`, `warning`, `spec`, `outofscope`}. |
| `confidence <x> below floor <y>` | `apply_quality_gate` | Below `PRXREF_CONFIDENCE_FLOOR`. |
| `error cap exceeded (max <n>)` | `apply_quality_gate` | Beyond `PRXREF_MAX_ERROR_FINDINGS`. Ties break on finding content, not arrival order, so the cap is reproducible. |
| `warning cap exceeded (max <n>)` | `apply_quality_gate` | Beyond `PRXREF_MAX_WARNING_FINDINGS`, ranked like the error cap. Unset caps nothing; `0` drops every warning. |
| `outofscope cap exceeded (max <n>)` | `apply_quality_gate` | Beyond `PRXREF_MAX_OUTOFSCOPE_FINDINGS`, ranked like the error cap. This caps the minor **severity** `outofscope`; it is **not** ticket scope `out`, so a finding the ticket marks `out` counts against its own severity's cap, not this one. Unset caps nothing. `spec` findings are never capped. |
| `duplicate of chunk finding` | `apply_sweep_dedup` | A whole-diff sweep finding restates a chunk finding that already survived the gate. |
| `duplicate of chunk finding (reworded, similarity <s>)` | `apply_sweep_dedup` | Only with `PRXREF_DEDUP_SIMILARITY` set. A reworded restatement of a kept chunk finding in the same file and on the same line: a sweep copy no more severe than the chunk copy, or the less severe, then less confident, of two chunk copies. `<s>` is the Jaccard title score to two decimals. |
| `duplicate of sweep finding (reworded, similarity <s>)` | `apply_sweep_dedup` | Only with `PRXREF_DEDUP_SIMILARITY` set. The same between two sweep findings. A chunk finding never carries it: a chunk copy is never dropped for a sweep copy. |

One more marker is **not** a drop reason. `apply_containment_note` appends
`" [containment boundary not stated]"` to the body of a finding that asserts a
throw, panic, crash, or unhandled rejection without naming the enclosing catch
or the caller it propagates to. The finding still posts; the suffix stops a
correct-but-underscoped "this throws" from reading as a smaller bug than it is.
It runs last, so the posted comment and the dropped-audit copy carry the same
text.

## What is and is not tunable

- `PRXREF_CONFIDENCE_FLOOR` and `PRXREF_MAX_ERROR_FINDINGS` move
  `apply_quality_gate`. The four opt-in levers added in 0.15.0 are all off by
  default: `PRXREF_MAX_WARNING_FINDINGS` and `PRXREF_MAX_OUTOFSCOPE_FINDINGS`
  add per-severity caps to the same pass, `PRXREF_GROUP_FINDINGS` turns on
  `apply_rule_grouping`, which folds chunk findings that break the same rule
  in one file into one comment, and
  `PRXREF_DEDUP_SIMILARITY` turns on the reworded tier of `apply_sweep_dedup`.
  One more 0.15.0 lever is on by default whenever a review rules file is
  loaded: `PRXREF_MAX_FINDINGS_PER_RULE` (default 2) sets how many findings
  one rule may produce across the review in `apply_rule_cap`, and `0` turns
  it off. These seven are the only knobs here. See
  [Tuning for Your Team](env-vars.md#tuning-for-your-team).
- The hedge gate, the manifest checks, and the removal-claim check have **no
  knob**. They are correctness checks against the diff itself, not noise
  levers. The example-echo check has none either: a finding that repeats the
  prompt's own example was never found in the diff. To change what it drops,
  change the example in an overridden template. A hedged finding is unverified by its own admission; the escape hatch
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
