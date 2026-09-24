"""prxref command-line interface.

Provides these subcommands:
  * ``review --pr-url URL`` — one-shot PR/MR review from a Bitbucket, GitHub,
    GitLab, or Azure DevOps URL (Cloud or self-hosted).
  * ``serve [--port N] [--host H]`` — webhook listener daemon.
  * ``eval run|score|compare`` — replay labelled cases, score the findings
    against the human labels, and compare two scored runs (``prxref.evals``).
  * ``trace render FILE`` — a JSONL run trace to a standalone HTML view.
  * ``prompts export DIR [--force]`` — the packaged prompt templates, written
    to ``DIR`` as the starting point for a ``PRXREF_PROMPTS_DIR`` override.

``review`` takes four optional inputs besides the PR itself, and they
compose: ``--spec URL_OR_PATH`` (repeatable) grounds the review against specs
or tickets and replaces ``PRXREF_SPEC_SOURCES``; ``--rules-file PATH`` adds a
team review-rules file (``PRXREF_REVIEW_RULES``) that reaches every review
unit; ``--scoped-rules PATH`` (repeatable) adds path-scoped rules files and
directories and replaces ``PRXREF_SCOPED_RULES``, each file reaching only
the chunks its ``applies_to:`` globs match, and the sweep their union;
``--context-file PATH`` names the ticket the PR implements
(``PRXREF_TICKET_CONTEXT_FILE``), so each finding is marked in, out of, or
of unknown ticket scope. Each flag wins over its variable, and
``--rules-file ""`` / ``--scoped-rules ""`` / ``--context-file ""`` turn the
variable off for one run. The rules and ticket files are read before any
network call, so an unusable one is a configuration error. The webhook
daemon reads both kinds of rules file from its own environment, re-reading
them on every webhook, and never reads a ticket-context file.

``--prompts-dir DIR`` (``PRXREF_PROMPTS_DIR``) replaces the packaged
``worker.md``, ``systemic.md`` and ``summary.md`` prompt templates with the
ones in ``DIR``, the flag winning and ``--prompts-dir ""`` turning the
variable off for one run. The directory is loaded and validated before any
network call, so a bad one exits 2 naming its source; the webhook daemon
reads it from its own environment on every webhook. The run record's
``prompt_templates`` stamps each override's sha256.

The replay flags review a pinned, reproducible input for evaluation:
``--base-sha`` / ``--head-sha`` a commit range in the ``--pr-url``
repository, ``--diff-file PATH`` a diff on disk (``--pr-url`` is then
optional, and no forge is contacted without it), and ``--no-threads`` hides
the PR's existing threads. Any replay flag makes the run a replay: it never
posts, and its run record gains a ``replay`` stamp. They are validated
before the URL is parsed, and a bad set exits 2 naming the flag. The webhook
daemon never replays.

A ``--pr-url`` replay also pins the PR's title and description by default
(issue #16): it shows the ones in force at a cutoff, which is ``--as-of
TIME`` when given, else the PR's first human review, else its head commit's
date. Reading that history is one call to the forge's history reader, made
before the review starts and not recorded in the run trace. A forge with no
history reader, a read that fails, or a history that does not reach the
cutoff keeps the current title and description and logs a WARNING, except
that an explicit ``--as-of`` on a forge with no history reader exits 2.
``--description-file PATH`` (that file's text) and ``--no-description`` (an
empty description) read no history and leave the title current. At most one
of the three flags may be given; each is CLI-only, with no environment
variable, and each makes the run a replay. The stamp's ``description``
(``pinned``, ``live``, ``file`` or ``none``), ``as_of`` and ``as_of_source``
record which title and description the review saw.

Non-blocking doctrine: ``review`` exits 0 on all review errors (empty diffs,
network failures, LLM timeouts, bad credentials), printing diagnostic notes to
stderr so a pipeline step never fails the build over an advisor's error. The one
exception is a configuration error — a required value missing, or one that is
malformed, out of range, or outside its key's allowed vocabulary — which is a
usage error rather than a review outcome and exits 2. Both kinds raise
``ConfigError`` and name whichever input supplied the offending value
(``--max-chunks`` when the flag was the source).

``PRXREF_FAIL_ON`` is the one opt-out of that doctrine. The default ``never``
is the doctrine itself: findings never move the exit code. ``error`` exits 1
when the completed review carries an active error-severity finding; ``any``
exits 1 on any active finding; and under either value a review that does not
complete also exits 1 — it crashes, or it ends with verdict ``Error`` (the
forge could not be read, the diff could not be parsed or chunked, or every
chunk review failed) — because a gate that silently passes on a broken run is
worse than none. An empty PR diff is not a failure: it is reviewed as
``Approved`` and exits 0. An unrecognized PR URL still exits 0 under every
value — nothing was reviewed, so there is no outcome to gate on. The webhook
daemon has no exit code and is unaffected by the knob.

``PRXREF_DRY_RUN=1`` suppresses every write to the forge on both paths — the
one-shot review and the webhook daemon — and ``--no-post`` does the same for a
single invocation.

``--format {text,json}`` controls ``review``'s stdout shape. ``json`` (default
``text``) emits exactly one JSON object and nothing else on stdout. In text
mode, ``--no-post`` or ``-v`` additionally prints every active finding's
location, title, and body, followed by any dropped findings and their reason.
"""
from __future__ import annotations

import argparse
import codecs
import errno
import functools
import importlib
import json
import logging
import os
import re
import stat
import sys
import time
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import prxref
from prxref.config import load_config, make_forge
from prxref.costs import cost_label
from prxref.forges.base import detect_forge
from prxref.forges.replay import DescriptionPin, LocalDiffForge, ReplayForge, choose_cutoff, pin_status
from prxref.llm import ConfigError
from prxref.prompt_templates import export_prompt_templates, load_prompt_templates
from prxref.rules import load_review_rules, load_scoped_rules
from prxref.text_inputs import check_readable_path, decode_text
from prxref.ticket import load_ticket_context
from prxref.triage import SCOPE_IN, SCOPE_OUT, normalize_scope
from prxref.viz import render_file

