"""Subscription CLI backends: ``claude-cli`` and ``kiro-cli``.

These backends run the user's own installed, logged-in CLI as a subprocess
and walk ``PRXREF_LLM_MODELS`` as a caller-side chain, exactly like the
openai-compat backend: a model that fails, times out or truncates is advanced
past at once, a model the CLI names as unknown is skipped for the rest of the
run, and exhausting the chain raises
:class:`~prxref.llm_backends.LLMError` with per-model reasons. Every call is
one process, launched from an argv list (never a shell) in a fresh, empty
temporary working directory that is removed afterwards, with the user message
on stdin. A deadline miss kills the whole process group and is spelled
``<model>: timeout (...)``, so the orchestrator's zero-context retry fires
exactly as it does for HTTP. A per-client semaphore
(``PRXREF_LLM_CLI_CONCURRENCY``) caps the processes running at once; the
wait for a slot does not count against the deadline.

``claude-cli`` runs ``claude -p`` with every built-in tool, settings source,
MCP server and session file turned off and a single turn allowed, reads the
``stream-json`` event stream, and loads the system prompt from a file beside
(not inside) the working directory. The child environment is the parent's
minus :data:`CLAUDE_ENV_DENYLIST`, a fixed list of credential-routing
variable NAMES whose values are never read, so the CLI falls back to its own
subscription login. ``PRXREF_LLM_REASONING_EFFORT`` becomes ``--effort``. The
call's ``total_cost_usd`` is reported as ``InvokeResult.cost_usd`` with
``cost_source`` ``"claude-cli"``: it is the CLI's API-equivalent figure at
list price, not what a subscription is invoiced.

Neither CLI has a temperature, seed or per-call output-token option, so none
is applied: ``max_tokens`` is accepted and ignored, and each client's
``temperature`` and ``seed`` attributes are ``None``, which the run record's
``sampling`` reports truthfully.

``kiro-cli`` runs ``kiro-cli chat --no-interactive`` on the v2 agent engine,
because the v1 engine does not emit ``stream-json``. That engine does not
apply a ``--model`` flag, so every attempt writes a working-directory-local agent file,
``.kiro/agents/prxref-review.json``, that carries the system prompt and the
chain model and allows no tools, MCP servers or resources. The environment is
passed through unchanged, and ``PRXREF_LLM_REASONING_EFFORT`` is not applied.
Kiro reports no token counts and meters credits rather than dollars, so a
kiro answer counts zero tokens and its ``cost_usd`` is ``None``; the credits
and the Kiro session id go to the INFO ok line instead.

The module is stdlib-only and is imported lazily by
:func:`prxref.llm_backends.create_llm_client`, so the HTTP backends never load
it. The backend names live in ``prxref.llm_backends.CLI_BACKENDS``.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from typing import NamedTuple

from .costs import combine_reported, valid_usd
from .llm import ConfigError, InvokeResult, LLMClient
from .llm_backends import (
    _TRUNCATION_FINISH_REASONS,
    CLI_BACKENDS,
    DEFAULT_CLI_CONCURRENCY,
    LLMError,
    _looks_permanently_unavailable,
    _mark_unavailable,
)

logger = logging.getLogger(__name__)

DEFAULT_BINARIES: Mapping[str, str] = {"claude-cli": "claude", "kiro-cli": "kiro-cli"}
JSON_ONLY_INSTRUCTION = (
    "\n\nRespond with exactly one JSON object and nothing else: no prose "
    "before or after it and no markdown code fences."
)
CLAUDE_ENV_DENYLIST: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_PROFILE",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SIMPLE",
)
# After a SIGKILL the pipes still have to be drained and the child reaped; a
# process that survives even that is killed directly and abandoned.
_REAP_TIMEOUT_S = 5.0
_DETAIL_CHARS = 200
_UNRECOGNIZED_MODEL_MARKER = "[claude-code:unrecognized_model]"
_CLAUDE_INPUT_TOKEN_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
KIRO_AGENT_NAME = "prxref-review"
_KIRO_LIST_MODELS_HINT = " (possibly an unknown model; check kiro-cli chat --list-models)"


def _not_a_cli_backend(backend: str) -> ConfigError:
    return ConfigError(
        f"PRXREF_LLM_BACKEND: {backend!r} is not a CLI backend; expected one of {', '.join(CLI_BACKENDS)}"
    )


class _Attempt(NamedTuple):
    """One CLI process's outcome, as the chain loop in :meth:`_CLIClient.invoke` consumes it.

    ``result`` is the parsed answer, or ``None`` when the model failed, in
    which case ``failure`` is the ``"<model>: ..."`` reason. ``unavailable``
    marks the model as gone for the rest of the run. ``reported`` is the
    ``(cost_usd, cost_source)`` of a response that came back -- billed even
    when it is an error -- or ``None`` when nothing was received or the
    backend never reports a dollar figure (kiro). ``log_extra`` is appended
    to the INFO ok line.
    """

    result: InvokeResult | None
    failure: str = ""
    unavailable: bool = False
    reported: tuple[float | None, str] | None = None
    log_extra: str = ""


class _ProcessFailed(Exception):
    """An ``OSError`` raised while a launched CLI process ran; the process has been killed.

    It keeps a failure after launch apart from a failure to launch, which
    :meth:`_CLIClient._attempt` reports differently. ``error`` is the original.
    """

    def __init__(self, error: OSError):
        super().__init__(str(error))
        self.error = error


def _run_with_deadline(
    runner, argv: list[str], stdin_text: str, cwd: str, env: dict[str, str], deadline: float
) -> tuple[int | None, str, str, bool]:
    """Run one CLI process to completion or for at most ``deadline`` seconds of wall clock.

    Returns ``(returncode, stdout, stderr, timed_out)``. On POSIX the process
    leads its own session, so a deadline miss kills the whole group rather
    than just the direct child (a wrapper script would otherwise leave the
    model process behind). Any other exception while the process runs also
    kills it before propagating; an ``OSError`` propagates as
    :class:`_ProcessFailed`, so it is not mistaken for a failed launch.
    """
    posix = os.name == "posix"
    proc = runner(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=posix,
    )
    try:
        out, err = proc.communicate(input=stdin_text, timeout=deadline)
    except subprocess.TimeoutExpired:
        _kill_tree(proc, posix)
        try:
            proc.communicate(timeout=_REAP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
        return proc.returncode, "", "", True
    except OSError as exc:
        _kill_tree(proc, posix)
        raise _ProcessFailed(exc) from exc
    except BaseException:
        _kill_tree(proc, posix)
        raise
    return proc.returncode, out or "", err or "", False


def _kill_tree(proc, posix: bool) -> None:
    """SIGKILL ``proc``'s process group on POSIX; otherwise, or if that fails, the process alone."""
    if posix:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _count(value: object) -> int:
    """A token count read from CLI JSON: a non-negative int, else 0 (bools and junk included)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _with_cost(result: InvokeResult, received: Sequence[tuple[float | None, str]]) -> InvokeResult:
    """``result`` carrying the folded reported cost of every response one invoke received."""
    cost_usd, cost_source = combine_reported(received)
    return dataclasses.replace(result, cost_usd=cost_usd, cost_source=cost_source)


class _CLIClient(LLMClient):
    """The model chain, process, deadline and concurrency handling shared by the CLI backends.

    A subclass sets ``backend_name`` and implements three hooks:
    :meth:`_prepare` writes the files its CLI reads under the per-attempt
    temporary root and returns the working directory, :meth:`_argv` builds
    the argv list, and :meth:`_parse` turns one finished process into an
    :class:`_Attempt`. It may override :meth:`_child_env`, which passes the
    parent environment through unchanged by default. The chain, the
    unavailable-model memory, truncation, the deadline and process-group
    kill, the concurrency cap and the cost fold all live here, so every CLI
    behaves identically in the chain.
    """

    backend_name = ""

    def __init__(
        self,
        *,
        binary: str,
        models: Sequence[str],
        default_timeout: float,
        concurrency: int = DEFAULT_CLI_CONCURRENCY,
        reasoning_effort: str | None = None,
        runner=subprocess.Popen,
    ):
        if not models:
            raise ValueError("models must be a non-empty list")
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            raise ValueError(f"concurrency must be an integer >= 1, got {concurrency!r}")
        self.binary = binary
        self.models = list(models)
        self.default_timeout = default_timeout
        self.reasoning_effort = reasoning_effort or None
        self.temperature: float | None = None
        self.seed: int | None = None
        self._runner = runner
        self._slots = threading.BoundedSemaphore(concurrency)
        self._unavailable: set[str] = set()
        self._unavailable_lock = threading.Lock()
        self._warned: set[str] = set()
        self._warned_lock = threading.Lock()

    def invoke(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 4096,
        json_mode: bool = False,
        timeout_s: float | None = None,
    ) -> InvokeResult:
        """Run the CLI once per model until one answers untruncated; fast-fail the rest.

        ``max_tokens`` is accepted for protocol conformance and deliberately
        not applied: neither CLI takes a per-call output budget, and capping
        claude through its environment makes it spend recovery turns and end
        in an error instead. ``json_mode`` appends
        :data:`JSON_ONLY_INSTRUCTION` to the system prompt; any code fence
        the model still adds is left for the reviewer's lenient parse.
        ``timeout_s`` (else ``default_timeout``) is each model's wall-clock
        deadline. If every model truncates, the last truncated answer is
        returned rather than raised, as on openai-compat. The result's
        ``cost_usd`` folds the reported cost of every response received in
        this call, failed and truncated attempts included, because each was
        billed; one received response without a figure makes it ``None``.
        """
        deadline = self.default_timeout if timeout_s is None else timeout_s
        sys_text = system + (JSON_ONLY_INSTRUCTION if json_mode else "")
        failures: list[str] = []
        received: list[tuple[float | None, str]] = []
        last_truncated: InvokeResult | None = None
        total = len(self.models)
        for attempt, model in enumerate(self.models, start=1):
            if model in self._unavailable:
                failures.append(f"{model}: skipped (unavailable)")
                continue
            logger.info(
                "llm attempt %d/%d: backend=%s model=%s deadline=%.0fs",
                attempt, total, self.backend_name, model, deadline,
            )
            outcome = self._attempt(model, sys_text, user, deadline)
            if outcome.reported is not None:
                received.append(outcome.reported)
            result = outcome.result
            if result is None:
                logger.warning(
                    "llm attempt %d/%d failed: backend=%s %s",
                    attempt, total, self.backend_name, outcome.failure,
                )
                failures.append(outcome.failure)
                if outcome.unavailable and _mark_unavailable(model, self._unavailable, self._unavailable_lock):
                    logger.warning(
                        "model=%s marked unavailable (%s), skipping for the rest of the run",
                        model, outcome.failure,
                    )
                continue
            if result.finish_reason.strip().lower() in _TRUNCATION_FINISH_REASONS:
                logger.warning(
                    "llm attempt %d/%d truncated: backend=%s model=%s finish_reason=%s after %dms out=%s",
                    attempt, total, self.backend_name, result.model, result.finish_reason,
                    result.elapsed_ms, result.output_tokens,
                )
                failures.append(f"{model}: truncated (finish_reason={result.finish_reason})")
                last_truncated = result
                continue
            logger.info(
                "llm attempt %d/%d ok: backend=%s model=%s %dms in=%s out=%s finish=%s%s",
                attempt, total, self.backend_name, result.model, result.elapsed_ms,
                result.input_tokens, result.output_tokens, result.finish_reason or "-", outcome.log_extra,
            )
            return _with_cost(result, received)
        if last_truncated is not None:
            return _with_cost(last_truncated, received)
        raise LLMError("all models failed: " + "; ".join(failures))

    def _attempt(self, model: str, sys_text: str, user: str, deadline: float) -> _Attempt:
        """Run one model's CLI process inside a concurrency slot and a throwaway directory."""
        waiting_since = time.perf_counter()
        with self._slots:
            logger.debug(
                "%s: waited %dms for a CLI slot",
                self.backend_name, int((time.perf_counter() - waiting_since) * 1000),
            )
            root: str | None = None
            try:
                root = tempfile.mkdtemp(prefix=f"prxref-{self.backend_name}-")
                cwd = self._prepare(root, model, sys_text)
                argv = self._argv(root, model)
                t0 = time.perf_counter()
                rc, out, err, timed_out = _run_with_deadline(
                    self._runner, argv, user, cwd, self._child_env(), deadline
                )
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
            except _ProcessFailed as exc:
                error = exc.error
                return _Attempt(None, f"{model}: process failed ({type(error).__name__}: {error})")
            except OSError as exc:
                return _Attempt(None, f"{model}: launch failed ({type(exc).__name__}: {exc})")
            finally:
                if root is not None:
                    shutil.rmtree(root, ignore_errors=True)
        if timed_out:
            return _Attempt(None, f"{model}: timeout (TimeoutExpired after {deadline:.0f}s)")
        return self._parse(model, rc, out, err, elapsed_ms)

    def _prepare(self, root: str, model: str, sys_text: str) -> str:
        """Write the files this CLI reads under ``root``; return the working directory."""
        raise NotImplementedError

    def _argv(self, root: str, model: str) -> list[str]:
        """The argv list for one call to ``model``; ``argv[0]`` is :attr:`binary`."""
        raise NotImplementedError

    def _parse(self, model: str, rc: int | None, out: str, err: str, elapsed_ms: int) -> _Attempt:
        """Turn one finished (not timed-out) process into an :class:`_Attempt`."""
        raise NotImplementedError

    def _child_env(self, environ: Mapping[str, str] | None = None) -> dict[str, str]:
        """The child process environment: ``environ`` (default ``os.environ``) unchanged."""
        return dict(os.environ if environ is None else environ)

    def _warn_once(self, key: str, message: str, *args: object) -> None:
        """Log ``message`` at WARNING the first time this client sees ``key``."""
        with self._warned_lock:
            if key in self._warned:
                return
            self._warned.add(key)
        logger.warning(message, *args)


