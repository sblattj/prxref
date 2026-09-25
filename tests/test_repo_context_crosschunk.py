"""Tests for ``prxref.repo_crosschunk.diff_definitions`` (issue #17, miss b).

The fixture half runs the real parser and chunker over
``tests/fixtures/issue17``: ``TransportConfig`` gains a compact constructor
that makes ``url`` and ``legacyUrl`` mutually exclusive in one chunk, while
``ConnectorService`` in another chunk calls ``new TransportConfig(...)``. The
rest builds duck-typed stand-ins for ``triage.FileDiff`` so each rule (reasons,
skips, change windows, caps, order, dedup) is pinned on its own.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from prxref import chunk_context, repo_crosschunk
from prxref.repo_context import REASONS, ContextEntry
from prxref.repo_crosschunk import MAX_CHANGE_LINES, diff_definitions
from prxref.triage import build_chunks, parse_unified_diff

FIXTURE = Path(__file__).parent / "fixtures" / "issue17"
REPO = FIXTURE / "repo"
TRANSPORT_CONFIG = "src/main/java/com/acme/connectors/TransportConfig.java"
CONNECTOR_SERVICE = "src/main/java/com/acme/connectors/ConnectorService.java"
MIGRATION = "db/changelog/003-idempotency-unique.sql"
EXCLUSIVITY = "exactly one of url or legacyUrl must be set"


class _Reader:
    """A ``read(path)`` callable over a dict that records every path asked for."""

    def __init__(self, texts: dict[str, str]):
        self.texts = texts
        self.calls: list[str] = []

    def __call__(self, path: str) -> str | None:
        self.calls.append(path)
        return self.texts.get(path)


class _RepoReader:
    """A ``read(path)`` callable over the fixture's PR-head working tree."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, path: str) -> str | None:
        self.calls.append(path)
        target = REPO / path
        return target.read_text(encoding="utf-8") if target.is_file() else None


def _file(path: str, *hunks: tuple[int, list[str]], status: str = "modified") -> SimpleNamespace:
    """A FileDiff stand-in; each hunk is ``(new_start, lines)`` with a ``+``/``-``/`` `` prefix per line."""
    built = []
    for new_start, body in hunks:
        number = new_start
        lines = []
        for raw in body:
            kind, text = raw[0], raw[1:]
            if kind == "-":
                lines.append(SimpleNamespace(kind="-", text=text, new_line=None))
            else:
                lines.append(SimpleNamespace(kind=kind, text=text, new_line=number))
                number += 1
        built.append(SimpleNamespace(lines=lines))
    return SimpleNamespace(path=path, status=status, hunks=built)


def _keys(entries: list[ContextEntry]) -> list[tuple[str, int, str, str, str]]:
    return [(e.path, e.line, e.symbol, e.kind, e.reason) for e in entries]


def _fixture():
    files = parse_unified_diff((FIXTURE / "pr.diff").read_text(encoding="utf-8"))
    chunks = build_chunks(files, max_files_per_chunk=1)
    return files, chunks


def _chunk_holding(chunks, path: str):
    return next(chunk for chunk in chunks if any(f.path == path for f in chunk))


def _hunk_lines(files, path: str) -> dict[int, str]:
    diff = next(f for f in files if f.path == path)
    return {
        line.new_line: line.text
        for hunk in diff.hunks
        for line in hunk.lines
        if line.kind in (" ", "+") and line.new_line is not None
    }


def _assert_shows_only_known_lines(entries: list[ContextEntry], known_by_path: dict[str, dict[int, str]]):
    for entry in entries:
        known = known_by_path[entry.path]
        for offset, text in enumerate(entry.text.split("\n")):
            if text.startswith("\N{HORIZONTAL ELLIPSIS} "):
                continue
            assert entry.line + offset in known, (entry.path, entry.line + offset)
            assert known[entry.line + offset].rstrip() == text


