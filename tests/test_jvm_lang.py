"""Unit tests for :mod:`prxref.jvm_lang`, the Java and Kotlin facts behind #20.

The module is a pure stdlib leaf, so these pin its definition regexes, word
sets, import parsing and annotation lookback directly, over Spring and Kotlin
snippets with placeholder names and without a forge, an LLM or a filesystem.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from prxref import jvm_lang, repo_context
from prxref.jvm_lang import (
    JAVA_DEFINITION_REGEXES,
    JAVA_ENUM_CONSTANT_RE,
    JAVA_FIELD_RE,
    JAVA_KEYWORDS,
    JAVA_METHOD_RE,
    JAVA_TYPE_RE,
    JDK_NAMES,
    KOTLIN_DEFINITION_REGEXES,
    KOTLIN_FUN_RE,
    KOTLIN_KEYWORDS,
    KOTLIN_PROPERTY_RE,
    KOTLIN_TYPE_RE,
    MAX_ANNOTATION_LINES,
    JvmImport,
    annotation_start,
    definition_regexes,
    jvm_language,
    keywords,
    parse_import,
    parse_imports,
)

TYPE, METHOD, FIELD, ENUM_CONSTANT = range(4)
KOTLIN_TYPE, KOTLIN_FUN, KOTLIN_PROPERTY = range(3)

ALL_REGEXES = {
    "java-type": JAVA_TYPE_RE,
    "java-method": JAVA_METHOD_RE,
    "java-field": JAVA_FIELD_RE,
    "java-enum-constant": JAVA_ENUM_CONSTANT_RE,
    "kotlin-type": KOTLIN_TYPE_RE,
    "kotlin-fun": KOTLIN_FUN_RE,
    "kotlin-property": KOTLIN_PROPERTY_RE,
}

HARD_NEGATIVES = [
    " * must be set;",
    "// int x;",
    "import a.b.C;",
    "package a.b;",
    "return f(x);",
    "new Foo(",
    "} else if (x) {",
    "throw new E(",
    "a.b(c);",
    "@RestController",
    "}",
    "",
]


def _first(line: str, language: str) -> tuple[int, str] | None:
    for position, regex in enumerate(definition_regexes(language)):
        match = regex.match(line)
        if match:
            return position, match.group(1)
    return None


def _scan(text: str, language: str) -> list[tuple[str, str, str]]:
    lines = text.splitlines()
    out = []
    for index, line in enumerate(lines):
        hit = _first(line, language)
        if hit:
            out.append((hit[1], line.strip(), lines[annotation_start(lines, index)].strip()))
    return out


class TestLeafModule:
    def test_imports_only_the_standard_library(self):
        tree = ast.parse(pathlib.Path(jvm_lang.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.level == 0, "a relative import makes jvm_lang depend on prxref"
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert imported == {"__future__", "re", "collections.abc", "dataclasses"}


class TestSharedWithRepoContext:
    def test_type_regex_is_the_repo_context_pattern_byte_for_byte(self):
        assert JAVA_TYPE_RE.pattern == repo_context._JAVA_DEF_RE.pattern
        assert JAVA_TYPE_RE.flags == repo_context._JAVA_DEF_RE.flags

    def test_keyword_set_equals_repo_context(self):
        assert JAVA_KEYWORDS == repo_context._JAVA_KEYWORDS

    def test_jdk_names_equal_repo_context(self):
        assert JDK_NAMES == repo_context._JDK_NAMES

    def test_reserved_words_are_all_keywords(self):
        assert jvm_lang._JAVA_RESERVED <= JAVA_KEYWORDS


class TestJvmLanguage:
    @pytest.mark.parametrize("path, expected", [
        ("src/main/java/com/acme/billing/InvoiceService.java", "java"),
        ("Legacy.JAVA", "java"),
        ("src/main/kotlin/com/acme/billing/Invoice.kt", "kotlin"),
        ("build.gradle.kts", "kotlin"),
        ("Script.KTS", "kotlin"),
        ("build.gradle", ""),
        ("pom.xml", ""),
        ("Service.javax", ""),
        ("notes.ktx", ""),
        ("app/main.py", ""),
        ("", ""),
    ])
    def test_suffix_decides(self, path, expected):
        assert jvm_language(path) == expected


class TestLanguageTables:
    def test_java_regexes_in_the_order_a_caller_tries_them(self):
        assert definition_regexes("java") == (JAVA_TYPE_RE, JAVA_METHOD_RE, JAVA_FIELD_RE, JAVA_ENUM_CONSTANT_RE)
        assert definition_regexes("java") is JAVA_DEFINITION_REGEXES

    def test_kotlin_regexes_in_the_order_a_caller_tries_them(self):
        assert definition_regexes("kotlin") == (KOTLIN_TYPE_RE, KOTLIN_FUN_RE, KOTLIN_PROPERTY_RE)
        assert definition_regexes("kotlin") is KOTLIN_DEFINITION_REGEXES

    @pytest.mark.parametrize("language", ["js", "python", "go", "rust", "", "scala"])
    def test_other_languages_get_nothing(self, language):
        assert definition_regexes(language) == ()
        assert keywords(language) == frozenset()

    def test_keywords_by_language(self):
        assert keywords("java") is JAVA_KEYWORDS
        assert keywords("kotlin") is KOTLIN_KEYWORDS

    def test_java_keywords_cover_modifiers_and_statements(self):
        assert {"public", "final", "new", "return", "static", "throws", "var"} <= JAVA_KEYWORDS

    def test_kotlin_keywords_cover_hard_soft_and_modifier_words(self):
        assert {
            "public", "final", "return", "override", "fun", "val", "var", "when",
            "object", "typealias", "suspend", "data", "sealed", "lateinit", "by", "it",
        } <= KOTLIN_KEYWORDS

    def test_every_group_one_is_the_defined_name(self):
        for regex in JAVA_DEFINITION_REGEXES + KOTLIN_DEFINITION_REGEXES:
            assert regex.groups == 1


class TestHardNegatives:
    @pytest.mark.parametrize("line", HARD_NEGATIVES, ids=[repr(line) for line in HARD_NEGATIVES])
    @pytest.mark.parametrize("regex_id", list(ALL_REGEXES))
    def test_no_definition_regex_matches(self, regex_id, line):
        assert ALL_REGEXES[regex_id].match(line) is None


class TestJavaMethods:
    @pytest.mark.parametrize("line, name", [
        ("public static <T extends Comparable<T>> List<T> sortAll(Collection<? extends T> items) {", "sortAll"),
        ("@Override public String toString() {", "toString"),
        ('@GetMapping("/{id}") public ResponseEntity<InvoiceDto> get(@PathVariable String id) {', "get"),
        ("    protected abstract Map<String, List<Integer>> index(", "index"),
        ("public String[] names() {", "names"),
        ("default Optional<Invoice> find(String id) {", "find"),
        ("private synchronized void flush() throws IOException {", "flush"),
        ("void run();", "run"),
        ("<T> T identity(T value) {", "identity"),
        ("public java.util.List<Map.Entry<String, Integer>> entries() {", "entries"),
        ('String value() default "";', "value"),
    ])
    def test_declarations_match_as_methods(self, line, name):
        assert _first(line, "java") == (METHOD, name)

    @pytest.mark.parametrize("line", [
        "public InvoiceService(String ledgerId) {",
        "public TransportConfig {",
        "Collections.<String>emptyList();",
        "invoiceRepository.save(invoice);",
        'String label = String.format("%s", id);',
    ])
    def test_constructors_and_calls_are_not_methods(self, line):
        assert JAVA_METHOD_RE.match(line) is None


class TestJavaFields:
    @pytest.mark.parametrize("line, name", [
        ("private static final int MAX_RETRIES = 3;", "MAX_RETRIES"),
        ('public static final String DEFAULT_URL = "https://example.com";', "DEFAULT_URL"),
        ("private final Map<String, List<Invoice>> invoicesByCustomer = new HashMap<>();", "invoicesByCustomer"),
        ("private static final Logger LOG = LoggerFactory.getLogger(InvoiceService.class);", "LOG"),
        ("@Autowired private AcmeClient client;", "client"),
        ("protected volatile boolean running;", "running"),
        ("private final @Nullable String nickname;", "nickname"),
        ("int[] counts = new int[4];", "counts"),
        ("int counts[] = new int[4];", "counts"),
        ('String PREFIX = "acme";', "PREFIX"),
    ])
    def test_declarations_match_as_fields(self, line, name):
        assert _first(line, "java") == (FIELD, name)

    def test_a_typed_local_matches_too(self):
        assert _first("        boolean hasUrl = url != null;", "java") == (FIELD, "hasUrl")


class TestJavaEnumConstants:
    @pytest.mark.parametrize("line, name", [
        ("    DRAFT,", "DRAFT"),
        ('    ISSUED("issued", 2),', "ISSUED"),
        ("    @Deprecated VOID,", "VOID"),
        ("    PENDING {", "PENDING"),
        ("    CLOSED;", "CLOSED"),
        ("    LAST", "LAST"),
        ('    REFUNDED("refunded",', "REFUNDED"),
        ("    ARCHIVED, // kept for old rows", "ARCHIVED"),
    ])
    def test_list_entries_match(self, line, name):
        assert _first(line, "java") == (ENUM_CONSTANT, name)

    @pytest.mark.parametrize("line", [
        "MAX_RETRIES = 3;",
        'LOG.info("x");',
        "FOO.bar(x);",
        "Draft,",
        "ACTIVE_state,",
        "case DRAFT -> run(x);",
    ])
    def test_non_entries_do_not_match(self, line):
        assert JAVA_ENUM_CONSTANT_RE.match(line) is None


class TestJavaTypesComeFirst:
    @pytest.mark.parametrize("line, name", [
        ("public class InvoiceService {", "InvoiceService"),
        ("public record Pair(String a, String b) {", "Pair"),
        ("enum Status {", "Status"),
        ("@interface Audited {", "Audited"),
    ])
    def test_type_declarations_are_types(self, line, name):
        assert _first(line, "java") == (TYPE, name)


class TestJavaStatementsAreNotDeclarations:
    @pytest.mark.parametrize("line", [
        "return x;",
        "yield x;",
        "throw e;",
        "else if (x) {",
        "else foo(x);",
        "assert check(x);",
        "super(x);",
        "this.ledgerId = ledgerId;",
        "x = y;",
        "var x = load();",
        "foo.bar = baz;",
        "requires transitive a.b;",
        "synchronized (lock) {",
        "for (int i = 0; i < n; i++) {",
        "} catch (IOException e) {",
        "return new Invoice() {",
        "import static java.util.Objects.requireNonNull;",
        "/* int x; */",
    ])
    def test_no_java_regex_matches(self, line):
        assert _first(line, "java") is None


class TestKotlinDefinitions:
    @pytest.mark.parametrize("line, name", [
        ("class Invoice", "Invoice"),
        ("data class Point(val x: Int, val y: Int)", "Point"),
        ("sealed class Outcome<out T> {", "Outcome"),
        ("enum class Status(val label: String) {", "Status"),
        ("interface Ledger {", "Ledger"),
        ("object Defaults {", "Defaults"),
        ("internal object Registry", "Registry"),
        ("companion object Factory {", "Factory"),
        ("data object Empty", "Empty"),
        ("fun interface Callback {", "Callback"),
        ("@JvmInline value class InvoiceId(val raw: String)", "InvoiceId"),
        ("annotation class Audited", "Audited"),
        ("private abstract class Base", "Base"),
        ("typealias Handler<T> = (T) -> Unit", "Handler"),
        ("private typealias CustomerId = String", "CustomerId"),
    ])
    def test_types(self, line, name):
        assert _first(line, "kotlin") == (KOTLIN_TYPE, name)

    @pytest.mark.parametrize("line, name", [
        ("fun total(x: Int): Int = x", "total"),
        ("fun Invoice.isEmpty(): Boolean = lines.isEmpty()", "isEmpty"),
        ("fun <T> List<T>.second(): T = this[1]", "second"),
        ('fun String?.orBlank(): String = this ?: ""', "orBlank"),
        ("override suspend fun post(invoice: Invoice): Outcome {", "post"),
        ("@JvmStatic fun create() = InvoiceService()", "create"),
        ("private inline fun <reified T> load(): T {", "load"),
        ("infix fun Int.times(label: String) = label.repeat(this)", "times"),
    ])
    def test_functions(self, line, name):
        assert _first(line, "kotlin") == (KOTLIN_FUN, name)

    @pytest.mark.parametrize("line, name", [
        ("val total = 3", "total"),
        ("var owner: String? = null", "owner"),
        ("const val MAX_LINES = 50", "MAX_LINES"),
        ("lateinit var repository: InvoiceRepository", "repository"),
        ("override val name: String", "name"),
        ("private val cache by lazy { mutableMapOf<String, Int>() }", "cache"),
        ('@field:JsonProperty("ledger_id") val ledgerId: String,', "ledgerId"),
        ("val <T> List<T>.lastIndex: Int get() = size - 1", "lastIndex"),
        ("val Invoice.lineCount: Int", "lineCount"),
    ])
    def test_properties(self, line, name):
        assert _first(line, "kotlin") == (KOTLIN_PROPERTY, name)

    @pytest.mark.parametrize("line", [
        "companion object {",
        "object : Runnable {",
        'println("class Invoice")',
        "invoice.value = 3",
        "listOf(1, 2).map { it * 2 }",
        "when (status) {",
        "if (x) return",
        "return result",
        "val (first, second) = pair",
        "fun(x: Int) = x",
        "init {",
        "constructor(id: String) : this()",
        "super.onCreate(state)",
        "import org.example.ledger.Ledger as Book",
        "package com.acme.billing",
    ])
    def test_non_declarations_do_not_match(self, line):
        assert _first(line, "kotlin") is None


class TestImports:
    @pytest.mark.parametrize("line, expected", [
        ("import com.fasterxml.jackson.databind.ObjectMapper;",
         JvmImport("com.fasterxml.jackson.databind.ObjectMapper")),
        ("import static org.assertj.core.api.Assertions.assertThat;",
         JvmImport("org.assertj.core.api.Assertions.assertThat", static=True)),
        ("import org.springframework.web.bind.annotation.*;",
         JvmImport("org.springframework.web.bind.annotation", wildcard=True)),
        ("import static org.example.Limits.*;", JvmImport("org.example.Limits", static=True, wildcard=True)),
        ("import javax.servlet.http.HttpServletRequest;", JvmImport("javax.servlet.http.HttpServletRequest")),
        ("import java.util.List;", JvmImport("java.util.List")),
        ("    import com.acme.billing.Invoice; // moved in 2.0", JvmImport("com.acme.billing.Invoice")),
        ("import com.acme.billing.Invoice", JvmImport("com.acme.billing.Invoice")),
        ("import kotlinx.coroutines.flow.Flow as EventFlow",
         JvmImport("kotlinx.coroutines.flow.Flow", alias="EventFlow")),
        ("import org.example.ledger.*", JvmImport("org.example.ledger", wildcard=True)),
    ])
    def test_forms(self, line, expected):
        assert parse_import(line) == expected

    @pytest.mark.parametrize("line", [
        "package com.acme.billing;",
        "// import com.acme.billing.Invoice;",
        " * import com.acme.billing.Invoice;",
        "importantThing();",
        "import",
        "import a.b.*.C;",
        "import x from 'y';",
        "from acme import billing",
        "",
    ])
    def test_non_imports(self, line):
        assert parse_import(line) is None

    def test_parse_imports_keeps_order_and_skips_other_lines(self):
        lines = [
            "package com.acme.billing;",
            "",
            "import com.fasterxml.jackson.annotation.JsonProperty;",
            "import static java.util.Objects.requireNonNull;",
            "public class InvoiceService {",
            "import org.example.ledger.*;",
        ]
        assert parse_imports(lines) == [
            JvmImport("com.fasterxml.jackson.annotation.JsonProperty"),
            JvmImport("java.util.Objects.requireNonNull", static=True),
            JvmImport("org.example.ledger", wildcard=True),
        ]

    def test_parse_imports_accepts_a_one_shot_iterable(self):
        assert parse_imports(iter(["import a.b.C;"])) == [JvmImport("a.b.C")]

    def test_segments(self):
        assert parse_import("import com.acme.billing.Invoice;").segments == ("com", "acme", "billing", "Invoice")


class TestAnnotationStart:
    def test_cap_is_two(self):
        assert MAX_ANNOTATION_LINES == 2

    def test_no_annotation_starts_at_the_definition(self):
        lines = ["", "    private final String ledgerId;"]
        assert annotation_start(lines, 1) == 1

    def test_one_annotation(self):
        lines = ['    @JsonProperty("ledger_id")', "    private final String ledgerId;"]
        assert annotation_start(lines, 1) == 0

    def test_two_annotations(self):
        lines = ["    }", "    @Transactional", "    @Override", "    public void issue() {"]
        assert annotation_start(lines, 3) == 1

    def test_a_third_annotation_is_left_out(self):
        lines = ["@Service", "@Validated", "@RequiredArgsConstructor", "public class InvoiceService {"]
        assert annotation_start(lines, 3) == 1

    def test_a_blank_line_stops_the_walk(self):
        lines = ["@Service", "", "@Validated", "public class InvoiceService {"]
        assert annotation_start(lines, 3) == 2

    def test_a_javadoc_end_stops_the_walk(self):
        lines = ["/**", " * Issues invoices.", " */", "@Service", "public class InvoiceService {"]
        assert annotation_start(lines, 4) == 3

    @pytest.mark.parametrize("annotation", [
        '@Table(name = "invoice", indexes = @Index(columnList = "customer_id"))',
        "@Id @GeneratedValue(strategy = GenerationType.IDENTITY)",
        '@field:JsonProperty("ledger_id")',
        "@Deprecated // replaced by issueAll",
        "@org.springframework.lang.Nullable",
    ])
    def test_annotation_shapes(self, annotation):
        assert annotation_start(["    " + annotation, "    private String field;"], 1) == 0

    @pytest.mark.parametrize("above", [
        "@interface Audited {",
        "@Override public String toString() { return id; }",
        "// @Deprecated",
        "}",
    ])
    def test_non_annotation_lines_stop_the_walk(self, above):
        assert annotation_start([above, "    private String field;"], 1) == 1

    def test_index_zero(self):
        assert annotation_start(["public class InvoiceService {"], 0) == 0

    def test_max_annotations_is_a_keyword(self):
        lines = ["@Service", "@Validated", "public class InvoiceService {"]
        assert annotation_start(lines, 2, max_annotations=1) == 1
        assert annotation_start(lines, 2, max_annotations=0) == 2


SPRING_SERVICE = """\
package com.acme.billing;

