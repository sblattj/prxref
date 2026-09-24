"""The review inputs the CLI resolves before any network I/O.

``--rules-file`` / ``PRXREF_REVIEW_RULES`` and ``--context-file`` /
``PRXREF_TICKET_CONTEXT_FILE`` ride the ``load_config`` override path (``""``
blanks the variable for one run) and are loaded after config and before the
forge and the LLM client exist. The replay flags are validated before the URL
is even parsed. A rules or ticket-context file that cannot be read exits 2
naming the input that supplied it, and so does a bad set of replay flags;
tests/test_cli_replay.py pins the rest of replay mode.

The daemon never reads a ticket file and says so once at startup. ``--spec``
is proven end to end here: through the real orchestrator and the real
reviewer, a constraint in a local spec file reaches every worker prompt and
the sweep prompt, from the flag and from the daemon's environment alike.
"""
from __future__ import annotations

import inspect
import json
import logging
import re
import sys
import types
from unittest.mock import MagicMock

import pytest

from prxref import cli, orchestrator
from prxref.cli import main
from prxref.forges.base import detect_forge
from prxref.llm import ConfigError, InvokeResult
from prxref.quality import SEVERITIES
from prxref.rules import (
    MAPPABLE_SEVERITIES,
    RESERVED_SEVERITIES,
    ReviewRules,
    load_review_rules,
    split_front_matter,
)
from prxref.text_inputs import cap_text
from prxref.ticket import TicketContext, fence, load_ticket_context
from tests.test_orchestrator import REF, FakeForge, _added_file_diff

CLI_URL = "https://github.com/org/repo/pull/7"

RULES_MISSING = "cannot read rules file 'team.md': No such file or directory"
TICKET_MISSING = "cannot read ticket-context file 'team.md': No such file or directory"
SHORT_SHA = "--base-sha: must be a full 40- or 64-character hex commit SHA, got 'abc123'"

NEW_KWARGS = (
    "rules", "ticket", "replay", "price_table", "post_cost",
    "size_warn_lines", "size_warn_files", "size_ignore_globs",
)


def _install_fake_module(monkeypatch, fullname: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(fullname)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, fullname, mod)
    return mod


@pytest.fixture
def runtime(monkeypatch):
    """Doubles for everything past config: the orchestrator, the LLM client
    and the forge are recorded, never built, and ``detect_forge`` is the real
    parser behind a call counter."""
    rec = types.SimpleNamespace(orchestrate=[], llm=[], forge=[], detect=[])

    def fake_orchestrate_review(**kwargs):
        rec.orchestrate.append(kwargs)
        return {"verdict": "commented", "findings_active": [], "findings_dropped": []}

    def fake_create_llm_client(cfg):
        rec.llm.append(cfg)
        return MagicMock(name="LLMClient")

    def spy_make_forge(ref):
        rec.forge.append(ref)
        return MagicMock(name="Forge")

    def spy_detect_forge(url):
        rec.detect.append(url)
        return detect_forge(url)

    monkeypatch.setattr("prxref.cli.make_forge", spy_make_forge)
    monkeypatch.setattr("prxref.cli.detect_forge", spy_detect_forge)
    _install_fake_module(
        monkeypatch, "prxref.llm_backends", create_llm_client=fake_create_llm_client,
    )
    _install_fake_module(
        monkeypatch, "prxref.orchestrator", orchestrate_review=fake_orchestrate_review,
    )
    return rec


def _review(*extra: str) -> int:
    return main(["review", "--pr-url", CLI_URL, "--no-post", *extra])


def _assert_nothing_ran(rec) -> None:
    """A configuration error exits before the forge or the LLM client exists."""
    assert rec.orchestrate == []
    assert rec.forge == []
    assert rec.llm == []


