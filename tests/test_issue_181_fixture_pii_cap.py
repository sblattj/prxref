"""Issue #181: personal data in a test fixture is never a blocking error.

The issue's failure case: a PR adds a unit test whose ``setUp`` pins a
notification sender to a developer's own address, and the model reports
"Replace real employee email addresses in test fixtures" as ``error`` at
confidence 0.6, which flips the verdict to Request-Changes. The fix caps such
a finding at ``warning`` (:mod:`prxref.fixture_pii`) unless it reads as a
secret or customer data.
"""
from __future__ import annotations

import pytest

from prxref.fixture_pii import (
    apply_fixture_pii_cap,
    is_fixture_pii_finding,
    is_test_path,
)
from prxref.orchestrator import orchestrate_review
from prxref.triage import Finding
from tests.test_orchestrator import REF, FakeForge, FakeLLM

TEST_FILE = "tests/test_reminders.py"
SRC_FILE = "src/library/reminders.py"
ISSUE_TITLE = "Replace real employee email addresses in test fixtures"
ISSUE_BODY = (
    "`LIBRARY_NOTIFY_FROM` is set to a real person's work address "
    "(`avery.quinn@northwind-labs.test`). Use an `example.test` address instead."
)


def test_diff(path: str = TEST_FILE) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1,7 @@\n"
        "+import os\n"
        "+import unittest\n"
        "+\n"
        "+\n"
        "+class ReminderTests(unittest.TestCase):\n"
        "+    def setUp(self):\n"
        '+        os.environ["LIBRARY_NOTIFY_FROM"] = "avery.quinn@northwind-labs.test"\n'
    )


test_diff.__test__ = False


def raw(file=TEST_FILE, severity="error", title=ISSUE_TITLE, body=ISSUE_BODY,
        confidence=0.6, line=7):
    return {
        "file": file, "line": line, "severity": severity, "confidence": confidence,
        "title": title, "body": body,
    }


def run(path, findings):
    forge = FakeForge(diff=test_diff(path))
    res = orchestrate_review(forge, REF, FakeLLM({path: findings}), post=True, max_workers=1)
    return forge, res


def finding(file=TEST_FILE, severity="error", title=ISSUE_TITLE, body=ISSUE_BODY, **kw):
    return Finding(file=file, line=7, severity=severity, title=title, body=body,
                   confidence=kw.pop("confidence", 0.6), **kw)


@pytest.mark.usefixtures("contract_stubs")
class TestTheIssueCase:
    def test_the_fixture_email_error_is_posted_as_a_warning(self):
        _, res = run(TEST_FILE, [raw()])
        (f,) = res["findings_active"]
        assert f.severity == "warning"
        assert f.title == ISSUE_TITLE

    def test_the_verdict_is_no_longer_request_changes(self):
        _, res = run(TEST_FILE, [raw()])
        assert res["verdict"] == "Approved"

    def test_the_same_finding_on_production_code_keeps_the_error(self):
        _, res = run(SRC_FILE, [raw(file=SRC_FILE)])
        (f,) = res["findings_active"]
        assert f.severity == "error"
        assert res["verdict"] == "Request-Changes"

    def test_a_secret_in_a_test_fixture_keeps_the_error(self):
        _, res = run(TEST_FILE, [raw(
            title="Hard-coded API token in test fixture",
            body="The fixture's email and token `sk_live_...` look like a real credential.",
        )])
        (f,) = res["findings_active"]
        assert f.severity == "error"

    def test_customer_data_in_a_test_fixture_keeps_the_error(self):
        _, res = run(TEST_FILE, [raw(
            title="Real customer email addresses in test fixture",
            body="These look like customer records copied from production.",
        )])
        (f,) = res["findings_active"]
        assert f.severity == "error"

    def test_a_body_that_mentions_production_is_still_lowered(self):
        _, res = run(TEST_FILE, [raw(body=(
            "Not a secret, but this is a real employee's work address and the "
            "test could mail them if it ever ran against a production relay."
        ))])
        (f,) = res["findings_active"]
        assert f.severity == "warning"

    def test_an_email_behaviour_error_in_a_test_file_keeps_its_severity(self):
        _, res = run(TEST_FILE, [raw(
            title="Test never asserts the email was sent",
            body="The mock mailer swallows the email send failure, so the test passes either way.",
        )])
        (f,) = res["findings_active"]
        assert f.severity == "error"

    def test_an_unrelated_error_in_a_test_file_keeps_its_severity(self):
        _, res = run(TEST_FILE, [raw(
            title="setUp never restores LIBRARY_NOTIFY_FROM",
            body="The environment variable leaks into later tests; add a tearDown.",
        )])
        (f,) = res["findings_active"]
        assert f.severity == "error"


