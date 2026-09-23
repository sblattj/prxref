"""Spec unit extraction: block grouping, sentence units, pins, relevance scoring.

Covers COR-1/LIVE-3 (hard-wrapped normative statements), COR-5 (normative
keywords never count as relevance overlap) and COR backlog 8 (only a
standalone version pin becomes its own constraint).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import prxref.specs as specs
from prxref.specs import SpecSource, build_spec_digest
from prxref.triage import parse_unified_diff

EVALS = Path(__file__).parent / "evals"

_RENDER_RE = re.compile(r"^\[spec:[^\]#]+#L(\d+)\] \((MUST|SHOULD|MAY)\) (.+)$")


def _src(text: str, origin: str = "/docs/spec.md") -> SpecSource:
    return SpecSource(origin=origin, kind="file", text=text, error="")


def _units(text: str) -> list[specs._Unit]:
    return specs._spec_units(_src(text), 0)


def _parsed(text: str) -> list[tuple[int, str, str]]:
    out = []
    for unit in _units(text):
        m = _RENDER_RE.match(unit.render)
        assert m, unit.render
        out.append((int(m.group(1)), m.group(2), m.group(3)))
    return out


def _statements(text: str) -> list[str]:
    return [statement for _, _, statement in _parsed(text)]


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


def _eval_sources(case: str) -> list[SpecSource]:
    root = EVALS / case
    paths = [root / "ticket.md", *sorted((root / "docs").iterdir())]
    return [_src(p.read_text(encoding="utf-8"), origin=str(p)) for p in paths]


def _eval_digest(case: str, *, with_diff: bool) -> str:
    files = parse_unified_diff((EVALS / case / "diff.patch").read_text()) if with_diff else []
    return build_spec_digest(_eval_sources(case), files, token_budget=3000)


def _eval_doc(case: str) -> str:
    (doc,) = sorted((EVALS / case / "docs").iterdir())
    return doc.read_text(encoding="utf-8")


_FILLER = (
    "The ingest service accepts webhook deliveries from partner systems. "
    "Each delivery carries a JSON body and a small set of headers. "
    "Deliveries arrive in bursts during the nightly settlement window. "
    "The queue absorbs those bursts before the workers pick them up. "
    "Operators watch the queue depth on the shared ingest dashboard. "
    "Partners are onboarded through the integrations team."
)


class TestWrappedStatements:
    @pytest.mark.parametrize("with_diff", [False, True])
    def test_case_002_digest_keeps_the_wrapped_subjects(self, with_diff):
        digest = _eval_digest("case-002-session-token-logging", with_diff=with_diff)
        for needle in ("APP_", "VITE_", "Authorization", "Tokens, session tokens"):
            assert needle in digest, needle

    def test_rfc_wrapped_paragraph_keeps_its_middle_line(self):
        text = (
            "   A client MUST NOT send a request body larger than the limit\n"
            "   advertised by the server in the max-body-size field, and it\n"
            "   MUST close the connection if the server responds with 413.\n"
        )
        assert _statements(text) == [
            "A client MUST NOT send a request body larger than the limit advertised by the "
            "server in the max-body-size field, and it MUST close the connection if the "
            "server responds with 413."
        ]
        digest = build_spec_digest([_src(text, origin="rfc.txt")], [], token_budget=3000)
        assert "max-body-size" in digest

    def test_a_must_wrapped_over_two_lines_keeps_both_halves(self):
        text = "Session cookies MUST set the\n`Secure` and `HttpOnly` attributes.\n"
        assert _statements(text) == ["Session cookies MUST set the `Secure` and `HttpOnly` attributes."]

    def test_an_indented_continuation_joins_its_list_item(self):
        text = "- Clients MUST send the\n    `X-Trace` header on every retry.\n- Unrelated item.\n"
        assert _statements(text) == ["- Clients MUST send the `X-Trace` header on every retry."]

    def test_blockquote_markers_are_stripped_and_quoted_lines_join(self):
        text = "> Relays MUST forward the\n> `traceparent` header unchanged.\n"
        assert _statements(text) == ["Relays MUST forward the `traceparent` header unchanged."]

    def test_a_keyword_free_block_keeps_nothing(self):
        assert _units(_FILLER.replace(". ", ".\n")) == []


class TestSentenceUnits:
    def test_a_late_must_in_a_paragraph_over_400_chars_keeps_its_clause(self):
        text = (
            _FILLER.replace(". ", ".\n")
            + " Webhook signatures MUST be verified with the\nshared HMAC key.\n"
        )
        assert len(" ".join(text.split())) > specs._STATEMENT_MAX_CHARS
        assert _parsed(text) == [(1, "MUST", "Webhook signatures MUST be verified with the shared HMAC key.")]

    def test_consecutive_keyword_lines_keep_their_own_labels(self):
        text = (
            "Clients MUST send the header.\n"
            "Servers SHOULD log the request.\n"
            "Proxies MAY cache the response.\n"
        )
        assert [(label, s) for _, label, s in _parsed(text)] == [
            ("MUST", "Clients MUST send the header."),
            ("SHOULD", "Servers SHOULD log the request."),
            ("MAY", "Proxies MAY cache the response."),
        ]

    def test_semicolon_clauses_with_two_keywords_split_with_their_labels(self):
        text = (
            "An initialize request missing any required field MUST be treated as a spec\n"
            "violation; conforming servers SHOULD reject it.\n"
        )
        assert [(label, s) for _, label, s in _parsed(text)] == [
            ("MUST", "An initialize request missing any required field MUST be treated as a spec violation;"),
            ("SHOULD", "conforming servers SHOULD reject it."),
        ]

    def test_eg_and_backticked_dots_do_not_split_a_sentence(self):
        text = (
            _FILLER
            + " Server-side secrets MUST be read from variables carrying the\n"
            "`APP_` prefix only (e.g. `APP_SESSION_SECRET` or `app.secret. key`), i.e. never\n"
            "from a `VITE_` variable.\n"
        )
        assert _statements(text) == [
            "Server-side secrets MUST be read from variables carrying the `APP_` prefix only "
            "(e.g. `APP_SESSION_SECRET` or `app.secret. key`), i.e. never from a `VITE_` variable."
        ]

    def test_a_short_block_with_one_keyword_sentence_stays_whole(self):
        text = (
            "Tokens and `Authorization` header values MUST NEVER be written\n"
            "to logs. Log the opaque session ID instead; the session ID is not a secret.\n"
        )
        assert _statements(text) == [
            "Tokens and `Authorization` header values MUST NEVER be written to logs. "
            "Log the opaque session ID instead; the session ID is not a secret."
        ]

    def test_can_runs_per_sentence_not_per_block(self):
        text = (
            "Support tickets spike after every deploy: the admin console keeps sessions in\n"
            "a process-local dict, so any restart invalidates them. Introduce a durable\n"
            "session store and wire it into the app. Add an issuance log line so support\n"
            "can correlate a session with a user when triaging incidents.\n"
        )
        assert _parsed(text) == [
            (1, "MAY", "Add an issuance log line so support can correlate a session with a user "
                       "when triaging incidents."),
        ]

    def test_every_unit_is_one_capped_line(self):
        text = "Clients MUST " + "retry " * 120 + "forever.\n"
        (statement,) = _statements(text)
        assert len(statement) == specs._STATEMENT_MAX_CHARS
        assert "\n" not in statement


class TestBlockBoundaries:
    def test_table_rows_are_their_own_units(self):
        text = (
            "| Field | Rule |\n"
            "|---|---|\n"
            "| `id` | MUST be a UUID |\n"
            "| `name` | SHOULD be short |\n"
        )
        assert _parsed(text) == [
            (3, "MUST", "| `id` | MUST be a UUID |"),
            (4, "SHOULD", "| `name` | SHOULD be short |"),
        ]

    def test_fenced_lines_are_never_joined_or_read_as_headings(self):
        text = (
            "The client MUST send pings.\n"
            "```python\n"
            "# The client MUST retry on 503\n"
            "x = 1\n"
            "```\n"
            "Servers SHOULD answer pings.\n"
        )
        units = _units(text)
        assert _parsed(text) == [
            (1, "MUST", "The client MUST send pings."),
            (3, "MUST", "# The client MUST retry on 503"),
            (6, "SHOULD", "Servers SHOULD answer pings."),
        ]
        assert all(u.heading_render is None for u in units)

    def test_a_setext_heading_scopes_and_an_underline_never_joins(self):
        text = (
            "Transport Rules\n"
            "---------------\n"
            "Clients MUST use TLS.\n"
            "===\n"
            "Servers SHOULD pin certificates.\n"
        )
        units = _units(text)
        assert _statements(text) == ["Clients MUST use TLS.", "Servers SHOULD pin certificates."]
        assert [u.heading_key for u in units] == ["transport-rules", "transport-rules"]

    def test_anchor_and_doc_idx_come_from_the_block_first_original_line(self):
        text = "Intro prose.\n\nMore prose.\n\n- The retry header is\n  REQUIRED on every retry.\n"
        (unit,) = _units(text)
        assert unit.render.startswith("[spec:spec.md#L5] (MUST) - The retry header is REQUIRED")
        assert unit.doc_idx == 5

    def test_sentence_units_share_the_block_anchor(self):
        text = "Intro prose.\n\nClients MUST send the header.\nServers SHOULD log it.\n"
        assert [line for line, _, _ in _parsed(text)] == [3, 3]


class TestColonLeadIn:
    def test_case_003_naming_rule_carries_its_list(self):
        statements = _statements(_eval_doc("case-003-config-schema-pin"))
        (naming,) = [s for s in statements if "MUST be named" in s]
        assert "EXACTLY two underscores" in naming
        assert "lowercase words separated by single underscores" in naming

    def test_case_001_required_fields_lead_in_carries_its_list(self):
        statements = _statements(_eval_doc("case-001-mcp-protocol-upgrade"))
        (lead,) = [s for s in statements if "MUST include ALL of:" in s]
        for needle in ("`protocolVersion`", "`capabilities`", "`clientInfo`", "`name`", "`version`"):
            assert needle in lead, needle
        assert any(s.startswith("- `clientInfo`: object with REQUIRED") for s in statements)

    def test_a_list_item_lead_in_attaches_only_its_children(self):
        text = (
            "- Tokens MUST include:\n"
            "  - an issuer\n"
            "  - an expiry\n"
            "- Refresh tokens MUST rotate on use.\n"
        )
        assert _statements(text) == [
            "- Tokens MUST include: - an issuer - an expiry",
            "- Refresh tokens MUST rotate on use.",
        ]

    def test_the_attached_list_stops_at_the_cap_on_an_item_boundary(self):
        items = [f"- `feature_{i:02d}`: a descriptive capability entry" for i in range(20)]
        text = "Clients MUST support:\n\n" + "\n".join(items) + "\n- `final`: MUST be negotiated last\n"
        statements = _statements(text)
        lead = statements[0]
        assert lead.startswith("Clients MUST support: - `feature_00`")
        assert len(lead) <= specs._STATEMENT_MAX_CHARS
        assert lead.endswith("a descriptive capability entry")
        assert statements[1:] == ["- `final`: MUST be negotiated last"]

    def test_a_paragraph_after_the_list_is_not_attached(self):
        text = "Clients MUST send:\n\n- a name\n\nThe rest is prose.\n"
        assert _statements(text) == ["Clients MUST send: - a name"]


class TestVersionPins:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("2026-07-28", True),
            ('`"2026-07-28"`.', True),
            ("- 2026-07-28", True),
            ("v1.2", True),
            ("3.0.1:", True),
            ("Last updated 2025-03-01 by the docs team.", False),
            ("Version 2025-06-18", False),
            ('"protocolVersion": "2025-06-18",', False),
            ("Protocol version 3.2 is the floor.", False),
        ],
    )
    def test_only_a_standalone_pin_is_a_pin(self, line, expected):
        assert specs._is_version_pin_line(line) is expected

    def test_dated_prose_is_not_a_must(self):
        assert _units("Last updated 2025-03-01 by the docs team.\n\nVersion 2025-06-18\n") == []

    def test_a_standalone_pin_sentence_is_its_own_must(self):
        text = "Protocol version 3.2 is REQUIRED for all clients.\n2026-07-28\n"
        assert [(label, s) for _, label, s in _parsed(text)] == [
            ("MUST", "Protocol version 3.2 is REQUIRED for all clients."),
            ("MUST", "2026-07-28"),
        ]

    def test_case_001_pin_stays_inside_its_must_sentence(self):
        statements = _statements(_eval_doc("case-001-mcp-protocol-upgrade"))
        assert "- The initialize request MUST carry `protocolVersion` exactly `\"2026-07-28\"`." in statements
        assert all(s.strip("`\".") != "2026-07-28" for s in statements)
        assert any("request header on every subsequent request" in s for s in statements)


class TestEvalCorpusUnits:
    @pytest.mark.parametrize("case", sorted(p.name for p in EVALS.glob("case-*")))
    def test_units_are_verbatim_single_capped_lines(self, case):
        for src in _eval_sources(case):
            flat = " ".join(src.text.split())
            for unit in specs._spec_units(src, 0):
                m = _RENDER_RE.match(unit.render)
                assert m, unit.render
                statement = m.group(3)
                assert statement in flat, statement
                assert len(statement) <= specs._STATEMENT_MAX_CHARS

    @pytest.mark.parametrize("case", sorted(p.name for p in EVALS.glob("case-*")))
    def test_digest_is_deterministic(self, case):
        assert _eval_digest(case, with_diff=True) == _eval_digest(case, with_diff=True)


class TestNormativeTokensNeverScore:
    _SHOULD = "Exports SHOULD include the invoice currency."

    def _digest(self, spec_text: str, diff_lines: list[str], budget: int) -> str:
        diff = parse_unified_diff(_added_file("src/export.py", diff_lines))
        return build_spec_digest([_src(spec_text)], diff, token_budget=budget)

    def test_the_set_covers_every_strength_word(self):
        assert {
            "must", "shall", "required", "recommended", "forbidden",
            "discouraged", "should", "never", "optional",
        } <= specs._NORMATIVE_TOKENS

    def test_a_must_in_the_diff_does_not_reorder_the_digest(self):
        spec_text = (
            "The audit ledger MUST be append-only.\n\n"
            "Webhooks MUST be signed.\n\n"
            "Backups MUST be encrypted at rest.\n\n"
            f"{self._SHOULD}\n"
        )
        control = self._digest(spec_text, ["def export(currency):"], budget=70)
        keyword = self._digest(spec_text, ["def export(currency):", "    # currency must be ISO-4217"], budget=70)
        assert "invoice currency" in control
        assert keyword == control

    def test_required_true_in_the_diff_does_not_reorder_the_digest(self):
        spec_text = (
            "A signing key is REQUIRED for uploads.\n\n"
            "An audit trail is REQUIRED for deletes.\n\n"
            f"{self._SHOULD}\n"
        )
        control = self._digest(spec_text, ['parser.add_argument("--currency")'], budget=70)
        keyword = self._digest(spec_text, ['parser.add_argument("--currency", required=True)'], budget=70)
        assert "invoice currency" in control
        assert keyword == control

    def test_content_overlap_still_ranks(self):
        spec_text = "Webhooks MUST be signed.\n\nThe invoice currency MUST be ISO-4217.\n"
        digest = self._digest(spec_text, ["# currency must be set"], budget=3000)
        assert digest.index("invoice currency MUST") < digest.index("Webhooks MUST be signed")
