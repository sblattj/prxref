
**Title:** Lockfile finding crosses the `devDependencies` block boundary and anchors to a phantom line

**Labels:** bug, accuracy, false-positive

**Version:** prxref 0.12.1 · `claude-opus-5` · Bitbucket Server forge

---

## Summary

A `warning` finding claimed that build/test tooling (`vitest`, `tsup`, `tsx`,
`typescript`) was declared under **runtime `dependencies`** for `@syf-mcp/mcp`. It
is not: every one of those packages is inside the adjacent `devDependencies`
block, correctly. The tool read past the `"devDependencies": {` delimiter and
attributed that block's entries to the preceding `dependencies` block, then
anchored the comment to a line number (`bun.lock:47`) that belongs to neither
named package. Pure false positive, and it cleared the confidence floor to post.

## The finding as posted

> 🟧 **[WARNING] vitest and tsup listed under runtime dependencies for
> @syf-mcp/mcp** (`bun.lock:47`)
> The `@syf-mcp/mcp` workspace entry adds `"vitest": "^4.1.10"` … under
> `dependencies`, shipping test/build tooling as a runtime dependency.

## Ground truth (same commit, `db2044b7`)

`bun.lock`, the `src/apps/mcp` workspace entry — the two blocks are explicitly
labelled and non-overlapping:

```jsonc
"src/apps/mcp": {
  "name": "@syf-mcp/mcp",
  "dependencies": {                      // lines 45–58
    "@effect/platform-node": "...",
    "@modelcontextprotocol/client": "^2.0.0",   // <-- line 47, what the anchor points at
    ...
    "bun": "1.3.14",
    "effect": "4.0.0-rc.110",
  },
  "devDependencies": {                   // lines 59–71
    ...
    "tsup": "^8.5.0",
    "tsx": "^4.20.3",
    "typescript": "^7.0.2",
    "vitest": "^4.1.10",                 // <-- line 70, where vitest actually lives
  },
},
```

The authoritative `src/apps/mcp/package.json` agrees exactly: `dependencies`
holds only runtime packages (`effect`, the MCP SDK, the `@syf-mcp/*` workspaces,
`bun`); `vitest`/`tsup`/`tsx`/`typescript` are all under `devDependencies`. The
sole runtime-dependency change this PR makes is adding `@syf-mcp/nexus-iq`.

Two independent errors compound here:

1. **Block-boundary misparse.** The finding names line 47 but describes line 70's
   content, i.e. it treated the `dependencies` and `devDependencies` blocks as one
   list. A JSON/lockfile finding must respect the object key that scopes the
   entries it cites.
2. **Phantom anchor.** `bun.lock:47` is `@modelcontextprotocol/client`, which is
   neither `vitest` nor `tsup` nor any package the finding discusses. The line
   number was not derived from the entry being flagged, so it cannot be trusted to
   point a reviewer at the alleged problem.

## Impact

- A confident, specific, wrong dependency-hygiene warning on a structured file
  whose ground truth is one `grep` away. It reads as authoritative ("shipping test
  tooling to production") and would send an author chasing a non-existent fix.
- The anchor points at an unrelated, correct dependency, so anyone trusting the
  line number is doubly misled.
- Structured-file findings (lockfiles, `package.json`, manifests) are exactly the
  class where the tool should be *most* reliable, because the answer is
  deterministic and local — no library-semantics or cross-file reasoning needed.
  Getting this wrong signals the diff is being read as flat text without tracking
  the enclosing object key.

## Suggested fix

1. For findings that turn on which section of a structured file an entry lives in,
   parse the file (or the hunk) as JSON and resolve the entry's **enclosing key
   path** (`dependencies` vs `devDependencies`) rather than inferring section from
   proximity in the diff text.
2. Require that a finding's cited line actually contain the token it names; reject
   or re-anchor a finding whose anchor line does not match its subject. A phantom
   anchor is a cheap, mechanical thing to gate on.
3. Add a regression eval: a `bun.lock`/`package.json` diff where dev tooling sits
   in `devDependencies` directly below a runtime `dependencies` block must not
   produce a "runtime dependency" finding.




