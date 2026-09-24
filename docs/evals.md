# Evaluating prxref: `prxref eval`

A single review of a single PR says little about whether a change to prxref
helped. A new model, a rewritten prompt template, a team rules file, a
setting such as `PRXREF_GROUP_FINDINGS`, or a new prxref build can find more
of what reviewers care about on one PR and less on the next. `prxref eval`
measures the change across a dataset instead. You give it pull requests that
human reviewers have already reviewed, each labelled with the findings those
reviewers left. It reviews every case again through the real pipeline,
grades the findings prxref produced against the labels, and puts two such
runs side by side.

It has three actions:

| Action | What it does | What it writes |
|---|---|---|
| `prxref eval run` | reviews every case, never posting | the run directory `<out>/<label>/` |
| `prxref eval score` | grades one run against its labels | `score.json` and `score.md` in the run directory |
| `prxref eval compare` | prints two scored runs side by side | standard output only |

```bash
prxref eval run --cases tests/evals --label base
prxref eval score --label base
```

`prxref eval` never posts to a forge. It adds no environment variable: its
own settings are the command-line flags below, and the review of each case
reads the same `PRXREF_*` settings `prxref review` does (see
[What a case inherits from the environment](#what-a-case-inherits-from-the-environment)).

- [Exit codes](#exit-codes)
- [Cases](#cases)
- [`prxref eval run`](#prxref-eval-run)
- [`prxref eval score`](#prxref-eval-score)
- [`prxref eval compare`](#prxref-eval-compare)
- [Worked example: two arms](#worked-example-two-arms)
- [The judge prompt](#the-judge-prompt)
- [Where runs are kept](#where-runs-are-kept)

## Exit codes

- `0`: the action finished. `eval run` exits `0` however its cases went. A
  case whose review fails is recorded as a failed case and the run moves on
  to the next one. `PRXREF_FAIL_ON` does not apply to `prxref eval`, which
  never exits `1`.
- `2`: a configuration error, printed to standard error as
  `configuration error: <message>`. The message names what supplied the bad
  value: a flag (`--cases`, `--label`, `--out`, `--rules-file`,
  `--scoped-rules`, `--prompts-dir`, `--judge-model`), the
  argument `A` or `B` of `compare`, or an environment variable. Every
  exit-`2` check of `eval run` happens before any case runs and before
  anything is written. `prxref eval` with no action prints the help to
  standard error and exits `2`.

Log lines, warnings included, go to standard error as `WARNING <message>`.

## Cases

`--cases` names either a `cases.json` file or a directory of `case-*/`
directories. Both forms load into the same cases, so the rest of the harness
never sees which form a dataset used. The whole dataset is checked when it
loads, before any review runs. One bad case makes the command exit `2`, and
no case runs.

### `cases.json`

```json
{
  "version": 1,
  "cases": [
    {
      "id": "widgets-42",
      "pr_url": "https://github.com/acme/widgets/pull/42",
      "base_sha": "0123456789abcdef0123456789abcdef01234567",
      "head_sha": "89abcdef0123456789abcdef0123456789abcdef",
      "expected": [
        {"id": "H1", "file": "src/cart.py", "line": 88, "severity": "error",
         "category": "logic", "accepted": true,
         "text": "total() divides by the item count, which is zero for an empty cart.",
         "must_match": "re:divi(de|sion) by zero|empty cart"},
        {"id": "H2", "file": "src/cart.py", "line": 120, "severity": "minor",
         "text": "The name tmp says nothing about what the value holds."}
      ]
    },
    {
      "id": "local-1",
      "diff_file": "diffs/local-1.diff",
      "context_file": "tickets/local-1.md",
      "spec": ["docs/specs"],
      "expected": []
    }
  ]
}
```

The top level holds exactly `version` (the integer `1`) and `cases` (a
non-empty array). Each case is an object with these fields; any other field
is refused, and a field set to `null` counts as absent.

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | The case's name and its directory name in the run: letters, digits, `.`, `_` and `-`, starting with a letter or digit. Unique in the dataset. |
| `expected` | yes | The human labels: an array, possibly empty. A case with no labels still counts its unmatched AI findings. |
| `pr_url` | one of these two | The pull request, on any forge `prxref review` supports. |
| `diff_file` | one of these two | A unified diff (`git diff` or `git format-patch` output) holding at least one file diff. |
| `base_sha`, `head_sha` | with `pr_url` | The pinned range, as the replay flags take it: a pair, full 40- or 64-character hex, two different commits. Stored lowercased. |
| `context_file` | no | The ticket the PR implements, given to the review as `--context-file`. |
| `spec` | no | Spec sources, as `--spec` takes them: one string or an array of strings, each a local path or an `http(s)` URL. |

A case replays either a local diff (`diff_file`), or a pull request pinned
to a range (`pr_url` with both SHAs). It may also give `pr_url` beside
`diff_file`, with or without the SHAs, as `prxref review` allows. SHAs need
`pr_url`, and `pr_url` without `diff_file` needs both SHAs. A relative
`diff_file`, `context_file` or local `spec` path is read relative to the
directory holding `cases.json`. `context_file` and every local `spec` path
must exist, and a `spec` URL is kept as given.

Each entry of `expected` is one label, a finding a human reviewer left:

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | A non-empty string, unique within the case. |
| `file` | yes | The file path as the diff names it (the post-`b/` form). |
| `line` | yes | The 1-based line in the new file, an integer `>= 1`. |
| `severity` | yes | One of `error`, `warning`, `minor`, `spec`, `outofscope`. Stored as given. |
| `category` | no | The labeller's own class, free-form (`logic`, `security`, `spec`, ...). Recall is reported per category. |
| `accepted` | no | `true` when the PR author acted on the comment, `false` when not. Recall is reported over the accepted labels. |
| `text` | no | The reviewer's words. The judge reads it, so give every label without `must_match` one. |
| `must_match` | no | The deterministic predicate: a plain substring, or a regular expression when prefixed `re:`. A label without one is graded by the judge. |

`minor` is the labeller's lowest tier. It keeps its own name everywhere
except severity agreement, which compares it as `warning`. `spec` and
`outofscope` are prxref's own tiers, for labels that come from prxref's own
vocabulary.

### A directory of `case-*/` directories

This is the layout of the repo's own dataset,
[`tests/evals/`](../tests/evals/README.md). Every `case-*/` directory is one
case, read in name order, and its id is the directory name.

| File | Required | Becomes |
|---|---|---|
| `diff.patch` | yes | `diff_file` |
| `expected.json` | yes | `expected`: a JSON array of labels |
| `ticket.md` | no | `context_file` |
| `docs/` | no | the case's one `spec` source |

`meta.json` is not read. `expected.json` spells two label fields
differently: `line` is `line_hint`, and `category` is `source`. The other
field names are the same, and messages about the file use its spellings. A
directory case has no `pr_url` and no SHAs.

### What makes a dataset invalid

Each of these exits `2` with a message of the form
`--cases: case '<id>': <field>: <problem>`. A case whose id is itself the
problem is named `cases[<n>]`, and a problem with the file as a whole names
the path.

- The file is not an object holding exactly `version` and `cases`,
  `version` is not `1`, or `cases` is empty. A directory holds no `case-*/`
  directory.
- A case id that is missing, not a safe name, or used twice. A field the
  case does not have.
- Neither `pr_url` nor `diff_file`; a `pr_url` no forge recognises; SHAs
  that break the rules above; a `diff_file` that cannot be read or holds no
  file diff; a `context_file` or local `spec` path that does not exist.
- `expected` missing or not an array.
- A label with a required field missing, a `line` below `1`, a `severity`
  outside the five, a `category`, `text` or `must_match` that is not a
  non-empty string, an `accepted` that is not `true` or `false`, a `re:`
  pattern that is empty or does not compile, an id used twice in the case,
  or a field a label does not have.
- **A label that does not anchor.** When the case has a diff file, every
  label's `file` must be a file the diff touches and its `line` a line the
  diff adds. A case pinned to a pull request's range has no diff until it
  runs, so its labels are checked for shape only.

For example:

```text
--cases: case 'c1': expected[0].line: line 1 is not a line the diff adds to 'src/a.py'
--cases: case 'c1': expected[0].severity: must be one of error, warning, minor, spec, outofscope, got the string 'nit'
```

## `prxref eval run`

```bash
prxref eval run --cases PATH --label NAME [--out DIR] [--rules-file PATH] [--scoped-rules PATH] [--prompts-dir DIR] [--resume]
```

| Flag | Meaning |
|---|---|
| `--cases PATH` | The labelled cases: a `cases.json` file, or a directory of `case-*/` directories. Required. |
| `--label NAME` | The run's name, and its directory under `--out`. Required. Letters, digits, `.`, `_` and `-`, starting with a letter or digit. |
| `--out DIR` | The directory that holds the runs. Default `./prxref-eval/`. |
| `--rules-file PATH` | Team review rules for every case, as `prxref review --rules-file`. Overrides `PRXREF_REVIEW_RULES`; `--rules-file ""` turns it off. |
| `--scoped-rules PATH` | Path-scoped review rules for every case, as `prxref review --scoped-rules`. Repeatable. Replaces `PRXREF_SCOPED_RULES`; `--scoped-rules ""` turns it off. |
| `--prompts-dir DIR` | Prompt templates for every case, as `prxref review --prompts-dir`. Overrides `PRXREF_PROMPTS_DIR`; `--prompts-dir ""` turns it off. |
| `--resume` | Continue an existing run instead of refusing it. |

It exits `2`, before any case runs and before anything is written, on a
`--label` that is not a safe name, a bad dataset (naming `--cases`), a
malformed environment (naming the variable), an unusable rules file,
scoped rules entry or prompts directory (naming `--rules-file`,
`--scoped-rules` or `--prompts-dir`, or the variable when the flag is not
given), an existing run directory without `--resume` (naming `--label`), or
a run directory that cannot be created (naming `--out`).

### How a case is reviewed

The cases run one after another, in dataset order. Each is reviewed in
process by the same code `prxref review` runs, as a replay with
`--no-threads`, and it never posts:

- **The case's ticket and spec only.** The review gets the case's
  `context_file` and `spec`, and nothing when the case sets none:
  `PRXREF_TICKET_CONTEXT_FILE` and `PRXREF_SPEC_SOURCES` never reach a case.
- **No threads.** The PR's existing discussion is hidden from the prompt and
  from the thread-dedup passes, as with `--no-threads`.
- **A diff-file case** contacts no forge. It has no threads and no file
  context, and a `git format-patch` file supplies the title and description.
- **A pull-request case** reads the merge-base diff `BASE...HEAD` from the
  forge, with file context at `HEAD`, so it needs the forge's credentials as
  `prxref review` does. Its title and description are pinned as a replay
  pins them: to the PR's first human review, else to its head commit's date.
  When the forge cannot pin them, the review uses the current ones and logs
  a WARNING. A pull-request case with a `diff_file` and no `head_sha` reads
  file context at the PR's current head, with a warning. See
  [Replay Mode](../README.md#replay-mode-evaluation).

Each record carries the replay stamp, and its `description` says which title
and description the reviewer saw. `eval compare` warns when two runs of one
case differ in it:

| `description` | When |
|---|---|
| `file` | a diff-file case: any description came from the diff file (`threads` is `hidden`, `as_of` is null) |
| `pinned` | a pull-request case whose title and description were pinned to the cutoff |
| `live` | a pull-request case that fell back to the current title and description |

### What a case inherits from the environment

Every other setting applies to each case as it does to `prxref review`: the
LLM backend and model chain, the chunking, the quality passes,
`PRXREF_GROUP_FINDINGS`, the price table, and so on. The settings that
matter for a comparison are recorded in `run.json`. In particular:

- `PRXREF_REVIEW_RULES` reaches every case unless `--rules-file` is given.
  `--rules-file ""` turns it off for the run. The run records the file in
  force under `review_rules`.
- `PRXREF_SCOPED_RULES` reaches every case unless `--scoped-rules` is given.
  The flags replace the whole list, and `--scoped-rules ""` turns it off
  for the run. The run records the scoped rules in force under
  `scoped_rules`.
- `PRXREF_PROMPTS_DIR` reaches every case unless `--prompts-dir` is given.
  `--prompts-dir ""` turns it off for the run. The run records the
  templates in force under `prompts.prompt_templates`.
- `PRXREF_TRACE_DIR` is replaced: each case's trace goes to its own
  `trace/` directory.
- `PRXREF_TRACE_FILE` is inherited, so every case appends to the same JSONL
  trace.

So two arms that differ only in their scoped rules or prompt templates are
set with these flags, and `run.json` and the `run` block of `score.json`
tell them apart.

An environment that cannot load at all (a malformed number, a value out of
range) exits `2` before any case runs. The rules file, the scoped rules and
the prompts directory every case shares are loaded once, before the first
case, as `prxref review` loads them, the scoped rules checked against the
rules file. An unusable one exits `2`, naming the flag, or the variable
when the flag is not given, and no case runs. Each case then loads them
again for its own review.

### The run directory

```text
prxref-eval/                        --out
  base/                             one run, named by --label
    run.json                        the run's stamp, written after the last case
    cases/
      case-001-mcp-protocol-upgrade/
        case.json                   the case and its labels, written before it runs
        trace/                      each review unit's prompt, response and meta
        record.json                 the review's JSON record, or error.json
    judge-cache/                    written by eval score
    score.json                      written by eval score
    score.md                        written by eval score
```

- **`cases/<id>/case.json`** holds the case as the harness read it: `id`,
  `pr_url`, `base_sha`, `head_sha`, `diff_file`, `context_file`, `spec` (a
  list) and `expected`, each label with all eight fields, in the
  `cases.json` spelling. `eval score` grades against this copy, so it needs
  no `--cases` and never opens the files the case names.
- **`cases/<id>/record.json`** is the review's record, exactly what
  `prxref review --format json` prints: the verdict, every finding (active,
  then dropped with its `drop_reason`), the chunk counts, `elapsed_ms`,
  tokens and cost, the stamps, and `replay`. A review whose verdict is
  `Error` is a normal `record.json` with that verdict.
- **`cases/<id>/error.json`** replaces it when the review raised:
  `{"case_id": "<id>", "error": "<ExceptionType>: <message>"}`. That covers
  a configuration error the review raised for this case alone, such as an
  unreadable `diff_file`.
- **`cases/<id>/trace/`** holds what `--trace-dir` writes for the review:
  each unit's `.system.md`, `.user.md`, `.response.json` and `.meta.json`
  files (`chunk0`, ..., `sweep`). `eval score` adds the judge's `judge.*`
  files after a live judge call.

`run.json` holds, in this order:

| Key | Value |
|---|---|
| `version` | `1` |
| `label` | `--label` |
| `cases_path` | `--cases` as given |
| `created_at` | when this invocation started, in UTC, like `2026-09-24T10:30:00Z`; a `--resume` rewrites it |
| `case_ids` | every case id, in dataset order; `eval score` reads exactly these |
| `prompts` | `sha256`: the SHA-256 of the packaged `worker`, `systemic` and `summary` templates; `prompt_templates`: the record's stamp of a `PRXREF_PROMPTS_DIR` override, or `null` |
| `sampling` | the reviewer's `temperature`, `seed` and `models` |
| `review_rules` | the record's stamp of the rules file, or `null` |
| `scoped_rules` | the record's stamp of the path-scoped rules (`entries`, `files`, `max_chars`, `units`; never the rules text), or `null` |
| `config` | the settings `llm_backend`, `llm_models`, `llm_max_tokens`, `max_chunks`, `chunk_token_budget`, `chunk_max_files`, `dedup_similarity`, `group_findings`, `max_warning_findings`, `max_outofscope_findings`, `scoped_rules_max_chars` |

`prompts.sha256` always hashes the packaged templates, so an override shows
only under `prompts.prompt_templates`. `prompt_templates`, `sampling`,
`review_rules` and `scoped_rules` are copied from the first case, in dataset
order, whose record's verdict is not `Error`. They are `null` when there is
none, and `review_rules`, `scoped_rules` and `prompt_templates` are `null`
when their input is off.

**No credential is ever written.** `config` is an allowlist of the eleven
settings above, and a record carries no credential. The traces do hold the
prompts, and the prompts hold the diff, so treat a run directory like the
code it reviewed.

Every JSON file is indented, written to a temporary file, and moved into
place, so an interrupted run never leaves a half-written file under its
real name.

### Per-case fencing and `--resume`

Every case is fenced. A crash, a configuration error for that case, or any
other exception is written to the case's `error.json`, and the next case
runs. Standard output gets one line per case as it finishes, then the run
directory:

```text
case-001-mcp-protocol-upgrade: Request-Changes (3 active findings)
case-002-session-token-logging: failed: ConfigError: --diff-file: ...
case-003-config-schema-pin: Approved (1 active finding)
run directory: prxref-eval/base
```

An existing run directory is refused unless you pass `--resume`. With it,
every case that already has a `record.json` or an `error.json` is skipped
(`<id>: skipped (already recorded)`), the rest run, and `run.json` is
rewritten from the records on disk. To run a failed case again, delete its
`error.json` and resume. A skipped case keeps the `case.json` it ran with,
even when `--cases` has changed since.

## `prxref eval score`

```bash
prxref eval score --label NAME [--judge-model MODEL] [--out DIR]
```

| Flag | Meaning |
|---|---|
| `--label NAME` | The run to score, under `--out`. Required. |
| `--judge-model MODEL` | The one model that grades every label without `must_match`. Required when any label of the run lacks `must_match`. |
| `--out DIR` | The directory that holds the runs. Default `./prxref-eval/`. |

It reads `<out>/<label>/run.json` and the cases its `case_ids` list; the
run directory is never listed. Every case needs its `case.json` and either
a `record.json` or an `error.json`. It writes `score.json` and `score.md`
into the run directory, overwriting both on a rescore, and prints the
headline, then the path of `score.md`:

```text
Recall (micro): 37.5% (credit 1.5 of 4 scored labels) over 3 cases
score: prxref-eval/base/score.md
```

It exits `2` on a `--label` that is not a safe name; a run with no
`run.json` (write it with `eval run`, or finish an interrupted one with
`--resume`); a run file that cannot be read; a case with neither
`record.json` nor `error.json`; a malformed environment; and a missing or
unusable `--judge-model`, all before any judge call.

### Two tiers of grading

Only a finding a PR author would receive counts: an AI finding is a row of
the record whose `drop_reason` is null, after every quality pass. Each label
is graded `full` (credit 1), `partial` (credit 0.5) or `none` (credit 0):

- **A label with `must_match`** is graded deterministically, with no LLM
  call. An AI finding credits it `full` when it is in the same `file`
  (exact), within 5 lines of the label's `line`, and its title plus body
  passes the predicate. A plain `must_match` is a substring, compared after
  both sides are lowercased and stripped of backticks, quotes, emphasis
  marks and repeated whitespace. A `re:` pattern is searched anywhere in the
  title and body, case-insensitively. An AI finding without a line never
  matches. This tier never gives `partial`. When several labels compete for
  the same findings, the assignment credits as many labels as the cap allows;
  among a label's candidates the nearest line wins, then the finding sharing
  the most words with the label's `text` and `must_match`.
- **A label without `must_match`** goes to the judge: one single-shot LLM
  call per case that sees the case's judge-tier labels (`id`, `file`,
  `line`, `severity`, `text`) and the active AI findings in those labels'
  files, numbered `A1`, `A2`, ... (with their `file`, `line`, `severity`,
  `title` and `body`). It sees no code and no diff. It answers `full`,
  `partial` or `none` per label, citing the AI finding that earns a credit.
  The code then checks every answer: a credit must name a real finding in
  the label's own file, or it becomes `none`; a label the judge left out is
  `none`. A reply that cannot be read at all, or a call that fails, makes
  every judge-tier label of that case `judge_error`.

`judge_error` is not `none`. A label the judge failed to grade is counted
on its own and left out of every denominator, so a flaky judge lowers
nothing. Judge errors are not cached, so a rescore tries those cases again.

No judge call is made for a case with no active AI finding, because nothing
could be credited, and its judge-tier labels are `none`.

### One AI finding credits at most two labels

A finding that says "this file has problems" must not collect credit for
every label in the file. One AI finding credits at most 2 labels, across
both tiers. The deterministic tier assigns first; a judge credit to a
finding whose 2 slots are already used becomes `none` with a WARNING
(`full` credits keep their slot before `partial` ones, then label order).

A grouped finding (`PRXREF_GROUP_FINDINGS=1`) stands for one problem at
several places. When its row lists extra `locations`, each location is its
own finding for this cap, in both tiers, exactly as the separate findings
it replaced would have been. The judge sees each location of a grouped
finding as its own AI finding, with its own number. A grouped finding still
counts once among the unmatched AI findings.

### Failed cases count

A case whose review failed (`error.json`) and a record whose verdict is
`Error` are scored, not skipped: the author received no findings, so every
label of the case is `none` and counts in the denominator. A failed case is
listed under `failed` and in the `Failed cases` section of `score.md`, and
its verdict reads `failed` in the case table. Its review cost is unknown,
which makes the run's review-cost total unknown too.

### The judge

- **When it is needed.** `--judge-model` is required as soon as one label
  of the run lacks `must_match`; leaving it out exits `2`, naming how many
  labels need it and the first one. When every label has `must_match`, no
  judge is built, `judge` is `null` in `score.json`, and a `--judge-model`
  you gave anyway is logged as unused. The repo's own dataset needs no
  judge.
- **Same backend, no separate key.** The judge runs on the review's own
  LLM backend, base URL, credentials, timeout, temperature and seed, with
  only the model changed to `--judge-model`. It is one model, not a chain:
  an empty value or a comma-separated list exits `2`. There is no judge
  setting other than the flag.
- **The cache.** Each case's graded reply is cached under
  `<out>/<label>/judge-cache/`, keyed on the judge prompt's SHA-256, the
  judge model, and the labels and AI findings exactly as the judge sees
  them. A rescore of an unchanged run makes no judge call. A change the
  judge would see (a label's `text`, a finding's body) misses the cache; a
  field it never sees (`category`, `accepted`) does not. A cache entry that
  is corrupt or no longer parses is a miss, with a WARNING, and is
  overwritten. The judge client is still built on a fully cached rescore,
  so the LLM configuration must still load, but no request is made.
- **Self-judging.** When the judge model is one of the reviewer's own
  models (`sampling.models` of `run.json`, compared case-insensitively), the
  score logs a WARNING and stamps `self_judged: true`, and `score.md` opens
  with a note that the grades may be inflated. It is not an error: the
  score is still written.
- **Traces.** A live judge call writes `judge.system.md`, `judge.user.md`,
  `judge.response.json` and `judge.meta.json` into the case's `trace/`.

### `score.json`

The keys, in order:

- `version`: `1`.
- `label`: `--label`.
- `run`: the run's `prompts`, `sampling`, `review_rules`, `scoped_rules`
  and `config`, copied from `run.json`.
- `judge`: `null` when no judge was built. Otherwise:
  - `model`: `--judge-model`;
  - `sampling`: the judge's `temperature`, `seed` and `models`;
  - `prompt_version` and `prompt_sha256`: the packaged judge prompt's
    version and SHA-256;
  - `self_judged`;
  - `cost_usd` and `cost_estimated`: the cost of this scoring's judge
    calls. A cached case costs nothing, so a fully cached rescore reports
    `0.0`. A call that raised adds nothing, unless every call raised,
    which makes it `null`, and so does one reply nothing could price;
  - `llm_calls`: the judge calls made;
  - `cached`: the cases answered from the cache;
  - `errors`: `[{"case_id", "error"}]` for each case the judge failed to
    grade, by case id.
- `failed`: `[{"case_id", "error"}]` for each case whose review failed, by
  case id.
- `metrics`: the run's metrics, below.
- `cases`: one row per case, sorted by case id, with the keys `case_id`,
  `verdict`, `recall`, `credit`, `scored`, `full`, `partial`, `none`,
  `judge_error`, `ai_findings`, `unmatched_ai`, `severity_compared`,
  `severity_agreed`, `chunks_failed`, `elapsed_ms`, `review_cost_usd`,
  `review_cost_estimated`, `judge_cost_usd`, `judge_cost_estimated` and
  `findings`. Each `findings` entry is one label, sorted by id: `human_id`,
  `file`, `line`, `severity`, `category`, `accepted`, `grade`, `credit`
  (`null` for `judge_error`), `ai_ref` (the index of the crediting row in
  the record's `findings`, dropped rows included), `ai_line` (the line of
  the location credited), `ai_severity`, `severity_agrees` and `method`
  (`must_match` or `judge`). The `ai_*` fields and `severity_agrees` are
  `null` unless the label was credited.

### The metrics

`metrics` holds these keys, in order:

- **`case_count`**: the cases scored.
- **`recall`**: the headline, micro recall over every label of every case:
  `credit / scored`, where `full` credits 1, `partial` 0.5 and `none` 0.
  The block holds `recall`, `credit`, `scored`, `full`, `partial`, `none`
  and `judge_error`; `judge_error` labels are outside `scored`, and
  `recall` is `null` when nothing was scored. Micro means every label weighs
  the same, so a case with more labels weighs more; the per-case rows give
  each case's own recall.
- **`recall_by_severity`**: the same block per human severity, as
  labelled (`minor` has its own row).
- **`recall_by_category`**: the same block per `category` (the directory
  form's `source`), with labels that have none under `(none)`. On the
  repo's dataset, `spec` is spec recall and `generic` the plain bugs.
- **`recall_accepted`**: the same block over the labels whose `accepted`
  is `true`.
- **`unmatched_ai`**: `total`, the active AI findings that credit no label;
  `ai_findings`, all active AI findings; and `per_pr`, `total` divided by
  `case_count`. Labels are a floor, not an exhaustive list, so an unmatched
  finding is not necessarily wrong: read this as noise per PR, not as
  precision.
- **`severity_agreement`**: over the credited labels, whether the crediting
  AI finding has the label's severity, both lowercased and a `minor` read
  as `warning` on either side. It holds `compared`, `agreed`, `rate`
  (`null` when nothing was credited) and `confusion`, which maps each human
  severity to each AI severity to a count.
- **`chunks_failed`** and **`elapsed_ms`**: `total` over the cases, and
  `missing`, the cases with no usable value (a failed case), which are
  never counted as 0.
- **`review_cost`** and **`judge_cost`**: `total_usd`, `per_pr_usd`,
  `priced`, `unpriced` and `estimated` (the cases whose cost came from
  `PRXREF_PRICE_TABLE`). **An unknown cost is never summed:** one unpriced
  case makes `total_usd` and `per_pr_usd` `null`, and `unpriced` says how
  many. `judge_cost` sums the per-case judge costs, where a case whose judge
  call raised is unpriced, so it can be `null` while `judge.cost_usd`, which
  skips a raised call, is a number.

### `score.md`

The same result for a reader. It opens with `# prxref eval score: <label>`,
the headline, and the self-judging note when stamped, then these sections,
in order: `## Failed cases`, `## Cases` (one row per case), `## Recall by
severity`, `## Recall by category`, `## Accepted labels`, `## Unmatched AI
findings`, `## Severity agreement`, `## Chunks failed`, `## Elapsed`,
`## Cost` and `## Judge`. An empty section reads `None.` (or `No labels.`
for a recall table), an unknown value reads `unknown`, and a recall with
nothing scored reads `n/a`. It holds no wall-clock time of the scoring run.

This is the `score.md` of a three-case fixture run in the test suite, with a
judge; case `c` failed:

```markdown
# prxref eval score: L

Recall (micro): 37.5% (credit 1.5 of 4 scored labels) over 3 cases

## Failed cases

- `c`: RuntimeError: boom

## Cases

| Case | Verdict | Recall | Credit | Full | Partial | None | Judge error | AI findings | Unmatched AI | Chunks failed | Elapsed | Review cost | Judge cost |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| a | Request-Changes | 100.0% | 1 of 1 | 1 | 0 | 0 | 0 | 2 | 1 | 0 | 1.2 s | $0.0100 | $0.0000 |
| b | Request-Changes | 25.0% | 0.5 of 2 | 0 | 1 | 1 | 0 | 1 | 0 | 0 | 0.8 s | $0.0200 | $0.0020 |
| c | failed | 0.0% | 0 of 1 | 0 | 0 | 1 | 0 | 0 | 0 | unknown | unknown | unknown | $0.0000 |

## Recall by severity

| Severity | Recall | Credit | Full | Partial | None | Judge error |
|---|---:|---:|---:|---:|---:|---:|
| error | 50.0% | 1 of 2 | 1 | 0 | 1 | 0 |
| minor | 0.0% | 0 of 1 | 0 | 0 | 1 | 0 |
| warning | 50.0% | 0.5 of 1 | 0 | 1 | 0 | 0 |

## Recall by category

| Category | Recall | Credit | Full | Partial | None | Judge error |
|---|---:|---:|---:|---:|---:|---:|
| (none) | 0.0% | 0 of 2 | 0 | 0 | 2 | 0 |
| logic | 100.0% | 1 of 1 | 1 | 0 | 0 | 0 |
| security | 50.0% | 0.5 of 1 | 0 | 1 | 0 | 0 |

## Accepted labels

Recall over accepted labels: 100.0% (credit 1 of 1 scored labels).

## Unmatched AI findings

1 of 3 active AI findings matched no label: 0.33 per PR.

## Severity agreement

Agreed on 1 of 2 credited labels (50.0%); a human `minor` counts as `warning`.

- human `error`, AI `error`: 1
- human `warning`, AI `error`: 1

## Chunks failed

0 in total; unknown for 1 case(s), which are not counted.

## Elapsed

2.0 s in total; unknown for 1 case(s), which are not counted.

## Cost

- Review: unknown (1 of 3 case(s) unpriced; an unknown cost is never summed)
- Judge: $0.0020, 1 call(s), 0 case(s) from the cache

## Judge

- Model: `judge-m`
- Prompt: version 1, sha256 `<sha256 of the packaged judge prompt>`
- Self-judged: no
- Judge errors: none
```

With no judge, the `## Judge` section reads `No judge: every label has a
must_match predicate.` and the cost line reads `- Judge: none (no judge
ran)`.

## `prxref eval compare`

```bash
prxref eval compare A B [--out DIR]
```

`A` and `B` each name a scored run. **A value is a label** when it is a
safe name and `<out>/<value>/` is a directory, even when a directory of the
same name sits in the working directory. Otherwise it is the path of a run
directory, so `./base` always means the directory `base` here. Only each
run's `score.json` is compared. A case's `record.json` is read for its
replay stamp alone, and the runs are never rescored.

It exits `2`, naming `A` or `B`, when a run is neither a label under
`--out` nor a directory holding a `score.json` (a run that has a `run.json`
but no `score.json` gets the `eval score` command to run first), when a
`score.json` or `record.json` cannot be read, or when a `score.json` is not
version `1` or not shaped like one.

### The report

Standard output is Markdown, in this order:

- `# prxref eval compare`, then `- A: <A>` and `- B: <B>` as you typed them.
- `## Metrics`: a table `| Metric | A | B | Change |`. Its rows are
  `Cases`, `Recall (micro)`, `Judge errors`, one `Recall, severity` row per
  severity and one `Recall, category` row per category found in either run
  (each sorted), `Recall, accepted labels`, `Unmatched AI per PR`,
  `Severity agreement`, `Chunks failed`, `Elapsed`, `Review cost` and
  `Judge cost`. A severity or category only one run has reads `n/a` on the
  other side.
- `## Changed labels`: a table `| Case | Label | Location | A | B |` of every
  label in both runs whose grade changed, sorted by case and label id. A
  cell reads `full (1)`, `partial (0.5)`, `none (0)` or `judge_error`. When
  the label's location differs between the runs, the cell reads
  `<A location> (B: <B location>)`.
- `## Only in one run`: one bullet per case (``- Case `c3`: only in B (2
  labels)``) or label (``- Label `H4` of case `c1` at `src/a.py:40`: only
  in B``) that only one run has, sorted. These are not counted as changes.

An empty section reads `None.`.

The `Change` column is B minus A:

| Metric | Change |
|---|---|
| a recall, `Severity agreement` | percentage points: `-37.5 pp`, `0.0 pp` |
| `Cases`, `Judge errors`, `Chunks failed` | a count: `+1`, `0` |
| `Unmatched AI per PR` | `+0.50` |
| `Elapsed` | seconds: `+0.5 s` |
| `Review cost`, `Judge cost` | dollars: `+$0.0010`, `$0.0000` |

The change is `unknown`, never a number, when either side is unknown: a
`null` value (a recall with nothing scored, an unpriced cost), a metric one
run lacks, or a total that leaves a case out, whose cell then reads
`<total> (<n> unknown)`. A run with no judge has the judge cost `none`,
counted as 0. The judge cost is `judge.cost_usd` of `score.json`: what that
scoring's judge calls cost, so a run scored from the cache shows `$0.0000`.

### Warnings

The comparison is always printed. Before it, a WARNING is logged when the
two runs are not like for like:

1. **The judge prompt differs**: `the judge prompt sha256 differs: A "...",
   B "..."; the judge grades are not like for like`. The runs were scored by
   different prxref builds whose judge prompts differ.
2. **The judge model differs**: `the judge model differs: A "...", B
   "..."; the judge grades are not like for like`. Rescore both runs with
   the same `--judge-model`. Two runs
   without a judge never warn, and a run without a judge warns against a
   judged run only when both runs graded labels through a judge.
3. **The cases differ**: `the runs cover different cases (only in A: ...;
   only in B: ...); the metrics are not like for like`. The headline
   metrics cover different PRs.
4. **A replay description differs**: `case '<id>': the replay description
   differs: A "pinned", B "live"; the two arms did not see the same PR
   description, so this case is not a like-for-like comparison`. Checked for
   every case with a `record.json` in both runs; a missing stamp reads
   `null`. It usually means the forge's description history could be read
   for one arm and not the other.

### Byte-identical output

The report holds no timestamp and no path other than `A` and `B` as given,
and it is ASCII. Comparing the same two runs twice prints the same bytes, so
a report can be kept in a repository and diffed.

## Worked example: two arms

Measure whether another model finds more of the labelled problems. Each arm
is one `eval run` with its own `--label`, and the only difference is the
environment:

```bash
# Arm A: the configuration as it is
prxref eval run --cases tests/evals --label base

# Arm B: the same cases, another model
PRXREF_LLM_MODELS=example-model-b prxref eval run --cases tests/evals --label cand

# Grade both. Every label of tests/evals has must_match, so no --judge-model
prxref eval score --label base
prxref eval score --label cand

prxref eval compare base cand > compare.md
```

A dataset with judge-tier labels needs `--judge-model` on both `score`
commands, and the same model on both, or `compare` warns.

This is what a comparison looks like. It is the fixture pair the test suite
pins (two made-up cases `c1` and `c2`, graded partly by a judge), not the
result of the commands above:

```text
# prxref eval compare

- A: base
- B: cand

## Metrics

| Metric | A | B | Change |
|---|---:|---:|---:|
| Cases | 2 | 2 | 0 |
| Recall (micro) | 62.5% (2.5 of 4) | 25.0% (1 of 4) | -37.5 pp |
| Judge errors | 0 | 1 | +1 |
| Recall, severity `error` | 75.0% (1.5 of 2) | 0.0% (0 of 1) | -75.0 pp |
| Recall, severity `minor` | 100.0% (1 of 1) | 100.0% (1 of 1) | 0.0 pp |
| Recall, severity `spec` | n/a | 0.0% (0 of 1) | unknown |
| Recall, severity `warning` | 0.0% (0 of 1) | 0.0% (0 of 1) | 0.0 pp |
| Recall, category `(none)` | n/a | 0.0% (0 of 1) | unknown |
| Recall, category `logic` | 75.0% (1.5 of 2) | 0.0% (0 of 1) | -75.0 pp |
| Recall, category `security` | 100.0% (1 of 1) | 100.0% (1 of 1) | 0.0 pp |
| Recall, category `style` | 0.0% (0 of 1) | 0.0% (0 of 1) | 0.0 pp |
| Recall, accepted labels | 100.0% (1 of 1) | 0.0% (0 of 1) | -100.0 pp |
| Unmatched AI per PR | 0.00 (0 of 3) | 0.50 (1 of 2) | +0.50 |
| Severity agreement | 66.7% (2 of 3) | 100.0% (1 of 1) | +33.3 pp |
| Chunks failed | 0 | 1 | +1 |
| Elapsed | 3.0 s | 3.5 s | +0.5 s |
| Review cost | $0.0300 | unknown | unknown |
| Judge cost | $0.0020 | $0.0030 | +$0.0010 |

## Changed labels

| Case | Label | Location | A | B |
|---|---|---|---|---|
| c1 | H1 | src/a.py:10 | full (1) | none (0) |
| c1 | H3 | src/a.py:30 | partial (0.5) | judge_error |

## Only in one run

- Label `H4` of case `c1` at `src/a.py:40`: only in B
```

Reading it:

- B's micro recall fell from 62.5% to 25.0%. But B graded `H3` as
  `judge_error`, which is left out of its denominator (`1 of 4` counts the
  new `H4` and not `H3`), and B has a label A does not, so rescore before
  trusting the headline.
- `H1` lost its credit: B's findings no longer raise it. The
  `Changed labels` table is where to start reading B's records.
- B's review cost is `unknown` because one of its cases could not be priced,
  so the change is `unknown` too, not a misleading number.
- B failed a chunk that A did not, which alone can explain missing findings.

## The judge prompt

The judge prompt is packaged with prxref and cannot be overridden:
`PRXREF_PROMPTS_DIR` and `--prompts-dir` replace only the reviewer's
`worker.md`, `systemic.md` and `summary.md`, and `prxref prompts export`
never writes a judge template. Two runs reviewed with different templates
are therefore always graded by the same judge. `score.json` stamps the
prompt's version and SHA-256 under `judge.prompt_version` and
`judge.prompt_sha256`, and `eval compare` warns when two runs were graded
by different judge prompts. See
[Prompt Template Overrides](prompt-templates.md).

## Where runs are kept

`--out` defaults to `./prxref-eval/` in the working directory, for all
three actions. Add it to your `.gitignore`: a run holds every prompt and raw
model response, and those hold the code under review.

```gitignore
prxref-eval/
```

To run the repo's own three cases, see
[`tests/evals/README.md`](../tests/evals/README.md).
