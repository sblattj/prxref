"""A comment-only hunk is not a logic change (#178).

A PR edits only a comment above an unchanged calculation: it drops a tracker
prefix from a note that mentions the old rounding. The model read the note and
reported "relative-time logic changed (floor vs round)", anchored on the
calculation, which is identical on the base branch. A behavior-change claim
needs a changed code line; :func:`prxref.quality.apply_comment_only_change_check`
drops it when every ``+``/``-`` line of its hunk is a comment or blank.

The orchestrator test runs the REAL reviewer and passes over the issue's own
diff with a scripted LLM; no test touches the network.
"""

from __future__ import annotations

import json

import pytest

from prxref import orchestrator
from prxref.llm import InvokeResult
from prxref.quality import COMMENT_ONLY_PREFIX, apply_comment_only_change_check
from prxref.triage import Finding, parse_unified_diff
from tests.test_orchestrator import REF, FakeForge

SWEEP_MARK = "running a systemic sweep"
NO_FINDINGS = json.dumps({"findings": []})
PATH = "src/time/ago.ts"
PATCH = (
    f"diff --git a/{PATH} b/{PATH}\n"
    f"--- a/{PATH}\n"
    f"+++ b/{PATH}\n"
    "@@ -1,5 +1,5 @@\n"
    " export function ago(ms: number): string {\n"
    '-  // ABC-123: floor at every tier; Math.round showed 30s as "1m ago".\n'
    '+  // Floor at every tier; Math.round showed 30s as "1m ago".\n'
    "   const minutes = Math.max(Math.floor(ms / 60_000), 0);\n"
    '   return minutes < 1 ? "just now" : `${minutes}m ago`;\n'
    " }\n"
)
TITLE = "relative-time logic changed (floor vs round)"
BODY = (
    "The comment now says the function floors at every tier where it used Math.round, so "
    "`Math.floor(ms / 60_000)` changes how 30s renders: callers that expected \"1m ago\" now see "
    "\"just now\"."
)
DROP = f"{COMMENT_ONLY_PREFIX}{PATH} @@ -1 +1"


class _ScriptLLM:
    """Answers the chunk with the issue's finding on the unchanged calculation line; the sweep with nothing."""

    def __init__(self, title: str = TITLE, line: int = 3):
        self.title = title
        self.line = line

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        if SWEEP_MARK in system:
            text = NO_FINDINGS
        else:
            text = json.dumps({"findings": [{
                "file": PATH, "line": self.line, "severity": "error", "confidence": 0.9,
                "title": self.title, "body": BODY,
            }]})
        return InvokeResult(
            text=text, input_tokens=10, output_tokens=5, model="fake-model", backend="fake", elapsed_ms=1,
        )


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    monkeypatch.setattr(orchestrator, "_elapsed_ms", lambda t0: 0)


def _review(patch: str = PATCH, llm=None):
    forge = FakeForge(diff=patch)
    return orchestrator.orchestrate_review(
        forge, REF, llm or _ScriptLLM(), post=False, max_workers=1, ci_wiring="off",
    )


def _ours(findings) -> list[Finding]:
    return [f for f in findings if f.title == TITLE]


class TestTheIssueThroughTheOrchestrator:
    def test_the_logic_change_claim_on_a_comment_only_hunk_is_dropped(self):
        res = _review()
        assert _ours(res["findings_active"]) == []
        dropped = _ours(res["findings_dropped"])
        assert [f.drop_reason for f in dropped] == [DROP]
        assert res["verdict"] != "Request-Changes"

    def test_the_same_claim_stays_when_the_calculation_line_changed(self):
        patch = PATCH.replace(
            "   const minutes = Math.max(Math.floor(ms / 60_000), 0);\n",
            "-  const minutes = Math.max(Math.round(ms / 60_000), 0);\n"
            "+  const minutes = Math.max(Math.floor(ms / 60_000), 0);\n",
        ).replace("@@ -1,5 +1,5 @@", "@@ -1,6 +1,6 @@")
        res = _review(patch)
        assert [f.title for f in _ours(res["findings_active"])] == [TITLE]


