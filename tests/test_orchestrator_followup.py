"""The context follow-up in the orchestrator and the CLI (#22): ``orchestrate_review(context_followup=...)``.

The orchestrator takes the level (``off`` or ``on``), gates it on the
repository-context level and reader, resolves the confidence floor once,
calls :func:`prxref.followup.run_chunk_followup` in each chunk worker after
the first attempt, records one row per chunk and the run totals, and emits
one ``context_followup ok`` event. The CLI passes the config value and emits
the record in ``--format json`` after ``parse_retries``.

These tests run the REAL reviewer over ``tests/fixtures/issue22`` with a
recording LLM that answers the chunk holding ``assistant/progress.py`` with
one sub-floor question, the follow-up prompt with a scripted reply, and
everything else, the sweep included, with no findings. No test touches the
network.
"""
from __future__ import annotations

import inspect
import json
import logging
import threading
from pathlib import Path

import pytest

from prxref import cli, followup, orchestrator
from prxref.forges.repo_dir import RepoDir
from prxref.llm import InvokeResult
from prxref.repo_followup import FOLLOWUP_HEADER
from prxref.triage import Finding, build_chunks, parse_unified_diff
from tests.test_orchestrator import REF, FakeForge

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issue22"
REPO = FIXTURE / "repo"
PATCH_FILE = FIXTURE / "pr.patch"
PATCH = PATCH_FILE.read_text(encoding="utf-8")
PROGRESS = "assistant/progress.py"
WARN_NAME = "PRXREF_CONTEXT_FOLLOWUP"
SWEEP_MARK = "running a systemic sweep"
NO_FINDINGS = json.dumps({"findings": []})
R2_BODY = (
    "`_ledger` puts a live `ProgressLedger` object into `run.root().data`. `Engine.run_turn` passes that "
    "same `run` to `self.store.save(run)` on handoff. `StateStore` is not shown here. Does it serialize "
    "frame data in a way that fails on arbitrary objects, e.g. JSON? The conftest turns the feature off for "
    "every existing test, so save/resume with the default-on setting looks untested."
)
QUESTION = {
    "file": PROGRESS, "line": 47, "severity": "warning", "confidence": 0.5,
    "title": "Progress ledger in frame data may break state serialization", "body": R2_BODY,
}
CONFIRMATION = {
    "file": PROGRESS, "line": 47, "severity": "error", "confidence": 0.9,
    "title": "Progress ledger breaks StateStore.save",
    "body": "`StateStore.save` calls `json.dumps(asdict(run))`, which raises on the live `ProgressLedger`.",
}
INACTIVE = {
    "active": False, "calls": 0, "confirmed": 0, "unconfirmed": 0, "discarded": 0,
    "input_tokens": 0, "output_tokens": 0, "chunks": None,
}
FAKE_BLOCK = FOLLOWUP_HEADER + "\n\nnote\n\nassistant/state_store.py:8: class StateStore:"


class _ScriptLLM:
    """Records every ``(system, user)``; the progress chunk gets ``QUESTION``, a follow-up gets ``followup_text``."""

    def __init__(self, *, followup_text: str = NO_FINDINGS, timeout_progress: bool = False):
        self.calls: list[tuple[str, str]] = []
        self.followup_text = followup_text
        self.timeout_progress = timeout_progress
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        with self._lock:
            self.calls.append((system, user))
            first_progress = (
                self.timeout_progress
                and f"diff --git a/{PROGRESS} " in user
                and SWEEP_MARK not in system
            )
            if first_progress:
                self.timeout_progress = False
        if first_progress:
            raise TimeoutError("request timeout after 60s")
        if SWEEP_MARK in system:
            text = NO_FINDINGS
        elif FOLLOWUP_HEADER in user:
            text = self.followup_text
        elif f"diff --git a/{PROGRESS} " in user:
            text = json.dumps({"findings": [QUESTION]})
        else:
            text = NO_FINDINGS
        return InvokeResult(
            text=text, input_tokens=10, output_tokens=5, model="fake-model", backend="fake", elapsed_ms=1,
        )

    def worker_calls(self) -> list[tuple[str, str]]:
        return [(s, u) for s, u in self.calls if SWEEP_MARK not in s]


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    monkeypatch.setattr(orchestrator, "_elapsed_ms", lambda t0: 0)


