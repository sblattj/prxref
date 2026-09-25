# HANDOFF — v0.16.0 shipped: repository context

**Repo:** `sblattj/prxref` (public) · **Released:** 2026-09-25 · **Supersedes** the
v0.15.0 handoff.

0.16.0 lets a chunk worker see code outside its own hunks (#17): definitions
from the pull request's other files and from files the diff never touches, and
excerpts of the API and database contracts a change points at.
`PRXREF_REPO_CONTEXT` turns it on at `diff` or `repo`. It is off by default,
and at `off` the prompts, posted comments, trace and logs are byte-identical to
0.15.0. The release also retries an empty model reply once. The user-facing
account is the `[0.16.0]` section of `CHANGELOG.md`. This file is for whoever
cuts the next release. The v0.15.0 handoff is in git history.

## What landed

- **#17 Repository context, as a module map.** The first five modules below
  do no I/O and import nothing from `prxref.forges`: they take a
  `read(path)` callable plus plain data.
  - `repo_unit.build_unit_context` is the place to start. It builds one
    chunk's `UnitContext` from the sources below, ranks the entries by
    `repo_context.REASONS` (`cross-chunk`, `contract`, `diff-file`, `import`,
    `path-convention`, `name-search`), admits them into
    `PRXREF_REPO_CONTEXT_MAX_CHARS`, and writes the `… N more context entries
    omitted` line. `repo_unit.MODES` is the vocabulary: `off`, `diff`, `repo`.
  - `repo_context`: `ContextEntry`, `find_definitions`, `language_of`
    (`chunk_context`'s map plus `.java`, with `_JAVA_DEF_RE` for type
    declarations), and the exclude floor, `EXCLUDE_FLOOR` and
    `exclude_predicate`.
  - `repo_crosschunk.diff_definitions`: definitions from the pull request's
    other files, and, for a type another chunk changes, up to
    `MAX_CHANGE_LINES` of its changed lines.
  - `repo_resolve.resolve_candidates`: files outside the diff, found through
    imports (Java, Python, TypeScript and JavaScript), Java's same-package
    path convention, and the name search over the listing.
  - `repo_contracts`: `contract_triggers` reads routes, operation ids, tables
    and schema names off the added lines; `select_contract_files` and
    `earlier_migrations` pick the files; `contract_excerpts` dispatches to the
    OpenAPI, JSON Schema, SQL and Liquibase excerpters.
  - `repo_reader.RepoReader` is the one reader a run shares, built by
    `forge_reader` or `repo_dir_reader`. It fetches each path and the listing
    at most once, single-flight across threads, and keeps the counters behind
    the record's `reads` and `read_cap_hit`.
  - `forges/repo_dir.RepoDir` reads and lists a `--repo-dir` checkout.
  - `forges/base.PathListing(paths, complete)` and the optional
    `Forge.list_paths(ref, *, sha)`, implemented by all five adapters.
    `forges/replay.ReplayForge.list_paths` delegates at the pinned head.
  - `orchestrator`: `_plan_repo_context` builds the run's reader, takes the
    listing and selects the contract files once, and logs `repo`'s one
    WARNING; `_routed_read` routes each read (lesson 3); each
    chunk worker calls `repo_unit.build_unit_context`; `_repo_context_record`
    and `_unit_row` build the record, `retry_dropped` included; and the
    `chunk context` and `repo_context ok` trace events.
  - `chunk_context.render_context_blocks` takes `extra_def_lines` and
    `contract_lines`, the second under the new `CONTRACT_HEADER`
    (`### Contract excerpts`). With both empty its output is unchanged.
  - `cli`: `--repo-dir` (`_open_repo_dir`, which exits 2 before any network
    call), the `repo_context` JSON key after `rule_counts`, and the `-v` line
    (`_repo_context_line`).
  - `eval_cases` and `evals`: a case's `repo_dir` field or `case-*/repo/`
    directory, and four more `evals.RUN_CONFIG_KEYS` (16 in all).
- **Entry points and caps.**
  - `orchestrate_review` takes `repo_context="off"`,
    `repo_context_max_chars=12000`, `context_contract_globs=()`,
    `context_exclude_globs=()` and `repo_dir=None`. `cli._run_review` passes
    the four config values by name, and `repo_dir` from `--repo-dir` or the
    eval case.
  - Reads: `repo_reader.MAX_CHUNK_READS` (16) and `MAX_RUN_READS` (200) cap
    the reads of paths outside the pull request; its own files are uncapped.
  - Listing: `forges/base.MAX_LISTING_PAGES` (20) pages; `RepoDir` lists at
    most 100,000 files (`forges/repo_dir._MAX_LISTED_FILES`); Bitbucket Cloud
    walks at `max_depth=64` (`forges/bitbucket._LISTING_MAX_DEPTH`).
  - Contracts: `repo_contracts.MAX_SPEC_FILES` (6), `MAX_EARLIER_MIGRATIONS`
    (4), `MAX_CONTRACT_LINES` (40) and `MAX_CONTRACT_CHARS` (2,000).
  - Definitions: `repo_resolve.MAX_NAME_SEARCH_PER_NAME` (3) and
    `repo_crosschunk.MAX_CHANGE_LINES` (12).
  - Files over 512 KiB read as missing, on every forge and in `--repo-dir`
    (`forges/repo_dir._MAX_FILE_CONTENT_BYTES`).
- **GitLab lists through GraphQL.** `forges/gitlab.py` `list_paths` walks
  `project.repository.tree(recursive: true).blobs`, 100 files a page
  (`_list_paths_graphql`). It falls back to the REST
  `repository/tree?recursive=true` walk (`_list_paths_rest`) only when the
  first GraphQL page is unusable, including the empty page GraphQL answers
  for a sha it cannot resolve. A later GraphQL page that fails returns the
  paths read so far with `complete=False`. Lesson 1 says why.
- **An empty model reply is retried once.** `reviewer._invoke_and_parse` asks
  again, with the same prompt and budget, when `_is_empty_reply` holds and the
  provider did not stop at the budget (`finish_reason` `length` or
  `max_tokens`). Chunks and the sweep share the path. `_fold_retry_usage` sums
  both calls' tokens and costs, and the unit's `model` is the second call's. A
  retry that raises fails the unit and keeps the first call's usage. The worst
  case is 4 `llm.invoke` calls for a chunk (this retry times the
  orchestrator's one timeout retry) and 2 for the sweep.
- **Config went from 63 to 67 keys.** The four new keys:
  - `PRXREF_REPO_CONTEXT` (`off`, `diff` or `repo`; default `off`)
  - `PRXREF_REPO_CONTEXT_MAX_CHARS` (default 12,000; must be above 0)
  - `PRXREF_CONTEXT_CONTRACT_GLOBS` (default the built-in set; a set value
    replaces it)
  - `PRXREF_CONTEXT_EXCLUDE_GLOBS` (default empty; adds to the always-on
    floor)

  The last three do nothing while `PRXREF_REPO_CONTEXT` is `off`, its
  default. No existing config default changed.

## What this release taught

Written down because each one cost real time.

1. **A live listing check found what the fakes could not.** GitLab's REST
   `repository/tree?recursive=true` returns every directory before any file,
   across the whole recursive walk. On gitlab-org/gitlab, all 2,100 entries
   read (the 20-page cap plus one probe page) were directories, so the capped
   walk listed 0 files. GraphQL's `tree.blobs` lists files only, and the same
   project now lists 2,000 files. A fake built from the documented response
   shape carries no ordering; record a real response past the page cap before
   trusting a paged walk.
2. **"Off is byte-identical" is proven against the previous release, never
   the tree.** `tests/fixtures/issue17/golden_off_prompts.json` was captured
   by `make_golden.py` under `uv run --no-project --with prxref==0.15.0`, from
   outside the repository. `TestOffMatchesTheReleased015` compares the tip's
   `off` prompts and reads with it byte for byte, over two design points and
   three keyword-argument variants.
   - `test_the_golden_is_a_0_15_0_oracle_that_exercises_the_old_blocks` pins
     the golden's provenance: `prxref_version` and `generated_from:
     installed distribution`.
   - `test_control_the_same_comparison_fails_for_repo` shows the comparison
     can fail.
   - A golden written from the tree would only compare the tree with itself.
     When a later release changes an `off` prompt on purpose, capture the
     golden again the same way, from the release the claim is now made
     against, and move the version the provenance test pins.
3. **Reads of the pull request's own files are uncapped.** With one per-chunk
   cap for every read, a chunk with many diff files spent its 16 reads before
   it reached its contract file. `orchestrator._routed_read` now sends diff
   paths to the uncapped `RepoReader.read`, and only other paths to the
   capped `chunk_reader`. `TestReadCapStarvation` in
   `tests/test_issue_17_acceptance.py` pins it, with a control that routes
   every read through the capped reader and loses the spec. It also removed a
   dependence on thread scheduling: which chunk reads a shared diff file first
   no longer changes any chunk's entries.
4. **A thinking model needs more than prxref's defaults.** GLM 5.3 Flash with
   thinking on, reached through a reasoning lane in llm-ferry, did not fit
   `PRXREF_LLM_MAX_TOKENS` 4096 and `PRXREF_LLM_TIMEOUT` 45.
   - At 4,096 tokens a smoke call stopped with `finish_reason=length`: one
     spent 4,094 tokens on reasoning and returned no answer, another spent
     3,902 and returned a cut-off one.
   - On the #17 eval fixture, 16,384 tokens and a 600 s timeout completed
     every call. The largest reply used 11,433 completion tokens.
   - On a larger pull request, 16,384 tokens was not enough: a chunk of
     11,161 input tokens ran out of room after 357 s. At 32,768 tokens and a
     900 s timeout the same chunk completed in 345 s, with 15,265 output
     tokens.
   - Single calls took 90 to 390 s.

   The timeout retry drops the repository-context blocks, so a timeout that
   is too short quietly removes the feature. One small probe, 4 calls a
   point, is an observation, not a measurement: `reasoning_effort` `low` and
   unset gave overlapping reasoning-token ranges (212 to 800 and 496 to
   1,054), and a request with thinking disabled was answered by a different
   model.
5. **`-v` summary lines are text output only.** With `--format json`,
   `cli._cmd_review` prints the JSON object and skips `_print_summary`, as
   0.15.0 already did for every `-v` line. A live check scripted against
   `--format json -v` found no `repo context:` line. Read the `repo_context`
   key instead, or check the line through `cli._repo_context_line`
   (`TestVerboseLine` in `tests/test_cli_repo_context.py`).
6. **Know what repository context cannot reach.**
   - The name search matches a file name in the same language (ignoring
     case, and also in snake case for Python), never file content. A type
     declared in a differently named file is not found (`TestNameSearch` in
     `tests/test_repo_context_resolve.py`).
   - Only chunk workers get repository context. The sweep prompt is the same
     at every level: `test_control_the_same_comparison_fails_for_repo` finds
     every worker prompt changed at `repo` and the sweep's unchanged.

   A check that looks for either finds nothing, by design.
7. **A new run-record or JSON key moves twelve test pins in five files.**
   0.15.0's count of eight in three files was already short: the #12 and #18
   tests pin the key order too.
   - `tests/test_orchestrator.py`: three `set(res) == {...}` literals and
     `RESULT_KEYS`.
   - `tests/test_run_record.py`: `RECORD_KEYS` and `NULL_WHEN_OFF`.
   - `tests/test_cli_output.py`: `JSON_KEYS` and `NEW_RECORD_KEYS`.
   - `tests/test_orchestrator_rule_cap.py`: `TestJsonOutput`'s next-key
     assertion, and `TestOffPathMatchesBase`'s key set and JSON key list.
   - `tests/test_cli_scoped_rules.py`: `TestJson`'s `sampling` offset.

   A keyword argument that the CLI builds rather than reads from config, as
   `repo_dir` is, must also be named in
   `test_run_review_passes_every_configured_orchestrate_kwarg`.

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
key. All 4 keys 0.16.0 added feed it this way. Current values: **67** keys,
**1** legacy alias, **68** accepted names.

## Release shape (follow this next time)

How 0.16.0 was built:

1. **One map and one decisions file first.** #17 got a read-only map of the
   code it would touch. A decisions file then settled every open question
   before any code was written: off by default, `diff` without a reader
   still adds its hunk-based entries, `list_paths` on all five adapters,
   `--repo-dir` and the eval field, the contract glob set, the 512 KiB
   ceiling and the 12,000-character budget. It also gave each new module one
   owner and fixed the shared interface names, so parallel tasks merged
   clean.
2. **Foundation.** One task landed all four config keys on every surface,
   off by default, before any feature work started.
3. **Pure modules first, wiring last.** Tasks ran in rounds of parallel
   agents, each agent in its own worktree against a pinned base commit. Each
   task was rated at most 5 of 10 for complexity and brought its own new
   test file. 20 tasks merged in seven rounds:
   - 6: the config keys, the definitions core, the contract excerpters, the
     `list_paths` Protocol with GitHub's listing, the `--repo-dir` reader,
     and the acceptance fixture repository
   - 5: the resolver, the cross-chunk definitions, the run reader, and the
     listings of the other four forges
   - 2: the contract triggers and file selection, and the eval case field
   - 1: the per-chunk unit context
   - 2: the orchestrator wiring, and the GitLab GraphQL listing that the
     listing check brought in
   - 3: the CLI and eval wiring, the acceptance tests through
     `orchestrate_review`, and the empty-reply retry
   - 1: the user documentation and the CHANGELOG section
4. **One integration gate per merge.** Each branch merged into `release/X.Y.Z`
   on its own. A merge stayed only if the full `uv run pytest` and
   `uv run ruff check src tests` passed on the merged tree. Over the 20
   merges the passing count rose from 6,797 at 0.15.0 to 7,675 and never
   fell; the documentation merge, the last, added no tests.
5. **Read-only live checks.** They ran against public repositories and pull
   requests, with a guard that blocked forge writes, and reviews used one
   model through one lane. Three ran: the listing on four live forges once
   the listing tasks merged, then, once the wiring merged, `prxref eval` on
   the #17 fixture and the bundled cases, and a review of one public pull
   request end to end. The listing check found the GitLab ordering of lesson
   1, which brought in the GraphQL walk before release, and GitLab was
   checked again after it.
6. **Release.** This commit bumps the version, dates the CHANGELOG and
   rewrites this file, after the documentation task wrote the CHANGELOG
   section.

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
7675 passed                                   uv run pytest -q
All checks passed!                            uv run ruff check src tests
0.16.0                                        uv run prxref --version
```

These counts come from the release branch, measured at the commit that last
updated this file.

### Live checks

- **`list_paths` on four live forges.** 19 of 19 checks passed, with no LLM
  call. Each listing matched a raw walk exactly: sblattj/prxref on GitHub
  (229 paths), gitlab-org/gitlab-test on GitLab (40, its 3 submodules
  dropped), atlassian/forge-bitbucket-related-prs on Bitbucket Cloud (20),
  and dnceng-public/public's dotnet-public-wiki on Azure DevOps (5). An
  all-zero sha gave `None` on all four, reads were pinned to the head, and
  torvalds/linux's truncated tree came back `complete=False` (67,498 of
  71,638 entries). Before the GraphQL walk, gitlab-org/gitlab listed 0 files
  (lesson 1). Bitbucket Server has no public instance to check.
- **GitLab after the GraphQL walk.** Without a token, gitlab-org/gitlab-test
  listed 40 paths, complete, in 0.4 s, and gitlab-org/gitlab listed 2,000
  paths in 57.4 s, stopped at the 20-page cap and marked incomplete. GitLab's
  GraphQL endpoint honours `PRIVATE-TOKEN`: an invalid token gets 401, and no
  header gets 200.
- **`prxref eval` on the #17 fixture and the bundled cases**, GLM 5.3 Flash
  through `domestic.flash`, which served every call (19 of 19 header probes,
  0 fallbacks). 9 runs made 28 LLM calls with no failed case, at
  `PRXREF_LLM_MAX_TOKENS=16384` and `PRXREF_LLM_TIMEOUT=600`; at prxref's
  default of 4,096 tokens a smoke call was cut off (lesson 4).
  - The fixture ran 3 times at `off` and 3 times at `repo`, reading its
    `repo_dir`. Within each level the prompts were byte-identical. Between
    the levels only the chunk prompt differed, by the added
    repository-context block (62 lines, `### Contract excerpts` among them).
  - The reader, the contract entries and the `off` null held in every run.
    Each `repo` record named the `repo-dir` reader, its one chunk carried
    both the `connectors.yaml` OpenAPI excerpt for the Java service and the
    earlier migration for the SQL file, and every `off` record held
    `repo_context: null`. The fixture's three files fit in one chunk at
    default chunking, and the cross-chunk link needs two chunks: a run with
    one file per chunk (3 chunks) confirmed it, with each contract entry in
    its own file's chunk.
  - An HTTP 201 status code that exists only in the injected OpenAPI excerpt
    appeared in the findings of all 3 `repo` runs and of none of the 3 `off`
    runs. The labels could not separate the levels: one matched in 0 of 3
    runs at `off` and 1 of 3 at `repo`, the other in none at either.
  - On the three bundled cases, `diff` added no entry (single-file diffs, no
    reader) and sent byte-identical prompts. Recall was 7 of 8 at both
    levels, with two labels flipping in opposite directions.
  - Each `run.json` recorded all 16 `RUN_CONFIG_KEYS`, the four new ones
    included.
- **End to end on sblattj/multi-auto-claude-sub#5** (9 files, TypeScript),
  GLM 5.3 Flash through `domestic.flash`, which served every call (8 of 8
  header probes, 0 fallbacks).
  - `off` gave `repo_context: null`.
  - `repo`, through the GitHub forge and through `--repo-dir`, built the same
    110 entries over 2 chunks, from a complete 64-path listing, with 22 reads
    and no cap hit. 26 definitions from outside the diff reached the prompts,
    25 through imports and 1 through the name search. 1 entry was left out
    under the 12,000-character budget, with its marker line.
  - It needed `PRXREF_LLM_MAX_TOKENS=32768` and `PRXREF_LLM_TIMEOUT=900`
    (lesson 4); at 16,384 an `off` run lost one of its two chunks.
  - Findings: 2 at `off`, 3 through the forge and 0 through `--repo-dir`. The
    last two runs sent identical prompts (27,466 input tokens each), so a
    single run's finding count cannot measure the feature's effect.
  - The forge run made 28 content reads for 22 paths: the double fetch in the
    CHANGELOG's known limitations. The repository has no contract files, so
    contract excerpts were not exercised live, and the empty-reply retry did
    not fire.

## Still open — not part of this release

- **#20 JVM chunk context.** `chunk_context._language` still gives `.java` and
  `.kt` no language, so the dependency-versions and same-file definitions
  blocks skip JVM files at every level. Repository context adds Java type
  declarations only (`repo_context.language_of`, `_JAVA_DEF_RE`). Methods,
  fields, constants, Kotlin, and Maven or Gradle versions are open.
- **#21 A reply that does not parse is not retried.** The reviewer retries
  only an empty reply (`reviewer._invoke_and_parse`, `_is_empty_reply`). A
  reply that fails to parse, parses to a non-object, or has no `findings`
  list still fails its unit.
- **Repository context's effect on findings is unmeasured.**
  - On the #17 fixture, at 3 runs a level, one label matched in 0 of 3 runs
    at `off` and 1 of 3 at `repo`, and the other in none. Sampling noise
    explains that as well as the context does. The defect the second label
    targets was raised as an active finding in 3 of 3 `off` runs and 1 of 3
    `repo` runs, never in the wording its predicate requires.
  - The clearest sign that the model reads the context: an HTTP 201 status
    code that exists only in the injected OpenAPI excerpt appeared in the
    findings of all 3 `repo` runs and of none of the `off` runs.
  - On sblattj/multi-auto-claude-sub#5, identical prompts gave 3 findings and
    0, so a single run's count cannot measure it.

  Measure with `prxref eval` over more runs a level before changing the
  default from `off`.

The known limitations, in full in the CHANGELOG:

- **Read caps cut the lowest-ranked sources first.** Contract files are read
  before import, path-convention and name-search files, so those definitions
  go first once a chunk has made 16 reads or the run 200. Which chunk meets
  the run cap first depends on thread scheduling.
- **No content search.** Outside the pull request, a definition is found
  only through an import, the Java path convention, or a same-language file
  named after it.
- **Files over 512 KiB read as missing**, so a large OpenAPI spec gives no
  excerpt.
- **A large repository is listed only in part.** A paged listing stops at 20
  pages and GitHub truncates a very large tree, so the name search and the
  contract globs see only the files listed. A large GitLab project's listing
  takes about a minute, once per run, at `repo` only.
- **The Bitbucket Server listing is untested live.** It is built from the
  Data Center REST documentation and tested against fakes of that shape.
  Whether it returns submodules is unverified.
- **A chunk that times out loses its repository context**, because the
  timeout retry drops the definitions and contract blocks (lesson 4).
- **A pull request's file can be fetched twice.** The 0.15.0 dependency and
  same-file definition blocks keep their own reader.

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
- **An empty-reply retry followed by a timeout retry loses tokens.** When the
  retry of an empty reply times out, `_run_worker`'s timeout retry replaces
  the chunk's result, so the billed empty call's tokens drop out of the run
  totals.
- **The retry's cost estimate uses the second call's model.** When either
  call reports no cost, the unit's cost is unknown, and `costs.run_cost`
  estimates it from the summed tokens at the rate of the unit's `model`,
  the second call's, even when another model in the fallback chain answered
  the first call.
- **prxref's defaults are too small for a thinking model.**
  `PRXREF_LLM_MAX_TOKENS` 4096 and `PRXREF_LLM_TIMEOUT` 45 lost chunks to a
  thinking model in the live checks (lesson 4). Neither default changed in
  0.16.0; raise both for such a model. The recipe under "Measuring repository
  context" in `tests/evals/README.md` sets neither, so run verbatim against
  such a model it cuts the replies off.

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
  but the dependency-versions and same-file definitions blocks still read
  only through the forge (`orchestrator._make_file_reader`), so they stay
  empty. The title comes from the patch mail or the file name. The
  description comes from the mail, `--description-file` or
  `--no-description`.
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
- **Missing definitions at `off`.** With repository context off,
  `chunk_context.referenced_definitions` still looks only in the same file,
  and only for JavaScript, TypeScript and Python. `diff` and `repo`
  add other files and Java types; the rest of the JVM gap is #20.
- **The history read is untraced.** The replay's history read is outside the
  trace, and only the `replay` stamp records its outcome.
- **The sweep's example is always in force.** `orchestrator._example_titles`
  always reads the systemic template. So the sweep's example title is in force
  even on a run whose sweep never runs.

The v0.15.0 handoff's #17 bullet is **done**, and so is its empty-reply
bullet (the empty-reply retry).

| Item | Value |
|---|---|
| Released version | `0.16.0` (minor: repository context behind four new config keys, all off by default; one new CLI flag, `--repo-dir`; one new run-record and `--format json` key, `repo_context`, `null` when off; an empty model reply is retried once; no existing config default changed) |
| Registration points | forges: the tuple in `forges/base.py` (`detect_forge`) and the `impls` dict in `config.py` (`make_forge`); repository listing: the optional `Forge.list_paths` in `forges/base.py`, on every adapter; LLM backends: `llm_backends.BACKENDS`; glyphs: `prxref.markers`; subcommands: `cli._build_parser`; prompt templates: `prompt_templates.TEMPLATE_NAMES` and `OPTIONAL_PLACEHOLDERS` |
| Version strings | `pyproject.toml`, `src/prxref/__init__.py`, and `uv.lock` |
| Test command | `uv run pytest` (dev tools are a `[dependency-groups]` group, not an extra) |
| Release assets | wheel **and** sdist attached by `release.yml`; PyPI by OIDC trusted publishing |
