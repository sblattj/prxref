"""Tier 1 of error verification (#126): :mod:`prxref.verify`.

A fake LLM answers the verification prompt. A ``refuted`` reply downgrades
the finding to ``warning`` only when its quoted line occurs in a text the
call was shown; every other reply, a garbage reply and a raising call leave
the finding an ``error``. No test touches the network.
"""

from __future__ import annotations

import json
import threading

import pytest

from prxref import verify
from prxref.llm import InvokeResult
from prxref.triage import Finding

HANDLER = "svc/watchdog.py"
GATE = "svc/gate.py"
HANDLER_TEXT = (
    "import time\n"  # 1
    "\n"  # 2
    "from svc.gate import tick\n"  # 3
    "\n"  # 4
    "\n"  # 5
    "def watchdog_handler(event):\n"  # 6
    "    now = time.time()\n"  # 7
    "    return tick(now, event)\n"  # 8
)
GATE_TEXT = (
    "import time\n"  # 1
    "\n"  # 2
    "\n"  # 3
    "def tick(stamp, event):\n"  # 4
    "    elapsed = time.time() - stamp\n"  # 5
    "    return elapsed > event.limit\n"  # 6
)
FILES = {HANDLER: HANDLER_TEXT, GATE: GATE_TEXT}
CLAIM = Finding(
    file=HANDLER,
    line=8,
    severity="error",
    confidence=0.9,
    title="watchdog_handler passes a monotonic clock to tick",
    body="`watchdog_handler` passes `time.monotonic()` to `tick`, which compares it with wall-clock time.",
)


def _read(path):
    return FILES.get(path)


class FakeLLM:
    """Answers every call with ``reply`` (a dict, a raw string, or an exception to raise)."""

    def __init__(self, reply):
        self.reply = reply
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=None):
        with self._lock:
            self.calls.append(
                {
                    "system": system,
                    "user": user,
                    "max_tokens": max_tokens,
                    "json_mode": json_mode,
                    "timeout_s": timeout_s,
                }
            )
        if isinstance(self.reply, BaseException):
            raise self.reply
        text = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return InvokeResult(
            text=text,
            input_tokens=100,
            output_tokens=7,
            model="fake-model",
            backend="fake",
            elapsed_ms=1,
        )


def _run(llm, findings=(CLAIM,), **kw):
    kw.setdefault("mode", "llm")
    kw.setdefault("confidence_floor", 0.5)
    return verify.verify_errors(llm, list(findings), _read, **kw)


class TestCase2:
    def test_a_served_refutation_downgrades_to_warning(self):
        llm = FakeLLM({"verdict": "refuted", "evidence": f"{HANDLER}:7 now = time.time()"})
        out, record, units = _run(llm)
        assert out[0].severity == "warning"
        assert out[0].drop_reason is None
        assert out[0].body.endswith(f"[downgraded: verification refuted: {HANDLER}:7]")
        assert record["refuted"] == 1 and record["called"] == 1
        assert record["detail"][0]["outcome"] == "refuted"
        assert len(units) == 1 and units[0]["input_tokens"] == 100

    def test_a_refutation_quoting_the_served_callee_downgrades(self):
        llm = FakeLLM({"verdict": "refuted", "evidence": f"{GATE}:5 elapsed = time.time() - stamp"})
        out, record, _ = _run(llm)
        assert "def tick(stamp, event):" in llm.calls[0]["user"]
        assert out[0].severity == "warning"
        assert out[0].body.endswith(f"[downgraded: verification refuted: {GATE}:5]")

    def test_the_line_comes_from_the_code_not_the_reply(self):
        llm = FakeLLM({"verdict": "refuted", "evidence": f"{HANDLER}:99 now = time.time()"})
        out, _, _ = _run(llm)
        assert out[0].body.endswith(f"{HANDLER}:7]")

    def test_an_unserved_quote_leaves_the_error(self):
        llm = FakeLLM({"verdict": "refuted", "evidence": f"{HANDLER}:7 now = time.monotonic()"})
        out, record, _ = _run(llm)
        assert out[0] == CLAIM
        assert record["unproven"] == 1 and record["refuted"] == 0

    def test_a_refutation_with_no_evidence_leaves_the_error(self):
        out, record, _ = _run(FakeLLM({"verdict": "refuted", "evidence": ""}))
        assert out[0] == CLAIM

    def test_the_prompt_shows_the_head_file_with_line_numbers(self):
        llm = FakeLLM({"verdict": "unsure", "evidence": ""})
        _run(llm)
        user = llm.calls[0]["user"]
        assert user.startswith(verify.SPLIT_MARKER)
        assert "    7|     now = time.time()" in user
        assert CLAIM.title in user and "## Finding" not in llm.calls[0]["system"]


