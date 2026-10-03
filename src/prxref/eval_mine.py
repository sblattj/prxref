"""``prxref eval mine``: build an eval dataset from a GitHub repo's merged pull requests (issue #81).

The human review comments left on merged PRs are the labels. ``mine`` walks
the most recently merged PRs of ``--repo``, keeps those with at least
``--min-comments`` qualifying comments, and writes into ``--out``:

- ``cases.json``: one case per (PR, reviewed commit), loadable by
  :func:`prxref.eval_cases.load_cases`. ``head_sha`` is the commit the
  comments were left on and ``base_sha`` its merge base with the PR's base
  branch, so ``prxref eval run`` replays exactly the range a reviewer saw.
- ``mine.json``: the provenance, which ``cases.json`` cannot hold because the
  loader refuses unknown fields, including a sha256 of ``cases.json``.
- ``severity-review.md``: the drafted severities for a human to confirm.

A qualifying comment is a thread root on a line, by a non-bot who is not the
PR's author. Its replies are appended to its label text. A label is
``accepted`` when the PR's final head changed the file within
:data:`ACCEPT_WINDOW` lines of the comment after the commented commit,
``False`` when the file was not changed afterwards, and ``None`` when that
cannot be told (an unreachable commit, a withheld patch). When the history
was rewritten (the commented commit is not an ancestor of the final head, as
after a force-push), the PR's own change is compared instead: the lines the
PR added within :data:`ACCEPT_WINDOW` lines of the comment at the commented
commit, against the added lines of the PR's final diff (``pulls/{n}/files``,
read at most once per PR and only when needed). One of them gone, or the
file no longer in the PR, is ``True``; all still there ``False``; no added
line near the comment, or a withheld patch, ``None``.
Severities are drafted by one single-shot judge call per case with
``--judge-model``, else ``warning``. ``--rehash DIR`` recomputes the hash
after a human has edited ``cases.json``.

``--until`` bounds the merge date from above. Candidates then come from the
GitHub search API (``is:pr is:merged merged:<since|*>..<until>``) rather than
the pulls walk; search reaches only the 1000 newest-created hits of a query,
so a wide window logs a warning and wants a narrower ``--since``. ``--pr``
mines exactly the listed PRs. ``--reviewers maintainers`` keeps only thread
roots whose ``author_association`` is OWNER, MEMBER or COLLABORATOR. A PR
whose base branch was later renamed is still mined: the merge base is looked
up against ``base.ref``, then the repo's default branch, then ``base.sha``.

GitHub only. Authentication, the API base for a GitHub Enterprise host,
paging and retries are the GitHub forge's own.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

import prxref
from prxref import reviewer
from prxref.config import load_config
from prxref.eval_cases import HUMAN_SEVERITIES, load_cases
from prxref.eval_judge import JUDGE_MODEL_FLAG, build_judge_client
from prxref.forges import github
from prxref.forges.base import FeedReadError, PRRef
from prxref.judge import split_judge_prompt
from prxref.llm import ConfigError

logger = logging.getLogger(__name__)

MINE_VERSION = 1
MINE_PROMPT_NAME = "mine_severity"
DEFAULT_SEVERITY = "warning"
ACCEPT_WINDOW = 3
DEFAULT_PRS = 50
DEFAULT_MIN_COMMENTS = 1
DEFAULT_HOST = "github.com"
SOURCE_JUDGE = "judge"
SOURCE_DEFAULT = "default"
SOURCE_JUDGE_ERROR = "judge_error"
REVIEW_TEXT_CHARS = 120
_COMPARE_FILE_CAP = 300
_PR_FILES_PAGE = 100
_PR_FILES_CAP = 3000
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
REVIEWERS_CHOICES = ("any", "maintainers")
SEARCH_PAGE_SIZE = 100
SEARCH_RESULT_CAP = 1000
_FALLBACK_STATUSES = (404, 422)
_PR_STATE_PARAMS = {"state": "closed", "sort": "updated", "direction": "desc"}
RATE_LIMIT_STATUSES = (403, 429)
RATE_LIMIT_MARGIN_S = 2
RATE_LIMIT_MAX_WAIT_S = 3700
RATE_LIMIT_MAX_WAITS = 3
STOPPED_RATE_LIMIT = "rate_limit"
_sleep = time.sleep
_now = time.time


class _MineSkip(Exception):
    """A pull request or case that cannot be mined and is skipped with a warning."""


class _RateLimited(Exception):
    """GitHub's rate limit answered and no bounded wait clears it; the walk stops and writes what it has."""


def _header(resp: Any, name: str) -> str | None:
    headers = getattr(resp, "headers", None)
    if not isinstance(headers, Mapping):
        return None
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value).strip()
    return None


def _is_rate_limited(resp: Any) -> bool:
    """A 403 or 429 that carries ``X-RateLimit-Remaining: 0``, a ``Retry-After``, or a "rate limit" body."""
    if getattr(resp, "status_code", None) not in RATE_LIMIT_STATUSES:
        return False
    if _header(resp, "X-RateLimit-Remaining") == "0" or _header(resp, "Retry-After") is not None:
        return True
    text = getattr(resp, "text", "")
    return isinstance(text, str) and "rate limit" in text.lower()


