"""Webhook receiver for GitHub, Bitbucket, and GitLab PR events.

POST /webhook verifies the forge-specific signature, extracts the PR URL
from the payload, and enqueues it; a single background daemon worker
drains the queue serially so webhook responses return immediately (202)
instead of blocking on multi-minute reviews. GET /health is a liveness
probe. The review callable is wired in by the caller (forge + LLM +
orchestrate_review); this module knows nothing about how reviews run.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import queue
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)

_GITHUB_SECRET_ENV = "PRXREF_GITHUB_WEBHOOK_SECRET"
_BITBUCKET_SECRET_ENV = "PRXREF_BITBUCKET_WEBHOOK_SECRET"
_GITLAB_SECRET_ENV = "PRXREF_GITLAB_WEBHOOK_SECRET"
_ALLOW_UNSIGNED_ENV = "PRXREF_ALLOW_UNSIGNED"
_UNSIGNED_PREFIX = "unsigned:"

_GITHUB_ACTIONS = ("opened", "synchronize")
# Bitbucket Cloud and Bitbucket Server both announce themselves with
# X-Event-Key, but they do not share an event vocabulary. Cloud sends
# pullrequest:created / pullrequest:updated; Server sends pr:opened /
# pr:modified, plus pr:from_ref_updated when the source branch moves.
# Only the Server names were listed here, against a payload extractor that
# reads Cloud's shape — so a real Cloud webhook was rejected as "not
# reviewable" while a real Server webhook failed to yield a URL.
_BITBUCKET_CLOUD_EVENTS = ("pullrequest:created", "pullrequest:updated")
_BITBUCKET_SERVER_EVENTS = ("pr:opened", "pr:modified", "pr:from_ref_updated")
_BITBUCKET_EVENTS = _BITBUCKET_CLOUD_EVENTS + _BITBUCKET_SERVER_EVENTS
_GITLAB_ACTIONS = ("open", "update")


def verify_signature(body: bytes, headers: dict) -> tuple[bool, str]:
    """Verify a webhook from any supported forge and extract its PR URL.

    The source forge is detected from its event header (X-GitHub-Event,
    X-Event-Key, or X-Gitlab-Event; header names are case-insensitive).
    Bitbucket Cloud and Bitbucket Server are both recognized by X-Event-Key
    and share one verification path; their event names and payload shapes
    differ and both are accepted. Signature is checked per forge: GitHub
    HMAC-SHA256 in X-Hub-Signature-256, Bitbucket HMAC-SHA256 in X-Hub-Signature,
    GitLab plain token in X-Gitlab-Token — each against its
    PRXREF_<FORGE>_WEBHOOK_SECRET env var. Only PR-open/update events are
    reviewable; anything else is ignored.

    Returns (True, pr_url) on success. When PRXREF_ALLOW_UNSIGNED=1 and no
    secret/signature is available, returns (True, "unsigned:<pr_url>") so
    callers can flag the dev bypass. Returns (False, reason) on failure;
    the reason classifies the HTTP status: reasons mentioning signature,
    token, or secret map to 401; "ignored:" reasons are valid but
    unreviewable events; everything else maps to 400.
    """
    normalized = {str(key).lower(): value for key, value in headers.items()}
    if "x-github-event" in normalized:
        return _verify_github(body, normalized)
    if "x-event-key" in normalized:
        return _verify_bitbucket(body, normalized)
    if "x-gitlab-event" in normalized:
        return _verify_gitlab(body, normalized)
    return False, "unrecognized source"


def make_webhook_handler(review_queue: queue.Queue) -> type[BaseHTTPRequestHandler]:
    """Build an HTTP request handler class bound to the given review queue."""

    class WebhookHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                self._respond(200, {"ok": True})
            else:
                self._respond(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path == "/webhook":
                self._handle_webhook()
            else:
                self._respond(404, {"error": "not found"})

        def _handle_webhook(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            headers = {key: value for key, value in self.headers.items()}
            ok, detail = verify_signature(body, headers)
            if not ok:
                status, payload = _status_for_reason(detail)
                self._respond(status, payload)
                return
            extra_headers = None
            if detail.startswith(_UNSIGNED_PREFIX):
                detail = detail[len(_UNSIGNED_PREFIX):]
                extra_headers = {
                    "X-Prxref-Warning": "accepted unsigned webhook (PRXREF_ALLOW_UNSIGNED=1)"
                }
                logger.warning("Accepted unsigned webhook for %s — dev bypass", detail)
            self._respond(202, {"queued": True}, extra_headers)
            review_queue.put(detail)

        def _respond(
            self,
            status: int,
            payload: dict,
            extra_headers: dict | None = None,
        ) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

    return WebhookHandler


def serve(handler: Callable[[str], None], host: str = "0.0.0.0", port: int = 8080) -> None:
    """Run the webhook server until interrupted.

    ``handler`` is called once per accepted webhook with the PR URL,
    serially on one background daemon thread, so reviews never overlap.
    """
    if _allow_unsigned():
        logger.warning(
            "PRXREF_ALLOW_UNSIGNED=1 — accepting unsigned webhooks. INSECURE: do not use in production."
        )
    review_queue: queue.Queue = queue.Queue()
    threading.Thread(
        target=_worker_loop, args=(handler, review_queue), daemon=True
    ).start()
    server = ThreadingHTTPServer((host, port), make_webhook_handler(review_queue))
    logger.info("prxref webhook server listening on %s:%d", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _allow_unsigned() -> bool:
    return os.environ.get(_ALLOW_UNSIGNED_ENV, "").strip() == "1"


def _parse_json(body: bytes) -> dict | None:
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _check_hmac(
    body: bytes,
    headers: dict,
    header: str,
    secret_env: str,
    forge: str,
) -> tuple[str, str | None]:
    signature = headers.get(header, "")
    secret = os.environ.get(secret_env)
    if not signature or not secret:
        if _allow_unsigned():
            return "unsigned", None
        if not secret:
            return "reject", f"{forge} secret not configured"
        return "reject", f"missing {forge} signature header"
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return "reject", f"{forge} signature mismatch"
    return "ok", None


def _result(state: str, url: str) -> tuple[bool, str]:
    if state == "unsigned":
        return True, _UNSIGNED_PREFIX + url
    return True, url


def _verify_github(body: bytes, h: dict) -> tuple[bool, str]:
    state, reason = _check_hmac(body, h, "x-hub-signature-256", _GITHUB_SECRET_ENV, "github")
    if state == "reject":
        return False, reason or "github signature verification failed"
    event = h.get("x-github-event", "")
    if event != "pull_request":
        return False, f"ignored: github event {event!r} is not pull_request"
    payload = _parse_json(body)
    if payload is None:
        return False, "invalid JSON payload"
    action = payload.get("action", "")
    if action not in _GITHUB_ACTIONS:
        return False, f"ignored: github pull_request action {action!r} is not reviewable"
    url = (payload.get("pull_request") or {}).get("html_url")
    if not url:
        return False, "github payload missing pull_request.html_url"
    return _result(state, url)


def _verify_bitbucket(body: bytes, h: dict) -> tuple[bool, str]:
    state, reason = _check_hmac(body, h, "x-hub-signature", _BITBUCKET_SECRET_ENV, "bitbucket")
    if state == "reject":
        return False, reason or "bitbucket signature verification failed"
    event = h.get("x-event-key", "")
    if event not in _BITBUCKET_EVENTS:
        return False, f"ignored: bitbucket event {event!r} is not reviewable"
    payload = _parse_json(body)
    if payload is None:
        return False, "invalid JSON payload"
    url = _bitbucket_pr_url(payload)
    if not url:
        return False, (
            "bitbucket payload missing pullrequest.links.html.href "
            "(Cloud) or pullRequest.links.self[].href (Server)"
        )
    return _result(state, url)


def _bitbucket_pr_url(payload: dict) -> str | None:
    """Extract the PR URL from either a Cloud or a Server webhook payload.

    Cloud nests the browsable link under pullrequest.links.html.href. Server
    uses a differently-cased pullRequest key and exposes the link as the first
    entry of a links.self list.
    """
    cloud = payload.get("pullrequest") or {}
    url = ((cloud.get("links") or {}).get("html") or {}).get("href")
    if url:
        return url

    server = payload.get("pullRequest") or {}
    self_links = (server.get("links") or {}).get("self") or []
    for link in self_links:
        if isinstance(link, dict) and link.get("href"):
            return link["href"]
    return None


def _verify_gitlab(body: bytes, h: dict) -> tuple[bool, str]:
    token = h.get("x-gitlab-token", "")
    secret = os.environ.get(_GITLAB_SECRET_ENV)
    unsigned = False
    if not token or not secret:
        if not _allow_unsigned():
            if not secret:
                return False, "gitlab secret not configured"
            return False, "missing gitlab token header"
        unsigned = True
    elif not hmac.compare_digest(token, secret):
        return False, "gitlab token mismatch"
    event = h.get("x-gitlab-event", "")
    if event.replace(" ", "") != "MergeRequestHook":
        return False, f"ignored: gitlab event {event!r} is not MergeRequestHook"
    payload = _parse_json(body)
    if payload is None:
        return False, "invalid JSON payload"
    attrs = payload.get("object_attributes") or {}
    action = attrs.get("action", "")
    if action not in _GITLAB_ACTIONS:
        return False, f"ignored: gitlab merge request action {action!r} is not reviewable"
    url = attrs.get("url")
    if not url:
        return False, "gitlab payload missing object_attributes.url"
    return _result("unsigned" if unsigned else "ok", url)


def _status_for_reason(reason: str) -> tuple[int, dict]:
    if reason.startswith("ignored:"):
        return 202, {"queued": False, "reason": reason}
    if any(token in reason for token in ("signature", "token", "secret")):
        return 401, {"error": reason}
    return 400, {"error": reason}


def _worker_loop(handler: Callable[[str], None], review_queue: queue.Queue) -> None:
    while True:
        url = review_queue.get()
        if url is None:
            review_queue.task_done()
            return
        try:
            handler(url)
        except Exception as exc:  # noqa: BLE001
            logger.error("Review failed for %s: %s", url, exc)
        finally:
            review_queue.task_done()
