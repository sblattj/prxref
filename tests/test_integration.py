"""End-to-end integration tests for prxref.

Proves the real chain end-to-end without sys.modules stubs:
- Scenario 1: Happy path with 1 chunk, 2 findings (one dropped for confidence).
- Scenario 2: Model fallback across "fast" (500) -> "strong" (200).
- Scenario 3: Real webhook verify_signature with valid HMAC/token and wrong sig.
- Scenario 4: Cross-forge parse_pr_url matrix.
- Scenario 5: CLI end-to-end with real localhost LLM server and monkeypatched forge session.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import requests

from prxref import cli
from prxref.config import load_config
from prxref.forges.base import InlineComment, PRData, PRRef, Thread
from prxref.forges.bitbucket import ForgeImpl as BitbucketForgeImpl
from prxref.forges.github import ForgeImpl as GitHubForgeImpl
from prxref.forges.gitlab import ForgeImpl as GitLabForgeImpl
from prxref.llm_backends import create_llm_client
from prxref.orchestrator import orchestrate_review
from prxref.webhooks import verify_signature


class FakeForge:
    """In-memory Forge implementation for integration testing."""

    name = "fake"

    def __init__(
        self,
        pr: PRData | None = None,
        diff: str = "",
        threads: list[Thread] | None = None,
    ) -> None:
        self.pr = pr or PRData(
            title="Add user auth",
            description="Adds token auth check",
            author="alice",
            source_branch="feature/auth",
            target_branch="main",
            source_sha="1111111111111111111111111111111111111111",
            target_sha="0000000000000000000000000000000000000000",
            raw={},
        )
        self.diff = diff
        self.threads = threads or []
        self.summaries: list[str] = []
        self.inline_batches: list[list[InlineComment]] = []

    @staticmethod
    def parse_pr_url(url: str) -> PRRef | None:
        return None

    def get_pr(self, ref: PRRef) -> PRData:
        return self.pr

    def get_diff(self, ref: PRRef) -> str:
        return self.diff

    def post_summary(self, ref: PRRef, body: str) -> None:
        self.summaries.append(body)

    def post_inline_comments(self, ref: PRRef, comments: list[InlineComment]) -> int:
        self.inline_batches.append(list(comments))
        return len(comments)

    def list_threads(self, ref: PRRef) -> list[Thread]:
        return self.threads


class MockOpenAIServer:
    """Threaded local HTTP server speaking OpenAI /v1/chat/completions."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = routes or {}
        self.requests: list[dict[str, Any]] = []
        self.server: HTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0

    def start(self) -> str:
        routes_ref = self.routes
        requests_ref = self.requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                raw_body = self.rfile.read(length)
                try:
                    payload = json.loads(raw_body)
                except Exception:
                    payload = {}

                model = payload.get("model", "")
                requests_ref.append({
                    "path": self.path,
                    "model": model,
                    "headers": dict(self.headers),
                    "payload": payload,
                })

                route_config = routes_ref.get(model, routes_ref.get("*"))
                if callable(route_config):
                    status, resp_data = route_config(payload)
                elif isinstance(route_config, tuple):
                    status, resp_data = route_config
                elif isinstance(route_config, dict):
                    status = 200
                    resp_data = route_config
                elif isinstance(route_config, int):
                    status = route_config
                    resp_data = {"error": f"error {status}"}
                else:
                    status = 200
                    resp_data = {
                        "id": "chatcmpl-test",
                        "object": "chat.completion",
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps({"findings": [], "escalations": []}),
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                    }

                resp_bytes = json.dumps(resp_data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp_bytes)))
                self.end_headers()
                self.wfile.write(resp_bytes)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=2.0)


def _make_diff(path: str = "src/auth.py") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1,10 @@\n"
        "+def authenticate(token):\n"
        "+    if not token:\n"
        "+        raise ValueError('missing token')\n"
        "+    user = decode(token)\n"
        "+    if user.is_admin:\n"
        "+        return user\n"
        "+    return None\n"
    )


