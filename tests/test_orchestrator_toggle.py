"""Orchestrator wiring for ``heuristics.toggle_pinned_off_findings`` (issue #22).

The heuristic itself (its supported toggle/pin shapes, its body text, its
sorting) is covered by ``tests/test_toggle_pinned_off.py``. This file covers
only the wiring: that ``orchestrate_review`` actually calls it, folds the
result in at the chunk/sweep boundary next to ``release_shape``, and that the
finding survives the quality passes exactly once even when a chunk worker
reports something similar on the same line.

The fixture diff reuses the #22 fixture lines from
``tests/test_toggle_pinned_off.py`` (``PROGRESS_PY``, ``CONFTEST_PY``):
``assistant/progress.py`` adds the toggle at line
:data:`tests.test_toggle_pinned_off.TOGGLE_LINE`, ``tests/conftest.py`` pins
it off for the whole suite.
"""
from __future__ import annotations

import pytest

from prxref import heuristics
from prxref.orchestrator import orchestrate_review
from prxref.triage import Finding
from tests.test_orchestrator import REF, FakeForge, FakeLLM
from tests.test_toggle_pinned_off import CONFTEST_PY, PROGRESS_PY, TOGGLE_LINE, _added_file

pytestmark = pytest.mark.usefixtures("contract_stubs")

PROGRESS_PATH = "assistant/progress.py"
CONFTEST_PATH = "tests/conftest.py"

# A reworded restatement of the deterministic finding's own title: it shares
# five tokens with "Toggle \"progress_notes\" defaults on but the test setup
# pins it off" (progress, notes, toggle, test, setup) for a Jaccard of 0.45,
# well past TITLE_MIN_SHARED_TOKENS (3) and the 0.4 threshold the same-line
# test below turns on.
MODEL_TITLE = "Progress notes toggle stays on though test setup disables it"
MODEL_BODY = (
    'The `enabled("progress_notes", default=True)` call ships the toggle on '
    "by default; the review flags the same toggle as a duplicate finding."
)


def _diff(*, pin_value: str = "false") -> str:
    """The #22 fixture diff: adds the toggle, and pins it to ``pin_value``."""
    conftest = (
        CONFTEST_PY if pin_value == "false"
        else [ln.replace('"false"', f'"{pin_value}"') for ln in CONFTEST_PY]
    )
    return _added_file(PROGRESS_PATH, PROGRESS_PY) + _added_file(CONFTEST_PATH, conftest)


def _all_findings(res: dict) -> list[Finding]:
    """Every finding the run recorded, active or dropped, quality-gate order."""
    return list(res["findings_active"]) + list(res["findings_dropped"])


def _toggle_findings(res: dict) -> list[Finding]:
    """The subset of recorded findings the deterministic toggle check made."""
    return [f for f in _all_findings(res) if f.body.endswith(heuristics._BODY_SUFFIX)]


class TestSurvivesOnce:
    """The toggle finding reaches the review exactly once, undropped."""

    def test_the_toggle_finding_reaches_the_review_exactly_once(self):
        forge = FakeForge(diff=_diff())
        res = orchestrate_review(forge, REF, FakeLLM(), post=False)

        found = _toggle_findings(res)
        assert len(found) == 1
        f = found[0]
        assert f.file == PROGRESS_PATH
        assert f.line == TOGGLE_LINE
        assert f.drop_reason is None
        assert f.body.endswith(heuristics._BODY_SUFFIX)


class TestModelFindingOnTheSameLine:
    """A chunk worker also reports on the toggle line, with a similar title.

    ``dedup_similarity`` turns on ``apply_sweep_dedup``'s reworded tier, the
    only pass that compares two CHUNK-side findings against each other on the
    same line (the exact tier drops only sweep-side duplicates). Both the
    deterministic finding and the chunk worker's finding are chunk-side here
    (the deterministic one is spliced in at the chunk/sweep boundary), so the
    reworded tier keeps only the higher-ranked one of the two: severity ties
    (both ``warning``), so confidence decides, and the deterministic finding's
    confidence is 1.0 against the model's 0.9. The model's copy is dropped as
    ``duplicate of chunk finding (reworded, similarity 0.45)``.
    """

    def test_exactly_one_finding_survives_the_toggle_line(self):
        forge = FakeForge(diff=_diff())
        llm = FakeLLM(findings_by_path={
            PROGRESS_PATH: [{
                "file": PROGRESS_PATH,
                "line": TOGGLE_LINE,
                "severity": "warning",
                "confidence": 0.9,
                "title": MODEL_TITLE,
                "body": MODEL_BODY,
            }],
        })
        res = orchestrate_review(forge, REF, llm, post=False, dedup_similarity=0.4)

        same_line = [
            f for f in _all_findings(res)
            if f.file == PROGRESS_PATH and f.line == TOGGLE_LINE
        ]
        assert len(same_line) == 2, "both the deterministic and the model copy were recorded"

        survivors = [f for f in same_line if f.drop_reason is None]
        assert len(survivors) == 1
        assert survivors[0].body.endswith(heuristics._BODY_SUFFIX), (
            "the deterministic finding (confidence 1.0) outranks the model's "
            "restatement (confidence 0.9) in the reworded tier's keep order"
        )

        dropped = [f for f in same_line if f.drop_reason is not None]
        assert len(dropped) == 1
        assert not dropped[0].body.endswith(heuristics._BODY_SUFFIX)
        assert dropped[0].drop_reason.startswith("duplicate of chunk finding (reworded")


class TestNegativeControl:
    """The same diff, pin value flipped to ``\"true\"``: no toggle finding."""

    def test_a_pin_set_to_true_yields_no_toggle_finding(self):
        forge = FakeForge(diff=_diff(pin_value="true"))
        res = orchestrate_review(forge, REF, FakeLLM(), post=False)

        assert _toggle_findings(res) == []
