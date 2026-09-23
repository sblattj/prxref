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
  text, and reads local files as UTF-8. Jira tickets (a ``/browse/`` or REST
  URL under a context path of up to two segments, a Cloud issue view, or a
  Cloud board's ``selectedIssue``) are fetched over REST. Credentials are
  only ever sent to ``PRXREF_JIRA_BASE_URL``: basic auth goes out only when
  it, ``PRXREF_JIRA_EMAIL`` and ``PRXREF_JIRA_API_TOKEN`` are all set, and
  every other fetch is anonymous. Credentials set without the base URL are
  withheld with a warning, and a plain-http base URL is used with a warning.
  An anonymous 401, 403 or 404 — Jira Cloud hides a private issue behind a
  404 — names the variables that would fix it, never their values, and a
  200 that is not a JSON issue fails its source cleanly.

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
import logging
import os
import re
import time
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

import requests
from requests.adapters import HTTPAdapter

from .quality import _evidence_tokens, _tokens
from .retry_logging import LoggingRetry
from .triage import FileDiff

logger = logging.getLogger(__name__)

SPEC_FETCH_TIMEOUT_S = 15

SPEC_FETCH_BUDGET_S = 30

SPEC_DIR_MAX_FILES = 20

CHARS_PER_TOKEN = 4

TRUNCATION_MARKER = "[spec digest truncated: budget reached]"

SOURCE_TRUNCATION_MARKER = "[source truncated at {n} chars]"

TICKET_DESC_BUDGET_CHARS = 6000

_STATEMENT_MAX_CHARS = 400

_KEY = r"[A-Z][A-Z0-9_]*-\d+"

_KEY_RE = re.compile(_KEY)

_CTX = r"(?:/[^/?#]+){0,2}"

_TICKET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"^(?P<base>https?://[^/?#]+{_CTX})/browse/(?P<key>{_KEY})(?:[/?#]|$)"),
    re.compile(rf"^(?P<base>https?://[^/?#]+{_CTX})/rest/api/(?:2|3)/issue/(?P<key>{_KEY})(?:[/?#]|$)"),
    re.compile(
        rf"^(?P<base>https?://[^/?#]+)/jira/software/(?:c/)?projects/[^/?#]+/issues/(?P<key>{_KEY})(?:[/?#]|$)"
    ),
)