class TestScenario1HappyPath:
    """Scenario 1: Happy path through the real chain.

    Small diff (1 chunk), 2 findings from the worker (one error above the
    confidence floor, one warning below it). Verdict must be
    Request-Changes; the summary carries the verdict banner, per-severity
    counts, and the "Reviewed by prxref" attribution; inline comments are
    posted to the FakeForge with line-snapped anchors; the sub-floor
    finding is retained in findings_dropped with a below-floor drop_reason.

    The LLM is wired through PRXREF_LLM_* env (the documented path:
    load_config() reads env, create_llm_client() honors it). A cfg dict
    produced by load_config() carries lowercase keys, which
    create_llm_client() ignores — reported as a src bug, not patched here.
    """

    def test_happy_path_end_to_end(self, monkeypatch):
        llm_response_findings = {
            "findings": [
                {
                    "file": "src/auth.py",
                    "line": 4,
                    "severity": "error",
                    "confidence": 0.95,
                    "title": "Uncaught token decode exception",
                    "body": "decode(token) can raise JWTError if malformed.",
                },
                {
                    "file": "src/auth.py",
                    "line": 6,
                    "severity": "warning",
                    "confidence": 0.35,
                    "title": "Missing audit log",
                    "body": "Admin login should be audit logged.",
                },
            ],
            "escalations": [],
        }

        server = MockOpenAIServer(
            routes={
                "fast": {
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "model": "fast",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(llm_response_findings),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 200, "completion_tokens": 80},
                }
            }
        )
        base_url = server.start()
        try:
            monkeypatch.setenv("PRXREF_LLM_BACKEND", "openai-compat")
            monkeypatch.setenv("PRXREF_LLM_BASE_URL", base_url)
            monkeypatch.setenv("PRXREF_LLM_API_KEY", "test-key")
            monkeypatch.setenv("PRXREF_LLM_MODELS", "fast")

            cfg = load_config()
            llm_client = create_llm_client(cfg)

            ref = PRRef(
                forge="fake",
                host="fake.test",
                owner="acme",
                repo="auth-service",
                number=42,
                url="https://fake.test/acme/auth-service/pull/42",
            )
            forge = FakeForge(diff=_make_diff("src/auth.py"))

            result = orchestrate_review(
                forge=forge,
                ref=ref,
                llm=llm_client,
                post=True,
                max_chunks=8,
            )

            assert result["verdict"] == "Request-Changes"
            assert len(result["findings_active"]) == 1
            assert result["findings_active"][0].title == "Uncaught token decode exception"
            assert result["findings_active"][0].severity == "error"

            assert len(result["findings_dropped"]) == 1
            dropped = result["findings_dropped"][0]
            assert dropped.title == "Missing audit log"
            assert "below floor" in dropped.drop_reason

            assert len(forge.summaries) == 1
            summary = forge.summaries[0]
            assert "Request-Changes" in summary
            assert "Reviewed by prxref" in summary
            assert "1 error" in summary
            assert "model=fast" in summary

            assert len(forge.inline_batches) == 1
            comments = forge.inline_batches[0]
            assert len(comments) == 1
            assert comments[0].path == "src/auth.py"
            assert comments[0].line == 4
            assert "Uncaught token decode exception" in comments[0].body
            assert "Reviewed by prxref · model=fast" in comments[0].body

            assert [r["model"] for r in server.requests] == ["fast"]
        finally:
            server.stop()


class TestScenario2ModelFallback:
    """Scenario 2: Model fallback across two models.

    The LLM server returns HTTP 500 for model "fast" and 200 for "strong".
    The review must succeed with InvokeResult.model == "strong", verified both
    via the summary attribution (model=strong) and the server-side record of
    which model each request carried.
    """

    def test_model_fallback_500_to_200(self, monkeypatch):
        llm_response = {
            "findings": [
                {
                    "file": "src/auth.py",
                    "line": 2,
                    "severity": "outofscope",
                    "confidence": 0.8,
                    "title": "Token validation note",
                    "body": "Token check is standard.",
                }
            ],
            "escalations": [],
        }

        server = MockOpenAIServer(
            routes={
                "fast": (500, {"error": "Internal server error in the fast lane"}),
                "strong": {
                    "id": "cmpl-2",
                    "object": "chat.completion",
                    "model": "strong",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(llm_response),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 150, "completion_tokens": 40},
                },
            }
        )
        base_url = server.start()
        try:
            monkeypatch.setenv("PRXREF_LLM_BACKEND", "openai-compat")
            monkeypatch.setenv("PRXREF_LLM_BASE_URL", base_url)
            monkeypatch.setenv("PRXREF_LLM_API_KEY", "test-key")
            monkeypatch.setenv("PRXREF_LLM_MODELS", "fast,strong")

            cfg = load_config()
            llm_client = create_llm_client(cfg)

            ref = PRRef(
                forge="fake",
                host="fake.test",
                owner="acme",
                repo="auth-service",
                number=99,
                url="https://fake.test/acme/auth-service/pull/99",
            )
            forge = FakeForge(diff=_make_diff("src/auth.py"))

            result = orchestrate_review(
                forge=forge,
                ref=ref,
                llm=llm_client,
                post=True,
            )

            assert result["verdict"] == "Approved"
            assert len(result["findings_active"]) == 1

            assert [r["model"] for r in server.requests] == ["fast", "strong"]

            assert len(forge.summaries) == 1
            assert "model=strong" in forge.summaries[0]
            assert "Reviewed by prxref" in forge.summaries[0]
        finally:
            server.stop()


