"""Read-only forges for evaluation replays (issue #65).

A replay reviews a fixed, reproducible input instead of whatever the PR looks
like now, and it never writes to a forge. Two forges serve it:

- :class:`LocalDiffForge` serves a diff file on disk (``--diff-file`` with no
  ``--pr-url``): no network, no threads, no file reads.
- :class:`ReplayForge` wraps a real forge and pins what the orchestrator sees:
  the diff of a commit range (``base_sha``/``head_sha``, through the inner
  forge's optional ``get_compare_diff``) or a diff text, file reads and path
  listings at the pinned head, optionally no existing threads, and the PR's title and
  description as they stood at a cutoff (issue #16).

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
from datetime import datetime
from pathlib import Path
from typing import Literal

from .base import Forge, InlineComment, PathListing, PRData, PRHistory, PRRef, Thread, _require_aware

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
    ``description``, when given (``--description-file`` or
    ``--no-description``), is the PR description instead of the patch
    mail's; the title is still the mail's or the file name's.
    """

    name = "local"

    def __init__(self, diff_text: str, *, path: str, description: str | None = None):
        self._diff_text = diff_text
        self._path = path
        self._description = description

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
        path as the caller gave it. A ``description`` given to the
        constructor replaces the mail's.
        """
        title, description, author = _patch_metadata(self._diff_text, self._path)
        if self._description is not None:
            description = self._description
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
    head. ``history`` and ``cutoff`` (given together) pin the PR's title and
    description: ``get_pr`` replaces them with :func:`pin_pr_metadata`'s,
    taking the inner PR's as the live ones, so pinning adds no network call.
    ``description``, when given (``--description-file`` or
    ``--no-description``), replaces the description alone, after any pin;
    the title stays the current one. With none of the three, the title and
    description are the PR's current ones. ``description_pin`` is not read
    here: it is the caller's resolved :class:`DescriptionPin`, kept as the
    public attribute ``description_pin`` for the run's stamp. ``diff_text``,
    when given, is the diff instead of any fetched one. A blank pinned or
    ``diff_text`` diff raises ``ValueError``, which the orchestrator turns into
    an ``Error`` run. With neither, ``get_diff`` is the inner forge's own, so
    that run differs from a normal one only in its threads. ``hide_threads``
    makes ``list_threads`` return ``[]`` without asking the inner forge.

    Raises ``ValueError`` when only one of the two shas is given, when a
    pinned range must be fetched from a forge with no ``get_compare_diff``,
    when only one of ``history`` and ``cutoff`` is given, or when ``cutoff``
    is naive.
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
        history: PRHistory | None = None,
        cutoff: datetime | None = None,
        description: str | None = None,
        description_pin: DescriptionPin | None = None,
    ):
        if bool(base_sha) != bool(head_sha):
            raise ValueError("ReplayForge: base_sha and head_sha must be given together")
        if (history is None) != (cutoff is None):
            raise ValueError("ReplayForge: history and cutoff must be given together")
        if cutoff is not None:
            _require_aware(cutoff, "cutoff")
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
        self._history = history
        self._cutoff = cutoff
        self._description = description
        self.description_pin = description_pin

    @staticmethod
    def parse_pr_url(url: str) -> PRRef | None:
        """Never recognizes a URL: a replay always wraps an already-built forge."""
        return None

    def get_pr(self, ref: PRRef) -> PRData:
        """The inner PR, with its shas, title and description replaced as the replay pins them.

        The shas become the pinned range when one is set; the title and
        description become the ones in force at ``cutoff`` when a history is
        set; and the description becomes the fixed ``description`` when one
        is set.
        """
        pr = self._inner.get_pr(ref)
        if self._head_sha:
            pr = dataclasses.replace(
                pr, source_sha=self._head_sha, target_sha=self._base_sha,
            )
        if self._history is not None and self._cutoff is not None:
            pinned = pin_pr_metadata(
                self._history, live_title=pr.title, live_description=pr.description,
                cutoff=self._cutoff,
            )
            pr = dataclasses.replace(pr, title=pinned.title, description=pinned.description)
        if self._description is not None:
            pr = dataclasses.replace(pr, description=self._description)
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

    def list_paths(self, ref: PRRef, *, sha: str) -> PathListing | None:
        """The inner forge's path listing at ``sha``; ``None`` when it has none or it raises."""
        lister = getattr(self._inner, "list_paths", None)
        if lister is None:
            return None
        try:
            return lister(ref, sha=sha)
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


PinStatus = Literal["pinned", "live"]
CutoffSource = Literal["flag", "first-review", "head-commit"]


@dataclasses.dataclass(frozen=True)
class PinnedMetadata:
    """The title and description a replay shows, and whether they were pinned.

    ``status`` is ``"pinned"`` when both are the ones in force at the cutoff,
    and ``"live"`` when both are the PR's current ones because the history
    could not answer. The title never has a status of its own: it is pinned
    exactly when the description is.
    """

    title: str
    description: str
    status: PinStatus


