"""Worker-review layer: one LLM call per diff chunk, plus the systemic sweep.

The reviewer renders ``prompts/worker.md`` with the chunk's unified diff,
makes a single :meth:`LLMClient.invoke` call (no retries — the model
fallback chain handles transient failures), and maps the JSON response
onto :class:`prxref.triage.Finding` records. Unparseable or malformed
responses degrade to ``([], [])`` with a logged warning; this layer
never raises. :func:`review_systemic` is the same contract over the
whole-PR digest built by :mod:`prxref.systemic`, for the second-order
classes no single chunk seat can see.

When a response is unparseable *because* the model ran out of completion
budget (``finish_reason == "length"``), the reported error names the budget
and the variable that raises it rather than the bare ``JSONDecodeError`` the
operator cannot act on. A truncated-but-parseable response is logged at
warning level and still counted as reviewed.
"""
from __future__ import annotations

import logging
import time
from importlib import resources
from typing import Any

from .llm import LLMClient
from .parser import loads_lenient
from .triage import FileDiff, Finding, trim_hunk_context

logger = logging.getLogger("prxref")

MAX_TOKENS = 4096  # fallback only; the configured budget arrives per call
DEFAULT_CONFIDENCE = 0.5

_CONTEXT_MARKER = "## Review Context"

_MAX_TOKENS_ENV = "PRXREF_LLM_MAX_TOKENS"

# Every spelling that means "I stopped because I ran out of output budget".
# ``length`` is the OpenAI vocabulary that litellm normalises to; a plain
# OpenAI-compatible proxy in front of another provider may pass that provider's
# own word through untouched, and ``max_tokens`` is what those use. Matching is
# exact against this set after casefolding, never a substring test, so a
# neighbouring reason like ``length_finish`` cannot be mistaken for truncation.
_TRUNCATION_FINISH_REASONS = frozenset({"length", "max_tokens"})

# Written for whoever reads the failing chunk's error, not for a stack trace:
# it names the budget that was in force and the one variable that changes it.
_TRUNCATED_ERROR = (
    "response truncated at max_tokens={budget} (finish_reason={reason}); "
    "raise " + _MAX_TOKENS_ENV
)


def _budget_stop_reason(result: Any) -> str:
    """The provider's stop reason, when it says generation hit the token budget.

    Returns the reason AS THE PROVIDER SPELLED IT (whitespace stripped, casing
    left alone), or ``""`` when the response was not truncated. Matching is
    casefolded because the vocabulary is the provider's and gateways disagree
    on casing, but the reported string is not: quoting a normalised
    ``max_tokens`` back at an operator whose gateway logged ``MAX_TOKENS``
    sends them grepping for a string that is not in their log.

    Tolerant of a backend or test double whose result predates
    ``InvokeResult.finish_reason``: an absent or non-string attribute reads as
    "not reported", never as truncation. An unrecognised spelling falls back to
    the plain parse error, which is the safe direction — a missed hint, never a
    false claim.
    """
    reason = getattr(result, "finish_reason", "")
    if not isinstance(reason, str):
        return ""
    literal = reason.strip()
    return literal if literal.lower() in _TRUNCATION_FINISH_REASONS else ""


def load_prompt(name: str) -> str:
    """Load a prompt template from the packaged ``prxref/prompts`` directory."""
    fname = f"{name}.md" if not name.endswith(".md") else name
    return resources.files("prxref").joinpath("prompts").joinpath(fname).read_text(encoding="utf-8")


def _render_file(f: FileDiff, context_lines: int | None = None) -> str:
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
        shown = h if context_lines is None else trim_hunk_context(h, context_lines)
        old_count = sum(1 for line in shown.lines if line.kind != "+")
        new_count = sum(1 for line in shown.lines if line.kind != "-")
        out.append(f"@@ -{shown.old_start},{old_count} +{shown.new_start},{new_count} @@")
        out.extend(line.kind + line.text for line in shown.lines)
    return "\n".join(out)


def render_chunk(chunk: list[FileDiff], context_lines: int | None = None) -> str:
    """Render parsed files back to unified-diff text for the worker prompt.

    ``context_lines`` bounds the context lines kept around each change via
    :func:`prxref.triage.trim_hunk_context`; ``None`` renders verbatim.
    """
    return "\n\n".join(_render_file(f, context_lines) for f in chunk)


def _render_prompt(
    chunk: list[FileDiff],
    pr_title: str,
    pr_description: str,
    repo_hint: str,
    context_lines: int | None = None,
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
        "{diff}", render_chunk(chunk, context_lines) or "(empty chunk)"
    )
    return head.strip(), user.strip()


