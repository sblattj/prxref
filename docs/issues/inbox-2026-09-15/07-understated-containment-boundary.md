
**Title:** "This throws" findings do not state where the exception is caught, and systematically understate blast radius

**Labels:** accuracy, prompt, severity

**Version:** prxref 0.11.0 · `claude-opus-5`

---

## Summary

A correct finding described a total-outage bug as a partial one, because it
stopped at the throwing line instead of walking out to the nearest `catch`.

## The finding as posted

> 🟥 **decodeServerJsonSchema throws during register for non-object inputSchema**
>
> ...any tool whose `inputSchema` is not an object throws synchronously,
> **aborting registration of all remaining Figma tools** instead of skipping
> that one tool.

## Actual scope

The throwing call is in a `for` loop inside `register(server)`, invoked from
`serverFactory()` **after** three other tool families have already registered.
Nothing catches it, so the exception escapes `serverFactory` and MCP handler
construction fails outright — Jira, BitBucket and Confluence tooling go down
with Figma.

(The finding itself was true and led to a real fix — `Schema.decodeUnknownOption`
plus `continue` — so this is about scoping, not correctness.)

## Impact

A maintainer triaging by stated impact reads "some Figma tools missing" and
deprioritises a full-server outage.

## Suggested fix

Require any finding asserting a throw / panic / unhandled rejection to name the
containment boundary: the enclosing `catch`, or an explicit "uncaught, propagates
to <caller>". "Throws" without a boundary is not actionable, and the severity
assigned to it is arbitrary.

---

## Addendum — 0.11.1: the error also runs the other way (overstatement)

Same root cause (the finding never resolves the enclosing `catch`), opposite
direction: containment **overstated into a crash that does not happen**.

`GAIENG/syf-mcp` PR #45 at head `f05e179`, finding on
`src/packages/jenkins/src/path.ts:31`:

> safeSegment/parseBuildUrl throw TypeError synchronously, and jobPath is called
> inside Effect.fn generators (read.ts listJobs/getJob/listBuilds). A thrown
> error there becomes an unhandled defect rather than a JenkinsError, so bad
> user-supplied paths **crash the fiber** instead of returning a tool error.

Everything up to the last clause is correct and was verified against the pinned
`effect@4.0.0-rc.110`. The last clause is wrong. The single consumer of these
operations is `src/apps/mcp/src/tools/jenkins.ts:76`, whose handler is invoked by
`@modelcontextprotocol/server@2.0.0`, which wraps the executor
(`dist/mcp-DXXb3Vv3.mjs:1400-1407`):

```js
const result = await this.executeToolHandler(tool, args, ctx);
...
} catch (error) {
  return this.createToolError(error instanceof Error ? error.message : String(error));
}
```

The caller therefore receives `{ content: [{ text: "Jenkins path traversal is not
allowed" }], isError: true }` — a tool error, which is precisely what the finding
says does not happen. No fiber crash is observable, no unhandled rejection, and
the traversal guard fails closed on every input tested.

The finding remains worth acting on (a defect is outside the package's typed-error
contract), but its severity as written is inflated: "crashes on bad input" reads
as a blocker, and it is not one.

**Why this strengthens the original request.** A boundary requirement that only
guards against *understatement* would not have caught this. The rule should be
symmetric: a finding asserting a throw must name the containment boundary and
state the observed outcome at that boundary — including when the boundary
handles it cleanly and the correct severity is therefore lower.


