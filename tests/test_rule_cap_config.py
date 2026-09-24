"""PRXREF_MAX_FINDINGS_PER_RULE (#18): the per-rule cap's configuration key.

The key is an int that defaults to 2, not ``None``: the cap is on by default
whenever a review rules file is loaded, and ``0`` is the declared off. So the
low bound is inclusive, and an empty value reads as unset, which keeps the
default. The pass itself lives in ``quality.apply_rule_cap``; this module pins
the key, its documentation on every surface, the eval run's config allowlist,
and the pass counts the new pass moved.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from prxref import cli, config, evals
from prxref.config import load_config
from prxref.llm import ConfigError
from tests.test_cli_output import _option_strings
from tests.test_config import _KEYS_0_15, _doc_entry
from tests.test_docs_consistency import SURFACES

KEY = "max_findings_per_rule"
ENV = "PRXREF_MAX_FINDINGS_PER_RULE"
DROP_REASON = "rule cap exceeded (max <n>): listed at <file>:<line>"

REPO_ROOT = Path(__file__).resolve().parents[1]
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
QUALITY_MD = (REPO_ROOT / "docs" / "quality.md").read_text(encoding="utf-8")
ENV_VARS_MD = SURFACES["docs/env-vars.md"]
EVALS_MD = (REPO_ROOT / "docs" / "evals.md").read_text(encoding="utf-8")

NUMBER_WORDS = {
    10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen",
    15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen",
}


def _section(text: str, heading: str) -> str:
    start = text.index(f"\n{heading}\n")
    end = text.find("\n## ", start + len(heading) + 2)
    return text[start : end if end != -1 else len(text)]


def _pass_table() -> list[tuple[int, str]]:
    section = _section(QUALITY_MD, "## The passes, in the order they run")
    return [
        (int(m.group(1)), m.group(2))
        for m in re.finditer(r"^\| (\d+) \| `(\w+)` \|", section, re.M)
    ]


class TestTheKey:
    def test_default_is_two(self):
        assert config._DEFAULTS[KEY] == 2
        value = load_config()[KEY]
        assert value == 2
        assert isinstance(value, int) and not isinstance(value, bool)

    def test_declared_as_an_int_with_an_inclusive_zero_bound(self):
        assert KEY in config._INT_KEYS
        assert KEY not in config._FLOAT_KEYS | config._BOOL_KEYS | config._LIST_KEYS
        assert KEY not in config._CHOICE_KEYS
        assert config._RANGES[KEY] == config._Range(0, low_inclusive=True)

    def test_the_env_name_is_derived_and_cleared_suite_wide(self):
        assert config._ENV_PREFIX + KEY.upper() == ENV
        assert (KEY, ENV) in _KEYS_0_15

    def test_it_sits_after_the_outofscope_cap_in_the_defaults(self):
        keys = list(config._DEFAULTS)
        assert keys.index(KEY) == keys.index("max_outofscope_findings") + 1

    @pytest.mark.parametrize("raw,expected", [("0", 0), ("1", 1), (" 5 ", 5), ("100000", 100_000)])
    def test_env_coerces_to_an_int(self, monkeypatch, raw, expected):
        monkeypatch.setenv(ENV, raw)
        value = load_config()[KEY]
        assert value == expected
        assert isinstance(value, int)

    def test_zero_is_accepted_and_is_not_the_default(self, monkeypatch):
        monkeypatch.setenv(ENV, "0")
        assert load_config()[KEY] == 0

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_empty_or_whitespace_keeps_the_default(self, monkeypatch, raw):
        monkeypatch.setenv(ENV, raw)
        assert load_config()[KEY] == 2

    @pytest.mark.parametrize("raw", ["-1", "-50"])
    def test_a_negative_value_is_rejected_naming_the_variable(self, monkeypatch, raw):
        monkeypatch.setenv(ENV, raw)
        with pytest.raises(ConfigError, match=rf"^{ENV}: ") as exc:
            load_config()
        assert "greater than or equal to 0" in str(exc.value)

    @pytest.mark.parametrize("raw", ["x", "two", "1.5", "off"])
    def test_a_non_integer_is_rejected_naming_the_variable(self, monkeypatch, raw):
        monkeypatch.setenv(ENV, raw)
        with pytest.raises(ConfigError, match=rf"^{ENV}: "):
            load_config()

    def test_an_override_cannot_smuggle_a_negative_cap(self):
        with pytest.raises(ConfigError, match=rf"^{KEY}: ") as exc:
            load_config(**{KEY: -1})
        assert ENV not in str(exc.value)

    def test_an_override_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv(ENV, "7")
        assert load_config(**{KEY: 0})[KEY] == 0

    def test_it_is_independent_of_the_severity_caps(self, monkeypatch):
        monkeypatch.setenv(ENV, "3")
        cfg = load_config()
        assert cfg[KEY] == 3
        assert cfg["max_warning_findings"] is None
        assert cfg["max_outofscope_findings"] is None

    def test_a_bad_value_exits_2_through_the_entry_point(self, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(cli, "_run_review", lambda *a, **k: calls.append(a))
        monkeypatch.setenv(ENV, "many")

        rc = cli.main(["review", "--pr-url", "https://github.com/org/repo/pull/7"])

        assert rc == 2
        assert f"configuration error: {ENV}: " in capsys.readouterr().err
        assert calls == []

    def test_there_is_no_cli_flag(self):
        flags = _option_strings(cli._build_parser())
        assert flags, "the parser walk found no flag, so the check is vacuous"
        assert not [flag for flag in flags if "per-rule" in flag or "per_rule" in flag]


class TestEverySurfaceDocumentsIt:
    @pytest.mark.parametrize("surface", sorted(SURFACES))
    def test_one_entry_names_the_release_and_both_rules_inputs(self, surface):
        entry = _doc_entry(surface, ENV)
        assert "0.15.0" in entry
        assert "PRXREF_REVIEW_RULES" in entry
        assert "PRXREF_SCOPED_RULES" in entry
        assert re.search(r"\b0\b[^.;]*\boff\b", entry), entry

    @pytest.mark.parametrize("surface,default", [
        ("docs/env-vars.md", f"| `{ENV}` | `2` |"),
        (".env.example", f"# {ENV}=2"),
        ("src/prxref/config.py (module docstring)", "(default 2)"),
    ])
    def test_each_surface_states_the_default(self, surface, default):
        assert default in _doc_entry(surface, ENV)

    def test_the_env_example_block_ends_with_the_commented_default(self):
        block = next(p for p in SURFACES[".env.example"].split("\n\n") if f"# {ENV}=" in p)
        assert block.rstrip("\n").splitlines()[-1] == f"# {ENV}=2"

    def test_the_env_vars_row_sits_after_the_outofscope_cap(self):
        rows = [ln.split("|")[1].strip() for ln in ENV_VARS_MD.splitlines() if ln.startswith("| `PRXREF_")]
        assert rows.index(f"`{ENV}`") == rows.index("`PRXREF_MAX_OUTOFSCOPE_FINDINGS`") + 1

    def test_the_env_vars_row_names_the_drop_reason(self):
        entry = _doc_entry("docs/env-vars.md", ENV)
        assert "`rule cap exceeded (max N): listed at <file>:<line>`" in entry
        assert "**Per-rule cap (0.15.0).**" in entry

    def test_the_category_list_names_it_after_the_outofscope_cap(self):
        line = next(ln for ln in ENV_VARS_MD.splitlines() if ln.startswith("- **LLM / Pipeline ("))
        names = re.findall(r"`(PRXREF_[A-Z0-9_]+)`", line)
        assert names.index(ENV) == names.index("PRXREF_MAX_OUTOFSCOPE_FINDINGS") + 1
        assert f"- **LLM / Pipeline ({len(names)}):**" in line

    def test_the_filtering_paragraph_counts_it_as_a_lever(self):
        section = _section(ENV_VARS_MD, "## Quality Passes and Drop Reasons")
        assert f"`{ENV}`" in section
        assert "the per-rule cap" in section


class TestQualityDoc:
    def test_the_pass_table_is_numbered_in_order(self):
        rows = _pass_table()
        assert [number for number, _ in rows] == list(range(1, len(rows) + 1))

    def test_the_rule_cap_runs_after_grouping_and_before_the_gate(self):
        names = [name for _, name in _pass_table()]
        assert names.index("apply_rule_cap") == names.index("apply_rule_grouping") + 1
        assert names.index("apply_quality_gate") == names.index("apply_rule_cap") + 1

    def test_the_drop_reason_row_follows_grouped_into(self):
        section = _section(QUALITY_MD, "## Drop reasons")
        reasons = [ln.split(" | ")[0].lstrip("| ") for ln in section.splitlines() if ln.startswith("| `")]
        assert reasons.index(f"`{DROP_REASON}`") == reasons.index("`grouped into <file>:<line>`") + 1
        assert f"| `{DROP_REASON}` | `apply_rule_cap` |" in section

    def test_the_tunable_list_names_the_key(self):
        assert f"`{ENV}`" in _section(QUALITY_MD, "## What is and is not tunable")

    def test_the_json_note_names_both_conditions_for_null(self):
        flat = " ".join(QUALITY_MD.split())
        assert (
            "With grouping off and the per-rule cap inactive (no review rules file, or "
            f"`{ENV}` set to `0`), `rule` and `locations` are `null` on every row."
        ) in flat

    @pytest.mark.parametrize("where,phrase", [
        ("README.md", "through {word} more deterministic passes"),
        ("docs/env-vars.md", "The {word} deterministic passes"),
    ])
    def test_the_stated_pass_count_matches_the_table(self, where, phrase):
        text = {"README.md": README, "docs/env-vars.md": ENV_VARS_MD}[where]
        word = NUMBER_WORDS[len(_pass_table())]
        assert phrase.format(word=word) in " ".join(text.split())

    def test_the_readme_names_the_cap_between_grouping_and_the_gate(self):
        flat = " ".join(README.split())
        sentence = flat[flat.index("more deterministic passes:") :]
        sentence = sentence[: sentence.index("containment note")]
        assert sentence.index("finding grouping") < sentence.index("the per-rule cap")
        assert sentence.index("the per-rule cap") < sentence.index("the quality gate")
        assert f"`{ENV}`" in sentence


class TestEvalRunConfig:
    def test_the_key_is_allowlisted_after_the_outofscope_cap(self):
        keys = list(evals.RUN_CONFIG_KEYS)
        assert keys.index(KEY) == keys.index("max_outofscope_findings") + 1

    def test_the_run_json_table_lists_the_allowlist_in_order(self):
        row = next(ln for ln in EVALS_MD.splitlines() if ln.startswith("| `config` |"))
        assert re.findall(r"`(\w+)`", row)[1:] == list(evals.RUN_CONFIG_KEYS)

    def test_the_allowlist_size_is_stated_in_words(self):
        word = NUMBER_WORDS[len(evals.RUN_CONFIG_KEYS)]
        flat = " ".join(EVALS_MD.split())
        assert f"`config` is an allowlist of the {word} settings above" in flat
