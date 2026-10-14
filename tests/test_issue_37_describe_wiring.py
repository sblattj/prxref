"""PR auto-description and auto-labels wired through ``orchestrate_review`` (#37).

End to end: a fake forge with the optional write methods, a fake LLM that
answers the describe prompt and the review prompts differently, and the REAL
reviewer and describe module. Each acceptance box of issue #37 has a test
here that runs the orchestrator entry, not the unit functions alone.
"""
from __future__ import annotations

import json
import threading
import types
from datetime import UTC, datetime

import pytest

from prxref import describe, orchestrator
from prxref.cli import _build_json_result
from prxref.forges.base import FeedReadError, PRData
from prxref.gate import worker
from prxref.llm import InvokeResult
from tests.test_cli_inputs import _review as _cli_review
from tests.test_cli_inputs import runtime  # noqa: F401 -- pytest fixture
from tests.test_orchestrator import REF, FakeForge, _added_file_diff

HEAD = "a" * 40
NEW_HEAD = "c" * 40
AT = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
DIFF = _added_file_diff("src/app.py", 5)
DESCRIBE_SYSTEM_HEAD = "You write the description of a pull request from its diff"
GENERATED = "## Summary\n\nAdds the app module.\n\n## Changes\n\n- `src/app.py`: new file."
DESCRIBE_REPLY = json.dumps({"description": GENERATED, "labels": ["feat", "breaking"]})
ONE_FINDING = json.dumps({"findings": [{
    "file": "src/app.py", "line": 3, "severity": "warning", "confidence": 0.9,
    "title": "Leak", "body": "data 3 leaks",
}]})
HUMAN = "Fixes the widget.\n\nTicket: ACME-1"


class _LLM:
    """Answers the describe prompt with ``describe_reply`` and every review prompt with ``review_reply``."""

    def __init__(self, describe_reply: str = DESCRIBE_REPLY, review_reply: str = ONE_FINDING,
                 describe_error: Exception | None = None):
        self.describe_reply = describe_reply
        self.review_reply = review_reply
        self.describe_error = describe_error
        self.describe_calls: list[tuple[str, str]] = []
        self.review_calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        is_describe = system.startswith(DESCRIBE_SYSTEM_HEAD)
        with self._lock:
            (self.describe_calls if is_describe else self.review_calls).append((system, user))
        if is_describe and self.describe_error is not None:
            raise self.describe_error
        return InvokeResult(
            text=self.describe_reply if is_describe else self.review_reply,
            input_tokens=100 if is_describe else 10, output_tokens=7 if is_describe else 5,
            model="fake-model", backend="fake", elapsed_ms=1,
            cost_usd=0.5 if is_describe else 0.25,
        )


class _WritingForge(FakeForge):
    """A FakeForge with the #37 write methods, recording every call."""

    def __init__(self, description: str = "", *, head: str = HEAD, repo_labels=("bug", "Feature")):
        super().__init__(
            pr=PRData(
                title="Add app", description=description, author="alice",
                source_branch="feature/app", target_branch="main",
                source_sha=head, target_sha="b" * 40, raw={},
            ),
            diff=DIFF,
        )
        self.repo_labels = list(repo_labels)
        self.descriptions: list[str] = []
        self.label_calls: list[tuple[list[str], bool]] = []
        self.list_calls = 0

    def update_description(self, ref, body):
        if "update_description" in self.fail:
            raise RuntimeError("boom update_description")
        self.descriptions.append(body)

    def list_labels(self, ref):
        self.list_calls += 1
        if "list_labels" in self.fail:
            raise FeedReadError("label list could not be read")
        return list(self.repo_labels)

    def add_labels(self, ref, labels, *, create_missing=False):
        self.label_calls.append((list(labels), create_missing))
        applied = []
        for name in labels:
            if name in self.repo_labels:
                applied.append(name)
            elif create_missing:
                self.repo_labels.append(name)
                applied.append(name)
        return applied


class _NoLabelForge(_WritingForge):
    """Bitbucket-shaped: description writes, no label methods."""

    list_labels = None  # type: ignore[assignment]
    add_labels = None  # type: ignore[assignment]

    def __getattribute__(self, name):
        if name in ("list_labels", "add_labels"):
            raise AttributeError(name)
        return super().__getattribute__(name)


