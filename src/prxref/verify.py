"""Verify blocking error findings against the head file before publishing (#126).

A chunk worker sees a few context lines around each hunk, never the file's
imports and never the body of a function the hunk calls in another file. It
can therefore report, as a blocking ``error``, a claim the head file plainly
contradicts. This pass checks every active, model-made error at or above the
confidence floor before the quality gate counts it, in two tiers:

- Tier 0 (``names``, the default): :func:`prxref.quality.apply_undefined_name_check`
  drops an undefined-name claim ("`os` may not be imported") when the head
  file binds every named identifier. No model call.
- Tier 1 (``llm``): every eligible error tier 0 left active is sent, one
  call each and at most ``max_calls`` calls, to the model with the finding,
  its head file (whole, or a window around the line) and the definitions it
  calls into, looked up exactly as the context follow-up looks them up
  (:func:`prxref.repo_followup.lookup_excerpts`). A ``refuted`` reply whose
  evidence quote occurs, checked by code, in a text that was actually served
  downgrades the finding to ``warning`` with a body suffix; it is never
  dropped, so a wrong refutation still posts.

The pass is advisory and never raises: a failed read, a failed or timed-out
call, an unparseable reply, ``confirmed``, ``unsure``, or a refutation whose
quote was not served leaves the finding exactly as it was. Every call that
came back is returned as a run-cost unit, so its tokens and cost join the
run's. A unit also names the finding it checked (``file``, ``line``) and,
when the backend reported one, the ``requested_model`` chain entry that
answered, which ``PRXREF_LLM_REQUIRE_MODELS`` checks (issue #138).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Collection, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

from . import heuristics
from .costs import combine_reported
from .quality import (
    _resolve_confidence_floor,
    apply_undefined_name_check,
    finding_rank_key,
)
from .repo_context import _type_like
from .repo_followup import FollowupExcerpt, finding_names, lookup_excerpts
from .reviewer import fill_template, load_prompt
from .triage import Finding

logger = logging.getLogger("prxref")

#: ``PRXREF_VERIFY_ERRORS`` values: no pass, tier 0 only, tier 0 then tier 1.
VERIFY_MODES = ("off", "names", "llm")
DEFAULT_MODE = "names"
#: ``PRXREF_VERIFY_ERRORS_MAX``: tier-1 calls per run.
DEFAULT_MAX_CALLS = 5

#: Completion budget of one verifier call; the reply is one short JSON object.
MAX_TOKENS = 1024
#: Wall-clock deadline of one verifier call, in seconds.
TIMEOUT_S = 120.0
#: A head file longer than this many lines is served as a window around the finding.
MAX_FILE_LINES = 400
#: Lines served on each side of the finding's line when the file is windowed.
WINDOW_LINES = 200
#: The served head text is cut to this many characters.
MAX_FILE_CHARS = 40_000
#: Names tried for callee lookup per finding.
MAX_LOOKUP_NAMES = 12
#: Lines around the finding's line whose identifiers seed the callee lookup.
NEAR_LINES = 5
#: A refutation quote shorter than this (non-space characters) proves nothing.
MIN_EVIDENCE_CHARS = 6

#: The heading that splits ``verify.md`` into system (above) and user (from it on).
SPLIT_MARKER = "## Finding"

OUTCOMES = (
    "name-refuted",
    "not-checked",
    "confirmed",
    "refuted",
    "unproven",
    "unsure",
    "error",
    "capped",
)

_IDENT_RE = re.compile(r"(\.)?\b([A-Za-z_][A-Za-z0-9_]*)\b")
_EVIDENCE_RE = re.compile(r"^\s*`?([^\s`:]+):(\d+)`?\s*:?\s+(.+)$", re.DOTALL)
_GUTTER_RE = re.compile(r"^\s*\d+\s*\|\s?")
_FROM_ANY_RE = re.compile(r"^\s*from\s+[.\w]+\s+import\s+\(?([^)#]*)")
_KEYWORDS = frozenset(
    {
        "False",
        "None",
        "True",
        "and",
        "as",
        "assert",
        "async",
        "await",
        "break",
        "class",
        "continue",
        "def",
        "del",
        "elif",
        "else",
        "except",
        "finally",
        "for",
        "from",
        "global",
        "if",
        "import",
        "in",
        "is",
        "lambda",
        "nonlocal",
        "not",
        "or",
        "pass",
        "raise",
        "return",
        "try",
        "while",
        "with",
        "yield",
        "self",
        "cls",
        "this",
        "const",
        "let",
        "var",
        "function",
        "new",
        "public",
        "private",
        "protected",
        "static",
        "void",
        "final",
        "null",
        "true",
        "false",
    }
)


@dataclass(frozen=True)
class Served:
    """One text the verifier was shown: ``path``, its first 1-based line, and the raw text."""

    path: str
    first_line: int
    text: str


def empty_record(mode: str) -> dict[str, Any]:
    """The ``error_verification`` record before the pass runs (or on an exit before it)."""
    return {
        "mode": mode,
        "eligible": 0,
        "name_refuted": 0,
        "called": 0,
        "confirmed": 0,
        "refuted": 0,
        "unproven": 0,
        "unsure": 0,
        "errors": 0,
        "capped": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "detail": [],
    }


def is_eligible(f: Finding, floor: float) -> bool:
    """An active, model-made ``error`` at or above ``floor``: the findings this pass checks."""
    return (
        f.drop_reason is None
        and (f.severity or "").strip().lower() == "error"
        and not heuristics.is_deterministic(f)
        and float(f.confidence or 0.0) >= floor
    )


def _safe_read(read: Callable[[str], str | None], path: str) -> str | None:
    try:
        text = read(path)
    except Exception:  # noqa: BLE001 -- a failed read is "no head file"
        return None
    return text if isinstance(text, str) else None


def serve_head(text: str, line: int, path: str) -> Served:
    """The part of the head file the verifier sees: whole, or a window around ``line``."""
    lines = text.splitlines()
    if len(lines) <= MAX_FILE_LINES:
        start = 0
        kept = lines
    else:
        anchor = max(line, 1) - 1
        start = max(anchor - WINDOW_LINES, 0)
        kept = lines[start : anchor + WINDOW_LINES + 1]
    body = "\n".join(kept)
    if len(body) > MAX_FILE_CHARS:
        body = body[:MAX_FILE_CHARS].rsplit("\n", 1)[0]
    return Served(path, start + 1, body)


def _numbered(served: Served) -> str:
    return "\n".join(f"{served.first_line + i:>5}| {text}" for i, text in enumerate(served.text.splitlines()))


def _norm(name: str) -> str:
    return name.replace("_", "").lower()


def lookup_names(f: Finding, text: str) -> list[str]:
    """Names whose definitions may settle ``f``: the finding's own, then the code around its line.

    The finding's :func:`prxref.repo_followup.finding_names` come first. Then
    the identifiers on the head lines within :data:`NEAR_LINES` of the
    finding's line and on the lines of its enclosing ``def`` up to it:
    type-like names, then names a ``from ... import`` in the file binds whose
    spelling matches a nearby identifier with case and underscores ignored
    (``deps.watchdog`` finds ``Watchdog``), then attribute names, then the
    rest. Keywords, receivers and names under three characters are left out,
    and at most :data:`MAX_LOOKUP_NAMES` are kept.
    """
    out: list[str] = list(finding_names(f.title or "", f.body or ""))
    lines = text.splitlines()
    anchor = min(max(f.line, 1), len(lines)) - 1 if lines else -1
    if anchor >= 0:
        picked = list(range(max(anchor - NEAR_LINES, 0), min(anchor + NEAR_LINES + 1, len(lines))))
        indent = len(lines[anchor]) - len(lines[anchor].lstrip())
        for i in range(anchor, -1, -1):
            row = lines[i]
            if (
                row.strip()
                and len(row) - len(row.lstrip()) < indent
                and re.match(r"\s*(?:async\s+)?def\b|\s*(?:export\s+)?(?:async\s+)?function\b", row)
            ):
                picked = list(range(i, anchor)) + picked
                break
        typed: list[str] = []
        attrs: list[str] = []
        plain: list[str] = []
        for i in dict.fromkeys(picked):
            for m in _IDENT_RE.finditer(lines[i]):
                name = m.group(2)
                if name in _KEYWORDS or len(name) < 3:
                    continue
                (typed if _type_like(name) else attrs if m.group(1) else plain).append(name)
        nearby = {_norm(n) for n in (*typed, *attrs, *plain)}
        imported: list[str] = []
        for row in lines:
            m = _FROM_ANY_RE.match(row)
            if m is None:
                continue
            for item in m.group(1).split(","):
                parts = item.split()
                if parts and parts[-1] != "*" and _norm(parts[-1]) in nearby:
                    imported.append(parts[-1])
        out += typed + imported + attrs + plain
    seen: list[str] = []
    for name in out:
        if name and name not in seen and name not in _KEYWORDS and len(name) >= 3:
            seen.append(name)
    return seen[:MAX_LOOKUP_NAMES]


def render_prompt(f: Finding, head: Served, excerpts: Sequence[FollowupExcerpt]) -> tuple[str, str]:
    """The ``(system, user)`` pair for one verifier call, from the packaged ``prompts/verify.md``."""
    template = load_prompt("verify")
    system, marker, tail = template.partition(SPLIT_MARKER)
    if excerpts:
        blocks = "\n\n".join(f"{e.path}:{e.line}:\n" + _numbered(Served(e.path, e.line, e.text)) for e in excerpts)
    else:
        blocks = "(none could be looked up)"
    user = fill_template(
        marker + tail,
        {
            "file": f.file,
            "line": str(f.line),
            "title": (f.title or "").strip(),
            "body": (f.body or "").strip(),
            "head_file": f"{head.path}:\n{_numbered(head)}",
            "excerpts": blocks,
        },
    )
    return system.rstrip() + "\n", user


def parse_reply(text: str) -> tuple[str, str] | None:
    """``(verdict, evidence)`` from a verifier reply, or ``None`` when it is not usable."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in ("confirmed", "refuted", "unsure"):
        return None
    evidence = data.get("evidence")
    return verdict, evidence if isinstance(evidence, str) else ""


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def locate_evidence(evidence: str, served: Sequence[Served]) -> tuple[str, int] | None:
    """Where the quote in ``evidence`` occurs in a served text: ``(path, line)``, else ``None``.

    ``evidence`` is ``<path>:<line> <quote>`` (the prefix is optional). The
    quote loses surrounding backticks and quotes and any line-number gutter,
    and must hold at least :data:`MIN_EVIDENCE_CHARS` non-space characters.
    The cited path's served text is searched first, then the others: an
    exact substring first, then with runs of whitespace collapsed. The line
    returned is where the quote starts in that served file, computed here,
    never the model's number.
    """
    m = _EVIDENCE_RE.match(evidence or "")
    cited, quote = (m.group(1), m.group(3)) if m else ("", evidence or "")
    quote = "\n".join(_GUTTER_RE.sub("", row) for row in quote.strip().strip("`'\"").splitlines()).strip()
    if len(re.sub(r"\s", "", quote)) < MIN_EVIDENCE_CHARS:
        return None
    ordered = sorted(served, key=lambda s: s.path != cited)
    for s in ordered:
        index = s.text.find(quote)
        if index >= 0:
            return s.path, s.first_line + s.text.count("\n", 0, index)
    first = _collapse(quote.splitlines()[0])
    whole = _collapse(quote)
    for s in ordered:
        if whole and whole in _collapse(s.text):
            for i, row in enumerate(s.text.splitlines()):
                if first and first in _collapse(row):
                    return s.path, s.first_line + i
            return s.path, s.first_line
    return None


