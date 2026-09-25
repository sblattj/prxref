"""Issue #20 acceptance: JVM chunk context through the real ``orchestrate_review``, end to end.

Every test drives ``orchestrate_review`` over ``tests/fixtures/issue20`` through
the version-neutral harness in ``tests/fixtures/issue20/harness.py``: a
read-and-list fake forge that records every ``get_file_content`` and
``list_paths`` call, and an LLM that captures every worker and sweep prompt and
answers with no findings. The reviewer is the real one, ``post`` is false and
every changed file is its own chunk. Each test asserts on what a user sees: the
prompts, the forge's reads, the run record and the trace.

The fixture holds a Maven build (``shop/``: a parent with a property and a BOM
import, a module whose ``jackson-databind`` has no version, and a module whose
``pom.xml`` is malformed), a Gradle build (``inventory/``: a version catalog
reached through ``version.ref``, and a ``reports`` subproject whose
``build.gradle`` is garbage) and a Python control (``tools/``).

The regression golden, ``golden_0_16_0.json``, was captured from the RELEASED
prxref 0.16.0 by ``make_golden.py`` (its docstring has the command); it is
never regenerated from the tree. No test touches the network.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest

from prxref import jvm_lang
from prxref.chunk_context import (
    DEFINITIONS_HEADER,
    DEPENDENCY_HEADER,
    MAX_DEFINITION_CHARS,
    MAX_DEFINITION_ENTRIES,
    chunk_files,
)
from prxref.triage import build_chunks, parse_unified_diff
from tests.fixtures.issue20.harness import (
    CAPS_CONSTANTS,
    CAPS_PATH,
    CHANGED,
    DIFF,
    EXPORT_ORDERS,
    FIXTURE,
    LEGACY_EXPORT,
    MAX_CHUNKS,
    ORDER_MAPPER,
    PRICE_CALCULATOR,
    REPO,
    REPORT_WRITER,
    STOCK_CLIENT,
    STOCK_REPORT,
    FixtureForge,
    caps_design,
    golden_run,
    review,
)

GOLDEN = json.loads((FIXTURE / "golden_0_16_0.json").read_text(encoding="utf-8"))
OUTPUT_HEADER = "\n\n## Output Format"
ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"
JVM_CHANGED = (ORDER_MAPPER, PRICE_CALCULATOR, LEGACY_EXPORT, STOCK_CLIENT, STOCK_REPORT, REPORT_WRITER)
BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts", "libs.versions.toml")

JACKSON_MAVEN = "com.fasterxml.jackson.core:jackson-databind@2.17.2"
SLF4J_MANAGED = "org.slf4j:slf4j-api@(managed by org.springframework.boot:spring-boot-dependencies@3.3.4)"
JACKSON_GRADLE = "com.fasterxml.jackson.core:jackson-databind@2.16.1"

PRICE_ENTRIES = [
    f"{PRICE_CALCULATOR}:11:     @Deprecated\n"
    "    public static final int MAX_ITEMS = 50;",
    f"{PRICE_CALCULATOR}:14:     @Nullable\n"
    '    @JsonProperty("rate")\n'
    "    private BigDecimal taxRate;",
    f"{PRICE_CALCULATOR}:20:     @Transactional\n"
    "    public BigDecimal applyDiscount(BigDecimal amount, int items) {\n"
    '        BigDecimal factor = items > 10 ? new BigDecimal("0.95") : BigDecimal.ONE;\n'
    "        return amount.multiply(factor);\n"
    "    }",
]
KOTLIN_ENTRIES = [
    f"{STOCK_REPORT}:12: data class StockLevel(val sku: String, val onHand: Int)",
    f"{STOCK_REPORT}:14: fun formatLevel(level: StockLevel): String {{\n"
    '    return "${level.sku}: ${level.onHand}"\n'
    "}",
]
OWN_FILE_DEFINITIONS = [(PRICE_CALCULATOR, 11), (PRICE_CALCULATOR, 14), (PRICE_CALCULATOR, 20),
                        (STOCK_REPORT, 12), (STOCK_REPORT, 14)]


def _owner(user: str) -> str:
    (owner,) = [path for path in CHANGED if f"diff --git a/{path} " in user]
    return owner


def _worker_prompts(calls) -> dict[str, tuple[str, str]]:
    """Each chunk's one worker ``(system, user)``, keyed by the chunk's file; the sweep (always last) is left out."""
    *workers, _sweep = calls
    prompts: dict[str, tuple[str, str]] = {}
    for system, user in workers:
        owner = _owner(user)
        assert owner not in prompts, f"{owner} was prompted twice"
        prompts[owner] = (system, user)
    assert set(prompts) == set(CHANGED)
    return prompts


def _users(llm) -> dict[str, str]:
    return {path: user for path, (_system, user) in _worker_prompts(llm.calls).items()}


def _block(prompt: str, header: str) -> str:
    """The prompt's ``header`` block, header included, up to the next block or the output section; "" if absent."""
    if header not in prompt:
        return ""
    start = prompt.index(header)
    ends = [
        end for end in (prompt.find("\n\n### ", start + len(header)), prompt.find(OUTPUT_HEADER, start))
        if end >= 0
    ]
    return prompt[start:min(ends)]


