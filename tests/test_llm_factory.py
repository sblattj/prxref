"""Tests for prxref.llm_backends.create_llm_client: backend vocabulary (#66 D11),
the openai-compat-only base URL (#61), the CLI-backend wiring (#66), the
InvokeResult cost fields (#67), and the llm_cli_backends entry-point contract.

The CLI-backend tests assert only what holds both before and after the real
claude-cli / kiro-cli clients land: the factory wiring is observed through a
recorder, and every "cannot run" case points ``PRXREF_LLM_CLI_PATH`` at a path
that does not exist, so nothing here depends on what is installed on ``PATH``.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import logging
import pathlib
import shutil
import subprocess
import sys
import types
from types import SimpleNamespace

import pytest

import prxref.llm_backends
import prxref.llm_cli_backends
from prxref import cli
from prxref import orchestrator as real_orchestrator
from prxref.forges.base import PRRef
from prxref.llm import ConfigError, InvokeResult
from prxref.llm_backends import (
    BACKENDS,
    CLI_BACKENDS,
    DEFAULT_CLI_CONCURRENCY,
    OPENAI_COMPAT_BACKENDS,
    LiteLLMClient,
    OpenAICompatClient,
    create_llm_client,
)

_IGNORED_LINE = "PRXREF_LLM_BASE_URL is set but not used by the {} backend; ignoring it"
_LOGGER = "prxref.llm_backends"


def _fake_litellm(monkeypatch) -> list[dict]:
    """Install a fake ``litellm`` module; returns the kwargs of every completion call."""
    captured: list[dict] = []

    def completion(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=None,
            model=kwargs["model"],
        )

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(completion=completion))
    return captured


def _ignored_lines(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER and "is set but not used by the" in r.getMessage()
    ]


@pytest.fixture
def recorded_cli_builds(monkeypatch):
    """Replace ``build_cli_client`` with a recorder; returns the recorded calls.

    The factory imports it lazily from ``prxref.llm_cli_backends`` at call time,
    so patching the module attribute is exactly what the factory sees.
    """
    calls: list[tuple[tuple, dict]] = []
    sentinel = object()

    def fake_build(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(prxref.llm_cli_backends, "build_cli_client", fake_build)
    monkeypatch.delenv("PRXREF_LLM_CLI_PATH", raising=False)
    monkeypatch.delenv("PRXREF_LLM_CLI_CONCURRENCY", raising=False)
    return SimpleNamespace(calls=calls, sentinel=sentinel)


class TestBackendVocabulary:
    def test_the_constants_name_the_six_backends(self):
        assert OPENAI_COMPAT_BACKENDS == ("openai-compat", "ferry", "http")
        assert CLI_BACKENDS == ("claude-cli", "kiro-cli")
        assert BACKENDS == (*OPENAI_COMPAT_BACKENDS, "litellm", *CLI_BACKENDS)
        assert len(set(BACKENDS)) == 6
        assert DEFAULT_CLI_CONCURRENCY == 2

    def test_unknown_backend_is_named_before_missing_models(self, monkeypatch):
        """A typo is reported as itself, not as the missing endpoint or chain it
        would otherwise trip over first."""
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "claude_cli")
        monkeypatch.delenv("PRXREF_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("PRXREF_LLM_MODELS", raising=False)
        with pytest.raises(ConfigError) as exc:
            create_llm_client()
        message = str(exc.value)
        assert message.startswith("PRXREF_LLM_BACKEND:")
        assert "'claude_cli'" in message
        assert "PRXREF_LLM_MODELS" not in message
        assert "PRXREF_LLM_BASE_URL" not in message

    def test_the_error_lists_every_accepted_name(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "claude")
        with pytest.raises(ConfigError) as exc:
            create_llm_client()
        for name in BACKENDS:
            assert name in str(exc.value)
        assert "case-insensitive" in str(exc.value)

    def test_an_unknown_backend_from_cfg_is_a_config_error_too(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "litellm")
        with pytest.raises(ConfigError, match=r"^PRXREF_LLM_BACKEND: must be one of .*got 'bedrock'$"):
            create_llm_client({"llm_backend": "bedrock"})

    @pytest.mark.parametrize(
        ("raw", "cls"),
        [(" LiteLLM ", LiteLLMClient), ("FERRY", OpenAICompatClient), ("Http", OpenAICompatClient)],
    )
    def test_backend_name_is_case_insensitive(self, monkeypatch, raw, cls):
        _fake_litellm(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_BACKEND", raw)
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.test/v1")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "a")
        assert isinstance(create_llm_client(), cls)

    def test_a_blank_backend_means_the_default(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "   ")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.test/v1")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "a")
        assert isinstance(create_llm_client(), OpenAICompatClient)


class TestBaseUrlIsOpenAICompatOnly:
    """#61: PRXREF_LLM_BASE_URL is required by openai-compat/ferry/http only."""

    def test_unset_endpoint_names_base_url_before_models_when_both_missing(self, monkeypatch):
        """Precedence guard: gating the check must not move it after the models check."""
        monkeypatch.delenv("PRXREF_LLM_BACKEND", raising=False)
        monkeypatch.delenv("PRXREF_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("PRXREF_LLM_MODELS", raising=False)
        with pytest.raises(ConfigError) as exc:
            create_llm_client()
        assert "PRXREF_LLM_BASE_URL" in str(exc.value)
        assert "PRXREF_LLM_MODELS" not in str(exc.value)

    @pytest.mark.parametrize("backend", OPENAI_COMPAT_BACKENDS)
    @pytest.mark.parametrize("base_url", [None, "   "])
    def test_openai_compat_backend_still_requires_base_url(self, monkeypatch, backend, base_url):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", backend)
        monkeypatch.setenv("PRXREF_LLM_MODELS", "a")
        if base_url is None:
            monkeypatch.delenv("PRXREF_LLM_BASE_URL", raising=False)
        else:
            monkeypatch.setenv("PRXREF_LLM_BASE_URL", base_url)
        with pytest.raises(ConfigError, match="PRXREF_LLM_BASE_URL"):
            create_llm_client()

    def test_litellm_backend_does_not_require_base_url(self, monkeypatch):
        """The issue's own repro: litellm, a model chain, and no endpoint."""
        _fake_litellm(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "litellm")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "openrouter/openai/gpt-oss-20b")
        monkeypatch.delenv("PRXREF_LLM_BASE_URL", raising=False)
        client = create_llm_client()
        assert isinstance(client, LiteLLMClient)
        assert client.models == ["openrouter/openai/gpt-oss-20b"]

    def test_litellm_builds_with_no_base_url_and_no_models_still_raises(self, monkeypatch):
        _fake_litellm(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "litellm")
        monkeypatch.delenv("PRXREF_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("PRXREF_LLM_MODELS", raising=False)
        with pytest.raises(ConfigError) as exc:
            create_llm_client()
        assert "PRXREF_LLM_MODELS" in str(exc.value)
        assert "PRXREF_LLM_BASE_URL" not in str(exc.value)

    def test_litellm_ignores_a_set_base_url(self, monkeypatch, caplog):
        """Never forwarded: every pre-0.14 litellm deployment carries a dummy URL."""
        captured = _fake_litellm(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "litellm")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "m1,m2")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "http://127.0.0.1:9/v1")
        monkeypatch.setenv("PRXREF_LLM_API_KEY", "not-a-real-key")
        caplog.set_level(logging.INFO, logger=_LOGGER)
        create_llm_client().invoke("sys", "usr")
        assert _ignored_lines(caplog) == [_IGNORED_LINE.format("litellm")]
        kwargs = captured[0]
        assert kwargs["model"] == "m1"
        assert kwargs["fallbacks"] == ["m2"]
        for leaked in ("api_base", "base_url", "api_key"):
            assert leaked not in kwargs
        assert "127.0.0.1" not in json.dumps(kwargs, default=str)

    def test_a_base_url_from_cfg_is_ignored_the_same_way(self, monkeypatch, caplog):
        captured = _fake_litellm(monkeypatch)
        caplog.set_level(logging.INFO, logger=_LOGGER)
        create_llm_client(
            {"llm_backend": "litellm", "llm_models": ["m1"], "llm_base_url": "https://gw.test/v1"}
        ).invoke("sys", "usr")
        assert _ignored_lines(caplog) == [_IGNORED_LINE.format("litellm")]
        assert "api_base" not in captured[0]

    def test_litellm_without_a_base_url_logs_nothing_about_it(self, monkeypatch, caplog):
        _fake_litellm(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "litellm")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "m1")
        monkeypatch.delenv("PRXREF_LLM_BASE_URL", raising=False)
        caplog.set_level(logging.INFO, logger=_LOGGER)
        create_llm_client()
        assert _ignored_lines(caplog) == []

    @pytest.mark.parametrize("backend", OPENAI_COMPAT_BACKENDS)
    def test_openai_compat_uses_the_base_url_and_never_logs_it_as_ignored(
        self, monkeypatch, caplog, backend
    ):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", backend)
        monkeypatch.setenv("PRXREF_LLM_MODELS", "m1")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.test/v1/")
        caplog.set_level(logging.INFO, logger=_LOGGER)
        client = create_llm_client()
        assert client.base_url == "https://llm.test/v1"
        assert _ignored_lines(caplog) == []


