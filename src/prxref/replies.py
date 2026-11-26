"""Opt-in AI replies in review threads (issue #32): one bounded model call per reply.

A reply sweep reads every conversation a forge reports through the optional
:meth:`~prxref.forges.base.Forge.list_conversations` (each prxref-rooted
inline thread, plus the PR's top-level comment stream) and answers the ones
that are due, one single-shot call each, with pre-gathered context and no
tools, no follow-up and no retry beyond the client's own fallback chain.

Trigger rule (:func:`classify`), stateless so a redelivered webhook or a
re-run sweep is idempotent:

- a conversation is due only when its last comment is human (does not carry
  :data:`~prxref.forges.base.ATTRIBUTION_MARKER`). prxref's own reply is
  therefore the end of the conversation until a person writes again, which is
  the loop guard and the dedup at once;
- ``mention`` mode needs the mention token (case-insensitive) in that last
  comment; ``always`` mode answers any last human reply in an inline thread;
  a ``pr`` conversation needs the token in both modes;
- a settled inline thread (resolved, an explicit won't-fix from a person,
  or a last human reply that rebuts the finding without asking a question)
  is skipped as ``settled`` unless the last comment names the token, in
  which case the prompt says the finding is settled and must not be
  re-raised.

Context is gathered deterministically and capped at ``context_chars``: the
conversation's comments (the root finding first), then for an inline thread
the diff hunk(s) of its path covering its line and a window of the head file
around it; for a ``pr`` conversation the summary body and the diff's file
list. Every piece of it is wrapped in an untrusted data block whose
delimiters the data cannot forge. A sweep never creates findings or inline
comments, only replies, and each reply carries the model attribution line.

The sweep never raises for a review-type failure: a failed read, call or
post is a ``failed`` record, its reason redacted for posting.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any

from .forges.base import (
    ATTRIBUTION_MARKER,
    SUMMARY_MARKER,
    Conversation,
    ConversationComment,
    PRRef,
    dismissal_reply,
    says_wont_fix,
)
from .formatter import build_attribution
from .orchestrator import redact_for_post
from .prompt_templates import REPLY_SPLIT_MARKER, packaged_reply_text
from .reviewer import fill_template
from .triage import FileDiff, Hunk, parse_unified_diff

logger = logging.getLogger("prxref")

#: ``PRXREF_THREAD_REPLIES`` values.
REPLY_MODES = ("off", "mention", "always")
DEFAULT_MODE = "off"
DEFAULT_MENTION = "@prxref"
#: ``PRXREF_REPLY_MAX``: model calls per sweep.
DEFAULT_MAX_REPLIES = 5
#: ``PRXREF_REPLY_CONTEXT_CHARS``: characters of data one reply prompt may carry.
DEFAULT_CONTEXT_CHARS = 24_000

#: Completion budget of one reply call; the answer is a short Markdown paragraph.
MAX_TOKENS = 1024
#: Wall-clock deadline of one reply call, in seconds.
TIMEOUT_S = 120.0
#: Head-file lines served on each side of an inline thread's line.
WINDOW_LINES = 30
#: A model answer is cut to this many characters before it is posted.
MAX_REPLY_CHARS = 4000
#: Files listed for a ``pr`` conversation.
MAX_LISTED_FILES = 200

#: The heading that splits ``reply.md`` into system (above) and user (from it on).
SPLIT_MARKER = REPLY_SPLIT_MARKER
#: Opens an untrusted data block; the label follows on the same line.
DATA_OPEN = "<<<BEGIN UNTRUSTED DATA"
#: Closes an untrusted data block.
DATA_CLOSE = "<<<END UNTRUSTED DATA>>>"

_LABEL_RE = re.compile(r"[^\w./@:+#-]")

_OPEN = "The conversation is open."
_SETTLED = (
    "This conversation is SETTLED: the thread is resolved or a person declined the change. "
    "The finding must not be re-raised or re-argued. Answer only what the latest comment asks, "
    "and accept the decision."
)
_PR = (
    "The question was asked in the pull request's top-level comments, not under one finding. "
    "The review summary and the changed files are under Code."
)


def _neutral(text: str) -> str:
    return text.replace("<<<", "<< <")


def _label(text: object) -> str:
    return _LABEL_RE.sub("_", str(text or ""))[:120] or "_"


def fence(label: str, text: str) -> str:
    """Wrap ``text`` in an untrusted data block that ``text`` cannot close early.

    Every ``<<<`` inside ``text`` is broken up, so neither :data:`DATA_OPEN`
    nor :data:`DATA_CLOSE` can occur in the body; ``label`` keeps only
    word characters and ``./@:+#-``.
    """
    return f"{DATA_OPEN}: {_label(label)}>>>\n{_neutral(text)}\n{DATA_CLOSE}"


def _cut(text: str, room: int) -> str:
    if len(text) <= room:
        return text
    if room <= 0:
        return ""
    return text[:room].rstrip() + "\n[truncated]"


def mentioned(text: str, mention: str) -> bool:
    """Whether ``text`` contains ``mention``, case-insensitively; an empty token never matches."""
    return bool(mention) and mention.lower() in (text or "").lower()


def is_settled(conv: Conversation) -> bool:
    """Whether a person already decided an inline thread (#32), so a reply must not re-argue it.

    True when the thread is resolved, when any human comment is an explicit
    won't-fix (:func:`~prxref.forges.base.says_wont_fix`), or when its last
    human reply rebuts the finding (:func:`~prxref.forges.base.dismissal_reply`)
    without asking anything: a reply holding a ``?`` is a question, which is
    what this feature answers. A ``pr`` conversation is never settled.
    """
    if conv.kind != "inline":
        return False
    if conv.resolved:
        return True
    if any(says_wont_fix(c.body) for c in conv.comments if not c.is_prxref):
        return True
    dismissal = dismissal_reply([(c.author, c.body) for c in conv.comments])
    return bool(dismissal) and "?" not in dismissal


def classify(conv: Conversation, *, mode: str, mention: str) -> tuple[bool, str, bool]:
    """``(due, reason, settled)`` for one conversation under ``mode`` (the module docstring's rule).

    ``reason`` is ``""`` when due, else one of ``empty``, ``not-prxref``,
    ``answered-already``, ``no-mention`` and ``settled``.
    """
    if not conv.comments:
        return False, "empty", False
    if conv.kind == "inline" and not conv.comments[0].is_prxref:
        return False, "not-prxref", False
    last = conv.comments[-1]
    if last.is_prxref:
        return False, "answered-already", False
    named = mentioned(last.body, mention)
    if (conv.kind != "inline" or mode == "mention") and not named:
        return False, "no-mention", False
    settled = is_settled(conv)
    if settled and not named:
        return False, "settled", True
    return True, "", settled


def _render_hunk(h: Hunk) -> str:
    head = f"@@ -{h.old_start},{h.old_count} +{h.new_start},{h.new_count} @@"
    return "\n".join([head, *(f"{ln.kind}{ln.text}" for ln in h.lines)])


def _file_of(files: list[FileDiff], path: str) -> FileDiff | None:
    for f in files:
        if path in (f.path, f.new_path):
            return f
    return None


def hunks_at(files: list[FileDiff], path: str, line: int) -> str:
    """The rendered hunk(s) of ``path`` covering new-file ``line``, else the nearest one, else ``""``."""
    f = _file_of(files, path)
    if f is None or not f.hunks:
        return ""
    covering = [h for h in f.hunks if h.new_start <= line < h.new_start + max(h.new_count, 1)]
    if not covering:
        covering = [min(f.hunks, key=lambda h: min(abs(h.new_start - line), abs(h.new_start + h.new_count - line)))]
    return "\n".join(_render_hunk(h) for h in covering)


def head_window(text: str, line: int) -> tuple[int, str]:
    """``(first_line, numbered_text)``: :data:`WINDOW_LINES` head lines on each side of ``line``."""
    lines = text.splitlines()
    start = max(line - 1 - WINDOW_LINES, 0)
    kept = lines[start : line + WINDOW_LINES]
    return start + 1, "\n".join(f"{start + 1 + i:>5}| {row}" for i, row in enumerate(kept))


class _Sources:
    """Lazy, once-per-sweep reads of the diff, the head SHA and the summary; never raises."""

    def __init__(self, forge: Any, ref: PRRef) -> None:
        self._forge = forge
        self._ref = ref
        self._files: list[FileDiff] | None = None
        self._sha: str | None = None
        self._sha_read = False
        self._summary: str | None = None
        self._summary_read = False

    def files(self) -> list[FileDiff]:
        if self._files is None:
            try:
                self._files = parse_unified_diff(self._forge.get_diff(self._ref) or "")
            except Exception as e:  # noqa: BLE001 -- context is best-effort
                logger.warning("thread replies: the diff could not be read: %s", e)
                self._files = []
        return self._files

    def head_sha(self) -> str | None:
        if not self._sha_read:
            self._sha_read = True
            try:
                self._sha = getattr(self._forge.get_pr(self._ref), "source_sha", None) or None
            except Exception as e:  # noqa: BLE001
                logger.warning("thread replies: the head commit could not be read: %s", e)
        return self._sha

    def head_file(self, path: str) -> str | None:
        read = getattr(self._forge, "get_file_content", None)
        sha = self.head_sha()
        if not callable(read) or not sha:
            return None
        try:
            text = read(self._ref, path, sha=sha)
        except Exception:  # noqa: BLE001
            return None
        return text if isinstance(text, str) else None

    def summary(self) -> str | None:
        if not self._summary_read:
            self._summary_read = True
            read = getattr(self._forge, "get_summary", None)
            if callable(read):
                try:
                    body = read(self._ref)
                except Exception as e:  # noqa: BLE001
                    logger.warning("thread replies: the summary could not be read: %s", e)
                    body = None
                self._summary = body if isinstance(body, str) else None
        return self._summary


def _comment_block(i: int, c: ConversationComment, room: int) -> str:
    who = "prxref" if c.is_prxref else "human"
    return fence(f"comment {i} by {c.author} ({who})", _cut(c.body or "", room))


def gather_context(conv: Conversation, sources: _Sources, *, context_chars: int) -> tuple[str, str]:
    """``(conversation, code)``: the fenced data of one prompt, at most about ``context_chars`` long.

    The root comment (an inline thread's finding) and the last comment are
    always kept, each cut to a third of the budget. What is left goes, in
    order, to the diff hunk (an inline thread) or the summary (``pr``), then
    to the comments between them, newest first, then to the head-file window
    (an inline thread) or the file list (``pr``). A piece that does not fit
    is cut, or left out with a note.
    """
    budget = max(int(context_chars), 1)
    third = max(budget // 3, 1)
    comments = [c for c in conv.comments if SUMMARY_MARKER not in (c.body or "")] or list(conv.comments[-1:])
    inline = conv.kind == "inline"
    first = comments[0] if inline and len(comments) > 1 else None
    last = comments[-1]
    middle = comments[1:-1] if first is not None else comments[:-1]
    first_block = _comment_block(1, first, third) if first is not None else ""
    last_block = _comment_block(len(comments), last, third)
    left = budget - len(first_block) - len(last_block)

    code: list[str] = []
    if inline and conv.path and conv.line:
        hunk = hunks_at(sources.files(), conv.path, conv.line)
        if hunk and left > 0:
            block = fence(f"diff hunk {conv.path}", _cut(hunk, left))
            code.append(block)
            left -= len(block)
    elif not inline:
        summary = sources.summary()
        if summary and left > 0:
            block = fence("review summary", _cut(summary, left))
            code.append(block)
            left -= len(block)

    kept: list[str] = []
    omitted = 0
    offset = 2 if first is not None else 1
    for i in range(len(middle) - 1, -1, -1):
        block = _comment_block(i + offset, middle[i], len(middle[i].body or ""))
        if len(block) <= left:
            kept.insert(0, block)
            left -= len(block)
        else:
            omitted = i + 1
            break
    parts = [first_block] if first_block else []
    if omitted:
        parts.append(f"[{omitted} earlier comment(s) left out to fit the context budget]")
    parts += kept + [last_block]

    if inline and conv.path and conv.line:
        text = sources.head_file(conv.path)
        if text and left > 0:
            start, window = head_window(text, conv.line)
            code.append(fence(f"head file {conv.path} from line {start}", _cut(window, left)))
    elif not inline:
        paths = [f.path for f in sources.files()][:MAX_LISTED_FILES]
        if paths and left > 0:
            code.append(fence("changed files", _cut("\n".join(paths), left)))
    if inline and not (conv.path and conv.line):
        code.append("(This comment is not anchored to a current line, so no code is shown.)")
    return "\n\n".join(parts), "\n\n".join(code) or "(no code could be read)"


def render_prompt(
    conv: Conversation, sources: _Sources, *, settled: bool, context_chars: int, template: str | None = None,
) -> tuple[str, str]:
    """The ``(system, user)`` pair for one reply call, from ``template`` or the packaged ``reply.md``."""
    text = template or packaged_reply_text()
    system, marker, tail = text.partition(SPLIT_MARKER)
    conversation, code = gather_context(conv, sources, context_chars=context_chars)
    status = _PR if conv.kind != "inline" else _SETTLED if settled else _OPEN
    user = fill_template(marker + tail, {"status": status, "conversation": conversation, "code": code})
    return system.rstrip() + "\n", user


def clean_answer(text: str) -> str:
    """The model's answer as posted: prxref's markers removed, cut to :data:`MAX_REPLY_CHARS`."""
    out = (text or "").replace(SUMMARY_MARKER, "").replace(ATTRIBUTION_MARKER, "").strip()
    if len(out) > MAX_REPLY_CHARS:
        out = out[:MAX_REPLY_CHARS].rsplit("\n", 1)[0].rstrip() + "\n\n[truncated]"
    return out


def _record(conv: Conversation | None, outcome: str, reason: str = "", **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "kind": conv.kind if conv else "",
        "reply_to": conv.reply_to if conv else "",
        "path": conv.path if conv else None,
        "line": conv.line if conv else None,
        "outcome": outcome,
        "reason": reason,
        "body": "",
        "model": "",
        "input_tokens": 0,
        "output_tokens": 0,
    }
    row.update(extra)
    return row


def _failure(e: BaseException) -> str:
    return redact_for_post(f"{type(e).__name__}: {e}")


def answer_one(
    llm: Any, conv: Conversation, sources: _Sources, *, settled: bool, context_chars: int,
    template: str | None, post: Callable[[str], None] | None,
) -> dict[str, Any]:
    """Make the one call for a due conversation and post its answer unless ``post`` is None; never raises."""
    try:
        system, user = render_prompt(
            conv, sources, settled=settled, context_chars=context_chars, template=template,
        )
    except Exception as e:  # noqa: BLE001
        return _record(conv, "failed", f"prompt: {_failure(e)}")
    t0 = time.perf_counter()
    try:
        res = llm.invoke(system, user, max_tokens=MAX_TOKENS, json_mode=False, timeout_s=TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        return _record(conv, "failed", f"llm: {_failure(e)}")
    model = str(getattr(res, "model", "") or "unknown")
    tin = int(getattr(res, "input_tokens", 0) or 0)
    tout = int(getattr(res, "output_tokens", 0) or 0)
    elapsed = int(getattr(res, "elapsed_ms", 0) or (time.perf_counter() - t0) * 1000)
    usage = {"model": model, "input_tokens": tin, "output_tokens": tout}
    answer = clean_answer(getattr(res, "text", "") or "")
    if not answer:
        return _record(conv, "failed", "llm: empty reply", **usage)
    body = f"{answer}\n\n*{build_attribution(model, elapsed, tin + tout)}*"
    if post is None:
        return _record(conv, "dry-run", body=body, **usage)
    try:
        post(body)
    except Exception as e:  # noqa: BLE001
        return _record(conv, "failed", f"post: {_failure(e)}", body=body, **usage)
    return _record(conv, "answered", body=body, **usage)


def answer_replies(
    forge: Any,
    ref: PRRef,
    *,
    mode: str,
    mention: str = DEFAULT_MENTION,
    max_replies: int = DEFAULT_MAX_REPLIES,
    llm: Any,
    context_chars: int = DEFAULT_CONTEXT_CHARS,
    post: bool = True,
    template: str | None = None,
) -> list[dict[str, Any]]:
    """Run one reply sweep over ``ref`` and return one record per conversation considered.

    ``mode`` is a member of :data:`REPLY_MODES`; any other value raises
    ``ValueError``. ``off`` returns ``[]`` before any read or call. A forge
    without ``list_conversations`` logs one line and returns ``[]``; a
    listing that fails returns one ``failed`` record. Due conversations
    (:func:`classify`) are answered in listing order, one ``llm.invoke`` each
    with :data:`MAX_TOKENS` and :data:`TIMEOUT_S`, at most ``max_replies``
    calls (a failed call uses its slot; the rest are ``skipped``/``capped``).
    ``post=False`` makes the calls and posts nothing (``dry-run`` records
    carry the body that would be posted). ``template`` is the reply template
    text (:func:`prxref.prompt_templates.load_reply_template`), ``None`` for
    the packaged one.

    Each record holds ``kind``, ``reply_to``, ``path``, ``line``, ``outcome``
    (``answered``, ``dry-run``, ``skipped`` or ``failed``), ``reason``,
    ``body``, ``model``, ``input_tokens`` and ``output_tokens``. Never raises
    otherwise.
    """
    if mode not in REPLY_MODES:
        raise ValueError(f"thread replies must be one of {REPLY_MODES}, got {mode!r}")
    if mode == "off":
        return []
    listing = getattr(forge, "list_conversations", None)
    poster = getattr(forge, "post_reply", None)
    if not callable(listing) or not callable(poster):
        logger.info(
            "thread replies: the %s forge does not implement list_conversations/post_reply; nothing answered",
            getattr(forge, "name", type(forge).__name__),
        )
        return []
    try:
        conversations = list(listing(ref))
    except Exception as e:  # noqa: BLE001
        logger.warning("thread replies: conversations of %s could not be read: %s", ref.url, e)
        return [_record(None, "failed", f"list_conversations: {_failure(e)}")]
    sources = _Sources(forge, ref)
    cap = max(int(max_replies), 0)
    calls = 0
    records: list[dict[str, Any]] = []
    for conv in conversations:
        due, reason, settled = classify(conv, mode=mode, mention=mention)
        if not due:
            records.append(_record(conv, "skipped", reason))
            continue
        if calls >= cap:
            records.append(_record(conv, "skipped", "capped"))
            continue
        calls += 1

        def send(body: str, _to: str = conv.reply_to) -> None:
            poster(ref, _to, body)

        row = answer_one(
            llm, conv, sources, settled=settled, context_chars=context_chars,
            template=template, post=send if post else None,
        )
        if row["outcome"] == "failed":
            logger.warning("thread replies: %s %s failed: %s", conv.kind, conv.reply_to or "(pr)", row["reason"])
        records.append(row)
    return records
