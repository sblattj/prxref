"""Issue #12 — package.json section misattribution: a devDependency reported
as a runtime dependency.

docs/issues/inbox-2026-09-04/prxref-issue-2026-09-03.md §2: on ``syf-mcp``
PR #45, prxref flagged ``vitest`` as a production dependency at
``src/apps/mcp/package.json:20`` — line 20 is ``"@syf-mcp/jenkins": "*",``, a
``dependencies`` entry, not the ``vitest`` line (which is line 37, inside
``devDependencies``). Posted as an OUTOFSCOPE comment, then retracted.

Frozen contract: ``prxref.quality.apply_manifest_claim_check(findings,
files: list[FileDiff]) -> list[Finding]`` for findings on package.json files
(basename match):
- Extract the claimed dependency key (first backticked or bare npm package
  name that also appears as a JSON key in the file's post-image hunk
  lines). If the anchored line's key differs from the claimed key, drop with
  ``drop_reason`` starting ``anchor mismatch:`` naming both keys.
- Else, if the claim asserts a section ("runtime dependencies", "production
  dependenc", "under `dependencies`", "in dependencies" vs
  "devDependencies") and the section enclosing the anchor differs from the
  claimed one, drop with ``drop_reason`` starting ``section mismatch:``
  naming claimed vs actual.
- Non-package.json findings, and findings with no extractable key, are
  untouched.

Mechanism finding (traced empirically with quality.apply_line_align against
the fixture below, not guessed): the existing content-pass realign
(quality._realign_member, called from apply_line_align via
prxref/quality.py:397) shares the token "dependencies" between BOTH the
``"dependencies": {`` and ``"devDependencies": {`` header lines (the latter
via the compound split of "devDependencies"), so its best-content-match
target is a HEADER line rather than the vitest content line. A finding
anchored at 20 realigns to 20 (a no-op, since the nearest added line to the
winning header is the anchor's own line). A finding anchored at 37 realigns
DOWN to 20 (the header's nearest added line is the jenkins entry, not
vitest, because 20 sits numerically closer to the devDependencies header at
26 than 37 does). So if ``apply_manifest_claim_check`` ran AFTER
``apply_line_align``, both a line-20 and a line-37 report would already be
anchored at 20 by the time the new check saw them, and BOTH would report
"anchor mismatch:" — Test B's "section mismatch:" case would be
unreachable. The fix must run BEFORE ``apply_line_align`` (between
prxref/orchestrator.py:453 ``apply_location_validation`` and :454
``apply_line_align``), operating on each finding's raw, LLM-reported anchor.

Tests A and B FAIL today (no such pass exists, so nothing drops either
finding). Test C is a direct unit-level guard on both anchor variants. The
controls are the no-op-shim pattern from test_issue_05_hedged_findings.py:
green today by construction, and bind the real gate once it lands.
"""
from __future__ import annotations

import json

import pytest

from prxref import quality
from prxref.orchestrator import orchestrate_review
from prxref.triage import Finding, parse_unified_diff
from tests.test_orchestrator import REF, FakeForge, FakeLLM  # noqa: F401

PKG_PATH = "src/apps/mcp/package.json"

