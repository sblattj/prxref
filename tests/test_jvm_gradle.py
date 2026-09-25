"""Unit tests for :mod:`prxref.jvm_gradle`, the Gradle half of #20's dependency versions.

The module is pure: every test hands it text, or a ``read`` callable over a
dict, so these pin the build-file scan, the platform owners, the own group, the
three catalog forms and the catalog probe without a forge or a filesystem.
"""
from __future__ import annotations

import ast
from pathlib import Path

import prxref.jvm_gradle as jvm_gradle
from prxref.jvm_gradle import (
    BUILD_FILE_NAMES,
    GradleBuild,
    GradleDependency,
    catalog_paths,
    gradle_build,
    parse_build_file,
    parse_catalog,
)

JACKSON = "com.fasterxml.jackson.core"
BOOT_BOM = GradleDependency("org.springframework.boot", "spring-boot-dependencies", "3.3.4")


def counting_reader(files: dict[str, str]):
    calls: list[str] = []

    def _read(path: str) -> str | None:
        calls.append(path)
        return files.get(path)

    return _read, calls


GROOVY_BUILD = """\
plugins {
    id 'java'
}

dependencies {
    implementation 'com.fasterxml.jackson.core:jackson-databind:2.17.0'
    api "org.slf4j:slf4j-api:2.0.13"
    testImplementation 'org.junit.jupiter:junit-jupiter'
    compileOnly("com.example:annotations:1.0")
}
"""

KOTLIN_BUILD = """\
plugins {
    kotlin("jvm") version "2.0.0"
}

dependencies {
    implementation("com.fasterxml.jackson.core:jackson-databind:2.17.0")
    implementation(
        "com.acme:billing-client:3.1.4"
    )
    testImplementation('org.junit.jupiter:junit-jupiter:5.10.2')
    runtimeOnly("com.example:driver")
}
"""

CATALOG = """\
[versions]
jackson = "2.17.0"
boot = { strictly = "3.3.4" }

[libraries]
jackson-databind = { module = "com.fasterxml.jackson.core:jackson-databind", version.ref = "jackson" }
jackson-core = { group = "com.fasterxml.jackson.core", name = "jackson-core", version = { ref = "jackson" } }
jackson-annotations = { group = "com.fasterxml.jackson.core", name = "jackson-annotations", version.ref = "jackson" }
acme-client = "com.acme:client:1.4.2"
acme-model = { module = "com.acme:model", version = "0.9.1" }
acme-bare = { module = "com.acme:bare" }
slf4j-api = { group = "org.slf4j", name = "slf4j-api", version = "2.0.13" }
spring-boot-bom = { module = "org.springframework.boot:spring-boot-dependencies", version.ref = "boot" }

[bundles]
jackson = ["jackson-databind", "jackson-core"]

[plugins]
boot = { id = "org.springframework.boot", version.ref = "boot" }
"""


