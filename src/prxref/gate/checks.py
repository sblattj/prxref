"""Publish a gate generation to GitHub as the ``sdlc-review`` check (#102).

Every write renews the worker lease first (:class:`Lease`), so a worker
whose fencing token is no longer current raises :class:`LeaseLost` before
it touches GitHub. A timeout or 5xx on a create is answered by looking the
object up (the check by ``external_id``, the review by the bot login and
:data:`REVIEW_MARKER`) before one retry, so a duplicate is never created.
Errors carry the HTTP status and ``X-GitHub-Request-Id``, never a token.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import requests

from prxref.forges.base import SUMMARY_MARKER, InlineComment, with_summary_marker
from prxref.gate.app_auth import GateAuthError
from prxref.gate.receiver import CHECK_NAME
from prxref.gate.store import Generation, LeaseLost, Store
from prxref.triage import Finding

__all__ = [
    "ANNOTATION_BATCH",
    "Captured",
    "GitHubClient",
    "GitHubError",
    "Lease",
    "LeaseLost",
    "Publisher",
    "REVIEW_MARKER",
    "Stale",
    "UncertainReply",
    "annotations_for",
    "conclusion_for",
    "summary_text",
    "SUMMARY_LIMIT",
    "VERDICTS",
]

REVIEW_MARKER = "<!-- sdlc-gate:generation={external_id} -->"
ANNOTATION_BATCH = 50
SUMMARY_LIMIT = 60000
VERDICTS = frozenset({"Approved", "Request-Changes", "Incomplete", "Error"})
_LEVELS = {"error": "failure", "warning": "warning"}
_TIMEOUT_S = 30


class GitHubError(RuntimeError):
    """A GitHub API call failed; the message names the status and request id only."""

    def __init__(self, message: str, *, status: int | None = None, request_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.request_id = request_id


class UncertainReply(GitHubError):
    """A timeout, a dropped connection or a 5xx: the write may or may not have landed."""


class Stale(RuntimeError):
    """The live PR no longer matches the generation; it must not publish."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Lease:
    """The worker's lease, renewed before every write."""

    holder: str
    token: int
    ttl_s: float


@dataclass(frozen=True)
class Captured:
    """What the review asked to post: the summary body and the inline comments."""

    summary: str | None = None
    comments: tuple[InlineComment, ...] = ()


def conclusion_for(result: object) -> tuple[str, str]:
    """Map a review result to ``(conclusion, reason)``; only a clean ``Approved`` succeeds."""
    if not isinstance(result, dict) or result.get("verdict") not in VERDICTS:
        return "failure", "malformed review result"
    verdict = result["verdict"]
    if verdict == "Error":
        return "failure", "review error: the error class is in the summary comment and the run record"
    if verdict == "Incomplete" or (result.get("chunks_failed") or 0) > 0:
        return "failure", "incomplete review: a partial review is not a review"
    if (result.get("overflow_files") or 0) > 0:
        return "failure", f"{result['overflow_files']} file(s) did not fit the review budget"
    if result.get("degraded"):
        return "failure", "degraded review"
    if verdict == "Request-Changes":
        return "failure", "error-severity findings"
    return "success", "approved"


def annotations_for(findings: Sequence[Finding], demoted: Sequence[InlineComment]) -> list[dict]:
    """File-level findings, then inline comments GitHub refused, as check annotations."""
    out = [
        {
            "path": f.file,
            "start_line": 1,
            "end_line": 1,
            "annotation_level": _LEVELS.get(f.severity, "notice"),
            "title": f.title[:255],
            "message": f.body or f.title,
        }
        for f in findings
        if isinstance(f, Finding) and f.line == 0
    ]
    out += [
        {
            "path": c.path,
            "start_line": c.start_line or c.line,
            "end_line": c.line,
            "annotation_level": "warning",
            "message": c.body,
        }
        for c in demoted
    ]
    return out


