# Golden offline eval dataset

Self-contained eval cases for **spec-grounded review**: the future reviewer is
given a Jira-style ticket (scope/intent) plus a docs/spec corpus and must
review a unified diff against that context — catching spec violations, not
just generic bugs. Every planted spec violation is detectable ONLY by reading
the case's `docs/` corpus; from the diff alone it looks like correct code.

`test_evals.py` here is a STRUCTURAL scorer only: it proves the dataset is
well-formed and self-consistent. It runs NO LLM. A later pass will run the
real pipeline per case and score its findings against `expected.json`.

## Case layout

```
tests/evals/case-NNN-<slug>/
├── ticket.md        Jira-style ticket: summary, description, acceptance criteria
├── docs/            1-3 short normative markdown docs the feature must conform to
├── diff.patch       git-style unified diff implementing the ticket
├── expected.json    machine-checkable expected findings (the scoring key)
└── meta.json        case metadata incl. planted-violation manifest
```

## `ticket.md`

Free-form markdown with a `## Summary`, a `## Description`, and a
`## Acceptance Criteria` section. The ticket may REFERENCE the spec corpus
("per docs/session-security.md") but must not restate the violated rule —
otherwise the violation stops being doc-detectable.

## `docs/`

1-3 short markdown files. Concrete and checkable: exact version strings,
required fields, naming rules, forbidden patterns. Normative keywords MUST /
MUST NOT / FORBIDDEN mark every rule a finding can be grounded on.

## `diff.patch`

One raw git-style unified diff, parseable by the production parser
(`prxref.triage.parse_unified_diff`). Each diff contains:

- at least one PLANTED spec violation (something `docs/` forbids or requires
  that the diff gets wrong), and
- at most one generic bug (a plain code defect visible without the docs).

## `expected.json`

A JSON array. Each entry is a MUST-FIND item — the floor a correct review
must meet, NOT an exhaustive list of every possible finding. Extra true
positives are allowed.

| field        | type   | rules |
|--------------|--------|-------|
| `id`         | string | unique within the case; referenced by `meta.json` |
| `file`       | string | path as it appears in the diff (post-`b/` form); must be a file the diff touches |
| `line_hint`  | int    | 1-based line in the NEW (post-image) file; must land on a line the diff ADDS (`FileDiff.added_lines`) |
| `severity`   | string | `error` \| `warning` \| `outofscope` \| `spec` |
| `must_match` | string | acceptance predicate for the finding body: plain substring, or regex when prefixed `re:` |
| `source`     | string | `spec` (grounded in the docs corpus; planted) or `generic` (plain bug) |

Severity vocabulary: `error` (clear defect), `warning` (probable issue),
`outofscope` (change outside the ticket's declared scope — reserved, unused
in the current cases), `spec` (spec-grounded finding; used by every planted
violation).

## `meta.json`

```json
{
  "id": "case-NNN-<slug>",
  "title": "one-line description of the failure mode",
  "source_prs": [],
  "planted_violations": [
    {
      "id": "V1",
      "description": "what the diff gets wrong and which doc rule it breaks",
      "expected_ref": "S1"
    }
  ],
  "notes": "context for future scorers"
}
```

`source_prs` is optional. Every `planted_violations[].expected_ref` must be
the `id` of exactly one `expected.json` entry with `source: "spec"`, and
every `spec`-sourced entry must be referenced by exactly one planted
violation — the structural scorer enforces this bidirectionally.

## Adding a case

1. Create `case-NNN-<slug>/` with all five artifacts above.
2. Pick ONE failure mode not yet covered (required-omission, forbidden
   pattern, naming/version pin, scope creep, ...).
3. Write the planted violation so the diff alone looks correct.
4. Compute `line_hint` against the diff's new-image line numbers.
5. Run `uv run pytest tests/evals/ -q` — the structural scorer must pass.
