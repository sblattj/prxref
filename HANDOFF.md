# HANDOFF — v0.19.0 shipped: JVM BOM owners and litellm effort/seed (#25, #26)

**Repo:** `sblattj/prxref` (public) · **Released:** 2026-09-27 · **Supersedes** the
v0.18.0 handoff.

0.19.0 is two fixes. #25: in the `### Dependency versions` block, a
version-less Maven or Gradle dependency was attributed to the first imported
BOM whether or not that BOM manages it; it now names an owner only when there
is one candidate or a clear best guess, and marks a dependency matched to an
import by its group alone. #26: the litellm backend now forwards
`PRXREF_LLM_REASONING_EFFORT`, and `PRXREF_LLM_SEED=off` sends no seed, for
providers such as Bedrock that reject it. No config key, run-record key or
module was added. At the defaults nothing moves except the dependency-version
lines of Java and Kotlin files. The user-facing account is the `[0.19.0]`
section of `CHANGELOG.md`. This file is for whoever cuts the next release.
The v0.18.0 handoff is in git history.

## What landed

- **The change, as a module map.** `git diff --stat v0.18.0..HEAD -- src/`
  touches four files and adds none.
  - `jvm_maven`, the owner rule (#25). `managed_dependency(group_id,
    artifact_id, owners, kind="bom")` takes the candidate owners in declared
    order and returns a `MavenDependency`. `MavenDependency` gains `likely`,
    `owners` and `owner_kind` (one of `OWNER_KINDS`: `bom`, `parent`,
    `platform`); the positional four-field form is unchanged. `_resolve`
    builds the candidates as the external parent, if any, then the imported
    BOMs, nearest pom first, and hands them to `managed_dependency` with kind
    `parent` or `bom`.
  - `jvm_deps`, the Gradle side and the group-only mark (#25).
    `_gradle_owner` is gone; `_gradle_line` calls `managed_dependency` with
    the platforms as candidates and kind `platform`. `_matches` also returns
    whether the best match scored above 0, and `_dependency_lines` appends
    `GROUP_MATCH_SUFFIX`, ` (group match only)`, to a line only a 0-score
    match produced.
  - `llm_backends`, the litellm fix (#26). `LiteLLMClient` takes
    `reasoning_effort` (normalised with `or None`) and sends it as
    `reasoning_effort=` after `seed` when truthy. `create_llm_client` passes
    the configured effort to it, and reads the raw seed once: when it strips
    to exactly `SEED_OFF` (`"off"`), the seed is `None` and
    `_auto_run_seed` is never called; otherwise the 0.18.0 path is unchanged.
  - `config`, the value (#26). `_SEED_OFF` restates `"off"` privately (a
    test pins it equal to `llm_backends.SEED_OFF`); `_coerce_env` returns
    it for `llm_seed` before integer coercion, and `_check_ranges` skips it
    as it skips `None`. Lowercase only: `OFF` exits 2, like every other
    choice-style value `config.py` checks.
- **The owner rule.** With one candidate, it is named as in 0.18.0:
  `g:a@(managed by bg:ba@bv)`. With several, each candidate's groupId is
  scored by the leading segments it shares with the dependency's; the best is
  named as `g:a@(likely managed by bg:ba@bv)` only when it shares at least
  `MIN_OWNER_SHARED_SEGMENTS` (2) and strictly more than every other.
  Otherwise no owner is named and at most `MAX_LISTED_OWNERS` (3) artifactIds
  are listed in declared order, then `+K more`: `one of N imported BOMs: ...`,
  `the parent or an imported BOM: ...` or `the parent or one of B imported
  BOMs: ...` (the parent listed first, B counting the BOMs only), or `one of
  N platforms: ...`. The external parent takes part in the guess, so
  `spring-boot-starter-parent` can be the likely owner of a
  `org.springframework.boot` starter. Gradle always named one platform
  before, the one sharing the most segments and the first on a tie, even at
  0 shared; it now follows the same rule.
- **Why it lives in `jvm_maven`.** `jvm_deps` imports `jvm_maven`, and
  `test_the_module_imports_only_the_standard_library` in
  `tests/test_jvm_maven.py` forbids `jvm_maven` importing any non-stdlib
  module, prxref's own included. So the one helper both Maven's `_resolve`
  and Gradle's `_gradle_line` can call without an import cycle is in
  `jvm_maven`.
- **The group-only mark.** A match scores 0 when no artifactId token names a
  segment of the import (`org.slf4j:slf4j-api` for `import
  org.slf4j.Logger`). Such lines are kept, not dropped, because a true
  dependency such as `spring-webmvc` for `org.springframework.web` also
  scores 0; every tied 0-score match is marked. An artifact that another
  import of the same file matches by name renders once, unmarked, in either
  import order.
- **Effort and seed on litellm.** Unset effort leaves the kwargs exactly as
  0.18.0 sent them. `off` applies on `openai-compat` and `litellm`, which
  then send no `seed`, and the run record's `sampling.seed` is `null`. On
  `claude-cli` and `kiro-cli`, which never send a seed, `off` is not an
  error. Error texts for a bad seed are unchanged.
- **Tests.** Two new files, `tests/test_issue_25_bom_owner.py` and
  `tests/test_issue_26_litellm_effort_seed.py`. The group-only mark moved
  existing assertions: 18 expected lines in `tests/test_jvm_deps.py` gained
  the mark, the Gradle owner test there and
  `test_owner_precedence_is_external_parent_then_boms_nearest_first` in
  `tests/test_jvm_maven.py` now expect the new owner text, and the slf4j line
  of `tests/test_issue_20_acceptance.py` gained the mark. The #26 change
  moved no existing test.

## What this release taught

Written down because each one cost real time.

1. **"First candidate wins" is a silent wrong answer, and a test pinned
   it.** 0.17.0 named the external parent, or else the nearest first
   imported BOM, as the owner of every version-less dependency.
   `test_owner_precedence_is_external_parent_then_boms_nearest_first` in
   `tests/test_jvm_maven.py` had three candidates and asserted that
   `jackson-databind` was managed by `corporate-parent`, then by
   `spring-boot-dependencies`, with `jackson-bom` imported beside them: it
   pinned the order of the candidates, not which one manages the artifact,
   so it read as a precedence rule rather than a wrong answer. On #25's repro,
   `dependency_lines` at v0.18.0 printed `managed by
   com.fasterxml.jackson:jackson-bom@2.21.5` for both
   `jackson-databind` and `micrometer-registry-dynatrace`; at 0.19.0 the
   first is `likely managed by` jackson-bom and the second is `managed by one
   of 2 imported BOMs: jackson-bom, spring-boot-dependencies (group match
   only)`. A test of a rule that picks one of several candidates should say
   why the expected one is right, and include a case where the first is
   wrong.
2. **The local diff mode cannot check dependency lines end to end.** The same
   repro through `prxref review --no-post --diff-file ... --repo-dir ...`
   produced no dependency block at either version, because chunk context
   reads only through the forge (`orchestrator._make_file_reader`), and a
   `--diff-file` run has no forge. The end-to-end check had to call
   `jvm_deps.dependency_lines` with a reader over the repro on disk (Still
   open, "Replay from a diff file").
3. **A provider-parameter fix needs the real library in the loop.** The
   mocked tests prove what prxref passes to `litellm.completion`, not what
   litellm does with it. An offline probe through real litellm (a dead
   proxy, fake AWS credentials, a Bedrock model, effort `medium`) showed the
   reported bug before any request: with `PRXREF_LLM_SEED` unset or `7`,
   litellm raised `UnsupportedParamsError` naming `seed`. With `off`, param
   mapping passed, `reasoning_effort` included, and the call failed only
   with `APIConnectionError`, where the network was blocked on purpose.

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
Current values: **69** keys, **1** legacy alias, **70** accepted names,
unchanged from 0.18.0.

## Release shape (follow this next time)

How 0.19.0 was built:

1. **Two parallel code tasks from the 0.18.0 merge.** Each ran in its own
   worktree against the pinned 0.18.0 merge commit, brought its own tests in
   a new test file, and kept to its own files: the #26 task `config.py`,
   `llm_backends.py` and the seed and effort docs in `README.md`,
   `docs/env-vars.md`, `.env.example` and `docs/llm.md`; the #25 task
   `jvm_maven.py`, `jvm_deps.py`, the existing JVM tests and the Java and
   Kotlin bullet of `docs/llm.md`, the one file both edited, in separate
   regions. Both were cut off by a usage
   limit mid-task and resumed from their uncommitted worktrees without loss.
2. **One integration gate per merge.** Each branch merged into
   `release/X.Y.Z` on its own, and a merge stayed only if the full
   `uv run pytest` and `uv run ruff check src tests` passed on the merged
   tree. The passing count rose from 8,865 at 0.18.0 to 8,900 after the
   effort and seed fix and 8,926 after the BOM-owner fix, and never fell.
3. **Two offline end-to-end checks**, once both merged, one per issue (Live
   checks). No live model run: nothing model-facing changed except the JVM
   dependency lines.
4. **Release.** This commit bumps the version, adds the CHANGELOG section,
   and rewrites this file.

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
8926 passed                                   uv run pytest -q
All checks passed!                            uv run ruff check src tests
0.19.0                                        uv run prxref --version
```

