"""Worker-review layer: one LLM call per diff chunk.

The reviewer renders ``prompts/worker.md`` with the chunk's unified diff,
makes a single :meth:`LLMClient.invoke` call (no retries — the model
fallback chain handles transient failures), and maps the JSON response
onto :class:`prxref.triage.Finding` records. Unparseable or malformed
responses degrade to ``([], [])`` with a logged warning; this layer
never raises.
"""
from __future__ import annotations

import logging
import time
from importlib import resources
from typing import Any

from .llm import LLMClient
from .parser import loads_lenient
from .triage import FileDiff, Finding

logger = logging.getLogger("prxref")

MAX_TOKENS = 4096  # fallback only; the configured budget arrives per call
DEFAULT_CONFIDENCE = 0.5

_CONTEXT_MARKER = "## Review Context"


def load_prompt(name: str) -> str:
    """Load a prompt template from the packaged ``prxref/prompts`` directory."""
    fname = f"{name}.md" if not name.endswith(".md") else name
    return resources.files("prxref").joinpath("prompts").joinpath(fname).read_text(encoding="utf-8")


def _render_file(f: FileDiff) -> str:
    old = f.old_path or f.new_path or f.path
    new = f.new_path or f.old_path or f.path
    out = [f"diff --git a/{old} b/{new}"]
    if f.status == "renamed" and f.old_path and f.new_path:
        out.append(f"rename from {f.old_path}")
        out.append(f"rename to {f.new_path}")
    if f.is_binary:
        out.append(f"Binary files a/{old} and b/{new} differ")
        return "\n".join(out)
    if not f.hunks:
        return "\n".join(out)
    old_op = "/dev/null" if f.old_path is None else f"a/{f.old_path}"
    new_op = "/dev/null" if f.new_path is None else f"b/{f.new_path}"
    out.append(f"--- {old_op}")
    out.append(f"+++ {new_op}")
    for h in f.hunks:
        old_count = sum(1 for line in h.lines if line.kind != "+")
        new_count = sum(1 for line in h.lines if line.kind != "-")
        out.append(f"@@ -{h.old_start},{old_count} +{h.new_start},{new_count} @@")
        out.extend(line.kind + line.text for line in h.lines)
    return "\n".join(out)


def render_chunk(chunk: list[FileDiff]) -> str:
    """Render parsed files back to unified-diff text for the worker prompt."""
    return "\n\n".join(_render_file(f) for f in chunk)


def _render_prompt(
    chunk: list[FileDiff],
    pr_title: str,
    pr_description: str,
    repo_hint: str,
) -> tuple[str, str]:
    template = load_prompt("worker.md")
    head, marker, tail = template.partition(_CONTEXT_MARKER)
    if not marker:
        raise ValueError(f"worker.md is missing the {_CONTEXT_MARKER!r} split marker")
    user = (
        marker + tail
    ).replace(
        "{pr_title}", pr_title.strip() or "(untitled)"
    ).replace(
        "{pr_description}", pr_description.strip() or "(none)"
    ).replace(
        "{repo_hint}", repo_hint.strip() or "(unspecified)"
    ).replace(
        "{diff}", render_chunk(chunk) or "(empty chunk)"
    )
    return head.strip(), user.strip()


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _finding_from(raw: Any) -> Finding | None:
    if not isinstance(raw, dict):
        return None
    file = str(raw.get("file") or raw.get("path") or "").strip()
    if not file:
        return None
    try:
        confidence = float(raw.get("confidence", DEFAULT_CONFIDENCE))
    except (TypeError, ValueError):
        confidence = DEFAULT_CONFIDENCE
    return Finding(
        file=file,
        line=_as_int(raw.get("line")),
        severity=str(raw.get("severity") or ""),
        confidence=confidence,
        title=str(raw.get("title") or "").strip(),
        body=str(raw.get("body") or "").strip(),
    )


def review_chunk(
    llm: LLMClient,
    chunk: list[FileDiff],
    *,
    pr_title: str = "",
    pr_description: str = "",
    repo_hint: str = "",
    max_tokens: int | None = None,
) -> tuple[list[Finding], dict]:
    """Review one chunk with a single LLM call.

    Returns ``(findings, meta)`` where ``meta`` carries ``escalations`` plus
    cost telemetry (``input_tokens``, ``output_tokens``, ``model``,
    ``elapsed_ms`` — zeros when the call failed). Severity passes through
    unfiltered — the quality gate normalizes and drops downstream. A missing
    ``confidence`` maps to 0.5. Any LLM or parse failure logs a warning and
    yields ``([], meta)`` with ``meta["error"]`` set to the failure reason;
    this layer never raises and never retries. ``meta["error"]`` is the
    empty string on success.

    ``max_tokens`` is the completion budget for the call; ``None`` keeps the
    module default :data:`MAX_TOKENS`, so direct callers and older stubs are
    unaffected. The CLI threads ``PRXREF_LLM_MAX_TOKENS`` down to here.
    """
    system, user = _render_prompt(
        chunk=chunk,
        pr_title=pr_title,
        pr_description=pr_description,
        repo_hint=repo_hint,
    )
    t0 = time.perf_counter()
    meta = {
        "escalations": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "model": "",
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "error": "",
    }
    try:
        result = llm.invoke(
            system=system,
            user=user,
            max_tokens=MAX_TOKENS if max_tokens is None else max_tokens,
            json_mode=True,
        )
        parsed = loads_lenient(result.text)
    except Exception as e:  # noqa: BLE001
        logger.warning("worker review failed for chunk of %d files: %s", len(chunk), e)
        meta["error"] = f"{type(e).__name__}: {e}"
        meta["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return [], meta

    meta["input_tokens"] = result.input_tokens
    meta["output_tokens"] = result.output_tokens
    meta["model"] = result.model
    meta["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)

    if not isinstance(parsed, dict):
        logger.warning("worker review JSON is not an object: %s", type(parsed).__name__)
        meta["error"] = f"worker review JSON is not an object: {type(parsed).__name__}"
        return [], meta

    raw_findings = parsed.get("findings")
    if not isinstance(raw_findings, list):
        raw_findings = []
    findings = [f for f in (_finding_from(r) for r in raw_findings) if f is not None]

    raw_esc = parsed.get("escalations")
    if not isinstance(raw_esc, list):
        raw_esc = []
    meta["escalations"] = [e for e in raw_esc if isinstance(e, dict)]

    return findings, meta
