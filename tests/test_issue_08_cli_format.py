"""Eval for issue 08: --no-post prints only the verdict, and there is no
machine-readable output at all.

Mirrors ``tests/test_cli.py``'s ``fake_runtime`` stubbing style (fake
``prxref.orchestrator`` / ``prxref.llm_backends`` modules installed via
monkeypatch) rather than importing that fixture, so this file is
self-contained and the crafted result dict — real ``Finding`` objects with
title/body/drop_reason — is local to the two tests that need it.

Test A and Test B are expected to FAIL against today's ``src/prxref/cli.py``:
  * Test A: ``review`` has no ``--format`` flag, so argparse raises
    ``SystemExit(2)`` before any output happens.
  * Test B: text mode with ``--no-post`` prints only ``verdict:`` — no
    finding titles, bodies, or the dropped-findings section.
Both pass once the FROZEN CLI CONTRACT (see the issue file) is implemented.
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from prxref.cli import main
from prxref.forges.base import PRRef
from prxref.triage import Finding

REF = PRRef(
    forge="github",
    host="github.com",
    owner="org",
    repo="repo",
    number=7,
    url="https://github.com/org/repo/pull/7",
)
URL = "https://github.com/org/repo/pull/7"

ACTIVE = Finding(
    file="src/foo.py",
    line=42,
    severity="error",
    confidence=0.92,
    title="Off-by-one in loop bound",
    body="The loop uses `<=` where `<` was intended, causing an out-of-bounds read.",
    drop_reason=None,
)
DROPPED = Finding(
    file="src/bar.py",
    line=10,
    severity="warning",
    confidence=0.40,
    title="Possible unused import",
    body="`os` appears unused after this change.",
    drop_reason="confidence 0.40 below floor 0.60",
)

RESULT = {
    "verdict": "Request-Changes",
    "findings_active": [ACTIVE],
    "findings_dropped": [DROPPED],
    "chunk_count": 3,
    "chunks_reviewed": 3,
    "chunks_failed": 0,
    "elapsed_ms": 1234,
    "input_tokens": 500,
    "output_tokens": 120,
    "posted": False,
}


def _install_fake_module(monkeypatch, fullname: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(fullname)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, fullname, mod)
    return mod


@pytest.fixture
def stub_review(monkeypatch):
    """Installs a fake orchestrator returning RESULT (one active, one dropped
    finding), and a fake llm_backends, exactly as fake_runtime does in
    tests/test_cli.py."""
    monkeypatch.setattr("prxref.cli.detect_forge", lambda url: REF)
    _install_fake_module(
        monkeypatch,
        "prxref.llm_backends",
        create_llm_client=lambda cfg: object(),
    )
    _install_fake_module(
        monkeypatch,
        "prxref.orchestrator",
        orchestrate_review=lambda **kwargs: RESULT,
    )


def test_format_json_emits_one_object_with_active_and_dropped_findings(
    stub_review, capsys
):
    """Test A (must FAIL now). Today ``--format`` does not exist: argparse
    rejects it with a SystemExit(2) usage error before anything is printed.
    That failure is asserted explicitly below and then turned into a clean
    pytest.fail so the test reads as a FAIL rather than an uncaught
    SystemExit; once ``--format`` lands the except branch is never reached
    and the real assertions (the fixed behaviour) run instead.
    """
    try:
        rc = main([
            "review", "--pr-url", URL, "--no-post", "--format", "json",
        ])
    except SystemExit as exc:
        assert exc.code == 2, f"expected today's usage-error exit 2, got {exc.code}"
        pytest.fail(
            "review --format json not implemented yet: argparse rejected "
            "--format with SystemExit(2) (issue 08 / FROZEN CLI CONTRACT)"
        )
        return

    assert rc == 0
    out, err = capsys.readouterr()
    # "exactly ONE JSON object to stdout and nothing else on stdout" — a
    # stray print anywhere in stdout breaks this parse.
    payload = json.loads(out.strip())

    assert payload["verdict"] == "Request-Changes"
    assert payload["chunk_count"] == 3

    findings = payload["findings"]
    assert len(findings) == 2
    active_row, dropped_row = findings[0], findings[1]

    assert active_row["file"] == "src/foo.py"
    assert active_row["line"] == 42
    assert active_row["severity"] == "error"
    assert active_row["confidence"] == pytest.approx(0.92)
    assert active_row["title"] == "Off-by-one in loop bound"
    assert active_row["body"] == ACTIVE.body
    assert active_row["drop_reason"] is None

    assert dropped_row["file"] == "src/bar.py"
    assert dropped_row["line"] == 10
    assert dropped_row["title"] == "Possible unused import"
    assert dropped_row["drop_reason"] == "confidence 0.40 below floor 0.60"


def test_no_post_text_mode_prints_finding_bodies_and_dropped_reasons(
    stub_review, capsys
):
    """Test B (must FAIL now). Today ``_print_summary`` never renders finding
    titles, bodies, or the dropped section in any mode — with ``--no-post``
    they are unreachable without importing ``prxref.cli._run_review``
    directly, which is exactly the bug the issue reports."""
    rc = main(["review", "--pr-url", URL, "--no-post"])
    assert rc == 0
    out, _ = capsys.readouterr()

    assert "error src/foo.py:42 Off-by-one in loop bound (confidence 0.92)" in out
    assert "  " + ACTIVE.body in out
    assert "dropped:" in out
    assert "  src/bar.py:10 Possible unused import -- confidence 0.40 below floor 0.60" in out


def test_control_verdict_and_counts_lines_still_appear_in_text_mode(
    stub_review, capsys
):
    """Control (must PASS now and after): the pre-existing verbose summary
    lines are not regressed by the fix."""
    rc = main(["review", "--pr-url", URL, "--no-post", "-v"])
    assert rc == 0
    out, _ = capsys.readouterr()
    assert "verdict: Request-Changes" in out
    assert "counts:" in out


def test_control_no_bodies_without_no_post_or_verbose(stub_review, capsys):
    """Control (must PASS now and after): the default (posting) invocation,
    with neither --no-post nor -v, prints only the verdict line — no finding
    titles or bodies leak into plain default-mode stdout."""
    rc = main(["review", "--pr-url", URL])
    assert rc == 0
    out, _ = capsys.readouterr()
    assert "verdict: Request-Changes" in out
    assert ACTIVE.title not in out
    assert ACTIVE.body not in out
    assert DROPPED.title not in out
