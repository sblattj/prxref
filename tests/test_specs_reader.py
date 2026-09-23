"""The spec reader: a wall-clock budget per source, one quick retry, charset decoding, a marker that survives HTML.

SEC-6: a source is held to ``SPEC_FETCH_BUDGET_S`` from before its request,
however the host paces the body (close-delimited, chunked, compressed), and
Jira streams through the same reader under a byte cap. The session retries
once and never sleeps for ``Retry-After``.

COR-6: the charset comes from the ``Content-Type`` header, then an HTML
``<meta>``, then strict UTF-8, then cp1252; requests' ISO-8859-1 default for
a charset-less ``text/*`` is never used.

Backlog 2: the truncation marker is appended after HTML stripping, and a cut
that lands exactly on ``max_chars`` is still marked.

The budget and charset tests run against a real socket on 127.0.0.1. A fake
``iter_content`` that yields on a timer passes a deadline check that never
runs in production, where the read blocks inside urllib3 until 8192 bytes or
EOF arrive, and a fake ``encoding`` hides what requests really reports.
"""
from __future__ import annotations

import gzip
import json
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

from prxref import specs
from prxref.retry_logging import LoggingRetry
from prxref.specs import SOURCE_TRUNCATION_MARKER, SpecSource, fetch_specs, parse_ticket_url
from tests.test_specs import _FakeResponse, _FakeSession

BUDGET_S = 0.5
READ_TIMEOUT_S = 5.0
GAP_S = 0.05
TRICKLE_LIMIT_S = 8.0
SLACK_S = 2.0

SENTENCE = "Clients MUST send the “Mcp-Session-Id” header — always. Café naïve."
LATIN1_SENTENCE = "Clients MUST send the header. Café naïve à la carte."
GREEK_SENTENCE = "Clients MUST send the Ωμέγα header."
TICKET_FIELDS = {"summary": "Rotate keys", "description": "Keys MUST rotate every 90 days."}
TICKET_TEXT = "Summary: Rotate keys\n\nKeys MUST rotate every 90 days."

Route = Callable[[BaseHTTPRequestHandler, threading.Event], None]


class _Server:
    """A local HTTP/1.0 server whose routes write their own responses and count their hits."""

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.hits: dict[str, int] = {}
        self.stop = threading.Event()
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                outer.hits[path] = outer.hits.get(path, 0) + 1
                route = outer.routes.get(path)
                try:
                    if route is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    route(self, outer.stop)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, format, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def url(self, path: str) -> str:
        return self.base + path

    def close(self) -> None:
        self.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()


def _send(h: BaseHTTPRequestHandler, body: bytes, content_type: str, *, status: int = 200,
          headers: tuple[tuple[str, str], ...] = ()) -> None:
    h.send_response(status)
    h.send_header("Content-Type", content_type)
    h.send_header("Content-Length", str(len(body)))
    for name, value in headers:
        h.send_header(name, value)
    h.end_headers()
    h.wfile.write(body)


def _body(body: bytes, content_type: str, *, status: int = 200,
          headers: tuple[tuple[str, str], ...] = ()) -> Route:
    return lambda h, stop: _send(h, body, content_type, status=status, headers=headers)


def _gzipped(body: bytes, content_type: str) -> Route:
    return _body(gzip.compress(body), content_type, headers=(("Content-Encoding", "gzip"),))


def _delayed(delay_s: float, body: bytes, content_type: str) -> Route:
    def route(h: BaseHTTPRequestHandler, stop: threading.Event) -> None:
        stop.wait(delay_s)
        _send(h, body, content_type)

    return route


def _trickle(content_type: str = "text/plain", first: bytes = b"") -> Route:
    """No Content-Length: the body is close-delimited, one byte every GAP_S until the client leaves."""

    def route(h: BaseHTTPRequestHandler, stop: threading.Event) -> None:
        h.send_response(200)
        h.send_header("Content-Type", content_type)
        h.end_headers()
        h.wfile.write(first)
        end = time.monotonic() + TRICKLE_LIMIT_S
        while not stop.is_set() and time.monotonic() < end:
            h.wfile.write(b"x")
            stop.wait(GAP_S)

    return route


