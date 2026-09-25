"""Cross-feature seams of the 0.14.0 second batch of features: each needs code from two merged features.

- #65 replay x #62 Azure DevOps. ``cli._replay_forge`` exits 2 when a forge
  cannot fetch a pinned commit range. Every built-in forge's real adapter must
  be accepted instead, so that exit stays reachable only with a fake. The forge
  set is ``BUILTIN_FORGES`` of tests/test_forge_compare_contract.py, so a new
  ``prxref.forges`` module joins on its own. No adapter here sends a request:
  each is built on a session that fails the test on any call.
- #65 replay x #67 cost. A ``--diff-file`` replay through ``main`` and the real
  orchestrator and reviewer, with an LLM double whose every answer reports its
  cost, emits both the ``replay`` stamp and the cost keys, and the cost is the
  sum over every LLM call the run made. A normal run with the same double has
  no ``replay`` key in its run record, its ``--format json`` payload or its
  ``run start`` trace event (CONTRACT §3.3: absent on normal runs).
- #66 subscription CLIs x #67 cost. The real ``ClaudeCLIClient`` and
  ``KiroCLIClient``, driven by the fake process harness of
  tests/test_llm_cli_backends.py, are the LLM of a full ``--diff-file`` run,
  so their results reach ``orchestrator._stamp_run_cost`` through its real
  caller. claude's ``total_cost_usd`` makes the run's cost a reported figure.
  kiro meters credits and reports neither dollars nor tokens, so the run's
  cost is unknown: ``None``, never ``0``, even when ``PRXREF_PRICE_TABLE``
  prices the kiro model, and never a partial sum beside a priced unit.
"""
from __future__ import annotations

import dataclasses
import importlib
import json
import logging
import math
import types

import pytest
import requests

import prxref.llm_cli_backends as clib
from prxref import cli, orchestrator
from prxref.cli import main
from prxref.config import make_forge
from prxref.forges.base import PRRef
from prxref.forges.replay import ReplayForge
from prxref.llm import ConfigError
from prxref.llm_cli_backends import ClaudeCLIClient, KiroCLIClient
from tests.test_forge_compare_contract import BUILTIN_FORGES, CASES
from tests.test_llm_cli_backends import (
    BINARY,
    CLEAN_RESULT,
    KIRO_BINARY,
    KIRO_MODEL,
    FakeRunner,
    Script,
    _init,
    _kiro_stream,
    _rate_limit,
    _result,
    _stream,
)
from tests.test_orchestrator import FakeForge, FakeLLM, _added_file_diff
from tests.test_replay import BASE, HEAD, RAW_OK

URL = "https://github.com/acme/widget/pull/7"
PINNED_STAMP = {
    "base_sha": BASE, "head_sha": HEAD, "threads": "hidden", "diff_file": None,
    "description": "none", "as_of": None, "as_of_source": None,
}
CHUNKS = 3
REVIEW_DIFF = "".join(_added_file_diff(f"src/mod_{i}.py", 6) for i in range(CHUNKS))
CALL_COST = 0.125

CLAUDE_MODEL = "claude-sonnet-5"
CLAUDE_CALL_COST = CLEAN_RESULT["total_cost_usd"]
CLAUDE_STREAM = _stream(_init(), _rate_limit(), _result(result=RAW_OK))
KIRO_STREAM = _kiro_stream(finalText=RAW_OK)
CLAUDE_TABLE = {CLAUDE_MODEL: {"input": 3.0, "output": 15.0}}
KIRO_TABLE = {KIRO_MODEL: {"input": 1.0, "output": 5.0}}
_REAL_STAMP_RUN_COST = orchestrator._stamp_run_cost


@pytest.fixture(autouse=True)
def _no_process_group_kill(monkeypatch):
    """The fake CLI process has a made-up pid, so no test here may signal a real process group."""
    monkeypatch.setattr(clib.os, "killpg", lambda pid, sig: None, raising=False)


class _NoNetworkSession(requests.Session):
    """A ``requests.Session`` that fails the test on any request and records it."""

    def __init__(self):
        super().__init__()
        self.sent: list[str] = []

    def request(self, method, url, *args, **kwargs):
        self.sent.append(f"{method} {url}")
        raise AssertionError(f"network request in a replay seam test: {method} {url}")

    def send(self, request, **kwargs):
        self.sent.append(f"{request.method} {request.url}")
        raise AssertionError(f"network request in a replay seam test: {request.method} {request.url}")