def _files(patch: str = PATCH):
    return parse_unified_diff(patch)


def _f(**kw) -> Finding:
    base = dict(file=PATH, line=3, severity="error", title=TITLE, body=BODY, confidence=0.9)
    base.update(kw)
    return Finding(**base)


def _hunk_patch(path: str, minus: str, plus: str, context: str = "x = compute(1)") -> str:
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        "@@ -1,2 +1,2 @@\n"
        f"-{minus}\n+{plus}\n {context}\n"
    )


class TestTheCheck:
    def test_the_issue_finding_is_dropped(self):
        [out] = apply_comment_only_change_check([_f()], _files())
        assert out.drop_reason == DROP

    def test_a_file_level_claim_drops_when_every_hunk_is_comments(self):
        [out] = apply_comment_only_change_check([_f(line=0)], _files())
        assert out.drop_reason == DROP

    def test_a_finding_about_the_comment_stays(self):
        for title in (
            "Comment still mentions Math.round",
            "Typo in note",
            "Stale wording about rounding",
        ):
            [out] = apply_comment_only_change_check([_f(title=title)], _files())
            assert out.drop_reason is None, title

    def test_an_outofscope_or_claimless_finding_stays(self):
        [a] = apply_comment_only_change_check([_f(severity="outofscope")], _files())
        [b] = apply_comment_only_change_check(
            [_f(title="Negative ms yields 0m", body="Math.max hides a negative input.")], _files(),
        )
        assert a.drop_reason is None and b.drop_reason is None

    def test_a_defect_in_unchanged_code_is_not_a_change_claim(self):
        for title, body in (
            ("Minutes clamp hides negative input", "The comment changed, but the calculation is still wrong."),
            ("Negative ms renders just now", "This rounding was already wrong before the PR changed anything."),
            ("Rounding logic did not change", "The calculation never changed; the clamp hides negative ms."),
            ("Clamp hides negative ms", "Floor vs round doesn't change here, but negative input renders as just now."),
            ("Clamp is pre-existing", "A pre-existing clamp changed nothing here; negative input is hidden."),
        ):
            [out] = apply_comment_only_change_check([_f(title=title, body=body)], _files())
            assert out.drop_reason is None, title

    def test_a_change_claim_in_the_body_alone_is_dropped(self):
        [out] = apply_comment_only_change_check(
            [_f(title="Relative time renders differently", body="The calculation switched from round to floor.")],
            _files(),
        )
        assert out.drop_reason == DROP

    def test_a_hunk_with_a_changed_code_line_keeps_the_finding(self):
        patch = _hunk_patch(PATH, "  const n = Math.round(ms);", "  const n = Math.floor(ms);")
        [out] = apply_comment_only_change_check([_f(line=1)], _files(patch))
        assert out.drop_reason is None

    def test_code_shaped_lines_count_as_code(self):
        for path, minus, plus in (
            ("src/a.c", "#include <a.h>", "#include <b.h>"),
            ("src/a.c", "*p = 1;", "*p = 2;"),
            ("src/a.js", "#count = 0;", "#count = 1;"),
            ("src/a.c", "#define N 1", "#define N 2"),
            ("src/a.c", "# include <old.h>", "# include <new.h>"),
            ("src/a.c", "-- i;", "-- j;"),
            ("src/a.ts", "# old", "# new"),
            ("src/a.c", "/* note */ value = 1;", "/* note */ value = 2;"),
            ("src/a.py", "    * 2", "    * 3"),
            ("src/a.js", "  * rate * 2", "  * rate * 3"),
            ("src/a.js", "  * factor)", "  * scale)"),
            ("bin/run.sh", "#!/bin/sh", "#!/bin/bash"),
            ("src/a.py", "x = 1  # note", "x = 2  # note"),
            ("docs/a.md", "# Old title", "# New title"),
            ("notes.txt", "# Old", "# New"),
        ):
            [out] = apply_comment_only_change_check([_f(file=path, line=1)], _files(_hunk_patch(path, minus, plus)))
            assert out.drop_reason is None, (path, minus)

    def test_other_comment_syntaxes_are_comment_only(self):
        for path, minus, plus in (
            ("src/a.py", "# old note", "# new note"),
            ("src/a.sql", "-- old note", "-- new note"),
            ("src/a.java", " * old javadoc", " * new javadoc"),
            ("src/a.html", "<!-- old -->", "<!-- new -->"),
            ("src/a.ts", "/* old */", ""),
            ("src/a.ts", "/* old note", "/* new note */"),
            ("src/a.ts", " * @param ms elapsed", " * @param ms elapsed time"),
            ("src/a.ts", " */", " *"),
            ("src/a.py", "#old note", "#new note"),
            ("deploy/app.yaml", "#old", "#new"),
            ("Makefile", "#old", "#new"),
            ("CMakeLists.txt", "#old", "#new"),
        ):
            [out] = apply_comment_only_change_check([_f(file=path, line=1)], _files(_hunk_patch(path, minus, plus)))
            assert out.drop_reason == f"{COMMENT_ONLY_PREFIX}{path} @@ -1 +1", (path, minus)

    def test_a_comment_shaped_line_inside_a_multiline_string_is_data(self):
        for path, context in (
            ("src/a.py", 'QUERY = """'),
            ("src/a.ts", "const sql = `"),
        ):
            patch = (
                f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
                f"@@ -1,2 +1,2 @@\n {context}\n-# old\n+# new\n"
            )
            if path.endswith(".ts"):
                patch = patch.replace("-# old\n+# new", "-// old\n+// new")
            [out] = apply_comment_only_change_check([_f(file=path, line=2)], _files(patch))
            assert out.drop_reason is None, path

    def test_a_hunk_inside_a_string_opened_above_it_is_data(self):
        path = "src/a.py"
        patch = (
            f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            "@@ -3,2 +3,2 @@\n-# old\n+# new\n SELECT 1\n"
        )
        opened = 'QUERY = """\nSELECT 0\n# new\nSELECT 1\n"""\n'
        closed = '"""Docstring."""\nx = 1\n# new\nSELECT 1\n'
        [inside] = apply_comment_only_change_check([_f(file=path, line=3)], _files(patch), lambda p: opened)
        [outside] = apply_comment_only_change_check([_f(file=path, line=3)], _files(patch), lambda p: closed)
        [unread] = apply_comment_only_change_check([_f(file=path, line=3)], _files(patch), lambda p: None)
        assert inside.drop_reason is None
        assert outside.drop_reason == f"{COMMENT_ONLY_PREFIX}{path} @@ -3 +3"
        assert unread.drop_reason == outside.drop_reason

    def test_a_raising_reader_judges_the_hunk_alone(self):
        def boom(path):
            raise RuntimeError("down")
        [out] = apply_comment_only_change_check([_f()], _files(), boom)
        assert out.drop_reason == DROP

    def test_a_line_outside_every_hunk_is_left_alone(self):
        [out] = apply_comment_only_change_check([_f(line=40)], _files())
        assert out.drop_reason is None

    def test_deterministic_and_dropped_findings_pass_through(self):
        det = _f(body="x (deterministic check, no model)")
        gone = _f(drop_reason="earlier")
        out = apply_comment_only_change_check([det, gone], _files())
        assert out[0].drop_reason is None and out[1].drop_reason == "earlier"

    def test_order_and_length_are_preserved(self):
        items = [_f(title="Typo in note"), _f(), _f(file="src/other.ts")]
        out = apply_comment_only_change_check(items, _files())
        assert [f.title for f in out] == [f.title for f in items]
        assert [f.drop_reason is not None for f in out] == [False, True, False]
