r"""Issue #22 acceptance for the opt-in context follow-up, end to end.

Every review here runs the production command, ``prxref review`` with
``--diff-file``, ``--repo-dir``, ``--description-file``, ``--format json`` and
``--trace-dir``, over ``tests/fixtures/issue22`` (see
``tests/test_issue_22_acceptance.py`` for how the fixture is generated).
``PRXREF_REPO_CONTEXT=repo`` and ``PRXREF_CONTEXT_FOLLOWUP=on`` are set in the
environment, as a user sets them. The LLM is the real openai-compat HTTP client
talking to a local scripted server that records every request:

- the systemic sweep answers no findings;
- the first worker prompt of the chunk holding ``assistant/progress.py``
  answers the issue's own dropped question (confidence 0.5, below the default
  floor of 0.6), every other first worker prompt answers no findings;
- a worker prompt that carries the follow-up block answers per case.

The payload asserted on is the one ``--format json`` prints. Nothing leaves
localhost.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from prxref import cli, repo_followup
from prxref.followup_merge import UNCONFIRMED_PREFIX
from prxref.repo_followup import FOLLOWUP_HEADER
from tests.test_integration import MockOpenAIServer, _completion

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issue22"
PATCH = FIXTURE / "pr.patch"
REPO = FIXTURE / "repo"
DESCRIPTION = FIXTURE / "desc.md"

PROGRESS = "assistant/progress.py"
STATE_STORE = "assistant/state_store.py"
SWEEP_MARK = "running a systemic sweep"
PROGRESS_DIFF = f"diff --git a/{PROGRESS} "
OUTPUT_HEADER = "## Output Format"

R2_BODY = (
    "`_ledger` puts a live `ProgressLedger` object into `run.root().data`. `Engine.run_turn` passes that "
    "same `run` to `self.store.save(run)` on handoff. `StateStore` is not shown here. Does it serialize "
    "frame data in a way that fails on arbitrary objects, e.g. JSON? The conftest turns the feature off for "
    "every existing test, so save/resume with the default-on setting looks untested."
)
R2_TITLE = "Progress ledger in frame data may break state serialization"
QUESTION = {
    "file": PROGRESS, "line": 47, "severity": "warning", "confidence": 0.5,
    "title": R2_TITLE, "body": R2_BODY,
}
CONFIRMATION = {
    "file": PROGRESS, "line": 47, "severity": "error", "confidence": 0.9,
    "title": "Progress ledger breaks StateStore.save",
    "body": (
        "`StateStore.save` calls `json.dumps(asdict(run))`, which raises `TypeError` on the live "
        "`ProgressLedger` the PR stores in the root frame's data."
    ),
}
ELSEWHERE = {
    "file": STATE_STORE, "line": 13, "severity": "error", "confidence": 0.9,
    "title": "save cannot serialize arbitrary frame data",
    "body": "`json.dumps(asdict(run))` raises on any object that is not JSON-serializable.",
}
PLAIN_QUESTION = {
    "file": PROGRESS, "line": 40, "severity": "warning", "confidence": 0.5,
    "title": "Progress notes may push real turns out of the model history",
    "body": (
        "Every announce appends one more progress row for the session. If history.py builds "
        "model_history from table.recent(session_id, HISTORY_WINDOW), these rows count against "
        "the window and can evict user and assistant turns. That code is not shown here."
    ),
}
PLAIN_CONFIRMATION = {
    "file": PROGRESS, "line": 40, "severity": "error", "confidence": 0.9,
    "title": "Progress notes evict turns from the model history",
    "body": (
        "model_history reads table.recent(session_id, HISTORY_WINDOW), which counts progress rows, "
        "so each announce pushes one real turn out of the window."
    ),
}
FLOOR_REASON = "confidence 0.50 below floor 0.60"
UNCONFIRMED_REASON = f"{UNCONFIRMED_PREFIX} (confidence 0.50 below floor 0.60)"


def _findings(*findings: dict) -> tuple[int, dict]:
    return 200, _completion(json.dumps({"findings": list(findings)}), "stop")


FOLLOWUP_REPLIES = {
    "confirm": _findings(CONFIRMATION),
    "refute": _findings(),
    "http-500": (500, {"error": "error 500"}),
    "not-json": (200, _completion("I could not decide; the definition looks fine to me.", "stop")),
    "another-file": _findings(ELSEWHERE),
    "confirm-plain": _findings(PLAIN_CONFIRMATION),
}


def _messages(payload: dict, role: str) -> str:
    return [m["content"] for m in payload.get("messages", []) if m.get("role") == role][-1]


class _Route:
    """The scripted server's callable route; ``followup`` selects the follow-up reply.

    ``question`` is the first reply's question on ``assistant/progress.py``.
    """

    def __init__(self) -> None:
        self.followup = "refute"
        self.question = QUESTION

    def __call__(self, payload: dict) -> tuple[int, dict]:
        system = _messages(payload, "system")
        user = _messages(payload, "user")
        if SWEEP_MARK in system:
            return _findings()
        if FOLLOWUP_HEADER in user:
            return FOLLOWUP_REPLIES[self.followup]
        if PROGRESS_DIFF in user:
            return _findings(self.question)
        return _findings()


@pytest.fixture(scope="module")
def llm_server():
    route = _Route()
    server = MockOpenAIServer(routes={"*": route})
    base_url = server.start()
    yield server, base_url, route
    server.stop()


class Run:
    """One finished review: the exit code, the JSON payload, every request, the trace dir."""

    def __init__(self, code: int, payload: dict, requests: list[dict], trace_dir: Path) -> None:
        self.code = code
        self.payload = payload
        self.requests = requests
        self.trace_dir = trace_dir

    def kind(self, request: dict) -> str:
        system = _messages(request["payload"], "system")
        user = _messages(request["payload"], "user")
        if SWEEP_MARK in system:
            return "sweep"
        return "followup" if FOLLOWUP_HEADER in user else "worker"

    def of_kind(self, kind: str) -> list[dict]:
        return [r for r in self.requests if self.kind(r) == kind]

    def progress_first(self) -> dict:
        owners = [r for r in self.of_kind("worker") if PROGRESS_DIFF in _messages(r["payload"], "user")]
        assert len(owners) == 1
        return owners[0]

    def record(self) -> dict:
        return self.payload["context_followup"]

    def row(self) -> dict:
        rows = [row for row in self.record()["chunks"] if row is not None and row["questions"]]
        assert len(rows) == 1
        return rows[0]

    def at(self, path: str, line: int) -> list[dict]:
        return [f for f in self.payload["findings"] if (f["file"], f["line"]) == (path, line)]


@pytest.fixture
def review(monkeypatch, llm_server, tmp_path, capsys):
    """Run ``prxref review`` once with a follow-up reply; returns a :class:`Run`."""
    server, base_url, route = llm_server

    def _review(followup: str, *, context_followup: str = "on") -> Run:
        server.requests.clear()
        route.followup = followup
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "openai-compat")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", base_url)
        monkeypatch.setenv("PRXREF_LLM_MODELS", "fast")
        monkeypatch.setenv("PRXREF_REPO_CONTEXT", "repo")
        monkeypatch.setenv("PRXREF_CONTEXT_FOLLOWUP", context_followup)
        monkeypatch.delenv("PRXREF_CONFIDENCE_FLOOR", raising=False)
        monkeypatch.delenv("PRXREF_FAIL_ON", raising=False)
        trace_dir = tmp_path / f"trace-{followup}-{context_followup}"
        capsys.readouterr()
        code = cli.main([
            "review", "--diff-file", str(PATCH), "--repo-dir", str(REPO),
            "--description-file", str(DESCRIPTION), "--format", "json", "--trace-dir", str(trace_dir),
        ])
        payload = json.loads(capsys.readouterr().out)
        return Run(code, payload, list(server.requests), trace_dir)

    return _review


class TestConfirm:
    """The looked-up ``StateStore`` body turns the dropped question into an active error."""

    def test_the_question_becomes_one_active_error(self, review):
        run = review("confirm")
        assert run.code == 0
        assert run.payload["verdict"] == "Request-Changes"
        errors = [f for f in run.payload["findings"] if f["severity"] == "error" and f["drop_reason"] is None]
        assert [(f["file"], f["line"], f["confidence"]) for f in errors] == [(PROGRESS, 47, 0.9)]
        assert "json.dumps(asdict(run))" in errors[0]["body"]
        assert [f["title"] for f in run.at(PROGRESS, 47)] == [CONFIRMATION["title"]]

    def test_the_record_counts_one_call_and_one_confirmation(self, review):
        run = review("confirm")
        record = run.record()
        assert record["active"] is True
        assert (record["calls"], record["confirmed"], record["unconfirmed"], record["discarded"]) == (1, 1, 0, 0)
        row = run.row()
        assert row["called"] is True and row["error"] == "" and row["skipped"] is None
        assert row["names"][0] == "StateStore"
        assert (row["excerpts"][0]["path"], row["excerpts"][0]["line"]) == (STATE_STORE, 8)
        assert row["excerpts"][0]["symbol"] == "StateStore"
        assert record["input_tokens"] == row["input_tokens"] > 0

    def test_three_worker_requests_and_one_sweep(self, review):
        run = review("confirm")
        assert len(run.requests) == 4
        assert len(run.of_kind("worker")) == 2
        assert len(run.of_kind("followup")) == 1
        assert len(run.of_kind("sweep")) == 1

    def test_the_rerun_is_the_first_prompt_with_the_block_appended(self, review):
        run = review("confirm")
        first = run.progress_first()["payload"]
        (rerun,) = [r["payload"] for r in run.of_kind("followup")]
        first_user, rerun_user = _messages(first, "user"), _messages(rerun, "user")
        at = rerun_user.index("\n\n" + FOLLOWUP_HEADER)
        cut = rerun_user[at:at + len(rerun_user) - len(first_user)]
        assert rerun_user == first_user[:at] + cut + first_user[at:]
        assert first_user.replace(cut, "") == first_user
        assert rerun_user.replace(cut, "", 1) == first_user
        assert OUTPUT_HEADER not in cut
        assert OUTPUT_HEADER in first_user[at:]
        block = cut[2:]
        assert block.startswith(FOLLOWUP_HEADER + "\n\n" + repo_followup.FOLLOWUP_NOTE + "\n\n")
        assert f"{STATE_STORE}:8:" in block
        assert "json.dumps(asdict(run))" in block
        assert FOLLOWUP_HEADER not in first_user
        assert _messages(rerun, "system") == _messages(first, "system")
        assert rerun["messages"][:-1] == first["messages"][:-1]
        assert rerun["model"] == first["model"]
        assert rerun.get("max_tokens") == first.get("max_tokens")


class TestRefute:
    def test_an_empty_rerun_leaves_the_question_unconfirmed(self, review):
        run = review("refute")
        assert run.code == 0
        assert run.payload["verdict"] == "Approved"
        (question,) = run.at(PROGRESS, 47)
        assert question["title"] == R2_TITLE
        assert question["drop_reason"] == UNCONFIRMED_REASON
        record = run.record()
        assert (record["calls"], record["confirmed"], record["unconfirmed"], record["discarded"]) == (1, 0, 1, 0)
        assert run.row()["error"] == ""
        assert len(run.of_kind("followup")) == 1


class TestFailure:
    """A failed follow-up keeps the first review: the 0.17 floor reason, a row error, exit 0."""

    @pytest.mark.parametrize("reply", ["http-500", "not-json"])
    def test_a_failed_rerun_keeps_the_first_review(self, review, reply):
        run = review(reply)
        assert run.code == 0
        assert run.payload["verdict"] == "Approved"
        assert run.payload["chunks_failed"] == 0
        (question,) = run.at(PROGRESS, 47)
        assert question["title"] == R2_TITLE
        assert question["drop_reason"] == FLOOR_REASON
        row = run.row()
        assert row["called"] is True
        assert row["error"]
        record = run.record()
        assert (record["confirmed"], record["unconfirmed"], record["discarded"]) == (0, 0, 0)
        assert 1 <= len(run.of_kind("followup")) <= 2
        assert len(run.of_kind("worker")) == 2
        assert len(run.of_kind("sweep")) == 1


class TestAnotherFile:
    def test_a_finding_in_another_file_confirms_nothing(self, review):
        run = review("another-file")
        assert run.code == 0
        assert run.payload["verdict"] == "Approved"
        assert run.at(STATE_STORE, 13) == []
        (question,) = run.at(PROGRESS, 47)
        assert question["drop_reason"] == UNCONFIRMED_REASON
        record = run.record()
        assert (record["calls"], record["confirmed"], record["unconfirmed"], record["discarded"]) == (1, 0, 1, 1)
        row = run.row()
        assert (row["confirmed"], row["unconfirmed"], row["discarded"]) == (0, 1, 1)


class TestTrace:
    def test_the_trace_dir_holds_the_followup_files(self, review):
        run = review("confirm")
        names = {p.name for p in run.trace_dir.iterdir()}
        assert "chunk0.followup.user.md" in names
        assert {"chunk0.followup.system.md", "chunk0.followup.response.json", "chunk0.followup.meta.json"} <= names
        assert "chunk0.user.md" in names
        user = (run.trace_dir / "chunk0.followup.user.md").read_text(encoding="utf-8")
        assert FOLLOWUP_HEADER in user
        assert PROGRESS_DIFF in user
        assert not any(".followup" in n for n in names if not n.startswith("chunk0."))


class TestPlainTextNames:
    """A question that names the code without backticks still gets its follow-up."""

    def test_a_question_without_backticks_is_confirmed(self, review, llm_server, monkeypatch):
        assert "`" not in PLAIN_QUESTION["title"] + PLAIN_QUESTION["body"]
        monkeypatch.setattr(llm_server[2], "question", PLAIN_QUESTION)
        run = review("confirm-plain")
        assert run.code == 0
        record = run.record()
        assert (record["calls"], record["confirmed"], record["unconfirmed"]) == (1, 1, 0)
        assert len(run.of_kind("followup")) == 1
        row = run.row()
        assert row["skipped"] is None and row["error"] == ""
        assert {"model_history", "recent"} & set(row["names"])
        assert "assistant/history.py" in {excerpt["path"] for excerpt in row["excerpts"]}
        assert [f["title"] for f in run.at(PROGRESS, 40)] == [PLAIN_CONFIRMATION["title"]]


class TestControls:
    """The same route with the follow-up off, or with no name to look up, keeps 0.17's outcome."""

    def test_off_makes_no_followup_and_keeps_the_floor_reason(self, review):
        run = review("confirm", context_followup="off")
        assert run.payload["context_followup"] is None
        assert run.payload["verdict"] == "Approved"
        assert run.of_kind("followup") == []
        assert len(run.requests) == 3
        (question,) = run.at(PROGRESS, 47)
        assert question["drop_reason"] == FLOOR_REASON
        assert not any(".followup" in p.name for p in run.trace_dir.iterdir())

    def test_no_lookup_names_means_no_call(self, review, monkeypatch):
        monkeypatch.setattr(repo_followup, "MAX_FOLLOWUP_NAMES", 0)
        run = review("confirm")
        assert run.payload["verdict"] == "Approved"
        assert run.of_kind("followup") == []
        assert run.row()["skipped"] == "no-names"
        assert run.record()["calls"] == 0
        (question,) = run.at(PROGRESS, 47)
        assert question["drop_reason"] == FLOOR_REASON