def _forge_impl(name: str) -> type:
    return importlib.import_module(f"prxref.forges.{name}").ForgeImpl


def _case_url(name: str) -> str:
    for case in CASES:
        if case.forge == name:
            return case.pr_url
    pytest.skip(
        f"prxref.forges.{name} has no CompareCase in tests/test_forge_compare_contract.py, "
        "so there is no PR URL to drive the CLI with"
    )


class TestPinnedReplayOverEveryBuiltinForge:
    """#65 x #62: no real adapter hits the pinned-range configuration error.

    ``--no-description`` keeps these runs off the network: since #16 a
    ``--pr-url`` replay reads the description history by default, which is
    a deliberate request covered in ``tests/test_replay_pinning.py``.
    """

    def test_the_forge_set_is_discovered_and_includes_azure_devops(self):
        assert "azure_devops" in BUILTIN_FORGES
        assert len(BUILTIN_FORGES) >= 5

    @pytest.mark.parametrize("no_threads", [True, False], ids=["blind", "threads-shown"])
    @pytest.mark.parametrize("name", BUILTIN_FORGES)
    def test_replay_forge_accepts_the_real_adapter(self, name, no_threads):
        session = _NoNetworkSession()
        forge = _forge_impl(name)(session=session)
        ref = PRRef(forge=name, host="", owner="acme", repo="api", number=1, url="")
        replay = cli._ReplayRequest(base_sha=BASE, head_sha=HEAD, no_threads=no_threads, no_description=True)
        assert isinstance(cli._replay_forge(forge, ref, replay), ReplayForge)
        assert session.sent == []

    def test_control_a_forge_without_compare_is_refused(self):
        ref = PRRef(forge="fake", host="", owner="acme", repo="api", number=1, url="")
        replay = cli._ReplayRequest(base_sha=BASE, head_sha=HEAD, no_threads=True)
        with pytest.raises(ConfigError) as exc:
            cli._replay_forge(object(), ref, replay)
        assert str(exc.value) == "--base-sha/--head-sha: the fake forge cannot fetch a pinned commit range"

    @pytest.mark.parametrize("name", BUILTIN_FORGES)
    def test_cli_hands_the_orchestrator_a_replay_forge_over_the_real_adapter(self, name, monkeypatch, capsys):
        url = _case_url(name)
        session = _NoNetworkSession()
        built: list[object] = []
        calls: list[dict] = []

        def build(ref):
            forge = make_forge(ref, session=session)
            built.append(forge)
            return forge

        def fake_orchestrate_review(**kwargs):
            calls.append(kwargs)
            return {"verdict": "Approved", "findings_active": [], "findings_dropped": [],
                    "replay": dict(kwargs["replay"])}

        monkeypatch.setattr("prxref.cli.make_forge", build)
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: FakeLLM(RAW_OK))
        monkeypatch.setattr(orchestrator, "orchestrate_review", fake_orchestrate_review)
        assert main([
            "review", "--pr-url", url, "--base-sha", BASE, "--head-sha", HEAD,
            "--no-threads", "--no-description", "--format", "json",
        ]) == 0
        assert "configuration error" not in capsys.readouterr().err
        [adapter] = built
        assert type(adapter) is _forge_impl(name)
        [call] = calls
        assert isinstance(call["forge"], ReplayForge)
        assert call["replay"] == PINNED_STAMP
        assert call["post"] is False
        assert session.sent == []


