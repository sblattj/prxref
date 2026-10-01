"""Deterministic, opt-in CI-wiring check (issue #66).

A PR that adds a verification script (a ``scripts/verify.sh`` smoke test, a
new test file, a check flag) has only partly met an acceptance criterion
like "add regression checks so this cannot come back" when nothing in the
repository's CI configuration ever runs it: the check runs only when
someone remembers to run it locally. This module flags exactly that — a
check-shaped file the PR adds that no CI configuration file invokes.

Like :mod:`prxref.metadata_rules` it is a companion to
:mod:`prxref.heuristics` under the same doctrine: no model in the loop,
every finding ends its body with " (deterministic check, no model)" (the
suffix is imported from heuristics so the two literals cannot drift), so
:func:`prxref.heuristics.is_deterministic` exempts it from severity
consistency. Unlike the metadata checks it is not pure: it must READ the
repository's CI files, so every I/O it makes is a ``read`` callable the
orchestrator passes in (the forge's head-sha reads or ``--repo-dir``,
never the ``repo_context`` reader whose chunk caps a CI file could starve
on), bounded by :data:`MAX_CI_FILES` literal reads per run. The check is
opt-in — ``ci_wiring = "on"`` — and never changes the verdict: a finding
is ``spec`` when the ticket mentions regression checks, CI, pipelines or
automated tests (relabeled ``warning`` by spec grounding on an ungrounded
run) and ``warning`` otherwise.

Known false negatives, accepted for v1 and documented here rather than
hidden: an invocation embedded inside a folded YAML block scalar the
indentation scanner mis-slices, a CI job that renames the script before
running it (``cp scripts/verify.sh stage.sh``), and a make/npm target that
runs the check without naming its path (``make verify`` matches nothing —
target matching is deferred as the highest false-positive risk). The
conservative direction is under-flagging.
"""
from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from .heuristics import _BODY_SUFFIX
from .rules import match_globs
from .triage import FileDiff, Finding

#: The name fragments that make a file check-shaped: a script whose name
#: (or a ``--flag`` it gains) says verify, smoke or check.
CI_SUFFIX_HINT: tuple[str, ...] = ("verify", "smoke", "check")

#: The built-in CI-configuration globs, restated as ``ci_wiring_globs`` in
#: :mod:`prxref.config` (config stays a leaf module); a set value replaces
#: this set rather than adding to it, as ``context_contract_globs`` does.
DEFAULT_CI_GLOBS: tuple[str, ...] = (
    ".github/workflows/*.y*ml",
    ".gitlab-ci.yml",
    "azure-pipelines.yml",
    ".circleci/config.yml",
    "Jenkinsfile",
    "bitbucket-pipelines.yml",
    ".drone.yml",
    "cloudbuild.yaml",
    ".travis.yml",
)

#: The most CI files one run reads, bounding the check's read cost however
#: many workflow files a glob match finds.
MAX_CI_FILES = 12

# The ticket words that upgrade a finding to ``spec``: the issue's own
# criterion ("the ticket ... mentions regression checks, CI, or automated
# tests"). ``ci\b`` so "ci" matches "CI" and "ci:" but not "circle".
_SPEC_TICKET_RE = re.compile(
    r"regression check|regression test|ci\b|pipeline|automated test", re.I,
)

# A ``--flag`` an added line gains: ``--verify``, ``--smoke-test``,
# ``--check-headers``. Long-form only; a single-dash ``-v`` style flag is
# too short to be check-shaped.
_FLAG_RE = re.compile(r"--([A-Za-z0-9][A-Za-z0-9_-]*)")

# A YAML mapping key line, with GitHub's list dash: ``run: x``,
# ``- run: x``, ``    script:``. The key must be followed by ``:`` but not
# ``:=``-style text; Azure's ``- bash: |`` is the same shape.
_YAML_KEY_RE = re.compile(r"^(?P<indent> *)(?P<dash>- +)?(?P<key>[A-Za-z_][\w.-]*) *:(?!=)")

#: YAML keys whose lines (and indented blocks) are invocations: GitHub's
#: ``run``/``uses``, GitLab's and Bitbucket's ``script``, Azure's ``bash``/
#: ``pwsh``/``script``, plus the generic shell spellings.
_INVOCATION_KEYS = frozenset({
    "run", "uses", "script", "bash", "pwsh", "powershell", "shell", "cmd",
})

