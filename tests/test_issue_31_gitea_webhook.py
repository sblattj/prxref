"""Gitea / Forgejo webhook verification (issue #31).

The fixtures under tests/fixtures/gitea/ are real deliveries captured from
throwaway local Gitea 1.24 and Forgejo 11 instances, with the host rewritten to
git.example.com and the signature values dropped. Every test re-signs the
scrubbed body the way the instance did, so the header NAMES and the payload
shapes are the observed ones.

The load-bearing observation: both forges send GitHub-compatible headers
(X-GitHub-Event, X-Hub-Signature-256) on every delivery, alongside their own.
Before #31 such a delivery was routed to the GitHub verifier.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from prxref.webhooks import _status_for_reason, _verify_github, verify_signature

FIXTURES = Path(__file__).parent / "fixtures" / "gitea"
SECRET = "gitea-secret"
PR_URL = "https://git.example.com/acme/widgets/pulls/1"

GITEA_HEADER_NAMES = {
    "Content-Type",
    "User-Agent",
    "X-GitHub-Delivery",
    "X-GitHub-Event",
    "X-GitHub-Event-Type",
    "X-GitHub-Hook-Installation-Target-Type",
    "X-Gitea-Delivery",
    "X-Gitea-Event",
    "X-Gitea-Event-Type",
    "X-Gitea-Hook-Installation-Target-Type",
    "X-Gitea-Signature",
    "X-Gogs-Delivery",
    "X-Gogs-Event",
    "X-Gogs-Event-Type",
    "X-Gogs-Signature",
    "X-Hub-Signature",
    "X-Hub-Signature-256",
}
FORGEJO_HEADER_NAMES = {
    "Content-Type",
    "User-Agent",
    "X-Forgejo-Delivery",
    "X-Forgejo-Event",
    "X-Forgejo-Event-Type",
    "X-Forgejo-Signature",
    "X-GitHub-Delivery",
    "X-GitHub-Event",
    "X-GitHub-Event-Type",
    "X-Gitea-Delivery",
    "X-Gitea-Event",
    "X-Gitea-Event-Type",
    "X-Gitea-Signature",
    "X-Gogs-Delivery",
    "X-Gogs-Event",
    "X-Gogs-Event-Type",
    "X-Gogs-Signature",
    "X-Hub-Signature",
    "X-Hub-Signature-256",
}

FAMILIES = ("gitea", "forgejo")
REVIEWABLE = ("opened", "synchronized", "reopened")


def _sign_like_the_forge(headers: dict, body: bytes, secret: str) -> dict:
    """Fill every signature header the way Gitea and Forgejo compute it."""
    sha256 = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    sha1 = hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    signed = dict(headers)
    for name in signed:
        lowered = name.lower()
        if lowered == "x-hub-signature-256":
            signed[name] = "sha256=" + sha256
        elif lowered == "x-hub-signature":
            signed[name] = "sha1=" + sha1
        elif lowered.endswith("-signature"):
            signed[name] = sha256
    return signed


def _delivery(family: str, name: str, secret: str = SECRET) -> tuple[bytes, dict]:
    fixture = json.loads((FIXTURES / f"{family}-{name}.json").read_text(encoding="utf-8"))
    body = fixture["body"].encode("utf-8")
    return body, _sign_like_the_forge(fixture["headers"], body, secret)


def _with_body(family: str, name: str, mutate) -> tuple[bytes, dict]:
    fixture = json.loads((FIXTURES / f"{family}-{name}.json").read_text(encoding="utf-8"))
    payload = json.loads(fixture["body"])
    mutate(payload)
    body = json.dumps(payload).encode()
    return body, _sign_like_the_forge(fixture["headers"], body, SECRET)


def _drop(headers: dict, *prefixes: str) -> dict:
    return {
        key: value for key, value in headers.items()
        if not key.lower().startswith(tuple(p.lower() for p in prefixes))
    }


class TestObservedHeaders:
    """The header names each instance actually sent, pinned from the capture."""

    @pytest.mark.parametrize("name", REVIEWABLE + ("closed", "push", "issue-comment"))
    def test_gitea_sends_its_own_gogs_and_github_families(self, name):
        _body, headers = _delivery("gitea", name)
        assert set(headers) == GITEA_HEADER_NAMES

    @pytest.mark.parametrize("name", REVIEWABLE + ("closed", "push", "issue-comment"))
    def test_forgejo_sends_its_own_gitea_gogs_and_github_families(self, name):
        _body, headers = _delivery("forgejo", name)
        assert set(headers) == FORGEJO_HEADER_NAMES

    @pytest.mark.parametrize("family", FAMILIES)
    def test_the_github_compatible_headers_claim_a_pull_request(self, family):
        _body, headers = _delivery(family, "opened")
        assert headers["X-GitHub-Event"] == "pull_request"
        assert headers["X-Hub-Signature-256"].startswith("sha256=")


class TestHappyPath:
    @pytest.mark.parametrize("family", FAMILIES)
    @pytest.mark.parametrize("name", REVIEWABLE)
    def test_reviewable_actions_yield_the_pr_url(self, monkeypatch, family, name):
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)
        body, headers = _delivery(family, name)
        assert verify_signature(body, headers) == (True, PR_URL)

    @pytest.mark.parametrize("family", FAMILIES)
    def test_header_names_are_case_insensitive(self, monkeypatch, family):
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)
        body, headers = _delivery(family, "opened")
        lowered = {key.lower(): value for key, value in headers.items()}
        assert verify_signature(body, lowered) == (True, PR_URL)

    def test_forgejo_is_verified_by_its_own_signature_alone(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)
        body, headers = _delivery("forgejo", "opened")
        headers = _drop(headers, "X-Gitea-", "X-Gogs-", "X-GitHub-", "X-Hub-")
        assert verify_signature(body, headers) == (True, PR_URL)

    def test_gitea_is_verified_by_its_own_headers_alone(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)
        body, headers = _delivery("gitea", "synchronized")
        headers = _drop(headers, "X-Gogs-", "X-GitHub-", "X-Hub-")
        assert verify_signature(body, headers) == (True, PR_URL)


class TestRejections:
    @pytest.mark.parametrize("family", FAMILIES)
    def test_wrong_secret_is_a_401_mismatch(self, monkeypatch, family):
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)
        body, headers = _delivery(family, "opened", secret="not-the-secret")
        ok, reason = verify_signature(body, headers)
        assert (ok, reason) == (False, "gitea signature mismatch")
        assert _status_for_reason(reason)[0] == 401

    @pytest.mark.parametrize("family", FAMILIES)
    def test_missing_signature_is_a_401(self, monkeypatch, family):
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)
        body, headers = _delivery(family, "opened")
        headers = _drop(headers, "X-Forgejo-Signature", "X-Gitea-Signature")
        ok, reason = verify_signature(body, headers)
        assert (ok, reason) == (False, "missing gitea signature header")
        assert _status_for_reason(reason)[0] == 401

    @pytest.mark.parametrize("family", FAMILIES)
    def test_unconfigured_secret_is_a_401(self, family):
        body, headers = _delivery(family, "opened")
        ok, reason = verify_signature(body, headers)
        assert (ok, reason) == (False, "gitea secret not configured")
        assert _status_for_reason(reason)[0] == 401

    def test_a_github_style_prefixed_signature_is_refused(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)
        body, headers = _delivery("gitea", "opened")
        headers["X-Gitea-Signature"] = headers["X-Hub-Signature-256"]
        assert verify_signature(body, headers) == (False, "gitea signature mismatch")

    def test_a_tampered_body_is_refused(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)
        body, headers = _delivery("forgejo", "opened")
        tampered = body.replace(b"/pulls/1", b"/pulls/2")
        assert tampered != body
        assert verify_signature(tampered, headers) == (False, "gitea signature mismatch")


class TestAllowUnsigned:
    def test_no_secret_is_accepted_and_flagged(self, monkeypatch):
        monkeypatch.setenv("PRXREF_ALLOW_UNSIGNED", "1")
        body, headers = _delivery("gitea", "opened")
        assert verify_signature(body, headers) == (True, "unsigned:" + PR_URL)

    def test_no_signature_is_accepted_and_flagged(self, monkeypatch):
        monkeypatch.setenv("PRXREF_ALLOW_UNSIGNED", "1")
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)
        body, headers = _delivery("forgejo", "synchronized")
        headers = _drop(headers, "X-Forgejo-Signature", "X-Gitea-Signature")
        assert verify_signature(body, headers) == (True, "unsigned:" + PR_URL)

    def test_a_wrong_signature_is_still_refused(self, monkeypatch):
        monkeypatch.setenv("PRXREF_ALLOW_UNSIGNED", "1")
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)
        body, headers = _delivery("gitea", "opened", secret="not-the-secret")
        assert verify_signature(body, headers) == (False, "gitea signature mismatch")

    @pytest.mark.parametrize("value", ["true", "yes", "on", "0"])
    def test_only_the_literal_one_enables_the_bypass(self, monkeypatch, value):
        monkeypatch.setenv("PRXREF_ALLOW_UNSIGNED", value)
        body, headers = _delivery("gitea", "opened")
        assert verify_signature(body, headers) == (False, "gitea secret not configured")


class TestIgnored:
    @pytest.mark.parametrize("family", FAMILIES)
    @pytest.mark.parametrize(
        ("name", "event"), [("push", "push"), ("issue-comment", "issue_comment")]
    )
    def test_non_pull_request_events_are_ignored(self, monkeypatch, family, name, event):
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)
        body, headers = _delivery(family, name)
        ok, reason = verify_signature(body, headers)
        assert (ok, reason) == (False, f"ignored: gitea event {event!r} is not pull_request")
        assert _status_for_reason(reason)[0] == 202

    @pytest.mark.parametrize("family", FAMILIES)
    def test_a_closed_pull_request_is_ignored(self, monkeypatch, family):
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)
        body, headers = _delivery(family, "closed")
        assert verify_signature(body, headers) == (
            False, "ignored: gitea pull_request action 'closed' is not reviewable"
        )

    @pytest.mark.parametrize("action", ["edited", "labeled", "assigned", "synchronize", ""])
    def test_other_actions_are_ignored(self, monkeypatch, action):
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)
        body, headers = _with_body(
            "gitea", "opened", lambda payload: payload.update(action=action)
        )
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert reason == f"ignored: gitea pull_request action {action!r} is not reviewable"


class TestPrUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://user:pass@git.example.com/acme/widgets/pulls/1",
            "https://token@git.example.com/acme/widgets/pulls/1",
            "ftp://git.example.com/acme/widgets/pulls/1",
            "javascript:alert(1)",
            "/acme/widgets/pulls/1",
            "",
            None,
            42,
        ],
    )
    def test_an_unusable_html_url_is_rejected(self, monkeypatch, url):
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)

        def mutate(payload):
            payload["pull_request"]["html_url"] = url

        body, headers = _with_body("gitea", "opened", mutate)
        ok, reason = verify_signature(body, headers)
        assert (ok, reason) == (False, "gitea payload missing a valid pull_request.html_url")
        assert _status_for_reason(reason)[0] == 400

    def test_a_missing_pull_request_object_is_rejected(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)
        body, headers = _with_body("forgejo", "opened", lambda p: p.pop("pull_request"))
        assert verify_signature(body, headers) == (
            False, "gitea payload missing a valid pull_request.html_url"
        )

    def test_a_plain_http_instance_is_accepted(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)
        url = "http://git.internal.example:3000/acme/widgets/pulls/1"

        def mutate(payload):
            payload["pull_request"]["html_url"] = url

        body, headers = _with_body("gitea", "opened", mutate)
        assert verify_signature(body, headers) == (True, url)

    def test_invalid_json_is_a_400(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)
        body = b"not json"
        _unused, headers = _delivery("gitea", "opened")
        headers = _sign_like_the_forge(headers, body, SECRET)
        ok, reason = verify_signature(body, headers)
        assert (ok, reason) == (False, "invalid JSON payload")
        assert _status_for_reason(reason)[0] == 400


class TestDispatchPriority:
    """A Gitea/Forgejo delivery must never reach the GitHub verifier."""

    @pytest.mark.parametrize("family", FAMILIES)
    def test_a_valid_github_signature_does_not_admit_a_gitea_delivery(
        self, monkeypatch, family
    ):
        monkeypatch.setenv("PRXREF_GITHUB_WEBHOOK_SECRET", SECRET)
        body, headers = _delivery(family, "opened")
        assert verify_signature(body, headers) == (False, "gitea secret not configured")

    @pytest.mark.parametrize("family", FAMILIES)
    def test_synchronized_is_reviewable_although_github_would_ignore_it(
        self, monkeypatch, family
    ):
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)
        monkeypatch.setenv("PRXREF_GITHUB_WEBHOOK_SECRET", SECRET)
        body, headers = _delivery(family, "synchronized")
        assert verify_signature(body, headers) == (True, PR_URL)
        github_view = _verify_github(body, {k.lower(): v for k, v in headers.items()})
        assert github_view == (
            False, "ignored: github pull_request action 'synchronized' is not reviewable"
        )

    @pytest.mark.parametrize("family", FAMILIES)
    def test_the_gitea_signature_decides_even_when_the_github_one_is_valid(
        self, monkeypatch, family
    ):
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", SECRET)
        body, headers = _delivery(family, "opened")
        wrong = _sign_like_the_forge(headers, body, "not-the-secret")
        for name in headers:
            if name.lower() in ("x-gitea-signature", "x-forgejo-signature"):
                headers[name] = wrong[name]
        assert verify_signature(body, headers) == (False, "gitea signature mismatch")


def _github_body(action: str, url: str = "https://github.com/owner/repo/pull/42") -> bytes:
    return json.dumps({"action": action, "pull_request": {"html_url": url}}).encode()


def _github_signed(body: bytes, secret: str = "gh-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


_GH_OPENED = _github_body("opened")
_GH_SYNC = _github_body("synchronize")
_GH_CLOSED = _github_body("closed")

GITHUB_ONLY_CASES = {
    "opened": (_GH_OPENED, {
        "X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _github_signed(_GH_OPENED),
    }, (True, "https://github.com/owner/repo/pull/42")),
    "synchronize": (_GH_SYNC, {
        "X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _github_signed(_GH_SYNC),
    }, (True, "https://github.com/owner/repo/pull/42")),
    "closed": (_GH_CLOSED, {
        "X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _github_signed(_GH_CLOSED),
    }, (False, "ignored: github pull_request action 'closed' is not reviewable")),
    "push": (_GH_OPENED, {
        "X-GitHub-Event": "push", "X-Hub-Signature-256": _github_signed(_GH_OPENED),
    }, (False, "ignored: github event 'push' is not pull_request")),
    "bad-signature": (_GH_OPENED, {
        "X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=" + "0" * 64,
    }, (False, "github signature mismatch")),
    "missing-signature": (_GH_OPENED, {
        "X-GitHub-Event": "pull_request",
    }, (False, "missing github signature header")),
    "bare-hex-signature": (_GH_OPENED, {
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": _github_signed(_GH_OPENED)[len("sha256="):],
    }, (False, "github signature mismatch")),
}


class TestGitHubOnlyDeliveriesAreUnchanged:
    """A request carrying only GitHub headers gets exactly the 0.23.0 verdict."""

    @pytest.mark.parametrize("case", sorted(GITHUB_ONLY_CASES))
    def test_the_verdict_matches_0_23_0(self, monkeypatch, case):
        monkeypatch.setenv("PRXREF_GITHUB_WEBHOOK_SECRET", "gh-secret")
        monkeypatch.setenv("PRXREF_GITEA_WEBHOOK_SECRET", "gh-secret")
        body, headers, expected = GITHUB_ONLY_CASES[case]
        assert verify_signature(body, headers) == expected
        assert verify_signature(body, headers) == _verify_github(
            body, {k.lower(): v for k, v in headers.items()}
        )

    def test_github_with_no_secret_still_names_github(self):
        body, headers, _expected = GITHUB_ONLY_CASES["opened"]
        assert verify_signature(body, headers) == (False, "github secret not configured")

    def test_github_unsigned_bypass_is_unchanged(self, monkeypatch):
        monkeypatch.setenv("PRXREF_ALLOW_UNSIGNED", "1")
        body, headers, _expected = GITHUB_ONLY_CASES["missing-signature"]
        assert verify_signature(body, headers) == (
            True, "unsigned:https://github.com/owner/repo/pull/42"
        )
