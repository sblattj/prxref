"""Error verification in the orchestrator (#126): ``orchestrate_review(verify_errors=...)``.

Runs the REAL reviewer and passes over a one-file diff with a forge that
serves head files, and a scripted LLM that answers the chunk with one
model ``error``, the sweep with nothing, and the verification prompt with
a scripted reply. Covers the issue's two failure cases end to end: an error
the head file contradicts no longer turns the review Request-Changes, and a
real one still does. No test touches the network.
"""

from __future__ import annotations

import inspect
import json
import threading

import pytest

from prxref import orchestrator, verify
from prxref.llm import InvokeResult
from tests.test_orchestrator import REF, FakeForge

SWEEP_MARK = "running a systemic sweep"
VERIFY_MARK = "You check one code-review finding"
NO_FINDINGS = json.dumps({"findings": []})

SETTINGS = "app/settings.py"
SETTINGS_HEAD = (
    '"""Settings."""\n'  # 1
    "from __future__ import annotations\n"  # 2
    "\n"  # 3
    "import os\n"  # 4
    "\n"  # 5
    'BASE = "/srv"\n'  # 6
    'DATA_DIR = os.path.join(BASE, "data")\n'  # 7
)
SETTINGS_HEAD_NO_IMPORT = SETTINGS_HEAD.replace("import os\n", "import sys\n")
SETTINGS_DIFF = (
    f"diff --git a/{SETTINGS} b/{SETTINGS}\n"
    f"--- a/{SETTINGS}\n"
    f"+++ b/{SETTINGS}\n"
    "@@ -4,3 +4,4 @@\n"
    " import os\n"
    " \n"
    ' BASE = "/srv"\n'
    '+DATA_DIR = os.path.join(BASE, "data")\n'
)
CASE1 = {
    "file": SETTINGS,
    "line": 7,
    "severity": "error",
    "confidence": 0.9,
    "title": "`os` may not be imported before `DATA_DIR` uses it",
    "body": '`os` may not be imported in this module. `DATA_DIR = os.path.join(BASE, "data")` '
    "would raise a NameError at import time.",
}

HANDLER = "svc/watchdog.py"
GATE = "svc/gate.py"
HANDLER_HEAD = (
    "import time\n"  # 1
    "\n"  # 2
    "from svc.gate import tick\n"  # 3
    "\n"  # 4
    "\n"  # 5
    "def watchdog_handler(event):\n"  # 6
    "    now = time.time()\n"  # 7
    "    return tick(now, event)\n"  # 8
)
GATE_HEAD = "import time\n\n\ndef tick(stamp, event):\n    return time.time() - stamp > event.limit\n"
HANDLER_DIFF = (
    f"diff --git a/{HANDLER} b/{HANDLER}\n"
    f"--- a/{HANDLER}\n"
    f"+++ b/{HANDLER}\n"
    "@@ -5,3 +5,4 @@\n"
    " \n"
    " def watchdog_handler(event):\n"
    "     now = time.time()\n"
    "+    return tick(now, event)\n"
)
CASE2 = {
    "file": HANDLER,
    "line": 8,
    "severity": "error",
    "confidence": 0.9,
    "title": "watchdog_handler passes a monotonic clock to tick",
    "body": "`watchdog_handler` passes `now`, a `time.monotonic()` reading, to `tick`, which "
    "subtracts it from wall-clock time, so the watchdog never fires.",
}
REFUTED = json.dumps({"verdict": "refuted", "evidence": f"{HANDLER}:7 now = time.time()"})
UNPROVEN = json.dumps({"verdict": "refuted", "evidence": f"{HANDLER}:7 now = time.monotonic()"})


class HeadForge(FakeForge):
    """A :class:`FakeForge` that serves head files from ``files``."""

    def __init__(self, diff: str, files: dict[str, str]):
        super().__init__(diff=diff)
        self.files = files

    def get_file_content(self, ref, path, *, sha):
        return self.files.get(path)


class ScriptLLM:
    """The chunk gets ``findings``, the sweep nothing, a verification call ``verify_reply``."""

    def __init__(self, findings, verify_reply=NO_FINDINGS):
        self.findings = findings
        self.verify_reply = verify_reply
        self.calls: list[tuple[str, str]] = []
        self.verify_kwargs: list[dict] = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        with self._lock:
            self.calls.append((system, user))
            if VERIFY_MARK in system:
                self.verify_kwargs.append({"max_tokens": max_tokens, "timeout_s": timeout_s})
        if VERIFY_MARK in system:
            if isinstance(self.verify_reply, BaseException):
                raise self.verify_reply
            text, tokens = self.verify_reply, (1000, 30)
        elif SWEEP_MARK in system:
            text, tokens = NO_FINDINGS, (10, 5)
        else:
            text, tokens = json.dumps({"findings": self.findings}), (10, 5)
        return InvokeResult(
            text=text,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            model="fake-model",
            backend="fake",
            elapsed_ms=1,
        )

    def verify_calls(self) -> list[str]:
        return [u for s, u in self.calls if VERIFY_MARK in s]


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    monkeypatch.setattr(orchestrator, "_elapsed_ms", lambda t0: 0)


def _review(llm, diff, files, **kwargs):
    kwargs.setdefault("max_workers", 1)
    return orchestrator.orchestrate_review(HeadForge(diff, files), REF, llm, post=False, **kwargs)


