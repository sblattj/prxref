"""``review --prompts-dir DIR`` and ``PRXREF_PROMPTS_DIR`` at the CLI (#11 T4).

``_run_review`` loads the prompt-template directory next to the rules and
ticket files: after config, before ``make_forge`` and the LLM client, under
the flag's name when the flag was given and the variable's otherwise. The
loaded :class:`prxref.prompt_templates.PromptTemplates` reaches
``orchestrate_review`` as ``prompts=``; its record comes back as the
``prompt_templates`` key of ``--format json`` (always present, ``null`` when
unset) and as the ``-v`` ``prompts:`` line. The webhook daemon goes through
the same ``_run_review``, so it honours the variable on every webhook.

Most tests stub the orchestrator the way ``tests/test_cli.py``'s
``fake_runtime`` does, copied here rather than imported. The stub answers
with the record the real orchestrator would stamp, so the JSON and ``-v``
surfaces are driven by the templates the CLI actually loaded. One end-to-end
test runs the real orchestrator and reviewer with only the forge and the
model doubled. Nothing here uses ``contract_stubs``, and the loader confines
the directory to the working directory, so each test runs from a fresh one.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import sys
import threading
import types
from pathlib import Path

import pytest

from prxref import cli
from prxref import orchestrator as real_orchestrator
from prxref.forges.base import PRRef
from prxref.llm import ConfigError, InvokeResult
from prxref.prompt_templates import TEMPLATE_NAMES, PromptTemplates, export_prompt_templates
from tests.test_orchestrator import FakeForge, _added_file_diff

URL = "https://github.com/acme/widget/pull/7"
REF = PRRef(forge="github", host="github.com", owner="acme", repo="widget", number=7, url=URL)

STYLE_LINE = "no style-guide nits that change neither behavior nor risk"
WORKER_LINE = "ACME chunk policy: style-guide findings that enforce the team standard are welcome"
SUMMARY_HEAD = "## prxref automated review: "
SUMMARY_LINE = "## ACME review digest: "
MARKER = "## Review Context"


def _install_fake_module(monkeypatch, fullname: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(fullname)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, fullname, mod)
    return mod


@pytest.fixture
def work(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def runtime(work, monkeypatch):
    """A stub orchestrator that records its kwargs and stamps ``prompts.record()`` as the real one does."""
    calls: list[dict] = []

    def orchestrate_review(**kwargs):
        calls.append(kwargs)
        prompts = kwargs["prompts"]
        return {
            "verdict": "Approved",
            "findings_active": [],
            "findings_dropped": [],
            "prompt_templates": prompts.record() if prompts is not None else None,
        }

    monkeypatch.setattr("prxref.cli.detect_forge", lambda url: REF)
    monkeypatch.setattr("prxref.cli.make_forge", lambda ref: object())
    _install_fake_module(monkeypatch, "prxref.llm_backends", create_llm_client=lambda cfg: object())
    _install_fake_module(monkeypatch, "prxref.orchestrator", orchestrate_review=orchestrate_review)
    return calls


def _templates_dir(root: Path, name: str, *keep: str) -> str:
    """Export the packaged templates into ``root/name``, keep ``keep`` (edited), delete the rest."""
    d = root / name
    export_prompt_templates(d)
    for template in TEMPLATE_NAMES:
        path = d / f"{template}.md"
        if template not in keep:
            path.unlink()
            continue
        text = path.read_text(encoding="utf-8")
        if template == "summary":
            text = text.replace(SUMMARY_HEAD, SUMMARY_LINE)
        else:
            assert STYLE_LINE in text
            text = text.replace(STYLE_LINE, WORKER_LINE)
        path.write_text(text, encoding="utf-8")
    return name


def _sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _expected_record(dirname: str, *names: str) -> dict:
    templates = {}
    for name in TEMPLATE_NAMES:
        if name in names:
            path = f"{dirname}/{name}.md"
            templates[name] = {
                "path": path, "sha256": _sha(path), "chars": len(Path(path).read_text(encoding="utf-8")),
            }
    return {"dir": dirname, "templates": templates}


def _review(*extra: str) -> int:
    return cli.main(["review", "--pr-url", URL, "--no-post", *extra])


def _prompts_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("prompts:")]


class TestTheFlag:
    def test_it_is_a_review_option_named_dir_and_off_by_default(self):
        args = cli._build_parser().parse_args(["review", "--pr-url", URL])
        assert args.prompts_dir is None
        args = cli._build_parser().parse_args(["review", "--pr-url", URL, "--prompts-dir", "p"])
        assert args.prompts_dir == "p"

    def test_unset_passes_prompts_none(self, runtime):
        assert _review() == 0
        assert len(runtime) == 1
        assert "prompts" in runtime[0]
        assert runtime[0]["prompts"] is None

    def test_the_flag_reaches_orchestrate_review_as_the_loaded_templates(self, runtime, work):
        d = _templates_dir(work, "from-flag", "worker")
        assert _review("--prompts-dir", d) == 0
        prompts = runtime[0]["prompts"]
        assert isinstance(prompts, PromptTemplates)
        assert prompts.overridden == ("worker",)
        assert prompts.override("worker") == (work / d / "worker.md").read_text(encoding="utf-8")
        assert WORKER_LINE in prompts.worker
        assert prompts.override("systemic") == "" and prompts.override("summary") == ""
        assert prompts.record() == _expected_record(d, "worker")

    def test_the_variable_reaches_orchestrate_review(self, runtime, work, monkeypatch):
        d = _templates_dir(work, "from-env", "summary")
        monkeypatch.setenv("PRXREF_PROMPTS_DIR", d)
        assert _review() == 0
        prompts = runtime[0]["prompts"]
        assert prompts.overridden == ("summary",)
        assert prompts.record() == _expected_record(d, "summary")

    def test_the_flag_wins_over_the_variable(self, runtime, work, monkeypatch):
        env = _templates_dir(work, "from-env", "summary")
        flag = _templates_dir(work, "from-flag", "worker")
        monkeypatch.setenv("PRXREF_PROMPTS_DIR", env)
        assert _review("--prompts-dir", flag) == 0
        assert runtime[0]["prompts"].record() == _expected_record(flag, "worker")

    def test_the_flag_wins_over_a_variable_that_would_fail(self, runtime, work, monkeypatch):
        flag = _templates_dir(work, "from-flag", "worker")
        monkeypatch.setenv("PRXREF_PROMPTS_DIR", "absent")
        assert _review("--prompts-dir", flag) == 0
        assert runtime[0]["prompts"].record()["dir"] == flag

    @pytest.mark.parametrize("env", ["valid", "absent"])
    def test_an_empty_flag_turns_the_variable_off(self, runtime, work, monkeypatch, capsys, env):
        if env == "valid":
            env = _templates_dir(work, "from-env", "worker")
        monkeypatch.setenv("PRXREF_PROMPTS_DIR", env)
        assert _review("--prompts-dir", "", "--format", "json") == 0
        assert runtime[0]["prompts"] is None
        assert json.loads(capsys.readouterr().out)["prompt_templates"] is None


class TestABadDirectoryExits2BeforeAnyForgeCall:
    @pytest.fixture
    def no_network(self, work, monkeypatch):
        """Every step after the loader fails the test if it is reached."""
        def forge_factory(ref):
            pytest.fail("make_forge was called before the prompts directory was validated")

        def llm_factory(cfg):
            pytest.fail("the LLM client was built before the prompts directory was validated")

        def orchestrate_review(**kwargs):
            pytest.fail("orchestrate_review was called with an invalid prompts directory")

        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: REF)
        monkeypatch.setattr("prxref.cli.make_forge", forge_factory)
        _install_fake_module(monkeypatch, "prxref.llm_backends", create_llm_client=llm_factory)
        _install_fake_module(monkeypatch, "prxref.orchestrator", orchestrate_review=orchestrate_review)

    @staticmethod
    def _run(monkeypatch, via: str, path: str) -> tuple[int, str]:
        if via == "flag":
            return _review("--prompts-dir", path), "--prompts-dir"
        monkeypatch.setenv("PRXREF_PROMPTS_DIR", path)
        return _review(), "PRXREF_PROMPTS_DIR"

    @pytest.mark.parametrize("via", ["flag", "variable"])
    def test_a_missing_directory(self, no_network, monkeypatch, capsys, via):
        code, source = self._run(monkeypatch, via, "absent")
        assert code == 2
        assert capsys.readouterr().err == (
            f"configuration error: {source}: prompts directory 'absent' does not exist\n"
        )

    @pytest.mark.parametrize("via", ["flag", "variable"])
    def test_a_template_without_the_marker(self, no_network, work, monkeypatch, capsys, via):
        d = _templates_dir(work, "bad", "worker")
        worker = work / d / "worker.md"
        worker.write_text(worker.read_text(encoding="utf-8").replace(MARKER, "## Context"), encoding="utf-8")
        code, source = self._run(monkeypatch, via, d)
        assert code == 2
        assert capsys.readouterr().err.startswith(
            f"configuration error: {source}: prompt template 'bad/worker.md' is missing the '{MARKER}' marker"
        )

    @pytest.mark.parametrize("fail_on", ["error", "any"])
    def test_the_fail_on_gate_does_not_turn_it_into_exit_1(self, no_network, monkeypatch, capsys, fail_on):
        monkeypatch.setenv("PRXREF_FAIL_ON", fail_on)
        assert _review("--prompts-dir", "absent") == 2
        assert capsys.readouterr().err.startswith("configuration error: --prompts-dir: ")

    def test_an_escaping_error_is_fenced_into_a_config_error(self, monkeypatch):
        def boom(path, *, source):
            raise OSError("device not ready")

        monkeypatch.setattr("prxref.cli.load_prompt_templates", boom)
        with pytest.raises(ConfigError) as info:
            cli._load_prompts_dir("p", source="--prompts-dir")
        assert str(info.value) == "--prompts-dir: cannot load prompts directory 'p': device not ready"


class TestJson:
    def test_the_key_is_present_and_null_when_unset(self, runtime, capsys):
        assert _review("--format", "json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert "prompt_templates" in payload
        assert payload["prompt_templates"] is None

    def test_the_key_equals_the_record_when_set(self, runtime, work, capsys):
        d = _templates_dir(work, "prompts", "worker", "summary")
        assert _review("--format", "json", "--prompts-dir", d) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["prompt_templates"] == _expected_record(d, "worker", "summary")
        assert payload["prompt_templates"] == runtime[0]["prompts"].record()

    def test_the_key_follows_size_advisory_and_precedes_sampling(self):
        payload = cli._build_json_result({"prompt_templates": {"dir": "p", "templates": {}}, "sampling": {}})
        keys = list(payload)
        assert keys.index("prompt_templates") == keys.index("size_advisory") + 1
        assert keys[-1] == "sampling"


class TestVerboseLine:
    def test_it_appears_only_when_set(self, runtime, work, capsys):
        assert _review("-v") == 0
        assert _prompts_lines(capsys.readouterr().out) == []
        d = _templates_dir(work, "prompts", "worker", "summary")
        assert _review("-v", "--prompts-dir", d) == 0
        assert _prompts_lines(capsys.readouterr().out) == [
            f"prompts: {d} summary={_sha(f'{d}/summary.md')[:12]} worker={_sha(f'{d}/worker.md')[:12]}",
        ]

    def test_it_is_not_printed_without_verbose(self, runtime, work, capsys):
        d = _templates_dir(work, "prompts", "worker")
        assert _review("--prompts-dir", d) == 0
        assert _prompts_lines(capsys.readouterr().out) == []

    def test_it_follows_the_rules_line(self):
        rules = {"path": "r.md", "sha256": "a" * 64, "chars": 3, "max_chars": 10, "truncated": False}
        prompts = {"dir": "p", "templates": {"worker": {"path": "p/worker.md", "sha256": "b" * 64, "chars": 9}}}
        buf = io.StringIO()
        cli._print_summary({"review_rules": rules, "prompt_templates": prompts}, 1.0, verbose=True, out=buf)
        lines = buf.getvalue().splitlines()
        assert lines[lines.index(f"rules: r.md sha256={'a' * 12} chars=3") + 1] == f"prompts: p worker={'b' * 12}"

    @pytest.mark.parametrize(
        "record, line",
        [
            ({"dir": "p", "templates": {}}, "prompts: p"),
            ({"dir": "p", "templates": "junk"}, "prompts: p"),
            ({"dir": "", "templates": {"worker": {}}}, "prompts: - worker=-"),
            ({"templates": {"summary": "junk"}}, "prompts: - summary=-"),
        ],
    )
    def test_a_partial_record_prints_dashes_rather_than_raising(self, record, line):
        buf = io.StringIO()
        cli._print_summary({"prompt_templates": record}, 1.0, verbose=True, out=buf)
        assert _prompts_lines(buf.getvalue()) == [line]


class TestTheWebhookDaemon:
    def test_it_honours_the_variable_and_rereads_it_for_every_webhook(self, runtime, work, monkeypatch):
        d = _templates_dir(work, "prompts", "worker")
        monkeypatch.setenv("PRXREF_PROMPTS_DIR", d)
        cli._webhook_handler(URL)
        first = runtime[0]["prompts"].record()
        assert first == _expected_record(d, "worker")
        worker = work / d / "worker.md"
        worker.write_text(worker.read_text(encoding="utf-8").replace(WORKER_LINE, "EDITED"), encoding="utf-8")
        cli._webhook_handler(URL)
        second = runtime[1]["prompts"].record()
        assert second == _expected_record(d, "worker")
        assert second["templates"]["worker"]["sha256"] != first["templates"]["worker"]["sha256"]

    def test_it_logs_a_bad_directory_and_reviews_nothing(self, runtime, monkeypatch, caplog):
        monkeypatch.setenv("PRXREF_PROMPTS_DIR", "absent")
        with caplog.at_level(logging.ERROR, logger="prxref"):
            cli._webhook_handler(URL)
        errors = [str(r.exc_info[1]) for r in caplog.records if r.exc_info]
        assert errors == ["PRXREF_PROMPTS_DIR: prompts directory 'absent' does not exist"]
        assert runtime == []


class _RecordingLLM:
    """Records every ``(system, user)`` prompt and answers with no findings."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        with self._lock:
            self.calls.append((system, user))
        return InvokeResult(
            text='{"findings": []}', input_tokens=10, output_tokens=5,
            model="fake-model", backend="fake", elapsed_ms=1,
        )


class TestThroughTheRealOrchestrator:
    def test_the_overrides_reach_the_prompts_the_post_and_the_record(self, work, monkeypatch, capsys):
        assert sys.modules["prxref.orchestrator"] is real_orchestrator
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        llm = _RecordingLLM()
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: REF)
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: forge)
        _install_fake_module(monkeypatch, "prxref.llm_backends", create_llm_client=lambda cfg: llm)
        d = _templates_dir(work, "prompts", "worker", "summary")

        assert cli.main(["review", "--pr-url", URL, "--prompts-dir", d, "--format", "json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["prompt_templates"] == _expected_record(d, "worker", "summary")
        *chunks, sweep = llm.calls
        assert chunks and all(WORKER_LINE in system for system, _user in chunks)
        assert WORKER_LINE not in sweep[0] and STYLE_LINE in sweep[0]
        assert len(forge.summaries) == 1
        assert SUMMARY_LINE in forge.summaries[0] and SUMMARY_HEAD not in forge.summaries[0]
