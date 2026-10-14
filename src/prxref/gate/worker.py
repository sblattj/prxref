"""Gate worker: one generation at a time, under a deadline (#102).

:func:`run_generation` starts a generation (fenced by the lease), checks
its binding to the live PR, opens the check, runs the review in a
daemon thread bounded by ``deadline_s``, and publishes. A deadline hit
or a crash is a ``failure``. A generation whose head, base or base ref
moved goes ``stale`` and, when nothing live covers the PR's current head
and base, queues a ``repair`` generation for them, which repairs a
delivery that arrived out of order. A closed PR is ``cancelled``.

A deadline cannot stop the review thread; it runs on in the background,
and :meth:`CapturingForge.seal` makes its late posts no-ops, so it can
never publish. The thread is a daemon, so a runaway review never keeps the
process alive after the worker stops.

Every stored, logged or recorded text passes through :func:`_redact`, which
masks GitHub token shapes and every value given to :func:`register_secret`
(the installation tokens as they are minted; ``serve`` adds the webhook
secrets and the Bedrock key).
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import tempfile
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from prxref import cli
from prxref.forges.github import ForgeImpl
from prxref.gate.binding import check_binding, read_live_pr
from prxref.gate.checks import Captured, Lease, Publisher, Stale
from prxref.gate.rules_source import materialize_rules, rules_sha
from prxref.gate.store import Generation, LeaseLost, Store, holder_alive

logger = logging.getLogger(__name__)

TTL_PAD_S = 120
_DRIFT = frozenset({"head moved", "base moved", "retargeted"})
_SECRET = re.compile(r"github_pat_[A-Za-z0-9_]+|gh[a-z]_[A-Za-z0-9]+|(?i:(?:bearer|token)\s+\S+)")
_MIN_SECRET_LEN = 8
_ERROR_LINE = "The review could not complete:"
_MAX_REGISTERED = 256
_registered: dict[str, None] = {}
_registered_lock = threading.Lock()


def register_secret(value: str) -> None:
    """Have :func:`_redact` mask ``value`` wherever it appears; values under 8 characters are ignored."""
    if isinstance(value, str) and len(value) >= _MIN_SECRET_LEN:
        with _registered_lock:
            _registered.pop(value, None)
            _registered[value] = None
            while len(_registered) > _MAX_REGISTERED:
                del _registered[next(iter(_registered))]


def _redact(text: str) -> str:
    """Mask registered secrets, GitHub tokens and ``Bearer``/``token`` credentials before text is stored or logged."""
    with _registered_lock:
        values = sorted(_registered, key=len, reverse=True)
    for value in values:
        text = text.replace(value, "[redacted]")
    return _SECRET.sub("[redacted]", text)


def _log_failure(message: str, exc: BaseException) -> None:
    """Log ``message`` with the exception's traceback, every line redacted."""
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error("%s\n%s", _redact(message), _redact(detail))


