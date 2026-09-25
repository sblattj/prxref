# HANDOFF — v0.17.0 shipped: JVM chunk context, parse retry, shared-state readers

**Repo:** `sblattj/prxref` (public) · **Released:** 2026-09-25 · **Supersedes** the
v0.16.0 handoff.

0.17.0 gives a chunk worker Java and Kotlin chunk context (#20): for a changed
`.java`, `.kt` or `.kts` file, the definitions its added lines reference from
the rest of that file and the Maven or Gradle versions of what they import, at
every `PRXREF_REPO_CONTEXT` level. A model reply that cannot be used as a
review is sent again, up to `PRXREF_LLM_PARSE_RETRIES` times, default 1 (#21).
At `PRXREF_REPO_CONTEXT=repo` a chunk also sees unchanged code that reads the
state its added lines write, and a new deterministic check, always on, flags a
toggle that ships on while the pull request's own test setup pins it off (#22,
parts 2 and 3). `off` still adds no repository context, but a pull request with
Java or Kotlin files gets new prompt blocks and forge reads with every setting
at its default, and a `{}` reply now costs a second call. The user-facing
account is the `[0.17.0]` section of `CHANGELOG.md`. This file is for whoever
cuts the next release. The v0.16.0 handoff is in git history.

## What landed

- **#20 Java and Kotlin chunk context, as a module map.** Four new modules,
  stdlib only, that do no I/O except through a `read(path)` callable:
  - `jvm_lang`: `jvm_language` (`.java` gives `java`, `.kt` and `.kts` give
    `kotlin`, in any case), `definition_regexes` (Java types, methods, fields
    and constants, and enum constants; Kotlin types, `fun`, and `val` and
    `var`), `annotation_start`, `parse_imports`, `keywords` and `JDK_NAMES`.
  - `jvm_maven.resolve_pom`: a `pom.xml` through its `<properties>`, its
    parent chain inside the repository, `<dependencyManagement>` and imported
    BOMs.
  - `jvm_gradle.gradle_build`: string-notation dependencies, `platform`,
    `enforcedPlatform` and `mavenBom` as BOM owners, and the
    `libs.versions.toml` catalog, probed at `catalog_paths` only when the
    build file mentions `libs.`.
  - `jvm_deps.dependency_lines`: finds the nearest build file
    (`_nearest_manifest`, trying `MANIFEST_NAMES` at each level from the
    file's directory up to the root), then matches the added lines' imports
    to its dependencies, skipping `SKIPPED_ROOTS` and the project's own group.

  How they reach the prompt: `chunk_context._language` now asks
  `jvm_lang.jvm_language`; `chunk_context.dependency_versions` hands a Java or
  Kotlin file to `jvm_deps.dependency_lines`, and skips `build.gradle.kts` and
  `settings.gradle.kts` (`_GRADLE_SCRIPTS`) without a read; and
  `chunk_context.referenced_definitions` uses `jvm_lang`'s regexes and starts
  an entry at the annotation lines above a definition. Both run in
  `orchestrator._context_blocks`, over the reader that
  `orchestrator._make_file_reader` builds. `orchestrate_review` builds that
  reader before, and outside, its `repo_context != "off"` gate, so these
  blocks and their reads happen at every level, `off` included, whenever the
  forge has `get_file_content` and the pull request has a head sha. The
  reader is cached per run and is not counted against the repository-context
  read caps.
- **#20 in repository context.** `repo_context.language_of` claims `.kt` and
  `.kts` as `kotlin`, and `repo_context.definition_regexes` gives Kotlin one
  regex, `jvm_lang.KOTLIN_TYPE_RE`, for type declarations only, as Java has
  one for its types. Methods, functions and properties stay with chunk
  context. `repo_context` keeps the 0.16.0 Java names (`_JAVA_DEF_RE`,
  `_JAVA_KEYWORDS`, `_JDK_NAMES`) as aliases of `jvm_lang`'s objects.
- **Who shows a chunk file's own definitions.** Chunk context and repository
  context both serve definitions, and they split by name, not by file
  (lesson 3). `repo_crosschunk._SAME_FILE_LANGUAGES` is `js`, `python`,
  `java` and `kotlin`. With a reader, `repo_crosschunk.diff_definitions`
  searches a chunk file in one of those languages only for the wanted names
  that are not identifiers on that file's own added lines, because
  `chunk_context.referenced_definitions` already shows those. A name that
  only another file of the chunk references is still found there, as a
  `diff-file` entry, and a file with no name left is skipped without a read.
- **#21 `PRXREF_LLM_PARSE_RETRIES`.** `reviewer._invoke_and_parse` holds the
  whole mechanism, for chunks and the sweep alike.
  - `_read_reply` classifies a reply and `_may_retry` spends one shared
    budget, N. Retried: an empty reply, one that does not parse, one that is
    not a JSON object and, at N of 1 or more only, an object without a
    `findings` list (`_NO_FINDINGS_ERROR`). An empty reply keeps 0.16.0's one
    retry even at N = 0.
  - Never retried: a reply the provider stopped at the budget
    (`_TRUNCATION_FINISH_REASONS`, `length` or `max_tokens`) and a call that
    raised.
  - A unit makes at most `1 + max(N, 1)` calls, so with the orchestrator's
    timeout retry a chunk tops out at `2 * (1 + max(N, 1))`: 4 at the
    default, as in 0.16.0.
  - `_fold_retry_usage` sums every call's tokens and time, and the unit's
    `model` is the last call's. A traced unit that retried keeps
    `<label>.attempt<K>.response.json` for each discarded reply, and its meta
    gains `parse_retries` and `first_error`, which `orchestrator._retry_meta`
    carries into the worker result.
  - `orchestrate_review` (`llm_parse_retries`), `review_chunk` and
    `review_systemic` default to 0 for library callers. `cli._run_review`
    passes the config value, so `prxref review`, the webhook server and
    `prxref eval run` get 1.
- **#21 recording and the judge.**
  - The run record and `--format json` gain `parse_retries`, right after
    `repo_context` (`cli._build_json_result`): `null` at N = 0, otherwise
    `orchestrator._parse_retry_total` over every chunk and the sweep.
  - `eval_judge.judge_case` retries on any `JudgeParseError`, truncation
    included, up to N times, and `JudgeOutcome.parse_retries` counts them. At
    N = 0 an empty judge reply gets no retry. `score.json`'s `judge` block
    gains `parse_retries` after `llm_calls`, and `score.md`'s cost line names
    the retries only when there were any.
  - `evals.RUN_CONFIG_KEYS` gains `llm_parse_retries`, 17 keys in all.
- **#22 part 2, shared-state readers.** `repo_readers` is new and pure.
  - `shared_state_keys` takes the keys the added lines write: subscript
    stores, and calls of a `WRITE_VERBS` verb, through a local alias. A key
    the added lines assign a new value is skipped.
  - `reader_candidates` orders the listed, unchanged, same-language files by
    shared leading directories; `reader_matches` finds the reads (`.key`,
    `["key"]`, or `key.<method>(` with a method that is not a write verb);
    `reader_entries` runs the search and cuts each excerpt from the enclosing
    definition down to the read.
  - `repo_unit.build_unit_context` calls it at `repo` only, with a file
    listing, after the resolver, on the reads the other sources leave.
    `repo_context.KINDS` gains `reader`, and `REASONS` gains `shared-state`,
    ranked last so the budget cuts it first.
  - `chunk_context.render_context_blocks` takes `reader_lines` and renders
    them last, under `READER_HEADER` (`### Code elsewhere that reads state
    this chunk writes`). With it empty, the output is unchanged.
- **#22 part 3, the pinned-off toggle.**
  `heuristics.toggle_pinned_off_findings(files)` is pure and always on. It
  posts one `warning` at confidence 1.0 on a toggle the pull request adds
  with a default of on, when the pull request also adds a line to
  `conftest.py`, `setupTests.*`, `jest.setup.*` or `vitest.setup.*` that pins
  it off. The body ends `(deterministic check, no model)`. `orchestrate_review`
  folds it in beside the release-shape finding, on the main path and on the
  path for a diff with no chunks, so it goes through every quality pass.
- **Caps**, each read from its constant.
  - Chunk context, unchanged: `chunk_context.MAX_DEFINITION_ENTRIES` (40),
    `MAX_LINES_PER_DEFINITION` (6, annotation lines included),
    `MAX_DEFINITION_CHARS` (8,000) and `MAX_FILE_BYTES` (512 KiB).
    `jvm_lang.MAX_ANNOTATION_LINES` (2) bounds the annotation lines an entry
    starts at.
  - Maven: `jvm_maven.MAX_POM_BYTES` (512 KiB), `MAX_POM_PARENTS` (5),
    `MAX_PROPERTY_PASSES` (5) and `MAX_INTERPOLATED_CHARS` (1,024). A
    `pom.xml` that holds `<!DOCTYPE` or `<!ENTITY` is refused before the XML
    parser runs.
  - Dependency matching: `jvm_deps.MIN_GROUP_PREFIX_SEGMENTS` (2) and
    `MIN_SHARED_SEGMENTS` (3).
  - Readers: `repo_readers.MAX_READER_ENTRIES` (6), `MAX_READER_SCAN` (24)
    and `MAX_READER_LINES` (8), within `repo_reader.MAX_CHUNK_READS` (16) and
    `MAX_RUN_READS` (200). They are module constants with no config key, and
    setting `MAX_READER_ENTRIES` to 0 turns readers off with no read.
- **Config went from 67 to 68 keys.** The one new key is
  `PRXREF_LLM_PARSE_RETRIES` (integer, at least 0, no upper bound, default 1).
  No existing config default changed.

## What this release taught

Written down because each one cost real time.

1. **A golden "from the released version" can silently be the tree.** Run
   inside the checkout, `uv run --no-project --with prxref==0.16.0` imported
   the working tree's prxref, whose `__version__` read 0.16.0 until this
   commit bumped it. The first cross-check of the parse retry against 0.16.0
   therefore compared the tree with itself, and its 0 differences meant
   nothing. Only `prxref.__file__`, `src/` rather than `site-packages`,
   showed it. The fix is in `tests/fixtures/issue20/make_golden.py`: run from
   outside the repository with `uv run --isolated --no-project --with
   prxref==0.16.0`, and refuse to write unless `prxref.__file__` is under
   `/site-packages/` as well as the version matching. Assert where the module
   came from, never only its version string.
2. **"Off is byte-identical" was a claim about prompts, and #17's test also
   pinned forge reads.** Java and Kotlin chunk context added 23 `off` reads
   to the #17 fixture, 21 build-file probes over 7 directory levels and its
   two Java files, while every `off` prompt stayed byte-identical to
   0.15.0's. `TestOffMatchesTheReleased015` in
   `tests/test_issue_17_acceptance.py` went red 12 times, on the read pins
   alone. It now compares the prompts with the 0.15.0 golden byte for byte
   and the reads only after leaving out JVM paths (`_is_jvm`);
   `test_off_jvm_reads_are_the_pinned_list` pins those reads literally
   (`JVM_READS`), and `test_the_golden_reads_no_jvm_path` proves the filter
   hides nothing the golden holds. When a feature adds reads on purpose,
   split the pin by what changed; regenerating the golden would stop it
   being a 0.15.0 oracle.
3. **Two sources that serve definitions must split by name, not by file.**
   Chunk context now shows a Java or Kotlin file's own definitions, so
   repository context had to stop repeating them. Leaving every chunk file in
   a same-file language out of `repo_crosschunk.diff_definitions` did that,
   but it also lost a definition that one file of a chunk references and
   another file of the same chunk holds outside its hunks, and 7 tests beyond
   the #17 read pins went red. The rule that shipped leaves out only the
   names on a file's own added lines (What landed). `TestTheChunksOwnFiles`
   in `tests/test_repo_context_crosschunk.py` pins it for Java, Kotlin,
   Python and JavaScript, and it fixed the same gap in 0.16.0's Python and
   JavaScript handling (CHANGELOG `[0.17.0]`, Fixed).
4. **The parse retry changes behaviour at its default, and `0` is exactly
   0.16.0.** At `PRXREF_LLM_PARSE_RETRIES=1` a `{}` reply costs a second call,
   and the unit fails if that reply has no `findings` list either, where
   0.16.0 counted a clean review. `TestZeroIsTheOldBehaviour` in
   `tests/test_issue_21_acceptance.py` pins `0`. A one-off cross-check ran 6
   reply scenarios through the tree at `0` and through the released 0.16.0,
   installed as lesson 1 says, and found 0 differences in exit code, calls
   per unit, request bodies and JSON output. The control, the tree at `1`,
   differed in 5 of the 6. A custom worker or systemic template whose reply
   drops `findings` now fails every unit, and no template check catches it.
5. **The reader block made a missed bug an asserted one, once.** On issue
   #22's own fixture through GLM 5.3 Flash (N=3 per arm, interleaved, one
   factor varied), the shared-state reader block turned the serialization
   bug from absent or dropped under the confidence floor (0 of 3 active)
   into an active error-severity finding (3 of 3), for 216 more input tokens
   per review. The history-window bug surfaced in 3 of 3 runs with readers,
   but always under the 0.60 floor. The toggle check fired in all 6 runs.
   Three runs an arm on one fixture is a first measurement, not a verdict on
   the feature (Live checks).
6. **A pass that runs over every finding runs over the deterministic ones
   too.** The toggle check has no model, yet its finding was posted as an
   `error` in 2 of arm B's 3 live runs and as a `warning` in the third
   (Live checks). Severity consistency had raised it: a model `error` in the
   same file shared a rare code token with it. Those two runs logged
   `severity consistency: raised 1 finding(s) via shared rare code
   token(s)`, binding on `true` in one and on `assistant_progress_notes` in
   the other, and the third run logged no such line. Only the variation gave
   it away, because a check with no model should post the same severity on
   every run. A finding that `heuristics.is_deterministic` marks now takes no
   part in severity consistency. `TestDeterministicFindingsKeepTheirSeverity`
   in `tests/test_quality.py` and `tests/test_deterministic_severity.py`,
   through the local review path on #22's fixture, pin it, each with a
   control that shows the raise without the exemption. Finding grouping,
   which is opt-in, can still raise one (`docs/quality.md`).

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
key. The one key 0.17.0 added, `llm_parse_retries`, feeds it this way, and
`prxref eval score` also reads it for the judge. Current values: **68** keys,
**1** legacy alias, **69** accepted names.

## Release shape (follow this next time)

How 0.17.0 was built:

1. **One survey and one decisions file first.** A read-only survey of the
   code #20, #21 and #22 would touch came first, down to the test pins each
   change would move. A decisions file then settled every open question
   before any code was written: keep the 0.15.0 `off` golden and re-scope
   its read check rather than regenerate it; put the JVM parsing in new
   stdlib leaf modules, because `chunk_context` cannot import `repo_context`
   without a cycle; the groupId matching rule; one shared parse-retry budget
   that keeps 0.16.0's empty-reply retry, with library callers at 0; readers
   inside repository context, at `repo` only and ranked last; the toggle as a
   deterministic check rather than a prompt hint; and #22 part 1 deferred.
   It also gave each new module one owner.
2. **Foundation.** One task landed `PRXREF_LLM_PARSE_RETRIES` on every config
   surface, and it merged first, before the wiring that reads it.
3. **Pure modules first, wiring last.** Tasks ran in rounds of parallel
   agents, each agent in its own worktree against a pinned base commit. Each
   task was rated at most 5 of 10 for complexity, and each code task brought
   its own tests. 20 tasks merged in five rounds:
   - 7: the config key, the JVM language module, the Maven parser, the Gradle
     parser, the reviewer's parse retry, the shared-state reader search, and
     the toggle check
   - 4: the JVM dependency matcher, the orchestrator and CLI threading of the
     retry budget with its record key, the judge's retry, and the reader
     wiring into the unit context
   - 5: the chunk-context JVM wiring with the own-file rule, Kotlin in
     repository context, the eval recording, the #21 acceptance tests, and
     the toggle wiring
   - 1: the #22 acceptance tests over the issue's own fixture
   - 3: the #17 acceptance re-scope (lesson 2), the #20 acceptance tests
     against a golden from the released 0.16.0 (lesson 1), and the user
     documentation with the CHANGELOG section
4. **One integration gate per merge.** Each branch merged into `release/X.Y.Z`
   on its own, and the 0.16.0 release commit was merged in after the first.
   A merge stayed only if the full `uv run pytest` and
   `uv run ruff check src tests` passed on the merged tree, with one
   exception. The chunk-context JVM wiring was first held at 19 failures
   (lesson 3), came back with 12, every one an `off` read pin in
   `tests/test_issue_17_acceptance.py` with every prompt intact (lesson 2),
   and merged with those 12 known, because the next round's re-scope owned
   them. That re-scope turned them green: 8,660 passed, 0 failed. Over the 20
   merges the passing count rose from 7,675 at 0.16.0 to 8,695 and never
   fell; the documentation merge added no tests.
5. **One read-only live check**, once the reader and toggle wiring merged:
   the reader block against no reader on #22's fixture, through one model
   and one lane, with a guard that blocked forge writes (Live checks).
6. **Release.** This commit bumps the version, dates the CHANGELOG, corrects
   the `PRXREF_REPO_CONTEXT` row of `docs/env-vars.md` and rewrites this
   file, after the documentation task wrote the CHANGELOG section.

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
8703 passed                                   uv run pytest -q
All checks passed!                            uv run ruff check src tests
0.17.0                                        uv run prxref --version
```

These counts come from the release branch, measured at the commit that last
updated this file.

### Live checks

- **Shared-state readers on issue #22's own fixture**, GLM 5.3 Flash through
  llm-ferry (`domestic.flash`), on the local CLI path (`--diff-file`,
  `--repo-dir`, `--description-file`) at `PRXREF_REPO_CONTEXT=repo`,
  `PRXREF_LLM_MAX_TOKENS=32768` and `PRXREF_LLM_TIMEOUT=900`. One factor
  varied: arm A set `repo_readers.MAX_READER_ENTRIES` to 0, and arm B kept
  the default of 6. The runs were interleaved, A B A B A B, N=3 per arm, with
  no re-rolls.
  - The reader block reached a prompt only in B: 1 of each run's 3 prompts,
    the chunk holding `progress.py`.
  - The serialization bug went from absent or dropped under the 0.60
    confidence floor (0 of 3 active in A; dropped at 0.50 in 2) to an active
    error-severity finding in 3 of 3 B runs.
  - The history-window bug surfaced in 3 of 3 B runs and in none of A's,
    always dropped at 0.50 under the 0.60 floor: the block made the model see
    it, not assert it (Still open).
  - The toggle check fired in all 6 runs, on the same line.
  - Cost: 216 more input tokens per review (+2.8%). Output tokens overlap
    between the arms.
  - The parse retry never fired: 0 retries and 0 failed chunks in 6 runs,
    with no malformed reply seen.
  - GLM served every run. llm-ferry's log shows no fallback in the run
    window, and a probe after the runs reported 0 attempted fallbacks. No
    probe was taken before the runs; the log window stands in for it.
  - The write guard was active in all 6 runs.

## Still open — not part of this release

- **#22 part 1 is not built, and #22 stays open.** Its follow-up lookup, one
  bounded extra call that looks up the symbol a below-floor finding says it
  could not see, is deferred. It would be the first time a model's output
  chooses the next input, which crosses the project's single-shot,
  pre-gathered-context rule, so it needs a decision on that rule first.
- **The history-window bug stays under the floor.** With readers, #22's
  second bug surfaced in 3 of 3 live runs, always at confidence 0.50 under
  the 0.60 floor, so it was never posted. Nothing in 0.17.0 raises it.
- **Repository context's effect on findings is measured for readers only.**
  - The reader block has one single-factor measurement, at N=3 per arm on one
    fixture (Live checks).
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

0.17.0's known limitations, in full in its CHANGELOG section:

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
- **The toggle finding's posted severity varied live.** The check emits
  `warning`, but the live check recorded its finding at `error` in 2 of the
  3 reader runs. The pass that raised it was not traced.
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
  an earlier call.
- **prxref's defaults are too small for a thinking model.**
  `PRXREF_LLM_MAX_TOKENS` 4096 and `PRXREF_LLM_TIMEOUT` 45 lost chunks to a
  thinking model in 0.16.0's live checks (lesson 4 of the v0.16.0 handoff),
  and 0.17.0's live check ran at 32,768 tokens and 900 s. Neither default
  changed in 0.16.0 or 0.17.0; raise both for such a model. The recipe under
  "Measuring repository context" in `tests/evals/README.md` sets neither, so
  run verbatim against such a model it cuts the replies off.

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
  (`orchestrator._make_file_reader`), so they stay empty. The title comes
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

The v0.16.0 handoff's #20 bullet is **done** (Java and Kotlin chunk context),
and so is its #21 bullet (the parse retry).

| Item | Value |
|---|---|
| Released version | `0.17.0` (minor: Java and Kotlin chunk context, at every level; one new config key, `PRXREF_LLM_PARSE_RETRIES`, default `1`, which changes behaviour: a `{}` reply now costs a second call and can fail its unit, and `0` restores 0.16.0; one new run-record and `--format json` key, `parse_retries`, `null` at `0`; a new context block at `repo`, `### Code elsewhere that reads state this chunk writes`; one new always-on deterministic check, the pinned-off toggle; no new CLI flag; no existing config default changed) |
| Registration points | forges: the tuple in `forges/base.py` (`detect_forge`) and the `impls` dict in `config.py` (`make_forge`); repository listing: the optional `Forge.list_paths` in `forges/base.py`, on every adapter; repository-context entries: `repo_context.KINDS`, where an entry's kind picks the prompt block it renders in and the tuple's order ranks nothing, and `REASONS`, whose order is the budget's rank; LLM backends: `llm_backends.BACKENDS`; glyphs: `prxref.markers`; subcommands: `cli._build_parser`; prompt templates: `prompt_templates.TEMPLATE_NAMES` and `OPTIONAL_PLACEHOLDERS` |
| Version strings | `pyproject.toml`, `src/prxref/__init__.py`, and `uv.lock` |
| Test command | `uv run pytest` (dev tools are a `[dependency-groups]` group, not an extra) |
| Release assets | wheel **and** sdist attached by `release.yml`; PyPI by OIDC trusted publishing |
