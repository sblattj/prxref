"""Issue #113: the code-overlap tier of ``apply_sweep_dedup``.

The chunk pass and the systemic sweep reported the same two bugs in a
12-line ``orders.py`` with reworded titles, and all four findings were
posted. The line-6 pair shares only two title tokens (``sql``,
``injection``), under the reworded tier's floor of three, so only a tier
that reads the bodies' code identifiers can merge it.

The fixtures are the four findings from the issue. The negatives are the
closest different-problem pairs: two bugs on one line naming the same call,
and the measured false merge of a body-only rule (two nonce-generator
findings sharing three identifiers but describing different problems).
"""
from __future__ import annotations

import inspect
import json
import random
import types
from collections.abc import Sequence
from dataclasses import replace

import pytest

from prxref import config, orchestrator, quality
from prxref.cli import main
from prxref.quality import apply_sweep_dedup
from prxref.triage import Finding
from tests.test_dedup_similarity_wiring import CLI_REF, _meta
from tests.test_orchestrator import FakeForge, FakeLLM

PATH = "orders.py"

ORDERS_PY = '''import sqlite3


def find_orders(conn: sqlite3.Connection, customer_name: str) -> list[tuple]:
    """Return every order placed by the named customer."""
    query = f"SELECT id, total FROM orders WHERE customer = '{customer_name}'"
    return conn.execute(query).fetchall()


def apply_discount(total: float, percent: float) -> float:
    """Return the order total after taking `percent` off."""
    return total + total * percent / 100
'''

L6_CHUNK_TITLE = "SQL injection in find_orders query"
L6_CHUNK_BODY = (
    "customer_name is interpolated directly into the SQL string via f-string on "
    "line 6, so any quote or SQL fragment in the name alters the query (e.g. "
    "\"' OR 1=1 --\" returns all orders). Use a parameterized query: "
    "conn.execute(\"SELECT id, total FROM orders WHERE customer = ?\", (customer_name,))."
)
L6_SWEEP_TITLE = "SQL injection via f-string interpolation of customer_name"
L6_SWEEP_BODY = (
    "The query is built by interpolating `customer_name` directly into the SQL "
    "string (`f\"SELECT id, total FROM orders WHERE customer = '{customer_name}'\"`) "
    "instead of using a bound parameter, so any caller-controlled name can inject "
    "arbitrary SQL against the orders table. Use "
    "`conn.execute(\"SELECT id, total FROM orders WHERE customer = ?\", (customer_name,))`."
)
L12_CHUNK_TITLE = "apply_discount adds the discount instead of subtracting it"
L12_CHUNK_BODY = (
    "`return total + total * percent / 100` computes a surcharge: a 10% \"discount\" "
    "returns 110% of the total, contradicting the docstring \"Return the order total "
    "after taking `percent` off\". The operator must be `-`."
)
L12_SWEEP_TITLE = "apply_discount adds instead of subtracts"
L12_SWEEP_BODY = (
    "The docstring says the total is returned after taking `percent` off, but line "
    "12 computes total + total * percent / 100, which increases the total. It should "
    "be total - total * percent / 100 (or total * (1 - percent / 100))."
)

L6_REASON = "duplicate of chunk finding (same line, shared code: conn.execute, customer_name)"
L12_REASON = "duplicate of chunk finding (same line, shared code: apply_discount, percent)"


def _f(title: str, body: str, line: int, **kwargs) -> Finding:
    defaults = {
        "file": PATH, "line": line, "severity": "error", "confidence": 0.9,
        "title": title, "body": body,
    }
    defaults.update(kwargs)
    return Finding(**defaults)


def _issue_findings() -> tuple[list[Finding], list[Finding]]:
    chunk = [
        _f(L6_CHUNK_TITLE, L6_CHUNK_BODY, 6),
        _f(L12_CHUNK_TITLE, L12_CHUNK_BODY, 12),
    ]
    sweep = [
        _f(L6_SWEEP_TITLE, L6_SWEEP_BODY, 6, confidence=0.85),
        _f(L12_SWEEP_TITLE, L12_SWEEP_BODY, 12, confidence=0.85),
    ]
    return chunk, sweep


def _dedup(
    chunk: Sequence[Finding], sweep: Sequence[Finding], **kw
) -> tuple[list[Finding], list[Finding]]:
    out = apply_sweep_dedup(list(chunk) + list(sweep), len(chunk), **kw)
    assert len(out) == len(chunk) + len(sweep)
    return out[: len(chunk)], out[len(chunk):]


