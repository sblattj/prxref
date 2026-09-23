"""Dollar cost of a review run: reported, estimated, or unknown.

A run's cost is always in exactly one of three states, and they are never
blended into a number that looks more certain than it is:

- **Reported.** The backend returned a dollar figure for the call:
  OpenRouter's body ``usage.cost``, a LiteLLM gateway's (or llm-ferry's)
  ``x-litellm-response-cost`` header, the litellm SDK's ``response_cost``, or
  claude-cli's ``total_cost_usd``. A reported figure always wins.
- **Estimated.** No figure came back, but ``PRXREF_PRICE_TABLE`` prices the
  exact model name the call reported. The run is flagged ``cost_estimated``.
- **Unknown.** Neither. The run's ``cost_usd`` is ``None``: never ``0`` and
  never a partial sum of the units that were priced.

Backends only report (:func:`valid_usd`, :func:`combine_reported`); nothing
below the orchestrator ever sees the price table. Estimation happens once,
over the finished run, in :func:`run_cost`. A run that made no LLM request at
all never calls it: its cost is a known ``0.0``.
"""
from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from .llm import ConfigError

_TOKENS_PER_PRICE_UNIT = 1_000_000
_PRICE_FIELDS = ("input", "output")
_TABLE_SHAPE = 'a JSON object mapping model name to {"input": USD, "output": USD} per million tokens'
_ENTRY_EXAMPLE = '{"input": 0.15, "output": 0.60}'
_UNKNOWN_MODEL = "<unknown model>"
_SMALLEST_SHOWN = 0.0001


class ModelPrice(NamedTuple):
    """List price of one model in USD per MILLION tokens."""

    input: float
    output: float


