# LLM Backends & Failover Architecture

`prxref` connects to LLM inference endpoints using two interchangeable backends: a lightweight OpenAI-compatible plain-HTTP client (`llm-ferry`) or an optional in-process `litellm` wrapper.

## Key Architectural Principles

1. **No Provider Credentials in `prxref`:** `prxref` reads no third-party cloud provider credentials (no AWS IAM keys, no OpenAI keys, no Google Cloud keys, and no Anthropic API keys). All provider credentials, quota pools, and upstream authentication live securely behind the inference proxy endpoint.
2. **Fast Caller-Side Failover:** Fallback is implemented as a fast sequential loop over the model list. If a model encounters HTTP 429 (rate limit), HTTP >= 500 (server/upstream error), connection failures, or timeouts, the client immediately advances to the next model in the chain without same-model retries.

---

## Primary Backend: `llm-ferry` (`openai-compat`)

The primary and default backend communicates via plain HTTP requests with the `llm-ferry` local daemon or any OpenAI-compatible `/v1/chat/completions` server.

- **Default Backend Alias:** `PRXREF_LLM_BACKEND=openai-compat` (aliases: `ferry`, `http`).
- **Endpoint URL:** `PRXREF_LLM_BASE_URL=http://<host>.local:8090/v1` (defaults to `http://127.0.0.1:8090/v1`).
- **API Key:** `PRXREF_LLM_API_KEY=local` (sent as `Authorization: Bearer local`).
- **Model Lanes:** Model names correspond to configured lanes on the daemon (e.g. `flash`, `orch`).

### Configuration Example

```bash
PRXREF_LLM_BACKEND=openai-compat
PRXREF_LLM_BASE_URL=http://mac-studio.local:8090/v1
PRXREF_LLM_API_KEY=local
PRXREF_LLM_MODELS=flash,orch
```

### Failover Semantics

The client iterates through `PRXREF_LLM_MODELS` in left-to-right priority order (e.g., cheap/fast lane `flash` first, escalating to `orch` on failure):
- A model attempt fails if it encounters:
  - Request timeout
  - Connection/network error
  - HTTP status code `>= 400` (including HTTP 429 and 5xx)
  - Malformed JSON response
- On any failure, `OpenAICompatClient` logs the model error and tries the next model in the chain immediately.
- If all configured models fail, `LLMError` is raised containing the per-model failure reasons.

---

## Optional Backend: `litellm`

For environments running without a centralized ferry daemon, `prxref` supports in-process multi-provider routing via `litellm`.

- **Backend Setting:** `PRXREF_LLM_BACKEND=litellm`
- **Installation:** `pip install 'prxref[litellm]'`

### Configuration Example

```bash
PRXREF_LLM_BACKEND=litellm
PRXREF_LLM_MODELS=bedrock/anthropic.claude-3-7-sonnet-20250219-v1:0,vertex_ai/gemini-2.5-pro,openrouter/meta-llama/llama-3.3-70b-instruct
```

### Execution Details

- The first model in `PRXREF_LLM_MODELS` is used as the primary model.
- Remaining models in the list are passed to `litellm.completion` via the `fallbacks=` parameter.
- `num_retries=0` is enforced to ensure immediate failover to backup models without blocking retries on failed endpoints.
