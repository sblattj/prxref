# assistant

A tiny chat assistant engine: a user message runs a loop of steps (model calls and tool calls).
A run that needs a human hands off: its state is saved to the state store and resumed later.

Progress notes (`ASSISTANT_PROGRESS_NOTES`, default on): before each step the user sees a short
line such as "Running find order...". Notes are saved as `progress` messages and never sent
to the model.
