"""Tests for prxref.retry_logging: retries that announce themselves.

These drive a real ``requests.Session`` against a real localhost server, rather
than calling ``increment`` by hand. The question worth answering is not whether
the log line formats -- it is whether urllib3, several layers under the
adapter, actually calls *this* class on *every* hop. ``Retry.new`` rebuilds the
policy per hop, so a subclass can log the first retry and silently decay to the
base class for the rest; only a real multi-hop request can tell the two apart.
"""
from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from prxref.retry_logging import LoggingRetry

LOGGER = "prxref.retry_logging"


def _make_server(statuses):
    """Serve ``statuses`` in order, repeating the last one forever."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's contract
            seen.append(self.path)
            i = min(len(seen) - 1, len(statuses) - 1)
            self.send_response(statuses[i])
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", seen


def _session(total=3):
    session = requests.Session()
    retry = LoggingRetry(
        total=total,
        backoff_factor=0,
        status_forcelist=[503],
        allowed_methods=frozenset(["GET"]),
    )
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


class TestRetriesAreVisible:
    def test_every_retry_of_a_real_request_is_logged(self, caplog):
        server, base, seen = _make_server([503, 503, 200])
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                resp = _session().get(f"{base}/pulls/1")
            assert resp.status_code == 200
            assert len(seen) == 3, "the server should have been hit three times"
            lines = [r for r in caplog.records if r.name == LOGGER]
            # Two retries, two lines: the second can only exist if Retry.new
            # rebuilt the policy as a LoggingRetry rather than a plain Retry.
            assert len(lines) == 2
            assert "retrying GET" in lines[0].getMessage()
            assert "HTTP 503" in lines[0].getMessage()
            assert "/pulls/1" in lines[0].getMessage()
        finally:
            server.shutdown()
            server.server_close()

    def test_a_request_that_never_retries_says_nothing(self, caplog):
        """Control: the signal must be able to come out empty."""
        server, base, seen = _make_server([200])
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                assert _session().get(f"{base}/pulls/1").status_code == 200
            assert len(seen) == 1
            assert [r for r in caplog.records if r.name == LOGGER] == []
        finally:
            server.shutdown()
            server.server_close()

    def test_running_out_of_retries_is_logged_not_just_raised(self, caplog):
        server, base, _ = _make_server([503])
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                with pytest.raises(requests.RequestException):
                    _session(total=1).get(f"{base}/pulls/1")
            assert "budget exhausted" in caplog.text
        finally:
            server.shutdown()
            server.server_close()

    def test_a_query_string_is_not_written_to_the_log(self, caplog):
        """A log line is the wrong place to discover a token was in a URL."""
        server, base, _ = _make_server([503, 200])
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                _session().get(f"{base}/pulls/1?private_token=hunter2")
            assert "hunter2" not in caplog.text
            assert "/pulls/1" in caplog.text
        finally:
            server.shutdown()
            server.server_close()


class TestEveryForgeUsesIt:
    """A policy wired into three adapters out of four is the worst outcome:
    the one that stayed quiet is the one you stop suspecting."""

    @pytest.mark.parametrize(
        "module",
        [
            "prxref.forges.github",
            "prxref.forges.gitlab",
            "prxref.forges.bitbucket",
            "prxref.forges.bitbucket_server",
        ],
    )
    def test_the_default_session_retries_out_loud(self, module):
        """Built through the forge, not through its private session helper.

        The default session is reached differently in each adapter (a
        module-level singleton in three of them, a call in the fourth), so the
        only check that covers all four is the object a review actually uses.
        """
        import importlib

        impl = importlib.import_module(module).ForgeImpl()
        session = getattr(impl, "_session", None) or impl.session
        policy = session.get_adapter("https://example.com").max_retries
        assert isinstance(policy, LoggingRetry)

    def test_a_plain_policy_would_fail_that_check(self):
        """Control: isinstance above is not satisfied by any Retry at all."""
        assert not isinstance(Retry(total=3), LoggingRetry)
