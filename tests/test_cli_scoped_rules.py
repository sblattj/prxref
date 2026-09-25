"""``review --scoped-rules PATH`` and ``PRXREF_SCOPED_RULES`` at the CLI (#12).

``_run_review`` loads the path-scoped rules right after the always-on rules
file, through the same ``_load_text_input`` fence: after config, before
``make_forge`` and the LLM client, under the flag's name when the flag was
given and the variable's otherwise. The loader is handed the loaded
always-on file, so a team word the two map to different tiers exits 2. The
loaded :class:`prxref.rules.ScopedRules` reaches ``orchestrate_review`` as
``scoped_rules=``, with ``PRXREF_SCOPED_RULES_MAX_CHARS`` as
``scoped_rules_max_chars=``; the orchestrator's record comes back as the
``scoped_rules`` key of ``--format json`` (always present, ``null`` when
unset) and as the ``-v`` ``scoped rules:`` line. The webhook daemon goes
through the same ``_run_review``, so it re-reads the variable and the files
on every webhook.

Most tests stub the orchestrator the way ``tests/test_cli_prompts_dir.py``
does, copied here rather than imported. The stub answers with the record the
real orchestrator stamps before it plans any unit, so the JSON and ``-v``
surfaces are driven by the files the CLI actually loaded. One end-to-end
test runs the real orchestrator and reviewer with only the forge and the
model doubled. The loader confines every path to the working directory, so
each test runs from a fresh one.
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
from prxref.llm import InvokeResult
from prxref.rules import ScopedRules
from tests.test_orchestrator import FakeForge, _added_file_diff

URL = "https://github.com/acme/widget/pull/7"
REF = PRRef(forge="github", host="github.com", owner="acme", repo="widget", number=7, url=URL)

JAVA_RULE = "- blocker: a public method that returns null instead of an Optional."
HELM_RULE = "- blocker: a container without resource limits."
ALWAYS_RULE = "- blocker: any network call without an explicit timeout."
JAVA_FILE = f'---\napplies_to: ["**/*.java", "!**/src/test/**"]\nseverity:\n  nit: outofscope\n---\n{JAVA_RULE}\n'
HELM_FILE = f'---\napplies_to:\n  - "helm/**"\n---\n{HELM_RULE}\n'
ALWAYS_FILE = f"---\nseverity:\n  blocker: error\n---\n{ALWAYS_RULE}\n"
CONFLICT_FILE = "---\nseverity:\n  blocker: warning\n---\n- blocker: a missing changelog entry.\n"


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
    """A stub orchestrator that records its kwargs and stamps the scoped record as the real one does."""
    calls: list[dict] = []

    def orchestrate_review(**kwargs):
        calls.append(kwargs)
        scoped = kwargs["scoped_rules"]
        return {
            "verdict": "Approved",
            "findings_active": [],
            "findings_dropped": [],
            "scoped_rules": (
                {**scoped.record(), "max_chars": kwargs["scoped_rules_max_chars"], "units": None}
                if scoped is not None else None
            ),
        }

    monkeypatch.setattr("prxref.cli.detect_forge", lambda url: REF)
    monkeypatch.setattr("prxref.cli.make_forge", lambda ref: object())
    _install_fake_module(monkeypatch, "prxref.llm_backends", create_llm_client=lambda cfg: object())
    _install_fake_module(monkeypatch, "prxref.orchestrator", orchestrate_review=orchestrate_review)
    return calls


@pytest.fixture
def scoped_dir(work) -> str:
    """``scoped/`` holding ``helm.md`` and ``java.md``; returns the directory's relative name."""
    d = work / "scoped"
    d.mkdir()
    (d / "java.md").write_text(JAVA_FILE, encoding="utf-8")
    (d / "helm.md").write_text(HELM_FILE, encoding="utf-8")
    return "scoped"


def _write(root: Path, name: str, text: str) -> str:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return name


def _sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _review(*extra: str) -> int:
    return cli.main(["review", "--pr-url", URL, "--no-post", *extra])


def _paths(scoped: ScopedRules) -> list[str]:
    return [rules.path for rules in scoped.files]


def _scoped_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("scoped rules:")]