class ClaudeCLIClient(_CLIClient):
    """``claude-cli``: the user's own logged-in Claude Code CLI, one print-mode process per call.

    The argv is ``claude -p --model <m> --output-format stream-json --verbose
    --tools "" --setting-sources "" --strict-mcp-config
    --no-session-persistence --max-turns 1 --system-prompt-file <file>``,
    plus ``--effort <e>`` when a reasoning effort is set. The working
    directory is an empty temporary directory, and the prompt file sits
    beside it, not in it. The child environment drops
    :data:`CLAUDE_ENV_DENYLIST` by name.

    The answer is the stream's ``result`` event. ``is_error`` is
    authoritative (the CLI reports ``subtype "success"`` on some failed
    calls), and a non-zero exit, a missing result or a non-string result
    also fail the model. Input tokens include cache creation and cache
    reads, the model is the one the CLI reports it ran, and the cost is
    ``total_cost_usd``. A 404, a 4xx naming the model as gone, or the CLI's
    unrecognized-model marker marks the model unavailable for the run. The
    ``system/init`` and ``rate_limit_event`` events feed one-time WARNINGs:
    an ``apiKeySource`` other than none (the call is not on the subscription
    login), tools or MCP servers loaded despite the flags, and a rate-limit
    status other than ``allowed``.
    """

    backend_name = "claude-cli"

    def _prepare(self, root: str, model: str, sys_text: str) -> str:
        with open(os.path.join(root, "system.md"), "w", encoding="utf-8") as fh:
            fh.write(sys_text)
        cwd = os.path.join(root, "cwd")
        os.mkdir(cwd)
        return cwd

    def _argv(self, root: str, model: str) -> list[str]:
        argv = [
            self.binary, "-p",
            "--model", model,
            "--output-format", "stream-json", "--verbose",
            "--tools", "",
            "--setting-sources", "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--max-turns", "1",
            "--system-prompt-file", os.path.join(root, "system.md"),
        ]
        if self.reasoning_effort:
            argv += ["--effort", self.reasoning_effort]
        return argv

    def _child_env(self, environ: Mapping[str, str] | None = None) -> dict[str, str]:
        """The parent environment minus :data:`CLAUDE_ENV_DENYLIST`; a denylisted value is never read."""
        source = os.environ if environ is None else environ
        return {key: source[key] for key in source if key not in CLAUDE_ENV_DENYLIST}

    def _parse(self, model: str, rc: int | None, out: str, err: str, elapsed_ms: int) -> _Attempt:
        init: dict | None = None
        rate_limit: dict | None = None
        res: dict | None = None
        lines = [line for line in (raw.strip() for raw in out.splitlines()) if line]
        skipped = 0
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                event = None
            if not isinstance(event, dict):
                skipped += 1
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                if init is None:
                    init = event
            elif kind == "rate_limit_event":
                info = event.get("rate_limit_info")
                if isinstance(info, dict):
                    rate_limit = info
            elif kind == "result":
                res = event
        if skipped:
            logger.debug("claude-cli: skipped %d non-event stdout line(s) for model=%s", skipped, model)
        self._check_init(init)
        self._check_rate_limit(rate_limit)

        reported: tuple[float | None, str] | None = None
        if res is not None:
            cost = valid_usd(res.get("total_cost_usd"))
            reported = (cost, self.backend_name if cost is not None else "")
        text = res.get("result") if res is not None else None
        if rc != 0 or res is None or res.get("is_error") is True or not isinstance(text, str):
            kind = self._failure_kind(rc, res, rate_limit, unparseable=bool(lines) and skipped == len(lines))
            if isinstance(text, str) and text.strip():
                detail = _one_line(text)[:_DETAIL_CHARS]
            elif err.strip():
                detail = _one_line(err)[-_DETAIL_CHARS:]
            else:
                detail = "(no output)"
            return _Attempt(
                None,
                f"{model}: {kind}: {detail}",
                unavailable=self._names_model_unavailable(res, err),
                reported=reported,
            )

        usage = res.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        finish_reason = res.get("stop_reason")
        auth = init.get("apiKeySource") if init is not None else None
        return _Attempt(
            InvokeResult(
                text=text,
                input_tokens=sum(_count(usage.get(field)) for field in _CLAUDE_INPUT_TOKEN_FIELDS),
                output_tokens=_count(usage.get("output_tokens")),
                model=self._resolved_model(model, init, res),
                backend=self.backend_name,
                elapsed_ms=elapsed_ms,
                finish_reason=finish_reason if isinstance(finish_reason, str) else "",
            ),
            reported=reported,
            log_extra=f" auth={'-' if auth is None else auth}",
        )

    @staticmethod
    def _failure_kind(rc: int | None, res: dict | None, rate_limit: dict | None, *, unparseable: bool) -> str:
        """Name why a finished claude process did not answer, most specific first."""
        if unparseable:
            return "unparseable output"
        if res is not None:
            reason = res.get("terminal_reason")
            if isinstance(reason, str) and reason and reason != "completed":
                return reason
        if rate_limit is not None and rate_limit.get("status") == "rejected":
            return "rate limited"
        if rc != 0:
            return f"exit {rc}"
        if res is None:
            return "no result event"
        return "error" if res.get("is_error") is True else "no result text"

    @staticmethod
    def _names_model_unavailable(res: dict | None, err: str) -> bool:
        """True when the CLI says the model itself is gone (404, 4xx naming it, or its stderr marker)."""
        if _UNRECOGNIZED_MODEL_MARKER in err:
            return True
        if res is None:
            return False
        status = res.get("api_error_status")
        if isinstance(status, bool) or not isinstance(status, int):
            return False
        if status == 404:
            return True
        text = res.get("result")
        return 400 <= status < 500 and isinstance(text, str) and _looks_permanently_unavailable(text)

    @staticmethod
    def _resolved_model(requested: str, init: dict | None, res: dict) -> str:
        """The model the CLI ran: its ``modelUsage`` key (most output wins), else init's, else the alias."""
        model_usage = res.get("modelUsage")
        if isinstance(model_usage, dict) and model_usage:

            def output_tokens(name: str) -> int:
                entry = model_usage[name]
                return _count(entry.get("outputTokens")) if isinstance(entry, dict) else 0

            return str(max(model_usage, key=output_tokens))
        reported = init.get("model") if init is not None else None
        return reported if isinstance(reported, str) and reported else requested

    def _check_init(self, init: dict | None) -> None:
        if init is None:
            return
        source = init.get("apiKeySource")
        if source not in (None, "none"):
            self._warn_once(
                "apiKeySource",
                "claude-cli: the CLI reports apiKeySource=%s, so this call is NOT on your subscription "
                "login (check managed settings / apiKeyHelper)",
                source,
            )
        tools, servers = init.get("tools"), init.get("mcp_servers")
        loaded = (
            len(tools) if isinstance(tools, list) else 0,
            len(servers) if isinstance(servers, list) else 0,
        )
        if any(loaded):
            self._warn_once(
                "tools",
                "claude-cli: the CLI loaded tools/MCP servers despite --tools '' --strict-mcp-config "
                "(%d/%d); the CLI's flags may have changed",
                *loaded,
            )

    def _check_rate_limit(self, rate_limit: dict | None) -> None:
        if rate_limit is None:
            return
        status = rate_limit.get("status")
        if status in (None, "allowed"):
            return
        self._warn_once(
            f"rate_limit:{status}",
            "claude-cli: subscription rate limit status=%s type=%s utilization=%s",
            status, rate_limit.get("rateLimitType"), rate_limit.get("utilization"),
        )


