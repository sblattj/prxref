
**Title:** Feature request — flag a release-shaped PR that also touches source files

**Labels:** enhancement, recall, heuristic

**Version:** prxref 0.11.0 · `claude-opus-5`

---

## Summary

prxref returned **zero findings** on a release PR whose branch carried the
commits of a different, still-open, still-under-review PR. Merging it would have
landed unreviewed code into the trunk and bypassed an open review comment.

## Evidence

PR: `release/v0.4.0 -> develop`, described as `chore: release v0.4.0`. Alongside
the expected version bumps, CHANGELOGs, lockfile and consumed changesets, the
diff also touched `src/apps/cli/src/config/json-file.ts` and its test.

```
src/apps/cli/src/config/json-file.ts
  release/v0.4.0                7b6b46d4f69ed979b43a2f78e79e57b09518346b
  fix/intellij-config-dir-mode  7b6b46d4f69ed979b43a2f78e79e57b09518346b   <- identical (open PR)
  develop                       c552f95611d1eaa6fb8ebf10f27b0414a3e75806   <- differs
```

The corresponding changeset was absent from the release branch (consumed into
the CHANGELOG) but still present on the open PR's branch, confirming the release
was cut on top of the open PR rather than on top of the trunk.

## Why this is in scope for a diff reviewer

It is diff-visible without any repo archaeology: a PR whose changed-file set is
otherwise entirely release machinery (version fields, CHANGELOG.md, lockfile,
`.changeset/*` deletions) *and* contains source-file edits is either a release
cut from an unmerged branch or a hand edit smuggled into a release. Both are
worth a comment.

## Suggested heuristic

Classify a PR as release-shaped when >80% of changed files are version
manifests, changelogs, lockfiles or consumed changeset files. If any remaining
file is source, raise one finding naming those files. Cheap, deterministic, no
extra LLM call.


