"""Tests for prxref.quality: line alignment, thread dedup, and quality gate."""
from __future__ import annotations

import itertools
import logging

import pytest

from prxref.forges.base import Thread
from prxref.quality import (
    _body_cited_lines,
    _resolve_max_errors,
    active,
    apply_hedge_gate,
    apply_line_align,
    apply_location_validation,
    apply_manifest_claim_check,
    apply_quality_gate,
    apply_removal_claim_check,
    apply_settled_thread_suppression,
    apply_severity_consistency,
    apply_thread_dedup,
    finding_rank_key,
    finding_sort_key,
    is_duplicate_of_existing,
    normalize_title,
    snap_line,
)
from prxref.triage import Finding, added_lines_by_file, parse_unified_diff


def _f(**kwargs) -> Finding:
    defaults = {
        "file": "src/app.py",
        "line": 10,
        "severity": "warning",
        "confidence": 0.8,
        "title": "Possible bug",
        "body": "Details about the finding.",
    }
    defaults.update(kwargs)
    return Finding(**defaults)


class TestSnapLine:
    def test_line_in_added_stays_unchanged(self):
        assert snap_line(5, {3, 5, 8}) == 5

    def test_snaps_to_nearest_within_tolerance(self):
        # Line 6 with added {8} and tolerance 3 (distance 2) -> snaps to 8
        assert snap_line(6, {8}) == 8

    def test_drops_to_file_level_when_beyond_tolerance(self):
        # Line 6 with added {20} and tolerance 3 (distance 14) -> 0
        assert snap_line(6, {20}) == 0

    def test_drops_to_file_level_when_no_added_lines(self):
        assert snap_line(10, set()) == 0


class TestApplyLocationValidation:
    """Issue #32: ``file: "package."`` rendered as ``- 🟧 `package.:—```.
    A location that names no path of the diff is not a review; it drops."""

    def test_a_file_in_the_diff_is_never_dropped(self):
        finding = _f(file="src/app.py")
        result = apply_location_validation([finding], ["src/app.py", "other.py"])
        assert result[0] is finding
        assert result[0].drop_reason is None

    def test_an_empty_file_field_is_dropped(self):
        result = apply_location_validation([_f(file="")], ["src/app.py"])
        assert result[0].drop_reason == "malformed location: ''"

    def test_a_non_path_shape_is_dropped(self):
        result = apply_location_validation([_f(file="package.")], ["src/app.py"])
        assert result[0].drop_reason == "malformed location: 'package.'"

    def test_a_plausible_path_missing_from_the_diff_is_dropped(self):
        result = apply_location_validation([_f(file="src/ghost.py")], ["src/app.py"])
        assert result[0].drop_reason == "malformed location: 'src/ghost.py'"

    def test_already_dropped_findings_keep_their_reason(self):
        finding = _f(file="src/ghost.py", drop_reason="confidence 0.10 below floor 0.60")
        result = apply_location_validation([finding], ["src/app.py"])
        assert result[0].drop_reason == "confidence 0.10 below floor 0.60"

    def test_the_input_is_not_mutated(self):
        finding = _f(file="package.")
        apply_location_validation([finding], ["src/app.py"])
        assert finding.drop_reason is None

    def test_survivors_and_drops_keep_their_order(self):
        findings = [_f(file="src/app.py"), _f(file="package."), _f(file="src/app.py")]
        result = apply_location_validation(findings, ["src/app.py"])
        assert [f.drop_reason for f in result] == [
            None, "malformed location: 'package.'", None,
        ]


class TestApplyLineAlign:
    def test_aligns_across_files(self):
        findings = [
            _f(file="a.py", line=10),
            _f(file="b.py", line=15),
        ]
        added = {
            "a.py": {12},       # distance 2 <= 3 -> snaps to 12
            "b.py": {50},       # distance 35 > 3 -> drops to 0
        }
        aligned = apply_line_align(findings, added)
        assert aligned[0].line == 12
        assert aligned[1].line == 0
        # Originals untouched
        assert findings[0].line == 10
        assert findings[1].line == 15

    def test_preserves_finding_identity_when_unmodified(self):
        finding = _f(file="a.py", line=10)
        aligned = apply_line_align([finding], {"a.py": {10}})
        assert aligned[0] is finding


# The issue #19 drift shape: a multi-hunk file where the model adds the wrong
# hunk's @@ start to an in-hunk offset. The emitted number is a real added
# line — of the wrong hunk — so membership validation keeps it, and the posted
# anchor lands ~170 lines from the code the finding describes.
MULTI_HUNK_DIFF = """\
diff --git a/src/functions.php b/src/functions.php
--- a/src/functions.php
+++ b/src/functions.php
@@ -1260,1 +1260,5 @@ function handle_request(array $input): void {
     $stmt = $pdo->prepare('SELECT * FROM users WHERE id = :id');
+    if (!array_key_exists('user_id', $input)) {
+        throw new InvalidArgumentException('user_id missing');
+    }
+    $stmt->execute(['id' => $input['user_id']]);
@@ -1425,2 +1428,4 @@ function cache_prune(): void {
     $keys = array_keys($this->items);
+    $ttl = $this->ttl;
+    $this->gc->collect();
"""


class TestHunkAwareAlignment:
    """Exact added-line members are corroborated (or re-resolved) by content.

    Membership alone cannot catch wrong-hunk citations, so when the parsed
    hunks are supplied, a member anchor whose hunk shares no tokens with the
    finding's title+body is refuted and re-resolved to the best token-
    matching added line elsewhere in the file — or to file-level (0) when
    nothing matches.
    """

    def _aligned(self, **kwargs) -> list[Finding]:
        parsed = parse_unified_diff(MULTI_HUNK_DIFF)
        defaults = {
            "file": "src/functions.php",
            "line": 1429,
            "title": "user_id used without a guard",
            "body": (
                "The added code reads $input['user_id'] without an "
                "array_key_exists or isset check; add the "
                "InvalidArgumentException guard here."
            ),
        }
        defaults.update(kwargs)
        finding = _f(**defaults)
        return apply_line_align(
            [finding],
            added_lines_by_file(parsed),
            files=parsed,
        )

    def test_wrong_hunk_member_is_reresolved_by_content(self):
        aligned = self._aligned()
        # 1262 (the InvalidArgumentException throw) now outranks 1261: the
        # claim's most specific evidence token lives on the throw line.
        assert aligned[0].line == 1262

    def test_refuted_member_with_no_match_anywhere_drops_to_file_level(self):
        aligned = self._aligned(
            title="Unhandled edge",
            body="This branch can crash when empty.",
        )
        assert aligned[0].line == 0

    def test_corroborated_member_is_kept(self):
        aligned = self._aligned(
            title="ttl not applied to gc sweep",
            body="cache_prune reads ttl but never passes it to collect().",
        )
        assert aligned[0].line == 1429

    def test_alignment_without_files_keeps_membership_behavior(self):
        finding = _f(file="src/functions.php", line=1429)
        aligned = apply_line_align(
            [finding], {"src/functions.php": {1429, 1261}}
        )
        assert aligned[0].line == 1429

    def test_file_level_citation_stays_file_level(self):
        finding = _f(file="src/functions.php", line=0)
        parsed = parse_unified_diff(MULTI_HUNK_DIFF)
        aligned = apply_line_align(
            [finding], added_lines_by_file(parsed), files=parsed
        )
        assert aligned[0].line == 0