class KiroCLIClient(_CLIClient):
    """``kiro-cli``: the user's own logged-in Kiro CLI, one headless chat process per call.

    The argv is ``kiro-cli chat --no-interactive --agent prxref-review
    --output-format stream-json --trust-tools= --agent-engine v2``. The v1
    engine does not emit ``stream-json``, and v2 does not apply a ``--model``
    flag, so each attempt writes ``.kiro/agents/prxref-review.json`` into
    its temporary working directory: the system prompt, the chain model, and
    no tools, allowed tools, MCP servers or resources. The environment is the
    parent's, unchanged, and a reasoning effort is not applied.

    The answer is the ``runFinished`` event's ``finalText``, or the joined
    ``agent_message_chunk`` texts when Kiro marks that text truncated.
    Success needs exit 0, no ``runError``, status ``success`` and a
    non-empty answer. Kiro reports no tokens and meters credits, not
    dollars, so the result counts zero tokens, names the requested model
    (Kiro does not echo it) and has ``cost_usd`` ``None``; the summed
    ``credit`` metering and the session id go to the INFO ok line. A
    ``runError`` fails the model as ``<stage> error: <message>``, with a
    ``--list-models`` hint at the ``prompt`` stage, where an unknown model
    fails. No kiro failure marks a model unavailable, because none names the
    model.
    """

    backend_name = "kiro-cli"

    def _prepare(self, root: str, model: str, sys_text: str) -> str:
        agents = os.path.join(root, ".kiro", "agents")
        os.makedirs(agents)
        agent = {
            "name": KIRO_AGENT_NAME,
            "description": "prxref single-shot reviewer: no tools, no MCP, no resources",
            "prompt": sys_text,
            "tools": [],
            "allowedTools": [],
            "mcpServers": {},
            "includeMcpJson": False,
            "resources": [],
            "model": model,
        }
        with open(os.path.join(agents, f"{KIRO_AGENT_NAME}.json"), "w", encoding="utf-8") as fh:
            json.dump(agent, fh)
        return root

    def _argv(self, root: str, model: str) -> list[str]:
        return [
            self.binary, "chat", "--no-interactive",
            "--agent", KIRO_AGENT_NAME,
            "--output-format", "stream-json",
            "--trust-tools=",
            "--agent-engine", "v2",
        ]

    def _parse(self, model: str, rc: int | None, out: str, err: str, elapsed_ms: int) -> _Attempt:
        chunks: list[str] = []
        finished: dict | None = None
        run_error: dict | None = None
        session = ""
        credits: float | None = None
        lines = [line for line in (raw.strip() for raw in out.splitlines()) if line]
        skipped = 0
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                event = None
            if not isinstance(event, dict):
                skipped += 1
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            if not session and isinstance(data.get("sessionId"), str):
                session = data["sessionId"]
            kind = event.get("type")
            if kind == "metadata":
                metered = _kiro_credits(data.get("meteringUsage"))
                if metered is not None:
                    credits = metered if credits is None else credits + metered
            elif kind == "sessionUpdate":
                update = data.get("update")
                if isinstance(update, dict) and update.get("sessionUpdate") == "agent_message_chunk":
                    content = update.get("content")
                    if isinstance(content, dict) and isinstance(content.get("text"), str):
                        chunks.append(content["text"])
            elif kind == "runFinished":
                finished = data
            elif kind == "runError":
                run_error = data
        if skipped:
            logger.debug("kiro-cli: skipped %d non-event stdout line(s) for model=%s", skipped, model)

        final = finished.get("finalText") if finished is not None else None
        if isinstance(final, str) and final.strip() and finished.get("finalTextTruncated") is not True:
            text = final
        else:
            text = "".join(chunks)
        status = finished.get("status") if finished is not None else None
        if rc != 0 or run_error is not None or status != "success" or not text.strip():
            if run_error is not None:
                return _Attempt(None, self._run_error_reason(model, run_error))
            unparseable = bool(lines) and skipped == len(lines)
            if unparseable:
                kind = "unparseable output"
            elif rc != 0:
                kind = f"exit {rc}"
            elif finished is None:
                kind = "no runFinished event"
            elif status != "success":
                kind = f"run status {status!r}"
            else:
                kind = "empty answer"
            detail = _one_line(err)[-_DETAIL_CHARS:] if err.strip() else "(no output)"
            return _Attempt(None, f"{model}: {kind}: {detail}")

        stop_reason = finished.get("stopReason")
        return _Attempt(
            InvokeResult(
                text=text,
                input_tokens=0,
                output_tokens=0,
                model=model,
                backend=self.backend_name,
                elapsed_ms=elapsed_ms,
                finish_reason=stop_reason if isinstance(stop_reason, str) else "",
            ),
            log_extra=(
                f" credits={'-' if credits is None else format(credits, '.4f')}"
                f" session={session or '-'}"
            ),
        )

    @staticmethod
    def _run_error_reason(model: str, run_error: dict) -> str:
        """``<model>: <stage> error: <message>``, plus the list-models hint at the prompt stage."""
        stage = run_error.get("stage")
        stage = stage if isinstance(stage, str) and stage.strip() else "run"
        message = run_error.get("message")
        detail = _one_line(message)[:_DETAIL_CHARS] if isinstance(message, str) and message.strip() else "(no message)"
        hint = _KIRO_LIST_MODELS_HINT if stage == "prompt" else ""
        return f"{model}: {stage} error: {detail}{hint}"


