# HANDOFF — v0.25.0 shipped: repository config file .prxref.toml (#38)

**Repo:** `sblattj/prxref` (public) · **Released:** 2026-09-28 · **Supersedes** the
v0.24.0 handoff.

0.25.0 is one feature. #38: a repository can commit its prxref settings in a
flat TOML file, `.prxref.toml`, instead of copying them into every pipeline.
The file sits between the built-in defaults and the environment
(`defaults < file < PRXREF_* < flags`), and whatever can send a secret
somewhere, run a program, read or write a file outside the review's inputs,
or move the exit code stays in the environment. New surfaces: `--config PATH`
and `--no-config` on `review` and `eval run`, `--config PATH` on `serve`, a
`prxref config check` subcommand, a `config_file` run-record key, and the
`PRXREF_CONFIG_FILE` variable, which is not a config key. No new config key,
`Forge` method or adapter. With no file found or named, every config value,
error message, prompt, forge read, forge write and stdout byte is the same as
in 0.24.0, and the record gains only `"config_file": null`. The user-facing
account is the `[0.25.0]` section of `CHANGELOG.md`. This file is for whoever
cuts the next release. The v0.24.0 handoff is in git history.

## What landed

- **The file layer, `src/prxref/config.py`.** `find_config_file(*, explicit,
  cwd=None, environ=None)` resolves `--config`, else `PRXREF_CONFIG_FILE`
  (`CONFIG_FILE_ENV`), else `.prxref.toml` (`CONFIG_FILE_NAME`) in the
  working directory, with no walk up; `off` in any case reads none, an empty
  variable reads as unset, and a named file that is missing raises
  `ConfigError` naming the flag or the variable. `read_config_file(path, *,
  display=None)` parses it with `tomllib` and checks each key in a fixed
  order: table, unknown key (with a `difflib` hint), environment-only, type,
  empty value (read as unset), `spec_sources` URL, path containment
  (`_contained_path`: the value is joined to the file's directory, both
  sides `realpath`-ed, and must stay inside). `load_config(*, config_file=None,
  ...)` layers the file under the environment and labels each file value
  `<file>: <key>` for error messages; `load_config_with_sources` also returns
  each key's layer (`default`, `file`, `env <NAME>`, `override`).
- **The partition.** `FILE_KEYS` (44) and `ENV_ONLY_KEYS` (30) are two
  hand-written frozensets, and `TestPartition` requires them to partition
  `_DEFAULTS`, so a new key cannot land unclassified. `_ENV_ONLY_REASONS`
  gives each environment-only key its class: credential, endpoint
  (`llm_base_url`, `jira_base_url`), executable (`llm_backend`,
  `llm_cli_path`), local write (`trace_file`, `trace_dir`, `fallback`), local
  read (`price_table`) or gate (`fail_on`, `dry_run`, `allow_unsigned`).
- **Types.** A file takes TOML types: integers (booleans refused;
  `llm_seed` also takes `"off"`), numbers, booleans, arrays of strings, and
  strings. `llm_temperature` is a string key that also takes a TOML number,
  stored as the string the variable would hold (`0.2` becomes `"0.2"`).
- **Command line, `src/prxref/cli.py`.** `_resolve_config_file` serves
  `review`, `config check` and (in `evals.py`) `eval run`; `--config` and
  `--no-config` are an argparse mutually exclusive group. `_serve_config_file`
  calls `find_config_file` only when `--config` or `PRXREF_CONFIG_FILE` is
  non-blank, so `serve` never looks in its working directory; `_cmd_serve`
  checks the named file with `read_config_file` before listening and binds
  it to `_webhook_handler` with `functools.partial`. `_cmd_config_check`
  prints every `_DEFAULTS` key, sorted, with its layer, and prints a
  credential-class value only as `<set>` or `<unset>`.
  `_config_file_stamp` builds the record's `config_file`
  (`{"path", "sha256", "keys"}`), attached in `cli._run_review` after
  `orchestrate_review` returns, so the orchestrator's own dict and
  `tests/test_run_record.py` did not move. `_build_json_result` puts it after
  `degraded`.
