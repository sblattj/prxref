# Contributing to prxref

Thanks for taking the time. Bug reports, forge quirks, and prompt improvements
are all welcome.

## Development Setup

prxref uses [uv](https://docs.astral.sh/uv/) for environments and locking.

```bash
git clone https://github.com/sblattj/prxref
cd prxref
uv sync --extra dev
```

## The Checks

Both must pass before a PR can merge. CI runs exactly these on Python 3.12 and
3.13:

```bash
uv run pytest              # no network required
uv run ruff check src tests
```

The suite is fully offline — every forge and LLM call is stubbed. If a change
you make needs the network to test, that is a design smell worth raising in the
issue first. `pytest --collect-only -q | tail -1` prints the current test count
if you want one; this file deliberately does not, because a hard number here
goes stale on the next PR and nobody notices.

Note that `pyproject.toml` sets no `addopts`. That is deliberate: pytest appends
your flags to whatever lives there, so a `-q` in the config turned every
`pytest -q` into `-qq` and silently swallowed the pass/fail summary line.

To check that packaging still works:

```bash
uv build                   # must produce BOTH a .tar.gz and a .whl
```

## Architecture

One unified diff in, inline comments out:

```
PR URL -> detect_forge -> Forge adapter -> unified diff -> risk-ranked chunks
       -> parallel LLM workers (fallback chain) -> quality gate -> post
```

- `forges/base.py` defines a single `Forge` Protocol. `bitbucket.py`,
  `github.py`, and `gitlab.py` implement it. **Adding a forge means implementing
  that Protocol and nothing else** — the pipeline above is forge-agnostic.
- `llm_backends.py` holds the interchangeable backends. Core code talks to the
  protocol in `llm.py`, never to a vendor SDK.
- `quality.py` is deterministic. Findings are dropped by rule, not by asking a
  model to double-check.

## Conventions

These are load-bearing, not style preferences:

- **stdlib + `requests` only in core.** Every LLM backend beyond the plain-HTTP
  one is an optional extra. A core import of a heavy dependency is a bug.
- **No provider credentials.** prxref reads no OpenAI/AWS/Google/Anthropic key.
  It calls one OpenAI-compatible endpoint the user configures. Keep it that way.
- **Single-shot LLM calls only.** All context is gathered before the call. No
  agent loops, no tool use, no multi-turn negotiation with the model.
- **Non-blocking by design.** `review` exits 0 even when the review fails.
  prxref advises; it does not gate merges. Do not add a failure exit code.
- **Every posted comment carries model attribution.**
- Docstrings on public API. No inline commentary restating the code.

## Prompt Changes

`src/prxref/prompts/*.md` are packaged data files loaded via
`importlib.resources`. If you add one, confirm it survives packaging:

```bash
uv build && python -c "
import zipfile, glob
print([n for n in zipfile.ZipFile(glob.glob('dist/*.whl')[0]).namelist() if 'prompts' in n])"
```

Prompt edits change review output in ways tests cannot fully capture. Say in the
PR what you ran it against and what changed in the findings.

## Pull Requests

- Branch off `main`, one logical change per PR.
- Say what breaks without the change, not just what the change does.
- New behavior needs a test. Bug fixes need a test that fails before the fix.
- prxref reviews its own PRs — it is an advisor, so feel free to disagree with it
  in a comment.

## Reporting Bugs

Include the forge, the URL shape that failed (redacted is fine), the backend and
model chain, and the output of `prxref review --pr-url ... -v`. Forge API quirks
are the most common cause, and the verbose output usually shows which call
returned what.

For security issues, do **not** open an issue — see [SECURITY.md](SECURITY.md).

## License

Contributions are licensed under the MIT License, matching the project.
