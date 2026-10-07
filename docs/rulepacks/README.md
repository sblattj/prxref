# Rule packs

A rule pack is a curated, ready-to-use [team review rules](../review-rules.md)
file. Each pack is an ordinary rules file (Markdown, optional front matter),
so it needs no code and no new setting. Every rule carries a cited source, a
severity word that is already one of prxref's tiers (`error`, `warning`), and a
"Do not flag" guard that lists the false positives to skip.

Packs are documentation only: they are not shipped in the wheel. Copy the file
into your repository (for example `.prxref/api-evolution.md`) and review the
rules before you adopt them. Treat a pack as a starting checklist, not as a
measured gain in review precision.

| Pack | Covers |
| --- | --- |
| [`api-evolution.md`](api-evolution.md) | Public API and interface evolution: breaking signatures, removals and renames, silent default changes, surface growth, deprecation, wire formats, error types. |

## Use a pack

```bash
prxref review --pr-url https://github.com/acme/widget/pull/42 \
  --rules-file .prxref/api-evolution.md
# or, for every run of this process (the flag wins when both are set):
export PRXREF_REVIEW_RULES=.prxref/api-evolution.md
```

Or in the repository's `.prxref.toml` (relative to that file, kept inside the
repository):

```toml
review_rules = ".prxref/api-evolution.md"
```

Read the pack from a trusted checkout, never from the pull request under
review; see [CI safety](../review-rules.md#ci-safety-read-the-rules-from-something-the-pr-cannot-change).

## Combine a pack with your own rules

`--rules-file` (`PRXREF_REVIEW_RULES`) takes exactly one file. To add a pack
beside your team's file, load the pack as a path-scoped file with
`--scoped-rules`, which adds to the always-on file and never replaces it:

```bash
prxref review --pr-url https://github.com/acme/widget/pull/42 \
  --rules-file .prxref/team-rules.md \
  --scoped-rules .prxref/api-evolution.md
```

`--scoped-rules` is repeatable and also takes a directory of `*.md` files, so
several packs can sit in one directory. A pack has no `applies_to` key, so it
reaches every review unit; add `applies_to: ["src/**"]` to its front matter in
your copy to limit it to part of the repository. The pack has no `severity:`
map, so it never conflicts with your team's map. If you would rather keep one
file, append the pack's body to your team file.

Loading any rules input turns on the per-rule cap
(`PRXREF_MAX_FINDINGS_PER_RULE`), so findings for one rule are deduplicated.
Each file is capped by `PRXREF_REVIEW_RULES_MAX_CHARS` (default 24000); the
packs here stay well under it.

## Write a pack

- Keep each rule checkable from the diff alone. A rule that needs a test run,
  a ticket or a sign-off produces no finding.
- Give each section a source you can name (a specification, a book chapter, a
  documented policy). Paraphrase it; do not paste it.
- Write a "Do not flag" line for every rule: internal code, test-only code,
  additive changes, and projects that state they are pre-1.0.
- Avoid headings that name a language or artifact (`Java`, `Python`,
  `OpenAPI`): the rules loader reads those as a section scope.