# Post-image line map, exact (traced with tests/../parse_unified_diff, not
# guessed):
#   12  "version": "0.1.0",                        context
#   13  "dependencies": {                           context — dependencies header
#   14..19  six existing runtime deps               context
#   20  "@syf-mcp/jenkins": "*",                     ADDED (+) — the real finding's anchor
#   21..24  four more existing runtime deps          context
#   25  },                                           context — dependencies close
#   26  "devDependencies": {                         context — devDependencies header
#   27..36  ten existing dev deps                    context
#   37  "vitest": "^4.0.18",                         ADDED (+) — vitest's real line
#   38  }                                            context — devDependencies close
#
# old_count = 25 (25 context lines, 2 added lines added on top) and
# new_count = 27 (12..38 inclusive); nothing before line 12 changed, so
# old_start == new_start == 12. Hunk header: "@@ -12,25 +12,27 @@".
_HUNK_LINES = [
    '  "version": "0.1.0",',
    '  "dependencies": {',
    '    "@syf-mcp/core": "^1.0.0",',
    '    "@syf-mcp/logging": "^1.0.0",',
    '    "axios": "^1.6.0",',
    '    "commander": "^11.0.0",',
    '    "dotenv": "^16.3.0",',
    '    "express": "^4.18.0",',
    '+    "@syf-mcp/jenkins": "*",',
    '    "lodash": "^4.17.21",',
    '    "winston": "^3.11.0",',
    '    "yargs": "^17.7.0",',
    '    "zod": "^3.22.0",',
    '  },',
    '  "devDependencies": {',
    '    "@syf-mcp/eslint-config": "^1.0.0",',
    '    "@types/express": "^4.17.0",',
    '    "@types/node": "^20.0.0",',
    '    "eslint": "^8.50.0",',
    '    "prettier": "^3.0.0",',
    '    "ts-node": "^10.9.0",',
    '    "typescript": "^5.2.0",',
    '    "@types/jest": "^29.5.0",',
    '    "esbuild": "^0.19.0",',
    '    "nodemon": "^3.0.0",',
    '+    "vitest": "^4.0.18",',
    '  }',
]
assert sum(1 for ln in _HUNK_LINES if ln.startswith("+")) == 2
assert len(_HUNK_LINES) == 27  # new_count

PACKAGE_JSON_DIFF = (
    f"diff --git a/{PKG_PATH} b/{PKG_PATH}\n"
    f"--- a/{PKG_PATH}\n"
    f"+++ b/{PKG_PATH}\n"
    "@@ -12,25 +12,27 @@\n"
    + "\n".join(_HUNK_LINES) + "\n"
)

# Verbatim from docs/issues/inbox-2026-09-04/prxref-issue-2026-09-03.md §2,
# comment 1253956.
TITLE = "vitest added to runtime dependencies"
BODY = (
    "vitest is a test-only tool but is added under `dependencies` in "
    "src/apps/mcp/package.json, unlike src/packages/jenkins/package.json "
    "where it is correctly a devDependency. This bloats production "
    "installs/bundles."
)


def _finding(line: int) -> Finding:
    return Finding(
        file=PKG_PATH, line=line, severity="outofscope", confidence=0.8,
        title=TITLE, body=BODY,
    )


def _reasons_for(res: dict, title: str) -> list[str]:
    return [f.drop_reason or "" for f in res["findings_dropped"] if f.title == title]


class TestAAnchorAtJenkinsLine:
    """The real-world report: anchored at 20, the jenkins line — must be
    dropped for an ANCHOR key mismatch (claimed key ``vitest`` != anchor
    key ``@syf-mcp/jenkins``), which is the anchor-key check the frozen
    contract runs BEFORE the section check. Traced above: apply_line_align
    leaves a line-20 anchor at 20 (a no-op realign), so this holds whether
    the new pass runs before or after apply_line_align — it is Test B where
    the ordering bites.
    """

    def test_end_to_end(self):
        response = json.dumps({"findings": [
            {"file": PKG_PATH, "line": 20, "severity": "outofscope",
             "confidence": 0.8, "title": TITLE, "body": BODY},
        ]})
        forge = FakeForge(diff=PACKAGE_JSON_DIFF)
        res = orchestrate_review(forge, REF, FakeLLM(response), post=False)

        active_titles = {f.title for f in res["findings_active"]}
        assert TITLE not in active_titles, "false-positive vitest claim survived"
        reasons = _reasons_for(res, TITLE)
        assert reasons, f"{TITLE!r} not in findings_dropped"
        assert any(r.startswith("anchor mismatch:") for r in reasons), (
            f"expected 'anchor mismatch:', got {reasons!r}"
        )


