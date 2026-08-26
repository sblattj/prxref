"""prxref command-line interface.

Provides two subcommands:
  * ``review --pr-url URL`` — one-shot PR/MR review from a forge URL.
  * ``serve [--port N] [--host H]`` — webhook listener daemon.

Non-blocking doctrine: ``review`` exits 0 on all review errors (empty diffs,
network failures, LLM timeouts, bad credentials), printing diagnostic notes to
stderr so a pipeline step never fails the build over an advisor's error. The one
exception is a missing-configuration error, which is a usage error rather than a
review outcome and exits 2.
"""
from __future__ import annotations

import argparse
import importlib
import logging
import sys
import time
from typing import Any

import prxref
from prxref.config import load_config, make_forge
from prxref.forges.base import detect_forge
from prxref.llm import ConfigError

logger = logging.getLogger("prxref")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prxref",
        description="Fast automated AI code review for Bitbucket, GitLab, and GitHub.",
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
        help="full URL of the PR or MR on Bitbucket, GitHub, or GitLab",
    )
    rev.add_argument(
        "--no-post",
        action="store_true",
        help="dry run: do not post comments to the forge",
    )
    rev.add_argument(
        "--max-chunks",
        type=int,
        default=None,
        help="override the maximum number of diff chunks to review",
    )
    rev.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print timing, token, and findings breakdown to stdout",
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


def _print_summary(
    result: Any,
    elapsed_s: float,
    *,
    verbose: bool,
    out=None,
) -> None:
    target = sys.stdout if out is None else out
    verdict = result.get("verdict") if isinstance(result, dict) else result
    print(f"verdict: {verdict if verdict is not None else 'done'}", file=target)
    failed = result.get("chunks_failed", 0) if isinstance(result, dict) else 0
    if failed:
        reviewed = result.get("chunks_reviewed", 0)
        print(f"coverage: {reviewed}/{reviewed + failed} chunks reviewed", file=target)
    if not verbose:
        return
    dropped = result.get("findings_dropped", []) if isinstance(result, dict) else []
    dropped = len(dropped) if isinstance(dropped, list) else 0
    print(f"counts: {_fmt_counts(result)} (dropped: {dropped})", file=target)
    print(f"elapsed: {elapsed_s:.1f}s tokens: {_fmt_tokens(result)}", file=target)


def _run_review(
    url: str,
    *,
    post: bool = True,
    max_chunks: int | None = None,
    config: dict | None = None,
) -> Any:
    ref = detect_forge(url)
    if ref is None:
        return None
    cfg = config if config is not None else load_config(max_chunks=max_chunks)
    forge = make_forge(ref)
    llm = importlib.import_module("prxref.llm_backends").create_llm_client(cfg)
    orchestrate = importlib.import_module("prxref.orchestrator").orchestrate_review
    return orchestrate(
        forge=forge,
        ref=ref,
        llm=llm,
        post=post,
        max_chunks=max_chunks if max_chunks is not None else cfg.get("MAX_CHUNKS", 8),
    )


def _webhook_handler(url: str) -> None:
    try:
        _run_review(url, post=True)
    except Exception:
        logger.exception("webhook review failed for %s", url)


def _cmd_review(args: argparse.Namespace) -> int:
    t0 = time.perf_counter()
    try:
        result = _run_review(
            args.pr_url,
            post=not args.no_post,
            max_chunks=args.max_chunks,
        )
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"review failed: {exc}", file=sys.stderr)
        logger.debug("review failed with traceback", exc_info=True)
        return 0

    if result is None:
        print(
            f"unrecognized PR URL {args.pr_url!r} — expected bitbucket.org, "
            "github.com, or gitlab.com PR/MR link",
            file=sys.stderr,
        )
        return 0

    elapsed = time.perf_counter() - t0
    _print_summary(result, elapsed, verbose=args.verbose)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    serve_fn = importlib.import_module("prxref.webhooks").serve
    serve_fn(port=args.port, host=args.host, handler=_webhook_handler)
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

    parser.print_help(sys.stderr)
    return 2
