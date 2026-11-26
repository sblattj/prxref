# Release notes (#45)

prxref already writes a summary comment on every pull request it reviews.
`prxref release-notes` turns those summaries into release notes: one bullet
per pull request merged since a tag, grouped by conventional-commit category.
The webhook daemon can also keep an `Unreleased` draft current as pull
requests merge.

Both are deterministic. No model is called, and nothing is posted to the
forge. The output is plain markdown with no prxref markers, so you edit it
like any other file before you commit it.

## The command

```bash
prxref release-notes --since v1.4.0 [--until HEAD] [--repo .] \
  --repo-url https://github.com/OWNER/REPO [--out NOTES.md]
```

| Flag | Meaning |
|---|---|
| `--since TAG` | Required. The tag or revision the notes start after. |
| `--until REV` | The revision the notes end at. Default `HEAD`. |
| `--repo DIR` | The local git checkout to read commits from. Default `.`. |
| `--pr-url-base URL` | The pull request URL up to its number, such as `https://git.example.com/OWNER/REPO/pulls/`. A missing trailing `/` is added. |
| `--repo-url URL` | The repository's web URL. prxref derives the pull request URL from it (below). Give this or `--pr-url-base`, not both. |
| `--out FILE` | Write the markdown to `FILE` instead of stdout. An existing file is replaced. |

It runs `git log SINCE..UNTIL` in `--repo` (a list of arguments, never a
shell, with a 60 second timeout per call), finds the pull request each
commit merged, reads prxref's summary comment on each one through the forge,
and prints the notes. The forge token is the same one `prxref review` uses
(`PRXREF_GITHUB_TOKEN` and so on, see [forges.md](forges.md)).

`--repo-url` is mapped by its shape:

| Repository URL | Pull request URL |
|---|---|
| `https://github.com/O/R` | `.../pull/N` |
| `https://gitlab.com/G/R` (subgroups too) | `.../-/merge_requests/N` |
| `https://bitbucket.org/W/R` | `.../pull-requests/N` |
| `https://codeberg.org/O/R` | `.../pulls/N` |
| `.../projects/KEY/repos/R` or `.../users/KEY/repos/R` (Bitbucket Server) | `.../pull-requests/N` |
| `.../_git/R` (Azure DevOps) | `.../pullrequest/N` |

A self-hosted GitHub, GitLab or Gitea repository URL looks the same for all
three, so prxref does not guess: it exits `2` and asks for `--pr-url-base`.

### Exit codes

- `2`, a configuration error, naming the flag: `--repo` is not a git
  checkout (or git is missing), `--since` or `--until` names no commit (a
  value starting with `-` is refused before git sees it), a URL flag prxref
  cannot use, or an `--out` it cannot write.
- `0` for everything else. A summary that cannot be read does not fail the
  run: that pull request becomes a fallback bullet, and the reason goes to
  stderr.

stderr ends with one count line:

```text
release-notes: 4 pull requests, 2 prxref summaries, 2 fallbacks, 1 commits naming no pull request
```

## How a pull request number is found

Only these commit shapes count. A bare `#N` anywhere else, such as
`Fixes #12` in a body, is never read as a pull request.

| Forge | Commit shape | Title used for the category |
|---|---|---|
| GitHub, Gitea/Forgejo (squash) | subject ends in `(#N)` | the subject |
| GitHub (merge commit), Bitbucket Server | subject starts `Merge pull request #N` | the first body line |
| Gitea/Forgejo (merge commit) | `Merge pull request 'TITLE' (#N) from ...` | `TITLE` |
| GitLab | a body line `See merge request GROUP/REPO!N` | the first body line |
| Bitbucket Cloud | subject starts `Merged in BRANCH (pull request #N)` | the first body line |
| Azure DevOps | subject starts `Merged PR N: TITLE` | `TITLE` |

When several commits name the same number, one bullet is written. A merge
commit wins, because it is the forge's own record of the merge. Otherwise
the oldest commit with a known conventional type wins (so a later
`release: ... (#N)` commit does not replace a `feat: ... (#N)` one), else the
oldest commit. A commit with none of these shapes is left out and counted.

A squash subject's `(#N)` is only a pull request where the forge writes it
that way. In a repository whose commits cite issue numbers as `(#N)`, the
link points at that number's pull request URL and the forge has no prxref
summary there, so the bullet falls back to the subject.

## Categories

