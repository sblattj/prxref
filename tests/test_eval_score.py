"""``prxref eval score``: grading a run in two tiers into ``score.json`` and ``score.md`` (#14).

Most tests write a run directory by hand, exactly as ``eval run`` lays it out
(``run.json``, then ``cases/<id>/case.json`` plus ``record.json`` or
``error.json``), so a record can hold any row order, a grouped row or a failed
case on demand. One test builds its run with :func:`prxref.evals.eval_run`
itself. The judge is a stub client returned by a patched
``prxref.llm_backends.create_llm_client``, the factory
:func:`prxref.eval_judge.build_judge_client` looks up at call time, so nothing
reaches a network. :class:`TestThroughTheCli` goes through ``cli.main``.
"""
from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from prxref import cli, evals
from prxref.eval_cases import EvalCase, ExpectedFinding, case_to_json
from prxref.judge import JUDGE_PROMPT_VERSION, judge_prompt_sha
from prxref.llm import ConfigError, InvokeResult

JUDGE = "judge-m"
REVIEWER = "reviewer-m"
SAMPLING = {"temperature": 0.0, "seed": 7, "models": [REVIEWER]}
RUN_PROMPTS = {"sha256": {"worker": "1" * 64, "systemic": "2" * 64, "summary": "3" * 64}, "prompt_templates": None}
RUN_CONFIG = {"llm_backend": "openai-compat", "llm_models": [REVIEWER]}


def _row(file: str, line: int | None, title: str, *, severity: str = "warning",
         drop_reason: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "file": file, "line": line, "severity": severity, "confidence": 0.9, "scope": "unknown",
        "title": title, "body": f"{title}.", "drop_reason": drop_reason, **extra,
    }


def _record(findings=(), *, verdict: str = "Request-Changes", cost_usd: float | None = 0.01,
            cost_estimated: bool = False, elapsed_ms: int | None = 1000, chunks_failed: int | None = 0) -> dict:
    return {
        "verdict": verdict, "findings": list(findings), "chunk_count": 1, "chunks_reviewed": 1,
        "chunks_failed": chunks_failed, "elapsed_ms": elapsed_ms, "input_tokens": 100, "output_tokens": 50,
        "cost_usd": cost_usd, "cost_estimated": cost_estimated, "posted": False, "review_rules": None,
        "ticket_context": None, "spec_grounding": None, "size_advisory": None, "sampling": SAMPLING,
    }


def _label(label_id: str, file: str, line: int, severity: str = "error", **fields: Any) -> ExpectedFinding:
    return ExpectedFinding(label_id, file, line, severity, **fields)


def _case(case_id: str, *labels: ExpectedFinding) -> EvalCase:
    return EvalCase(id=case_id, expected=tuple(labels), diff_file=f"{case_id}.patch")


def _write(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def _write_run(out: Path, cases: list[tuple[EvalCase, dict | str]], *, label: str = "L",
               sampling: Any = SAMPLING) -> Path:
    """Lay a run out as ``eval run`` does; a ``str`` outcome is that case's ``error.json`` text."""
    run_dir = out / label
    for case, outcome in cases:
        case_dir = run_dir / "cases" / case.id
        (case_dir / "trace").mkdir(parents=True)
        _write(case_dir / "case.json", case_to_json(case))
        if isinstance(outcome, str):
            _write(case_dir / "error.json", {"case_id": case.id, "error": outcome})
        else:
            _write(case_dir / "record.json", outcome)
    _write(run_dir / "run.json", {
        "version": 1, "label": label, "cases_path": "cases.json", "created_at": "2026-09-24T00:00:00Z",
        "case_ids": [case.id for case, _ in cases], "prompts": RUN_PROMPTS, "sampling": sampling,
        "review_rules": None, "config": RUN_CONFIG,
    })
    return run_dir


def _args(out: Path, *, label: str = "L", judge_model: str | None = None):
    argv = ["eval", "score", f"--label={label}", "--out", str(out)]
    if judge_model is not None:
        argv += ["--judge-model", judge_model]
    return cli._build_parser().parse_args(argv)


def _reply(*grades: tuple[str, str, str | None]) -> str:
    return json.dumps({"grades": [{"human_id": h, "grade": g, "ai_ref": ref} for h, g, ref in grades]})


class StubJudge:
    """A judge client: ``answer(user)`` gives each reply (or raises), and every call is recorded."""

    def __init__(self, model: str, answer: Callable[[str], str], *, cost_usd: float | None = 0.002) -> None:
        self.models = [model]
        self.temperature = 0.0
        self.seed = 5
        self.answer = answer
        self.cost_usd = cost_usd
        self.calls: list[dict[str, Any]] = []

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=None):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens, "json_mode": json_mode})
        return InvokeResult(
            text=self.answer(user), model=self.models[0], backend="stub", input_tokens=900,
            output_tokens=100, cost_usd=self.cost_usd, cost_source="stub" if self.cost_usd is not None else "",
        )


