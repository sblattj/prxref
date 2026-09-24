"""Bitbucket Cloud REST API v2 forge implementation."""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator, Sequence
from datetime import datetime
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter

from prxref.forges.base import (
    ATTRIBUTION_MARKER,
    SUMMARY_MARKER,
    DescriptionVersion,
    FeedReadError,
    InlineComment,
    PRData,
    PRHistory,
    PRRef,
    Thread,
    TitleRename,
    _require_aware,
    with_summary_marker,
)
from prxref.retry_logging import LoggingRetry

logger = logging.getLogger(__name__)

_API_BASE = "https://api.bitbucket.org/2.0"
_REQUEST_TIMEOUT = (10.0, 30.0)
_PAGE_SIZE = 100
# The comment walk used to stop at 5 pages, which silently truncated dedup at
# 500 comments. 50 puts the ceiling far past any real PR, and running out of
# budget is now a refusal to post rather than an invisible short read.
_MAX_PAGES = 50
# get_file_content is best-effort context, not the review itself: a body past
# this size (or one that looks binary) is worth skipping rather than shipping
# hundreds of KB into a worker prompt.
_MAX_FILE_CONTENT_BYTES = 512 * 1024
# A rejection body is operator-only diagnostics, never posted to the forge, but
# it is still bounded: Bitbucket's validation errors run long enough to bury
# the log line that carries them.
_ERROR_DETAIL_CHARS = 400


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

_BB_URL_RE = re.compile(
    r"^https?://bitbucket\.org/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?:pull-requests|pullrequests|pullrequest)/(?P<number>\d+)(?:/.*)?$",
    re.IGNORECASE,
)