def _dependency_lines(prompt: str) -> list[str]:
    block = _block(prompt, DEPENDENCY_HEADER)
    return block.split("\n\n", 1)[1].split("\n") if block else []


def _definitions(prompt: str) -> str:
    block = _block(prompt, DEFINITIONS_HEADER)
    return block.split("\n\n", 1)[1] if block else ""


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _is_jvm_read(path: str) -> bool:
    return path.endswith((".java", ".kt")) or path.rsplit("/", 1)[-1] in BUILD_FILES


def _file_line(path: str, number: int) -> str:
    return (REPO / path).read_text(encoding="utf-8").splitlines()[number - 1]


def _added(path: str) -> tuple[str, ...]:
    (entry,) = [f for f in chunk_files(parse_unified_diff(DIFF)) if f.path == path]
    return entry.added


@pytest.fixture(scope="module")
def regression_runs() -> tuple[dict, dict]:
    """``(golden, tip)``: the 0.16.0 fixture run from the golden file and the same run at the tip."""
    return GOLDEN["runs"]["fixture"], golden_run("fixture")


class TestChunking:
    """The fixture chunks one file per chunk, and no expected definition sits inside a hunk."""

    def test_each_changed_file_is_its_own_chunk(self):
        chunks = build_chunks(parse_unified_diff(DIFF), max_chunks=MAX_CHUNKS, max_files_per_chunk=1)
        assert len(chunks) == len(CHANGED) == 7
        assert sorted(f.path for chunk in chunks for f in chunk) == sorted(CHANGED)
        assert all(len(chunk) == 1 for chunk in chunks)

    def test_the_review_prompts_every_chunk_once_plus_the_sweep(self):
        _result, _forge, llm = review(max_workers=1)
        assert len(llm.calls) == len(CHANGED) + 1
        _worker_prompts(llm.calls)

    def test_every_expected_definition_lies_outside_its_hunks(self):
        hunks = {f.path: f.hunk_lines for f in chunk_files(parse_unified_diff(DIFF))}
        for path, line in OWN_FILE_DEFINITIONS + [(PRICE_CALCULATOR, 18), (EXPORT_ORDERS, 7)]:
            assert line not in hunks[path], (path, line)


class TestMavenDependencyVersions:
    """A versionless module dependency resolves through the parent's property; a BOM-managed one names the BOM."""

    def test_the_module_prompt_carries_the_resolved_and_the_managed_line(self):
        _result, forge, llm = review(max_workers=1)
        assert _dependency_lines(_users(llm)[ORDER_MAPPER]) == [JACKSON_MAVEN, SLF4J_MANAGED]
        assert {"shop/shop-api/pom.xml", "shop/pom.xml"} <= set(forge.content_calls)

    def test_the_module_pom_declares_jackson_without_a_version(self):
        child = (REPO / "shop/shop-api/pom.xml").read_text(encoding="utf-8")
        jackson = child.split("<artifactId>jackson-databind</artifactId>", 1)[1].split("</dependency>", 1)[0]
        assert "<version>" not in jackson
        parent = (REPO / "shop/pom.xml").read_text(encoding="utf-8")
        assert "<jackson.version>2.17.2</jackson.version>" in parent
        assert "<type>pom</type>" in parent and "<scope>import</scope>" in parent


