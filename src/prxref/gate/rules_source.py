"""Materialize a repository's prxref config at one commit (#102).

The gate has no checkout. It fetches ``.prxref.toml`` at the rules SHA (the
PR's base under ``PRXREF_RULES_SOURCE=base``) and every file its path keys
name at the same SHA, and writes them as regular files under a private
directory, which then serves as the review's ``config_file``. A path that is
absolute, starts with ``~``, climbs out with ``..`` or is a URL is never
fetched: the config loader refuses it with a ConfigError, so the review
fails closed. A named file missing at the SHA is left missing, with the
same result.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from prxref import config

DIRECTORY_KEYS = frozenset({"prompts_dir"})


@dataclass(frozen=True)
class PinnedRules:
    """What :func:`materialize_rules` wrote."""

    directory: Path
    config_file: Path | None
    fetched: tuple[str, ...]
    missing: tuple[str, ...]


def rules_sha(head_sha: str, base_sha: str, rules_source: str) -> str:
    """The commit rules are read at: ``base_sha`` under ``"base"``, else ``head_sha``."""
    return base_sha if rules_source == "base" else head_sha


def _safe(raw: str) -> PurePosixPath | None:
    if not raw.strip() or raw.startswith("~") or "://" in raw:
        return None
    rel = PurePosixPath(raw)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    return rel


def materialize_rules(read: Callable[[str], str | None], directory: Path) -> PinnedRules:
    """Write the config at ``read``'s commit, and every file its path keys name, under ``directory``."""
    text = read(config.CONFIG_FILE_NAME)
    if text is None:
        return PinnedRules(directory, None, (), ())
    config_file = directory / config.CONFIG_FILE_NAME
    config_file.write_text(text, encoding="utf-8")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return PinnedRules(directory, config_file, (), ())
    wanted: set[str] = set()
    for key in config._FILE_PATH_KEYS - DIRECTORY_KEYS:
        value = data.get(key)
        values = value if isinstance(value, list) else [value]
        for raw in values:
            if not isinstance(raw, str):
                continue
            if key == "metadata_rules" and raw.strip() in config.METADATA_RULES_SWITCHES:
                continue
            rel = _safe(raw)
            if rel is not None:
                wanted.add(rel.as_posix())
    fetched, missing = [], []
    for rel in sorted(wanted):
        content = read(rel)
        if content is None:
            missing.append(rel)
            continue
        dest = directory / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        fetched.append(rel)
    return PinnedRules(directory, config_file, tuple(fetched), tuple(missing))
