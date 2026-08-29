# HANDOFF — v0.5.0 shipped: the Bitbucket Server / Data Center forge

**Repo:** `sblattj/prxref` (public) · **Released:** 2026-08-28 · **Supersedes** the
"cut v0.5.0" handoff written the same day.

The Bitbucket Server / Data Center forge is released. It had been finished and
proven in real use but lived only on `origin/feat/bitbucket-server-forge`
(`cc58915`), so every deployment that needed it ran a hand-maintained overlay of
`bitbucket_server.py` on top of a tagged release. **Delete that overlay** — v0.5.0
carries the forge natively.

## What landed

- `src/prxref/forges/bitbucket_server.py` — project and personal (`~slug`)
  repositories, deployment context paths, anchored inline comments, `start`/`limit`
  paging, and the `version` field Data Center requires when updating a comment.
- Registration in both places that matter: the `detect_forge` module tuple in
  `forges/base.py` and the `impls` dict in `config.py`'s `make_forge`.
- Three env vars: `PRXREF_BITBUCKET_SERVER_TOKEN` (falls back to
  `PRXREF_BITBUCKET_TOKEN`), `PRXREF_BITBUCKET_SERVER_USER` and
  `PRXREF_BITBUCKET_SERVER_PASSWORD`.
- **A real bug fix, not just the new forge:** Bitbucket webhooks were broken for
  *both* products. The receiver accepted only `pr:opened` / `pr:modified` —
  Bitbucket **Server** event names — while reading the PR URL from
  `pullrequest.links.html.href`, which is Bitbucket **Cloud**'s payload shape. A
  genuine Cloud webhook was rejected as not reviewable; a genuine Server webhook
  produced no URL. Both dialects now work.
- Docs, README, `CLAUDE.md` and `.env.example` updated, and every "Bitbucket is
  Cloud only" / "Server is not supported" claim removed.

## Four things the previous handoff got wrong

Recorded because each one would have cost the next person real time.

1. **The Cloud-before-Server ordering rationale was false.** The old handoff said
   Cloud's parser is the more specific of the two and that Server-first would make
   Cloud URLs match Server. Tested by reversing the tuple and running six URLs
   through `detect_forge`: **every case resolved identically.** The parsers are
   disjoint — Cloud pins `^https?://bitbucket\.org/` plus a bare
   `owner/repo/pull-requests/N`; Server requires a `/projects|users/KEY/repos/REPO/`
   prefix. No URL matches both, including the adversarial `bitbucket.org` host with
   a Server-shaped path, which only Server matches under either order. The Cloud-first
   order is kept as **defence in depth** — if either parser is later loosened, the
   failure degrades into a shadowed forge rather than a mis-routed one — but it is
   not load-bearing, and no document should claim it is.

2. **Both registration line numbers pointed at the wrong line.** `forges/base.py:93`
   and `config.py:378` are the `def` lines; the literals that actually need editing
   were the tuple at `base.py:97` and the dict at `config.py:386-390`. Cite the
   construct, not the function.

3. **`uv run pytest` does not work in this repo.** pytest lives in
   `[project.optional-dependencies] dev`, so the documented command dies with
   `Failed to spawn: pytest / No such file or directory (os error 2)` — an error that
   reads like a broken venv rather than a missing extra. The invocation is:

   ```bash
   uv run --extra dev pytest
   ```

4. **The diff direction that reads "what the branch changed" is backwards here.**
   `cc58915` was cut from v0.2.0, so `git diff main...cc58915` renders main's own
   v0.3/v0.4 work as *additions* — applying it literally reverts the entire config-surface
   release (17 config rows and 4 sections in `docs/env-vars.md` alone). Most hunks on
   that branch are regressions, not features. Diff the branch's **own** delta instead:

   ```bash
   git diff a7abbf1 cc58915 -- <path>
   ```

## The coupling that will catch the next person adding a config key

`tests/test_docs_consistency.py` compiles `docs/env-vars.md` and `.env.example`
against `config._DEFAULTS` **in both directions**, and asserts two hard-coded
integers built as `f"**{len(_DEFAULTS)}** configuration keys"` and
`f"for {len(_DEFAULTS)+len(_LEGACY_ENV_ALIASES)} accepted variable names"`.

So a new config key is not a source change — it is an atomic four-surface change:
`_DEFAULTS`, the `config.py` docstring, `.env.example`, and `docs/env-vars.md`
including its counts and its `Per-Forge Auth (N)` section heading. Adding three keys
here failed five tests until all four surfaces moved together. Current values: **33**
keys, **1** legacy alias, **34** accepted names.

## Release shape (follow this next time)

```bash
uv build                       # produces BOTH sdist and wheel
gh release upload vX.Y.Z dist/prxref-X.Y.Z.tar.gz dist/prxref-X.Y.Z-py3-none-any.whl
```