class TestTheFlag:
    def test_it_is_a_repeatable_review_option_and_off_by_default(self):
        parser = cli._build_parser()
        assert parser.parse_args(["review", "--pr-url", URL]).scoped_rules is None
        args = parser.parse_args(["review", "--pr-url", URL, "--scoped-rules", "a", "--scoped-rules", "b"])
        assert args.scoped_rules == ["a", "b"]

    def test_unset_passes_none_and_the_default_cap(self, runtime):
        assert _review() == 0
        assert len(runtime) == 1
        assert runtime[0]["scoped_rules"] is None
        assert runtime[0]["scoped_rules_max_chars"] == 24000

    def test_the_flag_reaches_orchestrate_review_as_the_loaded_rules(self, runtime, scoped_dir):
        assert _review("--scoped-rules", scoped_dir) == 0
        scoped = runtime[0]["scoped_rules"]
        assert isinstance(scoped, ScopedRules)
        assert scoped.entries == (scoped_dir,)
        assert _paths(scoped) == ["scoped/helm.md", "scoped/java.md"]
        assert scoped.files[1].applies_to == ("**/*.java", "!**/src/test/**")
        assert scoped.severity_map == {"nit": "outofscope"}

    def test_the_flag_is_repeatable_in_the_order_given(self, runtime, work):
        _write(work, "b.md", HELM_FILE)
        _write(work, "a.md", JAVA_FILE)
        assert _review("--scoped-rules", "b.md", "--scoped-rules", "a.md") == 0
        assert runtime[0]["scoped_rules"].entries == ("b.md", "a.md")
        assert _paths(runtime[0]["scoped_rules"]) == ["b.md", "a.md"]

    def test_the_variable_reaches_orchestrate_review(self, runtime, work, monkeypatch):
        _write(work, "b.md", HELM_FILE)
        _write(work, "a.md", JAVA_FILE)
        monkeypatch.setenv("PRXREF_SCOPED_RULES", "b.md, a.md")
        assert _review() == 0
        assert _paths(runtime[0]["scoped_rules"]) == ["b.md", "a.md"]

    def test_the_flag_replaces_the_variable_rather_than_adding_to_it(self, runtime, work, monkeypatch):
        _write(work, "env.md", HELM_FILE)
        _write(work, "flag.md", JAVA_FILE)
        monkeypatch.setenv("PRXREF_SCOPED_RULES", "env.md")
        assert _review("--scoped-rules", "flag.md") == 0
        assert _paths(runtime[0]["scoped_rules"]) == ["flag.md"]

    def test_the_flag_wins_over_a_variable_that_would_fail(self, runtime, work, monkeypatch):
        _write(work, "flag.md", JAVA_FILE)
        monkeypatch.setenv("PRXREF_SCOPED_RULES", "absent.md")
        assert _review("--scoped-rules", "flag.md") == 0
        assert _paths(runtime[0]["scoped_rules"]) == ["flag.md"]

    def test_the_flag_can_name_a_path_with_a_space(self, runtime, work):
        _write(work, "team rules.md", JAVA_FILE)
        assert _review("--scoped-rules", "team rules.md") == 0
        assert _paths(runtime[0]["scoped_rules"]) == ["team rules.md"]

    @pytest.mark.parametrize("env", ["valid", "absent.md"])
    def test_an_empty_flag_turns_the_variable_off(self, runtime, scoped_dir, monkeypatch, capsys, env):
        monkeypatch.setenv("PRXREF_SCOPED_RULES", scoped_dir if env == "valid" else env)
        assert _review("--scoped-rules", "", "--format", "json") == 0
        assert runtime[0]["scoped_rules"] is None
        assert json.loads(capsys.readouterr().out)["scoped_rules"] is None

    def test_the_per_unit_cap_comes_from_its_variable(self, runtime, scoped_dir, monkeypatch):
        monkeypatch.setenv("PRXREF_SCOPED_RULES_MAX_CHARS", "500")
        assert _review("--scoped-rules", scoped_dir) == 0
        assert runtime[0]["scoped_rules_max_chars"] == 500

    def test_the_per_file_cap_is_the_always_on_files_cap(self, runtime, scoped_dir, monkeypatch):
        monkeypatch.setenv("PRXREF_REVIEW_RULES_MAX_CHARS", "10")
        assert _review("--scoped-rules", scoped_dir) == 0
        java = runtime[0]["scoped_rules"].files[1]
        assert java.body.text == JAVA_RULE[:10]
        assert java.body.truncated and java.body.max_chars == 10


