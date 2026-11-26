"""The per-repository review history file, its trend line and the regression gate (#46).

``PRXREF_HISTORY_FILE`` names a file in the CI checkout. Each finished
``prxref review`` appends exactly one compact JSON line to it, so a
``git diff`` of the file shows one added line per run. prxref never runs
git: the CI job commits the file back (``docs/history.md``).

One line carries, in this order: ``timestamp`` (ISO 8601 UTC), ``pr`` (the
PR number), ``head_sha``, ``verdict``, ``findings`` (active findings by
severity, all four severities always present), ``findings_total``,
``dropped`` (dropped findings by the quality pass that dropped them, named
by :func:`prxref.metrics.gate_for` and sorted by name), ``dropped_total``
and ``prxref_version``.

:func:`read` skips a line that is not a JSON object with a whole,
non-negative ``findings_total`` and logs a warning naming its line number;
a bad line is never fatal. :func:`baseline` takes the last
:data:`WINDOW` valid lines as they are, one sample per run. With at least
:data:`MIN_PRIOR` of them the summary gains :func:`trend_note`, and the
opt-in gate (:func:`regression`) may fire when ``findings_total`` is above
``mean * (1 + tolerance)``.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from prxref.metrics import gate_for

logger = logging.getLogger(__name__)

#: The rolling window: the trend and the gate average the last this many runs.
WINDOW = 20
#: Fewer prior runs than this: no trend line, and the gate never fires.
MIN_PRIOR = 3
#: The severities every line counts, always all four, in this order.
SEVERITIES = ("error", "warning", "spec", "outofscope")
#: The keys of one history line, in the order they are written.
LINE_KEYS = (
    "timestamp", "pr", "head_sha", "verdict", "findings", "findings_total",
    "dropped", "dropped_total", "prxref_version",
)


@dataclass(frozen=True)
class History:
    """What :func:`read` found: the valid entries in file order, and the skipped line count."""

    entries: tuple[Mapping[str, Any], ...] = ()
    skipped: int = 0
    error: str | None = None


@dataclass(frozen=True)
class Baseline:
    """The rolling mean of the last ``window`` valid runs, read once before a review.

    ``prior_runs`` is how many runs the mean covers (at most ``window``);
    ``mean`` is ``None`` when it covers none.
    """

    prior_runs: int = 0
    mean: float | None = None
    window: int = WINDOW
    skipped: int = 0
    error: str | None = None

    @property
    def enough(self) -> bool:
        """True when the baseline covers at least :data:`MIN_PRIOR` runs."""
        return self.mean is not None and self.prior_runs >= MIN_PRIOR


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_lines(lines: Iterable[str], *, source: str = "history file") -> History:
    """Parse history lines; a corrupt or partial line is skipped with a warning.

    A blank line is ignored silently. Any other line that is not a JSON
    object whose ``findings_total`` is a whole number of at least 0 is
    skipped, and a WARNING names ``source`` and its 1-based line number.
    """
    entries: list[Mapping[str, Any]] = []
    skipped = 0
    for number, raw in enumerate(lines, start=1):
        text = raw.strip()
        if not text:
            continue
        try:
            entry = json.loads(text)
        except ValueError:
            entry = None
        if not isinstance(entry, dict) or not _count(entry.get("findings_total")):
            skipped += 1
            logger.warning("%s: skipping unreadable line %d", source, number)
            continue
        entries.append(entry)
    return History(tuple(entries), skipped)


def read(path: str) -> History:
    """Read the history file at ``path``; never raises.

    A missing file is an empty history. A file that cannot be read is an
    empty history whose ``error`` says why, logged at WARNING.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return parse_lines(fh, source=path)
    except FileNotFoundError:
        return History()
    except OSError as exc:
        reason = exc.strerror or str(exc)
        logger.warning("%s: cannot read the history file: %s", path, reason)
        return History(error=f"cannot read: {reason}")


def baseline(history: History, *, window: int = WINDOW) -> Baseline:
    """The mean ``findings_total`` of the last ``window`` valid entries of ``history``."""
    recent = history.entries[-window:] if window > 0 else ()
    if not recent:
        return Baseline(0, None, window, history.skipped, history.error)
    totals = [int(e["findings_total"]) for e in recent]
    return Baseline(len(totals), sum(totals) / len(totals), window, history.skipped, history.error)


