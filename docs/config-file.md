# Repository config file (`.prxref.toml`)

Most of prxref's settings can live in the repository instead of the pipeline.
Commit a `.prxref.toml` at the repository root, and every CI run in that
checkout reads it: the rules files, the model chain, the context level and the
comment policy travel with the code, and each team can differ without touching
a pipeline definition.

```toml
# .prxref.toml
review_rules = ".prxref/rules.md"
llm_models = ["z-ai/glm-5.3-flash", "openai/gpt-4o-mini"]
repo_context = "diff"
post_mode = "summary+inline"
max_inline_comments = 10
```

A complete, commented example is in
[docs/examples/prxref.toml](examples/prxref.toml). `prxref config check`
validates a file before you commit it; see [Validating](#validating).

The file is optional. With no `.prxref.toml` in the working directory, and none
named by `--config` or `PRXREF_CONFIG_FILE`, prxref behaves exactly as it did
before the file existed.

## Where it is read from

`prxref review` and `prxref eval run` look for the file in this order, and use
the first one that applies:

1. `--config PATH` names the file for one run.
2. `PRXREF_CONFIG_FILE` names it for every run of the process.
3. `.prxref.toml` in the **working directory**. Parent directories are never
   searched, so run prxref from the repository root.

The value `off` (in any case) in `--config` or `PRXREF_CONFIG_FILE` turns the
file off, and `--no-config` does the same for one run; `--config` and
`--no-config` cannot be combined. A path named by `--config` or
`PRXREF_CONFIG_FILE` must exist: a missing file is a configuration error
naming whichever one supplied it. An empty `PRXREF_CONFIG_FILE` reads as unset.

`prxref serve` never auto-discovers. The webhook daemon's working directory is
not a repository, so it reads a file only when `--config PATH` or
`PRXREF_CONFIG_FILE` names one (`off` reads none). A file given to the daemon
is checked by the same rules as a repository's, once before the daemon listens
(a missing or invalid file exits `2`), and it is read again for every webhook
review. `prxref eval score` reads no file: its judge takes every setting from
the environment.

The run record and `--format json` carry `config_file`, after `degraded`:
`null` when no file was read, otherwise the file's `path` (as its errors name
it, relative to the working directory when inside it), the `sha256` of its
bytes, and the sorted `keys` it set, including keys the environment or a flag
then overrode. A run can always be traced to the exact file that shaped it:

```json
"config_file": {"path": ".prxref.toml", "sha256": "<64 hex digits>", "keys": ["max_chunks", "post_mode"]}
```

With `-v` in text mode, `prxref review` also logs `config: <path> (<n> keys)`.

## Precedence

Each setting is taken from the highest layer that sets it:

```
built-in defaults  <  .prxref.toml  <  environment (PRXREF_*)  <  command-line flags
```

The pipeline therefore always has the last word: any key the file sets can be
overridden by the matching `PRXREF_` variable, and a flag overrides both.

A worked example, with this file committed:

```toml
max_chunks = 4
confidence_floor = 0.7
```

| Run | `max_chunks` | `confidence_floor` |
|---|---|---|
| `prxref review --pr-url ...` | `4` (file) | `0.7` (file) |
| `PRXREF_MAX_CHUNKS=6 prxref review --pr-url ...` | `6` (environment) | `0.7` (file) |
| `PRXREF_MAX_CHUNKS=6 prxref review --pr-url ... --max-chunks 10` | `10` (flag) | `0.7` (file) |
| `prxref review --pr-url ... --no-config` | `8` (default) | `0.6` (default) |

An error about a value from the file names the file and the key rather than an
environment variable, for example
`.prxref.toml: max_chunks: must be a finite number greater than 0, got 0`.

## Schema

The file is a **flat** TOML document, read with Python's standard `tomllib`.
Every key sits at the top level; a table (`[review]`) is an error.

