"""Tests for prxref.costs: price-table parsing, reported-cost validation, the
run total and its null rule, and the dollar labels.

The issue #67 acceptance cases are the three ``run_cost`` states: passthrough
(reported), estimate (flagged), and unknown (``None``, never ``0``).
"""
from __future__ import annotations

import ast
import math
import sys
from pathlib import Path

import pytest

from prxref import costs
from prxref.costs import (
    ModelPrice,
    combine_reported,
    cost_label,
    estimate_usd,
    format_usd,
    parse_price_table,
    run_cost,
    unit_cost,
    valid_usd,
)
from prxref.llm import ConfigError

MINI = "openai/gpt-4o-mini"
TABLE = {MINI: ModelPrice(0.15, 0.60)}


def _unit(model: str = MINI, input_tokens: int = 1000, output_tokens: int = 100, **extra) -> dict:
    """An orchestrator unit dict shaped like a finished worker or sweep result."""
    unit = {
        "findings": [],
        "error": None,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "elapsed_ms": 10,
    }
    unit.update(extra)
    return unit


def _raised_unit() -> dict:
    """A unit whose request raised: no model, no tokens, an error."""
    return _unit(model="", input_tokens=0, output_tokens=0, error="LLMError: deadline", cost_usd=None, cost_source="")


