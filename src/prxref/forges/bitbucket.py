"""Bitbucket Cloud REST API v2 forge implementation."""
from __future__ import annotations

import os
import re
from collections.abc import Sequence
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from prxref.forges.base import InlineComment, PRData, PRRef, Thread

_API_BASE = "https://api.bitbucket.org/2.0"
_REQUEST_TIMEOUT = (10.0, 30.0)

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

    def post_summary(self, ref: PRRef, body: str) -> None:
        """Post the top-level review summary comment."""
        headers, auth = self._get_auth()
        url = self._pr_url(ref, "/comments")
        payload = {"content": {"raw": body}}
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
        """List existing discussion threads on the PR."""
        headers, auth = self._get_auth()
        url: str | None = self._pr_url(ref, "/comments")
        params: dict[str, int | str] | None = {"pagelen": 100}
        threads: list[Thread] = []
        page_count = 0

        while url and page_count < 5:
            page_count += 1
            resp = self._session.get(
                url,
                params=params,
                headers=headers,
                auth=auth,
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("values", []):
                inline = item.get("inline")
                path = inline.get("path") if inline else None
                line = inline.get("to") if inline else None

                is_deleted = bool(item.get("deleted", False)) or item.get("deleted_on") is not None
                resolved = is_deleted

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
                        resolved=resolved,
                        author=author,
                        body_snippet=body_snippet,
                    )
                )

            url = data.get("next")
            params = None

        return threads