class TestCliBackendWiring:
    """#66: the factory side of claude-cli / kiro-cli, observed through a recorder."""

    @pytest.mark.parametrize("backend", CLI_BACKENDS)
    def test_a_cli_backend_builds_without_base_url_and_gets_the_defaults(
        self, monkeypatch, recorded_cli_builds, backend
    ):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", backend)
        monkeypatch.setenv("PRXREF_LLM_MODELS", "sonnet, haiku")
        monkeypatch.delenv("PRXREF_LLM_BASE_URL", raising=False)
        assert create_llm_client() is recorded_cli_builds.sentinel
        [(args, kwargs)] = recorded_cli_builds.calls
        assert args == (backend,)
        assert kwargs == {
            "models": ["sonnet", "haiku"],
            "default_timeout": 45.0,
            "reasoning_effort": None,
            "cli_path": "",
            "concurrency": DEFAULT_CLI_CONCURRENCY,
        }

    def test_env_settings_reach_the_builder(self, monkeypatch, recorded_cli_builds):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "claude-cli")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "sonnet")
        monkeypatch.setenv("PRXREF_LLM_TIMEOUT", "180")
        monkeypatch.setenv("PRXREF_LLM_REASONING_EFFORT", "low")
        monkeypatch.setenv("PRXREF_LLM_CLI_PATH", "~/bin/claude")
        monkeypatch.setenv("PRXREF_LLM_CLI_CONCURRENCY", "3")
        create_llm_client()
        [(_, kwargs)] = recorded_cli_builds.calls
        assert kwargs["default_timeout"] == 180.0
        assert kwargs["reasoning_effort"] == "low"
        assert kwargs["cli_path"] == "~/bin/claude"
        assert kwargs["concurrency"] == 3

    def test_cfg_settings_win_over_env(self, monkeypatch, recorded_cli_builds):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "litellm")
        monkeypatch.setenv("PRXREF_LLM_CLI_PATH", "/env/claude")
        monkeypatch.setenv("PRXREF_LLM_CLI_CONCURRENCY", "3")
        create_llm_client({
            "llm_backend": "kiro-cli",
            "llm_models": ["claude-haiku-4.5"],
            "llm_cli_path": "/cfg/kiro-cli",
            "llm_cli_concurrency": 5,
        })
        [(args, kwargs)] = recorded_cli_builds.calls
        assert args == ("kiro-cli",)
        assert kwargs["cli_path"] == "/cfg/kiro-cli"
        assert kwargs["concurrency"] == 5

    def test_backend_name_is_case_insensitive_for_cli_backends(
        self, monkeypatch, recorded_cli_builds
    ):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", " Claude-CLI ")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "sonnet")
        create_llm_client()
        assert recorded_cli_builds.calls[0][0] == ("claude-cli",)

    def test_a_cli_backend_still_requires_models(self, monkeypatch, recorded_cli_builds):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "claude-cli")
        monkeypatch.delenv("PRXREF_LLM_MODELS", raising=False)
        with pytest.raises(ConfigError, match="PRXREF_LLM_MODELS"):
            create_llm_client()
        assert recorded_cli_builds.calls == []

    @pytest.mark.parametrize("raw", ["0", "-1", "two", "1.5"])
    def test_a_bad_cli_concurrency_names_the_variable(self, monkeypatch, recorded_cli_builds, raw):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "claude-cli")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "sonnet")
        monkeypatch.setenv("PRXREF_LLM_CLI_CONCURRENCY", raw)
        with pytest.raises(ConfigError, match="PRXREF_LLM_CLI_CONCURRENCY"):
            create_llm_client()
        assert recorded_cli_builds.calls == []

    def test_a_blank_cli_concurrency_means_the_default(self, monkeypatch, recorded_cli_builds):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "kiro-cli")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "claude-haiku-4.5")
        monkeypatch.setenv("PRXREF_LLM_CLI_CONCURRENCY", "  ")
        create_llm_client()
        assert recorded_cli_builds.calls[0][1]["concurrency"] == DEFAULT_CLI_CONCURRENCY

    def test_cli_concurrency_is_not_read_by_the_http_backends(self, monkeypatch):
        """Only a CLI backend parses it; an HTTP run is not failed by a knob it never uses."""
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.test/v1")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "a")
        monkeypatch.setenv("PRXREF_LLM_CLI_CONCURRENCY", "zero")
        assert isinstance(create_llm_client(), OpenAICompatClient)

    @pytest.mark.parametrize("backend", CLI_BACKENDS)
    def test_a_set_base_url_is_ignored_with_an_info_line(
        self, monkeypatch, caplog, recorded_cli_builds, backend
    ):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", backend)
        monkeypatch.setenv("PRXREF_LLM_MODELS", "m")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.test/v1")
        caplog.set_level(logging.INFO, logger=_LOGGER)
        create_llm_client()
        assert _ignored_lines(caplog) == [_IGNORED_LINE.format(backend)]
        assert "https://llm.test/v1" not in repr(recorded_cli_builds.calls)

    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            ({"PRXREF_LLM_TEMPERATURE": "0.2"}, "PRXREF_LLM_TEMPERATURE is not applied by claude-cli"),
            ({"PRXREF_LLM_SEED": "0"}, "PRXREF_LLM_SEED is not applied by claude-cli"),
            (
                {"PRXREF_LLM_TEMPERATURE": "0.2", "PRXREF_LLM_SEED": "7"},
                "PRXREF_LLM_TEMPERATURE / PRXREF_LLM_SEED are not applied by claude-cli",
            ),
        ],
    )
    def test_temperature_or_seed_set_with_a_cli_backend_warns_not_applied(
        self, monkeypatch, caplog, recorded_cli_builds, env, expected
    ):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "claude-cli")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "sonnet")
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        caplog.set_level(logging.INFO, logger=_LOGGER)
        create_llm_client()
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == [f"{expected} (the CLI has no such option)"]
        [(_, kwargs)] = recorded_cli_builds.calls
        assert "temperature" not in kwargs
        assert "seed" not in kwargs

    def test_a_seed_from_cfg_warns_too(self, monkeypatch, caplog, recorded_cli_builds):
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        create_llm_client({"llm_backend": "kiro-cli", "llm_models": ["m"], "llm_seed": 3})
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == ["PRXREF_LLM_SEED is not applied by kiro-cli (the CLI has no such option)"]

    def test_no_warning_when_temperature_and_seed_are_unset(
        self, monkeypatch, caplog, recorded_cli_builds
    ):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "claude-cli")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "sonnet")
        monkeypatch.setenv("PRXREF_LLM_TEMPERATURE", "   ")
        caplog.set_level(logging.INFO, logger=_LOGGER)
        create_llm_client()
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_a_malformed_temperature_still_exits_2_on_a_cli_backend(
        self, monkeypatch, recorded_cli_builds
    ):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "claude-cli")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "sonnet")
        monkeypatch.setenv("PRXREF_LLM_TEMPERATURE", "hot")
        with pytest.raises(ConfigError, match="PRXREF_LLM_TEMPERATURE"):
            create_llm_client()
        assert recorded_cli_builds.calls == []