def summary_text(
    gen: Generation,
    result: object,
    conclusion: str,
    reason: str,
    *,
    effort: str,
) -> str:
    """The check output summary: model, effort, tokens, verdict, chunks, SHAs, run record.

    The run record is named relative to ``PRXREF_GATE_RUNS_DIR`` (the worker
    writes it to ``<runs dir>/<generation id>``, and the external id is that
    id), so the operator's home path is never posted to the PR.
    """
    r = result if isinstance(result, dict) else {}
    models = ", ".join((r.get("sampling") or {}).get("models") or []) or "unknown"
    lines = [
        f"**{conclusion}**: {reason}",
        "",
        f"- Verdict: {r.get('verdict', 'none')}",
        f"- Model: {models} (effort: {effort or 'default'})",
        f"- Tokens: {r.get('input_tokens', 0)} in, {r.get('output_tokens', 0)} out",
        f"- Chunks reviewed: {r.get('chunks_reviewed', 0)}/{r.get('chunk_count', 0)}"
        f" ({r.get('chunks_failed', 0)} failed)",
        f"- Base: `{gen.base_sha}` ({gen.base_ref}); head: `{gen.head_sha}`",
        f"- Generation {gen.external_id}; run record: `runs/{gen.external_id}`",
    ]
    return "\n".join(lines)[:SUMMARY_LIMIT]


class GitHubClient:
    """Installation-token REST calls for one repository at a time."""

    def __init__(self, auth: Any, session: Any, *, api: str = "https://api.github.com"):
        self.auth = auth
        self.session = session
        self.api = api.rstrip("/")

    def _send(self, method: str, repo: str, path: str, json: dict | None, params: dict | None):
        """One HTTP call; a failed mint or any transport error is an :class:`UncertainReply`."""
        from prxref.gate.worker import register_secret  # worker imports this module; import late

        try:
            token = self.auth.token_for(repo)
        except GateAuthError as exc:
            raise UncertainReply(
                f"{method} {path}: installation credential unavailable ({type(exc).__name__}: {exc})"
            ) from None
        register_secret(token)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            return self.session.request(
                method,
                f"{self.api}/repos/{repo}{path}",
                headers=headers,
                timeout=_TIMEOUT_S,
                json=json,
                params=params,
            )
        except requests.RequestException as exc:
            raise UncertainReply(f"{method} {path}: {type(exc).__name__}") from None

    def request(
        self, method: str, repo: str, path: str, *, json: dict | None = None, params: dict | None = None
    ) -> Any:
        """Send one call; a 401 mints a fresh token and retries once."""
        resp = self._send(method, repo, path, json, params)
        if resp.status_code == 401:
            self.auth.invalidate(repo)
            resp = self._send(method, repo, path, json, params)
        rid = resp.headers.get("X-GitHub-Request-Id")
        if resp.status_code >= 500:
            raise UncertainReply(
                f"{method} {path}: HTTP {resp.status_code} (request {rid})", status=resp.status_code, request_id=rid
            )
        if resp.status_code >= 400:
            raise GitHubError(
                f"{method} {path}: HTTP {resp.status_code} (request {rid})", status=resp.status_code, request_id=rid
            )
        return resp.json() if resp.content else None