- **`config check` opens the files path keys name.** The rules file, the
  scoped rules, the ticket file and the prompts directory are loaded by one
  helper, `review_inputs.load_path_inputs`, which `cli._run_review`,
  `cli._cmd_config_check` and `evals.eval_run` (without the ticket, which
  each case names) all call. So a missing rules file or an unusable prompts
  directory now fails `config check` with the exit 2 and the
  `configuration error: ...` line the review would print, and the
  misplaced-copy recipe error of lesson 1 is caught there. Each input is
  named by `review_inputs.path_input_source` from the layer
  `load_config_with_sources` reports: the flag, `<file>: <key>` when the
  config file set the path (before, a file value was reported under its
  `PRXREF_` variable), else the variable. `spec_sources` is not among them:
  the orchestrator loads spec sources best-effort after the forge is built,
  and a failed one never exits 2. `cli._load_prompts_dir` stays as a thin
  wrapper, and the CLI hands the helper its own loader names
  (`cli._path_loaders`), because tests replace `prxref.cli.load_review_rules`
  and friends.
- **A file named through a symlinked directory displays as its resolved
  spelling.** `config._display_path` first compares `os.path.abspath(path)`
  with `Path.cwd()`, as before, then the file's resolved directory (the file
  name itself is not followed) with the resolved working directory. So on
  macOS, from `/private/tmp/x`, `config check --config /tmp/x/.prxref.toml`
  prints `config file: .prxref.toml`, as the `/private/tmp/...` spelling
  does, and errors and the record's `config_file.path` agree. A path
  outside the working directory still displays as given.
- **Eval.** `eval run` resolves and checks the file once before the first
  case and passes it to every case (`evals._run_case(..., config_file=...)`).
  `eval score` builds its judge from a file-free `load_config()`.
- **Docs.** `docs/config-file.md` (where the file is read from, precedence,
  schema, both key tables, paths, every error message, `config check` with a
  real sample, and "Security: which copy of the file does CI read?" with the
  target-branch recipe); a commented `docs/examples/prxref.toml`; a
  `PRXREF_CONFIG_FILE` row in `docs/env-vars.md` and `.env.example`; pointers
  from the README, the per-forge CI recipes in `docs/forges.md`,
  `docs/review-rules.md`, `docs/prompt-templates.md` and `docs/evals.md`.
- **Tests.** Three new files: `tests/test_issue_38_config_file.py` (the
  layer), `tests/test_issue_38_cli_config.py` (flags, `serve`,
  `config check`, the record, a byte-for-byte no-file invariant against
  0.24.0's stdout, the path inputs `config check` opens and the key they
  are named by, and the symlinked-directory display), and `tests/test_issue_38_config_docs.py` (the doc's key
  tables against `FILE_KEYS` / `ENV_ONLY_KEYS` both ways, each error-table
  example fed to `read_config_file`, and the example file loaded).
  `tests/conftest.py` now also clears `PRXREF_CONFIG_FILE`.

## What this release taught

Written down because each one cost real time.

1. **A repository config file is PR-controlled in CI, so the file/environment
   split is a security boundary, and paths need containment.** A
   `pull_request` job's workspace is the PR's code, so auto-discovery reads
   the author's own copy. Hence the environment-only classes and
   `_contained_path`. Containment has a consequence for CI recipes: paths
   resolve against the file's directory, so copying the file alone out of
   the target branch (`git show FETCH_HEAD:.prxref.toml > $RUNNER_TEMP/...`)
   silently repoints `review_rules = ".prxref/rules.md"` at
   `$RUNNER_TEMP/.prxref/rules.md`, which does not exist. The docs task
   found this by reading `_contained_path`, and the release check confirmed
   it: that copy passed `prxref config check` with `ok`, because
   `config check` did not yet open the files path keys name. The shipped
   recipe extracts `.prxref.toml` and `.prxref/` together with `git archive`,
   and `config check` now opens those files, so the one-file copy fails it
   (What landed).
2. **Splitting a flag task from its docs task leaves both red alone.**
   `tests/test_cli_output.py`, `tests/test_eval_cli.py` and
   `tests/test_evals_docs.py` pin every parser option and every
   `--format json` key to the README and `docs/evals.md`. The command-line
   task could not edit those docs and the docs task could not see its flags,
   so the merged base measured 9771 passed and 6 failed, all docs pins for
   `--config`, `--no-config` and `config_file`. Tell the release commit to
   expect it, or give the flag task README ownership.
3. **The early `load_config()` in `cli._cmd_review` needs no file.** It reads
   only `fail_on` and `fallback`, and both are in `ENV_ONLY_KEYS` (gate,
   local write), so a file could never set either, and it stays file-free.
   The file is resolved in the same `try` block, so a missing `--config`
   still exits 2 before any review.