class TestNonRefutationsLeaveTheError:
    @pytest.mark.parametrize(
        "reply, key",
        [
            ({"verdict": "confirmed", "evidence": ""}, "confirmed"),
            ({"verdict": "unsure", "evidence": ""}, "unsure"),
            ("not json at all", "errors"),
            ({"verdict": "maybe"}, "errors"),
            (TimeoutError("request timeout"), "errors"),
            (RuntimeError("boom"), "errors"),
        ],
    )
    def test_unchanged(self, reply, key):
        out, record, _ = _run(FakeLLM(reply))
        assert out[0] == CLAIM
        assert record[key] == 1

    def test_a_raising_call_records_no_unit(self):
        _, record, units = _run(FakeLLM(RuntimeError("boom")))
        assert units == [] and record["called"] == 0 and record["errors"] == 1

    def test_a_missing_head_file_is_an_error_without_a_call(self):
        llm = FakeLLM({"verdict": "refuted", "evidence": "x"})
        moved = Finding(file="svc/gone.py", line=1, severity="error", confidence=0.9, title="t", body="b")
        out, record, _ = _run(llm, [moved])
        assert llm.calls == [] and out[0] == moved and record["errors"] == 1


class TestBounds:
    def test_every_call_carries_max_tokens_and_a_timeout(self):
        llm = FakeLLM({"verdict": "unsure", "evidence": ""})
        _run(llm)
        call = llm.calls[0]
        assert call["max_tokens"] == verify.MAX_TOKENS
        assert call["timeout_s"] == verify.TIMEOUT_S
        assert call["json_mode"] is True

    def test_max_calls_caps_the_calls_most_confident_first(self):
        llm = FakeLLM({"verdict": "unsure", "evidence": ""})
        fs = [
            Finding(file=HANDLER, line=8, severity="error", confidence=c, title=f"claim {c}", body="b")
            for c in (0.6, 0.95, 0.8)
        ]
        out, record, units = _run(llm, fs, max_calls=2)
        assert len(llm.calls) == 2 and len(units) == 2
        sent = {t for c in llm.calls for t in ("claim 0.95", "claim 0.8", "claim 0.6") if t in c["user"]}
        assert sent == {"claim 0.95", "claim 0.8"}
        assert record["capped"] == 1
        assert [r["outcome"] for r in record["detail"]] == ["capped", "unsure", "unsure"]

    def test_max_calls_zero_makes_no_call(self):
        llm = FakeLLM({"verdict": "refuted", "evidence": f"{HANDLER}:7 now = time.time()"})
        out, record, _ = _run(llm, max_calls=0)
        assert llm.calls == [] and out[0] == CLAIM and record["capped"] == 1


class TestEligibility:
    def test_off_reads_nothing_and_returns_no_record(self):
        def refuse(path):
            raise AssertionError("off must not read")

        llm = FakeLLM({"verdict": "refuted", "evidence": "x"})
        out, record, units = verify.verify_errors(llm, [CLAIM], refuse, mode="off")
        assert out == [CLAIM] and record is None and units == [] and llm.calls == []

    def test_names_mode_makes_no_call(self):
        llm = FakeLLM({"verdict": "refuted", "evidence": f"{HANDLER}:7 now = time.time()"})
        out, record, units = _run(llm, mode="names")
        assert llm.calls == [] and out[0] == CLAIM and units == []
        assert record["eligible"] == 1 and record["detail"][0]["outcome"] == "not-checked"

    @pytest.mark.parametrize(
        "finding",
        [
            Finding(file=HANDLER, line=8, severity="warning", confidence=0.9, title="w", body="b"),
            Finding(file=HANDLER, line=8, severity="error", confidence=0.3, title="low", body="b"),
            Finding(
                file=HANDLER,
                line=8,
                severity="error",
                confidence=1.0,
                title="det",
                body="Something. (deterministic check, no model)",
            ),
            Finding(
                file=HANDLER,
                line=8,
                severity="error",
                confidence=0.9,
                title="gone",
                body="b",
                drop_reason='hedged: "may"',
            ),
        ],
    )
    def test_never_verified(self, finding):
        llm = FakeLLM({"verdict": "refuted", "evidence": f"{HANDLER}:7 now = time.time()"})
        out, record, _ = _run(llm, [finding])
        assert llm.calls == [] and out == [finding] and record["eligible"] == 0

    def test_a_name_refuted_finding_is_not_sent(self):
        llm = FakeLLM({"verdict": "confirmed", "evidence": ""})
        claim = Finding(
            file=HANDLER,
            line=8,
            severity="error",
            confidence=0.9,
            title="`tick` is not imported",
            body="`tick` is not imported: NameError.",
        )
        out, record, _ = _run(llm, [claim])
        assert llm.calls == []
        assert out[0].drop_reason == f'contradicted by head file: {HANDLER}:3 binds "tick"'
        assert record["name_refuted"] == 1

    def test_an_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="verify_errors"):
            verify.verify_errors(FakeLLM({}), [CLAIM], _read, mode="on")


class TestLocateEvidence:
    def test_whitespace_and_gutter_are_tolerated(self):
        served = [verify.Served(HANDLER, 1, HANDLER_TEXT)]
        assert verify.locate_evidence(f"{HANDLER}:7     7|     now  =  time.time()", served) == (HANDLER, 7)

    def test_a_too_short_quote_is_rejected(self):
        served = [verify.Served(HANDLER, 1, HANDLER_TEXT)]
        assert verify.locate_evidence(f"{HANDLER}:7 now", served) is None

    def test_a_window_reports_the_file_line(self):
        served = [verify.Served(GATE, 41, "x = 1\ny = 2\nreturn elapsed")]
        assert verify.locate_evidence(f"{GATE}:1 return elapsed", served) == (GATE, 43)
