"""In-process Prometheus metrics and the loopback ``/metrics`` listener (#44).

Every review of ``prxref serve`` and ``prxref gate serve`` ends in
:func:`observe_result`, which records the finished run into a small
thread-safe registry. Recording always happens; the registry is only
exposed when ``PRXREF_METRICS_PORT`` is above 0, on a listener of its own
that binds ``127.0.0.1`` and never the webhook port.

No label carries a PR URL, a repository or a file path. ``verdict`` and
``severity`` come from closed sets, ``gate`` is the quality pass that wrote
a finding's ``drop_reason`` (anything unrecognised is ``other``), and
``model`` is the exact model name a unit reported (``cost_by_model``), so it
is bounded by the configured fallback chain.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)

LOOPBACK = "127.0.0.1"
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
DURATION_BUCKETS = (5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0)

_VERDICTS = frozenset({"Approved", "Request-Changes", "Error"})
_SEVERITIES = frozenset({"error", "warning", "spec", "outofscope"})
_MODEL_LABEL_MAX = 200

_GATES: tuple[tuple[str, str], ...] = (
    ("not confirmed by context follow-up", "merge_followup"),
    ("echoes the prompt's example", "apply_example_echo_check"),
    ("contradicted by execution evidence", "apply_evidence_drops"),
    ("restates execution evidence", "drop_restated_failures"),
    ("malformed location", "apply_location_validation"),
    ("anchor mismatch", "apply_manifest_claim_check"),
    ("section mismatch", "apply_manifest_claim_check"),
    ("duplicate of existing thread", "apply_thread_dedup"),
    ("settled in thread", "apply_settled_thread_suppression"),
    ("refuted in earlier run", "apply_stable_ids"),
    ("suppressed by learning", "apply_learning_suppression"),
    ("claims removal of a path", "apply_removal_claim_check"),
    ("claims removal of a symbol", "apply_head_removal_claim_check"),
    ("claims removal of a file", "apply_head_removal_claim_check"),
    ("claims a code change, but only comments changed", "apply_comment_only_change_check"),
    ("hedged", "apply_hedge_gate"),
    ("contradicted by head file", "apply_undefined_name_check"),
    ("same root cause as", "apply_root_cause_fold"),
    ("stability", "stability_merge"),
    ("grouped into", "apply_rule_grouping"),
    ("rule cap exceeded", "apply_rule_cap"),
    ("invalid severity", "apply_quality_gate"),
    ("confidence ", "apply_quality_gate"),
    ("error cap exceeded", "apply_quality_gate"),
    ("warning cap exceeded", "apply_quality_gate"),
    ("outofscope cap exceeded", "apply_quality_gate"),
    ("duplicate of chunk finding", "apply_sweep_dedup"),
    ("duplicate of sweep finding", "apply_sweep_dedup"),
)


def gate_for(drop_reason: object) -> str:
    """The quality pass behind a ``drop_reason``, or ``other`` when none matches."""
    if isinstance(drop_reason, str):
        for prefix, gate in _GATES:
            if drop_reason.startswith(prefix):
                return gate
    return "other"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _num(value: float) -> str:
    if isinstance(value, int) or float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


def _labels(names: tuple[str, ...], values: tuple[str, ...], extra: str = "") -> str:
    parts = [f'{n}="{_escape(v)}"' for n, v in zip(names, values, strict=True)]
    if extra:
        parts.append(extra)
    return "{" + ",".join(parts) + "}" if parts else ""


class _Counter:
    kind = "counter"

    def __init__(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> None:
        self.name, self.help, self.labels = name, help_text, labels
        self.values: dict[tuple[str, ...], float] = {}
        if not labels:
            self.values[()] = 0

    def inc(self, labels: tuple[str, ...] = (), amount: float = 1) -> None:
        self.values[labels] = self.values.get(labels, 0) + amount

    def lines(self) -> list[str]:
        return [
            f"{self.name}{_labels(self.labels, key)} {_num(val)}"
            for key, val in sorted(self.values.items())
        ]


class _Histogram:
    kind = "histogram"

    def __init__(self, name: str, help_text: str, buckets: Iterable[float]) -> None:
        self.name, self.help, self.labels = name, help_text, ()
        self.buckets = tuple(buckets)
        self.counts = [0] * len(self.buckets)
        self.total = 0
        self.sum = 0.0

    def observe(self, value: float) -> None:
        self.total += 1
        self.sum += value
        for i, bound in enumerate(self.buckets):
            if value <= bound:
                self.counts[i] += 1

    def lines(self) -> list[str]:
        out = [
            f'{self.name}_bucket{{le="{_num(b)}"}} {c}'
            for b, c in zip(self.buckets, self.counts, strict=True)
        ]
        out.append(f'{self.name}_bucket{{le="+Inf"}} {self.total}')
        out.append(f"{self.name}_sum {_num(self.sum)}")
        out.append(f"{self.name}_count {self.total}")
        return out


class Registry:
    """The metric set of one process; every method takes the registry lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reviews = _Counter(
            "prxref_reviews_total", "Finished reviews by verdict.", ("verdict",))
        self.posted = _Counter(
            "prxref_findings_posted_total",
            "Active findings of finished reviews by severity.", ("severity",))
        self.dropped = _Counter(
            "prxref_findings_dropped_total",
            "Findings dropped by a quality gate, by the gate that dropped them.", ("gate",))
        self.cost = _Counter(
            "prxref_llm_cost_usd_total",
            "Dollar cost of finished reviews with a known cost, by the model that answered.", ("model",))
        self.cost_unknown = _Counter(
            "prxref_llm_cost_unknown_reviews_total",
            "Finished reviews whose cost no source could price.")
        self.tokens = _Counter(
            "prxref_llm_tokens_total", "LLM tokens of finished reviews.", ("direction",))
        self.duration = _Histogram(
            "prxref_review_duration_seconds",
            "Wall time of one review in seconds.", DURATION_BUCKETS)
        self.errors = _Counter(
            "prxref_review_errors_total", "Reviews that ended with the Error verdict.")
        self._all = (
            self.reviews, self.posted, self.dropped, self.cost, self.cost_unknown,
            self.tokens, self.duration, self.errors,
        )

    def names(self) -> list[str]:
        """Every registered metric name."""
        return [m.name for m in self._all]

    def render(self) -> str:
        """The Prometheus text exposition (version 0.0.4) of every metric."""
        with self._lock:
            out: list[str] = []
            for metric in self._all:
                out.append(f"# HELP {metric.name} {metric.help}")
                out.append(f"# TYPE {metric.name} {metric.kind}")
                out.extend(metric.lines())
            return "\n".join(out) + "\n"

    def observe(self, result: object, elapsed_s: float | None = None) -> None:
        """Record one finished review result; a non-dict result counts as ``other``."""
        data: Mapping[str, Any] = result if isinstance(result, dict) else {}
        verdict = data.get("verdict")
        verdict = verdict if verdict in _VERDICTS else "other"
        posted = [getattr(f, "severity", None) for f in data.get("findings_active") or []]
        dropped = [gate_for(getattr(f, "drop_reason", None)) for f in data.get("findings_dropped") or []]
        ms = data.get("elapsed_ms")
        seconds = ms / 1000 if isinstance(ms, (int, float)) and not isinstance(ms, bool) else elapsed_s
        with self._lock:
            self.reviews.inc((verdict,))
            if verdict == "Error":
                self.errors.inc()
            for severity in posted:
                self.posted.inc((severity if severity in _SEVERITIES else "other",))
            for gate in dropped:
                self.dropped.inc((gate,))
            if isinstance(seconds, (int, float)) and seconds >= 0:
                self.duration.observe(float(seconds))
            for direction, key in (("input", "input_tokens"), ("output", "output_tokens")):
                n = data.get(key)
                if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                    self.tokens.inc((direction,), n)
            by_model = data.get("cost_by_model")
            if isinstance(by_model, dict) and by_model:
                unknown = False
                for model, cost in sorted(by_model.items(), key=lambda kv: str(kv[0])):
                    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
                        self.cost.inc((str(model)[:_MODEL_LABEL_MAX],), float(cost))
                    else:
                        unknown = True
                if unknown:
                    self.cost_unknown.inc()
            elif "cost_usd" in data and data["cost_usd"] is None:
                self.cost_unknown.inc()


