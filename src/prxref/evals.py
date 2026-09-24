"""The ``prxref eval`` actions: replay labelled cases, score them, compare runs.

``prxref.cli`` routes ``eval run``, ``eval score`` and ``eval compare`` to
:func:`eval_run`, :func:`eval_score` and :func:`eval_compare`. Each takes the
parsed ``argparse.Namespace`` and returns the process exit code. A
configuration problem raises ``ConfigError`` naming the flag or argument that
supplied it, and the CLI prints it as ``configuration error: ...`` and exits
2, as it does for ``review``.

The CLI imports this module lazily, inside its ``eval`` handler. This module
must never import ``prxref.cli``: that import would close a cycle.

``eval`` adds no environment variable; every setting is a flag. Every LLM
call it makes is single-shot, and it never posts to a forge.
"""
from __future__ import annotations

import argparse


def eval_run(args: argparse.Namespace) -> int:
    """Replay every case of ``--cases`` and write one labelled run under ``--out``.

    Reads ``args.cases``, ``args.label``, ``args.out``, ``args.rules_file`` and
    ``args.resume``.

    - ``args.cases`` is loaded with ``prxref.eval_cases.load_cases``. A bad
      case raises ``ConfigError`` naming ``--cases``, the case id and the
      field, so the command exits 2 before any case runs.
    - Each case is reviewed in process with ``post=False`` and
      ``no_threads=True``, so nothing is ever posted. ``context_file`` is
      ``""`` and ``spec_sources`` is ``[]`` unless the case sets them, so the
      environment's ticket and spec inputs cannot leak into a case.
      ``args.rules_file`` applies to every case, as ``review --rules-file``
      does.
    - The run is written to ``<out>/<label>/``: ``cases/<id>/record.json``
      (the review's JSON record) and ``cases/<id>/trace/`` per case, and
      ``run.json`` holding the prxref version, the prompt sha256s, the
      ``sampling`` and ``review_rules`` records, and an allowlist of
      non-secret config keys, so no credential is ever written.
    - Each case is fenced: a crash or an ``Error`` verdict is recorded for
      that case, the next case still runs, and the run returns 0.
    - An existing ``<out>/<label>`` raises ``ConfigError`` naming ``--label``
      unless ``args.resume`` is set, which continues that run.
    """
    raise NotImplementedError("prxref eval run is not built yet (seat E14-C)")


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