class TestBuildFileStrings:
    def test_groovy_build_reads_both_quote_styles(self):
        build = parse_build_file(GROOVY_BUILD)
        assert build.dependencies == (
            GradleDependency(JACKSON, "jackson-databind", "2.17.0"),
            GradleDependency("org.slf4j", "slf4j-api", "2.0.13"),
            GradleDependency("org.junit.jupiter", "junit-jupiter", None),
            GradleDependency("com.example", "annotations", "1.0"),
        )
        assert build.boms == ()

    def test_kotlin_build_reads_both_quote_styles_and_wrapped_calls(self):
        build = parse_build_file(KOTLIN_BUILD)
        assert build.dependencies == (
            GradleDependency(JACKSON, "jackson-databind", "2.17.0"),
            GradleDependency("com.acme", "billing-client", "3.1.4"),
            GradleDependency("org.junit.jupiter", "junit-jupiter", "5.10.2"),
            GradleDependency("com.example", "driver", None),
        )

    def test_gradle_build_reads_the_same_as_parse_build_file_for_both_dialects(self):
        read, calls = counting_reader({})
        assert gradle_build("build.gradle", GROOVY_BUILD, read) == parse_build_file(GROOVY_BUILD)
        assert gradle_build("app/build.gradle.kts", KOTLIN_BUILD, read) == parse_build_file(KOTLIN_BUILD)
        assert calls == []

    def test_classifier_extension_and_interpolated_versions(self):
        text = """\
dependencies {
    implementation "com.acme:native:1.2:linux-x86_64"
    implementation 'com.acme:widget:4.0@aar'
    implementation "com.acme:core:$coreVersion"
    implementation("com.acme:util:${versions.util}")
}
"""
        assert parse_build_file(text).dependencies == (
            GradleDependency("com.acme", "native", "1.2"),
            GradleDependency("com.acme", "widget", "4.0"),
            GradleDependency("com.acme", "core", "$coreVersion"),
            GradleDependency("com.acme", "util", "${versions.util}"),
        )

    def test_projects_urls_plugin_ids_and_class_names_are_not_coordinates(self):
        text = """\
plugins {
    id("org.springframework.boot") version "3.3.4"
}
repositories {
    maven { url = uri("https://repo.example.com/maven") }
    maven { url 'https://repo.example.com/other' }
}
dependencies {
    implementation(project(":core"))
    implementation project(':lib:model')
}
application {
    mainClass.set("com.acme.Main")
}
"""
        assert parse_build_file(text).dependencies == ()

    def test_commented_out_declarations_are_ignored(self):
        text = """\
sourceSets {
    main { java { include '**/*.java' } }
}
dependencies {
    // implementation "com.acme:old:1.0"
    /* api 'com.acme:gone:2.0' */
    /*
     * testImplementation("com.acme:also-gone:3.0")
     */
    implementation "com.acme:kept:1.0" // pinned for #20
}
tasks.test {
    exclude '**/Slow*'
}
"""
        assert parse_build_file(text).dependencies == (GradleDependency("com.acme", "kept", "1.0"),)

    def test_several_coordinates_in_one_call_and_duplicates_collapse_in_order(self):
        text = """\
dependencies {
    implementation "com.acme:a:1", "com.acme:b:2"
    testImplementation "com.acme:a:1"
    add("implementation", "com.acme:c:3")
}
"""
        assert parse_build_file(text).dependencies == (
            GradleDependency("com.acme", "a", "1"),
            GradleDependency("com.acme", "b", "2"),
            GradleDependency("com.acme", "c", "3"),
        )

    def test_map_notation_is_out_of_scope(self):
        text = """\
dependencies {
    implementation group: 'com.acme', name: 'legacy', version: '1.0'
    implementation(
        group: 'com.acme',
        name: 'wrapped',
    )
}
"""
        build = parse_build_file(text)
        assert build.dependencies == ()
        assert build.group is None

    def test_crlf_line_endings(self):
        text = "group = 'com.acme'\r\ndependencies {\r\n    api 'org.slf4j:slf4j-api:2.0.13'\r\n}\r\n"
        build = parse_build_file(text)
        assert build.group == "com.acme"
        assert build.dependencies == (GradleDependency("org.slf4j", "slf4j-api", "2.0.13"),)


