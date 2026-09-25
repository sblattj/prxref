"""``prxref eval`` records the parse retry (#21).

``eval run`` allowlists ``llm_parse_retries`` into ``run.json``'s
``config``. ``eval score`` passes ``PRXREF_LLM_PARSE_RETRIES`` to
:func:`prxref.eval_judge.judge_case` as ``parse_retries`` and totals the
judge's retries into ``score.json``'s ``judge`` block, beside ``llm_calls``.
``score.md`` names the retries only when there were any, so a scoring run
without one renders exactly as it did before.

The run directories are laid out by the helpers of ``tests/test_eval_run.py``
and ``tests/test_eval_score.py``. The judge is a stub whose reply depends on
how often it has seen the same prompt, so "malformed, then valid" holds per
case; nothing reaches a network.
"""
from __future__ import annotations

import copy
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from prxref import config, eval_judge, evals
from prxref.eval_judge import JudgeOutcome
from prxref.judge import JUDGE_PROMPT_VERSION, judge_prompt_sha
from tests import test_eval_run as run_helpers
from tests import test_eval_score as score_helpers

ENV = "PRXREF_LLM_PARSE_RETRIES"
KEY = "llm_parse_retries"
JUDGE = score_helpers.JUDGE
MALFORMED = "not json at all"
DOC = (Path(__file__).resolve().parents[1] / "docs" / "evals.md").read_text(encoding="utf-8")
FLAT_DOC = " ".join(DOC.split())

CASE_B = score_helpers.CASE_B
RECORD_B = score_helpers.RECORD_B
REPLY_B = score_helpers.REPLY_B
CASE_D = score_helpers._case("d", score_helpers._label("L1", "src/d.py", 7, "error", text="Leaks a handle."))
RECORD_D = score_helpers._record([score_helpers._row("src/d.py", 7, "Handle leak", severity="error")])
REPLY_D = score_helpers._reply(("L1", "full", "A1"))


def _malformed_first(valid: str, bad: int = 1) -> Callable[[str, int], str]:
    """Answer the first ``bad`` calls of each prompt with an unparseable reply, then ``valid``."""
    return lambda user, seen: MALFORMED if seen <= bad else valid


@pytest.fixture
def judge(monkeypatch):
    """Patch the client factory with a stub judge; set ``state.answer(user, seen)`` before scoring.

    ``seen`` counts the calls made so far with that same prompt, this one
    included, so a reply can depend on whether the request is a retry.
    """
    state = SimpleNamespace(built=[], clients=[], answer=lambda user, seen: REPLY_B, seen=Counter())

    def answer(user: str) -> str:
        state.seen[user] += 1
        return state.answer(user, state.seen[user])

    def create(cfg=None, session=None):
        state.built.append(copy.deepcopy(cfg))
        client = score_helpers.StubJudge(cfg["llm_models"][0], answer)
        state.clients.append(client)
        return client

    monkeypatch.setattr("prxref.llm_backends.create_llm_client", create)
    state.calls = lambda: [call for client in state.clients for call in client.calls]
    return state


@pytest.fixture
def forwarded(monkeypatch):
    """Spy on ``eval_judge.judge_case``: record each call's keyword arguments, then call it."""
    seen: list[dict] = []
    real = eval_judge.judge_case

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(eval_judge, "judge_case", spy)
    return seen


def _one_case(out: Path, case=CASE_B, record=RECORD_B) -> Path:
    return score_helpers._write_run(out, [(case, record)])


def _cost_line(markdown: str) -> str:
    return next(line for line in markdown.splitlines() if line.startswith("- Judge: "))


