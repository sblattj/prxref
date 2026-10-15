"""Issue 117: an error still open in an existing thread keeps Request-Changes.

The repro: the gate requested changes on a PR with two error findings, the
author pushed an unrelated commit, and the re-review found both bugs again.
Thread dedup dropped both as ``duplicate of existing thread`` (correct: no
repeat comment), and the verdict, computed from active findings only, came
out ``Approved`` with both error threads still open.

Frozen contracts under test: an error-severity finding at or above the
confidence floor that a thread gate drops against an open thread holds the
verdict at ``Request-Changes`` without being posted again; a won't-fix
thread, a warning, a sub-floor finding, or a fixed bug does not; the run
record counts these in ``thread_dedup.open_errors``; and the summary says why.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_orchestrator import (  # noqa: E402  (shared fixtures, read not guessed)
    REF,
    FakeForge,
    FakeLLM,
    _added_file_diff,
)

from prxref import orchestrator  # noqa: E402
from prxref.forges.base import Thread  # noqa: E402
from prxref.orchestrator import orchestrate_review  # noqa: E402

DIFF = _added_file_diff("src/app.py", 20)

THREAD_SNIPPET = (
    "Null deref: x may be None when config is missing here, did you "
    "handle the empty config case?"
)


def _finding(severity="error", confidence=0.9):
    return {
        "file": "src/app.py",
        "line": 3,
        "severity": severity,
        "confidence": confidence,
        "title": "Null deref",
        "body": "x may be None when config is missing; data loss follows.",
    }


def _thread(*, resolved=False, outdated=False, wont_fix=False):
    return Thread(
        path="src/app.py",
        line=3,
        resolved=resolved,
        outdated=outdated,
        author="prxref",
        body_snippet=THREAD_SNIPPET,
        wont_fix=wont_fix,
    )


def _review(findings, threads):
    forge = FakeForge(diff=DIFF, threads=threads)
    res = orchestrate_review(
        forge, REF, FakeLLM(findings_by_path={"src/app.py": findings}),
    )
    return forge, res


def _posted_inline(forge):
    return [c for batch in forge.inline_batches for c in batch]


class TestOpenThreadHoldsTheVerdict:
    def test_an_error_still_open_in_its_thread_requests_changes(self, contract_stubs):
        forge, res = _review([_finding()], [_thread()])

        assert res["findings_active"] == []
        assert [f.drop_reason for f in res["findings_dropped"]] == [
            "duplicate of existing thread"
        ]
        assert res["verdict"] == "Request-Changes"
        assert _posted_inline(forge) == []

    def test_the_run_record_counts_it(self, contract_stubs):
        _, res = _review([_finding()], [_thread()])

        assert res["thread_dedup"]["suppressed"] == 1
        assert res["thread_dedup"]["open_errors"] == 1

    def test_the_summary_says_why(self, contract_stubs):
        forge, _ = _review([_finding()], [_thread()])

        assert (
            "1 suppressed error finding(s) are still open in their threads, "
            "so the verdict stays Request-Changes"
        ) in forge.summaries[0]


class TestWhatDoesNotHoldTheVerdict:
    def test_a_fixed_bug_approves_with_the_thread_still_open(self, contract_stubs):
        _, res = _review([], [_thread()])

        assert res["verdict"] == "Approved"

    def test_a_suppressed_warning_approves(self, contract_stubs):
        _, res = _review([_finding(severity="warning")], [_thread()])

        assert res["findings_active"] == []
        assert res["thread_dedup"]["open_errors"] == 0
        assert res["verdict"] == "Approved"

    def test_a_wont_fix_thread_approves(self, contract_stubs):
        _, res = _review([_finding()], [_thread(resolved=True, wont_fix=True)])

        assert res["findings_active"] == []
        assert res["thread_dedup"]["open_errors"] == 0
        assert res["verdict"] == "Approved"

    def test_an_open_wont_fix_thread_approves(self, contract_stubs):
        _, res = _review([_finding()], [_thread(wont_fix=True)])

        assert res["findings_active"] == []
        assert res["verdict"] == "Approved"

    def test_a_sub_floor_error_approves(self, contract_stubs):
        _, res = _review([_finding(confidence=0.2)], [_thread()])

        assert res["thread_dedup"]["open_errors"] == 0
        assert res["verdict"] == "Approved"

    def test_a_resolved_thread_reposts_and_requests_changes(self, contract_stubs):
        forge, res = _review([_finding()], [_thread(resolved=True)])

        assert [f.title for f in res["findings_active"]] == ["Null deref"]
        assert res["thread_dedup"]["open_errors"] == 0
        assert res["verdict"] == "Request-Changes"
        assert len(_posted_inline(forge)) == 1


class TestAccountingHelper:
    def test_no_open_errors_keeps_the_old_shape(self):
        assert orchestrator._thread_dedup_accounting(1, 0) == (
            orchestrator._thread_dedup_accounting(1, 0, [], [], 0)
        )
        assert "still open" not in orchestrator._thread_dedup_accounting(1, 0)


class TestFailOnAgreesWithTheVerdict:
    def _result(self, open_errors):
        return {
            "verdict": "Request-Changes" if open_errors else "Approved",
            "findings_active": [],
            "thread_dedup": {"open_errors": open_errors},
        }

    def test_error_policy_exits_1_on_an_open_thread_error(self):
        from prxref import cli

        code, note = cli._fail_on_exit(self._result(2), "error")
        assert code == 1
        assert note == (
            "PRXREF_FAIL_ON=error: 2 error findings still open in existing "
            "threads; exiting 1"
        )

    def test_any_policy_exits_1_too(self):
        from prxref import cli

        assert cli._fail_on_exit(self._result(1), "any")[0] == 1

    def test_never_and_zero_stay_0(self):
        from prxref import cli

        assert cli._fail_on_exit(self._result(1), "never") == (0, None)
        assert cli._fail_on_exit(self._result(0), "error") == (0, None)
