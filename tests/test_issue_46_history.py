"""The review history file, the trend line and the regression gate (issue #46).

Acceptance, from the issue:

1. A committed per-repository history file records findings per PR and
   gate-drop counts, and the trend is surfaced in the executive summary.
2. An opt-in exit-code gate on regression beyond a tolerance.
3. The history file is diff-friendly: one line per run.

The CLI tests drive ``cli.main`` with the orchestrator and the LLM factory
stubbed, so nothing touches the network. The summary tests run the real
orchestrator against a fake forge and read the summary it posts.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

import prxref
from prxref import cli, history, orchestrator
from prxref.orchestrator import orchestrate_review
from prxref.triage import Finding
from tests.test_issue_143_stability_mode import B1, ScriptLLM, _limit
from tests.test_orchestrator import REF, FakeForge, multi_chunk_diff

DIFF = Path(__file__).resolve().parent / "fixtures" / "issue17" / "pr.diff"
NOW = datetime(2026, 11, 26, 9, 30, 0, tzinfo=UTC)


def _finding(severity="warning", *, drop_reason=None, line=3):
    return Finding(
        file="src/a.py", line=line, severity=severity, confidence=0.9, title="t", body="b",
        drop_reason=drop_reason,
    )


def _result(active=(), dropped=(), **over):
    res = {
        "verdict": "Request-Changes" if active else "Approved",
        "findings_active": list(active), "findings_dropped": list(dropped),
        "chunk_count": 1, "chunks_reviewed": 1, "chunks_failed": 0, "elapsed_ms": 5,
        "input_tokens": 10, "output_tokens": 2, "posted": False,
    }
    res.update(over)
    return res


def _seed(path: Path, totals) -> None:
    path.write_text(
        "".join(history.encode({"findings_total": n}) for n in totals), encoding="utf-8",
    )


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# --- the line format (criterion 3) ------------------------------------------


class TestTheLine:
    def test_the_exact_bytes_and_key_order(self):
        res = _result(
            [_finding("error"), _finding("warning"), _finding("warning", line=4)],
            [_finding(drop_reason="hedged: maybe"), _finding(drop_reason="confidence 0.2 < 0.5"),
             _finding(drop_reason="invalid severity 'x'")],
        )
        entry = history.make_line(res, pr=412, head_sha="abc123", version="9.9.9", now=NOW)
        assert history.encode(entry) == (
            '{"timestamp":"2026-11-26T09:30:00Z","pr":412,"head_sha":"abc123",'
            '"verdict":"Request-Changes","findings":{"error":1,"warning":2,"spec":0,"outofscope":0},'
            '"findings_total":3,"dropped":{"apply_hedge_gate":1,"apply_quality_gate":2},'
            '"dropped_total":3,"prxref_version":"9.9.9"}\n'
        )
        assert tuple(entry) == history.LINE_KEYS

    def test_a_line_is_one_line(self):
        entry = history.make_line(_result(), pr=1, head_sha="x\ny", version="1", now=NOW)
        text = history.encode(entry)
        assert text.count("\n") == 1 and text.endswith("\n")

    def test_append_adds_exactly_one_line_per_call(self, tmp_path):
        path = tmp_path / "h.jsonl"
        for n in range(3):
            assert history.append(str(path), {"findings_total": n}) is None
        assert path.read_text(encoding="utf-8").splitlines() == [
            '{"findings_total":0}', '{"findings_total":1}', '{"findings_total":2}',
        ]

    def test_a_partial_last_line_gets_a_newline_first(self, tmp_path):
        path = tmp_path / "h.jsonl"
        path.write_text('{"findings_total":1}\n{"findings_to', encoding="utf-8")
        assert history.append(str(path), {"findings_total": 5}) is None
        assert path.read_text(encoding="utf-8").splitlines()[-1] == '{"findings_total":5}'
        assert [e["findings_total"] for e in history.read(str(path)).entries] == [1, 5]

    def test_a_failed_append_returns_the_error_and_never_raises(self, tmp_path):
        assert history.append(str(tmp_path), {"findings_total": 1}).startswith("cannot append: ")


# --- reading and the baseline -----------------------------------------------


class TestRead:
    def test_a_missing_file_is_an_empty_history(self, tmp_path):
        assert history.read(str(tmp_path / "none.jsonl")) == history.History()

    def test_a_corrupt_line_is_skipped_with_a_warning(self, tmp_path, caplog):
        path = tmp_path / "h.jsonl"
        path.write_text(
            '{"findings_total":2}\nnot json\n\n[1,2]\n{"findings_total":-1}\n'
            '{"findings_total":true}\n{"findings_total":4}\n',
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING, logger="prxref.history"):
            hist = history.read(str(path))
        assert [e["findings_total"] for e in hist.entries] == [2, 4]
        assert hist.skipped == 4
        assert [r.getMessage().rsplit(" ", 1)[-1] for r in caplog.records] == ["2", "4", "5", "6"]

    def test_an_unreadable_file_is_recorded_not_raised(self, tmp_path):
        assert history.read(str(tmp_path)).error.startswith("cannot read: ")

    def test_the_baseline_is_the_mean_of_the_last_twenty(self):
        hist = history.History(tuple({"findings_total": n} for n in [100] * 5 + [2] * 20))
        base = history.baseline(hist)
        assert (base.prior_runs, base.mean, base.window) == (20, 2.0, 20)

    def test_fewer_than_three_is_not_enough(self):
        two = history.baseline(history.History(({"findings_total": 1}, {"findings_total": 1})))
        three = history.baseline(history.History(({"findings_total": 1},) * 3))
        assert (two.enough, three.enough) == (False, True)


# --- the trend line (criterion 1) -------------------------------------------


def _base(*totals):
    return history.baseline(history.History(tuple({"findings_total": n} for n in totals)))


class TestTrendNote:
    def test_it_says_per_pr_and_the_drop_total(self):
        assert history.trend_note(_base(1, 2, 4), 5, 4) == (
            "> 📈 Trend: 5 findings on this PR vs a mean of 2.3 per PR over the last 3 runs "
            "(+114%); 4 dropped by quality gates."
        )

    def test_none_below_three_prior_runs(self):
        assert history.trend_note(_base(1, 2), 5, 0) == ""
        assert history.trend_note(None, 5, 0) == ""

    def test_a_zero_mean_leaves_out_the_percentage(self):
        assert history.trend_note(_base(0, 0, 0), 1, 0) == (
            "> 📈 Trend: 1 finding on this PR vs a mean of 0 per PR over the last 3 runs; "
            "0 dropped by quality gates."
        )

    def test_a_drop_is_negative(self):
        assert "(-50%)" in history.trend_note(_base(4, 4, 4), 2, 0)


@pytest.mark.usefixtures("contract_stubs")
class TestTheSummaryCarriesTheTrend:
    def _review(self, base, diff=None):
        forge = FakeForge(diff=multi_chunk_diff(2) if diff is None else diff)
        llm = ScriptLLM({B1: [[_limit()]]})
        res = orchestrate_review(forge, REF, llm, post=True, max_workers=1, history_baseline=base)
        return res, forge

    def test_the_posted_summary_has_the_trend_line(self):
        res, forge = self._review(_base(0, 0, 0))
        (summary,) = forge.summaries
        trend = "> 📈 Trend: 1 finding on this PR vs a mean of 0 per PR over the last 3 runs; "
        assert trend in summary
        assert res["history"]["trend"].startswith(trend)
        assert res["history"]["head_sha"] == forge.pr.source_sha

    def test_without_enough_history_there_is_none(self):
        res, forge = self._review(_base(0, 0))
        assert "Trend:" not in forge.summaries[0]
        assert res["history"]["trend"] == ""

    def test_without_a_baseline_the_summary_and_record_are_unchanged(self):
        res, forge = self._review(None)
        assert "Trend:" not in forge.summaries[0]
        assert "history" not in res

    def test_the_summary_only_path_carries_it_too(self):
        res, forge = self._review(_base(2, 2, 2), diff="")
        assert res["chunks_reviewed"] == 0
        assert "> 📈 Trend: 0 findings on this PR vs a mean of 2 per PR over the last 3 runs (-100%)" in (
            forge.summaries[0]
        )
        assert res["history"]["trend"].startswith("> 📈 Trend: 0 findings")


# --- the regression gate math (criterion 2) ---------------------------------


class TestRegressionMath:
    def test_above_the_limit_fires(self):
        out = history.regression(_base(2, 2, 2), 4, enabled=True, tolerance=0.5)
        assert out == {"enabled": True, "fired": True, "threshold": 3.0, "reason": "regression"}

    def test_at_the_limit_does_not(self):
        out = history.regression(_base(2, 2, 2), 3, enabled=True, tolerance=0.5)
        assert (out["fired"], out["reason"]) == (False, "within tolerance")

    def test_mean_zero_zero_findings_does_not_fire_one_does(self):
        assert not history.regression(_base(0, 0, 0), 0, enabled=True, tolerance=0.5)["fired"]
        assert history.regression(_base(0, 0, 0), 1, enabled=True, tolerance=0.5)["fired"]

    def test_too_few_prior_runs_never_fires(self):
        out = history.regression(_base(0, 0), 50, enabled=True, tolerance=0.0)
        assert out == {
            "enabled": True, "fired": False, "threshold": None,
            "reason": "skipped: 2 prior run(s), at least 3 needed",
        }

    def test_off_never_fires(self):
        assert history.regression(_base(0, 0, 0), 50, enabled=False, tolerance=0.0) == {
            "enabled": False, "fired": False, "threshold": None, "reason": "off",
        }


# --- through the CLI --------------------------------------------------------


@pytest.fixture
def stub(monkeypatch):
    """Stub the orchestrator; ``stub.result`` is what the next review returns."""
    class _Stub:
        result = _result()
        calls: list[dict] = []

    def _orchestrate(**kwargs):
        _Stub.calls.append(kwargs)
        return dict(_Stub.result)

    monkeypatch.setattr("prxref.orchestrator.orchestrate_review", _orchestrate)
    monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: object())
    monkeypatch.setattr(cli, "make_forge", lambda ref: object())
    _Stub.calls = []
    return _Stub


@pytest.fixture
def hfile(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".prxref-history.jsonl"
    monkeypatch.setenv("PRXREF_HISTORY_FILE", str(path))
    return path


def _review(*extra: str) -> int:
    return cli.main(["review", "--diff-file", str(DIFF), "--no-post", *extra])


def _json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


class TestTheCliAppends:
    def test_one_line_per_run(self, stub, hfile, capsys):
        stub.result = _result([_finding("error")], [_finding(drop_reason="hedged: x")])
        assert _review() == 0
        assert _review() == 0
        (one, two) = _lines(hfile)
        assert tuple(one) == history.LINE_KEYS
        assert (one["pr"], one["findings_total"], one["dropped"], one["prxref_version"]) == (
            0, 1, {"apply_hedge_gate": 1}, prxref.__version__,
        )
        assert two["findings_total"] == 1

    def test_the_json_record(self, stub, hfile, capsys):
        _seed(hfile, [1, 1, 1])
        stub.result = _result([_finding()] * 2)
        assert _review("--format", "json") == 0
        rec = _json(capsys)["history"]
        assert list(rec) == [
            "file", "written", "reason", "error", "read_error", "skipped_lines",
            "prior_runs", "mean", "window", "trend", "regression",
        ]
        assert (rec["written"], rec["prior_runs"], rec["mean"], rec["window"]) == (True, 3, 1.0, 20)
        assert rec["regression"]["reason"] == "off"

    def test_the_text_output_says_where_it_went(self, stub, hfile, capsys):
        assert _review() == 0
        out = capsys.readouterr().out
        assert f"history: appended to {hfile} (0 prior runs, mean -)" in out

    def test_off_by_default_writes_nothing_and_emits_null(self, stub, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert _review("--format", "json") == 0
        assert _json(capsys)["history"] is None
        assert list(tmp_path.iterdir()) == []
        assert "history_baseline" in stub.calls[0] and stub.calls[0]["history_baseline"] is None

    @pytest.mark.parametrize(("over", "reason"), [
        ({"verdict": "Error"}, "verdict Error"),
        ({"verdict": "Incomplete"}, "verdict Incomplete"),
        ({"chunks_failed": 1}, "partial review"),
        ({"incremental": {"mode": "incremental"}}, "incremental review of part of the PR"),
    ])
    def test_a_run_that_did_not_see_the_whole_pr_is_not_appended(self, stub, hfile, capsys, over, reason):
        stub.result = _result([_finding()], **over)
        _review("--format", "json")
        rec = _json(capsys)["history"]
        assert (rec["written"], rec["reason"]) == (False, reason)
        assert not hfile.exists()

    def test_a_write_failure_is_recorded_and_the_exit_code_stays(self, stub, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PRXREF_HISTORY_FILE", str(tmp_path))
        stub.result = _result([_finding("error")])
        assert _review("--format", "json") == 0
        payload = _json(capsys)
        assert payload["history"]["written"] is False
        assert payload["history"]["error"].startswith("cannot append: ")
        assert payload["verdict"] == "Request-Changes"

    def test_a_corrupt_line_is_counted_and_skipped(self, stub, hfile, capsys):
        hfile.write_text('{"findings_total":1}\ngarbage\n{"findings_total":1}\n{"findings_total":1}\n')
        assert _review("--format", "json") == 0
        rec = _json(capsys)["history"]
        assert (rec["skipped_lines"], rec["prior_runs"], rec["written"]) == (1, 3, True)


class TestTheCliGate:
    def test_exit_1_on_a_regression_and_0_with_the_gate_off(self, stub, hfile, monkeypatch, capsys):
        _seed(hfile, [0, 0, 0])
        stub.result = _result([_finding()] * 2)
        assert _review() == 0  # control: gate off
        _seed(hfile, [0, 0, 0])
        monkeypatch.setenv("PRXREF_FAIL_ON_REGRESSION", "on")
        assert _review() == 1
        err = capsys.readouterr().err
        assert "PRXREF_FAIL_ON_REGRESSION=on: 2 active findings is above 0" in err

    def test_within_tolerance_is_0(self, stub, hfile, monkeypatch, capsys):
        _seed(hfile, [2, 2, 2])
        monkeypatch.setenv("PRXREF_FAIL_ON_REGRESSION", "on")
        stub.result = _result([_finding()] * 3)
        assert _review() == 0
        monkeypatch.setenv("PRXREF_REGRESSION_TOLERANCE", "0.4")
        _seed(hfile, [2, 2, 2])
        assert _review() == 1

    def test_too_few_prior_runs_is_0(self, stub, hfile, monkeypatch, capsys):
        _seed(hfile, [0, 0])
        monkeypatch.setenv("PRXREF_FAIL_ON_REGRESSION", "on")
        stub.result = _result([_finding()] * 9)
        assert _review("--format", "json") == 0
        assert _json(capsys)["history"]["regression"]["reason"].startswith("skipped: 2 prior run(s)")

    def test_a_run_not_recorded_is_not_gated(self, stub, hfile, monkeypatch, capsys):
        _seed(hfile, [0, 0, 0])
        monkeypatch.setenv("PRXREF_FAIL_ON_REGRESSION", "on")
        stub.result = _result([_finding()] * 9, chunks_failed=1)
        assert _review() == 0

    def test_fail_on_keeps_its_meaning(self, stub, hfile, monkeypatch, capsys):
        _seed(hfile, [5, 5, 5])
        monkeypatch.setenv("PRXREF_FAIL_ON", "error")
        stub.result = _result([_finding("error")])
        assert _review() == 1  # FAIL_ON=error trips on its own, gate off
        stub.result = _result([_finding("warning")])
        monkeypatch.setenv("PRXREF_FAIL_ON_REGRESSION", "on")
        assert _review() == 0  # neither trips

    def test_the_gate_forces_a_full_review_under_incremental(self, stub, hfile, monkeypatch, capsys):
        monkeypatch.setenv("PRXREF_INCREMENTAL", "on")
        monkeypatch.setenv("PRXREF_FAIL_ON_REGRESSION", "on")
        _review()
        assert stub.calls[-1]["full_review_reason"] == "PRXREF_FAIL_ON_REGRESSION=on"
        monkeypatch.setenv("PRXREF_FAIL_ON_REGRESSION", "off")
        _review()
        assert stub.calls[-1]["full_review_reason"] is None


class TestConfigErrors:
    @pytest.mark.parametrize(("env", "value"), [
        ("PRXREF_FAIL_ON_REGRESSION", "yes"),
        ("PRXREF_REGRESSION_TOLERANCE", "abc"),
        ("PRXREF_REGRESSION_TOLERANCE", "-0.1"),
        ("PRXREF_REGRESSION_TOLERANCE", "11"),
    ])
    def test_a_bad_value_exits_2_naming_the_variable(self, stub, hfile, monkeypatch, capsys, env, value):
        monkeypatch.setenv(env, value)
        assert _review() == 2
        assert env in capsys.readouterr().err
        assert stub.calls == []

    def test_the_gate_without_a_file_exits_2(self, stub, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PRXREF_FAIL_ON_REGRESSION", "on")
        assert _review() == 2
        err = capsys.readouterr().err
        assert "PRXREF_FAIL_ON_REGRESSION" in err and "PRXREF_HISTORY_FILE" in err

    @pytest.mark.parametrize(("key", "reason"), [
        ("history_file", "local write"),
        ("fail_on_regression", "gate"),
        ("regression_tolerance", "gate"),
    ])
    def test_a_config_file_cannot_set_them(self, stub, hfile, capsys, key, reason):
        Path(".prxref.toml").write_text(f'{key} = "x"\n', encoding="utf-8")
        assert _review() == 2
        err = capsys.readouterr().err
        assert f"'{key}' cannot be set in a repository config file ({reason})" in err
        assert f"set PRXREF_{key.upper()} in the pipeline instead" in err


class TestTheRealIncrementalRecord:
    """The skip rule reads the orchestrator's own ``incremental`` record."""

    def _record(self, mode):
        scope = orchestrator._IncrementalScope(mode, reason=None if mode == "incremental" else "r")
        return orchestrator._incremental_record(scope, 3, None)

    def test_a_scoped_run_is_skipped(self):
        res = _result(incremental=self._record("incremental"))
        assert cli._history_skip_reason(res) == "incremental review of part of the PR"

    def test_a_full_run_under_incremental_is_recorded(self):
        assert cli._history_skip_reason(_result(incremental=self._record("full"))) is None
        assert cli._history_skip_reason(_result(incremental=orchestrator._incremental_record(
            None, 0, None,
        ))) is None


class TestOtherCallers:
    def test_run_review_without_history_ignores_the_variable(self, stub, hfile):
        res = cli._run_review(None, post=False, diff_file=str(DIFF))
        assert res["history"] is None
        assert stub.calls[0]["history_baseline"] is None
        assert not hfile.exists()

    def test_eval_run_never_writes_the_file(self, stub, hfile, tmp_path):
        fixture = DIFF.parent
        argv = ["eval", "run", "--cases", str(fixture / "cases.json"), "--label", "L",
                "--out", str(tmp_path / "out")]
        assert cli.main(argv) == 0
        assert stub.calls and all(c["history_baseline"] is None for c in stub.calls)
        assert not hfile.exists()