#: A line that is a command outright, no YAML key above it: a plain shell
#: line, a Makefile recipe body, a Jenkins ``sh '...'`` step.
_PLAIN_COMMAND_PREFIXES = ("sh ", "bash ", "./", "make ", "pytest ", "npm run ")


@dataclass(frozen=True)
class CiCandidate:
    """One check-shaped file the PR adds or changes.

    ``path`` is the diff path; ``reason`` is the human phrase naming why
    the file counts (which hint matched, the shebang, or a test file
    outside the runner's default include) — it rides the finding body and
    the run record unchanged.
    """

    path: str
    reason: str
    new: bool = True


def _added_lines(file: FileDiff):
    """``(new_line, text)`` for every added (``+``) line, in diff order.

    Same shape as :func:`prxref.heuristics._added_lines`, restated because
    that helper is private to heuristics.
    """
    for hunk in file.hunks:
        for ln in hunk.lines:
            if ln.kind == "+" and ln.new_line is not None:
                yield ln.new_line, ln.text


def _name_tokens(name: str) -> tuple[str, ...]:
    """``name`` lowercased and split on every non-alphanumeric run."""
    return tuple(part for part in re.split(r"[^a-z0-9]+", name.lower()) if part)


def _hint_in_tokens(tokens: tuple[str, ...]) -> str | None:
    """The first CI hint any token contains, or None."""
    for hint in CI_SUFFIX_HINT:
        if any(hint in token for token in tokens):
            return hint
    return None


def default_include(path: str) -> bool:
    """True when ``path`` sits inside a test runner's default include.

    Conservative table, matched case-sensitively on the basename like
    heuristics' frozen basename sets (real ecosystem tooling always emits
    these exact spellings): pytest ``test_*.py`` / ``*_test.py``, jest and
    vitest ``*.test.*`` / ``*.spec.*`` over ts/tsx/js/jsx/mjs/cjs and the
    ``__tests__/`` directory, Go ``*_test.go``, JUnit ``*Test.java`` /
    ``*IT.java``, XCTest ``*Tests.swift`` / ``*Tests.m``, and any file
    under ``tests/``, ``test/`` or ``spec/`` for the ecosystems with no
    name convention (Rust's ``tests/`` included). A file in here runs when
    the suite runs, so it is never a CI-wiring candidate.
    """
    parts = PurePosixPath(path).parts
    if any(part in ("__tests__", "tests", "test", "spec") for part in parts[:-1]):
        return True
    name = parts[-1] if parts else path
    if any(
        fnmatch.fnmatchcase(name, pattern)
        for pattern in (
            "test_*.py", "*_test.py", "*_test.go",
            "*Test.java", "*IT.java", "*Tests.swift", "*Tests.m",
        )
    ):
        return True
    return any(
        name.endswith(suffix)
        for suffix in (
            ".test.ts", ".test.tsx", ".test.js", ".test.jsx", ".test.mjs", ".test.cjs",
            ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx", ".spec.mjs", ".spec.cjs",
        )
    )


def _looks_like_test(path: str) -> bool:
    """True when a whole basename token says test or spec.

    ``src/App.tests.tsx`` (jest's default pattern is ``.test.``, singular)
    and ``src/login_test.jsx`` look like tests; ``special_offer.py`` does
    not ("spec" as a prefix is not "spec" as a word), and neither does
    ``testutils.py`` (a helper, not a check).
    """
    return any(
        token in ("test", "tests", "spec", "specs")
        for token in _name_tokens(PurePosixPath(path).name)
    )


