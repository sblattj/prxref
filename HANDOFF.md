# HANDOFF — v0.23.0 shipped: graceful degradation when the token cannot post (#48)

**Repo:** `sblattj/prxref` (public) · **Released:** 2026-09-27 · **Supersedes** the
v0.22.0 handoff.

0.23.0 is one feature. #48: when the token can read a pull request but not
comment on it (a fork PR under a plain `pull_request` workflow, or a
read-only pipeline token), the review still reaches the author through the
CI job: GitHub annotations and the job summary, Azure Pipelines logging
commands, a GitLab Code Quality report, or the log. The new key
`PRXREF_FALLBACK` is `auto` by default but acts only when a post fails, so
when every post succeeds (or nothing is posted) every prompt, forge read,
forge write, stdout byte and run-record value is the same as in 0.22.0,
apart from the one new run-record key `degraded`, which is `null`. One new
config key, one new module (`ci_fallback`), one new run-record and
`--format json` key, and no new CLI flag or `Forge` method. The user-facing
account is the `[0.23.0]` section of `CHANGELOG.md`. This file is for
whoever cuts the next release. The v0.22.0 handoff is in git history.

## What landed

- **The key.** `PRXREF_FALLBACK` (`auto`|`off`, default `auto`) is a
  `_CHOICE_KEYS` key. It never reaches the orchestrator:
  `cli._cmd_review` reads it from the same `load_config()` call as
  `fail_on`, and after the review returns, and before either output format
  prints, calls `cli._emit_fallback` when it is `auto`. It is not an
  `orchestrate_review` keyword and is left out of `evals.RUN_CONFIG_KEYS`.
