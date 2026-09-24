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
:data:`MAPPABLE_SEVERITIES`. Every other key is ignored and reported at INFO,
so a Claude-style skill file (``name:``, ``description: |``) can be pointed
at unmodified. ``spec`` is never a legal target: it means "violates a quoted
spec constraint", and a team word mapped onto it would mint spec findings on
runs with no spec at all.

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
import logging
import os
import re
import stat
from collections.abc import Mapping, Sequence
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
    other front-matter keys, which are not used.
    """

    path: str
    body: CappedText
    severity_map: Mapping[str, str]
    ignored_keys: tuple[str, ...] = ()

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
    loaded rules, since the file was configured.
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
    if ignored:
        logger.info("%s: ignoring front-matter keys other than 'severity': %s", source, ", ".join(ignored))
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
