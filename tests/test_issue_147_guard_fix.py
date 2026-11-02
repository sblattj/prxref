"""Issue #147: a suggested fix that would remove a guard is marked, not trusted.

``quality.apply_guard_fix_check`` (always on, no config key) never drops a
finding and never moves its severity or confidence. Pinned here:

- a structured suggestion that replaces a guard line without keeping any of
  its guard identifiers is cleared, the finding is kept, and the body gets the
  ``Unverified fix:`` line; one that keeps the identifier is the same object;
- prose that ADVISES removing or relocating a guard gets the line once, and
  prose that REPORTS a removal, adds a check or removes something else is
  passed through as the same object;
- a body that already holds ``Unverified fix:`` gets no second line and
  survives the hedge gate;
- all of it through ``orchestrate_review`` with a fake LLM: the finding posts,
  severity unchanged, every other pass leaves the line alone, and a run with
  no match carries no ``guard_fix`` record key.
"""
from __future__ import annotations

import pytest

from prxref import orchestrator, quality
from prxref.quality import (
    GUARD_FIX_LINE,
    GUARD_FIX_REASONS,
    apply_guard_fix_check,
    apply_hedge_gate,
)
from prxref.triage import Finding, parse_unified_diff
from tests.test_orchestrator import REF, FakeForge, FakeLLM

SERVICE_DIFF = (
    "diff --git a/src/service.py b/src/service.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/service.py\n"
    "@@ -0,0 +1,6 @@\n"
    "+def start(tenant, operation):\n"
    "+    if tenant != SERVICE_TENANT:\n"
    "+        raise Forbidden(tenant)\n"
    "+    return run_paid(operation)\n"
    "+\n"
    "+LIMIT = 3\n"
)
SERVICE_FILES = parse_unified_diff(SERVICE_DIFF)
CASE_ONE_BODY = (
    "The service checks the tenant inline before starting a paid operation. "
    "Drop the inline check and rely on the endpoint's declared security scope."
)
CASE_ONE_PROSE = "drop the inline check and rely on the endpoint's declared security scope"


def _finding(body: str = "plain body", title: str = "Inline tenant check", **extra) -> Finding:
    values = {
        "file": "src/service.py", "line": 2, "severity": "warning", "confidence": 0.9,
        "title": title, "body": body,
    }
    values.update(extra)
    return Finding(**values)


def _check(*findings: Finding):
    return apply_guard_fix_check(list(findings), SERVICE_FILES)


ADVICE = [
    CASE_ONE_PROSE,
    "Drop the inline check and rely on the endpoint's declared security scope.",
    "remove the tenant check, the scope already enforces it",
    "The auth check is redundant and can be deleted.",
    "Move validation to the caller.",
    "You can simply delete the authorization guard here.",
    "Consider removing the permission check.",
    "This is safe to remove the ownership validation.",
    "The role check is redundant and should be removed.",
    "Relocate the input validation into the controller.",
    "Rely on the gateway's tenant policy instead.",
]

CONTROLS = [
    "This PR removes the tenant check.",
    "The auth guard was dropped.",
    "Add a tenant check here.",
    "Remove the unused import.",
    "Rely on the framework's connection pool.",
    "Do not remove the tenant check.",
    "Never drop the authorization check.",
    "The PR deletes the permission check and the caller no longer validates.",
    "Moving the file to src/util.py breaks the import.",
    "Remove the health check endpoint from the router.",
    "Keep the auth check and add a test for it.",
    "The tenant check is skipped when the header is missing.",
    "Validation is moved to the caller in this diff, which leaves the endpoint unprotected.",
]


