"""Issue 73: resolved or outdated threads must not suppress findings.

The repro: a reviewer thread that mentioned the same subject was resolved —
often for an unrelated sub-issue — while the code it flagged stayed
unchanged. Both thread gates ignored ``resolved`` (it was write-only: every
adapter populated it, nothing read it), so every re-review silently dropped
the finding as ``duplicate of existing thread`` / ``settled in thread``, and
the summary said nothing about it.

Frozen contracts under test: only an OPEN, CURRENT thread suppresses; a
surviving finding that matches a resolved or outdated thread carries a
``previous_thread`` note rendered as a suffix on the inline body and the
summary bullet; the run record carries a ``thread_dedup`` block; and the
summary carries the thread-dedup accounting line exactly when a count is
non-zero.
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
    make_pr,
)

from prxref import orchestrator  # noqa: E402
from prxref.forges.base import Thread  # noqa: E402
from prxref.orchestrator import orchestrate_review  # noqa: E402
from prxref.quality import (  # noqa: E402
    apply_settled_thread_suppression,
    is_duplicate_of_existing,
    previously_discussed_thread,
)
from prxref.triage import Finding, parse_unified_diff  # noqa: E402

DIFF = _added_file_diff("src/app.py", 20)

# The finding every thread below restates: same path, same line, same claim.
FINDING = {
    "file": "src/app.py",
    "line": 3,
    "severity": "error",
    "confidence": 0.9,
    "title": "Null deref",
    "body": "x may be None when config is missing; data loss follows.",
}
FINDINGS = {"src/app.py": [FINDING]}

THREAD_SNIPPET = (
    "Null deref: x may be None when config is missing here — did you "
    "handle the empty config case?"
)
THREAD_URL = "https://github.com/acme/api/pull/42/files#discussion_r123"


def _thread(
    *, resolved=False, outdated=False, line=3, path="src/app.py", url=None,
    author="alice", wont_fix=False,
):
    return Thread(
        path=path,
        line=line,
        resolved=resolved,
        outdated=outdated,
        author=author,
        body_snippet=THREAD_SNIPPET,
        url=url,
        wont_fix=wont_fix,
    )


def _finding():
    return Finding(
        file=FINDING["file"],
        line=FINDING["line"],
        severity=FINDING["severity"],
        confidence=FINDING["confidence"],
        title=FINDING["title"],
        body=FINDING["body"],
    )


class TestWontFixThreadsStillSuppress:
    def test_wont_fix_resolved_thread_still_dedupes(self):
        assert is_duplicate_of_existing(_finding(), [_thread(resolved=True, wont_fix=True)])

    def test_wont_fix_resolved_thread_still_settles(self):
        out = apply_settled_thread_suppression(
            [_finding()], [_thread(resolved=True, wont_fix=True)],
        )
        assert out[0].drop_reason is not None

    def test_wont_fix_thread_gives_no_previously_raised_note(self):
        assert previously_discussed_thread(
            _finding(), [_thread(resolved=True, wont_fix=True)],
        ) is None


class TestGatesSkipClosedThreads:
    def test_resolved_thread_does_not_match_the_dedup_gate(self):
        f = _finding()
        assert is_duplicate_of_existing(f, [_thread()])
        assert not is_duplicate_of_existing(f, [_thread(resolved=True)])

    def test_outdated_thread_does_not_match_the_dedup_gate(self):
        assert not is_duplicate_of_existing(_finding(), [_thread(outdated=True)])

    def test_resolved_thread_does_not_settle_its_subject(self):
        f = _finding()
        out = apply_settled_thread_suppression([f], [_thread(resolved=True)])
        assert out[0].drop_reason is None

    def test_outdated_thread_does_not_settle_its_subject(self):
        out = apply_settled_thread_suppression(
            [_finding()], [_thread(outdated=True)],
        )
        assert out[0].drop_reason is None

    def test_an_open_current_thread_still_settles_and_dedupes(self):
        f = _finding()
        assert is_duplicate_of_existing(f, [_thread()])
        out = apply_settled_thread_suppression([f], [_thread(line=None)])
        assert out[0].drop_reason == "settled in thread: alice"


class TestPreviouslyDiscussedThread:
    def test_a_resolved_thread_is_found_again_for_the_note(self):
        t = _thread(resolved=True)
        assert previously_discussed_thread(_finding(), [t]) is t

    def test_an_outdated_thread_is_found_again_for_the_note(self):
        t = _thread(outdated=True)
        assert previously_discussed_thread(_finding(), [t]) is t

    def test_the_match_is_line_independent_like_the_settled_gate(self):
        # distance 0 was the dedup gate's easy case; the settled-style match
        # (same path, >=4 shared tokens, no line test) is what carries the
        # note once line alignment has demoted the finding to line 0.
        t = _thread(resolved=True, line=None)
        assert previously_discussed_thread(_finding(), [t]) is t

    def test_an_open_current_thread_is_never_a_previous_thread(self):
        assert previously_discussed_thread(_finding(), [_thread()]) is None

    def test_a_thread_on_another_path_is_never_a_previous_thread(self):
        t = _thread(resolved=True, path="src/other.py")
        assert previously_discussed_thread(_finding(), [t]) is None


class TestEndToEnd:
    def test_a_resolved_thread_keeps_the_finding_and_notes_it(self, contract_stubs):
        forge = FakeForge(diff=DIFF, threads=[_thread(resolved=True)])
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=FINDINGS))

        assert [f.title for f in res["findings_active"]] == ["Null deref"]
        assert res["findings_dropped"] == []
        assert res["findings_active"][0].previous_thread == (
            "Previously raised in thread by alice at src/app.py:3; "
            "still present at src/app.py:3."
        )

    def test_a_resolved_and_outdated_thread_keeps_the_finding(self, contract_stubs):
        forge = FakeForge(diff=DIFF, threads=[_thread(resolved=True, outdated=True)])
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=FINDINGS))

        assert [f.title for f in res["findings_active"]] == ["Null deref"]
        assert res["findings_dropped"] == []

    def test_an_open_outdated_thread_keeps_the_finding(self, contract_stubs):
        # A moved anchor alone (thread line != finding line) is not enough to
        # escape the line-window tier; ``outdated`` explicitly is.
        forge = FakeForge(diff=DIFF, threads=[_thread(outdated=True)])
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=FINDINGS))

        assert [f.title for f in res["findings_active"]] == ["Null deref"]
        assert res["findings_dropped"] == []
        assert res["findings_active"][0].previous_thread is not None

    def test_an_open_current_thread_still_suppresses(self, contract_stubs):
        forge = FakeForge(diff=DIFF, threads=[_thread()])
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=FINDINGS))

        assert res["findings_active"] == []
        dropped = res["findings_dropped"]
        assert [f.title for f in dropped] == ["Null deref"]
        assert dropped[0].drop_reason == "duplicate of existing thread"
        assert dropped[0].previous_thread is None


class TestRunRecord:
    def test_counts_a_suppressed_duplicate(self, contract_stubs):
        forge = FakeForge(diff=DIFF, threads=[_thread()])
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=FINDINGS))

        assert res["thread_dedup"] == {
            "suppressed": 1,
            "matched_resolved": 0,
            "threads_read": 1,
            "threads_resolved": 0,
        }

    def test_counts_a_resolved_match(self, contract_stubs):
        forge = FakeForge(diff=DIFF, threads=[_thread(resolved=True, outdated=True)])
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=FINDINGS))

        assert res["thread_dedup"] == {
            "suppressed": 0,
            "matched_resolved": 1,
            "threads_read": 1,
            "threads_resolved": 1,
        }

    def test_a_run_without_threads_has_no_thread_dedup_block(self, contract_stubs):
        forge = FakeForge(diff=DIFF)
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=FINDINGS))

        assert "thread_dedup" not in res


class TestSummaryAccounting:
    def test_the_accounting_line_names_both_counts(self, contract_stubs):
        forge = FakeForge(diff=DIFF, threads=[_thread(resolved=True)])
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=FINDINGS))

        assert (
            "Thread dedup: 0 suppressed as duplicates of open threads; "
            "1 matched resolved threads and are posted below."
        ) in forge.summaries[0]
        assert res["posted"] is True

    def test_a_suppressed_duplicate_is_also_accounted(self, contract_stubs):
        forge = FakeForge(diff=DIFF, threads=[_thread()])
        orchestrate_review(forge, REF, FakeLLM(findings_by_path=FINDINGS))

        assert (
            "Thread dedup: 1 suppressed as duplicates of open threads; "
            "0 matched resolved threads and are posted below."
        ) in forge.summaries[0]

    def test_no_thread_history_means_no_accounting_line(self, contract_stubs):
        forge = FakeForge(diff=DIFF)
        orchestrate_review(forge, REF, FakeLLM(findings_by_path=FINDINGS))

        assert "Thread dedup:" not in forge.summaries[0]

    def test_the_accounting_helpers_exact_shape(self):
        assert orchestrator._thread_dedup_accounting(2, 1) == (
            "Thread dedup: 2 suppressed as duplicates of open threads; "
            "1 matched resolved threads and are posted below."
        )
        assert orchestrator._thread_dedup_accounting(0, 0) == ""


class TestPreviouslyRaisedRendering:
    def test_the_inline_body_carries_the_note_as_a_suffix(self, contract_stubs):
        forge = FakeForge(diff=DIFF, threads=[_thread(resolved=True, url=THREAD_URL)])
        orchestrate_review(forge, REF, FakeLLM(findings_by_path=FINDINGS))

        body = forge.inline_batches[0][0].body
        # The frozen header/body/footer stay intact; the note trails them.
        assert "---\n*Reviewed by prxref" in body
        assert body.endswith(
            f"Previously raised in {THREAD_URL}; still present at src/app.py:3."
        )

    def test_the_summary_bullet_carries_the_note(self, contract_stubs):
        forge = FakeForge(diff=DIFF, threads=[_thread(resolved=True)])
        orchestrate_review(forge, REF, FakeLLM(findings_by_path=FINDINGS))

        assert (
            "`src/app.py:3` — Null deref — Previously raised in thread by "
            "alice at src/app.py:3; still present at src/app.py:3."
        ) in forge.summaries[0]

    def test_a_finding_without_a_note_renders_exactly_as_before(self, contract_stubs):
        forge = FakeForge(diff=DIFF)
        orchestrate_review(forge, REF, FakeLLM(findings_by_path=FINDINGS))

        body = forge.inline_batches[0][0].body
        assert "Previously raised" not in body
        assert body.endswith("---\n*Reviewed by prxref · model=test-model-1*")

    def test_render_summary_places_the_accounting_after_the_findings(self):
        f = _finding()
        f.previous_thread = (
            "Previously raised in thread by alice at src/app.py:3; "
            "still present at src/app.py:3."
        )
        summary = orchestrator._render_summary(
            make_pr(),
            parse_unified_diff(DIFF),
            "Request-Changes",
            [f],
            "test-model-1",
            10,
            5,
            7,
            thread_accounting=orchestrator._thread_dedup_accounting(0, 1),
        )
        bullets = "Null deref — Previously raised in thread by alice"
        accounting = (
            "Thread dedup: 0 suppressed as duplicates of open threads; "
            "1 matched resolved threads and are posted below."
        )
        assert bullets in summary
        assert accounting in summary
        # The accounting line rides the end of {findings}, after its bullets.
        assert summary.index(bullets) < summary.index(accounting)


class TestNoteReferenceShapes:
    def test_a_thread_without_a_url_names_author_path_and_line(self):
        t = _thread(resolved=True)
        assert orchestrator._thread_reference(t) == "thread by alice at src/app.py:3"

    def test_a_thread_url_wins_when_the_forge_reported_one(self):
        t = _thread(resolved=True, url=THREAD_URL)
        assert orchestrator._thread_reference(t) == THREAD_URL

    def test_a_thread_without_a_line_names_the_path_only(self):
        t = _thread(resolved=True, line=None)
        assert orchestrator._thread_reference(t) == "thread by alice at src/app.py"

    def test_a_thread_without_a_path_names_the_pr(self):
        t = _thread(resolved=True, path=None, line=None)
        assert orchestrator._thread_reference(t) == "thread by alice on the PR"