def _review(llm: _ScriptLLM | None = None, *, repo_dir: bool = True, **kwargs):
    llm = llm if llm is not None else _ScriptLLM()
    kwargs.setdefault("repo_context", "repo")
    kwargs.setdefault("max_workers", 1)
    res = orchestrator.orchestrate_review(
        FakeForge(diff=PATCH), REF, llm, post=False,
        repo_dir=RepoDir(REPO) if repo_dir else None, **kwargs,
    )
    return res, llm


def _followup_warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING and WARN_NAME in r.getMessage()]


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _chunk_total(res: dict) -> int:
    return res["chunk_count"] - 1


class _FakeDriver:
    """Stands in for ``followup.run_chunk_followup``: records its kwargs, re-sends once, returns a set row."""

    def __init__(self, *, block: str = FAKE_BLOCK):
        self.calls: list[dict] = []
        self.block = block
        self._lock = threading.Lock()

    def __call__(self, first, **kwargs):
        with self._lock:
            self.calls.append({"first": first, **kwargs})
        rerun = kwargs["invoke"](self.block)
        result = dict(first)
        result["findings"] = [Finding(
            file=PROGRESS, line=47, severity="error", confidence=0.9,
            title="Replaced by the fake follow-up", body="The fake driver's finding.",
        )] if kwargs["index"] == 1 else list(first["findings"])
        row = followup.skipped_row()
        row.update(called=True, confirmed=1, input_tokens=rerun["input_tokens"], output_tokens=rerun["output_tokens"])
        return result, row


# --------------------------------------------------------------------------- the argument


class TestTheArgument:
    @pytest.mark.parametrize("value", ["ON", "true", "1", "", None])
    def test_an_unknown_value_raises_naming_it(self, value):
        with pytest.raises(ValueError, match="context_followup"):
            orchestrator.orchestrate_review(FakeForge(diff=PATCH), REF, _ScriptLLM(), context_followup=value)

    def test_the_default_is_off(self):
        default = inspect.signature(orchestrator.orchestrate_review).parameters["context_followup"].default
        assert default == "off"
        assert orchestrator.FOLLOWUP_MODES == ("off", "on")


# --------------------------------------------------------------------------- off


class TestOff:
    @pytest.mark.parametrize("level", ["off", "diff", "repo"])
    def test_off_changes_nothing_and_records_null(self, level, caplog, tmp_path):
        caplog.set_level(logging.INFO, logger="prxref")
        implicit, llm_a = _review(repo_context=level, trace_file=str(tmp_path / "a.jsonl"))
        explicit, llm_b = _review(repo_context=level, context_followup="off", trace_file=str(tmp_path / "b.jsonl"))

        assert implicit["context_followup"] is None
        assert explicit["context_followup"] is None
        assert llm_a.calls == llm_b.calls
        assert not any(FOLLOWUP_HEADER in user for _, user in llm_a.calls)
        assert implicit["repo_context"] == explicit["repo_context"]
        assert _followup_warnings(caplog) == []
        assert not any("follow-up" in r.getMessage() for r in caplog.records)
        for name in ("a.jsonl", "b.jsonl"):
            nodes = {e["node"] for e in _events(tmp_path / name)}
            assert not nodes & {"followup", "context_followup"}

    def test_off_never_resolves_the_floor_or_calls_the_driver(self, monkeypatch):
        def _refuse(*args, **kwargs):
            raise AssertionError("must not run with the follow-up off")

        monkeypatch.setattr(orchestrator, "_resolve_confidence_floor", _refuse)
        monkeypatch.setattr(followup, "run_chunk_followup", _refuse)
        res, _ = _review()
        assert res["context_followup"] is None


# --------------------------------------------------------------------------- the level gate


class TestTheLevelGate:
    @pytest.mark.parametrize("level", ["off", "diff"])
    def test_on_below_repo_warns_once_and_makes_no_extra_call(self, level, caplog, monkeypatch):
        driver = _FakeDriver()
        monkeypatch.setattr(followup, "run_chunk_followup", driver)
        caplog.set_level(logging.INFO, logger="prxref")
        off, llm_off = _review(repo_context=level)
        caplog.clear()
        on, llm_on = _review(repo_context=level, context_followup="on")

        (warning,) = _followup_warnings(caplog)
        assert warning.getMessage() == orchestrator.FOLLOWUP_INACTIVE_WARNING
        assert "PRXREF_REPO_CONTEXT=repo" in warning.getMessage()
        assert on["context_followup"] == INACTIVE
        assert llm_on.calls == llm_off.calls
        assert driver.calls == []
        assert off["findings_dropped"] == on["findings_dropped"]

    def test_on_at_repo_with_no_reader_warns_once(self, caplog, monkeypatch):
        driver = _FakeDriver()
        monkeypatch.setattr(followup, "run_chunk_followup", driver)
        caplog.set_level(logging.WARNING, logger="prxref")
        res, llm = _review(repo_dir=False, context_followup="on")

        assert len(_followup_warnings(caplog)) == 1
        assert res["context_followup"] == INACTIVE
        assert res["repo_context"]["reader"] is None
        assert driver.calls == []
        assert not any(FOLLOWUP_HEADER in user for _, user in llm.calls)

    def test_an_early_exit_carries_the_inactive_record(self):
        forge = FakeForge(diff="")
        res = orchestrator.orchestrate_review(
            forge, REF, _ScriptLLM(), post=False, repo_context="repo", context_followup="on",
        )
        assert res["context_followup"] == INACTIVE


