"""Issue 04 — finding sets vary between identical runs.

Temperature 0 and ``PRXREF_LLM_SEED`` already shipped (0.10.0), so what
remains on the pipeline side is order-sensitivity in the passes that CAP or
pick: a tie in the error cap, and a tie in the inline-comment cap, are broken
by the arrival order of the list rather than by finding content. Tests A/A2
pin the frozen order-invariance contract (identical result for any permutation
of the same multiset, ordered by ``(file, line, title)``); Test B pins the new
``sampling`` observability key. The controls pin what must keep working.
"""
from __future__ import annotations

import itertools
import threading

from prxref.forges.base import PRRef
from prxref.llm import InvokeResult
from prxref.orchestrator import orchestrate_review
from prxref.quality import apply_quality_gate
from prxref.triage import Finding
from tests.test_orchestrator import FakeForge, make_pr

REF = PRRef(
    forge="fake", host="fake.test", owner="acme", repo="widget",
    number=7, url="https://fake.test/acme/widget/pull/7",
)

DIFF = (
    "diff --git a/src/a.py b/src/a.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/a.py\n"
    "@@ -0,0 +1,3 @@\n"
    "+data 1\n"
    "+data 2\n"
    "+data 3\n"
)


def _finding(file: str, line: int, title: str, conf: float = 0.9) -> Finding:
    return Finding(
        file=file, line=line, severity="error", confidence=conf,
        title=title, body=f"data {line} is unsafe in {file}",
    )


def _key(f: Finding) -> tuple[str, int, str]:
    return (f.file, f.line, f.title)


class _StubLLM:
    """No findings; optionally exposes sampling attributes."""

    def __init__(self, **attrs) -> None:
        for k, v in attrs.items():
            setattr(self, k, v)
        self._lock = threading.Lock()
        self.calls = 0

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        with self._lock:
            self.calls += 1
        return InvokeResult(
            text='{"findings":[],"escalations":[]}',
            input_tokens=10, output_tokens=5,
            model="test-model-1", backend="fake", elapsed_ms=1,
        )


# --------------------------------------------------------------------------
# Test A — the error cap must not depend on arrival order (frozen contract)
# --------------------------------------------------------------------------

TIED = [
    _finding("src/a.py", 1, "alpha leak"),
    _finding("src/a.py", 2, "bravo leak"),
    _finding("src/a.py", 3, "charlie leak"),
]


def test_a_error_cap_survivors_are_permutation_invariant():
    """Three tied-confidence errors, cap 2: the same two must survive always."""
    results = []
    for perm in itertools.permutations(TIED):
        staged = apply_quality_gate(list(perm), confidence_floor=0.6, max_errors=2)
        survivors = frozenset(_key(f) for f in staged if f.drop_reason is None)
        dropped = frozenset(
            (_key(f), f.drop_reason) for f in staged if f.drop_reason is not None
        )
        results.append((survivors, dropped))
    assert len({r[0] for r in results}) == 1, (
        "error-cap survivors depend on arrival order: "
        f"{sorted({tuple(sorted(r[0])) for r in results})}"
    )
    assert len({r[1] for r in results}) == 1


def test_a_gate_output_is_sorted_by_file_line_title():
    """The gate's output order must be content-derived, not arrival-derived."""
    for perm in itertools.permutations(TIED):
        staged = apply_quality_gate(list(perm), confidence_floor=0.6, max_errors=3)
        assert [_key(f) for f in staged] == sorted(_key(f) for f in staged)


def test_a2_inline_batch_is_permutation_invariant(monkeypatch):
    """The posted inline batch is the same set for any worker-result order."""
    from prxref import orchestrator

    batches = []
    for perm in itertools.permutations(TIED):
        forge = FakeForge(pr=make_pr(), diff=DIFF)

        def _fake_review_chunk(llm, files, _perm=perm, **kwargs):
            return list(_perm), {
                "escalations": [], "input_tokens": 0, "output_tokens": 0,
                "model": "test-model-1", "elapsed_ms": 1, "error": "",
            }

        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _fake_review_chunk)
        orchestrate_review(
            forge, REF, _StubLLM(), post=True, post_mode="inline",
            max_workers=1, max_errors=3, max_inline_comments=2,
        )
        batch = forge.inline_batches[-1] if forge.inline_batches else []
        assert len(batch) == 2
        batches.append(tuple((c.path, c.line) for c in batch))
    assert len(set(batches)) == 1, f"inline batch varies by arrival order: {set(batches)}"


# --------------------------------------------------------------------------
# Test B — sampling observability (frozen contract)
# --------------------------------------------------------------------------


def test_b_result_reports_sampling_from_client():
    forge = FakeForge(pr=make_pr(), diff=DIFF)
    llm = _StubLLM(temperature=0.0, seed=7, models=["m1", "m2"])
    result = orchestrate_review(forge, REF, llm, post=False)
    assert result["sampling"] == {
        "temperature": 0.0, "seed": 7, "models": ["m1", "m2"],
    }


def test_b_sampling_key_present_when_client_is_silent():
    forge = FakeForge(pr=make_pr(), diff=DIFF)
    result = orchestrate_review(forge, REF, _StubLLM(), post=False)
    assert "sampling" in result
    assert result["sampling"]["temperature"] is None
    assert result["sampling"]["seed"] is None
    assert result["sampling"]["models"] in ([], None)


# --------------------------------------------------------------------------
# Controls — must pass today (plus the error-exit half of Test B)
# --------------------------------------------------------------------------


def test_b_sampling_survives_the_error_exit():
    """``_error_run`` is a SEPARATE exit reached from five call sites; the
    sampling record must ride it too, or a failed run says nothing about what
    sampling was in force."""
    forge = FakeForge(pr=make_pr(), diff=DIFF)
    forge.fail.add("get_pr")
    llm = _StubLLM(temperature=0.0, seed=7, models=["m1", "m2"])
    result = orchestrate_review(forge, REF, llm, post=False)
    assert result["sampling"]["seed"] == 7


def test_control_single_order_active_set(monkeypatch):
    """One ordering still yields exactly the two highest-ranked errors."""
    from prxref import orchestrator

    forge = FakeForge(pr=make_pr(), diff=DIFF)

    def _fake_review_chunk(llm, files, **kwargs):
        return list(TIED), {
            "escalations": [], "input_tokens": 0, "output_tokens": 0,
            "model": "test-model-1", "elapsed_ms": 1, "error": "",
        }

    monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _fake_review_chunk)
    result = orchestrate_review(
        forge, REF, _StubLLM(), post=False, max_workers=1, max_errors=2,
    )
    assert len(result["findings_active"]) == 2
    assert result["verdict"] == "Request-Changes"
    assert len(result["findings_dropped"]) == 1
    assert "error cap exceeded" in result["findings_dropped"][0].drop_reason


def test_control_temperature_reaches_the_wire():
    """Temperature 0.0 in the wire payload is owned by
    ``tests/test_llm_backends.py::TestTemperature``; not duplicated here, only
    executed, so the payload contract keeps one owner."""
    from tests.test_llm_backends import TestTemperature

    TestTemperature().test_zero_is_a_real_value_not_an_omission()
    TestTemperature().test_sent_when_configured()
