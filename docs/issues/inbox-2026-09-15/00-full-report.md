
# prxref issue report — v0.11.0

Repo: `sblattj/prxref` · Version reviewed: **0.11.0** · Date: 2026-09-01
Reporter environment: macOS, Python 3.12, LLM backend = GitHub Copilot Business
(`api.business.githubcopilot.com`), models `claude-opus-5,claude-sonnet-5`,
forge = Bitbucket Server (native `bitbucket_server.py`).

Four PRs reviewed in one sitting on a TypeScript monorepo. Three distinct
problems surfaced, ordered by severity.

---

## 1. (Bug, high) Library-version-blind reasoning produces confident false
positives — and remediation that introduces the bug it alleges

### What happened

Reviewing a PR that migrates a Figma MCP proxy to Effect, prxref emitted a
`error` / confidence 0.75 finding:

> **Effect.promise turns callTool rejections into defects, breaking
> UnauthorizedError handling**
>
> `Effect.promise(() => client.callTool(...))` treats rejections as defects, and
> `Effect.runPromise` rejects with a wrapped FiberFailure rather than the
> original error. The subsequent `UnauthorizedError.isInstance(cause)` /
> `SdkHttpError.isInstance(cause)` / `cause instanceof TypeError` checks will
> never match, so cache invalidation and the 'run syf-mcp refresh' response are
> dead code. **Use `Effect.tryPromise` and unwrap the cause.**

