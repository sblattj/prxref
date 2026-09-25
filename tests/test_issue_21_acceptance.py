"""Issue #21 acceptance: a worker reply that cannot be used is asked for again.

Every run here takes the production path. ``cli._run_review`` reviews
``tests/fixtures/issue21/pr.diff`` through the local-diff forge (the
``--diff-file`` path), the real openai-compat client, reviewer and
orchestrator, against a local ``MockOpenAIServer``; ``cli.main`` stands in
where the exit code is the point. With ``PRXREF_CHUNK_MAX_FILES=1`` the
patch is two chunks, ``chunk0`` for ``billing/ledger.py`` and ``chunk1`` for
``billing/export.py``, plus the systemic sweep.

The server answers each of those three review units from its own script of
canned replies (``replies.json``) and counts that unit's requests, so every
call count below is per unit and does not depend on worker order. A request
past the end of a unit's script gets an HTTP 500 and still counts, so an
extra retry shows up as a wrong count, never as a silently accepted reply.
Every reply reports 200 prompt tokens, 80 completion tokens and a cost of
$0.001.

Configuration arrives through the environment, the way a user sets it:
``PRXREF_LLM_PARSE_RETRIES`` unset (the default, 1), ``0`` or ``2``. The
judge's retry is tested in tests/test_judge_parse_retry.py.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from prxref import cli
from tests.test_integration import MockOpenAIServer, _completion

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issue21"
DIFF = FIXTURE / "pr.diff"
REPLIES = {
    name: value if isinstance(value, str) else json.dumps(value)
    for name, value in json.loads((FIXTURE / "replies.json").read_text(encoding="utf-8")).items()
    if not name.startswith("_")
}

LEDGER = "billing/ledger.py"
EXPORT = "billing/export.py"
LABELS = {"ledger": "chunk0", "export": "chunk1", "sweep": "sweep"}
UNSET = None

IN_TOKENS = 200
OUT_TOKENS = 80
COST = 0.001

BASE_META_KEYS = [
    "unit", "model", "input_tokens", "output_tokens",
    "elapsed_ms", "error", "cost_usd", "cost_source",
]
RETRY_META_KEYS = [*BASE_META_KEYS, "parse_retries", "first_error"]
BASE_UNIT_FILES = {".system.md", ".user.md", ".response.json", ".meta.json"}

PROSE_ERROR = "JSONDecodeError: Expecting value: line 1 column 1 (char 0)"
LIST_ERROR = "worker review JSON is not an object: list"
NO_FINDINGS_ERROR = "worker review JSON has no findings list"
BUDGET_ERROR = (
    "response truncated at max_tokens=4096 (finish_reason=length); "
    "raise PRXREF_LLM_MAX_TOKENS"
)
LEDGER_FINDING = (LEDGER, "Amount is added without validation")
EXPORT_FINDING = (EXPORT, "Export file is written without an encoding")


def _unit_of(payload: dict) -> str:
    """Which review unit a chat request is for: ``ledger``, ``export``, ``sweep`` or ``unknown``."""
    system = payload["messages"][0]["content"]
    user = payload["messages"][-1]["content"]
    if "systemic sweep" in system:
        return "sweep"
    if f"+++ b/{LEDGER}" in user:
        return "ledger"
    if f"+++ b/{EXPORT}" in user:
        return "export"
    return "unknown"


class UnitScript:
    """An OpenAI route that answers each review unit from its own reply queue.

    ``ledger``, ``export`` and ``sweep`` list reply names from replies.json,
    each optionally paired with a finish reason (``stop`` when bare).
    ``calls`` counts every unit's requests and ``payloads`` keeps them in
    order. A request past the end of its unit's queue gets an HTTP 500.
    """

    def __init__(self, *, ledger=("clean",), export=("clean",), sweep=("clean",)):
        self.queues = {"ledger": list(ledger), "export": list(export), "sweep": list(sweep)}
        self.calls = dict.fromkeys(self.queues, 0)
        self.payloads: dict[str, list[dict]] = {unit: [] for unit in self.queues}

    def __call__(self, payload: dict) -> tuple[int, dict]:
        unit = _unit_of(payload)
        self.calls[unit] = self.calls.get(unit, 0) + 1
        self.payloads.setdefault(unit, []).append(payload)
        queue = self.queues.get(unit, [])
        if not queue:
            return 500, {"error": f"no reply scripted for {unit} call {self.calls[unit]}"}
        reply = queue.pop(0)
        name, reason = reply if isinstance(reply, tuple) else (reply, "stop")
        body = _completion(REPLIES[name], reason)
        body["usage"]["cost"] = COST
        return 200, body


@dataclass
class Run:
    """One review's result dict, its script and its trace directory."""

    result: dict
    script: UnitScript
    trace: Path

    @property
    def calls(self) -> dict[str, int]:
        return self.script.calls

    @property
    def payload(self) -> dict:
        """The ``--format json`` payload the CLI prints for this result."""
        return cli._build_json_result(self.result)

    def meta(self, unit: str) -> dict:
        return json.loads((self.trace / f"{LABELS[unit]}.meta.json").read_text(encoding="utf-8"))

    def response(self, unit: str, attempt: int | None = None) -> str | None:
        suffix = ".response.json" if attempt is None else f".attempt{attempt}.response.json"
        return json.loads((self.trace / f"{LABELS[unit]}{suffix}").read_text(encoding="utf-8"))

    def attempt_files(self) -> list[str]:
        return sorted(p.name for p in self.trace.iterdir() if ".attempt" in p.name)

    def findings(self) -> list[tuple[str, str]]:
        return sorted((f.file, f.title) for f in self.result["findings_active"])


