"""Spec fetch + digest tests: prxref.specs dispatch, failure doctrine, ranking."""
from __future__ import annotations

import pytest

from prxref.specs import (
    SOURCE_TRUNCATION_MARKER,
    SPEC_DIR_MAX_FILES,
    TRUNCATION_MARKER,
    SpecSource,
    build_spec_digest,
    fetch_specs,
    parse_ticket_url,
)
from prxref.triage import parse_unified_diff


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        content_type: str = "text/plain",
        body: bytes = b"",
        chunks: list[bytes] | None = None,
        encoding: str = "utf-8",
        payload: object | None = None,
    ):
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self._chunks = chunks if chunks is not None else [body]
        self.encoding = encoding
        self._payload = payload if payload is not None else {}

    def iter_content(self, chunk_size: int = 8192, **kwargs):
        return iter(self._chunks)

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, response: _FakeResponse | None = None, exc: Exception | None = None):
        self.response = response
        self.exc = exc
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.response


def _added_file(path: str, lines: list[str]) -> str:
    body = "".join(f"+{text}\n" for text in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}"
    )


class TestParseTicketUrl:
    def test_browse_shape(self):
        ref = parse_ticket_url("https://jira.acme.com/browse/PROJ-123")
        assert ref is not None
        assert ref.base_url == "https://jira.acme.com"
        assert ref.key == "PROJ-123"
        assert ref.url == "https://jira.acme.com/browse/PROJ-123"

    def test_rest_api_v2_and_v3(self):
        for version in ("2", "3"):
            ref = parse_ticket_url(f"https://jira.internal/rest/api/{version}/issue/OPS-4")
            assert ref is not None
            assert ref.base_url == "https://jira.internal"
            assert ref.key == "OPS-4"

    def test_cloud_new_ui(self):
        ref = parse_ticket_url(
            "https://acme.atlassian.net/jira/software/c/projects/ENG/issues/ENG-9?jql=all"
        )
        assert ref is not None
        assert ref.base_url == "https://acme.atlassian.net"
        assert ref.key == "ENG-9"

    def test_non_matches(self):
        assert parse_ticket_url("https://github.com/o/r/pull/1") is None
        assert parse_ticket_url("https://x.com/browse/proj-1") is None
        assert parse_ticket_url("https://x.com/browse/PROJ") is None
        assert parse_ticket_url("/browse/PROJ-1") is None
        assert parse_ticket_url("") is None


class TestFetchSpecsNeverRaises:
    def test_garbage_sources_become_errors(self):
        sources = ["", "   ", "not a path or url", "\x00bad"]
        session = _FakeSession(exc=RuntimeError("no network"))
        results = fetch_specs(sources, max_chars=100, session=session)
        assert len(results) == len(sources)
        for src in results:
            assert src.text == ""
            assert src.error != ""

    def test_session_exception_becomes_error(self):
        session = _FakeSession(exc=ConnectionError("refused"))
        results = fetch_specs(["https://example.com/spec.md"], max_chars=100, session=session)
        assert results[0].error != ""
        assert results[0].text == ""

    def test_output_order_matches_input(self):
        results = fetch_specs(
            ["https://example.com/a.md", "nope"], max_chars=10, session=_FakeSession(
                _FakeResponse(body=b"ok")
            )
        )
        assert [src.origin for src in results] == ["https://example.com/a.md", "nope"]
        assert results[0].text == "ok"
        assert "not a URL or path" in results[1].error


class TestDispatch:
    def test_file(self, tmp_path):
        path = tmp_path / "spec.md"
        path.write_text("The API MUST return 200.", encoding="utf-8")
        (src,) = fetch_specs([str(path)], max_chars=1000)
        assert src.kind == "file"
        assert src.error == ""
        assert src.text == "The API MUST return 200."

    def test_directory(self, tmp_path):
        (tmp_path / "a.md").write_text("alpha MUST hold", encoding="utf-8")
        (tmp_path / "b.txt").write_text("beta SHOULD wait", encoding="utf-8")
        (src,) = fetch_specs([str(tmp_path)], max_chars=1000)
        assert src.kind == "dir"
        assert "alpha MUST hold" in src.text
        assert "beta SHOULD wait" in src.text

    def test_url(self):
        session = _FakeSession(_FakeResponse(content_type="text/markdown", body=b"# Rules\n\nno MUST here"))
        (src,) = fetch_specs(["https://example.com/rules.md"], max_chars=1000, session=session)
        assert src.kind == "url"
        assert src.error == ""
        assert "# Rules" in src.text

    def test_garbage(self):
        (src,) = fetch_specs(["definitely not real"], max_chars=100)
        assert src.kind == ""
        assert src.error.startswith("not a URL or path")


