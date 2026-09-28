"""The repository config file's documentation and example (#38).

docs/config-file.md is the page every config-file error links to, so its two
key tables are a claim about ``config.FILE_KEYS`` and ``config.ENV_ONLY_KEYS``
that can go stale on its own. The tables are parsed and compared as sets in
both directions, so a key added to either list without a row, a row left
behind for a removed key, and a key filed in the wrong table all fail here.
docs/examples/prxref.toml is loaded by the real reader, so the example cannot
drift into a file prxref rejects.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

from prxref import config
from prxref.config import (
    CONFIG_DOCS_URL,
    CONFIG_FILE_ENV,
    CONFIG_FILE_NAME,
    ENV_ONLY_KEYS,
    FILE_KEYS,
    load_config,
    read_config_file,
)
from prxref.llm import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "config-file.md"
EXAMPLE_PATH = REPO_ROOT / "docs" / "examples" / "prxref.toml"
DOC = DOC_PATH.read_text(encoding="utf-8")

FILE_SECTION = "## Keys a repository file can set"
ENV_ONLY_SECTION = "## Settings a repository file cannot set"

_ROW_RE = re.compile(r"^\| `([a-z0-9_]+)` \| ([^|]+?) \| (.+) \|$")


def section(heading: str) -> str:
    """The text from ``heading`` to the next level-2 heading."""
    start = DOC.index(heading + "\n")
    end = DOC.find("\n## ", start + len(heading))
    return DOC[start:] if end == -1 else DOC[start:end]


def rows(heading: str) -> dict[str, tuple[str, str]]:
    """``{key: (second column, third column)}`` for every key row in a section."""
    found: dict[str, tuple[str, str]] = {}
    for line in section(heading).splitlines():
        match = _ROW_RE.match(line)
        if match:
            key, second, third = match.groups()
            assert key not in found, f"docs/config-file.md lists {key!r} twice"
            found[key] = (second.strip(), third.strip())
    return found


def expected_type(key: str) -> str:
    """The Type column docs/config-file.md must show for a file key."""
    if key == "llm_temperature":
        return "number or string"
    if key == "llm_seed":
        return 'integer or "off"'
    if key in config._INT_KEYS:
        return "integer"
    if key in config._FLOAT_KEYS:
        return "number"
    if key in config._BOOL_KEYS:
        return "boolean"
    if key in config._LIST_KEYS:
        return "array of strings"
    return "string"


class TestTheKeyTablesMatchTheSchema:
    def test_the_tables_are_found_at_all(self):
        assert len(rows(FILE_SECTION)) > 0
        assert len(rows(ENV_ONLY_SECTION)) > 0

    def test_file_table_lists_exactly_the_file_keys(self):
        documented = set(rows(FILE_SECTION))
        assert documented - FILE_KEYS == set(), (
            "docs/config-file.md lists keys a repository file cannot set: "
            f"{sorted(documented - FILE_KEYS)}"
        )
        assert FILE_KEYS - documented == set(), (
            "config.FILE_KEYS has keys missing from docs/config-file.md: "
            f"{sorted(FILE_KEYS - documented)}"
        )

    def test_env_only_table_lists_exactly_the_env_only_keys(self):
        documented = set(rows(ENV_ONLY_SECTION))
        assert documented - ENV_ONLY_KEYS == set(), (
            "docs/config-file.md lists as environment-only keys that are not: "
            f"{sorted(documented - ENV_ONLY_KEYS)}"
        )
        assert ENV_ONLY_KEYS - documented == set(), (
            "config.ENV_ONLY_KEYS has keys missing from docs/config-file.md: "
            f"{sorted(ENV_ONLY_KEYS - documented)}"
        )

    @pytest.mark.parametrize("key", sorted(FILE_KEYS))
    def test_file_key_type_matches_the_schema(self, key):
        documented_type, meaning = rows(FILE_SECTION)[key]
        assert documented_type == expected_type(key)
        assert "(env-vars.md#llm--pipeline)" in meaning

    @pytest.mark.parametrize("key", sorted(ENV_ONLY_KEYS))
    def test_env_only_reason_matches_the_schema(self, key):
        reason, why = rows(ENV_ONLY_SECTION)[key]
        assert reason == config._ENV_ONLY_REASONS[key]
        assert why

    def test_the_link_target_heading_exists(self):
        env_vars = (REPO_ROOT / "docs" / "env-vars.md").read_text(encoding="utf-8")
        assert "\n### LLM & Pipeline\n" in env_vars
        table = env_vars.split("\n### LLM & Pipeline\n", 1)[1].split("\n### ", 1)[0]
        missing = sorted(k for k in FILE_KEYS if f"| `PRXREF_{k.upper()}` |" not in table)
        assert missing == []

    def test_the_docs_url_names_this_page(self):
        assert CONFIG_DOCS_URL.endswith("/docs/config-file.md")
        assert DOC.startswith("# Repository config file")


class TestTheErrorExamplesAreVerbatim:
    """Each single-line example in the Errors table is fed to the real reader,
    and the message it raises must be the one the page prints."""

    _ERROR_ROW = re.compile(r"^\| `([^`]+)` \| `configuration error: (.+)` \|$")

    def examples(self) -> list[tuple[str, str]]:
        found = [
            m.groups()
            for m in map(self._ERROR_ROW.match, section("## Errors").splitlines())
            if m and CONFIG_DOCS_URL in m.group(2)
        ]
        return found

    def test_there_are_examples(self):
        assert len(self.examples()) >= 8

    def test_each_example_raises_its_documented_message(self, tmp_path):
        for toml_text, message in self.examples():
            path = tmp_path / CONFIG_FILE_NAME
            path.write_text(toml_text + "\n", encoding="utf-8")
            with pytest.raises(ConfigError) as info:
                read_config_file(path, display=CONFIG_FILE_NAME)
            assert str(info.value) == message, toml_text


@pytest.fixture
def example_repo(tmp_path, monkeypatch):
    """The example copied to the root of a repository, as its README says."""
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copyfile(EXAMPLE_PATH, root / CONFIG_FILE_NAME)
    monkeypatch.chdir(root)
    monkeypatch.delenv(CONFIG_FILE_ENV, raising=False)
    return root


class TestTheExampleFile:
    def test_the_reader_accepts_it(self, example_repo):
        values = read_config_file(example_repo / CONFIG_FILE_NAME)
        assert set(values) <= FILE_KEYS
        assert values["llm_models"] == ["z-ai/glm-5.3-flash", "openai/gpt-4o-mini"]
        assert values["repo_context"] == "diff"
        assert values["post_mode"] == "summary+inline"
        assert values["group_findings"] is True

    def test_it_covers_what_the_issue_asks_for(self, example_repo):
        values = read_config_file(example_repo / CONFIG_FILE_NAME)
        for key in (
            "review_rules", "scoped_rules", "llm_models", "repo_context",
            "post_mode", "max_inline_comments",
        ):
            assert key in values, key

    def test_its_paths_resolve_inside_the_repository(self, example_repo):
        values = read_config_file(example_repo / CONFIG_FILE_NAME)
        root = str(example_repo.resolve())
        assert values["review_rules"] == str(example_repo.resolve() / ".prxref" / "rules.md")
        for entry in values["scoped_rules"]:
            assert entry.startswith(root + "/.prxref/")

    def test_it_loads_through_the_whole_config_layer(self, example_repo, monkeypatch):
        for name in [k for k in list(os.environ) if k.startswith("PRXREF_")]:
            monkeypatch.delenv(name)
        cfg = load_config(config_file=example_repo / CONFIG_FILE_NAME)
        assert cfg["confidence_floor"] == 0.7
        assert cfg["max_error_findings"] == 5
        assert cfg["llm_timeout"] == 60.0

    def test_it_sets_nothing_environment_only(self):
        text = EXAMPLE_PATH.read_text(encoding="utf-8")
        assigned = set(re.findall(r"^([a-z0-9_]+)\s*=", text, re.M))
        assert assigned
        assert assigned & ENV_ONLY_KEYS == set()


class TestTheOtherDocumentsPointAtIt:
    @pytest.mark.parametrize(
        "relative",
        [
            "README.md",
            "docs/env-vars.md",
            "docs/forges.md",
            "docs/review-rules.md",
            "docs/prompt-templates.md",
        ],
    )
    def test_the_document_links_the_config_file_page(self, relative):
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "config-file.md" in text, relative
        assert ".prxref.toml" in text, relative

    @pytest.mark.parametrize("relative", ["docs/env-vars.md", ".env.example"])
    def test_the_variable_is_documented(self, relative):
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert CONFIG_FILE_ENV in text, relative

    def test_the_ci_recipes_name_the_validator(self):
        for relative in ("README.md", "docs/forges.md"):
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            assert "prxref config check" in text, relative
