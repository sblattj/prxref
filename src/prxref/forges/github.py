"""GitHub forge implementation for PR metadata, diffs, comments, and threads."""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

from prxref.retry_logging import LoggingRetry
from prxref.triage import parse_unified_diff

from ._diff_render import render_diff_entries
from .base import (
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
    with_summary_marker,
)

logger = logging.getLogger(__name__)

_PR_URL_RE = re.compile(
    r"^https?://([^/]+)/([^/]+)/([^/]+)/pull/(\d+)(?:[/#?].*)?$",
    re.IGNORECASE,
)
# Connect and read deadlines, the same pair every other adapter passes. Without
# one a stalled connection blocks the call forever, and with it the review and
# the webhook worker running it.
_REQUEST_TIMEOUT = (10.0, 30.0)
# Both comment reads used to go out unparameterised, which is GitHub's default
# page of 30 and no second page: a summary or a thread past the 30th comment
# did not exist as far as this adapter was concerned. 100 is the API maximum;
# 50 pages puts the ceiling far past any real PR, and running out of budget is
# a refusal to post rather than an invisible short read.
_PAGE_SIZE = 100
_MAX_PAGES = 50
# A rejection body is operator-only diagnostics, never posted to the forge, but
# it is still bounded: GitHub's validation errors enumerate every unmatched
# subschema and run long enough to bury the log line that carries them.
_ERROR_DETAIL_CHARS = 400
# get_file_content is best-effort context, not the review itself: a body past
# this size (or one that looks binary) is worth skipping rather than shipping
# hundreds of KB into a worker prompt.
_MAX_FILE_CONTENT_BYTES = 512 * 1024
# The media types GitHub labels a raw file body with. github.com answers the raw
# Accept with ``application/vnd.github.raw+json``. The bare ``.raw`` and the
# versioned ``.v3.raw`` spellings are GitHub's older names for the same variant,
# accepted for GitHub Enterprise Server without a live sighting of either.
_RAW_MEDIA_TYPES = frozenset({
    "application/vnd.github.raw+json",
    "application/vnd.github.raw",
    "application/vnd.github.v3.raw",
    "application/vnd.github.v3.raw+json",
})


def _is_json_envelope(content_type: str) -> bool:
    """True when a contents response's ``Content-Type`` is a JSON envelope, not the file.

    The decision is on the media type (the value before any ``;``, stripped
    and lowercased), never on a substring: GitHub's raw variant carries
    ``+json`` in its name but its body is the file's own bytes. A raw variant
    in ``_RAW_MEDIA_TYPES`` is the file; ``application/json`` (a directory
    listing) and any other ``+json`` media type is an envelope. Anything else,
    ``text/*`` included, is the file, left to the size and binary checks.
    """
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type in _RAW_MEDIA_TYPES:
        return False
    return media_type == "application/json" or media_type.endswith("+json")


def _is_count(value: object) -> bool:
    """True for an int that is not a bool, the only shape a GitHub count takes."""
    return isinstance(value, int) and not isinstance(value, bool)


def _listed_count(entry: dict, key: str) -> int:
    """A files-listing entry's ``additions`` or ``deletions``, 0 when it is not a count."""
    value = entry.get(key)
    return value if _is_count(value) else 0


def _compare_mismatch(text: str, pr_raw: dict[str, Any]) -> str | None:
    """Why a compare diff cannot stand in for the PR's diff, or None when it can.

    The diff is parsed with the review pipeline's own parser and its file
    count and ``+``/``-`` line sums are held against the PR's
    ``changed_files``, ``additions`` and ``deletions``. The parser counts a
    binary file, a pure rename, a mode change and an empty file as one file of
    zero lines each, and a submodule bump as ``+1``/``-1``, as GitHub does.
    """
    expected = {key: pr_raw.get(key) for key in ("changed_files", "additions", "deletions")}
    missing = [key for key, value in expected.items() if not _is_count(value)]
    if missing:
        return f"the PR metadata carries no integer {', '.join(missing)} to check it against"
    files = parse_unified_diff(text)
    if len(files) != expected["changed_files"]:
        return (
            f"its file count {len(files)} differs from the PR's changed_files "
            f"{expected['changed_files']}"
        )
    added = sum(f.lines_added for f in files)
    removed = sum(f.lines_removed for f in files)
    if (added, removed) != (expected["additions"], expected["deletions"]):
        return (
            f"its line counts +{added}/-{removed} differ from the PR's "
            f"additions/deletions +{expected['additions']}/-{expected['deletions']}"
        )
    return None