class TestScenario3WebhookSignatures:
    """Scenario 3: Webhook verify_signature against all 3 forges."""

    def test_github_webhook_signature(self, monkeypatch):
        gh_secret = "gh-top-secret"
        monkeypatch.setenv("PRXREF_GITHUB_WEBHOOK_SECRET", gh_secret)

        pr_url = "https://github.com/my-org/my-repo/pull/123"
        payload = json.dumps({"action": "opened", "pull_request": {"html_url": pr_url}}).encode()
        good_sig = "sha256=" + hmac.new(gh_secret.encode(), payload, hashlib.sha256).hexdigest()
        bad_sig = "sha256=" + "0" * 64

        ok, url_or_reason = verify_signature(
            payload,
            {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": good_sig},
        )
        assert ok is True
        assert url_or_reason == pr_url

        ok, reason = verify_signature(
            payload,
            {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": bad_sig},
        )
        assert ok is False
        assert "signature mismatch" in reason

    def test_bitbucket_webhook_signature(self, monkeypatch):
        bb_secret = "bb-top-secret"
        monkeypatch.setenv("PRXREF_BITBUCKET_WEBHOOK_SECRET", bb_secret)

        pr_url = "https://bitbucket.org/my-org/my-repo/pull-requests/456"
        payload = json.dumps({"pullrequest": {"links": {"html": {"href": pr_url}}}}).encode()
        good_sig = "sha256=" + hmac.new(bb_secret.encode(), payload, hashlib.sha256).hexdigest()
        bad_sig = "sha256=" + "f" * 64

        ok, url_or_reason = verify_signature(
            payload,
            {"X-Event-Key": "pr:opened", "X-Hub-Signature": good_sig},
        )
        assert ok is True
        assert url_or_reason == pr_url

        ok, reason = verify_signature(
            payload,
            {"X-Event-Key": "pr:opened", "X-Hub-Signature": bad_sig},
        )
        assert ok is False
        assert "signature mismatch" in reason

    def test_gitlab_webhook_signature(self, monkeypatch):
        gl_secret = "gl-top-token"
        monkeypatch.setenv("PRXREF_GITLAB_WEBHOOK_SECRET", gl_secret)

        mr_url = "https://gitlab.com/my-org/my-repo/-/merge_requests/789"
        payload = json.dumps({"object_attributes": {"url": mr_url, "action": "open"}}).encode()

        ok, url_or_reason = verify_signature(
            payload,
            {"X-Gitlab-Event": "MergeRequestHook", "X-Gitlab-Token": gl_secret},
        )
        assert ok is True
        assert url_or_reason == mr_url

        ok, reason = verify_signature(
            payload,
            {"X-Gitlab-Event": "MergeRequestHook", "X-Gitlab-Token": "wrong-token"},
        )
        assert ok is False
        assert "token mismatch" in reason


class TestScenario4ForgeUrlDetection:
    """Scenario 4: Cross-forge matrix for parse_pr_url."""

    def test_parse_pr_url_matrix(self):
        gh_url = "https://github.com/octocat/Hello-World/pull/42"
        bb_url = "https://bitbucket.org/workspace-slug/repo-slug/pull-requests/101"
        gl_url = "https://gitlab.com/group/subgroup/project/-/merge_requests/7"
        invalid_url = "https://example.com/other/project/1"

        gh_ref = GitHubForgeImpl.parse_pr_url(gh_url)
        assert gh_ref is not None
        assert gh_ref.forge == "github"
        assert gh_ref.owner == "octocat"
        assert gh_ref.repo == "Hello-World"
        assert gh_ref.number == 42
        assert GitHubForgeImpl.parse_pr_url(bb_url) is None
        assert GitHubForgeImpl.parse_pr_url(gl_url) is None
        assert GitHubForgeImpl.parse_pr_url(invalid_url) is None

        bb_ref = BitbucketForgeImpl.parse_pr_url(bb_url)
        assert bb_ref is not None
        assert bb_ref.forge == "bitbucket"
        assert bb_ref.owner == "workspace-slug"
        assert bb_ref.repo == "repo-slug"
        assert bb_ref.number == 101
        assert BitbucketForgeImpl.parse_pr_url(gh_url) is None
        assert BitbucketForgeImpl.parse_pr_url(gl_url) is None
        assert BitbucketForgeImpl.parse_pr_url(invalid_url) is None

        gl_ref = GitLabForgeImpl.parse_pr_url(gl_url)
        assert gl_ref is not None
        assert gl_ref.forge == "gitlab"
        assert gl_ref.owner == "group"
        assert gl_ref.repo == "project"
        assert gl_ref.number == 7
        assert GitLabForgeImpl.parse_pr_url(gh_url) is None
        assert GitLabForgeImpl.parse_pr_url(bb_url) is None
        assert GitLabForgeImpl.parse_pr_url(invalid_url) is None


class TestScenario5CliEndToEnd:
    """Scenario 5: CLI end-to-end over a real github.com PR URL.

    cli.main(["review", ...]) → detect_forge (real) → make_forge (real
    GitHub ForgeImpl) → create_llm_client (real, against the local mock LLM
    server over a real localhost socket) → orchestrate_review → reviewer →
    quality passes. Only the forge layer's requests.Session.get is
    monkeypatched (no network); Session.post stays real for the LLM client.
    """

    def test_cli_review_command(self, monkeypatch, capsys):
        llm_response = {
            "findings": [
                {
                    "file": "src/cli.py",
                    "line": 5,
                    "severity": "error",
                    "confidence": 0.9,
                    "title": "Missing argument check",
                    "body": "Argument parser should check for empty string.",
                }
            ],
            "escalations": [],
        }

        server = MockOpenAIServer(
            routes={
                "fast": {
                    "id": "cmpl-cli",
                    "object": "chat.completion",
                    "model": "fast",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(llm_response),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 45},
                }
            }
        )
        base_url = server.start()
        try:
            monkeypatch.setenv("PRXREF_LLM_BASE_URL", base_url)
            monkeypatch.setenv("PRXREF_LLM_API_KEY", "local")
            monkeypatch.setenv("PRXREF_LLM_MODELS", "fast")
            monkeypatch.setenv("PRXREF_GITHUB_TOKEN", "fake-gh-token")

            diff_text = _make_diff("src/cli.py")

            class MockForgeResponse:
                def __init__(self, status_code: int, json_data: Any = None, text: str = ""):
                    self.status_code = status_code
                    self._json = json_data
                    self.text = text
                    self.ok = 200 <= status_code < 300

                def json(self):
                    return self._json

                def raise_for_status(self):
                    if not self.ok:
                        raise requests.HTTPError(f"HTTP {self.status_code}")

            def mock_get(self, url, *args, **kwargs):
                headers = kwargs.get("headers", {})
                accept = headers.get("Accept", "")
                if "diff" in accept:
                    return MockForgeResponse(200, text=diff_text)
                if url.endswith("/pulls/15"):
                    return MockForgeResponse(200, json_data={
                        "title": "Refactor CLI entrypoint",
                        "body": "Clean up CLI arguments",
                        "user": {"login": "dev1"},
                        "head": {"ref": "feat/cli", "sha": "1111111111111111111111111111111111111111"},
                        "base": {"ref": "main", "sha": "0000000000000000000000000000000000000000"},
                    })
                return MockForgeResponse(200, json_data=[])

            monkeypatch.setattr(requests.Session, "get", mock_get)

            rc = cli.main([
                "review",
                "--pr-url",
                "https://github.com/acme/prxref-test/pull/15",
                "--no-post",
                "-v",
            ])

            assert rc == 0
            captured = capsys.readouterr()
            assert "verdict: Request-Changes" in captured.out
            assert [r["model"] for r in server.requests] == ["fast"]
        finally:
            server.stop()


class TestLLMBudgetKnobsEndToEnd:
    """Scenario: PRXREF_LLM_MAX_TOKENS / _TEMPERATURE from env to the wire.

    Runs the documented CLI path (``cli._run_review`` -> ``load_config`` ->
    ``create_llm_client`` -> ``orchestrate_review`` -> ``reviewer``) against the
    mock OpenAI server, then reads the request the server actually received.
    Only the forge is faked; every configuration hop is real.
    """

    PR_URL = "https://github.com/acme/prxref-test/pull/15"

    def _run(self, monkeypatch, server_url, env: dict[str, str]) -> None:
        ref = PRRef(
            forge="github",
            host="github.com",
            owner="acme",
            repo="prxref-test",
            number=15,
            url=self.PR_URL,
        )
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "openai-compat")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", server_url)
        monkeypatch.setenv("PRXREF_LLM_MODELS", "fast")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(cli, "detect_forge", lambda url: ref)
        monkeypatch.setattr(
            cli, "make_forge", lambda r, session=None: FakeForge(diff=_make_diff("src/auth.py"))
        )
        cli._run_review(self.PR_URL, post=False)

    def test_defaults_send_budget_temperature_0_and_no_seed(
        self, monkeypatch
    ):
        """The reproducibility contract, end to end: with nothing configured,
        temperature 0.0 IS on the wire (that is the fix for the vanished
        error-severity finding) and no seed is sent."""
        server = MockOpenAIServer()
        base_url = server.start()
        try:
            self._run(monkeypatch, base_url, {})
            assert len(server.requests) == 1
            payload = server.requests[0]["payload"]
            assert payload["max_tokens"] == 4096
            assert payload["temperature"] == 0.0
            assert "seed" not in payload
        finally:
            server.stop()

    def test_malformed_temperature_exits_2_through_the_real_client(
        self, monkeypatch, capsys
    ):
        """Temperature is parsed inside create_llm_client, so only the real
        backend proves the exit-2 contract for it."""
        ref = PRRef(
            forge="github",
            host="github.com",
            owner="acme",
            repo="prxref-test",
            number=15,
            url=self.PR_URL,
        )
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.invalid/v1")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "fast")
        monkeypatch.setenv("PRXREF_LLM_TEMPERATURE", "hot")
        monkeypatch.setattr(cli, "detect_forge", lambda url: ref)
        monkeypatch.setattr(
            cli, "make_forge", lambda r, session=None: FakeForge(diff=_make_diff())
        )

        rc = cli.main(["review", "--pr-url", self.PR_URL, "--no-post"])

        assert rc == 2
        _, err = capsys.readouterr()
        assert "configuration error" in err
        assert "PRXREF_LLM_TEMPERATURE" in err

    def test_configured_budget_and_temperature_reach_the_request(
        self, monkeypatch
    ):
        server = MockOpenAIServer()
        base_url = server.start()
        try:
            self._run(
                monkeypatch,
                base_url,
                {"PRXREF_LLM_MAX_TOKENS": "8192", "PRXREF_LLM_TEMPERATURE": "0.3"},
            )
            payload = server.requests[0]["payload"]
            assert payload["max_tokens"] == 8192
            assert payload["temperature"] == 0.3
        finally:
            server.stop()

    def test_configured_seed_reaches_the_request(self, monkeypatch):
        server = MockOpenAIServer()
        base_url = server.start()
        try:
            self._run(monkeypatch, base_url, {"PRXREF_LLM_SEED": "1234"})
            payload = server.requests[0]["payload"]
            assert payload["seed"] == 1234
            assert isinstance(payload["seed"], int)
        finally:
            server.stop()


