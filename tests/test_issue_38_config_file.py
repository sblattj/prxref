"""The repository config file layer (#38): ``.prxref.toml``."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from prxref import config
from prxref.config import (
    CONFIG_DOCS_URL,
    CONFIG_FILE_ENV,
    CONFIG_FILE_NAME,
    ENV_ONLY_KEYS,
    FILE_KEYS,
    SUGGESTIONS_MAX_TOKENS,
    find_config_file,
    load_config,
    read_config_file,
)
from prxref.llm import ConfigError

EXPECTED_ENV_ONLY = {
    "llm_backend": "executable",
    "llm_base_url": "endpoint",
    "llm_api_key": "credential",
    "llm_cli_path": "executable",
    "fail_on": "gate",
    "dry_run": "gate",
    "allow_unsigned": "gate",
    "trace_file": "local write",
    "trace_dir": "local write",
    "fallback": "local write",
    "price_table": "local read",
    "jira_base_url": "endpoint",
    "jira_email": "credential",
    "jira_api_token": "credential",
    "bitbucket_token": "credential",
    "bitbucket_user": "credential",
    "bitbucket_app_password": "credential",
    "bitbucket_server_token": "credential",
    "bitbucket_server_user": "credential",
    "bitbucket_server_password": "credential",
    "github_token": "credential",
    "github_enterprise_token": "credential",
    "gitlab_token": "credential",
    "gitea_token": "credential",
    "azure_devops_token": "credential",
    "bitbucket_webhook_secret": "credential",
    "github_webhook_secret": "credential",
    "gitlab_webhook_secret": "credential",
    "gitea_webhook_secret": "credential",
    "azure_devops_webhook_secret": "credential",
}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repository directory that is also the working directory."""
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.delenv(CONFIG_FILE_ENV, raising=False)
    return root


def write(root: Path, text: str, name: str = CONFIG_FILE_NAME) -> Path:
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


def config_error(path: Path) -> str:
    with pytest.raises(ConfigError) as info:
        read_config_file(path)
    return str(info.value)


class TestPartition:
    def test_every_key_is_classified_exactly_once(self):
        assert FILE_KEYS | ENV_ONLY_KEYS == set(config._DEFAULTS)
        assert not FILE_KEYS & ENV_ONLY_KEYS

    def test_env_only_set_matches_the_reviewed_table(self):
        assert ENV_ONLY_KEYS == set(EXPECTED_ENV_ONLY)

    def test_config_file_env_is_not_a_config_key(self):
        assert CONFIG_FILE_ENV == "PRXREF_CONFIG_FILE"
        assert "config_file" not in config._DEFAULTS


class TestPrecedence:
    def test_file_beats_default(self, repo):
        path = write(repo, "max_chunks = 3\n")
        assert load_config(config_file=path)["max_chunks"] == 3

    def test_env_beats_file(self, repo, monkeypatch):
        path = write(repo, "max_chunks = 3\n")
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "5")
        assert load_config(config_file=path)["max_chunks"] == 5

    def test_empty_env_leaves_the_file_value(self, repo, monkeypatch):
        path = write(repo, "max_chunks = 3\n")
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", " ")
        assert load_config(config_file=path)["max_chunks"] == 3

    def test_override_beats_env_and_file(self, repo, monkeypatch):
        path = write(repo, "max_chunks = 3\n")
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "5")
        assert load_config(config_file=path, max_chunks=7)["max_chunks"] == 7

    def test_file_value_error_names_file_and_key(self, repo):
        path = write(repo, "max_chunks = 0\n")
        with pytest.raises(ConfigError, match=r"^\.prxref\.toml: max_chunks: must be"):
            load_config(config_file=path)

    def test_env_value_error_names_env_var_over_file(self, repo, monkeypatch):
        path = write(repo, "max_chunks = 3\n")
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "0")
        with pytest.raises(ConfigError, match=r"^PRXREF_MAX_CHUNKS: must be"):
            load_config(config_file=path)

    def test_override_error_names_flag_over_file(self, repo):
        path = write(repo, "max_chunks = 3\n")
        with pytest.raises(ConfigError, match=r"^--max-chunks: must be"):
            load_config(
                config_file=path,
                source_labels={"max_chunks": "--max-chunks"},
                max_chunks=0,
            )

    def test_file_choice_error_names_file(self, repo):
        path = write(repo, 'repo_context = "everything"\n')
        with pytest.raises(ConfigError, match=r"^\.prxref\.toml: repo_context: must be one of"):
            load_config(config_file=path)

    def test_file_outside_cwd_is_labelled_by_its_path(self, tmp_path, monkeypatch):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        path = write(elsewhere, "max_chunks = 0\n", name="team.toml")
        with pytest.raises(ConfigError) as info:
            load_config(config_file=path)
        assert str(info.value).startswith(f"{path}: max_chunks: must be")


