
**Title:** Do not conclude from one file when the refuting evidence is another file in the same diff

**Labels:** bug, accuracy, prompt

**Version:** prxref 0.11.1 · GitHub Copilot Business backend · `claude-opus-5` · bitbucket-server forge

---

## Summary

On a single PR, prxref produced two findings and **both were false for the same
reason**: it reasoned from one changed file while the fact that refutes it sat in
another changed file *in the same diff*. This is not a context-window problem —
the diff is 14 files and ~21.8k input tokens, well inside the budget, and both
refuting files were themselves part of the reviewed changeset.

Issue #2 in this folder covers trusting an identifier's *name* over its
definition in the same file. This is the broader case: the definition is present
and changed, just in a different file, and the sweep never cross-references it.

Corpus: `GAIENG/syf-mcp` PR #48 at head `c989af3d`, both findings verified false
by running the code.

## Evidence

### Finding 1, verbatim

> 🟥 **Fake downloaded binary is never made executable** — `src/apps/cli/test/install-scripts.test.ts:52`
>
> `downloadedBinary` is written but only `bin/uname` and `bin/curl` get `chmod 0o755`. The stub curl copies `$FAKE_BINARY` to the install path with `cp`, preserving the non-executable mode, so the installer's attempt to run it should fail and every test in this file throws from the non-zero exit check.

The premise is accurate: `downloadedBinary` really is never chmod'd. The
conclusion is wrong, because `src/apps/mcp/docs/public/install.sh` — **modified by
this same PR** — contains, before it executes the file:

```sh
# A truncated or proxy-intercepted download would otherwise be chmod'd and run.
[ -s "$tmp" ] || die "the downloaded file is empty"

chmod +x "$tmp"
```

The installer makes the binary executable itself; the test never needs to. The
finding asserts "every test in this file throws". Actual result at that commit:

```
$ bun test test/install-scripts.test.ts test/service-selection.test.ts test/enrollment-resolve.test.ts
 23 pass
 0 fail
```

### Finding 2, verbatim

> 🟧 **Docs drop --force flag from enroll table** — `src/apps/mcp/docs/en/guide/start/cli.md:142`
>
> The enroll flag table replaces `--force` with `--all`/`--services` rather than adding them; nothing in this diff shows `--force` being removed from the enroll command, so the docs may now omit a supported flag.

Two errors. The table is under `### syf-mcp install`, not enroll. And `install`
has never had a `--force` flag — `src/apps/cli/src/index.ts`, **also modified by
this PR**, defines it on the base branch as:

```ts
const install = Command.make('install', {
    all: Flag.boolean('all')...   // only flag
  }, ({ all }) => Install.run({ all }));
```

`--force` belongs to `refresh`, whose table in the very same diff hunk correctly
retains it. The PR is *correcting* a doc entry for a flag that never existed;
prxref inverted that into "docs may now omit a supported flag".

Note the hedge in finding 2 — "nothing in this diff shows `--force` being
removed" — which is false on its face: the diff contains the file that defines
the command's flags. See also issue #5 on self-hedged findings surviving the
quality gate.

## Reproduction

```bash
git fetch origin refs/pull-requests/48/from && git worktree add /tmp/wt48 FETCH_HEAD
cd /tmp/wt48 && bun install

# Finding 1: the claim is "every test in this file throws"
cd src/apps/cli && bun test test/install-scripts.test.ts   # -> 4 pass, 0 fail
grep -n 'chmod +x' ../mcp/docs/public/install.sh           # -> the refuting line

# Finding 2: the claim is that --force is a supported install flag
git show origin/develop:src/apps/cli/src/index.ts | sed -n '20,35p'   # install has only --all
```

## Impact

Two wrong review comments on one PR, both stated with enough specificity to be
believed. Finding 1 is severity `error` and asserts a total test-suite failure
that does not exist; an author who trusts it goes looking for a bug in a passing
test, and a reviewer who trusts it blocks a correct PR. Finding 2 would push the
author to restore documentation for a flag that does not exist, actively
degrading the docs this PR fixes.

Both were caught only because the review workflow independently verifies every
finding before posting. Measured precision on this PR: **0 of 2**.

