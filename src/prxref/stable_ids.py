"""Stable finding ids and the id-assignment pass that stamps them (#71).

A finding's identity today is its ``(file, line)`` plus its title — both
move between runs. Issue #71 asks for an id that survives a reworded
title and a small anchor drift, so a verdict stored against it in one run
still applies in the next.

The id is deterministic and content-derived, never model- or
embedding-derived (the pipeline has no model on this path by design):

- the file path, as the finding carries it;
- the rule label the finding names, casefolded with whitespace collapsed,
  or ``norule`` when it names none;
- a 12-hex claim hash: the sha256 of the finding title's content tokens
  (``quality._title_tokens``: lowercase ``[a-z0-9]+`` words of three or
  more characters, minus the shared stopword list, hyphens split) taken
  as a SORTED set joined by single spaces, so a rewording that merely
  reorders or re-punctuates the claim keeps the id. A synonym swap does
  not; :func:`apply_stable_ids` bridges that with
  :func:`quality.titles_similar` over findings of one run and over the
  titles the verdict store recorded for the same file and rule in an
  earlier run, recorded in ``id_reused_from``, never inside the id
  itself.

``anchor_block`` is the smallest enclosing name at the finding's line —
a function or type name, a YAML key path, a manifest dependency key —
computed from the parsed diff alone. It is deliberately NOT part of the
id: the acceptance in issue #71 wants the id stable when the anchor
moves one key, so the block travels as metadata (and as a thread-matching
signal), while the id stays anchor-free.

:func:`apply_stable_ids` runs after both thread gates and before the
quality gate, stamps ``id``, ``anchor_block`` and ``id_reused_from`` on
every finding, and drops a finding whose id a loaded verdict store holds
as refuted in an earlier run. The pass always runs; the
``PRXREF_STABLE_IDS`` knob that once turned it off is deprecated and
ignored. A store entry a 0.30.0 run recorded — keyed by the claim hash
before it stemmed words, and carrying no ``title`` — still matches
through :func:`legacy_claim_hash`.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import PurePosixPath

from .forges.base import Thread
from .quality import (
    MANIFEST_BASENAMES,
    _line_json_key,
    _normalised_path,
    _title_tokens,
    previously_discussed_thread,
    title_similarity,
    titles_similar,
)
from .repo_context import definition_regexes, language_of
from .repo_contracts import _Yaml
from .triage import FileDiff, Finding, Hunk

logger = logging.getLogger(__name__)

#: How many hex characters of the claim hash travel in the id. 12 (48
#: bits) keeps a run's collision odds negligible while the id stays
#: readable in a comment thread; a within-run collision is reported, not
#: silently disambiguated, by :func:`apply_stable_ids`.
CLAIM_HASH_CHARS: int = 12

#: The rule component a finding that names no rule carries. A fixed
#: word, not the empty string, so the id's ``#`` separators stay
#: unambiguous.
NO_RULE: str = "norule"

#: The Jaccard similarity at which :func:`apply_stable_ids` proposes that
#: two findings of one run and one file restate the same claim whose
#: titles a synonym swap pushed apart. The shared-token floor of
#: :func:`quality.titles_similar` still applies on top of it.
REUSE_SIMILARITY: float = 0.6

#: What ``id_reused_from`` records: the id was proposed by a
#: near-identical earlier finding of the SAME run (``run``), matched a
#: verdict the store holds from an EARLIER run (``verdict``), or matched
#: an existing thread (``thread`` — matching only this release; reading
#: the id back out of the posted body is deferred).
REUSED_FROM_RUN = "run"
REUSED_FROM_VERDICT = "verdict"
REUSED_FROM_THREAD = "thread"


_SUFFIXES: tuple[str, ...] = ("ing", "ed", "es", "s", "e")


def _stem(word: str) -> str:
    """Light suffix stemming so inflections of one word hash alike.

    Strips one of ``ing``, ``ed``, ``es``, ``s``, ``e`` (first match,
    longest first), keeping at least three characters and never stripping
    the ``s`` of a ``ss`` ending. "derived", "derive", "deriving" and
    "derives" all become "deriv"; "access" stays whole.
    """
    for suffix in _SUFFIXES:
        if not word.endswith(suffix) or len(word) - len(suffix) < 3:
            continue
        if suffix == "s" and word.endswith("ss"):
            continue
        return word[: -len(suffix)]
    return word


def claim_hash(title: str) -> str:
    """The order-insensitive claim hash of a finding title.

    ``quality._title_tokens`` gives the content words (lowercase, three
    or more characters, minus stopwords and remedy verbs, hyphens
    split); sorting them makes the hash a function of the claim's word
    SET, so "Drain duration hardcoded" and "Hardcoded drain duration"
    hash identically; :func:`_stem` folds inflections ("derived" and
    "derive") into one token. An empty token set hashes the empty string — a
    title of stopwords only still gets an id, just one shared with every
    other stopword-only title of that file and rule.
    """
    words = " ".join(sorted({_stem(token) for token in _title_tokens(title)}))
    return hashlib.sha256(words.encode("utf-8")).hexdigest()[:CLAIM_HASH_CHARS]


def legacy_claim_hash(title: str) -> str:
    """The claim hash prxref 0.30.0 computed: :func:`claim_hash` without stemming.

    0.30.0 hashed the sorted content tokens as they stood, so any title
    holding an inflected word ("Drain duration is hardcoded") carries a
    different id there than here. :func:`_store_match` looks a finding up
    under this hash too, so a verdict a 0.30.0 run recorded — an entry
    with no ``title`` for the rewording bridge to read — keeps matching.
    """
    words = " ".join(sorted(_title_tokens(title)))
    return hashlib.sha256(words.encode("utf-8")).hexdigest()[:CLAIM_HASH_CHARS]


def finding_id(finding: Finding) -> str:
    """The stable id of a finding: ``<file>#<rule|norule>#<claim hash>``.

    The rule label is casefolded with whitespace collapsed, mirroring the
    grouping key, so a team rule spelled with different case in two runs
    still finds its own findings. The anchor block and the line are
    deliberately absent (see the module docstring).
    """
    return f"{_id_prefix(finding)}{claim_hash(finding.title)}"


def _id_prefix(finding: Finding) -> str:
    """The ``<file>#<rule|norule>#`` head every id of this file and rule shares."""
    rule = " ".join((finding.rule or "").split()).casefold() or NO_RULE
    return f"{_normalised_path(finding.file)}#{rule}#"


