"""Issue #61: chunk overflow and over-budget chunks show in the run record.

Once ``max_chunks`` is reached, the chunker appends every file that finds no
room to the smallest chunk, past the token budget and the file cap. These
tests pin the stats that make that visible: ``triage.plan_chunks`` and its
``ChunkPlan``, the four record keys every exit of ``orchestrate_review``
carries, the ``--format json`` payload, and the text-mode ``chunks:`` line.
They also pin that ``build_chunks`` places files exactly as it did before
the stats existed, against a frozen copy of the 0.26.0 body.
"""
from __future__ import annotations

import io
import random

import pytest

from prxref import cli
from prxref.orchestrator import orchestrate_review
from prxref.triage import (
    DEFAULT_MAX_FILES_PER_CHUNK,
    DEFAULT_TOKEN_BUDGET,
    FileDiff,
    _shared_dir_depth,
    build_chunks,
    est_tokens,
    parse_unified_diff,
    plan_chunks,
    score_file,
)
from tests.test_orchestrator import REF, FakeForge, FakeLLM, _added_file_diff

pytestmark = pytest.mark.usefixtures("contract_stubs")

CHUNK_KEYS = ("chunks_over_budget", "largest_chunk_tokens", "overflow_files", "chunk_token_budget")


def _legacy_build_chunks(
    files, max_chunks=8, token_budget=DEFAULT_TOKEN_BUDGET, churn_by_path=None,
    max_files_per_chunk=DEFAULT_MAX_FILES_PER_CHUNK,
):
    """``triage.build_chunks`` exactly as it stood at 7e5375c (0.26.0)."""
    candidates = [f for f in files if not f.is_binary]
    if not candidates:
        return []
    churn = churn_by_path or {}
    sorted_files = sorted(
        candidates, key=lambda f: score_file(f, churn.get(f.path, 0)), reverse=True
    )

    def est(f):
        return (f.lines_added + f.lines_removed) * 40

    chunks = []
    chunk_tokens = []
    for f in sorted_files:
        ftokens = est(f)
        best_chunk = -1
        best_affinity = -1
        for idx, chunk in enumerate(chunks):
            if chunk_tokens[idx] + ftokens > token_budget:
                continue
            if len(chunk) >= max_files_per_chunk:
                continue
            affinity = max(_shared_dir_depth(f.path, cf.path) for cf in chunk)
            if affinity > best_affinity:
                best_affinity = affinity
                best_chunk = idx
        if best_chunk >= 0:
            chunks[best_chunk].append(f)
            chunk_tokens[best_chunk] += ftokens
        elif len(chunks) < max_chunks:
            chunks.append([f])
            chunk_tokens.append(ftokens)
        else:
            smallest = min(range(len(chunks)), key=lambda k: chunk_tokens[k])
            chunks[smallest].append(f)
            chunk_tokens[smallest] += ftokens
    return chunks


def _files(*specs: tuple[str, int]) -> list[FileDiff]:
    return parse_unified_diff("".join(_added_file_diff(path, n) for path, n in specs))


def _binary(path: str) -> FileDiff:
    return FileDiff(path=path, old_path=path, new_path=path, is_binary=True)


def _paths(chunks):
    return [[f.path for f in chunk] for chunk in chunks]


class TestEstTokens:
    def test_forty_tokens_per_changed_line(self):
        (f,) = _files(("src/a.py", 7))
        assert est_tokens(f) == 7 * 40

    def test_removed_lines_count_too(self):
        (f,) = parse_unified_diff(
            "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,1 @@\n-a\n-b\n+c\n"
        )
        assert est_tokens(f) == 3 * 40