class TestProseAdvice:
    @pytest.mark.parametrize("text", ADVICE)
    def test_advice_gets_exactly_one_line(self, text):
        f = _finding(body=text, suggestion=None)
        (out,), (reason,) = _check(f)
        assert reason == ("advises_guard_removal",)
        assert out.body == f"{text}\n\n{GUARD_FIX_LINE}"
        assert out.body.count("Unverified fix:") == 1
        assert (out.severity, out.confidence, out.drop_reason) == (f.severity, f.confidence, None)

    def test_the_title_counts_too(self):
        f = _finding(title="Drop the tenant check and rely on scope", body="The scope is declared.")
        (out,), (reason,) = _check(f)
        assert reason == ("advises_guard_removal",)
        assert out.body.endswith(GUARD_FIX_LINE)

    @pytest.mark.parametrize("text", CONTROLS)
    def test_a_report_or_other_advice_is_the_same_object(self, text):
        f = _finding(body=text)
        (out,), (reason,) = _check(f)
        assert out is f
        assert reason == ()

    @pytest.mark.parametrize("text", ADVICE)
    def test_idempotent(self, text):
        once, _ = _check(_finding(body=text))
        twice, reasons = apply_guard_fix_check(once, SERVICE_FILES)
        assert twice[0].body == once[0].body
        assert twice[0].body.count("Unverified fix:") == 1
        assert reasons == [("advises_guard_removal",)]

    @pytest.mark.parametrize("text", ADVICE[:3])
    def test_a_model_written_line_gets_no_second_one(self, text):
        body = f"Unverified fix: the scope may enforce it. {text}"
        f = _finding(body=body)
        (out,), _ = _check(f)
        assert out is f

    def test_a_dropped_finding_is_untouched(self):
        f = _finding(body=CASE_ONE_PROSE, drop_reason="duplicate of chunk finding")
        (out,), (reason,) = _check(f)
        assert out is f and reason == ()

    def test_an_empty_body_still_gets_the_line_alone(self):
        f = _finding(body="", title="Drop the tenant check")
        (out,), _ = _check(f)
        assert out.body == GUARD_FIX_LINE

    def test_order_and_length_are_kept(self):
        a, b, c = _finding(body=CONTROLS[0]), _finding(body=CASE_ONE_PROSE), _finding(body=CONTROLS[1])
        out, reasons = _check(a, b, c)
        assert out[0] is a and out[2] is c and out[1] is not b
        assert reasons == [(), ("advises_guard_removal",), ()]


class TestStructuredSuggestion:
    @pytest.mark.parametrize("text", ["", "    return run_paid(operation)", "        pass"])
    def test_a_suggestion_without_the_guard_identifier_is_cleared(self, text):
        f = _finding(body="Needless check.", line=2, suggestion=text, suggestion_end_line=3)
        (out,), (reason,) = _check(f)
        assert reason == ("suggestion_drops_guard",)
        assert out.suggestion is None and out.suggestion_end_line == 0
        assert out.body == f"Needless check.\n\n{GUARD_FIX_LINE}"
        assert (out.severity, out.confidence, out.file, out.line) == ("warning", 0.9, f.file, f.line)

    def test_a_suggestion_that_keeps_an_identifier_is_the_same_object(self):
        text = "    if tenant != SERVICE_TENANT or not operation:\n        raise Forbidden(tenant)"
        f = _finding(body="Tighten it.", line=2, suggestion=text, suggestion_end_line=3)
        (out,), (reason,) = _check(f)
        assert out is f and reason == ()

    def test_a_suggestion_over_a_line_with_no_guard_is_the_same_object(self):
        f = _finding(body="Name it.", line=6, suggestion="LIMIT = 4")
        (out,), (reason,) = _check(f)
        assert out is f and reason == ()

    def test_a_suggestion_outside_the_hunk_is_left_to_the_suggestion_validator(self):
        f = _finding(body="Far away.", line=90, suggestion="")
        (out,), (reason,) = _check(f)
        assert out is f and reason == ()

    def test_the_line_is_not_added_twice_when_the_body_has_it(self):
        f = _finding(body="Unverified fix: check the scope.", line=2, suggestion="", suggestion_end_line=3)
        (out,), (reason,) = _check(f)
        assert reason == ("suggestion_drops_guard",)
        assert out.suggestion is None
        assert out.body == "Unverified fix: check the scope."

    def test_both_reasons_can_fire_and_add_one_line(self):
        f = _finding(body=CASE_ONE_PROSE, line=2, suggestion="", suggestion_end_line=3)
        (out,), (reason,) = _check(f)
        assert reason == GUARD_FIX_REASONS
        assert out.body.count("Unverified fix:") == 1


class TestHedgeGateKeepsIt:
    def test_the_line_is_not_hedging(self):
        f = _finding(body=f"{CASE_ONE_BODY}\n\n{GUARD_FIX_LINE}")
        (out,) = apply_hedge_gate([f])
        assert out is f and out.drop_reason is None

    def test_a_model_written_unverified_body_is_not_hedging(self):
        f = _finding(body="Unverified fix: confirm the scope rejects other tenants. Drop the check.")
        (out,) = apply_hedge_gate([f])
        assert out.drop_reason is None