class TestStubLoaders:
    """Both loaders are real: an unset path loads nothing, and a file that
    cannot be read fails naming the input that supplied it."""

    @pytest.mark.parametrize("loader", [load_review_rules, load_ticket_context])
    @pytest.mark.parametrize("path", [None, "", "   "])
    def test_an_unset_path_loads_nothing(self, loader, path):
        assert loader(path, max_chars=100, source="--x") is None

    @pytest.mark.parametrize("source", ["--rules-file", "PRXREF_REVIEW_RULES"])
    def test_a_missing_rules_file_fails_naming_its_source(self, source, tmp_path):
        missing = tmp_path / "absent.md"
        with pytest.raises(ConfigError) as exc:
            load_review_rules(str(missing), max_chars=100, source=source)
        assert str(exc.value) == (
            f"{source}: cannot read rules file {str(missing)!r}: No such file or directory"
        )

    def test_the_front_matter_splitter_rejects_a_malformed_map_naming_its_source(self):
        with pytest.raises(ConfigError, match=r"^--rules-file: r\.md:3: unknown severity 'eror'"):
            split_front_matter(
                "---\nseverity:\n  blocker: eror\n---\nbody\n", source="--rules-file", path="r.md",
            )

    def test_spec_is_the_one_severity_rules_cannot_map_onto(self):
        assert RESERVED_SEVERITIES == frozenset({"spec"})
        assert MAPPABLE_SEVERITIES == SEVERITIES - {"spec"}
        assert "spec" not in MAPPABLE_SEVERITIES

    def test_the_rules_record_is_json_native_and_never_carries_the_text(self):
        rules = ReviewRules(
            path="team/rules.md", body=cap_text("SECRET-RULES body", 8),
            severity_map={"blocker": "error"},
        )
        record = rules.record()
        assert set(record) == {
            "path", "sha256", "chars", "max_chars", "truncated", "severity_map",
        }
        assert record["path"] == "team/rules.md"
        assert record["truncated"] is True
        assert record["severity_map"] == {"blocker": "error"}
        assert "SECRET-RULES" not in json.dumps(record)

    def test_the_ticket_record_is_json_native_and_never_carries_the_text(self):
        ticket = TicketContext(
            path="t.md", capped=cap_text("SECRET-TICKET body", 100),
            text="SECRET-TICKET body", has_acceptance_criteria=False,
        )
        record = ticket.record()
        assert set(record) == {
            "path", "sha256", "chars", "max_chars", "truncated",
            "has_acceptance_criteria", "empty",
        }
        assert record["empty"] is False
        assert "SECRET-TICKET" not in json.dumps(record)

    def test_no_record_key_collides_with_the_trace_event_signature(self):
        """``tracer.event(node, phase, **record)`` splats the record, so a key
        named ``node`` or ``phase`` would raise out of orchestrate_review."""
        rules = ReviewRules(path="r", body=cap_text("x", 5), severity_map={})
        ticket = TicketContext(
            path="t", capped=cap_text("", 5), text="", has_acceptance_criteria=False,
        )
        for record in (rules.record(), ticket.record()):
            assert not {"node", "phase"} & set(record)

    def test_an_empty_ticket_is_inactive_and_a_non_empty_one_is_active(self):
        empty = TicketContext(
            path="t", capped=cap_text("", 5), text="", has_acceptance_criteria=False,
        )
        full = TicketContext(
            path="t", capped=cap_text("x", 5), text="x", has_acceptance_criteria=False,
        )
        assert empty.active is False
        assert empty.record()["empty"] is True
        assert full.active is True

    @pytest.mark.parametrize("source", ["--context-file", "PRXREF_TICKET_CONTEXT_FILE"])
    def test_a_missing_ticket_path_fails_closed_naming_its_source(self, source, tmp_path):
        missing = str(tmp_path / "input.md")
        with pytest.raises(ConfigError) as exc:
            load_ticket_context(missing, max_chars=100, source=source)
        assert str(exc.value) == (
            f"{source}: cannot read ticket-context file {missing!r}: No such file or directory"
        )

    @pytest.mark.parametrize(("text", "ticks"), [
        ("plain", 3),
        ("has ``` inside", 4),
        ("has ````` inside", 6),
        ("`a` and ``b``", 3),
    ])
    def test_the_fence_outlasts_every_backtick_run(self, text, ticks):
        assert fence(text) == f"{'`' * ticks}text\n{text}\n{'`' * ticks}"


