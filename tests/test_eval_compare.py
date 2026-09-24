"""``prxref eval compare``: two scored runs side by side, label by label (#14 T8).

Most tests build each run's ``score.json`` from graded cases through
:func:`prxref.eval_metrics.score_cases` and :func:`prxref.evals._score_json`,
the exact schema ``eval score`` writes, and place it in a run directory by
hand, so a judge stamp, a judge error or an unpriced case is available on
demand. :class:`TestThroughTheCli` scores two runs laid out as ``eval run``
writes them with the real ``eval score`` (every label has ``must_match``, so
no judge is built) and compares them through ``cli.main``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from prxref import cli, evals
from prxref.eval_cases import EvalCase, ExpectedFinding, case_to_json
from prxref.eval_metrics import MATCH_METHOD, GradedCase, score_cases
from prxref.llm import ConfigError

RUN = {"prompts": None, "sampling": None, "review_rules": None, "config": None}
SHA_A = "a" * 64
SHA_B = "b" * 64


def _label(label_id: str, file: str, line: int, severity: str = "error", **fields: Any) -> ExpectedFinding:
    return ExpectedFinding(label_id, file, line, severity, **fields)


def _row(file: str, line: int, title: str, severity: str = "warning") -> dict[str, Any]:
    return {"file": file, "line": line, "severity": severity, "title": title, "body": f"{title}.", "drop_reason": None}


def _grade(human_id: str, grade: str, ai_ref: int | None = None, *, method: str = evals.JUDGE_METHOD) -> dict:
    return {"human_id": human_id, "grade": grade, "ai_ref": ai_ref, "ai_line": None, "method": method}


def _case(case_id: str, labels: list[ExpectedFinding], findings: list[dict], grades: list[dict], *,
          cost_usd: float | None = 0.01, elapsed_ms: int | None = 1000, chunks_failed: int | None = 0) -> GradedCase:
    record = {
        "verdict": "Request-Changes", "findings": findings, "chunks_failed": chunks_failed,
        "elapsed_ms": elapsed_ms, "cost_usd": cost_usd, "cost_estimated": False,
    }
    return GradedCase(case_id=case_id, expected=tuple(labels), ai_findings=findings, grades=grades, record=record)


def _judge(*, model: str = "judge-m", sha: str = SHA_A, cost_usd: float | None = 0.002) -> dict[str, Any]:
    return {
        "model": model, "sampling": {"temperature": 0.0, "seed": 5, "models": [model]}, "prompt_version": 1,
        "prompt_sha256": sha, "self_judged": False, "cost_usd": cost_usd, "cost_estimated": False,
        "llm_calls": 1, "cached": 0, "errors": [],
    }


def _score(label: str, cases: list[GradedCase], judge: dict[str, Any] | None) -> dict[str, Any]:
    return evals._score_json(label, RUN, [], score_cases(cases), judge)


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def _put(run_dir: Path, score: dict[str, Any], records: dict[str, Any] | None = None) -> Path:
    """Write a scored run: ``score.json`` plus any ``cases/<id>/record.json``."""
    _write_json(run_dir / "score.json", score)
    for case_id, record in (records or {}).items():
        _write_json(run_dir / "cases" / case_id / "record.json", record)
    return run_dir


def _compare(out: Path, run_a: str, run_b: str, capsys) -> str:
    capsys.readouterr()
    args = cli._build_parser().parse_args(["eval", "compare", run_a, run_b, "--out", str(out)])
    assert evals.eval_compare(args) == 0
    return capsys.readouterr().out


def _warnings(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]


def _one_label_score(label: str, grade: str, *, method: str = evals.JUDGE_METHOD,
                     judge: dict[str, Any] | None = None, case_id: str = "c1") -> dict[str, Any]:
    findings = [_row("src/a.py", 10, "Divide by zero", "error")]
    ref = 0 if grade in ("full", "partial") else None
    graded = _case(case_id, [_label("H1", "src/a.py", 10)], findings, [_grade("H1", grade, ref, method=method)])
    return _score(label, [graded], judge)


C1_A_LABELS = [
    _label("H1", "src/a.py", 10, "error", category="logic", accepted=True),
    _label("H2", "src/a.py", 20, "warning", category="style"),
    _label("H3", "src/a.py", 30, "error", category="logic"),
]
C2_LABELS = [_label("H1", "src/b.py", 5, "minor", category="security", accepted=False)]
C2_FINDINGS = [_row("src/b.py", 5, "Token logged")]


def _golden_pair(out: Path) -> None:
    """Two runs of cases c1 and c2: c1/H1 loses its credit, c1/H3 becomes a judge error, the rest hold."""
    a_findings = [_row("src/a.py", 11, "Divide by zero", "error"), _row("src/a.py", 31, "Race")]
    base = _score("base", [
        _case("c1", C1_A_LABELS, a_findings,
              [_grade("H1", "full", 0), _grade("H2", "none"), _grade("H3", "partial", 1)],
              cost_usd=0.01, elapsed_ms=1000),
        _case("c2", C2_LABELS, C2_FINDINGS, [_grade("H1", "full", 0)], cost_usd=0.02, elapsed_ms=2000),
    ], _judge(cost_usd=0.002))
    b_labels = [*C1_A_LABELS, _label("H4", "src/a.py", 40, "spec")]
    cand = _score("cand", [
        _case("c1", b_labels, [_row("src/a.py", 31, "Race", "error")],
              [_grade("H1", "none"), _grade("H2", "none"), _grade("H3", "judge_error"), _grade("H4", "none")],
              cost_usd=None, elapsed_ms=1500, chunks_failed=1),
        _case("c2", C2_LABELS, C2_FINDINGS, [_grade("H1", "full", 0)], cost_usd=0.02, elapsed_ms=2000),
    ], _judge(cost_usd=0.003))
    _put(out / "base", base)
    _put(out / "cand", cand)


GOLDEN = """\
# prxref eval compare

