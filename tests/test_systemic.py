"""Digest extractor tests: prxref.systemic.build_digest and its patterns."""
from __future__ import annotations

import pytest

from prxref.reviewer import load_prompt
from prxref.systemic import (
    FULL_CONTENT_MAX_ADDED_LINES,
    MAX_LINES_PER_FILE,
    MIGRATION_FULL_CONTENT_MAX_ADDED_LINES,
    TRUNCATION_MARKER,
    build_digest,
    match_class,
)
from prxref.triage import parse_unified_diff


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


def _modified_file(path: str, before: list[str], removed: list[str], after: list[str]) -> str:
    body = (
        "".join(f" {text}\n" for text in before)
        + "".join(f"-{text}\n" for text in removed)
        + "".join(f"+{text}\n" for text in after)
    )
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{len(before) + len(removed)} +1,{len(before) + len(after)} @@\n"
        f"{body}"
    )


def _digest(*file_diffs: str, token_budget: int = 25_000) -> str:
    return build_digest(parse_unified_diff("\n".join(file_diffs)), token_budget)


class TestPatternClasses:
    @pytest.mark.parametrize("line,expected", [
        ("export async function handler(req, res) {", "entry-point"),
        ("export function main() {}", "entry-point"),
        ("def handler(event, context):", "entry-point"),
        ("def lambda_handler(event, context):", "entry-point"),
        ("@app.route('/billing/charge', methods=['POST'])", "entry-point"),
        ("@router.post('/subscriptions')", "entry-point"),
        ("@api_view(['POST'])", "entry-point"),
        ("add_action('wp_ajax_nopriv_avatar_upload', 'avatar_upload');", "entry-point"),
        ("add_action('wp_ajax_avatar_upload', 'avatar_upload');", "entry-point"),
        ("router.get('/invoice/:id', invoiceHandler)", "entry-point"),
        ("app.post('/charge', chargeHandler)", "entry-point"),
        ("const key = process.env.STRIPE_SECRET_KEY;", "secret"),
        ("const cfg = import.meta.env;", "secret"),
        ("export const API_URL = import.meta.env.VITE_LOCAL_SUPABASE_URL;", "secret"),
        ("const SUPABASE_KEY = 'eyJhbGciOi';", "secret"),
        ("apiKey = config['API_KEY']", "secret"),
        ("const token = await jwt.sign(payload);", "secret"),
        ("check_ajax_referer('avatar_nonce', 'nonce');", "auth-check"),
        ("if (!wp_verify_nonce($_POST['_wpnonce'], 'avatar')) {", "auth-check"),
        ("const decoded = verify_jwt(payload);", "auth-check"),
        ("const user = authenticate(request);", "auth-check"),
        ("if (!isAuthenticated(session)) return 401;", "auth-check"),
        ("} catch (err) {", "error-swallow"),
        ("except ValueError:", "error-swallow"),
        ("except: pass", "error-swallow"),
        ("fetch(url).catch(() => {})", "error-swallow"),
        ("CREATE TABLE users (id bigint);", "migration-ddl"),
        ("alter table subscriptions add column trial bool;", "migration-ddl"),
        ("ENABLE ROW LEVEL SECURITY;", "migration-ddl"),
        ("CREATE POLICY tenant_isolation ON users;", "migration-ddl"),
        ("DROP TABLE audit_log;", "migration-ddl"),
        ("ALTER TABLE invoices ADD COLUMN paid_at timestamptz;", "migration-ddl"),
        ("console.log('charging card', card);", "console-log"),
        ("console.error('write failed', err);", "console-log"),
        ("logger.warn('retrying');", "console-log"),
        ("logging.exception('charge failed')", "console-log"),
        ("const timer = setInterval(poll, 3000);", "loop-timer"),
        ("setTimeout(() => refresh(), 500);", "loop-timer"),
        ("while (true) { tick(); }", "loop-timer"),
        ("requestAnimationFrame(render);", "loop-timer"),
        ("for (;;) { poll(); }", "loop-timer"),
        ("\"packageManager\": \"yarn@4.5.0\",", "repo-config"),
    ])
    def test_high_signal_lines_match(self, line, expected):
        assert match_class(line) == expected

    @pytest.mark.parametrize("line", [
        "const total = a + b;",
        "  return render(<Invoice rows={rows} />);",
        "self.assertEqual(result.status, 200)",
        "}",
        "",
        "users = User.objects.filter(active=True)",
        "for (const item of items) {",
        "\"name\": \"web\",",
        "clearInterval(timer);",
    ])
    def test_ordinary_lines_match_nothing(self, line):
        assert match_class(line) is None

    def test_a_multi_match_line_reports_the_first_class(self):
        # Defines a handler AND reads an env var: entry-point wins, and the
        # line enters the digest exactly once.
        assert match_class("export async function handler(req) { run(process.env.X) }") == "entry-point"


