"""Unit tests for :mod:`prxref.chunk_context`.

The module is pure — every test supplies its own ``read`` callable — so these
pin the extraction, the manifest walk, and the caps without a forge, an LLM, or
a filesystem anywhere in the loop.
"""
from __future__ import annotations

from prxref.chunk_context import (
    ChunkFile,
    chunk_files,
    dependency_versions,
    imported_packages,
    referenced_definitions,
    render_context_blocks,
)
from prxref.triage import parse_unified_diff


def reader(files: dict[str, str]):
    def _read(path: str) -> str | None:
        return files.get(path)
    return _read


class TestImportExtraction:
    def test_js_forms_all_resolve_to_bare_package_names(self):
        added = [
            "import { Effect } from 'effect';",
            "import 'side-effect-pkg';",
            "const fs = require('graceful-fs');",
            "const lazy = await import('lodash/merge');",
            "import { x } from '@scope/pkg/deep';",
        ]
        assert imported_packages("src/a.ts", added) == {
            "effect", "side-effect-pkg", "graceful-fs", "lodash", "@scope/pkg",
        }

    def test_relative_and_node_specifiers_are_ignored(self):
        added = [
            "import { a } from './local';",
            "import { b } from '../up/there';",
            "import fs from 'node:fs';",
            "import x from '/abs/path';",
        ]
        assert imported_packages("src/a.ts", added) == set()

    def test_python_stdlib_and_relative_imports_are_excluded(self):
        added = [
            "import json",
            "import requests",
            "from pathlib import Path",
            "from .local import thing",
            "from prxref.triage import FileDiff",
            "import os, click",
        ]
        assert imported_packages("src/a.py", added) == {
            "requests", "prxref", "click",
        }

    def test_rust_external_crates_only(self):
        added = ["use serde::Serialize;", "use crate::thing::X;", "use std::io;"]
        assert imported_packages("src/a.rs", added) == {"serde"}

    def test_unknown_language_yields_nothing(self):
        assert imported_packages("README.md", ["import x from 'y';"]) == set()


class TestDependencyVersions:
    def test_package_json_dependencies_and_dev_dependencies(self):
        files = [ChunkFile("src/a.ts", ("import { E } from 'effect';",
                                        "import { t } from 'vitest';"))]
        read = reader({"package.json": (
            '{"dependencies": {"effect": "4.0.0-rc.110", "unused": "1.0.0"},'
            ' "devDependencies": {"vitest": "^2.1.0"}}'
        )})
        assert dependency_versions(files, read) == [
            "effect@4.0.0-rc.110", "vitest@^2.1.0",
        ]

    def test_only_imported_packages_are_reported(self):
        files = [ChunkFile("a.ts", ("import { E } from 'effect';",))]
        read = reader({"package.json": '{"dependencies": {"left-pad": "1.0.0"}}'})
        assert dependency_versions(files, read) == []

    def test_pyproject_project_dependencies(self):
        files = [ChunkFile("src/a.py", ("import requests",))]
        read = reader({"pyproject.toml": (
            '[project]\nname = "x"\ndependencies = ["requests>=2.31", "rich"]\n'
        )})
        assert dependency_versions(files, read) == ["requests@>=2.31"]

    def test_pyproject_poetry_dependencies(self):
        files = [ChunkFile("src/a.py", ("import httpx",))]
        read = reader({"pyproject.toml": (
            '[tool.poetry.dependencies]\nhttpx = "^0.27"\n'
        )})
        assert dependency_versions(files, read) == ["httpx@^0.27"]

    def test_pyproject_name_normalization_across_dash_and_underscore(self):
        files = [ChunkFile("a.py", ("import ruamel_yaml",))]
        read = reader({"pyproject.toml": '[project]\ndependencies = ["ruamel-yaml==0.18"]\n'})
        assert dependency_versions(files, read) == ["ruamel-yaml@==0.18"]

    def test_go_mod_require_block(self):
        files = [ChunkFile("cmd/a.go", ('import "github.com/pkg/errors"',))]
        read = reader({"go.mod": (
            "module x\n\nrequire (\n\tgithub.com/pkg/errors v0.9.1\n)\n"
        )})
        # Go package names are not import paths; extraction is deliberately
        # out of scope, so the manifest parses but nothing is emitted.
        assert dependency_versions(files, read) == []

    def test_cargo_dependencies_table_and_inline_version(self):
        files = [ChunkFile("src/a.rs", ("use serde::Serialize;", "use tokio::spawn;"))]
        read = reader({"Cargo.toml": (
            '[dependencies]\nserde = "1.0.203"\n'
            'tokio = { version = "1.38", features = ["full"] }\n'
        )})
        assert dependency_versions(files, read) == ["serde@1.0.203", "tokio@1.38"]

    def test_nearest_manifest_wins_over_the_repo_root(self):
        files = [ChunkFile("packages/web/src/a.ts", ("import { E } from 'effect';",))]
        read = reader({
            "packages/web/package.json": '{"dependencies": {"effect": "4.0.0"}}',
            "package.json": '{"dependencies": {"effect": "2.0.0"}}',
        })
        assert dependency_versions(files, read) == ["effect@4.0.0"]

    def test_walk_stops_at_the_repo_root(self):
        files = [ChunkFile("a/b/c.ts", ("import { E } from 'effect';",))]
        seen: list[str] = []

        def read(path: str) -> str | None:
            seen.append(path)
            return None

        assert dependency_versions(files, read) == []
        assert seen == ["a/b/package.json", "a/package.json", "package.json"]

    def test_results_are_sorted_and_deduplicated(self):
        files = [
            ChunkFile("a.ts", ("import { E } from 'effect';",)),
            ChunkFile("b.ts", ("import { E } from 'effect';", "import z from 'zod';")),
        ]
        read = reader({"package.json": '{"dependencies": {"effect": "4.0.0", "zod": "3.0"}}'})
        assert dependency_versions(files, read) == ["effect@4.0.0", "zod@3.0"]

    def test_a_malformed_manifest_contributes_nothing(self):
        files = [ChunkFile("a.ts", ("import { E } from 'effect';",))]
        assert dependency_versions(files, reader({"package.json": "{not json"})) == []