class TestIsTestPath:
    @pytest.mark.parametrize("path", [
        "tests/test_reminders.py",
        "test/helpers.py",
        "pkg/__tests__/mail.ts",
        "src/mail.test.ts",
        "src/mail.spec.js",
        "internal/mail/mail_test.go",
        "src/test/java/com/acme/MailTest.java",
        "tests/fixtures/users.json",
        "testdata/users.yaml",
        "src/test_reminders.py",
        "conftest.py",
        "spec/models/user_spec.rb",
    ])
    def test_matches(self, path):
        assert is_test_path(path)

    @pytest.mark.parametrize("path", [
        "src/library/reminders.py",
        "src/contest.py",
        "src/testing_mode.py",
        "src/special_offer.py",
        "docs/latest.md",
        "spec/openapi.yaml",
        "",
    ])
    def test_does_not_match(self, path):
        assert not is_test_path(path)


class TestIsFixturePiiFinding:
    @pytest.mark.parametrize("title,body", [
        (ISSUE_TITLE, ISSUE_BODY),
        ("Personal email in fixture", "Use a placeholder."),
        ("PII in test data", "Names and phone numbers of real people."),
        ("Developer's work address committed", "jane.doe@corp.test is a real e-mail."),
    ])
    def test_matches(self, title, body):
        assert is_fixture_pii_finding(finding(title=title, body=body))

    @pytest.mark.parametrize("title,body", [
        ("Hard-coded password in fixture", "The email fixture also holds a password."),
        ("Leaked private key", "An email and an SSH private key."),
        ("Customer email list in fixture", "Real customer addresses."),
        ("Production user data in fixture", "Email addresses copied from prod."),
        ("Missing tearDown", "The environment leaks."),
        ("Test never asserts the email was sent", "Check the outbox."),
        ("Mock mailer swallows the email send failure", "Raise instead."),
        ("Phone number parser test misses the empty case", "Add a case."),
        ("Real users cannot open settings", "The fixture hides the settings route."),
        ("Real people will see this stack trace", "Catch the error in the test helper."),
        ("Fixture email has a real-looking session token", "The session token is live."),
    ])
    def test_does_not_match(self, title, body):
        assert not is_fixture_pii_finding(finding(title=title, body=body))


class TestApplyFixturePiiCap:
    def test_lowers_only_error(self):
        out = apply_fixture_pii_cap([
            finding(), finding(severity="warning"), finding(severity="spec"),
            finding(severity="outofscope"),
        ])
        assert [f.severity for f in out] == ["warning", "warning", "spec", "outofscope"]

    def test_leaves_dropped_and_non_test_findings_alone(self):
        dropped = finding(drop_reason="confidence below floor")
        prod = finding(file=SRC_FILE)
        out = apply_fixture_pii_cap([dropped, prod])
        assert out == [dropped, prod]

    def test_is_one_to_one_and_order_preserving(self):
        items = [finding(file=SRC_FILE), finding(), finding(title="Missing tearDown", body="x")]
        out = apply_fixture_pii_cap(items)
        assert len(out) == 3
        assert [f.title for f in out] == [f.title for f in items]