class TestBAnchorAtVitestLine:
    """Same finding, anchored at 37 — the vitest line itself. The anchor
    key now matches the claim (``vitest`` == ``vitest``), so the drop must
    come from the SECTION check: claim says dependencies/runtime, actual
    section is devDependencies. This is the case that pins pass order: if
    apply_manifest_claim_check ran after apply_line_align, the realign
    traced above moves this anchor from 37 to 20 first, and the check would
    see an anchor-key mismatch instead — the wrong reason.
    """

    def test_end_to_end(self):
        response = json.dumps({"findings": [
            {"file": PKG_PATH, "line": 37, "severity": "outofscope",
             "confidence": 0.8, "title": TITLE, "body": BODY},
        ]})
        forge = FakeForge(diff=PACKAGE_JSON_DIFF)
        res = orchestrate_review(forge, REF, FakeLLM(response), post=False)

        active_titles = {f.title for f in res["findings_active"]}
        assert TITLE not in active_titles, "false-positive vitest claim survived"
        reasons = _reasons_for(res, TITLE)
        assert reasons, f"{TITLE!r} not in findings_dropped"
        assert any(r.startswith("section mismatch:") for r in reasons), (
            f"expected 'section mismatch:', got {reasons!r}"
        )


class TestCDirectUnit:
    """Direct unit call on both anchor variants, no orchestrator involved."""

    def test_apply_manifest_claim_check_exists_and_classifies_both(self):
        if not hasattr(quality, "apply_manifest_claim_check"):
            pytest.fail(
                "prxref.quality.apply_manifest_claim_check does not exist "
                "yet — issue #12's fix is not implemented"
            )
        files = parse_unified_diff(PACKAGE_JSON_DIFF)

        out_a = quality.apply_manifest_claim_check([_finding(20)], files)
        assert len(out_a) == 1
        assert out_a[0].drop_reason is not None, "line-20 finding not dropped"
        assert out_a[0].drop_reason.startswith("anchor mismatch:"), out_a[0].drop_reason

        out_b = quality.apply_manifest_claim_check([_finding(37)], files)
        assert len(out_b) == 1
        assert out_b[0].drop_reason is not None, "line-37 finding not dropped"
        assert out_b[0].drop_reason.startswith("section mismatch:"), out_b[0].drop_reason


def _claim_check(findings, files):
    """The frozen entry point, or an identity shim before it exists.

    Mirrors test_issue_05_hedged_findings.py's TestCControls pattern: green
    today by construction (the shim never drops anything), and the same
    assertions bind the real gate once ``apply_manifest_claim_check`` lands.
    """
    fn = getattr(quality, "apply_manifest_claim_check", lambda fs, fl: list(fs))
    return fn(findings, files)


class TestDControls:
    """Legitimate / out-of-scope findings must stay untouched, now and after."""

    def test_correct_runtime_claim_stays_active(self):
        files = parse_unified_diff(PACKAGE_JSON_DIFF)
        f = Finding(
            file=PKG_PATH, line=20, severity="warning", confidence=0.7,
            title="@syf-mcp/jenkins pinned to * in dependencies",
            body=(
                "The version is pinned to `*` instead of a specific range, "
                "so installs are not reproducible."
            ),
        )
        out = _claim_check([f], files)
        assert len(out) == 1
        assert out[0].drop_reason is None, out[0].drop_reason

    def test_non_package_json_file_untouched(self):
        files = parse_unified_diff(PACKAGE_JSON_DIFF)
        f = Finding(
            file="src/index.ts", line=5, severity="warning", confidence=0.7,
            title="Unused import",
            body="`lodash` is imported here but never used.",
        )
        out = _claim_check([f], files)
        assert len(out) == 1
        assert out[0].drop_reason is None, out[0].drop_reason

    def test_correct_devdependency_claim_stays_active(self):
        files = parse_unified_diff(PACKAGE_JSON_DIFF)
        f = Finding(
            file=PKG_PATH, line=37, severity="outofscope", confidence=0.8,
            title="vitest ^4.0.18 added to devDependencies",
            body="This adds `vitest` as a test runner dependency for the mcp app.",
        )
        out = _claim_check([f], files)
        assert len(out) == 1
        assert out[0].drop_reason is None, out[0].drop_reason
