"""Tests for prxref.parser: lenient JSON recovery from LLM outputs."""
import json

import pytest

from prxref.parser import loads_lenient, strip_json_fences


def test_strip_json_fences_fenced_with_lang():
    raw = '```json\n{"verdict": "confirm"}\n```'
    assert strip_json_fences(raw) == '{"verdict": "confirm"}'


def test_strip_json_fences_fenced_without_lang():
    raw = '```\n{"a": 1}\n```'
    assert strip_json_fences(raw) == '{"a": 1}'


def test_strip_json_fences_unfenced_returns_stripped():
    assert strip_json_fences('  {"a": 1}  ') == '{"a": 1}'


def test_strip_json_fences_preamble_then_fence():
    raw = "Here's the JSON:\n```json\n{\"x\": 2}\n```\nThanks!"
    assert strip_json_fences(raw) == '{"x": 2}'


def test_loads_lenient_plain_json():
    assert loads_lenient('{"verdict": "confirm"}') == {"verdict": "confirm"}


def test_loads_lenient_fenced_json():
    assert loads_lenient('```json\n{"a": 1}\n```') == {"a": 1}


def test_loads_lenient_preamble_then_fence():
    raw = "Here's the analysis:\n```json\n{\"x\": 2}\n```\nHope that helps!"
    assert loads_lenient(raw) == {"x": 2}


def test_loads_lenient_json_string_contains_inner_backticks():
    raw = (
        "```json\n"
        '{"verdict": "confirm", "reason": "see ```js\\nfoo()\\n```"}\n'
        "```"
    )
    result = loads_lenient(raw)
    assert result["verdict"] == "confirm"
    assert "foo()" in result["reason"]


def test_loads_lenient_empty_preamble_fence_then_unfenced_json():
    raw = '```\n\n```\n{"verdict": "dismiss", "confidence": 0.5}'
    assert loads_lenient(raw) == {"verdict": "dismiss", "confidence": 0.5}


def test_loads_lenient_array_via_brace_balance():
    raw = "Here you go: [1, 2, 3] — done."
    assert loads_lenient(raw) == [1, 2, 3]


def test_loads_lenient_trailing_comma_in_object():
    raw = '{"verdict": "confirm", "confidence": 0.9,}'
    assert loads_lenient(raw) == {"verdict": "confirm", "confidence": 0.9}


def test_loads_lenient_trailing_comma_in_array():
    raw = '[1, 2, 3,]'
    assert loads_lenient(raw) == [1, 2, 3]


def test_loads_lenient_trailing_comma_nested():
    raw = '{"items": [{"a": 1,}, {"b": 2,},],}'
    assert loads_lenient(raw) == {"items": [{"a": 1}, {"b": 2}]}


def test_loads_lenient_fenced_with_trailing_comma():
    raw = "```json\n{\n  \"a\": 1,\n  \"b\": 2,\n}\n```"
    assert loads_lenient(raw) == {"a": 1, "b": 2}


def test_loads_lenient_garbage_raises():
    with pytest.raises(json.JSONDecodeError):
        loads_lenient("nothing parseable here, no braces or brackets")
