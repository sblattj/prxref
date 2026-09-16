
**Title:** `--no-post` prints only the verdict — finding bodies are unreachable without calling internals

**Labels:** ux, cli

**Version:** prxref 0.11.0

---

## Summary

Dry run is the mode an operator uses to evaluate the tool before pointing it at
a busy repo, and it is the mode with the least output.

```
$ prxref review --pr-url ... --no-post -v
verdict: Request-Changes
counts: 2 (dropped: 0)
elapsed: 17.3s tokens: 22712+1318
```

Titles, bodies, files, lines and confidences are never rendered to stdout in any
mode. With `--no-post` they are not written anywhere at all. The only way to
read them is to bypass the CLI:

```python
from prxref.cli import _run_review
from prxref.formatter import format_summary, format_inline_comment, build_attribution
res = _run_review(url, post=False)
print(format_summary(res["verdict"], res["findings_active"], res["findings_dropped"],
      chunk_count=res["chunk_count"], elapsed_ms=res["elapsed_ms"],
      input_tokens=res["input_tokens"], output_tokens=res["output_tokens"], model="..."))
```

## Suggested fix

`--print` / `--format {text,md,json}` rendering the same summary plus inline
bodies to stdout, or have `--no-post` imply it. `json` in particular makes the
tool scriptable — a wrapper can then diff runs, which is currently impossible
without importing private functions.

Dropped findings should be included with their drop reason; they are already
tracked (`findings_dropped`) and are useful when auditing the quality gate.


