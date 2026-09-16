
**Title:** Finding reasons from an identifier's name when its definition is in the same file, outside the diff hunk

**Labels:** bug, accuracy, context

**Version:** prxref 0.11.0 · `claude-opus-5` · Bitbucket Server forge

---

## Summary

A finding asserted a validation failure based on what a constant is *called*,
not what it *is*. The definition sat 78 lines above in the same file, outside
the hunk supplied to the chunk worker.

## The finding as posted

> 🟧 **start pagination param typed PositiveInt rejects 0** (confidence 0.7)
>
> `start` is the 0-based pagination index (default `opts.start ?? 0` in
> bitbucket.ts) but is validated as `PositiveInt`, so the first page value 0
> would be rejected by schema validation. Same for the projects schema.

## Why it is wrong

`PositiveInt` in that file is not positive-only:

```ts
// src/packages/bitbucket/src/schema.ts:18
const PositiveInt = Schema.Number.check(
  Schema.isInt(),
  Schema.isBetween({ minimum: 0, maximum: Number.MAX_SAFE_INTEGER }),
);
```

`Schema.isBetween` is inclusive. Verified against the pinned
`effect@4.0.0-rc.110`:

```
0    -> accepted
1    -> accepted
25   -> accepted
-1   -> REJECTED (Expected a value between 0 and 9007199254740991)
1.5  -> REJECTED (Expected an integer)
```

`start: 0` validates, and the first page works. The identifier is misleading —
`NonNegativeInt` would be the honest name — but that is a naming nit and it is
pre-existing, not introduced by the reviewed diff.

## Impact

A confident, plausible, wrong finding on a PR that had no other issues, posted
as the only comment. A reviewer trusting it would send the author chasing a
non-bug.

## Suggested fix

When a finding turns on the semantics of a named symbol defined in the repo,
resolve that symbol before raising it. Cheapest useful version: if a finding's
body names an identifier that is *referenced but not defined* in the chunk,
grep the same file (then the same package) for its definition and add it to the
prompt; if it cannot be resolved, cap the confidence.

Same root cause as the third-party-version issue filed separately — the
reviewer asserting facts about symbols it was never shown.


