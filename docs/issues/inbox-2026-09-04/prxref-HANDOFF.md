# prxref — Handoff (Bitbucket Server + Copilot Business)

_Consolidated state + next steps. Written for eventual transfer to a personal `sblattj/prxref` clone. Not yet filed/pushed — the only GitHub credential on this machine is a Copilot OAuth token with `read:user` scope, and git clone/SSH is Zscaler-blocked._

Last verified: local `~/dev/prxref` reports **prxref 0.4.0** with the Bitbucket Server forge active.

---

## 1. What exists right now (local, working)

**`~/dev/prxref` = release v0.4.0 + overlaid Bitbucket Server forge.**

- Base source is the published **v0.4.0** release (`c02cfc402e29`), which natively includes: `PRXREF_LLM_TIMEOUT` knob, posting controls (`--no-post`, `PRXREF_DRY_RUN=1`), exit-code policy (`PRXREF_FAIL_ON`, default `never`), and chunk shaping.
- `src/prxref/forges/bitbucket_server.py` is overlaid from `feat/bitbucket-server-forge` (`cc5891587fa3`) because Server support is **not** in any release yet.
- Registered in two places: `forges/base.py:detect_forge` (Cloud tried before Server) and `config.py:make_forge` (`"bitbucket-server"` → `ForgeImpl`).
- The old local timeout patch was dropped — v0.4.0 has the knob natively (verified `PRXREF_LLM_TIMEOUT=180` → `default_timeout=180.0`).

Git history (vendored tarball checkout; not a real clone):
```
9e4e339 vendor: sync prxref 0.4.0 + overlay Bitbucket Server forge (feat/bitbucket-server-forge)
58ddfca vendor: upgrade to prxref v0.4.0 + overlay Bitbucket Server forge
0941836 vendor: feat/bitbucket-server-forge @ cc5891587fa3 (tarball; git clone blocked by Zscaler)
```

**Why the overlay is a safe drop-in (verified, not assumed):** shared types (`PRRef`/`PRData`/`InlineComment`/`Thread`) are identical between the feat branch and v0.4.0, the Forge Protocol methods (`get_pr`/`get_diff`/`post_summary`/`post_inline_comments`/`list_threads`) match exactly, and the `review` CLI flags are unchanged. The 18 commits in v0.4.0 touched llm_backends/cli/chunking, not the forge contract.

