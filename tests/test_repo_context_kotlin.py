"""Kotlin in repository context, and the Java facts shared with :mod:`prxref.jvm_lang` (#20).

:mod:`prxref.repo_context` keeps a types-only view of Java and Kotlin for
repository context. These pin that the Java regex, keywords and JDK names are
``jvm_lang``'s own objects, that ``.kt`` and ``.kts`` paths are Kotlin, that the
Kotlin definition regex finds types and nothing else, which names a Kotlin
added line references, and that a Kotlin file is a same-language reader
candidate for a Kotlin chunk. No test touches the network.
"""
from __future__ import annotations

import pytest

from prxref import jvm_lang, repo_context, repo_contracts
from prxref.chunk_context import ChunkFile
from prxref.repo_context import definition_regexes, language_of, referenced_names
from prxref.repo_readers import reader_candidates, reader_entries


class _Recording:
    """A ``read(path)`` over a dict that records every path asked for, in call order."""

    def __init__(self, files: dict[str, str]):
        self.files = files
        self.calls: list[str] = []

    def __call__(self, path: str) -> str | None:
        self.calls.append(path)
        return self.files.get(path)


def _kotlin_match(line: str) -> str | None:
    for regex in definition_regexes("kotlin"):
        match = regex.match(line)
        if match:
            return match.group(1)
    return None


class TestSharedJavaFacts:
    def test_java_regex_keywords_and_jdk_names_are_the_jvm_lang_objects(self):
        assert repo_context._JAVA_DEF_RE is jvm_lang.JAVA_TYPE_RE
        assert repo_context._JAVA_KEYWORDS is jvm_lang.JAVA_KEYWORDS
        assert repo_context._JDK_NAMES is jvm_lang.JDK_NAMES

    def test_repo_contracts_keeps_importing_the_java_keywords(self):
        assert repo_contracts._JAVA_KEYWORDS is jvm_lang.JAVA_KEYWORDS

    def test_java_stays_types_only_here(self):
        assert definition_regexes("java") == (jvm_lang.JAVA_TYPE_RE,)


class TestLanguageOf:
    @pytest.mark.parametrize(
        ("path", "language"),
        [
            ("src/main/kotlin/com/acme/App.kt", "kotlin"),
            ("src/main/kotlin/com/acme/App.KT", "kotlin"),
            ("build.gradle.kts", "kotlin"),
            ("scripts/tool.main.KTS", "kotlin"),
            ("src/main/java/com/acme/App.java", "java"),
            ("src/main/java/com/acme/App.JAVA", "java"),
            ("pkg/mod.py", "python"),
            ("web/app.ts", "js"),
            ("notes/App.kt.md", ""),
            ("res/App.ktx", ""),
        ],
    )
    def test_suffix_gives_the_language(self, path, language):
        assert language_of(path) == language


class TestKotlinDefinitions:
    def test_kotlin_gets_the_types_only_regex(self):
        assert definition_regexes("kotlin") == (jvm_lang.KOTLIN_TYPE_RE,)
        assert definition_regexes("kotlin")[0] is jvm_lang.KOTLIN_TYPE_RE

    @pytest.mark.parametrize(
        ("line", "name"),
        [
            ("class Widget(val id: String)", "Widget"),
            ("data class Payload(val body: String)", "Payload"),
            ("@Serializable data class Dto(", "Dto"),
            ("sealed class Result<out T> {", "Result"),
            ("enum class Mode { A, B }", "Mode"),
            ("object Registry {", "Registry"),
            ("    companion object Factory {", "Factory"),
            ("interface Transport {", "Transport"),
            ("fun interface Listener {", "Listener"),
            ("typealias Handler = (String) -> Unit", "Handler"),
        ],
    )
    def test_type_declarations_match(self, line, name):
        assert _kotlin_match(line) == name

    @pytest.mark.parametrize(
        "line",
        [
            "fun f()",
            "val x = 1",
            "var y = 2",
            "    override fun toString(): String = name",
            "    private val cache = mutableMapOf<String, Int>()",
            "suspend fun <T> List<T>.firstOrFail(): T {",
            "    companion object {",
            "import com.acme.Widget",
            "package com.acme",
        ],
    )
    def test_functions_properties_and_locals_match_nothing(self, line):
        assert _kotlin_match(line) is None


