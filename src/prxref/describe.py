"""PR auto-description and auto-labels (#37): the forge-independent half.

prxref can draft a pull request's description from its diff and propose
conventional labels for it. Both are opt-in (``PRXREF_DESCRIBE`` and
``PRXREF_LABELS``, both ``off`` by default). This module holds everything
that does not touch a forge: the marker that delimits the generated text, the
compose/split pair that keeps the author's original description verbatim,
the decision whether a write is allowed, the view of a description the
review prompts see, the label picker, and the one LLM call. The forge writes
(``update_description``, ``list_labels``, ``add_labels``) are optional
:class:`~prxref.forges.base.Forge` methods; the orchestrator wires the two.

The written body has this layout (``v1`` is the marker format version)::

    <!-- prxref:generated v1 head=<sha> at=<YYYY-MM-DDTHH:MM:SSZ> -->
    <generated markdown>
    <!-- /prxref:generated -->

    <details><summary>Original description</summary>

    <!-- prxref:original v1 -->
    <the author's original, byte for byte>
    <!-- /prxref:original -->

    </details>

The ``head`` and ``at`` fields pin the generated text to the PR head it was
written from and the moment it was written (the issue's "description-history
pinning"). The ``<details>`` section is omitted when the original is empty or
whitespace only. The comment markers, never the ``<details>`` HTML, delimit
the original, so an original that itself contains ``</details>`` survives the
round trip. The generated text is stripped of anything that looks like a
prxref marker before it is written, so a model reply cannot forge a
boundary.

Safety rules, enforced by :func:`decide_write`: with ``empty`` (the
recommended mode) a body that holds any human-written text is never
replaced; only ``force`` replaces it, and even then the original is kept in
the ``<details>`` section. Text a human adds around a generated block later
counts as human text, and :func:`split_description` folds it into the
original so a regeneration keeps it.

The describe prompt (``prompts/describe.md``) is packaged-only, like
``judge.md``: it is not one of the overridable
:data:`prxref.prompt_templates.TEMPLATE_NAMES`.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .llm import LLMClient
from .parser import loads_lenient
from .reviewer import _TRUNCATED_ERROR, _budget_stop_reason, _result_cost, fill_template, load_prompt

logger = logging.getLogger("prxref")

#: ``PRXREF_DESCRIBE`` values, restated in ``config._CHOICE_KEYS["describe"]``.
DESCRIBE_MODES: tuple[str, ...] = ("off", "empty", "force")

#: ``PRXREF_LABELS`` values, restated in ``config._CHOICE_KEYS["labels"]``.
LABEL_MODES: tuple[str, ...] = ("off", "existing", "create")

#: The only labels prxref proposes, in the order it applies them.
CONVENTIONAL_LABELS: tuple[str, ...] = (
    "feat", "fix", "docs", "refactor", "test", "chore", "ci", "perf", "breaking",
)

#: Common repository spellings of each conventional label. Matching is
#: casefolded, so only lowercase forms are listed.
LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "feat": ("feature", "features", "enhancement", "enhancements", "new feature"),
    "fix": ("bug", "bugs", "bugfix", "bug-fix", "bug fix", "fixes"),
    "docs": ("doc", "documentation"),
    "refactor": ("refactoring",),
    "test": ("tests", "testing"),
    "chore": ("chores", "maintenance"),
    "ci": ("continuous integration",),
    "perf": ("performance",),
    "breaking": ("breaking change", "breaking-change", "breaking changes", "breaking-changes"),
}

MARKER_VERSION = "v1"
GENERATED_OPEN_PREFIX = "<!-- prxref:generated "
GENERATED_CLOSE = "<!-- /prxref:generated -->"
ORIGINAL_OPEN = f"<!-- prxref:original {MARKER_VERSION} -->"
ORIGINAL_CLOSE = "<!-- /prxref:original -->"
DETAILS_OPEN = "<details><summary>Original description</summary>"
DETAILS_CLOSE = "</details>"

_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_GENERATED_OPEN_RE = re.compile(
    re.escape(GENERATED_OPEN_PREFIX) + r"(v\d+) head=(\S+) at=(\S+) -->\r?\n"
)
_GENERATED_CLOSE_RE = re.compile(r"\r?\n" + re.escape(GENERATED_CLOSE))
_ORIGINAL_OPEN_RE = re.compile(
    r"(?:" + re.escape(DETAILS_OPEN) + r"[ \t]*(?:\r?\n)*)?" + re.escape(ORIGINAL_OPEN) + r"\r?\n"
)
_ORIGINAL_CLOSE_RE = re.compile(
    r"\r?\n" + re.escape(ORIGINAL_CLOSE) + r"(?:\s*" + re.escape(DETAILS_CLOSE) + r")?"
)
# Anything shaped like a prxref describe marker, in either direction. Removed
# from generated text so a model reply can never open or close a block.
_ANY_MARKER_RE = re.compile(r"<!--\s*/?\s*prxref:(?:generated|original)\b.*?-->", re.DOTALL)

#: The heading that splits ``describe.md`` into system (above) and user (from it on).
DESCRIBE_SPLIT_MARKER = "## Pull Request"

#: The prompt's diff budget, in characters, unless the caller passes its own.
DEFAULT_MAX_DIFF_CHARS = 60_000

#: The longest generated description kept, in characters. Forges cap the
#: whole body (GitHub at 65,536 characters) and the original rides beside it.
MAX_GENERATED_CHARS = 20_000

_NONE_TEXT = "(none)"


@dataclass(frozen=True)
class DescriptionParts:
    """One PR body taken apart by :func:`split_description`.

    ``original`` is the human-written text: the preserved original plus any
    text a human added outside the prxref sections, or the whole body when
    it carries no generated block. ``generated`` is the generated markdown,
    or ``None`` when there is no complete block. ``head`` and ``at`` are the
    pinned PR head and generation time (``at`` is ``None`` when unreadable).
    ``edited`` is true when human text sits outside the prxref sections.
    """

    original: str
    generated: str | None = None
    head: str | None = None
    at: datetime | None = None
    version: str | None = None
    edited: bool = False


def _utc(at: datetime) -> datetime:
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)


def _sanitize_generated(text: str) -> str:
    return _ANY_MARKER_RE.sub("", text).strip()


def compose_description(generated: str, original: str, *, head_sha: str, at: datetime) -> str:
    """The PR body for ``generated`` text, keeping ``original`` verbatim below it.

    ``head_sha`` must be a non-empty token with no whitespace, else
    ``ValueError``; ``at`` is written in UTC to the second (a naive datetime
    is read as UTC). The generated text is stripped and cleaned of any prxref
    marker. The ``Original description`` section is omitted when
    ``original`` is empty or whitespace only; otherwise
    ``split_description(result).original == original``.

    To regenerate a body that already holds a block, pass
    ``split_description(current).original``, never ``current``, so blocks
    never nest.
    """
    if not isinstance(head_sha, str) or not re.fullmatch(r"\S+", head_sha):
        raise ValueError(f"head_sha must be a non-empty token without whitespace, got {head_sha!r}")
    stamp = _utc(at).strftime(_AT_FORMAT)
    body = (
        f"{GENERATED_OPEN_PREFIX}{MARKER_VERSION} head={head_sha} at={stamp} -->\n"
        f"{_sanitize_generated(generated)}\n"
        f"{GENERATED_CLOSE}\n"
    )
    if original and original.strip():
        body += (
            f"\n{DETAILS_OPEN}\n\n{ORIGINAL_OPEN}\n{original}\n{ORIGINAL_CLOSE}\n\n{DETAILS_CLOSE}\n"
        )
    return body


def _parse_at(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw, _AT_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def split_description(text: str) -> DescriptionParts:
    """Take a PR body apart into the human original and the generated block.

    Never raises. An empty or ``None`` body gives ``original == ""``. A body
    with no complete generated block (no marker, or an open marker with no
    close) and no preserved-original section is all original, returned
    verbatim. Otherwise the original is the preserved section, byte for
    byte, joined (blank-line separated, in document order) with any
    non-blank text a human wrote outside the prxref sections; that case sets
    ``edited``.
    """
    if not text:
        return DescriptionParts(original="")
    generated: str | None = None
    head: str | None = None
    at: datetime | None = None
    version: str | None = None
    outside: list[str] = []
    pos = 0

    opened = _GENERATED_OPEN_RE.search(text)
    if opened:
        closed = _GENERATED_CLOSE_RE.search(text, opened.end())
        if closed:
            version, head, raw_at = opened.group(1), opened.group(2), opened.group(3)
            at = _parse_at(raw_at)
            generated = text[opened.end():closed.start()]
            outside.append(text[:opened.start()])
            pos = closed.end()

    stored = ""
    found_original = False
    orig_open = _ORIGINAL_OPEN_RE.search(text, pos)
    if orig_open:
        ends = list(_ORIGINAL_CLOSE_RE.finditer(text, orig_open.end()))
        if ends:
            orig_close = ends[-1]
            found_original = True
            outside.append(text[pos:orig_open.start()])
            stored = text[orig_open.end():orig_close.start()]
            pos = orig_close.end()

    if generated is None and not found_original:
        return DescriptionParts(original=text)

    outside.append(text[pos:])
    before = [piece.strip() for piece in outside[:-1]]
    after = outside[-1].strip()
    pieces = [p for p in before if p]
    if stored.strip():
        pieces.append(stored)
    if after:
        pieces.append(after)
    edited = any(before) or bool(after)
    original = stored if not edited else "\n\n".join(pieces)
    if not original.strip():
        original = ""
    return DescriptionParts(
        original=original, generated=generated, head=head, at=at, version=version, edited=edited,
    )


def review_description(text: str) -> str:
    """The description the review prompts should see: the human text only.

    prxref must not read its own generated text back as author intent, so
    the generated block is removed and only :attr:`DescriptionParts.original`
    is returned. A body with no generated block comes back unchanged.
    """
    return split_description(text or "").original


def _require_mode(mode: str, modes: tuple[str, ...], setting: str) -> None:
    if mode not in modes:
        raise ValueError(f"{setting} must be one of {' | '.join(modes)}, got {mode!r}")


def decide_write(current: str, mode: str) -> tuple[bool, str]:
    """Whether the describe step may write the PR body: ``(write, reason)``.

    ``off`` never writes. ``empty`` writes only when there is no human text:
    an empty or blank body, or one that is only a previous prxref block (no
    preserved original, nothing added around it). ``force`` always writes;
    the caller still composes from ``split_description(current).original``,
    so the human text is kept. Any other ``mode`` raises ``ValueError``.
    """
    _require_mode(mode, DESCRIBE_MODES, "PRXREF_DESCRIBE")
    if mode == "off":
        return False, "PRXREF_DESCRIBE is off"
    parts = split_description(current or "")
    human = bool(parts.original.strip())
    if mode == "force":
        if human:
            return True, "forced; the human-written description is preserved below the generated one"
        return True, "forced; there is no human-written description"
    if human:
        return False, (
            "the PR has a human-written description; PRXREF_DESCRIBE=force replaces it "
            "(keeping the original)"
        )
    if parts.generated is not None:
        return True, "the PR body holds only a previous prxref-generated description"
    return True, "the PR has no description"


def _canonical(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    key = " ".join(name.strip().casefold().split())
    if key in CONVENTIONAL_LABELS:
        return key
    for label, aliases in LABEL_ALIASES.items():
        if key in aliases:
            return label
    return None


def conventional_labels(proposed: Sequence[Any]) -> list[str]:
    """``proposed`` mapped onto :data:`CONVENTIONAL_LABELS` (aliases included), deduplicated.

    Anything that is not a string, or names no conventional label, is dropped;
    the first occurrence's order is kept.
    """
    out: list[str] = []
    for name in proposed:
        label = _canonical(name)
        if label is not None and label not in out:
            out.append(label)
    return out


def pick_labels(
    proposed: Sequence[str], existing: Sequence[str] | None, mode: str,
) -> tuple[list[str], bool]:
    """The labels to apply and whether the forge may create missing ones.

    ``off``: ``([], False)``. ``existing``: each conventional proposal that
    matches a label the repository already has, case-insensitively and
    through :data:`LABEL_ALIASES`, in the repository's own spelling (an exact
    name beats an alias; otherwise the first match in ``existing`` order),
    with ``create_missing`` False; ``existing=None`` (the list could not be
    read) applies nothing. ``create``: the conventional names themselves,
    with ``create_missing`` True. Any other ``mode`` raises ``ValueError``.
    """
    _require_mode(mode, LABEL_MODES, "PRXREF_LABELS")
    if mode == "off":
        return [], False
    wanted = conventional_labels(proposed)
    if mode == "create":
        return wanted, True
    if existing is None:
        return [], False
    picked: list[str] = []
    for label in wanted:
        matches = [name for name in existing if isinstance(name, str) and _canonical(name) == label]
        if not matches:
            continue
        exact = [name for name in matches if name.strip().casefold() == label]
        choice = (exact or matches)[0]
        if choice not in picked:
            picked.append(choice)
    return picked, False


@dataclass
class DescribeResult:
    """One describe call's outcome plus its usage, in the reviewer's meta vocabulary.

    ``body`` is the generated markdown (``""`` when the call failed or the
    reply was unusable, with ``error`` saying why); ``labels`` are already
    restricted to :data:`CONVENTIONAL_LABELS`. The usage fields mean what
    they mean on a reviewer unit; :meth:`unit` hands them to
    :func:`prxref.costs.run_cost`.
    """

    body: str = ""
    labels: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    elapsed_ms: int = 0
    cost_usd: float | None = None
    cost_source: str = ""
    error: str = ""

    def unit(self) -> dict[str, Any]:
        """The run-cost unit for this call (the keys a reviewer meta dict carries)."""
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "elapsed_ms": self.elapsed_ms,
            "cost_usd": self.cost_usd,
            "cost_source": self.cost_source,
            "error": self.error,
        }


def _cap_diff(diff: str, limit: int) -> str:
    if limit <= 0 or len(diff) <= limit:
        return diff
    return diff[:limit] + f"\n... (diff truncated: {limit} of {len(diff)} characters shown)"


def render_describe_prompt(*, title: str, diff: str, original: str, max_diff_chars: int) -> tuple[str, str]:
    """The ``(system, user)`` pair for one describe call.

    ``original`` is passed through :func:`review_description`, so a previous
    generated block never reaches the model as the author's text.
    """
    template = load_prompt("describe")
    head, marker, tail = template.partition(DESCRIBE_SPLIT_MARKER)
    user = fill_template(marker + tail, {
        "title": (title or "").strip() or _NONE_TEXT,
        "original": review_description(original or "").strip() or _NONE_TEXT,
        "diff": _cap_diff(diff or "", max_diff_chars),
    })
    return head.rstrip() + "\n", user


def generate_description(
    llm: LLMClient,
    *,
    title: str,
    diff: str,
    original: str,
    max_tokens: int,
    timeout_s: float | None = None,
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
) -> DescribeResult:
    """Ask the model for a description and labels; never raises.

    One ``invoke`` with ``json_mode=True`` and ``max_tokens``; the wall-clock
    deadline is the backend's (``PRXREF_LLM_TIMEOUT``) unless ``timeout_s``
    is given, exactly as the review calls leave it. ``diff`` is the PR's
    unified diff text, capped at ``max_diff_chars``. The reply is parsed with
    :func:`prxref.parser.loads_lenient`; an invoke that raises, unparseable
    output, a non-object, or a missing or blank ``description`` gives
    ``body == ""`` with ``error`` set and one WARNING logged. A reply cut off
    at the budget names ``max_tokens`` and ``PRXREF_LLM_MAX_TOKENS``. Labels
    that are not a list, or name no conventional label, are dropped. There
    is no retry: a failed describe leaves the PR untouched.
    """
    t0 = time.perf_counter()
    res = DescribeResult()
    system, user = render_describe_prompt(
        title=title, diff=diff, original=original, max_diff_chars=max_diff_chars,
    )
    try:
        result = llm.invoke(system, user, max_tokens=max_tokens, json_mode=True, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001 -- the describe step never fails a review
        res.error = f"{type(exc).__name__}: {exc}"
        res.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.warning("describe: the model call failed: %s", res.error)
        return res

    res.elapsed_ms = int((time.perf_counter() - t0) * 1000)
    res.input_tokens = getattr(result, "input_tokens", 0) or 0
    res.output_tokens = getattr(result, "output_tokens", 0) or 0
    res.model = getattr(result, "model", "") or ""
    res.cost_usd, res.cost_source = _result_cost(result)

    stop_reason = _budget_stop_reason(result)
    text = getattr(result, "text", "")
    try:
        parsed = loads_lenient(text if isinstance(text, str) else "")
    except (json.JSONDecodeError, ValueError) as exc:
        res.error = (
            _TRUNCATED_ERROR.format(budget=max_tokens, reason=stop_reason)
            if stop_reason else f"describe reply is not JSON: {exc}"
        )
        logger.warning("describe: no description written: %s", res.error)
        return res
    if not isinstance(parsed, dict):
        res.error = f"describe reply is a JSON {type(parsed).__name__}, not an object"
        logger.warning("describe: no description written: %s", res.error)
        return res

    labels = parsed.get("labels")
    res.labels = conventional_labels(labels) if isinstance(labels, list) else []

    body = parsed.get("description")
    body = _sanitize_generated(body) if isinstance(body, str) else ""
    if not body:
        res.error = "describe reply has no description text"
        logger.warning("describe: no description written: %s", res.error)
        return res
    if len(body) > MAX_GENERATED_CHARS:
        logger.warning(
            "describe: generated description of %d characters cut to %d",
            len(body), MAX_GENERATED_CHARS,
        )
        body = body[:MAX_GENERATED_CHARS].rstrip()
    res.body = body
    return res
