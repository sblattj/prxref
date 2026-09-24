# HANDOFF — v0.15.0 shipped: the tuning release

**Repo:** `sblattj/prxref` (public) · **Released:** 2026-09-24 · **Supersedes** the
v0.14.0 handoff.

0.15.0 gives a team the knobs to tune a review, and a harness to measure what
the tuning did. The knobs are prompt template overrides, path-scoped review
rules, finding grouping with per-severity caps, a per-rule cap that a review
rules file turns on, and an opt-in tier for reworded duplicates. The harness is
`prxref eval run|score|compare`. GitHub pull requests past the diff endpoint's
size limit are now reviewed, and a replay pins the PR's title and description to
the time it replays. The user-facing account is the `[0.15.0]` section of
`CHANGELOG.md`. This file is for whoever cuts the next release. The v0.14.0
handoff is in git history.

## What landed

- **#10 Reworded duplicates.** `quality.apply_sweep_dedup(..., similarity=)`
  has a second tier that runs after the exact one. It treats two active
  findings in the same file on the same line as duplicates when
  `quality.titles_similar` holds. That test is a Jaccard index over title
  tokens (`quality.title_similarity`). `PRXREF_DEDUP_SIMILARITY` sets the
  threshold, and leaving it unset skips the tier. A chunk copy always outlives
  a sweep copy, and the tier never lowers a review's worst severity.
- **#11 Prompt template overrides.** `prompt_templates.load_prompt_templates`
  reads `worker.md`, `systemic.md` and `summary.md` from `--prompts-dir` /
  `PRXREF_PROMPTS_DIR`. A template that is not there falls back to the packaged
  one. An override must carry every placeholder the packaged template has after
  its context marker, except those in `OPTIONAL_PLACEHOLDERS`; a missing one
  exits 2. `prxref prompts export` (`cli._cmd_prompts_export` →
  `export_prompt_templates`) writes the packaged templates out as a starting
  point. The run record's `prompt_templates` lists each override.
- **#12 Path-scoped rules.** `--scoped-rules` / `PRXREF_SCOPED_RULES` name rules
  files whose `applies_to:` front-matter globs pick the chunks each one reaches.
  They add to `PRXREF_REVIEW_RULES` and never replace it. Start at
  `rules.load_scoped_rules`, `rules.match_globs` (where `**/` also matches zero
  directories) and `ScopedRules.select` / `unit_block`. `unit_block` fits the
  rules into `PRXREF_SCOPED_RULES_MAX_CHARS`. The orchestrator builds one block
  per chunk in `_scoped_unit_blocks`, and the sweep gets the union.
