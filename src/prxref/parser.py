"""Lenient JSON recovery for LLM responses.

LLMs routinely emit fences, preamble prose, trailing commas, or inner
triple-backticks in string fields; this module recovers valid JSON from
every known failure mode without dropping the model's response.
"""
from __future__ import annotations

import json
import re
from typing import Any


def strip_json_fences(raw_response: str) -> str:
    """Return the JSON body with surrounding code fences stripped.

    If no ``` fence is present, returns the stripped input.
    """
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw_response, re.DOTALL)
    return match.group(1).strip() if match else raw_response.strip()


def _largest_balanced_span(s: str) -> str | None:
    """Find the longest substring that is a balanced ``{...}`` or ``[...]``.

    String-aware: skips braces/brackets inside quoted strings (respecting
    escapes). Catches cases where a greedy or non-greedy fence regex closes
    early on an inner triple-backtick inside a string value, or where the
    model emits an empty fence preamble before the real JSON.
    """
    longest: str | None = None
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        for i, ch in enumerate(s):
            if ch != open_ch:
                continue
            depth = 0
            in_str = False
            escape = False
            for j in range(i, len(s)):
                c = s[j]
                if in_str:
                    if escape:
                        escape = False
                    elif c == "\\":
                        escape = True
                    elif c == '"':
                        in_str = False
                else:
                    if c == '"':
                        in_str = True
                    elif c == open_ch:
                        depth += 1
                    elif c == close_ch:
                        depth -= 1
                        if depth == 0:
                            span = s[i : j + 1]
                            if longest is None or len(span) > len(longest):
                                longest = span
                            break
    return longest


_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _strip_trailing_commas(s: str) -> str:
    """Remove trailing commas immediately preceding closing braces or brackets."""
    return _TRAILING_COMMA_RE.sub(r"\1", s)


def loads_lenient(raw_response: str) -> Any:
    """Parse JSON from arbitrary LLM output across four fallback strategies:

    1. Raw input (after ``.strip()``)
    2. ``strip_json_fences`` output
    3. Largest brace/bracket-balanced substring (string-aware)
    4. Trailing-comma repair across candidates 1-3

    Raises ``json.JSONDecodeError`` when no strategy yields valid JSON.
    """
    candidates: list[str] = []

    s = raw_response.strip()
    if s:
        candidates.append(s)

    fenced = strip_json_fences(raw_response)
    if fenced and fenced not in candidates:
        candidates.append(fenced)

    balanced = _largest_balanced_span(raw_response)
    if balanced and balanced not in candidates:
        candidates.append(balanced)

    # Strategy 4: trailing-comma repaired variants of all candidates
    for c in list(candidates):
        repaired = _strip_trailing_commas(c)
        if repaired and repaired not in candidates:
            candidates.append(repaired)

    last_err: json.JSONDecodeError | None = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            last_err = e

    if last_err is None:
        raise json.JSONDecodeError("no parseable content", raw_response or "", 0)
    raise last_err
