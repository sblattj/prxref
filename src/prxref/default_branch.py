"""Lower versioning findings on stacked PRs when the surface is not on the default branch (#146).

A PR stacked on another PR targets a branch that is not the repository's
default branch. A finding such as "moved URIs need a new API version" assumes
a released client depends on the old surface; when that surface never existed
on the default branch, no released client can. With
``PRXREF_DEFAULT_BRANCH_CHECK=on`` the orchestrator reads the finding's own file
at the default branch's tip and lowers the finding to the minor tier
(``outofscope``, the tier the severity caps call minor) when the file or the
named surface is not there. It never drops a finding.

Detection is by text, since nothing tags a versioning finding. Only the
finding's own file is read: there is no repo-wide search over forge APIs and no
release-tag check. A route that moved files can look absent, which is why the
outcome lowers a finding rather than dropping it.

Everything fails open: an unknown default branch, a forge without the optional
lookup methods, a failed read or an exception leaves the finding as it was.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .triage import FileDiff, Finding

logger = logging.getLogger(__name__)

DEFAULT_BRANCH_CHECK_MODES = ("off", "on")
"""The legal values of ``PRXREF_DEFAULT_BRANCH_CHECK``."""

MAX_PATHS = 20
"""Most distinct files one run reads from the default branch."""

LOWERED_SEVERITY = "outofscope"
"""The minor tier: a lowered finding takes this severity."""

LOWERABLE_SEVERITIES = frozenset({"error", "warning"})
"""Only these severities are candidates; ``spec`` and ``outofscope`` are left alone."""

REASON_FILE_ABSENT = "file absent on default branch"
REASON_SURFACE_ABSENT = "named surface absent from file on default branch"

_COMPAT_PATTERNS = (
    r"\bbreaking[\s-]+changes?\b",
    r"\bbackwards?[\s-]+compat\w*",
    r"\b(?:new|bump(?:ed|ing|s)?)\s+(?:(?:the|an?)\s+)?(?:api\s+)?version\b",
    r"\bversioned\s+(?:route|endpoint|path|uri|url|api)s?\b",
    r"\b(?:remov|renam|mov)(?:e|es|ed|ing)\s+(?:(?:the|an?|public|existing)\s+)*"
    r"(?:\S+\s+){0,2}(?:route|endpoint|uri|url|public\s+api)s?\b",
    r"\b(?:route|endpoint|uri|url)s?\s+(?:is|are|was|were|gets?|got)\s+"
    r"(?:removed|renamed|moved)\b",
    r"\bpublic\s+api\b.*\b(?:remov|renam|chang)\w*",
    r"\bkeep\w*\b.{0,40}\bor\s+version\w*",
)
COMPAT_RE = re.compile("|".join(f"(?:{p})" for p in _COMPAT_PATTERNS), re.IGNORECASE | re.DOTALL)
"""Matches finding text that talks about compatibility or versioning of a public surface."""

_BACKTICK_RE = re.compile(r"`([^`\n]{3,80})`")
_URL_PATH_RE = re.compile(r"(?<![\w/.:-])/[A-Za-z0-9_{}][\w.{}:~-]*(?:/[\w.{}:~-]+)*")


@dataclass
class CheckOutcome:
    """What one default-branch check did, for the run record and the trace."""

    branch: str | None = None
    stacked: bool = False
    checked: int = 0
    lowered: int = 0
    present: int = 0
    unknown: int = 0
    lowered_findings: list[dict[str, str]] = field(default_factory=list)

    def record(self) -> dict[str, Any]:
        """The run record's ``default_branch_check`` value."""
        return {
            "branch": self.branch,
            "stacked": self.stacked,
            "checked": self.checked,
            "lowered": self.lowered,
            "present": self.present,
            "unknown": self.unknown,
            "lowered_findings": [dict(row) for row in self.lowered_findings],
        }


def is_compat_finding(finding: Finding) -> bool:
    """True when the finding's title or body reads as a compatibility or versioning concern."""
    return bool(COMPAT_RE.search(f"{finding.title}\n{finding.body}"))


def removed_text(files: Sequence[FileDiff], path: str) -> str:
    """The removed (``-``) lines of ``path`` in the parsed diff, joined by newlines."""
    out: list[str] = []
    for fd in files:
        if path not in (fd.path, fd.new_path, fd.old_path):
            continue
        out.extend(ln.text for h in fd.hunks for ln in h.lines if ln.kind == "-")
    return "\n".join(out)


def read_path(files: Sequence[FileDiff], path: str) -> str:
    """The path to read ``path`` at on the default branch: the old path of a renamed file.

    A file the PR renamed does not exist at its new path on the default branch,
    so the read goes to the path it had before. Any other file reads at its own.
    """
    for fd in files:
        if path in (fd.path, fd.new_path) and fd.old_path and fd.old_path != path:
            return fd.old_path
    return path