logger = logging.getLogger("prxref")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prxref",
        description="Fast automated AI code review for Bitbucket, GitLab, GitHub, and Azure DevOps.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print version and exit",
    )
    sub = parser.add_subparsers(dest="command")

    rev = sub.add_parser("review", help="review one PR/MR from its web URL")
    rev.add_argument(
        "--pr-url",
        default=None,
        help=(
            "full URL of the PR or MR on Bitbucket, GitHub, GitLab, or Azure "
            "DevOps (required unless --diff-file is given)"
        ),
    )
    rev.add_argument(
        "--no-post",
        action="store_true",
        help=(
            "dry run: do not post comments to the forge "
            "(PRXREF_DRY_RUN=1 does the same for every run, daemon included)"
        ),
    )
    rev.add_argument(
        "--max-chunks",
        type=int,
        default=None,
        help="override the maximum number of diff chunks to review",
    )
    rev.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="override the per-model request deadline in seconds",
    )
    rev.add_argument(
        "--spec",
        action="append",
        default=None,
        metavar="URL_OR_PATH",
        help=(
            "spec/ticket source to review against; repeatable "
            "(PRXREF_SPEC_SOURCES otherwise)"
        ),
    )
    rev.add_argument(
        "--rules-file",
        default=None,
        metavar="PATH",
        help=(
            "team review rules (Markdown/text) added to every review prompt; "
            "overrides PRXREF_REVIEW_RULES, and '' turns it off for this run; "
            "read it from a trusted checkout, never from the PR under review"
        ),
    )
    rev.add_argument(
        "--scoped-rules",
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "path-scoped team review rules: a rules file, or a directory of "
            "*.md rules files, each reaching only the chunks its applies_to: "
            "globs match; repeatable; replaces PRXREF_SCOPED_RULES, and '' "
            "turns it off for this run; read it from a trusted checkout, never "
            "from the PR under review"
        ),
    )
    rev.add_argument(
        "--context-file",
        default=None,
        metavar="PATH",
        help=(
            "ticket context (plain text/Markdown) the PR is meant to "
            "implement; findings get a scope of in/out/unknown against it; "
            "overrides PRXREF_TICKET_CONTEXT_FILE, and '' turns it off for "
            "this run"
        ),
    )
    rev.add_argument(
        "--prompts-dir",
        default=None,
        metavar="DIR",
        help=(
            "directory of worker.md, systemic.md and summary.md templates "
            "that replace the packaged prompts (start from 'prxref prompts "
            "export DIR'); overrides PRXREF_PROMPTS_DIR, and '' turns it off "
            "for this run; read it from a trusted checkout, never from the PR "
            "under review"
        ),
    )
    rev.add_argument(
        "--base-sha",
        default=None,
        metavar="SHA",
        help=(
            "replay: review the range BASE...HEAD (merge-base diff, like the "
            "PR's own) in the --pr-url repository; needs --head-sha; implies "
            "no posting"
        ),
    )
    rev.add_argument(
        "--head-sha",
        default=None,
        metavar="SHA",
        help=(
            "replay: head commit of the pinned range; file context is read at "
            "this commit; needs --base-sha"
        ),
    )
    rev.add_argument(
        "--no-threads",
        action="store_true",
        help=(
            "replay: hide the PR's existing threads from the prompt and the "
            "thread-dedup passes; implies no posting"
        ),
    )
    rev.add_argument(
        "--diff-file",
        default=None,
        metavar="PATH",
        help=(
            "replay: review this unified diff (git diff or git format-patch "
            "output) instead of fetching one; --pr-url becomes optional; "
            "implies no posting"
        ),
    )
    rev.add_argument(
        "--as-of",
        default=None,
        metavar="TIME",
        help=(
            "replay: show the PR's title and description as they were at TIME, "
            "an ISO-8601 time with a UTC offset (2026-05-01T09:30:00Z); needs "
            "--pr-url; excludes --description-file and --no-description; "
            "implies no posting"
        ),
    )
    rev.add_argument(
        "--description-file",
        default=None,
        metavar="PATH",
        help=(
            "replay: use this UTF-8 text file as the PR description; excludes "
            "--as-of and --no-description; implies no posting"
        ),
    )
    rev.add_argument(
        "--no-description",
        action="store_true",
        help=(
            "replay: review with an empty PR description; excludes --as-of "
            "and --description-file; implies no posting"
        ),
    )
    rev.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print timing, token, and findings breakdown to stdout",
    )
    rev.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help=(
            "output format for review results (default text); json emits "
            "exactly one JSON object on stdout"
        ),
    )
    rev.add_argument(
        "--trace-dir",
        default=None,
        help=(
            "directory for per-chunk prompt/response traces "
            "(chunk0.system.md, chunk0.response.json, ...; PRXREF_TRACE_DIR "
            "does the same for every run and this flag wins when both are set)"
        ),
    )

    srv = sub.add_parser("serve", help="run webhook listener daemon")
    srv.add_argument(
        "--port",
        type=int,
        default=8080,
        help="HTTP port to listen on (default 8080)",
    )
    srv.add_argument(
        "--host",
        default="0.0.0.0",
        help="bind address (default 0.0.0.0)",
    )

    ev = sub.add_parser(
        "eval", help="replay labelled cases, score them, and compare runs"
    )
    ev_sub = ev.add_subparsers(dest="eval_command")
    eval_out = "./prxref-eval/"
    ev_run = ev_sub.add_parser(
        "run", help="replay every case and write one labelled run"
    )
    ev_run.add_argument(
        "--cases",
        required=True,
        metavar="PATH",
        help="the labelled cases: a cases.json file or a directory of case-*/ directories",
    )
    ev_run.add_argument(
        "--label",
        required=True,
        metavar="NAME",
        help=(
            "name of this run and of its directory under --out; an existing "
            "one is refused unless --resume is given"
        ),
    )
    ev_run.add_argument(
        "--out",
        default=eval_out,
        metavar="DIR",
        help=f"directory that holds the runs (default {eval_out})",
    )
    ev_run.add_argument(
        "--rules-file",
        default=None,
        metavar="PATH",
        help="team review rules for every case, as review --rules-file",
    )
    ev_run.add_argument(
        "--scoped-rules",
        action="append",
        default=None,
        metavar="PATH",
        help="path-scoped team review rules for every case, as review --scoped-rules; repeatable",
    )
    ev_run.add_argument(
        "--prompts-dir",
        default=None,
        metavar="DIR",
        help="prompt templates for every case, as review --prompts-dir",
    )
    ev_run.add_argument(
        "--resume",
        action="store_true",
        help="continue an existing --label run instead of refusing it",
    )
    ev_score = ev_sub.add_parser(
        "score", help="grade a run against its human labels"
    )
    ev_score.add_argument(
        "--label",
        required=True,
        metavar="NAME",
        help="the run to score, under --out",
    )
    ev_score.add_argument(
        "--judge-model",
        default=None,
        metavar="MODEL",
        help=(
            "model that grades the labels without a must_match predicate, on "
            "the review's own LLM backend; required when any label lacks one"
        ),
    )
    ev_score.add_argument(
        "--out",
        default=eval_out,
        metavar="DIR",
        help=f"directory that holds the runs (default {eval_out})",
    )
    ev_cmp = ev_sub.add_parser(
        "compare", help="compare two scored runs, label by label"
    )
    ev_cmp.add_argument(
        "run_a", metavar="A", help="first run: a label under --out, or a run directory"
    )
    ev_cmp.add_argument(
        "run_b", metavar="B", help="second run: a label under --out, or a run directory"
    )
    ev_cmp.add_argument(
        "--out",
        default=eval_out,
        metavar="DIR",
        help=f"directory that holds the runs (default {eval_out})",
    )

    tr = sub.add_parser("trace", help="work with a JSONL run trace")
    tr_sub = tr.add_subparsers(dest="trace_command")
    tr_render = tr_sub.add_parser(
        "render", help="render a run trace to a standalone HTML pipeline view"
    )
    tr_render.add_argument("trace_file", help="path to the JSONL trace to render")
    tr_render.add_argument(
        "-o", "--out",
        help="output HTML path (default: the trace path with an .html suffix)",
    )

    pr = sub.add_parser("prompts", help="work with the overridable prompt templates")
    pr_sub = pr.add_subparsers(dest="prompts_command")
    pr_export = pr_sub.add_parser(
        "export",
        help="write the packaged worker, systemic and summary templates to DIR as an override starting point",
    )
    pr_export.add_argument(
        "prompts_export_dir", metavar="DIR", help="directory to write into (created when missing)"
    )
    pr_export.add_argument(
        "--force",
        action="store_true",
        help="overwrite templates already in DIR (without it, an existing one is refused and nothing is written)",
    )

    return parser


def _fmt_counts(result: Any) -> str:
    if not isinstance(result, dict):
        return "-"
    active = result.get("findings_active")
    if not isinstance(active, list):
        return "-"
    sev: dict[str, int] = {}
    for f in active:
        sev[getattr(f, "severity", "?")] = sev.get(getattr(f, "severity", "?"), 0) + 1
    items = [f"{k}={v}" for k, v in sorted(sev.items())]
    return " ".join(items) if items else "0"