class _RecordingLLM:
    """Records every (system, user) prompt; the worker may answer one finding."""

    def __init__(self, worker_findings: list[dict] | None = None):
        self.prompts: list[tuple[str, str]] = []
        self.worker_findings = worker_findings or []

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        self.prompts.append((system, user))
        findings = self.worker_findings if "### Diff" in user else []
        return InvokeResult(
            text=json.dumps({"findings": findings, "escalations": []}),
            input_tokens=10, output_tokens=5, model="rec-model-1",
            backend="fake", elapsed_ms=1,
        )

    def users(self, marker: str) -> list[str]:
        return [user for _system, user in self.prompts if marker in user]


def _real_run(**kw):
    forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
    llm = _RecordingLLM()
    res = orchestrator.orchestrate_review(forge, REF, llm, **kw)
    return forge, llm, res


class TestStubSurfaceRidesTheRealPipeline:
    """The loaded types satisfy the orchestrator's duck-typed surface: loaded
    objects run the real pipeline, are recorded, and change no prompt. The
    rules and the ticket are EMPTY ones, the only state that adds nothing to
    the prompts; rules with text are proven in
    tests/test_issue_63_review_rules.py and a ticket with text in
    tests/test_issue_64_ticket_context.py."""

    def test_loaded_objects_are_recorded_and_change_no_prompt(self, tmp_path):
        rules = ReviewRules(path="r.md", body=cap_text("", 100), severity_map={})
        ticket = TicketContext(
            path="t.md", capped=cap_text("", 100), text="",
            has_acceptance_criteria=False,
        )
        trace = tmp_path / "run.jsonl"
        _forge, with_llm, res = _real_run(
            post=False, rules=rules, ticket=ticket, trace_file=str(trace),
        )
        _forge, without_llm, _res = _real_run(post=False)
        assert with_llm.prompts, "no LLM call was made, so the comparison is vacuous"
        assert with_llm.prompts == without_llm.prompts
        assert res["review_rules"] == rules.record()
        assert res["ticket_context"] == ticket.record()
        events = [json.loads(x) for x in trace.read_text().splitlines() if x.strip()]
        nodes = {e["node"] for e in events}
        assert {"rules", "ticket"} <= nodes


class TestParser:
    NEW_FLAGS = {
        "rules_file": None, "context_file": None, "base_sha": None,
        "head_sha": None, "no_threads": False, "diff_file": None,
    }

    def test_every_new_flag_defaults_to_off(self):
        args = cli._build_parser().parse_args(["review", "--pr-url", CLI_URL])
        for name, default in self.NEW_FLAGS.items():
            assert getattr(args, name) is default, name

    def test_an_empty_path_is_kept_as_an_empty_string(self):
        args = cli._build_parser().parse_args([
            "review", "--pr-url", CLI_URL, "--rules-file", "", "--context-file", "",
        ])
        assert args.rules_file == ""
        assert args.context_file == ""

    def test_the_pr_url_is_required_unless_a_diff_file_is_given(self, capsys):
        assert cli._build_parser().parse_args(["review"]).pr_url is None
        assert main(["review"]) == 2
        assert capsys.readouterr().err == (
            "configuration error: --pr-url: required unless --diff-file is given\n"
        )


