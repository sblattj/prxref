# Forge Integrations & Webhooks

`prxref` provides unified pull/merge request reviews across Bitbucket (Cloud and Server / Data Center), GitHub (Cloud and Enterprise Server), GitLab (SaaS and self-hosted), and Azure DevOps (Services and Server).

## Supported Hosts

| Forge | Cloud | Self-hosted |
|---|---|---|
| **Bitbucket** | `bitbucket.org` | Supported — Bitbucket Server / Data Center, any host, including a deployment context path |
| **GitHub** | `github.com` | Supported — GitHub Enterprise Server, any host |
| **GitLab** | `gitlab.com` | Supported — any host, including nested subgroups |
| **Azure DevOps** | `dev.azure.com`, `*.visualstudio.com` | Supported, untested live — Azure DevOps Server, any host; the URL must include the collection and the project |

Every host is covered, but not by the same means. GitHub and GitLab are host-agnostic within one adapter each, because their self-hosted products speak the same REST API as their SaaS ones, differing only in base URL (`/api/v3` for GHES, `/api/v4` for every GitLab). Bitbucket is not: Server / Data Center exposes a different API surface (`/rest/api/1.0`) with different resource shapes, so it is a fourth adapter rather than a base-URL setting, selected automatically from the URL. See [Bitbucket Server / Data Center](#4-bitbucket-server--data-center). Azure DevOps is the fifth adapter, and like GitHub and GitLab it serves both products: Services and Server speak the same REST API and differ only in where the collection sits in the URL. See [Azure DevOps Services & Server](#5-azure-devops-services--server).

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
  - **Summary Comments:** `POST /2.0/repositories/{owner}/{repo}/pullrequests/{number}/comments` with `{"content": {"raw": body}}`, or `PUT .../comments/{id}` when a previous prxref summary is found. The comment feed is scanned for the summary marker first, so a re-review updates its own summary rather than adding another one; inline comments and tombstoned deletions are skipped as update targets.
  - **Inline Comments:** `POST /2.0/repositories/{owner}/{repo}/pullrequests/{number}/comments` with `inline.path` and `inline.to`. Non-fatal 4xx errors on individual inline comments are skipped.
  - **Thread List:** `GET /2.0/repositories/{owner}/{repo}/pullrequests/{number}/comments`, following the cursor until the feed is exhausted. A walk that stops early logs a warning naming the count rather than silently under-reporting.
  - **File Content:** `GET /2.0/repositories/{owner}/{repo}/src/{sha}/{path}`, best-effort, read with the same token as everything else above (no extra scope beyond `pullrequest:write`/repository read). A 404, oversize, or binary body returns `None` and is logged at debug, never a hard error.
- **Webhook Integration:**
  - **Event Header:** `X-Event-Key`
  - **Accepted Events:** `pullrequest:created`, `pullrequest:updated`
  - **Payload:** PR URL read from `pullrequest.links.html.href`.
  - **Signature Header:** `X-Hub-Signature` (HMAC-SHA256) validated against `PRXREF_BITBUCKET_WEBHOOK_SECRET`.
- **Pinned Commit Range (Replay):** `GET /2.0/repositories/{owner}/{repo}/diff/{head_sha}..{base_sha}?topic=true` with `Accept: text/plain`, returning the changes on the head side of the merge-base. Bitbucket spells a range SOURCE..DEST, the reverse of git, so the head SHA comes first; the other order names the reverse range and returns a different diff that still parses. `topic=true` is the merge-base ("three-dot") form and is sent explicitly rather than left to the default, because `topic=false` diffs the two commits directly and so also shows whatever landed on the base after the fork. The text is returned unmodified, an empty range returns empty text, and an HTTP or transport error raises.
- **Description History (Replay):** `GET /2.0/repositories/{owner}/{repo}/pullrequests/{number}` (the creation date, the author, and the current title and description), then the pull request's `GET …/pullrequests/{number}/activity` feed (`pagelen=50`, following `next`, at most 50 pages), then `GET /2.0/repositories/{owner}/{repo}/commit/{sha}` for the head commit's `date` (`--head-sha` when given, else the pull request's source commit). Each `update` entry's `changes.description` carries the old and the new text with the update's `date`, and `changes.title` a rename. The first human review is the earliest approval, change request or comment by a user account (not an app) other than the pull request's author, skipping deleted comments and prxref's own posts. A history that cannot be trusted whole pins nothing: a feed longer than the page budget, an unreadable change entry, or edits that do not chain to the current text leave the current title and description in place, with a warning. The requests carry the same credentials as everything above, and a public repository needs none. On 2026-09-24 the reader was run live, read-only and with no token, against a public pull request whose description had been edited: it read the whole history, took the approval as the first review (an app's comment was skipped), and pinned the text in force then. The `changes.title` shape has not been seen live, because that pull request was never renamed.

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
  - **Diffs:** `GET /repos/{owner}/{repo}/pulls/{number}` with `Accept: application/vnd.github.v3.diff, application/vnd.diff`. GitHub refuses this diff for a pull request whose diff runs past 20,000 lines, answering HTTP `406` with error code `too_large`. On exactly that answer the diff is rebuilt from the paged `GET /repos/{owner}/{repo}/pulls/{number}/files` listing (`per_page=100`, read to the last page), with the same header reconstruction as the GitLab adapter's **Diffs**. The switch is logged at debug only and adds nothing to the summary. A file GitHub lists without a `patch` (binary, or too large for GitHub to render) is logged as a warning and reviewed as a header-only file. GitHub's listing stops at 3,000 files, so it is checked against the pull request's `changed_files` count: a pull request past 3,000 files, or a listing page that cannot be read, fails the review with verdict `Error` rather than reviewing part of it. A `406` without `too_large` (a wrong media type, say) still fails the review, and a pull request under the limit makes the same single request as before. The pinned-range replay below reads the compare endpoint instead, which has no such limit.
  - **Summary Comments:** Managed on the issue comments endpoint (`/repos/{owner}/{repo}/issues/{number}/comments`). Summary deduplication is handled via the embedded hidden HTML marker `<!-- prxref-summary -->`. If an existing review comment contains this marker, it is updated via `PATCH /repos/{owner}/{repo}/issues/comments/{comment_id}` instead of creating a duplicate comment.
  - **Inline Comments:** `POST /repos/{owner}/{repo}/pulls/{number}/comments` with `body`, `path`, `line`, and `side` (`RIGHT`). HTTP 422 errors (e.g. comment line not part of diff hunk) are gracefully skipped.
  - **Thread List:** `GET /repos/{owner}/{repo}/pulls/{number}/comments`.
  - **File Content:** `GET /repos/{owner}/{repo}/contents/{path}?ref={sha}` with `Accept: application/vnd.github.raw+json`, best-effort, read with the same token as everything else above (no extra scope). A 403/404, a JSON body (directory or a file over the 1 MB raw ceiling), oversize, or binary body returns `None` and is logged at debug, never a hard error.
- **Webhook Integration:**
  - **Event Header:** `X-GitHub-Event` (must equal `pull_request`)
  - **Accepted Actions:** `opened`, `synchronize`
  - **Signature Header:** `X-Hub-Signature-256` (HMAC-SHA256) validated against `PRXREF_GITHUB_WEBHOOK_SECRET`.
- **Pinned Commit Range (Replay):** `GET /repos/{owner}/{repo}/compare/{base_sha}...{head_sha}` on the same base URL (GHES included), with `Accept: application/vnd.github.diff`. The three dots are the merge-base form and are required, because the two-dot spelling returns 404. Without the diff media type the endpoint returns its JSON comparison object rather than a diff. The text is returned unmodified, an empty range (a head already merged into the base) returns empty text, and an HTTP or transport error raises. The compare endpoint does not apply the pull-request diff's 20,000-line limit: two read-only probes of public tag ranges on 2026-09-24, one of about 1.0 million diff lines across 3,316 files and one of about 1.05 million lines across 10,373 files, each returned HTTP `200` with the whole diff. So a `--base-sha`/`--head-sha` replay is unaffected by the limit and has no files-listing fallback.
- **Description History (Replay):** one GraphQL query, `POST https://api.github.com/graphql` for `github.com` or `POST https://{host}/api/graphql` on GHES, reads the pull request's description versions (`userContentEdits`, one full text per edit, ordered by `editedAt`), every title rename (the `RENAMED_TITLE_EVENT` timeline), its reviews and conversation comments, and the head commit's `committedDate` (of `--head-sha` when given, else of the pull request's last commit). A connection longer than one page (100 nodes) is read by a further POST, up to 50 in all. The first human review is the earliest submitted review or comment by a `User` (not a bot) other than the pull request's author, skipping prxref's own posts. GitHub's GraphQL API refuses anonymous reads, so this needs `PRXREF_GITHUB_TOKEN` (or `PRXREF_GITHUB_ENTERPRISE_TOKEN` on GHES); without one, and on any failed read, the replay keeps the current title and description and logs a warning. The fields the query reads were checked live against public `github.com` pull requests while the reader was designed, and its tests use recorded response shapes. The GHES endpoint has **not been probed**.

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
  - **Diffs:** `GET /merge_requests/{number}/diffs`, paged with `per_page=100` and read to the last page. Reconstructs a full unified multi-file diff string from GitLab's structured diff items (including new, deleted, renamed, and modified file headers). A page that cannot be read (a transport error, a non-OK status, or a body that is not a JSON list), or a listing longer than 50 pages (5,000 files), fails the review with `FeedReadError` rather than reviewing part of the MR. An MR with no file entries at all is an error too. An entry flagged `too_large` or `collapsed` carries no inline diff: it is logged as a warning and reviewed as a header-only file.
  - **Summary Comments:** Managed via `GET/POST/PUT /merge_requests/{number}/notes`. Searches for `<!-- prxref-summary -->` and updates existing note via `PUT` if found.
  - **Inline Comments:** Posted as discussions via `POST /merge_requests/{number}/discussions` with text position references (`base_sha`, `start_sha`, `head_sha`, `new_path`, `new_line`). If position anchoring fails with HTTP 400 (e.g. line outside diff or obsolete context), it automatically falls back to posting a plain note via `POST /merge_requests/{number}/notes` formatted with `file: {path}\n\n{body}`.
  - **Thread List:** `GET /merge_requests/{number}/discussions`. Thread dedup needs `PRXREF_GITLAB_TOKEN` even on a public gitlab.com project: gitlab.com serves the MR and its diffs anonymously but answers anonymous `/notes` and `/discussions` requests with HTTP 401, so a tokenless review logs `discussion feed read was incomplete` and dedups against no threads.
  - **File Content:** `GET /repository/files/{url_encoded_path}/raw?ref={sha}` (path percent-encoded including slashes), best-effort, read with the same `PRIVATE-TOKEN` as everything else above (no extra scope). A non-2xx, oversize, or binary body returns `None` and is logged at debug, never a hard error.
- **Webhook Integration:**
  - **Event Header:** `X-Gitlab-Event` (normalized to `MergeRequestHook`)
  - **Accepted Actions:** `open`, `update`
  - **Signature Header:** `X-Gitlab-Token` (plain secret token) validated against `PRXREF_GITLAB_WEBHOOK_SECRET`.
- **Pinned Commit Range (Replay):** `GET /repository/compare?from={base_sha}&to={head_sha}&straight=false`. `straight=false` is the merge-base form; `straight=true` would diff the two commits directly. `unidiff` is deliberately not requested, so each entry's `diff` holds only its hunks, and the entries are rendered by the same header reconstruction as **Diffs** above. A response with `compare_timeout: true` raises rather than reviewing an incomplete file list. An entry flagged `too_large` or `collapsed` carries no inline diff: it is logged as a warning and reviewed as a header-only file. An empty range returns empty text, and an HTTP or transport error raises.
- **Description History (Replay):** none. The adapter cannot read an MR's description history, so a `--pr-url` replay here shows the MR's current title and description and logs a warning, and `--as-of` exits `2`. GitLab's system notes answered anonymous reads with HTTP `401`, and whether they carry the old description text is unverified.

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
    `nextPageStart` until `isLastPage` or the page cap. The summary lookup exits as soon
    as it finds the marker, so the common case is one request; exhausting the cap is an
    error rather than a silent stop, because a summary hidden past the window is how a
    re-review ends up posting a second one.
  - **Resolution:** read from whichever of `state == "RESOLVED"`, `threadResolved`, or
    `resolvedDate` the deployment's version exposes.
  - **File Content:** `GET {scheme}://{host}{context}/rest/api/1.0/projects/{key}/repos/{slug}/raw/{path}?at={sha}`
    (the `~{slug}` personal-repository form of `{key}` too), best-effort, read with the same
    token as everything else above (no extra scope). A non-2xx, oversize, or binary body
    returns `None` and is logged at debug, never a hard error.
- **Webhook Integration:**
  - **Event Header:** `X-Event-Key` (shared with Cloud; the two are told apart by event
    name and payload shape)
  - **Accepted Events:** `pr:opened`, `pr:modified`, `pr:from_ref_updated`
  - **Payload:** PR URL read from the first entry of `pullRequest.links.self` — note the
    capital `R`, and the list, both of which differ from Cloud.
  - **Signature Header:** `X-Hub-Signature` (HMAC-SHA256) validated against
    `PRXREF_BITBUCKET_WEBHOOK_SECRET`, the same secret Cloud uses.
- **Pinned Commit Range (Replay):** two requests under
  `{scheme}://{host}{context}/rest/api/1.0/projects/{key}/repos/{slug}`. First
  `GET …/commits/{head_sha}/merge-base?otherCommitId={base_sha}`, whose `id` is the fork point;
  then `GET …/diff?since={merge_base}&until={head_sha}` with `Accept: text/plain`, the raw diff,
  returned unmodified. That raw diff runs from whatever `since` names, with no merge-base step
  of its own, so the lookup is what makes it a three-dot diff. The spec lists the raw diff only
  as `text/plain; qs=0.1`, so the request names that type. If the merge-base lookup fails (an
  HTTP or transport error, or a response naming no commit), a warning is logged and the diff
  runs from `since={base_sha}`. That is still right whenever the base SHA is already the fork
  point, as a PR's recorded target commit usually is. An empty range returns empty text, and a
  failed diff request raises. Both endpoints come from the Data Center 9.4 REST reference and
  have **not been probed against a live Data Center**. No minimum version is claimed, but one
  Atlassian knowledge-base article reports that the path-less `/diff` returns 400 on some older
  versions.
- **Description History (Replay):** none. The adapter cannot read a pull request's
  description history (whether the `/activities` feed carries the old text is unverified),
  so a `--pr-url` replay here shows the current title and description and logs a warning,
  and `--as-of` exits `2`.

---

## 5. Azure DevOps Services & Server

Azure DevOps has no endpoint that returns a unified diff, so this is the one
adapter that builds its diff instead of downloading it. One adapter covers
Azure DevOps Services and Azure DevOps Server (on-prem): both speak REST
`api-version=7.1`, and they differ only in where the collection sits in the URL.

- **Forge Identifier:** `azure-devops`
- **Supported URL Shapes:**
  - `https://dev.azure.com/{organization}/{project}/_git/{repo}/pullrequest/{number}`
  - `https://dev.azure.com/{organization}/_git/{repo}/pullrequest/{number}` (short form,
    for a project named like its repository)
  - `https://{organization}.visualstudio.com/{project}/_git/{repo}/pullrequest/{number}`,
    with or without a `DefaultCollection` segment after the host, plus the same short form
  - `http(s)://{host}/{collection path}/{project}/_git/{repo}/pullrequest/{number}` for
    Azure DevOps Server, e.g. `https://{host}/tfs/DefaultCollection/{project}/_git/...`.
    A Server URL must name both the collection and the project. With a single segment
    before `_git` there is no telling which one it is, so the URL is rejected.
  - Percent-encoded names (`Web%20Platform`) are decoded. A query string or fragment
    (`?_a=files`) and a trailing route are ignored. The URL is normalized to the
    explicit-project form.
- **Scheme Note:** as on Bitbucket Server, the scheme of the URL you pass is kept, so
  an on-prem server on plain HTTP works.
- **Detection Order:** `detect_forge` asks this parser last. No other forge's pattern
  accepts the `/_git/{repo}/pullrequest/{number}` shape, so the position is defensive.
- **Authentication:** the first of these that is set wins.
  1. `PRXREF_AZURE_DEVOPS_TOKEN`: a personal access token, sent as Basic `:PAT` (empty
     user name). **Code (Read)** to review; **Code (Read & write)** to post.
  2. `SYSTEM_ACCESSTOKEN`: the Azure Pipelines job token, sent as `Bearer`. Pipelines
     does not hand it to scripts unless the step maps it (below).
  3. Neither: anonymous. A public project can be reviewed with no token at all.
     Posting always needs one.

  Every request sends `X-TFS-FedAuthRedirect: Suppress`, so an unauthenticated
  request gets a plain `401` rather than a sign-in page. A `203` or a non-JSON body is
  refused with an error that names `PRXREF_AZURE_DEVOPS_TOKEN`.
- **Azure Pipelines:** map the job token into the step, and grant the project's
  **Build Service** identity **Contribute to pull requests** on the repository so it
  can post. `System.CollectionUri` ends with a `/` and covers both Services and Server:

  ```yaml
  steps:
    - script: >-
        uvx prxref review --pr-url
        "$(System.CollectionUri)$(System.TeamProject)/_git/$(Build.Repository.Name)/pullrequest/$(System.PullRequest.PullRequestId)"
      env:
        SYSTEM_ACCESSTOKEN: $(System.AccessToken)
        PRXREF_LLM_BASE_URL: $(PRXREF_LLM_BASE_URL)
        PRXREF_LLM_MODELS: $(PRXREF_LLM_MODELS)
        PRXREF_LLM_API_KEY: $(PRXREF_LLM_API_KEY)
  ```

  Run it as a build-validation pipeline (a branch policy), which is what sets the
  `System.PullRequest.*` variables.
- **API Endpoints & Behavior:**
  - **Base URL:** `{scheme}://{host}{collection}/{project}/_apis/git/repositories/{repo}`,
    always project-scoped (the organization-level routes refuse anonymous reads), with
    `api-version=7.1` on every request.
  - **Metadata:** `GET {base}/pullrequests/{number}`. Branches come from
    `sourceRefName`/`targetRefName` without `refs/heads/`, and SHAs from
    `lastMergeSourceCommit`/`lastMergeTargetCommit`. The author is
    `createdBy.displayName`, because the unique name is null on anonymous reads.
  - **Diffs:** rebuilt locally.
    `GET {base}/diffs/commits?baseVersion={target sha}&targetVersion={source sha}&diffCommonCommit=true`
    lists the changed files against the merge base, which is the PR's own view (three
    dots). It is paged 1000 entries at a time until `allChangesIncluded`, and more
    than 50 pages is an error, not a partial review. Contents come from
    `GET {base}/blobs/{objectId}?$format=octetstream`, eight at a time, and `difflib`
    renders a git-style unified diff, including `\ No newline at end of file`, that
    `git apply` accepts. Azure DevOps detects renames itself, and a pure rename
    fetches nothing. A binary file (by extension, or a NUL byte in its first 8000
    bytes) renders as `Binary files … differ`. Content is capped at 512 KiB per blob,
    300 files and 16 MiB per diff; a file past a cap is listed without hunks, with a
    warning. A blob that returns `404` or `410` is listed without hunks too, but any other
    failed blob fetch fails the review rather than silently emptying a file. The
    change list comes from the Diffs API rather than the PR's iterations because the
    iterations list is not readable anonymously, even on a public project.
  - **Summary Comments:** a PR-level thread with status `closed`, created with
    `POST {base}/pullrequests/{number}/threads`. A re-review finds its earlier summary
    by the `<!-- prxref-summary -->` marker and edits it with
    `PATCH {base}/pullrequests/{number}/threads/{thread}/comments/{comment}`. When the
    thread list cannot be read, nothing is posted, so a failed lookup never produces
    a second summary.
  - **Inline Comments:** one thread per finding, with status `active` and a
    `threadContext` carrying the `/`-prefixed `filePath` and the line in
    `rightFileStart`/`rightFileEnd`. When the PR's iterations are readable,
    `pullRequestThreadContext` pins the thread to the latest iteration's
    `changeTrackingId` for that file; otherwise it is left out. A 4xx on one comment
    is skipped with a warning.
  - **Thread List:** `GET {base}/pullrequests/{number}/threads` returns every thread in
    one response. System threads (votes, pushes, status changes) and deleted threads
    are skipped. A thread counts as resolved when its status is `fixed`, `wontFix`,
    `closed` or `byDesign`.
  - **Prune:** a stale prxref inline thread is removed by deleting its root comment,
    `DELETE {base}/pullrequests/{number}/threads/{thread}/comments/{comment}`, matched
    by the attribution marker. The summary thread and human replies are never touched.
  - **File Content:** `GET {base}/items?path=/{path}&versionDescriptor.version={sha}&versionDescriptor.versionType=commit&download=true`,
    best-effort, read with the same token as everything else above. A `404`, an
    oversize (512 KiB) body, or a binary body returns `None` and is never a hard error.
- **Thread statuses and "Check for comment resolution":** inline threads are posted
  `active`, like an unresolved inline comment on every other forge. So a branch policy
  that requires comment resolution holds the PR until someone resolves prxref's
  threads, which is what GitHub's "require conversation resolution" rule already does.
  The summary is posted `closed`, so it never blocks a merge.
- **Webhook Integration:** Azure DevOps service hooks.
  - **Detection:** there is no event header. The request is recognized by its JSON
    body (`publisherId` is `tfs`), and only when none of the other forges' event
    headers is present.
  - **Accepted Events:** `git.pullrequest.created` and `git.pullrequest.updated`, and
    only while `resource.status` is `active`. A completed or abandoned PR, like any
    other event, is acknowledged with `202` and not reviewed.
  - **Payload:** the PR URL is `resource.repository.webUrl` (else `remoteUrl` without
    its `user@` prefix) plus `/pullrequest/{resource.pullRequestId}`.
  - **Authentication:** HTTP Basic. The password is compared in constant time with
    `PRXREF_AZURE_DEVOPS_WEBHOOK_SECRET`, and the user name is ignored. An unset secret
    rejects every Azure DevOps webhook with `401` unless `PRXREF_ALLOW_UNSIGNED=1`.
    Setup: [Azure DevOps service hooks](deploy.md#azure-devops-service-hooks).
- **Known Limitations:**
  - CRLF files lose their `\r` in the rendered diff. The diff parser reads lines
    without their terminators on every forge.
  - A path containing a tab or a newline cannot be written in a unified diff, so such
    a file is skipped with a warning.
  - Inline-comment line numbers can drift in a file that uses a form feed or a Unicode
    line or paragraph separator inside a line.
  - `difflib` does not promise a minimal diff on pathological files. Its output is
    still self-consistent, and `git apply` accepts it.
  - Azure DevOps exposes no file modes here, so every file is mode `100644`.
- **What is tested live:** reviewing a public Azure DevOps Services project with no
  token. The write paths (summary, inline threads, prune), PAT and `SYSTEM_ACCESSTOKEN`
  authentication, and the service-hook payload are covered by unit tests against
  recorded API shapes, but **have not been exercised against a live server**. Azure
  DevOps Server is parsed and authenticated the same way but is **untested**. It needs
  a release that accepts REST `api-version=7.1` (2022.1 or later, going by Microsoft's
  API version table).
- **Pinned Commit Range (Replay):** the **Diffs** path above with both ends pinned as
  commits:
  `GET {base}/diffs/commits?baseVersion={base_sha}&baseVersionType=commit&targetVersion={head_sha}&targetVersionType=commit&diffCommonCommit=true`,
  paged with `$top=1000`/`$skip`, then `GET {base}/blobs/{objectId}?$format=octetstream`
  for the file contents. `diffCommonCommit=true` is the merge-base ("three-dot")
  form: the list runs from the merge base of the two commits to `head_sha`, and each
  file's old side is its blob at that merge base. `false` would diff the two commits
  directly and so also list whatever changed on the base after the fork. Given a PR's
  own target and source commits, the result is the PR's diff. There is no raw text to
  return unmodified: the diff is rebuilt as for **Diffs**, so it has no `index` lines or
  function names after `@@`, every mode is `100644`, and a `similarity index` line
  appears only for a pure rename. The same budgets apply: 512 KiB per blob, 300 files
  and 16 MiB per diff, and a file past a cap, or whose blob is gone (`404` or `410`), is
  listed without hunks, with a warning. An empty range returns empty text, which replay
  reports as an error run. An HTTP or transport error raises, and so do a non-JSON
  listing, any other failed blob fetch, and a listing longer than 50 pages. While the
  adapter was designed, a prototype of this method returned a PR's own diff from a
  public Azure DevOps Services project, and a probe there saw `true` leave out files
  that `false` listed. On 2026-09-23 the shipped method was run live, read-only and with
  no token, against a public Azure DevOps Services project. Given a PR's own target and
  source commits, it returned the adapter's diff of that PR byte for byte, also for two
  PRs whose target had gained a commit since they forked: for those, its file list
  matched git's three-dot `target...source` diff, and the `false` listing named extra
  files that it left out. A range whose two ends are the same commit returned empty
  text. Beyond that run, its tests use recorded response shapes. Azure DevOps Server is
  untested, as above.
- **Description History (Replay):** none. The adapter cannot read a pull request's
  description history, so a `--pr-url` replay here shows the current title and
  description and logs a warning, and `--as-of` exits `2`.
