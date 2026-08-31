# Environment Variables

All environment variables used by `prxref` are prefixed with `PRXREF_`.

Configuration is loaded from built-in defaults, overridden by environment variables, and further overridden by explicit programmatic keyword arguments (the CLI's `--max-chunks` is one). An empty or whitespace-only value reads as **unset**, so a stray `PRXREF_LLM_TIMEOUT=` in a `.env` file keeps the default instead of aborting.

## Variable Reference

### LLM & Pipeline

| Variable | Default | Purpose |
|---|---|---|
| `PRXREF_LLM_BACKEND` | `openai-compat` | LLM backend selector: `openai-compat`, `ferry`, or `http` (aliases for plain-HTTP OpenAI-compatible endpoint), or `litellm` (in-process LiteLLM router). |
| `PRXREF_LLM_BASE_URL` | *(none — required)* | Base URL for the OpenAI-compatible endpoint (e.g. `https://openrouter.ai/api/v1`). Unset raises `ConfigError` and `prxref review` exits `2`. |
| `PRXREF_LLM_API_KEY` | *(empty)* | API key / Bearer token sent to the OpenAI-compatible endpoint. Optional: leave empty for a local no-auth server. |
| `PRXREF_LLM_MODELS` | *(none — required)* | Comma-separated model fallback chain evaluated in order, cheapest first. First model that answers successfully wins. Unset raises `ConfigError` and `prxref review` exits `2`. |
| `PRXREF_LLM_REASONING_EFFORT` | *(empty)* | Reasoning effort for models that cannot disable reasoning (e.g. `low`\|`high`\|`max` for GLM-5.3-Flash). Empty omits the parameter entirely from the request. Provider-specific vocabulary; not validated client-side. Raising it makes truncation more likely — see [Reasoning models and the token budget](#reasoning-models-and-the-token-budget). |
| `PRXREF_LLM_MAX_TOKENS` | `4096` | Completion-token budget for each worker's review call. Must be **greater than 0**. Too small and the model runs out of budget mid-JSON: that chunk is counted as failed and the summary says so. |
| `PRXREF_LLM_TIMEOUT` | `45.0` | Wall-clock deadline in seconds for one model's review call. Must be **greater than 0**. A model that runs past it is abandoned for the next one in the chain, so this is a per-model deadline, not a per-review one: a three-model chain can spend three times this value before the chunk is given up on. Under the default `openai-compat`/`ferry`/`http` backend the deadline is enforced client-side against elapsed time, including the response body — an endpoint that trickles bytes cannot outlast it. Under `litellm` the value is handed to that library, and its own timeout semantics apply. |
| `PRXREF_LLM_TEMPERATURE` | *(empty — omitted)* | Sampling temperature, e.g. `0.2`. Must be **finite and >= 0**; there is no upper bound, because the maximum is provider-specific. Empty omits `temperature` from the request entirely, which is deliberately not the same as sending `0` — some endpoints reject `temperature` alongside reasoning parameters. |
| `PRXREF_CONFIDENCE_FLOOR` | `0.6` | Minimum confidence score. Must be **within `[0.0, 1.0]` inclusive** — it is a probability everywhere in the pipeline. Findings below the floor are dropped. |
| `PRXREF_MAX_ERROR_FINDINGS` | `10` | Maximum number of error-severity findings reported per review. Excess errors are dropped lowest-confidence-first. Must be **>= 0**; `0` is legal and caps every error. (Legacy alias: `PRXREF_MAX_ERRORS`.) |
| `PRXREF_MAX_CHUNKS` | `8` | Maximum number of diff chunks reviewed per PR. Must be **greater than 0**. Overridable per run with `--max-chunks`. |
| `PRXREF_CHUNK_TOKEN_BUDGET` | `25000` | Approximate token budget per diff chunk. Must be **greater than 0**. Lowering it splits a PR into more, smaller chunks: more LLM calls, but less diff per call. Chunk count is still capped by `PRXREF_MAX_CHUNKS`, and overflow past that cap lands in the smallest existing chunk rather than opening a new one. |
| `PRXREF_CHUNK_MAX_FILES` | `5` | Maximum files placed in one review chunk. Must be **greater than 0**. Chunks stay under the cap while any chunk has room; once `PRXREF_MAX_CHUNKS` is reached and every chunk is full, overflow files join the smallest chunk past the cap rather than being dropped from review. |
| `PRXREF_CHUNK_CONTEXT_LINES` | `3` | Context lines kept around each change when a chunk's diff is rendered into the worker prompt. Must be **>= 0**; `0` emits the changed lines only. The forge's diff is the only source of context — prxref trims what it received and never adds what it did not. |
| `PRXREF_MAX_WORKERS` | `4` | Parallel chunk-review workers. Must be **greater than 0**. The cap that matters is usually the endpoint's rate limit, not the machine. |
| `PRXREF_MAX_INLINE_COMMENTS` | `15` | Maximum inline comments posted per review, applied **after** the quality gate. Must be **greater than 0**. Findings past the cap are still listed in the summary comment; only the inline posting is trimmed. |
| `PRXREF_TRACE_FILE` | *(empty — off)* | Path to append a JSONL run trace to. Empty disables tracing and the tracer becomes a no-op, so there is no cost when unset. One event per line (`run`, `forge.get_pr`, `forge.get_diff`, `parse_diff`, `build_chunks`, `chunk`, `heartbeat`, `post`), flushed as it happens. Each carries a phase: `start`, then `ok` or `fail`; `post` also uses `skip`, so a stage nobody asked to run is distinguishable from one the run never reached — a run still in flight, or one that was killed mid-hang, is as readable as a completed one. Render it to a standalone HTML pipeline view with `prxref trace render <file>`. |
| `PRXREF_DRY_RUN` | `False` | Set to the literal `1` to run the full review and write nothing to the forge — no summary, no inline comments. Applies to the webhook daemon as well as the CLI, which is the only way to watch the daemon against a real repository before letting it comment. `--no-post` is the per-invocation equivalent and still wins when the environment says nothing. Only the literal `1` enables it. |
| `PRXREF_FAIL_ON` | `never` | Exit-code policy for `prxref review`. `never` (the default) keeps the advisory contract — the exit code never reflects findings. `error` exits `1` when the completed review carries an active error-severity finding; `any` exits `1` on any active finding. Under either value a review that fails to complete also exits `1`, so a gating lane cannot read a broken run as green. The webhook daemon has no exit code and is unaffected. See [Bad Configuration Is the Only Thing That Fails a Build](#bad-configuration-is-the-only-thing-that-fails-a-build). |
| `PRXREF_POST_MODE` | `summary+inline` | What gets posted to the forge: `summary+inline` (the summary comment, then inline comments only if the summary landed), `summary` (the summary comment only — inline comments are never posted), or `inline` (inline comments only — no summary is posted on any path, including the error notice). Any other value raises `ConfigError` and `prxref review` exits `2`. A dry run posts nothing in any mode. |
| `PRXREF_POST_VERDICT` | `True` | Set to the literal `1` to keep the verdict stamp in the posted summary; any other value renders the summary without it (no `Approved` / `Request-Changes` heading), keeping the findings, counts, and attribution. The computed verdict printed to stdout and the total-failure notice are unaffected. |

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

### Webhook Receiver

| Variable | Default | Purpose |
|---|---|---|
| `PRXREF_BITBUCKET_WEBHOOK_SECRET` | *(empty)* | HMAC secret for Bitbucket webhooks, Cloud and Server alike (verified against `X-Hub-Signature` via HMAC-SHA256). |
| `PRXREF_GITHUB_WEBHOOK_SECRET` | *(empty)* | HMAC secret for GitHub webhooks (verified against `X-Hub-Signature-256` via HMAC-SHA256). |
| `PRXREF_GITLAB_WEBHOOK_SECRET` | *(empty)* | Secret token for GitLab webhooks (verified against `X-Gitlab-Token`). |
| `PRXREF_ALLOW_UNSIGNED` | `False` | Accepts webhooks without valid HMAC/token signatures (dev/testing only; logs a warning). Must be the literal string `1` — `true`/`yes`/`on` deliberately do **not** enable the bypass, so it cannot be switched on by a stray truthy value. |

## Bad Configuration Is the Only Thing That Fails a Build

`prxref review` exits **0** on every review error — an empty diff, a network failure, an LLM timeout, bad forge credentials, even a review in which every chunk failed. prxref is an advisor, not a merge gate.

It exits **2** on exactly one class of problem: a **configuration error**. That is a required value missing, a value that will not parse, a value outside its valid range, or one outside its key's allowed vocabulary (`PRXREF_FAIL_ON` accepts only `never`, `error`, or `any`). The check runs after the environment *and* any programmatic override, so no path into the config can smuggle a degenerate value through to the wire.

The message names whichever input actually supplied the offending value:

```
$ PRXREF_MAX_CHUNKS=0 prxref review --pr-url https://github.com/org/repo/pull/1
configuration error: PRXREF_MAX_CHUNKS: must be a finite number greater than 0, got 0
$ prxref review --pr-url https://github.com/org/repo/pull/1 --max-chunks 0
configuration error: --max-chunks: must be a finite number greater than 0, got 0
```

The second form exists because naming the environment variable unconditionally sent operators hunting for a `PRXREF_MAX_CHUNKS` they had never set.

One knob can move the exit code beyond that: `PRXREF_FAIL_ON`. Its default `never` is everything above, unchanged. Setting it to `error` exits **1** when the completed review carries an active error-severity finding; `any` exits **1** on any active finding; and under either value a review that fails to complete also exits **1** — a gate that silently passes on a broken run is worse than none. An unrecognized PR URL still exits **0** under every value: nothing was reviewed, so there is no outcome to gate on. The webhook daemon has no exit code and is unaffected.

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

Neither knob affects the exit code — `PRXREF_FAIL_ON` is the only one that can. See [Bad Configuration Is the Only Thing That Fails a Build](#bad-configuration-is-the-only-thing-that-fails-a-build).

## Environment Cross-Check & Defaults

The tables above define all **34** configuration keys in `src/prxref/config.py` (`_DEFAULTS`), and every one of them appears in `.env.example`:

- **LLM / Pipeline (21):** `PRXREF_LLM_BACKEND`, `PRXREF_LLM_BASE_URL`, `PRXREF_LLM_API_KEY`, `PRXREF_LLM_MODELS`, `PRXREF_LLM_REASONING_EFFORT`, `PRXREF_LLM_MAX_TOKENS`, `PRXREF_LLM_TIMEOUT`, `PRXREF_LLM_TEMPERATURE`, `PRXREF_CONFIDENCE_FLOOR`, `PRXREF_MAX_ERROR_FINDINGS`, `PRXREF_MAX_CHUNKS`, `PRXREF_CHUNK_TOKEN_BUDGET`, `PRXREF_CHUNK_MAX_FILES`, `PRXREF_CHUNK_CONTEXT_LINES`, `PRXREF_MAX_WORKERS`, `PRXREF_MAX_INLINE_COMMENTS`, `PRXREF_TRACE_FILE`, `PRXREF_DRY_RUN`, `PRXREF_FAIL_ON`, `PRXREF_POST_MODE`, `PRXREF_POST_VERDICT`
- **Per-Forge Auth (9):** `PRXREF_BITBUCKET_TOKEN`, `PRXREF_BITBUCKET_USER`, `PRXREF_BITBUCKET_APP_PASSWORD`, `PRXREF_BITBUCKET_SERVER_TOKEN`, `PRXREF_BITBUCKET_SERVER_USER`, `PRXREF_BITBUCKET_SERVER_PASSWORD`, `PRXREF_GITHUB_TOKEN`, `PRXREF_GITHUB_ENTERPRISE_TOKEN`, `PRXREF_GITLAB_TOKEN`
- **Webhooks (4):** `PRXREF_BITBUCKET_WEBHOOK_SECRET`, `PRXREF_GITHUB_WEBHOOK_SECRET`, `PRXREF_GITLAB_WEBHOOK_SECRET`, `PRXREF_ALLOW_UNSIGNED`

*(34 configuration keys, plus one deprecated alias — `PRXREF_MAX_ERRORS` for `PRXREF_MAX_ERROR_FINDINGS` — for 35 accepted variable names.)*