class _DuplicateKeyError(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def parse_price_table(
    raw: str | Mapping[str, Any] | None, source: str = "PRXREF_PRICE_TABLE"
) -> dict[str, ModelPrice]:
    """Parse and strictly validate a price table.

    ``raw`` is one of:

    - ``None``, ``""`` or whitespace: no table, so ``{}``.
    - a string whose first non-space character is ``{``: inline JSON.
    - any other string: a path to a JSON file (``~`` expanded, read as UTF-8).
    - a mapping: an already-decoded table from a library caller, validated
      exactly like decoded JSON.

    The table maps a model name to ``{"input": n, "output": n}``, each ``n`` a
    finite number >= 0 in USD per million tokens. Model names are stripped
    and must be non-empty and unique. Invalid JSON, an unreadable file, a
    non-object at any level, a missing or unknown field (``"ouput"``), a
    duplicate key, a bool, a numeric string, ``NaN``, ``Infinity`` or a
    negative price each raise ``ConfigError`` whose message starts with
    ``source``, so the CLI names the env var or flag that supplied it. Zero
    prices are legal, for local or free models. Returns a new dict.
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        data = _load_json(text, source, "") if text.startswith("{") else _load_json_file(text, source)
    elif isinstance(raw, Mapping):
        data = raw
    else:
        raise ConfigError(f"{source}: must be inline JSON or a path to a JSON file, got {type(raw).__name__}")
    return _validate_table(data, source)


def estimate_usd(
    table: Mapping[str, ModelPrice], model: str, input_tokens: int, output_tokens: int
) -> float | None:
    """Price one call from ``table``, or ``None`` when the model has no entry.

    The lookup is on the exact model name, never a prefix or a pattern:
    a wrong estimate is worse than an unknown one.
    """
    price = table.get(model) if table and isinstance(model, str) else None
    if price is None:
        return None
    return (
        _count(input_tokens) * price.input / _TOKENS_PER_PRICE_UNIT
        + _count(output_tokens) * price.output / _TOKENS_PER_PRICE_UNIT
    )


def valid_usd(value: object) -> float | None:
    """Return ``value`` as a reported dollar amount, or ``None`` if it is not one.

    Accepted: a finite real number >= 0 (not a bool), or a string that
    parses to one, because response headers are strings. Everything else,
    including ``NaN``, ``inf``, negatives and ``""``, is ``None``: an
    unusable figure is no figure.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    elif isinstance(value, int | float):
        try:
            number = float(value)
        except OverflowError:
            return None
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return 0.0 if number == 0 else number


def combine_reported(parts: Sequence[tuple[float | None, str]]) -> tuple[float | None, str]:
    """Fold the reported costs of every attempt received inside ONE invoke.

    Each part is ``(cost_usd, cost_source)``. Every attempt that came back
    was billed, including truncated ones a fallback chain moved past, so the
    figures are summed and the last part's source is kept. No parts, or any
    part without a figure, gives ``(None, "")``: one unpriced attempt makes
    the whole call's cost unknown.
    """
    parts = list(parts)
    if not parts:
        return None, ""
    costs: list[float] = []
    for cost, _source in parts:
        if cost is None:
            return None, ""
        costs.append(cost)
    return math.fsum(costs), parts[-1][1]


def unit_cost(unit: Mapping[str, Any]) -> tuple[bool, float | None, str]:
    """Read ``(received, cost_usd, cost_source)`` off one review unit.

    ``unit`` is an orchestrator worker or sweep result, or a reviewer meta
    dict. ``received`` is true when a completion came back: the unit names a
    model or counts any tokens. A unit whose request raised has model ``""``
    and zero tokens, so it was never received. The cost goes through
    :func:`valid_usd`; a unit without cost keys (an older stub) reads as
    ``(…, None, "")``. The source is ``""`` whenever the cost is ``None``.
    """
    received = (
        bool(unit.get("model"))
        or _count(unit.get("input_tokens")) > 0
        or _count(unit.get("output_tokens")) > 0
    )
    cost = valid_usd(unit.get("cost_usd"))
    source = str(unit.get("cost_source") or "") if cost is not None else ""
    return received, cost, source


def run_cost(
    units: Iterable[Mapping[str, Any]], table: Mapping[str, ModelPrice] | None
) -> tuple[float | None, bool, list[str]]:
    """Total a run's cost: ``(cost_usd, cost_estimated, unpriced_models)``.

    ``units`` are the orchestrator's chunk results plus the sweep; ``table``
    is the parsed ``PRXREF_PRICE_TABLE`` (``None`` or ``{}`` estimates
    nothing). Rules:

    1. A unit that was never received is skipped: its request raised, so no
       completion came back to read a cost from.
    2. A received unit's reported cost is used whenever there is one, even if
       the table also prices its model.
    3. Otherwise the table prices it, if the model has an exact entry and the
       unit counted input tokens (every prompt has a system prompt, so zero
       input tokens means the backend reported no usage and an estimate would
       be a fake ``0``). Such a unit makes the run ``cost_estimated``.
    4. Otherwise the unit is unknown, and its model is named in
       ``unpriced_models``.

    No unit received gives ``(None, False, [])``: requests went out and
    nothing came back to price. Any unknown unit gives ``(None, False,
    sorted_models)``: unknown is ``None``, never ``0`` and never a partial
    sum. Otherwise the total is rounded to 10 decimals, which removes float
    noise and nothing else.

    Boundaries: tokens and cost cover the same units, so a unit that failed
    after its response arrived (a parse failure, a truncation) was billed and
    is counted. A request abandoned at the deadline returned nothing and adds
    nothing, so a provider that bills abandoned generations may charge more
    than this reports. An estimate prices every input token at the list rate,
    so it ignores prompt-cache discounts a provider's own figure reflects.
    """
    parts: list[float] = []
    unpriced: set[str] = set()
    estimated = False
    received_any = False
    for unit in units:
        received, reported, _source = unit_cost(unit)
        if not received:
            continue
        received_any = True
        if reported is not None:
            parts.append(reported)
            continue
        model = unit.get("model")
        model = model if isinstance(model, str) else ""
        input_tokens = _count(unit.get("input_tokens"))
        estimate = None
        if table and model and input_tokens > 0:
            estimate = estimate_usd(table, model, input_tokens, _count(unit.get("output_tokens")))
        if estimate is None:
            unpriced.add(model or _UNKNOWN_MODEL)
            continue
        parts.append(estimate)
        estimated = True
    if not received_any:
        return None, False, []
    if unpriced:
        return None, False, sorted(unpriced)
    return round(math.fsum(parts), 10), estimated, []


def format_usd(value: float) -> str:
    """Render a dollar amount for people.

    ``0`` is ``$0.00``; anything above zero but below $0.0001 is
    ``<$0.0001``; below $1 shows four decimals (``$0.0007``); $1 and up shows
    two (``$1.23``). A nonzero cost never renders as ``$0.00``. A value that
    is not a finite number >= 0 raises ``ValueError``.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"not a dollar amount: {value!r}")
    try:
        number = float(value)
    except OverflowError as e:
        raise ValueError(f"not a dollar amount: {value!r}") from e
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"not a dollar amount: {value!r}")
    if number == 0:
        return "$0.00"
    if number < _SMALLEST_SHOWN:
        return "<$0.0001"
    if number < 1:
        four = f"{number:.4f}"
        if four != "1.0000":
            return f"${four}"
    return f"${number:.2f}"


def cost_label(cost_usd: float | None, estimated: bool) -> str:
    """Return the label a run's cost is shown with.

    ``"cost unknown"`` when there is no usable figure; ``"~$0.0007 (est.)"``
    when it was estimated; ``"$0.0007"`` when it was reported (or a known
    zero). The same label goes on the attribution line and the CLI.
    """
    value = valid_usd(cost_usd)
    if value is None:
        return "cost unknown"
    text = format_usd(value)
    return f"~{text} (est.)" if estimated else text


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def _load_json(text: str, source: str, where: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{source}: not valid JSON ({e.msg} at line {e.lineno} column {e.colno}){where}") from e
    except _DuplicateKeyError as e:
        raise ConfigError(
            f"{source}: duplicate key {e.key!r}{where}; each model name, and each field of an entry, may appear once"
        ) from e


def _load_json_file(text: str, source: str) -> Any:
    path = Path(text).expanduser()
    try:
        data = path.read_bytes()
    except OSError as e:
        raise ConfigError(
            f"{source}: cannot read price table file '{path}': {e.strerror or e} (inline JSON must start with '{{')"
        ) from e
    try:
        body = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ConfigError(f"{source}: price table file '{path}' is not valid UTF-8") from e
    return _load_json(body, source, f" in '{path}'")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _validate_table(data: object, source: str) -> dict[str, ModelPrice]:
    if not isinstance(data, Mapping):
        raise ConfigError(f"{source}: must be {_TABLE_SHAPE}, got {_json_kind(data)}")
    table: dict[str, ModelPrice] = {}
    for name, entry in data.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{source}: every model name must be a non-empty string, got {name!r}")
        model = name.strip()
        if model in table:
            raise ConfigError(f"{source}: duplicate model name {model!r}")
        table[model] = _validate_entry(model, entry, source)
    return table


def _validate_entry(model: str, entry: object, source: str) -> ModelPrice:
    if not isinstance(entry, Mapping):
        raise ConfigError(f"{source}: the entry for {model!r} must be an object like {_ENTRY_EXAMPLE}, "
                          f"got {_json_kind(entry)}")
    unknown = sorted(repr(key) for key in entry if key not in _PRICE_FIELDS)
    if unknown:
        raise ConfigError(f"{source}: the entry for {model!r} has unknown key(s) {', '.join(unknown)}; "
                          "only 'input' and 'output' are allowed")
    missing = [f"'{field}'" for field in _PRICE_FIELDS if field not in entry]
    if missing:
        raise ConfigError(f"{source}: the entry for {model!r} is missing {' and '.join(missing)}")
    return ModelPrice(*(_price(model, field, entry[field], source) for field in _PRICE_FIELDS))


def _price(model: str, field: str, value: object, source: str) -> float:
    problem = f"{source}: {model!r} {field} must be a finite number >= 0 (USD per million tokens), got "
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(problem + _json_kind(value))
    try:
        number = float(value)
    except OverflowError as e:
        raise ConfigError(problem + "a number too large to represent") from e
    if not math.isfinite(number) or number < 0:
        raise ConfigError(problem + repr(value))
    return number


def _json_kind(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"the boolean {str(value).lower()}"
    if isinstance(value, int | float):
        return f"the number {value!r}"
    if isinstance(value, str):
        return f"the string {value!r}"
    if isinstance(value, Mapping):
        return "an object"
    if isinstance(value, list | tuple):
        return "an array"
    return type(value).__name__