class TestSnapTolerance:
    def test_distance_four_snaps_at_default_tolerance(self):
        assert snap_line(1426, {1430}) == 1430

    def test_distance_six_drops_to_file_level(self):
        assert snap_line(1424, {1430}) == 0


class TestThreadDedup:
    def test_exact_line_single_distinctive_token_dedupes(self):
        finding = _f(
            file="a.ts",
            line=10,
            title="Possible cache invalidation race",
            body="Concurrent calls may corrupt state.",
        )
        threads = [
            Thread(
                path="a.ts",
                line=10,
                resolved=False,
                author="reviewer",
                body_snippet="Is the invalidation atomic?",
            )
        ]
        assert is_duplicate_of_existing(finding, threads)

    def test_nearby_line_single_token_does_not_dedupe(self):
        # Single shared token at distance 2 is not enough (requires min_shared_tokens=2)
        finding = _f(
            file="a.ts",
            line=10,
            title="Possible cache invalidation race",
            body="Concurrent calls may corrupt state.",
        )
        threads = [
            Thread(
                path="a.ts",
                line=12,
                resolved=False,
                author="reviewer",
                body_snippet="Is the invalidation atomic?",
            )
        ]
        assert not is_duplicate_of_existing(finding, threads)

    def test_nearby_line_two_tokens_dedupes(self):
        finding = _f(
            file="a.ts",
            line=10,
            title="Cache invalidation concurrency race",
            body="",
        )
        threads = [
            Thread(
                path="a.ts",
                line=12,
                resolved=False,
                author="reviewer",
                body_snippet="Is the invalidation atomic under concurrency?",
            )
        ]
        assert is_duplicate_of_existing(finding, threads)

    def test_different_path_never_dedupes(self):
        finding = _f(file="a.ts", line=10, title="Double cast", body="")
        threads = [
            Thread(
                path="b.ts",
                line=10,
                resolved=False,
                author="reviewer",
                body_snippet="Double cast here.",
            )
        ]
        assert not is_duplicate_of_existing(finding, threads)

    def test_programming_keywords_do_not_drive_false_dedup(self):
        finding = _f(
            file="src/api.ts",
            line=10,
            title="Promise rejection unhandled",
            body="Async function returns Promise without await.",
        )
        threads = [
            Thread(
                path="src/api.ts",
                line=12,
                resolved=False,
                author="reviewer",
                body_snippet="Async function returns void instead of Promise.",
            )
        ]
        assert not is_duplicate_of_existing(finding, threads)

    def test_apply_thread_dedup_populates_drop_reason(self):
        f1 = _f(file="a.ts", line=10, title="Double cast unknown", body="")
        f2 = _f(file="b.ts", line=5, title="Null pointer risk", body="")
        threads = [
            Thread(
                path="a.ts",
                line=10,
                resolved=False,
                author="reviewer",
                body_snippet="The double cast unknown here.",
            )
        ]
        result = apply_thread_dedup([f1, f2], threads)
        assert len(result) == 2
        assert result[0].drop_reason == "duplicate of existing thread"
        assert result[1].drop_reason is None
        assert active(result) == [f2]


class TestSeverityConsistency:
    def test_same_file_same_title_warning_and_note_both_raise_to_warning(self):
        # The live issue #18 case: per-chunk workers flagged the same
        # read-modify-write pattern warning at one anchor, note at another.
        warning = _f(
            file="src/supabase.ts", line=42, severity="warning", confidence=0.8,
            title="Race when updating config",
            body="Read-modify-write is not atomic.",
        )
        note = _f(
            file="src/supabase.ts", line=88, severity="outofscope", confidence=0.7,
            title="`race` when updating config.",
            body="Same pattern, softer call.",
        )
        result = apply_severity_consistency([warning, note])
        assert result[0].severity == "warning"
        assert result[1].severity == "warning"

    def test_rewritten_finding_keeps_its_own_identity(self):
        warning = _f(
            file="src/supabase.ts", line=42, severity="warning", confidence=0.8,
            title="Race when updating config", body="Own body.",
        )
        note = _f(
            file="src/supabase.ts", line=88, severity="outofscope", confidence=0.7,
            title="`race` when updating config.", body="Other body.",
        )
        result = apply_severity_consistency([warning, note])
        assert (result[1].file, result[1].line) == ("src/supabase.ts", 88)
        assert result[1].body == "Other body."
        assert result[1].confidence == 0.7
        assert result[1].drop_reason is None
        # Unmodified finding keeps object identity, matching the other passes
        assert result[0] is warning

    def test_cross_file_same_title_error_and_warning_both_error(self):
        error = _f(
            file="functions/a.ts", line=10, severity="error", confidence=0.9,
            title="Hardcoded secret in serverless handler", body="",
        )
        warning = _f(
            file="functions/b.ts", line=10, severity="warning", confidence=0.8,
            title="hardcoded secret in serverless handler", body="",
        )
        result = apply_severity_consistency([error, warning])
        assert result[0].severity == "error"
        assert result[1].severity == "error"

    def test_different_titles_never_merge(self):
        # One title mentioning the other's words is still a different pattern
        short = _f(severity="warning", confidence=0.8, title="Null deref")
        long = _f(
            severity="outofscope", confidence=0.7,
            title="Null deref in the config loader fallback path",
        )
        result = apply_severity_consistency([short, long])
        assert result[0].severity == "warning"
        assert result[1].severity == "outofscope"

    def test_dropped_findings_neither_join_nor_get_rewritten(self):
        dropped = _f(severity="error", confidence=0.9, title="Shared title",
                     drop_reason="duplicate of existing thread")
        note = _f(severity="outofscope", confidence=0.7, title="Shared title")
        result = apply_severity_consistency([dropped, note])
        assert result[0] is dropped
        assert result[0].severity == "error"
        assert result[1].severity == "outofscope"

    def test_backtick_and_punctuation_variants_normalize_together(self):
        assert normalize_title("`foo` bar") == normalize_title("foo bar")
        assert normalize_title('"Quoted" title!') == normalize_title("quoted title")
        assert normalize_title("  Mixed   CASE  ") == "mixed case"