class _CostLLM(FakeLLM):
    """``FakeLLM`` whose every answer reports ``CALL_COST`` dollars, as an openai-compat ``usage.cost`` does."""

    def invoke(self, system, user, **kwargs):
        result = super().invoke(system, user, **kwargs)
        return dataclasses.replace(result, cost_usd=CALL_COST, cost_source="usage.cost")


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """The real CLI, orchestrator and reviewer; only the forge and the LLM are doubles.

    Every ``orchestrate_review`` call's keyword arguments are kept in
    ``calls`` and its return (the run record) in ``records``.
    ``PRXREF_CHUNK_MAX_FILES=1`` makes ``REVIEW_DIFF`` three
    chunks, so a run is several LLM calls, and the trace goes to ``trace``.
    """
    rig = types.SimpleNamespace(
        records=[], calls=[], made=[], forge=FakeForge(diff=REVIEW_DIFF), llm=_CostLLM(RAW_OK),
        trace=tmp_path / "run.jsonl",
    )
    real = orchestrator.orchestrate_review

    def spy(**kwargs):
        rig.calls.append(kwargs)
        record = real(**kwargs)
        rig.records.append(record)
        return record

    def spy_make_forge(ref):
        rig.made.append(ref)
        return rig.forge

    monkeypatch.setattr(orchestrator, "orchestrate_review", spy)
    monkeypatch.setattr("prxref.cli.make_forge", spy_make_forge)
    monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: rig.llm)
    monkeypatch.setenv("PRXREF_CHUNK_MAX_FILES", "1")
    monkeypatch.setenv("PRXREF_TRACE_FILE", str(rig.trace))
    return rig


@pytest.fixture
def diff_file(tmp_path) -> str:
    path = tmp_path / "case.patch"
    path.write_text(REVIEW_DIFF, encoding="utf-8")
    return str(path)


@pytest.fixture
def stamped(monkeypatch) -> list[list[dict]]:
    """The review units each ``_stamp_run_cost`` call received, copied; the real function still runs."""
    seen: list[list[dict]] = []

    def spy(run_inputs, units, price_table):
        seen.append([dict(unit) for unit in units])
        return _REAL_STAMP_RUN_COST(run_inputs, units, price_table)

    monkeypatch.setattr(orchestrator, "_stamp_run_cost", spy)
    return seen