class TestGradleDependencyVersions:
    """A catalog alias resolves through ``version.ref``, for a Java and a Kotlin file of the module."""

    def test_the_java_and_kotlin_prompts_carry_the_catalog_version(self):
        _result, forge, llm = review(max_workers=1)
        users = _users(llm)
        assert _dependency_lines(users[STOCK_CLIENT]) == [JACKSON_GRADLE]
        assert _dependency_lines(users[STOCK_REPORT]) == [JACKSON_GRADLE]
        assert "inventory/gradle/libs.versions.toml" in forge.content_calls

    def test_the_catalog_uses_version_ref(self):
        catalog = (REPO / "inventory/gradle/libs.versions.toml").read_text(encoding="utf-8")
        assert 'version.ref = "jackson"' in catalog
        assert "libs.jackson.databind" in (REPO / "inventory/build.gradle.kts").read_text(encoding="utf-8")


class TestSkippedImports:
    """A JDK import and an import of the build's own group yield no dependency line, though both are declared."""

    def test_the_maven_module_skips_java_util_and_its_own_group(self):
        assert "import java.util.List;" in _added(ORDER_MAPPER)
        assert "import com.example.shop.core.Money;" in _added(ORDER_MAPPER)
        assert "<artifactId>shop-core</artifactId>" in (REPO / "shop/shop-api/pom.xml").read_text(encoding="utf-8")
        _result, _forge, llm = review(max_workers=1)
        lines = _dependency_lines(_users(llm)[ORDER_MAPPER])
        assert not [line for line in lines if line.startswith(("java.", "com.example.shop:"))]

    def test_the_gradle_module_skips_its_own_group(self):
        assert "import com.example.inventory.model.Sku;" in _added(STOCK_CLIENT)
        build = (REPO / "inventory/build.gradle.kts").read_text(encoding="utf-8")
        assert 'group = "com.example.inventory"' in build and "com.example.inventory:inventory-model" in build
        _result, _forge, llm = review(max_workers=1)
        assert not [line for line in _dependency_lines(_users(llm)[STOCK_CLIENT]) if "com.example.inventory" in line]

    def test_a_file_adding_only_a_jdk_import_reads_no_build_file(self):
        assert "import java.util.Objects;" in _added(PRICE_CALCULATOR)
        _result, forge, llm = review(max_workers=1)
        assert DEPENDENCY_HEADER not in _users(llm)[PRICE_CALCULATOR]
        folder = PRICE_CALCULATOR.rsplit("/", 1)[0]
        assert not [path for path in forge.content_calls if path.startswith(folder + "/") and path != PRICE_CALCULATOR]


class TestDefinitions:
    """A constant, a field and a method outside the hunks, each from its first annotation line; no keyword."""

    def test_the_java_prompt_carries_all_three_entries_from_the_first_annotation(self):
        for line, annotation in ((11, "@Deprecated"), (14, "@Nullable"), (20, "@Transactional")):
            assert _file_line(PRICE_CALCULATOR, line).strip() == annotation
        _result, _forge, llm = review(max_workers=1)
        assert _definitions(_users(llm)[PRICE_CALCULATOR]) == "\n".join(PRICE_ENTRIES)

    def test_a_keyword_named_field_is_never_a_referenced_name(self):
        assert _file_line(PRICE_CALCULATOR, 18).strip() == "private boolean open;"
        assert any("!open" in text for text in _added(PRICE_CALCULATOR))
        assert "open" in jvm_lang.JAVA_KEYWORDS and jvm_lang.JAVA_FIELD_RE.match(_file_line(PRICE_CALCULATOR, 18))
        _result, _forge, llm = review(max_workers=1)
        prompt = _users(llm)[PRICE_CALCULATOR]
        assert f"{PRICE_CALCULATOR}:18: " not in prompt
        assert "private boolean open;" not in _definitions(prompt)


class TestKotlinDefinitions:
    """A Kotlin added line referencing a ``fun`` and a ``class`` outside its hunk gets both."""

    def test_the_kotlin_prompt_carries_the_class_and_the_fun(self):
        _result, _forge, llm = review(max_workers=1)
        assert _definitions(_users(llm)[STOCK_REPORT]) == "\n".join(KOTLIN_ENTRIES)