class TestFixture:
    def test_connector_service_chunk_gets_the_transport_config_change(self):
        files, chunks = _fixture()
        read = _RepoReader()
        entries = diff_definitions(_chunk_holding(chunks, CONNECTOR_SERVICE), files, read)

        cross = [e for e in entries if e.reason == "cross-chunk" and e.symbol == "TransportConfig"]
        assert any(EXCLUSIVITY in e.text for e in cross)
        assert _keys(entries) == [
            (TRANSPORT_CONFIG, 9, "TransportConfig", "definition", "cross-chunk"),
            (TRANSPORT_CONFIG, 11, "TransportConfig", "definition", "cross-chunk"),
        ]
        declaration, change = entries
        assert declaration.text.startswith("public record TransportConfig(String url, String legacyUrl) {")
        assert EXCLUSIVITY not in declaration.text
        assert EXCLUSIVITY in change.text
        assert "if (hasUrl == hasLegacyUrl) {" in change.text
        head = (REPO / TRANSPORT_CONFIG).read_text(encoding="utf-8").splitlines()
        assert change.text == "\n".join(text.rstrip() for text in head[10:19])

    def test_reads_only_the_files_a_definition_regex_covers(self):
        files, chunks = _fixture()
        read = _RepoReader()
        diff_definitions(_chunk_holding(chunks, CONNECTOR_SERVICE), files, read)
        assert read.calls == [TRANSPORT_CONFIG]

    @pytest.mark.parametrize("read", [None, lambda path: None], ids=["no-reader", "reader-returns-none"])
    def test_without_file_text_the_entries_come_from_hunks(self, read):
        files, chunks = _fixture()
        chunk = _chunk_holding(chunks, CONNECTOR_SERVICE)
        entries = diff_definitions(chunk, files, read)

        assert [(e.path, e.line, e.reason) for e in entries] == [
            (TRANSPORT_CONFIG, 9, "cross-chunk"),
            (TRANSPORT_CONFIG, 11, "cross-chunk"),
        ]
        assert EXCLUSIVITY in entries[1].text
        _assert_shows_only_known_lines(entries, {TRANSPORT_CONFIG: _hunk_lines(files, TRANSPORT_CONFIG)})
        assert entries == diff_definitions(chunk, files, _RepoReader())

    @pytest.mark.parametrize("path", [TRANSPORT_CONFIG, MIGRATION])
    def test_the_other_chunks_get_nothing(self, path):
        files, chunks = _fixture()
        assert diff_definitions(_chunk_holding(chunks, path), files, _RepoReader()) == []


CALLER = _file(
    "app/Caller.java",
    (10, ["     void run() {", "+        Limits limits = Limits.defaults();", "     }"]),
)


class TestReasons:
    def test_unchanged_definition_in_another_diff_file_is_diff_file(self):
        limits = _file("app/Limits.java", (4, ["     int max;", "-    int min;", " }"]))
        read = _Reader({"app/Limits.java": "package app;\n\npublic final class Limits {\n    int max;\n}\n"})
        entries = diff_definitions([CALLER], [CALLER, limits], read)
        assert _keys(entries) == [("app/Limits.java", 3, "Limits", "definition", "diff-file")]
        assert entries[0].text == "public final class Limits {\n    int max;\n}"

    def test_a_changed_file_outside_the_chunk_is_cross_chunk_even_when_the_definition_is_untouched(self):
        limits = _file("app/Limits.java", (6, [" class Note {", "+    // note", " }"]))
        read = _Reader({
            "app/Limits.java": "package app;\n\npublic final class Limits {\n}\n\nclass Note {\n    // note\n}\n",
        })
        entries = diff_definitions([CALLER], [CALLER, limits], read)
        assert _keys(entries) == [("app/Limits.java", 3, "Limits", "definition", "cross-chunk")]

    @pytest.mark.parametrize(("path", "hunk", "text"), [
        (
            "p/Widget.java",
            ["     Widget copy() {", "+        return new Widget();", "     }"],
            "package p;\n\npublic class Widget {\n\n    private int size;\n\n"
            "    Widget copy() {\n        return new Widget();\n    }\n}\n",
        ),
        (
            "p/Widget.kt",
            ["     fun copy(): Widget {", "+        return Widget()", "     }"],
            "package p\n\nclass Widget {\n\n    private val size = 0\n\n"
            "    fun copy(): Widget {\n        return Widget()\n    }\n}\n",
        ),
    ], ids=["java", "kotlin"])
    def test_a_jvm_definition_in_the_chunks_own_file_is_left_to_chunk_context(self, path, hunk, text):
        widget = _file(path, (7, hunk))
        read = _Reader({path: text})
        assert diff_definitions([widget], [widget], read) == []
        assert read.calls == []
        lines = text.splitlines()
        assert chunk_context.referenced_definitions(chunk_context.chunk_files([widget]), read) == [
            f"{path}:3: " + "\n".join(lines[2:8]),
        ]