def _review(rig, capsys, args: list[str]) -> tuple[dict, dict, list[dict]]:
    """One ``review --format json`` through ``main``: ``(payload, run record, trace events)``."""
    rig.trace.unlink(missing_ok=True)
    before = len(rig.records)
    assert main(["review", *args, "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    [record] = rig.records[before:]
    events = [json.loads(line) for line in rig.trace.read_text(encoding="utf-8").splitlines() if line.strip()]
    return payload, record, events


def _run_event(events: list[dict], phase: str) -> dict:
    [event] = [e for e in events if e["node"] == "run" and e["phase"] == phase]
    return event


def _summed(cost: float, calls: int) -> float:
    return round(math.fsum([cost] * calls), 10)


class TestReplayCarriesTheCost:
    """#65 x #67: a replay's payload is a normal payload plus the stamp, cost included."""

    def test_diff_file_replay_json_carries_the_stamp_and_the_summed_cost(self, rig, capsys, diff_file):
        payload, record, events = _review(rig, capsys, ["--diff-file", diff_file])
        stamp = {
            "base_sha": None, "head_sha": None, "threads": "hidden", "diff_file": diff_file,
            "description": "file", "as_of": None, "as_of_source": None,
        }
        assert rig.made == []
        assert payload["replay"] == stamp
        assert record["replay"] == stamp
        assert _run_event(events, "start")["meta"]["replay"] == stamp
        assert payload["verdict"] == "Approved"
        assert payload["chunks_failed"] == 0
        assert rig.llm.calls == CHUNKS + 1
        assert payload["cost_usd"] == _summed(CALL_COST, rig.llm.calls) == 0.5
        assert payload["cost_estimated"] is False
        assert (record["cost_usd"], record["cost_estimated"]) == (payload["cost_usd"], False)
        assert _run_event(events, "ok")["meta"]["cost_usd"] == payload["cost_usd"]

    def test_normal_run_has_no_replay_key_at_any_layer(self, rig, capsys, diff_file):
        normal, record, events = _review(rig, capsys, ["--pr-url", URL, "--no-post"])
        assert rig.made != []
        assert "replay" not in record
        assert "replay" not in normal
        assert "replay" not in _run_event(events, "start").get("meta", {})
        assert rig.llm.calls == CHUNKS + 1
        assert normal["cost_usd"] == record["cost_usd"] == _summed(CALL_COST, rig.llm.calls)
        assert normal["cost_estimated"] is False

        replayed, _, _ = _review(rig, capsys, ["--diff-file", diff_file])
        assert set(replayed) == set(normal) | {"replay"}
        assert replayed["cost_usd"] == normal["cost_usd"]


class TestSubscriptionCliCost:
    """#66 x #67: the real CLI clients' results priced by the orchestrator's run-cost path."""

    @pytest.mark.parametrize("table", [None, CLAUDE_TABLE], ids=["no-table", "table-prices-the-model"])
    def test_claude_cli_total_cost_usd_makes_the_run_cost_reported(
        self, rig, capsys, diff_file, stamped, monkeypatch, table,
    ):
        if table is not None:
            monkeypatch.setenv("PRXREF_PRICE_TABLE", json.dumps(table))
        runner = FakeRunner(Script(stdout=CLAUDE_STREAM))
        rig.llm = ClaudeCLIClient(binary=BINARY, models=["sonnet"], runner=runner, default_timeout=30.0)
        payload, record, _ = _review(rig, capsys, ["--diff-file", diff_file])
        assert set(rig.calls[-1]["price_table"]) == set(table or {})
        assert payload["verdict"] == "Approved"
        assert payload["chunks_failed"] == 0
        [units] = stamped
        assert len(units) == len(runner.launches) == CHUNKS + 1
        assert {(u["model"], u["cost_usd"], u["cost_source"]) for u in units} == {
            (CLAUDE_MODEL, CLAUDE_CALL_COST, "claude-cli"),
        }
        assert payload["cost_usd"] == record["cost_usd"] == _summed(CLAUDE_CALL_COST, len(units))
        assert payload["cost_estimated"] is False

    @pytest.mark.parametrize("table", [None, KIRO_TABLE], ids=["no-table", "table-prices-the-model"])
    def test_kiro_cli_credits_leave_the_run_cost_unknown(
        self, rig, capsys, diff_file, stamped, monkeypatch, caplog, table,
    ):
        if table is not None:
            monkeypatch.setenv("PRXREF_PRICE_TABLE", json.dumps(table))
        caplog.set_level(logging.INFO, logger="prxref")
        runner = FakeRunner(Script(stdout=KIRO_STREAM))
        rig.llm = KiroCLIClient(binary=KIRO_BINARY, models=[KIRO_MODEL], runner=runner, default_timeout=30.0)
        payload, record, events = _review(rig, capsys, ["--diff-file", diff_file])
        assert set(rig.calls[-1]["price_table"]) == set(table or {})
        assert payload["verdict"] == "Approved"
        assert payload["chunks_failed"] == 0
        [units] = stamped
        assert len(units) == len(runner.launches) == CHUNKS + 1
        assert {(u["model"], u["input_tokens"], u["cost_usd"], u["cost_source"]) for u in units} == {
            (KIRO_MODEL, 0, None, ""),
        }
        assert payload["cost_usd"] is None
        assert record["cost_usd"] is None
        assert payload["cost_estimated"] is False
        assert _run_event(events, "ok")["meta"]["cost_usd"] is None
        assert f"estimate for model(s) {KIRO_MODEL!r}" in caplog.text

    def test_one_kiro_unit_makes_a_claude_run_unknown_never_a_partial_sum(
        self, rig, capsys, diff_file, stamped,
    ):
        rig.llm = ClaudeCLIClient(
            binary=BINARY, models=["sonnet"], runner=FakeRunner(Script(stdout=CLAUDE_STREAM)),
            default_timeout=30.0,
        )
        _review(rig, capsys, ["--diff-file", diff_file])
        rig.llm = KiroCLIClient(
            binary=KIRO_BINARY, models=[KIRO_MODEL], runner=FakeRunner(Script(stdout=KIRO_STREAM)),
            default_timeout=30.0,
        )
        _review(rig, capsys, ["--diff-file", diff_file])
        claude_units, kiro_units = stamped
        claude_only: dict = {}
        _REAL_STAMP_RUN_COST(claude_only, claude_units, {})
        assert claude_only["cost_usd"] == _summed(CLAUDE_CALL_COST, len(claude_units))
        mixed: dict = {}
        _REAL_STAMP_RUN_COST(mixed, [*claude_units, kiro_units[0]], {})
        assert (mixed["cost_usd"], mixed["cost_estimated"]) == (None, False)
