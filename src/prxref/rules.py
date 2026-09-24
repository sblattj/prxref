"""Team review rules: an operator-named file added to every review prompt.

``PRXREF_REVIEW_RULES`` (or ``--rules-file PATH``, which wins) names a
Markdown or plain-text file of team conventions. Optional front matter maps
the team's own severity words onto prxref's tiers, and the body reaches every
worker and the systemic sweep as a ``## Team review rules`` block in the
system prompt. The file is read by ``prxref review`` and by the webhook daemon
alike, before any network call, so a missing, unreadable or malformed file is
a configuration error (exit 2) naming whichever input supplied the path.

:class:`ReviewRules` is the loaded result the orchestrator duck-types: it
reads ``prompt_block("worker")`` / ``prompt_block("sweep")``, ``record()``
(the run-record and trace view, never the rules text) and ``severity_map``
(falsy means no remapping pass).

The front matter is optional and only its ``severity:`` key is read: a block
of indented ``<team word>: <tier>`` lines, where the tier is one of
:data:`MAPPABLE_SEVERITIES`. An ``applies_to:`` key (alias ``applyTo``)
scopes a scoped rules file to paths (:func:`parse_applies_to`); the always-on
file does not read it and says so in a WARNING. Every other key is ignored
and reported at INFO, so a Claude-style skill file (``name:``,
``description: |``) can be pointed at unmodified. ``spec`` is never a legal
target: it means "violates a quoted spec constraint", and a team word mapped
onto it would mint spec findings on runs with no spec at all.

The rules steer the model, and whoever controls the file controls that
steering, so the path must never come from the pull request under review.
This loader reads only a local path the operator configured: it refuses a
URL, it never reads through a forge (``get_file_content`` at the PR's head),
and nothing templates the path from PR data. Keep all three true. A path
under the working directory must still resolve under it once its symlinks
are followed (:func:`prxref.text_inputs.confine_to_cwd`), so a committed
symlink cannot point the loader at a file outside the checkout.
"""
from __future__ import annotations

import codecs
import errno
import fnmatch
import hashlib
import itertools
import json
import logging
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .llm import ConfigError
from .quality import SEVERITIES
from .text_inputs import CappedText, cap_text, check_readable_path, confine_to_cwd, decode_text

logger = logging.getLogger(__name__)

RULES_HEADING = "## Team review rules"
RESERVED_SEVERITIES: frozenset[str] = frozenset({"spec"})
MAPPABLE_SEVERITIES: frozenset[str] = SEVERITIES - RESERVED_SEVERITIES

_UNITS = ("worker", "sweep")

_WORKER_FRAMING = (
    "The team that owns this repository reviews changes against the rules below. "
    "Check this chunk against them as well. Every instruction above still binds: a "
    "finding must cite a line of the diff, follow the Confidence and No Speculation "
    "rules, and use only the Severity Vocabulary above. A rule the diff cannot show "
    "evidence for — a test run, a linked ticket, a sign-off — produces no finding."
)
_SWEEP_FRAMING = (
    "The team that owns this repository reviews changes against the rules below. In "
    "this sweep, apply only the rules that concern a whole-PR or cross-file property "
    'the digest can show (for example, "every new migration ships a rollback"); a '
    "violation of such a rule is reportable here alongside the systemic classes above. "
    "A rule about individual lines belongs to the chunk reviewers, and repeating it "
    "here only duplicates their findings. Every other instruction above still binds: "
    "cite a line shown in the digest (or `line: 0` for a file-level finding) and use "
    "only the Severity Vocabulary above."
)
_FRAMING = {"worker": _WORKER_FRAMING, "sweep": _SWEEP_FRAMING}

_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_FENCE_RE = re.compile(r"^---[ \t]*$")
_COMMENT_RE = re.compile(r"(^|[ \t])#.*$")
_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*)[ \t]*:(.*)$")
_ENTRY_RE = re.compile(
    r"""^[ \t]+(['"]?)([^:'"#\s-][^:'"#]*?)\1[ \t]*:[ \t]*(['"]?)([A-Za-z]+)\3[ \t]*$"""
)


