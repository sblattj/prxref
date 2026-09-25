"""Tests for readers of shared state in the per-chunk repository context (issue #22).

Covers the fourth source of :func:`prxref.repo_unit.build_unit_context`:
:func:`prxref.repo_readers.reader_entries` runs at ``repo`` only, with a
reader and a file listing, after the resolver and on the chunk reads the
resolver left. Its ``reader`` / ``shared-state`` entries are ranked last,
render under their own :data:`prxref.chunk_context.READER_HEADER` block, and
reach the prompt through :func:`prxref.orchestrator._context_blocks`. The
last class runs the #22 fixture's shape through the production reader over
inline copies of the fixture's head files. No test touches the network.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from prxref import orchestrator, repo_readers
from prxref.chunk_context import (
    CONTRACT_HEADER,
    DEFINITIONS_HEADER,
    DEPENDENCY_HEADER,
    READER_HEADER,
    render_context_blocks,
)
from prxref.repo_context import REASONS, ContextEntry, exclude_predicate
from prxref.repo_reader import RepoReader
from prxref.repo_unit import EMPTY_UNIT, UnitContext, build_unit_context

MAX_CHARS = 12000


def _omitted(count: int) -> str:
    return f"\N{HORIZONTAL ELLIPSIS} {count} more context entries omitted"


class _Recording:
    """Wraps a ``read(path)`` callable and records every path asked for, in call order."""

    def __init__(self, read):
        self.read = read
        self.calls: list[str] = []

    def __call__(self, path: str) -> str | None:
        self.calls.append(path)
        return self.read(path)


def _file(path: str, text: str, *, added: set[int] | None = None, status: str = "modified") -> SimpleNamespace:
    """A FileDiff stand-in with one hunk over all of ``text``; lines in ``added`` (1-based) are ``+``, all when None."""
    lines = []
    for number, line in enumerate(text.splitlines(), start=1):
        kind = "+" if added is None or number in added else " "
        lines.append(SimpleNamespace(kind=kind, text=line, new_line=number))
    return SimpleNamespace(path=path, status=status, hunks=[SimpleNamespace(lines=lines)])


def _readers(unit: UnitContext) -> list[ContextEntry]:
    return [entry for entry in unit.entries if entry.kind == "reader"]


def _keys(entries) -> list[tuple[str, int, str, str, str]]:
    return [(e.path, e.line, e.symbol, e.kind, e.reason) for e in entries]


WRITER = "pkg/writer.py"
READER = "pkg/reader.py"
OTHER = "pkg/other.py"

WRITER_TEXT = """\
class Writer:
    def remember(self, k, v):
        self.cache[k] = v
"""

READER_TEXT = """\
def lookup(store, k):
    return store.cache.get(k)
