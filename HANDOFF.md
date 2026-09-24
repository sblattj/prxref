# HANDOFF — v0.14.0 shipped: the inputs release

**Repo:** `sblattj/prxref` (public) · **Released:** 2026-09-24 · **Supersedes** the
v0.5.0 handoff.

0.14.0 lets a review read more than the diff: the spec a PR implements, a team's
own review rules, and the ticket the PR is for. It also adds a replay mode for
evaluation, Azure DevOps as the fifth forge, two backends that run on a Claude
Code or Kiro CLI login, a dollar cost for every run, and a PR size advisory. The
user-facing account is the `[0.14.0]` section of `CHANGELOG.md`. This file is for
whoever cuts the next release.

This is the first release on the rewritten history. The repository was recreated
on 2026-09-24. Every tag and nearly every commit before 0.14.0 has a new SHA, so
re-clone rather than pull, and treat any SHA quoted in an older note as dead.
Issue numbers restarted too: the eight 0.14.0 issues are #1 to #8 on the new
tracker.

## What landed

- **Spec-grounded review.** `--spec` / `PRXREF_SPEC_SOURCES` fetch web pages,
  local files or directories, and Jira tickets. `specs.build_spec_digest` prunes
  them to the constraints relevant to the diff, and the digest goes into every
  worker and sweep prompt. A diff that breaks a quoted constraint draws an
  advisory `spec` finding. The finding has to be earned. When no constraint
  reached the prompts, `quality.apply_spec_grounding` relabels any `spec` finding
  as a `warning`. On a grounded run, `quality.apply_hedge_gate(...,
  spec_digest=...)` skips the text a finding quotes verbatim from the digest.
  The run record carries `spec_grounding`.
- **#1 litellm without a base URL.** Only `openai-compat`, `ferry` and `http`
  need `PRXREF_LLM_BASE_URL`. The other backends ignore it and log one INFO line.
- **#2 Azure DevOps.** `forges/azure_devops.py` covers Services and Server, and
  `detect_forge` asks it last. Azure DevOps has no unified-diff endpoint, so the
  adapter rebuilds the diff from the Diffs API change list plus blob contents.
  Webhooks arrive as service hooks, checked against a Basic-auth secret.
- **#3 Team review rules.** `rules.py` reads `--rules-file` /
  `PRXREF_REVIEW_RULES` into the system prompt. Optional `severity:` front matter
  maps team words onto prxref severities, and `quality.apply_severity_map`
  rewrites them before every other quality pass.
- **#4 Ticket context and scope.** `ticket.py` reads `--context-file` /
  `PRXREF_TICKET_CONTEXT_FILE`, and every finding is judged `in`, `out` or
  `unknown` against it (`triage.normalize_scope`). 🟦 now marks an
  out-of-ticket finding, so minor findings moved to ⬜.
- **#5 Replay.** `--base-sha` / `--head-sha`, `--no-threads` and `--diff-file`
  review a pinned range or a diff file. Every forge implements
  `Forge.get_compare_diff`, `forges/replay.py` holds `LocalDiffForge` and
  `ReplayForge`, and a replay never posts. `tests/evals/test_eval_replay.py`
  replays each eval case with one offline CLI call.
- **#6 Subscription CLI backends.** `llm_cli_backends.py` adds `claude-cli` and
  `kiro-cli`. Each model attempt is one process started in a fresh temporary
  directory, the credential-routing variables are stripped from its
  environment, and a missed deadline kills the whole process group.
- **#7 Dollar cost.** In `costs.py`, a figure the backend reported always wins.
  Otherwise `PRXREF_PRICE_TABLE` gives an estimate, and without one the cost is
  `null`, never `0` and never a partial sum.
- **#8 Size advisory.** `PRXREF_SIZE_WARN_LINES` / `PRXREF_SIZE_WARN_FILES` flag
  an oversized PR. `triage.count_size_relevant_changes` counts the parsed diff,
  skipping lockfiles, generated files and `PRXREF_SIZE_IGNORE_GLOBS` matches.