@dataclass(frozen=True)
class ReviewRules:
    """A loaded team review-rules file.

    ``path`` is the path as configured (not resolved); ``body`` is the rules
    text with the front matter removed, capped for the prompt, and
    fingerprinted by the raw file bytes (front matter included);
    ``severity_map`` maps a casefolded team word to one of
    :data:`MAPPABLE_SEVERITIES`, in file order; ``ignored_keys`` names the
    other front-matter keys, which are not used. ``applies_to`` holds the
    file's path globs in file order, as :func:`parse_applies_to` returns
    them, or ``None`` when the file applies to every unit;
    :func:`load_review_rules` always leaves it ``None``, and neither
    :meth:`prompt_block` nor :meth:`record` reads it.
    """

    path: str
    body: CappedText
    severity_map: Mapping[str, str]
    ignored_keys: tuple[str, ...] = ()
    applies_to: tuple[str, ...] | None = None

    def prompt_block(self, unit: str) -> str:
        """The system-prompt block for one review unit (``"worker"`` or ``"sweep"``).

        The block opens with :data:`RULES_HEADING` and a framing paragraph for
        the unit: a chunk worker checks its chunk against the rules, while the
        sweep applies only whole-PR or cross-file rules. A severity paragraph
        listing the map in file order follows when the map is non-empty, then
        the body inside ``<team_rules>`` tags when it is non-empty, then a
        truncation line when the cap cut it. Deterministic, and ``""`` when
        both the body and the map are empty, so an empty file adds nothing.
        Any other ``unit`` raises ``ValueError``.
        """
        if unit not in _FRAMING:
            raise ValueError(f"unit must be one of {', '.join(_UNITS)}, got {unit!r}")
        severity_map = dict(self.severity_map or {})
        if not self.body.text and not severity_map:
            return ""
        parts = [RULES_HEADING, _FRAMING[unit]]
        if severity_map:
            entries = "; ".join(f"`{word}` → `{tier}`" for word, tier in severity_map.items())
            parts.append(
                f"Team severity words map onto that vocabulary: {entries}. Classify a "
                "problem by the team's definition, then write the mapped word in `severity`."
            )
        if self.body.text:
            parts.append(f"<team_rules>\n{self.body.text}\n</team_rules>")
        if self.body.truncated:
            parts.append(
                f"[team rules truncated: only the first {self.body.max_chars} of "
                f"{self.body.chars} characters are shown]"
            )
        return "\n\n".join(parts)

    def record(self) -> dict[str, object]:
        """Return the run-record view: ``path``, ``sha256``, ``chars``,
        ``max_chars``, ``truncated`` and ``severity_map``, JSON-native values
        only and never the rules text. ``chars`` and ``truncated`` describe
        the body after the front matter; ``sha256`` covers the whole file.
        """
        return {"path": self.path, **self.body.record(), "severity_map": dict(self.severity_map or {})}


def split_front_matter(
    text: str, *, source: str, path: str
) -> tuple[dict[str, str], tuple[str, ...], str]:
    """Split ``text`` into ``(severity_map, ignored_keys, body)``.

    ``text`` is decoded, newline-normalised file text. Front matter exists
    only when the first line is ``---`` and a later line is too; the first
    such later line closes it, and everything after it is the body. A ``---``
    first line that never closes is logged as a warning and the whole text is
    body; a ``---`` anywhere else is ordinary Markdown.

    Inside the fence a ``#`` at the start of a line or after a space or tab
    starts a comment, and blank lines are skipped. A top-level ``severity:``
    with nothing after the colon opens the map, and each following indented
    line must be ``<word>: <tier>`` (either side may be quoted). The word is
    casefolded with its whitespace collapsed, so ``Must  Fix`` becomes
    ``must fix``; the tier must be one of :data:`MAPPABLE_SEVERITIES`. Any
    other top-level key is returned in ``ignored_keys`` and its indented
    lines are skipped, which is how a multi-line ``description: |`` passes.

    A malformed severity map is a :class:`~prxref.llm.ConfigError` of the
    form ``"<source>: <path>:<line>: <problem>"`` (1-based over the whole
    file): an inline value after ``severity:``, a second ``severity`` key, a
    line that is not ``<word>: <tier>`` (a YAML list item, a nested block),
    the reserved tier ``spec``, an unknown tier, a remap of one of prxref's
    own severities (the identity ``error: error`` is allowed and ignored),
    or one word mapped to two different tiers. An empty ``severity:`` block
    is legal and yields an empty map.
    """
    lines = text.split("\n")
    close = None
    if _FENCE_RE.match(lines[0]):
        close = next((k for k in range(1, len(lines)) if _FENCE_RE.match(lines[k])), None)
        if close is None:
            logger.warning(
                "%s: rules file %r starts with '---' but never closes it; "
                "treating the whole file as rules text",
                source, path,
            )
    if close is None:
        return {}, (), text

    def fail(lineno: int, problem: str) -> ConfigError:
        return ConfigError(f"{source}: {path}:{lineno}: {problem}")

    severity_map: dict[str, str] = {}
    ignored: list[str] = []
    seen_severity = False
    in_severity = False
    for index in range(1, close):
        lineno = index + 1
        raw = lines[index]
        line = _COMMENT_RE.sub(r"\1", raw)
        if not line.strip():
            continue
        if line[0] not in " \t":
            key = _KEY_RE.match(line)
            in_severity = False
            if key is None:
                continue
            name = key.group(1)
            if name.casefold() != "severity":
                if name not in ignored:
                    ignored.append(name)
                continue
            if key.group(2).strip():
                raise fail(lineno, "'severity' must be a block of indented '<word>: <tier>' lines")
            if seen_severity:
                raise fail(lineno, "duplicate 'severity' key")
            seen_severity = in_severity = True
            continue
        if not in_severity:
            continue
        entry = _ENTRY_RE.match(line)
        if entry is None:
            raise fail(lineno, f"severity map entry must be '<word>: <tier>', got {raw.strip()!r}")
        word = " ".join(entry.group(2).split()).casefold()
        tier = entry.group(4).casefold()
        if tier in RESERVED_SEVERITIES:
            raise fail(
                lineno,
                f"'{tier}' is reserved for spec-grounded findings (PRXREF_SPEC_SOURCES / "
                "--spec); map team words to error, warning, or outofscope",
            )
        if tier not in MAPPABLE_SEVERITIES:
            raise fail(
                lineno,
                f"unknown severity '{tier}' for '{word}'; expected one of "
                f"{', '.join(sorted(MAPPABLE_SEVERITIES))}",
            )
        if word in SEVERITIES:
            if word == tier:
                continue
            raise fail(lineno, f"cannot remap prxref's own severity '{word}'")
        previous = severity_map.get(word)
        if previous is not None and previous != tier:
            raise fail(lineno, f"'{word}' is mapped twice ({previous} and {tier})")
        severity_map[word] = tier
    return severity_map, tuple(ignored), "\n".join(lines[close + 1:])


