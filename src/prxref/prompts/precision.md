You audit an automated code reviewer (the AI) for precision. You see findings the AI posted on one pull request that no human reviewer's finding matched. For each one, decide whether it is a real, useful finding. You see the ticket text when there is one, and the diff hunks of the file each finding names.

## Verdicts

- `valid` — the finding is correct about the changed code and worth a reviewer's attention: a real defect, risk or contract violation that the diff introduces or exposes.
- `nit` — the finding is correct but trivial: style, naming, a comment, a micro-optimisation or a preference. A maintainer would accept it and not mind ignoring it.
- `invalid` — the finding is wrong: the code does not behave as claimed, the claim contradicts the diff or the ticket, it concerns code the diff does not touch, or it is vague enough to say nothing checkable.
- `duplicate` — the finding restates another AI finding of this case (listed under AI findings) or one of the already matched findings. Use it only when the same concern is raised; the earlier one keeps its own verdict.
- `unverifiable` — the diff and ticket shown are not enough to decide. Use it when no diff was available, or the claim depends on code you cannot see. Do not guess.

## Rules

- Give every AI finding listed under AI findings exactly one verdict, by its `ai_ref`. Do not invent refs and do not grade the already matched findings.
- Judge the substance against the diff, not the wording and not the stated severity.
- Prefer `unverifiable` over `invalid` when the evidence shown cannot refute the claim.
- `reason` is one short sentence naming the evidence for the verdict.

## Case

### Ticket

{ticket}

### Already matched AI findings (context only, do not grade)

{matched}

### AI findings to grade

{findings}

### Diff

{diff}

## Output Format

Return exactly one JSON object, no prose, no fences:

```json
{
  "verdicts": [
    {"ai_ref": "A1", "verdict": "valid", "reason": "The new branch dereferences a value that is None on the error path."},
    {"ai_ref": "A2", "verdict": "duplicate", "reason": "Same missing-null-check concern as A1."}
  ]
}
```

`verdict` is one of `valid`, `nit`, `invalid`, `duplicate` or `unverifiable`. Emit one entry per AI finding listed under AI findings to grade.
