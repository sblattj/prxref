#!/usr/bin/env bash
# Builds the fixture: branch main (base) and feature/progress-notes (the PR). Usage: make-fixture.sh [dir]
set -euo pipefail; D=${1:-shared-sink}; rm -rf "$D"; mkdir -p "$D"; cd "$D"; git init -q -b main
git config user.name fixture; git config user.email fixture@example.invalid
mkdir -p "$(dirname 'README.md')"; cat > 'README.md' <<'__EOF__'
# assistant

A tiny chat assistant engine: a user message runs a loop of steps (model calls and tool calls).
A run that needs a human hands off: its state is saved to the state store and resumed later.
__EOF__
mkdir -p "$(dirname 'assistant/__init__.py')"; cat > 'assistant/__init__.py' <<'__EOF__'
__EOF__
mkdir -p "$(dirname 'assistant/engine.py')"; cat > 'assistant/engine.py' <<'__EOF__'
"""Runs one user turn as a sequence of steps."""
from dataclasses import dataclass
from typing import Callable

from assistant.messages import Message, MessageTable
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

    def run_turn(self, run: Run, steps: list[Step]) -> str | None:
        answer = None
        for step in steps:
            if step.kind == "handoff":
                # A human takes over: park the run so it can resume later.
                self.store.save(run)
                return None
            answer = step.execute(run)
            self.table.append(Message(run.session_id, "tool" if step.kind == "tool" else "assistant", answer))
        return answer
__EOF__
mkdir -p "$(dirname 'assistant/features.py')"; cat > 'assistant/features.py' <<'__EOF__'
"""Feature toggles, read from the environment at startup."""
import os


def enabled(name: str, default: bool) -> bool:
    raw = os.environ.get(f"ASSISTANT_{name.upper()}")
    return default if raw is None else raw.lower() == "true"
__EOF__
mkdir -p "$(dirname 'assistant/history.py')"; cat > 'assistant/history.py' <<'__EOF__'
"""Builds the model's conversation history from the message table."""
from assistant.messages import MessageTable

HISTORY_WINDOW = 20

_MODEL_ROLES = {"user": "user", "assistant": "assistant", "tool": "tool"}


def model_history(table: MessageTable, session_id: str) -> list[dict]:
    history = []
    for m in table.recent(session_id, HISTORY_WINDOW):
        role = _MODEL_ROLES.get(m.role)
        if role is None:
            continue
        history.append({"role": role, "content": m.text})
    return history
__EOF__
mkdir -p "$(dirname 'assistant/messages.py')"; cat > 'assistant/messages.py' <<'__EOF__'
"""Append-only message table for a session."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    session_id: str
    role: str  # "user" | "assistant" | "tool"
    text: str


class MessageTable:
    def __init__(self) -> None:
        self._rows: list[Message] = []

    def append(self, message: Message) -> None:
        self._rows.append(message)

    def recent(self, session_id: str, limit: int) -> list[Message]:
        rows = [m for m in self._rows if m.session_id == session_id]
        return rows[-limit:]
__EOF__
mkdir -p "$(dirname 'assistant/session.py')"; cat > 'assistant/session.py' <<'__EOF__'
"""Live state of one run."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Frame:
    """One agent working on the request. The bottom frame is the root agent."""

    agent_id: str
    # Scratch space any engine component can use for per-frame bookkeeping.
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Run:
    session_id: str
    turn_id: str
    frames: list[Frame] = field(default_factory=list)

    def root(self) -> Frame:
        return self.frames[0]

    def current(self) -> Frame:
        return self.frames[-1]
__EOF__
mkdir -p "$(dirname 'assistant/state_store.py')"; cat > 'assistant/state_store.py' <<'__EOF__'
"""Saves a run so it can resume after a human handoff."""
import json
from dataclasses import asdict

from assistant.session import Frame, Run


class StateStore:
    def __init__(self) -> None:
        self._blobs: dict[str, str] = {}

    def save(self, run: Run) -> None:
        self._blobs[run.session_id] = json.dumps(asdict(run))

    def load(self, session_id: str) -> Run | None:
        blob = self._blobs.get(session_id)
        if blob is None:
            return None
        raw = json.loads(blob)
        frames = [Frame(f["agent_id"], f["data"]) for f in raw["frames"]]
        return Run(raw["session_id"], raw["turn_id"], frames)
__EOF__
mkdir -p "$(dirname 'pyproject.toml')"; cat > 'pyproject.toml' <<'__EOF__'
[project]
name = "assistant"
version = "0.1.0"
requires-python = ">=3.11"

[tool.pytest.ini_options]
pythonpath = ["."]
__EOF__
mkdir -p "$(dirname 'tests/test_engine.py')"; cat > 'tests/test_engine.py' <<'__EOF__'
from assistant.engine import Engine, Step
from assistant.messages import MessageTable
from assistant.session import Frame, Run
from assistant.state_store import StateStore


