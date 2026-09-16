
**Title:** Findings assert third-party library semantics from the wrong major version, and prescribe a fix that introduces the bug

**Labels:** bug, accuracy, prompt

**Version:** prxref 0.11.0 · backend: GitHub Copilot Business (`api.business.githubcopilot.com`) · models `claude-opus-5,claude-sonnet-5` · forge: Bitbucket Server

---

## Summary

The chunk prompt contains only the diff, so a finding that turns on a library's
runtime semantics is answered from whichever major version dominates training
data. On a repo pinned to `effect@4.0.0-rc.110` this produced an `error`-severity
finding at confidence 0.75 that was correct for Effect 3, wrong for Effect 4,
and whose prescribed remediation would have *created* the bug it alleged.

## The finding as posted

> **Effect.promise turns callTool rejections into defects, breaking
> UnauthorizedError handling**
>
> `Effect.promise(() => client.callTool(...))` treats rejections as defects, and
> `Effect.runPromise` rejects with a wrapped FiberFailure rather than the
> original error. The subsequent `UnauthorizedError.isInstance(cause)` /
> `SdkHttpError.isInstance(cause)` / `cause instanceof TypeError` checks will
> never match, so cache invalidation and the 'run syf-mcp refresh' response are
> dead code. **Use `Effect.tryPromise` and unwrap the cause.**

## Reproduction

```bash
mkdir -p /tmp/effcheck && cd /tmp/effcheck && bun add effect@4.0.0-rc.110
```

```ts
import { Effect } from 'effect';

class UnauthorizedError extends Error {
  static isInstance(x: unknown): x is UnauthorizedError { return x instanceof UnauthorizedError; }
}
const boom = () => Promise.reject(new UnauthorizedError('nope'));

try { await Effect.runPromise(Effect.promise(() => boom())); }
catch (c) { console.log('Effect.promise   ', (c as any)?.constructor?.name, UnauthorizedError.isInstance(c)); }

try { await Effect.runPromise(Effect.tryPromise(() => boom())); }
catch (c) { console.log('Effect.tryPromise', (c as any)?.constructor?.name, UnauthorizedError.isInstance(c)); }

try {
  await Effect.runPromise(Effect.scoped(Effect.succeed({}).pipe(
    Effect.flatMap(() => Effect.promise(() => boom())))));
} catch (c) { console.log('nested defect    ', (c as any)?.constructor?.name, UnauthorizedError.isInstance(c)); }
```

**Actual output:**

```
Effect.promise    UnauthorizedError true
Effect.tryPromise UnknownError      false
nested defect     UnauthorizedError true
```

## Impact

- The reviewed code is correct; the finding drove the run's verdict to
  `Request-Changes`.
- Following the prescribed fix wraps the rejection in `UnknownError`, at which
  point `isInstance` genuinely fails and the token-refresh path genuinely dies.
  The remediation is worse than the non-existent bug.

## Suggested fix

1. **Inject resolved dependency versions into the chunk prompt.** For the files
   in a chunk, resolve the nearest manifest (`package.json`, `pyproject.toml`,
   `go.mod`, `Cargo.toml`) from the PR head and include `name@version` for
   packages the changed files import. Costs a few dozen tokens.
2. **Prompt rule:** when a finding depends on third-party runtime semantics and
   the version is not in context, cap confidence and phrase it as a question.
3. **Quality gate:** treat "behavioural claim about a library API" as its own
   finding kind with a higher confidence floor than diff-local logic findings.

Related: the same missing-context root cause with an in-repo symbol is filed
separately (identifier-name reasoning).