class TestSkips:
    def test_lines_inside_the_chunks_own_hunks_are_skipped(self):
        widget = _file(
            "p/Widget.java",
            (3, [" public class Widget {", "+    Widget copy() { return new Widget(); }", " }"]),
        )
        assert diff_definitions([widget], [widget], None) == []

    @pytest.mark.parametrize(("path", "line"), [
        ("p/Widget.java", "+    Widget copy() { return new Widget(); }"),
        ("p/Widget.kt", "+    fun copy(): Widget = Widget()"),
    ], ids=["java", "kotlin"])
    def test_a_jvm_file_in_the_chunk_is_skipped_when_a_reader_is_given(self, path, line):
        widget = _file(path, (3, [" class Widget {", line, " }"]))
        read = _Reader({path: "package p\n\nclass Widget {\n" + line[1:] + "\n}\n"})
        assert diff_definitions([widget], [widget], read) == []
        assert read.calls == []

    def test_a_python_file_in_the_chunk_is_skipped_when_a_reader_is_given(self):
        service = _file("app/service.py", (20, [" def handle(req):", "+    return build_reply(req)"]))
        other = _file("app/other.py", (4, [" x = 1", "-y = 2"]))
        read = _Reader({
            "app/service.py": "import os\ndef build_reply(req):\n    return req\n",
            "app/other.py": "def build_reply(req):\n    return None\n\nx = 1\n",
        })
        entries = diff_definitions([service], [service, other], read)
        assert _keys(entries) == [("app/other.py", 1, "build_reply", "definition", "diff-file")]
        assert read.calls == ["app/other.py"]

    def test_a_js_file_in_the_chunk_is_skipped_when_a_reader_is_given(self):
        page = _file("web/page.ts", (5, [" export function page() {", "+  return renderCard(props);"]))
        read = _Reader({"web/page.ts": "export function renderCard(props) {\n  return props;\n}\n"})
        assert diff_definitions([page], [page], read) == []
        assert read.calls == []

    def test_a_removed_file_is_skipped(self):
        caller = _file("app/Caller.java", (4, ["+        Legacy old = null;"]))
        legacy = _file("old/Legacy.java", (0, ["-public class Legacy {}"]), status="removed")
        read = _Reader({"old/Legacy.java": "public class Legacy {}\n"})
        assert diff_definitions([caller], [caller, legacy], read) == []
        assert "old/Legacy.java" not in read.calls

    def test_no_referenced_names_reads_nothing(self):
        deletion = _file("app/Gone.java", (3, ["-    Legacy old = null;"]))
        other = _file("app/Legacy.java", (1, [" public class Legacy {}"]))
        read = _Reader({"app/Legacy.java": "public class Legacy {}\n"})
        assert diff_definitions([deletion], [deletion, other], read) == []
        assert read.calls == []


INTRA_CHUNK = {
    "java": {
        "caller": ("p/A.java", (6, ["     void run() {", "+        Helper h = new Helper(new Own());", "     }"]),
                   "package p;\n\nclass Own {\n}\n\npublic class A {\n    void run() {\n"
                   "        Helper h = new Helper(new Own());\n    }\n}\n"),
        "helper": ("p/Helper.java", (7, ["     int size() {", "+        return 2;", "     }"]),
                   "package p;\n\npublic class Helper {\n    Helper(Own own) {\n    }\n\n"
                   "    int size() {\n        return 2;\n    }\n}\n"),
        "self_reference": "+        return new Helper(null).size();",
        "name": "Helper", "name_line": 3, "own_line": 3,
    },
    "kotlin": {
        "caller": ("p/A.kt", (6, ["     fun run() {", "+        val h = Helper(Own())", "     }"]),
                   "package p\n\nclass Own\n\nclass A {\n    fun run() {\n        val h = Helper(Own())\n    }\n}\n"),
        "helper": ("p/Helper.kt", (5, ["     fun size(): Int {", "+        return 2", "     }"]),
                   "package p\n\nclass Helper(val own: Own) {\n\n    fun size(): Int {\n        return 2\n    }\n}\n"),
        "self_reference": "+        return Helper(Own()).size()",
        "name": "Helper", "name_line": 3, "own_line": 3,
    },
    "python": {
        "caller": ("app/a.py", (5, [" def run():", "+    return load_helper(own())"]),
                   "def own():\n    return 1\n\n\ndef run():\n    return load_helper(own())\n"),
        "helper": ("app/helper.py", (5, [" def size():", "+    return 2"]),
                   "def load_helper(value):\n    return value\n\n\ndef size():\n    return 2\n"),
        "self_reference": "+    return load_helper(2)",
        "name": "load_helper", "name_line": 1, "own_line": 1,
    },
    "js": {
        "caller": ("web/a.ts", (5, [" export function run() {", "+  return renderCard(own());", " }"]),
                   "function own() {\n  return 1;\n}\n\nexport function run() {\n  return renderCard(own());\n}\n"),
        "helper": ("web/card.ts", (5, [" export function size() {", "+  return 2;", " }"]),
                   "export function renderCard(value) {\n  return value;\n}\n\n"
                   "export function size() {\n  return 2;\n}\n"),
        "self_reference": "+  return renderCard(2);",
        "name": "renderCard", "name_line": 1, "own_line": 1,
    },
}