DescriptionStatus = Literal["pinned", "live", "file", "none"]


@dataclasses.dataclass(frozen=True)
class DescriptionPin:
    """Which title and description a replay shows, and the cutoff it was pinned to.

    ``status`` is ``"pinned"`` when the title and description are the ones in
    force at the cutoff, ``"live"`` when they are the PR's current ones,
    ``"file"`` when the description is the ``--description-file`` text and
    ``"none"`` under ``--no-description``; under ``"file"`` and ``"none"``
    the title is the PR's current one. ``as_of`` is the cutoff and
    ``as_of_source`` where it came from (``"flag"`` for ``--as-of``, else
    ``"first-review"`` or ``"head-commit"``). Both are set whenever a cutoff
    was chosen, including a ``"live"`` pin whose history did not reach it,
    and both are ``None`` otherwise.
    """

    status: DescriptionStatus
    as_of: datetime | None
    as_of_source: CutoffSource | None


def _resolve_pin(history: PRHistory, cutoff: datetime) -> tuple[PinStatus, str | None]:
    """Return the pin status at ``cutoff`` and the pinned description text.

    The text is ``None`` when the status is ``"live"``, and when a complete
    history holds no versions: the description was never edited, so the
    PR's live description is the one in force. Raises ``ValueError`` when
    ``cutoff`` is naive.
    """
    _require_aware(cutoff, "cutoff")
    versions = sorted(history.description_versions, key=lambda version: version.edited_at)
    if not versions:
        return ("pinned" if history.complete else "live"), None
    reached = [version for version in versions if version.edited_at <= cutoff]
    if reached:
        in_force = reached[-1]
    elif history.complete:
        in_force = versions[0]
    else:
        return "live", None
    if in_force.text is None:
        return "live", None
    return "pinned", in_force.text


def pin_status(history: PRHistory, cutoff: datetime) -> PinStatus:
    """Return whether ``history`` pins the description at ``cutoff``, without the texts.

    The status is exactly the one :func:`pin_pr_metadata` returns for the same
    history and cutoff, which depends on neither the live title nor the live
    description, so a caller can decide before the PR itself is read.

    Raises ``ValueError`` when ``cutoff`` is naive.
    """
    return _resolve_pin(history, cutoff)[0]


def pin_pr_metadata(
    history: PRHistory, *, live_title: str, live_description: str, cutoff: datetime,
) -> PinnedMetadata:
    """Return the title and description in force at ``cutoff``, or the live ones.

    The description in force is the last version, ordered by ``edited_at``
    and never by position, whose ``edited_at`` is at or before the cutoff.
    A complete history with no such version gives its oldest, which is the
    original, so a cutoff before ``history.created_at`` clamps to the
    original. With no versions at all, a complete history was never edited,
    so ``live_description`` is the original and is ``pinned``.
    The result is ``live``, with ``live_title`` and ``live_description``,
    when the version in force has no text (deleted), or when an incomplete
    history holds no version at or before the cutoff and so does not reach
    it. When the description is pinned, the title is the ``previous_title``
    of the first rename, ordered by ``created_at``, made strictly after the
    cutoff, else ``live_title``. The status is the one :func:`pin_status`
    gives.

    Raises ``ValueError`` when ``cutoff`` is naive.
    """
    status, text = _resolve_pin(history, cutoff)
    if status == "live":
        return PinnedMetadata(title=live_title, description=live_description, status="live")
    description = live_description if text is None else text
    renames = sorted(history.title_renames, key=lambda rename: rename.created_at)
    title = next((rename.previous_title for rename in renames if rename.created_at > cutoff), live_title)
    return PinnedMetadata(title=title, description=description, status="pinned")


def choose_cutoff(as_of: datetime | None, history: PRHistory | None) -> tuple[datetime, CutoffSource] | None:
    """Return the replay cutoff and where it came from, or ``None`` when nothing gives one.

    The ladder is ``as_of`` (``"flag"``, from ``--as-of``), else the
    history's ``first_review_at`` (``"first-review"``), else its
    ``head_committed_at`` (``"head-commit"``). ``history`` is ``None`` for a
    forge with no ``get_pr_history``, which leaves only the flag. The cutoff
    is returned as given, unclamped; ``pin_pr_metadata`` clamps it.

    Raises ``ValueError`` when ``as_of`` is naive.
    """
    if as_of is not None:
        _require_aware(as_of, "as_of")
        return as_of, "flag"
    if history is None:
        return None
    if history.first_review_at is not None:
        return history.first_review_at, "first-review"
    if history.head_committed_at is not None:
        return history.head_committed_at, "head-commit"
    return None
