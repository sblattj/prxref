"""``prxref eval dashboard``: a read-only live view of one campaign directory (#81).

The campaign runner writes ``progress.json`` atomically on every state change
and one log per pass under ``logs/``. This module only reads them. It owns no
state beyond the last good ``progress.json`` it parsed, per directory, so a
read that lands mid-write serves the previous status instead of crashing.

:func:`status` builds the status dictionary, :func:`status_table` renders it as
plain text for ``--once``, and :func:`make_server` / :func:`serve` expose it
over ``http.server`` on ``GET /`` (one self-contained page that polls
``/status.json`` every two seconds) and ``GET /status.json``. The page shows
log tails, so a non-loopback ``--host`` logs a warning.
"""
from __future__ import annotations

import argparse
import errno
import html
import ipaddress
import json
import logging
import re
import socket
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from prxref.llm import ConfigError

logger = logging.getLogger(__name__)

# Private copy of eval_units.RATE_LIMIT_RE; import that one once it is merged.
RATE_LIMIT_RE = re.compile(
    r"\b429\b|rate[ _-]?limit|too many requests|quota|session (cap|limit)", re.IGNORECASE
)

DEFAULT_TAIL = 20
POLL_SECONDS = 2

_LAST_GOOD: dict[str, dict[str, Any]] = {}
_LAST_GOOD_LOCK = threading.Lock()


def _parse_ts(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z`` allowed) to an aware datetime, or ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _pct(done: int, total: int) -> float:
    return round(100.0 * min(done, total) / total, 1) if total > 0 else 0.0


def _eta(done: int, total: int, elapsed: float | None) -> float | None:
    """Remaining units over the observed rate; ``None`` until a unit is done."""
    if done <= 0 or elapsed is None or elapsed <= 0:
        return None
    remaining = max(total - done, 0)
    return round(remaining / (done / elapsed), 1)


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_progress(campaign_dir: Path) -> dict[str, Any]:
    """Return ``progress.json``, or the last good copy when it is mid-write or unreadable."""
    path = campaign_dir / "progress.json"
    key = str(campaign_dir.resolve())
    if not path.is_file() and key not in _LAST_GOOD:
        raise ConfigError(f"--campaign: no progress.json in {campaign_dir}")
    try:
        data = _read_json(path)
        if not isinstance(data, dict):
            raise ValueError("progress.json is not an object")
    except (OSError, ValueError):
        with _LAST_GOOD_LOCK:
            cached = _LAST_GOOD.get(key)
        if cached is None:
            raise ConfigError(f"--campaign: {path} is not valid JSON") from None
        return cached
    with _LAST_GOOD_LOCK:
        _LAST_GOOD[key] = data
    return data


def _load_campaign(campaign_dir: Path) -> dict[str, Any] | None:
    try:
        data = _read_json(campaign_dir / "campaign.json")
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_log(campaign_dir: Path, rel: object) -> list[str]:
    """Return the lines of a pass log, or ``[]`` when it is absent or outside the campaign."""
    if not isinstance(rel, str) or not rel:
        return []
    root = campaign_dir.resolve()
    path = (root / rel).resolve()
    if root != path and root not in path.parents:
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _pass_status(campaign_dir: Path, raw: dict[str, Any], now: datetime, tail: int) -> dict[str, Any]:
    out = dict(raw)
    total = _int(raw.get("units_total"))
    done = _int(raw.get("units_ok")) + _int(raw.get("units_failed"))
    started = _parse_ts(raw.get("started_at"))
    finished = _parse_ts(raw.get("finished_at"))
    elapsed = ((finished or now) - started).total_seconds() if started else None
    lines = _read_log(campaign_dir, raw.get("log"))
    out["pct"] = _pct(done, total)
    out["eta_s"] = _eta(done, total, elapsed)
    out["log_tail"] = lines[-tail:] if tail > 0 else []
    out["rate_limit_lines"] = sum(1 for line in lines if RATE_LIMIT_RE.search(line))
    return out


