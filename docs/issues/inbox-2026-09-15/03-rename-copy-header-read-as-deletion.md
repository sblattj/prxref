
**Title:** Git `copy from` / `rename from` diff headers are read as file deletions

**Labels:** bug, diff-parsing

**Version:** prxref 0.11.0 · `claude-opus-5` · Bitbucket Server forge

---

## Summary

On a PR that created a new package by copying an existing one, prxref reported
the *source* file of the copy as removed. It is present, and separately
modified, in the same diff.

## The finding as posted

> 🟧 `src/packages/splunk/package.json:2` — **ServiceNow package.json removed by
> rename to splunk**

## Why it is wrong

The hunk header is a copy, not a rename:

```
diff --git src://src/packages/servicenow/package.json dst://src/packages/splunk/package.json
similarity index 53%
copy from src/packages/servicenow/package.json
copy to src/packages/splunk/package.json
```

And the same diff contains a *separate* hunk modifying the source file, which
still exists on the branch:

```
diff --git src://src/packages/servicenow/package.json dst://src/packages/servicenow/package.json
@@ -1,16 +1,18 @@
   "scripts": {
-    "check-types": "npx tsc --noEmit"
+    "check-types": "npx tsc --noEmit",
+    "test": "vitest run"
```

Confirmed against the branch:

```
GET /raw/src/packages/servicenow/package.json?at=refs/heads/feat/splunk-servicenow-tools
-> HTTP 200  {"name": "@syf-mcp/servicenow", ...}
```

## Impact

Claims a package was deleted when it was not. On a monorepo split/extract PR —
exactly the shape where a reviewer is looking hardest for accidental deletions —
this is the most alarming possible false positive.

## Suggested fix

- Parse `rename from` / `copy from` / `similarity index` headers explicitly and
  attribute the pre-image path to the rename, not to a deletion.
- Before raising any finding that asserts a file was removed, check the path
  against the post-image tree.


