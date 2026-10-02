"""``prxref eval verdict``: do candidate runs beat baseline runs by more than their run-to-run noise.

Each run is one case with four ``error`` and four ``warning`` labels, graded
by the deterministic tier (no judge), so a run's recall is set by how many
labels of each severity it hits, and its unmatched AI findings by how many
extra findings it raises.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from prxref import cli, evals
from prxref.eval_metrics import MATCH_METHOD
from tests.test_eval_compare import _case, _grade, _label, _put, _row, _score

LABELS = [_label(f"E{n}", "src/a.py", 10 * n, "error") for n in range(1, 5)]
LABELS += [_label(f"W{n}", "src/b.py", 10 * n, "warning") for n in range(1, 5)]


def _run(out: Path, label: str, errors: int, warnings: int = 2, extra: int = 0, strict: float | None = None) -> str:
    """Write a scored run that hits the first ``errors`` error and ``warnings`` warning labels.

    ``strict`` sets ``metrics.precision.strict`` the way ``eval score --precision`` records it.
    """
    hit = [f"E{n}" for n in range(1, errors + 1)] + [f"W{n}" for n in range(1, warnings + 1)]
    findings = [_row("src/a.py" if h[0] == "E" else "src/b.py", 10 * int(h[1]), h) for h in hit]
    findings += [_row("src/c.py", n, f"Extra {n}") for n in range(1, extra + 1)]
    grades = [_grade(lab.id, "full", hit.index(lab.id), method=MATCH_METHOD) if lab.id in hit
              else _grade(lab.id, "none", method=MATCH_METHOD) for lab in LABELS]
    score = _score(label, [_case("c1", LABELS, findings, grades)], None)
    if strict is not None:
        score["metrics"]["precision"] = {"graded": 4, "strict": strict, "lenient": min(1.0, strict + 0.1)}
    _put(out / label, score)
    return label


def _verdict(out: Path, baseline: list[str], candidate: list[str], capsys, *extra: str) -> tuple[int, str]:
    capsys.readouterr()
    argv = ["eval", "verdict", "--baseline", *baseline, "--candidate", *candidate, "--out", str(out), *extra]
    code = cli.main(argv)
    return code, capsys.readouterr().out


GOLDEN = """\
# prxref eval verdict

- Baseline: base-r1, base-r2
- Candidate: cand-r1, cand-r2
- Gate: Recall, severity `error`

## Runs

| Run | Side | Gate | Recall (micro) | Unmatched AI per PR |
|---|---|---:|---:|---:|
| base-r1 | baseline | 25.0% | 37.5% | 0.00 |
| base-r2 | baseline | 50.0% | 50.0% | 1.00 |
| cand-r1 | candidate | 75.0% | 62.5% | 0.00 |
| cand-r2 | candidate | 75.0% | 62.5% | 1.00 |

## Summary

| Metric | Baseline mean (range) | Candidate mean (range) | Change |
|---|---|---|---:|
| Gate | 37.5% (25.0% to 50.0%) | 75.0% (75.0% to 75.0%) | +37.5 pp |
| Recall (micro) | 43.8% (37.5% to 50.0%) | 62.5% (62.5% to 62.5%) | +18.8 pp |
| Unmatched AI per PR | 0.50 (0.00 to 1.00) | 0.50 (0.00 to 1.00) | 0.00 |

## Verdict

**better**: the candidate's mean gate 75.0% is above the best baseline run's 50.0%, with micro recall and \
unmatched AI findings per PR no worse than the worst baseline run.