def _fmt_tokens(result: Any) -> str:
    if not isinstance(result, dict):
        return "0+0"
    tokens = result.get("tokens")
    if isinstance(tokens, dict):
        return f"{tokens.get('input', 0)}+{tokens.get('output', 0)}"
    inp = result.get("input_tokens", 0)
    out = result.get("output_tokens", 0)
    return f"{inp}+{out}"


def _fmt_cost(result: Any) -> str:
    """Render the run's cost for the ``-v`` line.

    ``costs.cost_label`` of the record's ``cost_usd``, ``cost_estimated`` and
    ``cost_api_equivalent`` (``$0.0007``, ``$0.0007 (API-equivalent)`` for a
    claude-cli-priced run, ``~$0.0007 (est.)``, or ``cost unknown`` for
    ``None``), and ``-`` when the result carries no ``cost_usd`` key at all.
    An absent key means nothing measured the cost; ``None`` means it was
    measured and no source could price it. The two are different claims, so
    they print differently.
    """
    if not isinstance(result, dict) or "cost_usd" not in result:
        return "-"
    return cost_label(
        result.get("cost_usd"), result.get("cost_estimated") is True,
        api_equivalent=result.get("cost_api_equivalent") is True,
    )


def _dash(value: Any, width: int | None = None) -> str:
    if value is None or value == "":
        return "-"
    text = str(value)
    return text[:width] if width else text


def _scope_counts(result: dict) -> tuple[int, int, int]:
    active = result.get("findings_active")
    scopes = [normalize_scope(getattr(f, "scope", None)) for f in active] if isinstance(active, list) else []
    n_in = scopes.count(SCOPE_IN)
    n_out = scopes.count(SCOPE_OUT)
    return n_in, n_out, len(scopes) - n_in - n_out


def _print_summary(
    result: Any,
    elapsed_s: float,
    *,
    verbose: bool,
    out=None,
) -> None:
    """Print the text-mode summary of one review.

    Always printed: ``verdict:``; ``coverage:`` when a chunk failed;
    ``size advisory:`` when the PR-size advisory fired; and ``replay:`` when
    the run was a replay, so a replay can never be read as a live review.
    The ``replay:`` line carries the stamp as ``key=value`` pairs, ending in
    ``description=<status>`` and, when a cutoff was chosen,
    `` as_of=<time> (<source>)``.
    Under ``-v`` it adds the finding counts, the ``elapsed/tokens/cost`` line,
    and one line for each configured input: ``rules:``, ``scoped rules:``
    (the number of scoped rules files, then ``<path>=<sha256 prefix>`` for
    each in load order, then ``cap=<per-unit cap>``), ``prompts:`` (the
    directory, then ``<name>=<sha256 prefix>`` for each overridden template
    in name order), ``ticket:`` (with the active findings' scope counts), and
    ``spec:``. ``result`` may be partial, or not a dict at all; a missing or
    ``None`` record prints nothing.
    """
    target = sys.stdout if out is None else out
    record = result if isinstance(result, dict) else {}
    verdict = result.get("verdict") if isinstance(result, dict) else result
    print(f"verdict: {verdict if verdict is not None else 'done'}", file=target)
    failed = record.get("chunks_failed", 0)
    if failed:
        reviewed = record.get("chunks_reviewed", 0)
        print(f"coverage: {reviewed}/{reviewed + failed} chunks reviewed", file=target)
    size = record.get("size_advisory")
    if isinstance(size, dict) and size.get("message"):
        print(f"size advisory: {size['message']}", file=target)
    replay = record.get("replay")
    if isinstance(replay, dict):
        as_of = replay.get("as_of")
        cutoff = f" as_of={as_of} ({_dash(replay.get('as_of_source'))})" if as_of else ""
        print(
            f"replay: base={_dash(replay.get('base_sha'), 12)} head={_dash(replay.get('head_sha'), 12)} "
            f"threads={_dash(replay.get('threads'))} diff_file={_dash(replay.get('diff_file'))} "
            f"description={_dash(replay.get('description'))}{cutoff}",
            file=target,
        )
    if not verbose:
        return
    dropped = record.get("findings_dropped", [])
    dropped = len(dropped) if isinstance(dropped, list) else 0
    print(f"counts: {_fmt_counts(result)} (dropped: {dropped})", file=target)
    print(f"elapsed: {elapsed_s:.1f}s tokens: {_fmt_tokens(result)} cost: {_fmt_cost(result)}", file=target)
    rules = record.get("review_rules")
    if isinstance(rules, dict):
        truncated = f" (truncated at {_dash(rules.get('max_chars'))})" if rules.get("truncated") else ""
        print(
            f"rules: {_dash(rules.get('path'))} sha256={_dash(rules.get('sha256'), 12)} "
            f"chars={_dash(rules.get('chars'))}{truncated}",
            file=target,
        )
    scoped = record.get("scoped_rules")
    if isinstance(scoped, dict):
        files = scoped.get("files")
        files = [info if isinstance(info, dict) else {} for info in files] if isinstance(files, list) else []
        stamps = "".join(f" {_dash(info.get('path'))}={_dash(info.get('sha256'), 12)}" for info in files)
        print(f"scoped rules: {len(files)} file(s){stamps} cap={_dash(scoped.get('max_chars'))}", file=target)
    prompts = record.get("prompt_templates")
    if isinstance(prompts, dict):
        templates = prompts.get("templates")
        templates = templates if isinstance(templates, dict) else {}
        stamps = "".join(
            f" {name}={_dash(info.get('sha256') if isinstance(info, dict) else None, 12)}"
            for name, info in sorted(templates.items())
        )
        print(f"prompts: {_dash(prompts.get('dir'))}{stamps}", file=target)
    ticket = record.get("ticket_context")
    if isinstance(ticket, dict):
        truncated = " truncated" if ticket.get("truncated") else ""
        n_in, n_out, n_unknown = _scope_counts(record)
        print(
            f"ticket: {_dash(ticket.get('path'))} sha256={_dash(ticket.get('sha256'), 12)} "
            f"chars={_dash(ticket.get('chars'))}{truncated} in={n_in} out={n_out} unknown={n_unknown}",
            file=target,
        )
    spec = record.get("spec_grounding")
    if isinstance(spec, dict):
        print(
            f"spec: {_dash(spec.get('ok'))}/{_dash(spec.get('sources'))} source(s) ok, "
            f"{_dash(spec.get('constraints'))} constraint(s)",
            file=target,
        )


def _fmt_finding_line(f: Any) -> str:
    """Render one active finding as ``<severity> <file>:<line> <title> (confidence 0.NN)``.

    A finding the ticket judged gains `` [scope: in]`` or `` [scope: out]``
    after the frozen prefix; ``unknown`` (always the case without a ticket)
    adds nothing. A finding that names a rule then gains `` [rule: <rule>]``,
    after any scope tag. Only a run with finding grouping on keeps a rule
    (the orchestrator resets every rule to ``None`` otherwise), so with
    grouping off the line is unchanged.
    """
    severity = getattr(f, "severity", None) or ""
    location = f"{getattr(f, 'file', '')}:{getattr(f, 'line', 0)}"
    title = getattr(f, "title", None) or ""
    confidence = getattr(f, "confidence", None) or 0.0
    line = f"{severity} {location} {title} (confidence {confidence:.2f})"
    scope = getattr(f, "scope", None)
    if scope in (SCOPE_IN, SCOPE_OUT):
        line = f"{line} [scope: {scope}]"
    rule = getattr(f, "rule", None)
    if rule:
        line = f"{line} [rule: {rule}]"
    return line


def _fmt_indented_body(body: str) -> str:
    """Indent every line of a finding's body by two spaces."""
    return "\n".join(f"  {line}" for line in (body or "").splitlines())