class TestBrokenBuilds:
    """A malformed ``pom.xml`` and a garbage ``build.gradle`` yield no dependency line, and the review completes."""

    def test_no_dependency_line_and_the_broken_build_files_were_read(self):
        _result, forge, llm = review(max_workers=1)
        users = _users(llm)
        assert DEPENDENCY_HEADER not in users[LEGACY_EXPORT]
        assert DEPENDENCY_HEADER not in users[REPORT_WRITER]
        assert {"shop/shop-legacy/pom.xml", "inventory/reports/build.gradle"} <= set(forge.content_calls)

    def test_the_run_record_the_trace_and_the_log_show_a_completed_review(self, tmp_path, caplog):
        trace = tmp_path / "trace.jsonl"
        with caplog.at_level(logging.DEBUG, logger="prxref"):
            result, _forge, _llm = review(max_workers=1, trace_file=str(trace))
        assert result["chunks_failed"] == 0
        events = _events(trace)
        steps = [(e["node"], e["phase"]) for e in events]
        assert ("chunk", "fail") not in steps
        assert steps.count(("run", "ok")) == 1
        chunk_events = [(e["phase"], e["meta"]) for e in events if e["node"] == "chunk"]
        started = {meta["index"]: meta["files"] for phase, meta in chunk_events if phase == "start"}
        finished = [meta["index"] for phase, meta in chunk_events if phase == "ok"]
        assert sorted(finished) == sorted(started) == list(range(1, len(CHANGED) + 1))
        assert sorted(path for files in started.values() for path in files) == sorted(CHANGED)
        records = [r for r in caplog.records if r.name.startswith("prxref")]
        assert not [r.getMessage() for r in records if r.levelno >= logging.WARNING]
        assert not [r.getMessage() for r in records if "chunk context unavailable" in r.getMessage()]

    def test_control_without_the_broken_files_the_walk_reaches_the_enclosing_build(self, tmp_path):
        root = tmp_path / "repo"
        shutil.copytree(REPO, root)
        (root / "shop/shop-legacy/pom.xml").unlink()
        (root / "inventory/reports/build.gradle").unlink()
        _result, _forge, llm = review(FixtureForge(root=root), max_workers=1)
        users = _users(llm)
        assert _dependency_lines(users[LEGACY_EXPORT]) == [JACKSON_MAVEN]
        assert _dependency_lines(users[REPORT_WRITER]) == [JACKSON_GRADLE]


class TestNoRegressionAgainstTheReleased016:
    """Against the 0.16.0 golden: the Python control and the sweep are byte-identical; JVM chunks only gain blocks."""

    EXPECTED_BLOCKS = {
        ORDER_MAPPER: {DEPENDENCY_HEADER},
        PRICE_CALCULATOR: {DEFINITIONS_HEADER},
        LEGACY_EXPORT: set(),
        STOCK_CLIENT: {DEPENDENCY_HEADER},
        STOCK_REPORT: {DEPENDENCY_HEADER, DEFINITIONS_HEADER},
        REPORT_WRITER: set(),
    }

    def test_the_golden_came_from_the_installed_release(self):
        assert GOLDEN["prxref_version"] == "0.16.0"
        assert GOLDEN["generated_from"] == "installed distribution"

    def test_the_python_control_and_the_sweep_are_byte_identical(self, regression_runs):
        golden, tip = regression_runs
        golden_calls = [(p["system"], p["user"]) for p in golden["prompts"]]
        tip_calls = [(p["system"], p["user"]) for p in tip["prompts"]]
        assert len(tip_calls) == len(golden_calls) == len(CHANGED) + 1
        assert [_owner(user) for _s, user in tip_calls[:-1]] == [_owner(user) for _s, user in golden_calls[:-1]]
        assert _worker_prompts(tip_calls)[EXPORT_ORDERS] == _worker_prompts(golden_calls)[EXPORT_ORDERS]
        assert DEPENDENCY_HEADER in _worker_prompts(golden_calls)[EXPORT_ORDERS][1]
        assert DEFINITIONS_HEADER in _worker_prompts(golden_calls)[EXPORT_ORDERS][1]
        assert tip_calls[-1] == golden_calls[-1]

    @pytest.mark.parametrize("path", JVM_CHANGED)
    def test_a_jvm_chunk_differs_only_by_the_added_blocks(self, regression_runs, path):
        golden, tip = regression_runs
        golden_system, golden_user = _worker_prompts([(p["system"], p["user"]) for p in golden["prompts"]])[path]
        tip_system, tip_user = _worker_prompts([(p["system"], p["user"]) for p in tip["prompts"]])[path]
        assert tip_system == golden_system
        assert DEPENDENCY_HEADER not in golden_user and DEFINITIONS_HEADER not in golden_user
        stripped = tip_user
        removed = set()
        for header in (DEPENDENCY_HEADER, DEFINITIONS_HEADER):
            block = _block(tip_user, header)
            if block:
                assert stripped.count("\n\n" + block) == 1
                stripped = stripped.replace("\n\n" + block, "", 1)
                removed.add(header)
        assert removed == self.EXPECTED_BLOCKS[path]
        assert stripped == golden_user

    def test_every_read_0_16_0_made_is_made_again_and_the_rest_are_jvm(self, regression_runs):
        golden, tip = regression_runs
        assert [path for path in tip["content_calls"] if not _is_jvm_read(path)] == golden["content_calls"]
        assert not [path for path in golden["content_calls"] if _is_jvm_read(path)]
        assert tip["list_calls"] == golden["list_calls"]