def _ts_file(defline: str = "const Positive = check(") -> str:
    body = [f"// filler {i}" for i in range(1, 10)]
    body += [defline, "  isInt(),", ");"]
    body += [f"// filler {i}" for i in range(13, 30)]
    return "\n".join(body) + "\n"


class TestReferencedDefinitions:
    def test_multi_line_definition_is_captured_to_the_balanced_paren(self):
        files = [ChunkFile("src/a.ts", ("  limit: Positive,",), frozenset({25, 26}))]
        out = referenced_definitions(files, reader({"src/a.ts": _ts_file()}))
        assert out == ["src/a.ts:10: const Positive = check(\n  isInt(),\n);"]

    def test_a_definition_inside_the_hunk_is_not_emitted(self):
        files = [ChunkFile("src/a.ts", ("  limit: Positive,",), frozenset(range(1, 30)))]
        assert referenced_definitions(files, reader({"src/a.ts": _ts_file()})) == []

    def test_an_identifier_defined_on_an_added_line_is_not_emitted(self):
        added = ("const Positive = 5;", "  limit: Positive,")
        files = [ChunkFile("src/a.ts", added, frozenset({25}))]
        assert referenced_definitions(files, reader({"src/a.ts": _ts_file()})) == []

    def test_python_def_class_and_column_zero_assignment(self):
        source = (
            "LIMIT = 10\n"
            "\n"
            "def helper(x):\n"
            "    return x\n"
            "\n"
            "class Widget:\n"
            "    pass\n"
        ) + "".join(f"# pad {i}\n" for i in range(8, 40))
        files = [ChunkFile("m.py", ("    return helper(LIMIT) or Widget()",), frozenset({50}))]
        assert referenced_definitions(files, reader({"m.py": source})) == [
            "m.py:1: LIMIT = 10",
            "m.py:3: def helper(x):",
            "m.py:6: class Widget:",
        ]

    def test_language_keywords_are_never_looked_up(self):
        source = "const type = 1;\n" + "// pad\n" * 20
        files = [ChunkFile("a.ts", ("  const x: type = 1;",), frozenset({30}))]
        assert referenced_definitions(files, reader({"a.ts": source})) == []

    def test_entries_are_ordered_by_path_then_line(self):
        a = "const Alpha = 1;\n" + "// pad\n" * 20
        b = "const Beta = 2;\nconst Gamma = 3;\n" + "// pad\n" * 20
        files = [
            ChunkFile("z/b.ts", ("Beta; Gamma;",), frozenset({40})),
            ChunkFile("a/a.ts", ("Alpha;",), frozenset({40})),
        ]
        out = referenced_definitions(files, reader({"a/a.ts": a, "z/b.ts": b}))
        assert out == [
            "a/a.ts:1: const Alpha = 1;",
            "z/b.ts:1: const Beta = 2;",
            "z/b.ts:2: const Gamma = 3;",
        ]

    def test_max_lines_per_entry_truncates_an_unbalanced_definition(self):
        source = "const Wide = f(\n" + "  a,\n" * 20 + ");\n"
        files = [ChunkFile("a.ts", ("Wide;",), frozenset({99}))]
        out = referenced_definitions(
            files, reader({"a.ts": source}), max_lines_per_entry=3,
        )
        assert out == ["a.ts:1: const Wide = f(\n  a,\n  a,"]

    def test_max_entries_appends_the_omitted_line(self):
        source = "".join(f"const N{i} = {i};\n" for i in range(6)) + "// pad\n" * 10
        added = (" ".join(f"N{i}" for i in range(6)),)
        files = [ChunkFile("a.ts", added, frozenset({99}))]
        out = referenced_definitions(files, reader({"a.ts": source}), max_entries=2)
        assert out[:2] == ["a.ts:1: const N0 = 0;", "a.ts:2: const N1 = 1;"]
        assert out[-1] == "… 4 more definitions omitted"

    def test_max_chars_appends_the_omitted_line(self):
        source = "".join(f"const N{i} = {i};\n" for i in range(4)) + "// pad\n" * 10
        added = (" ".join(f"N{i}" for i in range(4)),)
        files = [ChunkFile("a.ts", added, frozenset({99}))]
        out = referenced_definitions(files, reader({"a.ts": source}), max_chars=40)
        assert out[-1].endswith("more definitions omitted")

    def test_a_file_over_512_kib_is_skipped(self):
        source = "const Big = 1;\n" + "// pad\n" * 100_000
        files = [ChunkFile("a.ts", ("Big;",), frozenset({999_999}))]
        assert referenced_definitions(files, reader({"a.ts": source})) == []

    def test_an_unreadable_file_is_skipped(self):
        files = [ChunkFile("a.ts", ("Big;",), frozenset({9}))]
        assert referenced_definitions(files, reader({})) == []