def _post_lines(hunks: Sequence[Hunk]) -> list[tuple[int, str]]:
    """The post-image lines of ``hunks`` as ``(new_line, text)``, in order."""
    out: list[tuple[int, str]] = []
    for hunk in hunks:
        for ln in hunk.lines:
            if ln.kind != "-" and ln.new_line is not None:
                out.append((ln.new_line, ln.text))
    out.sort()
    return out


def _function_anchor(path: str, lines: Sequence[tuple[int, str]], line: int) -> str | None:
    """``symbol:<name>`` of the last definition at or above ``line``, or None.

    Scans the post-image diff lines the file's hunks carry, so the anchor
    exists only for definitions the diff itself shows — enough for a
    finding anchored on a changed line, which is every finding the
    passes upstream did not demote to file level. Uses
    :func:`prxref.repo_context.definition_regexes`, the same regexes the
    chunk context extracts definitions with; a language with no regexes
    answers ``None``.
    """
    regexes = definition_regexes(language_of(path))
    if not regexes:
        return None
    name: str | None = None
    for number, text in lines:
        if number >= line:
            break
        for regex in regexes:
            match = regex.match(text)
            if match:
                name = match.group(1)
                break
    return f"symbol:{name}" if name else None


def _yaml_anchor(lines: Sequence[tuple[int, str]], line: int) -> str | None:
    """``yaml:<dotted.key.path>`` of the mapping keys enclosing ``line``.

    The visible lines are re-joined into one sparse document (unknown
    lines blank) and read through ``repo_contracts._Yaml``, the same
    per-line indentation facts the contract excerpts use; the path walks
    inward from column 0 through every key whose block covers ``line``,
    ending at the tightest one the visible lines prove.
    """
    if not lines:
        return None
    first, last = lines[0][0], lines[-1][0]
    if not first <= line <= last:
        return None
    known = dict(lines)
    rebuilt = [known.get(number, "") for number in range(first, last + 1)]
    facts = _Yaml("\n".join(rebuilt))
    index = line - first
    path: list[tuple[str, int]] = []
    for i in range(index + 1):
        if facts.filler[i] or facts.scalar[i] or facts.key[i] is None:
            continue
        while path and facts.indent[path[-1][1]] >= facts.indent[i]:
            path.pop()
        path.append((facts.key[i], i))
    if not path:
        return None
    return "yaml:" + ".".join(key for key, _ in path)


