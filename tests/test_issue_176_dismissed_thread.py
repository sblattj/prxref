"""Issue 176: a resolved thread dismissed with a reason must not be re-raised.

The repro: review 1 posts a finding, the author replies with the reason it
is wrong ("invalid: start() is meant to fail, refresh() catches and logs")
and resolves the thread. Review 2, with those lines unchanged, posts the
same claim three lines away carrying the #73 "Previously raised in ...;
still present at ..." footer: the sweep matched the old thread and re-posted
anyway, because a resolved thread only kept suppressing when a human wrote
the literal words "won't fix".

Frozen contracts under test: the last human reply on a thread, from someone
other than the thread's author and not a fix acknowledgement, is the
thread's ``dismissal``; a resolved, current (not outdated) thread with a
dismissal is a decision (``Thread.decided``) and keeps suppressing like a
won't-fix one; once the code under it moves (outdated) the finding is
re-posted with the previously-raised note again; the summary names the
dismissal reply on the suppressed bullet.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_forge_github import _graphql_nodes, _mock_response, _node, _ref  # noqa: E402
from test_orchestrator import REF, FakeForge, FakeLLM, _added_file_diff  # noqa: E402

from prxref.forges.base import ATTRIBUTION_MARKER, Thread, dismissal_reply  # noqa: E402
from prxref.forges.github import ForgeImpl  # noqa: E402
from prxref.formatter import format_inline_comment  # noqa: E402
from prxref.orchestrator import orchestrate_review  # noqa: E402
from prxref.quality import (  # noqa: E402
    apply_settled_thread_suppression,
    apply_thread_dedup,
    previously_discussed_thread,
)
from prxref.triage import Finding  # noqa: E402

JAVA = "src/main/java/store/ValueStore.java"
PY = "jobs/reconcile.py"

REVIEW_1 = Finding(
    file=JAVA, line=154, severity="warning", confidence=0.9,
    title="SDK exceptions escape readValue unhandled",
    body=(
        "client.getValue can throw an SDK exception that escapes readValue "
        "unhandled; catch and log it so callers do not crash."
    ),
)
DISMISSAL = (
    "invalid: start() is meant to fail, refresh() catches and logs, "
    "parse() rejects empty"
)
REVIEW_2 = {
    "file": JAVA, "line": 157, "severity": "warning", "confidence": 0.9,
    "title": "readValue can throw or return an empty value with no handling",
    "body": (
        "client.getValue may throw an SDK exception, or the value may be "
        "empty, and readValue has no handling; callers may crash."
    ),
}

PY_RAISED = Finding(
    file=PY, line=151, severity="warning", confidence=0.9,
    title="Re-created active component orphaned when the write fails",
    body=(
        "A re-created active component is orphaned if the write then fails; "
        "track the created id and discard it like the rotate path does."
    ),
)
PY_DISMISSAL = (
    "it's created under the entry's own name and key, so it publishes the "
    "active kid; if the write fails, the next run adopts it through find_by_name."
)
PY_AGAIN = {
    "file": PY, "line": 143, "severity": "warning", "confidence": 0.9,
    "title": "Re-created active component orphaned when the write fails",
    "body": (
        "A re-created active component is orphaned if the write then fails; "
        "track the created id and discard it like the rotate path does."
    ),
}


def _github_threads(monkeypatch, root: Finding, reply: str, *, resolved=True, outdated=False):
    """The threads the real GitHub adapter reads for one prxref finding and its reply."""
    monkeypatch.setenv("PRXREF_GITHUB_TOKEN", "t")
    session = MagicMock(spec=requests.Session)
    line = None if outdated else root.line
    session.get.return_value = _mock_response(json_data=[
        {"id": 70, "path": root.file, "line": line, "user": {"login": "prxref-bot"},
         "body": format_inline_comment(root, f"{ATTRIBUTION_MARKER} · model=m")},
        {"id": 71, "path": root.file, "line": line, "in_reply_to_id": 70,
         "user": {"login": "dev"}, "body": reply},
    ])
    session.post.return_value = _graphql_nodes(_node(70, None, resolved, outdated))
    return ForgeImpl(session=session).list_threads(_ref())


def _review(threads, finding):
    forge = FakeForge(diff=_added_file_diff(finding["file"], 200), threads=threads)
    res = orchestrate_review(forge, REF, FakeLLM(findings_by_path={finding["file"]: [finding]}))
    return res, forge


class TestIssueFailureCase:
    def test_review_2_does_not_re_raise_a_dismissed_claim(self, monkeypatch, contract_stubs):
        threads = _github_threads(monkeypatch, REVIEW_1, DISMISSAL)
        res, forge = _review(threads, REVIEW_2)

        assert res["findings_active"] == []
        assert [f.title for f in res["findings_dropped"]] == [REVIEW_2["title"]]
        assert all(f.previous_thread is None for f in res["findings_dropped"])
        assert not any(b for b in forge.inline_batches)
        summary = forge.summaries[-1]
        assert "Previously raised" not in summary
        assert "dismissed: “invalid: start() is meant to fail" in summary

    def test_a_dismissal_with_no_keyword_counts_too(self, monkeypatch, contract_stubs):
        threads = _github_threads(monkeypatch, PY_RAISED, PY_DISMISSAL)
        res, _ = _review(threads, PY_AGAIN)

        assert res["findings_active"] == []
        assert [f.title for f in res["findings_dropped"]] == [PY_AGAIN["title"]]

    def test_once_the_code_moves_the_finding_is_posted_with_the_note(
        self, monkeypatch, contract_stubs,
    ):
        threads = _github_threads(monkeypatch, REVIEW_1, DISMISSAL, outdated=True)
        res, _ = _review(threads, REVIEW_2)

        [active] = res["findings_active"]
        assert active.previous_thread is not None
        assert active.previous_thread.startswith("Previously raised in thread by prxref-bot")

    def test_a_fix_acknowledgement_is_not_a_dismissal(self, monkeypatch, contract_stubs):
        threads = _github_threads(monkeypatch, REVIEW_1, "Good catch, fixed in the next commit.")
        res, _ = _review(threads, REVIEW_2)

        [active] = res["findings_active"]
        assert active.previous_thread is not None

    def test_an_unresolved_dismissed_thread_still_suppresses_as_open(
        self, monkeypatch, contract_stubs,
    ):
        threads = _github_threads(monkeypatch, REVIEW_1, DISMISSAL, resolved=False)
        res, _ = _review(threads, REVIEW_2)

        assert res["findings_active"] == []


class TestAdapterReadsTheDismissal:
    def test_the_reply_marks_every_comment_of_its_thread(self, monkeypatch):
        threads = _github_threads(monkeypatch, REVIEW_1, DISMISSAL)
        assert [t.dismissal for t in threads] == [DISMISSAL, DISMISSAL]
        assert all(t.decided and not t.lapsed for t in threads)

    def test_a_root_without_a_reply_has_no_dismissal(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITHUB_TOKEN", "t")
        session = MagicMock(spec=requests.Session)
        session.get.return_value = _mock_response(json_data=[
            {"id": 80, "path": JAVA, "line": 154, "user": {"login": "prxref-bot"},
             "body": "x"},
        ])
        session.post.return_value = _graphql_nodes(_node(80, None, True, False))
        [t] = ForgeImpl(session=session).list_threads(_ref())
        assert (t.dismissal, t.decided, t.lapsed) == (None, False, True)


class TestDismissalReply:
    @pytest.mark.parametrize("comments", [
        [],
        [("bot", "claim")],
        [("bot", "claim"), ("dev", "Good catch, fixed.")],
        [("bot", "claim"), ("dev", "fixed in abc123")],
        [("bot", "claim"), ("dev", "Done")],
        [("bot", "claim"), ("dev", "Thanks, addressed.")],
        [("bot", "claim"), ("dev", "Agreed, will fix")],
        [("bot", "claim"), ("dev", "   ")],
        [("alice", "claim"), ("alice", "also the other branch")],
        [("alice", "claim"), ("dev", "not a bug: x is set in init"), ("alice", "it is not, see L40")],
        [("bot", "claim"), ("other-bot", f"invalid\n\n*{ATTRIBUTION_MARKER} · model=m*")],
    ])
    def test_not_a_dismissal(self, comments):
        assert dismissal_reply(comments) is None

    @pytest.mark.parametrize(("comments", "expected"), [
        ([("bot", "claim"), ("dev", DISMISSAL)], DISMISSAL),
        ([("bot", "claim"), ("dev", PY_DISMISSAL)], PY_DISMISSAL),
        ([("bot", "claim"), ("dev", "  not a bug: x is set in init  ")], "not a bug: x is set in init"),
        (
            [("bot", "claim"), ("dev", "false positive"),
             ("bot", f"follow-up\n\n*{ATTRIBUTION_MARKER} · model=m*")],
            "false positive",
        ),
    ])
    def test_a_dismissal(self, comments, expected):
        assert dismissal_reply(comments) == expected


class TestGates:
    def _thread(self, **kw):
        base = dict(
            path=JAVA, line=154, resolved=True, author="prxref-bot",
            body_snippet=format_inline_comment(REVIEW_1, "m")[:120],
        )
        base.update(kw)
        return Thread(**base)

    def _finding(self):
        return Finding(**REVIEW_2)

    def test_dismissed_resolved_current_thread_suppresses(self):
        t = self._thread(dismissal=DISMISSAL)
        assert apply_thread_dedup([self._finding()], [t])[0].drop_reason is not None
        assert previously_discussed_thread(self._finding(), [t]) is None

    def test_dismissed_but_outdated_thread_lapses(self):
        t = self._thread(dismissal=DISMISSAL, outdated=True)
        assert t.lapsed and not t.decided
        assert apply_thread_dedup([self._finding()], [t])[0].drop_reason is None
        assert apply_settled_thread_suppression([self._finding()], [t])[0].drop_reason is None
        assert previously_discussed_thread(self._finding(), [t]) is t

    def test_resolved_without_dismissal_still_lapses(self):
        t = self._thread()
        assert t.lapsed and not t.decided
        assert previously_discussed_thread(self._finding(), [t]) is t


class TestOtherForgesReadTheDismissal:
    """Every forge whose feed carries replies sets ``dismissal`` the same way."""

    def test_gitlab_discussion(self):
        import test_forge_gitlab as gl

        from prxref.forges.gitlab import ForgeImpl as GitLab

        session = MagicMock(spec=requests.Session)
        session.get.return_value = gl._mock_response(200, json_data=[{
            "id": "d1", "resolved": True, "notes": [
                {"id": 1, "author": {"username": "prxref-bot"}, "body": "claim",
                 "position": {"new_path": JAVA, "new_line": 154}, "resolved": True},
                {"id": 2, "author": {"username": "dev"}, "body": DISMISSAL, "resolved": True},
                {"id": 3, "author": {"username": "dev"}, "body": "resolved all threads",
                 "system": True},
            ],
        }])
        ref = gl.PRRef("gitlab", "gitlab.com", "g", "r", 3, "https://gitlab.com/g/r/-/merge_requests/3")
        threads = GitLab(session=session).list_threads(ref)
        assert [t.dismissal for t in threads] == [DISMISSAL] * 3
        assert all(t.decided for t in threads)

    def test_azure_devops_thread(self):
        import test_forge_azure_devops as ado

        forge, _ = ado._threads_forge({"value": [
            {"id": 1, "status": "closed", "comments": [
                {"id": 1, "content": "claim", "commentType": "text",
                 "author": {"uniqueName": "prxref-bot"}},
                {"id": 2, "content": DISMISSAL, "commentType": "text",
                 "author": {"uniqueName": "dev"}},
                {"id": 3, "content": "closed", "commentType": "system"},
            ]},
        ]})
        (thread,) = forge.list_threads(ado._ref())
        assert (thread.dismissal, thread.decided) == (DISMISSAL, True)

    def test_bitbucket_server_nested_replies(self):
        import test_forge_bitbucket_server as bbs

        from prxref.forges.bitbucket_server import ForgeImpl as BBS

        session = MagicMock()
        session.get.return_value = bbs._mock_response(json_data={
            "values": [{"action": "COMMENTED", "comment": {
                "id": 10, "text": "claim", "author": {"name": "prxref-bot"},
                "anchor": {"path": JAVA, "line": 154}, "state": "RESOLVED",
                "comments": [{"id": 11, "text": DISMISSAL, "author": {"name": "dev"}}],
            }}],
            "isLastPage": True,
        })
        [thread] = BBS(session=session).list_threads(bbs._ref())
        assert (thread.dismissal, thread.decided) == (DISMISSAL, True)

    def test_bitbucket_cloud_parent_chain(self):
        import test_forge_bitbucket as bb

        from prxref.forges.bitbucket import ForgeImpl as BB

        session = MagicMock(spec=requests.Session)
        session.get.return_value = bb._page([
            {"id": 1, "content": {"raw": "claim"}, "inline": {"path": JAVA, "to": 154},
             "user": {"nickname": "prxref-bot"}},
            {"id": 2, "content": {"raw": DISMISSAL}, "inline": {"path": JAVA, "to": 154},
             "user": {"nickname": "dev"}, "parent": {"id": 1}},
            {"id": 3, "content": {"raw": "other"}, "inline": {"path": JAVA, "to": 9},
             "user": {"nickname": "bob"}},
        ])
        threads = BB(session=session).list_threads(bb._ref())
        assert [t.dismissal for t in threads] == [DISMISSAL, DISMISSAL, None]