class TestTheIssuePairs:
    def test_both_sweep_copies_are_dropped_and_both_chunk_copies_kept(self):
        chunk, sweep = _dedup(*_issue_findings(), code_overlap=True)
        assert [f.drop_reason for f in chunk] == [None, None]
        assert [f.drop_reason for f in sweep] == [L6_REASON, L12_REASON]

    def test_the_dropped_copies_are_retained_unchanged_apart_from_the_reason(self):
        _, original = _issue_findings()
        _, sweep = _dedup(*_issue_findings(), code_overlap=True)
        for before, after in zip(original, sweep, strict=True):
            assert after.title == before.title
            assert after.body == before.body
            assert after.severity == before.severity
            assert after.confidence == before.confidence

    def test_the_reworded_tier_alone_still_misses_the_line_6_pair(self):
        chunk, sweep = _dedup(*_issue_findings(), similarity=0.3)
        assert [f.drop_reason for f in chunk] == [None, None]
        assert sweep[0].drop_reason is None
        assert sweep[1].drop_reason is not None
        assert sweep[1].drop_reason.startswith("duplicate of chunk finding (reworded")

    def test_with_both_tiers_the_reworded_tier_claims_line_12_first(self):
        chunk, sweep = _dedup(*_issue_findings(), similarity=0.3, code_overlap=True)
        assert [f.drop_reason for f in chunk] == [None, None]
        assert sweep[0].drop_reason == L6_REASON
        assert sweep[1].drop_reason.startswith("duplicate of chunk finding (reworded")

    def test_off_by_default_on_the_bare_function(self):
        chunk, sweep = _dedup(*_issue_findings())
        assert [f.drop_reason for f in chunk + sweep] == [None] * 4

    def test_order_of_the_input_does_not_change_the_result(self):
        chunk, sweep = _issue_findings()
        _, expected = _dedup(chunk, sweep, code_overlap=True)
        _, flipped = _dedup(chunk[::-1], sweep[::-1], code_overlap=True)
        assert sorted(f.drop_reason for f in flipped) == sorted(f.drop_reason for f in expected)


class TestDifferentProblemsStaySeparate:
    def test_two_bugs_naming_the_same_call_are_both_kept(self):
        chunk = [_f(
            "SQL injection in find_orders query",
            "`customer_name` is interpolated into the string passed to `conn.execute`; "
            "bind it with `conn.execute(sql, (customer_name,))`.",
            7,
        )]
        sweep = [_f(
            "Query result never closed",
            "The cursor returned by `conn.execute` is never closed, so each "
            "`find_orders` call leaks one for the life of the connection.",
            7,
        )]
        # Two shared identifiers (conn.execute, find_orders) clear the code
        # floor; the titles share one token and no problem class, so the
        # title condition keeps them apart.
        assert quality._code_tokens(chunk[0].title + " " + chunk[0].body) & quality._code_tokens(
            sweep[0].title + " " + sweep[0].body
        ) == {"conn.execute", "find_orders"}
        out_chunk, out_sweep = _dedup(chunk, sweep, code_overlap=True)
        assert [f.drop_reason for f in out_chunk + out_sweep] == [None, None]

    def test_the_measured_body_only_false_merge_is_kept(self):
        # glm53-zai rails-pr43227 line 20: a wrong-variable bug and a weak
        # nonce source on one expression. Three shared identifiers, two
        # shared title words, no common problem class.
        chunk = [_f(
            "Nonce generator references undefined `request` instead of lambda param `r`",
            "The suggested nonce generator is `->(r) { Digest::SHA1.hexdigest("
            "request.session.id.to_s) }` but the lambda parameter is `r`.",
            20,
        )]
        sweep = [_f(
            "Suggested nonce generator is derivable from the session cookie",
            "The template recommends `Digest::SHA1.hexdigest(request.session.id.to_s)` "
            "as the nonce generator; the session id is sent to every client.",
            20,
        )]
        out_chunk, out_sweep = _dedup(chunk, sweep, code_overlap=True)
        assert [f.drop_reason for f in out_chunk + out_sweep] == [None, None]

    def test_one_shared_identifier_is_not_enough(self):
        chunk = [_f("SQL injection in find_orders", "Interpolates into `conn.execute`.", 6)]
        sweep = [_f("SQL injection via string formatting", "Built for `conn.execute`.", 6)]
        out_chunk, out_sweep = _dedup(chunk, sweep, code_overlap=True)
        assert [f.drop_reason for f in out_chunk + out_sweep] == [None, None]


