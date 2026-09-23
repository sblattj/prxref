"""Ticket context: the ticket a pull request is meant to implement.

``PRXREF_TICKET_CONTEXT_FILE`` (or ``--context-file PATH``, which wins) names
a plain-text or Markdown file holding that ticket. When it has text, every
review unit sees it as fenced, untrusted data and is asked to mark each
finding ``in``, ``out`` or ``unknown`` of the ticket's scope. An empty or
whitespace-only file is the EMPTY state, "this PR has no ticket": nothing is
added to the prompts and the summary says so. The file is read by
``prxref review`` before any network call, so a missing, unreadable or
non-UTF-8 file is a configuration error (exit 2) naming whichever input
supplied the path. ``prxref serve`` ignores it: one file cannot describe
every PR.

:class:`TicketContext` is the loaded result the orchestrator duck-types: it
reads ``active``, ``record()`` (the run-record and trace view, never the
ticket text), ``prompt_block()``, ``scope_block()`` and ``note()``.

This build does not wire the loader yet: :func:`load_ticket_context` returns
``None`` for an empty path and otherwise fails closed with a
:class:`~prxref.llm.ConfigError` naming the source, so a configured ticket
file can never be silently ignored.
"""
from __future__ import annotations

from dataclasses import dataclass

from .llm import ConfigError
from .text_inputs import CappedText


def _not_wired(source: str) -> ConfigError:
    """The fail-closed error for a ticket-context file this build cannot load."""
    return ConfigError(f"{source}: loading a ticket-context file is not wired in this build")


@dataclass(frozen=True)
class TicketContext:
    """A loaded ticket-context file.

    ``path`` is the path as configured; ``capped`` is the decoded text,
    capped for the prompt and fingerprinted by the raw file bytes; ``text``
    is the kept text, stripped, and empty for an EMPTY ticket;
    ``has_acceptance_criteria`` says whether that text carries acceptance
    criteria.
    """

    path: str
    capped: CappedText
    text: str
    has_acceptance_criteria: bool

    @property
    def active(self) -> bool:
        """True when the ticket has text, so its blocks reach the prompts and
        a finding's ``scope`` is accepted; False for an EMPTY ticket."""
        return bool(self.text)

    def record(self) -> dict[str, object]:
        """Return the run-record view: ``path``, ``sha256``, ``chars``,
        ``max_chars``, ``truncated``, ``has_acceptance_criteria`` and
        ``empty``, JSON-native values only and never the ticket text.
        """
        return {
            "path": self.path,
            **self.capped.record(),
            "has_acceptance_criteria": self.has_acceptance_criteria,
            "empty": not self.text,
        }

    def prompt_block(self) -> str:
        """The user-prompt ``### Ticket context`` block. In this build: ``""``."""
        return ""

    def scope_block(self) -> str:
        """The system-prompt ``## Ticket scope`` block. In this build: ``""``."""
        return ""

    def note(self) -> str:
        """The summary note for this ticket's state: ``""`` or a line ending
        in ``"\\n"``. In this build: ``""``."""
        return ""


def has_acceptance_criteria(text: str) -> bool:
    """Whether ``text`` carries acceptance criteria. In this build: ``False``."""
    return False


def fence(text: str) -> str:
    """Wrap ``text`` in a Markdown code fence longer than any backtick run inside it.

    The fence is at least three backticks and one longer than the longest
    run of backticks in ``text``, so the text can never close it early.
    """
    longest = run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}text\n{text}\n{ticks}"


def load_ticket_context(path: str | None, *, max_chars: int, source: str) -> TicketContext | None:
    """Load the ticket-context file at ``path``, capped at ``max_chars``.

    An empty, whitespace-only or ``None`` path means "no ticket" and returns
    ``None``. ``source`` is the input that supplied the path
    (``--context-file`` or ``PRXREF_TICKET_CONTEXT_FILE``), and every failure
    is a :class:`~prxref.llm.ConfigError` whose message starts with it. In
    this build a non-empty path always fails closed that way.
    """
    if path is None or not path.strip():
        return None
    raise _not_wired(source)