- **Fixes found on the way.** GitLab's MR diff listing now reads every page and
  fails on a short read instead of stopping at 20 files. GitHub calls time out.
  Prompts and the summary fill in a single pass, so a `{diff}` in PR text stays
  literal. `load_config` no longer shares list defaults between calls. An
  unrecognized `PRXREF_LLM_BACKEND` exits 2. Under `PRXREF_FAIL_ON=error` or
  `any`, a review that ends with verdict `Error` exits 1. A total LLM failure
  counts the sweep that answered, and the `forge.get_diff` trace span counts
  bytes, not characters.
- **Config went from 36 to 55 keys.** The 19 new keys are the CLI path and
  concurrency, cost, size, spec, rules and ticket keys, the Jira credentials, and
  the Azure DevOps token and webhook secret.

## What this release taught

Written down because each one cost a seat real time.

1. **Lay the seams first, and make every stub fail closed.** A foundation stage
   landed every shared surface with a placeholder body before any feature seat
   started: config keys, run-record and JSON keys, prompt slots, trace events,
   the glyph table and the new `Forge` method. That let the eight issue seats run
   in parallel on disjoint files. A stub that fails closed cannot ship as a
   silent no-op. The cost is that placeholder prose outlives the placeholder.
   Docstrings saying the loaders "fail closed in this build" and the cost hooks
   are "inert in this build" survived after the real bodies landed, and the
   release had to sweep them. Grep for `in this build` before cutting.
2. **Cite the symbol, not the line.** Several seats found the `file:line` pins in
   their briefs stale against the base they had been given, and every one of
   them still resolved by symbol name. Pin a SHA if you must give a line, and
   prefer `module.function`.
3. **Keep one table per cross-cutting literal.** Every severity and scope glyph
   comes from `prxref.markers`, and `tests/test_markers.py`
   (`TestGlyphsLiveInOnePlace`) fails when a glyph literal turns up anywhere else
   in the package. So a glyph change is made in one table, and the test finds
   any stray copy.
4. **`Tracer.event(node, phase, **meta)` reserves two keyword names.** A dict
   splatted into it must not carry a `node` or `phase` key. Such a call raises
   `TypeError` at the call site, before tracing's never-raise guard can catch it.
5. **A mutation check needs `PYTHONDONTWRITEBYTECODE=1`.** To prove a test can
   fail, revert a line, watch the test go red, then restore the line and `cmp` it.
   Without the variable, the mutant's bytecode gets cached. A same-size restore
   within the same second can then run the mutant again.
6. **Two contract rules over one field need a tiebreak.** One rule said every
   new JSON key is always present and `null` when its feature is off. Another
   said `replay` is absent on a normal run. The code followed the second, and a
   seam test now pins that. The next contract should say which rule wins before
   any seat starts.

## The coupling that will catch the next person adding a config key

`tests/test_docs_consistency.py` checks `docs/env-vars.md` and `.env.example`
against `config._DEFAULTS` **in both directions**. It also asserts two hard-coded
integers, built as `f"**{len(_DEFAULTS)}** configuration keys"` and
`f"for {len(_DEFAULTS)+len(_LEGACY_ENV_ALIASES)} accepted variable names"`.

So a new config key is not a source change. It is an atomic change across four
surfaces: `_DEFAULTS` plus the `_INT_KEYS` / `_FLOAT_KEYS` / `_RANGES` /
`_CHOICE_KEYS` tables, the `config.py` docstring, `.env.example`, and
`docs/env-vars.md`, including its counts and its per-section headings. 0.14.0
added 19 keys this way. Current values: **55** keys, **1** legacy alias, **56**
accepted names.

## Release shape (follow this next time)

How 0.14.0 was built:

1. **Foundation.** Seats lay the shared seams every feature needs, each with a
   placeholder body that fails closed. Nothing user-visible lands here.
2. **Wave 1.** One seat per issue runs in parallel, each in its own worktree
   with a disjoint file list. A seat fills a placeholder; it does not add a seam.
3. **Wave 2.** Next come the pieces that needed two wave-1 bodies in place (the
   replay CLI, replay over Azure DevOps, the second CLI backend) and the forge
   fixes wave 1 surfaced.
