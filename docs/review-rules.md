# Team Review Rules

Most teams already keep a written review checklist: "every network call has a
timeout", "no migration without a rollback", "a TODO names its ticket". Point
prxref at that file and every review unit reads it as part of its
instructions.

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
Every chunk worker gets them, and so does the whole-PR systemic sweep, each
with its own framing:

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
  multi-line `description: |` included, works unmodified.
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

## Cost

The rules ride **every** review unit: each chunk and the sweep. So they add
about `chars / 4 × (chunks + 1)` input tokens per run. At the 12000-character
default that is roughly 3000 tokens per unit. They also count toward the
prefill share of `PRXREF_LLM_TIMEOUT`, and the timeout retry keeps them.
Keep the file to rules a reviewer can check from a diff.

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

`prxref serve` reads `PRXREF_REVIEW_RULES` from its own environment and
re-reads the file for every review. Editing the file therefore takes effect on
the next webhook with no restart, and the recorded `sha256` says which version
reviewed which PR. A webhook only ever delivers a PR URL, and the daemon has
no checkout of the PR, so nothing a PR contains can reach the rules loader. A
bad rules file fails each review with the configuration error in the daemon's
log.

If the daemon is started with a **relative** path, the path is resolved
against the daemon's working directory. Start it from a directory whose
contents no PR can change.

The loader keeps that guarantee by design. It refuses URLs, it never reads the
rules through the forge (for example, from the PR's head commit), and nothing
builds the path from PR data.
