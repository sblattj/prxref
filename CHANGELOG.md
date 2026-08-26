# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-08-26

First public release.

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

[Unreleased]: https://github.com/sblattj/prxref/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sblattj/prxref/releases/tag/v0.1.0
