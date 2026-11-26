"""Issue 206: a source module with "test" in its name is not an unwired test.

A PR added ``src/lib/<feature>-test-version.ts``, an ordinary module that
exports helpers and that a ``.tsx`` component in the same PR imports. The CI
wiring check flagged it as "a new test file outside the runner's default
include", because the test-name branch of ``candidate_checks`` was the one
branch without the library guard (:func:`prxref.ci_wiring._is_library`) the
name-hint branch already had. A test-named file with no importer, or with an
entry point, still counts.
"""
from __future__ import annotations

from prxref import ci_wiring
from prxref.triage import parse_unified_diff

LIB = "src/lib/feature-test-version.ts"
COMPONENT = "src/components/FeatureBanner.tsx"
CI = ".github/workflows/ci.yml"
CI_TEXT = "jobs:\n  t:\n    steps:\n      - run: npm ci\n      - run: npm run build\n"


def _added(path: str, lines: list[str]) -> str:
    body = "\n".join(f"+{line}" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n"
        f"+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}\n"
    )


MODULE = [
    "export function isTestVersion(v: string): boolean {",
    "  return v.endsWith('-test');",
    "}",
    "export const TEST_VERSION_PREFIX = 'tv';",
]
IMPORTER = [
    "import { isTestVersion } from '@/lib/feature-test-version';",
    "export function FeatureBanner({ v }: { v: string }) {",
    "  return isTestVersion(v) ? <span>beta</span> : null;",
    "}",
]


def _findings(diff: str):
    files = parse_unified_diff(diff)
    return ci_wiring.ci_wiring_findings(files, read={CI: CI_TEXT}.get, listing=[CI])


def test_the_issue_repro_raises_no_finding():
    findings, record = _findings(_added(LIB, MODULE) + _added(COMPONENT, IMPORTER))
    assert findings == []
    assert record["candidates"] == []


def test_a_relative_import_also_marks_it_a_library():
    importer = ["import { isTestVersion } from '../lib/feature-test-version.ts';", *IMPORTER[1:]]
    findings, _ = _findings(_added(LIB, MODULE) + _added(COMPONENT, importer))
    assert findings == []


def test_with_no_importer_it_is_still_flagged():
    findings, record = _findings(_added(LIB, MODULE))
    assert [f.file for f in findings] == [LIB]
    assert "a new test file outside the runner's default include" in findings[0].body
    assert record["candidates"] == [LIB]


def test_with_an_entry_point_it_is_still_flagged():
    entry = [*MODULE, "if (import.meta.main) { console.log(isTestVersion('x')); }"]
    findings, _ = _findings(_added(LIB, entry) + _added(COMPONENT, IMPORTER))
    assert [f.file for f in findings] == [LIB]