def _rate_limit_wait(resp: Any) -> float | None:
    """Seconds to wait out a rate limit, from ``Retry-After`` or ``X-RateLimit-Reset``; ``None`` when unknown."""
    retry = _header(resp, "Retry-After")
    if retry is not None:
        try:
            return max(0.0, float(retry))
        except ValueError:
            pass
    reset = _header(resp, "X-RateLimit-Reset")
    if reset is not None:
        try:
            return max(0.0, float(reset) - _now()) + RATE_LIMIT_MARGIN_S
        except ValueError:
            pass
    return None


class _WaitingSession:
    """Wraps a session so every GET waits out a GitHub rate limit and retries the same request.

    The shared session's retry policy has no 403 (GitHub's rate-limit
    status) and serves the review path too, so the wait lives here, on the
    mining path only. :class:`_RateLimited` is raised when the wait cannot be
    told, exceeds :data:`RATE_LIMIT_MAX_WAIT_S`, or recurs
    :data:`RATE_LIMIT_MAX_WAITS` times for one request.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def get(self, *args: Any, **kwargs: Any) -> Any:
        waits = 0
        while True:
            resp = self._inner.get(*args, **kwargs)
            if not _is_rate_limited(resp):
                return resp
            wait = _rate_limit_wait(resp)
            if wait is None or wait > RATE_LIMIT_MAX_WAIT_S or waits >= RATE_LIMIT_MAX_WAITS:
                raise _RateLimited(
                    f"HTTP {resp.status_code} rate limit, "
                    + ("no reset time given" if wait is None else f"wait of {math.ceil(wait)} s not taken")
                )
            waits += 1
            logger.warning("GitHub rate limit reached; waiting %d s until it resets", math.ceil(wait))
            _sleep(wait)


@dataclass
class _Label:
    comment_id: int
    file: str
    line: int
    text: str
    url: str
    commit: str
    accepted: bool | None = None
    severity: str = DEFAULT_SEVERITY
    source: str = SOURCE_DEFAULT

    @property
    def id(self) -> str:
        return f"c{self.comment_id}"


@dataclass
class _Case:
    id: str
    pr_url: str
    base_sha: str
    head_sha: str
    labels: list[_Label] = field(default_factory=list)


@dataclass
class _PR:
    number: int
    merged_at: str
    cases: list[_Case] = field(default_factory=list)


def _cfg_flag(name: str, message: str) -> ConfigError:
    return ConfigError(f"{name}: {message}")


def _validate(args: Any) -> None:
    """Refuse a flag combination ``mine`` cannot run, naming the flag."""
    rehash = getattr(args, "rehash", None)
    if rehash:
        for flag, dest in (("--repo", "repo"), ("--out", "out")):
            if getattr(args, dest, None):
                raise _cfg_flag("--rehash", f"cannot be combined with {flag}")
        return
    if getattr(args, "allow_unconfirmed", False):
        raise _cfg_flag("--allow-unconfirmed", "only applies to --rehash")
    repo = getattr(args, "repo", None)
    if not repo:
        raise _cfg_flag("--repo", "required (OWNER/NAME of the GitHub repository to mine)")
    if not _REPO_RE.fullmatch(repo):
        raise _cfg_flag("--repo", f"must be OWNER/NAME, got {repo!r}")
    if not getattr(args, "out", None):
        raise _cfg_flag("--out", "required (the directory to write cases.json, mine.json and severity-review.md)")
    if getattr(args, "since", None) and _parse_since(args.since) is None:
        raise _cfg_flag("--since", f"must be a date like 2026-01-31, got {args.since!r}")
    until_text = getattr(args, "until", None)
    if until_text and _parse_since(until_text) is None:
        raise _cfg_flag("--until", f"must be a date like 2026-01-31, got {until_text!r}")
    if until_text and getattr(args, "since", None) and _parse_since(until_text) < _parse_since(args.since):
        raise _cfg_flag("--until", f"must not be earlier than --since ({args.since}), got {until_text!r}")
    reviewers = getattr(args, "reviewers", None) or "any"
    if reviewers not in REVIEWERS_CHOICES:
        raise _cfg_flag("--reviewers", f"must be one of {', '.join(REVIEWERS_CHOICES)}, got {reviewers!r}")
    if getattr(args, "pr", None) is not None:
        _parse_pr_numbers(args.pr)
        for flag, dest in (("--since", "since"), ("--until", "until")):
            if getattr(args, dest, None):
                raise _cfg_flag("--pr", f"cannot be combined with {flag}")
    for flag, dest in (("--prs", "prs"), ("--min-comments", "min_comments")):
        value = getattr(args, dest, None)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise _cfg_flag(flag, f"must be an integer of at least 1, got {value!r}")
    model = getattr(args, "judge_model", None)
    if model is not None and (not model.strip() or "," in model):
        raise _cfg_flag(JUDGE_MODEL_FLAG, f"must name one model, got {model!r}")


def _parse_since(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_pr_numbers(value: Any) -> list[int] | None:
    """The distinct positive PR numbers of a ``--pr 1,2,3`` value in order; ``None`` when unset."""
    if value is None:
        return None
    numbers: list[int] = []
    for part in str(value).split(","):
        text = part.strip()
        if not text.isascii() or not text.isdigit() or int(text) < 1:
            raise _cfg_flag("--pr", f"must be comma-separated positive integers, got {value!r}")
        if int(text) not in numbers:
            numbers.append(int(text))
    return numbers


def _check_out(out: str) -> Path:
    path = Path(out)
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise _cfg_flag("--out", f"{str(path)!r} exists and is not an empty directory; refusing to overwrite it")
    return path


def _is_bot(user: Any) -> bool:
    if not isinstance(user, dict):
        return True
    login = str(user.get("login") or "")
    return user.get("type") == "Bot" or login.lower().endswith("[bot]") or not login


def _get(forge: github.ForgeImpl, ref: PRRef, url: str, *, params: Mapping[str, str] | None = None) -> Any:
    resp = forge.session.get(
        url, headers=forge._headers(ref.host), params=dict(params or {}), timeout=github._REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _list(forge: github.ForgeImpl, ref: PRRef, url: str, what: str,
          params: Mapping[str, str] | None = None) -> Iterator[list[dict]]:
    yield from forge._iter_pages(ref, url, forge._headers(ref.host), what=what, extra_params=dict(params or {}))


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _candidate_prs(forge: github.ForgeImpl, ref: PRRef, since: date | None, wanted: int,
                   qualifies: Any) -> list[dict]:
    """Walk closed PRs newest-updated first; return up to ``wanted`` qualifying merged PRs, newest merge first.

    GitHub sorts by update time, and a merge is never later than the last
    update, so once ``wanted`` qualifying PRs are held and a page's oldest
    update predates the ``wanted``-th newest merge, no later page can improve
    the set. With ``since`` set, a page whose newest update predates it ends
    the walk.
    """
    base = f"{forge._api_base(ref)}/repos/{ref.owner}/{ref.repo}/pulls"
    kept: list[dict] = []
    seen = 0
    cutoff = datetime(since.year, since.month, since.day, tzinfo=UTC) if since else None
    try:
        for page in _list(forge, ref, base, "merged pull requests", _PR_STATE_PARAMS):
            seen += len(page)
            updates = [t for t in (_parse_time(pr.get("updated_at")) for pr in page) if t is not None]
            for pr in page:
                merged = _parse_time(pr.get("merged_at"))
                if merged is None or (cutoff is not None and merged < cutoff):
                    continue
                if qualifies(pr):
                    kept.append(pr)
            kept.sort(key=lambda pr: pr["merged_at"], reverse=True)
            if cutoff is not None and updates and max(updates) < cutoff:
                break
            if len(kept) >= wanted and updates:
                nth = _parse_time(kept[wanted - 1]["merged_at"])
                if nth is not None and min(updates) < nth:
                    break
    except (FeedReadError, requests.RequestException) as exc:
        if not seen:
            raise ConfigError(
                f"--repo: cannot list the pull requests of {ref.owner}/{ref.repo} on {ref.host}: {exc}"
            ) from exc
        logger.warning("listing pull requests of %s/%s stopped early: %s", ref.owner, ref.repo, exc)
    return kept[:wanted]


def _http_status(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _fetch_pr(forge: github.ForgeImpl, ref: PRRef, number: int) -> dict | None:
    """One PR by number, or ``None`` with a warning when it cannot be read or is not merged."""
    url = f"{forge._api_base(ref)}/repos/{ref.owner}/{ref.repo}/pulls/{number}"
    try:
        pr = _get(forge, ref, url)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("%s/%s#%s: skipped: cannot read the pull request: %s", ref.owner, ref.repo, number, exc)
        return None
    if not isinstance(pr, dict) or _parse_time(pr.get("merged_at")) is None:
        logger.warning("%s/%s#%s: skipped: not a merged pull request", ref.owner, ref.repo, number)
        return None
    return pr


def _search_prs(forge: github.ForgeImpl, ref: PRRef, since: date | None, until: date, wanted: int,
                qualifies: Any) -> list[dict]:
    """Page the GitHub search API for PRs merged in ``[since, until]``; keep up to ``wanted`` qualifying ones.

    Search returns issues, so each hit is fetched as a full PR before
    ``qualifies`` sees it. Search is ordered by creation, newest first, and
    reaches at most :data:`SEARCH_RESULT_CAP` hits. Search has its own rate
    limit; it is waited out like any other (see :class:`_WaitingSession`). A
    403 or 429 that is no rate limit stops the walk with what was kept.
    """
    url = f"{forge._api_base(ref)}/search/issues"
    query = (f"repo:{ref.owner}/{ref.repo} is:pr is:merged "
             f"merged:{since.isoformat() if since else '*'}..{until.isoformat()}")
    kept: list[dict] = []
    seen = 0
    max_pages = SEARCH_RESULT_CAP // SEARCH_PAGE_SIZE
    for page_number in range(1, max_pages + 1):
        params = {"q": query, "sort": "created", "order": "desc", "per_page": str(SEARCH_PAGE_SIZE),
                  "page": str(page_number)}
        try:
            body = _get(forge, ref, url, params=params)
            items = body.get("items") if isinstance(body, dict) else None
            if not isinstance(items, list):
                raise ValueError("search answer has no items array")
        except (requests.RequestException, ValueError) as exc:
            if _http_status(exc) in (403, 429):
                logger.warning("searching pull requests of %s/%s was refused and stopped: %s",
                               ref.owner, ref.repo, exc)
                break
            if not seen:
                raise ConfigError(
                    f"--repo: cannot search the pull requests of {ref.owner}/{ref.repo} on {ref.host}: {exc}"
                ) from exc
            logger.warning("searching pull requests of %s/%s stopped early: %s", ref.owner, ref.repo, exc)
            break
        total = body.get("total_count")
        if page_number == 1 and isinstance(total, int) and total > SEARCH_RESULT_CAP:
            logger.warning(
                "%s/%s: %d pull requests match the window, but search reaches only the %d newest-created; "
                "narrow the window with a later --since to cover the rest",
                ref.owner, ref.repo, total, SEARCH_RESULT_CAP,
            )
        seen += len(items)
        for item in items:
            number = github._as_int(item.get("number")) if isinstance(item, dict) else None
            if number is None:
                continue
            pr = _fetch_pr(forge, ref, number)
            if pr is not None and qualifies(pr):
                kept.append(pr)
                if len(kept) >= wanted:
                    break
        if len(kept) >= wanted or len(items) < SEARCH_PAGE_SIZE:
            break
    kept.sort(key=lambda pr: pr["merged_at"], reverse=True)
    return kept[:wanted]


def _listed_prs(forge: github.ForgeImpl, ref: PRRef, numbers: Sequence[int], qualifies: Any) -> list[dict]:
    """The listed PRs that are merged and qualify, newest merge first."""
    kept = []
    for number in numbers:
        pr = _fetch_pr(forge, ref, number)
        if pr is not None and qualifies(pr):
            kept.append(pr)
    kept.sort(key=lambda pr: pr["merged_at"], reverse=True)
    return kept


def _qualifying_labels(pr: dict, comments: Sequence[dict], reviewers: str = "any") -> list[_Label]:
    """The thread-root comments on a line by a human other than the PR's author, replies appended.

    With ``reviewers == "maintainers"`` a root counts only when its
    ``author_association`` is in :data:`MAINTAINER_ASSOCIATIONS`.
    """
    author = str((pr.get("user") or {}).get("login") or "")
    replies: dict[int, list[dict]] = {}
    for item in comments:
        parent = github._as_int(item.get("in_reply_to_id"))
        if parent is not None:
            replies.setdefault(parent, []).append(item)
    labels: list[_Label] = []
    for item in comments:
        comment_id = github._as_int(item.get("id"))
        if comment_id is None or github._as_int(item.get("in_reply_to_id")) is not None:
            continue
        if _is_bot(item.get("user")) or (item.get("user") or {}).get("login") == author:
            continue
        if reviewers == "maintainers" and item.get("author_association") not in MAINTAINER_ASSOCIATIONS:
            continue
        line = github._as_int(item.get("original_line"))
        path, commit = item.get("path"), item.get("original_commit_id")
        body = str(item.get("body") or "").strip()
        if line is None or not isinstance(path, str) or not isinstance(commit, str) or not body:
            continue
        text = body
        for reply in sorted(replies.get(comment_id, ()), key=lambda r: str(r.get("created_at") or "")):
            reply_body = str(reply.get("body") or "").strip()
            if reply_body:
                login = (reply.get("user") or {}).get("login") or "unknown"
                text += f"\n\nReply from {login}: {reply_body}"
        labels.append(_Label(
            comment_id=comment_id, file=path, line=line, text=text,
            url=str(item.get("html_url") or ""), commit=commit.lower(),
        ))
    return labels


def _changed_positions(patch: str) -> list[float]:
    """Old-side positions a unified-diff patch touches: removed lines, and insertions as half-lines."""
    positions: list[float] = []
    old_no = 0
    in_hunk = False
    for raw in patch.splitlines():
        hunk = _HUNK_RE.match(raw)
        if hunk:
            old_no, in_hunk = int(hunk.group(1)), True
            continue
        if not in_hunk or raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            positions.append(float(old_no))
            old_no += 1
        elif raw.startswith("+"):
            positions.append(old_no - 0.5)
        else:
            old_no += 1
    return positions


def _accepted(line: int, file: str, comparison: Mapping[str, Any] | None, commit: str) -> bool | None:
    """Whether the PR's later history changed ``file`` near ``line``; ``None`` when it cannot be told."""
    if comparison is None:
        return None
    merge_base = (comparison.get("merge_base_commit") or {}).get("sha")
    if isinstance(merge_base, str) and merge_base.lower() != commit:
        return None
    files = comparison.get("files")
    if not isinstance(files, list):
        return None
    if len(files) >= _COMPARE_FILE_CAP:
        return None
    for entry in files:
        if not isinstance(entry, dict):
            continue
        names = {entry.get("filename"), entry.get("previous_filename")}
        if file not in names:
            continue
        if entry.get("status") in ("renamed", "removed"):
            return None if entry.get("status") == "renamed" else True
        patch = entry.get("patch")
        if not isinstance(patch, str) or not patch:
            return None
        return any(abs(p - line) <= ACCEPT_WINDOW for p in _changed_positions(patch))
    return False