def _fmt_dropped_line(f: Any) -> str:
    """Render one dropped finding as ``  <file>:<line> <title> -- <drop_reason>``."""
    location = f"{getattr(f, 'file', '')}:{getattr(f, 'line', 0)}"
    title = getattr(f, "title", None) or ""
    reason = getattr(f, "drop_reason", None) or ""
    return f"  {location} {title} -- {reason}"


def _print_findings(result: Any, *, out=None) -> None:
    """Print each active finding's body, then any dropped findings and why.

    Text-mode only, gated by the caller on ``--no-post`` or ``-v``: the
    frozen CLI contract (issue 08) makes finding bodies reachable from stdout
    without importing ``prxref.cli._run_review`` directly.
    """
    target = sys.stdout if out is None else out
    if not isinstance(result, dict):
        return
    for f in result.get("findings_active") or []:
        print(_fmt_finding_line(f), file=target)
        body = _fmt_indented_body(getattr(f, "body", None))
        if body:
            print(body, file=target)
    dropped = result.get("findings_dropped") or []
    if dropped:
        print("dropped:", file=target)
        for f in dropped:
            print(_fmt_dropped_line(f), file=target)


def _finding_json(f: Any, *, drop_reason: str | None) -> dict:
    """Build one JSON finding row explicitly (``Finding`` is a dataclass, not
    JSON-serializable by default).

    ``scope`` is the finding's position relative to the ticket context
    (``in``, ``out`` or ``unknown``); a finding object without the attribute
    reports ``unknown``.

    ``rule`` and ``locations`` follow ``scope`` and are always present. ``rule``
    is the rule the finding names, or ``null`` (never ``""``) when it names
    none, which is every finding of a run with finding grouping off.
    ``locations`` is set only on the representative of a grouped finding: a
    list of ``{"file": ..., "line": ...}`` objects for the other locations its
    ``Also at:`` paragraph lists, in the same order, never including the row's
    own ``file`` and ``line``. It is ``null`` on every other row, a member
    row dropped as ``grouped into <file>:<line>`` included (that row still
    carries its own ``rule``). A finding object without either attribute
    reports ``null`` for it.
    """
    locations = getattr(f, "locations", None) or ()
    return {
        "file": f.file,
        "line": f.line,
        "severity": f.severity,
        "confidence": f.confidence,
        "scope": getattr(f, "scope", "unknown"),
        "rule": getattr(f, "rule", None) or None,
        "locations": [{"file": path, "line": line} for path, line in locations] or None,
        "title": f.title,
        "body": f.body,
        "drop_reason": drop_reason,
    }


def _build_json_result(result: Any) -> dict:
    """Build the single JSON payload for ``--format json``.

    Key order: ``verdict``, ``findings``, ``chunk_count``, ``chunks_reviewed``,
    ``chunks_failed``, ``elapsed_ms``, ``input_tokens``, ``output_tokens``,
    ``cost_usd``, ``cost_estimated``, ``posted``, ``review_rules``,
    ``ticket_context``, ``spec_grounding``, ``size_advisory``,
    ``prompt_templates``, ``scoped_rules``, then ``sampling`` and ``replay``
    when present.

    Tolerates an error-shaped or partial result (a dict missing keys, as an
    incomplete or failed run may return): every always-present key defaults
    to ``None`` and ``findings`` defaults to ``[]`` rather than raising. The
    run-record keys new in 0.14 (``cost_usd`` through ``size_advisory``) and
    0.15's ``prompt_templates`` and ``scoped_rules`` are always emitted and
    are ``null`` when their feature is off; ``cost_usd`` is also ``null``
    when no source could price the run, never ``0``. Every ``findings`` row,
    active or dropped, carries 0.15's ``rule`` and ``locations`` the same way
    (see :func:`_finding_json`).
    ``sampling`` and ``replay`` are forwarded only when the result already
    carries them. ``replay`` is on replay runs only, so a normal run's
    payload has no ``replay`` key at all.
    """
    if not isinstance(result, dict):
        result = {}
    findings = [
        _finding_json(f, drop_reason=None) for f in result.get("findings_active") or []
    ]
    findings.extend(
        _finding_json(f, drop_reason=getattr(f, "drop_reason", None))
        for f in result.get("findings_dropped") or []
    )
    payload = {
        "verdict": result.get("verdict"),
        "findings": findings,
        "chunk_count": result.get("chunk_count"),
        "chunks_reviewed": result.get("chunks_reviewed"),
        "chunks_failed": result.get("chunks_failed"),
        "elapsed_ms": result.get("elapsed_ms"),
        "input_tokens": result.get("input_tokens"),
        "output_tokens": result.get("output_tokens"),
        "cost_usd": result.get("cost_usd"),
        "cost_estimated": result.get("cost_estimated"),
        "posted": result.get("posted"),
        "review_rules": result.get("review_rules"),
        "ticket_context": result.get("ticket_context"),
        "spec_grounding": result.get("spec_grounding"),
        "size_advisory": result.get("size_advisory"),
        "prompt_templates": result.get("prompt_templates"),
        "scoped_rules": result.get("scoped_rules"),
    }
    if "sampling" in result:
        payload["sampling"] = result["sampling"]
    if "replay" in result:
        payload["replay"] = result["replay"]
    return payload


_FULL_SHA_RE = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?")


@dataclass(frozen=True)
class _ReplayRequest:
    """The validated replay flags of one ``review`` run (issue #65).

    ``base_sha`` / ``head_sha`` are both full, lowercased SHAs or both
    ``None``. ``diff_file`` is the path exactly as the operator typed it, and
    ``diff_text`` is that file's text once ``_run_review`` has read it
    (``None`` until then, and without ``--diff-file``).

    The description fields (issue #16) hold at most one choice, because
    ``--as-of``, ``--description-file`` and ``--no-description`` are mutually
    exclusive. ``as_of`` is the ``--as-of`` time as a timezone-aware UTC
    ``datetime``. ``description_file`` is the path as typed, and
    ``description_text`` is its decoded text once ``_run_review`` has read it
    (``None`` until then, and without ``--description-file``).
    ``no_description`` is ``--no-description``.
    """

    base_sha: str | None = None
    head_sha: str | None = None
    no_threads: bool = False
    diff_file: str | None = None
    diff_text: str | None = None
    as_of: datetime | None = None
    description_file: str | None = None
    description_text: str | None = None
    no_description: bool = False

    def stamp(self, *, has_forge: bool, pin: Any = None) -> dict[str, Any]:
        """The run record's ``replay`` stamp: seven keys, in a fixed order, all present.

        ``threads`` is ``"hidden"`` whenever the PR's threads were not
        consulted: under ``--no-threads``, or with no forge at all
        (``--diff-file`` without ``--pr-url``).

        ``description``, ``as_of`` and ``as_of_source`` (issue #16) say which
        title and description the review saw. ``pin`` is the replay forge's
        :class:`~prxref.forges.replay.DescriptionPin`, and its status, cutoff
        and cutoff source are copied as they are. ``as_of`` is written as a
        UTC ISO-8601 time ending in ``Z``, with the fraction of a second only
        when there is one, so ``--as-of`` given that value picks the same
        cutoff. Anything else as ``pin`` (``None``, as from a
        :class:`~prxref.forges.replay.LocalDiffForge`) is read from the
        flags: ``"file"`` under ``--description-file``, ``"none"`` under
        ``--no-description``, else ``"file"`` without a forge (the diff
        file supplies the description) and ``"live"`` with one; ``as_of``
        and ``as_of_source`` are then ``None``. There is no title key: the
        title is pinned exactly when the description is.
        """
        if isinstance(pin, DescriptionPin):
            description, as_of, as_of_source = pin.status, _iso_z(pin.as_of), pin.as_of_source
        else:
            description, as_of, as_of_source = self._flag_description(has_forge=has_forge), None, None
        return {
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "threads": "hidden" if self.no_threads or not has_forge else "shown",
            "diff_file": self.diff_file,
            "description": description,
            "as_of": as_of,
            "as_of_source": as_of_source,
        }

    def _flag_description(self, *, has_forge: bool) -> str:
        if self.description_file is not None:
            return "file"
        if self.no_description:
            return "none"
        return "live" if has_forge else "file"


