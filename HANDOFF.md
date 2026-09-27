# HANDOFF — v0.18.0 shipped: opt-in context follow-up (#22 part 1)

**Repo:** `sblattj/prxref` (public) · **Released:** 2026-09-27 · **Supersedes** the
v0.17.0 handoff.

0.18.0 adds the context follow-up (#22, part 1). A chunk worker that is not
shown a symbol's definition is told to ask about it at confidence 0.5 or
below, so the default floor of 0.6 drops the question however real the bug
is. With `PRXREF_CONTEXT_FOLLOWUP=on` at `PRXREF_REPO_CONTEXT=repo`, prxref
looks up the definitions of the symbols such a question names and sends the
chunk once more with them appended; a question the second reply confirms
posts, and one it does not is dropped with its own reason. The follow-up is
off by default, and at `off` every prompt and LLM call is the one 0.17.0
makes; the run record and `--format json` only gain `context_followup`,
`null`. The user-facing account is the `[0.18.0]` section of
`CHANGELOG.md`. This file is for whoever cuts the next release. The v0.17.0
handoff is in git history.

## What landed

- **The follow-up, as a module map.** Three new modules, stdlib only, that
  read files only through the `read` callable they are given.
  - `repo_followup`, the lookup half. `question_indices` picks a chunk's
    questions: its findings below the floor, most confident first.
    `name_tiers` takes the names each one asks about, and `lookup_names` picks
    the chunk's names from them. `lookup_excerpts` reads their definitions at
    the pull request head, cut by `definition_body`, and
    `render_followup_block` renders the prompt block under `FOLLOWUP_HEADER`
    (`### Definitions referenced by this chunk, looked up for its open
    questions`) and `FOLLOWUP_NOTE`.
  - `followup_merge`, the confirm rule. `confirms` decides whether one
    finding of the second reply confirms one question; `merge_followup` folds
    the second reply into the first and returns a `MergeOutcome` with the
    `confirmed`, `unconfirmed` and `discarded` counts. `UNCONFIRMED_PREFIX`
    starts the new drop reason.
  - `followup`, the per-chunk driver. `run_chunk_followup` runs the steps in
    order, makes at most one `invoke`, never mutates the first result and
    never raises. `skipped_row`, `ROW_KEYS` and `SKIP_REASONS` define the
    chunk's run-record row.

  How they reach a review: `orchestrate_review` takes
  `context_followup="off"|"on"` (`FOLLOWUP_MODES`), which `cli._run_review`
  passes from the config, so `prxref review` and the webhook server get it.
  `orchestrator._run_worker` calls `_chunk_followup` after a chunk's first
  attempt, and the re-run is `_invoke_chunk` with the block as
  `extra_blocks`, joined after every other context block by a blank line.
- **The gate.** The follow-up is active only when the key is `on`, the level
  is `repo` and the repository-context plan has a reader (the forge or
  `--repo-dir`). The check sits in `orchestrate_review` right after
  `_plan_repo_context`, and the confidence floor is resolved only when it
  passes. Otherwise the run logs `FOLLOWUP_INACTIVE_WARNING` once,
  `PRXREF_CONTEXT_FOLLOWUP=on needs PRXREF_REPO_CONTEXT=repo and a repository
  reader; the context follow-up is off for this run`, records `active: false`
  and makes no extra call. That is not a configuration error; a value other
  than `off` or `on` is, and exits 2.
- **The trigger.** A chunk gets a follow-up when its first result has no
  error, it did not take the timeout retry (`_run_worker` records
  `skipped_row("timeout-retry")` instead of calling the driver), and at least
  one finding without a `drop_reason` is below the floor. It is then skipped,
  with the reason in the row, when no name is left (`no-names`) or no excerpt
  is admitted (`no-excerpts`); with no excerpt there is no call. The sweep
  never gets one.
- **Names.** `name_tiers` reads the backtick spans of a question's title and
  body first, then its plain text for names shaped like code: both halves of
  a dotted access or call (`table.recent(`), a bare call (`name(`), a
  snake_case or UPPER_SNAKE identifier, and a type-like word. A source file
  name (`history.py`) and any part of a path give nothing. Every name has at
  least 3 characters and is not a receiver, a literal, a Python, JS/TS, Java
  or Kotlin keyword, a capitalised Python builtin (`TypeError`), or a bare
  lowercase one (`len(`); after a dot a lowercase builtin name is kept as a
  repository attribute. Type-like names rank first, then names after a dot,
  then the rest, and plain-text names come after every backtick name.
  `lookup_names` drops the names the pull request defines
  (`diff_defined_names`) and gives every question one slot before any
  question gets a second: first each question's best name, most confident
  question first, then the remaining slots tier by tier.
- **The one call.** The re-run keeps the first call's token budget, context
  lines and prompt context, and gets `parse_retries=0` (an empty reply is
  still retried once, as at 0.17.0's `N = 0`) and no timeout retry, so it
  adds at most 2 calls to a chunk. Nothing it returns triggers another
  follow-up.
- **Confirmation.** `confirms`: the second-reply finding is at or above the
  floor, in the same file, and within `quality.DEFAULT_LINE_TOLERANCE` (5)
  lines of the question (both lines positive), or has the same normalized
  title, or mentions a name resolved for the question at a word boundary.
  The names resolved for a question are its own `finding_names` that an
  admitted excerpt defines or covers. Questions settle in index order, and
  each takes the most confident unused confirming finding, which replaces it
  and then runs every quality pass. A question with resolved names and no
  confirmation gets `not confirmed by context follow-up (confidence C below
  floor F)`, which `apply_quality_gate` keeps instead of its own reason. A
  question with no resolved name is left to the floor.
- **Discards.** Every other finding of the second reply is dropped, never
  posted: `discarded = len(rerun) - confirmed`, counted per chunk and per run.
- **Failure.** A re-run that errors, times out, is truncated or raises keeps
  the first findings, logs `context follow-up failed (keeping the first
  review)` at WARNING and fills the row's `error`. `_chunk_followup` handles
  a failure outside the driver the same way.
- **The record.** The run record and `--format json` carry
  `context_followup` right after `parse_retries` (`cli._build_json_result`):
  `null` at `off`; at `on`, `{active, calls, confirmed, unconfirmed,
  discarded, input_tokens, output_tokens, chunks}`
  (`orchestrator._followup_record`). `chunks` is `null` when not active, and
  otherwise holds one `ROW_KEYS` row per chunk (`null` for a chunk whose
  worker left none): `questions`, `names`, each excerpt's `path`, `line`,
  `symbol`, `source` and `chars` (never its text), `called`, `error`, the
  three counts, the tokens and `skipped`. A run whose follow-up is on but
  inactive, or that exits before its workers finish, records
  `active: false`, zero counts and `chunks: null`.
  `evals.RUN_CONFIG_KEYS` gains `context_followup` before `repo_context`, 18
  keys in all.
- **Traces.** Per chunk, `followup` events `start`, `ok`, `fail`, and `skip`
  when the chunk had at least one question; one run-level `context_followup
  ok` event with the totals, emitted only when the follow-up is active. With
  `PRXREF_TRACE_DIR`, the re-run's files are `chunk<i>.followup.system.md`,
  `.user.md`, `.response.json` and `.meta.json`, beside the chunk's own.
- **Cost.** `followup._fold_usage` adds the re-run's tokens and time to the
  chunk's, makes the chunk's `model` the re-run's when it answered, and folds
  the costs through `costs.combine_reported`, so the cost is unknown when
  either is. `parse_retries` and `first_error` stay the first call's. A
  re-run that returned an error still counts its billed tokens.
- **Caps**, each read from its constant in `repo_followup` when the function
  runs, so a test can patch it: `MAX_FOLLOWUP_NAMES` (3) names per chunk,
  `MAX_FOLLOWUP_READS` (8) reads, cached ones included, `MAX_FOLLOWUP_EXCERPTS`
  (4) excerpts, `MAX_FOLLOWUP_EXCERPT_LINES` (30) lines per excerpt, ending
  `… N more lines` when cut, and `MAX_FOLLOWUP_CHARS` (4,000) characters.
  The reads go through a fresh `_routed_read`, so the follow-up has its own
  per-chunk read cap; a file the pull request changes and an excluded path
  are never read. There is no config key for any cap.
- **Config went from 68 to 69 keys.** The one new key is
  `PRXREF_CONTEXT_FOLLOWUP` (choice, `off` or `on`, default `off`). No
  existing config default changed.

## What this release taught

Written down because each one cost real time.

1. **A feature can pass every mocked acceptance test and do nothing live.**
   The first live run made 0 follow-up calls in 3 runs with the follow-up
   on: every question was skipped `no-names`, because the model wrote the
   identifiers it asked about (`model_history`, `table.recent(`,
   `session_id`) without backticks, and name extraction read backtick spans
   only. Every acceptance reply had been written by people, who use
   backticks. `name_tiers` now also reads plain-text code shapes, and
   `TestPlainTextNames::test_a_question_without_backticks_is_confirmed` in
   `tests/test_issue_22_followup_acceptance.py` pins a reply with no
   backticks at all. Write at least one fixture reply the way the model
   under test writes, not the way the author would.
2. **A shared cap needs a fairness rule.** In the second live run the
   follow-up fired and confirmed a real serialization finding the first
   reply had left under the floor, but a second question, the history-window
   one, got no lookup: `lookup_names` merged tier by tier across questions,
   so the first question's names filled the 3 slots, one of them
   with the builtin exception name `TypeError`. Now every question gets one
   slot before any gets two, and Python builtin classes and bare builtins are
   skipped. `test_every_question_gets_a_slot_before_any_gets_two` and
   `test_a_second_question_gets_a_name_and_builtins_get_none` in
   `tests/test_repo_followup.py`, and
   `TestPlainTextNames::test_a_second_question_still_gets_a_name`, pin it.
   In the third live run the history question's names reached the lookup
   both times it appeared.
3. **A per-chunk call ceiling that looks like it rises does not.** The
   follow-up adds at most 2 calls, but only to a chunk that took no timeout
   retry, so the ceiling stays `2 * (1 + max(N, 1))`, 4 at the default: a
   follow-up chunk makes at most `3 + max(N, 1)`, and `1 + max(N, 1)` is at
   least 2. `test_a_timeout_retried_chunk_is_skipped_without_the_driver` in
   `tests/test_orchestrator_followup.py` pins the half that keeps it there.
4. **Off-identity was pinned against the released tree.**
   `tests/test_context_followup_off_identity.py` records, as literals
   captured from the `v0.17.0` commit, the request count and the sha256 of
   every request's `messages` on #22's fixture at `PRXREF_REPO_CONTEXT=repo`,
   with a sub-floor question in every chunk reply, exactly what the
   follow-up keys on. The key unset and `off` must both match those literals
   and give the same `--format json` payload, with `context_followup` null.
   The golden pins prompts and calls, not forge reads. That `off` never
   resolves the floor and never calls the driver, the only path to a
   follow-up read, is pinned separately, by
   `test_off_never_resolves_the_floor_or_calls_the_driver` in
   `tests/test_orchestrator_followup.py`.
5. **The mechanism works live; the model still did not assert the second
   bug.** In the third live run the history-window question appeared in 2 of
   3 runs with the follow-up on, and both times its definitions were looked
   up and sent, yet GLM 5.3 Flash did not re-assert it at or above the
   floor, so both landed as "not confirmed". That is a model judgment with
   the definition in view, not a starved pipeline. The named confound: a
   confirmation mixes the new excerpt with a second sample of the same
   chunk, so N=3 cannot separate "the excerpt helped" from "resampling
   helped" (Live checks).

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
key. The one key 0.18.0 added, `context_followup`, feeds it this way, and
`prxref eval run` records it in `run.json` (`evals.RUN_CONFIG_KEYS`). Current
values: **69** keys, **1** legacy alias, **70** accepted names.

## Release shape (follow this next time)

How 0.18.0 was built:

1. **One design pass first.** Before any code, a read-only design settled
   every open question: the follow-up is opt-in with a choice key, active
   only at `repo` with a reader; one call per chunk with no parse retry and
   no timeout retry, never for the sweep; names from structure, never from
   phrase lists; whole definitions under fixed caps with no config key; the
   confirm rule and the one new drop reason; an always-present record key,
   `null` when off, following the `parse_retries` precedent; and an
   off-identity golden taken from the released tree. It also gave each new
   module one owner and wrote down the rewording of the single-shot rule in
   `CLAUDE.md` and `docs/llm.md`.
2. **Pure modules first, wiring last.** Tasks ran in rounds of parallel
   agents, each in its own worktree against a pinned base commit, and each
   code task brought its own tests. 10 tasks merged:
   - 4: the config key, the lookup module, the confirm rule, and the
     off-identity golden
   - 2: the per-chunk driver, and the user documentation with the CHANGELOG
     section
   - 1: the orchestrator and CLI wiring with the record key
   - 1: the #22 acceptance tests over the issue's own fixture
   - 2 fixes, each sent back by a live check: plain-text names (lesson 1),
     then slot fairness with the builtin filter (lesson 2)
3. **One integration gate per merge.** Each branch merged into
   `release/X.Y.Z` on its own, and a merge stayed only if the full
   `uv run pytest` and `uv run ruff check src tests` passed on the merged
   tree. The wiring task's own branch had one red test, the README key list
   for `--format json`, which only the documentation task could fix; the
   documentation merged first, so the wiring merged green. Over the 10
   merges the passing count rose from 8,703 at 0.17.0 through 8,729, 8,751,
   8,781, 8,797, 8,837, 8,848 and 8,856 to 8,865, and never fell; the
   documentation merge added no tests.
4. **Three read-only live checks**, once the wiring merged: the
   follow-up off against on, on #22's fixture, through one model and one
   lane. The first two each sent a fix back before release (Live checks).
5. **Release.** This commit bumps the version, dates the CHANGELOG, states
   the live result in its intro, corrects one README sentence about which
   names are looked up, and rewrites this file.

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
8865 passed                                   uv run pytest -q
All checks passed!                            uv run ruff check src tests
0.18.0                                        uv run prxref --version
```

These counts come from the release branch, measured at the commit that last
updated this file.

### Live checks

- **Context follow-up on issue #22's own fixture**, GLM 5.3 Flash through
  llm-ferry (`domestic.flash`), `prxref review --no-post` at
  `PRXREF_REPO_CONTEXT=repo`. One factor varied: arm A had
  `PRXREF_CONTEXT_FOLLOWUP=off`, arm B `on`. Each check ran A B A B A B,
  interleaved, N=3 per arm. Three checks ran; the first two each sent a fix
  back. In every check GLM served with 0 fallbacks in a probe before and a
  probe after the runs.
  - **Before the plain-text names fix:** B made 0 follow-up calls in 3 runs.
    Each run with a sub-floor question skipped it `no-names`, because the
    model wrote the identifiers without backticks (lesson 1).
  - **After it:** the follow-up fired in 1 of 3 B runs, 1 call, and
    confirmed the serialization finding at `progress.py:47` (error,
    confidence 0.9), for 2,937 more input and 9,687 more output tokens. The
    history-window question (`README.md:7`, 0.50) got no lookup, because the
    serialization question's names filled the 3 slots (lesson 2). The other
    B runs had no sub-floor question, and so no call. Verdicts: mechanism
    PASS; history-window bug FAIL, starved of slots.
  - **After slot fairness (the released code):** the history-window
    question appeared in 2 of 3 B runs (`progress.py:40` and `README.md:6`,
    both 0.50). Both times its names reached the lookup and its definitions
    were sent (`assistant/history.py:9` `model_history` and
    `assistant/messages.py:19` `recent` in one run; `recent` beside
    `StateStore` and `Frame` in the other). In neither did GLM re-assert it
    at or above the floor, so both were dropped as `not confirmed by context
    follow-up (confidence 0.50 below floor 0.60)`. One re-run confirmed a
    serialization-side finding instead (`progress.py:31`, error, 0.85).
    Verdicts: mechanism PASS; history-window bug asserted by the model FAIL
    on GLM 5.3 Flash, 0 of 2; off arm unchanged.
  - Cost when it fired: 2,903 and 3,027 more input tokens, about 36% of this
    8,060-token review, plus the second reply's output tokens.
  - The serialization bug was active in all 6 runs of the last check. In
    the second check A missed it in 2 of 3 runs (Approved); N=3 cannot
    separate an arm effect on it from GLM's variance, so none is claimed.
  - The toggle check fired in all 6 runs of each check. The last check had
    0 parse retries and 0 failed chunks.
  - Named confound: a confirmation is a second sample of the chunk plus the
    excerpt, so these runs cannot separate the excerpt's effect from
    resampling.

## Still open — not part of this release

- **The history-window bug is still not asserted.** With the follow-up on,
  #22's second bug surfaced in 2 of 3 live runs, and both times its
  definitions were looked up and sent, but GLM 5.3 Flash did not re-assert
  it at or above the 0.60 floor, so it was dropped as "not confirmed". On
  that model, nothing in 0.18.0 posts it. A stronger model, or more runs, is
  the next measurement (Live checks).
- **The follow-up's effect is one single-factor measurement.** Three live
  checks at N=3 per arm on one fixture, through one model. A confirmation is
  a second sample of the chunk plus the excerpt, and N=3 cannot separate the
  two (lesson 5). Measure with `prxref eval` over more runs before changing
  the default from `off`.
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
  changed in 0.16.0, 0.17.0 or 0.18.0; raise both for such a model. The
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

The v0.17.0 handoff's #22 part 1 bullet is **done**: the follow-up lookup is
built, opt-in, and the single-shot rule in `CLAUDE.md` and `docs/llm.md` now
names it as its one bounded exception. The history-window bug it was meant to
raise is still not posted on the model measured (Still open).

| Item | Value |
|---|---|
| Released version | `0.18.0` (minor: the opt-in context follow-up, #22 part 1; one new config key, `PRXREF_CONTEXT_FOLLOWUP`, choice `off` or `on`, default `off`, no behaviour change at the defaults; one new run-record and `--format json` key, `context_followup`, `null` when off; three new modules, `repo_followup`, `followup_merge` and `followup`; one new drop reason, `not confirmed by context follow-up`, only when on; no new CLI flag; no existing config default changed) |
| Registration points | forges: the tuple in `forges/base.py` (`detect_forge`) and the `impls` dict in `config.py` (`make_forge`); repository listing: the optional `Forge.list_paths` in `forges/base.py`, on every adapter; repository-context entries: `repo_context.KINDS`, where an entry's kind picks the prompt block it renders in and the tuple's order ranks nothing, and `REASONS`, whose order is the budget's rank; LLM backends: `llm_backends.BACKENDS`; glyphs: `prxref.markers`; subcommands: `cli._build_parser`; prompt templates: `prompt_templates.TEMPLATE_NAMES` and `OPTIONAL_PLACEHOLDERS` |
| Version strings | `pyproject.toml`, `src/prxref/__init__.py`, and `uv.lock` |
| Test command | `uv run pytest` (dev tools are a `[dependency-groups]` group, not an extra) |
| Release assets | wheel **and** sdist attached by `release.yml`; PyPI by OIDC trusted publishing |
