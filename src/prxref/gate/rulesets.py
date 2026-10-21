"""GitHub App manifest and merge ruleset for the sdlc gate (#102).

The ruleset requires the ``sdlc-review`` check from one app
(``integration_id``), a branch up to date with its base, zero required
approvals, and no bypass actors. The required check is the gate. The app's own
APPROVE or REQUEST_CHANGES review is informational and does not count toward
merge. The ruleset is applied with the operator's own ``gh`` login: the app's token has no administration
permission and never edits rulesets.
"""

from __future__ import annotations

import difflib
import json
import subprocess
from collections.abc import Callable
from typing import Any

from prxref.gate.app_auth import TOKEN_PERMISSIONS
from prxref.gate.receiver import CHECK_NAME

RULESET_NAME = "sdlc-review"
EVENTS = ["pull_request", "check_run", "check_suite"]


class RulesetError(RuntimeError):
    """A ``gh api`` call failed, or the live ruleset is missing."""


def render_manifest(webhook_url: str, *, name: str = "prxref-sdlc") -> dict:
    """The App manifest for GitHub's manifest flow."""
    return {
        "name": name,
        "url": "https://github.com/sblattj/prxref",
        "hook_attributes": {"url": webhook_url, "active": True},
        "public": False,
        # Check-only model: the required check is the gate, so the installation needs only
        # contents:read. The app's review is informational and no longer counts toward merge.
        "default_permissions": {**TOKEN_PERMISSIONS, "contents": "read", "metadata": "read"},
        "default_events": list(EVENTS),
    }


def render_ruleset(integration_id: int) -> dict:
    """The default-branch ruleset that requires the app's check and no approvals."""
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [{"context": CHECK_NAME, "integration_id": integration_id}],
                },
            },
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews_on_push": True,
                    "require_last_push_approval": False,
                    "require_code_owner_review": False,
                    "required_review_thread_resolution": False,
                },
            },
        ],
    }


def _gh(run: Callable, method: str, path: str, body: dict | None = None) -> Any:
    args = ["gh", "api", "--method", method, path]
    if body is not None:
        args += ["--input", "-"]
    proc = run(args, input=json.dumps(body) if body is not None else None, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RulesetError(f"gh api {method} {path} exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def _existing_id(run: Callable, repo: str) -> int | None:
    for ruleset in _gh(run, "GET", f"repos/{repo}/rulesets") or []:
        if ruleset.get("name") == RULESET_NAME:
            return ruleset["id"]
    return None


def apply_ruleset(repo: str, integration_id: int, *, run: Callable = subprocess.run) -> int:
    """Create the ruleset, or replace the one named ``sdlc-review``; returns its id."""
    body = render_ruleset(integration_id)
    existing = _existing_id(run, repo)
    if existing is None:
        return _gh(run, "POST", f"repos/{repo}/rulesets", body)["id"]
    return _gh(run, "PUT", f"repos/{repo}/rulesets/{existing}", body)["id"]


def _project(live: Any, shape: Any) -> Any:
    """``live`` cut down to the keys ``shape`` has, so fields GitHub adds are not a diff."""
    if isinstance(shape, dict) and isinstance(live, dict):
        return {k: _project(live[k], v) if k in live else None for k, v in shape.items()}
    if isinstance(shape, list) and isinstance(live, list) and shape and isinstance(shape[0], dict):
        return [_project(item, shape[min(i, len(shape) - 1)]) for i, item in enumerate(live)]
    return live


def diff_ruleset(repo: str, integration_id: int, *, run: Callable = subprocess.run) -> str:
    """A unified diff of the rendered ruleset against the live one; ``""`` when they match."""
    existing = _existing_id(run, repo)
    if existing is None:
        raise RulesetError(f"{repo} has no ruleset named {RULESET_NAME}")
    rendered = render_ruleset(integration_id)
    live = _project(_gh(run, "GET", f"repos/{repo}/rulesets/{existing}"), rendered)
    a = json.dumps(rendered, indent=2, sort_keys=True).splitlines()
    b = json.dumps(live, indent=2, sort_keys=True).splitlines()
    return "\n".join(difflib.unified_diff(a, b, "rendered", "live", lineterm=""))
