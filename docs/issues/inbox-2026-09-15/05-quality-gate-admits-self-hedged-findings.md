
**Title:** Quality gate admits findings whose own text states an unverified precondition

**Labels:** bug, quality-gate, precision

**Version:** prxref 0.11.0 · `claude-opus-5`

---

## Summary

A finding was posted at `warning` severity whose body contains its own escape
hatch — "If X still leases a client". The precondition was false, so the finding
was false. Text of that shape is the model signalling it could not resolve
something, and it should not survive the quality gate as an assertion.

## The finding as posted

> 🟧 **Per-request Figma resources no longer released after response**
> (`src/apps/mcp/src/services/mcp-server.ts:37`)
>
> The diff removes the close()/stream-wrapper lifecycle and returns
> createMcpHandler(serverFactory) directly, so nothing calls figma cleanup at
> end of request. **If figmaProxy.prepare still leases a client**, that lease is
> only released when the enclosing scope (service layer) closes, not per
> request.

## Why it is wrong

`prepare()` returns `{ register }` and holds no lease. Per-call
`Effect.scoped(ScopedCache.get(...))` releases at scope close without closing
the pooled client. Verified on `effect@4.0.0-rc.110`:

```
after request 1 scope closed: opened=1 closed=0
after request 2 scope closed: opened=1 closed=0   <- reused, not reopened
after invalidate:             opened=1 closed=1
```

That is connection pooling working as designed, reported as a leak. The
pre-change code also kept the client alive after the response (a 5-minute idle
timer plus lease counting), so "no longer released after response" is not even
the delta.

## Suggested fix

Add a gate rule: a finding whose body contains a conditional about the code's
own behaviour ("if", "assuming", "unless", "may still") either gets demoted to a
question or dropped. The model has already told you it is guessing.