- **#13 Grouping and per-severity caps.** With `PRXREF_GROUP_FINDINGS` on, the
  worker prompt asks each finding for a `rule` (`triage.normalize_rule`,
  `orchestrator._enforce_rule`). `quality.apply_rule_grouping` then folds the
  chunk findings that break one rule in one file into one representative. Its
  body ends `Also at:` and its `locations` lists the other lines.
  `--format json` emits `rule` and `locations` (`cli._finding_json`), with
  `locations` null on an ungrouped finding unless the per-rule cap (#18)
  folded others into it. `PRXREF_MAX_WARNING_FINDINGS` and
  `PRXREF_MAX_OUTOFSCOPE_FINDINGS` cap those two severities in
  `quality.apply_quality_gate`, ranked by `quality.finding_rank_key`, and every
  cap counts groups.
- **#14 Eval harness.** `prxref eval run|score|compare` goes from
  `cli._cmd_eval` to `evals.eval_run`, `eval_score` and `eval_compare`. `eval`
  adds no environment variable, since every setting is a flag, and it never
  posts. `eval_cases.load_cases` reads a case: a diff file or a pinned `pr_url`
  range, plus its expected labels. `eval_metrics` grades a label that has a
  `must_match` predicate deterministically. Every other label goes to an LLM
  judge, whose code is in `judge.py` and `eval_judge.py` and whose prompt is
  `prompts/judge.md`. `compare` also warns when two runs' replay description
  stamps differ.
- **#15 GitHub PRs past the diff limit.** In `forges/github.py`, `get_diff`
  handles a 406 whose error code is `too_large` by calling
  `_get_diff_past_the_limit`. That reads `get_pr` and then
  `get_compare_diff(base.sha...head.sha)`. The compare diff is accepted only
  when `_compare_mismatch` finds its file count and its `+`/`-` line sums equal
  to the PR's `changed_files`, `additions` and `deletions`. On a mismatch or any
  HTTP or transport failure, it logs one WARNING and falls back to
  `_get_diff_from_files`. That pages `/pulls/{number}/files` and renders the
  diff through `forges/_diff_render.render_diff_entries`. The fallback fails
  closed: a listing shorter than `changed_files`, or totals that disagree with
  the PR's (`_refuse_short_totals`), end the run as `Error`, never as a partial
  review.
- **GitHub file content.** Start at `get_file_content` in `forges/github.py`.
  `_is_json_envelope` judges the response by its media type: a type in
  `_RAW_MEDIA_TYPES` is the file, and `application/json` or any other `+json`
  type is an envelope that reads as no content. `orchestrator._make_file_reader`
  wraps the adapter's reader in a per-run cache and feeds it to the chunk
  context blocks (dependency versions and symbol definitions) and to
  `quality.apply_manifest_claim_check`. The bug itself is under "Fixes found on
  the way" below.
- **#16 Replay pins the title and description.** A `--pr-url` replay goes
  through `cli._replay_forge` → `_resolve_description`. That reads the forge's
  `get_pr_history` once: GitHub over GraphQL, Bitbucket Cloud over `/activity`.
  `forges/replay.choose_cutoff` then picks the cutoff: `--as-of`, else the first
  human review, else the head commit's date. `ReplayForge` shows the title and
  description in force at the cutoff (`pin_pr_metadata`). `--description-file`
  and `--no-description` fix the description instead. Every other outcome
  replays the live text and logs a WARNING that says why. The run's `replay`
  stamp records `description` (`pinned`, `live`, `file` or `none`) with the
  cutoff and its source.
- **#18 Per-rule cap.** Start at `quality.apply_rule_cap`. `orchestrate_review`
  calls it through `_cap_rules`, after the grouping pass and before
  `apply_quality_gate`, only when `rule_cap_active` holds:
  `max_findings_per_rule` (`PRXREF_MAX_FINDINGS_PER_RULE`, default 2) is above 0
  and `rules` or `scoped_rules` is not `None`. The same flag turns on #13's rule
  request through `rule_active`, so a rules-file run asks every unit for a
  `rule` with grouping off. The pass keys on the rule, or the normalized title,
  across files, keeps the first `cap` ranked by severity and then
  `finding_rank_key`, and folds the rest onto the first one's `locations` and
  `Also at:` paragraph. `quality.rule_cap_counts` builds the run record's
  `rule_counts`.
- **Fixes found on the way.**
  - `quality.apply_example_echo_check` drops a finding whose title echoes a
    prompt template's example finding. It is the first pass that drops
    anything.
  - `worker.md` lost a false sentence claiming the input "stays under roughly
    30k tokens".
  - The GitHub adapter's `get_file_content` took GitHub's raw media type,
    `application/vnd.github.raw+json`, for a JSON envelope and dropped every
    file it read, so GitHub reviews got no full-file context. Earlier releases
    are affected too. `_is_json_envelope` now decides by the media type.
- **Config went from 55 to 63 keys.** The eight new keys are:
  - `PRXREF_DEDUP_SIMILARITY` (#10)
  - `PRXREF_PROMPTS_DIR` (#11)
  - `PRXREF_SCOPED_RULES` and `PRXREF_SCOPED_RULES_MAX_CHARS` (#12; the second
    defaults to 24,000)
  - `PRXREF_GROUP_FINDINGS`, `PRXREF_MAX_WARNING_FINDINGS` and
    `PRXREF_MAX_OUTOFSCOPE_FINDINGS` (#13)
  - `PRXREF_MAX_FINDINGS_PER_RULE` (#18; defaults to 2, and applies only while
    a review rules file is loaded)

  The first seven are off or unset by default. The per-rule cap is on by
  default whenever a review rules file is loaded. No existing config default
  changed.

## What this release taught

Written down because each one cost real time.

1. **A new run-record or JSON key moves eight test pins in three files.**
   - `tests/test_orchestrator.py`: three `set(res) == {...}` literals and
     `RESULT_KEYS`.
   - `tests/test_run_record.py`: `RECORD_KEYS` and `NULL_WHEN_OFF`.
   - `tests/test_cli_output.py`: `JSON_KEYS` and `NEW_RECORD_KEYS`.

   All eight spell out the full key set, and the key order is pinned too. A new
   exit from `orchestrate_review` also moves `TestOneChokePoint`'s count.
2. **Trace-event order is nondeterministic.** Chunk workers run on a
   `ThreadPoolExecutor` capped at `max_workers`. A test that asserts trace
   events must compare them as a multiset, or pin `max_workers=1`.
3. **A new prompt slot belongs in `prompt_templates.OPTIONAL_PLACEHOLDERS`.**
   An override must carry every other placeholder the packaged template has. So
   a slot left out of that set becomes required, and every existing override
   that lacks it stops loading. A new slot also needs its `replace` line in
   `tests/test_prompt_context.py`'s `_old_user`.
4. **The hub files take every issue's kwargs, so merge them one at a time.**
   `orchestrator.py` was touched by six issues and `cli.py` by seven. Merging
   one branch at a time kept them green. Each merge got a full gate, and each
   kwarg-order conflict was resolved as the union of both sides. The order is
   pinned by tests such as
   `test_both_kwargs_are_keyword_only_and_follow_prompts` and the record-key
   order tests in `tests/test_cli_prompts_dir.py` and
   `tests/test_cli_scoped_rules.py`.
5. **With its feature off, a new key is present and null, and the output stays
   byte-identical.** That was decided before any feature work, and every 0.15
   key keeps to it. The one deliberate exception is removing `worker.md`'s
   false "stays under roughly 30k tokens" sentence. That moved every worker
   user-prompt hash, including on runs with every feature off. It shows in the
   trace, in `--trace-dir`, and in the golden tests of
   `tests/test_rule_prompt_slot.py` and `tests/test_orchestrator_grouping.py`.
   The system, sweep and summary-only hashes did not move. A run with a review
   rules file moved further: the per-rule cap (#18) is on there by default, and
   its rule request changes both prompt halves of every chunk and of the sweep,
   which `PRXREF_MAX_FINDINGS_PER_RULE=0` undoes. Keep this in mind when
   comparing hashes across 0.14 and 0.15 runs.
6. **Read a packaged template through `prompt_templates.packaged_text`.** The
   stub `_contract_load_prompt` in `tests/test_orchestrator.py` asserts that the
   orchestrator asks `reviewer.load_prompt` for the summary template only.
   Route a new orchestrator read through `load_prompt` instead, and 248 tests
   fail with a misleading `'worker.md' == 'summary'`.
7. **Mock the headers the server really sends.** The GitHub file-content bug
   went unnoticed because the tests' success mocks answered
   `text/plain`. github.com answers `application/vnd.github.raw+json`, and
   only a live check showed it. So the `json` substring test that dropped
   every file stayed green through five releases, 0.12.0 to 0.14.0. Copy
   content types from a recorded response.
8. **An unverified API shape may add evidence, never veto verified evidence.**
   Bitbucket Cloud's history reader was first built so that an unverified
   snapshot field could fail the verified `changes.description` history of
   every edited PR. The full gate was green, and only a final review caught it.
9. **A mutation check still needs `PYTHONDONTWRITEBYTECODE=1`.** Restore the
   file and `cmp` it afterwards, or a same-size restore can run the cached
   mutant again.

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
key. Of the 8 keys 0.15.0 added, 7 feed it this way; `prompts_dir` does not,
since `orchestrate_review` takes the loaded templates as `prompts`. Current
values: **63** keys, **1** legacy alias, **64** accepted names.

## Release shape (follow this next time)

How 0.15.0 was built:

1. **Maps and one plan first.** Each issue got a read-only map of the code it
   would touch. One plan merged the maps, and the owner decided the cross-issue
   questions before any code was written, including the rule that a new key is
   null when its feature is off.
2. **Foundation.** One task landed every new config key, off by default, before
   any feature work started. #18, which joined later, brought its own key.
3. **Lanes in waves.** Work was split into lanes by subsystem: quality (#10,
   #13, #18), prompts (#11, #12), eval (#14), forge (#15) and replay (#16).
   - Tasks ran in waves of parallel agents, each agent in its own worktree
     against a pinned base commit.
   - Each task was rated at most 5 of 10 for complexity and brought its own new
     test file.
   - 45 tasks merged in eight waves before the release documents were written:
     the foundation, 5 forge, 9 quality, 11 prompts, 12 eval and 6 replay
     tasks, and the example-echo fix. #18 joined the release after that, as 3
     more tasks in two waves: the pass and its config surfaces in parallel,
     then the wiring.
4. **One integration gate per merge.** Each branch merged into `release/X.Y.Z`
   on its own. A merge stayed only if the full `uv run pytest` and
   `uv run ruff check src tests` passed on the merged tree.
5. **Read-only live checks.** They ran against public PRs, with a guard that
   blocked forge writes. Six ran:
   - One found GitHub silently withholding patches once a listing page nears
     1 MB. That brought in the compare-first path before release.
   - A later one found the GitHub file-content bug.
6. **Release.** Parallel tasks swept stale docs, bumped the version, and wrote
   the CHANGELOG and this file. The file-content fix merged alongside them.

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
6776 passed                                   uv run pytest -q
All checks passed!                            uv run ruff check src tests
0.15.0                                        uv run prxref --version
```

These counts come from the release branch, measured at the commit that last
updated this file.

### Live checks

- **#15, before the fix: GitHub withholds patches.** sblattj/prxref#9 (127
  files, +32,003/-612) gets a 406 from the diff endpoint. An interim build
  rebuilt it from the `/files` listing: the 110 files whose patch came back
  matched the compare diff line for line, but 16 ordinary text files
  (+6,880/-15, 21% of the added lines) came back at 0/0 with no patch at
  `per_page=100`, and whole at `per_page=30`. That defect brought in the
  compare-first path. 0.14.0 ends the same review as `Error`.
- **#15: GitHub's counts on 23 public PRs.** Across 11 repositories (binaries,
  renames, a submodule, mode-only, empty and deleted files, 211- and 438-file
  PRs, 48k-line lockfiles, 10 open PRs, 8 of them from forks), the `/files`
  sums and the compare diff each matched the PR's counts on 23 of 23. The
  compare diff was byte-identical to the PR diff on 22 of 22, resolved fork
  SHAs on 17 of 17, and on 10 of 10 open PRs started from the base tip's merge
  base. GitHub dropped the patch but kept the counts on 10 lockfile entries in
  4 ordinary PRs, so such a file is reviewed header-only with a warning.
- **#15: the shipped fix.** `get_diff` reads sblattj/prxref#9 (127 files,
  +32,003/-612, refused at 20,000 lines) and ObiterDictum/obiter#226 (438
  files, refused at 300 files) whole from the compare endpoint in 3 GETs, with
  no `/files` request; the listing alone would refuse #9 (+25,123/-597 against
  +32,003/-612). The no-post review of #9 completes: `Request-Changes`, 8
  chunks plus the sweep, 3 findings, USD 0.0745. The same check found the
  GitHub file-content bug fixed in this release: that review dropped 83 of 83
  file reads.
- **#11, #12 and #13 on pallets/click#3860.** It ran as 3 diff chunks at
  `PRXREF_CHUNK_TOKEN_BUDGET=1500`. #12: each chunk got exactly the scoped
  rules its globs matched, the sweep got both, and `--scoped-rules ""` turned
  them off. #13: gpt-4o-mini named a rule on 20 of 20 raw findings and
  gpt-4.1-mini on 20 of 26; 2 groups per model, `locations` matching `Also
  at`; the warning cap left 1 warning each. #11: the record stamped the edited
  `worker.md`'s SHA-256, and a copy without the marker exited 2 before any
  network call. #10: 0 candidate pairs in 4 on/off pairs. USD 0.046 over 23
  runs.
- **Zero chunk findings on sblattj/prxref#9.** Chunk size is not the cause: 8
  chunks at 39,962-59,470 input tokens and 32 chunks at 9,488-19,532 both
  returned 0 findings, as did an exact repeat and gpt-4.1-mini (0 of 56 chunk
  calls). A positive control found 5 of 8 planted labels, all spec-tier, and 0
  of 3 generic bugs. `worker.md`'s "roughly 30k tokens" sentence was false at
  defaults (8 of 8 chunks over it) and is removed. 9 of 12 gpt-4o-mini sweep
  findings were `tests/evals/` fixture text. USD 0.323.
- **#16 on GitHub and Bitbucket Cloud.** On astral-sh/ruff#28750 (5
  description versions, 1 rename), `get_pr_history` read a complete history
  ending at the live body. Two `--as-of` cutoffs pinned versions 2 and 3 with
  the pre-rename title, no `--as-of` chose the first review, and with no token
  the replay fell back to the live text with a WARNING naming
  `PRXREF_GITHUB_TOKEN`. On Bitbucket Cloud, 4 of 4 cutoffs matched a hand
  derivation, a 25-PR scan found live `changes.title` renames, a PR renamed
  twice pinned at 6 of 6 cutoffs, and a two-page feed at 10 of 10. USD 0.0034.

## Still open — not part of this release

- **#17 Repository context outside the diff.** Mapped and planned for 0.16.0.

The known limitations, in full in the CHANGELOG:

- **#10 The reworded tier is unproven.** It ships off. Its suggested threshold
  was chosen on invented negatives. A live check found no candidate pair in
  eight runs, so the tier has not yet been seen to fire on a real PR.
- **#12 Chunks are not packed by language.**
  - `triage.build_chunks` fills chunks by token budget and file cap. Among the
    chunks with room, it prefers the one sharing the deepest directory
    (`_shared_dir_depth`).
  - So a chunk that holds two kinds of file gets the union of both files'
    scoped rules. A live check saw exactly this.
- **#13 Grouping keys on the rule the model names.**
  - A finding with no rule groups by title only. It never joins a group whose
    findings name a rule, even with the same title in the same file.
  - A member that line alignment moved to file level adds no `Also at`
    location.
  - The caps rank by `finding_rank_key` (confidence, then file path), not by
    group size. So capping a representative drops all its folded locations from
    the output. The same goes for the finding the per-rule cap (#18) folded a
    rule's other findings into.
- **Findings on files outside the chunk.** A worker can report on a file it saw
  only in the bounded `### Other files changed in this PR` excerpt
  (`chunk_context.sibling_summary_block`). Location validation and line
  alignment run over the whole PR's files, so such a finding is kept and
  anchored like any other. Nothing marks it as coming from an excerpt.
- **#14 The judge shares the review's backend.**
  - Only `--judge-model` changes which model judges.
  - Self-judging is detected by exact model name. It warns and stamps
    `self_judged`, and is never refused.
  - Scoring needs human-labelled cases, and the repository ships three.
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
  - Weigh it against #17's context budget.
- **Generic-bug recall is unmeasured beyond a small sample.** On the three
  bundled eval cases, gpt-4o-mini found every spec-grounded label and none of
  the three generic bug labels. `worker.md`'s "Prefer zero findings over one
  speculative finding" is an untested suspect. Measure with `prxref eval`
  before tuning the prompt.
- **Review has no path-ignore setting.** `PRXREF_SIZE_IGNORE_GLOBS` affects only
  the size counts. A repository's test fixtures that contain deliberate
  violations are reviewed as code. In this repository, the sweep reviews the
  eval fixtures under `tests/evals/` and reports their planted violations as
  findings.
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

Carried over from 0.14.0, still true:

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
  file context and no threads. The title comes from the patch mail or the file
  name. The description comes from the mail, `--description-file` or
  `--no-description`.
- **Azure DevOps.**
  - Only anonymous reads are verified live: the forge reads, the dry-run output
    shape, the pinned-range compare diff and a pinned-range replay.
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

- **After this release, tune #10's threshold.** Run `prxref eval` on a real
  replay set, then consider changing the default. Use replays pinned by #16:
  earlier tuning ran on replays that leaked the PR's current description.
- **Measure #18's default cap.** The issue's before and after numbers come from
  a simulation over graded replays, and were not re-measured on prxref's own
  output. `prxref eval compare` on two labelled runs measures it: `prxref eval
  run` the same cases with the same `--rules-file` twice, once with
  `PRXREF_MAX_FINDINGS_PER_RULE=0` and once at the default, `prxref eval score`
  both, then compare them. Each run's `run.json` records the cap it used.
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
- **Missing definitions.** `chunk_context.referenced_definitions` looks only in
  the same file, and `_language` has no Java entry, so a Java chunk gets no
  definitions.
- **The history read is untraced.** The replay's history read is outside the
  trace, and only the `replay` stamp records its outcome.
- **The sweep's example is always in force.** `orchestrator._example_titles`
  always reads the systemic template. So the sweep's example title is in force
  even on a run whose sweep never runs.

The v0.14.0 handoff's GitHub 20,000-line bullet is **fixed** (#15), and so is
its manual eval scoring (#14).

| Item | Value |
|---|---|
| Released version | `0.15.0` (minor: new eval commands, new inputs and eight new config keys, seven off by default and the per-rule cap on by default whenever a review rules file is loaded; no existing config default changed, but a `--pr-url` replay now pins its title and description by default, a review rules file now also asks every unit for a rule and folds each rule's findings past the second, and every worker prompt hash moved) |
| Registration points | forges: the tuple in `forges/base.py` (`detect_forge`) and the `impls` dict in `config.py` (`make_forge`); LLM backends: `llm_backends.BACKENDS`; glyphs: `prxref.markers`; subcommands: `cli._build_parser`; prompt templates: `prompt_templates.TEMPLATE_NAMES` and `OPTIONAL_PLACEHOLDERS` |
| Version strings | `pyproject.toml`, `src/prxref/__init__.py`, and `uv.lock` |
| Test command | `uv run pytest` (dev tools are a `[dependency-groups]` group, not an extra) |
| Release assets | wheel **and** sdist attached by `release.yml`; PyPI by OIDC trusted publishing |
