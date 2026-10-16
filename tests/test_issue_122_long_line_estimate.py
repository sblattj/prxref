"""Issue 122: a file of few, very long lines is sized by its characters.

The repro: a 3.5 MB minified bundle diff was estimated at about 27k tokens
(40 per line), so it shared a chunk with other files and was never reported
over budget. Frozen contracts: ordinary lines keep ``lines * 40`` exactly;
a long-line file is estimated at ``chars // 3``, opens its own chunk, and
counts in ``chunks_over_budget``.
"""
from __future__ import annotations

from prxref.triage import DiffLine, FileDiff, Hunk, est_tokens, plan_chunks


def _file(path: str, lines: list[str]) -> FileDiff:
    body = [DiffLine(kind="+", text=t, new_line=i) for i, t in enumerate(lines, start=1)]
    return FileDiff(
        path=path, old_path=None, new_path=path, status="added",
        hunks=[Hunk(old_start=0, old_count=0, new_start=1, new_count=len(body), lines=body)],
    )


def test_ordinary_lines_keep_forty_per_line():
    f = _file("src/a.py", ["x = compute(value, other)  # a normal line of code"] * 50)
    assert est_tokens(f) == 50 * 40


def test_context_lines_do_not_count():
    f = _file("src/a.py", ["y"] * 3)
    f.hunks[0].lines.append(DiffLine(kind=" ", text="z" * 9000, old_line=1, new_line=4))
    assert est_tokens(f) == 3 * 40


def test_long_lines_are_sized_by_characters():
    f = _file("vendor/bundle.mjs", ["a" * 30_000] * 4)
    assert est_tokens(f) == 120_000 // 3


def test_a_bundle_gets_its_own_chunk_and_reads_over_budget():
    bundle = _file("vendor/bundle.mjs", ["a" * 60_000] * 2)
    small = _file("vendor/readme.md", ["notes"] * 10)
    plan = plan_chunks([bundle, small], token_budget=12_000)
    assert [[f.path for f in c] for c in plan.chunks] == [["vendor/bundle.mjs"], ["vendor/readme.md"]]
    assert plan.chunks_over_budget == 1
    assert plan.largest_chunk_tokens == 40_000
