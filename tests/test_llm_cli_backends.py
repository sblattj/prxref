"""Tests for prxref.llm_cli_backends: the shared CLI chain and the claude-cli client (#66).

Every process here is a scripted fake handed to the client as ``runner``;
nothing spawns a real CLI. ``os.killpg`` is replaced for the whole module, so
a timeout test can never signal a real process group.

The claude stream fixtures are built by hand from the fields the client
reads, with the values OBSERVED in the design probes (``clean``,
``noskills``, ``trunc`` and ``badmodel``) and a zeroed session id. Nothing
else from the probe streams is copied.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping
from types import SimpleNamespace

import pytest

import prxref.llm_cli_backends as clib
from prxref import cli
from prxref import orchestrator as real_orchestrator
from prxref.forges.base import PRRef
from prxref.llm import ConfigError, InvokeResult
from prxref.llm_backends import CLI_BACKENDS, LLMError, create_llm_client
from prxref.llm_cli_backends import (
    CLAUDE_ENV_DENYLIST,
    DEFAULT_BINARIES,
    JSON_ONLY_INSTRUCTION,
    ClaudeCLIClient,
    build_cli_client,
    resolve_cli_binary,
)

LOGGER = "prxref.llm_cli_backends"
BINARY = "/opt/example/bin/claude"
ZERO_ID = "00000000-0000-0000-0000-000000000000"
FAKE_PID = 424242


def _init(**overrides) -> dict:
    event = {
        "type": "system",
        "subtype": "init",
        "model": "claude-sonnet-5",
        "apiKeySource": "none",
        "tools": [],
        "mcp_servers": [],
        "session_id": ZERO_ID,
    }
    event.update(overrides)
    return event


def _rate_limit(status: str = "allowed") -> dict:
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": status,
            "rateLimitType": "seven_day",
            "utilization": 0.82,
            "isUsingOverage": False,
            "resetsAt": 1790434800,
        },
        "session_id": ZERO_ID,
    }


def _assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}, "session_id": ZERO_ID}


def _result(**overrides) -> dict:
    """The ``clean`` probe's result event (E1), with ``overrides`` applied."""
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": '{"ok": true, "n": 3}',
        "stop_reason": "end_turn",
        "terminal_reason": "completed",
        "api_error_status": None,
        "num_turns": 1,
        "duration_ms": 1395,
        "total_cost_usd": 0.001276,
        "usage": {
            "input_tokens": 563,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 15,
            "output_tokens_details": {"thinking_tokens": 0},
        },
        "modelUsage": {
            "claude-sonnet-5": {"inputTokens": 563, "outputTokens": 15, "costUSD": 0.001276, "costBasis": "list"},
        },
        "session_id": ZERO_ID,
    }
    event.update(overrides)
    return event


def _stream(*events: dict) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


CLEAN_RESULT = _result()
CLEAN_STREAM = _stream(_init(), _assistant('{"ok": true, "n": 3}'), _rate_limit(), CLEAN_RESULT)

NOSKILLS_STREAM = _stream(
    _init(),
    _rate_limit(),
    _result(
        result='{"ok":true,"n":3}',
        total_cost_usd=0.002392,
        usage={
            "input_tokens": 2,
            "cache_creation_input_tokens": 562,
            "cache_read_input_tokens": 0,
            "output_tokens": 14,
        },
        modelUsage={"claude-sonnet-5": {"inputTokens": 2, "outputTokens": 14, "costBasis": "list"}},
    ),
)

TRUNC_TEXT = (
    "API Error: Claude's response exceeded the 24 output token maximum. To configure this behavior, "
    "set the CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable."
)
TRUNC_STREAM = _stream(
    _init(),
    _rate_limit(),
    _result(
        is_error=True,
        result=TRUNC_TEXT,
        stop_reason="stop_sequence",
        terminal_reason="api_error",
        num_turns=4,
        total_cost_usd=0.0096972,
        usage={
            "input_tokens": 14,
            "cache_creation_input_tokens": 2149,
            "cache_read_input_tokens": 566,
            "output_tokens": 96,
        },
        modelUsage={"claude-sonnet-5": {"inputTokens": 14, "outputTokens": 96, "costBasis": "list"}},
    ),
)