def _intra_chunk(language: str, *, helper_references_the_name: bool = False):
    """The caller/helper pair for ``language``; line 1 of each helper hunk body is its one ``+`` line."""
    case = INTRA_CHUNK[language]
    caller_path, caller_hunk, caller_text = case["caller"]
    helper_path, (start, body), helper_text = case["helper"]
    if helper_references_the_name:
        assert helper_text.count(body[1][1:]) == 1
        helper_text = helper_text.replace(body[1][1:], case["self_reference"][1:])
        body = [body[0], case["self_reference"], *body[2:]]
    caller = _file(caller_path, caller_hunk)
    helper = _file(helper_path, (start, body))
    read = _Reader({caller_path: caller_text, helper_path: helper_text})
    return case, caller, helper, read


class TestTheChunksOwnFiles:
    """With a reader, a chunk file is searched only for names its own added lines do not mention.

    ``referenced_definitions`` shows a file's own out-of-hunk definitions for the
    names on that file's added lines, so those are left to it; a name another
    file of the same chunk references is still searched for, and so still found.
    """

    @pytest.mark.parametrize("language", sorted(INTRA_CHUNK))
    def test_a_name_another_file_of_the_chunk_references_is_found_outside_the_hunk(self, language):
        case, caller, helper, read = _intra_chunk(language)
        entries = diff_definitions([caller, helper], [caller, helper], read)
        assert _keys(entries) == [(helper.path, case["name_line"], case["name"], "definition", "diff-file")]
        assert read.calls == [helper.path]
        shown = chunk_context.referenced_definitions(chunk_context.chunk_files([caller, helper]), _Reader(read.texts))
        assert [entry for entry in shown if entry.startswith(f"{helper.path}:")] == []

    @pytest.mark.parametrize("language", sorted(INTRA_CHUNK))
    def test_a_files_own_reference_is_left_to_chunk_context_and_that_file_is_not_read(self, language):
        case, caller, helper, read = _intra_chunk(language)
        entries = diff_definitions([caller, helper], [caller, helper], read)
        assert [entry for entry in entries if entry.path == caller.path] == []
        assert caller.path not in read.calls
        lines = read.texts[caller.path].splitlines()
        shown = chunk_context.referenced_definitions(chunk_context.chunk_files([caller, helper]), _Reader(read.texts))
        assert [entry for entry in shown if entry.startswith(f"{caller.path}:")] == [
            f"{caller.path}:{case['own_line']}: "
            + chunk_context._entry_text(lines, case["own_line"] - 1, chunk_context.MAX_LINES_PER_DEFINITION),
        ]

    @pytest.mark.parametrize("language", sorted(INTRA_CHUNK))
    def test_a_one_file_chunk_reads_nothing(self, language):
        _, caller, _, read = _intra_chunk(language)
        assert diff_definitions([caller], [caller], read) == []
        assert read.calls == []

    @pytest.mark.parametrize("language", sorted(INTRA_CHUNK))
    def test_a_name_both_files_reference_is_shown_once_by_chunk_context(self, language):
        case, caller, helper, read = _intra_chunk(language, helper_references_the_name=True)
        assert diff_definitions([caller, helper], [caller, helper], read) == []
        assert caller.path not in read.calls
        shown = chunk_context.referenced_definitions(chunk_context.chunk_files([caller, helper]), _Reader(read.texts))
        assert [entry.split(":", 2)[1] for entry in shown if entry.startswith(f"{helper.path}:")] == [
            str(case["name_line"]),
        ]


