"""Tests for prxref.quality: line alignment, thread dedup, and quality gate."""
from __future__ import annotations

from prxref.forges.base import Thread
from prxref.quality import (
    _resolve_max_errors,
    active,
    apply_line_align,
    apply_quality_gate,
    apply_severity_consistency,
    apply_thread_dedup,
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
