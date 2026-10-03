"""``prxref.eval_units``: subset datasets, shards, unit states, resets and run merges."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prxref import cli, evals
from prxref import eval_units as units
from prxref.eval_cases import load_cases
from tests import test_eval_run as run_helpers

DIFF = run_helpers.DIFF
LABEL = {"id": "h1", "file": "src/app.py", "line": 3, "severity": "warning", "must_match": "total"}


def _json(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def _cases_json(tmp_path: Path, ids=("a", "b", "c")) -> Path:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "change.patch").write_text(DIFF, encoding="utf-8")
    (data / "ticket.md").write_text("ticket", encoding="utf-8")
    cases = [
        {"id": i, "diff_file": "change.patch", "context_file": "ticket.md", "expected": [LABEL]}
        for i in ids
    ]
    return _json(data / "cases.json", {"version": 1, "cases": cases})


def _cases_dir(tmp_path: Path, ids=("case-1", "case-2", "case-3")) -> Path:
    root = tmp_path / "dirset"
    for name in ids:
        case = root / name
        case.mkdir(parents=True)
        (case / "diff.patch").write_text(DIFF, encoding="utf-8")
        (case / "ticket.md").write_text("ticket", encoding="utf-8")
        (case / "docs").mkdir()
        (case / "docs" / "spec.md").write_text("spec", encoding="utf-8")
        _json(case / "expected.json", [{**{k: v for k, v in LABEL.items() if k != "line"}, "line_hint": 3}])
    return root


class TestSubset:
    def test_cases_json_round_trip_with_relative_paths_in_the_given_order(self, tmp_path, monkeypatch):
        source = _cases_json(tmp_path)
        monkeypatch.chdir(tmp_path)
        relative = Path("data") / "cases.json"
        dest = units.write_subset_cases(str(relative), ["c", "a"], tmp_path / "elsewhere" / "sub.json")
        assert dest == tmp_path / "elsewhere" / "sub.json"
        monkeypatch.chdir("/")
        loaded = load_cases(dest)
        full = {case.id: case for case in load_cases(source)}
        assert [case.id for case in loaded] == ["c", "a"]
        for case in loaded:
            assert case.diff_file == str(tmp_path / "data" / "change.patch")
            assert Path(case.diff_file).is_absolute() and Path(case.context_file).is_absolute()
            assert case.expected == full[case.id].expected
            assert case.pr_url == full[case.id].pr_url

    def test_directory_dataset_round_trip(self, tmp_path):
        root = _cases_dir(tmp_path)
        dest = units.write_subset_cases(str(root), ["case-3", "case-1"], tmp_path / "sub" / "cases.json")
        loaded = load_cases(dest)
        full = {case.id: case for case in load_cases(root)}
        assert [case.id for case in loaded] == ["case-3", "case-1"]
        for case in loaded:
            assert case == full[case.id]
            assert case.spec and Path(case.spec[0]).is_dir() and case.context_file

    def test_the_file_holds_version_1_and_no_stray_tmp(self, tmp_path):
        dest = units.write_subset_cases(str(_cases_json(tmp_path)), ["b"], tmp_path / "s.json")
        assert json.loads(dest.read_text(encoding="utf-8"))["version"] == 1
        assert sorted(p.name for p in tmp_path.glob("s.json*")) == ["s.json"]

    def test_unknown_or_duplicate_ids_raise(self, tmp_path):
        source = str(_cases_json(tmp_path))
        with pytest.raises(ValueError, match="zzz"):
            units.write_subset_cases(source, ["a", "zzz"], tmp_path / "s.json")
        with pytest.raises(ValueError, match="duplicate"):
            units.write_subset_cases(source, ["a", "a"], tmp_path / "s.json")

    def test_a_subset_runs_only_its_cases(self, tmp_path):
        source = str(_cases_json(tmp_path))
        sub = units.write_subset_cases(source, ["b"], tmp_path / "s.json")
        review = run_helpers.FakeReview()
        run_helpers._run(run_helpers._args(sub, tmp_path / "out", label="s0"), review)
        assert review.case_ids == ["b"]


class TestShards:
    def test_round_robin_keeps_order_and_balance(self):
        ids = [f"c{i}" for i in range(7)]
        assert units.shard_ids(ids, 3) == [["c0", "c3", "c6"], ["c1", "c4"], ["c2", "c5"]]

    def test_more_shards_than_ids_gives_no_empty_shard(self):
        assert units.shard_ids(["a", "b"], 5) == [["a"], ["b"]]
        assert units.shard_ids([], 3) == []

    def test_one_shard_is_everything_and_every_id_appears_once(self):
        ids = [f"c{i}" for i in range(10)]
        assert units.shard_ids(ids, 1) == [ids]
        flat = [i for shard in units.shard_ids(ids, 4) for i in shard]
        assert sorted(flat) == sorted(ids)
        sizes = {len(shard) for shard in units.shard_ids(ids, 4)}
        assert max(sizes) - min(sizes) <= 1

    def test_zero_shards_is_refused(self):
        with pytest.raises(ValueError):
            units.shard_ids(["a"], 0)


def _unit(tmp_path: Path, *, record=None, error=None, metas=None) -> Path:
    case = tmp_path / "unit"
    case.mkdir()
    if record is not None:
        _json(case / "record.json", record)
    if error is not None:
        _json(case / "error.json", {"case_id": "a", "error": error})
    for name, meta in (metas or {}).items():
        _json(case / "trace" / name, meta)
    return case


class TestUnitState:
    def test_missing_without_either_file(self, tmp_path):
        assert units.unit_state(_unit(tmp_path)) == units.UNIT_MISSING
        assert units.unit_state(tmp_path / "no-such-dir") == units.UNIT_MISSING

    def test_ok_record(self, tmp_path):
        case = _unit(tmp_path, record={"verdict": "Approved", "findings": []})
        assert units.unit_state(case) == units.UNIT_OK

    def test_ok_record_whose_findings_mention_a_quota_is_still_ok(self, tmp_path):
        case = _unit(tmp_path, record={"verdict": "Approved", "findings": [{"body": "quota check"}]})
        assert units.unit_state(case) == units.UNIT_OK

    def test_plain_error_json(self, tmp_path):
        assert units.unit_state(_unit(tmp_path, error="RuntimeError: boom")) == units.UNIT_ERROR

    @pytest.mark.parametrize(
        "text",
        ["HTTP 429 from provider", "Rate limit reached", "rate_limit_error", "Too Many Requests",
         "monthly quota exceeded", "session limit hit", "Session cap"],
    )
    def test_rate_limit_text_in_error_json(self, tmp_path, text):
        assert units.unit_state(_unit(tmp_path, error=f"LLMError: {text}")) == units.UNIT_RATE_LIMITED

    def test_error_verdict_without_rate_limit_text_is_an_error(self, tmp_path):
        case = _unit(tmp_path, record={"verdict": "Error", "summary": "every chunk failed"})
        assert units.unit_state(case) == units.UNIT_ERROR

    def test_error_verdict_with_rate_limit_text_is_rate_limited(self, tmp_path):
        case = _unit(tmp_path, record={"verdict": "Error", "summary": "chunk0: 429 Too Many Requests"})
        assert units.unit_state(case) == units.UNIT_RATE_LIMITED

    def test_rate_limit_text_found_only_in_a_trace_meta_file(self, tmp_path):
        meta = {"unit": "chunk0", "error": "rate limit exceeded", "input_tokens": 10}
        error_case = _unit(tmp_path, error="RuntimeError: all chunks failed", metas={"chunk0.meta.json": meta})
        assert units.unit_state(error_case) == units.UNIT_RATE_LIMITED

    def test_error_record_with_the_text_only_in_a_meta_file(self, tmp_path):
        meta = {"unit": "sweep", "error": "429", "first_error": ""}
        case = _unit(tmp_path, record={"verdict": "Error"}, metas={"sweep.meta.json": meta})
        assert units.unit_state(case) == units.UNIT_RATE_LIMITED

    def test_an_ok_record_with_a_rate_limited_meta_is_rate_limited(self, tmp_path):
        meta = {"unit": "chunk1", "error": "quota exhausted"}
        case = _unit(tmp_path, record={"verdict": "Approved"}, metas={"chunk1.meta.json": meta})
        assert units.unit_state(case) == units.UNIT_RATE_LIMITED

    def test_a_clean_meta_and_a_numeric_429_do_not_trip_it(self, tmp_path):
        meta = {"unit": "chunk0", "error": "", "elapsed_ms": 429, "input_tokens": 429}
        case = _unit(tmp_path, record={"verdict": "Approved"}, metas={"chunk0.meta.json": meta})
        assert units.unit_state(case) == units.UNIT_OK

    def test_only_meta_files_are_scanned(self, tmp_path):
        case = _unit(tmp_path, record={"verdict": "Approved"})
        (case / "trace").mkdir()
        (case / "trace" / "chunk0.user.md").write_text("handle the rate limit", encoding="utf-8")
        assert units.unit_state(case) == units.UNIT_OK

    def test_regex_is_case_insensitive_and_word_bounded(self):
        assert units.RATE_LIMIT_RE.search("RATE-LIMIT")
        assert not units.RATE_LIMIT_RE.search("code 4290")


class TestResetUnit:
    def test_removes_the_four_and_keeps_case_json(self, tmp_path):
        case = _unit(tmp_path, record={"verdict": "Approved"}, error="x", metas={"c.meta.json": {}})
        _json(case / "case.json", {"id": "a"})
        (case / "diff.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")
        units.reset_unit(case)
        assert sorted(p.name for p in case.iterdir()) == ["case.json"]
        assert units.unit_state(case) == units.UNIT_MISSING

    def test_a_missing_unit_is_a_no_op(self, tmp_path):
        units.reset_unit(tmp_path / "nothing")

    def test_resume_reruns_only_the_reset_case(self, tmp_path):
        cases = run_helpers._dataset(tmp_path)
        out = tmp_path / "out"
        run_helpers._run(run_helpers._args(cases, out), run_helpers.FakeReview())
        run_dir = out / "L"
        units.reset_unit(run_dir / "cases" / "b")
        review = run_helpers.FakeReview()
        run_helpers._run(run_helpers._args(cases, out, "--resume"), review)
        assert review.case_ids == ["b"]
        assert units.unit_state(run_dir / "cases" / "b") == units.UNIT_OK
        assert units.unit_state(run_dir / "cases" / "a") == units.UNIT_OK


def _shard_run(tmp_path: Path, source: str, ids, label: str, **run_edits) -> Path:
    sub = units.write_subset_cases(source, ids, tmp_path / f"{label}.json")
    run_helpers._run(run_helpers._args(sub, tmp_path / "units", label=label), run_helpers.FakeReview())
    run_dir = tmp_path / "units" / label
    if run_edits:
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        run.update(run_edits)
        _json(run_dir / "run.json", run)
    return run_dir


def _score_args(out: Path, label: str):
    return cli._build_parser().parse_args(["eval", "score", f"--label={label}", "--out", str(out)])


class TestMergeRuns:
    def test_merge_and_score(self, tmp_path):
        source = str(_cases_json(tmp_path))
        s0 = _shard_run(tmp_path, source, ["a", "c"], "f0-s0", created_at="2026-01-02T00:00:00Z")
        s1 = _shard_run(tmp_path, source, ["b"], "f1-s0", created_at="2026-01-01T00:00:00Z")
        dest = tmp_path / "runs" / "r1"
        units.merge_runs([s0, s1], dest, label="r1", case_ids=["a", "b", "c"])
        run = json.loads((dest / "run.json").read_text(encoding="utf-8"))
        assert run["label"] == "r1" and run["case_ids"] == ["a", "b", "c"]
        assert run["created_at"] == "2026-01-01T00:00:00Z"
        assert "folds" not in run
        assert list(run) == [
            "version", "prxref_version", "label", "cases_path", "created_at", "case_ids", "prompts",
            "sampling", "review_rules", "scoped_rules", "config",
        ]
        assert sorted(p.name for p in (dest / "cases").iterdir()) == ["a", "b", "c"]
        assert not (tmp_path / "runs" / "r1.merging").exists()
        assert evals.eval_score(_score_args(tmp_path / "runs", "r1")) == 0
        score = json.loads((dest / "score.json").read_text(encoding="utf-8"))
        assert [row["case_id"] for row in score["cases"]] == ["a", "b", "c"]
        assert score["metrics"]["case_count"] == 3

    def test_each_id_is_copied_from_its_only_holder_and_sources_are_untouched(self, tmp_path):
        source = str(_cases_json(tmp_path))
        s0 = _shard_run(tmp_path, source, ["a"], "x")
        s1 = _shard_run(tmp_path, source, ["b"], "y")
        before = sorted(p.name for p in (s0 / "cases").iterdir())
        units.merge_runs([s0, s1], tmp_path / "m", label="m", case_ids=["b", "a"])
        run = json.loads((tmp_path / "m" / "run.json").read_text(encoding="utf-8"))
        assert run["case_ids"] == ["b", "a"]
        assert sorted(p.name for p in (s0 / "cases").iterdir()) == before

    def test_duplicate_id_raises_and_writes_nothing(self, tmp_path):
        source = str(_cases_json(tmp_path))
        s0 = _shard_run(tmp_path, source, ["a", "b"], "x")
        s1 = _shard_run(tmp_path, source, ["b", "c"], "y")
        with pytest.raises(ValueError, match="more than one.*b"):
            units.merge_runs([s0, s1], tmp_path / "m", label="m", case_ids=["a", "b", "c"])
        assert not (tmp_path / "m").exists() and not (tmp_path / "m.merging").exists()

    def test_missing_id_raises(self, tmp_path):
        source = str(_cases_json(tmp_path))
        s0 = _shard_run(tmp_path, source, ["a"], "x")
        with pytest.raises(ValueError, match="no source run holds.*c"):
            units.merge_runs([s0], tmp_path / "m", label="m", case_ids=["a", "c"])
        assert not (tmp_path / "m").exists()

    def test_equal_rules_leave_no_folds_key(self, tmp_path):
        source = str(_cases_json(tmp_path))
        rules = {"path": "r.md", "sha256": "x"}
        s0 = _shard_run(tmp_path, source, ["a"], "x", review_rules=rules)
        s1 = _shard_run(tmp_path, source, ["b"], "y", review_rules=rules)
        units.merge_runs([s0, s1], tmp_path / "m", label="m", case_ids=["a", "b"])
        run = json.loads((tmp_path / "m" / "run.json").read_text(encoding="utf-8"))
        assert run["review_rules"] == rules and "folds" not in run

    def test_differing_rules_null_the_stamps_and_list_folds(self, tmp_path):
        source = str(_cases_json(tmp_path))
        r0 = {"path": "f0.md", "sha256": "0"}
        r1 = {"path": "f1.md", "sha256": "1"}
        s0 = _shard_run(tmp_path, source, ["a", "c"], "x", review_rules=r0)
        s1 = _shard_run(tmp_path, source, ["b"], "y", review_rules=r1, scoped_rules={"entries": 1})
        dest = tmp_path / "runs" / "m"
        units.merge_runs([s0, s1], dest, label="m", case_ids=["a", "b", "c"])
        run = json.loads((dest / "run.json").read_text(encoding="utf-8"))
        assert run["review_rules"] is None and run["scoped_rules"] is None
        assert run["folds"] == [
            {"run": str(s0), "case_ids": ["a", "c"], "review_rules": r0, "scoped_rules": None},
            {"run": str(s1), "case_ids": ["b"], "review_rules": r1, "scoped_rules": {"entries": 1}},
        ]
        assert list(run)[-1] == "folds"
        assert evals.eval_score(_score_args(tmp_path / "runs", "m")) == 0

    def test_remerging_replaces_the_previous_dest(self, tmp_path):
        source = str(_cases_json(tmp_path))
        s0 = _shard_run(tmp_path, source, ["a"], "x")
        dest = tmp_path / "m"
        units.merge_runs([s0], dest, label="m", case_ids=["a"])
        (dest / "score.json").write_text("{}", encoding="utf-8")
        units.merge_runs([s0], dest, label="m", case_ids=["a"])
        assert not (dest / "score.json").exists()