def _too_large_message(body: dict) -> str:
    """GitHub's own ``message`` from a 406 ``too_large`` body, on one bounded line."""
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return "no message"
    return " ".join(message.split())[:_ERROR_DETAIL_CHARS]


def _refuse_short_totals(where: str, listed: list[dict], pr_raw: dict[str, Any]) -> None:
    """Raise ``ValueError`` when the listing's line totals differ from the PR's.

    The listing's ``additions`` and ``deletions`` are summed and held against
    the PR's own; PR metadata without integer totals raises too.
    """
    pr_additions, pr_deletions = pr_raw.get("additions"), pr_raw.get("deletions")
    if not (_is_count(pr_additions) and _is_count(pr_deletions)):
        raise ValueError(
            f"GitHub PR diff for {where} cannot be checked for completeness: "
            "the PR metadata carries no integer additions and deletions; "
            "refusing to review a diff that may be partial"
        )
    additions = sum(_listed_count(f, "additions") for f in listed)
    deletions = sum(_listed_count(f, "deletions") for f in listed)
    if (additions, deletions) != (pr_additions, pr_deletions):
        raise ValueError(
            f"GitHub PR diff for {where} cannot be rebuilt whole: the "
            f"changed-file listing totals +{additions}/-{deletions} lines but "
            f"the PR has +{pr_additions}/-{pr_deletions}; refusing to review a "
            "partial diff"
        )


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
    session = requests.Session()
    retry_strategy = LoggingRetry(
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
        # delete it. The other write verbs go with POST: DELETE (the prune
        # pass) is held back with them rather than special-cased for the
        # idempotency a replayed delete would enjoy, and the summary update
        # (PUT, or PATCH on GitHub) is at best a no-op on replay and at worst
        # a version conflict. Connection
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
        resp = self.session.get(
            url, headers=self._headers(ref.host), timeout=_REQUEST_TIMEOUT
        )
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
        """Fetch the raw unified diff of the PR (all files).

        Under GitHub's diff limits this is one GET with the diff media type,
        and its body is returned as-is. Past 20,000 lines or 300 files GitHub
        refuses that request with HTTP 406 and a JSON ``errors`` entry whose
        ``code`` is ``too_large``. Exactly that answer is logged at DEBUG with
        GitHub's own ``message``, which names the limit hit, and the diff is
        read by ``_get_diff_past_the_limit``: the compare diff of the PR's
        base and head, else the changed-file listing. Its ``ValueError`` (a
        diff that cannot be read whole), ``FeedReadError`` (a listing page
        that cannot be read) or failed PR metadata read propagates. Any other
        406, including one whose body is not JSON, and every other non-OK
        status raise ``HTTPError`` as before.
        """
        url = f"{self._api_base(ref)}/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}"
        headers = self._headers(
            ref.host,
            {"Accept": "application/vnd.github.v3.diff, application/vnd.diff"},
        )
        resp = self.session.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
        if resp.status_code == 406:
            try:
                body = resp.json()
            except ValueError:
                body = None
            errors = body.get("errors") if isinstance(body, dict) else None
            if isinstance(errors, list) and any(
                isinstance(e, dict) and e.get("code") == "too_large" for e in errors
            ):
                logger.debug(
                    "diff for %s/%s#%s was refused by GitHub as too large "
                    "(406 too_large: %s); reading it from the compare endpoint",
                    ref.owner, ref.repo, ref.number, _too_large_message(body),
                )
                return self._get_diff_past_the_limit(ref)
        resp.raise_for_status()
        return resp.text

    def _get_diff_past_the_limit(self, ref: PRRef) -> str:
        """Read the diff of a PR past GitHub's diff limits (20,000 lines or 300 files).

        The PR is fetched once, and a failed read raises as ``get_pr`` does.
        Its ``base.sha`` and ``head.sha`` then name the compare diff
        (``get_compare_diff``), which applies neither limit. That text is
        returned as-is, logged at DEBUG only, when its file count and line
        sums equal the PR's ``changed_files``, ``additions`` and
        ``deletions``. Otherwise, and on any HTTP or transport failure of the
        compare read, one WARNING names the reason and the diff is rebuilt
        from the changed-file listing by ``_get_diff_from_files`` with the
        same PR metadata; nothing the compare read raises propagates.
        """
        pr = self.get_pr(ref)
        where = f"{ref.owner}/{ref.repo}#{ref.number}"
        if not (pr.target_sha and pr.source_sha):
            reason = "the PR metadata carries no base.sha and head.sha"
        else:
            try:
                text = self.get_compare_diff(
                    ref, base_sha=pr.target_sha, head_sha=pr.source_sha,
                )
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else "error"
                reason = f"it answered HTTP {status}"
            except requests.RequestException as e:
                reason = f"it could not be read: {type(e).__name__}: {e}"
            else:
                mismatch = _compare_mismatch(text, pr.raw)
                if mismatch is None:
                    logger.debug(
                        "diff for %s read from compare %s...%s; its file and "
                        "line counts match the PR's",
                        where, pr.target_sha[:12], pr.source_sha[:12],
                    )
                    return text
                reason = mismatch
        logger.warning(
            "GitHub PR diff for %s: the compare diff was not used (%s); "
            "rebuilding it from the changed-file listing",
            where, reason,
        )
        return self._get_diff_from_files(ref, pr)

    def get_compare_diff(self, ref: PRRef, *, base_sha: str, head_sha: str) -> str:
        """Return the unified diff of ``head_sha`` against its merge-base with ``base_sha``.

        Uses the compare endpoint with three dots, ``{base}...{head}``, which
        diffs from the merge-base exactly as the PR's own diff does; GitHub
        answers the two-dot spelling with a 404. The diff media type makes the
        body the raw diff text rather than the JSON comparison. An empty range
        (``head_sha`` already merged into ``base_sha``) comes back as ``""``,
        returned unmodified. Raises on an HTTP or transport failure.
        """
        url = (
            f"{self._api_base(ref)}/repos/{ref.owner}/{ref.repo}"
            f"/compare/{base_sha}...{head_sha}"
        )
        headers = self._headers(ref.host, {"Accept": "application/vnd.github.diff"})
        resp = self.session.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text

    def _get_diff_from_files(self, ref: PRRef, pr: PRData) -> str:
        """Rebuild the PR's unified diff from its ``/pulls/{n}/files`` listing.

        The fallback for a diff past the diff media type's limit whose compare
        diff could not be used; ``pr`` is the PR metadata already read for
        it. Each listed file is mapped into the renderer's per-file entry:
        ``filename`` is the new path, ``previous_filename`` (else
        ``filename``) the old path, and the ``added``, ``removed`` and
        ``renamed`` statuses pick the new, deleted and rename headers; every
        other status renders plain ``---``/``+++``. ``patch`` supplies the
        hunks. A file listed without a ``patch`` renders header-only: with no
        changed lines (an empty file, a pure rename, a binary, a mode change)
        that is logged at DEBUG, and with changed lines, where GitHub withheld
        the patch but kept the file's true counts (seen on whole-file lockfile
        adds and removes), one WARNING names the file and those counts.

        Completeness is checked, not assumed, and every miss raises
        ``ValueError`` instead of returning a partial diff: a listing shorter
        than the PR's ``changed_files`` (GitHub caps it at 3,000 files), PR
        metadata carrying no integer ``changed_files``, and listing
        ``additions`` or ``deletions`` totals that differ from the PR's, or PR
        metadata carrying no integer totals. The totals are what catch a large
        listing page: GitHub has been seen to drop the ``patch`` of ordinary
        text files from such a page and list them with zero changed lines. A
        failed listing read raises ``FeedReadError``.
        """
        url = (
            f"{self._api_base(ref)}/repos/{ref.owner}/{ref.repo}"
            f"/pulls/{ref.number}/files"
        )
        listed: list[dict] = []
        for page in self._iter_pages(
            ref, url, self._headers(ref.host), what="changed-file listing"
        ):
            listed.extend(page)

        where = f"{ref.owner}/{ref.repo}#{ref.number}"
        changed_files = pr.raw.get("changed_files")
        if not isinstance(changed_files, int) or isinstance(changed_files, bool):
            raise ValueError(
                f"GitHub PR diff for {where} cannot be checked for completeness: "
                "the PR metadata carries no integer changed_files; refusing to "
                "review a diff that may be partial"
            )
        if len(listed) < changed_files:
            raise ValueError(
                f"GitHub PR diff for {where} cannot be rebuilt whole: "
                f"changed_files={changed_files} but the changed-file listing "
                f"returned {len(listed)} (GitHub caps the listing at 3,000 "
                "files); refusing to review a partial diff"
            )
        _refuse_short_totals(where, listed, pr.raw)

        entries: list[dict] = []
        for f in listed:
            filename = f.get("filename") or ""
            status = f.get("status")
            patch = f.get("patch")
            added, removed = _listed_count(f, "additions"), _listed_count(f, "deletions")
            if not patch and (added or removed):
                logger.warning(
                    "GitHub PR diff for %s: GitHub withheld the patch of %s "
                    "(+%d/-%d); it is reviewed as header-only",
                    where, filename, added, removed,
                )
            elif not patch:
                logger.debug(
                    "GitHub PR diff: %s is listed with no patch and no changed "
                    "lines; it is reviewed as header-only",
                    filename,
                )
            entries.append({
                "old_path": f.get("previous_filename") or filename,
                "new_path": filename,
                "new_file": status == "added",
                "deleted_file": status == "removed",
                "renamed_file": status == "renamed",
                "diff": patch,
            })
        return render_diff_entries(entries)

    def _iter_pages(
        self,
        ref: PRRef,
        url: str,
        headers: dict[str, str],
        *,
        what: str,
        extra_params: dict[str, str] | None = None,
    ) -> Iterator[list[dict]]:
        """Yield a paginated GitHub collection one page at a time, oldest page first.

        Every request carries ``per_page`` and ``page``; ``extra_params`` is
        merged over them. ``what`` names the collection in every error, so a
        failed read says which listing it was reading.

        A page at a time rather than one flat list, so a caller hunting for a
        single entry stops at the page it appears on instead of paying for
        the whole listing. Any read that does not reach the end — transport
        failure, non-OK status, unparseable body, or the page budget running
        out — raises ``FeedReadError`` instead of returning short, so no
        caller can mistake "I stopped early" for "that was all".

        A short page ends the walk. A page that comes back exactly full costs
        one extra request to confirm the end, which is the price of GitHub
        stating the total nowhere in the body.
        """
        where = f"{ref.owner}/{ref.repo}#{ref.number}"
        for page_number in range(1, _MAX_PAGES + 1):
            params: dict[str, int | str] = {
                "per_page": _PAGE_SIZE,
                "page": page_number,
            }
            if extra_params:
                params.update(extra_params)
            try:
                resp = self.session.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=_REQUEST_TIMEOUT,
                )
            except requests.RequestException as e:
                raise FeedReadError(
                    f"{what} for {where} could not be read at page "
                    f"{page_number}: {e}"
                ) from e
            if not resp.ok:
                raise FeedReadError(
                    f"{what} for {where} returned HTTP "
                    f"{resp.status_code} at page {page_number}"
                )
            try:
                items = resp.json()
            except ValueError as e:
                raise FeedReadError(
                    f"{what} for {where} returned an unreadable body at "
                    f"page {page_number}: {e}"
                ) from e
            if not isinstance(items, list):
                raise FeedReadError(
                    f"{what} for {where} returned "
                    f"{type(items).__name__}, not a list"
                )

            yield [item for item in items if isinstance(item, dict)]

            if len(items) < _PAGE_SIZE:
                return

        raise FeedReadError(
            f"{what} for {where} outran the {_MAX_PAGES}-page budget "
            f"({_MAX_PAGES * _PAGE_SIZE} entries) without reaching the end"
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
        for comments in self._iter_pages(ref, list_url, headers, what="comment feed"):
            for c in comments:
                if SUMMARY_MARKER in (c.get("body") or ""):
                    existing_comment_id = c.get("id")
                    break
            if existing_comment_id is not None:
                break

        if existing_comment_id is not None:
            patch_url = f"{self._api_base(ref)}/repos/{ref.owner}/{ref.repo}/issues/comments/{existing_comment_id}"
            resp = self.session.patch(
                patch_url, json={"body": body}, headers=headers,
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
        else:
            resp = self.session.post(
                list_url, json={"body": body}, headers=headers,
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()

    def post_inline_comments(self, ref: PRRef, comments: Sequence[InlineComment]) -> int:
        """Post inline comments; returns the number actually posted.

        ``commit_id`` is required: without it GitHub rejects the whole payload
        as matching no subschema, and reports ``line`` itself as an
        unpermitted key, so every comment 422s rather than only the ones whose
        line falls outside the diff. The head SHA is read from ``get_pr`` the
        way the GitLab adapter reads its own position SHAs.
        """
        if not comments:
            return 0

        url = f"{self._api_base(ref)}/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/comments"
        headers = self._headers(ref.host)
        commit_id = self.get_pr(ref).source_sha
        posted = 0

        for comment in comments:
            payload = {
                "body": comment.body,
                "path": comment.path,
                "line": comment.line,
                "side": comment.side or "RIGHT",
                "commit_id": commit_id,
            }
            resp = self.session.post(
                url, json=payload, headers=headers, timeout=_REQUEST_TIMEOUT
            )
            if resp.status_code == 422:
                # A line outside the diff is the expected 422 and skipping it
                # is correct, but the same status covers a malformed payload
                # that would skip every comment. Log the body so the two are
                # distinguishable instead of both reading as "0 posted".
                logger.warning(
                    "GitHub rejected an inline comment on %s:%s (422): %s",
                    comment.path, comment.line, _response_detail(resp),
                )
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
            for data in self._iter_pages(ref, url, headers, what="comment feed"):
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
                "dedup is working from the %d threads recovered so far: %s",
                ref.owner, ref.repo, ref.number, len(threads), e,
            )

        return threads

    def get_file_content(self, ref: PRRef, path: str, *, sha: str) -> str | None:
        """Return the text of ``path`` at commit ``sha``, best-effort.

        Requests the raw media type so a 2xx response is the file's bytes
        rather than a base64-wrapped JSON envelope. The response is judged by
        its media type (``_is_json_envelope``): github.com labels the raw body
        ``application/vnd.github.raw+json``, and that, GitHub's older raw
        names and any ``text/*`` type are read as the file. A JSON envelope
        (``path`` names a directory, or the raw Accept was not honoured) reads
        as "no content". A body over ``_MAX_FILE_CONTENT_BYTES`` is dropped by
        the size check and one holding a NUL byte by the binary check. Never
        raises.
        """
        if not sha:
            return None
        url = (
            f"{self._api_base(ref)}/repos/{ref.owner}/{ref.repo}/contents/"
            f"{quote(path, safe='/')}"
        )
        headers = self._headers(ref.host, {"Accept": "application/vnd.github.raw+json"})
        try:
            resp = self.session.get(
                url, headers=headers, params={"ref": sha}, timeout=_REQUEST_TIMEOUT
            )
        except requests.RequestException as e:
            logger.debug("get_file_content failed for %s@%s: %s", path, sha, e)
            return None
        if not resp.ok:
            logger.debug(
                "get_file_content got HTTP %s for %s@%s", resp.status_code, path, sha
            )
            return None
        content_type = resp.headers.get("Content-Type") or ""
        if _is_json_envelope(content_type):
            logger.debug(
                "get_file_content got a JSON envelope (%s) for %s@%s (a "
                "directory, or the raw Accept not honoured)", content_type, path, sha,
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
        candidates, so a human's comment is never touched. A delete the token
        is not allowed to perform (403 from a different identity's comment)
        is logged and skipped, and a feed that cannot be read ends the prune
        with what it already removed: best-effort, because a cleanup must
        never abort the review that follows it.
        """
        list_url = f"{self._api_base(ref)}/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/comments"
        headers = self._headers(ref.host)
        removed = 0
        try:
            for comments in self._iter_pages(ref, list_url, headers, what="comment feed"):
                for comment in comments:
                    if ATTRIBUTION_MARKER not in (comment.get("body") or ""):
                        continue
                    comment_id = comment.get("id")
                    if comment_id is None:
                        continue
                    # The delete route lives outside the pull-number
                    # namespace — /pulls/comments/{id}, not /pulls/N/
                    # comments/{id} — so the listing URL and the delete URL
                    # differ in shape, and the listing-shaped one 404s for
                    # every comment.
                    delete_url = (
                        f"{self._api_base(ref)}/repos/{ref.owner}/{ref.repo}"
                        f"/pulls/comments/{comment_id}"
                    )
                    resp = self.session.delete(
                        delete_url, headers=headers, timeout=_REQUEST_TIMEOUT
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
                "prune of review comments on %s/%s#%s ended early after %d "
                "removals (best-effort): %s",
                ref.owner, ref.repo, ref.number, removed, e,
            )
        return removed

    _PR_HISTORY_QUERY = """
query PrxrefPRHistory(
  $owner: String!, $repo: String!, $number: Int!, $pageSize: Int!,
  $head: GitObjectID, $byHead: Boolean!, $byLastCommit: Boolean!,
  $withEdits: Boolean!, $editsAfter: String,
  $withRenames: Boolean!, $renamesAfter: String,
  $withReviews: Boolean!, $reviewsAfter: String,
  $withComments: Boolean!, $commentsAfter: String
) {
  repository(owner: $owner, name: $repo) {
    object(oid: $head) @include(if: $byHead) {
      ... on Commit { committedDate }
    }
    pullRequest(number: $number) {
      createdAt
      author { __typename login }
      commits(last: 1) @include(if: $byLastCommit) {
        nodes { commit { oid committedDate } }
      }
      userContentEdits(first: $pageSize, after: $editsAfter) @include(if: $withEdits) {
        pageInfo { hasNextPage endCursor }
        nodes { editedAt deletedAt diff }
      }
      timelineItems(itemTypes: [RENAMED_TITLE_EVENT], first: $pageSize, after: $renamesAfter)
        @include(if: $withRenames) {
        filteredCount
        pageInfo { hasNextPage endCursor }
        nodes { ... on RenamedTitleEvent { createdAt previousTitle currentTitle } }
      }
      reviews(first: $pageSize, after: $reviewsAfter) @include(if: $withReviews) {
        pageInfo { hasNextPage endCursor }
        nodes {
          submittedAt
          body
          author { __typename login }
          comments(first: 1) { nodes { body } }
        }
      }
      comments(first: $pageSize, after: $commentsAfter) @include(if: $withComments) {
        pageInfo { hasNextPage endCursor }
        nodes { createdAt body author { __typename login } }
      }
    }
  }
}
"""

    _PR_HISTORY_CONNECTIONS: tuple[tuple[str, str, str], ...] = (
        ("userContentEdits", "withEdits", "editsAfter"),
        ("timelineItems", "withRenames", "renamesAfter"),
        ("reviews", "withReviews", "reviewsAfter"),
        ("comments", "withComments", "commentsAfter"),
    )

    def get_pr_history(self, ref: PRRef, *, head_sha: str | None = None) -> PRHistory:
        """Return the PR's description versions, title renames and replay cutoff inputs.

        Reads GitHub's GraphQL API, which has no anonymous access: without a
        token from the same lookup the REST calls use, it raises
        ``FeedReadError`` before any request is made. The endpoint is
        ``https://api.github.com/graphql`` for github.com and
        ``https://{host}/api/graphql`` for an Enterprise Server host; the
        Enterprise path follows GitHub's documented layout and has not been
        probed against a live instance.

        Each POST carries one query that reads the next page of every
        connection still open: ``userContentEdits`` (one node per description
        version, newest first, the original included once the body has been
        edited), the ``RENAMED_TITLE_EVENT`` timeline, ``reviews`` and the
        conversation ``comments``. The first POST also reads ``createdAt``,
        the PR author and the head commit date: ``committedDate`` of
        ``head_sha`` when one is given, else of the PR's last commit. At most
        ``_MAX_PAGES`` POSTs are made.

        ``first_review_at`` is the earliest submitted review or conversation
        comment written by a ``User`` other than the PR author and carrying
        neither ``ATTRIBUTION_MARKER`` nor ``SUMMARY_MARKER``; a review counts
        as prxref's own when its body or its first inline comment carries one.

        When the budget runs out with description versions still unread, the
        history is returned with ``complete=False`` and the newest versions
        read. When the title renames, the reviews or the comments could not
        all be read, it is returned with ``complete=False`` and no description
        versions, which pins nothing; ``first_review_at`` is then ``None`` if
        the reviews or comments were the ones cut short.

        Raises ``FeedReadError`` on a missing token, a transport failure, a
        non-OK status (401 and 403 included), an unreadable body, a 200 whose
        body carries a GraphQL ``errors`` array, a PR the response does not
        contain, or a timestamp that is missing, malformed or naive.
        """
        where = f"{ref.owner}/{ref.repo}#{ref.number}"
        headers = self._headers(ref.host)
        if "Authorization" not in headers:
            env_names = (
                "PRXREF_GITHUB_TOKEN"
                if ref.host.lower() == "github.com"
                else "PRXREF_GITHUB_ENTERPRISE_TOKEN or PRXREF_GITHUB_TOKEN"
            )
            raise FeedReadError(
                f"PR history for {where} needs a token: GitHub's GraphQL API "
                f"refuses anonymous reads; set {env_names}"
            )

        cursors: dict[str, str | None] = {field: None for field, _, _ in self._PR_HISTORY_CONNECTIONS}
        nodes: dict[str, list[dict]] = {field: [] for field in cursors}
        open_fields = set(cursors)
        renames_expected: object = None

        repository = self._post_pr_history_page(
            ref, headers, self._pr_history_variables(ref, head_sha, cursors, open_fields, first_page=True),
        )
        pr = repository["pullRequest"]
        created_at = self._history_time(pr.get("createdAt"), "createdAt", where)
        author = pr.get("author")
        pr_author = author.get("login") if isinstance(author, dict) else None
        head_committed_at = self._head_committed_at(repository, pr, head_sha, where)
        pages = 1
        while True:
            for field in sorted(open_fields):
                connection = pr.get(field)
                if not isinstance(connection, dict):
                    raise FeedReadError(f"PR history for {where} returned no {field} connection")
                nodes[field].extend(node for node in connection.get("nodes") or () if isinstance(node, dict))
                if field == "timelineItems":
                    renames_expected = connection.get("filteredCount")
                page_info = connection.get("pageInfo") or {}
                if not page_info.get("hasNextPage"):
                    open_fields.discard(field)
                    continue
                cursor = page_info.get("endCursor")
                if not isinstance(cursor, str) or not cursor:
                    raise FeedReadError(f"PR history for {where} reported more {field} but no endCursor")
                cursors[field] = cursor
            if not open_fields or pages >= _MAX_PAGES:
                break
            repository = self._post_pr_history_page(
                ref, headers, self._pr_history_variables(ref, head_sha, cursors, open_fields, first_page=False),
            )
            pr = repository["pullRequest"]
            pages += 1

        versions = tuple(self._description_versions(nodes["userContentEdits"], where))
        renames, renames_readable = self._title_renames(nodes["timelineItems"], where)
        first_review_at = self._first_review_at(nodes["reviews"], nodes["comments"], pr_author, where)
        renames_short = (
            "timelineItems" in open_fields
            or not renames_readable
            or (isinstance(renames_expected, int) and len(renames) < renames_expected)
        )
        feedback_short = "reviews" in open_fields or "comments" in open_fields
        if renames_short or feedback_short:
            logger.debug(
                "PR history for %s is incomplete after %d page(s) (renames short: %s, reviews or "
                "comments short: %s); it pins nothing",
                where, pages, renames_short, feedback_short,
            )
            return PRHistory(
                created_at=created_at,
                title_renames=renames,
                first_review_at=None if feedback_short else first_review_at,
                head_committed_at=head_committed_at,
                complete=False,
            )
        if "userContentEdits" in open_fields:
            logger.debug(
                "PR history for %s holds the newest %d description versions after %d page(s); older ones "
                "were not read",
                where, len(versions), pages,
            )
        return PRHistory(
            created_at=created_at,
            description_versions=versions,
            title_renames=renames,
            first_review_at=first_review_at,
            head_committed_at=head_committed_at,
            complete="userContentEdits" not in open_fields,
        )

    def _graphql_url(self, ref: PRRef) -> str:
        if ref.host.lower() == "github.com":
            return "https://api.github.com/graphql"
        return f"https://{ref.host}/api/graphql"

    def _pr_history_variables(
        self,
        ref: PRRef,
        head_sha: str | None,
        cursors: dict[str, str | None],
        open_fields: set[str],
        *,
        first_page: bool,
    ) -> dict[str, Any]:
        variables: dict[str, Any] = {
            "owner": ref.owner,
            "repo": ref.repo,
            "number": ref.number,
            "pageSize": _PAGE_SIZE,
            "head": head_sha if first_page else None,
            "byHead": first_page and head_sha is not None,
            "byLastCommit": first_page and head_sha is None,
        }
        for field, include_name, after_name in self._PR_HISTORY_CONNECTIONS:
            variables[include_name] = field in open_fields
            variables[after_name] = cursors[field]
        return variables

    def _post_pr_history_page(
        self, ref: PRRef, headers: dict[str, str], variables: dict[str, Any],
    ) -> dict[str, Any]:
        where = f"{ref.owner}/{ref.repo}#{ref.number}"
        try:
            resp = self.session.post(
                self._graphql_url(ref),
                json={"query": self._PR_HISTORY_QUERY, "variables": variables},
                headers=headers,
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            raise FeedReadError(f"PR history for {where} could not be read: {e}") from e
        if not resp.ok:
            raise FeedReadError(
                f"PR history for {where} returned HTTP {resp.status_code}: {_response_detail(resp)}"
            )
        try:
            body = resp.json()
        except ValueError as e:
            raise FeedReadError(f"PR history for {where} returned an unreadable body: {e}") from e
        if not isinstance(body, dict):
            raise FeedReadError(f"PR history for {where} returned {type(body).__name__}, not an object")
        if body.get("errors"):
            raise FeedReadError(
                f"PR history for {where} returned GraphQL errors: {_response_detail(resp)}"
            )
        repository = (body.get("data") or {}).get("repository")
        if not isinstance(repository, dict) or not isinstance(repository.get("pullRequest"), dict):
            raise FeedReadError(f"PR history for {where} returned no pull request")
        return repository

    @staticmethod
    def _history_time(value: object, what: str, where: str) -> datetime:
        if not isinstance(value, str):
            raise FeedReadError(f"PR history for {where} carries no {what} timestamp")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as e:
            raise FeedReadError(f"PR history for {where} carries a malformed {what} {value!r}") from e
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise FeedReadError(f"PR history for {where} carries a naive {what} {value!r}")
        return parsed

    def _head_committed_at(
        self, repository: dict[str, Any], pr: dict[str, Any], head_sha: str | None, where: str,
    ) -> datetime | None:
        if head_sha is not None:
            commit = repository.get("object")
        else:
            last = ((pr.get("commits") or {}).get("nodes") or [None])[-1]
            commit = last.get("commit") if isinstance(last, dict) else None
        if not isinstance(commit, dict) or commit.get("committedDate") is None:
            return None
        return self._history_time(commit.get("committedDate"), "committedDate", where)

    def _description_versions(self, edits: list[dict], where: str) -> Iterator[DescriptionVersion]:
        for edit in edits:
            edited_at = self._history_time(edit.get("editedAt"), "userContentEdits.editedAt", where)
            diff = edit.get("diff")
            text = diff if isinstance(diff, str) and edit.get("deletedAt") is None else None
            yield DescriptionVersion(text=text, edited_at=edited_at)

    def _title_renames(self, events: list[dict], where: str) -> tuple[tuple[TitleRename, ...], bool]:
        renames: list[TitleRename] = []
        readable = True
        for event in events:
            previous_title = event.get("previousTitle")
            current_title = event.get("currentTitle")
            if not isinstance(previous_title, str) or not isinstance(current_title, str):
                readable = False
                continue
            created_at = self._history_time(event.get("createdAt"), "RenamedTitleEvent.createdAt", where)
            renames.append(TitleRename(previous_title, current_title, created_at))
        return tuple(renames), readable

    def _first_review_at(
        self, reviews: list[dict], comments: list[dict], pr_author: str | None, where: str,
    ) -> datetime | None:
        times: list[datetime] = []
        for review in reviews:
            submitted_at = review.get("submittedAt")
            if submitted_at is None:
                continue
            inline = (review.get("comments") or {}).get("nodes") or ()
            bodies = [review.get("body"), *(c.get("body") for c in inline if isinstance(c, dict))]
            if self._is_human_post(review.get("author"), bodies, pr_author):
                times.append(self._history_time(submitted_at, "reviews.submittedAt", where))
        for comment in comments:
            if self._is_human_post(comment.get("author"), [comment.get("body")], pr_author):
                times.append(self._history_time(comment.get("createdAt"), "comments.createdAt", where))
        return min(times, default=None)

    @staticmethod
    def _is_human_post(author: object, bodies: list[object], pr_author: str | None) -> bool:
        if not isinstance(author, dict) or author.get("__typename") != "User":
            return False
        if pr_author is not None and author.get("login") == pr_author:
            return False
        return not any(
            isinstance(body, str) and (ATTRIBUTION_MARKER in body or SUMMARY_MARKER in body)
            for body in bodies
        )