class TestJiraFetch:
    _PAYLOAD = {
        "fields": {
            "summary": "Fix login flow",
            "issuetype": {"name": "Bug"},
            "labels": ["auth", "urgent"],
            "description": "Users MUST re-authenticate after password change.",
        }
    }

    def _jira_source(self) -> str:
        return "https://jira.example.com/browse/AUTH-7"

    def test_authenticated_render(self):
        session = _FakeSession(_FakeResponse(content_type="application/json", payload=self._PAYLOAD))
        (src,) = fetch_specs(
            [self._jira_source()],
            max_chars=1000,
            jira_base_url="https://jira.example.com",
            jira_email="ops@example.com",
            jira_api_token="secret-token",
            session=session,
        )
        assert src.kind == "jira"
        assert src.error == ""
        assert "Summary: Fix login flow" in src.text
        assert "Type: Bug" in src.text
        assert "Labels: auth, urgent" in src.text
        assert "Users MUST re-authenticate" in src.text
        url, kwargs = session.calls[0]
        assert "/rest/api/2/issue/AUTH-7" in url
        assert "fields=summary,description,issuetype,labels" in url
        assert kwargs["auth"] == ("ops@example.com", "secret-token")

    def test_jira_base_url_override(self):
        session = _FakeSession(_FakeResponse(content_type="application/json", payload=self._PAYLOAD))
        fetch_specs(
            [self._jira_source()],
            max_chars=1000,
            jira_base_url="https://rest.internal",
            session=session,
        )
        assert session.calls[0][0].startswith("https://rest.internal/rest/api/2/issue/AUTH-7")

    def test_anonymous_when_no_credentials(self):
        session = _FakeSession(_FakeResponse(content_type="application/json", payload=self._PAYLOAD))
        (src,) = fetch_specs([self._jira_source()], max_chars=1000, session=session)
        assert src.kind == "jira"
        assert src.error == ""
        assert session.calls[0][1]["auth"] is None

    def test_401_without_credentials_names_env_vars(self):
        session = _FakeSession(_FakeResponse(status_code=401, content_type="application/json"))
        (src,) = fetch_specs([self._jira_source()], max_chars=1000, session=session)
        assert src.text == ""
        assert "401" in src.error
        assert "PRXREF_JIRA_EMAIL" in src.error
        assert "PRXREF_JIRA_API_TOKEN" in src.error
        assert "PRXREF_JIRA_BASE_URL" in src.error
        assert "secret" not in src.error.lower()

    def test_403_with_credentials_does_not_advise_env_vars(self):
        session = _FakeSession(_FakeResponse(status_code=403, content_type="application/json"))
        (src,) = fetch_specs(
            [self._jira_source()],
            max_chars=1000,
            jira_base_url="https://jira.example.com",
            jira_email="ops@example.com",
            jira_api_token="secret-token",
            session=session,
        )
        assert session.calls[0][1]["auth"] == ("ops@example.com", "secret-token")
        assert src.error != ""
        assert "403" in src.error
        assert "PRXREF_JIRA_EMAIL" not in src.error