def _manifest_anchor(hunks: Sequence[Hunk], line: int) -> str | None:
    """``manifest:<key>`` of the dependency key the anchored line declares.

    Reuses ``quality._line_json_key``, so a section header or a bare
    brace — a line that declares no dependency — answers ``None``.
    """
    for hunk in hunks:
        for ln in hunk.lines:
            if ln.kind != "-" and ln.new_line == line:
                key = _line_json_key(ln)
                return f"manifest:{key}" if key else None
    return None


def anchor_block(files: Sequence[FileDiff], finding: Finding) -> str | None:
    """The smallest enclosing name at ``finding.line`` in the file's diff.

    Priority: a function or type definition (``symbol:<name>``), a YAML
    mapping key path (``yaml:a.b``), a manifest dependency key
    (``manifest:<name>``); ``None`` when the file is not in the diff, the
    finding is file-level (line 0), its line is not a post-image line of
    the diff, or nothing encloses the line. Computed from the parsed
    diff alone — no repository read — and never part of the id.
    """
    if finding.line <= 0:
        return None
    diff = next((f for f in files if f.path == finding.file), None)
    if diff is None:
        return None
    lines = _post_lines(diff.hunks)
    if not any(number == finding.line for number, _ in lines):
        return None
    lowered = finding.file.lower()
    if lowered.endswith((".yml", ".yaml")):
        return _yaml_anchor(lines, finding.line)
    if PurePosixPath(finding.file).name in MANIFEST_BASENAMES:
        return _manifest_anchor(diff.hunks, finding.line)
    return _function_anchor(finding.file, lines, finding.line)


def _store_entry(
    store: Mapping[str, object] | None, fid: str,
) -> Mapping[str, object] | None:
    """The store's entry for one id, or ``None`` for no store or no entry."""
    if not isinstance(store, Mapping):
        return None
    verdicts = store.get("verdicts")
    if not isinstance(verdicts, Mapping):
        return None
    entry = verdicts.get(fid)
    return entry if isinstance(entry, Mapping) else None


def _store_match(
    store: Mapping[str, object] | None, finding: Finding, fid: str,
) -> tuple[str, Mapping[str, object]] | None:
    """The ``(id, entry)`` of the store that answers ``finding``, or ``None``.

    An entry under ``fid`` itself wins. Next, the entry under the id
    prxref 0.30.0 gave the finding (its ``<file>#<rule>#`` head plus
    :func:`legacy_claim_hash` of its title), so a verdict recorded
    before the claim hash stemmed its words still matches. On a miss,
    the cross-run rewording bridge: every entry whose id shares the
    finding's ``<file>#<rule>#`` head and whose recorded ``title`` is a
    reworded restatement of the finding's (:func:`quality.titles_similar`
    at :data:`REUSE_SIMILARITY`) is a candidate, and the one with the
    highest title Jaccard wins, ties broken by the smaller id so the
    match never depends on the store's key order. An entry recorded
    without a title matches by exact id or by that 0.30.0 id only.
    """
    entry = _store_entry(store, fid)
    if entry is not None:
        return fid, entry
    legacy = f"{_id_prefix(finding)}{legacy_claim_hash(finding.title)}"
    entry = _store_entry(store, legacy)
    if entry is not None:
        return legacy, entry
    if not isinstance(store, Mapping):
        return None
    verdicts = store.get("verdicts")
    if not isinstance(verdicts, Mapping):
        return None
    prefix = _id_prefix(finding)
    best: tuple[float, str, Mapping[str, object]] | None = None
    for sid, candidate in verdicts.items():
        if not (isinstance(sid, str) and sid.startswith(prefix) and isinstance(candidate, Mapping)):
            continue
        title = candidate.get("title")
        if not isinstance(title, str) or not titles_similar(title, finding.title, REUSE_SIMILARITY):
            continue
        score = title_similarity(title, finding.title)[0]
        if best is None or score > best[0] or (score == best[0] and sid < best[1]):
            best = (score, sid, candidate)
    return (best[1], best[2]) if best is not None else None