- A: base
- B: cand

## Metrics

| Metric | A | B | Change |
|---|---:|---:|---:|
| Cases | 2 | 2 | 0 |
| Recall (micro) | 62.5% (2.5 of 4) | 25.0% (1 of 4) | -37.5 pp |
| Judge errors | 0 | 1 | +1 |
| Recall, severity `error` | 75.0% (1.5 of 2) | 0.0% (0 of 1) | -75.0 pp |
| Recall, severity `minor` | 100.0% (1 of 1) | 100.0% (1 of 1) | 0.0 pp |
| Recall, severity `spec` | n/a | 0.0% (0 of 1) | unknown |
| Recall, severity `warning` | 0.0% (0 of 1) | 0.0% (0 of 1) | 0.0 pp |
| Recall, category `(none)` | n/a | 0.0% (0 of 1) | unknown |
| Recall, category `logic` | 75.0% (1.5 of 2) | 0.0% (0 of 1) | -75.0 pp |
| Recall, category `security` | 100.0% (1 of 1) | 100.0% (1 of 1) | 0.0 pp |
| Recall, category `style` | 0.0% (0 of 1) | 0.0% (0 of 1) | 0.0 pp |
| Recall, accepted labels | 100.0% (1 of 1) | 0.0% (0 of 1) | -100.0 pp |
| Unmatched AI per PR | 0.00 (0 of 3) | 0.50 (1 of 2) | +0.50 |
| Severity agreement | 66.7% (2 of 3) | 100.0% (1 of 1) | +33.3 pp |
| Chunks failed | 0 | 1 | +1 |
| Elapsed | 3.0 s | 3.5 s | +0.5 s |
| Review cost | $0.0300 | unknown | unknown |
| Judge cost | $0.0020 | $0.0030 | +$0.0010 |

## Changed labels

| Case | Label | Location | A | B |
|---|---|---|---|---|
| c1 | H1 | src/a.py:10 | full (1) | none (0) |
| c1 | H3 | src/a.py:30 | partial (0.5) | judge_error |

## Only in one run