That is correct for `effect@3.x`. The repository pins **`effect@4.0.0-rc.110`**
(visible in the repo's `package.json`, though not in the diff). In Effect 4,
`runPromise` rejects with the *original* error object, unwrapped.

### Reproduction

```bash
mkdir -p /tmp/effcheck && cd /tmp/effcheck
bun add effect@4.0.0-rc.110
```

```ts
// t.ts
import { Effect } from 'effect';

class UnauthorizedError extends Error {
  static isInstance(x: unknown): x is UnauthorizedError { return x instanceof UnauthorizedError; }
}
const boom = () => Promise.reject(new UnauthorizedError('nope'));

try { await Effect.runPromise(Effect.promise(() => boom())); }
catch (c) { console.log('Effect.promise    ->', (c as any)?.constructor?.name, UnauthorizedError.isInstance(c)); }

try { await Effect.runPromise(Effect.tryPromise(() => boom())); }
catch (c) { console.log('Effect.tryPromise ->', (c as any)?.constructor?.name, UnauthorizedError.isInstance(c)); }

// nested under scope + flatMap, matching the reviewed code's real shape
try {
  await Effect.runPromise(Effect.scoped(Effect.succeed({}).pipe(
    Effect.flatMap(() => Effect.promise(() => boom())))));
} catch (c) { console.log('nested defect     ->', (c as any)?.constructor?.name, UnauthorizedError.isInstance(c)); }
```

Output:

```
Effect.promise    -> UnauthorizedError true
Effect.tryPromise -> UnknownError      false
nested defect     -> UnauthorizedError true
```

### Why this is worse than an ordinary false positive

The code under review is correct. prxref's prescribed fix — switch to
`Effect.tryPromise` — is precisely the change that wraps the rejection in
`UnknownError` and makes `UnauthorizedError.isInstance(cause)` fail. A
maintainer who follows the advice **creates** the dead-code path the finding
claims to have found, silently disabling token-refresh handling.

The finding also drove the run's verdict to `Request-Changes`.

### Suggested fix

The reviewer prompt sees only the diff. It has no way to know which major
version of a dependency the repo is on, so it falls back to the version best
represented in training data. Options, roughly in order of cost:

1. **Inject resolved dependency versions into the chunk prompt.** For the files
   in a chunk, resolve the nearest manifest (`package.json`, `pyproject.toml`,
   `go.mod`, `Cargo.toml`) from the PR head and include a short
   `name@version` list for packages actually imported by the changed files. A
   few dozen tokens; directly prevents this failure class.
2. **Add a prompt rule:** when a finding depends on the runtime semantics of a
   third-party library, and the version is not visible in the provided context,
   cap confidence and phrase it as a question rather than an assertion.
3. **Treat "behavioral claim about a library API" as a distinct finding kind**
   in the quality gate, with a higher confidence floor than diff-local logic
   findings.

Option 1 is the real fix; 2 is a cheap mitigation that would have demoted this
from a blocking `error` to a comment.

---

## 2. (Recall, medium) The 0.11.0 systemic sweep produced zero findings on its
own advertised class

The same PR deleted three upstream-input guards outright:

```
-const MAX_TOOL_NAME_LENGTH = 128;
-const MAX_TOOL_DESCRIPTION_LENGTH = 4_000;
-const MAX_TOOL_SCHEMA_BYTES = 100_000;
```

...along with the `isSupported()` filter that enforced them, on data supplied by
a remote third-party MCP server. Nothing replaced them. The sweep ran on all
four PRs (`[sweep] 0 findings` each time, 16–3503 char digests) and reported
nothing.

The 0.11.0 changelog sells the sweep as hunting exactly this: "unbounded input,
entry points, guard removal." Two observations that may explain the miss:

- The digest is built from **added/removed lines matching six high-signal
  patterns**. A deleted `const MAX_TOOL_NAME_LENGTH = 128;` is a removed line
  with no env/secret/auth/entry-point keyword in it, so it likely never entered
  the digest. Consider adding a pattern for removed `const`/`#define`-style
  numeric limits and removed guard functions (`isValid*`, `isSupported*`,
  `assert*`, `check*`, `validate*`).
- More generally, the sweep appears to weight *added* lines. Guard removal is a
  pure-deletion vulnerability class and needs deletion-side patterns to be seen.

**Caveat, in fairness to the tool:** on this specific PR the guard removal was
an intentional, already-litigated decision — the PR carries a human thread at
`figma.ts:27` ("This still feels too defensive, I don't think we need it imo")
ending in "Removed the defensive tool metadata guard in commit 917d1f4". A
finding re-raising it would have been noise on this PR. The structural point
stands (deletion-side patterns are missing from the digest), but this example
is not proof of lost value — and it suggests a second improvement: the sweep
should read existing PR discussion before raising a guard-removal finding,
since prxref already fetches PR activities for stale-comment pruning.

Sweep token cost was real (1878–3585 input tokens per PR) with zero yield across
four PRs, so this is a cost/benefit regression as currently tuned, at least on
this corpus.

---

## 3. (Bug, medium) Impact scoping understated on a true positive

Same PR, second finding — this one correct:

> **decodeUnknownSync on tool.inputSchema throws and aborts all tool
> registration** ... "prevents registration of every remaining Figma tool."

Verified: `Schema.decodeUnknownSync(Schema.declare(Predicate.isObject))` throws
`SchemaError` on a string, `null`, or an array (`Predicate.isObject` is false
for arrays and functions in Effect 4).

The scope is understated. The throwing call sits in a `for` loop inside
`register(server)`, which is called from `serverFactory()` **after** three other
tool families have registered. The exception escapes `serverFactory`, so the
whole MCP handler fails to construct — not just the Figma tools. A reader
triaging by the stated impact ("some Figma tools missing") would deprioritize a
total-outage bug.

Suggestion: when a finding is inside a loop or a factory, ask the model to state
where the exception is caught, or say explicitly that it is uncaught. "Throws"
without a containment boundary is not actionable.

---

## 4. (UX, low) `--no-post` makes findings unreadable

`prxref review --pr-url ... --no-post -v` prints:

```
verdict: Request-Changes
counts: 2 (dropped: 0)
elapsed: 17.3s tokens: 22712+1318
```

The finding titles, bodies, files, and lines are never rendered to stdout in any
mode. On a dry run they go nowhere at all — the only way to read what the
reviewer found is to bypass the CLI:

```python
from prxref.cli import _run_review
from prxref.formatter import format_summary, format_inline_comment, build_attribution
res = _run_review(url, post=False)
print(format_summary(res["verdict"], res["findings_active"], res["findings_dropped"], ...))
```

Dry run is the mode operators use to evaluate the tool before pointing it at a
busy repo, and it is the mode with the least output. Request: a
`--print`/`--format {text,md,json}` flag that renders the same summary +
inline bodies to stdout, or make `--no-post` imply it.

---

## 5. (Efficiency, low) A rejected model is re-attempted on every chunk

With `PRXREF_LLM_MODELS=claude-opus-4.6,claude-sonnet-5` where the first model
had been deprovisioned server-side, every chunk *and* the sweep independently
tried `claude-opus-4.6`, took `HTTP 400`, and fell back:

```
WARNING llm attempt 1/2 failed: model=claude-opus-4.6 HTTP 400 after 380ms
INFO    llm attempt 2/2: model=claude-sonnet-5 ...
WARNING llm attempt 1/2 failed: model=claude-opus-4.6 HTTP 400 after 127ms   # sweep, same run
```

A 400 of the form "model is not available / not supported" is a property of the
run, not of the chunk. Suggest caching that per-run and skipping the model for
subsequent units — the retry budget is then spent on transient failures, which
is what it is for.

---

## 6. (Consistency, medium) The finding set is not stable across identical runs,
and the run-only finding was also a false positive

The same PR, same `PRXREF_LLM_MODELS=claude-opus-5,claude-sonnet-5`, same commit,
two runs minutes apart:

| Run | Findings | Verdict |
|---|---|---|
| 1 (`--no-post`) | 2 error | Request-Changes |
| 2 (posting) | 2 error + **1 warning** | Request-Changes |

The two errors were identical in both runs. The extra warning appeared only in
run 2:

> 🟧 **Per-request Figma resources no longer released after response**
> (`src/apps/mcp/src/services/mcp-server.ts:37`) — "nothing calls figma cleanup
> at end of request ... that lease is only released when the enclosing scope
> (service layer) closes, not per request."

Also a false positive. Verified `ScopedCache` on `effect@4.0.0-rc.110`:

```
after request 1 scope closed: opened=1 closed=0
after request 2 scope closed: opened=1 closed=0   <- reused, not reopened
after invalidate:             opened=1 closed=1
```

A per-call `Effect.scoped(ScopedCache.get(...))` releases the lease at scope
close without closing the pooled client; the finalizer runs on invalidate, TTL
expiry, capacity eviction, or layer shutdown. That is connection pooling
working as designed, described as a leak. `prepare()` returns `{ register }`
and holds no lease, so nothing is pinned to the service-layer scope; and the
pre-change code kept the client open after the response too (a 5-minute idle
timer plus lease counting), so "no longer released after response" is not the
delta.

Two asks:

1. Temperature and/or sampling for the review pass should be pinned so an
   operator comparing runs is comparing the tool, not the dice. If it is
   already 0, say so in the docs, because the observed behaviour is not what a
   reader would predict.
2. The quality gate let a hedged finding through at `warning` — the body itself
   says *"If figmaProxy.prepare still leases a client"*. A finding whose text
   contains its own unverified precondition should be demoted or dropped, not
   posted as a claim.

Precision across the two runs: 1/2 then 1/3.

### Addendum, 2026-09-02: a second corpus, and the failure is worse than variance

A six-PR pass over the same repository produced a third data point on the same
PR family and two new defect classes.

**Instability is not confined to low-value findings.** On a 36-file PR
(`feat: add Splunk tools`), three runs over one unchanged commit gave **7, then
2** active findings. The one that disappeared was the most valuable of the set:

> 🟧 **Credential-store fallback grants ambient creds to any client** —
> `auth.ts` falls back to `~/.syf-mcp/credentials.json` when auth headers are
> absent, and the server is started with
> `NodeHttpServer.layer(() => createServer(), { port })` — no `host`, so Node
> binds every interface. Any caller reaching the port executes ServiceNow and
> Splunk calls with the operator's cookies.

Verified true, and absent from the run that actually posted. An operator who
runs prxref once and reads the posted comments gets no signal about the one
security-relevant issue in the PR. Whatever the fix for item 6 is, it needs to
account for high-severity findings being the ones that vanish.

**New defect class A — reasoning from an identifier's name when the definition
is in the same file, outside the hunk.**

> 🟧 `start` pagination param typed `PositiveInt` rejects 0 — "the first page
> value 0 would be rejected by schema validation."

The definition, 78 lines above in the same file, is
`Schema.Number.check(Schema.isInt(), Schema.isBetween({ minimum: 0, ... }))` —
inclusive, so `0` validates. Confirmed on `effect@4.0.0-rc.110`: `0 ->
accepted`, `-1 -> REJECTED`. The name is misleading, the code is correct, and
prxref trusted the name. Same root cause as item 1 (reasoning from a symbol
whose definition it was not given), so the fix is related: when a finding turns
on the semantics of a named local symbol, pull that symbol's definition into
the chunk context, or cap the finding's confidence.

**New defect class B — git rename/copy headers read as deletions.**

> 🟧 `src/packages/splunk/package.json:2` — "ServiceNow package.json removed by
> rename to splunk"

The diff header is `similarity index 53% / copy from src/packages/servicenow/
package.json / copy to src/packages/splunk/package.json` — a **copy**.
`src/packages/servicenow/package.json` is present on the branch (HTTP 200,
`"name": "@syf-mcp/servicenow"`) and is separately modified in the same diff.
The parser appears to treat the `copy from` source path as removed. A finding
asserting a file was deleted should be checked against the post-image tree
before it is raised.

**Recall, again, on the highest-value item of the pass.** On a release PR
(`release/v0.4.0 -> develop`) prxref returned zero findings. The branch in fact
carried the commits of a still-open, still-under-review PR — `json-file.ts` is
byte-identical to the open PR's branch and differs from `develop` — so merging
the release would land unreviewed code and bypass an open review comment. This
is diff-visible (the release diff touches source files that no consumed
changeset accounts for) and is exactly what a release PR should be checked for.
Suggestion: a release-shaped PR (only version bumps, CHANGELOGs, lockfile,
consumed changesets) that also touches source files is worth a finding on its
own.

Precision over the second corpus: 8 findings verified, 2 false
(`PositiveInt`, `package.json removed`), 6 true — with the caveat that the
true/false split changes run to run.

---

## Summary of asks

| # | Type | Ask |
|---|---|---|
| 1 | Bug | Put resolved dependency versions in the chunk prompt; cap confidence on unverifiable library-semantics claims |
| 2 | Recall | Add deletion-side patterns (removed limits/guards) to the systemic sweep digest; check existing PR discussion before re-raising a settled decision |
| 3 | Bug | Require findings to state the exception containment boundary |
| 4 | UX | Render findings to stdout on dry runs (`--print` / `--format`) |
| 5 | Perf | Cache per-run "model unavailable" 400s instead of retrying per chunk |
| 6 | Consistency | Pin sampling so runs are comparable; drop findings that state their own unverified precondition |

Item 1 is the one that matters: as it stands, prxref can block a correct PR and
hand the author a patch that breaks it.


