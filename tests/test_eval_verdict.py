"""``prxref eval verdict``: do candidate runs beat baseline runs by more than their run-to-run noise.

Each run is one case with four ``error`` and four ``warning`` labels, graded
by the deterministic tier (no judge), so a run's recall is set by how many
labels of each severity it hits, and its unmatched AI findings by how many
extra findings it raises.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from prxref import cli, evals
from prxref.eval_metrics import MATCH_METHOD
from tests.test_eval_compare import _case, _grade, _label, _put, _row, _score

LABELS = [_label(f"E{n}", "src/a.py", 10 * n, "error") for n in range(1, 5)]
LABELS += [_label(f"W{n}", "src/b.py", 10 * n, "warning") for n in range(1, 5)]


def _run(out: Path, label: str, errors: int, warnings: int = 2, extra: int = 0) -> str:
    """Write a scored run that hits the first ``errors`` error and ``warnings`` warning labels."""
    hit = [f"E{n}" for n in range(1, errors + 1)] + [f"W{n}" for n in range(1, warnings + 1)]
    findings = [_row("src/a.py" if h[0] == "E" else "src/b.py", 10 * int(h[1]), h) for h in hit]
    findings += [_row("src/c.py", n, f"Extra {n}") for n in range(1, extra + 1)]
    grades = [_grade(lab.id, "full", hit.index(lab.id), method=MATCH_METHOD) if lab.id in hit
              else _grade(lab.id, "none", method=MATCH_METHOD) for lab in LABELS]
    _put(out / label, _score(label, [_case("c1", LABELS, findings, grades)], None))
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
