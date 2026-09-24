"""The ``prxref eval`` actions: replay labelled cases, score them, compare runs.

``prxref.cli`` routes ``eval run``, ``eval score`` and ``eval compare`` to
:func:`eval_run`, :func:`eval_score` and :func:`eval_compare`. Each takes the
parsed ``argparse.Namespace`` and returns the process exit code;
:func:`eval_run` also takes the review runner and the record builder, which
the CLI passes in. A configuration problem raises ``ConfigError`` naming the
flag or argument that supplied it, and the CLI prints it as
``configuration error: ...`` and exits 2, as it does for ``review``.

The CLI imports this module lazily, inside its ``eval`` handler. This module
must never import ``prxref.cli``: that import would close a cycle.

``eval`` adds no environment variable; every setting is a flag. Every LLM
call it makes is single-shot, and it never posts to a forge.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prxref import reviewer
from prxref.config import load_config
from prxref.eval_cases import EvalCase, case_to_json, is_safe_id, load_cases
from prxref.llm import ConfigError

logger = logging.getLogger(__name__)

RUN_VERSION = 1
RUN_PROMPTS = ("worker", "systemic", "summary")
RUN_CONFIG_KEYS = (
    "llm_backend",
    "llm_models",
    "llm_max_tokens",
    "max_chunks",
    "chunk_token_budget",
    "chunk_max_files",
    "dedup_similarity",
    "group_findings",
    "max_warning_findings",
    "max_outofscope_findings",
    "scoped_rules_max_chars",
)


def eval_run(
    args: argparse.Namespace,
    *,
    run_review: Callable[..., Any],
    build_record: Callable[[Any], dict],
) -> int:
    """Replay every case of ``--cases`` and write one labelled run under ``--out``.

    Reads ``args.cases``, ``args.label``, ``args.out``, ``args.rules_file`` and
    ``args.resume``. ``run_review`` is the CLI's ``_run_review`` and
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
    - an existing ``<out>/<label>`` without ``--resume``, naming ``--label``;
    - a run directory that cannot be created, naming ``--out``.

    Each case is reviewed in process by ``run_review`` with ``post=False``
    and ``no_threads=True``, so nothing is ever posted. ``context_file`` is
    ``""`` and ``spec_sources`` is ``[]`` unless the case sets them, so the
    environment's ticket and spec inputs cannot leak into a case.
    ``args.rules_file`` applies to every case, as ``review --rules-file`` does.
    A case with a ``pr_url`` gets the replay pinning ``run_review`` applies by
    default; nothing extra is passed.

    Each case is fenced. A crash, a ``None`` result (an unrecognised URL) or
    any other exception is recorded as that case's ``error.json`` and the next
    case still runs. That includes a ``ConfigError`` the review raises for one
    case (an unreadable ``diff_file``, or an unusable ``--rules-file``, which
    is read per case): it is a failed case, not exit 2. A review whose verdict
    is ``Error`` is recorded as a normal ``record.json`` carrying that verdict.
    The run returns 0 whatever the cases did.

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
    ``label``; ``cases_path`` (``--cases`` as given); ``created_at`` (this
    invocation's start, ISO-8601 UTC with a ``Z``); ``case_ids`` in dataset
    order; ``prompts``, holding ``sha256`` (the sha256 of each packaged
    template of :data:`RUN_PROMPTS`, as ``reviewer.load_prompt`` reads it)
    and ``prompt_templates``; ``sampling`` and ``review_rules``; and
    ``config``, the keys of :data:`RUN_CONFIG_KEYS` from ``load_config``.
    ``config`` is an allowlist, so no credential is ever written.
    ``prompt_templates``, ``sampling`` and ``review_rules`` are copied from
    the first case, in dataset order, whose ``record.json`` has a verdict
    other than ``Error``; each is ``null`` when there is no such case or the
    record lacks the key.

    With ``--resume`` an existing run directory is continued: every case that
    already has a ``record.json`` or an ``error.json`` is skipped, and
    ``run.json`` is rewritten from the records on disk.

    Standard output gets one line per case as it finishes, ``<id>: <verdict>
    (<n> active findings)`` (``finding`` when ``<n>`` is 1),
    ``<id>: failed: <error>`` or ``<id>: skipped (already recorded)``, then
    ``run directory: <out>/<label>``. Nothing is logged at WARNING or above
    for a case that runs normally.
    """
    if not is_safe_id(args.label):
        raise ConfigError(
            "--label: must be one directory name of letters, digits, '.', '_' and '-' "
            f"that starts with a letter or digit, got {args.label!r}"
        )
    cases = load_cases(args.cases, source="--cases")
    cfg = load_config()
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
            run_review=run_review, build_record=build_record,
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
) -> str:
    """Run one fenced case into ``case_dir`` and return its stdout line."""
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
            trace_dir=str(trace_dir),
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
    """Build ``run.json`` from the run's inputs and the records on disk."""
    case_ids = [case.id for case in cases]
    first = _first_reviewed_record(cases_dir, case_ids) or {}
    return {
        "version": RUN_VERSION,
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

    Reads ``args.label``, ``args.out`` and ``args.judge_model``.

    - A label with ``must_match`` is graded deterministically: same file,
      ``|line delta| <= 5`` and the predicate. It makes no LLM call.
    - Every other label goes to one single-shot judge call per case
      (``json_mode``) that returns ``{human_id, grade, ai_ref}`` with a grade
      of ``match``, ``partial`` (half credit) or ``none``. The code checks
      that ``ai_ref`` names a real AI finding in the same file, and one AI
      finding credits at most two human labels; a grouped finding is
      credited per location.
    - ``--judge-model`` is required whenever any label lacks ``must_match``.
      Leaving it out raises ``ConfigError`` naming ``--judge-model``.
    - The judge runs on the review's own backend, base URL and key; only the
      model changes. A judge model among the reviewer's ``sampling.models``
      logs a WARNING and stamps ``self_judged``; it is not an error. The
      judge prompt is packaged and never overridable.
    - Grades are cached on the sha256 of the judge prompt sha, the model, the
      labels and the AI findings. A failed judge call counts as
      ``judge_error``, never as a grade of ``none``.
    - Writes ``score.json`` and ``score.md``: recall overall (micro, with
      per-case rows), by severity, by category and over accepted labels;
      unmatched AI findings per PR; severity agreement, where a human
      ``minor`` counts as ``warning``; ``chunks_failed``; ``elapsed_ms``; and
      review and judge cost, where a ``None`` cost is never summed.
      ``score.json`` stamps the judge's ``sampling``, the judge prompt
      version and the template sha256.
    """
    raise NotImplementedError("prxref eval score is not built yet (seat E14-G)")


def eval_compare(args: argparse.Namespace) -> int:
    """Print two scored runs side by side, then every label whose credit changed.

    Reads ``args.run_a``, ``args.run_b`` and ``args.out``. Each of ``run_a``
    and ``run_b`` is a label under ``--out`` or a run directory.

    - The metrics come first, side by side, then every human label whose
      credit changed between the two runs, sorted and without timestamps, so
      comparing the same two runs twice prints byte-identical output.
    - A WARNING says so when the two runs' judge prompt sha or judge model
      differ.
    - A run that is not there exits 2 naming it, as ``trace render`` does for
      a missing trace.
    """
    raise NotImplementedError("prxref eval compare is not built yet (seat E14-H)")
