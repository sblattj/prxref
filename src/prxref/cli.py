"""prxref command-line interface.

Provides three subcommands:
  * ``review --pr-url URL`` — one-shot PR/MR review from a Bitbucket, GitHub,
    GitLab, or Azure DevOps URL (Cloud or self-hosted).
  * ``serve [--port N] [--host H]`` — webhook listener daemon.
  * ``trace render FILE`` — a JSONL run trace to a standalone HTML view.

``review`` takes three optional inputs besides the PR itself, and they
compose: ``--spec URL_OR_PATH`` (repeatable) grounds the review against specs
or tickets and replaces ``PRXREF_SPEC_SOURCES``; ``--rules-file PATH`` adds a
team review-rules file (``PRXREF_REVIEW_RULES``); ``--context-file PATH``
names the ticket the PR implements (``PRXREF_TICKET_CONTEXT_FILE``), so each
finding is marked in, out of, or of unknown ticket scope. Each flag wins over
its variable, and ``--rules-file ""`` / ``--context-file ""`` turn the
variable off for one run. Both files are read before any network call, so an
unusable one is a configuration error. The webhook daemon reads the rules
file from its own environment and never reads a ticket-context file. The
replay flags (``--base-sha`` / ``--head-sha``, ``--no-threads``,
``--diff-file``) review a pinned, reproducible input for evaluation, and a
replay never posts. In this build the rules and ticket-context loaders and
replay mode fail closed: a non-empty path or any replay flag exits 2.

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
exits 1 on any active finding; and under either value a review that fails to
complete also exits 1, because a gate that silently passes on a broken run is
worse than none. An unrecognized PR URL still exits 0 under every value —
nothing was reviewed, so there is no outcome to gate on. The webhook daemon
has no exit code and is unaffected by the knob.

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
import importlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import prxref
from prxref.config import load_config, make_forge
from prxref.costs import cost_label
from prxref.forges.base import detect_forge
from prxref.llm import ConfigError
from prxref.rules import load_review_rules
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
        required=True,
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

    ``costs.cost_label`` of the record's ``cost_usd`` and ``cost_estimated``
    (``$0.0007``, ``~$0.0007 (est.)``, or ``cost unknown`` for ``None``), and
    ``-`` when the result carries no ``cost_usd`` key at all. An absent key
    means nothing measured the cost; ``None`` means it was measured and no
    source could price it. The two are different claims, so they print
    differently.
    """
    if not isinstance(result, dict) or "cost_usd" not in result:
        return "-"
    return cost_label(result.get("cost_usd"), result.get("cost_estimated") is True)


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
    Under ``-v`` it adds the finding counts, the ``elapsed/tokens/cost`` line,
    and one line for each configured input: ``rules:``, ``ticket:`` (with the
    active findings' scope counts), and ``spec:``. ``result`` may be partial,
    or not a dict at all; a missing or ``None`` record prints nothing.
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
        print(
            f"replay: base={_dash(replay.get('base_sha'), 12)} head={_dash(replay.get('head_sha'), 12)} "
            f"threads={_dash(replay.get('threads'))} diff_file={_dash(replay.get('diff_file'))}",
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
    """Render one active finding as ``<severity> <file>:<line> <title> (confidence 0.NN)``."""
    severity = getattr(f, "severity", None) or ""
    location = f"{getattr(f, 'file', '')}:{getattr(f, 'line', 0)}"
    title = getattr(f, "title", None) or ""
    confidence = getattr(f, "confidence", None) or 0.0
    return f"{severity} {location} {title} (confidence {confidence:.2f})"


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
    """
    return {
        "file": f.file,
        "line": f.line,
        "severity": f.severity,
        "confidence": f.confidence,
        "scope": getattr(f, "scope", "unknown"),
        "title": f.title,
        "body": f.body,
        "drop_reason": drop_reason,
    }


def _build_json_result(result: Any) -> dict:
    """Build the single JSON payload for ``--format json``.

    Key order: ``verdict``, ``findings``, ``chunk_count``, ``chunks_reviewed``,
    ``chunks_failed``, ``elapsed_ms``, ``input_tokens``, ``output_tokens``,
    ``cost_usd``, ``cost_estimated``, ``posted``, ``review_rules``,
    ``ticket_context``, ``spec_grounding``, ``size_advisory``, then
    ``sampling`` and ``replay`` when present.

    Tolerates an error-shaped or partial result (a dict missing keys, as an
    incomplete or failed run may return): every always-present key defaults
    to ``None`` and ``findings`` defaults to ``[]`` rather than raising. The
    run-record keys new in 0.14 (``cost_usd`` through ``size_advisory``) are
    always emitted and are ``null`` when their feature is off; ``cost_usd`` is
    also ``null`` when no source could price the run, never ``0``.
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
    }
    if "sampling" in result:
        payload["sampling"] = result["sampling"]
    if "replay" in result:
        payload["replay"] = result["replay"]
    return payload


def _resolve_replay(
    url: str | None,
    *,
    base_sha: str | None = None,
    head_sha: str | None = None,
    no_threads: bool = False,
    diff_file: str | None = None,
) -> None:
    """Validate the replay flags; ``None`` means a normal, non-replay run.

    Pure: it reads nothing and calls nothing. ``_run_review`` calls it first,
    before ``detect_forge``, so a bad set of replay flags exits 2 even next
    to an unrecognised URL.

    Replay mode is not wired in this build, so any replay flag, even with an
    empty value, raises a ``ConfigError`` naming the flags given (exit 2)
    rather than silently reviewing, and posting to, the live PR.
    """
    given = [
        flag
        for flag, value in (
            ("--base-sha", base_sha),
            ("--head-sha", head_sha),
            ("--no-threads", no_threads),
            ("--diff-file", diff_file),
        )
        if value is not None and value is not False
    ]
    if given:
        raise ConfigError(f"{'/'.join(given)}: replay mode is not wired in this build")
    return None


def _load_text_input(loader: Any, path: str, *, max_chars: int, source: str) -> Any:
    """Run the rules or ticket-context ``loader``, fencing every failure into a ``ConfigError``.

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


def _run_review(
    url: str,
    *,
    post: bool = True,
    max_chunks: int | None = None,
    timeout: float | None = None,
    trace_dir: str | None = None,
    spec_sources: list[str] | None = None,
    rules_file: str | None = None,
    context_file: str | None = None,
    base_sha: str | None = None,
    head_sha: str | None = None,
    no_threads: bool = False,
    diff_file: str | None = None,
) -> Any:
    replay = _resolve_replay(
        url, base_sha=base_sha, head_sha=head_sha, no_threads=no_threads,
        diff_file=diff_file,
    )
    ref = detect_forge(url)
    if ref is None:
        return None
    # --max-chunks, --timeout, --spec, --rules-file and --context-file arrive
    # as load_config overrides (None is ignored, "" is not), so each flag rides
    # exactly the path its environment variable does: --max-chunks and
    # --timeout are range-checked on the same pass as PRXREF_MAX_CHUNKS and
    # PRXREF_LLM_TIMEOUT, --spec replaces PRXREF_SPEC_SOURCES wholesale rather
    # than merging with it, and --rules-file "" / --context-file "" blank
    # their variable for one run. Precedence is derived once, here. There is
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
        ticket_context_file=context_file,
        # The operator typed a flag, so a rejection has to name the flag. Only
        # the CLI knows that spelling; config takes the label and reports it.
        source_labels={
            "max_chunks": "--max-chunks",
            "llm_timeout": "--timeout",
            "spec_sources": "--spec",
            "review_rules": "--rules-file",
            "ticket_context_file": "--context-file",
        },
    )
    # Both files are read here, after config and before make_forge and the LLM
    # client, so an unusable one exits 2 before any network I/O. load_config
    # stays I/O-free. Each is reported under the input that supplied its
    # path: the flag whenever it was given, else the variable.
    rules = _load_text_input(
        load_review_rules, cfg["review_rules"],
        max_chars=cfg["review_rules_max_chars"],
        source="--rules-file" if rules_file is not None else "PRXREF_REVIEW_RULES",
    )
    ticket = _load_text_input(
        load_ticket_context, cfg["ticket_context_file"],
        max_chars=cfg["ticket_context_max_chars"],
        source=(
            "--context-file" if context_file is not None else "PRXREF_TICKET_CONTEXT_FILE"
        ),
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
    forge = make_forge(ref)
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
        replay=replay,
    )


def _webhook_handler(url: str) -> None:
    """Review one webhook-delivered PR, posting unless PRXREF_DRY_RUN is set.

    ``post=True`` is the daemon's intent, not its last word: ``_run_review``
    downgrades it when the configured dry run says so, which is the only way to
    observe the daemon against a real repo without writing to it.

    ``context_file=""`` blanks ``PRXREF_TICKET_CONTEXT_FILE`` for every
    webhook: one static ticket file cannot describe every PR the daemon sees,
    so its findings always carry scope ``unknown``. The team rules file still
    comes from the daemon's environment, re-read on every webhook. The daemon
    passes no replay flag, so it never replays.
    """
    try:
        _run_review(url, post=True, context_file="")
    except Exception:
        logger.exception("webhook review failed for %s", url)


def _fail_on_exit(result: Any, fail_on: str) -> tuple[int, str | None]:
    """The exit code a completed review earns under the ``fail_on`` policy.

    Severity is compared exactly as the verdict is built in the orchestrator
    (``Request-Changes`` iff an active finding has severity ``error``), so the
    gate and the posted verdict can never disagree about what counts. A result
    without parseable findings is tolerated the way ``_fmt_counts`` tolerates
    one: nothing countable means nothing to gate on.

    Returns the exit code and, when the gate fires, the stderr line that says
    why — silence would read as a crash rather than a decision.
    """
    if fail_on == "never":
        return 0, None
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
            context_file=args.context_file,
            base_sha=args.base_sha,
            head_sha=args.head_sha,
            no_threads=args.no_threads,
            diff_file=args.diff_file,
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
    if args.command == "trace":
        if args.trace_command == "render":
            return _cmd_trace_render(args)
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