class TestPatterns:
    def test_every_pattern_has_a_true_and_a_false_example(self):
        assert quality._GUARD_REMOVE_RE.search("Drop the inline check")
        assert not quality._GUARD_REMOVE_RE.search("The check was dropped")
        assert quality._GUARD_RELY_RE.search("and rely on the security scope")
        assert not quality._GUARD_RELY_RE.search("rely on the connection pool")
        assert quality._GUARD_PASSIVE_RE.search("the auth check can be deleted")
        assert not quality._GUARD_PASSIVE_RE.search("the auth check was deleted")
        assert quality._guard_identifiers(["if (tenant != SERVICE_TENANT) throw x"]) == {
            "tenant", "service_tenant",
        }
        assert quality._guard_identifiers(["return run_paid(operation)"]) == set()


def _chunk_reviewer(findings):
    def review_chunk(llm, files, **_kw):
        return list(findings), {
            "escalations": [], "input_tokens": 1, "output_tokens": 1,
            "model": "test-model-1", "elapsed_ms": 1, "error": "",
        }
    return review_chunk


def _run(monkeypatch, findings, diff=SERVICE_DIFF, **knobs):
    monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _chunk_reviewer(findings))
    return orchestrator.orchestrate_review(
        FakeForge(diff=diff), REF, FakeLLM({}), post=False, max_workers=1, **knobs,
    )


@pytest.mark.usefixtures("contract_stubs")
class TestThroughTheOrchestrator:
    def test_case_one_keeps_severity_and_gets_the_line(self):
        llm = FakeLLM({"src/service.py": [{
            "file": "src/service.py", "line": 2, "severity": "error", "confidence": 0.9,
            "title": "Inline tenant check duplicates the endpoint scope", "body": CASE_ONE_BODY,
        }]})
        forge = FakeForge(diff=SERVICE_DIFF)
        res = orchestrator.orchestrate_review(forge, REF, llm, post=True, max_workers=1)
        (f,) = res["findings_active"]
        assert f.severity == "error" and f.confidence == 0.9
        assert f.body == f"{CASE_ONE_BODY}\n\n{GUARD_FIX_LINE}"
        assert not res["findings_dropped"]
        assert res["guard_fix"] == {"tagged": 1, "suggestions_cleared": 0}
        posted = [c.body for batch in forge.inline_batches for c in batch]
        assert len(posted) == 1 and "Unverified fix:" in posted[0]

    def test_a_model_written_line_survives_every_pass_once(self):
        body = f"Unverified fix: confirm the scope rejects other tenants. {CASE_ONE_BODY}"
        llm = FakeLLM({"src/service.py": [{
            "file": "src/service.py", "line": 2, "severity": "warning", "confidence": 0.9,
            "title": "Inline tenant check duplicates the endpoint scope", "body": body,
        }]})
        res = orchestrator.orchestrate_review(FakeForge(diff=SERVICE_DIFF), REF, llm, post=False, max_workers=1)
        (f,) = res["findings_active"]
        assert f.body == body
        assert "guard_fix" not in res

    def test_a_cleared_suggestion_keeps_the_finding(self, monkeypatch):
        f = _finding(
            title="Inline tenant check is redundant", body="The endpoint already checks it.",
            suggestion="", suggestion_end_line=3, severity="error",
        )
        res = _run(monkeypatch, [f], suggestions="on")
        (out,) = res["findings_active"]
        assert out.suggestion is None and out.suggestion_end_line == 0
        assert out.severity == "error" and out.body.endswith(GUARD_FIX_LINE)
        assert res["guard_fix"] == {"tagged": 1, "suggestions_cleared": 1}

    def test_a_suggestion_that_keeps_the_identifier_is_untouched(self, monkeypatch):
        text = "    if tenant != SERVICE_TENANT or not operation:\n        raise Forbidden(tenant)"
        f = _finding(
            title="Inline tenant check misses an empty operation", body="Reject it as well.",
            suggestion=text, suggestion_end_line=3,
        )
        res = _run(monkeypatch, [f], suggestions="on")
        (out,) = res["findings_active"]
        assert out.suggestion == text and out.body == "Reject it as well."
        assert "guard_fix" not in res

    def test_a_report_posts_unchanged_with_no_record_key(self):
        llm = FakeLLM({"src/service.py": [{
            "file": "src/service.py", "line": 2, "severity": "warning", "confidence": 0.9,
            "title": "Tenant check weakened",
            "body": "This PR removes the tenant check, so any tenant can start the operation.",
        }]})
        res = orchestrator.orchestrate_review(FakeForge(diff=SERVICE_DIFF), REF, llm, post=False, max_workers=1)
        (f,) = res["findings_active"]
        assert "Unverified fix" not in f.body
        assert "guard_fix" not in res
