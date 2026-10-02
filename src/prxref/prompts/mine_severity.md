You draft the severity of human code review comments left on one pull request. A person confirms your draft afterwards. You do not see the code or the diff: judge each comment from its own words and its file and line.

## Severities

- `error` — a defect the reviewer wanted fixed before merge: a bug, a crash, data loss, a security hole, a broken contract or a missing check that matters.
- `warning` — a real problem worth fixing that would not block the merge on its own: a risky edge case, weak error handling, a missing test, a maintainability trap.
- `minor` — a nit: naming, style, formatting, wording, a preference, a suggestion the author may decline.
- `spec` — the change does not do what the ticket, the requirement or the documented behaviour asks.
- `outofscope` — not about the change itself: a question, praise, a thank-you, a remark on unrelated code, or a follow-up for later.

## Rules

- Give every comment exactly one severity, by its `id`. Do not invent ids and do not skip any.
- Judge what the reviewer asked for, not the tone: a polite request to fix a crash is `error`.
- When a comment mixes a nit with a real defect, take the defect's severity.
- When the comment is too vague to tell, answer `warning`.

## Case

### Comments

{labels}

## Output Format

Return exactly one JSON object, no prose, no fences:

```json
{
  "severities": [
    {"id": "c101", "severity": "error"},
    {"id": "c102", "severity": "minor"}
  ]
}
```

`severity` is one of `error`, `warning`, `minor`, `spec` or `outofscope`. Emit one entry per comment listed above.
