r"""Off-identity golden test for issue #22 part 1, the context follow-up.

With ``PRXREF_CONTEXT_FOLLOWUP`` unset or ``off``, a run must be byte-identical
to 0.17.0: the same requests, in the same shape, and the same
``--format json`` payload (apart from ``elapsed_ms``, which is a clock
reading). This runs the production local path, ``cli._run_review``, over
``tests/fixtures/issue22`` at ``PRXREF_REPO_CONTEXT=repo`` (the only level the
follow-up can ever activate at, per the 0.18.0 design), against a scripted
server that answers every worker chunk prompt with one sub-floor question
finding (a backticked ``StateStore``, confidence 0.5) and the systemic sweep
with no findings — exactly the shape the follow-up would key off of, if it
existed on this code. At 43ec560 the ``PRXREF_CONTEXT_FOLLOWUP`` key does not
exist yet: ``load_config`` does not read it, so setting it in the environment
has no effect and the run cannot tell it apart from being unset. That is what
this test is proving.

The request count and the sorted sha256 of each request's ``messages`` are
recorded here as literals, captured from a real run at 43ec560 (see the class
docstring below for the literals and how they were produced). The JSON
payload (minus ``elapsed_ms``) is compared between the unset run and the
``=off`` run directly, rather than against a third copy of the same literal,
so the two configurations are asserted equal to each other as well as to the
recorded shape.

One value was re-recorded at 0.20.0: issue #29 gives this diff-file plus
repo-dir replay chunk context, so one worker prompt gains exactly the
definition line ``assistant/engine.py:12: class Step:``. Its recorded sha
changed from ``8e0aa4c7...`` to ``92ed30e3...``; every other value is still
the 0.17.0 recording. ``test_the_29_delta_is_exactly_one_definition_line``
pins that delta: the prompt holds the line, and with the line removed it
hashes to the 0.17.0 value again.

Both worker request shas (and the #29 pair with them) were re-recorded again
for issue #67's "Matching rules" section in worker.md: stripping that one
section from each worker system message reproduces the previous recording
(``575f52b8...`` and ``92ed30e3...``) byte for byte. The sweep request sha is
still the 0.17.0 recording.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from prxref import cli
from tests.test_integration import MockOpenAIServer, _completion

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issue22"
PATCH = FIXTURE / "pr.patch"
REPO = FIXTURE / "repo"
DESCRIPTION = FIXTURE / "desc.md"

QUESTION_FINDING = {
    "file": "assistant/progress.py",
    "line": 47,
    "severity": "warning",
    "confidence": 0.5,
    "title": "Does `StateStore` already hold ledger data at this point?",
    "body": (
        "This reads `data.get(LEDGER_KEY)` from the run's root data. Whether that key "
        "is already populated depends on `StateStore`'s save path, which is not shown "
        "in this chunk."
    ),
}
WORKER_CONTENT = json.dumps({"findings": [QUESTION_FINDING]})
SWEEP_CONTENT = json.dumps({"findings": []})

# Recorded at 43ec560 with PRXREF_REPO_CONTEXT=repo, PRXREF_CONTEXT_FOLLOWUP
# unset, over tests/fixtures/issue22/pr.patch against the route below.
EXPECTED_REQUEST_COUNT = 3
PRE_29_SHA = "fb4c02e61c303d5abf4a5b434850848c79e839a76d65f420524750ce3992dd84"
POST_29_SHA = "1d558d0313e2c3c603958f8137a43924da82502c71763e59b8e813128a11659c"
ISSUE_29_LINE = "assistant/engine.py:12: class Step:\n"
EXPECTED_MESSAGE_SHAS = [
    # The sweep request: still the 0.17.0 recording.
    "058306f7b8a3cf8f8e744a6393745bd31c124805790530e0e00314afdac7b1aa",
    # Both worker request shas were re-recorded at the issue #67 worker.md
    # "Matching rules" section (was 575f52b8... and, for the annotated one
    # below, 92ed30e3...): stripping that one section from each worker system
    # message reproduces the earlier recording byte for byte.
    # Re-recorded at 0.20.0: issue #29 gives this diff-file plus repo-dir
    # replay chunk context, so this prompt gains exactly
    # ``assistant/engine.py:12: class Step:``; it was PRE_29_SHA at 0.17.0.
    POST_29_SHA,
    "74b7798451fd51d57c14e691aaf9ac432e9f8df9cd0585a9d38ef18be4c89e2e",
]


def _route(payload: dict) -> tuple[int, dict]:
    """Sub-floor question finding for a worker chunk; no findings for the sweep."""
    system = next(
        (m["content"] for m in payload.get("messages", []) if m.get("role") == "system"), ""
    )
    content = SWEEP_CONTENT if "systemic sweep" in system else WORKER_CONTENT
    return 200, _completion(content, "stop")


def _messages_sha(request: dict) -> str:
    blob = json.dumps(request["payload"]["messages"], sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@pytest.fixture(scope="module")
def llm_server():
    server = MockOpenAIServer(routes={"*": _route})
    base_url = server.start()
    yield server, base_url
    server.stop()


@pytest.fixture
def review(monkeypatch, llm_server):
    """Run one local review over the issue22 fixture; returns (payload, requests)."""
    server, base_url = llm_server

    def _review(followup_env: dict) -> tuple[dict, list[dict]]:
        server.requests.clear()
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "openai-compat")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", base_url)
        monkeypatch.setenv("PRXREF_LLM_MODELS", "fast")
        monkeypatch.setenv("PRXREF_REPO_CONTEXT", "repo")
        for key, value in followup_env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        result = cli._run_review(
            None, diff_file=str(PATCH), repo_dir=str(REPO), description_file=str(DESCRIPTION),
        )
        payload = json.loads(json.dumps(cli._build_json_result(result)))
        return payload, list(server.requests)

    return _review


def _normalize(payload: dict) -> dict:
    """The payload with ``elapsed_ms`` dropped and ``context_followup`` popped.

    ``context_followup`` does not exist in the payload at 43ec560. Landing
    the config key adds it as an always-``None``-when-off key; popping it
    with a default of ``None`` tolerates both its absence here and its
    presence, as long as its value is ``None`` either way.
    """
    payload = dict(payload)
    payload.pop("elapsed_ms", None)
    followup = payload.pop("context_followup", None)
    assert followup is None
    return payload


class TestOffIdentity:
    """Unset and ``=off`` must both match the shape recorded at 43ec560.

    Literals were captured by running, at 43ec560, with the environment set
    exactly as the ``unset`` case below (``PRXREF_CONTEXT_FOLLOWUP`` never
    set) and printing ``len(requests)`` and the sorted list of
    ``_messages_sha(r) for r in requests``.
    """

    def test_unset_matches_recorded_shape(self, review):
        payload, requests = review({"PRXREF_CONTEXT_FOLLOWUP": None})
        assert len(requests) == EXPECTED_REQUEST_COUNT
        assert sorted(_messages_sha(r) for r in requests) == EXPECTED_MESSAGE_SHAS
        _normalize(payload)

    def test_off_matches_recorded_shape(self, review):
        payload, requests = review({"PRXREF_CONTEXT_FOLLOWUP": "off"})
        assert len(requests) == EXPECTED_REQUEST_COUNT
        assert sorted(_messages_sha(r) for r in requests) == EXPECTED_MESSAGE_SHAS
        _normalize(payload)

    def test_unset_and_off_give_the_identical_payload(self, review):
        payload_unset, requests_unset = review({"PRXREF_CONTEXT_FOLLOWUP": None})
        payload_off, requests_off = review({"PRXREF_CONTEXT_FOLLOWUP": "off"})
        assert _normalize(payload_unset) == _normalize(payload_off)
        assert sorted(_messages_sha(r) for r in requests_unset) == sorted(
            _messages_sha(r) for r in requests_off
        )

    def test_the_29_delta_is_exactly_one_definition_line(self, review):
        """Issue #29 adds one same-file definition line; without it the prompt is the 0.17.0 recording."""
        _, requests = review({"PRXREF_CONTEXT_FOLLOWUP": None})
        (changed,) = [r for r in requests if _messages_sha(r) == POST_29_SHA]
        messages = changed["payload"]["messages"]
        holding = [i for i, m in enumerate(messages) if ISSUE_29_LINE in m["content"]]
        assert len(holding) == 1
        assert messages[holding[0]]["content"].count(ISSUE_29_LINE) == 1
        stripped = [dict(m) for m in messages]
        stripped[holding[0]]["content"] = stripped[holding[0]]["content"].replace(ISSUE_29_LINE, "")
        blob = json.dumps(stripped, sort_keys=True).encode("utf-8")
        assert hashlib.sha256(blob).hexdigest() == PRE_29_SHA

    def test_the_key_is_absent_or_none(self, review):
        """At 43ec560 the run.json config has no such key at all; once it lands it is None when off."""
        payload, _ = review({"PRXREF_CONTEXT_FOLLOWUP": None})
        assert payload.get("context_followup") is None
