"""Spec-source fetching and constraint digest for spec-grounded reviews.

A review can be grounded in written specs: files, directories, web pages, and
Jira tickets supplied by the operator. Workers each see one slice of the diff,
so a constraint that lives only in a spec document has no seat that knows it —
this module fetches those sources and reduces them to a compact, deterministic
digest of the constraints this diff could plausibly violate.

Two halves, both deterministic and model-free, mirroring
:mod:`prxref.systemic`:

- The fetch layer (:func:`fetch_specs`) turns each configured source into one
  :class:`SpecSource` of plain text, or a human-readable error. It never
  raises: a failed source is data, not an exception, and a run whose sources
  all failed behaves exactly like a run with no specs. HTTP goes through a
  read-only retried session (GET/HEAD/OPTIONS only, the same policy the forge
  adapters use), streams at most ``max_chars`` per source with an explicit
  truncation marker, accepts only text-like content types, strips HTML to
  text, and reads local files as UTF-8. Jira tickets are fetched over REST
  with basic auth when credentials are configured, anonymously otherwise;
  a 401/403 without credentials names the ``PRXREF_JIRA_*`` variables — the
  fix an operator can act on — never their values.

- The digest (:func:`build_spec_digest`) extracts constraints per source in
  document order — headings, RFC-2119 normative statements, version pins, and
  naming/shape rules — then ranks them: ticket scope first, then constraints
  whose tokens overlap the diff's token set, then unmatched MUST-level rules,
  with weaker unmatched strengths dropped first as the budget runs out.
  Same input, same text, so evals and traces stay stable.
"""
from __future__ import annotations

import codecs
import json
import os
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from .quality import _evidence_tokens, _tokens
from .retry_logging import LoggingRetry
from .triage import FileDiff

SPEC_FETCH_TIMEOUT_S = 15

SPEC_DIR_MAX_FILES = 20

CHARS_PER_TOKEN = 4

TRUNCATION_MARKER = "[spec digest truncated: budget reached]"

SOURCE_TRUNCATION_MARKER = "[source truncated at {n} chars]"

TICKET_DESC_BUDGET_CHARS = 6000

_STATEMENT_MAX_CHARS = 400

_KEY = r"[A-Z][A-Z0-9_]*-\d+"

_TICKET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"^(?P<base>https?://[^/]+)/browse/(?P<key>{_KEY})(?:[/?#]|$)"),
    re.compile(rf"^(?P<base>https?://[^/]+)/rest/api/(?:2|3)/issue/(?P<key>{_KEY})(?:[/?#]|$)"),
    re.compile(
        rf"^(?P<base>https?://[^/]+)/jira/software/c/projects/[A-Z][A-Z0-9_]*/issues/(?P<key>{_KEY})(?:[/?#]|$)"
    ),
)

_MD_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")

_SETEXT_UNDER_RE = re.compile(r"^[-=]{3,}\s*$")

_SETEXT_TEXT_RE = re.compile(r"^[A-Z][^\n]{0,80}$")

_STRENGTH3_RE = re.compile(r"\bMUST\b|\bSHALL\b|\bREQUIRED\b|\bFORBIDDEN\b")

_STRENGTH2_RE = re.compile(r"\bSHOULD\b|\bRECOMMENDED\b")

_STRENGTH2_PROSE_RE = re.compile(r"recommended to|forbidden to", re.IGNORECASE)

_STRENGTH1_RE = re.compile(r"\bMAY\b")

_STRENGTH1_PROSE_RE = re.compile(r"\bcan\b|discouraged", re.IGNORECASE)

_NAMING_RE = re.compile(
    r"(?:MUST|SHOULD|SHALL)[^.]{0,120}"
    r"(?:named|name|prefix|suffix|header|field|snake_case|camelCase|lowercase|uppercase)"
)

_VERSION_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

_VERSION_NUM_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b")

_VERSION_CONTEXT_RE = re.compile(r"version|protocol|revision|draft", re.IGNORECASE)