def status(campaign_dir: str | Path, tail: int = DEFAULT_TAIL, now: datetime | None = None) -> dict[str, Any]:
    """Build the campaign status from ``progress.json``, ``campaign.json`` and the pass logs.

    Returns ``{"campaign", "updated_at", "passes", "overall"}``. Each pass is its
    ``progress.json`` entry plus ``pct``, ``eta_s`` (remaining units over the
    pass's own observed rate, ``None`` until a unit is done), ``log_tail`` (the
    last ``tail`` lines) and ``rate_limit_lines`` (matches in the whole log).
    ``overall`` sums the passes: ``units_total``, ``units_done`` (ok plus
    failed), ``pct``, ``elapsed_s`` since the top-level ``started_at``,
    ``eta_s`` from the campaign-wide rate and ``rate_limited_passes``.

    A missing ``progress.json`` raises ``ConfigError`` naming ``--campaign``.
    One that is invalid JSON (a read that landed mid-write) yields the last
    good status for that directory.
    """
    root = Path(campaign_dir)
    progress = _load_progress(root)
    now = now or datetime.now(UTC)
    entries = [p for p in progress.get("passes") or [] if isinstance(p, dict)]
    passes = [_pass_status(root, p, now, tail) for p in entries]
    total = sum(_int(p.get("units_total")) for p in entries)
    done = sum(_int(p.get("units_ok")) + _int(p.get("units_failed")) for p in entries)
    started = _parse_ts(progress.get("started_at"))
    elapsed = max((now - started).total_seconds(), 0.0) if started else None
    limited = sum(
        1 for raw, p in zip(entries, passes, strict=True)
        if p["rate_limit_lines"] or _int(raw.get("units_rate_limited"))
    )
    return {
        "campaign": _load_campaign(root),
        "updated_at": progress.get("updated_at"),
        "passes": passes,
        "overall": {
            "units_total": total,
            "units_done": done,
            "pct": _pct(done, total),
            "elapsed_s": round(elapsed, 1) if elapsed is not None else None,
            "eta_s": _eta(done, total, elapsed),
            "rate_limited_passes": limited,
        },
    }


def _fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    whole = int(seconds)
    return f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


def _limited(p: dict[str, Any]) -> bool:
    return bool(p.get("rate_limit_lines")) or bool(_int(p.get("units_rate_limited")))


