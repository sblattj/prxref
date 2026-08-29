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