class TestCliBackendModuleContract:
    """The llm_cli_backends entry points every later seat codes against."""

    def test_resolve_cli_binary_signature(self):
        sig = inspect.signature(prxref.llm_cli_backends.resolve_cli_binary)
        assert list(sig.parameters) == ["backend", "cli_path", "which"]
        assert sig.parameters["which"].kind is inspect.Parameter.KEYWORD_ONLY
        assert sig.parameters["which"].default is shutil.which

    def test_build_cli_client_signature(self):
        sig = inspect.signature(prxref.llm_cli_backends.build_cli_client)
        assert list(sig.parameters) == [
            "backend", "models", "default_timeout", "reasoning_effort",
            "cli_path", "concurrency", "which", "runner",
        ]
        params = list(sig.parameters.values())
        assert params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params[1:])
        assert sig.parameters["which"].default is shutil.which
        assert sig.parameters["runner"].default is subprocess.Popen

    @pytest.mark.parametrize("backend", CLI_BACKENDS)
    def test_an_unresolvable_cli_is_a_config_error_naming_a_variable(self, tmp_path, backend):
        with pytest.raises(ConfigError) as exc:
            prxref.llm_cli_backends.resolve_cli_binary(
                backend, str(tmp_path / "missing"), which=lambda name: None
            )
        assert "PRXREF_LLM_" in str(exc.value)

    @pytest.mark.parametrize("backend", CLI_BACKENDS)
    def test_a_client_that_cannot_run_fails_closed_before_launching(self, tmp_path, backend):
        launched: list[object] = []

        def runner(*args, **kwargs):
            launched.append(args)
            raise AssertionError("no process may start when the CLI cannot be resolved")

        with pytest.raises(ConfigError) as exc:
            prxref.llm_cli_backends.build_cli_client(
                backend,
                models=["m"],
                default_timeout=45.0,
                reasoning_effort=None,
                cli_path=str(tmp_path / "missing"),
                concurrency=2,
                which=lambda name: None,
                runner=runner,
            )
        assert "PRXREF_LLM_" in str(exc.value)
        assert launched == []

    def test_the_module_is_stdlib_only(self):
        source = pathlib.Path(inspect.getfile(prxref.llm_cli_backends)).read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            for root in roots:
                assert root in sys.stdlib_module_names or root == "__future__", root

    def test_the_module_names_no_private_host_or_lane(self):
        source = pathlib.Path(inspect.getfile(prxref.llm_cli_backends)).read_text()
        for token in ("8090", "flash,orch", "llm-ferry"):
            assert token not in source, f"private default {token!r} in source"

    def test_the_http_backends_do_not_import_it(self):
        source = pathlib.Path(inspect.getfile(prxref.llm_backends)).read_text()
        top_level = [
            node for node in ast.parse(source).body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        assert not any(
            isinstance(node, ast.ImportFrom) and node.module == "llm_cli_backends"
            for node in top_level
        )


class TestInvokeResultCostFields:
    """#67: the two fields every backend fills in, defined once."""

    def test_defaults_mean_not_reported(self):
        result = InvokeResult(text="ok")
        assert result.cost_usd is None
        assert result.cost_source == ""

    def test_the_fields_follow_finish_reason(self):
        names = [f.name for f in dataclasses.fields(InvokeResult)]
        assert names[-3:] == ["finish_reason", "cost_usd", "cost_source"]

    def test_existing_positional_constructions_keep_working(self):
        result = InvokeResult("t", 1, 2, "m", "b", 3, "stop")
        assert (result.finish_reason, result.cost_usd, result.cost_source) == ("stop", None, "")

    def test_a_reported_cost_is_carried(self):
        result = InvokeResult(text="ok", cost_usd=0.0021, cost_source="usage.cost")
        assert (result.cost_usd, result.cost_source) == (0.0021, "usage.cost")


class TestExitCodesThroughTheCli:
    """The factory's ConfigErrors surface as exit 2 through the real ``review`` entry point."""

    URL = "https://github.com/org/repo/pull/7"

    @pytest.fixture
    def runtime(self, monkeypatch):
        ref = PRRef(forge="github", host="github.com", owner="org", repo="repo", number=7, url=self.URL)
        orchestrate_calls: list[dict] = []

        def fake_orchestrate(**kwargs):
            orchestrate_calls.append(kwargs)
            return {
                "verdict": "commented",
                "findings_active": [],
                "findings_dropped": [],
                "input_tokens": 0,
                "output_tokens": 0,
            }

        monkeypatch.setattr(cli, "detect_forge", lambda url: ref)
        monkeypatch.setattr(cli, "make_forge", lambda r: object())
        monkeypatch.setattr(real_orchestrator, "orchestrate_review", fake_orchestrate)
        monkeypatch.delenv("PRXREF_LLM_CLI_PATH", raising=False)
        monkeypatch.delenv("PRXREF_LLM_CLI_CONCURRENCY", raising=False)
        return SimpleNamespace(orchestrate_calls=orchestrate_calls)

    def _review(self) -> int:
        return cli.main(["review", "--pr-url", self.URL, "--no-post", "--format", "json"])

    def test_review_exits_2_on_unknown_backend(self, monkeypatch, capsys, runtime):
        """Before 0.14 this was an LLMError, i.e. "review failed" and exit 0."""
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "claude")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.test/v1")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "m")
        assert self._review() == 2
        err = capsys.readouterr().err
        assert "configuration error: PRXREF_LLM_BACKEND: must be one of" in err
        assert runtime.orchestrate_calls == []

    @pytest.mark.parametrize("backend", CLI_BACKENDS)
    def test_review_exits_2_when_a_cli_backend_cannot_run(
        self, monkeypatch, capsys, tmp_path, runtime, backend
    ):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", backend)
        monkeypatch.setenv("PRXREF_LLM_MODELS", "sonnet")
        monkeypatch.setenv("PRXREF_LLM_CLI_PATH", str(tmp_path / "missing-cli"))
        assert self._review() == 2
        err = capsys.readouterr().err
        assert "configuration error: PRXREF_LLM_" in err
        assert runtime.orchestrate_calls == []

    def test_review_on_litellm_without_base_url_reaches_the_orchestrator(
        self, monkeypatch, capsys, runtime
    ):
        """#61 through the real entry point; also the control for the two exit-2 cases."""
        _fake_litellm(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "litellm")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "openrouter/openai/gpt-oss-20b")
        monkeypatch.delenv("PRXREF_LLM_BASE_URL", raising=False)
        assert self._review() == 0
        [call] = runtime.orchestrate_calls
        assert isinstance(call["llm"], LiteLLMClient)
        assert "configuration error" not in capsys.readouterr().err
