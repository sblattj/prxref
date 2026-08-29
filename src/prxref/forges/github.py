"""GitHub forge implementation for PR metadata, diffs, comments, and threads."""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator, Sequence
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from .base import (
    SUMMARY_MARKER,
    FeedReadError,
    InlineComment,
    PRData,
    PRRef,
    Thread,
    with_summary_marker,
)

logger = logging.getLogger(__name__)

_PR_URL_RE = re.compile(
    r"^https?://([^/]+)/([^/]+)/([^/]+)/pull/(\d+)(?:[/#?].*)?$",
    re.IGNORECASE,
)
# Both comment reads used to go out unparameterised, which is GitHub's default
# page of 30 and no second page: a summary or a thread past the 30th comment
# did not exist as far as this adapter was concerned. 100 is the API maximum;
# 50 pages puts the ceiling far past any real PR, and running out of budget is
# a refusal to post rather than an invisible short read.
_PAGE_SIZE = 100
_MAX_PAGES = 50


def _create_default_session() -> requests.Session:
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=True,
        allowed_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "HEAD", "OPTIONS"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _get_token(host: str) -> str | None:
    if host.lower() != "github.com":
        token = os.environ.get("PRXREF_GITHUB_ENTERPRISE_TOKEN")
        if token:
            return token
    return os.environ.get("PRXREF_GITHUB_TOKEN")


