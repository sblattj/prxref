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

### Configuration Example

```bash
PRXREF_LLM_BACKEND=openai-compat
PRXREF_LLM_BASE_URL=https://llm.example.com/v1
PRXREF_LLM_API_KEY=$MY_ENDPOINT_KEY
PRXREF_LLM_MODELS=z-ai/glm-5.3-flash,openai/gpt-4.1-mini
```

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

### Configuration Example

```bash
PRXREF_LLM_BACKEND=litellm
PRXREF_LLM_MODELS=bedrock/anthropic.claude-3-7-sonnet-20250219-v1:0,vertex_ai/gemini-2.5-pro,openrouter/meta-llama/llama-3.3-70b-instruct
```

### Execution Details

- The first model in `PRXREF_LLM_MODELS` is used as the primary model.
- Remaining models in the list are passed to `litellm.completion` via the `fallbacks=` parameter.
- `num_retries=0` is enforced to ensure immediate failover to backup models without blocking retries on failed endpoints.
