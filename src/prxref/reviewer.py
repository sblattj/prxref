"""Worker-review layer: one LLM call per diff chunk, plus the systemic sweep.

The reviewer renders ``prompts/worker.md`` with the chunk's unified diff,
makes a single :meth:`LLMClient.invoke` call (no retries — the model
fallback chain handles transient failures), and maps the JSON response
onto :class:`prxref.triage.Finding` records. Unparseable or malformed
responses degrade to ``([], [])`` with a logged warning; this layer
never raises. :func:`review_systemic` is the same contract over the
whole-PR digest built by :mod:`prxref.systemic`, for the second-order
classes no single chunk seat can see.

Both calls take optional supplementary context, and both degrade to the
pre-existing prompt when it is absent: :func:`review_chunk` takes
``context_blocks`` (the dependency-version and referenced-definition
blocks the orchestrator builds from an optional forge
``get_file_content``) and ``sibling_files`` (the whole PR's parsed files,
reduced by :func:`prxref.chunk_context.sibling_summary_block` to a bounded
summary of what the chunk's siblings change, so a worker cannot conclude
from one file that something is absent when a sibling file in the same
diff refutes it), and :func:`review_systemic` takes ``threads`` (the
PR's existing review discussion, rendered by
:func:`_render_discussion_block` under the ``DISCUSSION_MAX_*`` caps) so
the sweep stops re-raising subjects the team already argued out.

Inputs an operator or a ticket supplies for the whole run ride one frozen
:class:`PromptContext` through every hop, in one fixed order: team rules and
the ticket-scope instructions are appended to the SYSTEM half (policy), while
the ticket context and the spec digest are filled into the USER half ahead of
the diff (per-PR data). While the ticket-scope instructions are in force, the
``## Output Format`` JSON example that ends the USER half also shows a
``"scope": "in"`` key on its finding, because a model copies the example it
read last; the key is filled into the template's ``{scope_example}`` slot. Each
template is filled in one pass by :func:`fill_template`, so a value that
contains another placeholder (a PR description quoting ``{diff}``) renders
literally. With :data:`NO_PROMPT_CONTEXT` the system prompt is the template
head unchanged and the user prompt gains nothing: the ``{scope_example}`` slot
renders empty, so the example is the pre-ticket one byte for byte.

Both ``prompts/worker.md`` and
``prompts/systemic.md`` require a throw/panic/crash/unhandled-rejection
finding to name its containment boundary; :func:`prxref.quality.apply_containment_note`
enforces that deterministically on any finding that skips it.

When a response is unparseable *because* the model ran out of completion
budget (``finish_reason == "length"``), the reported error names the budget
and the variable that raises it rather than the bare ``JSONDecodeError`` the
operator cannot act on. A truncated-but-parseable response is logged at
warning level and still counted as reviewed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any

from .chunk_context import sibling_summary_block
from .costs import valid_usd
from .forges.base import Thread
from .llm import LLMClient
from .parser import loads_lenient
from .triage import SCOPE_IN, SCOPE_UNKNOWN, FileDiff, Finding, normalize_scope, trim_hunk_context

logger = logging.getLogger("prxref")

MAX_TOKENS = 4096  # fallback only; the configured budget arrives per call
DEFAULT_CONFIDENCE = 0.5

_CONTEXT_MARKER = "## Review Context"

_NO_SPECS_TEXT = "(no specs provided for this review)"

# Fills the ``{scope_example}`` slot glued to the example finding's last value
# in both templates' ``## Output Format``: the comma travels with the key, so
# the empty value a no-ticket run gets leaves the example valid and unchanged.
_SCOPE_EXAMPLE = f',\n      "scope": "{SCOPE_IN}"'

_MAX_TOKENS_ENV = "PRXREF_LLM_MAX_TOKENS"

# Caps on the ``### Existing discussion`` block appended to the sweep prompt.
# The sweep's measured input is 1878-3585 tokens, so the discussion is held to
# roughly 300 tokens: it informs the sweep, it never rivals the digest.
DISCUSSION_MAX_THREADS = 15
DISCUSSION_MAX_SNIPPET_CHARS = 200
DISCUSSION_MAX_CHARS = 1200

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


def fill_template(template: str, values: Mapping[str, str]) -> str:
    """Replace each ``{name}`` in ``template`` whose name is a key of ``values``.

    One :func:`re.sub` pass over the template: substituted text is never
    scanned again, so a value that itself contains ``{diff}`` or any other
    placeholder renders literally instead of receiving that placeholder's
    value. Braces whose name is not a key (the JSON example in
    ``## Output Format``, a literal ``{foo}``) stay as written. Values must
    be strings and are inserted verbatim; backslashes are not interpreted.
    An empty ``values`` returns the template unchanged.
    """
    if not values:
        return template
    pattern = re.compile(r"\{(" + "|".join(re.escape(k) for k in values) + r")\}")
    return pattern.sub(lambda m: values[m.group(1)], template)


@dataclass(frozen=True)
class PromptContext:
    """Run-wide inputs injected into every review unit's prompt, in one fixed order.

    SYSTEM half, appended to the template head in this order:
    ``rules_worker`` for chunk units or ``rules_sweep`` for the sweep (the
    team review rules), then ``ticket_scope`` (the instructions that ask the
    model for a per-finding ``scope``). USER half, after the Review Context
    lines: ``ticket_context`` (the fenced ticket text), then ``spec_digest``
    (the Spec constraints block), then the diff or digest.

    Every field defaults to ``""``, which injects nothing; ``spec_digest``
    empty renders ``(no specs provided for this review)`` as before.
    :attr:`scope_active` is true only when the scope instructions are in the
    prompt, and it alone decides whether a model-supplied ``scope`` is read
    and whether the ``## Output Format`` example finding shows a ``"scope"``
    key.
    """

    rules_worker: str = ""
    rules_sweep: str = ""
    ticket_scope: str = ""
    ticket_context: str = ""
    spec_digest: str = ""

    @property
    def scope_active(self) -> bool:
        """True when the prompt asks for ``scope``, so the answer may be kept."""
        return bool(self.ticket_scope)


NO_PROMPT_CONTEXT = PromptContext()


def _append_block(system: str, block: str) -> str:
    block = block.strip()
    return f"{system}\n\n{block}" if block else system


def _ticket_context_value(prompt_context: PromptContext) -> str:
    block = prompt_context.ticket_context.strip()
    return f"{block}\n\n" if block else ""


def _scope_example_value(prompt_context: PromptContext) -> str:
    return _SCOPE_EXAMPLE if prompt_context.scope_active else ""


def _render_file(f: FileDiff, context_lines: int | None = None) -> str:
    old = f.old_path or f.new_path or f.path
    new = f.new_path or f.old_path or f.path
    out = [f"diff --git a/{old} b/{new}"]
    if f.status == "renamed" and f.old_path and f.new_path:
        out.append(f"rename from {f.old_path}")
        out.append(f"rename to {f.new_path}")
    if f.status == "copied" and f.old_path and f.new_path:
        out.append(f"copy from {f.old_path}")
        out.append(f"copy to {f.new_path}")
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
    context_blocks: str = "",
    sibling_files: Sequence[FileDiff] = (),
    *,
    prompt_context: PromptContext = NO_PROMPT_CONTEXT,
) -> tuple[str, str]:
    template = load_prompt("worker.md")
    head, marker, tail = template.partition(_CONTEXT_MARKER)
    if not marker:
        raise ValueError(f"worker.md is missing the {_CONTEXT_MARKER!r} split marker")
    sibling_block = sibling_summary_block(chunk, sibling_files)
    blocks = "\n\n".join(b for b in (sibling_block, context_blocks.strip()) if b)
    user = fill_template(marker + tail, {
        "pr_title": pr_title.strip() or "(untitled)",
        "pr_description": pr_description.strip() or "(none)",
        "repo_hint": repo_hint.strip() or "(unspecified)",
        "ticket_context": _ticket_context_value(prompt_context),
        "spec_digest": prompt_context.spec_digest.strip() or _NO_SPECS_TEXT,
        "context_blocks": blocks,
        "diff": render_chunk(chunk, context_lines) or "(empty chunk)",
        "scope_example": _scope_example_value(prompt_context),
    })
    system = _append_block(head.strip(), prompt_context.rules_worker)
    system = _append_block(system, prompt_context.ticket_scope)
    return system, user.strip()


def _render_discussion_block(threads: Sequence[Thread]) -> str:
    """The ``### Existing discussion`` block for the sweep user prompt.

    One ``- <path>: <author>: <snippet>`` line per thread, snippets truncated
    to :data:`DISCUSSION_MAX_SNIPPET_CHARS`, the block capped at
    :data:`DISCUSSION_MAX_THREADS` threads and
    :data:`DISCUSSION_MAX_CHARS` characters so the discussion cannot rival the
    digest for prompt budget. Returns ``""`` when there is nothing to say, so
    an absent discussion prints no header at all.
    """
    lines: list[str] = []
    used = 0
    shown = 0
    for t in threads[:DISCUSSION_MAX_THREADS]:
        snippet = " ".join((t.body_snippet or "").split())
        if not snippet:
            continue
        if len(snippet) > DISCUSSION_MAX_SNIPPET_CHARS:
            snippet = snippet[:DISCUSSION_MAX_SNIPPET_CHARS].rstrip() + "…"
        line = f"- {t.path}: {t.author or 'unknown'}: {snippet}"
        if used + len(line) + 1 > DISCUSSION_MAX_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    if not lines:
        return ""
    omitted = len([t for t in threads if (t.body_snippet or "").strip()]) - shown
    if omitted > 0:
        lines.append(f"… {omitted} more threads omitted")
    return "### Existing discussion\n\n" + "\n".join(lines)


def _render_systemic_prompt(
    digest: str,
    pr_title: str,
    pr_description: str,
    repo_hint: str,
    threads: Sequence[Thread] = (),
    *,
    prompt_context: PromptContext = NO_PROMPT_CONTEXT,
) -> tuple[str, str]:
    template = load_prompt("systemic.md")
    head, marker, tail = template.partition(_CONTEXT_MARKER)
    if not marker:
        raise ValueError(f"systemic.md is missing the {_CONTEXT_MARKER!r} split marker")
    user = fill_template(marker + tail, {
        "pr_title": pr_title.strip() or "(untitled)",
        "pr_description": pr_description.strip() or "(none)",
        "repo_hint": repo_hint.strip() or "(unspecified)",
        "ticket_context": _ticket_context_value(prompt_context),
        "spec_digest": prompt_context.spec_digest.strip() or _NO_SPECS_TEXT,
        "digest": digest.strip() or "(empty digest)",
        "scope_example": _scope_example_value(prompt_context),
    })
    discussion = _render_discussion_block(threads)
    user = user.strip()
    if discussion:
        user = f"{user}\n\n{discussion}"
    system = _append_block(head.strip(), prompt_context.rules_sweep)
    system = _append_block(system, prompt_context.ticket_scope)
    return system, user


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _finding_from(raw: Any, *, accept_scope: bool = False) -> Finding | None:
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
        scope=normalize_scope(raw.get("scope")) if accept_scope else SCOPE_UNKNOWN,
    )


def _write_trace_files(
    trace_dir: str,
    trace_label: str,
    system: str,
    user: str,
    raw_text: str | None,
    meta: dict,
) -> None:
    """Dump one review unit's prompt, response, and cost meta under ``trace_dir``.

    Writes four files named after ``trace_label`` (``chunk0``, ``sweep``, …):
    ``<label>.system.md`` and ``<label>.user.md`` are the exact rendered
    prompt halves, ``<label>.response.json`` is the raw model text JSON-encoded
    so any JSON reader gets it back verbatim (``null`` when the call never
    produced a response), and ``<label>.meta.json`` carries ``unit``, ``model``,
    token counts, ``elapsed_ms``, ``error``, ``cost_usd`` (the dollar figure
    the backend reported for the call, ``null`` when it reported none) and
    ``cost_source`` (where that figure came from, ``""`` when ``null``).

    Each file lands via a temp file plus :func:`os.replace`, so a concurrent
    reader never observes a half-written file, and a timeout retry simply
    overwrites: the trace ends up showing the attempt whose result was used.
    Empty ``trace_dir`` is the declared off switch and does nothing — no
    directory, no syscalls, no cost. Every failure (directory cannot be
    created, path unwritable, disk full) is a logged warning and nothing more:
    tracing must never be able to fail a review.
    """
    if not trace_dir or not trace_label:
        return
    try:
        os.makedirs(trace_dir, exist_ok=True)
        base = os.path.join(trace_dir, trace_label)
        payload = {
            "unit": trace_label,
            "model": meta.get("model", ""),
            "input_tokens": meta.get("input_tokens", 0),
            "output_tokens": meta.get("output_tokens", 0),
            "elapsed_ms": meta.get("elapsed_ms", 0),
            "error": meta.get("error", ""),
            "cost_usd": meta.get("cost_usd"),
            "cost_source": meta.get("cost_source", ""),
        }
        files = [
            (".system.md", system),
            (".user.md", user),
            (".response.json", json.dumps(raw_text, ensure_ascii=False)),
            (".meta.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n"),
        ]
        for suffix, data in files:
            tmp = base + suffix + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, base + suffix)
    except OSError as e:
        logger.warning("trace write for %s failed (continuing): %s", trace_label, e)


def _invoke_and_parse(
    llm: LLMClient, system: str, user: str, *, budget: int, label: str,
    trace_dir: str = "", trace_label: str = "", accept_scope: bool = False,
) -> tuple[list[Finding], dict]:
    """One single-shot invoke plus lenient JSON parse, shared by both reviewers.

    ``label`` names the caller in log lines (``chunk of 2 files``, ``systemic
    sweep``). The contract is the one :func:`review_chunk` documents: never
    raises, empty findings and zeros on failure, and truncation named as the
    cause — with the budget lever — when the budget is why the response was
    unusable.

    ``trace_dir`` with ``trace_label`` turns on the per-unit prompt/response
    dump (:func:`_write_trace_files`); the default empty ``trace_dir`` keeps
    the write path dormant.

    ``accept_scope`` keeps each finding's model-supplied ``scope`` (through
    :func:`prxref.triage.normalize_scope`); false, the default, stamps every
    finding ``unknown``, because a prompt that never asked for ``scope`` has
    no answer worth reading.
    """
    t0 = time.perf_counter()
    meta = {
        "escalations": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "model": "",
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "error": "",
        "cost_usd": None,
        "cost_source": "",
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
        _write_trace_files(trace_dir, trace_label, system, user, None, meta)
        return [], meta

    meta["input_tokens"] = result.input_tokens
    meta["output_tokens"] = result.output_tokens
    meta["model"] = result.model
    meta["cost_usd"] = valid_usd(getattr(result, "cost_usd", None))
    meta["cost_source"] = str(getattr(result, "cost_source", "") or "") if meta["cost_usd"] is not None else ""

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
        _write_trace_files(trace_dir, trace_label, system, user, result.text, meta)
        return [], meta

    meta["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)

    if not isinstance(parsed, dict):
        # Valid JSON of the wrong shape is just as unusable as none, so it gets
        # the same treatment: if the budget is why it came out that way, say so
        # rather than reporting the shape and leaving the cause unspoken.
        detail = f"worker review JSON is not an object: {type(parsed).__name__}"
        logger.warning(detail)
        meta["error"] = truncated_error if stop_reason else detail
        _write_trace_files(trace_dir, trace_label, system, user, result.text, meta)
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
    findings = [
        f for f in (_finding_from(r, accept_scope=accept_scope) for r in raw_findings)
        if f is not None
    ]

    raw_esc = parsed.get("escalations")
    if not isinstance(raw_esc, list):
        raw_esc = []
    meta["escalations"] = [e for e in raw_esc if isinstance(e, dict)]

    _write_trace_files(trace_dir, trace_label, system, user, result.text, meta)
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
    context_blocks: str = "",
    sibling_files: Sequence[FileDiff] = (),
    trace_dir: str = "",
    trace_label: str = "",
    prompt_context: PromptContext = NO_PROMPT_CONTEXT,
) -> tuple[list[Finding], dict]:
    """Review one chunk with a single LLM call.

    Returns ``(findings, meta)`` where ``meta`` carries ``escalations`` plus
    cost telemetry (``input_tokens``, ``output_tokens``, ``model``,
    ``elapsed_ms`` — zeros when the call failed — and ``cost_usd`` /
    ``cost_source``, ``None`` / ``""`` when the call failed or the backend
    reported no cost). Severity passes through
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

    ``context_blocks`` is pre-rendered supplementary context — dependency pins
    and out-of-hunk definitions built by :mod:`prxref.chunk_context` — placed
    after the diff, inside the ``user`` half of the prompt. The empty default
    renders nothing at all, leaving no stray header for direct callers.

    ``sibling_files`` is the PR's full parsed file list; files already in
    ``chunk`` are skipped. The rest are summarized — path, status, +/- counts,
    and a bounded excerpt of each one's added/context hunk lines — under an
    ``### Other files changed in this PR`` block placed before
    ``context_blocks``, so a claim that something is absent, unsupported, or
    contradicted can be checked against the sibling evidence the chunk split
    moved out of view. The empty default renders no block at all.

    ``trace_dir`` with ``trace_label`` (``chunk0``, ``chunk1``, …) writes the
    unit's exact prompts, raw response, and cost meta to four files under
    that directory (:func:`_write_trace_files`); the empty default traces
    nothing. The orchestrator passes both, so ``PRXREF_TRACE_DIR`` covers
    every chunk without any per-caller wiring.

    ``prompt_context`` carries the run-wide injected inputs
    (:class:`PromptContext`): ``rules_worker`` and ``ticket_scope`` are
    appended to the system prompt, ``ticket_context`` and ``spec_digest`` are
    filled into the user prompt before the diff. An empty ``spec_digest``
    renders the literal ``(no specs provided for this review)``, and the
    prompt tells the model ``spec`` is then not a legal severity. A finding's
    ``scope`` is read from the response only when
    :attr:`PromptContext.scope_active`; otherwise it is ``unknown``. The
    default :data:`NO_PROMPT_CONTEXT` injects nothing. The orchestrator
    always passes this keyword too, so any test double must accept it.
    """
    system, user = _render_prompt(
        chunk=chunk,
        pr_title=pr_title,
        pr_description=pr_description,
        repo_hint=repo_hint,
        context_lines=context_lines,
        context_blocks=context_blocks,
        sibling_files=sibling_files,
        prompt_context=prompt_context,
    )
    budget = MAX_TOKENS if max_tokens is None else max_tokens
    return _invoke_and_parse(
        llm, system, user, budget=budget, label=f"chunk of {len(chunk)} files",
        trace_dir=trace_dir, trace_label=trace_label,
        accept_scope=prompt_context.scope_active,
    )


def review_systemic(
    llm: LLMClient,
    digest: str,
    *,
    pr_title: str = "",
    pr_description: str = "",
    repo_hint: str = "",
    max_tokens: int | None = None,
    threads: Sequence[Thread] = (),
    trace_dir: str = "",
    trace_label: str = "",
    prompt_context: PromptContext = NO_PROMPT_CONTEXT,
) -> tuple[list[Finding], dict]:
    """Review the whole-PR systemic digest with a single LLM call.

    The second-order complement to :func:`review_chunk`: chunk workers each
    see one slice of the diff, so cross-file classes — an unauthenticated
    handler, a secret in a client-exposed constant, a migration with no
    policy — have no seat that sees enough to name them. ``digest`` is the
    deterministic whole-PR text built by
    :func:`prxref.systemic.build_digest`; the prompt
    (``prompts/systemic.md``) restricts findings to those systemic classes.

    ``prompt_context`` works as in :func:`review_chunk`, except that the
    sweep's system prompt takes ``rules_sweep`` instead of ``rules_worker``.
    Its ``spec_digest`` rides the same prompt under the Spec constraints
    block: empty renders ``(no specs provided for this review)``, and with
    the whole-diff digest plus any spec constraints in view, this sweep is
    the natural seat for cross-file spec classes.

    Returns ``(findings, meta)`` under exactly the :func:`review_chunk`
    contract — never raises, ``meta["error"]`` empty on success, truncation
    named when the budget is why — so the orchestrator can treat the sweep
    as one more worker-style unit for coverage accounting.

    ``trace_dir``/``trace_label`` work exactly as in :func:`review_chunk`;
    the orchestrator passes ``trace_label="sweep"`` so the sweep's prompt and
    response land beside the chunks' when ``PRXREF_TRACE_DIR`` is set.
    """
    system, user = _render_systemic_prompt(
        digest=digest,
        pr_title=pr_title,
        pr_description=pr_description,
        repo_hint=repo_hint,
        threads=threads,
        prompt_context=prompt_context,
    )
    budget = MAX_TOKENS if max_tokens is None else max_tokens
    return _invoke_and_parse(
        llm, system, user, budget=budget, label="systemic sweep",
        trace_dir=trace_dir, trace_label=trace_label,
        accept_scope=prompt_context.scope_active,
    )