Both assets matter: the v0.4.0 release ships both, and at least one consumer updates
itself with `gh release download --pattern '*.tar.gz'`, which does **not** match
GitHub's auto-generated source archive. A release without the attached sdist silently
breaks those consumers.

## Verified at release

```
840 passed                                    uv run --extra dev pytest
All checks passed!                            uv run --extra dev ruff check src/ tests/
0.5.0                                         uv run prxref --version
bitbucket-server   .../projects/PROJ/repos/app/pull-requests/42
bitbucket          https://bitbucket.org/ws/app/pull-requests/7
github             https://github.com/o/r/pull/3
gitlab             https://gitlab.com/o/r/-/merge_requests/9
make_forge(ref) -> prxref.forges.bitbucket_server.ForgeImpl, name 'bitbucket-server'
```

## Still open — not part of this release

- **Observability / tracing** — prompt+response tracing, a `PRXREF_TRACE_DIR`,
  per-finding drop reasons, a machine-readable run report. Nothing has landed.
- **`review --timeout SECONDS`** — the per-run counterpart to `PRXREF_LLM_TIMEOUT`,
  which shipped natively in v0.4.0.

The next three came out of the v0.5.0 release review, which raised them against
`bitbucket_server.py`. Each one is real, and each one is **repo-wide, not a porting
defect**: the new adapter does what its siblings already do, so all three were left
alone rather than fixed in one adapter and creating a four-way inconsistency. Whoever
takes one on should change all four adapters in the same commit.

- **Retries re-send non-idempotent writes.** All four retry sessions list `POST` in
  `allowed_methods` against `status_forcelist [429, 500, 502, 503, 504]` with
  `total=3` — `bitbucket_server.py:62-65`, `bitbucket.py:33-36`, `gitlab.py:33-36`,
  `github.py:27-29` (which adds `PATCH`). If a comment POST commits server-side and
  the response is lost to a 502/504 or a read timeout, urllib3 re-sends it and the
  comment is duplicated. The fix is to drop the write verbs from `allowed_methods`
  and let the caller decide, but it changes retry behaviour for every forge.
- **Comment listings are capped and the cap is silent.** Bitbucket Server reads
  5 x 100 activities (`bitbucket_server.py:17-18,267`) and Bitbucket Cloud 5 x 100
  comments (`bitbucket.py:216-220`); GitLab reads a single page of 50
  (`gitlab.py:232,321`) and GitHub a single unparameterised page, so 30
  (`github.py:124,167`). Past the cap `list_threads` under-reports and `post_summary`
  can miss its own `<!-- prxref-summary -->` and post a second summary. Server is the
  *most* thorough of the four here, not the least. A shared paging helper with an
  explicit "truncated" signal would fix all four at once.
- **A failed comment-listing read is treated as "no summary exists".** In
  `post_summary`, a listing that errors leaves the existing-summary handle unset and
  control falls through to the create-a-new-comment POST:
  `bitbucket_server.py:310-311` then `:324`, and `gitlab.py:241-242` then `:254`.
  GitHub has no `try` at all — a transport error propagates — but a non-`ok`
  response takes the same fall-through (`github.py:125` -> `:139`). Bitbucket Cloud
  is furthest from correct: `post_summary` (`bitbucket.py:165`) never looks for an
  existing summary, so it posts a duplicate on *every* re-review. Distinguishing
  "read failed" from "nothing found" and skipping the post is the fix, and it is the
  same three-line change in each adapter.
- **Move the dev tools to a dependency group.** `pytest` and `ruff` are declared under
  `[project.optional-dependencies] dev`, and `uv run` never installs a project *extra* —
  which is why the bare `uv run pytest` fails on a cold checkout with
  `Failed to spawn: pytest` (reproduced on a pristine `git archive` of this branch,
  exit 2). Every doc now spells the `--extra dev` form, but replacing
  `[project.optional-dependencies] dev` with `[dependency-groups] dev` would make the
  bare command correct and remove the trap at its source. It is a real behaviour change
  — the `dev` extra stops being installable as `pip install prxref[dev]` — so it needs
  its own decision rather than riding along with a release.

- **`origin/feat/bitbucket-server-forge` can be deleted** once you are satisfied with
  v0.5.0. Everything worth keeping from it is on `main`; the rest is v0.2.0-era text.
- **`CONTRIBUTING.md` has no inbound link any more.** Deleting the
  `### Bitbucket Server / Data Center (unsupported)` section from `docs/forges.md`
  removed the docs' only pointer to it. The file still exists and GitHub surfaces it
  natively, so nothing is broken — but nothing points at it either.

| Item | Value |
|---|---|
| Released version | `0.5.0` (minor — new forge plus a webhook fix, nothing breaking) |
| Registration points | the tuple in `forges/base.py`, the `impls` dict in `config.py` |
| Version strings | `pyproject.toml`, `src/prxref/__init__.py`, and `uv.lock` |
| Test command | `uv run --extra dev pytest` |
| Release assets | sdist **and** wheel, both attached |