class TestSeverityTokenGrouping:
    def test_shared_rare_token_binds_different_phrasings_across_files(self):
        # The live issue #30 shape (v0.10.1): the same unescaped Airtable
        # interpolation bug, two phrasings, two files. Title equality (#18)
        # cannot bind these; the shared rare tokens + common injection
        # class must.
        error = _f(
            file="src/airtable-video-processor.ts", line=80, severity="error",
            confidence=0.9,
            title="Airtable formula injection via unescaped vimeo_code in filterByFormula",
            body="filterByFormula({vimeo_code}) lets a crafted code break out of the quote.",
        )
        warning = _f(
            file="src/get-video-feedbacks.ts", line=72, severity="warning",
            confidence=0.8,
            title="Formula interpolation of vimeo_code allows filter manipulation",
            body="vimeo_code is interpolated into filterByFormula without escaping.",
        )
        result = apply_severity_consistency([error, warning])
        assert result[0].severity == "error"
        assert result[1].severity == "error"

    def test_shared_token_different_problem_classes_never_merges(self):
        inject = _f(
            file="src/deploy.ts", line=10, severity="error", confidence=0.9,
            title="Command injection building the deploy_script shell command",
            body="child_process.exec concatenates deploy_script with user input.",
        )
        ratelimit = _f(
            file="src/hooks.ts", line=20, severity="warning", confidence=0.8,
            title="Missing rate limit on the deploy_script webhook",
            body="deploy_script triggers fire without throttling.",
        )
        result = apply_severity_consistency([inject, ratelimit])
        assert result[0].severity == "error"
        assert result[1].severity == "warning"

    def test_shared_generic_token_never_merges(self):
        # `handler` is on the code-token stopword list: a ubiquitous name
        # cannot bind findings even when the guard would otherwise pass.
        f1 = _f(
            file="src/upload.ts", line=5, severity="error", confidence=0.9,
            title="Missing rate limit on upload route",
            body="The `handler` accepts unlimited uploads.",
        )
        f2 = _f(
            file="src/export.ts", line=5, severity="warning", confidence=0.8,
            title="Missing rate limit on export route",
            body="The `handler` loops over every record.",
        )
        result = apply_severity_consistency([f1, f2])
        assert result[0].severity == "error"
        assert result[1].severity == "warning"

    def test_token_in_three_or_more_findings_is_not_rare(self):
        findings = [
            _f(
                file=f"f{i}.ts", line=1, severity=sev, confidence=0.8,
                title=f"Formula injection via vimeo_code in filter {i}",
                body="vimeo_code interpolated raw.",
            )
            for i, sev in enumerate(["error", "warning", "outofscope"])
        ]
        result = apply_severity_consistency(findings)
        assert [f.severity for f in result] == ["error", "warning", "outofscope"]

    def test_token_in_three_findings_still_merges_via_title_rule(self):
        shared = "Formula injection via vimeo_code in filterByFormula"
        err = _f(file="f1.ts", line=1, severity="error", confidence=0.9,
                 title=shared, body="raw.")
        warn = _f(file="f2.ts", line=1, severity="warning", confidence=0.8,
                  title=shared, body="raw.")
        third = _f(
            file="f3.ts", line=1, severity="outofscope", confidence=0.7,
            title="vimeo_code reused for cache keys",
            body="unrelated rate limit note.",
        )
        result = apply_severity_consistency([err, warn, third])
        assert result[0].severity == "error"
        assert result[1].severity == "error"
        assert result[2].severity == "outofscope"

    def test_token_groups_are_transitive(self):
        # A shares vimeo_code with B, B shares sort_field with C, A and C
        # share nothing directly: the component still groups all three.
        a = _f(
            file="f1.ts", line=1, severity="error", confidence=0.9,
            title="Formula injection via unescaped vimeo_code",
            body="vimeo_code builds the filter string.",
        )
        b = _f(
            file="f2.ts", line=1, severity="warning", confidence=0.8,
            title="Formula injection via unescaped vimeo_code and sort_field",
            body="vimeo_code and sort_field are concatenated.",
        )
        c = _f(
            file="f3.ts", line=1, severity="outofscope", confidence=0.7,
            title="Formula injection through sort_field interpolation",
            body="sort_field interpolated raw.",
        )
        result = apply_severity_consistency([a, b, c])
        assert all(f.severity == "error" for f in result)

    def test_same_file_sharing_rare_token_merges_without_class_keyword(self):
        f1 = _f(
            file="src/checkout.ts", line=10, severity="error", confidence=0.9,
            title="checkout_session_id persists after the tenant is deleted",
            body="stale row lingers.",
        )
        f2 = _f(
            file="src/checkout.ts", line=40, severity="warning", confidence=0.8,
            title="checkout_session_id may exceed the schema limit",
            body="no guard before insert.",
        )
        result = apply_severity_consistency([f1, f2])
        assert result[0].severity == "error"
        assert result[1].severity == "error"

    def test_shared_rare_token_alone_without_same_file_or_class_never_merges(self):
        f1 = _f(
            file="src/a.ts", line=10, severity="error", confidence=0.9,
            title="checkout_session_id persists after the tenant is deleted",
            body="stale row.",
        )
        f2 = _f(
            file="src/b.ts", line=40, severity="warning", confidence=0.8,
            title="checkout_session_id may exceed the schema limit",
            body="no guard.",
        )
        result = apply_severity_consistency([f1, f2])
        assert result[0].severity == "error"
        assert result[1].severity == "warning"

    def test_rewritten_via_token_keeps_identity_and_logs_binding_tokens(self, caplog):
        from prxref.quality import logger as quality_logger

        error = _f(
            file="src/a.ts", line=80, severity="error", confidence=0.9,
            title="Airtable formula injection via unescaped vimeo_code",
            body="filterByFormula({vimeo_code}).",
        )
        warning = _f(
            file="src/b.ts", line=72, severity="warning", confidence=0.8,
            title="Formula interpolation of vimeo_code allows filter manipulation",
            body="vimeo_code interpolated.",
        )
        with caplog.at_level(logging.INFO, logger=quality_logger.name):
            result = apply_severity_consistency([error, warning])
        assert result[1].severity == "error"
        assert (result[1].file, result[1].line, result[1].body) == ("src/b.ts", 72, "vimeo_code interpolated.")
        assert result[1].confidence == 0.8
        lines = [r for r in caplog.records if r.name == quality_logger.name]
        assert len(lines) == 1
        assert "1 finding(s)" in lines[0].getMessage()
        assert "vimeo_code" in lines[0].getMessage()

    def test_no_token_rewrites_logs_nothing(self, caplog):
        from prxref.quality import logger as quality_logger

        f1 = _f(file="src/a.ts", line=1, severity="error", confidence=0.9,
                title="Command injection in deploy_script", body="")
        f2 = _f(file="src/b.ts", line=2, severity="warning", confidence=0.8,
                title="Missing rate limit on deploy_script webhook", body="")
        with caplog.at_level(logging.INFO, logger=quality_logger.name):
            apply_severity_consistency([f1, f2])
        assert not [r for r in caplog.records if r.name == quality_logger.name]