def _rewritten(comparison: Mapping[str, Any] | None, commit: str) -> bool:
    """Whether ``comparison`` shows ``commit`` is not an ancestor of the final head (a rewritten history)."""
    if comparison is None:
        return False
    merge_base = (comparison.get("merge_base_commit") or {}).get("sha")
    return isinstance(merge_base, str) and merge_base.lower() != commit


def _added_lines(patch: str) -> list[tuple[int, str]]:
    """New-side line number and whitespace-stripped text of each ``+`` line of a unified-diff patch."""
    added: list[tuple[int, str]] = []
    new_no = 0
    in_hunk = False
    for raw in patch.splitlines():
        hunk = _HUNK_RE.match(raw)
        if hunk:
            new_no, in_hunk = int(hunk.group(3)), True
            continue
        if not in_hunk or raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            added.append((new_no, raw[1:].strip()))
            new_no += 1
        elif not raw.startswith("-"):
            new_no += 1
    return added


def _file_entry(files: Any, file: str) -> dict | None:
    """The entry of ``files`` whose ``filename`` or ``previous_filename`` is ``file``."""
    if not isinstance(files, list):
        return None
    for entry in files:
        if isinstance(entry, dict) and file in (entry.get("filename"), entry.get("previous_filename")):
            return entry
    return None