def apply_stable_ids(
    findings: Sequence[Finding],
    files: Sequence[FileDiff],
    store: Mapping[str, object] | None = None,
    threads: Sequence[Thread] = (),
) -> list[Finding]:
    """Stamp ``id``, ``anchor_block`` and ``id_reused_from`` on every finding.

    Runs after both thread gates and before the quality gate (#71). For
    each finding, in order:

    1. compute :func:`finding_id` and :func:`anchor_block`;
    2. when the id is new, an earlier ACTIVE finding of the same file
       whose title is near-identical (:func:`quality.titles_similar` at
       :data:`REUSE_SIMILARITY`) proposes its own id — the synonym-swap
       case the sorted-token hash cannot see — recorded as
       ``id_reused_from="run"``;
    3. when the verdict store holds that id — or, on a miss, the id a
       0.30.0 run gave the finding, or an entry of the same file and
       rule whose recorded title is a reworded restatement of this one
       (:func:`_store_match`), whose id the
       finding then takes over — record ``id_reused_from="verdict"``; a
       ``refuted`` entry drops the finding with
       ``drop_reason="refuted in earlier run (<id>)"``;
    4. else, when a resolved or outdated thread matches the finding
       under the previously-raised rule
       (:func:`quality.previously_discussed_thread`), record
       ``id_reused_from="thread"`` (the thread itself carries no id this
       release; that bridge is deferred).

    Ids are never rewritten by a thread match, only reused from an
    earlier finding of the same run or from a store entry. Two findings of one run that
    share an id but sit at different ``(anchor_block, line)`` are a
    collision: one WARNING names the id, and both keep it — the id stays
    honest to its rule, and the operator decides. Already-dropped
    findings get their id and anchor but no reuse and no store drop, so
    the reason an earlier pass gave survives. Pure, 1:1 and
    order-preserving.
    """
    seen: list[Finding] = []
    out: list[Finding] = []
    for f in findings:
        fid = finding_id(f)
        anchor = anchor_block(files, f)
        reused: str | None = None
        if f.drop_reason is None:
            if not any(p.id == fid for p in seen):
                near = next(
                    (p for p in seen
                     if p.file == f.file and p.drop_reason is None
                     and titles_similar(p.title, f.title, REUSE_SIMILARITY)),
                    None,
                )
                if near is not None:
                    fid = near.id or fid
                    reused = REUSED_FROM_RUN
            match = _store_match(store, f, fid)
            if match is not None:
                fid, entry = match
                reused = REUSED_FROM_VERDICT
                label = entry.get("verdict")
                norm = label.strip().casefold() if isinstance(label, str) else None
                if norm == "refuted":
                    f = replace(f, drop_reason=f"refuted in earlier run ({fid})")
                elif norm != "accepted":
                    logger.warning(
                        "verdict store entry %s has unrecognised verdict %r; ignored",
                        fid, label,
                    )
            elif threads and previously_discussed_thread(f, threads):
                reused = REUSED_FROM_THREAD
        stamped = replace(f, id=fid, anchor_block=anchor, id_reused_from=reused)
        out.append(stamped)
        seen.append(stamped)
    _warn_collisions(out)
    return out


def stable_id_collisions(findings: Sequence[Finding]) -> list[str]:
    """Ids two findings of one run share while sitting at different places.

    An id collides when the findings carrying it differ in
    ``(anchor_block, line)`` — the same claim raised twice at genuinely
    different sites, or two claims whose token sets happened to hash
    alike. Each colliding id is listed once, sorted; the count feeds the
    run record's ``stable_ids`` block.
    """
    sites: dict[str, set[tuple[str | None, int]]] = {}
    for f in findings:
        if f.id is None:
            continue
        sites.setdefault(f.id, set()).add((f.anchor_block, f.line))
    return sorted(fid for fid, spots in sites.items() if len(spots) > 1)


def _warn_collisions(findings: Sequence[Finding]) -> None:
    """One WARNING per id two differently-placed findings share; ids stand."""
    for fid in stable_id_collisions(findings):
        logger.warning(
            "stable id %r is shared by findings at different anchors; both keep the id", fid,
        )