APPLIES_TO_KEYS: frozenset[str] = frozenset({"applies_to", "applyto"})

_ITEM_RE = re.compile(r"^[ \t]+-(?:[ \t]+(.*))?$")
_SINGLE_QUOTED_RE = re.compile(r"^'((?:[^']|'')*)'$")
_COLLECTION_RE = re.compile(r"^(?:[\[{]|-(?:[ \t]|$))|:(?:[ \t]|$)")
_YAML_NULL_RE = re.compile(r"^(?:~|null|Null|NULL)$")
_YAML_NON_STRING_RE = re.compile(
    r"^(?:~|null|Null|NULL|true|True|TRUE|false|False|FALSE"
    r"|[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+"
    r"|[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
    r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
)


def _glob_scalar(value: str, name: str) -> str:
    if value.startswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            raise ValueError(f"'{name}' has a malformed quoted string, got {value!r}") from None
    if value.startswith("'"):
        quoted = _SINGLE_QUOTED_RE.match(value)
        if quoted is None:
            raise ValueError(f"'{name}' has a malformed quoted string, got {value!r}")
        return quoted.group(1).replace("''", "'")
    if _COLLECTION_RE.search(value) or _YAML_NON_STRING_RE.match(value):
        raise ValueError(
            f"'{name}' entries must be glob strings, got {value!r}; "
            "quote a glob that YAML reads as another type"
        )
    return value


def _inline_globs(value: str, name: str) -> list[str]:
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            raise ValueError(
                f"'{name}' flow list must be JSON-style, with double-quoted globs such as "
                f'["**/*.java"], got {value!r}'
            ) from None
        for entry in parsed:
            if not isinstance(entry, str):
                raise ValueError(f"'{name}' entries must be glob strings, got {json.dumps(entry)}")
        return parsed
    if _YAML_NULL_RE.match(value):
        return []
    scalar = _glob_scalar(value, name)
    return scalar.split(",") if scalar.strip() else []


def _glob_problem(glob: str) -> str | None:
    if not glob:
        return "has an empty entry; every entry must be a glob"
    pattern = glob[1:] if glob[0] == "!" else glob
    if not pattern or pattern[0].isspace():
        return f"entry {glob!r} must put its glob right after '!', as in '!**/test/**'"
    if pattern[0] == "/":
        return (
            f"entry {glob!r} starts with '/', but diff paths are relative to the "
            "repository root; drop the leading '/'"
        )
    return None


def parse_applies_to(text: str, *, source: str, path: str) -> tuple[str, ...] | None:
    """Return the path globs of ``text``'s ``applies_to`` front-matter key.

    ``text`` is decoded, newline-normalised file text, and the fence, the
    comments and the line numbers are those of :func:`split_front_matter`,
    which still lists the key in its ``ignored_keys``. The key is matched
    casefolded against :data:`APPLIES_TO_KEYS`, so ``applies_to`` and its
    alias ``applyTo`` (the ``.github/instructions`` spelling) both work.
    ``None`` means the file has no such key, or no closed front matter, and
    so applies to every unit; this never logs, since
    :func:`split_front_matter` already warns about an unclosed fence.

    The value takes one of three forms, and the result is a tuple of globs,
    each stripped, in file order:

    - a scalar, bare or quoted, split on commas:
      ``applies_to: "**/*.ts, **/*.tsx"``;
    - a one-line JSON-style flow list of double-quoted globs, never split:
      ``applies_to: ["**/*.java", "!**/src/test/**"]``;
    - nothing after the colon, then indented ``- <glob>`` lines, bare or
      quoted, never split.

    A glob starting with ``!`` excludes paths. The globs are validated here
    but not matched. Every problem is a :class:`~prxref.llm.ConfigError` of
    the form ``"<source>: <path>:<line>: <problem>"``, where the line is
    1-based over the whole file: the entry's own line in a block list, the
    key's line otherwise. The problems are a second ``applies_to`` or
    ``applyTo`` key; an empty value (``[]``, ``""``, ``~`` or no entries); an
    empty entry; an entry that is not a string (``[1]``, ``- 42``,
    ``- true``, ``- a: b``, a nested list or mapping); a malformed flow list
    or quoted string (single-quoted flow items included); a flow list that
    does not close on its own line; an inline value followed by indented
    lines; an indented line that is not ``- <glob>``; a block scalar
    (``|`` or ``>``); a ``!`` not followed directly by a glob; a glob
    starting with ``/``, since diff paths are relative; and a list with
    only ``!`` globs, which can match no path.
    """
    lines = text.split("\n")
    if not _FENCE_RE.match(lines[0]):
        return None
    close = next((k for k in range(1, len(lines)) if _FENCE_RE.match(lines[k])), None)
    if close is None:
        return None

    def fail(lineno: int, problem: str) -> ConfigError:
        return ConfigError(f"{source}: {path}:{lineno}: {problem}")

    name = ""
    key_line = 0
    inline = ""
    in_key = False
    entries: list[tuple[int, str]] = []
    for index in range(1, close):
        lineno = index + 1
        raw = lines[index]
        line = _COMMENT_RE.sub(r"\1", raw)
        if not line.strip():
            continue
        if line[0] not in " \t":
            key = _KEY_RE.match(line)
            in_key = key is not None and key.group(1).casefold() in APPLIES_TO_KEYS
            if key is None or not in_key:
                continue
            if key_line:
                raise fail(lineno, f"duplicate '{key.group(1)}' key ('{name}' is already set on line {key_line})")
            name, key_line, inline = key.group(1), lineno, key.group(2).strip()
            if inline[:1] in ("|", ">"):
                raise fail(
                    lineno,
                    f"'{name}' cannot be a block scalar ('|' or '>'); give a glob, a "
                    "comma-separated string of globs, or a list",
                )
            continue
        if not in_key:
            continue
        if inline.startswith("["):
            raise fail(
                lineno,
                f"'{name}' flow list must close on the line that opens it; write a long "
                "list as indented '- <glob>' lines",
            )
        if inline:
            raise fail(lineno, f"'{name}' has a value after the colon, so it cannot continue on an indented line")
        item = _ITEM_RE.match(line)
        if item is None:
            raise fail(lineno, f"'{name}' list entries must be '- <glob>' lines, got {raw.strip()!r}")
        try:
            entries.append((lineno, _glob_scalar((item.group(1) or "").strip(), name)))
        except ValueError as exc:
            raise fail(lineno, str(exc)) from None
    if not key_line:
        return None
    if inline:
        try:
            entries = [(key_line, glob) for glob in _inline_globs(inline, name)]
        except ValueError as exc:
            raise fail(key_line, str(exc)) from None
    if not entries:
        raise fail(
            key_line,
            f"'{name}' is empty; list at least one glob, or omit the key to apply the "
            "file to every unit",
        )
    globs: list[str] = []
    for lineno, entry in entries:
        glob = entry.strip()
        problem = _glob_problem(glob)
        if problem is not None:
            raise fail(lineno, f"'{name}' {problem}")
        globs.append(glob)
    if all(glob.startswith("!") for glob in globs):
        raise fail(
            key_line,
            f"'{name}' has only negated ('!') globs, so it matches no path; add a glob "
            "the file applies to",
        )
    return tuple(globs)