class TestKotlinReferencedNames:
    def test_keeps_referenced_types_in_first_appearance_order(self):
        added = [
            "import kotlin.reflect.KClass",
            "import java.time.Year",
            "import javax.inject.Inject",
            "import com.acme.billing.Invoice",
            "class Widget @Inject constructor(private val store: Store) {",
            "    override fun render(invoice: Invoice): Payload = Payload(store.load(invoice.id), Year.now())",
            "    val kind: KClass<Order> = Order::class",
            "}",
        ]
        assert referenced_names(added, "kotlin") == ["Invoice", "Store", "Payload", "Order"]

    def test_the_kotlin_import_is_what_drops_an_unlisted_platform_type(self):
        use = "    val kind: KClass<Widget> = Widget::class"
        assert referenced_names([use], "kotlin") == ["KClass", "Widget"]
        assert referenced_names(["import kotlin.reflect.KClass", use], "kotlin") == ["Widget"]

    @pytest.mark.parametrize(
        ("line", "name"),
        [
            ("import java.time.Year", "Year"),
            ("import java.time.Year;", "Year"),
            ("import javax.inject.Inject", "Inject"),
            ("import kotlin.reflect.KClass", "KClass"),
            ("import kotlin.reflect.KClass // reflection", "KClass"),
        ],
    )
    def test_java_javax_and_kotlin_import_segments_are_dropped(self, line, name):
        use = "val made = Year.of(1) + Inject() + KClass<Widget>()"
        assert referenced_names([use], "kotlin") == ["Year", "Inject", "KClass", "Widget"]
        expected = [kept for kept in ["Year", "Inject", "KClass", "Widget"] if kept != name]
        assert referenced_names([line, use], "kotlin") == expected

    def test_another_organizations_import_is_kept(self):
        added = ["import com.acme.billing.Invoice", "val made = Invoice()"]
        assert referenced_names(added, "kotlin") == ["Invoice"]

    def test_the_alias_of_a_platform_import_is_dropped(self):
        use = "val q = Ring<Widget>()"
        assert referenced_names([use], "kotlin") == ["Ring", "Widget"]
        assert referenced_names(["import kotlin.collections.ArrayDeque as Ring", use], "kotlin") == ["Widget"]
        assert referenced_names(["import com.acme.collections.Deque as Ring", use], "kotlin") == ["Ring", "Widget"]

    def test_kotlinx_is_a_library_not_the_platform(self):
        added = ["import kotlinx.coroutines.flow.Flow", "fun stream(): Flow<Widget> = TODO()"]
        assert referenced_names(added, "kotlin") == ["Flow", "Widget"]

    def test_jdk_names_are_dropped(self):
        assert referenced_names(["val m = HashMap<String, Widget>()"], "kotlin") == ["Widget"]

    def test_only_type_like_names_are_kept(self):
        added = ["val total = MAX_SIZE + X + widgetCount + Widget.DEFAULT + _Hidden + limit"]
        assert referenced_names(added, "kotlin") == ["Widget"]

    def test_types_declared_on_the_added_lines_are_dropped(self):
        assert referenced_names(["data class Local(val remote: Remote)"], "kotlin") == ["Remote"]

    def test_keywords_come_from_the_kotlin_keyword_set(self, monkeypatch):
        added = ["override fun render(): Payload = Widget()"]
        assert referenced_names(added, "kotlin") == ["Payload", "Widget"]
        monkeypatch.setattr(jvm_lang, "KOTLIN_KEYWORDS", jvm_lang.KOTLIN_KEYWORDS | {"Payload"})
        assert referenced_names(added, "kotlin") == ["Widget"]
        assert referenced_names(["Payload p = new Widget();"], "java") == ["Payload", "Widget"]


class TestKotlinReaders:
    WRITER = ChunkFile(path="src/main/kotlin/com/acme/Ledger.kt", added=("        this.cache[key] = value",))

    READER = "\n".join([
        "package com.acme",
        "",
        "class Report(private val frames: Frames) {",
        "    fun load(id: String): Any? {",
        "        return frames.cache[id]",
        "    }",
        "}",
    ])

    def test_a_kotlin_file_is_a_same_language_candidate(self):
        listing = ["src/b/Other.kt", "src/a/Reader.kt", "build.gradle.kts", "src/a/Legacy.java", "src/a/view.py"]
        assert reader_candidates(["src/a/Writer.kt"], listing) == [
            "src/a/Reader.kt",
            "src/b/Other.kt",
            "build.gradle.kts",
        ]

    def test_reader_entries_reads_kotlin_readers_of_a_kotlin_chunk(self):
        java_reader = "class Legacy {\n    Object load(String id) {\n        return frames.cache.get(id);\n    }\n}\n"
        files = {
            "src/main/kotlin/com/acme/Report.kt": self.READER,
            "src/main/kotlin/com/acme/Changed.kt": self.READER,
            "src/main/java/com/acme/Legacy.java": java_reader,
            "src/main/python/reader.py": "def f(x):\n    return x.cache\n",
        }
        read = _Recording(files)
        entries = reader_entries(
            [self.WRITER],
            listing=set(files),
            read=read,
            diff_paths={"src/main/kotlin/com/acme/Changed.kt"},
        )
        assert read.calls == ["src/main/kotlin/com/acme/Report.kt"]
        assert [(e.path, e.line, e.symbol, e.kind, e.reason) for e in entries] == [
            ("src/main/kotlin/com/acme/Report.kt", 4, "cache", "reader", "shared-state"),
        ]
        assert entries[0].text == "    fun load(id: String): Any? {\n        return frames.cache[id]"
