# LLM Backends & Failover Architecture

`prxref` connects to LLM inference endpoints using two interchangeable backends: a lightweight OpenAI-compatible plain-HTTP client or an optional in-process `litellm` wrapper. There is no default endpoint and no default model chain — `PRXREF_LLM_BASE_URL` and `PRXREF_LLM_MODELS` are required, and leaving either unset raises `ConfigError` (`prxref review` exits `2`).

## Key Architectural Principles

1. **No Provider Credentials in `prxref`:** `prxref` reads no third-party cloud provider credentials (no AWS IAM keys, no OpenAI keys, no Google Cloud keys, and no Anthropic API keys). All provider credentials, quota pools, and upstream authentication live securely behind the inference proxy endpoint.
2. **Fast Caller-Side Failover:** Fallback is implemented as a fast sequential loop over the model list. If a model encounters HTTP 429 (rate limit), HTTP >= 500 (server/upstream error), connection failures, or timeouts, the client immediately advances to the next model in the chain without same-model retries.

---

## Primary Backend: OpenAI-compatible HTTP

The primary and default backend communicates via plain HTTP requests with any OpenAI-compatible `/v1/chat/completions` server — a hosted router (OpenRouter, Together, Groq), a self-hosted gateway such as `llm-ferry`, or a local runtime such as vLLM or Ollama.

- **Default Backend Alias:** `PRXREF_LLM_BACKEND=openai-compat` (aliases: `ferry`, `http`).
- **Endpoint URL:** `PRXREF_LLM_BASE_URL=https://llm.example.com/v1`. Required; there is no default.
- **API Key:** `PRXREF_LLM_API_KEY` (sent as `Authorization: Bearer <key>`). Optional — leave empty for a local no-auth server.
- **Models:** Model names are whatever the endpoint accepts, listed cheapest first. Required; there is no default.

### Request Budget

Four variables shape the request itself. All are optional, and a bad value exits `2` naming the variable rather than degrading the review. `PRXREF_LLM_MAX_TOKENS`, `PRXREF_LLM_TIMEOUT`, and `PRXREF_LLM_SEED` are range-checked when the config loads; `PRXREF_LLM_TEMPERATURE` is validated later, when the LLM client is constructed (`create_llm_client`) — same operator-visible outcome, just a later checkpoint.

| Variable | Default | Effect on the request |
|---|---|---|
| `PRXREF_LLM_MAX_TOKENS` | `4096` | `max_tokens` on every worker call. Must be > 0. This is a per-call budget threaded config → orchestrator → reviewer → `invoke`; the client never reads it. |
| `PRXREF_LLM_TIMEOUT` | `45.0` | The client's default request timeout, in seconds. Must be > 0. It is a **per-model** deadline: a model that exceeds it is abandoned and the next in the chain is tried immediately, so a chain of three can take up to three timeouts. |
| `PRXREF_LLM_TEMPERATURE` | `0.0` (sent) | `temperature` in the payload. Must be finite and >= 0; no upper bound, since the maximum is provider-specific. Unset or empty sends the default `0.0` rather than omitting the field, so an identical diff reviews identically by default; a set value wins. `PRXREF_LLM_REASONING_EFFORT` keeps its own pass-through-unvalidated rule. |
| `PRXREF_LLM_SEED` | *(omitted)* | Top-level `seed` in the payload, OpenAI-compatible backends and `litellm` alike. Must be an integer >= 0 (`0` is a valid seed). Unset omits the field **entirely**, leaving the provider's own seed behaviour in place. |

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

---

## Optional Backend: `litellm`

For environments running without a centralized inference gateway, `prxref` supports in-process multi-provider routing via `litellm`.

- **Backend Setting:** `PRXREF_LLM_BACKEND=litellm`
- **Installation:** `pip install 'prxref[litellm]'`
- **Shared settings:** `PRXREF_LLM_MAX_TOKENS`, `PRXREF_LLM_TIMEOUT`, `PRXREF_LLM_TEMPERATURE`, and `PRXREF_LLM_SEED` apply here too — temperature resolves to the same `0.0` default when unset, and a configured seed is passed as `seed=` to `litellm.completion`. `PRXREF_LLM_REASONING_EFFORT` is openai-compat only.

### Configuration Example

```bash
PRXREF_LLM_BACKEND=litellm
PRXREF_LLM_MODELS=bedrock/anthropic.claude-3-7-sonnet-20250219-v1:0,vertex_ai/gemini-2.5-pro,openrouter/meta-llama/llama-3.3-70b-instruct
```

### Execution Details

- The first model in `PRXREF_LLM_MODELS` is used as the primary model.
- Remaining models in the list are passed to `litellm.completion` via the `fallbacks=` parameter.
- `num_retries=0` is enforced to ensure immediate failover to backup models without blocking retries on failed endpoints.

---

## Worker Prompt Context

Each worker sees one chunk's unified diff, trimmed to `PRXREF_CHUNK_CONTEXT_LINES` lines around every change. Two optional blocks are appended after the diff to answer the questions the diff alone cannot.

### Dependency versions and definitions

- **`### Dependency versions`** — `name@version` for each third-party package the chunk's *added* lines import, resolved from the nearest manifest walking up from each changed file to the repository root: `package.json` (`dependencies` + `devDependencies`), `pyproject.toml` (`[project] dependencies` and `[tool.poetry.dependencies]`), `go.mod` `require` lines, and `Cargo.toml` `[dependencies]`. Only imported packages appear. Relative and `node:` specifiers, Python stdlib modules, and relative Python imports are excluded. Without this block a reviewer answers library semantics from whichever major dominates its training data.
- **`### Definitions referenced by this chunk`** — for identifiers used on added lines whose definition sits in the same file but *outside* the rendered hunk, one `path:line: definition` entry each, taken from the file as served at the PR head. The entry is the defining line plus continuation lines up to a balanced bracket or 6 lines. Caps: at most 40 entries and 8000 characters, with a trailing `… N more definitions omitted` when trimmed; files over 512 KiB are skipped. Definitions the chunk itself adds are never repeated.

Both blocks are **best effort**. They are built from an optional forge method, `get_file_content(ref, path, *, sha) -> str | None`, resolved with `getattr` and always called at the PR head sha (`pr.source_sha`). Every read is cached per run, so one manifest is fetched once no matter how many chunks want it, and any exception from the adapter degrades to no block. A forge that does not implement the method — and a PR with no head sha — reviews exactly as before, with no header and no extra requests. Nothing here can fail a review.

The worker prompt also carries two confidence rules tied to these blocks: a finding that depends on third-party runtime semantics whose version is not listed, or on the semantics of a symbol whose definition is not shown, must cap confidence at 0.5 and be phrased as a question.

On the timeout retry — the one deterministic re-run with `context_lines=0` — the dependency block is kept and the definitions block is dropped, because shrinking the prompt is the entire point of that retry.