def load_review_rules(path: str | None, *, max_chars: int, source: str) -> ReviewRules | None:
    """Load the team review-rules file at ``path``, capped at ``max_chars``.

    An empty, whitespace-only or ``None`` path means "no rules" and returns
    ``None``. ``source`` is the input that supplied the path
    (``--rules-file`` or ``PRXREF_REVIEW_RULES``), and every failure is a
    :class:`~prxref.llm.ConfigError` whose message starts with it: a cap
    below 1, a URL instead of a local path, a missing, unreadable or
    non-regular file, a symlink that escapes the working directory, invalid
    UTF-8, NUL bytes, or malformed front matter (:func:`split_front_matter`).

    The file is read whole, hashed, decoded (a BOM dropped, CRLF and CR
    folded to LF) and split; the cap applies to the stripped body after the
    front matter, which is why this does not stream the way
    :func:`prxref.text_inputs.read_capped_file` does (a streamed cap could
    cut inside the front matter). ``sha256`` covers the raw file bytes, so
    it equals ``shasum -a 256`` of the file and does not move with the cap.
    A truncated body is logged once as a WARNING naming
    ``PRXREF_REVIEW_RULES_MAX_CHARS``, an empty body with no map as a
    WARNING, and ignored front-matter keys at INFO; each still returns the
    loaded rules, since the file was configured. An ``applies_to`` or
    ``applyTo`` key is neither parsed nor validated here, stays in
    ``ignored_keys`` and leaves ``applies_to`` ``None``; it is logged as a
    WARNING instead of at INFO, because this file reaches every unit
    whatever it says.
    """
    if path is None or not path.strip():
        return None
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise ConfigError(f"{source}: PRXREF_REVIEW_RULES_MAX_CHARS must be at least 1, got {max_chars!r}")
    if _URL_RE.match(path.strip()):
        raise ConfigError(f"{source}: rules must be a local file path, not a URL: {path!r}")
    try:
        resolved = check_readable_path(path, confine=True)
        with open(resolved, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise OSError(errno.EINVAL, "not a regular file", path)
            raw = fh.read()
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "strerror", None) or str(exc)
        raise ConfigError(f"{source}: cannot read rules file {path!r}: {reason}") from exc
    sha256 = hashlib.sha256(raw).hexdigest()
    try:
        text = decode_text(raw)
    except UnicodeDecodeError as exc:
        offset = exc.start + (len(codecs.BOM_UTF8) if raw.startswith(codecs.BOM_UTF8) else 0)
        raise ConfigError(
            f"{source}: rules file {path!r} is not UTF-8 text ({exc.reason} at byte {offset})"
        ) from exc
    if "\x00" in text:
        raise ConfigError(f"{source}: rules file {path!r} contains NUL bytes; expected Markdown or plain text")
    severity_map, ignored, body = split_front_matter(text, source=source, path=path)
    capped = cap_text(body.strip(), max_chars, sha256=sha256)
    scoping = [key for key in ignored if key.casefold() in APPLIES_TO_KEYS]
    unused = [key for key in ignored if key not in scoping]
    if unused:
        logger.info("%s: ignoring front-matter keys other than 'severity': %s", source, ", ".join(unused))
    if scoping:
        logger.warning(
            "%s: rules file %r sets %s, which only a scoped rules file (PRXREF_SCOPED_RULES / "
            "--scoped-rules) reads; this file still reaches every unit",
            source, path, ", ".join(f"'{key}'" for key in scoping),
        )
    if capped.truncated:
        logger.warning(
            "%s: rules file %r has %d characters (after front matter); only the first %d "
            "reach the prompt — raise PRXREF_REVIEW_RULES_MAX_CHARS",
            source, path, capped.chars, max_chars,
        )
    if not capped.text and not severity_map:
        logger.warning("%s: rules file %r is empty; no rules injected", source, path)
    return ReviewRules(path=path, body=capped, severity_map=severity_map, ignored_keys=ignored)


