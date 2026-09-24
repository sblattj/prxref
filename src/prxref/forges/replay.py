"""Read-only forges for evaluation replays (issue #65).

A replay reviews a fixed, reproducible input instead of whatever the PR looks
like now, and it never writes to a forge. Two forges serve it:

- :class:`LocalDiffForge` serves a diff file on disk (``--diff-file`` with no
  ``--pr-url``): no network, no threads, no file reads.
- :class:`ReplayForge` wraps a real forge and pins what the orchestrator sees:
  the diff of a commit range (``base_sha``/``head_sha``, through the inner
  forge's optional ``get_compare_diff``) or a diff text, file reads at the
  pinned head, and optionally no existing threads.

Both raise on every write method, as defence in depth: the CLI already forces
``post=False`` on a replay. Neither is registered in ``detect_forge`` or
``make_forge``, because neither is ever produced from a URL, and neither
implements ``get_compare_diff``: pinning is resolved against the inner forge.
"""
from __future__ import annotations

import dataclasses
import email
import email.message
import email.policy
import email.utils
import re
from collections.abc import Sequence
from pathlib import Path

from .base import Forge, InlineComment, PRData, PRRef, Thread

NEVER_POSTS = "replay runs never write to a forge"

_DIFF_START_RE = re.compile(r"^diff --git ", re.MULTILINE)
_PLAIN_DIFF_START_RE = re.compile(r"^--- ", re.MULTILINE)
_SUBJECT_HEADER_RE = re.compile(r"^Subject:", re.MULTILINE | re.IGNORECASE)
_PATCH_PREFIX_RE = re.compile(r"^\s*\[[^\]]*\bPATCH\b[^\]]*\]\s*", re.IGNORECASE)
_ENCODED_TRANSFERS = ("quoted-printable", "base64")


def _patch_metadata(text: str, path: str) -> tuple[str, str, str]:
    """Return ``(title, description, author)`` for a diff file's text.

    A ``git format-patch`` mail (the preamble before the first ``diff --git``
    line starts with ``From `` and has a ``Subject:`` header) gives its
    subject without the ``[PATCH …]`` tag (``[RFC PATCH v2 1/3]`` included;
    a bracket without ``PATCH`` is part of the title), its body up to git's ``---``
    diffstat separator, and the author's display name (else the address).
    Headers are unfolded and RFC 2047-decoded by ``email.policy.default``.
    A series yields the metadata of its first patch. Anything else is a plain
    diff, titled ``Local diff <file name>`` with no description or author;
    so is a mail that cannot be parsed, because metadata is never worth a
    failed review.
    """
    plain = (f"Local diff {Path(path).name}", "", "")
    start = _DIFF_START_RE.search(text) or _PLAIN_DIFF_START_RE.search(text)
    preamble = text[: start.start()] if start else text
    if not preamble.startswith("From ") or not _SUBJECT_HEADER_RE.search(preamble):
        return plain
    rest = preamble.split("\n", 1)[1] if "\n" in preamble else ""
    try:
        msg = email.message_from_string(rest, policy=email.policy.default)
        title = _PATCH_PREFIX_RE.sub("", str(msg.get("Subject", ""))).strip()
        name, address = email.utils.parseaddr(str(msg.get("From", "")))
        body = _mail_body(msg)
    except Exception:  # noqa: BLE001 - metadata is never worth a failed review
        return plain
    kept: list[str] = []
    for line in body.split("\n"):
        if line.rstrip("\r") == "---":
            break
        kept.append(line)
    return title or plain[0], "\n".join(kept).strip(), name or address


def _mail_body(msg: email.message.Message) -> str:
    """The text body of a parsed patch mail, decoded, or ``""`` if multipart.

    ``get_content()`` is not used: on a message parsed from ``str`` it
    re-decodes an 8bit body and mangles every non-ASCII character, while
    ``get_payload()`` returns it as written. Only a quoted-printable or base64
    body needs decoding, with the declared charset.
    """
    cte = str(msg.get("Content-Transfer-Encoding", "")).strip().lower()
    if cte in _ENCODED_TRANSFERS:
        raw = msg.get_payload(decode=True) or b""
        return raw.decode(msg.get_content_charset() or "utf-8", errors="replace")
    payload = msg.get_payload()
    return payload if isinstance(payload, str) else ""