import com.fasterxml.jackson.annotation.JsonProperty;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import static java.util.Objects.requireNonNull;

/**
 * Issues invoices for the example ledger. The ledger id
 * must be set;
 */
@Service
public class InvoiceService {

    public static final int MAX_LINES = 50;

    @JsonProperty("ledger_id")
    private final String ledgerId;

    private final Map<String, List<Invoice>> invoicesByCustomer = new HashMap<>();

    public InvoiceService(String ledgerId) {
        this.ledgerId = requireNonNull(ledgerId);
    }

    @Transactional
    @Override
    public <T extends Invoice> List<T> issue(String customerId, List<T> drafts) {
        if (drafts.size() > MAX_LINES) {
            throw new IllegalArgumentException("too many lines");
        }
        // int x;
        return drafts;
    }

    enum Status {
        DRAFT("draft"),
        ISSUED("issued"),
        @Deprecated
        VOID("void");

        private final String label;

        Status(String label) {
            this.label = label;
        }
    }
}
"""

KOTLIN_SERVICE = """\
package com.acme.billing

import com.fasterxml.jackson.annotation.JsonProperty
import kotlinx.coroutines.flow.Flow as EventFlow
import org.example.ledger.*

typealias CustomerId = String

const val MAX_LINES = 50

@Serializable
data class Invoice(
    @field:JsonProperty("ledger_id")
    val ledgerId: String,
    var total: Long = 0,
)

