"""Digest extractor tests: prxref.systemic.build_digest and its patterns."""
from __future__ import annotations

import pytest

from prxref.systemic import (
    MAX_LINES_PER_FILE,
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
    ])
    def test_ordinary_lines_match_nothing(self, line):
        assert match_class(line) is None

    def test_a_multi_match_line_reports_the_first_class(self):
        # Defines a handler AND reads an env var: entry-point wins, and the
        # line enters the digest exactly once.
        assert match_class("export async function handler(req) { run(process.env.X) }") == "entry-point"


class TestDigestShape:
    def test_every_file_gets_a_header_and_hunk_headers(self):
        digest = _digest(
            _added_file("src/supabase.ts", ["const x = 1;", "const y = 2;"]),
            _modified_file(
                "src/other.py", ["def helper():"], ["pass"], ["return 1"],
            ),
        )
        assert "## src/supabase.ts" in digest
        assert "## src/other.py" in digest
        assert "@@ -0,0 +1,2 @@" in digest
        assert "@@ -1,2 +1,2 @@" in digest
        # No matched lines in either file: only the skeleton appears.
        assert "const x = 1;" not in digest

    def test_matched_added_lines_carry_new_file_numbers(self):
        digest = _digest(_added_file(
            "src/supabase.ts",
            ["const a = 1;", "const KEY = process.env.SUPABASE_KEY;", "const c = 3;"],
        ))
        assert "+2| const KEY = process.env.SUPABASE_KEY;" in digest
        assert "const a = 1;" not in digest
        assert "const c = 3;" not in digest

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
        lines = [f"console.log('line {i}');" for i in range(MAX_LINES_PER_FILE + 10)]
        digest = _digest(_added_file("src/noisy.js", lines))
        assert digest.count("console.log") == MAX_LINES_PER_FILE
        assert f"... {10} more matched line(s) in this file omitted" in digest

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
