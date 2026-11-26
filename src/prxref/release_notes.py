"""Release notes from merged pull requests (#45).

Two entry points share this module. ``prxref release-notes`` reads the
commits between two revisions of a local checkout with ``git log``, finds
the pull request each one merged, reads prxref's summary comment on it, and
prints one markdown document grouped by conventional-commit category. The
webhook daemon's post-merge hook (``PRXREF_RELEASE_NOTES_FILE``) appends one
merged pull request at a time to an ``Unreleased`` draft file on disk. Both
are deterministic: no LLM call is made, and nothing is posted to the forge.

The output is plain markdown with no prxref markers, so a maintainer edits
it like any other file before committing it. See docs/release-notes.md.

A pull request number is taken only from these anchored commit shapes
(:func:`pr_number`):

* squash merge, the subject ending in ``(#N)`` (GitHub, Gitea/Forgejo);
* ``Merge pull request #N ...`` (GitHub merge commit, Bitbucket Server);
* ``Merge pull request 'title' (#N) from ...`` (Gitea/Forgejo merge commit);
* a ``See merge request group/repo!N`` body line (GitLab);
* ``Merged in branch (pull request #N)`` (Bitbucket Cloud);
* ``Merged PR N: title`` (Azure DevOps).

A bare ``#N`` elsewhere (``Fixes #12``) is never read as a pull request, and
a commit with none of these shapes is left out and counted.
"""
from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .forges.base import ATTRIBUTION_MARKER, PRRef, detect_forge
from .llm import ConfigError

logger = logging.getLogger(__name__)

#: Seconds one ``git`` call may take before the run gives up on it.
GIT_TIMEOUT_SECONDS = 60

#: Longest executive-summary excerpt under a bullet, in characters.
EXCERPT_MAX_CHARS = 240

#: Heading of the draft file the daemon hook appends to.
DRAFT_TITLE = "# Unreleased"

#: ``(key, heading)`` per category, in output order. ``breaking`` collects
#: any type marked ``!`` or carrying a ``BREAKING CHANGE:`` footer;
#: ``other`` collects every subject without a known type.
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("breaking", "Breaking changes"),
    ("feat", "Features"),
    ("fix", "Fixes"),
    ("perf", "Performance"),
    ("revert", "Reverts"),
    ("refactor", "Refactoring"),
    ("docs", "Documentation"),
    ("test", "Tests"),
    ("build", "Build"),
    ("ci", "CI"),
    ("chore", "Chores"),
    ("other", "Other"),
)
_HEADINGS = dict(CATEGORIES)
_TYPES = frozenset(key for key, _ in CATEGORIES) - {"breaking", "other"}

_CONVENTIONAL_RE = re.compile(r"^(?P<type>[A-Za-z]+)(?:\([^)]*\))?(?P<bang>!)?:\s*(?P<rest>\S.*)$")
_BREAKING_FOOTER_RE = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
_TRAILING_REF_RE = re.compile(r"\s*\(#\d+\)\s*$")

_SQUASH_RE = re.compile(r"\(#(?P<n>\d+)\)\s*$")
_GITHUB_MERGE_RE = re.compile(r"^Merge pull request #(?P<n>\d+)\b")
_GITEA_MERGE_RE = re.compile(r"^Merge pull request '(?P<title>.+)' \(#(?P<n>\d+)\) from ")
_GITLAB_BODY_RE = re.compile(r"^See merge request \S+!(?P<n>\d+)\s*$", re.MULTILINE)
_BITBUCKET_MERGE_RE = re.compile(r"^Merged in \S+ \(pull request #(?P<n>\d+)\)")
_AZURE_MERGE_RE = re.compile(r"^Merged PR (?P<n>\d+): (?P<title>\S.*)$")

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_VERDICT_HEADING_RE = re.compile(r"^#{1,6}\s*prxref automated review:\s*(?P<verdict>.+?)\s*$")
_PR_LINE_RE = re.compile(r"^PR: .* · files reviewed: \d+\s*$")


@dataclass(frozen=True)
class Commit:
    """One commit as ``git log`` reports it."""

    sha: str
    subject: str
    body: str


@dataclass(frozen=True)
class PullRequest:
    """One merged pull request found in the commit range.

    ``title`` is the text the category is read from: the squash commit's
    subject, or for a merge commit the pull request title it carries.
    """

    number: int
    title: str
    sha: str
    merge_form: bool


@dataclass(frozen=True)
class Entry:
    """One release-notes bullet."""

    number: int
    url: str
    category: str
    text: str
    excerpt: str