_STANDALONE_PIN_RE = re.compile(
    rf"^(?:{_VERSION_DATE_RE.pattern}|{_VERSION_NUM_RE.pattern})[.:]?\s*$"
)

_DIR_SUFFIXES = (".md", ".markdown", ".txt", ".adoc")

_TEXT_TYPE_MARKS = ("json", "xml", "html", "markdown", "javascript", "yaml")


@dataclass
class SpecSource:
    """One fetched spec source: what was asked for, what came back."""

    origin: str
    kind: str
    text: str
    error: str


@dataclass
class TicketRef:
    """A Jira ticket recognized inside a URL."""

    base_url: str
    key: str
    url: str


def parse_ticket_url(url: str) -> TicketRef | None:
    """Recognize a Jira ticket URL, returning its REST base and key.

    Three shapes are recognized: ``{host}/browse/{KEY}-{n}`` (Jira Cloud and
    Server classic), ``{host}/rest/api/{2|3}/issue/{KEY}-{n}`` (raw REST
    links), and ``{host}/jira/software/c/projects/{KEY}/issues/{KEY}-{n}``
    (Cloud new UI). Project keys are uppercase letters, digits, and
    underscores; the numeric suffix is required. Anything else returns
    ``None``.
    """
    text = (url or "").strip()
    for pattern in _TICKET_PATTERNS:
        m = pattern.match(text)
        if m:
            return TicketRef(base_url=m.group("base"), key=m.group("key"), url=text)
    return None


