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
SECRET_SHAPES = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY|ABSK[A-Za-z0-9+/=]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}|sk-or-[A-Za-z0-9-]{20,}|(?i:bearer)\s+[A-Za-z0-9._-]{20,}"
)


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


def test_no_file_in_the_directory_carries_a_credential():
    files = [p for p in sorted(DIR.iterdir()) if p.is_file()]
    assert {p.name for p in files} >= {"README.md", "capture.py", "faults.py", "index.json"}
    for path in files:
        assert not SECRET_SHAPES.search(path.read_text(encoding="utf-8")), path.name


def test_the_secret_shapes_pattern_catches_what_it_claims_to():
    for sample in ("ghp_" + "a" * 24, "github_pat_" + "A1_" * 10, "sk-or-v1-" + "ab12" * 8,
                   "Authorization: Bearer " + "x" * 30, "-----BEGIN RSA PRIVATE KEY-----"):
        assert SECRET_SHAPES.search(sample), sample
    assert not SECRET_SHAPES.search("a bearer of news")


def test_every_json_in_the_directory_is_indexed(index):
    on_disk = {p.name for p in DIR.glob("*.json")} - {"index.json"}
    assert on_disk == {e["file"] for e in index}


def _statuses(cases):
    return [(c["status"], c["reply"].get("status") or c["reply"].get("error")) for c in cases]


def test_a_bad_signature_is_rejected_with_401(index):
    cases = _capture(index, "4", "bad-signature")["cases"]
    assert _statuses(cases) == [(401, "signature rejected")]


def test_a_repeated_delivery_is_accepted_once_then_flagged_duplicate(index):
    cases = _capture(index, "4", "duplicate")["cases"]
    assert _statuses(cases) == [(202, "accepted"), (202, "duplicate")]
    assert cases[0]["delivery"] == cases[1]["delivery"]


def test_a_rerequest_is_accepted_and_the_new_check_run_on_the_same_head_succeeds(index):
    cap = _capture(index, "4", "rerequest")
    assert _statuses(cap["deliveries"]) == [(202, "accepted")]
    run = cap["new_check_run"]
    assert (run["name"], run["status"], run["conclusion"]) == ("sdlc-review", "completed", "success")
    assert run["head_sha"] == _entry(index, "4", "rerequest")["head_sha"]
    assert run["id"] == _entry(index, "4", "rerequest")["check_run_id"]


def test_an_incomplete_review_concludes_failure_and_names_the_verdict(index):
    run = _capture(index, "4", "incomplete")
    assert run["conclusion"] == "failure"
    assert "Verdict: Incomplete" in run["output"]["summary"]


def test_a_review_past_its_deadline_concludes_failure(index):
    run = _capture(index, "4", "deadline")
    assert run["conclusion"] == "failure"
    assert "deadline: review exceeded 20s" in run["output"]["summary"]


def test_a_superseded_head_goes_stale_and_never_succeeds(index):
    cap = _capture(index, "4", "stale-head")
    first, second = cap["generations"]
    assert (first["state"], first["error"], first["conclusion"]) == ("stale", "head moved", None)
    assert first["head_sha"] != second["head_sha"] and second["state"] == "completed"
    stale_run = cap["first_head_check_runs"]
    assert [(r["id"], r["conclusion"]) for r in stale_run] == [(first["check_run_id"], None)]
    assert [r["id"] for r in cap["second_head_check_runs"]] == [second["check_run_id"]]


def test_close_cancels_the_queued_generation_and_reopen_gets_a_new_one(index):
    cap = _capture(index, "4", "close-reopen")
    cancelled, reopened = cap["generations"]
    assert (cancelled["state"], cancelled["check_run_id"]) == ("cancelled", None)
    assert (reopened["reason"], reopened["state"], reopened["conclusion"]) == ("reopened", "completed", "success")
    assert reopened["gen"] > cancelled["gen"]
    assert [r["id"] for r in cap["check_runs"]] == [reopened["check_run_id"]]


def test_a_lost_publish_reply_leaves_exactly_one_concluded_check_run(index):
    cap = _capture(index, "4", "publish-5xx")
    assert any("injected 502" in line for line in cap["stderr"])
    runs = cap["sdlc_review_check_runs"]
    assert len(runs) == 1 and (runs[0]["status"], runs[0]["conclusion"]) == ("completed", "success")
    assert [g["check_run_id"] for g in cap["generations"]] == [runs[0]["id"]]


def test_a_worker_restart_keeps_the_same_check_run_id_through_to_success(index):
    cap = _capture(index, "4", "worker-restart")
    after = cap["check_run_after"]
    assert cap["check_run_seen_in_progress"] == after["id"] == _entry(index, "4", "worker-restart")["check_run_id"]
    assert (after["status"], after["conclusion"]) == ("completed", "success")
    assert any("re-queued in-progress generations" in line for line in cap["log"])
    mine = [g for g in cap["generations"] if g["check_run_id"] == after["id"]]
    assert len(mine) == 1 and mine[0]["attempts"] == 2 and mine[0]["state"] == "completed"


def test_the_previous_secret_works_inside_the_grace_window_and_not_after(index):
    cap = _capture(index, "4", "secret-rotation")
    assert cap["grace_minutes"] == 2
    assert _statuses(cap["inside_window_old_secret"]) == [(202, "accepted"), (202, "duplicate")]
    assert _statuses(cap["after_window_old_secret_sent_17:44:43"]) == [(401, "signature rejected")] * 2
    assert _statuses(cap["after_window_new_secret"]) == [(202, "accepted"), (202, "duplicate")]
