# Prompt template overrides

prxref reviews with three packaged Markdown templates. `PRXREF_PROMPTS_DIR`,
or `--prompts-dir DIR` for one run, names a directory whose templates replace
them. Reach for it when team rules are not enough: a
[rules file](review-rules.md) adds policy to the prompts, but it cannot take
out a line of prxref's own, such as the instruction to skip style-guide nits.

```bash
prxref prompts export ~/acme-prompts
# edit the templates you want to change, and delete the others
prxref review --pr-url https://github.com/acme/widget/pull/42 --prompts-dir ~/acme-prompts
```

- `PRXREF_PROMPTS_DIR` names the directory for every run, the webhook daemon
  included. `--prompts-dir DIR` names it for one run and wins over the
  variable. `--prompts-dir ""` turns the variable off for one run.
- The directory can also go in the repository's `.prxref.toml`
  (`prompts_dir = ".prxref/prompts"`), relative to that file and kept inside
  the repository; see [docs/config-file.md](config-file.md). The CI safety
  section below applies to it as well.
- `prxref prompts export DIR [--force]` writes the packaged templates into
  `DIR` byte for byte. An unedited export loads without a warning and reviews
  exactly as the packaged templates do.
- A template missing from the directory keeps the packaged one. Delete the
  templates you leave unchanged, so they follow prxref's upgrades.

## The three templates

| Template | Used for |
|---|---|
| `worker.md` | the prompt of every chunk worker |
| `systemic.md` | the prompt of the whole-PR sweep |
| `summary.md` | the summary comment prxref posts on the PR |

