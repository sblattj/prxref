"""Join the tickets named in a PR title onto the ticket context (#145).

Kept out of :mod:`prxref.ticket` so the orchestrator, which duck-types the
ticket and imports neither it nor the rules module, can build the merged
context without naming the class.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .text_inputs import cap_text
from .ticket import TicketContext, has_acceptance_criteria


def with_title_tickets(
    base: TicketContext | None,
    fetched: Sequence[tuple[str, str | None]],
    *,
    max_chars: int,
) -> TicketContext | None:
    """Add the tickets named in a PR title to ``base``, under ``max_chars``.

    ``fetched`` holds one ``(key, text)`` per title key, in title order;
    ``text`` is ``None`` for a key that could not be read. The result is the
    file's text (when ``base`` has any) followed by one ``## KEY`` section per
    ticket read, capped at ``max_chars`` as a whole, with the keys read and
    not read recorded on it. A ``base`` already cut at its own cap stays
    marked truncated. With no keys, ``base`` comes back marked ``from_title``
    (or ``None`` when there is no file either), so a run that found nothing in
    the title is otherwise unchanged.
    """
    if not fetched:
        return replace(base, from_title=True) if base is not None else None
    loaded = tuple(key for key, text in fetched if text is not None)
    failed = tuple(key for key, text in fetched if text is None)
    head = base.text if base is not None else ""
    sections = [f"## {key}\n{text.strip()}" for key, text in fetched if text is not None]
    joined = "\n\n".join(([head] if head else []) + sections)
    capped = cap_text(joined, max_chars)
    if base is not None and base.capped.truncated:
        capped = replace(capped, truncated=True, chars=max(capped.chars, base.capped.chars))
    text = capped.text.strip()
    return TicketContext(
        path=base.path if base is not None else "",
        capped=capped,
        text=text,
        has_acceptance_criteria=has_acceptance_criteria(text),
        ids_loaded=loaded,
        ids_failed=failed,
        from_title=True,
    )
