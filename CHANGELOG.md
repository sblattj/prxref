# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.12.0] — 2026-09-04

The context release. A real-PR audit of 0.11.0/0.11.1 — two review passes over
ten Bitbucket Server PRs — produced a twelve-issue bundle dated 2026-09-03/04,
and this entry answers all of it. Four of the eleven verified findings were
wrong for the same reason: the worker was reasoning about code it could not
see, so it now receives the versions of the libraries a chunk imports and the
definitions of the symbols it references. Around that sit five new
deterministic gates that drop a claim the diff itself contradicts, a systemic
sweep that finally sees deletions and the PR's own discussion, a stable
finding order with a `sampling` record on every run, a run-lifetime cache for a
model the provider has taken away, and machine-readable CLI output.

Inbox issue numbers below refer to `docs/issues/inbox-2026-09-04/`, not to
GitHub issues.

### Added

- **Workers see the versions of the third-party packages a chunk imports**
  (inbox issue 01), resolved from the nearest `package.json`, `pyproject.toml`,
  `go.mod`, or `Cargo.toml` at the PR head, so a review no longer guesses which
  library major the code runs against. The audit's headline false positive was
  Effect 3 semantics asserted against an Effect 4 repo, with a suggested fix
  that would have created the bug it alleged.
- **Workers see the definitions of same-file symbols a chunk references** whose
  declaration sits outside the rendered hunk (inbox issue 02), so a finding is
  no longer inferred from an identifier's name alone. Both blocks are best
  effort, capped (40 entries / 6 lines / 8000 chars / 512 KiB), fetched at most
  once per file per run, and a forge that cannot serve file content reviews
  exactly as before.
- **Forge adapters can serve a file's content at a commit** — a new optional
  `get_file_content(ref, path, *, sha)` on all four adapters (GitHub, GitLab,
  Bitbucket Cloud, Bitbucket Server), read-only and best effort, using the token
  already configured for reviews. No new environment variable.
- **A deterministic release-shaped-PR check** (inbox issue 10): a PR that is at
  least 80% release machinery — manifests, changelogs, lockfiles, changesets —
  yet still touches source flags every offending path, with no extra LLM call.
  The body ends `(deterministic check, no model)`.
- **`prxref review --format {text,json}`** (inbox issue 08). `json` prints
  exactly one JSON object to stdout — verdict, active and dropped findings,
  chunk and token counts, posted — for scriptable, diffable output. Default
  remains `text`.
- **Every review result carries a `sampling` record** (inbox issue 04) —
  temperature, seed, and the model chain — including on a failed or empty-diff
  run, and on the `run/start` trace event, so a report says which knobs were
  actually in force.
- **New file status `copied`** (inbox issue 03), rendered back into the worker
  prompt as `copy from` / `copy to` so the model can tell a copy from a move.
- **`docs/quality.md`**, the single reference for the deterministic checks, the
  eleven quality passes, and every `drop_reason` string; plus
  `docs/systemic-sweep.md` for the sweep's digest classes and caps.

### Changed

- **Five new deterministic gates run before posting**, all unconditional and
  all named in the run record: `apply_manifest_claim_check`,
  `apply_settled_thread_suppression`, `apply_removal_claim_check`,
  `apply_hedge_gate`, and the decorating `apply_containment_note`. See
  `docs/quality.md` for the full order and reasons.
- **The systemic sweep now sees deleted guards** (inbox issue 06): a removed
  numeric limit constant or validator/sanitiser definition is classed
  `guard-removal` and admitted to the digest ahead of noisier matches, so a PR
  whose only risk is a deletion no longer digests to nothing.
- **The sweep prompt carries the PR's existing review discussion** (inbox issue
  06), so it stops re-raising subjects the team already argued out. Existing
  threads are fetched once per review, before the review units run rather than
  after them, and still after the stale-inline prune.
