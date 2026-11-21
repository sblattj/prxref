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

import difflib
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
            ("src/a.ts", "/* old note", "/* new note"),
            ("src/a.ts", " * @param ms elapsed", " * @param ms elapsed time"),
            ("src/a.ts", " */", "  */"),
            ("src/a.py", "#old note", "#new note"),
            ("deploy/app.yaml", "#old", "#new"),
            ("Makefile", "#old", "#new"),
            ("CMakeLists.txt", "#old", "#new"),
        ):
            [out] = apply_comment_only_change_check([_f(file=path, line=1)], _files(_hunk_patch(path, minus, plus)))
            assert out.drop_reason == f"{COMMENT_ONLY_PREFIX}{path} @@ -1 +1", (path, minus)

    def test_moving_a_comment_boundary_is_a_code_change(self):
        for path, minus, plus in (
            ("src/a.ts", "/* old note", "/* new note */"),
            ("src/a.ts", " */", " *"),
            ("src/a.rs", "/* old note", "/* new note */"),
            ("src/a.html", "<!-- old", "<!-- new -->"),
        ):
            [out] = apply_comment_only_change_check([_f(file=path, line=1)], _files(_hunk_patch(path, minus, plus)))
            assert out.drop_reason is None, (path, minus)

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



def _diff(path: str, old: str, new: str) -> str:
    """A real unified diff of ``old`` to ``new`` (three lines of context)."""
    body = "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}",
    ))
    return f"diff --git a/{path} b/{path}\n{body}"


def _judge(path: str, old: str, new: str, line: int, *, head: bool = True) -> Finding:
    [out] = apply_comment_only_change_check(
        [_f(file=path, line=line)], _files(_diff(path, old, new)), (lambda p: new) if head else None,
    )
    return out


JSDOC_PATH = "src/time/ago.ts"
JSDOC_OLD = (
    "/**\n"
    " * Relative time for a duration in ms.\n"
    " * ABC-7\n"
    " */\n"
    "export function ago(ms: number): string {\n"
    "  const minutes = Math.max(Math.floor(ms / 60_000), 0);\n"
    "  return `${minutes}m ago`;\n"
    "}\n"
)
JSDOC_NEW = JSDOC_OLD.replace(" * ABC-7\n", " * Deprecated\n")

BLOCK_PATH = "src/time/round.c"
BLOCK_NEW = (
    "/*\n"
    "   Rounding notes for the minutes helper.\n"
    "   Every tier floors.\n"
    "   The seconds tier floors.\n"
    "   The hours tier floors.\n"
    "   Floor at every tier; round showed 30s as one minute.\n"
    "   The days tier floors.\n"
    "   The weeks tier floors.\n"
    "   The months tier floors.\n"
    "   End of notes.\n"
    "*/\n"
    "int minutes(long ms) { return (int)(ms / 60000); }\n"
)
BLOCK_OLD = BLOCK_NEW.replace("   Floor at every", "   ABC-9: floor at every")


def _drop(path: str, start: int) -> str:
    return f"{COMMENT_ONLY_PREFIX}{path} @@ -{start} +{start}"


class _HeadForge(FakeForge):
    """A fake forge that also serves the head revision of one file."""

    def __init__(self, diff: str, path: str, head: str):
        super().__init__(diff=diff)
        self._head = {path: head}

    def get_file_content(self, ref, path, sha=None):
        return self._head.get(path)


class _ScriptLLMAt(_ScriptLLM):
    """:class:`_ScriptLLM` answering for another file and line."""

    def __init__(self, path: str, line: int):
        super().__init__(line=line)
        self.path = path

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        res = super().invoke(system, user, max_tokens=max_tokens, json_mode=json_mode, timeout_s=timeout_s)
        return InvokeResult(
            text=res.text.replace(PATH, self.path), input_tokens=10, output_tokens=5,
            model="fake-model", backend="fake", elapsed_ms=1,
        )


def _review_head(path: str, old: str, new: str, line: int, *, head: bool = True):
    patch = _diff(path, old, new)
    forge = _HeadForge(patch, path, new) if head else FakeForge(diff=patch)
    return orchestrator.orchestrate_review(
        forge, REF, _ScriptLLMAt(path, line), post=False, max_workers=1, ci_wiring="off",
    )


