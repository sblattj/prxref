"""Unit tests for :mod:`prxref.repo_resolve`, the repository-context resolver.

The resolver is pure: it turns a referencing file, its text and the names its
added lines reference into ordered candidate files, and never reads one. These
tests pin the per-language import rules, the Java same-package convention, the
name search over a listing, the two dead-candidate filters and the ordering,
against the issue #17 fixture and small synthetic sources. No reads of the
repository under review, no forge, no network.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from prxref.chunk_context import chunk_files
from prxref.repo_context import REASONS, language_of, referenced_names
from prxref.repo_resolve import MAX_NAME_SEARCH_PER_NAME, Candidate, resolve_candidates
from prxref.triage import parse_unified_diff

FIXTURE = Path(__file__).parent / "fixtures" / "issue17"
REPO = FIXTURE / "repo"
SERVICE = "src/main/java/com/acme/connectors/ConnectorService.java"
CONFIG = "src/main/java/com/acme/connectors/TransportConfig.java"
LISTING = sorted(p.relative_to(REPO).as_posix() for p in REPO.rglob("*") if p.is_file())
SPRING = ("PathVariable", "PostMapping", "RequestBody", "RequestHeader")


def resolve(path, text, names, *, listing=None, complete=False):
    return [
        (c.name, c.path, c.reason)
        for c in resolve_candidates(path, text, names, listing=listing, listing_complete=complete)
    ]


def fixture_added(path: str) -> tuple[str, ...]:
    files = parse_unified_diff((FIXTURE / "pr.diff").read_text(encoding="utf-8"))
    for chunk_file in chunk_files(files):
        if chunk_file.path == path:
            return chunk_file.added
    raise AssertionError(f"{path} is not in the fixture diff")


def fixture_names(path: str) -> list[str]:
    return referenced_names(fixture_added(path), language_of(path))


def fixture_text(path: str) -> str:
    return (REPO / path).read_text(encoding="utf-8")


def probes(stem: str, suffixes) -> list[str]:
    return [stem + suffix for suffix in suffixes]


TS_PROBES = (".ts", ".tsx", ".d.ts", ".js", "/index.ts")
PY_PROBES = (".py", "/__init__.py")


class TestCandidateShape:
    def test_candidate_is_frozen(self):
        candidate = Candidate(name="A", path="a/A.java", reason="import")
        with pytest.raises(dataclasses.FrozenInstanceError):
            candidate.path = "b/A.java"

    def test_every_reason_is_an_admission_rank(self):
        assert {"import", "path-convention", "name-search"} <= set(REASONS)
        text = "package com.acme.billing;\nimport com.acme.shared.Money;\n"
        found = resolve(
            "src/main/java/com/acme/billing/Invoice.java",
            text,
            ["Money", "Tax", "Ledger"],
            listing=["lib/Ledger.java"],
        )
        assert {reason for _, _, reason in found} == {"import", "path-convention", "name-search"}

    def test_cap_constant(self):
        assert MAX_NAME_SEARCH_PER_NAME == 3


class TestFixture:
    def test_fixture_names_include_the_record_and_the_spring_annotations(self):
        names = fixture_names(SERVICE)
        assert "TransportConfig" in names
        assert set(SPRING) <= set(names)

    def test_transport_config_resolves_by_same_package_convention(self):
        found = resolve(SERVICE, fixture_text(SERVICE), fixture_names(SERVICE), listing=LISTING, complete=True)
        assert found == [("TransportConfig", CONFIG, "path-convention")]

    def test_spring_imports_give_no_candidate_even_without_a_listing(self):
        found = resolve(SERVICE, fixture_text(SERVICE), fixture_names(SERVICE))
        assert not {name for name, _, _ in found} & set(SPRING)
        assert found == [
            ("TransportConfig", CONFIG, "path-convention"),
            ("Tenant", "src/main/java/com/acme/connectors/Tenant.java", "path-convention"),
            ("Id", "src/main/java/com/acme/connectors/Id.java", "path-convention"),
            (
                "CreateTransportRequest",
                "src/main/java/com/acme/connectors/CreateTransportRequest.java",
                "path-convention",
            ),
        ]

    def test_added_lines_as_text_lose_the_context_import(self):
        text = "\n".join(fixture_added(SERVICE))
        found = resolve(SERVICE, text, fixture_names(SERVICE))
        names = [name for name, _, _ in found]
        assert "RequestHeader" in names
        assert not {"PathVariable", "PostMapping", "RequestBody"} & set(names)

    def test_the_changed_record_never_resolves_to_itself(self):
        names = fixture_names(CONFIG)
        assert names == ["TransportConfig"]
        assert resolve(CONFIG, fixture_text(CONFIG), names, listing=LISTING, complete=True) == []
        assert resolve(CONFIG, fixture_text(CONFIG), names, listing=LISTING) == []

    def test_in_org_import_resolves_under_the_derived_source_root(self):
        text = fixture_text(SERVICE).replace(
            "package com.acme.connectors;\n", "package com.acme.connectors;\n\nimport com.acme.shared.Tenant;\n", 1
        )
        tenant = "src/main/java/com/acme/shared/Tenant.java"
        found = resolve(SERVICE, text, fixture_names(SERVICE), listing=[*LISTING, tenant], complete=True)
        assert found == [
            ("Tenant", tenant, "import"),
            ("TransportConfig", CONFIG, "path-convention"),
        ]

    def test_sql_file_resolves_nothing(self):
        path = "db/changelog/003-idempotency-unique.sql"
        assert resolve(path, fixture_text(path), fixture_names(path), listing=LISTING) == []


BILLING = "src/main/java/com/acme/billing/Invoice.java"
HEADER = "package com.acme.billing;\n\n"


class TestJava:
    def test_explicit_import(self):
        found = resolve(BILLING, HEADER + "import com.acme.shared.Money;\n", ["Money"])
        assert found == [("Money", "src/main/java/com/acme/shared/Money.java", "import")]

    def test_explicit_import_of_an_unreferenced_name_gives_nothing(self):
        assert resolve(BILLING, HEADER + "import com.acme.shared.Money;\n", ["Tax"]) == [
            ("Tax", "src/main/java/com/acme/billing/Tax.java", "path-convention")
        ]

    def test_wildcard_import_probes_each_name_then_conventions_follow(self):
        found = resolve(BILLING, HEADER + "import com.acme.shared.*;\n", ["Money", "Tax"])
        assert found == [
            ("Money", "src/main/java/com/acme/shared/Money.java", "import"),
            ("Tax", "src/main/java/com/acme/shared/Tax.java", "import"),
            ("Money", "src/main/java/com/acme/billing/Money.java", "path-convention"),
            ("Tax", "src/main/java/com/acme/billing/Tax.java", "path-convention"),
        ]

    def test_wildcard_skips_a_name_a_single_type_import_binds(self):
        text = HEADER + "import com.acme.shared.*;\nimport com.acme.other.Money;\n"
        found = resolve(BILLING, text, ["Money"])
        assert found == [("Money", "src/main/java/com/acme/other/Money.java", "import")]

    def test_static_import_gives_no_candidate(self):
        text = HEADER + "import static com.acme.shared.Money.Zero;\nimport static com.acme.shared.Money.*;\n"
        assert resolve(BILLING, text, ["Zero"]) == []
        assert [r for _, _, r in resolve(BILLING, text, ["Money"])] == ["path-convention"]

    def test_jdk_import_is_skipped_and_not_name_searched(self):
        text = HEADER + "import java.util.List;\nimport javax.inject.Named;\n"
        listing = ["lib/List.java", "lib/Named.java"]
        assert resolve(BILLING, text, ["List", "Named"], listing=listing) == []

    def test_other_organization_is_third_party(self):
        text = HEADER + "import org.acme.Widget;\nimport com.other.Gadget;\nimport com.acme.shared.Money;\n"
        listing = ["lib/Widget.java", "lib/Gadget.java"]
        found = resolve(BILLING, text, ["Widget", "Gadget", "Money"], listing=listing)
        assert found == [("Money", "src/main/java/com/acme/shared/Money.java", "import")]

    def test_one_segment_package_is_its_own_organization(self):
        text = "package acme;\nimport acme.shared.Money;\nimport other.Tax;\n"
        assert resolve("src/acme/Invoice.java", text, ["Money", "Tax"]) == [
            ("Money", "src/acme/shared/Money.java", "import")
        ]

    def test_unknown_root_skips_imports_but_not_conventions(self):
        path = "src/main/java/com/acme/wrong/Invoice.java"
        found = resolve(path, HEADER + "import com.acme.shared.Money;\n", ["Money", "Tax"])
        assert found == [("Tax", "src/main/java/com/acme/wrong/Tax.java", "path-convention")]

    def test_no_package_line_resolves_no_import(self):
        found = resolve(BILLING, "import com.acme.shared.Money;\nimport com.acme.shared.*;\n", ["Money", "Tax"])
        assert found == [("Tax", "src/main/java/com/acme/billing/Tax.java", "path-convention")]

    def test_nested_type_maps_to_the_outer_file(self):
        found = resolve(BILLING, HEADER + "import com.acme.shared.Money.Currency;\n", ["Currency"])
        assert found == [("Currency", "src/main/java/com/acme/shared/Money.java", "import")]

    def test_nested_wildcard_maps_to_the_outer_file(self):
        found = resolve(BILLING, HEADER + "import com.acme.shared.Money.*;\n", ["Currency"])
        assert found == [
            ("Currency", "src/main/java/com/acme/shared/Money.java", "import"),
            ("Currency", "src/main/java/com/acme/billing/Currency.java", "path-convention"),
        ]

    def test_text_none_still_gives_conventions(self):
        assert resolve(BILLING, None, ["Money"]) == [
            ("Money", "src/main/java/com/acme/billing/Money.java", "path-convention")
        ]

    def test_package_at_the_repository_root(self):
        found = resolve("com/acme/billing/Invoice.java", HEADER + "import com.acme.shared.Money;\n", ["Money"])
        assert found == [("Money", "com/acme/shared/Money.java", "import")]

    def test_top_level_file_convention(self):
        assert resolve("Invoice.java", None, ["Money"]) == [("Money", "Money.java", "path-convention")]


class TestPython:
    def test_absolute_import_tries_root_then_src(self):
        found = resolve("app/service.py", "from acme.models import User\n", ["User"])
        assert found == [
            ("User", "acme/models.py", "import"),
            ("User", "acme/models/__init__.py", "import"),
            ("User", "src/acme/models.py", "import"),
            ("User", "src/acme/models/__init__.py", "import"),
        ]

    def test_relative_single_dot(self):
        found = resolve("pkg/m.py", "from .x import C\n", ["C"])
        assert found == [("C", "pkg/x.py", "import"), ("C", "pkg/x/__init__.py", "import")]

    def test_relative_double_dot(self):
        found = resolve("pkg/sub/m.py", "from ..y.z import C\n", ["C"])
        assert found == [("C", "pkg/y/z.py", "import"), ("C", "pkg/y/z/__init__.py", "import")]

    def test_relative_to_the_root_is_kept(self):
        found = resolve("pkg/m.py", "from ..z import C\n", ["C"])
        assert found == [("C", "z.py", "import"), ("C", "z/__init__.py", "import")]

    def test_relative_beyond_the_root_is_dropped(self):
        assert resolve("pkg/m.py", "from ...z import C\n", ["C"]) == []
        assert resolve("m.py", "from ..z import C\n", ["C"]) == []

    def test_bare_relative_package(self):
        assert resolve("pkg/sub/m.py", "from . import C\n", ["C"]) == [("C", "pkg/sub/__init__.py", "import")]
        assert resolve("pkg/sub/m.py", "from .. import C\n", ["C"]) == [("C", "pkg/__init__.py", "import")]

    def test_plain_import_is_skipped(self):
        assert resolve("app/m.py", "import acme.models\nimport acme.models as am\n", ["acme", "models", "am"]) == []

    def test_alias_looks_up_the_imported_name(self):
        found = resolve("app/m.py", "from acme.models import User as Account\n", ["Account"])
        assert [(name, reason) for name, _, reason in found] == [("User", "import")] * 4

    def test_parenthesized_import_orders_by_names(self):
        text = "from .models import (\n    User,  # the user\n    Group,\n)\n"
        found = resolve("app/m.py", text, ["Group", "User"])
        assert found == [
            ("Group", "app/models.py", "import"),
            ("Group", "app/models/__init__.py", "import"),
            ("User", "app/models.py", "import"),
            ("User", "app/models/__init__.py", "import"),
        ]

    def test_indented_and_continued_import(self):
        text = "if TYPE_CHECKING:\n    from .models import User, \\\n        Group\n"
        found = resolve("app/m.py", text, ["Group"])
        assert found == [("Group", "app/models.py", "import"), ("Group", "app/models/__init__.py", "import")]

    def test_star_import_probes_every_unbound_name(self):
        text = "from .models import *\nfrom .other import Group\n"
        found = resolve("app/m.py", text, ["User", "Group"])
        assert found == [
            ("User", "app/models.py", "import"),
            ("User", "app/models/__init__.py", "import"),
            ("Group", "app/other.py", "import"),
            ("Group", "app/other/__init__.py", "import"),
        ]

    def test_standard_library_is_skipped_and_not_name_searched(self):
        text = "from __future__ import annotations\nfrom typing import Any\nfrom collections import OrderedDict\n"
        listing = ["lib/any.py", "lib/ordered_dict.py", "lib/annotations.py"]
        assert resolve("app/m.py", text, ["annotations", "Any", "OrderedDict"], listing=listing) == []

    def test_pyi_is_python(self):
        found = resolve("app/m.pyi", "from .x import C\n", ["C"])
        assert found == [("C", "app/x.py", "import"), ("C", "app/x/__init__.py", "import")]


APP = "web/src/app.ts"


class TestTypeScript:
    def test_named_import(self):
        found = resolve(APP, "import {Button} from './ui/button';\n", ["Button"])
        assert found == [("Button", p, "import") for p in probes("web/src/ui/button", TS_PROBES)]

    def test_default_import(self):
        found = resolve(APP, 'import Card from "../card";\n', ["Card"])
        assert found == [("Card", p, "import") for p in probes("web/card", TS_PROBES)]

    def test_namespace_import_probes_referenced_members(self):
        text = "import * as Api from './api';\nconst user = Api.fetchUser(1);\n"
        found = resolve(APP, text, ["Api", "fetchUser", "user"])
        assert found == [("fetchUser", p, "import") for p in probes("web/src/api", TS_PROBES)]

    def test_export_from(self):
        found = resolve(APP, "export {Modal} from './modal';\n", ["Modal"])
        assert found == [("Modal", p, "import") for p in probes("web/src/modal", TS_PROBES)]

    def test_export_default_as(self):
        found = resolve(APP, "export { default as Panel } from './panel';\n", ["Panel"])
        assert found == [("Panel", p, "import") for p in probes("web/src/panel", TS_PROBES)]

    def test_import_alias_looks_up_the_exported_name(self):
        found = resolve(APP, "import {Button as Btn} from './button';\n", ["Btn"])
        assert {name for name, _, _ in found} == {"Button"}

    def test_default_and_named_together(self):
        text = "import React, { useThing } from './react-lite';\n"
        found = resolve(APP, text, ["useThing", "React"])
        assert [name for name, _, _ in found] == ["useThing"] * 5 + ["React"] * 5

    def test_multiline_type_only_import_with_comments(self):
        text = "import type {\n  User,\n  Group, // the group\n} from './types';\n"
        found = resolve(APP, text, ["Group"])
        assert found == [("Group", p, "import") for p in probes("web/src/types", TS_PROBES)]

    def test_candidate_order_follows_names_not_statements(self):
        text = "import {B} from './b';\nimport {A} from './a';\n"
        found = resolve(APP, text, ["A", "B"])
        assert [path for _, path, _ in found] == probes("web/src/a", TS_PROBES) + probes("web/src/b", TS_PROBES)

    def test_bare_package_is_skipped_and_not_name_searched(self):
        text = "import {useState} from 'react';\nimport {Thing} from '@scope/pkg';\n"
        listing = ["web/src/useState.ts", "web/src/Thing.ts"]
        assert resolve(APP, text, ["useState", "Thing"], listing=listing) == []

    def test_esm_js_specifier_tries_the_typescript_source(self):
        found = resolve(APP, "import {slug} from './util.js';\n", ["slug"])
        assert found == [("slug", p, "import") for p in probes("web/src/util", (".ts", ".tsx", ".d.ts", ".js"))]

    def test_explicit_script_extension_is_literal(self):
        found = resolve(APP, "import {slug} from './util.mjs';\n", ["slug"])
        assert found == [("slug", "web/src/util.mjs", "import")]

    def test_asset_import_gives_nothing(self):
        text = "import styles from './app.module.css';\nimport logo from './logo.svg';\n"
        assert resolve(APP, text, ["styles", "logo"]) == []

    def test_directory_specifier_tries_index(self):
        assert resolve(APP, "import {A} from '.';\n", ["A"]) == [("A", "web/src/index.ts", "import")]
        assert resolve(APP, "import {A} from '../';\n", ["A"]) == [("A", "web/index.ts", "import")]

    def test_escaping_specifier_is_dropped(self):
        assert resolve("app.ts", "import X from '../x';\n", ["X"]) == []

    def test_plain_javascript_uses_the_same_rules(self):
        found = resolve("lib/a.js", "import {B} from './b';\n", ["B"])
        assert found == [("B", p, "import") for p in probes("lib/b", TS_PROBES)]

    def test_side_effect_import_and_export_star_give_nothing(self):
        text = "import './polyfill';\nexport * from './all';\n"
        assert resolve(APP, text, ["polyfill", "all"]) == []


class TestFilters:
    def test_complete_listing_drops_absent_import_and_convention(self):
        text = HEADER + "import com.acme.shared.Money;\n"
        listing = ["src/main/java/com/acme/billing/Tax.java"]
        found = resolve(BILLING, text, ["Money", "Tax", "Fee"], listing=listing, complete=True)
        assert found == [("Tax", "src/main/java/com/acme/billing/Tax.java", "path-convention")]

    def test_incomplete_listing_keeps_them(self):
        text = HEADER + "import com.acme.shared.Money;\n"
        listing = ["src/main/java/com/acme/billing/Tax.java"]
        found = resolve(BILLING, text, ["Money", "Tax", "Fee"], listing=listing, complete=False)
        assert found == [
            ("Money", "src/main/java/com/acme/shared/Money.java", "import"),
            ("Tax", "src/main/java/com/acme/billing/Tax.java", "path-convention"),
            ("Fee", "src/main/java/com/acme/billing/Fee.java", "path-convention"),
        ]

    def test_complete_listing_filters_python_probes(self):
        listing = ["src/acme/models.py"]
        found = resolve("app/m.py", "from acme.models import User\n", ["User"], listing=listing, complete=True)
        assert found == [("User", "src/acme/models.py", "import")]

    def test_complete_flag_without_a_listing_filters_nothing(self):
        found = resolve("app/m.py", "from acme.models import User\n", ["User"], listing=None, complete=True)
        assert len(found) == 4

    def test_listing_as_a_set(self):
        listing = {"src/acme/models.py"}
        found = resolve("app/m.py", "from acme.models import User\n", ["User"], listing=listing, complete=True)
        assert found == [("User", "src/acme/models.py", "import")]

    def test_spring_import_in_the_fixture_gives_no_candidate(self):
        found = resolve(SERVICE, fixture_text(SERVICE), list(SPRING), listing=LISTING, complete=False)
        assert found == []


class TestNameSearch:
    def test_no_listing_no_search(self):
        assert resolve("app/m.py", None, ["Widget"]) == []

    def test_case_sensitive_beats_case_insensitive(self):
        listing = ["a/widget.ts", "z/Widget.ts"]
        assert resolve(APP, None, ["Widget"], listing=listing) == [
            ("Widget", "z/Widget.ts", "name-search"),
            ("Widget", "a/widget.ts", "name-search"),
        ]

    def test_python_snake_case_form(self):
        listing = ["lib/transport_config.py", "lib/TransportConfig.ts"]
        assert resolve("app/m.py", None, ["TransportConfig"], listing=listing) == [
            ("TransportConfig", "lib/transport_config.py", "name-search")
        ]

    def test_snake_case_is_python_only(self):
        assert resolve(APP, None, ["TransportConfig"], listing=["lib/transport_config.ts"]) == []

    def test_snake_case_ranks_after_case_insensitive(self):
        listing = ["a/transport_config.py", "z/transportconfig.py"]
        assert [p for _, p, _ in resolve("app/m.py", None, ["TransportConfig"], listing=listing)] == [
            "z/transportconfig.py",
            "a/transport_config.py",
        ]

    def test_cap_of_three_per_name(self):
        listing = [f"m{i}/Widget.py" for i in range(5)]
        found = resolve("app/m.py", None, ["Widget"], listing=listing)
        assert [p for _, p, _ in found] == ["m0/Widget.py", "m1/Widget.py", "m2/Widget.py"]

    def test_deepest_shared_prefix_first_then_path(self):
        listing = ["z/Foo.py", "a/Foo.py", "a/b/x/Foo.py", "a/b/Foo.py"]
        found = resolve("a/b/c/m.py", None, ["Foo"], listing=listing)
        assert [p for _, p, _ in found] == ["a/b/Foo.py", "a/b/x/Foo.py", "a/Foo.py"]

    def test_shared_prefix_counts_whole_directories(self):
        listing = ["ab/Foo.py", "a/Foo.py"]
        found = resolve("a/m.py", None, ["Foo"], listing=listing)
        assert [p for _, p, _ in found] == ["a/Foo.py", "ab/Foo.py"]

    def test_other_language_excluded(self):
        listing = ["web/Widget.ts", "lib/Widget.py", "src/Widget.java", "docs/Widget.md"]
        assert resolve("app/m.py", None, ["Widget"], listing=listing) == [("Widget", "lib/Widget.py", "name-search")]

    def test_declaration_file_stem(self):
        assert resolve(APP, None, ["Widget"], listing=["types/Widget.d.ts"]) == [
            ("Widget", "types/Widget.d.ts", "name-search")
        ]

    def test_dunder_names_are_not_searched(self):
        assert resolve("app/m.py", None, ["__init__"], listing=["pkg/__init__.py"]) == []

    def test_language_without_definition_regexes_resolves_nothing(self):
        listing = ["x/idempotency_keys.sql", "cmd/Server.go"]
        assert resolve("db/001.sql", None, ["idempotency_keys"], listing=listing) == []
        assert resolve("cmd/main.go", None, ["Server"], listing=listing) == []

    def test_name_search_follows_imports_and_conventions(self):
        text = HEADER + "import com.acme.shared.Money;\n"
        listing = ["lib/Money.java", "lib/Tax.java"]
        assert resolve(BILLING, text, ["Money", "Tax"], listing=listing) == [
            ("Money", "src/main/java/com/acme/shared/Money.java", "import"),
            ("Tax", "src/main/java/com/acme/billing/Tax.java", "path-convention"),
            ("Money", "lib/Money.java", "name-search"),
            ("Tax", "lib/Tax.java", "name-search"),
        ]


class TestDedupAndPaths:
    def test_duplicate_keeps_the_first_reason(self):
        text = HEADER + "import com.acme.billing.*;\n"
        listing = ["src/main/java/com/acme/billing/Money.java"]
        assert resolve(BILLING, text, ["Money"], listing=listing) == [
            ("Money", "src/main/java/com/acme/billing/Money.java", "import")
        ]

    def test_convention_and_name_search_on_one_path_keep_the_convention(self):
        found = resolve(SERVICE, None, ["TransportConfig"], listing=LISTING)
        assert found == [("TransportConfig", CONFIG, "path-convention")]

    def test_referencing_path_is_never_a_candidate(self):
        path = "src/main/java/com/acme/billing/Money.java"
        text = HEADER + "import com.acme.billing.*;\n"
        assert resolve(path, text, ["Money"], listing=[path], complete=True) == []

    def test_duplicate_names_resolve_once(self):
        assert resolve(BILLING, None, ["Money", "Money"]) == [
            ("Money", "src/main/java/com/acme/billing/Money.java", "path-convention")
        ]

    def test_paths_are_normalized(self):
        found = resolve("./web//src/app.ts", "import {A} from './x/../y';\n", ["A"])
        assert [p for _, p, _ in found] == probes("web/src/y", TS_PROBES)
        for _, path, _ in found:
            assert not path.startswith(("/", "./")) and "/./" not in path and ".." not in path.split("/")

    def test_leading_slash_is_stripped_and_self_still_excluded(self):
        listing = ["lib/Widget.py"]
        assert resolve("/lib/Widget.py", None, ["Widget"], listing=listing) == []

    def test_unresolvable_names_are_absent(self):
        assert resolve("app/m.py", "from .x import C\n", ["Nope"], listing=[]) == []
