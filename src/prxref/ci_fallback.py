"""Where a review goes when the token cannot post it (issue #48).

A pull request from a fork, or a pipeline whose token is read-only, lets
prxref read the diff but not comment on it. The review still completes; these
pure functions render it into the channel the CI system reads from its own
job output instead, so the author still sees it:

- GitHub Actions: workflow-command annotations (:func:`github_annotations`)
  and the job summary (:func:`step_summary_markdown`);
- Azure Pipelines: ``##vso[task.logissue]`` logging commands
  (:func:`azure_log_issues`);
- GitLab CI: a Code Quality report (:func:`gitlab_codequality`);
- Bitbucket Pipelines and everything else: the log.

Nothing here reads the environment or writes a file; the CLI decides when to
call them and where their output goes. Every function takes the findings as
objects carrying ``file``, ``line``, ``severity``, ``title`` and ``body`` (and
optionally ``rule``), the same fields the text and JSON outputs read.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

DEGRADED_SUMMARY_KEY = "degraded_summary"
"""The review result key holding a degraded run's summary markdown.

:func:`prxref.orchestrator.orchestrate_review` sets it only when its record's
``degraded`` is not ``None``. It is never part of the ``--format json``
record: it is the text the CLI hands to :func:`step_summary_markdown`.
"""

GITLAB_REPORT_FILE ="gl-code-quality-report.json"
"""The GitLab Code Quality report the CLI writes in the working directory."""

GITHUB_ANNOTATION_CAP = 50
"""GitHub shows at most 50 annotations per step, so no more are emitted."""

CI_GITHUB = "github"
CI_AZURE = "azure"
CI_GITLAB = "gitlab"
CI_BITBUCKET = "bitbucket"

_SEVERITY_ORDER = {"error": 0, "warning": 1}


def detect_ci(environ: Mapping[str, str]) -> str | None:
    """Name the CI system this process runs under, or ``None`` outside one.

    Returns ``"github"`` when ``GITHUB_ACTIONS`` is ``true``, ``"azure"`` when
    ``TF_BUILD`` is ``true``, ``"gitlab"`` when ``GITLAB_CI`` is ``true`` (each
    compared case-insensitively), and ``"bitbucket"`` when
    ``BITBUCKET_BUILD_NUMBER`` is set to anything non-empty. The checks run in
    that order, so the first match wins when several are set.
    """
    if _is_true(environ.get("GITHUB_ACTIONS")):
        return CI_GITHUB
    if _is_true(environ.get("TF_BUILD")):
        return CI_AZURE
    if _is_true(environ.get("GITLAB_CI")):
        return CI_GITLAB
    if environ.get("BITBUCKET_BUILD_NUMBER"):
        return CI_BITBUCKET
    return None


def github_annotations(findings: Iterable[Any]) -> list[str]:
    """Render findings as GitHub Actions workflow-command annotation lines.

    Each finding becomes ``::error``, ``::warning`` or ``::notice`` (severity
    ``error``, ``warning`` and anything else) with the properties ``file``,
    ``line`` and ``title``; ``file`` is omitted for a finding with no path and
    ``line`` for one with no positive line. The message is the finding's body.
    Values escape ``%``, ``\\r`` and ``\\n`` as ``%25``, ``%0D`` and ``%0A``,
    and property values also escape ``:`` and ``,`` as ``%3A`` and ``%2C``.
    Errors come first, then warnings, then the rest, each group in the order
    given, and at most :data:`GITHUB_ANNOTATION_CAP` lines are returned.
    """
    lines = []
    for f in _by_severity(findings)[:GITHUB_ANNOTATION_CAP]:
        severity = _severity(f)
        command = severity if severity in _SEVERITY_ORDER else "notice"
        props = []
        path = _path(f)
        if path:
            props.append(f"file={_gh_property(path)}")
        line = _line(f)
        if line:
            props.append(f"line={line}")
        props.append(f"title={_gh_property(_text(f, 'title'))}")
        lines.append(f"::{command} {','.join(props)}::{_gh_data(_text(f, 'body'))}")
    return lines


def azure_log_issues(findings: Iterable[Any]) -> list[str]:
    """Render findings as Azure Pipelines ``##vso[task.logissue]`` lines.

    Severity ``error`` becomes ``type=error`` and anything else
    ``type=warning``; ``sourcepath`` and ``linenumber`` are omitted for a
    finding with no path or no positive line. The message is the title, a
    colon and the body. Property values and the message escape ``;``, ``]``,
    ``\\r`` and ``\\n`` as ``%3B``, ``%5D``, ``%0D`` and ``%0A``. Errors come
    first, then warnings, then the rest; there is no cap.
    """
    lines = []
    for f in _by_severity(findings):
        kind = "error" if _severity(f) == "error" else "warning"
        props = [f"type={kind}"]
        path = _path(f)
        if path:
            props.append(f"sourcepath={_vso(path)}")
        line = _line(f)
        if line:
            props.append(f"linenumber={line}")
        message = f"{_text(f, 'title')}: {_text(f, 'body')}"
        lines.append(f"##vso[task.logissue {';'.join(props)};]{_vso(message)}")
    return lines


def gitlab_codequality(findings: Iterable[Any]) -> list[dict]:
    """Render findings as GitLab Code Quality report entries.

    Each entry is ``{"description", "check_name", "fingerprint", "severity",
    "location": {"path", "lines": {"begin"}}}``. ``description`` is the title,
    a colon and the body; ``check_name`` is the finding's rule, or
    ``"prxref"`` when it names none. ``fingerprint`` is the SHA-256 hex digest
    of the JSON array ``[file, line, title]``, so the same finding keeps the
    same fingerprint across runs. Severity ``error`` maps to ``major``,
    ``warning`` to ``minor`` and anything else to ``info``. ``begin`` is the
    finding's line, or ``1`` when it has no positive line. Entries keep the
    order given.
    """
    entries = []
    for f in findings:
        path = _path(f)
        line = _line(f)
        title = _text(f, "title")
        severity = _severity(f)
        fingerprint = hashlib.sha256(
            json.dumps([path, line, title], ensure_ascii=False).encode("utf-8"),
        ).hexdigest()
        entries.append({
            "description": f"{title}: {_text(f, 'body')}",
            "check_name": getattr(f, "rule", None) or "prxref",
            "fingerprint": fingerprint,
            "severity": {"error": "major", "warning": "minor"}.get(severity, "info"),
            "location": {"path": path, "lines": {"begin": line or 1}},
        })
    return entries


def step_summary_markdown(summary_text: str) -> str:
    """The markdown appended to a GitHub job summary or written to the log.

    One quoted line saying the review could not be posted to the pull
    request, a blank line, then the review summary exactly as it would have
    been posted, ending in a newline.
    """
    body = summary_text if summary_text.endswith("\n") else summary_text + "\n"
    return (
        "> prxref could not post this review to the pull request, "
        "so it is shown here instead.\n\n" + body
    )


def _is_true(value: str | None) -> bool:
    return isinstance(value, str) and value.strip().lower() == "true"


def _severity(f: Any) -> str:
    value = getattr(f, "severity", "")
    return value.strip().lower() if isinstance(value, str) else ""


def _path(f: Any) -> str:
    value = getattr(f, "file", "")
    return value if isinstance(value, str) else ""


def _line(f: Any) -> int:
    value = getattr(f, "line", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _text(f: Any, name: str) -> str:
    value = getattr(f, name, "")
    return value if isinstance(value, str) else ""


def _by_severity(findings: Iterable[Any]) -> list[Any]:
    return sorted(findings, key=lambda f: _SEVERITY_ORDER.get(_severity(f), 2))


def _gh_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _gh_property(value: str) -> str:
    return _gh_data(value).replace(":", "%3A").replace(",", "%2C")


def _vso(value: str) -> str:
    return (
        value.replace(";", "%3B").replace("]", "%5D")
        .replace("\r", "%0D").replace("\n", "%0A")
    )
