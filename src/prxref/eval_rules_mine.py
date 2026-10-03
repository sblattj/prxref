"""Leave-fold-out rule mining for ``prxref eval campaign``.

A campaign that scores a team rules file must not let the file see the labels
of the cases it is scored on. This module splits cases into folds and mines a
rules file from the human labels of every fold but one:

1. :func:`assign_folds` maps each case id to a fold. Cases sharing a
   ``pr_url`` (or, without one, an id) share a fold, so two cases from one PR
   never straddle the train/test line.
2. :func:`training_cases` returns the cases outside one fold, the only cases
   :func:`mine_rules` may be handed.
3. :func:`mine_rules` makes one single-shot call over those labels with the
   packaged ``prompts/rules_mine.md`` and returns a markdown rules file that
   ``--rules-file`` accepts, within the ``review_rules_max_chars`` cap
   (:data:`RULES_MAX_CHARS`). The reply is the markdown itself (not
   ``json_mode``); one surrounding code fence is stripped. An empty reply or
   one over the cap is a parse failure, sent again up to ``parse_retries``
   times; still failing, it raises :class:`RuntimeError`.
4. The cache is one JSON file per sha256 of prompt and model under
   ``cache_dir``, written atomically. An entry that cannot be read or no
   longer validates is a miss with a WARNING.

This module never imports :mod:`prxref.cli`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import reviewer
from .config import _DEFAULTS
from .eval_cases import EvalCase, ExpectedFinding
from .llm import LLMClient

logger = logging.getLogger("prxref")

RULES_MINE_PROMPT_NAME = "rules_mine"
RULES_MINE_CACHE_VERSION = 1
RULES_MAX_CHARS: int = _DEFAULTS["review_rules_max_chars"]

_FENCE_RE = re.compile(r"\A```[\w-]*[ \t]*\n(.*?)\n?```[ \t]*\Z", re.DOTALL)


@dataclass(frozen=True)
class MinedRules:
    """A mined rules file and what producing it cost.

    ``text`` is the markdown, stripped and within the size cap. ``model`` is
    the name the backend reported for the last request (the stored one for a
    cache hit). ``input_tokens`` and ``output_tokens`` cover every request
    made, 0 for a hit. ``cost_usd`` is the summed reported cost: ``0.0`` for a
    hit, ``None`` when any request reported none (never ``0``). ``cached`` is
    true when the text came from the cache.
    """

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    cached: bool


def _group_key(case: EvalCase) -> str:
    return case.pr_url if case.pr_url else f"id:{case.id}"


def assign_folds(cases: Sequence[EvalCase], k: int) -> dict[str, int]:
    """Map each case id to a fold ``0..k-1``.

    Cases sharing a ``pr_url`` (or, without one, an id) form one group and
    share a fold. Groups are ordered by the sha256 of their key (ties by the
    key), then each goes to the fold with the fewest cases so far (the lowest
    index on a tie). That is a round-robin when every group has one case, the
    result is the same for the same cases in any input order, no fold is
    empty, and fold sizes differ by at most the size of the largest group. Raises
    ``ValueError`` when ``k`` is not an int below 2 or above the number of
    groups, or when two cases share an id.
    """
    if isinstance(k, bool) or not isinstance(k, int):
        raise ValueError(f"folds must be an integer, got {k!r}")
    groups: dict[str, list[str]] = {}
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"duplicate case id {case.id!r}")
        seen.add(case.id)
        groups.setdefault(_group_key(case), []).append(case.id)
    if k < 2 or k > len(groups):
        raise ValueError(f"folds must be between 2 and the number of PR groups ({len(groups)}), got {k}")
    ordered = sorted(groups, key=lambda key: (hashlib.sha256(key.encode("utf-8")).hexdigest(), key))
    loads = [0] * k
    assignment: dict[str, int] = {}
    for key in ordered:
        fold = loads.index(min(loads))
        loads[fold] += len(groups[key])
        assignment.update((case_id, fold) for case_id in groups[key])
    return assignment


def training_cases(cases: Sequence[EvalCase], folds: Mapping[str, int], fold: int) -> list[EvalCase]:
    """The cases NOT in ``fold``, in input order: the only cases fold ``fold``'s rules may be mined from.

    Raises ``ValueError`` for a case id missing from ``folds``.
    """
    missing = [case.id for case in cases if case.id not in folds]
    if missing:
        raise ValueError(f"cases missing from the fold assignment: {missing}")
    return [case for case in cases if folds[case.id] != fold]


def _label_line(label: ExpectedFinding) -> str:
    parts = [f"- {label.file}:{label.line} [{label.severity}]"]
    if label.category:
        parts.append(f"category={label.category}")
    if label.accepted is not None:
        parts.append(f"accepted={'yes' if label.accepted else 'no'}")
    text = " ".join((label.text or "").split())
    return " ".join(parts) + (f": {text}" if text else "")


def _render_labels(cases: Sequence[EvalCase]) -> str:
    blocks: list[str] = []
    for number, case in enumerate(cases, start=1):
        if not case.expected:
            continue
        blocks.append("\n".join([f"### PR {number}", *map(_label_line, case.expected)]))
    return "\n\n".join(blocks) if blocks else "(no labels)"


def build_mine_prompt(cases: Sequence[EvalCase], max_chars: int = RULES_MAX_CHARS) -> str:
    """The filled ``prompts/rules_mine.md`` for ``cases``: labels grouped by case, numbered ``PR 1..``."""
    template = reviewer.load_prompt(RULES_MINE_PROMPT_NAME)
    return reviewer.fill_template(template, {"max_chars": str(max_chars), "labels": _render_labels(cases)})


def split_mine_prompt(prompt: str) -> tuple[str, str]:
    """Cut a filled prompt at ``## Training labels`` into ``(system, user)``."""
    head, marker, tail = prompt.partition("## Training labels")
    if not marker:
        return "", prompt.strip()
    return head.strip(), (marker + tail).strip()