_ANY_DIRS = "**/"


def match_globs(path: str, patterns: Sequence[str]) -> bool:
    """True when ``patterns``, a scoped rules file's ``applies_to`` list, selects ``path``.

    ``path`` is a diff path: POSIX, relative to the repository root. Each
    pattern is matched against the whole path with
    :func:`fnmatch.fnmatchcase`, as ``PRXREF_SIZE_IGNORE_GLOBS`` is, so the
    match is case-sensitive on every host and ``*`` crosses ``/``. A pattern
    that starts with ``!`` negates the glob after it.

    The path is selected when at least one positive pattern matches it and no
    negation does. Order does not matter: a negation vetoes the path wherever
    it sits in the list, and a positive pattern after it cannot bring the path
    back, unlike the last-match-wins rule of ``.gitignore``. An empty list, or
    a list of negations only, selects nothing. A leading ``!`` always
    negates, so a pattern for a path that itself starts with ``!`` starts
    with ``?`` instead.

    One addition to plain ``fnmatch``: a ``**/`` that starts the pattern or
    follows a ``/`` also matches zero directories, so ``**/*.java`` selects a
    root-level ``Foo.java``, ``!**/src/test/**`` vetoes ``src/test/A.java``,
    and ``src/**/*.java`` selects ``src/Foo.java``. Stdlib ``fnmatch`` needs a
    ``/`` there, and ``PRXREF_SIZE_IGNORE_GLOBS`` keeps that stricter match.
    A pattern matches when it, or any copy of it with some of those ``**/``
    removed, matches under ``fnmatchcase``; a run such as ``**/**/`` counts as
    one. Each of ``k`` such ``**/`` doubles the copies tried, so the cost is
    ``2**k`` ``fnmatchcase`` calls at worst.
    """
    selected = False
    for pattern in patterns:
        if pattern.startswith("!"):
            if _glob_matches(path, pattern[1:]):
                return False
        elif not selected:
            selected = _glob_matches(path, pattern)
    return selected


def _glob_matches(path: str, pattern: str) -> bool:
    """True when ``path`` matches ``pattern`` or a copy of it with some ``**/`` removed."""
    head, *tail = _split_any_dirs(pattern)
    for kept in itertools.product((_ANY_DIRS, ""), repeat=len(tail)):
        variant = head + "".join(sep + piece for sep, piece in zip(kept, tail, strict=True))
        if fnmatch.fnmatchcase(path, variant):
            return True
    return False


def _split_any_dirs(pattern: str) -> list[str]:
    """Split ``pattern`` at each ``**/`` that starts it or follows a ``/``, a run counting as one."""
    pieces: list[str] = []
    start = index = 0
    while index < len(pattern):
        if pattern.startswith(_ANY_DIRS, index) and (index == 0 or pattern[index - 1] == "/"):
            pieces.append(pattern[start:index])
            index += len(_ANY_DIRS)
            while pattern.startswith(_ANY_DIRS, index):
                index += len(_ANY_DIRS)
            start = index
        else:
            index += 1
    pieces.append(pattern[start:])
    return pieces


SCOPED_RULES_MAX_FILES = 50