def status_table(st: dict[str, Any]) -> str:
    """Render a status as a plain-text table, one row per pass, then an overall line."""
    header = ("arm", "repeat", "state", "ok/total", "pct", "eta", "rate-limit")
    rows = [header]
    for p in st["passes"]:
        rows.append((
            str(p.get("arm", "")), str(p.get("repeat", "")), str(p.get("state", "")),
            f"{_int(p.get('units_ok'))}/{_int(p.get('units_total'))}", f"{p['pct']:.1f}%",
            _fmt_eta(p["eta_s"]), "RATE-LIMITED" if _limited(p) else "-",
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows]
    o = st["overall"]
    lines.append(
        f"overall: {o['units_done']}/{o['units_total']} units ({o['pct']:.1f}%), "
        f"eta {_fmt_eta(o['eta_s'])}, rate-limited passes {o['rate_limited_passes']}"
    )
    return "\n".join(lines)


def render_page(st: dict[str, Any] | None = None) -> str:
    """Return the self-contained dashboard page, pre-rendered from ``st`` with every value escaped."""
    esc = html.escape
    body = ""
    if st is not None:
        o = st["overall"]
        body += (
            f"<p id=\"overall\">overall {o['units_done']}/{o['units_total']} units "
            f"({o['pct']:.1f}%), eta {esc(_fmt_eta(o['eta_s']))}, "
            f"rate-limited passes {o['rate_limited_passes']}</p>"
        )
        for p in st["passes"]:
            tail = "\n".join(esc(str(line)) for line in p["log_tail"])
            body += (
                f"<section><h2>{esc(str(p.get('arm', '')))} r{esc(str(p.get('repeat', '')))} "
                f"<span class=\"state {esc(str(p.get('state', '')))}\">{esc(str(p.get('state', '')))}</span></h2>"
                f"<p>{_int(p.get('units_ok'))}/{_int(p.get('units_total'))} ok, {p['pct']:.1f}%, "
                f"eta {esc(_fmt_eta(p['eta_s']))}</p><pre>{tail}</pre></section>"
            )
    return _PAGE.replace("@@BODY@@", body).replace("@@POLL@@", str(POLL_SECONDS * 1000))


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>prxref campaign</title>
<style>
body{font:14px/1.4 system-ui,sans-serif;margin:1.5rem;color:#1b1f24;background:#fff}
h1{font-size:1.2rem}h2{font-size:1rem;margin:1rem 0 .2rem}
pre{background:#f4f5f7;padding:.5rem;overflow:auto;max-height:14rem;font-size:12px}
.bar{background:#e3e6ea;height:8px;border-radius:4px;overflow:hidden}
.bar i{display:block;height:100%;background:#2f7ed8}
.state{font-weight:600;padding:0 .4rem;border-radius:3px;background:#e3e6ea}
.running,.scoring{background:#cfe3fb}.scored{background:#cdeccf}.failed{background:#f7cfcf}
.rl{color:#b42318;font-weight:600}
</style></head><body>
<h1>prxref campaign</h1><div id="app">@@BODY@@</div>
<script>
function esc(s){return String(s).replace(/[&<>"']/g,function(c){
return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
function eta(s){if(s===null||s===undefined)return "-";s=Math.floor(s);
return Math.floor(s/3600)+":"+("0"+Math.floor(s%3600/60)).slice(-2)+":"+("0"+s%60).slice(-2);}
function draw(st){var o=st.overall,h='<p id="overall">overall '+o.units_done+"/"+o.units_total+
" units ("+o.pct.toFixed(1)+"%), eta "+esc(eta(o.eta_s))+", rate-limited passes "+o.rate_limited_passes+
'</p><div class="bar"><i style="width:'+o.pct+'%"></i></div>';
st.passes.forEach(function(p){var rl=p.rate_limit_lines||p.units_rate_limited;
h+="<section><h2>"+esc(p.arm)+" r"+esc(p.repeat)+' <span class="state '+esc(p.state)+'">'+esc(p.state)+
"</span>"+(rl?' <span class="rl">rate-limited</span>':"")+"</h2><p>"+(p.units_ok||0)+"/"+(p.units_total||0)+
" ok, "+p.pct.toFixed(1)+"%, eta "+esc(eta(p.eta_s))+'</p><div class="bar"><i style="width:'+p.pct+
'%"></i></div><pre>'+p.log_tail.map(esc).join("\\n")+"</pre></section>";});
document.getElementById("app").innerHTML=h;}
function poll(){fetch("/status.json").then(function(r){return r.json();}).then(draw).catch(function(){});}
poll();setInterval(poll,@@POLL@@);
</script></body></html>
"""


def _handler(campaign_dir: Path, tail: int) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        """Serve ``/`` and ``/status.json`` for one campaign directory; everything else is 404."""

        def _send(self, code: int, ctype: str, payload: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            path = self.path.split("?", 1)[0]
            if path not in ("/", "/status.json"):
                self._send(404, "text/plain; charset=utf-8", b"not found\n")
                return
            try:
                st = status(campaign_dir, tail=tail)
            except ConfigError as exc:
                self._send(503, "application/json", json.dumps({"error": str(exc)}).encode())
                return
            if path == "/":
                self._send(200, "text/html; charset=utf-8", render_page(st).encode("utf-8"))
            else:
                self._send(200, "application/json", json.dumps(st).encode("utf-8"))

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            logger.debug("dashboard: " + format, *args)

    return Handler


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_server(campaign_dir: str | Path, host: str = "127.0.0.1", port: int = 8765,
                tail: int = DEFAULT_TAIL) -> ThreadingHTTPServer:
    """Bind a dashboard server without starting it; ``--port 0`` picks a free port.

    A port that is taken (or a host that cannot be bound) raises ``ConfigError``
    naming ``--port`` (``--host``). A non-loopback host logs a warning, because
    the page exposes log tails.
    """
    root = Path(campaign_dir)
    status(root, tail=tail)
    if not _is_loopback(host):
        logger.warning("dashboard: --host %s is not loopback; the page exposes campaign log tails", host)

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    try:
        return Server((host, port), _handler(root, tail))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise ConfigError(f"--port: {port} is already in use") from exc
        raise ConfigError(f"--host/--port: cannot bind {host}:{port}: {exc}") from exc


def serve(campaign_dir: str | Path, host: str, port: int, tail: int) -> int:
    """Run the dashboard until interrupted; Ctrl-C returns 0."""
    server = make_server(campaign_dir, host, port, tail)
    shown = f"[{host}]" if ":" in host else host
    print(f"dashboard: http://{shown}:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def run(args: argparse.Namespace) -> int:
    """Entry point behind ``eval_dashboard``: ``--once`` prints the table, otherwise serve."""
    tail = args.tail
    if tail < 0:
        raise ConfigError("--tail: must be 0 or more")
    if not 0 <= args.port <= 65535:
        raise ConfigError("--port: must be between 0 and 65535")
    campaign = Path(args.campaign)
    if not campaign.is_dir():
        raise ConfigError(f"--campaign: {campaign} is not a directory")
    if args.once:
        print(status_table(status(campaign, tail=tail)))
        return 0
    return serve(campaign, args.host, args.port, tail)
