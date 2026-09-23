"""Spec digest contract: the constraint count, the empty digest, no origin leak, section scoping.

Covers ``specs.constraint_count`` (COR-4/INT-5/LIVE-5: ticket lines count, the
strength label binds to spec lines only), ``build_spec_digest`` returning
``""`` when nothing was extracted (LIVE-4), the short origin in every digest
line (SEC-4), heading re-emission and HTML headings (COR-3), and the grounding
note's ``source N (kind)`` failure labels (SEC-5).

The end-to-end cases run the REAL reviewer (this module does not request
``contract_stubs``), so what they assert is what the LLM is actually sent.
"""
from __future__ import annotations

import re

import pytest

from prxref import orchestrator
from prxref.llm import InvokeResult
from prxref.orchestrator import _spec_note, orchestrate_review
from prxref.reviewer import _NO_SPECS_TEXT as NO_SPECS
from prxref.specs import (
    SOURCE_TRUNCATION_MARKER,
    TRUNCATION_MARKER,
    SpecSource,
    _origin_short,
    _strip_html,
    build_spec_digest,
    constraint_count,
    fetch_specs,
)
from prxref.triage import parse_unified_diff
from tests.test_orchestrator import REF, FakeForge, _added_file_diff
from tests.test_specs import _added_file, _FakeResponse, _FakeSession

AUTH_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,1 +1,2 @@
 x = 1
