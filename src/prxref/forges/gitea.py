"""Gitea and Forgejo forge implementation (#31).

One adapter covers Gitea, Forgejo, Codeberg and Gitea Cloud: all of them serve
the same ``/api/v1`` REST API, on any host and optionally under a sub-path.
A pull request URL is ``<scheme>://<host>[/<sub-path>]/<owner>/<repo>/pulls/<n>``,
and the API base is ``<scheme>://<host>[/<sub-path>]/api/v1``. The scheme is
kept, because a self-hosted instance can serve plain HTTP.

The token comes from ``PRXREF_GITEA_TOKEN`` and is sent as ``Authorization:
token <t>``. Without one every read still works against a public repository.

The summary is an issue comment, found again by ``SUMMARY_MARKER`` and edited in
place. Inline comments go out as one ``COMMENT`` review per call, each anchored
by ``new_position``, which the API reads as a line of the new file. The API has
no raw compare diff, so ``get_compare_diff`` rebuilds one from the compare
listing and whole files at the merge base and the head.
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Collection, Iterator, Sequence
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

from prxref.retry_logging import LoggingRetry

from ._diff_render import render_diff_entries
from .azure_devops import _render_hunks
from .base import (
    ATTRIBUTION_MARKER,
    MAX_LISTING_PAGES,
    SUMMARY_MARKER,
    CommitData,
    FeedReadError,
    InlineComment,
    PathListing,
    PRData,
    PRRef,
    Thread,
    with_summary_marker,
)

logger = logging.getLogger(__name__)

_PR_URL_RE = re.compile(
    r"^(?P<scheme>https?)://(?P<host>[^/?#]+)"
    r"(?P<prefix>(?:/[^/?#]+)*?)"
    r"/(?P<owner>[^/?#]+)/(?P<repo>[^/?#]+)/pulls/(?P<number>\d+)"
    r"(?:[/?#].*)?$",
    re.IGNORECASE,
)
# Hosts that belong to another forge, and an ``/api/`` segment ahead of the
# owner, name an API URL (``https://api.github.com/repos/o/r/pulls/1``) or
# another forge's page, never a Gitea pull request, so neither is claimed.
_OTHER_FORGE_HOSTS = frozenset({
    "github.com", "api.github.com", "gitlab.com", "bitbucket.org", "api.bitbucket.org", "dev.azure.com",
})
_API_SEGMENT_RE = re.compile(r"(?:^|/)api(?:/|$)", re.IGNORECASE)
_TOKEN_ENV = "PRXREF_GITEA_TOKEN"
_REQUEST_TIMEOUT = (10.0, 30.0)
# Gitea clamps a page to MAX_RESPONSE_ITEMS, 50 by default, whatever limit the
# caller asks for, so a page size above it would read a clamped page as the
# last one. The review walk also stops only on an empty page, which stays
# correct under an administrator's lower clamp.
_PAGE_SIZE = 50
_MAX_PAGES = 50
_TREE_PAGE_SIZE = 1000
_ERROR_DETAIL_CHARS = 400
_MAX_FILE_CONTENT_BYTES = 512 * 1024
# The rebuilt compare diff fetches two whole files per changed file; past
# these budgets a file keeps its header and loses its hunks, with one warning.
_MAX_COMPARE_FILES = 300
_MAX_COMPARE_BLOB_BYTES = 512 * 1024


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


def _create_default_session() -> requests.Session:
    """A session that retries read verbs only, as every other adapter's does."""
    session = requests.Session()
    retry_strategy = LoggingRetry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=True,
        allowed_methods=frozenset(["GET", "HEAD", "OPTIONS"]),
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class ForgeImpl:
    """Gitea and Forgejo forge implementation (Codeberg and Gitea Cloud included)."""

    name: str = "gitea"
    suggestion_style: str = "gitea"

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or _create_default_session()

    @staticmethod
    def parse_pr_url(url: str) -> PRRef | None:
        """Return a PRRef for a ``.../<owner>/<repo>/pulls/<n>`` URL, else None.

        Any self-hosted host is accepted, with or without a sub-path ahead of
        the owner. The hosts of the other forges' clouds, and a URL with an
        ``api`` segment ahead of the owner (an API URL, not a page), are
        refused. The normalized ``url`` keeps the scheme and the sub-path,
        which the API base is derived from, and drops any trailing path,
        query or fragment.
        """
        m = _PR_URL_RE.match(url.strip())
        if not m:
            return None
        scheme = m.group("scheme").lower()
        host, prefix = m.group("host"), m.group("prefix")
        if host.lower().split(":", 1)[0] in _OTHER_FORGE_HOSTS or _API_SEGMENT_RE.search(prefix):
            return None
        owner, repo, number = m.group("owner"), m.group("repo"), int(m.group("number"))
        return PRRef(
            forge="gitea",
            host=host,
            owner=owner,
            repo=repo,
            number=number,
            url=f"{scheme}://{host}{prefix}/{owner}/{repo}/pulls/{number}",
        )

    def _api_base(self, ref: PRRef) -> str:
        m = _PR_URL_RE.match(ref.url or "")
        if m:
            return f"{m.group('scheme').lower()}://{m.group('host')}{m.group('prefix')}/api/v1"
        return f"https://{ref.host}/api/v1"

    def _repo_url(self, ref: PRRef) -> str:
        return f"{self._api_base(ref)}/repos/{quote(ref.owner, safe='')}/{quote(ref.repo, safe='')}"

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        token = os.environ.get(_TOKEN_ENV)
        if token:
            headers["Authorization"] = f"token {token}"
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _where(ref: PRRef) -> str:
        return f"{ref.owner}/{ref.repo}#{ref.number}"

    def get_pr(self, ref: PRRef) -> PRData:
        """Fetch normalized PR metadata from ``/pulls/{n}``."""
        resp = self.session.get(
            f"{self._repo_url(ref)}/pulls/{ref.number}",
            headers=self._headers(), timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        user = data.get("user") or {}
        head = data.get("head") or {}
        base = data.get("base") or {}
        return PRData(
            title=data.get("title") or "",
            description=data.get("body") or "",
            author=user.get("login", "") if isinstance(user, dict) else "",
            source_branch=head.get("ref", ""),
            target_branch=base.get("ref", ""),
            source_sha=head.get("sha", ""),
            target_sha=base.get("sha", ""),
            raw=data,
        )

    def get_diff(self, ref: PRRef) -> str:
        """Fetch the PR's raw unified diff from ``/pulls/{n}.diff``, returned as-is."""
        resp = self.session.get(
            f"{self._repo_url(ref)}/pulls/{ref.number}.diff",
            headers=self._headers({"Accept": "text/plain"}), timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.text

    def get_compare_diff(self, ref: PRRef, *, base_sha: str, head_sha: str) -> str:
        """Return the unified diff of ``head_sha`` against its merge-base with ``base_sha``.

        The API serves a compare only as JSON, so the diff is rebuilt. One read
        of ``/compare/{base}...{head}`` lists the changed files from the merge
        base and the commits on the head side. The merge base is the one
        parent of those commits that is not itself listed; a range where that
        is not exactly one commit (a head that merged the base branch in), or
        whose listing is short of ``total_commits``, raises ``ValueError``
        rather than guessing. Each file's two sides are then read through
        ``/raw/{path}?ref=`` and rendered with ``difflib``. A binary side, a
        side over 512 KiB, or any file past the first 300 renders header-only,
        with one warning naming the count. An empty range returns ``""``.
        Raises on an HTTP or transport failure.
        """
        repo = self._repo_url(ref)
        resp = self.session.get(
            f"{repo}/compare/{base_sha}...{head_sha}",
            headers=self._headers(), timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError(f"Gitea compare for {self._where(ref)} returned {type(body).__name__}, not an object")
        files = [f for f in body.get("files") or () if isinstance(f, dict) and f.get("filename")]
        commits = [c for c in body.get("commits") or () if isinstance(c, dict)]
        if not files:
            return ""
        total = body.get("total_commits")
        if isinstance(total, int) and total != len(commits):
            raise ValueError(
                f"Gitea compare for {self._where(ref)} listed {len(commits)} of {total} commits; "
                "the merge base cannot be derived from a partial listing"
            )
        listed = {c.get("sha") for c in commits}
        outside = {
            p.get("sha")
            for c in commits
            for p in c.get("parents") or ()
            if isinstance(p, dict) and p.get("sha") and p.get("sha") not in listed
        }
        if len(outside) != 1:
            raise ValueError(
                f"Gitea compare for {self._where(ref)} {base_sha[:12]}...{head_sha[:12]} has "
                f"{len(outside)} candidate merge bases; refusing to guess one"
            )
        merge_base = outside.pop()

        entries: list[dict] = []
        skipped = 0
        for index, f in enumerate(files):
            new_path = f["filename"]
            status = f.get("status") or "modified"
            old_path = f.get("previous_filename") or new_path
            entry = {
                "old_path": old_path,
                "new_path": new_path,
                "new_file": status == "added",
                "deleted_file": status in ("removed", "deleted"),
                "renamed_file": status == "renamed",
                "diff": None,
            }
            entries.append(entry)
            if index >= _MAX_COMPARE_FILES:
                skipped += 1
                continue
            old = b"" if entry["new_file"] else self._compare_blob(repo, old_path, merge_base)
            new = b"" if entry["deleted_file"] else self._compare_blob(repo, new_path, head_sha)
            if old is None or new is None:
                skipped += 1
                continue
            hunks = _render_hunks(old, new)
            if hunks:
                entry["diff"] = "\n".join(hunks)
        if skipped:
            logger.warning(
                "Gitea compare for %s: %d file(s) are reviewed header-only (binary, over "
                "512 KiB, or past the first %d files)",
                self._where(ref), skipped, _MAX_COMPARE_FILES,
            )
        return render_diff_entries(entries)

    def _compare_blob(self, repo: str, path: str, sha: str) -> bytes | None:
        """One side of a compare file; ``None`` when it is binary or over the cap. Raises on HTTP failure."""
        resp = self.session.get(
            f"{repo}/raw/{quote(path, safe='/')}",
            headers=self._headers({"Accept": "*/*"}), params={"ref": sha},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        content = resp.content
        if len(content) > _MAX_COMPARE_BLOB_BYTES or b"\x00" in content[:8000]:
            return None
        return content

    def get_commits(
        self, ref: PRRef, *, base_sha: str = "", head_sha: str = ""
    ) -> list[CommitData]:
        """Return the PR's commits, oldest first (issue #70).

        The API has no per-PR commit listing, so one read of the same
        compare endpoint ``get_compare_diff`` uses supplies them:
        ``/compare/{base}...{head}`` answers a ``commits`` array in
        oldest-first order. Either sha left empty is filled by re-reading
        the PR (one extra request, so the caller that already holds
        ``get_pr``'s answer pays nothing). Unlike ``get_compare_diff`` the
        merge base is not derived here — only the commit entries
        themselves are read — so a partial listing is returned as-is. Each
        entry keeps the ``sha``, the first line of the nested
        ``commit.message`` as the subject, and ``len(parents)``.
        """
        base, head = base_sha, head_sha
        if not base or not head:
            pr = self.get_pr(ref)
            base, head = pr.target_sha, pr.source_sha
        resp = self.session.get(
            f"{self._repo_url(ref)}/compare/{base}...{head}",
            headers=self._headers(), timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        commits = body.get("commits") if isinstance(body, dict) else None
        if not isinstance(commits, list):
            raise ValueError(
                f"Gitea compare for {self._where(ref)} returned "
                f"{type(body).__name__}, not an object with a commits list"
            )
        result: list[CommitData] = []
        for entry in commits:
            if not isinstance(entry, dict):
                continue
            commit = entry.get("commit")
            message = commit.get("message") if isinstance(commit, dict) else None
            subject = message.splitlines()[0] if message else ""
            result.append(CommitData(
                sha=entry.get("sha") or "",
                subject=subject,
                parent_count=len(entry.get("parents") or []),
            ))
        return result

    def _get_json(self, ref: PRRef, url: str, *, what: str, params: dict | None = None) -> Any:
        """GET one JSON document, raising ``FeedReadError`` on any failure."""
        where = self._where(ref)
        try:
            resp = self.session.get(url, headers=self._headers(), params=params, timeout=_REQUEST_TIMEOUT)
        except requests.RequestException as e:
            raise FeedReadError(f"{what} for {where} could not be read: {e}") from e
        if not resp.ok:
            raise FeedReadError(f"{what} for {where} returned HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as e:
            raise FeedReadError(f"{what} for {where} returned an unreadable body: {e}") from e

    def _get_list(self, ref: PRRef, url: str, *, what: str, params: dict | None = None) -> list[dict]:
        items = self._get_json(ref, url, what=what, params=params)
        if not isinstance(items, list):
            raise FeedReadError(f"{what} for {self._where(ref)} returned {type(items).__name__}, not a list")
        return [item for item in items if isinstance(item, dict)]

    def _iter_reviews(self, ref: PRRef) -> Iterator[list[dict]]:
        """Yield the PR's reviews a page at a time; an empty page ends the walk.

        Every failure, and a walk that outruns ``_MAX_PAGES``, raises
        ``FeedReadError``.
        """
        url = f"{self._repo_url(ref)}/pulls/{ref.number}/reviews"
        for page in range(1, _MAX_PAGES + 1):
            items = self._get_list(ref, url, what="review feed", params={"limit": _PAGE_SIZE, "page": page})
            if not items:
                return
            yield items
        raise FeedReadError(
            f"review feed for {self._where(ref)} outran the {_MAX_PAGES}-page budget without reaching the end"
        )

    def _review_comments(self, ref: PRRef, review: dict) -> list[dict]:
        """Every inline comment of one review; the endpoint is not paged."""
        if review.get("comments_count") == 0 or review.get("id") is None:
            return []
        url = f"{self._repo_url(ref)}/pulls/{ref.number}/reviews/{review['id']}/comments"
        return self._get_list(ref, url, what="review comment feed")

    def _find_summary(self, ref: PRRef) -> tuple[int | None, str | None]:
        """Return the id and body of the summary comment ``post_summary`` overwrites.

        The issue-comment listing is not paged (``limit`` and ``page`` are
        ignored and every comment comes back), so this is one read. A read
        that fails raises ``FeedReadError``. ``(None, None)`` means none.
        """
        url = f"{self._repo_url(ref)}/issues/{ref.number}/comments"
        for c in self._get_list(ref, url, what="comment feed"):
            body = c.get("body") or ""
            if SUMMARY_MARKER in body and c.get("id") is not None:
                return c.get("id"), body
        return None, None

    def get_summary(self, ref: PRRef) -> str | None:
        """Return the raw body of the summary comment ``post_summary`` would update, or None.

        Raises ``FeedReadError`` when the comment listing cannot be read. Never writes.
        """
        _, body = self._find_summary(ref)
        return body

    def post_summary(self, ref: PRRef, body: str) -> None:
        """Post the summary as an issue comment, or PATCH the one already carrying the marker.

        A ``FeedReadError`` from the lookup propagates, so a failed read never
        falls through to a second summary.
        """
        body = with_summary_marker(body)
        existing_id, _ = self._find_summary(ref)
        if existing_id is not None:
            resp = self.session.patch(
                f"{self._repo_url(ref)}/issues/comments/{existing_id}",
                json={"body": body}, headers=self._headers(), timeout=_REQUEST_TIMEOUT,
            )
        else:
            resp = self.session.post(
                f"{self._repo_url(ref)}/issues/{ref.number}/comments",
                json={"body": body}, headers=self._headers(), timeout=_REQUEST_TIMEOUT,
            )
        resp.raise_for_status()

    def post_inline_comments(self, ref: PRRef, comments: Sequence[InlineComment]) -> int:
        """Post every comment in one ``COMMENT`` review; returns the number posted.

        Each comment is anchored at ``line`` through ``new_position``, a line of
        the new file (``old_position`` for a ``LEFT``-side comment). A
        multi-line comment anchors at its last line; ``start_line`` is not
        sent, since the API has no range. The review carries the head SHA as
        ``commit_id`` and an empty body. The server does not check a line
        against the diff, so the review is accepted whole or refused whole: a
        refusal is logged with the response body and raised.
        """
        if not comments:
            return 0
        commit_id = self.get_pr(ref).source_sha
        payload_comments = []
        for comment in comments:
            left = (comment.side or "RIGHT").upper() == "LEFT"
            payload_comments.append({
                "path": comment.path,
                "body": comment.body,
                "new_position": 0 if left else comment.line,
                "old_position": comment.line if left else 0,
            })
        resp = self.session.post(
            f"{self._repo_url(ref)}/pulls/{ref.number}/reviews",
            json={"event": "COMMENT", "body": "", "commit_id": commit_id, "comments": payload_comments},
            headers=self._headers(), timeout=_REQUEST_TIMEOUT,
        )
        if not resp.ok:
            logger.warning(
                "Gitea refused the review of %d inline comment(s) on %s (HTTP %s): %s",
                len(comments), self._where(ref), resp.status_code, _response_detail(resp),
            )
            resp.raise_for_status()
        return len(comments)

    def list_threads(self, ref: PRRef) -> list[Thread]:
        """List every review comment as a thread, for re-review dedup.

        ``line`` is the comment's new-file ``position``, or ``None`` for an
        old-side or outdated comment. ``resolved`` is true when the comment
        carries a ``resolver``. A feed that cannot be read to the end is
        logged and the threads read so far are returned.
        """
        threads: list[Thread] = []
        try:
            for reviews in self._iter_reviews(ref):
                for review in reviews:
                    for item in self._review_comments(ref, review):
                        position = item.get("position")
                        user = item.get("user") or {}
                        threads.append(Thread(
                            path=item.get("path"),
                            line=position if _is_positive_int(position) else None,
                            resolved=bool(item.get("resolver")),
                            author=user.get("login", "") if isinstance(user, dict) else "",
                            body_snippet=(item.get("body") or "")[:120],
                        ))
        except FeedReadError as e:
            logger.warning(
                "review-comment feed read was incomplete for %s; thread dedup is working "
                "from the %d threads recovered so far: %s",
                self._where(ref), len(threads), e,
            )
        return threads

    def prune_inline_comments(self, ref: PRRef, *, paths: Collection[str] | None = None) -> int:
        """Delete prxref-attributed inline comments; returns the count removed.

        Only a comment carrying ``ATTRIBUTION_MARKER`` is a candidate, and with
        ``paths`` only one whose ``path`` is in it. Every review is read before
        anything is deleted, so a deletion cannot shift a later page. A review
        whose every comment is a candidate and whose body is empty or
        attributed is deleted whole through ``DELETE /reviews/{id}``, which
        Gitea and Forgejo both serve. Otherwise each candidate is deleted
        through ``DELETE /reviews/{id}/comments/{c}``, which Forgejo serves and
        Gitea may not. A refused delete is logged and skipped, and a feed that
        cannot be read ends the prune: best-effort, never raising.
        """
        wanted = None if paths is None else frozenset(paths)
        removed = 0
        try:
            reviews = [review for page in self._iter_reviews(ref) for review in page]
            plan = [(review, self._review_comments(ref, review)) for review in reviews]
        except FeedReadError as e:
            logger.warning(
                "prune of review comments on %s read nothing to delete (best-effort): %s",
                self._where(ref), e,
            )
            return 0
        base = f"{self._repo_url(ref)}/pulls/{ref.number}/reviews"
        headers = self._headers()
        for review, items in plan:
            candidates = [
                c for c in items
                if ATTRIBUTION_MARKER in (c.get("body") or "")
                and c.get("id") is not None
                and (wanted is None or (isinstance(c.get("path"), str) and c.get("path") in wanted))
            ]
            if not candidates:
                continue
            review_body = (review.get("body") or "").strip()
            if len(candidates) == len(items) and (not review_body or ATTRIBUTION_MARKER in review_body):
                resp = self.session.delete(f"{base}/{review['id']}", headers=headers, timeout=_REQUEST_TIMEOUT)
                if resp.ok:
                    removed += len(candidates)
                    continue
                logger.warning(
                    "could not prune review %s on %s (HTTP %s): %s",
                    review["id"], self._where(ref), resp.status_code, _response_detail(resp),
                )
                continue
            for c in candidates:
                resp = self.session.delete(
                    f"{base}/{review['id']}/comments/{c['id']}", headers=headers, timeout=_REQUEST_TIMEOUT
                )
                if resp.ok:
                    removed += 1
                else:
                    logger.warning(
                        "could not prune inline comment %s on %s (HTTP %s): %s",
                        c["id"], self._where(ref), resp.status_code, _response_detail(resp),
                    )
        return removed

    def get_file_content(self, ref: PRRef, path: str, *, sha: str) -> str | None:
        """Return the text of ``path`` at commit ``sha`` from ``/raw/{path}?ref=``, best-effort.

        A missing file or a directory (HTTP 404), any other failure, a body
        over 512 KiB and one holding a NUL byte all give ``None``. Never raises.
        """
        if not sha:
            return None
        try:
            resp = self.session.get(
                f"{self._repo_url(ref)}/raw/{quote(path, safe='/')}",
                headers=self._headers({"Accept": "*/*"}), params={"ref": sha},
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            logger.debug("get_file_content failed for %s@%s: %s", path, sha, e)
            return None
        if not resp.ok:
            logger.debug("get_file_content got HTTP %s for %s@%s", resp.status_code, path, sha)
            return None
        content = resp.content
        if len(content) > _MAX_FILE_CONTENT_BYTES:
            logger.debug("get_file_content body over 512 KiB for %s@%s", path, sha)
            return None
        if b"\x00" in content:
            logger.debug("get_file_content body looked binary for %s@%s", path, sha)
            return None
        return content.decode("utf-8", errors="replace")

    def list_paths(self, ref: PRRef, *, sha: str) -> PathListing | None:
        """Return every file path in the repository at commit ``sha``, best-effort.

        Pages through the recursive tree listing, ``/git/trees/{sha}``, where
        ``truncated`` means another page follows. Only ``blob`` entries are
        kept, sorted and deduplicated. ``complete`` is ``False`` when the walk
        stops at ``MAX_LISTING_PAGES`` with pages left. An empty ``sha``, or a
        failure on any page, gives ``None``. Never raises.
        """
        if not sha:
            return None
        url = f"{self._repo_url(ref)}/git/trees/{quote(sha, safe='')}"
        paths: set[str] = set()
        truncated = False
        for page in range(1, MAX_LISTING_PAGES + 1):
            try:
                resp = self.session.get(
                    url, headers=self._headers(),
                    params={"recursive": "true", "per_page": _TREE_PAGE_SIZE, "page": page},
                    timeout=_REQUEST_TIMEOUT,
                )
            except requests.RequestException as e:
                logger.debug("list_paths failed for %s@%s: %s", self._where(ref), sha, e)
                return None
            if not resp.ok:
                logger.debug("list_paths got HTTP %s for %s@%s", resp.status_code, self._where(ref), sha)
                return None
            try:
                body = resp.json()
            except ValueError as e:
                logger.debug("list_paths got a non-JSON body for %s@%s: %s", self._where(ref), sha, e)
                return None
            tree = body.get("tree") if isinstance(body, dict) else None
            if not isinstance(tree, list):
                logger.debug("list_paths got no tree list for %s@%s", self._where(ref), sha)
                return None
            paths.update(
                entry["path"] for entry in tree
                if isinstance(entry, dict) and entry.get("type") == "blob"
                and isinstance(entry.get("path"), str) and entry["path"]
            )
            truncated = bool(body.get("truncated"))
            if not truncated or not tree:
                break
        return PathListing(paths=tuple(sorted(paths)), complete=not truncated)
