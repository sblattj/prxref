"""Tests for the multi-forge webhook receiver."""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import queue
import threading
import types
from http.server import ThreadingHTTPServer

import pytest

from prxref.webhooks import _worker_loop, make_webhook_handler, verify_signature

GH_SECRET = "gh-secret"
BB_SECRET = "bb-secret"
GL_SECRET = "gl-secret"

GH_URL = "https://github.com/owner/repo/pull/42"
BB_URL = "https://bitbucket.org/owner/repo/pull-requests/7"
GL_URL = "https://gitlab.com/group/repo/-/merge_requests/9"


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _gh_body(action: str = "opened", url: str = GH_URL) -> bytes:
    return json.dumps({"action": action, "pull_request": {"html_url": url}}).encode()


def _gh_headers(action: str = "opened") -> tuple[bytes, dict]:
    body = _gh_body(action)
    return body, {
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": _sign(GH_SECRET, body),
    }


def _bb_body(url: str = BB_URL) -> bytes:
    return json.dumps({"pullrequest": {"links": {"html": {"href": url}}}}).encode()


def _bb_headers(event: str = "pr:opened") -> tuple[bytes, dict]:
    body = _bb_body()
    return body, {"X-Event-Key": event, "X-Hub-Signature": _sign(BB_SECRET, body)}


def _gl_body(action: str = "open", url: str = GL_URL) -> bytes:
    return json.dumps({"object_attributes": {"url": url, "action": action}}).encode()


def _gl_headers(event: str = "MergeRequestHook") -> tuple[bytes, dict]:
    body = _gl_body()
    return body, {"X-Gitlab-Event": event, "X-Gitlab-Token": GL_SECRET}