"""

FILES = {WRITER: WRITER_TEXT, READER: READER_TEXT, OTHER: READER_TEXT, "pkg/unrelated.py": "X = 1\n"}
LISTING = frozenset(FILES)


def _simple(mode: str, *, read=None, listing=LISTING, all_files=None, **overrides) -> UnitContext:
    chunk = [_file(WRITER, WRITER_TEXT, added={3})]
    kwargs = dict(
        mode=mode,
        read=read if read is not None else _Recording(FILES.get),
        max_chars=MAX_CHARS,
        listing_paths=listing,
        listing_complete=True,
    )
    kwargs.update(overrides)
    return build_unit_context(chunk, all_files if all_files is not None else chunk, **kwargs)


class TestLevels:
    def test_repo_with_a_listing_and_a_reader_gets_the_reader_entry(self):
        unit = _simple("repo")

        assert _keys(_readers(unit)) == [
            (OTHER, 1, "cache", "reader", "shared-state"),
            (READER, 1, "cache", "reader", "shared-state"),
        ]
        assert unit.reader_lines == tuple(entry.rendered() for entry in _readers(unit))
        assert unit.reader_lines[0] == f"{OTHER}:1: def lookup(store, k):\n    return store.cache.get(k)"
        assert unit.contract_lines == ()

    @pytest.mark.parametrize("mode", ["diff", "off"])
    def test_diff_and_off_have_no_readers_even_with_a_listing_and_a_reader(self, mode):
        read = _Recording(FILES.get)
        unit = _simple(mode, read=read)

        assert _readers(unit) == []
        assert unit.reader_lines == ()
        assert READER not in read.calls and OTHER not in read.calls

    def test_off_is_still_the_empty_unit(self):
        read = _Recording(FILES.get)
        assert _simple("off", read=read) is EMPTY_UNIT
        assert read.calls == []

    def test_repo_without_a_reader_has_no_readers(self):
        unit = build_unit_context(
            [_file(WRITER, WRITER_TEXT, added={3})], [_file(WRITER, WRITER_TEXT, added={3})],
            mode="repo", read=None, max_chars=MAX_CHARS, listing_paths=LISTING, listing_complete=True,
        )
        assert _readers(unit) == []


class TestListing:
    def test_no_listing_gives_no_readers_and_no_extra_reads(self, monkeypatch):
        without = _Recording(FILES.get)
        unit = _simple("repo", read=without, listing=None)
        monkeypatch.setattr(repo_readers, "MAX_READER_ENTRIES", 0)
        switched_off = _Recording(FILES.get)
        _simple("repo", read=switched_off, listing=None)

        assert _readers(unit) == []
        assert without.calls == switched_off.calls

    def test_control_the_listing_is_what_adds_the_reads(self):
        with_listing = _Recording(FILES.get)
        without = _Recording(FILES.get)
        _simple("repo", read=with_listing)
        _simple("repo", read=without, listing=None)

        assert with_listing.calls[: len(without.calls)] == without.calls
        assert with_listing.calls[len(without.calls):] == [OTHER, READER, "pkg/unrelated.py"]


class TestPathsNeverRead:
    def test_an_excluded_candidate_is_never_read(self):
        read = _Recording(FILES.get)
        unit = _simple("repo", read=read, exclude=lambda path: path == READER)

        assert READER not in read.calls
        assert [e.path for e in _readers(unit)] == [OTHER]

    def test_an_exclude_that_raises_counts_as_excluded(self):
        def exclude(path: str) -> bool:
            if path == READER:
                raise RuntimeError("cannot answer")
            return False

        read = _Recording(FILES.get)
        unit = _simple("repo", read=read, exclude=exclude)

        assert READER not in read.calls
        assert [e.path for e in _readers(unit)] == [OTHER]

    def test_a_diff_file_candidate_is_skipped(self, monkeypatch):
        chunk_file = _file(WRITER, WRITER_TEXT, added={3})
        other_diff = _file(OTHER, READER_TEXT, added={2})
        all_files = [chunk_file, other_diff]
        on = _Recording(FILES.get)
        unit = _simple("repo", read=on, all_files=all_files)
        monkeypatch.setattr(repo_readers, "MAX_READER_ENTRIES", 0)
        off = _Recording(FILES.get)
        _simple("repo", read=off, all_files=all_files)

        assert [e.path for e in _readers(unit)] == [READER]
        assert on.calls[: len(off.calls)] == off.calls
        assert OTHER not in on.calls[len(off.calls):]
        assert READER in on.calls[len(off.calls):]


class TestSwitch:
    def test_zero_max_reader_entries_at_runtime_gives_no_reader_entries_and_no_reader_reads(self, monkeypatch):
        on = _Recording(FILES.get)
        assert _readers(_simple("repo", read=on)) != []
        monkeypatch.setattr(repo_readers, "MAX_READER_ENTRIES", 0)
        off = _Recording(FILES.get)
        unit = _simple("repo", read=off)

        assert _readers(unit) == []
        assert unit.reader_lines == ()
        assert READER not in off.calls and OTHER not in off.calls

    def test_zero_max_reader_scan_at_runtime_gives_no_reader_entries(self, monkeypatch):
        monkeypatch.setattr(repo_readers, "MAX_READER_SCAN", 0)
        assert _readers(_simple("repo")) == []

    def test_the_entry_cap_is_read_at_call_time(self, monkeypatch):
        monkeypatch.setattr(repo_readers, "MAX_READER_ENTRIES", 1)
        assert [e.path for e in _readers(_simple("repo"))] == [OTHER]


class TestRecord:
    def test_record_rows_carry_kind_reader_and_reason_shared_state(self):
        unit = _simple("repo")
        rows = [row for row in unit.record()["entries"] if row["kind"] == "reader"]

        assert rows == [
            {
                "path": entry.path,
                "line": entry.line,
                "symbol": "cache",
                "kind": "reader",
                "reason": "shared-state",
                "chars": len(entry.rendered()),
            }
            for entry in _readers(unit)
        ]
        assert len(rows) == 2


IMPORTING = "pkg/importing.py"
IMPORTING_TEXT = """\
from pkg.alpha import Alpha
from pkg.beta import Beta