def make_run():
    return Run("s1", "t1", [Frame("root")])


def test_turn_returns_last_answer():
    engine = Engine(MessageTable(), StateStore())
    steps = [Step("tool", "lookup", lambda r: "found"), Step("model", "reply", lambda r: "done")]
    assert engine.run_turn(make_run(), steps) == "done"


def test_handoff_parks_and_resumes():
    store = StateStore()
    engine = Engine(MessageTable(), store)
    run = make_run()
    run.root().data["attempts"] = 1
    assert engine.run_turn(run, [Step("handoff", "human", lambda r: "")]) is None
    assert store.load("s1").root().data == {"attempts": 1}
__EOF__
git add -A; git commit -qm "chore: engine, state store, message history"; git checkout -qb feature/progress-notes
mkdir -p "$(dirname '.gitignore')"; cat > '.gitignore' <<'__EOF__'
__pycache__/
.pytest_cache/
__EOF__
mkdir -p "$(dirname 'README.md')"; cat > 'README.md' <<'__EOF__'
# assistant

A tiny chat assistant engine: a user message runs a loop of steps (model calls and tool calls).
A run that needs a human hands off: its state is saved to the state store and resumed later.

Progress notes (`ASSISTANT_PROGRESS_NOTES`, default on): before each step the user sees a short
line such as "Running find order...". Notes are saved as `progress` messages and never sent
to the model.
__EOF__
mkdir -p "$(dirname 'assistant/engine.py')"; cat > 'assistant/engine.py' <<'__EOF__'
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
__EOF__
mkdir -p "$(dirname 'assistant/progress.py')"; cat > 'assistant/progress.py' <<'__EOF__'
"""Progress notes: a short user-facing line announced before each step runs.

Each note is saved as a message (role "progress") so the transcript holds exactly what the
user saw. A per-turn ledger on the root frame drops a note that repeats one already shown.
"""
from assistant.features import enabled
from assistant.messages import Message, MessageTable
from assistant.session import Run

LEDGER_KEY = "progress.ledger"


class ProgressLedger:
    """Notes shown to the user during one turn."""

    def __init__(self, turn_id: str) -> None:
        self._turn_id = turn_id
        self._lines: list[str] = []

    def turn_id(self) -> str:
        return self._turn_id

    def record(self, line: str) -> bool:
        key = line.strip().lower()
        if any(shown.strip().lower() == key for shown in self._lines):
            return False
        self._lines.append(line)
        return True


class ProgressAnnouncer:
    def __init__(self, table: MessageTable) -> None:
        self.table = table
        self.active = enabled("progress_notes", default=True)

    def announce(self, run: Run, text: str) -> None:
        if not self.active:
            return
        if self._ledger(run).record(text):
            self.table.append(Message(run.session_id, "progress", text))

    def _ledger(self, run: Run) -> ProgressLedger:
        data = run.root().data
        ledger = data.get(LEDGER_KEY)
        if not isinstance(ledger, ProgressLedger) or ledger.turn_id() != run.turn_id:
            ledger = ProgressLedger(run.turn_id)
            data[LEDGER_KEY] = ledger
        return ledger
__EOF__
mkdir -p "$(dirname 'tests/conftest.py')"; cat > 'tests/conftest.py' <<'__EOF__'
import pytest


@pytest.fixture(autouse=True)
def _progress_notes_off(monkeypatch):
    # Keep exact message-table assertions deterministic; progress tests opt in.
    monkeypatch.setenv("ASSISTANT_PROGRESS_NOTES", "false")
__EOF__
mkdir -p "$(dirname 'tests/test_progress.py')"; cat > 'tests/test_progress.py' <<'__EOF__'
from assistant.engine import Engine, Step
from assistant.messages import MessageTable
from assistant.session import Frame, Run
from assistant.state_store import StateStore


def test_each_step_is_announced_once(monkeypatch):
    monkeypatch.setenv("ASSISTANT_PROGRESS_NOTES", "true")
    table = MessageTable()
    engine = Engine(table, StateStore())
    run = Run("s1", "t1", [Frame("root")])
    steps = [
        Step("tool", "find_order", lambda r: "order 7"),
        Step("model", "reply", lambda r: "thinking"),
        Step("model", "reply", lambda r: "your order shipped"),
    ]
    assert engine.run_turn(run, steps) == "your order shipped"
    notes = [m.text for m in table.recent("s1", 50) if m.role == "progress"]
    assert notes == ["Running find order...", "Thinking about your request..."]
__EOF__
git add -A; git commit -qm "feat: progress notes before each engine step"; git log --oneline --all
