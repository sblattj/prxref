"""Issue #69, acceptance 2 (OD11): failing evidence raises deterministic findings.

"Given a failing lint output for a changed file, a finding is raised
citing it." :func:`prxref.evidence.failure_findings` reads every item
with a non-zero exit code, parses the ``path:line`` (or ``path(line,col)``)
positions its output names, keeps the ones whose path is one of the PR's
changed files, and raises one deterministic warning per position, citing
the command and its exit code, at most :data:`FAILURE_FINDINGS_CAP` (10)
per run. :func:`prxref.evidence.drop_restated_failures` drops a model
finding that restates one at the same file and line, so the PR gets one
comment, not two. Pinned here through the pure functions and through the
real orchestrator, the no-chunk path included.
"""
from __future__ import annotations

import json

import pytest

from prxref import orchestrator
from prxref.cli import main
from prxref.evidence import (
    FAILURE_FINDINGS_CAP,
    EvidenceBundle,
    EvidenceItem,
    drop_restated_failures,
    failure_findings,
    load_evidence,
)
from prxref.heuristics import is_deterministic
from prxref.llm import InvokeResult
from prxref.triage import Finding
from tests.test_issue_69_evidence import _install_fake_module
from tests.test_orchestrator import REF, FakeForge, _added_file_diff

PR_PATHS = ("src/app.py", "src/other.py")

LINT = {
    "command": "ruff check src",
    "exit_code": 1,
    "output": "src/app.py:3:1: F401 [*] `os` imported but unused\nFound 1 error.",
}


def _bundle(*entries: dict) -> EvidenceBundle:
    return EvidenceBundle(
        items=tuple(
            EvidenceItem(
                command=e["command"], exit_code=e.get("exit_code", 0),
                output=e.get("output", ""), files=tuple(e.get("files", ())),
            )
            for e in entries
        ),
        files=("run.json",),
    )


def _model(**overrides) -> Finding:
    base = {
        "file": "src/app.py", "line": 3, "severity": "error", "confidence": 0.9,
        "title": "Unused import", "body": "F401: `os` is imported but never used.",
    }
    return Finding(**{**base, **overrides})


class TestFailureFindings:
    def test_a_failing_lint_on_a_changed_file_raises_one_finding_citing_it(self):
        out = failure_findings(_bundle(LINT), PR_PATHS)
        assert len(out) == 1
        finding = out[0]
        assert (finding.file, finding.line) == ("src/app.py", 3)
        assert finding.severity == "warning"
        assert finding.confidence == 1.0
        assert "`ruff check src`" in finding.body
        assert "exit code 1" in finding.body
        assert "F401 [*] `os` imported but unused" in finding.body
        assert "F401" in finding.title
        assert is_deterministic(finding)
        assert finding.drop_reason is None

    def test_a_path_outside_the_pr_raises_nothing(self):
        item = dict(LINT, output="lib/vendor.py:3:1: F401 `os` imported but unused")
        assert failure_findings(_bundle(item), PR_PATHS) == []

    def test_a_passing_item_raises_nothing(self):
        assert failure_findings(_bundle(dict(LINT, exit_code=0)), PR_PATHS) == []

    def test_twelve_failures_give_ten_in_output_order(self):
        output = "\n".join(f"src/app.py:{n}:1: E501 line too long" for n in range(1, 13))
        out = failure_findings(_bundle(dict(LINT, output=output)), PR_PATHS)
        assert FAILURE_FINDINGS_CAP == 10
        assert [f.line for f in out] == list(range(1, 11))

    def test_the_cap_spans_every_item(self):
        first = dict(LINT, output="\n".join(f"src/app.py:{n}: E1 x" for n in range(1, 8)))
        second = dict(LINT, command="mypy src", output="\n".join(
            f"src/other.py:{n}: error: y" for n in range(1, 8)
        ))
        out = failure_findings(_bundle(first, second), PR_PATHS)
        assert len(out) == 10
        assert [f.file for f in out] == ["src/app.py"] * 7 + ["src/other.py"] * 3

    def test_one_position_named_twice_raises_once(self):
        again = dict(LINT, command="flake8 src")
        out = failure_findings(_bundle(LINT, again), PR_PATHS)
        assert len(out) == 1
        assert "`ruff check src`" in out[0].body

    @pytest.mark.parametrize("line", [
        "src/app.py:3: error: Name 'x' is not defined",
        "./src/app.py:3:12: E999 SyntaxError",
        "/home/runner/work/repo/repo/src/app.py:3:1: F401 unused",
        "src/app.py(3,5): error TS2322: wrong type",
        "app.py:3:1: F401 unused",
    ])
    def test_the_position_shapes_linters_print(self, line):
        out = failure_findings(_bundle(dict(LINT, output=line)), PR_PATHS)
        assert [(f.file, f.line) for f in out] == [("src/app.py", 3)]

    def test_an_ambiguous_suffix_raises_nothing(self):
        item = dict(LINT, output="app.py:3:1: F401 unused")
        assert failure_findings(_bundle(item), ("src/app.py", "lib/app.py")) == []

    def test_a_line_zero_or_a_bare_path_raises_nothing(self):
        item = dict(LINT, output="src/app.py:0: odd\nsrc/app.py failed to parse")
        assert failure_findings(_bundle(item), PR_PATHS) == []

    def test_the_command_line_is_not_a_position(self):
        item = dict(LINT, command="pytest src/app.py:3", output="1 failed")
        assert failure_findings(_bundle(item), PR_PATHS) == []

    @pytest.mark.parametrize("evidence", [None, EvidenceBundle()])
    def test_no_evidence_raises_nothing(self, evidence):
        assert failure_findings(evidence, PR_PATHS) == []


