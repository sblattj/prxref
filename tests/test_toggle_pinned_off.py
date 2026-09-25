"""Unit tests for ``heuristics.toggle_pinned_off_findings`` (issue #22, proposal 3).

A PR adds a toggle that defaults on and, in the same PR, a suite-wide test
setup file that turns it off, so the green suite never runs the shipped
default. Every FileDiff here comes out of the production parser,
``triage.parse_unified_diff``, over hand-written diff text, so added,
context and removed lines are exactly what a real review would see.
"""
from __future__ import annotations

import pytest

from prxref import markers
from prxref.heuristics import _BODY_SUFFIX, toggle_pinned_off_findings
from prxref.quality import apply_hedge_gate, apply_line_align
from prxref.triage import FileDiff, added_lines_by_file, parse_unified_diff

PROGRESS_PY = [
    '"""Progress notes: a short user-facing line announced before each step runs.',
    "",
    'Each note is saved as a message (role "progress") so the transcript holds exactly what the',
    "user saw. A per-turn ledger on the root frame drops a note that repeats one already shown.",
    '"""',
    "from assistant.features import enabled",
    "from assistant.messages import Message, MessageTable",
    "from assistant.session import Run",
    "",
    'LEDGER_KEY = "progress.ledger"',
    "",
    "",
    "class ProgressLedger:",
    '    """Notes shown to the user during one turn."""',
    "",
    "    def __init__(self, turn_id: str) -> None:",
    "        self._turn_id = turn_id",
    "        self._lines: list[str] = []",
    "",
    "    def turn_id(self) -> str:",
    "        return self._turn_id",
    "",
    "    def record(self, line: str) -> bool:",
    "        key = line.strip().lower()",
    "        if any(shown.strip().lower() == key for shown in self._lines):",
    "            return False",
    "        self._lines.append(line)",
    "        return True",
    "",
    "",
    "class ProgressAnnouncer:",
    "    def __init__(self, table: MessageTable) -> None:",
    "        self.table = table",
    '        self.active = enabled("progress_notes", default=True)',
    "",
    "    def announce(self, run: Run, text: str) -> None:",
    "        if not self.active:",
    "            return",
    "        if self._ledger(run).record(text):",
    '            self.table.append(Message(run.session_id, "progress", text))',
]

CONFTEST_PY = [
    "import pytest",
    "",
    "",
    "@pytest.fixture(autouse=True)",
    "def _progress_notes_off(monkeypatch):",
    "    # Keep exact message-table assertions deterministic; progress tests opt in.",
    '    monkeypatch.setenv("ASSISTANT_PROGRESS_NOTES", "false")',
]

TOGGLE = '        self.active = enabled("progress_notes", default=True)'
PIN = '    monkeypatch.setenv("ASSISTANT_PROGRESS_NOTES", "false")'
TOGGLE_LINE = PROGRESS_PY.index(TOGGLE) + 1


def _section(path: str, lines: list[str], *, new_file: bool = False) -> str:
    """One file's diff section; each line already carries its ``+``/``-``/`` `` prefix."""
    old = sum(1 for ln in lines if ln[0] in " -")
    new = sum(1 for ln in lines if ln[0] in " +")
    if new_file:
        head = f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{new} @@\n"
    else:
        head = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,{old} +1,{new} @@\n"
    return head + "".join(ln + "\n" for ln in lines)


def _added_file(path: str, lines: list[str]) -> str:
    """A diff section that adds ``path`` with ``lines`` as its whole content."""
    return _section(path, ["+" + ln for ln in lines], new_file=True)


def _files(*sections: str) -> list[FileDiff]:
    return parse_unified_diff("".join(sections))


def _fixture_files() -> list[FileDiff]:
    return _files(
        _added_file("assistant/progress.py", PROGRESS_PY),
        _added_file("tests/conftest.py", CONFTEST_PY),
    )


