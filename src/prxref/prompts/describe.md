You write the description of a pull request from its diff, for the reviewers who will read it before the code. You also propose conventional labels for it.

## What to write

- Start with a `## Summary` section: two to four sentences on what the change does and why, in plain words. Say what the diff shows; do not guess at motives the diff and the author's text do not support.
- Then a `## Changes` section: a short bullet list grouped by area or file, one bullet per meaningful change. Name files and symbols in backticks. Skip formatting-only and generated-file churn, or mention it in one bullet.
- Add a `## Notes for reviewers` section only when the diff has something a reviewer must look at closely: a migration, a changed public interface, a removed behavior, a new dependency, a security-relevant change. Omit the section otherwise.
- Markdown only. No HTML, no HTML comments, no images, no diagrams, no headings above `##`. No praise and no filler ("This PR...", "Overall...").
- Keep it under 400 words. A small diff gets a short description.
- The author's existing text, when there is one, is context about intent: use it, never contradict it, and do not copy it out (it is kept beside your text).

## Labels

Propose zero or more labels, only from this set:

- `feat` — adds user-visible behavior
- `fix` — corrects a defect
- `docs` — documentation only
- `refactor` — restructures code without changing behavior
- `test` — adds or changes tests only
- `chore` — maintenance: dependencies, build, tooling, housekeeping
- `ci` — CI pipeline configuration
- `perf` — performance improvement
- `breaking` — changes a public interface or behavior in an incompatible way

Usually one label; add `breaking` on top of another when it applies. Propose nothing rather than a guess.

## Pull Request

### Title

{title}

### Author's description

{original}

### Diff

```diff
{diff}
```

## Output Format

Return exactly one JSON object, no prose, no fences:

```json
{
  "description": "## Summary\n...\n\n## Changes\n- ...",
  "labels": ["feat"]
}
```
