"""The ``prxref eval`` actions: replay labelled cases, score them, compare runs, gate an upgrade.

``prxref.cli`` routes ``eval run``, ``eval score``, ``eval compare`` and
``eval verdict`` to :func:`eval_run`, :func:`eval_score`, :func:`eval_compare`
and :func:`eval_verdict`. Each takes the
parsed ``argparse.Namespace`` and returns the process exit code;
:func:`eval_run` also takes the review runner and the record builder, which
the CLI passes in. A configuration problem raises ``ConfigError`` naming the
flag or argument that supplied it, and the CLI prints it as
``configuration error: ...`` and exits 2, as it does for ``review``.

The CLI imports this module lazily, inside its ``eval`` handler. This module
must never import ``prxref.cli``: that import would close a cycle.

``eval`` adds no environment variable; every setting is a flag. ``eval
run`` reads the repository config file ``review`` would read (#38). Every LLM
call it makes is single-shot, and it never posts to a forge.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import prxref
from prxref import eval_judge, eval_metrics, eval_precision, reviewer
from prxref.config import find_config_file, load_config, load_config_with_sources
from prxref.eval_cases import EvalCase, case_from_json_record, case_to_json, is_safe_id, load_cases
from prxref.eval_metrics import (
    FULL,
    JUDGE_ERROR,
    MAX_CREDITS_PER_FINDING,
    NONE,
    PARTIAL,
    GradedCase,
    match_case,
    score_cases,
)
from prxref.judge import GRADE_FULL, GRADE_NONE, GRADE_PARTIAL
from prxref.llm import ConfigError
from prxref.review_inputs import load_path_inputs

logger = logging.getLogger(__name__)

RUN_VERSION = 1
RUN_PROMPTS = ("worker", "systemic", "summary")
RUN_CONFIG_KEYS = (
    "llm_backend",
    "llm_models",
    "llm_max_tokens",
    "llm_parse_retries",
    "max_chunks",
    "chunk_token_budget",
    "chunk_max_files",
    "dedup_similarity",
    "group_findings",
    "max_warning_findings",
    "max_outofscope_findings",
    "max_findings_per_rule",
    "scoped_rules_max_chars",
    "suggestions",
    "context_followup",
    "repo_context",
    "repo_context_max_chars",
    "context_contract_globs",
    "context_exclude_globs",
    "context_standards_globs",
    "context_standards_max_chars",
    "repo_context_max_reads",
    "repo_context_max_chunk_reads",
    "routing_probe",
    "ci_wiring",
)
EVAL_PINS: Mapping[str, Any] = {
    "ci_wiring": "off",
    "routing_probe": "off",
    "context_standards_globs": [],
}
SCORE_VERSION = 1
SCORE_RUN_KEYS = ("prompts", "sampling", "review_rules", "scoped_rules", "config")
JUDGE_METHOD = "judge"


def eval_run(
    args: argparse.Namespace,
    *,
    run_review: Callable[..., Any],
    build_record: Callable[[Any], dict],
) -> int:
    """Replay every case of ``--cases`` and write one labelled run under ``--out``.

    Reads ``args.cases``, ``args.label``, ``args.out``, ``args.rules_file``,
    ``args.scoped_rules``, ``args.prompts_dir`` and ``args.resume``, and
    ``args.config`` / ``args.no_config`` when present (#38): the repository
    config file is resolved once as ``review`` resolves it (``--config``,
    else ``PRXREF_CONFIG_FILE``, else ``.prxref.toml`` in the working
    directory; ``--no-config`` reads none), and every case and ``run.json``'s
    ``config`` use it.
    ``run_review`` is the CLI's ``_run_review`` and
    ``build_record`` its ``_build_json_result`` (the ``--format json``
    payload); the CLI passes both, because this module cannot import it.

    Each of these raises ``ConfigError`` before any case runs or anything is
    written, so the command exits 2:

    - ``--label`` that is not one safe path segment (the case-id rule of
      :func:`prxref.eval_cases.is_safe_id`: ``''``, ``.``, ``..``, ``/`` and
      ``\\`` are refused);
    - a bad ``--cases`` dataset, from :func:`prxref.eval_cases.load_cases`,
      naming ``--cases``, the case id and the field;
    - a malformed environment, from ``load_config``, naming the variable;
    - a missing or invalid config file, naming ``--config``,
      ``PRXREF_CONFIG_FILE`` or the file and key;
    - an unusable rules file, scoped rules entry or prompts directory, loaded
      once as ``review`` loads it, through
      :func:`prxref.review_inputs.load_path_inputs` (the scoped rules against
      the always-on file), naming ``--rules-file``, ``--scoped-rules`` or
      ``--prompts-dir`` when the flag was given, ``<file>: <key>`` when the
      config file set the path, and ``PRXREF_REVIEW_RULES``,
      ``PRXREF_SCOPED_RULES`` or ``PRXREF_PROMPTS_DIR`` otherwise;
    - an existing ``<out>/<label>`` without ``--resume``, naming ``--label``;
    - a run directory that cannot be created, naming ``--out``.

    Each case is reviewed in process by ``run_review`` with ``post=False``
    and ``no_threads=True``, so nothing is ever posted. ``context_file`` is
    ``""`` and ``spec_sources`` is ``[]`` unless the case sets them, so the
    environment's ticket and spec inputs cannot leak into a case. The
    default-on features that would move a baseline are pinned off for every
    case by :data:`EVAL_PINS` (OD2): ``ci_wiring="off"``,
    ``routing_probe="off"`` and ``context_standards_globs=[]`` reach
    ``run_review`` as overrides, and the same values are the ``load_config``
    overrides ``run.json``'s ``config`` is read from, so it records what
    ran whatever the environment or the config file says.
    ``repo_dir`` is the case's ``repo_dir``, as ``review --repo-dir`` takes
    it, or ``None``. ``args.rules_file``, ``args.scoped_rules`` and ``args.prompts_dir`` are
    passed to every case as given, as ``review --rules-file``,
    ``--scoped-rules`` and ``--prompts-dir`` take them: ``None`` leaves the
    variable in force, and ``""`` (``[""]`` for the scoped rules) turns it
    off. A case with a ``pr_url`` gets the replay pinning ``run_review``
    applies by default; nothing extra is passed.

    Each case is fenced. A crash, a ``None`` result (an unrecognised URL) or
    any other exception is recorded as that case's ``error.json`` and the next
    case still runs. That includes a ``ConfigError`` the review raises for one
    case, such as an unreadable ``diff_file`` or a ``repo_dir`` that has
    vanished since the dataset was loaded: it is a failed case, not exit
    2. A review whose verdict is ``Error`` is recorded as a normal
    ``record.json`` carrying that verdict. The run returns 0 whatever the
    cases did.

    The run directory ``<out>/<label>/`` holds:

    - ``run.json``, written after the last case (see below);
    - ``cases/<id>/case.json``: the case, by
      :func:`prxref.eval_cases.case_to_json`, written before it runs, so
      ``eval score`` needs no ``--cases``;
    - ``cases/<id>/trace/``: the review's ``trace_dir`` (each review unit's
      prompt, response and meta);
    - ``cases/<id>/record.json``: ``build_record(result)``, when the review
      returned;
    - ``cases/<id>/error.json``: ``{"case_id", "error"}`` with ``error`` as
      ``"<ExceptionType>: <message>"``, when it did not.

    ``run.json`` holds, in this order: ``version`` (:data:`RUN_VERSION`);
    ``prxref_version`` (``prxref.__version__`` of the process that wrote it);
    ``label``; ``cases_path`` (``--cases`` as given); ``created_at`` (this
    invocation's start, ISO-8601 UTC with a ``Z``); ``case_ids`` in dataset
    order; ``prompts``, holding ``sha256`` (the sha256 of each packaged
    template of :data:`RUN_PROMPTS`, as ``reviewer.load_prompt`` reads it)
    and ``prompt_templates``; ``sampling``, ``review_rules`` and
    ``scoped_rules``; and ``config``, the keys of :data:`RUN_CONFIG_KEYS`
    from ``load_config``. ``config`` is an allowlist, so no credential is
    ever written. ``prompt_templates``, ``sampling``, ``review_rules`` and
    ``scoped_rules`` are copied from the first case, in dataset order, whose
    ``record.json`` has a verdict other than ``Error``; each is ``null`` when
    there is no such case, the record lacks the key, or the feature is off.

    With ``--resume`` an existing run directory is continued: every case that
    already has a ``record.json`` or an ``error.json`` is skipped, and
    ``run.json`` is rewritten from the records on disk.

    Standard output gets one line per case as it finishes, ``<id>: <verdict>
    (<n> active findings)`` (``finding`` when ``<n>`` is 1),
    ``<id>: failed: <error>`` or ``<id>: skipped (already recorded)``, then
    ``run directory: <out>/<label>``. A diff-file case without a ``pr_url``
    that runs normally logs nothing at WARNING or above. A case with a
    ``pr_url`` logs the WARNINGs a ``prxref review`` replay of it logs: one
    that starts ``replay shows the PR's CURRENT title and description``
    whenever its title and description fall back to the current ones (a
    forge without ``get_pr_history``, a failed history read, or a history
    that cannot pin them), and one more when it gives a ``diff_file`` and no
    ``head_sha``, because file context is then read at the PR's current head.
    """
    if not is_safe_id(args.label):
        raise ConfigError(
            "--label: must be one directory name of letters, digits, '.', '_' and '-' "
            f"that starts with a letter or digit, got {args.label!r}"
        )
    cases = load_cases(args.cases, source="--cases")
    config_file = find_config_file(
        explicit="off" if getattr(args, "no_config", False) else getattr(args, "config", None),
    )
    cfg, layers = load_config_with_sources(
        config_file=config_file,
        review_rules=args.rules_file,
        scoped_rules=args.scoped_rules,
        prompts_dir=args.prompts_dir,
        **_pins(),
        source_labels={
            "review_rules": "--rules-file",
            "scoped_rules": "--scoped-rules",
            "prompts_dir": "--prompts-dir",
        },
    )
    load_path_inputs(cfg, layers, config_file=config_file, ticket=False, evidence=False)
    created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_dir = Path(args.out) / args.label
    if run_dir.exists() and not args.resume:
        raise ConfigError(
            f"--label: the run {str(run_dir)!r} already exists; pass --resume to "
            "continue it, or choose another --label"
        )
    cases_dir = run_dir / "cases"
    try:
        cases_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(
            f"--out: cannot create the run directory {str(run_dir)!r}: {exc.strerror or exc}"
        ) from exc
    for case in cases:
        line = _run_case(
            case, cases_dir / case.id, args,
            run_review=run_review, build_record=build_record, config_file=config_file,
        )
        print(line, flush=True)
    _write_json(
        run_dir / "run.json",
        _run_json(args, cases, cases_dir, cfg, created_at=created_at),
    )
    print(f"run directory: {run_dir}", flush=True)
    return 0


def _run_case(
    case: EvalCase,
    case_dir: Path,
    args: argparse.Namespace,
    *,
    run_review: Callable[..., Any],
    build_record: Callable[[Any], dict],
    config_file: Path | None = None,
) -> str:
    """Run one fenced case into ``case_dir`` and return its stdout line.

    ``config_file`` is the repository config file every case reviews with
    (#38), handed to ``run_review`` as is.
    """
    if args.resume and any((case_dir / name).is_file() for name in ("record.json", "error.json")):
        return f"{case.id}: skipped (already recorded)"
    trace_dir = case_dir / "trace"
    trace_dir.mkdir(parents=True, exist_ok=True)
    _write_json(case_dir / "case.json", case_to_json(case))
    try:
        result = run_review(
            case.pr_url,
            post=False,
            no_threads=True,
            diff_file=case.diff_file,
            base_sha=case.base_sha,
            head_sha=case.head_sha,
            context_file=case.context_file or "",
            spec_sources=list(case.spec),
            rules_file=args.rules_file,
            scoped_rules=args.scoped_rules,
            prompts_dir=args.prompts_dir,
            trace_dir=str(trace_dir),
            repo_dir=case.repo_dir,
            config_file=config_file,
            evidence_files=[],
            **_pins(),
        )
        if result is None:
            raise RuntimeError(f"unrecognized PR URL {case.pr_url!r}")
        record = build_record(result)
        _write_json(case_dir / "record.json", record)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.debug("eval case %s failed", case.id, exc_info=True)
        _write_json(case_dir / "error.json", {"case_id": case.id, "error": error})
        return f"{case.id}: failed: {error}"
    count = _active_count(record)
    return f"{case.id}: {record.get('verdict')} ({count} active finding{'' if count == 1 else 's'})"


def _pins() -> dict[str, Any]:
    """A fresh copy of :data:`EVAL_PINS`, so no caller can mutate the shared lists."""
    return {key: list(value) if isinstance(value, list) else value for key, value in EVAL_PINS.items()}


def _active_count(record: Any) -> int:
    """How many rows of ``record['findings']`` are active (no ``drop_reason``)."""
    findings = record.get("findings") if isinstance(record, dict) else None
    return sum(
        1 for row in findings or [] if isinstance(row, dict) and row.get("drop_reason") is None
    )


def _run_json(
    args: argparse.Namespace,
    cases: list[EvalCase],
    cases_dir: Path,
    cfg: dict,
    *,
    created_at: str,
) -> dict[str, Any]:
    """Build ``run.json`` from the run's inputs and the records on disk.

    ``prompts.prompt_templates``, ``sampling``, ``review_rules`` and
    ``scoped_rules`` come from the first reviewed record and are always
    present, ``null`` when that record has none.
    """
    case_ids = [case.id for case in cases]
    first = _first_reviewed_record(cases_dir, case_ids) or {}
    return {
        "version": RUN_VERSION,
        "prxref_version": prxref.__version__,
        "label": args.label,
        "cases_path": args.cases,
        "created_at": created_at,
        "case_ids": case_ids,
        "prompts": {
            "sha256": {
                name: hashlib.sha256(reviewer.load_prompt(name).encode("utf-8")).hexdigest()
                for name in RUN_PROMPTS
            },
            "prompt_templates": first.get("prompt_templates"),
        },
        "sampling": first.get("sampling"),
        "review_rules": first.get("review_rules"),
        "scoped_rules": first.get("scoped_rules"),
        "config": {key: cfg[key] for key in RUN_CONFIG_KEYS},
    }


def _first_reviewed_record(cases_dir: Path, case_ids: list[str]) -> dict | None:
    """The first ``record.json``, in ``case_ids`` order, whose verdict is not ``Error``."""
    for case_id in case_ids:
        try:
            record = json.loads((cases_dir / case_id / "record.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("verdict") != "Error":
            return record
    return None


def _write_json(path: Path, obj: Any) -> None:
    """Write ``obj`` to ``path`` as indented JSON, via a temp file and ``os.replace``."""
    text = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def eval_score(args: argparse.Namespace) -> int:
    """Grade the run ``--label`` under ``--out`` against its human labels.

    Reads ``args.label``, ``args.out`` and ``args.judge_model``. The run is
    ``<out>/<label>/`` as :func:`eval_run` wrote it: its ``run.json`` lists
    the case ids, and each ``cases/<id>/`` holds the ``case.json`` labels and
    either the ``record.json`` or the ``error.json`` of that case. The
    directory is never listed; only the ids of ``run.json`` are scored.

    Grading has two tiers:

    - A label with ``must_match`` is graded deterministically by
      :func:`prxref.eval_metrics.match_case`: same file, ``|line delta| <= 5``
      and the predicate. It makes no LLM call.
    - The other labels of a case go to one single-shot judge call
      (:func:`prxref.eval_judge.judge_case`), which sees only those labels
      and returns ``full``, ``partial`` (half credit) or ``none`` per label.
      The judge numbers the post-gate AI findings ``A1``, ``A2``, ...,
      each extra location of a grouped finding as its own finding right
      after it; each credited ``ai_ref`` is mapped back through
      :attr:`~prxref.eval_judge.JudgeOutcome.ref_index` to its index in the
      record's ``findings`` list, dropped rows included, and ``ai_line`` is
      the line of the location credited. The code checks that
      the ref is real and in the label's file, and the judge's replies are
      cached under ``<out>/<label>/judge-cache/``; a live call's prompt and
      reply are traced as ``judge.*`` in the case's ``trace/``. A reply the
      judge parser rejects, for any reason, is asked for again with the same
      request up to ``llm_parse_retries`` times (``PRXREF_LLM_PARSE_RETRIES``,
      passed to :func:`~prxref.eval_judge.judge_case` as ``parse_retries``;
      #21). A judge call that fails, or a reply still rejected when the
      retries run out, makes every judge-tier label of that case
      ``judge_error``, which is left out of every denominator and never read
      as ``none``.
    - One AI finding credits at most two labels, across both tiers: when the
      deterministic tier has already used a finding's slots, a judge credit
      to it becomes ``none`` with a WARNING (``full`` keeps its slot before
      ``partial``, then label order). A grouped finding's JSON ``locations``
      make each location its own credit unit in both tiers; a row whose key
      is missing or null is read as not grouped. A record with no grouped
      row reaches the judge as written, so its prompt and cache key do not
      change.

    A case whose review failed (``error.json``) and a record whose verdict is
    ``Error`` are scored, not skipped: the author received no findings, so
    every label of the case is ``none`` and counts in the denominator. No
    judge call is made for a case with no active AI finding, because nothing
    could be credited.

    ``--judge-model`` is required whenever any label of the run lacks
    ``must_match``; it is checked before any judge call. The judge runs on
    the review's own backend, base URL and key with only the model changed
    (:func:`prxref.eval_judge.build_judge_client`). It is not built when
    every label has ``must_match``, and ``--judge-model`` is then unused. A
    judge model among the run's ``sampling.models`` logs a WARNING and stamps
    ``self_judged``; it is not an error.

    Each of these raises ``ConfigError``, so the command exits 2: a
    ``--label`` that is not one safe path segment; no ``run.json`` under
    ``<out>/<label>``, or a run file that cannot be read (both name
    ``--label``); a case directory with neither ``record.json`` nor
    ``error.json`` (naming ``--label``); a malformed environment, from
    ``load_config``; a missing or unusable ``--judge-model``.

    ``score.json`` and ``score.md`` are written to ``<out>/<label>/`` via a
    temp file and ``os.replace``, and a rescore overwrites both.
    ``score.json`` holds, in this order:

    - ``version``: :data:`SCORE_VERSION`;
    - ``label``: ``--label``;
    - ``run``: the run's ``prompts``, ``sampling``, ``review_rules``,
      ``scoped_rules`` and ``config``, copied from ``run.json``
      (:data:`SCORE_RUN_KEYS`), each ``null`` when ``run.json`` lacks it;
    - ``judge``: ``null`` when no judge was built, otherwise
      :func:`prxref.eval_judge.judge_stamp` (``model``, ``sampling``,
      ``prompt_version``, ``prompt_sha256``, ``self_judged``) followed by
      ``cost_usd`` and ``cost_estimated`` (:func:`prxref.eval_judge.judge_cost`
      over every judge request), ``llm_calls``, ``parse_retries`` (the
      calls that re-sent a request whose reply the judge parser rejected,
      summed over the cases; ``PRXREF_LLM_PARSE_RETRIES`` bounds them per
      case, #21), ``cached`` (cases served from the cache) and ``errors``
      (``[{"case_id", "error"}]``, by case id);
    - ``failed``: ``[{"case_id", "error"}]`` for each case whose review
      failed, by case id;
    - ``metrics`` and ``cases``: :func:`prxref.eval_metrics.score_cases`.
      Each ``cases[].findings[]`` entry's ``method`` is ``must_match`` or
      :data:`JUDGE_METHOD`. ``metrics`` ends with ``precision`` and each
      case row with ``precision``, both ``null`` without ``--precision``.

    ``--precision`` also grades precision (:mod:`prxref.eval_precision`):
    each active AI finding that no credited grade names (the
    ``unmatched_ai`` set; a grouped finding is one unit) gets a verdict
    ``valid``, ``nit``, ``invalid``, ``duplicate`` or ``unverifiable`` from
    one single-shot judge call per case that has such a finding. The judge
    sees the finding, the case's ``context_file`` text and the diff hunks of
    the finding's file, read from the case's ``diff_file`` or else from
    ``cases/<id>/diff.patch``. It needs ``--judge-model`` (a missing one
    raises ``ConfigError`` naming ``--precision``), uses the ``judge-cache/``
    and ``llm_parse_retries`` as the label judge does, and its calls and
    cost count in ``judge``. A failed call makes the case's units
    ``judge_error``, which is counted nowhere. ``metrics.precision`` is
    ``{"graded", "matched", "valid", "nit", "invalid", "duplicate",
    "unverifiable", "judge_error", "strict", "lenient"}`` where ``matched``
    is the active AI findings some credited grade names, ``strict`` is
    ``(matched + valid) / (matched + valid + nit + invalid + duplicate)``,
    ``lenient`` adds ``nit`` to the numerator, and both are ``null`` when
    the denominator is 0. A case row's ``precision`` is ``{"verdicts":
    [{"ai_ref", "verdict", "reason"}], <the counts above>}``, ``ai_ref``
    being an index into the record's ``findings``.

    ``score.md`` is the same result for a reader, in this order: a title, the
    headline (micro recall), a self-judging note when stamped, then the
    sections ``Failed cases``, ``Cases`` (one row per case), ``Recall by
    severity``, ``Recall by category``, ``Accepted labels``, ``Unmatched AI
    findings``, ``Precision``, ``Severity agreement`` (a human ``minor`` counts as
    ``warning``), ``Chunks failed``, ``Elapsed``, ``Cost`` and ``Judge``. A
    ``None`` cost is written as ``unknown`` and never summed. The judge's
    cost line names its parse retries only when there were any. It holds no
    wall-clock time of the scoring run.

    Standard output gets the headline line, then ``score: <path of
    score.md>``. The return value is 0.
    """
    if not is_safe_id(args.label):
        raise ConfigError(
            "--label: must be one directory name of letters, digits, '.', '_' and '-' "
            f"that starts with a letter or digit, got {args.label!r}"
        )
    precision = bool(getattr(args, "precision", False))
    if precision and args.judge_model is None:
        raise ConfigError(
            f"{eval_precision.PRECISION_FLAG}: requires --judge-model, because the precision judge is a model call"
        )
    run_dir = Path(args.out) / args.label
    run = _read_run(run_dir)
    cfg = load_config()
    cases = [_read_run_case(run_dir, case_id) for case_id in run["case_ids"]]
    judge_model = _judge_model(args.judge_model, cases)
    if precision and judge_model is None:
        judge_model = args.judge_model
    client = None
    self_judged = False
    if judge_model is not None:
        client = eval_judge.build_judge_client(cfg, judge_model)
        self_judged = eval_judge.check_self_judging(judge_model, run.get("sampling"))
    cache_dir = eval_judge.judge_cache_dir(args.out, args.label)
    graded: list[GradedCase] = []
    outcomes: list[eval_judge.JudgeOutcome] = []
    blocks: dict[str, dict[str, Any]] = {}
    precision_outcomes: list[eval_precision.PrecisionOutcome] = []
    for run_case in cases:
        graded_case, outcome = _grade_case(run_case, client, judge_model, cache_dir=cache_dir, cfg=cfg)
        if outcome is not None:
            outcomes.append(outcome)
        if precision:
            graded_case, block, precision_outcome = _precision_case(
                run_case, graded_case, client, judge_model, cache_dir=cache_dir, cfg=cfg
            )
            blocks[run_case.case.id] = block
            if precision_outcome is not None:
                precision_outcomes.append(precision_outcome)
        graded.append(graded_case)
    judge = None
    if client is not None and judge_model is not None:
        judge = _judge_block(
            client, judge_model, self_judged, outcomes, cfg["price_table"], extra=precision_outcomes
        )
    scored = _with_precision(score_cases(graded), blocks if precision else None)
    score = _score_json(args.label, run, cases, scored, judge)
    _write_json(run_dir / "score.json", score)
    _write_text(run_dir / "score.md", _score_markdown(score))
    print(_headline(score["metrics"]), flush=True)
    print(f"score: {run_dir / 'score.md'}", flush=True)
    return 0


_JUDGE_GRADES = {GRADE_FULL: FULL, GRADE_PARTIAL: PARTIAL, GRADE_NONE: NONE}
_CREDITED = (FULL, PARTIAL)


@dataclass(frozen=True)
class _RunCase:
    """One case of a run as ``eval score`` reads it back: its labels and its outcome."""

    case: EvalCase
    case_dir: Path
    record: dict[str, Any] | None
    error: str | None


def _read_run(run_dir: Path) -> dict[str, Any]:
    """Read and check ``<run_dir>/run.json``; every problem names ``--label``."""
    path = run_dir / "run.json"
    if not path.is_file():
        raise ConfigError(
            f"--label: there is no run {str(run_dir)!r} to score (no run.json); write it with "
            "'prxref eval run', or finish an interrupted one with --resume"
        )
    run = _read_run_file(path)
    case_ids = run.get("case_ids") if isinstance(run, dict) else None
    if (
        not isinstance(case_ids, list)
        or not all(is_safe_id(case_id) for case_id in case_ids)
        or len(set(case_ids)) != len(case_ids)
    ):
        raise ConfigError(f"--label: {path}: case_ids: must be a list of unique case ids")
    return run


def _read_run_file(path: Path) -> Any:
    """Parse one JSON file of the run; an unreadable one names ``--label``."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(
            f"--label: the run is incomplete: {path} is missing; finish it with 'prxref eval run --resume'"
        ) from exc
    except (OSError, ValueError) as exc:
        raise ConfigError(f"--label: cannot read {path}: {exc}") from exc


def _read_run_case(run_dir: Path, case_id: str) -> _RunCase:
    """Read one case's labels and its ``record.json`` or ``error.json``."""
    case_dir = run_dir / "cases" / case_id
    case_path = case_dir / "case.json"
    case = case_from_json_record(_read_run_file(case_path), source=f"--label: {case_path}")
    record_path = case_dir / "record.json"
    if record_path.is_file():
        record = _read_run_file(record_path)
        if not isinstance(record, dict) or not isinstance(record.get("findings"), list):
            raise ConfigError(f"--label: {record_path}: not a run record (an object with a findings list)")
        return _RunCase(case, case_dir, record, None)
    error_path = case_dir / "error.json"
    if not error_path.is_file():
        raise ConfigError(
            f"--label: the run is incomplete: case {case_id!r} has neither record.json nor error.json "
            f"in {case_dir}; finish it with 'prxref eval run --resume'"
        )
    payload = _read_run_file(error_path)
    error = payload.get("error") if isinstance(payload, dict) else None
    return _RunCase(case, case_dir, None, error if isinstance(error, str) and error else "unknown error")


def _judge_model(given: str | None, cases: Sequence[_RunCase]) -> str | None:
    """The judge model the run needs: ``None`` when every label has ``must_match``."""
    unjudged = [(run_case.case.id, label.id) for run_case in cases for label in run_case.case.expected
                if not label.must_match]
    if not unjudged:
        if given is not None:
            logger.info("every label has a must_match predicate; --judge-model %r is not used", given)
        return None
    if given is None:
        case_id, label_id = unjudged[0]
        raise ConfigError(
            f"--judge-model: required, because {len(unjudged)} label(s) have no must_match predicate "
            f"(the first is {label_id!r} of case {case_id!r})"
        )
    return given


def _grade(human_id: str, grade: str, ai_ref: int | None = None, ai_line: Any = None) -> dict[str, Any]:
    return {"human_id": human_id, "grade": grade, "ai_ref": ai_ref, "ai_line": ai_line, "method": JUDGE_METHOD}


def _row_value(row: Any, key: str) -> Any:
    return row.get(key) if isinstance(row, Mapping) else getattr(row, key, None)


def _grade_case(
    run_case: _RunCase,
    client: Any,
    judge_model: str | None,
    *,
    cache_dir: Path,
    cfg: Mapping[str, Any],
) -> tuple[GradedCase, eval_judge.JudgeOutcome | None]:
    """Grade one case in both tiers; the outcome is ``None`` when no judge call was needed.

    The deterministic tier grades the record's ``findings`` as written. The
    judge grades :func:`_judge_record`'s copy, in which each extra location
    of a grouped finding is its own row, and :func:`_judge_grades` maps its
    refs back to the record, so both tiers credit a grouped finding per
    location. The returned :class:`~prxref.eval_metrics.GradedCase` always
    holds the record's own ``findings``.
    """
    case, record = run_case.case, run_case.record
    findings: list[Any] = record["findings"] if record is not None else []
    grades = {grade["human_id"]: grade for grade in match_case(case.expected, findings)}
    judged = tuple(label for label in case.expected if not label.must_match)
    outcome = None
    active = any(_row_value(row, "drop_reason") is None for row in findings)
    if judged and record is not None and active and judge_model is not None:
        judge_record, origin = _judge_record(record)
        outcome = eval_judge.judge_case(
            client, judge_model, replace(case, expected=judged), judge_record,
            cache_dir=cache_dir,
            price_table=cfg["price_table"],
            max_tokens=cfg["llm_max_tokens"],
            trace_dir=str(run_case.case_dir / "trace"),
            parse_retries=cfg["llm_parse_retries"],
        )
        grades.update(_judge_grades(outcome, judged, judge_record["findings"], origin))
    else:
        grades.update((label.id, _grade(label.id, NONE)) for label in judged)
    _cap_across_tiers(case, grades)
    graded = GradedCase(
        case_id=case.id,
        expected=case.expected,
        ai_findings=findings,
        grades=[grades[label.id] for label in case.expected],
        record=record,
        judge_cost_usd=outcome.cost_usd if outcome is not None else 0.0,
        judge_cost_estimated=outcome.cost_estimated if outcome is not None else False,
    )
    return graded, outcome


def _precision_case(
    run_case: _RunCase,
    graded: GradedCase,
    client: Any,
    judge_model: str | None,
    *,
    cache_dir: Path,
    cfg: Mapping[str, Any],
) -> tuple[GradedCase, dict[str, Any], eval_precision.PrecisionOutcome | None]:
    """Grade one case's unmatched active AI findings for ``--precision``.

    Returns the case with the precision call's cost added to its judge cost,
    its ``precision`` row, and the outcome (``None`` when the case had no
    unmatched active finding, so no call was made).
    """
    record = run_case.record
    findings: list[Any] = record["findings"] if record is not None else []
    credited = eval_precision.credited_refs(graded.grades)
    units = eval_precision.unit_indexes(findings, credited)
    outcome = None
    if units and client is not None and judge_model is not None:
        outcome = eval_precision.grade_case(
            client, judge_model, run_case.case.id, findings, credited,
            ticket=_read_ticket(run_case.case.context_file),
            diff=eval_precision.load_diff(run_case.case.diff_file, run_case.case_dir),
            cache_dir=cache_dir,
            price_table=cfg["price_table"],
            max_tokens=cfg["llm_max_tokens"],
            trace_dir=str(run_case.case_dir / "trace"),
            parse_retries=cfg["llm_parse_retries"],
        )
    block = eval_precision.case_block(outcome, units, len(credited))
    cost, estimated = eval_precision.add_cost(graded.judge_cost_usd, graded.judge_cost_estimated, outcome)
    return replace(graded, judge_cost_usd=cost, judge_cost_estimated=estimated), block, outcome


def _with_precision(scored: dict[str, Any], blocks: Mapping[str, dict[str, Any]] | None) -> dict[str, Any]:
    """Append ``precision`` to ``metrics`` and to each case row of ``scored``, in place.

    ``blocks`` maps a case id to its :func:`prxref.eval_precision.case_block`;
    ``None`` (no ``--precision``) writes ``null`` everywhere.
    """
    scored["metrics"]["precision"] = None if blocks is None else eval_precision.summarize(blocks.values())
    for row in scored["cases"]:
        row["precision"] = None if blocks is None else blocks.get(row["case_id"])
    return scored


def _read_ticket(path: str | None) -> str | None:
    """The case's ticket text, or ``None`` when it has none or it cannot be read."""
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _judge_record(record: dict[str, Any]) -> tuple[dict[str, Any], list[int]]:
    """The record the judge grades, and the record index each of its ``findings`` rows came from.

    Every row of ``record["findings"]`` is kept, in order. An active row
    (``drop_reason`` null) that is a grouped representative is followed by
    one copy per extra location of
    :func:`prxref.eval_metrics.credit_locations`, with that location's
    ``file`` and ``line`` and a null
    :data:`~prxref.eval_metrics.LOCATIONS_FIELD`. The judge then numbers
    each location as its own AI finding, under its own per-finding cap, as
    the deterministic tier counts it. A dropped row is never expanded, so
    the judge still skips it. A record with nothing to expand is returned
    itself, so its judge prompt and cache key are exactly those of the
    record as written.
    """
    findings = record["findings"]
    rows: list[Any] = []
    origin: list[int] = []
    for index, row in enumerate(findings):
        rows.append(row)
        origin.append(index)
        if not isinstance(row, Mapping) or row.get("drop_reason") is not None:
            continue
        for file, line in eval_metrics.credit_locations(row)[1:]:
            rows.append({**row, "file": file, "line": line, eval_metrics.LOCATIONS_FIELD: None})
            origin.append(index)
    if len(rows) == len(findings):
        return record, origin
    return {**record, "findings": rows}, origin


def _judge_grades(
    outcome: eval_judge.JudgeOutcome, judged: Sequence[Any], rows: Sequence[Any], origin: Sequence[int]
) -> dict[str, dict[str, Any]]:
    """Turn a judge outcome into eval_metrics grades whose ``ai_ref`` indexes the record's ``findings``.

    ``rows`` and ``origin`` are :func:`_judge_record`'s. The judge's ref
    names a row of ``rows`` through ``outcome.ref_index``; ``ai_ref`` is the
    index ``origin`` gives that row in the record, dropped rows included,
    and ``ai_line`` is that row's own ``line``: for a grouped finding, the
    location the judge credited. So ``(ai_ref, label file, ai_line)`` is one
    credit unit per location, as in the deterministic tier.
    """
    if not outcome.ok or outcome.grades is None:
        return {label.id: _grade(label.id, JUDGE_ERROR) for label in judged}
    grades: dict[str, dict[str, Any]] = {}
    for grade in outcome.grades:
        value = _JUDGE_GRADES[grade.grade]
        if value == NONE or grade.ai_ref is None:
            grades[grade.human_id] = _grade(grade.human_id, NONE)
            continue
        ref = outcome.ref_index[grade.ai_ref]
        grades[grade.human_id] = _grade(grade.human_id, value, origin[ref], _row_value(rows[ref], "line"))
    return grades


def _cap_across_tiers(case: EvalCase, grades: dict[str, dict[str, Any]]) -> None:
    """Hold each AI credit unit to the per-finding cap across the two tiers, in place."""
    used: Counter[tuple[Any, str, Any]] = Counter()
    for label in case.expected:
        grade = grades[label.id]
        if label.must_match and grade["grade"] in _CREDITED:
            used[(grade["ai_ref"], label.file, grade["ai_line"])] += 1
    judged = [
        (index, label) for index, label in enumerate(case.expected)
        if not label.must_match and grades[label.id]["grade"] in _CREDITED
    ]
    for _, label in sorted(judged, key=lambda item: (grades[item[1].id]["grade"] != FULL, item[0])):
        grade = grades[label.id]
        unit = (grade["ai_ref"], label.file, grade["ai_line"])
        if used[unit] >= MAX_CREDITS_PER_FINDING:
            logger.warning(
                "case %r: AI finding %s already credits %d labels; the judge's %s credit for %r is "
                "graded none (cap %d)",
                case.id, grade["ai_ref"], used[unit], grade["grade"], label.id, MAX_CREDITS_PER_FINDING,
            )
            grades[label.id] = _grade(label.id, NONE)
            continue
        used[unit] += 1


def _judge_block(
    client: Any,
    judge_model: str,
    self_judged: bool,
    outcomes: Sequence[eval_judge.JudgeOutcome],
    price_table: Any,
    *,
    extra: Sequence[eval_precision.PrecisionOutcome] = (),
) -> dict[str, Any]:
    """The ``judge`` stamp of ``score.json``: the judge stamp plus the run's judge cost, calls and retries.

    ``extra`` holds the precision outcomes of ``--precision``. Their cost,
    calls, parse retries and cache hits are folded into the totals; their
    errors are not listed in ``errors``, which names label-judge errors, and
    show as ``judge_error`` in the precision block instead.
    """
    every = [*outcomes, *extra]
    cost_usd, cost_estimated = eval_judge.judge_cost(every, price_table)
    return {
        **eval_judge.judge_stamp(client, judge_model, self_judged=self_judged),
        "cost_usd": cost_usd,
        "cost_estimated": cost_estimated,
        "llm_calls": sum(outcome.llm_calls for outcome in every),
        "parse_retries": sum(outcome.parse_retries for outcome in every),
        "cached": sum(1 for outcome in every if outcome.cached),
        "errors": [
            {"case_id": outcome.case_id, "error": outcome.error}
            for outcome in sorted(outcomes, key=lambda item: item.case_id)
            if not outcome.ok
        ],
    }


def _score_json(
    label: str,
    run: Mapping[str, Any],
    cases: Sequence[_RunCase],
    scored: Mapping[str, Any],
    judge: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble ``score.json`` in its fixed key order."""
    return {
        "version": SCORE_VERSION,
        "label": label,
        "run": {key: run.get(key) for key in SCORE_RUN_KEYS},
        "judge": judge,
        "failed": [
            {"case_id": run_case.case.id, "error": run_case.error}
            for run_case in sorted(cases, key=lambda item: item.case.id)
            if run_case.record is None
        ],
        "metrics": scored["metrics"],
        "cases": scored["cases"],
    }


def _write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file and ``os.replace``."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _cell(value: Any) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _num(value: float) -> str:
    return f"{value:g}"


def _usd(value: float | None) -> str:
    return "unknown" if value is None else f"${value:.4f}"


def _seconds(value: int | None) -> str:
    return "unknown" if value is None else f"{value / 1000:.1f} s"


def _count(value: int | None) -> str:
    return "unknown" if value is None else str(value)


def _recall_text(block: Mapping[str, Any]) -> str:
    text = f"{_pct(block['recall'])} (credit {_num(block['credit'])} of {block['scored']} scored labels"
    if block["judge_error"]:
        text += f"; {block['judge_error']} with a judge error, not scored"
    return text + ")"


def _headline(metrics: Mapping[str, Any]) -> str:
    """The one-line result: micro recall over every scored label."""
    return f"Recall (micro): {_recall_text(metrics['recall'])} over {metrics['case_count']} cases"


def _recall_table(heading: str, blocks: Mapping[str, Mapping[str, Any]]) -> list[str]:
    if not blocks:
        return ["No labels."]
    lines = [
        f"| {heading} | Recall | Credit | Full | Partial | None | Judge error |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, block in blocks.items():
        lines.append(
            f"| {_cell(name)} | {_pct(block['recall'])} | {_num(block['credit'])} of {block['scored']} | "
            f"{block['full']} | {block['partial']} | {block['none']} | {block['judge_error']} |"
        )
    return lines


def _case_table(score: Mapping[str, Any]) -> list[str]:
    failed = {entry["case_id"] for entry in score["failed"]}
    lines = [
        "| Case | Verdict | Recall | Credit | Full | Partial | None | Judge error | AI findings | "
        "Unmatched AI | Chunks failed | Elapsed | Review cost | Judge cost |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in score["cases"]:
        verdict = "failed" if row["case_id"] in failed else _cell(row["verdict"] or "unknown")
        review_cost = _usd(row["review_cost_usd"]) + (" (est.)" if row["review_cost_estimated"] else "")
        judge_cost = _usd(row["judge_cost_usd"]) + (" (est.)" if row["judge_cost_estimated"] else "")
        lines.append(
            f"| {_cell(row['case_id'])} | {verdict} | {_pct(row['recall'])} | "
            f"{_num(row['credit'])} of {row['scored']} | {row['full']} | {row['partial']} | {row['none']} | "
            f"{row['judge_error']} | {row['ai_findings']} | {row['unmatched_ai']} | "
            f"{_count(row['chunks_failed'])} | {_seconds(row['elapsed_ms'])} | {review_cost} | {judge_cost} |"
        )
    return lines


def _agreement_lines(block: Mapping[str, Any]) -> list[str]:
    lines = [
        f"Agreed on {block['agreed']} of {block['compared']} credited labels ({_pct(block['rate'])}); "
        "a human `minor` counts as `warning`."
    ]
    confusion = [
        f"- human `{_cell(human)}`, AI `{_cell(ai)}`: {count}"
        for human, row in block["confusion"].items()
        for ai, count in row.items()
    ]
    return [*lines, "", *confusion] if confusion else lines


def _total_line(block: Mapping[str, Any], render: Callable[[Any], str]) -> str:
    text = f"{render(block['total'])} in total"
    if block["missing"]:
        text += f"; unknown for {block['missing']} case(s), which are not counted"
    return text + "."


def _review_cost_line(block: Mapping[str, Any]) -> str:
    if block["total_usd"] is None:
        cases = block["priced"] + block["unpriced"]
        return f"- Review: unknown ({block['unpriced']} of {cases} case(s) unpriced; an unknown cost is never summed)"
    text = f"- Review: {_usd(block['total_usd'])} in total, {_usd(block['per_pr_usd'])} per PR"
    if block["estimated"]:
        text += f" (estimated for {block['estimated']} case(s))"
    return text


def _judge_cost_line(judge: Mapping[str, Any] | None) -> str:
    if judge is None:
        return "- Judge: none (no judge ran)"
    text = f"- Judge: {_usd(judge['cost_usd'])}"
    if judge["cost_usd"] is not None and judge["cost_estimated"]:
        text += " (estimated)"
    text += f", {judge['llm_calls']} call(s)"
    retries = judge["parse_retries"]
    if retries:
        text += f" ({retries} parse {'retry' if retries == 1 else 'retries'})"
    return text + f", {judge['cached']} case(s) from the cache"


def _judge_lines(judge: Mapping[str, Any] | None) -> list[str]:
    if judge is None:
        return ["No judge: every label has a must_match predicate."]
    lines = [
        f"- Model: `{_cell(judge['model'])}`",
        f"- Prompt: version {judge['prompt_version']}, sha256 `{judge['prompt_sha256']}`",
        f"- Self-judged: {'yes' if judge['self_judged'] else 'no'}",
    ]
    if not judge["errors"]:
        return [*lines, "- Judge errors: none"]
    return [*lines, *(f"- Judge error in `{_cell(e['case_id'])}`: {_cell(e['error'])}" for e in judge["errors"])]


def _precision_lines(block: Mapping[str, Any] | None) -> list[str]:
    if block is None:
        return ["Not graded: run `prxref eval score --precision` to grade the unmatched AI findings."]
    return [
        f"Strict precision: {_pct(block['strict'])}. Lenient precision: {_pct(block['lenient'])}.",
        "",
        f"- Matched (credited by a label): {block['matched']}",
        f"- Graded unmatched: {block['graded']} ({block['valid']} valid, {block['nit']} nit, "
        f"{block['invalid']} invalid, {block['duplicate']} duplicate, {block['unverifiable']} unverifiable)",
        f"- Judge error (not counted): {block['judge_error']}",
    ]


def _score_markdown(score: Mapping[str, Any]) -> str:
    """Render ``score.md`` from the ``score.json`` dict alone."""
    metrics = score["metrics"]
    judge = score["judge"]
    unmatched = metrics["unmatched_ai"]
    per_pr = "n/a" if unmatched["per_pr"] is None else f"{unmatched['per_pr']:.2f}"
    lines = [f"# prxref eval score: {_cell(score['label'])}", "", _headline(metrics)]
    if judge is not None and judge["self_judged"]:
        lines += [
            "",
            f"**Self-judged:** the judge model `{_cell(judge['model'])}` is also one of the reviewer's "
            "models, so these grades may be inflated.",
        ]
    failed = [f"- `{_cell(entry['case_id'])}`: {_cell(entry['error'])}" for entry in score["failed"]]
    lines += ["", "## Failed cases", "", *(failed or ["None."])]
    lines += ["", "## Cases", "", *_case_table(score)]
    lines += ["", "## Recall by severity", "", *_recall_table("Severity", metrics["recall_by_severity"])]
    lines += ["", "## Recall by category", "", *_recall_table("Category", metrics["recall_by_category"])]
    lines += ["", "## Accepted labels", "", f"Recall over accepted labels: {_recall_text(metrics['recall_accepted'])}."]
    lines += [
        "", "## Unmatched AI findings", "",
        f"{unmatched['total']} of {unmatched['ai_findings']} active AI findings matched no label: "
        f"{per_pr} per PR.",
    ]
    lines += ["", "## Precision", "", *_precision_lines(metrics.get("precision"))]
    lines += ["", "## Severity agreement", "", *_agreement_lines(metrics["severity_agreement"])]
    lines += ["", "## Chunks failed", "", _total_line(metrics["chunks_failed"], str)]
    lines += ["", "## Elapsed", "", _total_line(metrics["elapsed_ms"], _seconds)]
    lines += ["", "## Cost", "", _review_cost_line(metrics["review_cost"]), _judge_cost_line(judge)]
    lines += ["", "## Judge", "", *_judge_lines(judge)]
    return "\n".join(lines) + "\n"


def eval_compare(args: argparse.Namespace) -> int:
    """Print two scored runs side by side, then every label whose credit changed.

    Reads ``args.run_a``, ``args.run_b`` and ``args.out``. Each of ``A``
    (``run_a``) and ``B`` (``run_b``) names a run that :func:`eval_score` has
    scored. It is a label when it is one safe path segment
    (:func:`prxref.eval_cases.is_safe_id`) and ``<out>/<label>/`` is a
    directory, even when a directory of the same name sits in the working
    directory; otherwise it is the path of a run directory. Only the run's
    ``score.json`` is compared; each case's ``record.json`` is read for its
    replay stamp alone.

    Standard output is Markdown in this order, and holds no timestamp and no
    path other than ``A`` and ``B`` as given, so comparing the same two runs
    twice prints byte-identical output:

    - ``# prxref eval compare``, then ``- A: <A>`` and ``- B: <B>``.
    - ``## Metrics``: the table ``| Metric | A | B | Change |`` with the rows
      ``Cases``, ``Recall (micro)``, ``Judge errors``, one ``Recall,
      severity ...`` row per severity and one ``Recall, category ...`` row
      per category of either run (each sorted), ``Recall, accepted labels``,
      ``Unmatched AI per PR``, ``Severity agreement``, ``Chunks failed``,
      ``Elapsed``, ``Review cost`` and ``Judge cost``. A severity or category
      that only one run has is ``n/a`` on the other side. A recall or agreement change is in percentage points (``pp``).
      The change is ``unknown``, never a number, when either side is unknown:
      a ``None`` value, a missing key, or a total that leaves some case out.
      A run with no judge has the judge cost ``none``, counted as 0.
    - ``## Stable-id reuse``, between Metrics and Changed labels, ONLY
      when at least one case both runs score carries finding ids in its
      ``record.json`` (a default run; two runs without
      ids print nothing here, byte-identical to a prxref without the
      section): the table ``| Case | A ids | B ids | Reuse |``, one row
      per shared case with ids on at least one side, the reuse being the
      fraction of B's active finding ids A already held
      (:func:`stable_id_reuse`); a side with no ids is ``n/a``, and so
      is its reuse.
    - ``## Changed labels``: a table ``| Case | Label | Location | A | B |``
      of every label, keyed by case id and label id and sorted by that key,
      whose ``grade`` or ``credit`` differs between the runs. A grade reads
      ``<grade> (<credit>)``, or ``judge_error`` when the credit is ``None``.
    - ``## Only in one run``: one bullet per case or label that only one run
      holds, sorted; these are not counted as changes.

    An empty section reads ``None.``. A WARNING is logged, and the comparison
    still printed, when:

    - the judge prompt sha256 or the judge model differ. A run with no judge
      (every label has ``must_match``) differs from a judged run only when
      both runs graded labels in the judge tier; two runs without a judge
      never differ;
    - the runs cover different cases;
    - a case holds a ``record.json`` in both runs and their replay stamps'
      ``description`` differ (a missing key reads as ``null``). A failed
      case, which holds ``error.json``, is skipped.

    Each of these raises ``ConfigError`` naming ``A`` or ``B``, so the command
    exits 2: a run with no ``score.json`` (when it holds ``run.json``, the
    message says to run ``prxref eval score`` first); a ``score.json`` or
    ``record.json`` that cannot be read; a ``score.json`` whose ``version``
    is not :data:`SCORE_VERSION` or whose shape is not a score. The return
    value is 0.
    """
    run_a = _read_scored_run("A", args.run_a, args.out)
    run_b = _read_scored_run("B", args.run_b, args.out)
    _warn_judge(run_a.score, run_b.score)
    _warn_cases(run_a.score, run_b.score)
    _warn_replay(run_a, run_b)
    print(_compare_text(run_a, run_b), end="", flush=True)
    return 0


@dataclass(frozen=True)
class _ScoredRun:
    """One side of ``eval compare``: its argument, the run as given, its directory and ``score.json``."""

    argument: str
    given: str
    run_dir: Path
    score: dict[str, Any]


def _read_scored_run(argument: str, given: str, out: str) -> _ScoredRun:
    """Resolve ``A`` or ``B`` to a scored run and read its ``score.json``."""
    label_dir = Path(out) / given
    as_label = is_safe_id(given) and label_dir.is_dir()
    run_dir = label_dir if as_label else Path(given)
    path = run_dir / "score.json"
    if not path.is_file():
        if (run_dir / "run.json").is_file():
            resolved = run_dir.resolve()
            name, parent = (given, out) if as_label else (resolved.name, str(resolved.parent))
            raise ConfigError(
                f"{argument}: the run {given!r} is not scored yet (no score.json in {run_dir}); "
                f"run 'prxref eval score --label {name} --out {parent}' first"
            )
        raise ConfigError(
            f"{argument}: there is no scored run {given!r}: it is neither a label under --out {out!r} "
            "nor a run directory holding a score.json"
        )
    score = _read_compare_json(argument, path)
    if not isinstance(score, dict):
        raise ConfigError(f"{argument}: {path}: not a score.json (a JSON object)")
    version = score.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != SCORE_VERSION:
        raise ConfigError(
            f"{argument}: {path}: score.json version {version!r} is not {SCORE_VERSION}; "
            "rescore the run with this prxref's 'prxref eval score'"
        )
    judge = score.get("judge")
    if not isinstance(score.get("metrics"), dict) or not isinstance(score.get("cases"), list) or not (
        judge is None or isinstance(judge, dict)
    ):
        raise ConfigError(f"{argument}: {path}: not a score.json (metrics, cases and judge are malformed)")
    return _ScoredRun(argument, given, run_dir, score)


def _read_compare_json(argument: str, path: Path) -> Any:
    """Parse one JSON file of a compared run; an unreadable one names ``A`` or ``B``."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{argument}: cannot read {path}: {exc}") from exc


def _shown(value: Any) -> str:
    return json.dumps(value)


def _judge_tier_labels(score: Mapping[str, Any]) -> bool:
    return any(entry.get("method") == JUDGE_METHOD for row in score["cases"] for entry in row.get("findings") or [])


def _warn_judge(score_a: Mapping[str, Any], score_b: Mapping[str, Any]) -> None:
    """Warn when the two runs' judges differ in prompt sha256 or model."""
    judge_a, judge_b = score_a.get("judge"), score_b.get("judge")
    if judge_a is None and judge_b is None:
        return
    if (judge_a is None or judge_b is None) and not (_judge_tier_labels(score_a) and _judge_tier_labels(score_b)):
        return
    for key, what in (("prompt_sha256", "judge prompt sha256"), ("model", "judge model")):
        value_a = judge_a.get(key) if judge_a is not None else None
        value_b = judge_b.get(key) if judge_b is not None else None
        if value_a != value_b:
            logger.warning(
                "the %s differs: A %s, B %s; the judge grades are not like for like",
                what, _shown(value_a), _shown(value_b),
            )


def _case_ids(score: Mapping[str, Any]) -> set[str]:
    return {row["case_id"] for row in score["cases"]}


def _warn_cases(score_a: Mapping[str, Any], score_b: Mapping[str, Any]) -> None:
    """Warn when the two runs do not cover the same cases."""
    ids_a, ids_b = _case_ids(score_a), _case_ids(score_b)
    if ids_a != ids_b:
        logger.warning(
            "the runs cover different cases (only in A: %s; only in B: %s); the metrics are not like for like",
            ", ".join(sorted(ids_a - ids_b)) or "none", ", ".join(sorted(ids_b - ids_a)) or "none",
        )


def _replay_description(run: _ScoredRun, case_id: str) -> tuple[bool, Any]:
    """Whether the case holds a ``record.json``, and its replay stamp's ``description``."""
    path = run.run_dir / "cases" / case_id / "record.json"
    if not path.is_file():
        return False, None
    record = _read_compare_json(run.argument, path)
    replay = record.get("replay") if isinstance(record, dict) else None
    return True, replay.get("description") if isinstance(replay, dict) else None


def _warn_replay(run_a: _ScoredRun, run_b: _ScoredRun) -> None:
    """Warn, per case recorded in both runs, when the replay description stamps differ."""
    for case_id in sorted(_case_ids(run_a.score) & _case_ids(run_b.score)):
        if not is_safe_id(case_id):
            continue
        recorded_a, description_a = _replay_description(run_a, case_id)
        recorded_b, description_b = _replay_description(run_b, case_id)
        if recorded_a and recorded_b and description_a != description_b:
            logger.warning(
                "case %r: the replay description differs: A %s, B %s; the two arms did not see the same "
                "PR description, so this case is not a like-for-like comparison",
                case_id, _shown(description_a), _shown(description_b),
            )


def _case_record(run: _ScoredRun, case_id: str) -> Any:
    """The case's ``record.json``, or ``None`` when the run holds none."""
    path = run.run_dir / "cases" / case_id / "record.json"
    if not path.is_file():
        return None
    return _read_compare_json(run.argument, path)


def _active_ids(record: Any) -> set[str]:
    """The active (no ``drop_reason``) finding ids a case's record carries."""
    findings = record.get("findings") if isinstance(record, dict) else None
    return {
        row["id"]
        for row in findings or []
        if isinstance(row, dict) and row.get("drop_reason") is None
        and isinstance(row.get("id"), str)
    }


def stable_id_reuse(record_a: Any, record_b: Any) -> float | None:
    """The id churn between two records of one case (#71): shared ids over B's.

    ``|A ids ∩ B ids| / |B ids|`` over the ACTIVE finding ids each record's
    findings carry — 1.0 when B re-raised exactly A's findings, 0.0 when
    the two share none. ``None`` when either record carries no id at all
    (stable ids off, no record, or no active finding with an id), because
    a missing side is not zero reuse: it is nothing to measure.
    """
    ids_a, ids_b = _active_ids(record_a), _active_ids(record_b)
    if not ids_a or not ids_b:
        return None
    return len(ids_a & ids_b) / len(ids_b)


def _stable_id_rows(run_a: _ScoredRun, run_b: _ScoredRun) -> list[str]:
    """The ``Stable-id reuse`` table: per-case id churn between the runs.

    One row per case both runs score whose records carry ids on at least
    one side — a comparison of two runs that never stamped ids emits
    nothing at all, so its output is byte-identical to a prxref without
    the section. A side with no ids renders ``n/a`` and the reuse with
    it: the metric is measurable only when both sides stamped ids.
    """
    rows: list[str] = []
    for case_id in sorted(_case_ids(run_a.score) & _case_ids(run_b.score)):
        record_a, record_b = _case_record(run_a, case_id), _case_record(run_b, case_id)
        ids_a, ids_b = _active_ids(record_a), _active_ids(record_b)
        if not ids_a and not ids_b:
            continue
        reuse = stable_id_reuse(record_a, record_b)
        cell_a = str(len(ids_a)) if ids_a else "n/a"
        cell_b = str(len(ids_b)) if ids_b else "n/a"
        cell_reuse = f"{reuse:.0%}" if reuse is not None else "n/a"
        rows.append(f"| {_cell(case_id)} | {cell_a} | {cell_b} | {cell_reuse} |")
    return rows


def _compare_text(run_a: _ScoredRun, run_b: _ScoredRun) -> str:
    """Render the comparison printed to standard output."""
    score_a, score_b = run_a.score, run_b.score
    lines = ["# prxref eval compare", "", f"- A: {run_a.given}", f"- B: {run_b.given}"]
    lines += ["", "## Metrics", "", *_metric_table(score_a, score_b)]
    reuse_rows = _stable_id_rows(run_a, run_b)
    if reuse_rows:
        lines += [
            "", "## Stable-id reuse", "",
            "| Case | A ids | B ids | Reuse |", "|---|---:|---:|---:|", *reuse_rows,
        ]
    lines += ["", "## Changed labels", "", *_changed_labels(score_a, score_b)]
    lines += ["", "## Only in one run", "", *_only_in_one_run(score_a, score_b)]
    return "\n".join(lines) + "\n"


_Side = tuple[Any, str]


def _signed(value: float, digits: int) -> str:
    rounded = round(value, digits) + 0.0
    return f"{rounded:.{digits}f}" if rounded == 0 else f"{rounded:+.{digits}f}"


def _points_change(delta: float) -> str:
    return f"{_signed(delta * 100, 1)} pp"


def _count_change(delta: float) -> str:
    return _signed(delta, 0)


def _per_pr_change(delta: float) -> str:
    return _signed(delta, 2)


def _seconds_change(delta: float) -> str:
    return f"{_signed(delta / 1000, 1)} s"


def _usd_change(delta: float) -> str:
    rounded = round(delta, 4) + 0.0
    if rounded == 0:
        return "$0.0000"
    return f"{'+' if rounded > 0 else '-'}${abs(rounded):.4f}"


def _recall_side(block: Mapping[str, Any] | None) -> _Side:
    if block is None:
        return None, "n/a"
    return block["recall"], f"{_pct(block['recall'])} ({_num(block['credit'])} of {block['scored']})"


def _count_side(value: int | None) -> _Side:
    return value, _count(value)


def _unmatched_side(block: Mapping[str, Any] | None) -> _Side:
    if block is None or block["per_pr"] is None:
        return None, "n/a"
    return block["per_pr"], f"{block['per_pr']:.2f} ({block['total']} of {block['ai_findings']})"


def _precision_side(block: Mapping[str, Any] | None, key: str) -> _Side:
    if not isinstance(block, Mapping) or block.get(key) is None:
        return None, "n/a"
    return block[key], _pct(block[key])


def _agreement_side(block: Mapping[str, Any] | None) -> _Side:
    if block is None:
        return None, "n/a"
    return block["rate"], f"{_pct(block['rate'])} ({block['agreed']} of {block['compared']})"


def _total_side(block: Mapping[str, Any] | None, render: Callable[[Any], str]) -> _Side:
    if block is None:
        return None, "n/a"
    if block["missing"]:
        return None, f"{render(block['total'])} ({block['missing']} unknown)"
    return block["total"], render(block["total"])


def _review_cost_side(block: Mapping[str, Any] | None) -> _Side:
    if block is None:
        return None, "n/a"
    total = block["total_usd"]
    return total, _usd(total) + (" (est.)" if total is not None and block["estimated"] else "")


def _judge_cost_side(judge: Mapping[str, Any] | None) -> _Side:
    if judge is None:
        return 0.0, "none"
    cost = judge["cost_usd"]
    return cost, _usd(cost) + (" (est.)" if cost is not None and judge["cost_estimated"] else "")


def _metric_row(name: str, side_a: _Side, side_b: _Side, change: Callable[[Any], str]) -> str:
    (value_a, cell_a), (value_b, cell_b) = side_a, side_b
    delta = "unknown" if value_a is None or value_b is None else change(value_b - value_a)
    return f"| {name} | {cell_a} | {cell_b} | {delta} |"


def _metric_table(score_a: Mapping[str, Any], score_b: Mapping[str, Any]) -> list[str]:
    """The ``Metrics`` table: A, B and the change, one row per metric."""
    a, b = score_a["metrics"], score_b["metrics"]
    recall_a, recall_b = a.get("recall"), b.get("recall")
    rows = [
        _metric_row("Cases", _count_side(a.get("case_count")), _count_side(b.get("case_count")), _count_change),
        _metric_row("Recall (micro)", _recall_side(recall_a), _recall_side(recall_b), _points_change),
        _metric_row(
            "Judge errors",
            _count_side(recall_a["judge_error"] if recall_a is not None else None),
            _count_side(recall_b["judge_error"] if recall_b is not None else None),
            _count_change,
        ),
    ]
    for key, what in (("recall_by_severity", "severity"), ("recall_by_category", "category")):
        blocks_a, blocks_b = a.get(key) or {}, b.get(key) or {}
        rows += [
            _metric_row(
                f"Recall, {what} `{_cell(name)}`", _recall_side(blocks_a.get(name)), _recall_side(blocks_b.get(name)),
                _points_change,
            )
            for name in sorted(blocks_a.keys() | blocks_b.keys())
        ]
    rows += [
        _metric_row(
            "Recall, accepted labels", _recall_side(a.get("recall_accepted")), _recall_side(b.get("recall_accepted")),
            _points_change,
        ),
        _metric_row(
            "Unmatched AI per PR", _unmatched_side(a.get("unmatched_ai")), _unmatched_side(b.get("unmatched_ai")),
            _per_pr_change,
        ),
        _metric_row(
            "Strict precision", _precision_side(a.get("precision"), "strict"),
            _precision_side(b.get("precision"), "strict"), _points_change,
        ),
        _metric_row(
            "Lenient precision", _precision_side(a.get("precision"), "lenient"),
            _precision_side(b.get("precision"), "lenient"), _points_change,
        ),
        _metric_row(
            "Severity agreement", _agreement_side(a.get("severity_agreement")),
            _agreement_side(b.get("severity_agreement")), _points_change,
        ),
        _metric_row(
            "Chunks failed", _total_side(a.get("chunks_failed"), str), _total_side(b.get("chunks_failed"), str),
            _count_change,
        ),
        _metric_row(
            "Elapsed", _total_side(a.get("elapsed_ms"), _seconds), _total_side(b.get("elapsed_ms"), _seconds),
            _seconds_change,
        ),
        _metric_row(
            "Review cost", _review_cost_side(a.get("review_cost")), _review_cost_side(b.get("review_cost")),
            _usd_change,
        ),
        _metric_row(
            "Judge cost", _judge_cost_side(score_a.get("judge")), _judge_cost_side(score_b.get("judge")),
            _usd_change,
        ),
    ]
    return ["| Metric | A | B | Change |", "|---|---:|---:|---:|", *rows]


def _labels(score: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    return {(row["case_id"], str(entry["human_id"])): entry for row in score["cases"] for entry in row["findings"]}


def _label_location(entry: Mapping[str, Any]) -> str:
    file, line = entry.get("file"), entry.get("line")
    text = "unknown" if file is None else str(file)
    return text if line is None else f"{text}:{line}"


def _grade_text(entry: Mapping[str, Any]) -> str:
    if entry["credit"] is None:
        return JUDGE_ERROR
    return f"{entry['grade']} ({_num(entry['credit'])})"


def _changed_labels(score_a: Mapping[str, Any], score_b: Mapping[str, Any]) -> list[str]:
    """The ``Changed labels`` table: labels in both runs whose grade or credit differs."""
    labels_a, labels_b = _labels(score_a), _labels(score_b)
    changed = [
        key for key in sorted(labels_a.keys() & labels_b.keys())
        if (labels_a[key]["grade"], labels_a[key]["credit"]) != (labels_b[key]["grade"], labels_b[key]["credit"])
    ]
    if not changed:
        return ["None."]
    lines = ["| Case | Label | Location | A | B |", "|---|---|---|---|---|"]
    for key in changed:
        entry_a, entry_b = labels_a[key], labels_b[key]
        location_a, location_b = _label_location(entry_a), _label_location(entry_b)
        location = location_a if location_a == location_b else f"{location_a} (B: {location_b})"
        lines.append(
            f"| {_cell(key[0])} | {_cell(key[1])} | {_cell(location)} | {_grade_text(entry_a)} | "
            f"{_grade_text(entry_b)} |"
        )
    return lines


def _only_in_one_run(score_a: Mapping[str, Any], score_b: Mapping[str, Any]) -> list[str]:
    """The ``Only in one run`` bullets: cases, then labels of shared cases, that one run lacks."""
    scores = {"A": score_a, "B": score_b}
    rows = {side: {row["case_id"]: row for row in score["cases"]} for side, score in scores.items()}
    labels = {side: _labels(score) for side, score in scores.items()}
    entries: list[tuple[str, str, str, str]] = []
    for side, other in (("A", "B"), ("B", "A")):
        for case_id in sorted(rows[side].keys() - rows[other].keys()):
            count = len(rows[side][case_id]["findings"])
            entries.append((
                case_id, "", side,
                f"- Case `{case_id}`: only in {side} ({count} label{'' if count == 1 else 's'})",
            ))
    for side, other in (("A", "B"), ("B", "A")):
        for case_id, label_id in sorted(labels[side].keys() - labels[other].keys()):
            if case_id in rows[other]:
                location = _label_location(labels[side][(case_id, label_id)])
                entries.append((
                    case_id, label_id, side,
                    f"- Label `{label_id}` of case `{case_id}` at `{location}`: only in {side}",
                ))
    return [text for *_, text in sorted(entries)] or ["None."]


VERDICT_BETTER = "better"
VERDICT_WORSE = "worse"
VERDICT_NOISE = "within noise"
DECISION_ADOPT = "adopt"
DECISION_HOLD = "do not adopt"
GUARD_STRICT = "strict_precision"
GUARD_UNMATCHED = "unmatched_per_pr"
ROLE_RULES = "rules"
ROLE_NO_RULES = "no-rules"
VERDICT_JSON_VERSION = 1
STRICT_MISSING_NOTE = "strict precision is missing from at least one run, so the guard is unmatched AI findings per PR"


@dataclass(frozen=True)
class _VerdictRun:
    """One run of ``eval verdict``: its side, the run as given, and its gated values."""

    side: str
    given: str
    gate: float
    recall: float
    unmatched: float
    strict: float | None = None
    path: str = ""


@dataclass(frozen=True)
class _VerdictArm:
    """The runs of one arm (``name`` and ``role`` are ``None`` in runs mode) and the verdict reached on them."""

    name: str | None
    role: str | None
    runs: tuple[_VerdictRun, ...]
    verdict: str = ""
    reason: str = ""

    @property
    def base(self) -> list[_VerdictRun]:
        return [run for run in self.runs if run.side == "baseline"]

    @property
    def cand(self) -> list[_VerdictRun]:
        return [run for run in self.runs if run.side == "candidate"]

    @property
    def strict_guard(self) -> bool:
        return all(run.strict is not None for run in self.runs)


def eval_verdict(args: argparse.Namespace) -> int:
    """Decide whether the candidate runs beat the baseline runs by more than their run-to-run noise.

    Reads ``args.baseline`` and ``args.candidate`` (one or more scored runs
    each, resolved as ``eval compare`` resolves ``A`` and ``B``),
    ``args.severity``, ``args.out`` and ``args.json_path``. The runs of one
    side are repeats of one configuration: their spread is the noise a single
    run carries.

    The gate is the recall of the human labels of severity ``--severity``
    (``recall_by_severity``), or the micro recall without it. The candidate is:

    - ``better`` when its mean gate is above the best baseline run's gate,
      its mean micro recall is not below the worst baseline run's, and the
      guard holds;
    - ``worse`` when its mean gate is below the worst baseline run's gate;
    - ``within noise`` otherwise.

    The guard is strict precision (``metrics.precision.strict``: the mean
    must not be below the worst baseline run's) when every run of the
    comparison has it, and otherwise the mean unmatched AI findings per PR,
    which must not be above the worst baseline run's; the report then says
    strict precision is missing.

    When ``--baseline`` and ``--candidate`` are each one directory holding a
    ``campaign.json``, the arms of the two campaigns are compared one by one
    (each arm's repeats are its ``runs/<arm>/r<k>`` that hold a
    ``score.json``) and the decision is ``adopt`` only when every arm that
    uses rules is ``better`` and no arm without rules is ``worse``; with no
    rules arm, only when every arm is ``better``.

    Standard output is Markdown: ``# prxref eval verdict``, the inputs, a
    ``## Runs`` table, a ``## Summary`` table of each side's mean and range,
    and ``## Verdict`` (campaign mode: one such block per ``## Arm``, then
    ``## Decision``). It holds no timestamp and no path other than the runs
    as given. ``eval compare``'s judge and case warnings are logged for every
    run against the first baseline run. ``--json PATH`` also writes the
    verdict as ``verdict.json``.

    Returns 0 when the candidate is ``better`` (campaign: the decision is
    ``adopt``) and 1 otherwise, so a CI job can gate an upgrade on it. Raises
    ``ConfigError`` (exit 2) for any run ``eval compare`` refuses, for a run
    with no scored label of the gated severity or no case, for a campaign
    mixed with runs, an arm only one campaign has, an arm with no scored run,
    a ``campaign.json`` that cannot be read, and a ``--json`` path that
    cannot be written.
    """
    campaign = _campaign_sides(args)
    if campaign is None:
        arms = [_runs_arm(args)]
    else:
        arms = _campaign_arms(args, *campaign)
    arms = [_judged(arm) for arm in arms]
    mode = "runs" if campaign is None else "campaign"
    if campaign is None:
        decision, decision_reason = (arms[0].verdict == VERDICT_BETTER), ""
        text = _verdict_text(arms[0].runs, args.severity)[1]
    else:
        decision, decision_reason = _decide_campaign(arms)
        text = _campaign_text(args, arms, decision, decision_reason)
    print(text, end="", flush=True)
    exit_code = 0 if decision else 1
    if getattr(args, "json_path", None):
        _write_verdict_json(args.json_path, mode, args.severity, arms, decision, exit_code)
    return exit_code


def _judged(arm: _VerdictArm) -> _VerdictArm:
    """The arm with its verdict and reason decided."""
    verdict, reason = _decide(arm.base, arm.cand)
    return replace(arm, verdict=verdict, reason=reason)


def _runs_arm(args: argparse.Namespace) -> _VerdictArm:
    """Read the runs given on the command line as the one arm of runs mode."""
    baseline = [_read_scored_run(f"--baseline {given}", given, args.out) for given in args.baseline]
    candidate = [_read_scored_run(f"--candidate {given}", given, args.out) for given in args.candidate]
    return _arm_from_scored(None, None, baseline, candidate, args.severity)


def _arm_from_scored(name: str | None, role: str | None, baseline: Sequence[_ScoredRun],
                     candidate: Sequence[_ScoredRun], severity: str | None) -> _VerdictArm:
    first = baseline[0]
    for run in [*baseline[1:], *candidate]:
        _warn_judge(first.score, run.score)
        _warn_cases(first.score, run.score)
    runs = [_verdict_run("baseline", run, severity) for run in baseline]
    runs += [_verdict_run("candidate", run, severity) for run in candidate]
    return _VerdictArm(name, role, tuple(runs))


def _campaign_dir(given: str, out: str) -> Path | None:
    """The campaign directory ``given`` names (a label under ``out`` or a path), or ``None``."""
    label_dir = Path(out) / given
    directory = label_dir if is_safe_id(given) and label_dir.is_dir() else Path(given)
    return directory if (directory / "campaign.json").is_file() else None


def _campaign_sides(args: argparse.Namespace) -> tuple[Path, Path] | None:
    """The two campaign directories, ``None`` for runs mode; mixing the two modes is a ``ConfigError``."""
    sides = (("--baseline", args.baseline), ("--candidate", args.candidate))
    found = {flag: [_campaign_dir(given, args.out) for given in given_list] for flag, given_list in sides}
    if not any(directory for directories in found.values() for directory in directories):
        return None
    for flag, given_list in sides:
        if len(given_list) != 1 or found[flag][0] is None:
            raise ConfigError(
                f"{flag}: a campaign directory (one holding campaign.json) must be the only value of both "
                "--baseline and --candidate; the two modes cannot be mixed"
            )
    return found["--baseline"][0], found["--candidate"][0]  # type: ignore[return-value]


def _read_campaign(flag: str, given: str, directory: Path) -> dict[str, dict[str, Any]]:
    """The arms of one ``campaign.json`` by name, in file order."""
    path = directory / "campaign.json"
    data = _read_compare_json(f"{flag} {given}", path)
    listed = data.get("arms") if isinstance(data, dict) else None
    if not isinstance(listed, list) or not listed:
        raise ConfigError(f"{flag} {given}: {path}: not a campaign.json (it lists no arms)")
    arms: dict[str, dict[str, Any]] = {}
    for arm in listed:
        name = arm.get("name") if isinstance(arm, dict) else None
        if not isinstance(name, str) or not is_safe_id(name) or name in arms:
            raise ConfigError(f"{flag} {given}: {path}: not a campaign.json (arm {name!r} is not a unique safe id)")
        arms[name] = arm
    return arms


def _campaign_role(arm: Mapping[str, Any]) -> str:
    """``no-rules`` for an arm that turns rules off and mines none, else ``rules``."""
    bare = arm.get("rules_file") == "" and not arm.get("scoped_rules") and not arm.get("mine_rules")
    return ROLE_NO_RULES if bare else ROLE_RULES


def _arm_scored_runs(flag: str, given: str, directory: Path, name: str, out: str) -> list[_ScoredRun]:
    """The scored ``runs/<name>/r<k>`` of a campaign, ordered by ``k``, each named ``r<k>``."""
    root = directory / "runs" / name
    found: list[tuple[int, Path]] = []
    if root.is_dir():
        for child in root.iterdir():
            match = re.fullmatch(r"r(\d+)", child.name)
            if match and (child / "score.json").is_file():
                found.append((int(match[1]), child))
    if not found:
        raise ConfigError(f"{flag} {given}: arm {name!r} has no scored run (no runs/{name}/r<k>/score.json)")
    runs = []
    for k, child in sorted(found):
        run = _read_scored_run(f"{flag} {given}: arm {name!r} r{k}", str(child), out)
        runs.append(replace(run, given=f"r{k}"))
    return runs


def _campaign_arms(args: argparse.Namespace, base_dir: Path, cand_dir: Path) -> list[_VerdictArm]:
    """Read both campaigns and pair their arms by name."""
    base_given, cand_given = args.baseline[0], args.candidate[0]
    base_arms = _read_campaign("--baseline", base_given, base_dir)
    cand_arms = _read_campaign("--candidate", cand_given, cand_dir)
    for name in cand_arms.keys() - base_arms.keys():
        raise ConfigError(f"--candidate {cand_given}: arm {name!r} is not in the --baseline campaign {base_given}")
    for name in base_arms.keys() - cand_arms.keys():
        raise ConfigError(f"--candidate {cand_given}: it has no arm {name!r}, which the --baseline campaign has")
    arms = []
    for name, arm in cand_arms.items():
        baseline = _arm_scored_runs("--baseline", base_given, base_dir, name, args.out)
        candidate = _arm_scored_runs("--candidate", cand_given, cand_dir, name, args.out)
        arms.append(_arm_from_scored(name, _campaign_role(arm), baseline, candidate, args.severity))
    return arms


def _decide_campaign(arms: Sequence[_VerdictArm]) -> tuple[bool, str]:
    """Whether to adopt the candidate campaign, and the one sentence that explains it."""
    if any(arm.role == ROLE_RULES for arm in arms):
        blockers = [f"rules arm `{arm.name}` is {arm.verdict}" for arm in arms
                    if arm.role == ROLE_RULES and arm.verdict != VERDICT_BETTER]
        blockers += [f"no-rules arm `{arm.name}` is {arm.verdict}" for arm in arms
                     if arm.role == ROLE_NO_RULES and arm.verdict == VERDICT_WORSE]
        adopt = "every rules arm is better and no no-rules arm is worse."
    else:
        blockers = [f"arm `{arm.name}` is {arm.verdict}" for arm in arms if arm.verdict != VERDICT_BETTER]
        adopt = "no arm uses rules and every arm is better."
    if blockers:
        return False, f"{'; '.join(blockers)}."
    return True, adopt


def _stats(values: Sequence[float]) -> dict[str, Any]:
    return {"mean": _mean(values), "min": min(values), "max": max(values), "values": list(values)}


def _side_json(runs: Sequence[_VerdictRun]) -> dict[str, Any]:
    strict = [run.strict for run in runs]
    return {
        "runs": [run.path for run in runs],
        "gate": _stats([run.gate for run in runs]),
        "recall": _stats([run.recall for run in runs]),
        "precision": None if any(value is None for value in strict) else _stats(strict),  # type: ignore[arg-type]
        "unmatched_per_pr": _stats([run.unmatched for run in runs]),
    }


def _write_verdict_json(path: str, mode: str, severity: str | None, arms: Sequence[_VerdictArm], decision: bool,
                        exit_code: int) -> None:
    """Write ``verdict.json`` atomically; an unwritable path is a ``ConfigError`` naming ``--json``."""
    guard = GUARD_STRICT if all(arm.strict_guard for arm in arms) else GUARD_UNMATCHED
    payload = {
        "version": VERDICT_JSON_VERSION, "mode": mode, "severity": severity, "guard": guard,
        "decision": DECISION_ADOPT if decision else DECISION_HOLD, "exit_code": exit_code,
        "arms": [
            {"arm": arm.name, "role": arm.role, "verdict": arm.verdict, "reason": arm.reason,
             "baseline": _side_json(arm.base), "candidate": _side_json(arm.cand)}
            for arm in arms
        ],
    }
    target = Path(path)
    temp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        raise ConfigError(f"--json {path}: cannot write {target}: {exc}") from exc


def eval_dashboard(args: argparse.Namespace) -> int:
    """Show the live progress of a campaign directory, as a table or a local web page.

    Reads ``args.campaign``, ``args.host``, ``args.port``, ``args.once`` and
    ``args.tail``. With ``--once`` it prints one status table and returns 0;
    otherwise it serves the page until interrupted and returns 0. The work is in
    :mod:`prxref.eval_dashboard`, imported here so the web server code is only
    loaded by this action. Raises ``ConfigError`` (exit 2) for a campaign with
    no ``progress.json`` and for a port already in use.
    """
    from prxref import eval_dashboard as dashboard

    return dashboard.run(args)


def eval_mine(args: argparse.Namespace) -> int:
    """Build an eval dataset from a GitHub repo's merged PRs, or re-hash one after a human edit.

    Reads ``args.repo``, ``args.out``, ``args.host``, ``args.since``,
    ``args.prs``, ``args.judge_model``, ``args.min_comments``, ``args.rehash``
    and ``args.allow_unconfirmed``. The work is in :mod:`prxref.eval_mine`,
    imported here so that no other action pays for it.
    """
    from prxref import eval_mine as _eval_mine

    return _eval_mine.mine(args)


def _verdict_run(side: str, run: _ScoredRun, severity: str | None) -> _VerdictRun:
    """Read the gate, the micro recall, the unmatched AI per PR and the strict precision of one run."""
    metrics = run.score["metrics"]
    recall = (metrics.get("recall") or {}).get("recall")
    gate = recall if severity is None else ((metrics.get("recall_by_severity") or {}).get(severity) or {}).get("recall")
    unmatched = (metrics.get("unmatched_ai") or {}).get("per_pr")
    precision = metrics.get("precision")
    strict = precision.get("strict") if isinstance(precision, dict) else None
    if gate is None or recall is None:
        what = "no scored label" if severity is None else f"no scored label of severity {severity!r}"
        raise ConfigError(f"{run.argument}: the run has {what}, so it cannot be gated")
    if unmatched is None:
        raise ConfigError(f"{run.argument}: the run has no case, so it cannot be gated")
    return _VerdictRun(side, run.given, gate, recall, unmatched, strict, str(run.run_dir))


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _spread(values: Sequence[float], render: Callable[[float], str]) -> str:
    return f"{render(_mean(values))} ({render(min(values))} to {render(max(values))})"


def _per_pr(value: float) -> str:
    return f"{value:.2f}"


def _decide(base: Sequence[_VerdictRun], cand: Sequence[_VerdictRun]) -> tuple[str, str]:
    """The verdict and the one sentence that explains it."""
    strict = all(run.strict is not None for run in [*base, *cand])
    gate, recall, unmatched = (_mean([run.gate for run in cand]), _mean([run.recall for run in cand]),
                               _mean([run.unmatched for run in cand]))
    best, worst = max(run.gate for run in base), min(run.gate for run in base)
    floor, ceiling = min(run.recall for run in base), max(run.unmatched for run in base)
    if gate < worst:
        return VERDICT_WORSE, f"the candidate's mean gate {_pct(gate)} is below the worst baseline run's {_pct(worst)}."
    if gate <= best:
        return VERDICT_NOISE, (f"the candidate's mean gate {_pct(gate)} is not above the best baseline run's "
                               f"{_pct(best)}.")
    if recall < floor:
        return VERDICT_NOISE, (f"the gate rose, but the mean micro recall {_pct(recall)} is below the worst baseline "
                               f"run's {_pct(floor)}.")
    if strict:
        precision = _mean([run.strict for run in cand])  # type: ignore[misc]
        lowest = min(run.strict for run in base)  # type: ignore[type-var]
        if precision < lowest:
            return VERDICT_NOISE, (f"the gate rose, but the mean strict precision {_pct(precision)} is below the "
                                   f"worst baseline run's {_pct(lowest)}.")
        return VERDICT_BETTER, (f"the candidate's mean gate {_pct(gate)} is above the best baseline run's "
                                f"{_pct(best)}, with micro recall and strict precision no worse than the worst "
                                "baseline run.")
    if unmatched > ceiling:
        return VERDICT_NOISE, (f"the gate rose, but the mean unmatched AI findings per PR {_per_pr(unmatched)} is "
                               f"above the worst baseline run's {_per_pr(ceiling)}.")
    return VERDICT_BETTER, (f"the candidate's mean gate {_pct(gate)} is above the best baseline run's {_pct(best)}, "
                            "with micro recall and unmatched AI findings per PR no worse than the worst baseline run.")


def _verdict_blocks(runs: Sequence[_VerdictRun], heading: str) -> tuple[str, list[str]]:
    """The verdict and the Runs, Summary and Verdict blocks of one arm, their headings at level ``heading``."""
    base = [run for run in runs if run.side == "baseline"]
    cand = [run for run in runs if run.side == "candidate"]
    strict = all(run.strict is not None for run in runs)
    head = ("| Run | Side | Gate | Recall (micro) |" + (" Strict precision |" if strict else "")
            + " Unmatched AI per PR |")
    rule = "|---|---|---:|---:|" + ("---:|" if strict else "") + "---:|"
    lines = [
        f"{heading} Runs", "", head, rule,
        *(f"| {_cell(run.given)} | {run.side} | {_pct(run.gate)} | {_pct(run.recall)} | "
          + (f"{_pct(run.strict)} | " if strict else "") + f"{_per_pr(run.unmatched)} |" for run in runs),
        "", f"{heading} Summary", "",
        "| Metric | Baseline mean (range) | Candidate mean (range) | Change |", "|---|---|---|---:|",
    ]
    rows = [("Gate", "gate", _pct, _points_change), ("Recall (micro)", "recall", _pct, _points_change)]
    if strict:
        rows.append(("Strict precision", "strict", _pct, _points_change))
    rows.append(("Unmatched AI per PR", "unmatched", _per_pr, _per_pr_change))
    for name, key, render, change in rows:
        values_b = [getattr(run, key) for run in base]
        values_c = [getattr(run, key) for run in cand]
        lines.append(f"| {name} | {_spread(values_b, render)} | {_spread(values_c, render)} | "
                     f"{change(_mean(values_c) - _mean(values_b))} |")
    verdict, reason = _decide(base, cand)
    lines += ["", f"{heading} Verdict", "", f"**{verdict}**: {reason}"]
    if not strict:
        lines += ["", STRICT_MISSING_NOTE]
    if len(base) < 2:
        lines += ["", "The baseline has one run, so it has no noise range. Repeat it before adopting the candidate."]
    return verdict, lines


def _gate_name(severity: str | None) -> str:
    return "Recall (micro)" if severity is None else f"Recall, severity `{_cell(severity)}`"


def _verdict_text(runs: Sequence[_VerdictRun], severity: str | None) -> tuple[str, str]:
    """The verdict and the Markdown printed to standard output in runs mode."""
    base = [run for run in runs if run.side == "baseline"]
    cand = [run for run in runs if run.side == "candidate"]
    lines = [
        "# prxref eval verdict", "",
        f"- Baseline: {', '.join(run.given for run in base)}",
        f"- Candidate: {', '.join(run.given for run in cand)}",
        f"- Gate: {_gate_name(severity)}", "",
    ]
    verdict, blocks = _verdict_blocks(runs, "##")
    return verdict, "\n".join(lines + blocks) + "\n"


def _campaign_text(args: argparse.Namespace, arms: Sequence[_VerdictArm], decision: bool, reason: str) -> str:
    """The Markdown printed to standard output in campaign mode."""
    lines = [
        "# prxref eval verdict", "",
        f"- Baseline: {args.baseline[0]}", f"- Candidate: {args.candidate[0]}",
        f"- Gate: {_gate_name(args.severity)}",
    ]
    for arm in arms:
        lines += ["", f"## Arm {_cell(arm.name)}", "", f"Role: {arm.role}", ""]
        lines += _verdict_blocks(arm.runs, "###")[1]
    lines += ["", "## Decision", "", f"**{DECISION_ADOPT if decision else DECISION_HOLD}**: {reason}"]
    return "\n".join(lines) + "\n"