class TestTypes:
    @pytest.mark.parametrize(
        ("text", "key", "expected"),
        [
            ("max_chunks = 4", "max_chunks", 4),
            ("llm_timeout = 30", "llm_timeout", 30.0),
            ("llm_timeout = 12.5", "llm_timeout", 12.5),
            ("group_findings = true", "group_findings", True),
            ("post_verdict = false", "post_verdict", False),
            ('llm_models = ["a/b", "c"]', "llm_models", ["a/b", "c"]),
            ('post_mode = "summary"', "post_mode", "summary"),
            ('llm_temperature = "0.2"', "llm_temperature", "0.2"),
            ("llm_seed = 0", "llm_seed", 0),
            ('llm_seed = "off"', "llm_seed", "off"),
        ],
    )
    def test_accepted(self, repo, text, key, expected):
        value = read_config_file(write(repo, text + "\n"))[key]
        assert value == expected
        assert type(value) is type(expected)

    @pytest.mark.parametrize(
        ("text", "expected", "got"),
        [
            ("max_chunks = true", "an integer", "a boolean"),
            ("max_chunks = 4.0", "an integer", "a float"),
            ('max_chunks = "4"', "an integer", "a string"),
            ("llm_seed = true", 'an integer or "off"', "a boolean"),
            ('llm_seed = "OFF"', 'an integer or "off"', "a string"),
            ("llm_timeout = true", "a number", "a boolean"),
            ('llm_timeout = "30"', "a number", "a string"),
            ("group_findings = 1", "a boolean", "an integer"),
            ('group_findings = "1"', "a boolean", "a string"),
            ('llm_models = "a,b"', "an array of strings", "a string"),
            ("llm_models = [1, 2]", "an array of strings", "an array"),
            ("post_mode = 1", "a string", "an integer"),
            ("llm_temperature = 0.2", "a string", "a float"),
            ("post_mode = 1979-05-27", "a string", "a date"),
        ],
    )
    def test_rejected(self, repo, text, expected, got):
        key = text.split(" ", 1)[0]
        message = config_error(write(repo, text + "\n"))
        assert message == (
            f"{repo / CONFIG_FILE_NAME}: {key!r} must be {expected}, got {got}; "
            f"see {CONFIG_DOCS_URL}"
        )

    def test_empty_values_read_as_unset(self, repo):
        text = 'post_mode = ""\ncontext_contract_globs = []\nprompts_dir = ""\n'
        assert read_config_file(write(repo, text)) == {}
        cfg = load_config(config_file=repo / CONFIG_FILE_NAME)
        assert cfg["context_contract_globs"] == config._DEFAULTS["context_contract_globs"]
        assert cfg["prompts_dir"] is None

    def test_blank_list_entries_are_dropped(self, repo):
        path = write(repo, 'llm_models = ["a", " ", ""]\n')
        assert read_config_file(path) == {"llm_models": ["a"]}


