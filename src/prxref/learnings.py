"""Team learnings: glob-scoped dismissals that suppress repeat findings (#33).

A reviewer who answers a prxref finding with "won't fix, this is on
purpose" has settled a question the next review of a different PR will
ask again. A learnings file records that answer once, per repository, in
a form the team reviews and edits through ordinary pull requests::

    [[learning]]
    id = "legacy-raw-sql"
    paths = ["src/legacy/**"]
    claim = "Raw SQL string built by concatenation"
    rule = "SEC-3"                       # optional; omitted = any rule
    reason = "Inputs are compile-time constants in this module"
    source = "https://github.com/o/r/pull/12#discussion_r1"
    added = 2026-10-07                   # optional
    expires = 2027-04-01                 # optional

:func:`load_learnings` reads the file ``PRXREF_LEARNINGS_FILE`` (or
``learnings_file`` in ``.prxref.toml``) names; a missing, unreadable or
malformed one is a :class:`~prxref.llm.ConfigError` (exit 2) naming the
input that supplied the path. The review only READS it:
:func:`prxref.quality.apply_learning_suppression` drops an active finding
whose path a learning's ``paths`` globs select (:func:`prxref.rules.match_globs`),
whose ``rule`` matches the learning's (any rule when the learning names
none), and whose title and body share the learning's claim tokens. A
learning past its ``expires`` date suppresses nothing and is counted in
the run record; one whose ``added`` date is more than
:data:`STALE_AFTER_DAYS` days old is logged as a WARNING so the team
revisits it.

:func:`harvest_candidates` is the writer's half, and it only PROPOSES:
``prxref learnings harvest --pr-url ...`` turns every prxref-rooted thread
a human closed as won't-fix (:attr:`prxref.forges.base.Thread.wont_fix`)
into a candidate entry and prints it as TOML (:func:`render_toml`). prxref
never writes the learnings file into a repository; a human commits the
candidates they agree with.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import stat
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from .forges.base import Thread
from .llm import ConfigError
from .quality import _tokens
from .text_inputs import check_readable_path, decode_text

logger = logging.getLogger(__name__)

#: The largest learnings file :func:`load_learnings` accepts, in characters.
LEARNINGS_FILE_MAX_CHARS: int = 262_144

#: A learning added more than this many days ago is logged as stale.
STALE_AFTER_DAYS: int = 180

#: The array-of-tables name each entry sits under.
TABLE: str = "learning"

#: The ``drop_reason`` prefix the suppression pass writes.
DROP_PREFIX: str = "suppressed by learning: "

_REQUIRED = ("id", "paths", "claim")
_OPTIONAL = ("rule", "reason", "source", "added", "expires")
_KEYS = frozenset(_REQUIRED + _OPTIONAL)


@dataclass(frozen=True)
class Learning:
    """One entry of the learnings file.

    ``claim_tokens`` are the claim's content tokens, the same tokenizer the
    settled-thread gate uses; a claim with none is refused at load time, so
    an entry can never match every finding of a path.
    """

    id: str
    paths: tuple[str, ...]
    claim: str
    claim_tokens: frozenset[str]
    rule: str | None = None
    reason: str = ""
    source: str = ""
    added: date | None = None
    expires: date | None = None

    def expired(self, today: date) -> bool:
        """True when ``expires`` is set and strictly before ``today``."""
        return self.expires is not None and self.expires < today


@dataclass(frozen=True)
class Learnings:
    """A loaded learnings file: its entries and the date expiry is judged on."""

    path: str
    sha256: str
    entries: tuple[Learning, ...]
    today: date
    stale: tuple[str, ...] = field(default=())

    @property
    def active(self) -> tuple[Learning, ...]:
        """The entries that have not expired by :attr:`today`."""
        return tuple(e for e in self.entries if not e.expired(self.today))

    @property
    def expired_count(self) -> int:
        """How many entries expired before :attr:`today`."""
        return sum(1 for e in self.entries if e.expired(self.today))

    def record(self) -> dict[str, Any]:
        """The run record's ``learnings`` value before the pass runs.

        ``{"file", "sha256", "loaded", "expired", "suppressed"}``: ``loaded``
        counts every entry in the file, ``expired`` the ones skipped for a
        past ``expires`` date, and ``suppressed`` starts empty — the
        orchestrator fills it with one ``{learning_id, finding_id, title}``
        per finding the pass dropped.
        """
        return {
            "file": self.path,
            "sha256": self.sha256,
            "loaded": len(self.entries),
            "expired": self.expired_count,
            "suppressed": [],
        }


def _today() -> date:
    return datetime.now(UTC).date()


def _date_field(value: object, *, where: str) -> date:
    if isinstance(value, datetime):
        raise ValueError(f"{where} must be a date (YYYY-MM-DD), got a date-time")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise ValueError(f"{where} must be a date (YYYY-MM-DD), got {value!r}")


def _string_field(value: object, *, where: str, required: bool) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{where} must be a string, got {value!r}")
    if required and not value.strip():
        raise ValueError(f"{where} must not be empty")
    return value.strip()


def _parse_entry(index: int, raw: object, seen: set[str]) -> Learning:
    where = f"[[{TABLE}]] #{index}"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} is not a table")
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise ValueError(
            f"{where} has unknown key(s) {', '.join(map(repr, unknown))}; "
            f"allowed: {', '.join(_REQUIRED + _OPTIONAL)}"
        )
    missing = [key for key in _REQUIRED if key not in raw]
    if missing:
        raise ValueError(f"{where} is missing {', '.join(map(repr, missing))}")
    entry_id = _string_field(raw["id"], where=f"{where} 'id'", required=True)
    where = f"learning {entry_id!r}"
    if entry_id in seen:
        raise ValueError(f"{where}: duplicate id")
    seen.add(entry_id)
    paths = raw["paths"]
    if isinstance(paths, str):
        paths = [paths]
    if (
        not isinstance(paths, list)
        or not paths
        or not all(isinstance(p, str) and p.strip() for p in paths)
    ):
        raise ValueError(f"{where}: 'paths' must be a non-empty array of glob strings")
    globs = tuple(p.strip() for p in paths)
    if all(g.startswith("!") for g in globs):
        raise ValueError(f"{where}: 'paths' holds only negations, so it selects nothing")
    claim = _string_field(raw["claim"], where=f"{where} 'claim'", required=True)
    tokens = frozenset(_tokens(claim))
    if not tokens:
        raise ValueError(
            f"{where}: 'claim' {claim!r} has no content word of four or more letters to match on"
        )
    rule: str | None = None
    if "rule" in raw:
        rule = _string_field(raw["rule"], where=f"{where} 'rule'", required=False) or None
    reason = _string_field(raw.get("reason", ""), where=f"{where} 'reason'", required=False)
    source = _string_field(raw.get("source", ""), where=f"{where} 'source'", required=False)
    added = _date_field(raw["added"], where=f"{where} 'added'") if "added" in raw else None
    expires = _date_field(raw["expires"], where=f"{where} 'expires'") if "expires" in raw else None
    return Learning(
        id=entry_id, paths=globs, claim=claim, claim_tokens=tokens, rule=rule,
        reason=reason, source=source, added=added, expires=expires,
    )


def parse_learnings(text: str) -> tuple[Learning, ...]:
    """Parse learnings TOML ``text``; every problem is a ``ValueError`` naming it.

    The document may hold only ``[[learning]]`` tables (a file with none is
    valid and loads no entries).
    """
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML: {exc}") from exc
    unknown = sorted(set(doc) - {TABLE})
    if unknown:
        raise ValueError(
            f"unknown top-level key(s) {', '.join(map(repr, unknown))}; "
            f"entries go under [[{TABLE}]]"
        )
    entries = doc.get(TABLE, [])
    if not isinstance(entries, list):
        raise ValueError(f"'{TABLE}' must be an array of tables ([[{TABLE}]])")
    seen: set[str] = set()
    return tuple(_parse_entry(i, raw, seen) for i, raw in enumerate(entries, start=1))


def load_learnings(
    path: str | None,
    *,
    max_chars: int = LEARNINGS_FILE_MAX_CHARS,
    source: str,
    today: date | None = None,
) -> Learnings | None:
    """Load the learnings file at ``path``; ``None``, ``""`` or blank means off.

    ``source`` names the input that supplied the path
    (``PRXREF_LEARNINGS_FILE``, or ``<config file>: learnings_file``) and
    starts every :class:`~prxref.llm.ConfigError`: a missing, unreadable,
    non-regular or oversized file, invalid UTF-8, invalid TOML, or an entry
    that breaks the schema in the module docstring. Like the team rules
    file the path is confined to the working directory. ``today`` (default:
    the current UTC date) decides which entries have expired; an entry
    added more than :data:`STALE_AFTER_DAYS` days before it is logged as a
    WARNING once, naming its id.
    """
    if path is None or not path.strip():
        return None
    try:
        resolved = check_readable_path(path, confine=True)
        with open(resolved, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise OSError(errno.EINVAL, "not a regular file", path)
            raw = fh.read()
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "strerror", None) or str(exc)
        raise ConfigError(f"{source}: cannot read learnings file {path!r}: {reason}") from exc
    sha256 = hashlib.sha256(raw).hexdigest()
    try:
        text = decode_text(raw)
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{source}: learnings file {path!r} is not UTF-8 text ({exc.reason})") from exc
    if len(text) > max_chars:
        raise ConfigError(
            f"{source}: learnings file {path!r} has {len(text)} characters, more than the {max_chars} allowed"
        )
    try:
        entries = parse_learnings(text)
    except ValueError as exc:
        raise ConfigError(f"{source}: learnings file {path!r}: {exc}") from exc
    when = today or _today()
    stale = tuple(
        e.id for e in entries
        if e.added is not None and (when - e.added).days > STALE_AFTER_DAYS
        and not e.expired(when)
    )
    if stale:
        logger.warning(
            "%s: %d learning(s) were added more than %d days ago and have no past expiry; "
            "revisit them: %s",
            source, len(stale), STALE_AFTER_DAYS, ", ".join(stale),
        )
    loaded = Learnings(path=path, sha256=sha256, entries=entries, today=when, stale=stale)
    expired = loaded.expired_count
    if expired:
        logger.info("%s: %d learning(s) expired and suppress nothing", source, expired)
    return loaded


# --- harvest -----------------------------------------------------------------

# The first line of every inline comment the pipeline posts
# (prxref.markers.inline_header): "🤖 <marker> **[<SEVERITY>] <title>** (`<loc>`)".
# The marker is one glyph, or two (the out-of-ticket glyph, a space, then the
# severity glyph) on a finding outside the ticket.
# A forge's thread snippet is truncated (120-200 characters), so the closing
# "**" and the location may be cut off; the title then runs to the end.
_HEADER_RE = re.compile(
    r"^🤖 [^*\n]+ \*\*\[(?:ERROR|WARNING|SPEC|OUTOFSCOPE)(?: · [^\]\n]+)?\] "
    r"(?P<title>[^\n]*?)(?:\*\*(?: \(`(?P<loc>[^`\n]*)`\))?\s*$|$)",
)


def parse_inline_header(snippet: str) -> tuple[str, str | None] | None:
    """``(title, location path)`` of a prxref inline comment's first line, else ``None``.

    The location path has its ``:line`` suffix removed and is ``None`` when
    the snippet was truncated before it. A title cut short by truncation is
    returned as far as it goes.
    """
    first = (snippet or "").split("\n", 1)[0]
    match = _HEADER_RE.match(first)
    if match is None:
        return None
    title = match.group("title").strip()
    if not title:
        return None
    loc = match.group("loc")
    loc_path = re.sub(r":\d+$", "", loc) if loc else None
    return title, loc_path or None


def _candidate_id(path: str, title: str) -> str:
    digest = hashlib.sha256(f"{path}\n{title}".encode()).hexdigest()[:10]
    return f"harvested-{digest}"


def harvest_candidates(
    threads: Iterable[Thread], pr_url: str, *, today: date | None = None,
) -> list[dict[str, Any]]:
    """Candidate learning entries for each prxref-rooted thread closed as won't-fix.

    A thread counts when its snippet opens with prxref's inline-comment
    header (:func:`parse_inline_header`) — so the thread's root is a prxref
    finding — and the forge marked it :attr:`Thread.wont_fix`, which the
    adapters derive from a human's explicit "won't fix" (never from a body
    carrying :data:`~prxref.forges.base.ATTRIBUTION_MARKER`) or from Azure
    DevOps' ``wontFix`` / ``byDesign`` status. A reply is never a candidate
    of its own, since its snippet has no header. Each candidate scopes the
    finding's own path exactly, claims its title, and names no rule (a
    posted comment does not carry it); entries are de-duplicated on
    ``(path, title)`` and returned in thread order.
    """
    when = today or _today()
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for t in threads:
        if not t.wont_fix:
            continue
        parsed = parse_inline_header(t.body_snippet or "")
        if parsed is None:
            continue
        title, loc_path = parsed
        path = t.path or loc_path
        if not path:
            continue
        key = (path, title)
        if key in seen:
            continue
        seen.add(key)
        if not _tokens(title):
            continue
        out.append({
            "id": _candidate_id(path, title),
            "paths": [path],
            "claim": title,
            "reason": "Closed as won't fix in review; replace with the team's reasoning.",
            "source": t.url or pr_url,
            "added": when,
        })
    return out


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml_value(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, Sequence):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot render {value!r} as TOML")


def render_toml(candidates: Sequence[dict[str, Any]], pr_url: str) -> str:
    """Render harvest candidates as a learnings-file fragment :func:`parse_learnings` accepts."""
    lines = [
        f"# Candidate prxref learnings harvested from {' '.join(pr_url.split())}",
        "# Review each entry, edit its reason (and widen paths or add a rule if the",
        "# team agrees), then commit the ones you keep to the learnings file.",
    ]
    if not candidates:
        lines.append("# No prxref finding on this PR was closed as won't fix.")
    for cand in candidates:
        lines.append("")
        lines.append(f"[[{TABLE}]]")
        for key in ("id", "paths", "claim", "rule", "reason", "source", "added", "expires"):
            if key in cand and cand[key] is not None:
                lines.append(f"{key} = {_toml_value(cand[key])}")
    return "\n".join(lines) + "\n"