def _create_default_session() -> requests.Session:
    """Build the read-only HTTP session spec fetching uses.

    The retry policy is the forge adapters' verbatim: read verbs only, since
    urllib3 retries beneath the requests adapter and a re-sent write is sent
    whole. Spec fetching only ever GETs.
    """
    session = requests.Session()
    retry = LoggingRetry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=True,
        allowed_methods=frozenset(["GET", "HEAD", "OPTIONS"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _is_text_like(content_type: str) -> bool:
    return bool(content_type) and (
        content_type.startswith("text/") or any(mark in content_type for mark in _TEXT_TYPE_MARKS)
    )


def _read_stream(resp: requests.Response, max_chars: int) -> str:
    """Decode the streamed body up to ``max_chars``, announcing truncation."""
    decoder = codecs.getincrementaldecoder(resp.encoding or "utf-8")(errors="replace")
    parts: list[str] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=8192):
        if not chunk:
            continue
        decoded = decoder.decode(chunk, final=False)
        if decoded:
            parts.append(decoded)
            total += len(decoded)
        if total >= max_chars:
            break
    parts.append(decoder.decode(b"", final=True))
    text = "".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + SOURCE_TRUNCATION_MARKER.format(n=max_chars)
    return text


class _HTMLTextExtractor(HTMLParser):
    """Strip tags to text: block tags become line breaks, script/style drop.

    An ``<hN>`` element becomes one markdown heading line, ``"#" * N + " " +
    text``, so HTML sources scope their constraints the way markdown sources
    do. Its text is every data run inside it with the block tags nested there
    ignored (doc sites wrap a permalink ``<div><a>`` inside each heading),
    zero-width spaces and pilcrow permalinks removed, whitespace collapsed. A
    heading never closed by any ``</hN>`` falls back to plain text, so a
    malformed page loses no content.
    """

    _BLOCK = frozenset(
        {
            "address", "article", "aside", "blockquote", "br", "caption", "dd", "div", "dl", "dt",
            "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5",
            "h6", "head", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
            "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
        }
    )
    _DROP = frozenset({"script", "style", "template"})
    _HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
    _HEADING_NOISE = str.maketrans("", "", "​¶")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0
        self._heading: str | None = None
        self._heading_text: list[str] = []
        self._heading_raw: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._DROP:
            self._skip += 1
        elif self._heading is not None:
            if tag in self._BLOCK:
                self._heading_raw.append("\n")
        elif tag in self._HEADINGS and not self._skip:
            self._heading = tag
            self._heading_text = []
            self._heading_raw = ["\n"]
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._DROP:
            self._skip = max(0, self._skip - 1)
        elif self._heading is not None:
            if tag in self._HEADINGS:
                self._close_heading()
            elif tag in self._BLOCK:
                self._heading_raw.append("\n")
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._heading is not None:
            self._heading_text.append(data)
            self._heading_raw.append(data)
        else:
            self._parts.append(data)

    def _close_heading(self) -> None:
        level = int((self._heading or "h1")[1])
        text = " ".join("".join(self._heading_text).translate(self._HEADING_NOISE).split())
        self._parts.append(f"\n{'#' * level} {text}\n" if text else "\n")
        self._heading = None
        self._heading_text = []
        self._heading_raw = []

    def text(self) -> str:
        if self._heading is not None:
            self._parts.extend(self._heading_raw)
            self._heading = None
            self._heading_text = []
            self._heading_raw = []
        collapsed: list[str] = []
        blank = True
        for line in "".join(self._parts).splitlines():
            stripped = line.strip()
            if not stripped:
                if not blank:
                    collapsed.append("")
                blank = True
                continue
            blank = False
            collapsed.append(stripped)
        while collapsed and not collapsed[-1]:
            collapsed.pop()
        return "\n".join(collapsed)


def _strip_html(text: str) -> str:
    extractor = _HTMLTextExtractor()
    extractor.feed(text)
    extractor.close()
    return extractor.text()


def _read_capped(path: str, max_chars: int) -> str:
    with open(path, encoding="utf-8") as fh:
        data = fh.read()
    if len(data) > max_chars:
        return data[:max_chars] + SOURCE_TRUNCATION_MARKER.format(n=max_chars)
    return data


def _fetch_url(src: SpecSource, url: str, max_chars: int, session: requests.Session) -> None:
    resp = session.get(url, timeout=SPEC_FETCH_TIMEOUT_S, stream=True)
    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if resp.status_code != 200:
        src.error = f"HTTP {resp.status_code} fetching {url}"
        return
    if not _is_text_like(content_type):
        src.error = f"not a text content type: {content_type or 'unknown'}"
        return
    text = _read_stream(resp, max_chars)
    if "html" in content_type:
        text = _strip_html(text)
    src.text = text


def _fetch_file(src: SpecSource, path: str, max_chars: int) -> None:
    src.text = _read_capped(path, max_chars)


def _fetch_dir(src: SpecSource, path: str, max_chars: int) -> None:
    names = sorted(
        name
        for name in os.listdir(path)
        if name.endswith(_DIR_SUFFIXES) and os.path.isfile(os.path.join(path, name))
    )[:SPEC_DIR_MAX_FILES]
    if not names:
        src.error = f"no {'/'.join(_DIR_SUFFIXES)} files in {path}"
        return
    parts = [f"## {name}\n\n{_read_capped(os.path.join(path, name), max_chars)}" for name in names]
    src.text = "\n\n".join(parts)


def _fetch_jira(
    src: SpecSource,
    ref: TicketRef,
    jira_base_url: str,
    jira_email: str,
    jira_api_token: str,
    session: requests.Session,
) -> None:
    base = (jira_base_url or ref.base_url).rstrip("/")
    url = f"{base}/rest/api/2/issue/{ref.key}?fields=summary,description,issuetype,labels"
    auth = (jira_email, jira_api_token) if jira_email and jira_api_token else None
    resp = session.get(url, timeout=SPEC_FETCH_TIMEOUT_S, auth=auth)
    if resp.status_code in (401, 403) and auth is None:
        src.error = (
            f"Jira returned {resp.status_code} for {ref.key} without credentials; set "
            "PRXREF_JIRA_BASE_URL, PRXREF_JIRA_EMAIL and PRXREF_JIRA_API_TOKEN to authenticate."
        )
        return
    if resp.status_code != 200:
        src.error = f"Jira returned {resp.status_code} for {ref.key}"
        return
    payload = resp.json()
    fields = payload.get("fields") or {} if isinstance(payload, dict) else {}
    summary = fields.get("summary") or ""
    issuetype = fields.get("issuetype")
    issue_type = issuetype.get("name", "") if isinstance(issuetype, dict) else ""
    labels = ", ".join(str(label) for label in fields.get("labels") or [])
    description = fields.get("description")
    if description is None:
        description = ""
    elif not isinstance(description, str):
        description = json.dumps(description)
    src.text = "\n".join([f"Summary: {summary}", f"Type: {issue_type}", f"Labels: {labels}", "", description]).strip()


def _dispatch(
    src: SpecSource,
    raw: str,
    max_chars: int,
    jira_base_url: str,
    jira_email: str,
    jira_api_token: str,
    session: requests.Session,
) -> None:
    text = raw.strip()
    if text.startswith(("http://", "https://")):
        ref = parse_ticket_url(text)
        if ref is not None:
            src.kind = "jira"
            _fetch_jira(src, ref, jira_base_url, jira_email, jira_api_token, session)
        else:
            src.kind = "url"
            _fetch_url(src, text, max_chars, session)
        return
    if os.path.isdir(text):
        src.kind = "dir"
        _fetch_dir(src, text, max_chars)
        return
    if os.path.isfile(text):
        src.kind = "file"
        _fetch_file(src, text, max_chars)
        return
    src.error = f"not a URL or path: {text}"


def fetch_specs(
    sources: list[str],
    *,
    max_chars: int,
    jira_base_url: str = "",
    jira_email: str = "",
    jira_api_token: str = "",
    session: requests.Session | None = None,
) -> list[SpecSource]:
    """Fetch every spec source, in the order given, never raising.

    Each source string is dispatched as: a URL matching a Jira ticket shape
    (:func:`parse_ticket_url`) goes to Jira REST; any other ``http(s)`` URL is
    fetched as a web page; an existing filesystem path is read as a file or a
    directory of spec files; anything else fails that source with a
    ``not a URL or path`` error. Every exception — network, decode, bad
    credentials — becomes that source's ``error`` string with empty ``text``;
    a source never aborts the run. ``jira_base_url`` overrides the ticket
    URL's own host when non-empty (self-hosted boards behind a different REST
    host); Jira authenticates with basic auth only when both ``jira_email``
    and ``jira_api_token`` are non-empty, anonymously otherwise.
    """
    active = session if session is not None else _create_default_session()
    out: list[SpecSource] = []
    for raw in sources:
        src = SpecSource(origin=raw, kind="", text="", error="")
        try:
            _dispatch(src, raw, max_chars, jira_base_url, jira_email, jira_api_token, active)
        except Exception as exc:  # noqa: BLE001 - a failed source is data, not an abort
            src.text = ""
            src.error = f"{type(exc).__name__}: {exc}"
        out.append(src)
    return out


@dataclass
class _Unit:
    """One kept digest line with its rank inputs."""

    origin: str
    source_idx: int
    doc_idx: int
    strength: int
    is_ticket: bool
    render: str
    heading_render: str | None = None
    heading_key: str | None = None
    tokens: frozenset[str] = frozenset()
    score: int = 0


def _origin_short(origin: str) -> str:
    """The short name a source goes by in the digest, which the LLM sees.

    A URL keeps only its last path segment, falling back to the bare host, so
    no query, fragment, userinfo, or port reaches the prompt; a path keeps its
    last component. A credential that IS the last path segment survives.
    """
    parsed = urlparse(origin)
    if parsed.scheme:
        tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        return tail or parsed.hostname or parsed.scheme
    return origin.rstrip("/").rsplit("/", 1)[-1] or origin


def _heading_slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40].strip("-")