BADMODEL_TEXT = (
    "There's an issue with the selected model (claude-nonexistent-9). It may not exist or you may not "
    "have access to it. Run --model to pick a different model."
)
BADMODEL_STREAM = _stream(
    _init(model="claude-nonexistent-9"),
    _result(
        is_error=True,
        result=BADMODEL_TEXT,
        stop_reason="stop_sequence",
        terminal_reason="api_error",
        api_error_status=404,
        total_cost_usd=0,
        usage={"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0},
        modelUsage={},
    ),
)
BADMODEL_STDERR = '[claude-code:unrecognized_model] {"model":"claude-nonexistent-9","query_source":"sdk"}\n'


@dataclasses.dataclass
class Script:
    """What one fake process does: print, exit, hang past its deadline, block, or fail to launch."""

    stdout: str = ""
    stderr: str = ""
    rc: int = 0
    hang: bool = False
    launch_error: OSError | None = None
    communicate_error: OSError | None = None
    gate: threading.Event | None = None


CLEAN = Script(stdout=CLEAN_STREAM)
TRUNC = Script(stdout=TRUNC_STREAM, rc=1)
BADMODEL = Script(stdout=BADMODEL_STREAM, stderr=BADMODEL_STDERR, rc=1)


@dataclasses.dataclass
class Launch:
    argv: list[str]
    kwargs: dict
    cwd_listing: list[str]
    prompt_path: str | None
    prompt: str | None
    stdin: str | None = None
    timeouts: list = dataclasses.field(default_factory=list)
    killed: bool = False


def _prompt_path(argv: list[str]) -> str | None:
    if "--system-prompt-file" not in argv:
        return None
    return argv[argv.index("--system-prompt-file") + 1]


class FakeProc:
    def __init__(self, runner: FakeRunner, script: Script, launch: Launch):
        self._runner, self._script, self.launch = runner, script, launch
        self.pid = FAKE_PID
        self.returncode: int | None = None

    def communicate(self, input=None, timeout=None):
        launch, script = self.launch, self._script
        if input is not None:
            launch.stdin = input
        launch.timeouts.append(timeout)
        if script.communicate_error is not None:
            raise script.communicate_error
        if script.hang:
            if len(launch.timeouts) == 1:
                raise subprocess.TimeoutExpired(launch.argv, timeout)
            self.returncode = -signal.SIGKILL
            return "", ""
        if script.gate is not None:
            self._runner.enter()
            try:
                script.gate.wait(timeout=10)
            finally:
                self._runner.leave()
        self.returncode = script.rc
        return script.stdout, script.stderr

    def kill(self):
        self.launch.killed = True


class FakeRunner:
    """A ``subprocess.Popen`` stand-in: records every launch and plays scripts in order.

    The last script repeats once the others are used up. The working
    directory's listing and the system prompt file are read at launch time,
    because the client deletes its temporary root as soon as the call returns.
    """

    def __init__(self, *scripts: Script):
        self.scripts = list(scripts) or [CLEAN]
        self.launches: list[Launch] = []
        self.procs: list[FakeProc] = []
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def __call__(self, argv, **kwargs):
        with self._lock:
            script = self.scripts.pop(0) if len(self.scripts) > 1 else self.scripts[0]
            path = _prompt_path(argv)
            prompt = None
            if path is not None and os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    prompt = fh.read()
            launch = Launch(
                argv=list(argv),
                kwargs=kwargs,
                cwd_listing=sorted(os.listdir(kwargs["cwd"])),
                prompt_path=path,
                prompt=prompt,
            )
            self.launches.append(launch)
        if script.launch_error is not None:
            raise script.launch_error
        proc = FakeProc(self, script, launch)
        self.procs.append(proc)
        return proc

    def enter(self):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def leave(self):
        with self._lock:
            self.active -= 1

    def models(self) -> list[str]:
        return [launch.argv[launch.argv.index("--model") + 1] for launch in self.launches]


@pytest.fixture(autouse=True)
def killpg_calls(monkeypatch) -> list[tuple[int, int]]:
    """Record every process-group kill instead of sending it: the fake pid must never be signalled."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(clib.os, "killpg", lambda pid, sig: calls.append((pid, sig)), raising=False)
    return calls


def _client(runner: FakeRunner, *, models=("sonnet",), **kwargs) -> ClaudeCLIClient:
    kwargs.setdefault("default_timeout", 30.0)
    return ClaudeCLIClient(binary=BINARY, models=list(models), runner=runner, **kwargs)


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


def _wait_until(predicate, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.005)


class TestClaudeProcess:
    """How one claude process is launched: argv, stdin, prompt file, cwd, environment."""

    def test_claude_argv_is_a_list_with_the_isolation_flags(self):
        runner = FakeRunner()
        _client(runner).invoke("sys", "usr")
        [launch] = runner.launches
        assert launch.argv == [
            BINARY, "-p",
            "--model", "sonnet",
            "--output-format", "stream-json", "--verbose",
            "--tools", "",
            "--setting-sources", "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--max-turns", "1",
            "--system-prompt-file", launch.prompt_path,
        ]
        assert not launch.kwargs.get("shell")
        assert launch.kwargs["start_new_session"] is (os.name == "posix")
        for stream in ("stdin", "stdout", "stderr"):
            assert launch.kwargs[stream] is subprocess.PIPE
        assert launch.kwargs["text"] is True
        assert launch.kwargs["encoding"] == "utf-8"

    def test_claude_user_message_goes_to_stdin_not_argv(self):
        runner = FakeRunner()
        user = "Review this diff: +secret_sauce = 1"
        _client(runner).invoke("sys", user)
        [launch] = runner.launches
        assert launch.stdin == user
        assert not any("secret_sauce" in arg for arg in launch.argv)

    @pytest.mark.parametrize("json_mode", [True, False])
    def test_claude_system_prompt_file_carries_json_only_suffix_in_json_mode(self, json_mode):
        runner = FakeRunner()
        _client(runner).invoke("You review diffs.", "usr", json_mode=json_mode)
        [launch] = runner.launches
        expected = "You review diffs." + (JSON_ONLY_INSTRUCTION if json_mode else "")
        assert launch.prompt == expected
        assert "You review diffs." not in " ".join(launch.argv)

    def test_claude_cwd_is_empty_fresh_and_removed(self):
        runner = FakeRunner()
        client = _client(runner)
        client.invoke("sys", "usr")
        client.invoke("sys", "usr")
        first, second = runner.launches
        assert first.cwd_listing == [] and second.cwd_listing == []
        assert first.kwargs["cwd"] != second.kwargs["cwd"]
        for launch in runner.launches:
            cwd = launch.kwargs["cwd"]
            assert os.path.isabs(cwd)
            assert not launch.prompt_path.startswith(cwd + os.sep)
            assert not os.path.exists(cwd)
            assert not os.path.exists(launch.prompt_path)

    def test_the_temporary_root_is_removed_when_the_launch_fails(self):
        runner = FakeRunner(Script(launch_error=FileNotFoundError(2, "No such file or directory")))
        with pytest.raises(LLMError):
            _client(runner).invoke("sys", "usr")
        [launch] = runner.launches
        assert not os.path.exists(os.path.dirname(launch.prompt_path))

    def test_claude_child_env_drops_denylist_and_keeps_oauth_token(self, monkeypatch):
        for name in CLAUDE_ENV_DENYLIST:
            monkeypatch.setenv(name, "set-by-test")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-set-by-test")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        before = dict(os.environ)
        runner = FakeRunner()
        _client(runner).invoke("sys", "usr")
        env = runner.launches[0].kwargs["env"]
        assert not set(CLAUDE_ENV_DENYLIST) & set(env)
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-set-by-test"
        assert env["PATH"] == "/usr/bin:/bin"
        assert dict(os.environ) == before

    def test_the_denylist_is_the_eight_credential_routing_names(self):
        assert CLAUDE_ENV_DENYLIST == (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_PROFILE",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
            "CLAUDE_CODE_SIMPLE",
        )

    def test_claude_env_scrub_never_reads_a_denylisted_value(self):
        class RecordingEnviron(Mapping):
            def __init__(self, data):
                self._data = dict(data)
                self.looked_up: list[str] = []

            def __getitem__(self, key):
                self.looked_up.append(key)
                return self._data[key]

            def __iter__(self) -> Iterator[str]:
                return iter(self._data)

            def __len__(self) -> int:
                return len(self._data)

        environ = RecordingEnviron({**dict.fromkeys(CLAUDE_ENV_DENYLIST, "x"), "HOME": "/home/example", "LANG": "C"})
        env = _client(FakeRunner())._child_env(environ)
        assert env == {"HOME": "/home/example", "LANG": "C"}
        assert not set(environ.looked_up) & set(CLAUDE_ENV_DENYLIST)

    @pytest.mark.parametrize(("effort", "tail"), [("low", ["--effort", "low"]), (None, None), ("", None)])
    def test_claude_effort_flag_only_when_reasoning_effort_set(self, effort, tail):
        runner = FakeRunner()
        _client(runner, reasoning_effort=effort).invoke("sys", "usr")
        argv = runner.launches[0].argv
        if tail is None:
            assert "--effort" not in argv
        else:
            assert argv[-2:] == tail

    def test_max_tokens_is_not_forwarded(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", raising=False)
        runner = FakeRunner()
        _client(runner).invoke("sys", "usr", max_tokens=12345)
        [launch] = runner.launches
        assert not any("12345" in arg for arg in launch.argv)
        assert launch.kwargs["env"] == {k: v for k, v in os.environ.items() if k not in CLAUDE_ENV_DENYLIST}

    def test_cli_client_sampling_attributes_are_none(self):
        client = _client(FakeRunner(), models=("sonnet", "haiku"))
        assert real_orchestrator._sampling(client) == {"temperature": None, "seed": None, "models": ["sonnet", "haiku"]}


class TestClaudeParse:
    """How one finished claude process becomes an answer or a failure."""

    def test_claude_success_maps_text_tokens_model_finish_and_cost(self):
        result = _client(FakeRunner(CLEAN)).invoke("sys", "usr")
        assert result.text == '{"ok": true, "n": 3}'
        assert (result.input_tokens, result.output_tokens) == (563, 15)
        assert result.model == "claude-sonnet-5"
        assert result.backend == "claude-cli"
        assert result.finish_reason == "end_turn"
        assert result.cost_usd == 0.001276
        assert result.cost_source == "claude-cli"
        assert isinstance(result.elapsed_ms, int) and result.elapsed_ms >= 0

    def test_claude_cached_prompt_tokens_count_as_input(self):
        result = _client(FakeRunner(Script(stdout=NOSKILLS_STREAM))).invoke("sys", "usr")
        assert (result.input_tokens, result.output_tokens) == (564, 14)
        assert result.text == '{"ok":true,"n":3}'

    def test_cache_reads_count_as_input_too(self):
        usage = {
            "input_tokens": 3,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 700,
            "output_tokens": 9,
        }
        stream = _stream(_init(), _result(usage=usage))
        assert _client(FakeRunner(Script(stdout=stream))).invoke("sys", "usr").input_tokens == 703

    def test_claude_fenced_result_is_returned_verbatim(self):
        fenced = '```json\n{"findings": []}\n```'
        stream = _stream(_init(), _result(result=fenced))
        assert _client(FakeRunner(Script(stdout=stream))).invoke("sys", "usr").text == fenced

    def test_claude_is_error_fails_the_model_even_with_subtype_success(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER)
        runner = FakeRunner(TRUNC, CLEAN)
        result = _client(runner, models=("sonnet", "haiku")).invoke("sys", "usr")
        assert runner.models() == ["sonnet", "haiku"]
        assert result.text == '{"ok": true, "n": 3}'
        [failed] = [m for m in _warnings(caplog) if "failed" in m]
        assert "backend=claude-cli sonnet: api_error: API Error: Claude's response exceeded" in failed

    def test_is_error_is_authoritative_even_on_exit_zero(self):
        stream = _stream(_init(), _result(is_error=True, terminal_reason="api_error", result="Overloaded"))
        with pytest.raises(LLMError) as exc:
            _client(FakeRunner(Script(stdout=stream, rc=0))).invoke("sys", "usr")
        assert str(exc.value) == "all models failed: sonnet: api_error: Overloaded"

    def test_a_nonzero_exit_fails_even_a_clean_looking_result(self):
        with pytest.raises(LLMError, match=r"sonnet: exit 3: "):
            _client(FakeRunner(Script(stdout=CLEAN_STREAM, rc=3))).invoke("sys", "usr")

    def test_claude_unrecognized_model_is_marked_unavailable_and_skipped_next_invoke(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER)
        runner = FakeRunner(BADMODEL, CLEAN)
        client = _client(runner, models=("claude-nonexistent-9", "sonnet"))
        client.invoke("sys", "usr")
        client.invoke("sys", "usr")
        assert runner.models() == ["claude-nonexistent-9", "sonnet", "sonnet"]
        marked = [m for m in _warnings(caplog) if "marked unavailable" in m]
        assert len(marked) == 1
        assert marked[0].startswith("model=claude-nonexistent-9 marked unavailable (claude-nonexistent-9: api_error:")

    def test_a_skipped_model_is_named_when_the_chain_is_exhausted(self):
        runner = FakeRunner(BADMODEL)
        client = _client(runner, models=("claude-nonexistent-9",))
        with pytest.raises(LLMError):
            client.invoke("sys", "usr")
        with pytest.raises(LLMError) as exc:
            client.invoke("sys", "usr")
        assert str(exc.value) == "all models failed: claude-nonexistent-9: skipped (unavailable)"
        assert len(runner.launches) == 1

    @pytest.mark.parametrize(
        ("status", "text", "stderr", "unavailable"),
        [
            (404, "Not found", "", True),
            (400, "The model claude-x does not exist", "", True),
            (403, "model not available for your plan", "", True),
            (400, "prompt is too long", "", False),
            (429, "rate limited", "", False),
            (500, "model not available", "", False),
            (None, "boom", BADMODEL_STDERR, True),
            (True, "not available", "", False),
        ],
    )
    def test_which_failures_mark_the_model_unavailable(self, status, text, stderr, unavailable):
        failed = _result(is_error=True, terminal_reason="api_error", api_error_status=status, result=text)
        stream = _stream(_init(), failed)
        runner = FakeRunner(Script(stdout=stream, stderr=stderr, rc=1), CLEAN)
        client = _client(runner, models=("a", "b"))
        client.invoke("sys", "usr")
        client.invoke("sys", "usr")
        assert runner.models() == (["a", "b", "b"] if unavailable else ["a", "b", "a"])

    def test_claude_nonzero_exit_without_result_uses_stderr_tail(self):
        stderr = "Error: Invalid API key\n   Please run /login\n"
        with pytest.raises(LLMError) as exc:
            _client(FakeRunner(Script(stderr=stderr, rc=1))).invoke("sys", "usr")
        assert str(exc.value) == "all models failed: sonnet: exit 1: Error: Invalid API key Please run /login"

    def test_a_long_stderr_is_cut_to_its_tail(self):
        stderr = "x" * 500 + " the real cause"
        with pytest.raises(LLMError) as exc:
            _client(FakeRunner(Script(stderr=stderr, rc=1))).invoke("sys", "usr")
        detail = str(exc.value).split("sonnet: exit 1: ", 1)[1]
        assert len(detail) == 200
        assert detail.endswith("the real cause")

    def test_claude_unparseable_stdout_fails_the_model(self):
        with pytest.raises(LLMError) as exc:
            _client(FakeRunner(Script(stdout="this is not json\n{broken\n[1, 2]\n"))).invoke("sys", "usr")
        assert str(exc.value) == "all models failed: sonnet: unparseable output: (no output)"

    def test_events_without_a_result_fail_the_model(self):
        stream = _stream(_init(), _assistant("partial"))
        with pytest.raises(LLMError) as exc:
            _client(FakeRunner(Script(stdout=stream))).invoke("sys", "usr")
        assert str(exc.value) == "all models failed: sonnet: no result event: (no output)"

    def test_a_non_string_result_fails_the_model(self):
        stream = _stream(_init(), _result(result={"ok": True}))
        with pytest.raises(LLMError, match=r"sonnet: no result text"):
            _client(FakeRunner(Script(stdout=stream))).invoke("sys", "usr")

    def test_a_rejected_rate_limit_names_the_failure(self):
        stream = _stream(_init(), _rate_limit("rejected"))
        with pytest.raises(LLMError, match=r"^all models failed: sonnet: rate limited: "):
            _client(FakeRunner(Script(stdout=stream, stderr="usage limit reached", rc=1))).invoke("sys", "usr")

    def test_noise_lines_around_the_events_are_skipped(self):
        stream = "warming up\n" + CLEAN_STREAM + "\n\n"
        assert _client(FakeRunner(Script(stdout=stream))).invoke("sys", "usr").text == '{"ok": true, "n": 3}'

    def test_claude_all_models_fail_raises_llmerror_naming_each(self):
        runner = FakeRunner(Script(stderr="logged out", rc=1), TRUNC)
        with pytest.raises(LLMError) as exc:
            _client(runner, models=("haiku", "sonnet")).invoke("sys", "usr")
        assert str(exc.value) == (
            f"all models failed: haiku: exit 1: logged out; sonnet: api_error: {TRUNC_TEXT}"
        )

    @pytest.mark.parametrize(
        ("model_usage", "init_model", "expected"),
        [
            ({"claude-sonnet-5": {"outputTokens": 15}}, "ignored", "claude-sonnet-5"),
            (
                {"claude-haiku-4-5": {"outputTokens": 3}, "claude-sonnet-5": {"outputTokens": 40}},
                None,
                "claude-sonnet-5",
            ),
            ({}, "claude-sonnet-5", "claude-sonnet-5"),
            ({}, None, "sonnet"),
        ],
    )
    def test_the_reported_model_is_the_one_the_cli_ran(self, model_usage, init_model, expected):
        events = [_init(model=init_model)] if init_model is not None else []
        stream = _stream(*events, _result(modelUsage=model_usage))
        assert _client(FakeRunner(Script(stdout=stream))).invoke("sys", "usr").model == expected

    def test_a_truncated_answer_advances_the_chain(self):
        truncated = _stream(_init(), _result(stop_reason="max_tokens", result='{"find'))
        runner = FakeRunner(Script(stdout=truncated), CLEAN)
        result = _client(runner, models=("haiku", "sonnet")).invoke("sys", "usr")
        assert runner.models() == ["haiku", "sonnet"]
        assert result.finish_reason == "end_turn"

    def test_all_truncated_returns_the_last_truncated_answer(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER)
        first = _stream(_init(), _result(stop_reason="max_tokens", result='{"a'))
        second = _stream(_init(), _result(stop_reason="max_tokens", result='{"b'))
        result = _client(FakeRunner(Script(stdout=first), Script(stdout=second)), models=("a", "b")).invoke("s", "u")
        assert (result.text, result.finish_reason) == ('{"b', "max_tokens")
        assert len([m for m in _warnings(caplog) if "truncated: backend=claude-cli" in m]) == 2

    def test_ok_line_names_backend_and_auth(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER)
        _client(FakeRunner(CLEAN)).invoke("sys", "usr")
        [ok] = [r.getMessage() for r in caplog.records if " ok: " in r.getMessage()]
        assert ok.startswith("llm attempt 1/1 ok: backend=claude-cli model=claude-sonnet-5 ")
        assert ok.endswith(" in=563 out=15 finish=end_turn auth=none")


class TestClaudeCost:
    """#67 via #66: ``total_cost_usd`` folded over every response one invoke received."""

    def test_the_reported_total_cost_is_the_call_cost(self):
        result = _client(FakeRunner(CLEAN)).invoke("sys", "usr")
        assert (result.cost_usd, result.cost_source) == (0.001276, "claude-cli")

    def test_claude_cost_sums_received_attempts_including_is_error(self):
        result = _client(FakeRunner(TRUNC, CLEAN), models=("a", "b")).invoke("sys", "usr")
        assert result.cost_usd == math.fsum([0.0096972, 0.001276])
        assert result.cost_source == "claude-cli"

    def test_a_returned_truncated_answer_carries_every_later_billed_attempt(self):
        truncated = _stream(_init(), _result(stop_reason="max_tokens", result='{"a', total_cost_usd=0.002))
        failed = _stream(_init(), _result(is_error=True, terminal_reason="api_error", result="x", total_cost_usd=0.003))
        result = _client(FakeRunner(Script(stdout=truncated), Script(stdout=failed, rc=1)), models=("a", "b")).invoke(
            "sys", "usr"
        )
        assert result.finish_reason == "max_tokens"
        assert result.cost_usd == math.fsum([0.002, 0.003])
        assert result.cost_source == "claude-cli"

    def test_a_reported_zero_is_zero_not_unknown(self):
        result = _client(FakeRunner(BADMODEL, CLEAN), models=("claude-nonexistent-9", "sonnet")).invoke("s", "u")
        assert (result.cost_usd, result.cost_source) == (0.001276, "claude-cli")

    @pytest.mark.parametrize("bad", ["missing", None, -1, "abc", True, float("nan")])
    def test_an_unusable_cost_makes_the_call_cost_unknown(self, bad):
        event = _result()
        if bad == "missing":
            del event["total_cost_usd"]
        else:
            event["total_cost_usd"] = bad
        result = _client(FakeRunner(Script(stdout=_stream(_init(), event)))).invoke("sys", "usr")
        assert (result.cost_usd, result.cost_source) == (None, "")

    def test_one_unpriced_received_attempt_makes_the_whole_call_unknown(self):
        unpriced = _result(is_error=True, terminal_reason="api_error", result="x")
        del unpriced["total_cost_usd"]
        runner = FakeRunner(Script(stdout=_stream(unpriced), rc=1), CLEAN)
        result = _client(runner, models=("a", "b")).invoke("sys", "usr")
        assert (result.cost_usd, result.cost_source) == (None, "")

    def test_attempts_that_returned_nothing_are_not_counted(self):
        runner = FakeRunner(Script(hang=True), Script(stderr="crash", rc=139), CLEAN)
        result = _client(runner, models=("a", "b", "c")).invoke("sys", "usr")
        assert (result.cost_usd, result.cost_source) == (0.001276, "claude-cli")


class TestDeadlineAndConcurrency:
    def test_claude_timeout_kills_process_group_and_reason_matches_is_timeout_error(self, killpg_calls):
        runner = FakeRunner(Script(hang=True))
        with pytest.raises(LLMError) as exc:
            _client(runner).invoke("sys", "usr")
        assert str(exc.value) == "all models failed: sonnet: timeout (TimeoutExpired after 30s)"
        assert real_orchestrator._is_timeout_error(str(exc.value))
        if os.name == "posix":
            assert killpg_calls == [(FAKE_PID, signal.SIGKILL)]
        assert runner.launches[0].timeouts == [30.0, 5.0]

    def test_a_non_timeout_failure_does_not_read_as_a_timeout(self):
        with pytest.raises(LLMError) as exc:
            _client(FakeRunner(BADMODEL)).invoke("sys", "usr")
        assert not real_orchestrator._is_timeout_error(str(exc.value))

    def test_a_timeout_advances_the_chain(self):
        runner = FakeRunner(Script(hang=True), CLEAN)
        result = _client(runner, models=("a", "b")).invoke("sys", "usr")
        assert runner.models() == ["a", "b"]
        assert result.text == '{"ok": true, "n": 3}'

    def test_a_group_that_is_already_gone_falls_back_to_killing_the_process(self, monkeypatch):
        def gone(pid, sig):
            raise ProcessLookupError(pid)

        monkeypatch.setattr(clib.os, "killpg", gone, raising=False)
        proc = SimpleNamespace(pid=FAKE_PID, killed=False)
        proc.kill = lambda: setattr(proc, "killed", True)
        clib._kill_tree(proc, posix=True)
        assert proc.killed

    def test_off_posix_only_the_process_is_killed(self, killpg_calls):
        proc = SimpleNamespace(pid=FAKE_PID, killed=False)
        proc.kill = lambda: setattr(proc, "killed", True)
        clib._kill_tree(proc, posix=False)
        assert proc.killed
        assert killpg_calls == []

    def test_an_error_while_the_process_runs_kills_it_and_fails_the_model(self, killpg_calls):
        runner = FakeRunner(Script(communicate_error=BrokenPipeError(32, "Broken pipe")), CLEAN)
        result = _client(runner, models=("a", "b")).invoke("sys", "usr")
        assert result.text == '{"ok": true, "n": 3}'
        if os.name == "posix":
            assert killpg_calls == [(FAKE_PID, signal.SIGKILL)]
        else:
            assert runner.procs[0].launch.killed

    @pytest.mark.parametrize(("timeout_s", "expected"), [(7.5, 7.5), (None, 30.0)])
    def test_timeout_s_overrides_default_timeout(self, timeout_s, expected):
        runner = FakeRunner()
        _client(runner).invoke("sys", "usr", timeout_s=timeout_s)
        assert runner.launches[0].timeouts == [expected]

    def test_concurrency_cap_bounds_live_processes(self):
        gate = threading.Event()
        runner = FakeRunner(Script(stdout=CLEAN_STREAM, gate=gate))
        client = _client(runner, concurrency=2)
        results: list[InvokeResult] = []
        threads = [threading.Thread(target=lambda: results.append(client.invoke("s", "u"))) for _ in range(6)]
        for thread in threads:
            thread.start()
        try:
            _wait_until(lambda: runner.active == 2)
            time.sleep(0.05)
            assert runner.active == 2
            assert len(runner.launches) == 2
        finally:
            gate.set()
            for thread in threads:
                thread.join(timeout=10)
        assert runner.max_active == 2
        assert len(results) == 6
        assert len(runner.launches) == 6

    def test_slot_released_after_timeout_and_after_oserror(self):
        runner = FakeRunner(
            Script(hang=True),
            Script(launch_error=FileNotFoundError(2, "No such file or directory")),
            CLEAN,
        )
        client = _client(runner, concurrency=1)
        outcomes: list[object] = []

        def run():
            for _ in range(3):
                try:
                    outcomes.append(client.invoke("s", "u"))
                except LLMError as exc:
                    outcomes.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), "a concurrency slot leaked"
        assert [type(o) for o in outcomes] == [LLMError, LLMError, InvokeResult]

    def test_launch_oserror_fails_the_model_not_the_process(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER)
        runner = FakeRunner(Script(launch_error=FileNotFoundError(2, "No such file or directory")), CLEAN)
        result = _client(runner, models=("a", "b")).invoke("sys", "usr")
        assert result.text == '{"ok": true, "n": 3}'
        assert any("a: launch failed (FileNotFoundError:" in m for m in _warnings(caplog))

    @pytest.mark.parametrize("bad", [0, -1, True, 1.5])
    def test_the_client_rejects_a_concurrency_below_one(self, bad):
        with pytest.raises(ValueError, match="concurrency"):
            _client(FakeRunner(), concurrency=bad)

    def test_the_client_rejects_an_empty_model_chain(self):
        with pytest.raises(ValueError, match="models"):
            _client(FakeRunner(), models=())


