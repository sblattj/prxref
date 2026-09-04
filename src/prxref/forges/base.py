"""Forge contract: one Protocol, four implementations.

The implementations are bitbucket (Cloud), bitbucket_server (Server / Data
Center), github (Cloud and Enterprise Server) and gitlab (SaaS and self-hosted).

Every value that flows through the pipeline is forge-agnostic past this module.
Diff handling is deliberately unified: each forge returns ONE raw unified diff
string; parsing/chunking happens downstream in triage.py, identically for every
forge.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PRRef:
    """A pull/merge request identity, normalized across forges."""

    forge: str  # "bitbucket" | "bitbucket-server" | "github" | "gitlab"
    host: str  # e.g. "bitbucket.org", "github.com", "gitlab.com", or self-hosted host
    owner: str  # workspace / org / group
    repo: str
    number: int
    url: str


@dataclass
class InlineComment:
    """A comment anchored to one line of the new file."""

    path: str
    line: int  # line in the NEW file
    body: str
    side: str = "RIGHT"


@dataclass
class Thread:
    """An existing discussion thread on a PR (for dedup against re-review)."""

    path: str | None
    line: int | None
    resolved: bool
    author: str
    body_snippet: str


@dataclass
class PRData:
    """Normalized PR metadata returned by get_pr()."""

    title: str
    description: str
    author: str
    source_branch: str
    target_branch: str
    source_sha: str
    target_sha: str
    raw: dict  # forge-native payload, for forge-specific needs


SUMMARY_MARKER = "<!-- prxref-summary -->"

# The attribution prefix every posted comment carries (the full line is
# ``Reviewed by prxref · model=… · N tok · Xs``). The prune pass matches on
# this prefix to recognise its own comments, so the constant lives beside the
# summary marker it keeps company with: both are the tool's own fingerprints
# on a forge's comment feed.
ATTRIBUTION_MARKER = "Reviewed by prxref"


class FeedReadError(RuntimeError):
    """A PR's comment/activity feed could not be read all the way to the end.

    ``post_summary`` finds its own previous summary by scanning that feed for
    ``SUMMARY_MARKER``; finding it is the only thing standing between a
    re-review and a SECOND summary comment on someone's PR. A scan that did
    not finish — a transport failure, a non-OK response, a page budget spent
    before the feed ran out — therefore cannot be read as "no summary exists".
    Every adapter raises this instead of falling through to the POST, so the
    caller sees a review that failed to post (recoverable: re-run it) rather
    than a duplicate comment (not recoverable without a human deleting it).

    ``list_threads`` makes the opposite trade and never raises it: its output
    only feeds best-effort dedup, and the orchestrator already substitutes an
    empty list for any exception, so returning the pages that WERE read beats
    throwing them away. It logs a warning instead, so the under-read is
    visible rather than silent.
    """


def with_summary_marker(body: str) -> str:
    """Return ``body`` guaranteed to carry ``SUMMARY_MARKER``.

    The marker is what a later run matches on, so the adapter that looks for
    it is also the one that has to put it there — nothing upstream of the
    forge layer adds it, and a summary posted without it is invisible to the
    next run's lookup, which then posts a duplicate. Idempotent: a body that
    already carries the marker (from a template, or from a caller that stamps
    its own) is returned untouched.
    """
    if SUMMARY_MARKER in body:
        return body
    return f"{SUMMARY_MARKER}\n{body}"


class Forge(Protocol):
    """The contract every forge adapter implements."""

    name: str

    @staticmethod
    def parse_pr_url(url: str) -> PRRef | None:
        """Return a PRRef if this forge recognizes the URL, else None."""
        ...

    def get_pr(self, ref: PRRef) -> PRData:
        """Fetch normalized PR metadata."""
        ...

    def get_diff(self, ref: PRRef) -> str:
        """Fetch the raw unified diff of the PR (all files)."""
        ...

    def post_summary(self, ref: PRRef, body: str) -> None:
        """Post (or update) the top-level review summary comment."""
        ...

    def post_inline_comments(self, ref: PRRef, comments: Sequence[InlineComment]) -> int:
        """Post inline comments; returns the number actually posted."""
        ...

    def list_threads(self, ref: PRRef) -> list[Thread]:
        """List existing threads so re-reviews skip already-discussed findings."""
        ...

    def get_file_content(self, ref: PRRef, path: str, *, sha: str) -> str | None:
        """Return the text of ``path`` at commit ``sha``.

        Optional: callers resolve it with ``getattr(forge, "get_file_content",
        None)``, so a Forge without it is still valid. Return ``None`` when
        the file is missing, binary, too large, or cannot be fetched for any
        other reason — this method never raises.
        """
        ...


def detect_forge(url: str) -> PRRef | None:
    """Try each registered forge's URL parser in order.

    Bitbucket Cloud is listed before Bitbucket Server so that the more specific
    parser gets first refusal: Cloud pins itself to bitbucket.org and to a bare
    ``owner/repo/pull-requests/N`` path, while Server matches any host and
    requires a ``/projects|users/KEY/repos/REPO/`` prefix. As written the two
    patterns are disjoint — no URL matches both, so the order does not change
    any result today. It is kept deliberately anyway: whichever parser is
    narrower should be asked first, so that loosening one later degrades into a
    shadowed forge rather than a silently mis-routed one.
    """
    from . import bitbucket, bitbucket_server, github, gitlab

    for forge in (bitbucket, bitbucket_server, github, gitlab):
        ref = forge.ForgeImpl.parse_pr_url(url)
        if ref is not None:
            return ref
    return None
