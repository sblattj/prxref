# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Issue numbers in entries before 0.14.0 refer to the project's previous issue
tracker.

## [Unreleased]

### Added

- `PRXREF_REVIEW_DEPTH` (`review_depth` in `.prxref.toml`, `--review-depth`
  on the CLI): `standard` (the default, prompts unchanged) or `thorough`, an
  opt-in worker prompt that also asks for the minor points a maintainer would
  raise. On a 233-PR, 600-label benchmark with `glm-5.3` it raised recall from
  6.1% to 15.2% and cut strict precision from 71% to 46%, mostly with nits,
  posting about 3x the findings. A custom `worker.md` wins and logs a warning.
  The run record and `--format json` carry `review_depth`.

## [0.32.3] — 2026-10-04

### Fixed

- A finding whose `file` lost the token `json` (`package.` for
  `package.json`, `src/librustdoc//mod.rs` for `src/librustdoc/json/mod.rs`)
  or a leading directory is now repaired to the one diff path that explains
  it instead of being dropped as a malformed location. Zero or several
  candidates still drop (#94).

## [0.32.2] — 2026-10-03

### Fixed

- `prxref eval mine` now decides `accepted` for comments on a commit that a
  force-push replaced (#91), which left most labels of amend-and-force-push
  repositories `null` (93 of 122 on django). When the commented commit is not
  an ancestor of the final head, the PR's diff at that commit is compared with
  its final diff (`pulls/{n}/files`, read once per PR and only when needed).
  The lines within 3 lines of the comment (added or context) are anchors. An
  added anchor gone from the final diff, a new line the final diff adds within
  3 lines of a matched anchor (a fix that only inserts), or the file dropped
  from the PR is `true`; anchors found with nothing new near them is `false`;
  no anchor found, or a withheld patch, stays `null`. Ancestor cases are
  unchanged.
- The `[Unreleased]` link in this changelog compares from `v0.32.1`, and
  links for 0.31.0, 0.32.0 and 0.32.1 are added.

## [0.32.1] — 2026-10-03

### Fixed

- `prxref eval mine` no longer drops every PR once GitHub's rate limit is hit
  (#88). A 403 or 429 that carries `X-RateLimit-Remaining: 0`, a
  `Retry-After` header or a "rate limit" body is waited out (until
  `X-RateLimit-Reset`, or for `Retry-After`) with a WARNING, and the same
  request is retried; it is never a per-PR skip. A wait that cannot be told,
  exceeds 3700 s or recurs 3 times for one request stops the walk: the PRs
  mined so far are written and `mine.json` records `"stopped": "rate_limit"`.
  The exit code stays 0.

### Changed

- `prxref eval mine` skips the comments fetch of a PR whose `review_comments`
  count is below `--min-comments` (#88), which saves a request per PR seen
  through `--until` and `--pr`.
- `mine.json` gains the key `stopped` (`null`, or `"rate_limit"`).

## [0.32.0] — 2026-10-03

### Added

- `prxref eval mine --until YYYY-MM-DD` bounds the merge date from above and
  reads candidates from the GitHub search API (#85), so an older window can be
  mined without paging through years of PRs. A query reaches at most 1000
  results; a wider window logs a warning.
- `prxref eval mine --reviewers maintainers` counts only comments by an
  OWNER, MEMBER or COLLABORATOR as labels.
- `prxref eval mine --pr N[,N...]` mines exactly the listed merged PRs.
- `mine.json` records `until`, `reviewers` and `pr_numbers`.

### Fixed

- `prxref eval mine` mines PRs whose base branch was later renamed or
  deleted (#85): the merge base falls back from `base.ref` to the repository's
  default branch, then to `base.sha`, before the commit is skipped.

## [0.31.0] — 2026-10-03

### Added

- `prxref eval verdict` decides whether a candidate setup beats the current
  one by more than run-to-run noise (#81). It takes repeated scored runs of
  each side (`--baseline`, `--candidate`), gates on the recall of one label
  severity (`--severity error`) or the micro recall, and calls the candidate
  `better` only when its mean gate beats the best baseline run while micro
  recall and unmatched AI findings per PR stay no worse than the worst one.
  It exits `1` when the candidate is not `better`, so CI can gate an upgrade.
  When every run was scored with `--precision`, the second guard is strict
  precision (no worse than the worst baseline run) instead of unmatched AI
  findings per PR, and the report says which guard it used. `--json PATH`
  also writes the verdict as `verdict.json`. Given one `eval campaign`
  directory per side, it compares the arms one by one and adopts (exit `0`)
  only when every rules arm is `better` and no no-rules arm is `worse`.
- `prxref eval campaign --cases PATH --arms TOML --out DIR` runs every arm
  of an arms file (`rules_file`, `scoped_rules`, `prompts_dir`, or rules
  mined per fold from the other folds' labels with `[arm.mine_rules]`)
  `--repeats` times as parallel, sharded `eval run` passes (`--jobs`,
  `--case-jobs`), retries rate-limited and failed cases with a capped pause
  (`--max-attempts`), scores every pass (with `--precision` when
  `--judge-model` is given), and keeps `campaign.json` and `progress.json`
  so `--resume` continues where it stopped. `--prxref VERSION|PATH` runs
  the passes with another prxref, installed once with `uv` (#81).
- `prxref eval score --precision` grades the active AI findings no label
  credited as `valid`, `nit`, `invalid`, `duplicate` or `unverifiable`, with
  one judge call per case, and reports strict and lenient precision in
  `score.json` (`metrics.precision` and each case's `precision`), in
  `score.md` (`## Precision`) and in `eval compare`. It requires
  `--judge-model` (#81).
- `prxref eval mine --repo OWNER/NAME --out DIR` builds an eval dataset from
  a GitHub repository's merged PRs, with the human review comments as
  labels: `cases.json`, `mine.json` (provenance and the dataset's sha256)
  and `severity-review.md`. `--judge-model` drafts each label's severity for
  a human to confirm, and `--rehash DIR` re-records the hash after the
  edit (#81).
- `prxref eval dashboard --campaign DIR` shows a campaign's live progress
  read-only, as a plain-text table with `--once` or as a local web page
  that polls `/status.json` (#81).
- `eval run` records `prxref_version` in `run.json` and keeps the unified
  diff each review read as `cases/<id>/diff.patch`, which the precision
  judge shows; the review's `--trace-dir` dump now includes that
  `diff.patch` too (#81).

## [0.30.1] — 2026-09-30

An audit of 0.30.0 against its nine issues' acceptance criteria and the
owner's design decisions found gaps in every one of them (#66, #67, #68,
#69, #70, #71, #73, #74, #75); this release closes them. **Several
defaults and behaviours change** — read the first section before
upgrading. Two review inputs that 0.30.0 shipped off are now **on by
default**: CI wiring (#66) and in-repo standards discovery (#68). The
matching-rules probe (#67), always on in 0.30.0, gains an `off` switch
and now also reads route tables by default. The eval harness pins all
three off, so environment and `.prxref.toml` values no longer leak into
eval runs. Stable finding ids are **always on** and their hash
now stems words (#71). Execution evidence now **drops** a finding it
contradicts instead of relabelling it (#69). PR metadata violations are
**summary notes, not findings**, and can live in their own rules file
(#70). `anchor_unverified` **no longer lowers confidence** (#74). The
config schema grows from 92 to 94 keys (`PRXREF_ROUTING_PROBE`,
`PRXREF_RULE_SCOPING`), one key is renamed with its old name kept as an
alias, and one is deprecated. The worker prompt template changed again
(#67), so prompt hashes move once more.

### Added

- **CI wiring follows runners and flags new targets (#66).** A CI step
  that runs `make <target>` or `npm run`/`yarn`/`pnpm run <script>` now
  counts as running a script that target's Makefile recipe or
  `package.json` script names, one hop deep, following make prerequisites
  within the root Makefile. A new Makefile target or `package.json`
  script that is a check and that no CI job runs is itself a candidate.
  `--spec` text now also drives the `spec`/`warning` severity switch, not
  only `--context-file`.
- **Route-table fetch for the matching-rules probe (#67).** With
  `routing_probe` on and a file reader, a chunk that adds a web-server or
  static-host rule also gets the conventional route-table files' route
  lines as a context block, so the probe can check inputs against routes
  the PR does not touch. A live eval pair covers the matching-rule failure
  mode: `case-004-spa-dotted-route` (must flag) and
  `case-005-spa-uuid-route` (must not). The eval harness pins the probe
  off, so the pair measures the model without the `## Matching rules`
  section rather than exercising it.
- **Failing evidence raises findings (#69).** A failing evidence item
  (non-zero exit) whose output names `path:line` in a file the PR changes
  raises a deterministic warning there citing the command and exit code,
  at most 10 per run, deduplicated against the model's findings. The
  `$ cmd` plain-text evidence format is recognised (it no longer renders
  as `$ $ cmd`). The summary's evidence note now lists each command with
  its exit code and the findings evidence dropped.
- **`commit_reference` works on every forge (#70).** GitLab, Bitbucket
  Cloud, Bitbucket Server / Data Center and Azure DevOps gained commit
  listing, so the check no longer skips with "no commit source" outside
  GitHub and Gitea.
- **A skipped metadata check is visible (#70).** The `PR metadata`
  summary section lists each configured check that could not run with its
  reason (`Skipped <check> check: <reason>`), and a metadata stage that
  fails outright shows a `Skipped metadata check` line instead of looking
  like a pass.
- **Reworded findings match stored verdicts across runs (#71).** A
  finding whose id misses the verdict store still matches an entry of the
  same file and rule whose recorded title it restates, reuses that id and
  takes its `refuted` drop. An unknown verdict label in the store logs a
  WARNING naming the id and is ignored; labels are case-normalised.
- **Won't-fix threads keep suppressing (#73).** An Azure DevOps thread
  closed as `wontFix` or `byDesign`, or a thread on any forge whose text
  is an explicit human "won't fix" decision, keeps suppressing duplicates
  even when resolved or outdated. GitHub, GitLab and Azure DevOps find
  the decision in any reply of the thread; Bitbucket Cloud, Bitbucket
  Server and Gitea/Forgejo read it only from the comment the finding is
  matched against. The summary's thread-dedup line now
  reads "suppressed as duplicates of open or won't-fix threads", and the
  summary and the `thread_dedup` run record name the thread each
  suppressed finding matched.
- **Unverified "Also at" sites are listed, not dropped (#74).** They move
  to their own `Also at (unverified):` paragraph.
- **Rule scope syntax (#75).** `Applies to:` lines are accepted alongside
  `scope:`, and a section heading that names a language or artifact
  (`## Java module boundaries`) scopes the section without a scope line.
  A label whose cited rule names another kind of defect than the
  finding's title (a Javadoc finding under "Remove fields never read") is
  cleared, keeping the finding.

### Changed

- **CI wiring is on by default (#66).** `PRXREF_CI_WIRING` /
  `ci_wiring` now defaults to `on`; set it to `off` for the 0.30.0
  behaviour. Without a repository reader the "did not run" notice is
  logged at INFO when the value came from the default, and at WARNING only
  when `on` was set explicitly.
- **The matching-rules probe has a switch, on by default, and reads
  route tables (#67).** New key `PRXREF_ROUTING_PROBE` / `routing_probe`
  (`on` | `off`, default `on`, file key, no CLI flag). `on` keeps the
  worker prompt's `## Matching rules` section and, with a file reader,
  reads the conventional route-table files for a chunk that adds a
  web-server or static-host rule (see Added); `off` cuts the section out,
  leaving the worker prompt byte for byte the template without it, and
  reads nothing.
- **Standards discovery is on by default and no longer needs
  `PRXREF_REPO_CONTEXT=repo` (#68).** With a repository reader, standards
  excerpts are planned at every `PRXREF_REPO_CONTEXT` level. The per-chunk
  budget `PRXREF_CONTEXT_STANDARDS_MAX_CHARS` rises from 4000 to 6000, and
  the built-in document set gains `.github/SECURITY.md` and
  `.github/CONTRIBUTING.md`.
- **The eval harness pins the three new defaults off (#66, #67, #68).**
  `prxref eval run` forces `ci_wiring="off"`, `routing_probe="off"` and
  `context_standards_globs=[]` for every case, whatever the environment or
  `.prxref.toml` says, and records the pinned values in `run.json`.
  Because the probe is pinned off, eval worker prompts no longer carry the
  `## Matching rules` section that 0.30.0's eval runs did.
- **Execution evidence drops contradicted findings (#69).** The 0.30.0
  model-labelled downgrade is gone, along with the `Finding.evidence`
  field and its `"evidence": "contradicts"` label. Instead a deterministic
  pass drops a finding that claims a header is missing when an exit-0
  evidence item shows that header as a `Name: value` line for the
  resource the finding names, with drop reason
  `contradicted by execution evidence: <cmd>`.
- **PR metadata violations are summary notes, not findings (#70).** They
  render in a `PR metadata` section of the posted summary and never enter
  the finding list: they never post inline (0.30.0 posted them inline when
  stable ids were on), never fall to a severity cap, and never change the
  verdict or the exit code — `PRXREF_FAIL_ON=any` included.
- **`metadata_rules` names a rules file (#70).** `PRXREF_METADATA_RULES` /
  `--metadata-rules PATH` (a new flag) takes `off` (default), `on`, or the
  path of a separate TOML rules file holding `branch_patterns`,
  `commit_reference`, `area_globs` and `max_areas_per_pr`. The file is
  loaded and validated before any network call; an unreadable, oversized
  (64 KiB) or invalid file, or a flat key set beside it, exits 2. `on` is
  the back-compat alias that reads the four flat keys as in 0.30.0. It
  stays a `.prxref.toml` key, but a path set there must stay inside the
  repository.
- **Stable finding ids are always on (#71).** Every finding carries an
  `id`, and `--format json` rows always carry `id`, `anchor_block` and
  `id_reused_from`. `PRXREF_VERDICT_STORE` is read whenever it is set.
  `PRXREF_STABLE_IDS` (and the `stable_ids` file key) is **deprecated and
  ignored**: the environment variable accepts any value, the file key
  must still be a boolean (any other type exits 2, as in 0.30.0), and a
  non-empty value other than `1` logs one WARNING saying the knob is
  ignored.
- **The stable-id claim hash stems words (#71).** "derived" and "derive"
  now hash alike, so a finding whose title contains inflected words gets
  a different id than 0.30.0 gave it. Refuted verdicts recorded by 0.30.0
  keep suppressing those findings: an entry without a recorded title also
  matches the finding's 0.30.0 id.
- **`anchor_unverified` leaves confidence unchanged (#74).** 0.30.0
  subtracted 0.1, which pushed borderline findings under the quality
  gate; the finding is now only marked. The anchor-snap window widens from
  ±80 to ±100 lines.
- **Rule scoping filters the rules each chunk sees (#75).** New key
  `PRXREF_RULE_SCOPING` / `rule_scoping` (`on` | `off`, default `on`, file
  key, no CLI flag). `on` leaves a scoped rules section out of every
  chunk whose files it does not cover, instead of sending the whole rules
  text with annotations; `off` restores the 0.30.0 behaviour of sending
  everything and leaving labels as the model wrote them.
- **The config schema grows from 92 to 94 keys.** New:
  `PRXREF_ROUTING_PROBE` (#67) and `PRXREF_RULE_SCOPING` (#75).
- **`PRXREF_EVIDENCE_MAX_CHUNK_CHARS` is renamed
  `PRXREF_EVIDENCE_MAX_CHARS` (#69), default 8000 (was 4000).** It caps
  the evidence text one review unit's prompt carries, truncation line
  included. The old environment name is still read, and the 0.30.0
  `.prxref.toml` key `evidence_max_chunk_chars` loads as a deprecated
  alias of `evidence_max_chars`; a file that sets both names is a
  configuration error.
- **`PRXREF_CONTEXT_STANDARDS_MAX_CHARS=0` disables standards excerpts
  (#68)** — no document is read and no block renders — instead of being a
  configuration error. `--context-standards-globs` is a new flag:
  `--context-standards-globs ""` (or `off`) turns standards off for one
  run, and in `.prxref.toml` both `[]` and `"off"` turn them off. In the
  environment a bare empty value still reads as unset.
- **Worker prompt text (#67, #75).** The `## Matching rules` section no
  longer covers firewall or allow-list entries, and when every capturable
  input is constrained it now says "report nothing" instead of asking for
  an outofscope note at confidence 0.6. This moves the packaged worker
  prompt's hash again. Separately, per-chunk rule filtering (#75) changes
  the rendered rules block a chunk receives, not the template.

### Fixed

- **CI wiring (#66).** A body-only edit to an existing script is no longer
  flagged; a modified script counts only when it gains a check flag, and
  is described as "changed", not "added". Literal CI-file globs that the
  repository listing does not hold no longer eat the read budget, and the
  searched list names only files actually read. A rule line that only
  adds a prerequisite to a make target already defined elsewhere in the
  root Makefile is no longer reported as a new unwired target; the head
  Makefile that check needs is read only when the PR adds a check-shaped
  make target. The docs now state that only file candidates ignore
  `$(MAKE)` chains.
- **Standards surfaces (#68).** The `PRXREF_CONTEXT_STANDARDS_GLOBS`
  docstring and `docs/examples/prxref.toml` now say standards apply at
  every `repo_context` level, that `[]` or `"off"` disables them, and use
  the 6000 budget; internal 4000 keyword defaults now match the config
  default.
- **Evidence (#69).** A finding that says a header lacks a directive or
  value (for example "Strict-Transport-Security lacks
  includeSubDomains", "Missing Cache-Control (no-store)") is no longer
  dropped when evidence shows the header is present. A per-header
  vocabulary of directive tokens, directive nouns and `Name: value`
  fragments decides this, not word order, so a plain missing-header
  claim still drops whatever verb follows the name ("Missing
  X-Frame-Options allows clickjacking"). A finding that names no resource is settled only by a probe
  that also targets no specific resource. An evidence item with no exit
  code has an unknown status: it shows as `exit: unknown` and never drops
  or raises a finding. The evidence note also counts and lists findings
  dropped for restating a failing check.
- **Metadata docs (#70).** The docs describe when the `PR metadata`
  section appears, including the `Skipped metadata check` line of a
  metadata stage that fails, and list the commit endpoints for GitHub,
  Gitea/Forgejo, Bitbucket Server and Azure DevOps.
- **Thread handling (#73).** GitHub joins GraphQL thread state on the root
  comment id instead of `(path, line)`, so two threads on one line no
  longer swap their resolved flag and permalink. prxref no longer reads
  its own resolved or outdated comment as a human "won't fix" when its
  text contains "By design." or "Works as intended."; every forge now
  reads won't-fix from the full comment body rather than a truncated
  snippet that lost the prxref attribution. Detection is stricter: "By
  design, X should …", "Works as intended, except …" and questions such
  as "won't fix?" no longer count as a decline.
- **Anchor snapping (#74).** A line-0 finding whose snippet sits outside
  the window snaps to a unique whole-file match, or is marked
  `anchor_unverified`, instead of posting at line 0. A fully qualified
  snippet prefers the usage over an import line or Javadoc. Anchor snap
  no longer moves a finding off a comment or docstring line when the
  finding is about that comment (its title says comment/docstring, or its
  title or body quotes the comment text); a body that only mentions a
  comment in passing no longer pins the finding there.
- **Rule applicability (#75).** The scope check keeps labels it cannot
  map to a scoped section (unknown labels, rules in unscoped sections,
  findings with no file), and maps labels to sections by rule-item lines
  as well as headings, so valid attributions are no longer cleared. A
  rules file's top-level title ("# Acme Java backend review rules") no
  longer scopes the whole file to one language, and a heading naming
  several languages ("## Python and TypeScript conventions") applies to
  files in any of them instead of none; explicit `scope:` lines still
  require every token to match. A `#` comment inside a fenced code block
  (a shell sample, say) is no longer read as a heading, so it no longer
  unseats the document title and hides the whole file from other
  languages' files. A section that survives per-unit filtering always
  keeps its `(applies to: ...)` annotation, whatever other sections that
  unit left out.

## [0.30.0] — 2026-09-30

Ten issues: five new review inputs and one new id scheme, and four
fidelity fixes. A review can now be shown execution evidence — the command
output of running the PR locally (#69) — and the repository's own standards
documents (#68); it can check the PR's branch name, commit reference and
touched areas against team rules with zero model calls (#70), and flag a
verification script or test the PR adds that no CI job runs (#66); findings
can carry stable ids that survive rewording (#71). A partial review is no
longer read as `Approved` (#72), resolved or outdated threads no longer
silence a finding that is still present (#73), findings that quote code
snap to the line it actually sits on (#74), and a finding no longer needs
a rule (#75). The worker prompt also gained a `## Matching rules` section
(#67), so prompts that probe added or widened matching rules. 0.29.0
shipped hours into this run carrying the first half of #72 — the
partial-run names, the `failed_chunks` key and the 120 s deadline — so
this release completes that fix rather than repeating it. The config
schema grows from 78 to 92 keys, and the worker prompt's text changed
(#67, #75): prompt hashes move once, everywhere, even with every new key
off.

### Added

- **`--ci-wiring {off,on}` / `PRXREF_CI_WIRING` (#66), default `off`.**
  When `on`, a deterministic check flags a check the PR *adds* — a
  verify/smoke/check script or flag, a file that gains a shebang, a test
  file outside the runner's default include — that no CI configuration
  file invokes. Its severity is `spec` when the ticket context mentions
  regression checks, CI, pipelines or automated tests, `warning`
  otherwise. The inline finding names the script and the CI files
  searched. A default-include table (pytest, jest/vitest, Go, and test
  directories) says which test files every runner picks up anyway, so only
  a test outside it can be unwired; `--ci-wiring-globs GLOBS` /
  `PRXREF_CI_WIRING_GLOBS` replaces the built-in CI-file globs. The check
  needs the forge's head-sha file reads or `--repo-dir`; without a
  repository reader it records why it did not run. The run record gains
  `ci_wiring`.
- **In-repo standards documents as chunk context (#68).** At
  `PRXREF_REPO_CONTEXT=repo` with a repository reader, each chunk worker's
  prompt gains a last block, `### In-repo standards for this chunk`,
  holding heading-sliced sections of the repository's own standards
  documents: `docs/standards/**`, `docs/adr/**`, `STANDARDS*.md`,
  `SECURITY.md` and `CONTRIBUTING.md`. A section qualifies when something
  the chunk itself names appears in it — a path atom of a changed file, a
  name, route or table the added lines reference, or a quoted string
  literal — and heading matches outrank body matches, so the choice is
  deterministic. A worker that finds a changed line contradicting a
  section cites it as `<path>:<line>` in the finding body; when two
  admitted sections disagree with each other the block says so with a
  `[note]` line instead of picking a side, and a Superseded or Rejected
  ADR section is annotated `(status: Superseded)`. The per-chunk budget is
  `PRXREF_CONTEXT_STANDARDS_MAX_CHARS` (default `4000`).
  `PRXREF_CONTEXT_STANDARDS_GLOBS` **replaces** the built-in document set;
  a bare empty value reads as unset (the house rule), and the exact value
  `off` is the one way to turn standards excerpts off on their own.
- **`--evidence-file PATH` (repeatable) / `PRXREF_EVIDENCE_FILES` (#69).**
  Execution evidence — command output from running the PR locally — as
  review context. Each file is JSON or lenient plain text; an item that
  names a file of a chunk rides that chunk's prompt, and a global item
  rides every chunk and the whole-PR sweep. The evidence is fenced,
  labelled data, not instructions, under a must-not-contradict rule: do
  not report a finding the evidence contradicts, and cite an item when it
  helps. A finding the model itself labels as contradicted
  (`"evidence": "contradicts"`) is downgraded to `warning` by a
  deterministic pass — relabelled, never dropped, so a mislabel cannot
  lose a real finding outright. `PRXREF_EVIDENCE_MAX_CHUNK_CHARS`
  (default `4000`) caps the text one unit carries. The run record gains
  `evidence`.
- **Opt-in deterministic PR metadata rules (#70), zero LLM calls.** Set
  `metadata_rules = "on"` — with `branch_patterns` (entries of the form
  `type=regex`), `commit_reference`, `area_globs` and `max_areas_per_pr` —
  to check the PR's own metadata against team rules. These are **flat
  config keys** (`PRXREF_METADATA_RULES`, `PRXREF_BRANCH_PATTERNS`,
  `PRXREF_COMMIT_REFERENCE`, `PRXREF_AREA_GLOBS`, `PRXREF_MAX_AREAS_PER_PR`)
  set in `.prxref.toml` or the environment, *not* a `[metadata]` table as
  the issue text proposed. The PR's type resolves from its labels first,
  else the conventional-commit prefix of its title (`fix(scope): …`); a
  resolved type whose source branch fails its `fullmatch` pattern is a
  file-level `warning`, as is a PR touching more than `max_areas_per_pr`
  of the `area_globs` areas. `commit_reference` requires every non-merge
  commit subject to match its pattern; a PR with a subject that does not
  is `outofscope`, and forges that cannot list commits (GitHub and Gitea
  can) skip the check rather than fail it. A PR with no resolvable type,
  or no source branch, is skipped with its reason, never flagged.
  Violations are summary-only findings — they never post inline, never
  touch the verdict, and end their body with
  `(deterministic check, no model)`. The run record gains `metadata_rules`.
- **Stable finding ids (#71), behind `PRXREF_STABLE_IDS` (default `off`).**
  With the key on, every finding gains `id`, `file#rule#<12 hex>` — a hash
  of the sorted tokens of its claim — so the same finding keeps its id
  across rewording and one-key anchor drift. A finding also carries
  `anchor_block`, the enclosing function, YAML key or manifest key at its
  anchor, and `--format json` rows gain `id`, `anchor_block` and
  `id_reused_from` (`run`, `verdict` or `thread`, saying where a reused id
  came from). `PRXREF_VERDICT_STORE` names a JSON verdict store keyed by
  stable id: a finding whose id the store holds as `refuted` — a verdict a
  reviewer or operator recorded against it (the code is fine, the claim is
  wrong, the behaviour is intended) — is dropped as a re-worded duplicate,
  `refuted in earlier run (<id>)`, instead of posting again. The pipeline
  only reads the store; recording into it is a script's or UI's call, and
  an `accepted` verdict matches for `id_reused_from` but drops nothing. `prxref eval compare` gains a `## Stable-id reuse`
  section between Metrics and Changed labels, holding the fraction of B's
  active finding ids A already held. The run record gains `stable_ids`.
- **`PRXREF_LLM_TIMEOUT_PER_1K` (#72), default `1.6`.** When the request
  deadline is left at its default (`45` s), each model request's deadline
  scales with its prompt: `min(900, 20 + 1.6 · input_tokens / 1000)`
  seconds, because a 22k-token prompt cannot be answered in 45 s. An
  explicit `--timeout` or `PRXREF_LLM_TIMEOUT` disables the scaling and is
  used as given. Must be greater than 0.

### Changed

- **The config schema grows from 78 to 92 keys (#66, #68, #69, #70, #71,
  #72).** The fourteen new keys: `PRXREF_LLM_TIMEOUT_PER_1K`,
  `PRXREF_CI_WIRING`, `PRXREF_CI_WIRING_GLOBS`, `PRXREF_METADATA_RULES`,
  `PRXREF_BRANCH_PATTERNS`, `PRXREF_COMMIT_REFERENCE`, `PRXREF_AREA_GLOBS`,
  `PRXREF_MAX_AREAS_PER_PR`, `PRXREF_EVIDENCE_FILES`,
  `PRXREF_EVIDENCE_MAX_CHUNK_CHARS`, `PRXREF_CONTEXT_STANDARDS_GLOBS`,
  `PRXREF_CONTEXT_STANDARDS_MAX_CHARS`, `PRXREF_STABLE_IDS` and
  `PRXREF_VERDICT_STORE`.
- **The worker prompt gained a `## Matching rules` section (#67).** Each
  chunk worker is now told to probe added or widened matching rules —
  location patterns, rewrites, router patterns, globs, allow-lists, regex
  validators — for inputs they newly capture, and to answer a constrained
  case with an outofscope note at confidence 0.6. Because prompt text
  moved, the packaged worker prompt's hash changes once for every run, and
  prompt-sha comparisons (`run.json`'s `prompts.sha256`, trace files)
  move with it, even with every new key off.
- **A finding no longer needs a rule (#75).** The `RULE_REQUEST` text now
  reads "A finding does not need a rule: if no rule applies, write `null`,"
  and the example finding shows a ruleless row, so findings the model
  cannot tie to a team rule post instead of inventing one. This changes
  the worker and sweep prompt hashes, as above.
- **The GitHub adapter stops re-anchoring outdated comments (#73).** An
  outdated review comment is marked outdated rather than laundered through
  its original line, and review-thread resolution is read through
  GraphQL's `reviewThreads` (best effort: a refused query falls back to
  the REST view).

### Fixed

- **Resolved or outdated threads no longer suppress findings (#73).** Only
  an open, current thread carrying the same claim suppresses a finding.
  A finding that matches a resolved or outdated thread posts with
  `Previously raised in <thread>; still present at <file>:<line>.`, the
  summary line counts the suppressions, and the run record gains a
  `thread_dedup` stamp.
- **A partial review is no longer `Approved` (#72, completing the 0.29.0
  fix).** 0.29.0 named what a partial review skipped — the
  `failed_chunks` entries, the `not reviewed:` line, the `hint:` on a
  deadline — and raised the flat model deadline to 120 s, but a partial
  run still ended `Approved`. Now a failed chunk sets `degraded` with its
  index and files, and the verdict becomes `Incomplete` — the ladder is
  `Error` → `Request-Changes` → `Incomplete` → `Approved`. Under
  `PRXREF_FAIL_ON=error` or `any` an `Incomplete` review exits `1`,
  because a gate must not read a broken run as green. A chunk that times
  out now says so: `[chunk i/N] timed out after <t>s; increase
  --timeout`, and while the deadline is left at its 120 s default the
  `openai-compat` family scales it up with the prompt
  (`PRXREF_LLM_TIMEOUT_PER_1K`, a new key) — scaling only ever extends
  the deadline above 120 s, never below it.
- **Anchor snapping (#74).** A finding that quotes a token or snippet of
  the head file snaps to the line that text actually sits on, within
  ±80 lines of its claimed line. A finding whose quoted evidence cannot
  be anchored — nothing parseable, a snippet the head file does not hold,
  an ambiguous multi-match — is flagged `anchor_unverified` and loses 0.1
  confidence instead of posting at a line its evidence does not hold.
  "Also at" locations are verified the same way, and a site nothing
  corroborates is dropped from the list. Line-0 findings join the
  reworded-duplicate dedup.
- **Rule applicability (#75).** Rules files support a `scope:` line under
  an ATX heading (`scope: java, openapi`), which annotates the prompt's
  heading with `(applies to: java, openapi)` and drives a deterministic
  pass that clears a wrong `rule` label — one whose section does not cover
  the finding's file — keeping the finding itself, which then groups and
  caps by title. Known scope tokens: `java`/`jvm`, `python`,
  `typescript`/`javascript`/`ts`/`js`, `docs`/`markdown`,
  `openapi`/`specs`, and `comments` (every path); an unknown token is
  inert and covers every path, because an unknown word must not silently
  suppress rules. The run record gains `rule_scope_cleared`.

## [0.29.0] — 2026-09-30

A partial review names what it did not review (#72). A run where some
review units fail still completes, and its record now carries
`failed_chunks`: one `{unit, kind, files, error}` per failed unit, in
review order (`kind` is `chunk` with that chunk's files, or `sweep` with
none). `--format json` gains the key after `chunks_failed` (`[]` when
every unit completed), and the text summary prints `not reviewed:` with
the files after the `coverage:` line, plus a `hint:` naming `--timeout`
when a failure was a model deadline. The verdict and the `degraded` key
keep their meaning: `degraded` still reports post failures only.

The `PRXREF_LLM_TIMEOUT` default rises from 45 to 120 seconds. Reasoning
models routinely take 50 s or more on a large chunk over HTTP, so the old
default dropped normal chunks on every run; a lower deadline stays one
environment variable or `--timeout` away.

## [0.28.0] — 2026-09-29

Team rules reach the model whole (#63). The
`PRXREF_REVIEW_RULES_MAX_CHARS` default rises from 12000 to 24000,
matching `PRXREF_SCOPED_RULES_MAX_CHARS`, so a rules file between 12k
and 24k characters no longer loses its tail — the last groups in the
file (tests, observability, process) that the old cap cut behind one
WARNING most CI runs never surface. Nothing else moves: the loader, the
prompt block and the record are unchanged, and the old ceiling stays one
environment variable away.

### Changed

- **`PRXREF_REVIEW_RULES_MAX_CHARS` default raised from 12000 to 24000 (#63).**
  Team rules files built from mined reviewer comments routinely run 18k to
  22k characters, and the 12000 default silently cut their tail — usually
  the last groups in the file (tests, observability, process), which then
  went unchecked behind one WARNING most CI runs never surface. The new
  default matches `PRXREF_SCOPED_RULES_MAX_CHARS` (24000), so the two caps
  no longer disagree. Prompts for rules files between 12k and 24k
  characters get longer, by up to roughly 3000 more input tokens per review
  unit at the top of that range; set `PRXREF_REVIEW_RULES_MAX_CHARS=12000`
  to keep the old behavior.

## [0.27.0] — 2026-09-28

Chunking and repository-context read limits, visible and settable (#61).
Every review now reports whether its chunks overflowed the token budget,
and which repository-context read cap was hit, per chunk or per run. The two
read caps are config keys (`repo_context_max_reads`,
`repo_context_max_chunk_reads`), both settable from `.prxref.toml`. No
default changes: the caps stay 200 and 16, `max_chunks` stays 8, chunk
placement is unchanged, and a review that does not overflow prints the same
text as in 0.26.0.

### Added

- **`PRXREF_REPO_CONTEXT_MAX_READS` / `repo_context_max_reads` and
  `PRXREF_REPO_CONTEXT_MAX_CHUNK_READS` / `repo_context_max_chunk_reads`
  (#61).** The run-wide and per-chunk caps on repository-context file reads
  (defaults 200 and 16, the previous hard-coded values). Integers greater
  than 0; anything else exits 2 naming the variable. Both are recorded in the
  eval harness's run config.
- **Which cap was hit (#61).** The run record's `repo_context` gains
  `chunk_read_cap_hit`, `run_read_cap_hit`, `max_reads` and
  `max_chunk_reads`. `read_cap_hit` stays, as the OR of the two flags. The
  `repo_context` trace event carries both flags. At the defaults with the
  context follow-up off, 8 chunks × 16 reads stay under 200, so only the
  per-chunk cap can bind.
- **Chunk overflow fields (#61).** `chunks_over_budget`,
  `largest_chunk_tokens`, `overflow_files` and `chunk_token_budget` sit next
  to `chunk_count` in the run record and `--format json`, on every exit.
  `overflow_files` counts files placed past `PRXREF_MAX_CHUNKS` into an
  already-full chunk. `triage.plan_chunks` returns the chunks with these
  counts from the same placement pass; `build_chunks` is unchanged.

### Changed

- **Text output (#61).** The `repo context:` line reads
  `reads=N max_reads=M max_chunk_reads=K cap_hit=no|chunk|run|chunk+run`
  (was `cap_hit=no|yes`). `reads` still counts PR-file fetches, which
  spend neither cap. A new `chunks:` line appears only when a chunk is over
  budget or files overflowed, for example
  `chunks: 2 over the 2000-token budget (largest ~7240) · 4 files placed past
  the chunk cap; raise PRXREF_MAX_CHUNKS or PRXREF_CHUNK_TOKEN_BUDGET`.

## [0.26.0] — 2026-09-28

Summary layout you can shape (#59). A `summary.md` override
(`PRXREF_PROMPTS_DIR`) can now group findings by severity under their own
headings, show the reviewed head commit, and use its own severity glyphs
everywhere prxref draws one. Two new config keys (`severity_markers`,
`summary_bullet_separator`), both settable from `.prxref.toml`, and new
summary slots. With neither key set and no new slot used, every summary,
inline comment, formatter output and stdout byte is the same as in 0.25.0.

### Added

- **Per-group summary slots (#59).** `{error_findings}`,
  `{warning_findings}`, `{spec_findings}` and `{outofscope_findings}` hold the
  bullet list `{findings}` builds, restricted to that severity and to
  findings inside the ticket (or with no ticket). `{outside_ticket_findings}`
  holds the findings outside the ticket. Each is `""` when its group is empty.
  The `*_section` variants (`{error_section}`, …, `{outside_ticket_section}`)
  add a bold heading such as `**🟥 Errors**` and render nothing at all for an
  empty group, so a template never shows an empty heading.
- **More summary slots (#59).** `{head_sha}` and `{head_sha_short}` (the
  first 7 characters) name the reviewed head commit. `{inline_accounting}`
  places the inline-comment accounting line. `{chunk_count}`,
  `{input_tokens}` and `{output_tokens}` bring the CLI summary level with the
  library formatter's. `{error_marker}`, `{warning_marker}`, `{spec_marker}`,
  `{outofscope_marker}` and `{out_of_ticket_marker}` hold the effective
  glyphs. `docs/prompt-templates.md` has a table of which slots each of the
  two summary renderers fills, and a worked example
  (`docs/examples/summary-by-severity.md`).
- **`PRXREF_SEVERITY_MARKERS` / `severity_markers` (#59).** Comma-separated
  `name=glyph` pairs, e.g. `error=🔴,warning=🟡,outofscope=⚪`, over the
  names `error`, `warning`, `spec`, `outofscope` and `out_of_ticket`. Names
  you leave out keep their default. The table applies to the summary counts
  line and bullets, the section headings, inline comment headers, the spec
  note and the library formatter. An unknown name (with a did-you-mean hint),
  a malformed pair, an empty or whitespace-bearing glyph, a repeated name, or
  two names that would render the same glyph is a configuration error (exit
  2).
- **`PRXREF_SUMMARY_BULLET_SEPARATOR` / `summary_bullet_separator` (#59).**
  The text between a summary bullet's location and its title, kept verbatim
  including its spaces. It defaults to ` — `; `: ` gives
  ``- 🔴 `a.py:3`: title``. At most 16 characters and no newline.

### Changed

- **A `summary.md` override no longer needs `{findings}` (#59).** It is
  valid with `{findings}` or with at least one per-group slot; a template
  with neither is still refused (exit 2). When a template without
  `{findings}` leaves a group without a slot, loading it logs a warning
  naming the group. At render time that group's findings are added under
  `**Other findings (N)**` just above the footer, so no finding is dropped.
- **The packaged summary templates draw glyphs from marker slots (#59)**
  instead of literals. The rendered text is unchanged. An override that
  still writes the glyphs literally keeps working, but it will not follow
  `PRXREF_SEVERITY_MARKERS`.

## [0.25.0] — 2026-09-28

Repository config file `.prxref.toml` (#38). Most settings can now be
committed with the code instead of copied into every pipeline: rules files,
the model chain, context levels, comment policy and caps. Credentials,
endpoints, executables, local reads and writes, and the gate stay in the
environment. Two new flags on `review` and `eval run` (`--config`,
`--no-config`), one on `serve` (`--config`), one new subcommand
(`prxref config check`), one new run-record key (`config_file`) and one new
environment variable (`PRXREF_CONFIG_FILE`), which is not a config key. When
no file is found or named, every config value, error message, prompt, forge
read, forge write and stdout byte is the same as in 0.24.0, and the run
record gains only `"config_file": null`.

### Added

- **Repository config file (#38).** A flat TOML file, read with the stdlib
  `tomllib`, whose keys are the config key names (`max_chunks = 4` for
  `PRXREF_MAX_CHUNKS`). Precedence is built-in defaults < file <
  environment < command-line flags, so the pipeline always has the last
  word. Values take TOML types: integers, numbers, booleans, arrays of
  strings, and strings for choice keys. An empty string or array reads as
  unset, as an empty environment variable does. An error about a file value
  names the file and the key, as in
  `.prxref.toml: max_chunks: must be ...`.
- **Where the file is read from (#38).** `--config PATH`, else
  `PRXREF_CONFIG_FILE`, else `.prxref.toml` in the working directory.
  Parent directories are never searched. The value `off`, in any case, in
  the flag or the variable reads no file, and so does `--no-config`. A named
  file that does not exist exits 2 with
  `--config: config file not found: PATH` or the same line naming
  `PRXREF_CONFIG_FILE`.
- **What a file may not set (#38).** 30 keys are environment-only, each with
  a reason class: credential (tokens, passwords, webhook secrets), endpoint
  (`llm_base_url`, `jira_base_url`), executable (`llm_backend`,
  `llm_cli_path`), local write (`trace_file`, `trace_dir`, `fallback`),
  local read (`price_table`) and gate (`fail_on`, `dry_run`,
  `allow_unsigned`). Setting one in the file exits 2 naming the key, its
  class and the `PRXREF_` variable to use instead, even when the value is
  empty.
- **Paths in the file stay in the repository (#38).** `review_rules`, each
  `scoped_rules` entry, `prompts_dir`, `ticket_context_file` and each
  `spec_sources` entry resolve against the file's own directory, not the
  working directory. An absolute path, a `~` path, or one that resolves
  outside that directory through `..` or a symlink exits 2. `spec_sources`
  in the file takes local paths only; web pages and Jira tickets stay in
  `PRXREF_SPEC_SOURCES` or `--spec`.
- **Loud errors (#38).** An unknown key, a wrong type, a table and a TOML
  syntax or UTF-8 error each exit 2 before any network call, naming the file
  and linking docs/config-file.md. An unknown key close to a real one gets a
  hint: `unknown key 'max_chunk'; did you mean 'max_chunks'?`.
- **`--config PATH` and `--no-config` on `prxref review` and
  `prxref eval run` (#38).** They are mutually exclusive. `eval run`
  resolves and checks the file once, before the first case, reviews every
  case with it, and its `run.json` `config` reflects it.
- **`prxref serve --config PATH` (#38).** The daemon never auto-discovers
  `.prxref.toml`, because its working directory is not the repository it
  reviews. It reads a file only when `--config` or `PRXREF_CONFIG_FILE`
  names one (`off` reads none), exits 2 before listening when that file is
  missing or invalid, and re-reads it for every webhook review.
- **`prxref config check [--config PATH | --no-config]
  [--format text|json]` (#38).** It resolves the file as `review` does,
  validates the file and the environment without reading a pull request or
  calling a model, and prints every setting with its source (`default`,
  `file` or `env PRXREF_<NAME>`), then `ok`. Credentials and webhook
  secrets print only as `<set>` or `<unset>`. It exits 0 when the
  configuration is valid, and 2 with the `configuration error: ...` line a
  review would print when it is not; under `--format json` an error leaves
  stdout empty. It opens the rules file, the scoped rules, the
  ticket-context file and the prompts directory as `review` does, so a
  missing or unusable one fails `config check` with the same exit 2 and the
  same message, and a file copied away from the files its paths name is
  caught before the review.
- **A path the file set is named as the file's key (#38).** When
  `review_rules`, `scoped_rules`, `ticket_context_file` or `prompts_dir`
  comes from the file and cannot be loaded, `review`, `eval run` and
  `config check` name it `.prxref.toml: review_rules`, as every other file
  error does, rather than the `PRXREF_` variable nobody set. A path from the
  environment or a flag is still named by the variable or the flag.
- **Run record and `--format json` key `config_file` (#38), after
  `degraded`.** `null` when no file was read, else
  `{"path", "sha256", "keys"}`: the file as errors name it, the sha256 of
  its bytes, and the sorted keys it sets, including keys a later layer
  overrode. A file inside the working directory is named relative to it
  even when it is named through a symlinked directory (`/tmp` for
  `/private/tmp` on macOS), so errors, `config check` and the record show
  the same short name for either spelling. With `-v` in text mode, `review` logs
  `config: <path> (<n> keys)`.
- **`llm_temperature` in the file takes a TOML number (#38)**, as in
  `llm_temperature = 0.2`, or a quoted string. The number is stored as the
  string the environment variable would hold.
- **Docs (#38).** docs/config-file.md covers where the file is read from,
  precedence, the schema, every key a file can and cannot set, the path
  rules, each error message, `prxref config check`, and which copy of the
  file CI reads. A commented example is in docs/examples/prxref.toml, and
  the README, the per-forge CI recipes, docs/env-vars.md
  (`PRXREF_CONFIG_FILE`), .env.example, docs/review-rules.md and
  docs/prompt-templates.md point at it. For lanes that gate merges, a recipe
  extracts `.prxref.toml` and the `.prxref/` directory it names from the
  target branch with `git archive` and passes the copy with `--config`, so
  the target branch's settings win over the pull request's.

### Security

- **A pull request can edit the file that reviews it.** In CI the
  workspace is usually the PR's own code, so auto-discovery reads the PR's
  copy of `.prxref.toml`. The file can relax review settings (lower caps,
  raise `confidence_floor`, point the rules or prompts at the PR's own
  copies, pick another model), but it cannot reach credentials, endpoints,
  executables, local reads or writes, or the gate, and its paths cannot
  leave the repository. On a lane where the review gates the merge
  (`PRXREF_FAIL_ON=error` or `any`), or one under `pull_request_target`,
  read the file from the target branch with the recipe in
  docs/config-file.md ("Security: which copy of the file does CI read?"),
  check out only the base, or set `PRXREF_CONFIG_FILE=off`.

### Known limitations

- **The webhook daemon cannot read the reviewed repository's file.** It
  would need to fetch `.prxref.toml` from the forge for each pull request;
  `serve` reads only a file named on its own host.
- **`prxref eval score` reads no file.** Its judge takes every setting from
  the environment, including `PRXREF_LLM_PARSE_RETRIES`.

## [0.24.0] — 2026-09-28

Gitea and Forgejo support (#31). prxref now reviews pull requests on a fifth
forge: Gitea, Forgejo, Codeberg and gitea.com, on any host, through one new
adapter, with webhook verification for `prxref serve` and a CI recipe for
Forgejo Actions and Gitea Actions. Two new config keys, `PRXREF_GITEA_TOKEN`
and `PRXREF_GITEA_WEBHOOK_SECRET`; no new CLI flag, run-record key or `Forge`
method. For every other forge, every URL resolution, prompt, forge read,
forge write, webhook verdict and stdout byte is the same as in 0.23.0.

### Added

- **Gitea / Forgejo reviews (#31).** `prxref review --pr-url
  <scheme>://<host>[/<sub-path>]/<owner>/<repo>/pulls/<n>` detects the forge
  on any host, including instances under a sub-path and plain-HTTP ones.
  `detect_forge` asks it after GitLab and before Azure DevOps, and it refuses
  the other forges' cloud hosts and any URL with an `api` segment ahead of
  the owner, so API URLs that resolved to nothing still do. The token comes
  from `PRXREF_GITEA_TOKEN`, sent as `Authorization: token <t>`; without one,
  a public repository is read anonymously. Observed scopes:
  `read:repository` to review, and `write:repository` (inline comments) plus
  `write:issue` (the summary) to post.
- **Gitea / Forgejo delivery (#31).** The summary is a comment on the pull
  request's conversation (`/issues/{n}/comments`) that later runs edit in
  place. Each run posts its inline findings as one `COMMENT` review, each
  comment anchored by `new_position` to a line of the new file; the review is
  accepted or refused whole, so a read-only token is reported as a failed
  inline post even under `PRXREF_POST_MODE=inline`. Suggestions render as the
  copyable **Suggested change** block, with no apply button. Re-review
  pruning deletes a whole review when every comment in it is prxref's own and
  its body is empty or prxref's; otherwise it deletes single comments through
  Forgejo's review-comment delete route, and where that route is refused it
  logs the failure and leaves the comment.
- **Gitea / Forgejo pinned-range replay (#31).** The API has no compare diff,
  so `--base-sha`/`--head-sha` rebuild one locally from the compare listing
  plus whole files at the merge base and the head. Binary files, files over
  512 KiB and files past the first 300 are reviewed header-only. A rename
  appears as a delete plus an add, as the compare listing reports it, and a
  range whose head merged the base branch in is refused. The API exposes no
  description edit history, so a replay shows the current title and
  description, and `--as-of` exits 2, as on GitLab.
- **Gitea / Forgejo webhooks (#31).** `prxref serve` recognizes
  `X-Forgejo-Event` / `X-Gitea-Event` and verifies the bare-hex HMAC-SHA256
  in `X-Forgejo-Signature` or `X-Gitea-Signature` against the new
  `PRXREF_GITEA_WEBHOOK_SECRET`; with the secret unset it rejects every such
  delivery with 401 unless `PRXREF_ALLOW_UNSIGNED=1`. `pull_request` actions
  `opened`, `synchronized` and `reopened` are reviewed; everything else is
  acknowledged with 202 and ignored. Both forges also send GitHub's headers
  (`X-GitHub-Event`, `X-Hub-Signature-256`) on every delivery, so their own
  headers are checked first; a delivery carrying only GitHub headers gets
  exactly the 0.23.0 verdict. Deliveries captured from live Gitea 1.24 and
  Forgejo 11 instances ship, scrubbed, as test fixtures.
- **Docs (#31).** A Gitea / Forgejo section in docs/forges.md (URL shapes,
  token scopes, every endpoint the adapter calls, webhook setup and headers),
  a Forgejo Actions / Gitea Actions CI recipe (`.forgejo/workflows/` or
  `.gitea/workflows/`) that builds `--pr-url` from `github.server_url` and
  `github.repository`, a row in the "When prxref cannot post" matrix, and the
  two keys in `.env.example`, docs/env-vars.md and the config reference.

### Known limitations

- **Whether Forgejo and Gitea runners render the fallback is unverified.**
  Their runners set `GITHUB_ACTIONS=true`, so a run that cannot post emits
  GitHub-style `::warning` annotations and appends to
  `$GITHUB_STEP_SUMMARY`; whether either forge shows those in its UI has not
  been checked. No Forgejo or Gitea Actions runner has run the CI recipe.
- **Single-comment prune on upstream Gitea is unverified.** The
  review-comment delete route was exercised on Forgejo only. Where it is
  refused, a partly human review keeps prxref's old comments.
- **The adapter was exercised live on Forgejo 11 only.** Webhook deliveries
  were captured from both Gitea 1.24 and Forgejo 11, but the adapter's reads
  and writes ran against a local Forgejo; Codeberg, gitea.com and upstream
  Gitea have not run a review.

## [0.23.0] — 2026-09-27

Graceful degradation when the token cannot post (#48). When prxref can read a
pull request but not comment on it, the review now reaches the author through
the CI job itself instead of only a log line. The new key `PRXREF_FALLBACK` is
on by default but acts only on a failed post, so when every post succeeds (or
nothing is posted) every prompt, forge read, forge write and stdout byte is the
same as in 0.22.0; the run record and `--format json` gain one key,
`degraded`, which is `null` then.

### Added

- **`PRXREF_FALLBACK` (`auto`|`off`, default `auto`) (#48).** When a summary
  or inline post fails (a fork PR under a plain `pull_request` workflow, or a
  read-only pipeline token), `prxref review` emits the review through the CI
  it runs under, detected from the environment:
  - GitHub Actions (`GITHUB_ACTIONS=true`): one `::error`, `::warning` or
    `::notice` annotation per active finding on stdout, errors first, at most
    50, and the summary appended to the job summary (`$GITHUB_STEP_SUMMARY`);
  - Azure Pipelines (`TF_BUILD=True`): one `##vso[task.logissue]` logging
    command per active finding on stdout;
  - GitLab CI (`GITLAB_CI=true`): a Code Quality report,
    `gl-code-quality-report.json`, in the working directory (declare it under
    `artifacts: reports: codequality:`);
  - Bitbucket Pipelines (`BITBUCKET_BUILD_NUMBER`) and outside CI: the
    summary in the log at WARNING.

  Every CI also logs one WARNING line naming the cause. Under `--format json`
  the stdout annotations and logging commands are skipped, so stdout stays one
  JSON document; the job summary and the GitLab report are still written.
  `off` emits nothing. The emission never raises, and the exit code never
  changes: it still follows `PRXREF_FAIL_ON`. The webhook daemon records the
  failure but never emits. Any other value exits 2 naming the variable.
- **The run record and `--format json` gain `degraded` (#48),** right after
  `incremental`: `null` when no post failed, otherwise `{"cause", "failed",
  "fallback", "annotations"}`. `cause` is `permission` when a failed post was
  refused with HTTP 401 or 403, else `error`; `failed` lists `summary` and/or
  `inline`; `fallback` lists what was emitted instead, and `annotations`
  counts the stdout lines or report entries.
- **docs/forges.md has a per-forge "When prxref cannot post" matrix (#48).**

### Known limitations

- **With `PRXREF_POST_MODE=inline`, a read-only token is detected on GitHub
  only.** Bitbucket Cloud, Bitbucket Server, GitLab and Azure DevOps skip a
  refused inline comment without raising, so an inline-only run there records
  no degradation and emits nothing. The default post mode is covered, because
  the summary post fails first and the inline comments are not attempted.
- **The fallback formats are tested, not yet seen on a live runner.** The
  annotations, logging commands and Code Quality report follow each CI's
  documented syntax and are checked against fakes and mocked 401/403
  responses; no live CI run has shown them yet.

## [0.22.0] — 2026-09-27

Incremental re-review on push (#34). With the new key `PRXREF_INCREMENTAL=on`,
a push re-reviews only the pull request's files changed since the head that
prxref's previous summary recorded, instead of the whole pull request. The key
is off by default, and at the default every prompt, LLM call, forge read and
forge write is the same as in 0.21.1; the run record and `--format json` gain
one key, `incremental`, which is `null` while the key is off.

### Added

- **`PRXREF_INCREMENTAL` (`off`|`on`, default `off`) (#34).** When `on`, each
  summary prxref posts ends with an invisible marker naming the PR head it
  reviewed, `<!-- prxref-reviewed-head: <sha> -->`. On a later push prxref
  reads that summary back, fetches the compare diff from the marked head to
  the new one, and chunks and reviews only the files of the PR's own diff
  that the compare diff touches, so a merge from the base branch or a rebase
  cannot widen the set. Only those files' earlier prxref inline comments are
  pruned, and they are the only files that get repository context. The
  whole-PR systemic sweep, the size advisory and the deterministic checks
  still see every file, and the summary notes the incremental scope in one
  line. A push that touched none of the PR's files runs the sweep alone and
  prunes nothing. With it on, the chunk findings, and so the verdict, cover
  only the re-reviewed files (plus what the sweep and the deterministic
  checks find anywhere in the PR), while earlier inline comments on the other
  files still stand. Any other value exits 2 naming the variable.
- **Full-review fallbacks (#34).** An incremental-on run reviews every file,
  and says why in the run record, on a first review, when the forge cannot
  read its summary, when that read fails (a WARNING), when the summary has
  no reviewed-head marker, when the PR head is unknown, when the forge cannot
  compare commits, when the compare diff fails (a force-push can make the
  marked head unknown; a WARNING), and with `PRXREF_POST_MODE=inline`, which
  never writes the marker. Replays always run full. `--full-review` and any
  `PRXREF_FAIL_ON` gate force a full review that still records the head, so
  the following push is incremental again.
- **`prxref review --full-review` (#34),** which reviews every file even when
  `PRXREF_INCREMENTAL=on`.
- **A failed review unit never advances the marker (#34).** When a chunk or
  the sweep fails, the summary keeps the previous marker's head, so the next
  push re-reviews the files that unit covered. A run with no previous marker
  (a first review, or a forced full review, which never reads it) writes none
  then, and the next push reviews every file.
- **The run record and `--format json` gain `incremental` (#34),** right
  after `suggestions`: `null` when the key is off, else `{"mode", "reason",
  "since_sha", "files_total", "files_reviewed", "marker_sha"}`.
- **Every forge adapter can read back the summary comment it last posted
  (`get_summary`) (#34),** through the same lookup `post_summary` uses to
  update it, so an incremental run finds its previous marker without ever
  writing. It is an optional `Forge` method; a forge without it runs full.
- **`prune_inline_comments` takes an optional `paths` collection (#34):**
  only prxref's own inline comments on those files are deleted, a renamed
  GitLab file matches under either name, and a comment whose file cannot be
  told is kept. Without `paths` it prunes exactly as before.
- **Cost (#34).** With the key on, a run makes one extra forge read for the
  previous summary, plus a compare diff read when the head has moved since
  the marker. Off, it makes none.

## [0.21.1] — 2026-09-27

A worker or sweep reply that the provider stopped at the completion budget
before it was usable is now retried once, automatically, at a larger budget
(#52).

### Fixed

- **A reply cut off at the completion budget before it was usable is now
  retried once, at double the budget, capped at 16384 (#52).** A reasoning
  model can spend its whole completion budget on hidden reasoning and return
  an empty reply; before this release that reply failed the unit outright.
  Now, after one WARNING naming the original and the doubled budget, the
  same request is sent again at the larger budget. This costs one extra LLM
  call, walking the model fallback chain again, and only for a reply that
  was unusable at the budget stop; a reply that is merely truncated but
  still usable is kept as before, with its existing warning, and is not
  retried. A budget already at 16384 is not retried either. When the retry
  is truncated too, the error names the larger budget; when it raises, the
  unit keeps the original truncation error with a note that the retry
  failed. An operator watching the log sees the new WARNING line naming both
  budgets, and, on a second failure, the truncation error citing the larger
  one. No new config key and no new run-record key.

## [0.21.0] — 2026-09-27

Apply-able code suggestions (#30). With the new key `PRXREF_SUGGESTIONS=on`,
a chunk worker may attach the exact replacement code for a finding's lines,
and the inline comment carries it in the form the forge can apply: a
`suggestion` block with GitHub's **Commit suggestion** button, a
`suggestion:-0+N` block with GitLab's **Apply suggestion** button, and a
labelled, copyable code block on Bitbucket Cloud, Bitbucket Server / Data
Center and Azure DevOps. The key is off by default, and at the default every
prompt, LLM call and forge read is the same as in 0.20.0. The issue proposed
an opt-out flag; this release ships the feature opt-in instead, because a
new request to the model stays off until its effect on reviews is measured.

### Added

- **`PRXREF_SUGGESTIONS` (`off`|`on`, default `off`) (#30).** When `on`,
  every chunk worker's system prompt gains a `## Code suggestions` block, and
  the worker template's reply example gains a `"suggestion"` key through a
  new optional `{suggestion_example}` slot. A finding may then carry
  `suggestion`, the exact replacement text for the new-file lines `line`
  through `suggestion_end_line`. The whole-PR sweep is never asked and never
  keeps one. At `off` the worker prompt renders byte-identical and a
  suggestion a model volunteers anyway is ignored. A worker template override
  without the slot still gets the request block, and the run logs one
  WARNING naming the file. Any other value exits 2 naming the variable.
- **A suggestion validation pass (#30).** After the other quality passes and
  before the gate, a suggestion that fails the first of these checks is
  cleared and the finding is kept, so it posts as a plain comment: `grouped`
  (the finding stands for several places), `line_moved` (line alignment
  moved it off the model's line), `file_level`, `range` (reversed, or over
  20 lines), `outside_hunk` (not all new-side lines of one diff hunk),
  `fence` (holds a triple-backtick fence), `too_long` (over 4,000
  characters) and `no_op` (equal to the current lines).
- **Per-forge rendering (#30).** GitHub gets a `suggestion` block, and a
  multi-line suggestion posts as a ranged review comment (`start_line` and
  `start_side`, ending at the last replaced line). GitLab gets
  `suggestion:-0+N`, anchored at the first replaced line. Bitbucket Cloud,
  Bitbucket Server / Data Center and Azure DevOps get a **Suggested change**
  label naming the lines and a plain code block, or a delete sentence when
  the replacement is empty. A suggestion with a fence, an inverted range or
  no line posts as a plain comment. `README.md` has a new Code Suggestions
  section.

### Changed

- **`--format json` finding rows gain `suggestion` and
  `suggestion_end_line` (#30),** right after `rule`. A finding without a
  suggestion, including every finding of a run with suggestions off, has
  them as `null` and `0`; `""` is a suggestion that deletes the lines.
- **The run record gains `suggestions` (#30),** also in `--format json`:
  `null` when suggestions are off, else `{"kept": n, "cleared": {<reason>:
  n, ...}}` over the active findings, one `cleared` count per validation
  reason, in the order above. The eval run record's `config` allowlist gains
  `suggestions`, so `run.json` records the setting.
- **GitHub thread dedup treats a multi-line comment as its whole line range
  (#30).** A finding anywhere from the comment's first line to its last is at
  distance 0 from it, and one outside is measured from the nearer end.
  Before, only the last line counted. This is a superset of the old
  matching, and single-line threads get exactly the verdicts they got
  before. `Thread` gains an optional `start_line`, and `InlineComment` an
  optional `start_line`, both `None` by default.
- **With suggestions on, the completion budget defaults to 8192 (#30).** When
  `PRXREF_SUGGESTIONS=on` and `PRXREF_LLM_MAX_TOKENS` is left unset, the
  budget is 8192 instead of 4096, because a suggestion lengthens the reply
  and the 4096 default truncated every suggestions-on run in a live check.
  An explicit value is always used as given, even below 8192. With
  suggestions off nothing changes.

## [0.20.0] — 2026-09-27

One fix. #29: a review of a local diff, `--diff-file` with `--repo-dir` and
no `--pr-url`, now builds the same chunk context a forge review gets, read
from the `--repo-dir` checkout. No config key, run-record key, CLI flag or
module is added. Forge-backed reviews (`--pr-url`, the webhook service, the
GitHub Action) are unchanged: every LLM call, forge read and run-record value
is the same as in 0.19.0. Only a `--diff-file` review that also passes
`--repo-dir`, including an eval diff-file case with a `repo_dir`, gets new
prompt lines.

### Fixed

- **Chunk context for a local diff (#29).** `prxref review --diff-file` with
  `--repo-dir` and no `--pr-url` now builds chunk context (the
  `### Dependency versions` block and same-file definitions) from the
  `--repo-dir` checkout, at any `PRXREF_REPO_CONTEXT` level, `off` included.
  Before, a review with no forge file reader built none. A forge that can
  read files at the PR head still serves chunk context when `--repo-dir` is
  also given, so forge-backed reviews are unchanged. The manifest check's
  full-file section lookup uses the same reader, so it also reads
  `--repo-dir` in such a run. Eval diff-file cases with a `repo_dir` get the
  same context. The `--repo-dir` help text, `README.md` and `docs/evals.md`
  no longer say that repository context `off` ignores the directory.

## [0.19.0] — 2026-09-27

Two fixes. #25: a version-less Maven or Gradle dependency in the
`### Dependency versions` block is no longer attributed to the first imported
BOM, whether or not that BOM manages it; prxref names an owner only when it
can know or reasonably guess one, and marks a dependency matched to an import
by its group alone. #26: the litellm backend forwards
`PRXREF_LLM_REASONING_EFFORT`, and `PRXREF_LLM_SEED=off` sends no seed, for
providers such as Bedrock that reject the parameter. No config key is added:
`PRXREF_LLM_SEED` gains one value, `off`. Nothing changes at the defaults
except the dependency-version lines of Java and Kotlin files: a pull request
with no version-less JVM dependency under 2 or more candidate owners and no
group-only match gets the same prompts as in 0.18.0, byte for byte, and every
LLM call, forge read and run-record value is unchanged.

### Added

- **`reasoning_effort` on the litellm backend (#26).**
  `PRXREF_LLM_REASONING_EFFORT` is now passed to `litellm.completion` as
  `reasoning_effort=`, unvalidated, and litellm maps it to each provider's
  own effort parameter. Unset or empty, the request is unchanged.
- **`PRXREF_LLM_SEED=off` (#26).** On `openai-compat` and `litellm`, `off`
  (lowercase) sends no seed at all: neither a configured one nor the
  per-process fallback. The run record's `sampling.seed` is then `null`.
  Unset still derives one seed per process, as before, and any other word
  still exits `2` naming the variable.

### Changed

- **Group-only JVM matches are marked (#25).** A Java or Kotlin dependency
  matched to an import only by its group, with no artifactId token naming a
  segment of the import, now ends in ` (group match only)`, as in
  `org.slf4j:slf4j-api@2.0.13 (group match only)` for
  `import org.slf4j.Logger`. Such lines are still listed, and an artifact
  another import of the file matches by name renders once, unmarked.
- **Docs for strict providers (#26).** The litellm section of `docs/llm.md`
  explains the Bedrock fixes (`PRXREF_LLM_SEED=off`, or litellm's own
  `LITELLM_DROP_PARAMS=true`) and that a model accepting only
  `temperature=1` needs `PRXREF_LLM_TEMPERATURE=1`.

### Fixed

- **The owner of a BOM-managed JVM version (#25).** One rule now serves
  Maven's imported BOMs and external parent and Gradle's platforms. A single
  candidate owner is named as before, `g:a@(managed by bg:ba@bv)`. Among
  several, the one whose groupId shares at least 2 leading segments with the
  dependency's, and strictly more than every other, is named as a guess,
  `g:a@(likely managed by bg:ba@bv)`. Otherwise no owner is named and up to
  3 candidates are listed in declared order, then `+N more`:
  `@(managed by one of 2 imported BOMs: jackson-bom,
  spring-boot-dependencies)`, `@(managed by the parent or an imported BOM:
  ...)`, or `@(managed by one of 2 platforms: ...)`.

### Known limitations

- **The owner is a guess from groupIds.** prxref does not download a BOM, so
  it never reads what a BOM manages; the likely owner is the one whose
  groupId is closest to the dependency's, and it can be wrong.

## [0.18.0] — 2026-09-27

An opt-in context follow-up (#22, part 1). A chunk worker that cannot see a
symbol's definition is told to ask about it at confidence 0.5 or below, so the
default confidence floor of 0.6 drops the question however real the bug is.
With `PRXREF_CONTEXT_FOLLOWUP=on` at `PRXREF_REPO_CONTEXT=repo`, prxref looks
up the symbols such a question names and sends the chunk once more with their
definitions; a question the second reply confirms posts, and one it does not
is dropped with its own reason. It is off by default, and at `off` every
prompt, forge read, LLM call and run-record value is the same as in 0.17.0.
The run record and `--format json` gain one key, `context_followup`, which is
`null` at `off`. On #22's own fixture through GLM 5.3 Flash (N=3 runs an arm,
follow-up off against on), the history-window question appeared in 2 of the 3
runs with it on; both times its definitions were looked up and sent, yet the
model did not re-assert it above the floor, so it was dropped as not
confirmed. The follow-up added about 3,000 input tokens when it fired.

### Added

- **`PRXREF_CONTEXT_FOLLOWUP` (#22).** `off` (the default) or `on`; any other
  value, `true` and `1` included, exits `2` naming the variable, as a bad
  `PRXREF_REPO_CONTEXT` does. It works only at `PRXREF_REPO_CONTEXT=repo`
  with a repository reader (the forge or `--repo-dir`). At another level, or
  with no reader, the run logs one WARNING saying the context follow-up is
  off for the run, and makes no extra call; this is not a configuration
  error. There are no tuning knobs: every cap below is fixed.
- **The context follow-up (#22).** A chunk gets one when its first call
  returned a review, it was not re-run after a timeout, and at least one of
  its findings is below the confidence floor. The names such a question puts
  in backticks are looked up: identifiers of at least 3 characters that are
  not a receiver, a literal, a language keyword, a Python builtin class such
  as `TypeError`, or a bare lowercase Python builtin such as `len(` (after a
  dot, as in `store.filter(`, it names repository code and is kept),
  type-like names first, then names that follow a `.`, then the rest. Its
  plain text adds, after them, the names shaped like
  code: both halves of a dotted access or call (`table.recent(`), a call
  (`name(`), an identifier holding `_` (`model_history`, `HISTORY_WINDOW`)
  and a type-like word; a source file name such as `history.py` is not
  split, and a plain English word never counts. The chunk looks up at most 3
  names, skipping any name the pull request defines, and every question gets
  one name before any question gets two. Nothing matches phrases in the
  model's wording.
- **Whole definitions for the follow-up (#22).** Each name is found through
  the chunk's imports, path conventions and file-name search, then in files
  of the same language from the listing. A file the pull request changes and
  an excluded path are never read, and a chunk makes at most 8 reads for it,
  cached ones included. The excerpt is the whole definition, not the one-line
  entry of the definitions block: a Python class or function with its
  indented body, a brace-language one up to its closing brace, at most 30
  lines, cut with `… N more lines`. A name defined inside another excerpt
  counts as found, and an excerpt the first prompt already showed is left
  out. At most 4 excerpts and 4000 characters are admitted; with none, no
  call is made.
- **The follow-up call (#22).** The first user prompt with one block added
  after its last context block, `### Definitions referenced by this chunk,
  looked up for its open questions`, and a note to treat the definitions as
  shown and to report findings at their lines in the diff. The system prompt
  and the worker template are unchanged. The call keeps the first call's
  token budget, gets no parse retries and no timeout retry, and never leads
  to another follow-up, so it adds at most 2 calls to a chunk (the empty
  reply retry included). The whole-PR sweep never gets one.
- **Confirmation (#22).** A finding of the second reply confirms a question
  when it is in the same file, at or above the floor, and within 5 lines of
  it, has the same normalized title, or names a symbol looked up for it. A
  confirmed question is replaced by the confirming finding, which then runs
  every quality pass. A question with a looked-up symbol and no confirmation
  is dropped as `not confirmed by context follow-up (confidence C below
  floor F)`; one whose symbols were not found is left to the floor. The
  second reply's other findings are discarded and counted, never posted, and
  findings already at or above the floor are never touched. A follow-up that
  fails, times out or is truncated keeps the first reply's findings, logs a
  WARNING and never fails the review.
- **`context_followup` in the run record (#22).** The run record and
  `--format json` gain `context_followup` after `parse_retries`: `null` at
  `off`; at `on`, `active`, the run's `calls`, `confirmed`, `unconfirmed`,
  `discarded`, `input_tokens` and `output_tokens`, and `chunks`, one row per
  chunk with its question count, the names looked up, each excerpt's path,
  line, symbol, source and size (never its text), its error and why it was
  `skipped`. The JSONL trace gains `followup` events per chunk and a
  run-level `context_followup` event, and `PRXREF_TRACE_DIR` gains
  `chunk<i>.followup.*` trace files beside the chunk's own.

### Changed

- **A follow-up's tokens and cost count toward its chunk (#22).** They are
  added to the first call's, the cost is unknown when either is, and the
  chunk's model becomes the follow-up's when it answered. The chunk's
  `parse_retries` and `first_error` stay the first call's.
- **`run.json` records one more setting (#22).** `prxref eval run` writes
  `context_followup` into `run.json`'s `config`, right before `repo_context`,
  so the block now holds 18 settings.

### Known limitations

- **Only code-shaped names are looked up.** A question that names the
  symbol it could not see as a plain lowercase word, with no backticks, `.`,
  `(` or `_`, gets no follow-up, and a file path is never looked up as such.
  `get`, `set` and `type` are JavaScript keywords to the lookup, so a symbol
  of one of those names is never looked up.
- **A confirmation is a second sample.** The follow-up changes two things at
  once: the prompt gains the definitions, and the model answers the chunk a
  second time. A confirmation does not show which of the two moved it.
- **One follow-up, chunks only.** A chunk re-run after a timeout, the
  whole-PR sweep, and a question in the second reply get no follow-up, and
  the caps have no setting.

## [0.17.0] — 2026-09-25

Java and Kotlin chunk context (#20), a bounded retry for unusable model replies
(#21), and code that reads the state a change writes (#22, parts 2 and 3). A
chunk worker now sees, for a changed Java or Kotlin file, the definitions its
added lines reference from the rest of that file and the Maven or Gradle
versions of what they import, as it already did for JavaScript, TypeScript and
Python. A reply that cannot be used as a review is sent again, up to
`PRXREF_LLM_PARSE_RETRIES` times (default `1`). At `PRXREF_REPO_CONTEXT=repo`,
a chunk also sees excerpts of unchanged code that reads state its added lines
write. A new deterministic check, always on, flags a toggle that ships on
while the pull request's own test setup turns it off. Repository context
at `off` still adds nothing, but Java and Kotlin chunk context does not depend
on it, so a pull request that changes a `.java`, `.kt` or `.kts` file can get
new prompt blocks and forge reads with every setting at its default.
`PRXREF_LLM_PARSE_RETRIES=0` handles replies exactly as 0.16.0 did. The run
record and `--format json` gain one key, `parse_retries`.

### Added

- **Java and Kotlin definitions in chunk context (#20).** The `### Definitions
  referenced by this chunk` block now covers `.java`, `.kt` and `.kts` files
  at every `PRXREF_REPO_CONTEXT` level. Java definitions are types, methods,
  fields and constants, and enum constants; Kotlin definitions are types
  (`class` in every flavour, `interface`, `fun interface`, `object` and
  `typealias`), `fun`, extension functions included, and `val` and `var`. An
  entry starts at up to 2 annotation lines directly above the definition, its
  line number is the first annotation's, and those lines count toward the
  6-line entry cap. The block's other caps are unchanged: 40 entries, 8000
  characters, and files over 512 KiB skipped.
- **Maven and Gradle dependency versions (#20).** The `### Dependency
  versions` block now lists `groupId:artifactId@version` for the imports on a
  changed Java or Kotlin file's added lines. The build file is found by
  walking up from the file's directory to the repository root, trying
  `pom.xml`, then `build.gradle.kts`, then `build.gradle` at each level; the
  first non-empty one wins. A `pom.xml` is resolved through its
  `<properties>`, its parent chain inside the repository (at most 5 parents),
  `<dependencyManagement>` and imported BOMs. A Gradle build file is read in
  string notation (`"g:a:v"` and `"g:a"`), with `platform`,
  `enforcedPlatform` and `mavenBom` as BOM owners and the version catalog
  `libs.versions.toml`, which is read only when the build file mentions
  `libs.`. A version that a BOM, a parent outside the repository or a Gradle
  platform supplies reads `groupId:artifactId@(managed by <owner>)`. Imports
  under `java`, `jdk`, `sun` and `kotlin` are skipped (`javax` is kept), and
  so are imports under the project's own groupId or Gradle `group`. A
  dependency matches an import when its groupId, of at least 2 segments, is a
  package prefix of the import or shares at least 3 leading segments with it.
  Among several matches, the artifactId that names a segment of the import
  wins, so `com.fasterxml.jackson.databind` gives `jackson-databind`, and a
  tie lists every tied artifact. A build file that cannot be parsed
  contributes nothing and never fails a review, and a `pom.xml` that holds a
  `<!DOCTYPE` or `<!ENTITY` declaration, or is larger than 512 KiB, is
  refused before the XML parser runs.
- **Kotlin in repository context (#20).** At `diff` and `repo`, `.kt` and
  `.kts` files are searched for Kotlin type declarations (`class` in every
  flavour, `interface`, a named `object` and `typealias`), as Java files are
  for Java types. Neither language gets a method, function or property here.
- **`PRXREF_LLM_PARSE_RETRIES` (#21).** A chunk or whole-PR sweep reply that
  cannot be used as a review is sent again, as the same request with the same
  prompt and budget, up to `PRXREF_LLM_PARSE_RETRIES` times: default `1`, at
  least `0`, with no upper bound. One budget covers every kind of unusable
  reply: an empty one, one that does not parse, one that parses to something
  other than a JSON object and, at `1` or more, an object without a `findings`
  list. An empty reply keeps the one retry 0.16.0 gave it, even at `0`. A
  reply the provider stopped at the budget (`finish_reason` `length` or
  `max_tokens`) is never retried, because its error already names
  `PRXREF_LLM_MAX_TOKENS`, and neither is a call that raised. Each retry is a
  new call that walks the model fallback chain again and logs one WARNING
  ending `parse retry <k> of <N>`. Token counts and elapsed time cover every
  call, and the unit's reported cost is their sum, or unknown when any call
  reported none. Once the retries are spent, the unit fails with the last
  reply's error, worded as in 0.16.0. `prxref review`, and so the webhook
  server and `prxref eval run`, reads the variable; `orchestrate_review`,
  `review_chunk` and `review_systemic` default to `0` for library callers.
- **`parse_retries` in the run record and the trace (#21).** The run record
  and `--format json` gain `parse_retries`, right after `repo_context`: the
  retries summed over every chunk and the sweep. It is `0` when nothing was
  sent again, including an exit before any review unit ran, and `null` when
  `PRXREF_LLM_PARSE_RETRIES` is `0`. With `PRXREF_TRACE_DIR` set and the
  variable at `1` or more, a unit that retried keeps each discarded reply as
  `<unit>.attempt<K>.response.json` (K from 1) beside its four trace files,
  which still show the reply that was used, and its `<unit>.meta.json` gains
  `parse_retries` and `first_error`, the error the first reply would have
  failed the unit with.
- **A parse retry for the `prxref eval` judge (#21).** `prxref eval score`
  reads `PRXREF_LLM_PARSE_RETRIES` (default `1`) from its own environment. A
  judge reply the parser rejects for any reason (empty, not JSON, not an
  object, no `grades` list, or a grades row that is not an object, has no
  `human_id` or has an unknown grade) is sent again, the same request, up to
  that many times. Unlike a review unit's, a truncated judge reply is
  retried, and an empty one gets no retry at `0`. A call that raised and a
  cache hit are never retried, and only the graded reply is cached.
  `score.json`'s `judge` block gains `parse_retries` after `llm_calls`, which
  counts every attempt, and `score.md`'s judge cost line adds `(1 parse
  retry)` or `(<n> parse retries)` only when there were any. The judge's
  `cost_usd` and the `judge_cost` metric count every attempt, and a case
  whose retry raised after a rejected reply is priced by that reply. A traced
  judge keeps each rejected reply as `judge.attempt<K>.response.json`, and
  `judge.meta.json` gains `parse_retries` and `first_error`. `prxref eval
  run` records `llm_parse_retries` in `run.json`'s `config`, which now holds
  17 settings.
- **Code elsewhere that reads state this chunk writes (#22).** At
  `PRXREF_REPO_CONTEXT=repo`, with a repository reader and a file listing, a
  chunk worker also gets excerpts of unchanged code that reads state the
  chunk's added lines write, such as a map the change stores a new object in,
  or a table it appends rows to. The keys come from subscript stores
  (`recv[k] = v`, key `recv`) and from calls of `append`, `add`, `insert`,
  `save`, `put`, `push`, `store`, `write`, `extend`, `update` or `setdefault`
  (key: the receiver's last segment); a local alias such as `data =
  run.root().data` resolves to its attribute, and a key the added lines
  assign a new value is skipped. A read is `.key` or `["key"]` used other
  than as a store, or `key.<method>(` with a method that is not one of those
  verbs, in a listed file of the same language that the pull request does not
  change; files sharing the most leading directories with the change come
  first. Each excerpt runs from the enclosing definition to the read, at most
  8 lines. A chunk gets at most 6 such entries and tries at most 24 files,
  within the same 16-read chunk cap and 200-read run cap, and only after the
  other sources have made their reads. The entries form a new last prompt
  block, `### Code elsewhere that reads state this chunk writes`, share the
  chunk's `PRXREF_REPO_CONTEXT_MAX_CHARS` budget, and rank last, so the
  budget cuts them first. An excluded path is never read. In the run record
  their kind is `reader` and their reason `shared-state`. `off` and `diff`
  get no such block.
- **Default-on toggles pinned off in tests (#22).** A new deterministic check,
  always on, posts one `warning` at confidence 1.0 when the pull request adds
  a toggle whose default is on and also adds a line to a suite-wide test
  setup file (`conftest.py`, `setupTests.*`, `jest.setup.*` or
  `vitest.setup.*`) that turns it off: the passing suite then never runs the
  shipped default. The finding sits on the toggle's line, names every file
  that pins it, and ends with `(deterministic check, no model)`. See
  `docs/quality.md`.

### Changed

- **An object without `findings` is no longer a clean review (#21).** At the
  default `PRXREF_LLM_PARSE_RETRIES=1`, a reply such as `{}` costs a second
  call, and if that reply has no `findings` list either, the unit fails with
  `worker review JSON has no findings list`, where 0.16.0 counted it as a
  review with no findings. A reply that does not parse, or is not a JSON
  object, also gets a second call, and the unit fails only when that reply
  cannot be used either. A custom worker or systemic template whose reply
  format drops `findings` fails every unit this way, and no template check
  catches it (see `docs/prompt-templates.md`). `0` restores the 0.16.0
  handling.
- **Java and Kotlin files are read at every level (#20).** Chunk context can
  read each changed Java or Kotlin file, and the build files its dependency
  walk tries, through the forge at the PR head whenever the forge can read
  files and the pull request has a head sha, with `PRXREF_REPO_CONTEXT` at
  `off` too. These reads are cached per run and are not counted against the
  repository-context read caps.
- **A Java file's own definitions are shown once (#20).** With a repository
  reader, the definitions a Java file of the chunk holds for the names its own
  added lines mention now come from chunk context only, and are no longer
  repeated as `diff-file` repository-context entries; Kotlin files work the
  same way. A Java or Kotlin file of the chunk whose own added lines mention
  every name the cross-chunk search wants is no longer read by that search.
- **`read_cap_hit` can be true in more runs (#22).** At `repo`, the
  shared-state search spends the reads the other sources leave, and a read the
  cap refuses counts, so `read_cap_hit` in the run record, the `repo_context`
  trace event and `cap_hit=yes` on the `-v` line can be true in a run where
  the definitions and contract excerpts alone fit the cap.

### Fixed

- **A definition from another file of the same chunk.** With a
  repository reader, repository context at `diff` and `repo` now finds a
  definition that one Python or JavaScript/TypeScript file of a chunk
  references and another file of the same chunk defines outside its hunks.
  0.16.0 left the chunk's own Python and JavaScript/TypeScript files out of
  that search entirely.
- **A deterministic check's finding keeps its own severity.** Severity
  consistency could raise the release-shape or pinned-off toggle finding to
  the severity of a model finding in the same file that shared a rare code
  token with it, or of a model finding with the same normalized title, so a
  finding that ends `(deterministic check, no model)` could post at a
  severity a model chose. The toggle finding was seen posted as an `error`
  that way. Both checks' findings now take no part in severity consistency:
  they are never raised, never raise another finding, and their text no
  longer counts toward a code token's rarity.

### Known limitations

- **The follow-up lookup of #22 is not built.** Part 1 of #22, one bounded
  extra call that looks up the symbol a below-floor finding says it could not
  see, is deferred, and #22 stays open.
- **A Java or Kotlin file can be fetched twice.** Chunk context and
  repository context keep separate readers with no shared cache, so a file
  both of them read costs two requests per run, as 0.16.0's dependency and
  same-file blocks already did for other languages.
- **Kotlin standard-library names cost lookups.** Names such as `Int`, `Unit`
  or `Pair` are not in the built-in list of platform names repository context
  ignores, so at `repo` each one a chunk mentions is looked for by the
  file-name search, as a project type would be. Repository context has no
  Kotlin import or path rules: outside the diff, a Kotlin type is found by the
  file-name search alone.
- **Precompiled Gradle script plugins are treated as Kotlin.** Only
  `build.gradle.kts` and `settings.gradle.kts` are skipped by the dependency
  lookup; another `*.gradle.kts` file is handled like any Kotlin source, so its
  Gradle API imports cost a walk for the nearest build file.
- **Some dependencies are not matched or not resolved.** An artifact whose
  groupId is not a prefix of its packages gets no line: Guava, Lombok, JUnit 4,
  Spring Boot starters and kotlinx among them. Gradle map notation
  (`group: 'g', name: 'a'`) is not read, and a version set through a variable
  or `gradle.properties` is not resolved.
- **A truncated review reply is not retried.** A reply stopped at
  `PRXREF_LLM_MAX_TOKENS` that cannot be used fails the unit on the first
  call, with the budget named, whatever `PRXREF_LLM_PARSE_RETRIES` says.
- **The timeout retry records its own parse retries only.** When a chunk is
  run again after a timeout, only the second run's `parse_retries` is
  counted, the same gap its token counts and cost have.
- **`score.json` does not total the reviews' parse retries.** It counts the
  judge's; each case's own run record carries its review's `parse_retries`.
- **The shared-state search matches names, not types.** A reader is any line
  that reads a key of the same name in a file of the same language, so an
  unrelated `.data` or `table` elsewhere can fill an entry, and a reader in
  another language is never found.
- **The toggle check needs both lines in the pull request.** A new pin on a
  toggle that already existed, or a new toggle that an existing setup line
  pins, is not reported, and neither is a pin outside the four suite-wide
  setup file names.

## [0.16.0] — 2026-09-25

The repository-context release (#17). A chunk worker can now see code outside
its own hunks: definitions from the pull request's other files and from files
the diff never touches, and excerpts of the API and database contracts a
change points at. It is off by default. `PRXREF_REPO_CONTEXT=diff` adds
definitions found in the pull request's own files, and `repo` also reads the
rest of the repository, through the forge or through a local checkout given
with `--repo-dir`. At `off`, the prompts, posted comments, trace and logs are
byte-identical to 0.15.0; the run record and `--format json` gain one key,
`repo_context`, which is `null`.

### Added

- **Repository context (#17).** `PRXREF_REPO_CONTEXT` sets how much of the
  repository a chunk worker sees beyond its own hunks: `off` (the default),
  `diff` or `repo`. `diff` shows the definitions that a chunk's added lines
  reference from the pull request's other files and, for a type another chunk
  changes, the changed lines inside it. Those files are read at the PR head
  through the forge or `--repo-dir`; with no reader, the entries come from the
  diff's hunk lines alone. `repo` also reads files outside the diff:
  definitions found through a file's imports, through Java's same-package
  path convention and by a search of the repository's file names, plus
  contract excerpts. Definitions extend the prompt's `### Definitions
  referenced by this chunk` block, and contract excerpts form a new
  `### Contract excerpts` block after it. Only chunk workers get repository
  context: the whole-PR sweep prompt is unchanged. At `repo`, a run with no
  repository reader, or with no file listing, logs one WARNING saying what it
  runs without. Matching is exact and case-sensitive, and any other value
  exits 2 naming the variable.
- **Java definitions (#17).** 0.15.0 showed a chunk the definitions it
  references for JavaScript, TypeScript and Python only. At `diff` and `repo`,
  Java type declarations (`class`, `interface`, `record`, `enum` and
  `@interface`, never a method or a field) are found too, in the chunk's own
  Java files outside its hunks as well as in other files. At `off`, Java still
  gets none.
- **`PRXREF_REPO_CONTEXT_MAX_CHARS` (#17).** The repository-context entries of
  one chunk share a character budget, default `12000`, which must be greater
  than 0. An entry costs the length of its `path:line: text` line. Entries are
  admitted in the rank order of their `reason`: `cross-chunk`, `contract`,
  `diff-file`, `import`, `path-convention`, then `name-search`, and within a
  rank by path and line. Admission stops at the first entry that does not
  fit, and a `… N more context entries omitted` line, not counted against the
  budget, ends the block of the first entry left out.
- **Contract excerpts and `PRXREF_CONTEXT_CONTRACT_GLOBS` (#17).** At `repo`, a
  chunk whose added lines use a route, an operation id, a table, or a name
  that matches a schema gets the matching slice of the repository's contract
  files: an OpenAPI operation or schema, a JSON Schema, or an earlier
  migration of a changed one. `PRXREF_CONTEXT_CONTRACT_GLOBS` picks the
  contract files once per run; its built-in set is `**/openapi*.y*ml`,
  `**/openapi*.json`, `**/openapi/**`, `**/swagger*`, `**/*.schema.json`,
  `**/db/changelog/**`, `**/db/migration/**` and `**/migrations/**`. A set
  value replaces the built-in set instead of adding to it, and an empty value
  keeps it. A chunk reads at most 6 spec files (`.yaml`, `.yml` or `.json`)
  and, for each changed migration, at most the 4 nearest earlier migrations in
  its directory; each excerpt is capped at 40 lines and 2,000 characters.
- **`PRXREF_CONTEXT_EXCLUDE_GLOBS` and an exclude floor (#17).** Repository
  context never lists, reads or shows a path under a floor that is always on:
  `**/expected.json`, `**/cases.json`, `**/case.json`, `**/prxref-eval/**`,
  `**/.env*`, `**/*.pem` and `**/*.key`, which keeps eval labels, eval output,
  dotenv files and keys out of the prompt. `PRXREF_CONTEXT_EXCLUDE_GLOBS` adds
  globs to that floor; a `!` negation in it never re-admits a floor path.
  Neither list hides a changed file's hunks, which still reach the prompt as
  the diff.
- **Bounded repository reads (#17).** One reader serves the whole run: each
  path is fetched at most once, and the file listing once. Reads of paths
  outside the pull request's own files are capped at 16 per chunk and 200 per
  run; a read past a cap is skipped, so the chunk gets fewer entries, and the
  run record's `read_cap_hit` says so. The pull request's own files are read
  without a cap, so the entries built from them never depend on which chunk
  read a file first.
- **`--repo-dir PATH` (#17).** `prxref review --repo-dir PATH` names a local
  checkout of the repository at the PR head. With `PRXREF_REPO_CONTEXT=repo`,
  repository context reads and lists files there instead of calling the forge
  (at `diff` it only reads there), so a `--diff-file` review gets repository
  context with no network. A path that is not an existing directory exits 2
  before any network call. It is not a replay flag: on its own it neither
  stops posting nor adds a `replay` stamp.
- **Repository context in `prxref eval` (#17).** A `cases.json` case takes an
  optional `repo_dir`, read relative to the file, and a `case-*/` directory
  takes a `repo/` directory; either is the case's `--repo-dir`. A `repo_dir`
  that is not an existing directory exits 2 naming it. `run.json` records the
  four new settings, so two runs that differ only in `PRXREF_REPO_CONTEXT` can
  be told apart and compared.
- **`repo_context` in the run record (#17).** The run record and
  `--format json` gain `repo_context`, always present and `null` at `off`. On,
  it holds the level, the budget, both glob lists, the reader (`forge`,
  `repo-dir` or `null`), the listing's path count and whether it is complete,
  the read count, `read_cap_hit`, and one row per chunk naming each admitted
  entry's path, line, symbol, kind, reason and length, how many entries were
  left out, and whether the timeout retry dropped them. It never holds file
  text. The JSONL trace gains one `chunk context` event per chunk and one
  `repo_context ok` event per run, and in text output `-v` prints a
  `repo context:` line with the level, the reader, the listing size, the read
  count, whether a cap was hit, and the entries admitted and left out. With
  the feature off none of them appears.
- **Repository listing on every forge (#17).** Each forge adapter can now list
  the repository's files at a commit, which `repo` does once per run. GitHub
  reads `GET /repos/{owner}/{repo}/git/trees/{sha}?recursive=1` in one request.
  GitLab walks GraphQL's `project.repository.tree(recursive: true).blobs`, 100
  files a page, and falls back to the REST `repository/tree?recursive=true`
  walk when the first GraphQL page is unusable. Bitbucket Cloud walks
  `/2.0/repositories/{owner}/{repo}/src/{sha}/?max_depth=64&pagelen=100`,
  Bitbucket Server walks `/rest/api/1.0/projects/{key}/repos/{slug}/files` at
  the commit, 100 paths a page, and Azure DevOps reads the `items` endpoint
  with `recursionLevel=Full` at the commit in one request. A paged walk stops
  at 20 pages. Only files are listed: directories and submodules are dropped,
  though whether Bitbucket Server returns submodules is unverified. A listing
  that stopped early is marked incomplete (`complete: false` in the run
  record, `(partial)` on the `-v` line), a listing whose first request fails
  is none at all, and neither fails a review. See `docs/forges.md`.

### Fixed

- **An empty model reply is now asked for once more.** A reply with no text, or
  only whitespace, is sent again once with the same prompt and the same
  budget, after one WARNING (`<unit>: empty model reply
  (finish_reason=<reason>); retrying once`). This covers chunks and the
  systemic sweep. A reply the provider stopped at the budget (`finish_reason`
  `length` or `max_tokens`) is not retried, because it already names
  `PRXREF_LLM_MAX_TOKENS`. Neither is a non-empty reply that fails to parse,
  nor a call that raised. Token counts and elapsed time cover both calls, and
  the reported cost is their sum, or unknown when either call reported none.
  In 0.15.0 and earlier the unit failed with `no parseable content` even
  though the provider billed it; the review of 0.15.0's own pull request lost
  1 of 8 chunks this way.

### Known limitations

- **Read caps cut the lowest-ranked sources first.** A chunk issues its capped
  reads in rank order, contract files before the files that imports, the path
  convention and the name search point at, so those definitions are the first
  left out once a chunk has made 16 reads or the run 200, and which chunk
  meets the run cap first depends on thread scheduling.
- **The name search matches file names, not file contents.** Outside the pull
  request, a definition is found only through an import, the Java path
  convention, or a same-language file whose name, less its extension, matches
  the referenced name, so a type declared in a differently named file is not
  shown.
- **A file over 512 KiB is read as missing.** Every forge and `--repo-dir`
  refuse a file past that size, so a large OpenAPI spec gives no excerpt.
- **A large repository is listed only in part.** A paged listing stops at 20
  pages and GitHub truncates the tree of a very large repository, so there the
  name search and the contract globs see only the files listed; on a large
  GitLab project the listing can take about a minute, once per run, at `repo`
  only.
- **The Bitbucket Server listing is untested live.** It is built from the
  Bitbucket Data Center REST documentation, tested against fakes of that
  shape, and has not been run against a live instance.
- **A chunk that times out loses its repository context.** The timeout retry
  drops the definitions and contract blocks to shrink the prompt, so with a
  model slower than `PRXREF_LLM_TIMEOUT` (default `45` seconds) a chunk is
  reviewed without them; raise the timeout for a slow model.
- **A pull request's file can be fetched twice.** The dependency and same-file
  definition blocks of 0.15.0 keep their own reader, so a file both they and
  repository context read costs two requests per run.

## [0.15.0] — 2026-09-24

The tuning release. A team can now replace the review prompts, scope its review
rules to paths, fold findings that break the same rule into one comment, cap
warnings and minor findings, cap how many findings one rule may produce, and
measure a configuration against labelled human findings with `prxref eval`. A
`--pr-url` replay shows the title and description the PR had at review time,
and GitHub pull requests past GitHub's diff size limit are reviewed. Each new
option is off until you configure it, except the per-rule cap, which loading a
review rules file turns on.

### Added

- **Reworded-duplicate dedup (#10).** Set `PRXREF_DEDUP_SIMILARITY` (above 0,
  at most 1.0) to make the sweep dedup also drop reworded restatements: two
  active findings in the same file and on the same line whose titles reach that
  Jaccard similarity over a dedicated title tokenizer, and share at least 3
  title tokens, count as one. Across the chunk/sweep boundary the chunk copy
  always survives, and the sweep copy is dropped only when it is no more
  severe, so the review's worst severity never drops. On one side of the
  boundary the more severe copy is kept, then the more confident one.
  File-level findings are never compared. The dropped copy is kept for audit in
  the run record, with the drop reason `duplicate of chunk finding (reworded,
  similarity 0.57)` or `duplicate of sweep finding (reworded, similarity
  0.57)`. It is an environment variable only, with no CLI flag, and the webhook
  server reads it the same way. Unset, the default, only the exact-title dedup
  runs, as before.
- **Prompt-template overrides (#11).** `PRXREF_PROMPTS_DIR`, or `--prompts-dir
  DIR` for one run, names a directory that may replace any of `worker.md`,
  `systemic.md` and `summary.md`; a template it lacks falls back to the packaged
  one. The worker template reaches every chunk, the systemic template the
  whole-PR sweep, and the summary template every posted summary, the empty-diff
  summary and the inline-accounting re-post included, but not the error notice.
  Overrides are checked before any network call. A review template must keep
  the `## Review Context` marker and every packaged placeholder below it
  (`{scope_example}` and `{rule_example}` are optional), and `summary.md` must
  keep `{findings}`. A URL, a missing directory, a template over 256 KiB, text
  that is not UTF-8 or holds NUL bytes, and a symlink out of the directory are
  each a configuration error (exit 2) naming the flag or the variable. An
  unknown placeholder, a placeholder above the marker and an unrecognised file
  each log a warning. The flag wins over the variable, `--prompts-dir ""` turns
  it off, and the webhook server re-reads the variable on every webhook. The
  run record and `--format json` gain `prompt_templates` (always present,
  `null` when unset), holding the path, raw-byte SHA-256 and character count of
  each template file the directory holds, edited or not; the trace gains a
  `prompts ok` event, and `-v` prints a `prompts:` line with the first 12
  characters of each hash. See `docs/prompt-templates.md`.
- **`prxref prompts export DIR [--force]` (#11).** It writes the packaged
  `worker.md`, `systemic.md` and `summary.md` into `DIR` byte for byte, as the
  starting point for an override directory, creating `DIR` when missing and
  printing each path it wrote. If any of the three already exists, nothing is
  written and the command exits 2 naming the file; `--force` overwrites. The
  `prxref eval` judge prompt cannot be overridden and is not exported.
- **Path-scoped review rules (#12).** `PRXREF_SCOPED_RULES`, or the repeatable
  `--scoped-rules PATH` that replaces it for one run (`--scoped-rules ""` turns
  it off), names rules files and directories. A directory is read one level
  deep for `*.md` files in name order, and a run loads at most 50 files. Each
  file's `applies_to:` front matter (alias `applyTo`, the `.github/instructions`
  spelling) decides which chunks receive it: a glob, a comma-separated string,
  a one-line `["...", "..."]` list, or indented `- <glob>` lines. Globs match
  the whole path case-sensitively, `*` crosses `/`, a `!` glob excludes
  wherever it sits in the list, and `**/` also matches zero directories, so
  `**/*.java` selects a root-level `Foo.java`; a renamed file's old path
  selects too. A file without the key reaches every chunk, and the whole-PR
  sweep receives the union. Scoped files add to the always-on
  `PRXREF_REVIEW_RULES` file, and their `severity:` maps merge into one
  run-wide map, which applies even without an always-on file; one word mapped
  to two tiers across the files is a configuration error. Each file is capped
  at `PRXREF_REVIEW_RULES_MAX_CHARS`, and `PRXREF_SCOPED_RULES_MAX_CHARS`
  (default 24000) caps the scoped text one review unit receives: files go in
  whole in load order, the first that does not fit is truncated, and later ones
  are omitted behind a marker, with one WARNING per run. Every file is checked
  before any network call. A URL, a path that escapes the working directory,
  text that is not UTF-8 or holds NUL bytes, and malformed front matter
  (`applies_to: []`, an entry that is not a string, a glob starting with `/`,
  or only `!` globs) each exit 2 naming the flag or the variable, the path and,
  where known, the line. The run record and `--format json` gain
  `scoped_rules` (`null` when off; otherwise the entries, each file with its
  SHA-256 and globs, the per-unit cap, and the files each unit received), `-v`
  prints a `scoped rules:` line, and the trace gains a `scoped_rules ok` event
  and a `rules` field on each `chunk start` and `sweep start` event. The
  webhook server re-reads the variable and the files on every webhook. See
  `docs/review-rules.md` "Path-scoped rules".
- **Finding grouping (#13).** With `PRXREF_GROUP_FINDINGS=1`, chunk findings
  in one file that break the same rule, or that name no rule and share a
  normalized title, fold into one comment at the group's first line. That
  comment takes the group's highest severity and highest confidence and ends
  with `Also at:` and the group's other lines as `file:line`; the other
  findings are kept for audit with the drop reason `grouped into
  <file>:<line>`. The worker and sweep prompts ask the model to name, in a
  per-finding `rule` key, the team review rule or standard a finding applies,
  and the example finding shows the key through a new optional
  `{rule_example}` slot in both packaged templates. A rule is kept as a
  one-line label of at most 120 characters with whitespace collapsed, and
  compared without regard to case. It is dropped, never truncated, when it is
  not a string, is empty or too long, or holds a control, surrogate or
  private-use character or a bidi embedding, override or isolate control
  (U+202A to U+202E, U+2066 to U+2069); a zero-width joiner or non-joiner is
  kept. A prompt override without the slot still loads and still asks for a
  rule, but its example shows no `rule` key, and a review with grouping on logs
  one WARNING naming each such override file; re-export with `prxref prompts
  export DIR --force` and re-apply your edits to pick the slot up. Grouping
  runs before the finding caps, so every cap, `PRXREF_MAX_ERROR_FINDINGS`
  included, counts a group once. Whole-PR sweep findings are never grouped,
  and a sweep finding that restates a grouped finding is still dropped as
  `duplicate of chunk finding`. Off by default: unless a review rules file
  turns on the per-rule cap (#18), the model is not asked for a rule, and
  output is unchanged apart from the `rule` and `locations` JSON keys being
  `null`.
- **Per-severity finding caps (#13).** `PRXREF_MAX_WARNING_FINDINGS` and
  `PRXREF_MAX_OUTOFSCOPE_FINDINGS` cap how many `warning` and how many
  `outofscope` findings one review posts, ranked like
  `PRXREF_MAX_ERROR_FINDINGS`: the most confident survive, then by file and
  line. The rest are kept for audit with the drop reason `warning cap exceeded
  (max N)` or `outofscope cap exceeded (max N)`, and `0` drops every finding of
  that severity; `spec` findings are never capped. `outofscope` here is the
  minor severity (style and nits), not the ticket scope `out`: a finding the
  ticket puts out of scope is capped by its severity like any other. Both caps
  also apply to a release-only PR's summary-only review. Under
  `PRXREF_FAIL_ON=any`, a cap of `0` can turn exit 1 into exit 0, since a cap
  narrows the gate and never widens it. Unset means unlimited.
- **`rule` and `locations` in the output (#13).** Every `--format json`
  finding row carries two more keys after `scope`: `rule`, the rule the finding
  names (`null` when it names none), and `locations`, which on a grouped
  finding lists the other places its `Also at:` paragraph names as `{"file",
  "line"}` objects, in that order. The per-rule cap (#18) sets it too, on the
  best finding of each rule it folds, and it is `null` on every other row,
  including each member dropped as `grouped into <file>:<line>`. In text
  output, a finding that names a rule ends its line with ` [rule: <rule>]`,
  after any scope tag.
- **`prxref eval run` (#14).** `prxref eval run --cases PATH --label NAME
  [--out DIR] [--resume]` replays every labelled case in process, never
  posting, and writes the run to `DIR/NAME/` (default `--out` is
  `./prxref-eval/`). Cases come from a `cases.json` file (`{"version": 1,
  "cases": [...]}`) or a directory of `case-*/` directories in the
  `tests/evals` layout, and every case is validated before the first review; a
  bad one exits 2 naming `--cases`, the case and the field. `--rules-file`, the
  repeatable `--scoped-rules` and `--prompts-dir` give every case the same team
  rules, path-scoped rules and prompt templates, as `review` takes them, and
  `PRXREF_SCOPED_RULES` and `PRXREF_PROMPTS_DIR` reach every case unless those
  flags override them. `run.json` records the case ids, the SHA-256 of each
  packaged prompt template and any overrides, the reviewer's sampling, review
  rules and scoped rules, and an allowlist of non-secret settings, so no
  credential is written; each case keeps its `--format json` record and a
  trace. A crash or a per-case configuration error is recorded as that case's
  `error.json`, the next case still runs, and the command exits 0. A bad
  `--label`, or an existing run without `--resume`, exits 2 naming the flag,
  and `--resume` skips every case already recorded. A bare `prxref eval`
  prints usage and exits 2, and `eval` adds no environment variable. See
  `docs/evals.md`.
- **`prxref eval score` (#14).** `prxref eval score --label NAME
  [--judge-model MODEL] [--out DIR]` grades a run against its human labels and
  writes `score.json` and `score.md` into the run directory. A label with a
  `must_match` predicate is graded in code with no LLM call: the same file,
  within 5 lines, and a match on the finding's title and body (a plain value
  ignores case and markup, a `re:` pattern is case-insensitive). Every other
  label goes to one single-shot judge call per case, which grades it `full`,
  `partial` or `none` under a versioned judge prompt. The judge runs on the
  review's own backend, base URL and credentials with only the model changed;
  `--judge-model` is required whenever such a label exists, and leaving it out
  exits 2 before any call. Judge replies are cached under
  `DIR/NAME/judge-cache/`, so a rescore makes no call, and a failed or
  malformed judge reply counts as `judge_error`, left out of every denominator
  rather than scored as a miss. One AI finding credits at most two labels, and
  a grouped finding is credited at each of its locations in both tiers. A case
  whose review failed is scored with every label a miss. The report gives
  micro recall with per-case rows, recall by severity, by category and over
  accepted labels (a `partial` grade counts 0.5), unmatched AI findings per
  PR, severity agreement (a human `minor` counts as `warning`), chunks failed,
  elapsed time, and review and judge cost, where an unknown cost is shown as
  unknown and never summed. A judge model that is also a reviewer model logs a
  warning and is stamped `self_judged`.
- **`prxref eval compare` (#14).** `prxref eval compare A B [--out DIR]`
  compares two scored runs, each named by a label under `--out` or by a run
  directory; a label wins over a same-named directory. It prints a Markdown
  report with no timestamps, so the same two runs always give byte-identical
  output: a metrics table with A, B and the change (recall in percentage
  points, and `unknown` rather than a number when either side is unknown),
  every label whose grade or credit changed, and the cases and labels only one
  run has. It warns when the runs used a different judge prompt or judge
  model, cover different cases, or carry different replay `description`
  stamps for a case, since a pinned run against a live one is not a
  like-for-like comparison. A run with no `score.json` exits 2 naming `A` or
  `B`.
- **Replay description flags (#16).** `prxref review --as-of TIME`,
  `--description-file PATH` and `--no-description` choose which PR description
  a replay shows. Each makes the run a replay with posting off, and they
  exclude each other: giving two or more exits 2 naming each one. `--as-of`
  takes an ISO-8601 time with a UTC offset (`2026-05-01T09:30:00Z`) and needs
  `--pr-url`; a date alone or a time without an offset exits 2 rather than
  being read in the local time zone. `--description-file` is read like
  `--rules-file`, so a missing, unreadable or non-regular file, text that is
  not UTF-8 or holds NUL bytes, and a path under the working directory that
  symlinks out of it each exit 2 naming the flag. `--no-description` reviews
  with an empty description.
- **Replays record the title and description they showed (#16).** The
  `replay` stamp in the run record and `--format json` gains `description`
  (`pinned`, `live`, `file` for `--description-file` or a `--diff-file` without
  `--pr-url`, `none` for `--no-description`), `as_of` (the cutoff as a UTC
  ISO-8601 time ending in `Z`, else `null`) and `as_of_source` (`flag`,
  `first-review` or `head-commit`, else `null`). All seven keys are present on
  every replay, and an `as_of` passed back as `--as-of` names the same instant.
  The text `replay:` line gains `description=<status>` and, when a cutoff was
  chosen, `as_of=<time> (<source>)`.
- **Description history on GitHub and Bitbucket Cloud (#16).** The GitHub
  adapter reads a pull request's description versions, title renames, first
  human review or comment (not by the author, a bot or prxref) and head commit
  date through GitHub's GraphQL API, which refuses anonymous reads, so it needs
  `PRXREF_GITHUB_TOKEN` (or `PRXREF_GITHUB_ENTERPRISE_TOKEN` on GitHub
  Enterprise Server). The Bitbucket Cloud adapter reads the same from the pull
  request's `/activity` feed and the head commit. A history that cannot be
  trusted whole, such as a feed longer than the page budget, an unreadable
  change entry or edits that do not chain to the live text, pins nothing, and
  the replay keeps the live text. A custom forge can take part by implementing
  the optional `get_pr_history` method, which returns a `PRHistory`. See
  `docs/forges.md`.
- **Per-rule cap (#18).** `PRXREF_MAX_FINDINGS_PER_RULE` (default `2`, `0` turns
  it off) caps how many findings one rule may produce in a review, across files.
  It applies only while a review rules file is loaded (`PRXREF_REVIEW_RULES` /
  `--rules-file` or `PRXREF_SCOPED_RULES` / `--scoped-rules`) and does nothing
  without one. While it applies, every review unit is asked to name the rule
  each finding applies, as under `PRXREF_GROUP_FINDINGS`, including that
  feature's WARNING for a prompt override without the `{rule_example}` slot.
  Active chunk findings at or above the confidence floor count together when
  they name the same rule, compared without regard to case, or, when they name
  none, share a normalized title; the two never mix. Of each rule, only as many
  as the cap allows stay active: the best, ranked by severity, then confidence,
  each keeping its own severity and confidence, so an error is never folded
  under a warning. The rest fold onto the best one: its body ends with `Also
  at:`, naming at most 5 locations, then `(+k more)`, and its `--format json`
  `locations` lists every one, across files. Each folded finding is kept for
  audit with the drop reason `rule cap exceeded (max N): listed at
  <file>:<line>`. A group from `PRXREF_GROUP_FINDINGS` counts once, and the cap
  runs after grouping and before the severity caps, so
  `PRXREF_MAX_ERROR_FINDINGS` and the per-severity caps count what it kept.
  Whole-PR sweep findings are never counted or capped. The run record and
  `--format json` gain `rule_counts`, right after `scoped_rules`: one `{"rule",
  "kind", "total", "kept"}` row for each rule or title that two or more findings
  share, `[]` when none does, and `null` when the cap did not run. One INFO line
  and one `rulecap ok` trace event report each pass. It is an environment
  variable only, with no CLI flag; a negative or non-integer value exits 2
  naming it, and `prxref eval run` records it in `run.json`.

### Changed

- **A `--pr-url` replay shows the title and description in force at review
  time (#16).** The cutoff is the `--as-of` time, else the first human review,
  else the head commit's date. When the forge cannot read description history
  (GitLab, Azure DevOps and Bitbucket Server cannot), the read fails, or the
  history does not reach the cutoff, the replay falls back to the current title
  and description and logs a WARNING saying why; an explicit `--as-of` on a
  forge that cannot read description history exits 2 naming `--as-of`.
  `--description-file` and `--no-description` read no history, and the webhook
  server never replays. Every `prxref eval run` case is a replay, so it pins
  too. In 0.14.0 a replay always showed the current title and description.
- **An `applies_to:` key in the always-on rules file logs a WARNING (#12).**
  The `PRXREF_REVIEW_RULES` / `--rules-file` file still ignores an
  `applies_to:` or `applyTo:` key and still reaches every unit, but it now
  names that key in a WARNING instead of the INFO line for ignored keys. Its
  prompts are unchanged.
- **A review rules file now turns on the per-rule cap (#18).** With a review
  rules file loaded (`PRXREF_REVIEW_RULES` or `PRXREF_SCOPED_RULES`), every
  review unit is asked to name the rule each finding applies, and each rule's
  findings past the second fold into its best one by default, so the worker and
  sweep prompt hashes and the finding counts of such runs change.
  `PRXREF_MAX_FINDINGS_PER_RULE=0` turns both off: the prompts, findings and
  comments are then what they were without the cap, and the run record and
  `--format json` carry `rule_counts` as `null`. With `PRXREF_GROUP_FINDINGS=1`
  the model is still asked for a rule, since grouping asks for one itself.

### Fixed

- **GitHub pull requests past the diff size limit are reviewed (#15).** GitHub
  refuses the unified diff of a pull request past 20,000 lines or 300 files
  with HTTP `406` (`too_large`), and every earlier release ended such a review
  with verdict `Error`. prxref now reads GitHub's compare diff (`base...head`)
  instead and accepts it only when its file and line counts match the pull
  request's. On a mismatch or a failed request it logs one WARNING and falls
  back to the paged `/pulls/{number}/files` listing, which fails closed: past
  GitHub's 3,000-file listing cap, or when the listing's line totals disagree
  with the pull request's, the review ends as `Error` rather than reviewing
  part of the pull request. A listed file without a patch is reviewed
  header-only, with a warning when GitHub withheld a patch that has changed
  lines (commonly a large lockfile). Any other `406` fails as before, a pull
  request under the limit makes the same single request as before, and pinned
  `--base-sha`/`--head-sha` replays already read the compare diff and are
  unchanged.
- **GitHub reviews get full-file context.** The GitHub adapter took GitHub's
  raw file media type, `application/vnd.github.raw+json`, for a JSON envelope
  and dropped every file it read, so GitHub reviews got none of the full-file
  context (dependency versions and symbol definitions) the other forges
  already had. It now decides by the media type, not by a `json` substring:
  GitHub's raw variants and `text/*` are read as the file, while a JSON
  envelope such as a directory listing still returns nothing. Releases 0.12.0
  through 0.14.0 are affected too.
- **A finding that copies the prompt's example finding is dropped.** A new
  deterministic pass compares each chunk and sweep finding's normalized title
  with the example-finding titles of the worker and sweep templates the run
  used, packaged or overridden, and drops an exact match with the drop reason
  `echoes the prompt's example: "<title>"`; a near-miss title is kept. It is
  the first pass that drops, so an echo never anchors a group, raises another
  finding's severity or takes a cap slot. A run with an echo logs one INFO line
  and emits one `prompts echo` trace event, and a run without one is
  unchanged.
- **The worker prompt no longer promises a size.** It told the model its input
  "stays under roughly 30k tokens", which nothing guarantees: the chunk budget
  is an estimate, and `PRXREF_MAX_CHUNKS` overflow can grow a chunk past it.
  The sentence now reads "The diff below is the complete chunk.", so worker
  prompt hashes change.
- **A whole-PR sweep finding no longer repeats a folded chunk finding.** When
  finding grouping (`PRXREF_GROUP_FINDINGS=1`) or the per-rule cap (on by
  default with a review rules file) folded a chunk finding, a field-identical
  sweep finding was taken for a chunk finding, escaped the sweep dedup and was
  posted as a second comment. It is now dropped as `duplicate of chunk
  finding`. The same fix covers two older paths that 0.14.0's code has too: a
  severity the model wrote in another case (`Warning`) let the sweep copy
  through, and a finding that a severity cap (`PRXREF_MAX_ERROR_FINDINGS`, and
  now the per-severity caps) kept could be dropped as a duplicate of another
  finding with the same title in the same file, and so was never posted.
- **The documentation matches the 0.15.0 code.** Among the corrections: the
  run record stamps every template file in the prompts directory, edited or
  not; `PRXREF_SCOPED_RULES_MAX_CHARS` cuts the overflowing file to the room
  left, or leaves it out; `--trace-dir` numbers chunks from 0 while the JSONL
  trace numbers them from 1; and a `live` replay stamp still records `as_of`
  when a cutoff was chosen.

### Known limitations

- **Reworded-duplicate dedup is untuned.** `PRXREF_DEDUP_SIMILARITY` has no
  default and no recommended value: no threshold has been measured on real
  review data. A live check found no candidate pair in eight runs, so the tier
  has not yet been seen to fire on a real pull request.
- **A chunk receives the scoped rules of every file it holds.** Chunks are
  filled by token budget and file count, preferring the chunk whose files share
  the deepest directory, not by language, so a chunk holding both a Java and a
  TypeScript file receives both files' scoped rules.
- **A finding without a rule groups by title only.** Grouping keys on the rule
  the model names. A finding with no rule, or whose label was dropped, groups
  only with other rule-less findings of the same normalized title, and never
  joins a group whose findings name a rule, even with the same title in the
  same file.
- **A group member moved to file level adds no `Also at` location.** When line
  alignment moves a member to file level, it is still folded into the group,
  but its place is not listed.
- **A capped group loses all its locations.** The finding caps rank by
  confidence, then file and line, not by group size, so when a cap drops a
  group's comment, every location folded into it leaves the posted review too.
  The same holds for the finding the per-rule cap folded a rule's other
  findings into.
- **A finding on a file outside its chunk has a guessed line.** A chunk worker
  also sees the PR's other files, as short excerpts without line numbers under
  `### Other files changed in this PR`, and can report on them. Line alignment
  snaps such a finding to an added line of that file within 5 lines, else
  moves it to file level. It can restate what the chunk holding that file
  reported: grouping merges the two only when they name the same rule or share
  a title, and the reworded-duplicate dedup compares only findings on the same
  line, never a file-level one.
- **The eval judge shares the review's backend.** It runs on the review's own
  backend, base URL and credentials, and `--judge-model` picks only the model.
  Self-judging is caught only when the judge's model name matches a reviewer
  model's, ignoring case: it logs a warning and is stamped `self_judged`, not
  refused, and another model of the same family passes unremarked. Scoring
  needs human-labelled cases; the repository ships three.
- **Some very large GitHub pull requests still fail.** When the compare diff
  cannot be used, a pull request past GitHub's 3,000-file listing cap, or one
  whose listing's line totals disagree with the pull request's because GitHub
  withheld patches, ends as `Error` instead of being reviewed in part. A file
  whose patch GitHub withholds while keeping its line counts is reviewed
  header-only.
- **A push during a large GitHub review can fail it.** On the file-listing
  path, a push between the read of the pull request and the listing pages makes
  the line totals disagree, and the review ends as `Error`; it is never
  reviewed in part.
- **The large-PR fallback can be slow.** A compare read that times out is
  retried under the GitHub session's retry policy, so reaching the file listing
  can take several 30-second read timeouts.
- **Only GitHub and Bitbucket Cloud replays pin the description.** On GitLab,
  Azure DevOps and Bitbucket Server a `--pr-url` replay shows the current title
  and description with a warning, and `--as-of` exits 2.
- **Description history on GitHub Enterprise Server is untested.** Its GraphQL
  endpoint, `https://{host}/api/graphql`, has not been probed live.
- **Very busy GitHub pull requests replay the live text.** A pull request with
  more than 5,000 reviews, conversation comments or title renames keeps its
  current title and description, even under `--as-of`.
- **Unauthenticated Bitbucket Cloud replays can fall back to the live text.**
  Without a credential Bitbucket Cloud allows 60 API reads an hour, as
  observed (Atlassian can change it), and a history read costs at least three
  of them (the pull request, its activity feed and the head commit), so a
  rate-limited read keeps the live text.
- **Bitbucket Cloud's `changes_requested` activity entry is unverified.** It
  has not been seen in a live feed, so a change request that is the first human
  review may not set the cutoff.

## [0.14.0] — 2026-09-24

The inputs release. A review can now be grounded in the spec a PR implements,
follow a team's own review rules, and judge each finding against the ticket the
PR is for. A replay mode reviews pinned commits for evaluation, Azure DevOps
becomes the fifth forge, two backends run a review on your own Claude Code or
Kiro CLI login, and every run records its dollar cost and can flag an oversized
PR. Each new input is off until you configure it.

### Added

- **Spec-grounded review (`--spec`, `PRXREF_SPEC_SOURCES`).** Name web pages,
  local spec files or directories, and Jira ticket URLs with a repeatable
  `--spec URL_OR_PATH` or the list-valued `PRXREF_SPEC_SOURCES`. prxref fetches
  them, prunes them to a digest of the constraints relevant to this diff
  (`PRXREF_SPEC_MAX_CHARS` caps each fetched text, `PRXREF_SPEC_DIGEST_TOKENS`
  the digest), and puts the digest into every chunk worker's and the whole-PR
  sweep's prompt. A diff that breaks a quoted constraint draws a finding of the
  new 🔍 `spec` severity, ranked below `warning`. Spec findings are advisory:
  they never change the verdict or count toward the error cap, and
  `PRXREF_FAIL_ON=any` is the opt-in gate. A source that fails never blocks the
  review; the summary's grounding note lists it as `source N (kind): reason`.
  The digest keeps hard-wrapped statements whole, splits a long or multi-rule
  block into one MUST/SHOULD/MAY unit per sentence, files each constraint under
  its own section heading (Markdown, setext or HTML `<h1>`–`<h6>`), turns a
  version pin on a line of its own into a MUST, and ranks constraints by the
  diff's content words while ignoring normative ones such as `must` or
  `required`. Each URL or Jira source gets a 15 s socket timeout, a 30 s
  wall-clock budget and one retry with no backoff (`Retry-After` is ignored),
  and a page served without a charset is decoded by its `<meta>` tag, then as
  UTF-8, then as cp1252. See the README's "Review Against a Spec or Ticket",
  `docs/quality.md` "Spec grounding" and `docs/deploy.md` "Spec Sources in CI
  and on the Daemon".
- **A `spec` finding has to be earned.** A run counts as spec-grounded only when
  at least one constraint reached the prompts. When every source failed or none
  held a constraint, nothing is injected, the prompts say that no specs were
  provided, and a `spec` finding the model returns anyway is posted as a
  `warning` (logged at INFO and counted by a `specs relabel` trace event). On a
  grounded run the hedge gate skips the text that a finding's `Spec: "…"` quote
  copies verbatim from the digest, compared case-insensitively and in a finding
  of any severity, so a condition that belongs to the spec ("If a session
  already exists, the server MUST …") does not drop the finding as hedged. A
  quote the digest does not hold exempts nothing.
- **Jira tickets as spec sources.** A Jira issue URL (`/browse/KEY-1` or a REST
  issue URL, either one under a context path of up to two segments, a Cloud
  team-managed issue view, or a board URL carrying `selectedIssue`) is read from
  Jira REST, and the ticket's summary, type, labels and description lines rank
  ahead of every other constraint. `PRXREF_JIRA_BASE_URL`, `PRXREF_JIRA_EMAIL`
  and `PRXREF_JIRA_API_TOKEN` configure access (see Security for where the
  credentials go). A 401, a 403 or an anonymous 404 comes with a credentials
  hint, and a 200 that is not a JSON issue, such as an SSO login page, fails
  that source with a clean message.
- **Spec grounding in the log, the run record and the trace.** Each failed
  source logs one WARNING, `spec source N/T (kind, origin) failed
  (best-effort): reason`, in every run mode, with a URL origin cut to
  `scheme://host[:port]/path`. Every run with spec sources logs one INFO line,
  `spec grounding: ok/T source(s) ok, N constraint(s) injected`. The run record
  and `--format json` carry `spec_grounding` (`sources`, `ok`, `failed`,
  `constraints`, `digest_sha256`), and the trace's `specs` event is `ok`, or
  `fail` with the `reasons` when no source was fetched or the stage crashed.
- **Azure DevOps Repos forge (#2).** `prxref review` and `prxref serve` handle
  Azure DevOps Services (`dev.azure.com`, `*.visualstudio.com`) and Azure DevOps
  Server (any host, with the collection in the URL). Inline findings post as
  active threads, the summary is one closed PR-level thread that later runs
  update in place, and stale inline comments of prxref's own are pruned. Azure
  DevOps has no unified-diff endpoint, so the diff is rebuilt from the Diffs API
  (merge-base semantics) plus blob contents: pure renames and known-binary files
  are never downloaded, and a file past the per-blob, file-count or byte budget
  keeps its header without hunks. Authentication is `PRXREF_AZURE_DEVOPS_TOKEN`
  (a PAT), else `SYSTEM_ACCESSTOKEN` inside Azure Pipelines, else anonymous
  reads of a public project. The webhook server accepts the
  `git.pullrequest.created` and `git.pullrequest.updated` service hooks for an
  active PR, checking the HTTP Basic password against
  `PRXREF_AZURE_DEVOPS_WEBHOOK_SECRET` in constant time (unset, it answers 401
  unless `PRXREF_ALLOW_UNSIGNED=1`). The CLI help, the unrecognized-URL hint and
  the package description name Azure DevOps; setup is in `docs/forges.md`
  section 5 and `docs/deploy.md`.
- **Team review rules (#3).** `--rules-file PATH` or `PRXREF_REVIEW_RULES` names
  a Markdown or plain-text checklist. Its body goes into the system prompt of
  every chunk worker and the sweep, under a `## Team review rules` heading and
  inside `<team_rules>` tags, and `PRXREF_REVIEW_RULES_MAX_CHARS` (default
  12000) caps it, with a truncation line and one WARNING. An optional `severity:`
  front-matter block maps team words onto `error`, `warning` or `outofscope`
  (`blocker: error`), and a mapped word the model writes is rewritten before
  every quality pass instead of being dropped as an invalid severity; `spec` is
  not a target, and prxref's own severities cannot be remapped. Other
  front-matter keys are ignored, so a skill file works unmodified. The run
  record's `review_rules` holds the path, the raw file's SHA-256, the lengths,
  the truncation flag and the severity map, never the text; `-v` prints a
  `rules:` line, and the trace gains `rules ok` and `rules remap` events.
  `--rules-file ""` turns an environment-configured file off for one run, an
  unusable file exits 2 before any network call, and the webhook server
  re-reads the file for every review. See `docs/review-rules.md`.
- **Ticket context and a per-finding scope (#4).** `--context-file PATH` or
  `PRXREF_TICKET_CONTEXT_FILE` names the ticket a PR implements, and every
  finding is judged `in`, `out` or `unknown` against it. The ticket is quoted to
  the model as fenced, untrusted data, capped by `PRXREF_TICKET_CONTEXT_MAX_CHARS`
  (default 6000). An out-of-ticket finding is marked 🟦 in front of its severity
  glyph, listed last in the summary under **🟦 Outside the ticket (N)**,
  labelled `OUTSIDE TICKET` in its inline comment, yields inline slots to
  in-ticket findings of the same severity, and ends in `[scope: out]` in CLI
  text. Scope never feeds dedup, the error cap, the verdict or `PRXREF_FAIL_ON`.
  An empty file means "this PR has no ticket" and the summary says so, and a
  ticket without acceptance criteria (an `Acceptance criteria` or `Definition of
  done` heading or label, a task-list item, or a Gherkin `Given` … `Then`) gets
  a note that scope was judged from its description alone. Every finding in
  `--format json` carries `scope`, which stays `unknown` without a ticket; the
  record's `ticket_context` holds metadata only. The webhook server never reads
  a ticket file, and warns once at startup if the variable is set.
- **Replay mode for evaluation (#5).** `--base-sha` and `--head-sha` review a
  pinned commit range (the merge-base diff, with file context read at the
  pinned head), `--no-threads` hides the PR's existing discussion, and
  `--diff-file` reviews a diff file, with `--pr-url` or with no forge at all
  (a `git format-patch` mail's subject, body and author become the PR's title,
  description and author). A replay never posts, even for a library caller that
  passes `post=True`; a blank replay diff is an `Error` run rather than an
  `Approved` one; and the run record gains a `replay` stamp. Every built-in
  forge implements the new optional `Forge.get_compare_diff(ref, *, base_sha,
  head_sha)`, and `docs/forges.md` documents each forge's endpoint and caveats.
- **Subscription CLI backends: `claude-cli` and `kiro-cli` (#6).**
  `PRXREF_LLM_BACKEND=claude-cli` or `kiro-cli` reviews with your own installed,
  logged-in Claude Code or Kiro CLI instead of an HTTP endpoint: one process per
  model attempt, started from a fresh temporary directory, with the diff on
  stdin. `claude-cli` runs `claude -p` with no built-in tools, settings files,
  MCP servers or saved session; it removes eight credential-routing variables
  (`ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_BEDROCK` and the
  like) from the child's environment so the call stays on your subscription
  login, maps `PRXREF_LLM_REASONING_EFFORT` to `--effort`, and warns if the CLI
  reports an API-key source, loads tools anyway, or reports a rate-limit status
  other than `allowed`. `kiro-cli` runs `kiro-cli chat --no-interactive` on the
  v2 agent engine, with a per-call agent file that carries the system prompt and
  the model and allows no tools, MCP servers or resources. `PRXREF_LLM_MODELS`
  is walked as a fallback chain, `PRXREF_LLM_CLI_PATH` overrides the binary,
  `PRXREF_LLM_CLI_CONCURRENCY` (default 2) caps the processes running at once, a
  deadline miss kills the whole process group, and a missing CLI exits 2 before
  any forge or model call. See the "Subscription CLI backends" section of
  `docs/llm.md`.
- **Dollar cost in the run record (#7).** `cost_usd` is the total of the
  review's calls, the chunk workers plus the sweep, including truncated or
  unparseable responses that were billed, and `0.0` when no model call went
  out. A figure the backend reported always wins: the response body's
  `usage.cost` (OpenRouter), the `x-litellm-response-cost` header (a LiteLLM
  gateway or llm-ferry), litellm's `response_cost`, or the Claude Code CLI's
  `total_cost_usd`, an API-equivalent at list price rather than what a
  subscription is billed. Otherwise the cost is estimated from
  `PRXREF_PRICE_TABLE` (inline JSON or a JSON file path, USD per million tokens,
  keyed by exact model name, validated at load so a malformed table exits 2),
  and `cost_estimated` is set. Otherwise it is `null`, never `0` and never a
  partial sum, and one INFO line names the unpriced models. `PRXREF_POST_COST=1`
  appends the cost to the posted attribution line, `-v` prints it (`$…`,
  `$… (API-equivalent)`, `~$… (est.)` or `cost unknown`), and the `chunk ok`,
  `sweep ok` and `run ok` trace events, each unit's `<unit>.meta.json`
  (`cost_usd`, `cost_source`) and the openai-compat attempt log line (`cost=`)
  carry it. A run whose every reported figure came from the Claude Code CLI
  shows its cost as `$0.0202 (API-equivalent)` on the `-v` line and on the
  posted attribution, because `total_cost_usd` is the API list price, not a
  subscription bill; an estimated run keeps `~$… (est.)`. The run record marks
  such a run with `cost_api_equivalent: true`, a key no other run carries, and
  `--format json` gains no key, because each unit's `cost_source` already says
  `claude-cli`. prxref never asks a provider to add usage to a response. See
  `docs/llm.md` "Cost accounting".
- **PR size advisory (#8).** Set `PRXREF_SIZE_WARN_LINES` (lines added plus
  removed) and/or `PRXREF_SIZE_WARN_FILES` (files changed), and a PR strictly
  above either one gets a line at the top of its summary: "This PR changes N
  lines in M files, above the team guideline of … Consider splitting it." Both
  are off by default, and `0` is a real threshold. The counts come from the
  parsed diff and skip lockfiles from the common ecosystems, generated files
  (`*.snap`, `__snapshots__/`, `*.min.js`, `*.map`, `*.generated.*`,
  `*.auto.*`) and any path matching `PRXREF_SIZE_IGNORE_GLOBS`. The advisory
  never changes the verdict or the exit code; it is also reported as
  `size_advisory` in the run record and `--format json`, and as a
  `size advisory:` line in the CLI output.
- **New run-record and `--format json` keys.** `cost_usd`, `cost_estimated`,
  `review_rules`, `ticket_context`, `spec_grounding` and `size_advisory` are
  present on every exit, error and empty-diff exits included, and `null` when
  their feature is off. In `--format json` they follow the existing keys in a
  fixed, documented order, and a replay run adds `replay`. The CLI text output
  gains a `replay:` line on a replay, and `-v` adds `rules:`, `ticket:` (with the
  in/out/unknown counts) and `spec:` lines. The README's "CLI Flags" section now
  documents every `review` flag and lists the JSON keys in payload order.

### Changed

- **Minor findings render ⬜, and 🟦 now means "outside the ticket".**
  `outofscope` findings and unrecognised severities show a grey square in the
  summary counts, the findings list, inline comment headers and the library
  formatter, where 0.13.0 showed 🟦; the JSON value and the `OUTOFSCOPE` label
  are unchanged. The summary counts line also gains a `🔍 N spec` count on
  every run. Every glyph now comes from one table, `prxref.markers`.
- **The review prompts changed for every run.** The worker and sweep prompts
  define the `spec` severity and its rules, and carry a `### Spec constraints`
  block that reads `(no specs provided for this review)` when none are
  configured, so a review of the same diff can differ from 0.13.0's even with
  none of the new inputs set.
- **An unrecognized `PRXREF_LLM_BACKEND` is a configuration error (exit 2)**
  that names the variable and lists the six accepted values, checked before any
  other LLM setting. In 0.13.0 it was a failed review that exited 0.
- **`PRXREF_LLM_MODELS` splits on whitespace as well as commas**, like the new
  list-valued `PRXREF_SPEC_SOURCES` and `PRXREF_SIZE_IGNORE_GLOBS`. 0.13.0 split
  it on commas only.
- **GitLab MR diffs are requested without `access_raw_diffs`.** Every earlier
  release sent it to `/merge_requests/:iid/diffs`, which ignores it: gitlab.com
  returned byte-identical diffs with and without it, and only the deprecated
  `/changes` endpoint reads it. The reviewed diff is unchanged.

### Fixed

- **The `litellm` backend no longer requires `PRXREF_LLM_BASE_URL` (#1).** Only
  `openai-compat`, `ferry` and `http` need it. Set on any other backend, it is
  ignored with one INFO line and never forwarded, so a deployment that set a
  placeholder URL to get past the old check keeps working unchanged. To use a
  LiteLLM proxy, choose `openai-compat`.
- **GitLab merge requests with more than 20 files are reviewed in full.** The
  adapter read only the first page of GitLab's MR diff list, 20 files by
  default, and dropped every file after that without saying so. It now reads
  every page, and a page that cannot be read fails the review with an error
  naming it instead of reviewing part of the MR. A warning names each file
  GitLab sends without hunks (`too_large` or `collapsed`).
- **GitHub and GitHub Enterprise API calls time out**: 10 s to connect and 30 s
  per read, the same as the other forges. A stalled GitHub connection could
  hang the review, and the webhook worker running it, forever. A write that
  times out is not retried, so it cannot post a duplicate comment.
- **A `{placeholder}` in PR text is shown literally.** A PR title or
  description containing `{diff}` had the diff pasted into the prompt at that
  spot, and a PR or finding title containing `{findings}` or `{attribution}` did
  the same to the posted summary. Prompts and the summary are now filled in a
  single pass.
- **`load_config` no longer shares list defaults between calls.** Appending to
  one loaded config's `llm_models` changed the default that every later load
  started from.
- **Under `PRXREF_FAIL_ON=error` or `any`, a review that ends with verdict
  `Error` exits 1**, as documented since 0.4.0. Before, only a crash did: a
  forge that could not be read, a diff that could not be parsed or chunked, and
  a review in which every chunk failed all return an `Error` result rather than
  raising, so a gating lane read those broken runs as green. The default
  `never` is unchanged.
- **A total LLM failure no longer counts a successful sweep as failed.** When
  every chunk worker failed but the whole-PR sweep answered, the run correctly
  ended with verdict `Error` but reported `chunks_reviewed` 0 and every review
  unit failed. It now counts the sweep as reviewed: `chunks_reviewed` is 1,
  `chunks_failed` is the number of chunks, and the two still add up to
  `chunk_count`. The text output therefore reads `coverage: 1/2 chunks
  reviewed` on a one-chunk PR. The verdict, the posted error notice and the
  `PRXREF_FAIL_ON` exit code are unchanged.
- **The `forge.get_diff` trace span counts bytes.** Its `bytes` field counted
  characters, so a diff with non-ASCII text or a byte-order mark read short
  (15,647 against 15,651 on one live Azure DevOps pull request).
- **Documentation corrections.** `docs/deploy.md` no longer says there is no
  `PRXREF_FAIL_ON` (there is: `never`, `error` or `any`, default `never`), its
  exit-code table gains the `1` row, and its webhook table lists the Bitbucket
  Cloud events prxref accepts (`pullrequest:created`, `pullrequest:updated`) and
  gains Bitbucket Server / Data Center and Azure DevOps rows. `.env.example` and
  `docs/env-vars.md` no longer say that an unset `PRXREF_LLM_SEED` leaves the
  seed out of the request: prxref sends one random seed per process and reports
  it as `sampling.seed`. `docs/llm.md` names the backends that apply the seed,
  the reasoning effort and the max-tokens settings. `docs/forges.md` documents
  GitLab's paged diff listing, and that reading a merge request's threads on
  gitlab.com needs `PRXREF_GITLAB_TOKEN` even for a public project: gitlab.com
  answers anonymous `/notes` and `/discussions` requests with HTTP 401, so a
  tokenless review logs `discussion feed read was incomplete` and dedups against
  no threads.

### Security

- **Earlier releases were withdrawn and the history rewritten.** The published
  sdists of 0.10.1 through 0.13.0 and the wheels of 0.12.0 through 0.13.0 were
  removed from PyPI, so 0.12.0 through 0.13.0 can no longer be installed from
  it. The git history before 0.14.0 was rewritten to drop internal planning
  notes: every tag and nearly every commit before 0.14.0 has a new SHA, so
  re-clone an existing clone rather than pulling into it.
- **Files named by path stay inside the working directory.** A rules file, a
  ticket-context file or a local spec source that sits under the working
  directory, such as a file committed in a PR checkout, must still resolve
  under it once its symlinks are followed. So a committed
  `docs/SPEC.md -> ~/.ssh/id_rsa` is refused without revealing the link target,
  and every symlinked entry inside a spec directory is skipped. An absolute path
  outside the working directory is the operator's own choice and is read as
  given. These files are read as strict UTF-8 in bounded memory, and the rules
  and ticket loaders refuse URLs.
- **Credentials and paths stay out of what prxref sends.** Jira credentials go
  only to `PRXREF_JIRA_BASE_URL`: set without it, ticket fetches are anonymous
  and a WARNING names the variable, and a plain-`http` base URL is used with a
  WARNING. The digest names a spec source to the model only by its last path
  segment or bare host, with no query, userinfo, port or full local path (a
  credential that is itself the last path segment still gets through), and the
  spec failure reasons the summary posts carry no local path. The run record
  and the trace hold a rules or ticket file's metadata, never its text.

### Known limitations

- **Unpunctuated spec lines merge.** Consecutive keyword lines in one paragraph
  that end without punctuation (a list with no bullets or full stops) become a
  single constraint labelled with the strongest keyword among them, and a run
  of them longer than 400 characters is cut at 400, losing the rest. End each
  rule with a full stop, or make it a list item.
- **A `Spec: "…"` quote that never closes is barely exempt.** The hedge gate
  exempts a quote only up to a closing quote mark. A quote with no closing
  quote, or one that departs from the digest's wording before it closes, is
  exempt only up to its last inner quote mark, and not at all without one, so a
  condition inside it can still drop the finding as hedged.
- **A spec directory is read shallowly.** A directory source reads at most the
  first 20 `.md`, `.markdown`, `.txt` or `.adoc` files directly inside it, by
  name, and says nothing about the rest. Files it skips as unreadable or
  symlinked are named in a WARNING log line only, not in the posted note, the
  run record or the trace.
- **A spec directory's constraints are not tagged per file.** They carry the
  directory's name and a line number counted through its files joined together,
  each file under a `## <file name>` heading, rather than the file's own name
  and line.
- **A Jira ticket can crowd out the spec.** Ticket lines rank ahead of every
  other constraint and may fill up to 6,000 characters of the digest, a fixed
  share (half the default `PRXREF_SPEC_DIGEST_TOKENS` budget) that does not
  shrink with `PRXREF_SPEC_MAX_CHARS`. With a smaller digest budget, a long
  ticket can leave no room for anything else.
- **A `spec` finding's quote is not checked against the digest.** On a
  grounded run, a `spec` finding keeps its severity even when the constraint it
  quotes is not in the digest; only the hedge-gate exemption requires a match.
- **A Jira ticket passed with `--spec` is not ticket context.** It grounds
  `spec` findings but sets no finding's scope; to judge scope, pass the
  ticket's text with `--context-file`.
- **Spec grounding can crowd out a generic finding.** In the bundled eval,
  case-002's one expected non-spec finding was missed in all 4 grounded runs,
  across two models, and found in 3 of the 4 runs without the spec. That is two
  runs per model in each arm, and the cause, grounding displacing generic
  review, is inferred, not proven.
- **`kiro-cli` runs always read "cost unknown".** Kiro meters credits, not
  dollars, and reports no token counts, so a price table cannot estimate it
  either; each call's INFO line carries the credits instead.
- **`kiro-cli` isolation and reporting are partial.** Whether Kiro adds
  user-level configuration, such as `~/.kiro/steering/`, to prxref's per-call
  agent has not been verified, and the agent file cannot turn it off. Kiro does
  not report which model ran, so the attribution names the model you
  configured, and it keeps every chat, the diff included, under
  `~/.kiro/sessions/cli/` (`docs/llm.md` shows how to delete them).
- **GitLab files without hunks are not reviewed.** A file whose diff GitLab
  withholds as `too_large` or `collapsed` arrives without hunks, so it is listed
  header-only and not reviewed; a warning names each such file.
- **GitLab merge requests past 5,000 files fail.** An MR whose diff listing runs
  past 50 pages of 100 files fails the review rather than reviewing part of it.
- **GitHub pull requests past 20,000 diff lines are not reviewed.** GitHub
  refuses the unified diff of such a pull request with HTTP `406`
  (`too_large`), so the review ends with verdict `Error` and posts the error
  notice. There is no fallback to the paged file listing yet. This limit
  applies to every earlier release too.
- **A forge-less replay sees only the diff.** A `--diff-file` run without
  `--pr-url` gives the workers no library versions and no out-of-hunk
  definitions, gives the manifest claim check no full-file lines, and has no
  existing discussion; its title and description come from a `git format-patch`
  header when there is one, else the file name. With `--pr-url`, a replay keeps
  the PR's current title and description, pinned SHAs without `--no-threads`
  still show the current threads, and a `--diff-file` without `--head-sha` reads
  file context at the PR's current head; the last two each log a warning.
- **Azure DevOps is verified live for anonymous reads only.** On a public Azure
  DevOps Services project, the forge reads, the dry-run output shape, the
  pinned-range compare diff (including a PR whose target branch had moved on)
  and a pinned-range replay were checked live. Posting the summary and inline
  threads, pruning, PAT and `SYSTEM_ACCESSTOKEN` authentication and the
  service-hook payload are tested against recorded API shapes only, and Azure
  DevOps Server (a release that accepts REST `api-version=7.1`) is untested.
- **A mistyped or unrecognized `--pr-url` exits 0 even under
  `PRXREF_FAIL_ON=error` or `any`.** prxref prints the unrecognized-URL hint to
  stderr and exits 0, because nothing was reviewed and there is no outcome to
  gate on, so a gating lane with a malformed URL stays green.

## [0.13.0] — 2026-09-17

### Added

- **Stale-inline pruning now runs on every forge (#17, completed).** GitLab,
  Bitbucket Cloud, and Bitbucket Server gain the `prune_inline_comments`
  pass 0.9.0 shipped for GitHub alone, closing the gap its changelog entry
  named ("GitHub does; the others follow"). Each walks its existing
  comment/activity feed, deletes only inline comments carrying the
  attribution marker, and stays best-effort: a 403 delete or an unreadable
  feed logs and continues. GitLab deletes a diff note through the discussion
  that holds it (`DELETE …/discussions/{id}/notes/{note_id}`) and only
  touches notes with a `position` — the top-level summary note carries the
  marker too and belongs to `post_summary`. Bitbucket Cloud deletes only
  comments with an `inline` anchor (`DELETE …/pullrequests/{n}/comments/{id}`).
  Bitbucket Server deletes only anchored comments and carries the `version`
  its optimistic locking requires (`DELETE …/comments/{id}?version={v}`).
- **Per-unit prompt/response traces (`PRXREF_TRACE_DIR`, `prxref review
  --trace-dir`).** The structural JSONL trace (#53's starting point) names
  phases, never prompts; this names both. When the directory is set, each
  review unit — `chunk0` … and the whole-PR `sweep` — writes four files:
  `<unit>.system.md` and `<unit>.user.md` (the exact rendered prompt),
  `<unit>.response.json` (the raw model text, JSON-encoded), and
  `<unit>.meta.json` (model, token counts, elapsed, error). Unset means zero
  cost, and a write failure is a logged warning, never a review failure.
- **`prxref review --timeout SECONDS`** — the per-invocation override for
  `PRXREF_LLM_TIMEOUT` (#52), on the same explicit-flag > env-var > default
  precedence as `--max-chunks`. An invalid value fails fast with
  `ConfigError` (exit 2) naming the flag.

## [0.12.2] — 2026-09-15

### Fixed

- **Findings no longer vary between identical runs for want of a seed** (#56).
  A reporter measured the same commit draw seven findings on a dry run and
  two on the posting run minutes later — and the two that vanished were the
  verified-true blockers. Temperature was already 0.0, but temp-0 is not
  bitwise-deterministic on hosted inference and the seed was unset. Every
  LLM call in a run now shares one process-level random seed
  (`secrets.randbits(31)`, lock-guarded), recorded in the result's
  `sampling` block; `PRXREF_LLM_SEED` still overrides for a fully pinned
  run.

- **The worker no longer concludes from one file when the refuting evidence
  is another file in the same diff** (#57). Twice on one PR the reviewer
  asserted something was absent or unsupported while the diff itself
  contained the refutation — a build-time `define` flagged as breaking a
  runtime override the same diff documented as build-time, and a claim
  about an install flag refuted by the install script two chunks over.
  Chunks are capped at five files, so the refuting sibling was never in
  view. The worker prompt now carries a cross-file corroboration rule and a
  bounded sibling-file summary (path, status, +A/-D, and up to 40
  added/context lines per file, 12 files and 4000 chars per block) rendered
  below the diff.

- **The manifest claim-check gate now covers lockfiles, not just
  `package.json`** (#58). A confident finding claimed `vitest`/`tsup` were
  runtime `dependencies` when they sit in the adjacent `devDependencies`
  block of `bun.lock`, anchored to a phantom line. The gate's section and
  anchor checks now apply to `package.json`, `bun.lock(b)`,
  `package-lock.json`, `yarn.lock`, and `pnpm-lock.yaml`; section scanning
  tolerates JSONC (unquoted keys, trailing commas) and falls back to the
  served full-file lines when the section header sits above the hunk.

## [0.12.1] — 2026-09-04

### Fixed

- **The settled-thread gate no longer crashes on a general PR comment** (#54).
  A thread without a path — any unanchored comment, near-universal on
  Bitbucket Server — raised `AttributeError` inside
  `apply_settled_thread_suppression` on 0.12.0, and because the pass chain is
  not guarded the whole review was lost (`review failed: ...`, nothing
  posted). Such a thread is now skipped by that gate; same-path suppression
  is unchanged.

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

[Unreleased]: https://github.com/sblattj/prxref/compare/v0.32.3...HEAD
[0.32.3]: https://github.com/sblattj/prxref/releases/tag/v0.32.3
[0.32.2]: https://github.com/sblattj/prxref/releases/tag/v0.32.2
[0.32.1]: https://github.com/sblattj/prxref/releases/tag/v0.32.1
[0.32.0]: https://github.com/sblattj/prxref/releases/tag/v0.32.0
[0.31.0]: https://github.com/sblattj/prxref/releases/tag/v0.31.0
[0.30.1]: https://github.com/sblattj/prxref/releases/tag/v0.30.1
[0.30.0]: https://github.com/sblattj/prxref/releases/tag/v0.30.0
[0.29.0]: https://github.com/sblattj/prxref/releases/tag/v0.29.0
[0.28.0]: https://github.com/sblattj/prxref/releases/tag/v0.28.0
[0.27.0]: https://github.com/sblattj/prxref/releases/tag/v0.27.0
[0.26.0]: https://github.com/sblattj/prxref/releases/tag/v0.26.0
[0.25.0]: https://github.com/sblattj/prxref/releases/tag/v0.25.0
[0.24.0]: https://github.com/sblattj/prxref/releases/tag/v0.24.0
[0.23.0]: https://github.com/sblattj/prxref/releases/tag/v0.23.0
[0.22.0]: https://github.com/sblattj/prxref/releases/tag/v0.22.0
[0.21.1]: https://github.com/sblattj/prxref/releases/tag/v0.21.1
[0.21.0]: https://github.com/sblattj/prxref/releases/tag/v0.21.0
[0.20.0]: https://github.com/sblattj/prxref/releases/tag/v0.20.0
[0.19.0]: https://github.com/sblattj/prxref/releases/tag/v0.19.0
[0.18.0]: https://github.com/sblattj/prxref/releases/tag/v0.18.0
[0.17.0]: https://github.com/sblattj/prxref/releases/tag/v0.17.0
[0.16.0]: https://github.com/sblattj/prxref/releases/tag/v0.16.0
[0.15.0]: https://github.com/sblattj/prxref/releases/tag/v0.15.0
[0.14.0]: https://github.com/sblattj/prxref/releases/tag/v0.14.0
[0.13.0]: https://github.com/sblattj/prxref/releases/tag/v0.13.0
[0.12.2]: https://github.com/sblattj/prxref/releases/tag/v0.12.2
[0.12.1]: https://github.com/sblattj/prxref/releases/tag/v0.12.1
[0.12.0]: https://github.com/sblattj/prxref/releases/tag/v0.12.0
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