def _make_retry_session() -> requests.Session:
    """Build a requests.Session with bounded retries for transient failures."""
    session = requests.Session()
    retry = LoggingRetry(
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
        # delete it. The other write verbs go with POST: DELETE (the prune
        # pass) is held back with them rather than special-cased for the
        # idempotency a replayed delete would enjoy, and the summary update
        # (PUT, or PATCH on GitHub) is at best a no-op on replay and at worst
        # a version conflict. Connection errors are still retried for every
        # verb: urllib3 gates only its read-error path on the method, and a
        # connection that was never established carried no write to
        # duplicate.
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

    def get_compare_diff(self, ref: PRRef, *, base_sha: str, head_sha: str) -> str:
        """Return the unified diff of ``head_sha`` against its merge-base with ``base_sha``.

        Bitbucket's diff spec is SOURCE..DEST, the reverse of git's order, so
        the range is spelled ``{head}..{base}``; swapping the two yields a
        different diff that still looks valid. ``topic=true`` selects the
        merge-base (three-dot) diff the PR itself shows. It is the default
        today, and it is sent explicitly because the result depends on it.
        An empty range comes back as ``""``. Raises on an HTTP or transport
        failure.
        """
        headers, auth = self._get_auth()
        headers["Accept"] = "text/plain"
        url = f"{_API_BASE}/repositories/{ref.owner}/{ref.repo}/diff/{head_sha}..{base_sha}"
        resp = self._session.get(
            url,
            headers=headers,
            auth=auth,
            params={"topic": "true"},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.text

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

    def get_file_content(self, ref: PRRef, path: str, *, sha: str) -> str | None:
        """Return the text of ``path`` at commit ``sha``, best-effort.

        Hits the repository-level ``/src`` endpoint directly rather than a
        pull-request-scoped one — this is a commit-addressed file read, not a
        PR resource. Never raises.
        """
        if not sha:
            return None
        headers, auth = self._get_auth()
        url = (
            f"{_API_BASE}/repositories/{ref.owner}/{ref.repo}/src/{sha}/"
            f"{quote(path, safe='/')}"
        )
        try:
            resp = self._session.get(
                url, headers=headers, auth=auth, timeout=_REQUEST_TIMEOUT
            )
        except requests.RequestException as e:
            logger.debug("get_file_content failed for %s@%s: %s", path, sha, e)
            return None
        if not resp.ok:
            logger.debug(
                "get_file_content got HTTP %s for %s@%s", resp.status_code, path, sha
            )
            return None
        content = resp.content
        if len(content) > _MAX_FILE_CONTENT_BYTES:
            logger.debug("get_file_content body over 512 KiB for %s@%s", path, sha)
            return None
        if b"\x00" in content:
            logger.debug("get_file_content body looked binary for %s@%s", path, sha)
            return None
        return content.decode("utf-8", errors="replace")

    def prune_inline_comments(self, ref: PRRef) -> int:
        """Delete prxref-attributed inline comments; returns the count removed.

        A re-review updates the summary in place, but the previous run's
        inline comments stayed standing — so a PR could carry an Approved
        summary above stale ERROR-severity comments from an earlier,
        nondeterministic run. Deleting our own comments first keeps what
        stands on the PR equal to the latest review.

        Only comments whose body carries the attribution marker are
        candidates, so a human's comment is never touched — and only inline
        ones (an ``inline`` anchor present): the summary is a top-level
        comment whose body carries the marker too, and is managed by
        ``post_summary``, not by this pass. A delete the token is not allowed
        to perform (403 from a different identity's comment) is logged and
        skipped, and a feed that cannot be read ends the prune with what it
        already removed: best-effort, because a cleanup must never abort the
        review that follows it.
        """
        headers, auth = self._get_auth()
        base = self._pr_url(ref, "/comments")
        removed = 0
        try:
            for comments in self._iter_comment_pages(ref):
                for comment in comments:
                    if not comment.get("inline"):
                        continue
                    raw = (comment.get("content") or {}).get("raw") or ""
                    if ATTRIBUTION_MARKER not in raw:
                        continue
                    comment_id = comment.get("id")
                    if comment_id is None:
                        continue
                    resp = self._session.delete(
                        f"{base}/{comment_id}",
                        headers=headers,
                        auth=auth,
                        timeout=_REQUEST_TIMEOUT,
                    )
                    if resp.ok:
                        removed += 1
                    else:
                        logger.warning(
                            "could not prune inline comment %s on %s/%s#%s "
                            "(HTTP %s): %s",
                            comment_id, ref.owner, ref.repo, ref.number,
                            resp.status_code, _response_detail(resp),
                        )
        except FeedReadError as e:
            logger.warning(
                "prune of inline comments on %s/%s#%s ended early after %d "
                "removals (best-effort): %s",
                ref.owner, ref.repo, ref.number, removed, e,
            )
        return removed

    def get_pr_history(self, ref: PRRef, *, head_sha: str | None = None) -> PRHistory:
        """Return the PR's description versions, title renames and replay cutoff inputs.

        Three reads, with the adapter's usual credentials: the pull request
        (``created_on``, the author, the live title and description, and the
        current head), its ``/activity`` feed, 50 entries a page for at most
        ``_MAX_PAGES`` pages, and ``/commit/{sha}``, whose ``date`` becomes
        ``head_committed_at`` for ``head_sha``, or for the PR's current head
        when ``head_sha`` is ``None``.

        A description edit is an ``update`` entry whose
        ``changes.description`` carries the full ``old`` and ``new`` texts,
        dated ``update.date``. Edits are ordered by that date, never by feed
        position, and the oldest edit's ``old`` is the original, dated
        ``created_on``. A title rename is read from ``changes.title`` in the
        same shape; that shape is inferred from the description entry and
        has not been checked against a live PR. A ``null`` text reads as
        ``""``, as a missing description does in ``get_pr``.

        The history comes back ``complete=False`` with no description
        versions and no renames, which pins nothing, whenever it cannot be
        trusted whole: the feed outran the page budget, so renames in the
        unread pages would be missing; a ``changes`` entry is not a pair of
        texts; the edits do not chain from each one's ``new`` to the next
        one's ``old`` and end at the live text; or an ``update`` entry's
        ``title`` or ``description`` snapshot is neither the text in force at
        its date nor the live text, which is how an edit missing from
        ``changes`` shows. At an edit's own date the snapshot may be either
        side of that edit. The live text is always accepted, so a snapshot
        that records the PR's current state rather than its state at the
        time can never veto the history ``changes`` records.

        ``first_review_at`` is the earliest approval, request for changes or
        comment by a ``user`` account other than the PR author, identified
        as ``list_threads`` identifies authors. Deleted comments, whose body
        is blanked, and prxref's own posts, whose body carries
        ``SUMMARY_MARKER`` or ``ATTRIBUTION_MARKER``, do not count. It is
        ``None`` when the PR's author cannot be identified, and when the feed
        outran the budget, because the earliest review may be on a page that
        was not read.

        Raises ``requests.HTTPError`` on a non-OK response, 401 and 403
        included; ``requests.RequestException`` on a transport failure; and
        ``ValueError`` on a body that is not a JSON object, or on a date that
        is missing, malformed or naive.
        """
        headers, auth = self._get_auth()
        pr = self._get_history_json(self._pr_url(ref), headers, auth)
        created_at = self._history_date(pr.get("created_on"), "pull request created_on")
        entries, read_to_end = self._read_activity(ref, headers, auth)
        head = head_sha or ((pr.get("source") or {}).get("commit") or {}).get("hash")
        head_committed_at = None
        if isinstance(head, str) and head:
            commit = self._get_history_json(
                f"{_API_BASE}/repositories/{ref.owner}/{ref.repo}/commit/{quote(head, safe='')}",
                headers,
                auth,
            )
            head_committed_at = self._history_date(commit.get("date"), "commit date")
        if not read_to_end:
            return PRHistory(created_at=created_at, head_committed_at=head_committed_at, complete=False)

        first_review_at = self._first_review_at(entries, pr.get("author"))
        live_title = pr.get("title") or ""
        live_description = pr.get("description") or ""
        descriptions = self._history_changes(entries, "description")
        titles = self._history_changes(entries, "title")
        trusted = (
            descriptions is not None
            and titles is not None
            and self._chains_to(descriptions, live_description)
            and self._chains_to(titles, live_title)
            and self._snapshots_agree(entries, "description", descriptions, live_description)
            and self._snapshots_agree(entries, "title", titles, live_title)
        )
        if not trusted:
            return PRHistory(
                created_at=created_at,
                first_review_at=first_review_at,
                head_committed_at=head_committed_at,
                complete=False,
            )

        versions: list[DescriptionVersion] = []
        if descriptions:
            first_edit_at, original, _ = descriptions[0]
            versions.append(DescriptionVersion(text=original, edited_at=min(created_at, first_edit_at)))
            versions.extend(DescriptionVersion(text=new, edited_at=at) for at, _, new in descriptions)
        return PRHistory(
            created_at=created_at,
            description_versions=tuple(versions),
            title_renames=tuple(
                TitleRename(previous_title=old, current_title=new, created_at=at) for at, old, new in titles
            ),
            first_review_at=first_review_at,
            head_committed_at=head_committed_at,
        )

    def _get_history_json(
        self,
        url: str,
        headers: dict[str, str],
        auth: tuple[str, str] | None,
        *,
        params: dict[str, int] | None = None,
    ) -> dict:
        """GET ``url`` for ``get_pr_history``; raise unless the response is OK and a JSON object."""
        resp = self._session.get(url, params=params, headers=headers, auth=auth, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"Bitbucket returned {type(data).__name__}, not an object, for {url}")
        return data

    def _read_activity(
        self, ref: PRRef, headers: dict[str, str], auth: tuple[str, str] | None,
    ) -> tuple[list[dict], bool]:
        """Return the PR's activity entries in feed order, and whether the feed was read to its end."""
        url: str | None = self._pr_url(ref, "/activity")
        params: dict[str, int] | None = {"pagelen": 50}
        entries: list[dict] = []
        for _ in range(_MAX_PAGES):
            page = self._get_history_json(url, headers, auth, params=params)
            entries.extend(value for value in page.get("values") or [] if isinstance(value, dict))
            url = page.get("next")
            params = None
            if not url:
                return entries, True
        return entries, False

    @classmethod
    def _history_changes(cls, entries: list[dict], field: str) -> list[tuple[datetime, str, str]] | None:
        """The feed's ``changes.<field>`` edits as ``(date, old, new)``, oldest first; ``None`` if one is unreadable."""
        changes: list[tuple[datetime, str, str]] = []
        for entry in reversed(entries):
            update = entry.get("update")
            if not isinstance(update, dict) or update.get("changes") is None:
                continue
            recorded = update["changes"]
            if not isinstance(recorded, dict):
                return None
            change = recorded.get(field)
            if change is None:
                continue
            if not isinstance(change, dict) or "old" not in change or "new" not in change:
                return None
            old, new = change["old"], change["new"]
            if not all(text is None or isinstance(text, str) for text in (old, new)):
                return None
            changes.append((cls._history_date(update.get("date"), "activity update date"), old or "", new or ""))
        return sorted(changes, key=lambda change: change[0])

    @staticmethod
    def _chains_to(changes: list[tuple[datetime, str, str]], live: str) -> bool:
        """Whether each edit starts from the text the previous one left, and the last one leaves ``live``."""
        linked = all(changes[i][2] == changes[i + 1][1] for i in range(len(changes) - 1))
        return linked and (not changes or changes[-1][2] == live)

    @classmethod
    def _snapshots_agree(
        cls, entries: list[dict], field: str, changes: list[tuple[datetime, str, str]], live: str,
    ) -> bool:
        """Whether each ``update.<field>`` snapshot is the live text, the text in force then, or an edit's old side."""
        original = changes[0][1] if changes else live
        for entry in entries:
            update = entry.get("update")
            if not isinstance(update, dict) or update.get(field) is None:
                continue
            snapshot = update[field]
            if not isinstance(snapshot, str):
                return False
            at = cls._history_date(update.get("date"), "activity update date")
            reached = [change for change in changes if change[0] <= at]
            allowed = {reached[-1][2] if reached else original, live}
            allowed.update(old for when, old, _ in changes if when == at)
            if snapshot not in allowed:
                return False
        return True

    @classmethod
    def _first_review_at(cls, entries: list[dict], author: object) -> datetime | None:
        """The earliest approval, request for changes or comment by a user other than ``author``, not prxref's."""
        author_key = cls._user_key(author)
        if not author_key:
            return None
        earliest: datetime | None = None
        for entry in entries:
            for kind, date_key in (("approval", "date"), ("changes_requested", "date"), ("comment", "created_on")):
                item = entry.get(kind)
                if not isinstance(item, dict):
                    continue
                user = item.get("user")
                user_key = cls._user_key(user)
                if not user_key or user_key == author_key or user.get("type", "user") != "user":
                    continue
                if kind == "comment":
                    body = (item.get("content") or {}).get("raw") or ""
                    if _is_deleted(item) or SUMMARY_MARKER in body or ATTRIBUTION_MARKER in body:
                        continue
                at = cls._history_date(item.get(date_key), f"activity {kind} {date_key}")
                if earliest is None or at < earliest:
                    earliest = at
        return earliest

    @staticmethod
    def _user_key(user: object) -> str:
        """A Bitbucket account's identity as ``list_threads`` records it: uuid, else nickname, else display name."""
        if not isinstance(user, dict):
            return ""
        return user.get("uuid") or user.get("nickname") or user.get("display_name") or ""

    @staticmethod
    def _history_date(value: object, name: str) -> datetime:
        """Parse a Bitbucket ISO 8601 time, raising ``ValueError`` naming ``name`` unless it is timezone-aware."""
        if not isinstance(value, str):
            raise ValueError(f"Bitbucket {name} is missing")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as e:
            raise ValueError(f"Bitbucket {name} is not an ISO 8601 time: {value!r}") from e
        _require_aware(parsed, f"Bitbucket {name}")
        return parsed


def _is_deleted(comment: dict) -> bool:
    """Whether Bitbucket has tombstoned this comment.

    A deleted comment stays in the feed with its body blanked. It counts as
    resolved for dedup, and it is not a slot ``post_summary`` may update into:
    the update would land somewhere nobody can read.
    """
    return bool(comment.get("deleted", False)) or comment.get("deleted_on") is not None