def _completion(content: str, finish_reason: str, model: str = "fast") -> dict:
    """One OpenAI-shaped completion body with an explicit stop reason."""
    return {
        "id": "cmpl-1",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 200, "completion_tokens": 80},
    }


_TRUNCATED_CONTENT = '{"findings": [{"file": "src/auth.py", "line": 4, "sev'

_GOOD_CONTENT = json.dumps({
    "findings": [
        {
            "file": "src/auth.py",
            "line": 4,
            "severity": "error",
            "confidence": 0.95,
            "title": "Uncaught token decode exception",
            "body": "decode(token) can raise JWTError if malformed.",
        }
    ],
    "escalations": [],
})


class _CLIHarness:
    """Runs the real CLI review path against a mock LLM, keeping the forge."""

    PR_URL = "https://github.com/acme/prxref-test/pull/15"
    REF = PRRef(
        forge="github", host="github.com", owner="acme",
        repo="prxref-test", number=15,
        url="https://github.com/acme/prxref-test/pull/15",
    )

    def __init__(self, monkeypatch, server_url: str, env: dict[str, str]):
        self.forge = FakeForge(diff=_make_diff("src/auth.py"))
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "openai-compat")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", server_url)
        monkeypatch.setenv("PRXREF_LLM_MODELS", "fast")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(cli, "detect_forge", lambda url: self.REF)
        monkeypatch.setattr(cli, "make_forge", lambda r, session=None: self.forge)

    def review(self, *, post: bool = True):
        return cli._run_review(self.PR_URL, post=post)