def surface_tokens(finding: Finding, removed: str) -> list[str]:
    """Backticked and URL-path-like strings of the finding that occur in ``removed``.

    Order of first appearance, no duplicates. A token must also occur in the
    removed lines of the PR diff for the finding's own file, so only a surface
    the PR itself took away is ever checked.
    """
    text = f"{finding.title}\n{finding.body}"
    found: list[str] = []
    for match in [*_BACKTICK_RE.finditer(text), *_URL_PATH_RE.finditer(text)]:
        token = match.group(1) if match.re is _BACKTICK_RE else match.group(0)
        token = token.strip()
        if len(token) < 3 or token in found or token not in removed:
            continue
        found.append(token)
    return found


def _note(branch: str, variant: str) -> str:
    what = "this file is not there" if variant == "A" else (
        "what this finding names is not in this file there"
    )
    return (
        f"Checked against {branch}: {what}, so no released client should depend on it. "
        "If it moved earlier in this stack, this check cannot tell. "
        "Docs that mention it may still need updating."
    )


def _append(body: str, note: str) -> str:
    return f"{body}\n\n{note}" if body else note


def apply_default_branch_check(
    findings: Sequence[Finding],
    *,
    files: Sequence[FileDiff],
    forge: Any,
    ref: Any,
    target_branch: str,
    max_paths: int = MAX_PATHS,
) -> tuple[list[Finding], CheckOutcome]:
    """Lower or annotate versioning findings of a stacked PR; 1:1 and order-preserving.

    Does nothing (and reads nothing) when the forge cannot name its default
    branch, or the PR already targets it. Otherwise, for each active
    ``error``/``warning`` finding that reads as a compatibility concern, reads
    its file at the default branch (a renamed file at its old path; one read per distinct path, at most
    ``max_paths`` paths per run): a 404 lowers it (note variant A); a file
    that holds none of the finding's surface tokens lowers it (variant B); a
    file that holds one keeps the severity and adds a "still present" line;
    anything else (no surface tokens, an unknown read, an exception) leaves it
    unchanged with no note. Lowering sets ``outofscope`` and appends one line;
    it never sets ``drop_reason``.
    """
    outcome = CheckOutcome()
    get_default = getattr(forge, "get_default_branch", None)
    get_file = getattr(forge, "get_file_at_branch", None)
    if not callable(get_default) or not callable(get_file):
        return list(findings), outcome
    try:
        branch = get_default(ref)
    except Exception as e:  # noqa: BLE001
        logger.warning("default branch lookup failed: %s", e)
        return list(findings), outcome
    if not isinstance(branch, str) or not branch:
        return list(findings), outcome
    outcome.branch = branch
    if target_branch == branch:
        return list(findings), outcome
    outcome.stacked = True

    cache: dict[str, tuple[bool, str | None] | None] = {}
    out: list[Finding] = []
    for f in findings:
        if (
            f.drop_reason is not None
            or (f.severity or "").strip().lower() not in LOWERABLE_SEVERITIES
            or not is_compat_finding(f)
        ):
            out.append(f)
            continue
        path = read_path(files, f.file)
        if path not in cache:
            if len(cache) >= max_paths:
                cache[path] = None
            else:
                try:
                    cache[path] = get_file(ref, path, branch)
                except Exception as e:  # noqa: BLE001
                    logger.warning("default branch read of %s failed: %s", path, e)
                    cache[path] = (True, None)
        result = cache[path]
        if result is None or (result[0] is True and result[1] is None):
            outcome.unknown += 1
            out.append(f)
            continue
        found, text = result
        if not found:
            outcome.checked += 1
            outcome.lowered += 1
            outcome.lowered_findings.append({"file": f.file, "reason": REASON_FILE_ABSENT})
            out.append(replace(
                f, severity=LOWERED_SEVERITY, body=_append(f.body, _note(branch, "A")),
            ))
            continue
        tokens = surface_tokens(f, removed_text(files, f.file))
        if not tokens:
            outcome.unknown += 1
            out.append(f)
            continue
        outcome.checked += 1
        if any(t in (text or "") for t in tokens):
            outcome.present += 1
            out.append(replace(
                f, body=_append(f.body, f"Checked against {branch}: still present there."),
            ))
        else:
            outcome.lowered += 1
            outcome.lowered_findings.append({"file": f.file, "reason": REASON_SURFACE_ABSENT})
            out.append(replace(
                f, severity=LOWERED_SEVERITY, body=_append(f.body, _note(branch, "B")),
            ))
    return out, outcome