These counts come from the release branch, measured at the commit that last
updated this file.

### Live checks

0.19.0 changes no model-facing behaviour except the JVM dependency lines, so
no live model run was made for it. Two offline end-to-end checks ran instead:

- **#25 on the issue's own repro, built on disk.** A root pom importing
  `com.fasterxml.jackson:jackson-bom:2.21.5`, then
  `org.springframework.boot:spring-boot-dependencies:3.5.0`; an `app` module
  with version-less `io.micrometer:micrometer-registry-dynatrace` and
  `com.fasterxml.jackson.core:jackson-databind`; a Java file adding
  `import io.micrometer.core.instrument.MeterRegistry;` and
  `import com.fasterxml.jackson.databind.ObjectMapper;`.
  `jvm_deps.dependency_lines` with a reader over the directory printed:
  - at 0.19.0:
    `com.fasterxml.jackson.core:jackson-databind@(likely managed by
    com.fasterxml.jackson:jackson-bom@2.21.5)` and
    `io.micrometer:micrometer-registry-dynatrace@(managed by one of 2
    imported BOMs: jackson-bom, spring-boot-dependencies) (group match
    only)`;
  - at v0.18.0, the control: `managed by
    com.fasterxml.jackson:jackson-bom@2.21.5` for both.

  Through `prxref review --no-post --diff-file ... --repo-dir ...` the same
  repro gave no dependency block at either version (lesson 2).