## The coupling that will catch the next person adding a config key

`tests/test_docs_consistency.py` checks `docs/env-vars.md` and `.env.example`
against `config._DEFAULTS` **in both directions**. It also asserts two hard-coded
integers, built as `f"**{len(_DEFAULTS)}** configuration keys"` and
`f"for {len(_DEFAULTS)+len(_LEGACY_ENV_ALIASES)} accepted variable names"`.

So a new config key is not a one-file change. It changes these surfaces
together:

- `_DEFAULTS`, plus whichever of the `_INT_KEYS`, `_FLOAT_KEYS`, `_BOOL_KEYS`,
  `_LIST_KEYS`, `_RANGES` and `_CHOICE_KEYS` tables apply to it
- **new in 0.25.0:** exactly one of `FILE_KEYS` or `ENV_ONLY_KEYS` (with a
  class in `_ENV_ONLY_REASONS`), because a test requires the two to
  partition `_DEFAULTS`, and its row in the matching table of
  `docs/config-file.md`, which `tests/test_issue_38_config_docs.py` compares
  to both sets. Anything that names a host, runs a program, writes a file or
  reads outside the repository is environment-only. An environment-only key
  also needs its row in `EXPECTED_ENV_ONLY` in
  `tests/test_issue_38_config_file.py`, the reviewed table the set is pinned
  to. A path key a file may set also goes in `_FILE_PATH_KEYS`, which puts
  it through `_contained_path`.
- the `config.py` docstring
- `.env.example`
- `docs/env-vars.md`, including its counts and its per-section headings

A key that feeds `orchestrate_review` needs one more step. `tests/test_cli.py`
(`test_run_review_passes_every_configured_orchestrate_kwarg`) requires
`cli._run_review` to pass every orchestrator kwarg whose name equals a config
key. A new value for an existing key touches none of the counts, but still
needs the docstring, `.env.example` and `docs/env-vars.md` to describe it,
and a value that is not an integer needs its own pass through `_coerce_env`,
`_check_ranges` and, for the file, `_file_value`.
Current values, counted from `config._DEFAULTS` and
`config._LEGACY_ENV_ALIASES` at this release: **74** keys (44 file, 30
environment-only), **1** legacy alias, **75** accepted names, unchanged from
0.24.0. `PRXREF_CONFIG_FILE` is counted in neither: it is not a key, and
`tests/test_docs_consistency.py` accepts it through `config.CONFIG_FILE_ENV`.
The most recent new keys are `gitea_token` and `gitea_webhook_secret`
(0.24.0), plain strings read by the adapter and the webhook verifier
themselves, with no `cli._run_review` pass-through.
`tests/conftest.py` derives its env-clearing list from `_DEFAULTS` and clears
`PRXREF_CONFIG_FILE` by name, so an ambient value never reaches a test.

## Release shape (follow this next time)

How 0.25.0 was built:

Three code tasks in two rounds, each in its own worktree with its tests in a
new file, merged one at a time behind a full gate, then this commit.

1. **First, the config-file layer** in `config.py`, from the pinned 0.24.0
   commit: 9497 to 9620.
2. **Then, in parallel on that merge:** the command-line wiring and
   `prxref config check` (9620 to 9677 passed and 6 failed, the docs pins of
   lesson 2), and the docs, example file and CI recipes (9620 to 9714).
   Merged, the base measured 9771 passed and 6 failed.
3. **Release.** This commit bumps the version, adds the CHANGELOG section,
   rewrites this file, and reconciles the README, `docs/evals.md`,
   `docs/config-file.md`, `CLAUDE.md` and the `cli.py` / `config.py`
   docstrings against the merged command line. It adds no test; the six
   docs pins turn green, so the suite measures 9777 passed.
