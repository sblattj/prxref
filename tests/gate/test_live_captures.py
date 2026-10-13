"""The sandbox live proof is complete and says what it proved (#102, spec "Live proof")."""

import json
import re
from pathlib import Path

import pytest

DIR = Path(__file__).resolve().parents[2] / "docs" / "live-instance-verification" / "sdlc-gate"
REQUIRED = {
    "1": {"ruleset", "foreign-status", "pr-blocked"},
    "2": {"check-run", "app-review", "pr-clean"},
    "3": {"approval-dismissed", "check-run-new-head", "pr-blocked-new-head"},
    "4": {"request-changes-review", "pr-blocked-failing", "owner-dismissal", "bad-signature", "incomplete", "deadline",
          "duplicate", "rerequest", "stale-head", "close-reopen", "publish-5xx", "worker-restart", "secret-rotation"},
}
SHA = re.compile(r"^[0-9a-f]{40}$")
SECRET_SHAPES = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY|ABSK[A-Za-z0-9+/=]{20,}")


@pytest.fixture(scope="module")
def index():
    return json.loads((DIR / "index.json").read_text(encoding="utf-8"))


def _entry(index, phase, name):
    return next(e for e in index if (e["phase"], e["name"]) == (phase, name))


def _capture(index, phase, name):
    return json.loads((DIR / _entry(index, phase, name)["file"]).read_text(encoding="utf-8"))


def test_every_required_capture_is_present(index):
    have = {}
    for e in index:
        have.setdefault(e["phase"], set()).add(e["name"])
    assert {p: names - have.get(p, set()) for p, names in REQUIRED.items()} == {p: set() for p in REQUIRED}


def test_every_entry_names_its_shas_and_ids(index):
    for e in index:
        assert (DIR / e["file"]).is_file(), e["file"]
        assert SHA.match(e["head_sha"]) and SHA.match(e["base_sha"]), e
        assert isinstance(e["ruleset_id"], int) and isinstance(e["app_id"], int), e
        assert e["check_run_id"] is None or isinstance(e["check_run_id"], int), e


def test_phases_one_and_two_hold_repo_pr_base_and_head_constant(index):
    keys = {(e["repo"], e["pr"], e["base_sha"], e["head_sha"], e["ruleset_id"])
            for e in index if e["phase"] in ("1", "2")}
    assert len(keys) == 1


def test_the_foreign_status_blocks_and_the_app_check_clears(index):
    ruleset = _capture(index, "1", "ruleset")
    required = [r for r in ruleset["rules"] if r["type"] == "required_status_checks"][0]
    integration = required["parameters"]["required_status_checks"][0]["integration_id"]
    assert _capture(index, "1", "foreign-status")["context"] == "sdlc-review"
    assert _capture(index, "1", "pr-blocked")["mergeable_state"] == "blocked"
    check = _capture(index, "2", "check-run")
    assert (check["name"], check["app"]["id"], check["conclusion"]) == ("sdlc-review", integration, "success")
    assert _capture(index, "2", "pr-clean")["mergeable_state"] == "clean"


def test_the_app_itself_approves_the_passing_pr(index):
    reviews = _capture(index, "2", "app-review")
    approvals = [r for r in reviews if r["state"] == "APPROVED"]
    assert approvals and all(r["user"]["type"] == "Bot" for r in approvals)
    assert all(r["user"]["login"].endswith("[bot]") for r in approvals)


def test_a_failing_pr_is_blocked_by_request_changes_and_the_failing_check(index):
    reviews = _capture(index, "4", "request-changes-review")
    assert any(r["state"] == "CHANGES_REQUESTED" and r["user"]["type"] == "Bot" for r in reviews)
    assert _capture(index, "4", "pr-blocked-failing")["mergeable_state"] == "blocked"


def test_dismissing_the_apps_approve_returns_to_review_required(index):
    reviews = _capture(index, "4", "owner-dismissal")
    assert any(r["state"] == "DISMISSED" and r["user"]["type"] == "Bot" for r in reviews)


def test_a_new_push_dismisses_the_approval_and_blocks_again(index):
    reviews = _capture(index, "3", "approval-dismissed")
    assert any(r["state"] == "DISMISSED" for r in reviews)
    assert _capture(index, "3", "pr-blocked-new-head")["mergeable_state"] == "blocked"
    assert _entry(index, "3", "check-run-new-head")["head_sha"] != _entry(index, "2", "check-run")["head_sha"]


def test_no_capture_carries_a_credential():
    for path in sorted(DIR.glob("*.json")):
        assert not SECRET_SHAPES.search(path.read_text(encoding="utf-8")), path.name