def _iso_z(value: datetime | None) -> str | None:
    """``value`` (timezone-aware) as a UTC ISO-8601 string ending in ``Z``; ``None`` stays ``None``.

    Whole seconds print as ``YYYY-MM-DDTHH:MM:SSZ``; a fraction of a second
    is kept (``.ffffff``) rather than floored, so the string names the same
    instant.
    """
    if value is None:
        return None
    return value.astimezone(UTC).replace(tzinfo=None).isoformat() + "Z"


def _resolve_replay(
    url: str | None,
    *,
    base_sha: str | None = None,
    head_sha: str | None = None,
    no_threads: bool = False,
    diff_file: str | None = None,
    as_of: str | None = None,
    description_file: str | None = None,
    no_description: bool = False,
) -> _ReplayRequest | None:
    """Validate the replay flags; ``None`` means a normal, non-replay run.

    Pure: it reads nothing and calls nothing. ``_run_review`` calls it first,
    before ``detect_forge``, so a bad set of replay flags exits 2 even next
    to an unrecognised URL. A flag counts as given whenever it is not
    ``None`` (``--no-description`` whenever it is true), so an empty value
    is validated rather than ignored.

    The checks run in this order, each a ``ConfigError`` naming its flag:
    no ``--pr-url`` and no ``--diff-file``; only one of ``--base-sha`` /
    ``--head-sha``; either one not a full 40- or 64-character hex SHA; the
    two naming the same commit (compared lowercased); a range without
    ``--pr-url`` to resolve it in; two or more of ``--as-of``,
    ``--description-file`` and ``--no-description``, naming every one
    given; an ``--as-of`` that :func:`_parse_as_of` refuses; and an
    ``--as-of`` without ``--pr-url``, whose history it reads. Whether the
    forge can fetch the range is only known once it exists, so
    ``_run_review`` checks that.
    """
    if url is None and diff_file is None:
        raise ConfigError("--pr-url: required unless --diff-file is given")
    if (base_sha is None) != (head_sha is None):
        only = "--base-sha" if head_sha is None else "--head-sha"
        raise ConfigError(f"--base-sha/--head-sha: must be given together (got only {only})")
    if base_sha is not None and head_sha is not None:
        for flag, value in (("--base-sha", base_sha), ("--head-sha", head_sha)):
            if not _FULL_SHA_RE.fullmatch(value):
                raise ConfigError(
                    f"{flag}: must be a full 40- or 64-character hex commit SHA, "
                    f"got {value!r} (resolve it with git rev-parse)"
                )
        base_sha, head_sha = base_sha.lower(), head_sha.lower()
        if base_sha == head_sha:
            raise ConfigError("--base-sha/--head-sha: must name two different commits")
        if url is None:
            raise ConfigError(
                "--base-sha/--head-sha: need --pr-url (the range is resolved in "
                "that PR's repository)"
            )
    chosen = [
        flag for flag, given in (
            ("--as-of", as_of is not None),
            ("--description-file", description_file is not None),
            ("--no-description", bool(no_description)),
        ) if given
    ]
    if len(chosen) > 1:
        raise ConfigError(
            f"{'/'.join(chosen)}: cannot be combined (give at most one of "
            "--as-of, --description-file and --no-description)"
        )
    cutoff = _parse_as_of(as_of) if as_of is not None else None
    if cutoff is not None and url is None:
        raise ConfigError(
            "--as-of: needs --pr-url (the description history is read from "
            "that PR)"
        )
    if head_sha is None and not no_threads and diff_file is None and not chosen:
        return None
    return _ReplayRequest(
        base_sha=base_sha, head_sha=head_sha, no_threads=bool(no_threads),
        diff_file=diff_file, as_of=cutoff, description_file=description_file,
        no_description=bool(no_description),
    )


def _parse_as_of(value: str) -> datetime:
    """Parse ``--as-of`` into a timezone-aware UTC ``datetime``; refusals are a ``ConfigError``.

    The value is ISO-8601 as :meth:`datetime.fromisoformat` reads it, and it
    must carry a UTC offset (``Z`` or ``+02:00``). A date alone
    (``2026-05-01``) and a time without an offset are refused rather than
    read in this machine's time zone or at an assumed hour, because either
    guess would move the cutoff with the host running the replay. A time
    whose UTC equivalent falls outside ``datetime``'s range is refused too.
    """
    try:
        day = date.fromisoformat(value)
    except ValueError:
        pass
    else:
        raise ConfigError(
            f"--as-of: {value!r} is a date without a time; give a time with a "
            f"UTC offset, such as '{day.isoformat()}T00:00:00Z'"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ConfigError(
            "--as-of: must be an ISO-8601 time with a UTC offset, such as "
            f"'2026-05-01T09:30:00Z', got {value!r}"
        ) from None
    if parsed.utcoffset() is None:
        raise ConfigError(
            f"--as-of: {value!r} has no UTC offset; add one, such as 'Z' for "
            "UTC or '+02:00'"
        )
    try:
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError):
        raise ConfigError(f"--as-of: {value!r} is out of range in UTC") from None


def _read_diff_file(path: str) -> str:
    """Read the ``--diff-file`` text; a file that cannot be read is a ``ConfigError``.

    The message is ``--diff-file: cannot read '<path>': <strerror>``, which
    covers a missing file and a directory alike. Undecodable bytes are
    replaced, not refused, because the diff is review input rather than
    configuration. A blank file is not a configuration error either: the
    replay forge raises on it, and the run ends as an ``Error`` run (exit 0,
    or 1 under ``PRXREF_FAIL_ON=error`` or ``any``).
    """
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ConfigError(
            f"--diff-file: cannot read {path!r}: {exc.strerror or exc}"
        ) from exc


