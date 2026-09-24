"""Issue #14 T6: the judge client, one judge call per case, the grade cache, judge cost and the stamp."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from types import SimpleNamespace

import pytest

from prxref import eval_judge, llm_backends, orchestrator, reviewer
from prxref.costs import parse_price_table
from prxref.eval_cases import EvalCase, ExpectedFinding
from prxref.eval_judge import (
    JUDGE_CACHE_DIRNAME,
    JUDGE_CACHE_VERSION,
    JudgeOutcome,
    build_judge_client,
    check_self_judging,
    judge_cache_dir,
    judge_case,
    judge_cost,
    judge_stamp,
)
from prxref.judge import (
    JUDGE_PROMPT_VERSION,
    Grade,
    assign_refs,
    build_judge_prompt,
    judge_cache_key,
    judge_prompt_sha,
    split_judge_prompt,
)
from prxref.llm import ConfigError, InvokeResult

JUDGE = "judge-m"
REVIEWER = "reviewer-m"

CFG = {
    "llm_backend": "openai-compat",
    "llm_base_url": "https://llm.example.com/v1",
    "llm_api_key": "placeholder-key",
    "llm_models": [REVIEWER],
    "llm_timeout": 30.0,
    "llm_temperature": 0.2,
    "llm_seed": 11,
    "llm_max_tokens": 4096,
    "price_table": {},
    "max_chunks": 8,
}

CASE = EvalCase(
    id="case-1",
    expected=(
        ExpectedFinding("H1", "src/a.py", 10, "error", text="Divides by zero when size is 0."),
        ExpectedFinding("H2", "src/b.py", 5, "warning", text="Token logged in clear."),
    ),
    diff_file="case-1.patch",
)

RECORD = {
    "findings": [
        {"file": "src/a.py", "line": 10, "severity": "error", "title": "Divide by zero",
         "body": "size may be 0.", "drop_reason": None},
        {"file": "src/a.py", "line": 11, "severity": "warning", "title": "Low confidence",
         "body": "gated out.", "drop_reason": "confidence"},
        {"file": "src/b.py", "line": 5, "severity": "error", "title": "Secret in log",
         "body": "token is logged.", "drop_reason": None},
    ],
    "sampling": {"temperature": 0.0, "seed": 3, "models": [REVIEWER]},
}

GOOD_REPLY = json.dumps({"grades": [
    {"human_id": "H1", "grade": "full", "ai_ref": "A1"},
    {"human_id": "H2", "grade": "partial", "ai_ref": "A2"},
]})
GOOD_GRADES = (Grade("H1", "full", "A1"), Grade("H2", "partial", "A2"))


class StubJudge:
    """A judge client that answers every call with ``text`` (or raises ``error``), recording each call."""

    def __init__(self, model: str = JUDGE, text: str = GOOD_REPLY, *, error: Exception | None = None,
                 cost_usd: float | None = None, cost_source: str = "",
                 input_tokens: int = 1000, output_tokens: int = 500) -> None:
        self.models = [model]
        self.temperature = 0.0
        self.seed = 7
        self.text = text
        self.error = error
        self.cost_usd = cost_usd
        self.cost_source = cost_source
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls: list[dict] = []

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=None):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens,
                           "json_mode": json_mode, "timeout_s": timeout_s})
        if self.error is not None:
            raise self.error
        return InvokeResult(
            text=self.text, model=self.models[0], backend="stub",
            input_tokens=self.input_tokens, output_tokens=self.output_tokens,
            cost_usd=self.cost_usd, cost_source=self.cost_source,
        )


@pytest.fixture
def factory(monkeypatch):
    """Patch the factory seam with one stub keyed on ``cfg["llm_models"]``, as the CLI's lookup sees it."""
    state = SimpleNamespace(built=[], clients={})

    def create(cfg=None, session=None):
        state.built.append(copy.deepcopy(cfg))
        model = cfg["llm_models"][0]
        return state.clients.setdefault(model, StubJudge(model))

    monkeypatch.setattr("prxref.llm_backends.create_llm_client", create)
    return state


def _key(model: str = JUDGE, case=CASE, record=RECORD) -> str:
    return judge_cache_key(judge_prompt_sha(), model, case, assign_refs(record["findings"]))


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


