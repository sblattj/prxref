"""Issue #143: the posted attribution line shows the sampling seed.

``sampling.seed`` is already in the run record; the footer now says it too, as
`` · seed=N`` after the elapsed field and before the cost label. A client with
no integer seed (none attribute, ``None``, ``PRXREF_LLM_SEED=off``, claude-cli)
renders the line exactly as before, and ``ATTRIBUTION_MARKER`` and ``model=``
consumers see the same prefix.
"""
from __future__ import annotations

import re

import pytest

from prxref import orchestrator
from prxref.forges.base import ATTRIBUTION_MARKER
from prxref.orchestrator import orchestrate_review
from tests.test_orchestrator import (
    HAPPY_FINDINGS,
    REF,
    FakeForge,
    FakeLLM,
    _added_file_diff,
)

pytestmark = pytest.mark.usefixtures("contract_stubs")

ONE_FILE_DIFF = _added_file_diff("src/app.py", 20)
OLD_LINE = re.compile(
    rf"{re.escape(ATTRIBUTION_MARKER)} · model=test-model-1 · \d+ tok · \d+\.\ds"
)


class SeededLLM(FakeLLM):
    """``FakeLLM`` carrying a ``seed`` attribute, as a real client does."""

    def __init__(self, seed, findings_by_path=None, **kw):
        super().__init__(findings_by_path, **kw)
        self.seed = seed


def _last_line(body: str) -> str:
    return body.rstrip("\n").splitlines()[-1]


def _post(llm, **kw):
    forge = FakeForge(diff=ONE_FILE_DIFF)
    orchestrate_review(forge, REF, llm, post=True, **kw)
    assert forge.summaries
    return forge


class TestAttributionHelper:
    def test_no_seed_is_byte_identical(self):
        assert (
            orchestrator._attribution("m", 150, 1200)
            == "Reviewed by prxref · model=m · 150 tok · 1.2s"
        )

    def test_seed_sits_between_elapsed_and_cost(self):
        assert (
            orchestrator._attribution("m", 150, 1200, seed=7, cost_label="$0.0007")
            == "Reviewed by prxref · model=m · 150 tok · 1.2s · seed=7 · $0.0007"
        )

    def test_zero_is_a_seed(self):
        assert orchestrator._attribution("m", 1, 1000, seed=0).endswith("1.0s · seed=0")

    @pytest.mark.parametrize("seed", [None, True, "7", 1.5])
    def test_a_non_integer_seed_is_not_shown(self, seed):
        assert orchestrator._attribution("m", 1, 1000, seed=seed) == (
            "Reviewed by prxref · model=m · 1 tok · 1.0s"
        )


class TestPostedSummary:
    def test_seed_is_in_the_posted_line(self):
        forge = _post(SeededLLM(7, HAPPY_FINDINGS))
        for body in forge.summaries:
            line = _last_line(body)
            assert re.fullmatch(
                rf"{re.escape(ATTRIBUTION_MARKER)} · model=test-model-1 · \d+ tok · \d+\.\ds · seed=7",
                line,
            ), line

    def test_seed_precedes_the_cost_label(self):
        forge = _post(SeededLLM(7, HAPPY_FINDINGS), post_cost=True)
        line = _last_line(forge.summaries[0])
        assert " · seed=7 · " in line
        assert line.index("seed=7") < line.index("cost unknown")
        assert line.endswith(" · cost unknown")

    def test_zero_seed_shows(self):
        forge = _post(SeededLLM(0, HAPPY_FINDINGS))
        assert _last_line(forge.summaries[0]).endswith(" · seed=0")

    def test_marker_prefix_and_model_field_still_parse(self):
        line = _last_line(_post(SeededLLM(7, HAPPY_FINDINGS)).summaries[0])
        assert line.startswith(ATTRIBUTION_MARKER)
        assert re.search(r"model=(\S+) · (\d+) tok", line).groups() == ("test-model-1", "150")

    def test_seed_none_is_byte_identical_to_the_old_format(self):
        line = _last_line(_post(SeededLLM(None, HAPPY_FINDINGS)).summaries[0])
        assert OLD_LINE.fullmatch(line), line

    def test_no_seed_attribute_is_byte_identical_to_the_old_format(self):
        line = _last_line(_post(FakeLLM(HAPPY_FINDINGS)).summaries[0])
        assert OLD_LINE.fullmatch(line), line

    def test_summary_only_exit_shows_the_seed(self):
        forge = FakeForge(diff="")
        res = orchestrate_review(forge, REF, SeededLLM(7), post=True)
        assert res["chunks_reviewed"] == 0
        assert _last_line(forge.summaries[0]) == (
            f"{ATTRIBUTION_MARKER} · model=unknown · 0 tok · 0.0s · seed=7"
        )


class TestErrorNotice:
    def test_total_failure_notice_shows_the_seed(self):
        llm = SeededLLM(7, error=RuntimeError("boom"))
        forge = _post(llm)
        line = _last_line(forge.summaries[0])
        assert "The review could not complete" in forge.summaries[0]
        assert line.endswith(" · seed=7"), line

    def test_total_failure_notice_without_a_seed_is_unchanged(self):
        forge = _post(SeededLLM(None, error=RuntimeError("boom")))
        line = _last_line(forge.summaries[0])
        assert re.fullmatch(
            rf"{re.escape(ATTRIBUTION_MARKER)} · model=\S+ · \d+ tok · \d+\.\ds", line,
        ), line
