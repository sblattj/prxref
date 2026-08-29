"""Bitbucket Server / Data Center REST API v1 forge implementation."""
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

_REQUEST_TIMEOUT = (10.0, 30.0)
_PAGE_LIMIT = 100
# 5 pages of 100 was the old ceiling: a summary sitting at activity 501 was
# invisible, so post_summary missed its own marker and posted a second comment.
# 50 keeps the walk bounded (a feed that long is pathological, and the budget
# is a refusal to post rather than a silent short read) while putting the
# ceiling far past any real PR.
_MAX_PAGES = 50

_BBS_URL_RE = re.compile(
    r"^(?P<scheme>https?)://(?P<host>[^/]+)"
    r"(?P<context>(?:/[^/]+)*?)"
    r"/(?P<kind>projects|users)/(?P<key>[^/]+)"
    r"/repos/(?P<repo>[^/]+)"
    r"/pull-requests/(?P<number>\d+)(?:/.*)?$",
    re.IGNORECASE,
)

# Data Center serves its REST API at /rest/api/<version> — 1.0 today, plus the
# /latest alias — hanging directly off any deployment context path. A PR's REST
# URL therefore has the same shape as its browse URL, so the pattern above
# captures /rest/api/1.0 as if it were a context path, and every request built
# from it would carry /rest/api/1.0 twice. Strip it back off before it is
# mistaken for a context.
_BBS_REST_PREFIX_RE = re.compile(
    r"(?P<context>(?:/[^/]+)*?)/rest/api/(?:latest|\d+(?:\.\d+)*)",
    re.IGNORECASE,
)