class TestDropRestatedFailures:
    def _drop(self, findings, evidence=None):
        return drop_restated_failures(findings, evidence or _bundle(LINT), PR_PATHS)

    def test_a_model_finding_restating_the_failure_is_dropped(self):
        out = self._drop([_model()])
        assert out[0].drop_reason == "restates execution evidence: ruff check src"

    def test_naming_the_tool_is_a_restatement_too(self):
        model = _model(body="ruff flags this import as unused.")
        out = self._drop([model])
        assert out[0].drop_reason is not None

    def test_a_different_claim_on_the_same_line_is_kept(self):
        model = _model(title="SQL injection", body="The query interpolates user input.")
        assert self._drop([model])[0].drop_reason is None

    def test_another_line_is_kept(self):
        assert self._drop([_model(line=9)])[0].drop_reason is None

    def test_deterministic_and_dropped_findings_are_left_alone(self):
        deterministic = _model(body="F401 again (deterministic check, no model)")
        dropped = _model(drop_reason="hedged")
        out = self._drop([deterministic, dropped])
        assert out[0].drop_reason is None
        assert out[1].drop_reason == "hedged"

    def test_one_to_one_and_identity_without_raised_findings(self):
        findings = [_model(), _model(line=9)]
        assert self._drop(findings, _bundle(dict(LINT, exit_code=0))) == findings
        assert drop_restated_failures(findings, None, PR_PATHS) == findings
        out = self._drop(findings)
        assert len(out) == 2 and out[1] is findings[1]


class _LLM:
    def __init__(self, worker_findings=None):
        self.worker_findings = worker_findings or []

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        findings = self.worker_findings if "### Diff" in user else []
        return InvokeResult(
            text=json.dumps({"findings": findings, "escalations": []}),
            input_tokens=10, output_tokens=5, model="rec-model-1",
            backend="fake", elapsed_ms=1,
        )


def _evidence(tmp_path, *entries):
    path = tmp_path / "run.json"
    path.write_text(json.dumps(list(entries)), encoding="utf-8")
    return load_evidence([str(path)], max_chars=120_000, source="--evidence-file")