## Suggested fix

1. Before emitting a finding about symbol or file A, resolve A's definition and
   its callers **across the whole changeset**, not just the current chunk. Both
   refuting files were in the diff.
2. Treat "nothing in this diff shows X" as a claim requiring a search of the
   diff, not a licence to assume. If the search is not performed, the finding
   should not survive the quality gate.
3. For a finding that predicts a test outcome ("every test in this file
   throws"), that prediction is cheaply checkable and unusually high-confidence
   when wrong — consider gating such claims behind an explicit "did you read the
   code under test, including scripts it invokes?" step.
4. Anchor findings to the correct heading/section. Finding 2 named the wrong
   command ("enroll" for an `install` table), which would have misdirected the
   fix even if the substance had been right.

---

## Retest on 0.12.0 — harm suppressed, root cause NOT fixed

0.12.0 ships fixes for inbox issues 01–10 and 12, including "workers see library
versions and **same-file** symbol definitions". This issue is the **cross-file**
case, and it has no eval (`tests/test_issue_11_*` does not exist). Re-ran the same
PR (#48, head `c989af3d`) twice on 0.12.0:

| Candidate | 0.11.1 | 0.12.0 run 1 | 0.12.0 run 2 |
|---|---|---|---|
| "downloaded binary never chmod'd" | posted, **error** | generated, conf 0.50/0.55 → **dropped** | not generated |
| "`--force` removed from docs" | posted, **warning** | demoted to `outofscope` → **dropped** | demoted → **dropped** |
| **Posted to the PR** | **2 false positives** | **0** | **0** |

**The good news.** Net output is now correct: 0 findings posted on a PR where
both prior findings were false. The confidence floor (0.60) and the severity
demotion are doing real work, and this is a large practical improvement.

**The caveat.** The model still reaches the same wrong conclusions — the chmod
finding is still *generated*, still asserting a failure refuted by `install.sh` in
the same diff, and the `--force` finding still misreads the `install` table as
`enroll`. They are filtered on **calibrated confidence**, not on having resolved
the contradicting file. That is suppression, not comprehension:

- A cross-file false positive that the model happens to feel confident about
  (≥0.60) will still be posted.
- The corresponding true finding in the same class gets no boost, so the fix
  trades precision for recall in both directions.

**Still requested:** resolve definitions and callers across the **whole
changeset**, not just the current chunk and its own file. In both cases here the
refuting file was already in the diff and had been paid for in tokens.

---

## Retest on 0.12.1 — cross-file false positive clears the confidence floor

PR `GAIENG/syf-mcp#52` at head `0a1a6a0` makes a deployment foundation a
build-time input. prxref 0.12.1 posted this as its sole active finding at
confidence 0.70:

> 🟧 **Baked-in define kills the documented SYF_MCP_BASE_URL runtime override**
> — `src/apps/cli/src/consts/syf-mcp.consts.ts:17`
>
> `build.ts` adds `'process.env.SYF_MCP_BASE_URL': JSON.stringify(baseUrl)` to
> `define`, so in compiled binaries `resolveBaseUrl()` becomes a string literal
> and the `??` fallback is dead. The doc comment retained here still advertises
> `SYF_MCP_BASE_URL` as a user-facing override of the base origin, which no
> longer works for installed binaries.

The diff says the opposite. `scripts/base-url.ts` calls the foundation a
"BUILD-TIME concern". `src/apps/cli/src/consts/syf-mcp.consts.ts` says the build
"bakes `SYF_MCP_BASE_URL` into each compiled binary". `cli/scripts/build.ts`
explains that this is required so a staging binary does not fall back to
production on a user's machine. The PR description and release instructions
also direct release engineers to set the variable while building.

Both foundation builds succeeded. The staging build produced only `dev1.use2`
URLs in `version.json`, install scripts, and rendered docs; the default build
produced only `dev1.use1` URLs. Type checking and all 178 tests passed. The
replacement is therefore the feature under review, not a broken runtime
contract.

This extends the issue beyond low-confidence candidates: 0.12.1 still failed to
reconcile a changed use site with explicit contract evidence elsewhere in the
same 19-file diff, and the resulting false positive cleared the 0.60 gate.