class TestPlatforms:
    def test_platform_and_enforced_platform_are_bom_owners_in_kotlin(self):
        text = """\
dependencies {
    implementation(platform("org.springframework.boot:spring-boot-dependencies:3.3.4"))
    implementation(enforcedPlatform("com.fasterxml.jackson:jackson-bom:2.17.0"))
    implementation("com.fasterxml.jackson.core:jackson-databind")
}
"""
        build = parse_build_file(text)
        assert build.boms == (
            BOOT_BOM,
            GradleDependency("com.fasterxml.jackson", "jackson-bom", "2.17.0"),
        )
        assert build.dependencies == (GradleDependency(JACKSON, "jackson-databind", None),)

    def test_platform_and_enforced_platform_are_bom_owners_in_groovy(self):
        text = """\
dependencies {
    implementation platform('org.springframework.boot:spring-boot-dependencies:3.3.4')
    testImplementation enforcedPlatform ("org.junit:junit-bom:5.10.2")
    implementation 'com.fasterxml.jackson.core:jackson-databind'
}
"""
        build = parse_build_file(text)
        assert build.boms == (BOOT_BOM, GradleDependency("org.junit", "junit-bom", "5.10.2"))
        assert build.dependencies == (GradleDependency(JACKSON, "jackson-databind", None),)

    def test_dependency_management_plugin_maven_bom_is_a_bom_owner(self):
        text = """\
dependencyManagement {
    imports {
        mavenBom "org.springframework.boot:spring-boot-dependencies:3.3.4"
    }
}
"""
        build = parse_build_file(text)
        assert build.boms == (BOOT_BOM,)
        assert build.dependencies == ()

    def test_platform_catalog_accessor_turns_the_catalog_library_into_a_bom(self):
        text = """\
dependencies {
    implementation(platform(libs.spring.boot.bom))
    implementation(libs.jackson.databind)
}
"""
        read, _ = counting_reader({"gradle/libs.versions.toml": CATALOG})
        build = gradle_build("build.gradle.kts", text, read)
        assert build.boms == (BOOT_BOM,)
        assert BOOT_BOM not in build.dependencies
        assert GradleDependency(JACKSON, "jackson-databind", "2.17.0") in build.dependencies

    def test_platform_catalog_accessor_without_a_catalog_names_nothing(self):
        build = parse_build_file("dependencies { implementation(platform(libs.spring.boot.bom)) }\n")
        assert build.boms == ()
        assert build.dependencies == ()


class TestOwnGroup:
    def test_assignment_and_command_forms_in_either_quote_style(self):
        assert parse_build_file('group = "com.acme"\n').group == "com.acme"
        assert parse_build_file("group = 'com.acme'\n").group == "com.acme"
        assert parse_build_file("group 'com.acme'\n").group == "com.acme"
        assert parse_build_file('group "com.acme";\n').group == "com.acme"
        assert parse_build_file('    project.group = "com.acme.billing"  // own\n').group == "com.acme.billing"

    def test_first_group_wins_and_the_group_is_not_a_dependency(self):
        text = """\
allprojects {
    group = "com.acme"
}
subprojects {
    group = "com.example"
}
"""
        build = parse_build_file(text)
        assert build.group == "com.acme"
        assert build.dependencies == ()

    def test_exclusions_interpolation_and_comments_never_set_the_group(self):
        text = """\
// group = "com.commented"
group = "$rootGroup"
dependencies {
    implementation('com.acme:web:1.0') {
        exclude group: 'org.slf4j', module: 'slf4j-simple'
    }
    implementation("com.acme:api:1.0") {
        exclude(group = "org.slf4j", module = "slf4j-simple")
        exclude(
            group = "commons-logging",
            module = "commons-logging"
        )
    }
}
"""
        build = parse_build_file(text)
        assert build.group is None
        assert build.dependencies == (
            GradleDependency("com.acme", "web", "1.0"),
            GradleDependency("com.acme", "api", "1.0"),
        )

    def test_no_group_is_none(self):
        assert parse_build_file(KOTLIN_BUILD).group is None


