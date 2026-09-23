"""Team review rules: an operator-named file added to every review prompt.

``PRXREF_REVIEW_RULES`` (or ``--rules-file PATH``, which wins) names a
Markdown or plain-text file of team conventions. Optional front matter maps
the team's own severity words onto prxref's tiers, and the body reaches every
worker and the systemic sweep as a ``## Team review rules`` block in the
system prompt. The file is read by ``prxref review`` and by the webhook daemon
alike, before any network call, so a missing, unreadable or malformed file is
a configuration error (exit 2) naming whichever input supplied the path.

:class:`ReviewRules` is the loaded result the orchestrator duck-types: it
reads ``prompt_block("worker")`` / ``prompt_block("sweep")``, ``record()``
(the run-record and trace view, never the rules text) and ``severity_map``
(falsy means no remapping pass).

This build does not wire the loader yet: :func:`load_review_rules` returns
``None`` for an empty path and otherwise fails closed with a
:class:`~prxref.llm.ConfigError` naming the source, so a configured rules file
can never be silently ignored.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .llm import ConfigError
from .quality import SEVERITIES
from .text_inputs import CappedText

RESERVED_SEVERITIES: frozenset[str] = frozenset({"spec"})
MAPPABLE_SEVERITIES: frozenset[str] = SEVERITIES - RESERVED_SEVERITIES


def _not_wired(source: str) -> ConfigError:
    """The fail-closed error for a rules file this build cannot load."""
    return ConfigError(f"{source}: loading a team review-rules file is not wired in this build")


@dataclass(frozen=True)
class ReviewRules:
    """A loaded team review-rules file.

    ``path`` is the path as configured (not resolved); ``body`` is the rules
    text with the front matter removed, capped for the prompt, and
    fingerprinted by the raw file bytes; ``severity_map`` maps a casefolded
    team word to one of :data:`MAPPABLE_SEVERITIES`, in file order;
    ``ignored_keys`` names the other front-matter keys, which are not used.
    """

    path: str
    body: CappedText
    severity_map: Mapping[str, str]
    ignored_keys: tuple[str, ...] = ()

    def prompt_block(self, unit: str) -> str:
        """The system-prompt block for one review unit (``"worker"`` or ``"sweep"``).

        In this build no block is rendered, so every unit's prompt is the
        prompt of a run without rules.
        """
        return ""

    def record(self) -> dict[str, object]:
        """Return the run-record view: ``path``, ``sha256``, ``chars``,
        ``max_chars``, ``truncated`` and ``severity_map``, JSON-native values
        only and never the rules text.
        """
        return {"path": self.path, **self.body.record(), "severity_map": dict(self.severity_map)}


def split_front_matter(
    text: str, *, source: str, path: str
) -> tuple[dict[str, str], tuple[str, ...], str]:
    """Split ``text`` into ``(severity_map, ignored_keys, body)``.

    A malformed severity map is a :class:`~prxref.llm.ConfigError` naming
    ``source`` and ``path``. In this build every call fails closed with that
    error type.
    """
    raise _not_wired(source)


def load_review_rules(path: str | None, *, max_chars: int, source: str) -> ReviewRules | None:
    """Load the team review-rules file at ``path``, capped at ``max_chars``.

    An empty, whitespace-only or ``None`` path means "no rules" and returns
    ``None``. ``source`` is the input that supplied the path
    (``--rules-file`` or ``PRXREF_REVIEW_RULES``), and every failure is a
    :class:`~prxref.llm.ConfigError` whose message starts with it. In this
    build a non-empty path always fails closed that way.
    """
    if path is None or not path.strip():
        return None
    raise _not_wired(source)