def _run(evidence, *, diff=None, findings=None, **kw):
    forge = FakeForge(diff=diff if diff is not None else _added_file_diff("src/app.py", 20))
    kw.setdefault("post", False)
    res = orchestrator.orchestrate_review(
        forge, REF, _LLM(findings), evidence=evidence, **kw,
    )
    return forge, res


def _raised(res):
    return [f for f in res["findings_active"] if f.body.startswith("Execution evidence")]


class TestThroughTheOrchestrator:
    def test_a_failing_lint_raises_one_finding_on_the_changed_line(self, tmp_path):
        _forge, res = _run(_evidence(tmp_path, LINT))
        raised = _raised(res)
        assert len(raised) == 1
        assert (raised[0].file, raised[0].line) == ("src/app.py", 3)
        assert "`ruff check src`" in raised[0].body

    def test_a_path_outside_the_pr_raises_nothing(self, tmp_path):
        item = dict(LINT, output="lib/vendor.py:3:1: F401 unused")
        _forge, res = _run(_evidence(tmp_path, item))
        assert _raised(res) == []

    def test_twelve_failures_reach_the_review_as_ten(self, tmp_path):
        output = "\n".join(f"src/app.py:{n}:1: E501 line too long" for n in range(1, 13))
        _forge, res = _run(_evidence(tmp_path, dict(LINT, output=output)))
        assert len(_raised(res)) == 10

    def test_a_model_restatement_is_dropped_and_one_comment_posts(self, tmp_path):
        model = {
            "file": "src/app.py", "line": 3, "severity": "error", "confidence": 0.9,
            "title": "Unused import data", "body": "ruff reports F401 on data 3.",
        }
        forge, res = _run(_evidence(tmp_path, LINT), findings=[model], post=True)
        assert len(_raised(res)) == 1
        assert [f.title for f in res["findings_active"]] == [_raised(res)[0].title]
        assert [f.drop_reason for f in res["findings_dropped"]] == [
            "restates execution evidence: ruff check src",
        ]

    def test_a_trace_event_counts_the_raised_findings(self, tmp_path):
        trace = tmp_path / "run.jsonl"
        _forge, _res = _run(_evidence(tmp_path, LINT), trace_file=str(trace))
        events = [
            json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        raises = [e for e in events if e["node"] == "evidence" and e["phase"] == "raise"]
        assert [e["meta"] for e in raises] == [{"findings": 1, "capped": 0, "restated": 0}]

    def test_without_evidence_nothing_is_raised(self):
        _forge, res = _run(None)
        assert _raised(res) == []

    def test_the_no_chunk_path_raises_them_too(self, tmp_path):
        diff = (
            "diff --git a/assets/logo.png b/assets/logo.png\nindex 111..222 100644\n"
            "Binary files a/assets/logo.png and b/assets/logo.png differ\n"
        )
        item = dict(LINT, command="imagelint assets", output="assets/logo.png:1: bad header")
        _forge, res = _run(_evidence(tmp_path, item), diff=diff)
        assert res["chunk_count"] == 0
        raised = _raised(res)
        assert [(f.file, f.line) for f in raised] == [("assets/logo.png", 1)]


class TestFailOn:
    """A raised warning is an ordinary active finding to ``PRXREF_FAIL_ON``."""

    @pytest.fixture
    def evidence_file(self, monkeypatch, tmp_path):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: REF)
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: forge)
        _install_fake_module(
            monkeypatch, "prxref.llm_backends", create_llm_client=lambda cfg: _LLM(),
        )
        path = tmp_path / "run.json"
        path.write_text(json.dumps([LINT]), encoding="utf-8")
        return str(path)

    @pytest.mark.parametrize(("fail_on", "code"), [("never", 0), ("error", 0), ("any", 1)])
    def test_the_exit_code(self, evidence_file, monkeypatch, fail_on, code):
        monkeypatch.setenv("PRXREF_FAIL_ON", fail_on)
        args = ["review", "--pr-url", REF.url, "--no-post", "--evidence-file", evidence_file]
        assert main(args) == code
