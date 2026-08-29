"""GitHub forge implementation for PR metadata, diffs, comments, and threads."""
from __future__ import annotations

import os
import re
from collections.abc import Sequence
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from .base import InlineComment, PRData, PRRef, Thread

_PR_URL_RE = re.compile(
    r"^https?://([^/]+)/([^/]+)/([^/]+)/pull/(\d+)(?:[/#?].*)?$",
    re.IGNORECASE,
)
_SUMMARY_MARKER = "<!-- prxref-summary -->"


def _create_default_session() -> requests.Session:
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=True,
        # Read verbs only, deliberately. urllib3 retries beneath the
        # requests adapter, so a re-sent write is sent whole: a comment POST
        # that commits server-side and then loses its 2xx to a 502/504 or a
        # read timeout would be posted a second time, and the PR carries a
        # duplicate comment — the most visible failure this tool has. No
        # response status tells the client whether the origin processed the
        # request, and `Retry.is_retry` tests the method before it looks at
        # `status_forcelist`, so a single policy cannot retry a POST on 429
        # (which the server states it did not process) while holding it back
        # on 502. Writes are therefore left to the caller, which already logs
        # a failed post and carries on; a duplicated comment needs a human to
        # delete it. The other write verbs go with POST: no adapter issues a
        # DELETE, and the summary update (PUT, or PATCH on GitHub) is at best
        # a no-op on replay and at worst a version conflict. Connection
        # errors are still retried for every verb: urllib3 gates only its
        # read-error path on the method, and a connection that was never
        # established carried no write to duplicate.
        allowed_methods=frozenset(["GET", "HEAD", "OPTIONS"]),
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

    def post_summary(self, ref: PRRef, body: str) -> None:
        """Post (or update) the top-level review summary comment."""
        list_url = f"{self._api_base(ref)}/repos/{ref.owner}/{ref.repo}/issues/{ref.number}/comments"
        headers = self._headers(ref.host)

        existing_comment_id: int | None = None
        list_resp = self.session.get(list_url, headers=headers)
        if list_resp.ok:
            comments = list_resp.json()
            if isinstance(comments, list):
                for c in comments:
                    c_body = c.get("body") or ""
                    if _SUMMARY_MARKER in c_body:
                        existing_comment_id = c.get("id")
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
        """List existing threads so re-reviews skip already-discussed findings."""
        url = f"{self._api_base(ref)}/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/comments"
        headers = self._headers(ref.host)
        resp = self.session.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        threads: list[Thread] = []
        if isinstance(data, list):
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

        return threads
