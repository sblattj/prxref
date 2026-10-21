"""The gate's webhook intake: verify, allowlist, dedup, enqueue, answer 202 (#102).

No GitHub call and no inference happens here. A delivery is accepted only
when ``webhooks._check_hmac`` returns ``"ok"``: its ``"unsigned"`` state,
which ``PRXREF_ALLOW_UNSIGNED=1`` produces for the advisory daemon, is a 401
here. The previous secret is tried only inside the rotation grace window,
measured from this process's start.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prxref.gate.settings import WEBHOOK_SECRET_ENV, WEBHOOK_SECRET_PREVIOUS_ENV, GateSettings
from prxref.gate.store import Generation, Store
from prxref.webhooks import _check_hmac

logger = logging.getLogger(__name__)

CHECK_NAME = "sdlc-review"
MAX_BODY_BYTES = 25 * 1024 * 1024
_PR_ENQUEUE_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})
_HANDLED_EVENTS = frozenset({"pull_request", "check_run", "check_suite"})


@dataclass(frozen=True)
class Intent:
    """What one delivery asks the gate to do."""

    kind: str
    pr_number: int
    head_sha: str = ""
    base_sha: str = ""
    base_ref: str = ""
    reason: str = ""


def _rerequest(head_sha: str, prs: list, *, repo: str, store: Store) -> Intent | None:
    if prs:
        pr = prs[0]
        base = pr.get("base") or {}
        return Intent(
            "enqueue", int(pr["number"]), head_sha, str(base.get("sha", "")), str(base.get("ref", "")), "rerequest"
        )
    known = store.latest_generation_for_head(repo, head_sha)
    if known is None:
        return None
    return Intent("enqueue", known.pr_number, head_sha, known.base_sha, known.base_ref, "rerequest")


def map_event(event: str, payload: dict, *, app_id: int, repo: str, store: Store) -> Intent | None:
    """Map one verified, allowlisted delivery to an :class:`Intent`, or None to ignore it."""
    action = payload.get("action")
    if event == "pull_request":
        pr = payload.get("pull_request") or {}
        head, base = pr.get("head") or {}, pr.get("base") or {}
        number = int(pr.get("number") or payload.get("number"))
        if action == "closed":
            return Intent("cancel", number)
        reason = action if action in _PR_ENQUEUE_ACTIONS else None
        if action == "edited" and "base" in (payload.get("changes") or {}):
            reason = "retarget"
        if reason is None:
            return None
        return Intent("enqueue", number, str(head["sha"]), str(base["sha"]), str(base["ref"]), reason)
    if event in ("check_run", "check_suite") and action == "rerequested":
        body = payload.get(event) or {}
        if int((body.get("app") or {}).get("id", -1)) != app_id:
            return None
        if event == "check_run" and body.get("name") != CHECK_NAME:
            return None
        return _rerequest(str(body["head_sha"]), body.get("pull_requests") or [], repo=repo, store=store)
    return None


class Receiver:
    """Pure request handling; :func:`serve_http` puts it on a socket.

    ``on_enqueue`` is called with each generation the receiver enqueues: the
    AWS receiver uses it to send the generation to SQS; the local gate leaves
    it unset, since its worker polls the store.
    """

    def __init__(
        self,
        store: Store,
        settings: GateSettings,
        *,
        started_at: float,
        clock: Callable[[], float] = time.time,
        on_enqueue: Callable[[Generation], None] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self._started_at = started_at
        self._clock = clock
        self._on_enqueue = on_enqueue

    def _verify(self, body: bytes, headers: dict) -> str:
        state, _ = _check_hmac(body, headers, "x-hub-signature-256", WEBHOOK_SECRET_ENV, "gate")
        if state == "ok":
            return "current"
        grace_open = self._clock() - self._started_at < self.settings.secret_grace_minutes * 60
        if grace_open:
            state, _ = _check_hmac(body, headers, "x-hub-signature-256", WEBHOOK_SECRET_PREVIOUS_ENV, "gate")
            if state == "ok":
                logger.warning(
                    "delivery %s matched only the previous webhook secret", headers.get("x-github-delivery", "?")
                )
                return "previous"
        return "reject"

    def health(self) -> dict:
        return {
            "queue_depth": self.store.queue_depth(),
            "lease": self.store.lease_info(),
            "last_publish": self.store.last_publish_time(),
            "paused": self.store.is_paused(),
            "last_auth_failure": self.store.last_auth_failure(),
        }

    def handle(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict]:
        """Answer one HTTP request with ``(status, json_body)``."""
        h = {k.lower(): v for k, v in headers.items()}
        if method == "GET" and path == "/health":
            if "cf-connecting-ip" in h:
                # Every request through the tunnel carries this header; /health (lease
                # holder, last auth failure) is for a local curl only.
                return 404, {"error": "not found"}
            return 200, self.health()
        if method != "POST" or path != "/webhook":
            return 404, {"error": "not found"}
        if self._verify(body, h) == "reject":
            return 401, {"error": "signature rejected"}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return 400, {"error": "body is not JSON"}
        if not isinstance(payload, dict):
            return 400, {"error": "body is not a JSON object"}
        delivery = h.get("x-github-delivery", "").strip()
        event = h.get("x-github-event", "").strip()
        if not delivery or not event:
            return 400, {"error": "missing X-GitHub-Delivery or X-GitHub-Event"}
        if event not in _HANDLED_EVENTS:
            logger.info("delivery %s: event %s ignored", delivery, event)
            return 202, {"status": "ignored", "delivery": delivery}
        installation = (payload.get("installation") or {}).get("id")
        repo = str((payload.get("repository") or {}).get("full_name") or "").lower()
        if installation not in self.settings.installation_ids or repo not in self.settings.repos:
            return 403, {"error": "installation or repository not allowlisted"}
        try:
            intent = map_event(event, payload, app_id=self.settings.app_id, repo=repo, store=self.store)
        except (KeyError, TypeError, ValueError):
            return 400, {"error": "payload is missing a required field"}
        if not self.store.record_delivery(
            delivery, event, payload.get("action"), repo, intent.pr_number if intent else None
        ):
            return 202, {"status": "duplicate", "delivery": delivery}
        if intent is None:
            self.store.mark_delivery_handled(delivery)
            logger.info("delivery %s: %s %s ignored", delivery, event, payload.get("action"))
            return 202, {"status": "ignored", "delivery": delivery}
        if intent.kind == "cancel":
            self.store.cancel_pr(repo, intent.pr_number)
        else:
            gen = self.store.enqueue(
                repo, intent.pr_number, intent.head_sha, intent.base_sha, intent.base_ref, intent.reason
            )
            if self._on_enqueue is not None:
                self._on_enqueue(gen)
        self.store.mark_delivery_handled(delivery)
        return 202, {"status": "accepted", "delivery": delivery}


def serve_http(receiver: Receiver, host: str, port: int) -> ThreadingHTTPServer:
    """Bind a threading HTTP server that hands every request to ``receiver.handle``."""

    class _Handler(BaseHTTPRequestHandler):
        timeout = 30  # applied to the socket: a slow sender cannot hold a thread

        def _reply(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method: str, body: bytes) -> None:
            try:
                result = receiver.handle(method, self.path, dict(self.headers), body)
            except Exception:
                delivery = self.headers.get("X-GitHub-Delivery", "?")
                logger.exception("handler failed: %s %s delivery=%s", method, self.path, delivery)
                result = (500, {"error": "internal"})
            self._reply(*result)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET", b"")

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.close_connection = True
                self._reply(400, {"error": "missing or invalid Content-Length"})
                return
            if length < 0:
                self.close_connection = True
                self._reply(400, {"error": "invalid Content-Length"})
                return
            if length > MAX_BODY_BYTES:
                self.close_connection = True
                self._reply(413, {"error": "body too large"})
                return
            self._dispatch("POST", self.rfile.read(length))

        def log_message(self, fmt: str, *args) -> None:
            logger.info("%s %s", self.address_string(), fmt % args)

    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    return server
