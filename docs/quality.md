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

One drop reason rests on a model reply: with the opt-in context follow-up
(`PRXREF_CONTEXT_FOLLOWUP=on`), a below-floor question that a second call
does not confirm is dropped as `not confirmed by context follow-up` before
the passes run (see [Drop reasons](#drop-reasons)). The rule that decides
confirmation is deterministic; the re-run's findings are not.

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
is validated, aligned, deduplicated and gated like a model finding.
It is spliced in at the chunk/sweep boundary — before the systemic sweep's
own findings, never after — so `apply_sweep_dedup` always treats it as a
CHUNK-side finding and can never drop it as a duplicate of a chunk worker's
own restatement of the same file and title. Being file-level (line 0), it
meets the reworded-duplicate tier (`PRXREF_DEDUP_SIMILARITY`) only through
that tier's per-file line-0 bucket (#74, see below). It also runs on a diff
that yields zero chunks (every file binary, or an empty diff): the heuristic
needs no chunk to fire on, so it is computed and gated on that path too,
not only when at least one chunk survives `build_chunks`.

**Line 0 and the reworded tier (#74).** The release-shape finding is
file-level (line 0), and the reworded-duplicate tier
(`PRXREF_DEDUP_SIMILARITY`) now compares line-0 findings per file: a model
or sweep finding that restates it file-level in the same machinery file can
be dropped as its reworded duplicate, which is intended — the same defect
needs one comment, not two. An anchored restatement still never matches it,
because the tier never compares a line-0 finding with an anchored one.

**Pinned-off toggles.** When a PR adds a toggle whose default is on and also
adds a line to a suite-wide test setup file that turns the same toggle off,
the toggle's line gets a `warning` at confidence 1.0: the passing suite runs
with the toggle off, so it never exercises the default the PR ships. Both
sides must be ADDED lines of this PR; a toggle or a pin on a context or
removed line is never reported. The toggle side is a call named by a string
literal whose default is a literal true, with exactly those two arguments —
`f("name", default=True)`, `f("name", True)` or `f("name", true)`, where `f`
is any call but a setter (a last name segment that is `set` or `put`, or
starts with one and goes on with anything but a lowercase letter, such as
`setFlag`, `set_flag` or `putBoolean`) — or `getenv`, `environ.get` or
`getProperty` called as `("NAME", "true")`, or `process.env.NAME ?? "true"`
or `|| "true"`. The pin side is a line in a file named `conftest.py`, or
whose name starts with `setupTests.`, `jest.setup.` or `vitest.setup.`, that
sets a name to `"false"`, `"0"` or `"off"` (any case, either quote): through
`setenv`, `stubEnv` or `setProperty` called as `("NAME", value)`, through
`process.env.NAME = value` or `process.env["NAME"] = value`, or through
`os.environ["NAME"] = value`. A pin names a toggle when the pin name's
tokens, lowercased and split on `_`, `.` and `-`, END with the toggle name's
tokens, so `ASSISTANT_PROGRESS_NOTES` names `progress_notes`. The title reads
`Toggle "<name>" defaults on but the test setup pins it off`, and the body
quotes the toggle call, names every file that pins it, and ends with
`(deterministic check, no model)`. The check is always on and has no knob.

Like the release-shape finding, it is folded in at the chunk/sweep boundary,
so it is a CHUNK-side finding, it also runs on a diff that yields zero
chunks, and it goes through the passes below like a model finding. Unlike
that finding, it sits on a real line, so line alignment and the
reworded-duplicate tier both reach it. When a chunk worker also reports the
toggle on the same line, both findings are kept at the default
(`PRXREF_DEDUP_SIMILARITY` unset), as for any two chunk findings on one
line. With the similarity set and titles similar enough, the reworded tier
keeps one copy, the more severe, then the more confident: on a severity tie
the check's 1.0 outranks a model's lower confidence, and the model's copy is
dropped as `duplicate of chunk finding (reworded, similarity <s>)`.

**Both checks keep their own severity.** A finding whose body ends with
`(deterministic check, no model)` takes no part in severity consistency
(pass 9): it joins no group, so no model finding raises it or is raised by
it, and its text does not count toward a code token's rarity. One other pass
can still change its severity or confidence: finding grouping (pass 13,
opt-in). When a chunk worker's finding in the same file names no rule and
has the same normalized title, the group's anchor takes the highest severity
and confidence, so a deterministic anchor can be raised, and a deterministic
finding that does not anchor is dropped as `grouped into <file>:<line>`.
Every other pass leaves its severity and confidence alone, and several can
still drop it, among them the thread passes, the per-rule cap, the gate's
caps and, for the toggle finding, the reworded-duplicate tier.

## The passes, in the order they run

`orchestrate_review` applies these in a fixed order; several of them depend on
it (noted in the table).

| # | Pass | What it does |
|---|---|---|
| 1 | `apply_example_echo_check` | Drops a finding whose title, normalized, equals the title of the example finding in the worker or sweep prompt template the run used. See [Example echoes](#example-echoes). The first pass that drops, deliberately, so an echo never reaches the thread, consistency or grouping comparisons, a cap, or sweep dedup, and its audit copy keeps the model's own anchor. |
| 2 | `apply_location_validation` | Drops a finding whose `file` names no path of the parsed diff. |
| 3 | `apply_manifest_claim_check` | Manifests and npm-family lockfiles (`package.json`, `bun.lock`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `bun.lockb`). When the anchor's hunk holds no section header, the served full-file lines decide the enclosing section. Runs **before** line align, deliberately, so it reads the model's raw anchor. |
| 4 | `apply_line_align` | Re-anchors a cited line to a real added line of that file, or demotes it to file-level. A deterministic finding already on an added line (a pinned toggle, a failing linter's `path:line`) keeps its line. |
| 5 | `apply_anchor_snap` | #74. Reads the finding's own file at the head sha — the same reader the chunk context uses — and anchors it on the nearest occurrence of its quoted code: a backticked span first, then a double-quoted one, then the interior of `catch (…` / `if (…` / `for (…` / `while (…`, within 100 lines of the line the MODEL reported (captured before line align, so a demoted finding is searched from where the model said the defect sits, not where the hunk-bounded passes gave up). Moves only what needs moving: a file-level (line 0) anchor, or one further than 5 lines from the match. A file-level finding whose snippet occurs only outside the 100-line window anchors on the snippet's single whole-file occurrence; when that occurrence is not unique it is marked `anchor_unverified` instead of staying silently at line 0. A finding whose snippet the head file does not hold, whose multi-match nothing breaks, or that sits file-level with no snippet parseable at all is marked `anchor_unverified`; its confidence is unchanged. An unreadable file, a dropped finding and a deterministic check's finding are untouched; without a reader the pass changes nothing. See [Anchor snapping and Also-at verification](#anchor-snapping-and-also-at-verification-74). |
| 6 | `apply_thread_dedup` | Drops a finding an existing PR thread already makes (path + line window + shared distinctive tokens). |
| 7 | `apply_settled_thread_suppression` | Drops a finding that re-litigates a subject a thread already argued out. Line-independent by design. A thread with no path — a general, unanchored PR comment — is ignored by this pass, since it cannot be "same path" as any finding. |
| 8 | `apply_stable_ids` | On by default (#71; `PRXREF_STABLE_IDS=0` opts out): runs **after** both thread gates and the previously-raised note — the ids inherit the gates' verdict — and **before** severity consistency, so a refuted finding is neither counted, raised nor grouped downstream. Stamps every finding with `id` `<file>#<rule or norule>#<12-hex claim hash>` — the claim hash is over the title's sorted, lightly stemmed content words ("derived" and "derive" are one word), so a rewording that merely reorders, re-punctuates or re-inflects the claim keeps the id — plus `anchor_block` (the enclosing function, YAML key path or manifest dependency key at the anchor, computed from the diff alone; `null` for a file-level or diff-absent line) and `id_reused_from`: `run` when a near-identical earlier finding of the same run proposed its id (the synonym-swap case the sorted-token hash cannot see — the id is never rewritten by a thread match), `verdict` when the `PRXREF_VERDICT_STORE` holds it, or — on a miss — holds an entry of the same file and rule whose recorded title the finding's restates (the finding then takes that entry's id, bridging a rewording across runs), `thread` when a resolved-or-outdated thread matches under the previously-raised rule, `null` when the id was freshly computed and matched nothing. Neither the anchor block nor the line is part of the id, so an anchor that drifts one key or line keeps it. A finding whose id the store holds as `refuted` is dropped as `refuted in earlier run (<id>)`. Two findings of one run that share an id at different `(anchor_block, line)` sites keep it: each such id is listed once in the run record's `stable_ids.collisions`. With the flag off (`PRXREF_STABLE_IDS=0`) the pass never runs and every `id`, `anchor_block` and `id_reused_from` stays `null`. |
| 9 | `apply_severity_consistency` | Rewrites only: groups findings and raises every member to the group's maximum severity. Two rules group findings, and a group binds transitively: a shared normalized title, in any file; or a shared rare code token (one in the text of at most 2 findings) when the two sit in the same file or their titles name a common problem class. A deterministic finding (see [Deterministic checks](#deterministic-checks-findings-prxref-computes-itself)) is left out: it is never raised, never raises another finding, and its text does not count toward a token's rarity. |
| 10 | `apply_removal_claim_check` | Drops a claim that a **named** path was removed when the post-image still carries it. The removal verb must **govern** that path (`removed src/app.py`, `src/app.py was removed`); a bare "removed" elsewhere in the body is not a removal claim. |
| 11 | `apply_hedge_gate` | Drops a finding whose own text conditions the defect on a precondition never established from the diff. One part of the body is not read, in any finding whatever its severity: after a `Spec:` marker (that exact spelling; the opening quote is optional), the text the finding copies verbatim from the injected spec digest, compared case-insensitively, up to a closing quote. A condition inside a real constraint belongs to the spec, not the model. Everything else is read: text the digest does not hold, so a made-up `Spec: "…"` hides nothing; every quote when no digest was injected (no spec sources, or an ungrounded run); and the title. Known limitation: a quote with no closing quote after its verbatim text, or one that departs from the digest before its closing quote, is exempt only up to the last quote mark inside its verbatim part (an apostrophe counts), and not at all when there is none. |
| 12 | `apply_rule_scope_check` | Clears — never drops — the `rule` label of a finding no scoped section of the loaded team rules covers (#75). A rules file declares a section's scope with a `scope: <token>[, <token>]…` line directly under an ATX heading (`##` to `####`); each such heading also gains an “`(applies to: …)`” note in the team-rules block the model reads, and — with `PRXREF_RULE_SCOPING=on` — a chunk none of whose files the section covers is not shown it at all (see [review-rules.md](review-rules.md#section-scopes)), so the scope binds before the fact and is cleared after it. A label is cleared only when it names a scoped section — the label equals the section's heading text or one of its bullet or numbered rule lines, or is their leading words, both compared case-insensitively with whitespace collapsed — and no such section has every token of its scope cover the finding's path: `java`/`jvm` covers `*.java`, `*.kt`, `pom.xml` and `build.gradle*`; `python` covers `*.py`; `typescript`/`javascript`/`ts`/`js` cover `*.ts`, `*.tsx`, `*.js`, `*.jsx`, `*.mjs` and `*.cjs`; `docs`/`markdown` covers `*.md`, `*.mdx`, `*.rst` and `*.txt`; `openapi`/`specs` covers `*.yaml`, `*.yml` and `*.json` whose name mentions `openapi` or `swagger` or that sit under a `spec`/`specs`/`openapi`/`swagger` directory; `comments` covers every path. Every token of the line must cover the path, so a line names an intersection (`scope: java, comments` reaches Java comment rules). A token outside the vocabulary is inert — it covers every path, because an unknown word must not silently suppress rules. A label that names no scoped section (an unknown name, or a rule from an unscoped section) and any label on a finding with an empty path are kept; a label whose scoped section does not cover the file has `rule` cleared to `null` and the finding kept, so it groups and caps by normalized title like any ruleless finding. Runs on its own guard — any loaded rules file (`PRXREF_REVIEW_RULES` or `PRXREF_SCOPED_RULES`) that declares at least one section scope, whatever the grouping and cap switches say — **after** the hedge gate and **before** grouping and the per-rule cap, so a wrong label never keys either. The claim-category half (`apply_rule_category_check`, right after it, with `PRXREF_RULE_SCOPING=on`) also clears a label whose rule names another kind of defect than the finding: the label is looked up the same way in every section of the loaded rules (scoped or not, plus the rule lines above the first heading), the rule's kinds are read from the matched heading or rule line and its section heading, the finding's from its title alone, over a fixed whole-word vocabulary — `docs` (Javadoc, JSDoc, KDoc, docstring, doc comment, comment, documentation), `unused` (unused, unread, never read/used/called, dead code, unreachable), `style` (style, naming, formatting, whitespace, indentation, camelCase and the other case names, lint, line length) and `errors` (exception, retry, transient, error handling, catch, swallow). It clears when both sides name a kind and share none (a `Stale duplicate Javadoc` finding citing `Remove fields never read`), or when the rule and its section heading name only `style`, the finding is an `error` and its title names no kind (a correctness bug under a style rule). A label naming no rule, and any rule or title naming no kind, keeps its label. That half runs whenever a loaded rules file holds a heading or a rule line. The run record's `rule_scope_cleared` counts the labels both halves cleared; it is `null` when the scope half did not run and the category half cleared nothing. |
| 13 | `apply_rule_grouping` | Opt-in: runs only with `PRXREF_GROUP_FINDINGS` set to `1`. Folds chunk findings in one file that name the same rule (compared case-insensitively), or that name no rule and share a normalized title, into one finding. The finding with the smallest positive line anchors the group; a file-level (line 0) finding anchors only when no member has a line. The anchor takes the group's highest severity and highest confidence, and its body gains `Also at:` followed by each other line of the group as a backticked `<file>:<line>`. Every other member is dropped as `grouped into <file>:<line>`. Only active findings at or above the confidence floor are grouped, and whole-PR sweep findings are never grouped. The same rule in two files makes two groups. Runs **after** the thread, removal and hedge passes, so a dropped finding is never listed as a location, and **before** the gate, so the error cap counts groups, not lines. |
| 14 | `apply_rule_cap` | Runs only when a review rules file is loaded (`PRXREF_REVIEW_RULES` or `PRXREF_SCOPED_RULES`) and `PRXREF_MAX_FINDINGS_PER_RULE` is above 0, which it is by default (2); the model is then asked to name the rule it applied, as with grouping. Caps how many findings one rule produces across the whole review. It considers the findings grouping would (active chunk findings with a valid severity, at or above the confidence floor) and keys them on their rule (compared case-insensitively, with whitespace collapsed) or, when they name none, on their normalized title; the two kinds never mix, and unlike grouping the file is not part of the key. A representative from pass 13 counts once. Within one key the findings are ranked by severity, then confidence, then content, and the first `<n>` (the cap) are kept with their own severity and confidence. Every other one is dropped as `rule cap exceeded (max <n>): listed at <file>:<line>`, naming the best (first-ranked) kept finding. That finding's `locations` gains each folded finding's location and each folded finding's own `locations`, deduplicated, without its own location, and sorted by file and line; its body's last paragraph becomes `Also at:` followed by at most five of them as backticked `<file>:<line>` (a file-level one as its bare `<file>`), then `(+<k> more)` for the other `<k>`, replacing a paragraph pass 13 wrote rather than adding a second. Whole-PR sweep findings are never counted, capped or folded. Runs **after** grouping and **before** verification and the gate, so the error, warning and outofscope caps count what it kept. |
| 15 | `apply_location_verification` | #74. Grouping (pass 13) and the cap (pass 14) list a folded member's location on their representative without ever checking the site against a source, so a member whose own anchor drifted posts a location its evidence does not hold. This pass re-runs pass 5's search for the representative's own quoted snippets within 100 lines of each `Also at:` site and moves the sites nothing corroborates out of `locations` and the `Also at:` paragraph into a separate last paragraph, `Also at (unverified):`, written by the same capped writer (at most five named, then `(+<k> more)`); a second run changes nothing. A file-level (line 0) site is kept when any snippet occurs anywhere in its file. An unreadable file, a dropped finding and a representative with no snippet parseable keep every site; nothing is dropped from the review — a folded member's own drop reason stays true, only the representative's verified list shrinks. Runs once, after grouping and the cap (`locations` is final there) and before the gate and the suggestion pass. Without a reader the pass changes nothing. |
| 16 | `apply_quality_gate` | Severity vocabulary, confidence floor, per-review error cap, and the optional per-review warning and outofscope caps (`PRXREF_MAX_WARNING_FINDINGS`, `PRXREF_MAX_OUTOFSCOPE_FINDINGS`; unset caps nothing, and `spec` is never capped). Returns its findings in content order. |
| 17 | `apply_sweep_dedup` | Drops a sweep finding that restates a chunk finding which **survived** the gate, on file plus normalized title. A chunk finding that grouping folded away (`grouped into <file>:<line>`) or the per-rule cap folded away (`rule cap exceeded (max <n>): listed at <file>:<line>`) still counts here, because the finding it was folded into lists its location. With `PRXREF_DEDUP_SIMILARITY` set, a second tier also drops a **reworded** duplicate: two active findings in the same file and on the same line — or two file-level (line 0) findings of the same file, which compare since #74 — whose titles reach that Jaccard similarity over a dedicated title tokenizer and share at least 3 title tokens. A chunk copy is kept over a sweep copy of equal or lower severity, and a more severe sweep copy is kept alongside it, so the tier never lowers a review's worst severity. Within one side the more severe, then the more confident, copy is kept. Unset, only the exact tier runs. |
| 18 | `apply_containment_note` | Decoration only: suffixes a throw/panic/crash finding that never named its containment boundary. |

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
`Spec: "…"` exemption (pass 11) reads the injected digest only, so an
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

## Execution evidence (#69)

With execution evidence loaded (`--evidence-file` / `PRXREF_EVIDENCE_FILES`,
see [docs/env-vars.md](env-vars.md)), `apply_evidence_drops` runs right after
the example echo check and before every other pass, over chunk and sweep
findings alike. It drops the one contradiction code can check without the
model: a finding that claims a header is missing when an evidence item shows
that header present. A finding is dropped only when one item meets all of:

- **Exit code 0.** A failing run, or one with any other exit code, settles
  nothing, so the finding stays. So does a run whose exit status is unknown:
  a JSON item without `exit_code` (or with `null`), or a text block without
  an `exit:` line. Such an item still rides the prompts, shown as
  `exit: unknown`, but it never drops or raises a finding.
- **A filled header field line.** The item's output holds a `Name: value`
  line (curl's `< ` response marker allowed, CRLF captures too) with a
  non-empty value. The name is matched case-insensitively; a name that only
  appears in prose (`no Cache-Control set`) or an empty `Name:` field is not
  presence.
- **A missing claim about that header.** The finding's title or body names
  the header as a whole token with a missing keyword (`missing`, `absent`,
  `lacks`, `without`, `no`, `not set`, `does not send`, ...) within a few
  words of it in the same clause (clauses split at `.`, `;`, `,`, a line
  break and `but`/`while`/`although`), with no other hyphenated header name
  between the two, and the name is hyphenated (`Cache-Control`) or the text
  says "header". "Cache-Control is set but X-Frame-Options is missing" claims
  nothing about `Cache-Control`. `no-cache` is not a missing keyword, and a claim about the
  header's value (`max-age is too short`) is not a missing claim.
- **The header itself, not a directive.** A claim that a directive or value
  of the header is missing does not say the header is missing, so a probe
  showing the header does not contradict it. When the keyword comes first
  (`missing Cache-Control`, `lacks a Cache-Control header`), the words up to
  the name may not hold a preposition (`in`, `on`, `of`, `for`, ...) or a
  directive word (`directive`, `value`, `flag`, `option`, ...), and none of
  the three words after the name may be a directive word or another
  hyphenated token. When the name comes first (`Cache-Control is not set`,
  `Cache-Control header missing`), only filler (`header`, `is`, ...) may sit
  between the two, and the keyword must end the clause or be followed by a
  preposition (`missing from responses`), never by an object. So
  "Strict-Transport-Security lacks includeSubDomains", "Cache-Control header
  without no-store", "Cache-Control is missing max-age" and "Missing
  includeSubDomains in Strict-Transport-Security" are all kept.
- **The same resource.** When the finding names a URL path (`/fonts/x.otf`), a
  URL or a glob (`*.otf`), the item's command or output must name it, or for
  a glob a path it covers. Repository file paths are not resources. When the
  finding names none, the item's command must probe no specific resource
  either (no URL path past `/`, no `/path`, no glob: `curl -sI
  https://example.com/` or `nginx -T`, but not `curl -sI
  https://example.com/index.html`, which says nothing about the font files a
  finding may mean), and the item must reach the finding: its paths match the
  finding's file, or they match no path of the PR (a global item every chunk
  prompt carries).

The finding gains `drop_reason` `contradicted by execution evidence: <cmd>`,
naming the item's command, and keeps its severity and confidence. Nothing is
relabelled or downgraded, and the model's own verdict on a contradiction
never drops a finding. A deterministic finding and one that already has a
`drop_reason` are left alone. The pass keeps the list's length and order, so
the chunk/sweep boundary holds. When it drops anything, prxref logs `evidence:
dropped N finding(s) the execution evidence contradicts` at INFO, the JSONL
trace gets one `evidence drop` event with `findings: N`, and the summary's
evidence note says how many were dropped, lists the supplied commands with
their exit codes (at most 10, an unknown one shown as `exit unknown`), and
names each dropped finding's title and the command that contradicted it.
Findings dropped as `restates execution evidence: <cmd>` (below) are counted
in the same note (`N finding(s) restating a failing check dropped`) and listed
after them as `Dropped: <title> (<file>), restates <cmd>`, under the same cap
of 10 lines. Without evidence the pass does not run.

### Failing evidence raises findings

The other direction is deterministic too. An evidence item with a non-zero
exit code (never one whose exit status is unknown) is read line by line, and a line whose first position token names a
changed file of the PR raises one finding there:

- **Positions.** `path:line`, `path:line:col` and `path(line,col)`, a trailing
  colon allowed — how ruff, flake8, mypy, ESLint's unix format, tsc, pytest and
  most compilers print a location. The path must name exactly one changed file:
  equal to it, a path-segment suffix of it (`app.py` for `src/app.py`), or an
  absolute CI path ending in it. A path outside the PR, one matching two
  changed files, line `0` and the command line itself raise nothing.
- **The finding.** A `warning` at confidence `1.0` on that file and line,
  titled `Failing check: <the rest of the output line>`, whose body cites the
  command, its exit code and the fenced output line, and ends with
  ` (deterministic check, no model)`. It joins the release-shape, toggle and
  CI-wiring findings at the chunk/sweep boundary, so it runs the same passes
  and severity consistency leaves it alone. On an added line it keeps its
  line through `apply_line_align`; on any other line it snaps like a model
  anchor, or goes file-level, so it never posts outside the diff.
- **The cap.** At most 10 per run, across every item, in item and output order;
  a position named twice raises once.
- **No duplicate comment.** Just before they join, a model finding on the
  same file and line that names the failure — a code from the output line
  (`F401`, `TS2322`, `no-unused-vars`) or the command's tool (`ruff`, past
  wrappers such as `uv run` or `npx`) — is dropped as
  `restates execution evidence: <cmd>`. A different claim on the same line is
  kept.

When anything is raised or restated, prxref logs `evidence: raised N
finding(s) from failing execution evidence (...)` at INFO and the JSONL trace
gets one `evidence raise` event with `findings`, `capped` (positions left out
past the cap) and `restated`. An empty or all-binary diff raises them too.
Because they are ordinary active warnings, `PRXREF_FAIL_ON=any` exits `1` on
one; `never` (the default) still exits `0`, and `error` ignores them.

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

With `PRXREF_GROUP_FINDINGS` set to `1`, a group (pass 13) reaches the output
as one active finding, its representative, plus a dropped audit copy of every
other member.

- **`--format json`.** Every finding row carries `rule` and `locations`, after
  `scope`, and `anchor_unverified` after `locations`. The representative's
  `locations` lists one
  `{"file": ..., "line": ...}` object for each location its `Also at:`
  paragraph names, in the same order. It never repeats the row's own `file`
  and `line`, and it is `null` when `Also at:` names nothing, because every
  other member is file-level or sits on the anchor's line. Each other member
  stays in `findings` as a dropped row with `drop_reason`
  `grouped into <file>:<line>`, `locations` `null`, and its own `rule`, which
  may differ in case from the representative's. `prxref eval` credits a group
  at the row's own location and at each entry of `locations`, never at a
  member row. The per-rule cap (pass 14) fills both keys too: while it is
  active, `rule` is filled as it is under grouping, and the best finding of
  a rule it folds carries `locations` that can span files, listing every
  folded location and not only the five its `Also at:` paragraph shows. Each
  folded finding stays in `findings` as a dropped row with `drop_reason`
  `rule cap exceeded (max <n>): listed at <file>:<line>`, and `prxref eval`
  reads those `locations` exactly as it reads a group's. With grouping off
  and the per-rule cap inactive (no review rules file, or
  `PRXREF_MAX_FINDINGS_PER_RULE` set to `0`), `rule` and `locations` are
  `null` on every row. The run record and `--format json` also carry
  `rule_counts`, the per-rule cap's tally (its rows are described in
  [the README's `--format json` list](../README.md#cli-flags)): `[]` when
  nothing repeated, and `null` when the cap did not run. They carry
  `rule_scope_cleared` too (#75): the count of rule labels pass 12 cleared
  (scope and claim-category halves together), `null` when no loaded rules
  file declares a section scope and the category half cleared nothing.
- **Text output** (`--no-post` or `-v`). Every active finding that names a
  rule, a representative included, ends its line in ` [rule: <rule>]`, after
  any ` [scope: in]` or ` [scope: out]` tag. A representative's body carries
  the `Also at:` paragraph. Each member is listed under
  `dropped:` with its `grouped into <file>:<line>` reason. A group formed by
  the title fallback names no rule, so its line gains no tag and its `rule` is
  `null`.

## Anchor snapping and Also-at verification (#74)

Line align and its hunk-bounded corroboration read only the diff hunks, so
a defect the model quoted correctly but anchored 40-70 lines outside every
hunk is invisible to them: the finding demotes to file-level and the review
posts nowhere near its evidence. Two passes settle anchors by the model's
own quoted code against the head file, through the same reader the chunk
context uses (the forge's `get_file_content` at the head sha, else
`--repo-dir`):

- **Quoted evidence** is parsed from the finding's title and body: a
  backticked span first (the prompt's own way of marking code), then a
  double-quoted span of 4+ characters, then the code inside `catch (…`,
  `if (…`, `for (…` and `while (…`. A span with no 4+-character token, a
  bare path citation (`` `src/app.py:31` `` — a location, not evidence),
  or a URL is not evidence. A body with none of these yields no snippet,
  and the finding keeps line align's verdict.
- **`apply_anchor_snap` (pass 5)** searches ±100 lines around the line the
  MODEL reported — `model_lines`, captured before line align, so a demoted
  finding is searched from where the model said the defect sits. The
  nearest occurrence of the first matching snippet wins, ties to the
  earliest. Import and comment/Javadoc lines are a tiebreak, not evidence:
  a code line holding the snippet anywhere in the window beats them, and a
  comment line is matched only when nothing else holds it. The anchor moves
  only when the move is real: the finding is file-level, it sits on an
  import or comment line while a code line holds the snippet, or the match
  is more than 5 lines from its aligned line. A
  match inside a diff hunk that shares an evidence token with the claim
  settles an ambiguity; otherwise an unbreakable tie keeps the line and
  marks the finding instead of guessing between siblings. A file-level finding whose snippet lies only outside the window anchors
  on its single whole-file occurrence, or is marked when that is not unique.
- **`anchor_unverified`** marks a model finding whose evidence could not
  anchor it: no snippet parseable while it sits file-level, a snippet the
  readable head file does not hold at all, or an ambiguous multi-match. Its
  confidence is not lowered. The flag is a plain finding field — visible
  in `--format json` after `locations` — never a `drop_reason`. A
  deterministic check's finding is never marked or moved: its anchor is
  its own evidence. When nothing can be read (no reader, or the file does
  not resolve at the head sha) the pass is silent: no head file means no
  verdict, not a bad one.
- **`apply_location_verification` (pass 15)** re-runs the same search for
  the representative's own snippets within ±100 lines of each `Also at:`
  site that grouping or the per-rule cap listed, and moves the sites
  nothing corroborates out of `locations` and the `Also at:` paragraph
  into a last paragraph, `Also at (unverified):` — both written with the
  shared capped writer (at most five named, then `(+<k> more)`), and a
  second run changes nothing. A
  file-level site survives when any snippet occurs anywhere in its file.
  An unreadable file keeps every site, a representative with no snippet
  parseable keeps all of them, and a folded member's own drop reason stays
  true — only the list shrinks. It runs once, after grouping and the cap
  and before the gate, so `locations` is final when it reads and verified
  when anything downstream consumes it.

**Line 0 remains the file-level marker.** A finding demoted by align whose
snippet the head file holds is anchored on it (the snap pass above); one
whose snippet is absent keeps line 0 plus `anchor_unverified`; a genuinely
file-level finding (a deterministic check's, or a model's with no quoted
code that still cannot be located) keeps line 0, and the mark says which.
The formatter still omits `:0`, and JSON still renders the `file` alone.
The reworded-duplicate tier now compares line-0 findings per file, so a
file-level restatement of a file-level finding in the same file is dropped
as the duplicate it is, while a line-0 finding and an anchored one still
never compare; the context follow-up (`merge_followup`) confirms a
file-level question on same-file shared evidence tokens for the same
reason.

Neither pass has a knob. They are correctness checks against the head
file, not noise levers.

## Replay runs and the thread passes

A `--no-threads` replay gives passes 6 and 7 (`apply_thread_dedup` and
`apply_settled_thread_suppression`) an empty thread list, so they drop nothing,
and a `--diff-file` replay with no `--pr-url` has no threads to start with. A
replay at pinned SHAs WITHOUT `--no-threads` still dedups against the PR's
*current* threads, which may postdate the pinned head; the CLI logs a warning
saying so. The stale-inline-comment prune never runs on a replay, because a
replay never posts. See the README's "Replay Mode (Evaluation)".

## Drop reasons

| `drop_reason` | Pass | Meaning |
| --- | --- | --- |
| `not confirmed by context follow-up (confidence <x> below floor <y>)` | `merge_followup` (context follow-up) | Only with `PRXREF_CONTEXT_FOLLOWUP=on` at `PRXREF_REPO_CONTEXT=repo` (#22). The chunk worker's finding was below `PRXREF_CONFIDENCE_FLOOR`, a definition it names was looked up and sent to the model once more, and no finding of that re-run confirmed it: none in the same file at or above the floor that sits within 5 lines of it, has the same normalized title, or names a looked-up symbol. The reason is set in the chunk worker, before every pass on this page, and `apply_quality_gate` keeps it instead of writing its own `confidence <x> below floor <y>`. A confirmed finding is replaced by the re-run's finding, which then runs every pass; the re-run's other findings are discarded, never posted, and counted in the run record's `context_followup`. |
| `echoes the prompt's example: "<title>"` | `apply_example_echo_check` | The finding's title, normalized, is the title of the example finding in the worker or sweep template the run used. `<title>` is the example's title as the template writes it. |
| `contradicted by execution evidence: <cmd>` | `apply_evidence_drops` | Only with execution evidence loaded (#69). The finding claims a header is missing (not one of its directives or values), and the exit-0 item `<cmd>` shows that header as a filled `Name: value` line for the resource the finding names, or, when it names none, probes no specific resource. See [Execution evidence](#execution-evidence-69). |
| `restates execution evidence: <cmd>` | `drop_restated_failures` | Only with execution evidence loaded (#69). The failing item `<cmd>` raised a deterministic finding on the same file and line, and this model finding names the same failure (a code from the output line, or the command's tool). See [Failing evidence raises findings](#failing-evidence-raises-findings). |
| `malformed location: '<file>'` | `apply_location_validation` | The finding names a path the diff never touches — empty, non-path, or invented. |
| `anchor mismatch: claims <pkg> but line <n> is <key>` | `apply_manifest_claim_check` | A manifest/lockfile finding names one dependency but is anchored on a different entry. |
| `section mismatch: claims <section> but <pkg> is under <actual>` | `apply_manifest_claim_check` | A manifest/lockfile finding calls an entry a runtime dependency when it lives under `devDependencies`, or the reverse. |
| `duplicate of existing thread` | `apply_thread_dedup` | An open, current thread on the PR already says this, or a resolved or outdated one that a human closed as "won't fix" (#73). The summary's `Thread dedup:` block and the run record's `thread_dedup.suppressed_detail` name the thread each suppressed finding matched (its URL when the forge reports one, else `thread by <author> at <path>:<line>`); `matched_resolved_detail` does the same for findings that matched a resolved thread. |
| `settled in thread: <author>` | `apply_settled_thread_suppression` | An **open, current** thread on the same path already argued this subject out. A resolved or outdated thread never suppresses (#73); a matching finding is kept and posted with a "Previously raised" note. The exception is a won't-fix thread — Azure DevOps `wontFix`/`byDesign`, or an explicit human "won't fix" (`won't fix`, `wontfix`, `will not fix`, `by design`, `working as intended`, stated on its own rather than inside a sentence, in a comment without the prxref attribution) anywhere in the thread on any forge — which keeps suppressing. |
| `refuted in earlier run (<id>)` | `apply_stable_ids` | Only with `PRXREF_STABLE_IDS=1` and a `PRXREF_VERDICT_STORE` loaded (#71). The store holds this finding's stable id with verdict `refuted`: an earlier run's reviewer already answered this claim, and the verdict outlives the thread it was recorded from. `<id>` is the stable id both runs share — the finding's own, or, when a rewording moved its claim hash, the id of the store entry of the same file and rule whose recorded title it restates; the finding also carries `id_reused_from: "verdict"`. |
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
| `duplicate of chunk finding (reworded, similarity <s>)` | `apply_sweep_dedup` | Only with `PRXREF_DEDUP_SIMILARITY` set. A reworded restatement of a kept chunk finding in the same file and on the same line — or, since #74, two file-level (line 0) findings of the same file, which a demoted anchor can no longer hide: a sweep copy no more severe than the chunk copy, or the less severe, then less confident, of two chunk copies. `<s>` is the Jaccard title score to two decimals. |
| `duplicate of sweep finding (reworded, similarity <s>)` | `apply_sweep_dedup` | Only with `PRXREF_DEDUP_SIMILARITY` set. The same between two sweep findings, file-level pair included (#74). A chunk finding never carries it: a chunk copy is never dropped for a sweep copy. |

One more marker is **not** a drop reason. `apply_containment_note` appends
`" [containment boundary not stated]"` to the body of a finding that asserts a
throw, panic, crash, or unhandled rejection without naming the enclosing catch
or the caller it propagates to. The finding still posts; the suffix stops a
correct-but-underscoped "this throws" from reading as a smaller bug than it is.
It runs last, so the posted comment and the dropped-audit copy carry the same
text.

One clearing is not a drop reason either. `apply_rule_scope_check` (pass 12,
#75) sets a finding's `rule` to `null` when no scoped section of the loaded
team rules covers it, or when the cited rule names another kind of defect
than the finding's title (its claim-category half) — the finding itself stays active, so it posts and groups
and caps by its normalized title like any ruleless finding. Nothing is dropped,
so the only trace of the clearing is the label's own `null` and the run
record's `rule_scope_cleared` count.

One mark is not a drop reason either. `apply_anchor_snap` (pass 5, #74) sets
the finding field `anchor_unverified` and lowers confidence by 0.1 when quoted
evidence cannot anchor a model finding; the finding itself stays active (a
sub-floor confidence still dies at the gate's floor, as any other would), and
the only trace is the field, in `--format json` after `locations`.

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
  it off. These seven are the only knobs that tune an existing pass. See
  [Tuning for Your Team](env-vars.md#tuning-for-your-team).
- `PRXREF_STABLE_IDS` (default `0`, #71) adds a pass of its own rather than
  tuning one: `1` turns on `apply_stable_ids` (pass 8), which stamps a
  stable id on every finding and drops a finding the `PRXREF_VERDICT_STORE`
  holds as `refuted`. Its reuse-similarity threshold is a fixed constant.
  With it off the pass never runs and every `id` stays `null`.
- The hedge gate, the manifest checks, the removal-claim check, the
  rule-scope check (#75 — the scope vocabulary lives in the rules file's
  `scope:` lines, not in configuration, and its claim-category vocabulary is
  fixed; `PRXREF_RULE_SCOPING=off` turns both halves off), the anchor-snap and Also-at
  verification passes (#74 — they read the head file, and their window and
  confidence decrement are fixed constants), and the
  pinned-off toggle check have **no knob**. They are correctness checks against the diff itself, not noise
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