_JIRA_ENV_HINT = "PRXREF_JIRA_BASE_URL, PRXREF_JIRA_EMAIL and PRXREF_JIRA_API_TOKEN"

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

    Four shapes are recognized:

    - ``{base}/browse/{KEY}-{n}`` (Jira Cloud and Server classic) and
      ``{base}/rest/api/{2|3}/issue/{KEY}-{n}`` (raw REST links), where
      ``{base}`` is ``scheme://host`` plus a context path of zero to two
      segments (``https://issues.apache.org/jira``,
      ``https://acme.com/tools/jira``); the base keeps that context path,
      because Server serves its REST API under it.
    - ``{host}/jira/software/projects/{P}/issues/{KEY}-{n}`` and its ``/c/``
      form (Cloud team- and company-managed issue views).
    - A Cloud board or backlog URL on a ``/jira/`` path carrying the ticket
      in its ``selectedIssue`` query parameter, read wherever it sits in the
      query string; the base is ``scheme://host``.

    Project keys are uppercase letters, digits, and underscores; the numeric
    suffix is required. The context-path bound is what keeps a Bitbucket
    Server file URL (``/projects/P/repos/R/browse/…``, four segments deep)
    from matching. A non-Jira URL with at most two path segments before
    ``/browse/{KEY}-{n}`` does match, and is fetched from Jira REST on that
    host anonymously, or looked up by key on ``PRXREF_JIRA_BASE_URL`` when
    that is set. Anything else returns ``None``.
    """
    text = (url or "").strip()
    for pattern in _TICKET_PATTERNS:
        m = pattern.match(text)
        if m:
            return TicketRef(base_url=m.group("base"), key=m.group("key"), url=text)
    return _board_ticket(text)


def _board_ticket(text: str) -> TicketRef | None:
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if not parsed.path.startswith("/jira/"):
        return None
    selected = parse_qs(parsed.query).get("selectedIssue")
    if not selected or not _KEY_RE.fullmatch(selected[0]):
        return None
    return TicketRef(base_url=f"{parsed.scheme}://{parsed.netloc}", key=selected[0], url=text)


def _create_default_session() -> requests.Session:
    """Build the read-only HTTP session spec fetching uses.

    Read verbs only, as in the forge adapters: urllib3 retries beneath the
    requests adapter and a re-sent write is sent whole. Spec fetching only
    ever GETs. Unlike the forges it retries once, with no backoff sleep, and
    ignores ``Retry-After``: the webhook daemon reviews one PR at a time, so
    a spec host that is down or asks for time is skipped, not waited for.

    Until the response headers arrive a source is bounded by those two
    attempts, each allowed :data:`SPEC_FETCH_TIMEOUT_S` to connect and per
    read, so a host that accepts and never answers costs 30 s. Once the
    headers are in, :func:`_read_stream` holds the source to
    :data:`SPEC_FETCH_BUDGET_S`, counted from before the request, plus at
    most one read timeout: 45 s for a host that answers and then trickles.
    Neither bound depends on ``--timeout``. A host trickling its header
    lines, or the body of a redirect, is bounded per read only.
    """
    session = requests.Session()
    retry = LoggingRetry(
        total=1,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=False,
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


class _FetchTimeout(Exception):
    """A spec body was still arriving when its :data:`SPEC_FETCH_BUDGET_S` ran out."""


def _read_stream(resp: requests.Response, byte_cap: int, deadline: float) -> tuple[bytes, bool]:
    """Read a streamed body up to ``byte_cap`` bytes, never past ``deadline``.

    Returns the bytes and whether the body went on past the cap. Every read
    is a single socket read — ``raw.read1`` with the content encoding undone
    — so the monotonic clock is checked between reads however the host paces
    the body: close-delimited, chunked or compressed. ``iter_content`` would
    block until a whole chunk arrived, and a byte every few seconds never
    trips the per-read timeout. A body still arriving at ``deadline`` raises
    :class:`_FetchTimeout`, at most one read timeout late. A response whose
    ``raw`` has no ``read1`` (urllib3 1.x) is read a byte at a time through
    ``iter_content`` under the same check.
    """
    read1 = getattr(getattr(resp, "raw", None), "read1", None)
    chunks = None if read1 is not None else resp.iter_content(chunk_size=1)
    buf = bytearray()
    while len(buf) <= byte_cap:
        if time.monotonic() >= deadline:
            raise _FetchTimeout
        chunk = read1(8192, decode_content=True) if read1 is not None else next(chunks, b"")
        if not chunk:
            return bytes(buf), False
        buf += chunk
    return bytes(buf[:byte_cap]), True


_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE)

_META_PRESCAN_BYTES = 1024


def _codec(name: str | None) -> str | None:
    """Return the codec a declared charset decodes with, or ``None`` if Python has none.

    UTF-8 is read as ``utf-8-sig``, so a byte-order mark never reaches the text.
    """
    if not name:
        return None
    try:
        canonical = codecs.lookup(name).name
    except LookupError:
        return None
    return "utf-8-sig" if canonical == "utf-8" else canonical


def _decode_body(data: bytes, content_type: str, *, cut: bool) -> str:
    """Decode a fetched body, never trusting requests' ISO-8859-1 guess for ``text/*``.

    The charset is the ``Content-Type`` header's, then for HTML a ``<meta>``
    charset in the first 1024 bytes, then strict UTF-8 (a byte-order mark
    dropped), and only when that fails cp1252 with undecodable bytes
    replaced. A declared charset Python cannot decode text with falls
    through to the next step instead of failing the source. ``cut`` says the
    bytes stop at the byte cap, so a multibyte sequence split there is not
    taken for invalid UTF-8.
    """
    header = Message()
    header["Content-Type"] = content_type
    declared = [header.get_content_charset()]
    if "html" in content_type.split(";")[0].lower():
        meta = _META_CHARSET_RE.search(data[:_META_PRESCAN_BYTES])
        declared.append(meta.group(1).decode("ascii") if meta else None)
    for name in declared:
        codec = _codec(name)
        if codec is None:
            continue
        try:
            return data.decode(codec, errors="replace")
        except LookupError:
            continue
    try:
        return codecs.getincrementaldecoder("utf-8-sig")().decode(data, final=not cut)
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


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
    """Fetch a web page into ``src`` within :data:`SPEC_FETCH_BUDGET_S`.

    The body is read up to ``4 * max_chars + 4`` bytes, decoded by
    :func:`_decode_body`, and cut at ``max_chars``. HTML is stripped after
    the cut and the truncation marker appended after the stripping, so a
    cut inside a ``<script>`` or an open tag cannot swallow the marker. A
    body still arriving when the budget runs out fails the source.
    """
    deadline = time.monotonic() + SPEC_FETCH_BUDGET_S
    resp = session.get(url, timeout=SPEC_FETCH_TIMEOUT_S, stream=True)
    try:
        header = resp.headers.get("Content-Type") or ""
        content_type = header.split(";")[0].strip().lower()
        if resp.status_code != 200:
            src.error = f"HTTP {resp.status_code} fetching {url}"
            return
        if not _is_text_like(content_type):
            src.error = f"not a text content type: {content_type or 'unknown'}"
            return
        data, cut = _read_stream(resp, 4 * max_chars + 4, deadline)
    except _FetchTimeout:
        src.error = f"timed out after {SPEC_FETCH_BUDGET_S:g} s"
        return
    finally:
        resp.close()
    text = _decode_body(data, header, cut=cut)
    truncated = cut or len(text) > max_chars
    text = text[:max_chars]
    if "html" in content_type:
        text = _strip_html(text)
    src.text = text + SOURCE_TRUNCATION_MARKER.format(n=max_chars) if truncated else text


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
    *,
    max_chars: int = 120_000,
) -> None:
    """Fetch one ticket over Jira REST into ``src`` within :data:`SPEC_FETCH_BUDGET_S`.

    The response streams through :func:`_read_stream` like a web page, with
    a ``4 * max_chars + 4`` byte cap. A body over the cap fails the source
    instead of being parsed as truncated JSON, and the rendered ticket text
    is cut at ``max_chars`` with the source truncation marker. ``max_chars``
    defaults to the ``PRXREF_SPEC_MAX_CHARS`` default.
    """
    url, auth = _jira_request(ref, jira_base_url, jira_email, jira_api_token)
    byte_cap = 4 * max_chars + 4
    deadline = time.monotonic() + SPEC_FETCH_BUDGET_S
    resp = session.get(url, timeout=SPEC_FETCH_TIMEOUT_S, auth=auth, stream=True)
    try:
        if resp.status_code != 200:
            src.error = _jira_status_error(
                resp.status_code, ref.key, auth=auth, credentials_set=bool(jira_email and jira_api_token)
            )
            return
        body, cut = _read_stream(resp, byte_cap, deadline)
    except _FetchTimeout:
        src.error = f"Jira timed out after {SPEC_FETCH_BUDGET_S:g} s for {ref.key}"
        return
    finally:
        resp.close()
    if cut:
        src.error = f"Jira response for {ref.key} exceeded {byte_cap} bytes"
        return
    try:
        payload = json.loads(body)
    except ValueError:
        content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        src.error = f"Jira returned a non-JSON body for {ref.key} ({content_type or 'no content type'})"
        return
    text = _jira_ticket_text(payload)
    if text is None:
        src.error = f"Jira returned no issue fields for {ref.key}"
        return
    if len(text) > max_chars:
        text = text[:max_chars] + SOURCE_TRUNCATION_MARKER.format(n=max_chars)
    src.text = text


def _jira_request(
    ref: TicketRef, jira_base_url: str, jira_email: str, jira_api_token: str
) -> tuple[str, tuple[str, str] | None]:
    """Resolve a ticket's REST URL and the basic auth to send with it.

    Credentials are only ever sent to ``jira_base_url``: without it the fetch
    is anonymous even when the email and token are set, because the request
    host then comes from the ticket URL, which is not the host the operator
    trusted with them. Both warnings name variables, never their values.
    """
    base = (jira_base_url or ref.base_url).rstrip("/")
    url = f"{base}/rest/api/2/issue/{ref.key}?fields=summary,description,issuetype,labels"
    if not (jira_email and jira_api_token):
        return url, None
    if not jira_base_url:
        logger.warning(
            "PRXREF_JIRA_EMAIL and PRXREF_JIRA_API_TOKEN are set but PRXREF_JIRA_BASE_URL is "
            "empty; Jira credentials are only sent to PRXREF_JIRA_BASE_URL, so %s is fetched "
            "anonymously",
            ref.key,
        )
        return url, None
    if urlparse(jira_base_url).scheme.lower() == "http":
        logger.warning(
            "PRXREF_JIRA_BASE_URL is plain http; the Jira credentials for %s cross the network "
            "unencrypted",
            ref.key,
        )
    return url, (jira_email, jira_api_token)


def _jira_status_error(
    status: int, key: str, *, auth: tuple[str, str] | None, credentials_set: bool
) -> str:
    """Explain a non-200 Jira answer, naming the variables that would fix it.

    An anonymous 401 or 403 is a missing login, and so is an anonymous 404:
    Jira Cloud answers 404, not 403, for a private issue it will not show
    anonymously.
    """
    if auth is not None or status not in (401, 403, 404):
        return f"Jira returned {status} for {key}"
    if credentials_set:
        return (
            f"Jira returned {status} for {key} without credentials: PRXREF_JIRA_EMAIL and "
            "PRXREF_JIRA_API_TOKEN are set, but credentials are only sent to "
            "PRXREF_JIRA_BASE_URL, which is empty; set it to this Jira's base URL to authenticate."
        )
    reason = " (Jira hides a private issue from anonymous readers as 404)" if status == 404 else ""
    return f"Jira returned {status} for {key} without credentials{reason}; set {_JIRA_ENV_HINT} to authenticate."


def _jira_ticket_text(payload: object) -> str | None:
    """Render an issue payload as ``Summary:``/``Type:``/``Labels:`` lines plus the description.

    A header line whose value is empty is left out rather than rendered bare,
    since every non-blank ticket line becomes a digest constraint. Returns
    ``None`` when the payload carries no ``fields`` object.
    """
    fields = payload.get("fields") if isinstance(payload, dict) else None
    if not isinstance(fields, dict):
        return None
    issuetype = fields.get("issuetype")
    raw_labels = fields.get("labels")
    header = [
        ("Summary", str(fields.get("summary") or "").strip()),
        ("Type", str(issuetype.get("name") or "").strip() if isinstance(issuetype, dict) else ""),
        ("Labels", ", ".join(str(label) for label in raw_labels) if isinstance(raw_labels, list) else ""),
    ]
    description = fields.get("description")
    if description is None:
        description = ""
    elif not isinstance(description, str):
        description = json.dumps(description)
    lines = [f"{name}: {value}" for name, value in header if value]
    return "\n".join([*lines, "", description]).strip()


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
    URL's own base when non-empty (self-hosted boards behind a different REST
    host), so every ticket is looked up there by key. Credentials are only
    ever sent to ``jira_base_url`` (``PRXREF_JIRA_BASE_URL``): Jira
    authenticates with basic auth only when ``jira_base_url``, ``jira_email``
    and ``jira_api_token`` are all non-empty, and anonymously otherwise. An
    email and token without a base URL are withheld and a warning names
    ``PRXREF_JIRA_BASE_URL``; a plain-http base URL is honoured with a
    warning.
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


_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

_LIST_ITEM_RE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s")

_TABLE_ROW_RE = re.compile(r"^\s*\|")

_QUOTE_RE = re.compile(r"^\s*(?:>\s?)+")

_CODE_SPAN_RE = re.compile(r"`[^`]*`")

_SENTENCE_BREAK_RE = re.compile(r"[.;!?][\"'”’)\]]*\s+")

_INITIALISM_RE = re.compile(r"(?:[a-z]\.)+")

_ABBREVIATIONS = frozenset({"etc.", "vs.", "cf.", "incl.", "approx.", "fig.", "sec.", "resp.", "viz."})

_PIN_DECORATION_RE = re.compile(r"[`\"'“”‘’*_()\[\]]")

_NORMATIVE_TOKENS = frozenset(
    {"must", "shall", "required", "recommended", "forbidden", "discouraged", "should", "never", "optional"}
)


def _is_version_pin_line(line: str) -> bool:
    """True when ``line`` is nothing but a version pin.

    A list marker and markdown decoration (backticks, quotes, emphasis,
    brackets) are peeled first, so ``- `"2026-07-28"`.`` pins. A date or
    version number inside prose does not: it rides along verbatim when its
    sentence is a kept constraint, and is dropped otherwise.
    """
    bare = _PIN_DECORATION_RE.sub("", _LIST_ITEM_RE.sub("", line, count=1)).strip()
    return _STANDALONE_PIN_RE.match(bare) is not None


def _classify(sentence: str) -> tuple[int, bool] | None:
    if _NAMING_RE.search(sentence):
        return 3, True
    strength = _strength(sentence)
    if strength is not None:
        return strength, strength > 1 or _STRENGTH1_RE.search(sentence) is not None
    if _is_version_pin_line(sentence):
        return 3, False
    return None


def _split_sentences(text: str) -> list[str]:
    masked = _CODE_SPAN_RE.sub(lambda m: "x" * len(m.group()), text)
    sentences: list[str] = []
    start = 0
    for m in _SENTENCE_BREAK_RE.finditer(masked):
        if masked[m.start()] == ".":
            word_start = max(start, masked.rfind(" ", start, m.start()) + 1)
            word = masked[word_start : m.start() + 1].lstrip("(\"'“‘[").lower()
            if word in _ABBREVIATIONS or _INITIALISM_RE.fullmatch(word):
                continue
        sentences.append(text[start : m.end()].strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


@dataclass
class _Block:
    """One structural element of a spec text, anchored on its first line."""

    line: int
    kind: str
    indent: int
    parts: list[str]

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


def _blocks(text: str) -> list[_Block]:
    lines = text.splitlines()
    blocks: list[_Block] = []
    current: _Block | None = None
    current_quoted = False
    fence: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        i += 1
        if fence is not None:
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
                fence = None
                blocks.append(_Block(i, "break", 0, []))
            elif stripped:
                blocks.append(_Block(i, "code", 0, [stripped]))
            continue
        opener = _FENCE_RE.match(line)
        if opener:
            fence = opener.group(1)
            current = None
            blocks.append(_Block(i, "break", 0, []))
            continue
        if not stripped:
            current = None
            continue
        md = _MD_HEADING_RE.match(line)
        setext = (
            md is None
            and _SETEXT_TEXT_RE.match(line) is not None
            and i < len(lines)
            and _SETEXT_UNDER_RE.match(lines[i]) is not None
            and _strength(line) is None
        )
        if md or setext:
            current = None
            blocks.append(_Block(i, "heading", 0, [(md.group(1) if md else line).strip()]))
            i += 1 if setext else 0
            continue
        if _SETEXT_UNDER_RE.match(line):
            current = None
            blocks.append(_Block(i, "break", 0, []))
            continue
        if _TABLE_ROW_RE.match(line):
            current = None
            blocks.append(_Block(i, "row", 0, [stripped]))
            continue
        quote = _QUOTE_RE.match(line)
        content = line[quote.end() :] if quote else line
        if not content.strip():
            current = None
            continue
        if current is not None and current_quoted != bool(quote):
            current = None
        current_quoted = bool(quote)
        item = _LIST_ITEM_RE.match(content)
        if item:
            current = _Block(i, "item", len(item.group(1).expandtabs(4)), [content.strip()])
            blocks.append(current)
        elif current is not None:
            current.parts.append(content.strip())
        else:
            current = _Block(i, "text", 0, [content.strip()])
            blocks.append(current)
    return blocks


def _block_statements(block: _Block) -> list[tuple[int, str]]:
    text = block.text()
    if block.kind in ("row", "code"):
        hit = _classify(text)
        return [(hit[0], _one_line(text, _STATEMENT_MAX_CHARS))] if hit else []
    hits = [(s, c) for s in _split_sentences(text) if (c := _classify(s)) is not None]
    if not hits:
        return []
    if len(text) > _STATEMENT_MAX_CHARS or len(hits) >= 2:
        return [(strength, _one_line(s, _STATEMENT_MAX_CHARS)) for s, (strength, _) in hits]
    sentence, (strength, strong) = hits[0]
    return [(strength, text if strong else sentence)]


def _attach_list(statement: str, blocks: list[_Block], pos: int) -> str:
    lead = blocks[pos]
    parts = [statement]
    size = len(statement)
    for block in blocks[pos + 1 :]:
        if block.kind != "item" or (lead.kind == "item" and block.indent <= lead.indent):
            break
        item = block.text()
        if size + 1 + len(item) > _STATEMENT_MAX_CHARS:
            break
        parts.append(item)
        size += 1 + len(item)
    return " ".join(parts)


def _spec_units(src: SpecSource, source_idx: int) -> list[_Unit]:
    """Extract the constraint units of one non-ticket source, in document order.

    Lines are grouped into blocks before anything is matched: a paragraph or
    list item joins its wrapped and indented continuation lines, while a
    blank line, heading, setext underline, code fence, table row or new list
    item ends a block. Table rows and fenced lines are single-line units and
    never headings. Each block is split into sentences (``e.g.``/``i.e.``
    and code spans never end one) and every sentence is matched on its own
    for RFC-2119 keywords, naming rules and standalone version pins. A
    block over :data:`_STATEMENT_MAX_CHARS` or holding two or more matching
    sentences yields one capped unit per matching sentence, each with its
    own strength; otherwise the block is kept whole, unless its only match
    is a prose ``can``/``discouraged`` or a bare pin, which keeps just that
    sentence. A unit that ends its block with ``:`` carries the following
    list items (nested ones only, under a list item) while they fit the
    cap; those items are still matched on their own. Every unit is anchored
    ``L{n}`` on its block's first original line.
    """
    short = _origin_short(src.origin)
    blocks = _blocks(src.text)
    units: list[_Unit] = []
    heading: tuple[str, str] | None = None
    for pos, block in enumerate(blocks):
        if block.kind == "heading":
            text = block.parts[0]
            slug = _heading_slug(text)
            heading = (
                f"[spec:{short}#{slug}] (heading) {_one_line(text, _STATEMENT_MAX_CHARS)}",
                slug,
            )
            continue
        if block.kind == "break":
            continue
        ends_with_colon = block.kind in ("text", "item") and block.text().endswith(":")
        for strength, statement in _block_statements(block):
            if ends_with_colon and statement.endswith(":"):
                statement = _attach_list(statement, blocks, pos)
            label = {3: "MUST", 2: "SHOULD", 1: "MAY"}[strength]
            units.append(
                _Unit(
                    origin=src.origin,
                    source_idx=source_idx,
                    doc_idx=block.line,
                    strength=strength,
                    is_ticket=False,
                    render=f"[spec:{short}#L{block.line}] ({label}) {statement}",
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
        u.score = len((u.tokens & diff_toks) - _NORMATIVE_TOKENS)
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