def _render_systemic_prompt(
    digest: str,
    pr_title: str,
    pr_description: str,
    repo_hint: str,
) -> tuple[str, str]:
    template = load_prompt("systemic.md")
    head, marker, tail = template.partition(_CONTEXT_MARKER)
    if not marker:
        raise ValueError(f"systemic.md is missing the {_CONTEXT_MARKER!r} split marker")
    user = (
        marker + tail
    ).replace(
        "{pr_title}", pr_title.strip() or "(untitled)"
    ).replace(
        "{pr_description}", pr_description.strip() or "(none)"
    ).replace(
        "{repo_hint}", repo_hint.strip() or "(unspecified)"
    ).replace(
        "{digest}", digest.strip() or "(empty digest)"
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


def _invoke_and_parse(
    llm: LLMClient, system: str, user: str, *, budget: int, label: str,
) -> tuple[list[Finding], dict]:
    """One single-shot invoke plus lenient JSON parse, shared by both reviewers.

    ``label`` names the caller in log lines (``chunk of 2 files``, ``systemic
    sweep``). The contract is the one :func:`review_chunk` documents: never
    raises, empty findings and zeros on failure, and truncation named as the
    cause — with the budget lever — when the budget is why the response was
    unusable.
    """
    t0 = time.perf_counter()
    meta = {
        "escalations": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "model": "",
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "error": "",
    }

    # The invoke and the parse are caught separately on purpose: only the
    # invoke's result knows WHY generation stopped, and the parse failure is
    # exactly where that reason has to be spoken. Neither is allowed to raise
    # out of this function — the never-raise contract is unchanged.
    try:
        result = llm.invoke(
            system=system,
            user=user,
            max_tokens=budget,
            json_mode=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("worker review failed for %s: %s", label, e)
        meta["error"] = f"{type(e).__name__}: {e}"
        meta["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return [], meta

    meta["input_tokens"] = result.input_tokens
    meta["output_tokens"] = result.output_tokens
    meta["model"] = result.model

    stop_reason = _budget_stop_reason(result)
    truncated_error = _TRUNCATED_ERROR.format(budget=budget, reason=stop_reason)

    try:
        parsed = loads_lenient(result.text)
    except Exception as e:  # noqa: BLE001
        # A truncated completion and a model that simply refused to emit JSON
        # produce the same JSONDecodeError, and only one of them has a lever
        # the operator can pull. Say which one this is.
        reason = truncated_error if stop_reason else f"{type(e).__name__}: {e}"
        logger.warning("worker review failed for %s: %s", label, reason)
        meta["error"] = reason
        meta["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return [], meta

    meta["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)

    if not isinstance(parsed, dict):
        # Valid JSON of the wrong shape is just as unusable as none, so it gets
        # the same treatment: if the budget is why it came out that way, say so
        # rather than reporting the shape and leaving the cause unspoken.
        detail = f"worker review JSON is not an object: {type(parsed).__name__}"
        logger.warning(detail)
        meta["error"] = truncated_error if stop_reason else detail
        return [], meta

    if stop_reason:
        # Parseable and usable, so the review still counts — but the model was
        # cut off mid-answer and the tail of its findings is gone. Loud, not
        # fatal. Logged only from here, where the response was actually used.
        logger.warning(
            "worker review for %s hit the completion budget "
            "(max_tokens=%d, finish_reason=%s); findings may be incomplete — "
            "raise %s",
            label, budget, stop_reason, _MAX_TOKENS_ENV,
        )

    raw_findings = parsed.get("findings")
    if not isinstance(raw_findings, list):
        raw_findings = []
    findings = [f for f in (_finding_from(r) for r in raw_findings) if f is not None]

    raw_esc = parsed.get("escalations")
    if not isinstance(raw_esc, list):
        raw_esc = []
    meta["escalations"] = [e for e in raw_esc if isinstance(e, dict)]

    return findings, meta


def review_chunk(
    llm: LLMClient,
    chunk: list[FileDiff],
    *,
    pr_title: str = "",
    pr_description: str = "",
    repo_hint: str = "",
    max_tokens: int | None = None,
    context_lines: int | None = None,
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

    When the response cannot be parsed and the backend reported
    ``finish_reason == "length"``, ``meta["error"]`` names the budget that was
    in force and the variable that raises it — the operator's lever — instead
    of a ``JSONDecodeError`` that looks like a model-quality problem. A clean
    empty response (any other finish reason) keeps the parse error verbatim
    and is never mislabelled as truncation. A response that parses to the wrong
    SHAPE is treated the same way: unusable is unusable, and the budget is named
    when the budget is why.

    ``max_tokens`` is the completion budget for the call; ``None`` keeps the
    module default :data:`MAX_TOKENS`, so direct callers are unaffected. The
    orchestrator always passes the keyword (``None`` included), so any test
    double for this function must accept it. The CLI threads
    ``PRXREF_LLM_MAX_TOKENS`` down to here.

    ``context_lines`` bounds the hunk context rendered into the prompt;
    ``None`` renders the parsed hunks verbatim, so direct callers are
    unaffected here too. The forge's diff is the only source of context —
    rendering can trim what was received, never add what it did not. The
    orchestrator always passes this keyword as well.
    """
    system, user = _render_prompt(
        chunk=chunk,
        pr_title=pr_title,
        pr_description=pr_description,
        repo_hint=repo_hint,
        context_lines=context_lines,
    )
    budget = MAX_TOKENS if max_tokens is None else max_tokens
    return _invoke_and_parse(
        llm, system, user, budget=budget, label=f"chunk of {len(chunk)} files",
    )


def review_systemic(
    llm: LLMClient,
    digest: str,
    *,
    pr_title: str = "",
    pr_description: str = "",
    repo_hint: str = "",
    max_tokens: int | None = None,
) -> tuple[list[Finding], dict]:
    """Review the whole-PR systemic digest with a single LLM call.

    The second-order complement to :func:`review_chunk`: chunk workers each
    see one slice of the diff, so cross-file classes — an unauthenticated
    handler, a secret in a client-exposed constant, a migration with no
    policy — have no seat that sees enough to name them. ``digest`` is the
    deterministic whole-PR text built by
    :func:`prxref.systemic.build_digest`; the prompt
    (``prompts/systemic.md``) restricts findings to those systemic classes.

    Returns ``(findings, meta)`` under exactly the :func:`review_chunk`
    contract — never raises, ``meta["error"]`` empty on success, truncation
    named when the budget is why — so the orchestrator can treat the sweep
    as one more worker-style unit for coverage accounting.
    """
    system, user = _render_systemic_prompt(
        digest=digest,
        pr_title=pr_title,
        pr_description=pr_description,
        repo_hint=repo_hint,
    )
    budget = MAX_TOKENS if max_tokens is None else max_tokens
    return _invoke_and_parse(llm, system, user, budget=budget, label="systemic sweep")