def candidate_checks(files: Sequence[FileDiff]) -> list[CiCandidate]:
    """The check-shaped files among ``files``.

    A file that is not ``removed`` counts when it is new (any status but
    ``modified``) and its basename contains a :data:`CI_SUFFIX_HINT` word
    or it gains a shebang as its first line, or when any ``--flag`` it
    gains (on an added line, absent from the removed lines) contains such
    a word — so a modified script counts only for a new flag, and a
    body-only edit never does. A NEW file whose name says test or spec but
    sits outside every default include counts too. The result is sorted by path, so both the findings and
    the run record are deterministic. Pure: reads only the parsed diff.
    """
    candidates: dict[str, tuple[str, bool]] = {}
    for file in files:
        if file.status == "removed":
            continue
        path = file.path
        fresh = file.status != "modified"
        if fresh:
            hint = _hint_in_tokens(_name_tokens(PurePosixPath(path).name))
            if hint is not None:
                candidates[path] = (f"its name mentions {hint!r}", True)
                continue
        dropped = {
            flag
            for hunk in file.hunks
            for ln in hunk.lines
            if ln.kind == "-"
            for flag in _FLAG_RE.findall(ln.text)
        }
        flag_hint: str | None = None
        for _, text in _added_lines(file):
            for flag in _FLAG_RE.findall(text):
                if flag in dropped:
                    continue
                flag_hint = flag_hint or _hint_in_tokens(_name_tokens(flag))
        if flag_hint is not None:
            candidates[path] = (f"it gains a --{flag_hint} flag", fresh)
            continue
        if fresh and any(
            new_line == 1 and text.startswith("#!") for new_line, text in _added_lines(file)
        ):
            candidates[path] = ("it gains a shebang line", True)
            continue
        if file.status == "added" and _looks_like_test(path) and not default_include(path):
            candidates[path] = (
                "it is a new test file outside the runner's default include",
                True,
            )
    return [
        CiCandidate(path, reason, fresh)
        for path, (reason, fresh) in sorted(candidates.items())
    ]


def _literal_globs(globs: Sequence[str]) -> list[str]:
    """The globs that are plain paths, in order, deduplicated.

    Same rule as :func:`prxref.repo_contracts.literal_contract_paths`
    (restated, not imported: this module shares nothing else with the
    contract excerptor): a glob without ``*``, ``?`` or ``[`` that does
    not start with ``!`` names one repository-relative path, read directly
    even when no listing shows it — a miss costs one read.
    """
    out: dict[str, None] = {}
    for glob in globs:
        if not glob.strip() or glob.startswith("!") or any(c in glob for c in "*?["):
            continue
        if match_globs(glob, globs):
            out.setdefault(glob)
    return list(out)


def ci_config_paths(
    listing: Collection[str] | None, globs: Sequence[str],
) -> list[str]:
    """The repository's CI configuration paths: sorted, deduplicated.

    Every path of ``listing`` (the head-sha file listing, or None when the
    source cannot list) :func:`~prxref.rules.match_globs` selects with
    ``globs`` counts, and every literal glob counts even without a listing
    (precedent: the contract files' literal reads). With no listing the
    glob entries — ``.github/workflows/*.y*ml`` among the built-ins —
    cannot match, and the literal entries are the fallback the check
    reads. Pure: reads neither the repository nor the diff.
    """
    selected = {path for path in (listing or ()) if path and match_globs(path, globs)}
    selected.update(_literal_globs(globs))
    return sorted(selected)


def _invocation_lines(text: str) -> list[str]:
    """Every line of ``text`` that can carry a command.

    A mini indentation scanner in the spirit of
    :class:`prxref.repo_contracts._Yaml` (restated here because that class
    is private to the contract excerptor; a shared scanner would be the
    third copy's excuse to exist): a line with a key in
    :data:`_INVOCATION_KEYS` opens a block, every deeper-indented
    non-comment line inside it is shell too, and a line that starts with a
    plain command prefix counts without any YAML around it. Blank lines
    and ``#`` comments never count, so a mention in a comment alone — the
    only line naming the script — leaves the check unwired.
    """
    out: list[str] = []
    floor: int | None = None
    for raw in text.split("\n"):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue  # a comment or blank never counts, nor closes a block
        indent = len(raw) - len(raw.lstrip(" "))
        match = _YAML_KEY_RE.match(raw)
        if match is not None:
            # The key's column sits after a list dash when there is one, so
            # GitHub's ``- run:`` and its aligned siblings compare columns.
            column = match.end("dash") if match.group("dash") else len(match.group("indent"))
            if match.group("key") in _INVOCATION_KEYS:
                floor = column
                out.append(stripped)
                continue
            floor = None  # a sibling or parent key closes the open block
        elif floor is not None and indent > floor:
            out.append(stripped)  # a line inside the open invocation block
            continue
        else:
            floor = None
        command = stripped.lstrip("- ")
        if stripped.startswith(_PLAIN_COMMAND_PREFIXES) or command.startswith(
            _PLAIN_COMMAND_PREFIXES,
        ):
            out.append(stripped)
    return out


def _mentions(line: str, path: str, basename: str) -> bool:
    """True when ``line`` names the candidate by full path or bare basename."""
    if path and path in line:
        return True
    return bool(
        re.search(rf"(?<![\w/.-]){re.escape(basename)}(?![\w.-])", line)
    )