class TestChangeEntries:
    def test_the_window_ends_at_the_next_top_level_definition(self):
        caller = _file("app/Caller.java", (1, ["+First first = new First();"]))
        target = _file(
            "p/First.java",
            (1, [
                " package p;", " ", " public class First {", "+    int added;", " }", " ",
                " class Second {", "+    int other;", " }",
            ]),
        )
        entries = diff_definitions([caller], [caller, target], None)
        assert [(e.line, e.symbol, e.text) for e in entries] == [
            (3, "First", "public class First {\n    int added;\n}"),
            (4, "First", "    int added;"),
        ]

    def test_a_nested_type_does_not_end_the_window_and_dedup_keeps_the_first(self):
        caller = _file("app/Caller.java", (1, ["+    Outer.Inner x = new Outer.Inner(1);"]))
        outer = _file(
            "p/Outer.java",
            (1, [
                " package p;", " ", " public class Outer {", "+    public record Inner(int a) {",
                "     }", "     void go() {", "         run();", "+        check();", "+        log();",
                "     }", " }",
            ]),
        )
        entries = diff_definitions([caller], [caller, outer], None)
        assert [(e.line, e.symbol, e.reason) for e in entries] == [
            (3, "Outer", "cross-chunk"),
            (4, "Inner", "cross-chunk"),
            (8, "Outer", "cross-chunk"),
        ]
        assert entries[1].text == "    public record Inner(int a) {\n    }"
        assert entries[2].text == "        check();\n        log();"

    def test_python_windows_follow_indentation(self):
        view = _file("app/views.py", (10, [" def view(request):", "+    cfg = Settings.load()"]))
        settings = _file(
            "app/settings.py",
            (3, [" ", " class Settings:", "+    debug = False", " "]),
            (11, [" ", " def helper():", "+    return 1"]),
        )
        text = (
            "import os\n\n\nclass Settings:\n    debug = False\n\n    @classmethod\n"
            "    def load(cls):\n        return cls()\n\n\ndef helper():\n    return 1\n"
        )
        entries = diff_definitions([view], [view, settings], _Reader({"app/settings.py": text}))
        assert [(e.line, e.symbol, e.text) for e in entries] == [
            (4, "Settings", "class Settings:"),
            (5, "Settings", "    debug = False"),
            (8, "load", "    def load(cls):"),
        ]

    def test_a_wholly_added_type_keeps_its_change_entry(self):
        caller = _file("app/Caller.java", (1, ["+NewType t = new NewType(null, \"b\");"]))
        body = [
            "package p;", "", "public record NewType(String a, String b) {", "",
            "    static final int LIMIT = 3;", "", "    public NewType {",
            "        if (a == null && b == null) {",
            "            throw new IllegalArgumentException(\"a or b is required\");",
            "        }", "    }", "}",
        ]
        added = _file("p/NewType.java", (1, ["+" + text for text in body]), status="added")
        entries = diff_definitions([caller], [caller, added], None)
        assert [(e.line, e.reason) for e in entries] == [(3, "cross-chunk"), (4, "cross-chunk")]
        assert "a or b is required" not in entries[0].text
        assert entries[1].text == "\n".join(body[3:12])

    @pytest.mark.parametrize(
        ("run", "tail"),
        [(20, ["\N{HORIZONTAL ELLIPSIS} 8 more changed lines"]), (MAX_CHANGE_LINES, [])],
    )
    def test_a_long_run_is_capped(self, run, tail):
        caller = _file("app/Caller.java", (1, ["+    Big big = new Big();"]))
        fields = [f"    int f{i};" for i in range(1, run + 1)]
        big = _file("p/Big.java", (1, [" public class Big {", *("+" + f for f in fields), " }"]))
        entries = diff_definitions([caller], [caller, big], None)
        assert MAX_CHANGE_LINES == 12
        assert [e.line for e in entries] == [1, 2]
        assert entries[1].text.split("\n") == fields[:12] + tail