def _one_line(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit]


def _strength(line: str) -> int | None:
    if _STRENGTH3_RE.search(line):
        return 3
    if _STRENGTH2_RE.search(line) or _STRENGTH2_PROSE_RE.search(line):
        return 2
    if _STRENGTH1_RE.search(line) or _STRENGTH1_PROSE_RE.search(line):
        return 1
    return None


def _is_version_pin_line(line: str) -> bool:
    if _VERSION_DATE_RE.search(line):
        return True
    return bool(_VERSION_NUM_RE.search(line) and _VERSION_CONTEXT_RE.search(line))


def _spec_units(src: SpecSource, source_idx: int) -> list[_Unit]:
    short = _origin_short(src.origin)
    lines = src.text.splitlines()
    units: list[_Unit] = []
    heading: tuple[str, str] | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        md = _MD_HEADING_RE.match(line)
        setext = (
            md is None
            and _SETEXT_TEXT_RE.match(line) is not None
            and i + 1 < len(lines)
            and _SETEXT_UNDER_RE.match(lines[i + 1]) is not None
            and _strength(line) is None
        )
        if md or setext:
            text = (md.group(1) if md else line).strip()
            slug = _heading_slug(text)
            heading = (
                f"[spec:{short}#{slug}] (heading) {_one_line(text, _STATEMENT_MAX_CHARS)}",
                slug,
            )
            i += 2 if setext else 1
            continue
        i += 1
        stripped = line.strip()
        if not stripped:
            continue
        strength = 3 if _NAMING_RE.search(stripped) else _strength(stripped)
        if strength is None and _is_version_pin_line(stripped):
            strength = 3
        if strength is None:
            continue
        anchor = f"L{i}"
        statement = _one_line(stripped, _STATEMENT_MAX_CHARS)
        label = {3: "MUST", 2: "SHOULD", 1: "MAY"}[strength]
        units.append(
            _Unit(
                origin=src.origin,
                source_idx=source_idx,
                doc_idx=i,
                strength=strength,
                is_ticket=False,
                render=f"[spec:{short}#{anchor}] ({label}) {statement}",
                heading_render=heading[0] if heading else None,
                heading_key=heading[1] if heading else None,
                tokens=frozenset(_evidence_tokens(statement)),
            )
        )
    return units