def invokes(text: str, path: str, candidate: CiCandidate) -> bool:
    """True when the CI file ``text`` at ``path`` invokes ``candidate``.

    An invocation is an invocation line (see :func:`_invocation_lines`)
    that mentions the candidate's full diff path — ``./scripts/verify.sh``
    contains ``scripts/verify.sh`` — or its bare basename at word
    boundaries, so ``verify.sh`` alone matches too. A mention in a
    comment, a ``name:`` label or a folded-scalar mis-slice never counts.
    Pure: reads only the text it is handed.
    """
    basename = PurePosixPath(candidate.path).name
    return any(
        _mentions(line, candidate.path, basename)
        for line in _invocation_lines(text)
    )


def ci_wiring_findings(
    files: Sequence[FileDiff],
    *,
    read: Callable[[str], str | None],
    listing: Collection[str] | None,
    globs: Sequence[str] = (),
    ticket_text: str | None = None,
) -> tuple[list[Finding], dict]:
    """Flag every candidate no CI configuration file invokes; never raises.

    ``read`` is the orchestrator's uncapped head-sha read (a None return
    is a miss and is skipped); ``listing`` the repository path listing or
    None; ``globs`` the effective CI-file globs, where empty falls back to
    :data:`DEFAULT_CI_GLOBS` (the replace-not-append rule: an operator
    value replaces the built-in set, and an empty one reads as unset).
    ``ticket_text`` is the ``--context-file`` ticket's text (None when
    there is no ticket): matching :data:`_SPEC_TICKET_RE` makes the
    findings ``spec``, else they are ``warning`` — spec grounding still
    relabels a ``spec`` to ``warning`` on an ungrounded run, the desired
    safe default.

    Returns the findings — one per unwired candidate, file-level
    (``line=0``) on the candidate's own path, confidence 1.0, sorted by
    path, body naming the CI files searched and ending with the
    deterministic suffix — and the run-record stamp
    ``{"candidates", "ci_files", "picked_up_default", "triggered"}``
    (``triggered`` exactly when a finding was raised). At most
    :data:`MAX_CI_FILES` files are read, and none when there is no
    candidate. Deterministic: no model, no randomness, the only I/O the
    ``read`` callable.
    """
    candidates = candidate_checks(files)
    picked_up_default = sorted({
        file.path for file in files
        if file.status == "added" and _looks_like_test(file.path)
        and default_include(file.path)
    })
    if not candidates:
        return [], {
            "candidates": [],
            "ci_files": [],
            "picked_up_default": picked_up_default,
            "triggered": False,
        }

    effective = [glob for glob in globs if glob.strip()] or list(DEFAULT_CI_GLOBS)
    ci_files = ci_config_paths(listing, effective)[:MAX_CI_FILES]
    texts = [
        (ci_path, text)
        for ci_path in ci_files
        if isinstance(text := read(ci_path), str)
    ]

    severity = (
        "spec" if ticket_text and _SPEC_TICKET_RE.search(ticket_text) else "warning"
    )
    searched = (
        "\n".join(f"- `{ci_path}`" for ci_path in ci_files)
        if ci_files
        else "- (no CI configuration file was found)"
    )
    findings: list[Finding] = []
    for candidate in candidates:
        verb = "added" if candidate.new else "changed"
        if any(invokes(text, ci_path, candidate) for ci_path, text in texts):
            continue
        findings.append(Finding(
            file=candidate.path,
            line=0,
            severity=severity,
            confidence=1.0,
            title=f"`{candidate.path}` is {verb} but no CI job runs it",
            body=(
                f"This PR {'adds' if candidate.new else 'changes'} `{candidate.path}`, and {candidate.reason}, but no "
                f"CI configuration file the run could read invokes it, so the "
                f"check runs only when someone remembers to run it locally. CI "
                f"files searched:\n{searched}\nWire the check into CI (a workflow "
                f"`run:` step, a `script:` entry, a make or npm target) or drop "
                f"it from the ticket's acceptance.{_BODY_SUFFIX}"
            ),
        ))
    record = {
        "candidates": [candidate.path for candidate in candidates],
        "ci_files": list(ci_files),
        "picked_up_default": picked_up_default,
        "triggered": bool(findings),
    }
    return findings, record