class TestRulesAndContextFailClosed:
    CASES = [
        ("--rules-file", "PRXREF_REVIEW_RULES", RULES_MISSING),
        ("--context-file", "PRXREF_TICKET_CONTEXT_FILE", TICKET_MISSING),
    ]

    @pytest.mark.parametrize(("flag", "env", "phrase"), CASES)
    def test_the_flag_exits_2_naming_the_flag(self, runtime, capsys, flag, env, phrase):
        assert _review(flag, "team.md") == 2
        err = capsys.readouterr().err
        assert err == f"configuration error: {flag}: {phrase}\n"
        _assert_nothing_ran(runtime)

    @pytest.mark.parametrize(("flag", "env", "phrase"), CASES)
    def test_the_variable_exits_2_naming_the_variable(
        self, runtime, monkeypatch, capsys, flag, env, phrase,
    ):
        monkeypatch.setenv(env, "team.md")
        assert _review() == 2
        assert capsys.readouterr().err == f"configuration error: {env}: {phrase}\n"
        _assert_nothing_ran(runtime)

    @pytest.mark.parametrize(("flag", "env", "phrase"), CASES)
    def test_a_flag_beside_the_variable_is_the_one_reported(
        self, runtime, monkeypatch, capsys, flag, env, phrase,
    ):
        monkeypatch.setenv(env, "from-env.md")
        assert _review(flag, "from-flag.md") == 2
        assert capsys.readouterr().err.startswith(f"configuration error: {flag}: ")

    @pytest.mark.parametrize(("flag", "env", "phrase"), CASES)
    def test_an_empty_flag_blanks_the_variable_for_one_run(
        self, runtime, monkeypatch, flag, env, phrase,
    ):
        monkeypatch.setenv(env, "team.md")
        assert _review(flag, "") == 0
        assert len(runtime.orchestrate) == 1
        call = runtime.orchestrate[0]
        assert call["rules"] is None
        assert call["ticket"] is None

    @pytest.mark.parametrize(("flag", "env", "phrase"), CASES)
    def test_a_whitespace_variable_reads_as_unset(
        self, runtime, monkeypatch, flag, env, phrase,
    ):
        monkeypatch.setenv(env, "   ")
        assert _review() == 0
        assert len(runtime.orchestrate) == 1