class TestClaudeWarnings:
    def test_warns_once_when_init_reports_api_key_source(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER)
        stream = _stream(_init(apiKeySource="ANTHROPIC_API_KEY"), CLEAN_RESULT)
        client = _client(FakeRunner(Script(stdout=stream)))
        client.invoke("sys", "usr")
        client.invoke("sys", "usr")
        assert _warnings(caplog) == [
            "claude-cli: the CLI reports apiKeySource=ANTHROPIC_API_KEY, so this call is NOT on your "
            "subscription login (check managed settings / apiKeyHelper)"
        ]
        assert any(r.getMessage().endswith(" auth=ANTHROPIC_API_KEY") for r in caplog.records)

    def test_warns_when_cli_loads_tools_or_mcp_servers(self, caplog):
        caplog.set_level(logging.WARNING, logger=LOGGER)
        stream = _stream(_init(tools=["Bash", "Read"], mcp_servers=[{"name": "x"}]), CLEAN_RESULT)
        client = _client(FakeRunner(Script(stdout=stream)))
        client.invoke("sys", "usr")
        client.invoke("sys", "usr")
        assert _warnings(caplog) == [
            "claude-cli: the CLI loaded tools/MCP servers despite --tools '' --strict-mcp-config (2/1); "
            "the CLI's flags may have changed"
        ]

    def test_rate_limit_warning_logged_when_status_not_allowed(self, caplog):
        caplog.set_level(logging.WARNING, logger=LOGGER)
        warning = _stream(_init(), _rate_limit("allowed_warning"), CLEAN_RESULT)
        client = _client(FakeRunner(Script(stdout=warning)))
        client.invoke("sys", "usr")
        client.invoke("sys", "usr")
        assert _warnings(caplog) == [
            "claude-cli: subscription rate limit status=allowed_warning type=seven_day utilization=0.82"
        ]

    def test_a_clean_subscription_call_logs_no_warning(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER)
        _client(FakeRunner(CLEAN)).invoke("sys", "usr")
        assert _warnings(caplog) == []