@dataclass
class Notes:
    """A rendered run: the entries plus what the run had to fall back on."""

    entries: list[Entry] = field(default_factory=list)
    summaries_found: int = 0
    fallbacks: list[tuple[int, str]] = field(default_factory=list)
    commits_without_pr: int = 0


def _git(repo: str, args: Sequence[str]) -> subprocess.CompletedProcess:
    """Run ``git -C repo <args>`` with a timeout; argument list only, never a shell."""
    try:
        return subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ConfigError("--repo: git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConfigError(f"--repo: git {args[0]} took longer than {GIT_TIMEOUT_SECONDS}s") from exc


def _resolve_rev(repo: str, rev: str, flag: str) -> str:
    """Return the commit sha ``rev`` names, or raise ``ConfigError`` naming ``flag``.

    A revision that starts with ``-`` is refused before git sees it, so it
    cannot be read as a git option.
    """
    if not rev or rev.startswith("-"):
        raise ConfigError(f"{flag}: {rev!r} is not a revision")
    done = _git(repo, ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}"])
    sha = done.stdout.strip()
    if done.returncode != 0 or not sha:
        raise ConfigError(f"{flag}: unknown tag or revision {rev!r} in {repo}")
    return sha


def read_commits(repo: str, since: str, until: str = "HEAD") -> list[Commit]:
    """Return the commits in ``since..until`` of the checkout at ``repo``, oldest first.

    Raises ``ConfigError`` naming ``--repo`` when ``repo`` is not a git
    checkout, and naming ``--since`` or ``--until`` when that revision is
    unknown.
    """
    if not Path(repo).is_dir():
        raise ConfigError(f"--repo: {repo!r} is not a directory")
    inside = _git(repo, ["rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise ConfigError(f"--repo: {repo!r} is not a git repository")
    base = _resolve_rev(repo, since, "--since")
    head = _resolve_rev(repo, until, "--until")
    done = _git(repo, ["log", "--reverse", "--format=%H%x1f%s%x1f%b%x1e", f"{base}..{head}"])
    if done.returncode != 0:
        raise ConfigError(f"--repo: git log failed: {done.stderr.strip()}")
    commits = []
    for record in done.stdout.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, _, rest = record.partition("\x1f")
        subject, _, body = rest.partition("\x1f")
        commits.append(Commit(sha=sha.strip(), subject=subject.strip(), body=body.strip()))
    return commits


def _first_body_line(body: str) -> str:
    return next((line.strip() for line in body.splitlines() if line.strip()), "")


def pr_number(commit: Commit) -> tuple[int, str, bool] | None:
    """Return ``(number, title, merge_form)`` for the pull request ``commit`` merged, else ``None``.

    ``merge_form`` is True for a merge commit, whose subject is not the pull
    request title: the title is then the one the commit carries (Gitea's
    quoted title, Azure DevOps' text after the colon, else the first body
    line), falling back to the subject.
    """
    subject, body = commit.subject, commit.body
    if m := _GITEA_MERGE_RE.match(subject):
        return int(m["n"]), m["title"], True
    if m := _AZURE_MERGE_RE.match(subject):
        return int(m["n"]), m["title"].strip(), True
    for pattern in (_GITHUB_MERGE_RE, _BITBUCKET_MERGE_RE):
        if m := pattern.match(subject):
            return int(m["n"]), _first_body_line(body) or subject, True
    if m := _GITLAB_BODY_RE.search(body):
        return int(m["n"]), _first_body_line(body) or subject, True
    if m := _SQUASH_RE.search(subject):
        return int(m["n"]), subject, False
    return None


def categorize(title: str, body: str = "") -> tuple[str, str]:
    """Return ``(category, text)`` for one pull request title.

    ``text`` is the title without its conventional type prefix and without a
    trailing ``(#N)``. A type outside :data:`CATEGORIES` goes to ``other``
    with the whole title kept; ``!`` after the type, or a ``BREAKING
    CHANGE:`` footer in ``body``, puts the entry under ``breaking``.
    """
    clean = _TRAILING_REF_RE.sub("", title).strip()
    m = _CONVENTIONAL_RE.match(clean)
    breaking = bool(_BREAKING_FOOTER_RE.search(body or ""))
    if m is None or m["type"].lower() not in _TYPES:
        return ("breaking" if breaking else "other"), clean
    text = m["rest"].strip()
    if m["bang"] or breaking:
        return "breaking", text
    return m["type"].lower(), text


def merged_pull_requests(commits: Iterable[Commit]) -> tuple[list[PullRequest], int]:
    """Return one :class:`PullRequest` per number, oldest first, and the count of commits naming none.

    When several commits name one number, a merge commit wins (it is the
    forge's own record of the merge); among the rest, the oldest commit with
    a known conventional type wins, else the oldest commit.
    """
    by_number: dict[int, list[PullRequest]] = {}
    without = 0
    for commit in commits:
        found = pr_number(commit)
        if found is None:
            without += 1
            continue
        number, title, merge_form = found
        by_number.setdefault(number, []).append(PullRequest(number, title, commit.sha, merge_form))
    chosen = []
    for candidates in by_number.values():
        merges = [pr for pr in candidates if pr.merge_form]
        typed = [pr for pr in candidates if categorize(pr.title)[0] != "other"]
        chosen.append((merges or typed or candidates)[0])
    return chosen, without


def executive_excerpt(body: str | None, max_chars: int = EXCERPT_MAX_CHARS) -> str:
    """Return a one-line excerpt of a prxref summary comment, or ``""`` when nothing is left.

    HTML comments (the summary and reviewed-head markers) are removed, and
    the footer with it: everything from the last ``---`` rule, and any
    paragraph carrying the attribution. The verdict is read from the
    ``## prxref automated review: <verdict>`` heading. Headings, blockquotes
    (the size and partial-review banners) and the ``PR: ... · files
    reviewed: N`` line are skipped, and the first paragraph left is the
    excerpt: on the packaged template, the finding counts. The result is
    ``<verdict>: <paragraph>`` on one line, cut at ``max_chars``.
    """
    if not body:
        return ""
    text = _HTML_COMMENT_RE.sub("", body).replace("\r\n", "\n")
    lines = text.split("\n")
    rules = [i for i, line in enumerate(lines) if line.strip() == "---"]
    if rules:
        lines = lines[: rules[-1]]
    verdict = ""
    paragraph = ""
    for block in re.split(r"\n\s*\n", "\n".join(lines)):
        kept = []
        for line in block.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            heading = _VERDICT_HEADING_RE.match(stripped)
            if heading:
                verdict = verdict or heading["verdict"]
                continue
            if stripped.startswith(("#", ">")) or _PR_LINE_RE.match(stripped):
                continue
            kept.append(stripped)
        joined = " ".join(kept)
        if not joined or ATTRIBUTION_MARKER in joined:
            continue
        paragraph = joined
        break
    excerpt = f"{verdict}: {paragraph}" if verdict and paragraph else (paragraph or verdict)
    excerpt = re.sub(r"\s+", " ", excerpt).strip()
    if len(excerpt) > max_chars:
        excerpt = excerpt[: max_chars - 3].rstrip() + "..."
    return excerpt


_CLOUD_PR_PATHS = {
    "github.com": "/pull/",
    "gitlab.com": "/-/merge_requests/",
    "bitbucket.org": "/pull-requests/",
    "codeberg.org": "/pulls/",
}


def pr_url_base(*, pr_url_base: str | None = None, repo_url: str | None = None) -> str:
    """Return the URL prefix a pull request number is appended to.

    ``pr_url_base`` is taken as given (a ``/`` is added when missing).
    ``repo_url`` is mapped by its shape: an Azure DevOps ``/_git/<repo>``
    path, a Bitbucket Server ``/projects|users/<key>/repos/<repo>`` path, or
    one of the cloud hosts github.com, gitlab.com, bitbucket.org and
    codeberg.org. Any other ``repo_url`` is ambiguous (a self-hosted GitHub,
    GitLab and Gitea look alike) and raises ``ConfigError`` pointing at
    ``--pr-url-base``. Either way the prefix must form a pull request URL
    :func:`prxref.forges.base.detect_forge` recognizes, or ``ConfigError``
    names the flag.
    """
    if pr_url_base:
        flag, base = "--pr-url-base", pr_url_base.strip()
        if not base.endswith("/"):
            base += "/"
    elif repo_url:
        flag = "--repo-url"
        raw = repo_url.strip().rstrip("/")
        if raw.endswith(".git"):
            raw = raw[:-4]
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host:
            raise ConfigError(f"--repo-url: {repo_url!r} is not an http(s) URL")
        if "/_git/" in parts.path:
            suffix = "/pullrequest/"
        elif re.search(r"/(?:projects|users)/[^/]+/repos/[^/]+$", parts.path):
            suffix = "/pull-requests/"
        elif host in _CLOUD_PR_PATHS:
            suffix = _CLOUD_PR_PATHS[host]
        else:
            raise ConfigError(
                f"--repo-url: cannot tell which forge serves {host!r}; "
                "pass --pr-url-base with the pull request URL up to its number"
            )
        base = raw + suffix
    else:
        raise ConfigError("--pr-url-base or --repo-url is required")
    ref = detect_forge(base + "1")
    if ref is None or ref.number != 1:
        raise ConfigError(f"{flag}: {base}<N> is not a pull request URL prxref recognizes")
    return base


def entry_for(
    pr_number_: int,
    title: str,
    url: str,
    summary: str | None,
    body: str = "",
) -> tuple[Entry, bool]:
    """Build one :class:`Entry`; the flag is True when a prxref summary supplied the excerpt."""
    category, text = categorize(title, body)
    excerpt = executive_excerpt(summary)
    return Entry(pr_number_, url, category, text, excerpt), bool(excerpt)


def render_entry(entry: Entry) -> str:
    """One bullet: ``- <text> ([#N](url))``, plus an indented excerpt line when there is one."""
    line = f"- {entry.text} ([#{entry.number}]({entry.url}))"
    if entry.excerpt:
        line += f"\n  prxref review: {entry.excerpt}"
    return line


def render(entries: Sequence[Entry], *, title: str | None = None) -> str:
    """Markdown for ``entries``: a ``## <Category>`` heading per non-empty group, in :data:`CATEGORIES` order."""
    out: list[str] = []
    if title:
        out += [title, ""]
    for key, heading in CATEGORIES:
        group = [e for e in entries if e.category == key]
        if not group:
            continue
        out += [f"## {heading}", ""]
        out += [render_entry(e) for e in group]
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n" if out else ""


SummaryReader = Callable[[PRRef], "str | None"]


def build_notes(
    prs: Sequence[PullRequest],
    base: str,
    read_summary: SummaryReader | None,
    *,
    bodies: dict[str, str] | None = None,
) -> Notes:
    """Read each pull request's summary through ``read_summary`` and build the entries.

    ``read_summary`` is the forge's ``get_summary``, or ``None`` when the
    forge has none. A read that raises, returns no summary, or returns one
    with no excerpt makes that entry a fallback (the title alone), recorded
    with its reason; it never fails the run.
    """
    notes = Notes()
    bodies = bodies or {}
    for pr in prs:
        url = f"{base}{pr.number}"
        summary = None
        reason = ""
        ref = detect_forge(url)
        if read_summary is None:
            reason = "the forge cannot read summaries"
        elif ref is None:
            reason = f"{url} is not a pull request URL"
        else:
            try:
                summary = read_summary(ref)
            except Exception as exc:  # noqa: BLE001 - one PR's failure is a fallback, not a failed run
                reason = f"summary read failed: {type(exc).__name__}: {exc}"
            else:
                if summary is None:
                    reason = "no prxref summary"
        entry, found = entry_for(pr.number, pr.title, url, summary, bodies.get(pr.sha, ""))
        if found:
            notes.summaries_found += 1
        else:
            notes.fallbacks.append((pr.number, reason or "the summary has no excerpt"))
        notes.entries.append(entry)
    return notes


def append_to_draft(path: str | Path, entry: Entry) -> bool:
    """Add ``entry`` under its category heading in the draft file at ``path``.

    The file is created with a ``# Unreleased`` title when missing. A
    missing category heading is inserted in :data:`CATEGORIES` order. An
    entry whose ``[#N](`` link is already in the file is skipped. Returns
    True when the file was written and False when the entry was already
    there. ``OSError`` propagates; the daemon caller logs it.
    """
    target = Path(path)
    text = target.read_text(encoding="utf-8") if target.exists() else f"{DRAFT_TITLE}\n"
    if f"[#{entry.number}](" in text:
        return False
    lines = text.rstrip("\n").split("\n")
    heading = f"## {_HEADINGS[entry.category]}"
    order = [f"## {h}" for _, h in CATEGORIES]
    bullet = render_entry(entry).split("\n")
    if heading in lines:
        start = lines.index(heading)
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        if end == start + 1:
            bullet = ["", *bullet]
        lines[end:end] = bullet
    else:
        later = order[order.index(heading) + 1:]
        at = next((i for i, line in enumerate(lines) if line in later), None)
        block = [heading, "", *bullet, ""]
        if at is None:
            lines += ["", *block]
        else:
            lines[at:at] = block
    target.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    return True
