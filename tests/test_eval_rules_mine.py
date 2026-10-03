"""Leave-fold-out rule mining: folds, the leakage guard, the single-shot call, retry, cache and cap."""
from __future__ import annotations

import json
import random

import pytest

from prxref.config import _DEFAULTS
from prxref.eval_cases import EvalCase, ExpectedFinding
from prxref.eval_rules_mine import (
    RULES_MAX_CHARS,
    MinedRules,
    assign_folds,
    build_mine_prompt,
    mine_rules,
    training_cases,
)
from prxref.llm import InvokeResult

MODEL = "judge/model"
RULES = "# Team rules\n\n- Flag unchecked errors.\n- Require tests for new branches.\n"


def _case(cid: str, pr: str | None = "auto", *texts: str) -> EvalCase:
    pr_url = f"https://example.test/o/r/pull/{cid}" if pr == "auto" else pr
    labels = tuple(
        ExpectedFinding(id=f"{cid}-{i}", file=f"src/{cid}.py", line=i + 1, severity="major",
                        category="spec", accepted=bool(i % 2), text=text)
        for i, text in enumerate(texts or (f"label text of {cid}",))
    )
    return EvalCase(id=cid, expected=labels, pr_url=pr_url)


class Stub:
    def __init__(self, *replies: str | Exception, cost: float | None = 0.01) -> None:
        self.replies = list(replies)
        self.cost = cost
        self.calls: list[dict] = []

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=None):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens,
                           "json_mode": json_mode, "timeout_s": timeout_s})
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return InvokeResult(text=reply, model="stub-model", input_tokens=100, output_tokens=50, cost_usd=self.cost)


class TestAssignFolds:
    def test_deterministic_and_order_independent(self):
        cases = [_case(f"c{i}") for i in range(12)]
        first = assign_folds(cases, 3)
        shuffled = cases[:]
        random.Random(4).shuffle(shuffled)
        assert assign_folds(shuffled, 3) == first
        assert assign_folds(cases, 3) == first
        assert set(first.values()) == {0, 1, 2}

    def test_cases_sharing_a_pr_share_a_fold(self):
        cases = [_case("a1", "https://x.test/p/1"), _case("a2", "https://x.test/p/1")]
        cases += [_case(f"b{i}") for i in range(5)]
        folds = assign_folds(cases, 3)
        assert folds["a1"] == folds["a2"]

    def test_cases_without_pr_url_group_by_id(self):
        cases = [_case(f"n{i}", None) for i in range(4)]
        assert sorted(assign_folds(cases, 4).values()) == [0, 1, 2, 3]

    def test_balanced_within_largest_group(self):
        cases = [_case(f"s{i}", f"https://x.test/p/{i // 3}") for i in range(9)]
        cases += [_case(f"u{i}") for i in range(7)]
        folds = assign_folds(cases, 4)
        sizes = [sum(1 for f in folds.values() if f == n) for n in range(4)]
        assert max(sizes) - min(sizes) <= 3

    @pytest.mark.parametrize("k", [0, 1, -2, 6, True, 2.0])
    def test_k_out_of_bounds(self, k):
        cases = [_case(f"c{i}") for i in range(5)]
        with pytest.raises(ValueError):
            assign_folds(cases, k)

    def test_k_above_groups_not_cases(self):
        cases = [_case("a", "https://x.test/p/1"), _case("b", "https://x.test/p/1"), _case("c")]
        with pytest.raises(ValueError):
            assign_folds(cases, 3)
        assert set(assign_folds(cases, 2).values()) == {0, 1}

    def test_duplicate_id_rejected(self):
        with pytest.raises(ValueError):
            assign_folds([_case("a"), _case("a", "https://x.test/other")], 2)


class TestLeakageGuard:
    def test_prompt_for_fold_j_has_no_fold_j_label_text(self):
        cases = [_case(f"c{i}", "auto", f"UNIQUE-SECRET-{i}-alpha", f"UNIQUE-SECRET-{i}-beta") for i in range(8)]
        folds = assign_folds(cases, 4)
        for j in range(4):
            train = training_cases(cases, folds, j)
            assert train and all(folds[c.id] != j for c in train)
            prompt = build_mine_prompt(train)
            for case in cases:
                for label in case.expected:
                    if folds[case.id] == j:
                        assert label.text not in prompt
                    else:
                        assert label.text in prompt

    def test_training_cases_missing_fold_entry(self):
        with pytest.raises(ValueError):
            training_cases([_case("a")], {}, 0)

    def test_mine_rules_sends_only_what_it_is_given(self):
        cases = [_case("c0", "auto", "KEEP-ME"), _case("c1", "auto", "HELD-OUT")]
        stub = Stub(RULES)
        mine_rules(training_cases(cases, {"c0": 0, "c1": 1}, 1), stub, MODEL, cache_dir=None)
        sent = stub.calls[0]["system"] + stub.calls[0]["user"]
        assert "KEEP-ME" in sent and "HELD-OUT" not in sent