- **Key names** are the environment variable names, lowercased, without the
  `PRXREF_` prefix: `PRXREF_MAX_CHUNKS` is `max_chunks`. Keys are
  case-sensitive, and an unknown key is an error that suggests the closest
  real one.
- **Types** are TOML types, not the strings the environment takes:

  | Type | Written as | Notes |
  |---|---|---|
  | integer | `max_chunks = 4` | A boolean is rejected. `llm_seed` also accepts the string `"off"`. |
  | number | `confidence_floor = 0.7` | An integer or a float. |
  | boolean | `group_findings = true` | `true` or `false` only; `1` and `"1"` are rejected. |
  | array of strings | `llm_models = ["model-a", "model-b"]` | A comma-separated string is rejected; use an array. |
  | string | `post_mode = "summary"` | Choice keys keep their vocabulary, checked as in the environment. |

  `llm_temperature` accepts a number or a quoted string (`llm_temperature = "0.2"`);
  the quoted form works with every prxref release that reads the file.
- **An empty value reads as unset**, exactly like an empty environment
  variable: `review_rules = ""` or `size_ignore_globs = []` leaves the key at
  its default. Blank entries inside an array are dropped.
- Range and vocabulary checks are the same as for the environment
  (`confidence_floor` must be within `[0.0, 1.0]`, `post_mode` must be one of
  its three values, and so on); see [docs/env-vars.md](env-vars.md).

## Keys a repository file can set