class TestPlanChunksStats:
    def test_under_the_cap_nothing_overflows(self):
        plan = plan_chunks(_files(("src/a.py", 10), ("lib/b.py", 10)), max_chunks=8)
        assert len(plan.chunks) >= 1
        assert plan.overflow_files == 0
        assert plan.chunks_over_budget == 0
        assert plan.largest_chunk_tokens == max(plan.chunk_tokens) <= DEFAULT_TOKEN_BUDGET
        assert plan.token_budget == DEFAULT_TOKEN_BUDGET

    def test_over_the_cap_with_budget_overflow(self):
        """Three 10-line files (400 tokens each), budget 500, one chunk allowed:
        the first opens the chunk and the other two overflow into it."""
        plan = plan_chunks(
            _files(("a/x.py", 10), ("b/y.py", 10), ("c/z.py", 10)),
            max_chunks=1, token_budget=500,
        )
        assert len(plan.chunks) == 1 and len(plan.chunks[0]) == 3
        assert plan.overflow_files == 2
        assert plan.chunks_over_budget == 1
        assert plan.largest_chunk_tokens == 1200
        assert plan.chunk_tokens == (1200,)

    def test_file_cap_overflow_under_budget(self):
        """The overflow branch also fires when every chunk is at the file cap,
        with the budget nowhere near reached: overflow without over-budget."""
        plan = plan_chunks(
            _files(("a/x.py", 1), ("b/y.py", 1), ("c/z.py", 1), ("d/w.py", 1)),
            max_chunks=2, max_files_per_chunk=1,
        )
        assert len(plan.chunks) == 2
        assert plan.overflow_files == 2
        assert plan.chunks_over_budget == 0
        assert plan.largest_chunk_tokens == 80

    def test_one_huge_file_below_the_cap_is_over_budget(self):
        plan = plan_chunks(
            _files(("src/big.py", 30), ("src/small.py", 2)), max_chunks=8, token_budget=1000,
        )
        assert plan.overflow_files == 0
        assert plan.chunks_over_budget == 1
        assert plan.largest_chunk_tokens == 30 * 40

    def test_a_chunk_exactly_at_the_budget_is_not_over(self):
        plan = plan_chunks(_files(("src/a.py", 25)), token_budget=1000)
        assert plan.largest_chunk_tokens == 1000
        assert plan.chunks_over_budget == 0

    def test_binary_files_are_skipped(self):
        files = [_binary("img/logo.png"), *_files(("src/a.py", 5)), _binary("img/icon.png")]
        plan = plan_chunks(files, max_chunks=1, max_files_per_chunk=1)
        assert _paths(plan.chunks) == [["src/a.py"]]
        assert plan.overflow_files == 0
        assert plan.largest_chunk_tokens == 200

    def test_all_binary_is_an_empty_plan(self):
        plan = plan_chunks([_binary("a.png"), _binary("b.png")], token_budget=123)
        assert plan.chunks == []
        assert plan.chunk_tokens == ()
        assert plan.overflow_files == plan.chunks_over_budget == plan.largest_chunk_tokens == 0
        assert plan.token_budget == 123

    def test_chunk_tokens_match_est_tokens_of_each_chunk(self):
        rng = random.Random(61)
        files = _files(*((f"d{rng.randrange(4)}/f{i}.py", rng.randrange(1, 60)) for i in range(25)))
        plan = plan_chunks(files, max_chunks=3, token_budget=2000, max_files_per_chunk=4)
        assert plan.chunk_tokens == tuple(sum(est_tokens(f) for f in c) for c in plan.chunks)
        assert plan.overflow_files > 0


CASES = [
    ("under_cap", [("src/a.py", 10), ("src/b.py", 12), ("lib/c.py", 3)], {}),
    ("budget_overflow", [("a/x.py", 10), ("b/y.py", 10), ("c/z.py", 10)], {"max_chunks": 1, "token_budget": 500}),
    (
        "file_cap_overflow",
        [("a/x.py", 1), ("b/y.py", 1), ("c/z.py", 1), ("d/w.py", 1)],
        {"max_chunks": 2, "max_files_per_chunk": 1},
    ),
    ("oversized_file", [("src/big.py", 900), ("src/small.py", 2)], {}),
    ("many_files", [(f"pkg{i % 5}/m{i}.py", (i * 37) % 80 + 1) for i in range(40)], {"token_budget": 3000}),
]


