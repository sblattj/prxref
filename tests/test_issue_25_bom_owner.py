"""Issue #25: a BOM-managed dependency names only an owner prxref can know.

Offline, prxref cannot tell which of several imported BOMs (or Gradle
platforms, or a Maven parent outside the repository) manages an artifact. One
owner rule, :func:`prxref.jvm_maven.managed_dependency`, serves Maven and
Gradle alike: one candidate is named as before, a candidate sharing a clear
majority of the dependency's leading group segments is named as a likely
owner, and otherwise no owner is named and the candidates are listed. A match
where no artifactId token names the import is marked as a group-level match.
"""
from __future__ import annotations

import pytest

from prxref.jvm_deps import GROUP_MATCH_SUFFIX, dependency_lines
from prxref.jvm_maven import (
    MAX_LISTED_OWNERS,
    MIN_OWNER_SHARED_SEGMENTS,
    MavenCoordinate,
    MavenDependency,
    managed_dependency,
    resolve_pom,
)

JACKSON_BOM = ("com.fasterxml.jackson", "jackson-bom", "2.21.5")
SPRING_BOM = ("org.springframework.boot", "spring-boot-dependencies", "3.5.0")
STARTER_PARENT = ("org.springframework.boot", "spring-boot-starter-parent", "3.5.0")
DATABIND = ("com.fasterxml.jackson.core", "jackson-databind")
DYNATRACE = ("io.micrometer", "micrometer-registry-dynatrace")
MICROMETER_CORE = ("io.micrometer", "micrometer-core")

METER_REGISTRY = "import io.micrometer.core.instrument.MeterRegistry;"
OBJECT_MAPPER = "import com.fasterxml.jackson.databind.ObjectMapper;"
LOGGER = "import org.slf4j.Logger;"

SINGLE_BOM_0_18_0 = (
    "com.fasterxml.jackson.core:jackson-databind@(managed by org.springframework.boot:spring-boot-dependencies@3.5.0)"
)


class Repo:
    """A fake repository reader over a path-to-text mapping; any other path reads as ``None``."""

    def __init__(self, files: dict[str, str]):
        self.files = files

    def __call__(self, path: str) -> str | None:
        return self.files.get(path)


