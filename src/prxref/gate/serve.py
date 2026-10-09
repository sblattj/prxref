"""``prxref gate``: one process running the receiver, the worker and the watchdog (#102).

Every component shares one :class:`~prxref.gate.store.Store`. The process
reads its credentials from a mode-0600 ``--env-file`` and logs the names it
loaded, never their values. ``PRXREF_RULES_SOURCE`` defaults to ``base`` here
and only here; ``prxref review`` and ``prxref serve`` keep ``head``.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from prxref.config import load_config
from prxref.gate import GateDependencyError, require_jwt, rulesets
from prxref.gate import worker as worker_module
from prxref.gate.app_auth import AppAuth, GateAuthError
from prxref.gate.binding import make_guard
from prxref.gate.checks import GitHubClient, Publisher
from prxref.gate.receiver import Receiver, serve_http
from prxref.gate.settings import GateSettings, apply_env_file, settings_from_config
from prxref.gate.store import Generation, Store
from prxref.gate.watchdog import Watchdog
from prxref.gate.worker import InstallationForge, ReviewOutcome, Worker

try:  # TEMPORARY GUARD until seat A lands register_secret (#102); the controller removes it
    from prxref.gate.worker import register_secret
except ImportError:
    register_secret = lambda v: None  # noqa: E731
from prxref.llm import ConfigError

logger = logging.getLogger(__name__)

BEDROCK_KEY_ENV = "AWS_BEARER_TOKEN_BEDROCK"


@dataclass
class Gate:
    """The wired components of one gate process."""

    settings: GateSettings
    store: Store
    auth: AppAuth
    client: GitHubClient
    publisher: Publisher
    receiver: Receiver
    worker: Worker
    watchdog: Watchdog


def make_review(auth: Any, settings: GateSettings, rules_source: str) -> Callable[..., ReviewOutcome]:
    """The worker's review callable: prxref reading the PR through an installation token."""

    def review(gen: Generation, *, register: Callable[[Any], None] | None = None) -> ReviewOutcome:
        inner = InstallationForge(lambda: auth.token_for(gen.repo))
        return worker_module.review_with_prxref(
            gen, inner, rules_source=rules_source, runs_dir=settings.runs_dir, register=register
        )

    return review


class _RedactingFilter(logging.Filter):
    """Mask tokens in every record a handler emits, whichever ``prxref.*`` logger raised it.

    Returns a redacted copy (permitted since Python 3.12), so the record other
    handlers share is left intact.
    """

    def filter(self, record: logging.LogRecord) -> logging.LogRecord:  # type: ignore[override]
        clean = copy.copy(record)
        clean.msg = worker_module._redact(record.getMessage())
        clean.args = ()
        if record.exc_info:
            clean.exc_text = worker_module._redact(logging.Formatter().formatException(record.exc_info))
            clean.exc_info = None
        elif record.exc_text:
            clean.exc_text = worker_module._redact(record.exc_text)
        if record.stack_info:
            clean.stack_info = worker_module._redact(record.stack_info)
        return clean


def _install_redaction() -> None:
    """Attach the filter to each root handler (a logger filter skips child-logger records)."""
    for handler in logging.getLogger().handlers:
        handler.addFilter(_RedactingFilter())


def build(settings: GateSettings, cfg: Mapping[str, Any], *, session: Any = None) -> Gate:
    """Construct every component from validated settings; nothing listens or runs yet."""
    for secret in (
        cfg["gate_webhook_secret"],
        cfg["gate_webhook_secret_previous"],
        os.environ.get(BEDROCK_KEY_ENV, ""),
    ):
        if secret:
            register_secret(secret)
    session = session or requests.Session()
    pem = settings.private_key_file.read_text(encoding="utf-8")
    auth = AppAuth(settings.app_id, pem, settings.installation_ids, session=session)
    store = Store(settings.state_file)
    client = GitHubClient(auth, session)
    publisher = Publisher(
        client, store, app_id=settings.app_id, bot_login=auth.bot_login(), guard=make_guard(client, store)
    )
    receiver = Receiver(store, settings, started_at=time.time())
    worker = Worker(
        store,
        publisher,
        client,
        make_review(auth, settings, cfg["rules_source"]),
        deadline_s=float(settings.review_deadline),
        effort=cfg["llm_reasoning_effort"] or "default",
    )
    watchdog = Watchdog(
        store,
        client,
        sorted(settings.repos),
        watchdog_s=float(settings.watchdog_seconds),
        reconcile_s=float(settings.reconcile_minutes * 60),
    )
    return Gate(settings, store, auth, client, publisher, receiver, worker, watchdog)


