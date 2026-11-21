"""Issue #182: a doc change that rewrites a rule sees the same file's old statement of it.

The failure case: a docs-only PR rewrites one paragraph of a long OpenAPI
``info.description`` to add an exception to a rule, and an unchanged
paragraph far below in the same block still states the old rule with no
exception. The worker only saw the hunk and a few context lines, so it had
nothing to contradict. Now a chunk that edits a document or spec gets a
block of unchanged same-file lines that share wording with the change, and
a finding anchored on the changed line that cites the stale line survives
the quality gate.
"""
from __future__ import annotations

import json
import threading
from dataclasses import replace

import pytest

from prxref import chunk_context, orchestrator
from prxref.chunk_context import (
    RESTATEMENT_GUIDANCE,
    RESTATEMENT_HEADER,
    ChunkFile,
    is_document_path,
    same_file_restatements,
)
from prxref.llm import InvokeResult
from prxref.quality import apply_restatement_anchor
from prxref.triage import Finding, parse_unified_diff
from tests.test_orchestrator import REF, FakeForge

PATH = "api/library.yaml"
OLD_RULE = (
    "    **Late returns.** A late return only adds a fee to the member's "
    "account; it never blocks the account."
)
NEW_RULE = (
    "    **Late returns.** A late return never blocks the member's account. "
    "One case does: a book that belongs to a closed branch blocks the "
    "account until staff clear it."
)
STALE = "    Late returns only add a fee; the account stays open."


def _head_lines(rule: str) -> list[str]:
    lines = [
        "openapi: 3.0.3",
        "info:",
        "  title: Library lending API",
        "  version: 2.4.0",
        "  description: |",
        "    The lending API lets branches check books in and out.",
        "",
        "    **Holds.** A member can place up to five holds at once.",
        "",
        rule,
        "",
        "    **Renewals.** A loan renews twice unless another member holds it.",
        "",
    ]
    for i in range(1, 21):
        lines.append(f"    Branch note {i}: opening hours follow the branch calendar.")
    lines += [
        "",
        "    **Accounts.** " + STALE.strip(),
        "",
        "    **Cards.** A lost card is replaced at the front desk.",
        "paths:",
        "  /loans:",
        "    get:",
        "      summary: List loans",
    ]
    return lines


HEAD = _head_lines(NEW_RULE)
BASE = _head_lines(OLD_RULE)
RULE_LINE = HEAD.index(NEW_RULE) + 1
STALE_LINE = next(i for i, ln in enumerate(HEAD, 1) if STALE.strip() in ln)


def _diff() -> str:
    start = RULE_LINE - 3
    body = [" " + ln for ln in HEAD[start - 1:RULE_LINE - 1]]
    body += ["-" + OLD_RULE, "+" + NEW_RULE]
    body += [" " + ln for ln in HEAD[RULE_LINE:RULE_LINE + 3]]
    return "\n".join([
        f"diff --git a/{PATH} b/{PATH}",
        f"--- a/{PATH}",
        f"+++ b/{PATH}",
        f"@@ -{start},7 +{start},7 @@",
        *body,
    ]) + "\n"


DIFF = _diff()
HEAD_TEXT = "\n".join(HEAD) + "\n"


def test_the_fixture_keeps_the_stale_line_out_of_the_hunk():
    (f,) = parse_unified_diff(DIFF)
    covered = {ln.new_line for h in f.hunks for ln in h.lines if ln.new_line is not None}
    assert RULE_LINE in covered
    assert STALE_LINE not in covered
    assert STALE_LINE - max(covered) > 20
    assert all(STALE.strip() not in ln.text for h in f.hunks for ln in h.lines)


def test_the_probe_returns_the_stale_line_with_its_number():
    files = chunk_context.chunk_files(parse_unified_diff(DIFF))
    rows = same_file_restatements(files, lambda p: HEAD_TEXT if p == PATH else None)
    assert f"{PATH}:{STALE_LINE}: **Accounts.** {STALE.strip()}" in rows
    assert all(not r.startswith(f"{PATH}:{RULE_LINE}:") for r in rows)
    assert not any("Branch note" in r for r in rows)


def test_document_paths():
    assert is_document_path("docs/guide.md")
    assert is_document_path("README.rst")
    assert is_document_path("api/openapi/loans.json")
    assert is_document_path(PATH, HEAD_TEXT)
    assert not is_document_path(PATH, "replicas: 3\n")
    assert not is_document_path("src/app.py", HEAD_TEXT)


def test_code_files_get_no_rows():
    entry = ChunkFile(path="src/loans.py", added=("late returns never block the account",))
    text = "# late returns only add a fee to the account\n"
    assert same_file_restatements([entry], lambda p: text) == []


def test_no_rows_without_a_read():
    files = chunk_context.chunk_files(parse_unified_diff(DIFF))
    assert same_file_restatements(files, lambda p: None) == []


def test_char_cap_adds_an_omitted_row():
    entry = ChunkFile(path="a.md", added=("late return blocks account",), added_lines=frozenset({1}))
    text = "late return blocks account\n" + "".join(
        f"late return blocks account, note {i}\n" for i in range(10)
    )
    rows = same_file_restatements([entry], lambda p: text, max_chars=80)
    assert rows[-1].startswith("… ") and rows[-1].endswith("more lines omitted")
    assert len(rows) < 11


class _ReadingForge(FakeForge):
    def __init__(self, diff: str, files: dict[str, str]):
        super().__init__(diff=diff)
        self.files = files

    def get_file_content(self, ref, path, *, sha):
        return self.files.get(path)


