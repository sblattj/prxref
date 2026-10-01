# LLM Backends & Failover Architecture

`prxref` reaches a model through one of four backends: a lightweight OpenAI-compatible plain-HTTP client, an optional in-process `litellm` wrapper, or one of two subscription CLI backends, `claude-cli` and `kiro-cli`, that run your own logged-in CLI (see [Subscription CLI backends](#subscription-cli-backends-claude-cli-and-kiro-cli)). There is no default model chain on any backend — `PRXREF_LLM_MODELS` is required, and leaving it unset raises `ConfigError` (`prxref review` exits `2`). There is no default endpoint either: `PRXREF_LLM_BASE_URL` is required by the `openai-compat` backend (and its `ferry`/`http` aliases), with the same exit `2` when unset, and is not used by any other backend (see [Optional Backend: litellm](#optional-backend-litellm)).

## Key Architectural Principles

1. **No Provider Credentials in `prxref`:** `prxref` never looks up, stores, or uses a provider credential (no AWS IAM keys, no OpenAI keys, no Google Cloud keys, and no Anthropic API keys), and its own settings are provider-neutral `PRXREF_*` names. The one key it sends is `PRXREF_LLM_API_KEY`, as a bearer token to the `openai-compat` endpoint you configured. Every provider key lives behind that endpoint, in the provider SDK's own environment (`litellm`), or inside your own logged-in CLI (`claude-cli`, `kiro-cli`). A CLI backend hands its CLI the environment `prxref` was started with; `claude-cli` first removes a fixed list of credential-routing variable *names*, so the CLI falls back to its subscription login, and never reads their values.
2. **Fast Caller-Side Failover:** Fallback is implemented as a fast sequential loop over the model list. If a model encounters HTTP 429 (rate limit), HTTP >= 500 (server/upstream error), connection failures, or timeouts, the client immediately advances to the next model in the chain without same-model retries. Above the backends, the reviewer sends a request again when its reply cannot be used as a review, up to `PRXREF_LLM_PARSE_RETRIES` times, and an empty reply once even at `0` (see [Parse Retries](#parse-retries)): the same prompt goes out as a new call, which walks the fallback chain like any other, so no backend ever retries a model in place.
3. **Single-Shot Calls With Pre-Gathered Context:** every review call is one request whose context prxref gathered before sending it; the model is offered no tools and there is no agent loop. One bounded exception, off by default: with `PRXREF_CONTEXT_FOLLOWUP=on` at `PRXREF_REPO_CONTEXT=repo`, a chunk whose first reply asks about a symbol it was not shown is sent once more with that symbol's definition appended. The lookup is deterministic, the model still calls no tools, and a follow-up never triggers another (see [Context follow-up](#context-follow-up-opt-in)).

---

## Primary Backend: OpenAI-compatible HTTP

The primary and default backend communicates via plain HTTP requests with any OpenAI-compatible `/v1/chat/completions` server — a hosted router (OpenRouter, Together, Groq), a self-hosted gateway such as `llm-ferry`, or a local runtime such as vLLM or Ollama.

- **Default Backend Alias:** `PRXREF_LLM_BACKEND=openai-compat` (aliases: `ferry`, `http`).
- **Endpoint URL:** `PRXREF_LLM_BASE_URL=https://llm.example.com/v1`. Required for this backend; there is no default.
- **API Key:** `PRXREF_LLM_API_KEY` (sent as `Authorization: Bearer <key>`). Optional — leave empty for a local no-auth server.
- **Models:** Model names are whatever the endpoint accepts, listed cheapest first. Required; there is no default.

### Request Budget

Four variables shape the request itself. All are optional, and a bad value exits `2` naming the variable rather than degrading the review. `PRXREF_LLM_MAX_TOKENS`, `PRXREF_LLM_TIMEOUT`, and `PRXREF_LLM_SEED` are range-checked when the config loads; `PRXREF_LLM_TEMPERATURE` is validated later, when the LLM client is constructed (`create_llm_client`) — same operator-visible outcome, just a later checkpoint.

| Variable | Default | Effect on the request |
|---|---|---|
| `PRXREF_LLM_MAX_TOKENS` | `4096` | `max_tokens` on every worker call on `openai-compat` and `litellm`; the CLI backends accept it and do not apply it (see [What is not applied](#what-is-not-applied)). Must be > 0. This is a per-call budget threaded config → orchestrator → reviewer → `invoke`; the client never reads it. |
| `PRXREF_LLM_TIMEOUT` | `45.0` | The client's default request timeout, in seconds. Must be > 0. It is a **per-model** deadline: a model that exceeds it is abandoned and the next in the chain is tried immediately, so a chain of three can take up to three timeouts. |
| `PRXREF_LLM_TEMPERATURE` | `0.0` (sent) | `temperature` in the payload. Must be finite and >= 0; no upper bound, since the maximum is provider-specific. Unset or empty sends the default `0.0` rather than omitting the field, so an identical diff reviews identically by default; a set value wins. `PRXREF_LLM_REASONING_EFFORT` keeps its own pass-through-unvalidated rule. |
| `PRXREF_LLM_SEED` | *(auto-derived)* | Top-level `seed` in the payload, OpenAI-compatible backends and `litellm` alike. Must be an integer >= 0 (`0` is a valid seed). Unset derives one random seed per process, shared by every client the run builds, so all LLM calls in a run pin the same sampling state; the run record's `sampling.seed` reports it. `off` (lowercase) sends no seed at all, configured or derived, and `sampling.seed` is then `null`. |

### Configuration Example

```bash
PRXREF_LLM_BACKEND=openai-compat
PRXREF_LLM_BASE_URL=https://llm.example.com/v1
PRXREF_LLM_API_KEY=$MY_ENDPOINT_KEY
PRXREF_LLM_MODELS=z-ai/glm-5.3-flash,openai/gpt-4.1-mini
PRXREF_LLM_MAX_TOKENS=4096
PRXREF_LLM_TIMEOUT=45.0
```

### Reasoning Models Share the Budget

`PRXREF_LLM_REASONING_EFFORT` is passed through unvalidated for models that cannot disable reasoning. On such a model the hidden reasoning trace is generated from the **same** completion budget as the answer, so raising the effort spends `PRXREF_LLM_MAX_TOKENS` before the model writes any of the JSON prxref asked for. Turning reasoning up therefore makes truncation *more* likely while every other signal — HTTP 200, plausible usage numbers — still says the run was fine.

prxref reads the choice's `finish_reason` to catch this. `length` or `max_tokens` on an unparseable response is reported as `response truncated at max_tokens=N (finish_reason=length); raise PRXREF_LLM_MAX_TOKENS`, the chunk is counted as failed, and the reason is repeated in the posted summary's partial-review banner. An unrecognized stop reason falls back to the plain parse error — a missed hint, never a false claim of truncation.

Raise `PRXREF_LLM_MAX_TOKENS` alongside the effort, or lower `PRXREF_CHUNK_TOKEN_BUDGET` so each answer has less to say. See [Reasoning models and the token budget](env-vars.md#reasoning-models-and-the-token-budget).

### Failover Semantics

The client iterates through `PRXREF_LLM_MODELS` in left-to-right priority order (cheapest/fastest model first, escalating to a stronger one on failure):
- A model attempt fails if it encounters:
  - Request timeout
  - Connection/network error
  - HTTP status code `>= 400` (including HTTP 429 and 5xx)
  - Malformed JSON response
- On any failure, `OpenAICompatClient` logs the model error and tries the next model in the chain immediately.
- If all configured models fail, `LLMError` is raised containing the per-model failure reasons.
- A 4xx body that names a model as permanently gone (deprovisioned, renamed, not supported, or never enabled for this integrator) is cached for the life of the client: every later call skips that model outright — no request, no per-call log — with a single WARNING logged the one time it is marked. `LiteLLMClient` mirrors the same cache, keyed on the exception litellm raises (a 4xx-shaped error naming a model) instead of an HTTP status; it filters the unavailable model out of both the primary `model` and its native `fallbacks` before calling litellm, and raises `LLMError` without calling litellm at all once every configured model has been marked. A 5xx, a timeout, or a connection error is never cached — only a 4xx carrying that phrasing is treated as permanent.

### Parse Retries

Failover handles a model that did not answer. A model that answered with something prxref cannot use as a review is handled one level up, by the reviewer, under one retry budget per review unit (a chunk worker, or the whole-PR sweep): `PRXREF_LLM_PARSE_RETRIES`, default `1`, at least `0`, with no upper bound. After each call the reviewer decides:

- **A reply stopped at the budget** (`finish_reason` `length` or `max_tokens`) never draws on `N`. If it can be used, it is used, with a WARNING that its findings may be incomplete, and is not retried. If it cannot be used, for example because hidden reasoning spent the whole budget and the reply is empty, it is retried once, automatically, at double the budget capped at `16384` (#52), after one WARNING `<unit>: reply stopped at the completion budget (max_tokens=<B>, finish_reason=<reason>) before it was usable; retrying once at max_tokens=<B2>`. That retry walks `PRXREF_LLM_MODELS` again and costs one extra call, only in this case; a budget already at `16384` or above is not retried. When the retry is stopped at the budget too, the unit fails with the truncation error described under [Reasoning Models Share the Budget](#reasoning-models-share-the-budget), naming the larger budget and `PRXREF_LLM_MAX_TOKENS`. When the retry raises (a provider that rejects the larger budget, say), the unit fails with the first reply's truncation error followed by `(a retry at max_tokens=<B2> failed: <ExcType>)`.
- **An empty reply** (no text, or whitespace only) is retried while fewer than `max(N, 1)` retries have run, so it gets one retry even at `0`, as in 0.16.0.
- **A reply that does not parse**, one that parses to something other than a JSON object, and, at `1` or more only, **an object without a `findings` list** are retried while fewer than `N` retries have run. At `0`, an object without `findings` is a review with no findings, as in 0.16.0.
- **A call that raised**, for example because every model in the chain failed, is never retried: the unit fails with that error.

A parse retry sends the same request, with the same prompt and the same `max_tokens`, as a new call. That call walks `PRXREF_LLM_MODELS` exactly like a first call, starting at the first model rather than at the one that answered, and a model marked permanently gone stays skipped. A reply that came back is never handed to the next model in the chain.

At `1` or more, each retry logs one WARNING, `<unit>: empty model reply (finish_reason=<reason>); parse retry <k> of <N>` or `<unit>: unusable model reply (<error>); parse retry <k> of <N>`; at `0`, the one empty-reply retry logs `<unit>: empty model reply (finish_reason=<reason>); retrying once`, as in 0.16.0. Once the budget is spent, the last reply is handled exactly like a first one: if it still cannot be used, the unit fails with that reply's error, worded as in 0.16.0, or with `worker review JSON has no findings list`. A retry that raises fails the unit with that exception, and the earlier calls still count.

A chunk makes at most `1 + max(N, 1)` calls per run, and the timeout retry can run it a second time, so 4 at the default; the sweep, which has no timeout retry, makes at most `1 + max(N, 1)`, 2 at the default. With `PRXREF_CONTEXT_FOLLOWUP=on`, a chunk that was not retried after a timeout can add a [context follow-up](#context-follow-up-opt-in) of at most 2 calls (the follow-up and its one empty-reply retry; it gets no parse retries), so such a chunk makes at most `3 + max(N, 1)`. A follow-up never runs after a timeout retry, so the per-chunk ceiling stays `2 * (1 + max(N, 1))`, 4 at the default. Each call may still try several models. Every call counts toward the unit's tokens, time and cost (see [Which calls count](#which-calls-count)). With `PRXREF_TRACE_DIR` set and `N` at `1` or more, the unit's four trace files show the reply that was used, each discarded reply is kept as `<unit>.attempt<K>.response.json` (K from 1), and `<unit>.meta.json` gains `parse_retries` and `first_error` after its eight keys. The run record and `--format json` carry `parse_retries`, the retries summed over the run: `0` when nothing was sent again, and `null` when `PRXREF_LLM_PARSE_RETRIES` is `0`. The `prxref eval` judge retries under the same variable, with its own rules; see [evals.md](evals.md).

A custom worker or systemic template whose reply format leaves out `findings` fails every unit at `1` or more; see [prompt-templates.md](prompt-templates.md#what-is-checked).

---

## Optional Backend: `litellm`

For environments running without a centralized inference gateway, `prxref` supports in-process multi-provider routing via `litellm`.

- **Backend Setting:** `PRXREF_LLM_BACKEND=litellm`
- **Installation:** `pip install 'prxref[litellm]'`
- **Endpoint URL: not used.** litellm resolves each model's own provider endpoint and reads that provider's credential (for example `OPENROUTER_API_KEY`) from its own environment, so `PRXREF_LLM_BASE_URL` is not required here and neither it nor `PRXREF_LLM_API_KEY` is ever passed to litellm. A set `PRXREF_LLM_BASE_URL` is ignored with one INFO line (`PRXREF_LLM_BASE_URL is set but not used by the litellm backend; ignoring it`), so a deployment that set a placeholder URL to get past the check older releases applied to every backend keeps working unchanged. To route through a LiteLLM **proxy**, which speaks the OpenAI API, use the `openai-compat` backend with `PRXREF_LLM_BASE_URL` pointing at the proxy.
- **Shared settings:** `PRXREF_LLM_MAX_TOKENS`, `PRXREF_LLM_TIMEOUT`, `PRXREF_LLM_TEMPERATURE`, and `PRXREF_LLM_SEED` apply here too — temperature resolves to the same `0.0` default when unset, and the seed, configured or else auto-derived, is passed as `seed=` to `litellm.completion` unless `PRXREF_LLM_SEED=off`, which passes none. `PRXREF_LLM_REASONING_EFFORT` is passed as `reasoning_effort=` when set and left out when empty, exactly as `openai-compat` sends it; prxref forwards the value unvalidated, and litellm maps it to each provider's own effort parameter.

### Configuration Example

```bash
PRXREF_LLM_BACKEND=litellm
PRXREF_LLM_MODELS=bedrock/anthropic.claude-3-7-sonnet-20250219-v1:0,vertex_ai/gemini-2.5-pro,openrouter/meta-llama/llama-3.3-70b-instruct
```

### Execution Details

- The first model in `PRXREF_LLM_MODELS` is used as the primary model.
- Remaining models in the list are passed to `litellm.completion` via the `fallbacks=` parameter.
- `num_retries=0` is enforced to ensure immediate failover to backup models without blocking retries on failed endpoints.

### Bedrock and other strict providers

Some providers reject a request that carries a parameter they do not support, and then every chunk fails. Bedrock answers `UnsupportedParamsError: bedrock does not support parameters: ['seed']`, because prxref sends a seed on every call by default. Two settings fix that:

- `PRXREF_LLM_SEED=off` stops prxref sending the seed at all. The run record's `sampling.seed` is then `null`.
- `LITELLM_DROP_PARAMS=true` is litellm's own setting, not a prxref one: litellm then drops any parameter the provider does not support, instead of raising. It covers the seed too, and any other parameter the provider refuses.

Some newer models accept only `temperature=1` and reject prxref's default `0.0` (`Only temperature=1 is supported`). Set `PRXREF_LLM_TEMPERATURE=1`; the variable takes any finite value >= 0, so `1` is accepted. Where litellm knows the model refuses the value, `LITELLM_DROP_PARAMS=true` drops the temperature instead, which leaves the provider's default in force; setting it to `1` does not depend on that.

```bash
PRXREF_LLM_BACKEND=litellm
PRXREF_LLM_MODELS=bedrock/anthropic.claude-3-7-sonnet-20250219-v1:0
PRXREF_LLM_REASONING_EFFORT=medium
PRXREF_LLM_SEED=off
PRXREF_LLM_TEMPERATURE=1
```

---

## Subscription CLI backends: `claude-cli` and `kiro-cli`

These two backends review with the Claude Code CLI or the Kiro CLI that is already installed and logged in on your machine, so the review runs on your own subscription instead of an API key. Every model attempt starts one CLI process. `PRXREF_LLM_MODELS` is walked as a failover chain exactly as on the other backends, and every posted comment still names the model.

> **Policy.** These backends only run *your own* locally installed, logged-in CLI, for *your own* use. prxref never ships, stores, or brokers subscription credentials. Anthropic's Agent SDK terms do not allow third-party products to offer claude.ai login or subscription rate limits without approval, so do not use `claude-cli` for a team, a shared webhook, or CI: use an API key, Workload Identity Federation, or Bedrock/Vertex/Foundry through `openai-compat` or `litellm`. prxref's guidance for `kiro-cli` is the same: a developer's own machine, not CI or the webhook daemon. The Docker image ships neither CLI; see [CLI Model Backends in Docker and CI](deploy.md#6-cli-model-backends-in-docker-and-ci).

### Requirements

- **The CLI is installed.** `claude` or `kiro-cli` must be on `PATH`, or `PRXREF_LLM_CLI_PATH` must name it (`~` is expanded, and a bare name is looked up on `PATH`). The binary is resolved when the LLM client is built, before any network call, and a missing or non-executable one exits `2`:

  ```
  configuration error: PRXREF_LLM_BACKEND: kiro-cli needs the 'kiro-cli' CLI, which was not found on PATH; install it and log in, or set PRXREF_LLM_CLI_PATH to its absolute path
  ```

- **The CLI is logged in.** For `claude`, run `claude` once and `/login`; in a headless shell, create a token with `claude setup-token` and export it as `CLAUDE_CODE_OAUTH_TOKEN`, which is passed through to the CLI. For `kiro-cli`, the browser login is enough, and `kiro-cli whoami` shows the account it uses. A `KIRO_API_KEY` in the environment is passed through unchanged.
- A logged-out CLI is not a configuration error. Every model fails, the review fails the way an unreachable endpoint does, and `prxref review` exits `0` — `1` under `PRXREF_FAIL_ON=error` or `any`, like any review that does not complete (see [Troubleshooting](#troubleshooting)).

### Configuration example

```bash
PRXREF_LLM_BACKEND=claude-cli
PRXREF_LLM_MODELS=sonnet,opus
PRXREF_LLM_TIMEOUT=120
```

```bash
PRXREF_LLM_BACKEND=kiro-cli
PRXREF_LLM_MODELS=claude-haiku-4.5,claude-sonnet-4.5
PRXREF_LLM_TIMEOUT=120
```

`PRXREF_LLM_CLI_PATH` and `PRXREF_LLM_CLI_CONCURRENCY` are the two settings only these backends read; see [env-vars.md](env-vars.md).

### What runs

`claude-cli`, one process per model attempt:

```
claude -p --model <model> --output-format stream-json --verbose --tools "" --setting-sources "" --strict-mcp-config --no-session-persistence --max-turns 1 --system-prompt-file <file>
```

`--effort <value>` is appended when `PRXREF_LLM_REASONING_EFFORT` is set. The CLI runs with no built-in tools, no settings files, no MCP servers, no saved session, and a single turn.

`kiro-cli`, one process per model attempt:

```
kiro-cli chat --no-interactive --agent prxref-review --output-format stream-json --trust-tools= --agent-engine v2
```

prxref asks for the v2 agent engine because the v1 engine does not emit `stream-json`, and v2 does not apply a `--model` flag, so each attempt writes the agent file `.kiro/agents/prxref-review.json` into its working directory. That file carries the system prompt and the model, and allows no tools, no MCP servers and no resources (`"tools": []`, `"allowedTools": []`, `"mcpServers": {}`, `"includeMcpJson": false`, `"resources": []`); `--trust-tools=` trusts none either. Whether Kiro still adds user-level configuration, such as `~/.kiro/steering/`, to a working-directory agent has not been verified, and the agent file cannot turn it off.

For both CLIs:

- The process is started from an argument list, never through a shell, and the user message (the diff) goes on stdin, never into the arguments.
- Each attempt runs in a fresh temporary working directory that is removed afterwards, whether the call answered, failed or timed out. For `claude-cli` it is empty and the system prompt file sits beside it; for `kiro-cli` it holds only the agent file.
- `json_mode` calls append one fixed "respond with exactly one JSON object" instruction to the system prompt. Any code fence the model still adds is stripped by the reviewer's lenient parse.

### Environment

- **`claude-cli`** hands the CLI `prxref`'s environment minus eight names: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_PROFILE`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY` and `CLAUDE_CODE_SIMPLE`. Each would move the call off your subscription login: an API key always wins in `-p` mode, a gateway token or base URL re-points the CLI, the three `USE_*` flags route it to a cloud provider, a profile selects an organization identity, and bare mode ignores the OAuth login. Only the names are removed; their values are never read. `CLAUDE_CODE_OAUTH_TOKEN`, `HOME` and `CLAUDE_CONFIG_DIR` are kept, because they are how the CLI finds your login.
- **Managed settings are out of prxref's reach.** An organization's managed `apiKeyHelper` or forced gateway still loads under `--setting-sources ""`. The CLI reports which credential it used: the INFO line of every answered `claude-cli` call ends `auth=<apiKeySource>`, and a value other than `none` logs one WARNING, `claude-cli: the CLI reports apiKeySource=…, so this call is NOT on your subscription login (check managed settings / apiKeyHelper)`.
- **`kiro-cli`** hands the CLI the environment unchanged.

### What is not applied

| Setting | `claude-cli` | `kiro-cli` |
|---|---|---|
| `PRXREF_LLM_BASE_URL` | Ignored, with one INFO line when set | Ignored, with one INFO line when set |
| `PRXREF_LLM_API_KEY` | Ignored | Ignored |
| `PRXREF_LLM_MAX_TOKENS` | Not applied | Not applied |
| `PRXREF_LLM_TEMPERATURE`, `PRXREF_LLM_SEED` | Not applied, with one WARNING when set | Not applied, with one WARNING when set |
| `PRXREF_LLM_REASONING_EFFORT` | `--effort <value>`, passed unvalidated | Not applied, with one INFO line when set |

Neither CLI takes a per-call output budget. `PRXREF_LLM_MAX_TOKENS` is deliberately not mapped to `CLAUDE_CODE_MAX_OUTPUT_TOKENS`: hitting that cap makes the CLI spend extra recovery turns and still end in an error. A `CLAUDE_CODE_MAX_OUTPUT_TOKENS` you export yourself reaches the CLI unchanged.

Neither CLI takes a temperature or a seed either, so a review on a CLI backend is less reproducible than one on `openai-compat` or `litellm` (see [Determinism](#determinism-what-is-pinned-and-what-still-varies)), and the run record's `sampling` field shows `"temperature": null` and `"seed": null`.

### Models

- **`claude-cli`** takes whatever `claude --model` takes: an alias such as `sonnet`, `opus` or `haiku`, or a full model id. The attribution and the logs name the model the CLI reports it ran, so an alias shows up as its full id. A model the CLI rejects as unknown (a 404, or its unrecognized-model marker on stderr) is skipped for the rest of the run, with one WARNING.
- **`kiro-cli`** takes the ids `kiro-cli chat --list-models` prints, such as `claude-haiku-4.5`. The model goes into the agent file because the v2 engine ignores `--model`: it warns `failed to set model … Method not found` and runs its `auto` model. Kiro does not report which model ran, so the attribution names the model you configured. An unknown id fails that model as `<model>: prompt error: Internal error (possibly an unknown model; check kiro-cli chat --list-models)`. Because Kiro's error does not name the model, prxref does not skip it for the rest of the run: every call tries it again before moving on down the chain.

### Concurrency and timeouts

- `PRXREF_LLM_CLI_CONCURRENCY` (default `2`) caps the CLI processes one client runs at once. The review's workers queue for a free slot, and the wait does not count against the timeout. A subscription's rate window belongs to your account, so a higher cap spends it faster.
- `PRXREF_LLM_TIMEOUT` is each model's wall-clock deadline, and it includes the CLI's own start-up: about 2.3 s for `claude` on a tiny prompt whose model time was 1.4 s, and 3.6–6.7 s for `kiro-cli`, as observed while designing these backends. Live `kiro-cli` calls on 2026-09-23 were much slower, 24–27 s for a one-line prompt and 28–37 s for a review call, so for `kiro-cli` `120` is the floor, not a comfortable margin. The default is `120` s: reasoning models routinely need 50 s or more for a large chunk over HTTP, and `kiro-cli` needs that much as a floor. A model that misses its deadline has its whole process group killed, the chain moves on, and the review's zero-context retry applies exactly as it does over HTTP.

### Tokens, cost and credits

- **`claude-cli`** counts cache-creation and cache-read tokens as input tokens, because that is prompt the model read.
- **`kiro-cli`** reports no token counts, so every Kiro call counts `0` tokens and the attribution reads `0 tok`. Kiro meters credits, not dollars. The INFO line of every answered call ends with the credits Kiro metered and its session id:

  ```
  INFO llm attempt 1/1 ok: backend=kiro-cli model=claude-haiku-4.5 3570ms in=0 out=0 finish=end_turn credits=0.0060 session=<session id>
  ```

What either backend contributes to `cost_usd` is set out in [Cost accounting](#cost-accounting).

### Privacy

- **`kiro-cli`** saves every chat under `~/.kiro/sessions/cli/<session id>.json` and `.jsonl`, prompt included, so every diff it reviewed is kept there. prxref does not delete these files, because deleting a session takes Kiro about 11 seconds. Use the `session=` id from the INFO line with `kiro-cli chat --delete-session <session id>`, or prune the directory yourself.
- **`claude-cli`** runs with `--no-session-persistence`, so the CLI saves no transcript of a review.

### Troubleshooting

| What you see | What it means |
|---|---|
| Exit `2`, `configuration error: PRXREF_LLM_BACKEND: … needs the '…' CLI, which was not found on PATH` | The CLI is not installed, or not on the `PATH` prxref runs with. Install it, or set `PRXREF_LLM_CLI_PATH`. |
| Exit `2`, `configuration error: PRXREF_LLM_CLI_PATH: '…' is not an executable file` | The override names a missing file or one without execute permission. |
| Every model fails, and each reason quotes the CLI's own login or authentication error | The CLI is logged out. Log in again (for headless `claude`, refresh `CLAUDE_CODE_OAUTH_TOKEN`). |
| `<model>: prompt error: Internal error (possibly an unknown model; …)` | Kiro does not know that model id. Check `kiro-cli chat --list-models`. |
| `<model>: engine error: …` | Kiro failed before the model ran, for example because the installed `kiro-cli` cannot start the v2 agent engine. Update `kiro-cli`. |
| WARNING `claude-cli: subscription rate limit status=… type=… utilization=…` | Your subscription window is close to its limit. A `rejected` status fails the call as `rate limited`. |
| WARNING `claude-cli: the CLI reports apiKeySource=…` | Managed settings or an `apiKeyHelper` put the call on an API key, not your subscription. |
| WARNING `claude-cli: the CLI loaded tools/MCP servers despite --tools '' --strict-mcp-config` | The CLI no longer honours the isolation flags; its options may have changed. |
| `<model>: timeout (TimeoutExpired after 45s)` | Start-up plus the answer took longer than `PRXREF_LLM_TIMEOUT`. Raise it to `120` or more. |

---

## Determinism: what is pinned, and what still varies

- `PRXREF_LLM_TEMPERATURE` defaults to `0.0`, and `0.0` is **sent** on the wire
  by the two API backends, `openai-compat` and `litellm`, rather than omitted.
- `PRXREF_LLM_SEED` is sent on every call by the two API backends,
  `openai-compat` and `litellm`: the configured
  value when set, else one random seed derived per process and shared by every
  client the run builds — temperature 0 alone cannot pin hosted inference
  (issue #56), so an unseeded run still varies call to call. The `sampling`
  field reports which seed was in force. `PRXREF_LLM_SEED=off` opts out: no
  seed is sent and `sampling.seed` is `null`. The CLI backends, `claude-cli` and
  `kiro-cli`, send no seed and no temperature: setting either logs one WARNING
  that it is not applied, and `sampling` reports both as `null` (see
  [What is not applied](#what-is-not-applied)).
- **Neither makes a review bit-reproducible.** Providers vary by system
  fingerprint, load-balanced backends serve the same model from different
  hardware, MoE routing shifts with batch composition, and many gateways accept
  an unknown top-level `seed` and ignore it. Two identical runs may still return
  different findings, and `prxref` cannot detect a gateway that dropped the seed.
- The `sampling` field in the run record — `{"temperature", "seed", "models"}`,
  present on every exit including a failed run — tells you which knobs were
  actually in force for the run you are looking at.
- What *is* deterministic is everything downstream of the model: findings are
  ordered by `(file, line, title)`, and the error and inline-comment caps break
  ties by finding content (confidence first, then file, line, and normalized
  title), never by the order the workers happened to return in. The same
  findings in any arrival order therefore produce the same review.

## Cost accounting

Every run record carries `cost_usd` (USD) and `cost_estimated` (bool), on every exit, and `--format json` prints both. A run's cost is in exactly one of three states:

- **Reported.** The backend returned a dollar figure for each call. A reported figure always wins, even over a price-table entry for the same model.
- **Estimated.** No figure came back, but `PRXREF_PRICE_TABLE` prices the model. `cost_estimated` is `true`.
- **Unknown.** Neither. `cost_usd` is `null`: never `0`, and never a partial sum of the calls that were priced.

### Where a reported figure comes from

| Backend | Source | `cost_source` |
|---|---|---|
| `openai-compat` (`ferry`, `http`) | The response body's `usage.cost` (OpenRouter returns it on every completion without being asked), else the `x-litellm-response-cost` response header that a LiteLLM gateway or `llm-ferry` sets. The body value must be a JSON number. LiteLLM omits the header when it cannot price the call **and** when the cost is zero, so a free model behind a gateway reports nothing. | `usage.cost` / `x-litellm-response-cost` |
| `litellm` | `response_cost`, which litellm computes from its own price map. prxref never calls `litellm.completion_cost()`. | `litellm` |
| `claude-cli` | The CLI's `total_cost_usd`. On a subscription this is the **API-equivalent cost at list price, not your subscription bill**, so the `-v` line and the posted attribution label it `(API-equivalent)` (see [Where the cost shows](#where-the-cost-shows)). | `claude-cli` |
| `kiro-cli` | None. Kiro reports credits, not dollars, and no token counts, so a price-table entry cannot estimate it either: a run on `kiro-cli` always reads "cost unknown". | — |

prxref sends nothing extra to get a figure: the request never carries `usage: {"include": true}`. A figure that is not a finite number `>= 0` (a negative, `NaN`, a string in the body, an empty or `None` header) counts as no figure.

### The price table

`PRXREF_PRICE_TABLE` is inline JSON (the first non-space character is `{`) or a path to a JSON file. It maps a model name to USD per **million** tokens:

```bash
PRXREF_PRICE_TABLE='{"openai/gpt-4o-mini": {"input": 0.15, "output": 0.60}}'
PRXREF_PRICE_TABLE=./prxref-prices.json
```

- The lookup is on the **exact** model name the call reported, which is the name shown as `model=` in the attribution. It can differ from the name in `PRXREF_LLM_MODELS`, because the endpoint's answer names the model. There is no prefix or pattern matching.
- The table is only consulted for a call with no reported figure, and only when that call counted input tokens. Zero input tokens means the backend reported no usage, and an estimate would be a fake `$0`.
- Give a free or local model a zero entry (`{"input": 0, "output": 0}`). Without one, a run on it reads "cost unknown", never `$0`.
- An estimate prices every input token at the list rate, so it ignores prompt-cache discounts that a provider's own figure reflects. That is one more reason a reported figure always wins.
- The schema is strict. Invalid JSON, an unreadable file, a missing or unknown field (`"ouput"`), a duplicate model, or a price that is not a finite number `>= 0` raises `ConfigError` naming `PRXREF_PRICE_TABLE`, and `prxref review` exits `2`.

When a run ends up unknown because some model had neither a reported figure nor a usable table entry, prxref logs one INFO line naming the model(s), in the exact spelling to key the table on:

```
cost unknown: no reported cost and no usable PRXREF_PRICE_TABLE estimate for model(s) 'openai/gpt-4o-mini'
```

### Which calls count

A review is its chunk workers plus the systemic sweep, and the total covers the same calls as the run's token counts:

- A call whose response **arrived** is counted, including one that was then truncated or failed to parse. It was billed.
- A call that raised (a timeout, a connection error, an HTTP error) returned nothing and adds nothing. A provider that bills abandoned generations may charge more than `cost_usd` says.
- A run that sent requests and got no response back at all is unknown (`null`).
- A run that made no LLM request (an empty diff, or a forge or diff error before the review) costs a known `0.0`.
- Inside one `openai-compat` call, truncated completions that the fallback chain moved past were billed too, so they are added to that call's figure. Its token counts still cover only the answering model. If any of those completions came back without a figure, the call's figure is unknown.
- The timeout retry (the one re-run with `context_lines=0`) replaces the first attempt's result, cost included, exactly as it replaces its tokens, and the run record's `parse_retries` counts only the re-run's parse retries.
- A [parse retry](#parse-retries), the empty-reply retry included, adds to the calls before it instead: every call was billed, so their tokens and time are summed, and the unit's reported cost is the sum of every call's figure, or unknown when any call reported none. A retry that raised adds nothing, and the calls before it still count.
- A [context follow-up](#context-follow-up-opt-in) adds to the chunk's first result the same way: its tokens are summed into the chunk's, its cost is combined with the chunk's (unknown when either is), and the chunk's `model` becomes the follow-up's when the follow-up answered. A follow-up that failed still counts what it was billed. The chunk's `parse_retries` and `first_error` stay the first call's.
- If the total cannot be computed at all, for example because a library caller passed a malformed table object, the run logs a WARNING and its cost is unknown. Cost accounting never fails a review.

### Where the cost shows

- The run record and `--format json`: `cost_usd` and `cost_estimated`.
- `prxref review -v`: `cost: $0.0007`, `$0.0007 (API-equivalent)`, `~$0.0007 (est.)` or `cost unknown` after the token count.
- The JSONL trace (`PRXREF_TRACE_FILE`): the `run ok` and `run fail` events carry `cost_usd` and `cost_estimated`. Each `chunk ok` and `sweep ok` event carries that unit's reported `cost_usd`; estimates are computed for the run only, so a unit priced from the table shows `null` there.
- The per-unit trace files (`PRXREF_TRACE_DIR`): each `<unit>.meta.json` carries `cost_usd` and `cost_source`.
- The posted comment, only with `PRXREF_POST_COST=1`. The cost is appended as the **last** field of the summary's attribution line and of the error notice's:

  ```
  Reviewed by prxref · model=openai/gpt-4o-mini · 4619 tok · 3.1s · $0.0007
  Reviewed by prxref · model=openai/gpt-4o-mini · 4619 tok · 3.1s · ~$0.0007 (est.)
  Reviewed by prxref · model=openai/gpt-4o-mini · 4619 tok · 3.1s · cost unknown
  Reviewed by prxref · model=claude-sonnet-5 · 7564 tok · 13.4s · $0.0202 (API-equivalent)
  ```

  `(API-equivalent)` appears on the `-v` line and in the attribution when every reported figure in the run came from `claude-cli`; an estimated run keeps `~… (est.)`, and `--format json` adds no label (each unit's `cost_source` in the `PRXREF_TRACE_DIR` meta files names the source). A notice posted before any LLM request says `$0.00`, and a cost below $0.0001 reads `<$0.0001`, never `$0.00`. Inline comments never carry a cost. With the flag off, which is the default, the attribution line is byte-identical to a build without cost accounting.

## Worker Prompt Context

Each worker sees one chunk's unified diff, trimmed to `PRXREF_CHUNK_CONTEXT_LINES` lines around every change. Two optional blocks are appended after the diff to answer the questions the diff alone cannot, and repository context can add three more after them. The opt-in [context follow-up](#context-follow-up-opt-in) can then send the chunk once more with one further block at the end.

### Dependency versions and definitions

- **`### Dependency versions`** — `name@version` for each third-party package the chunk's *added* lines import, resolved from the nearest manifest walking up from each changed file to the repository root: `package.json` (`dependencies` + `devDependencies`), `pyproject.toml` (`[project] dependencies` and `[tool.poetry.dependencies]`), `go.mod` `require` lines, and `Cargo.toml` `[dependencies]`. Only imported packages appear. Relative and `node:` specifiers, Python stdlib modules, and relative Python imports are excluded. Without this block a reviewer answers library semantics from whichever major dominates its training data.
  - **Java and Kotlin** (`.java`, `.kt`, `.kts`, since 0.17.0) list `groupId:artifactId@version` for the imports on the added lines. The walk tries `pom.xml`, then `build.gradle.kts`, then `build.gradle` at each level, and the first non-empty file wins, even one that cannot be parsed, which then contributes nothing. A `pom.xml` is resolved through `<properties>`, its parent chain inside the repository (at most 5 parents), `<dependencyManagement>` and imported BOMs, and one holding a `<!DOCTYPE` or `<!ENTITY` declaration, or larger than 512 KiB, is refused before it is parsed. A Gradle build file is read in string notation (`"g:a:v"`, `"g:a"`), with `platform`, `enforcedPlatform` and `mavenBom` as BOM owners and the version catalog `libs.versions.toml` when the build file mentions `libs.`; map notation, variables and `gradle.properties` are not resolved. A version owned by a BOM, a parent outside the repository or a Gradle platform names only an owner prxref can know offline (since 0.19.0): with one candidate owner it reads `groupId:artifactId@(managed by <owner>)`; among several, the one whose groupId shares at least 2 leading segments with the dependency's, and strictly more than every other, reads `@(likely managed by <owner>)`; otherwise no owner is named, as in `@(managed by one of 2 imported BOMs: jackson-bom, spring-boot-dependencies)`, `@(managed by the parent or an imported BOM: <parent>, <bom>)` or `@(managed by one of 2 platforms: ...)`, listing at most 3 artifactIds in declared order, then `+N more`. Imports under `java`, `jdk`, `sun` and `kotlin` (not `javax`) and under the project's own groupId or Gradle `group` are skipped. A dependency matches an import when its groupId, of at least 2 segments, is a package prefix of the import, or the two share at least 3 leading segments; among several matches, the artifactId naming a segment of the import wins, and a tie lists every tied artifact. When no matching artifactId names a segment of the import, every tied match is kept and its line ends in ` (group match only)` (`org.slf4j:slf4j-api@2.0.13 (group match only)` for `import org.slf4j.Logger`), unless another import of the file matches that artifact by name. A changed `build.gradle.kts` or `settings.gradle.kts` gets no dependency lines, and the lookup reads nothing for it.
- **`### Definitions referenced by this chunk`** — for identifiers used on added lines whose definition sits in the same file but *outside* the rendered hunk, one `path:line: definition` entry each, taken from the file as served at the PR head. The entry is the defining line plus continuation lines up to a balanced bracket or 6 lines. Caps: at most 40 entries and 8000 characters, with a trailing `… N more definitions omitted` when trimmed; files over 512 KiB are skipped. Definitions the chunk itself adds are never repeated. The languages are JavaScript and TypeScript, Python, and, since 0.17.0, Java (types, methods, fields and constants, and enum constants) and Kotlin (types, including `fun interface`, `object` and `typealias`, then `fun`, extension functions included, and `val`/`var`). A Java or Kotlin entry starts at up to 2 annotation lines directly above the definition, its line number is the first annotation's, and those lines count toward the 6.

Both blocks are **best effort**. They are built from an optional forge method, `get_file_content(ref, path, *, sha) -> str | None`, resolved with `getattr` and always called at the PR head sha (`pr.source_sha`). Every read is cached per run, so one manifest is fetched once no matter how many chunks want it, and any exception from the adapter degrades to no block. A forge that does not implement the method — and a PR with no head sha — reads these files from `--repo-dir` when one is given (since 0.20.0, #29: a `--diff-file` review without `--pr-url` gets both blocks from the checkout), and otherwise reviews exactly as before, with no header and no extra requests, unless repository context (below) is on. A forge that has the method and a head sha is used even when `--repo-dir` is also given. Nothing here can fail a review. Both blocks are built at every `PRXREF_REPO_CONTEXT` level, `off` included, so since 0.17.0 a PR that changes a Java or Kotlin file can cost reads of that file and of the build files the walk tries, on this reader and not under the repository-context read caps.

**Repository context (0.16.0; readers since 0.17.0).** With `PRXREF_REPO_CONTEXT` at `diff` or `repo`, the `### Definitions referenced by this chunk` block also carries definitions from other files, after the same-file entries: from the PR's other changed files at `diff`, Java and Kotlin type declarations included, and at `repo` also from files outside the diff that the chunk's imports, the Java path convention or a file-name search point at. With a reader, a file of the chunk itself is searched only for the names its own added lines do not mention, so a definition that one file of the chunk references and another file of the same chunk holds outside its hunks is found, and a file's own definitions, which the same-file entries already show, are not repeated. At `repo`, a `### Contract excerpts` block of OpenAPI, JSON Schema and migration slices follows. At `repo` with a file listing, the `### Code elsewhere that reads state this chunk writes` block holds excerpts of files the PR does not change that read state the chunk's added lines write, such as a map the change stores an object in or a table it appends rows to, found by name in files of the same language as the writing file: at most 6 entries of at most 8 lines each, from at most 24 files tried, using only the reads the other sources leave under the chunk's read cap. Last, a `### In-repo standards for this chunk` block (#68; on by default at every `PRXREF_REPO_CONTEXT` level since 0.30.1, `off` included, whenever there is a reader) carries heading-sliced sections of the repository's own standards documents — selected by `PRXREF_CONTEXT_STANDARDS_GLOBS` from `docs/standards/**`, `docs/adr/**`, `STANDARDS*.md`, `SECURITY.md` and `CONTRIBUTING.md` — ranked per chunk by what the chunk itself names (a path atom of a changed file, a name, route or table the added lines reference, or a quoted string literal; a heading match outranks any number of body matches), at most 6 documents read per chunk, within the read caps and under `PRXREF_CONTEXT_STANDARDS_MAX_CHARS` (default `6000`). A worker that finds a changed line contradicting a section cites it as `<path>:<line>` in the finding body; when two admitted sections disagree with each other the block says so with a `[note]` line instead of picking a side, and a Superseded or Rejected ADR section is annotated `(status: Superseded)`. The exact value `off` for the globs variable is the one way to turn the block off on its own (a bare empty value reads as unset). Two read caps bound the uncached reads of files outside the diff (a PR diff file, and a path already fetched by any chunk, spend neither): each chunk may make `PRXREF_REPO_CONTEXT_MAX_CHUNK_READS` (default 16), and all chunks together `PRXREF_REPO_CONTEXT_MAX_READS` (default 200) per run (#61). A context follow-up gets a fresh per-chunk allowance. With the defaults and the follow-up off, 8 chunks × 16 = 128 reads cannot reach 200, so only the per-chunk cap can bind; raising `PRXREF_MAX_CHUNKS` or turning the follow-up on can make the run cap bind as well. The run record's `repo_context` says which did: `chunk_read_cap_hit`, `run_read_cap_hit`, and `read_cap_hit` for either, next to the caps in force, `max_chunk_reads` and `max_reads`. These entries share the per-chunk `PRXREF_REPO_CONTEXT_MAX_CHARS` budget instead of the caps above, with the reader excerpts ranked last so the budget cuts them first, and come from one run-wide reader over the forge or `--repo-dir`, not from the reader above; with neither, only the definitions from the PR's own files are left, built from the diff's hunk lines. Only chunk workers receive them; the whole-PR sweep prompt is unchanged. See `PRXREF_REPO_CONTEXT` in [env-vars.md](env-vars.md).

The worker prompt also carries two confidence rules tied to these blocks: a finding that depends on third-party runtime semantics whose version is not listed, or on the semantics of a symbol whose definition is not shown, must cap confidence at 0.5 and be phrased as a question.

On the timeout retry — the one deterministic re-run with `context_lines=0` — the dependency block is kept and the definitions block is dropped, because shrinking the prompt is the entire point of that retry. Repository context goes with it: the retry carries none of the other-file definitions, the contract excerpts, the reader block or the standards sections, and the run record marks the chunk `retry_dropped`. The retry follows a chunk call that timed out (`PRXREF_LLM_TIMEOUT`, default `45` seconds), so a model slower than that reviews the chunk without its repository context.

### Context follow-up (opt-in)

The confidence rule above leaves a finding about a symbol the worker was not shown at 0.5 or below, under the default `PRXREF_CONFIDENCE_FLOOR` of 0.6, so the quality gate drops it however real the bug is. With `PRXREF_CONTEXT_FOLLOWUP=on` (#22), prxref looks the symbol up and asks once more. It is off by default, and at `off` every prompt, forge read, LLM call and run-record value is the same as in 0.17.0; the run record only gains the key `context_followup`, `null`. The values are `off` and `on`; anything else exits `2`, as a bad `PRXREF_REPO_CONTEXT` does.

- **Where it runs.** Only at `PRXREF_REPO_CONTEXT=repo` with a repository reader (the forge or `--repo-dir`), because only that level reads files outside the diff under the exclude floor and the read caps. At any other level, or at `repo` with no reader, the run logs one WARNING, `PRXREF_CONTEXT_FOLLOWUP=on needs PRXREF_REPO_CONTEXT=repo and a repository reader; the context follow-up is off for this run`, and makes no extra call. Chunk workers only: the whole-PR sweep never gets a follow-up.
- **When a chunk gets one.** Its first call returned a review, not an error; it was not re-run after a timeout, since a longer prompt would meet the same deadline; and at least one of its findings is below the confidence floor. Those findings are its questions.
- **Which names are looked up.** First the names a question puts in backticks, in its title or body: identifiers of at least 3 characters that are not `self`, `this`, `cls` or `super`, a literal, a Python, JavaScript/TypeScript, Java or Kotlin keyword, or a Python builtin that cannot resolve to a repository definition: a capitalised name of Python's `builtins` module (`TypeError`, `Exception`) anywhere, and a lowercase one (`len`, `dict`, `isinstance`, `filter`) only as a bare name or call such as `len(`. After a dot, as in `store.filter(` or `obj.format`, a lowercase builtin name is a repository attribute and ranks as following a `.`. A repository that defines its own `TypeError`, or a bare function named like a builtin, does not get it looked up. JavaScript and Java builtins are not filtered beyond their keywords. Type-like names rank first, then names that follow a `.`, then the rest. The plain text outside the backticks adds, under the same filters, the identifiers whose shape marks them as code: both halves of a dotted access or call (`table.recent(` gives `recent`, ranked as following a `.`, and `table`), a call (`name(`), an identifier holding `_` (`model_history`, `HISTORY_WINDOW`), and a type-like word, unless it is an ordinary capitalized word that starts a sentence. A plain English word never qualifies, and a source file name such as `history.py` or `Foo.java`, or any part of a path, gives nothing: file paths are not looked up. In a question with no backticks the plain-text names take the same three ranks; in one with backticks they all come after its backtick names, so a plain-text name never outranks a backticked one. The chunk keeps at most 3 names and skips any name the pull request itself defines. Every question gets one name before any question gets two: first each question's best name (its first name in rank order), highest confidence first, where a question whose best name an earlier question already took counts as served; then, in the slots left, the first-ranked names of every question, then the second-ranked ones, and so on. With more questions than slots, the most confident questions win. Nothing matches phrases in the model's wording.
- **How they are found.** The files the chunk's imports, path conventions and file-name search point at come first, then files of the same language from the listing, closest in the directory tree first. A file the pull request changes and an excluded path are never read, and a chunk makes at most 8 reads for its follow-up, a cached one included. Each name gets its whole definition, not the one-line entry the definitions block holds: for Python, the defining line and every line indented deeper; for brace languages, up to the closing brace; at most 30 lines, the last reading `… N more lines` when cut. A name whose definition sits inside another excerpt counts as found. An excerpt the first prompt already showed word for word is left out, and the rest are admitted in order up to 4 excerpts and 4000 characters, stopping at the first that does not fit. **With no excerpt, there is no call.**
- **What is sent.** The first user prompt with one block added after its last context block:

  ```
  ### Definitions referenced by this chunk, looked up for its open questions

  These definitions were read at the pull request head for symbols your first reading of this chunk could not see. Treat them as shown, and report any finding at its line in the diff above.

  path:line: <definition>
  ```

  The heading begins with the definitions block's own heading, which the worker prompt's confidence rule already treats as shown. The system prompt and the worker template are unchanged. The call keeps the first call's `max_tokens`, context lines and prompt context, gets no parse retries (an empty reply is still sent once more, so at most 2 calls), gets no timeout retry, and never leads to another follow-up.
- **How the reply is used.** A finding of the second reply confirms a question when it is in the same file, at or above the floor, and within 5 lines of it (both lines known), or has the same normalized title, or names one of the symbols looked up for it. Each reply finding confirms at most one question; a question takes the most confident one that confirms it. A confirmed question is replaced by that finding, which then goes through every quality pass. A question with a looked-up symbol and no confirmation is dropped as `not confirmed by context follow-up (confidence <x> below floor <y>)` (see [quality.md](quality.md#drop-reasons)); a question none of whose symbols was found is left alone, and the floor drops it as before. Every other finding of the second reply is discarded and counted, because posting them would make a second review of the chunk with duplicates. Findings already at or above the floor are never touched.
- **When it fails.** An error, an exception, a timeout or a truncated reply leaves the chunk's first findings as they were, so the floor drops its questions exactly as in 0.17.0, with a WARNING, and the review goes on. What the call was billed still counts; see [Which calls count](#which-calls-count).
- **Recorded.** The run record and `--format json` carry `context_followup` after `parse_retries`: `null` when the key is `off`; when it is `on`, `active`, the totals `calls`, `confirmed`, `unconfirmed`, `discarded`, `input_tokens` and `output_tokens`, and `chunks`, one row per chunk (`null` when not active) with its `questions`, the `names` looked up, each admitted excerpt's `path`, `line`, `symbol`, `source` and `chars` (never its text), whether it was `called`, its `error`, its counts and tokens, and `skipped`, which is `null` or `no-questions`, `no-names`, `no-excerpts`, `chunk-error` or `timeout-retry`. The JSONL trace gains `followup` events per chunk and a run-level `context_followup` event with the totals, and with `PRXREF_TRACE_DIR` set the follow-up's own trace files are `chunk<i>.followup.system.md`, `.user.md`, `.response.json` and `.meta.json`, beside the chunk's.

### Code suggestions (opt-in)

With `PRXREF_SUGGESTIONS=on` (#30), every chunk worker's system prompt gains a `## Code suggestions` block after any rule block, and the worker's reply example gains a `"suggestion"` key: a finding may carry the exact replacement text for its lines, `line` through an optional `suggestion_end_line` (at most 20 lines, no fences, no unchanged text), and a suggestion never changes the finding's severity or confidence. The whole-PR sweep is never asked and never keeps one. At the default `off` every prompt is byte-identical to the one sent without the key, a suggestion the model volunteers anyway is ignored, and the run record's `suggestions` is `null`; any value other than `off` or `on` exits `2`. A suggestion is only a request: after the other quality passes and before the gate, prxref clears (never drops the finding over) a suggestion that fails the first of these checks, in this order: `grouped` (the finding is a group representative), `line_moved` (line alignment moved it off the model's line), `file_level` (it has no line), `range` (the end is before the start, or the span is over 20 lines), `outside_hunk` (the lines are not all new-side lines of one hunk), `fence` (the text contains a code fence), `too_long` (over 4000 characters), `no_op` (the text equals the current lines). The run record and `--format json` carry `suggestions`, counted over the active findings: `kept`, and `cleared`, one count per reason in that order. A worker template override without a `{suggestion_example}` slot still gets the request block but not the example, and the run logs one WARNING naming the file. Suggestions lengthen the reply, so `on` raises `PRXREF_LLM_MAX_TOKENS` to `8192` when it is left unset; an explicit budget is respected as given.