sealed class Outcome {
    object Pending : Outcome()
    data class Failed(val reason: String) : Outcome()
}

interface Ledger {
    suspend fun post(invoice: Invoice): Outcome
}

fun Invoice.isEmpty(): Boolean = total == 0L

class InvoiceService(private val ledger: Ledger) : Ledger by ledger {
    override suspend fun post(invoice: Invoice): Outcome {
        val result = ledger.post(invoice)
        return result
    }

    companion object {
        @JvmStatic
        fun create(ledger: Ledger) = InvoiceService(ledger)
    }
}
"""


class TestSnippets:
    def test_spring_service_definitions_and_entry_starts(self):
        assert _scan(SPRING_SERVICE, "java") == [
            ("InvoiceService", "public class InvoiceService {", "@Service"),
            ("MAX_LINES", "public static final int MAX_LINES = 50;", "public static final int MAX_LINES = 50;"),
            ("ledgerId", "private final String ledgerId;", '@JsonProperty("ledger_id")'),
            (
                "invoicesByCustomer",
                "private final Map<String, List<Invoice>> invoicesByCustomer = new HashMap<>();",
                "private final Map<String, List<Invoice>> invoicesByCustomer = new HashMap<>();",
            ),
            (
                "issue",
                "public <T extends Invoice> List<T> issue(String customerId, List<T> drafts) {",
                "@Transactional",
            ),
            ("Status", "enum Status {", "enum Status {"),
            ("DRAFT", 'DRAFT("draft"),', 'DRAFT("draft"),'),
            ("ISSUED", 'ISSUED("issued"),', 'ISSUED("issued"),'),
            ("VOID", 'VOID("void");', "@Deprecated"),
            ("label", "private final String label;", "private final String label;"),
        ]

    def test_spring_service_imports(self):
        assert parse_imports(SPRING_SERVICE.splitlines()) == [
            JvmImport("com.fasterxml.jackson.annotation.JsonProperty"),
            JvmImport("org.springframework.stereotype.Service"),
            JvmImport("org.springframework.transaction.annotation.Transactional"),
            JvmImport("java.util.Objects.requireNonNull", static=True),
        ]

    def test_kotlin_service_definitions_and_entry_starts(self):
        assert _scan(KOTLIN_SERVICE, "kotlin") == [
            ("CustomerId", "typealias CustomerId = String", "typealias CustomerId = String"),
            ("MAX_LINES", "const val MAX_LINES = 50", "const val MAX_LINES = 50"),
            ("Invoice", "data class Invoice(", "@Serializable"),
            ("ledgerId", "val ledgerId: String,", '@field:JsonProperty("ledger_id")'),
            ("total", "var total: Long = 0,", "var total: Long = 0,"),
            ("Outcome", "sealed class Outcome {", "sealed class Outcome {"),
            ("Pending", "object Pending : Outcome()", "object Pending : Outcome()"),
            ("Failed", "data class Failed(val reason: String) : Outcome()",
             "data class Failed(val reason: String) : Outcome()"),
            ("Ledger", "interface Ledger {", "interface Ledger {"),
            ("post", "suspend fun post(invoice: Invoice): Outcome", "suspend fun post(invoice: Invoice): Outcome"),
            ("isEmpty", "fun Invoice.isEmpty(): Boolean = total == 0L", "fun Invoice.isEmpty(): Boolean = total == 0L"),
            (
                "InvoiceService",
                "class InvoiceService(private val ledger: Ledger) : Ledger by ledger {",
                "class InvoiceService(private val ledger: Ledger) : Ledger by ledger {",
            ),
            (
                "post",
                "override suspend fun post(invoice: Invoice): Outcome {",
                "override suspend fun post(invoice: Invoice): Outcome {",
            ),
            ("result", "val result = ledger.post(invoice)", "val result = ledger.post(invoice)"),
            ("create", "fun create(ledger: Ledger) = InvoiceService(ledger)", "@JvmStatic"),
        ]

    def test_kotlin_service_imports(self):
        assert parse_imports(KOTLIN_SERVICE.splitlines()) == [
            JvmImport("com.fasterxml.jackson.annotation.JsonProperty"),
            JvmImport("kotlinx.coroutines.flow.Flow", alias="EventFlow"),
            JvmImport("org.example.ledger", wildcard=True),
        ]