# --------------------------------------------------------------------------- the plumbing


class TestThePlumbing:
    @pytest.fixture
    def driver(self, monkeypatch):
        fake = _FakeDriver()
        monkeypatch.setattr(followup, "run_chunk_followup", fake)
        return fake

    def test_each_chunk_calls_the_driver_once_and_the_sweep_never(self, driver):
        res, llm = _review(context_followup="on")

        total = _chunk_total(res)
        assert total >= 2
        assert sorted(call["index"] for call in driver.calls) == list(range(1, total + 1))
        assert {call["total"] for call in driver.calls} == {total}
        sweeps = [user for system, user in llm.calls if SWEEP_MARK in system]
        assert len(sweeps) == 1
        assert FOLLOWUP_HEADER not in sweeps[0]
        assert len(llm.calls) == 2 * total + 1

    def test_the_driver_gets_the_plan_the_floor_and_the_shown_text(self, driver):
        _review(context_followup="on", context_exclude_globs=("vendor/**",))

        for call in driver.calls:
            assert call["floor"] == pytest.approx(0.6)
            assert isinstance(call["listing_paths"], frozenset) and "assistant/state_store.py" in call["listing_paths"]
            assert call["listing_complete"] is True
            assert call["exclude"]("config/.env") is True
            assert call["exclude"]("vendor/lib.py") is True
            assert call["exclude"](PROGRESS) is False
            assert callable(call["read"])
            assert call["tracer"] is not None
            paths = [f.path for f in call["chunk"]]
            assert all(f"diff --git a/{path} " in call["shown"] for path in paths)
            assert {f.path for f in call["all_files"]} >= set(paths)

    def test_an_explicit_floor_reaches_the_driver(self, driver):
        _review(context_followup="on", confidence_floor=0.75)
        assert {call["floor"] for call in driver.calls} == {0.75}

    def test_the_environment_floor_reaches_the_driver(self, driver, monkeypatch):
        monkeypatch.setenv("PRXREF_CONFIDENCE_FLOOR", "0.55")
        _review(context_followup="on")
        assert {call["floor"] for call in driver.calls} == {0.55}

    def test_the_rerun_is_the_first_prompt_with_the_block_appended_last(self, driver):
        _, llm = _review(context_followup="on")

        workers = llm.worker_calls()
        firsts = [(s, u) for s, u in workers if FAKE_BLOCK not in u]
        reruns = [(s, u) for s, u in workers if FAKE_BLOCK in u]
        assert len(firsts) == len(reruns) == len(driver.calls)
        for (system, first), (rerun_system, rerun) in zip(firsts, reruns, strict=True):
            assert rerun_system == system
            assert rerun.count("\n\n" + FAKE_BLOCK) == 1
            assert rerun.replace("\n\n" + FAKE_BLOCK, "", 1) == first

    def test_the_rerun_uses_the_followup_trace_label_and_no_parse_retry(self, driver, monkeypatch, tmp_path):
        seen: list[dict] = []
        real = orchestrator._invoke_chunk

        def _spy(*args, **kwargs):
            seen.append(kwargs)
            return real(*args, **kwargs)

        monkeypatch.setattr(orchestrator, "_invoke_chunk", _spy)
        _review(context_followup="on", trace_dir=str(tmp_path), llm_parse_retries=1)

        reruns = [kw for kw in seen if kw.get("extra_blocks")]
        assert {kw["trace_label"] for kw in reruns} == {f"chunk{i}.followup" for i in range(len(driver.calls))}
        assert {kw["parse_retries"] for kw in reruns} == {0}
        assert all(kw["unit"] is not None for kw in reruns)
        assert (tmp_path / "chunk0.followup.user.md").is_file()
        assert FAKE_BLOCK in (tmp_path / "chunk0.followup.user.md").read_text(encoding="utf-8")

    def test_the_record_and_the_run_event_carry_the_rows(self, driver, tmp_path):
        trace = tmp_path / "t.jsonl"
        res, _ = _review(context_followup="on", trace_file=str(trace))

        record = res["context_followup"]
        total = _chunk_total(res)
        assert list(record) == list(INACTIVE)
        assert record["active"] is True
        assert record["calls"] == total
        assert record["confirmed"] == total
        assert (record["input_tokens"], record["output_tokens"]) == (10 * total, 5 * total)
        assert len(record["chunks"]) == total
        assert all(row["called"] is True for row in record["chunks"])
        (event,) = [e for e in _events(trace) if e["node"] == "context_followup"]
        assert event["phase"] == "ok"
        assert event["meta"] == {k: v for k, v in record.items() if k != "chunks"}

    def test_the_driver_result_replaces_the_chunk_result(self, driver):
        res, _ = _review(context_followup="on")

        titles = [f.title for f in res["findings_active"]]
        assert "Replaced by the fake follow-up" in titles

    def test_a_raising_driver_keeps_the_first_result(self, monkeypatch, caplog):
        def _boom(first, **kwargs):
            raise RuntimeError("driver exploded")

        monkeypatch.setattr(followup, "run_chunk_followup", _boom)
        caplog.set_level(logging.WARNING, logger="prxref")
        on, _ = _review(context_followup="on")
        off, _ = _review()

        assert on["verdict"] == off["verdict"]
        assert [f.drop_reason for f in on["findings_dropped"]] == [f.drop_reason for f in off["findings_dropped"]]
        assert all(row["error"] == "driver exploded" for row in on["context_followup"]["chunks"])
        assert any("driver exploded" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- the real driver


class TestTheRealDriver:
    def test_a_question_about_state_store_is_looked_up_and_confirmed(self):
        res, llm = _review(
            _ScriptLLM(followup_text=json.dumps({"findings": [CONFIRMATION]})), context_followup="on",
        )

        record = res["context_followup"]
        assert (record["active"], record["calls"], record["confirmed"]) == (True, 1, 1)
        (rerun,) = [u for _, u in llm.calls if FOLLOWUP_HEADER in u]
        assert "assistant/state_store.py:8:" in rerun
        assert "json.dumps(asdict(run))" in rerun
        active = [(f.file, f.line, f.severity) for f in res["findings_active"]]
        assert (PROGRESS, 47, "error") in active
        rows = [row for row in record["chunks"] if row["called"]]
        assert rows[0]["names"][0] == "StateStore"

    def test_a_refuting_rerun_leaves_the_question_unconfirmed(self):
        res, _ = _review(context_followup="on")

        record = res["context_followup"]
        assert (record["calls"], record["confirmed"], record["unconfirmed"]) == (1, 0, 1)
        (dropped,) = [f for f in res["findings_dropped"] if f.file == PROGRESS]
        assert dropped.drop_reason.startswith("not confirmed by context follow-up")

    def test_the_rerun_tokens_are_counted(self):
        on, _ = _review(context_followup="on")
        off, _ = _review()
        assert on["input_tokens"] == off["input_tokens"] + 10
        assert on["output_tokens"] == off["output_tokens"] + 5


# --------------------------------------------------------------------------- the timeout retry


class TestTheTimeoutRetry:
    def test_a_timeout_retried_chunk_is_skipped_without_the_driver(self, monkeypatch):
        driver = _FakeDriver()
        monkeypatch.setattr(followup, "run_chunk_followup", driver)
        res, llm = _review(_ScriptLLM(timeout_progress=True), context_followup="on")

        total = _chunk_total(res)
        rows = res["context_followup"]["chunks"]
        skipped = [i for i, row in enumerate(rows, start=1) if row["skipped"] == "timeout-retry"]
        assert len(skipped) == 1
        assert rows[skipped[0] - 1] == followup.skipped_row("timeout-retry")
        assert sorted(call["index"] for call in driver.calls) == [i for i in range(1, total + 1) if i not in skipped]
        assert res["context_followup"]["calls"] == total - 1
        assert len(llm.calls) == total + 1 + (total - 1) + 1

    def test_the_real_driver_never_runs_on_the_retried_chunk(self):
        res, llm = _review(_ScriptLLM(timeout_progress=True), context_followup="on")

        assert not any(FOLLOWUP_HEADER in user for _, user in llm.calls)
        assert res["context_followup"]["calls"] == 0


# --------------------------------------------------------------------------- _invoke_chunk(extra_blocks=)


class TestExtraBlocks:
    @pytest.fixture
    def captured(self, monkeypatch):
        blocks: list[str] = []

        def _review_chunk(llm, chunk, **kwargs):
            blocks.append(kwargs["context_blocks"])
            return [], {"input_tokens": 0, "output_tokens": 0, "model": "m", "elapsed_ms": 0}

        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _review_chunk)
        return blocks

    @staticmethod
    def _chunk():
        files = parse_unified_diff(PATCH)
        chunk = next(c for c in build_chunks(files) if any(f.path == PROGRESS for f in c))
        return chunk, files

    @staticmethod
    def _reader(path: str) -> str | None:
        target = REPO / path
        return target.read_text(encoding="utf-8") if target.is_file() else None

    def _call(self, reader, **kwargs):
        chunk, files = self._chunk()
        pr = FakeForge().get_pr(REF)
        return orchestrator._invoke_chunk(None, chunk, pr, None, None, reader, all_files=files, **kwargs)

    @pytest.mark.parametrize("with_reader", [True, False])
    def test_an_empty_extra_block_renders_byte_identical_blocks(self, captured, with_reader):
        reader = self._reader if with_reader else None
        self._call(reader)
        self._call(reader, extra_blocks="")
        assert captured[0] == captured[1]

    def test_a_block_is_appended_last_after_a_blank_line(self, captured):
        self._call(self._reader)
        self._call(self._reader, extra_blocks=FAKE_BLOCK)
        assert captured[0].strip()
        assert captured[1] == captured[0].strip() + "\n\n" + FAKE_BLOCK

    def test_a_block_alone_is_the_whole_context(self, captured):
        self._call(None, extra_blocks=FAKE_BLOCK)
        assert captured == [FAKE_BLOCK]