def _clean_reply(text: str, max_chars: int) -> str:
    """The rules text of a reply; raises ``ValueError`` naming why it is unusable."""
    body = (text or "").strip()
    fenced = _FENCE_RE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    if not body:
        raise ValueError("the reply is empty")
    if len(body) > max_chars:
        raise ValueError(f"the reply is {len(body)} characters, over the {max_chars}-character rules cap")
    return body


def mine_rules(
    cases: Sequence[EvalCase],
    client: LLMClient,
    judge_model: str,
    *,
    cache_dir: Path | None,
    max_tokens: int = 4096,
    timeout_s: float | None = None,
    parse_retries: int = 0,
    max_chars: int = RULES_MAX_CHARS,
) -> MinedRules:
    """Mine a team rules file from the human labels of ``cases`` with one single-shot call.

    ``cases`` must be the training cases only (see :func:`training_cases`).
    ``judge_model`` is stripped and part of the cache key, which is the
    sha256 of the filled prompt and the model. A valid cache entry is used
    with no call. Otherwise ``client.invoke(system, user, max_tokens=...,
    timeout_s=...)`` runs without ``json_mode``; a reply that is empty or
    longer than ``max_chars`` (default :data:`RULES_MAX_CHARS`, the
    ``review_rules_max_chars`` default that ``--rules-file`` enforces) is
    sent again unchanged while fewer than ``parse_retries`` retries have run.
    Still unusable, it raises ``RuntimeError`` with the reason; an exception
    the client raises is never retried and propagates. Only a usable reply is
    cached, atomically; a failed write is a WARNING.
    """
    model = judge_model.strip() if isinstance(judge_model, str) else ""
    prompt = build_mine_prompt(cases, max_chars)
    key = hashlib.sha256((prompt + "\n\x00\n" + model).encode("utf-8")).hexdigest()
    entry_path = Path(cache_dir) / f"{key}.json" if cache_dir is not None else None
    if entry_path is not None:
        hit = _read_entry(entry_path, key, model, max_chars)
        if hit is not None:
            text, stored_model = hit
            return MinedRules(text=text, model=stored_model, input_tokens=0, output_tokens=0, cost_usd=0.0, cached=True)
    system, user = split_mine_prompt(prompt)
    input_tokens = output_tokens = 0
    cost: float | None = 0.0
    retries = 0
    while True:
        result = client.invoke(system, user, max_tokens=max_tokens, timeout_s=timeout_s)
        input_tokens += result.input_tokens
        output_tokens += result.output_tokens
        reported = result.cost_usd
        cost = None if cost is None or reported is None else cost + reported
        try:
            text = _clean_reply(result.text, max_chars)
        except ValueError as exc:
            if retries < parse_retries:
                retries += 1
                logger.warning("rules mining: unusable reply (%s); parse retry %d of %d", exc, retries, parse_retries)
                continue
            raise RuntimeError(f"rules mining failed after {retries} parse retries: {exc}") from exc
        break
    if entry_path is not None:
        _write_entry(entry_path, {
            "version": RULES_MINE_CACHE_VERSION,
            "key": key,
            "model": model,
            "response_model": result.model,
            "text": text,
        })
    return MinedRules(
        text=text, model=result.model, input_tokens=input_tokens, output_tokens=output_tokens,
        cost_usd=cost, cached=False,
    )


def _read_entry(path: Path, key: str, model: str, max_chars: int) -> tuple[str, str] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        return _corrupt(path, f"unreadable ({exc})")
    try:
        entry: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _corrupt(path, f"not JSON ({exc.msg})")
    if not isinstance(entry, dict):
        return _corrupt(path, "not a JSON object")
    if entry.get("version") != RULES_MINE_CACHE_VERSION or entry.get("key") != key or entry.get("model") != model:
        return _corrupt(path, "version, key or model mismatch")
    text = entry.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > max_chars:
        return _corrupt(path, "its text is empty, missing or over the cap")
    stored = entry.get("response_model")
    return text, stored if isinstance(stored, str) else ""


def _corrupt(path: Path, problem: str) -> None:
    logger.warning("rules-mining cache entry %s is corrupt (%s); treating it as a miss", path, problem)
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
        logger.warning("rules-mining cache write to %s failed (the rules stand): %s", path, exc)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
