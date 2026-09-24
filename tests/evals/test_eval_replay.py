"""Every eval case replays with one CLI call, offline (issue #65).

Each ``case-*`` directory is reviewed by exactly one ``prxref review`` call:
its diff through ``--diff-file`` (no ``--pr-url``, so no forge at all), its
ticket through ``--context-file`` and its spec corpus through ``--spec``. The
real CLI, orchestrator, reviewer, spec digest and ticket loader run; only the
LLM is a stub that finds nothing, and any HTTP request fails the run. So this
proves the wiring, not the review: the JSON record carries the replay stamp,
every chunk and the sweep are reviewed, and the case's ticket and one of its
spec rules reach every prompt that ``--trace-dir`` writes. Scoring real
findings against ``expected.json`` needs a live model and stays manual (see
README.md, "Running a case").
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from prxref.cli import main
from prxref.llm import InvokeResult
from tests.evals.test_evals import CASE_DIRS


class _NoFindingsLLM:
    """Answers every review unit with no findings, counting the calls."""

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        self.calls += 1
        return InvokeResult(
            text=json.dumps({"findings": [], "escalations": []}),
            model="fake", backend="fake",
        )


def _no_network(*args, **kwargs):
    raise AssertionError("an eval replay must not touch the network")


def _spec_rules(case: Path) -> list[str]:
    """The case's normative lines long enough to be distinctive."""
    return [
        line.strip()
        for doc in sorted((case / "docs").glob("*.md"))
        for line in doc.read_text(encoding="utf-8").splitlines()
        if "MUST" in line and len(line.strip()) >= 30
    ]


@pytest.mark.parametrize("case", CASE_DIRS, ids=[p.name for p in CASE_DIRS])
def test_each_case_replays_with_one_cli_call(case, monkeypatch, capsys, tmp_path):
    llm = _NoFindingsLLM()
    monkeypatch.setenv("PRXREF_LLM_MODELS", "fake")
    monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: llm)
    monkeypatch.setattr("requests.Session.request", _no_network)
    diff, ticket, docs = str(case / "diff.patch"), str(case / "ticket.md"), str(case / "docs")

    rc = main([
        "review", "--diff-file", diff, "--context-file", ticket, "--spec", docs,
        "--no-post", "--format", "json", "--trace-dir", str(tmp_path),
    ])

    out = capsys.readouterr().out
    assert rc == 0
    assert out.endswith("\n") and "\n" not in out[:-1]
    record = json.loads(out)
    assert record["replay"] == {
        "base_sha": None, "head_sha": None, "threads": "hidden", "diff_file": diff,
    }
    assert record["posted"] is False
    assert record["chunks_reviewed"] >= 1
    assert record["chunks_reviewed"] == record["chunk_count"] == llm.calls
    assert record["chunks_failed"] == 0
    assert record["spec_grounding"]["ok"] == record["spec_grounding"]["sources"] == 1
    assert record["ticket_context"]["path"] == ticket

    prompts = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.glob("*.user.md")}
    assert {"chunk0.user.md", "sweep.user.md"} <= set(prompts)
    rules = _spec_rules(case)
    first_ticket_line = next(
        line for line in Path(ticket).read_text(encoding="utf-8").splitlines() if line.strip()
    )
    assert rules
    for name, prompt in prompts.items():
        assert any(rule in prompt for rule in rules), f"no spec rule of {case.name} in {name}"
        assert "### Ticket context" in prompt, name
        assert first_ticket_line in prompt, f"the ticket of {case.name} is not in {name}"