class TestDigestShape:
    def test_every_file_gets_a_header_and_hunk_headers(self):
        plain = [f"const x{i} = {i};" for i in range(FULL_CONTENT_MAX_ADDED_LINES + 5)]
        digest = _digest(
            _added_file("src/supabase.ts", plain),
            _modified_file(
                "src/other.py", ["def helper():"], ["pass"], ["return 1"],
            ),
        )
        assert "## src/supabase.ts" in digest
        assert "## src/other.py" in digest
        assert f"@@ -0,0 +1,{len(plain)} @@" in digest
        assert "@@ -1,2 +1,2 @@" in digest
        # Above the full-content threshold and matching nothing: only the
        # skeleton appears.
        assert "const x0 = 0;" not in digest

    def test_matched_added_lines_carry_new_file_numbers(self):
        plain = ["// filler"] * (FULL_CONTENT_MAX_ADDED_LINES + 1)
        lines = plain[:1] + ["const KEY = process.env.SUPABASE_KEY;"] + plain[1:]
        digest = _digest(_added_file("src/supabase.ts", lines))
        assert "+2| const KEY = process.env.SUPABASE_KEY;" in digest
        assert "+1| // filler" not in digest
        assert "+3| // filler" not in digest

    def test_a_small_file_shows_every_added_line_with_its_number(self):
        digest = _digest(_added_file(
            "src/supabase.ts",
            ["const a = 1;", "const KEY = process.env.SUPABASE_KEY;", "const c = 3;"],
        ))
        assert "+1| const a = 1;" in digest
        assert "+2| const KEY = process.env.SUPABASE_KEY;" in digest
        assert "+3| const c = 3;" in digest

    def test_removed_lines_carry_old_file_numbers(self):
        digest = _digest(_modified_file(
            "migrations/001_init.sql",
            ["-- users table"],
            ["CREATE TABLE users (id bigint);"],
            ["CREATE TABLE users (id bigint, org bigint);"],
        ))
        assert "-2| CREATE TABLE users (id bigint);" in digest
        assert "+2| CREATE TABLE users (id bigint, org bigint);" in digest

    def test_context_lines_are_never_included(self):
        digest = _digest(_modified_file(
            "src/billing.py",
            ["import stripe", "def charge(card):", "    plan = lookup(card)"],
            ["    return bill(card)"],
            ["    return bill(card, plan)"],
        ))
        assert "def charge(card):" not in digest
        assert "-1| import stripe" not in digest

    def test_binary_files_contribute_only_a_header(self):
        digest = _digest(
            "diff --git a/logo.png b/logo.png\n"
            "Binary files a/logo.png and b/logo.png differ\n"
        )
        assert "## logo.png" in digest
        assert "@@" not in digest

    def test_the_digest_is_deterministic(self):
        diff = _added_file("src/supabase.ts", ["const KEY = process.env.TOKEN;"]) * 1
        first = _digest(diff, diff)
        second = _digest(diff, diff)
        assert first == second


class TestDigestCaps:
    def test_per_file_cap_omits_excess_lines_and_says_so(self):
        lines = [f"console.log('line {i}');" for i in range(MAX_LINES_PER_FILE + 30)]
        digest = _digest(_added_file("src/noisy.js", lines))
        assert digest.count("console.log") == MAX_LINES_PER_FILE
        assert f"... {30} more matched line(s) in this file omitted" in digest

    def test_the_token_budget_truncates_and_announces_it(self):
        files = "".join(
            _added_file(f"src/f{i}.ts", ["const KEY = process.env.TOKEN;"] * 5)
            for i in range(20)
        )
        digest = _digest(files, token_budget=1)
        assert TRUNCATION_MARKER in digest
        # The skeleton header survived; most files did not.
        assert digest.count("## src/") < 20

    def test_a_budget_that_fits_everything_adds_no_marker(self):
        files = _added_file("src/small.ts", ["const KEY = process.env.TOKEN;"])
        digest = _digest(files, token_budget=25_000)
        assert TRUNCATION_MARKER not in digest
        assert "+1| const KEY = process.env.TOKEN;" in digest

    def test_a_degenerate_budget_degrades_to_a_truncated_digest(self):
        digest = _digest(_added_file("src/a.ts", ["const KEY = process.env.TOKEN;"]), token_budget=0)
        assert TRUNCATION_MARKER in digest