class _LLM:
    """Records prompts; every worker prompt for the spec chunk answers with the expected Medium."""

    def __init__(self):
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        with self._lock:
            self.calls.append(user)
        findings = []
        if f"diff --git a/{PATH} " in user and f"{PATH}:{STALE_LINE}:" in user:
            findings = [{
                "file": PATH, "line": RULE_LINE, "severity": "warning", "confidence": 0.85,
                "title": "Old late-return rule still stated elsewhere in the description",
                "body": (
                    "This paragraph now says a closed-branch book blocks the account, "
                    f"but `{PATH}:{STALE_LINE}` still says late returns only add a fee "
                    "and the account stays open. Add the exception there too."
                ),
            }]
        return InvokeResult(
            text=json.dumps({"findings": findings}), input_tokens=10, output_tokens=5,
            model="fake-model", backend="fake", elapsed_ms=1,
        )


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    monkeypatch.setattr(orchestrator, "_elapsed_ms", lambda t0: 0)


def _review(files: dict[str, str]):
    llm = _LLM()
    res = orchestrator.orchestrate_review(
        _ReadingForge(DIFF, files), REF, llm, post=False, ci_wiring="off",
    )
    return res, llm


def test_the_worker_prompt_shows_the_stale_line_and_the_finding_survives():
    res, llm = _review({PATH: HEAD_TEXT})
    worker = [u for u in llm.calls if f"diff --git a/{PATH} " in u]
    assert worker, "no worker prompt carried the spec chunk"
    prompt = worker[0]
    assert RESTATEMENT_HEADER in prompt and RESTATEMENT_GUIDANCE in prompt
    assert f"{PATH}:{STALE_LINE}: **Accounts.** {STALE.strip()}" in prompt
    active = res["findings_active"]
    assert [(f.file, f.line) for f in active] == [(PATH, RULE_LINE)]
    assert f"{PATH}:{STALE_LINE}" in active[0].body


def test_control_without_a_reader_the_stale_line_is_not_shown():
    res, llm = _review({})
    worker = [u for u in llm.calls if f"diff --git a/{PATH} " in u]
    assert worker
    assert RESTATEMENT_HEADER not in worker[0]
    assert STALE.strip() not in worker[0]
    assert res["findings_active"] == []


class _StaleAnchorLLM(_LLM):
    """Answers like the live run did: the finding anchored on the STALE line, outside the diff."""

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        with self._lock:
            self.calls.append(user)
        findings = []
        if f"diff --git a/{PATH} " in user and f"{PATH}:{STALE_LINE}:" in user:
            findings = [{
                "file": PATH, "line": STALE_LINE, "severity": "warning", "confidence": 0.85,
                "title": "Accounts section still says late returns never close the account",
                "body": (
                    "The changed line now says a closed-branch book blocks the account. "
                    "This paragraph still says \"Late returns only add a fee; the account "
                    "stays open.\" and needs the same update."
                ),
            }]
        return InvokeResult(
            text=json.dumps({"findings": findings}), input_tokens=10, output_tokens=5,
            model="fake-model", backend="fake", elapsed_ms=1,
        )


def test_a_finding_anchored_on_the_stale_line_posts_inline_on_the_changed_line():
    """Follow-up to #182: GitHub only takes inline comments on diff lines, so line 35 was rejected."""
    forge = _ReadingForge(DIFF, {PATH: HEAD_TEXT})
    res = orchestrator.orchestrate_review(forge, REF, _StaleAnchorLLM(), post=True, ci_wiring="off")
    (finding,) = res["findings_active"]
    assert (finding.file, finding.line) == (PATH, RULE_LINE)
    assert f"{PATH}:{STALE_LINE}" in finding.body
    (batch,) = forge.inline_batches
    assert [(c.path, c.line) for c in batch] == [(PATH, RULE_LINE)]
    assert f"{PATH}:{STALE_LINE}" in batch[0].body


def _finding(path: str, line: int, body: str = "stale rule") -> Finding:
    return Finding(
        file=path, line=line, severity="warning", confidence=0.8, title="t", body=body,
    )


def _reanchor(findings, files_text=None):
    files = parse_unified_diff(DIFF)
    text = {PATH: HEAD_TEXT} if files_text is None else files_text
    return apply_restatement_anchor(findings, files, read=text.get)


def test_reanchor_adds_the_citation_once():
    (moved,) = _reanchor([_finding(PATH, STALE_LINE)])
    assert moved.line == RULE_LINE
    assert moved.body == f"stale rule (Unchanged text: `{PATH}:{STALE_LINE}`.)"
    cited = f"see `{PATH}:{STALE_LINE}`"
    (kept,) = _reanchor([_finding(PATH, STALE_LINE, cited)])
    assert (kept.line, kept.body) == (RULE_LINE, cited)


def test_reanchor_leaves_everything_else_alone():
    inside = _finding(PATH, RULE_LINE - 1)
    file_level = _finding(PATH, 0)
    unrelated = _finding(PATH, STALE_LINE + 2)
    dropped = replace(_finding(PATH, STALE_LINE), drop_reason="x")
    findings = [inside, file_level, unrelated, dropped]
    assert _reanchor(findings) == findings
    assert _reanchor([_finding(PATH, STALE_LINE)], {}) == [_finding(PATH, STALE_LINE)]
    code = _finding(PATH, STALE_LINE)
    assert _reanchor([code], {PATH: "replicas: 3\n" * 40}) == [code]
    assert apply_restatement_anchor([code], parse_unified_diff(DIFF), read=None) == [code]
