"""Structured run tracing: one JSONL event stream per review.

The logs answer "what happened"; a trace answers "where is it right now, and
how long has it been there". Those are different questions, and tonight's
motivating failure only needed the second one: a review sat for 496s with an
open socket and emitted nothing, because chunks log on completion and a
request that never completes never logs.

Every event is one JSON object on one line, appended as it happens, so a
partial file describing a run still in flight -- or one that was killed -- is
as readable as a complete one. That is the property a summary written at the
end cannot have.

Tracing is off unless ``PRXREF_TRACE_FILE`` names a path. When off,
:func:`get_tracer` returns a tracer whose methods do nothing, so callers never
branch on whether tracing is enabled.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

SCHEMA_VERSION = 1


class Tracer:
    """Appends JSONL events for one review run.

    Thread-safe: the orchestrator fans chunk reviews out across a thread pool,
    so events arrive concurrently and each line must land whole. Writes are
    flushed per event rather than buffered -- a trace that only becomes
    readable once the process exits cleanly is useless for the hang it is meant
    to explain.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._seq = 0

    @property
    def path(self) -> str:
        return self._path

    def event(self, node: str, phase: str, **meta: Any) -> None:
        """Record one event. Never raises: tracing must not fail a review."""
        try:
            with self._lock:
                self._seq += 1
                record = {
                    "v": SCHEMA_VERSION,
                    "seq": self._seq,
                    "t_ms": int((time.monotonic() - self._t0) * 1000),
                    "node": node,
                    "phase": phase,
                }
                if meta:
                    record["meta"] = _jsonable(meta)
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                    fh.flush()
        except Exception:  # noqa: BLE001 - see docstring: never fail a review
            pass

    @contextmanager
    def span(self, node: str, **meta: Any) -> Iterator[dict]:
        """Emit ``start`` now and ``ok``/``fail`` on exit, with elapsed_ms.

        The yielded dict is a scratchpad: anything the caller puts in it is
        merged into the closing event, which is how a span reports totals it
        could not know when it opened.
        """
        extra: dict[str, Any] = {}
        self.event(node, "start", **meta)
        t0 = time.monotonic()
        try:
            yield extra
        except BaseException as exc:
            self.event(
                node, "fail",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                error=exc.__class__.__name__,
                **extra,
            )
            raise
        else:
            self.event(
                node, "ok",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                **extra,
            )


class _NullTracer(Tracer):
    """The disabled tracer: same surface, no file, no writes."""

    def __init__(self) -> None:  # noqa: D107 - deliberately skips Tracer.__init__
        self._path = ""

    def event(self, node: str, phase: str, **meta: Any) -> None:
        return

    @contextmanager
    def span(self, node: str, **meta: Any) -> Iterator[dict]:
        yield {}


def _jsonable(value: Any) -> Any:
    """Coerce to something json.dumps accepts, without raising on a surprise."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def get_tracer(path: str | None = None) -> Tracer:
    """Return a tracer for ``path``, or for ``PRXREF_TRACE_FILE``.

    Returns the no-op tracer when neither is set, so callers can trace
    unconditionally.
    """
    target = path or os.environ.get("PRXREF_TRACE_FILE") or ""
    target = target.strip()
    if not target:
        return _NullTracer()
    return Tracer(target)
