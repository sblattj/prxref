"""App manifest and ruleset rendering, apply and diff (#102)."""

import copy
import json
import subprocess
from pathlib import Path

import pytest

from prxref.gate import rulesets

GOLDEN = Path(__file__).parent / "golden"
REPO = "sblattj/sdlc-sandbox"


def _golden(name):
    return json.loads((GOLDEN / name).read_text())


def test_manifest_matches_golden():
    assert rulesets.render_manifest("https://sdlc.example.dev/webhook") == _golden("manifest.json")


def test_ruleset_matches_golden():
    assert rulesets.render_ruleset(123456) == _golden("ruleset.json")


class _Gh:
    """A scripted ``subprocess.run`` for ``gh api``."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, args, input=None, capture_output=True, text=True, check=False):
        self.calls.append((args, input))
        rc, out = self.replies.pop(0)
        return subprocess.CompletedProcess(args, rc, stdout=out, stderr="" if rc == 0 else "HTTP 403: nope")


def test_apply_creates_when_absent():
    gh = _Gh([(0, "[]"), (0, json.dumps({"id": 77}))])
    assert rulesets.apply_ruleset(REPO, 123456, run=gh) == 77
    args, body = gh.calls[1]
    assert args == ["gh", "api", "--method", "POST", f"repos/{REPO}/rulesets", "--input", "-"]
    assert json.loads(body) == _golden("ruleset.json")


def test_apply_updates_the_existing_ruleset_by_name():
    gh = _Gh(
        [(0, json.dumps([{"id": 5, "name": "other"}, {"id": 77, "name": "sdlc-review"}])), (0, json.dumps({"id": 77}))]
    )
    assert rulesets.apply_ruleset(REPO, 123456, run=gh) == 77
    assert gh.calls[1][0][:5] == ["gh", "api", "--method", "PUT", f"repos/{REPO}/rulesets/77"]


def _live(**changes):
    live = copy.deepcopy(_golden("ruleset.json"))
    live.update({"id": 77, "source": REPO, "source_type": "Repository"})
    live["rules"][1]["parameters"]["allowed_merge_methods"] = ["merge", "squash", "rebase"]
    live.update(changes)
    return live


def test_diff_ignores_fields_github_adds():
    gh = _Gh([(0, json.dumps([{"id": 77, "name": "sdlc-review"}])), (0, json.dumps(_live()))])
    assert rulesets.diff_ruleset(REPO, 123456, run=gh) == ""


def test_diff_shows_an_added_bypass_actor():
    actor = {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
    gh = _Gh([(0, json.dumps([{"id": 77, "name": "sdlc-review"}])), (0, json.dumps(_live(bypass_actors=[actor])))])
    diff = rulesets.diff_ruleset(REPO, 123456, run=gh)
    assert "RepositoryRole" in diff and diff.startswith("--- rendered")


def test_diff_without_a_live_ruleset_says_so():
    gh = _Gh([(0, "[]")])
    with pytest.raises(rulesets.RulesetError, match="no ruleset named sdlc-review"):
        rulesets.diff_ruleset(REPO, 123456, run=gh)


def test_a_gh_failure_raises_with_its_stderr():
    gh = _Gh([(1, "")])
    with pytest.raises(rulesets.RulesetError, match="HTTP 403"):
        rulesets.apply_ruleset(REPO, 123456, run=gh)