class TestSizeCapAndTruncation:
    def test_file_truncation_marker(self, tmp_path):
        path = tmp_path / "big.md"
        path.write_text("x" * 500, encoding="utf-8")
        (src,) = fetch_specs([str(path)], max_chars=100)
        assert src.text == "x" * 100 + SOURCE_TRUNCATION_MARKER.format(n=100)

    def test_url_stream_truncation(self):
        session = _FakeSession(
            _FakeResponse(content_type="text/plain", chunks=[b"a" * 60, b"b" * 60])
        )
        (src,) = fetch_specs(["https://example.com/stream.md"], max_chars=100, session=session)
        assert src.text.startswith("a" * 60 + "b" * 40)
        assert src.text.endswith(SOURCE_TRUNCATION_MARKER.format(n=100))

    def test_under_cap_no_marker(self, tmp_path):
        path = tmp_path / "small.md"
        path.write_text("tiny", encoding="utf-8")
        (src,) = fetch_specs([str(path)], max_chars=1000)
        assert "truncated" not in src.text


class TestHtmlStripping:
    def test_tags_and_scripts_stripped(self):
        body = (
            b"<html><head><style>.x { color: red }</style>"
            b"<script>var tracking = 1;</script></head>"
            b"<body><h1>API Rules</h1><p>Clients MUST retry.</p></body></html>"
        )
        session = _FakeSession(_FakeResponse(content_type="text/html; charset=utf-8", body=body))
        (src,) = fetch_specs(["https://example.com/spec.html"], max_chars=5000, session=session)
        assert src.error == ""
        assert "API Rules" in src.text
        assert "Clients MUST retry." in src.text
        assert "<p>" not in src.text
        assert "var tracking" not in src.text
        assert "color: red" not in src.text

    def test_non_text_content_type_rejected(self):
        session = _FakeSession(_FakeResponse(content_type="application/octet-stream", body=b"\x00\x01"))
        (src,) = fetch_specs(["https://example.com/blob"], max_chars=100, session=session)
        assert src.text == ""
        assert "not a text content type" in src.error


class TestDirectoryCapAndSort:
    def test_sorted_and_capped_at_20(self, tmp_path):
        for i in range(SPEC_DIR_MAX_FILES + 5):
            (tmp_path / f"f{i:02d}.md").write_text(f"marker-{i:02d} MUST hold", encoding="utf-8")
        (src,) = fetch_specs([str(tmp_path)], max_chars=100_000)
        assert src.kind == "dir"
        assert "marker-00" in src.text
        assert f"marker-{SPEC_DIR_MAX_FILES - 1:02d}" in src.text
        assert f"marker-{SPEC_DIR_MAX_FILES:02d}" not in src.text
        first = src.text.index("marker-00")
        assert src.text.index(f"marker-{SPEC_DIR_MAX_FILES - 1:02d}") > first

    def test_sort_order_not_creation_order(self, tmp_path):
        for name in ("zulu.md", "alpha.md", "mike.md"):
            (tmp_path / name).write_text(f"content-{name}", encoding="utf-8")
        (src,) = fetch_specs([str(tmp_path)], max_chars=10_000)
        assert src.text.index("content-alpha.md") < src.text.index("content-mike.md")
        assert src.text.index("content-mike.md") < src.text.index("content-zulu.md")

    def test_empty_directory_errors(self, tmp_path):
        (src,) = fetch_specs([str(tmp_path)], max_chars=1000)
        assert src.text == ""
        assert src.error != ""


def _digest_sources() -> tuple[list[SpecSource], str]:
    jira = SpecSource(
        origin="https://jira.example.com/browse/SCOPE-1",
        kind="jira",
        text="Summary: Add widget export\nType: Story\nLabels: api\n\nWidgets MUST validate export size.",
        error="",
    )
    spec = SpecSource(
        origin="/docs/widget-spec.md",
        kind="file",
        text=(
            "# Widget rendering\n\n"
            "The widget renderer MUST stream chunks.\n\n"
            "The build cache MUST be purged weekly.\n\n"
            "Docs MAY reference usage examples.\n"
        ),
        error="",
    )
    diff = _added_file(
        "src/widget_renderer.py",
        ["def stream_chunks(self):", "    return chunks"],
    )
    return [jira, spec], diff


