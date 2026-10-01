"""Range-aware thread dedup and the JSON surfaces for code suggestions (#30).

A multi-line GitHub comment is read at its end line, as it was before
suggestions existed, and its first line is kept as ``Thread.start_line``.
Re-review dedup then treats such a thread as a range: a finding anywhere
inside it is at distance 0, and one outside it is measured from the nearer
end. For a single-line thread every verdict is the one the old arithmetic
gave. The ``--format json`` rows and the eval run record carry the new
suggestion fields.
"""
from __future__ import annotations

import itertools
from collections.abc import Sequence
from unittest.mock import MagicMock

import pytest
import requests

from prxref import cli, evals
from prxref.config import load_config
from prxref.forges import github
from prxref.forges.base import Thread
from prxref.quality import _tokens, apply_thread_dedup, is_duplicate_of_existing
from prxref.triage import Finding

PATH = "src/app.py"
GITHUB_URL = "https://github.com/acme/api/pull/42"


def _finding(line: int, body: str = "mongoose kestrel") -> Finding:
    return Finding(PATH, line, "warning", 0.9, "Zebrafish", body)


def _thread(line, body="zebrafish", start_line=None, path=PATH) -> Thread:
    return Thread(
        path=path, line=line, resolved=False, author="reviewer",
        body_snippet=body, start_line=start_line,
    )


