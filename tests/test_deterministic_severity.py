"""A deterministic finding keeps its own severity, end to end.

Issue #22's fixture, reviewed through the production local path
(``cli._run_review`` with ``--diff-file``, ``--repo-dir`` and
``--description-file``), against a local scripted openai-compat server. The
model answers the one prompt that holds ``assistant/progress.py``'s diff with
an ``error`` finding in that file whose body names
``ASSISTANT_PROGRESS_NOTES``, the rare code token the pinned-off toggle
finding's body also carries, and answers every other prompt with no
finding. That is the shape the live check saw posting the toggle as an
``error``. The payload asserted on is the one ``--format json`` prints.
Nothing leaves localhost.
"""
from __future__ import annotations

import json

import pytest

from prxref import cli, heuristics
from tests.test_integration import MockOpenAIServer, _completion
from tests.test_issue_22_acceptance import DESCRIPTION, PATCH, PROGRESS, REPO, TOGGLE_LINE, TOGGLE_TITLE

MODEL_TITLE = "Progress notes vanish when the notes flag is off"
MODEL_FINDING = {
    "file": PROGRESS,
    "line": 37,
    "severity": "error",
    "confidence": 0.9,
    "title": MODEL_TITLE,
    "body": "`announce` returns early whenever `ASSISTANT_PROGRESS_NOTES` is false, so no note is stored.",
}
NO_FINDINGS = json.dumps({"findings": []})


def _holds_progress_diff(payload: dict) -> bool:
    """True when the request's last user prompt holds ``assistant/progress.py``'s own diff."""
    user = [message["content"] for message in payload["messages"] if message["role"] == "user"][-1]
    return f"diff --git a/{PROGRESS} " in user


def _answer(payload: dict) -> tuple[int, dict]:
    """The model finding for the prompt holding the progress.py diff, no finding for any other."""
    if _holds_progress_diff(payload):
        return 200, _completion(json.dumps({"findings": [MODEL_FINDING]}), "stop")
    return 200, _completion(NO_FINDINGS, "stop")


@pytest.fixture
def review(monkeypatch):
    """Run the fixture review once: the ``--format json`` payload and the requests the server saw."""
    server = MockOpenAIServer(routes={"fast": _answer})
    base_url = server.start()

    def _review() -> tuple[dict, list[dict]]:
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "openai-compat")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", base_url)
        monkeypatch.setenv("PRXREF_LLM_MODELS", "fast")
        monkeypatch.setenv("PRXREF_REPO_CONTEXT", "off")
        result = cli._run_review(
            None, diff_file=str(PATCH), repo_dir=str(REPO), description_file=str(DESCRIPTION),
        )
        return json.loads(json.dumps(cli._build_json_result(result))), list(server.requests)

    yield _review
    server.stop()


def _titled(payload: dict, title: str) -> dict:
    (finding,) = [f for f in payload["findings"] if f["title"] == title]
    return finding


class TestTheToggleKeepsItsSeverity:
    def test_the_toggle_stays_a_warning_beside_the_models_error(self, review):
        payload, requests = review()
        assert len([r for r in requests if _holds_progress_diff(r["payload"])]) == 1
        toggle = _titled(payload, TOGGLE_TITLE)
        assert (toggle["file"], toggle["line"]) == (PROGRESS, TOGGLE_LINE)
        assert (toggle["severity"], toggle["confidence"]) == ("warning", 1.0)
        assert toggle["drop_reason"] is None
        assert toggle["body"].endswith(heuristics._BODY_SUFFIX)
        model = _titled(payload, MODEL_TITLE)
        assert model["file"] == PROGRESS
        assert (model["severity"], model["drop_reason"]) == ("error", None)

    def test_control_without_the_exemption_the_toggle_is_raised_to_error(self, review, monkeypatch):
        monkeypatch.setattr(heuristics, "is_deterministic", lambda finding: False)
        payload, _ = review()
        toggle = _titled(payload, TOGGLE_TITLE)
        assert (toggle["file"], toggle["line"]) == (PROGRESS, TOGGLE_LINE)
        assert (toggle["severity"], toggle["drop_reason"]) == ("error", None)
        assert _titled(payload, MODEL_TITLE)["severity"] == "error"