class Publisher:
    """Every GitHub write for one generation; fenced by the worker lease."""

    def __init__(
        self,
        client: GitHubClient,
        store: Store,
        *,
        app_id: int,
        bot_login: str,
        guard: Callable[[Generation], None] | None = None,
    ):
        self.client = client
        self.store = store
        self.app_id = app_id
        self.bot_login = bot_login
        self.guard = guard

    def _fence(self, lease: Lease, gen: Generation | None = None) -> None:
        if not self.store.renew_lease(lease.holder, lease.token, lease.ttl_s):
            raise LeaseLost(f"fencing token {lease.token} is not the live worker lease")
        if gen is not None and self.guard is not None:
            self.guard(gen)

    def _find_check(self, gen: Generation) -> int | None:
        data = self.client.request(
            "GET",
            gen.repo,
            f"/commits/{gen.head_sha}/check-runs",
            params={"check_name": CHECK_NAME, "app_id": self.app_id, "filter": "all"},
        )
        for run in (data or {}).get("check_runs", []):
            if run.get("external_id") == gen.external_id:
                return run["id"]
        return None

    def begin(self, gen: Generation, lease: Lease) -> int:
        """Create the in-progress check, or move the stored one back to in progress."""
        return self._ensure_check(gen, lease, guarded=True)

    def _ensure_check(self, gen: Generation, lease: Lease, *, guarded: bool) -> int:
        self._fence(lease, gen if guarded else None)
        if gen.check_run_id is not None:
            self.client.request("PATCH", gen.repo, f"/check-runs/{gen.check_run_id}", json={"status": "in_progress"})
            return gen.check_run_id
        body = {
            "name": CHECK_NAME,
            "head_sha": gen.head_sha,
            "external_id": gen.external_id,
            "status": "in_progress",
            "details_url": f"https://github.com/{gen.repo}/pull/{gen.pr_number}/checks",
            "output": {"title": "sdlc review running", "summary": f"Reviewing `{gen.base_sha}`..`{gen.head_sha}`."},
        }
        try:
            check_id = self.client.request("POST", gen.repo, "/check-runs", json=body)["id"]
        except UncertainReply:
            check_id = self._find_check(gen)
            if check_id is None:
                self._fence(lease, gen if guarded else None)
                check_id = self.client.request("POST", gen.repo, "/check-runs", json=body)["id"]
        self.store.set_check_run(gen.id, lease.token, check_id)
        return check_id

    def _find_review(self, gen: Generation, marker: str) -> int | None:
        reviews = (
            self.client.request("GET", gen.repo, f"/pulls/{gen.pr_number}/reviews", params={"per_page": 100}) or []
        )
        for review in reviews:
            if (review.get("user") or {}).get("login") == self.bot_login and marker in (review.get("body") or ""):
                return review["id"]
        return None

    def _post_review(self, gen: Generation, lease: Lease, event: str, comments: Sequence[InlineComment]) -> int | None:
        """Post the ``APPROVE`` or ``REQUEST_CHANGES`` review; ``None`` when GitHub refuses its anchors (422).

        A 422 on a review with no inline comments is not an anchor problem and propagates.
        """
        marker = REVIEW_MARKER.format(external_id=gen.external_id)
        headline = "approved" if event == "APPROVE" else "changes requested"
        payload = {
            "commit_id": gen.head_sha,
            "event": event,
            "body": f"{marker}\nsdlc review: {headline} - {len(comments)} inline finding(s).",
            "comments": [
                {
                    "path": c.path,
                    "line": c.line,
                    "side": c.side,
                    "body": c.body,
                    **({"start_line": c.start_line, "start_side": c.side} if c.start_line else {}),
                }
                for c in comments
            ],
        }
        path = f"/pulls/{gen.pr_number}/reviews"
        try:
            try:
                return self.client.request("POST", gen.repo, path, json=payload)["id"]
            except UncertainReply:
                found = self._find_review(gen, marker)
                if found is not None:
                    return found
                self._fence(lease, gen)
                return self.client.request("POST", gen.repo, path, json=payload)["id"]
        except GitHubError as exc:
            if exc.status == 422 and comments:
                return None
            raise

    def _upsert_summary(self, gen: Generation, body: str) -> None:
        page, mine = 1, None
        while mine is None:
            feed = (
                self.client.request(
                    "GET", gen.repo, f"/issues/{gen.pr_number}/comments", params={"per_page": 100, "page": page}
                )
                or []
            )
            mine = next(
                (
                    c["id"]
                    for c in feed
                    if (c.get("user") or {}).get("login") == self.bot_login and SUMMARY_MARKER in (c.get("body") or "")
                ),
                None,
            )
            if len(feed) < 100:
                break
            page += 1
        body = with_summary_marker(body)
        if mine is not None:
            self.client.request("PATCH", gen.repo, f"/issues/comments/{mine}", json={"body": body})
        else:
            self.client.request("POST", gen.repo, f"/issues/{gen.pr_number}/comments", json={"body": body})

    def _close_check(
        self,
        gen: Generation,
        lease: Lease,
        conclusion: str,
        title: str,
        summary: str,
        annotations: Sequence[dict] = (),
        *,
        guarded: bool = False,
    ) -> None:
        batches = [
            list(annotations[i : i + ANNOTATION_BATCH]) for i in range(0, len(annotations), ANNOTATION_BATCH)
        ] or [[]]
        path = f"/check-runs/{gen.check_run_id}"
        for index, batch in enumerate(batches):
            self._fence(lease, gen if guarded else None)
            output = {"title": title, "summary": summary, "annotations": batch}
            if index < len(batches) - 1:
                self.client.request("PATCH", gen.repo, path, json={"output": output})
            else:
                self.client.request(
                    "PATCH",
                    gen.repo,
                    path,
                    json={
                        "status": "completed",
                        "conclusion": conclusion,
                        "output": output,
                    },
                )

    def complete(self, gen: Generation, lease: Lease, result: object, captured: Captured, *, effort: str) -> str:
        """Post the verdict review and summary comment, then close the check; returns the conclusion."""
        gen = self.store.get(gen.id)
        if gen.check_run_id is None:
            self._ensure_check(gen, lease, guarded=True)
            gen = self.store.get(gen.id)
        conclusion, reason = conclusion_for(result)
        findings = result.get("findings_active") or [] if isinstance(result, dict) else []
        demoted: list[InlineComment] = []
        event = "APPROVE" if conclusion == "success" else "REQUEST_CHANGES"
        published = self.store.publication(gen.id)
        # Skip only when the recorded review is the very event about to be posted; a legacy "review" row
        # (event unknown) or a different event posts, so the app's last review always matches the check.
        if published is None or published[0] != event:
            self._fence(lease, gen)
            review_id = self._post_review(gen, lease, event, captured.comments)
            if review_id is None:
                # GitHub refused the anchors: the verdict still lands, comment-less; findings become annotations.
                demoted = list(captured.comments)
                self._fence(lease, gen)
                review_id = self._post_review(gen, lease, event, ())
            if review_id is not None:
                self.store.record_publication(gen.id, lease.token, event, review_id)
        if captured.summary:
            self._fence(lease, gen)
            self._upsert_summary(gen, captured.summary)
        details = summary_text(gen, result, conclusion, reason, effort=effort)
        if captured.summary:
            sep = "\n\n---\n\n"
            text = captured.summary[: max(SUMMARY_LIMIT - len(sep) - len(details), 0)] + sep + details
        else:
            text = details
        self._close_check(
            gen, lease, conclusion, f"sdlc review: {conclusion}", text, annotations_for(findings, demoted), guarded=True
        )
        return conclusion

    def fail(self, gen: Generation, lease: Lease, reason: str) -> None:
        """Close the check as ``failure`` with ``reason`` (deadline, crash, publish error)."""
        gen = self.store.get(gen.id)
        if gen.check_run_id is None:
            self._ensure_check(gen, lease, guarded=False)
            gen = self.store.get(gen.id)
        self._close_check(gen, lease, "failure", "sdlc review: failure", f"**failure**: {reason}")

    def cancel(self, gen: Generation, lease: Lease, reason: str) -> None:
        """Close an existing check as ``cancelled``; a generation with no check posts nothing."""
        gen = self.store.get(gen.id)
        if gen.check_run_id is not None:
            self._close_check(gen, lease, "cancelled", "sdlc review: cancelled", reason)
