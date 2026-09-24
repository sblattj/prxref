# Team Review Rules

Most teams already keep a written review checklist: "every network call has a
timeout", "no migration without a rollback", "a TODO names its ticket". Point
prxref at that file and every review unit reads it as part of its
instructions. Rules that concern only part of the repository, such as Java
conventions or Helm chart checks, can go in
[path-scoped rules files](#path-scoped-rules) instead, which reach only the
review units whose files they match.

```bash
prxref review --pr-url https://github.com/acme/widget/pull/42 --rules-file .prxref/rules.md
# or, for every run of this process (the flag wins when both are set):
export PRXREF_REVIEW_RULES=/etc/prxref/rules.md
```

- The file is Markdown or plain text, UTF-8, with optional front matter.
- `PRXREF_REVIEW_RULES` names it for every run. `--rules-file PATH` names it
  for one run and wins over the variable. `--rules-file ""` turns an
  environment-configured file off for one run.
- Unset (the default), nothing changes: the prompts, the trace files, the
  JSONL trace and the exit code are what they would be without the feature,
  and the run record carries `review_rules: null`.

**Read the rules from a trusted checkout, never from the pull request under
review.** See [CI safety](#ci-safety-read-the-rules-from-something-the-pr-cannot-change).

## Where the rules go

The rules are **operator policy**, so they go in the **system** half of each
prompt, after prxref's own instructions, under a `## Team review rules`
heading. They never touch the user half, which holds the PR's own data (the
title, the description, the ticket context, the spec constraints, the diff).
Every chunk worker gets the rules file, and so does the whole-PR systemic
sweep, each with its own framing (path-scoped rules files join the same
block, but only in the units they match; see
[What each unit receives](#what-each-unit-receives)):

- **A chunk worker** is told to check its chunk against the rules as well, and
  that everything above still binds: a finding must cite a line of the diff,
  follow the Confidence and No Speculation rules, and use only the Severity
  Vocabulary. A rule the diff cannot show evidence for — a test run, a linked
  ticket, a sign-off — produces no finding.
- **The sweep** is told to apply only the rules about a whole-PR or cross-file
  property the digest can show ("every new migration ships a rollback"). Rules
  about individual lines belong to the chunk workers; repeating them in the
  sweep would only duplicate their findings.

The body sits inside `<team_rules>` … `</team_rules>` tags, so the file's own
`##` headings never read as siblings of prxref's sections. The block for a
chunk worker looks like this:

```text
## Team review rules

The team that owns this repository reviews changes against the rules below. Check this chunk against them as well. […]

Team severity words map onto that vocabulary: `blocker` → `error`; `major` → `warning`; `must fix` → `warning`; `nit` → `outofscope`. Classify a problem by the team's definition, then write the mapped word in `severity`.

<team_rules>
# Team rules

- blocker: any network call without an explicit timeout.
- major: a function longer than 80 lines.
- nit: a TODO without a ticket id.
</team_rules>
```

That is the block for the example file in
[Front matter and the severity map](#front-matter-and-the-severity-map).

- The severity paragraph appears only when the file maps severity words (see
  below).
- The `<team_rules>` section appears only when the body has text.
- A file with neither a body nor a map adds nothing at all, not even the
  heading.
- The rules are added verbatim. Braces such as `{diff}`, or a line reading
  `## Review Context`, render literally and cannot move anything else in the
  prompt.
- The timeout retry of a chunk keeps the rules. The retry trims bulk context,
  and the rules are policy, not context.

To see exactly what each unit received, run with `--trace-dir DIR`: the rules
block is in `DIR/chunk0.system.md` … and `DIR/sweep.system.md`, and in no
`*.user.md`.

## Front matter and the severity map

A team usually has its own severity words. Map them onto prxref's tiers in a
`severity:` block of the file's front matter:

```markdown
---
name: team-review
description: |
  The checklist every reviewer on this team uses.
severity:
  blocker: error        # a merge blocker
  major: warning
  "Must Fix": warning
  nit: outofscope
---
# Team rules

- blocker: any network call without an explicit timeout.
- major: a function longer than 80 lines.
- nit: a TODO without a ticket id.
```

The model is shown the map and asked to write prxref's tier. If it writes the
team's word anyway, a deterministic pass rewrites it before every other
quality pass, so `blocker` reaches the quality gate as `error` instead of
being dropped as `invalid severity: 'blocker'`. See
[docs/quality.md](quality.md#severity-map-from-team-review-rules).

**The grammar, exactly:**

- **The fence.** Front matter exists only when the file's first line is `---`
  and a later line is also `---`. The first such later line closes it, and
  the body starts after it. A `---` anywhere else is an ordinary Markdown rule.
  A first-line `---` that never closes is logged as a warning, and the whole
  file is then rules text.
- **Comments and blank lines.** Inside the fence, `#` at the start of a line
  or after a space or tab starts a comment, and blank lines are skipped.
- **Only `severity:` is read.** Every other top-level key (`name:`,
  `description:`, …) is ignored and named once in an INFO log line, and any
  indented lines under it are skipped. So a Claude-style skill file,
  multi-line `description: |` included, works unmodified. An `applies_to:`
  or `applyTo:` key is skipped the same way but named in a WARNING instead,
  because this file reaches every unit whatever the key says. Only a
  [path-scoped rules file](#path-scoped-rules) reads `applies_to:`.
- **The map.** `severity:` has nothing after the colon, and each indented line
  under it is `<word>: <tier>`. Either side may be quoted, and a word may
  contain spaces (`Must Fix`). Words are case-insensitive, and runs of
  whitespace inside them count as one space.
- **The tiers** are `error`, `warning` and `outofscope`. `spec` is reserved
  for findings grounded in a quoted spec constraint (`PRXREF_SPEC_SOURCES` /
  `--spec`), so it is never a legal target: a team word mapped onto it would
  mint spec findings on runs with no spec at all.
- **prxref's own words** (`error`, `warning`, `spec`, `outofscope`) cannot be
  remapped. The identity `error: error` is allowed and ignored.
- An empty `severity:` block is legal and maps nothing.

Each of these is a configuration error (exit `2`), reported as
`<source>: <path>:<line>: <problem>`:

- an inline value (`severity: {blocker: error}`, `severity: error`);
- a second `severity:` key;
- an entry that is not `<word>: <tier>`: a YAML list item (`- blocker`), or a
  nested block;
- an unknown tier (`blocker: eror`), or `spec`;
- a remap of one of prxref's own words (`warning: error`);
- one word mapped to two different tiers.

For example:

```text
configuration error: --rules-file: .prxref/rules.md:7: unknown severity 'eror' for 'blocker'; expected one of error, outofscope, warning
```

The map is nested under `severity:`, rather than written as flat top-level
`word: tier` lines, so the file can carry other front matter (a skill file's
`name:` and `description:`). It also keeps the map strict: a typo in a tier
is an error rather than something silently skipped.

## The cap, the hash and the run record

- **The cap.** `PRXREF_REVIEW_RULES_MAX_CHARS` (default `12000`, must be
  greater than 0) caps the **body**: the text after the front matter, with
  surrounding whitespace stripped. A longer body is cut at the cap, and the
  block then ends with
  `[team rules truncated: only the first 12000 of 18344 characters are shown]`.
  prxref also logs one WARNING per run that names
  `PRXREF_REVIEW_RULES_MAX_CHARS`.
- **The hash.** `sha256` covers the raw bytes of the whole file, front matter
  included, before decoding and capping. It equals `shasum -a 256 FILE`, it
  does not change when you change the cap, and it changes when any byte of
  the file does. It is how you tell which version of the rules reviewed which
  PR.
- **Strict text.** The file must be UTF-8 (a leading BOM is dropped, CRLF and
  CR become LF) with no NUL bytes.

Every review result carries a `review_rules` record, `null` when no rules are
configured:

```json
{"path": ".prxref/rules.md", "sha256": "<64 hex>", "chars": 18344, "max_chars": 12000,
 "truncated": true,
 "severity_map": {"blocker": "error", "major": "warning", "must fix": "warning", "nit": "outofscope"}}
```

- `path` is the path as configured, not resolved.
- `chars` and `truncated` describe the body after the front matter; `sha256`
  covers the whole file.
- The record never carries the rules text.

It appears in these places:

| Where | What |
|---|---|
| `--format json` | the `review_rules` key, always present, `null` when off |
| `-v` text output | `rules: .prxref/rules.md sha256=<first 12 hex> chars=18344 (truncated at 12000)` |
| JSONL trace (`PRXREF_TRACE_FILE`) | one `rules ok` event whose meta is the record, right after `run start`; a `rules remap` event with `findings=<n>` when the map rewrote any finding |
| `--trace-dir` | the rules block itself, in every `<unit>.system.md` |

Nothing about the rules is added to the posted comments.

## Path-scoped rules

Some rules concern only part of a repository: Java conventions mean nothing
to a Helm chart, and a chunk of Helm templates should not pay for them.
Put such rules in **path-scoped rules files**. Each file says which paths it
covers, and each chunk worker reads only the files that match its own paths.

```bash
prxref review --pr-url https://github.com/acme/widget/pull/42 \
  --rules-file .prxref/rules.md --scoped-rules .prxref/scoped
# or, for every run of this process (the flags win when both are set):
export PRXREF_SCOPED_RULES=/etc/prxref/scoped
```

```markdown
---
applies_to: ["**/*.java", "!**/src/test/**"]
severity:
  nit: outofscope
---
- blocker: a public method that returns null instead of an Optional.
- nit: a wildcard import.
```

- `PRXREF_SCOPED_RULES` is a list of rules files and directories, separated
  by commas or whitespace. `--scoped-rules PATH` names one entry and can be
  repeated. When given, the flags replace the variable for one run rather
  than adding to it, and `--scoped-rules ""` turns the variable off. The
  flag can name a path that contains a space; the variable cannot.
- Scoped files add to the always-on `PRXREF_REVIEW_RULES` file and never
  replace it. Either one works without the other.
- Unset (the default), nothing changes: the prompts, the posts, the trace
  and the logs are what they would be without the feature, and the run
  record carries `scoped_rules: null`.
- Read them from a trusted checkout, exactly as the always-on file; see
  [CI safety](#ci-safety-read-the-rules-from-something-the-pr-cannot-change).

### `applies_to`

A scoped file names the paths it covers in an `applies_to:` front-matter
key. `applyTo:`, the spelling of GitHub Copilot's
`.github/instructions/*.instructions.md` files, is an alias, and the key's
name is matched case-insensitively. The value takes one of three forms:

- **A string**, split on commas: `applies_to: "**/*.ts, **/*.tsx"`.
- **A one-line flow list** of double-quoted globs, never split:
  `applies_to: ["**/*.java", "!**/src/test/**"]`.
- **A block list**: nothing after the colon, then one `- <glob>` line per
  glob, bare or quoted, never split:

  ```yaml
  applies_to:
    - "helm/**"
    - "**/Chart.yaml"
  ```

**Matching.**

- Each glob is matched against the whole diff path, which is relative to the
  repository root, with Python's `fnmatch.fnmatchcase`, as
  `PRXREF_SIZE_IGNORE_GLOBS` is. So the match is case-sensitive on every
  host, and `*` crosses `/`: `src/*.java` also matches `src/a/b/C.java`.
- A glob that starts with `!` excludes. A path is selected when **any**
  positive glob matches it and **no** `!` glob does, wherever each sits in
  the list. This is not `.gitignore`'s last-match-wins: a positive glob
  after a `!` cannot bring a path back.
- A `**/` at the start of a glob, or right after a `/`, also matches zero
  directories: `**/*.java` selects a root-level `Foo.java`, and
  `src/**/*.java` selects `src/Foo.java`. Plain `fnmatch` needs a `/` there,
  and `PRXREF_SIZE_IGNORE_GLOBS` keeps that stricter match.
- A chunk's paths are the paths of its files, plus the old path of a renamed
  file, so a rules file scoped to where a file used to live still reaches
  the rename. A binary file sits in no chunk, so it selects nothing.

**A file without `applies_to`** reaches every unit, as the always-on file
does, and prxref says so at INFO. `applies_to: []` is an error, never "no
unit".

Each of these is a configuration error (exit `2`), reported as
`<source>: <path>:<line>: <problem>`:

- an empty value: `[]`, `""`, `~`, or a key with no entries;
- an empty entry, or an entry that is not a string (`[1]`, `- true`, a
  nested list or mapping);
- a glob that starts with `/`, since diff paths are relative;
- a list of only `!` globs, which can match no path, and a `!` not followed
  directly by its glob;
- the key twice in one file (`applies_to` and `applyTo` count as one key);
- a block scalar (`|` or `>`), a flow list that does not close on the line
  that opens it, an inline value followed by indented lines, or a malformed
  quoted string.

`applies_to` in the always-on `PRXREF_REVIEW_RULES` file does nothing, since
that file reaches every unit: prxref ignores the key and names it in a
WARNING.

### Loading

- **Entries.** Each entry is a file or a directory. A directory is read one
  level deep: every name directly inside it that ends in `.md`
  (case-sensitive) and does not start with `.`. Subdirectories are not
  searched, and one whose own name ends in `.md` is an error.
- **Load order** is entry order, with each directory's files taking its
  place in name order (code-point order). A file reached twice, because it
  is listed twice or listed beside its directory, loads once, at its first
  position.
- **At most 50 files** in all, counted across every entry. More is an error,
  raised before any file is read.
- A directory with no `*.md` file logs a WARNING. The feature is still on,
  and the record shows `"files": []`.
- **Every file is read as the always-on file is**: a URL is refused, the
  file must be a regular file of strict UTF-8 with no NUL bytes, and a path
  inside the working directory must still resolve inside it once its
  symlinks are followed. A directory entry is confined the same way.
- **The per-file cap.** `PRXREF_REVIEW_RULES_MAX_CHARS` caps each scoped
  file's body, as it caps the always-on file's, with the same truncation
  line and the same WARNING.
- Front-matter keys other than `severity:` and `applies_to:` are ignored and
  named at INFO.

Every file is loaded, checked and merged before any network call, so a bad
one exits `2` without touching the forge or the model. The error names
`--scoped-rules` when the flags were given, else `PRXREF_SCOPED_RULES`.

### Severity maps across files

A scoped file can carry a `severity:` map, in the grammar
[above](#front-matter-and-the-severity-map). All the maps merge into one
run-wide map: the always-on file's words first, then the words only scoped
files map, in load order. It is the map the severity-remapping pass applies
to every finding, so a scoped file's map works even with no always-on file
set. It is also the severity paragraph of every unit's block, the units the
file does not reach included, because a finding's severity is remapped the
same way wherever it comes from.

One team word mapped to two different tiers, by two scoped files or by a
scoped file and the always-on file, is a configuration error naming both
files, with the line of each scoped file's mapping:

```text
configuration error: --scoped-rules: .prxref/scoped/legacy.md:3: 'blocker' is mapped to warning here but to error in the always-on rules file '.prxref/rules.md'; map each team word to one tier across all rules files
```

### What each unit receives

A unit's block is still one `## Team review rules` heading and one framing
paragraph, then the merged severity paragraph and the always-on
`<team_rules>` element. Each scoped file the unit selects follows in load
order, in its own element that names the file and its globs. For a chunk that
touches `src/main/java/App.java`:

```text
## Team review rules

The team that owns this repository reviews changes against the rules below. Check this chunk against them as well. […]

Team severity words map onto that vocabulary: `blocker` → `error`; `nit` → `outofscope`. Classify a problem by the team's definition, then write the mapped word in `severity`.

<team_rules>
- blocker: any network call without an explicit timeout.
</team_rules>

<team_rules source=".prxref/scoped/java.md" applies_to="**/*.java, !**/src/test/**">
- blocker: a public method that returns null instead of an Optional.
- nit: a wildcard import.
</team_rules>
```

- **The sweep** gets the union of the chunks' selections, with the sweep's
  own framing.
- A file without `applies_to` has no `applies_to` attribute in its element.
- **A unit no scoped file reaches** gets exactly the block it would get
  without scoped rules, unless a scoped file maps a team word the always-on
  file does not. Then the unit's block carries the merged map; with no
  always-on file, the block is the heading, the framing and that
  paragraph.
- The timeout retry of a chunk keeps the chunk's own block.

### The per-unit cap

`PRXREF_SCOPED_RULES_MAX_CHARS` (default `24000`, must be greater than 0)
caps the scoped text one unit receives:

- It counts the scoped files' body characters only: not the always-on body,
  which has its own cap, and not the heading, tags or markers.
- Whole files go in, in load order, while they fit. There is no repacking:
  the first file that does not fit is cut to the room left and followed by
  the usual truncation line, and every later file with a body is left out,
  even one small enough to fit.
- A file that fits exactly is not cut. When no room is left at all, the
  next file is left out rather than cut to nothing.
- A selected file with an empty body costs nothing and is never left out.

The files a unit leaves out are named by one marker that closes its block:

```text
[team rules omitted: .prxref/scoped/java.md (over the 24000-character limit on scoped rules for one review unit)]
```

and prxref logs one WARNING per run that names the variable, how many units
the cap touched (the sweep counts as a unit), and the files it cut or left
out:

```text
scoped rules exceed PRXREF_SCOPED_RULES_MAX_CHARS (24000 characters per review unit) in 2 of 3 review unit(s); truncated: .prxref/scoped/helm.md; omitted: .prxref/scoped/java.md
```

### The run record

Every review result carries a `scoped_rules` record, `null` when no scoped
rules are configured:

```json
{"entries": [".prxref/scoped"],
 "files": [
   {"path": ".prxref/scoped/helm.md", "sha256": "<64 hex>", "chars": 47, "max_chars": 12000,
    "truncated": false, "severity_map": {}, "applies_to": ["helm/**"]},
   {"path": ".prxref/scoped/java.md", "sha256": "<64 hex>", "chars": 94, "max_chars": 12000,
    "truncated": false, "severity_map": {"nit": "outofscope"},
    "applies_to": ["**/*.java", "!**/src/test/**"]}],
 "max_chars": 24000,
 "units": {"chunks": [[{"path": ".prxref/scoped/java.md", "chars": 94}],
                      [{"path": ".prxref/scoped/helm.md", "chars": 47}]],
           "sweep": [{"path": ".prxref/scoped/helm.md", "chars": 47},
                     {"path": ".prxref/scoped/java.md", "chars": 94}]}}
```

- `entries` are the configured entries as given; `files` has one row per
  loaded file, in load order.
- Each `files` row is a `review_rules` record plus `applies_to`, which is
  `null` for a file that reaches every unit. Its `max_chars` is the per-file
  cap. The top-level `max_chars` is the per-unit cap.
- `units` lists the files each unit received, one list per chunk in chunk
  order, then the sweep's: a file the per-unit cap cut is listed, one it
  left out is not. A row's `chars` equals that file's `chars` in `files`,
  cut or not. `units` is `null` when the run ended before its chunks were
  planned: a forge, parse or chunking failure, or an empty diff.
- The record never carries the rules text.

| Where | What |
|---|---|
| `--format json` | the `scoped_rules` key, right after `prompt_templates`, always present, `null` when off |
| `-v` text output | `scoped rules: 2 file(s) .prxref/scoped/helm.md=<first 12 hex> .prxref/scoped/java.md=<first 12 hex> cap=24000`, after the `rules:` line when there is one |
| JSONL trace (`PRXREF_TRACE_FILE`) | one `scoped_rules ok` event whose meta is the record without `units`, right after `rules ok` (after `run start` with no always-on file); each `chunk start` and `sweep start` event carries its unit's rows as `rules` |
| `--trace-dir` | each unit's own block, in its `<unit>.system.md` |

## Cost

The always-on file rides **every** review unit: each chunk and the sweep.
So it adds about `chars / 4 × (chunks + 1)` input tokens per run. At the
12000-character default that is roughly 3000 tokens per unit. A scoped file
adds about `chars / 4` to each unit it reaches: the chunks whose paths it
matches, plus the sweep whenever any chunk does. So it adds
`chars / 4 × (matching chunks + 1)` tokens, and a file without
`applies_to` costs what the always-on file costs.
`PRXREF_SCOPED_RULES_MAX_CHARS` bounds the scoped share of any one unit:
24000 characters, roughly 6000 tokens, by default.

Scoping saves tokens only when the chunks split by path. prxref fills
chunks by size and by `PRXREF_CHUNK_MAX_FILES`, not by language, so a small
PR that mixes Java and Helm files often lands in one chunk, which then
carries both files' rules.

All the rules also count toward the prefill share of `PRXREF_LLM_TIMEOUT`,
and the timeout retry keeps them. Keep each file to rules a reviewer can
check from a diff.

## Errors

| Situation | Outcome |
|---|---|
| unset, `""`, whitespace, or `--rules-file ""` | no rules; `review_rules: null` |
| the path is a URL (`https://…`) | configuration error: rules must be a local file path |
| missing file, a directory, a FIFO or device, permission denied | configuration error: `cannot read rules file '<path>': <reason>` |
| a path inside the working directory that symlinks out of it | configuration error: `… resolves outside the working directory` |
| invalid UTF-8, or NUL bytes | configuration error |
| malformed `severity:` block | configuration error with `<path>:<line>` |
| `PRXREF_REVIEW_RULES_MAX_CHARS` 0, negative or not an integer | configuration error naming the variable |
| a `---` first line that never closes | warning; the whole file is rules text |
| body longer than the cap | warning; `truncated: true`; a truncation line in the block |
| empty body and no map | warning; the record is still present; no block |
| the model writes a mapped team word | rewritten to its tier before every quality pass |
| the model writes a word that is not mapped | unchanged behaviour: dropped as `invalid severity` |

Every configuration error names the input that supplied the path:
`--rules-file` when the flag was given, else `PRXREF_REVIEW_RULES`. The file
is read before any network call, so `prxref review` exits `2` without touching
the forge or the model. This holds under `PRXREF_FAIL_ON` as well.

Each [path-scoped rules file](#path-scoped-rules) meets the same errors and
warnings, named `--scoped-rules` when the flags were given, else
`PRXREF_SCOPED_RULES`, and read before any network call as well. It adds
the `applies_to` errors listed [above](#applies_to), more than 50 files,
an unreadable directory, and a team word mapped to a different tier than
another rules file maps it to.

## CI safety: read the rules from something the PR cannot change

In CI, the workspace is usually the pull request's own code: a GitHub
Actions `pull_request` workflow checks out the PR's merge commit, and a GitLab
merge-request pipeline runs on the MR's source branch. So
`--rules-file .prxref/rules.md` reads **the PR's copy** of the rules, and a PR
could rewrite its own review rules. Read them from somewhere the PR cannot
reach instead:

- **The target branch, with plain git.** Fetch the branch the PR merges into
  and copy the file out of it:

  ```bash
  git fetch --depth=1 origin "$TARGET_BRANCH"
  git show FETCH_HEAD:.prxref/rules.md > "$RUNNER_TEMP/prxref-rules.md"
  prxref review --pr-url "$PR_URL" --rules-file "$RUNNER_TEMP/prxref-rules.md"
  ```

  The target branch is `${{ github.base_ref }}` on GitHub Actions,
  `$CI_MERGE_REQUEST_TARGET_BRANCH_NAME` on GitLab CI and
  `$BITBUCKET_PR_DESTINATION_BRANCH` on Bitbucket Pipelines. `$RUNNER_TEMP` is
  GitHub's per-job temporary directory; elsewhere, use any directory outside
  the checkout, such as one from `mktemp -d`.
- **Outside the repository.** A GitLab CI/CD variable of type *File* named
  `PRXREF_REVIEW_RULES` works directly: the runner writes the value to a
  temporary file and puts that file's path in the variable. A file baked into
  the runner image, or one from your CI's secure-files store, works too.

An absolute path outside the working directory, like the two above, is read
as given. A path **inside** the working directory must still resolve inside
it once its symlinks are followed, so a PR that commits
`.prxref/rules.md -> /some/other/file` gets a configuration error rather than
a read of that file.

**The residual risk.** A PR that can edit the pipeline definition itself can
change anything the pipeline does, the rules included. Protect the pipeline
files with required review (for example `CODEOWNERS`), or run prxref as the
webhook daemon, whose configuration no PR can reach.

## The webhook daemon

`prxref serve` reads `PRXREF_REVIEW_RULES` and `PRXREF_SCOPED_RULES` from its
own environment and re-reads the files for every review, a scoped directory's
listing included. Editing a file therefore takes effect on the next webhook
with no restart, and the recorded `sha256` says which version reviewed which
PR. A webhook only ever delivers a PR URL, and the daemon has
no checkout of the PR, so nothing a PR contains can reach the rules loader. A
bad rules file fails each review with the configuration error in the daemon's
log.

If the daemon is started with a **relative** path, the path is resolved
against the daemon's working directory. Start it from a directory whose
contents no PR can change.

The loader keeps that guarantee by design. It refuses URLs, it never reads the
rules through the forge (for example, from the PR's head commit), and nothing
builds the path from PR data.