def _executable(tmp_path, name: str = "claude", mode: int = 0o755) -> str:
    """A file that ``shutil.which`` accepts as a CLI. It is never executed."""
    path = tmp_path / name
    path.write_text("#!/bin/sh\nexit 97\n")
    path.chmod(mode)
    return str(path)


class TestResolveCliBinary:
    def test_the_default_binaries_cover_every_cli_backend(self):
        assert set(DEFAULT_BINARIES) == set(CLI_BACKENDS)
        assert DEFAULT_BINARIES == {"claude-cli": "claude", "kiro-cli": "kiro-cli"}

    @pytest.mark.parametrize("backend", CLI_BACKENDS)
    def test_the_default_name_is_looked_up_on_path(self, backend):
        asked: list[str] = []
        found = resolve_cli_binary(backend, "", which=lambda name: asked.append(name) or f"/opt/example/{name}")
        assert asked == [DEFAULT_BINARIES[backend]]
        assert found == f"/opt/example/{DEFAULT_BINARIES[backend]}"

    @pytest.mark.parametrize("backend", CLI_BACKENDS)
    @pytest.mark.parametrize("cli_path", ["", "   "])
    def test_resolve_cli_binary_missing_on_path_raises_config_error_naming_backend_and_cli_path(
        self, backend, cli_path
    ):
        with pytest.raises(ConfigError) as exc:
            resolve_cli_binary(backend, cli_path, which=lambda name: None)
        message = str(exc.value)
        assert message.startswith(f"PRXREF_LLM_BACKEND: {backend} needs the '{DEFAULT_BINARIES[backend]}' CLI")
        assert "not found on PATH" in message
        assert "PRXREF_LLM_CLI_PATH" in message

    def test_resolve_cli_binary_bad_override_raises_config_error_naming_cli_path(self):
        with pytest.raises(ConfigError) as exc:
            resolve_cli_binary("claude-cli", "/nonexistent/claude", which=lambda name: None)
        assert str(exc.value) == (
            "PRXREF_LLM_CLI_PATH: '/nonexistent/claude' is not an executable file (PRXREF_LLM_BACKEND=claude-cli)"
        )

    def test_resolve_cli_binary_expands_user_and_uses_which(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        asked: list[str] = []
        found = resolve_cli_binary("claude-cli", " ~/bin/claude ", which=lambda name: asked.append(name) or name)
        assert asked == [str(tmp_path / "bin" / "claude")]
        assert found == str(tmp_path / "bin" / "claude")

    def test_a_relative_hit_is_made_absolute(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        found = resolve_cli_binary("claude-cli", "", which=lambda name: os.path.join("bin", name))
        assert found == os.path.join(os.getcwd(), "bin", "claude")

    def test_a_real_executable_file_resolves_with_the_real_which(self, tmp_path):
        path = _executable(tmp_path)
        assert resolve_cli_binary("claude-cli", path) == path

    @pytest.mark.skipif(os.name != "posix", reason="execute permission is a POSIX mode bit")
    def test_a_file_without_execute_permission_is_rejected(self, tmp_path):
        path = _executable(tmp_path, mode=stat.S_IRUSR | stat.S_IWUSR)
        with pytest.raises(ConfigError, match=r"^PRXREF_LLM_CLI_PATH: "):
            resolve_cli_binary("claude-cli", path)

    def test_an_unknown_backend_names_the_backend_variable(self):
        with pytest.raises(ConfigError, match=r"^PRXREF_LLM_BACKEND: 'litellm' is not a CLI backend"):
            resolve_cli_binary("litellm", "", which=lambda name: "/bin/true")


class TestBuildCliClient:
    def _build(self, backend="claude-cli", **overrides):
        kwargs = {
            "models": ["sonnet", "haiku"],
            "default_timeout": 120.0,
            "reasoning_effort": "low",
            "cli_path": "",
            "concurrency": 2,
            "which": lambda name: f"/opt/example/{name}",
            "runner": FakeRunner(),
        }
        kwargs.update(overrides)
        return build_cli_client(backend, **kwargs)

    def test_claude_builds_a_claude_client_without_starting_a_process(self):
        runner = FakeRunner()
        client = self._build(runner=runner)
        assert isinstance(client, ClaudeCLIClient)
        assert client.binary == "/opt/example/claude"
        assert client.models == ["sonnet", "haiku"]
        assert client.default_timeout == 120.0
        assert client.reasoning_effort == "low"
        assert (client.temperature, client.seed) == (None, None)
        assert runner.launches == []

    def test_the_built_client_uses_the_injected_runner(self):
        runner = FakeRunner()
        self._build(runner=runner).invoke("sys", "usr")
        assert runner.launches[0].argv[0] == "/opt/example/claude"

    def test_the_concurrency_reaches_the_client(self):
        gate = threading.Event()
        runner = FakeRunner(Script(stdout=CLEAN_STREAM, gate=gate))
        client = self._build(runner=runner, concurrency=1, models=["sonnet"])
        threads = [threading.Thread(target=client.invoke, args=("s", "u")) for _ in range(3)]
        for thread in threads:
            thread.start()
        try:
            _wait_until(lambda: runner.active == 1)
            time.sleep(0.05)
            assert len(runner.launches) == 1
        finally:
            gate.set()
            for thread in threads:
                thread.join(timeout=10)
        assert runner.max_active == 1

    def test_a_missing_binary_fails_before_any_client_exists(self):
        with pytest.raises(ConfigError, match=r"^PRXREF_LLM_CLI_PATH: '/nonexistent/claude'"):
            self._build(cli_path="/nonexistent/claude", which=lambda name: None)

    @pytest.mark.parametrize("bad", [0, -2, True, "2", None])
    def test_a_bad_concurrency_is_a_config_error_naming_the_variable(self, bad):
        with pytest.raises(ConfigError, match=r"^PRXREF_LLM_CLI_CONCURRENCY: must be an integer at least 1"):
            self._build(concurrency=bad)

    def test_an_unknown_backend_is_a_config_error(self):
        with pytest.raises(ConfigError, match=r"^PRXREF_LLM_BACKEND: 'openai-compat' is not a CLI backend"):
            self._build(backend="openai-compat")


class TestKiroSeam:
    """Until W66B lands ``KiroCLIClient``, kiro-cli fails closed. W66B replaces this class."""

    def test_kiro_cli_is_not_wired_and_never_launches(self):
        runner = FakeRunner()
        with pytest.raises(ConfigError) as exc:
            build_cli_client(
                "kiro-cli",
                models=["claude-haiku-4.5"],
                default_timeout=120.0,
                reasoning_effort=None,
                cli_path="",
                concurrency=2,
                which=lambda name: f"/opt/example/{name}",
                runner=runner,
            )
        assert str(exc.value) == "PRXREF_LLM_BACKEND: kiro-cli is not wired in this build"
        assert runner.launches == []


class TestThroughTheFactoryAndCli:
    """The real ``create_llm_client`` and ``prxref review`` entry points, with no process started."""

    URL = "https://github.com/acme/widgets/pull/7"

    def test_the_factory_builds_a_claude_client_without_a_base_url(self, monkeypatch, tmp_path):
        path = _executable(tmp_path)
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "claude-cli")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "sonnet,opus")
        monkeypatch.setenv("PRXREF_LLM_CLI_PATH", path)
        monkeypatch.setenv("PRXREF_LLM_TIMEOUT", "180")
        client = create_llm_client()
        assert isinstance(client, ClaudeCLIClient)
        assert client.binary == path
        assert client.models == ["sonnet", "opus"]
        assert client.default_timeout == 180.0
        assert real_orchestrator._sampling(client)["temperature"] is None

    @pytest.fixture
    def runtime(self, monkeypatch):
        ref = PRRef(forge="github", host="github.com", owner="acme", repo="widgets", number=7, url=self.URL)
        calls: list[dict] = []

        def fake_orchestrate(**kwargs):
            calls.append(kwargs)
            return {"verdict": "commented", "findings_active": [], "findings_dropped": [],
                    "input_tokens": 0, "output_tokens": 0}

        monkeypatch.setattr(cli, "detect_forge", lambda url: ref)
        monkeypatch.setattr(cli, "make_forge", lambda r: object())
        monkeypatch.setattr(real_orchestrator, "orchestrate_review", fake_orchestrate)
        return calls

    def _review(self) -> int:
        return cli.main(["review", "--pr-url", self.URL, "--no-post", "--format", "json"])

    def test_review_exits_2_when_the_claude_binary_is_missing(self, monkeypatch, capsys, runtime):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "claude-cli")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "sonnet")
        monkeypatch.setenv("PRXREF_LLM_CLI_PATH", "/nonexistent/claude")
        assert self._review() == 2
        err = capsys.readouterr().err
        assert "configuration error: PRXREF_LLM_CLI_PATH: '/nonexistent/claude' is not an executable file" in err
        assert runtime == []

    def test_review_with_a_resolvable_claude_reaches_the_orchestrator(self, monkeypatch, capsys, tmp_path, runtime):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "claude-cli")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "sonnet")
        monkeypatch.setenv("PRXREF_LLM_CLI_PATH", _executable(tmp_path))
        assert self._review() == 0
        [call] = runtime
        assert isinstance(call["llm"], ClaudeCLIClient)
        assert "configuration error" not in capsys.readouterr().err