_REGISTRY = Registry()


def reset() -> None:
    """Replace the process registry with an empty one (tests)."""
    global _REGISTRY
    _REGISTRY = Registry()


def observe_result(result: object, elapsed_s: float | None = None) -> None:
    """Record one finished review; never raises, so a metric cannot fail a review.

    The wall time is the result's own ``elapsed_ms`` when it carries one, else
    ``elapsed_s``.
    """
    try:
        _REGISTRY.observe(result, elapsed_s)
    except Exception:  # noqa: BLE001
        logger.debug("metrics observe failed", exc_info=True)


def render() -> str:
    """The process registry in Prometheus text exposition format."""
    return _REGISTRY.render()


def metric_names() -> list[str]:
    """The names of every metric the process registry exposes."""
    return _REGISTRY.names()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/metrics":
            self.send_error(404)
            return
        body = render().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug("metrics: " + format, *args)


def start_metrics_listener(port: int) -> ThreadingHTTPServer | None:
    """Serve ``GET /metrics`` on ``127.0.0.1:port`` from a daemon thread.

    Returns ``None`` when ``port`` is 0 (off). The bind address is fixed to
    loopback: reach it locally or through your own proxy. The caller owns the
    server and ends it with ``shutdown()`` then ``server_close()``.
    """
    if not port:
        return None
    server = ThreadingHTTPServer((LOOPBACK, port), _Handler)
    threading.Thread(target=server.serve_forever, name="prxref-metrics", daemon=True).start()
    logger.info("prxref metrics listening on %s:%d", LOOPBACK, server.server_address[1])
    return server


def start_from_env() -> ThreadingHTTPServer | None:
    """Start the listener when ``PRXREF_METRICS_PORT`` is set; ``ConfigError`` on a bad value."""
    from prxref.config import load_config

    return start_metrics_listener(int(load_config()["metrics_port"]))
