"""Unit tests for :mod:`prxref.jvm_deps`, the JVM dependency lines behind #20.

Every test reads through a counting fake repository, so the manifest walk's
probe order, its first-hit stop, the skips that must not read at all, and the
one-read-per-path rule are pinned by the exact list of paths asked for.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from prxref import jvm_deps
from prxref.jvm_deps import (
    CATALOG_FILE_NAME,
    MANIFEST_NAMES,
    MIN_GROUP_PREFIX_SEGMENTS,
    MIN_SHARED_SEGMENTS,
    SKIPPED_ROOTS,
    dependency_lines,
)

DATABIND = ("com.fasterxml.jackson.core", "jackson-databind")
JACKSON_CORE = ("com.fasterxml.jackson.core", "jackson-core")
JACKSON_ANNOTATIONS = ("com.fasterxml.jackson.core", "jackson-annotations")
SPRING_BOM = ("org.springframework.boot", "spring-boot-dependencies")
SLF4J_API = ("org.slf4j", "slf4j-api")

OBJECT_MAPPER = "import com.fasterxml.jackson.databind.ObjectMapper;"
LOGGER = "import org.slf4j.Logger;"

DATABIND_BY_SPRING = (
    "com.fasterxml.jackson.core:jackson-databind"
    "@(managed by org.springframework.boot:spring-boot-dependencies@3.3.4)"
)


class Repo:
    """A fake repository reader that records every path it is asked for.

    ``files`` maps a repository-relative path to its text; any other path reads
    as ``None``. A path in ``raising`` raises instead.
    """

    def __init__(self, files: dict[str, object] | None = None, *, raising: frozenset[str] = frozenset()):
        self.files = dict(files or {})
        self.raising = raising
        self.reads: list[str] = []

    def __call__(self, path: str):
        self.reads.append(path)
        if path in self.raising:
            raise OSError(f"cannot read {path}")
        return self.files.get(path)


def pom(*parts: str, prolog: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'{prolog}<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
        "  <modelVersion>4.0.0</modelVersion>\n" + "\n".join(parts) + "\n</project>\n"
    )


def coords(group: str, artifact: str, version: str = "1.0.0") -> str:
    return f"<groupId>{group}</groupId><artifactId>{artifact}</artifactId><version>{version}</version>"


def parent(group: str, artifact: str, version: str, relative_path: str | None = None) -> str:
    relative = "" if relative_path is None else f"<relativePath>{relative_path}</relativePath>"
    return (
        f"<parent><groupId>{group}</groupId><artifactId>{artifact}</artifactId>"
        f"<version>{version}</version>{relative}</parent>"
    )


def dep(group: str, artifact: str, version: str | None = None, *, bom: bool = False) -> str:
    parts = [f"<groupId>{group}</groupId>", f"<artifactId>{artifact}</artifactId>"]
    if version is not None:
        parts.append(f"<version>{version}</version>")
    if bom:
        parts.append("<type>pom</type><scope>import</scope>")
    return "<dependency>" + "".join(parts) + "</dependency>"


def deps(*items: str) -> str:
    return "<dependencies>" + "".join(items) + "</dependencies>"


def managed(*items: str) -> str:
    return "<dependencyManagement>" + deps(*items) + "</dependencyManagement>"


def props(**values: str) -> str:
    return "<properties>" + "".join(f"<{k}>{v}</{k}>" for k, v in values.items()) + "</properties>"


def simple_pom(*dependencies: str, group: str = "com.acme", artifact: str = "app") -> str:
    return pom(coords(group, artifact), deps(*dependencies))


def gradle(*lines: str) -> str:
    return "dependencies {\n" + "".join(f"    {line}\n" for line in lines) + "}\n"


def probes(*directories: str) -> list[str]:
    return [
        f"{directory}/{name}" if directory else name
        for directory in directories
        for name in ("pom.xml", "build.gradle.kts", "build.gradle")
    ]


class TestModuleSurface:
    def test_the_manifest_names_are_probed_pom_first(self):
        assert MANIFEST_NAMES == ("pom.xml", "build.gradle.kts", "build.gradle")
        assert CATALOG_FILE_NAME == "libs.versions.toml"

    def test_the_skipped_roots_and_the_matching_thresholds(self):
        assert SKIPPED_ROOTS == frozenset({"java", "jdk", "sun", "kotlin"})
        assert (MIN_GROUP_PREFIX_SEGMENTS, MIN_SHARED_SEGMENTS) == (2, 3)

    def test_the_module_imports_only_the_stdlib_and_the_jvm_leaf_modules(self):
        tree = ast.parse(Path(jvm_deps.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0
                if node.module == "prxref":
                    imported.update(f"prxref.{alias.name}" for alias in node.names)
                else:
                    imported.add(node.module or "")
        prxref_modules = {name for name in imported if name.split(".")[0] == "prxref"}
        assert prxref_modules == {"prxref.jvm_lang", "prxref.jvm_maven", "prxref.jvm_gradle"}
        others = {name.split(".")[0] for name in imported - prxref_modules}
        assert others <= set(sys.stdlib_module_names) | {"__future__"}


class TestMatching:
    @pytest.mark.parametrize(("dependency", "line", "expected"), [
        (("javax.servlet", "javax.servlet-api", "4.0.1"), "import javax.servlet.http.HttpServletRequest;",
         "javax.servlet:javax.servlet-api@4.0.1"),
        ((*SLF4J_API, "2.0.13"), LOGGER, "org.slf4j:slf4j-api@2.0.13"),
    ])
    def test_a_two_segment_group_that_prefixes_the_import_matches(self, dependency, line, expected):
        repo = Repo({"pom.xml": simple_pom(dep(*dependency))})
        assert dependency_lines("src/App.java", [line], repo) == [expected]

    def test_the_jackson_tie_break_picks_databind_and_only_databind(self):
        repo = Repo({"pom.xml": simple_pom(
            dep(*JACKSON_CORE, "2.17.1"), dep(*DATABIND, "2.17.1"), dep(*JACKSON_ANNOTATIONS, "2.17.1"),
        )})
        assert dependency_lines("src/App.java", [OBJECT_MAPPER], repo) == [
            "com.fasterxml.jackson.core:jackson-databind@2.17.1",
        ]

    def test_a_tie_lists_every_tied_candidate(self):
        repo = Repo({"pom.xml": simple_pom(dep(*SLF4J_API, "2.0.13"), dep("org.slf4j", "jul-to-slf4j", "2.0.13"))})
        assert dependency_lines("src/App.java", [LOGGER], repo) == [
            "org.slf4j:jul-to-slf4j@2.0.13",
            "org.slf4j:slf4j-api@2.0.13",
        ]

    def test_two_shared_segments_without_a_group_prefix_do_not_match(self):
        repo = Repo({"pom.xml": simple_pom(
            dep("org.springframework.boot", "spring-boot-starter-web", "3.3.4"),
            dep("org.springframework", "spring-web", "6.1.13"),
        )})
        added = ["import org.springframework.web.bind.annotation.RestController;"]
        assert dependency_lines("src/App.java", added, repo) == ["org.springframework:spring-web@6.1.13"]

    def test_a_single_segment_group_never_matches(self):
        repo = Repo({"pom.xml": simple_pom(dep("junit", "junit", "4.13.2"))})
        assert dependency_lines("src/AppTest.java", ["import junit.framework.TestCase;"], repo) == []

    def test_a_static_import_matches_through_its_member(self):
        repo = Repo({"pom.xml": simple_pom(
            dep("org.junit.jupiter", "junit-jupiter-engine", "5.10.2"),
            dep("org.junit.jupiter", "junit-jupiter-api", "5.10.2"),
        )})
        added = ["import static org.junit.jupiter.api.Assertions.assertEquals;"]
        assert dependency_lines("src/AppTest.java", added, repo) == ["org.junit.jupiter:junit-jupiter-api@5.10.2"]

    def test_a_wildcard_import_equal_to_the_group_matches(self):
        repo = Repo({"pom.xml": simple_pom(dep(*SLF4J_API, "2.0.13"))})
        assert dependency_lines("src/App.java", ["import org.slf4j.*;"], repo) == ["org.slf4j:slf4j-api@2.0.13"]

    def test_a_kotlin_alias_import_matches_its_qualified_name(self):
        text = gradle('implementation("com.fasterxml.jackson.core:jackson-databind:2.17.1")')
        repo = Repo({"build.gradle.kts": text})
        added = ["import com.fasterxml.jackson.databind.ObjectMapper as Mapper"]
        assert dependency_lines("src/App.kt", added, repo) == ["com.fasterxml.jackson.core:jackson-databind@2.17.1"]

    def test_lines_are_sorted_and_deduplicated(self):
        repo = Repo({"pom.xml": simple_pom(dep(*SLF4J_API, "2.0.13"), dep(*DATABIND, "2.17.1"))})
        added = [LOGGER, "import org.slf4j.LoggerFactory;", OBJECT_MAPPER, OBJECT_MAPPER]
        assert dependency_lines("src/App.java", added, repo) == [
            "com.fasterxml.jackson.core:jackson-databind@2.17.1",
            "org.slf4j:slf4j-api@2.0.13",
        ]


class TestSkips:
    @pytest.mark.parametrize("line", [
        "import java.util.List;",
        "import static java.util.Objects.requireNonNull;",
        "import java.util.concurrent.*;",
        "import jdk.internal.misc.Unsafe;",
        "import sun.misc.Unsafe;",
        "import kotlin.collections.List",
        "import kotlin.math.max as maximum",
    ])
    def test_a_jdk_or_language_import_is_skipped_without_reading(self, line):
        repo = Repo({"pom.xml": simple_pom(dep(*SLF4J_API, "2.0.13"))})
        assert dependency_lines("src/App.java", [line], repo) == []
        assert repo.reads == []

    def test_a_file_whose_every_import_is_skipped_reads_nothing(self):
        repo = Repo({"pom.xml": simple_pom(dep(*SLF4J_API, "2.0.13"))})
        added = ["package com.acme;", "import java.util.List;", "import sun.misc.Unsafe;", "import jdk.jfr.Event;",
                 "import kotlin.io.println", "class App {}"]
        assert dependency_lines("src/App.kt", added, repo) == []
        assert repo.reads == []

    def test_added_lines_without_an_import_read_nothing(self):
        repo = Repo({"pom.xml": simple_pom(dep(*SLF4J_API, "2.0.13"))})
        assert dependency_lines("src/App.java", ["    Logger log = null;", "}"], repo) == []
        assert repo.reads == []

    def test_kotlinx_is_not_skipped_as_kotlin(self):
        text = gradle('implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1")')
        repo = Repo({"build.gradle.kts": text})
        assert dependency_lines("App.kt", ["import kotlinx.coroutines.flow.Flow"], repo) == []
        assert repo.reads == ["pom.xml", "build.gradle.kts"]

    @pytest.mark.parametrize("path", ["app.py", "src/index.ts", "README.md", "src/App.scala"])
    def test_a_non_jvm_path_reads_nothing(self, path):
        repo = Repo({"pom.xml": simple_pom(dep(*SLF4J_API, "2.0.13"))})
        assert dependency_lines(path, ["import org.slf4j.Logger", "import os.path"], repo) == []
        assert repo.reads == []

    def test_imports_under_the_maven_group_are_skipped(self):
        repo = Repo({"pom.xml": simple_pom(
            dep("com.acme.app", "app-core", "1.0.0"), dep(*SLF4J_API, "2.0.13"), group="com.acme.app",
        )})
        added = ["import com.acme.app.util.Strings;", LOGGER]
        assert dependency_lines("src/App.java", added, repo) == ["org.slf4j:slf4j-api@2.0.13"]

    def test_imports_under_the_gradle_group_are_skipped(self):
        text = 'group = "com.acme.app"\n' + gradle(
            'implementation("com.acme.app:app-core:1.0.0")', 'implementation("org.slf4j:slf4j-api:2.0.13")',
        )
        repo = Repo({"build.gradle.kts": text})
        added = ["import com.acme.app.util.Strings", "import org.slf4j.Logger"]
        assert dependency_lines("src/App.kt", added, repo) == ["org.slf4j:slf4j-api@2.0.13"]

    def test_the_maven_group_falls_back_to_the_parent_group(self):
        text = pom(parent("com.acme.app", "acme-parent", "1.0.0", ""), "<artifactId>service</artifactId>",
                   deps(dep("com.acme.app", "app-core", "1.0.0")))
        repo = Repo({"pom.xml": text})
        assert dependency_lines("App.java", ["import com.acme.app.util.Strings;"], repo) == []
        assert repo.reads == ["pom.xml"]

    def test_when_every_import_is_under_the_own_group_the_manifest_is_still_read(self):
        repo = Repo({"pom.xml": simple_pom(dep("com.acme.app", "app-core", "1.0.0"), group="com.acme.app")})
        assert dependency_lines("App.java", ["import com.acme.app.util.Strings;"], repo) == []
        assert repo.reads == ["pom.xml"]

    def test_the_own_group_is_a_segment_prefix_not_a_string_prefix(self):
        repo = Repo({"pom.xml": simple_pom(dep("com.acmecorp.billing", "billing-api", "3.1.0"), group="com.acme")})
        added = ["import com.acmecorp.billing.Invoice;"]
        assert dependency_lines("App.java", added, repo) == ["com.acmecorp.billing:billing-api@3.1.0"]


class TestWalk:
    def test_probe_order_and_first_hit_stop(self):
        repo = Repo({
            "a/pom.xml": simple_pom(dep(*SLF4J_API, "2.0.13")),
            "pom.xml": simple_pom(dep(*SLF4J_API, "1.7.36")),
            "build.gradle": gradle("implementation 'org.slf4j:slf4j-api:1.7.0'"),
        })
        assert dependency_lines("a/b/C.java", [LOGGER], repo) == ["org.slf4j:slf4j-api@2.0.13"]
        assert repo.reads == ["a/b/pom.xml", "a/b/build.gradle.kts", "a/b/build.gradle", "a/pom.xml"]

    def test_the_walk_climbs_to_the_repository_root(self):
        repo = Repo({"build.gradle": gradle("implementation 'org.slf4j:slf4j-api:2.0.13'")})
        assert dependency_lines("src/main/java/com/acme/App.java", [LOGGER], repo) == ["org.slf4j:slf4j-api@2.0.13"]
        assert repo.reads == probes(
            "src/main/java/com/acme", "src/main/java/com", "src/main/java", "src/main", "src", "",
        )

    def test_no_manifest_anywhere_reads_every_level_once_and_gives_nothing(self):
        repo = Repo()
        assert dependency_lines("a/b/C.java", [LOGGER], repo) == []
        assert repo.reads == probes("a/b", "a", "")

    def test_a_pom_wins_over_gradle_files_at_the_same_level(self):
        repo = Repo({
            "pom.xml": simple_pom(dep(*SLF4J_API, "2.0.13")),
            "build.gradle.kts": gradle('implementation("org.slf4j:slf4j-api:1.7.36")'),
        })
        assert dependency_lines("App.java", [LOGGER], repo) == ["org.slf4j:slf4j-api@2.0.13"]
        assert repo.reads == ["pom.xml"]

    def test_the_kotlin_build_script_wins_over_the_groovy_one(self):
        repo = Repo({
            "build.gradle.kts": gradle('implementation("org.slf4j:slf4j-api:2.0.13")'),
            "build.gradle": gradle("implementation 'org.slf4j:slf4j-api:1.7.36'"),
        })
        assert dependency_lines("App.java", [LOGGER], repo) == ["org.slf4j:slf4j-api@2.0.13"]
        assert repo.reads == ["pom.xml", "build.gradle.kts"]

    def test_an_empty_manifest_is_a_miss_and_the_walk_continues(self):
        repo = Repo({"a/pom.xml": "", "pom.xml": simple_pom(dep(*SLF4J_API, "2.0.13"))})
        assert dependency_lines("a/C.java", [LOGGER], repo) == ["org.slf4j:slf4j-api@2.0.13"]
        assert repo.reads == probes("a") + ["pom.xml"]

    def test_a_read_that_raises_is_a_miss_and_the_walk_continues(self):
        repo = Repo({"pom.xml": simple_pom(dep(*SLF4J_API, "2.0.13"))}, raising=frozenset({"a/pom.xml"}))
        assert dependency_lines("a/C.java", [LOGGER], repo) == ["org.slf4j:slf4j-api@2.0.13"]
        assert repo.reads == probes("a") + ["pom.xml"]

    def test_a_read_that_is_not_text_is_a_miss(self):
        repo = Repo({"a/pom.xml": b"<project/>", "pom.xml": simple_pom(dep(*SLF4J_API, "2.0.13"))})
        assert dependency_lines("a/C.java", [LOGGER], repo) == ["org.slf4j:slf4j-api@2.0.13"]

    def test_a_reader_that_always_raises_gives_nothing(self):
        repo = Repo(raising=frozenset(probes("a", "")))
        assert dependency_lines("a/C.java", [LOGGER], repo) == []
        assert repo.reads == probes("a", "")

    def test_no_path_is_read_twice_when_a_parent_points_back_at_a_probed_miss(self):
        child = pom(parent("com.acme", "acme-parent", "1.0.0", "b/pom.xml"), "<artifactId>a</artifactId>",
                    deps(dep(*DATABIND)))
        repo = Repo({"a/pom.xml": child})
        assert dependency_lines("a/b/C.java", [OBJECT_MAPPER], repo) == [
            "com.fasterxml.jackson.core:jackson-databind@(managed by com.acme:acme-parent@1.0.0)",
        ]
        assert repo.reads == ["a/b/pom.xml", "a/b/build.gradle.kts", "a/b/build.gradle", "a/pom.xml"]


class TestBrokenManifests:
    def test_a_malformed_pom_contributes_nothing_and_stops_the_walk(self):
        repo = Repo({
            "a/pom.xml": "<project><dependencies><dependency><groupId>org.slf4j",
            "pom.xml": simple_pom(dep(*SLF4J_API, "2.0.13")),
        })
        assert dependency_lines("a/C.java", [LOGGER], repo) == []
        assert repo.reads == ["a/pom.xml"]

    def test_a_doctype_pom_is_refused_and_stops_the_walk(self):
        doctype = pom(coords("com.acme", "app"), deps(dep(*SLF4J_API, "2.0.13")), prolog="<!DOCTYPE project>\n")
        control = Repo({"a/pom.xml": doctype.replace("<!DOCTYPE project>\n", "")})
        assert dependency_lines("a/C.java", [LOGGER], control) == ["org.slf4j:slf4j-api@2.0.13"]
        repo = Repo({"a/pom.xml": doctype, "pom.xml": simple_pom(dep(*SLF4J_API, "2.0.13"))})
        assert dependency_lines("a/C.java", [LOGGER], repo) == []
        assert repo.reads == ["a/pom.xml"]

    def test_a_broken_catalog_contributes_nothing_and_stops_the_walk(self):
        catalog = (
            '[versions]\njackson = "2.17.1"\n[libraries]\n'
            'jackson-databind = { module = "com.fasterxml.jackson.core:jackson-databind", version.ref = "jackson" }\n'
        )
        files = {
            "a/build.gradle.kts": gradle("implementation(libs.jackson.databind)"),
            "a/gradle/libs.versions.toml": catalog,
            "build.gradle.kts": gradle('implementation("com.fasterxml.jackson.core:jackson-databind:2.16.0")'),
        }
        control = Repo(files)
        assert dependency_lines("a/C.java", [OBJECT_MAPPER], control) == [
            "com.fasterxml.jackson.core:jackson-databind@2.17.1",
        ]
        repo = Repo({**files, "a/gradle/libs.versions.toml": "[libraries\njackson-databind = "})
        assert dependency_lines("a/C.java", [OBJECT_MAPPER], repo) == []
        assert repo.reads == ["a/pom.xml", "a/build.gradle.kts", "a/gradle/libs.versions.toml"]

    def test_an_unparseable_build_file_contributes_nothing_and_stops_the_walk(self):
        repo = Repo({
            "a/build.gradle": "plugins {{{ ((( 'org.slf4j:slf4j-api \x00\x01",
            "build.gradle": gradle("implementation 'org.slf4j:slf4j-api:2.0.13'"),
        })
        assert dependency_lines("a/C.java", [LOGGER], repo) == []
        assert repo.reads == probes("a")


class TestGradle:
    def test_a_version_ref_catalog_library_resolves(self):
        catalog = (
            '[versions]\njackson = "2.17.1"\n\n[libraries]\n'
            'jackson-core = { module = "com.fasterxml.jackson.core:jackson-core", version.ref = "jackson" }\n'
            'jackson-databind = { group = "com.fasterxml.jackson.core", name = "jackson-databind", '
            'version.ref = "jackson" }\n'
        )
        repo = Repo({
            "app/build.gradle.kts": gradle(
                "implementation(libs.jackson.databind)", "implementation(libs.jackson.core)",
            ),
            "gradle/libs.versions.toml": catalog,
        })
        assert dependency_lines("app/Main.java", [OBJECT_MAPPER], repo) == [
            "com.fasterxml.jackson.core:jackson-databind@2.17.1",
        ]
        assert repo.reads == [
            "app/pom.xml", "app/build.gradle.kts",
            "app/gradle/libs.versions.toml", "app/libs.versions.toml", "gradle/libs.versions.toml",
        ]

    def test_a_platform_owns_a_versionless_dependency(self):
        repo = Repo({"build.gradle": gradle(
            "implementation platform('org.springframework.boot:spring-boot-dependencies:3.3.4')",
            "implementation 'com.fasterxml.jackson.core:jackson-databind'",
        )})
        assert dependency_lines("App.java", [OBJECT_MAPPER], repo) == [DATABIND_BY_SPRING]

    def test_the_owner_is_the_platform_sharing_the_most_group_segments(self):
        repo = Repo({"build.gradle.kts": gradle(
            'implementation(platform("org.springframework.boot:spring-boot-dependencies:3.3.4"))',
            'implementation(enforcedPlatform("com.fasterxml.jackson:jackson-bom:2.17.1"))',
            'implementation("com.fasterxml.jackson.core:jackson-databind")',
            'implementation("org.slf4j:slf4j-api")',
            'implementation("io.micrometer:micrometer-core")',
        )})
        added = [OBJECT_MAPPER, LOGGER, "import io.micrometer.core.instrument.MeterRegistry;"]
        assert dependency_lines("App.java", added, repo) == [
            "com.fasterxml.jackson.core:jackson-databind@(managed by com.fasterxml.jackson:jackson-bom@2.17.1)",
            "io.micrometer:micrometer-core@(managed by org.springframework.boot:spring-boot-dependencies@3.3.4)",
            "org.slf4j:slf4j-api@(managed by org.springframework.boot:spring-boot-dependencies@3.3.4)",
        ]

    def test_a_platform_without_a_version_renders_without_one(self):
        repo = Repo({"build.gradle.kts": gradle(
            'implementation(platform("org.springframework.boot:spring-boot-dependencies"))',
            'implementation("com.fasterxml.jackson.core:jackson-databind")',
        )})
        assert dependency_lines("App.java", [OBJECT_MAPPER], repo) == [
            "com.fasterxml.jackson.core:jackson-databind"
            "@(managed by org.springframework.boot:spring-boot-dependencies)",
        ]

    def test_a_catalog_platform_owns_a_versionless_dependency(self):
        catalog = (
            '[versions]\nboot = "3.3.4"\n[libraries]\n'
            'spring-boot-bom = { module = "org.springframework.boot:spring-boot-dependencies", version.ref = "boot" }\n'
        )
        repo = Repo({
            "build.gradle.kts": gradle(
                "implementation(platform(libs.spring.boot.bom))",
                'implementation("com.fasterxml.jackson.core:jackson-databind")',
            ),
            "gradle/libs.versions.toml": catalog,
        })
        assert dependency_lines("App.java", [OBJECT_MAPPER], repo) == [DATABIND_BY_SPRING]

    def test_a_versionless_dependency_without_a_platform_gets_no_line(self):
        repo = Repo({"build.gradle.kts": gradle('implementation("com.fasterxml.jackson.core:jackson-databind")')})
        assert dependency_lines("App.java", [OBJECT_MAPPER], repo) == []


class TestMaven:
    def test_an_imported_bom_names_the_owner(self):
        repo = Repo({"pom.xml": pom(
            coords("com.acme", "app"),
            props(**{"spring-boot.version": "3.3.4"}),
            managed(dep(*SPRING_BOM, "${spring-boot.version}", bom=True)),
            deps(dep(*DATABIND)),
        )})
        assert dependency_lines("App.java", [OBJECT_MAPPER], repo) == [DATABIND_BY_SPRING]

    def test_a_property_in_an_in_repository_parent_resolves_the_version(self):
        parent_pom = pom(
            coords("com.acme", "acme-parent"),
            props(**{"jackson.version": "2.17.1"}),
            managed(dep(*DATABIND, "${jackson.version}")),
        )
        child = pom(parent("com.acme", "acme-parent", "1.0.0"), "<artifactId>api</artifactId>", deps(dep(*DATABIND)))
        repo = Repo({"pom.xml": parent_pom, "api/pom.xml": child})
        assert dependency_lines("api/src/Controller.java", [OBJECT_MAPPER], repo) == [
            "com.fasterxml.jackson.core:jackson-databind@2.17.1",
        ]
        assert repo.reads == probes("api/src") + ["api/pom.xml", "pom.xml"]

    def test_a_winning_dependency_with_no_version_and_no_owner_gets_no_line(self):
        repo = Repo({"pom.xml": simple_pom(dep(*DATABIND), dep(*JACKSON_CORE, "2.17.1"))})
        assert dependency_lines("App.java", [OBJECT_MAPPER], repo) == []


class TestLanguages:
    @pytest.mark.parametrize(("path", "added"), [
        ("src/App.java", [LOGGER]),
        ("src/App.kt", ["import org.slf4j.Logger"]),
        ("src/tool.main.kts", ["import org.slf4j.Logger"]),
        ("src/App.KT", ["import org.slf4j.Logger"]),
    ])
    def test_java_kotlin_and_kotlin_script_files_behave_alike(self, path, added):
        repo = Repo({"pom.xml": simple_pom(dep(*SLF4J_API, "2.0.13"))})
        assert dependency_lines(path, added, repo) == ["org.slf4j:slf4j-api@2.0.13"]
        assert repo.reads == probes("src") + ["pom.xml"]


class TestNeverRaises:
    def test_a_non_text_added_line_gives_nothing(self):
        repo = Repo({"pom.xml": simple_pom(dep(*SLF4J_API, "2.0.13"))})
        assert dependency_lines("App.java", [LOGGER, None], repo) == []

    def test_a_reader_that_is_not_callable_gives_nothing(self):
        assert dependency_lines("App.java", [LOGGER], None) == []

    def test_a_failing_parser_gives_nothing(self, monkeypatch):
        def explode(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(jvm_deps.jvm_maven, "resolve_pom", explode)
        repo = Repo({"pom.xml": simple_pom(dep(*SLF4J_API, "2.0.13"))})
        assert dependency_lines("App.java", [LOGGER], repo) == []
