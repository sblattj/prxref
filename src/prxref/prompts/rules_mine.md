You write a team code-review rules file. Below are the findings human reviewers left on a set of past pull requests, grouped by pull request. From them, infer the standards this team enforces and write rules that an automated reviewer can apply to a pull request it has never seen.

## Requirements

- Write the rules file as Markdown: a short heading, then a flat bulleted list of imperative rules ("Flag ...", "Require ...", "Check that ..."). Group related rules under at most a few short headings.
- Put the most important rules first. Rank by how often the labels raise the concern and by severity; findings the author accepted (`accepted: yes`) count for more than ones that were declined (`accepted: no`).
- Generalise. State the underlying defect class or convention, never the specific pull request, file, line number or function the label came from. Do not write "PR 3", a path, a line number or a test name into a rule.
- A rule must be checkable from a diff. Drop labels that are praise, questions, style preferences with no stated reason, or too specific to generalise.
- Merge duplicates. Do not invent rules the labels do not support.
- Stay under {max_chars} characters in total. Shorter is better; a dozen strong rules beat fifty weak ones.

## Output Format

Reply with the rules file itself and nothing else: no preamble, no commentary, no code fence around it.

## Training labels

{labels}