class Writer:
    def remember(self, k, v):
        Alpha.check(k)
        Beta.check(v)
        self.cache[k] = v
"""
ALPHA_TEXT = """\
class Alpha:
    @staticmethod
    def check(value):
        return value is not None
"""
BETA_TEXT = """\
class Beta:
    @staticmethod
    def check(value):
        return value is not None
"""
GAMMA_TEXT = """\
def lookup(store, k):
    return store.cache.get(k)
"""
CAP_FILES = {
    IMPORTING: IMPORTING_TEXT,
    "pkg/alpha.py": ALPHA_TEXT,
    "pkg/beta.py": BETA_TEXT,
    "pkg/gamma.py": GAMMA_TEXT,
}


def _capped(chunk_cap: int) -> tuple[UnitContext, RepoReader]:
    """Build the importing file's unit through the production routed reader capped at ``chunk_cap``."""
    chunk = [_file(IMPORTING, IMPORTING_TEXT, status="added")]
    reader = RepoReader(CAP_FILES.get, None, kind="repo-dir", chunk_cap=chunk_cap)
    read = orchestrator._routed_read(reader, frozenset({IMPORTING}))
    unit = build_unit_context(
        chunk, chunk, mode="repo", read=read, max_chars=MAX_CHARS,
        listing_paths=frozenset(CAP_FILES), listing_complete=True,
    )
    return unit, reader


class TestSharedChunkCap:
    def test_the_resolver_alone_spends_exactly_two_capped_reads(self, monkeypatch):
        monkeypatch.setattr(repo_readers, "MAX_READER_ENTRIES", 0)
        unit, reader = _capped(2)

        assert [(e.path, e.reason) for e in unit.entries] == [
            ("pkg/alpha.py", "import"),
            ("pkg/beta.py", "import"),
        ]
        assert reader.stats()["read_cap_hit"] is False

    def test_a_saturated_cap_leaves_the_resolver_unchanged_and_readers_add_nothing(self, monkeypatch):
        on, on_reader = _capped(2)
        monkeypatch.setattr(repo_readers, "MAX_READER_ENTRIES", 0)
        off, off_reader = _capped(2)

        assert [e for e in on.entries if e.kind == "definition"] == list(off.entries)
        assert on.definition_lines == off.definition_lines
        assert _readers(on) == []
        assert on.reader_lines == ()
        assert on_reader.stats()["reads"] == off_reader.stats()["reads"]
        assert on_reader.stats()["read_cap_hit"] is True

    def test_control_one_more_read_lets_the_reader_in(self):
        unit, reader = _capped(3)

        assert _keys(_readers(unit)) == [("pkg/gamma.py", 1, "cache", "reader", "shared-state")]
        assert reader.stats()["read_cap_hit"] is False


class TestBudget:
    def test_rank_puts_shared_state_last(self):
        assert REASONS[-1] == "shared-state"

    def test_shared_state_is_cut_first_under_a_tight_budget(self, monkeypatch):
        full, _ = _capped(3)
        definitions = [e for e in full.entries if e.kind == "definition"]
        budget = sum(len(e.rendered()) for e in definitions)
        chunk = [_file(IMPORTING, IMPORTING_TEXT, status="added")]
        reader = RepoReader(CAP_FILES.get, None, kind="repo-dir", chunk_cap=3)
        tight = build_unit_context(
            chunk, chunk, mode="repo", read=orchestrator._routed_read(reader, frozenset({IMPORTING})),
            max_chars=budget, listing_paths=frozenset(CAP_FILES), listing_complete=True,
        )

        assert list(tight.entries) == definitions
        assert tight.omitted == 1
        assert tight.definition_lines == full.definition_lines
        assert tight.reader_lines == (_omitted(1),)
        assert tight.contract_lines == ()

    def test_the_omitted_line_follows_a_reader_left_out_after_admitted_readers(self):
        unit = _simple("repo", max_chars=len(f"{OTHER}:1: ") + len(READER_TEXT.rstrip("\n")))

        assert [e.path for e in _readers(unit)] == [OTHER]
        assert unit.reader_lines == (_readers(unit)[0].rendered(), _omitted(1))
        assert unit.definition_lines == ()
        assert unit.contract_lines == ()