def _read_description_file(path: str) -> str:
    """Read the ``--description-file`` text; every failure is a ``ConfigError`` naming the flag.

    The file is configuration, not review input, so it is read as the rules
    file is: through :func:`prxref.text_inputs.check_readable_path` (a path
    under the working directory that symlinks out of it is refused, and so
    is anything but a regular file), then decoded strictly by
    :func:`prxref.text_inputs.decode_text` (a leading BOM dropped, CRLF and
    CR folded to LF). A missing, unreadable or non-regular file, invalid
    UTF-8 and NUL bytes each raise. A blank file is not an error: it is an
    empty description.
    """
    try:
        resolved = check_readable_path(path, confine=True)
        with open(resolved, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise OSError(errno.EINVAL, "not a regular file", path)
            raw = fh.read()
    except OSError as exc:
        raise ConfigError(
            f"--description-file: cannot read {path!r}: {exc.strerror or exc}"
        ) from exc
    try:
        text = decode_text(raw)
    except UnicodeDecodeError as exc:
        offset = exc.start + (len(codecs.BOM_UTF8) if raw.startswith(codecs.BOM_UTF8) else 0)
        raise ConfigError(
            f"--description-file: {path!r} is not UTF-8 text ({exc.reason} at byte {offset})"
        ) from exc
    if "\x00" in text:
        raise ConfigError(
            f"--description-file: {path!r} contains NUL bytes; expected Markdown or plain text"
        )
    return text


_CURRENT_DESCRIPTION = "replay shows the PR's CURRENT title and description"


def _replay_forge(forge: Any, ref: Any, replay: _ReplayRequest) -> ReplayForge:
    """Wrap the ``--pr-url`` forge in a :class:`ReplayForge` for this replay.

    A pinned range that has to be fetched (no ``--diff-file``) needs the
    forge's optional ``get_compare_diff``; without it this raises the
    ``ConfigError`` naming ``--base-sha/--head-sha`` (exit 2) before any
    network call. An explicit ``--as-of`` needs the forge's optional
    ``get_pr_history`` the same way, and without it raises the
    ``ConfigError`` naming ``--as-of``. Two combinations are allowed but
    logged as a WARNING, because each leaks the PR's present into a replay:
    pinned SHAs without ``--no-threads`` still show the PR's current
    threads, and a ``--diff-file`` without ``--head-sha`` reads file context
    at the PR's current head.

    Then the PR's title and description are resolved (issue #16), and the
    returned forge's ``description_pin`` records the outcome
    (:class:`prxref.forges.replay.DescriptionPin`). ``--description-file``
    and ``--no-description`` fix the description (``file``, ``none``) and
    read no history. Otherwise pinning is on: this reads the forge's
    ``get_pr_history`` once, at ``--head-sha`` when one is given, and picks
    the cutoff: ``--as-of``, else the first human review, else the head
    commit's date. When the history holds the description in force at the
    cutoff, the forge shows that title and description (``pinned``), with
    no further network call. Every other outcome shows the PR's current
    title and description (``live``) and logs a WARNING saying why: a forge
    with no ``get_pr_history``, a history read that raises (401/403,
    transport), a history with no cutoff to offer, or one that does not
    reach the cutoff (incomplete, or that version deleted). The pin carries
    the cutoff and its source whenever one was chosen, a ``live`` one
    included.
    """
    if (
        replay.head_sha is not None
        and replay.diff_text is None
        and getattr(forge, "get_compare_diff", None) is None
    ):
        raise ConfigError(
            f"--base-sha/--head-sha: the {ref.forge} forge cannot fetch a "
            "pinned commit range"
        )
    if replay.as_of is not None and getattr(forge, "get_pr_history", None) is None:
        raise ConfigError(
            f"--as-of: the {ref.forge} forge cannot read a pull request's "
            "description history"
        )
    if replay.head_sha is not None and not replay.no_threads:
        logger.warning(
            "replay at pinned SHAs still shows the PR's CURRENT threads to the "
            "prompt and the dedup passes; add --no-threads for a blind replay"
        )
    if replay.diff_file is not None and replay.head_sha is None:
        logger.warning(
            "--diff-file with --pr-url and no --head-sha: file context is read "
            "at the PR's current head, which may not match the file"
        )
    pin_kwargs, pin = _resolve_description(forge, ref, replay)
    return ReplayForge(
        forge, base_sha=replay.base_sha, head_sha=replay.head_sha,
        hide_threads=replay.no_threads, diff_text=replay.diff_text,
        description_pin=pin, **pin_kwargs,
    )


def _resolve_description(
    forge: Any, ref: Any, replay: _ReplayRequest,
) -> tuple[dict[str, Any], DescriptionPin]:
    """Resolve a ``--pr-url`` replay's title and description for :func:`_replay_forge`.

    Returns the ``ReplayForge`` keyword arguments that apply it (a fixed
    ``description``, or the ``history`` and ``cutoff`` to pin with, which
    are passed only when the status is ``pinned``, so a ``live`` resolution
    leaves ``get_pr`` untouched) and the :class:`DescriptionPin`. The
    ``--as-of`` configuration error is raised by the caller, before its
    warnings.
    """
    if replay.description_file is not None:
        return {"description": replay.description_text}, DescriptionPin("file", None, None)
    if replay.no_description:
        return {"description": ""}, DescriptionPin("none", None, None)
    getter = getattr(forge, "get_pr_history", None)
    if getter is None:
        logger.warning(
            "%s: the %s forge cannot read a pull request's description history",
            _CURRENT_DESCRIPTION, ref.forge,
        )
        return {}, DescriptionPin("live", None, None)
    history = None
    try:
        history = getter(ref, head_sha=replay.head_sha)
    except Exception as exc:  # noqa: BLE001 - a failed history read falls back to the live text
        logger.warning(
            "%s: reading its description history failed (%s: %s)",
            _CURRENT_DESCRIPTION, type(exc).__name__, exc,
        )
    chosen = choose_cutoff(replay.as_of, history)
    if chosen is None:
        if history is not None:
            logger.warning(
                "%s: its history has no first human review and no head commit "
                "date to pin them to (give --as-of to choose the time)",
                _CURRENT_DESCRIPTION,
            )
        return {}, DescriptionPin("live", None, None)
    cutoff, source = chosen
    if history is None:
        return {}, DescriptionPin("live", cutoff, source)
    if pin_status(history, cutoff) == "live":
        logger.warning(
            "%s: its description history does not reach the %s cutoff %s "
            "(it is incomplete, or the version then in force was deleted)",
            _CURRENT_DESCRIPTION, source, cutoff.isoformat(),
        )
        return {}, DescriptionPin("live", cutoff, source)
    return {"history": history, "cutoff": cutoff}, DescriptionPin("pinned", cutoff, source)


def _load_text_input(loader: Any, path: str | list[str], *, max_chars: int, source: str) -> Any:
    """Run the rules or ticket-context ``loader``, fencing every failure into a ``ConfigError``.

    ``path`` is handed to ``loader`` as given: one path for the rules and
    ticket files, the configured list for the path-scoped rules.
    The loaders raise ``ConfigError`` naming ``source`` themselves; an
    ``OSError`` or ``ValueError`` that escapes one is re-raised as a
    ``ConfigError`` naming it too. So an unusable file always exits 2 before
    any network call, and nothing a loader raises can reach the orchestrator,
    which reads the loaded object unfenced.
    """
    try:
        return loader(path, max_chars=max_chars, source=source)
    except ConfigError:
        raise
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{source}: cannot load {path!r}: {exc}") from exc


def _load_prompts_dir(path: str | None, *, source: str) -> Any:
    """Load the prompt-template overrides in ``path``, fenced as :func:`_load_text_input` fences a file.

    ``None``, ``""`` and whitespace mean "no overrides" and return ``None``,
    so ``--prompts-dir ""`` turns ``PRXREF_PROMPTS_DIR`` off. The loader
    raises ``ConfigError`` naming ``source`` itself; an ``OSError`` or
    ``ValueError`` that escapes it becomes one too, so an unusable directory
    always exits 2 before any network call.
    """
    try:
        return load_prompt_templates(path, source=source)
    except ConfigError:
        raise
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{source}: cannot load prompts directory {path!r}: {exc}") from exc


def _run_review(
    url: str | None,
    *,
    post: bool = True,
    max_chunks: int | None = None,
    timeout: float | None = None,
    trace_dir: str | None = None,
    spec_sources: list[str] | None = None,
    rules_file: str | None = None,
    scoped_rules: list[str] | None = None,
    context_file: str | None = None,
    prompts_dir: str | None = None,
    base_sha: str | None = None,
    head_sha: str | None = None,
    no_threads: bool = False,
    diff_file: str | None = None,
    as_of: str | None = None,
    description_file: str | None = None,
    no_description: bool = False,
) -> Any:
    replay = _resolve_replay(
        url, base_sha=base_sha, head_sha=head_sha, no_threads=no_threads,
        diff_file=diff_file, as_of=as_of, description_file=description_file,
        no_description=no_description,
    )
    # The diff file is read with the flags, before the URL is parsed, so an
    # unreadable one exits 2 whatever the URL. Without --pr-url it is the
    # whole input: a synthetic "local" ref, and no forge is ever built.
    if replay is not None and replay.diff_file is not None:
        replay = replace(replay, diff_text=_read_diff_file(replay.diff_file))
    if replay is not None and replay.description_file is not None:
        replay = replace(
            replay, description_text=_read_description_file(replay.description_file),
        )
    if url is None:
        ref = LocalDiffForge.ref_for(replay.diff_file)
    else:
        ref = detect_forge(url)
        if ref is None:
            return None
    # --max-chunks, --timeout, --spec, --rules-file, --scoped-rules,
    # --context-file and --prompts-dir arrive as load_config overrides (None is
    # ignored, "" is not), so each flag rides exactly the path its environment
    # variable does: --max-chunks and --timeout are range-checked on the same
    # pass as PRXREF_MAX_CHUNKS and PRXREF_LLM_TIMEOUT, --spec and
    # --scoped-rules replace PRXREF_SPEC_SOURCES / PRXREF_SCOPED_RULES
    # wholesale rather than merging with them, and --rules-file "" /
    # --scoped-rules "" / --context-file "" / --prompts-dir "" blank their
    # variable for one run. Precedence is derived once, here. There is
    # deliberately no way to inject a pre-built config dict: that would bypass
    # _check_ranges and make every range guarantee conditional on nobody using
    # the bypass. --timeout only ever feeds llm_timeout, for the LLM client:
    # orchestrate_review has no timeout parameter.
    cfg = load_config(
        max_chunks=max_chunks,
        llm_timeout=timeout,
        trace_dir=trace_dir,
        spec_sources=spec_sources,
        review_rules=rules_file,
        scoped_rules=scoped_rules,
        ticket_context_file=context_file,
        prompts_dir=prompts_dir,
        # The operator typed a flag, so a rejection has to name the flag. Only
        # the CLI knows that spelling; config takes the label and reports it.
        source_labels={
            "max_chunks": "--max-chunks",
            "llm_timeout": "--timeout",
            "spec_sources": "--spec",
            "review_rules": "--rules-file",
            "scoped_rules": "--scoped-rules",
            "ticket_context_file": "--context-file",
            "prompts_dir": "--prompts-dir",
        },
    )
    # The rules files, the ticket file and the prompts directory are read
    # here, after config and before make_forge and the LLM client, so an
    # unusable one exits 2 before any network I/O. load_config stays I/O-free.
    # Each is reported under the input that supplied its path: the flag
    # whenever it was given, else the variable. The scoped rules are checked
    # against the always-on file, so one team word mapped to two tiers across
    # them exits 2 here rather than silently taking the scoped tier.
    rules = _load_text_input(
        load_review_rules, cfg["review_rules"],
        max_chars=cfg["review_rules_max_chars"],
        source="--rules-file" if rules_file is not None else "PRXREF_REVIEW_RULES",
    )
    scoped = _load_text_input(
        functools.partial(load_scoped_rules, always_on=rules), cfg["scoped_rules"],
        max_chars=cfg["review_rules_max_chars"],
        source="--scoped-rules" if scoped_rules is not None else "PRXREF_SCOPED_RULES",
    )
    ticket = _load_text_input(
        load_ticket_context, cfg["ticket_context_file"],
        max_chars=cfg["ticket_context_max_chars"],
        source=(
            "--context-file" if context_file is not None else "PRXREF_TICKET_CONTEXT_FILE"
        ),
    )
    prompts = _load_prompts_dir(
        cfg["prompts_dir"],
        source="--prompts-dir" if prompts_dir is not None else "PRXREF_PROMPTS_DIR",
    )
    # PRXREF_DRY_RUN is the standing "never write to the forge" switch and
    # --no-post is the per-invocation one; either alone suppresses posting, so
    # the flag still wins when the environment says nothing. This sits inside
    # _run_review rather than in _cmd_review because the webhook daemon calls
    # _run_review directly with post=True, and the daemon is precisely the
    # thing an operator wants to watch before pointing it at a busy repo.
    if post and cfg["dry_run"]:
        logger.info("PRXREF_DRY_RUN=1: reviewing %s without posting to the forge", ref.url)
        post = False
    # A replay reviews a pinned input for evaluation, never the live PR as it
    # stands, so it must never write: any replay flag turns posting off, with
    # or without --no-post. The replay forges also refuse every write.
    if replay is not None and post:
        logger.info("replay run: posting to the forge is disabled")
        post = False
    if url is None:
        forge = LocalDiffForge(
            replay.diff_text, path=replay.diff_file,
            description=(
                replay.description_text if replay.description_file is not None
                else "" if replay.no_description else None
            ),
        )
    else:
        forge = make_forge(ref)
        if replay is not None:
            forge = _replay_forge(forge, ref, replay)
    llm = importlib.import_module("prxref.llm_backends").create_llm_client(cfg)
    orchestrate = importlib.import_module("prxref.orchestrator").orchestrate_review
    return orchestrate(
        forge=forge,
        ref=ref,
        llm=llm,
        post=post,
        # Lowercase, and indexed rather than .get(): load_config returns
        # lowercase, unprefixed keys and always returns every one of them. The
        # uppercase spelling that used to be here silently pinned every run to
        # the literal default, so PRXREF_MAX_CHUNKS never reached the pipeline.
        max_chunks=cfg["max_chunks"],
        max_tokens=cfg["llm_max_tokens"],
        token_budget=cfg["chunk_token_budget"],
        max_files_per_chunk=cfg["chunk_max_files"],
        context_lines=cfg["chunk_context_lines"],
        max_workers=cfg["max_workers"],
        max_inline_comments=cfg["max_inline_comments"],
        # Passed explicitly so the resolved config wins: apply_quality_gate
        # otherwise falls back to re-reading the environment itself, which
        # discards any value an override or a .env-driven load resolved.
        confidence_floor=cfg["confidence_floor"],
        max_errors=cfg["max_error_findings"],
        max_warning_findings=cfg["max_warning_findings"],
        max_outofscope_findings=cfg["max_outofscope_findings"],
        group_findings=cfg["group_findings"],
        dedup_similarity=cfg["dedup_similarity"],
        post_mode=cfg["post_mode"],
        post_verdict=cfg["post_verdict"],
        trace_file=cfg["trace_file"],
        trace_dir=cfg["trace_dir"],
        spec_sources=cfg["spec_sources"],
        spec_max_chars=cfg["spec_max_chars"],
        spec_digest_tokens=cfg["spec_digest_tokens"],
        jira_base_url=cfg["jira_base_url"],
        jira_email=cfg["jira_email"],
        jira_api_token=cfg["jira_api_token"],
        rules=rules,
        ticket=ticket,
        # Already parsed by load_config into {model: costs.ModelPrice}.
        price_table=cfg["price_table"],
        post_cost=cfg["post_cost"],
        size_warn_lines=cfg["size_warn_lines"],
        size_warn_files=cfg["size_warn_files"],
        size_ignore_globs=cfg["size_ignore_globs"],
        replay=(
            replay.stamp(has_forge=url is not None, pin=getattr(forge, "description_pin", None))
            if replay is not None else None
        ),
        prompts=prompts,
        scoped_rules=scoped,
        scoped_rules_max_chars=cfg["scoped_rules_max_chars"],
    )


def _webhook_handler(url: str) -> None:
    """Review one webhook-delivered PR, posting unless PRXREF_DRY_RUN is set.

    ``post=True`` is the daemon's intent, not its last word: ``_run_review``
    downgrades it when the configured dry run says so, which is the only way to
    observe the daemon against a real repo without writing to it.

    ``context_file=""`` blanks ``PRXREF_TICKET_CONTEXT_FILE`` for every
    webhook: one static ticket file cannot describe every PR the daemon sees,
    so its findings always carry scope ``unknown``. The team rules file, the
    ``PRXREF_SCOPED_RULES`` files and the ``PRXREF_PROMPTS_DIR`` templates
    still come from the daemon's environment, re-read on every webhook. The
    daemon passes no replay flag, so it never replays.
    """
    try:
        _run_review(url, post=True, context_file="")
    except Exception:
        logger.exception("webhook review failed for %s", url)


def _fail_on_exit(result: Any, fail_on: str) -> tuple[int, str | None]:
    """The exit code a returned review result earns under the ``fail_on`` policy.

    ``never`` is always 0. Under ``error`` and ``any``, a result with verdict
    ``Error`` exits 1 whatever its findings: the orchestrator returns one
    instead of raising when the forge could not be read, the diff could not be
    parsed or chunked, or every chunk review failed, so it is a review that did
    not complete — the same outcome as the crash ``_cmd_review`` gates, and one
    a gating lane must not read as green.

    Otherwise severity is compared exactly as the verdict is built in the
    orchestrator (``Request-Changes`` iff an active finding has severity
    ``error``), so the gate and the posted verdict can never disagree about
    what counts. A result without parseable findings is tolerated the way
    ``_fmt_counts`` tolerates one: nothing countable means nothing to gate on.

    Returns the exit code and, when the gate fires, the stderr line that says
    why — silence would read as a crash rather than a decision.
    """
    if fail_on == "never":
        return 0, None
    if isinstance(result, dict) and result.get("verdict") == "Error":
        return 1, (
            f"PRXREF_FAIL_ON={fail_on}: review did not complete "
            "(verdict Error); exiting 1"
        )
    findings = result.get("findings_active") if isinstance(result, dict) else None
    if not isinstance(findings, list):
        return 0, None
    if fail_on == "error":
        hits = [f for f in findings if getattr(f, "severity", None) == "error"]
    else:
        hits = findings
    if not hits:
        return 0, None
    plural = "" if len(hits) == 1 else "s"
    return 1, (
        f"PRXREF_FAIL_ON={fail_on}: review found {len(hits)} "
        f"active finding{plural}; exiting 1"
    )


def _cmd_review(args: argparse.Namespace) -> int:
    t0 = time.perf_counter()
    # The policy is resolved before the run, not taken from _run_review's
    # config: a review that fails to complete is precisely the outcome the
    # knob must gate, so the value has to be known before orchestration can
    # fail. load_config is a pure read of the same environment, and
    # _run_review loads it again with the flag overrides — within one process
    # the two cannot disagree.
    try:
        fail_on = load_config()["fail_on"]
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        result = _run_review(
            args.pr_url,
            post=not args.no_post,
            max_chunks=args.max_chunks,
            timeout=args.timeout,
            trace_dir=args.trace_dir,
            spec_sources=args.spec,
            rules_file=args.rules_file,
            scoped_rules=args.scoped_rules,
            context_file=args.context_file,
            prompts_dir=args.prompts_dir,
            base_sha=args.base_sha,
            head_sha=args.head_sha,
            no_threads=args.no_threads,
            diff_file=args.diff_file,
            as_of=args.as_of,
            description_file=args.description_file,
            no_description=args.no_description,
        )
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"review failed: {exc}", file=sys.stderr)
        logger.debug("review failed with traceback", exc_info=True)
        if fail_on != "never":
            print(
                f"PRXREF_FAIL_ON={fail_on}: review failed before completing; "
                "exiting 1",
                file=sys.stderr,
            )
            return 1
        return 0

    if result is None:
        print(
            f"unrecognized PR URL {args.pr_url!r} — expected a Bitbucket "
            "pull-requests, GitHub pull, or GitLab merge_requests link "
            "(bitbucket.org, github.com, gitlab.com, or a self-hosted "
            "Bitbucket Data Center, GitHub Enterprise Server, or GitLab "
            "host), or an Azure DevOps pullrequest link (dev.azure.com, "
            "*.visualstudio.com, or an Azure DevOps Server host); the URL "
            "must keep the forge's own path shape.",
            file=sys.stderr,
        )
        return 0

    elapsed = time.perf_counter() - t0
    if args.format == "json":
        print(json.dumps(_build_json_result(result)))
    else:
        _print_summary(result, elapsed, verbose=args.verbose)
        if args.no_post or args.verbose:
            _print_findings(result)
    code, note = _fail_on_exit(result, fail_on)
    if note:
        print(note, file=sys.stderr)
    return code