def _case1(head=SETTINGS_HEAD, **kwargs):
    llm = ScriptLLM([CASE1])
    return _review(llm, SETTINGS_DIFF, {SETTINGS: head}, **kwargs), llm


def _case2(reply=REFUTED, **kwargs):
    llm = ScriptLLM([CASE2], reply)
    return _review(llm, HANDLER_DIFF, {HANDLER: HANDLER_HEAD, GATE: GATE_HEAD}, **kwargs), llm


def _finding(res, title):
    return next(f for f in [*res["findings_active"], *res["findings_dropped"]] if f.title == title)


class TestTheArgument:
    def test_the_default_is_names(self):
        params = inspect.signature(orchestrator.orchestrate_review).parameters
        assert params["verify_errors"].default == "names"
        assert params["verify_errors_max"].default == 5
        assert verify.VERIFY_MODES == ("off", "names", "llm")

    @pytest.mark.parametrize("value", ["on", "NAMES", "", None])
    def test_an_unknown_value_raises_naming_it(self, value):
        with pytest.raises(ValueError, match="verify_errors"):
            _case1(verify_errors=value)


class TestCase1:
    def test_the_default_drops_the_contradicted_error_and_approves(self):
        res, llm = _case1()
        assert res["verdict"] == "Approved"
        f = _finding(res, CASE1["title"])
        assert f.drop_reason == (f'contradicted by head file: {SETTINGS}:4 binds "os"; {SETTINGS}:7 binds "DATA_DIR"')
        record = res["error_verification"]
        assert record["mode"] == "names"
        assert (record["eligible"], record["name_refuted"], record["called"]) == (1, 1, 0)
        assert llm.verify_calls() == []

    def test_off_keeps_the_old_behavior(self):
        res, llm = _case1(verify_errors="off")
        assert res["verdict"] == "Request-Changes"
        assert _finding(res, CASE1["title"]).drop_reason is None
        assert res["error_verification"] is None
        assert llm.verify_calls() == []

    def test_a_real_name_error_still_blocks(self):
        for mode in ("names", "llm"):
            res, _ = _case1(head=SETTINGS_HEAD_NO_IMPORT, verify_errors=mode)
            assert res["verdict"] == "Request-Changes", mode
            f = _finding(res, CASE1["title"])
            assert f.drop_reason is None and f.severity == "error"


class TestCase2:
    def test_names_makes_no_call_and_still_blocks(self):
        res, llm = _case2()
        assert res["verdict"] == "Request-Changes"
        assert llm.verify_calls() == []

    def test_llm_downgrades_a_served_refutation_and_approves(self):
        res, llm = _case2(verify_errors="llm")
        assert res["verdict"] == "Approved"
        f = _finding(res, CASE2["title"])
        assert f.drop_reason is None and f.severity == "warning"
        assert f.body.endswith(f"[downgraded: verification refuted: {HANDLER}:7]")
        assert "def tick(stamp, event):" in llm.verify_calls()[0]
        record = res["error_verification"]
        assert (record["called"], record["refuted"]) == (1, 1)

    def test_an_unserved_quote_still_blocks(self):
        res, _ = _case2(UNPROVEN, verify_errors="llm")
        assert res["verdict"] == "Request-Changes"
        assert _finding(res, CASE2["title"]).severity == "error"
        assert res["error_verification"]["unproven"] == 1

    def test_a_failing_verifier_still_blocks(self):
        res, _ = _case2(RuntimeError("boom"), verify_errors="llm")
        assert res["verdict"] == "Request-Changes"
        assert res["error_verification"]["errors"] == 1

    def test_the_call_is_bounded(self):
        _, llm = _case2(verify_errors="llm")
        assert llm.verify_kwargs == [{"max_tokens": verify.MAX_TOKENS, "timeout_s": verify.TIMEOUT_S}]

    def test_the_verify_tokens_count_toward_the_run(self):
        off, _ = _case2(verify_errors="names")
        on, _ = _case2(verify_errors="llm")
        assert on["input_tokens"] == off["input_tokens"] + 1000
        assert on["output_tokens"] == off["output_tokens"] + 30
        assert on["error_verification"]["input_tokens"] == 1000

    def test_max_zero_makes_no_call(self):
        res, llm = _case2(verify_errors="llm", verify_errors_max=0)
        assert llm.verify_calls() == []
        assert res["verdict"] == "Request-Changes"
        assert res["error_verification"]["capped"] == 1


class TestNeverVerified:
    def test_a_sub_floor_error_is_not_sent(self):
        low = {**CASE2, "confidence": 0.4}
        llm = ScriptLLM([low], REFUTED)
        res = _review(
            llm,
            HANDLER_DIFF,
            {HANDLER: HANDLER_HEAD, GATE: GATE_HEAD},
            verify_errors="llm",
            confidence_floor=0.5,
        )
        assert llm.verify_calls() == []
        assert res["error_verification"]["eligible"] == 0

    def test_a_warning_is_not_sent(self):
        warn = {**CASE2, "severity": "warning"}
        llm = ScriptLLM([warn], REFUTED)
        res = _review(llm, HANDLER_DIFF, {HANDLER: HANDLER_HEAD, GATE: GATE_HEAD}, verify_errors="llm")
        assert llm.verify_calls() == []
        assert res["error_verification"]["eligible"] == 0
