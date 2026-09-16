
**Title:** Settled-thread suppression crashes on a PR comment that has no file path

**Labels:** bug, crash, regression

**Version:** prxref 0.12.0 · GitHub Copilot Business backend · `claude-opus-5` · bitbucket-server forge

---

## Summary

The settled-thread gate added in 0.12.0 (the fix for inbox issue 06) dereferences
`Thread.path` unconditionally. `Thread.path` is declared `str | None`, and it is
`None` for any **general PR comment** — a comment on the PR itself rather than
anchored to a file. Almost every real PR has at least one, so this aborts the
review with an `AttributeError` before any finding is produced.

This is a hard regression: the same PR reviews fine on 0.11.1.

## Evidence

`src/prxref/forges/base.py:41`:

```python
@dataclass
class Thread:
    path: str | None      # <-- nullable
    line: int | None
    resolved: bool
    author: str
    body_snippet: str
```

`src/prxref/quality.py:859` assumes it is not:

```python
def _normalised_path(path: str) -> str:
    return path[2:] if path.startswith("./") else path
```

Traceback from a real review (`GAIENG/syf-mcp` PR #48, whose PR-level bot comment
is unanchored):

```
File "/…/prxref/orchestrator.py", line 546, in orchestrate_review
    findings = apply_settled_thread_suppression(findings, threads)
File "/…/prxref/quality.py", line 895, in apply_settled_thread_suppression
    if _normalised_path(t.path) != _normalised_path(f.file):
File "/…/prxref/quality.py", line 860, in _normalised_path
    return path[2:] if path.startswith("./") else path
AttributeError: 'NoneType' object has no attribute 'startswith'
```

## Reproduction

```python
from prxref.quality import apply_settled_thread_suppression
from prxref.forges.base import Thread
from prxref.triage import Finding

f = Finding(file='src/a.ts', line=1, severity='error', confidence=0.8, title='t', body='b')
threads = [Thread(path=None, line=None, resolved=False, author='bot', body_snippet='general comment')]

apply_settled_thread_suppression([f], threads)
# AttributeError: 'NoneType' object has no attribute 'startswith'
```

The shipped suite does not catch this: all 21 tests matching `settled`/`issue_06`
pass, because every fixture gives `Thread` a real path. `1462 passed` overall on
0.12.0 with the bug present.

## Impact

Any PR carrying a general comment — a bot summary, a "please rebase", a release
checklist — fails the whole review at the quality stage. On Bitbucket Server this
is close to universal, since PR-level automation comments are the norm. The
failure is a raw traceback with no partial result, so a scheduled or CI-driven
review just dies.

## Suggested fix

Treat a pathless thread as matching nothing, rather than letting `None` compare
equal to another `None` (which would let a general comment suppress a finding
that also lacks a file):

```python
def _normalised_path(path: str | None) -> str | None:
    if path is None:
        return None
    return path[2:] if path.startswith("./") else path
```

```python
for t in threads:
    thread_path = _normalised_path(t.path)
    finding_path = _normalised_path(f.file)
    if thread_path is None or finding_path is None:
        continue
    if thread_path != finding_path:
        continue
```

Verified locally: fixes the crash, keeps same-path suppression working
(`drop_reason: settled in thread: <author>`), and the full suite stays green
(1462 passed). Add a fixture with `path=None` to
`tests/test_issue_06_sweep_deletions_threads.py` so the gap does not reopen.

## Related

Worth auditing the other 0.12.0 gates for the same assumption — `Thread.line` is
also `int | None`, and `Finding.file` is not guaranteed to be set once
`apply_line_align` has demoted a non-anchorable finding.