@dataclass(frozen=True)
class ScopedBlock:
    """One review unit's team-rules block, as :meth:`ScopedRules.unit_block` builds it.

    ``text`` is the system-prompt block (``""`` adds nothing). ``files`` holds
    the scoped files the block carries, in load order: every selected file
    that was not omitted, the truncated one included. ``truncated`` is the
    path of the file the per-unit cap cut short, or ``None``; ``omitted``
    holds the paths of the selected files the cap left out entirely, in load
    order.

    Building a block logs nothing. The orchestrator reads ``truncated`` and
    ``omitted`` across every unit of a run and logs one WARNING for the run
    when any unit reports either, naming ``PRXREF_SCOPED_RULES_MAX_CHARS``.
    """

    text: str
    files: tuple[ReviewRules, ...] = ()
    truncated: str | None = None
    omitted: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScopedRules:
    """Path-scoped team review rules, as :func:`load_scoped_rules` loads them.

    ``entries`` holds the configured files and directories as given, blank
    entries dropped. ``files`` holds one :class:`ReviewRules` per rules file
    in load order: the configured entry order, with each directory's files in
    its place in name order. Each file carries its ``path`` as configured (a
    directory's file is the directory joined with the file name, never
    resolved), its ``body`` after the front matter, capped at
    ``PRXREF_REVIEW_RULES_MAX_CHARS`` and fingerprinted by the raw file
    bytes, its own ``severity_map``, its unused front-matter keys in
    ``ignored_keys`` (``applies_to`` and ``applyTo`` are used, so never
    listed), and its ``applies_to`` globs, or ``None`` when it reaches every
    unit. ``severity_map`` merges the files' maps, each team word at its
    first position in load order; no two files map one word to different
    tiers.

    The orchestrator reads :meth:`select` and :meth:`unit_block` per review
    unit, :meth:`merged_severity_map` for the severity-remapping pass, and
    :meth:`record` for the run record.
    """

    entries: tuple[str, ...]
    files: tuple[ReviewRules, ...]
    severity_map: Mapping[str, str]

    def select(self, paths: Sequence[str]) -> tuple[ReviewRules, ...]:
        """The files that reach a review unit whose diff touches ``paths``, in load order.

        A file with no ``applies_to`` reaches every unit, even one with no
        paths. Any other file is selected when :func:`match_globs` selects at
        least one of ``paths`` with its ``applies_to``, so a ``!`` pattern
        vetoes only the paths it matches. A chunk's paths are the ``path`` of
        each of its files plus the ``old_path`` of a renamed one, so a rules
        file scoped to a file's old location still reaches the rename; the
        sweep's paths are the union of every chunk's, which selects the union
        of the chunks' files. A bare string counts as one path, and empty or
        ``None`` entries are skipped, so a caller may pass ``(f.path,
        f.old_path)`` for every file.
        """
        if isinstance(paths, str):
            paths = (paths,)
        wanted = tuple(path for path in paths if path)
        return tuple(
            rules for rules in self.files
            if rules.applies_to is None or any(match_globs(path, rules.applies_to) for path in wanted)
        )

    def merged_severity_map(self, always_on: ReviewRules | None) -> dict[str, str]:
        """The run-wide severity map: the always-on file's map, then the scoped files' map.

        ``always_on`` is the loaded ``PRXREF_REVIEW_RULES`` file, or ``None``.
        The result is ``{**always_on.severity_map, **self.severity_map}``, a
        new plain dict: always-on words first in their order, then the words
        only scoped files map, in load order. It is what the orchestrator's
        severity-remapping pass applies, so a scoped file's map applies even
        when no always-on file is set. It is conflict-free when the same
        ``always_on`` was passed to :func:`load_scoped_rules`, which refuses
        a word the two map to different tiers.
        """
        always_map = dict(always_on.severity_map or {}) if always_on is not None else {}
        return {**always_map, **self.severity_map}

    def unit_block(
        self, unit: str, paths: Sequence[str], always_on: ReviewRules | None, *, max_chars: int
    ) -> ScopedBlock:
        """Build the team-rules block for one review unit (``"worker"`` or ``"sweep"``).

        ``paths`` are the unit's diff paths, passed to :meth:`select` (for the
        sweep, the union of every chunk's paths). ``always_on`` is the loaded
        ``PRXREF_REVIEW_RULES`` file, or ``None``. ``max_chars`` is the
        per-unit cap on scoped-rules text, ``PRXREF_SCOPED_RULES_MAX_CHARS``.

        When no scoped file is selected and :meth:`merged_severity_map` equals
        the always-on map (no scoped file maps a word the always-on file does
        not), the text is exactly ``always_on.prompt_block(unit)``, or ``""``
        with no always-on file, so a unit the scoped rules do not reach gets
        the prompt it had without them.

        Otherwise the block is the always-on file's
        :meth:`ReviewRules.prompt_block` rendered with the merged map: one
        :data:`RULES_HEADING`, the unit's framing paragraph, one severity
        paragraph over the merged map (omitted when it is empty), then the
        always-on body in ``<team_rules>`` and its truncation line. Each
        selected scoped file follows in load order as ``<team_rules
        source="<path>" applies_to="<glob>, <glob>">``, the ``applies_to``
        attribute left out for a file that reaches every unit and ``&`` and
        ``"`` in either value written as ``&amp;`` and ``&quot;``. A file with
        an empty body adds no element; its map is already in the severity
        paragraph. A scoped word the always-on file also maps adds nothing to
        that paragraph. The text is ``""`` when the block would hold no map
        and no rules text.

        The cap counts scoped body characters only, not the always-on body
        (capped per file on its own) or the tags. Whole files go in while
        they fit, and one that fits exactly is not cut. The first file that
        does not fit is truncated to the room left and followed by the same
        truncation line a file cut by ``PRXREF_REVIEW_RULES_MAX_CHARS`` gets;
        when no room is left at all, it is omitted instead. Every later file
        with a body is omitted, and one marker naming the omitted paths
        closes the block. Pure: it logs nothing (see :class:`ScopedBlock`).
        Any other ``unit``, or ``max_chars`` below 1, raises ``ValueError``.
        """
        if unit not in _FRAMING:
            raise ValueError(f"unit must be one of {', '.join(_UNITS)}, got {unit!r}")
        if max_chars < 1:
            raise ValueError(f"max_chars must be at least 1, got {max_chars!r}")
        selected = self.select(paths)
        always_map = dict(always_on.severity_map or {}) if always_on is not None else {}
        merged = self.merged_severity_map(always_on)
        if not selected and merged == always_map:
            return ScopedBlock(always_on.prompt_block(unit) if always_on is not None else "")
        body = always_on.body if always_on is not None else _NO_BODY
        head = ReviewRules(path="", body=body, severity_map=merged).prompt_block(unit)
        parts: list[str] = []
        files: list[ReviewRules] = []
        omitted: list[str] = []
        truncated: str | None = None
        room = max_chars
        for rules in selected:
            text = rules.body.text
            if text and not room:
                omitted.append(rules.path)
                continue
            files.append(rules)
            if not text:
                continue
            shown = text[:room]
            parts.append(_team_rules_element(rules, shown))
            if len(shown) < len(text):
                truncated = rules.path
            if len(shown) < len(text) or rules.body.truncated:
                parts.append(_truncation_note(len(shown), rules.body.chars))
            room -= len(shown)
        if omitted:
            parts.append(
                f"[team rules omitted: {', '.join(omitted)} (over the {max_chars}-character limit "
                "on scoped rules for one review unit)]"
            )
        if parts:
            head = head or f"{RULES_HEADING}\n\n{_FRAMING[unit]}"
            text = "\n\n".join([head, *parts])
        else:
            text = head
        return ScopedBlock(text, tuple(files), truncated, tuple(omitted))

    def record(self) -> dict[str, object]:
        """Return the run-record view, JSON-native values only and never the rules text.

        The shape is ``{"entries": [<entry>, ...], "files": [<file>, ...]}``.
        ``entries`` lists the configured entries as given, blank ones
        dropped. ``files`` has one object per loaded file in load order: the
        keys of :meth:`ReviewRules.record` (``path``, ``sha256``, ``chars``,
        ``max_chars``, ``truncated``, ``severity_map``) followed by
        ``applies_to``, the file's globs as a list in file order, ``!``
        patterns kept, or ``null`` when the file reaches every unit.
        ``files`` is ``[]`` when the configured directories hold no rules
        file. The per-unit cap and the per-unit selections are not part of
        it.
        """
        return {
            "entries": list(self.entries),
            "files": [
                {**rules.record(), "applies_to": list(rules.applies_to) if rules.applies_to is not None else None}
                for rules in self.files
            ],
        }


