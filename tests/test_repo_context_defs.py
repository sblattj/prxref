"""Unit tests for the definitions core of :mod:`prxref.repo_context`.

The module is pure, so these pin the Java declaration regex, the names an added
line references, the definition scan over any file's text, and the shared
``ContextEntry`` shape without a forge, an LLM or a filesystem in the loop.
"""
from __future__ import annotations

import dataclasses

import pytest

from prxref import chunk_context
from prxref.chunk_context import ChunkFile, referenced_definitions
from prxref.repo_context import (
    KINDS,
    REASONS,
    ContextEntry,
    definition_regexes,
    find_definitions,
    language_of,
    referenced_names,
)


def reader(files: dict[str, str]):
    def _read(path: str) -> str | None:
        return files.get(path)
    return _read


def _java_match(line: str) -> str | None:
    for regex in definition_regexes("java"):
        match = regex.match(line)
        if match:
            return match.group(1)
    return None


class TestVocabulary:
    def test_reasons_order_is_the_admission_rank(self):
        assert REASONS == (
            "cross-chunk", "contract", "diff-file", "import", "path-convention", "name-search",
        )

    def test_kinds(self):
        assert KINDS == ("definition", "contract")


class TestLanguageOf:
    @pytest.mark.parametrize("path", [
        "src/main/java/com/acme/ConnectorService.java",
        "Legacy.JAVA",
        "a/b/Mixed.Java",
    ])
    def test_java_suffix_is_case_insensitive(self, path):
        assert language_of(path) == "java"

    @pytest.mark.parametrize("path, expected", [
        ("web/app.ts", "js"),
        ("web/view.tsx", "js"),
        ("pkg/mod.py", "python"),
        ("cmd/main.go", "go"),
        ("src/lib.rs", "rust"),
        ("README.md", ""),
        ("build.gradle", ""),
        ("Service.javax", ""),
    ])
    def test_other_paths_delegate_to_chunk_context(self, path, expected):
        assert language_of(path) == expected
        assert language_of(path) == chunk_context._language(path)


class TestDefinitionRegexes:
    def test_js_and_python_delegate_to_chunk_context(self):
        assert definition_regexes("js") == chunk_context._definition_regexes("js")
        assert definition_regexes("python") == chunk_context._definition_regexes("python")

    @pytest.mark.parametrize("language", ["go", "rust", "", "cobol"])
    def test_languages_without_regexes_get_none(self, language):
        assert definition_regexes(language) == ()

    def test_java_is_not_added_to_chunk_context(self):
        assert chunk_context._definition_regexes("java") == ()
        assert len(definition_regexes("java")) == 1


class TestJavaRegex:
    @pytest.mark.parametrize("line, name", [
        ("public final class ConnectorService {", "ConnectorService"),
        ("public sealed interface Transport permits A, B {", "Transport"),
        ("enum Mode { A, B }", "Mode"),
        ("@interface Audited {", "Audited"),
        ("public @interface Audited {", "Audited"),
        ("@Entity public class Foo extends Bar<Baz> implements Q {", "Foo"),
        ("@Deprecated @SuppressWarnings(\"unused\") abstract class Old {", "Old"),
        ('@Table(name = "t", indexes = @Index(columnList = "a")) public class Tbl {', "Tbl"),
        ("public non-sealed class Open extends Transport {", "Open"),
        ("    static final class Inner<T extends Comparable<T>> {", "Inner"),
        ("protected strictfp class Calc {", "Calc"),
    ])
    def test_type_declarations_match(self, line, name):
        assert _java_match(line) == name

    @pytest.mark.parametrize("line, name", [
        ("record TransportConfig(String url, String legacyUrl) {", "TransportConfig"),
        ("public record Pair<A, B>(A a, B b) implements Q {", "Pair"),
    ])
    def test_record_declarations_match(self, line, name):
        assert _java_match(line) == name

    @pytest.mark.parametrize("line", [
        "public void send(TransportConfig c) {",
        "private final TransportConfig config;",
        "TransportConfig cfg = new TransportConfig(a, b);",
        "// class Foo in a comment",
        'String s = "class Foo";',
        ' * class Foo in a javadoc line',
        "var record = repository.find(id);",
        "    record.save();",
        "public static Record record(Foo x) {",
        "classic Foo bar;",
        "return new Foo() {",
    ])
    def test_methods_fields_locals_comments_and_strings_do_not_match(self, line):
        assert _java_match(line) is None