def _old_is_duplicate_of_existing(
    finding: Finding,
    threads: Sequence[Thread],
    line_window: int = 30,
    min_shared_tokens: int = 2,
    min_shared_for_distant: int = 4,
) -> bool:
    """Return True if an existing thread on the same path overlaps in topic.

    Tiered threshold based on line distance:
    - Same line (distance 0): 1 distinctive shared token is enough.
    - Within line_window: min_shared_tokens (default 2).
    - Beyond window or unknown/file-level line: distant threshold (default 4+).
    """
    finding_tokens = _tokens(f"{finding.title} {finding.body}")
    if not finding_tokens:
        return False

    distant_required = max(min_shared_for_distant, len(finding_tokens) // 2)

    for t in threads:
        if t.path != finding.file:
            continue

        body_tokens = _tokens(t.body_snippet or "")
        if not body_tokens:
            continue

        f_line = finding.line if finding.line > 0 else None
        t_line = t.line if t.line is not None and t.line > 0 else None

        if f_line is not None and t_line is not None:
            distance = abs(t_line - f_line)
            if distance == 0:
                required = 1
            elif distance <= line_window:
                required = min_shared_tokens
            else:
                required = distant_required
        else:
            required = distant_required

        shared = finding_tokens & body_tokens
        if len(shared) >= required:
            return True

    return False


# --- a human multi-line thread, start 10, end 14 --------------------------------

HUMAN_RANGE = _thread(14, start_line=10)


def test_a_finding_at_the_end_line_is_a_duplicate_at_one_token():
    assert is_duplicate_of_existing(_finding(14), [HUMAN_RANGE])


def test_a_finding_at_the_first_line_is_a_duplicate_at_one_token():
    assert is_duplicate_of_existing(_finding(10), [HUMAN_RANGE])
    assert not _old_is_duplicate_of_existing(_finding(10), [HUMAN_RANGE])


def test_a_finding_inside_the_range_is_a_duplicate_at_one_token():
    assert is_duplicate_of_existing(_finding(12), [HUMAN_RANGE])


def test_a_finding_below_the_range_is_measured_from_the_end_line():
    assert not is_duplicate_of_existing(_finding(30), [HUMAN_RANGE])
    two_shared = _thread(14, body="zebrafish mongoose", start_line=10)
    assert is_duplicate_of_existing(_finding(30), [two_shared])


def test_a_finding_above_the_range_is_measured_from_the_first_line():
    assert not is_duplicate_of_existing(_finding(9), [HUMAN_RANGE])
    assert is_duplicate_of_existing(_finding(9), [_thread(14, body="zebrafish mongoose", start_line=10)])
    assert not is_duplicate_of_existing(_finding(50), [_thread(14, body="zebrafish mongoose", start_line=10)])


def test_the_range_reaches_the_dedup_pass():
    (dup,) = apply_thread_dedup([_finding(10)], [HUMAN_RANGE])
    assert dup.drop_reason == "duplicate of existing thread"
    (kept,) = apply_thread_dedup([_finding(30)], [HUMAN_RANGE])
    assert kept.drop_reason is None


# --- the new matching only ever adds duplicates ---------------------------------

LINES = (0, 1, 5, 9, 10, 12, 14, 15, 29, 30, 44, 45, 46, 80)
BODIES = (
    "zebrafish",
    "zebrafish mongoose",
    "zebrafish mongoose kestrel",
    "unrelated words entirely",
    "",
)
FINDING_BODIES = ("mongoose kestrel", "mongoose kestrel ocelot wombat platypus narwhal")


def _single_line_threads():
    for line, body in itertools.product((None, *LINES), BODIES):
        yield _thread(line, body=body)
        if line is not None:
            yield _thread(line, body=body, start_line=line)
            yield _thread(line, body=body, start_line=line + 3)
            yield _thread(line, body=body, start_line=0)
    yield _thread(14, path="src/other.py")


def test_single_line_threads_get_exactly_the_old_verdicts():
    threads = list(_single_line_threads())
    compared = 0
    for line, body, t in itertools.product(LINES, FINDING_BODIES, threads):
        f = _finding(line, body)
        assert is_duplicate_of_existing(f, [t]) == _old_is_duplicate_of_existing(f, [t]), (f, t)
        compared += 1
    assert compared == len(LINES) * len(FINDING_BODIES) * len(threads)


def test_a_range_thread_is_a_duplicate_wherever_its_end_line_alone_was():
    for start, end in ((10, 14), (1, 2), (20, 45)):
        for line, body, fbody in itertools.product(LINES, BODIES, FINDING_BODIES):
            f = _finding(line, fbody)
            if _old_is_duplicate_of_existing(f, [_thread(end, body=body)]):
                assert is_duplicate_of_existing(f, [_thread(end, body=body, start_line=start)])


# --- the GitHub thread mapping ---------------------------------------------------


def _github_threads(items):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    resp.ok = True
    resp.headers = {}
    resp.json.return_value = items
    resp.text = ""
    session = MagicMock(spec=requests.Session)
    session.get.return_value = resp
    ref = github.ForgeImpl.parse_pr_url(GITHUB_URL)
    return github.ForgeImpl(session=session).list_threads(ref)


PAYLOADS = [
    {"path": PATH, "start_line": 10, "line": 14, "body": "a"},
    {"path": PATH, "start_line": None, "line": 20, "body": "b"},
    {"path": PATH, "line": 7, "body": "c"},
    {"path": PATH, "start_line": 14, "line": 14, "body": "d"},
    {"path": PATH, "start_line": 16, "line": 14, "body": "e"},
    {"path": PATH, "start_line": 0, "line": 14, "body": "f"},
    {"path": PATH, "start_line": 3, "line": None, "original_line": 9, "body": "g"},
    {"path": PATH, "start_line": 3, "line": None, "original_line": None, "position": None, "body": "h"},
    {"path": PATH, "start_line": "3", "line": 9, "body": "i"},
    {"path": None, "body": "j"},
]


def test_github_threads_keep_the_line_they_had_before_suggestions():
    # Issue #73: the old `line or original_line or position` fallback
    # re-anchored an outdated comment — null `line`, the value living in
    # `original_line` — at its original line, so payload "g" read as a
    # CURRENT thread anchored at 9. A null `line` now reads as outdated
    # with no anchor; a comment that still anchors the diff keeps its line
    # verbatim.
    threads = _github_threads(PAYLOADS)
    assert [t.line for t in threads] == [
        item.get("line") if isinstance(item.get("line"), int) else None
        for item in PAYLOADS
    ]
    assert threads[0].line == 14
    assert threads[6].line is None
    assert threads[6].outdated is True


def test_github_start_line_is_set_only_for_a_real_range():
    # Payload "g" loses its start_line of 3 with issue #73: without a
    # current `line` there is no range to anchor, only an outdated thread.
    threads = _github_threads(PAYLOADS)
    assert [t.start_line for t in threads] == [10, None, None, None, None, None, None, None, None, None]


def test_a_thread_built_without_start_line_is_single_line():
    assert Thread(path=PATH, line=5, resolved=False, author="a", body_snippet="b").start_line is None


# --- the --format json rows ---------------------------------------------------------


def test_a_row_carries_a_kept_suggestion():
    f = Finding(PATH, 10, "warning", 0.9, "T", "B", suggestion="a\nb", suggestion_end_line=11)
    row = cli._finding_json(f, drop_reason=None)
    assert (row["suggestion"], row["suggestion_end_line"]) == ("a\nb", 11)
    keys = list(row)
    assert keys.index("suggestion") == keys.index("rule") + 1
    assert keys.index("suggestion_end_line") == keys.index("suggestion") + 1


def test_a_row_keeps_a_deleting_suggestion_as_an_empty_string():
    f = Finding(PATH, 10, "warning", 0.9, "T", "B", suggestion="", suggestion_end_line=0)
    row = cli._finding_json(f, drop_reason=None)
    assert (row["suggestion"], row["suggestion_end_line"]) == ("", 0)


def test_a_row_without_a_suggestion_carries_null_and_zero():
    row = cli._finding_json(Finding(PATH, 10, "warning", 0.9, "T", "B"), drop_reason="x")
    assert (row["suggestion"], row["suggestion_end_line"]) == (None, 0)


def test_a_stale_end_line_without_a_suggestion_reports_zero():
    f = Finding(PATH, 10, "warning", 0.9, "T", "B", suggestion=None, suggestion_end_line=12)
    assert cli._finding_json(f, drop_reason=None)["suggestion_end_line"] == 0


@pytest.mark.parametrize("obj", [MagicMock(spec=["file", "line", "severity", "confidence", "title", "body"])])
def test_a_finding_object_without_the_fields_reports_null_and_zero(obj):
    row = cli._finding_json(obj, drop_reason=None)
    assert (row["suggestion"], row["suggestion_end_line"]) == (None, 0)


# --- the eval run record -----------------------------------------------------------


def test_the_run_record_allowlist_carries_suggestions_before_context_followup():
    keys = list(evals.RUN_CONFIG_KEYS)
    assert keys.index("suggestions") == keys.index("context_followup") - 1


def test_the_allowlisted_key_is_a_loaded_setting(monkeypatch):
    monkeypatch.delenv("PRXREF_SUGGESTIONS", raising=False)
    assert load_config()["suggestions"] == "off"
