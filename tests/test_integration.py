"""End-to-end integration tests for prxref.

Proves the real chain end-to-end without sys.modules stubs:
- Scenario 1: Happy path with 1 chunk, 2 findings (one dropped for confidence).
- Scenario 2: Model fallback across "flash" (500) -> "orch" (200).
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

import pytest
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


@pytest.fixture
def clean_prxref_env(monkeypatch):
    """Ensure PRXREF_* environment variables do not leak into tests."""
    vars_to_clear = [
        "PRXREF_LLM_BACKEND",
        "PRXREF_LLM_BASE_URL",
        "PRXREF_LLM_API_KEY",
        "PRXREF_LLM_MODELS",
        "PRXREF_CONFIDENCE_FLOOR",
        "PRXREF_MAX_ERRORS",
        "PRXREF_MAX_ERROR_FINDINGS",
        "PRXREF_MAX_CHUNKS",
        "PRXREF_GITHUB_TOKEN",
        "PRXREF_BITBUCKET_TOKEN",
        "PRXREF_GITLAB_TOKEN",
        "PRXREF_GITHUB_WEBHOOK_SECRET",
        "PRXREF_BITBUCKET_WEBHOOK_SECRET",
        "PRXREF_GITLAB_WEBHOOK_SECRET",
        "PRXREF_ALLOW_UNSIGNED",
    ]
    for v in vars_to_clear:
        monkeypatch.delenv(v, raising=False)


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

    def test_happy_path_end_to_end(self, clean_prxref_env, monkeypatch):
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
                "flash": {
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "model": "flash",
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
            monkeypatch.setenv("PRXREF_LLM_MODELS", "flash")

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
            assert "model=flash" in summary

            assert len(forge.inline_batches) == 1
            comments = forge.inline_batches[0]
            assert len(comments) == 1
            assert comments[0].path == "src/auth.py"
            assert comments[0].line == 4
            assert "Uncaught token decode exception" in comments[0].body
            assert "Reviewed by prxref · model=flash" in comments[0].body

            assert [r["model"] for r in server.requests] == ["flash"]
        finally:
            server.stop()


class TestScenario2ModelFallback:
    """Scenario 2: Model fallback across two models.

    The LLM server returns HTTP 500 for model "flash" and 200 for "orch".
    The review must succeed with InvokeResult.model == "orch", verified both
    via the summary attribution (model=orch) and the server-side record of
    which model each request carried.
    """

    def test_model_fallback_500_to_200(self, clean_prxref_env, monkeypatch):
        llm_response = {
            "findings": [
                {
                    "file": "src/auth.py",
                    "line": 2,
                    "severity": "note",
                    "confidence": 0.8,
                    "title": "Token validation note",
                    "body": "Token check is standard.",
                }
            ],
            "escalations": [],
        }

        server = MockOpenAIServer(
            routes={
                "flash": (500, {"error": "Internal server error in flash lane"}),
                "orch": {
                    "id": "cmpl-2",
                    "object": "chat.completion",
                    "model": "orch",
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
            monkeypatch.setenv("PRXREF_LLM_MODELS", "flash,orch")

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

            assert [r["model"] for r in server.requests] == ["flash", "orch"]

            assert len(forge.summaries) == 1
            assert "model=orch" in forge.summaries[0]
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

    def test_cli_review_command(self, clean_prxref_env, monkeypatch, capsys):
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
                "flash": {
                    "id": "cmpl-cli",
                    "object": "chat.completion",
                    "model": "flash",
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
            monkeypatch.setenv("PRXREF_LLM_MODELS", "flash")
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
            assert [r["model"] for r in server.requests] == ["flash"]
        finally:
            server.stop()
