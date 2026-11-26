"""The read-only run dashboard and its loopback listener (#41).

With ``PRXREF_DASHBOARD_PORT`` above 0, ``prxref serve`` and ``prxref gate
serve`` open one more listener, bound to ``127.0.0.1`` only, apart from the
webhook and ``/metrics`` ports. It answers ``GET /`` with one self-contained
HTML page of the most recent runs and ``GET /runs.json`` with the same rows
as JSON. Every other path is 404 and every other method 405: there is no
write surface.

Two row sources, neither of which persists anything new:

* :class:`RingSource`, for ``prxref serve``: the last :data:`MAX_RUNS`
  finished reviews, summarized in memory at the same seam the metrics
  registry is fed (:func:`observe_result`). It resets when the daemon
  restarts.
* :class:`RunsDirSource`, for ``prxref gate serve``: the gate's existing
  ``<PRXREF_GATE_RUNS_DIR>/<generation id>/result.json`` and
  ``run_record.json`` files, read on each request, newest :data:`MAX_RUNS`
  by modification time, joined to the gate store's generation row for the
  repository, PR number, head SHA and finish time. A malformed, partial or
  unreadable file is skipped.

Every value rendered into the page is HTML-escaped, a link is emitted only
for an ``http`` or ``https`` URL, and the page carries no script.
"""
from __future__ import annotations

import html
import json
import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

LOOPBACK = "127.0.0.1"
MAX_RUNS = 50
SEVERITIES = ("error", "warning", "spec", "outofscope")
ROW_KEYS = (
    "finished_at", "repo", "pr_number", "pr_url", "head_sha", "verdict",
    "findings", "cost_usd", "cost_estimated", "input_tokens", "output_tokens",
    "models", "elapsed_ms", "source",
)
_TEXT_MAX = 300
_MODELS_MAX = 10
_CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"


def _text(value: object) -> str | None:
    """A bounded string, or ``None`` for anything that is not a non-empty string."""
    return value[:_TEXT_MAX] if isinstance(value, str) and value else None


def _count(value: object) -> int | None:
    """A non-negative integer, or ``None`` (``bool`` excluded)."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _number(value: object) -> float | None:
    """A finite non-negative number as ``float``, or ``None`` (``bool`` excluded)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number) and number >= 0:
            return number
    return None


def _millis(value: object) -> int | None:
    """A non-negative whole number of milliseconds, or ``None``."""
    number = _number(value)
    return int(number) if number is not None else None


def _severity_of(finding: object) -> object:
    return finding.get("severity") if isinstance(finding, Mapping) else getattr(finding, "severity", None)


def _finding_counts(record: Mapping[str, Any]) -> dict[str, int]:
    """Active findings by severity, from a result dict or a ``result.json`` record."""
    counts = dict.fromkeys(SEVERITIES, 0)
    counts["other"] = 0
    if "findings_active" in record:
        active: Iterable[object] = record.get("findings_active") or []
    else:
        rows = record.get("findings")
        active = [
            f for f in (rows if isinstance(rows, list) else [])
            if isinstance(f, Mapping) and f.get("drop_reason") is None
        ]
    if not isinstance(active, (list, tuple)):
        active = []
    for finding in active:
        severity = _severity_of(finding)
        counts[severity if severity in SEVERITIES else "other"] += 1
    return counts


def _models(record: Mapping[str, Any]) -> list[str]:
    """The models that answered (``cost_by_model`` keys), else the configured chain."""
    by_model = record.get("cost_by_model")
    names: list[object] = list(by_model) if isinstance(by_model, Mapping) and by_model else []
    if not names:
        sampling = record.get("sampling")
        chain = sampling.get("models") if isinstance(sampling, Mapping) else None
        names = list(chain) if isinstance(chain, list) else []
    return [m for m in (_text(n) for n in names) if m][:_MODELS_MAX]


