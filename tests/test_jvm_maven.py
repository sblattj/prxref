"""Unit tests for :mod:`prxref.jvm_maven`, the pom.xml reader behind #20.

Every test supplies its own ``read`` callable, usually a counting one, so the
refusals, the parent walk and its caps are pinned by exactly which paths were
read, without a forge or a filesystem.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from prxref import chunk_context, jvm_maven
from prxref.jvm_maven import (
    MAX_INTERPOLATED_CHARS,
    MAX_POM_BYTES,
    MAX_POM_PARENTS,
    MAX_PROPERTY_PASSES,
    MavenCoordinate,
    MavenDependency,
    resolve_pom,
)

POM_NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'
SPRING_BOM = ("org.springframework.boot", "spring-boot-dependencies")
DATABIND = ("com.fasterxml.jackson.core", "jackson-databind")


def pom(body: str, *, xmlns: bool = True, prolog: str = "") -> str:
    head = f"<project {POM_NS}>" if xmlns else "<project>"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"{prolog}{head}\n  <modelVersion>4.0.0</modelVersion>\n{body}\n</project>\n"
    )


def dep(group: str, artifact: str, version: str | None = None, *, type_: str = "", scope: str = "") -> str:
    parts = [f"<groupId>{group}</groupId>", f"<artifactId>{artifact}</artifactId>"]
    if version is not None:
        parts.append(f"<version>{version}</version>")
    if type_:
        parts.append(f"<type>{type_}</type>")
    if scope:
        parts.append(f"<scope>{scope}</scope>")
    return "<dependency>" + "".join(parts) + "</dependency>"


def deps(*items: str) -> str:
    return "<dependencies>" + "".join(items) + "</dependencies>"


def managed(*items: str) -> str:
    return "<dependencyManagement>" + deps(*items) + "</dependencyManagement>"


def props(**values: str) -> str:
    return "<properties>" + "".join(f"<{k}>{v}</{k}>" for k, v in values.items()) + "</properties>"


def parent(group: str, artifact: str, version: str, relative: str | None = None) -> str:
    parts = [f"<groupId>{group}</groupId>", f"<artifactId>{artifact}</artifactId>", f"<version>{version}</version>"]
    if relative == "":
        parts.append("<relativePath/>")
    elif relative is not None:
        parts.append(f"<relativePath>{relative}</relativePath>")
    return "<parent>" + "".join(parts) + "</parent>"


def coords(group: str, artifact: str, version: str | None = None) -> str:
    out = f"<groupId>{group}</groupId><artifactId>{artifact}</artifactId>"
    return out + (f"<version>{version}</version>" if version else "")


class CountingReader:
    def __init__(self, files: dict[str, str]):
        self.files = dict(files)
        self.calls: list[str] = []

    def __call__(self, path: str) -> str | None:
        self.calls.append(path)
        return self.files.get(path)


def by_name(project) -> dict[str, MavenDependency]:
    return {d.name: d for d in project.dependencies}


@pytest.fixture
def parse_calls(monkeypatch):
    calls: list[str] = []
    real = jvm_maven._parse

    def spy(text: str):
        calls.append(text)
        return real(text)

    monkeypatch.setattr(jvm_maven, "_parse", spy)
    return calls


PLAIN = pom(coords("com.acme", "app", "1.0.0") + deps(dep(*DATABIND, "2.17.1")))


def billion_laughs() -> str:
    entities = ['  <!ENTITY lol0 "lol">']
    for level in range(1, 10):
        entities.append(f'  <!ENTITY lol{level} "' + f"&lol{level - 1};" * 10 + '">')
    return (
        '<?xml version="1.0"?>\n<!DOCTYPE project [\n' + "\n".join(entities) + "\n]>\n"
        "<project><groupId>com.acme</groupId><artifactId>&lol9;</artifactId></project>\n"
    )


class TestRefusal:
    def test_the_size_cap_is_the_chunk_context_file_cap(self):
        assert MAX_POM_BYTES == chunk_context.MAX_FILE_BYTES

    def test_the_spy_sees_a_clean_pom_parse_exactly_once(self, parse_calls):
        project = resolve_pom("pom.xml", CountingReader({"pom.xml": PLAIN}))
        assert project is not None
        assert project.lines() == ["com.fasterxml.jackson.core:jackson-databind@2.17.1"]
        assert len(parse_calls) == 1

    def test_a_doctype_is_refused_before_parsing(self, parse_calls):
        text = pom(
            coords("com.acme", "app", "1.0.0") + deps(dep(*DATABIND, "2.17.1")),
            prolog='<!DOCTYPE project SYSTEM "http://example.com/project.dtd">\n',
        )
        assert "ENTITY" not in text.upper()
        assert resolve_pom("pom.xml", CountingReader({"pom.xml": text})) is None
        assert parse_calls == []

    def test_a_lowercase_doctype_is_refused_before_parsing(self, parse_calls):
        text = pom(coords("com.acme", "app", "1.0.0"), prolog="<!doctype project>\n")
        assert resolve_pom("pom.xml", CountingReader({"pom.xml": text})) is None
        assert parse_calls == []

    def test_an_entity_declaration_anywhere_is_refused_before_parsing(self, parse_calls):
        text = pom(coords("com.acme", "app", "1.0.0") + '<!-- <!Entity x "y"> -->' + deps(dep(*DATABIND, "2.17.1")))
        assert "DOCTYPE" not in text.upper()
        assert resolve_pom("pom.xml", CountingReader({"pom.xml": text})) is None
        assert parse_calls == []

    def test_a_billion_laughs_payload_returns_nothing_and_is_never_parsed(self, parse_calls):
        payload = billion_laughs()
        assert payload.count("<!ENTITY") == 10
        assert resolve_pom("pom.xml", CountingReader({"pom.xml": payload})) is None
        assert resolve_pom("pom.xml", CountingReader({}), text=payload) is None
        assert parse_calls == []

    def test_a_pom_at_the_byte_cap_parses_and_one_byte_over_is_refused(self, parse_calls):
        def padded(size: int) -> str:
            filler = size - len(PLAIN.encode("utf-8")) - len("<!--  -->")
            return PLAIN + "<!-- " + "x" * filler + " -->"

        at_cap, over_cap = padded(MAX_POM_BYTES), padded(MAX_POM_BYTES + 1)
        assert len(at_cap.encode("utf-8")) == MAX_POM_BYTES
        assert resolve_pom("pom.xml", CountingReader({"pom.xml": at_cap})) is not None
        assert len(parse_calls) == 1
        assert resolve_pom("pom.xml", CountingReader({"pom.xml": over_cap})) is None
        assert len(parse_calls) == 1

    def test_the_cap_counts_utf8_bytes_not_characters(self, parse_calls):
        wide = "\N{LATIN SMALL LETTER E WITH ACUTE}"
        filler = (MAX_POM_BYTES - len(PLAIN)) // 2 + 1
        text = PLAIN + "<!-- " + wide * filler + " -->"
        assert len(text) <= MAX_POM_BYTES < len(text.encode("utf-8"))
        assert resolve_pom("pom.xml", CountingReader({"pom.xml": text})) is None
        assert parse_calls == []

    def test_a_refused_parent_is_external_and_the_child_still_resolves(self, parse_calls):
        bad_parent = pom(coords("com.acme", "parent", "1.0.0"), prolog="<!DOCTYPE project>\n")
        child = pom(parent("com.acme", "parent", "1.0.0") + coords("com.acme", "api") + deps(dep(*DATABIND)))
        reader = CountingReader({"api/pom.xml": child, "pom.xml": bad_parent})
        project = resolve_pom("api/pom.xml", reader)
        assert project is not None
        assert reader.calls == ["api/pom.xml", "pom.xml"]
        assert project.pom_paths == ("api/pom.xml",)
        assert project.external_parent == MavenCoordinate("com.acme", "parent", "1.0.0")
        assert len(parse_calls) == 1


class TestMalformed:
    @pytest.mark.parametrize("text", [
        pom(deps("<dependency><groupId>com.acme</groupId>")),
        "<project><groupId>com.acme</groupId>",
        "not xml at all",
        "",
        "   \n",
    ])
    def test_a_malformed_pom_contributes_nothing(self, text):
        assert resolve_pom("pom.xml", CountingReader({"pom.xml": text})) is None

    def test_a_root_other_than_project_contributes_nothing(self):
        text = '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"><localRepository/></settings>'
        assert resolve_pom("pom.xml", CountingReader({"pom.xml": text})) is None

    def test_a_missing_pom_contributes_nothing(self):
        reader = CountingReader({})
        assert resolve_pom("pom.xml", reader) is None
        assert reader.calls == ["pom.xml"]

    def test_a_raising_or_non_text_reader_never_raises(self):
        def boom(path: str) -> str | None:
            raise OSError("forge down")

        assert resolve_pom("pom.xml", boom) is None
        assert resolve_pom("pom.xml", lambda path: b"<project/>") is None

    def test_a_malformed_parent_is_external_and_the_child_still_resolves(self):
        child = pom(parent("com.acme", "parent", "1.0.0") + coords("com.acme", "api") + deps(dep(*DATABIND)))
        reader = CountingReader({"api/pom.xml": child, "pom.xml": "<project><dependencies>"})
        project = resolve_pom("api/pom.xml", reader)
        assert project is not None
        assert project.pom_paths == ("api/pom.xml",)
        assert project.external_parent == MavenCoordinate("com.acme", "parent", "1.0.0")
        assert project.lines() == [
            "com.fasterxml.jackson.core:jackson-databind@(managed by com.acme:parent@1.0.0)",
        ]


class TestProperties:
    def test_nested_properties_resolve(self):
        text = pom(
            coords("com.acme", "app", "1.0.0")
            + props(**{
                "jackson.major": "2",
                "jackson.minor": "${jackson.major}.17",
                "jackson.version": "${jackson.minor}.1",
            })
            + deps(dep(*DATABIND, "${jackson.version}"))
        )
        project = resolve_pom("pom.xml", CountingReader({"pom.xml": text}))
        assert project.lines() == ["com.fasterxml.jackson.core:jackson-databind@2.17.1"]

    def test_a_child_property_overrides_its_parent(self):
        parent_pom = pom(
            coords("com.acme", "parent", "1.0.0")
            + props(**{"jackson.version": "2.15.0", "slf4j.version": "2.0.13"})
            + managed(dep(*DATABIND, "${jackson.version}"), dep("org.slf4j", "slf4j-api", "${slf4j.version}"))
        )
        child = pom(
            parent("com.acme", "parent", "1.0.0")
            + coords("com.acme", "api")
            + props(**{"jackson.version": "2.17.1"})
            + deps(dep(*DATABIND), dep("org.slf4j", "slf4j-api"))
        )
        project = resolve_pom("api/pom.xml", CountingReader({"api/pom.xml": child, "pom.xml": parent_pom}))
        assert project.lines() == [
            "com.fasterxml.jackson.core:jackson-databind@2.17.1",
            "org.slf4j:slf4j-api@2.0.13",
        ]

    def test_project_version_group_and_parent_version_resolve(self):
        parent_pom = pom(coords("com.acme", "parent", "3.1.0"))
        child = pom(
            parent("com.acme", "parent", "3.1.0")
            + "<artifactId>api</artifactId>"
            + deps(
                dep("${project.groupId}", "core", "${project.version}"),
                dep("com.example", "client", "${project.parent.version}"),
            )
        )
        project = resolve_pom("api/pom.xml", CountingReader({"api/pom.xml": child, "pom.xml": parent_pom}))
        assert (project.group_id, project.artifact_id, project.version) == ("com.acme", "api", "3.1.0")
        assert project.lines() == ["com.acme:core@3.1.0", "com.example:client@3.1.0"]

    def test_an_unresolved_placeholder_stays_literal(self):
        text = pom(coords("com.acme", "app", "1.0.0") + deps(dep("com.example", "lib", "${x}")))
        project = resolve_pom("pom.xml", CountingReader({"pom.xml": text}))
        assert project.lines() == ["com.example:lib@${x}"]

    def test_resolution_stops_after_the_pass_limit(self):
        assert MAX_PROPERTY_PASSES == 5

        def chain(depth: int) -> dict[str, str]:
            values = {f"p{i}": f"${{p{i + 1}}}" for i in range(1, depth)}
            values[f"p{depth}"] = "9.9.9"
            return values

        fits = pom(coords("com.acme", "app", "1") + props(**chain(5)) + deps(dep("com.example", "a", "${p1}")))
        too_deep = pom(coords("com.acme", "app", "1") + props(**chain(6)) + deps(dep("com.example", "a", "${p1}")))
        assert resolve_pom("pom.xml", CountingReader({"pom.xml": fits})).lines() == ["com.example:a@9.9.9"]
        assert resolve_pom("pom.xml", CountingReader({"pom.xml": too_deep})).lines() == ["com.example:a@${p6}"]

    def test_a_property_cycle_terminates_literal(self):
        text = pom(coords("com.acme", "app", "1") + props(a="${b}", b="${a}") + deps(dep("com.example", "lib", "${a}")))
        project = resolve_pom("pom.xml", CountingReader({"pom.xml": text}))
        assert project.lines() in (["com.example:lib@${a}"], ["com.example:lib@${b}"])

    def test_a_self_multiplying_property_stops_below_the_char_cap(self):
        text = pom(
            coords("com.acme", "app", "1")
            + props(a="${a}" * 8, big="x" * 4000)
            + deps(dep("com.example", "grow", "${a}"), dep("com.example", "huge", "${big}" * 50))
        )
        project = resolve_pom("pom.xml", CountingReader({"pom.xml": text}))
        found = by_name(project)
        assert found["com.example:grow"].version == "${a}" * 64
        assert len("${a}" * 512) > MAX_INTERPOLATED_CHARS
        assert found["com.example:huge"].version == "${big}" * 50


class TestParentChain:
    PARENT = pom(
        coords("com.acme", "parent", "1.0.0")
        + props(**{"jackson.version": "2.17.1"})
        + managed(dep(*DATABIND, "${jackson.version}"))
    )

    def child(self, relative: str | None) -> str:
        return pom(parent("com.acme", "parent", "1.0.0", relative) + coords("com.acme", "api") + deps(dep(*DATABIND)))

    def test_a_parent_is_found_via_an_explicit_relative_path(self):
        reader = CountingReader({"services/api/pom.xml": self.child("../../build/parent/pom.xml"),
                                 "build/parent/pom.xml": self.PARENT})
        project = resolve_pom("services/api/pom.xml", reader)
        assert reader.calls == ["services/api/pom.xml", "build/parent/pom.xml"]
        assert project.pom_paths == ("services/api/pom.xml", "build/parent/pom.xml")
        assert project.external_parent is None
        assert project.lines() == ["com.fasterxml.jackson.core:jackson-databind@2.17.1"]

    def test_a_relative_path_naming_a_directory_gets_pom_xml_appended(self):
        reader = CountingReader({"services/api/pom.xml": self.child("../../build/parent"),
                                 "build/parent/pom.xml": self.PARENT})
        project = resolve_pom("services/api/pom.xml", reader)
        assert reader.calls == ["services/api/pom.xml", "build/parent/pom.xml"]
        assert project.lines() == ["com.fasterxml.jackson.core:jackson-databind@2.17.1"]

    def test_a_parent_is_found_via_the_default_relative_path(self):
        reader = CountingReader({"api/pom.xml": self.child(None), "pom.xml": self.PARENT})
        project = resolve_pom("api/pom.xml", reader)
        assert reader.calls == ["api/pom.xml", "pom.xml"]
        assert project.lines() == ["com.fasterxml.jackson.core:jackson-databind@2.17.1"]

    def test_an_empty_relative_path_is_external_and_never_read(self):
        reader = CountingReader({"api/pom.xml": self.child(""), "pom.xml": self.PARENT})
        project = resolve_pom("api/pom.xml", reader)
        assert reader.calls == ["api/pom.xml"]
        assert project.external_parent == MavenCoordinate("com.acme", "parent", "1.0.0")
        assert project.lines() == [
            "com.fasterxml.jackson.core:jackson-databind@(managed by com.acme:parent@1.0.0)",
        ]

    def test_an_artifact_id_mismatch_is_external(self):
        impostor = pom(coords("com.acme", "other-parent", "1.0.0") + props(**{"jackson.version": "9.9.9"})
                       + managed(dep(*DATABIND, "${jackson.version}")))
        reader = CountingReader({"api/pom.xml": self.child(None), "pom.xml": impostor})
        project = resolve_pom("api/pom.xml", reader)
        assert reader.calls == ["api/pom.xml", "pom.xml"]
        assert project.pom_paths == ("api/pom.xml",)
        assert project.external_parent == MavenCoordinate("com.acme", "parent", "1.0.0")
        assert project.lines() == [
            "com.fasterxml.jackson.core:jackson-databind@(managed by com.acme:parent@1.0.0)",
        ]

    def test_a_root_pom_default_parent_is_outside_the_repository_and_never_read(self):
        root = pom(parent("org.springframework.boot", "spring-boot-starter-parent", "3.3.4")
                   + coords("com.acme", "app") + deps(dep(*DATABIND)))
        reader = CountingReader({"pom.xml": root})
        project = resolve_pom("pom.xml", reader)
        assert reader.calls == ["pom.xml"]
        assert project.version == "3.3.4"
        assert project.lines() == [
            "com.fasterxml.jackson.core:jackson-databind"
            "@(managed by org.springframework.boot:spring-boot-starter-parent@3.3.4)",
        ]

    def test_an_absolute_relative_path_is_external_and_never_read(self):
        reader = CountingReader({"api/pom.xml": self.child("/etc/pom.xml"), "etc/pom.xml": self.PARENT})
        project = resolve_pom("api/pom.xml", reader)
        assert reader.calls == ["api/pom.xml"]
        assert project.external_parent == MavenCoordinate("com.acme", "parent", "1.0.0")

    def test_a_cycle_reads_each_pom_once_and_stops(self):
        a = pom(parent("com.acme", "b", "1", "../b/pom.xml") + coords("com.acme", "a", "1")
                + props(**{"from.a": "1.1"}) + deps(dep("com.example", "x", "${from.b}")))
        b = pom(parent("com.acme", "a", "1", "../a/pom.xml") + coords("com.acme", "b", "1")
                + props(**{"from.b": "2.2"}) + deps(dep("com.example", "y", "${from.a}")))
        reader = CountingReader({"a/pom.xml": a, "b/pom.xml": b})
        project = resolve_pom("a/pom.xml", reader)
        assert reader.calls == ["a/pom.xml", "b/pom.xml"]
        assert project.pom_paths == ("a/pom.xml", "b/pom.xml")
        assert project.external_parent is None
        assert project.lines() == ["com.example:x@2.2", "com.example:y@1.1"]

    def test_a_pom_naming_itself_as_parent_is_read_once(self):
        text = pom(parent("com.acme", "app", "1", "pom.xml") + coords("com.acme", "app", "1"))
        reader = CountingReader({"pom.xml": text})
        project = resolve_pom("pom.xml", reader)
        assert reader.calls == ["pom.xml"]
        assert project.external_parent is None

    @staticmethod
    def ladder() -> dict[str, str]:
        files: dict[str, str] = {}
        for level in range(7):
            path = "/".join(["m"] * (6 - level) + ["pom.xml"])
            body = coords("com.acme", f"level{level}", "1.0.0")
            if level < 6:
                body = parent("com.acme", f"level{level + 1}", "1.0.0") + body
            if level == 5:
                body += props(five="5.0.0")
            if level == 6:
                body += props(six="6.0.0")
            if level == 0:
                body += deps(dep("com.example", "five", "${five}"), dep("com.example", "six", "${six}"))
            files[path] = pom(body)
        return files

    def test_the_depth_cap_stops_after_five_parents(self):
        assert MAX_POM_PARENTS == 5
        reader = CountingReader(self.ladder())
        project = resolve_pom("m/m/m/m/m/m/pom.xml", reader)
        assert len(reader.calls) == 6
        assert "pom.xml" not in reader.calls
        assert project.pom_paths == tuple(reader.calls)
        assert project.lines() == ["com.example:five@5.0.0", "com.example:six@${six}"]
        assert project.external_parent == MavenCoordinate("com.acme", "level6", "1.0.0")

    def test_a_sixth_parent_is_read_only_when_the_cap_allows_it(self, monkeypatch):
        monkeypatch.setattr(jvm_maven, "MAX_POM_PARENTS", 6)
        reader = CountingReader(self.ladder())
        project = resolve_pom("m/m/m/m/m/m/pom.xml", reader)
        assert reader.calls[-1] == "pom.xml"
        assert project.lines() == ["com.example:five@5.0.0", "com.example:six@6.0.0"]
        assert project.external_parent is None

    def test_parent_dependencies_are_inherited_and_the_child_wins(self):
        parent_pom = pom(coords("com.acme", "parent", "1.0.0")
                         + deps(dep("org.slf4j", "slf4j-api", "2.0.13"), dep(*DATABIND, "2.15.0")))
        child = pom(parent("com.acme", "parent", "1.0.0") + coords("com.acme", "api") + deps(dep(*DATABIND, "2.17.1")))
        project = resolve_pom("api/pom.xml", CountingReader({"api/pom.xml": child, "pom.xml": parent_pom}))
        assert project.lines() == [
            "com.fasterxml.jackson.core:jackson-databind@2.17.1",
            "org.slf4j:slf4j-api@2.0.13",
        ]


class TestDependencies:
    def test_dependencies_and_dependency_management_are_both_read(self):
        text = pom(
            coords("com.acme", "app", "1.0.0")
            + managed(dep(*DATABIND, "2.17.1"), dep("jakarta.persistence", "jakarta.persistence-api", "3.1.0"))
            + deps(dep(*DATABIND), dep("org.slf4j", "slf4j-api", "2.0.13"))
        )
        project = resolve_pom("pom.xml", CountingReader({"pom.xml": text}))
        assert [d.name for d in project.dependencies] == [
            "com.fasterxml.jackson.core:jackson-databind",
            "org.slf4j:slf4j-api",
            "jakarta.persistence:jakarta.persistence-api",
        ]
        assert project.lines() == [
            "com.fasterxml.jackson.core:jackson-databind@2.17.1",
            "jakarta.persistence:jakarta.persistence-api@3.1.0",
            "org.slf4j:slf4j-api@2.0.13",
        ]

    def test_plugin_and_profile_dependencies_are_not_read(self):
        text = pom(
            coords("com.acme", "app", "1.0.0")
            + deps(dep("org.slf4j", "slf4j-api", "2.0.13"))
            + "<build><plugins><plugin>" + coords("org.apache.maven.plugins", "maven-surefire-plugin", "3.2.5")
            + deps(dep("com.example", "plugin-only", "1.0")) + "</plugin></plugins></build>"
            + "<profiles><profile><id>extra</id>" + deps(dep("com.example", "profile-only", "1.0"))
            + "</profile></profiles>"
        )
        project = resolve_pom("pom.xml", CountingReader({"pom.xml": text}))
        assert project.lines() == ["org.slf4j:slf4j-api@2.0.13"]

    def test_an_imported_bom_owns_versionless_dependencies(self):
        text = pom(
            coords("com.acme", "app", "1.0.0")
            + props(**{"spring-boot.version": "3.3.4"})
            + managed(dep(*SPRING_BOM, "${spring-boot.version}", type_="pom", scope="import"))
            + deps(dep(*DATABIND), dep("com.example", "pinned", "1.2.3"))
        )
        project = resolve_pom("pom.xml", CountingReader({"pom.xml": text}))
        spring_boot = MavenCoordinate(*SPRING_BOM, "3.3.4")
        assert project.boms == (spring_boot,)
        assert by_name(project)["com.fasterxml.jackson.core:jackson-databind"] == MavenDependency(
            *DATABIND, None, spring_boot,
        )
        assert project.lines() == [
            "com.example:pinned@1.2.3",
            "com.fasterxml.jackson.core:jackson-databind"
            "@(managed by org.springframework.boot:spring-boot-dependencies@3.3.4)",
        ]
        assert "org.springframework.boot:spring-boot-dependencies" not in by_name(project)

    def test_a_bom_imported_by_a_parent_owns_the_child_dependency(self):
        parent_pom = pom(coords("com.acme", "parent", "1.0.0")
                         + managed(dep(*SPRING_BOM, "3.3.4", type_="pom", scope="import")))
        child = pom(parent("com.acme", "parent", "1.0.0") + coords("com.acme", "api") + deps(dep(*DATABIND)))
        project = resolve_pom("api/pom.xml", CountingReader({"api/pom.xml": child, "pom.xml": parent_pom}))
        assert project.lines() == [
            "com.fasterxml.jackson.core:jackson-databind"
            "@(managed by org.springframework.boot:spring-boot-dependencies@3.3.4)",
        ]

    def test_import_scope_without_pom_type_is_not_a_bom(self):
        text = pom(coords("com.acme", "app", "1.0.0") + managed(dep(*SPRING_BOM, "3.3.4", scope="import"))
                   + deps(dep(*DATABIND)))
        project = resolve_pom("pom.xml", CountingReader({"pom.xml": text}))
        assert project.boms == ()
        assert by_name(project)["com.fasterxml.jackson.core:jackson-databind"].line() is None

    def test_owner_precedence_is_external_parent_then_boms_nearest_first(self):
        parent_pom = pom(
            parent("com.example", "corporate-parent", "7", "") + coords("com.acme", "parent", "1.0.0")
            + managed(dep("com.fasterxml.jackson", "jackson-bom", "2.17.1", type_="pom", scope="import"))
        )
        child = pom(parent("com.acme", "parent", "1.0.0") + coords("com.acme", "api")
                    + managed(dep(*SPRING_BOM, "3.3.4", type_="pom", scope="import")) + deps(dep(*DATABIND)))
        files = {"api/pom.xml": child, "pom.xml": parent_pom}
        project = resolve_pom("api/pom.xml", CountingReader(files))
        assert project.boms == (MavenCoordinate(*SPRING_BOM, "3.3.4"),
                                MavenCoordinate("com.fasterxml.jackson", "jackson-bom", "2.17.1"))
        assert project.external_parent == MavenCoordinate("com.example", "corporate-parent", "7")
        assert project.lines() == [
            "com.fasterxml.jackson.core:jackson-databind@(managed by com.example:corporate-parent@7)",
        ]
        files["pom.xml"] = parent_pom.replace(parent("com.example", "corporate-parent", "7", ""), "")
        project = resolve_pom("api/pom.xml", CountingReader(files))
        assert project.external_parent is None
        assert project.lines() == [
            "com.fasterxml.jackson.core:jackson-databind"
            "@(managed by org.springframework.boot:spring-boot-dependencies@3.3.4)",
        ]

    def test_a_managed_version_wins_over_every_owner(self):
        text = pom(parent("com.example", "corporate-parent", "7", "") + coords("com.acme", "app")
                   + managed(dep(*SPRING_BOM, "3.3.4", type_="pom", scope="import"), dep(*DATABIND, "2.17.1"))
                   + deps(dep(*DATABIND)))
        project = resolve_pom("pom.xml", CountingReader({"pom.xml": text}))
        assert project.lines() == ["com.fasterxml.jackson.core:jackson-databind@2.17.1"]

    def test_no_version_and_no_owner_renders_no_line(self):
        text = pom(coords("com.acme", "app", "1.0.0") + deps(dep(*DATABIND), dep("org.slf4j", "slf4j-api", "2.0.13")))
        project = resolve_pom("pom.xml", CountingReader({"pom.xml": text}))
        orphan = by_name(project)["com.fasterxml.jackson.core:jackson-databind"]
        assert (orphan.version, orphan.managed_by, orphan.line()) == (None, None, None)
        assert project.lines() == ["org.slf4j:slf4j-api@2.0.13"]

    def test_a_pom_without_a_namespace_reads_the_same(self):
        body = coords("com.acme", "app", "1.0.0") + deps(dep(*DATABIND, "2.17.1"))
        with_ns = resolve_pom("pom.xml", CountingReader({"pom.xml": pom(body)}))
        without_ns = resolve_pom("pom.xml", CountingReader({"pom.xml": pom(body, xmlns=False)}))
        assert with_ns == without_ns
        assert with_ns.lines() == ["com.fasterxml.jackson.core:jackson-databind@2.17.1"]

    def test_the_group_falls_back_to_the_parent_element(self):
        child = pom(parent("com.acme", "parent", "2.0.0", "") + "<artifactId>api</artifactId>")
        project = resolve_pom("api/pom.xml", CountingReader({"api/pom.xml": child}))
        assert (project.group_id, project.artifact_id, project.version) == ("com.acme", "api", "2.0.0")

    def test_supplied_text_is_not_read_again(self):
        reader = CountingReader({})
        project = resolve_pom("pom.xml", reader, text=PLAIN)
        assert reader.calls == []
        assert project.pom_paths == ("pom.xml",)
        assert project.lines() == ["com.fasterxml.jackson.core:jackson-databind@2.17.1"]

    def test_rendered_shapes(self):
        owner = MavenCoordinate(*SPRING_BOM, "3.3.4")
        assert owner.render() == "org.springframework.boot:spring-boot-dependencies@3.3.4"
        assert MavenCoordinate("com.acme", "bom").render() == "com.acme:bom"
        assert MavenDependency("com.acme", "core", "1.0").line() == "com.acme:core@1.0"
        assert MavenDependency("com.acme", "core", None, owner).line() == (
            "com.acme:core@(managed by org.springframework.boot:spring-boot-dependencies@3.3.4)"
        )
        assert MavenDependency("com.acme", "core").line() is None


def test_the_module_imports_only_the_standard_library():
    tree = ast.parse(Path(jvm_maven.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add((node.module or "").split(".")[0])
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}
