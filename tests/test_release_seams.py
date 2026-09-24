"""The 0.14.0 seams no single feature seat could test: each needs another seat's code.

1. #67 config to CLI exit code: a malformed ``PRXREF_PRICE_TABLE`` is a
   configuration error, so ``main(["review", ...])`` exits 2 naming the
   variable before the forge, the LLM client or the orchestrator exists.
2. #67 cost through the REAL reviewer: an LLM double whose ``InvokeResult``
   carries a reported ``cost_usd`` drives the real reviewer and the real
   orchestrator, and the run record's ``cost_usd`` is the sum over every call,
   the chunk workers and the sweep. One call with no figure makes the run's
   cost unknown (``None``, never a partial sum), unless the price table prices
   that call's model, which makes the run ``cost_estimated``.
3. #68 x #67: the size advisory and the cost label are both summary additions.
   With both on, the real ``summary.md`` carries each exactly once, the
   advisory first and the label as the attribution's last field.
4. #63 x #64 x spec: a rules file, a ticket-context file and a spec source,
   all given on the command line, reach one run's worker and sweep prompts in
   the contract's order: rules, then ticket scope, in the system prompt; the
   ticket context, then the spec constraints, then the diff or digest, in the
   user prompt.

No test here uses the ``contract_stubs`` fixture: seams 2 to 4 are about what
the real reviewer renders and reports.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
import types
from unittest.mock import MagicMock

import pytest

from prxref import costs, orchestrator
from prxref.cli import main
from prxref.forges.base import ATTRIBUTION_MARKER, detect_forge
from prxref.llm import InvokeResult
from tests.test_cli_inputs import CLI_URL, _assert_nothing_ran, _install_fake_module
from tests.test_orchestrator import REF, FakeForge, _added_file_diff, multi_chunk_diff

CLEAN = json.dumps({"findings": [], "escalations": []})

_DIFF_TARGET = re.compile(r"^\+\+\+ b/(\S+)$", re.MULTILINE)


def _unit(user: str) -> str:
    """Name the review unit a rendered user prompt belongs to.

    ``"sweep"`` for the systemic sweep (its template has a ``### Digest``
    heading), else the one file whose diff the worker prompt carries.
    """
    if "\n### Digest\n" in user:
        return "sweep"
    targets = _DIFF_TARGET.findall(user)
    assert len(targets) == 1, targets
    return targets[0]


class _CostLLM:
    """A real-reviewer LLM double: records every prompt, answers no findings.

    ``cost_for(unit)`` is the figure the call REPORTS (``None`` for none) and
    ``model_for(unit)`` the model it names; ``unit`` is :func:`_unit` of the
    user prompt. Workers fan out on a thread pool, so recording is locked.
    """

    def __init__(self, cost_for, model_for=lambda unit: "cost-model-1"):
        self.cost_for = cost_for
        self.model_for = model_for
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        unit = _unit(user)
        cost = self.cost_for(unit)
        with self._lock:
            self.calls.append({"unit": unit, "system": system, "user": user, "cost": cost})
        return InvokeResult(
            text=CLEAN, input_tokens=100, output_tokens=50,
            model=self.model_for(unit), backend="fake", elapsed_ms=1,
            cost_usd=cost, cost_source="usage.cost" if cost is not None else "",
        )


# --- 1. #67 config -> CLI exit code ------------------------------------------


@pytest.fixture
def runtime(monkeypatch):
    """Doubles for everything past config, copied from tests/test_cli_inputs.py:
    the orchestrator, the LLM client and the forge are recorded, never built."""
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


class TestPriceTableConfigErrorExitsTwo:
    MALFORMED = [
        pytest.param('{"m1": {"input": 1.0', id="invalid-json"),
        pytest.param('{"m1": {"input": -1.0, "output": 2.0}}', id="negative-price"),
        pytest.param('{"m1": {"input": 1.0, "ouput": 2.0}}', id="unknown-field"),
        pytest.param("MISSING_FILE", id="missing-file"),
    ]

    @pytest.mark.parametrize("value", MALFORMED)
    def test_malformed_price_table_exits_2_naming_the_variable(
        self, runtime, monkeypatch, capsys, tmp_path, value,
    ):
        if value == "MISSING_FILE":
            value = str(tmp_path / "no-such-prices.json")
        monkeypatch.setenv("PRXREF_PRICE_TABLE", value)
        assert main(["review", "--pr-url", CLI_URL, "--no-post"]) == 2
        err = capsys.readouterr().err
        assert err.startswith("configuration error: PRXREF_PRICE_TABLE: "), err
        assert err.count("\n") == 1
        assert "Traceback" not in err
        _assert_nothing_ran(runtime)

    def test_control_a_valid_table_runs_and_reaches_the_orchestrator_parsed(
        self, runtime, monkeypatch, capsys,
    ):
        monkeypatch.setenv("PRXREF_PRICE_TABLE", '{"m1": {"input": 1.0, "output": 2.0}}')
        assert main(["review", "--pr-url", CLI_URL, "--no-post"]) == 0
        assert "configuration error" not in capsys.readouterr().err
        assert len(runtime.orchestrate) == 1
        assert runtime.orchestrate[0]["price_table"] == {
            "m1": costs.ModelPrice(input=1.0, output=2.0),
        }


# --- 2. #67 cost through the real reviewer ------------------------------------

# Powers of two, so every subset of units sums to a different total: a run that
# dropped the sweep, or any one chunk, cannot land on the full figure.
UNIT_COSTS = {
    "src/big1.py": 0.001,
    "src/big2.py": 0.002,
    "src/big3.py": 0.004,
    "sweep": 0.008,
}
TABLE_JSON = '{"silent-model": {"input": 1.0, "output": 2.0}}'
SILENT_ESTIMATE = (100 * 1.0 + 50 * 2.0) / 1_000_000


def _cost_run(llm, tmp_path, **kw):
    """A real run over three chunks plus the sweep: four calls, nothing posted."""
    forge = FakeForge(diff=multi_chunk_diff(3))
    return orchestrator.orchestrate_review(
        forge, REF, llm, post=False, trace_dir=str(tmp_path), **kw,
    )


def _trace_costs(tmp_path) -> dict[str, float | None]:
    return {
        label: json.loads((tmp_path / f"{label}.meta.json").read_text())["cost_usd"]
        for label in ("chunk0", "chunk1", "chunk2", "sweep")
    }


def _silent(unit_name: str):
    """An LLM whose ``unit_name`` call reports no figure, under its own model."""
    return _CostLLM(
        lambda unit: None if unit == unit_name else UNIT_COSTS[unit],
        model_for=lambda unit: "silent-model" if unit == unit_name else "cost-model-1",
    )


class TestRunCostThroughTheRealReviewer:
    def test_the_run_cost_is_the_sum_of_every_chunk_and_the_sweep(self, tmp_path):
        llm = _CostLLM(lambda unit: UNIT_COSTS[unit])
        res = _cost_run(llm, tmp_path)
        assert sorted(c["unit"] for c in llm.calls) == sorted(UNIT_COSTS)
        assert res["chunk_count"] == 4
        assert res["chunks_failed"] == 0
        assert res["cost_usd"] == pytest.approx(sum(UNIT_COSTS.values()))
        assert res["cost_estimated"] is False
        traced = _trace_costs(tmp_path)
        assert traced["sweep"] == UNIT_COSTS["sweep"]
        assert sorted(traced[f"chunk{i}"] for i in range(3)) == [0.001, 0.002, 0.004]

    @pytest.mark.parametrize("silent", ["src/big2.py", "sweep"])
    def test_one_call_without_a_figure_makes_the_run_unknown_never_a_partial_sum(
        self, tmp_path, caplog, silent,
    ):
        llm = _silent(silent)
        with caplog.at_level(logging.INFO, logger="prxref"):
            res = _cost_run(llm, tmp_path)
        assert len(llm.calls) == 4
        assert res["chunks_failed"] == 0
        assert res["cost_usd"] is None
        assert res["cost_estimated"] is False
        unknown = [r.getMessage() for r in caplog.records if r.getMessage().startswith("cost unknown")]
        assert len(unknown) == 1
        assert "'silent-model'" in unknown[0]
        assert "'cost-model-1'" not in unknown[0]

    @pytest.mark.parametrize("silent", ["src/big2.py", "sweep"])
    def test_a_table_entry_for_the_silent_model_makes_the_run_estimated(
        self, tmp_path, silent,
    ):
        """Control for the null rule: only the parsed price table differs."""
        table = costs.parse_price_table(TABLE_JSON)
        res = _cost_run(_silent(silent), tmp_path, price_table=table)
        reported = sum(v for unit, v in UNIT_COSTS.items() if unit != silent)
        assert res["cost_usd"] == pytest.approx(reported + SILENT_ESTIMATE)
        assert res["cost_estimated"] is True


# --- 3. #68 x #67: size advisory and cost label in one summary ----------------

ADVISORY_MESSAGE = (
    "This PR changes 20 lines in 1 file, above the team guideline of 5 lines. "
    "Consider splitting it."
)


class TestSizeAdvisoryAndCostLabelShareOneSummary:
    def test_each_appears_once_advisory_first_label_last(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        llm = _CostLLM(lambda unit: 0.0021)
        res = orchestrator.orchestrate_review(
            forge, REF, llm, post=True, post_mode="summary", post_cost=True,
            size_warn_lines=5,
        )
        assert len(llm.calls) == 2
        assert res["size_advisory"]["message"] == ADVISORY_MESSAGE
        assert res["cost_usd"] == pytest.approx(2 * 0.0021)
        assert res["cost_estimated"] is False
        advisory = f"> ⚠️ {ADVISORY_MESSAGE}\n\n"
        label = costs.cost_label(res["cost_usd"], res["cost_estimated"])
        assert label == "$0.0042"

        assert len(forge.summaries) == 1
        body = forge.summaries[0]
        assert body.startswith(advisory)
        assert body.count(advisory) == 1
        assert body.count("⚠️") == 1
        assert body[len(advisory):].startswith("## prxref automated review: ")
        assert body.count(label) == 1
        attribution = [line for line in body.splitlines() if line.startswith(ATTRIBUTION_MARKER)]
        assert len(attribution) == 1
        assert attribution[0].endswith(f" · {label}")
        assert body.rstrip("\n").endswith(attribution[0])


# --- 4. #63 x #64 x spec: prompt composition order ----------------------------


def _line_index(text: str, line: str) -> int:
    """Offset of the ONE line of ``text`` that is exactly ``line``."""
    hits = [m.start() for m in re.finditer(rf"^{re.escape(line)}$", text, re.MULTILINE)]
    assert len(hits) == 1, (line, len(hits))
    return hits[0]


def _once(text: str, needle: str) -> int:
    assert text.count(needle) == 1, (needle, text.count(needle))
    return text.index(needle)


class TestRulesTicketAndSpecComposeInContractOrder:
    """Through ``main``: config, both loaders, spec fetch, orchestrator and the
    real reviewer, with only the forge and the model faked."""

    RULES = "RULES-SENTINEL-6301"
    TICKET = "TICKET-SENTINEL-6402"
    SPEC = "SPEC-SENTINEL-5150"

    @pytest.fixture
    def llm(self, monkeypatch, tmp_path):
        assert sys.modules["prxref.orchestrator"] is orchestrator
        (tmp_path / "team-rules.md").write_text(
            f"Never log a token; cite {self.RULES}.\n", encoding="utf-8",
        )
        (tmp_path / "ticket.md").write_text(
            f"Ship the widget header flag ({self.TICKET}).\n\n"
            "Acceptance criteria:\n- [ ] the header carries the flag\n",
            encoding="utf-8",
        )
        (tmp_path / "spec.md").write_text(
            f"## Rules\n\nTools MUST carry the {self.SPEC} prefix.\n", encoding="utf-8",
        )
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        llm = _CostLLM(lambda unit: None)
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: REF)
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: forge)
        _install_fake_module(
            monkeypatch, "prxref.llm_backends", create_llm_client=lambda cfg: llm,
        )
        assert main([
            "review", "--pr-url", REF.url, "--no-post",
            "--rules-file", str(tmp_path / "team-rules.md"),
            "--context-file", str(tmp_path / "ticket.md"),
            "--spec", str(tmp_path / "spec.md"),
        ]) == 0
        assert forge.summaries == []
        assert sorted(c["unit"] for c in llm.calls) == ["src/app.py", "sweep"]
        return llm

    def _call(self, llm, unit: str) -> dict:
        return next(c for c in llm.calls if c["unit"] == unit)

    @pytest.mark.parametrize("unit", ["src/app.py", "sweep"])
    def test_system_prompt_is_rules_then_ticket_scope(self, llm, unit):
        system = self._call(llm, unit)["system"]
        rules = _line_index(system, "## Team review rules")
        sentinel = _once(system, self.RULES)
        scope = _line_index(system, "## Ticket scope")
        assert rules < sentinel < scope
        assert self.TICKET not in system
        assert self.SPEC not in system

    @pytest.mark.parametrize(
        ("unit", "body_heading"), [("src/app.py", "### Diff"), ("sweep", "### Digest")],
    )
    def test_user_prompt_is_ticket_then_spec_then_the_diff(self, llm, unit, body_heading):
        user = self._call(llm, unit)["user"]
        order = [
            _line_index(user, "## Review Context"),
            _line_index(user, "### Ticket context"),
            _once(user, self.TICKET),
            _line_index(user, "### Spec constraints"),
            _once(user, f"(MUST) Tools MUST carry the {self.SPEC} prefix"),
            _line_index(user, body_heading),
            _line_index(user, "## Output Format"),
        ]
        assert order == sorted(order)
        assert "(no specs provided for this review)" not in user
        assert self.RULES not in user
