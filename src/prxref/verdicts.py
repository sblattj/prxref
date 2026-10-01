"""The persisted verdict store: review verdicts keyed by stable finding id (#71).

Issue #71's second half. A reviewer who refutes a finding — the code is
fine, the claim is wrong, the behaviour is intended — has no way to say
so that the NEXT run remembers: PR threads carry the discussion, but
nothing maps a thread onto the finding a future review will raise. The
store is that map: a small JSON document holding one entry per stable id
(:mod:`prxref.stable_ids`), each naming the verdict a human or an
operator recorded, the commit and run it was recorded against, and when.

Shape::

    {"version": 1,
     "verdicts": {"<stable id>": {"verdict": "refuted" | "accepted",
                                  "title": "<finding title>",
                                  "commit": "<sha>", "run": "<label>",
                                  "created_at": "<ISO 8601 UTC>"}}}

The pipeline only READS the store: ``prxref review`` loads it when
``PRXREF_VERDICT_STORE`` names a path and drops a finding whose id it
holds as ``refuted`` (``refuted in earlier run (<id>)``, see
:func:`prxref.stable_ids.apply_stable_ids`). An entry's ``title`` lets a
later run bridge a rewording: a finding whose own id misses the store
takes over the id of an entry of the same file and rule whose title it
restates. An entry without a title (one written before the field
existed) still matches by exact id. Recording is a caller's
decision — a script, a future UI — through :func:`record`, which merges
into whatever the path already holds and writes it back atomically, so
two runs never clobber each other's entries. With no path configured
(the default) nothing is loaded, nothing is written, and no id matches.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from .llm import ConfigError
from .stable_ids import finding_id

#: The store format version. A file with any other version is refused
#: rather than guess-read, so an upgrade can migrate it deliberately.
STORE_VERSION: int = 1

#: The verdict that suppresses a finding in a later run.
REFUTED: str = "refuted"

#: The verdict that says the finding stood; recorded for the audit trail
#: and matched for ``id_reused_from``, but it drops nothing.
ACCEPTED: str = "accepted"

VERDICTS: frozenset[str] = frozenset({REFUTED, ACCEPTED})


def empty_store() -> dict[str, object]:
    """The store of a first run: the current version, no verdicts."""
    return {"version": STORE_VERSION, "verdicts": {}}


def load(path: str | Path) -> dict[str, object]:
    """Read the store at ``path``; a missing file is an empty store.

    A file that is not valid JSON, not a JSON object, whose ``version``
    is not :data:`STORE_VERSION`, or whose ``verdicts`` is not an object
    of objects raises :class:`~prxref.llm.ConfigError` naming the path —
    a store the pass cannot trust must stop the run before it reviews,
    not silently count as empty.
    """
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty_store()
    except OSError as exc:
        raise ConfigError(f"verdict store {str(file)!r} cannot be read: {exc}") from exc
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"verdict store {str(file)!r} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or parsed.get("version") != STORE_VERSION:
        raise ConfigError(
            f"verdict store {str(file)!r}: version {parsed.get('version') if isinstance(parsed, dict) else None!r}"
            f" is not {STORE_VERSION}"
        )
    verdicts = parsed.get("verdicts")
    if not isinstance(verdicts, dict) or not all(
        isinstance(entry, dict) for entry in verdicts.values()
    ):
        raise ConfigError(f"verdict store {str(file)!r}: 'verdicts' is not an object of objects")
    return parsed


def record(
    path: str | Path,
    findings: Sequence[object],
    verdicts: Mapping[str, str],
    *,
    commit: str = "",
    run: str = "",
    created_at: str | None = None,
) -> dict[str, object]:
    """Merge ``verdicts`` for a run's findings into the store at ``path``.

    ``verdicts`` maps a stable id to its verdict label
    (:data:`REFUTED` or :data:`ACCEPTED`). Every key must name a finding
    of ``findings`` (its :func:`prxref.stable_ids.finding_id`), so a
    typo'd id can never plant a verdict nothing will ever match, and
    every value must be a known label. The store the path already holds
    is loaded and merged — an id both runs recorded keeps the newer
    verdict — each entry carrying the finding's ``title`` for the
    cross-run rewording match — and the result written back through a temp file and
    ``os.replace``, then returned. A parent directory that does not
    exist is created, so the natural first call on a fresh checkout
    works.
    """
    titles: dict[str, str] = {}
    for f in findings:
        fid = getattr(f, "id", None)
        title = getattr(f, "title", "")
        titles.setdefault(fid if isinstance(fid, str) else finding_id(f), title if isinstance(title, str) else "")
    unknown = sorted(set(verdicts) - set(titles))
    if unknown:
        raise ConfigError(
            f"verdict store: no finding of this run carries the stable id(s) {', '.join(unknown)}"
        )
    bad = sorted({value for value in verdicts.values() if value not in VERDICTS})
    if bad:
        raise ConfigError(
            f"verdict store: verdict(s) {', '.join(map(repr, bad))} not one of "
            f"{', '.join(sorted(VERDICTS))}"
        )
    store = load(path)
    entries = store["verdicts"]
    stamp = created_at or datetime.now(UTC).isoformat(timespec="seconds")
    for fid, verdict in sorted(verdicts.items()):
        entries[fid] = {
            "verdict": verdict, "title": titles[fid],
            "commit": commit, "run": run, "created_at": stamp,
        }
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_name(file.name + ".tmp")
    tmp.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, file)
    return store


def lookup(store: Mapping[str, object] | None, finding: object) -> tuple[str, Mapping[str, object]] | None:
    """The ``(id, entry)`` the store holds for ``finding``, or ``None``.

    The finding's own ``id`` field is the key — the id the run's
    :func:`prxref.stable_ids.apply_stable_ids` stamped, or one a caller
    computed with :func:`prxref.stable_ids.finding_id`. A finding with
    no id, a store without an entry for it, or no store at all answer
    ``None``; the caller decides what that means.
    """
    fid = getattr(finding, "id", None)
    if not isinstance(fid, str) or not isinstance(store, Mapping):
        return None
    verdicts = store.get("verdicts")
    if not isinstance(verdicts, Mapping):
        return None
    entry = verdicts.get(fid)
    return (fid, entry) if isinstance(entry, Mapping) else None