class InstallationForge(ForgeImpl):
    """The GitHub adapter authenticated by an installation token, never ``PRXREF_GITHUB_TOKEN``."""

    def __init__(self, token_source: Callable[[], str], session: Any = None) -> None:
        super().__init__(session=session)
        self._token_source = token_source

    def _headers(self, host: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        token = self._token_source()
        register_secret(token)
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"}
        if extra:
            headers.update(extra)
        return headers


_WRITE_PREFIXES = ("post_", "prune_", "delete_", "create_", "dismiss_", "update_", "add_")


class CapturingForge:
    """Reads through ``inner`` at the generation's SHAs; captures both posts.

    Any other write-shaped method (a name starting with one of
    :data:`_WRITE_PREFIXES`) is refused rather than forwarded, so a forge
    method added later can never write to the PR from inside a review.
    """

    def __init__(self, inner: Any, gen: Generation) -> None:
        self._inner = inner
        self._gen = gen
        self._summary: str | None = None
        self._comments: list = []
        self._sealed = False
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        if name.startswith(_WRITE_PREFIXES):
            raise AttributeError(f"{type(self).__name__} does not forward the write method {name!r}")
        return getattr(self._inner, name)

    def get_pr(self, ref):
        return replace(self._inner.get_pr(ref), source_sha=self._gen.head_sha, target_sha=self._gen.base_sha)

    def get_diff(self, ref) -> str:
        return self._inner.get_compare_diff(ref, base_sha=self._gen.base_sha, head_sha=self._gen.head_sha)

    def get_summary(self, ref) -> None:
        return None

    def post_summary(self, ref, body: str) -> None:
        with self._lock:
            if not self._sealed:
                self._summary = body

    def post_inline_comments(self, ref, comments) -> int:
        with self._lock:
            if self._sealed:
                return 0
            self._comments.extend(comments)
            return len(comments)

    def prune_inline_comments(self, ref, paths=None) -> int:
        return 0

    def seal(self) -> None:
        with self._lock:
            self._sealed = True

    def captured(self) -> Captured:
        with self._lock:
            return Captured(summary=self._summary, comments=tuple(self._comments))


@dataclass(frozen=True)
class ReviewOutcome:
    """A finished review: the result dict, what it asked to post, and its run directory."""

    result: object
    captured: Captured
    run_dir: Path


def review_with_prxref(
    gen: Generation,
    inner: Any,
    *,
    rules_source: str,
    runs_dir: Path,
    register: Callable[[CapturingForge], None] | None = None,
) -> ReviewOutcome:
    """Run prxref on ``gen`` with the repo config pinned to the rules SHA; persist the record.

    ``register`` receives the capture as soon as it exists, so a caller that
    gives up on the review (a deadline) can seal it.
    """
    url = f"https://github.com/{gen.repo}/pull/{gen.pr_number}"
    ref = ForgeImpl.parse_pr_url(url)
    run_dir = Path(runs_dir) / str(gen.id)
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    capture = CapturingForge(inner, gen)
    if register is not None:
        register(capture)
    sha = rules_sha(gen.head_sha, gen.base_sha, rules_source)
    try:
        with tempfile.TemporaryDirectory(prefix="prxref-gate-rules-") as tmp:
            pinned = materialize_rules(lambda p: inner.get_file_content(ref, p, sha=sha), Path(tmp))
            result = cli._run_review(
                url,
                forge=capture,
                config_file=pinned.config_file,
                context_file="",
                evidence_files=[""],
                prompts_dir="",
                full_review=True,
            )
    finally:
        capture.seal()
    record = cli._build_json_result(result) if isinstance(result, dict) else {"verdict": None}
    (run_dir / "result.json").write_text(_redact(json.dumps(record, indent=2, default=str)), encoding="utf-8")
    run_record = result if isinstance(result, dict) else {"error": type(result).__name__}
    (run_dir / "run_record.json").write_text(_redact(json.dumps(run_record, indent=2, default=str)), encoding="utf-8")
    return ReviewOutcome(result, capture.captured(), run_dir)


def _not_bound(gen: Generation, reason: str, *, store: Store, publisher: Publisher, client: Any, lease: Lease) -> str:
    if reason == "closed":
        try:
            publisher.cancel(gen, lease, "PR closed")
        except LeaseLost:
            raise
        except Exception as exc:  # noqa: BLE001
            return _publish_failed(gen, exc, store=store, publisher=publisher, lease=lease)
        store.finish(gen.id, lease.token, "cancelled", "cancelled", reason)
        return "cancelled"
    store.finish(gen.id, lease.token, "stale", None, reason)
    if reason in _DRIFT:
        # The row is already final; a failed read only skips the repair, which reconcile covers.
        try:
            live = read_live_pr(client, gen.repo, gen.pr_number)
        except LeaseLost:
            raise
        except Exception as exc:  # noqa: BLE001
            _log_failure(f"generation {gen.id}: the live PR could not be re-read for a repair: {exc}", exc)
            return "stale"
        if live.state == "open" and not store.has_live_generation(
            gen.repo,
            gen.pr_number,
            live.head_sha,
            live.base_sha,
        ):
            store.enqueue(gen.repo, gen.pr_number, live.head_sha, live.base_sha, live.base_ref, "repair")
    return "stale"


def _fail_publish(gen: Generation, lease: Lease, why: str, *, publisher: Publisher) -> None:
    """Try to close the check as failed; a failure here is logged and swallowed (a lost lease is not)."""
    try:
        publisher.fail(gen, lease, why)
    except LeaseLost:
        raise
    except Exception as exc:  # noqa: BLE001
        _log_failure(f"generation {gen.id}: the failure could not be published: {exc}", exc)


def _publish_failed(gen: Generation, exc: Exception, *, store: Store, publisher: Publisher, lease: Lease) -> str:
    why = f"publish failed: {type(exc).__name__}"
    _log_failure(f"generation {gen.id}: {why}: {exc}", exc)
    try:
        publisher.fail(store.get(gen.id), lease, why)
    except LeaseLost:
        raise
    except Exception:  # noqa: BLE001
        logger.error("generation %s: the failure could not be published either", gen.id)
    store.finish(gen.id, lease.token, "failed", "failure", _redact(f"{why}: {exc}")[:500])
    return "failed"


def _start_review(review: Callable[..., ReviewOutcome], gen: Generation, register: Callable) -> Future:
    """Run ``review`` on a daemon thread; the returned future carries its outcome or exception."""
    future: Future = Future()

    def target() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(review(gen, register=register))
        except BaseException as exc:  # noqa: BLE001 - delivered to the waiter, never swallowed
            future.set_exception(exc)

    threading.Thread(target=target, name=f"gate-review-{gen.id}", daemon=True).start()
    return future


def _error_reason(outcome: ReviewOutcome) -> str | None:
    """For an ``Error`` verdict, the posted "could not complete" line (already redacted for posting).

    Stored in the row's ``error`` column so ``last_auth_failure`` (status and
    ``/health``) can see an expired model key. ``None`` for any other verdict.
    """
    result = outcome.result
    if not isinstance(result, dict) or result.get("verdict") != "Error":
        return None
    for line in (outcome.captured.summary or "").splitlines():
        if line.startswith(_ERROR_LINE):
            return _redact(line)[:500]
    return "review error: the review posted no reason"


def run_generation(
    gen: Generation,
    *,
    store: Store,
    publisher: Publisher,
    client: Any,
    lease: Lease,
    review: Callable[..., ReviewOutcome],
    deadline_s: float,
    effort: str,
) -> str:
    """Review and publish one queued generation; returns its final state."""
    gen = store.start(gen.id, lease.token)
    try:
        live = read_live_pr(client, gen.repo, gen.pr_number)
    except LeaseLost:
        raise
    except Exception as exc:  # noqa: BLE001 - an auth failure or a malformed reply must not strand the row
        return _publish_failed(gen, exc, store=store, publisher=publisher, lease=lease)
    reason = check_binding(gen, live, store)
    if reason is not None:
        return _not_bound(gen, reason, store=store, publisher=publisher, client=client, lease=lease)
    try:
        publisher.begin(gen, lease)
    except Stale as stale:
        return _not_bound(gen, stale.reason, store=store, publisher=publisher, client=client, lease=lease)
    except LeaseLost:
        raise
    except Exception as exc:  # noqa: BLE001
        return _publish_failed(gen, exc, store=store, publisher=publisher, lease=lease)
    gen = store.get(gen.id)
    captures: list[CapturingForge] = []
    try:
        outcome = _start_review(review, gen, captures.append).result(timeout=deadline_s)
    except FuturesTimeout:
        why = f"deadline: review exceeded {deadline_s:g}s"
        for capture in captures:
            capture.seal()
        _fail_publish(gen, lease, why, publisher=publisher)
        store.finish(gen.id, lease.token, "failed", "failure", why)
        return "failed"
    except Exception as exc:  # noqa: BLE001
        why = f"review crashed: {type(exc).__name__}"
        _log_failure(f"generation {gen.id}: {why}: {exc}", exc)
        for capture in captures:
            capture.seal()
        _fail_publish(gen, lease, why, publisher=publisher)
        store.finish(gen.id, lease.token, "failed", "failure", _redact(f"{why}: {exc}")[:500])
        return "failed"
    try:
        conclusion = publisher.complete(gen, lease, outcome.result, outcome.captured, effort=effort)
    except Stale as stale:
        return _not_bound(gen, stale.reason, store=store, publisher=publisher, client=client, lease=lease)
    except LeaseLost:
        raise
    except Exception as exc:  # noqa: BLE001
        return _publish_failed(gen, exc, store=store, publisher=publisher, lease=lease)
    store.finish(gen.id, lease.token, "completed", conclusion, _error_reason(outcome))
    return "completed"


class Worker:
    """The single review loop: lease, adopt orphans, honor pause, dequeue."""

    def __init__(
        self,
        store: Store,
        publisher: Publisher,
        client: Any,
        review: Callable[..., ReviewOutcome],
        *,
        deadline_s: float,
        effort: str,
        holder: str | None = None,
        poll_s: float = 2.0,
    ) -> None:
        self.store = store
        self.publisher = publisher
        self.client = client
        self.review = review
        self.deadline_s = deadline_s
        self.effort = effort
        self.holder = holder or f"{socket.gethostname()}:{os.getpid()}"
        self.poll_s = poll_s
        self.ttl_s = deadline_s + TTL_PAD_S

    def acquire(self) -> Lease | None:
        """Take the lease (breaking a dead local holder's) and re-queue its orphans."""
        token = self.store.acquire_lease(self.holder, self.ttl_s, holder_alive=holder_alive)
        if token is None:
            return None
        adopted = self.store.adopt_orphans(token)
        if adopted:
            logger.warning("re-queued in-progress generations of a previous worker: %s", adopted)
        return Lease(self.holder, token, self.ttl_s)

    def run_once(self, lease: Lease) -> str | None:
        """Run the next generation; ``None`` when paused or idle."""
        if not self.store.renew_lease(lease.holder, lease.token, lease.ttl_s):
            raise LeaseLost(f"fencing token {lease.token} is not the live worker lease")
        if self.store.is_paused():
            return None
        gen = self.store.next_queued()
        if gen is None:
            return None
        return run_generation(
            gen,
            store=self.store,
            publisher=self.publisher,
            client=self.client,
            lease=lease,
            review=self.review,
            deadline_s=self.deadline_s,
            effort=self.effort,
        )

    def run(self, stop: threading.Event) -> None:
        """Loop until ``stop`` is set; a lost lease is re-acquired, never published through.

        A clean stop releases the lease, so the next start need not wait out its TTL.
        """
        lease: Lease | None = None
        while not stop.is_set():
            if lease is None:
                lease = self.acquire()
                if lease is None:
                    stop.wait(self.poll_s)
                    continue
            try:
                state = self.run_once(lease)
            except LeaseLost:
                logger.error("worker lease lost; re-acquiring")
                lease = None
                continue
            except Exception as exc:  # noqa: BLE001
                _log_failure("worker iteration failed", exc)
                state = None
            if state is None:
                stop.wait(self.poll_s)
        if lease is not None:
            self.store.release_lease(lease.holder, lease.token)