@pytest.mark.parametrize("mode", ["diff", "repo"])
class TestNoDuplicatesAcrossSources:
    """With repository context on, each own-file JVM definition is rendered once, while a cross-chunk one appears."""

    def test_each_own_file_definition_appears_exactly_once(self, mode):
        _result, _forge, llm = review(max_workers=1, repo_context=mode)
        users = _users(llm)
        counts = {(path, line): users[path].count(f"{path}:{line}: ") for path, line in OWN_FILE_DEFINITIONS}
        assert counts == {key: 1 for key in OWN_FILE_DEFINITIONS}
        assert _definitions(users[PRICE_CALCULATOR]) == "\n".join(PRICE_ENTRIES)
        assert _definitions(users[STOCK_REPORT]) == "\n".join(KOTLIN_ENTRIES)

    def test_control_the_cross_chunk_source_is_live_and_renders_once(self, mode):
        entry = f"{STOCK_REPORT}:12: "
        _result, _forge, off = review(max_workers=1)
        assert entry not in _users(off)[STOCK_CLIENT]
        _result, _forge, llm = review(max_workers=1, repo_context=mode)
        assert _users(llm)[STOCK_CLIENT].count(entry) == 1


@pytest.mark.parametrize("mode", ["off", "diff", "repo"])
class TestTheDefinitionCapHolds:
    """A Java file referencing more annotated constants than the entry cap renders the cap plus an omission line."""

    def test_the_cap_and_the_omission_line(self, mode):
        assert CAPS_CONSTANTS > MAX_DEFINITION_ENTRIES == 40
        diff, overlay = caps_design()
        _result, _forge, llm = review(FixtureForge(diff, overlay=overlay), max_workers=1, repo_context=mode)
        (_system, user), _sweep = llm.calls
        lines = _definitions(user).split("\n")
        heads = [line for line in lines if line.startswith(f"{CAPS_PATH}:")]
        assert len(heads) == MAX_DEFINITION_ENTRIES
        assert heads == [f"{CAPS_PATH}:{5 + 3 * n}:     @Deprecated" for n in range(MAX_DEFINITION_ENTRIES)]
        assert lines[-1] == f"{ELLIPSIS} {CAPS_CONSTANTS - MAX_DEFINITION_ENTRIES} more definitions omitted"
        assert "LIMIT_40 = 40;" in user.split(DEFINITIONS_HEADER, 1)[1]
        assert "LIMIT_41 = 41;" not in user.split(DEFINITIONS_HEADER, 1)[1]
        assert len(_definitions(user)) < MAX_DEFINITION_CHARS

    def test_every_constant_is_referenced_on_an_added_line(self, mode):
        diff, _overlay = caps_design()
        (entry,) = chunk_files(parse_unified_diff(diff))
        referenced = {
            name for text in entry.added for name in text.replace(";", " ").replace("+", " ").split()
            if name.startswith("LIMIT_")
        }
        assert sorted(referenced) == [f"LIMIT_{n:02d}" for n in range(1, CAPS_CONSTANTS + 1)]