class TestTruncationIsLegibleEndToEnd:
    """Scenario: a starved PRXREF_LLM_MAX_TOKENS explains itself.

    Every hop is real — HTTP client, reviewer, orchestrator, summary rendering —
    and only the forge is faked. The operator-visible artifact is the posted
    notice, so that is what is asserted: it has to name the budget and the
    variable rather than the ``JSONDecodeError`` the truncation surfaces as.
    """

    def test_the_posted_notice_names_the_budget_and_the_lever(self, monkeypatch):
        server = MockOpenAIServer(
            routes={"fast": _completion(_TRUNCATED_CONTENT, "length")}
        )
        base_url = server.start()
        try:
            harness = _CLIHarness(
                monkeypatch, base_url, {"PRXREF_LLM_MAX_TOKENS": "256"}
            )
            result = harness.review(post=True)

            assert server.requests[0]["payload"]["max_tokens"] == 256
            assert result["verdict"] == "Error"
            assert len(harness.forge.summaries) == 1
            notice = harness.forge.summaries[0]
            assert "response truncated at max_tokens=256" in notice
            assert "finish_reason=length" in notice
            assert "PRXREF_LLM_MAX_TOKENS" in notice
        finally:
            server.stop()

    def test_the_same_unparseable_body_without_the_stop_reason_is_not_blamed_on_the_budget(
        self, monkeypatch
    ):
        """The discriminating control: identical bytes, honest stop reason.

        Blaming the budget for every unparseable response would send operators
        to raise a limit that was never the problem.
        """
        server = MockOpenAIServer(
            routes={"fast": _completion(_TRUNCATED_CONTENT, "stop")}
        )
        base_url = server.start()
        try:
            harness = _CLIHarness(
                monkeypatch, base_url, {"PRXREF_LLM_MAX_TOKENS": "256"}
            )
            result = harness.review(post=True)

            assert result["verdict"] == "Error"
            notice = harness.forge.summaries[0]
            assert "PRXREF_LLM_MAX_TOKENS" not in notice
            assert "JSONDecodeError" in notice
        finally:
            server.stop()


