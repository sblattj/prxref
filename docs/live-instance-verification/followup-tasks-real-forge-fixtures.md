# Followup: real-instance fixtures for the forge adapters

## Status as of 2026-08-31

| Task | State |
|---|---|
| 1. Land the real Bitbucket Server diff as a fixture | **Done differently.** The diff went in verbatim inside `tests/test_triage.py::TestBitbucketServerDiffPrefixes` (PR #8) rather than as a standalone `tests/fixtures/` file, so it sits next to the assertions that read it. |
| 2. Regression test for `src://` / `dst://` | **Done** in PR #8, with a git-spelled control that must come out different. |
| 3. Write the live-instance requirement into `CLAUDE.md` | **Open.** This is the root-cause item and the one worth doing. |
| 4. Audit GitLab self-hosted and Bitbucket Cloud for the same gap | **Open.** |

The captured diff at the bottom of this file is kept because it is the only
surviving artifact of the container run, and it is the source the PR #8 test
was transcribed from.

## Origin

Produced by the `sharpen` retro of session
`974bbdda-6362-4426-9971-fec77fabc1d9` (2026-08-30/31). That session stood up
Bitbucket Data Center 10.4.2 in Docker with an unattended timebomb-license
setup, seeded a repo with three planted bugs, opened a real PR, and ran
`prxref review` against it with posting enabled.

**Two genuine bugs fell out of one live run, neither of which any hand-written
fixture had ever caught, for the life of the project:**

1. `src://` / `dst://` diff prefixes are not stripped on Bitbucket Server.
   Filed as issue #6.
   - `src/prxref/triage.py:107` — `_clean_path` strips only `a/` and `b/`
   - `src/prxref/triage.py:101` — `_DIFF_GIT_RE = r'^diff --git a/(.*?) b/(.*)$'`
     does not match the Server header at all
2. GitHub inline comments never post. The payload omits `commit_id`, GitHub
   answers 422 (`"commit_id" wasn't supplied. "line" is not a permitted key.`),
   and `src/prxref/forges/github.py:253` swallows it with a bare `continue`.
   Controlled: 0/3 posted without `commit_id`, 3/3 with it. GitLab sends
   `head_sha` correctly (`src/prxref/gitlab.py:378-387`); GitHub sends nothing.
   `commit_id` appears nowhere in `src/`. The head SHA already exists as
   `PRData.source_sha` but `post_inline_comments` does not receive it, so the
   fix needs signature plumbing across all four adapters.

The user authorised "File issue + fix on a branch" for #2 in that session; that
work is being handled separately. **What is NOT yet handled is the durable
lesson: the fixtures were the reason both bugs survived.**

The live Bitbucket container has been torn down (3-hour timebomb licence).
The real diff it served is preserved verbatim at the bottom of this file — that
is the only surviving artifact of the run, and re-obtaining it costs a full
container standup.

## Branch and PR strategy

One focused PR, `test/real-forge-fixtures`. Independent of the `commit_id` fix
branch; can land before or after it. No reviewers needed beyond the author.

## Task list

### 1. Land the real Bitbucket Server diff as a fixture

- **File:** `tests/fixtures/bitbucket_server_real.diff` (new)
- **What:** the verbatim diff at the bottom of this file, byte for byte. It is
  what Bitbucket Data Center 10.4.2 actually served for a two-file PR.
- **Why:** every existing Bitbucket Server fixture was hand-written with `a/`
  and `b/` prefixes, which is why `_DIFF_GIT_RE` never failed a test. A fixture
  captured FROM the product cannot encode the author's belief about the product.
- **Commit:** `test: capture the real Bitbucket Server diff as a fixture`

### 2. Regression test for the src:// / dst:// path handling

- **File:** `tests/test_triage.py`
- **What:** parse `bitbucket_server_real.diff` and assert every finding path is
  `cache.py` / `rates.py`, with no `src://` or `dst://` anywhere — in the
  finding path AND in the rendered comment body, which were two separate
  failures.
- **Why:** issue #6 needs a test that fails before the fix.
- **Commit:** `test: regression for Bitbucket Server src:// / dst:// prefixes`

### 3. Write the live-instance requirement into the repo conventions

- **File:** `CLAUDE.md`, under `## Conventions`
- **What:** add — *"a forge adapter is not considered covered until it has been
  run against a live instance of that product. Hand-written diff fixtures encode
  the shape the author already believed and cannot surface a bug in the belief;
  two production bugs (issue #6, and the GitHub `commit_id` omission) survived a
  full green suite for the life of the project for exactly this reason. The
  standup recipe for Bitbucket Data Center is in the `standing-up-a-live-forge-instance`
  skill."*
- **Why:** this is the actual root cause. Without it the next adapter repeats it.
- **Commit:** `docs: require live-instance verification for forge adapters`

### 4. Audit the other two adapters for the same class of gap

- **What:** GitLab self-hosted and Bitbucket Cloud have never been exercised
  against anything but recorded fixtures either. At minimum, capture one real
  diff per adapter from a live PR/MR and diff it against the fixture in the
  suite. The GitHub `commit_id` bug is proof this class is not hypothetical.
- **Why:** two of two adapters tested live produced a bug. The base rate on the
  untested two is not zero.
- **Commit:** `test: capture real diffs for the remaining adapters`

## PR description draft

```
## Summary

Standing up a real Bitbucket Data Center instance surfaced two production bugs
that the fixture suite had never caught (issue #6, and the GitHub `commit_id`
omission that silently disables inline comments entirely). Both survived because
every diff fixture in the suite was hand-written, and a hand-written fixture
encodes the shape the author already believed.

This PR lands the real diff Bitbucket Server served, a regression test built on
it, and a repo convention that an adapter is not covered until it has been run
against a live instance of its product.

## Test plan

- `uv run pytest` — the new regression test fails on `main` and passes on the
  `src://`/`dst://` fix branch
- `uv run ruff check src tests`
- The fixture is byte-identical to the diff captured from Bitbucket Data Center
  10.4.2 on 2026-08-30 (see docs/live-instance-verification/)
```

## The captured diff (Bitbucket Data Center 10.4.2, 2026-08-30, verbatim)

```diff
diff --git src://cache.py dst://cache.py
index 3cc2d49..4a62838 100644
--- src://cache.py
+++ dst://cache.py
@@ -17,16 +17,38 @@ class TTLCache:
         with self._lock:
             entry = self._data.get(key)
             if entry is None:
                 return None
             stored_at, value = entry
             if time.monotonic() - stored_at > self._ttl:
                 del self._data[key]
                 return None
             return value
 
+    def get_many(self, keys: list[str]) -> dict[str, object]:
+        """Return every live entry among ``keys``."""
+        out = {}
+        for key in keys:
+            entry = self._data.get(key)
+            if entry is None:
+                continue
+            stored_at, value = entry
+            if time.monotonic() - stored_at <= self._ttl:
+                out[key] = value
+        return out
+
+    def purge_expired(self) -> int:
+        """Drop every expired entry, returning how many were removed."""
+        removed = 0
+        with self._lock:
+            for key, (stored_at, _value) in self._data.items():
+                if time.monotonic() - stored_at > self._ttl:
+                    del self._data[key]
+                    removed += 1
+        return removed
+
     def put(self, key: str, value: object) -> None:
         with self._lock:
             if len(self._data) >= self._max_entries:
                 oldest = min(self._data, key=lambda k: self._data[k][0])
                 del self._data[oldest]
             self._data[key] = (time.monotonic(), value)
diff --git src://rates.py dst://rates.py
index f0578dd..1eaa9aa 100644
--- src://rates.py
+++ dst://rates.py
@@ -1,6 +1,13 @@
 """Currency conversion helpers."""
 
 
 def convert(amount_cents: int, rate: float) -> int:
     """Convert ``amount_cents`` using ``rate``, returning whole cents."""
     return int(amount_cents * rate)
+
+
+def convert_all(amounts: list[int], rate: float, results: list[int] = []) -> list[int]:
+    """Convert every amount in ``amounts``, accumulating into ``results``."""
+    for amount in amounts:
+        results.append(convert(amount, rate))
+    return results
```