def _cmd_serve(args: argparse.Namespace) -> int:
    # Said once at startup rather than per webhook: _webhook_handler blanks
    # the variable on every review, and an operator who set it should learn
    # that before the first PR arrives, not infer it from unscoped findings.
    if os.environ.get("PRXREF_TICKET_CONTEXT_FILE", "").strip():
        logger.warning(
            "PRXREF_TICKET_CONTEXT_FILE is ignored by prxref serve: one file "
            "cannot describe every PR"
        )
    serve_fn = importlib.import_module("prxref.webhooks").serve
    serve_fn(port=args.port, host=args.host, handler=_webhook_handler)
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    """Route ``eval run|score|compare`` to ``prxref.evals`` and return its exit code.

    ``prxref.evals`` is imported here rather than at module top, because the
    eval modules must never import the CLI back. For the same reason ``run``
    is handed :func:`_run_review` and :func:`_build_json_result` as keyword
    arguments. A ``ConfigError`` from any action exits 2, printed exactly as
    ``review`` prints one.
    """
    evals = importlib.import_module("prxref.evals")
    action = {
        "run": lambda parsed: evals.eval_run(
            parsed, run_review=_run_review, build_record=_build_json_result,
        ),
        "score": evals.eval_score,
        "compare": evals.eval_compare,
    }[args.eval_command]
    try:
        return action(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


def _cmd_trace_render(args: argparse.Namespace) -> int:
    """Render a JSONL trace to a self-contained HTML pipeline view.

    Exit 2 on a missing or unreadable trace, matching the configuration-error
    contract: the operator named a path that is not there, which is the same
    class of mistake as a malformed env var, not a review failure.
    """
    src = Path(args.trace_file)
    if not src.is_file():
        logger.error("trace render: no such trace file: %s", src)
        return 2
    out = Path(args.out) if args.out else src.with_suffix(".html")
    try:
        written = render_file(src, out)
    except OSError as e:
        logger.error("trace render: could not write %s: %s", out, e)
        return 2
    print(written)
    return 0


def _cmd_prompts_export(args: argparse.Namespace) -> int:
    """Write the packaged prompt templates to ``DIR``, printing one written path per line.

    Exit 2 when an existing template would be overwritten without
    ``--force``, or when ``DIR`` cannot be created or written: both are
    ``ConfigError``, reported as ``review`` reports one, and the refusal
    writes nothing.
    """
    try:
        written = export_prompt_templates(args.prompts_export_dir, force=args.force)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    for path in written:
        print(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point dispatching ``review``, ``serve``, or ``--version``."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )

    if getattr(args, "version", False):
        print(prxref.__version__)
        return 0

    if args.command == "review":
        return _cmd_review(args)
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "eval":
        if args.eval_command is not None:
            return _cmd_eval(args)
        parser.print_help(sys.stderr)
        return 2
    if args.command == "trace":
        if args.trace_command == "render":
            return _cmd_trace_render(args)
        parser.print_help(sys.stderr)
        return 2
    if args.command == "prompts":
        if args.prompts_command == "export":
            return _cmd_prompts_export(args)
        parser.print_help(sys.stderr)
        return 2

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    # Without this, ``python -m prxref.cli review ...`` imports the module,
    # runs nothing, and exits 0 — indistinguishable from a review that
    # succeeded silently. The console script (``prxref``) always called main();
    # the module path now agrees with it, exit code included.
    sys.exit(main())