class TestBuildJudgeClient:
    def test_only_llm_models_changes(self, factory):
        before = copy.deepcopy(CFG)
        client = build_judge_client(CFG, JUDGE)
        assert factory.built == [{**before, "llm_models": [JUDGE]}]
        assert CFG == before
        assert client is factory.clients[JUDGE]

    def test_one_stub_serves_the_review_and_the_judge(self, factory):
        review = llm_backends.create_llm_client(CFG)
        judge = build_judge_client(CFG, JUDGE)
        assert review.models == [REVIEWER]
        assert judge.models == [JUDGE]
        assert [cfg["llm_models"] for cfg in factory.built] == [[REVIEWER], [JUDGE]]

    def test_the_real_factory_keeps_backend_endpoint_key_and_sampling(self):
        review = llm_backends.create_llm_client(CFG)
        judge = build_judge_client(CFG, JUDGE)
        assert type(judge) is type(review)
        assert judge.models == [JUDGE]
        assert (judge.base_url, judge.api_key, judge.temperature, judge.seed) == (
            review.base_url, review.api_key, review.temperature, review.seed)
        assert judge.seed == 11

    def test_an_uppercase_models_override_cannot_shadow_the_judge(self):
        judge = build_judge_client({**CFG, "LLM_MODELS": REVIEWER}, JUDGE)
        assert judge.models == [JUDGE]

    def test_the_judge_model_is_stripped(self, factory):
        build_judge_client(CFG, f"  {JUDGE} ")
        assert factory.built[0]["llm_models"] == [JUDGE]

    @pytest.mark.parametrize("model", ["", "   ", "judge-a,judge-b", None])
    def test_a_blank_or_chained_judge_model_is_a_config_error_naming_the_flag(self, factory, model):
        with pytest.raises(ConfigError, match="^--judge-model: "):
            build_judge_client(CFG, model)
        assert factory.built == []

    def test_an_empty_judge_model_would_otherwise_fall_back_to_the_reviewer_chain(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_MODELS", REVIEWER)
        assert llm_backends.create_llm_client({**CFG, "llm_models": [""]}).models == [REVIEWER]
        with pytest.raises(ConfigError, match="--judge-model"):
            build_judge_client(CFG, "")


class TestJudgeCase:
    def test_a_miss_makes_one_json_mode_call_and_writes_the_entry(self, tmp_path):
        client = StubJudge()
        cache = tmp_path / "cache"
        outcome = judge_case(client, JUDGE, CASE, RECORD, cache_dir=cache, max_tokens=2048, timeout_s=9.0)

        system, user = split_judge_prompt(build_judge_prompt(CASE, assign_refs(RECORD["findings"])))
        assert client.calls == [{"system": system, "user": user, "max_tokens": 2048,
                                 "json_mode": True, "timeout_s": 9.0}]
        assert outcome.ok and outcome.error is None
        assert outcome.grades == GOOD_GRADES
        assert (outcome.cached, outcome.llm_calls, outcome.case_id) == (False, 1, "case-1")
        assert outcome.cache_key == _key()
        entry = json.loads((cache / f"{_key()}.json").read_text(encoding="utf-8"))
        assert entry == {
            "version": JUDGE_CACHE_VERSION, "key": _key(), "model": JUDGE,
            "prompt_version": JUDGE_PROMPT_VERSION, "prompt_sha256": judge_prompt_sha(),
            "case_id": "case-1", "response_model": JUDGE, "response": GOOD_REPLY,
        }
        assert sorted(p.name for p in cache.iterdir()) == [f"{_key()}.json"]

    def test_a_hit_makes_no_call(self, tmp_path):
        cache = tmp_path / "cache"
        first = judge_case(StubJudge(), JUDGE, CASE, RECORD, cache_dir=cache)
        again = StubJudge(text="this reply must never be read")
        outcome = judge_case(again, JUDGE, CASE, RECORD, cache_dir=cache)
        assert again.calls == []
        assert outcome.ok and outcome.cached
        assert outcome.llm_calls == 0
        assert outcome.grades == first.grades == GOOD_GRADES
        assert (outcome.cost_usd, outcome.unit, outcome.model) == (0.0, None, JUDGE)

    def test_another_judge_model_misses(self, tmp_path):
        cache = tmp_path / "cache"
        judge_case(StubJudge(), JUDGE, CASE, RECORD, cache_dir=cache)
        other = StubJudge("judge-other")
        outcome = judge_case(other, "judge-other", CASE, RECORD, cache_dir=cache)
        assert len(other.calls) == 1 and not outcome.cached

    def test_a_changed_judge_template_misses(self, tmp_path, monkeypatch):
        cache = tmp_path / "cache"
        judge_case(StubJudge(), JUDGE, CASE, RECORD, cache_dir=cache)
        packaged = reviewer.load_prompt("judge")
        monkeypatch.setattr(reviewer, "load_prompt", lambda name: packaged + "\nOne more rule.\n")
        client = StubJudge()
        outcome = judge_case(client, JUDGE, CASE, RECORD, cache_dir=cache)
        assert len(client.calls) == 1 and not outcome.cached

    def test_no_cache_dir_always_calls(self, tmp_path):
        client = StubJudge()
        judge_case(client, JUDGE, CASE, RECORD)
        judge_case(client, JUDGE, CASE, RECORD)
        assert len(client.calls) == 2

    @pytest.mark.parametrize("corrupt", [
        pytest.param(b"{not json", id="not-json"),
        pytest.param(b"[]", id="not-an-object"),
        pytest.param(b"\xff\xfe\x00garbage", id="not-utf8"),
        pytest.param({"version": 99}, id="wrong-version"),
        pytest.param({"key": "0" * 64}, id="wrong-key"),
        pytest.param({"model": "someone-else"}, id="wrong-model"),
        pytest.param({"prompt_sha256": "0" * 64}, id="wrong-prompt-sha"),
        pytest.param({"response": None}, id="no-response"),
        pytest.param({"response": '{"grades": 3}'}, id="response-no-longer-parses"),
    ])
    def test_a_corrupt_entry_is_a_miss_that_warns_and_is_rewritten(self, tmp_path, caplog, corrupt):
        cache = tmp_path / "cache"
        judge_case(StubJudge(), JUDGE, CASE, RECORD, cache_dir=cache)
        path = cache / f"{_key()}.json"
        if isinstance(corrupt, bytes):
            path.write_bytes(corrupt)
        else:
            entry = json.loads(path.read_text(encoding="utf-8"))
            path.write_text(json.dumps({**entry, **corrupt}), encoding="utf-8")
        client = StubJudge()
        with caplog.at_level(logging.WARNING, logger="prxref"):
            outcome = judge_case(client, JUDGE, CASE, RECORD, cache_dir=cache)
        assert len(client.calls) == 1
        assert outcome.ok and not outcome.cached and outcome.grades == GOOD_GRADES
        assert any("judge cache entry" in m and str(path) in m and "corrupt" in m for m in _warnings(caplog))
        assert json.loads(path.read_text(encoding="utf-8"))["response"] == GOOD_REPLY

    @pytest.mark.parametrize("reply", [
        pytest.param("", id="empty"),
        pytest.param("I think H1 is matched.", id="prose"),
        pytest.param("[]", id="array"),
        pytest.param('{"grades": "none"}', id="grades-not-a-list"),
        pytest.param('{"grades": [{"human_id": "H1", "grade": "match", "ai_ref": "A1"}]}', id="unknown-grade"),
        pytest.param('{"grades": [{"grade": "full", "ai_ref": "A1"}]}', id="no-human-id"),
    ])
    def test_malformed_judge_output_is_a_judge_error_not_a_zero(self, tmp_path, caplog, reply):
        cache = tmp_path / "cache"
        client = StubJudge(text=reply)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            outcome = judge_case(client, JUDGE, CASE, RECORD, cache_dir=cache)
        assert len(client.calls) == 1
        assert not outcome.ok
        assert outcome.grades is None
        assert outcome.error.startswith("judge response rejected: ")
        assert any("judge error for case 'case-1'" in m for m in _warnings(caplog))
        assert not cache.exists() or list(cache.iterdir()) == []

    def test_a_rejected_response_is_retried_on_the_next_score(self, tmp_path):
        cache = tmp_path / "cache"
        judge_case(StubJudge(text="not json"), JUDGE, CASE, RECORD, cache_dir=cache)
        client = StubJudge()
        outcome = judge_case(client, JUDGE, CASE, RECORD, cache_dir=cache)
        assert len(client.calls) == 1 and outcome.ok and outcome.grades == GOOD_GRADES

    @pytest.mark.parametrize("error", [TimeoutError("deadline passed"), RuntimeError("all models failed"),
                                       ConfigError("PRXREF_LLM_MODELS: bad")])
    def test_an_llm_exception_is_a_judge_error(self, tmp_path, caplog, error):
        cache = tmp_path / "cache"
        client = StubJudge(error=error)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            outcome = judge_case(client, JUDGE, CASE, RECORD, cache_dir=cache)
        assert len(client.calls) == 1
        assert not outcome.ok and outcome.grades is None
        assert outcome.error == f"judge call failed: {type(error).__name__}: {error}"
        assert outcome.cost_usd is None
        assert any("judge error for case 'case-1'" in m for m in _warnings(caplog))
        assert not cache.exists() or list(cache.iterdir()) == []

    def test_a_case_without_labels_makes_no_call(self, tmp_path):
        client = StubJudge()
        outcome = judge_case(client, JUDGE, EvalCase(id="empty", expected=()), RECORD, cache_dir=tmp_path)
        assert client.calls == []
        assert outcome.ok and outcome.grades == () and outcome.llm_calls == 0
        assert list(tmp_path.iterdir()) == []

    def test_ref_index_maps_each_judge_ref_to_its_record_row(self):
        outcome = judge_case(StubJudge(), JUDGE, CASE, RECORD)
        assert outcome.ref_index == {"A1": 0, "A2": 2}
        assert all(RECORD["findings"][i]["drop_reason"] is None for i in outcome.ref_index.values())

    def test_a_trace_dir_gets_the_judge_unit(self, tmp_path):
        trace = tmp_path / "trace"
        client = StubJudge(cost_usd=0.002, cost_source="usage.cost")
        judge_case(client, JUDGE, CASE, RECORD, trace_dir=str(trace))
        assert (trace / "judge.system.md").read_text(encoding="utf-8") == client.calls[0]["system"]
        assert (trace / "judge.user.md").read_text(encoding="utf-8") == client.calls[0]["user"]
        assert json.loads((trace / "judge.response.json").read_text(encoding="utf-8")) == GOOD_REPLY
        meta = json.loads((trace / "judge.meta.json").read_text(encoding="utf-8"))
        assert (meta["unit"], meta["model"], meta["cost_usd"], meta["error"]) == ("judge", JUDGE, 0.002, "")

    def test_the_cache_write_is_a_temp_file_then_a_rename(self, tmp_path, monkeypatch):
        cache = tmp_path / "cache"
        renames = []
        real_replace = os.replace

        def spy(src, dst):
            renames.append((str(src), str(dst), os.path.exists(dst)))
            real_replace(src, dst)

        monkeypatch.setattr(eval_judge.os, "replace", spy)
        judge_case(StubJudge(), JUDGE, CASE, RECORD, cache_dir=cache)
        final = str(cache / f"{_key()}.json")
        assert len(renames) == 1
        src, dst, existed = renames[0]
        assert dst == final and not existed
        assert os.path.dirname(src) == str(cache) and src.endswith(".tmp") and not os.path.exists(src)

    def test_a_failed_cache_write_warns_and_keeps_the_grades(self, tmp_path, monkeypatch, caplog):
        cache = tmp_path / "cache"

        def refuse(src, dst):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(eval_judge.os, "replace", refuse)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            outcome = judge_case(StubJudge(), JUDGE, CASE, RECORD, cache_dir=cache)
        assert outcome.ok and outcome.grades == GOOD_GRADES
        assert any("judge cache write" in m for m in _warnings(caplog))
        assert list(cache.iterdir()) == []

    def test_judge_cache_dir_lives_under_the_label(self, tmp_path):
        assert judge_cache_dir(tmp_path, "baseline") == tmp_path / "baseline" / JUDGE_CACHE_DIRNAME


class TestCost:
    def test_a_none_cost_stays_none(self):
        outcome = judge_case(StubJudge(cost_usd=None), JUDGE, CASE, RECORD, price_table={})
        assert outcome.ok
        assert outcome.cost_usd is None
        assert (outcome.cost_estimated, outcome.cost_source) == (False, "")
        assert judge_cost([outcome], {}) == (None, False)

    def test_a_reported_cost_wins_over_the_table(self):
        table = parse_price_table({JUDGE: {"input": 100.0, "output": 100.0}})
        outcome = judge_case(StubJudge(cost_usd=0.0021, cost_source="usage.cost"), JUDGE, CASE, RECORD,
                             price_table=table)
        assert (outcome.cost_usd, outcome.cost_estimated, outcome.cost_source) == (0.0021, False, "usage.cost")

    def test_the_price_table_estimates_an_unreported_cost(self):
        table = parse_price_table({JUDGE: {"input": 1.0, "output": 2.0}})
        outcome = judge_case(StubJudge(input_tokens=1000, output_tokens=500), JUDGE, CASE, RECORD,
                             price_table=table)
        assert (outcome.cost_usd, outcome.cost_estimated, outcome.cost_source) == (0.002, True, "")

    def test_a_rejected_response_was_still_billed(self):
        outcome = judge_case(StubJudge(text="nope", cost_usd=0.003, cost_source="usage.cost"), JUDGE, CASE, RECORD)
        assert not outcome.ok and outcome.cost_usd == 0.003

    def test_the_total_follows_run_cost(self, tmp_path):
        cache = tmp_path / "cache"
        paid = judge_case(StubJudge(cost_usd=0.002, cost_source="usage.cost"), JUDGE, CASE, RECORD, cache_dir=cache)
        hit = judge_case(StubJudge(), JUDGE, CASE, RECORD, cache_dir=cache)
        raised = judge_case(StubJudge(error=TimeoutError("late")), JUDGE, CASE, RECORD)
        unknown = judge_case(StubJudge(cost_usd=None), JUDGE, CASE, RECORD)
        assert judge_cost([paid, hit, raised], {}) == (0.002, False)
        assert judge_cost([paid, unknown], {}) == (None, False)
        assert judge_cost([hit], {}) == (0.0, False)
        assert judge_cost([], None) == (0.0, False)
        assert judge_cost([raised], {}) == (None, False)


class TestSelfJudging:
    def test_a_judge_among_the_reviewer_models_warns_and_flags(self, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref"):
            flagged = check_self_judging(JUDGE, {"temperature": 0.0, "seed": 1, "models": ["cheap-m", JUDGE]})
        assert flagged is True
        messages = _warnings(caplog)
        assert len(messages) == 1 and repr(JUDGE) in messages[0] and "self_judged" in messages[0]

    def test_names_are_compared_stripped_and_casefolded(self):
        assert check_self_judging(" Judge-M ", {"models": [JUDGE]}) is True

    @pytest.mark.parametrize("sampling", [
        {"models": [REVIEWER]}, {"models": []}, {"models": None}, {}, None,
    ])
    def test_otherwise_nothing_is_flagged(self, caplog, sampling):
        with caplog.at_level(logging.WARNING, logger="prxref"):
            assert check_self_judging(JUDGE, sampling) is False
        assert _warnings(caplog) == []

    def test_the_stamp_carries_the_flag(self):
        flagged = check_self_judging(REVIEWER, RECORD["sampling"])
        assert judge_stamp(StubJudge(REVIEWER), REVIEWER, self_judged=flagged)["self_judged"] is True


class TestStamp:
    def test_the_stamp_contents(self):
        stamp = judge_stamp(StubJudge(), JUDGE, self_judged=False)
        assert stamp == {
            "model": JUDGE,
            "sampling": {"temperature": 0.0, "seed": 7, "models": [JUDGE]},
            "prompt_version": JUDGE_PROMPT_VERSION,
            "prompt_sha256": hashlib.sha256(reviewer.load_prompt("judge").encode("utf-8")).hexdigest(),
            "self_judged": False,
        }

    def test_the_sampling_has_the_run_record_shape(self):
        client = StubJudge()
        assert judge_stamp(client, JUDGE, self_judged=False)["sampling"] == orchestrator._sampling(client)

    def test_a_real_judge_client_stamps_its_own_chain(self):
        stamp = judge_stamp(build_judge_client(CFG, JUDGE), JUDGE, self_judged=False)
        assert stamp["sampling"] == {"temperature": 0.2, "seed": 11, "models": [JUDGE]}


def test_the_outcome_type_is_frozen():
    outcome = JudgeOutcome(case_id="c", cache_key="k", grades=(), error=None, cached=False, llm_calls=0,
                           ref_index={})
    with pytest.raises(AttributeError):
        outcome.error = "x"