+token = request.headers["X-Auth"]
"""


def _jira() -> SpecSource:
    return SpecSource(
        origin="https://acme.example.invalid/browse/PROJ-9",
        kind="jira",
        text=(
            "Summary: auth header\nType: Bug\nLabels: auth\n"
            "Users MUST re-authenticate after password change."
        ),
        error="",
    )


def _doc() -> SpecSource:
    return SpecSource(
        origin="/tmp/spec.md",
        kind="file",
        text="# Auth\nThe client MUST send the X-Auth header.\nServers MAY cache tokens.\n",
        error="",
    )


def _failed(origin: str = "/nonexistent/spec.md", kind: str = "file") -> SpecSource:
    return SpecSource(origin=origin, kind=kind, text="", error="not a URL or path")


def _keywordless(origin: str = "/docs/prose.md") -> SpecSource:
    return SpecSource(origin=origin, kind="file", text="Filler prose without keywords.", error="")


def _digest(sources: list[SpecSource], budget: int = 3000) -> str:
    return build_spec_digest(sources, parse_unified_diff(AUTH_DIFF), budget)


class TestConstraintCount:
    """Exact counts, never ``> 0``: the count is the grounding predicate."""

    def test_jira_only_counts_every_ticket_line(self):
        digest = _digest([_jira()])
        assert constraint_count(digest) == 4

    def test_file_only_counts_its_units(self):
        digest = _digest([_doc()])
        assert constraint_count(digest) == 2

    def test_jira_plus_file_counts_both(self):
        digest = _digest([_jira(), _doc()])
        assert constraint_count(digest) == 6

    def test_scoping_and_bookkeeping_lines_never_count(self):
        digest = "\n".join(
            [
                "Spec constraints ranked for this diff: ticket scope first, then "
                "constraints sharing tokens with the diff, then unmatched MUST-level rules.",
                "[spec:spec.md#auth] (heading) Auth",
                "[spec:spec.md] (heading) (no section)",
                "[spec:spec.md#L2] (MUST) The client MUST send the X-Auth header.",
                "[ticket:PROJ-9] Summary: auth header",
                TRUNCATION_MARKER,
                SOURCE_TRUNCATION_MARKER.format(n=100),
                "[spec:unrelated.md: nothing diff-relevant kept]",
                '[ticket:PROJ-9: shown in full under "Ticket context" above]',
                "",
            ]
        )
        assert constraint_count(digest) == 2

    def test_the_strength_label_is_one_of_three(self):
        assert constraint_count("[spec:a.md#L1] (MUST) x") == 1
        assert constraint_count("[spec:a.md#L1] (SHOULD) x") == 1
        assert constraint_count("[spec:a.md#L1] (MAY) x") == 1
        assert constraint_count("[spec:a.md#L1] (heading) x") == 0
        assert constraint_count("[spec:a.md#L1] (must) x") == 0
        assert constraint_count("[spec:a.md] (MUST) x") == 0

    def test_a_ticket_line_counts_once_labelled_or_not(self):
        assert constraint_count("[ticket:K-1] plain statement") == 1
        assert constraint_count("[ticket:K-1] (MUST) labelled statement") == 1

    def test_only_line_starts_count(self):
        assert constraint_count("prose [ticket:K-1] inline mention") == 0
        assert constraint_count("a\n[ticket:K-1] b\n[spec:x#L1] (MAY) c\n") == 2

    def test_empty_digest_counts_zero(self):
        assert constraint_count("") == 0

    def test_the_no_section_line_leaves_the_count_at_the_unit_count(self):
        pre = (
            "Implementations MUST use UTF-8 encoding.\n\n## Client requirements\n\n"
            "Clients MUST send the session header.\n"
        )
        src = SpecSource(origin="/docs/pre.md", kind="file", text=pre, error="")
        diff = _added_file("src/c.py", ["session_header = 1"])
        digest = build_spec_digest([src], parse_unified_diff(diff), token_budget=3000)
        assert "(heading) (no section)" in digest
        assert constraint_count(digest) == 2


class TestEmptyDigest:
    """``""`` when sources were given and no unit came out of any of them."""

    def test_every_source_failed_is_empty(self):
        assert _digest([_failed(), _failed("not-a-url", kind="")]) == ""

    def test_only_keywordless_sources_is_empty(self):
        assert _digest([_keywordless()]) == ""

    def test_failed_plus_keywordless_is_empty(self):
        assert _digest([_failed(), _keywordless()]) == ""

    def test_no_sources_keeps_the_intro(self):
        digest = _digest([])
        assert digest.startswith("Spec constraints ranked for this diff")
        assert constraint_count(digest) == 0

    def test_a_budget_too_small_for_any_unit_is_not_empty(self):
        digest = _digest([_doc()], budget=10)
        assert digest.endswith(TRUNCATION_MARKER)
        assert constraint_count(digest) == 0

    def test_partial_failure_drops_the_failed_origin(self):
        digest = _digest([_doc(), _failed()])
        assert constraint_count(digest) == 2
        assert "nonexistent" not in digest
        assert "nothing diff-relevant kept" not in digest

    def test_a_keywordless_sibling_is_explained_by_short_name(self):
        digest = _digest([_doc(), _keywordless("/home/someone/docs/prose.md")])
        assert "[spec:prose.md: nothing diff-relevant kept]" in digest
        assert "/home/someone" not in digest


class TestOriginShort:
    @pytest.mark.parametrize(
        ("origin", "short"),
        [
            ("https://user:tok@wiki.example.com/", "wiki.example.com"),
            ("https://user:tok@wiki.example.com", "wiki.example.com"),
            ("https://wiki.example.com:8443/", "wiki.example.com"),
            ("https://git.example.com/raw/spec.md?private_token=abc#frag", "spec.md"),
            ("https://git.example.com/raw/specs/", "specs"),
            ("/abs/path/spec.md", "spec.md"),
            ("docs/specs/", "specs"),
            ("spec.md", "spec.md"),
        ],
    )
    def test_short_names(self, origin, short):
        assert _origin_short(origin) == short


SECRETS = ("SECRETPT0123", "SECRETSIG0123", "SECRETTOK0123", "user:")


def _leaky_sources() -> list[SpecSource]:
    return [
        SpecSource(
            origin="https://git.example.com/raw/spec.md?private_token=SECRETPT0123",
            kind="url", text="", error="HTTP 404",
        ),
        SpecSource(
            origin="https://files.example.com/doc.md?sig=SECRETSIG0123",
            kind="url", text="Filler prose without keywords.", error="",
        ),
        SpecSource(
            origin="https://user:SECRETTOK0123@wiki.example.com/",
            kind="url", text="Every handler MUST validate the data payload.\n", error="",
        ),
    ]


class _RecordingLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        self.calls.append((system, user))
        return InvokeResult(
            text='{"findings": []}', input_tokens=1, output_tokens=1,
            model="test-model-1", backend="fake", elapsed_ms=1,
        )


def _run(monkeypatch, fetched: list[SpecSource], *, post: bool = False):
    monkeypatch.setattr(orchestrator.specs, "fetch_specs", lambda *a, **k: fetched)
    forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
    llm = _RecordingLLM()
    orchestrate_review(forge, REF, llm, post=post, spec_sources=["configured"])
    assert len(llm.calls) == 2, "one chunk plus the sweep"
    return forge, llm


class TestNoOriginLeak:
    def test_the_digest_carries_no_query_userinfo_or_failed_origin(self):
        digest = build_spec_digest(_leaky_sources(), [], token_budget=3000)
        assert "Every handler MUST validate the data payload." in digest
        assert "[spec:doc.md: nothing diff-relevant kept]" in digest
        for secret in SECRETS:
            assert secret not in digest
        assert "git.example.com" not in digest

    def test_no_prompt_the_llm_is_sent_carries_them(self, monkeypatch):
        _forge, llm = _run(monkeypatch, _leaky_sources())
        for system, user in llm.calls:
            assert "Every handler MUST validate the data payload." in user
            for secret in SECRETS:
                assert secret not in system
                assert secret not in user


class TestPromptPlaceholder:
    def test_every_source_failed_leaves_every_prompt_on_the_no_specs_text(self, monkeypatch):
        _forge, llm = _run(monkeypatch, [_failed(), _failed("https://gone.example.com/x.md", "url")])
        for system, user in llm.calls:
            assert NO_SPECS in user
            assert "nonexistent" not in system + user
            assert "gone.example.com" not in system + user

    def test_a_grounded_run_replaces_the_no_specs_text(self, monkeypatch):
        _forge, llm = _run(monkeypatch, [_doc()])
        for _system, user in llm.calls:
            assert NO_SPECS not in user
            assert "(MUST) The client MUST send the X-Auth header." in user


_HEADING = re.compile(r"\(heading\) (.*)$")


def _scope_of(digest: str, needle: str) -> str | None:
    scope = None
    for line in digest.splitlines():
        m = _HEADING.search(line)
        if m:
            scope = m.group(1)
        elif needle in line:
            return scope
    raise AssertionError(f"{needle!r} not in digest")


def _html_source(body: bytes) -> SpecSource:
    session = _FakeSession(_FakeResponse(content_type="text/html; charset=utf-8", body=body))
    (src,) = fetch_specs(["https://example.com/spec"], max_chars=50_000, session=session)
    assert src.error == ""
    return src


SESSION_SPEC = (
    "## Client requirements\n\nClients MUST send the session header on every request.\n\n"
    "Clients MUST cache the protocol handshake.\n\n## Server requirements\n\n"
    "Servers MUST reject unknown session ids.\n"
)
SESSION_DIFF = _added_file("src/server.py", ["def check(header, session):"])


class TestSectionScoping:
    def test_ranked_interleaving_keeps_each_constraint_under_its_own_heading(self):
        src = SpecSource(origin="/docs/spec.md", kind="file", text=SESSION_SPEC, error="")
        digest = build_spec_digest([src], parse_unified_diff(SESSION_DIFF), token_budget=3000)
        assert _scope_of(digest, "Clients MUST send the session header") == "Client requirements"
        assert _scope_of(digest, "Servers MUST reject unknown session ids") == "Server requirements"
        assert _scope_of(digest, "Clients MUST cache the protocol handshake") == "Client requirements"
        assert digest.count("(heading) Client requirements") == 2

    def test_a_same_section_run_emits_its_heading_once(self):
        src = SpecSource(origin="/docs/spec.md", kind="file", text=SESSION_SPEC, error="")
        digest = build_spec_digest([src], [], token_budget=3000)
        assert digest.count("(heading) Client requirements") == 1
        assert digest.count("(heading) Server requirements") == 1

    def test_headingless_preamble_unit_is_not_scoped_under_a_later_heading(self):
        pre = (
            "Implementations MUST use UTF-8 encoding.\n\n## Client requirements\n\n"
            "Clients MUST send the session header.\n"
        )
        src = SpecSource(origin="/docs/pre.md", kind="file", text=pre, error="")
        diff = _added_file("src/c.py", ["session_header = 1"])
        digest = build_spec_digest([src], parse_unified_diff(diff), token_budget=3000)
        assert _scope_of(digest, "Implementations MUST use UTF-8 encoding") == "(no section)"
        assert "[spec:pre.md] (heading) (no section)" in digest

    def test_a_headingless_second_source_is_not_scoped_under_the_first(self):
        headed = SpecSource(
            origin="/docs/a.md", kind="file",
            text="## Alpha rules\n\nAlpha widgets MUST stream.\n", error="",
        )
        bare = SpecSource(
            origin="/docs/b.md", kind="file", text="Beta gadgets MUST batch.\n", error="",
        )
        digest = build_spec_digest([headed, bare], [], token_budget=3000)
        assert _scope_of(digest, "Beta gadgets MUST batch") == "(no section)"

    def test_html_headings_become_scoping_lines(self):
        html = (
            b"<html><body><h2>Client requirements</h2>"
            b"<p>Clients MUST send the session header on every request.</p>"
            b"<p>Clients MUST cache the protocol handshake.</p>"
            b"<h2>Server requirements</h2><p>Servers MUST reject unknown session ids.</p></body></html>"
        )
        digest = build_spec_digest(
            [_html_source(html)], parse_unified_diff(SESSION_DIFF), token_budget=3000,
        )
        assert _scope_of(digest, "Servers MUST reject unknown session ids") == "Server requirements"
        assert _scope_of(digest, "Clients MUST cache the protocol handshake") == "Client requirements"

    def test_html_heading_with_block_child_anchor_is_one_heading(self):
        html = (
            '<html><body><h3 id="initialization"><div class="absolute"><a href="#initialization">'
            "​<div><svg></svg></div></a></div><span>Initialization</span></h3>"
            "<p>The client MUST send an initialize request.</p></body></html>"
        ).encode()
        digest = build_spec_digest([_html_source(html)], [], token_budget=3000)
        assert _scope_of(digest, "The client MUST send an initialize request") == "Initialization"


class TestHtmlHeadings:
    def test_each_level_becomes_its_markdown_heading(self):
        text = _strip_html("<h1>One</h1><p>a</p><h4>Four</h4><p>b</p>")
        assert text.splitlines() == ["# One", "", "a", "", "#### Four", "", "b"]

    def test_permalink_glyphs_and_whitespace_are_dropped(self):
        text = _strip_html('<h2>\n  Error   handling<a class="headerlink" href="#e">¶</a></h2>')
        assert text == "## Error handling"

    def test_a_heading_inside_a_dropped_tag_stays_dropped(self):
        text = _strip_html("<template><h2>Hidden</h2></template><p>Shown</p>")
        assert "Hidden" not in text
        assert text == "Shown"

    def test_an_empty_heading_is_just_a_break(self):
        assert _strip_html("<p>a</p><h2> </h2><p>b</p>") == "a\n\nb"

    def test_an_unclosed_heading_loses_no_content(self):
        text = _strip_html("<h2>Title<p>Clients MUST retry.</p><p>Servers MAY cache.</p>")
        assert "Clients MUST retry." in text.splitlines()
        assert "Servers MAY cache." in text.splitlines()
        assert "Title" in text


class TestSpecNoteLabels:
    def test_failures_are_labelled_by_ordinal_and_kind_never_origin(self):
        sources = [
            _doc(),
            SpecSource(origin="https://host.example.com/x.md?t=1", kind="url", text="", error="HTTP 404"),
            SpecSource(origin="/Users/alice/Google Drive/specs", kind="", text="", error="not a URL or path"),
        ]
        note = _spec_note(sources, _digest(sources))
        assert note == (
            "> 🔍 Spec-grounded: 3 source(s) · 2 constraint(s) injected\n"
            "> ⚠️ Spec fetch failed for 2 source(s): "
            "source 2 (url): HTTP 404; source 3: not a URL or path\n"
        )

    def test_reasons_are_still_redacted(self):
        failed = SpecSource(
            origin="https://secret.example.invalid/spec.md", kind="url", text="",
            error="HTTP 404 fetching https://secret.example.invalid/spec.md",
        )
        note = _spec_note([failed], "")
        assert note.startswith("> ⚠️ Spec fetch failed for 1 source(s): source 1 (url): HTTP 404")
        assert "secret.example.invalid" not in note
        assert "Spec-grounded" not in note

    def test_a_jira_only_run_posts_the_exact_ticket_count(self, monkeypatch):
        forge, _llm = _run(monkeypatch, [_jira()], post=True)
        assert "> 🔍 Spec-grounded: 1 source(s) · 4 constraint(s) injected" in forge.summaries[0]

    def test_a_jira_plus_file_run_posts_the_exact_total(self, monkeypatch):
        forge, _llm = _run(monkeypatch, [_jira(), _doc()], post=True)
        assert "> 🔍 Spec-grounded: 2 source(s) · 6 constraint(s) injected" in forge.summaries[0]