class TestMineRules:
    def test_single_shot_call_shape(self, tmp_path):
        stub = Stub(RULES)
        out = mine_rules([_case("a", "auto", "check errors")], stub, f" {MODEL} ", cache_dir=tmp_path,
                         max_tokens=777, timeout_s=9.0)
        assert out == MinedRules(text=RULES.strip(), model="stub-model", input_tokens=100, output_tokens=50,
                                 cost_usd=0.01, cached=False)
        (call,) = stub.calls
        assert call["json_mode"] is False and call["max_tokens"] == 777 and call["timeout_s"] == 9.0
        assert "check errors" in call["user"]
        assert str(RULES_MAX_CHARS) in call["system"]

    def test_cap_matches_rules_file_default(self):
        assert RULES_MAX_CHARS == _DEFAULTS["review_rules_max_chars"]

    def test_cache_hit_makes_no_call(self, tmp_path):
        cases = [_case("a")]
        first = mine_rules(cases, Stub(RULES), MODEL, cache_dir=tmp_path)
        stub = Stub(RuntimeError("must not be called"))
        hit = mine_rules(cases, stub, MODEL, cache_dir=tmp_path)
        assert stub.calls == []
        assert hit.cached and hit.text == first.text and hit.cost_usd == 0.0
        assert hit.input_tokens == 0 and hit.output_tokens == 0
        assert len(list(tmp_path.glob("*.json"))) == 1
        assert not list(tmp_path.glob(".*.tmp"))

    def test_cache_key_covers_model_and_labels(self, tmp_path):
        mine_rules([_case("a")], Stub(RULES), MODEL, cache_dir=tmp_path)
        mine_rules([_case("a")], Stub(RULES), "other/model", cache_dir=tmp_path)
        mine_rules([_case("a", "auto", "different")], Stub(RULES), MODEL, cache_dir=tmp_path)
        assert len(list(tmp_path.glob("*.json"))) == 3

    def test_corrupt_cache_entry_is_a_miss(self, tmp_path):
        cases = [_case("a")]
        mine_rules(cases, Stub(RULES), MODEL, cache_dir=tmp_path)
        (path,) = tmp_path.glob("*.json")
        path.write_text("{not json", encoding="utf-8")
        stub = Stub(RULES)
        out = mine_rules(cases, stub, MODEL, cache_dir=tmp_path)
        assert len(stub.calls) == 1 and not out.cached
        assert json.loads(path.read_text(encoding="utf-8"))["text"] == RULES.strip()

    def test_code_fence_stripped(self):
        out = mine_rules([_case("a")], Stub(f"```markdown\n{RULES}```\n"), MODEL, cache_dir=None)
        assert out.text == RULES.strip()

    def test_retry_then_success(self, tmp_path):
        stub = Stub("   ", RULES, cost=0.02)
        out = mine_rules([_case("a")], stub, MODEL, cache_dir=tmp_path, parse_retries=1)
        assert len(stub.calls) == 2 and stub.calls[0] == stub.calls[1]
        assert out.text == RULES.strip()
        assert out.input_tokens == 200 and out.output_tokens == 100 and out.cost_usd == pytest.approx(0.04)

    def test_exhausted_retries_raise_and_cache_nothing(self, tmp_path):
        stub = Stub("")
        with pytest.raises(RuntimeError, match="empty"):
            mine_rules([_case("a")], stub, MODEL, cache_dir=tmp_path, parse_retries=2)
        assert len(stub.calls) == 3
        assert not list(tmp_path.glob("*.json"))

    def test_no_retries_by_default(self):
        stub = Stub("")
        with pytest.raises(RuntimeError):
            mine_rules([_case("a")], stub, MODEL, cache_dir=None)
        assert len(stub.calls) == 1

    def test_over_cap_is_a_parse_failure(self):
        stub = Stub("x" * (RULES_MAX_CHARS + 1))
        with pytest.raises(RuntimeError, match="cap"):
            mine_rules([_case("a")], stub, MODEL, cache_dir=None)

    def test_over_cap_retry_then_success(self):
        stub = Stub("x" * (RULES_MAX_CHARS + 1), "y" * RULES_MAX_CHARS)
        out = mine_rules([_case("a")], stub, MODEL, cache_dir=None, parse_retries=1)
        assert len(out.text) == RULES_MAX_CHARS

    def test_unreported_cost_is_none_not_zero(self):
        out = mine_rules([_case("a")], Stub(RULES, cost=None), MODEL, cache_dir=None)
        assert out.cost_usd is None

    def test_client_exception_propagates_without_retry(self):
        stub = Stub(ConnectionError("down"))
        with pytest.raises(ConnectionError):
            mine_rules([_case("a")], stub, MODEL, cache_dir=None, parse_retries=3)
        assert len(stub.calls) == 1