class TestBlockComments:
    """Follow-up to #178: JSDoc ``*`` lines and bare block-comment body lines are comments."""

    def test_a_one_word_jsdoc_edit_next_to_an_unchanged_calc_is_dropped(self):
        for head in (True, False):
            out = _judge(JSDOC_PATH, JSDOC_OLD, JSDOC_NEW, 3, head=head)
            assert out.drop_reason == _drop(JSDOC_PATH, 1), head

    def test_the_jsdoc_edit_through_the_orchestrator(self):
        res = _review_head(JSDOC_PATH, JSDOC_OLD, JSDOC_NEW, 6, head=False)
        assert _ours(res["findings_active"]) == []
        assert [f.drop_reason for f in _ours(res["findings_dropped"])] == [_drop(JSDOC_PATH, 1)]

    def test_a_block_body_edit_with_the_opener_above_the_hunk_is_dropped(self):
        assert _judge(BLOCK_PATH, BLOCK_OLD, BLOCK_NEW, 6).drop_reason == _drop(BLOCK_PATH, 3)

    def test_the_block_body_edit_through_the_orchestrator_reader(self):
        res = _review_head(BLOCK_PATH, BLOCK_OLD, BLOCK_NEW, 6)
        assert _ours(res["findings_active"]) == []
        assert [f.drop_reason for f in _ours(res["findings_dropped"])] == [_drop(BLOCK_PATH, 3)]

    def test_without_the_head_file_a_bare_body_line_stays_conservative(self):
        assert _judge(BLOCK_PATH, BLOCK_OLD, BLOCK_NEW, 6, head=False).drop_reason is None
        res = _review_head(BLOCK_PATH, BLOCK_OLD, BLOCK_NEW, 6, head=False)
        assert [f.title for f in _ours(res["findings_active"])] == [TITLE]

    def test_a_real_code_change_keeps_the_finding(self):
        calc = "Math.floor(ms / 60_000)"
        jsdoc_old = JSDOC_OLD.replace(calc, "Math.round(ms / 60_000)")
        for head in (True, False):
            assert _judge(JSDOC_PATH, jsdoc_old, JSDOC_NEW, 3, head=head).drop_reason is None, head
        block_new = BLOCK_NEW.replace("   The hours tier floors.\n", "*/\nint hours = 1;\n/*\n")
        block_old = BLOCK_OLD.replace("   The hours tier floors.\n", "*/\nint hours = 0;\n/*\n")
        assert _judge(BLOCK_PATH, block_old, block_new, 8).drop_reason is None

    def test_commenting_an_unchanged_call_in_or_out_is_a_code_change(self):
        code = "function f() {\n  setup();\n  tick();\n  teardown();\n}\n"
        commented = code.replace("  tick();\n", "  /*\n  tick();\n  */\n")
        for path in ("src/a.ts", "src/a.rs"):
            for old, new in ((code, commented), (commented, code)):
                for head in (True, False):
                    assert _judge(path, old, new, 3, head=head).drop_reason is None, (path, head)

    def test_a_block_closed_above_the_hunk_leaves_body_shaped_lines_as_code(self):
        new = "/* header */\n" + "".join(f"int v{i} = {i};\n" for i in range(5)) + "total = v1;\n"
        old = new.replace("total = v1;", "total = v0;")
        assert _judge("src/a.c", old, new, 7).drop_reason is None

    def test_a_comment_opener_inside_a_string_or_regex_is_not_a_comment(self):
        body = "".join(BLOCK_NEW.splitlines(keepends=True)[1:])
        for path, opener in (
            ("src/a.ts", 'const glob = "src/**/*.ts";\n'),
            ("src/a.ts", "const re = /[/*]/;\n"),
            ("src/a.ts", "// see src/*\n"),
            ("src/a.c", "char star = '*'; char slash = '/'; /* ok */\n"),
            ("src/a.ts", "const doc = `\n"),
            ("src/a.go", "var doc = `\n"),
            ("src/a.java", 'String doc = """\n'),
        ):
            new = opener + body
            old = new.replace("   Floor at every", "   ABC-9: floor at every")
            assert _judge(path, old, new, 6).drop_reason is None, (path, opener)

    def test_nested_comment_languages_stay_conservative(self):
        for path in ("src/a.rs", "src/a.swift", "src/a.kt", "src/a.sql"):
            assert _judge(path, BLOCK_OLD, BLOCK_NEW, 6).drop_reason is None, path

    def test_jsx_text_holding_a_comment_opener_stays_conservative(self):
        new = (
            "export const Help = () => (\n  <p>\n    Type /* to start a note.\n"
            "    Then press enter.\n    Count: {n}\n  </p>\n);\n"
        )
        old = new.replace("Count: {n}", "Count: {n + 1}")
        for path in ("src/Help.jsx", "src/Help.tsx"):
            assert _judge(path, old, new, 5).drop_reason is None, path
