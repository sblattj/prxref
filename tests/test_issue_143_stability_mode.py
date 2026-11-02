"""Issue #143: an opt-in stability mode that runs chunks N times and reports agreement.

The failure case is the issue's first row: one commit, identical input tokens,
and the only ``error`` is gone on one of three runs. With ``PRXREF_STABILITY_PASSES``
above 1 the chunk workers run that many times over the same chunks, the passes
are merged per chunk before any quality pass, and ``PRXREF_STABILITY_MIN_AGREE``
sets how many passes must raise a finding. Every orchestrator test runs the real
pipeline with a scripted model: the script answers by call order, so pass 1 can
raise what pass 2 does not.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from prxref import cli, config, evals, stability
from prxref.cli import _build_json_result, _fail_on_exit
from prxref.config import load_config
from prxref.llm import ConfigError, InvokeResult
from prxref.llm_backends import OpenAICompatClient
from prxref.orchestrator import orchestrate_review
from prxref.stability import STABILITY_DROP_PREFIX
from prxref.triage import Finding
from tests.test_issue_138_require_models import _resp
from tests.test_orchestrator import REF, FakeForge, multi_chunk_diff

B1 = "src/big1.py"
B2 = "src/big2.py"
ROOT = Path(__file__).resolve().parent.parent


def _raw(file, line, title, body, severity="warning", confidence=0.9):
    return {
        "file": file, "line": line, "severity": severity, "confidence": confidence,
        "title": title, "body": body,
    }


def _limit(line=10, severity="error", file=B1):
    return _raw(
        file, line, "Use the same HTTP status for over-limit data across services",
        "`LimitExceededException` maps `data` to `HttpStatus.CONFLICT` here.", severity,
    )


def _limit_reworded(line=12, severity="error", file=B1):
    return _raw(
        file, line, "Over-limit data returns 409 here but 400 in the other service",
        "`LimitExceededException` sends `data` to `HttpStatus.CONFLICT` and the peer says 400.",
        severity,
    )


def _timeout(file=B1):
    return _raw(
        file, 12, "Catch block swallows `TimeoutException` from `client.fetch`",
        "`client.fetch` raises `TimeoutException` and the handler reads `response.body` of `data` "
        "as if the call worked, so an outage looks like an empty result.",
    )


def _close(file=B1):
    return _raw(
        file, 13, "`client.close` is skipped when `client.fetch` throws",
        "`client.close` runs after `client.fetch` and `response.body` of `data`, so a throw "
        "leaves the socket open. Move it to a finally block.",
    )


class ScriptLLM:
    """A model that answers each chunk by how many times it has been asked.

    ``script[path]`` lists one answer per call for the chunk whose first file is
    ``path``: a findings list, or an exception to raise. Every call also records
    the seed the client carried, so a pass's seed is observable. The counters are
    shared by reference, so a shallow copy of this client (what a per-pass seed
    makes) keeps counting in the same place.
    """

    def __init__(self, script, seed=None):
        self.script = script
        self.seed = seed
        self.calls = {}
        self.seen = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        path = json.loads(user)[0]
        with self._lock:
            n = self.calls.get(path, 0)
            self.calls[path] = n + 1
            self.seen.append((path, n, self.seed))
        answers = self.script.get(path, [])
        answer = answers[n] if n < len(answers) else []
        if isinstance(answer, Exception):
            raise answer
        return InvokeResult(
            text=json.dumps({"findings": answer}), input_tokens=100, output_tokens=50,
            model="test-model-1", backend="fake", elapsed_ms=1,
        )


def _review(llm, passes, agree=1, *, post=True, **kw):
    forge = FakeForge(diff=multi_chunk_diff(2))
    res = orchestrate_review(
        forge, REF, llm, post=post, max_workers=1,
        stability_passes=passes, stability_min_agree=agree, **kw,
    )
    return res, forge


def _comments(forge):
    return [c for batch in forge.inline_batches for c in batch]


@pytest.mark.usefixtures("contract_stubs")
class TestTheIssuesFirstRow:
    """An error raised in two of three passes is posted, and says so."""

    SCRIPT = {B1: [[_limit()], [_limit()], []], B2: [[], [], []]}

    def test_union_posts_the_error_seen_twice(self):
        res, forge = _review(ScriptLLM(self.SCRIPT), 3, 1)
        (kept,) = res["findings_active"]
        assert (kept.severity, kept.agreement, kept.passes_ok) == ("error", 2, 3)
        (comment,) = _comments(forge)
        assert "Seen in 2 of 3 passes." in comment.body
        assert res["verdict"] == "Request-Changes"

    def test_the_summary_states_the_passes_and_the_bar(self):
        _, forge = _review(ScriptLLM(self.SCRIPT), 3, 2)
        (summary,) = forge.summaries
        assert "Stability mode: 3 review passes; a finding needs 2 of them to be listed." in summary

    def test_a_bar_of_two_still_posts_it(self):
        res, forge = _review(ScriptLLM(self.SCRIPT), 3, 2)
        assert [f.agreement for f in res["findings_active"]] == [2]
        assert "Seen in 2 of 3 passes." in _comments(forge)[0].body

    def test_an_error_in_one_of_three_is_dropped_but_kept_in_the_record(self):
        script = {B1: [[_limit()], [], []], B2: [[], [], []]}
        res, forge = _review(ScriptLLM(script), 3, 2)
        assert res["findings_active"] == []
        assert _comments(forge) == []
        (gone,) = res["findings_dropped"]
        assert gone.drop_reason.startswith(STABILITY_DROP_PREFIX)
        assert gone.drop_reason == "stability: seen in 1 of 3 passes, needs 2"
        (row,) = res["stability"]["dropped"]
        assert (row["chunk"], row["file"], row["agreement"], row["needed"]) == (1, B1, 1, 2)

    def test_a_dropped_error_does_not_trip_the_error_gate(self):
        script = {B1: [[_limit()], [], []], B2: [[], [], []]}
        res, _ = _review(ScriptLLM(script), 3, 2)
        code, note = _fail_on_exit(res, "error")
        assert (code, note) == (0, None)

    def test_the_same_error_in_every_pass_trips_it(self):
        script = {B1: [[_limit()], [_limit()], [_limit()]], B2: [[], [], []]}
        res, _ = _review(ScriptLLM(script), 3, 3)
        code, _note = _fail_on_exit(res, "error")
        assert code == 1


@pytest.mark.usefixtures("contract_stubs")
class TestMerging:
    def test_a_reworded_finding_across_passes_is_one_finding(self):
        script = {
            B1: [[_limit(10, "warning")], [_limit_reworded(12, "warning")], [_limit(11, "warning")]],
            B2: [[], [], []],
        }
        res, forge = _review(ScriptLLM(script), 3, 3)
        (kept,) = res["findings_active"]
        assert kept.agreement == 3
        assert len(_comments(forge)) == 1

    def test_two_distinct_bugs_at_nearby_lines_stay_two(self):
        script = {B1: [[_timeout(), _close()]] * 3, B2: [[], [], []]}
        res, _ = _review(ScriptLLM(script), 3, 3)
        assert sorted(f.line for f in res["findings_active"]) == [12, 13]
        assert {f.agreement for f in res["findings_active"]} == {3}

    def test_one_bug_never_borrows_agreement_from_its_neighbour(self):
        script = {B1: [[_timeout(), _close()], [_timeout()], [_timeout()]], B2: [[], [], []]}
        res, _ = _review(ScriptLLM(script), 3, 2)
        (kept,) = res["findings_active"]
        assert (kept.line, kept.agreement) == (12, 3)
        (gone,) = res["findings_dropped"]
        assert (gone.line, gone.drop_reason.startswith(STABILITY_DROP_PREFIX)) == (13, True)

    def test_findings_of_one_pass_never_merge_with_each_other(self):
        unit = {"error": "", "input_tokens": 1, "output_tokens": 1, "model": "m",
                "elapsed_ms": 1, "cost_usd": None, "cost_source": ""}
        one = Finding(B1, 10, "warning", 0.9, "Same title for this bug", "`foo.bar` is wrong here.")
        two = Finding(B1, 11, "warning", 0.9, "Same title for this bug", "`foo.bar` is wrong here.")
        merged, dropped, _ = stability.merge_chunk(
            [{**unit, "findings": [one, two]}, {**unit, "findings": []}], min_agree=1,
        )
        assert [f.line for f in merged["findings"]] == [10, 11]
        assert dropped == []

    def test_severity_differs_so_the_highest_is_posted(self):
        script = {
            B1: [[_limit(10, "warning")], [_limit(10, "error")], [_limit(10, "warning")]],
            B2: [[], [], []],
        }
        res, forge = _review(ScriptLLM(script), 3, 1)
        (kept,) = res["findings_active"]
        assert (kept.severity, kept.agreement) == ("error", 3)
        assert "Seen in 3 of 3 passes." in _comments(forge)[0].body

    def test_the_merge_is_deterministic_for_equal_severity_and_confidence(self):
        a = Finding(B1, 10, "warning", 0.9, "Same title for this bug", "`foo.bar` is wrong here.")
        b = Finding(B1, 10, "warning", 0.9, "Same title for this bug", "`foo.bar` is wrong here.")
        runs = [
            stability.merge_chunk(
                [{"findings": [a], "error": "", "input_tokens": 1, "output_tokens": 1,
                  "model": "m", "elapsed_ms": 1, "cost_usd": None, "cost_source": ""},
                 {"findings": [b], "error": "", "input_tokens": 1, "output_tokens": 1,
                  "model": "m", "elapsed_ms": 1, "cost_usd": None, "cost_source": ""}],
                min_agree=1,
            )[0]["findings"]
            for _ in range(2)
        ]
        assert runs[0] == runs[1]
        assert runs[0][0].agreement == 2


@pytest.mark.usefixtures("contract_stubs")
class TestAFailedPass:
    def test_a_failed_pass_lowers_the_bar_so_the_finding_stays(self):
        script = {
            B1: [[_limit()], RuntimeError("boom"), [_limit()]],
            B2: [[], [], []],
        }
        res, forge = _review(ScriptLLM(script), 3, 2)
        (kept,) = res["findings_active"]
        assert (kept.agreement, kept.passes_ok) == (2, 2)
        assert "Seen in 2 of 2 passes." in _comments(forge)[0].body
        assert res["chunks_failed"] == 0

    def test_the_bar_shrinks_to_the_ok_passes_when_fewer_than_k_remain(self):
        script = {
            B1: [[_limit()], RuntimeError("boom"), RuntimeError("boom")],
            B2: [[], [], []],
        }
        res, _ = _review(ScriptLLM(script), 3, 2)
        (kept,) = res["findings_active"]
        assert (kept.agreement, kept.passes_ok) == (1, 1)
        assert res["findings_dropped"] == []

    def test_a_chunk_failing_every_pass_counts_as_failed_and_the_review_exits_zero(self):
        script = {
            B1: [[_limit()], [_limit()], [_limit()]],
            B2: [RuntimeError("boom")] * 3,
        }
        res, _ = _review(ScriptLLM(script), 3, 2)
        assert res["chunks_failed"] == 1
        assert res["verdict"] in {"Request-Changes", "Incomplete"}
        assert [f.file for f in res["findings_active"]] == [B1]
        assert _fail_on_exit(res, "never")[0] == 0

    def test_every_chunk_failing_every_pass_is_the_total_failure_it_always_was(self):
        script = {B1: [RuntimeError("boom")] * 3, B2: [RuntimeError("boom")] * 3}
        res, _ = _review(ScriptLLM(script), 3, 2)
        assert res["verdict"] == "Error"
        assert _fail_on_exit(res, "never")[0] == 0


@pytest.mark.usefixtures("contract_stubs")
class TestSeeds:
    def test_each_pass_sends_the_seed_plus_its_index_and_the_client_is_untouched(self):
        llm = ScriptLLM({}, seed=100)
        res, _ = _review(llm, 3, 1)
        assert [seed for _, _, seed in llm.seen] == [100, 100, 101, 101, 102, 102]
        assert llm.seed == 100
        assert res["stability"]["seeds"] == [100, 101, 102]

    def test_the_real_payload_carries_each_pass_seed(self):
        sent = []

        class Session:
            def post(self, url, json=None, headers=None, timeout=None, stream=None):
                sent.append(json)
                return _resp(model="m", text='{"findings": []}')

        client = OpenAICompatClient(
            "http://llm.example/v1", "k", ["m"], session=Session(), seed=7,
        )
        _review(client, 3, 1)
        assert [p["seed"] for p in sent] == [7, 7, 8, 8, 9, 9]
        assert client.seed == 7

    def test_seed_off_sends_no_seed_in_any_request(self):
        sent = []

        class Session:
            def post(self, url, json=None, headers=None, timeout=None, stream=None):
                sent.append(json)
                return _resp(model="m", text='{"findings": []}')

        client = OpenAICompatClient(
            "http://llm.example/v1", "k", ["m"], session=Session(), seed=None,
        )
        res, _ = _review(client, 3, 1)
        assert len(sent) == 6
        assert all("seed" not in p for p in sent)
        assert res["stability"]["seeds"] == [None, None, None]


@pytest.mark.usefixtures("contract_stubs")
class TestAccounting:
    def test_tokens_sum_across_passes_and_the_sweep_runs_once(self, monkeypatch):
        from tests.test_orchestrator import _contract_review_systemic

        sweeps = []

        def counting(*a, **kw):
            sweeps.append(1)
            return _contract_review_systemic(*a, **kw)

        monkeypatch.setattr("prxref.orchestrator.reviewer.review_systemic", counting)
        llm = ScriptLLM({})
        res, forge = _review(llm, 3, 1)
        assert sum(llm.calls.values()) == 6
        assert len(sweeps) == 1
        assert (res["input_tokens"], res["output_tokens"]) == (600, 300)
        assert "900" in forge.summaries[0]

    def test_later_passes_get_their_own_trace_labels(self, monkeypatch, tmp_path):
        from prxref import orchestrator

        real = orchestrator.reviewer.review_chunk
        labels = []

        def spy(*a, **kw):
            labels.append(kw.get("trace_label"))
            return real(*a, **kw)

        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", spy)
        _review(ScriptLLM({}), 2, 1, trace_dir=str(tmp_path))
        assert sorted(labels) == ["chunk0", "chunk0.pass1", "chunk1", "chunk1.pass1"]
        labels.clear()
        _review(ScriptLLM({}), 1, 1, trace_dir=str(tmp_path))
        assert sorted(labels) == ["chunk0", "chunk1"]

    def test_the_record_counts_findings_per_pass_and_per_chunk(self):
        script = {B1: [[_limit()], RuntimeError("boom"), []], B2: [[], [], [_timeout(B2)]]}
        res, _ = _review(ScriptLLM(script), 3, 1)
        assert res["stability"]["passes"] == 3
        assert res["stability"]["min_agree"] == 1
        assert res["stability"]["findings_per_pass"] == [[1, 0], [None, 0], [0, 1]]


@pytest.mark.usefixtures("contract_stubs")
class TestOff:
    SCRIPT = {B1: [[_limit(10, "warning")]], B2: [[_timeout(B2)]]}

    def test_one_pass_is_the_run_without_the_keys(self):
        base_forge = FakeForge(diff=multi_chunk_diff(2))
        base = orchestrate_review(base_forge, REF, ScriptLLM(self.SCRIPT), post=True, max_workers=1)
        res, forge = _review(ScriptLLM(self.SCRIPT), 1, 1)
        assert "stability" not in res
        assert set(res) == set(base)
        assert [c.body for c in _comments(forge)] == [c.body for c in _comments(base_forge)]
        assert forge.summaries == base_forge.summaries
        assert all("Seen in" not in c.body for c in _comments(forge))
        assert all(f.agreement == 0 for f in res["findings_active"])

    def test_one_pass_makes_one_call_per_chunk(self):
        llm = ScriptLLM(self.SCRIPT, seed=5)
        _review(llm, 1, 1)
        assert llm.seen == [(B1, 0, 5), (B2, 0, 5)]

    def test_the_json_record_has_no_stability_keys_when_off(self):
        res, _ = _review(ScriptLLM(self.SCRIPT), 1, 1)
        payload = _build_json_result(res)
        assert "stability" not in payload
        assert all("agreement" not in row for row in payload["findings"])

    def test_the_json_record_carries_them_when_on(self):
        res, _ = _review(ScriptLLM({B1: [[_limit()], [_limit()]], B2: [[], []]}), 2, 1)
        payload = _build_json_result(res)
        assert payload["stability"]["passes"] == 2
        (row,) = payload["findings"]
        assert (row["agreement"], row["passes_ok"]) == (2, 2)


class TestConfig:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        for name in ("PRXREF_STABILITY_PASSES", "PRXREF_STABILITY_MIN_AGREE"):
            monkeypatch.delenv(name, raising=False)

    def test_defaults_are_off(self):
        cfg = load_config()
        assert (cfg["stability_passes"], cfg["stability_min_agree"]) == (1, 1)

    def test_valid_values_load(self, monkeypatch):
        monkeypatch.setenv("PRXREF_STABILITY_PASSES", "3")
        monkeypatch.setenv("PRXREF_STABILITY_MIN_AGREE", "2")
        cfg = load_config()
        assert (cfg["stability_passes"], cfg["stability_min_agree"]) == (3, 2)

    @pytest.mark.parametrize(
        ("env", "value"),
        [
            ("PRXREF_STABILITY_PASSES", "0"),
            ("PRXREF_STABILITY_PASSES", "6"),
            ("PRXREF_STABILITY_PASSES", "x"),
            ("PRXREF_STABILITY_MIN_AGREE", "0"),
            ("PRXREF_STABILITY_MIN_AGREE", "6"),
        ],
    )
    def test_out_of_range_names_the_variable(self, monkeypatch, env, value):
        monkeypatch.setenv(env, value)
        with pytest.raises(ConfigError, match=env):
            load_config()

    def test_a_bar_above_the_passes_names_the_variable(self, monkeypatch):
        monkeypatch.setenv("PRXREF_STABILITY_PASSES", "3")
        monkeypatch.setenv("PRXREF_STABILITY_MIN_AGREE", "4")
        with pytest.raises(ConfigError, match="PRXREF_STABILITY_MIN_AGREE"):
            load_config()

    def test_a_bar_with_the_default_single_pass_is_an_error_too(self, monkeypatch):
        monkeypatch.setenv("PRXREF_STABILITY_MIN_AGREE", "2")
        with pytest.raises(ConfigError, match="PRXREF_STABILITY_MIN_AGREE"):
            load_config()

    @pytest.mark.parametrize("key", ["stability_passes", "stability_min_agree"])
    def test_a_config_file_may_not_set_either(self, tmp_path, key):
        path = tmp_path / ".prxref.toml"
        path.write_text(f"{key} = 2\n")
        with pytest.raises(ConfigError, match=key):
            load_config(config_file=path)

    def test_both_are_env_only(self):
        assert {"stability_passes", "stability_min_agree"} <= config.ENV_ONLY_KEYS
        assert not {"stability_passes", "stability_min_agree"} & config.FILE_KEYS

    def test_the_cli_exits_two_before_any_review_on_a_bad_value(self, monkeypatch):
        monkeypatch.setenv("PRXREF_STABILITY_PASSES", "6")
        assert cli.main(["review", "--pr-url", "https://github.com/o/r/pull/1", "--no-post"]) == 2


class TestEvals:
    def test_the_eval_pins_and_run_config_do_not_move(self):
        assert "stability_passes" not in evals.EVAL_PINS
        assert "stability_min_agree" not in evals.EVAL_PINS
        assert "stability_passes" not in evals.RUN_CONFIG_KEYS
        assert "stability_min_agree" not in evals.RUN_CONFIG_KEYS

    def test_the_docs_say_how_an_eval_reader_knows(self):
        text = (ROOT / "docs" / "evals.md").read_text()
        assert "PRXREF_STABILITY_PASSES" in text
        assert "stability" in text