def summarize(
    record: object,
    *,
    pr_url: str | None = None,
    repo: str | None = None,
    pr_number: int | None = None,
    head_sha: str | None = None,
    finished_at: float | None = None,
    source: str = "memory",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One dashboard row from a review result or a persisted record; never raises on odd input.

    ``record`` may be the result dict of a review (findings as ``Finding``
    objects in ``findings_active``) or the ``result.json`` record the gate
    writes (findings as dict rows, dropped ones with a ``drop_reason``).
    ``extra`` is a second record consulted for ``cost_by_model`` and
    ``sampling`` when ``record`` lacks them (the gate's ``run_record.json``).
    A field of the wrong type becomes ``None``.
    """
    data: Mapping[str, Any] = record if isinstance(record, Mapping) else {}
    more: Mapping[str, Any] = extra if isinstance(extra, Mapping) else {}
    models = _models(data) or _models(more)
    return {
        "finished_at": _number(finished_at),
        "repo": _text(repo),
        "pr_number": _count(pr_number),
        "pr_url": _text(pr_url),
        "head_sha": _text(head_sha),
        "verdict": _text(data.get("verdict")),
        "findings": _finding_counts(data),
        "cost_usd": _number(data.get("cost_usd")),
        "cost_estimated": data.get("cost_estimated") is True,
        "input_tokens": _count(data.get("input_tokens")),
        "output_tokens": _count(data.get("output_tokens")),
        "models": models,
        "elapsed_ms": _millis(data.get("elapsed_ms")),
        "source": source,
    }


class Source(Protocol):
    """Anything that lists dashboard rows, newest first."""

    def rows(self) -> list[dict[str, Any]]:  # pragma: no cover - protocol
        ...


class RingSource:
    """The last ``maxlen`` reviews of this process, in memory only."""

    def __init__(self, maxlen: int = MAX_RUNS) -> None:
        self._lock = threading.Lock()
        self._rows: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def add(self, row: Mapping[str, Any]) -> None:
        """Append one row; the oldest falls off past ``maxlen``."""
        with self._lock:
            self._rows.append(dict(row))

    def rows(self) -> list[dict[str, Any]]:
        """Every held row, newest first."""
        with self._lock:
            return [dict(r) for r in reversed(self._rows)]


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


class RunsDirSource:
    """The gate's run directories, read on each request and never written.

    ``lookup`` maps a generation id (the directory name) to an object with
    ``repo``, ``pr_number``, ``head_sha`` and ``finished_at`` attributes
    (:meth:`prxref.gate.store.Store.get`); a failing lookup leaves those
    fields empty and the time falls back to the file's modification time.
    """

    def __init__(self, runs_dir: Path, *, lookup: Callable[[int], Any] | None = None,
                 limit: int = MAX_RUNS) -> None:
        self.runs_dir = Path(runs_dir)
        self.lookup = lookup
        self.limit = limit

    def _candidates(self) -> list[tuple[float, int, Path]]:
        found: list[tuple[float, int, Path]] = []
        try:
            entries = list(self.runs_dir.iterdir())
        except OSError:
            return found
        for entry in entries:
            if not entry.name.isdigit():
                continue
            result = entry / "result.json"
            try:
                found.append((result.stat().st_mtime, int(entry.name), entry))
            except OSError:
                continue
        found.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return found

    def rows(self) -> list[dict[str, Any]]:
        """The newest ``limit`` readable run records, newest first."""
        out: list[dict[str, Any]] = []
        for mtime, gen_id, entry in self._candidates():
            if len(out) >= self.limit:
                break
            record = _read_json(entry / "result.json")
            if not isinstance(record, dict):
                continue
            extra = _read_json(entry / "run_record.json")
            gen = None
            if self.lookup is not None:
                try:
                    gen = self.lookup(gen_id)
                except Exception:  # noqa: BLE001
                    gen = None
            repo = getattr(gen, "repo", None)
            number = getattr(gen, "pr_number", None)
            url = f"https://github.com/{repo}/pull/{number}" if _text(repo) and _count(number) else None
            out.append(summarize(
                record,
                pr_url=url,
                repo=repo,
                pr_number=number,
                head_sha=getattr(gen, "head_sha", None),
                finished_at=getattr(gen, "finished_at", None) or mtime,
                source="runs_dir",
                extra=extra if isinstance(extra, dict) else None,
            ))
        return out


_RING = RingSource()


def reset() -> None:
    """Replace the process ring with an empty one (tests)."""
    global _RING
    _RING = RingSource()


def ring() -> RingSource:
    """The process ring ``prxref serve`` shows."""
    return _RING


def _repo_of(url: str) -> tuple[str | None, int | None]:
    try:
        from prxref.forges.base import detect_forge

        ref = detect_forge(url)
    except Exception:  # noqa: BLE001
        return None, None
    if ref is None:
        return None, None
    return f"{ref.owner}/{ref.repo}", ref.number


def observe_result(result: object, *, pr_url: str | None = None) -> None:
    """Summarize one finished review into the process ring; never raises."""
    try:
        if not isinstance(result, dict):
            return
        url = pr_url if isinstance(pr_url, str) else None
        repo, number = _repo_of(url) if url else (None, None)
        _RING.add(summarize(
            result, pr_url=url, repo=repo, pr_number=number,
            finished_at=time.time(), source="memory",
        ))
    except Exception:  # noqa: BLE001
        logger.debug("dashboard observe failed", exc_info=True)


def _fmt_time(ts: object) -> str:
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not math.isfinite(ts):
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _fmt_cost(row: Mapping[str, Any]) -> str:
    cost = row.get("cost_usd")
    if not isinstance(cost, float):
        return "unknown"
    return f"${cost:.4f}" + (" (est.)" if row.get("cost_estimated") else "")


def _fmt_duration(ms: object) -> str:
    return f"{ms / 1000:.1f} s" if isinstance(ms, int) and not isinstance(ms, bool) else "-"


def _fmt_tokens(row: Mapping[str, Any]) -> str:
    i, o = row.get("input_tokens"), row.get("output_tokens")
    if i is None and o is None:
        return "-"
    return f"{i if i is not None else '-'} in / {o if o is not None else '-'} out"


def _safe_href(url: object) -> str | None:
    if isinstance(url, str) and url.lower().startswith(("https://", "http://")):
        return url
    return None


def _pr_cell(row: Mapping[str, Any]) -> str:
    esc = html.escape
    repo, number = row.get("repo"), row.get("pr_number")
    if repo:
        label = f"{repo}#{number}" if number is not None else str(repo)
    else:
        label = row.get("pr_url") or "-"
    href = _safe_href(row.get("pr_url"))
    if href is None:
        return esc(str(label), quote=True)
    return f'<a href="{esc(href, quote=True)}" rel="noreferrer">{esc(str(label), quote=True)}</a>'


def _findings_cell(counts: object) -> str:
    if not isinstance(counts, Mapping):
        return "-"
    parts = [f"{k} {counts.get(k)}" for k in (*SEVERITIES, "other") if counts.get(k)]
    return html.escape(", ".join(parts) or "none")


def render_page(rows: list[dict[str, Any]], *, source_label: str) -> str:
    """The self-contained page for ``rows`` with every value escaped; no script, no remote asset."""
    esc = html.escape
    body_rows = []
    for row in rows:
        verdict = str(row.get("verdict") or "-")
        head = row.get("head_sha")
        body_rows.append(
            "<tr>"
            f"<td>{esc(_fmt_time(row.get('finished_at')))}</td>"
            f"<td>{_pr_cell(row)}</td>"
            f"<td><code>{esc(str(head)[:7] if head else '-', quote=True)}</code></td>"
            f'<td class="v">{esc(verdict, quote=True)}</td>'
            f"<td>{_findings_cell(row.get('findings'))}</td>"
            f"<td>{esc(_fmt_cost(row))}</td>"
            f"<td>{esc(_fmt_tokens(row))}</td>"
            f"<td>{esc(', '.join(row.get('models') or []) or '-', quote=True)}</td>"
            f"<td>{esc(_fmt_duration(row.get('elapsed_ms')))}</td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>Finished</th><th>PR</th><th>Head</th><th>Verdict</th>"
        "<th>Findings</th><th>Cost</th><th>Tokens</th><th>Models</th><th>Duration</th>"
        "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table>"
        if body_rows else '<p class="empty">No finished runs yet.</p>'
    )
    return _PAGE.replace("@@SOURCE@@", esc(source_label)).replace(
        "@@COUNT@@", str(len(rows))).replace("@@BODY@@", table)


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>prxref runs</title>
<meta name="referrer" content="no-referrer">
<style>
body{font:14px/1.4 system-ui,sans-serif;margin:1.5rem;color:#1b1f24;background:#fff}
h1{font-size:1.2rem}p.meta{color:#57606a}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:.3rem .5rem;border-bottom:1px solid #e3e6ea;vertical-align:top}
th{background:#f4f5f7;font-weight:600}code{font-size:12px}td.v{font-weight:600}
</style></head><body>
<h1>prxref runs</h1>
<p class="meta">@@COUNT@@ most recent runs, newest first. Source: @@SOURCE@@. Read-only.</p>
@@BODY@@
</body></html>
"""


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _handler(source: Source, source_label: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        """``GET /`` and ``GET /runs.json``; 404 for other paths, 405 for other methods."""

        def _send(self, code: int, ctype: str, payload: bytes, *, allow: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", _CSP)
            if allow:
                self.send_header("Allow", "GET, HEAD")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def _rows(self) -> list[dict[str, Any]]:
            try:
                return source.rows()
            except Exception:  # noqa: BLE001
                logger.warning("dashboard: the run source failed", exc_info=True)
                return []

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            path = self.path.split("?", 1)[0]
            if path == "/":
                page = render_page(self._rows(), source_label=source_label)
                self._send(200, "text/html; charset=utf-8", page.encode("utf-8"))
            elif path == "/runs.json":
                body = json.dumps(_json_safe(self._rows()), allow_nan=False)
                self._send(200, "application/json", body.encode("utf-8"))
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found\n")

        do_HEAD = do_GET  # noqa: N815

        def _refuse(self) -> None:
            self._send(405, "text/plain; charset=utf-8", b"method not allowed\n", allow=True)

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _refuse  # noqa: N815

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            logger.debug("dashboard: " + format, *args)

    return Handler


def start_dashboard_listener(port: int, source: Source, *, source_label: str = "memory") -> ThreadingHTTPServer | None:
    """Serve the dashboard on ``127.0.0.1:port`` from a daemon thread.

    Returns ``None`` when ``port`` is 0 (off). The bind address is fixed to
    loopback. The caller owns the server and ends it with ``shutdown()``
    then ``server_close()``.
    """
    if not port:
        return None
    server = ThreadingHTTPServer((LOOPBACK, port), _handler(source, source_label))
    threading.Thread(target=server.serve_forever, name="prxref-dashboard", daemon=True).start()
    logger.info("prxref dashboard listening on %s:%d", LOOPBACK, server.server_address[1])
    return server


def start_from_env() -> ThreadingHTTPServer | None:
    """``prxref serve``: the in-memory dashboard when ``PRXREF_DASHBOARD_PORT`` is set.

    Raises ``ConfigError`` on a bad value.
    """
    from prxref.config import load_config

    port = int(load_config()["dashboard_port"])
    return start_dashboard_listener(port, ring(), source_label="this process's memory, reset on restart")