class TestReferencedNames:
    def test_java_drops_keywords_jdk_names_locals_and_own_declarations(self):
        added = [
            "import com.acme.connectors.TransportConfig;",
            "import java.util.concurrent.ConcurrentSkipListMap;",
            "import static java.util.Objects.requireNonNull;",
            "public record RetryPolicy(int max) {",
            "private static final int MAX_RETRIES = 3;",
            "public Optional<String> send(TransportConfig config, Payload payload) {",
            "    var cfg = new ConcurrentSkipListMap<String, Integer>();",
            "    List<T> items = RetryPolicy.of(MAX_RETRIES);",
            "    return Optional.of(Encoder.encode(payload, config.legacyUrl()));",
        ]
        assert referenced_names(added, "java") == ["TransportConfig", "Payload", "Encoder"]

    def test_java_import_filter_is_what_drops_an_unlisted_jdk_type(self):
        use = "    var index = new ConcurrentSkipListMap<String, Payload>();"
        assert referenced_names([use], "java") == ["ConcurrentSkipListMap", "Payload"]
        assert referenced_names(
            ["import java.util.concurrent.ConcurrentSkipListMap;", use], "java",
        ) == ["Payload"]

    def test_javax_imports_are_jdk_too(self):
        added = ["import javax.annotation.processing.Generated;", "@Generated Widget w;"]
        assert referenced_names(added, "java") == ["Widget"]

    def test_java_keeps_only_type_like_names(self):
        added = ["ID = URL + Widget.SIZE_MAX + widgetCount + _Hidden + $Proxy + X;"]
        assert referenced_names(added, "java") == ["Widget"]

    def test_python_matches_referenced_definitions_filtering(self):
        added = [
            "from app.models import Helper",
            "def local_fn(x):",
            "    return Helper(x) + other_value",
        ]
        assert referenced_names(added, "python") == ["app", "models", "Helper", "x", "other_value"]

    def test_js_matches_referenced_definitions_filtering(self):
        added = [
            "import { Positive } from './checks';",
            "export const total = (count: number) => Positive(count) + helper;",
        ]
        assert referenced_names(added, "js") == ["Positive", "checks", "count", "helper"]

    def test_accepts_a_one_shot_iterable(self):
        added = iter(["public class Local {", "    Remote r = Local.of();"])
        assert referenced_names(added, "java") == ["Remote"]

    def test_empty_added(self):
        assert referenced_names([], "java") == []


_JAVA_FILE = "\n".join([
    "package com.acme.connectors;",
    "",
    "import java.util.List;",
    "",
    "@Entity public class ConnectorService {",
    "    public void send(TransportConfig c) {",
    "        TransportConfig cfg = new TransportConfig(c.url(), null);",
    "    }",
    "}",
    "",
    "public record TransportConfig(",
    "    String url,",
    "    String legacyUrl",
    ") {}",
    "",
    "enum Mode { A, B }",
])

_PY_FILE = "\n".join([
    "class Foo:",
    "    pass",
    "",
    "def build(",
    "    a,",
    "    b,",
    "):",
    "    return Foo()",
    "",
    "Foo = build(1, 2)",
])

_TS_FILE = "\n".join([
    "export interface TransportConfig {",
    "  url: string;",
    "}",
    "",
    "export const send = (c: TransportConfig) => c.url;",
    "function helper() { return 1; }",
])


