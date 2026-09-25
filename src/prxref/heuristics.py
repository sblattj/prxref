"""Deterministic, non-LLM heuristics that manufacture Findings directly.

Every other Finding producer in the pipeline (a chunk worker, the systemic
sweep) is an LLM call whose output the quality passes filter or rewrite —
none of them originates a Finding on its own. A heuristic here is the
opposite: pure, computed once over the parsed diff's ``FileDiff`` list, no
model in the loop, so its output can be concatenated onto the LLM-sourced
findings list before the quality passes run and survive them the same way a
model finding would, except that severity consistency never regroups them
(``is_deterministic``).
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import PurePosixPath

from .triage import FileDiff, Finding

# Release-machinery basenames that are unambiguous on their own: version
# manifests, a bare VERSION file, Python's convention-named version modules,
# and the release-please manifest. Case-sensitive — real ecosystem tooling
# (npm, cargo, setuptools, release-please) always emits these exact names.
_MACHINERY_BASENAMES = frozenset({
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "setup.py",
    "setup.cfg",
    "VERSION",
    "version.py",
    "__version__.py",
    ".release-please-manifest.json",
})

# Lockfiles across ecosystems. This list is this module's own — it does not
# import ``systemic._LOCKFILE_BASENAMES``, which is npm-family only and
# private to that module — but is kept consistent with it on the basenames
# they share (package-lock.json, npm-shrinkwrap.json, pnpm-lock.yaml,
# yarn.lock, bun.lockb, bun.lock).
_LOCKFILE_BASENAMES = frozenset({
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "bun.lock",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "go.sum",
    "composer.lock",
})

# The same set, public, for callers outside this module: the PR-size advisory
# hands it to ``triage.count_size_relevant_changes``, because triage must never
# import heuristics. This module's own checks keep using the private name.
LOCKFILE_BASENAMES: frozenset[str] = _LOCKFILE_BASENAMES

# Case-insensitive basename prefixes: CHANGELOG.md, changelog.rst,
# HISTORY.txt, RELEASE_NOTES.md all match regardless of extension or case.
_PREFIX_BASENAMES = ("changelog", "history", "release_notes")

# The release-shaped ratio bar, 80%, held as an exact rational (4/5) rather
# than a float: the boundary is checked by cross-multiplication
# (``machinery * _RATIO_DEN >= total * _RATIO_NUM``) so no floating-point
# representation can nudge a file set across or off the line.
_RATIO_NUM = 4
_RATIO_DEN = 5

# A PR must touch at least this many files to be release-shaped at all — a
# single-file diff is never release-shaped regardless of ratio.
_MIN_FILES_FOR_SHAPE = 2

_BODY_SUFFIX = " (deterministic check, no model)"


def is_deterministic(finding: Finding) -> bool:
    """True when ``finding`` came from a check in this module, not a model.

    Every finding this module makes ends its body with " (deterministic
    check, no model)", and that ending is the mark: a finding whose ``body``
    is a string ending with it is deterministic. Its severity and
    confidence are the check's own, so
    :func:`prxref.quality.apply_severity_consistency` leaves it out.
    """
    body = finding.body
    return isinstance(body, str) and body.endswith(_BODY_SUFFIX)


def _is_release_machinery(path: str) -> bool:
    """True when ``path`` is release machinery under the frozen contract.

    Matching is basename-only, with one exception: any path with a
    ``.changeset`` directory component is machinery regardless of its own
    filename, since a changeset's filename is arbitrary (commonly a random
    two-word slug) and only its parent directory identifies it.
    """
    parts = PurePosixPath(path).parts
    if ".changeset" in parts[:-1]:
        return True
    basename = parts[-1] if parts else path
    if basename in _MACHINERY_BASENAMES or basename in _LOCKFILE_BASENAMES:
        return True
    if basename.endswith(".gemspec"):
        return True
    lower = basename.lower()
    return any(lower.startswith(prefix) for prefix in _PREFIX_BASENAMES)


def release_shape_findings(files: list[FileDiff]) -> list[Finding]:
    """Flag a release-shaped PR that also touches non-machinery source.

    A PR is release-shaped when it changes at least two files
    (:data:`_MIN_FILES_FOR_SHAPE`) and at least 80% of them
    (:data:`_RATIO_NUM` / :data:`_RATIO_DEN`, an exact rational comparison —
    no float) are release machinery: version manifests, changelogs,
    lockfiles, ``.changeset/`` entries, and the release-please manifest.
    Removed files count toward the total like any other changed file.

    When a release-shaped PR also changes one or more non-machinery files,
    returns exactly one file-level (``line=0``) warning naming every
    offending path — sorted, anchored on the first — with a body listing
    each offending path and ending with " (deterministic check, no
    model)". Otherwise returns ``[]``. Pure and deterministic: no LLM call,
    no I/O, no randomness.
    """
    total = len(files)
    if total < _MIN_FILES_FOR_SHAPE:
        return []

    offenders = sorted(f.path for f in files if not _is_release_machinery(f.path))
    machinery = total - len(offenders)

    if machinery * _RATIO_DEN < total * _RATIO_NUM:
        return []
    if not offenders:
        return []

    body = "\n".join(offenders) + _BODY_SUFFIX
    finding = Finding(
        file=offenders[0],
        line=0,
        severity="warning",
        confidence=1.0,
        title=f"Release-shaped PR touches source: {len(offenders)} non-release file(s)",
        body=body,
    )
    return [finding]


# Test-setup files that run ahead of every test in a suite, matched on
# basename: pytest's conftest.py, and the setup modules Jest (setupTests.*,
# jest.setup.*) and Vitest (vitest.setup.*) load before each test file. A
# value pinned here is pinned for the whole suite; a pin inside one test's own
# body is not, so plain test files never count as the pin side.
_TEST_SETUP_BASENAME = "conftest.py"
_TEST_SETUP_PREFIXES = ("setupTests.", "jest.setup.", "vitest.setup.")

# Regex building blocks shared by the toggle and pin patterns below. A name is
# the quoted key a toggle or a pin is looked up by: an env var, a property key,
# or a flag name. ``_PROCESS_ENV`` reads ``process.env.NAME`` or
# ``process.env["NAME"]`` and leaves the name in ``name`` or ``bname``.
_NAME = r"[A-Za-z0-9_.\-]+"
_QUOTED_NAME = r"(?P<q>[\"'])(?P<name>" + _NAME + r")(?P=q)"
_NOT_AFTER_IDENT = r"(?<![\w$])"
_DOTTED_PREFIX = r"(?:[A-Za-z_$][\w$]*\.)*"
_PROCESS_ENV = (
    r"process\.env(?:\.(?P<name>[A-Za-z_$][\w$]*)"
    r"|\[\s*(?P<bq>[\"'])(?P<bname>" + _NAME + r")(?P=bq)\s*\])"
)
_TRUE_STRING = r"(?P<v>[\"'])(?i:true)(?P=v)"
_FALSY_STRING = r"(?P<v>[\"'])(?P<value>(?i:false|0|off))(?P=v)"

# Toggle side, the three shapes of "a call named by a string literal whose
# default is a literal true":
#   any call ``f("name", default=True)`` / ``f("name", True)`` / ``f("name", true)``
#     with exactly those two arguments;
#   an env or property lookup ``getenv("NAME", "true")``,
#     ``environ.get("NAME", "true")`` or ``getProperty("name", "true")``;
#   ``process.env.NAME ?? "true"`` or ``|| "true"``, dot or bracket access.
_TOGGLE_CALL_RE = re.compile(
    _NOT_AFTER_IDENT
    + r"(?P<callee>" + _DOTTED_PREFIX + r"[A-Za-z_$][\w$]*)\(\s*"
    + _QUOTED_NAME
    + r"\s*,\s*(?:default\s*=\s*)?(?:True|true)\s*\)"
)
_TOGGLE_LOOKUP_RE = re.compile(
    _NOT_AFTER_IDENT
    + _DOTTED_PREFIX
    + r"(?:getenv|environ\.get|getProperty)\(\s*"
    + _QUOTED_NAME
    + r"\s*,\s*" + _TRUE_STRING + r"\s*\)"
)
_TOGGLE_PROCESS_ENV_RE = re.compile(
    _NOT_AFTER_IDENT + _PROCESS_ENV + r"\s*(?:\?\?|\|\|)\s*" + _TRUE_STRING
)
_TOGGLE_RES = (_TOGGLE_CALL_RE, _TOGGLE_LOOKUP_RE, _TOGGLE_PROCESS_ENV_RE)

# A call whose last name segment is a setter (``set``, ``setFlag``,
# ``set_flag``, ``put``, ``putBoolean``) writes a value rather than declaring
# a default, so ``settings.set("name", True)`` is not a toggle.
_SETTER_SEGMENT_RE = re.compile(r"(?:[Ss]et|[Pp]ut)(?![a-z])")

# Pin side, each setting a name to "false", "0" or "off" (any case):
#   ``setenv("NAME", ...)`` (pytest's monkeypatch), ``stubEnv("NAME", ...)``
#     (Vitest) and ``setProperty("name", ...)`` (a JVM system property);
#   ``process.env.NAME = ...`` or ``process.env["NAME"] = ...``;
#   ``os.environ["NAME"] = ...``.
_PIN_CALL_RE = re.compile(
    _NOT_AFTER_IDENT
    + _DOTTED_PREFIX
    + r"(?:setenv|stubEnv|setProperty)\(\s*"
    + _QUOTED_NAME
    + r"\s*,\s*" + _FALSY_STRING + r"\s*\)"
)
_PIN_PROCESS_ENV_RE = re.compile(_NOT_AFTER_IDENT + _PROCESS_ENV + r"\s*=\s*" + _FALSY_STRING)
_PIN_ENVIRON_RE = re.compile(
    _NOT_AFTER_IDENT + _DOTTED_PREFIX + r"environ\[\s*" + _QUOTED_NAME + r"\s*\]\s*=\s*" + _FALSY_STRING
)
_PIN_RES = (_PIN_CALL_RE, _PIN_PROCESS_ENV_RE, _PIN_ENVIRON_RE)

_NAME_SEPARATORS_RE = re.compile(r"[_.\-]+")


def _is_test_setup_file(path: str) -> bool:
    """True when ``path``'s basename is a suite-wide test-setup file."""
    name = PurePosixPath(path).name
    return name == _TEST_SETUP_BASENAME or name.startswith(_TEST_SETUP_PREFIXES)