def pom(*parts: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
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


def dep(group: str, artifact: str, version: str | None = None) -> str:
    stated = "" if version is None else f"<version>{version}</version>"
    return f"<dependency><groupId>{group}</groupId><artifactId>{artifact}</artifactId>{stated}</dependency>"


def bom(group: str, artifact: str, version: str) -> str:
    return (
        f"<dependency><groupId>{group}</groupId><artifactId>{artifact}</artifactId><version>{version}</version>"
        "<type>pom</type><scope>import</scope></dependency>"
    )


def deps(*items: str) -> str:
    return "<dependencies>" + "".join(items) + "</dependencies>"


def managed(*items: str) -> str:
    return "<dependencyManagement>" + deps(*items) + "</dependencyManagement>"


def maven(*boms: tuple[str, str, str], dependencies: tuple[tuple[str, str], ...], head: str = "") -> Repo:
    return Repo({"pom.xml": pom(
        head + coords("com.acme", "app"), managed(*(bom(*b) for b in boms)), deps(*(dep(*d) for d in dependencies)),
    )})


def gradle(*lines: str) -> Repo:
    return Repo({"build.gradle.kts": "dependencies {\n" + "".join(f"    {line}\n" for line in lines) + "}\n"})


def platform(group: str, artifact: str, version: str) -> str:
    return f'implementation(platform("{group}:{artifact}:{version}"))'


class TestTheIssueRepro:
    """Two imported BOMs in the root pom, jackson first; the micrometer module; a MeterRegistry import."""

    ROOT = pom(coords("com.acme", "root"), managed(bom(*JACKSON_BOM), bom(*SPRING_BOM)))
    MODULE = pom(parent("com.acme", "root", "1.0.0"), "<artifactId>metrics</artifactId>", deps(dep(*DYNATRACE)))
    FILES = {"pom.xml": ROOT, "metrics/pom.xml": MODULE}
    PATH = "metrics/src/main/java/com/acme/metrics/Metrics.java"

    def test_the_first_imported_bom_is_not_named_and_the_match_is_marked_group_only(self):
        lines = dependency_lines(self.PATH, [METER_REGISTRY], Repo(self.FILES))
        assert lines == [
            "io.micrometer:micrometer-registry-dynatrace"
            "@(managed by one of 2 imported BOMs: jackson-bom, spring-boot-dependencies) (group match only)",
        ]
        assert not [line for line in lines if "jackson-bom@" in line]

    def test_the_resolved_pom_carries_both_candidates_in_declared_order(self):
        project = resolve_pom("metrics/pom.xml", Repo(self.FILES))
        (dynatrace,) = project.dependencies
        assert dynatrace.managed_by is None
        assert dynatrace.owners == (MavenCoordinate(*JACKSON_BOM), MavenCoordinate(*SPRING_BOM))
        assert dynatrace.owner_kind == "bom"


class TestMavenOwnerRule:
    def test_one_bom_is_named_unchanged_from_0_18_0(self):
        repo = maven(SPRING_BOM, dependencies=(DATABIND,))
        assert dependency_lines("App.java", [OBJECT_MAPPER], repo) == [SINGLE_BOM_0_18_0]
        project = resolve_pom("pom.xml", repo)
        assert project.dependencies == (MavenDependency(*DATABIND, None, MavenCoordinate(*SPRING_BOM)),)

    @pytest.mark.parametrize("boms", [(JACKSON_BOM, SPRING_BOM), (SPRING_BOM, JACKSON_BOM)])
    def test_a_bom_sharing_the_group_is_the_likely_owner_in_either_order(self, boms):
        repo = maven(*boms, dependencies=(DATABIND,))
        assert dependency_lines("App.java", [OBJECT_MAPPER], repo) == [
            "com.fasterxml.jackson.core:jackson-databind@(likely managed by com.fasterxml.jackson:jackson-bom@2.21.5)",
        ]

    def test_a_tie_at_two_shared_segments_names_no_owner(self):
        cloud = ("org.springframework.cloud", "spring-cloud-dependencies", "2025.0.0")
        security = ("org.springframework.security", "spring-security-bom", "6.5.0")
        repo = maven(cloud, security, dependencies=(("org.springframework.boot", "spring-boot-starter-web"),))
        assert dependency_lines("App.java", ["import org.springframework.boot.web.Server;"], repo) == [
            "org.springframework.boot:spring-boot-starter-web"
            "@(managed by one of 2 imported BOMs: spring-cloud-dependencies, spring-security-bom)",
        ]

    def test_one_shared_segment_is_not_enough_for_a_guess(self):
        corporate = ("com.example", "corporate-bom", "7")
        repo = maven(corporate, SPRING_BOM, dependencies=(DATABIND,))
        assert dependency_lines("App.java", [OBJECT_MAPPER], repo) == [
            "com.fasterxml.jackson.core:jackson-databind"
            "@(managed by one of 2 imported BOMs: corporate-bom, spring-boot-dependencies)",
        ]

    def test_more_than_three_boms_list_three_then_the_rest_as_a_count(self):
        extra = [("org.acme", f"acme-bom-{n}", "1.0") for n in range(1, 4)]
        repo = maven(JACKSON_BOM, SPRING_BOM, *extra, dependencies=(MICROMETER_CORE,))
        assert dependency_lines("App.java", [METER_REGISTRY], repo) == [
            "io.micrometer:micrometer-core@(managed by one of 5 imported BOMs: "
            "jackson-bom, spring-boot-dependencies, acme-bom-1, +2 more)",
        ]


class TestExternalParentAndBoms:
    HEAD = parent(*STARTER_PARENT, "")

    def test_the_parent_and_one_bom_without_a_clear_best_name_neither(self):
        repo = maven(JACKSON_BOM, dependencies=(MICROMETER_CORE,), head=self.HEAD)
        assert dependency_lines("App.java", [METER_REGISTRY], repo) == [
            "io.micrometer:micrometer-core"
            "@(managed by the parent or an imported BOM: spring-boot-starter-parent, jackson-bom)",
        ]
        (core,) = resolve_pom("pom.xml", repo).dependencies
        assert core.owner_kind == "parent"

    def test_the_parent_and_two_boms_count_the_boms(self):
        repo = maven(JACKSON_BOM, ("org.acme", "acme-bom", "1.0"), dependencies=(MICROMETER_CORE,), head=self.HEAD)
        assert dependency_lines("App.java", [METER_REGISTRY], repo) == [
            "io.micrometer:micrometer-core@(managed by the parent or one of 2 imported BOMs: "
            "spring-boot-starter-parent, jackson-bom, acme-bom)",
        ]

    def test_the_parent_can_be_the_likely_owner(self):
        starter = ("org.springframework.boot", "spring-boot-starter-web")
        repo = maven(JACKSON_BOM, dependencies=(starter, DATABIND), head=self.HEAD)
        added = ["import org.springframework.boot.web.server.WebServer;", OBJECT_MAPPER]
        assert dependency_lines("App.java", added, repo) == [
            "com.fasterxml.jackson.core:jackson-databind@(likely managed by com.fasterxml.jackson:jackson-bom@2.21.5)",
            "org.springframework.boot:spring-boot-starter-web"
            "@(likely managed by org.springframework.boot:spring-boot-starter-parent@3.5.0)",
        ]

    def test_the_parent_alone_is_named_unchanged(self):
        repo = maven(dependencies=(DATABIND,), head=self.HEAD)
        assert dependency_lines("App.java", [OBJECT_MAPPER], repo) == [
            "com.fasterxml.jackson.core:jackson-databind"
            "@(managed by org.springframework.boot:spring-boot-starter-parent@3.5.0)",
        ]


class TestGradleOwnerRule:
    def test_one_platform_is_named_unchanged(self):
        repo = gradle(platform(*SPRING_BOM), 'implementation("com.fasterxml.jackson.core:jackson-databind")')
        assert dependency_lines("App.java", [OBJECT_MAPPER], repo) == [SINGLE_BOM_0_18_0]

    def test_a_platform_sharing_the_group_is_the_likely_owner(self):
        repo = gradle(platform(*SPRING_BOM), platform(*JACKSON_BOM),
                      'implementation("com.fasterxml.jackson.core:jackson-databind")')
        assert dependency_lines("App.java", [OBJECT_MAPPER], repo) == [
            "com.fasterxml.jackson.core:jackson-databind@(likely managed by com.fasterxml.jackson:jackson-bom@2.21.5)",
        ]

    def test_no_clear_best_names_no_platform(self):
        repo = gradle(platform(*JACKSON_BOM), platform(*SPRING_BOM),
                      'implementation("io.micrometer:micrometer-registry-dynatrace")')
        assert dependency_lines("App.kt", ["import io.micrometer.core.instrument.MeterRegistry"], repo) == [
            "io.micrometer:micrometer-registry-dynatrace"
            "@(managed by one of 2 platforms: jackson-bom, spring-boot-dependencies) (group match only)",
        ]


class TestGroupMatchOnly:
    def test_a_zero_score_single_candidate_carries_the_mark(self):
        repo = Repo({"pom.xml": pom(coords("com.acme", "app"), deps(dep("org.slf4j", "slf4j-api", "2.0.13")))})
        assert dependency_lines("App.java", [LOGGER], repo) == ["org.slf4j:slf4j-api@2.0.13" + GROUP_MATCH_SUFFIX]

    def test_a_positive_score_match_carries_no_mark(self):
        repo = Repo({"pom.xml": pom(coords("com.acme", "app"), deps(dep(*DATABIND, "2.17.1")))})
        assert dependency_lines("App.java", [OBJECT_MAPPER], repo) == [
            "com.fasterxml.jackson.core:jackson-databind@2.17.1",
        ]

    def test_a_true_group_level_dependency_is_kept_not_dropped(self):
        webmvc = dep("org.springframework", "spring-webmvc", "6.2.0")
        repo = Repo({"pom.xml": pom(coords("com.acme", "app"), deps(webmvc))})
        added = ["import org.springframework.web.servlet.DispatcherServlet;"]
        assert dependency_lines("App.java", added, repo) == [
            "org.springframework:spring-webmvc@6.2.0 (group match only)",
        ]

    def test_several_zero_score_candidates_are_each_marked(self):
        repo = Repo({"pom.xml": pom(coords("com.acme", "app"), deps(
            dep("org.slf4j", "slf4j-api", "2.0.13"), dep("org.slf4j", "jul-to-slf4j", "2.0.13"),
        ))})
        assert dependency_lines("App.java", [LOGGER], repo) == [
            "org.slf4j:jul-to-slf4j@2.0.13 (group match only)",
            "org.slf4j:slf4j-api@2.0.13 (group match only)",
        ]

    def test_an_artifact_match_from_another_import_wins_over_the_group_only_one(self):
        repo = Repo({"pom.xml": pom(coords("com.acme", "app"), deps(dep(*MICROMETER_CORE, "1.15.0")))})
        group_only = ["import io.micrometer.observation.Observation;"]
        assert dependency_lines("App.java", group_only, repo) == [
            "io.micrometer:micrometer-core@1.15.0 (group match only)",
        ]
        both = [*group_only, METER_REGISTRY]
        assert dependency_lines("App.java", both, repo) == ["io.micrometer:micrometer-core@1.15.0"]
        assert dependency_lines("App.java", list(reversed(both)), repo) == ["io.micrometer:micrometer-core@1.15.0"]


class TestManagedDependency:
    def test_the_thresholds(self):
        assert (MIN_OWNER_SHARED_SEGMENTS, MAX_LISTED_OWNERS) == (2, 3)
        assert GROUP_MATCH_SUFFIX == " (group match only)"

    def test_no_candidate_renders_no_line(self):
        assert managed_dependency(*DATABIND, ()).line() is None

    def test_exactly_one_candidate_is_the_0_18_0_object(self):
        owner = MavenCoordinate(*SPRING_BOM)
        assert managed_dependency(*DATABIND, [owner], "platform") == MavenDependency(*DATABIND, None, owner)

    def test_a_likely_owner_is_marked(self):
        dependency = managed_dependency(*DATABIND, [MavenCoordinate(*SPRING_BOM), MavenCoordinate(*JACKSON_BOM)])
        assert (dependency.managed_by, dependency.likely, dependency.owners) == (
            MavenCoordinate(*JACKSON_BOM), True, (),
        )

    def test_exactly_three_candidates_have_no_more_count(self):
        owners = [MavenCoordinate("org.acme", f"bom-{n}", "1") for n in range(3)]
        assert managed_dependency("io.example", "lib", owners, "platform").line() == (
            "io.example:lib@(managed by one of 3 platforms: bom-0, bom-1, bom-2)"
        )

    def test_a_stated_version_wins_and_carries_no_owner(self):
        line = MavenDependency(*DATABIND, "2.17.1", likely=True).line()
        assert line == "com.fasterxml.jackson.core:jackson-databind@2.17.1"
