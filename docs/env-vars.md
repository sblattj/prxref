# Environment Variables

All environment variables used by `prxref` are prefixed with `PRXREF_`.

Configuration is loaded from built-in defaults, overridden by environment variables, and further overridden by explicit programmatic keyword arguments (the CLI's `--max-chunks` is one). An empty or whitespace-only value reads as **unset**, so a stray `PRXREF_LLM_TIMEOUT=` in a `.env` file keeps the default instead of aborting.

## Variable Reference

### LLM & Pipeline

| Variable | Default | Purpose |
|---|---|---|
| `PRXREF_LLM_BACKEND` | `openai-compat` | LLM backend selector: `openai-compat`, `ferry`, or `http` (aliases for the plain-HTTP OpenAI-compatible client), `litellm` (in-process LiteLLM router), or `claude-cli` / `kiro-cli` (an already-installed, already-logged-in coding CLI run as a subprocess — see [docs/llm.md](llm.md#subscription-cli-backends-claude-cli-and-kiro-cli)). Read case-insensitively; any other value raises `ConfigError` and `prxref review` exits `2`. |
| `PRXREF_LLM_BASE_URL` | *(none)* | Base URL for the OpenAI-compatible endpoint (e.g. `https://openrouter.ai/api/v1`). **Required** for `openai-compat`/`ferry`/`http`: unset raises `ConfigError` and `prxref review` exits `2`. **Not used** by `litellm` (it resolves each model's own provider endpoint), `claude-cli` or `kiro-cli`; a value set there is ignored with one INFO line. A LiteLLM proxy is OpenAI-compatible, so point `openai-compat` at it. |
| `PRXREF_LLM_API_KEY` | *(empty)* | API key / Bearer token sent to the OpenAI-compatible endpoint (`openai-compat` only). Optional: leave empty for a local no-auth server. |
| `PRXREF_LLM_MODELS` | *(none — required)* | Model fallback chain evaluated in order, cheapest first, comma- **or** whitespace-separated. First model that answers successfully wins. Required by every backend: unset raises `ConfigError` and `prxref review` exits `2`. |
| `PRXREF_LLM_REASONING_EFFORT` | *(empty)* | Reasoning effort for models that cannot disable reasoning (e.g. `low`\|`high`\|`max` for GLM-5.3-Flash). Sent as `reasoning_effort` in the request by `openai-compat`/`ferry`/`http` and as `--effort <value>` by `claude-cli`; `litellm` does not apply it, and `kiro-cli` drops it with one INFO line (see [docs/llm.md](llm.md#what-is-not-applied)). Empty omits the parameter entirely from the request. Provider-specific vocabulary; not validated client-side. Raising it makes truncation more likely — see [Reasoning models and the token budget](#reasoning-models-and-the-token-budget). |
| `PRXREF_LLM_MAX_TOKENS` | `4096` | Completion-token budget (`max_tokens`) for each worker's review call on `openai-compat`/`ferry`/`http` and `litellm`; `claude-cli` and `kiro-cli` accept it and do not apply it (see [docs/llm.md](llm.md#what-is-not-applied)). Must be **greater than 0**. Too small and the model runs out of budget mid-JSON: that chunk is counted as failed and the summary says so. |
| `PRXREF_LLM_TIMEOUT` | `45.0` | Wall-clock deadline in seconds for one model's review call. Must be **greater than 0**. A model that runs past it is abandoned for the next one in the chain, so this is a per-model deadline, not a per-review one: a three-model chain can spend three times this value before the chunk is given up on. Under the default `openai-compat`/`ferry`/`http` backend the deadline is enforced client-side against elapsed time, including the response body — an endpoint that trickles bytes cannot outlast it. Under `litellm` the value is handed to that library, and its own timeout semantics apply. Overridable per run with `--timeout SECONDS`; the flag wins for that invocation only, and a bad value is reported as `--timeout`. |
| `PRXREF_LLM_TEMPERATURE` | `0.0` (sent) | Sampling temperature, e.g. `0.2`. Must be **finite and >= 0**; there is no upper bound, because the maximum is provider-specific. Unset or empty sends the default `0.0` rather than omitting the parameter, so an identical diff reviews identically by default; a set value wins and restores provider-default sampling. That holds on `openai-compat`/`ferry`/`http` and `litellm`; `claude-cli` and `kiro-cli` send no temperature, log one WARNING when it is set, and report `sampling.temperature` as `null`. |
| `PRXREF_LLM_SEED` | *(auto-derived)* | Optional integer sampling seed, sent as top-level `seed` to OpenAI-compatible backends (under `litellm` too). Must be **>= 0**; `0` is a valid seed. Empty or unset does not omit the parameter: one random seed is derived per process and sent on every call of the run, and the run record's `sampling.seed` reports it. `claude-cli` and `kiro-cli` send no seed, log one WARNING when it is set, and report `sampling.seed` as `null`. Together with the default `PRXREF_LLM_TEMPERATURE=0` this is the strongest reproducibility lever the API offers. |
| `PRXREF_LLM_CLI_PATH` | *(empty — found on `PATH`)* | `claude-cli` / `kiro-cli` only: the CLI binary to run (`~` is expanded). Empty looks up `claude` or `kiro-cli` on `PATH`. A path that does not resolve to an executable raises `ConfigError` and `prxref review` exits `2`. Ignored by the other backends. |
| `PRXREF_LLM_CLI_CONCURRENCY` | `2` | `claude-cli` / `kiro-cli` only: how many CLI processes one client may run at once. Must be **greater than 0**. Each call is a full CLI process, and subscription limits are per account. |
| `PRXREF_CONFIDENCE_FLOOR` | `0.6` | Minimum confidence score. Must be **within `[0.0, 1.0]` inclusive** — it is a probability everywhere in the pipeline. Findings below the floor are dropped. |
| `PRXREF_MAX_ERROR_FINDINGS` | `10` | Maximum number of error-severity findings reported per review. Excess errors are dropped lowest-confidence-first. Must be **>= 0**; `0` is legal and caps every error. (Legacy alias: `PRXREF_MAX_ERRORS`.) |
| `PRXREF_MAX_WARNING_FINDINGS` | *(empty — unlimited)* | **Per-severity caps (0.15.0).** Maximum number of warning-severity findings reported per review. Excess warnings are dropped lowest-confidence-first, with `drop_reason` `warning cap exceeded (max N)`. Must be **>= 0**; `0` is legal and caps every warning, distinct from unset, which caps nothing. |
| `PRXREF_MAX_OUTOFSCOPE_FINDINGS` | *(empty — unlimited)* | **Per-severity caps (0.15.0).** The same cap for the minor tier: findings of severity `outofscope`, dropped past the cap with `outofscope cap exceeded (max N)`. `outofscope` is a finding **severity**; it is **not** ticket scope `out` from `PRXREF_TICKET_CONTEXT_FILE`, so a finding marked out of the ticket's scope counts against its own severity's cap, not this one. `spec` findings are never capped. Must be **>= 0**; unset caps nothing. |
| `PRXREF_GROUP_FINDINGS` | `False` | **Finding grouping (0.15.0).** Set to the literal `1` to fold chunk findings that break the same rule in the same file into one comment: the model is asked to name the rule it applied, and findings without a rule are grouped by normalized title. The comment sits at the group's first line with its highest severity and confidence and lists the other locations as `Also at: …`; the other members are dropped with `grouped into <file>:<line>`. Grouping runs before the caps, so `PRXREF_MAX_ERROR_FINDINGS` counts groups, not lines. Whole-PR sweep findings are never grouped. Off by default, which keeps the prompts and the output unchanged. |
| `PRXREF_DEDUP_SIMILARITY` | *(empty — off)* | **Reworded-duplicate dedup (0.15.0).** Title similarity at or above which two findings in the same file and on the same line are treated as one: Jaccard over a dedicated title tokenizer, and the titles must also share at least 3 tokens. A chunk copy is kept over a sweep copy of equal or lower severity; within one side the more severe, then the more confident, finding is kept. The dropped one reads `duplicate of <chunk\|sweep> finding (reworded, similarity S)`. Must be **greater than 0 and at most 1.0**. Unset leaves only the exact-title dedup running, so output is unchanged; the threshold is not yet tuned on real review data. No CLI flag. |
| `PRXREF_MAX_CHUNKS` | `8` | Maximum number of diff chunks reviewed per PR. Must be **greater than 0**. Overridable per run with `--max-chunks`. |
| `PRXREF_CHUNK_TOKEN_BUDGET` | `25000` | Approximate token budget per diff chunk. Must be **greater than 0**. Lowering it splits a PR into more, smaller chunks: more LLM calls, but less diff per call. Chunk count is still capped by `PRXREF_MAX_CHUNKS`, and overflow past that cap lands in the smallest existing chunk rather than opening a new one. |
| `PRXREF_CHUNK_MAX_FILES` | `5` | Maximum files placed in one review chunk. Must be **greater than 0**. Chunks stay under the cap while any chunk has room; once `PRXREF_MAX_CHUNKS` is reached and every chunk is full, overflow files join the smallest chunk past the cap rather than being dropped from review. |
| `PRXREF_CHUNK_CONTEXT_LINES` | `3` | Context lines kept around each change when a chunk's diff is rendered into the worker prompt. Must be **>= 0**; `0` emits the changed lines only. The forge's diff is the only source of context — prxref trims what it received and never adds what it did not. |
| `PRXREF_MAX_WORKERS` | `4` | Parallel chunk-review workers. Must be **greater than 0**. The cap that matters is usually the endpoint's rate limit, not the machine. |
| `PRXREF_MAX_INLINE_COMMENTS` | `15` | Maximum inline comments posted per review, applied **after** the quality gate. Must be **greater than 0**. Findings past the cap are still listed in the summary comment; only the inline posting is trimmed. |
| `PRXREF_TRACE_FILE` | *(empty — off)* | Path to append a JSONL run trace to. Empty disables tracing and the tracer becomes a no-op, so there is no cost when unset. One event per line (`run`, `forge.get_pr`, `forge.get_diff`, `parse_diff`, `build_chunks`, `chunk`, `heartbeat`, `post`), flushed as it happens. Each carries a phase: `start`, then `ok` or `fail`; `post` also uses `skip`, so a stage nobody asked to run is distinguishable from one the run never reached — a run still in flight, or one that was killed mid-hang, is as readable as a completed one. Render it to a standalone HTML pipeline view with `prxref trace render <file>`. |
| `PRXREF_TRACE_DIR` | *(empty — off)* | Directory for per-unit prompt/response traces. Each review unit (`chunk0`, `chunk1`, … and the whole-PR `sweep`) writes four files there: `<unit>.system.md` and `<unit>.user.md` (the exact rendered prompt halves), `<unit>.response.json` (the raw model text, JSON-encoded), and `<unit>.meta.json` (`unit`, `model`, token counts, `elapsed_ms`, `error`, `cost_usd` — the dollar figure the backend reported for the call, `null` when it reported none — and `cost_source`, where that figure came from, `""` when `cost_usd` is `null`). Unset disables the dump entirely — no directory is created and there is no cost. Writes are best-effort: a failure is a logged warning, never a review failure, and a timeout retry overwrites the unit's files so the trace shows the attempt whose result was used. `--trace-dir DIR` on `prxref review` is the per-run equivalent and wins when both are set. |
| `PRXREF_DRY_RUN` | `False` | Set to the literal `1` to run the full review and write nothing to the forge — no summary, no inline comments. Applies to the webhook daemon as well as the CLI, which is the only way to watch the daemon against a real repository before letting it comment. `--no-post` is the per-invocation equivalent and still wins when the environment says nothing. Only the literal `1` enables it. |
| `PRXREF_FAIL_ON` | `never` | Exit-code policy for `prxref review`. `never` (the default) keeps the advisory contract — the exit code never reflects findings. `error` exits `1` when the completed review carries an active error-severity finding; `any` exits `1` on any active finding. Under either value a review that does not complete also exits `1` — it crashes, or it ends with verdict `Error` (the forge could not be read, the diff could not be parsed or chunked, or every chunk review failed) — so a gating lane cannot read a broken run as green. An empty PR diff is not a failure (verdict `Approved`, exit `0`). The webhook daemon has no exit code and is unaffected. See [Bad Configuration Is the Only Thing That Fails a Build](#bad-configuration-is-the-only-thing-that-fails-a-build). |
| `PRXREF_POST_MODE` | `summary+inline` | What gets posted to the forge: `summary+inline` (the summary comment, then inline comments only if the summary landed), `summary` (the summary comment only — inline comments are never posted), or `inline` (inline comments only — no summary is posted on any path, including the error notice). Any other value raises `ConfigError` and `prxref review` exits `2`. A dry run posts nothing in any mode. |
| `PRXREF_POST_VERDICT` | `True` | Set to the literal `1` to keep the verdict stamp in the posted summary; any other value renders the summary without it (no `Approved` / `Request-Changes` heading), keeping the findings, counts, and attribution. The computed verdict printed to stdout and the total-failure notice are unaffected. |
| `PRXREF_PRICE_TABLE` | *(empty — no estimates)* | Fallback price table, used only when the backend reports no dollar cost of its own. Inline JSON (starting with `{`) or a path to a JSON file, e.g. `{"openai/gpt-4o-mini": {"input": 0.15, "output": 0.60}}`: USD per **million** tokens, keyed on the exact model name the run reports (`model=` in the attribution). Every entry is exactly `{"input": n, "output": n}` with `n` a finite number `>= 0`. A malformed table raises `ConfigError` and `prxref review` exits `2`. A run priced from the table is flagged `cost_estimated`; a model with neither a reported cost nor an entry reads "cost unknown", never `$0` — give free or local models a zero entry. See [Cost accounting](llm.md#cost-accounting). |
| `PRXREF_POST_COST` | `False` | Set to the literal `1` to append the run's cost as the last field of the posted attribution line (`… · 3.1s · $0.0007`, `~$0.0007 (est.)` when estimated, else `$0.0007 (API-equivalent)` when every reported cost came from `claude-cli`, `cost unknown` when neither source priced it). Off by default, which keeps the attribution line byte-identical; the cost is always in the run record and the JSON output. See [Cost accounting](llm.md#cost-accounting). |
| `PRXREF_SIZE_WARN_LINES` | *(empty — off)* | Advisory-only threshold on lines changed (added plus removed, excluding lock and generated files): a PR strictly above it gets one non-blocking heads-up line at the top of the summary. Must be **>= 0**; `0` is a legal, extreme threshold (any change at all), distinct from unset, which disables the check. Never affects the verdict or the exit code. |
| `PRXREF_SIZE_WARN_FILES` | *(empty — off)* | Same contract as `PRXREF_SIZE_WARN_LINES`, counting changed files instead of lines. |
| `PRXREF_SIZE_IGNORE_GLOBS` | *(empty)* | Extra glob patterns excluded from both size counts, **added** to the built-in lock/generated-file detection. Matched case-sensitively (`fnmatch`) against the full diff path, where `*` crosses `/`. Comma- or whitespace-separated, so a literal space in a glob is written `?`. |
| `PRXREF_SPEC_SOURCES` | *(empty)* | Spec/ticket sources to review against: public web URLs, local file or directory paths, or Jira ticket URLs. Comma- **or** whitespace-separated when set through the environment. The repeatable `--spec` flag replaces this list entirely when given — there is no merge. In CI, a local path inside the PR's own checkout is content the PR controls; point it outside the workspace when the spec must be trusted. |
| `PRXREF_SPEC_MAX_CHARS` | `120000` | Raw fetched characters kept per spec source (after decoding), before pruning. Must be **greater than 0**. Truncation at the cap is announced in the fetched text, never silent. |
| `PRXREF_SPEC_DIGEST_TOKENS` | `3000` | Token budget for the final spec-constraint digest injected into worker prompts (estimated at 4 characters per token, like the systemic digest). Must be **greater than 0**. |
| `PRXREF_REVIEW_RULES` | *(empty — off)* | Path to a team review-rules file (Markdown, with optional front matter carrying a `severity:` map) added to every review prompt. A missing, unreadable or malformed file raises `ConfigError` naming its source and `prxref review` exits `2`. `--rules-file PATH` wins for one run, and `--rules-file ""` turns the file off. Read it from a trusted checkout: in CI the workspace is usually the PR's own code, so a rules file inside it lets the PR rewrite its own review rules. See [docs/review-rules.md](review-rules.md). |
| `PRXREF_REVIEW_RULES_MAX_CHARS` | `12000` | Characters of the rules body (after the front matter) kept in the prompt; a longer body is truncated with a warning. Must be **greater than 0**. |
| `PRXREF_SCOPED_RULES` | *(empty — off)* | **Path-scoped review rules (0.15.0).** Rules files and directories (each directory scanned one level deep for `*.md`, in name order, at most 50 files) whose front-matter `applies_to:` globs (alias `applyTo`) decide which review units get each file: a chunk gets every file matching one of its paths, and the whole-PR sweep gets the union. Globs match like `PRXREF_SIZE_IGNORE_GLOBS` (case-sensitive `fnmatch`, `*` crosses `/`), plus a `!` pattern that excludes, and a `**/` that also matches zero directories (for this key only). A file without `applies_to` reaches every unit; a URL, a path escaping the working directory, `applies_to: []`, a leading `/`, or two files mapping one word to different severity tiers raises `ConfigError` and `prxref review` exits `2`. Each file is capped at `PRXREF_REVIEW_RULES_MAX_CHARS`. Added to `PRXREF_REVIEW_RULES`, never replacing it. Comma- or whitespace-separated; the repeatable `--scoped-rules PATH` flag replaces this list for one run and can name a path containing a space. Read it from a trusted checkout, as with `PRXREF_REVIEW_RULES`. Unset leaves the prompts unchanged. |
| `PRXREF_SCOPED_RULES_MAX_CHARS` | `24000` | **Path-scoped review rules (0.15.0).** Characters of scoped-rules text one review unit (a chunk or the sweep) receives: whole files in name order, the first that does not fit is truncated with an omission marker, and one warning per run names this variable. Must be **greater than 0**. |
| `PRXREF_PROMPTS_DIR` | *(empty — off)* | **Prompt template overrides (0.15.0).** Directory holding replacement `worker.md`, `systemic.md` and `summary.md` prompt templates; a file that is absent keeps the packaged one. Every template is validated before any network call: `worker.md` and `systemic.md` must keep the `## Review Context` marker and every packaged placeholder below it (the feature slots `{scope_example}` and `{rule_example}` are optional), and `summary.md` needs only `{findings}`. A missing directory, a failed check, or a template over 256 KiB raises `ConfigError` and `prxref review` exits `2`; an empty directory or an unknown placeholder only warns. The run record stamps each template's source and SHA-256. `--prompts-dir DIR` wins for one run, and `prxref prompts export DIR [--force]` writes the packaged templates to start from. The judge prompt of `prxref eval` cannot be overridden. Read it from a trusted checkout: a PR that commits a template rewrites its own review. Unset uses the packaged templates, byte for byte. |
| `PRXREF_TICKET_CONTEXT_FILE` | *(empty — off)* | Plain-text or Markdown file holding the ticket this PR implements. When set, every finding is marked in, out of, or of unknown ticket scope. An empty or whitespace-only file means "this PR has no ticket". A missing or non-UTF-8 file raises `ConfigError` and `prxref review` exits `2`. The webhook daemon ignores it (and says so once). `--context-file PATH` wins for one run, and `--context-file ""` turns it off. |
| `PRXREF_TICKET_CONTEXT_MAX_CHARS` | `6000` | Characters of ticket text kept in the prompt; longer text is truncated with a visible marker. Must be **greater than 0**. |

The replay flags of `prxref review` (`--base-sha`, `--head-sha`, `--no-threads`, `--diff-file`) deliberately have no environment variable: set in the environment, a replay pin would silently pin every run, the webhook daemon's included.

### Per-Forge Authentication

| Variable | Default | Purpose |
|---|---|---|
| `PRXREF_BITBUCKET_TOKEN` | *(empty)* | Bitbucket **Cloud** workspace/repository Bearer access token. Preferred over basic authentication. For Bitbucket Server / Data Center use `PRXREF_BITBUCKET_SERVER_TOKEN` below — see [docs/forges.md](forges.md). |
| `PRXREF_BITBUCKET_USER` | *(empty)* | Bitbucket Cloud username for HTTP Basic authentication (used with `PRXREF_BITBUCKET_APP_PASSWORD`). |
| `PRXREF_BITBUCKET_APP_PASSWORD` | *(empty)* | Bitbucket Cloud app password for HTTP Basic authentication. |
| `PRXREF_BITBUCKET_SERVER_TOKEN` | *(empty)* | Bitbucket Server / Data Center HTTP access token (sent as `Bearer`). Falls back to `PRXREF_BITBUCKET_TOKEN` if unset. |
| `PRXREF_BITBUCKET_SERVER_USER` | *(empty)* | Bitbucket Server / Data Center username for HTTP Basic authentication (used with `PRXREF_BITBUCKET_SERVER_PASSWORD`). |
| `PRXREF_BITBUCKET_SERVER_PASSWORD` | *(empty)* | Bitbucket Server / Data Center password for HTTP Basic authentication. |
| `PRXREF_GITHUB_TOKEN` | *(empty)* | GitHub Personal Access Token or GitHub App token for `github.com`. |
| `PRXREF_GITHUB_ENTERPRISE_TOKEN` | *(empty)* | GitHub Enterprise token for custom/self-hosted GitHub Enterprise Server domains. Falls back to `PRXREF_GITHUB_TOKEN` if unset. |
| `PRXREF_GITLAB_TOKEN` | *(empty)* | GitLab Personal, Project, or Group Access Token (sent via `PRIVATE-TOKEN` header) for `gitlab.com` or self-hosted GitLab. |
| `PRXREF_AZURE_DEVOPS_TOKEN` | *(empty)* | Azure DevOps personal access token, sent as Basic `:PAT`: **Code (Read)** to review, **Code (Read & write)** to post. When empty, the Pipelines `SYSTEM_ACCESSTOKEN` is sent as a Bearer token; when both are empty, requests are anonymous (public projects, read-only). |

### Spec Sources / Jira

| Variable | Default | Purpose |
|---|---|---|
| `PRXREF_JIRA_BASE_URL` | *(empty)* | Jira base URL — `scheme://host` plus any context path (e.g. `https://jira.example.com/jira`) — that ticket fetches are looked up on, overriding a ticket URL's own base; a self-hosted board often sits behind a different REST host than its browse URL. **Jira credentials are only ever sent here.** Empty resolves the ticket URL's own base, anonymously. An `http://` base with credentials is allowed but logs a warning. |
| `PRXREF_JIRA_EMAIL` | *(empty)* | Jira account email for HTTP basic authentication when fetching a ticket, used together with `PRXREF_JIRA_API_TOKEN` and **only** together with `PRXREF_JIRA_BASE_URL`: credentials set without a base URL are never sent — the fetch stays anonymous and a warning is logged. When either credential is empty the fetch is anonymous, which public boards accept. |
| `PRXREF_JIRA_API_TOKEN` | *(empty)* | Jira API token for HTTP basic authentication, paired with `PRXREF_JIRA_EMAIL` and sent only to `PRXREF_JIRA_BASE_URL`. Missing credentials are a fetch failure (the review proceeds un-grounded with a note naming these variables), not a configuration error. |

### Webhook Receiver

| Variable | Default | Purpose |
|---|---|---|
| `PRXREF_BITBUCKET_WEBHOOK_SECRET` | *(empty)* | HMAC secret for Bitbucket webhooks, Cloud and Server alike (verified against `X-Hub-Signature` via HMAC-SHA256). |
| `PRXREF_GITHUB_WEBHOOK_SECRET` | *(empty)* | HMAC secret for GitHub webhooks (verified against `X-Hub-Signature-256` via HMAC-SHA256). |
| `PRXREF_GITLAB_WEBHOOK_SECRET` | *(empty)* | Secret token for GitLab webhooks (verified against `X-Gitlab-Token`). |
| `PRXREF_AZURE_DEVOPS_WEBHOOK_SECRET` | *(empty)* | Secret for Azure DevOps service hooks, compared in constant time with the **password** of the hook's Basic authentication (the user name is ignored). Empty rejects Azure DevOps webhooks with `401` unless `PRXREF_ALLOW_UNSIGNED` is `1`. |
| `PRXREF_ALLOW_UNSIGNED` | `False` | Accepts webhooks without valid HMAC/token signatures (dev/testing only; logs a warning). Must be the literal string `1` — `true`/`yes`/`on` deliberately do **not** enable the bypass, so it cannot be switched on by a stray truthy value. |

## Bad Configuration Is the Only Thing That Fails a Build

Under the default `PRXREF_FAIL_ON=never`, `prxref review` exits **0** on every review error — a network failure, an LLM timeout, bad forge credentials, even a review in which every chunk failed — and on an empty PR diff, which is not an error at all. prxref is an advisor, not a merge gate.

It exits **2** on exactly one class of problem: a **configuration error**. That is a required value missing, a value that will not parse, a value outside its valid range, or one outside its key's allowed vocabulary (`PRXREF_FAIL_ON` accepts only `never`, `error`, or `any`). The check runs after the environment *and* any programmatic override, so no path into the config can smuggle a degenerate value through to the wire.

The message names whichever input actually supplied the offending value:

```
$ PRXREF_MAX_CHUNKS=0 prxref review --pr-url https://github.com/org/repo/pull/1
configuration error: PRXREF_MAX_CHUNKS: must be a finite number greater than 0, got 0
$ prxref review --pr-url https://github.com/org/repo/pull/1 --max-chunks 0
configuration error: --max-chunks: must be a finite number greater than 0, got 0
```

The second form exists because naming the environment variable unconditionally sent operators hunting for a `PRXREF_MAX_CHUNKS` they had never set.

One knob can move the exit code beyond that: `PRXREF_FAIL_ON`. Its default `never` is everything above, unchanged. Setting it to `error` exits **1** when the completed review carries an active error-severity finding; `any` exits **1** on any active finding; and under either value a review that does not complete also exits **1** — it crashes, or it ends with verdict `Error` (the forge could not be read, the diff could not be parsed or chunked, or every chunk review failed). A gate that silently passes on a broken run is worse than none. An empty PR diff is not a failure (verdict `Approved`, exit **0**). An unrecognized PR URL still exits **0** under every value: nothing was reviewed, so there is no outcome to gate on. The webhook daemon has no exit code and is unaffected.

Think hard before reaching for it. Failing a build on a finding turns a probabilistic reviewer into a merge gate, and the first false positive teaches the team to bypass the gate. Read the verdict from the posted summary instead — and do not build a security control on the exit code.

## Reasoning Models and the Token Budget

`PRXREF_LLM_REASONING_EFFORT` and `PRXREF_LLM_MAX_TOKENS` are coupled, and the coupling is not obvious.

On a reasoning model the hidden reasoning trace is generated from the **same** completion budget as the answer. Raising `PRXREF_LLM_REASONING_EFFORT` therefore spends more of `PRXREF_LLM_MAX_TOKENS` before the model has written a single character of the JSON prxref asked for — so turning reasoning *up* makes truncation *more* likely, not less. Nothing else about the run changes: the request succeeds, the HTTP status is 200, the usage numbers look plausible.

A truncated chunk now says so. The provider reports `finish_reason: length` (or `max_tokens`), the response fails to parse, and prxref counts that chunk as **failed** rather than empty. The posted summary carries a partial-review banner naming the reason:

```
> ⚠️ Partial review: 5 of 8 chunks were reviewed; 3 failed. Findings may be incomplete.
>
> - response truncated at max_tokens=4096 (finish_reason=length); raise PRXREF_LLM_MAX_TOKENS
```

Reasons are deduplicated (seven chunks starved by one budget is one fact) and capped at three, with any remainder counted out loud. `prxref review` also prints a `coverage: 5/8 chunks reviewed` line to stdout.

If you raise `PRXREF_LLM_REASONING_EFFORT`, raise `PRXREF_LLM_MAX_TOKENS` with it. Lowering `PRXREF_CHUNK_TOKEN_BUDGET` is the other lever: smaller chunks produce shorter answers, so each one is likelier to fit.

## Tuning for Your Team

Two knobs decide how noisy prxref is: `PRXREF_CONFIDENCE_FLOOR` (how sure the model must be before a finding is reported) and `PRXREF_MAX_ERROR_FINDINGS` (how many error-severity findings survive the cap, keeping the highest-confidence ones). Two profiles cover most teams.

**Advisory — human-authored PRs.** The reviewer is a second pair of eyes on work someone already thought about, and a wrong comment costs more attention than a missed one saves.

```bash
export PRXREF_CONFIDENCE_FLOOR=0.7
export PRXREF_MAX_ERROR_FINDINGS=3
```

**Thorough — agent-authored PRs.** This is the shipped default. Generated diffs are large, uneven, and nobody has read them yet, so recall matters more than precision.

```bash
export PRXREF_CONFIDENCE_FLOOR=0.6
export PRXREF_MAX_ERROR_FINDINGS=10
```

The trade-off is the whole difference: the advisory profile gives you the three findings most likely to be real, and the thorough profile gives you ten that might be. Raising the floor does not make the reviewer smarter — it just moves where the cut falls, and everything below the cut is dropped unseen. A team that ignores prxref's comments should raise the floor before turning it off; a team reviewing machine-written code should leave it where it ships.

One filter has no knob and always runs: the **hedge gate**, immediately before the confidence floor. A finding whose own title or body conditions the defect on a precondition the model never established from the diff ("If X still leases a client", "Unless the backfill already ran", "if they are members of the root workspaces") is dropped regardless of its confidence, with `drop_reason` `hedged: "<matched phrase>"` in the run record, and never consumes an error-cap slot.

Neither knob affects the exit code — `PRXREF_FAIL_ON` is the only one that can. See [Bad Configuration Is the Only Thing That Fails a Build](#bad-configuration-is-the-only-thing-that-fails-a-build).

## Quality Passes and Drop Reasons

The two knobs above, plus the four opt-in levers added in 0.15.0 (`PRXREF_MAX_WARNING_FINDINGS`, `PRXREF_MAX_OUTOFSCOPE_FINDINGS`, `PRXREF_GROUP_FINDINGS` and `PRXREF_DEDUP_SIMILARITY`, all off by default), are the only configuration that touches the filtering. The twelve deterministic passes themselves, the team severity map and spec grounding that run before them, the release-shaped-PR check, and every `drop_reason` string they emit are documented in one place: **[docs/quality.md](quality.md)**. Everything on that page other than the confidence floor, the per-severity caps, finding grouping and reworded-duplicate dedup is a correctness check against the diff itself, not a noise lever, and has no environment variable.

## Environment Cross-Check & Defaults

The tables above define all **62** configuration keys in `src/prxref/config.py` (`_DEFAULTS`), and every one of them appears in `.env.example`:

- **LLM / Pipeline (44):** `PRXREF_LLM_BACKEND`, `PRXREF_LLM_BASE_URL`, `PRXREF_LLM_API_KEY`, `PRXREF_LLM_MODELS`, `PRXREF_LLM_REASONING_EFFORT`, `PRXREF_LLM_MAX_TOKENS`, `PRXREF_LLM_TIMEOUT`, `PRXREF_LLM_TEMPERATURE`, `PRXREF_LLM_SEED`, `PRXREF_LLM_CLI_PATH`, `PRXREF_LLM_CLI_CONCURRENCY`, `PRXREF_CONFIDENCE_FLOOR`, `PRXREF_MAX_ERROR_FINDINGS`, `PRXREF_MAX_WARNING_FINDINGS`, `PRXREF_MAX_OUTOFSCOPE_FINDINGS`, `PRXREF_GROUP_FINDINGS`, `PRXREF_DEDUP_SIMILARITY`, `PRXREF_MAX_CHUNKS`, `PRXREF_CHUNK_TOKEN_BUDGET`, `PRXREF_CHUNK_MAX_FILES`, `PRXREF_CHUNK_CONTEXT_LINES`, `PRXREF_MAX_WORKERS`, `PRXREF_MAX_INLINE_COMMENTS`, `PRXREF_FAIL_ON`, `PRXREF_DRY_RUN`, `PRXREF_TRACE_FILE`, `PRXREF_TRACE_DIR`, `PRXREF_POST_MODE`, `PRXREF_POST_VERDICT`, `PRXREF_PRICE_TABLE`, `PRXREF_POST_COST`, `PRXREF_SIZE_WARN_LINES`, `PRXREF_SIZE_WARN_FILES`, `PRXREF_SIZE_IGNORE_GLOBS`, `PRXREF_SPEC_SOURCES`, `PRXREF_SPEC_MAX_CHARS`, `PRXREF_SPEC_DIGEST_TOKENS`, `PRXREF_REVIEW_RULES`, `PRXREF_REVIEW_RULES_MAX_CHARS`, `PRXREF_SCOPED_RULES`, `PRXREF_SCOPED_RULES_MAX_CHARS`, `PRXREF_PROMPTS_DIR`, `PRXREF_TICKET_CONTEXT_FILE`, `PRXREF_TICKET_CONTEXT_MAX_CHARS`
- **Per-Forge Auth (10):** `PRXREF_BITBUCKET_TOKEN`, `PRXREF_BITBUCKET_USER`, `PRXREF_BITBUCKET_APP_PASSWORD`, `PRXREF_BITBUCKET_SERVER_TOKEN`, `PRXREF_BITBUCKET_SERVER_USER`, `PRXREF_BITBUCKET_SERVER_PASSWORD`, `PRXREF_GITHUB_TOKEN`, `PRXREF_GITHUB_ENTERPRISE_TOKEN`, `PRXREF_GITLAB_TOKEN`, `PRXREF_AZURE_DEVOPS_TOKEN`
- **Spec Sources / Jira (3):** `PRXREF_JIRA_BASE_URL`, `PRXREF_JIRA_EMAIL`, `PRXREF_JIRA_API_TOKEN`
- **Webhooks (5):** `PRXREF_BITBUCKET_WEBHOOK_SECRET`, `PRXREF_GITHUB_WEBHOOK_SECRET`, `PRXREF_GITLAB_WEBHOOK_SECRET`, `PRXREF_AZURE_DEVOPS_WEBHOOK_SECRET`, `PRXREF_ALLOW_UNSIGNED`

*(62 configuration keys, plus one deprecated alias — `PRXREF_MAX_ERRORS` for `PRXREF_MAX_ERROR_FINDINGS` — for 63 accepted variable names.)*
