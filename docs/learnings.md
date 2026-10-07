# Team learnings (#33)

A reviewer who answers a prxref finding with "won't fix, this is on purpose"
has settled a question that the next review of a different PR will ask again.
A **learnings file** records that answer once per repository, so later reviews
drop the same finding without asking.

The feature is off unless `PRXREF_LEARNINGS_FILE` (or `learnings_file` in
`.prxref.toml`) names a file. While it is off, prompts, findings and outputs
are byte-identical to a run without the feature. The only difference is the
run record's `learnings` key, which is `null`.

## The file

The file is TOML and holds only `[[learning]]` tables:

```toml
[[learning]]
id = "legacy-raw-sql"                 # required, unique
paths = ["src/legacy/**", "!src/legacy/new/**"]   # required globs
claim = "Raw SQL string built by concatenation"   # required
rule = "SEC-3"                        # optional; omitted = any rule
reason = "Inputs are compile-time constants in this module"
source = "https://github.com/o/r/pull/12#discussion_r1"
added = 2026-10-07                    # optional date
expires = 2027-04-01                  # optional date
```

- `paths` uses the same glob syntax as a scoped rules file's `applies_to`:
  case-sensitive `fnmatch` over the whole path (`*` crosses `/`), with a
  leading `!` for a negation that vetoes the path. A single string counts as a one-item
  list. A list made up only of negations is refused, because it would select
  no path.
- `claim` must contain at least one content word of four or more letters.
- `added` and `expires` are TOML dates (`YYYY-MM-DD`). A quoted ISO date is
  also accepted, but a date-time is refused.

prxref refuses the whole run with exit 2 when the file:

- is missing, unreadable, not UTF-8, or longer than 262,144 characters;
- is invalid TOML;
- has an unknown key, or an entry missing a required key;
- repeats an id;
- has a bad date.

The error names `PRXREF_LEARNINGS_FILE` (or the `.prxref.toml` key) and the
problem. `prxref config check` runs the same validation.

## Matching

The pass runs right after stable ids are assigned
([quality pass 9](quality.md)). An active finding is dropped with
`drop_reason` `suppressed by learning: <id>` by the first entry, in file
order, that passes all three checks:

1. **Path:** its `paths` globs select the finding's file. A leading `./` is
   ignored.
2. **Rule:** its `rule` equals the finding's rule. The comparison ignores case
   and surrounding whitespace. An entry with no `rule` matches any finding. An
   entry with a rule never matches a finding that has none.
3. **Claim:** the finding's title and body share content tokens with the
   `claim`. The pass uses the settled-thread gate's tokenizer and its threshold
   of 4 shared tokens. For a claim with fewer than 4 tokens, every one of its
   tokens must be shared.

The pass only ever drops findings. It never adds a finding, never brings a
dropped finding back, and never changes an earlier drop reason.

## Expiry and staleness

- An entry whose `expires` date is before today (UTC) suppresses nothing. It
  is counted in the record's `expired` field and logged at INFO.
- An entry whose `added` date is more than 180 days ago, and which has not
  expired, is named in a WARNING log line so the team can revisit it. It still
  applies.

## The run record

With a file loaded, the run record and `--format json` carry:

```json
"learnings": {"file": "...", "sha256": "...", "loaded": 2, "expired": 0,
              "suppressed": [{"learning_id": "legacy-raw-sql",
                              "finding_id": "src/legacy/db.py#norule#...",
                              "title": "Raw SQL string built by concatenation"}]}
```

`suppressed` is empty on a summary-only or error run, because such a run ends
before the pass. `learnings` is `null` when no file is configured.

## Harvesting candidates

```sh
prxref learnings harvest --pr-url https://github.com/o/r/pull/12 [--out FILE]
```

This command reads the PR's existing threads using the same forge call the
review uses to deduplicate against threads. It prints a candidate
`[[learning]]` entry for each thread that meets both conditions:

- The thread is rooted in a prxref inline comment, recognised by the comment's
  `🤖 … **[SEVERITY] title**` header.
- A human closed the thread as **won't fix**. That means an explicit "won't
  fix" reply, or Azure DevOps' `wontFix` / `byDesign` status. A prxref comment
  that itself says "won't fix" never counts, because bodies carrying the
  `Reviewed by prxref` marker are excluded.

Each candidate covers the finding's own path exactly and uses its title as the
claim. Its `reason` is a placeholder for the team to replace. A candidate never
names a `rule`, because a posted comment does not carry the finding's rule.

**prxref never writes the learnings file into the repository.** The output
goes to stdout, or to `--out` when given. A human reviews the candidates,
widens `paths` or adds a `rule` if the team agrees, and commits the ones worth
keeping through an ordinary pull request.

Exit codes:

- 2 for an unrecognised `--pr-url` or an unwritable `--out`.
- 1 when the forge cannot be read.
- 0 otherwise, including when no thread qualifies. The output is then a
  comment-only, valid file.

## Trust caveat

Like the review rules file and the verdict store, the learnings file is read
from the **PR checkout**, that is, from the working directory the review runs
in. The path is confined to that directory. This means a PR can edit the
learnings file and suppress findings on its own changes. A team that reviews
untrusted PRs should:

- treat changes to the learnings file like changes to the CI config, by
  requiring code-owner review;
- or point `PRXREF_LEARNINGS_FILE` at a copy taken from the base branch
  before the review runs.

The run record's `sha256` shows which version of the file a run used. A later
release may read the file from the base ref directly.
