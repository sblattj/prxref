# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] — 2026-08-27

The configuration surface release. Driven by a field report from an on-prem
deployment behind a self-hosted OpenAI-compatible gateway: roughly half of what
was asked for did not exist, and several keys that did exist never reached the
code that was supposed to honour them.

### Added

- **`PRXREF_LLM_MAX_TOKENS`** (default `4096`), **`PRXREF_LLM_TIMEOUT`**
  (default `45.0`), and **`PRXREF_LLM_TEMPERATURE`** (default empty, which
  omits the key from the payload entirely — some endpoints reject `temperature`
  alongside reasoning parameters, so no numeric default is ever sent).
  Temperature is validated when the client is built; a malformed value exits 2
  naming the variable.
- **`PRXREF_CHUNK_TOKEN_BUDGET`** (default `25000`). The token budget was a
  parameter of `build_chunks` all along; the orchestrator simply never passed
  it. This is the knob that actually governs chunk size.
- **`PRXREF_MAX_WORKERS`** (default `4`) and **`PRXREF_MAX_INLINE_COMMENTS`**
  (default `15`) — deployment-shaped comfort knobs that were module constants.
- **`PRXREF_DRY_RUN`** (literal `1`, via the shared `_truthy` parser). The CLI
  already had `--no-post`; the webhook daemon had no dry run at all, which is
  precisely backwards — the daemon is the thing you want to observe before
  pointing it at a busy repository. `--no-post` still wins when passed.
- **Truncation observability.** `InvokeResult` carries `finish_reason`, and a
  chunk whose response is truncated at the token budget now says so in
  operator language — `response truncated at max_tokens=4096
  (finish_reason=length); raise PRXREF_LLM_MAX_TOKENS` — instead of a bare
  `JSONDecodeError: no parseable content`. Deduplicated failure reasons (capped
  at three, overflow counted) are named in the partial-review banner posted to
  the PR, because the person who can act on them reads the PR, not the daemon's
  stderr.
- **The whole numeric config surface is range-checked.** A declarative
  `_RANGES` table in `config.py` rejects degenerate values at load time —
  `PRXREF_MAX_CHUNKS=0`, a confidence floor outside `[0.0, 1.0]`, a NaN or
  infinite timeout — with `ConfigError` (exit 2) naming the env var or the CLI
  flag that supplied the value. Before this, `PRXREF_CONFIDENCE_FLOOR=95`
  posted "No findings — nice work." on a broken PR. A drift guard fails CI if a
  numeric key is added without a range.
- **A docs/defaults consistency test.** Every key in `config._DEFAULTS` must
  appear in the `config.py` docstring, `.env.example`, and `docs/env-vars.md`,
  and no undocumented `PRXREF_` name may appear in those files. Adding a knob
  without documenting it now fails CI — the drift that cost the field reporter
  their afternoon cannot recur silently.
- **Documentation sweep.** `docs/env-vars.md` documents all 25 keys with a
  "Tuning for your team" section (advisory vs thorough profiles); the
  exit-code contract (advisor, never a gate) is stated in `README.md` and
  `docs/deploy.md`; Bitbucket's Cloud-only limitation is stated plainly in
  `README.md`, `docs/forges.md`, and `CLAUDE.md`, alongside the fact that
  GitHub Enterprise and self-hosted GitLab work on any host.

### Security

- **A posted failure reason could publish the LLM endpoint and its
  credential.** A `requests` `ConnectionError` carries the gateway host, the
  request path, and the query string in its message; that string was wrapped
  into `LLMError`, stored as the chunk's failure reason, and interpolated
  verbatim into a comment on the pull request — by the total-failure notice and
  by the partial-review banner alike. On a public repository that published the
  operator's endpoint and any `api_key=` riding in its URL. Both posting paths
  now sanitise the reason through one allowlist-flavoured redaction: URLs,
  quoted network locators, bearer tokens, and every `key=value` pair whose key
  is not explicitly postable lose their value. The diagnostic shape survives —
  the exception class, `HTTP 429`, a timeout, and the truncation message with
  its `PRXREF_LLM_MAX_TOKENS` hint are unchanged — and the stderr logs still
  carry the full, unredacted text, because they are operator-only.

### Fixed

- **`PRXREF_MAX_CHUNKS` was silently ignored.** `cli.py` read
  `cfg.get("MAX_CHUNKS", 8)` (uppercase) against `load_config`'s lowercase
  keys, so the env var never reached the orchestrator.
- **`PRXREF_CONFIDENCE_FLOOR` and `PRXREF_MAX_ERROR_FINDINGS` were dead on the
  override path.** `orchestrate_review` called `apply_quality_gate(findings)`
  with no arguments, so the gate re-read the environment directly and a
  programmatic `load_config(confidence_floor=...)` override had no effect.
- **`LiteLLMClient` was constructed without `default_timeout`**, silently
  ignoring any configured timeout.
- **A malformed config value exited 0**, contradicting the documented contract
  that usage errors exit 2. `_coerce_env` now raises `ConfigError` (a
  `ValueError` subclass), and a `--max-chunks 0` flag reports the flag rather
  than the env var it never came from.
- **`parse_unified_diff` and `build_chunks` were the only orchestrator stages
  not wrapped**, so a library caller of `orchestrate_review` could see a raise
  despite the module's documented never-raise contract. The contract is now
  true.
- **The sdist swept untracked internal planning directories** under `docs/`
  into the published tarball. They are now excluded alongside the existing
  `docs/superpowers` precedent.
- **A multi-line failure reason broke out of the partial-review blockquote.**
  The `> ` prefix was applied per reason rather than per line, so a two-line
  reason mangled the rest of the posted comment.