_RLS_LESS_MIGRATION = [
    "CREATE TABLE public.user_video_uploads (",
    "  id uuid primary key default gen_random_uuid(),",
    "  user_id uuid not null references auth.users(id),",
    "  storage_path text not null,",
    "  status text not null default 'processing',",
    "  created_at timestamptz not null default now()",
    ");",
]


class TestMigrationFullContent:
    """A DDL-matched file shows its whole added content, so the ABSENCE of
    RLS/policy lines inside it is a fact the sweep can check, not an
    artifact of which lines happened to match a pattern."""

    def test_an_rls_less_migration_shows_its_full_added_content(self):
        digest = _digest(_added_file("supabase/migrations/0001_uploads.sql", _RLS_LESS_MIGRATION))
        assert "+1| CREATE TABLE public.user_video_uploads (" in digest
        assert "+4|   storage_path text not null," in digest
        assert "+6|   created_at timestamptz not null default now()" in digest

    def test_removed_ddl_in_a_migration_still_carries_old_numbers(self):
        digest = _digest(_modified_file(
            "migrations/0001_init.sql",
            ["-- users table"],
            ["DROP POLICY tenant_isolation ON users;"],
            ["DROP POLICY tenant_isolation ON users;"],
        ))
        assert "-2| DROP POLICY tenant_isolation ON users;" in digest
        assert "+2| DROP POLICY tenant_isolation ON users;" in digest

    def test_a_migration_over_the_cap_degrades_to_pattern_lines_with_a_note(self):
        body = ["CREATE TABLE big ("]
        body += [f"  col_{i} bigint," for i in range(MIGRATION_FULL_CONTENT_MAX_ADDED_LINES)]
        body += [");"]
        digest = _digest(_added_file("migrations/0002_big.sql", body))
        assert "[full content omitted" in digest
        assert "+1| CREATE TABLE big (" in digest
        assert "+2|" not in digest

    def test_a_file_the_ddl_pattern_never_touched_stays_small_file_only(self):
        digest = _digest(_added_file("src/db.py", ["engine = create_engine(url);"]))
        assert "+1| engine = create_engine(url);" in digest


class TestSmallFileFullContent:
    """Files under the added-line threshold render whole, so loop bodies and
    config keys that match no pattern still reach the sweep."""

    def test_the_infinite_poll_store_shows_its_loop_body(self):
        digest = _digest(_added_file("src/stores/VideoThumbnailStore.ts", [
            "export const useVideoThumbnailStore = create((set) => ({",
            "  startPolling: (videoId) => {",
            "    const timer = setInterval(async () => {",
            "      const res = await fetch(`/api/videos/${videoId}/thumbnail`);",
            "      if (res.status === 404) {",
            "        return;",
            "      }, 3000);",
            "  },",
            "}));",
        ]))
        assert "+3|     const timer = setInterval(async () => {" in digest
        assert "+5|       if (res.status === 404) {" in digest
        assert "+7|       }, 3000);" in digest

    def test_a_file_over_the_threshold_stays_pattern_matched(self):
        plain = [f"const x{i} = {i};" for i in range(FULL_CONTENT_MAX_ADDED_LINES + 1)]
        digest = _digest(_added_file("src/big.ts", plain))
        assert "const x0 = 0;" not in digest

    def test_context_lines_stay_out_of_a_full_content_file(self):
        digest = _digest(_modified_file(
            "src/billing.py",
            ["import stripe", "def charge(card):", "    plan = lookup(card)"],
            ["    return bill(card)"],
            ["    return bill(card, plan)"],
        ))
        assert "+2| def charge(card):" not in digest
        assert "-1| import stripe" not in digest
        assert "+4|     return bill(card, plan)" in digest

    def test_full_content_inclusion_is_deterministic(self):
        diff = _added_file("src/small.ts", ["a = 1", "b = 2"])
        assert _digest(diff) == _digest(diff)


