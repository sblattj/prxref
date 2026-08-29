"""Bitbucket Cloud REST API v2 forge implementation."""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator, Sequence
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from prxref.forges.base import (
    SUMMARY_MARKER,
    FeedReadError,
    InlineComment,
    PRData,
    PRRef,
    Thread,
    with_summary_marker,
)

logger = logging.getLogger(__name__)

_API_BASE = "https://api.bitbucket.org/2.0"
_REQUEST_TIMEOUT = (10.0, 30.0)
_PAGE_SIZE = 100
# The comment walk used to stop at 5 pages, which silently truncated dedup at
# 500 comments. 50 puts the ceiling far past any real PR, and running out of
# budget is now a refusal to post rather than an invisible short read.
_MAX_PAGES = 50

_BB_URL_RE = re.compile(
    r"^https?://bitbucket\.org/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?:pull-requests|pullrequests|pullrequest)/(?P<number>\d+)(?:/.*)?$",
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
    """Bitbucket Cloud Forge adapter."""

    name: str = "bitbucket"

    def __init__(self, session: requests.Session | None = None) -> None:
        """Initialize with an optional custom requests Session."""
        self._session = session if session is not None else _DEFAULT_SESSION

    @staticmethod
    def parse_pr_url(url: str) -> PRRef | None:
        """Return a PRRef if this forge recognizes the URL, else None."""
        try:
            parsed = urlparse(url)
        except ValueError:
            return None

        if parsed.netloc.lower() != "bitbucket.org":
            return None

        match = _BB_URL_RE.match(url)
        if not match:
            return None

        owner = match.group("owner")
        repo = match.group("repo")
        number = int(match.group("number"))
        normalized_url = f"https://bitbucket.org/{owner}/{repo}/pull-requests/{number}"

        return PRRef(
            forge="bitbucket",
            host="bitbucket.org",
            owner=owner,
            repo=repo,
            number=number,
            url=normalized_url,
        )

    def _get_auth(self) -> tuple[dict[str, str], tuple[str, str] | None]:
        """Read authentication credentials from the environment at call time."""
        token = os.environ.get("PRXREF_BITBUCKET_TOKEN")
        if token:
            return {"Authorization": f"Bearer {token}"}, None

        user = os.environ.get("PRXREF_BITBUCKET_USER")
        password = os.environ.get("PRXREF_BITBUCKET_APP_PASSWORD")
        if user and password:
            return {}, (user, password)

        return {}, None

    def _pr_url(self, ref: PRRef, suffix: str = "") -> str:
        """Construct the Bitbucket API endpoint URL for a given PR."""
        base = f"{_API_BASE}/repositories/{ref.owner}/{ref.repo}/pullrequests/{ref.number}"
        return f"{base}{suffix}"

    def get_pr(self, ref: PRRef) -> PRData:
        """Fetch normalized PR metadata."""
        headers, auth = self._get_auth()
        url = self._pr_url(ref)
        resp = self._session.get(url, headers=headers, auth=auth, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        author_data = data.get("author") or {}
        user_data = author_data.get("user") or {}
        author_name = (
            author_data.get("display_name")
            or author_data.get("nickname")
            or user_data.get("display_name")
            or user_data.get("nickname")
            or author_data.get("raw")
            or ""
        )

        title = data.get("title") or ""
        description = data.get("description") or ""
        if not description:
            summary = data.get("summary") or {}
            description = summary.get("raw") or ""

        source = data.get("source") or {}
        source_branch = (source.get("branch") or {}).get("name") or ""
        source_sha = (source.get("commit") or {}).get("hash") or ""

        destination = data.get("destination") or {}
        target_branch = (destination.get("branch") or {}).get("name") or ""
        target_sha = (destination.get("commit") or {}).get("hash") or ""

        return PRData(
            title=title,
            description=description,
            author=author_name,
            source_branch=source_branch,
            target_branch=target_branch,
            source_sha=source_sha,
            target_sha=target_sha,
            raw=data,
        )

    def get_diff(self, ref: PRRef) -> str:
        """Fetch the raw unified diff of the PR."""
        headers, auth = self._get_auth()
        headers["Accept"] = "text/plain"
        url = self._pr_url(ref, "/diff")
        resp = self._session.get(url, headers=headers, auth=auth, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        diff_text = resp.text

        if not diff_text or not diff_text.strip():
            raise ValueError(
                f"Empty or truncated diff received from Bitbucket for {ref.owner}/{ref.repo}#{ref.number}"
            )

        return diff_text

    def _iter_comment_pages(self, ref: PRRef) -> Iterator[list[dict]]:
        """Yield the PR's comments one page at a time, following ``next``.

        A page at a time rather than one flat list, so a caller hunting for a
        single comment stops at the page it appears on instead of paying for
        the whole feed. Any read that does not reach the end — transport
        failure, non-OK status, unparseable body, or the page budget running
        out — raises ``FeedReadError`` instead of returning short, so no
        caller can mistake "I stopped early" for "that was all".
        """
        headers, auth = self._get_auth()
        url: str | None = self._pr_url(ref, "/comments")
        params: dict[str, int | str] | None = {"pagelen": _PAGE_SIZE}
        where = f"{ref.owner}/{ref.repo}#{ref.number}"

        for _ in range(_MAX_PAGES):
            try:
                resp = self._session.get(
                    url,
                    params=params,
                    headers=headers,
                    auth=auth,
                    timeout=_REQUEST_TIMEOUT,
                )
            except requests.RequestException as e:
                raise FeedReadError(
                    f"comment feed for {where} could not be read: {e}"
                ) from e
            if not resp.ok:
                raise FeedReadError(
                    f"comment feed for {where} returned HTTP {resp.status_code}"
                )
            try:
                data = resp.json()
            except ValueError as e:
                raise FeedReadError(
                    f"comment feed for {where} returned an unreadable body: {e}"
                ) from e
            if not isinstance(data, dict):
                raise FeedReadError(
                    f"comment feed for {where} returned "
                    f"{type(data).__name__}, not a page object"
                )

            yield [v for v in (data.get("values") or []) if isinstance(v, dict)]

            url = data.get("next")
            params = None
            if not url:
                return

        raise FeedReadError(
            f"comment feed for {where} outran the {_MAX_PAGES}-page budget "
            f"({_MAX_PAGES * _PAGE_SIZE} comments) without reaching the end"
        )

    def post_summary(self, ref: PRRef, body: str) -> None:
        """Post (or update) the top-level review summary comment.

        This adapter used to POST unconditionally — no lookup at all — so
        every re-review left another summary on the PR. It now does what the
        other three do: scan the comment feed for ``SUMMARY_MARKER`` on a
        top-level comment and PUT over that one if it is there.

        A ``FeedReadError`` from the lookup propagates rather than being
        swallowed. A summary that fails to post is recoverable by re-running;
        a second summary on someone's PR is not recoverable without a human
        deleting it, so the unreadable feed loses the tie.
        """
        headers, auth = self._get_auth()
        url = self._pr_url(ref, "/comments")
        body = with_summary_marker(body)

        existing_id = None
        for page in self._iter_comment_pages(ref):
            for item in page:
                # An inline comment quoting the marker is not the summary, and
                # a deleted comment is a slot nobody can read an update in.
                if item.get("inline") or _is_deleted(item):
                    continue
                raw = (item.get("content") or {}).get("raw") or ""
                if SUMMARY_MARKER in raw:
                    existing_id = item.get("id")
                    break
            if existing_id is not None:
                break

        payload = {"content": {"raw": body}}
        if existing_id is not None:
            resp = self._session.put(
                f"{url}/{existing_id}",
                json=payload,
                headers=headers,
                auth=auth,
                timeout=_REQUEST_TIMEOUT,
            )
        else:
            resp = self._session.post(
                url,
                json=payload,
                headers=headers,
                auth=auth,
                timeout=_REQUEST_TIMEOUT,
            )
        resp.raise_for_status()

    def post_inline_comments(self, ref: PRRef, comments: Sequence[InlineComment]) -> int:
        """Post inline comments; returns the number actually posted."""
        headers, auth = self._get_auth()
        url = self._pr_url(ref, "/comments")
        posted = 0

        for comment in comments:
            payload = {
                "content": {"raw": comment.body},
                "inline": {
                    "path": comment.path,
                    "to": comment.line,
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
        """List existing discussion threads on the PR.

        Unlike ``post_summary`` this keeps whatever it managed to read: the
        threads only feed best-effort dedup, and the orchestrator substitutes
        an empty list for any exception, so raising would throw away pages
        that were read successfully. The shortfall is logged rather than
        swallowed — an under-read here shows up as findings re-posted on a
        re-review, with nothing in the output to explain why.
        """
        threads: list[Thread] = []
        try:
            for page in self._iter_comment_pages(ref):
                for item in page:
                    inline = item.get("inline")
                    path = inline.get("path") if inline else None
                    line = inline.get("to") if inline else None

                    user = item.get("user") or item.get("author") or {}
                    author = (
                        user.get("uuid")
                        or user.get("nickname")
                        or user.get("display_name")
                        or ""
                    )

                    content = item.get("content") or {}
                    raw_body = content.get("raw") or ""
                    body_snippet = raw_body[:200]

                    threads.append(
                        Thread(
                            path=path,
                            line=line,
                            resolved=_is_deleted(item),
                            author=author,
                            body_snippet=body_snippet,
                        )
                    )
        except FeedReadError as e:
            logger.warning(
                "comment feed read was incomplete for %s/%s#%s; thread dedup "
                "is working from the %d comments that were read: %s",
                ref.owner, ref.repo, ref.number, len(threads), e,
            )

        return threads


def _is_deleted(comment: dict) -> bool:
    """Whether Bitbucket has tombstoned this comment.

    A deleted comment stays in the feed with its body blanked. It counts as
    resolved for dedup, and it is not a slot ``post_summary`` may update into:
    the update would land somewhere nobody can read.
    """
    return bool(comment.get("deleted", False)) or comment.get("deleted_on") is not None