def _kiro_credits(metering: object) -> float | None:
    """The summed ``credit`` values of one Kiro ``meteringUsage`` list, or ``None`` if it has none."""
    if not isinstance(metering, list):
        return None
    total: float | None = None
    for entry in metering:
        if not isinstance(entry, dict) or entry.get("unit") != "credit":
            continue
        value = entry.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        total = value if total is None else total + value
    return total


def resolve_cli_binary(backend: str, cli_path: str, *, which=shutil.which) -> str:
    """Resolve the CLI binary for ``backend`` to an absolute executable path.

    ``cli_path`` is ``PRXREF_LLM_CLI_PATH``: empty (or whitespace) means the
    backend's default binary name (:data:`DEFAULT_BINARIES`) on ``PATH``;
    otherwise it is ``~``-expanded and looked up through ``which``, so a bare
    name searches ``PATH`` and a path must be an executable file. The result
    is made absolute, because every call runs in a temporary working
    directory. A binary that cannot be found raises
    :class:`~prxref.llm.ConfigError` naming ``PRXREF_LLM_CLI_PATH`` when an
    override was given and ``PRXREF_LLM_BACKEND`` otherwise, so a missing CLI
    exits 2 before any network call. An unknown ``backend`` raises one naming
    ``PRXREF_LLM_BACKEND``.
    """
    if backend not in CLI_BACKENDS:
        raise _not_a_cli_backend(backend)
    default = DEFAULT_BINARIES[backend]
    override = (cli_path or "").strip()
    name = os.path.expanduser(override) if override else default
    found = which(name)
    if not found:
        if override:
            raise ConfigError(
                f"PRXREF_LLM_CLI_PATH: {name!r} is not an executable file (PRXREF_LLM_BACKEND={backend})"
            )
        raise ConfigError(
            f"PRXREF_LLM_BACKEND: {backend} needs the {default!r} CLI, which was not found on PATH; "
            f"install it and log in, or set PRXREF_LLM_CLI_PATH to its absolute path"
        )
    return os.path.abspath(found)