class TestRender:
    DEPS = ["effect@4.0.0"]
    DEFS = ["a.py:1: def a():"]
    EXTRA = ["b.java:9: record B() {"]
    CONTRACTS = ["s.yaml:3: B:"]
    READERS = ["c.py:4: def c(store):\n    return store.cache"]

    def test_empty_reader_lines_equal_the_output_without_them_byte_for_byte(self):
        without = render_context_blocks(self.DEPS, self.DEFS, self.EXTRA, self.CONTRACTS)

        assert render_context_blocks(self.DEPS, self.DEFS, self.EXTRA, self.CONTRACTS, ()) == without
        assert render_context_blocks(
            self.DEPS, self.DEFS, extra_def_lines=self.EXTRA, contract_lines=self.CONTRACTS, reader_lines=[],
        ) == without
        assert render_context_blocks([], [], reader_lines=()) == ""

    def test_reader_lines_render_last_under_their_own_header(self):
        without = render_context_blocks(self.DEPS, self.DEFS, self.EXTRA, self.CONTRACTS)
        out = render_context_blocks(self.DEPS, self.DEFS, self.EXTRA, self.CONTRACTS, self.READERS)

        assert out == without + "\n\n" + READER_HEADER + "\n\n" + self.READERS[0]
        order = [out.index(h) for h in (DEPENDENCY_HEADER, DEFINITIONS_HEADER, CONTRACT_HEADER, READER_HEADER)]
        assert order == sorted(order)
        assert out.count(READER_HEADER) == 1

    def test_reader_lines_alone(self):
        assert render_context_blocks([], [], reader_lines=self.READERS) == READER_HEADER + "\n\n" + self.READERS[0]

    def test_the_header_names_what_the_block_holds(self):
        assert READER_HEADER == "### Code elsewhere that reads state this chunk writes"


class TestPromptBlocks:
    CHUNK = [SimpleNamespace(path="pkg/writer.py", status="modified", hunks=[])]

    def test_context_blocks_render_a_unit_with_only_reader_lines_without_a_reader(self):
        unit = UnitContext((), (), (), 0, ("c.py:4: def c(store):",))

        out = orchestrator._context_blocks(self.CHUNK, None, include_definitions=True, unit=unit)

        assert out == READER_HEADER + "\n\nc.py:4: def c(store):"

    def test_context_blocks_put_the_reader_block_after_the_contracts(self):
        unit = UnitContext(("a.java:1: class A {}",), ("spec.yaml:2: /a:",), (), 0, ("c.py:4: def c(store):",))

        out = orchestrator._context_blocks(self.CHUNK, None, include_definitions=True, unit=unit)

        assert out == render_context_blocks(
            [], [], extra_def_lines=unit.definition_lines, contract_lines=unit.contract_lines,
            reader_lines=unit.reader_lines,
        )
        assert out.endswith(READER_HEADER + "\n\nc.py:4: def c(store):")

    def test_a_unit_without_reader_lines_renders_as_before(self):
        unit = UnitContext(("a.java:1: class A {}",), ("spec.yaml:2: /a:",), (), 0)

        out = orchestrator._context_blocks(self.CHUNK, None, include_definitions=True, unit=unit)

        assert out == render_context_blocks([], [], extra_def_lines=unit.definition_lines,
                                            contract_lines=unit.contract_lines)
        assert READER_HEADER not in out

    def test_positional_construction_and_the_empty_unit_still_work(self):
        assert EMPTY_UNIT == UnitContext((), (), (), 0) == UnitContext((), (), (), 0, ())
        assert EMPTY_UNIT.reader_lines == ()


PROGRESS = '''\
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
'''

ENGINE = '''\
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
'''

FEATURES = '''\
"""Feature toggles, read from the environment at startup."""
import os


def enabled(name: str, default: bool) -> bool:
    raw = os.environ.get(f"ASSISTANT_{name.upper()}")
    return default if raw is None else raw.lower() == "true"
'''

HISTORY = '''\
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
'''

MESSAGES = '''\
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
'''

SESSION = '''\
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
'''

STATE_STORE = '''\
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
'''

CONFTEST = '''\
import pytest


@pytest.fixture(autouse=True)
def _progress_notes_off(monkeypatch):
    # Keep exact message-table assertions deterministic; progress tests opt in.
    monkeypatch.setenv("ASSISTANT_PROGRESS_NOTES", "false")
'''

TEST_ENGINE = '''\
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
'''

TEST_PROGRESS = '''\
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
'''

