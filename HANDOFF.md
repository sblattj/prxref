# HANDOFF — cut v0.5.0: release the Bitbucket Server / Data Center forge

**Repo:** `sblattj/prxref` (public) · **`main` tip:** `c02cfc4` (v0.4.0) · **Written:** 2026-08-28

One capability is finished, proven in real use, and has never shipped: the **Bitbucket
Server / Data Center forge**. It exists only on `origin/feat/bitbucket-server-forge`
(`cc58915`, *"feat: Bitbucket Server / Data Center forge"*), so every deployment that needs it
runs a copy of `bitbucket_server.py` overlaid onto a released tree by hand. That overlay is the
thing to delete, and releasing is how you delete it.

## Why it isn't released yet

Not a technical blocker — the work was done on a machine that had no write-scoped GitHub
credential (read-only token, and git-over-SSH blocked by a network proxy), so it could
consume releases but not cut one. Anyone running this with a normal `gh auth login` can finish it.

## Scope of the release

**v0.5.0 — minor.** A new forge is additive: no existing flag, env var, or forge behaviour
changes. Nothing here is breaking, so it is not 1.0, and it is more than a patch.

What lands:

1. `src/prxref/forges/bitbucket_server.py` from the feature branch.
2. Registration in **two** places — both must change or the forge is dead code:
   - `src/prxref/forges/base.py:93` — `detect_forge()` iterates a literal tuple
     `(bitbucket, github, gitlab)`. Add the Server module, and keep **Cloud before Server**:
     Cloud's URL parser is the more specific of the two, and flipping the order makes Cloud
     URLs match Server first.
   - `src/prxref/config.py:378` — `make_forge()` maps names to implementations in an `impls`
     dict. Add `"bitbucket-server": bitbucket_server.ForgeImpl`.
3. Version bump to `0.5.0` in `pyproject.toml` (line 3) **and** `src/prxref/__init__.py`.
4. `CHANGELOG.md` entry.

**Optional, only if cheap:** port the branch's tests, and the `webhooks.py` Server routing (that
one only matters for the `serve` daemon).

## Why the overlay has been safe, and why merging is therefore low-risk

Checked rather than assumed, against v0.4.0: the shared types (`PRRef`, `PRData`,
`InlineComment`, `Thread`) are identical between the feature branch and the release; the Forge
protocol methods (`get_pr`, `get_diff`, `post_summary`, `post_inline_comments`, `list_threads`)
match exactly; and the `review` CLI flags are unchanged. The 18 commits that made up v0.4.0
touched `llm_backends`, `cli` and chunking — not the forge contract.

## Steps

```bash
git switch -c release/0.5.0 main
git checkout origin/feat/bitbucket-server-forge -- src/prxref/forges/bitbucket_server.py
# register in forges/base.py:93 and config.py:378 (see above), bump both version strings,
# add the CHANGELOG entry
uv run pytest
uv run prxref --version          # must print 0.5.0
```

Then: open a PR, merge it, tag `v0.5.0`, and create the GitHub Release.

**Author identity:** commits and tags use the GitHub noreply address
(`5125883+sblattj@users.noreply.github.com`). Never a work email.

**Do not skip the sdist asset.** The v0.4.0 release ships an attached `*.tar.gz`, and at least one
consumer updates itself with `gh release download --pattern '*.tar.gz'` — which does **not** match
GitHub's auto-generated source archive. A release without that asset silently breaks those
consumers:

```bash
uv build            # or: python -m build --sdist
gh release upload v0.5.0 dist/prxref-0.5.0.tar.gz
```

## How you know it worked

A downstream tree that used to overlay the forge should now report that the release already
carries it, and stop patching. Directly:

```bash
python -c "from prxref.forges import bitbucket_server; print(bitbucket_server.ForgeImpl)"
python -c "from prxref.config import make_forge"   # 'bitbucket-server' resolves
```

Point it at a Server PR URL (`/rest/api/1.0` style host) and confirm `detect_forge` returns a
`PRRef` with forge `bitbucket-server`, and that a Cloud URL still detects as `bitbucket`.

## Known-good runtime shape for the Server case

For anyone reproducing the setup this forge was built for: an OpenAI-compatible HTTP backend
(`PRXREF_LLM_BACKEND=http`, `PRXREF_LLM_BASE_URL`, `PRXREF_LLM_MODELS`, `PRXREF_LLM_TIMEOUT`),
with the forge token supplied at runtime from a credential store rather than committed anywhere.
`PRXREF_LLM_TIMEOUT` shipped natively in v0.4.0; a per-run `review --timeout SECONDS` flag does
**not** exist yet and is still a reasonable follow-up.

## Follow-ups that are NOT part of this release

- **Observability / tracing** — prompt+response tracing, a `PRXREF_TRACE_DIR`, per-finding drop
  reasons, and a machine-readable run report. None of it landed in v0.4.0; still worth filing.
- **`review --timeout SECONDS`** — the per-run counterpart to the env knob.
- A configurable-LLM-timeout issue draft is **obsolete**: v0.4.0 shipped the env knob it asked for.

| Item | Value |
|---|---|
| Target version | `0.5.0` (minor — new forge, nothing breaking) |
| Source of the forge | `origin/feat/bitbucket-server-forge` @ `cc58915` |
| Registration points | `src/prxref/forges/base.py:93`, `src/prxref/config.py:378` |
| Version strings | `pyproject.toml:3`, `src/prxref/__init__.py` |
| Release must include | an attached sdist `*.tar.gz` asset |
| Commit identity | `5125883+sblattj@users.noreply.github.com` |