def build_cli_client(
    backend: str,
    *,
    models: Sequence[str],
    default_timeout: float,
    reasoning_effort: str | None,
    cli_path: str,
    concurrency: int,
    which=shutil.which,
    runner=subprocess.Popen,
) -> LLMClient:
    """Build the client for ``backend`` (one of ``llm_backends.CLI_BACKENDS``).

    ``models`` is the chain walked in order; ``default_timeout`` is the
    per-model deadline in seconds; ``reasoning_effort`` feeds claude's
    ``--effort`` and is ignored by kiro; ``cli_path`` is passed to
    :func:`resolve_cli_binary`, which runs first, so a missing CLI is a
    :class:`~prxref.llm.ConfigError` before any client exists; ``concurrency``
    caps the CLI processes this client runs at once and must be an integer
    >= 1 (a ``ConfigError`` naming ``PRXREF_LLM_CLI_CONCURRENCY`` otherwise).
    ``which`` and ``runner`` are the binary lookup and the process launcher,
    injectable for tests. Building never starts a process.

    ``claude-cli`` returns a :class:`ClaudeCLIClient` and ``kiro-cli`` a
    :class:`KiroCLIClient`; a reasoning effort set for ``kiro-cli`` is
    dropped with one INFO line saying so.
    """
    if backend not in CLI_BACKENDS:
        raise _not_a_cli_backend(backend)
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ConfigError(f"PRXREF_LLM_CLI_CONCURRENCY: must be an integer at least 1, got {concurrency!r}")
    binary = resolve_cli_binary(backend, cli_path, which=which)
    if backend == "kiro-cli":
        if reasoning_effort:
            logger.info("PRXREF_LLM_REASONING_EFFORT is not applied by kiro-cli")
        return KiroCLIClient(
            binary=binary,
            models=models,
            default_timeout=default_timeout,
            concurrency=concurrency,
            runner=runner,
        )
    return ClaudeCLIClient(
        binary=binary,
        models=models,
        default_timeout=default_timeout,
        concurrency=concurrency,
        reasoning_effort=reasoning_effort,
        runner=runner,
    )
