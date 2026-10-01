"""The local files a review reads before any network call.

``review``, ``eval run`` and ``config check`` all open the same inputs the
same way, through :func:`load_path_inputs`: the team rules file, the
path-scoped rules, the ticket-context file, the prompt-template
directory and the execution-evidence files. An unusable one is a
:class:`~prxref.llm.ConfigError`, so every command that opens them exits 2
with the same ``configuration error: ...`` line, before any forge or LLM
is contacted.

Each input is reported under whatever supplied its path
(:func:`path_input_source`): the command-line flag when one was given, the
repository config file as ``<file>: <key>`` when the file set it (#38), and
otherwise the environment variable. ``spec_sources`` is not among them: the
orchestrator loads spec sources best-effort after the forge is built, and a
failed source is recorded as that source's error rather than exiting 2.

This module never imports :mod:`prxref.cli`, so :mod:`prxref.evals` can use
it too.
"""
from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import evidence as evidence_mod
from .config import _ENV_PREFIX, _display_path
from .llm import ConfigError
from .prompt_templates import load_prompt_templates
from .rules import load_review_rules, load_scoped_rules
from .ticket import load_ticket_context

#: The flag that overrides each path key, on every command that has one.
PATH_INPUT_FLAGS: Mapping[str, str] = {
    "review_rules": "--rules-file",
    "scoped_rules": "--scoped-rules",
    "ticket_context_file": "--context-file",
    "prompts_dir": "--prompts-dir",
    "evidence_files": "--evidence-file",
}


@dataclass(frozen=True)
class PathLoaders:
    """The five loaders :func:`load_path_inputs` calls.

    The defaults are the real loaders. The CLI passes the names it imported,
    looked up at call time, so a caller that replaces one on :mod:`prxref.cli`
    replaces it here too.
    """

    review_rules: Callable[..., Any] = load_review_rules
    scoped_rules: Callable[..., Any] = load_scoped_rules
    ticket_context: Callable[..., Any] = load_ticket_context
    prompt_templates: Callable[..., Any] = load_prompt_templates
    evidence: Callable[..., Any] = evidence_mod.load_evidence


@dataclass(frozen=True)
class PathInputs:
    """What :func:`load_path_inputs` loaded; each is ``None`` when its key is unset."""

    rules: Any
    scoped: Any
    ticket: Any
    prompts: Any
    evidence: Any


def path_input_source(key: str, layers: Mapping[str, str], *, config_file: Path | None) -> str:
    """The name an error about path key ``key`` reports, from its ``layers`` entry.

    ``layers`` is the second value of
    :func:`prxref.config.load_config_with_sources`. An ``override`` is named
    by its flag in :data:`PATH_INPUT_FLAGS`; a ``file`` value by
    ``<file>: <key>``, the label the config file layer uses for its own
    errors; an ``env NAME`` value by ``NAME``; and a default by the
    variable an operator would set.
    """
    layer = layers.get(key, "default")
    if layer == "override":
        return PATH_INPUT_FLAGS.get(key, key)
    if layer == "file" and config_file is not None:
        return f"{_display_path(config_file)}: {key}"
    if layer.startswith("env "):
        return layer.removeprefix("env ")
    return _ENV_PREFIX + key.upper()


def load_text_input(loader: Any, path: str | list[str], *, max_chars: int, source: str) -> Any:
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


def load_prompts_dir(
    path: str | None, *, source: str, loader: Callable[..., Any] = load_prompt_templates,
) -> Any:
    """Load the prompt-template overrides in ``path``, fenced as :func:`load_text_input` fences a file.

    ``None``, ``""`` and whitespace mean "no overrides" and return ``None``,
    so ``--prompts-dir ""`` turns ``PRXREF_PROMPTS_DIR`` off. The loader
    raises ``ConfigError`` naming ``source`` itself; an ``OSError`` or
    ``ValueError`` that escapes it becomes one too, so an unusable directory
    always exits 2 before any network call.
    """
    try:
        return loader(path, source=source)
    except ConfigError:
        raise
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{source}: cannot load prompts directory {path!r}: {exc}") from exc


def load_path_inputs(
    cfg: Mapping[str, Any],
    layers: Mapping[str, str],
    *,
    config_file: Path | None,
    ticket: bool = True,
    evidence: bool = True,
    loaders: PathLoaders | None = None,
) -> PathInputs:
    """Open the rules files, the ticket file and the prompts directory ``cfg`` names.

    ``cfg`` and ``layers`` are what
    :func:`prxref.config.load_config_with_sources` returned for
    ``config_file``. The inputs are loaded in order (rules, scoped rules,
    ticket, prompts, evidence), and the scoped rules are checked against the
    always-on file, so one team word mapped to two tiers across them is a
    ``ConfigError`` too. Each failure names :func:`path_input_source`.
    ``ticket=False`` skips the ticket file (``eval run`` names one per
    case) and ``evidence=False`` skips the evidence files (the environment
    cannot leak evidence into a case), each leaving that field ``None``.
    """
    use = loaders or PathLoaders()

    def source(key: str) -> str:
        return path_input_source(key, layers, config_file=config_file)

    rules = load_text_input(
        use.review_rules, cfg["review_rules"],
        max_chars=cfg["review_rules_max_chars"], source=source("review_rules"),
    )
    scoped = load_text_input(
        functools.partial(use.scoped_rules, always_on=rules), cfg["scoped_rules"],
        max_chars=cfg["review_rules_max_chars"], source=source("scoped_rules"),
    )
    loaded_ticket = None
    if ticket:
        loaded_ticket = load_text_input(
            use.ticket_context, cfg["ticket_context_file"],
            max_chars=cfg["ticket_context_max_chars"], source=source("ticket_context_file"),
        )
    prompts = load_prompts_dir(
        cfg["prompts_dir"], source=source("prompts_dir"), loader=use.prompt_templates,
    )
    loaded_evidence = None
    if evidence:
        loaded_evidence = load_text_input(
            use.evidence, cfg["evidence_files"],
            max_chars=evidence_mod.MAX_FILE_CHARS, source=source("evidence_files"),
        )
    return PathInputs(
        rules=rules, scoped=scoped, ticket=loaded_ticket, prompts=prompts,
        evidence=loaded_evidence,
    )