def _accepted_by_content(line: int, commented: Mapping[str, Any] | None,
                         final: Mapping[str, Any] | None) -> bool | None:
    """Whether a comment was acted on, read from the PR's own change when its history was rewritten.

    ``commented`` is the file's entry in the PR's diff at the commented
    commit (``None`` when absent) and ``final`` its entry in the PR's final
    diff (``None`` when the PR no longer touches the file, which is ``True``).
    The non-blank lines the PR added within :data:`ACCEPT_WINDOW` lines of
    ``line`` (new side) are compared, whitespace-stripped, with the final
    diff's added lines: one missing is ``True``, all present ``False``. No
    such line, or a withheld patch on either side, is ``None``.
    """
    patch = (commented or {}).get("patch")
    if not isinstance(patch, str) or not patch:
        return None
    if final is None:
        return True
    final_patch = final.get("patch")
    if not isinstance(final_patch, str) or not final_patch:
        return None
    near = {text for number, text in _added_lines(patch) if text and abs(number - line) <= ACCEPT_WINDOW}
    if not near:
        return None
    kept = {text for _, text in _added_lines(final_patch)}
    return not near <= kept


def _pr_files(forge: github.ForgeImpl, ref: PRRef, number: int) -> list[dict]:
    """The PR's final diff, one entry per file, read 100 a page up to GitHub's cap of 3000 files."""
    url = f"{forge._api_base(ref)}/repos/{ref.owner}/{ref.repo}/pulls/{number}/files"
    files: list[dict] = []
    for page in range(1, _PR_FILES_CAP // _PR_FILES_PAGE + 1):
        data = _get(forge, ref, url, params={"per_page": str(_PR_FILES_PAGE), "page": str(page)})
        if not isinstance(data, list):
            raise ValueError(f"pull request files returned {type(data).__name__}, not a list")
        files.extend(item for item in data if isinstance(item, dict))
        if len(data) < _PR_FILES_PAGE:
            break
    return files


def _compare(forge: github.ForgeImpl, ref: PRRef, left: str, right: str) -> dict:
    url = f"{forge._api_base(ref)}/repos/{ref.owner}/{ref.repo}/compare/{quote(left, safe='')}...{right}"
    data = _get(forge, ref, url)
    if not isinstance(data, dict):
        raise _MineSkip(f"compare {left}...{right} returned {type(data).__name__}, not an object")
    return data


class _Bases:
    """Where to measure a merge base: ``base.ref``, then the repo's default branch, then ``base.sha``.

    An old PR's base branch may have been renamed (``master`` to ``main``) or
    deleted. The default branch is read once per run and cached.
    """

    def __init__(self, forge: github.ForgeImpl, ref: PRRef) -> None:
        self._forge, self._ref = forge, ref
        self._default: str | None = None
        self._read = False

    def default_branch(self) -> str | None:
        if not self._read:
            self._read = True
            url = f"{self._forge._api_base(self._ref)}/repos/{self._ref.owner}/{self._ref.repo}"
            try:
                data = _get(self._forge, self._ref, url)
                name = data.get("default_branch") if isinstance(data, dict) else None
                self._default = name if isinstance(name, str) and name else None
            except (requests.RequestException, ValueError) as exc:
                logger.warning("%s/%s: cannot read the default branch: %s", self._ref.owner, self._ref.repo, exc)
        return self._default

    def candidates(self, pr: dict) -> Iterator[str]:
        base = pr.get("base") or {}
        tried: set[str] = set()
        for get in (lambda: base.get("ref"), self.default_branch, lambda: base.get("sha")):
            name = get()
            if isinstance(name, str) and name and name not in tried:
                tried.add(name)
                yield name


def _merge_base_compare(forge: github.ForgeImpl, ref: PRRef, pr: dict, commit: str, bases: _Bases) -> dict:
    """Compare ``commit`` against each base candidate until one exists; only a 404 or 422 moves on."""
    last: requests.RequestException | None = None
    for left in bases.candidates(pr):
        try:
            return _compare(forge, ref, left, commit)
        except requests.HTTPError as exc:
            if _http_status(exc) not in _FALLBACK_STATUSES:
                raise
            last = exc
    if last is None:
        raise _MineSkip("the pull request lists no base branch")
    raise last


def _build_pr(forge: github.ForgeImpl, ref: PRRef, pr: dict, labels: list[_Label], final_head: str,
              bases: _Bases) -> _PR:
    mined = _PR(number=int(pr["number"]), merged_at=str(pr["merged_at"]))
    pr_url = str(pr.get("html_url") or f"https://{ref.host}/{ref.owner}/{ref.repo}/pull/{pr['number']}")
    by_commit: dict[str, list[_Label]] = {}
    final_files: list[dict] | None = None
    final_read = False
    for label in labels:
        by_commit.setdefault(label.commit, []).append(label)
    for commit, group in by_commit.items():
        try:
            comparison_to_base = _merge_base_compare(forge, ref, pr, commit, bases)
            merge_base = (comparison_to_base.get("merge_base_commit") or {}).get("sha")
            if not isinstance(merge_base, str) or not merge_base:
                raise _MineSkip("no merge base in the compare answer")
            merge_base = merge_base.lower()
            if merge_base == commit:
                raise _MineSkip("the commit is already in the base branch")
        except (_MineSkip, requests.RequestException, ValueError) as exc:
            logger.warning("%s/%s#%s: skipping commit %s: %s", ref.owner, ref.repo, pr["number"], commit[:7], exc)
            continue
        comparison: dict | None = None
        if commit == final_head:
            comparison = {"merge_base_commit": {"sha": commit}, "files": []}
        elif final_head:
            try:
                comparison = _compare(forge, ref, commit, final_head)
            except (_MineSkip, requests.RequestException, ValueError) as exc:
                logger.warning("%s/%s#%s: cannot tell later changes after %s: %s",
                               ref.owner, ref.repo, pr["number"], commit[:7], exc)
        for label in group:
            label.accepted = _accepted(label.line, label.file, comparison, commit)
        if _rewritten(comparison, commit):
            if not final_read:
                final_read = True
                try:
                    final_files = _pr_files(forge, ref, int(pr["number"]))
                except (requests.RequestException, ValueError) as exc:
                    logger.warning("%s/%s#%s: cannot read the final files after a rewritten history: %s",
                                   ref.owner, ref.repo, pr["number"], exc)
            if final_files is not None:
                for label in group:
                    label.accepted = _accepted_by_content(
                        label.line, _file_entry(comparison_to_base.get("files"), label.file),
                        _file_entry(final_files, label.file),
                    )
        mined.cases.append(_Case(
            id=f"pr{pr['number']}-{commit[:7]}", pr_url=pr_url, base_sha=merge_base, head_sha=commit, labels=group,
        ))
    return mined


def _mine_pr(forge: github.ForgeImpl, ref: PRRef, pr: dict, labels: list[_Label], bases: _Bases) -> _PR:
    base = pr.get("base") or {}
    final_head = str((pr.get("head") or {}).get("sha") or "").lower()
    if not (base.get("ref") or base.get("sha")):
        raise _MineSkip("the pull request lists no base branch")
    return _build_pr(forge, ref, pr, labels, final_head, bases)


def _comments(forge: github.ForgeImpl, ref: PRRef, number: int) -> list[dict]:
    url = f"{forge._api_base(ref)}/repos/{ref.owner}/{ref.repo}/pulls/{number}/comments"
    return [item for page in _list(forge, ref, url, f"review comments of #{number}") for item in page]


def _draft_severities(cases: Sequence[_Case], client: Any, judge_model: str, cfg: Mapping[str, Any]) -> None:
    for case in cases:
        reply_severity = _judge_case(client, judge_model, case, cfg)
        for label in case.labels:
            if reply_severity is None:
                label.severity, label.source = DEFAULT_SEVERITY, SOURCE_JUDGE_ERROR
            else:
                label.severity, label.source = reply_severity[label.id], SOURCE_JUDGE


def build_severity_prompt(case: _Case) -> tuple[str, str]:
    """Fill ``prompts/mine_severity.md`` with one case's comments and split it into ``(system, user)``."""
    rows = [{"id": lb.id, "file": lb.file, "line": lb.line, "text": lb.text} for lb in case.labels]
    template = reviewer.load_prompt(MINE_PROMPT_NAME)
    filled = reviewer.fill_template(template, {"labels": json.dumps(rows, indent=2, ensure_ascii=False)})
    return split_judge_prompt(filled)


def parse_severities(text: str, ids: Sequence[str]) -> dict[str, str]:
    """Map each id to a severity from the judge's JSON ``text``; ``ValueError`` on any other shape."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"not JSON: {exc}") from exc
    rows = data.get("severities") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError('no "severities" array')
    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("id") not in ids or row.get("severity") not in HUMAN_SEVERITIES:
            raise ValueError(f"unusable entry {row!r}")
        if row["id"] in out:
            raise ValueError(f"repeated id {row['id']!r}")
        out[row["id"]] = row["severity"]
    if set(out) != set(ids):
        raise ValueError(f"expected one entry for each of {sorted(ids)}, got {sorted(out)}")
    return out


def _judge_case(client: Any, judge_model: str, case: _Case, cfg: Mapping[str, Any]) -> dict[str, str] | None:
    """One single-shot json_mode call over the case's labels; ``None`` on a failed call or reply."""
    system, user = build_severity_prompt(case)
    retries = int(cfg.get("llm_parse_retries") or 0)
    ids = [lb.id for lb in case.labels]
    attempt = 0
    while True:
        try:
            result = client.invoke(
                system, user, max_tokens=int(cfg.get("llm_max_tokens") or 4096), json_mode=True,
                timeout_s=cfg.get("llm_timeout"),
            )
        except Exception as exc:  # noqa: BLE001 - a failed draft is a default severity, never a failed mine
            logger.warning("severity judge failed for case %r: %s: %s", case.id, type(exc).__name__, exc)
            return None
        try:
            return parse_severities(result.text, ids)
        except ValueError as exc:
            if attempt < retries:
                attempt += 1
                logger.warning("severity judge for case %r: unusable reply (%s); parse retry %d of %d",
                               case.id, exc, attempt, retries)
                continue
            logger.warning("severity judge for case %r: unusable reply (%s)", case.id, exc)
            return None


def _case_json(case: _Case) -> dict[str, Any]:
    return {
        "id": case.id,
        "pr_url": case.pr_url,
        "base_sha": case.base_sha,
        "head_sha": case.head_sha,
        "expected": [
            {"id": lb.id, "file": lb.file, "line": lb.line, "severity": lb.severity, "category": None,
             "accepted": lb.accepted, "text": lb.text}
            for lb in case.labels
        ],
    }


def _dump(data: Any) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def severity_review_markdown(prs: Sequence[_PR]) -> str:
    """The ``severity-review.md`` text: one table of drafted severities per case, and how to confirm them."""
    lines = [
        "# Severity review",
        "",
        "Every severity below is a draft. Confirm or fix each one before using this dataset:",
        "",
        "1. Edit the `severity` of each label in `cases.json` (one of "
        + ", ".join(f"`{s}`" for s in HUMAN_SEVERITIES) + ").",
        "2. Set `confirmed` to `true` for each label you checked in `mine.json`.",
        "3. Run `prxref eval mine --rehash DIR` to record the new `cases_sha256` "
        "(it refuses while any label is unconfirmed, unless `--allow-unconfirmed` is given).",
        "",
    ]
    for pr in prs:
        for case in pr.cases:
            lines += [f"## {case.id}", "", "| Label | Location | Severity | Source | Text | Comment |",
                      "|---|---|---|---|---|---|"]
            for lb in case.labels:
                lines.append(
                    f"| {lb.id} | {_cell(lb.file)}:{lb.line} | {lb.severity} | {lb.source} | "
                    f"{_cell(lb.text)[:REVIEW_TEXT_CHARS]} | {lb.url} |"
                )
            lines.append("")
    return "\n".join(lines)


def _mine_json(args: Any, prs: Sequence[_PR], cases_bytes: bytes, requested: int,
               stopped: str | None = None) -> dict[str, Any]:
    return {
        "version": MINE_VERSION,
        "repo": args.repo,
        "host": args.host,
        "since": args.since or None,
        "until": getattr(args, "until", None) or None,
        "reviewers": getattr(args, "reviewers", None) or "any",
        "pr_numbers": _parse_pr_numbers(getattr(args, "pr", None)),
        "prs_requested": requested,
        "stopped": stopped,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prxref_version": prxref.__version__,
        "judge_model": (args.judge_model or "").strip() or None,
        "cases_sha256": hashlib.sha256(cases_bytes).hexdigest(),
        "prs": [{"number": pr.number, "merged_at": pr.merged_at, "cases": [c.id for c in pr.cases]} for pr in prs],
        "labels": [
            {"case_id": case.id, "label_id": lb.id, "severity": lb.severity, "severity_source": lb.source,
             "confirmed": False, "comment_url": lb.url}
            for pr in prs for case in pr.cases for lb in case.labels
        ],
    }


def mine(args: Any, *, session: requests.Session | None = None, client: Any = None) -> int:
    """Mine ``args.repo``'s merged PRs into ``args.out``; the action behind ``prxref eval mine``.

    ``session`` and ``client`` are test seams: the HTTP session handed to the
    GitHub forge and the severity judge client. Prints one ``#N: K cases, L
    labels`` line per PR written, then ``cases: <path>``, and returns 0. A
    PR whose reads fail is skipped with a WARNING. A rate limit is waited out;
    one that cannot be stops the walk, writes what was mined and records
    ``"stopped": "rate_limit"`` in ``mine.json``. A repository that cannot
    be listed, a refused ``--out`` or a bad flag raises ``ConfigError``.
    """
    if getattr(args, "rehash", None):
        return rehash(args)
    _validate(args)
    out = _check_out(args.out)
    host = (args.host or DEFAULT_HOST).strip()
    args.host = host
    owner, name = args.repo.split("/", 1)
    ref = PRRef(forge="github", host=host, owner=owner, repo=name, number=0,
                url=f"https://{host}/{owner}/{name}")
    if not github._get_token(host):
        logger.warning("no GitHub token set for %s (PRXREF_GITHUB_TOKEN%s): unauthenticated requests are "
                       "rate limited and private repositories are unreachable", host,
                       "" if host.lower() == "github.com" else " or PRXREF_GITHUB_ENTERPRISE_TOKEN")
    forge = github.ForgeImpl(_WaitingSession(session or github._create_default_session()))
    since = _parse_since(args.since)
    until = _parse_since(getattr(args, "until", None))
    reviewers = getattr(args, "reviewers", None) or "any"
    pr_numbers = _parse_pr_numbers(getattr(args, "pr", None))
    bases = _Bases(forge, ref)
    cfg: Mapping[str, Any] = {}
    judge_model = (args.judge_model or "").strip()
    if judge_model and client is None:
        cfg = load_config()
        client = build_judge_client(cfg, judge_model)
    elif judge_model:
        cfg = {"llm_parse_retries": 1}

    mined: dict[int, tuple[_PR, list[_Label]]] = {}

    def qualifies(pr: dict) -> bool:
        number = int(pr["number"])
        listed = pr.get("review_comments")
        if isinstance(listed, int) and not isinstance(listed, bool) and listed < args.min_comments:
            return False
        try:
            labels = _qualifying_labels(pr, _comments(forge, ref, number), reviewers)
            if len(labels) < args.min_comments:
                return False
            built = _mine_pr(forge, ref, pr, labels, bases)
        except (_MineSkip, FeedReadError, requests.RequestException, ValueError) as exc:
            logger.warning("%s/%s#%s: skipped: %s", owner, name, number, exc)
            return False
        if not built.cases:
            return False
        mined[number] = (built, labels)
        return True

    requested = len(pr_numbers) if pr_numbers is not None else args.prs
    stopped: str | None = None
    try:
        if pr_numbers is not None:
            picked = _listed_prs(forge, ref, pr_numbers, qualifies)
        elif until is not None:
            picked = _search_prs(forge, ref, since, until, args.prs, qualifies)
        else:
            picked = _candidate_prs(forge, ref, since, args.prs, qualifies)
        prs = [mined[int(pr["number"])][0] for pr in picked]
    except _RateLimited as exc:
        stopped = STOPPED_RATE_LIMIT
        held = sorted((built for built, _ in mined.values()), key=lambda built: built.merged_at, reverse=True)
        prs = held[:requested]
        logger.warning("%s/%s: the walk stopped on the GitHub rate limit (%s) with %d PRs mined; "
                       "re-run later with a narrower window for the rest", owner, name, exc, len(prs))
    cases = [case for pr in prs for case in pr.cases]
    if judge_model:
        _draft_severities(cases, client, judge_model, cfg)
    for pr in prs:
        print(f"#{pr.number}: {len(pr.cases)} cases, {sum(len(c.labels) for c in pr.cases)} labels")
    cases_bytes = _dump({"version": 1, "cases": [_case_json(c) for c in cases]})
    out.mkdir(parents=True, exist_ok=True)
    cases_path = out / "cases.json"
    _atomic_write(cases_path, cases_bytes)
    _atomic_write(out / "mine.json", _dump(_mine_json(args, prs, cases_bytes, requested, stopped)))
    _atomic_write(out / "severity-review.md", (severity_review_markdown(prs) + "\n").encode("utf-8"))
    if cases:
        load_cases(cases_path, source="--out")
    else:
        logger.warning("no pull request of %s/%s matched; cases.json holds no case", owner, name)
    print(f"cases: {cases_path}")
    return 0


def rehash(args: Any) -> int:
    """Recompute ``cases_sha256`` in ``DIR/mine.json`` after a human edited ``cases.json``.

    Refuses with ``ConfigError`` naming ``--rehash`` while any label in
    ``mine.json`` is still ``confirmed: false``, unless
    ``args.allow_unconfirmed`` is set, and when ``cases.json`` no longer
    loads. Prints ``cases_sha256: <hex>`` and returns 0.
    """
    root = Path(args.rehash)
    mine_path, cases_path = root / "mine.json", root / "cases.json"
    for path in (mine_path, cases_path):
        if not path.is_file():
            raise _cfg_flag("--rehash", f"no such file {str(path)!r}")
    try:
        meta = json.loads(mine_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise _cfg_flag("--rehash", f"{str(mine_path)!r} is not valid JSON: {exc}") from exc
    labels = meta.get("labels") if isinstance(meta, dict) else None
    if not isinstance(labels, list):
        raise _cfg_flag("--rehash", f"{str(mine_path)!r} has no labels array")
    pending = [f"{row.get('case_id')}/{row.get('label_id')}" for row in labels
               if isinstance(row, dict) and row.get("confirmed") is not True]
    if pending and not getattr(args, "allow_unconfirmed", False):
        raise _cfg_flag(
            "--rehash",
            f"{len(pending)} of {len(labels)} labels in {str(mine_path)!r} are still confirmed: false "
            f"(first: {pending[0]}); confirm them, or pass --allow-unconfirmed",
        )
    load_cases(cases_path, source="--rehash")
    digest = hashlib.sha256(cases_path.read_bytes()).hexdigest()
    meta["cases_sha256"] = digest
    _atomic_write(mine_path, _dump(meta))
    print(f"cases_sha256: {digest}")
    return 0