class TestBadScopedRulesExit2BeforeAnyForgeCall:
    @pytest.fixture
    def no_network(self, work, monkeypatch):
        """Every step after the loader fails the test if it is reached."""
        def forge_factory(ref):
            pytest.fail("make_forge was called before the scoped rules were validated")

        def llm_factory(cfg):
            pytest.fail("the LLM client was built before the scoped rules were validated")

        def orchestrate_review(**kwargs):
            pytest.fail("orchestrate_review was called with invalid scoped rules")

        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: REF)
        monkeypatch.setattr("prxref.cli.make_forge", forge_factory)
        _install_fake_module(monkeypatch, "prxref.llm_backends", create_llm_client=llm_factory)
        _install_fake_module(monkeypatch, "prxref.orchestrator", orchestrate_review=orchestrate_review)

    @staticmethod
    def _run(monkeypatch, via: str, entry: str, *extra: str) -> tuple[int, str]:
        if via == "flag":
            return _review("--scoped-rules", entry, *extra), "--scoped-rules"
        monkeypatch.setenv("PRXREF_SCOPED_RULES", entry)
        return _review(*extra), "PRXREF_SCOPED_RULES"

    @pytest.mark.parametrize("via", ["flag", "variable"])
    def test_a_url(self, no_network, monkeypatch, capsys, via):
        code, source = self._run(monkeypatch, via, "https://example.com/rules.md")
        assert code == 2
        assert capsys.readouterr().err == (
            f"configuration error: {source}: scoped rules must be local file or directory "
            "paths, not a URL: 'https://example.com/rules.md'\n"
        )

    @pytest.mark.parametrize("via", ["flag", "variable"])
    def test_a_missing_path(self, no_network, monkeypatch, capsys, via):
        code, source = self._run(monkeypatch, via, "absent.md")
        assert code == 2
        assert capsys.readouterr().err.startswith(
            f"configuration error: {source}: cannot read rules file 'absent.md': "
        )

    @pytest.mark.parametrize("via", ["flag", "variable"])
    def test_an_empty_applies_to(self, no_network, work, monkeypatch, capsys, via):
        _write(work, "rules/empty.md", "---\napplies_to: []\n---\n- nit: anything.\n")
        code, source = self._run(monkeypatch, via, "rules")
        assert code == 2
        assert capsys.readouterr().err == (
            f"configuration error: {source}: rules/empty.md:2: 'applies_to' is empty; list at "
            "least one glob, or omit the key to apply the file to every unit\n"
        )

    def test_the_variable_cannot_name_a_path_with_a_space(self, no_network, work, monkeypatch, capsys):
        _write(work, "team rules.md", JAVA_FILE)
        code, source = self._run(monkeypatch, "variable", "team rules.md")
        assert code == 2
        assert capsys.readouterr().err.startswith(f"configuration error: {source}: cannot read rules file 'team': ")

    @pytest.mark.parametrize("via", ["flag", "variable"])
    def test_a_tier_conflict_with_the_always_on_file_names_both_files(
        self, no_network, work, monkeypatch, capsys, via,
    ):
        _write(work, "rules.md", ALWAYS_FILE)
        _write(work, "scoped/conflict.md", CONFLICT_FILE)
        monkeypatch.setenv("PRXREF_REVIEW_RULES", "rules.md")
        code, source = self._run(monkeypatch, via, "scoped/conflict.md")
        assert code == 2
        assert capsys.readouterr().err == (
            f"configuration error: {source}: scoped/conflict.md:3: 'blocker' is mapped to warning "
            "here but to error in the always-on rules file 'rules.md'; map each team word to one "
            "tier across all rules files\n"
        )

    def test_the_always_on_file_named_by_its_flag_takes_part_too(self, no_network, work, capsys):
        _write(work, "rules.md", ALWAYS_FILE)
        _write(work, "conflict.md", CONFLICT_FILE)
        assert _review("--rules-file", "rules.md", "--scoped-rules", "conflict.md") == 2
        err = capsys.readouterr().err
        assert err.startswith("configuration error: --scoped-rules: conflict.md:3: 'blocker' ")
        assert "the always-on rules file 'rules.md'" in err

    @pytest.mark.parametrize("fail_on", ["error", "any"])
    def test_the_fail_on_gate_does_not_turn_it_into_exit_1(self, no_network, monkeypatch, capsys, fail_on):
        monkeypatch.setenv("PRXREF_FAIL_ON", fail_on)
        assert _review("--scoped-rules", "absent.md") == 2
        assert capsys.readouterr().err.startswith("configuration error: --scoped-rules: ")