class TestDryRunEndToEnd:
    """Scenario: PRXREF_DRY_RUN reviews for real and writes nothing.

    Asserted at the forge, which is the only place a write could show up.
    """

    def test_nothing_is_posted_when_dry_run_is_set(self, monkeypatch):
        server = MockOpenAIServer(routes={"fast": _completion(_GOOD_CONTENT, "stop")})
        base_url = server.start()
        try:
            harness = _CLIHarness(monkeypatch, base_url, {"PRXREF_DRY_RUN": "1"})
            result = harness.review(post=True)

            assert result["posted"] is False
            assert harness.forge.summaries == []
            assert harness.forge.inline_batches == []
            # The review itself still ran: the model was called and the finding
            # survived the quality gate.
            assert len(server.requests) == 1
            assert len(result["findings_active"]) == 1
        finally:
            server.stop()

    def test_the_same_run_posts_when_dry_run_is_unset(self, monkeypatch):
        """Control: proves the assertions above are about the variable."""
        server = MockOpenAIServer(routes={"fast": _completion(_GOOD_CONTENT, "stop")})
        base_url = server.start()
        try:
            harness = _CLIHarness(monkeypatch, base_url, {})
            result = harness.review(post=True)

            assert result["posted"] is True
            assert len(harness.forge.summaries) == 1
            assert len(harness.forge.inline_batches) == 1
        finally:
            server.stop()


