"""Java and Kotlin in :mod:`prxref.chunk_context` (#20).

A ``.java``, ``.kt`` or ``.kts`` file now gets the two blocks every other
language already had: definitions referenced from its added lines, found
outside its hunks in the same file and led by up to two annotation lines, and
dependency versions from the nearest ``pom.xml`` or Gradle build. Every other
language must behave exactly as before, so the last class pins js, python, go
and rust against output captured from the code before this change.
"""
from __future__ import annotations

import pytest

from prxref import chunk_context, jvm_deps, jvm_lang
from prxref.chunk_context import (
    MAX_LINES_PER_DEFINITION,
    ChunkFile,
    dependency_versions,
    referenced_definitions,
)

ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"


class Reader:
    """A ``read(path)`` callable over a dict that records every path asked for."""

    def __init__(self, texts: dict[str, str]):
        self.texts = texts
        self.calls: list[str] = []

    def __call__(self, path: str) -> str | None:
        self.calls.append(path)
        return self.texts.get(path)


def _text(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def _entry(path: str, lines: list[str], first: int, last: int) -> str:
    """The rendered entry for 1-based source lines ``first`` through ``last``."""
    return f"{path}:{first}: " + "\n".join(lines[first - 1:last])


ORDER_SERVICE = "src/main/java/com/acme/orders/OrderService.java"
ORDER_SERVICE_LINES = [
    "package com.acme.orders;",
    "",
    "import com.fasterxml.jackson.annotation.JsonProperty;",
    "",
    "public class OrderService {",
    "",
    "    @Deprecated",
    "    public static final int MAX_ITEMS = 50;",
    "",
    '    @JsonProperty("customer_id")',
    "    @Nullable",
    "    private String customerId;",
    "",
    "    @Transactional",
    "    @Override",
    "    public Order place(Order order, int count) {",
    "        return repository.save(order);",
    "    }",
    "",
    "    public void process(Order order) {",
    "        if (order.items() > MAX_ITEMS) {",
    "            log(customerId);",
    "        }",
    "        place(order, 1);",
    "    }",
    "}",
]
ORDER_SERVICE_CHUNK = ChunkFile(
    ORDER_SERVICE, tuple(ORDER_SERVICE_LINES[20:24]), frozenset(range(20, 27)),
)


class TestJavaDefinitions:
    def test_a_constant_a_field_and_a_method_outside_the_hunks_come_with_their_annotations(self):
        read = Reader({ORDER_SERVICE: _text(ORDER_SERVICE_LINES)})
        out = referenced_definitions([ORDER_SERVICE_CHUNK], read)
        assert out == [
            _entry(ORDER_SERVICE, ORDER_SERVICE_LINES, 7, 8),
            _entry(ORDER_SERVICE, ORDER_SERVICE_LINES, 10, 12),
            _entry(ORDER_SERVICE, ORDER_SERVICE_LINES, 14, 18),
        ]
        assert out[0].endswith("    @Deprecated\n    public static final int MAX_ITEMS = 50;")
        assert out[2].split("\n")[:3] == [
            f"{ORDER_SERVICE}:14:     @Transactional",
            "    @Override",
            "    public Order place(Order order, int count) {",
        ]
        assert all(len(entry.split("\n")) <= MAX_LINES_PER_DEFINITION for entry in out)
        assert read.calls == [ORDER_SERVICE]

    @pytest.mark.parametrize(("above", "first"), [
        (["    @A", "    @B", "    @C"], 3),
        (["    @A", "", "    @B"], 4),
        (["    // @A", "    @B"], 3),
        ([], 2),
    ], ids=["three-annotations-keep-the-nearest-two", "a-blank-line-stops-the-walk", "a-comment-stops-the-walk",
            "no-annotation"])
    def test_the_entry_starts_at_the_nearest_two_contiguous_annotation_lines(self, above, first):
        lines = ["public class Limits {", *above, "    int limit = 3;", "}", *["    // pad"] * 20]
        definition = 2 + len(above)
        files = [ChunkFile("p/Limits.java", ("        return limit;",), frozenset({30}))]
        out = referenced_definitions(files, Reader({"p/Limits.java": _text(lines)}))
        assert out == [_entry("p/Limits.java", lines, first, definition)]

    CALC = [
        "public class Calc {",
        "    @A",
        "    @B",
        "    public int compute(int a,",
        "            int b) {",
        "        return a + b;",
        "    }",
        "}",
        *["    // pad"] * 20,
    ]

    @pytest.mark.parametrize(("cap", "first", "last"), [
        (1, 4, 4), (2, 3, 4), (3, 2, 4), (4, 2, 5), (6, 2, 7), (7, 2, 7),
    ])
    def test_annotation_lines_count_toward_the_line_cap_and_never_push_out_the_definition(self, cap, first, last):
        files = [ChunkFile("p/Calc.java", ("        compute(1, 2);",), frozenset({25}))]
        out = referenced_definitions(
            files, Reader({"p/Calc.java": _text(self.CALC)}), max_lines_per_entry=cap,
        )
        assert out == [_entry("p/Calc.java", self.CALC, first, last)]
        assert len(out[0].split("\n")) <= cap
        assert "public int compute(int a," in out[0]

    def test_a_keyword_is_never_a_referenced_name(self):
        lines = [
            "public class Holder {",
            "    private String module;",
            "    int record = 0;",
            "    private String name;",
            "}",
            *["    // pad"] * 20,
        ]
        files = [ChunkFile("p/Holder.java", ("        return module + record + name;",), frozenset({20}))]
        out = referenced_definitions(files, Reader({"p/Holder.java": _text(lines)}))
        assert out == [_entry("p/Holder.java", lines, 4, 4)]
        assert {"module", "record", "return"} <= jvm_lang.JAVA_KEYWORDS

    def test_a_name_defined_on_an_added_line_is_not_looked_up(self):
        lines = ["public class Box {", "    int size = 1;", "}", *["    // pad"] * 20]
        added = ("    int size = 2;", "    return size;")
        files = [ChunkFile("p/Box.java", added, frozenset({20, 21}))]
        assert referenced_definitions(files, Reader({"p/Box.java": _text(lines)})) == []


class TestJavaCaps:
    CONSTANTS = [
        "public class Consts {",
        *[f"    static final int LIMIT_{i} = {i};" for i in range(6)],
        "}",
        *["    // pad"] * 10,
    ]
    ADDED = ("        return " + " + ".join(f"LIMIT_{i}" for i in range(6)) + ";",)

    def test_the_entry_cap_appends_the_omitted_line(self):
        files = [ChunkFile("p/Consts.java", self.ADDED, frozenset({30}))]
        out = referenced_definitions(files, Reader({"p/Consts.java": _text(self.CONSTANTS)}), max_entries=2)
        assert out == [
            _entry("p/Consts.java", self.CONSTANTS, 2, 2),
            _entry("p/Consts.java", self.CONSTANTS, 3, 3),
            f"{ELLIPSIS} 4 more definitions omitted",
        ]

    def test_the_character_cap_appends_the_omitted_line(self):
        files = [ChunkFile("p/Consts.java", self.ADDED, frozenset({30}))]
        out = referenced_definitions(files, Reader({"p/Consts.java": _text(self.CONSTANTS)}), max_chars=100)
        assert out == [
            _entry("p/Consts.java", self.CONSTANTS, 2, 2),
            _entry("p/Consts.java", self.CONSTANTS, 3, 3),
            f"{ELLIPSIS} 4 more definitions omitted",
        ]

    def test_annotation_lines_count_toward_the_character_cap(self):
        lines = ["public class A {", "    @Nullable", "    String value;", "}", *["    // pad"] * 10]
        files = [ChunkFile("p/A.java", ("        return value;",), frozenset({20}))]
        entry = _entry("p/A.java", lines, 2, 3)
        read = Reader({"p/A.java": _text(lines)})
        assert referenced_definitions(files, read, max_chars=len(entry)) == [entry]
        assert referenced_definitions(files, read, max_chars=len(entry) - 1) == [
            f"{ELLIPSIS} 1 more definitions omitted",
        ]

    def test_a_file_over_512_kib_is_skipped(self):
        text = "public class Big {\n    static final int BIG = 1;\n" + "    // pad\n" * 60_000
        assert len(text.encode("utf-8")) > chunk_context.MAX_FILE_BYTES
        files = [ChunkFile("p/Big.java", ("        return BIG;",), frozenset({70_000}))]
        assert referenced_definitions(files, Reader({"p/Big.java": text})) == []


ORDERS_KT = "src/main/kotlin/com/acme/orders/Orders.kt"
ORDERS_KT_LINES = [
    "package com.acme.orders",
    "",
    "@Serializable",
    "data class Order(val id: String, val count: Int)",
    "",
    '@field:JsonProperty("limit")',
    "val limit: Int = 10",
    "",
    "@Throws(IOException::class)",
    "fun load(id: String): Order {",
    "    return Order(id, limit)",
    "}",
    "",
    "fun process(id: String) {",
    "    val order = load(id)",
    "    println(limit + Order(id, 1).count)",
    "}",
]


class TestKotlinDefinitions:
    def test_a_class_a_val_and_a_fun_come_with_their_annotations(self):
        files = [ChunkFile(ORDERS_KT, tuple(ORDERS_KT_LINES[14:16]), frozenset(range(14, 18)))]
        out = referenced_definitions(files, Reader({ORDERS_KT: _text(ORDERS_KT_LINES)}))
        assert out == [
            _entry(ORDERS_KT, ORDERS_KT_LINES, 3, 4),
            _entry(ORDERS_KT, ORDERS_KT_LINES, 6, 7),
            _entry(ORDERS_KT, ORDERS_KT_LINES, 9, 12),
        ]

    def test_a_kotlin_keyword_is_never_a_referenced_name(self):
        lines = [
            "class Holder {",
            "    val data = load()",
            "    val value = 2",
            '    val name = "x"',
            "}",
            *["    // pad"] * 20,
        ]
        files = [ChunkFile("p/Holder.kt", ("    println(data + value + name)",), frozenset({20}))]
        out = referenced_definitions(files, Reader({"p/Holder.kt": _text(lines)}))
        assert out == [_entry("p/Holder.kt", lines, 4, 4)]

    def test_a_gradle_script_is_searched_as_kotlin(self):
        lines = [
            "import org.gradle.api.tasks.Exec",
            "",
            'val releaseVersion = "1.2.3"',
            "",
            'tasks.register<Exec>("release") {',
            '    commandLine("git", "tag", releaseVersion)',
            "}",
        ]
        files = [ChunkFile("build.gradle.kts", (lines[5],), frozenset({5, 6, 7}))]
        out = referenced_definitions(files, Reader({"build.gradle.kts": _text(lines)}))
        assert out == [_entry("build.gradle.kts", lines, 3, 3)]


class TestAnnotationsAreJvmOnly:
    @pytest.mark.parametrize(("path", "lines", "added", "first", "last"), [
        (
            "web/foo.ts",
            ["import { Injectable } from '@angular/core';", "", "@Injectable()", "export class Foo {",
             "  run() {}", "}", *["// pad"] * 20],
            "  return new Foo();", 4, 6,
        ),
        (
            "app/widget.py",
            ["import dataclasses", "", "@dataclasses.dataclass", "class Widget:", "    size: int",
             *["# pad"] * 20],
            "    return Widget(1)", 4, 4,
        ),
    ], ids=["ts-decorator", "python-decorator"])
    def test_a_decorator_above_a_js_or_python_definition_is_not_included(self, path, lines, added, first, last):
        assert jvm_lang.annotation_start(lines, first - 1) == first - 2
        files = [ChunkFile(path, (added,), frozenset({40}))]
        out = referenced_definitions(files, Reader({path: _text(lines)}))
        assert out == [_entry(path, lines, first, last)]


def _pom(*dependencies: tuple[str, str, str]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
        "  <modelVersion>4.0.0</modelVersion>\n"
        "  <groupId>com.acme</groupId><artifactId>orders</artifactId><version>1.0.0</version>\n"
        "  <dependencies>\n"
        + "".join(
            f"    <dependency><groupId>{g}</groupId><artifactId>{a}</artifactId>"
            f"<version>{v}</version></dependency>\n"
            for g, a, v in dependencies
        )
        + "  </dependencies>\n</project>\n"
    )


JACKSON_POM = _pom(
    ("com.fasterxml.jackson.core", "jackson-core", "2.17.1"),
    ("com.fasterxml.jackson.core", "jackson-databind", "2.17.1"),
)
DATABIND_LINE = "com.fasterxml.jackson.core:jackson-databind@2.17.1"
MAPPER = "orders/src/main/java/com/acme/orders/OrderMapper.java"
MAPPER_ADDED = (
    "import com.fasterxml.jackson.databind.ObjectMapper;",
    "import java.util.List;",
    "import com.acme.orders.model.Order;",
)


class TestJvmDependencies:
    def test_a_java_chunk_importing_databind_gets_the_databind_line_from_the_pom_beside_it(self):
        texts = {"orders/pom.xml": JACKSON_POM}
        files = [ChunkFile(MAPPER, MAPPER_ADDED)]
        assert dependency_versions(files, Reader(texts)) == [DATABIND_LINE]
        assert dependency_versions(files, Reader(texts)) == jvm_deps.dependency_lines(
            MAPPER, MAPPER_ADDED, Reader(texts),
        )

    def test_a_kotlin_chunk_reads_its_gradle_build(self):
        texts = {
            "build.gradle.kts": (
                'dependencies {\n    implementation("com.fasterxml.jackson.core:jackson-databind:2.17.1")\n}\n'
            ),
        }
        files = [ChunkFile("app/src/main/kotlin/App.kt", ("import com.fasterxml.jackson.databind.ObjectMapper",))]
        assert dependency_versions(files, Reader(texts)) == [DATABIND_LINE]

    def test_jvm_lines_merge_with_other_languages_sorted_and_deduplicated(self):
        texts = {
            "orders/pom.xml": JACKSON_POM,
            "package.json": '{"dependencies": {"effect": "4.0.0"}}',
        }
        files = [
            ChunkFile("web/a.ts", ("import { E } from 'effect';",)),
            ChunkFile(MAPPER, MAPPER_ADDED),
            ChunkFile("orders/src/main/java/com/acme/orders/Other.java", MAPPER_ADDED[:1]),
        ]
        assert dependency_versions(files, Reader(texts)) == [DATABIND_LINE, "effect@4.0.0"]

    @pytest.mark.parametrize("path", ["build.gradle.kts", "app/build.gradle.kts", "settings.gradle.kts"])
    def test_a_gradle_script_is_skipped_without_a_read(self, path):
        read = Reader({"build.gradle.kts": "plugins { java }\n"})
        files = [ChunkFile(path, ("import org.gradle.api.tasks.Exec",))]
        assert dependency_versions(files, read) == []
        assert read.calls == []

    def test_control_the_same_import_in_a_kotlin_source_file_walks_for_a_manifest(self):
        read = Reader({})
        files = [ChunkFile("app/Tasks.kt", ("import org.gradle.api.tasks.Exec",))]
        assert dependency_versions(files, read) == []
        assert read.calls == [
            "app/pom.xml", "app/build.gradle.kts", "app/build.gradle",
            "pom.xml", "build.gradle.kts", "build.gradle",
        ]


def _ts_file() -> str:
    body = [f"// filler {i}" for i in range(1, 10)]
    body += ["const Positive = check(", "  isInt(),", ");"]
    body += [f"// filler {i}" for i in range(13, 30)]
    return "\n".join(body) + "\n"


PY_SOURCE = (
    "LIMIT = 10\n"
    "\n"
    "def helper(x):\n"
    "    return x\n"
    "\n"
    "class Widget:\n"
    "    pass\n"
) + "".join(f"# pad {i}\n" for i in range(8, 40))

BASE_CASES = {
    "js": (
        [
            ChunkFile("src/a.ts", ("import { E } from 'effect';", "import { t } from 'vitest';")),
            ChunkFile("src/b.ts", ("  limit: Positive,",), frozenset({25, 26})),
        ],
        {
            "package.json": (
                '{"dependencies": {"effect": "4.0.0-rc.110", "unused": "1.0.0"},'
                ' "devDependencies": {"vitest": "^2.1.0"}}'
            ),
            "src/b.ts": _ts_file(),
        },
    ),
    "python": (
        [
            ChunkFile("src/a.py", ("import requests",)),
            ChunkFile("m.py", ("    return helper(LIMIT) or Widget()",), frozenset({50})),
        ],
        {
            "pyproject.toml": '[project]\nname = "x"\ndependencies = ["requests>=2.31", "rich"]\n',
            "m.py": PY_SOURCE,
        },
    ),
    "go": (
        [ChunkFile("cmd/a.go", ('import "github.com/pkg/errors"',))],
        {"go.mod": "module x\n\nrequire (\n\tgithub.com/pkg/errors v0.9.1\n)\n"},
    ),
    "rust": (
        [ChunkFile("src/a.rs", ("use serde::Serialize;", "use tokio::spawn;"))],
        {
            "Cargo.toml": (
                '[dependencies]\nserde = "1.0.203"\n'
                'tokio = { version = "1.38", features = ["full"] }\n'
            ),
        },
    ),
}

BASE_OUTPUT = {
    "js": {
        "deps": ["effect@4.0.0-rc.110", "vitest@^2.1.0"],
        "deps_reads": ["src/package.json", "package.json"],
        "defs": ["src/b.ts:10: const Positive = check(\n  isInt(),\n);"],
        "defs_reads": ["src/a.ts", "src/b.ts"],
    },
    "python": {
        "deps": ["requests@>=2.31"],
        "deps_reads": ["src/pyproject.toml", "pyproject.toml"],
        "defs": ["m.py:1: LIMIT = 10", "m.py:3: def helper(x):", "m.py:6: class Widget:"],
        "defs_reads": ["src/a.py", "m.py"],
    },
    "go": {"deps": [], "deps_reads": [], "defs": [], "defs_reads": []},
    "rust": {
        "deps": ["serde@1.0.203", "tokio@1.38"],
        "deps_reads": ["src/Cargo.toml", "Cargo.toml"],
        "defs": [],
        "defs_reads": [],
    },
}


class TestOtherLanguagesAreUnchanged:
    """Output and reads captured from the code before #20, for one existing input per language."""

    @pytest.mark.parametrize("language", sorted(BASE_CASES))
    def test_output_and_reads_equal_the_pre_jvm_capture(self, language):
        files, texts = BASE_CASES[language]
        deps_read, defs_read = Reader(texts), Reader(texts)
        observed = {
            "deps": dependency_versions(files, deps_read),
            "deps_reads": deps_read.calls,
            "defs": referenced_definitions(files, defs_read),
            "defs_reads": defs_read.calls,
        }
        assert observed == BASE_OUTPUT[language]

    @pytest.mark.parametrize(("path", "language"), [
        ("a.ts", "js"), ("a.py", "python"), ("a.go", "go"), ("a.rs", "rust"),
        ("a.scala", ""), ("README.md", ""), ("build.gradle", ""), ("Service.javax", ""),
        ("A.java", "java"), ("B.JAVA", "java"), ("a.kt", "kotlin"), ("build.gradle.kts", "kotlin"),
    ])
    def test_the_language_map(self, path, language):
        assert chunk_context._language(path) == language

    @pytest.mark.parametrize("language", ["go", "rust", "", "scala"])
    def test_languages_without_regexes_keep_none_and_the_js_keywords(self, language):
        assert chunk_context._definition_regexes(language) == ()
        assert chunk_context._keywords(language) is chunk_context._JS_KEYWORDS

    def test_js_and_python_keep_their_own_regexes_and_keywords(self):
        assert chunk_context._definition_regexes("js") == (chunk_context._JS_DEF_RE,)
        assert chunk_context._definition_regexes("python") == (chunk_context._PY_DEF_RE, chunk_context._PY_ASSIGN_RE)
        assert chunk_context._keywords("js") is chunk_context._JS_KEYWORDS
        assert chunk_context._keywords("python") is chunk_context._PY_KEYWORDS

    @pytest.mark.parametrize(("language", "regexes", "keywords"), [
        ("java", jvm_lang.JAVA_DEFINITION_REGEXES, jvm_lang.JAVA_KEYWORDS),
        ("kotlin", jvm_lang.KOTLIN_DEFINITION_REGEXES, jvm_lang.KOTLIN_KEYWORDS),
    ])
    def test_java_and_kotlin_use_the_jvm_tables(self, language, regexes, keywords):
        assert chunk_context._definition_regexes(language) is regexes
        assert chunk_context._keywords(language) is keywords