@pytest.fixture(scope="module")
def llm_server():
    """One local OpenAI server for the module; each run installs its script as the route.

    Shared because stopping a server waits out its poll interval, about half
    a second per test.
    """
    route: dict[str, UnitScript] = {}
    server = MockOpenAIServer(routes={"fast": lambda payload: route["script"](payload)})
    base_url = server.start()
    yield base_url, route
    server.stop()


@pytest.fixture
def configure(monkeypatch, llm_server):
    """Point the environment at the shared server and install ``script``; returns nothing."""
    base_url, route = llm_server

    def apply(script: UnitScript, retries: str | None, env: dict[str, str] | None) -> None:
        route["script"] = script
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "openai-compat")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", base_url)
        monkeypatch.setenv("PRXREF_LLM_MODELS", "fast")
        monkeypatch.setenv("PRXREF_CHUNK_MAX_FILES", "1")
        monkeypatch.setenv("PRXREF_MAX_WORKERS", "1")
        if retries is not None:
            monkeypatch.setenv("PRXREF_LLM_PARSE_RETRIES", retries)
        for name, value in (env or {}).items():
            monkeypatch.setenv(name, value)

    return apply


@pytest.fixture
def review(configure, tmp_path):
    """``review(script, retries=...)``: ``cli._run_review`` on the fixture patch, traced.

    ``retries=UNSET`` leaves ``PRXREF_LLM_PARSE_RETRIES`` out of the
    environment; a string sets it.
    """
    def run(script: UnitScript, *, retries: str | None, env=None) -> Run:
        configure(script, retries, env)
        trace = tmp_path / "trace"
        result = cli._run_review(None, post=False, diff_file=str(DIFF), trace_dir=str(trace))
        return Run(result, script, trace)

    return run


@pytest.fixture
def review_cli(configure, tmp_path, capsys):
    """``review_cli(script, retries=...)``: ``prxref review --diff-file ... --format json``; ``(rc, payload)``."""
    def run(script: UnitScript, *, retries: str | None, env=None) -> tuple[int, dict]:
        configure(script, retries, env)
        rc = cli.main([
            "review", "--diff-file", str(DIFF), "--format", "json",
            "--trace-dir", str(tmp_path / "trace"),
        ])
        return rc, json.loads(capsys.readouterr().out)

    return run


class TestTheFixture:
    def test_the_patch_is_two_chunks_and_a_sweep_with_stable_labels(self, review):
        run = review(UnitScript(), retries=UNSET)
        assert run.calls == {"ledger": 1, "export": 1, "sweep": 1}
        assert run.result["chunk_count"] == 3
        assert run.result["chunks_failed"] == 0
        assert f"+++ b/{LEDGER}" in (run.trace / "chunk0.user.md").read_text(encoding="utf-8")
        assert f"+++ b/{EXPORT}" in (run.trace / "chunk1.user.md").read_text(encoding="utf-8")

    def test_a_clean_run_at_the_default_records_zero_and_writes_no_attempt_file(self, review):
        run = review(UnitScript(), retries=UNSET)
        assert run.result["parse_retries"] == 0
        assert run.payload["parse_retries"] == 0
        assert run.attempt_files() == []
        for unit in LABELS:
            assert list(run.meta(unit)) == BASE_META_KEYS

    @pytest.mark.parametrize(("name", "error"), [
        ("stray_quote", LIST_ERROR),
        ("prose", PROSE_ERROR),
        ("list", LIST_ERROR),
        ("no_findings", NO_FINDINGS_ERROR),
        ("escalations_only", NO_FINDINGS_ERROR),
        ("truncated", BUDGET_ERROR),
    ])
    def test_each_bad_reply_fails_the_chunk_with_its_own_error(self, review, name, error):
        """Pins what each canned reply means, so the retry tests below are about the retry.

        ``stray_quote`` is the issue's own example: one stray quote inside
        the object, which the lenient parser turns into its inner list.
        """
        reason = "length" if name == "truncated" else "stop"
        run = review(UnitScript(ledger=[(name, reason), (name, reason)]), retries="1")
        assert run.meta("ledger")["error"] == error
        assert run.result["chunks_failed"] == 1


