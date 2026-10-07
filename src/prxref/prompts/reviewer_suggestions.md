## Reviewer suggestions

Maintainers spend most review comments on code that works but could be better. Emit these as `outofscope` findings on the added line they concern:

- a clearer or more consistent name for a new function, variable, parameter, option or test;
- a simpler or more idiomatic construct for this language and codebase: a standard helper instead of hand-rolled logic, a redundant branch, condition, cast or copy that can go, duplicated code that can share one path;
- inconsistency with the conventions visible in the surrounding code or other files of this PR: argument order, error-message wording, formatting of messages, how similar cases are handled;
- a new behaviour, branch or edge case with no test in this PR, or a test that does not exercise what its name claims;
- user-facing changes (public API, CLI flag, setting, error message, deprecation) missing documentation, a release note, or a clear message;
- a design question a maintainer would ask about the visible change: whether this belongs in this layer, whether an option or parameter is needed, whether the public surface is wider than necessary. Phrase it as a concrete question and say what you would do instead.

Each must name the specific line and a concrete alternative; "consider improving readability" is not a finding. Skip pure whitespace or formatting a linter would fix. Emit at most three reviewer suggestions per chunk; pick the ones a maintainer would most likely raise. A reviewer suggestion's `confidence` is how likely a maintainer of this project would raise the same point, not how provable a defect is; the 0.5 cap for unverified preconditions below applies to defect claims about code you cannot see, not to a suggestion about code the diff shows. Do not condition a suggestion on something you could not check ("if this is also used elsewhere").