4. **One integration gate per merge.** Each seat branch merges into
   `release/X.Y.Z` on its own, and a merge stays only if the full
   `uv run pytest` and `uv run ruff check src tests` pass on the merged tree.
5. **REL.** Parallel seats sweep stale docs, add cross-seat seam tests, and
   write the version bump, the CHANGELOG and this file. Read-only live checks
   follow against public PRs and real CLIs (see "Live checks" below).

Cutting the release:

```bash
# bump pyproject.toml and src/prxref/__init__.py, then:
uv lock                              # uv.lock carries the version too
git tag vX.Y.Z && git push origin vX.Y.Z
```

Pushing a `v*` tag runs `.github/workflows/release.yml`. Its `release` job runs
`uv build` and creates the GitHub release with the wheel **and** the sdist
attached. The job lists both by explicit pattern, not `dist/*`, which once
shipped a stray `.gitignore` as an asset. Its `publish` job builds again and
publishes to PyPI by OIDC trusted publishing, so no token is stored anywhere. The
two jobs build separately, which makes the PyPI files and the release assets two
builds of the same tag. Keep the attached sdist: at least one consumer updates
itself with `gh release download --pattern '*.tar.gz'`, and that pattern does not
match GitHub's auto-generated source archive.

The repository was recreated, so check three things before its first tag push:
Actions is enabled, the `pypi` environment exists, and the PyPI trusted
publisher still names owner `sblattj`, repository `prxref`, workflow
`release.yml` and environment `pypi`.

## Verified at release

```
4222 passed                                   uv run pytest -q
All checks passed!                            uv run ruff check src tests
0.14.0                                        uv run prxref --version
```

These counts come from the release tip, after every feature, fix and test
branch had merged.

### Live checks

All ran on 2026-09-23 and were read-only: reviews ran with `--no-post` or with
the forge's write methods captured, and a guard blocked forge writes. The
targets were psf/requests#6963 on GitHub, a PR in a public Azure DevOps
Services project (read anonymously), merge requests in the gitlab-org group on
gitlab.com, and the bundled eval cases.

- **#1 litellm without `PRXREF_LLM_BASE_URL`.** It answered with a cost and no
  configuration error. With a URL set, it logged the "set but not used" INFO
  line.
- **#2 Azure DevOps.** The forge-level check passed 43 of 43. The dry-run JSON
  key set equalled the GitHub control's in 3 of 3 runs. The first two lost
  chunks to LLM timeouts and a malformed model reply, and the third reviewed
  every chunk. The compare range matched `get_diff` byte for byte, including
  two PRs one commit behind their target. A pinned-range replay passed 25 of
  25.
- **#3 Team review rules.** 19 of 19 checks passed: the prompts, the run
  record's hash and severity map, truncation, and exit 2 on a bad path.
- **#4 Ticket context and scope.** Passed after the scope fix, which shows a
  `"scope"` key in the prompt's JSON example while a ticket is active. On the
  case-002 eval, gpt-4.1-mini went from 0 of 10 findings labelled to 12 of 12,
  and prompts without a ticket stayed byte-identical. With an off-ticket file
  added, 5 of 5 off-ticket findings came back `out`. The example's value does
  not anchor the answer: with the example set to `out`, 12 of 12 on-ticket
  findings still came back `in`.
- **#5 Replay.** The mechanics passed: the stamp, the pinned diff, the hidden
  threads, a forge-less `--diff-file` run with no token, and exit 2 on 6 of 6
  bad flag sets. A replay pinned to a different PR's range is model-fragile:
  the PR's current title and description are kept, as documented, and the
  model returned non-JSON in 3 of 3 tries, while the matching control was
  approved in 2 of 2.
- **#6 Subscription CLI backends.** `claude-cli` ran on the subscription login,
  with no `ANTHROPIC_API_KEY` in the child's environment and `--effort low`
  passed through. `kiro-cli` read "cost unknown", logged its credits, and
  honoured the model in its agent file. The `(API-equivalent)` cost label
  landed after this check. It is covered by `tests/test_issue_67_cost.py` and
  was not re-run live.