class TestFindDefinitions:
    def test_java_types_in_line_order_with_continuation(self):
        found = find_definitions(
            _JAVA_FILE, ["Mode", "TransportConfig", "ConnectorService"], language="java",
        )
        assert found == [
            ("ConnectorService", 5, "@Entity public class ConnectorService {\n"
                                    "    public void send(TransportConfig c) {\n"
                                    "        TransportConfig cfg = new TransportConfig(c.url(), null);\n"
                                    "    }\n"
                                    "}"),
            ("TransportConfig", 11, "public record TransportConfig(\n"
                                    "    String url,\n"
                                    "    String legacyUrl\n"
                                    ") {}"),
            ("Mode", 16, "enum Mode { A, B }"),
        ]

    def test_java_ignores_names_nobody_wants(self):
        assert find_definitions(_JAVA_FILE, ["Mode"], language="java") == [
            ("Mode", 16, "enum Mode { A, B }"),
        ]

    def test_first_definition_only(self):
        found = find_definitions(_PY_FILE, ["Foo"], language="python")
        assert found == [("Foo", 1, "class Foo:")]

    def test_skip_lines_passes_over_a_definition(self):
        found = find_definitions(_PY_FILE, ["Foo"], language="python", skip_lines=frozenset({1}))
        assert found == [("Foo", 10, "Foo = build(1, 2)")]

    def test_max_lines_caps_the_continuation(self):
        default = find_definitions(_PY_FILE, ["build"], language="python")
        capped = find_definitions(_PY_FILE, ["build"], language="python", max_lines=2)
        assert default == [("build", 4, "def build(\n    a,\n    b,\n):")]
        assert capped == [("build", 4, "def build(\n    a,")]

    def test_typescript(self):
        found = find_definitions(_TS_FILE, ["helper", "TransportConfig", "send"], language="js")
        assert found == [
            ("TransportConfig", 1, "export interface TransportConfig {\n  url: string;\n}"),
            ("send", 5, "export const send = (c: TransportConfig) => c.url;"),
            ("helper", 6, "function helper() { return 1; }"),
        ]

    def test_unknown_language_finds_nothing(self):
        assert find_definitions(_PY_FILE, ["Foo"], language="") == []
        assert find_definitions(_PY_FILE, ["Foo"], language="go") == []

    def test_java_text_under_a_js_language_finds_nothing(self):
        assert find_definitions(_JAVA_FILE, ["Mode"], language="python") == []

    def test_no_names_or_no_text(self):
        assert find_definitions(_PY_FILE, [], language="python") == []
        assert find_definitions("", ["Foo"], language="python") == []

    def test_byte_cap_is_inclusive_at_the_limit(self):
        head = "class Foo:\n    pass\n"
        at_cap = head + "#" * (chunk_context.MAX_FILE_BYTES - len(head.encode("utf-8")))
        assert len(at_cap.encode("utf-8")) == chunk_context.MAX_FILE_BYTES
        assert find_definitions(at_cap, ["Foo"], language="python") == [("Foo", 1, "class Foo:")]
        assert find_definitions(at_cap + "#", ["Foo"], language="python") == []

    def test_byte_cap_counts_utf8_bytes_not_characters(self):
        head = "class Foo:\n    pass\n# "
        wide = head + "\N{SNOWMAN}" * (chunk_context.MAX_FILE_BYTES // 3)
        assert len(wide) <= chunk_context.MAX_FILE_BYTES
        assert len(wide.encode("utf-8")) > chunk_context.MAX_FILE_BYTES
        assert find_definitions(wide, ["Foo"], language="python") == []


class TestContextEntry:
    def test_rendered_matches_the_referenced_definitions_line(self):
        content = "import x\n\nclass Helper:\n    pass\n\ndef use():\n    return Helper()\n"
        chunk = ChunkFile(path="pkg/a.py", added=("    return Helper()",), hunk_lines=frozenset({7}))
        emitted = referenced_definitions([chunk], reader({"pkg/a.py": content}))
        found = find_definitions(
            content,
            referenced_names(chunk.added, "python"),
            language="python",
            skip_lines=chunk.hunk_lines,
        )
        entries = [
            ContextEntry(path="pkg/a.py", line=line, symbol=symbol,
                         kind="definition", reason="diff-file", text=text)
            for symbol, line, text in found
        ]
        assert emitted == ["pkg/a.py:3: class Helper:"]
        assert [entry.rendered() for entry in entries] == emitted

    def test_record_carries_the_rendered_length(self):
        entry = ContextEntry(
            path="com/acme/TransportConfig.java", line=11, symbol="TransportConfig",
            kind="definition", reason="cross-chunk",
            text="public record TransportConfig(\n    String url\n) {}",
        )
        assert entry.rendered() == (
            "com/acme/TransportConfig.java:11: public record TransportConfig(\n    String url\n) {}"
        )
        assert entry.record() == {
            "path": "com/acme/TransportConfig.java",
            "line": 11,
            "symbol": "TransportConfig",
            "kind": "definition",
            "reason": "cross-chunk",
            "chars": len(entry.rendered()),
        }
        assert entry.record()["chars"] == 84

    def test_entries_are_frozen(self):
        entry = ContextEntry(path="a.py", line=1, symbol="A", kind="definition",
                             reason="import", text="class A:")
        with pytest.raises(dataclasses.FrozenInstanceError):
            entry.line = 2  # type: ignore[misc]