def _strip_rest_prefix(context: str) -> str:
    """Return a captured context path with any REST API prefix removed.

    ``/rest/api/1.0`` yields ``""``; a REST prefix sitting under a genuine
    deployment context, ``/bitbucket/rest/api/latest``, yields ``/bitbucket``.
    Anything that is not a REST prefix is returned untouched, so a deployment
    context that merely happens to contain ``/rest`` survives.
    """
    match = _BBS_REST_PREFIX_RE.fullmatch(context)
    return match.group("context") if match else context


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

        A PR's REST URL (``/rest/api/1.0/projects/KEY/repos/...``) is accepted
        too and normalized back to the browse URL, because ``url`` is the link
        a human clicks and the value every request path is rebuilt from.

        The scheme is carried through rather than normalized to ``https``. A
        Data Center standalone install serves plain HTTP on port 7990, so
        ``http://`` is the product's out-of-the-box shape and not an edge case;
        rewriting it would aim every request at a TLS listener that is not
        there. The scheme is lowercased, so ``HTTP://`` round-trips as
        ``http://``.
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

        scheme = match.group("scheme").lower()
        host = match.group("host")
        context = _strip_rest_prefix(match.group("context") or "")
        kind = match.group("kind").lower()
        key = match.group("key")
        repo = match.group("repo")
        number = int(match.group("number"))

        # Personal repositories live under the ~slug project key in the API,
        # while their browsable URL uses /users/slug. Keep the API form in
        # `owner` so every request path is built the same way for both kinds,
        # and put the browse form in the normalized URL — a REST URL names the
        # repository the API way, so it arrives here as /projects/~slug.
        if kind == "projects" and len(key) > 1 and key.startswith("~"):
            kind, key = "users", key[1:]
        owner = f"~{key}" if kind == "users" else key
        normalized_url = (
            f"{scheme}://{host}{context}/{kind}/{key}/repos/{repo}/pull-requests/{number}"
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
        """Recover the deployment context path from the normalized URL.

        ``parse_pr_url`` has already stripped any REST prefix out of ``url``;
        stripping again here costs nothing and keeps a hand-built PRRef from
        reintroducing the doubled ``/rest/api/1.0`` this recovers into.
        """
        match = _BBS_URL_RE.match(ref.url)
        if match:
            return _strip_rest_prefix(match.group("context") or "")
        return ""

    def _scheme(self, ref: PRRef) -> str:
        """Recover the URL scheme from the normalized URL.

        ``PRRef`` carries no scheme field, so the value is read back out of
        ``url`` exactly as the deployment context path is. A hand-built PRRef
        whose ``url`` this pattern does not match falls back to ``https``,
        which is what every other adapter here assumes.
        """
        match = _BBS_URL_RE.match(ref.url)
        if match:
            return match.group("scheme").lower()
        return "https"

    def _pr_url(self, ref: PRRef, suffix: str = "") -> str:
        """Construct the Data Center API endpoint URL for a given PR."""
        context = self._context_path(ref)
        base = (
            f"{self._scheme(ref)}://{ref.host}{context}/rest/api/1.0"
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

    def _iter_activity_pages(self, ref: PRRef) -> Iterator[list[dict]]:
        """Yield the COMMENTED activity entries one page at a time.

        Data Center has no flat comment listing: comments arrive as entries in
        an activity stream, paged with start/limit rather than a next URL.

        A page at a time rather than one flat list, so a caller that is
        hunting for one entry stops at the page it appears on instead of
        paying for the whole feed. Any read that does not reach the end of the
        feed — transport failure, non-OK status, unparseable body, or the page
        budget running out — raises ``FeedReadError`` instead of returning
        short, so no caller can mistake "I stopped early" for "that was all".
        """
        headers, auth = self._get_auth()
        url = self._pr_url(ref, "/activities")
        where = f"{ref.owner}/{ref.repo}#{ref.number}"
        start = 0

        for _ in range(_MAX_PAGES):
            try:
                resp = self._session.get(
                    url,
                    params={"start": start, "limit": _PAGE_LIMIT},
                    headers=headers,
                    auth=auth,
                    timeout=_REQUEST_TIMEOUT,
                )
            except requests.RequestException as e:
                raise FeedReadError(
                    f"activity feed for {where} could not be read at "
                    f"start={start}: {e}"
                ) from e
            if not resp.ok:
                raise FeedReadError(
                    f"activity feed for {where} returned HTTP "
                    f"{resp.status_code} at start={start}"
                )
            try:
                page = resp.json()
            except ValueError as e:
                raise FeedReadError(
                    f"activity feed for {where} returned an unreadable body at "
                    f"start={start}: {e}"
                ) from e
            if not isinstance(page, dict):
                raise FeedReadError(
                    f"activity feed for {where} returned "
                    f"{type(page).__name__}, not a page object"
                )

            yield [
                item
                for item in (page.get("values") or [])
                if isinstance(item, dict)
                and (item.get("action") or "").upper() == "COMMENTED"
            ]

            if page.get("isLastPage", True):
                return
            next_start = page.get("nextPageStart")
            if next_start is None:
                return
            start = next_start

        raise FeedReadError(
            f"activity feed for {where} outran the {_MAX_PAGES}-page budget "
            f"({_MAX_PAGES * _PAGE_LIMIT} entries) without reaching the end"
        )

    def post_summary(self, ref: PRRef, body: str) -> None:
        """Post (or update) the top-level review summary comment.

        Data Center rejects a comment update that does not carry the comment's
        current ``version``, so the existing comment is looked up for its
        version rather than only its id.

        A ``FeedReadError`` from the lookup propagates rather than being
        swallowed: it used to be caught here and turned into ``existing =
        None``, which fell straight through to the POST and put a second
        summary on a PR that already had one.
        """
        headers, auth = self._get_auth()
        url = self._pr_url(ref, "/comments")
        body = with_summary_marker(body)

        existing: dict | None = None
        for entries in self._iter_activity_pages(ref):
            for item in entries:
                comment = item.get("comment") or {}
                if comment.get("anchor"):
                    continue
                if SUMMARY_MARKER in (comment.get("text") or ""):
                    existing = comment
                    break
            if existing is not None:
                break

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
            for entries in self._iter_activity_pages(ref):
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
        except FeedReadError as e:
            logger.warning(
                "activity feed read was incomplete for %s/%s#%s; thread dedup "
                "is working from the %d entries that were read: %s",
                ref.owner, ref.repo, ref.number, len(threads), e,
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