class TestBuildSpecDigest:
    def test_determinism(self):
        sources, diff = _digest_sources()
        files = parse_unified_diff(diff)
        first = build_spec_digest(sources, files, token_budget=3000)
        second = build_spec_digest(sources, files, token_budget=3000)
        assert first == second
        assert first != ""

    def test_rank_ticket_then_relevant_then_unmatched_must_then_may(self):
        sources, diff = _digest_sources()
        digest = build_spec_digest(sources, parse_unified_diff(diff), token_budget=3000)
        ticket = digest.index("[ticket:SCOPE-1]")
        relevant = digest.index("widget renderer MUST stream chunks")
        unmatched_must = digest.index("build cache MUST be purged")
        unmatched_may = digest.index("Docs MAY reference")
        assert ticket < relevant < unmatched_must < unmatched_may

    def test_budget_truncation_marker(self):
        sources, diff = _digest_sources()
        digest = build_spec_digest(sources, parse_unified_diff(diff), token_budget=10)
        assert digest.endswith(TRUNCATION_MARKER)
        assert "Widget rendering" not in digest

    def test_empty_source_explanatory_line(self):
        sources, diff = _digest_sources()
        empty = SpecSource(origin="/docs/unrelated.md", kind="file", text="Filler prose without keywords.", error="")
        digest = build_spec_digest([*sources, empty], parse_unified_diff(diff), token_budget=3000)
        assert "[spec:unrelated.md: nothing diff-relevant kept]" in digest
        assert "/docs/" not in digest

    def test_failed_source_explained(self):
        failed = SpecSource(origin="https://example.com/gone.md", kind="url", text="", error="HTTP 404 fetching")
        digest = build_spec_digest([failed], [], token_budget=3000)
        assert digest == ""

    def test_heading_scoping_line_accompanies_kept_constraint(self):
        sources, diff = _digest_sources()
        digest = build_spec_digest(sources, parse_unified_diff(diff), token_budget=3000)
        assert "(heading)" in digest
        assert digest.index("Widget rendering") < digest.index("widget renderer MUST stream chunks")

    def test_version_pin_kept(self):
        src = SpecSource(
            origin="/docs/protocol.md",
            kind="file",
            text="Protocol version 3.2 is REQUIRED for all clients.\n2026-07-28\n",
            error="",
        )
        digest = build_spec_digest([src], [], token_budget=3000)
        assert "Protocol version 3.2 is REQUIRED" in digest
        assert "2026-07-28" in digest

    def test_ticket_description_subbudget(self, monkeypatch):
        import prxref.specs as specs

        monkeypatch.setattr(specs, "TICKET_DESC_BUDGET_CHARS", 40)
        jira = SpecSource(
            origin="https://jira.example.com/browse/BIG-1",
            kind="jira",
            text="Summary: Big ticket\n" + "\n".join(f"Detail line {i} text." for i in range(20)),
            error="",
        )
        digest = build_spec_digest([jira], [], token_budget=3000)
        assert "Summary: Big ticket" in digest
        assert "Detail line 19 text." not in digest

    def test_no_sources_no_files(self):
        digest = build_spec_digest([], [], token_budget=3000)
        assert isinstance(digest, str)
        assert digest != ""


class TestRelevanceScoring:
    def test_diff_token_overlap_ranks_first_among_specs(self):
        widgets = SpecSource(
            origin="/docs/widgets.md",
            kind="file",
            text="The widget renderer MUST stream chunks.\n",
            error="",
        )
        other = SpecSource(
            origin="/docs/billing.md",
            kind="file",
            text="The invoice ledger MUST balance totals.\n",
            error="",
        )
        diff = _added_file("src/widget_renderer.py", ["chunks = stream()"])
        digest = build_spec_digest([widgets, other], parse_unified_diff(diff), token_budget=3000)
        assert digest.index("widget renderer MUST stream") < digest.index("invoice ledger MUST balance")

    def test_unmatched_must_kept_without_overlap(self):
        src = SpecSource(
            origin="/docs/rules.md",
            kind="file",
            text="The deploy pipeline MUST gate on green tests.\n",
            error="",
        )
        diff = _added_file("src/unrelated.py", ["pass"])
        digest = build_spec_digest([src], parse_unified_diff(diff), token_budget=3000)
        assert "deploy pipeline MUST gate on green tests" in digest


if __name__ == "__main__":
    pytest.main([__file__])