def run_gate(gate: Gate, host: str, port: int, *, stop: threading.Event | None = None) -> int:
    """Serve until SIGTERM/SIGINT or ``stop``; returns 0 after a clean shutdown."""
    stop = stop or threading.Event()
    previous: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda *_: stop.set())
    try:
        return _serve_until_stopped(gate, host, port, stop)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        gate.store.close()


def _serve_until_stopped(gate: Gate, host: str, port: int, stop: threading.Event) -> int:
    server = serve_http(gate.receiver, host, port)
    threads = [
        threading.Thread(target=server.serve_forever, name="gate-http", daemon=True),
        threading.Thread(target=gate.worker.run, args=(stop,), name="gate-worker", daemon=True),
        threading.Thread(target=gate.watchdog.run, args=(stop,), name="gate-watchdog", daemon=True),
    ]
    for t in threads:
        t.start()
    logger.info("gate listening on %s:%d", host, server.server_address[1])
    stop.wait()
    server.shutdown()
    server.server_close()
    for t in threads:
        t.join(timeout=5)
    return 0


def serve_main(env_file: Path, host: str, port: int) -> int:
    """``prxref gate serve``: load the env file, validate, build and run."""
    _install_redaction()
    names = apply_env_file(env_file)
    os.environ.setdefault("PRXREF_RULES_SOURCE", "base")
    require_jwt()
    cfg = load_config()
    settings = settings_from_config(cfg)
    logger.info("gate env file %s set: %s", env_file, ", ".join(sorted(names)))
    return run_gate(build(settings, cfg), host, port)


def _fmt_time(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "never"


def status_text(store: Store) -> str:
    """The operator view: pause, queue, lease, last publish, last auth failure, last 10 generations."""
    lease = store.lease_info()
    lines = [
        f"paused: {'yes' if store.is_paused() else 'no'}",
        f"queue depth: {store.queue_depth()}",
        f"lease: {lease['holder'] + ' until ' + _fmt_time(lease['expires_at']) if lease and lease['live'] else 'none'}",
        f"last publish: {_fmt_time(store.last_publish_time())}",
        f"last auth failure: {store.last_auth_failure() or 'none'}",
        "recent generations:",
    ]
    for g in store.recent(10):
        lines.append(
            f"  #{g.id} {g.repo}#{g.pr_number} gen {g.gen} {g.head_sha[:12]} {g.state} {g.conclusion or '-'} {g.reason}"
        )
    return "\n".join(lines)


def _state_store(env_file: Path) -> Store:
    apply_env_file(env_file)
    state = str(load_config()["gate_state_file"]).strip()
    if not state:
        raise ConfigError("PRXREF_GATE_STATE_FILE is required")
    return Store(Path(state).expanduser())


def run_command(args: argparse.Namespace) -> int:
    """Dispatch ``prxref gate <action>``; 2 on a configuration error or the missing extra."""
    try:
        action = args.gate_command
        if action == "serve":
            return serve_main(Path(args.env_file), args.host, args.port)
        if action in ("pause", "resume"):
            store = _state_store(Path(args.env_file))
            try:
                store.set_paused(action == "pause")
            finally:
                store.close()
            print(f"gate {'paused' if action == 'pause' else 'resumed'}")
            return 0
        if action == "status":
            store = _state_store(Path(args.env_file))
            try:
                text = status_text(store)
            finally:
                store.close()
            print(text)
            return 0
        if action == "render-manifest":
            print(json.dumps(rulesets.render_manifest(args.webhook_url), indent=2, sort_keys=True))
            return 0
        if action == "render-ruleset":
            print(json.dumps(rulesets.render_ruleset(args.integration_id), indent=2, sort_keys=True))
            return 0
        if action == "apply-ruleset":
            try:
                if args.diff:
                    diff = rulesets.diff_ruleset(args.repo, args.integration_id)
                    print(diff or "ruleset matches")
                    return 1 if diff else 0
                print(f"ruleset {rulesets.RULESET_NAME} id {rulesets.apply_ruleset(args.repo, args.integration_id)}")
                return 0
            except (ValueError, TypeError, KeyError, FileNotFoundError) as e:
                # gh output that is not JSON, the wrong shape, or no gh binary at all
                print(f"prxref gate: {e}", file=sys.stderr)
                return 1
    except (ConfigError, GateDependencyError, GateAuthError) as e:
        print(f"prxref gate: {e}", file=sys.stderr)
        return 2
    except rulesets.RulesetError as e:
        print(f"prxref gate: {e}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled gate action {action!r}")