class TestCatalog:
    def test_version_ref_resolves_through_versions(self):
        libraries = parse_catalog(CATALOG)
        assert libraries["jackson-databind"] == GradleDependency(JACKSON, "jackson-databind", "2.17.0")
        assert libraries["jackson-annotations"] == GradleDependency(JACKSON, "jackson-annotations", "2.17.0")

    def test_ref_table_form_resolves_the_same_as_version_ref(self):
        assert parse_catalog(CATALOG)["jackson-core"] == GradleDependency(JACKSON, "jackson-core", "2.17.0")

    def test_the_three_library_forms(self):
        libraries = parse_catalog(CATALOG)
        assert libraries["acme-client"] == GradleDependency("com.acme", "client", "1.4.2")
        assert libraries["acme-model"] == GradleDependency("com.acme", "model", "0.9.1")
        assert libraries["slf4j-api"] == GradleDependency("org.slf4j", "slf4j-api", "2.0.13")

    def test_a_rich_version_behind_a_ref_resolves(self):
        assert parse_catalog(CATALOG)["spring-boot-bom"] == BOOT_BOM

    def test_every_library_is_listed_in_catalog_order_and_bundles_and_plugins_are_ignored(self):
        assert list(parse_catalog(CATALOG)) == [
            "jackson-databind", "jackson-core", "jackson-annotations", "acme-client",
            "acme-model", "acme-bare", "slf4j-api", "spring-boot-bom",
        ]

    def test_missing_and_dangling_versions_are_none(self):
        text = """\
[libraries]
bare = { module = "com.acme:bare" }
dangling = { module = "com.acme:dangling", version.ref = "missing" }
string-bare = "com.acme:string-bare"
"""
        assert parse_catalog(text) == {
            "bare": GradleDependency("com.acme", "bare", None),
            "dangling": GradleDependency("com.acme", "dangling", None),
            "string-bare": GradleDependency("com.acme", "string-bare", None),
        }

    def test_rich_versions_take_strictly_then_require_then_prefer(self):
        text = """\
[libraries]
strict = { module = "com.acme:strict", version = { strictly = "1.0", prefer = "1.1" } }
required = { module = "com.acme:required", version = { require = "2.0", prefer = "2.1" } }
preferred = { module = "com.acme:preferred", version = { prefer = "3.1" } }
"""
        libraries = parse_catalog(text)
        assert libraries["strict"].version == "1.0"
        assert libraries["required"].version == "2.0"
        assert libraries["preferred"].version == "3.1"

    def test_malformed_entries_are_skipped(self):
        text = """\
[libraries]
number = 42
no-colon = "com.acme.client"
name-only = { name = "client", version = "1.0" }
bad-module = { module = "com.acme", version = "1.0" }
too-many = { module = "com.acme:a:b", version = "1.0" }
good = "com.acme:good:1.0"
"""
        assert parse_catalog(text) == {"good": GradleDependency("com.acme", "good", "1.0")}

    def test_broken_toml_contributes_nothing(self):
        assert parse_catalog("[libraries\njackson = { module = ") == {}
        assert parse_catalog('libraries = "not a table"\n') == {}
        assert parse_catalog("") == {}