ISSUE_22_HEAD = {
    "README.md": "# assistant\n",
    ".gitignore": "__pycache__/\n.pytest_cache/\n",
    "assistant/__init__.py": "",
    "assistant/engine.py": ENGINE,
    "assistant/features.py": FEATURES,
    "assistant/history.py": HISTORY,
    "assistant/messages.py": MESSAGES,
    "assistant/progress.py": PROGRESS,
    "assistant/session.py": SESSION,
    "assistant/state_store.py": STATE_STORE,
    "pyproject.toml": '[project]\nname = "assistant"\n',
    "tests/conftest.py": CONFTEST,
    "tests/test_engine.py": TEST_ENGINE,
    "tests/test_progress.py": TEST_PROGRESS,
}
ENGINE_ADDED = {6, 22, 31, 35, 36, 37, 38, 39, 40}


def _issue_22(mode: str) -> tuple[UnitContext, _Recording]:
    """The progress.py chunk of the #22 PR, built the way a worker builds it, over the head tree."""
    progress = _file("assistant/progress.py", PROGRESS, status="added")
    all_files = [
        _file(".gitignore", ISSUE_22_HEAD[".gitignore"], status="added"),
        _file("README.md", ISSUE_22_HEAD["README.md"], added={1}),
        _file("assistant/engine.py", ENGINE, added=ENGINE_ADDED),
        progress,
        _file("tests/conftest.py", CONFTEST, status="added"),
        _file("tests/test_progress.py", TEST_PROGRESS, status="added"),
    ]
    exclude = exclude_predicate()
    reader = RepoReader(ISSUE_22_HEAD.get, None, kind="repo-dir", exclude=exclude)
    read = _Recording(orchestrator._routed_read(reader, frozenset(f.path for f in all_files)))
    unit = build_unit_context(
        [progress], all_files, mode=mode, read=read, max_chars=MAX_CHARS,
        listing_paths=frozenset(ISSUE_22_HEAD), listing_complete=True, exclude=exclude,
    )
    return unit, read


class TestIssue22Shape:
    def test_the_progress_chunk_gets_the_state_store_and_history_readers(self):
        unit, _ = _issue_22("repo")
        readers = _readers(unit)

        assert [(e.path, e.line, e.symbol) for e in readers] == [
            ("assistant/history.py", 9, "table"),
            ("assistant/state_store.py", 15, "data"),
            ("tests/test_engine.py", 17, "data"),
        ]
        history, store, test_engine = readers
        assert history.text.splitlines()[-1] == "    for m in table.recent(session_id, HISTORY_WINDOW):"
        assert store.text.splitlines()[0] == "    def load(self, session_id: str) -> Run | None:"
        assert store.text.splitlines()[-1].endswith('f["data"]) for f in raw["frames"]]')
        assert test_engine.text.splitlines()[-1].endswith('.root().data == {"attempts": 1}')
        assert unit.reader_lines == tuple(entry.rendered() for entry in readers)

    def test_the_readers_reach_the_prompt_in_their_own_last_block(self):
        unit, _ = _issue_22("repo")

        out = orchestrator._context_blocks([], None, include_definitions=True, unit=unit)

        block = out.split(READER_HEADER + "\n\n", 1)[1]
        assert block.startswith("assistant/history.py:9: def model_history(")
        assert "assistant/state_store.py:15:     def load(self, session_id: str) -> Run | None:" in block
        assert CONTRACT_HEADER not in block and DEFINITIONS_HEADER not in block
        assert "assistant/state_store.py" not in out.split(READER_HEADER, 1)[0]

    def test_the_reader_source_walks_non_diff_files_only_after_the_other_sources(self, monkeypatch):
        unit, on = _issue_22("repo")
        monkeypatch.setattr(repo_readers, "MAX_READER_ENTRIES", 0)
        _, off = _issue_22("repo")

        assert on.calls[: len(off.calls)] == off.calls
        assert on.calls[len(off.calls):] == [
            "assistant/__init__.py",
            "assistant/features.py",
            "assistant/history.py",
            "assistant/messages.py",
            "assistant/session.py",
            "assistant/state_store.py",
            "tests/test_engine.py",
        ]
        assert "tests/test_progress.py" in off.calls
        assert not {e.path for e in _readers(unit)} & {"assistant/engine.py", "tests/test_progress.py"}

    @pytest.mark.parametrize("mode", ["diff", "off"])
    def test_diff_and_off_have_no_readers_and_never_read_them(self, mode):
        unit, read = _issue_22(mode)

        assert _readers(unit) == []
        assert READER_HEADER not in orchestrator._context_blocks([], None, include_definitions=True, unit=unit)
        assert "assistant/history.py" not in read.calls
        assert "assistant/state_store.py" not in read.calls