class TestFileShape:
    def test_empty_file_is_valid(self, repo):
        path = write(repo, "")
        assert read_config_file(path) == {}
        assert load_config(config_file=path) == load_config()

    def test_comment_only_file_is_valid(self, repo):
        assert read_config_file(write(repo, "# nothing yet\n")) == {}

    def test_unknown_key_with_suggestion(self, repo):
        message = config_error(write(repo, "max_chunk = 3\n"))
        assert message == (
            f"{repo / CONFIG_FILE_NAME}: unknown key 'max_chunk'; did you mean "
            f"'max_chunks'? see {CONFIG_DOCS_URL}"
        )

    @pytest.mark.parametrize("key", ["MAX_CHUNKS", "PRXREF_MAX_CHUNKS"])
    def test_env_style_key_suggests_the_file_key(self, repo, key):
        message = config_error(write(repo, f"{key} = 3\n"))
        assert f"unknown key {key!r}; did you mean 'max_chunks'?" in message

    def test_unknown_key_without_suggestion(self, repo):
        message = config_error(write(repo, "zzz = 3\n"))
        assert message == (
            f"{repo / CONFIG_FILE_NAME}: unknown key 'zzz'; see {CONFIG_DOCS_URL}"
        )

    @pytest.mark.parametrize(
        "text", ["[prxref]\nmax_chunks = 3\n", "[tool.prxref]\nmax_chunks = 3\n"]
    )
    def test_nested_table_is_rejected(self, repo, text):
        message = config_error(write(repo, text))
        assert "is a table, but the config file is flat" in message
        assert message.endswith(f"see {CONFIG_DOCS_URL}")

    def test_inline_table_value_is_rejected(self, repo):
        message = config_error(write(repo, "max_chunks = { a = 1 }\n"))
        assert "'max_chunks' is a table, but the config file is flat" in message

    def test_syntax_error_names_the_line(self, repo):
        message = config_error(write(repo, "max_chunks = 3\npost_mode = \n"))
        assert message.startswith(f"{repo / CONFIG_FILE_NAME}: invalid TOML: ")
        assert "line 2" in message

    def test_non_utf8_file(self, repo):
        path = repo / CONFIG_FILE_NAME
        path.write_bytes(b'post_mode = "summary\xff"\n')
        message = config_error(path)
        assert message.startswith(f"{path}: not valid UTF-8")

    def test_display_overrides_the_path(self, repo):
        path = write(repo, "zzz = 1\n")
        with pytest.raises(ConfigError, match=r"^shown\.toml: unknown key"):
            read_config_file(path, display="shown.toml")

    def test_read_never_touches_the_environment(self, repo, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "5")
        assert read_config_file(write(repo, "max_chunks = 3\n")) == {"max_chunks": 3}


class TestEnvOnly:
    @pytest.mark.parametrize("key", sorted(EXPECTED_ENV_ONLY))
    def test_env_only_key_is_rejected(self, repo, key):
        message = config_error(write(repo, f'{key} = ""\n'))
        assert message == (
            f"{repo / CONFIG_FILE_NAME}: {key!r} cannot be set in a repository "
            f"config file ({EXPECTED_ENV_ONLY[key]}); set PRXREF_{key.upper()} "
            f"in the pipeline instead; see {CONFIG_DOCS_URL}"
        )


class TestSpecSources:
    @pytest.mark.parametrize(
        "entry",
        ["https://example.com/spec.md", "https://acme.atlassian.net/browse/ACME-1",
         "file:///etc/passwd"],
    )
    def test_url_is_rejected(self, repo, entry):
        message = config_error(write(repo, f'spec_sources = ["docs/spec.md", "{entry}"]\n'))
        assert f"'spec_sources' entry {entry!r} is a URL" in message
        assert "set PRXREF_SPEC_SOURCES in the pipeline" in message

    def test_local_paths_resolve(self, repo):
        path = write(repo, 'spec_sources = ["docs/spec.md", "SPEC.md"]\n')
        real = os.path.realpath(repo)
        assert read_config_file(path)["spec_sources"] == [
            os.path.join(real, "docs", "spec.md"),
            os.path.join(real, "SPEC.md"),
        ]

    def test_env_urls_are_still_allowed(self, repo, monkeypatch):
        monkeypatch.setenv("PRXREF_SPEC_SOURCES", "https://example.com/spec.md")
        path = write(repo, 'spec_sources = ["SPEC.md"]\n')
        assert load_config(config_file=path)["spec_sources"] == [
            "https://example.com/spec.md"
        ]


