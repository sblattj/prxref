"""The review gate on AWS Lambda: three handlers over the same gate code (#102).

- :func:`receiver_handler` (API Gateway HTTP API, payload v2): verify, map,
  record and enqueue exactly as :class:`~prxref.gate.receiver.Receiver` does
  locally, then send ``{"generation_id": id}`` to the SQS FIFO queue.
- :func:`worker_handler` (SQS, batch size 1, reserved concurrency 1): take the
  lease, run :func:`~prxref.gate.worker.run_generation`, release the lease.
- :func:`watchdog_handler` (EventBridge, every minute): the local watchdog's
  expiry repair and reconcile, then one SQS message per queued generation.

State lives in :class:`~prxref.gate.dynamo_store.DynamoStore`. The GitHub App
key and the webhook secret come from Secrets Manager once per cold start and
are registered for redaction. The model is reached through litellm with the
function's execution role: no ``AWS_BEARER_TOKEN_BEDROCK`` here.

Lambda-only environment (plain names, not prxref config keys):
``GATE_TABLE_NAME``, ``GATE_QUEUE_URL``, ``GATE_APP_KEY_SECRET_ARN`` (worker,
watchdog) and ``GATE_WEBHOOK_SECRET_ARN`` (receiver). The app id,
installation ids, repos and review settings are the usual ``PRXREF_GATE_*``
keys.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prxref.config import load_config
from prxref.gate import require_jwt
from prxref.gate.checks import Lease
from prxref.gate.dynamo_store import DynamoStore
from prxref.gate.receiver import Receiver
from prxref.gate.settings import WEBHOOK_SECRET_ENV, GateSettings, lambda_settings
from prxref.gate.store import Generation, StateError
from prxref.gate.worker import ReviewOutcome, register_secret, run_generation

logger = logging.getLogger(__name__)

TABLE_ENV = "GATE_TABLE_NAME"
QUEUE_ENV = "GATE_QUEUE_URL"
APP_KEY_SECRET_ENV = "GATE_APP_KEY_SECRET_ARN"
WEBHOOK_SECRET_ARN_ENV = "GATE_WEBHOOK_SECRET_ARN"
RUNS_DIR = Path("/tmp/prxref-gate-runs")
LEASE_TTL_S = 840.0
DEADLINE_PAD_S = 120.0

_cache: dict[str, Any] = {}


class LeaseHeld(RuntimeError):
    """Another holder has the live lease; raised so SQS redelivers the message later."""


def _reset() -> None:
    """Forget every cold-start object (tests)."""
    _cache.clear()


def _cached(name: str, build: Callable[[], Any]) -> Any:
    if name not in _cache:
        _cache[name] = build()
    return _cache[name]


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def _boto(service: str) -> Any:
    import boto3

    return boto3.client(service)


def _logging() -> None:
    """On Lambda: INFO to CloudWatch, every record redacted. Left alone elsewhere (tests)."""
    from prxref.gate.serve import _install_redaction

    if not os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return
    logging.getLogger().setLevel(logging.INFO)
    _install_redaction()


def _secret(env_name: str) -> str:
    """The SecretString of the secret whose ARN is in ``env_name``; fetched once, registered for redaction."""

    def fetch() -> str:
        value = _cached("secretsmanager", lambda: _boto("secretsmanager")).get_secret_value(SecretId=_env(env_name))[
            "SecretString"
        ]
        register_secret(value)
        return value

    return _cached(f"secret:{env_name}", fetch)


def _config() -> tuple[GateSettings, dict]:
    def build() -> tuple[GateSettings, dict]:
        _logging()
        os.environ.setdefault("PRXREF_RULES_SOURCE", "base")
        cfg = load_config()
        if cfg.get("llm_api_key"):
            register_secret(cfg["llm_api_key"])
        return lambda_settings(cfg, RUNS_DIR), cfg

    return _cached("config", build)


def _store() -> DynamoStore:
    return _cached("store", lambda: DynamoStore(_env(TABLE_ENV), _cached("dynamodb", lambda: _boto("dynamodb"))))


def send_generation(sqs: Any, queue_url: str, gen: Generation) -> None:
    """Send ``{"generation_id": id}`` in the PR's FIFO group.

    The first send of a generation dedupes on its id. A re-queued one has a
    higher ``attempts``, which goes into the dedupe id, so the FIFO 5-minute
    dedupe window cannot swallow it.
    """
    dedupe = str(gen.id) if gen.attempts == 0 else f"{gen.id}-{gen.attempts}"
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({"generation_id": gen.id}),
        MessageGroupId=f"{gen.repo}#{gen.pr_number}",
        MessageDeduplicationId=dedupe,
    )


def _sender() -> Callable[[Generation], None]:
    sqs = _cached("sqs", lambda: _boto("sqs"))
    queue_url = _env(QUEUE_ENV)
    return lambda gen: send_generation(sqs, queue_url, gen)


# -- receiver -------------------------------------------------------------------


def _receiver() -> Receiver:
    def build() -> Receiver:
        settings, _cfg = _config()
        os.environ[WEBHOOK_SECRET_ENV] = _secret(WEBHOOK_SECRET_ARN_ENV)
        return Receiver(_store(), settings, started_at=time.time(), on_enqueue=_sender())

    return _cached("receiver", build)


def _reply(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def receiver_handler(event: dict, context: Any) -> dict:
    """API Gateway HTTP API (v2) ``POST /webhook``; the raw body is what the HMAC covers."""
    http = (event.get("requestContext") or {}).get("http") or {}
    method = str(http.get("method") or "POST").upper()
    path = str(event.get("rawPath") or http.get("path") or "")
    if method != "POST" or path != "/webhook":
        return _reply(404, {"error": "not found"})
    body = event.get("body") or ""
    try:
        raw = base64.b64decode(body, validate=True) if event.get("isBase64Encoded") else body.encode("utf-8")
    except ValueError:
        return _reply(400, {"error": "body is not valid base64"})
    headers = {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}
    try:
        status, reply = _receiver().handle("POST", "/webhook", headers, raw)
    except Exception:
        logger.exception("receiver failed: delivery=%s", headers.get("x-github-delivery", "?"))
        return _reply(500, {"error": "internal"})
    return _reply(status, reply)


# -- worker -----------------------------------------------------------------------


@dataclass
class WorkerDeps:
    """What the worker handler needs, built once per cold start."""

    store: Any
    publisher: Any
    client: Any
    review: Callable[..., ReviewOutcome]
    settings: GateSettings
    effort: str
    send: Callable[[Generation], None]


def _github(settings: GateSettings) -> tuple[Any, Any, Any]:
    """App auth, the GitHub client and the session, from the key in Secrets Manager."""
    import requests

    from prxref.gate.app_auth import AppAuth
    from prxref.gate.checks import GitHubClient

    require_jwt()
    session = requests.Session()
    auth = AppAuth(settings.app_id, _secret(APP_KEY_SECRET_ENV), settings.installation_ids, session=session)
    return auth, GitHubClient(auth, session), session


def _worker_deps() -> WorkerDeps:
    def build() -> WorkerDeps:
        from prxref.gate.binding import make_guard
        from prxref.gate.checks import Publisher
        from prxref.gate.serve import make_review

        settings, cfg = _config()
        store = _store()
        auth, client, _session = _github(settings)
        publisher = Publisher(
            client, store, app_id=settings.app_id, bot_login=auth.bot_login(), guard=make_guard(client, store)
        )
        return WorkerDeps(
            store=store,
            publisher=publisher,
            client=client,
            review=make_review(auth, settings, cfg["rules_source"]),
            settings=settings,
            effort=cfg["llm_reasoning_effort"] or "default",
            send=_sender(),
        )

    return _cached("worker", build)


def process_generation(gen_id: int, context: Any, deps: WorkerDeps) -> str:
    """Run one generation under the lease; returns its final state or why it was skipped.

    Raises :class:`LeaseHeld` (or a :class:`~prxref.gate.store.LeaseLost`) so SQS
    redelivers the message after its visibility timeout.
    """
    store = deps.store
    try:
        gen = store.get(gen_id)
    except StateError:
        logger.warning("generation %s does not exist; message dropped", gen_id)
        return "missing"
    if gen.state not in ("queued", "in_progress"):
        logger.info("generation %s is %s; redelivery is a no-op", gen_id, gen.state)
        return f"skipped:{gen.state}"
    if store.is_paused():
        logger.info("gate paused; generation %s stays queued for the watchdog to resend", gen_id)
        return "paused"
    holder = str(getattr(context, "aws_request_id", "") or f"lambda:{time.time()}")
    token = store.acquire_lease(holder, LEASE_TTL_S)
    if token is None:
        raise LeaseHeld(f"generation {gen_id}: the worker lease is held; SQS will redeliver")
    lease = Lease(holder, token, LEASE_TTL_S)
    try:
        for orphan in store.adopt_orphans(token):
            logger.warning("re-queued in-progress generation %s of a previous worker", orphan)
            if orphan != gen_id:
                deps.send(store.get(orphan))
        gen = store.get(gen_id)
        if gen.state != "queued":
            logger.info("generation %s is %s; nothing to run", gen_id, gen.state)
            return f"skipped:{gen.state}"
        remaining = context.get_remaining_time_in_millis() / 1000.0
        deadline = min(float(deps.settings.review_deadline), remaining - DEADLINE_PAD_S)
        if deadline <= 0:
            raise RuntimeError(f"generation {gen_id}: {remaining:.0f}s left is too little to review")
        return run_generation(
            gen,
            store=store,
            publisher=deps.publisher,
            client=deps.client,
            lease=lease,
            review=deps.review,
            deadline_s=deadline,
            effort=deps.effort,
        )
    finally:
        store.release_lease(holder, token)


def worker_handler(event: dict, context: Any) -> dict:
    """SQS batch (size 1): ``{"generation_id": id}`` per record."""
    deps = _worker_deps()
    results = []
    for record in event.get("Records") or []:
        gen_id = int(json.loads(record["body"])["generation_id"])
        results.append(process_generation(gen_id, context, deps))
    return {"results": results}


# -- watchdog ---------------------------------------------------------------------


@dataclass
class WatchdogDeps:
    """The local :class:`~prxref.gate.watchdog.Watchdog` plus the queue sender."""

    store: Any
    watchdog: Any
    send: Callable[[Generation], None]


def _watchdog_deps() -> WatchdogDeps:
    def build() -> WatchdogDeps:
        from prxref.gate.watchdog import Watchdog

        settings, _cfg = _config()
        store = _store()
        _auth, client, _session = _github(settings)
        watchdog = Watchdog(
            store,
            client,
            sorted(settings.repos),
            watchdog_s=float(settings.watchdog_seconds),
            reconcile_s=float(settings.reconcile_minutes * 60),
        )
        return WatchdogDeps(store=store, watchdog=watchdog, send=_sender())

    return _cached("watchdog", build)


def watchdog_tick(deps: WatchdogDeps, now: float) -> dict:
    """Expiry repair and reconcile (when due), then one message per queued generation.

    Sending every queued generation, not only the ones this tick created,
    also recovers a row whose receiver enqueued it but failed to send. A
    resend of a still-waiting generation is deduplicated by SQS for 5
    minutes and is a no-op in the worker once the generation has run.
    """
    result = deps.watchdog.tick(now)
    sent: list[int] = []
    if not deps.store.is_paused():
        for gen in deps.store.queued():
            deps.send(gen)
            sent.append(gen.id)
    return {
        "requeued": result["requeued"],
        "reconciled": [g.id for g in result["reconciled"]] if result["reconciled"] is not None else None,
        "sent": sent,
    }


def watchdog_handler(event: dict, context: Any) -> dict:
    """EventBridge ``rate(1 minute)``; enqueues only, the worker does every repair."""
    return watchdog_tick(_watchdog_deps(), time.monotonic())