class TestBuildChunksUnchanged:
    @pytest.mark.parametrize(("name", "specs", "kw"), CASES, ids=[c[0] for c in CASES])
    def test_matches_the_legacy_placement(self, name, specs, kw):
        files = _files(*specs)
        assert _paths(build_chunks(files, **kw)) == _paths(_legacy_build_chunks(files, **kw))

    def test_mixed_binary_matches_the_legacy_placement(self):
        files = [_binary("x.png"), *_files(("src/a.py", 3), ("src/b.py", 4)), _binary("y.bin")]
        assert _paths(build_chunks(files)) == _paths(_legacy_build_chunks(files))

    def test_all_binary_matches_the_legacy_placement(self):
        assert build_chunks([_binary("x.png")]) == _legacy_build_chunks([_binary("x.png")]) == []

    def test_churn_is_honoured_the_same_way(self):
        files = _files(("a/x.py", 3), ("b/y.py", 3), ("c/z.py", 3))
        churn = {"c/z.py": 50, "a/x.py": 5}
        kw = {"max_chunks": 2, "max_files_per_chunk": 1, "churn_by_path": churn}
        assert _paths(build_chunks(files, **kw)) == _paths(_legacy_build_chunks(files, **kw))

    def test_randomised_inputs_match(self):
        rng = random.Random(6100)
        for _ in range(40):
            specs = [
                (f"d{rng.randrange(6)}/s{rng.randrange(3)}/f{i}.py", rng.randrange(1, 200))
                for i in range(rng.randrange(1, 30))
            ]
            kw = {
                "max_chunks": rng.randrange(1, 9),
                "token_budget": rng.choice([500, 2000, 8000, DEFAULT_TOKEN_BUDGET]),
                "max_files_per_chunk": rng.randrange(1, 7),
            }
            files = _files(*specs)
            assert _paths(build_chunks(files, **kw)) == _paths(_legacy_build_chunks(files, **kw)), kw

    def test_build_chunks_is_the_plans_chunks(self):
        files = _files(("a/x.py", 10), ("b/y.py", 10), ("c/z.py", 10))
        kw = {"max_chunks": 1, "token_budget": 500}
        assert _paths(build_chunks(files, **kw)) == _paths(plan_chunks(files, **kw).chunks)

    def test_max_chunks_zero_still_raises(self):
        with pytest.raises(ValueError):
            build_chunks(_files(("a/x.py", 1)), max_chunks=0)


def _review(diff: str, **kw) -> dict:
    return orchestrate_review(FakeForge(diff=diff), REF, FakeLLM("{}"), post=False, **kw)


THREE_FILES = _added_file_diff("a/x.py", 10) + _added_file_diff("b/y.py", 10) + _added_file_diff("c/z.py", 10)


class TestTheRecordFields:
    def test_a_normal_run_with_no_pressure(self):
        res = _review(_added_file_diff("src/app.py", 20))
        assert res["verdict"] != "Error"
        assert (res["chunks_over_budget"], res["overflow_files"]) == (0, 0)
        assert res["largest_chunk_tokens"] == 20 * 40
        assert res["chunk_token_budget"] == DEFAULT_TOKEN_BUDGET

    def test_a_normal_run_with_overflow(self):
        res = _review(THREE_FILES, max_chunks=1, token_budget=500)
        assert res["chunk_count"] == 2
        assert res["overflow_files"] == 2
        assert res["chunks_over_budget"] == 1
        assert res["largest_chunk_tokens"] == 1200
        assert res["chunk_token_budget"] == 500

    def test_a_file_cap_overflow_run(self):
        res = _review(THREE_FILES, max_chunks=2, max_files_per_chunk=1)
        assert (res["overflow_files"], res["chunks_over_budget"]) == (1, 0)

    def test_the_empty_diff_exit(self):
        res = _review("", token_budget=777)
        assert res["chunk_count"] == 0
        assert {k: res[k] for k in CHUNK_KEYS} == {
            "chunks_over_budget": 0, "largest_chunk_tokens": 0, "overflow_files": 0,
            "chunk_token_budget": 777,
        }

    def test_an_error_exit_before_chunking(self):
        forge = FakeForge(diff=THREE_FILES)
        forge.fail.add("get_pr")
        res = orchestrate_review(forge, REF, FakeLLM("{}"), post=False, token_budget=900)
        assert res["verdict"] == "Error"
        assert [res[k] for k in CHUNK_KEYS] == [0, 0, 0, 900]

    def test_the_chunking_error_exit(self):
        res = _review(THREE_FILES, max_chunks=0)
        assert res["verdict"] == "Error"
        assert [res[k] for k in CHUNK_KEYS] == [0, 0, 0, DEFAULT_TOKEN_BUDGET]

    def test_the_total_failure_exit_keeps_the_real_counts(self):
        res = orchestrate_review(
            FakeForge(diff=THREE_FILES), REF, FakeLLM(error=RuntimeError("no model")), post=False,
            max_chunks=1, token_budget=500,
        )
        assert res["verdict"] == "Error"
        assert [res[k] for k in CHUNK_KEYS] == [1, 1200, 2, 500]

    def test_the_json_payload_carries_them_after_chunks_failed(self):
        payload = cli._build_json_result(_review(THREE_FILES, max_chunks=1, token_budget=500))
        keys = list(payload)
        at = keys.index("chunks_failed")
        assert keys[at + 1 : at + 5] == list(CHUNK_KEYS)
        assert [payload[k] for k in CHUNK_KEYS] == [1, 1200, 2, 500]

    def test_a_partial_result_gives_null_not_a_crash(self):
        payload = cli._build_json_result({"verdict": "Error"})
        assert [payload[k] for k in CHUNK_KEYS] == [None] * 4