class TestGitHubVerify:
    def test_good_signature_opened(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITHUB_WEBHOOK_SECRET", GH_SECRET)
        body, headers = _gh_headers()
        ok, detail = verify_signature(body, headers)
        assert ok is True
        assert detail == GH_URL

    def test_good_signature_synchronize(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITHUB_WEBHOOK_SECRET", GH_SECRET)
        body, headers = _gh_headers(action="synchronize")
        ok, detail = verify_signature(body, headers)
        assert ok is True
        assert detail == GH_URL

    def test_bad_signature(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITHUB_WEBHOOK_SECRET", GH_SECRET)
        body, headers = _gh_headers()
        headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert "signature" in reason

    def test_missing_signature_header(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITHUB_WEBHOOK_SECRET", GH_SECRET)
        body, headers = _gh_headers()
        del headers["X-Hub-Signature-256"]
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert "signature" in reason

    def test_secret_not_configured(self):
        body, headers = _gh_headers()
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert "secret" in reason

    def test_non_pr_event_ignored(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITHUB_WEBHOOK_SECRET", GH_SECRET)
        body = _gh_body()
        headers = {"X-GitHub-Event": "push", "X-Hub-Signature-256": _sign(GH_SECRET, body)}
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert reason.startswith("ignored:")

    def test_closed_action_ignored(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITHUB_WEBHOOK_SECRET", GH_SECRET)
        body, headers = _gh_headers(action="closed")
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert reason.startswith("ignored:")

    def test_missing_html_url(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITHUB_WEBHOOK_SECRET", GH_SECRET)
        body = json.dumps({"action": "opened", "pull_request": {}}).encode()
        headers = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sign(GH_SECRET, body)}
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert "html_url" in reason

    def test_headers_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITHUB_WEBHOOK_SECRET", GH_SECRET)
        body, headers = _gh_headers()
        lowered = {key.lower(): value for key, value in headers.items()}
        ok, detail = verify_signature(body, lowered)
        assert ok is True
        assert detail == GH_URL


class TestBitbucketVerify:
    def test_good_signature_pr_opened(self, monkeypatch):
        monkeypatch.setenv("PRXREF_BITBUCKET_WEBHOOK_SECRET", BB_SECRET)
        body, headers = _bb_headers("pr:opened")
        ok, detail = verify_signature(body, headers)
        assert ok is True
        assert detail == BB_URL

    def test_good_signature_pr_modified(self, monkeypatch):
        monkeypatch.setenv("PRXREF_BITBUCKET_WEBHOOK_SECRET", BB_SECRET)
        body, headers = _bb_headers("pr:modified")
        ok, detail = verify_signature(body, headers)
        assert ok is True
        assert detail == BB_URL

    def test_bad_signature(self, monkeypatch):
        monkeypatch.setenv("PRXREF_BITBUCKET_WEBHOOK_SECRET", BB_SECRET)
        body, headers = _bb_headers()
        headers["X-Hub-Signature"] = "sha256=" + "0" * 64
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert "signature" in reason

    def test_missing_signature_header(self, monkeypatch):
        monkeypatch.setenv("PRXREF_BITBUCKET_WEBHOOK_SECRET", BB_SECRET)
        body, headers = _bb_headers()
        del headers["X-Hub-Signature"]
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert "signature" in reason

    def test_comment_posted_ignored(self, monkeypatch):
        monkeypatch.setenv("PRXREF_BITBUCKET_WEBHOOK_SECRET", BB_SECRET)
        body, headers = _bb_headers("pr:comment:posted")
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert reason.startswith("ignored:")

    def test_missing_links_url(self, monkeypatch):
        monkeypatch.setenv("PRXREF_BITBUCKET_WEBHOOK_SECRET", BB_SECRET)
        body = json.dumps({"pullrequest": {"links": {}}}).encode()
        headers = {"X-Event-Key": "pr:opened", "X-Hub-Signature": _sign(BB_SECRET, body)}
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert "pullrequest.links.html.href" in reason


class TestGitLabVerify:
    def test_good_token_open(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITLAB_WEBHOOK_SECRET", GL_SECRET)
        body, headers = _gl_headers()
        ok, detail = verify_signature(body, headers)
        assert ok is True
        assert detail == GL_URL

    def test_good_token_update_action(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITLAB_WEBHOOK_SECRET", GL_SECRET)
        body = _gl_body(action="update")
        headers = {"X-Gitlab-Event": "MergeRequestHook", "X-Gitlab-Token": GL_SECRET}
        ok, detail = verify_signature(body, headers)
        assert ok is True
        assert detail == GL_URL

    def test_legacy_event_name_with_spaces(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITLAB_WEBHOOK_SECRET", GL_SECRET)
        body, headers = _gl_headers(event="Merge Request Hook")
        ok, detail = verify_signature(body, headers)
        assert ok is True
        assert detail == GL_URL

    def test_wrong_token(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITLAB_WEBHOOK_SECRET", GL_SECRET)
        body, headers = _gl_headers()
        headers["X-Gitlab-Token"] = "wrong-token"
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert "token" in reason

    def test_missing_token_header(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITLAB_WEBHOOK_SECRET", GL_SECRET)
        body, headers = _gl_headers()
        del headers["X-Gitlab-Token"]
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert "token" in reason

    def test_push_hook_ignored(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITLAB_WEBHOOK_SECRET", GL_SECRET)
        body, headers = _gl_headers(event="Push Hook")
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert reason.startswith("ignored:")

    def test_close_action_ignored(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITLAB_WEBHOOK_SECRET", GL_SECRET)
        body = _gl_body(action="close")
        headers = {"X-Gitlab-Event": "MergeRequestHook", "X-Gitlab-Token": GL_SECRET}
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert reason.startswith("ignored:")

    def test_missing_object_attributes_url(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITLAB_WEBHOOK_SECRET", GL_SECRET)
        body = json.dumps({"object_attributes": {"action": "open"}}).encode()
        headers = {"X-Gitlab-Event": "MergeRequestHook", "X-Gitlab-Token": GL_SECRET}
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert "object_attributes.url" in reason


class TestGenericVerify:
    def test_unknown_source(self):
        ok, reason = verify_signature(b"{}", {"Content-Type": "application/json"})
        assert ok is False
        assert reason == "unrecognized source"

    def test_invalid_json_payload(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITHUB_WEBHOOK_SECRET", GH_SECRET)
        body = b"this is not json"
        headers = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sign(GH_SECRET, body)}
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert "JSON" in reason


class TestUnsignedBypass:
    def test_bypass_on_github(self, monkeypatch):
        monkeypatch.setenv("PRXREF_ALLOW_UNSIGNED", "1")
        body = _gh_body()
        headers = {"X-GitHub-Event": "pull_request"}
        ok, detail = verify_signature(body, headers)
        assert ok is True
        assert detail == "unsigned:" + GH_URL

    def test_bypass_on_gitlab(self, monkeypatch):
        monkeypatch.setenv("PRXREF_ALLOW_UNSIGNED", "1")
        body = _gl_body()
        headers = {"X-Gitlab-Event": "MergeRequestHook"}
        ok, detail = verify_signature(body, headers)
        assert ok is True
        assert detail == "unsigned:" + GL_URL

    def test_bypass_off_rejects_unsigned(self):
        body = _gh_body()
        headers = {"X-GitHub-Event": "pull_request"}
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert "secret" in reason or "signature" in reason

    def test_bypass_requires_literal_1(self, monkeypatch):
        monkeypatch.setenv("PRXREF_ALLOW_UNSIGNED", "true")
        body = _gh_body()
        headers = {"X-GitHub-Event": "pull_request"}
        ok, _reason = verify_signature(body, headers)
        assert ok is False

    def test_bypass_does_not_accept_wrong_signature(self, monkeypatch):
        monkeypatch.setenv("PRXREF_ALLOW_UNSIGNED", "1")
        monkeypatch.setenv("PRXREF_GITHUB_WEBHOOK_SECRET", GH_SECRET)
        body, headers = _gh_headers()
        headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
        ok, reason = verify_signature(body, headers)
        assert ok is False
        assert "signature mismatch" in reason


class TestWorker:
    def test_drains_queue_in_order(self):
        q: queue.Queue = queue.Queue()
        seen: list[str] = []
        worker = threading.Thread(target=_worker_loop, args=(seen.append, q), daemon=True)
        worker.start()
        q.put("https://github.com/o/r/pull/1")
        q.put("https://gitlab.com/g/r/-/merge_requests/2")
        q.put(None)
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert seen == ["https://github.com/o/r/pull/1", "https://gitlab.com/g/r/-/merge_requests/2"]

    def test_survives_handler_exception(self):
        q: queue.Queue = queue.Queue()
        seen: list[str] = []

        def handler(url: str) -> None:
            if url == "boom":
                raise RuntimeError("review exploded")
            seen.append(url)

        worker = threading.Thread(target=_worker_loop, args=(handler, q), daemon=True)
        worker.start()
        q.put("boom")
        q.put("https://github.com/o/r/pull/3")
        q.put(None)
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert seen == ["https://github.com/o/r/pull/3"]


class _FakeConnection:
    def __init__(self, request_bytes: bytes):
        self._request_bytes = request_bytes
        self.wfile_buffer = io.BytesIO()

    def makefile(self, mode, *args, **kwargs):
        if mode.startswith("r"):
            return io.BytesIO(self._request_bytes)
        return self.wfile_buffer

    def sendall(self, data):
        self.wfile_buffer.write(data)


def _build_request(method: str, path: str, body: bytes = b"", headers: dict | None = None) -> bytes:
    lines = [f"{method} {path} HTTP/1.1", "Host: localhost"]
    for key, value in (headers or {}).items():
        lines.append(f"{key}: {value}")
    lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def _quiet_handler(review_queue: queue.Queue):
    base = make_webhook_handler(review_queue)

    class QuietHandler(base):
        def log_message(self, fmt, *args):
            pass

    return QuietHandler


def _perform(handler_cls, method: str, path: str, body: bytes = b"", headers: dict | None = None) -> bytes:
    conn = _FakeConnection(_build_request(method, path, body, headers))
    handler_cls(conn, ("127.0.0.1", 0), types.SimpleNamespace())
    return conn.wfile_buffer.getvalue()


def _parse_response(raw: bytes) -> tuple[int, dict, bytes]:
    head, _, response_body = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        if ": " in line:
            key, value = line.split(": ", 1)
            headers[key] = value
    return status, headers, response_body


class TestHTTPEndpoints:
    def test_health(self):
        raw = _perform(_quiet_handler(queue.Queue()), "GET", "/health")
        status, _, body = _parse_response(raw)
        assert status == 200
        assert json.loads(body) == {"ok": True}

    def test_get_unknown_path_404(self):
        raw = _perform(_quiet_handler(queue.Queue()), "GET", "/nope")
        status, _, _ = _parse_response(raw)
        assert status == 404

    def test_post_unknown_path_404(self):
        raw = _perform(_quiet_handler(queue.Queue()), "POST", "/nope")
        status, _, _ = _parse_response(raw)
        assert status == 404

    def test_webhook_github_accepted_and_queued(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITHUB_WEBHOOK_SECRET", GH_SECRET)
        body = _gh_body()
        headers = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sign(GH_SECRET, body)}
        q: queue.Queue = queue.Queue()
        raw = _perform(_quiet_handler(q), "POST", "/webhook", body, headers)
        status, _, response_body = _parse_response(raw)
        assert status == 202
        assert json.loads(response_body) == {"queued": True}
        assert q.get_nowait() == GH_URL

    def test_webhook_gitlab_token_accepted_and_queued(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITLAB_WEBHOOK_SECRET", GL_SECRET)
        body = _gl_body()
        headers = {"X-Gitlab-Event": "MergeRequestHook", "X-Gitlab-Token": GL_SECRET}
        q: queue.Queue = queue.Queue()
        raw = _perform(_quiet_handler(q), "POST", "/webhook", body, headers)
        status, _, _ = _parse_response(raw)
        assert status == 202
        assert q.get_nowait() == GL_URL

    def test_webhook_bad_signature_401(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITHUB_WEBHOOK_SECRET", GH_SECRET)
        body = _gh_body()
        headers = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=" + "0" * 64}
        q: queue.Queue = queue.Queue()
        raw = _perform(_quiet_handler(q), "POST", "/webhook", body, headers)
        status, _, _ = _parse_response(raw)
        assert status == 401
        assert q.empty()

    def test_webhook_unknown_source_400(self):
        q: queue.Queue = queue.Queue()
        raw = _perform(_quiet_handler(q), "POST", "/webhook", b"{}", {"Content-Type": "application/json"})
        status, _, _ = _parse_response(raw)
        assert status == 400
        assert q.empty()

    def test_webhook_ignored_event_not_queued(self, monkeypatch):
        monkeypatch.setenv("PRXREF_BITBUCKET_WEBHOOK_SECRET", BB_SECRET)
        body = _bb_body()
        headers = {"X-Event-Key": "pr:comment:posted", "X-Hub-Signature": _sign(BB_SECRET, body)}
        q: queue.Queue = queue.Queue()
        raw = _perform(_quiet_handler(q), "POST", "/webhook", body, headers)
        status, _, response_body = _parse_response(raw)
        assert status == 202
        assert json.loads(response_body)["queued"] is False
        assert q.empty()

    def test_webhook_unsigned_bypass_note_header(self, monkeypatch):
        monkeypatch.setenv("PRXREF_ALLOW_UNSIGNED", "1")
        body = _gh_body()
        headers = {"X-GitHub-Event": "pull_request"}
        q: queue.Queue = queue.Queue()
        raw = _perform(_quiet_handler(q), "POST", "/webhook", body, headers)
        status, response_headers, response_body = _parse_response(raw)
        assert status == 202
        assert json.loads(response_body) == {"queued": True}
        assert "X-Prxref-Warning" in response_headers
        assert q.get_nowait() == GH_URL

    def test_serve_smoke_binds_ephemeral_port(self):
        review_queue: queue.Queue = queue.Queue()
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_webhook_handler(review_queue))
        assert server.server_address[1] > 0
        server.server_close()


BBS_URL = "https://bitbucket.corp.example/projects/PLAT/repos/api/pull-requests/42"


def _bb_cloud_body(url: str = BB_URL) -> bytes:
    """A real Bitbucket Cloud payload."""
    return json.dumps({"pullrequest": {"links": {"html": {"href": url}}}}).encode()


def _bb_server_body(url: str = BBS_URL) -> bytes:
    """A real Bitbucket Server / Data Center payload.

    Different key casing (pullRequest) and the browsable link is the first
    entry of a links.self list rather than a links.html object.
    """
    return json.dumps({"pullRequest": {"links": {"self": [{"href": url}]}}}).encode()


class TestBitbucketDialects:
    """Cloud and Server both arrive as X-Event-Key and must both be accepted.

    These pair each product's real event names with its real payload shape.
    The older cases above pair a Server event key with a Cloud payload — a
    combination neither product sends, which is how the mismatch went unnoticed:
    only the Server event names were accepted, against a Cloud-only extractor,
    so every genuine Cloud webhook was rejected as "not reviewable" and every
    genuine Server webhook yielded no URL.
    """

    @pytest.mark.parametrize("event", ["pullrequest:created", "pullrequest:updated"])
    def test_real_cloud_event_and_payload(self, monkeypatch, event):
        monkeypatch.setenv("PRXREF_BITBUCKET_WEBHOOK_SECRET", BB_SECRET)
        body = _bb_cloud_body()
        headers = {"X-Event-Key": event, "X-Hub-Signature": _sign(BB_SECRET, body)}
        ok, detail = verify_signature(body, headers)
        assert ok is True
        assert detail == BB_URL

    @pytest.mark.parametrize(
        "event", ["pr:opened", "pr:modified", "pr:from_ref_updated"]
    )
    def test_real_server_event_and_payload(self, monkeypatch, event):
        monkeypatch.setenv("PRXREF_BITBUCKET_WEBHOOK_SECRET", BB_SECRET)
        body = _bb_server_body()
        headers = {"X-Event-Key": event, "X-Hub-Signature": _sign(BB_SECRET, body)}
        ok, detail = verify_signature(body, headers)
        assert ok is True
        assert detail == BBS_URL

    def test_server_payload_without_a_self_link_is_reported(self, monkeypatch):
        monkeypatch.setenv("PRXREF_BITBUCKET_WEBHOOK_SECRET", BB_SECRET)
        body = json.dumps({"pullRequest": {"links": {"self": []}}}).encode()
        headers = {"X-Event-Key": "pr:opened", "X-Hub-Signature": _sign(BB_SECRET, body)}
        ok, detail = verify_signature(body, headers)
        assert ok is False
        assert "missing" in detail

    def test_an_unreviewable_bitbucket_event_is_still_ignored(self, monkeypatch):
        monkeypatch.setenv("PRXREF_BITBUCKET_WEBHOOK_SECRET", BB_SECRET)
        body = _bb_cloud_body()
        headers = {
            "X-Event-Key": "pullrequest:comment_created",
            "X-Hub-Signature": _sign(BB_SECRET, body),
        }
        ok, detail = verify_signature(body, headers)
        assert ok is False
        assert detail.startswith("ignored:")

    def test_a_server_webhook_url_routes_to_the_server_forge(self, monkeypatch):
        # End to end: the URL the webhook hands off must be one detect_forge
        # resolves, or the review dies one step later with "unknown forge".
        from prxref.forges.base import detect_forge

        monkeypatch.setenv("PRXREF_BITBUCKET_WEBHOOK_SECRET", BB_SECRET)
        body = _bb_server_body()
        headers = {"X-Event-Key": "pr:opened", "X-Hub-Signature": _sign(BB_SECRET, body)}
        ok, url = verify_signature(body, headers)
        assert ok is True
        ref = detect_forge(url)
        assert ref is not None
        assert ref.forge == "bitbucket-server"
        assert ref.owner == "PLAT"
        assert ref.number == 42