- **The truncation message quoted a normalised stop reason** rather than the
  one the provider actually sent, sending an operator whose gateway logged
  `MAX_TOKENS` to grep for a string that was not in their log.
- **`python -m prxref.cli` exited 0 having done nothing** — no `__main__`
  guard.

### Changed

- **Test environment isolation is derived, not hand-maintained.** Five test
  files each kept their own list of `PRXREF_*` names to clear, and the lists
  had already drifted. A `tests/conftest.py` autouse fixture now derives the
  full set from `config._DEFAULTS` plus legacy aliases, so a new key cannot
  leak ambient environment into the suite.

## [0.2.0] — 2026-08-26

First published release. 0.1.0 was never tagged or uploaded — its entry is kept
below as the development baseline for the work it describes.

### Changed

- **Shipped defaults no longer point at anything.** `PRXREF_LLM_BASE_URL`,
  `PRXREF_LLM_API_KEY` and `PRXREF_LLM_MODELS` now default to empty. They
  previously defaulted to a private LAN endpoint (`http://127.0.0.1:8090/v1`,
  model chain `flash,orch`), so a fresh install with no configuration issued a
  request that could only fail, against infrastructure that was never yours.
- **`prxref review` exits 2 when required configuration is missing**, raising
  the new `prxref.llm.ConfigError` naming the exact variable to set. This is
  narrow and deliberate: a missing endpoint is a usage error, not a review
  outcome. Genuine review failures still exit 0 — non-blocking is a product
  tenet and is unchanged. An empty `PRXREF_LLM_API_KEY` remains valid, since a
  local no-auth server (Ollama, vLLM) needs none.

### Fixed

- **`PRXREF_ALLOW_UNSIGNED` was parsed two different ways.** `config.py`
  accepted `1`, `true`, `yes` and `on`, while `webhooks._allow_unsigned` — the
  only gate that runs — accepted the literal `1` alone, and nothing in the
  package read the config value at all. Setting it to `true` made the config
  dict report the bypass as enabled while signature verification stayed on.
  This failed safe, so it was a correctness and documentation defect rather
  than a hole. Both now parse identically, pinned by a test asserting they
  agree across 15 inputs.
- **The bundled `pull_request_target` review workflow could never install
  prxref.** It used `uv pip install --system`, which fails on GitHub's Ubuntu
  runners because their system interpreter is PEP 668 externally-managed, and
  which additionally suppressed the virtualenv the setup action provisions.
  The failure was invisible: `continue-on-error` masked it and the review step
  was skipped rather than failed, so the run reported success without a review
  having happened.

### Added

- A test pinning the packaging metadata version to `prxref.__version__`, so the
  two version declarations cannot drift apart across a release.

## [0.1.0] — 2026-08-26

Development baseline. Never published to PyPI and never tagged; superseded by
0.2.0 before release.

### Added

- **Three forges behind one command.** `prxref review --pr-url <url>` detects
  Bitbucket Cloud, GitHub, GitHub Enterprise Server, GitLab SaaS, and self-hosted
  GitLab from the URL alone, including arbitrarily nested GitLab subgroups. All
  three adapters implement a single `Forge` Protocol.
- **Review pipeline.** Parses one unified diff, partitions it into risk-ranked
  chunks, fans out parallel single-shot LLM worker reviews, then gates findings
  through deterministic quality passes (line alignment, dedup, confidence floor)
  before posting.
- **Provider-agnostic LLM access with a fallback chain.** `PRXREF_LLM_MODELS`
  is tried left to right; a model that times out, refuses, or returns malformed
  JSON is abandoned immediately for the next one, with no same-model retries.
  Backends: plain-HTTP OpenAI-compatible (default, zero extra dependencies),
  `litellm` (optional extra). prxref reads no upstream provider credentials.
- **Inline comments plus a summary.** On GitHub and GitLab, summaries are
  deduplicated across re-runs via a hidden `<!-- prxref-summary -->` marker — a
  re-review updates the existing comment instead of stacking a new one.
  Bitbucket posts a fresh summary each run. Every comment carries model
  attribution.
- **Webhook server.** `prxref serve` verifies HMAC-SHA256 (GitHub, Bitbucket) or
  a shared token (GitLab) in constant time, returns `202 Accepted` immediately,
  and processes reviews serially on one background worker. `GET /health` for
  liveness.
- **Graceful degradation per forge.** GitHub 422s on out-of-hunk lines are
  skipped; GitLab position-anchoring failures fall back to a plain note;
  Bitbucket inline 4xxs are non-fatal.
- Docker image and compose file, systemd unit example, and CI templates for
  GitHub Actions, GitLab CI, and Bitbucket Pipelines.
- Docs: [deployment](docs/deploy.md), [forge specifics](docs/forges.md),
  [LLM backends](docs/llm.md), [environment variables](docs/env-vars.md).

### Security

- `PRXREF_ALLOW_UNSIGNED` requires the literal string `1`. `true`, `yes`, and
  `on` are deliberately rejected so a stray truthy value cannot silently disable
  webhook signature verification. Even when enabled, a payload carrying a wrong
  signature is still rejected — only a missing one is tolerated.
- Per-forge token separation, so a self-hosted GitHub Enterprise token is never
  sent to `github.com`.

### Notes

- `review` exits 0 even when the review fails. prxref is an advisor, not a merge
  gate; do not build a security control on its exit code.
- Diff content is sent to whichever OpenAI-compatible endpoint you configure.
- Requires Python 3.12+. Tested on 3.12 and 3.13.

[Unreleased]: https://github.com/sblattj/prxref/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/sblattj/prxref/releases/tag/v0.3.0
[0.2.0]: https://github.com/sblattj/prxref/releases/tag/v0.2.0