Every key below is documented in full, with its default and its checks, in the
[LLM & Pipeline table of docs/env-vars.md](env-vars.md#llm--pipeline), under
its `PRXREF_` name.

### Rules, prompts, ticket and spec

| Key | Type | Meaning |
|---|---|---|
| `review_rules` | string | Path to the team review-rules file ([more](env-vars.md#llm--pipeline)). |
| `review_rules_max_chars` | integer | Characters of the rules body kept in the prompt ([more](env-vars.md#llm--pipeline)). |
| `scoped_rules` | array of strings | Path-scoped rules files and directories ([more](env-vars.md#llm--pipeline)). |
| `scoped_rules_max_chars` | integer | Characters of scoped-rules text one review unit receives ([more](env-vars.md#llm--pipeline)). |
| `max_findings_per_rule` | integer | Most findings one team rule may produce; `0` turns the cap off ([more](env-vars.md#llm--pipeline)). |
| `prompts_dir` | string | Directory of replacement prompt templates ([more](env-vars.md#llm--pipeline)). |
| `ticket_context_file` | string | File holding the ticket this PR implements ([more](env-vars.md#llm--pipeline)). |
| `ticket_context_max_chars` | integer | Characters of ticket text kept in the prompt ([more](env-vars.md#llm--pipeline)). |
| `evidence_files` | array of strings | Execution evidence files to review against ([more](env-vars.md#llm--pipeline)). |
| `evidence_max_chunk_chars` | integer | Characters of evidence text one review unit receives ([more](env-vars.md#llm--pipeline)). |
| `spec_sources` | array of strings | Local spec files and directories to review against; no URLs here ([more](env-vars.md#llm--pipeline)). |
| `spec_max_chars` | integer | Raw characters kept per spec source ([more](env-vars.md#llm--pipeline)). |
| `spec_digest_tokens` | integer | Token budget of the spec digest in worker prompts ([more](env-vars.md#llm--pipeline)). |

### Model chain

| Key | Type | Meaning |
|---|---|---|
| `llm_models` | array of strings | Model fallback chain, cheapest first ([more](env-vars.md#llm--pipeline)). |
| `llm_reasoning_effort` | string | Reasoning effort sent to reasoning models ([more](env-vars.md#llm--pipeline)). |
| `llm_max_tokens` | integer | Completion-token budget of each worker call ([more](env-vars.md#llm--pipeline)). |
| `llm_timeout` | number | Per-model deadline in seconds ([more](env-vars.md#llm--pipeline)). |
| `llm_timeout_per_1k` | number | Deadline-scaling coefficient, seconds per 1k estimated input tokens, `openai-compat` only and applied only while `llm_timeout` is at its default ([more](env-vars.md#llm--pipeline)). |
| `llm_temperature` | number or string | Sampling temperature ([more](env-vars.md#llm--pipeline)). |
| `llm_seed` | integer or "off" | Sampling seed, or `"off"` to send none ([more](env-vars.md#llm--pipeline)). |
| `llm_cli_concurrency` | integer | CLI processes one `claude-cli` / `kiro-cli` client runs at once ([more](env-vars.md#llm--pipeline)). |
| `llm_parse_retries` | integer | Times an unparseable reply is re-sent ([more](env-vars.md#llm--pipeline)). |

### Context

| Key | Type | Meaning |
|---|---|---|
| `repo_context` | string | Repository context level: `off`, `diff` or `repo` ([more](env-vars.md#llm--pipeline)). |
| `repo_context_max_chars` | integer | Per-chunk character budget for repository context ([more](env-vars.md#llm--pipeline)). |
| `repo_context_max_reads` | integer | Uncached repository-context reads all chunks together may make in one run ([more](env-vars.md#llm--pipeline)). |
| `repo_context_max_chunk_reads` | integer | Uncached repository-context reads one chunk may make ([more](env-vars.md#llm--pipeline)). |
| `context_followup` | string | `on` re-sends a chunk once with a symbol it asked about ([more](env-vars.md#llm--pipeline)). |
| `context_contract_globs` | array of strings | Globs selecting contract files; replaces the built-in set ([more](env-vars.md#llm--pipeline)). |
| `context_exclude_globs` | array of strings | Globs never read for repository context, added to a fixed floor ([more](env-vars.md#llm--pipeline)). |
| `chunk_context_lines` | integer | Context lines kept around each change in a chunk ([more](env-vars.md#llm--pipeline)). |

### Comment policy

| Key | Type | Meaning |
|---|---|---|
| `post_mode` | string | What is posted: `summary+inline`, `summary` or `inline` ([more](env-vars.md#llm--pipeline)). |
| `post_verdict` | boolean | Keep the verdict stamp in the posted summary ([more](env-vars.md#llm--pipeline)). |
| `post_cost` | boolean | Append the run's cost to the attribution line ([more](env-vars.md#llm--pipeline)). |
| `severity_markers` | string | Replace finding glyphs, as `name=glyph` pairs, e.g. `"error=🔴,warning=🟡"` ([more](env-vars.md#llm--pipeline)). |
| `summary_bullet_separator` | string | Text between a summary bullet's location and its title, spaces kept, e.g. `": "` ([more](env-vars.md#llm--pipeline)). |
| `max_inline_comments` | integer | Most inline comments posted per review ([more](env-vars.md#llm--pipeline)). |
| `group_findings` | boolean | Fold findings that break one rule in one file into one comment ([more](env-vars.md#llm--pipeline)). |
| `suggestions` | string | `on` asks for applicable code suggestions ([more](env-vars.md#llm--pipeline)). |
| `incremental` | string | `on` re-reviews only the files changed since the last reviewed head ([more](env-vars.md#llm--pipeline)). |

### Limits

| Key | Type | Meaning |
|---|---|---|
| `confidence_floor` | number | Findings below this confidence are dropped ([more](env-vars.md#llm--pipeline)). |
| `max_error_findings` | integer | Cap on error-severity findings ([more](env-vars.md#llm--pipeline)). |
| `max_warning_findings` | integer | Cap on warning-severity findings ([more](env-vars.md#llm--pipeline)). |
| `max_outofscope_findings` | integer | Cap on `outofscope`-severity findings ([more](env-vars.md#llm--pipeline)). |
| `dedup_similarity` | number | Title similarity at which two findings on one line are merged ([more](env-vars.md#llm--pipeline)). |
| `max_chunks` | integer | Most diff chunks reviewed per PR ([more](env-vars.md#llm--pipeline)). |
| `chunk_token_budget` | integer | Approximate token budget per chunk ([more](env-vars.md#llm--pipeline)). |
| `chunk_max_files` | integer | Most files in one chunk ([more](env-vars.md#llm--pipeline)). |
| `max_workers` | integer | Parallel chunk-review workers ([more](env-vars.md#llm--pipeline)). |

### Everything else

| Key | Type | Meaning |
|---|---|---|
| `size_warn_lines` | integer | Changed-line threshold for the PR size advisory ([more](env-vars.md#llm--pipeline)). |
| `size_warn_files` | integer | Changed-file threshold for the PR size advisory ([more](env-vars.md#llm--pipeline)). |
| `size_ignore_globs` | array of strings | Extra globs left out of both size counts ([more](env-vars.md#llm--pipeline)). |

### PR metadata rules

Flat keys, not a `[metadata]` table — the file is flat, so a table is a configuration error.

| Key | Type | Meaning |
|---|---|---|
| `metadata_rules` | string | `on` runs the three deterministic PR-metadata checks below; `off` (the default) runs none ([more](env-vars.md#llm--pipeline)). |
| `branch_patterns` | array of strings | `type=regex` pairs: the source branch must match the PR type's pattern ([more](env-vars.md#llm--pipeline)). |
| `commit_reference` | string | Regex every non-merge commit subject must contain ([more](env-vars.md#llm--pipeline)). |
| `area_globs` | array of strings | `name=glob` pairs classifying diff paths into areas ([more](env-vars.md#llm--pipeline)). |
| `max_areas_per_pr` | integer | Most distinct areas a PR may touch before the area check flags it ([more](env-vars.md#llm--pipeline)). |
| `ci_wiring` | string | `on` flags a check the PR adds that no CI configuration file invokes; `off` (the default) reads nothing ([more](env-vars.md#llm--pipeline)). |
| `ci_wiring_globs` | array of strings | Globs selecting the CI configuration files the CI wiring check reads; replaces the built-in set ([more](env-vars.md#llm--pipeline)). |

## Settings a repository file cannot set

Whoever can change the repository controls this file, and on a pull request
that includes the PR's author. So a setting that could send a secret somewhere,
run a program, read or write a file outside the review's inputs, or move the
pipeline's exit code stays in the environment. Setting one of these in the file
is a configuration error that names the key and the `PRXREF_` variable to use
instead, even when the value is empty.

| Key | Reason | Why |
|---|---|---|
| `llm_api_key` | credential | The LLM endpoint's API key. |
| `jira_email` | credential | Jira account for ticket fetches. |
| `jira_api_token` | credential | Jira API token. |
| `bitbucket_token` | credential | Bitbucket Cloud token. |
| `bitbucket_user` | credential | Bitbucket Cloud Basic-auth user. |
| `bitbucket_app_password` | credential | Bitbucket Cloud app password. |
| `bitbucket_server_token` | credential | Bitbucket Server / Data Center token. |
| `bitbucket_server_user` | credential | Bitbucket Server / Data Center Basic-auth user. |
| `bitbucket_server_password` | credential | Bitbucket Server / Data Center password. |
| `github_token` | credential | GitHub token. |
| `github_enterprise_token` | credential | GitHub Enterprise Server token. |
| `gitlab_token` | credential | GitLab token. |
| `gitea_token` | credential | Gitea / Forgejo token. |
| `azure_devops_token` | credential | Azure DevOps personal access token. |
| `bitbucket_webhook_secret` | credential | Bitbucket webhook signing secret. |
| `github_webhook_secret` | credential | GitHub webhook signing secret. |
| `gitlab_webhook_secret` | credential | GitLab webhook secret token. |
| `gitea_webhook_secret` | credential | Gitea / Forgejo webhook signing secret. |
| `azure_devops_webhook_secret` | credential | Azure DevOps service-hook secret. |
| `llm_base_url` | endpoint | The host the diff and the prompts are sent to. |
| `jira_base_url` | endpoint | The host Jira credentials are sent to. |
| `llm_backend` | executable | Can select `claude-cli` or `kiro-cli`, which run a local binary, and picks where the diff goes. |
| `llm_cli_path` | executable | The path of a binary prxref runs. |
| `trace_file` | local write | Appends a JSONL trace at a chosen path. |
| `trace_dir` | local write | Writes prompt and response files into a chosen directory. |
| `fallback` | local write | `auto` writes `gl-code-quality-report.json` into the working directory and emits CI annotations. |
| `price_table` | local read | Its value may be a path to any JSON file on the runner. |
| `fail_on` | gate | The pipeline's exit-code policy. |
| `dry_run` | gate | The switch that suppresses every forge write. |
| `allow_unsigned` | gate | The webhook signature safety switch. |

## Paths

The path keys (`review_rules`, each `scoped_rules` entry, `prompts_dir`,
`ticket_context_file` and each `spec_sources` entry) are read **relative to
the directory holding the config file**, not the working directory. So
`review_rules = ".prxref/rules.md"` in a root `.prxref.toml` names the same
file whichever directory prxref runs from, and a file named by
`--config /tmp/base/.prxref.toml` reads `/tmp/base/.prxref/rules.md`.

Every path must stay inside that directory:

- an absolute path (`/etc/prxref/rules.md`) is rejected;
- a home-directory path (`~/rules.md`) is rejected;
- a path that resolves outside the directory is rejected, whether through
  `..` or through a symlink, because symlinks are followed before the check.

`spec_sources` in the file takes local paths only. Web pages and Jira tickets
carry a host, so they stay in `PRXREF_SPEC_SOURCES` or `--spec`.

The glob keys (`size_ignore_globs`, `context_contract_globs`,
`context_exclude_globs`) are patterns matched against diff paths, not files,
so they are used as written.

## Errors

Every problem with the file is a configuration error: `prxref review` exits
`2` before any network call, and the message names the file and the key and
links this page. For a `.prxref.toml` in the working directory:

| The file holds | prxref prints |
|---|---|
| `max_chunk = 4` | `configuration error: .prxref.toml: unknown key 'max_chunk'; did you mean 'max_chunks'? see https://github.com/sblattj/prxref/blob/main/docs/config-file.md` |
| `colour = "red"` | `configuration error: .prxref.toml: unknown key 'colour'; see https://github.com/sblattj/prxref/blob/main/docs/config-file.md` |
| `llm_base_url = "https://llm.example.com/v1"` | `configuration error: .prxref.toml: 'llm_base_url' cannot be set in a repository config file (endpoint); set PRXREF_LLM_BASE_URL in the pipeline instead; see https://github.com/sblattj/prxref/blob/main/docs/config-file.md` |
| `max_chunks = "4"` | `configuration error: .prxref.toml: 'max_chunks' must be an integer, got a string; see https://github.com/sblattj/prxref/blob/main/docs/config-file.md` |
| `llm_models = "model-a, model-b"` | `configuration error: .prxref.toml: 'llm_models' must be an array of strings, got a string; see https://github.com/sblattj/prxref/blob/main/docs/config-file.md` |
| `group_findings = 1` | `configuration error: .prxref.toml: 'group_findings' must be a boolean, got an integer; see https://github.com/sblattj/prxref/blob/main/docs/config-file.md` |
| `[review]` then `max_chunks = 4` | `configuration error: .prxref.toml: 'review' is a table, but the config file is flat; write each key at the top level; see https://github.com/sblattj/prxref/blob/main/docs/config-file.md` |
| `spec_sources = ["https://example.com/spec"]` | `configuration error: .prxref.toml: 'spec_sources' entry 'https://example.com/spec' is a URL; a repository config file lists local paths only, so set PRXREF_SPEC_SOURCES in the pipeline for web and Jira sources; see https://github.com/sblattj/prxref/blob/main/docs/config-file.md` |
| `review_rules = "/etc/prxref/rules.md"` | `configuration error: .prxref.toml: 'review_rules' path '/etc/prxref/rules.md' must stay inside the repository (an absolute path); use a path relative to the config file's directory; see https://github.com/sblattj/prxref/blob/main/docs/config-file.md` |
| `review_rules = "~/rules.md"` | `configuration error: .prxref.toml: 'review_rules' path '~/rules.md' must stay inside the repository (a home-directory path); use a path relative to the config file's directory; see https://github.com/sblattj/prxref/blob/main/docs/config-file.md` |
| `review_rules = "../rules.md"` | `configuration error: .prxref.toml: 'review_rules' path '../rules.md' must stay inside the repository (it resolves outside the config file's directory); use a path relative to the config file's directory; see https://github.com/sblattj/prxref/blob/main/docs/config-file.md` |
| `max_chunks = ` (no value) | `configuration error: .prxref.toml: invalid TOML: Invalid value (at line 1, column 14); see https://github.com/sblattj/prxref/blob/main/docs/config-file.md` |
| a byte that is not UTF-8 | `configuration error: .prxref.toml: not valid UTF-8 (byte 17); save the file as UTF-8; see https://github.com/sblattj/prxref/blob/main/docs/config-file.md` |
| `max_chunks = 0` | `configuration error: .prxref.toml: max_chunks: must be a finite number greater than 0, got 0` |

A file that exists but cannot be read reports
`<file>: cannot read config file: <reason>`. A missing file named by the flag
or the variable reports `--config: config file not found: <path>` or
`PRXREF_CONFIG_FILE: config file not found: <path>`.

## Validating

```bash
prxref config check                       # the ./.prxref.toml, if there is one
prxref config check --config ci/prxref.toml
prxref config check --format json
```

`prxref config check` loads the file and the whole environment exactly as a
review would, without reading a pull request or calling a model. It prints
the file it read (or `none`), then every setting, sorted by key, with its value
and where the value came from: `default`, `file`, or `env PRXREF_<NAME>` for
the variable that set it. The last line is `ok`. Credentials and webhook
secrets print as `<set>` or `<unset>`, never their value. With this file and
`PRXREF_POST_MODE=summary` and `PRXREF_GITHUB_TOKEN` set in the environment:

```toml
max_chunks = 4
llm_temperature = 0.2
review_rules = ".prxref/rules.md"
post_mode = "summary+inline"
```

the output reads, with most of its 78 lines left out here:

```text
config file: .prxref.toml
allow_unsigned = False  (default)
azure_devops_token = <unset>  (default)
...
github_token = <set>  (env PRXREF_GITHUB_TOKEN)
...
llm_temperature = 0.2  (file)
...
max_chunks = 4  (file)
...
post_mode = summary  (env PRXREF_POST_MODE)
...
review_rules = /home/ci/repo/.prxref/rules.md  (file)
...
ok
```

A path key prints as the absolute path it resolved to. `config check` checks
that each path stays inside the repository, then opens the rules file, the
scoped rules, the ticket-context file and the prompts directory exactly as
`review` does. One that is missing or fails its own checks exits `2` with the
line the review would print, naming the key as `.prxref.toml: review_rules`
when the file set the path, or the variable or flag that did.
`spec_sources` entries are not opened: a review reads them best-effort and
records a failed one rather than stopping.

`--format json` prints the same as one line of JSON:
`{"config_file": ".prxref.toml", "values": {"allow_unsigned": {"value": false, "source": "default"}, ...}}`,
with `config_file` `null` when no file was read. It exits `0` when
everything is valid and `2`, with the same `configuration error: ...` line a
review would print on stderr, when anything is not; with `--format json` an
error leaves stdout empty. `--config PATH` and `--no-config` work as they do
for `review`.

Run it in the pipeline step before the review, or as a pre-commit hook, so a
broken file fails in the change that broke it.

## Security: which copy of the file does CI read?

In CI the workspace is usually the pull request's own code: a GitHub Actions
`pull_request` workflow checks out the PR's merge commit, a GitLab
merge-request pipeline runs on the MR's source branch, and an Azure DevOps
build-validation pipeline checks out the PR's merge commit. Auto-discovery
then reads **the PR's copy** of `.prxref.toml`, so the PR's author chooses the
settings their own change is reviewed with.

What that author **cannot** reach through the file: credentials, endpoints,
executables, local reads and writes, and the gate. Those keys are rejected
([the list](#settings-a-repository-file-cannot-set)), and every path must stay
inside the repository ([Paths](#paths)).

What the author **can** do is relax the review: lower the caps
(`max_error_findings = 0`), raise `confidence_floor` to `1.0`, point
`review_rules`, `scoped_rules` or `prompts_dir` at the PR's own copies,
switch `post_mode` to `inline`, or pick a weaker model in `llm_models`. On an
advisory lane that is the same trust you already extend to the PR's code. On
a lane where the review gates the merge (`PRXREF_FAIL_ON` set to `error` or
`any`), or one running under `pull_request_target` with a write token, it is
not: read the file from something the PR cannot change.

- **The target branch, with plain git.** Fetch the branch the PR merges into
  and extract the file, together with the files its paths name, from it.
  Paths resolve against the file's directory, so extracting `.prxref` beside
  it keeps `review_rules = ".prxref/rules.md"` pointing at the target
  branch's copy:

  ```yaml
  - name: Read .prxref.toml from the target branch
    run: |
      git fetch --depth=1 origin "${{ github.base_ref }}"
      mkdir -p "$RUNNER_TEMP/base"
      git archive FETCH_HEAD .prxref.toml .prxref | tar -x -f - -C "$RUNNER_TEMP/base"
  - name: Run prxref review
    run: prxref review --pr-url "${{ github.event.pull_request.html_url }}" --config "$RUNNER_TEMP/base/.prxref.toml"
  ```

  List only paths that exist on the target branch; `git archive` fails on a
  missing one. For a file that names no paths, copying the one file is
  enough. Do not copy a file that names paths this way: its paths would then
  resolve beside the copy, where nothing was extracted. `prxref config check`
  catches this: it opens those paths and exits `2`, naming the file key
  (`.prxref.toml: review_rules`).

  ```bash
  git fetch --depth=1 origin "${{ github.base_ref }}"
  git show FETCH_HEAD:.prxref.toml > "$RUNNER_TEMP/prxref.toml"
  prxref review --pr-url "$PR_URL" --config "$RUNNER_TEMP/prxref.toml"
  ```

  The target branch is `${{ github.base_ref }}` on GitHub Actions,
  `$CI_MERGE_REQUEST_TARGET_BRANCH_NAME` on GitLab CI, and
  `$BITBUCKET_PR_DESTINATION_BRANCH` on Bitbucket Pipelines; see
  [review-rules.md](review-rules.md#ci-safety-read-the-rules-from-something-the-pr-cannot-change).
- **Check out the base, not the PR.** prxref reads the diff over the forge
  API, so it never needs the PR's code on disk. A job that checks out only the
  target branch, as the repository's own `pull_request_target` workflow
  (`.github/workflows/prxref-review.yml`) does, auto-discovers the target
  branch's `.prxref.toml`.
- **Turn it off.** `PRXREF_CONFIG_FILE=off` (or `--no-config`) in a gated lane
  ignores any file, and the pipeline's environment is the whole configuration.

**The residual risk.** A PR that can edit the pipeline definition can change
anything the pipeline does, which config file it reads included. Protect the
pipeline files with required review (for example `CODEOWNERS`), or run prxref
as the webhook daemon, which reads no repository's file.
