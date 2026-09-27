"""Issue #30, the request half: ask for, parse and validate code suggestions.

``PRXREF_SUGGESTIONS=on`` asks every chunk worker (never the sweep) for an
optional per-finding ``suggestion``, reads it off the reply, and clears the
ones :func:`prxref.quality.apply_suggestion_validation` finds unsafe while
keeping the finding. What is pinned:

- at the default ``off`` every worker and sweep prompt, through the renderers
  and through a real ``orchestrate_review`` run, is byte-identical to the
  goldens :mod:`tests.test_rule_prompt_slot` recorded before this key existed;
  a model that volunteers a suggestion is ignored, and the run record's
  ``suggestions`` is ``None``;
- ``on`` puts :data:`prxref.reviewer.SUGGESTION_REQUEST` and the example key
  into the worker prompt only, and a reply's suggestion parses onto the
  ``Finding``;
- one test per clear reason, plus the kept case, each keeping the finding;
- a kept suggestion survives the whole quality pipeline and is counted;
- a worker override without ``{suggestion_example}`` warns once when on and
  never when off;
- ``PRXREF_SUGGESTIONS=bogus`` is exit 2 naming the variable.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging

import pytest

from prxref import cli, config, orchestrator
from prxref.cli import main
from prxref.config import load_config
from prxref.forges.base import PRRef
from prxref.llm import ConfigError
from prxref.prompt_templates import (
    CONTEXT_MARKER,
    OPTIONAL_PLACEHOLDERS,
    PromptTemplates,
    TemplateFile,
    required_placeholders,
)
from prxref.quality import (
    MAX_SUGGESTION_CHARS,
    MAX_SUGGESTION_LINES,
    SUGGESTION_CLEAR_REASONS,
    apply_suggestion_validation,
)
from prxref.reviewer import (
    _SUGGESTION_EXAMPLE,
    NO_PROMPT_CONTEXT,
    SUGGESTION_REQUEST,
    PromptContext,
    load_prompt,
    parse_suggestion,
    review_chunk,
    review_systemic,
)
from prxref.triage import Finding, parse_unified_diff
from tests.test_cli import _install_fake_module
from tests.test_orchestrator import REF, FakeForge, _added_file_diff, make_pr
from tests.test_rule_prompt_slot import BASE_GOLDEN, MINI_DIFF, _example, _Recorder, _sweep, _worker

ON = PromptContext(suggestion_request=SUGGESTION_REQUEST)
_REF_CLI = PRRef(
    forge="github", host="github.com", owner="acme", repo="widget", number=7,
    url="https://github.com/acme/widget/pull/7",
)

CALC_DIFF = (
    "diff --git a/src/calc.py b/src/calc.py\n"
    "--- a/src/calc.py\n"
    "+++ b/src/calc.py\n"
    "@@ -1,3 +1,5 @@\n"
    " def ratio(total, size):\n"
    "-    return total / size\n"
    "+    if size is None:\n"
    "+        return 0\n"
    "+    return total / size\n"
    " \n"
    "@@ -20,3 +21,3 @@\n"
    " def other():\n"
    "-    return 1\n"
    "+    return 2\n"
    " \n"
)
CALC_FILES = parse_unified_diff(CALC_DIFF)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _finding(line: int = 3, suggestion: str | None = "        return 0.0", end: int = 0, **extra) -> Finding:
    return Finding(
        file="src/calc.py", line=line, severity="warning", confidence=0.9,
        title="Zero size returns an int", body="ratio returns 0 for a None size.",
        suggestion=suggestion, suggestion_end_line=end, **extra,
    )


def _validate(finding: Finding, model_line: int | None = None) -> tuple[Finding, str | None]:
    lines = None if model_line is None else [model_line]
    (out,), (reason,) = apply_suggestion_validation([finding], CALC_FILES, model_lines=lines)
    return out, reason


def _reply(**finding) -> str:
    base = {
        "file": "src/app.py", "line": 2, "severity": "warning", "confidence": 0.8,
        "title": "t", "body": "b",
    }
    return json.dumps({"findings": [{**base, **finding}]})


class TestOffIsTheBase:
    @pytest.mark.parametrize("unit, render", [("worker", _worker), ("sweep", _sweep)])
    def test_the_default_prompts_are_byte_identical_to_the_base(self, unit, render):
        system, user = render(NO_PROMPT_CONTEXT)
        assert (_sha(system), _sha(user)) == BASE_GOLDEN[f"{unit}/off"]

    @pytest.mark.parametrize("knobs", [{}, {"suggestions": "off"}])
    def test_a_real_run_sends_the_base_prompts_and_records_null(self, knobs):
        llm = _Recorder(_reply(line=3, suggestion="data 3 fixed"))
        res = orchestrator.orchestrate_review(
            FakeForge(diff=_added_file_diff("src/app.py", 20)), REF, llm, post=False, **knobs,
        )
        prompts = sorted(llm.prompts)
        got = {f"orchestrator/{i}": (_sha(s), _sha(u)) for i, (s, u) in enumerate(prompts)}
        assert got == {k: v for k, v in BASE_GOLDEN.items() if k.startswith("orchestrator/")}
        assert res["suggestions"] is None
        assert cli._build_json_result(res)["suggestions"] is None
        assert all(f.suggestion is None for f in res["findings_active"] + res["findings_dropped"])

    def test_the_parser_ignores_a_volunteered_suggestion(self):
        llm = _Recorder(_reply(suggestion="import sys as system", suggestion_end_line=2))
        findings, _meta = review_chunk(llm, parse_unified_diff(MINI_DIFF), prompt_context=NO_PROMPT_CONTEXT)
        assert [(f.suggestion, f.suggestion_end_line) for f in findings] == [(None, 0)]

    def test_the_default_keyword_is_off(self):
        import inspect

        default = inspect.signature(orchestrator.orchestrate_review).parameters["suggestions"].default
        assert default == "off"

    def test_an_unknown_mode_is_a_value_error(self):
        with pytest.raises(ValueError, match="suggestions"):
            orchestrator.orchestrate_review(FakeForge(diff=MINI_DIFF), REF, _Recorder(), suggestions="yes")


class TestOnAsksTheWorkerOnly:
    def test_the_worker_system_ends_with_the_request(self):
        system, _user = _worker(ON)
        assert system.endswith(SUGGESTION_REQUEST)
        assert system.startswith(_worker(NO_PROMPT_CONTEXT)[0])

    def test_the_worker_example_shows_the_suggestion_key(self):
        _system, user = _worker(ON)
        assert "suggestion" in _example(user)
        assert _SUGGESTION_EXAMPLE in user
        off_user = _worker(NO_PROMPT_CONTEXT)[1]
        assert user.replace(_SUGGESTION_EXAMPLE, "", 1) == off_user

    def test_the_sweep_prompt_is_untouched(self):
        assert _sweep(ON) == _sweep(NO_PROMPT_CONTEXT)
        assert SUGGESTION_REQUEST not in "".join(_sweep(ON))

    def test_the_request_after_the_rule_request(self):
        ctx = PromptContext(rule_request="## Rule names\n\nR", suggestion_request=SUGGESTION_REQUEST)
        system, _ = _worker(ctx)
        assert system.index("## Rule names") < system.index("## Code suggestions")

    def test_the_slot_is_optional_and_worker_only(self):
        assert "suggestion_example" in OPTIONAL_PLACEHOLDERS
        assert "suggestion_example" not in required_placeholders("worker")
        assert load_prompt("worker.md").count("{rule_example}{suggestion_example}") == 1
        assert "{suggestion_example}" not in load_prompt("systemic.md")

    def test_the_request_names_the_line_cap(self):
        assert f"at most {MAX_SUGGESTION_LINES} lines" in SUGGESTION_REQUEST
        assert "```" not in SUGGESTION_REQUEST

    def test_a_reply_suggestion_parses_onto_the_finding(self):
        llm = _Recorder(_reply(suggestion="import sys\nimport os", suggestion_end_line=3))
        (finding,), _meta = review_chunk(llm, parse_unified_diff(MINI_DIFF), prompt_context=ON)
        assert (finding.suggestion, finding.suggestion_end_line) == ("import sys\nimport os", 3)
        system, _user = llm.prompts[0]
        assert system.endswith(SUGGESTION_REQUEST)

    def test_the_sweep_never_reads_a_suggestion(self):
        llm = _Recorder(_reply(suggestion="x"))
        (finding,), _meta = review_systemic(llm, "digest", prompt_context=ON)
        assert finding.suggestion is None
        assert SUGGESTION_REQUEST not in llm.prompts[0][0]

    @pytest.mark.parametrize("raw, want", [
        ({"suggestion": "x"}, ("x", 0)),
        ({"suggestion": ""}, ("", 0)),
        ({"suggestion": "x", "suggestion_end_line": 4}, ("x", 4)),
        ({"suggestion": "x", "suggestion_end_line": -1}, ("x", 0)),
        ({"suggestion": "x", "suggestion_end_line": "4"}, ("x", 0)),
        ({"suggestion": "x", "suggestion_end_line": 4.0}, ("x", 0)),
        ({"suggestion": "x", "suggestion_end_line": True}, ("x", 0)),
        ({"suggestion": 7, "suggestion_end_line": 4}, (None, 0)),
        ({"suggestion": ["x"]}, (None, 0)),
        ({"suggestion": None}, (None, 0)),
        ({}, (None, 0)),
    ])
    def test_the_parse_rules(self, raw, want):
        assert parse_suggestion(raw) == want

    def test_a_dict_shaped_stub_keeps_it_only_when_on(self):
        item = {"file": "a.py", "line": 1, "severity": "warning", "confidence": 0.9,
                "title": "t", "body": "b", "suggestion": "y", "suggestion_end_line": 2}
        on = orchestrator._coerce_finding(item, accept_suggestion=True)
        off = orchestrator._coerce_finding(item)
        assert (on.suggestion, on.suggestion_end_line) == ("y", 2)
        assert (off.suggestion, off.suggestion_end_line) == (None, 0)

    def test_the_followup_resend_carries_the_request(self):
        llm = _Recorder()
        orchestrator._invoke_chunk(
            llm, parse_unified_diff(MINI_DIFF), make_pr(), None, None,
            prompt_context=ON, extra_blocks="### Follow-up definitions\n\nX",
        )
        (system, user), = llm.prompts
        assert system.endswith(SUGGESTION_REQUEST)
        assert "### Follow-up definitions" in user


class TestValidation:
    def test_a_valid_suggestion_is_kept(self):
        finding = _finding()
        out, reason = _validate(finding, model_line=3)
        assert reason is None
        assert out is finding

    def test_a_multi_line_suggestion_inside_one_hunk_is_kept(self):
        text = "    if not size:\n        return 0\n    return total / size"
        out, reason = _validate(_finding(line=2, end=4, suggestion=text))
        assert reason is None
        assert out.suggestion_end_line == 4

    def test_a_deletion_is_kept(self):
        out, reason = _validate(_finding(line=2, end=3, suggestion=""))
        assert reason is None
        assert out.suggestion == ""

    def _cleared(self, finding: Finding, want: str, model_line: int | None = None) -> None:
        out, reason = _validate(finding, model_line=model_line)
        assert reason == want
        assert (out.suggestion, out.suggestion_end_line) == (None, 0)
        assert out.drop_reason is None
        assert dataclasses.replace(out, suggestion=finding.suggestion,
                                   suggestion_end_line=finding.suggestion_end_line) == finding

    def test_grouped(self):
        self._cleared(_finding(locations=(("src/calc.py", 3), ("src/calc.py", 4))), "grouped")

    def test_line_moved(self):
        self._cleared(_finding(), "line_moved", model_line=2)

    def test_file_level(self):
        self._cleared(_finding(line=0), "file_level")

    def test_range_reversed(self):
        self._cleared(_finding(line=3, end=2), "range")

    def test_range_too_long(self):
        self._cleared(_finding(line=1, end=1 + MAX_SUGGESTION_LINES), "range")

    def test_outside_hunk_across_two_hunks(self):
        self._cleared(_finding(line=4, end=21), "outside_hunk")

    def test_outside_hunk_past_the_diff(self):
        self._cleared(_finding(line=10), "outside_hunk")

    def test_outside_hunk_in_another_file(self):
        self._cleared(dataclasses.replace(_finding(), file="src/missing.py"), "outside_hunk")

    def test_fence(self):
        self._cleared(_finding(suggestion="```python\n        return 0.0\n```"), "fence")

    def test_too_long(self):
        self._cleared(_finding(suggestion="x" * (MAX_SUGGESTION_CHARS + 1)), "too_long")

    @pytest.mark.parametrize("text", ["        return 0", "        return 0\n"])
    def test_no_op(self, text):
        self._cleared(_finding(suggestion=text), "no_op")

    def test_the_first_failing_rule_wins(self):
        finding = _finding(line=0, suggestion="```", locations=(("src/calc.py", 3),))
        assert _validate(finding, model_line=5)[1] == "grouped"

    def test_the_reasons_are_the_documented_order(self):
        assert SUGGESTION_CLEAR_REASONS == (
            "grouped", "line_moved", "file_level", "range", "outside_hunk", "fence", "too_long", "no_op",
        )

    def test_a_finding_without_a_suggestion_passes_through(self):
        finding = _finding(suggestion=None)
        assert _validate(finding, model_line=9) == (finding, None)

    def test_a_model_line_count_mismatch_raises(self):
        with pytest.raises(ValueError, match="model_lines"):
            apply_suggestion_validation([_finding()], CALC_FILES, model_lines=[])


APP_DIFF = _added_file_diff("src/app.py", 20)
KEPT = {"file": "src/app.py", "line": 3, "severity": "warning", "confidence": 0.9,
        "title": "data 3 is never validated", "body": "The loader trusts data 3 as-is.",
        "suggestion": "data 3 validated"}
FENCED = {"file": "src/app.py", "line": 7, "severity": "warning", "confidence": 0.9,
          "title": "data 7 is logged in full", "body": "data 7 reaches the log unredacted.",
          "suggestion": "```\ndata 7 redacted\n```"}
LOW = {"file": "src/app.py", "line": 9, "severity": "warning", "confidence": 0.1,
       "title": "data 9 may overflow", "body": "data 9 has no bound.",
       "suggestion": "```\ndata 9 bounded\n```"}


class TestCarryThroughTheOrchestrator:
    def _run(self, **knobs):
        llm = _Recorder(json.dumps({"findings": [KEPT, FENCED, LOW]}))
        forge = FakeForge(diff=APP_DIFF)
        res = orchestrator.orchestrate_review(forge, REF, llm, post=False, max_workers=1, **knobs)
        return res, llm

    def test_a_kept_suggestion_survives_every_pass(self):
        res, _llm = self._run(suggestions="on")
        by_line = {f.line: f for f in res["findings_active"]}
        assert by_line[3].suggestion == "data 3 validated"
        assert by_line[3].suggestion_end_line == 0
        assert by_line[7].suggestion is None

    def test_the_record_counts_active_findings_only(self):
        res, _llm = self._run(suggestions="on")
        record = res["suggestions"]
        assert record["kept"] == 1
        assert list(record["cleared"]) == list(SUGGESTION_CLEAR_REASONS)
        assert record["cleared"]["fence"] == 1
        assert sum(record["cleared"].values()) == 1
        assert any(f.line == 9 for f in res["findings_dropped"])
        assert cli._build_json_result(res)["suggestions"] == record

    def test_sweep_findings_never_carry_one(self):
        res, llm = self._run(suggestions="on")
        sweep_prompts = [s for s, _u in llm.prompts if SUGGESTION_REQUEST not in s]
        assert len(sweep_prompts) == 1
        assert all(f.suggestion is None for f in res["findings_dropped"])

    def test_grouping_clears_the_representatives_suggestion(self):
        group = [
            {**KEPT, "rule": "validate-input"},
            {**KEPT, "line": 5, "title": "data 5 is never validated", "body": "The loader trusts data 5.",
             "suggestion": "data 5 validated", "rule": "validate-input"},
        ]
        llm = _Recorder(json.dumps({"findings": group}))
        res = orchestrator.orchestrate_review(
            FakeForge(diff=APP_DIFF), REF, llm, post=False, max_workers=1,
            suggestions="on", group_findings=True,
        )
        (rep,) = [f for f in res["findings_active"] if f.locations]
        assert rep.suggestion is None
        assert res["suggestions"]["cleared"]["grouped"] == 1
        assert res["suggestions"]["kept"] == 0

    def test_an_error_exit_carries_the_zero_record(self):
        forge = FakeForge(diff=APP_DIFF)
        forge.fail.add("get_pr")
        res = orchestrator.orchestrate_review(forge, REF, _Recorder(), post=False, suggestions="on")
        assert res["suggestions"] == {"kept": 0, "cleared": dict.fromkeys(SUGGESTION_CLEAR_REASONS, 0)}

    @pytest.mark.parametrize("env, want", [(None, "off"), ("on", "on"), ("off", "off")])
    def test_the_cli_passes_the_config_value(self, monkeypatch, env, want):
        calls: list[dict] = []

        def fake_orchestrate_review(**kwargs):
            calls.append(kwargs)
            return {"verdict": "commented", "findings_active": [], "findings_dropped": []}

        _install_fake_module(monkeypatch, "prxref.llm_backends", create_llm_client=lambda cfg: object())
        _install_fake_module(monkeypatch, "prxref.orchestrator", orchestrate_review=fake_orchestrate_review)
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: _REF_CLI)
        if env is None:
            monkeypatch.delenv("PRXREF_SUGGESTIONS", raising=False)
        else:
            monkeypatch.setenv("PRXREF_SUGGESTIONS", env)
        assert main(["review", "--pr-url", _REF_CLI.url, "--no-post"]) == 0
        (call,) = calls
        assert call["suggestions"] == want


def _templates(worker_text: str) -> PromptTemplates:
    file = TemplateFile(
        name="worker", path="team-prompts/worker.md", text=worker_text,
        sha256=hashlib.sha256(worker_text.encode("utf-8")).hexdigest(), chars=len(worker_text),
    )
    return PromptTemplates(
        dir="team-prompts", worker=worker_text, systemic=load_prompt("systemic.md"),
        summary=load_prompt("summary.md"), overrides=(file,),
    )


def _without_slot() -> str:
    head, marker, tail = load_prompt("worker.md").partition(CONTEXT_MARKER)
    return head + marker + tail.replace("{suggestion_example}", "")


class TestCustomTemplate:
    def _warnings(self, caplog) -> list[str]:
        return [r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING and "{suggestion_example}" in r.getMessage()]

    def _run(self, caplog, templates, mode):
        llm = _Recorder()
        with caplog.at_level(logging.WARNING, logger="prxref"):
            orchestrator.orchestrate_review(
                FakeForge(diff=APP_DIFF), REF, llm, post=False, prompts=templates, suggestions=mode,
            )
        return llm

    def test_a_worker_override_without_the_slot_warns_once_when_on(self, caplog):
        llm = self._run(caplog, _templates(_without_slot()), "on")
        (message,) = self._warnings(caplog)
        assert "team-prompts/worker.md" in message
        assert "PRXREF_SUGGESTIONS" in message
        assert any(s.endswith(SUGGESTION_REQUEST) for s, _u in llm.prompts)

    def test_it_never_warns_when_off(self, caplog):
        self._run(caplog, _templates(_without_slot()), "off")
        assert self._warnings(caplog) == []

    def test_an_override_with_the_slot_does_not_warn(self, caplog):
        self._run(caplog, _templates(load_prompt("worker.md")), "on")
        assert self._warnings(caplog) == []


class TestConfig:
    def test_the_default_is_off(self):
        assert config._DEFAULTS["suggestions"] == "off"
        assert load_config()["suggestions"] == "off"

    @pytest.mark.parametrize("raw", ["off", "on"])
    def test_each_legal_value_loads(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_SUGGESTIONS", raw)
        assert load_config()["suggestions"] == raw

    def test_the_vocabulary_is_declared_in_the_choice_table(self):
        assert config._CHOICE_KEYS["suggestions"] == frozenset({"off", "on"})

    @pytest.mark.parametrize("raw", ["bogus", "true", "ON", "1"])
    def test_a_value_outside_the_vocabulary_is_a_config_error(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_SUGGESTIONS", raw)
        with pytest.raises(ConfigError, match="PRXREF_SUGGESTIONS"):
            load_config()

    def test_bogus_exits_2_naming_the_variable(self, monkeypatch, capsys):
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: _REF_CLI)
        monkeypatch.setenv("PRXREF_SUGGESTIONS", "bogus")
        rc = main(["review", "--pr-url", _REF_CLI.url, "--no-post"])
        assert rc == 2
        _, err = capsys.readouterr()
        assert "configuration error" in err
        assert "PRXREF_SUGGESTIONS" in err
