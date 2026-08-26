# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/sblattj/prxref/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/sblattj/prxref/releases/tag/v0.2.0
