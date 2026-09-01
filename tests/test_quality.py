"""Tests for prxref.quality: line alignment, thread dedup, and quality gate."""
from __future__ import annotations

from prxref.forges.base import Thread
from prxref.quality import (
    _resolve_max_errors,
    active,
    apply_line_align,
    apply_quality_gate,
    apply_thread_dedup,
    is_duplicate_of_existing,
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
        assert aligned[0].line == 1261

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
