"""The ``prxref eval`` subcommand: its parser, its dispatch, and its README rows.

``eval`` has three actions, ``run``, ``score`` and ``compare``. The parser
tests pin every flag's ``dest`` and default, because the seats that build the
actions read them from the namespace. The dispatch tests go through
``cli.main``, the entry point the console script calls, with the three
``prxref.evals`` functions replaced by recorders.
"""
from __future__ import annotations

import argparse
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

from prxref import cli, evals
from prxref.llm import ConfigError

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")

EVAL_ACTIONS = ("run", "score", "compare")


def _parse(*argv: str) -> argparse.Namespace:
    return cli._build_parser().parse_args(list(argv))


def _subparser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[name]
    raise AssertionError(f"no subparsers under {parser.prog}")


def _option_strings(parser: argparse.ArgumentParser) -> set[str]:
    found: set[str] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                found |= _option_strings(sub)
        elif not isinstance(action, argparse._HelpAction):
            found.update(action.option_strings)
    return found


def _eval_block() -> str:
    start = README.index("\n## CLI Flags\n")
    end = README.index("\n## ", start + 1)
    section = README[start:end]
    return section[section.index("`prxref eval` scores") :]


class TestEvalRunFlags:
    def test_the_required_flags_parse_and_every_other_flag_takes_its_default(self):
        args = _parse("eval", "run", "--cases", "cases.json", "--label", "baseline")
        assert args.command == "eval"
        assert args.eval_command == "run"
        assert args.cases == "cases.json"
        assert args.label == "baseline"
        assert args.out == "./prxref-eval/"
        assert args.rules_file is None
        assert args.resume is False

    def test_every_flag_parses_to_its_dest(self):
        args = _parse(
            "eval", "run", "--cases", "tests/evals", "--label", "rules-v2",
            "--out", "/tmp/evals", "--rules-file", "RULES.md", "--resume",
        )
        assert (args.cases, args.label, args.out, args.rules_file, args.resume) == (
            "tests/evals", "rules-v2", "/tmp/evals", "RULES.md", True,
        )

    def test_an_empty_rules_file_is_kept_so_it_can_turn_the_variable_off(self):
        args = _parse("eval", "run", "--cases", "c.json", "--label", "L", "--rules-file", "")
        assert args.rules_file == ""

    @pytest.mark.parametrize(
        "argv, flag",
        [
            (["eval", "run", "--label", "L"], "--cases"),
            (["eval", "run", "--cases", "c.json"], "--label"),
        ],
    )
    def test_a_missing_required_flag_exits_2_naming_it(self, argv, flag, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(argv)
        assert excinfo.value.code == 2
        assert flag in capsys.readouterr().err


class TestEvalScoreFlags:
    def test_the_label_parses_and_every_other_flag_takes_its_default(self):
        args = _parse("eval", "score", "--label", "baseline")
        assert args.eval_command == "score"
        assert args.label == "baseline"
        assert args.judge_model is None
        assert args.out == "./prxref-eval/"

    def test_every_flag_parses_to_its_dest(self):
        args = _parse("eval", "score", "--label", "L", "--judge-model", "judge/model-x", "--out", "runs")
        assert (args.label, args.judge_model, args.out) == ("L", "judge/model-x", "runs")

    def test_a_missing_label_exits_2_naming_it(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["eval", "score", "--judge-model", "m"])
        assert excinfo.value.code == 2
        assert "--label" in capsys.readouterr().err


class TestEvalCompareArgs:
    def test_both_positionals_parse_and_out_takes_its_default(self):
        args = _parse("eval", "compare", "baseline", "candidate")
        assert args.eval_command == "compare"
        assert (args.run_a, args.run_b) == ("baseline", "candidate")
        assert args.out == "./prxref-eval/"

    def test_out_parses_to_its_dest(self):
        args = _parse("eval", "compare", "a", "runs/b", "--out", "elsewhere")
        assert (args.run_a, args.run_b, args.out) == ("a", "runs/b", "elsewhere")

    @pytest.mark.parametrize("argv", [["eval", "compare"], ["eval", "compare", "only-one"]])
    def test_fewer_than_two_runs_exits_2(self, argv, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(argv)
        assert excinfo.value.code == 2
        assert "usage:" in capsys.readouterr().err.lower()


class TestEvalParserShape:
    def test_eval_has_exactly_the_three_actions(self):
        ev = _subparser(cli._build_parser(), "eval")
        actions = next(a for a in ev._actions if isinstance(a, argparse._SubParsersAction))
        assert tuple(actions.choices) == EVAL_ACTIONS

    def test_the_eval_option_strings_are_exactly_these(self):
        ev = _subparser(cli._build_parser(), "eval")
        assert _option_strings(ev) == {"--cases", "--label", "--out", "--rules-file", "--resume", "--judge-model"}

    def test_an_unknown_action_exits_2(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["eval", "grade"])
        assert excinfo.value.code == 2
        capsys.readouterr()


class TestBareEval:
    def test_eval_without_an_action_exits_2_with_usage(self, capsys):
        assert cli.main(["eval"]) == 2
        assert "usage:" in capsys.readouterr().err.lower()


@pytest.fixture
def recorders(monkeypatch):
    calls: dict[str, list[argparse.Namespace]] = {name: [] for name in EVAL_ACTIONS}
    codes = {"run": 0, "score": 3, "compare": 5}

    def _recorder(name: str):
        def _record(args: argparse.Namespace) -> int:
            calls[name].append(args)
            return codes[name]

        return _record

    for name in EVAL_ACTIONS:
        monkeypatch.setattr(evals, f"eval_{name}", _recorder(name))
    return calls


class TestDispatch:
    def test_run_routes_to_eval_run_with_its_parsed_args(self, recorders):
        code = cli.main([
            "eval", "run", "--cases", "cases.json", "--label", "L",
            "--out", "D", "--rules-file", "R.md", "--resume",
        ])
        assert code == 0
        assert [len(recorders[n]) for n in EVAL_ACTIONS] == [1, 0, 0]
        args = recorders["run"][0]
        assert (args.eval_command, args.cases, args.label, args.out, args.rules_file, args.resume) == (
            "run", "cases.json", "L", "D", "R.md", True,
        )

    def test_score_routes_to_eval_score_and_returns_its_exit_code(self, recorders):
        code = cli.main(["eval", "score", "--label", "L", "--judge-model", "judge-x"])
        assert code == 3
        assert [len(recorders[n]) for n in EVAL_ACTIONS] == [0, 1, 0]
        args = recorders["score"][0]
        assert (args.eval_command, args.label, args.judge_model, args.out) == (
            "score", "L", "judge-x", "./prxref-eval/",
        )

    def test_compare_routes_to_eval_compare_and_returns_its_exit_code(self, recorders):
        code = cli.main(["eval", "compare", "A1", "B1", "--out", "D"])
        assert code == 5
        assert [len(recorders[n]) for n in EVAL_ACTIONS] == [0, 0, 1]
        args = recorders["compare"][0]
        assert (args.eval_command, args.run_a, args.run_b, args.out) == ("compare", "A1", "B1", "D")

    @pytest.mark.parametrize(
        "action, argv",
        [
            ("run", ["eval", "run", "--cases", "c.json", "--label", "L"]),
            ("score", ["eval", "score", "--label", "L"]),
            ("compare", ["eval", "compare", "A", "B"]),
        ],
    )
    def test_a_config_error_exits_2_printed_as_review_prints_one(self, action, argv, monkeypatch, capsys):
        def _refuse(args: argparse.Namespace) -> int:
            raise ConfigError("--judge-model: required because 2 labels have no must_match")

        monkeypatch.setattr(evals, f"eval_{action}", _refuse)
        assert cli.main(argv) == 2
        err = capsys.readouterr().err
        assert "configuration error: --judge-model: required because 2 labels have no must_match" in err

    def test_an_error_that_is_not_a_config_error_is_not_turned_into_exit_2(self, monkeypatch):
        def _crash(args: argparse.Namespace) -> int:
            raise RuntimeError("boom")

        monkeypatch.setattr(evals, "eval_run", _crash)
        with pytest.raises(RuntimeError, match="boom"):
            cli.main(["eval", "run", "--cases", "c.json", "--label", "L"])


class TestEvalsModule:
    @pytest.mark.parametrize("name", [f"eval_{action}" for action in EVAL_ACTIONS])
    def test_each_action_takes_only_the_namespace(self, name):
        params = list(inspect.signature(getattr(evals, name)).parameters)
        assert params == ["args"]

    @pytest.mark.parametrize("name", [f"eval_{action}" for action in EVAL_ACTIONS])
    def test_each_action_documents_its_contract(self, name):
        assert (getattr(evals, name).__doc__ or "").strip()

    def test_importing_evals_does_not_import_the_cli(self):
        probe = "import sys, prxref.evals; sys.exit(1 if 'prxref.cli' in sys.modules else 0)"
        control = "import sys, prxref.cli; sys.exit(1 if 'prxref.cli' in sys.modules else 0)"
        assert subprocess.run([sys.executable, "-c", probe], check=False).returncode == 0
        assert subprocess.run([sys.executable, "-c", control], check=False).returncode == 1

    def test_the_cli_imports_evals_lazily(self):
        probe = "import sys, prxref.cli; sys.exit(1 if 'prxref.evals' in sys.modules else 0)"
        assert subprocess.run([sys.executable, "-c", probe], check=False).returncode == 0


def _mentions(text: str, flag: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", text) is not None


class TestReadmeEvalRows:
    @pytest.mark.parametrize("flag", sorted(_option_strings(_subparser(cli._build_parser(), "eval"))))
    def test_every_eval_flag_is_documented_in_the_eval_block(self, flag):
        assert _mentions(_eval_block(), flag)

    @pytest.mark.parametrize("action", EVAL_ACTIONS)
    def test_every_action_has_a_row(self, action):
        assert f"`prxref eval {action} " in _eval_block()

    def test_the_block_states_the_default_out(self):
        assert "`./prxref-eval/`" in _eval_block()

    def test_the_block_sits_inside_the_cli_flags_section(self):
        start = README.index("\n## CLI Flags\n")
        end = README.index("\n## ", start + 1)
        assert start < README.index("`prxref eval` scores") < end