class TestQualityGate:
    def test_drops_below_confidence_floor(self):
        # Default floor is 0.60
        f_low = _f(confidence=0.59)
        f_at = _f(confidence=0.60)
        f_high = _f(confidence=0.90)

        result = apply_quality_gate([f_low, f_at, f_high])
        assert result[0].drop_reason is not None
        assert "below floor 0.60" in result[0].drop_reason
        assert result[1].drop_reason is None
        assert result[2].drop_reason is None
        assert len(active(result)) == 2

    def test_respects_env_floor_override(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CONFIDENCE_FLOOR", "0.75")
        f = _f(confidence=0.70)
        result = apply_quality_gate([f])
        assert result[0].drop_reason is not None
        assert "below floor 0.75" in result[0].drop_reason

    def test_enforces_severity_vocabulary(self):
        f_invalid = _f(severity="concern", confidence=0.95)
        f_blueprint = _f(severity="blueprint", confidence=0.95)
        f_note = _f(severity="outofscope", confidence=0.95)

        result = apply_quality_gate([f_invalid, f_blueprint, f_note])
        assert result[0].drop_reason == "invalid severity: 'concern'"
        assert result[1].drop_reason == "invalid severity: 'blueprint'"
        assert result[2].drop_reason is None

    def test_normalizes_severity_case(self):
        f = _f(severity="Error", confidence=0.95)
        result = apply_quality_gate([f])
        assert result[0].drop_reason is None
        assert result[0].severity == "error"

    def test_caps_errors_per_review(self):
        # 12 errors with varying confidences; default cap is 10
        errors = [
            _f(severity="error", confidence=0.80 + (i * 0.01), title=f"Err {i}")
            for i in range(12)
        ]
        result = apply_quality_gate(errors, max_errors=10)
        active_errors = active(result)
        assert len(active_errors) == 10
        # The two lowest-confidence errors (i=0,1 at 0.80 and 0.81) were dropped
        assert result[0].drop_reason == "error cap exceeded (max 10)"
        assert result[1].drop_reason == "error cap exceeded (max 10)"

    def test_warnings_and_notes_are_uncapped(self):
        warnings = [_f(severity="warning", confidence=0.9, title=f"W {i}") for i in range(15)]
        result = apply_quality_gate(warnings, max_errors=5)
        assert len(active(result)) == 15

    def test_does_not_mutate_input_findings(self):
        original = _f(severity="error", confidence=0.3)
        apply_quality_gate([original])
        assert original.drop_reason is None
        assert original.confidence == 0.3


class TestMaxErrorFindingsEnv:
    def test_new_env_name_is_honored(self, monkeypatch):
        monkeypatch.delenv("PRXREF_MAX_ERRORS", raising=False)
        monkeypatch.setenv("PRXREF_MAX_ERROR_FINDINGS", "2")
        assert _resolve_max_errors(None) == 2

    def test_legacy_env_name_still_works(self, monkeypatch):
        monkeypatch.delenv("PRXREF_MAX_ERROR_FINDINGS", raising=False)
        monkeypatch.setenv("PRXREF_MAX_ERRORS", "4")
        assert _resolve_max_errors(None) == 4

    def test_new_name_wins_over_legacy(self, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_ERROR_FINDINGS", "2")
        monkeypatch.setenv("PRXREF_MAX_ERRORS", "9")
        assert _resolve_max_errors(None) == 2

    def test_explicit_argument_beats_both_env_names(self, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_ERROR_FINDINGS", "2")
        assert _resolve_max_errors(7) == 7


# The five shapes below reconstruct the live anchor misses from the v0.10.0
# re-audit (14/19 on-target). Each fixture is a self-contained unified diff
# whose cited line is a valid added line, so the drift survives membership
# validation exactly the way the audited reviews did.
SHAPE1_DIFF = """\
diff --git a/src/functions.php b/src/functions.php
--- a/src/functions.php
+++ b/src/functions.php
@@ -1498,2 +1498,28 @@ function register_ajax_handlers(): void {
     add_action('wp_ajax_avatar_upload', 'handle_legacy_upload');
+    add_action('wp_ajax_nopriv_avatar_upload', 'handle_avatar_upload');
+    add_action('wp_ajax_avatar_upload', 'handle_avatar_upload');
+    add_action('wp_ajax_fetch_notifications', 'handle_fetch_notifications');
+    add_action('wp_ajax_save_settings', 'handle_save_settings');
+    add_action('wp_ajax_load_settings', 'handle_load_settings');
+    add_action('admin_menu', 'register_settings_screen');
+    add_action('admin_enqueue_scripts', 'enqueue_settings_assets');
+    add_filter('plugin_action_links', 'add_settings_link');
+    add_action('plugins_loaded', 'bootstrap_settings');
+    add_action('admin_init', 'register_settings_group');
+    add_action('rest_api_init', 'register_rest_routes');
+
+    /**
+     * Renders the admin settings screen for the plugin.
+     */
+    function render_settings_screen(): void {
+        if (!current_user_can('manage_options')) {
+            wp_die(esc_html__('Insufficient permissions.'));
+        }
+        echo '<div class="wrap">';
+        echo '<form method="post">';
+        settings_fields('prxref_settings');
+        do_settings_sections('prxref_settings');
+        echo '</form>';
+        echo '</div>';
+    }
 }
"""

SHAPE2_DIFF = """\
diff --git a/src/functions.php b/src/functions.php
--- a/src/functions.php
+++ b/src/functions.php
@@ -2398,2 +2398,4 @@ function enqueue_badge_styles(): void {
     $css = '<style>.badge { color: #333; }';
+    $css .= '.like-button .rating-star { font-weight: 600; }';
+    $css .= '.badge { padding: 12px 16px; }';
     wp_add_inline_style('prxref', $css);
@@ -3200,2 +3200,6 @@ function bp_like_get_rating(): int {
     $post_id = intval($_POST['post_id']);
+    if (!isset($_POST['member_id'])) {
+        bp_core_add_message(__('Missing member id.'));
+    }
+    $member_id = intval($_POST['member_id']);
     bp_like_register_vote($post_id, $member_id);
"""

SHAPE3_DIFF = """\
diff --git a/src/components/ExerciseFeedbackDialog.tsx b/src/components/ExerciseFeedbackDialog.tsx
--- a/src/components/ExerciseFeedbackDialog.tsx
+++ b/src/components/ExerciseFeedbackDialog.tsx
@@ -20,3 +20,8 @@ export const ExerciseFeedbackDialog = observer(() => {
     const [visible, setVisible] = useState(false);
+    const summary = useMemo(
+        () => computeSummary(feedback),
+        [feedback, store],
+    );
     const theme = useTheme();
+    const dense = useDenseViewport();
     return (
@@ -40,2 +45,4 @@ function FeedbackBody(props) {
     const dialog = useDialog();
+    const { rating, feedback } = exerciseStore.observables;
+    const canSubmit = rating !== null;
     return (
"""

SHAPE4_DIFF = """\
diff --git a/src/components/SessionDialog.tsx b/src/components/SessionDialog.tsx
--- a/src/components/SessionDialog.tsx
+++ b/src/components/SessionDialog.tsx
@@ -96,2 +96,34 @@ render() {
     <div className="dialog-header">
+        <button
+            type="button"
+            className="btn-close"
+            aria-label="Close dialog"
+            onClick={this.handleClose}
+        >
+            ×
+        </button>
+    </div>
+    <div className="dialog-body">
+        <p>Review the session details below.</p>
+        <label htmlFor="session-topic">Topic</label>
+        <input id="session-topic" readOnly />
+        <label htmlFor="session-length">Length</label>
+        <select id="session-length">
+            <option value="30">30 minutes</option>
+            <option value="60">60 minutes</option>
+        </select>
+        <label htmlFor="session-notes">Notes</label>
+        <textarea id="session-notes" rows={4} />
+    </div>
+    <div className="dialog-footer">
+        <span className="footer-note">
+            Saved responses appear here.
+        </span>
+        <button className="btn-secondary" onClick={this.handleCancel}>
+            Cancel
+        </button>
+    </div>
+    <div className="dialog-preview">
+        <input placeholder={formatStamp(FIXED_EPOCH)} disabled />
+    </div>
     )
"""

SHAPE5_DIFF = """\
diff --git a/assets/js/el.js b/assets/js/el.js
--- a/assets/js/el.js
+++ b/assets/js/el.js
@@ -30,6 +30,11 @@ import boot from './boot.js';
     const registry = new Map();
+
+    export function ready() {
     startTimers();
     }
+    const state = { ready: false };
     function tick() {}
     function idle() {}
+    const legacy = buildLegacyEntry();
+    const fs = require('fs');
     export default ready;
"""


class TestLiveAnchorDriftShapes:
    """One test per audited v0.10.0 anchor miss (live kzetxa PR shapes)."""

    def _align(self, diff: str, path: str, **kwargs) -> list[Finding]:
        parsed = parse_unified_diff(diff)
        defaults = {
            "file": path,
            "severity": "warning",
            "confidence": 0.8,
        }
        defaults.update(kwargs)
        finding = _f(**defaults)
        return apply_line_align(
            [finding], added_lines_by_file(parsed), files=parsed
        )[0]

    def test_shape1_docblock_near_miss_resolves_to_registration_line(self):
        # Live: functions.php:1520 — a claim about the nopriv avatar upload
        # registration anchored on an unrelated docblock inside the right
        # hunk, ~15 lines from the real add_action line.
        aligned = self._align(
            SHAPE1_DIFF,
            "src/functions.php",
            line=1512,
            title="Unauthenticated avatar upload handler registered for nopriv",
            body=(
                "The upload callback runs with no capability check, so any "
                "visitor can upload a file through the avatar endpoint."
            ),
        )
        assert aligned.line == 1499

    def test_shape2_far_hunk_generic_overlap_resolves_to_evidence_hunk(self):
        # Live: functions.php:2400 — a member_id claim corroborated by a far
        # CSS-tweak hunk purely because its like/rating class names overlap
        # the title; the evidence hunk is ~800 lines away.
        aligned = self._align(
            SHAPE2_DIFF,
            "src/functions.php",
            line=2400,
            title="Like/rating handlers trust client-supplied member_id",
            body=(
                "The vote callback reads member_id straight from the "
                "request, so one member can act as another."
            ),
        )
        assert aligned.line == 3204

    def test_shape3_deps_line_resolves_to_actual_destructure(self):
        # Live: ExerciseFeedbackDialog.tsx:30 — a destructuring claim
        # anchored on the related-but-wrong useMemo deps line, 12+ lines
        # above the actual destructure.
        aligned = self._align(
            SHAPE3_DIFF,
            "src/components/ExerciseFeedbackDialog.tsx",
            line=23,
            title="Destructured MobX observables read before store hydration",
            body=(
                "rating and feedback come straight off the root store "
                "before it finishes hydrating, so both can be undefined "
                "on first paint."
            ),
        )
        assert aligned.line == 46

    def test_shape4_click_handler_resolves_to_placeholder_timestamp(self):
        # Live: ExerciseFeedbackDialog.tsx:101 — a placeholder-timestamp
        # claim anchored on an unrelated close-button onClick, ~26 lines
        # from the code the claim describes.
        aligned = self._align(
            SHAPE4_DIFF,
            "src/components/SessionDialog.tsx",
            line=101,
            title="Placeholder timestamp shows a fixed epoch",
            body=(
                "The field placeholder renders a frozen timestamp instead "
                "of the current local time, so the sample reads as stale."
            ),
        )
        assert aligned.line == 127

    def test_shape5_blank_added_line_never_beats_token_bearing_line(self):
        # Live: el.js:31 — the claim text itself cited a line that is a
        # blank added line; the require("fs") evidence sits 8 lines lower.
        aligned = self._align(
            SHAPE5_DIFF,
            "assets/js/el.js",
            line=31,
            title="require used in ES module",
            body=(
                'require("fs") is CommonJS and will crash the ES module '
                "build; see assets/exercise-library-el.js:31."
            ),
        )
        assert aligned.line == 39

    def test_blank_landing_from_snap_pass_also_resolves_to_tokens(self):
        # The blank-anchor rule must hold on the snap path too: a citation
        # that snaps onto a blank added line re-resolves to the nearest
        # token-bearing evidence instead of posting on the blank line.
        aligned = self._align(
            SHAPE5_DIFF,
            "assets/js/el.js",
            line=30,
            title="require used in ES module",
            body=(
                'require("fs") is CommonJS and will crash the ES module '
                "build; see assets/exercise-library-el.js:31."
            ),
        )
        assert aligned.line == 39


# Issue #28 live shapes: the model's line field drifts while its own body
# still names the right line. SYNC_TS_DIFF is the sync.ts:15-vs-553 shape
# (added `debugger` lines at 15 and 553); CLI_TS_DIFF is the el.js:3-vs-46
# shape (added `await init` at 47, citation one line above it).
SYNC_TS_DIFF = """\
diff --git a/src/sync.ts b/src/sync.ts
--- a/src/sync.ts
+++ b/src/sync.ts
@@ -14,2 +14,3 @@ export async function sync() {
   const ready = prepare();
+  debugger;
   await send(ready);
@@ -553,2 +553,3 @@ export function flushQueue() {
+  debugger;
   const stale = queue.filter(isDone);
   persist(stale);
"""

CLI_TS_DIFF = """\
diff --git a/src/cli.ts b/src/cli.ts
--- a/src/cli.ts
+++ b/src/cli.ts
@@ -44,4 +44,5 @@ function banner() {
   const opts = parse(argv);
   applyDefaults(opts);
   main(opts);
+  await init(opts);
   return 0;
"""

WORKER_TS_DIFF = """\
diff --git a/src/worker.ts b/src/worker.ts
--- a/src/worker.ts
+++ b/src/worker.ts
@@ -14,1 +15,2 @@ export function tick() {
+  debugger;
   const ready = prepare();
@@ -553,1 +553,2 @@ export function report() {
+  retryBudget = maxRetries;
   audit(log);
"""


class TestBodyCitedAnchor:
    """A corroborated body citation outranks a drifted line field."""

    def _align(self, diff: str, path: str, **kwargs) -> Finding:
        parsed = parse_unified_diff(diff)
        defaults = {
            "file": path,
            "severity": "warning",
            "confidence": 0.8,
        }
        defaults.update(kwargs)
        return apply_line_align(
            [_f(**defaults)], added_lines_by_file(parsed), files=parsed
        )[0]

    def test_body_line_citation_outranks_drifted_field(self):
        # Live sync.ts shape: field said 15, body said line 553, the
        # actual `debugger` sits at 553.
        aligned = self._align(
            SYNC_TS_DIFF,
            "src/sync.ts",
            line=15,
            title="Leftover debugger statement",
            body=(
                "A debugger statement ships in the queue flush; remove "
                "the debugger before merging (line 553)."
            ),
        )
        assert aligned.line == 553

    def test_backticked_path_citation_resolves_within_tolerance(self):
        # Live el.js shape: field said 3, body cited `cli.ts:46`, the
        # actual added line is 47 (one below the cited context line).
        aligned = self._align(
            CLI_TS_DIFF,
            "src/cli.ts",
            line=3,
            title="main() entry point never awaits init",
            body=(
                "The main() entry point never awaits initialization; "
                "see `cli.ts:46`."
            ),
        )
        assert aligned.line == 47

    def test_citation_outside_the_diff_is_ignored(self):
        aligned = self._align(
            SYNC_TS_DIFF,
            "src/sync.ts",
            line=15,
            title="Leftover debugger statement",
            body="A debugger statement ships in the flush; see line 999.",
        )
        assert aligned.line == 15

    def test_uncorroborated_citation_is_ignored(self):
        # The cited hunk (retryBudget) shares zero evidence tokens with a
        # claim about the debugger statement, so the citation is dropped
        # and the shipped content rules keep the corroborated member 15.
        aligned = self._align(
            WORKER_TS_DIFF,
            "src/worker.ts",
            line=15,
            title="Leftover debugger statement",
            body=(
                "A debugger statement ships in the tick path; remove "
                "it, see line 553."
            ),
        )
        assert aligned.line == 15

    def test_first_corroborating_citation_wins(self):
        # line 553 names the retryBudget hunk (zero shared tokens) and is
        # skipped; line 15 corroborates.
        aligned = self._align(
            WORKER_TS_DIFF,
            "src/worker.ts",
            line=15,
            title="Leftover debugger statement",
            body=(
                "Check line 553 first; the debugger statement itself "
                "sits at line 15."
            ),
        )
        assert aligned.line == 15

    def test_earlier_citation_wins_when_both_corroborate(self):
        aligned = self._align(
            SYNC_TS_DIFF,
            "src/sync.ts",
            line=15,
            title="Leftover debugger statement",
            body=(
                "The debugger statement at line 553 matters more than "
                "the one at line 15."
            ),
        )
        assert aligned.line == 553

    def test_file_level_finding_promoted_by_corroborated_citation(self):
        aligned = self._align(
            SYNC_TS_DIFF,
            "src/sync.ts",
            line=0,
            title="Leftover debugger statement",
            body="A debugger statement remains at line 553.",
        )
        assert aligned.line == 553

    def test_bare_path_mention_never_overrides(self):
        aligned = self._align(
            SYNC_TS_DIFF,
            "src/sync.ts",
            line=15,
            title="Leftover debugger statement",
            body="See sync.ts for the debugger statement left in flush.",
        )
        assert aligned.line == 15


class TestBodyCitationGrammar:
    def test_backticked_own_file_path_line(self):
        f = _f(file="a/b/sync.ts", body="see `sync.ts:553` for the leak.")
        assert _body_cited_lines(f) == [553]

    def test_at_line_prose_form(self):
        f = _f(body="The bug appears at line 46 of the entry path.")
        assert _body_cited_lines(f) == [46]

    def test_bare_path_cites_nothing(self):
        f = _f(file="a/b/sync.ts", body="See sync.ts for details.")
        assert _body_cited_lines(f) == []

    def test_other_file_path_line_ignored(self):
        f = _f(file="a/b/sync.ts", body="Mirrors other.ts:12 behavior.")
        assert _body_cited_lines(f) == []

    def test_document_order_across_title_and_body(self):
        f = _f(
            file="src/cli.ts",
            title="Entry at cli.ts:40",
            body="Also line 55 matters.",
        )
        assert _body_cited_lines(f) == [40, 55]


class TestDeterministicCaps:
    """The error cap and the gate's output order are content-derived."""

    TIED = [
        _f(file="src/a.py", line=1, severity="error", confidence=0.9, title="alpha"),
        _f(file="src/a.py", line=2, severity="error", confidence=0.9, title="bravo"),
        _f(file="src/a.py", line=3, severity="error", confidence=0.9, title="charlie"),
    ]

    def test_error_cap_survivors_do_not_depend_on_arrival_order(self):
        seen = set()
        for perm in itertools.permutations(self.TIED):
            staged = apply_quality_gate(list(perm), confidence_floor=0.6, max_errors=2)
            seen.add(frozenset(f.title for f in staged if f.drop_reason is None))
        assert seen == {frozenset({"alpha", "bravo"})}

    def test_gate_output_is_sorted_by_file_line_title(self):
        staged = apply_quality_gate(
            list(reversed(self.TIED)), confidence_floor=0.6, max_errors=3
        )
        assert [f.title for f in staged] == ["alpha", "bravo", "charlie"]

    def test_confidence_outranks_content_in_the_rank_key(self):
        low = _f(file="src/a.py", line=1, severity="error", confidence=0.7, title="aaa")
        high = _f(file="src/z.py", line=9, severity="error", confidence=0.95, title="zzz")
        assert finding_rank_key(high) < finding_rank_key(low)

    def test_sort_key_tolerates_a_missing_line(self):
        assert finding_sort_key(_f(line=None, title="t")) == ("src/app.py", -1, "t")

class TestSettledThreadSuppression:
    """Line-independent suppression of re-litigated subjects (issue 06)."""

    SETTLED = Thread(
        path="src/tools/registry.ts",
        line=None,
        resolved=False,
        author="bob",
        body_snippet=(
            "This still feels too defensive, I don't think we need it imo — "
            "Removed the defensive tool metadata guard in commit 917d1f4"
        ),
    )

    def _finding(self, **kwargs):
        fields = {
            "file": "src/tools/registry.ts",
            "line": 0,
            "title": (
                "Tool metadata guard removed: unbounded tool names from remote servers"
            ),
            "body": (
                "This change removes the tool metadata guard: MAX_TOOL_NAME_LENGTH "
                "and isSupported no longer bound names or schemas supplied by a "
                "remote MCP server."
            ),
        }
        fields.update(kwargs)
        return _f(**fields)

    def test_a_file_level_finding_is_dropped_against_a_file_level_thread(self):
        out = apply_settled_thread_suppression([self._finding()], [self.SETTLED])
        assert out[0].drop_reason == "settled in thread: bob"

    def test_the_reason_names_the_author(self):
        thread = Thread(**{**self.SETTLED.__dict__, "author": "carol"})
        out = apply_settled_thread_suppression([self._finding()], [thread])
        assert out[0].drop_reason.endswith("carol")

    def test_a_leading_dot_slash_path_still_matches(self):
        thread = Thread(**{**self.SETTLED.__dict__, "path": "./src/tools/registry.ts"})
        out = apply_settled_thread_suppression([self._finding()], [thread])
        assert out[0].drop_reason is not None

    def test_a_thread_on_another_file_never_suppresses(self):
        thread = Thread(**{**self.SETTLED.__dict__, "path": "src/other/cache.ts"})
        out = apply_settled_thread_suppression([self._finding()], [thread])
        assert out[0].drop_reason is None

    def test_a_same_file_thread_on_another_subject_never_suppresses(self):
        thread = Thread(
            path="src/tools/registry.ts", line=None, resolved=True, author="dave",
            body_snippet="Please rename listTools to enumerateTools for consistency.",
        )
        out = apply_settled_thread_suppression([self._finding()], [thread])
        assert out[0].drop_reason is None

    def test_a_resolved_thread_still_settles_its_own_subject(self):
        thread = Thread(**{**self.SETTLED.__dict__, "resolved": True})
        out = apply_settled_thread_suppression([self._finding()], [thread])
        assert out[0].drop_reason == "settled in thread: bob"

    def test_an_already_dropped_finding_keeps_its_first_reason(self):
        dropped = self._finding(drop_reason="duplicate of existing thread")
        out = apply_settled_thread_suppression([dropped], [self.SETTLED])
        assert out[0].drop_reason == "duplicate of existing thread"

    def test_no_threads_is_a_passthrough_preserving_order(self):
        findings = [self._finding(), self._finding(line=7)]
        out = apply_settled_thread_suppression(findings, [])
        assert [f.line for f in out] == [0, 7]
        assert all(f.drop_reason is None for f in out)

    def test_an_empty_finding_body_cannot_match_anything(self):
        out = apply_settled_thread_suppression(
            [_f(file="src/tools/registry.ts", title="", body="")], [self.SETTLED],
        )
        assert out[0].drop_reason is None

_MANIFEST_PATH = "web/package.json"

# Post-image lines: 1 "{", 2 dependencies header, 3 express,
# 4 +@acme/jenkins, 5 lodash, 6 "},", 7 devDependencies header,
# 8-14 seven dev deps, 15 +vitest, 16 typescript, 17 "}", 18 "}".
# vitest deliberately sits FARTHER from its own section header (8 lines)
# than the jenkins entry does (3), which is what lets a header-ranked
# realign pull a correct vitest anchor onto the jenkins line.
_MANIFEST_DIFF = (
    f"diff --git a/{_MANIFEST_PATH} b/{_MANIFEST_PATH}\n"
    f"--- a/{_MANIFEST_PATH}\n"
    f"+++ b/{_MANIFEST_PATH}\n"
    "@@ -1,16 +1,18 @@\n"
    "{\n"
    '  "dependencies": {\n'
    '    "express": "^4.18.0",\n'
    '+    "@acme/jenkins": "*",\n'
    '    "lodash": "^4.17.21"\n'
    "  },\n"
    '  "devDependencies": {\n'
    '    "eslint": "^8.50.0",\n'
    '    "prettier": "^3.0.0",\n'
    '    "nodemon": "^3.0.0",\n'
    '    "esbuild": "^0.19.0",\n'
    '    "ts-node": "^10.9.0",\n'
    '    "@types/node": "^20.0.0",\n'
    '    "@types/jest": "^29.5.0",\n'
    '+    "vitest": "^4.0.18",\n'
    '    "typescript": "^5.2.0"\n'
    "  }\n"
    "}\n"
)

_MANIFEST_FILES = parse_unified_diff(_MANIFEST_DIFF)


class TestManifestClaimCheck:
    """apply_manifest_claim_check: a package.json claim must anchor on the
    key and the dependency section it actually names."""

    def test_anchor_mismatch_is_dropped(self):
        f = _f(
            file=_MANIFEST_PATH, line=4,
            title="vitest added to runtime dependencies",
            body="`vitest` is test-only but appears under `dependencies`.",
        )
        out = apply_manifest_claim_check([f], _MANIFEST_FILES)
        assert len(out) == 1
        assert out[0].drop_reason is not None
        assert out[0].drop_reason.startswith("anchor mismatch:")
        assert "vitest" in out[0].drop_reason
        assert "@acme/jenkins" in out[0].drop_reason

    def test_section_mismatch_is_dropped(self):
        f = _f(
            file=_MANIFEST_PATH, line=15,
            title="vitest added to runtime dependencies",
            body="This bloats production installs.",
        )
        out = apply_manifest_claim_check([f], _MANIFEST_FILES)
        assert out[0].drop_reason is not None
        assert out[0].drop_reason.startswith("section mismatch:")
        assert "devDependencies" in out[0].drop_reason

    def test_correct_claim_untouched(self):
        f = _f(
            file=_MANIFEST_PATH, line=15,
            title="vitest added to devDependencies",
            body="A new test runner is pulled in.",
        )
        out = apply_manifest_claim_check([f], _MANIFEST_FILES)
        assert out[0].drop_reason is None

    def test_scoped_package_name_resolves(self):
        f = _f(
            file=_MANIFEST_PATH, line=4,
            title="@acme/jenkins pinned to * in dependencies",
            body="A wildcard version makes installs unreproducible.",
        )
        out = apply_manifest_claim_check([f], _MANIFEST_FILES)
        assert out[0].drop_reason is None

    def test_no_claimed_key_untouched(self):
        f = _f(
            file=_MANIFEST_PATH, line=4,
            title="Manifest formatting is inconsistent",
            body="Indentation mixes two and four spaces.",
        )
        out = apply_manifest_claim_check([f], _MANIFEST_FILES)
        assert out[0].drop_reason is None

    def test_claimed_key_absent_from_hunks_untouched(self):
        f = _f(
            file=_MANIFEST_PATH, line=4,
            title="webpack should not be a runtime dependency",
            body="`webpack` belongs in devDependencies.",
        )
        out = apply_manifest_claim_check([f], _MANIFEST_FILES)
        assert out[0].drop_reason is None

    def test_non_manifest_file_untouched(self):
        f = _f(
            file="src/index.ts", line=4,
            title="vitest added to runtime dependencies",
            body="`vitest` is test-only but appears under `dependencies`.",
        )
        out = apply_manifest_claim_check([f], _MANIFEST_FILES)
        assert out[0].drop_reason is None

    def test_anchor_on_brace_line_has_no_key_and_no_section(self):
        f = _f(
            file=_MANIFEST_PATH, line=1,
            title="vitest added to runtime dependencies",
            body="This bloats production installs.",
        )
        out = apply_manifest_claim_check([f], _MANIFEST_FILES)
        assert out[0].drop_reason is None, out[0].drop_reason

    def test_anchor_on_section_header_falls_through_to_section_check(self):
        f = _f(
            file=_MANIFEST_PATH, line=7,
            title="vitest added to runtime dependencies",
            body="This bloats production installs.",
        )
        out = apply_manifest_claim_check([f], _MANIFEST_FILES)
        assert out[0].drop_reason is not None
        assert out[0].drop_reason.startswith("section mismatch:")

    def test_already_dropped_finding_passes_through(self):
        f = _f(
            file=_MANIFEST_PATH, line=4,
            title="vitest added to runtime dependencies",
            body="`vitest` is test-only but appears under `dependencies`.",
            drop_reason="confidence 0.10 below floor 0.60",
        )
        out = apply_manifest_claim_check([f], _MANIFEST_FILES)
        assert out[0].drop_reason == "confidence 0.10 below floor 0.60"

    def test_order_preserved(self):
        a = _f(file=_MANIFEST_PATH, line=4, title="a", body="b")
        b = _f(file="src/index.ts", line=1, title="c", body="d")
        out = apply_manifest_claim_check([a, b], _MANIFEST_FILES)
        assert [x.title for x in out] == ["a", "c"]


class TestManifestRealignRegression:
    """The section words must not decide a package.json realign: a claim
    correctly anchored on the vitest entry stays there instead of being
    pulled onto the nearest added line below the dependencies header."""

    def test_correct_vitest_anchor_survives_line_align(self):
        f = _f(
            file=_MANIFEST_PATH, line=15,
            title="vitest added to runtime dependencies",
            body=(
                "vitest is a test-only tool but is added under `dependencies` "
                "in web/package.json. This bloats production installs."
            ),
        )
        out = apply_line_align(
            [f], added_lines_by_file(_MANIFEST_FILES), files=_MANIFEST_FILES
        )
        assert out[0].line == 15

COPY_DIFF = (
    "diff --git src://pkg/servicenow/package.json dst://pkg/splunk/package.json\n"
    "similarity index 53%\n"
    "copy from pkg/servicenow/package.json\n"
    "copy to pkg/splunk/package.json\n"
    "@@ -1,1 +1,1 @@\n"
    "-old\n"
    "+new\n"
)

DELETE_DIFF = (
    "diff --git a/src/old.ts b/src/old.ts\n"
    "deleted file mode 100644\n"
    "--- a/src/old.ts\n"
    "+++ /dev/null\n"
    "@@ -1,1 +0,0 @@\n"
    "-gone\n"
)


class TestRemovalClaimCheck:
    """``apply_removal_claim_check`` drops removal claims the diff contradicts."""

    def test_drops_claim_when_the_copy_source_is_still_present(self):
        files = parse_unified_diff(COPY_DIFF)
        f = _f(
            file="pkg/splunk/package.json",
            title="ServiceNow package.json removed by rename to splunk",
            body="pkg/servicenow/package.json no longer exists after this PR.",
        )
        out = apply_removal_claim_check([f], files)
        assert len(out) == 1
        assert (out[0].drop_reason or "").startswith(
            "claims removal of a path present in the post-image"
        )
        assert "pkg/servicenow/package.json" in out[0].drop_reason

    def test_claim_named_only_in_prose_against_the_new_file_is_dropped(self):
        files = parse_unified_diff(COPY_DIFF)
        f = _f(
            file="pkg/splunk/package.json",
            title="Package moved",
            body="The servicenow/package.json was removed in this change.",
        )
        out = apply_removal_claim_check([f], files)
        assert (out[0].drop_reason or "").startswith(
            "claims removal of a path present in the post-image"
        )

    def test_keeps_claim_about_a_genuinely_removed_path(self):
        files = parse_unified_diff(DELETE_DIFF)
        f = _f(
            file="src/old.ts",
            title="File removed",
            body="src/old.ts was removed by this PR.",
        )
        out = apply_removal_claim_check([f], files)
        assert out[0].drop_reason is None

    def test_keeps_a_non_removal_finding(self):
        files = parse_unified_diff(COPY_DIFF)
        f = _f(
            file="pkg/splunk/package.json",
            title="Missing version field",
            body="pkg/servicenow/package.json defines a version; the copy does not.",
        )
        out = apply_removal_claim_check([f], files)
        assert out[0].drop_reason is None

    def test_passes_through_already_dropped_findings(self):
        files = parse_unified_diff(COPY_DIFF)
        f = _f(
            file="pkg/splunk/package.json",
            title="ServiceNow package.json removed",
            body="pkg/servicenow/package.json no longer exists.",
            drop_reason="invalid severity: 'nope'",
        )
        out = apply_removal_claim_check([f], files)
        assert out[0].drop_reason == "invalid severity: 'nope'"

    def test_preserves_input_order(self):
        files = parse_unified_diff(COPY_DIFF)
        findings = [
            _f(title="First", body="nothing to see"),
            _f(title="Second", body="pkg/servicenow/package.json was removed."),
            _f(title="Third", body="still nothing"),
        ]
        out = apply_removal_claim_check(findings, files)
        assert [x.title for x in out] == ["First", "Second", "Third"]
        assert [x.drop_reason is None for x in out] == [True, False, True]

HEDGED_CASES = [
    ("if-still", "If figmaProxy.prepare still leases a client, the lease "
                 "outlives the request."),
    ("if-already", "If the migration already applied the default, this write "
                   "is a no-op."),
    ("modal-still", "The socket may still be held open by the outer pool."),
    ("assuming", "Assuming the cache is keyed by tenant, this write clobbers "
                 "another entry."),
    ("unless-already", "Unless the backfill job already ran, this deploy "
                       "fails."),
    ("not-verified", "I cannot confirm whether the caller retries, so the "
                     "request may fail permanently."),
    ("not-in-diff", "The token refresh is not visible in the diff, so the "
                    "credential goes stale."),
    ("only-caller", "If this is the only call site the change is safe; "
                    "otherwise every other caller breaks."),
    ("membership", "If they are members of the root workspaces globs, "
                   "`npm ci` will fail."),
]

LEGITIMATE_CASES = [
    ("divisor", "size defaults to None and is used as a divisor on line 42."),
    ("early-return", "Returns early if the list is empty, so the counter is "
                     "never incremented."),
    ("retry-spin", "The retry loop may spin forever because the deadline is "
                   "never decremented."),
    ("typeerror", "Throws TypeError if `opts` is undefined: line 12 "
                  "dereferences opts.start."),
    ("lodash-unless", "unless() from lodash is called with a string, which it "
                      "does not accept."),
    ("jwterror", "decode(token) can raise JWTError if malformed."),
    ("null-deref", "x may be None when config is missing; data loss follows."),
    ("concurrency", "Concurrent calls may corrupt state."),
    ("assuming-noun", "The parser is assuming-safe only for ASCII; line 20 "
                      "indexes bytes directly."),
    ("may-return", "readFile may return a Buffer here and the caller "
                   "concatenates it with a string."),
    ("if-branch", "If retries is 0 the loop body never runs, so the initial "
                  "request is skipped."),
    ("unless-flag", "The endpoint is registered unless DEBUG is set, so "
                    "production serves the unauthenticated route."),
    ("already-plain", "The lock is already held here, so the second acquire "
                      "deadlocks."),
    ("may-plain", "The callback may be invoked twice, double-charging the "
                  "customer."),
    ("still-plain", "The temp file is still on disk after the handler "
                    "returns."),
    ("if-null", "If cfg is None line 30 raises AttributeError."),
]


class TestHedgeGate:
    @pytest.mark.parametrize(
        "label,body", HEDGED_CASES, ids=[c[0] for c in HEDGED_CASES]
    )
    def test_hedged_body_is_dropped(self, label, body):
        out = apply_hedge_gate([_f(title="Finding", body=body)])
        assert len(out) == 1
        assert out[0].drop_reason is not None, label
        assert out[0].drop_reason.startswith('hedged: "'), out[0].drop_reason
        assert out[0].drop_reason.endswith('"')

    @pytest.mark.parametrize(
        "label,body", LEGITIMATE_CASES, ids=[c[0] for c in LEGITIMATE_CASES]
    )
    def test_legitimate_body_survives(self, label, body):
        out = apply_hedge_gate([_f(title="Finding", body=body)])
        assert out[0].drop_reason is None, f"{label}: {out[0].drop_reason}"

    def test_hedge_in_title_is_dropped(self):
        out = apply_hedge_gate(
            [_f(title="Lease may still be held", body="Plain body.")]
        )
        assert out[0].drop_reason.startswith("hedged:")

    def test_drop_reason_quotes_the_matched_span(self):
        out = apply_hedge_gate(
            [_f(body="The socket may still be held open by the pool.")]
        )
        assert out[0].drop_reason == 'hedged: "may still"'

    def test_drop_reason_span_is_bounded(self):
        body = "If " + "x" * 75 + " still leaks."
        out = apply_hedge_gate([_f(body=body)])
        assert out[0].drop_reason is not None
        assert len(out[0].drop_reason) == len('hedged: ""') + 80

    def test_span_beyond_the_window_is_not_a_hedge(self):
        body = "If " + "x" * 200 + " still leaks."
        assert apply_hedge_gate([_f(body=body)])[0].drop_reason is None

    def test_already_dropped_passes_through(self):
        pre = _f(body="If the router is still mounted, both fire.",
                 drop_reason="invalid severity: 'nit'")
        out = apply_hedge_gate([pre])
        assert out[0].drop_reason == "invalid severity: 'nit'"

    def test_order_preserved_and_input_not_mutated(self):
        findings = [
            _f(title="a", body="Plain bug."),
            _f(title="b", body="If the flag is still set, the loop spins."),
            _f(title="c", body="Another plain bug."),
        ]
        out = apply_hedge_gate(findings)
        assert [f.title for f in out] == ["a", "b", "c"]
        assert [f.drop_reason is None for f in out] == [True, False, True]
        assert all(f.drop_reason is None for f in findings)

    def test_empty_input(self):
        assert apply_hedge_gate([]) == []

    def test_hedged_finding_does_not_consume_error_cap(self):
        hedged = apply_hedge_gate(
            [_f(severity="error", confidence=0.9,
                body="If the cache is still warm, this errors.")]
        )
        out = apply_quality_gate(hedged, max_errors=1)
        assert out[0].drop_reason.startswith("hedged:")