The category comes from the title's conventional-commit prefix,
`type(scope)!: text`. The type is matched without regard to case, the
`(scope)` is dropped, and so is a trailing `(#N)`. Groups print in this
fixed order, and an empty group is left out:

1. `Breaking changes`: any type with `!` after it, or a `BREAKING CHANGE:`
   (or `BREAKING-CHANGE:`) footer in the commit body
2. `Features`: `feat`
3. `Fixes`: `fix`
4. `Performance`: `perf`
5. `Reverts`: `revert`
6. `Refactoring`: `refactor`
7. `Documentation`: `docs`
8. `Tests`: `test`
9. `Build`: `build`
10. `CI`: `ci`
11. `Chores`: `chore`
12. `Other`: anything else, with the whole title kept

## The output

```markdown
## Features

- add widgets ([#10](https://github.com/acme/widgets/pull/10))
  prxref review: Approved: 🟥 0 error · 🟧 0 warning · 🔍 0 spec · ⬜ 0 outofscope

## Fixes

- null owner ([#11](https://github.com/acme/widgets/pull/11))
```

The indented line is the summary excerpt. It is built from prxref's summary
comment like this: HTML comments (the summary and reviewed-head markers) are
removed, and so is the footer, everything from the last `---` rule, plus any
paragraph with the `Reviewed by prxref` attribution. The verdict comes from
the `## prxref automated review: VERDICT` heading, or from the bold
`prxref review` banner of a review that failed (so its excerpt reads
`Error: The review could not complete: ...`). Headings, blockquotes
(the size and partial-review banners) and the `PR: ... · files reviewed: N`
line are skipped, and the first paragraph left is the excerpt. On the
packaged summary template that is the finding counts, because the first two
paragraphs are the heading and the pull request's own title. A custom
`summary.md` gets its first plain paragraph. The excerpt is one line, cut at
240 characters.

### Fallbacks

A pull request keeps its title alone, with no excerpt, when the forge has no
summary reader, when the read fails (an HTTP error, a missing token), when
there is no prxref summary on it, or when nothing is left of the summary.
Each one is listed on stderr with its reason:

```text
release-notes: #11: fallback to the commit subject (no prxref summary)
release-notes: #13: fallback to the commit subject (summary read failed: RuntimeError: HTTP 502)
```

## Edit, then commit

The notes are a starting point. Write them to a file, edit the wording, and
commit them with the release:

```bash
prxref release-notes --since v1.4.0 --repo-url https://github.com/acme/widgets --out notes-1.5.0.md
$EDITOR notes-1.5.0.md        # reword bullets, drop the noise, add context
cat notes-1.5.0.md >> RELEASE_NOTES.md
git add RELEASE_NOTES.md && git commit -m "docs: release notes for 1.5.0"
```

The file is plain markdown. prxref never reads it back, so any edit is safe.

## The post-merge draft (`PRXREF_RELEASE_NOTES_FILE`)

Off by default. Set `PRXREF_RELEASE_NOTES_FILE` to a path, and `prxref
serve` keeps a draft there as pull requests merge. It is environment-only
because it writes a file; a `.prxref.toml` cannot set it.

- A GitHub `pull_request` delivery with action `closed` and
  `pull_request.merged` true is queued on the same worker as reviews. A
  close without a merge is ignored, as is every closed delivery while the
  variable is unset (the response reason stays the one it was before).
- The worker reads prxref's summary comment on that pull request and
  appends its bullet, built from the webhook's pull request title, under the
  category heading. A missing heading is inserted in the fixed order above.
  A summary that cannot be read leaves the title alone.
- A pull request whose `[#N](URL)` link is already in the file is skipped,
  so a redelivered webhook writes nothing twice. The match is on the whole
  link, so several repositories can share one draft without the same number
  colliding.
- A write that fails (a directory in the way, no permission) is logged at
  ERROR and the daemon keeps running.
- It never posts to the forge.

A new draft starts like this:

```markdown
# Unreleased

## Fixes

- paging stops at the last page ([#21](https://github.com/acme/widgets/pull/21))
  prxref review: Approved: 🟥 0 error · 🟧 0 warning · 🔍 0 spec · ⬜ 0 outofscope
```

Edit it as you go. Text you add (an intro paragraph, reworded bullets) is
kept, because prxref only inserts lines. At release time, rename the title to
the version, move the section into your changelog, commit it, and empty the
file for the next release.

Only GitHub deliveries feed the draft today. For the other forges, run the
command at release time instead.