def _chunked_trickle(h: BaseHTTPRequestHandler, stop: threading.Event) -> None:
    h.protocol_version = "HTTP/1.1"
    h.send_response(200)
    h.send_header("Content-Type", "text/plain")
    h.send_header("Transfer-Encoding", "chunked")
    h.send_header("Connection", "close")
    h.end_headers()
    end = time.monotonic() + TRICKLE_LIMIT_S
    while not stop.is_set() and time.monotonic() < end:
        h.wfile.write(b"1\r\nx\r\n")
        stop.wait(GAP_S)
    h.wfile.write(b"0\r\n\r\n")


@pytest.fixture
def server(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    srv = _Server()
    yield srv
    srv.close()


@pytest.fixture
def short_budget(monkeypatch):
    monkeypatch.setattr(specs, "SPEC_FETCH_BUDGET_S", BUDGET_S)
    monkeypatch.setattr(specs, "SPEC_FETCH_TIMEOUT_S", READ_TIMEOUT_S)


def _fetch(url: str, max_chars: int = 100_000, **kwargs) -> tuple[SpecSource, float]:
    start = time.monotonic()
    (src,) = fetch_specs([url], max_chars=max_chars, **kwargs)
    return src, time.monotonic() - start


def _marker(n: int) -> str:
    return SOURCE_TRUNCATION_MARKER.format(n=n)


class _DribbleResponse:
    """A response without ``raw.read1`` whose ``iter_content`` yields a byte every GAP_S."""

    status_code = 200

    def __init__(self) -> None:
        self.headers = CaseInsensitiveDict({"Content-Type": "text/plain"})
        self.chunk_sizes: list[int] = []
        self.closed = False

    def iter_content(self, chunk_size: int = 1, **kwargs) -> Iterator[bytes]:
        self.chunk_sizes.append(chunk_size)
        end = time.monotonic() + TRICKLE_LIMIT_S
        while time.monotonic() < end:
            time.sleep(GAP_S)
            yield b"x"

    def close(self) -> None:
        self.closed = True


@pytest.mark.usefixtures("short_budget")
class TestBudget:
    """GAP_S is far below the read timeout, so only the budget can end these fetches near BUDGET_S."""

    def test_close_delimited_trickle_fails_at_the_budget(self, server):
        server.routes["/trickle"] = _trickle()
        src, elapsed = _fetch(server.url("/trickle"))
        assert src.kind == "url"
        assert src.error == f"timed out after {BUDGET_S:g} s"
        assert src.text == ""
        assert BUDGET_S <= elapsed < BUDGET_S + SLACK_S

    def test_chunked_trickle_fails_at_the_budget(self, server):
        server.routes["/chunked"] = _chunked_trickle
        src, elapsed = _fetch(server.url("/chunked"))
        assert src.error == f"timed out after {BUDGET_S:g} s"
        assert BUDGET_S <= elapsed < BUDGET_S + SLACK_S

    def test_jira_trickle_fails_at_the_budget(self, server):
        server.routes["/rest/api/2/issue/ABC-1"] = _trickle("application/json", first=b'{"fields": {"summary": "')
        src, elapsed = _fetch(server.url("/browse/ABC-1"))
        assert src.kind == "jira"
        assert src.error == f"Jira timed out after {BUDGET_S:g} s for ABC-1"
        assert src.text == ""
        assert BUDGET_S <= elapsed < BUDGET_S + SLACK_S

    def test_the_budget_counts_the_wait_for_headers(self, server, monkeypatch):
        server.routes["/late"] = _delayed(BUDGET_S + 0.5, b"Clients MUST retry.", "text/plain")
        src, elapsed = _fetch(server.url("/late"))
        assert src.error == f"timed out after {BUDGET_S:g} s"
        assert elapsed < BUDGET_S + 0.5 + SLACK_S
        monkeypatch.setattr(specs, "SPEC_FETCH_BUDGET_S", 10)
        src, _ = _fetch(server.url("/late"))
        assert (src.error, src.text) == ("", "Clients MUST retry.")

    def test_without_read1_the_clock_is_checked_per_byte(self):
        resp = _DribbleResponse()
        src, elapsed = _fetch("https://example.com/slow.md", session=_FakeSession(resp))
        assert src.error == f"timed out after {BUDGET_S:g} s"
        assert resp.chunk_sizes == [1]
        assert resp.closed
        assert elapsed < BUDGET_S + SLACK_S


class TestRetryPolicy:
    def test_the_session_retries_once_and_ignores_retry_after(self):
        session = specs._create_default_session()
        for url in ("https://example.com/spec.md", "http://example.com/spec.md"):
            retry = session.get_adapter(url).max_retries
            assert isinstance(retry, LoggingRetry)
            assert retry.total == 1
            assert retry.respect_retry_after_header is False
            assert retry.allowed_methods == frozenset({"GET", "HEAD", "OPTIONS"})

    def test_a_503_asking_for_30_s_is_not_waited_for(self, server):
        server.routes["/busy"] = _body(b"", "text/plain", status=503, headers=(("Retry-After", "30"),))
        src, elapsed = _fetch(server.url("/busy"))
        assert src.error != ""
        assert src.text == ""
        assert server.hits["/busy"] == 2
        assert elapsed < SLACK_S

    def test_control_a_session_honouring_retry_after_does_wait(self, server):
        server.routes["/busy"] = _body(b"", "text/plain", status=503, headers=(("Retry-After", "1"),))
        session = requests.Session()
        retry = LoggingRetry(
            total=1, status_forcelist=[503], respect_retry_after_header=True,
            allowed_methods=frozenset(["GET"]),
        )
        session.mount("http://", HTTPAdapter(max_retries=retry))
        src, elapsed = _fetch(server.url("/busy"), session=session)
        assert src.error != ""
        assert server.hits["/busy"] == 2
        assert elapsed >= 1.0


class TestBodies:
    def test_a_fast_body_is_read_whole(self, server):
        text = "The API MUST keep x.\n" * 2000
        server.routes["/fast.md"] = _body(text.encode(), "text/markdown")
        src, _ = _fetch(server.url("/fast.md"))
        assert (src.error, src.text) == ("", text)

    def test_a_gzip_body_reads_like_the_plain_one(self, server):
        text = "The API MUST keep x.\n" * 2000
        server.routes["/plain.md"] = _body(text.encode(), "text/markdown")
        server.routes["/gz.md"] = _gzipped(text.encode(), "text/markdown")
        plain, _ = _fetch(server.url("/plain.md"))
        packed, _ = _fetch(server.url("/gz.md"))
        assert packed.error == ""
        assert packed.text == plain.text == text

    def test_the_byte_cap_ends_a_large_body_with_the_marker(self, server):
        server.routes["/big.txt"] = _body(b"x" * 50_000, "text/plain")
        src, _ = _fetch(server.url("/big.txt"), max_chars=100)
        assert src.text == "x" * 100 + _marker(100)

    @pytest.mark.parametrize(("size", "marked"), [(99, False), (100, False), (101, True)])
    def test_the_marker_appears_only_past_max_chars(self, server, size, marked):
        server.routes["/edge.txt"] = _body(b"y" * size, "text/plain")
        src, _ = _fetch(server.url("/edge.txt"), max_chars=100)
        assert src.text == "y" * min(size, 100) + (_marker(100) if marked else "")

    def test_a_chunk_ending_exactly_at_max_chars_is_still_marked(self):
        session = _FakeSession(_FakeResponse(content_type="text/plain", chunks=[b"a" * 100, b"b" * 10]))
        (src,) = fetch_specs(["https://example.com/edge.md"], max_chars=100, session=session)
        assert src.text == "a" * 100 + _marker(100)

    def test_a_multibyte_char_split_by_the_byte_cap_is_not_mojibake(self, server):
        server.routes["/euro.txt"] = _body(("€" * 1000).encode(), "text/plain")
        src, _ = _fetch(server.url("/euro.txt"), max_chars=100)
        assert src.text == "€" * 100 + _marker(100)

    @pytest.mark.parametrize(
        "response",
        [
            _FakeResponse(content_type="text/plain", body=b"ok"),
            _FakeResponse(status_code=404, content_type="text/plain"),
            _FakeResponse(content_type="application/octet-stream", body=b"\x00"),
        ],
        ids=["read", "http-error", "not-text"],
    )
    def test_the_response_is_always_closed(self, response):
        fetch_specs(["https://example.com/spec.md"], max_chars=100, session=_FakeSession(response))
        assert response.closed


class TestCharset:
    @pytest.mark.parametrize(
        ("content_type", "body", "expected"),
        [
            ("text/markdown", f"# Spec\n\n{SENTENCE}\n".encode(), SENTENCE),
            ("text/plain", SENTENCE.encode(), SENTENCE),
            ("text/html", f'<html><head><meta charset="utf-8"></head><p>{SENTENCE}</p></html>'.encode(), SENTENCE),
            ("text/html", f"<html><p>{SENTENCE}</p></html>".encode(), SENTENCE),
            ("text/plain", LATIN1_SENTENCE.encode("latin-1"), LATIN1_SENTENCE),
            ("text/plain; charset=iso-8859-1", LATIN1_SENTENCE.encode("latin-1"), LATIN1_SENTENCE),
            (
                "text/html",
                f'<meta charset="iso-8859-7"><p>{GREEK_SENTENCE}</p>'.encode("iso-8859-7"),
                GREEK_SENTENCE,
            ),
            (
                "text/html",
                (
                    '<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-7">'
                    f"<p>{GREEK_SENTENCE}</p>"
                ).encode("iso-8859-7"),
                GREEK_SENTENCE,
            ),
            ("text/html; charset=utf-8", f'<meta charset="iso-8859-7"><p>{SENTENCE}</p>'.encode(), SENTENCE),
            ("text/plain; charset=utf8mb4", SENTENCE.encode(), SENTENCE),
            ("text/plain; charset=base64", SENTENCE.encode(), SENTENCE),
            ("text/xml", f"<r>{SENTENCE}</r>".encode(), SENTENCE),
        ],
        ids=[
            "markdown-no-charset", "plain-no-charset", "html-meta-utf8", "html-no-meta",
            "latin1-no-charset", "latin1-declared", "html-meta-greek", "html-http-equiv-greek",
            "header-beats-meta", "unknown-charset-falls-through", "non-text-codec-falls-through",
            "text-xml-no-charset",
        ],
    )
    def test_decoding(self, server, content_type, body, expected):
        server.routes["/doc"] = _body(body, content_type)
        src, _ = _fetch(server.url("/doc"))
        assert src.error == ""
        assert expected in src.text

    @pytest.mark.parametrize("content_type", ["text/plain", "text/plain; charset=utf-8"])
    def test_a_utf8_byte_order_mark_is_dropped(self, server, content_type):
        server.routes["/bom.txt"] = _body(b"\xef\xbb\xbf" + SENTENCE.encode(), content_type)
        src, _ = _fetch(server.url("/bom.txt"))
        assert src.text == SENTENCE


class TestHtmlTruncationMarker:
    def test_a_cut_inside_a_script_keeps_the_marker(self, server):
        body = b"<html><body><p>Clients MUST retry.</p><script>" + b"var x = 1;" * 100 + b"</script></body></html>"
        server.routes["/page.html"] = _body(body, "text/html; charset=utf-8")
        src, _ = _fetch(server.url("/page.html"), max_chars=80)
        assert "Clients MUST retry." in src.text
        assert "var x" not in src.text
        assert src.text.endswith(_marker(80))

    def test_a_cut_inside_an_open_tag_keeps_the_marker(self, server):
        body = b'<p>Clients MUST retry.</p><a href="' + b"x" * 500 + b'">link</a>'
        server.routes["/page.html"] = _body(body, "text/html")
        src, _ = _fetch(server.url("/page.html"), max_chars=60)
        assert src.text == "Clients MUST retry." + _marker(60)

    def test_an_uncut_page_has_no_marker(self, server):
        server.routes["/page.html"] = _body(b"<p>Clients MUST retry.</p>", "text/html")
        src, _ = _fetch(server.url("/page.html"), max_chars=60)
        assert src.text == "Clients MUST retry."


class TestJiraBody:
    def _route(self, server: _Server, key: str, route: Route) -> str:
        server.routes[f"/rest/api/2/issue/{key}"] = route
        return server.url(f"/browse/{key}")

    def _fetch_jira(self, server: _Server, key: str, route: Route, *, max_chars: int) -> SpecSource:
        url = self._route(server, key, route)
        src = SpecSource(origin=url, kind="jira", text="", error="")
        ref = parse_ticket_url(url)
        assert ref is not None
        specs._fetch_jira(src, ref, "", "", "", specs._create_default_session(), max_chars=max_chars)
        return src

    def test_a_ticket_reads_over_a_real_socket(self, server):
        url = self._route(server, "ABC-3", _body(json.dumps({"fields": TICKET_FIELDS}).encode(), "application/json"))
        src, _ = _fetch(url)
        assert (src.kind, src.error, src.text) == ("jira", "", TICKET_TEXT)

    def test_a_gzip_ticket_reads_like_the_plain_one(self, server):
        url = self._route(server, "ABC-4", _gzipped(json.dumps({"fields": TICKET_FIELDS}).encode(), "application/json"))
        src, _ = _fetch(url)
        assert (src.error, src.text) == ("", TICKET_TEXT)

    def test_a_body_over_the_cap_is_a_clean_error(self, server):
        big = json.dumps({"fields": {"summary": "s", "description": "D" * 500_000}}).encode()
        url = self._route(server, "ABC-2", _body(big, "application/json"))
        src, _ = _fetch(url, max_chars=120_000)
        assert src.text == ""
        assert src.error == "Jira response for ABC-2 exceeded 480004 bytes"

    def test_the_cap_follows_max_chars(self, server):
        body = json.dumps({"fields": TICKET_FIELDS}).encode()
        src = self._fetch_jira(server, "ABC-5", _body(body, "application/json"), max_chars=10)
        assert src.text == ""
        assert src.error == "Jira response for ABC-5 exceeded 44 bytes"

    def test_ticket_text_is_cut_at_max_chars(self, server):
        body = json.dumps({"fields": TICKET_FIELDS}).encode()
        src = self._fetch_jira(server, "ABC-6", _body(body, "application/json"), max_chars=30)
        assert src.error == ""
        assert src.text == TICKET_TEXT[:30] + _marker(30)

    def test_a_login_page_keeps_the_non_json_error(self, server):
        url = self._route(server, "ABC-7", _body(b"<html>Log in</html>", "text/html; charset=utf-8"))
        src, _ = _fetch(url)
        assert src.error == "Jira returned a non-JSON body for ABC-7 (text/html)"

    def test_invalid_utf8_is_a_non_json_error(self, server):
        url = self._route(server, "ABC-8", _body(b'{"fields": {"summary": "\xff"}}', "application/json"))
        src, _ = _fetch(url)
        assert src.text == ""
        assert src.error == "Jira returned a non-JSON body for ABC-8 (application/json)"