Only these three file names are read. Any other file in the directory is
ignored with a warning; dotfiles such as `.gitkeep` are skipped silently.
Two texts cannot be overridden: the judge prompt of `prxref eval`, so runs
with different templates are always graded by the same judge (see
[docs/evals.md](evals.md#the-judge-prompt)), and the notice
prxref posts when a review cannot complete.

### `worker.md` and `systemic.md`

Each review template is split at its first `## Review Context` line:

- Everything above that line is the **system** prompt, prxref's instructions
  to the model. It is sent as written, and a placeholder in it is not
  filled. When configured, the team rules block and the ticket-scope request
  are appended after it. One section is switchable: with
  `PRXREF_ROUTING_PROBE=off`, the `## Matching rules` section of the worker
  system prompt (its heading and everything up to the next `## ` heading) is
  cut out, whether it comes from the packaged `worker.md` or from your
  override. With the default `on` it is sent as written, and an override
  without that section is unaffected either way.
- The line itself and everything below it is the **user** prompt, the review
  input. Its placeholders are filled in one pass, so a PR title or a diff that
  contains `{diff}` or any other placeholder is never filled a second time.

| Placeholder | Filled with | Template |
|---|---|---|
| `{pr_title}` | the PR title, or `(untitled)` | both |
| `{pr_description}` | the PR description, or `(none)` | both |
| `{repo_hint}` | `(unspecified)` in every current review | both |
| `{ticket_context}` | the fenced `### Ticket context` block followed by a blank line, or nothing without a ticket | both |
| `{spec_digest}` | the spec-constraint digest, or `(no specs provided for this review)` | both |
| `{diff}` | the chunk's diff | `worker.md` |
| `{context_blocks}` | the summary of the PR's other files and the file context read around the chunk, or nothing | `worker.md` |
| `{digest}` | the digest of the whole diff | `systemic.md` |
| `{scope_example}` | a `"scope": "in"` field for the example finding while a ticket is configured, else nothing | both |
| `{rule_example}` | a `"rule"` field for the example finding while finding grouping (`PRXREF_GROUP_FINDINGS`) is on, else nothing | both |

`{scope_example}` and `{rule_example}` are the two optional slots: an override
may leave them out. Every other placeholder the packaged template has below
`## Review Context` must stay below it in yours.

Two placements matter when you edit. `{ticket_context}` sits flush against
the `### Spec constraints` heading, because its value brings its own blank
line. `{scope_example}` and `{rule_example}` are glued to the last value of
the example finding under `## Output Format`, because each value starts with
the comma it needs. When the PR has threads, the sweep's user prompt also
gains an `### Existing discussion` block after the template; it is not a
placeholder.

The example finding's title is read back out of your template on every run.
A finding whose title, normalized, equals it is dropped as
`echoes the prompt's example: "<title>"`, because a model that copies the
example reports nothing it found. Only fenced blocks tagged `json` or left
untagged are read. So give the example a title that no real finding of yours
should carry. See [Example echoes](quality.md#example-echoes).

### `summary.md`

The whole file is filled: `{verdict}`, `{title}`, `{file_count}`,
`{error_count}`, `{warning_count}`, `{spec_count}`, `{outofscope_count}`,
`{spec_note}`, `{ticket_note}`, `{findings}` and `{attribution}`, plus five
marker slots.

The marker slots hold the finding glyphs, after any
[`PRXREF_SEVERITY_MARKERS`](env-vars.md#llm--pipeline) override (#59):

| Slot | Default | Glyph for |
|---|---|---|
| `{error_marker}` | 🟥 | `error` findings |
| `{warning_marker}` | 🟧 | `warning` findings |
| `{spec_marker}` | 🔍 | `spec` findings |
| `{outofscope_marker}` | ⬜ | `outofscope` findings, and any unrecognised severity |
| `{out_of_ticket_marker}` | 🟦 | the prefix of a finding outside the ticket |

The packaged counts line is `{error_marker} {error_count} error ·
{warning_marker} {warning_count} warning · {spec_marker} {spec_count} spec ·
{outofscope_marker} {outofscope_count} outofscope`. `{out_of_ticket_marker}`
is not in the packaged template, but it is filled and known, so using it
draws no unknown-placeholder warning. An override exported by 0.25.0 or earlier,
which spells the glyphs literally, still loads, but it keeps those literal glyphs
when the table is overridden; switch its counts line to the slots. The
findings list and its `Outside the ticket` heading are built from the same
table, so `{findings}` follows an override on its own.

- Dropping `{attribution}` does not drop the attribution: the model
  attribution line is appended to a summary that lacks it, because every
  posted comment carries one.
- `PRXREF_POST_VERDICT=false` removes `{verdict}`, with a `:` or dash in front
  of it, from your template just as it does from the packaged one.
- The partial-review banner and the PR-size advisory are added around the
  filled template, not through a placeholder.

#### Optional summary slots (#59)

These are filled on every render but are not in the packaged `summary.md`,
so the default comment is unchanged. An override may use any of them without
an unknown-placeholder warning.

| Slot | Value |
|---|---|
| `{error_findings}`, `{warning_findings}`, `{spec_findings}`, `{outofscope_findings}` | the bullets of that severity's findings that are not outside the ticket (scope `in` or unjudged), in `{findings}` order; empty when there are none. A finding whose severity is none of the four joins `outofscope`, whose glyph it already carries. |
| `{outside_ticket_findings}` | the bullets of every finding outside the ticket, whatever its severity; each already carries the `{out_of_ticket_marker}` prefix. Empty when there are none. |
| `{error_section}`, `{warning_section}`, `{spec_section}`, `{outofscope_section}` | `**<marker> <Label>**`, a blank line, the bullets and a newline, with the labels `Errors`, `Warnings`, `Spec` and `Minor` and the marker from the effective glyph table; empty when the group is. |
| `{outside_ticket_section}` | the `**🟦 Outside the ticket (N)**` block exactly as `{findings}` carries it, plus a newline; empty when there is no such finding. |
| `{inline_accounting}` | the `Inline comments: …` line explained below; empty on every render that has none. |
| `{head_sha}`, `{head_sha_short}` | the PR's head commit and its first 7 characters; both empty when the forge reported none. |
| `{chunk_count}` | review units run: those reviewed plus those that failed. |
| `{input_tokens}`, `{output_tokens}` | the run's prompt and completion tokens. |

Every bullet is `- <marker> `file:line`<separator><title>`, where the
separator is [`PRXREF_SUMMARY_BULLET_SEPARATOR`](env-vars.md#llm--pipeline)
(default ` — `), in `{findings}` and in the per-group slots alike.

**Inline accounting.** When the inline pass could not post a comment for
every finding, the summary is re-posted with one `Inline comments: N of M
findings (…)` line. A template with `{inline_accounting}` gets it there
alone. A template with `{findings}` and no `{inline_accounting}` gets it at
the end of `{findings}`, as before. A template with neither gets it appended
after the body.

**What a summary override must contain.** `{findings}`, or at least one of
the ten per-group slots (`{error_findings}` … `{outside_ticket_section}`).
A template with neither is refused (exit `2`):

```text
PRXREF_PROMPTS_DIR: prompt template 'prompts/summary.md' is missing the required {findings} placeholder; a summary template needs it or at least one per-group slot ({error_findings}, {error_section}, {outofscope_findings}, {outofscope_section}, {outside_ticket_findings}, {outside_ticket_section}, {spec_findings}, {spec_section}, {warning_findings}, {warning_section})
```

**No finding is dropped.** A template without `{findings}` must place each
of the five groups (error, warning, spec, outofscope, outside_ticket)
through its `_findings` or its `_section` slot. A group with neither draws a
warning when the template loads:

```text
PRXREF_PROMPTS_DIR: prompt template 'prompts/summary.md' has no {findings} and no slot for the spec finding group(s); those findings are appended under an 'Other findings' heading
```

and at render time any finding in such a group is added under
`**Other findings (N)**`, with a warning in the log. That block, followed by
the inline accounting when the template has neither `{findings}` nor
`{inline_accounting}`, goes just above the footer: the attribution, together
with a `---` rule directly over it if there is one. A template that dropped
`{attribution}` gets them at the end of the body, with the attribution
appended after them. The partial-review banner always comes last.

Substitution is still one pass: a finding title that contains
`{error_section}` or any other slot renders literally.

#### Which slots each summary renderer fills

prxref has two summary renderers. The CLI, the webhook daemon and
`orchestrate_review` render `summary.md` (packaged or overridden); the library
function `prxref.formatter.format_summary` renders its own table-shaped
template and reads no override.

| Slot | `summary.md` (CLI / `orchestrate_review`) | `formatter.format_summary` |
|---|---|---|
| `{verdict}` | yes | no |
| `{verdict_banner}` | no; use `{verdict}` | yes |
| `{title}`, `{file_count}`, `{ticket_note}` | yes | no |
| `{error_count}`, `{warning_count}`, `{spec_count}`, `{outofscope_count}` | yes | yes |
| `{spec_note}` | yes | yes, always empty |
| the five `{…_marker}` slots | yes | yes |
| `{findings}` | yes | no |
| `{findings_table}`, `{dropped_section}`, `{active_count}`, `{total_count}` | no | yes |
| per-group `{…_findings}` and `{…_section}` slots | yes | no |
| `{inline_accounting}`, `{head_sha}`, `{head_sha_short}` | yes | no |
| `{chunk_count}`, `{input_tokens}`, `{output_tokens}` | yes | yes |
| `{elapsed_s}`, `{model}` | no | yes |
| `{attribution}` | yes | yes |

#### Worked example: one section per severity

[`docs/examples/summary-by-severity.md`](examples/summary-by-severity.md)
lists findings by severity, with the head commit in the header. It is the
target template of issue #59 with two additions, `{spec_section}` (and the
spec count) and `{inline_accounting}`, so it covers every group and loads
without a warning:

```markdown
## prxref automated review

PR: {title} · files reviewed: {file_count} · head `{head_sha_short}`

{error_marker} {error_count} error · {warning_marker} {warning_count} warning · {spec_marker} {spec_count} spec · {outofscope_marker} {outofscope_count} minor

{error_section}
{warning_section}
{spec_section}
{outofscope_section}
{outside_ticket_section}
{inline_accounting}

---

{attribution}
```

With

```bash
PRXREF_PROMPTS_DIR=prompts   # prompts/summary.md is the file above
PRXREF_SEVERITY_MARKERS="error=🔴,warning=🟡,outofscope=⚪"
PRXREF_SUMMARY_BULLET_SEPARATOR=": "
```

a run with one finding of each severity and one outside the ticket posts
(rendered by prxref, not typed):

```markdown
## prxref automated review

PR: Add retry budget to the webhook client · files reviewed: 4 · head `3f9c2ab`

🔴 1 error · 🟡 3 warning · 🔍 1 spec · ⚪ 1 minor

**🔴 Errors**

- 🔴 `src/client.py:42`: Retry loop never gives up on 5xx

**🟡 Warnings**

- 🟡 `src/client.py:88`: Backoff ignores Retry-After
- 🟡 `src/config.py:—`: New key missing from .env.example

**🔍 Spec**

- 🔍 `src/client.py:57`: Budget must reset per delivery (T-12 §2)

**⚪ Minor**

- ⚪ `tests/test_client.py:12`: Test name says 3 retries, asserts 4

**🟦 Outside the ticket (1)**

- 🟦 🟡 `src/log.py:7`: Log line leaks the webhook URL



---

Reviewed by prxref · model=openai/gpt-5-mini · 20146 tok · 41.8s
```

The counts include findings outside the ticket, so `3 warning` counts the
one under `Outside the ticket`. An empty section leaves its blank line
behind, which Markdown collapses.

The issue's template as written has no `{spec_section}`. It still loads,
with the spec-group warning shown above, and the same run adds the spec
finding just above the footer:

```markdown
**🟦 Outside the ticket (1)**

- 🟦 🟡 `src/log.py:7`: Log line leaks the webhook URL

**Other findings (1)**

- 🔍 `src/client.py:57`: Budget must reset per delivery (T-12 §2)

---

Reviewed by prxref · model=openai/gpt-5-mini · 20146 tok · 41.8s
```

## What is checked

The directory is loaded once per run, before any network call. A problem
that would break the review is a configuration error: `prxref review` exits
`2` naming `--prompts-dir`, `PRXREF_PROMPTS_DIR` or the config file's
`prompts_dir` key (`.prxref.toml: prompts_dir`), whichever supplied the
path; `prxref config check` fails the same way; and the webhook daemon logs it and reviews nothing. What each template
must keep, and the size limit, are stated once, under `PRXREF_PROMPTS_DIR` in
[env-vars.md](env-vars.md).

Checking up front is what keeps a broken template visible. A review template
without its `## Review Context` line would otherwise fail inside every chunk,
and the run would end as an `Error` review that still exits `0`.

Beyond those rules, the directory and its templates are read the way a rules
file is: a URL, a template that is not a regular file, one that is not UTF-8
or contains NUL bytes, and a symlink that leads out of the directory are all
refused.

These only warn, and the run goes on:

- a placeholder that template does not fill, usually a typo such as
  `{pr_titel}`, which reaches the model literally;
- a known placeholder above `## Review Context`, which reaches the system
  prompt literally;
- a second `## Review Context` line, of which only the first splits the
  prompt;
- a `summary.md` without `{findings}` that gives some finding group no slot,
  whose findings then land under `Other findings`;
- a file other than the three templates;
- a directory that holds none of the three, which reviews with the packaged
  templates.

Nothing checks the reply format a worker or systemic template asks for. The
only content rule beyond the marker and the placeholders is that
`summary.md` keeps `{findings}` or a per-group slot; no check reads the `"findings"` key of the
example reply under `## Output Format`. That key matters since 0.17.0: with
`PRXREF_LLM_PARSE_RETRIES` at `1` or more (the default is `1`), a reply
that is a JSON object without a `findings` list is sent again, and when the
retries run out the unit fails with `worker review JSON has no findings
list`. So a template whose reply format drops or renames `findings` fails
every chunk and the sweep, each after its retries, and the review ends as an
`Error` review, which exits `0` under the default `PRXREF_FAIL_ON=never`.
At `0`, such a reply counts as a review
with no findings, as it did in 0.16.0, so the template silently reports
nothing. Keep the `findings` list in the reply format. See
[Parse Retries](llm.md#parse-retries).

## What is recorded

The run record's `prompt_templates` names the directory and fingerprints each
template file in it. It never holds template text.

```json
"prompt_templates": {
  "dir": "/etc/prxref/prompts",
  "templates": {
    "worker": {
      "path": "/etc/prxref/prompts/worker.md",
      "sha256": "4f6a0c1e9b7d25a3c8e0f41b6d92a7e5c3b1f08d6e4a29c7b5d3f1e0a8c6b4d2",
      "chars": 9731
    }
  }
}
```

- `dir` and `path` are as configured. `sha256` is taken over the file's raw
  bytes, so it equals `shasum -a 256 worker.md`, and `chars` is the length of
  the decoded text. Every template file present in the directory gets an
  entry, edited or not, so an unchanged `prxref prompts export` copy is
  stamped too; a template absent from the directory has no entry.
- `--format json` always carries the key; it is `null` without a prompts
  directory. The JSONL trace (`PRXREF_TRACE_FILE`) gains one `prompts ok`
  event with the same fields.
- `-v` prints one line, the directory followed by the first 12 characters of
  each stamped template's SHA-256, in name order. For the record above it
  is `prompts: /etc/prxref/prompts worker=4f6a0c1e9b7d`; with `summary.md`
  overridden too, `summary=` comes before `worker=`.
- `--trace-dir` writes the prompts each review unit actually sent
  (`chunk0.system.md`, `sweep.user.md`, and so on), so you can read what an
  edit changed.

## CI safety: read the templates from something the PR cannot change

In CI the workspace is usually the pull request's own code, so
`--prompts-dir .prxref/prompts` reads **the PR's copy** of the templates, and
a PR could rewrite its own review: delete the checks that would flag it, or
tell the model to approve. The stakes are higher than for a rules file,
because a template replaces prxref's instructions instead of adding to them.
Read the directory from somewhere the PR cannot reach:

- **The target branch, with plain git.** Fetch the branch the PR merges into
  and extract the directory from it:

  ```bash
  git fetch --depth=1 origin "$TARGET_BRANCH"
  git archive FETCH_HEAD .prxref/prompts | tar -x -f - -C "$RUNNER_TEMP"
  prxref review --pr-url "$PR_URL" --prompts-dir "$RUNNER_TEMP/.prxref/prompts"
  ```

  Where each CI names the target branch and a temporary directory:
  [review-rules.md](review-rules.md#ci-safety-read-the-rules-from-something-the-pr-cannot-change).
- **Outside the repository.** A directory baked into the runner image, or
  one restored from your CI's secure-files store, with `PRXREF_PROMPTS_DIR`
  pointing at it.

An absolute path outside the working directory is read as given. A directory
inside the working directory must resolve inside it once its symlinks are
followed, and every template must resolve inside the directory, so a PR that
commits `worker.md -> ../notes.md` gets a configuration error rather than a
read of that file.

**The residual risk.** A PR that can edit the pipeline definition can change
anything the pipeline does, the prompts directory included. Protect the
pipeline files with required review (for example `CODEOWNERS`), or run prxref
as the webhook daemon, whose configuration no PR can reach.

## The webhook daemon

`prxref serve` reads `PRXREF_PROMPTS_DIR` from its own environment and loads
the directory again for every review. An edit therefore takes effect on the
next webhook with no restart, and the recorded `sha256` says which version
reviewed which PR. A bad directory fails each review with the configuration
error in the daemon's log. A relative path is resolved against the daemon's
working directory, so start it from a directory no PR can change.

## Upgrading prxref

What a review-template override must keep is derived from the packaged
templates of the prxref that runs it, not from a list kept by hand. When a
release adds a required placeholder to `worker.md` or `systemic.md`, an
override exported from an older release lacks it, and the run exits `2`
naming the missing placeholder rather than silently leaving that input out
of the prompt. A slot added to `summary.md`, or an optional slot, is not
required: the override loads and simply does not use it. Either way, export
the new templates and carry your edits across:

```bash
prxref prompts export /tmp/prxref-prompts-new
diff -u /tmp/prxref-prompts-new/worker.md ~/acme-prompts/worker.md
```

Keeping an unedited export of the release you started from makes the merge
easier: comparing it with the new export shows exactly what prxref changed.