class TestLoaderWiring:
    """What ``_run_review`` hands each loader, and what it does with the result."""

    @pytest.fixture
    def loaders(self, monkeypatch):
        calls: dict[str, list] = {"rules": [], "ticket": []}
        loaded = {"rules": object(), "ticket": object()}

        def _fake(kind):
            def loader(path, *, max_chars, source):
                calls[kind].append({"path": path, "max_chars": max_chars, "source": source})
                return loaded[kind] if path else None
            return loader

        monkeypatch.setattr("prxref.cli.load_review_rules", _fake("rules"))
        monkeypatch.setattr("prxref.cli.load_ticket_context", _fake("ticket"))
        return calls, loaded

    def test_flag_paths_caps_and_sources_reach_the_loaders(self, runtime, loaders):
        calls, loaded = loaders
        assert _review("--rules-file", "r.md", "--context-file", "t.md") == 0
        assert calls["rules"] == [
            {"path": "r.md", "max_chars": 12000, "source": "--rules-file"},
        ]
        assert calls["ticket"] == [
            {"path": "t.md", "max_chars": 6000, "source": "--context-file"},
        ]
        call = runtime.orchestrate[0]
        assert call["rules"] is loaded["rules"]
        assert call["ticket"] is loaded["ticket"]

    def test_the_environment_supplies_path_cap_and_source(
        self, runtime, loaders, monkeypatch,
    ):
        calls, _loaded = loaders
        monkeypatch.setenv("PRXREF_REVIEW_RULES", "env-r.md")
        monkeypatch.setenv("PRXREF_REVIEW_RULES_MAX_CHARS", "500")
        monkeypatch.setenv("PRXREF_TICKET_CONTEXT_FILE", "env-t.md")
        monkeypatch.setenv("PRXREF_TICKET_CONTEXT_MAX_CHARS", "400")
        assert _review() == 0
        assert calls["rules"] == [
            {"path": "env-r.md", "max_chars": 500, "source": "PRXREF_REVIEW_RULES"},
        ]
        assert calls["ticket"] == [
            {"path": "env-t.md", "max_chars": 400, "source": "PRXREF_TICKET_CONTEXT_FILE"},
        ]

    @pytest.mark.parametrize(("target", "flag", "error"), [
        ("prxref.cli.load_review_rules", "--rules-file",
         FileNotFoundError(2, "No such file or directory")),
        ("prxref.cli.load_ticket_context", "--context-file",
         UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")),
    ])
    def test_a_stray_loader_error_is_fenced_into_exit_2(
        self, runtime, monkeypatch, capsys, target, flag, error,
    ):
        def boom(path, *, max_chars, source):
            raise error

        monkeypatch.setattr(target, boom)
        assert _review(flag, "input.md") == 2
        err = capsys.readouterr().err
        assert err.startswith(f"configuration error: {flag}: cannot load 'input.md': ")
        _assert_nothing_ran(runtime)


class TestReplayFlagsAreValidatedFirst:
    """A bad set of replay flags exits 2 naming the flag, before the URL is
    parsed and before the forge or the LLM client exist."""

    @pytest.mark.parametrize(("extra", "message"), [
        (["--base-sha", "abc123"],
         "--base-sha/--head-sha: must be given together (got only --base-sha)"),
        (["--head-sha", "def456"],
         "--base-sha/--head-sha: must be given together (got only --head-sha)"),
        (["--base-sha", "abc123", "--head-sha", "b" * 40],
         f"{SHORT_SHA} (resolve it with git rev-parse)"),
        (["--base-sha", "", "--head-sha", ""],
         "--base-sha: must be a full 40- or 64-character hex commit SHA, got '' "
         "(resolve it with git rev-parse)"),
        (["--diff-file", "no-such-dir/pr.diff"],
         "--diff-file: cannot read 'no-such-dir/pr.diff': No such file or directory"),
    ])
    def test_a_bad_replay_flag_exits_2_naming_it(self, runtime, capsys, extra, message):
        assert _review(*extra) == 2
        assert capsys.readouterr().err == f"configuration error: {message}\n"
        assert runtime.detect == []
        _assert_nothing_ran(runtime)

    def test_it_is_checked_before_the_url_is_parsed(self, runtime, capsys):
        url = "https://example.com/not/a/pr"
        assert detect_forge(url) is None
        assert main([
            "review", "--pr-url", url, "--base-sha", "abc123", "--head-sha", "b" * 40,
        ]) == 2
        assert SHORT_SHA in capsys.readouterr().err
        assert runtime.detect == []

    def test_no_replay_flag_passes_no_replay_stamp(self, runtime):
        assert _review() == 0
        assert runtime.orchestrate[0]["replay"] is None

    def test_the_resolver_returns_none_without_a_replay_flag(self):
        assert cli._resolve_replay(CLI_URL) is None
        with pytest.raises(ConfigError, match=r"^--pr-url: required unless --diff-file"):
            cli._resolve_replay(None)


class TestWebhookDaemon:
    def test_it_blanks_the_ticket_file_for_every_review(self, monkeypatch):
        seen: list[tuple] = []
        monkeypatch.setattr(
            "prxref.cli._run_review", lambda url, **kw: seen.append((url, kw)),
        )
        cli._webhook_handler(CLI_URL)
        assert seen == [(CLI_URL, {"post": True, "context_file": ""})]

    def test_a_configured_ticket_file_never_reaches_a_webhook_review(
        self, runtime, monkeypatch,
    ):
        monkeypatch.setenv("PRXREF_TICKET_CONTEXT_FILE", "/nonexistent/ticket.md")
        cli._webhook_handler(CLI_URL)
        assert len(runtime.orchestrate) == 1
        assert runtime.orchestrate[0]["ticket"] is None
        assert runtime.orchestrate[0]["replay"] is None

    def test_it_still_reads_the_rules_variable(self, runtime, monkeypatch, caplog):
        monkeypatch.setenv("PRXREF_REVIEW_RULES", "team.md")
        with caplog.at_level(logging.ERROR, logger="prxref"):
            cli._webhook_handler(CLI_URL)
        assert runtime.orchestrate == []
        errors = [r.exc_info[1] for r in caplog.records if r.exc_info]
        assert [str(e) for e in errors] == [f"PRXREF_REVIEW_RULES: {RULES_MISSING}"]


class TestServeWarnsAboutTheTicketFile:
    WARNING = "PRXREF_TICKET_CONTEXT_FILE is ignored by prxref serve"

    @pytest.fixture
    def served(self, monkeypatch):
        calls: list[dict] = []
        _install_fake_module(
            monkeypatch, "prxref.webhooks", serve=lambda **kw: calls.append(kw),
        )
        return calls

    def _warnings(self, caplog) -> list[str]:
        return [
            r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and self.WARNING in r.getMessage()
        ]

    def test_a_set_variable_is_warned_about_once(self, served, monkeypatch, caplog):
        monkeypatch.setenv("PRXREF_TICKET_CONTEXT_FILE", "ticket.md")
        with caplog.at_level(logging.WARNING, logger="prxref"):
            assert main(["serve"]) == 0
        assert self._warnings(caplog) == [
            f"{self.WARNING}: one file cannot describe every PR",
        ]
        assert len(served) == 1
        assert served[0]["handler"] is cli._webhook_handler

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_an_unset_variable_says_nothing(self, served, monkeypatch, caplog, raw):
        if raw is not None:
            monkeypatch.setenv("PRXREF_TICKET_CONTEXT_FILE", raw)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            assert main(["serve"]) == 0
        assert self._warnings(caplog) == []
        assert len(served) == 1


class TestFeatureOffKwargs:
    def test_every_new_kwarg_arrives_at_its_off_value(self, runtime):
        """With nothing configured the CLI passes exactly what omitting each
        kwarg would: the orchestrator treats ``price_table`` ``None`` and ``{}``
        alike, and ``size_ignore_globs`` only ever iterates."""
        params = inspect.signature(orchestrator.orchestrate_review).parameters
        assert _review() == 0
        call = runtime.orchestrate[0]
        for name in ("rules", "ticket", "replay", "post_cost",
                     "size_warn_lines", "size_warn_files"):
            assert call[name] == params[name].default, name
        assert params["price_table"].default is None
        assert call["price_table"] == {}
        assert list(call["size_ignore_globs"]) == list(params["size_ignore_globs"].default)

    def test_timeout_never_reaches_the_orchestrator(self, runtime):
        assert _review("--timeout", "30") == 0
        assert "timeout" not in runtime.orchestrate[0]
        assert runtime.llm[0]["llm_timeout"] == 30.0


class TestSpecEndToEnd:
    """``--spec`` through ``main`` and the daemon, with nothing mocked between
    the CLI and the prompt but the forge and the model."""

    SENTINEL = "SENTINEL-5150"
    SPEC = f"## Rules\n\nTools MUST carry the {SENTINEL} prefix.\n"

    @pytest.fixture
    def rig(self, monkeypatch, tmp_path):
        assert sys.modules["prxref.orchestrator"] is orchestrator
        spec = tmp_path / "spec.md"
        spec.write_text(self.SPEC, encoding="utf-8")
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        llm = _RecordingLLM()
        fetches: list[list[str]] = []
        real_fetch = orchestrator.specs.fetch_specs

        def spy_fetch(sources, **kw):
            fetches.append(list(sources))
            return real_fetch(sources, **kw)

        monkeypatch.setattr(orchestrator.specs, "fetch_specs", spy_fetch)
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: REF)
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: forge)
        _install_fake_module(
            monkeypatch, "prxref.llm_backends", create_llm_client=lambda cfg: llm,
        )
        return types.SimpleNamespace(spec=spec, forge=forge, llm=llm, fetches=fetches)

    def _assert_grounded(self, rig) -> None:
        workers = rig.llm.users("### Diff")
        sweeps = rig.llm.users("### Digest")
        assert len(workers) == 1
        assert len(sweeps) == 1
        assert len(rig.llm.prompts) == 2
        for user in workers + sweeps:
            assert f"(MUST) Tools MUST carry the {self.SENTINEL} prefix" in user
            assert "(no specs provided for this review)" not in user
        for system, _user in rig.llm.prompts:
            assert self.SENTINEL not in system
        assert rig.fetches == [[str(rig.spec)]]

    def test_the_flag_grounds_every_worker_and_the_sweep(self, rig):
        assert main(["review", "--pr-url", REF.url, "--no-post", "--spec", str(rig.spec)]) == 0
        self._assert_grounded(rig)
        assert rig.forge.summaries == []

    def test_the_daemon_grounds_from_its_environment(self, rig, monkeypatch):
        monkeypatch.setenv("PRXREF_SPEC_SOURCES", str(rig.spec))
        cli._webhook_handler(REF.url)
        self._assert_grounded(rig)
        assert len(rig.forge.summaries) == 1

    def test_without_a_source_nothing_is_fetched(self, rig):
        assert main(["review", "--pr-url", REF.url, "--no-post"]) == 0
        assert rig.fetches == []
        assert len(rig.llm.prompts) == 2
        for _system, user in rig.llm.prompts:
            assert self.SENTINEL not in user
            assert "(no specs provided for this review)" in user