class TestPartialFailureIsExplainedOnThePR:
    """Scenario: 2 chunks, 1 truncated, through the whole real chain.

    A partial review is the dangerous case — it looks like a working review
    with a footnote, so a reason that only reaches the daemon's stderr reaches
    nobody. Asserted on the posted comment, which is the artifact the person
    who can act on it actually reads.
    """

    def _two_chunk_diff(self) -> str:
        def big(path: str) -> str:
            body = "\n".join(f"+line {i}" for i in range(1, 401))
            return (
                f"diff --git a/{path} b/{path}\n"
                "new file mode 100644\n"
                f"--- /dev/null\n+++ b/{path}\n"
                f"@@ -0,0 +1,400 @@\n{body}\n"
            )

        return big("src/one.py") + big("src/two.py")

    def _run(self, monkeypatch, second_finish_reason: str):
        """First chunk answers cleanly, second stops with the given reason."""
        state = {"n": 0}

        def route(payload):
            state["n"] += 1
            if state["n"] == 1:
                return 200, _completion(_GOOD_CONTENT, "stop")
            return 200, _completion(_TRUNCATED_CONTENT, second_finish_reason)

        server = MockOpenAIServer(routes={"fast": route})
        base_url = server.start()
        try:
            monkeypatch.setenv("PRXREF_LLM_BACKEND", "openai-compat")
            monkeypatch.setenv("PRXREF_LLM_BASE_URL", base_url)
            monkeypatch.setenv("PRXREF_LLM_MODELS", "fast")
            monkeypatch.setenv("PRXREF_LLM_MAX_TOKENS", "256")
            cfg = load_config()
            forge = FakeForge(diff=self._two_chunk_diff())
            ref = PRRef(
                forge="fake", host="fake.test", owner="acme",
                repo="auth-service", number=42,
                url="https://fake.test/acme/auth-service/pull/42",
            )
            result = orchestrate_review(
                forge=forge, ref=ref, llm=create_llm_client(cfg), post=True,
                max_chunks=2, token_budget=1000, max_workers=1,
                max_tokens=cfg["llm_max_tokens"],
            )
            return forge, result, server
        finally:
            server.stop()

    def test_the_truncation_reason_lands_on_the_pr(self, monkeypatch):
        forge, result, server = self._run(monkeypatch, "length")

        assert len(server.requests) == 2
        assert result["chunks_reviewed"] == 1
        assert result["chunks_failed"] == 1
        summary = forge.summaries[0]
        assert "⚠️ Partial review: 1 of 2 chunks were reviewed" in summary
        assert (
            "> - response truncated at max_tokens=256 (finish_reason=length); "
            "raise PRXREF_LLM_MAX_TOKENS" in summary
        )

    def test_a_non_budget_failure_reports_its_own_reason(self, monkeypatch):
        """Control: identical response bytes, honest stop reason. The banner
        still explains itself, but must not blame the budget."""
        forge, result, _server = self._run(monkeypatch, "stop")

        assert result["chunks_failed"] == 1
        summary = forge.summaries[0]
        assert "⚠️ Partial review: 1 of 2 chunks were reviewed" in summary
        assert "PRXREF_LLM_MAX_TOKENS" not in summary
        assert "JSONDecodeError" in summary

    def test_the_surviving_chunk_still_reports_its_findings(self, monkeypatch):
        """The banner is an addition, not a replacement: a partial review is
        still a review."""
        forge, result, _server = self._run(monkeypatch, "length")

        assert result["verdict"] == "Request-Changes"
        assert len(result["findings_active"]) == 1
        assert "Uncaught token decode exception" in forge.summaries[0]
