"""The live binding guard (#102).

A generation is bound to the PR state it was queued for: head SHA, base
SHA, base ref, open. Before every publishing write the guard re-reads the
PR; any drift, or a newer generation for the PR, means the generation is
stale and publishes nothing, because a newer generation owns the result.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from prxref.gate.checks import Stale
from prxref.gate.store import Generation, Store


@dataclass(frozen=True)
class LivePR:
    """The fields of a live pull request the binding compares."""

    head_sha: str
    base_sha: str
    base_ref: str
    state: str
    merged: bool


def read_live_pr(client: Any, repo: str, number: int) -> LivePR:
    """``GET /repos/{repo}/pulls/{number}`` reduced to a :class:`LivePR`."""
    pull = client.request("GET", repo, f"/pulls/{number}")
    return LivePR(
        head_sha=pull["head"]["sha"],
        base_sha=pull["base"]["sha"],
        base_ref=pull["base"]["ref"],
        state=pull["state"],
        merged=bool(pull.get("merged")),
    )


def check_binding(gen: Generation, live: LivePR, store: Store) -> str | None:
    """``None`` while ``gen`` may publish; else why it may not."""
    if live.state != "open":
        return "closed"
    if live.head_sha != gen.head_sha:
        return "head moved"
    if live.base_ref != gen.base_ref:
        return "retargeted"
    if live.base_sha != gen.base_sha:
        return "base moved"
    latest = store.latest_generation(gen.repo, gen.pr_number)
    if latest is not None and latest.gen > gen.gen:
        return "superseded"
    return None


def make_guard(client: Any, store: Store) -> Callable[[Generation], None]:
    """A :class:`~prxref.gate.checks.Publisher` guard that raises :class:`Stale`."""

    def guard(gen: Generation) -> None:
        reason = check_binding(gen, read_live_pr(client, gen.repo, gen.pr_number), store)
        if reason is not None:
            raise Stale(reason)

    return guard