# --------------------------------------------------------------------------- the CLI


class TestTheCli:
    @pytest.fixture
    def recorder(self, monkeypatch):
        calls: list[dict] = []

        def _orchestrate(**kwargs):
            calls.append(kwargs)
            return {"verdict": "Approved", "findings_active": [], "findings_dropped": []}

        def _no_network(*args, **kwargs):
            raise AssertionError("no network")

        monkeypatch.setattr("prxref.orchestrator.orchestrate_review", _orchestrate)
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: object())
        monkeypatch.setattr("requests.Session.request", _no_network)
        return calls

    def test_the_default_reaches_orchestrate(self, recorder):
        assert cli.main(["review", "--diff-file", str(PATCH_FILE)]) == 0
        (kwargs,) = recorder
        assert kwargs["context_followup"] == "off"

    def test_the_environment_value_reaches_orchestrate(self, recorder, monkeypatch):
        monkeypatch.setenv(WARN_NAME, "on")
        assert cli.main(["review", "--diff-file", str(PATCH_FILE)]) == 0
        (kwargs,) = recorder
        assert kwargs["context_followup"] == "on"

    def test_a_bad_value_exits_2_naming_the_variable_before_orchestrate(self, recorder, monkeypatch, capsys):
        monkeypatch.setenv(WARN_NAME, "true")
        assert cli.main(["review", "--diff-file", str(PATCH_FILE)]) == 2
        assert recorder == []
        assert WARN_NAME in capsys.readouterr().err

    def test_the_webhook_passes_it_too(self, recorder, monkeypatch):
        monkeypatch.setenv(WARN_NAME, "on")
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: object())
        cli._webhook_handler("https://github.com/org/repo/pull/7")
        (kwargs,) = recorder
        assert kwargs["context_followup"] == "on"

    def test_the_json_key_follows_parse_retries(self):
        record = {**INACTIVE, "active": True, "chunks": []}
        keys = list(cli._build_json_result({"verdict": "Approved", "context_followup": record,
                                            "sampling": {"seed": 1}, "replay": {}}))
        assert keys[keys.index("parse_retries") + 1:] == [
            "context_followup", "suggestions", "incremental", "degraded", "config_file", "sampling",
            "replay",
        ]
        assert cli._build_json_result({"context_followup": record})["context_followup"] == record

    @pytest.mark.parametrize("result", [{}, None, {"verdict": "Approved"}])
    def test_the_json_key_is_null_when_absent(self, result):
        assert cli._build_json_result(result)["context_followup"] is None
