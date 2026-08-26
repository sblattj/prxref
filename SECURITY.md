# Security Policy

## Reporting a Vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/sblattj/prxref/security/advisories/new)
rather than opening a public issue.

Include the version, the forge and backend involved, and the smallest set of
steps that reproduces the problem. You should get an initial response within a
week.

## Supported Versions

prxref is pre-1.0. Only the latest release receives security fixes.

## Security Model

prxref holds credentials for third-party code forges and accepts unauthenticated
network input on its webhook endpoint. The properties below are the ones worth
attacking, and the ones worth reporting if you can break them.

### Credentials

- Every credential is read from the environment at call time. prxref writes no
  credential file and has no config file that can hold one.
- prxref reads **no upstream LLM provider credentials** — no OpenAI, AWS, Google,
  or Anthropic keys. It talks to one OpenAI-compatible endpoint whose URL and key
  you supply. Provider credentials live behind that endpoint, not here.
- Forge tokens are scoped per forge (`PRXREF_BITBUCKET_TOKEN`,
  `PRXREF_GITHUB_TOKEN`, `PRXREF_GITHUB_ENTERPRISE_TOKEN`, `PRXREF_GITLAB_TOKEN`).
  Grant only pull-request write scope.
- `PRXREF_GITHUB_ENTERPRISE_TOKEN` exists so a self-hosted GHES token is not sent
  to `github.com`. Host determines which token is used.

See [docs/env-vars.md](docs/env-vars.md) for the full variable table.

### Webhook signature verification

`prxref serve` verifies every payload before acting on it:

| Forge | Header | Scheme |
|---|---|---|
| GitHub | `X-Hub-Signature-256` | HMAC-SHA256, constant-time compare |
| Bitbucket | `X-Hub-Signature` | HMAC-SHA256, constant-time compare |
| GitLab | `X-Gitlab-Token` | shared secret, constant-time compare |

A payload that fails verification is rejected and never reaches the review
pipeline. A correctly-signed payload for an event prxref does not handle is
ignored rather than queued.

### `PRXREF_ALLOW_UNSIGNED`

This flag disables signature verification and is for local development only.
**Do not set it in production.**

It requires the **literal string `1`**. `true`, `yes`, and `on` are deliberately
rejected so that a stray truthy value cannot silently disable verification. When
it is on, prxref logs a warning at startup and marks each bypassed response with
an `X-Prxref-Warning` header.

Even with the bypass on, a payload carrying a *wrong* signature is still
rejected — the bypass only tolerates a *missing* one.

### Deliberate non-goals

- **prxref does not gate merges.** `review` exits 0 even when the review fails.
  It is an advisor. Do not build a security control on its exit code.
- **Diff content is sent to the LLM endpoint you configure.** If your diffs are
  sensitive, point prxref at an endpoint you trust. prxref does not choose or
  contact any provider on its own.
- **Review output is model-generated and not authoritative.** Findings can be
  wrong. Treat them as review comments, not as verified vulnerability reports.