@pytest.fixture
def judge(monkeypatch):
    """Patch the client factory; set ``state.answer`` / ``state.cost_usd`` before scoring."""
    state = SimpleNamespace(built=[], clients=[], answer=lambda user: _reply(), cost_usd=0.002)

    def create(cfg=None, session=None):
        state.built.append(copy.deepcopy(cfg))
        client = StubJudge(cfg["llm_models"][0], lambda user: state.answer(user), cost_usd=state.cost_usd)
        state.clients.append(client)
        return client

    monkeypatch.setattr("prxref.llm_backends.create_llm_client", create)
    state.calls = lambda: [call for client in state.clients for call in client.calls]
    return state


def _score(out: Path, **kwargs: Any) -> tuple[dict, str]:
    assert evals.eval_score(_args(out, **kwargs)) == 0
    run_dir = out / kwargs.get("label", "L")
    score = json.loads((run_dir / "score.json").read_text(encoding="utf-8"))
    return score, (run_dir / "score.md").read_text(encoding="utf-8")


def _finding(score: dict, case_id: str, human_id: str) -> dict:
    row = next(row for row in score["cases"] if row["case_id"] == case_id)
    return next(entry for entry in row["findings"] if entry["human_id"] == human_id)


CASE_A = _case("a", _label("H1", "src/a.py", 10, "error", category="logic", accepted=True,
                           must_match="divide by zero"))
RECORD_A = _record([
    _row("src/a.py", 11, "Divide by zero", severity="error"),
    _row("src/a.py", 40, "Unused import"),
], verdict="Request-Changes", cost_usd=0.01, elapsed_ms=1200)
CASE_B = _case(
    "b",
    _label("J1", "src/b.py", 5, "warning", category="security", accepted=False, text="Token logged in clear."),
    _label("J2", "src/b.py", 20, "minor", text="Name is vague."),
)
RECORD_B = _record([_row("src/b.py", 5, "Secret in log", severity="error")],
                   verdict="Request-Changes", cost_usd=0.02, elapsed_ms=800)
CASE_C = _case("c", _label("K1", "src/c.py", 3, "error", must_match="leak"))
REPLY_B = _reply(("J1", "partial", "A1"), ("J2", "none", None))