class TestCatalogProbe:
    def test_no_catalog_read_without_a_libs_mention(self):
        read, calls = counting_reader({"gradle/libs.versions.toml": CATALOG})
        build = gradle_build("build.gradle", GROOVY_BUILD, read)
        assert calls == []
        assert build.catalog_path is None
        assert build == parse_build_file(GROOVY_BUILD)

    def test_a_libs_mention_inside_a_comment_does_not_probe(self):
        read, calls = counting_reader({"gradle/libs.versions.toml": CATALOG})
        text = 'dependencies {\n    // implementation(libs.jackson.databind)\n    api("com.acme:a:1")\n}\n'
        build = gradle_build("build.gradle.kts", text, read)
        assert calls == []
        assert build.dependencies == (GradleDependency("com.acme", "a", "1"),)

    def test_probe_order_is_own_directory_then_root(self):
        read, calls = counting_reader({})
        build = gradle_build("services/api/build.gradle.kts", "dependencies { implementation(libs.acme.client) }", read)
        assert calls == [
            "services/api/gradle/libs.versions.toml",
            "services/api/libs.versions.toml",
            "gradle/libs.versions.toml",
        ]
        assert build == GradleBuild()

    def test_first_hit_wins(self):
        own = '[libraries]\nown = "com.acme:own:1.0"\n'
        read, calls = counting_reader({
            "services/api/libs.versions.toml": own,
            "gradle/libs.versions.toml": CATALOG,
        })
        build = gradle_build("services/api/build.gradle", "dependencies { implementation libs.own }", read)
        assert calls == ["services/api/gradle/libs.versions.toml", "services/api/libs.versions.toml"]
        assert build.catalog_path == "services/api/libs.versions.toml"
        assert build.dependencies == (GradleDependency("com.acme", "own", "1.0"),)

    def test_root_catalog_resolves_the_jackson_import_through_version_ref(self):
        text = """\
group = "com.acme"

dependencies {
    implementation(libs.jackson.databind)
    implementation("org.slf4j:slf4j-api:2.0.13")
}
"""
        read, calls = counting_reader({"gradle/libs.versions.toml": CATALOG})
        build = gradle_build("orders/build.gradle.kts", text, read)
        assert calls[-1] == "gradle/libs.versions.toml"
        assert build.catalog_path == "gradle/libs.versions.toml"
        assert build.group == "com.acme"
        assert GradleDependency(JACKSON, "jackson-databind", "2.17.0") in build.dependencies

    def test_every_catalog_library_counts_as_declared(self):
        read, _ = counting_reader({"gradle/libs.versions.toml": CATALOG})
        text = "dependencies {\n    implementation(libs.jackson.databind)\n    api 'com.example:extra:1.0'\n}\n"
        build = gradle_build("build.gradle.kts", text, read)
        assert build.dependencies == (
            GradleDependency("com.example", "extra", "1.0"),
            GradleDependency(JACKSON, "jackson-databind", "2.17.0"),
            GradleDependency(JACKSON, "jackson-core", "2.17.0"),
            GradleDependency(JACKSON, "jackson-annotations", "2.17.0"),
            GradleDependency("com.acme", "client", "1.4.2"),
            GradleDependency("com.acme", "model", "0.9.1"),
            GradleDependency("com.acme", "bare", None),
            GradleDependency("org.slf4j", "slf4j-api", "2.0.13"),
            BOOT_BOM,
        )
        assert build.boms == ()

    def test_broken_catalog_contributes_nothing_and_stops_the_probe(self):
        read, calls = counting_reader({
            "gradle/libs.versions.toml": "[libraries\nbroken = {",
            "libs.versions.toml": CATALOG,
        })
        text = "dependencies {\n    implementation(libs.jackson.databind)\n    api('com.acme:kept:1.0')\n}\n"
        build = gradle_build("build.gradle.kts", text, read)
        assert calls == ["gradle/libs.versions.toml"]
        assert build.catalog_path == "gradle/libs.versions.toml"
        assert build.dependencies == (GradleDependency("com.acme", "kept", "1.0"),)

    def test_a_read_that_raises_is_a_miss(self):
        calls: list[str] = []

        def read(path: str) -> str | None:
            calls.append(path)
            if path.startswith("app/"):
                raise OSError("forge unavailable")
            return CATALOG if path == "gradle/libs.versions.toml" else None

        build = gradle_build("app/build.gradle", "dependencies { implementation libs.acme.client }", read)
        assert calls[-1] == "gradle/libs.versions.toml"
        assert build.catalog_path == "gradle/libs.versions.toml"
        assert GradleDependency("com.acme", "client", "1.4.2") in build.dependencies

    def test_catalog_paths_for_a_root_build_and_an_explicit_root(self):
        assert catalog_paths("build.gradle") == ("gradle/libs.versions.toml", "libs.versions.toml")
        assert catalog_paths("modules/app/build.gradle.kts", root="modules/") == (
            "modules/app/gradle/libs.versions.toml",
            "modules/app/libs.versions.toml",
            "modules/gradle/libs.versions.toml",
        )

    def test_non_text_input_contributes_nothing(self):
        read, calls = counting_reader({})
        assert gradle_build("build.gradle", None, read) == GradleBuild()
        assert parse_build_file(None) == GradleBuild()
        assert parse_catalog(None) == {}
        assert calls == []


class TestModuleShape:
    def test_build_file_names_put_the_kotlin_dsl_first(self):
        assert BUILD_FILE_NAMES == ("build.gradle.kts", "build.gradle")

    def test_leaf_module_imports_nothing_from_prxref(self):
        tree = ast.parse(Path(jvm_gradle.__file__).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.level == 0
                imported.append(node.module or "")
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        assert imported
        assert not [name for name in imported if name == "prxref" or name.startswith("prxref.")]