4. **Two fixes found by the release checks.** `config check` now opens the
   files that path keys name, through a loader it shares with `review` and
   `eval run`, and a file named through a symlinked directory displays as
   its short name: 9777 to 9792.

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
9792 passed                                   uv run pytest -q
All checks passed!                            uv run ruff check src tests
0.25.0                                        uv run prxref --version
```

These counts come from the release branch, measured at the commit that last
updated this file.

### Live checks

No forge and no model is needed for these; each ran the installed
`prxref` in a scratch directory.

- **`prxref config check` with a file.** A `.prxref.toml` holding
  `max_chunks = 4`, `llm_temperature = 0.2` (a bare number),
  `review_rules = ".prxref/rules.md"` and `post_mode = "summary+inline"`,
  with `PRXREF_POST_MODE=summary` and a fake `PRXREF_GITHUB_TOKEN` in the
  environment: exit 0, 76 lines (`config file: .prxref.toml`, 74 settings,
  `ok`), `max_chunks = 4  (file)`, `llm_temperature = 0.2  (file)`,
  `post_mode = summary  (env PRXREF_POST_MODE)` (the environment beat the
  file), `review_rules` printed as its resolved absolute path, and
  `github_token = <set>  (env PRXREF_GITHUB_TOKEN)`. The fake token's text
  appeared 0 times in stdout and stderr, in text and in JSON. `--format json`
  gave one object whose `config_file` was `".prxref.toml"`.
- **Without a file**, and with `--no-config` beside one: `config file: none`
  (`null` in JSON), `max_chunks = 8  (default)`. `PRXREF_CONFIG_FILE=off`,
  `OFF` and `--config off` read none; an empty `PRXREF_CONFIG_FILE` still
  found `.prxref.toml`.
- **Errors.** `max_chunk = 4` exited 2 with the `did you mean 'max_chunks'?`
  hint, and with `--format json` left stdout empty (0 bytes);
  `fallback = "off"` exited 2 as `(local write)`; `max_chunks = 0` printed
  `.prxref.toml: max_chunks: must be a finite number greater than 0, got 0`;
  a missing `--config` exited 2 from both `config check` and `review`, before
  any network call; `--config x --no-config` was refused by argparse.
- **The target-branch recipe, run as written** in a scratch repository: a
  base commit with `.prxref.toml` (`max_chunks = 4`, `review_rules =
  ".prxref/rules.md"`) and `.prxref/rules.md` (`BASE RULES`), and a PR
  commit that changed both (`max_chunks = 1`, `confidence_floor = 1.0`,
  `PR RULES`), cloned onto the PR branch as a CI checkout. `git fetch
  --depth=1 origin main` and `git archive FETCH_HEAD .prxref.toml .prxref |
  tar -x` extracted both files with the base's rules text, and
  `config check --config $RUNNER_TEMP/base/.prxref.toml` gave
  `max_chunks = 4  (file)`, `confidence_floor = 0.6  (default)` and
  `review_rules` under `$RUNNER_TEMP/base/.prxref/`: the base won. The
  control, auto-discovery in the same checkout, read the PR's copy
  (`max_chunks = 1`, `confidence_floor = 1.0`). The one-file `git show`
  variant resolved `review_rules` to `$RUNNER_TEMP/.prxref/rules.md`, which
  was never extracted, and still printed `ok` (lesson 1), before
  `config check` opened path keys; it now exits 2 naming
  `.prxref.toml: review_rules`, which the test suite covers. `git archive` with
  a path missing on the branch exited 128.

End-to-end review: `config check` from a scratch directory whose
`.prxref.toml` sets `llm_models`, `max_chunks = 2`, `llm_temperature = 0.2`
and `max_inline_comments = 5` reported all four as `(file)` and the model
endpoint as `(env ...)`. A `--no-post` review of a 32-file pull request on
this repository then split the diff into 2 chunks (13 and 19 files), which is
the file's `max_chunks`, and the record's `config_file` listed those 4 keys
right after `degraded`. The verdict was Error, because on the fast model both
chunks were cut off at the default 4096-token output budget, even after a
retry. That is the known token-budget limit, not a config-file fault.
`config check` on a file naming a missing `review_rules` exited 2 with an
empty stdout and named `.prxref.toml: review_rules`; once the file existed,
it printed `ok`.

## Still open — not part of this release

- **The webhook daemon cannot read the reviewed repository's file.** `serve`
  reads only a file named on its own host (`--config` or
  `PRXREF_CONFIG_FILE`), the same for every repository it serves. Reading
  each pull request's `.prxref.toml` (from its target branch) would need a
  forge fetch per webhook, through `Forge.get_file_content`, and a decision
  about which branch's copy to trust.
- **`prxref eval score` reads no file.** Its judge is built from a file-free
  `load_config()`, and it has no `--config` flag. Only `llm_parse_retries`
  among the judge's inputs is a file key, so a file value for it reaches
  `eval run`'s reviews but not the judge.
- **No CI runner has run the target-branch recipe.** It ran as written in a
  scratch repository (Live checks), not inside GitHub Actions, GitLab CI or
  Bitbucket Pipelines.
- **Upstream Gitea's single-comment delete is unverified.** Pruning a review
  that also holds a human's comment uses
  `DELETE /pulls/{n}/reviews/{id}/comments/{c}`, which ran live on Forgejo
  only. If upstream Gitea refuses it, the prune logs a WARNING and prxref's
  old comment stays. Check it against a Gitea instance.
- **Whether Forgejo and Gitea runners render the fallback is unverified.**
  Their runners set `GITHUB_ACTIONS=true`, so a run that cannot post prints
  GitHub `::warning` annotations and appends to `$GITHUB_STEP_SUMMARY`;
  whether either UI shows them is unknown, though the lines are always in
  the job log.
- **Gitea Actions and Forgejo Actions have not run the CI recipe live.**
  The recipe in `docs/forges.md` follows the GitHub Actions syntax both
  document, including the `synchronize` event name; the first live run is
  its proof. The adapter itself has not reviewed a pull request on
  Codeberg, gitea.com or upstream Gitea.
- **With `PRXREF_POST_MODE=inline`, a read-only token is detected on GitHub
  and Gitea / Forgejo only.** Bitbucket Cloud, Bitbucket Server, GitLab and
  Azure DevOps skip a refused inline comment without raising (lesson 2 of
  the v0.23.0 handoff), so an inline-only run on those forges records no
  degradation and emits nothing. Gitea / Forgejo post every inline comment
  in one review that is accepted or refused whole, and a refusal raises.
  The default post mode is covered everywhere, because the summary post
  fails first and the inline comments wait on it. Closing it means those
  adapters must report a 401 or 403 on an inline comment, which changes what
  `post_inline_comments` returns or raises on four forges.
- **The fallback formats have no live proof yet.** The first will be a fork
  PR under a plain `pull_request` workflow in a real CI run; watch its
  annotations, its job summary and its `degraded` record.

- **The incremental marker round-trip has not been exercised live with
  posting.** Verification never posts, so no live run has written a marker
  and read it back on the next push. The first real multi-push use will be
  the first live proof; watch its `incremental` record and the summary's
  note line.
- **5-file chunks on GLM 5.3 Flash can exhaust even the 8192 retry
  budget.** In the v0.22.0 handoff's live check, 2 of 4 units stopped at 4096 and again
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
  - **Other forges.** On GitLab, Gitea / Forgejo, Azure DevOps and Bitbucket
    Server, a replay logs a WARNING and uses the live title and description.
    An explicit `--as-of` there exits 2.
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
| Released version | `0.25.0` (minor: repository config file `.prxref.toml`, #38; flags `--config` / `--no-config` on `review` and `eval run`, `--config` on `serve`; subcommand `prxref config check`; run-record key `config_file`; variable `PRXREF_CONFIG_FILE`, not a config key; no new config key, adapter or `Forge` method) |
| Registration points | config-file classification: `config.FILE_KEYS` / `config.ENV_ONLY_KEYS` with `_ENV_ONLY_REASONS`, and `_FILE_PATH_KEYS` for contained paths; CI fallback: `ci_fallback.detect_ci` (which CI) and `cli._emit_fallback` (what each CI gets); forges: the tuple in `forges/base.py` (`detect_forge`, where order matters only as a guard, and Gitea's any-host pattern must keep refusing the other forges' hosts) and the `impls` dict in `config.py` (`make_forge`); webhooks: the header dispatch in `webhooks.verify_signature`, where a forge that also sends GitHub's headers must be checked before GitHub; repository listing: the optional `Forge.list_paths` in `forges/base.py`, on every adapter; summary read-back: the optional `Forge.get_summary`, on every adapter; the reviewed-head marker: `orchestrator.REVIEWED_HEAD_PREFIX` and `REVIEWED_HEAD_SUFFIX`; repository-context entries: `repo_context.KINDS`, where an entry's kind picks the prompt block it renders in and the tuple's order ranks nothing, and `REASONS`, whose order is the budget's rank; LLM backends: `llm_backends.BACKENDS`; glyphs: `prxref.markers`; subcommands: `cli._build_parser`; prompt templates: `prompt_templates.TEMPLATE_NAMES` and `OPTIONAL_PLACEHOLDERS` |
| Version strings | `pyproject.toml`, `src/prxref/__init__.py`, and `uv.lock` |
| Test command | `uv run pytest` (dev tools are a `[dependency-groups]` group, not an extra) |
| Release assets | wheel **and** sdist attached by `release.yml`; PyPI by OIDC trusted publishing |