def _ticket_units(src: SpecSource, source_idx: int) -> list[_Unit]:
    ref = parse_ticket_url(src.origin)
    tag = f"ticket:{ref.key}" if ref else f"ticket:{_origin_short(src.origin)}"
    units: list[_Unit] = []
    used = 0
    for doc_idx, line in enumerate(line for line in src.text.splitlines() if line.strip()):
        if used >= TICKET_DESC_BUDGET_CHARS:
            break
        statement = " ".join(line.split())
        used += len(statement) + 1
        units.append(
            _Unit(
                origin=src.origin,
                source_idx=source_idx,
                doc_idx=doc_idx,
                strength=3,
                is_ticket=True,
                render=f"[{tag}] {statement}",
                tokens=frozenset(_evidence_tokens(statement)),
            )
        )
    return units


def _diff_tokens(files: list[FileDiff]) -> frozenset[str]:
    toks: set[str] = set()
    for f in files:
        toks |= _tokens(f.path, split_compounds=True)
        for h in f.hunks:
            for ln in h.lines:
                if ln.kind != " ":
                    toks |= _tokens(ln.text, split_compounds=True)
    return frozenset(toks)


def _rank_units(sources: list[SpecSource], diff_toks: frozenset[str]) -> list[_Unit]:
    units: list[_Unit] = []
    for idx, src in enumerate(sources):
        if src.error or not src.text:
            continue
        if src.kind == "jira":
            units.extend(_ticket_units(src, idx))
        else:
            units.extend(_spec_units(src, idx))
    ticket = [u for u in units if u.is_ticket]
    rest: list[_Unit] = []
    for u in units:
        if u.is_ticket:
            continue
        u.score = len(u.tokens & diff_toks)
        rest.append(u)
    relevant = sorted(
        (u for u in rest if u.score >= 1),
        key=lambda u: (-u.score, u.source_idx, u.doc_idx),
    )
    unmatched = [u for u in rest if u.score == 0]
    return (
        ticket
        + relevant
        + [u for u in unmatched if u.strength >= 3]
        + [u for u in unmatched if u.strength == 2]
        + [u for u in unmatched if u.strength == 1]
    )


