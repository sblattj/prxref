"""Issues #142 and #144: one root cause posts once, with the rest under "Also at".

The must-not-fold rows sit first because they decide whether the fold is safe.
Each acceptance row is one failure case from the issues. Bodies other than the
quoted titles are written the way a worker writes them: they name the code the
finding is about, and each side has words of its own.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from prxref import quality
from prxref.orchestrator import orchestrate_review
from prxref.quality import (
    ROOT_CAUSE_PREFIX,
    _also_at_paragraph,
    apply_quality_gate,
    apply_root_cause_fold,
    apply_rule_cap,
    apply_rule_grouping,
    apply_sweep_dedup,
)
from prxref.triage import Finding
from tests.test_orchestrator import REF, FakeForge, FakeLLM, multi_chunk_diff
from tests.test_orchestrator_rule_cap import _rules

EXC = "src/Exception.java"
SPEC = "api/openapi.yaml"
APP = "src/app.py"


def _f(file, line, title, body, *, severity="warning", confidence=0.9, rule=None, **kw):
    return Finding(
        file=file, line=line, severity=severity, confidence=confidence,
        title=title, body=body, rule=rule, **kw,
    )


def _fold(*findings, sweep_start=None):
    start = len(findings) if sweep_start is None else sweep_start
    return apply_root_cause_fold(findings, confidence_floor=None, sweep_start=start)


def _active(result):
    return [f for f in result if f.drop_reason is None]


def _dropped(result):
    return [f for f in result if f.drop_reason is not None]


def _swallowed_timeout(rule="error-handling"):
    return _f(
        APP, 12, "Catch block swallows `TimeoutException` from `client.fetch`",
        "`client.fetch` raises `TimeoutException` and the handler reads `response.body` "
        "as if the call worked, so an outage looks like an empty result.",
        rule=rule,
    )


def _skipped_close(rule="error-handling"):
    return _f(
        APP, 13, "`client.close` is skipped when `client.fetch` throws",
        "`client.close` runs after `client.fetch` and `response.body`, so a throw "
        "leaves the socket open. Move it to a finally block.",
        rule=rule,
    )


class TestMustNotFold:
    def test_a_two_different_defects_in_one_try_block_with_one_rule(self):
        result = _fold(_swallowed_timeout(), _skipped_close())
        assert len(_active(result)) == 2

    def test_a_the_same_pair_when_the_titles_name_no_code(self):
        result = _fold(
            _f(APP, 12, "Catch block hides the failure",
               "`client.fetch` can fail and the handler reads `response.body` anyway.",
               rule="error-handling"),
            _f(APP, 13, "Connection stays open after a failed call",
               "A failure in `client.fetch` skips the cleanup of `response.body`.",
               rule="error-handling"),
        )
        assert len(_active(result)) == 2

    def test_b_sql_injection_at_three_sibling_functions(self):
        body = "`{name}` builds its query with an f-string and passes it to `conn.execute`."
        result = _fold(*[
            _f(APP, line, "SQL injection via string interpolation", body.format(name=name),
               severity="error", rule="sql-safety")
            for line, name in ((9, "find_orders"), (15, "find_users"), (21, "find_items"))
        ])
        assert [f.line for f in _active(result)] == [9, 15, 21]

    def test_b_siblings_inside_the_window_stay_apart_on_their_own_names(self):
        body = "`{name}` builds its query with an f-string and passes it to `conn.execute`."
        result = _fold(*[
            _f(APP, line, "SQL injection via string interpolation", body.format(name=name),
               severity="error", rule="sql-safety")
            for line, name in ((9, "find_orders"), (11, "find_users"))
        ])
        assert len(_active(result)) == 2

    def test_b_siblings_naming_only_their_own_function_stay_apart(self):
        result = _fold(
            _f(APP, 9, "SQL injection via string interpolation",
               "`find_orders` builds its query with an f-string.", severity="error"),
            _f(APP, 11, "SQL injection via string interpolation",
               "`find_users` builds its query with an f-string.", severity="error"),
        )
        assert len(_active(result)) == 2

    def test_c_one_rule_in_two_files_about_unrelated_code(self):
        result = _fold(
            _f("src/billing.py", 4, "Handle the error from the invoice call",
               "The call can fail and nothing handles the failure, so the caller never learns.",
               rule="error-handling"),
            _f("src/search.py", 9, "Handle the error from the index call",
               "The call can fail and nothing handles the failure, so the caller never learns.",
               rule="error-handling"),
        )
        assert len(_active(result)) == 2

    def test_c_one_rule_in_two_files_sharing_one_common_call(self):
        result = _fold(
            _f("src/billing.py", 4, "Invoice total is logged in full",
               "`logger.info` writes `invoice_total` to the shared log.", rule="logging"),
            _f("src/search.py", 9, "Search term is logged in full",
               "`logger.info` writes `search_term` to the shared log.", rule="logging"),
        )
        assert len(_active(result)) == 2

    def test_c_one_pattern_in_two_files_is_two_sites_not_one_cause(self):
        result = _fold(
            _f("src/orders.py", 9, "SQL injection in the order lookup",
               "`find_orders` passes `user_input` to `conn.execute`.",
               severity="error", rule="sql-safety"),
            _f("src/users.py", 9, "SQL injection in the user lookup",
               "`find_users` passes `user_input` to `conn.execute`.",
               severity="error", rule="sql-safety"),
        )
        assert len(_active(result)) == 2

    def test_d_similar_title_outside_the_window(self):
        result = _fold(
            _f(APP, 40, "Extract repeated retry settings to constants",
               "`retry_count` is spelled out here."),
            _f(APP, 80, "Extract repeated retry settings to constants",
               "`backoff_ms` is spelled out here."),
        )
        assert [f.line for f in _active(result)] == [40, 80]

    def test_d_shared_code_outside_the_window(self):
        result = _fold(
            _f(APP, 40, "Extract repeated retry settings to constants",
               "`retry_count` and `backoff_ms` are spelled out here."),
            _f(APP, 80, "Extract repeated retry settings to constants",
               "`retry_count` and `backoff_ms` are spelled out here too."),
        )
        assert [f.line for f in _active(result)] == [40, 80]

    def test_two_bugs_on_one_expression_from_the_113_docstring(self):
        result = _fold(
            _f(APP, 6, "Nonce generator references undefined `request`",
               "`nonce_gen` reads `request.session` and `session_id`, and no `request` exists here."),
            _f(APP, 6, "Suggested nonce generator is derivable from the session cookie",
               "`nonce_gen` hashes `request.session` and `session_id`, which the client can read."),
        )
        assert len(_active(result)) == 2

    def test_a_deterministic_finding_is_never_folded_or_folded_into(self):
        check = _f(APP, 5, "Feature toggle `new_flow` pinned off",
                   "`new_flow` is `False` here. (deterministic check, no model)")
        model = _f(APP, 5, "Feature toggle `new_flow` pinned off",
                   "`new_flow` is `False` here, so the branch never runs.")
        assert len(_active(_fold(check, model))) == 2

    def test_a_sub_floor_finding_neither_folds_nor_absorbs(self):
        strong = _f(APP, 5, "Extract inline field-name literals to constants", "Same literal.")
        weak = _f(APP, 5, "Extract repeated field-name literals to constants", "Same literal.",
                  severity="error", confidence=0.2)
        result = _fold(strong, weak)
        assert len(_active(result)) == 2
        assert result[0] is strong and result[1] is weak

    def test_a_sweep_finding_is_left_to_the_sweep_dedup(self):
        chunk = _f(APP, 5, "Extract inline field-name literals to constants", "Same literal.")
        sweep = _f(APP, 5, "Extract repeated field-name literals to constants", "Same literal.")
        assert len(_active(_fold(chunk, sweep, sweep_start=1))) == 2

    def test_one_title_over_two_different_numbers_is_two_values(self):
        result = _fold(
            _f(APP, 20, "Magic number in data limit", "The data limit 42 is unexplained."),
            _f(APP, 22, "Magic number in data limit", "The data limit 43 is unexplained."),
        )
        assert [f.line for f in _active(result)] == [20, 22]

    def test_one_injection_at_two_call_sites_in_two_files_stays_two(self):
        result = _fold(
            _f("src/a.py", 10, "Store formula injection via unescaped item_code in filterExpr",
               "filterExpr({item_code}) allows filter manipulation.", severity="error"),
            _f("src/b.py", 20, "Formula interpolation of item_code allows filter manipulation",
               "item_code interpolated into filterExpr."),
        )
        assert len(_active(result)) == 2


def _over_limit_pair():
    return (
        _f(EXC, 10, "Use the same HTTP status for over-limit values across services",
           "`LimitExceededException` maps to `HttpStatus.CONFLICT` here while the other "
           "service answers 400, so clients must handle both.",
           severity="error"),
        _f(EXC, 10, "Over-limit value returns 409 here but 400 in the other service",
           "An over-limit value raises `LimitExceededException`, and `HttpStatus.CONFLICT` "
           "disagrees with the 400 from the peer service.",
           severity="error", confidence=0.8),
    )


def _seeder_pair():
    return (
        _f("src/Seeder.java", 30, "Guard startup seeding against concurrent replicas",
           "`Seeder.seed()` reads then inserts with no lock. Every replica runs it through "
           "`StartupRunner.run()`, so two replicas race. Take `seed_lock` first.",
           severity="error"),
        _f("src/StartupRunner.java", 12, "Startup run races with other replicas",
           "`StartupRunner.run()` calls `Seeder.seed()` on every replica at boot with no "
           "`seed_lock`, so inserts race.",
           confidence=0.8),
    )


def _route_pair():
    return (
        _f(SPEC, 88, "Removing `GET /v1/things` breaks existing clients",
           "The spec drops `GET /v1/things`, and the deleted `ThingsController` was its only handler.",
           severity="error"),
        _f("src/ThingsController.java", 0, "Deleted controller drops the `GET /v1/things` route",
           "`ThingsController` served `GET /v1/things`, and existing clients still call it."),
    )


def _literal_pair():
    return (
        _f("src/AdaptiveCheck.java", 20, "Use enum values for check values",
           'Both `AdaptiveCheck` and `BudgetCheck` compare against "adaptive" and "budget". '
           "Compare against an enum instead."),
        _f("src/BudgetCheck.java", 14, "Use enum values instead of inline literals",
           '`BudgetCheck` compares against "adaptive" and "budget". Use an enum.'),
    )


class TestAcceptance:
    def test_1_two_reworded_findings_at_one_line_from_different_chunks(self):
        result = _fold(*_over_limit_pair())
        (kept,) = _active(result)
        assert (kept.file, kept.line, kept.severity) == (EXC, 10, "error")

    def test_1_with_bodies_that_name_no_code(self):
        result = _fold(
            _f(EXC, 10, "Use the same HTTP status for over-limit values across services",
               "This answers 409 where the peer answers 400.", severity="error"),
            _f(EXC, 10, "Over-limit value returns 409 here but 400 in the other service",
               "Clients have to special-case the two answers.", severity="error"),
        )
        assert len(_active(result)) == 1

    def test_2_field_name_literals_two_lines_apart(self):
        result = _fold(
            _f(APP, 174, "Extract inline field-name literals to constants",
               '"fieldName" is written inline.', severity="outofscope"),
            _f(APP, 176, "Extract repeated field-name literals to constants",
               '"fieldName" and "fieldType" repeat through this block.', severity="outofscope"),
        )
        (kept,) = _active(result)
        assert kept.line == 174
        assert kept.locations == ((APP, 176),)

    @pytest.mark.parametrize("subject", ["`quotaLimit`", "Quota limit"])
    def test_3_one_contradiction_three_lines_apart(self, subject):
        result = _fold(
            _f(SPEC, 1384, f"{subject} contradicts the other API",
               "`quotaLimit` is capped at `maxItems` 50 here and 100 in the other spec."),
            _f(SPEC, 1387, f"Reconcile {subject} across specs",
               "The other spec caps `quotaLimit` with `maxItems` 100, this one with 50."),
        )
        (kept,) = _active(result)
        assert kept.locations == ((SPEC, 1387),)

    def test_4_callee_and_caller(self):
        result = _fold(*_seeder_pair())
        (kept,) = _active(result)
        assert (kept.file, kept.line) == ("src/Seeder.java", 30)
        assert kept.locations == (("src/StartupRunner.java", 12),)
        assert "Also at: `src/StartupRunner.java:12`" in kept.body

    def test_5_removed_route_on_the_spec_and_the_deleted_controller(self):
        result = _fold(*_route_pair())
        (kept,) = _active(result)
        assert (kept.file, kept.line) == (SPEC, 88)
        assert kept.locations == (("src/ThingsController.java", 0),)
        assert "Also at: `src/ThingsController.java`" in kept.body

    def test_6_shared_literals_in_two_classes(self):
        result = _fold(*_literal_pair())
        (kept,) = _active(result)
        assert kept.locations == (("src/BudgetCheck.java", 14),) or kept.locations == (
            ("src/AdaptiveCheck.java", 20),
        )

    def test_7_yaml_property_and_its_enum_line(self):
        result = _fold(
            _f(SPEC, 1053, "Reuse the `ThingStatus` schema instead of an inline enum",
               "This property re-declares `ThingStatus` inline. Point it at "
               "`components.schemas.ThingStatus`."),
            _f(SPEC, 1055, "Inline enum duplicates `ThingStatus`",
               "The values here repeat `ThingStatus`. Use `components.schemas.ThingStatus`."),
        )
        (kept,) = _active(result)
        assert kept.locations in (((SPEC, 1055),), ((SPEC, 1053),))

    @pytest.mark.parametrize("first_severe", [True, False])
    def test_8_the_kept_finding_has_the_higher_severity(self, first_severe):
        a, b = _seeder_pair()
        pair = (a, b) if first_severe else (b, a)
        (kept,) = _active(_fold(*pair))
        assert kept.severity == "error"

    def test_8_the_kept_finding_also_takes_the_higher_confidence(self):
        result = _fold(
            _f(APP, 30, "Extract inline field-name literals to constants",
               "Same literal.", severity="warning", confidence=0.95),
            _f(APP, 31, "Extract repeated field-name literals to constants",
               "Same literal.", severity="error", confidence=0.7),
        )
        (kept,) = _active(result)
        assert (kept.line, kept.severity, kept.confidence) == (31, "error", 0.95)

    def test_9_the_dropped_location_is_listed_and_the_drop_is_auditable(self):
        result = _fold(*_seeder_pair())
        (kept,) = _active(result)
        (gone,) = _dropped(result)
        assert kept.body.endswith(_also_at_paragraph(((gone.file, gone.line),)))
        assert gone.drop_reason.startswith(f"{ROOT_CAUSE_PREFIX}src/Seeder.java:30 (")
        assert "seeder.seed" in gone.drop_reason

    def test_9_a_folded_member_passes_on_locations_it_already_lists(self):
        a, b = _seeder_pair()
        listed = ("src/Elsewhere.java", 3)
        result = _fold(a, replace(b, locations=(listed,)))
        (kept,) = _active(result)
        assert set(kept.locations) == {("src/StartupRunner.java", 12), listed}


class TestSemantics:
    def test_the_most_specific_location_is_kept_over_a_higher_severity_file_level_copy(self):
        spec, controller = _route_pair()
        file_level_error = replace(controller, severity="error")
        line_warning = replace(spec, severity="warning")
        (kept,) = _active(_fold(file_level_error, line_warning))
        assert (kept.file, kept.line, kept.severity) == (SPEC, 88, "error")

    def test_a_tie_on_every_rank_breaks_on_content_not_arrival(self):
        a, b = _over_limit_pair()
        a = replace(a, confidence=0.9)
        b = replace(b, confidence=0.9)
        first = _fold(a, b)
        second = _fold(b, a)
        assert [f for f in first if f.drop_reason is None] == [
            f for f in second if f.drop_reason is None
        ]
        assert _active(first)[0].title == b.title

    def test_length_order_and_untouched_identity_hold(self):
        lone = _f("src/z.py", 1, "A lone finding", "Nothing else is like it.")
        a, b = _seeder_pair()
        result = _fold(lone, a, b)
        assert len(result) == 3
        assert result[0] is lone
        assert [f.file for f in result] == [lone.file, a.file, b.file]

    def test_a_dropped_copy_never_drops_a_third(self):
        a, b = _seeder_pair()
        third = _f("src/Boot.java", 2, "Startup run races with other replicas",
                   "`StartupRunner.run()` calls `Seeder.seed()` on every replica with no `seed_lock`.")
        result = _fold(a, b, third)
        assert len(_active(result)) == 1
        assert all(
            f.drop_reason.startswith(f"{ROOT_CAUSE_PREFIX}src/Seeder.java:30") for f in _dropped(result)
        )
        assert set(_active(result)[0].locations) == {("src/StartupRunner.java", 12), ("src/Boot.java", 2)}

    def test_a_token_in_four_claims_is_a_pattern_and_does_not_bind(self):
        result = _fold(*[
            _f(f"src/m{n}.py", 3, "Connection pool settings are hard to reuse",
               "`pool_size` and `pool_timeout` are read inline here.", rule="config")
            for n in range(4)
        ])
        assert len(_active(result)) == 4

    def test_a_cross_file_root_cause_reported_three_times_folds(self):
        result = _fold(*[
            _f(f"src/m{n}.py", 3, "Connection pool settings are hard to reuse",
               "`pool_size` and `pool_timeout` are read inline here.", rule="config")
            for n in range(3)
        ])
        assert len(_active(result)) == 1

    def test_the_same_remedy_is_a_cross_file_signal(self):
        result = _fold(
            _f("src/a.py", 3, "Cursor closes too late",
               "`open_cursor` and `close_cursor` straddle the commit.",
               suggestion="with open_cursor() as cur:\n    close_cursor(cur)"),
            _f("src/b.py", 3, "Commit happens before cleanup",
               "`open_cursor` and `close_cursor` straddle the commit.",
               suggestion="with  open_cursor() as cur:\n    close_cursor(cur)"),
        )
        assert len(_active(result)) == 1

    def test_a_different_remedy_is_no_signal(self):
        result = _fold(
            _f("src/a.py", 3, "Cursor closes too late",
               "`open_cursor` and `close_cursor` straddle the commit.",
               suggestion="close_cursor(cur)"),
            _f("src/b.py", 3, "Commit happens before cleanup",
               "`open_cursor` and `close_cursor` straddle the commit.",
               suggestion="open_cursor(timeout=1)"),
        )
        assert len(_active(result)) == 2


class TestItsNeighbours:
    def test_a_repeat_does_not_spend_the_per_rule_cap(self):
        a, b = _seeder_pair()
        rule_a = replace(a, rule="startup")
        rule_b = replace(b, rule="startup")
        other = _f("src/Other.java", 7, "Another startup defect entirely",
                   "`other_step` runs before `config_ready`.", rule="startup")
        folded = _fold(rule_a, rule_b, other)
        capped = apply_rule_cap(folded, cap=2, confidence_floor=None, sweep_start=3)
        assert sorted(f.file for f in _active(capped)) == ["src/Other.java", "src/Seeder.java"]
        assert all(f.drop_reason.startswith(ROOT_CAUSE_PREFIX) for f in _dropped(capped))

    def test_the_cap_keeps_a_folded_location_and_writes_one_also_at(self):
        spec, controller = _route_pair()
        rule_spec = replace(spec, rule="api")
        rule_controller = replace(controller, rule="api")
        extra = _f("src/Other.java", 7, "Another API defect entirely",
                   "`other_step` runs before `config_ready`.", rule="api", confidence=0.7)
        folded = _fold(rule_spec, rule_controller, extra)
        capped = apply_rule_cap(folded, cap=1, confidence_floor=None, sweep_start=3)
        (kept,) = _active(capped)
        assert set(kept.locations) == {("src/ThingsController.java", 0), ("src/Other.java", 7)}
        assert kept.body.count("Also at:") == 1
        assert "`src/ThingsController.java`" in kept.body and "`src/Other.java:7`" in kept.body

    def test_grouping_keeps_a_folded_location_and_writes_one_also_at(self):
        a, b = _seeder_pair()
        rule_a = replace(a, rule="startup")
        rule_b = replace(b, rule="startup")
        same_file = _f("src/Seeder.java", 90, "Another seeding defect", "`other_step` is unchecked.",
                       rule="startup")
        folded = _fold(rule_a, rule_b, same_file)
        grouped = apply_rule_grouping(folded, confidence_floor=None, sweep_start=3)
        (kept,) = _active(grouped)
        assert set(kept.locations) == {("src/StartupRunner.java", 12), ("src/Seeder.java", 90)}
        assert kept.body.count("Also at:") == 1
        assert "`src/StartupRunner.java:12`" in kept.body and "`src/Seeder.java:90`" in kept.body

    def test_a_sweep_restatement_of_a_folded_member_is_still_deduped(self):
        a, b = _seeder_pair()
        sweep = _f(b.file, 99, b.title, "The sweep saw the same race.")
        folded = _fold(a, b, sweep, sweep_start=2)
        out = apply_sweep_dedup(folded, sweep_start=2)
        assert out[2].drop_reason == "duplicate of chunk finding"

    def test_the_gate_counts_the_kept_finding_once(self):
        a, b = _over_limit_pair()
        gated = apply_quality_gate(_fold(a, b), confidence_floor=None, max_errors=1)
        assert len(_active(gated)) == 1
        assert not any("error cap" in (f.drop_reason or "") for f in gated)


def _raw(file, line, title, body, severity="warning", **kw):
    return {
        "file": file, "line": line, "severity": severity, "confidence": 0.9,
        "title": title, "body": body, **kw,
    }


@pytest.mark.usefixtures("contract_stubs")
class TestThroughTheOrchestrator:
    """The real pipeline: chunking, every pass and posting, with a fake model and forge."""

    B1 = "src/big1.py"
    B2 = "src/big2.py"

    def _review(self, by_path):
        diff = multi_chunk_diff(2)
        forge = FakeForge(diff=diff)
        res = orchestrate_review(forge, REF, FakeLLM(by_path), post=True, max_workers=1)
        return res, forge

    def test_144_two_chunks_report_one_line_twice(self):
        res, forge = self._review({
            self.B1: [_raw(
                self.B1, 10, "Use the same HTTP status for over-limit data across services",
                "`LimitExceededException` maps `data` to `HttpStatus.CONFLICT` here.", "error",
            )],
            self.B2: [_raw(
                self.B1, 10, "Over-limit data returns 409 here but 400 in the other service",
                "`LimitExceededException` sends `data` to `HttpStatus.CONFLICT` and the peer says 400.",
                "error",
            )],
        })
        assert [(f.file, f.line) for f in res["findings_active"]] == [(self.B1, 10)]
        (gone,) = res["findings_dropped"]
        assert gone.drop_reason.startswith(f"{ROOT_CAUSE_PREFIX}{self.B1}:10 (")
        assert sum(len(batch) for batch in forge.inline_batches) == 1

    def test_142_two_chunks_report_one_root_cause_in_two_files(self):
        res, forge = self._review({
            self.B1: [_raw(
                self.B1, 30, "Guard startup seeding against concurrent replicas",
                "`Seeder.seed()` reads `data` with no lock and `StartupRunner.run()` calls it "
                "on every replica, so two replicas race.", "error",
            )],
            self.B2: [_raw(
                self.B2, 12, "Startup run races with other replicas",
                "`StartupRunner.run()` calls `Seeder.seed()` on `data` at boot, so inserts race.",
            )],
        })
        (kept,) = res["findings_active"]
        assert (kept.file, kept.line, kept.severity) == (self.B1, 30, "error")
        assert f"`{self.B2}:12`" in kept.body
        (gone,) = res["findings_dropped"]
        assert gone.drop_reason.startswith(f"{ROOT_CAUSE_PREFIX}{self.B1}:30 (")
        (comment,) = [c for batch in forge.inline_batches for c in batch]
        assert f"`{self.B2}:12`" in comment.body

    def test_the_pass_runs_before_the_per_rule_cap(self, monkeypatch):
        seen = []
        real = quality.apply_rule_cap

        def spy(findings, **kw):
            seen.append([f.drop_reason for f in findings])
            return real(findings, **kw)

        monkeypatch.setattr("prxref.orchestrator.apply_rule_cap", spy)
        res = orchestrate_review(
            FakeForge(diff=multi_chunk_diff(2)), REF,
            FakeLLM({
                self.B1: [_raw(
                    self.B1, 30, "Guard startup seeding against concurrent replicas",
                    "`Seeder.seed()` reads `data` and `StartupRunner.run()` calls it, so replicas race.",
                    rule="startup",
                )],
                self.B2: [_raw(
                    self.B2, 12, "Startup run races with other replicas",
                    "`StartupRunner.run()` calls `Seeder.seed()` on `data`, so replicas race.",
                    rule="startup",
                )],
            }),
            post=False, max_workers=1, max_findings_per_rule=1,
            rules=_rules(),
        )
        assert len(res["findings_active"]) == 1
        assert seen, "the cap never ran, so the ordering check is vacuous"
        assert any(reason and reason.startswith(ROOT_CAUSE_PREFIX) for reason in seen[0])
        assert res["findings_dropped"][0].drop_reason.startswith(ROOT_CAUSE_PREFIX)

    def test_three_siblings_through_the_pipeline_all_post(self):
        def row(line, name):
            return _raw(
                self.B1, line, "SQL injection via string interpolation",
                f"`{name}` builds its query from `data` with an f-string for `conn.execute`.",
                "error", rule="sql-safety",
            )

        res, forge = self._review({
            self.B1: [row(9, "find_orders"), row(15, "find_users"), row(21, "find_items")],
        })
        assert [f.line for f in res["findings_active"]] == [9, 15, 21]
        assert sum(len(batch) for batch in forge.inline_batches) == 3
