"""Building blocks for running one eval dataset as many independent units.

Internal helpers for the campaign runner; there is no CLI flag and no user
documentation. A *unit* is one case's directory ``cases/<id>/`` inside a run
written by :func:`prxref.evals.eval_run`. The helpers split a dataset into
subset ``cases.json`` files (:func:`write_subset_cases`, :func:`shard_ids`),
classify what a unit left on disk (:func:`unit_state`), make a unit run again
under ``eval run --resume`` (:func:`reset_unit`) and join the shards of one
logical run back into a single run that ``eval score`` reads
(:func:`merge_runs`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from prxref.eval_cases import case_to_json, load_cases

RATE_LIMIT_RE = re.compile(
    r"\b429\b|rate[ _-]?limit|too many requests|quota|session (?:cap|limit)", re.IGNORECASE
)
UNIT_OK = "ok"
UNIT_ERROR = "error"
UNIT_RATE_LIMITED = "rate_limited"
UNIT_MISSING = "missing"

_PATH_KEYS = ("diff_file", "context_file", "repo_dir")


def write_subset_cases(cases_path: str, case_ids: Sequence[str], dest: Path) -> Path:
    """Write ``dest``, a ``cases.json`` holding only ``case_ids``, and return it.

    ``cases_path`` is a ``cases.json`` file or a ``case-*/`` directory, as
    :func:`prxref.eval_cases.load_cases` takes it. The cases keep the order of
    ``case_ids``, and every file path (``diff_file``, ``context_file``,
    ``repo_dir`` and each local ``spec`` entry) is made absolute, so the
    subset loads from anywhere. An id missing from the dataset or listed
    twice raises ``ValueError``.
    """
    by_id = {case.id: case for case in load_cases(cases_path)}
    ids = list(case_ids)
    missing = [case_id for case_id in ids if case_id not in by_id]
    if missing:
        raise ValueError(f"case ids not in {cases_path}: {', '.join(missing)}")
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate case ids in the subset")
    entries = []
    for case_id in ids:
        entry = case_to_json(by_id[case_id])
        for key in _PATH_KEYS:
            if entry[key] is not None:
                entry[key] = os.path.abspath(entry[key])
        entry["spec"] = [_absolute_spec(item) for item in entry["spec"]]
        entries.append(entry)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _write_json(dest, {"version": 1, "cases": entries})
    return dest


def shard_ids(case_ids: Sequence[str], shards: int) -> list[list[str]]:
    """Deal ``case_ids`` round-robin into at most ``shards`` lists, none empty.

    Shard ``i`` takes the ids at positions ``i``, ``i + n``, ... so each keeps
    the dataset order and the sizes differ by at most one. Fewer ids than
    ``shards`` gives one list per id; no ids gives ``[]``. ``shards < 1``
    raises ``ValueError``.
    """
    if shards < 1:
        raise ValueError(f"shards must be at least 1, got {shards}")
    ids = list(case_ids)
    count = min(shards, len(ids))
    return [ids[i::count] for i in range(count)]


def unit_state(case_dir: Path) -> str:
    """Classify the unit in ``case_dir`` as one of the ``UNIT_*`` states.

    ``rate_limited``: :data:`RATE_LIMIT_RE` matches ``error.json``, the text of
    a ``record.json`` whose verdict is ``Error``, or any string value of a
    ``trace/*meta*.json`` (a unit that fell back from a rate-limited call is
    rate limited even when it produced a record; only strings are searched, so
    a token count of 429 is not a hit). Otherwise ``error``: an ``error.json``
    or an ``Error`` record. ``ok``: a ``record.json`` with any other verdict.
    ``missing``: neither file, or a ``record.json`` that cannot be read.
    """
    case_dir = Path(case_dir)
    error_text = _read_text(case_dir / "error.json")
    record_text = _read_text(case_dir / "record.json")
    if error_text is None and record_text is None:
        return UNIT_MISSING
    record_error = False
    if record_text is not None:
        try:
            record = json.loads(record_text)
        except ValueError:
            return UNIT_MISSING if error_text is None else _failed(case_dir, error_text)
        record_error = not isinstance(record, dict) or record.get("verdict") == "Error"
    if error_text is not None:
        return _failed(case_dir, error_text)
    if record_error:
        return _failed(case_dir, record_text or "")
    return UNIT_RATE_LIMITED if _meta_rate_limited(case_dir) else UNIT_OK


def reset_unit(case_dir: Path) -> None:
    """Delete ``record.json``, ``error.json`` and ``trace/`` of the unit in ``case_dir``.

    ``eval run --resume`` then runs the case again; ``case.json`` stays, and
    a unit with none of these files is left alone.
    """
    case_dir = Path(case_dir)
    for name in ("record.json", "error.json"):
        try:
            (case_dir / name).unlink()
        except FileNotFoundError:
            pass
    shutil.rmtree(case_dir / "trace", ignore_errors=True)


def merge_runs(src_runs: Sequence[Path], dest: Path, *, label: str, case_ids: Sequence[str]) -> None:
    """Join the shard runs ``src_runs`` into the single run ``dest`` named ``label``.

    ``dest`` is ``<out>/<label>``. Each of ``case_ids`` has its
    ``cases/<id>/`` copied from the one source that holds it; an id held by no
    source or by two raises ``ValueError`` before anything is written. A
    previous ``dest`` is replaced. ``run.json`` is the first source's with
    ``label`` and ``case_ids`` replaced and ``created_at`` the earliest of the
    sources. When ``review_rules`` or ``scoped_rules`` differ between sources
    (rules mined per fold) both become ``null`` and ``folds`` lists, per
    source, ``run`` (its path), ``case_ids`` (those it supplied),
    ``review_rules`` and ``scoped_rules``. ``eval score`` reads the result.
    """
    sources = [Path(src) for src in src_runs]
    if not sources:
        raise ValueError("no source runs to merge")
    ids = list(case_ids)
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate case ids in the merged run")
    runs = [json.loads((src / "run.json").read_text(encoding="utf-8")) for src in sources]
    owners: dict[str, list[int]] = {case_id: [] for case_id in ids}
    for index, src in enumerate(sources):
        for case_id in ids:
            if (src / "cases" / case_id).is_dir():
                owners[case_id].append(index)
    absent = [case_id for case_id, held in owners.items() if not held]
    if absent:
        raise ValueError(f"no source run holds case ids: {', '.join(absent)}")
    twice = [case_id for case_id, held in owners.items() if len(held) > 1]
    if twice:
        raise ValueError(f"more than one source run holds case ids: {', '.join(twice)}")
    merged = dict(runs[0])
    merged["label"] = label
    merged["case_ids"] = ids
    created = [run["created_at"] for run in runs if isinstance(run.get("created_at"), str)]
    if created:
        merged["created_at"] = min(created)
    merged.pop("folds", None)
    if any(
        run.get(key) != runs[0].get(key) for run in runs[1:] for key in ("review_rules", "scoped_rules")
    ):
        merged["review_rules"] = None
        merged["scoped_rules"] = None
        merged["folds"] = [
            {
                "run": str(src),
                "case_ids": [case_id for case_id in ids if owners[case_id] == [index]],
                "review_rules": run.get("review_rules"),
                "scoped_rules": run.get("scoped_rules"),
            }
            for index, (src, run) in enumerate(zip(sources, runs, strict=True))
        ]
    dest = Path(dest)
    stage = dest.with_name(dest.name + ".merging")
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "cases").mkdir(parents=True)
    for case_id in ids:
        shutil.copytree(sources[owners[case_id][0]] / "cases" / case_id, stage / "cases" / case_id)
    _write_json(stage / "run.json", merged)
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(stage, dest)


def _failed(case_dir: Path, text: str) -> str:
    """``rate_limited`` when ``text`` or a trace meta file matches, else ``error``."""
    if RATE_LIMIT_RE.search(text) or _meta_rate_limited(case_dir):
        return UNIT_RATE_LIMITED
    return UNIT_ERROR


def _meta_rate_limited(case_dir: Path) -> bool:
    """Whether any string in a ``trace/*meta*.json`` file matches :data:`RATE_LIMIT_RE`."""
    for path in sorted((case_dir / "trace").glob("*meta*.json")):
        text = _read_text(path)
        if text is None:
            continue
        try:
            strings = list(_strings(json.loads(text)))
        except ValueError:
            strings = [text]
        if any(RATE_LIMIT_RE.search(item) for item in strings):
            return True
    return False


def _strings(value: Any):
    """Yield every string value (not key) nested in ``value``."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _absolute_spec(item: str) -> str:
    parsed = urlparse(item)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return item
    return os.path.abspath(item)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _write_json(path: Path, obj: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