class TestIssue22Fixture:
    """The #22 fixture's own lines: progress.py adds the toggle, conftest.py pins it off."""

    def test_exactly_one_warning_at_the_toggle_line(self):
        findings = toggle_pinned_off_findings(_fixture_files())
        assert len(findings) == 1
        f = findings[0]
        assert f.file == "assistant/progress.py"
        assert f.line == TOGGLE_LINE
        assert f.severity == "warning"
        assert f.confidence == 1.0
        assert f.drop_reason is None
        assert f.rule is None
        assert f.body.endswith(_BODY_SUFFIX)

    def test_body_says_what_happened_and_names_both_files(self):
        f = toggle_pinned_off_findings(_fixture_files())[0]
        assert "progress_notes" in f.title
        assert '`enabled("progress_notes", default=True)`' in f.body
        assert "assistant/progress.py" in f.body
        assert 'tests/conftest.py sets `ASSISTANT_PROGRESS_NOTES` to "false"' in f.body
        assert "on by default" in f.body
        assert "whole suite" in f.body
        assert "do not exercise the shipped default" in f.body

    def test_text_is_ascii_with_no_glyph(self):
        f = toggle_pinned_off_findings(_fixture_files())[0]
        text = f.title + f.body
        assert all(ord(ch) < 128 for ch in text)
        glyphs = [*markers.SEVERITY_MARKERS.values(), markers.OUT_OF_TICKET_MARKER]
        assert not any(g in text for g in glyphs)

    def test_anchor_survives_line_alignment(self):
        files = _fixture_files()
        findings = toggle_pinned_off_findings(files)
        aligned = apply_line_align(findings, added_lines_by_file(files), files=files)
        assert [f.line for f in aligned] == [TOGGLE_LINE]

    def test_body_is_not_hedged(self):
        findings = toggle_pinned_off_findings(_fixture_files())
        assert [f.drop_reason for f in apply_hedge_gate(findings)] == [None]

    def test_pure_and_order_independent(self):
        files = _fixture_files()
        first = toggle_pinned_off_findings(files)
        assert toggle_pinned_off_findings(files) == first
        assert toggle_pinned_off_findings(list(reversed(files))) == first


class TestNegatives:
    """Every shape that must stay silent."""

    def test_no_pin(self):
        assert toggle_pinned_off_findings(_files(_added_file("assistant/progress.py", PROGRESS_PY))) == []

    def test_pin_sets_true(self):
        conftest = [ln.replace('"false"', '"true"') for ln in CONFTEST_PY]
        assert conftest != CONFTEST_PY
        files = _files(
            _added_file("assistant/progress.py", PROGRESS_PY),
            _added_file("tests/conftest.py", conftest),
        )
        assert toggle_pinned_off_findings(files) == []

    def test_pin_in_a_plain_test_body(self):
        files = _files(
            _added_file("assistant/progress.py", PROGRESS_PY),
            _added_file("tests/test_x.py", ["def test_quiet(monkeypatch):", PIN]),
        )
        assert toggle_pinned_off_findings(files) == []

    def test_toggle_on_a_context_line(self):
        files = _files(
            _section("assistant/progress.py", [
                " class ProgressAnnouncer:",
                "     def __init__(self, table: MessageTable) -> None:",
                "         self.table = table",
                " " + TOGGLE,
                "+        self.muted = False",
            ]),
            _added_file("tests/conftest.py", CONFTEST_PY),
        )
        assert toggle_pinned_off_findings(files) == []

    def test_pin_on_a_context_line(self):
        files = _files(
            _added_file("assistant/progress.py", PROGRESS_PY),
            _section("tests/conftest.py", [
                " @pytest.fixture(autouse=True)",
                " def _progress_notes_off(monkeypatch):",
                " " + PIN,
                '+    monkeypatch.setenv("ASSISTANT_LOG_LEVEL", "debug")',
            ]),
        )
        assert toggle_pinned_off_findings(files) == []

    def test_toggle_on_a_removed_line(self):
        files = _files(
            _section("assistant/progress.py", [
                " class ProgressAnnouncer:",
                "-" + TOGGLE,
                "+" + TOGGLE.replace("default=True", "default=False"),
            ]),
            _added_file("tests/conftest.py", CONFTEST_PY),
        )
        assert toggle_pinned_off_findings(files) == []

    @pytest.mark.parametrize("pin_name", [
        "ASSISTANT_DARK_MODE",
        "NOTES",
        "PROGRESS_NOTES_EXTRA",
        "ASSISTANT_XPROGRESS_NOTES",
        "PROGRESSNOTES",
    ])
    def test_pin_name_does_not_end_with_the_toggle_name(self, pin_name):
        files = _files(
            _added_file("assistant/progress.py", PROGRESS_PY),
            _added_file("tests/conftest.py", [PIN.replace("ASSISTANT_PROGRESS_NOTES", pin_name)]),
        )
        assert toggle_pinned_off_findings(files) == []

    def test_toggle_defaults_false(self):
        progress = [ln.replace("default=True", "default=False") for ln in PROGRESS_PY]
        assert progress != PROGRESS_PY
        files = _files(
            _added_file("assistant/progress.py", progress),
            _added_file("tests/conftest.py", CONFTEST_PY),
        )
        assert toggle_pinned_off_findings(files) == []

    @pytest.mark.parametrize("toggle", [
        'settings.set("progress_notes", True)',
        'prefs.putBoolean("progress_notes", true)',
        'config.set_flag("progress_notes", True)',
        'enabled("progress_notes", True, strict=True)',
        'enabled(progress_notes, default=True)',
        'monkeypatch.setenv("PROGRESS_NOTES", "true")',
    ])
    def test_not_a_default_on_toggle(self, toggle):
        files = _files(
            _added_file("assistant/progress.py", [toggle]),
            _added_file("tests/conftest.py", CONFTEST_PY),
        )
        assert toggle_pinned_off_findings(files) == []

    @pytest.mark.parametrize("path", [
        "tests/conftest_helpers.py",
        "tests/test_conftest.py",
        "tests/setup.py",
        "web/jest.config.js",
        "web/setupTests",
        "web/vitest.config.ts",
    ])
    def test_not_a_test_setup_file(self, path):
        files = _files(
            _added_file("assistant/progress.py", PROGRESS_PY),
            _added_file(path, [PIN]),
        )
        assert toggle_pinned_off_findings(files) == []

    def test_a_comparison_is_not_a_pin(self):
        files = _files(
            _added_file("web/src/progress.ts", ['const on = process.env.PROGRESS_NOTES ?? "true";']),
            _added_file("web/src/setupTests.ts", ['if (process.env.PROGRESS_NOTES == "false") {}']),
        )
        assert toggle_pinned_off_findings(files) == []


