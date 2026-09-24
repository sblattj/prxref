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
  are appended after it.
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
`{spec_note}`, `{ticket_note}`, `{findings}` and `{attribution}`.

- Dropping `{attribution}` does not drop the attribution: the model
  attribution line is appended to a summary that lacks it, because every
  posted comment carries one.
- `PRXREF_POST_VERDICT=false` removes `{verdict}`, with a `:` or dash in front
  of it, from your template just as it does from the packaged one.
- The partial-review banner and the PR-size advisory are added around the
  filled template, not through a placeholder.

## What is checked

The directory is loaded once per run, before any network call. A problem
that would break the review is a configuration error: `prxref review` exits
`2` naming `--prompts-dir` or `PRXREF_PROMPTS_DIR`, whichever supplied the
path, and the webhook daemon logs it and reviews nothing. What each template
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
- a file other than the three templates;
- a directory that holds none of the three, which reviews with the packaged
  templates.

## What is recorded

The run record's `prompt_templates` names the directory and fingerprints each
overridden template. It never holds template text.

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
  the decoded text. A template left packaged has no entry.
- `--format json` always carries the key; it is `null` without a prompts
  directory. The JSONL trace (`PRXREF_TRACE_FILE`) gains one `prompts ok`
  event with the same fields.
- `-v` prints one line, the directory followed by the first 12 characters of
  each overridden template's SHA-256, in name order. For the record above it
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
