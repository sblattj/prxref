"""Lower versioning findings on stacked PRs when the surface is not on the default branch (#146).

A PR stacked on another PR targets a branch that is not the repository's
default branch. A finding such as "moved URIs need a new API version" assumes
a released client depends on the old surface; when that surface never existed
on the default branch, no released client can. With
``PRXREF_DEFAULT_BRANCH_CHECK=on`` (the default; ``off`` opts out) the orchestrator checks the surface the
finding names and lowers the finding to the minor tier (``outofscope``, the
tier the severity caps call minor) when it is not there. It never drops a
finding.

Detection is by text, since nothing tags a versioning finding. The surface is
the finding's backticked and URL-path-like strings that the PR removed: from
the finding's own file first, else from any file of the diff (so the build
file duplicate of a controller finding is tied to the controller's route).

Two ways to check, the first that can answer wins:

1. Whole tree, when ``--repo-dir`` is a git checkout that holds the default
   branch ref (``refs/remotes/origin/<branch>`` or ``refs/heads/<branch>``):
   ``git grep -F`` over that ref and the newest :data:`MAX_TAGS` tags. A hit
   in any of them keeps the finding; none lowers it. A shallow clone without
   the ref, or no git at all, falls through to 2.
2. The forge: the finding's own file at the default branch's tip (a renamed
   file at its old path), plus each other diff file whose removed lines hold
   the surface. A route that moved to a file the PR does not touch can look
   absent here, which is why the outcome lowers rather than drops.

Everything fails open: an unknown default branch, a forge without the optional
lookup methods, a failed read or an exception leaves the finding as it was.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
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

MAX_TAGS = 3
"""Newest tags (by creation date) the git search also checks, as release tags."""

GIT_TIMEOUT = 10.0
"""Seconds each git call of the whole-tree search may take."""

REASON_FILE_ABSENT = "file absent on default branch"
REASON_SURFACE_ABSENT = "named surface absent from file on default branch"
REASON_TREE_ABSENT = "named surface absent from default branch and release tags"

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
    refs: list[str] = field(default_factory=list)

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
            "refs": list(self.refs),
        }


@dataclass
class GitSearch:
    """One whole-tree search: the refs searched and the ones holding a token."""

    refs: list[str]
    present_in: list[str]
    matched: list[str] = field(default_factory=list)
    incomplete: bool = False


_GIT_ENV_DROP = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
})


def _git_env(root: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env.update({
        "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
        "GIT_CEILING_DIRECTORIES": os.path.dirname(os.path.realpath(root)),
    })
    return env


def _git(root: str, *args: str, timeout: float) -> subprocess.CompletedProcess[str] | None:
    exe = shutil.which("git")
    if exe is None:
        return None
    try:
        return subprocess.run(
            [exe, "-C", root, "-c", "core.fsmonitor=false", *args],
            capture_output=True, text=True, timeout=timeout, check=False,
            env=_git_env(root),
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("default branch git search failed: %s", e)
        return None


def _resolve_branch(root: str, branch: str, timeout: float) -> str | None:
    for full in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
        proc = _git(root, "rev-parse", "--verify", "--quiet", f"{full}^{{commit}}", timeout=timeout)
        if proc is None:
            return None
        if proc.returncode == 0:
            return full
    return None


def _grep(root: str, full_ref: str, tokens: Sequence[str], timeout: float) -> bool | None:
    args = ["grep", "-q", "-I", "-F"]
    for t in tokens:
        args += ["-e", t]
    proc = _git(root, *args, full_ref, "--", timeout=timeout)
    if proc is None or proc.returncode not in (0, 1):
        return None
    return proc.returncode == 0


def _grep_each(root: str, full_ref: str, tokens: Sequence[str], timeout: float) -> list[str] | None:
    found: list[str] = []
    for t in tokens:
        hit = _grep(root, full_ref, [t], timeout)
        if hit is None:
            return None
        if hit:
            found.append(t)
    return found


def git_search(
    root: str, branch: str, tokens: Sequence[str], *,
    max_tags: int = MAX_TAGS, timeout: float = GIT_TIMEOUT,
) -> GitSearch | None:
    """Search ``tokens`` (fixed strings) over the default branch and the newest tags.

    ``root`` is a local git checkout. The branch resolves to
    ``refs/remotes/origin/<branch>``, else ``refs/heads/<branch>``; the newest
    ``max_tags`` tags by creation date are searched too. ``matched`` lists the
    tokens found on any ref. None (unknown) when git is missing, ``root`` is
    not the top of a checkout (a plain directory nested in another repository
    never searches that repository), the branch ref is not there
    (a shallow CI clone), ``branch`` looks like an option, or the branch grep
    fails. A tag whose grep fails, or a failed tag listing, is left out of
    ``refs`` and sets ``incomplete``: the caller then leaves a finding it
    found no hit for unchanged rather than lower it on a partial search.
    Never raises.
    """
    if not branch or branch.startswith("-") or not tokens:
        return None
    top = _git(root, "rev-parse", "--show-toplevel", timeout=timeout)
    if top is None or top.returncode != 0:
        return None
    if os.path.realpath(top.stdout.strip()) != os.path.realpath(root):
        return None
    full = _resolve_branch(root, branch, timeout)
    if full is None:
        return None
    hits = _grep_each(root, full, tokens, timeout)
    if hits is None:
        return None
    matched = list(hits)
    refs, present = [branch], [branch] if hits else []
    incomplete = False
    if max_tags > 0:
        proc = _git(
            root, "for-each-ref", "--sort=-creatordate", f"--count={max_tags}",
            "--format=%(refname)", "refs/tags", timeout=timeout,
        )
        listed = proc is not None and proc.returncode == 0
        incomplete = not listed
        names = proc.stdout.split() if listed and proc is not None else []
        for tag_ref in names:
            tag_hits = _grep_each(root, tag_ref, tokens, timeout)
            if tag_hits is None:
                incomplete = True
                continue
            short = tag_ref.removeprefix("refs/tags/")
            refs.append(short)
            if tag_hits:
                present.append(short)
                matched.extend(t for t in tag_hits if t not in matched)
    return GitSearch(refs=refs, present_in=present, matched=matched, incomplete=incomplete)


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


def _join(names: Sequence[str]) -> str:
    names = list(names)
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _tree_note(refs: Sequence[str]) -> str:
    return (
        f"Checked against {_join(refs)} (whole tree): what this finding names is not there, "
        "so no released client should depend on it. Docs that mention it may still need updating."
    )


def _note(branch: str, variant: str) -> str:
    what = {
        "A": "this file is not there",
        "B": "what this finding names is not in this file there",
        "D": "what this finding names is not in the files this PR removed it from",
    }[variant]
    return (
        f"Checked against {branch}: {what}, so no released client should depend on it. "
        "If it moved earlier in this stack, this check cannot tell. "
        "Docs that mention it may still need updating."
    )


def _append(body: str, note: str) -> str:
    return f"{body}\n\n{note}" if body else note


def _removed_by_path(files: Sequence[FileDiff]) -> dict[str, str]:
    out: dict[str, str] = {}
    for fd in files:
        key = fd.path or fd.new_path or fd.old_path
        if key:
            text = removed_text(files, key)
            if text:
                out[key] = text
    return out


def apply_default_branch_check(
    findings: Sequence[Finding],
    *,
    files: Sequence[FileDiff],
    forge: Any,
    ref: Any,
    target_branch: str,
    max_paths: int = MAX_PATHS,
    repo_root: str | None = None,
) -> tuple[list[Finding], CheckOutcome]:
    """Lower or annotate versioning findings of a stacked PR; 1:1 and order-preserving.

    Does nothing (and reads nothing) when the forge cannot name its default
    branch, or the PR already targets it. Otherwise, for each active
    ``error``/``warning`` finding that reads as a compatibility concern, the
    surface tokens are taken from its own file's removed lines, else from the
    removed lines of the whole diff. With tokens and a ``repo_root`` where
    :func:`git_search` can answer (only the URL-path-like tokens when there
    are any, so a generic name elsewhere in the tree does not count; the
    first unknown answer turns git off for the rest of the run), a hit on the
    default branch or a release tag keeps the severity and adds a "still
    present" line naming the tokens and where; no
    hit lowers it (the whole-tree note names every ref searched). Otherwise
    the forge path: its own file at the default branch (a renamed file at its
    old path) and each other diff file whose removed lines hold a token, one
    read per distinct path, at most ``max_paths`` per run. A token found in
    any file read keeps the severity, even when its own file is a 404; else
    its own file 404 lowers it (note variant A); the tokens in none of the files read lower it
    (variant B, or D when the tokens came from other files); a token found
    keeps the severity with a "still present" line; anything else (no tokens,
    an unknown read, an exception) leaves it unchanged with no note. Lowering
    sets ``outofscope`` and appends one line; it never sets ``drop_reason``.
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

    removed = _removed_by_path(files)
    all_removed = "\n".join(removed.values())
    cache: dict[str, tuple[bool, str | None] | None] = {}
    git_cache: dict[tuple[str, ...], GitSearch | None] = {}
    git_usable = True

    def read(path: str) -> tuple[bool, str | None] | None:
        if path not in cache:
            if len(cache) >= max_paths:
                cache[path] = None
            else:
                try:
                    cache[path] = get_file(ref, path, branch)
                except Exception as e:  # noqa: BLE001
                    logger.warning("default branch read of %s failed: %s", path, e)
                    cache[path] = (True, None)
        return cache[path]

    def unknown(result: tuple[bool, str | None] | None) -> bool:
        return result is None or (result[0] is True and result[1] is None)

    def lower(f: Finding, reason: str, note: str) -> Finding:
        outcome.checked += 1
        outcome.lowered += 1
        outcome.lowered_findings.append({"file": f.file, "reason": reason})
        return replace(f, severity=LOWERED_SEVERITY, body=_append(f.body, note))

    def keep(f: Finding, where: Sequence[str]) -> Finding:
        outcome.checked += 1
        outcome.present += 1
        return replace(f, body=_append(f.body, f"Checked against {_join(where)}: still present there."))

    out: list[Finding] = []
    for f in findings:
        if (
            f.drop_reason is not None
            or (f.severity or "").strip().lower() not in LOWERABLE_SEVERITIES
            or not is_compat_finding(f)
        ):
            out.append(f)
            continue
        tokens = surface_tokens(f, removed_text(files, f.file))
        own_tokens = bool(tokens)
        if not tokens:
            tokens = surface_tokens(f, all_removed)

        if tokens and repo_root and git_usable:
            paths = [t for t in tokens if _URL_PATH_RE.fullmatch(t)]
            key = tuple(paths or tokens)
            if key not in git_cache:
                git_cache[key] = git_search(repo_root, branch, list(key))
            gs = git_cache[key]
            if gs is None:
                git_usable = False
            else:
                if not outcome.refs:
                    outcome.refs = list(gs.refs)
                if gs.present_in:
                    outcome.checked += 1
                    outcome.present += 1
                    names = ", ".join(f"`{t}`" for t in gs.matched)
                    out.append(replace(f, body=_append(
                        f.body, f"Checked against {_join(gs.present_in)}: {names} still present there.",
                    )))
                elif gs.incomplete:
                    outcome.unknown += 1
                    out.append(f)
                else:
                    out.append(lower(f, REASON_TREE_ABSENT, _tree_note(gs.refs)))
                continue

        own = read(read_path(files, f.file))
        if unknown(own):
            outcome.unknown += 1
            out.append(f)
            continue
        assert own is not None
        found, text = own
        texts = [text or ""] if found else []
        some_unknown = False
        others = [
            read_path(files, p) for p, rem in removed.items()
            if p != f.file and any(t in rem for t in tokens)
        ]
        for path in dict.fromkeys(others):
            if path == read_path(files, f.file):
                continue
            res = read(path)
            if unknown(res):
                some_unknown = True
            elif res is not None and res[0]:
                texts.append(res[1] or "")
        if tokens and any(t in body for t in tokens for body in texts):
            out.append(keep(f, [branch]))
        elif some_unknown or (found and not tokens):
            outcome.unknown += 1
            out.append(f)
        elif not found:
            out.append(lower(f, REASON_FILE_ABSENT, _note(branch, "A")))
        elif own_tokens:
            out.append(lower(f, REASON_SURFACE_ABSENT, _note(branch, "B")))
        else:
            out.append(lower(f, REASON_SURFACE_ABSENT, _note(branch, "D")))
    return out, outcome
