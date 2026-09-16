
**Title:** A model that returns "not available" 400 is retried on every chunk and on the sweep

**Labels:** performance, llm, low

**Version:** prxref 0.11.0 · Copilot Business backend

---

## Summary

With `PRXREF_LLM_MODELS=claude-opus-4.6,claude-sonnet-5`, where the first model
had been deprovisioned server-side, every chunk *and* the sweep independently
tried it, took `HTTP 400`, and fell back:

```
WARNING llm attempt 1/2 failed: model=claude-opus-4.6 HTTP 400 after 380ms
INFO    llm attempt 2/2: model=claude-sonnet-5 ...
WARNING llm attempt 1/2 failed: model=claude-opus-4.6 HTTP 400 after 127ms   # sweep, same run
```

The 400 body is explicit and permanent for the run:

```json
{"error":{"message":"The requested model is not available for integrator \"opencode\". Available models: [...]"}}
```

## Impact

Wasted round trips scaling with chunk count, and the two-attempt retry budget is
consumed by a deterministic failure rather than kept for transient ones. On a
9-chunk PR that is 10 pointless requests.

## Suggested fix

Cache "model unavailable / not supported" per run and skip that model for
subsequent units. Optionally warn once at startup rather than per chunk.