def build_spec_digest(sources: list[SpecSource], files: list[FileDiff], token_budget: int) -> str:
    """Reduce the fetched sources to a bounded digest of this diff's constraints.

    Per source, in document order, headings, RFC-2119 normative statements,
    version pins, and naming/shape rules are extracted; each kept unit
    renders as one ``[spec:{short}#anchor] (STRENGTH) statement`` line, where
    ``{short}`` is the source's last path segment (or bare host), never its
    full origin. Units are ranked: ticket constraints first (scope beats
    relevance), then units sharing at least one content token with the
    diff's token set (file paths plus changed lines, compound-split, the same
    evidence vocabulary :mod:`prxref.quality` uses — higher overlap first),
    then unmatched MUST-level rules in source order, then unmatched SHOULD-
    and MAY-level rules, which the budget exhausts first. Ranking interleaves
    sections, so a constraint's heading line is re-emitted whenever the open
    section changes, and a unit with no heading after one that had one is
    preceded by ``[spec:{short}] (heading) (no section)``: every constraint
    sits under its own section. The walk stops at ``token_budget * 4`` chars
    with :data:`TRUNCATION_MARKER`. A source that fetched fine but
    contributed nothing gets an explicit
    ``[spec:{short}: nothing diff-relevant kept]`` line, so silence is
    explained; a failed source gets no line (the grounding note reports it).

    Returns ``""`` when sources were given but no unit was extracted from any
    of them — every source failed, or none held a constraint — so the prompt
    shows its no-specs text and :func:`constraint_count` is 0. The test runs
    before the budget cut: a budget too small for any unit, a ``token_budget``
    below 1 included, yields the intro plus :data:`TRUNCATION_MARKER` rather
    than raising. No sources at all yields the intro alone.
    """
    units = _rank_units(sources, _diff_tokens(files))
    if sources and not units:
        return ""
    budget = max(1, token_budget) * CHARS_PER_TOKEN
    intro = (
        "Spec constraints ranked for this diff: ticket scope first, then "
        "constraints sharing tokens with the diff, then unmatched MUST-level rules."
    )
    out = [intro]
    used = len(intro)
    contributed: set[str] = set()
    open_scope: tuple[int, str | None] | None = None
    truncated = False
    for unit in units:
        parts: list[str] = []
        if not unit.is_ticket:
            key = (unit.source_idx, unit.heading_key) if unit.heading_render is not None else None
            if key != open_scope:
                parts.append(
                    unit.heading_render
                    or f"[spec:{_origin_short(unit.origin)}] (heading) (no section)"
                )
                open_scope = key
        parts.append(unit.render)
        block = "\n".join(parts)
        if used + len(block) + 1 > budget:
            out.append(TRUNCATION_MARKER)
            truncated = True
            break
        out.append(block)
        used += len(block) + 1
        contributed.add(unit.origin)
    if not truncated:
        for src in sources:
            if src.error or src.origin in contributed:
                continue
            line = f"[spec:{_origin_short(src.origin)}: nothing diff-relevant kept]"
            if used + len(line) + 1 > budget:
                out.append(TRUNCATION_MARKER)
                break
            out.append(line)
            used += len(line) + 1
    return "\n".join(out)


_CONSTRAINT_LINE_RE = re.compile(
    r"^\[(?:spec:[^\]]*#\S+\] \((?:MUST|SHOULD|MAY)\) |ticket:[^\]]+\] )", re.M
)


def constraint_count(digest: str) -> int:
    """The number of constraint lines a :func:`build_spec_digest` digest injects.

    A constraint line is a spec unit, ``[spec:{short}#anchor] (MUST|SHOULD|MAY)
    statement``, or a ticket line, ``[ticket:KEY] statement``; the strength
    label binds to the spec form only, because ticket lines carry none. The
    intro, heading lines (``(heading)``, including ``(heading) (no
    section)``), the truncation markers, and the bracket-closed bookkeeping
    lines such as ``[spec:{short}: nothing diff-relevant kept]`` are scoping
    or bookkeeping and never count. An empty digest counts 0. The grounding
    note and every decision about whether a run is spec-grounded use this
    count, so it and the render f-strings live in one module.
    """
    return len(_CONSTRAINT_LINE_RE.findall(digest))