**Not ported:** `webhooks.py` Server routing (only matters for the `serve` daemon, which we don't use) and the branch's tests.

---

## 2. `review-syf-pr.sh` (in `~/dev/prxref`)

Two modes; secrets are injected at runtime, never stored.

```
./review-syf-pr.sh                        # PR #26, dry run (--no-post -v)
./review-syf-pr.sh <pr-url>               # given PR, dry run
./review-syf-pr.sh <pr-url> -v            # ACTUALLY POSTS (omit --no-post)
./review-syf-pr.sh <pr-url> --no-post -v  # explicit dry run
./review-syf-pr.sh update                 # re-sync latest release + re-overlay Server forge, verify, commit
```

- **review**: sources `.env`, pulls the Copilot token from `~/.local/share/opencode/auth.json` and the Bitbucket token from `~/.config/syf-skills/bitbucket/token.json`, then `exec prxref review`.
- **update**: downloads the latest release `*.tar.gz` asset via `gh`, and if the release lacks `bitbucket_server.py`, fetches it from `feat/bitbucket-server-forge` and re-registers it in `base.py` + `config.py`. It is **idempotent**, **auto-skips** the overlay if a release ever ships Server support, and **fails loudly** if a future release restructures forge registration (so it never silently ships a broken forge). Preserves `.git`/`.venv`/`.env`/the script; commits with a noreply identity.
- Config knobs: `PRXREF_REPO` (default `sblattj/prxref`), `PRXREF_SERVER_FORGE_REF` (default `feat/bitbucket-server-forge`), `PRXREF_DIR`.

**Verified:** `--version` → 0.4.0; Server URL detects as `bitbucket-server`; `update` idempotent (2nd run = "nothing new to commit"); PR #26 dry run → Approved, 0 findings, 3 chunks, 180s timeout, no post.

---

## 3. Runtime environment (our setup)

- Backend: **GitHub Copilot Business** as an OpenAI-compatible endpoint.
  - `PRXREF_LLM_BACKEND=http`
  - `PRXREF_LLM_BASE_URL=https://api.business.githubcopilot.com` (note: `business` — the plain `api.githubcopilot.com` host is unreachable on the locked-down network)
  - `PRXREF_LLM_MODELS=claude-opus-4.6,claude-sonnet-5`
  - `PRXREF_LLM_TIMEOUT=180`
- Forge: Bitbucket **Server/Data Center** REST v1 (`/rest/api/1.0`), host `bitbucket.glb.syfbank.com`.
- Secrets (runtime-injected, not in repo/.env): Copilot token from `~/.local/share/opencode/auth.json`; Bitbucket token from `~/.config/syf-skills/bitbucket/token.json`.
- Constraints: git clone/SSH is Zscaler-blocked (use GitHub tarballs/REST API); the on-machine Copilot OAuth token is `read:user` only (cannot push/tag/release/PR).

---

## 4. PENDING: cut the v0.5.0 release (blocked on a write token)

Goal: merge Server support onto `main` so the overlay hack is no longer needed. Decisions already made: **version v0.5.0** (minor — new forge), land via **PR then release**, author commits/tags as **`5125883+sblattj@users.noreply.github.com`** (GitHub noreply, never the company email).

Blocker: need a write-scoped credential for `sblattj` — either `gh auth login`, or a PAT (classic `repo`, or fine-grained on `sblattj/prxref` with **Contents: R/W** + **Administration: R/W** for the Release object) supplied via `GH_TOKEN`.

Repo facts: `main` tip `c02cfc402e29`; latest release `v0.4.0`; the v0.4.0 release ships an attached sdist `*.tar.gz` asset (the `update` script depends on that asset existing, not the auto source archive).

Release steps once a token exists (git clone blocked → do it via GitHub REST API):
1. Branch off `main`; add `src/prxref/forges/bitbucket_server.py`; register in `forges/base.py:detect_forge` and `config.py:make_forge`; bump version in `pyproject.toml` + `src/prxref/__init__.py` to `0.5.0`. (Optional: port `webhooks.py` Server routing + tests.)
2. Open a PR, merge it.
3. Tag `v0.5.0` + create the GitHub Release.
4. Build the sdist locally from `~/dev/prxref` and upload it as a release asset so `gh release download --pattern '*.tar.gz'` keeps working.
5. After release, `./review-syf-pr.sh update` should print "release already ships the bitbucket_server forge — no overlay needed."

---

## 5. Companion issue drafts on the Desktop (status)

- **`prxref-issue-configurable-llm-timeout.md` — mostly OBSOLETE.** v0.4.0 already shipped the `PRXREF_LLM_TIMEOUT` env knob its main ask requested. Only surviving asks: the optional `review --timeout SECONDS` per-run flag (not in v0.4.0; flags are `--pr-url/--no-post/--max-chunks/-v`) and the "release Bitbucket Server support" note (covered by §4). Don't file as written.
- **`prxref-issue-observability-trace.md` — still VALID.** v0.4.0 did not add prompt/response tracing, `PRXREF_TRACE_DIR`, per-finding drop reasons, or a machine-readable run report. Only stale part is its Environment header (says `feat/...@cc5891587fa3`; current tree is v0.4.0).

---

## Addendum (2026-09-17, after this snapshot)

Two items above are no longer open — both landed upstream after this document was
written (it is kept as-is as a v0.4.0-era snapshot):

- `review --timeout SECONDS` — the surviving ask in §5's config-timeout draft — has
  landed as a real flag.
- Prompt+response tracing via `review --trace-dir` / `PRXREF_TRACE_DIR` — the main
  ask in §5's observability draft — has landed. Per-finding drop reasons and a
  machine-readable run report remain open from that draft.