@pytest.fixture(autouse=True)
def work(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(orchestrator, "_describe_now", lambda: AT)
    return root


def _run(forge, llm=None, **kwargs):
    llm = llm or _LLM()
    res = orchestrator.orchestrate_review(forge, REF, llm, **kwargs)
    return res, llm


class TestOptIn:
    def test_default_settings_make_no_describe_call_and_no_write(self):
        forge = _WritingForge("")
        res, llm = _run(forge)
        assert llm.describe_calls == []
        assert forge.descriptions == [] and forge.label_calls == [] and forge.list_calls == 0
        assert res["describe"] is None
        assert _build_json_result(res)["describe"] is None

    def test_empty_mode_on_an_empty_body_writes_the_marked_pinned_description(self):
        forge = _WritingForge("")
        res, llm = _run(forge, describe_mode="empty")
        assert len(llm.describe_calls) == 1
        assert len(forge.descriptions) == 1
        body = forge.descriptions[0]
        assert body.startswith(f"<!-- prxref:generated v1 head={HEAD} at=2026-10-14T12:00:00Z -->\n")
        assert GENERATED in body
        assert "<details>" not in body  # no original to preserve
        parts = describe.split_description(body)
        assert (parts.head, parts.at, parts.original) == (HEAD, AT, "")
        rec = res["describe"]
        assert rec["written"] is True and rec["error"] is None and rec["head"] == HEAD
        assert rec["mode"] == "empty" and rec["labels_mode"] == "off"
        assert rec["labels_reason"] == "PRXREF_LABELS is off"
        assert forge.label_calls == []
        assert _build_json_result(res)["describe"] == rec

    def test_existing_labels_mode_applies_only_repo_labels_and_never_creates(self):
        forge = _WritingForge("", repo_labels=("bug", "Feature"))
        res, _ = _run(forge, describe_mode="empty", labels_mode="existing")
        # "feat" maps to the repo's "Feature"; "breaking" has no repo label and is not created.
        assert forge.label_calls == [(["Feature"], False)]
        assert forge.repo_labels == ["bug", "Feature"]
        assert res["describe"]["labels_applied"] == ["Feature"]
        assert res["describe"]["labels_reason"] is None

    def test_create_labels_mode_creates_the_conventional_labels(self):
        forge = _WritingForge("", repo_labels=())
        res, _ = _run(forge, describe_mode="empty", labels_mode="create")
        assert forge.list_calls == 0
        assert forge.label_calls == [(["feat", "breaking"], True)]
        assert forge.repo_labels == ["feat", "breaking"]
        assert res["describe"]["labels_applied"] == ["feat", "breaking"]

    def test_an_unreadable_label_list_applies_nothing_and_records_it(self):
        forge = _WritingForge("")
        forge.fail.add("list_labels")
        res, _ = _run(forge, describe_mode="empty", labels_mode="existing")
        assert forge.label_calls == []
        assert res["describe"]["written"] is True
        assert "label list could not be read" in res["describe"]["error"]
        assert res["describe"]["labels_applied"] == []

    def test_a_forge_without_labels_writes_the_description_and_says_why(self):
        forge = _NoLabelForge("")
        res, _ = _run(forge, describe_mode="empty", labels_mode="create")
        assert len(forge.descriptions) == 1
        assert res["describe"]["labels_reason"] == "the fake forge has no PR labels"
        assert res["describe"]["error"] is None

    def test_unknown_modes_raise_before_any_forge_call(self):
        forge = _WritingForge("")
        forge.fail.add("get_pr")
        with pytest.raises(ValueError, match="describe_mode"):
            _run(forge, describe_mode="always")
        with pytest.raises(ValueError, match="labels_mode"):
            _run(forge, labels_mode="all")


class TestPinning:
    def test_a_second_run_on_the_same_head_makes_no_call_and_no_write(self):
        forge = _WritingForge("")
        _run(forge, describe_mode="empty")
        written = forge.descriptions[0]
        again = _WritingForge(written)
        res, llm = _run(again, describe_mode="empty", labels_mode="create")
        assert llm.describe_calls == []
        assert again.descriptions == [] and again.label_calls == []
        assert res["describe"]["written"] is False
        assert "already pinned to head" in res["describe"]["reason"]

    def test_force_on_the_same_head_also_skips(self):
        first = _WritingForge(HUMAN)
        _run(first, describe_mode="force")
        again = _WritingForge(first.descriptions[0])
        res, llm = _run(again, describe_mode="force")
        assert llm.describe_calls == [] and again.descriptions == []
        assert "already pinned to head" in res["describe"]["reason"]

    def test_a_new_head_regenerates_and_keeps_the_original(self):
        first = _WritingForge(HUMAN)
        _run(first, describe_mode="force")
        moved = _WritingForge(first.descriptions[0], head=NEW_HEAD)
        res, llm = _run(moved, describe_mode="force")
        assert len(llm.describe_calls) == 1
        body = moved.descriptions[0]
        parts = describe.split_description(body)
        assert parts.head == NEW_HEAD
        assert parts.original == HUMAN
        assert body.count("<!-- prxref:generated ") == 1  # blocks never nest
        assert res["describe"]["written"] is True

    def test_empty_mode_regenerates_a_generated_only_body_on_a_new_head(self):
        first = _WritingForge("")
        _run(first, describe_mode="empty")
        moved = _WritingForge(first.descriptions[0], head=NEW_HEAD)
        res, llm = _run(moved, describe_mode="empty")
        assert len(llm.describe_calls) == 1
        assert describe.split_description(moved.descriptions[0]).head == NEW_HEAD


class TestNeverOverwriteHuman:
    def test_empty_mode_leaves_a_human_description_and_records_why(self):
        forge = _WritingForge(HUMAN)
        res, llm = _run(forge, describe_mode="empty", labels_mode="create")
        assert llm.describe_calls == []
        assert forge.descriptions == [] and forge.label_calls == []
        assert res["describe"]["written"] is False
        assert "human-written description" in res["describe"]["reason"]

    def test_force_writes_and_preserves_the_original_verbatim(self):
        forge = _WritingForge(HUMAN)
        res, _ = _run(forge, describe_mode="force")
        body = forge.descriptions[0]
        assert "<details><summary>Original description</summary>" in body
        assert f"\n{HUMAN}\n" in body
        assert describe.split_description(body).original == HUMAN
        assert res["describe"]["written"] is True


class TestFailureIsolation:
    def _baseline(self):
        res, _ = _run(_WritingForge(""))
        return res

    def test_a_forge_write_failure_keeps_the_verdict_and_records_the_error(self):
        base = self._baseline()
        forge = _WritingForge("")
        forge.fail.add("update_description")
        res, _ = _run(forge, describe_mode="empty", labels_mode="create")
        assert res["verdict"] == base["verdict"]
        assert [f.title for f in res["findings_active"]] == [f.title for f in base["findings_active"]]
        assert forge.summaries, "the review still posted"
        assert res["describe"]["written"] is False
        assert res["describe"]["error"] == "RuntimeError: boom update_description"
        assert forge.label_calls == []

    def test_a_model_failure_keeps_the_verdict_and_records_the_error(self):
        base = self._baseline()
        forge = _WritingForge("")
        res, _ = _run(forge, _LLM(describe_error=TimeoutError("slow")), describe_mode="empty")
        assert res["verdict"] == base["verdict"]
        assert forge.descriptions == []
        assert "TimeoutError: slow" in res["describe"]["error"]

    def test_a_bad_json_reply_writes_nothing(self):
        forge = _WritingForge("")
        res, _ = _run(forge, _LLM(describe_reply="not json at all"), describe_mode="empty")
        assert forge.descriptions == []
        assert res["describe"]["written"] is False and res["describe"]["error"]

    def test_a_label_failure_keeps_the_description_and_verdict(self):
        base = self._baseline()
        forge = _WritingForge("")

        def boom(*a, **k):
            raise RuntimeError("labels down")

        forge.add_labels = boom
        res, _ = _run(forge, describe_mode="empty", labels_mode="create")
        assert res["verdict"] == base["verdict"]
        assert len(forge.descriptions) == 1
        assert res["describe"]["written"] is True
        assert "labels down" in res["describe"]["error"]


class TestNoPost:
    def test_post_false_makes_no_call_and_no_write(self):
        forge = _WritingForge("")
        res, llm = _run(forge, post=False, describe_mode="force", labels_mode="create")
        assert llm.describe_calls == []
        assert forge.descriptions == [] and forge.label_calls == [] and forge.summaries == []
        assert res["describe"]["written"] is False
        assert res["describe"]["reason"].startswith("posting is off")

    def test_the_cli_passes_both_settings_and_no_post(self, runtime, monkeypatch):  # noqa: F811
        monkeypatch.setenv("PRXREF_DESCRIBE", "force")
        monkeypatch.setenv("PRXREF_LABELS", "existing")
        assert _cli_review() == 0
        call = runtime.orchestrate[0]
        assert (call["describe_mode"], call["labels_mode"], call["post"]) == ("force", "existing", False)

    def test_the_cli_defaults_are_off(self, runtime):  # noqa: F811
        assert _cli_review() == 0
        call = runtime.orchestrate[0]
        assert (call["describe_mode"], call["labels_mode"]) == ("off", "off")


class TestReviewInput:
    def test_the_review_prompts_see_the_original_only(self):
        body = describe.compose_description(
            "GENERATED-SENTINEL text", HUMAN, head_sha="d" * 40, at=AT,
        )
        forge = _WritingForge(body)
        _, llm = _run(forge)
        assert llm.review_calls, "the review ran"
        for system, user in llm.review_calls:
            assert "GENERATED-SENTINEL" not in system + user
            assert "prxref:generated" not in system + user
        # The worker prompt carries the author's text; the sweep is last.
        assert any("Ticket: ACME-1" in user for _, user in llm.review_calls[:-1])
        assert "Ticket: ACME-1" in llm.review_calls[-1][1]


class TestCost:
    def test_the_describe_call_joins_the_run_cost_and_tokens(self):
        base, _ = _run(_WritingForge(""))
        res, _ = _run(_WritingForge(""), describe_mode="empty")
        assert res["cost_usd"] == pytest.approx(base["cost_usd"] + 0.5)
        assert res["input_tokens"] == base["input_tokens"] + 100
        assert res["output_tokens"] == base["output_tokens"] + 7

    def test_a_skipped_step_adds_no_unit(self):
        base, _ = _run(_WritingForge(HUMAN))
        res, _ = _run(_WritingForge(HUMAN), describe_mode="empty")
        assert res["cost_usd"] == base["cost_usd"]
        assert res["input_tokens"] == base["input_tokens"]


class TestLengthCap:
    def test_the_generated_part_is_shortened_to_the_forge_limit(self):
        long_reply = json.dumps({"description": "x" * 5000, "labels": []})
        forge = _WritingForge(HUMAN)
        forge.max_description_chars = 4000
        res, _ = _run(forge, _LLM(describe_reply=long_reply), describe_mode="force")
        body = forge.descriptions[0]
        assert len(body) <= 4000
        assert describe.split_description(body).original == HUMAN
        assert "shortened to fit" in body
        assert res["describe"]["written"] is True

    def test_an_original_too_long_to_fit_skips_the_write(self):
        forge = _WritingForge("y" * 4100)
        forge.max_description_chars = 4000
        res, _ = _run(forge, describe_mode="force")
        assert forge.descriptions == []
        assert res["describe"]["written"] is False
        assert "4000-character limit" in res["describe"]["reason"]

    def test_azure_declares_its_limit(self):
        from prxref.forges.azure_devops import ForgeImpl

        assert ForgeImpl.max_description_chars == 4000


class TestGate:
    def test_the_capturing_forge_refuses_description_and_label_writes(self):
        inner = _WritingForge("")
        gen = types.SimpleNamespace(head_sha=HEAD, base_sha="b" * 40)
        cap = worker.CapturingForge(inner, gen)
        assert getattr(cap, "update_description", None) is None
        assert getattr(cap, "add_labels", None) is None
        assert cap.list_labels(REF) == ["bug", "Feature"]  # a read still forwards

    def test_a_gate_review_records_why_and_never_writes(self):
        inner = _WritingForge("")
        gen = types.SimpleNamespace(head_sha=HEAD, base_sha="b" * 40)
        inner.get_compare_diff = lambda ref, base_sha, head_sha: DIFF
        cap = worker.CapturingForge(inner, gen)
        res, llm = _run(cap, describe_mode="empty", labels_mode="create")
        assert llm.describe_calls == []
        assert inner.descriptions == [] and inner.label_calls == []
        assert res["describe"]["reason"] == "the fake forge cannot update a PR description"
