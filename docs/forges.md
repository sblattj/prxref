# Forge Integrations & Webhooks

`prxref` provides unified pull/merge request reviews across Bitbucket (Cloud and Server / Data Center), GitHub (Cloud and Enterprise Server), and GitLab (SaaS and self-hosted).

## Supported Hosts

| Forge | Cloud | Self-hosted |
|---|---|---|
| **Bitbucket** | `bitbucket.org` | Supported — Bitbucket Server / Data Center, any host, including a deployment context path |
| **GitHub** | `github.com` | Supported — GitHub Enterprise Server, any host |
| **GitLab** | `gitlab.com` | Supported — any host, including nested subgroups |

Every host is covered, but not by the same means. GitHub and GitLab are host-agnostic within one adapter each, because their self-hosted products speak the same REST API as their SaaS ones, differing only in base URL (`/api/v3` for GHES, `/api/v4` for every GitLab). Bitbucket is not: Server / Data Center exposes a different API surface (`/rest/api/1.0`) with different resource shapes, so it is a fourth adapter rather than a base-URL setting, selected automatically from the URL. See [Bitbucket Server / Data Center](#4-bitbucket-server--data-center).

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
  - **Accepted Events:** `pullrequest:created`, `pullrequest:updated`
  - **Payload:** PR URL read from `pullrequest.links.html.href`.
  - **Signature Header:** `X-Hub-Signature` (HMAC-SHA256) validated against `PRXREF_BITBUCKET_WEBHOOK_SECRET`.

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

---

## 4. Bitbucket Server / Data Center

Self-hosted Bitbucket is a different product from Bitbucket Cloud, not the same
API on another host: `/rest/api/1.0` rather than `/2.0`, project keys rather
than workspaces, an activity feed rather than a comment list, and `start`/`limit`
paging rather than `page`/`pagelen`. It therefore gets its own adapter.

- **Forge Identifier:** `bitbucket-server`
- **Supported URL Shapes:**
  - `http(s)://{host}/projects/{PROJECTKEY}/repos/{slug}/pull-requests/{number}`
  - `http(s)://{host}/users/{userslug}/repos/{slug}/pull-requests/{number}` (personal repository)
  - Either shape behind a deployment context path, e.g. `https://{host}/bitbucket/projects/...`
  - A trailing route (`/overview`, `/diff`, …) is tolerated and normalized away.
  - The REST form of any of the above — `https://{host}{context}/rest/api/1.0/projects/...`,
    plus other version numbers and the `/rest/api/latest` alias — is accepted and normalized
    back to the browse URL. Server is the only forge here whose REST URL has the same path
    shape as its browse URL, so an unstripped `/rest/api/1.0` reads as a context path and gets
    replayed in front of the API base.
- **Scheme Note:** the scheme of the URL you pass is preserved, not normalized to
  `https`. A Data Center standalone install serves plain HTTP on port 7990, so
  `http://{host}:7990/projects/...` is the product's out-of-the-box shape; the
  normalized `PRRef.url` and every API request keep that scheme. `HTTP://` is
  lowercased. This adapter is the exception — the other three build their API base
  URL as `https://` regardless of the scheme given.
- **Project Key Note:** a personal repository browses under `/users/{slug}` but is
  addressed in the API as the project key `~{slug}`. `PRRef.owner` always holds the
  API form, so every request path is built identically for both kinds.
- **Detection Order:** `detect_forge` asks the Cloud parser before this one. The two
  patterns are disjoint — Cloud pins `bitbucket.org` and a bare
  `owner/repo/pull-requests/N` path, while Server requires a
  `/projects|users/{KEY}/repos/{REPO}/` prefix — so every URL resolves the same
  under either order. The order is defensive, not load-bearing: asking the narrower
  parser first means a later loosening degrades into a shadowed forge rather than a
  silently mis-routed one.
- **Authentication Environment Variables:**
  - `PRXREF_BITBUCKET_SERVER_TOKEN` (HTTP access token, sent as `Bearer`). Falls back to
    `PRXREF_BITBUCKET_TOKEN` when unset, mirroring how GitHub Enterprise falls back to
    `PRXREF_GITHUB_TOKEN`.
  - `PRXREF_BITBUCKET_SERVER_USER` + `PRXREF_BITBUCKET_SERVER_PASSWORD` (HTTP Basic fallback)
- **API Endpoints & Behavior:**
  - **Base URL:** `{scheme}://{host}{context}/rest/api/1.0/projects/{key}/repos/{slug}/pull-requests/{number}`,
    where `{scheme}` is the one the PR URL was given with.
  - **Metadata:** `GET` on the base URL. Branches and SHAs come from `fromRef`/`toRef`
    (`displayId`, `latestCommit`); author from `author.user.name`, falling back to
    its `slug` then `displayName`.
  - **Diffs:** `GET {base}.diff` with `Accept: text/plain` — the `.diff` suffix on the PR
    resource, not a `/diff` subpath. Returns one raw unified diff for the whole PR.
  - **Summary Comments:** `POST {base}/comments` with `{"text": body}`. Dedup scans the
    activity feed for `<!-- prxref-summary -->` on a comment with no `anchor`, and updates
    via `PUT {base}/comments/{id}`. **Data Center rejects an update that omits the
    comment's current `version`**, so the lookup keeps the version, not just the id.
  - **Inline Comments:** `POST {base}/comments` with an `anchor` object
    (`path`, `line`, `lineType: ADDED`, `fileType: TO`) rather than Cloud's `inline.path` /
    `inline.to`. Individual 4xx responses (line outside the diff) are skipped, as elsewhere.
  - **Thread List:** `GET {base}/activities`, filtered to `action == "COMMENTED"`. There is
    no flat comment listing on Data Center. Paged with `start`/`limit`, following
    `nextPageStart` until `isLastPage`, capped at 5 pages.
  - **Resolution:** read from whichever of `state == "RESOLVED"`, `threadResolved`, or
    `resolvedDate` the deployment's version exposes.
- **Webhook Integration:**
  - **Event Header:** `X-Event-Key` (shared with Cloud; the two are told apart by event
    name and payload shape)
  - **Accepted Events:** `pr:opened`, `pr:modified`, `pr:from_ref_updated`
  - **Payload:** PR URL read from the first entry of `pullRequest.links.self` — note the
    capital `R`, and the list, both of which differ from Cloud.
  - **Signature Header:** `X-Hub-Signature` (HMAC-SHA256) validated against
    `PRXREF_BITBUCKET_WEBHOOK_SECRET`, the same secret Cloud uses.
