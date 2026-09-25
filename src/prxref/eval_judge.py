"""The judge side of ``prxref eval score``: client, one call per case, cache, cost, stamp.

:mod:`prxref.judge` holds the pure half of a judge grading (prompt, parser,
per-ref cap, cache key). This module runs it against a live client:

1. :func:`build_judge_client` builds the judge from the review's own config
   with only ``llm_models`` replaced, through the same
   ``prxref.llm_backends.create_llm_client`` factory the CLI looks up at call
   time. The judge therefore shares the review's backend, base URL and key
   path, and one test stub keyed on ``cfg["llm_models"]`` serves both.
2. :func:`judge_case` grades one case with one single-shot ``json_mode``
   call, sent again unchanged up to ``parse_retries`` times while
   :func:`prxref.judge.parse_judge_response` rejects the reply (#21). A
   cached grading makes no call. A response still rejected when the retries
   run out, and any exception the client raises, make the case a judge
   error (:attr:`JudgeOutcome.error`), never a score of 0.
3. The cache is one JSON file per :func:`prxref.judge.judge_cache_key` under
   a directory the caller chooses; :func:`judge_cache_dir` names the
   conventional one, ``<out>/<label>/judge-cache/``. An entry holds the raw
   response, which a hit re-parses under the current code rules. Entries are
   written atomically (a temp file, then :func:`os.replace`). An entry that
   cannot be read, is not the expected shape, or no longer parses is a miss,
   logged as a WARNING, and the fresh grading overwrites it. A failed grading
   is never cached.
4. A live call's cost goes through :func:`prxref.costs.run_cost` with the
   ``price_table`` of :func:`prxref.config.load_config`: a reported figure
   wins, a table entry estimates, anything else is ``None`` and never ``0``.
   :func:`judge_cost` totals a scoring run the same way.
5. :func:`check_self_judging` warns when the judge model is one of the
   reviewer's ``sampling.models``; :func:`judge_stamp` records that flag with
   the judge's ``sampling``, :data:`~prxref.judge.JUDGE_PROMPT_VERSION` and
   the template sha256 for ``score.json``. Self-judging never stops a run.

This module never imports :mod:`prxref.cli`.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import costs
from .judge import (
    AI_REF_PREFIX,
    JUDGE_PROMPT_VERSION,
    Grade,
    JudgeParseError,
    ai_ref_files,
    assign_refs,
    build_judge_prompt,
    human_files,
    judge_cache_key,
    judge_prompt_sha,
    parse_judge_response,
    split_judge_prompt,
)
from .llm import ConfigError, LLMClient
from .orchestrator import _sampling
from .reviewer import _fold_retry_usage, _write_trace_files

logger = logging.getLogger("prxref")

JUDGE_MODEL_FLAG = "--judge-model"
JUDGE_CACHE_DIRNAME = "judge-cache"
JUDGE_CACHE_VERSION = 1
JUDGE_TRACE_LABEL = "judge"

_LLM_MODELS_KEY = "llm_models"


@dataclass(frozen=True)
class JudgeOutcome:
    """The result of judging one case, whether graded, cached or failed.

    ``grades`` holds one :class:`~prxref.judge.Grade` per human finding in
    label order, or is ``None`` exactly when ``error`` is set: a judge error
    carries no grades, so it can never be read as a score of 0. ``error`` is
    ``None`` on success, otherwise a one-line reason. ``cached`` is true when
    the grades came from the cache, and ``llm_calls`` is the number of judge
    requests this call made (0 for a cache hit or a case with no labels,
    otherwise 1 plus ``parse_retries``).

    ``ref_index`` maps each judge ref (``A1``, ``A2``, ...) to the index of
    its row in the run record's ``findings`` list as written, dropped rows
    included, which is how :mod:`prxref.eval_metrics` addresses an AI
    finding.

    ``model``, ``input_tokens``, ``output_tokens`` and ``elapsed_ms`` describe
    the live calls: tokens and time cover every request, and ``model`` is the
    name the backend reported for the last one (a cache hit keeps the stored
    one, with zero tokens and time). ``cost_usd`` is the cost of every
    request: ``0.0`` when no request was made, ``None`` when it is unknown
    (never ``0``), and ``cost_estimated`` is true when the price table
    supplied it. ``cost_source`` is the backend's reported source, ``""``
    unless a reported figure was used. ``unit`` is the cost unit
    :func:`judge_cost` totals, with every request's usage folded in, or
    ``None`` when no request was made.

    ``parse_retries`` is the number of times :func:`judge_case` sent the same
    request again because :func:`prxref.judge.parse_judge_response` rejected
    the reply (#21): 0 for a cache hit, a case with no labels, a first reply
    that parsed, and any call made with ``parse_retries=0``.
    """

    case_id: str
    cache_key: str
    grades: tuple[Grade, ...] | None
    error: str | None
    cached: bool
    llm_calls: int
    ref_index: dict[str, int]
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_ms: int = 0
    cost_usd: float | None = 0.0
    cost_estimated: bool = False
    cost_source: str = ""
    unit: dict[str, Any] | None = None
    parse_retries: int = 0

    @property
    def ok(self) -> bool:
        """Whether the case was graded; false for a judge error."""
        return self.error is None


def build_judge_client(cfg: Mapping[str, Any], judge_model: str) -> LLMClient:
    """Build the judge client from the review's ``cfg`` with only ``llm_models`` changed.

    ``cfg`` is the :func:`prxref.config.load_config` dict the review used. The
    judge gets ``{**cfg, "llm_models": [judge_model]}``: the same backend,
    base URL, key path, timeout, temperature and seed, and a one-model chain.
    An uppercase ``LLM_MODELS`` override a library caller may pass is removed
    first, because the factory reads it before the lowercase key. ``cfg`` is
    never mutated. The factory is looked up as
    ``prxref.llm_backends.create_llm_client`` at call time, as the CLI does,
    so a monkeypatched factory builds the judge too.

    ``judge_model`` is stripped. An empty one, which the factory would
    silently replace with ``PRXREF_LLM_MODELS`` (the reviewer's chain), and
    one holding a comma, which it would split into a chain, raise
    :class:`~prxref.llm.ConfigError` naming :data:`JUDGE_MODEL_FLAG`. Any
    other configuration problem is the factory's own ``ConfigError``.
    """
    model = judge_model.strip() if isinstance(judge_model, str) else ""
    if not model:
        raise ConfigError(f"{JUDGE_MODEL_FLAG}: must name one model, got {judge_model!r}")
    if "," in model:
        raise ConfigError(f"{JUDGE_MODEL_FLAG}: must name one model, not a comma-separated chain, got {model!r}")
    judge_cfg = {key: value for key, value in cfg.items() if key != _LLM_MODELS_KEY.upper()}
    judge_cfg[_LLM_MODELS_KEY] = [model]
    return importlib.import_module("prxref.llm_backends").create_llm_client(judge_cfg)


def check_self_judging(judge_model: str, reviewer_sampling: Mapping[str, Any] | None) -> bool:
    """Whether ``judge_model`` is one of the reviewer's models; warn when it is.

    ``reviewer_sampling`` is a run record's ``sampling`` dict; its ``models``
    list is the reviewer's chain. Names are compared stripped and casefolded.
    A match logs one WARNING and returns true, for :func:`judge_stamp`'s
    ``self_judged``; it never raises, because self-judging is reported, not
    refused. A missing ``sampling`` or ``models`` is no match.
    """
    wanted = judge_model.strip().casefold() if isinstance(judge_model, str) else ""
    models = reviewer_sampling.get("models") if isinstance(reviewer_sampling, Mapping) else None
    names = [m for m in models if isinstance(m, str)] if isinstance(models, list | tuple) else []
    if not wanted or wanted not in {name.strip().casefold() for name in names}:
        return False
    logger.warning(
        "judge model %r is also a reviewer model (sampling.models %s): the judge is grading its "
        "own findings, so the scores may be inflated; score.json stamps self_judged",
        judge_model, names,
    )
    return True


def judge_stamp(client: object, judge_model: str, *, self_judged: bool) -> dict[str, Any]:
    """The judge stamp ``score.json`` records.

    Keys: ``model`` (the judge model as requested), ``sampling`` (the
    client's ``temperature`` / ``seed`` / ``models``, the same shape a run
    record's ``sampling`` has), ``prompt_version``
    (:data:`~prxref.judge.JUDGE_PROMPT_VERSION`), ``prompt_sha256`` (of the
    packaged judge template) and ``self_judged``
    (:func:`check_self_judging`).
    """
    return {
        "model": judge_model.strip() if isinstance(judge_model, str) else judge_model,
        "sampling": _sampling(client),
        "prompt_version": JUDGE_PROMPT_VERSION,
        "prompt_sha256": judge_prompt_sha(),
        "self_judged": bool(self_judged),
    }


def judge_cache_dir(out_dir: str | os.PathLike[str], label: str) -> Path:
    """The conventional judge cache directory of one label: ``<out>/<label>/judge-cache``."""
    return Path(out_dir) / label / JUDGE_CACHE_DIRNAME


def judge_case(
    client: LLMClient,
    judge_model: str,
    case: Any,
    record: Mapping[str, Any],
    *,
    cache_dir: str | os.PathLike[str] | None = None,
    price_table: Mapping[str, Any] | None = None,
    max_tokens: int = 4096,
    timeout_s: float | None = None,
    trace_dir: str = "",
    parse_retries: int = 0,
) -> JudgeOutcome:
    """Grade one case's human findings against its run record's AI findings.

    ``case`` is an :class:`~prxref.eval_cases.EvalCase` (or anything with an
    ``id`` and ``expected`` labels); ``record`` is its ``record.json`` dict,
    whose ``findings`` rows are numbered by :func:`prxref.judge.assign_refs`.
    ``judge_model`` is the requested judge model, stripped, and part of the
    cache key.

    - A case with no labels has nothing to grade: empty grades, no call.
    - With ``cache_dir`` set, a valid entry for this case's cache key is used
      without a call; a corrupt one is a miss with a WARNING.
    - Otherwise ``client.invoke(system, user, json_mode=True)`` is called
      with ``max_tokens`` and ``timeout_s``. A response
      :func:`prxref.judge.parse_judge_response` rejects, for any reason, is
      asked for again with the same request while fewer than
      ``parse_retries`` (N, default 0) retries have run, so a case makes at
      most ``1 + N`` calls; each retry logs one WARNING ending ``parse retry
      <k> of <N>``. An exception the client raises is never retried. A
      response still rejected when the retries run out, and any exception,
      is a judge error with a WARNING, whose reason is the last call's;
      neither is cached. Only the graded response is written to the cache,
      atomically; a failed write is a WARNING and keeps the grades.
    - Every call counts. Its tokens, time and reported cost are folded into
      the outcome's ``unit`` (through
      :func:`prxref.reviewer._fold_retry_usage`) before
      :func:`prxref.costs.run_cost` prices it, ``model`` is the last call's,
      ``llm_calls`` is ``1`` plus the retries made, and
      :attr:`JudgeOutcome.parse_retries` counts them. At N=0 nothing is
      retried, exactly as in 0.16.0.
    - ``price_table`` is the parsed ``PRXREF_PRICE_TABLE``; ``None`` or
      ``{}`` estimates nothing.
    - A non-empty ``trace_dir`` receives the prompt, the last response and
      the meta as ``judge.*`` files, as a review unit's trace does. At N of 1
      or more, each rejected response that was retried is kept as
      ``judge.attempt<K>.response.json`` (K from 1), and once a retry ran the
      meta adds ``parse_retries`` and ``first_error``, the reason the first
      response was rejected.

    A label without an id, or a repeated id, raises ``ValueError`` from
    :func:`prxref.judge.human_files`; the case loader refuses both first.
    """
    case_id = _case_id(case)
    judge_model = judge_model.strip()
    findings = list(record.get("findings") or ())
    ai = assign_refs(findings)
    humans = human_files(case)
    prompt_sha = judge_prompt_sha()
    key = judge_cache_key(prompt_sha, judge_model, case, ai)
    base = {"case_id": case_id, "cache_key": key, "ref_index": _ref_index(findings)}
    if not humans:
        return JudgeOutcome(**base, grades=(), error=None, cached=False, llm_calls=0)
    entry_path = Path(cache_dir) / f"{key}.json" if cache_dir is not None else None
    if entry_path is not None:
        hit = _cached_grades(entry_path, key, judge_model, prompt_sha, humans, ai)
        if hit is not None:
            grades, stored_model = hit
            return JudgeOutcome(**base, grades=grades, error=None, cached=True, llm_calls=0, model=stored_model)
    system, user = split_judge_prompt(build_judge_prompt(case, ai))
    t0 = time.perf_counter()
    unit: dict[str, Any] = {"model": "", "input_tokens": 0, "output_tokens": 0, "cost_usd": None, "cost_source": ""}
    attempts: list[str | None] | None = [] if parse_retries >= 1 else None
    retries = 0
    first_error = ""
    while True:
        try:
            result = client.invoke(system, user, max_tokens=max_tokens, json_mode=True, timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001 - a failed judge call is a judge error, never a failed run
            elapsed = _elapsed_ms(t0)
            reason = f"judge call failed: {type(exc).__name__}: {exc}"
            logger.warning("judge error for case %r: %s", case_id, reason)
            trace_meta = {**_trace_meta(unit, elapsed, retries, first_error), "error": reason}
            _write_trace_files(trace_dir, JUDGE_TRACE_LABEL, system, user, None, trace_meta, attempts=attempts)
            return JudgeOutcome(**_live(base, unit, elapsed, retries, price_table), grades=None, error=reason)
        elapsed = _elapsed_ms(t0)
        if retries:
            _fold_retry_usage(unit, result)
        else:
            unit = {
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": result.cost_usd,
                "cost_source": result.cost_source,
            }
        try:
            grades = tuple(parse_judge_response(result.text, humans, ai_ref_files(ai)))
        except JudgeParseError as exc:
            reason = f"judge response rejected: {exc}"
            if attempts is not None and retries < parse_retries:
                retries += 1
                attempts.append(result.text)
                first_error = first_error or reason
                logger.warning(
                    "judge for case %r: unusable reply (%s); parse retry %d of %d",
                    case_id, exc, retries, parse_retries,
                )
                continue
            logger.warning("judge error for case %r: %s", case_id, reason)
            trace_meta = {**_trace_meta(unit, elapsed, retries, first_error), "error": reason}
            _write_trace_files(trace_dir, JUDGE_TRACE_LABEL, system, user, result.text, trace_meta, attempts=attempts)
            return JudgeOutcome(**_live(base, unit, elapsed, retries, price_table), grades=None, error=reason)
        break
    live = _live(base, unit, elapsed, retries, price_table)
    trace_meta = _trace_meta(unit, elapsed, retries, first_error)
    _write_trace_files(trace_dir, JUDGE_TRACE_LABEL, system, user, result.text, trace_meta, attempts=attempts)
    if entry_path is not None:
        _write_entry(entry_path, {
            "version": JUDGE_CACHE_VERSION,
            "key": key,
            "model": judge_model,
            "prompt_version": JUDGE_PROMPT_VERSION,
            "prompt_sha256": prompt_sha,
            "case_id": case_id,
            "response_model": result.model,
            "response": result.text,
        })
    return JudgeOutcome(**live, grades=grades, error=None)


def judge_cost(outcomes: Iterable[JudgeOutcome], price_table: Mapping[str, Any] | None) -> tuple[float | None, bool]:
    """Total a scoring run's judge cost: ``(cost_usd, cost_estimated)``.

    Totals the :attr:`JudgeOutcome.unit` of every outcome that made a request
    with :func:`prxref.costs.run_cost`, the rule a review's cost follows. A
    case's unit sums every attempt it made, parse retries included, so a case
    is skipped only when none of its calls received a reply; a case whose
    retry raised after a rejected reply still counts that reply. Any received
    case without a reported or estimated figure makes the whole total
    ``None``, never ``0`` and never a partial sum. A run that made no request
    (every case cached or unlabelled) costs a known ``0.0``.
    """
    units = [outcome.unit for outcome in outcomes if outcome.unit is not None]
    if not units:
        return 0.0, False
    cost_usd, estimated, _unpriced = costs.run_cost(units, price_table)
    return cost_usd, estimated


def _case_id(case: Any) -> str:
    value = case.get("id") if isinstance(case, Mapping) else getattr(case, "id", None)
    return "" if value is None else str(value)


def _ref_index(findings: Sequence[Any]) -> dict[str, int]:
    kept = [index for index, row in enumerate(findings) if not _drop_reason(row)]
    return {f"{AI_REF_PREFIX}{number}": index for number, index in enumerate(kept, start=1)}


def _drop_reason(row: Any) -> Any:
    if isinstance(row, Mapping):
        return row.get("drop_reason")
    return getattr(row, "drop_reason", None)


def _elapsed_ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _live(
    base: Mapping[str, Any], unit: dict[str, Any], elapsed: int, retries: int, price_table: Mapping[str, Any] | None,
) -> dict[str, Any]:
    cost_usd, cost_estimated, _unpriced = costs.run_cost([unit], price_table)
    return {
        **base,
        "cached": False,
        "llm_calls": 1 + retries,
        "model": unit["model"],
        "input_tokens": unit["input_tokens"],
        "output_tokens": unit["output_tokens"],
        "elapsed_ms": elapsed,
        "cost_usd": cost_usd,
        "cost_estimated": cost_estimated,
        "cost_source": unit["cost_source"] if costs.valid_usd(unit["cost_usd"]) is not None else "",
        "unit": unit,
        "parse_retries": retries,
    }


def _trace_meta(unit: Mapping[str, Any], elapsed: int, retries: int, first_error: str) -> dict[str, Any]:
    meta = {**unit, "elapsed_ms": elapsed}
    if retries:
        meta["parse_retries"] = retries
        meta["first_error"] = first_error
    return meta


def _cached_grades(
    path: Path, key: str, judge_model: str, prompt_sha: str, humans: Mapping[str, str], ai: Sequence[Any],
) -> tuple[tuple[Grade, ...], str] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        return _corrupt(path, f"unreadable ({exc})")
    try:
        entry = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _corrupt(path, f"not JSON ({exc.msg})")
    problem = _entry_problem(entry, key, judge_model, prompt_sha)
    if problem:
        return _corrupt(path, problem)
    try:
        grades = tuple(parse_judge_response(entry["response"], humans, ai_ref_files(ai)))
    except JudgeParseError as exc:
        return _corrupt(path, f"its response no longer parses ({exc})")
    stored_model = entry.get("response_model")
    return grades, stored_model if isinstance(stored_model, str) else ""


def _entry_problem(entry: Any, key: str, judge_model: str, prompt_sha: str) -> str:
    if not isinstance(entry, dict):
        return f"a JSON {type(entry).__name__}, not an object"
    if entry.get("version") != JUDGE_CACHE_VERSION:
        return f"version {entry.get('version')!r}, not {JUDGE_CACHE_VERSION}"
    if entry.get("key") != key:
        return "its key does not match its file name"
    if entry.get("model") != judge_model:
        return f"its model {entry.get('model')!r} is not {judge_model!r}"
    if entry.get("prompt_sha256") != prompt_sha:
        return "its prompt sha256 is not the current judge template's"
    if not isinstance(entry.get("response"), str):
        return "it has no response text"
    return ""


def _corrupt(path: Path, problem: str) -> None:
    logger.warning("judge cache entry %s is corrupt (%s); treating it as a miss and judging again", path, problem)
    return None


def _write_entry(path: Path, entry: Mapping[str, Any]) -> None:
    tmp = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("judge cache write to %s failed (the grades stand): %s", path, exc)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