class TestParsePriceTable:
    @pytest.mark.parametrize("raw", [None, "", "   ", "\n\t"])
    def test_unset_empty_and_whitespace_mean_no_table(self, raw):
        assert parse_price_table(raw) == {}

    def test_inline_json_object_is_parsed_into_model_prices(self):
        table = parse_price_table(
            ' {"openai/gpt-4o-mini": {"input": 0.15, "output": 0.60}, "m2": {"input": 3, "output": 15}}'
        )
        assert table == {MINI: ModelPrice(0.15, 0.60), "m2": ModelPrice(3.0, 15.0)}
        assert all(isinstance(p, ModelPrice) for p in table.values())
        assert isinstance(table["m2"].input, float)

    def test_a_path_is_read_as_a_json_file(self, tmp_path):
        path = tmp_path / "prices.json"
        path.write_text('{"m": {"input": 1.5, "output": 2}}', encoding="utf-8")
        assert parse_price_table(str(path)) == {"m": ModelPrice(1.5, 2.0)}

    def test_a_json_file_with_a_bom_is_read(self, tmp_path):
        path = tmp_path / "prices.json"
        path.write_bytes(b'\xef\xbb\xbf{"m": {"input": 1, "output": 2}}')
        assert parse_price_table(str(path)) == {"m": ModelPrice(1.0, 2.0)}

    def test_tilde_in_the_path_is_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "prices.json").write_text('{"m": {"input": 1, "output": 2}}', encoding="utf-8")
        assert parse_price_table("~/prices.json") == {"m": ModelPrice(1.0, 2.0)}

    def test_a_mapping_override_is_validated_like_a_string(self):
        assert parse_price_table({" m ": {"input": 1, "output": 0}}) == {"m": ModelPrice(1.0, 0.0)}
        with pytest.raises(ConfigError, match="PRXREF_PRICE_TABLE: 'm' input must be a finite number"):
            parse_price_table({"m": {"input": -1, "output": 0}})

    def test_the_result_is_a_new_dict(self):
        given = {"m": {"input": 1, "output": 2}}
        table = parse_price_table(given)
        table["other"] = ModelPrice(0, 0)
        assert "other" not in given

    def test_invalid_json_is_a_config_error_naming_the_variable(self):
        with pytest.raises(ConfigError, match=r"^PRXREF_PRICE_TABLE: not valid JSON \(.* at line 1 column \d+\)$"):
            parse_price_table('{"m": {"input": 1, "output": 2}')

    def test_invalid_json_in_a_file_names_the_file(self, tmp_path):
        path = tmp_path / "prices.json"
        path.write_text("{nope", encoding="utf-8")
        with pytest.raises(ConfigError, match=r"not valid JSON .* in '.*prices\.json'"):
            parse_price_table(str(path))

    def test_an_unreadable_path_is_a_config_error_naming_variable_and_path(self, tmp_path):
        missing = tmp_path / "nope.json"
        with pytest.raises(ConfigError) as info:
            parse_price_table(str(missing))
        message = str(info.value)
        assert message.startswith("PRXREF_PRICE_TABLE: cannot read price table file")
        assert str(missing) in message
        assert "No such file or directory" in message
        assert "inline JSON must start with '{'" in message

    def test_a_directory_path_is_a_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="cannot read price table file"):
            parse_price_table(str(tmp_path))

    def test_a_file_that_is_not_utf8_is_a_config_error(self, tmp_path):
        path = tmp_path / "prices.json"
        path.write_bytes(b'{"m\xff": {"input": 1, "output": 2}}')
        with pytest.raises(ConfigError, match="is not valid UTF-8"):
            parse_price_table(str(path))

    @pytest.mark.parametrize(
        ("raw", "kind"),
        [("[1, 2]", "an array"), ('"text"', "the string 'text'"), ("3", "the number 3"), ("null", "null")],
    )
    def test_top_level_must_be_an_object(self, tmp_path, raw, kind):
        path = tmp_path / "prices.json"
        path.write_text(raw, encoding="utf-8")
        with pytest.raises(ConfigError, match="must be a JSON object mapping model name") as info:
            parse_price_table(str(path))
        assert str(info.value).endswith(f"got {kind}")

    @pytest.mark.parametrize(
        ("entry", "fragment"),
        [
            ('{"input": 0.15}', "is missing 'output'"),
            ("{}", "is missing 'input' and 'output'"),
            ('{"input": 0.15, "ouput": 0.6}', "unknown key(s) 'ouput'"),
            ('{"input": -0.01, "output": 0.6}', "input must be a finite number >= 0"),
            ('{"input": NaN, "output": 0.6}', "got nan"),
            ('{"input": 0.15, "output": Infinity}', "got inf"),
            ('{"input": 0.15, "output": -Infinity}', "got -inf"),
            ('{"input": true, "output": 0.6}', "got the boolean true"),
            ('{"input": "0.15", "output": 0.6}', "got the string '0.15'"),
            ('{"input": null, "output": 0.6}', "got null"),
            ('{"input": [0.15], "output": 0.6}', "got an array"),
            ('{"input": 1' + "0" * 400 + ', "output": 0.6}', "too large to represent"),
            ("[0.15, 0.6]", "must be an object like"),
            ("0.15", "must be an object like"),
        ],
    )
    def test_bad_entry_is_rejected(self, entry, fragment):
        with pytest.raises(ConfigError) as info:
            parse_price_table('{"m": ' + entry + "}")
        assert str(info.value).startswith("PRXREF_PRICE_TABLE: ")
        assert fragment in str(info.value)

    @pytest.mark.parametrize("name", ['""', '"   "'])
    def test_empty_model_name_is_rejected(self, name):
        with pytest.raises(ConfigError, match="every model name must be a non-empty string"):
            parse_price_table("{" + name + ': {"input": 1, "output": 2}}')

    def test_non_string_model_name_in_a_mapping_is_rejected(self):
        with pytest.raises(ConfigError, match="every model name must be a non-empty string, got 5"):
            parse_price_table({5: {"input": 1, "output": 2}})

    def test_duplicate_model_names_are_rejected(self):
        with pytest.raises(ConfigError, match="duplicate key 'm'"):
            parse_price_table('{"m": {"input": 1, "output": 2}, "m": {"input": 3, "output": 4}}')

    def test_names_that_collide_after_stripping_are_duplicates(self):
        with pytest.raises(ConfigError, match="duplicate model name 'm'"):
            parse_price_table('{"m": {"input": 1, "output": 2}, " m": {"input": 3, "output": 4}}')

    def test_a_duplicate_field_inside_an_entry_is_rejected(self):
        with pytest.raises(ConfigError, match="duplicate key 'input'"):
            parse_price_table('{"m": {"input": 1, "input": 2, "output": 3}}')

    def test_the_error_names_the_source_label_it_was_given(self):
        with pytest.raises(ConfigError, match=r"^price_table: not valid JSON"):
            parse_price_table("{bad", source="price_table")

    def test_a_non_string_non_mapping_is_rejected(self):
        with pytest.raises(ConfigError, match="PRXREF_PRICE_TABLE: must be inline JSON or a path"):
            parse_price_table(42)

    def test_zero_prices_are_legal(self):
        table = parse_price_table('{"local/llama": {"input": 0, "output": 0.0}}')
        assert table == {"local/llama": ModelPrice(0.0, 0.0)}

    def test_config_error_is_a_value_error(self):
        with pytest.raises(ValueError):
            parse_price_table("{bad")


class TestEstimate:
    def test_estimate_multiplies_per_million_prices(self):
        assert estimate_usd(TABLE, MINI, 9, 2) == pytest.approx(2.55e-06, rel=1e-12)

    def test_estimate_is_none_on_a_model_miss(self):
        assert estimate_usd(TABLE, "openai/gpt-4o", 9, 2) is None
        assert estimate_usd({}, MINI, 9, 2) is None

    def test_lookup_is_exact_not_prefix(self):
        assert estimate_usd(TABLE, "openai/gpt-4o-mini-2024-07-18", 9, 2) is None
        assert estimate_usd(TABLE, "gpt-4o-mini", 9, 2) is None
        assert estimate_usd(TABLE, "OPENAI/GPT-4O-MINI", 9, 2) is None