@dataclass
class _Outcome:
    outcome: str
    finding: Finding
    evidence: str = ""
    unit: dict[str, Any] | None = None
    error: str = ""


def verify_one(
    llm: Any,
    f: Finding,
    *,
    read: Callable[[str], str | None],
    listing_paths: Collection[str] | None = None,
    listing_complete: bool = False,
    exclude: Callable[[str], bool] | None = None,
) -> _Outcome:
    """Run tier 1 on one finding; never raises. The finding changes only on a served refutation."""
    try:
        text = _safe_read(read, f.file)
        if text is None:
            return _Outcome("error", f, error="no head file")
        head = serve_head(text, f.line, f.file)
        stub = SimpleNamespace(path=f.file, hunks=[], status="modified")
        excerpts = lookup_excerpts(
            [stub],
            [stub],
            lookup_names(f, text),
            read=read,
            listing_paths=listing_paths,
            listing_complete=listing_complete,
            exclude=exclude,
            shown=head.text,
        )
        system, user = render_prompt(f, head, excerpts)
    except Exception as e:  # noqa: BLE001 -- the verifier never fails a review
        return _Outcome("error", f, error=f"{type(e).__name__}: {e}")
    try:
        res = llm.invoke(system, user, max_tokens=MAX_TOKENS, json_mode=True, timeout_s=TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        return _Outcome("error", f, error=f"{type(e).__name__}: {e}")
    unit: dict[str, Any] | None = None
    try:
        cost, source = combine_reported([(getattr(res, "cost_usd", None), getattr(res, "cost_source", "") or "")])
        unit = {
            "model": getattr(res, "model", "") or "",
            "input_tokens": int(getattr(res, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(res, "output_tokens", 0) or 0),
            "elapsed_ms": int(getattr(res, "elapsed_ms", 0) or 0),
            "cost_usd": cost,
            "cost_source": source,
            "error": "",
            "file": f.file,
            "line": f.line,
        }
        requested = str(getattr(res, "requested_model", "") or "")
        if requested:
            unit["requested_model"] = requested
        parsed = parse_reply(getattr(res, "text", "") or "")
        if parsed is None:
            return _Outcome("error", f, unit=unit, error="unparseable reply")
        verdict, evidence = parsed
        if verdict != "refuted":
            return _Outcome(verdict, f, evidence=evidence, unit=unit)
        served = [head, *(Served(e.path, e.line, e.text) for e in excerpts)]
        where = locate_evidence(evidence, served)
        if where is None:
            return _Outcome("unproven", f, evidence=evidence, unit=unit)
        path, line = where
        body = (f.body or "").rstrip()
        suffix = f"[downgraded: verification refuted: {path}:{line}]"
        downgraded = replace(f, severity="warning", body=f"{body}\n\n{suffix}" if body else suffix)
        return _Outcome("refuted", downgraded, evidence=evidence, unit=unit)
    except Exception as e:  # noqa: BLE001
        return _Outcome("error", f, unit=unit, error=f"{type(e).__name__}: {e}")


def _row(f: Finding, outcome: str, evidence: str = "") -> dict[str, Any]:
    return {
        "id": f.id,
        "file": f.file,
        "line": f.line,
        "title": f.title,
        "outcome": outcome,
        "evidence": evidence[:300],
    }


def verify_errors(
    llm: Any,
    findings: Sequence[Finding],
    read: Callable[[str], str | None] | None,
    *,
    mode: str = DEFAULT_MODE,
    max_calls: int = DEFAULT_MAX_CALLS,
    confidence_floor: float | None = None,
    listing_paths: Collection[str] | None = None,
    listing_complete: bool = False,
    exclude: Callable[[str], bool] | None = None,
    max_workers: int = 4,
) -> tuple[list[Finding], dict[str, Any] | None, list[dict[str, Any]]]:
    """Run the error-verification pass: ``(findings, record, cost_units)``.

    ``mode`` is a member of :data:`VERIFY_MODES`. ``off`` returns the
    findings unchanged, record ``None`` and no units, and reads nothing. With
    ``read`` None every finding is unchanged too and the record counts the
    eligible errors only. Otherwise tier 0 runs, then at ``llm`` tier 1 on
    the eligible errors still active, most confident first
    (:func:`prxref.quality.finding_rank_key`), at most ``max_calls`` of them
    (the rest are ``capped``), on up to ``max_workers`` threads. The output
    has the input's length and order. The record is :func:`empty_record`
    filled in, with one ``detail`` row per eligible finding. Never raises.
    """
    if mode not in VERIFY_MODES:
        raise ValueError(f"verify_errors must be one of {VERIFY_MODES}, got {mode!r}")
    if mode == "off":
        return list(findings), None, []
    record = empty_record(mode)
    out = list(findings)
    units: list[dict[str, Any]] = []
    try:
        floor = _resolve_confidence_floor(confidence_floor)
        eligible = [i for i, f in enumerate(out) if is_eligible(f, floor)]
        record["eligible"] = len(eligible)
        if read is None or not eligible:
            record["detail"] = [_row(out[i], "not-checked") for i in eligible]
            return out, record, units
        checked = apply_undefined_name_check(out, read, confidence_floor=floor)
        outcomes: dict[int, tuple[str, str]] = {}
        for i in eligible:
            if checked[i].drop_reason is not None:
                outcomes[i] = ("name-refuted", checked[i].drop_reason or "")
                record["name_refuted"] += 1
        out = checked
        remaining = sorted(
            (i for i in eligible if i not in outcomes),
            key=lambda i: finding_rank_key(out[i]),
        )
        if mode == "llm":
            cap = max(int(max_calls), 0)
            to_call, over = remaining[:cap], remaining[cap:]
            for i in over:
                outcomes[i] = ("capped", "")
            record["capped"] = len(over)
            if to_call:
                workers = max(1, min(int(max_workers or 1), len(to_call)))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {
                        i: pool.submit(
                            verify_one,
                            llm,
                            out[i],
                            read=read,
                            listing_paths=listing_paths,
                            listing_complete=listing_complete,
                            exclude=exclude,
                        )
                        for i in to_call
                    }
                for i in to_call:
                    try:
                        result = futures[i].result()
                    except Exception as e:  # noqa: BLE001
                        result = _Outcome("error", out[i], error=f"{type(e).__name__}: {e}")
                    if result.unit is not None:
                        units.append(result.unit)
                        record["called"] += 1
                        record["input_tokens"] += result.unit["input_tokens"]
                        record["output_tokens"] += result.unit["output_tokens"]
                    key = "errors" if result.outcome == "error" else result.outcome
                    record[key] += 1
                    if result.outcome == "error":
                        logger.warning(
                            "error verification of %s:%d failed (keeping the finding): %s",
                            out[i].file,
                            out[i].line,
                            result.error,
                        )
                    out[i] = result.finding
                    outcomes[i] = (result.outcome, result.evidence or result.error)
        for i in remaining:
            outcomes.setdefault(i, ("not-checked", ""))
        record["detail"] = [_row(findings[i], *outcomes[i]) for i in eligible]
    except Exception as e:  # noqa: BLE001 -- the pass never fails a review
        logger.warning("error verification failed (keeping every finding): %s", e)
        return list(findings), {**empty_record(mode), "errors": 1}, units
    return out, record, units
