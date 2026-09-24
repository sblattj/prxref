"""Eval cases: load and validate the ``--cases`` input of ``prxref eval`` (issue #14).

``--cases`` names either a ``cases.json`` file or a directory of ``case-*/``
directories, the layout of ``tests/evals/``. Both forms load into the same
frozen :class:`EvalCase` records holding their :class:`ExpectedFinding`
labels, so the run and score steps never see which form a dataset used.

A ``cases.json`` file is ``{"version": 1, "cases": [...]}``. Each case is an
object with a required ``id`` and ``expected`` and the optional ``pr_url``,
``base_sha``, ``head_sha``, ``diff_file``, ``context_file`` and ``spec`` (one
string or a list of them). ``expected`` is a list, possibly empty, of human
findings, each with ``id``, ``file``, ``line`` and ``severity`` and the
optional ``category``, ``accepted``, ``text`` and ``must_match``. An optional
field set to ``null`` counts as absent. A relative ``diff_file``,
``context_file`` or ``spec`` path is read relative to the directory holding
the ``cases.json`` file; a ``spec`` entry that is an ``http(s)`` URL is kept
as given.

A ``case-*/`` directory supplies ``diff.patch`` (required) as its
``diff_file``, ``expected.json`` (required: the ``expected`` list above) and,
when present, ``ticket.md`` as its ``context_file`` and ``docs/`` as its one
``spec`` source. Its id is the directory name. ``expected.json`` spells
``line`` as ``line_hint`` and ``category`` as ``source``, and every message
about it uses those spellings.

A case replays either a local diff (``diff_file``) or a pinned commit range of
a pull request (``pr_url`` with both ``base_sha`` and ``head_sha``), and may
give ``pr_url`` beside ``diff_file`` as ``prxref review`` allows. The SHA
rules are those of the replay flags: full 40- or 64-character hex, stored
lowercased, naming two different commits.

Human severity is one of :data:`HUMAN_SEVERITIES` and is stored as given.
``minor`` is the labeller's lowest tier, which a scorer maps to ``warning``
only when it compares severities; ``spec`` and ``outofscope`` are prxref's
own tiers, which the directory dataset labels with. ``must_match`` is a plain
substring, or a regular expression when prefixed :data:`MUST_MATCH_REGEX_PREFIX`.

Every case is validated when the dataset loads, before any review runs, so a
bad dataset is a configuration error: each problem raises
:class:`~prxref.llm.ConfigError` whose message is
``<source>: case '<id>': <field>: <problem>``, naming ``--cases``, the case
and the field, and the CLI exits 2. A case whose id is itself the problem is
named by its position, ``cases[<n>]``; a problem with the file as a whole
names the path instead of a case. When a case has a diff file, every label
must anchor on a line that diff adds (:func:`check_anchors`); a case pinned
to a pull request's range has no diff until it runs, so its labels are
checked for shape only.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from prxref.forges.base import detect_forge
from prxref.llm import ConfigError
from prxref.triage import FileDiff, parse_unified_diff

CASES_VERSION = 1
HUMAN_SEVERITIES = ("error", "warning", "minor", "spec", "outofscope")
MUST_MATCH_REGEX_PREFIX = "re:"

_CASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_FULL_SHA_RE = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?")
_TOP_KEYS = ("version", "cases")
_CASE_KEYS = (
    "id", "pr_url", "base_sha", "head_sha", "diff_file", "context_file", "spec", "expected",
)
_EXPECTED_KEYS = (
    "id", "file", "line", "severity", "category", "accepted", "text", "must_match",
)
_JSON_SPELLING: dict[str, str] = {}
_DIRECTORY_SPELLING = {"line": "line_hint", "category": "source"}


@dataclass(frozen=True)
class ExpectedFinding:
    """One human-labelled finding that a review of its case should report.

    ``line`` is the 1-based line in the new (post-image) file. ``severity``
    is one of :data:`HUMAN_SEVERITIES`, exactly as labelled. ``category`` is
    the labeller's free-form class (the directory form's ``source``,
    ``spec`` or ``generic``), ``accepted`` whether the author acted on the
    comment, and ``text`` the reviewer's words; each is ``None`` when not
    labelled. ``must_match`` is the deterministic acceptance predicate, or
    ``None`` when the label is left to a judge.
    """

    id: str
    file: str
    line: int
    severity: str
    category: str | None = None
    accepted: bool | None = None
    text: str | None = None
    must_match: str | None = None


@dataclass(frozen=True)
class EvalCase:
    """One validated eval case: what to replay and the findings it should yield.

    ``id`` is a single safe path segment, unique within its dataset.
    ``diff_file`` and ``context_file`` are paths ready to open, already
    joined onto the ``cases.json`` directory or the case directory; ``spec``
    holds the spec sources in order, each such a path or an ``http(s)`` URL,
    and is empty when the case sets none. ``base_sha`` and ``head_sha`` are
    both full lowercased SHAs, and ``pr_url`` is set, or both are ``None``.
    ``expected`` keeps the labels in file order and may be empty.
    """

    id: str
    expected: tuple[ExpectedFinding, ...]
    pr_url: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    diff_file: str | None = None
    context_file: str | None = None
    spec: tuple[str, ...] = ()


def load_cases(path: str | os.PathLike[str], *, source: str = "--cases") -> list[EvalCase]:
    """Load and validate every eval case under ``path``, in dataset order.

    A directory is read as ``case-*/`` directories, sorted by name; anything
    else is read as a ``cases.json`` file. Nothing is returned until every
    case has passed, so a dataset is used whole or not at all. Each problem
    raises :class:`~prxref.llm.ConfigError` naming ``source`` (the flag that
    supplied ``path``), the case and the field.
    """
    root = Path(path)
    if root.is_dir():
        return _load_directory(root, source)
    return _load_cases_json(root, source)


def check_anchors(case: EvalCase, diff_text: str, *, source: str = "--cases") -> None:
    """Refuse a label of ``case`` that does not anchor on a line ``diff_text`` adds.

    Every expected finding's ``file`` must be a file the diff touches and its
    ``line`` one of that file's added lines. The loader runs this check on
    every case with a diff file; a caller holding the diff of a pinned range
    can run it on that diff. A failure raises
    :class:`~prxref.llm.ConfigError` naming ``source``, the case and the field.
    """
    files = parse_unified_diff(diff_text)
    _check_anchors(case.expected, files, f"case {case.id!r}", "expected", _JSON_SPELLING, source)


def _load_cases_json(root: Path, source: str) -> list[EvalCase]:
    data = _read_json(root, source)
    where = repr(str(root))
    if not isinstance(data, dict):
        raise ConfigError(
            f'{source}: {where}: must be an object like {{"version": 1, "cases": [...]}}, '
            f"got {_kind(data)}"
        )
    unknown = sorted(set(data) - set(_TOP_KEYS))
    if unknown:
        raise ConfigError(
            f"{source}: {where}: {', '.join(unknown)}: unknown field; "
            f"expected {' and '.join(_TOP_KEYS)}"
        )
    version = data.get("version")
    if type(version) is not int or version != CASES_VERSION:
        raise ConfigError(f"{source}: {where}: version: must be {CASES_VERSION}, got {_kind(version)}")
    entries = data.get("cases")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{source}: {where}: cases: must be a non-empty array, got {_kind(entries)}")
    cases: list[EvalCase] = []
    first_index: dict[str, int] = {}
    for index, entry in enumerate(entries):
        case = _case_from_json(entry, index, root.parent, source)
        if case.id in first_index:
            raise ConfigError(
                f"{source}: case {case.id!r}: id: duplicate of cases[{first_index[case.id]}] "
                f"(cases[{index}])"
            )
        first_index[case.id] = index
        cases.append(case)
    return cases


def _case_from_json(entry: Any, index: int, base: Path, source: str) -> EvalCase:
    if not isinstance(entry, dict):
        raise ConfigError(f"{source}: cases[{index}]: must be an object, got {_kind(entry)}")
    case_id = entry.get("id")
    if not isinstance(case_id, str) or not _CASE_ID_RE.fullmatch(case_id):
        raise ConfigError(
            f"{source}: cases[{index}]: id: must be a name of letters, digits, '.', '_' "
            f"and '-' that starts with a letter or digit, got {_kind(case_id)}"
        )
    where = f"case {case_id!r}"
    unknown = sorted(set(entry) - set(_CASE_KEYS))
    if unknown:
        raise ConfigError(
            f"{source}: {where}: {', '.join(unknown)}: unknown field; "
            f"allowed: {', '.join(_CASE_KEYS)}"
        )
    pr_url = _optional_text(entry, "pr_url", where, source)
    base_sha = _optional_text(entry, "base_sha", where, source)
    head_sha = _optional_text(entry, "head_sha", where, source)
    diff_file = _optional_text(entry, "diff_file", where, source)
    context_file = _optional_text(entry, "context_file", where, source)
    base_sha, head_sha = _check_replay(pr_url, base_sha, head_sha, diff_file, where, source)
    if pr_url is not None and detect_forge(pr_url) is None:
        raise ConfigError(
            f"{source}: {where}: pr_url: not a pull request URL of a supported forge, "
            f"got {pr_url!r}"
        )
    if context_file is not None:
        context_file = _join(base, context_file)
        if not Path(context_file).is_file():
            raise ConfigError(f"{source}: {where}: context_file: no such file {context_file!r}")
    spec = _spec_sources(entry.get("spec"), base, where, source)
    if "expected" not in entry:
        raise ConfigError(f"{source}: {where}: expected: required (an array of findings, possibly empty)")
    expected = _expected_findings(entry["expected"], where, "expected", _JSON_SPELLING, source)
    if diff_file is not None:
        diff_file = _join(base, diff_file)
        files = _read_diff(diff_file, where, "diff_file", source)
        _check_anchors(expected, files, where, "expected", _JSON_SPELLING, source)
    return EvalCase(
        id=case_id, expected=expected, pr_url=pr_url, base_sha=base_sha, head_sha=head_sha,
        diff_file=diff_file, context_file=context_file, spec=spec,
    )


def _check_replay(
    pr_url: str | None,
    base_sha: str | None,
    head_sha: str | None,
    diff_file: str | None,
    where: str,
    source: str,
) -> tuple[str | None, str | None]:
    if pr_url is None and diff_file is None:
        raise ConfigError(
            f"{source}: {where}: pr_url/diff_file: one is required (diff_file replays a "
            "local diff; pr_url with base_sha and head_sha replays a pinned range)"
        )
    if (base_sha is None) != (head_sha is None):
        missing, given = ("head_sha", "base_sha") if head_sha is None else ("base_sha", "head_sha")
        raise ConfigError(f"{source}: {where}: {missing}: required together with {given}")
    if base_sha is None or head_sha is None:
        if diff_file is None:
            raise ConfigError(
                f"{source}: {where}: base_sha/head_sha: required with pr_url when there is "
                "no diff_file, so the replay is pinned to a commit range"
            )
        return None, None
    for field, value in (("base_sha", base_sha), ("head_sha", head_sha)):
        if not _FULL_SHA_RE.fullmatch(value):
            raise ConfigError(
                f"{source}: {where}: {field}: must be a full 40- or 64-character hex "
                f"commit SHA, got {value!r}"
            )
    base_sha, head_sha = base_sha.lower(), head_sha.lower()
    if base_sha == head_sha:
        raise ConfigError(f"{source}: {where}: base_sha/head_sha: must name two different commits")
    if pr_url is None:
        raise ConfigError(
            f"{source}: {where}: pr_url: required with base_sha/head_sha (the range is "
            "resolved in that pull request's repository)"
        )
    return base_sha, head_sha


def _spec_sources(raw: Any, base: Path, where: str, source: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        items: list[Any] = [raw]
        labels = ["spec"]
    elif isinstance(raw, list):
        items = raw
        labels = [f"spec[{i}]" for i in range(len(raw))]
    else:
        raise ConfigError(
            f"{source}: {where}: spec: must be a string or an array of strings, got {_kind(raw)}"
        )
    out: list[str] = []
    for item, label in zip(items, labels, strict=True):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{source}: {where}: {label}: must be a non-empty string, got {_kind(item)}")
        parsed = urlparse(item)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            out.append(item)
            continue
        resolved = _join(base, item)
        if not Path(resolved).exists():
            raise ConfigError(f"{source}: {where}: {label}: no such file or directory {resolved!r}")
        out.append(resolved)
    return tuple(out)


def _load_directory(root: Path, source: str) -> list[EvalCase]:
    try:
        case_dirs = sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("case-"))
    except OSError as exc:
        raise ConfigError(f"{source}: cannot read {str(root)!r}: {exc.strerror or exc}") from exc
    if not case_dirs:
        raise ConfigError(
            f"{source}: {str(root)!r} has no case-*/ directories (pass a cases.json file, "
            "or a directory of case-*/ directories)"
        )
    return [_case_from_directory(case_dir, source) for case_dir in case_dirs]


def _case_from_directory(case_dir: Path, source: str) -> EvalCase:
    case_id = case_dir.name
    where = f"case {case_id!r}"
    if not _CASE_ID_RE.fullmatch(case_id):
        raise ConfigError(
            f"{source}: {where}: id: the directory name must be letters, digits, '.', '_' "
            "and '-' only"
        )
    diff_file = str(case_dir / "diff.patch")
    files = _read_diff(diff_file, where, "diff.patch", source)
    expected_path = case_dir / "expected.json"
    raw = _read_json(expected_path, f"{source}: {where}: expected.json")
    expected = _expected_findings(raw, where, "expected.json", _DIRECTORY_SPELLING, source)
    _check_anchors(expected, files, where, "expected.json", _DIRECTORY_SPELLING, source)
    ticket = case_dir / "ticket.md"
    docs = case_dir / "docs"
    return EvalCase(
        id=case_id,
        expected=expected,
        diff_file=diff_file,
        context_file=str(ticket) if ticket.is_file() else None,
        spec=(str(docs),) if docs.is_dir() else (),
    )


def _expected_findings(
    raw: Any, where: str, prefix: str, spelling: dict[str, str], source: str
) -> tuple[ExpectedFinding, ...]:
    if not isinstance(raw, list):
        raise ConfigError(f"{source}: {where}: {prefix}: must be an array of findings, got {_kind(raw)}")
    allowed = {spelling.get(key, key): key for key in _EXPECTED_KEYS}
    findings: list[ExpectedFinding] = []
    seen: dict[str, int] = {}
    for index, entry in enumerate(raw):
        at = f"{prefix}[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{source}: {where}: {at}: must be an object, got {_kind(entry)}")
        unknown = sorted(set(entry) - set(allowed))
        if unknown:
            raise ConfigError(
                f"{source}: {where}: {at}.{unknown[0]}: unknown field; "
                f"allowed: {', '.join(allowed)}"
            )
        values = {key: entry.get(name) for name, key in allowed.items()}
        problem = _finding_problem(entry, values, spelling)
        if problem is None and values["id"] in seen:
            problem = ("id", f"duplicate of {prefix}[{seen[values['id']]}]")
        if problem is not None:
            key, text = problem
            raise ConfigError(f"{source}: {where}: {at}.{spelling.get(key, key)}: {text}")
        seen[values["id"]] = index
        findings.append(ExpectedFinding(**values))
    return tuple(findings)


def _finding_problem(
    entry: dict, values: dict[str, Any], spelling: dict[str, str]
) -> tuple[str, str] | None:
    for key in ("id", "file", "line", "severity"):
        if spelling.get(key, key) not in entry:
            return key, "required"
    for key in ("id", "file"):
        if not isinstance(values[key], str) or not values[key].strip():
            return key, f"must be a non-empty string, got {_kind(values[key])}"
    line = values["line"]
    if type(line) is not int or line < 1:
        return "line", f"must be an integer >= 1, got {_kind(line)}"
    severity = values["severity"]
    if not isinstance(severity, str) or severity not in HUMAN_SEVERITIES:
        return "severity", f"must be one of {', '.join(HUMAN_SEVERITIES)}, got {_kind(severity)}"
    for key in ("category", "text", "must_match"):
        value = values[key]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            return key, f"must be a non-empty string or null, got {_kind(value)}"
    accepted = values["accepted"]
    if accepted is not None and not isinstance(accepted, bool):
        return "accepted", f"must be true, false or null, got {_kind(accepted)}"
    problem = _must_match_problem(values["must_match"])
    if problem:
        return "must_match", problem
    return None


def _must_match_problem(pattern: str | None) -> str:
    if pattern is None or not pattern.startswith(MUST_MATCH_REGEX_PREFIX):
        return ""
    body = pattern[len(MUST_MATCH_REGEX_PREFIX):]
    if not body:
        return f"an empty regular expression after {MUST_MATCH_REGEX_PREFIX!r} matches everything"
    try:
        re.compile(body)
    except re.error as exc:
        return f"invalid regular expression after {MUST_MATCH_REGEX_PREFIX!r}: {exc}"
    return ""


def _check_anchors(
    expected: tuple[ExpectedFinding, ...],
    files: list[FileDiff],
    where: str,
    prefix: str,
    spelling: dict[str, str],
    source: str,
) -> None:
    by_path = {f.path: f for f in files}
    for index, finding in enumerate(expected):
        at = f"{prefix}[{index}]"
        file_diff = by_path.get(finding.file)
        if file_diff is None:
            raise ConfigError(
                f"{source}: {where}: {at}.file: {finding.file!r} is not a file the diff touches"
            )
        if finding.line not in file_diff.added_lines:
            raise ConfigError(
                f"{source}: {where}: {at}.{spelling.get('line', 'line')}: line {finding.line} "
                f"is not a line the diff adds to {finding.file!r}"
            )


def _read_diff(path: str, where: str, field: str, source: str) -> list[FileDiff]:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ConfigError(f"{source}: {where}: {field}: cannot read {path!r}: {exc.strerror or exc}") from exc
    files = parse_unified_diff(text)
    if not files:
        raise ConfigError(f"{source}: {where}: {field}: {path!r} holds no file diffs")
    return files


def _read_json(path: Path, head: str) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{head}: cannot read {str(path)!r}: {exc.strerror or exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{head}: {str(path)!r} is not valid UTF-8") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{head}: {str(path)!r} is not valid JSON "
            f"({exc.msg} at line {exc.lineno} column {exc.colno})"
        ) from exc


def _optional_text(entry: dict, key: str, where: str, source: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{source}: {where}: {key}: must be a non-empty string or null, got {_kind(value)}")
    return value


def _join(base: Path, value: str) -> str:
    return str(base / value)


def _kind(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"the boolean {str(value).lower()}"
    if isinstance(value, int | float):
        return f"the number {value!r}"
    if isinstance(value, str):
        return f"the string {value!r}"
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, list):
        return "an array"
    return type(value).__name__