- **`src/prxref/ci_fallback.py`, pure renderers.** Nothing in it reads the
  environment or writes a file. `github_annotations` renders `::error`,
  `::warning` or `::notice` lines with `file`, `line` and `title`
  properties, errors first, then warnings, then the rest, capped at
  `GITHUB_ANNOTATION_CAP` (50). `azure_log_issues` renders
  `##vso[task.logissue type=error|warning;...]` lines with no cap.
  `gitlab_codequality` renders Code Quality entries: `check_name` is the
  finding's rule or `prxref`, `fingerprint` is the SHA-256 of `[file, line,
  title]` so it is stable across runs, and severity maps to `major`, `minor`
  or `info`. `step_summary_markdown` prefixes the summary with one quoted
  line saying it could not be posted. `GITLAB_REPORT_FILE` is
  `gl-code-quality-report.json`, and `DEGRADED_SUMMARY_KEY` names the result
  key that carries the summary markdown.
- **`detect_ci(environ)`** returns `github` when `GITHUB_ACTIONS` is `true`,
  `azure` when `TF_BUILD` is `true`, `gitlab` when `GITLAB_CI` is `true`
  (each case-insensitive), `bitbucket` when `BITBUCKET_BUILD_NUMBER` is
  non-empty, else `None`; the first match wins. The GitHub branch also reads
  `GITHUB_STEP_SUMMARY` for the job-summary path.
- **The emission, `cli._emit_fallback`,** does nothing unless the result's
  `degraded` is a dict, and emits only the active findings. Under GitHub
  Actions it prints the annotations and appends the summary to
  `$GITHUB_STEP_SUMMARY` when that is set; under Azure Pipelines it prints
  the logging commands; under GitLab CI it writes the report in the working
  directory, replacing any existing file; under Bitbucket Pipelines and no
  CI it logs the summary at WARNING. Under `--format json` the stdout lines
  are skipped so stdout stays one JSON document. Every CI then logs one
  WARNING naming the cause. It never raises: a failed write is logged and
  left out of `fallback`, and any other failure is logged and stops the
  emission. The exit code is `_fail_on_exit`'s, untouched. The webhook
  daemon never calls it, so it records `degraded` but never emits.
- **The run-record key.** `degraded`, after `incremental` in the record and
  in `--format json`: `null` when no post failed, else `{"cause", "failed",
  "fallback", "annotations"}`. The orchestrator fills `cause` and `failed`:
  `orchestrator.post_failure_cause` classifies each failed post as
  `permission` when the exception's `response.status_code` is in
  `PERMISSION_STATUSES` (401, 403), else `error`, and `_degraded_record`
  lists each failed post kind once (a failed summary re-post counts as
  `summary`) and says `permission` when any failure was. The main exit,
  `_summary_only_run` and `_error_run` stamp it through `_with_degraded`,
  which also sets `DEGRADED_SUMMARY_KEY` to the summary the post would have
  carried (rendered fresh for `PRXREF_POST_MODE=inline`); that key never
  reaches `--format json`. The CLI fills `fallback` and `annotations` as it
  emits.
- **The conftest clears the CI variables.** The autouse env fixture in
  `tests/conftest.py` now also deletes `GITHUB_ACTIONS`,
  `GITHUB_STEP_SUMMARY`, `TF_BUILD`, `GITLAB_CI` and
  `BITBUCKET_BUILD_NUMBER` (`CI_ENV_NAMES`), because the suite itself runs
  under GitHub Actions (lesson 1).
- **Tests.** One new file, `tests/test_issue_48_degradation.py`, which also
  pins sha256 goldens of the text, `-v` and JSON stdout and the posted
  summaries of a successful review, measured on the 0.22.0 CLI. Existing
  pins moved only where they list record or JSON keys.
- **Docs.** `docs/forges.md` gains the "When prxref cannot post" matrix,
  and `docs/env-vars.md`, `.env.example` and the `config.py` docstring
  describe the key.

## What this release taught

Written down because each one cost real time.

1. **The test suite runs under GitHub Actions, so a feature keyed on CI
   environment variables fires inside the project's own CI run.**
   `.github/workflows/ci.yml` runs `uv run pytest -q` on a runner where
   `GITHUB_ACTIONS=true` and `GITHUB_STEP_SUMMARY` names the job's real
   summary file. Any CLI test with a failing post would have printed
   annotations into the job log and appended to that summary, and passed or
   failed differently than on a laptop. The fixtures now clear those
   variables (`CI_ENV_NAMES` in `tests/conftest.py`), and each test sets the
   ones it needs.
2. **A forge adapter that swallows per-comment 4xx errors hides a permission
   failure from any caller that looks for exceptions.** Only GitHub's
   `post_inline_comments` raises on a 401 or 403. Bitbucket Cloud,
   Bitbucket Server, GitLab and Azure DevOps skip each refused comment and
   return a count, so `PRXREF_POST_MODE=inline` on those forges records no
   degradation. The default mode is covered only because the summary post
   raises first. It is a documented gap (Still open), not a fix.
3. **A record key pinned by numeric offset escapes a name-based search.**
   `tests/test_cli_scoped_rules.py` asserts `keys.index("sampling") ==
   keys.index("scoped_rules") + 8` (it was `+ 7`), so a grep for
   `incremental` to find every pin of the record's key list misses it. The
   full suite found it.

## The coupling that will catch the next person adding a config key

`tests/test_docs_consistency.py` checks `docs/env-vars.md` and `.env.example`
against `config._DEFAULTS` **in both directions**. It also asserts two hard-coded
integers, built as `f"**{len(_DEFAULTS)}** configuration keys"` and
`f"for {len(_DEFAULTS)+len(_LEGACY_ENV_ALIASES)} accepted variable names"`.

So a new config key is not a one-file change. It changes four surfaces
together:

- `_DEFAULTS`, plus whichever of the `_INT_KEYS`, `_FLOAT_KEYS`, `_BOOL_KEYS`,
  `_LIST_KEYS`, `_RANGES` and `_CHOICE_KEYS` tables apply to it (the v0.14.0
  handoff listed only four of these)
- the `config.py` docstring
- `.env.example`
- `docs/env-vars.md`, including its counts and its per-section headings

A key that feeds `orchestrate_review` needs one more step. `tests/test_cli.py`
(`test_run_review_passes_every_configured_orchestrate_kwarg`) requires
`cli._run_review` to pass every orchestrator kwarg whose name equals a config
key. A new value for an existing key, as `off` for `PRXREF_LLM_SEED` in
0.19.0, touches none of the counts, but still needs the docstring,
`.env.example` and `docs/env-vars.md` to describe it, and a value that is
not an integer needs its own pass through `_coerce_env` and `_check_ranges`.
Current values, counted from `config._DEFAULTS` and
`config._LEGACY_ENV_ALIASES` at this release: **72** keys, **1** legacy
alias, **73** accepted names, up from 71 and 72 in 0.22.0. The most recent
new key is `fallback` (`PRXREF_FALLBACK`), added in 0.23.0. It took the
`_CHOICE_KEYS` table, the docstring, `.env.example`, both counts and the
LLM / Pipeline heading of `docs/env-vars.md`, but no `cli._run_review`
pass-through, because it is not an `orchestrate_review` keyword:
`cli._cmd_review` reads it itself. It is left out of
`evals.RUN_CONFIG_KEYS`. The key before it, `incremental`
(`PRXREF_INCREMENTAL`, 0.22.0), took every surface above, the pass-through
included, and is left out of `evals.RUN_CONFIG_KEYS` too, which a test
pins. `suggestions` (`PRXREF_SUGGESTIONS`, 0.21.0) took the same surfaces.
`config.SUGGESTIONS_MAX_TOKENS` is a module constant, not a key. 0.21.0 also
changes one default conditionally:
`llm_max_tokens` is 8192 instead of 4096 when suggestions are on and the
operator left it unset. That needed no new key, but it needed a way to tell
"unset" from "set to the default", because `load_config` pre-fills its
sources for every default; it now keeps an explicit set of supplied keys.

## Release shape (follow this next time)

How 0.23.0 was built:

1. **One code task in its own worktree from a pinned commit, its tests in
   a new file, merged behind a full gate.** It added the key, `ci_fallback`,
   the orchestrator's failure classification and the CLI's emission, and
   pinned sha256 goldens of a successful review's output measured on the
   0.22.0 CLI before any edit. `uv run pytest` rose from 9222 to 9280 (55
   new tests, plus 3 from `tests/test_docs_consistency.py` parametrizing
   over the new key), and `uv run ruff check src tests` stayed clean.
2. **No live checks** (see below).
3. **Release.** This commit bumps the version, adds the CHANGELOG section,
   rewrites this file and tightens one README sentence; it adds no test,
   so the count stays at 9280.

Cutting the release:

```bash
# bump pyproject.toml and src/prxref/__init__.py, then:
uv lock                              # uv.lock carries the version too
git tag vX.Y.Z && git push origin vX.Y.Z
```

Pushing a `v*` tag runs `.github/workflows/release.yml`, which has two jobs:

- The `release` job runs `uv build` and creates the GitHub release with the
  wheel **and** the sdist attached. It lists both by explicit pattern, not
  `dist/*`, because `dist/*` once shipped a stray `.gitignore` as an asset.
- The `publish` job builds again and publishes to PyPI by OIDC trusted
  publishing, so no token is stored anywhere.

Because the two jobs build separately, the PyPI files and the release assets
are two builds of the same tag. Keep the attached sdist: at least one consumer
updates itself with `gh release download --pattern '*.tar.gz'`, and that
pattern does not match GitHub's auto-generated source archive.

## Verified at release

```
9280 passed                                   uv run pytest -q
All checks passed!                            uv run ruff check src tests
0.23.0                                        uv run prxref --version
```

These counts come from the release branch, measured at the commit that last
updated this file.

### Live checks

None. The feature only acts when a post fails, and verification never
writes to a forge, so a refused post was exercised only through mocked
HTTP 401 and 403 responses in the test suite. The GitHub annotations, the
job summary, the Azure Pipelines logging commands and the GitLab Code
Quality report follow each CI's documented syntax, and are checked against
that syntax in the suite, but none of them has run on a live runner yet.

## Still open — not part of this release

- **With `PRXREF_POST_MODE=inline`, a read-only token is detected on GitHub
  only.** Bitbucket Cloud, Bitbucket Server, GitLab and Azure DevOps skip a
  refused inline comment without raising (lesson 2), so an inline-only run
  on those forges records no degradation and emits nothing. The default
  post mode is covered, because the summary post fails first and the
  inline comments wait on it. Closing it means those adapters must report
  a 401 or 403 on an inline comment, which changes what `post_inline_comments`
  returns or raises on four forges.
- **The fallback formats have no live proof yet.** The first will be a fork
  PR under a plain `pull_request` workflow in a real CI run; watch its
  annotations, its job summary and its `degraded` record.

- **The incremental marker round-trip has not been exercised live with
  posting.** Verification never posts, so no live run has written a marker
  and read it back on the next push. The first real multi-push use will be
  the first live proof; watch its `incremental` record and the summary's
  note line.
- **5-file chunks on GLM 5.3 Flash can exhaust even the 8192 retry
  budget.** In the live check above, 2 of 4 units stopped at 4096 and again
  at 8192. Raising the default budget stays with the head-to-head
  measurement in the next bullet.
- **With incremental on, the verdict can be Approved over files with open
  findings.** The chunk findings, and so the verdict, cover only the
  re-reviewed files, plus the sweep's and the deterministic checks'. Earlier
  inline comments on the other files stand, and the summary says so, but
  the verdict does not count them.
- **Raising the default budget itself (option 1 of #52) is left to a
  head-to-head measurement across many cases.** 0.21.1 added a second
  call for a reply that is unusable at the budget stop; it does not
  establish whether a higher default budget would avoid that extra call in
  the common case, or what a higher default costs on models that do not
  need it. Measure both before changing `PRXREF_LLM_MAX_TOKENS`'s default.
- **Suggestions are unmeasured for quality.** 0.21.0's live checks show the path
  works and how often a suggestion survives validation, not whether the
  suggestions are right or whether asking for them changes the findings.
  The eval harness does not score suggestions; `run.json` records only the
  setting. Measure before changing the default from `off`.

0.21.0's known limitations:

- **Azure DevOps has a native suggestion UI that prxref does not use.**
  Azure DevOps documents suggested changes in pull request comments, with
  an apply button; whether a comment posted through the API gets that
  button is unverified, so Azure DevOps gets the copyable fallback block.
- **Bitbucket Server / Data Center native suggestions are unverified**, so
  it also gets the fallback block, as Bitbucket Cloud does.
- **GitLab's position fallback loses the apply target.** When GitLab refuses
  an inline position with a 400, the existing fallback reposts the body as
  a general merge request note, where a `suggestion` block cannot be
  applied.
- **Only GitHub threads carry a range.** `Thread.start_line` is set by the
  GitHub adapter only; every other forge's multi-line threads are still
  measured at one line.
- **The model's line reaches the validation pass by position.** The
  orchestrator captures each finding's line before line alignment and
  hands it to `apply_suggestion_validation` as a list, which relies on the
  passes between them keeping findings one to one and in order. A new pass
  there that drops, splits or reorders findings raises `ValueError` on a
  length mismatch at best, and misattributes `line_moved` at worst.
- **The summary never shows suggestions**, only inline comments do.

- **The history-window bug is still not asserted.** With the follow-up on,
  #22's second bug surfaced in 2 of 3 live runs, and both times its
  definitions were looked up and sent, but GLM 5.3 Flash did not re-assert
  it at or above the 0.60 floor, so it was dropped as "not confirmed". On
  that model, nothing in 0.18.0 through 0.20.0 posts it. A stronger model, or
  more runs, is the next measurement (the v0.18.0 handoff's Live checks).
- **The follow-up's effect is one single-factor measurement.** Three live
  checks at N=3 per arm on one fixture, through one model. A confirmation is
  a second sample of the chunk plus the excerpt, and N=3 cannot separate the
  two (lesson 5 of the v0.18.0 handoff). Measure with `prxref eval` over
  more runs before changing the default from `off`.
- **Repository context's effect on findings is measured for readers only.**
  - The reader block has one single-factor measurement, at N=3 per arm on one
    fixture (the v0.17.0 handoff's Live checks).
  - #17's definitions and contract excerpts are still unmeasured. On the #17
    fixture, at 3 runs a level, one label matched in 0 of 3 runs at `off` and
    1 of 3 at `repo`, and the other in none; sampling noise explains that as
    well as the context does. An HTTP 201 status code that exists only in the
    injected OpenAPI excerpt appeared in the findings of all 3 `repo` runs
    and of none of the `off` runs, which shows the model reads the context.
  - On sblattj/multi-auto-claude-sub#5, identical prompts gave 3 findings and
    0, so a single run's count cannot measure it.

  Measure with `prxref eval` over more runs a level before changing the
  default from `off`.

0.20.0's known limitations:

- **Nothing records where chunk context came from.** The run record's
  `repo_context.reader` names the repository-context reader only (`forge`,
  `repo-dir` or `null`), and `repo_context` itself is `null` at `off`, so a
  run record does not say
  whether the dependency and definitions blocks were read from the forge or
  from `--repo-dir`; the trace's prompt files show what was sent.
- **The `--repo-dir` fallback applies no exclusion**, as the forge's chunk
  reader applies none. `PRXREF_CONTEXT_EXCLUDE_GLOBS` limits repository
  context only.

0.19.0's known limitations:

- **The JVM owner is a guess from groupIds.** prxref does not download a
  BOM, a parent or a platform, so it never reads what one manages.
  `jvm_maven.managed_dependency` scores the candidates by the leading
  groupId segments they share with the dependency's, and nothing else: a
  BOM that manages artifacts outside its own group (as
  `spring-boot-dependencies` manages `io.micrometer`) is never the likely
  owner of them, and a close groupId can be named for an artifact it does
  not manage.
- **The group-only mark is by name tokens.** A match is group-only when no
  artifactId token equals an import segment, so a true dependency whose
  artifactId shares no word with its packages, such as `spring-webmvc` for
  `org.springframework.web`, is marked too. Such lines are kept, never
  dropped.
- **`PRXREF_LLM_SEED=off` still warns on the CLI backends.** On `claude-cli`
  and `kiro-cli`, any non-empty `PRXREF_LLM_SEED`, `off` included, logs the
  existing `PRXREF_LLM_SEED is not applied by <backend>` WARNING, which
  reads oddly for an explicit opt-out. It is not an error
  (`test_off_on_a_cli_backend_is_not_an_error`).
- **`off` is lowercase only.** `OFF` or `Off` exits 2 naming the variable,
  as other choice values in `config.py` do.

0.18.0's known limitations of the context follow-up:

- **Confirmation needs the same file.** `followup_merge.confirms` rejects a
  second-reply finding in another file, whatever it says. Live, the first
  reply pinned the history-window question to `README.md:7` in one check and
  to `README.md:6` in another, and in that run the re-run re-stated it at
  `progress.py:40`: even at or above the floor it could only have landed as
  "not confirmed", its re-statement discarded.
- **Only code-shaped names are looked up.** A plain lowercase word with no
  backticks, `.`, `(` or `_` gives no name, and a file path is never looked
  up (`name_tiers`). A dotted name that is not a source file splits:
  `pyproject.toml` gives `toml` and `pyproject`, `example.com` gives `com`
  and `example`.
- **The keyword sets are shared across languages.** Every name is checked
  against the Python, JS/TS, Java and Kotlin keywords together, so a
  repository symbol named like any of them is never looked up in any
  language: `get`, `set` and `type` (JavaScript), and `open` (Kotlin), even
  after a dot, as in `session.open(`. The builtin filter is Python's only;
  JavaScript and Java builtins are not filtered beyond their keywords, and a
  repository that defines its own `TypeError`, or a bare function named like
  a Python builtin, does not get it looked up.
- **One follow-up, chunks only, with no retries of its own.** A chunk that
  took the timeout retry, the whole-PR sweep, and a question in the second
  reply get no follow-up. The re-run has no parse retry (only the empty-reply
  retry) and no timeout retry, so a failed re-run leaves the first findings
  to the floor.
- **The caps have no setting.** 3 names, 8 reads, 4 excerpts, 30 lines and
  4,000 characters are module constants in `repo_followup`. With more
  questions than name slots, the least confident questions get none.
- **The follow-up costs about a third more input when it fires.** Live, a
  re-run added 2,903 to 3,027 input tokens to an 8,060-token review, plus a
  second reply's output tokens, which reached 9,687.

0.17.0's known limitations, still true, in full in its CHANGELOG section:

- **Java and Kotlin files cost forge reads at every level.** For a changed
  JVM file with an import outside `jvm_deps.SKIPPED_ROOTS`, the dependency
  walk tries 3 build-file names at each directory level up to the root: up
  to `3 * (d + 1)` reads for a file `d` directories deep. A Gradle build file
  that mentions `libs.` adds up to 3 catalog probes, and a `pom.xml` up to 5
  parents. The reads are cached per run and uncapped; on the #17 fixture
  they were 21 probes over 7 levels.
- **Kotlin names and paths.** Kotlin standard-library names such as `Int`,
  `Unit` or `Pair` are not in `jvm_lang.JDK_NAMES`, so at `repo` each one a
  chunk mentions is looked for by the file-name search. `repo_resolve` has no
  Kotlin import or path rules: outside the diff, a Kotlin type is found by
  the file-name search alone.
- **Precompiled Gradle script plugins are walked.** Only `build.gradle.kts`
  and `settings.gradle.kts` are skipped (`chunk_context._GRADLE_SCRIPTS`).
  Another `*.gradle.kts` file is a Kotlin source, so its Gradle API imports
  cost a walk for the nearest build file.
- **Some dependencies are not matched or not resolved.** An artifact whose
  groupId is not a package prefix of its imports gets no line (Guava,
  Lombok, JUnit 4, Spring Boot starters and kotlinx among them). Gradle map
  notation is not read, and a version set through a variable or
  `gradle.properties` is not resolved.
- **A truncated review reply never draws on the parse retries**, by design,
  whatever `PRXREF_LLM_PARSE_RETRIES` says; since 0.21.1 an unusable one
  gets the single budget retry instead (#52), and its error still names
  `PRXREF_LLM_MAX_TOKENS`.
- **`score.json` does not total the reviews' parse retries.** It counts the
  judge's; each case's own run record carries its review's.
- **The shared-state search matches names, not types.** Any line that reads
  a key of the same name, in a file of the same language, is a reader, so an
  unrelated `.data` or `table` can fill an entry, and a reader in another
  language is never found.
- **The toggle check needs both lines in the pull request**, and only the
  four suite-wide setup file names count as pins.

Found while building 0.17.0, not in the CHANGELOG:

- **A model finding on the toggle's line survives beside the check's.** At
  the default `PRXREF_DEDUP_SIMILARITY` (unset), both are kept, as any two
  chunk findings on one line are. With the similarity set, the reworded tier
  keeps one copy (`docs/quality.md`).
- **A status-added chunk file is read for nothing.** With a reader,
  `repo_crosschunk.diff_definitions` still reads an added chunk file when
  another file of the chunk wants a name the added file's own lines lack.
  Every line of an added file sits inside its hunks, which the search skips,
  so that read can never yield an entry.

0.16.0's repository-context limitations, still true:

- **Read caps cut the lowest-ranked sources first.** Contract files are read
  before import, path-convention and name-search files, and the shared-state
  search spends only the reads those leave, so readers go first, then the
  other definitions, once a chunk has made 16 reads or the run 200. Which
  chunk meets the run cap first depends on thread scheduling.
- **No content search.** Outside the pull request, a definition is found
  only through an import, the Java path convention, or a same-language file
  named after it.
- **Files over 512 KiB read as missing**, so a large OpenAPI spec gives no
  excerpt.
- **A large repository is listed only in part.** A paged listing stops at 20
  pages and GitHub truncates a very large tree, so the name search, the
  contract globs and the shared-state search see only the files listed. A
  large GitLab project's listing takes about a minute, once per run, at
  `repo` only.
- **The Bitbucket Server listing is untested live.** It is built from the
  Data Center REST documentation and tested against fakes of that shape.
  Whether it returns submodules is unverified.
- **A chunk that times out loses most of its context.** The timeout retry in
  `orchestrator._run_worker` renders with `include_definitions=False` and no
  repository-context unit, so it drops the same-file definitions, Java and
  Kotlin ones included, and every repository-context block: definitions,
  contract excerpts and readers. The record marks the unit `retry_dropped`.
  The dependency block, JVM lines included, survives.
- **A pull request's file can be fetched twice.** Chunk context's reader
  (`orchestrator._make_file_reader`) and the repository reader
  (`repo_reader.RepoReader`) share no cache, so a file both of them read
  costs two requests per run. Java and Kotlin files now read through both at
  `diff` and `repo`, as other languages' files already did.

Listing, retry and cost notes:

- **Bitbucket Cloud's `complete=false` is inferred from directory depth.** The
  walk asks for `max_depth=64` and marks the listing incomplete when a path
  reaches 63 slashes. The depth rule comes from probing, not from
  Bitbucket's documentation, and the live check never came near it: its
  deepest path had 1 slash.
- **Azure DevOps continuation tokens are not followed.** A listing that comes
  back with `x-ms-continuationtoken` is marked incomplete with a WARNING. No
  live listing has returned one.
- **GitLab's GraphQL listing is not retried on 429 or 5xx.** The session
  retries only `GET`, `HEAD` and `OPTIONS`
  (`allowed_methods` in `forges/gitlab.py`), and the listing is a `POST`. A
  failed first page falls back to the REST walk; a failed later page gives a
  partial listing.
- **A parse retry followed by a timeout retry loses tokens and retries.**
  When a chunk's retried call times out, `_run_worker`'s timeout retry
  replaces the chunk's result, so the billed earlier calls' tokens drop out
  of the run totals, and only the second run's `parse_retries` is counted.
- **The retry's cost estimate uses the last call's model.** When any call
  reports no cost, the unit's cost is unknown, and `costs.run_cost`
  estimates it from the summed tokens at the rate of the unit's `model`,
  the last call's, even when another model in the fallback chain answered
  an earlier call. A context follow-up folds the same way: the chunk's
  `model` becomes the re-run's when it answered.
- **prxref's defaults are too small for a thinking model.**
  `PRXREF_LLM_MAX_TOKENS` 4096 and `PRXREF_LLM_TIMEOUT` 45 lost chunks to a
  thinking model in 0.16.0's live checks (lesson 4 of the v0.16.0 handoff),
  and 0.17.0's live check ran at 32,768 tokens and 900 s. Neither default
  changed in 0.16.0 through 0.20.0, and 0.21.0 raises the token budget to
  8192 only when suggestions are on (#52 tracks the rest). 0.21.1 retries
  an unusable truncated reply once at double the budget, which the 0.22.0
  live check shows is not always enough; raise both for such a model. The
  recipe under "Measuring repository context" in `tests/evals/README.md` sets
  neither, so run verbatim against such a model it cuts the replies off.

Sizing, recall and GitHub notes:

- **The size advisory is opt-in.** `size_warn_lines` and `size_warn_files`
  default to `None` in `config._DEFAULTS`, so nothing warns on a 30,000-line
  PR.
- **Chunk sizing on very large PRs.**
  - Once `max_chunks` binds, the overflow branch of `triage.build_chunks` puts
    each remaining file into the smallest chunk and ignores the token budget.
    So a very large PR packs thousands of changed lines into each chunk.
  - The budget is compared against an estimate of 40 tokens per changed line,
    which is above real prompt tokens.
  - A live check found that shrinking chunks fourfold did not change the result
    on one large PR. So this is a sizing note, not a known recall loss.
  - Repository context's character budget comes on top of the chunk token
    budget, which does not count it.
- **Generic-bug recall is unmeasured beyond a small sample.** On the three
  bundled eval cases, gpt-4o-mini found every spec-grounded label and none of
  the three generic bug labels. `worker.md`'s "Prefer zero findings over one
  speculative finding" is an untested suspect. Measure with `prxref eval`
  before tuning the prompt.
- **Review has no path-ignore setting.** `PRXREF_SIZE_IGNORE_GLOBS` affects only
  the size counts. A repository's test fixtures that contain deliberate
  violations are reviewed as code. In this repository, the sweep reviews the
  eval fixtures under `tests/evals/` and reports their planted violations as
  findings. (Repository context's exclude floor keeps eval label files out of
  the context entries, not out of the diff.)
- **GitHub's limits are undocumented.**
  - The 406 `too_large` fires past 20,000 lines **or** 300 files.
  - The `/files` listing withholds `patch` in two ways:
    - Silently: 0/0 counts once a listing page nears about 1 MB, which the
      listing-sum check catches.
    - Declared: the true counts are kept. This is commonly a lockfile added or
      removed whole, and the file is reviewed header-only with a warning.
  - GitHub documents neither trigger's exact threshold.
- **Keep the listing-sum check.**
  - The withheld entries in sblattj/prxref#9 all report 0/0/0. So only
    `_refuse_short_totals` catches silent withholding. Removing it would reopen
    #9 whenever the compare leg also fails.
  - `tests/fixtures/github/` is trimmed from recordings of sblattj/prxref#9.
    The compare fixture is an 8-file subset, not the whole 1.67 MB diff.
  - It is a hypothesis, not isolated, that the 20,000-line trigger skips
    patch-withheld files.
- **A review past GitHub's diff limit reads the pull request twice.**
  `orchestrate_review` reads `get_pr` for the review's metadata, and
  `_get_diff_past_the_limit` reads it again before the compare read. That is
  one redundant GET per oversized review.
- **Four #15 branches are covered by unit tests only.** No live check reached
  the patch-less 0/0 header-only branch, GitHub's 3,000-file listing cap, a
  406 without `too_large`, or a compare diff whose counts disagree with the
  PR's.

Carried over from 0.15.0 and earlier, still true:

- **#10 The reworded tier is unproven.** It ships off. Its suggested threshold
  was chosen on invented negatives, and a live check found no candidate pair
  in eight runs.
- **#12 Chunks are not packed by language.** `triage.build_chunks` fills
  chunks by token budget and file cap, preferring the chunk that shares the
  deepest directory (`_shared_dir_depth`). So a chunk that holds two kinds of
  file gets the union of both files' scoped rules.
- **#13 Grouping keys on the rule the model names.**
  - A finding with no rule groups by title only. It never joins a group whose
    findings name a rule, even with the same title in the same file.
  - A member that line alignment moved to file level adds no `Also at`
    location.
  - The caps rank by `finding_rank_key` (confidence, then file path), not by
    group size. So capping a representative drops all its folded locations
    from the output, and the same goes for the finding the per-rule cap (#18)
    folded a rule's other findings into.
- **Findings on files outside the chunk.** A worker can report on a file it saw
  only in the bounded `### Other files changed in this PR` excerpt
  (`chunk_context.sibling_summary_block`). Location validation and line
  alignment run over the whole PR's files, so such a finding is kept and
  anchored like any other. Nothing marks it as coming from an excerpt.
- **#14 The judge shares the review's backend.** Only `--judge-model` changes
  which model judges. Self-judging is detected by exact model name; it warns
  and stamps `self_judged`, and is never refused. Scoring needs
  human-labelled cases, and the repository ships three.
- **#15 Two ways a GitHub run past the diff limit can take long or fail.**
  - A PR pushed to between the PR read and the listing pages can fail the
    totals check. The run then ends as `Error`, never as a partial review.
  - A compare read that times out is retried by the session. The retry policy
    is `LoggingRetry(total=3)` with a 30-second read timeout, so reaching the
    fallback can take up to four such timeouts.
- **#16 Only GitHub and Bitbucket Cloud read description history.**
  - **Other forges.** On GitLab, Azure DevOps and Bitbucket Server, a replay
    logs a WARNING and uses the live title and description. An explicit
    `--as-of` there exits 2.
  - **Unprobed endpoint.** GitHub Enterprise Server's GraphQL endpoint
    (`https://<host>/api/graphql`) has not been probed live.
  - **Large PRs.** A PR with more than 5,000 reviews, comments or title renames
    pins nothing, so it replays the live text even under `--as-of`.
  - **Rate limit.** Bitbucket Cloud allows 60 anonymous reads an hour, and a
    history read costs at least three. So a replay without a credential can
    fall back to the live text.
  - **Unverified Bitbucket Cloud shapes.** The `changes_requested` activity
    entry has not been seen live (0 in 4 feeds) and is read by analogy with an
    approval. It is also unverified whether the commit `date` used as the head
    commit's date is the author date or the committer date.
  - **CRLF.** GitHub stores some description versions with CRLF line endings,
    and a pinned replay passes them to the prompt verbatim.
- **Spec digest.**
  - Unpunctuated keyword lines in one paragraph merge into one constraint, and a
    run of them past 400 characters is cut.
  - A spec directory reads only its first 20 files. It names the files it skips
    in a log line only, and its constraints carry the directory's name rather
    than each file's.
  - A Jira ticket's fixed 6,000-character share can crowd out the rest of the
    spec.
  - A `spec` finding's quote is not checked against the digest.
  - A `Spec: "…"` quote that never closes is barely exempt from the hedge gate.
  - A Jira ticket passed with `--spec` sets no finding's scope.
  - Grounding can crowd out a generic finding. In the bundled eval, case-002's
    one expected non-spec finding was missed in 4 of 4 grounded runs across
    two models and found in 3 of 4 ungrounded runs. That is two runs per model
    in each arm, and the cause is inferred, not proven.
- **`kiro-cli`.**
  - It always reports "cost unknown".
  - Whether user-level Kiro configuration such as `~/.kiro/steering/` reaches
    prxref's per-call agent has not been verified.
  - The model that actually ran is not reported.
  - Every chat is kept under `~/.kiro/sessions/cli/`.
- **GitLab.**
  - Files GitLab withholds as `too_large` or `collapsed` are listed header-only.
  - An MR past 5,000 files fails.
  - Reading an MR's threads on gitlab.com needs `PRXREF_GITLAB_TOKEN`, even for
    a public project. Without one, thread dedup runs against no threads.
- **Replay from a diff file.** A `--diff-file` run without `--pr-url` has no
  threads, and no file context unless `--repo-dir` names a checkout. The
  title comes from the patch mail or the file name. The description comes
  from the mail, `--description-file` or `--no-description`.
- **Azure DevOps.**
  - Only anonymous reads are verified live: the forge reads, the dry-run output
    shape, the pinned-range compare diff, a pinned-range replay, and, new in
    0.16.0, the repository file listing.
  - Posting, pruning, PAT and `SYSTEM_ACCESSTOKEN` authentication and service
    hooks are tested against recorded API shapes only.
  - Azure DevOps Server is untested.
- **Gating.** A mistyped or unrecognized `--pr-url` exits 0 even under
  `PRXREF_FAIL_ON=error` or `any`, because nothing was reviewed.
- **One seam is tested only in halves on two paths.** The size advisory and the
  cost label are tested together on the main summary post
  (`tests/test_release_seams.py`). On the inline-accounting refresh post and on
  the summary-only run, each is tested alone.
- **Scope labelling is measured on one fixture shape.** An off-ticket change
  inside an on-ticket file is unmeasured.

Follow-ups a maintainer can act on:

- **Tune #10's threshold.** Run `prxref eval` on a real replay set, then
  consider changing the default. Use replays pinned by #16: earlier tuning ran
  on replays that leaked the PR's current description.
- **Measure #18's default cap.** The issue's before and after numbers come from
  a simulation over graded replays, and were not re-measured on prxref's own
  output. `prxref eval compare` on two labelled runs measures it: `prxref eval
  run` the same cases with the same `--rules-file` twice, once with
  `PRXREF_MAX_FINDINGS_PER_RULE=0` and once at the default, `prxref eval score`
  both, then compare them. Each run's `run.json` records the cap it used.
- **The #17 fixture's second label under-counts.** In
  `tests/fixtures/issue17/cases.json`, its `must_match` requires "mutually
  exclusive" or "exactly one of", which is narrower than the wording the
  model writes for the same defect. Widen the predicate, or grade it with
  the judge, before using the fixture to measure repository context.
- **Unchecked anchors.** `eval_cases.check_anchors` is public but never runs on
  a `pr_url` case's fetched diff, so those labels are only shape-checked.
- **Eval cases cannot pin the description.** A case file cannot carry `as_of` or
  `description_file`, because `eval_cases._CASE_KEYS` has neither.
- **Gaps in the judge's numbering.** The judge numbers references over the
  whole record but is shown only the findings in labelled files. So it can see
  A1 and A3 with no A2.
- **`{repo_hint}` is never passed.** `orchestrate_review` never passes it, so
  the slot reads `(unspecified)` in every review.
- **The posted summary never counts dropped findings.**
  `orchestrator._render_summary` does not tally drop reasons.
  `formatter.format_summary` and its `_dropped_section` are imported by nothing
  in `src/`. Wire them in or delete them.
- **Duplicated code in `rules.py`.** `rules._read_scoped_file` duplicates the
  read block of `load_review_rules`. A `_reason` helper exists in both
  `rules.py` and `prompt_templates.py`.
- **Missing definitions at `off`, and for Go and Rust.** With repository
  context off, `chunk_context.referenced_definitions` still looks only in the
  same file, for JavaScript, TypeScript, Python, Java and Kotlin. `diff` and
  `repo` add other files, with Java and Kotlin types only. Go and Rust get
  dependency versions but no definitions at any level:
  `chunk_context._definition_regexes` has no regex for them.
- **The history read is untraced.** The replay's history read is outside the
  trace, and only the `replay` stamp records its outcome.
- **The sweep's example is always in force.** `orchestrator._example_titles`
  always reads the systemic template. So the sweep's example title is in force
  even on a run whose sweep never runs.

| Item | Value |
|---|---|
| Released version | `0.23.0` (minor: graceful degradation when the token cannot post, #48, on by default but acting only on a failed post; one new config key, `PRXREF_FALLBACK`; one new run-record and `--format json` key, `degraded`, `null` when no post failed; one new module, `ci_fallback`; no new CLI flag or `Forge` method) |
| Registration points | CI fallback: `ci_fallback.detect_ci` (which CI) and `cli._emit_fallback` (what each CI gets); forges: the tuple in `forges/base.py` (`detect_forge`) and the `impls` dict in `config.py` (`make_forge`); repository listing: the optional `Forge.list_paths` in `forges/base.py`, on every adapter; summary read-back: the optional `Forge.get_summary`, on every adapter; the reviewed-head marker: `orchestrator.REVIEWED_HEAD_PREFIX` and `REVIEWED_HEAD_SUFFIX`; repository-context entries: `repo_context.KINDS`, where an entry's kind picks the prompt block it renders in and the tuple's order ranks nothing, and `REASONS`, whose order is the budget's rank; LLM backends: `llm_backends.BACKENDS`; glyphs: `prxref.markers`; subcommands: `cli._build_parser`; prompt templates: `prompt_templates.TEMPLATE_NAMES` and `OPTIONAL_PLACEHOLDERS` |
| Version strings | `pyproject.toml`, `src/prxref/__init__.py`, and `uv.lock` |
| Test command | `uv run pytest` (dev tools are a `[dependency-groups]` group, not an extra) |
| Release assets | wheel **and** sdist attached by `release.yml`; PyPI by OIDC trusted publishing |