def _text(record: dict, verbose: bool = False) -> str:
    buf = io.StringIO()
    cli._print_summary(record, 1.0, verbose=verbose, out=buf)
    return buf.getvalue()


BASE_RECORD = {
    "verdict": "Approved", "findings_active": [], "findings_dropped": [], "chunk_count": 3,
    "chunks_reviewed": 3, "chunks_failed": 0,
}


class TestTheTextLine:
    @pytest.mark.parametrize("verbose", [False, True])
    def test_no_pressure_output_is_byte_identical(self, verbose):
        quiet = {**BASE_RECORD, "chunks_over_budget": 0, "largest_chunk_tokens": 4000,
                 "overflow_files": 0, "chunk_token_budget": 25000}
        assert _text(quiet, verbose) == _text(BASE_RECORD, verbose)
        assert "chunks:" not in _text(quiet, verbose)

    def test_both_counts(self):
        record = {**BASE_RECORD, "chunks_over_budget": 2, "largest_chunk_tokens": 61200,
                  "overflow_files": 5, "chunk_token_budget": 25000}
        lines = _text(record).splitlines()
        assert lines == [
            "verdict: Approved",
            "chunks: 2 over the 25000-token budget (largest ~61200) · 5 files placed past the "
            "chunk cap; raise PRXREF_MAX_CHUNKS or PRXREF_CHUNK_TOKEN_BUDGET",
        ]

    def test_over_budget_alone(self):
        record = {**BASE_RECORD, "chunks_over_budget": 1, "largest_chunk_tokens": 36000,
                  "overflow_files": 0, "chunk_token_budget": 25000}
        assert _text(record).splitlines()[1] == "chunks: 1 over the 25000-token budget (largest ~36000)"

    def test_overflow_alone_singular(self):
        record = {**BASE_RECORD, "chunks_over_budget": 0, "largest_chunk_tokens": 80,
                  "overflow_files": 1, "chunk_token_budget": 25000}
        assert _text(record).splitlines()[1] == (
            "chunks: 1 file placed past the chunk cap; raise PRXREF_MAX_CHUNKS or PRXREF_CHUNK_TOKEN_BUDGET"
        )

    def test_the_line_comes_from_a_real_run(self):
        out = _text(_review(THREE_FILES, max_chunks=1, token_budget=500))
        assert (
            "chunks: 1 over the 500-token budget (largest ~1200) · 2 files placed past the chunk cap; "
            "raise PRXREF_MAX_CHUNKS or PRXREF_CHUNK_TOKEN_BUDGET"
        ) in out.splitlines()

    def test_a_real_run_without_pressure_prints_no_line(self):
        out = _text(_review(_added_file_diff("src/app.py", 20)))
        assert not any(line.startswith("chunks:") for line in out.splitlines())
