"""Jira ticket sources: URL shapes, where credentials go, the fetch hints, the rendered text.

SEC-2: Jira credentials are only ever sent to ``PRXREF_JIRA_BASE_URL``. With an
email and token but no base URL the fetch is anonymous and a warning names the
missing variable; a plain-http base URL is honoured with a warning. The last
class drives ``cli._run_review`` against a local recording server, which is
the only place the config wiring and the credential rule meet.

COR-2: a ticket URL under a Jira context path, a Cloud issue view, and a Cloud
board's ``selectedIssue`` are recognized; a Bitbucket Server file URL is not.

COR-4: an empty ``Type:`` or ``Labels:`` value is left out rather than
rendered as a bare line, since every non-blank ticket line is a constraint.

Backlog 9b: a 200 whose body is not a JSON issue fails its source cleanly.
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from prxref import cli
from prxref.specs import SpecSource, build_spec_digest, fetch_specs, parse_ticket_url
from tests.test_orchestrator import REF, FakeForge, FakeLLM, _added_file_diff
from tests.test_specs import _FakeResponse, _FakeSession

EMAIL = "ops@example.com"
TOKEN = "secret-token-value"
TICKET = "https://jira.example.com/browse/AUTH-7"
FIELDS_QUERY = "?fields=summary,description,issuetype,labels"
PAYLOAD = {
    "fields": {
        "summary": "Fix login flow",
        "issuetype": {"name": "Bug"},
        "labels": ["auth", "urgent"],
        "description": "Users MUST re-authenticate after password change.",
    }
}


def _json_session(status_code: int = 200, payload: object = PAYLOAD) -> _FakeSession:
    return _FakeSession(_FakeResponse(status_code=status_code, content_type="application/json", payload=payload))


def _real_response(body: bytes, content_type: str, status_code: int = 200) -> requests.Response:
    resp = requests.models.Response()
    resp.status_code = status_code
    resp._content = body
    resp._content_consumed = True
    resp.headers["Content-Type"] = content_type
    return resp


def _spec_warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "prxref.specs" and r.levelno == logging.WARNING]


class TestTicketUrlShapes:
    @pytest.mark.parametrize(
        ("url", "base", "key"),
        [
            ("https://jira.acme.com/browse/PROJ-12?focusedCommentId=5", "https://jira.acme.com", "PROJ-12"),
            ("https://issues.apache.org/jira/browse/KAFKA-1", "https://issues.apache.org/jira", "KAFKA-1"),
            ("https://acme.com/tools/jira/browse/OPS-1", "https://acme.com/tools/jira", "OPS-1"),
            ("https://acme.com/jira/rest/api/2/issue/OPS-2", "https://acme.com/jira", "OPS-2"),
            ("https://acme.com/tools/jira/rest/api/3/issue/OPS-3", "https://acme.com/tools/jira", "OPS-3"),
            (
                "https://acme.atlassian.net/jira/software/projects/ENG/issues/ENG-9",
                "https://acme.atlassian.net",
                "ENG-9",
            ),
            (
                "https://acme.atlassian.net/jira/software/c/projects/ENG/issues/ENG-10?jql=all",
                "https://acme.atlassian.net",
                "ENG-10",
            ),
            (
                "https://acme.atlassian.net/jira/software/c/projects/ENG/boards/12?a=1&selectedIssue=ENG-42",
                "https://acme.atlassian.net",
                "ENG-42",
            ),
            (
                "https://acme.atlassian.net/jira/software/projects/ENG/boards/3/backlog?selectedIssue=ENG-7",
                "https://acme.atlassian.net",
                "ENG-7",
            ),
        ],
    )
    def test_recognized(self, url, base, key):
        ref = parse_ticket_url(url)
        assert ref is not None
        assert (ref.base_url, ref.key, ref.url) == (base, key, url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://bitbucket.acme.com/projects/ARCH/repos/adrs/browse/ADR-0012",
            "https://acme.com/bitbucket/projects/ARCH/repos/adrs/browse/ADR-0012",
            "https://bitbucket.acme.com/users/jdoe/repos/notes/browse/ADR-0012",
            "https://acme.com/a/b/c/browse/OPS-1",
            "https://acme.com/a/b/c/rest/api/2/issue/OPS-1",
            "https://jira.acme.com?x=/browse/ABC-1",
            "https://acme.atlassian.net/jira/software/c/projects/ENG/boards/12",
            "https://acme.atlassian.net/jira/software/c/projects/ENG/boards/12?selectedIssue=eng-1",
            "https://acme.atlassian.net/jira/software/c/projects/ENG/boards/12?selectedIssue=ENG-1x",
            "https://example.com/boards?selectedIssue=ENG-1",
            "https://example.com/wiki/jira?selectedIssue=ENG-1",
            "ftp://acme.com/jira/boards?selectedIssue=ENG-1",
        ],
    )
    def test_not_a_ticket(self, url):
        assert parse_ticket_url(url) is None

    def test_documented_residual_short_browse_path_matches(self):
        ref = parse_ticket_url("https://git.acme.com/scm/browse/ADR-1")
        assert ref is not None
        assert (ref.base_url, ref.key) == ("https://git.acme.com/scm", "ADR-1")


class TestContextPathDispatch:
    def test_context_path_ticket_reaches_jira_rest_under_the_context_path(self):
        session = _json_session()
        (src,) = fetch_specs(["https://acme.com/jira/browse/OPS-1"], max_chars=1000, session=session)
        assert src.kind == "jira"
        assert src.error == ""
        assert session.calls[0][0] == "https://acme.com/jira/rest/api/2/issue/OPS-1" + FIELDS_QUERY

    def test_board_ticket_reaches_jira_rest_on_the_host(self):
        session = _json_session()
        url = "https://acme.atlassian.net/jira/software/c/projects/ENG/boards/12?a=1&selectedIssue=ENG-42"
        (src,) = fetch_specs([url], max_chars=1000, session=session)
        assert src.kind == "jira"
        assert session.calls[0][0] == "https://acme.atlassian.net/rest/api/2/issue/ENG-42" + FIELDS_QUERY

    def test_bitbucket_server_browse_url_is_fetched_as_a_page(self):
        url = "https://bitbucket.acme.com/projects/ARCH/repos/adrs/browse/ADR-0012"
        session = _FakeSession(_FakeResponse(content_type="text/plain", body=b"ADRs MUST be numbered."))
        (src,) = fetch_specs([url], max_chars=1000, session=session)
        assert src.kind == "url"
        assert session.calls[0][0] == url


class TestCredentialsOnlyGoToTheBaseUrl:
    def test_credentials_without_base_fetch_anonymously_and_warn(self, caplog):
        caplog.set_level(logging.WARNING, logger="prxref.specs")
        session = _json_session()
        (src,) = fetch_specs([TICKET], max_chars=1000, jira_email=EMAIL, jira_api_token=TOKEN, session=session)
        assert src.error == ""
        url, kwargs = session.calls[0]
        assert kwargs["auth"] is None
        assert url.startswith("https://jira.example.com/rest/api/2/issue/AUTH-7")
        (warning,) = _spec_warnings(caplog)
        assert "PRXREF_JIRA_BASE_URL" in warning.getMessage()
        assert TOKEN not in caplog.text
        assert EMAIL not in caplog.text

    def test_credentials_reach_a_configured_https_base(self, caplog):
        caplog.set_level(logging.WARNING, logger="prxref.specs")
        session = _json_session()
        fetch_specs(
            [TICKET], max_chars=1000, jira_base_url="https://rest.example.com/jira/",
            jira_email=EMAIL, jira_api_token=TOKEN, session=session,
        )
        url, kwargs = session.calls[0]
        assert kwargs["auth"] == (EMAIL, TOKEN)
        assert url == "https://rest.example.com/jira/rest/api/2/issue/AUTH-7" + FIELDS_QUERY
        assert _spec_warnings(caplog) == []

    def test_a_foreign_ticket_host_never_receives_the_credentials(self):
        session = _json_session()
        fetch_specs(
            ["https://tickets.other.example/browse/AUTH-7"], max_chars=1000,
            jira_base_url="https://jira.example.com", jira_email=EMAIL, jira_api_token=TOKEN, session=session,
        )
        url, kwargs = session.calls[0]
        assert url.startswith("https://jira.example.com/rest/api/2/issue/AUTH-7")
        assert kwargs["auth"] == (EMAIL, TOKEN)

    def test_plain_http_base_is_honoured_with_a_warning(self, caplog):
        caplog.set_level(logging.WARNING, logger="prxref.specs")
        session = _json_session()
        (src,) = fetch_specs(
            [TICKET], max_chars=1000, jira_base_url="http://jira.internal",
            jira_email=EMAIL, jira_api_token=TOKEN, session=session,
        )
        assert src.error == ""
        assert session.calls[0][1]["auth"] == (EMAIL, TOKEN)
        (warning,) = _spec_warnings(caplog)
        assert "PRXREF_JIRA_BASE_URL" in warning.getMessage()
        assert "http" in warning.getMessage()
        assert TOKEN not in caplog.text

    @pytest.mark.parametrize(
        ("base", "email", "token"),
        [("", "", ""), ("https://jira.example.com", "", ""), ("https://jira.example.com", EMAIL, "")],
    )
    def test_incomplete_credentials_are_anonymous_and_silent(self, caplog, base, email, token):
        caplog.set_level(logging.WARNING, logger="prxref.specs")
        session = _json_session()
        fetch_specs([TICKET], max_chars=1000, jira_base_url=base, jira_email=email, jira_api_token=token,
                    session=session)
        assert session.calls[0][1]["auth"] is None
        assert _spec_warnings(caplog) == []


class TestStatusHints:
    def test_anonymous_404_without_credentials_names_every_variable(self):
        (src,) = fetch_specs([TICKET], max_chars=1000, session=_json_session(status_code=404))
        assert src.text == ""
        assert src.error.startswith("Jira returned 404 for AUTH-7 without credentials")
        assert "private issue" in src.error
        for name in ("PRXREF_JIRA_BASE_URL", "PRXREF_JIRA_EMAIL", "PRXREF_JIRA_API_TOKEN"):
            assert name in src.error

    @pytest.mark.parametrize("status", [401, 403, 404])
    def test_credentials_without_base_explain_where_credentials_go(self, status):
        (src,) = fetch_specs(
            [TICKET], max_chars=1000, jira_email=EMAIL, jira_api_token=TOKEN,
            session=_json_session(status_code=status),
        )
        assert src.error.startswith(f"Jira returned {status} for AUTH-7 without credentials")
        assert "only sent to PRXREF_JIRA_BASE_URL" in src.error
        assert TOKEN not in src.error
        assert EMAIL not in src.error

    @pytest.mark.parametrize("status", [401, 404])
    def test_authenticated_failure_gives_no_env_advice(self, status):
        (src,) = fetch_specs(
            [TICKET], max_chars=1000, jira_base_url="https://jira.example.com",
            jira_email=EMAIL, jira_api_token=TOKEN, session=_json_session(status_code=status),
        )
        assert src.error == f"Jira returned {status} for AUTH-7"

    def test_anonymous_server_error_gives_no_env_advice(self):
        (src,) = fetch_specs([TICKET], max_chars=1000, session=_json_session(status_code=500))
        assert src.error == "Jira returned 500 for AUTH-7"


class TestNonIssueBodies:
    def test_non_json_200_is_a_clean_source_error(self):
        session = _FakeSession(_real_response(b"<html>Log in to continue</html>", "text/html; charset=utf-8"))
        (src,) = fetch_specs([TICKET], max_chars=1000, session=session)
        assert src.kind == "jira"
        assert src.text == ""
        assert src.error == "Jira returned a non-JSON body for AUTH-7 (text/html)"

    def test_non_json_200_without_content_type(self):
        session = _FakeSession(_real_response(b"", ""))
        (src,) = fetch_specs([TICKET], max_chars=1000, session=session)
        assert src.error == "Jira returned a non-JSON body for AUTH-7 (no content type)"

    @pytest.mark.parametrize("payload", [[], {"errorMessages": ["nope"]}, {"fields": None}, {"fields": "x"}])
    def test_json_without_issue_fields_is_a_source_error(self, payload):
        session = _FakeSession(_real_response(json.dumps(payload).encode(), "application/json"))
        (src,) = fetch_specs([TICKET], max_chars=1000, session=session)
        assert src.text == ""
        assert src.error == "Jira returned no issue fields for AUTH-7"


class TestTicketText:
    def _text(self, fields: dict) -> str:
        (src,) = fetch_specs([TICKET], max_chars=1000, session=_json_session(payload={"fields": fields}))
        assert src.error == ""
        return src.text

    def test_full_payload_renders_header_then_description(self):
        assert self._text(PAYLOAD["fields"]) == (
            "Summary: Fix login flow\nType: Bug\nLabels: auth, urgent\n\n"
            "Users MUST re-authenticate after password change."
        )

    def test_missing_type_and_labels_are_dropped(self):
        text = self._text({"summary": "Fix login flow", "description": "Tokens MUST expire."})
        assert text == "Summary: Fix login flow\n\nTokens MUST expire."

    def test_empty_type_and_labels_are_dropped(self):
        text = self._text(
            {"summary": "Fix login flow", "issuetype": {"name": None}, "labels": [], "description": "D."}
        )
        assert "Type:" not in text
        assert "Labels:" not in text
        assert text == "Summary: Fix login flow\n\nD."

    def test_non_list_labels_are_not_split_into_characters(self):
        text = self._text({"summary": "S", "labels": "auth"})
        assert text == "Summary: S"

    def test_structured_description_is_serialized(self):
        doc = {"type": "doc", "content": [{"type": "text", "text": "Hello"}]}
        text = self._text({"summary": "S", "description": doc})
        assert text == "Summary: S\n\n" + json.dumps(doc)

    def test_every_digest_ticket_line_carries_content(self):
        text = self._text({"summary": "Rotate keys", "description": "Keys MUST rotate every 90 days."})
        src = SpecSource(origin=TICKET, kind="jira", text=text, error="")
        digest = build_spec_digest([src], [], token_budget=3000)
        ticket_lines = [ln for ln in digest.splitlines() if ln.startswith("[ticket:AUTH-7]")]
        assert ticket_lines == [
            "[ticket:AUTH-7] Summary: Rotate keys",
            "[ticket:AUTH-7] Keys MUST rotate every 90 days.",
        ]


class _RecordingJira:
    """A local Jira stand-in that records each request's path and Authorization header."""

    def __init__(self) -> None:
        self.seen: list[dict[str, str | None]] = []
        seen = self.seen
        body = json.dumps(
            {"fields": {"summary": "Rotate keys", "description": "Keys MUST rotate every 90 days."}}
        ).encode()

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append({"path": self.path, "authorization": self.headers.get("Authorization")})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class TestRunReviewSendsCredentialsOnlyToTheBase:
    """``cli._run_review`` → load_config → orchestrate_review → fetch_specs, against a real socket."""

    @pytest.fixture
    def jira(self, monkeypatch):
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        server = _RecordingJira()
        forge = FakeForge(diff=_added_file_diff("src/keys.py", 3))
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: REF)
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: forge)
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: FakeLLM({}))
        monkeypatch.setenv("PRXREF_SPEC_SOURCES", f"http://127.0.0.1:{server.port}/browse/ABC-1")
        monkeypatch.setenv("PRXREF_JIRA_EMAIL", EMAIL)
        monkeypatch.setenv("PRXREF_JIRA_API_TOKEN", TOKEN)
        yield server
        server.close()

    @pytest.mark.usefixtures("contract_stubs")
    def test_no_base_url_means_no_authorization_header(self, jira, caplog):
        caplog.set_level(logging.WARNING, logger="prxref.specs")
        result = cli._run_review(REF.url, post=False)
        assert isinstance(result, dict)
        assert len(jira.seen) == 1
        assert jira.seen[0]["path"].startswith("/rest/api/2/issue/ABC-1?")
        assert jira.seen[0]["authorization"] is None
        assert any("PRXREF_JIRA_BASE_URL" in r.getMessage() for r in _spec_warnings(caplog))
        assert TOKEN not in caplog.text

    @pytest.mark.usefixtures("contract_stubs")
    def test_control_with_base_url_sends_basic_auth(self, jira, monkeypatch):
        monkeypatch.setenv("PRXREF_JIRA_BASE_URL", f"http://127.0.0.1:{jira.port}")
        result = cli._run_review(REF.url, post=False)
        assert isinstance(result, dict)
        assert len(jira.seen) == 1
        assert jira.seen[0]["path"].startswith("/rest/api/2/issue/ABC-1?")
        assert (jira.seen[0]["authorization"] or "").startswith("Basic ")
