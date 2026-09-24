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
import hashlib
import json
import logging
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass

from .llm import ConfigError
from .quality import SEVERITIES
from .text_inputs import CappedText, cap_text, check_readable_path, decode_text

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
