"""Cap personal-data findings in test fixtures at ``warning`` (#181).

A unit test that pins a sender to a developer's own address, or seeds a
fixture with a made-up name and phone number, never reaches a real mail
system or a real user. A model still tends to report it as ``error``
("Replace real employee email addresses in test fixtures"), which alone
flips the verdict to Request-Changes. This check lowers such a finding from
``error`` to ``warning``: it still posts as a nudge to use an
``example.test`` address, but it no longer blocks.

A finding is lowered only when all of these hold:

- it is active, its severity is ``error``, and it is not a deterministic
  check's own finding;
- its file is a test path (:func:`is_test_path`);
- its title or body talks about personal data (:data:`PII_RE`) and never
  about a secret, a credential or customer or production data
  (:data:`KEEP_TITLE_RE`, :data:`KEEP_BODY_RE`), which keep their ``error``.

Detection is by text, since nothing tags a personal-data finding. It never
drops a finding and never changes anything but ``severity``. Always on, no
key; it is not one of the numbered passes of :mod:`prxref.quality`.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import PurePosixPath

from .heuristics import is_deterministic
from .triage import Finding

CAPPED_SEVERITY = "warning"
"""The tier a lowered finding takes."""

_TEST_DIRS = frozenset({
    "test", "tests", "__tests__", "testdata", "test-data",
    "test_data", "fixture", "fixtures", "__fixtures__", "__mocks__",
})
_TEST_BASENAME_RE = re.compile(
    r"^(?:test_.+|.+_test|.+_spec|.+[.-](?:test|spec)s?|.+[a-z0-9](?:Test|Tests|Spec)"
    r"|conftest)\.[A-Za-z0-9]+$"
)

_PII_WORDS = r"e-?mails?|e-?mail\s+address(?:es)?|address(?:es)?|names?|phone(?:\s+numbers?)?"
_PII_NOUN = f"(?:{_PII_WORDS})"

PII_RE = re.compile(
    r"\bpii\b|\bpersonally[\s-]+identifiable\b"
    rf"|\bpersonal\s+(?:data|information|info|details|{_PII_WORDS})\b"
    r"|\b(?:employee|developer|author|staff|colleague|person|someone)(?:'s|s)?\s+"
    rf"(?:(?:own|real|work|personal)\s+)*{_PII_NOUN}\b"
    rf"|\b(?:own|work)\s+{_PII_NOUN}\b"
    rf"|\breal\s+(?:persons?|people|employees?|developers?|users?|{_PII_WORDS})\b"
    rf"|\bpeople's\s+{_PII_NOUN}\b",
    re.IGNORECASE,
)
"""Matches finding text about a real person's data: a real or work email, a personal name, PII.

A bare "email" is not enough: "the test never asserts the email was sent" is
about behaviour, not personal data.
"""

_CREDENTIAL = (
    r"\bpasswords?\b|\bpasswd\b|\bcredentials?\b|\bapi[\s_-]?keys?\b|\bprivate\s+keys?\b"
    r"|\bssh\s+keys?\b|\baccess\s+keys?\b|\bsecret\s+keys?\b"
    r"|\b(?:auth|access|bearer|api|refresh|session)\s+tokens?\b"
    r"|\bssn\b|\bsocial\s+security\b|\bcredit\s+cards?\b|\bcard\s+numbers?\b"
    r"|\bcustomers?'?\s+(?:data|records?|e-?mails?|address(?:es)?|information|pii|lists?)\b"
    r"|\bpatients?\b|\bmedical\b"
)
KEEP_BODY_RE = re.compile(_CREDENTIAL, re.IGNORECASE)
"""Body text that keeps ``error``: a credential, or customer, card or medical data."""

KEEP_TITLE_RE = re.compile(
    _CREDENTIAL + r"|\bsecrets?\b|\btokens?\b|\bcustomers?\b|\bclients?\b|\bproduction\b|\bprod\b",
    re.IGNORECASE,
)
"""Title text that keeps ``error``: everything :data:`KEEP_BODY_RE` matches, plus a bare
secret, token, customer, client, production or prod. The body is held to the narrower
list because a model's explanation often says "could mail real people against a
production relay" or "not a secret" about a fixture address."""


def is_test_path(path: str) -> bool:
    """True when ``path`` sits in a test or fixture directory or names a test file.

    ``tests/test_mail.py``, ``pkg/__tests__/mail.ts``, ``src/mail.spec.js``,
    ``mail_test.go``, ``MailTest.java`` and ``tests/fixtures/users.json``
    are test paths; ``src/contest.py`` and ``src/testing_mode.py`` are not.
    """
    if not path:
        return False
    p = PurePosixPath(path.replace("\\", "/"))
    if any(part.lower() in _TEST_DIRS for part in p.parts[:-1]):
        return True
    return bool(_TEST_BASENAME_RE.match(p.name))


def is_fixture_pii_finding(finding: Finding) -> bool:
    """True when the finding's text is about personal data and not a secret or customer data."""
    title, body = finding.title or "", finding.body or ""
    if not PII_RE.search(f"{title}\n{body}"):
        return False
    return not (KEEP_TITLE_RE.search(title) or KEEP_BODY_RE.search(body))


def apply_fixture_pii_cap(findings: Sequence[Finding]) -> list[Finding]:
    """Lower each active personal-data ``error`` on a test path to ``warning``.

    One output per input, in input order; nothing is dropped and only
    ``severity`` changes.
    """
    out: list[Finding] = []
    for f in findings:
        if (
            f.drop_reason is None
            and (f.severity or "").strip().lower() == "error"
            and not is_deterministic(f)
            and is_test_path(f.file)
            and is_fixture_pii_finding(f)
        ):
            out.append(replace(f, severity=CAPPED_SEVERITY))
        else:
            out.append(f)
    return out
