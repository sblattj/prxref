"""Runs one user turn as a sequence of steps."""
from dataclasses import dataclass
from typing import Callable

from assistant.messages import Message, MessageTable
from assistant.progress import ProgressAnnouncer
from assistant.session import Run
from assistant.state_store import StateStore


@dataclass(frozen=True)
class Step:
    kind: str  # "model" | "tool" | "handoff"
    name: str
    execute: Callable[[Run], str]


class Engine:
    def __init__(self, table: MessageTable, store: StateStore) -> None:
        self.table = table
        self.store = store
        self.progress = ProgressAnnouncer(table)

    def run_turn(self, run: Run, steps: list[Step]) -> str | None:
        answer = None
        for step in steps:
            if step.kind == "handoff":
                # A human takes over: park the run so it can resume later.
                self.store.save(run)
                return None
            self.progress.announce(run, _note_for(step))
            answer = step.execute(run)
            self.table.append(Message(run.session_id, "tool" if step.kind == "tool" else "assistant", answer))
        return answer


def _note_for(step: Step) -> str:
    if step.kind == "tool":
        return f"Running {step.name.replace('_', ' ')}..."
    return "Thinking about your request..."
