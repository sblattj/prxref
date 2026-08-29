"""GitLab REST API v4 forge implementation."""
from __future__ import annotations

import os
import re
from collections.abc import Sequence
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from prxref.forges.base import InlineComment, PRData, PRRef, Thread

_REQUEST_TIMEOUT = (10.0, 30.0)
_SUMMARY_MARKER = "<!-- prxref-summary -->"

_GL_URL_RE = re.compile(
    r"^https?://(?P<host>[^/]+)/(?P<path>.+?)/-/merge_requests/(?P<number>\d+)(?:/.*)?$",
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
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_DEFAULT_SESSION = _make_retry_session()


class ForgeImpl:
    """GitLab Forge adapter."""

    name: str = "gitlab"

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

        if not parsed.scheme or not parsed.netloc:
            return None

        match = _GL_URL_RE.match(url)
        if not match:
            return None

        host = match.group("host")
        raw_path = match.group("path").strip("/")
        number = int(match.group("number"))

        segments = [s for s in raw_path.split("/") if s]
        if not segments:
            return None

        owner = segments[0]
        repo = segments[-1]
        project_path = "/".join(segments)
        normalized_url = f"https://{host}/{project_path}/-/merge_requests/{number}"

        return PRRef(
            forge="gitlab",
            host=host,
            owner=owner,
            repo=repo,
            number=number,
            url=normalized_url,
        )

    def _get_auth_headers(self) -> dict[str, str]:
        """Read GitLab auth token from environment."""
        token = os.environ.get("PRXREF_GITLAB_TOKEN")
        if token:
            return {"PRIVATE-TOKEN": token}
        return {}

    def _project_path(self, ref: PRRef) -> str:
        """Extract full project path from normalized PRRef URL."""
        match = _GL_URL_RE.match(ref.url)
        if match:
            return match.group("path").strip("/")
        return f"{ref.owner}/{ref.repo}"

    def _api_base(self, ref: PRRef) -> str:
        """Construct the base API URL for the host and project."""
        project_path = self._project_path(ref)
        encoded_project = quote(project_path, safe="")
        return f"https://{ref.host}/api/v4/projects/{encoded_project}"

    def get_pr(self, ref: PRRef) -> PRData:
        """Fetch normalized PR metadata."""
        headers = self._get_auth_headers()
        base = self._api_base(ref)
        url = f"{base}/merge_requests/{ref.number}"

        resp = self._session.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        title = data.get("title") or ""
        description = data.get("description") or ""
        author_info = data.get("author") or {}
        author = (
            author_info.get("username")
            or author_info.get("name")
            or ""
        )

        source_branch = data.get("source_branch") or ""
        target_branch = data.get("target_branch") or ""
        source_sha = data.get("sha") or ""

        target_sha = ""
        diff_refs = data.get("diff_refs") or {}
        if diff_refs.get("base_sha"):
            target_sha = diff_refs["base_sha"]
        elif target_branch:
            try:
                branch_url = f"{base}/repository/branches/{quote(target_branch, safe='')}"
                b_resp = self._session.get(branch_url, headers=headers, timeout=_REQUEST_TIMEOUT)
                if b_resp.ok:
                    b_data = b_resp.json()
                    commit = b_data.get("commit") or {}
                    target_sha = commit.get("id") or ""
            except requests.RequestException:
                target_sha = ""

        return PRData(
            title=title,
            description=description,
            author=author,
            source_branch=source_branch,
            target_branch=target_branch,
            source_sha=source_sha,
            target_sha=target_sha,
            raw=data,
        )

    def get_diff(self, ref: PRRef) -> str:
        """Fetch the raw unified diff of the PR (all files)."""
        headers = self._get_auth_headers()
        base = self._api_base(ref)
        url = f"{base}/merge_requests/{ref.number}/diffs"
        params = {"access_raw_diffs": "true"}

        resp = self._session.get(url, headers=headers, params=params, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        diffs = resp.json()

        if not diffs:
            raise ValueError(
                f"Empty diff received from GitLab for {ref.owner}/{ref.repo}#{ref.number}"
            )

        diff_parts: list[str] = []
        for d in diffs:
            old_path = d.get("old_path") or ""
            new_path = d.get("new_path") or ""
            new_file = d.get("new_file", False)
            deleted_file = d.get("deleted_file", False)
            renamed_file = d.get("renamed_file", False)
            raw_diff = d.get("diff") or ""

            header_lines = [f"diff --git a/{old_path} b/{new_path}"]
            if new_file:
                header_lines.append("new file mode 100644")
                header_lines.append("--- /dev/null")
                header_lines.append(f"+++ b/{new_path}")
            elif deleted_file:
                header_lines.append("deleted file mode 100644")
                header_lines.append(f"--- a/{old_path}")
                header_lines.append("+++ /dev/null")
            elif renamed_file:
                header_lines.append(f"rename from {old_path}")
                header_lines.append(f"rename to {new_path}")
                header_lines.append(f"--- a/{old_path}")
                header_lines.append(f"+++ b/{new_path}")
            else:
                header_lines.append(f"--- a/{old_path}")
                header_lines.append(f"+++ b/{new_path}")

            file_unified = "\n".join(header_lines)
            if raw_diff:
                if not raw_diff.startswith("\n"):
                    file_unified += "\n"
                file_unified += raw_diff
                if not file_unified.endswith("\n"):
                    file_unified += "\n"
            else:
                file_unified += "\n"

            diff_parts.append(file_unified)

        return "".join(diff_parts)

    def post_summary(self, ref: PRRef, body: str) -> None:
        """Post (or update) the top-level review summary comment."""
        headers = self._get_auth_headers()
        base = self._api_base(ref)
        notes_url = f"{base}/merge_requests/{ref.number}/notes"

        existing_note_id: int | None = None
        try:
            list_resp = self._session.get(
                notes_url,
                headers=headers,
                params={"per_page": 50},
                timeout=_REQUEST_TIMEOUT,
            )
            if list_resp.ok:
                notes = list_resp.json()
                for note in notes:
                    if _SUMMARY_MARKER in (note.get("body") or ""):
                        existing_note_id = note.get("id")
                        break
        except requests.RequestException:
            pass

        if existing_note_id is not None:
            update_url = f"{notes_url}/{existing_note_id}"
            resp = self._session.put(
                update_url,
                headers=headers,
                json={"body": body},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
        else:
            resp = self._session.post(
                notes_url,
                headers=headers,
                json={"body": body},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()

    def post_inline_comments(self, ref: PRRef, comments: Sequence[InlineComment]) -> int:
        """Post inline comments; returns the number actually posted."""
        if not comments:
            return 0

        headers = self._get_auth_headers()
        base = self._api_base(ref)
        disc_url = f"{base}/merge_requests/{ref.number}/discussions"
        notes_url = f"{base}/merge_requests/{ref.number}/notes"

        pr_data = self.get_pr(ref)
        raw = pr_data.raw
        diff_refs = raw.get("diff_refs") or {}
        base_sha = diff_refs.get("base_sha") or pr_data.target_sha
        start_sha = diff_refs.get("start_sha") or pr_data.target_sha
        head_sha = diff_refs.get("head_sha") or pr_data.source_sha

        posted = 0
        for comment in comments:
            payload = {
                "body": comment.body,
                "position": {
                    "base_sha": base_sha,
                    "start_sha": start_sha,
                    "head_sha": head_sha,
                    "position_type": "text",
                    "new_path": comment.path,
                    "new_line": comment.line,
                },
            }
            try:
                resp = self._session.post(
                    disc_url,
                    json=payload,
                    headers=headers,
                    timeout=_REQUEST_TIMEOUT,
                )
                if 200 <= resp.status_code < 300:
                    posted += 1
                elif resp.status_code == 400:
                    fallback_body = f"file: {comment.path}\n\n{comment.body}"
                    fb_resp = self._session.post(
                        notes_url,
                        json={"body": fallback_body},
                        headers=headers,
                        timeout=_REQUEST_TIMEOUT,
                    )
                    if 200 <= fb_resp.status_code < 300:
                        posted += 1
            except requests.RequestException:
                continue

        return posted

    def list_threads(self, ref: PRRef) -> list[Thread]:
        """List existing discussion threads on the PR."""
        headers = self._get_auth_headers()
        base = self._api_base(ref)
        url = f"{base}/merge_requests/{ref.number}/discussions"
        params = {"per_page": 50}

        threads: list[Thread] = []
        try:
            resp = self._session.get(url, headers=headers, params=params, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            discussions = resp.json()
        except requests.RequestException:
            return threads

        for disc in discussions:
            notes = disc.get("notes") or []
            if not notes:
                continue

            disc_resolved = False
            if "resolved" in disc:
                disc_resolved = bool(disc["resolved"])

            first_path: str | None = None
            first_line: int | None = None

            for note in notes:
                pos = note.get("position")
                if pos and isinstance(pos, dict):
                    if not first_path:
                        first_path = pos.get("new_path") or pos.get("old_path")
                    if first_line is None:
                        first_line = pos.get("new_line") or pos.get("old_line")
                if "resolved" in note and not disc_resolved:
                    disc_resolved = bool(note["resolved"])

            for note in notes:
                author_info = note.get("author") or {}
                author = author_info.get("username") or author_info.get("name") or ""
                body = note.get("body") or ""
                note_resolved = note.get("resolved", disc_resolved)

                threads.append(
                    Thread(
                        path=first_path,
                        line=first_line,
                        resolved=bool(note_resolved),
                        author=author,
                        body_snippet=body[:200],
                    )
                )

        return threads
