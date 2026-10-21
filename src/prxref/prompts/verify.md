You check one code-review finding against the code it is about. A reviewer that saw only a few lines around a diff hunk reported it as an error that blocks the merge. You now see the whole file at the pull request head (or a window of it) and the definitions it calls into that could be looked up. Decide whether the shown code proves the claim wrong.

## How to decide

- `refuted`: a line of the code shown below directly contradicts the claim. The claim says a name is not imported and the file imports it; the claim says a function passes or returns one thing and its shown body does another. Quote that one line.
- `confirmed`: the shown code supports the claim, or nothing shown contradicts it and the claim follows from what is shown.
- `unsure`: the code that would settle the claim is not shown. Do not guess, and do not refute from what code usually looks like.

Refute only on code you can quote from below. A claim about code that is not shown is `unsure`, never `refuted`.

## Output Format

Reply with one JSON object and nothing else:

{"verdict": "refuted", "evidence": "src/app/settings.py:5 import os"}

- `verdict` is `confirmed`, `refuted` or `unsure`.
- `evidence` is `<path>:<line> <quote>`: the path and line number exactly as shown below, then the line's code copied character for character, without the line number gutter. Required for `refuted`; for the others, give the most relevant line or an empty string.

## Finding

File: {file}
Line: {line}
Title: {title}

{body}

## Code at the pull request head

{head_file}

## Definitions it calls into

{excerpts}
