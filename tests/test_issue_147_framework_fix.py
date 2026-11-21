"""Issue #147, case two: a "do X instead" fix that rests on framework behaviour.

The issue's second suggestion said to mark an inner method as not
transactional so a slow external call stops holding a database connection.
That does not help: the caller's suspended transaction still holds its
connection. Whether such a fix works is decided by framework code the diff
never shows, so ``quality.apply_guard_fix_check`` marks it with
:data:`quality.FRAMEWORK_FIX_LINE` (reason ``framework_fix_unverified``)
instead of letting it post as a confident instruction. Pinned here:

- prose that ADVISES a framework mechanism (an annotation, propagation, a
  proxy, an interceptor or middleware...) gets the line once, even when the
  body also quotes the diff's own ``@Transactional`` while describing the
  defect; a report, a negation, a gerund consequence and a plain fix pass
  through as the same object;
- a code suggestion that introduces an annotation the replaced head lines do
  not carry gets the line and keeps its suggestion;
- a guard removal and a framework fix together get only the guard line;
- the issue's case through ``orchestrate_review``: the finding posts with its
  severity, the line and the ``guard_fix`` record.
"""
from __future__ import annotations

import pytest

from prxref import orchestrator, quality
from prxref.quality import (
    FRAMEWORK_FIX_LINE,
    GUARD_FIX_LINE,
    GUARD_FIX_REASONS,
    apply_guard_fix_check,
    apply_hedge_gate,
)
from prxref.triage import Finding, parse_unified_diff
from tests.test_orchestrator import REF, FakeForge, FakeLLM

ORDER_DIFF = (
    "diff --git a/src/main/java/shop/OrderService.java b/src/main/java/shop/OrderService.java\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/main/java/shop/OrderService.java\n"
    "@@ -0,0 +1,12 @@\n"
    "+package shop;\n"
    "+\n"
    "+public class OrderService {\n"
    "+    @Transactional\n"
    "+    public Order placeOrder(Cart cart) {\n"
    "+        Order order = orders.save(Order.from(cart));\n"
    "+        Rates rates = rateClient.fetchRates(cart.currency());\n"
    "+        order.price(rates);\n"
    "+        return order;\n"
    "+    }\n"
    "+\n"
    "+}\n"
)
ORDER_FILE = "src/main/java/shop/OrderService.java"
ORDER_FILES = parse_unified_diff(ORDER_DIFF)
CASE_TWO_BODY = (
    "`placeOrder` is `@Transactional` and calls `rateClient.fetchRates`, a slow external "
    "HTTP call, so the database connection stays checked out for the whole call. "
    "Mark `fetchRates` as not transactional with "
    "`@Transactional(propagation = Propagation.NOT_SUPPORTED)`."
)

FRAMEWORK_ADVICE = [
    CASE_TWO_BODY,
    "Mark the inner method as not transactional.",
    "Annotate fetchRates with @Transactional(propagation = NOT_SUPPORTED).",
    "Use REQUIRES_NEW propagation on the audit write instead.",
    "Set the propagation of `Rates.fetch` to NOT_SUPPORTED.",
    "Consider marking the client call @Async so it leaves the transaction.",
    "You should add @Retryable to the client method.",
    "Fix: wrap the handler in the retry middleware.",
    "Instead, register an interceptor that opens the session lazily.",
    "Call it through the proxy so the annotation applies.",
    "Decorate the view with @login_required.",
]

FRAMEWORK_CONTROLS = [
    "The method is annotated `@Transactional` and calls a slow HTTP client.",
    "`placeOrder` is `@Transactional`, so the connection is held during the call.",
    "Marking it NOT_SUPPORTED would not help because the caller's transaction is suspended.",
    "Do not rely on REQUIRES_NEW here.",
    "Do not use REQUIRES_NEW here.",
    "Add a null check before dividing.",
    "Move the HTTP call before the transaction starts.",
    "Add @Override to the method.",
    "Add a @param tag for cart.",
    "Use a timeout on the HTTP client.",
    "The interceptor was registered twice in this diff.",
]


def _finding(body: str = "plain body", title: str = "Slow call inside a transaction", **extra) -> Finding:
    values = {
        "file": ORDER_FILE, "line": 7, "severity": "warning", "confidence": 0.85,
        "title": title, "body": body,
    }
    values.update(extra)
    return Finding(**values)


def _check(*findings: Finding):
    return apply_guard_fix_check(list(findings), ORDER_FILES)