- Label `H4` of case `c1` at `src/a.py:40`: only in B
"""


class TestGolden:
    def test_a_fixed_pair_prints_the_golden_comparison_and_logs_no_warning(self, tmp_path, capsys, caplog):
        caplog.set_level(logging.INFO)
        out = tmp_path / "out"
        _golden_pair(out)

        assert _compare(out, "base", "cand", capsys) == GOLDEN
        assert _warnings(caplog) == []

    def test_comparing_the_same_runs_twice_is_byte_identical_and_ascii(self, tmp_path, capsys):
        out = tmp_path / "out"
        _golden_pair(out)

        first = _compare(out, "base", "cand", capsys)
        second = _compare(out, "base", "cand", capsys)

        assert first.encode("utf-8") == second.encode("utf-8")
        assert first.isascii()

    def test_an_unchanged_run_has_no_changed_labels_and_zero_changes(self, tmp_path, capsys):
        out = tmp_path / "out"
        _golden_pair(out)

        text = _compare(out, "base", "base", capsys)

        assert "## Changed labels\n\nNone.\n" in text
        assert "## Only in one run\n\nNone.\n" in text
        assert "| Recall (micro) | 62.5% (2.5 of 4) | 62.5% (2.5 of 4) | 0.0 pp |" in text
        assert "| Review cost | $0.0300 | $0.0300 | $0.0000 |" in text

    def test_a_total_that_leaves_a_case_out_has_an_unknown_change(self, tmp_path, capsys):
        out = tmp_path / "out"
        findings = [_row("src/a.py", 10, "Divide by zero", "error")]
        labels = [_label("H1", "src/a.py", 10)]
        grades = [_grade("H1", "full", 0)]
        _put(out / "a", _score("a", [
            _case("c1", labels, findings, grades, elapsed_ms=1000), _case("c2", labels, findings, grades),
        ], None))
        _put(out / "b", _score("b", [
            _case("c1", labels, findings, grades, elapsed_ms=None, chunks_failed=None),
            _case("c2", labels, findings, grades, elapsed_ms=2000),
        ], None))

        text = _compare(out, "a", "b", capsys)

        assert "| Chunks failed | 0 | 0 (1 unknown) | unknown |" in text
        assert "| Elapsed | 2.0 s | 2.0 s (1 unknown) | unknown |" in text
        assert "| Judge cost | none | none | $0.0000 |" in text


class TestJudgeWarnings:
    PROMPT = "the judge prompt sha256 differs: A {a}, B {b}; the judge grades are not like for like"
    MODEL = "the judge model differs: A {a}, B {b}; the judge grades are not like for like"

    def test_a_different_judge_prompt_sha_warns_naming_both(self, tmp_path, capsys, caplog):
        out = tmp_path / "out"
        _put(out / "a", _one_label_score("a", "full", judge=_judge(sha=SHA_A)))
        _put(out / "b", _one_label_score("b", "full", judge=_judge(sha=SHA_B)))

        _compare(out, "a", "b", capsys)

        assert _warnings(caplog) == [self.PROMPT.format(a=f'"{SHA_A}"', b=f'"{SHA_B}"')]

    def test_a_different_judge_model_warns_naming_both(self, tmp_path, capsys, caplog):
        out = tmp_path / "out"
        _put(out / "a", _one_label_score("a", "full", judge=_judge(model="judge-m")))
        _put(out / "b", _one_label_score("b", "full", judge=_judge(model="judge-n")))

        _compare(out, "a", "b", capsys)

        assert _warnings(caplog) == [self.MODEL.format(a='"judge-m"', b='"judge-n"')]

    def test_two_runs_without_a_judge_never_warn(self, tmp_path, capsys, caplog):
        out = tmp_path / "out"
        _put(out / "a", _one_label_score("a", "full", method=MATCH_METHOD))
        _put(out / "b", _one_label_score("b", "none", method=MATCH_METHOD))

        text = _compare(out, "a", "b", capsys)

        assert _warnings(caplog) == []
        assert "| Judge cost | none | none | $0.0000 |" in text

    def test_a_run_without_judge_labels_against_a_judged_run_does_not_warn(self, tmp_path, capsys, caplog):
        out = tmp_path / "out"
        _put(out / "a", _one_label_score("a", "full", method=MATCH_METHOD))
        _put(out / "b", _one_label_score("b", "full", judge=_judge()))

        text = _compare(out, "a", "b", capsys)

        assert _warnings(caplog) == []
        assert "| Judge cost | none | $0.0020 | +$0.0020 |" in text

    def test_a_null_judge_differs_when_both_runs_graded_judge_tier_labels(self, tmp_path, capsys, caplog):
        out = tmp_path / "out"
        _put(out / "a", _one_label_score("a", "none", method=evals.JUDGE_METHOD, judge=None))
        _put(out / "b", _one_label_score("b", "full", judge=_judge()))

        _compare(out, "a", "b", capsys)

        assert _warnings(caplog) == [
            self.PROMPT.format(a="null", b=f'"{SHA_A}"'),
            self.MODEL.format(a="null", b='"judge-m"'),
        ]


class TestCaseSets:
    def test_different_case_sets_warn_and_list_what_only_one_run_holds_sorted(self, tmp_path, capsys, caplog):
        out = tmp_path / "out"
        findings = [_row("src/a.py", 10, "Divide by zero", "error")]
        h1 = _label("H1", "src/a.py", 10)
        h2 = _label("H2", "src/a.py", 20)
        h5 = _label("H5", "src/a.py", 50)
        _put(out / "a", _score("a", [
            _case("c1", [h1, h5], findings, [_grade("H1", "full", 0), _grade("H5", "none")]),
            _case("c9", [h1], findings, [_grade("H1", "full", 0)]),
        ], _judge()))
        _put(out / "b", _score("b", [
            _case("c1", [h1], findings, [_grade("H1", "full", 0)]),
            _case("c3", [h1, h2], findings, [_grade("H1", "full", 0), _grade("H2", "none")]),
        ], _judge()))

        text = _compare(out, "a", "b", capsys)

        assert _warnings(caplog) == [
            "the runs cover different cases (only in A: c9; only in B: c3); the metrics are not like for like"
        ]
        assert text.endswith(
            "## Only in one run\n\n"
            "- Label `H5` of case `c1` at `src/a.py:50`: only in A\n"
            "- Case `c3`: only in B (2 labels)\n"
            "- Case `c9`: only in A (1 label)\n"
        )
        assert "## Changed labels\n\nNone.\n" in text


class TestReplayStamps:
    REPLAY = (
        "case {case!r}: the replay description differs: A {a}, B {b}; the two arms did not see the same "
        "PR description, so this case is not a like-for-like comparison"
    )

    def test_a_differing_description_stamp_warns_per_case_and_a_missing_key_reads_as_null(
        self, tmp_path, capsys, caplog,
    ):
        out = tmp_path / "out"

        def score(label: str) -> dict[str, Any]:
            return _score(label, [
                _case(case_id, [_label("H1", "src/a.py", 10)], [], [_grade("H1", "none")])
                for case_id in ("c1", "c2", "c3", "c4", "c5")
            ], _judge())

        def stamp(description: Any = ...) -> dict[str, Any]:
            replay: dict[str, Any] = {"base_sha": "1" * 40, "head_sha": "2" * 40}
            if description is not ...:
                replay["description"] = description
            return {"findings": [], "replay": replay}

        _put(out / "a", score("a"), {
            "c1": stamp("pinned"),
            "c2": stamp(),
            "c4": {"findings": []},
            "c5": {"findings": [], "replay": None},
        })
        _write_json(out / "a/cases/c3/error.json", {"case_id": "c3", "error": "RuntimeError: boom"})
        _put(out / "b", score("b"), {
            "c1": stamp("live"),
            "c2": stamp(None),
            "c3": stamp("pinned"),
            "c4": stamp("live"),
            "c5": stamp(None),
        })

        _compare(out, "a", "b", capsys)

        assert _warnings(caplog) == [
            self.REPLAY.format(case="c1", a='"pinned"', b='"live"'),
            self.REPLAY.format(case="c4", a="null", b='"live"'),
        ]

    def test_an_unreadable_record_is_refused_naming_its_side(self, tmp_path, capsys):
        out = tmp_path / "out"
        _put(out / "a", _one_label_score("a", "full", judge=_judge()), {"c1": {"findings": []}})
        _put(out / "b", _one_label_score("b", "full", judge=_judge()), {"c1": {"findings": []}})
        (out / "b/cases/c1/record.json").write_text("{not json", encoding="utf-8")

        args = cli._build_parser().parse_args(["eval", "compare", "a", "b", "--out", str(out)])
        with pytest.raises(ConfigError, match=r"^B: cannot read .*record\.json"):
            evals.eval_compare(args)
        assert capsys.readouterr().out == ""


class TestResolution:
    @staticmethod
    def _two_places(tmp_path: Path) -> Path:
        """``base`` exists both under ``--out`` (recall 100%) and in the working directory (recall 0%)."""
        out = tmp_path / "out"
        _put(out / "base", _one_label_score("base", "full", method=MATCH_METHOD))
        _put(tmp_path / "base", _one_label_score("base", "none", method=MATCH_METHOD))
        return out

    def test_a_safe_id_that_is_also_a_working_directory_path_is_the_label_under_out(
        self, tmp_path, capsys, monkeypatch,
    ):
        out = self._two_places(tmp_path)
        monkeypatch.chdir(tmp_path)

        text = _compare(out, "base", "base", capsys)

        assert "| Recall (micro) | 100.0% (1 of 1) | 100.0% (1 of 1) | 0.0 pp |" in text

    def test_a_path_that_is_not_a_safe_id_is_the_run_directory(self, tmp_path, capsys, monkeypatch):
        out = self._two_places(tmp_path)
        monkeypatch.chdir(tmp_path)

        text = _compare(out, "./base", "base", capsys)

        assert "- A: ./base\n- B: base\n" in text
        assert "| Recall (micro) | 0.0% (0 of 1) | 100.0% (1 of 1) | +100.0 pp |" in text

    def test_a_safe_id_with_no_label_under_out_is_a_path(self, tmp_path, capsys, monkeypatch):
        self._two_places(tmp_path)
        monkeypatch.chdir(tmp_path)

        text = _compare(tmp_path / "elsewhere", "base", "base", capsys)

        assert "| Recall (micro) | 0.0% (0 of 1) | 0.0% (0 of 1) | 0.0 pp |" in text

    def test_run_directories_given_by_path_print_as_given(self, tmp_path, capsys):
        run_a = _put(tmp_path / "runs" / "one", _one_label_score("one", "none", method=MATCH_METHOD))
        run_b = _put(tmp_path / "runs" / "two", _one_label_score("two", "full", method=MATCH_METHOD))

        text = _compare(tmp_path / "out", str(run_a), str(run_b), capsys)

        assert text.startswith(f"# prxref eval compare\n\n- A: {run_a}\n- B: {run_b}\n\n## Metrics\n")
        assert "| c1 | H1 | src/a.py:10 | none (0) | full (1) |" in text


class TestConfigErrors:
    @staticmethod
    def _scored(out: Path) -> None:
        _put(out / "cand", _one_label_score("cand", "full", method=MATCH_METHOD))

    def _main(self, capsys, *argv: str) -> tuple[int, str, str]:
        capsys.readouterr()
        code = cli.main(["eval", "compare", *argv])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    def test_a_missing_run_exits_2_naming_a(self, tmp_path, capsys):
        out = tmp_path / "out"
        self._scored(out)

        code, stdout, err = self._main(capsys, "nope", "cand", "--out", str(out))

        assert (code, stdout) == (2, "")
        assert err.startswith("configuration error: A: there is no scored run 'nope': it is neither a label")

    def test_a_missing_run_exits_2_naming_b(self, tmp_path, capsys):
        out = tmp_path / "out"
        self._scored(out)

        code, stdout, err = self._main(capsys, "cand", str(tmp_path / "missing"), "--out", str(out))

        assert (code, stdout) == (2, "")
        assert err.startswith("configuration error: B: there is no scored run")

    def test_an_unscored_label_exits_2_and_says_to_score_it_first(self, tmp_path, capsys):
        out = tmp_path / "out"
        self._scored(out)
        _write_json(out / "base" / "run.json", {"version": 1, "label": "base", "case_ids": []})

        code, stdout, err = self._main(capsys, "cand", "base", "--out", str(out))

        assert (code, stdout) == (2, "")
        assert err.startswith("configuration error: B: the run 'base' is not scored yet")
        assert f"run 'prxref eval score --label base --out {out}' first" in err

    def test_an_unscored_run_directory_names_its_label_and_parent(self, tmp_path, capsys):
        out = tmp_path / "out"
        self._scored(out)
        run_dir = tmp_path / "runs" / "base"
        _write_json(run_dir / "run.json", {"version": 1, "label": "base", "case_ids": []})

        code, _, err = self._main(capsys, str(run_dir), "cand", "--out", str(out))

        assert code == 2
        assert err.startswith("configuration error: A: the run")
        assert f"'prxref eval score --label base --out {run_dir.resolve().parent}'" in err

    @pytest.mark.parametrize("version", [2, 0, "1", True, None])
    def test_a_score_json_of_another_version_exits_2_naming_the_side_and_path(self, tmp_path, capsys, version):
        out = tmp_path / "out"
        self._scored(out)
        score = _one_label_score("base", "full", method=MATCH_METHOD)
        score["version"] = version
        _put(out / "base", score)

        code, stdout, err = self._main(capsys, "base", "cand", "--out", str(out))

        assert (code, stdout) == (2, "")
        assert err.startswith(f"configuration error: A: {out / 'base' / 'score.json'}: score.json version")

    @pytest.mark.parametrize("text", ["{not json", "[]", '{"version": 1, "metrics": {}, "cases": {}}'])
    def test_an_unreadable_or_malformed_score_json_exits_2_naming_the_side(self, tmp_path, capsys, text):
        out = tmp_path / "out"
        self._scored(out)
        (out / "base").mkdir()
        (out / "base" / "score.json").write_text(text, encoding="utf-8")

        code, stdout, err = self._main(capsys, "cand", "base", "--out", str(out))

        assert (code, stdout) == (2, "")
        assert err.startswith("configuration error: B: ")
        assert "score.json" in err


def _write_run(out: Path, label: str, cases: list[tuple[EvalCase, dict]]) -> None:
    """Lay a run out as ``eval run`` writes it."""
    for case, record in cases:
        _write_json(out / label / "cases" / case.id / "case.json", case_to_json(case))
        _write_json(out / label / "cases" / case.id / "record.json", record)
    _write_json(out / label / "run.json", {
        "version": 1, "label": label, "cases_path": "cases.json", "created_at": "2026-09-24T00:00:00Z",
        "case_ids": [case.id for case, _ in cases], "prompts": None, "sampling": None, "review_rules": None,
        "config": None,
    })


class TestThroughTheCli:
    CASE = EvalCase(
        id="c1", diff_file="c1.patch",
        expected=(_label("H1", "src/a.py", 10, must_match="divide by zero"),),
    )

    @staticmethod
    def _record(findings: list[dict], cost_usd: float) -> dict[str, Any]:
        return {
            "verdict": "Request-Changes", "findings": findings, "chunks_failed": 0, "elapsed_ms": 1000,
            "cost_usd": cost_usd, "cost_estimated": False,
            "replay": {"base_sha": None, "head_sha": None, "threads": "hidden", "diff_file": "c1.patch",
                       "description": "file", "as_of": None, "as_of_source": None},
        }

    def test_cli_main_compares_two_runs_scored_by_eval_score(self, tmp_path, capsys, caplog):
        out = tmp_path / "out"
        _write_run(out, "base", [(self.CASE, self._record([_row("src/a.py", 11, "Divide by zero", "error")], 0.01))])
        _write_run(out, "cand", [(self.CASE, self._record([], 0.02))])
        for label in ("base", "cand"):
            assert cli.main(["eval", "score", "--label", label, "--out", str(out)]) == 0
        caplog.clear()
        capsys.readouterr()

        argv = ["eval", "compare", "base", "cand", "--out", str(out)]
        first = cli.main(argv), capsys.readouterr().out
        second = cli.main(argv), capsys.readouterr().out

        assert first == second
        code, text = first
        assert code == 0
        assert text.startswith("# prxref eval compare\n\n- A: base\n- B: cand\n\n## Metrics\n")
        assert "| Recall (micro) | 100.0% (1 of 1) | 0.0% (0 of 1) | -100.0 pp |" in text
        assert "| Review cost | $0.0100 | $0.0200 | +$0.0100 |" in text
        assert "| Judge cost | none | none | $0.0000 |" in text
        assert "| c1 | H1 | src/a.py:10 | full (1) | none (0) |" in text
        assert text.endswith("## Only in one run\n\nNone.\n")
        assert _warnings(caplog) == []
