"""Bitbucket Server / Data Center REST API v1 forge implementation."""
from __future__ import annotations

import os
import re
from collections.abc import Sequence
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from prxref.forges.base import InlineComment, PRData, PRRef, Thread

_REQUEST_TIMEOUT = (10.0, 30.0)
_SUMMARY_MARKER = "<!-- prxref-summary -->"
_PAGE_LIMIT = 100
_MAX_PAGES = 5

_BBS_URL_RE = re.compile(
    r"^https?://(?P<host>[^/]+)"
    r"(?P<context>(?:/[^/]+)*?)"
    r"/(?P<kind>projects|users)/(?P<key>[^/]+)"
    r"/repos/(?P<repo>[^/]+)"
    r"/pull-requests/(?P<number>\d+)(?:/.*)?$",
    re.IGNORECASE,
)


def _make_retry_session() -> requests.Session:
    """Build a requests.Session with bounded retries for transient failures."""
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(
            ["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"]
        ),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_DEFAULT_SESSION = _make_retry_session()


class ForgeImpl:
    """Bitbucket Server / Data Center Forge adapter."""

    name: str = "bitbucket-server"

    def __init__(self, session: requests.Session | None = None) -> None:
        """Initialize with an optional custom requests Session."""
        self._session = session if session is not None else _DEFAULT_SESSION

    @staticmethod
    def parse_pr_url(url: str) -> PRRef | None:
        """Return a PRRef if this forge recognizes the URL, else None.

        Accepts both project (``/projects/KEY/repos/...``) and personal
        (``/users/slug/repos/...``) repositories, and tolerates a deployment
        context path such as ``/bitbucket`` between host and route. ``owner``
        holds the API project key, which for a personal repository is the user
        slug prefixed with ``~``.
        """
        try:
            parsed = urlparse(url)
        except ValueError:
            return None

        if not parsed.scheme or not parsed.netloc:
            return None

        match = _BBS_URL_RE.match(url)
        if not match:
            return None

        host = match.group("host")
        context = match.group("context") or ""
        kind = match.group("kind").lower()
        key = match.group("key")
        repo = match.group("repo")
        number = int(match.group("number"))

        # Personal repositories live under the ~slug project key in the API,
        # while their browsable URL uses /users/slug. Keep the API form in
        # `owner` so every request path is built the same way for both kinds.
        owner = f"~{key}" if kind == "users" else key
        normalized_url = (
            f"https://{host}{context}/{kind}/{key}/repos/{repo}/pull-requests/{number}"
        )

        return PRRef(
            forge="bitbucket-server",
            host=host,
            owner=owner,
            repo=repo,
            number=number,
            url=normalized_url,
        )

    def _get_auth(self) -> tuple[dict[str, str], tuple[str, str] | None]:
        """Read authentication credentials from the environment at call time.

        Mirrors how GitHub Enterprise resolves its token: the deployment-specific
        variable wins, and the Cloud variable is the fallback so a single-forge
        setup does not need two names.
        """
        token = (
            os.environ.get("PRXREF_BITBUCKET_SERVER_TOKEN")
            or os.environ.get("PRXREF_BITBUCKET_TOKEN")
        )
        if token:
            return {"Authorization": f"Bearer {token}"}, None

        user = os.environ.get("PRXREF_BITBUCKET_SERVER_USER")
        password = os.environ.get("PRXREF_BITBUCKET_SERVER_PASSWORD")
        if user and password:
            return {}, (user, password)

        return {}, None

    def _context_path(self, ref: PRRef) -> str:
        """Recover the deployment context path from the normalized URL."""
        match = _BBS_URL_RE.match(ref.url)
        if match:
            return match.group("context") or ""
        return ""

    def _pr_url(self, ref: PRRef, suffix: str = "") -> str:
        """Construct the Data Center API endpoint URL for a given PR."""
        context = self._context_path(ref)
        base = (
            f"https://{ref.host}{context}/rest/api/1.0"
            f"/projects/{ref.owner}/repos/{ref.repo}/pull-requests/{ref.number}"
        )
        return f"{base}{suffix}"

    def get_pr(self, ref: PRRef) -> PRData:
        """Fetch normalized PR metadata."""
        headers, auth = self._get_auth()
        resp = self._session.get(
            self._pr_url(ref), headers=headers, auth=auth, timeout=_REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()

        author_user = ((data.get("author") or {}).get("user")) or {}
        author = (
            author_user.get("name")
            or author_user.get("slug")
            or author_user.get("displayName")
            or ""
        )

        from_ref = data.get("fromRef") or {}
        to_ref = data.get("toRef") or {}

        return PRData(
            title=data.get("title") or "",
            description=data.get("description") or "",
            author=author,
            source_branch=from_ref.get("displayId") or "",
            target_branch=to_ref.get("displayId") or "",
            source_sha=from_ref.get("latestCommit") or "",
            target_sha=to_ref.get("latestCommit") or "",
            raw=data,
        )

    def get_diff(self, ref: PRRef) -> str:
        """Fetch the raw unified diff of the PR (all files)."""
        headers, auth = self._get_auth()
        headers["Accept"] = "text/plain"
        resp = self._session.get(
            self._pr_url(ref, ".diff"),
            headers=headers,
            auth=auth,
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        diff_text = resp.text

        if not diff_text or not diff_text.strip():
            raise ValueError(
                "Empty or truncated diff received from Bitbucket Server for "
                f"{ref.owner}/{ref.repo}#{ref.number}"
            )

        return diff_text

    def _iter_activities(self, ref: PRRef) -> list[dict]:
        """Page through the PR activity feed and return the COMMENTED entries.

        Data Center has no flat comment listing: comments arrive as entries in
        an activity stream, paged with start/limit rather than a next URL.
        """
        headers, auth = self._get_auth()
        url = self._pr_url(ref, "/activities")
        entries: list[dict] = []
        start = 0

        for _ in range(_MAX_PAGES):
            resp = self._session.get(
                url,
                params={"start": start, "limit": _PAGE_LIMIT},
                headers=headers,
                auth=auth,
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            page = resp.json()

            for item in page.get("values", []):
                if (item.get("action") or "").upper() == "COMMENTED":
                    entries.append(item)

            if page.get("isLastPage", True):
                break
            next_start = page.get("nextPageStart")
            if next_start is None:
                break
            start = next_start

        return entries

    def post_summary(self, ref: PRRef, body: str) -> None:
        """Post (or update) the top-level review summary comment.

        Data Center rejects a comment update that does not carry the comment's
        current ``version``, so the existing comment is looked up for its
        version rather than only its id.
        """
        headers, auth = self._get_auth()
        url = self._pr_url(ref, "/comments")

        existing: dict | None = None
        try:
            for item in self._iter_activities(ref):
                comment = item.get("comment") or {}
                if comment.get("anchor"):
                    continue
                if _SUMMARY_MARKER in (comment.get("text") or ""):
                    existing = comment
                    break
        except requests.RequestException:
            existing = None

        if existing is not None and existing.get("id") is not None:
            resp = self._session.put(
                f"{url}/{existing['id']}",
                json={"text": body, "version": existing.get("version", 0)},
                headers=headers,
                auth=auth,
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return

        resp = self._session.post(
            url,
            json={"text": body},
            headers=headers,
            auth=auth,
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()

    def post_inline_comments(self, ref: PRRef, comments: Sequence[InlineComment]) -> int:
        """Post inline comments; returns the number actually posted."""
        if not comments:
            return 0

        headers, auth = self._get_auth()
        url = self._pr_url(ref, "/comments")
        posted = 0

        for comment in comments:
            payload = {
                "text": comment.body,
                # lineType ADDED + fileType TO anchor the comment to the line in
                # the NEW file, matching InlineComment's contract. A line that is
                # not part of the diff is a 4xx, skipped like every other forge.
                "anchor": {
                    "line": comment.line,
                    "lineType": "ADDED",
                    "fileType": "TO",
                    "path": comment.path,
                },
            }
            try:
                resp = self._session.post(
                    url,
                    json=payload,
                    headers=headers,
                    auth=auth,
                    timeout=_REQUEST_TIMEOUT,
                )
                if 200 <= resp.status_code < 300:
                    posted += 1
                elif 400 <= resp.status_code < 500:
                    continue
                else:
                    resp.raise_for_status()
            except requests.RequestException:
                continue

        return posted

    def list_threads(self, ref: PRRef) -> list[Thread]:
        """List existing discussion threads on the PR."""
        threads: list[Thread] = []
        try:
            entries = self._iter_activities(ref)
        except requests.RequestException:
            return threads

        for item in entries:
            comment = item.get("comment") or {}
            if not comment:
                continue

            anchor = comment.get("anchor") or {}
            path = anchor.get("path")
            line = anchor.get("line")

            author = comment.get("author") or {}
            threads.append(
                Thread(
                    path=path,
                    line=line,
                    resolved=_is_resolved(comment),
                    author=author.get("name") or author.get("slug") or "",
                    body_snippet=(comment.get("text") or "")[:200],
                )
            )

        return threads


def _is_resolved(comment: dict) -> bool:
    """Decide whether a Data Center comment counts as already-addressed.

    DC exposes resolution three ways depending on version and on whether the
    comment is the thread root: an explicit RESOLVED state, a threadResolved
    flag, or a resolvedDate on the root comment.
    """
    if (comment.get("state") or "").upper() == "RESOLVED":
        return True
    if comment.get("threadResolved"):
        return True
    return comment.get("resolvedDate") is not None