class TestChunkFiles:
    def test_added_lines_and_hunk_coverage_come_from_the_parsed_diff(self):
        diff = (
            "diff --git a/src/a.ts b/src/a.ts\n"
            "--- a/src/a.ts\n"
            "+++ b/src/a.ts\n"
            "@@ -10,3 +10,4 @@\n"
            " ctx one\n"
            "+added line\n"
            " ctx two\n"
            " ctx three\n"
        )
        files = chunk_files(parse_unified_diff(diff))
        assert len(files) == 1
        assert files[0].path == "src/a.ts"
        assert files[0].added == ("added line",)
        assert files[0].hunk_lines == frozenset({10, 11, 12, 13})


class TestRenderContextBlocks:
    def test_both_blocks_render_with_headers(self):
        out = render_context_blocks(["effect@4.0.0"], ["a.ts:1: const X = 1;"])
        assert out.startswith("### Dependency versions\n\neffect@4.0.0")
        assert "### Definitions referenced by this chunk\n\na.ts:1: const X = 1;" in out

    def test_each_block_is_omitted_when_empty(self):
        assert render_context_blocks(["effect@4.0.0"], []) == (
            "### Dependency versions\n\neffect@4.0.0"
        )
        assert render_context_blocks([], ["a.ts:1: const X = 1;"]) == (
            "### Definitions referenced by this chunk\n\na.ts:1: const X = 1;"
        )

    def test_both_empty_renders_nothing(self):
        assert render_context_blocks([], []) == ""