class TestJson:
    def test_the_key_is_present_and_null_when_unset(self, runtime, capsys):
        assert _review("--format", "json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert "scoped_rules" in payload
        assert payload["scoped_rules"] is None

    def test_the_key_equals_the_orchestrator_record_when_set(self, runtime, scoped_dir, capsys):
        assert _review("--format", "json", "--scoped-rules", scoped_dir) == 0
        payload = json.loads(capsys.readouterr().out)
        scoped = runtime[0]["scoped_rules"]
        assert payload["scoped_rules"] == {**scoped.record(), "max_chars": 24000, "units": None}
        assert [f["sha256"] for f in payload["scoped_rules"]["files"]] == [
            _sha("scoped/helm.md"), _sha("scoped/java.md"),
        ]

    def test_the_key_follows_prompt_templates_and_precedes_sampling(self):
        payload = cli._build_json_result({"scoped_rules": {"entries": [], "files": []}, "sampling": {}})
        keys = list(payload)
        assert keys.index("scoped_rules") == keys.index("prompt_templates") + 1
        assert keys[keys.index("scoped_rules") + 1] == "rule_counts"
        assert keys.index("sampling") == keys.index("scoped_rules") + 4

    def test_the_key_is_null_for_a_pre_0_15_result(self):
        assert cli._build_json_result({})["scoped_rules"] is None


class TestVerboseLine:
    def test_it_appears_only_when_set(self, runtime, scoped_dir, capsys):
        assert _review("-v") == 0
        assert _scoped_lines(capsys.readouterr().out) == []
        assert _review("-v", "--scoped-rules", scoped_dir) == 0
        assert _scoped_lines(capsys.readouterr().out) == [
            f"scoped rules: 2 file(s) scoped/helm.md={_sha('scoped/helm.md')[:12]} "
            f"scoped/java.md={_sha('scoped/java.md')[:12]} cap=24000",
        ]

    def test_it_is_not_printed_without_verbose(self, runtime, scoped_dir, capsys):
        assert _review("--scoped-rules", scoped_dir) == 0
        assert _scoped_lines(capsys.readouterr().out) == []

    def test_unset_leaves_the_verbose_output_byte_identical(self):
        rules = {"path": "r.md", "sha256": "a" * 64, "chars": 3, "max_chars": 10, "truncated": False}
        before, after = io.StringIO(), io.StringIO()
        cli._print_summary({"review_rules": rules}, 1.0, verbose=True, out=before)
        cli._print_summary({"review_rules": rules, "scoped_rules": None}, 1.0, verbose=True, out=after)
        assert after.getvalue() == before.getvalue()

    def test_it_sits_between_the_rules_and_prompts_lines(self):
        rules = {"path": "r.md", "sha256": "a" * 64, "chars": 3, "max_chars": 10, "truncated": False}
        scoped = {"entries": ["s"], "files": [{"path": "s/x.md", "sha256": "c" * 64}], "max_chars": 900}
        prompts = {"dir": "p", "templates": {"worker": {"path": "p/worker.md", "sha256": "b" * 64, "chars": 9}}}
        buf = io.StringIO()
        record = {"review_rules": rules, "scoped_rules": scoped, "prompt_templates": prompts}
        cli._print_summary(record, 1.0, verbose=True, out=buf)
        lines = buf.getvalue().splitlines()
        at = lines.index(f"rules: r.md sha256={'a' * 12} chars=3")
        assert lines[at + 1:at + 3] == [
            f"scoped rules: 1 file(s) s/x.md={'c' * 12} cap=900",
            f"prompts: p worker={'b' * 12}",
        ]

    @pytest.mark.parametrize(
        "record, line",
        [
            ({"entries": ["s"], "files": [], "max_chars": 5}, "scoped rules: 0 file(s) cap=5"),
            ({"files": "junk"}, "scoped rules: 0 file(s) cap=-"),
            ({"files": [{}], "max_chars": 5}, "scoped rules: 1 file(s) -=- cap=5"),
            ({"files": ["junk", {"path": "x.md"}]}, "scoped rules: 2 file(s) -=- x.md=- cap=-"),
        ],
    )
    def test_a_partial_record_prints_dashes_rather_than_raising(self, record, line):
        buf = io.StringIO()
        cli._print_summary({"scoped_rules": record}, 1.0, verbose=True, out=buf)
        assert _scoped_lines(buf.getvalue()) == [line]


class TestTheWebhookDaemon:
    def test_it_rereads_the_variable_and_the_files_for_every_webhook(self, runtime, work, scoped_dir, monkeypatch):
        monkeypatch.setenv("PRXREF_SCOPED_RULES", scoped_dir)
        cli._webhook_handler(URL)
        first = runtime[0]["scoped_rules"]
        assert _paths(first) == ["scoped/helm.md", "scoped/java.md"]

        (work / "scoped" / "java.md").write_text(JAVA_FILE.replace("Optional", "Maybe"), encoding="utf-8")
        _write(work, "scoped/proto.md", "---\napplies_to: \"**/*.proto\"\n---\n- nit: a renumbered field.\n")
        cli._webhook_handler(URL)
        second = runtime[1]["scoped_rules"]
        assert _paths(second) == ["scoped/helm.md", "scoped/java.md", "scoped/proto.md"]
        assert second.files[1].body.sha256 != first.files[1].body.sha256

        _write(work, "other.md", HELM_FILE)
        monkeypatch.setenv("PRXREF_SCOPED_RULES", "other.md")
        cli._webhook_handler(URL)
        assert _paths(runtime[2]["scoped_rules"]) == ["other.md"]

        monkeypatch.delenv("PRXREF_SCOPED_RULES")
        cli._webhook_handler(URL)
        assert runtime[3]["scoped_rules"] is None

    def test_it_logs_bad_scoped_rules_and_reviews_nothing(self, runtime, monkeypatch, caplog):
        monkeypatch.setenv("PRXREF_SCOPED_RULES", "https://example.com/rules.md")
        with caplog.at_level(logging.ERROR, logger="prxref"):
            cli._webhook_handler(URL)
        errors = [str(r.exc_info[1]) for r in caplog.records if r.exc_info]
        assert errors == [
            "PRXREF_SCOPED_RULES: scoped rules must be local file or directory paths, not a URL: "
            "'https://example.com/rules.md'"
        ]
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
    def test_each_chunk_gets_its_own_rules_and_the_sweep_their_union(
        self, work, scoped_dir, monkeypatch, capsys,
    ):
        assert sys.modules["prxref.orchestrator"] is real_orchestrator
        diff = _added_file_diff("src/main/java/App.java", 12) + _added_file_diff("helm/Chart.yaml", 12)
        forge = FakeForge(diff=diff)
        llm = _RecordingLLM()
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: REF)
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: forge)
        _install_fake_module(monkeypatch, "prxref.llm_backends", create_llm_client=lambda cfg: llm)
        monkeypatch.setenv("PRXREF_CHUNK_MAX_FILES", "1")
        _write(work, "rules.md", ALWAYS_FILE)

        assert cli.main([
            "review", "--pr-url", URL, "--no-post", "--rules-file", "rules.md",
            "--scoped-rules", scoped_dir, "--format", "json",
        ]) == 0

        *chunks, sweep = llm.calls
        assert len(chunks) == 2
        java = next(system for system, user in chunks if "+++ b/src/main/java/App.java" in user)
        helm = next(system for system, user in chunks if "+++ b/helm/Chart.yaml" in user)
        assert ALWAYS_RULE in java and JAVA_RULE in java and HELM_RULE not in java
        assert ALWAYS_RULE in helm and HELM_RULE in helm and JAVA_RULE not in helm
        assert "`nit`" in helm and "`outofscope`" in helm
        assert sweep[0].index(HELM_RULE) < sweep[0].index(JAVA_RULE)

        record = json.loads(capsys.readouterr().out)["scoped_rules"]
        java_row = {"path": "scoped/java.md", "chars": len(JAVA_RULE)}
        helm_row = {"path": "scoped/helm.md", "chars": len(HELM_RULE)}
        assert record["max_chars"] == 24000
        assert [f["path"] for f in record["files"]] == ["scoped/helm.md", "scoped/java.md"]
        assert sorted(record["units"]["chunks"], key=str) == sorted([[java_row], [helm_row]], key=str)
        assert record["units"]["sweep"] == [helm_row, java_row]