class TestSupportedForms:
    """One positive per toggle shape and per pin shape, as the docstring lists them."""

    @pytest.mark.parametrize(("path", "toggle", "call", "name"), [
        ("app/progress.py", 'on = enabled("progress_notes", default=True)',
         'enabled("progress_notes", default=True)', "progress_notes"),
        ("app/progress.py", 'on = flag("progress_notes", True)', 'flag("progress_notes", True)', "progress_notes"),
        ("web/progress.ts", 'const on = features.isEnabled("progress-notes", true);',
         'features.isEnabled("progress-notes", true)', "progress-notes"),
        ("app/Progress.kt", 'val on = Flags.flag("progress.notes", default = true)',
         'Flags.flag("progress.notes", default = true)', "progress.notes"),
        ("app/progress.py", 'on = os.getenv("APP_PROGRESS_NOTES", "true") == "true"',
         'os.getenv("APP_PROGRESS_NOTES", "true")', "APP_PROGRESS_NOTES"),
        ("app/progress.py", "on = os.environ.get('PROGRESS_NOTES', 'True')",
         "os.environ.get('PROGRESS_NOTES', 'True')", "PROGRESS_NOTES"),
        ("app/Progress.java", 'boolean on = Boolean.parseBoolean(System.getProperty("app.progress.notes", "true"));',
         'System.getProperty("app.progress.notes", "true")', "app.progress.notes"),
        ("web/progress.ts", 'const on = process.env.PROGRESS_NOTES ?? "true";',
         'process.env.PROGRESS_NOTES ?? "true"', "PROGRESS_NOTES"),
        ("web/progress.js", "const on = process.env['PROGRESS_NOTES'] || 'true';",
         "process.env['PROGRESS_NOTES'] || 'true'", "PROGRESS_NOTES"),
    ])
    def test_toggle_form(self, path, toggle, call, name):
        files = _files(
            _added_file(path, [toggle]),
            _added_file("tests/conftest.py", ['    monkeypatch.setenv("APP_PROGRESS_NOTES", "false")']),
        )
        findings = toggle_pinned_off_findings(files)
        assert [(f.file, f.line) for f in findings] == [(path, 1)]
        assert f"`{call}`" in findings[0].body
        assert f'"{name}"' in findings[0].title

    @pytest.mark.parametrize(("path", "pin", "pin_name", "value"), [
        ("tests/conftest.py", 'monkeypatch.setenv("ASSISTANT_PROGRESS_NOTES", "false")',
         "ASSISTANT_PROGRESS_NOTES", "false"),
        ("tests/conftest.py", "monkeypatch.setenv('ASSISTANT_PROGRESS_NOTES', '0')", "ASSISTANT_PROGRESS_NOTES", "0"),
        ("conftest.py", 'monkeypatch.setenv("ASSISTANT_PROGRESS_NOTES", "OFF")', "ASSISTANT_PROGRESS_NOTES", "OFF"),
        ("tests/conftest.py", 'os.environ["ASSISTANT_PROGRESS_NOTES"] = "False"', "ASSISTANT_PROGRESS_NOTES", "False"),
        ("web/src/setupTests.ts", 'process.env.ASSISTANT_PROGRESS_NOTES = "false";',
         "ASSISTANT_PROGRESS_NOTES", "false"),
        ("web/jest.setup.js", "process.env['ASSISTANT_PROGRESS_NOTES'] = '0';", "ASSISTANT_PROGRESS_NOTES", "0"),
        ("web/vitest.setup.ts", 'vi.stubEnv("ASSISTANT_PROGRESS_NOTES", "off");', "ASSISTANT_PROGRESS_NOTES", "off"),
        ("src/test/setupTests.kt", 'System.setProperty("assistant.progress.notes", "false")',
         "assistant.progress.notes", "false"),
    ])
    def test_pin_form(self, path, pin, pin_name, value):
        files = _files(
            _added_file("assistant/progress.py", PROGRESS_PY),
            _added_file(path, [pin]),
        )
        findings = toggle_pinned_off_findings(files)
        assert [(f.file, f.line) for f in findings] == [("assistant/progress.py", TOGGLE_LINE)]
        assert f'{path} sets `{pin_name}` to "{value}"' in findings[0].body