class TestFullContentBudgetShare:
    """Full content is a bounded share of the digest budget, never a way to
    blow it: files that do not fit degrade to pattern lines and say so."""

    def test_a_small_file_too_big_for_the_share_degrades_to_pattern_lines(self):
        lines = ["x" * 90 for _ in range(FULL_CONTENT_MAX_ADDED_LINES)]
        lines[10] = "const KEY = process.env.SUPABASE_KEY;"
        digest = _digest(_added_file("src/wide.ts", lines), token_budget=100)
        assert "[full content omitted" in digest
        assert "+11| const KEY = process.env.SUPABASE_KEY;" in digest
        assert "+1| " + "x" * 90 not in digest
        assert TRUNCATION_MARKER not in digest

    def test_a_migration_is_not_share_capped_but_still_budget_capped(self):
        body = [f"  col_{i} bigint," for i in range(120)]
        diff = _added_file("migrations/0003.sql", ["CREATE TABLE t ("] + body + [");"])
        wide_open = _digest(diff, token_budget=2_000)
        assert "+2|   col_0 bigint," in wide_open
        assert TRUNCATION_MARKER not in wide_open
        tight = _digest(diff, token_budget=200)
        assert "[full content omitted" in tight
        assert "+1| CREATE TABLE t (" in tight

    def test_the_digest_never_exceeds_its_token_budget(self):
        files = []
        for i in range(30):
            files.append(_added_file(f"src/small{i}.ts", [f"const a{i} = {i};" ] * 55))
        for i in range(40):
            files.append(_added_file(f"src/noisy{i}.tsx", [f"console.log('x{i}');" ] * 60))
        for i in range(10):
            files.append(_added_file(f"migrations/{i:04}.sql", _RLS_LESS_MIGRATION))
        digest = _digest(*files, token_budget=25_000)
        assert len(digest) <= 25_000 * 4


class TestRepoConfigNotes:
    """A lockfile added beside another lockfile or a packageManager pin is a
    repo-config signal the digest states deterministically."""

    def test_a_new_lockfile_beside_a_package_manager_pin_gets_a_note(self):
        digest = _digest(
            _added_file("web/package-lock.json", ['{', '"lockfileVersion": 3', '}']),
            _modified_file("web/package.json", ['{'], [], ['  "packageManager": "yarn@4.5.0",']),
        )
        assert (
            "! repo-config: package-lock.json is newly added alongside a "
            '"packageManager" pin in package.json' in digest
        )

    def test_a_new_lockfile_beside_another_lockfile_names_it(self):
        digest = _digest(
            _added_file("web/package-lock.json", ['{']),
            _modified_file("web/yarn.lock", ["# yarn lockfile v1"], [], ["# yarn lockfile v1"]),
        )
        assert (
            "! repo-config: package-lock.json is newly added alongside the "
            "yarn.lock lockfile" in digest
        )

    def test_a_lone_new_lockfile_gets_no_note(self):
        digest = _digest(_added_file("web/package-lock.json", ['{']))
        assert "! repo-config:" not in digest

    def test_a_modified_lockfile_gets_no_note(self):
        digest = _digest(
            _modified_file("web/package-lock.json", ['{'], [], ['  "x": 1']),
            _modified_file("web/yarn.lock", ["# yarn lockfile v1"], [], ["# yarn lockfile v1"]),
        )
        assert "! repo-config:" not in digest

    def test_two_new_lockfiles_note_each_other(self):
        digest = _digest(
            _added_file("web/package-lock.json", ['{']),
            _added_file("web/pnpm-lock.yaml", ['lockfileVersion: 9']),
        )
        assert (
            "! repo-config: package-lock.json is newly added alongside the "
            "pnpm-lock.yaml lockfile" in digest
        )
        assert (
            "! repo-config: pnpm-lock.yaml is newly added alongside the "
            "package-lock.json lockfile" in digest
        )


