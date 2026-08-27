# Forge Integrations & Webhooks

`prxref` provides unified pull/merge request reviews across Bitbucket Cloud, GitHub (Cloud and Enterprise Server), and GitLab (SaaS and self-hosted).

## Supported Hosts

| Forge | Cloud | Self-hosted |
|---|---|---|
| **Bitbucket** | `bitbucket.org` only | **Not supported** — Bitbucket Server / Data Center has no adapter |
| **GitHub** | `github.com` | Supported — GitHub Enterprise Server, any host |
| **GitLab** | `gitlab.com` | Supported — any host, including nested subgroups |

The asymmetry is real and worth stating plainly: GitHub and GitLab are host-agnostic because their self-hosted products speak the same REST API as their SaaS ones, differing only in base URL (`/api/v3` for GHES, `/api/v4` for every GitLab). Bitbucket Server / Data Center does not — it exposes a different API surface (`/rest/api/1.0`) with different resource shapes, so supporting it means writing a fourth adapter, not setting a base URL. See [Bitbucket Server / Data Center](#bitbucket-server--data-center-unsupported).

---

## 1. Bitbucket Cloud

- **Forge Identifier:** `bitbucket`
- **Supported URL Shapes:**
  - `https://bitbucket.org/{workspace}/{repo}/pull-requests/{number}`
  - `https://bitbucket.org/{workspace}/{repo}/pullrequests/{number}`
  - `https://bitbucket.org/{workspace}/{repo}/pullrequest/{number}`
- **Authentication Environment Variables:**
  - `PRXREF_BITBUCKET_TOKEN` (Bearer token, e.g. Workspace or Repository Access Token with `pullrequest:write` scope)
  - `PRXREF_BITBUCKET_USER` + `PRXREF_BITBUCKET_APP_PASSWORD` (fallback to HTTP Basic Auth)
- **API Endpoints & Behavior:**
  - **Metadata:** `GET /2.0/repositories/{owner}/{repo}/pullrequests/{number}`
  - **Diffs:** `GET /2.0/repositories/{owner}/{repo}/pullrequests/{number}/diff` with `Accept: text/plain`. Note that Bitbucket's `/diff` endpoint enforces an upstream size ceiling of ~5–10MB.
  - **Summary Comments:** `POST /2.0/repositories/{owner}/{repo}/pullrequests/{number}/comments` with `{"content": {"raw": body}}`.
  - **Inline Comments:** `POST /2.0/repositories/{owner}/{repo}/pullrequests/{number}/comments` with `inline.path` and `inline.to`. Non-fatal 4xx errors on individual inline comments are skipped.
  - **Thread List:** `GET /2.0/repositories/{owner}/{repo}/pullrequests/{number}/comments` (paginated, up to 500 comments).
- **Webhook Integration:**
  - **Event Header:** `X-Event-Key`
  - **Accepted Events:** `pr:opened`, `pr:modified`
  - **Signature Header:** `X-Hub-Signature` (HMAC-SHA256) validated against `PRXREF_BITBUCKET_WEBHOOK_SECRET`.

### Bitbucket Server / Data Center (unsupported)

Only `bitbucket.org` is recognized. A Bitbucket Server or Data Center URL — `https://<your-host>/projects/{KEY}/repos/{repo}/pull-requests/{n}` — is rejected by `ForgeImpl.parse_pr_url`, which checks the host before anything else:

```python
if parsed.netloc.lower() != "bitbucket.org":
    return None
```

Two consequences follow from *where* that check sits.

**Credentials are never the problem.** `parse_pr_url` is a `staticmethod` that reads no environment; auth is only consulted later, at request time. So a Server URL fails identically whether `PRXREF_BITBUCKET_TOKEN` is correct, wrong, or unset — do not go debugging the token. `detect_forge` returns `None` for the URL, and `prxref review` prints `unrecognized PR URL ...` and exits `0`, because an unusable URL is a review error and review errors never fail a build.

**There is no base-URL setting that would fix it.** The Cloud API root is the module constant `_API_BASE = "https://api.bitbucket.org/2.0"`, and every request path (`/2.0/repositories/{owner}/{repo}/pullrequests/...`) is written against Cloud's v2 resource shapes. Server's `/rest/api/1.0` returns different bodies for the same concepts, so pointing this adapter at a Server host would fail on the first response rather than the first URL. Bitbucket Server support is a fourth adapter implementing the `Forge` Protocol in `forges/base.py` — see [CONTRIBUTING.md](../CONTRIBUTING.md), which is exactly the shape of contribution the Protocol exists for.

---

## 2. GitHub & GitHub Enterprise Server

- **Forge Identifier:** `github`
- **Supported URL Shapes:**
  - `https://github.com/{owner}/{repo}/pull/{number}`
  - `https://{ghes-host}/{owner}/{repo}/pull/{number}`
- **Authentication Environment Variables:**
  - `PRXREF_GITHUB_TOKEN` (Personal Access Token or GitHub App token for `github.com`)
  - `PRXREF_GITHUB_ENTERPRISE_TOKEN` (used when host is not `github.com`; falls back to `PRXREF_GITHUB_TOKEN`)
- **API Endpoints & Behavior:**
  - **Base URL:** `https://api.github.com` for `github.com`, or `https://{host}/api/v3` for GHES.
  - **Metadata:** `GET /repos/{owner}/{repo}/pulls/{number}`
  - **Diffs:** `GET /repos/{owner}/{repo}/pulls/{number}` with `Accept: application/vnd.github.v3.diff, application/vnd.diff`.
  - **Summary Comments:** Managed on the issue comments endpoint (`/repos/{owner}/{repo}/issues/{number}/comments`). Summary deduplication is handled via the embedded hidden HTML marker `<!-- prxref-summary -->`. If an existing review comment contains this marker, it is updated via `PATCH /repos/{owner}/{repo}/issues/comments/{comment_id}` instead of creating a duplicate comment.
  - **Inline Comments:** `POST /repos/{owner}/{repo}/pulls/{number}/comments` with `body`, `path`, `line`, and `side` (`RIGHT`). HTTP 422 errors (e.g. comment line not part of diff hunk) are gracefully skipped.
  - **Thread List:** `GET /repos/{owner}/{repo}/pulls/{number}/comments`.
- **Webhook Integration:**
  - **Event Header:** `X-GitHub-Event` (must equal `pull_request`)
  - **Accepted Actions:** `opened`, `synchronize`
  - **Signature Header:** `X-Hub-Signature-256` (HMAC-SHA256) validated against `PRXREF_GITHUB_WEBHOOK_SECRET`.

---

## 3. GitLab (SaaS & Self-Hosted)

- **Forge Identifier:** `gitlab`
- **Supported URL Shapes:**
  - `https://gitlab.com/{namespace}/{project}/-/merge_requests/{number}`
  - `https://{gitlab-host}/{group}/{subgroup...}/{project}/-/merge_requests/{number}` (supports arbitrary nested subgroup paths)
- **Authentication Environment Variables:**
  - `PRXREF_GITLAB_TOKEN` (Personal, Project, or Group Access Token sent via `PRIVATE-TOKEN` header)
- **API Endpoints & Behavior:**
  - **Base URL:** `https://{host}/api/v4/projects/{url_encoded_project_path}`
  - **Metadata:** `GET /merge_requests/{number}`. Target SHA is resolved from `diff_refs.base_sha` or fallback branch lookup.
  - **Diffs:** `GET /merge_requests/{number}/diffs?access_raw_diffs=true`. Reconstructs a full unified multi-file diff string from GitLab's structured diff items (including new, deleted, renamed, and modified file headers).
  - **Summary Comments:** Managed via `GET/POST/PUT /merge_requests/{number}/notes`. Searches for `<!-- prxref-summary -->` and updates existing note via `PUT` if found.
  - **Inline Comments:** Posted as discussions via `POST /merge_requests/{number}/discussions` with text position references (`base_sha`, `start_sha`, `head_sha`, `new_path`, `new_line`). If position anchoring fails with HTTP 400 (e.g. line outside diff or obsolete context), it automatically falls back to posting a plain note via `POST /merge_requests/{number}/notes` formatted with `file: {path}\n\n{body}`.
  - **Thread List:** `GET /merge_requests/{number}/discussions`.
- **Webhook Integration:**
  - **Event Header:** `X-Gitlab-Event` (normalized to `MergeRequestHook`)
  - **Accepted Actions:** `open`, `update`
  - **Signature Header:** `X-Gitlab-Token` (plain secret token) validated against `PRXREF_GITLAB_WEBHOOK_SECRET`.