class TestMultiplicity:
    """One finding per toggle, every pinning file named, stable order."""

    def test_two_pin_files_one_finding_naming_both_in_order(self):
        files = _files(
            _added_file("assistant/progress.py", PROGRESS_PY),
            _added_file("web/vitest.setup.ts", ['vi.stubEnv("PROGRESS_NOTES", "0");']),
            _added_file("tests/conftest.py", CONFTEST_PY),
        )
        findings = toggle_pinned_off_findings(files)
        assert len(findings) == 1
        body = findings[0].body
        first = body.index('tests/conftest.py sets `ASSISTANT_PROGRESS_NOTES` to "false"')
        second = body.index('web/vitest.setup.ts sets `PROGRESS_NOTES` to "0"')
        assert first < second

    def test_a_repeated_pin_is_named_once(self):
        files = _files(
            _added_file("assistant/progress.py", PROGRESS_PY),
            _added_file("tests/conftest.py", [PIN, PIN]),
        )
        body = toggle_pinned_off_findings(files)[0].body
        assert body.count("tests/conftest.py sets") == 1

    def test_two_pinned_toggles_two_findings_sorted(self):
        files = _files(
            _added_file("b/second.py", ['dark = enabled("dark_mode", default=True)']),
            _added_file("a/first.py", ["", 'on = flag("progress_notes", True)']),
            _added_file("tests/conftest.py", [
                PIN,
                '    monkeypatch.setenv("ASSISTANT_DARK_MODE", "off")',
            ]),
        )
        findings = toggle_pinned_off_findings(files)
        assert [(f.file, f.line) for f in findings] == [("a/first.py", 2), ("b/second.py", 1)]

    def test_only_the_pinned_toggle_is_reported(self):
        files = _files(
            _added_file("app/flags.py", [
                'notes = enabled("progress_notes", default=True)',
                'dark = enabled("dark_mode", default=True)',
            ]),
            _added_file("tests/conftest.py", [PIN]),
        )
        findings = toggle_pinned_off_findings(files)
        assert [(f.file, f.line) for f in findings] == [("app/flags.py", 1)]