class TestMustSeeSurvival:
    """The per-file cap consumes fill-class matches first: a secret or entry
    point can never be omitted because noisier classes filled the cap."""

    def test_a_secret_beyond_the_cap_is_kept_ahead_of_console_noise(self):
        lines = [f"console.log('noise {i}');" for i in range(MAX_LINES_PER_FILE + 30)]
        lines.append("console.log('VITE_VIMEO_ACCESS_TOKEN:', process.env.VITE_VIMEO_ACCESS_TOKEN);")
        digest = _digest(_added_file("src/config.ts", lines))
        assert "'VITE_VIMEO_ACCESS_TOKEN:', process.env.VITE_VIMEO_ACCESS_TOKEN" in digest
        assert "... 31 more matched line(s) in this file omitted" in digest

    def test_an_entry_point_beyond_the_cap_is_kept_ahead_of_console_noise(self):
        lines = [f"console.log('noise {i}');" for i in range(MAX_LINES_PER_FILE + 30)]
        lines.append("app.post('/charge', chargeHandler)")
        digest = _digest(_added_file("src/routes.ts", lines))
        assert f"+{MAX_LINES_PER_FILE + 31}| app.post('/charge', chargeHandler)" in digest

    def test_fill_lines_keep_their_order_behind_the_must_see_lines(self):
        lines = [f"console.log('noise {i}');" for i in range(MAX_LINES_PER_FILE + 30)]
        lines.insert(0, "const SECRET = 'x';")
        digest = _digest(_added_file("src/config.ts", lines))
        first_console = digest.index("+2| console.log('noise 0');")
        last_console = digest.index(f"+{MAX_LINES_PER_FILE}| console.log('noise {MAX_LINES_PER_FILE - 2}');")
        assert first_console < last_console


class TestSweepPromptContract:
    """The sweep prompt names the classes the digest now makes visible."""

    def test_the_prompt_names_the_new_evidence(self):
        prompt = load_prompt("systemic.md")
        assert "CREATE TABLE" in prompt
        assert "ROW LEVEL SECURITY" in prompt
        assert "setInterval" in prompt
        assert "repo-config" in prompt
        assert "full content" in prompt

    def test_the_prompt_still_keeps_the_context_marker(self):
        from prxref.reviewer import _CONTEXT_MARKER

        assert _CONTEXT_MARKER in load_prompt("systemic.md")


class TestGuardRemoval:
    """A deleted limit constant or validator is a class of its own (issue 06).

    Removal-only by construction: the same text ADDED is a guard being put in
    place, which is not a finding.
    """

    @pytest.mark.parametrize("line", [
        "const MAX_TOOL_NAME_LENGTH = 128;",
        "const MAX_TOOL_SCHEMA_BYTES = 100_000;",
        "static final int REQUEST_TIMEOUT = 30;",
        "MAX_UPLOAD_BYTES: int = 5_000",
        "MAX_RETRIES_CAP = 3",
    ])
    def test_removed_limit_constants(self, line):
        assert match_class(line, kind="-") == "guard-removal"

    @pytest.mark.parametrize("line", [
        "function isSupported(tool) {",
        "function validateSchema(s) {",
        "def sanitize_name(name):",
        "def check_bounds(n):",
        "fn escape_html(s: &str) {",
        "func assertOwner(u User) {",
        "const guardInput = (x) => {",
        "public boolean isValidName(String n) {",
    ])
    def test_removed_validator_definitions(self, line):
        assert match_class(line, kind="-") == "guard-removal"

    @pytest.mark.parametrize("line", [
        "const MAX_TOOL_NAME_LENGTH = 128;",
        "function isSupported(tool) {",
    ])
    def test_the_same_line_added_is_not_guard_removal(self, line):
        assert match_class(line, kind="+") != "guard-removal"

    def test_a_removed_log_line_keeps_its_own_class(self):
        assert match_class('console.log("x")', kind="-") == "console-log"

    def test_default_kind_is_an_addition(self):
        assert match_class("const MAX_TOOL_NAME_LENGTH = 128;") is None

    def test_the_digest_renders_removals_with_their_minus_marker(self):
        digest = _digest(_modified_file(
            "src/tools/registry.ts",
            ['import { z } from "zod";'],
            ["const MAX_TOOL_NAME_LENGTH = 128;", "function isSupported(tool) {"],
            ["  return raw.slice();"],
        ))
        assert "-2| const MAX_TOOL_NAME_LENGTH = 128;" in digest
        assert "-3| function isSupported(tool) {" in digest

    def test_guard_removal_survives_a_tight_per_file_cap(self):
        noise = [f"console.log('noise {i}');" for i in range(MAX_LINES_PER_FILE + 5)]
        diff = _modified_file(
            "src/big.ts",
            ["const a = 1;"],
            [*noise, "const MAX_BODY_BYTES = 1024;"],
            [f"const filler{i} = {i};" for i in range(FULL_CONTENT_MAX_ADDED_LINES + 5)],
        )
        digest = _digest(diff)
        assert "MAX_BODY_BYTES" in digest