strict precision is missing from at least one run, so the guard is unmatched AI findings per PR
"""


class TestTheVerdict:
    def test_a_candidate_above_the_best_baseline_run_is_better_and_exits_0(self, tmp_path, capsys):
        base = [_run(tmp_path, "base-r1", 1), _run(tmp_path, "base-r2", 2, extra=1)]
        cand = [_run(tmp_path, "cand-r1", 3), _run(tmp_path, "cand-r2", 3, extra=1)]
        code, text = _verdict(tmp_path, base, cand, capsys, "--severity", "error")
        assert code == 0
        assert text == GOLDEN
        assert text.isascii()

    def test_a_candidate_inside_the_baseline_range_is_within_noise_and_exits_1(self, tmp_path, capsys):
        base = [_run(tmp_path, "b1", 1), _run(tmp_path, "b2", 3)]
        cand = [_run(tmp_path, "c1", 2), _run(tmp_path, "c2", 3)]
        code, text = _verdict(tmp_path, base, cand, capsys, "--severity", "error")
        assert code == 1
        assert "**within noise**: the candidate's mean gate 62.5% is not above the best baseline run's 75.0%." in text

    def test_a_candidate_below_the_worst_baseline_run_is_worse(self, tmp_path, capsys):
        base = [_run(tmp_path, "b1", 2), _run(tmp_path, "b2", 3)]
        cand = [_run(tmp_path, "c1", 1), _run(tmp_path, "c2", 0)]
        code, text = _verdict(tmp_path, base, cand, capsys, "--severity", "error")
        assert code == 1
        assert "**worse**: the candidate's mean gate 12.5% is below the worst baseline run's 50.0%." in text

    def test_a_higher_gate_bought_with_lower_micro_recall_is_not_better(self, tmp_path, capsys):
        base = [_run(tmp_path, "b1", 1, warnings=4), _run(tmp_path, "b2", 2, warnings=4)]
        cand = [_run(tmp_path, "c1", 4, warnings=0), _run(tmp_path, "c2", 4, warnings=0)]
        code, text = _verdict(tmp_path, base, cand, capsys, "--severity", "error")
        assert code == 1
        assert "the mean micro recall 50.0% is below the worst baseline run's 62.5%." in text

    def test_a_higher_gate_bought_with_more_noise_is_not_better(self, tmp_path, capsys):
        base = [_run(tmp_path, "b1", 1, extra=1), _run(tmp_path, "b2", 2, extra=2)]
        cand = [_run(tmp_path, "c1", 4, extra=6), _run(tmp_path, "c2", 4, extra=8)]
        code, text = _verdict(tmp_path, base, cand, capsys, "--severity", "error")
        assert code == 1
        assert "the mean unmatched AI findings per PR 7.00 is above the worst baseline run's 2.00." in text

    def test_without_severity_the_gate_is_the_micro_recall(self, tmp_path, capsys):
        base = [_run(tmp_path, "b1", 1), _run(tmp_path, "b2", 2)]
        cand = [_run(tmp_path, "c1", 4, warnings=4)]
        code, text = _verdict(tmp_path, base, cand, capsys)
        assert code == 0
        assert "- Gate: Recall (micro)" in text
        assert "| c1 | candidate | 100.0% | 100.0% | 0.00 |" in text

    def test_a_single_baseline_run_says_it_has_no_noise_range(self, tmp_path, capsys):
        code, text = _verdict(tmp_path, [_run(tmp_path, "b1", 1)], [_run(tmp_path, "c1", 1)], capsys)
        assert code == 1
        assert text.endswith("The baseline has one run, so it has no noise range. Repeat it before adopting the "
                             "candidate.\n")

    def test_the_same_runs_print_byte_identical_output(self, tmp_path, capsys):
        base, cand = [_run(tmp_path, "b1", 1), _run(tmp_path, "b2", 2)], [_run(tmp_path, "c1", 3)]
        assert _verdict(tmp_path, base, cand, capsys) == _verdict(tmp_path, base, cand, capsys)


class TestRefusals:
    def test_a_severity_no_run_holds_exits_2_naming_the_run(self, tmp_path, capsys):
        capsys.readouterr()
        code = cli.main(["eval", "verdict", "--baseline", _run(tmp_path, "b1", 1), "--candidate",
                         _run(tmp_path, "c1", 1), "--severity", "spec", "--out", str(tmp_path)])
        assert code == 2
        assert ("configuration error: --baseline b1: the run has no scored label of severity 'spec', so it cannot "
                "be gated") in capsys.readouterr().err

    def test_a_missing_candidate_exits_2_naming_it(self, tmp_path, capsys):
        capsys.readouterr()
        code = cli.main(["eval", "verdict", "--baseline", _run(tmp_path, "b1", 1), "--candidate", "nope",
                         "--out", str(tmp_path)])
        assert code == 2
        assert "--candidate nope: there is no scored run 'nope'" in capsys.readouterr().err

    @pytest.mark.parametrize("missing", ["--baseline", "--candidate"])
    def test_both_sides_are_required(self, tmp_path, missing):
        argv = ["eval", "verdict", "--baseline", "b1", "--candidate", "c1"]
        at = argv.index(missing)
        with pytest.raises(SystemExit) as exc:
            cli._build_parser().parse_args(argv[:at] + argv[at + 2:])
        assert exc.value.code == 2

    def test_the_verdict_names_are_the_module_constants(self):
        assert (evals.VERDICT_BETTER, evals.VERDICT_WORSE, evals.VERDICT_NOISE) == ("better", "worse", "within noise")


class TestTheGuardSwitch:
    def test_strict_precision_is_the_guard_when_every_run_has_it(self, tmp_path, capsys):
        base = [_run(tmp_path, "b1", 1, strict=0.8), _run(tmp_path, "b2", 2, extra=1, strict=0.7)]
        cand = [_run(tmp_path, "c1", 3, extra=9, strict=0.75), _run(tmp_path, "c2", 3, extra=9, strict=0.85)]
        code, text = _verdict(tmp_path, base, cand, capsys, "--severity", "error")
        assert code == 0
        assert "| Run | Side | Gate | Recall (micro) | Strict precision | Unmatched AI per PR |" in text
        assert "| Strict precision | 75.0% (70.0% to 80.0%) | 80.0% (75.0% to 85.0%) | +5.0 pp |" in text
        assert ("**better**: the candidate's mean gate 75.0% is above the best baseline run's 50.0%, with micro recall "
                "and strict precision no worse than the worst baseline run.") in text
        assert "strict precision is missing" not in text

    def test_a_mean_strict_precision_below_the_worst_baseline_run_is_not_better(self, tmp_path, capsys):
        base = [_run(tmp_path, "b1", 1, strict=0.8), _run(tmp_path, "b2", 2, strict=0.7)]
        cand = [_run(tmp_path, "c1", 3, strict=0.6), _run(tmp_path, "c2", 3, strict=0.62)]
        code, text = _verdict(tmp_path, base, cand, capsys, "--severity", "error")
        assert code == 1
        assert ("**within noise**: the gate rose, but the mean strict precision 61.0% is below the worst baseline "
                "run's 70.0%.") in text

    def test_strict_precision_equal_to_the_worst_baseline_run_passes(self, tmp_path, capsys):
        base = [_run(tmp_path, "b1", 1, strict=0.8), _run(tmp_path, "b2", 2, strict=0.7)]
        cand = [_run(tmp_path, "c1", 3, strict=0.7), _run(tmp_path, "c2", 3, strict=0.7)]
        assert _verdict(tmp_path, base, cand, capsys, "--severity", "error")[0] == 0

    def test_one_run_without_strict_precision_falls_back_to_unmatched_and_says_so(self, tmp_path, capsys):
        base = [_run(tmp_path, "b1", 1, strict=0.8), _run(tmp_path, "b2", 2, strict=0.7)]
        cand = [_run(tmp_path, "c1", 3, strict=0.1), _run(tmp_path, "c2", 3)]
        code, text = _verdict(tmp_path, base, cand, capsys, "--severity", "error")
        assert code == 0
        assert "| Run | Side | Gate | Recall (micro) | Unmatched AI per PR |" in text
        assert ("\nstrict precision is missing from at least one run, so the guard is unmatched AI findings per PR\n"
                in text)

    def test_the_fallback_guard_still_blocks_on_unmatched_findings(self, tmp_path, capsys):
        base = [_run(tmp_path, "b1", 1, strict=0.8), _run(tmp_path, "b2", 2)]
        cand = [_run(tmp_path, "c1", 4, extra=5, strict=0.9), _run(tmp_path, "c2", 4, extra=5, strict=0.9)]
        code, text = _verdict(tmp_path, base, cand, capsys, "--severity", "error")
        assert code == 1
        assert "the mean unmatched AI findings per PR 5.00 is above the worst baseline run's 0.00." in text

    def test_the_note_precedes_the_single_baseline_sentence(self, tmp_path, capsys):
        _, text = _verdict(tmp_path, [_run(tmp_path, "b1", 1)], [_run(tmp_path, "c1", 1)], capsys)
        assert text.index("strict precision is missing") < text.index("The baseline has one run")


STAT_KEYS = ["mean", "min", "max", "values"]
SIDE_KEYS = ["runs", "gate", "recall", "precision", "unmatched_per_pr"]


class TestJsonOutput:
    def test_runs_mode_writes_the_spec_keys_in_order(self, tmp_path, capsys):
        base = [_run(tmp_path, "base-r1", 1), _run(tmp_path, "base-r2", 2, extra=1)]
        cand = [_run(tmp_path, "cand-r1", 3), _run(tmp_path, "cand-r2", 3, extra=1)]
        target = tmp_path / "deep" / "verdict.json"
        code, text = _verdict(tmp_path, base, cand, capsys, "--severity", "error", "--json", str(target))
        assert code == 0
        raw = target.read_text(encoding="utf-8")
        assert raw.endswith("}\n") and raw.startswith('{\n  "version": 1,\n')
        data = json.loads(raw)
        assert list(data) == ["version", "mode", "severity", "guard", "decision", "exit_code", "arms"]
        assert (data["mode"], data["severity"], data["guard"], data["decision"], data["exit_code"]) == (
            "runs", "error", "unmatched_per_pr", "adopt", 0)
        (arm,) = data["arms"]
        assert list(arm) == ["arm", "role", "verdict", "reason", "baseline", "candidate"]
        assert (arm["arm"], arm["role"], arm["verdict"]) == (None, None, "better")
        assert arm["reason"] in text
        assert list(arm["baseline"]) == SIDE_KEYS and list(arm["candidate"]) == SIDE_KEYS
        assert arm["baseline"]["runs"] == [str(tmp_path / "base-r1"), str(tmp_path / "base-r2")]
        assert list(arm["baseline"]["gate"]) == STAT_KEYS
        assert arm["baseline"]["gate"] == {"mean": 0.375, "min": 0.25, "max": 0.5, "values": [0.25, 0.5]}
        assert arm["baseline"]["unmatched_per_pr"]["values"] == [0.0, 1.0]
        assert arm["baseline"]["precision"] is None
        assert [p.name for p in target.parent.iterdir()] == ["verdict.json"]

    def test_the_strict_guard_and_precision_stats_are_recorded(self, tmp_path, capsys):
        base = [_run(tmp_path, "b1", 1, strict=0.8), _run(tmp_path, "b2", 2, strict=0.7)]
        cand = [_run(tmp_path, "c1", 3, strict=0.9)]
        target = tmp_path / "v.json"
        _verdict(tmp_path, base, cand, capsys, "--json", str(target))
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["severity"] is None and data["guard"] == "strict_precision"
        assert data["arms"][0]["baseline"]["precision"] == {"mean": 0.75, "min": 0.7, "max": 0.8, "values": [0.8, 0.7]}

    def test_a_not_better_candidate_records_exit_code_1(self, tmp_path, capsys):
        target = tmp_path / "v.json"
        code, _ = _verdict(tmp_path, [_run(tmp_path, "b1", 2)], [_run(tmp_path, "c1", 2)], capsys, "--json",
                           str(target))
        data = json.loads(target.read_text(encoding="utf-8"))
        assert (code, data["decision"], data["exit_code"], data["arms"][0]["verdict"]) == (
            1, "do not adopt", 1, "within noise")

    def test_the_markdown_is_unchanged_by_json(self, tmp_path, capsys):
        base, cand = [_run(tmp_path, "b1", 1), _run(tmp_path, "b2", 2)], [_run(tmp_path, "c1", 3)]
        plain = _verdict(tmp_path, base, cand, capsys)
        assert _verdict(tmp_path, base, cand, capsys, "--json", str(tmp_path / "v.json")) == plain

    def test_an_unwritable_path_exits_2_naming_the_flag(self, tmp_path, capsys):
        blocker = tmp_path / "file"
        blocker.write_text("x", encoding="utf-8")
        capsys.readouterr()
        code = cli.main(["eval", "verdict", "--baseline", _run(tmp_path, "b1", 1), "--candidate",
                         _run(tmp_path, "c1", 1), "--out", str(tmp_path), "--json", str(blocker / "v.json")])
        assert code == 2
        assert "configuration error: --json " in capsys.readouterr().err


def _campaign(root: Path, arms: dict[str, list[tuple[int, int]]], defs: dict[str, dict] | None = None,
              strict: float | None = None) -> str:
    """Write ``root`` as a campaign: per arm, one ``(error hits, extra findings)`` pair per repeat."""
    root.mkdir(parents=True, exist_ok=True)
    listed = [{"name": name, "rules_file": None, "scoped_rules": [], "prompts_dir": None, "mine_rules": None,
               **((defs or {}).get(name) or {})} for name in arms]
    (root / "campaign.json").write_text(json.dumps({"version": 1, "arms": listed}), encoding="utf-8")
    for name, repeats in arms.items():
        for k, (errors, extra) in enumerate(repeats, start=1):
            _run(root / "runs" / name, f"r{k}", errors, extra=extra, strict=strict)
    return str(root)


NO_RULES = {"rules_file": ""}
CAMPAIGN_ARMS = {"no-rules": NO_RULES, "rules": {"rules_file": "r.md"}}


def _campaign_verdict(tmp_path, base_arms, cand_arms, capsys, *extra, defs=CAMPAIGN_ARMS) -> tuple[int, str, dict]:
    base = _campaign(tmp_path / "A", base_arms, defs)
    cand = _campaign(tmp_path / "B", cand_arms, defs)
    target = tmp_path / "v.json"
    code, text = _verdict(tmp_path, [base], [cand], capsys, "--severity", "error", "--json", str(target), *extra)
    return code, text, json.loads(target.read_text(encoding="utf-8"))


BASE2 = [(1, 0), (2, 0)]
BETTER2 = [(3, 0), (3, 0)]
NOISE2 = [(1, 0), (2, 0)]
WORSE2 = [(0, 0), (0, 0)]


class TestCampaignMode:
    def test_every_rules_arm_better_and_no_rules_arm_in_noise_adopts(self, tmp_path, capsys):
        code, text, data = _campaign_verdict(tmp_path, {"no-rules": BASE2, "rules": BASE2},
                                             {"no-rules": NOISE2, "rules": BETTER2}, capsys)
        assert code == 0
        assert (data["mode"], data["decision"], data["exit_code"]) == ("campaign", "adopt", 0)
        assert [(a["arm"], a["role"], a["verdict"]) for a in data["arms"]] == [
            ("no-rules", "no-rules", "within noise"), ("rules", "rules", "better")]
        assert text.endswith("## Decision\n\n**adopt**: every rules arm is better and no no-rules arm is worse.\n")
        assert "## Arm no-rules\n\nRole: no-rules\n\n### Runs\n" in text
        assert "## Arm rules\n\nRole: rules\n" in text
        assert "| r1 | baseline |" in text and "| r2 | candidate |" in text
        assert data["arms"][0]["baseline"]["runs"] == [str(tmp_path / "A" / "runs" / "no-rules" / f"r{k}")
                                                       for k in (1, 2)]

    def test_a_rules_arm_within_noise_blocks_adoption(self, tmp_path, capsys):
        code, text, data = _campaign_verdict(tmp_path, {"no-rules": BASE2, "rules": BASE2},
                                             {"no-rules": BETTER2, "rules": NOISE2}, capsys)
        assert code == 1
        assert (data["decision"], data["exit_code"]) == ("do not adopt", 1)
        assert text.endswith("## Decision\n\n**do not adopt**: rules arm `rules` is within noise.\n")

    def test_a_no_rules_arm_that_is_worse_blocks_adoption(self, tmp_path, capsys):
        code, text, data = _campaign_verdict(tmp_path, {"no-rules": BASE2, "rules": BASE2},
                                             {"no-rules": WORSE2, "rules": BETTER2}, capsys)
        assert code == 1
        assert data["decision"] == "do not adopt"
        assert text.endswith("**do not adopt**: no-rules arm `no-rules` is worse.\n")

    def test_a_campaign_with_only_rules_arms_needs_every_arm_better(self, tmp_path, capsys):
        defs = {"a": {"rules_file": "a.md"}, "b": {"scoped_rules": ["x.md"]}}
        code, text, data = _campaign_verdict(tmp_path, {"a": BASE2, "b": BASE2}, {"a": BETTER2, "b": BETTER2}, capsys,
                                             defs=defs)
        assert code == 0 and data["decision"] == "adopt"
        assert {a["role"] for a in data["arms"]} == {"rules"}

    def test_a_campaign_with_only_no_rules_arms_needs_every_arm_better(self, tmp_path, capsys):
        defs = {"a": NO_RULES, "b": NO_RULES}
        code, text, _ = _campaign_verdict(tmp_path, {"a": BASE2, "b": BASE2}, {"a": BETTER2, "b": NOISE2}, capsys,
                                          defs=defs)
        assert code == 1
        assert text.endswith("**do not adopt**: arm `b` is within noise.\n")
        ok = _campaign_verdict(tmp_path / "ok", {"a": BASE2}, {"a": BETTER2}, capsys, defs=defs)
        assert ok[0] == 0 and "no arm uses rules and every arm is better." in ok[1]

    def test_an_arm_that_mines_rules_is_a_rules_arm(self, tmp_path, capsys):
        defs = {"m": {"rules_file": "", "mine_rules": {"judge_model": "j"}}}
        _, _, data = _campaign_verdict(tmp_path, {"m": BASE2}, {"m": BETTER2}, capsys, defs=defs)
        assert data["arms"][0]["role"] == "rules"

    def test_an_omitted_rules_file_is_a_rules_arm(self, tmp_path, capsys):
        _, _, data = _campaign_verdict(tmp_path, {"m": BASE2}, {"m": BETTER2}, capsys, defs={})
        assert data["arms"][0]["role"] == "rules"

    def test_an_arm_uses_strict_precision_when_its_runs_have_it(self, tmp_path, capsys):
        base = _campaign(tmp_path / "A", {"a": BASE2}, {"a": NO_RULES}, strict=0.8)
        cand = _campaign(tmp_path / "B", {"a": BETTER2}, {"a": NO_RULES}, strict=0.9)
        target = tmp_path / "v.json"
        _, text = _verdict(tmp_path, [base], [cand], capsys, "--severity", "error", "--json", str(target))
        assert json.loads(target.read_text(encoding="utf-8"))["guard"] == "strict_precision"
        assert "### Verdict\n\n**better**" in text and "strict precision is missing" not in text

    def test_the_label_form_resolves_a_campaign_under_out(self, tmp_path, capsys):
        _campaign(tmp_path / "A", {"a": BASE2}, {"a": NO_RULES})
        _campaign(tmp_path / "B", {"a": BETTER2}, {"a": NO_RULES})
        code, text = _verdict(tmp_path, ["A"], ["B"], capsys, "--severity", "error")
        assert code == 0 and "- Baseline: A\n- Candidate: B\n" in text

    def test_only_runs_holding_a_score_count_as_the_arms_repeats(self, tmp_path, capsys):
        base = _campaign(tmp_path / "A", {"a": BASE2}, {"a": NO_RULES})
        cand = _campaign(tmp_path / "B", {"a": BETTER2}, {"a": NO_RULES})
        (tmp_path / "B" / "runs" / "a" / "r3").mkdir()
        (tmp_path / "B" / "runs" / "a" / "notes").mkdir()
        _, text = _verdict(tmp_path, [base], [cand], capsys, "--severity", "error")
        assert "| r3 |" not in text and text.count("| candidate |") == 2

    def test_the_same_campaigns_print_byte_identical_output(self, tmp_path, capsys):
        base = _campaign(tmp_path / "A", {"a": BASE2}, {"a": NO_RULES})
        cand = _campaign(tmp_path / "B", {"a": BETTER2}, {"a": NO_RULES})
        assert _verdict(tmp_path, [base], [cand], capsys) == _verdict(tmp_path, [base], [cand], capsys)


class TestCampaignRefusals:
    def _err(self, tmp_path, capsys, baseline, candidate) -> tuple[int, str]:
        capsys.readouterr()
        code = cli.main(["eval", "verdict", "--baseline", *baseline, "--candidate", *candidate, "--out",
                         str(tmp_path)])
        return code, capsys.readouterr().err

    def test_a_campaign_against_a_run_exits_2(self, tmp_path, capsys):
        camp = _campaign(tmp_path / "A", {"a": BASE2}, {"a": NO_RULES})
        code, err = self._err(tmp_path, capsys, [camp], [_run(tmp_path, "c1", 1)])
        assert code == 2 and "configuration error: --candidate: a campaign directory" in err

    def test_a_run_against_a_campaign_exits_2(self, tmp_path, capsys):
        camp = _campaign(tmp_path / "A", {"a": BASE2}, {"a": NO_RULES})
        code, err = self._err(tmp_path, capsys, [_run(tmp_path, "b1", 1)], [camp])
        assert code == 2 and "configuration error: --baseline: a campaign directory" in err

    def test_a_campaign_among_several_values_exits_2(self, tmp_path, capsys):
        camp = _campaign(tmp_path / "A", {"a": BASE2}, {"a": NO_RULES})
        other = _campaign(tmp_path / "B", {"a": BASE2}, {"a": NO_RULES})
        code, err = self._err(tmp_path, capsys, [camp, _run(tmp_path, "b1", 1)], [other])
        assert code == 2 and "--baseline: a campaign directory" in err

    def test_an_arm_only_the_candidate_has_exits_2_naming_candidate(self, tmp_path, capsys):
        base = _campaign(tmp_path / "A", {"a": BASE2}, {"a": NO_RULES})
        cand = _campaign(tmp_path / "B", {"a": BASE2, "extra": BASE2}, {"a": NO_RULES, "extra": NO_RULES})
        code, err = self._err(tmp_path, capsys, [base], [cand])
        assert code == 2 and f"--candidate {cand}: arm 'extra' is not in the --baseline campaign" in err

    def test_an_arm_only_the_baseline_has_exits_2_naming_candidate(self, tmp_path, capsys):
        base = _campaign(tmp_path / "A", {"a": BASE2, "extra": BASE2}, {"a": NO_RULES, "extra": NO_RULES})
        cand = _campaign(tmp_path / "B", {"a": BASE2}, {"a": NO_RULES})
        code, err = self._err(tmp_path, capsys, [base], [cand])
        assert code == 2 and f"--candidate {cand}: it has no arm 'extra'" in err

    @pytest.mark.parametrize("side", ["A", "B"])
    def test_an_arm_with_no_scored_run_exits_2_naming_its_side(self, tmp_path, capsys, side):
        base = _campaign(tmp_path / "A", {"a": BASE2}, {"a": NO_RULES})
        cand = _campaign(tmp_path / "B", {"a": BASE2}, {"a": NO_RULES})
        for score in (tmp_path / side / "runs" / "a").glob("r*/score.json"):
            score.unlink()
        code, err = self._err(tmp_path, capsys, [base], [cand])
        flag = "--baseline" if side == "A" else "--candidate"
        assert code == 2 and f"configuration error: {flag} " in err and "arm 'a' has no scored run" in err

    def test_an_unreadable_campaign_json_exits_2(self, tmp_path, capsys):
        base = _campaign(tmp_path / "A", {"a": BASE2}, {"a": NO_RULES})
        cand = _campaign(tmp_path / "B", {"a": BASE2}, {"a": NO_RULES})
        (tmp_path / "B" / "campaign.json").write_text("{not json", encoding="utf-8")
        code, err = self._err(tmp_path, capsys, [base], [cand])
        assert code == 2 and f"--candidate {cand}: cannot read" in err

    @pytest.mark.parametrize("body", ['{"arms": []}', '[]', '{"arms": [{"name": "a b"}]}'])
    def test_a_campaign_json_without_usable_arms_exits_2(self, tmp_path, capsys, body):
        base = _campaign(tmp_path / "A", {"a": BASE2}, {"a": NO_RULES})
        cand = _campaign(tmp_path / "B", {"a": BASE2}, {"a": NO_RULES})
        (tmp_path / "A" / "campaign.json").write_text(body, encoding="utf-8")
        code, err = self._err(tmp_path, capsys, [base], [cand])
        assert code == 2 and f"--baseline {base}: " in err and "not a campaign.json" in err

    def test_a_severity_no_arm_run_holds_exits_2(self, tmp_path, capsys):
        base = _campaign(tmp_path / "A", {"a": BASE2}, {"a": NO_RULES})
        cand = _campaign(tmp_path / "B", {"a": BASE2}, {"a": NO_RULES})
        capsys.readouterr()
        code = cli.main(["eval", "verdict", "--baseline", base, "--candidate", cand, "--severity", "spec"])
        assert code == 2 and "no scored label of severity 'spec'" in capsys.readouterr().err
