"""Rendering layer: findings and run stats to forge-neutral markdown.

Everything here is pure string assembly (the only I/O is the optional
summary-template read) and uses the markdown subset all three forges
render identically: bold/italic, tables, fenced sections, ``<details>``.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .triage import Finding

try:
    from .reviewer import load_prompt as _reviewer_load_prompt
except ImportError:  # reviewer seat not landed yet; inline default applies
    _reviewer_load_prompt = None


_SEVERITY_MARKERS: dict[str, str] = {
    "error": "🟥",
    "warning": "🟧",
    "outofscope": "🟦",
}
_SEVERITY_ORDER: dict[str, int] = {"error": 0, "warning": 1, "outofscope": 2}

_DEFAULT_SUMMARY_TEMPLATE = """\
## {verdict_banner}

**Findings:** 🟥 {error_count} error · 🟧 {warning_count} warning · 🟦 {outofscope_count} outofscope

{active_count} active of {total_count} raw

{findings_table}
{dropped_section}
---

*chunks {chunk_count} · {input_tokens} in / {output_tokens} out tokens · {elapsed_s}s · model {model}*

*{attribution}*
"""


def _norm_severity(severity: str) -> str:
    """Normalize a severity for lookup; unknown values read as ``outofscope``."""
    norm = (severity or "").strip().lower()
    if norm in _SEVERITY_MARKERS:
        return norm
    return "outofscope"


def _fmt_seconds(elapsed_ms: int) -> str:
    """Render milliseconds as compact seconds (``4`` / ``3.5`` / ``0.2``)."""
    text = f"{elapsed_ms / 1000:.1f}".rstrip("0").rstrip(".")
    return text or "0"


def _fmt_location(f: Finding) -> str:
    """Render ``file:line``, omitting ``:0`` for file-level findings."""
    return f"{f.file}:{f.line}" if f.line > 0 else f.file


def _escape_cell(text: str) -> str:
    """Escape pipe characters so a cell cannot break the table layout."""
    return text.replace("|", "\\|")


def _findings_table(findings: list[Finding]) -> str:
    """Render the ``| severity | file:line | title |`` table, error-first."""
    if not findings:
        return "No findings survived the quality passes."
    ordered = sorted(
        findings,
        key=lambda f: (
            _SEVERITY_ORDER.get(_norm_severity(f.severity), 3),
            -f.confidence,
            f.file,
            f.line,
            f.title,
        ),
    )
    rows = [
        "| Severity | Location | Title |",
        "| --- | --- | --- |",
    ]
    rows.extend(
        f"| {_SEVERITY_MARKERS[_norm_severity(f.severity)]} "
        f"| {_escape_cell(_fmt_location(f))} "
        f"| {_escape_cell(f.title)} |"
        for f in ordered
    )
    return "\n".join(rows)


def _dropped_section(findings_dropped: list[Finding]) -> str:
    """Render the collapsed disclosure retaining every dropped finding."""
    if not findings_dropped:
        return ""
    tally = Counter(f.drop_reason or "unspecified" for f in findings_dropped)
    tally_lines = "\n".join(
        f"- {count} × {reason}"
        for reason, count in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    return (
        "<details>\n"
        f"<summary>Dropped findings: {len(findings_dropped)} "
        "(retained for audit)</summary>\n\n"
        f"{tally_lines}\n\n"
        f"{_findings_table(findings_dropped)}\n"
        "</details>\n"
    )


def _verdict_banner(verdict: str) -> str:
    """Map a verdict string to its banner; unrecognized means request-changes."""
    norm = verdict.strip().lower().replace("-", "_").replace(" ", "_")
    if norm in {"approved", "approve"}:
        return "✅ Approved"
    return "🛑 Request-Changes"


def _load_summary_template() -> str:
    """Load ``prompts/summary.md`` via the shared loader, else inline default.

    Placeholder contract for the template owner: ``verdict_banner``,
    ``error_count``, ``warning_count``, ``outofscope_count``, ``active_count``,
    ``total_count``, ``findings_table``, ``dropped_section``,
    ``chunk_count``, ``input_tokens``, ``output_tokens``, ``elapsed_s``,
    ``model``, ``attribution``.
    """
    if _reviewer_load_prompt is not None:
        try:
            return _reviewer_load_prompt("summary.md")
        except (OSError, KeyError, ValueError):
            pass
    path = Path(__file__).resolve().parent / "prompts" / "summary.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return _DEFAULT_SUMMARY_TEMPLATE


def build_attribution(model: str, elapsed_ms: int, tokens: int) -> str:
    """Render the one-line attribution every posted comment carries."""
    return (
        f"Reviewed by prxref · model={model} · {tokens} tok"
        f" · {_fmt_seconds(elapsed_ms)}s"
    )


def format_inline_comment(f: Finding, attribution: str) -> str:
    """Render one finding as a forge-neutral inline-comment body."""
    marker = _SEVERITY_MARKERS[_norm_severity(f.severity)]
    return f"{marker} **{f.title}**\n\n{f.body}\n\n*{attribution}*"


def format_summary(
    verdict: str,
    findings_active: list[Finding],
    findings_dropped: list[Finding],
    *,
    chunk_count: int,
    elapsed_ms: int,
    input_tokens: int,
    output_tokens: int,
    model: str,
) -> str:
    """Render the PR summary comment from the summary template.

    Falls back to the inline default template when the shared loader or
    the on-disk template is missing or carries unknown placeholders.
    """
    values = {
        "verdict_banner": _verdict_banner(verdict),
        "error_count": sum(
            1 for f in findings_active if _norm_severity(f.severity) == "error"
        ),
        "warning_count": sum(
            1 for f in findings_active if _norm_severity(f.severity) == "warning"
        ),
        "outofscope_count": sum(
            1 for f in findings_active if _norm_severity(f.severity) == "outofscope"
        ),
        "active_count": len(findings_active),
        "total_count": len(findings_active) + len(findings_dropped),
        "findings_table": _findings_table(findings_active),
        "dropped_section": _dropped_section(findings_dropped),
        "chunk_count": chunk_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "elapsed_s": _fmt_seconds(elapsed_ms),
        "model": model,
        "attribution": build_attribution(
            model, elapsed_ms, input_tokens + output_tokens
        ),
    }
    try:
        return _load_summary_template().format(**values)
    except (KeyError, IndexError, ValueError):
        return _DEFAULT_SUMMARY_TEMPLATE.format(**values)