class TestBoundaries:
    def test_a_more_severe_sweep_copy_is_kept(self):
        chunk, sweep = _issue_findings()
        chunk = [_f(f.title, f.body, f.line, severity="warning") for f in chunk]
        out_chunk, out_sweep = _dedup(chunk, sweep, code_overlap=True)
        assert [f.drop_reason for f in out_chunk + out_sweep] == [None] * 4

    def test_a_less_severe_sweep_copy_is_dropped(self):
        chunk, sweep = _issue_findings()
        sweep = [_f(f.title, f.body, f.line, severity="warning") for f in sweep]
        _, out_sweep = _dedup(chunk, sweep, code_overlap=True)
        assert [f.drop_reason for f in out_sweep] == [L6_REASON, L12_REASON]

    def test_a_different_line_is_never_compared(self):
        chunk, sweep = _issue_findings()
        sweep = [_f(f.title, f.body, f.line + 1) for f in sweep]
        _, out_sweep = _dedup(chunk, sweep, code_overlap=True)
        assert [f.drop_reason for f in out_sweep] == [None, None]

    def test_a_different_file_is_never_compared(self):
        chunk, sweep = _issue_findings()
        sweep = [_f(f.title, f.body, f.line, file="other.py") for f in sweep]
        _, out_sweep = _dedup(chunk, sweep, code_overlap=True)
        assert [f.drop_reason for f in out_sweep] == [None, None]

    def test_file_level_findings_are_never_compared(self):
        chunk, sweep = _issue_findings()
        chunk = [_f(f.title, f.body, 0) for f in chunk]
        sweep = [_f(f.title, f.body, 0) for f in sweep]
        out_chunk, out_sweep = _dedup(chunk, sweep, code_overlap=True)
        assert [f.drop_reason for f in out_chunk + out_sweep] == [None] * 4

    def test_a_chunk_finding_is_never_dropped_and_two_sweep_findings_never_compared(self):
        chunk, sweep = _issue_findings()
        # Everything on the chunk side: nothing to compare across.
        out = apply_sweep_dedup(chunk + sweep, len(chunk + sweep), code_overlap=True)
        assert [f.drop_reason for f in out] == [None] * 4
        # Everything on the sweep side: likewise.
        out = apply_sweep_dedup(chunk + sweep, 0, code_overlap=True)
        assert [f.drop_reason for f in out] == [None] * 4

    def test_a_dropped_chunk_finding_does_not_suppress_its_sweep_copy(self):
        chunk, sweep = _issue_findings()
        chunk = [replace(f, drop_reason="confidence 0.40 below floor 0.60") for f in chunk]
        _, out_sweep = _dedup(chunk, sweep, code_overlap=True)
        assert [f.drop_reason for f in out_sweep] == [None, None]

    def test_randomized_input_order_never_changes_the_result(self):
        rng = random.Random(113)
        chunk, sweep = _issue_findings()
        extra_chunk = [_f("Unrelated `total` rounding", "uses `round_half`", 12)]
        extra_sweep = [_f("Docstring typo", "`find_orders` docstring", 5)]
        base_chunk = chunk + extra_chunk
        base_sweep = sweep + extra_sweep
        expected = {
            repr(f) for f in apply_sweep_dedup(
                base_chunk + base_sweep, len(base_chunk), code_overlap=True
            )
        }
        for _ in range(50):
            c = base_chunk[:]
            s = base_sweep[:]
            rng.shuffle(c)
            rng.shuffle(s)
            got = {repr(f) for f in apply_sweep_dedup(c + s, len(c), code_overlap=True)}
            assert got == expected


class TestConfig:
    def test_on_by_default(self):
        assert config._DEFAULTS["dedup_code_overlap"] is True
        assert "dedup_code_overlap" in config._BOOL_KEYS
        assert "dedup_code_overlap" in config.FILE_KEYS

    def test_zero_turns_it_off(self, monkeypatch):
        monkeypatch.setenv("PRXREF_DEDUP_CODE_OVERLAP", "0")
        assert config.load_config()["dedup_code_overlap"] is False

    def test_orchestrate_review_defaults_it_on(self):
        param = inspect.signature(orchestrator.orchestrate_review).parameters[
            "dedup_code_overlap"
        ]
        assert param.default is True


def _orders_diff() -> str:
    lines = ORDERS_PY.splitlines()
    body = "\n".join(f"+{ln}" for ln in lines)
    return (
        f"diff --git a/{PATH} b/{PATH}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{PATH}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}\n"
    )


@pytest.mark.usefixtures("contract_stubs")
class TestThroughTheRealCliAndOrchestrator:
    """The acceptance case end to end: only forge, model and reviewer units are doubles."""

    @pytest.fixture
    def rig(self, monkeypatch):
        chunk, sweep = _issue_findings()

        def _review_chunk(llm, files, **kwargs):
            return list(chunk), _meta()

        def _review_systemic(llm, digest, **kwargs):
            return list(sweep), _meta()

        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _review_chunk)
        monkeypatch.setattr(orchestrator.reviewer, "review_systemic", _review_systemic)
        forge = FakeForge(diff=_orders_diff())
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: CLI_REF)
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: forge)
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: FakeLLM())
        return types.SimpleNamespace(forge=forge)

    def _rows(self, capsys) -> list[dict]:
        argv = ["review", "--pr-url", CLI_REF.url, "--no-post", "--format", "json"]
        assert main(argv) == 0
        out, _ = capsys.readouterr()
        return json.loads(out)["findings"]

    def test_the_default_config_posts_two_findings_not_four(self, rig, capsys):
        rows = self._rows(capsys)
        active = sorted((r["line"], r["title"]) for r in rows if r["drop_reason"] is None)
        dropped = sorted((r["line"], r["drop_reason"]) for r in rows if r["drop_reason"])
        assert active == [(6, L6_CHUNK_TITLE), (12, L12_CHUNK_TITLE)]
        assert dropped == [(6, L6_REASON), (12, L12_REASON)]

    def test_turning_it_off_posts_all_four(self, rig, monkeypatch, capsys):
        monkeypatch.setenv("PRXREF_DEDUP_CODE_OVERLAP", "0")
        rows = self._rows(capsys)
        assert [r["drop_reason"] for r in rows] == [None] * 4