class TestTheRunRecordsTheSetting:
    def test_the_allowlist_holds_the_key_after_the_token_cap(self):
        keys = list(evals.RUN_CONFIG_KEYS)
        assert KEY in keys
        assert keys.index(KEY) == keys.index("llm_max_tokens") + 1
        assert len(keys) == 17
        assert KEY in config._DEFAULTS and KEY in config._INT_KEYS

    @pytest.mark.parametrize("value,expected", [(None, 1), ("0", 0), ("3", 3)])
    def test_run_json_config_records_the_loaded_value(self, tmp_path, monkeypatch, value, expected):
        if value is not None:
            monkeypatch.setenv(ENV, value)
        cases = run_helpers._dataset(tmp_path)

        run_helpers._run(run_helpers._args(cases, tmp_path / "out"), run_helpers.FakeReview())

        run = run_helpers._read(tmp_path / "out" / "L" / "run.json")
        assert run["config"][KEY] == expected
        assert list(run["config"]) == list(evals.RUN_CONFIG_KEYS)

    def test_the_documented_allowlist_names_the_key_in_place(self):
        row = next(line for line in DOC.splitlines() if line.startswith("| `config` |"))
        assert "`llm_max_tokens`, `llm_parse_retries`, `max_chunks`" in row
        assert "`config` is an allowlist of the seventeen settings above" in FLAT_DOC


class TestTheJudgeGetsTheBudget:
    @pytest.mark.parametrize("value,expected", [(None, 1), ("0", 0), ("1", 1), ("3", 3)])
    def test_judge_case_receives_the_config_value(self, tmp_path, judge, forwarded, monkeypatch, value, expected):
        if value is not None:
            monkeypatch.setenv(ENV, value)
        _one_case(tmp_path / "out")

        score_helpers._score(tmp_path / "out", judge_model=JUDGE)

        assert [kwargs["parse_retries"] for kwargs in forwarded] == [expected]

    def test_a_malformed_then_valid_reply_is_retried_and_counted(self, tmp_path, judge, forwarded, monkeypatch):
        monkeypatch.setenv(ENV, "3")
        _one_case(tmp_path / "out")
        judge.answer = _malformed_first(REPLY_B)

        score, markdown = score_helpers._score(tmp_path / "out", judge_model=JUDGE)

        assert forwarded[0]["parse_retries"] == 3
        assert len(judge.calls()) == 2
        assert (score["judge"]["parse_retries"], score["judge"]["llm_calls"]) == (1, 2)
        assert score["judge"]["errors"] == []
        assert score["judge"]["cost_usd"] == pytest.approx(0.004)
        assert score_helpers._finding(score, "b", "J1")["grade"] == "partial"
        assert _cost_line(markdown) == "- Judge: $0.0040, 2 call(s) (1 parse retry), 0 case(s) from the cache"

    def test_the_documented_cost_line_is_the_rendered_one(self, tmp_path, judge, monkeypatch):
        _one_case(tmp_path / "out")
        judge.answer = _malformed_first(REPLY_B)

        _, markdown = score_helpers._score(tmp_path / "out", judge_model=JUDGE)

        assert f"as in `{_cost_line(markdown)}`" in FLAT_DOC

    def test_the_block_keeps_parse_retries_beside_llm_calls(self, tmp_path, judge):
        _one_case(tmp_path / "out")
        judge.answer = _malformed_first(REPLY_B)

        score, _ = score_helpers._score(tmp_path / "out", judge_model=JUDGE)

        keys = list(score["judge"])
        assert keys.index("parse_retries") == keys.index("llm_calls") + 1

    def test_the_retries_sum_over_the_cases(self, tmp_path, judge, monkeypatch):
        monkeypatch.setenv(ENV, "2")
        score_helpers._write_run(tmp_path / "out", [(CASE_B, RECORD_B), (CASE_D, RECORD_D)])
        replies = {"b": _malformed_first(REPLY_B, bad=2), "d": _malformed_first(REPLY_D, bad=1)}
        judge.answer = lambda user, seen: replies["b" if "src/b.py" in user else "d"](user, seen)

        score, markdown = score_helpers._score(tmp_path / "out", judge_model=JUDGE)

        assert (score["judge"]["parse_retries"], score["judge"]["llm_calls"]) == (3, 5)
        assert score["judge"]["errors"] == []
        assert "5 call(s) (3 parse retries), 0 case(s)" in _cost_line(markdown)

    def test_a_reply_rejected_past_the_budget_is_a_judge_error_that_still_counts(self, tmp_path, judge):
        _one_case(tmp_path / "out")
        judge.answer = lambda user, seen: MALFORMED

        score, markdown = score_helpers._score(tmp_path / "out", judge_model=JUDGE)

        assert (score["judge"]["parse_retries"], score["judge"]["llm_calls"]) == (1, 2)
        assert [error["case_id"] for error in score["judge"]["errors"]] == ["b"]
        assert score["judge"]["errors"][0]["error"].startswith("judge response rejected:")
        assert score_helpers._finding(score, "b", "J1")["grade"] == "judge_error"
        assert "2 call(s) (1 parse retry)" in _cost_line(markdown)

    def test_a_cached_rescore_counts_no_retry(self, tmp_path, judge):
        _one_case(tmp_path / "out")
        judge.answer = _malformed_first(REPLY_B)
        score_helpers._score(tmp_path / "out", judge_model=JUDGE)

        second, markdown = score_helpers._score(tmp_path / "out", judge_model=JUDGE)

        assert (second["judge"]["parse_retries"], second["judge"]["llm_calls"], second["judge"]["cached"]) == (0, 0, 1)
        assert _cost_line(markdown) == "- Judge: $0.0000, 0 call(s), 1 case(s) from the cache"