def _fmt(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".") if value != int(value) else str(int(value))


def trend_note(base: Baseline | None, findings_total: int, dropped_total: int) -> str:
    """The summary's one-line trend blockquote, ``""`` without enough history.

    ``"> 📈 Trend: N findings on this PR vs a mean of M per PR over the last
    R runs (+P%); D dropped by quality gates."``, the percentage left out
    when the mean is 0. Advisory only.
    """
    if base is None or not base.enough:
        return ""
    mean = float(base.mean or 0.0)
    noun = "finding" if findings_total == 1 else "findings"
    change = ""
    if mean > 0:
        pct = round((findings_total - mean) / mean * 100)
        change = f" ({'+' if pct >= 0 else ''}{pct}%)"
    return (
        f"> 📈 Trend: {findings_total} {noun} on this PR vs a mean of {_fmt(mean)} "
        f"per PR over the last {base.prior_runs} runs{change}; "
        f"{dropped_total} dropped by quality gates."
    )


def severity_counts(findings: Sequence[Any]) -> dict[str, int]:
    """Active findings by severity: the four severities always, then any other seen, sorted."""
    counts = dict.fromkeys(SEVERITIES, 0)
    extra: dict[str, int] = {}
    for f in findings:
        sev = getattr(f, "severity", None)
        if sev in counts:
            counts[sev] += 1
        else:
            key = str(sev)
            extra[key] = extra.get(key, 0) + 1
    return {**counts, **dict(sorted(extra.items()))}


def gate_counts(dropped: Sequence[Any]) -> dict[str, int]:
    """Dropped findings by the quality pass that dropped them, sorted by gate name."""
    counts: dict[str, int] = {}
    for f in dropped:
        gate = gate_for(getattr(f, "drop_reason", None))
        counts[gate] = counts.get(gate, 0) + 1
    return dict(sorted(counts.items()))


def make_line(
    result: Mapping[str, Any], *, pr: object, head_sha: str, version: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The history entry of one finished review, keys in :data:`LINE_KEYS` order."""
    active = list(result.get("findings_active") or [])
    dropped = list(result.get("findings_dropped") or [])
    stamp = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    return {
        "timestamp": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pr": pr,
        "head_sha": head_sha,
        "verdict": result.get("verdict"),
        "findings": severity_counts(active),
        "findings_total": len(active),
        "dropped": gate_counts(dropped),
        "dropped_total": len(dropped),
        "prxref_version": version,
    }


def encode(entry: Mapping[str, Any]) -> str:
    """One entry as one compact, newline-terminated JSON line, keys in their given order."""
    return json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n"


def append(path: str, entry: Mapping[str, Any]) -> str | None:
    """Append ``entry`` as one line; return ``None``, or the error text on failure.

    When the existing file does not end in a newline (a partial last line),
    one is written first so the new line never joins it. Never raises.
    """
    line = encode(entry)
    try:
        with open(path, "ab+") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() > 0:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    line = "\n" + line
            fh.write(line.encode("utf-8"))
    except OSError as exc:
        reason = exc.strerror or str(exc)
        logger.warning("%s: cannot append to the history file: %s", path, reason)
        return f"cannot append: {reason}"
    return None


def regression(
    base: Baseline | None, findings_total: int, *, enabled: bool, tolerance: float,
) -> dict[str, Any]:
    """The regression gate's outcome: ``{enabled, fired, threshold, reason}``.

    ``threshold`` is ``mean * (1 + tolerance)``, ``None`` without enough
    history. The gate fires only when it is enabled, the baseline covers at
    least :data:`MIN_PRIOR` runs and ``findings_total`` is strictly above the
    threshold, so 0 findings against a mean of 0 never fires and 1 does.
    """
    if not enabled:
        return {"enabled": False, "fired": False, "threshold": None, "reason": "off"}
    if base is None or not base.enough:
        runs = 0 if base is None else base.prior_runs
        return {
            "enabled": True, "fired": False, "threshold": None,
            "reason": f"skipped: {runs} prior run(s), at least {MIN_PRIOR} needed",
        }
    threshold = float(base.mean or 0.0) * (1 + tolerance)
    fired = findings_total > threshold
    return {
        "enabled": True, "fired": fired, "threshold": round(threshold, 4),
        "reason": "regression" if fired else "within tolerance",
    }