class TestValidUsd:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0, 0.0), (0.0, 0.0), (1e-6, 1e-6), ("0.0021", 0.0021), (" 0.5 ", 0.5), (3, 3.0), (-0.0, 0.0)],
    )
    def test_accepts_finite_non_negative_numbers_and_numeric_strings(self, value, expected):
        result = valid_usd(value)
        assert result == expected
        assert isinstance(result, float)
        assert math.copysign(1.0, result) == 1.0

    @pytest.mark.parametrize(
        "value",
        [-1, -1e-9, float("nan"), float("inf"), "nan", "inf", True, False, "abc", None, [], {}, "", "  ", 10**400],
    )
    def test_rejects_everything_else(self, value):
        assert valid_usd(value) is None


class TestCombineReported:
    def test_empty_is_unknown(self):
        assert combine_reported([]) == (None, "")

    def test_every_received_attempt_is_summed_and_the_last_source_kept(self):
        total, source = combine_reported([(0.001, "usage.cost"), (0.002, "x-litellm-response-cost")])
        assert total == pytest.approx(0.003)
        assert source == "x-litellm-response-cost"

    def test_any_attempt_without_a_figure_makes_the_call_unknown(self):
        assert combine_reported([(0.001, "usage.cost"), (None, ""), (0.002, "usage.cost")]) == (None, "")

    def test_a_reported_zero_is_kept(self):
        assert combine_reported([(0.0, "claude-cli")]) == (0.0, "claude-cli")

    def test_accepts_any_iterable(self):
        assert combine_reported(iter([(0.5, "litellm")])) == (0.5, "litellm")


class TestUnitCost:
    def test_a_received_unit_with_a_reported_cost(self):
        assert unit_cost(_unit(cost_usd=0.002, cost_source="usage.cost")) == (True, 0.002, "usage.cost")

    def test_a_unit_that_raised_was_never_received(self):
        assert unit_cost(_raised_unit()) == (False, None, "")

    def test_tokens_alone_mean_received(self):
        assert unit_cost(_unit(model="", input_tokens=0, output_tokens=5))[0] is True

    def test_legacy_unit_without_cost_keys_reads_as_unknown(self):
        assert unit_cost({"model": MINI, "input_tokens": 10, "output_tokens": 2}) == (True, None, "")

    def test_none_tokens_from_an_old_stub_do_not_crash(self):
        assert unit_cost({"model": "", "input_tokens": None, "output_tokens": None}) == (False, None, "")

    def test_an_invalid_cost_reads_as_none_and_drops_the_source(self):
        assert unit_cost(_unit(cost_usd=-1.0, cost_source="usage.cost")) == (True, None, "")