- **#26 on the real litellm library, offline.** A dead proxy, fake AWS
  credentials, the model `bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0`,
  `PRXREF_LLM_REASONING_EFFORT=medium`, `LITELLM_DROP_PARAMS` unset.
  `PRXREF_LLM_SEED` unset or `7`: `UnsupportedParamsError` naming `seed`
  before any request, the reported bug. `off`: param mapping passed,
  `reasoning_effort` included, and the call failed only with
  `APIConnectionError`, the network being blocked by design.
- The last live model result is 0.18.0's, the context follow-up on #22's
  fixture through GLM 5.3 Flash: see the intro of the `[0.18.0]` section of
  `CHANGELOG.md`.

## Still open — not part of this release

- **The history-window bug is still not asserted.** With the follow-up on,
  #22's second bug surfaced in 2 of 3 live runs, and both times its
  definitions were looked up and sent, but GLM 5.3 Flash did not re-assert
  it at or above the 0.60 floor, so it was dropped as "not confirmed". On
  that model, nothing in 0.18.0 or 0.19.0 posts it. A stronger model, or
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
- **A truncated review reply is not retried**, by design: its error already
  names `PRXREF_LLM_MAX_TOKENS`, whatever `PRXREF_LLM_PARSE_RETRIES` says.
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
  changed in 0.16.0 through 0.19.0; raise both for such a model. The
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
  file context and no threads. `--repo-dir` gives it repository context,
  but the dependency-versions and same-file definitions blocks, Java and
  Kotlin ones included, still read only through the forge
  (`orchestrator._make_file_reader`), so they stay empty; 0.19.0's #25
  check hit this (lesson 2). The title comes
  from the patch mail or the file name. The description comes from the
  mail, `--description-file` or `--no-description`.
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
| Released version | `0.19.0` (minor: the JVM BOM-owner fix, #25, and the litellm effort and seed fix, #26; no new config key, one new value, `PRXREF_LLM_SEED=off`; no new run-record or `--format json` key; no new module; no new CLI flag; no existing config default changed; at the defaults only the dependency-version lines of Java and Kotlin files change) |
| Registration points | forges: the tuple in `forges/base.py` (`detect_forge`) and the `impls` dict in `config.py` (`make_forge`); repository listing: the optional `Forge.list_paths` in `forges/base.py`, on every adapter; repository-context entries: `repo_context.KINDS`, where an entry's kind picks the prompt block it renders in and the tuple's order ranks nothing, and `REASONS`, whose order is the budget's rank; LLM backends: `llm_backends.BACKENDS`; glyphs: `prxref.markers`; subcommands: `cli._build_parser`; prompt templates: `prompt_templates.TEMPLATE_NAMES` and `OPTIONAL_PLACEHOLDERS` |
| Version strings | `pyproject.toml`, `src/prxref/__init__.py`, and `uv.lock` |
| Test command | `uv run pytest` (dev tools are a `[dependency-groups]` group, not an extra) |
| Release assets | wheel **and** sdist attached by `release.yml`; PyPI by OIDC trusted publishing |