_NO_BODY = CappedText(text="", sha256="", chars=0, truncated=False, max_chars=1)


def _team_rules_element(rules: ReviewRules, text: str) -> str:
    """Wrap ``text``, a scoped file's body or its first part, in its ``<team_rules>`` element."""
    attributes = f' source="{_attribute(rules.path)}"'
    if rules.applies_to is not None:
        attributes += f' applies_to="{_attribute(", ".join(rules.applies_to))}"'
    return f"<team_rules{attributes}>\n{text}\n</team_rules>"


def _attribute(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;")


def _truncation_note(shown: int, chars: int) -> str:
    """The line :meth:`ReviewRules.prompt_block` writes after a truncated body."""
    return f"[team rules truncated: only the first {shown} of {chars} characters are shown]"


def load_scoped_rules(
    entries: str | Sequence[str] | None,
    *,
    max_chars: int,
    source: str,
    always_on: ReviewRules | None = None,
) -> ScopedRules | None:
    """Load the path-scoped rules files that ``entries`` names, each capped at ``max_chars``.

    ``entries`` is the ``PRXREF_SCOPED_RULES`` list, or the ``--scoped-rules``
    flags that replace it; a bare string counts as one entry. Blank entries
    are dropped, so ``None``, ``[]`` and ``[""]`` mean "off" and return
    ``None``. ``max_chars`` is the per-file cap,
    ``PRXREF_REVIEW_RULES_MAX_CHARS``. ``source`` names the input that
    supplied the list, and every failure is a :class:`~prxref.llm.ConfigError`
    whose message starts with it and names the path, followed by ``:<line>``
    when the problem sits on a line of the file.

    Each entry is a local file or a directory. A directory is read one level
    deep: every name directly inside it that ends in ``.md`` (case-sensitive)
    and does not start with ``.`` is a rules file, in code-point order of the
    names; a subdirectory with such a name is an error, not a skip. Files are
    loaded in entry order, a directory's files taking its place. A file
    reached twice (listed twice, or listed beside its directory) is loaded
    once, at its first position, and logged at INFO. More than
    :data:`SCOPED_RULES_MAX_FILES` files in all is an error, raised before any
    file is read. A directory with no rules file is a WARNING, so a run whose
    entries hold no rules file at all returns a :class:`ScopedRules` with no
    ``files``.

    Every file is read as :func:`load_review_rules` reads its one file: an
    entry that is a URL is refused; the file must be a regular file of
    strict UTF-8 with no NUL bytes; and a path under the working directory
    must still resolve under it once its symlinks are followed
    (:func:`prxref.text_inputs.confine_to_cwd`). So a symlinked rules file
    that stays inside the working directory is read, one that escapes it is
    an error and never a silent skip, and a directory entry is confined the
    same way. The front matter is split by :func:`split_front_matter`, and
    ``applies_to`` (alias ``applyTo``) is parsed by :func:`parse_applies_to`,
    so ``applies_to: []``, a glob with a leading ``/`` and a malformed
    severity map are errors. The body is capped as the always-on body is. A
    truncated body is logged as a WARNING naming
    ``PRXREF_REVIEW_RULES_MAX_CHARS``, an empty body with no map as a
    WARNING, a file with no ``applies_to`` at INFO (it reaches every unit),
    and unused front-matter keys at INFO; each file is still loaded.

    The files' severity maps merge into :attr:`ScopedRules.severity_map`, and
    one word mapped to different tiers by two files is an error naming both,
    each with its line. ``always_on``, the loaded ``PRXREF_REVIEW_RULES``
    file, takes part in that check but is not merged, so
    ``{**always_on.severity_map, **scoped.severity_map}`` is a conflict-free
    run-wide map. A cap below 1 is an error too.
    """
    if isinstance(entries, str):
        entries = [entries]
    configured = tuple(entry for entry in entries or () if entry.strip())
    if not configured:
        return None
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise ConfigError(f"{source}: PRXREF_REVIEW_RULES_MAX_CHARS must be at least 1, got {max_chars!r}")
    paths: list[str] = []
    seen: set[str] = set()
    for entry in configured:
        if _URL_RE.match(entry.strip()):
            raise ConfigError(f"{source}: scoped rules must be local file or directory paths, not a URL: {entry!r}")
        for path in _scoped_rules_paths(entry, source=source):
            real = os.path.realpath(path)
            if real in seen:
                logger.info("%s: rules file %r is reached more than once; it is loaded once", source, path)
                continue
            seen.add(real)
            paths.append(path)
    if len(paths) > SCOPED_RULES_MAX_FILES:
        raise ConfigError(
            f"{source}: {len(paths)} rules files are configured, over the limit of {SCOPED_RULES_MAX_FILES}; "
            f"the first one past it is {paths[SCOPED_RULES_MAX_FILES]!r}"
        )
    owners: dict[str, tuple[str, str]] = {}
    if always_on is not None:
        for word, tier in (always_on.severity_map or {}).items():
            owners[word] = (tier, f"the always-on rules file {always_on.path!r}")
    merged: dict[str, str] = {}
    files: list[ReviewRules] = []
    for path in paths:
        rules, lines = _load_scoped_file(path, max_chars=max_chars, source=source)
        for word, tier in rules.severity_map.items():
            where = f"{path}:{lines[word]}" if word in lines else path
            held_tier, held_where = owners.setdefault(word, (tier, where))
            if held_tier != tier:
                raise ConfigError(
                    f"{source}: {where}: '{word}' is mapped to {tier} here but to {held_tier} in "
                    f"{held_where}; map each team word to one tier across all rules files"
                )
            merged.setdefault(word, tier)
        files.append(rules)
    return ScopedRules(entries=configured, files=tuple(files), severity_map=merged)


def _scoped_rules_paths(entry: str, *, source: str) -> list[str]:
    """The rules-file paths ``entry`` stands for: itself, or a directory's ``*.md`` files by name."""
    if not os.path.isdir(entry):
        return [entry]
    try:
        confine_to_cwd(entry)
        with os.scandir(entry) as listing:
            names = sorted(e.name for e in listing if e.name.endswith(".md") and not e.name.startswith("."))
    except OSError as exc:
        raise ConfigError(f"{source}: cannot read rules directory {entry!r}: {_reason(exc)}") from exc
    if not names:
        logger.warning("%s: rules directory %r holds no *.md files; no rules loaded from it", source, entry)
    return [os.path.join(entry, name) for name in names]


def _load_scoped_file(path: str, *, max_chars: int, source: str) -> tuple[ReviewRules, dict[str, int]]:
    """Load one scoped rules file; also return the line of each team word its severity map sets."""
    text, sha256 = _read_scoped_file(path, source=source)
    severity_map, ignored, body = split_front_matter(text, source=source, path=path)
    applies_to = parse_applies_to(text, source=source, path=path)
    capped = cap_text(body.strip(), max_chars, sha256=sha256)
    unused = tuple(key for key in ignored if key.casefold() not in APPLIES_TO_KEYS)
    if unused:
        logger.info(
            "%s: rules file %r: ignoring front-matter keys other than 'severity' and 'applies_to': %s",
            source, path, ", ".join(unused),
        )
    if applies_to is None:
        logger.info("%s: rules file %r has no 'applies_to' key, so it reaches every unit", source, path)
    if capped.truncated:
        logger.warning(
            "%s: rules file %r has %d characters (after front matter); only the first %d "
            "reach the prompt — raise PRXREF_REVIEW_RULES_MAX_CHARS",
            source, path, capped.chars, max_chars,
        )
    if not capped.text and not severity_map:
        logger.warning("%s: rules file %r is empty; no rules injected", source, path)
    rules = ReviewRules(
        path=path, body=capped, severity_map=severity_map, ignored_keys=unused, applies_to=applies_to,
    )
    return rules, (_severity_lines(text) if severity_map else {})


def _read_scoped_file(path: str, *, source: str) -> tuple[str, str]:
    """Read, hash and decode one rules file as :func:`load_review_rules` does: ``(text, sha256)``."""
    try:
        resolved = check_readable_path(path, confine=True)
        with open(resolved, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise OSError(errno.EINVAL, "not a regular file", path)
            raw = fh.read()
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{source}: cannot read rules file {path!r}: {_reason(exc)}") from exc
    try:
        text = decode_text(raw)
    except UnicodeDecodeError as exc:
        offset = exc.start + (len(codecs.BOM_UTF8) if raw.startswith(codecs.BOM_UTF8) else 0)
        raise ConfigError(
            f"{source}: rules file {path!r} is not UTF-8 text ({exc.reason} at byte {offset})"
        ) from exc
    if "\x00" in text:
        raise ConfigError(f"{source}: rules file {path!r} contains NUL bytes; expected Markdown or plain text")
    return text, hashlib.sha256(raw).hexdigest()


def _severity_lines(text: str) -> dict[str, int]:
    """Map each team word in ``text``'s ``severity:`` block to the 1-based line that first maps it."""
    lines = text.split("\n")
    close = 0
    if _FENCE_RE.match(lines[0]):
        close = next((k for k in range(1, len(lines)) if _FENCE_RE.match(lines[k])), 0)
    found: dict[str, int] = {}
    in_severity = False
    for index in range(1, close):
        line = _COMMENT_RE.sub(r"\1", lines[index])
        if not line.strip():
            continue
        if line[0] not in " \t":
            key = _KEY_RE.match(line)
            in_severity = key is not None and key.group(1).casefold() == "severity"
            continue
        entry = _ENTRY_RE.match(line) if in_severity else None
        if entry is not None:
            found.setdefault(" ".join(entry.group(2).split()).casefold(), index + 1)
    return found


def _reason(exc: BaseException) -> str:
    return getattr(exc, "strerror", None) or str(exc)