class TestRunCost:
    def test_passthrough_all_units_reported_sums_and_is_not_estimated(self):
        units = [_unit(cost_usd=0.0004, cost_source="usage.cost"), _unit(cost_usd=0.0003, cost_source="usage.cost")]
        assert run_cost(units, None) == (pytest.approx(0.0007), False, [])

    def test_provider_cost_wins_over_a_table_entry(self):
        usd, estimated, unpriced = run_cost([_unit(cost_usd=0.5, cost_source="litellm")], TABLE)
        assert (usd, estimated, unpriced) == (0.5, False, [])

    def test_estimate_fills_a_unit_without_a_reported_cost_and_flags_the_run(self):
        usd, estimated, unpriced = run_cost([_unit(input_tokens=9, output_tokens=2, cost_usd=None)], TABLE)
        assert usd == pytest.approx(2.55e-06)
        assert estimated is True
        assert unpriced == []

    def test_mixed_reported_and_estimated_is_estimated(self):
        units = [_unit(cost_usd=0.001, cost_source="usage.cost"), _unit(input_tokens=1000, output_tokens=0)]
        usd, estimated, _ = run_cost(units, TABLE)
        assert usd == pytest.approx(0.001 + 1000 * 0.15 / 1e6)
        assert estimated is True

    def test_unknown_is_null_never_zero(self):
        usd, estimated, unpriced = run_cost([_unit(model="vendor/unpriced", cost_usd=None)], TABLE)
        assert usd is None
        assert usd != 0
        assert estimated is False
        assert unpriced == ["vendor/unpriced"]

    def test_a_partial_sum_is_never_reported(self):
        units = [_unit(cost_usd=0.001, cost_source="usage.cost") for _ in range(3)] + [_unit(model="x/unpriced")]
        assert run_cost(units, TABLE) == (None, False, ["x/unpriced"])

    def test_an_estimated_unit_next_to_an_unknown_one_is_still_unknown(self):
        assert run_cost([_unit(), _unit(model="x/unpriced")], TABLE) == (None, False, ["x/unpriced"])

    def test_a_unit_without_usage_is_not_estimated_to_zero(self):
        assert run_cost([_unit(input_tokens=0, output_tokens=0)], TABLE) == (None, False, [MINI])

    def test_units_that_raised_are_skipped(self):
        units = [_raised_unit(), _unit(cost_usd=0.002, cost_source="usage.cost")]
        assert run_cost(units, None) == (0.002, False, [])

    def test_no_completion_received_is_null(self):
        assert run_cost([_raised_unit(), _raised_unit()], TABLE) == (None, False, [])
        assert run_cost([], TABLE) == (None, False, [])

    def test_reported_zero_is_a_real_zero(self):
        usd, estimated, unpriced = run_cost([_unit(cost_usd=0.0, cost_source="usage.cost")], None)
        assert usd == 0.0
        assert usd is not None
        assert (estimated, unpriced) == (False, [])

    def test_legacy_unit_without_cost_keys_counts_as_unknown(self):
        legacy = {"findings": [], "error": None, "model": MINI, "input_tokens": 10, "output_tokens": 2}
        assert run_cost([legacy], None) == (None, False, [MINI])

    def test_a_received_unit_without_a_model_is_named_as_unknown_model(self):
        assert run_cost([_unit(model="", input_tokens=5)], TABLE) == (None, False, ["<unknown model>"])

    def test_unpriced_models_are_sorted_and_unique(self):
        units = [_unit(model="b/m"), _unit(model="a/m"), _unit(model="b/m")]
        assert run_cost(units, {})[2] == ["a/m", "b/m"]

    def test_the_total_is_rounded_to_remove_float_noise(self):
        assert 9 * 0.15 / 1e6 + 2 * 0.60 / 1e6 != 2.55e-06
        usd, _, _ = run_cost([_unit(input_tokens=9, output_tokens=2)], TABLE)
        assert usd == 2.55e-06

    def test_accepts_a_generator_of_units(self):
        usd, _, _ = run_cost((u for u in [_unit(cost_usd=0.25, cost_source="litellm")]), None)
        assert usd == 0.25


class TestFormatUsd:
    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (0, "$0.00"),
            (0.0, "$0.00"),
            (0.00000001, "<$0.0001"),
            (0.0000999, "<$0.0001"),
            (0.0001, "$0.0001"),
            (0.00070635, "$0.0007"),
            (0.0123, "$0.0123"),
            (0.99994, "$0.9999"),
            (0.99996, "$1.00"),
            (1, "$1.00"),
            (1.234, "$1.23"),
            (12.5, "$12.50"),
        ],
    )
    def test_format_usd_cases(self, value, text):
        assert format_usd(value) == text

    @pytest.mark.parametrize("value", [1e-12, 1e-9, 4e-5, 5e-5, 9.9999e-5, 0.00004999])
    def test_nonzero_cost_never_renders_as_zero(self, value):
        assert format_usd(value) != "$0.00"
        assert format_usd(value) != "$0.0000"

    @pytest.mark.parametrize("value", [-0.01, float("nan"), float("inf"), None, "0.1", True, 10**400])
    def test_format_usd_rejects_what_is_not_a_dollar_amount(self, value):
        with pytest.raises(ValueError, match="not a dollar amount"):
            format_usd(value)


class TestCostLabel:
    def test_unknown(self):
        assert cost_label(None, False) == "cost unknown"
        assert cost_label(None, True) == "cost unknown"

    def test_reported(self):
        assert cost_label(0.0007, False) == "$0.0007"

    def test_estimated(self):
        assert cost_label(0.0007, True) == "~$0.0007 (est.)"

    def test_a_known_zero_is_zero_not_unknown(self):
        assert cost_label(0.0, False) == "$0.00"

    @pytest.mark.parametrize("value", [float("nan"), -1.0, True])
    def test_an_unusable_figure_is_unknown_not_a_crash(self, value):
        assert cost_label(value, False) == "cost unknown"


class TestModuleIsALeaf:
    def test_imports_only_the_standard_library_and_the_config_error(self):
        tree = ast.parse(Path(costs.__file__).read_text(encoding="utf-8"))
        stdlib: set[str] = set()
        package: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                stdlib.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    package.add(f"{node.module}:{','.join(a.name for a in node.names)}")
                else:
                    stdlib.add((node.module or "").split(".")[0])
        assert stdlib <= set(sys.stdlib_module_names) | {"__future__"}, stdlib
        assert package == {"llm:ConfigError"}