class TestHunkText:
    def test_a_definition_at_the_edge_of_a_hunk_shows_no_unknown_line(self):
        caller = _file("app/Caller.java", (1, ["+    Spill s = new Spill();"]))
        spill = _file(
            "lib/Spill.java",
            (1, [" package lib;", "+", " public class Spill extends Base {"]),
            (10, ["+    int late;", " }"]),
        )
        entries = diff_definitions([caller], [caller, spill], None)
        assert [(e.line, e.text) for e in entries] == [
            (3, "public class Spill extends Base {"),
            (10, "    int late;"),
        ]
        _assert_shows_only_known_lines(entries, {"lib/Spill.java": {
            1: "package lib;", 2: "", 3: "public class Spill extends Base {", 10: "    int late;", 11: "}",
        }})
        fields = [f"    int {name};" for name in ("a", "b", "c", "d", "e", "f")]
        full = "\n".join(["package lib;", "", "public class Spill extends Base {", *fields, "    int late;", "}"])
        read = diff_definitions([caller], [caller, spill], _Reader({"lib/Spill.java": full + "\n"}))
        assert [(e.line, e.text) for e in read] == [
            (3, "\n".join(["public class Spill extends Base {", *fields[:5]])),
            (10, "    int late;"),
        ]

    def test_hunk_runs_keep_their_new_file_line_numbers(self):
        caller = _file("app/Caller.java", (1, ["+    Far far = Near.make();"]))
        target = _file(
            "p/Types.java",
            (40, [" class Near {", "+    int n1;", " }"]),
            (90, [" class Far {", " }"]),
        )
        entries = diff_definitions([caller], [caller, target], None)
        assert [(e.line, e.symbol) for e in entries] == [(40, "Near"), (41, "Near"), (90, "Far")]
        _assert_shows_only_known_lines(entries, {"p/Types.java": {
            40: "class Near {", 41: "    int n1;", 42: "}", 90: "class Far {", 91: "}",
        }})


class TestOrder:
    def test_reason_rank_then_path_then_line(self):
        main = _file(
            "app/Main.java",
            (5, [" class Main {", "+    Zeta z = new Zeta(); Alpha a = Alpha.of(); Beta b = new Beta();", " }"]),
        )
        alpha = _file("z/Alpha.java", (1, [" public class Alpha {", "+    int a;", " }"]))
        beta = _file("b/Beta.java", (1, [" public class Beta {", "-    int old;", " }"]))
        zeta = _file("a/Zeta.java", (1, [" public class Zeta {", "+    int z;", " }"]))
        entries = diff_definitions([main], [main, alpha, beta, zeta], None)
        assert [(e.path, e.line, e.reason) for e in entries] == [
            ("a/Zeta.java", 1, "cross-chunk"),
            ("a/Zeta.java", 2, "cross-chunk"),
            ("z/Alpha.java", 1, "cross-chunk"),
            ("z/Alpha.java", 2, "cross-chunk"),
            ("b/Beta.java", 1, "diff-file"),
        ]
        assert all(e.reason in REASONS and e.kind == "definition" for e in entries)

    def test_names_are_the_union_over_the_chunks_files(self):
        java = _file("app/Main.java", (1, ["+    Alpha a = null;"]))
        python = _file("app/run.py", (1, ["+beta_result = compute_beta()"]))
        alpha = _file("z/Alpha.java", (1, [" public class Alpha {", " }"]))
        helpers = _file("lib/helpers.py", (1, [" def compute_beta():", "     return 2"]))
        entries = diff_definitions([java, python], [java, python, alpha, helpers], None)
        assert [(e.path, e.symbol, e.reason) for e in entries] == [
            ("lib/helpers.py", "compute_beta", "diff-file"),
            ("z/Alpha.java", "Alpha", "diff-file"),
        ]

    def test_the_module_is_pure(self):
        source = Path(repo_crosschunk.__file__).read_text(encoding="utf-8")
        imports = sorted(
            line.strip() for line in source.splitlines() if line.startswith(("import ", "from "))
        )
        assert imports == [
            "from . import chunk_context",
            "from .repo_context import (",
            "from __future__ import annotations",
            "from collections.abc import Callable, Sequence",
        ]