class TestMalformedThenValid:
    def test_two_calls_keep_the_findings_and_the_record_counts_one(self, review):
        run = review(UnitScript(ledger=["stray_quote", "ledger_finding"]), retries=UNSET)
        assert run.calls == {"ledger": 2, "export": 1, "sweep": 1}
        assert run.result["chunks_failed"] == 0
        assert run.findings() == [LEDGER_FINDING]
        assert run.result["verdict"] == "Request-Changes"
        assert run.result["parse_retries"] == 1
        assert run.payload["parse_retries"] == 1

    def test_the_retry_resends_the_same_request(self, review):
        script = UnitScript(ledger=["prose", "ledger_finding"])
        review(script, retries=UNSET)
        first, second = script.payloads["ledger"]
        assert second == first

    def test_the_trace_keeps_both_replies_and_meta_names_the_first_error(self, review):
        run = review(UnitScript(ledger=["stray_quote", "ledger_finding"]), retries=UNSET)
        meta = run.meta("ledger")
        assert list(meta) == RETRY_META_KEYS
        assert meta["parse_retries"] == 1
        assert meta["first_error"] == LIST_ERROR
        assert meta["error"] == ""
        assert run.response("ledger", attempt=1) == REPLIES["stray_quote"]
        assert run.response("ledger") == REPLIES["ledger_finding"]
        assert run.attempt_files() == ["chunk0.attempt1.response.json"]

    def test_control_a_unit_that_parsed_first_time_keeps_the_old_meta(self, review):
        """Inside the same run, the export chunk and the sweep never retried."""
        run = review(UnitScript(ledger=["prose", "ledger_finding"]), retries=UNSET)
        assert list(run.meta("export")) == BASE_META_KEYS
        assert list(run.meta("sweep")) == BASE_META_KEYS
        assert run.meta("ledger")["first_error"] == PROSE_ERROR

    def test_the_retry_is_logged_once_and_nothing_is_reported_failed(self, review, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref"):
            review(UnitScript(ledger=["prose", "ledger_finding"]), retries=UNSET)
        messages = [r.getMessage() for r in caplog.records]
        assert messages.count(
            f"chunk of 1 files: unusable model reply ({PROSE_ERROR}); parse retry 1 of 1"
        ) == 1
        assert not [m for m in messages if m.startswith("worker review failed")]


class TestWhenTheRetryAlsoFails:
    @pytest.mark.parametrize(("replies", "first_error", "last_error"), [
        (["prose", "list"], PROSE_ERROR, LIST_ERROR),
        (["list", "prose"], LIST_ERROR, PROSE_ERROR),
    ], ids=["then-a-list", "then-prose"])
    def test_malformed_twice_makes_two_calls_and_fails_with_the_last_error(
        self, review, replies, first_error, last_error,
    ):
        run = review(UnitScript(ledger=replies, export=["export_finding"]), retries=UNSET)
        assert run.calls == {"ledger": 2, "export": 1, "sweep": 1}
        assert run.result["chunks_failed"] == 1
        meta = run.meta("ledger")
        assert meta["error"] == last_error
        assert meta["first_error"] == first_error
        assert meta["parse_retries"] == 1
        assert run.result["parse_retries"] == 1
        assert run.response("ledger", attempt=1) == REPLIES[replies[0]]
        assert run.response("ledger") == REPLIES[replies[1]]

    def test_the_other_chunk_still_reports_its_findings(self, review):
        run = review(UnitScript(ledger=["prose", "prose"], export=["export_finding"]), retries=UNSET)
        assert run.findings() == [EXPORT_FINDING]
        assert run.result["verdict"] == "Request-Changes"

    def test_an_empty_object_twice_fails_with_no_findings_list(self, review):
        run = review(UnitScript(ledger=["no_findings", "no_findings"]), retries=UNSET)
        assert run.calls == {"ledger": 2, "export": 1, "sweep": 1}
        assert run.result["chunks_failed"] == 1
        assert run.meta("ledger")["error"] == NO_FINDINGS_ERROR
        assert run.meta("ledger")["first_error"] == NO_FINDINGS_ERROR
        assert run.result["parse_retries"] == 1

    def test_review_still_exits_zero_when_one_chunk_fails(self, review_cli, tmp_path):
        script = UnitScript(ledger=["prose", "prose"])
        rc, payload = review_cli(script, retries=UNSET)
        assert rc == 0
        assert script.calls == {"ledger": 2, "export": 1, "sweep": 1}
        assert payload["chunks_failed"] == 1
        assert payload["parse_retries"] == 1
        attempt = tmp_path / "trace" / "chunk0.attempt1.response.json"
        assert json.loads(attempt.read_text(encoding="utf-8")) == REPLIES["prose"]

    def test_review_still_exits_zero_when_every_unit_fails(self, review_cli):
        bad = ["prose", "prose"]
        script = UnitScript(ledger=bad, export=bad, sweep=bad)
        rc, payload = review_cli(script, retries=UNSET)
        assert rc == 0
        assert script.calls == {"ledger": 2, "export": 2, "sweep": 2}
        assert payload["verdict"] == "Error"
        assert payload["parse_retries"] == 3

    def test_control_the_same_failure_exits_one_when_the_gate_is_opted_into(self, review_cli):
        """PRXREF_FAIL_ON=error is the one opt-in gate; it proves the zero above is the doctrine."""
        bad = ["prose", "prose"]
        rc, payload = review_cli(
            UnitScript(ledger=bad, export=bad, sweep=bad),
            retries=UNSET, env={"PRXREF_FAIL_ON": "error"},
        )
        assert rc == 1
        assert payload["verdict"] == "Error"


class TestTheWrongShapeRetriesTheSameWay:
    @pytest.mark.parametrize(("name", "error"), [
        ("list", LIST_ERROR),
        ("no_findings", NO_FINDINGS_ERROR),
        ("escalations_only", NO_FINDINGS_ERROR),
    ])
    def test_valid_json_of_the_wrong_shape_is_asked_for_again(self, review, name, error):
        run = review(UnitScript(ledger=[name, "ledger_finding"]), retries=UNSET)
        assert run.calls == {"ledger": 2, "export": 1, "sweep": 1}
        assert run.findings() == [LEDGER_FINDING]
        assert run.meta("ledger")["first_error"] == error
        assert run.response("ledger", attempt=1) == REPLIES[name]


class TestTruncationIsNotRetried:
    def test_a_length_stop_makes_one_call_and_names_the_budget(self, review):
        run = review(UnitScript(ledger=[("truncated", "length")]), retries=UNSET)
        assert run.calls == {"ledger": 1, "export": 1, "sweep": 1}
        assert run.result["chunks_failed"] == 1
        meta = run.meta("ledger")
        assert meta["error"] == BUDGET_ERROR
        assert list(meta) == BASE_META_KEYS
        assert run.attempt_files() == []
        assert run.result["parse_retries"] == 0

    def test_control_the_same_bytes_with_an_honest_stop_are_retried(self, review):
        run = review(UnitScript(ledger=[("truncated", "stop"), "ledger_finding"]), retries=UNSET)
        assert run.calls == {"ledger": 2, "export": 1, "sweep": 1}
        assert run.meta("ledger")["first_error"].startswith("JSONDecodeError: Unterminated string")
        assert run.findings() == [LEDGER_FINDING]


class TestZeroIsTheOldBehaviour:
    """``PRXREF_LLM_PARSE_RETRIES=0``: 0.16.0's behaviour, with the record key null."""

    def test_an_empty_object_is_a_clean_review(self, review):
        script = UnitScript(ledger=["no_findings"], export=["no_findings"], sweep=["no_findings"])
        run = review(script, retries="0")
        assert run.calls == {"ledger": 1, "export": 1, "sweep": 1}
        assert run.result["chunks_failed"] == 0
        assert run.result["verdict"] == "Approved"
        assert run.meta("ledger")["error"] == ""

    def test_garbage_costs_one_call_and_fails_the_chunk(self, review):
        run = review(UnitScript(ledger=["prose"]), retries="0")
        assert run.calls == {"ledger": 1, "export": 1, "sweep": 1}
        assert run.result["chunks_failed"] == 1
        assert run.meta("ledger")["error"] == PROSE_ERROR
        assert run.result["input_tokens"] == 3 * IN_TOKENS

    def test_the_record_is_null_and_no_attempt_file_or_retry_key_is_written(self, review):
        run = review(
            UnitScript(ledger=["prose"], export=["list"], sweep=["no_findings"]), retries="0",
        )
        assert run.result["parse_retries"] is None
        assert run.payload["parse_retries"] is None
        assert run.attempt_files() == []
        names = {p.name for p in run.trace.iterdir()}
        assert names == {label + suffix for label in LABELS.values() for suffix in BASE_UNIT_FILES}
        for unit in LABELS:
            assert list(run.meta(unit)) == BASE_META_KEYS

    def test_an_empty_reply_still_gets_its_one_retry(self, review):
        """The empty-reply retry predates the parse retry and stays at 0, with no new trace."""
        run = review(UnitScript(ledger=["empty", "ledger_finding"]), retries="0")
        assert run.calls == {"ledger": 2, "export": 1, "sweep": 1}
        assert run.findings() == [LEDGER_FINDING]
        assert run.result["parse_retries"] is None
        assert run.attempt_files() == []
        assert list(run.meta("ledger")) == BASE_META_KEYS

    def test_control_the_default_asks_again_for_the_same_replies(self, review):
        """The same first replies with the variable unset: the retry is what zero turned off."""
        script = UnitScript(ledger=["prose", "ledger_finding"], export=["no_findings", "clean"])
        run = review(script, retries=UNSET)
        assert run.calls == {"ledger": 2, "export": 2, "sweep": 1}
        assert run.result["parse_retries"] == 2


class TestTwoRetries:
    def test_repeated_garbage_makes_three_calls(self, review):
        run = review(UnitScript(ledger=["prose", "list", "prose"]), retries="2")
        assert run.calls == {"ledger": 3, "export": 1, "sweep": 1}
        assert run.result["chunks_failed"] == 1
        meta = run.meta("ledger")
        assert meta["error"] == PROSE_ERROR
        assert meta["first_error"] == PROSE_ERROR
        assert meta["parse_retries"] == 2
        assert run.result["parse_retries"] == 2
        assert run.attempt_files() == [
            "chunk0.attempt1.response.json", "chunk0.attempt2.response.json",
        ]
        assert run.response("ledger", attempt=2) == REPLIES["list"]

    def test_the_third_call_can_still_succeed(self, review):
        run = review(UnitScript(ledger=["prose", "no_findings", "ledger_finding"]), retries="2")
        assert run.calls == {"ledger": 3, "export": 1, "sweep": 1}
        assert run.findings() == [LEDGER_FINDING]
        assert run.result["parse_retries"] == 2


class TestTheSweep:
    def test_the_sweep_retries_the_same_way(self, review):
        run = review(UnitScript(sweep=["stray_quote", "clean"]), retries=UNSET)
        assert run.calls == {"ledger": 1, "export": 1, "sweep": 2}
        assert run.result["chunks_failed"] == 0
        meta = run.meta("sweep")
        assert list(meta) == RETRY_META_KEYS
        assert meta["first_error"] == LIST_ERROR
        assert run.response("sweep", attempt=1) == REPLIES["stray_quote"]
        assert run.attempt_files() == ["sweep.attempt1.response.json"]
        assert run.result["parse_retries"] == 1


class TestEveryCallIsCounted:
    def test_tokens_and_cost_cover_every_attempt(self, review):
        script = UnitScript(
            ledger=["prose", "ledger_finding"],
            export=["list", "export_finding"],
            sweep=["no_findings", "clean"],
        )
        run = review(script, retries=UNSET)
        calls = sum(run.calls.values())
        assert calls == 6
        assert run.result["input_tokens"] == calls * IN_TOKENS
        assert run.result["output_tokens"] == calls * OUT_TOKENS
        assert run.result["cost_usd"] == pytest.approx(calls * COST)
        assert run.result["cost_estimated"] is False
        assert run.result["parse_retries"] == 3
        for unit in LABELS:
            assert run.meta(unit)["input_tokens"] == 2 * IN_TOKENS
            assert run.meta(unit)["cost_usd"] == pytest.approx(2 * COST)

    def test_control_a_clean_run_bills_one_call_per_unit(self, review):
        run = review(UnitScript(), retries=UNSET)
        assert run.result["input_tokens"] == 3 * IN_TOKENS
        assert run.result["output_tokens"] == 3 * OUT_TOKENS
        assert run.result["cost_usd"] == pytest.approx(3 * COST)