class TestFeatureOffIsByteIdentical:
    """A CLI run with nothing new configured posts and prompts exactly what the
    same run posts without the new kwargs at all."""

    FINDING = {
        "file": "src/app.py", "line": 3, "severity": "warning",
        "confidence": 0.9, "title": "Unchecked data write",
        "body": "The data line is written without validation.",
    }
    ELAPSED = re.compile(r"\d+(?:\.\d+)?\s?(?:ms|s)\b")

    def _norm(self, text: str) -> str:
        return self.ELAPSED.sub("<t>", text)

    def test_the_cli_run_matches_a_run_without_the_new_kwargs(self, monkeypatch):
        assert sys.modules["prxref.orchestrator"] is orchestrator
        real = orchestrator.orchestrate_review
        captured: list[dict] = []

        def spy(**kwargs):
            captured.append(kwargs)
            return real(**kwargs)

        cli_forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        cli_llm = _RecordingLLM([self.FINDING])
        monkeypatch.setattr(orchestrator, "orchestrate_review", spy)
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: REF)
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: cli_forge)
        _install_fake_module(
            monkeypatch, "prxref.llm_backends", create_llm_client=lambda cfg: cli_llm,
        )
        assert main(["review", "--pr-url", REF.url]) == 0
        assert len(captured) == 1
        assert set(NEW_KWARGS) <= set(captured[0])

        lib_forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        lib_llm = _RecordingLLM([self.FINDING])
        base = {k: v for k, v in captured[0].items() if k not in NEW_KWARGS}
        base.update(forge=lib_forge, llm=lib_llm)
        real(**base)

        assert len(cli_llm.prompts) == 2
        assert cli_llm.prompts == lib_llm.prompts
        assert len(cli_forge.summaries) == 1
        assert [self._norm(s) for s in cli_forge.summaries] == [
            self._norm(s) for s in lib_forge.summaries
        ]
        assert [[c.body for c in batch] for batch in cli_forge.inline_batches] == [
            [c.body for c in batch] for batch in lib_forge.inline_batches
        ]