def _added_lines(file: FileDiff) -> Iterator[tuple[int, str]]:
    """``(new_line, text)`` for every added (``+``) line of ``file``, in diff order."""
    for hunk in file.hunks:
        for ln in hunk.lines:
            if ln.kind == "+" and ln.new_line is not None:
                yield ln.new_line, ln.text


def _matched_name(match: re.Match[str]) -> str:
    """The toggle or pin name a pattern captured, in either access form."""
    groups = match.groupdict()
    return groups.get("name") or groups.get("bname") or ""


def _name_tokens(name: str) -> tuple[str, ...]:
    """``name`` lowercased and split on ``_``, ``.`` and ``-``, empty parts dropped."""
    return tuple(part for part in _NAME_SEPARATORS_RE.split(name.lower()) if part)


def _pin_names_toggle(pin: tuple[str, ...], toggle: tuple[str, ...]) -> bool:
    """True when the pin's name tokens END with the toggle's name tokens."""
    return bool(toggle) and len(pin) >= len(toggle) and pin[-len(toggle):] == toggle


def toggle_pinned_off_findings(files: list[FileDiff]) -> list[Finding]:
    """Flag a default-on toggle that this PR's test setup pins off (issue #22).

    A green suite proves nothing about a toggle's shipped default when a
    suite-wide setup file turns the toggle off. Both sides must be ADDED
    lines of this PR; a toggle or a pin that only appears on a context or
    removed line is never reported.

    The toggle side is a call named by a string literal whose default is a
    literal true: ``f("name", default=True)``, ``f("name", True)`` or
    ``f("name", true)`` with exactly those two arguments, where ``f`` is any
    call except a setter (a last name segment that is ``set`` or ``put``, or
    starts with one and goes on with anything but a lowercase letter:
    ``setFlag``, ``set_flag``, ``putBoolean``); ``getenv``, ``environ.get``
    or ``getProperty`` called as ``("NAME", "true")``; or
    ``process.env.NAME ?? "true"`` / ``|| "true"``. The pin side is a line
    in a test-setup file (basename ``conftest.py``, ``setupTests.*``,
    ``jest.setup.*`` or ``vitest.setup.*``) that sets a name to ``"false"``,
    ``"0"`` or ``"off"`` (any case, either quote): ``setenv``, ``stubEnv``
    or ``setProperty`` called as ``("NAME", value)``,
    ``process.env.NAME = value`` or ``os.environ["NAME"] = value``. A pin
    names a toggle when the pin name's tokens (lowercased, split on ``_``,
    ``.`` and ``-``) END with the toggle name's tokens, so
    ``ASSISTANT_PROGRESS_NOTES`` names ``progress_notes``.

    Returns one ``warning`` per pinned toggle, anchored at the toggle's own
    line, with confidence 1.0, a body quoting the toggle call and naming
    every pinning file, ending with " (deterministic check, no model)".
    Sorted by file, line and position on the line. Pure and deterministic:
    no LLM call, no I/O, no randomness.
    """
    pins: list[tuple[str, str, str]] = []
    for file in files:
        if not _is_test_setup_file(file.path):
            continue
        for _, text in _added_lines(file):
            for pattern in _PIN_RES:
                for m in pattern.finditer(text):
                    pins.append((file.path, _matched_name(m), m.group("value")))

    toggles: dict[tuple[str, int, str], tuple[int, str]] = {}
    for file in files:
        for line, text in _added_lines(file):
            for pattern in _TOGGLE_RES:
                for m in pattern.finditer(text):
                    callee = m.groupdict().get("callee")
                    if callee and _SETTER_SEGMENT_RE.match(callee.rsplit(".", 1)[-1]):
                        continue
                    key = (file.path, line, _matched_name(m))
                    if key not in toggles or m.start() < toggles[key][0]:
                        toggles[key] = (m.start(), m.group(0))

    findings: list[Finding] = []
    for (path, line, name), (_, call) in sorted(toggles.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[1][0])):
        wanted = _name_tokens(name)
        matched = sorted({
            (pin_path, pin_name, value)
            for pin_path, pin_name, value in pins
            if _pin_names_toggle(_name_tokens(pin_name), wanted)
        })
        if not matched:
            continue
        pinned = "; ".join(f'{pin_path} sets `{pin_name}` to "{value}"' for pin_path, pin_name, value in matched)
        body = (
            f"`{call}` in {path} turns the `{name}` toggle on by default, but the test setup "
            f"turns it off for the whole suite: {pinned}. The passing tests therefore run with "
            f"the toggle off and do not exercise the shipped default."
            + _BODY_SUFFIX
        )
        findings.append(Finding(
            file=path,
            line=line,
            severity="warning",
            confidence=1.0,
            title=f'Toggle "{name}" defaults on but the test setup pins it off',
            body=body,
        ))
    return findings