- **The worker prompt demands more of a finding**: a claim that turns on an
  unlisted library version or an unshown definition caps confidence at 0.5 and
  is phrased as a question (inbox issues 01, 02); a claim conditioned on a
  precondition the diff never established must be omitted or filed as a
  question (inbox issue 05); and a throw, panic, crash, or unhandled-rejection
  claim must name its containment boundary (inbox issue 07).
- **`--no-post` and `-v` now print the findings** in text mode — every active
  finding's file, line, title, and body, plus dropped findings with their drop
  reason (inbox issue 08).

### Fixed

- **Findings are emitted in a stable `(file, line, title)` order** (inbox issue
  04), and the error and inline-comment caps break ties by finding content
  instead of by whichever worker answered first. The audit ran the same commit
  twice and got 7 findings then 2 — the one that vanished was the security
  finding. Documented in `docs/llm.md` and the README: temperature 0 and
  `PRXREF_LLM_SEED` are sent but do **not** make a review bit-reproducible.
- **A `copy from` / `copy to` diff header is parsed as a copied file** (inbox
  issue 03), git or Bitbucket Server `src://`/`dst://` form, instead of being
  mistaken for a rename — and a finding claiming a named path was removed is
  dropped when that path is still present after the PR lands
  (`claims removal of a path present in the post-image: <path>`). A claim about
  a file that really was deleted still posts.
- **Self-hedged findings are dropped before the confidence floor** (inbox
  issues 05, 11) — "If X still leases a client", "unless the backfill already
  ran", "I cannot verify" — with `drop_reason` `hedged: "<matched phrase>"`. A
  hedged finding no longer consumes an error-cap slot ahead of a proven one.
- **A `package.json` finding anchored on the wrong dependency is dropped**
  (inbox issue 12) with `anchor mismatch:`, and one that calls a `devDependency`
  a runtime dependency (or the reverse) with `section mismatch:`. Line
  realignment on a manifest no longer lets the section word `dependencies`
  outrank the package name and drag a correct comment onto its neighbour.
- **A finding that re-litigates a settled thread is dropped** (inbox issue 06)
  with `settled in thread: <author>`, regardless of whether it could be anchored
  to a line. A resolved thread still settles its subject.
- **A "this throws" finding that never names its containment boundary is
  flagged inline** (inbox issue 07) with `[containment boundary not stated]`, so
  a correct-but-underscoped finding cannot read as a smaller bug than it is.
- **A model that reports itself permanently unavailable is skipped for the rest
  of the run** (inbox issue 09) — deprovisioned, renamed, unsupported — instead
  of being retried on every chunk and the sweep, with one warning the first time
  it happens. Applies to both the openai-compat and litellm backends; only a 4xx
  qualifies, never a 5xx.

## [0.11.1] — 2026-09-02

### Fixed

