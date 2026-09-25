"""Azure DevOps Services / Server REST API forge implementation.

Covers Azure DevOps Services (``dev.azure.com`` and the legacy
``*.visualstudio.com`` hosts) and Azure DevOps Server (on-prem, any host, URL
carrying the collection and the project). Every request speaks REST
``api-version=7.1`` against a project-scoped route, because the org-level
routes refuse anonymous callers even on public projects.

Azure DevOps has no unified-diff endpoint, so ``get_diff`` rebuilds one: the
Diffs API (``diffs/commits`` with ``diffCommonCommit=true``) lists the changed
files from the merge base to the source head, each side's content comes from
the blobs API by object id, and ``difflib`` renders git-apply-faithful hunks,
``\\ No newline at end of file`` included. The same path serves
``get_compare_diff`` for a pinned commit range.
"""
from __future__ import annotations

import base64
import concurrent.futures
import difflib
import functools
import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple
from urllib.parse import quote, unquote, urlsplit

import requests
from requests.adapters import HTTPAdapter

from prxref.forges.base import (
    ATTRIBUTION_MARKER,
    SUMMARY_MARKER,
    FeedReadError,
    InlineComment,
    PathListing,
    PRData,
    PRRef,
    Thread,
    with_summary_marker,
)
from prxref.retry_logging import LoggingRetry

logger = logging.getLogger(__name__)

_API_VERSION = "7.1"
_REQUEST_TIMEOUT = (10.0, 30.0)
# A rejection body is operator-only diagnostics, never posted to the forge, but
# it is still bounded so a long validation error cannot bury the log line.
_ERROR_DETAIL_CHARS = 400
# get_file_content is best-effort context, not the review itself: a body past
# this size (or one that looks binary) is worth skipping rather than shipping
# hundreds of KB into a worker prompt.
_MAX_FILE_CONTENT_BYTES = 512 * 1024
# The Diffs API pages with $top/$skip, and only the last page carries
# allChangesIncluded. Running out of pages RAISES: an incomplete file list is a
# wrong review, not a smaller one.
_DIFF_PAGE_SIZE = 1000
_MAX_PAGES = 50
# Content budget for the rebuilt diff. Blob sizes are unknown until download,
# so the file and byte budgets are checked between fetch batches; a file past
# any of them keeps its header (the reviewer still sees it changed) and loses
# its hunks, with one warning naming the count.
_MAX_BLOB_BYTES = 512 * 1024
_MAX_CONTENT_FILES = 300
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_FETCH_WORKERS = 8
# git's own heuristic: a NUL in the first 8000 bytes of either side is binary.
_BINARY_SNIFF_BYTES = 8000
# Known-binary extensions are never downloaded at all.
_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".webp", ".pdf", ".zip", ".gz", ".7z", ".jar",
    ".dll", ".exe", ".so", ".dylib", ".pdb", ".mov", ".mp4", ".mp3", ".wav", ".woff", ".woff2",
    ".ttf", ".otf", ".eot", ".bacpac", ".dacpac", ".pfx", ".snk", ".nupkg",
})
_TOKEN_ENV = "PRXREF_AZURE_DEVOPS_TOKEN"
_PIPELINE_TOKEN_ENV = "SYSTEM_ACCESSTOKEN"
# Inline findings open as "active", like an unresolved review comment on every
# other forge. The summary is posted "closed": it is the equivalent of a
# GitHub issue comment, which nothing can require to be resolved, so it must
# never hold a PR behind a "Check for comment resolution" policy.
_INLINE_THREAD_STATUS = "active"
_SUMMARY_THREAD_STATUS = "closed"
_COMMENT_TYPE_TEXT = 1
_RESOLVED_STATUSES = frozenset({"fixed", "wontfix", "closed", "bydesign"})