class TestFrameworkAdvice:
    @pytest.mark.parametrize("text", FRAMEWORK_ADVICE)
    def test_advice_gets_exactly_one_line(self, text):
        f = _finding(body=text)
        (out,), (reason,) = _check(f)
        assert reason == ("framework_fix_unverified",)
        assert out.body == f"{text}\n\n{FRAMEWORK_FIX_LINE}"
        assert out.body.count("Unverified fix:") == 1
        assert (out.severity, out.confidence, out.drop_reason) == (f.severity, f.confidence, None)

    @pytest.mark.parametrize("text", FRAMEWORK_CONTROLS)
    def test_a_report_or_plain_fix_is_the_same_object(self, text):
        f = _finding(body=text)
        (out,), (reason,) = _check(f)
        assert out is f and reason == ()

    def test_the_title_counts_too(self):
        f = _finding(title="Mark fetchRates @Transactional(propagation = NOT_SUPPORTED)", body="It is slow.")
        (out,), (reason,) = _check(f)
        assert reason == ("framework_fix_unverified",)
        assert out.body.endswith(FRAMEWORK_FIX_LINE)

    @pytest.mark.parametrize("text", FRAMEWORK_ADVICE[:3])
    def test_idempotent(self, text):
        once, _ = _check(_finding(body=text))
        twice, reasons = apply_guard_fix_check(once, ORDER_FILES)
        assert twice[0].body == once[0].body
        assert reasons == [("framework_fix_unverified",)]

    def test_a_model_written_line_gets_no_second_one(self):
        body = f"Unverified fix: the caller's transaction may still hold it. {CASE_TWO_BODY}"
        f = _finding(body=body)
        (out,), _ = _check(f)
        assert out is f

    def test_a_dropped_finding_is_untouched(self):
        f = _finding(body=CASE_TWO_BODY, drop_reason="hedged")
        (out,), (reason,) = _check(f)
        assert out is f and reason == ()

    def test_the_line_is_not_hedging(self):
        (out,) = apply_hedge_gate([_finding(body=f"{CASE_TWO_BODY}\n\n{FRAMEWORK_FIX_LINE}")])
        assert out.drop_reason is None

    def test_the_guard_line_wins_when_both_fire(self):
        text = "Drop the inline tenant check and annotate the endpoint with @PreAuthorize instead."
        f = _finding(body=text)
        (out,), (reason,) = _check(f)
        assert reason == ("advises_guard_removal", "framework_fix_unverified")
        assert out.body == f"{text}\n\n{GUARD_FIX_LINE}"

    def test_the_reasons_tuple_names_all_three(self):
        assert GUARD_FIX_REASONS == (
            "suggestion_drops_guard", "advises_guard_removal", "framework_fix_unverified",
        )


class TestFrameworkSuggestion:
    def test_a_suggestion_adding_an_annotation_is_kept_and_marked(self):
        text = (
            "    @Transactional(propagation = Propagation.NOT_SUPPORTED)\n"
            "    public Order placeOrder(Cart cart) {"
        )
        f = _finding(body="Hold no connection.", line=4, suggestion=text, suggestion_end_line=5)
        (out,), (reason,) = _check(f)
        assert reason == ("framework_fix_unverified",)
        assert out.suggestion == text and out.suggestion_end_line == 5
        assert out.body == f"Hold no connection.\n\n{FRAMEWORK_FIX_LINE}"

    def test_a_suggestion_keeping_the_same_annotation_is_the_same_object(self):
        text = "    @Transactional\n    public Order placeOrder(Cart cart) {"
        f = _finding(body="Rename nothing.", line=4, suggestion=text, suggestion_end_line=5)
        (out,), (reason,) = _check(f)
        assert out is f and reason == ()

    def test_a_suggestion_with_no_annotation_is_the_same_object(self):
        f = _finding(body="Price later.", line=8, suggestion="        order.price(rates, cart);")
        (out,), (reason,) = _check(f)
        assert out is f and reason == ()

    def test_an_override_annotation_is_not_framework_behaviour(self):
        text = "    @Override\n    public Order placeOrder(Cart cart) {"
        f = _finding(body="Mark the override.", line=5, suggestion=text, suggestion_end_line=5)
        (out,), (reason,) = _check(f)
        assert out is f and reason == ()


class TestPatterns:
    def test_the_patterns_have_a_true_and_a_false_example(self):
        assert quality._advises_framework_fix("Mark the inner method as not transactional.")
        assert not quality._advises_framework_fix("The inner method is marked as not transactional.")
        assert quality._framework_tokens("@Transactional(propagation = X)") == {"@transactional(propagation=x)"}
        assert quality._framework_tokens("@Override @param") == set()
        assert quality._framework_tokens("@Transactional") == {"@transactional"}


@pytest.mark.usefixtures("contract_stubs")
class TestThroughTheOrchestrator:
    def test_case_two_keeps_severity_and_gets_the_line(self):
        llm = FakeLLM({ORDER_FILE: [{
            "file": ORDER_FILE, "line": 7, "severity": "error", "confidence": 0.85,
            "title": "Slow external call inside a database transaction", "body": CASE_TWO_BODY,
        }]})
        forge = FakeForge(diff=ORDER_DIFF)
        res = orchestrator.orchestrate_review(forge, REF, llm, post=True, max_workers=1)
        (f,) = res["findings_active"]
        assert f.severity == "error" and f.confidence == 0.85
        assert f.body == f"{CASE_TWO_BODY}\n\n{FRAMEWORK_FIX_LINE}"
        assert not res["findings_dropped"]
        assert res["guard_fix"] == {"tagged": 1, "suggestions_cleared": 0}
        posted = [c.body for batch in forge.inline_batches for c in batch]
        assert len(posted) == 1 and FRAMEWORK_FIX_LINE in posted[0]

    def test_a_report_posts_unchanged_with_no_record_key(self):
        llm = FakeLLM({ORDER_FILE: [{
            "file": ORDER_FILE, "line": 7, "severity": "warning", "confidence": 0.85,
            "title": "Slow external call inside a database transaction",
            "body": "`placeOrder` is `@Transactional` and calls a slow HTTP client, so the "
                    "connection is held for the whole call.",
        }]})
        res = orchestrator.orchestrate_review(FakeForge(diff=ORDER_DIFF), REF, llm, post=False, max_workers=1)
        (f,) = res["findings_active"]
        assert "Unverified fix" not in f.body
        assert "guard_fix" not in res
