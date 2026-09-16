
**Title:** Systemic sweep returns nothing on guard removal (its advertised class) and re-raises settled decisions

**Labels:** recall, sweep, cost

**Version:** prxref 0.11.0 · `claude-opus-5`

---

## Summary

The 0.11.0 sweep is sold as hunting "unbounded input, entry points, guard
removal". Across a six-PR pass it produced **zero findings on every PR**, at
1878–3585 input tokens each, including on a PR that deleted three input-size
guards outright.

## Evidence

```
-const MAX_TOOL_NAME_LENGTH = 128;
-const MAX_TOOL_DESCRIPTION_LENGTH = 4_000;
-const MAX_TOOL_SCHEMA_BYTES = 100_000;
```

...along with the `isSupported()` filter enforcing them, on data supplied by a
remote third-party MCP server. Sweep output on that PR: `[sweep] 0 findings`,
digest 3503 chars.

## Likely cause

The digest is assembled from added/removed lines matching six high-signal
patterns. A removed `const MAX_TOOL_NAME_LENGTH = 128;` carries no
env/secret/auth/entry-point keyword, so it probably never entered the digest.
Guard removal is a **pure-deletion** vulnerability class and needs deletion-side
patterns to be visible at all.

## Suggested fix

1. Add deletion-side patterns: removed numeric limit constants, and removed
   functions matching `isValid*`, `isSupported*`, `assert*`, `check*`,
   `validate*`, `sanitiz*`, `escape*`.
2. **Before raising a guard-removal finding, read the PR discussion.** On the PR
   above, the removal was deliberate and already argued out in a thread on the
   same file ("This still feels too defensive, I don't think we need it imo" →
   "Removed the defensive tool metadata guard in commit 917d1f4"). prxref already
   fetches PR activities for stale-comment pruning, so the data is in hand. A
   finding that re-litigates a settled thread is noise even when technically
   correct.

Point 2 is why this is filed as one issue rather than two: making the sweep see
deletions without also making it read the discussion would trade silence for
noise.