- **#7 Dollar cost.** A reported cost matched the sum of its units, a
  cost-stripping relay gave `null`, a price table's estimate matched the
  formula, and a malformed table exited 2. The `-v` line showed `$…`,
  `~$… (est.)` and `cost unknown`.
- **#8 Size advisory.** 4 of 4 checks passed, and the parsed diff's counts
  equalled the GitHub API's.
- **Spec grounding.** On the bundled eval, grounded against ungrounded runs
  surfaced 8 of 10 against 3 of 10 planted spec violations on gpt-4.1-mini,
  and 10 of 10 against 4 of 10 on claude-haiku-4.5.
- **GitLab paging.** A 130-file MR was read across 2 pages, and `too_large`
  files were listed header-only with a warning.
- **Spend.** About $0.31 of LLM calls in total. That is an upper bound,
  because it counts calls that timed out before reporting a cost at their
  largest possible cost. The `claude-cli` runs
  (about $0.09 API-equivalent) and the `kiro-cli` runs (about 0.09 credits)
  used subscriptions and are not included.

## Still open — not part of this release

The known limitations, in full in the CHANGELOG:

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
- **`kiro-cli`.** It always reports "cost unknown". Whether user-level Kiro
  configuration such as `~/.kiro/steering/` reaches prxref's per-call agent has
  not been verified. The model that actually ran is not reported, and every chat
  is kept under `~/.kiro/sessions/cli/`.
- **GitLab.** Files GitLab withholds as `too_large` or `collapsed` are listed
  header-only, and an MR past 5,000 files fails. Reading an MR's threads on
  gitlab.com needs `PRXREF_GITLAB_TOKEN` even for a public project; without one,
  thread dedup runs against no threads.
- **Replay.** A `--diff-file` run without `--pr-url` sees only the diff.
- **Azure DevOps.** Only anonymous reads are verified live: the forge reads, the
  dry-run output shape, the pinned-range compare diff and a pinned-range replay.
  Posting, pruning, PAT and `SYSTEM_ACCESSTOKEN` authentication and service hooks
  are tested against recorded API shapes only. Azure DevOps Server is untested.
- **Gating.** A mistyped or unrecognized `--pr-url` exits 0 even under
  `PRXREF_FAIL_ON=error` or `any`, because nothing was reviewed.

Follow-ups a seat reported that did not land:

- **Eval scoring is manual.** `tests/evals/test_eval_replay.py` replays every
  case offline, but scoring (section 7.2 of `docs/spec-grounded-review.md`)
  is not built: judging findings against a case's `expected.json` needs a live
  model and stays manual.
- **One seam is tested only in halves on two paths.** The size advisory and
  the cost label are tested together on the main summary post
  (`tests/test_release_seams.py`), but on the inline-accounting refresh post
  and on the summary-only run each is tested alone.
- **Scope labelling is measured on one fixture shape.** The live check added an
  off-ticket file in its own directory to the case-002 eval, and ran three
  times for each example value on one model, plus once more on a second model.
  An off-ticket change inside an on-ticket file is unmeasured.

The v0.5.0 handoff left three forge-wide items open. All three are **fixed**:

- Every retry session allows only `GET`, `HEAD` and `OPTIONS`.
- A comment listing that fails or comes back short raises `FeedReadError`
  instead of passing for "no summary exists".
- Every forge, Bitbucket Cloud included, finds its own summary by
  `SUMMARY_MARKER` and updates it in place.

| Item | Value |
|---|---|
| Released version | `0.14.0` (minor: new inputs, a forge and two backends; an unrecognized `PRXREF_LLM_BACKEND` now exits 2) |
| Registration points | forges: the tuple in `forges/base.py` (`detect_forge`) and the `impls` dict in `config.py` (`make_forge`); LLM backends: `llm_backends.BACKENDS`; glyphs: `prxref.markers` |
| Version strings | `pyproject.toml`, `src/prxref/__init__.py`, and `uv.lock` |
| Test command | `uv run pytest` (dev tools are a `[dependency-groups]` group, not an extra) |
| Release assets | wheel **and** sdist attached by `release.yml`; PyPI by OIDC trusted publishing |