class TestAtZeroNothingChanges:
    def test_a_malformed_reply_is_not_retried_and_the_line_names_no_retry(self, tmp_path, judge, forwarded,
                                                                         monkeypatch):
        monkeypatch.setenv(ENV, "0")
        _one_case(tmp_path / "out")
        judge.answer = _malformed_first(REPLY_B)

        score, markdown = score_helpers._score(tmp_path / "out", judge_model=JUDGE)

        assert forwarded[0]["parse_retries"] == 0
        assert len(judge.calls()) == 1
        assert (score["judge"]["parse_retries"], score["judge"]["llm_calls"]) == (0, 1)
        assert score_helpers._finding(score, "b", "J1")["grade"] == "judge_error"
        assert _cost_line(markdown) == "- Judge: $0.0020, 1 call(s), 0 case(s) from the cache"
        assert "retr" not in markdown

    def test_the_golden_run_renders_the_golden_score_md_byte_for_byte(self, tmp_path, judge, monkeypatch):
        monkeypatch.setenv(ENV, "0")
        out = score_helpers._golden_run(tmp_path)
        judge.answer = lambda user, seen: REPLY_B

        score, markdown = score_helpers._score(out, judge_model=JUDGE)

        assert score["judge"]["parse_retries"] == 0
        assert markdown == score_helpers.GOLDEN.format(version=JUDGE_PROMPT_VERSION, sha=judge_prompt_sha())

    def test_the_default_budget_with_a_clean_reply_renders_the_golden_too(self, tmp_path, judge):
        out = score_helpers._golden_run(tmp_path)
        judge.answer = lambda user, seen: REPLY_B

        score, markdown = score_helpers._score(out, judge_model=JUDGE)

        assert (score["judge"]["parse_retries"], score["judge"]["llm_calls"]) == (0, 1)
        assert markdown == score_helpers.GOLDEN.format(version=JUDGE_PROMPT_VERSION, sha=judge_prompt_sha())


class TestTheBlockTotal:
    def _outcome(self, case_id: str, *, llm_calls: int, parse_retries: int, cached: bool = False) -> JudgeOutcome:
        return JudgeOutcome(case_id=case_id, cache_key="k", grades=(), error=None, cached=cached,
                            llm_calls=llm_calls, ref_index={}, parse_retries=parse_retries)

    def test_it_sums_every_outcome_s_retries(self):
        client = SimpleNamespace(models=[JUDGE], temperature=0.0, seed=1)
        outcomes = [
            self._outcome("a", llm_calls=3, parse_retries=2),
            self._outcome("b", llm_calls=0, parse_retries=0, cached=True),
            self._outcome("c", llm_calls=1, parse_retries=0),
        ]

        block = evals._judge_block(client, JUDGE, False, outcomes, None)

        assert (block["llm_calls"], block["parse_retries"], block["cached"]) == (4, 2, 1)

    def test_no_outcome_gives_zero(self):
        client = SimpleNamespace(models=[JUDGE], temperature=0.0, seed=1)
        assert evals._judge_block(client, JUDGE, False, [], None)["parse_retries"] == 0