_ADO_URL_RE = re.compile(
    r"^(?P<scheme>https?)://(?P<host>[^/?#]+)"
    r"(?P<prefix>(?:/[^/?#]+)*?)"
    r"/_git/(?P<repo>[^/?#]+)"
    r"/pullrequest/(?P<number>\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)


class _Location(NamedTuple):
    """Where a pull request lives: everything a request URL is rebuilt from."""

    scheme: str
    host: str
    collection: str
    project: str
    repo: str
    number: int


def _locate(url: str) -> _Location | None:
    """Split an Azure DevOps pull request URL into its location, or None.

    The path segments in front of ``/_git/{repo}`` mean different things per
    host. On ``dev.azure.com`` the first is the organization and an optional
    second is the project. On ``*.visualstudio.com`` the organization is the
    subdomain, and an optional leading ``DefaultCollection`` precedes an
    optional project. Anywhere else is Azure DevOps Server, where the last
    segment is the project and everything before it is the collection path;
    fewer than two segments there is ambiguous and refused. A missing project
    is the short form, whose project is named like the repository.
    """
    match = _ADO_URL_RE.match(url.strip())
    if not match:
        return None
    scheme, host = match.group("scheme").lower(), match.group("host")
    try:
        hostname = (urlsplit(f"{scheme}://{host}").hostname or "").lower()
    except ValueError:
        return None
    segments = [s for s in (match.group("prefix") or "").split("/") if s]
    if hostname == "dev.azure.com":
        if len(segments) not in (1, 2):
            return None
        collection, rest = "/" + segments[0], segments[1:]
    elif hostname.endswith(".visualstudio.com"):
        if segments and segments[0].lower() == "defaultcollection":
            collection, rest = "/" + segments[0], segments[1:]
        else:
            collection, rest = "", segments
        if len(rest) > 1:
            return None
    else:
        if len(segments) < 2:
            return None
        collection, rest = "/" + "/".join(segments[:-1]), segments[-1:]
    repo = unquote(match.group("repo"))
    project = unquote(rest[0]) if rest else repo
    return _Location(scheme, host, collection, project, repo, int(match.group("number")))


def _response_detail(resp: requests.Response) -> str:
    """Return a bounded, single-line rendering of an error response body."""
    try:
        body = resp.text or ""
    except Exception:  # noqa: BLE001 - a body that will not decode is not a failure
        return "<unreadable body>"
    collapsed = " ".join(body.split())
    if len(collapsed) > _ERROR_DETAIL_CHARS:
        return collapsed[:_ERROR_DETAIL_CHARS] + "…"
    return collapsed or "<empty body>"


def _make_retry_session() -> requests.Session:
    """Build a requests.Session with bounded retries for transient failures.

    Read verbs only, for the reason spelled out in bitbucket_server.py: a write
    that commits server-side and then loses its response would be re-sent
    whole by urllib3, and the PR would carry a duplicate comment.
    """
    session = requests.Session()
    retry = LoggingRetry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD", "OPTIONS"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_DEFAULT_SESSION = _make_retry_session()


def _strip_heads(ref_name: str) -> str:
    """Return a branch ref name without its ``refs/heads/`` prefix."""
    prefix = "refs/heads/"
    return ref_name[len(prefix):] if ref_name.startswith(prefix) else ref_name


def _extension(path: str | None) -> str:
    """Return the lowercased extension of the last path segment, or ``""``."""
    lowered = (path or "").lower()
    dot = lowered.rfind(".")
    return lowered[dot:] if dot > lowered.rfind("/") else ""


@dataclass
class _Change:
    """One blob change from the Diffs API, classified for rendering."""

    status: str
    old: str | None
    new: str | None
    old_oid: str | None
    new_oid: str | None
    pure_rename: bool = False
    binary: bool = False
    header_only: bool = False

    @property
    def label(self) -> str:
        """The path a log line names."""
        return self.new or self.old or ""


def _classify(changes: Sequence[dict]) -> list[_Change]:
    """Turn raw Diffs API entries into the blob changes a diff renders.

    Tree entries (folders) and commit entries (submodules) drop out. A rename
    arrives as two entries, the ``rename`` itself plus a ``delete,
    sourceRename`` for the old path; the second half is dropped, or every
    rename would also show up as a deletion. The rename's source is in
    ``sourceServerItem``.
    """
    out: list[_Change] = []
    for change in changes:
        item = change.get("item") or {}
        kind = item.get("gitObjectType")
        if kind != "blob":
            if kind == "commit":
                logger.debug("skipping submodule entry %s", item.get("path"))
            continue
        tokens = {t.strip().lower() for t in (change.get("changeType") or "").split(",")}
        if "sourcerename" in tokens:
            continue
        path = (item.get("path") or "").lstrip("/")
        if tokens & {"add", "undelete", "branch"}:
            entry = _Change("added", None, path, None, item.get("objectId"))
        elif "delete" in tokens:
            entry = _Change("removed", path, None, item.get("originalObjectId"), None)
        elif "rename" in tokens:
            source = (change.get("sourceServerItem") or change.get("originalPath") or "").lstrip("/")
            entry = _Change("renamed", source, path, item.get("originalObjectId"), item.get("objectId"))
        else:
            entry = _Change("modified", path, path, item.get("originalObjectId"), item.get("objectId"))
        if any(ch in (entry.old or "") + (entry.new or "") for ch in "\t\n"):
            logger.warning("skipping %r: a path with a tab or newline cannot be expressed in a diff", entry.label)
            continue
        entry.pure_rename = bool(
            entry.status == "renamed"
            and entry.old_oid
            and entry.new_oid
            and entry.old_oid.lower() == entry.new_oid.lower()
        )
        entry.binary = _extension(entry.new or entry.old) in _BINARY_EXTENSIONS
        out.append(entry)
    return out


def _render_hunks(old: bytes, new: bytes) -> list[str]:
    """Render the ``@@`` hunks between two text blobs, git-apply-faithfully.

    Both sides split with ``splitlines(keepends=True)``: the diff parser splits
    the whole diff with ``str.splitlines()``, so splitting content any other
    way would leave a boundary (``\\x0c``, ``\\x85``, …) inside an emitted line
    for the parser to split a second time, and the hunk bodies would stop
    matching their ``@@`` counts. Keeping the terminators also makes a
    trailing-newline-only change a hunk, as it is in git, and a last line
    without a terminator gets git's ``\\ No newline at end of file`` marker.
    """
    old_lines = old.decode("utf-8", errors="replace").splitlines(keepends=True)
    new_lines = new.decode("utf-8", errors="replace").splitlines(keepends=True)
    out: list[str] = []
    for raw in list(difflib.unified_diff(old_lines, new_lines, n=3, lineterm=""))[2:]:
        if raw.startswith("@@"):
            out.append(raw)
            continue
        body = raw[1:]
        parts = body.splitlines()
        content = parts[0] if parts else ""
        out.append(raw[0] + content)
        if content == body:
            out.append("\\ No newline at end of file")
    return out


class ForgeImpl:
    """Azure DevOps Services / Server Forge adapter."""

    name: str = "azure-devops"

    def __init__(self, session: requests.Session | None = None) -> None:
        """Initialize with an optional custom requests Session."""
        self._session = session if session is not None else _DEFAULT_SESSION

    @staticmethod
    def parse_pr_url(url: str) -> PRRef | None:
        """Return a PRRef if this forge recognizes the URL, else None.

        Accepts ``https://dev.azure.com/{org}/{project}/_git/{repo}/pullrequest/{n}``,
        the same on ``{org}.visualstudio.com`` (with or without
        ``DefaultCollection``), the short form without a project (the project
        is then named like the repository), and Azure DevOps Server URLs on
        any host whose path carries the collection and the project. ``owner``
        holds the project and ``repo`` the repository, both percent-decoded.
        The collection lives only in ``url``, which is normalized: the query
        and fragment are dropped, the short form gains its explicit project,
        and the scheme is lowercased but kept, because an on-prem Server can
        serve plain HTTP.
        """
        loc = _locate(url)
        if loc is None:
            return None
        normalized = (
            f"{loc.scheme}://{loc.host}{loc.collection}/{quote(loc.project, safe='')}"
            f"/_git/{quote(loc.repo, safe='')}/pullrequest/{loc.number}"
        )
        return PRRef(
            forge="azure-devops",
            host=loc.host,
            owner=loc.project,
            repo=loc.repo,
            number=loc.number,
            url=normalized,
        )

    def _api_base(self, ref: PRRef) -> str:
        """Return the project-scoped repository API root, rebuilt from ``ref.url``."""
        loc = _locate(ref.url)
        if loc is None:
            raise ValueError(f"not an Azure DevOps pull request URL: {ref.url}")
        return (
            f"{loc.scheme}://{loc.host}{loc.collection}/{quote(loc.project, safe='')}"
            f"/_apis/git/repositories/{quote(loc.repo, safe='')}"
        )

    def _pr_api(self, ref: PRRef, suffix: str = "") -> str:
        """Return the pull request's API URL, plus ``suffix``."""
        return f"{self._api_base(ref)}/pullrequests/{ref.number}{suffix}"

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        """Build request headers, reading credentials from the environment at call time.

        ``PRXREF_AZURE_DEVOPS_TOKEN`` (a PAT, sent as Basic auth with an empty
        user name) wins; inside Azure Pipelines ``SYSTEM_ACCESSTOKEN`` (a
        bearer token) is the fallback; with neither, requests go out
        anonymously, which reads public projects. ``X-TFS-FedAuthRedirect:
        Suppress`` is always sent, so an unauthenticated call gets a clean 401
        instead of a redirect to a sign-in page.
        """
        headers = {"Accept": accept, "X-TFS-FedAuthRedirect": "Suppress"}
        pat = os.environ.get(_TOKEN_ENV, "").strip()
        pipeline = os.environ.get(_PIPELINE_TOKEN_ENV, "").strip()
        if pat:
            headers["Authorization"] = "Basic " + base64.b64encode(f":{pat}".encode()).decode()
        elif pipeline:
            headers["Authorization"] = f"Bearer {pipeline}"
        return headers

    def _get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        accept: str = "application/json",
        stream: bool = False,
    ) -> requests.Response:
        """GET ``url`` with the API version, the auth headers and the timeout."""
        return self._session.get(
            url,
            params={"api-version": _API_VERSION, **(params or {})},
            headers=self._headers(accept),
            timeout=_REQUEST_TIMEOUT,
            stream=stream,
        )

    @staticmethod
    def _json(resp: requests.Response, what: str) -> dict:
        """Return a response's JSON object, refusing anything that is not one.

        A server that ignores the Suppress header answers an unauthenticated
        call with a 2xx sign-in page (203 text/html). Parsing that as data
        would read as success with garbage in it, so it raises instead, with
        a hint to set the token.
        """
        resp.raise_for_status()
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if resp.status_code == 203 or "json" not in content_type:
            raise ValueError(
                f"Azure DevOps returned a non-JSON {what} (HTTP {resp.status_code}); the request was "
                f"probably not authenticated — set {_TOKEN_ENV}"
            )
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"Azure DevOps returned a {type(data).__name__} {what}, not an object")
        return data

    def _pr_json(self, ref: PRRef) -> dict:
        """Fetch the pull request's JSON."""
        return self._json(self._get(self._pr_api(ref)), "pull request")

    def get_pr(self, ref: PRRef) -> PRData:
        """Fetch normalized PR metadata.

        The author is the display name first: ``uniqueName`` is null for an
        anonymous caller and an e-mail address for an authenticated one.
        """
        pr = self._pr_json(ref)
        who = pr.get("createdBy") or {}
        return PRData(
            title=pr.get("title") or "",
            description=pr.get("description") or "",
            author=who.get("displayName") or who.get("uniqueName") or "",
            source_branch=_strip_heads(pr.get("sourceRefName") or ""),
            target_branch=_strip_heads(pr.get("targetRefName") or ""),
            source_sha=(pr.get("lastMergeSourceCommit") or {}).get("commitId") or "",
            target_sha=(pr.get("lastMergeTargetCommit") or {}).get("commitId") or "",
            raw=pr,
        )

    def get_diff(self, ref: PRRef) -> str:
        """Fetch the unified diff of the PR (all files), rebuilt from the Diffs API.

        The range is the PR's own: the merge base of the last merged target
        commit (or, lacking one, the target branch) up to the source head.
        An empty result raises, as on every other forge.
        """
        pr = self._pr_json(ref)
        head = (pr.get("lastMergeSourceCommit") or {}).get("commitId")
        if not head:
            raise ValueError(f"Azure DevOps PR {ref.number} has no source commit")
        base = (pr.get("lastMergeTargetCommit") or {}).get("commitId")
        if base:
            text = self._diff_between(ref, base, "commit", head)
        else:
            text = self._diff_between(ref, _strip_heads(pr.get("targetRefName") or ""), "branch", head)
        if not text:
            raise ValueError(f"empty diff for Azure DevOps PR {ref.number}")
        return text

    def get_compare_diff(self, ref: PRRef, *, base_sha: str, head_sha: str) -> str:
        """Return the unified diff of ``head_sha`` against its merge-base with ``base_sha``.

        The same Diffs API reconstruction as ``get_diff``, with both ends given
        as commits; ``diffCommonCommit=true`` supplies the three-dot semantics.
        Raises on transport or HTTP failure; returns ``""`` for an empty range.
        """
        return self._diff_between(ref, base_sha, "commit", head_sha)

    def _list_changes(self, ref: PRRef, base: str, base_type: str, head: str) -> list[dict]:
        """Page through the Diffs API change list from the merge base to ``head``."""
        changes: list[dict] = []
        skip = 0
        for _ in range(_MAX_PAGES):
            page_json = self._json(
                self._get(
                    f"{self._api_base(ref)}/diffs/commits",
                    {
                        "baseVersion": base,
                        "baseVersionType": base_type,
                        "targetVersion": head,
                        "targetVersionType": "commit",
                        "diffCommonCommit": "true",
                        "$top": _DIFF_PAGE_SIZE,
                        "$skip": skip,
                    },
                ),
                "diff listing",
            )
            page = page_json.get("changes") or []
            changes.extend(page)
            if page_json.get("allChangesIncluded") or not page:
                return changes
            skip += len(page)
        raise ValueError(
            f"Azure DevOps diff listing exceeded {_MAX_PAGES} pages; refusing an incomplete diff"
        )

    def _fetch_blob(self, ref: PRRef, oid: str) -> bytes | None:
        """Download one blob by object id; ``None`` when it is gone or over the cap.

        Any other failure raises: a diff where a 401 or a 5xx silently emptied
        every file would be a review of nothing that still says "Approved".
        """
        resp = self._get(
            f"{self._api_base(ref)}/blobs/{oid}",
            {"$format": "octetstream"},
            accept="application/octet-stream",
            stream=True,
        )
        try:
            if resp.status_code in (404, 410):
                logger.debug("Azure DevOps blob %s not found (HTTP %s)", oid, resp.status_code)
                return None
            resp.raise_for_status()
            buf = bytearray()
            for chunk in resp.iter_content(64 * 1024):
                buf += chunk
                if len(buf) > _MAX_BLOB_BYTES:
                    return None
            return bytes(buf)
        finally:
            resp.close()

    def _fetch_contents(self, ref: PRRef, entries: list[_Change]) -> dict[str, bytes | None]:
        """Fetch the blobs the diff needs, within the content budget.

        Binary-by-extension files and pure renames need no content. The rest
        are fetched in API order, one batch of ``_FETCH_WORKERS`` at a time,
        and the budget is checked between batches; every file after it runs
        out becomes header-only.
        """
        pending: list[_Change] = []
        for entry in entries:
            if entry.binary or entry.pure_rename:
                continue
            if (entry.status != "removed" and not entry.new_oid) or (
                entry.status != "added" and not entry.old_oid
            ):
                logger.warning("Azure DevOps change for %s lacks an object id; header only", entry.label)
                entry.header_only = True
                continue
            pending.append(entry)

        blobs: dict[str, bytes | None] = {}
        fetched = total = skipped = 0
        fetch = functools.partial(self._fetch_blob, ref)
        with concurrent.futures.ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
            for start in range(0, len(pending), _FETCH_WORKERS):
                batch = pending[start:start + _FETCH_WORKERS]
                if fetched >= _MAX_CONTENT_FILES or total > _MAX_TOTAL_BYTES:
                    for entry in batch:
                        entry.header_only = True
                    skipped += len(batch)
                    continue
                oids = list(dict.fromkeys(
                    oid for entry in batch for oid in (entry.old_oid, entry.new_oid) if oid and oid not in blobs
                ))
                for oid, data in zip(oids, pool.map(fetch, oids), strict=True):
                    blobs[oid] = data
                    total += len(data or b"")
                fetched += len(batch)
        if skipped:
            logger.warning("Azure DevOps diff: %d file(s) past the content budget are header-only", skipped)
        return blobs

    def _diff_between(self, ref: PRRef, base: str, base_type: str, head: str) -> str:
        """Rebuild the unified diff from ``base``'s merge base with ``head`` up to ``head``.

        One block per blob change, in API order. Paths are written unquoted,
        and the ``---``/``+++`` lines are kept even for binary files (where git
        omits them): the parser takes exact paths from them, which matters for
        a path containing `` b/`` that the ``diff --git`` line alone would
        split wrongly. Mode lines are always ``100644``; the Diffs API exposes
        no file modes. Returns ``""`` when no blob changed.
        """
        entries = _classify(self._list_changes(ref, base, base_type, head))
        blobs = self._fetch_contents(ref, entries)
        out: list[str] = []
        for entry in entries:
            a_path = f"a/{entry.old or entry.new}"
            b_path = f"b/{entry.new or entry.old}"
            block = [f"diff --git {a_path} {b_path}"]
            if entry.status == "added":
                block.append("new file mode 100644")
            elif entry.status == "removed":
                block.append("deleted file mode 100644")
            elif entry.status == "renamed":
                if entry.pure_rename:
                    block.append("similarity index 100%")
                block += [f"rename from {entry.old}", f"rename to {entry.new}"]
            if entry.pure_rename:
                out += block
                continue
            old_label = a_path if entry.status != "added" else "/dev/null"
            new_label = b_path if entry.status != "removed" else "/dev/null"
            block += [f"--- {old_label}", f"+++ {new_label}"]
            if entry.binary:
                out += block + [f"Binary files {old_label} and {new_label} differ"]
                continue
            if entry.header_only:
                out += block
                continue
            old = blobs.get(entry.old_oid, b"") if entry.old_oid else b""
            new = blobs.get(entry.new_oid, b"") if entry.new_oid else b""
            if old is None or new is None:
                logger.warning(
                    "Azure DevOps content for %s is over %d bytes or missing; header only",
                    entry.label, _MAX_BLOB_BYTES,
                )
                out += block
                continue
            if b"\x00" in old[:_BINARY_SNIFF_BYTES] or b"\x00" in new[:_BINARY_SNIFF_BYTES]:
                out += block + [f"Binary files {old_label} and {new_label} differ"]
                continue
            out += block + _render_hunks(old, new)
        return ("\n".join(out) + "\n") if out else ""

    def get_file_content(self, ref: PRRef, path: str, *, sha: str) -> str | None:
        """Return the text of ``path`` at commit ``sha``, best-effort.

        Reads the ``items`` endpoint as raw bytes, so a UTF-8 BOM survives as
        it does in the diff. Returns ``None`` on any failure, on a body over
        512 KiB, and on one that looks binary. Never raises.
        """
        if not sha:
            return None
        try:
            resp = self._get(
                f"{self._api_base(ref)}/items",
                {
                    "path": "/" + path.lstrip("/"),
                    "versionDescriptor.version": sha,
                    "versionDescriptor.versionType": "commit",
                    "download": "true",
                },
                accept="application/octet-stream",
                stream=True,
            )
            try:
                if not resp.ok:
                    logger.debug("get_file_content got HTTP %s for %s@%s", resp.status_code, path, sha)
                    return None
                buf = bytearray()
                for chunk in resp.iter_content(64 * 1024):
                    buf += chunk
                    if len(buf) > _MAX_FILE_CONTENT_BYTES:
                        logger.debug("get_file_content body over 512 KiB for %s@%s", path, sha)
                        return None
            finally:
                resp.close()
        except (requests.RequestException, ValueError) as e:
            logger.debug("get_file_content failed for %s@%s: %s", path, sha, e)
            return None
        if b"\x00" in buf[:_BINARY_SNIFF_BYTES]:
            logger.debug("get_file_content body looked binary for %s@%s", path, sha)
            return None
        return bytes(buf).decode("utf-8", errors="replace")

    def list_paths(self, ref: PRRef, *, sha: str) -> PathListing | None:
        """Return every file path in the repository at commit ``sha``, best-effort.

        One request to the ``items`` endpoint with ``recursionLevel=Full`` at
        the commit, starting at the repository root, with no paging. Only
        ``blob`` entries that are not flagged ``isFolder`` are kept, so the
        root (``/``), directories (``tree``) and every other object type,
        submodules included, are dropped. Azure DevOps returns each path with
        a leading slash, which is stripped; the paths are sorted and
        deduplicated.

        ``complete`` is ``False`` when the response carries an
        ``x-ms-continuationtoken`` header. Continuation on this endpoint is
        undocumented and was never observed, so the token is not followed: the
        paths already returned are kept and the listing is marked incomplete.
        An empty ``sha`` (no request is made), a transport failure, a non-2xx
        status, a 203 sign-in page or other non-JSON body, or a body with no
        ``value`` list gives ``None``. Never raises.
        """
        if not sha:
            return None
        where = f"{ref.owner}/{ref.repo}@{sha}"
        try:
            resp = self._get(
                f"{self._api_base(ref)}/items",
                {
                    "recursionLevel": "Full",
                    "versionDescriptor.version": sha,
                    "versionDescriptor.versionType": "commit",
                },
            )
            body = self._json(resp, "item listing")
        except (requests.RequestException, ValueError) as e:
            logger.debug("list_paths failed for %s: %s", where, e)
            return None
        values = body.get("value")
        if not isinstance(values, list):
            logger.debug("list_paths got no value list for %s", where)
            return None
        paths = {
            entry["path"].lstrip("/") for entry in values
            if isinstance(entry, dict) and entry.get("gitObjectType") == "blob"
            and not entry.get("isFolder")
            and isinstance(entry.get("path"), str) and entry["path"].lstrip("/")
        }
        complete = not resp.headers.get("x-ms-continuationtoken")
        if not complete:
            logger.debug(
                "list_paths got an x-ms-continuationtoken for %s and did not follow it; "
                "the listing is incomplete", where,
            )
        return PathListing(paths=tuple(sorted(paths)), complete=complete)

    def _read_threads(self, ref: PRRef) -> list[dict]:
        """Read every thread on the PR (one response; the API does not page them).

        Raises ``FeedReadError`` on anything short of a complete list, so
        ``post_summary`` can never mistake a failed read for "no summary".
        """
        try:
            resp = self._get(self._pr_api(ref, "/threads"))
        except requests.RequestException as e:
            raise FeedReadError(f"Azure DevOps threads for PR {ref.number} could not be read: {e}") from e
        if not resp.ok:
            raise FeedReadError(
                f"Azure DevOps threads for PR {ref.number} returned HTTP {resp.status_code}: "
                f"{_response_detail(resp)}"
            )
        try:
            value = self._json(resp, "thread list").get("value")
        except ValueError as e:
            raise FeedReadError(str(e)) from e
        if not isinstance(value, list):
            raise FeedReadError(f"Azure DevOps thread list for PR {ref.number} has no 'value' array")
        return [t for t in value if isinstance(t, dict)]

    @staticmethod
    def _usable(thread: dict) -> bool:
        """True for a live thread with comments that is not a system notice."""
        comments = thread.get("comments") or []
        return (
            bool(comments)
            and not thread.get("isDeleted")
            and isinstance(comments[0], dict)
            and comments[0].get("commentType") != "system"
        )

    @staticmethod
    def _root(thread: dict) -> dict:
        """Return the thread's first comment that is not deleted, or ``{}``."""
        return next(
            (c for c in thread.get("comments") or [] if isinstance(c, dict) and not c.get("isDeleted")),
            {},
        )

    def list_threads(self, ref: PRRef) -> list[Thread]:
        """List existing discussion threads on the PR.

        System notices (votes, pushes, status changes) are skipped. A thread
        counts as resolved when its status is fixed, won't-fix, closed or
        by-design. A feed that cannot be read is logged and yields what was
        read, because these threads only feed best-effort dedup.
        """
        threads: list[Thread] = []
        try:
            for thread in self._read_threads(ref):
                if not self._usable(thread):
                    continue
                context = thread.get("threadContext") or {}
                root = self._root(thread)
                who = root.get("author") or {}
                threads.append(
                    Thread(
                        path=(context.get("filePath") or "").lstrip("/") or None,
                        line=(context.get("rightFileStart") or {}).get("line"),
                        resolved=str(thread.get("status") or "").lower() in _RESOLVED_STATUSES,
                        author=who.get("displayName") or who.get("uniqueName") or "",
                        body_snippet=(root.get("content") or "")[:200],
                    )
                )
        except FeedReadError as e:
            logger.warning(
                "thread read was incomplete for %s/%s#%s; thread dedup is working from the %d "
                "threads that were read: %s",
                ref.owner, ref.repo, ref.number, len(threads), e,
            )
        return threads

    def post_summary(self, ref: PRRef, body: str) -> None:
        """Post (or update) the top-level review summary comment.

        The summary is a PR-level thread (no ``threadContext``) whose root
        comment carries ``SUMMARY_MARKER``; an existing one has its root
        comment PATCHed, and otherwise a new thread is created, closed. A
        ``FeedReadError`` from the lookup propagates: a failed lookup must not
        post a second summary.
        """
        body = with_summary_marker(body)
        for thread in self._read_threads(ref):
            if not self._usable(thread) or thread.get("threadContext"):
                continue
            root = self._root(thread)
            if SUMMARY_MARKER not in (root.get("content") or ""):
                continue
            if thread.get("id") is None or root.get("id") is None:
                continue
            resp = self._session.patch(
                self._pr_api(ref, f"/threads/{thread['id']}/comments/{root['id']}"),
                params={"api-version": _API_VERSION},
                json={"content": body},
                headers=self._headers(),
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return
        resp = self._session.post(
            self._pr_api(ref, "/threads"),
            params={"api-version": _API_VERSION},
            json={
                "comments": [{"parentCommentId": 0, "content": body, "commentType": _COMMENT_TYPE_TEXT}],
                "status": _SUMMARY_THREAD_STATUS,
            },
            headers=self._headers(),
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()

    def _change_tracking(self, ref: PRRef) -> dict[str, tuple[int, int]]:
        """Map each changed path to its ``(changeTrackingId, iteration)`` in the latest iteration.

        Best-effort enrichment for inline threads: iterations need credentials
        even on a public project, and a thread posts without this context, so
        any failure logs at debug and returns ``{}``.
        """
        try:
            iterations = self._json(self._get(self._pr_api(ref, "/iterations")), "iteration list").get("value")
            latest = max(int(i["id"]) for i in iterations or [])
            tracking: dict[str, tuple[int, int]] = {}
            skip = 0
            for _ in range(_MAX_PAGES):
                page = self._json(
                    self._get(self._pr_api(ref, f"/iterations/{latest}/changes"), {"$top": 2000, "$skip": skip}),
                    "iteration changes",
                )
                for entry in page.get("changeEntries") or []:
                    key = ((entry.get("item") or {}).get("path") or entry.get("originalPath") or "").lstrip("/")
                    if key and entry.get("changeTrackingId") is not None:
                        tracking[key] = (int(entry["changeTrackingId"]), latest)
                next_skip = page.get("nextSkip")
                if not next_skip:
                    break
                skip = int(next_skip)
            return tracking
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as e:
            logger.debug("Azure DevOps iteration context unavailable for PR %s: %s", ref.number, e)
            return {}

    def post_inline_comments(self, ref: PRRef, comments: Sequence[InlineComment]) -> int:
        """Post inline comments; returns the number actually posted.

        Each finding becomes its own thread anchored to one line of the new
        file, opened active. The latest iteration's ``changeTrackingId`` is
        attached when it can be read. A 4xx (a line outside the diff, say)
        is logged and skipped; a 5xx or transport failure is skipped too.
        """
        if not comments:
            return 0
        tracking = self._change_tracking(ref)
        posted = 0
        for comment in comments:
            path = comment.path.lstrip("/")
            payload: dict[str, Any] = {
                "comments": [{"parentCommentId": 0, "content": comment.body, "commentType": _COMMENT_TYPE_TEXT}],
                "status": _INLINE_THREAD_STATUS,
                "threadContext": {
                    "filePath": "/" + path,
                    "rightFileStart": {"line": comment.line, "offset": 1},
                    "rightFileEnd": {"line": comment.line, "offset": 1},
                },
            }
            if path in tracking:
                tracking_id, iteration = tracking[path]
                payload["pullRequestThreadContext"] = {
                    "changeTrackingId": tracking_id,
                    "iterationContext": {
                        "firstComparingIteration": iteration,
                        "secondComparingIteration": iteration,
                    },
                }
            try:
                resp = self._session.post(
                    self._pr_api(ref, "/threads"),
                    params={"api-version": _API_VERSION},
                    json=payload,
                    headers=self._headers(),
                    timeout=_REQUEST_TIMEOUT,
                )
                if 200 <= resp.status_code < 300:
                    posted += 1
                elif 400 <= resp.status_code < 500:
                    logger.warning(
                        "inline comment on %s:%s rejected (HTTP %s): %s",
                        comment.path, comment.line, resp.status_code, _response_detail(resp),
                    )
                else:
                    resp.raise_for_status()
            except requests.RequestException as e:
                logger.warning("inline comment on %s:%s failed: %s", comment.path, comment.line, e)
        return posted

    def prune_inline_comments(self, ref: PRRef) -> int:
        """Delete prxref-attributed inline comments; returns the count removed.

        Only the root comment of a file-anchored thread whose body carries the
        attribution marker is deleted, so the summary (a PR-level thread) and
        every human comment are left alone; a human reply keeps its thread.
        A delete the token may not perform (403 on another identity's comment)
        is logged and skipped, and an unreadable feed ends the pass:
        best-effort, because a cleanup must never abort the review.
        """
        try:
            threads = self._read_threads(ref)
        except FeedReadError as e:
            logger.warning("prune of inline comments on %s/%s#%s skipped: %s", ref.owner, ref.repo, ref.number, e)
            return 0
        removed = 0
        for thread in threads:
            if not (self._usable(thread) and thread.get("threadContext")):
                continue
            root = self._root(thread)
            if ATTRIBUTION_MARKER not in (root.get("content") or ""):
                continue
            if thread.get("id") is None or root.get("id") is None:
                continue
            try:
                resp = self._session.delete(
                    self._pr_api(ref, f"/threads/{thread['id']}/comments/{root['id']}"),
                    params={"api-version": _API_VERSION},
                    headers=self._headers(),
                    timeout=_REQUEST_TIMEOUT,
                )
            except requests.RequestException as e:
                logger.warning("could not prune inline thread %s: %s", thread.get("id"), e)
                continue
            if 200 <= resp.status_code < 300:
                removed += 1
            else:
                logger.warning(
                    "could not prune inline thread %s on %s/%s#%s (HTTP %s): %s",
                    thread.get("id"), ref.owner, ref.repo, ref.number, resp.status_code, _response_detail(resp),
                )
        return removed