class ForgeImpl:
    """GitHub and GitHub Enterprise forge implementation."""

    name: str = "github"

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or _create_default_session()

    @staticmethod
    def parse_pr_url(url: str) -> PRRef | None:
        """Return a PRRef if this forge recognizes the URL, else None."""
        m = _PR_URL_RE.match(url.strip())
        if not m:
            return None
        host, owner, repo, number_str = m.groups()
        return PRRef(
            forge="github",
            host=host,
            owner=owner,
            repo=repo,
            number=int(number_str),
            url=url,
        )

    def _api_base(self, ref: PRRef) -> str:
        if ref.host.lower() == "github.com":
            return "https://api.github.com"
        return f"https://{ref.host}/api/v3"

    def _headers(self, host: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
        }
        token = _get_token(host)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if extra:
            headers.update(extra)
        return headers

    def get_pr(self, ref: PRRef) -> PRData:
        """Fetch normalized PR metadata."""
        url = f"{self._api_base(ref)}/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}"
        resp = self.session.get(url, headers=self._headers(ref.host))
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()

        user = data.get("user") or {}
        head = data.get("head") or {}
        base = data.get("base") or {}

        return PRData(
            title=data.get("title", ""),
            description=data.get("body") or "",
            author=user.get("login", ""),
            source_branch=head.get("ref", ""),
            target_branch=base.get("ref", ""),
            source_sha=head.get("sha", ""),
            target_sha=base.get("sha", ""),
            raw=data,
        )

    def get_diff(self, ref: PRRef) -> str:
        """Fetch the raw unified diff of the PR (all files)."""
        url = f"{self._api_base(ref)}/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}"
        headers = self._headers(
            ref.host,
            {"Accept": "application/vnd.github.v3.diff, application/vnd.diff"},
        )
        resp = self.session.get(url, headers=headers)
        resp.raise_for_status()
        return resp.text

    def _iter_comment_pages(
        self, ref: PRRef, url: str, headers: dict[str, str]
    ) -> Iterator[list[dict]]:
        """Yield a comment listing one page at a time, oldest page first.

        A page at a time rather than one flat list, so a caller hunting for a
        single comment stops at the page it appears on instead of paying for
        the whole feed. Any read that does not reach the end — transport
        failure, non-OK status, unparseable body, or the page budget running
        out — raises ``FeedReadError`` instead of returning short, so no
        caller can mistake "I stopped early" for "that was all".

        A short page ends the walk. A page that comes back exactly full costs
        one extra request to confirm the end, which is the price of GitHub
        stating the total nowhere in the body.
        """
        where = f"{ref.owner}/{ref.repo}#{ref.number}"
        for page_number in range(1, _MAX_PAGES + 1):
            try:
                resp = self.session.get(
                    url,
                    headers=headers,
                    params={"per_page": _PAGE_SIZE, "page": page_number},
                )
            except requests.RequestException as e:
                raise FeedReadError(
                    f"comment feed for {where} could not be read at page "
                    f"{page_number}: {e}"
                ) from e
            if not resp.ok:
                raise FeedReadError(
                    f"comment feed for {where} returned HTTP "
                    f"{resp.status_code} at page {page_number}"
                )
            try:
                items = resp.json()
            except ValueError as e:
                raise FeedReadError(
                    f"comment feed for {where} returned an unreadable body at "
                    f"page {page_number}: {e}"
                ) from e
            if not isinstance(items, list):
                raise FeedReadError(
                    f"comment feed for {where} returned "
                    f"{type(items).__name__}, not a list of comments"
                )

            yield [item for item in items if isinstance(item, dict)]

            if len(items) < _PAGE_SIZE:
                return

        raise FeedReadError(
            f"comment feed for {where} outran the {_MAX_PAGES}-page budget "
            f"({_MAX_PAGES * _PAGE_SIZE} comments) without reaching the end"
        )

    def post_summary(self, ref: PRRef, body: str) -> None:
        """Post (or update) the top-level review summary comment.

        A ``FeedReadError`` from the lookup propagates. The old code branched
        on ``if list_resp.ok:`` with no else, so a rate-limited or otherwise
        failed listing fell straight through to the POST and put a second
        summary on a PR that already had one.
        """
        list_url = f"{self._api_base(ref)}/repos/{ref.owner}/{ref.repo}/issues/{ref.number}/comments"
        headers = self._headers(ref.host)
        body = with_summary_marker(body)

        existing_comment_id: int | None = None
        for comments in self._iter_comment_pages(ref, list_url, headers):
            for c in comments:
                if SUMMARY_MARKER in (c.get("body") or ""):
                    existing_comment_id = c.get("id")
                    break
            if existing_comment_id is not None:
                break

        if existing_comment_id is not None:
            patch_url = f"{self._api_base(ref)}/repos/{ref.owner}/{ref.repo}/issues/comments/{existing_comment_id}"
            resp = self.session.patch(patch_url, json={"body": body}, headers=headers)
            resp.raise_for_status()
        else:
            resp = self.session.post(list_url, json={"body": body}, headers=headers)
            resp.raise_for_status()

    def post_inline_comments(self, ref: PRRef, comments: Sequence[InlineComment]) -> int:
        """Post inline comments; returns the number actually posted."""
        url = f"{self._api_base(ref)}/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/comments"
        headers = self._headers(ref.host)
        posted = 0

        for comment in comments:
            payload = {
                "body": comment.body,
                "path": comment.path,
                "line": comment.line,
                "side": comment.side or "RIGHT",
            }
            resp = self.session.post(url, json=payload, headers=headers)
            if resp.status_code == 422:
                continue
            resp.raise_for_status()
            posted += 1

        return posted

    def list_threads(self, ref: PRRef) -> list[Thread]:
        """List existing threads so re-reviews skip already-discussed findings.

        Unlike ``post_summary`` this keeps whatever it managed to read: the
        threads only feed best-effort dedup, and the orchestrator substitutes
        an empty list for any exception, so raising would throw away pages
        that were read successfully. The shortfall is logged rather than
        swallowed — an under-read here shows up as findings re-posted on a
        re-review, with nothing in the output to explain why.
        """
        url = f"{self._api_base(ref)}/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/comments"
        headers = self._headers(ref.host)

        threads: list[Thread] = []
        try:
            for data in self._iter_comment_pages(ref, url, headers):
                for item in data:
                    path = item.get("path")
                    line = item.get("line") or item.get("original_line") or item.get("position")
                    user = item.get("user") or {}
                    author = user.get("login", "") if isinstance(user, dict) else ""
                    body = item.get("body") or ""
                    snippet = body[:120]
                    threads.append(
                        Thread(
                            path=path,
                            line=line,
                            resolved=False,
                            author=author,
                            body_snippet=snippet,
                        )
                    )
        except FeedReadError as e:
            logger.warning(
                "review-comment feed read was incomplete for %s/%s#%s; thread "
                "dedup is working from the %d comments that were read: %s",
                ref.owner, ref.repo, ref.number, len(threads), e,
            )

        return threads