- **The sweep digest reaches the four classes the 2026-09-02 re-audit found
  missing (#29 residual, PR #38).** Migration-ddl-matched files render their
  full added content (absence of RLS becomes a checkable fact); an added
  lockfile coexisting with another lockfile or a `packageManager` pin emits a
  deterministic repo-config note; `setInterval`/`setTimeout`-style loops are a
  digest class so no-cap polling is visible; and within the per-file line cap,
  must-see classes (entry points, secrets, auth checks) admit ahead of fill
  classes, so a secret can never be capped out by console-log noise. Files of
  60 or fewer added lines render full content within a 30% budget share.

## [0.11.0] — 2026-09-02

The recall release. The 2026-09-01 re-audit of v0.10.1 found precision and
mechanics fixed but recall weak (27% on the big PR's known bugs) and one
anchor shape surviving; all five findings are addressed here.

### Added

- **A systemic sweep pass (#29, PR #36).** After the chunk workers, one more
  single-shot call reviews a deterministic digest of the WHOLE PR — the file
  list, hunk headers, and the added/removed lines matching six high-signal
  patterns (entry points, env/secret refs, auth checks, error-swallows,
  migration DDL, console/logs), capped per-file and inside the chunk token
  budget. It hunts the classes no single chunk seat can see: missing auth on
  entry points reaching paid APIs, secrets in client-exposed variables,
  swallowed errors in billing/persistence paths, migrations missing RLS or
  backfills, destructive ops without guards. Sweep findings flow through the
  full pipeline; duplicates of chunk findings drop with
  `duplicate of chunk finding` — after the quality gate, so a sub-floor
  chunk finding cannot suppress its higher-confidence sweep twin. The sweep
  counts as one review unit in coverage accounting and in the partial banner.

- **Timed-out chunks retry once with zero context lines (#29, PR #36).**
  Deadline overruns are dominated by prompt prefill (the response-side
  hypothesis was investigated and refuted — truncation already degrades
  gracefully as of 0.10.0), so a timed-out chunk retries once with
  `context_lines=0` before failing.

- **The partial-review banner names the failed chunks' files (#31, PR #35).**
  `> - chunk of 4 files (a/1.py, b/2.py, c/3.py, +1 more): LLMError: timeout`
  — file paths verbatim, reason redacted, identical chunk+reason pairs
  collapse, cap semantics kept.

- **Severity groups bind on shared rare code tokens (#30, PR #34).**
  Findings phrased differently but sharing a rare code token
  (`vimeo_code`, `filterByFormula`) now join the same severity group —
  with an over-merging guard: same file, or both titles in a common
  problem-class family. The #18 title-equality rule is unchanged.

### Fixed

- **A line cited in the finding's own body outranks a drifted line field
  (#28, PR #33).** Live shape: anchored at sync.ts:15 while the body said
  "line 553" — the actual code was at 553. Own-file `path:line` and prose
  `line N` citations, corroborated by the hunk's evidence tokens, now win
  over the model's `line` field; a corroborated citation also promotes a
  file-level finding to its real anchor.

- **Malformed finding locations are dropped, not rendered (#32, PR #35).**
  A `file` field matching no path of the diff (empty, non-path shape,
  invented) drops with `malformed location: '<file>'` into the
  dropped-findings audit section, instead of rendering `- 🟧 `package.:—``.

## [0.10.1] — 2026-09-01

### Fixed

- **Anchor re-resolution prefers token-bearing hunks (#19 follow-up, PR #27).**
  The v0.10.0 pass was much better (14/19 on-target live) but five shapes
  still drifted — including a 391-line miss into a hunk sharing zero claim
  tokens and an anchor on a blank line. A hunk must now corroborate with a
  non-generic evidence token, ties prefer the most-specific token's hunk
  before nearest, and blank/context anchors never survive a token-bearing
  line.

## [0.10.0] — 2026-09-01

The audit release. Four fixes, every one traced to a live finding by the
2026-08-31 review-the-reviews audit and verified against real PRs.

### Fixed

- **Truncation advances the fallback chain (#10, PR #23).** A completion cut
  off by `max_tokens` comes back HTTP 200 with `finish_reason: "length"` and
  returned as success, so the model chain never advanced. Truncation is now a
  per-model failure inside the attempt loop; only chain exhaustion returns
  the truncated result, as a last resort the reviewer already handles.

- **Inline anchors are re-resolved against the diff (#19, PR #25).** A cited
  line that was merely *an* added line passed alignment even when it belonged
  to the wrong hunk — accurate claims anchored 10–420 lines from their code.
  Anchors now snap within a 5-line tolerance, are re-resolved by content
  overlap when refuted, and post file-level (`line=0`) when unresolvable.

- **Consistent severities for the same pattern (#18, PR #22).** Per-chunk
  workers decided severity in isolation, so one bug class could arrive as
  error in one file and note in a sibling. A deterministic pass now
  normalizes severity across findings sharing a normalized title — same file
  or not — before the quality gate.

### Changed

- **Reviews are reproducible by default (#11, PR #24).** The effective
  default sampling temperature is now `0.0` and is sent when the operator
  sets nothing (an explicit `PRXREF_LLM_TEMPERATURE` still wins), and a new
  `PRXREF_LLM_SEED` (int >= 0, unset = omitted) seeds OpenAI-compatible
  requests. Identical diff + identical config now aims for identical
  findings, which is what `PRXREF_FAIL_ON=error`'s exit code promises.


### Fixed

- **The GitHub prune pass deleted nothing (0.9.0).** The delete route was
  built from the listing URL, which carries the pull number
  (`/pulls/N/comments/{id}`); the delete route lives outside that namespace
  (`/pulls/comments/{id}`), so every DELETE 404'd while the run reported
  partial success. Found live against a real PR; the mocked-session test
  now pins the endpoint's shape.

## [0.9.0] — 2026-08-31

The honesty release. What stands on a PR after a re-review now equals the
latest review, and the summary keeps the promises its findings list makes.

### Added

- **Stale-inline pruning (#17).** A re-review updates the summary in place,
  but the previous run's inline comments stayed standing — an Approved
  summary could sit above stale ERROR comments from an earlier,
  nondeterministic run. Forges may now implement
  `prune_inline_comments(ref) -> int` (GitHub does; the others follow): the
  orchestrator calls it before reading threads, so the dedup cannot suppress
  this run's findings against comments that are about to disappear. Only
  comments carrying the attribution marker (`Reviewed by prxref`) are ever
  deleted — a human's comment is not a candidate — and the whole pass is
  best-effort: a 403 from a different identity's comment, or an unreadable
  feed, logs and continues rather than aborting the review.

- **Inline accounting in the summary (#16).** The summary itemizes every
  active finding, but the inline pass could silently post fewer — the
  `PRXREF_MAX_INLINE_COMMENTS` cap, a forge that rejected the anchor (a 422
  on a line outside the diff), or a failed batch — with nothing on the PR
  saying so. When fewer inline comments land than findings are itemized, the
  summary is re-posted (update-in-place) with a line naming the shortfall and
  its reasons, e.g. `Inline comments: 13 of 21 findings (6 over the
  15-comment cap · 2 anchors rejected by the forge).` A failed batch is
  disclosed the same way.

### Changed

- **The inline slice is severity-ordered.** Findings previously became
  comments in chunk order, so the cap and rejected anchors cost the run
  whatever sat at the tail — including error-severity findings. The slice is
  now error-first, then warning, then outofscope, confidence-descending
  within each.

- `post_inline_comments`'s return value — the number actually posted — was
  computed by the forges and discarded by the orchestrator; it now drives
  the accounting line and the posted-count trace event.

## [0.8.0] — 2026-08-31

The severity-rename release. The blue bucket is now called `outofscope`.

### Changed

- **The third severity is renamed `note` -> `outofscope`** (owner decision). The
  class contents are unchanged — minor findings: misleading naming, TODOs
  without context, dead code the diff adds — as are the marker (🟦), the
  ordering (error-first, outofscope last) and the unknown-severity fallback.
  Every surface moved together: the worker prompt vocabulary, the quality-gate
  `SEVERITIES` set, the summary template placeholder (`{outofscope_count}`),
  both renderers, and the tests. Posted comments now read
  `🟥 N error · 🟧 N warning · 🟦 N outofscope`.

## [0.7.0] — 2026-08-31

The severity-marker release. Every posted comment now carries the severity
class at a glance: 🟥 error, 🟧 warning, 🟦 note.

### Changed

- **Severity markers in every posted comment.** The squares existed only in
  `formatter.py`, which nothing imports — the live renderer posted 🚨/⚠️/📝 on
  inline comments and a plain `0 error · 0 warning · 0 note` counts line on
  summaries. `_SEVERITY_EMOJI` is now `_SEVERITY_MARKERS` mapping to
  🟥/🟧/🟦 (unknown severities read as 🟦 note), and both the shipped
  `prompts/summary.md` counts line and the fallback template carry the same
  markers. Summary counts lines read `🟥 N error · 🟧 N warning · 🟦 N note`,
  findings bullets are prefixed with their square, and each inline comment
  body is prefixed `🤖 🟥 **[ERROR] …` / `🟧 **[WARNING] …` / `🟦 **[NOTE] …`.

## [0.6.0] — 2026-08-28

The duplicate-comment release. Three ways prxref could post the same thing twice,
and the test command the repo documented but could not run.

### Changed

- **The dev tools moved from a project extra to a PEP 735 dependency group, so
  the test command is now the bare `uv run pytest`.** `pytest`, `pytest-cov` and
  `ruff` lived in `[project.optional-dependencies] dev`, and `uv run` never
  installs a project *extra* — only `--extra dev` does. On a cold checkout the
  documented-everywhere-else `uv run pytest` therefore exited 2 with
  `Failed to spawn: pytest / No such file or directory (os error 2)`, printed
  immediately after a cheerful `Installed N packages`, which reads as a broken
  virtualenv rather than a missing flag. uv installs the default `dev` group
  automatically on both `uv run` and `uv sync`, so `[dependency-groups] dev`
  removes the trap at its source rather than documenting around it. Every
  surface dropped the flag with it: `uv sync`, `uv run pytest`,
  `uv run ruff check src tests` in CI, `CONTRIBUTING.md`, `CLAUDE.md` and
  `HANDOFF.md`.

  **`pip install prxref[dev]` no longer works.** Dependency groups are a
  lockfile-and-workspace concept: they are never written into wheel or sdist
  metadata, so the built distribution now carries no `Provides-Extra: dev` and
  `prxref[dev]` resolves to plain `prxref`. That failure is quiet by design on
  pip's side: the install exits 0 having shipped none of the tools (`uv pip`
  warns "does not have an extra named `dev`"; pip installing the wheel directly
  says nothing at all). Contributors clone the repo and run `uv sync`; with pip,
  install the tools directly (`pip install pytest pytest-cov ruff`). The
  `litellm` extra is untouched — that one is a genuine runtime extra for users,
  and `pip install 'prxref[litellm]'` still works.

### Fixed

- **The GitHub Actions review workflow reviewed only part of a PR and said so in
  a banner nobody was meant to see in normal operation.**
  `.github/workflows/prxref-review.yml` set no `PRXREF_LLM_MAX_TOKENS`, so every
  worker call ran on prxref's built-in default of 4096. The model configured
  there is a reasoning model, and a reasoning model draws its hidden reasoning
  trace from the *same* completion budget as the answer — so the budget was
  spent before the findings JSON began, the provider returned
  `finish_reason=length`, and prxref counted that chunk as failed. Nothing about
  the run looked wrong: HTTP 200, plausible usage numbers, exit 0. On a real PR
  it reviewed 2 of 4 chunks and posted "Findings may be incomplete"; the same PR
  reviewed locally at 32000 completed 4/4. The workflow now passes
  `PRXREF_LLM_MAX_TOKENS: ${{ vars.PRXREF_LLM_MAX_TOKENS || '32000' }}` —
  operator-tunable through a repository variable like its neighbours, but with a
  literal fallback they do not need, because an unset variable renders as the
  empty string and prxref falls back to the 4096 that caused this. The budget is
  per worker chunk, not per run, so raising it widens each chunk's headroom
  rather than one large request.

- **A retry could post the same review comment four times.** All four forge
  adapters built their `requests.Session` with the write verbs in
  `allowed_methods` — `POST`, `PUT`, `DELETE`, plus `PATCH` on GitHub — against
  a `status_forcelist` of `[429, 500, 502, 503, 504]` with `total=3`. urllib3
  retries underneath the `requests` adapter, so what it re-sends is the whole
  request: a comment `POST` the forge had already committed, whose `2xx` was
  then lost on the way back — a 502 or 504 from a proxy in front of the API, or
  a read timeout — was sent again, up to three more times, and the PR ended up
  carrying the same summary or the same inline finding two, three, or four
  times. Nothing downstream could notice, because from the client's side a
  duplicated comment is a successful POST. `allowed_methods` is now
  `GET`/`HEAD`/`OPTIONS` on all four adapters: reads still retry, writes are
  attempted exactly once. A write that fails is left to the caller, which
  already logs a failed post and finishes the run; a duplicated comment needs a
  human to delete it. The one case given up is 429, where replaying a write
  would in fact have been safe — the server is stating it did not process the
  request — but `Retry.is_retry` tests the method before it consults
  `status_forcelist`, so no single policy can retry a `POST` on 429 while
  holding it back on 502, and the safe half of that pair is the one worth
  keeping. Connection errors are unaffected and still retry for every verb:
  urllib3 gates only its read-error path on the method, and a connection that
  was never established carried no write to duplicate.

- **Every re-review posted a second summary comment, on all four forges.**
  Each adapter's `post_summary` is meant to find its own previous summary by
  the hidden `<!-- prxref-summary -->` marker and update that comment instead
  of posting beside it — but nothing ever put the marker into the body.
  `orchestrator._render_summary` and `prompts/summary.md` render the verdict,
  the findings and the attribution and no marker, so the lookup matched
  nothing on the second run, and every run after the first left another
  summary on the PR. The three adapters that looked were looking for something
  that was never written; the fourth did not look at all (below). The marker is
  now stamped by the adapter that searches for it — `forges/base.py` owns
  `SUMMARY_MARKER` and an idempotent `with_summary_marker()`, so a body that
  already carries one (a caller's, or a template's) is left alone.
- **Bitbucket Cloud never looked for an existing summary.** Its `post_summary`
  was an unconditional POST, with no lookup of any kind, so it duplicated on
  every re-review even once the marker was present. It now does what the other
  three do: walk the comment feed for a top-level comment carrying the marker
  and `PUT` over that one. Inline comments quoting the marker are skipped, as
  are deleted comments — Bitbucket keeps those in the feed with the body
  blanked, and an update aimed at one lands where nobody can read it.
- **A busy PR hid the existing summary past the end of the read.** The comment
  and activity walks all stopped early, each in its own way: Bitbucket Cloud
  and Data Center capped at 5 pages of 100, GitLab read one page of 50 with no
  loop at all, and both GitHub reads went out unparameterised — one default
  page of 30. Past that window a summary simply did not exist as far as the
  adapter was concerned, so `post_summary` missed its own marker and posted a
  duplicate, and `list_threads` under-reported the threads that suppress
  already-discussed findings. Every walk now pages to the end of the feed at
  100 per page, stopping the moment the marker turns up — the common case is
  still one request — and the marker is searched for page by page rather than
  after collecting the whole feed. The bound is 50 pages rather than 5, and
  reaching it is now an error rather than a silent short read. GitLab's note
  walk asks for oldest-first explicitly: GitLab lists notes newest-first, and
  offset paging over a feed that grows at the front steps over entries, which
  is the same miss by another route.
- **A feed read that failed was treated as "no summary exists".** Bitbucket
  Data Center caught `RequestException` and set `existing = None`; GitLab
  caught it and left `existing_note_id` unset; GitHub branched on
  `if list_resp.ok:` with no else. All three then fell through to the POST — so
  a rate-limited or briefly unreachable forge turned a re-review into a second
  summary on someone's PR. An incomplete read now raises `FeedReadError`
  (`forges/base.py`) and no summary is posted at all. This is a deliberate
  trade: a summary that failed to post is recoverable by re-running, and the
  orchestrator already logs it and reports `posted=False`, while a duplicate
  comment on a PR is not recoverable without a human deleting it. `list_threads`
  makes the opposite trade on purpose — its output only feeds best-effort
  dedup, and the orchestrator substitutes an empty list for any exception, so
  raising would throw away the pages that were read. It keeps them and logs a
  warning naming how many it got, rather than under-reporting in silence.

## [0.5.0] — 2026-08-28

The self-hosted Bitbucket release. Bitbucket Server / Data Center was the one
supported forge family with no self-hosted path; deployments that needed it ran
a hand-maintained overlay on top of a tagged release.

### Added

- **Bitbucket Server / Data Center forge** (`bitbucket-server`). Self-hosted
  Bitbucket previously had no path at all: the Cloud adapter pins itself to
  `bitbucket.org`, while GitHub and GitLab each covered their self-hosted
  deployment. Data Center is a different API rather than the same one on
  another host — `/rest/api/1.0`, project keys instead of workspaces, an
  activity feed instead of a comment list, `start`/`limit` paging — so it is a
  fourth adapter under the existing `Forge` Protocol, and nothing downstream of
  `forges/base.py` changed. Handles project and personal (`~slug`)
  repositories, a deployment context path, anchored inline comments, and the
  version field Data Center requires when updating a comment. `detect_forge`
  tries Cloud before Server, though the two parsers are disjoint — Cloud pins
  `bitbucket.org`, Server requires a `/projects|users/KEY/repos/REPO/` path — so
  no URL matches both and the order is defensive rather than load-bearing.
- **`PRXREF_BITBUCKET_SERVER_TOKEN`** (HTTP access token, falls back to
  `PRXREF_BITBUCKET_TOKEN`), **`PRXREF_BITBUCKET_SERVER_USER`** and
  **`PRXREF_BITBUCKET_SERVER_PASSWORD`** (basic-auth pair, used only when no
  token is set).

### Fixed

- **Bitbucket webhooks were broken for both products.** The receiver accepted
  only `pr:opened` / `pr:modified` — Bitbucket **Server** event names — while
  reading the PR URL from `pullrequest.links.html.href`, which is Bitbucket
  **Cloud**'s payload shape. So a genuine Cloud webhook was rejected as "not
  reviewable" (Cloud sends `pullrequest:created` / `pullrequest:updated`) and a
  genuine Server webhook produced no URL. Both dialects are now accepted and
  their payloads read correctly, `pr:from_ref_updated` included.
- **A Data Center PR's REST URL built a doubled API path.** The Server URL
  pattern captures whatever sits between the host and the `/projects|users/`
  route as the deployment context path, because Data Center is commonly
  reverse-proxied under one. Server is also the only forge here whose REST URL
  has the same path shape as its browse URL — the same route, one prefixed with
  `/rest/api/1.0` and the other not — so pasting a PR's REST URL into
  `prxref review` parsed happily with `/rest/api/1.0` captured as the context,
  and every request then replayed it in front of the adapter's own
  `/rest/api/1.0`: metadata, diff, activities and comments all went to
  `…/rest/api/1.0/rest/api/1.0/projects/…` and 404ed. Parsing now strips a REST
  prefix (`/rest/api/1.0`, other version numbers, and the `/rest/api/latest`
  alias, case-insensitively) off the captured context while keeping any genuine
  context underneath it, so `PRRef.url` is the browse URL a human can click and
  the API base is built exactly once. A REST URL for a personal repository,
  which names it the API way as `~slug`, likewise normalizes back to its
  `/users/slug` browse route. Webhooks never hit this: Data Center's payload
  carries the browse URL in `pullRequest.links.self[].href`.
- **A plain-HTTP Data Center deployment was silently retargeted to TLS.** The
  Server URL pattern accepts `http://` as well as `https://`, but normalization
  and the API base both wrote `https://` back unconditionally, so an
  `http://host:7990/projects/…` URL parsed happily and then sent every request
  — metadata, diff, activities, comments — to `https://host:7990/…`. That is
  not a hypothetical host: a Data Center standalone install serves plain HTTP on
  port 7990, so the out-of-the-box deployment shape was the one that broke, and
  it broke as a TLS handshake failure against a URL the operator never typed.
  `PRRef.url` now carries the scheme it was parsed with, and `_pr_url` reads it
  back out of `url` the same way the deployment context path is recovered, so an
  `http://` deployment stays on `http://` end to end and an uppercase `HTTP://`
  round-trips lowercased. Nothing changes for an `https://` URL. This makes the
  Server adapter deliberately unlike its three siblings, which all build their
  API base as `https://` whatever scheme they were handed; only Bitbucket
  Server ships a default install that is not on TLS.

### Changed

- `prxref review`'s hint for an unparseable PR URL now names the self-hosted
  deployments alongside the three cloud hosts, and says that the URL must keep
  the forge's own path shape.

## [0.4.0] — 2026-08-27

The second half of the on-prem field report: posting controls, the exit-code
policy, and chunk shaping.

### Added

- **`PRXREF_FAIL_ON`** (default `never`) — the exit-code policy for
  `prxref review`, from a field report running prxref over human-authored PRs
  where the review must stay advisory. `never` keeps the standing contract:
  the exit code never reflects findings. `error` exits 1 when the completed
  review carries an active error-severity finding; `any` exits 1 on any
  active finding. Under either non-`never` value a review that fails to
  complete also exits 1, so a gating lane cannot read a broken run as green.
  A value outside the vocabulary is a configuration error (exit 2) naming the
  legal values. The webhook daemon has no exit code and is unaffected.
- **`PRXREF_CHUNK_MAX_FILES`** (default `5`) caps the number of files placed
  in one review chunk, and **`PRXREF_CHUNK_CONTEXT_LINES`** (default `3`)
  bounds the context lines rendered around each change in the worker prompt.
  The file cap shapes placement like the token budget: once `PRXREF_MAX_CHUNKS`
  is reached and every chunk is full, an overflow file joins the smallest chunk
  past the cap rather than being dropped. Context can only be trimmed, never
  added — the forge's diff is the source — and `0` emits the changed lines
  only.
- **`PRXREF_POST_MODE`** (default `summary+inline`) selects what is written to
  the forge — `summary` never posts inline comments, `inline` never posts a
  summary on any path — and **`PRXREF_POST_VERDICT`** (literal `1`, default
  on) omits the verdict stamp from the posted summary when unset. Both flow
  through `load_config` → `orchestrate_review` → the posting block, and a dry
  run still posts nothing in any mode.

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

[Unreleased]: https://github.com/sblattj/prxref/compare/v0.12.0...HEAD
[0.11.1]: https://github.com/sblattj/prxref/releases/tag/v0.11.1
[0.11.0]: https://github.com/sblattj/prxref/releases/tag/v0.11.0
[0.10.1]: https://github.com/sblattj/prxref/releases/tag/v0.10.1
[0.10.0]: https://github.com/sblattj/prxref/releases/tag/v0.10.0
[0.9.0]: https://github.com/sblattj/prxref/releases/tag/v0.9.0
[0.8.0]: https://github.com/sblattj/prxref/releases/tag/v0.8.0
[0.7.0]: https://github.com/sblattj/prxref/releases/tag/v0.7.0
[0.6.0]: https://github.com/sblattj/prxref/releases/tag/v0.6.0
[0.5.0]: https://github.com/sblattj/prxref/releases/tag/v0.5.0
[0.4.0]: https://github.com/sblattj/prxref/releases/tag/v0.4.0
[0.3.0]: https://github.com/sblattj/prxref/releases/tag/v0.3.0
[0.2.0]: https://github.com/sblattj/prxref/releases/tag/v0.2.0