class TestPaths:
    @pytest.mark.parametrize(
        "key", ["review_rules", "prompts_dir", "ticket_context_file"]
    )
    def test_inside_path_resolves_against_the_file_dir(self, tmp_path, monkeypatch, key):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(tmp_path)
        path = write(root, f'{key} = "./rules/../rules/team.md"\n')
        assert read_config_file(path)[key] == os.path.join(
            os.path.realpath(root), "rules", "team.md"
        )

    def test_scoped_rules_entries_resolve(self, repo):
        path = write(repo, 'scoped_rules = ["rules/a.md", "rules"]\n')
        real = os.path.realpath(repo)
        assert read_config_file(path)["scoped_rules"] == [
            os.path.join(real, "rules", "a.md"),
            os.path.join(real, "rules"),
        ]

    def test_resolved_path_reaches_the_loaded_config(self, tmp_path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.chdir(other)
        path = write(root, 'review_rules = "rules.md"\n')
        cfg = load_config(config_file=path)
        assert cfg["review_rules"] == os.path.join(os.path.realpath(root), "rules.md")

    @pytest.mark.parametrize(
        ("value", "why"),
        [
            ("/etc/passwd", "an absolute path"),
            ("~/rules.md", "a home-directory path"),
            ("~", "a home-directory path"),
            ("../outside/rules.md", "it resolves outside the config file's directory"),
            ("rules/../../x.md", "it resolves outside the config file's directory"),
        ],
    )
    def test_escaping_path_is_rejected(self, repo, value, why):
        message = config_error(write(repo, f'review_rules = "{value}"\n'))
        assert message == (
            f"{repo / CONFIG_FILE_NAME}: 'review_rules' path {value!r} must stay "
            f"inside the repository ({why}); use a path relative to the config "
            f"file's directory; see {CONFIG_DOCS_URL}"
        )

    def test_symlink_escape_is_rejected(self, repo, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink("../outside", repo / "link")
        message = config_error(write(repo, 'review_rules = "link/rules.md"\n'))
        assert "'review_rules' path 'link/rules.md' must stay inside the repository" in message

    def test_symlink_inside_is_accepted(self, repo):
        (repo / "real").mkdir()
        os.symlink("real", repo / "alias")
        path = write(repo, 'prompts_dir = "alias"\n')
        assert read_config_file(path)["prompts_dir"] == os.path.join(
            os.path.realpath(repo), "real"
        )

    @pytest.mark.parametrize("key", ["scoped_rules", "spec_sources"])
    def test_list_entry_escape_is_rejected(self, repo, key):
        message = config_error(write(repo, f'{key} = ["ok.md", "../x.md"]\n'))
        assert f"{key!r} path '../x.md' must stay inside the repository" in message

    def test_globs_pass_through(self, repo):
        path = write(repo, 'size_ignore_globs = ["../**/*.lock", "/abs/*"]\n')
        assert read_config_file(path)["size_ignore_globs"] == ["../**/*.lock", "/abs/*"]


class TestFindConfigFile:
    def test_auto_discovers_in_cwd(self, repo):
        write(repo, "")
        assert find_config_file(explicit=None, environ={}) == Path.cwd() / CONFIG_FILE_NAME

    def test_explicit_cwd(self, tmp_path):
        write(tmp_path, "")
        assert find_config_file(explicit=None, cwd=tmp_path, environ={}) == (
            tmp_path / CONFIG_FILE_NAME
        )

    def test_absent_returns_none(self, tmp_path):
        assert find_config_file(explicit=None, cwd=tmp_path, environ={}) is None

    def test_does_not_walk_up(self, tmp_path):
        write(tmp_path, "")
        child = tmp_path / "child"
        child.mkdir()
        assert find_config_file(explicit=None, cwd=child, environ={}) is None

    def test_directory_named_like_the_file_is_ignored(self, tmp_path):
        (tmp_path / CONFIG_FILE_NAME).mkdir()
        assert find_config_file(explicit=None, cwd=tmp_path, environ={}) is None

    def test_explicit_path(self, tmp_path):
        write(tmp_path, "", name="team.toml")
        write(tmp_path, "")
        assert find_config_file(explicit="team.toml", cwd=tmp_path, environ={}) == (
            tmp_path / "team.toml"
        )

    def test_explicit_beats_env(self, tmp_path):
        write(tmp_path, "", name="a.toml")
        write(tmp_path, "", name="b.toml")
        env = {CONFIG_FILE_ENV: "b.toml"}
        assert find_config_file(explicit="a.toml", cwd=tmp_path, environ=env) == (
            tmp_path / "a.toml"
        )

    def test_env_path(self, tmp_path):
        write(tmp_path, "", name="b.toml")
        env = {CONFIG_FILE_ENV: "b.toml"}
        assert find_config_file(explicit=None, cwd=tmp_path, environ=env) == (
            tmp_path / "b.toml"
        )

    def test_env_defaults_to_os_environ(self, tmp_path, monkeypatch):
        write(tmp_path, "", name="b.toml")
        monkeypatch.setenv(CONFIG_FILE_ENV, "b.toml")
        assert find_config_file(explicit=None, cwd=tmp_path) == tmp_path / "b.toml"

    def test_absolute_explicit_path(self, tmp_path):
        path = write(tmp_path, "", name="abs.toml")
        other = tmp_path / "other"
        other.mkdir()
        assert find_config_file(explicit=str(path), cwd=other, environ={}) == path

    @pytest.mark.parametrize("off", ["off", "OFF", "Off"])
    def test_off_disables(self, tmp_path, off):
        write(tmp_path, "")
        assert find_config_file(explicit=off, cwd=tmp_path, environ={}) is None
        env = {CONFIG_FILE_ENV: off}
        assert find_config_file(explicit=None, cwd=tmp_path, environ=env) is None

    def test_explicit_off_beats_env_path(self, tmp_path):
        write(tmp_path, "", name="b.toml")
        env = {CONFIG_FILE_ENV: "b.toml"}
        assert find_config_file(explicit="off", cwd=tmp_path, environ=env) is None

    def test_empty_values_read_as_unset(self, tmp_path):
        write(tmp_path, "")
        env = {CONFIG_FILE_ENV: " "}
        assert find_config_file(explicit="", cwd=tmp_path, environ=env) == (
            tmp_path / CONFIG_FILE_NAME
        )

    def test_missing_explicit_path(self, tmp_path):
        with pytest.raises(ConfigError, match=r"^--config: config file not found: nope\.toml$"):
            find_config_file(explicit="nope.toml", cwd=tmp_path, environ={})

    def test_missing_env_path(self, tmp_path):
        env = {CONFIG_FILE_ENV: "nope.toml"}
        with pytest.raises(
            ConfigError, match=r"^PRXREF_CONFIG_FILE: config file not found: nope\.toml$"
        ):
            find_config_file(explicit=None, cwd=tmp_path, environ=env)

    def test_explicit_directory_is_not_found(self, tmp_path):
        (tmp_path / "dir").mkdir()
        with pytest.raises(ConfigError, match=r"^--config: config file not found"):
            find_config_file(explicit="dir", cwd=tmp_path, environ={})


class TestSuggestionsBump:
    def test_file_budget_counts_as_supplied(self, repo):
        path = write(repo, 'suggestions = "on"\nllm_max_tokens = 100\n')
        assert load_config(config_file=path)["llm_max_tokens"] == 100

    def test_file_suggestions_without_budget_bumps(self, repo):
        path = write(repo, 'suggestions = "on"\n')
        assert load_config(config_file=path)["llm_max_tokens"] == SUGGESTIONS_MAX_TOKENS


class TestInvariant:
    ENV = {
        "PRXREF_MAX_CHUNKS": "6",
        "PRXREF_LLM_MODELS": "a, b",
        "PRXREF_POST_MODE": "summary",
    }

    def test_no_file_matches_the_env_only_config(self, repo, monkeypatch):
        for name, value in self.ENV.items():
            monkeypatch.setenv(name, value)
        expected = {
            key: list(value) if isinstance(value, list) else value
            for key, value in config._DEFAULTS.items()
        }
        expected.update(
            max_chunks=6, llm_models=["a", "b"], post_mode="summary", price_table={}
        )
        assert load_config() == expected
        assert load_config(config_file=None) == expected

    def test_load_config_never_discovers_a_file(self, repo):
        write(repo, "max_chunks = 3\n")
        assert load_config()["max_chunks"] == 8