class LocalDiffForge:
    """A read-only Forge over a diff file: no network, no threads, no file reads, never posts.

    It deliberately has no ``get_file_content``, and its PR has no head sha,
    so the orchestrator skips context injection. ``get_diff`` raises on a
    blank diff, which the orchestrator turns into an ``Error`` run: an empty
    replay input almost always means the wrong file, never a clean PR.
    """

    name = "local"

    def __init__(self, diff_text: str, *, path: str):
        self._diff_text = diff_text
        self._path = path

    @staticmethod
    def parse_pr_url(url: str) -> PRRef | None:
        """Never recognizes a URL: a local diff is never produced from one."""
        return None

    @staticmethod
    def ref_for(path: str) -> PRRef:
        """The synthetic ``PRRef`` of a diff-only run: forge ``"local"``, the file's URI."""
        return PRRef(
            forge="local", host="", owner="", repo="", number=0,
            url=Path(path).resolve().as_uri(),
        )

    def get_pr(self, ref: PRRef) -> PRData:
        """PR metadata from the patch mail headers, or a title from the file name.

        Both shas are empty and ``raw`` is ``{"diff_file": path}``, with the
        path as the caller gave it.
        """
        title, description, author = _patch_metadata(self._diff_text, self._path)
        return PRData(
            title=title, description=description, author=author,
            source_branch="", target_branch="", source_sha="", target_sha="",
            raw={"diff_file": self._path},
        )

    def get_diff(self, ref: PRRef) -> str:
        """The diff text, unmodified; raises ``ValueError`` when it is blank."""
        if not self._diff_text.strip():
            raise ValueError("replay diff from the --diff-file is empty")
        return self._diff_text

    def list_threads(self, ref: PRRef) -> list[Thread]:
        """Always empty: a diff file has no discussion."""
        return []

    def post_summary(self, ref: PRRef, body: str) -> None:
        """Always raises ``RuntimeError``: a replay never writes to a forge."""
        raise RuntimeError(NEVER_POSTS)

    def post_inline_comments(self, ref: PRRef, comments: Sequence[InlineComment]) -> int:
        """Always raises ``RuntimeError``: a replay never writes to a forge."""
        raise RuntimeError(NEVER_POSTS)

    def prune_inline_comments(self, *args: object, **kwargs: object) -> int:
        """Always raises ``RuntimeError``: a replay never writes to a forge."""
        raise RuntimeError(NEVER_POSTS)


class ReplayForge:
    """Wrap a Forge so a review sees one pinned, reproducible input and can never post.

    ``base_sha``/``head_sha`` (full, lowercased, given together) pin the
    range: ``get_diff`` returns the inner forge's ``get_compare_diff`` of it,
    and ``get_pr`` reports ``head_sha``/``base_sha`` as the PR's
    ``source_sha``/``target_sha``, so every file read happens at the pinned
    head. The PR's title and description stay the current ones. ``diff_text``,
    when given, is the diff instead of any fetched one. A blank pinned or
    ``diff_text`` diff raises ``ValueError``, which the orchestrator turns into
    an ``Error`` run. With neither, ``get_diff`` is the inner forge's own, so
    that run differs from a normal one only in its threads. ``hide_threads``
    makes ``list_threads`` return ``[]`` without asking the inner forge.

    Raises ``ValueError`` when only one of the two shas is given, or when a
    pinned range must be fetched from a forge with no ``get_compare_diff``.
    """

    name = "replay"

    def __init__(
        self,
        inner: Forge,
        *,
        base_sha: str | None = None,
        head_sha: str | None = None,
        hide_threads: bool = False,
        diff_text: str | None = None,
    ):
        if bool(base_sha) != bool(head_sha):
            raise ValueError("ReplayForge: base_sha and head_sha must be given together")
        if head_sha and diff_text is None and getattr(inner, "get_compare_diff", None) is None:
            forge_name = getattr(inner, "name", type(inner).__name__)
            raise ValueError(
                f"ReplayForge: the {forge_name} forge cannot fetch a pinned commit range"
            )
        self._inner = inner
        self._base_sha = base_sha or None
        self._head_sha = head_sha or None
        self._hide_threads = hide_threads
        self._diff_text = diff_text

    @staticmethod
    def parse_pr_url(url: str) -> PRRef | None:
        """Never recognizes a URL: a replay always wraps an already-built forge."""
        return None

    def get_pr(self, ref: PRRef) -> PRData:
        """The inner PR, with its shas replaced by the pinned range when one is set."""
        pr = self._inner.get_pr(ref)
        if self._head_sha:
            pr = dataclasses.replace(
                pr, source_sha=self._head_sha, target_sha=self._base_sha,
            )
        return pr

    def get_diff(self, ref: PRRef) -> str:
        """The replay's diff: ``diff_text``, else the pinned range, else the live PR diff."""
        if self._diff_text is not None:
            if not self._diff_text.strip():
                raise ValueError("replay diff from the --diff-file is empty")
            return self._diff_text
        if self._head_sha:
            text = self._inner.get_compare_diff(
                ref, base_sha=self._base_sha, head_sha=self._head_sha,
            )
            if not text.strip():
                raise ValueError(
                    f"replay diff from {self._base_sha[:12]}...{self._head_sha[:12]} "
                    "is empty (is the head already contained in the base?)"
                )
            return text
        return self._inner.get_diff(ref)

    def list_threads(self, ref: PRRef) -> list[Thread]:
        """``[]`` when threads are hidden (the inner forge is not asked), else the inner's."""
        if self._hide_threads:
            return []
        return self._inner.list_threads(ref)

    def get_file_content(self, ref: PRRef, path: str, *, sha: str) -> str | None:
        """The inner forge's file read; ``None`` when it has none or it raises."""
        reader = getattr(self._inner, "get_file_content", None)
        if reader is None:
            return None
        try:
            return reader(ref, path, sha=sha)
        except Exception:  # noqa: BLE001 - the Protocol says this never raises
            return None

    def post_summary(self, ref: PRRef, body: str) -> None:
        """Always raises ``RuntimeError``: a replay never writes to a forge."""
        raise RuntimeError(NEVER_POSTS)

    def post_inline_comments(self, ref: PRRef, comments: Sequence[InlineComment]) -> int:
        """Always raises ``RuntimeError``: a replay never writes to a forge."""
        raise RuntimeError(NEVER_POSTS)

    def prune_inline_comments(self, *args: object, **kwargs: object) -> int:
        """Always raises ``RuntimeError``: a replay never writes to a forge."""
        raise RuntimeError(NEVER_POSTS)