def _golden_run(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    _write_run(out, [(CASE_A, RECORD_A), (CASE_B, RECORD_B), (CASE_C, "RuntimeError: boom")])
    return out


GOLDEN = """\
# prxref eval score: L

Recall (micro): 37.5% (credit 1.5 of 4 scored labels) over 3 cases

## Failed cases

- `c`: RuntimeError: boom

## Cases

| Case | Verdict | Recall | Credit | Full | Partial | None | Judge error | AI findings | Unmatched AI | \
Chunks failed | Elapsed | Review cost | Judge cost |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| a | Request-Changes | 100.0% | 1 of 1 | 1 | 0 | 0 | 0 | 2 | 1 | 0 | 1.2 s | $0.0100 | $0.0000 |
| b | Request-Changes | 25.0% | 0.5 of 2 | 0 | 1 | 1 | 0 | 1 | 0 | 0 | 0.8 s | $0.0200 | $0.0020 |
| c | failed | 0.0% | 0 of 1 | 0 | 0 | 1 | 0 | 0 | 0 | unknown | unknown | unknown | $0.0000 |

## Recall by severity

| Severity | Recall | Credit | Full | Partial | None | Judge error |
|---|---:|---:|---:|---:|---:|---:|
| error | 50.0% | 1 of 2 | 1 | 0 | 1 | 0 |
| minor | 0.0% | 0 of 1 | 0 | 0 | 1 | 0 |
| warning | 50.0% | 0.5 of 1 | 0 | 1 | 0 | 0 |

## Recall by category

| Category | Recall | Credit | Full | Partial | None | Judge error |
|---|---:|---:|---:|---:|---:|---:|
| (none) | 0.0% | 0 of 2 | 0 | 0 | 2 | 0 |
| logic | 100.0% | 1 of 1 | 1 | 0 | 0 | 0 |
| security | 50.0% | 0.5 of 1 | 0 | 1 | 0 | 0 |

## Accepted labels

Recall over accepted labels: 100.0% (credit 1 of 1 scored labels).

## Unmatched AI findings

1 of 3 active AI findings matched no label: 0.33 per PR.

## Severity agreement

Agreed on 1 of 2 credited labels (50.0%); a human `minor` counts as `warning`.

- human `error`, AI `error`: 1
- human `warning`, AI `error`: 1

## Chunks failed

0 in total; unknown for 1 case(s), which are not counted.

## Elapsed

2.0 s in total; unknown for 1 case(s), which are not counted.

## Cost

- Review: unknown (1 of 3 case(s) unpriced; an unknown cost is never summed)
- Judge: $0.0020, 1 call(s), 0 case(s) from the cache

## Judge

- Model: `judge-m`
- Prompt: version {version}, sha256 `{sha}`
- Self-judged: no
- Judge errors: none
"""


class TestGolden:
    def test_a_three_case_run_renders_the_golden_score_md(self, tmp_path, judge, capsys):
        out = _golden_run(tmp_path)
        judge.answer = lambda user: REPLY_B

        score, markdown = _score(out, judge_model=JUDGE)

        assert markdown == GOLDEN.format(version=JUDGE_PROMPT_VERSION, sha=judge_prompt_sha())
        assert capsys.readouterr().out.splitlines() == [
            "Recall (micro): 37.5% (credit 1.5 of 4 scored labels) over 3 cases",
            f"score: {out / 'L' / 'score.md'}",
        ]
        assert len(judge.calls()) == 1

    def test_score_json_keeps_its_key_order_and_stamps(self, tmp_path, judge):
        out = _golden_run(tmp_path)
        judge.answer = lambda user: REPLY_B

        score, _ = _score(out, judge_model=JUDGE)

        assert list(score) == ["version", "label", "run", "judge", "failed", "metrics", "cases"]
        assert score["version"] == evals.SCORE_VERSION == 1
        assert score["label"] == "L"
        assert score["run"] == {"prompts": RUN_PROMPTS, "sampling": SAMPLING, "review_rules": None,
                                "scoped_rules": None, "config": RUN_CONFIG}
        assert list(score["judge"]) == [
            "model", "sampling", "prompt_version", "prompt_sha256", "self_judged",
            "cost_usd", "cost_estimated", "llm_calls", "cached", "errors",
        ]
        assert score["judge"]["model"] == JUDGE
        assert score["judge"]["sampling"] == {"temperature": 0.0, "seed": 5, "models": [JUDGE]}
        assert score["judge"]["prompt_version"] == JUDGE_PROMPT_VERSION
        assert score["judge"]["prompt_sha256"] == judge_prompt_sha()
        assert (score["judge"]["cost_usd"], score["judge"]["llm_calls"], score["judge"]["cached"]) == (0.002, 1, 0)
        assert score["failed"] == [{"case_id": "c", "error": "RuntimeError: boom"}]
        assert [row["case_id"] for row in score["cases"]] == ["a", "b", "c"]
        assert _finding(score, "a", "H1")["method"] == "must_match"
        assert _finding(score, "b", "J1")["method"] == evals.JUDGE_METHOD == "judge"

    def test_a_failed_case_counts_its_labels_as_none_without_a_judge_call(self, tmp_path, judge):
        out = tmp_path / "out"
        _write_run(out, [(_case("c", _label("J9", "src/c.py", 3, text="Leaks.")), "RuntimeError: boom")])

        score, _ = _score(out, judge_model=JUDGE)

        assert judge.calls() == []
        assert _finding(score, "c", "J9")["grade"] == "none"
        assert score["metrics"]["recall"]["scored"] == 1
        assert score["cases"][0]["verdict"] is None

    def test_an_error_verdict_is_scored_as_none_and_counts_in_the_denominator(self, tmp_path, judge):
        out = tmp_path / "out"
        case = _case("e", _label("J1", "src/e.py", 3, text="Leaks."), _label("M1", "src/e.py", 9, must_match="x"))
        _write_run(out, [(case, _record([], verdict="Error", cost_usd=None, chunks_failed=1))])

        score, markdown = _score(out, judge_model=JUDGE)

        assert judge.calls() == []
        assert [_finding(score, "e", h)["grade"] for h in ("J1", "M1")] == ["none", "none"]
        assert score["metrics"]["recall"] == {
            "recall": 0.0, "credit": 0.0, "scored": 2, "full": 0, "partial": 0, "none": 2, "judge_error": 0,
        }
        assert score["cases"][0]["verdict"] == "Error"
        assert score["metrics"]["chunks_failed"] == {"total": 1, "missing": 0}
        assert score["failed"] == []
        assert "| e | Error | 0.0% | 0 of 2 |" in markdown


class TestRefBridge:
    def test_a_dropped_row_before_the_credited_one_shifts_the_index_to_the_right_row(self, tmp_path, judge):
        out = tmp_path / "out"
        case = _case("b", _label("J1", "src/b.py", 6, "error", text="Token logged in clear."))
        record = _record([
            _row("src/b.py", 4, "Gated out", drop_reason="confidence"),
            _row("src/b.py", 30, "Naming", severity="warning"),
            _row("src/b.py", 6, "Token logged", severity="error"),
        ])
        _write_run(out, [(case, record)])
        judge.answer = lambda user: _reply(("J1", "full", "A2"))

        score, _ = _score(out, judge_model=JUDGE)

        entry = _finding(score, "b", "J1")
        assert (entry["grade"], entry["ai_ref"], entry["ai_line"]) == ("full", 2, 6)
        assert (entry["ai_severity"], entry["severity_agrees"]) == ("error", True)
        assert score["cases"][0]["unmatched_ai"] == 1

    def test_the_judge_sees_only_the_labels_without_must_match(self, tmp_path, judge):
        out = tmp_path / "out"
        case = _case("m", _label("M1", "src/m.py", 3, must_match="overflow"),
                     _label("J1", "src/m.py", 3, text="Overflows on large input."))
        _write_run(out, [(case, _record([_row("src/m.py", 3, "Integer overflow")]))])
        judge.answer = lambda user: _reply(("J1", "full", "A1"))

        score, _ = _score(out, judge_model=JUDGE)

        [call] = judge.calls()
        assert '"J1"' in call["user"] and '"M1"' not in call["user"]
        assert call["json_mode"] is True
        assert [_finding(score, "m", h)["method"] for h in ("J1", "M1")] == ["judge", "must_match"]
        assert (tmp_path / "out/L/cases/m/trace/judge.user.md").is_file()


class TestJudgeErrors:
    def test_a_judge_call_that_raises_makes_every_judge_label_judge_error(self, tmp_path, judge):
        out = tmp_path / "out"
        case = _case("b", _label("J1", "src/b.py", 5, text="Token logged."), _label("J2", "src/b.py", 9, text="x"),
                     _label("M1", "src/b.py", 5, must_match="secret"))
        _write_run(out, [(case, _record([_row("src/b.py", 5, "Secret in log")]))])

        def boom(user):
            raise TimeoutError("judge timed out")
        judge.answer = boom

        score, markdown = _score(out, judge_model=JUDGE)

        assert [_finding(score, "b", h)["grade"] for h in ("J1", "J2", "M1")] == ["judge_error", "judge_error", "full"]
        assert [_finding(score, "b", h)["credit"] for h in ("J1", "J2")] == [None, None]
        assert score["metrics"]["recall"]["scored"] == 1
        assert score["metrics"]["recall"]["judge_error"] == 2
        assert score["judge"]["errors"] == [
            {"case_id": "b", "error": "judge call failed: TimeoutError: judge timed out"},
        ]
        assert "- Judge error in `b`: judge call failed: TimeoutError: judge timed out" in markdown
        assert "(credit 1 of 1 scored labels; 2 with a judge error, not scored)" in markdown

    def test_a_rejected_reply_is_a_judge_error_never_none(self, tmp_path, judge):
        out = tmp_path / "out"
        _write_run(out, [(_case("b", _label("J1", "src/b.py", 5, text="x")), _record([_row("src/b.py", 5, "y")]))])
        judge.answer = lambda user: "not json at all"

        score, _ = _score(out, judge_model=JUDGE)

        assert _finding(score, "b", "J1")["grade"] == "judge_error"
        assert score["metrics"]["recall"]["recall"] is None
        assert score["judge"]["errors"][0]["error"].startswith("judge response rejected:")


class TestJudgeModelFlag:
    def test_a_label_without_must_match_needs_judge_model_before_any_call(self, tmp_path, judge):
        out = _golden_run(tmp_path)

        with pytest.raises(ConfigError, match=r"^--judge-model: required, because 2 label\(s\) have no must_match"):
            evals.eval_score(_args(out))

        assert judge.built == [] and judge.calls() == []
        assert not (out / "L/score.json").exists() and not (out / "L/score.md").exists()

    @pytest.mark.parametrize("judge_model", [None, JUDGE])
    def test_no_judge_is_built_when_every_label_has_must_match(self, tmp_path, judge, judge_model):
        out = tmp_path / "out"
        _write_run(out, [(CASE_A, RECORD_A)])

        score, markdown = _score(out, judge_model=judge_model)

        assert judge.built == []
        assert score["judge"] is None
        assert _finding(score, "a", "H1")["grade"] == "full"
        assert "No judge: every label has a must_match predicate." in markdown
        assert "- Judge: none (no judge ran)" in markdown

    def test_an_empty_judge_model_is_refused_naming_the_flag(self, tmp_path, judge):
        out = _golden_run(tmp_path)
        with pytest.raises(ConfigError, match=r"^--judge-model: must name one model"):
            evals.eval_score(_args(out, judge_model="  "))
        assert judge.calls() == []


class TestCacheAndSelfJudging:
    def test_a_second_score_is_served_from_the_cache_with_no_call(self, tmp_path, judge):
        out = _golden_run(tmp_path)
        judge.answer = lambda user: REPLY_B
        first, _ = _score(out, judge_model=JUDGE)
        assert len(judge.calls()) == 1

        second, markdown = _score(out, judge_model=JUDGE)

        assert len(judge.calls()) == 1
        assert (second["judge"]["llm_calls"], second["judge"]["cached"]) == (0, 1)
        assert second["judge"]["cost_usd"] == 0.0
        assert second["cases"] == [{**row, "judge_cost_usd": 0.0} for row in first["cases"]]
        assert list((out / "L/judge-cache").glob("*.json"))
        assert "- Judge: $0.0000, 0 call(s), 1 case(s) from the cache" in markdown

    def test_a_judge_model_among_the_reviewers_models_warns_and_is_stamped(self, tmp_path, judge, caplog):
        out = _golden_run(tmp_path)
        judge.answer = lambda user: REPLY_B

        with caplog.at_level(logging.WARNING):
            score, markdown = _score(out, judge_model=REVIEWER)

        assert any("is also a reviewer model" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
        assert score["judge"]["self_judged"] is True
        assert f"**Self-judged:** the judge model `{REVIEWER}` is also one of the reviewer's models" in markdown
        assert "- Self-judged: yes" in markdown

    def test_a_distinct_judge_model_logs_no_warning(self, tmp_path, judge, caplog):
        out = _golden_run(tmp_path)
        judge.answer = lambda user: REPLY_B
        with caplog.at_level(logging.WARNING):
            score, markdown = _score(out, judge_model=JUDGE)
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert score["judge"]["self_judged"] is False
        assert "**Self-judged:**" not in markdown


class TestCost:
    def test_a_none_review_cost_is_never_summed(self, tmp_path, judge):
        out = tmp_path / "out"
        _write_run(out, [
            (_case("a", _label("M1", "src/a.py", 1, must_match="x")), _record(cost_usd=0.01)),
            (_case("b", _label("M2", "src/b.py", 1, must_match="x")), _record(cost_usd=None)),
        ])

        score, markdown = _score(out)

        assert score["metrics"]["review_cost"] == {
            "total_usd": None, "per_pr_usd": None, "priced": 1, "unpriced": 1, "estimated": 0,
        }
        assert "- Review: unknown (1 of 2 case(s) unpriced; an unknown cost is never summed)" in markdown
        assert ("| b | Request-Changes | 0.0% | 0 of 1 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 1.0 s | unknown | "
                "$0.0000 |") in markdown
        assert "$0.0100 in total" not in markdown

    def test_every_priced_case_is_summed_and_an_estimate_is_flagged(self, tmp_path, judge):
        out = tmp_path / "out"
        _write_run(out, [
            (_case("a", _label("M1", "src/a.py", 1, must_match="x")), _record(cost_usd=0.01)),
            (_case("b", _label("M2", "src/b.py", 1, must_match="x")), _record(cost_usd=0.03, cost_estimated=True)),
        ])
        _, markdown = _score(out)
        assert "- Review: $0.0400 in total, $0.0200 per PR (estimated for 1 case(s))" in markdown
        assert "| $0.0300 (est.) |" in markdown

    def test_an_unpriced_judge_call_is_unknown_not_zero(self, tmp_path, judge):
        out = _golden_run(tmp_path)
        judge.answer = lambda user: REPLY_B
        judge.cost_usd = None

        score, markdown = _score(out, judge_model=JUDGE)

        assert score["judge"]["cost_usd"] is None
        assert score["metrics"]["judge_cost"]["total_usd"] is None
        assert "- Judge: unknown, 1 call(s), 0 case(s) from the cache" in markdown
        assert ("| b | Request-Changes | 25.0% | 0.5 of 2 | 0 | 1 | 1 | 0 | 1 | 0 | 0 | 0.8 s | $0.0200 | "
                "unknown |") in markdown


class TestGroupedAndCap:
    @staticmethod
    def _grouped_run(tmp_path: Path, row: dict) -> Path:
        out = tmp_path / "out"
        case = _case("g", _label("M1", "src/g.py", 10, must_match="race"),
                     _label("M2", "src/g.py", 50, must_match="race"))
        _write_run(out, [(case, _record([row]))])
        return out

    def test_a_grouped_row_with_locations_credits_a_label_at_each_location(self, tmp_path, judge):
        row = _row("src/g.py", 10, "Race condition", locations=[{"file": "src/g.py", "line": 50}])
        score, _ = _score(self._grouped_run(tmp_path, row))

        assert [(_finding(score, "g", h)["grade"], _finding(score, "g", h)["ai_ref"],
                 _finding(score, "g", h)["ai_line"]) for h in ("M1", "M2")] == [("full", 0, 10), ("full", 0, 50)]
        assert score["cases"][0]["unmatched_ai"] == 0

    @pytest.mark.parametrize("extra", [{}, {"locations": None}])
    def test_a_row_without_locations_or_with_null_is_not_grouped(self, tmp_path, judge, extra):
        score, _ = _score(self._grouped_run(tmp_path, _row("src/g.py", 10, "Race condition", **extra)))
        assert [_finding(score, "g", h)["grade"] for h in ("M1", "M2")] == ["full", "none"]

    def test_the_cap_of_two_holds_across_the_deterministic_and_judge_tiers(self, tmp_path, judge, caplog):
        out = tmp_path / "out"
        case = _case(
            "k",
            _label("M1", "src/k.py", 3, must_match="overflow"),
            _label("M2", "src/k.py", 4, must_match="overflow"),
            _label("J1", "src/k.py", 3, text="Overflow."),
            _label("J2", "src/k.py", 30, text="Other."),
        )
        _write_run(out, [(case, _record([_row("src/k.py", 3, "Integer overflow"), _row("src/k.py", 30, "Other")]))])
        judge.answer = lambda user: _reply(("J1", "full", "A1"), ("J2", "partial", "A2"))

        with caplog.at_level(logging.WARNING):
            score, _ = _score(out, judge_model=JUDGE)

        grades = [_finding(score, "k", h)["grade"] for h in ("M1", "M2", "J1", "J2")]
        assert grades == ["full", "full", "none", "partial"]
        assert any("already credits 2 labels" in r.getMessage() for r in caplog.records)


class TestRunDirectory:
    def test_a_missing_run_is_refused_naming_label(self, tmp_path):
        with pytest.raises(ConfigError, match=r"^--label: there is no run .* to score \(no run\.json\)"):
            evals.eval_score(_args(tmp_path / "out"))

    @pytest.mark.parametrize("label", ["..", "a/b", "-x"])
    def test_a_label_that_is_not_one_path_segment_is_refused(self, tmp_path, label):
        with pytest.raises(ConfigError, match=r"^--label: must be one directory name"):
            evals.eval_score(_args(tmp_path / "out", label=label))

    def test_a_case_with_neither_record_nor_error_is_refused_naming_label(self, tmp_path):
        out = _golden_run(tmp_path)
        (out / "L/cases/c/error.json").unlink()
        with pytest.raises(ConfigError, match=r"^--label: the run is incomplete: case 'c' has neither"):
            evals.eval_score(_args(out, judge_model=JUDGE))

    def test_an_unreadable_run_json_is_refused_naming_label(self, tmp_path):
        out = _golden_run(tmp_path)
        (out / "L/run.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ConfigError, match=r"^--label: cannot read .*run\.json"):
            evals.eval_score(_args(out, judge_model=JUDGE))

    def test_a_bad_case_json_is_refused_naming_label_and_the_file(self, tmp_path):
        out = _golden_run(tmp_path)
        data = json.loads((out / "L/cases/a/case.json").read_text(encoding="utf-8"))
        data["expected"][0]["severity"] = "fatal"
        _write(out / "L/cases/a/case.json", data)
        with pytest.raises(ConfigError, match=r"^--label: .*case\.json: case 'a': expected\[0\]\.severity"):
            evals.eval_score(_args(out, judge_model=JUDGE))

    def test_only_the_run_json_case_ids_are_scored(self, tmp_path, judge):
        out = _golden_run(tmp_path)
        stale = out / "L/cases/zz"
        stale.mkdir()
        _write(stale / "case.json", case_to_json(_case("zz", _label("Z1", "src/z.py", 1, text="stale"))))
        judge.answer = lambda user: REPLY_B

        score, _ = _score(out, judge_model=JUDGE)

        assert [row["case_id"] for row in score["cases"]] == ["a", "b", "c"]

    def test_a_run_written_by_eval_run_scores(self, tmp_path, judge):
        data = tmp_path / "data"
        data.mkdir()
        (data / "a.patch").write_text(
            "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,1 +1,2 @@\n x = 1\n+y = x / 0\n",
            encoding="utf-8",
        )
        _write(data / "cases.json", {"version": 1, "cases": [{
            "id": "a", "diff_file": "a.patch",
            "expected": [{"id": "H1", "file": "src/a.py", "line": 2, "severity": "error", "must_match": "zero"}],
        }]})
        finding = SimpleNamespace(file="src/a.py", line=2, severity="error", confidence=0.9, scope="unknown",
                                  title="Division by zero", body="x / 0 raises.", drop_reason=None)
        result = {"verdict": "Request-Changes", "findings_active": [finding], "findings_dropped": [],
                  "chunks_failed": 0, "elapsed_ms": 5, "cost_usd": 0.001, "sampling": SAMPLING}
        run_args = cli._build_parser().parse_args(
            ["eval", "run", "--cases", str(data / "cases.json"), "--label=L", "--out", str(tmp_path / "out")]
        )
        assert evals.eval_run(run_args, run_review=lambda url, **kw: result, build_record=cli._build_json_result) == 0

        score, _ = _score(tmp_path / "out")

        assert _finding(score, "a", "H1")["grade"] == "full"
        assert score["run"]["sampling"] == SAMPLING


class TestThroughTheCli:
    def test_cli_main_scores_a_run_and_prints_the_headline_and_path(self, tmp_path, judge, capsys):
        out = _golden_run(tmp_path)
        judge.answer = lambda user: REPLY_B

        code = cli.main(["eval", "score", "--label", "L", "--out", str(out), "--judge-model", JUDGE])

        assert code == 0
        assert capsys.readouterr().out.splitlines() == [
            "Recall (micro): 37.5% (credit 1.5 of 4 scored labels) over 3 cases",
            f"score: {out / 'L' / 'score.md'}",
        ]
        assert (out / "L/score.md").read_text(encoding="utf-8") == GOLDEN.format(
            version=JUDGE_PROMPT_VERSION, sha=judge_prompt_sha(),
        )

    def test_cli_main_exits_2_naming_judge_model_before_any_call(self, tmp_path, judge, capsys):
        out = _golden_run(tmp_path)

        code = cli.main(["eval", "score", "--label", "L", "--out", str(out)])

        assert code == 2
        assert capsys.readouterr().err.startswith("configuration error: --judge-model: required")
        assert judge.built == [] and judge.calls() == []

    def test_cli_main_exits_2_naming_label_for_a_missing_run(self, tmp_path, capsys):
        code = cli.main(["eval", "score", "--label", "nope", "--out", str(tmp_path / "out")])
        assert code == 2
        assert capsys.readouterr().err.startswith("configuration error: --label: there is no run")
