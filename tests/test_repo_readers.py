"""Tests for the shared-state reader search (issue #22, readers of state the PR writes).

Covers :mod:`prxref.repo_readers`: the write forms and the added-assignment
skip in ``shared_state_keys``, the read forms and the excerpt window in
``reader_matches``, candidate ordering in ``reader_candidates``, and in
``reader_entries`` the caps read at call time, the scan order and the paths
never read. The last test runs the #22 fixture's shape over trimmed inline
copies of its files. No test touches the network.
"""
from __future__ import annotations

import pytest

from prxref import chunk_context, repo_readers
from prxref.chunk_context import ChunkFile
from prxref.repo_readers import (
    WRITE_VERBS,
    reader_candidates,
    reader_entries,
    reader_matches,
    shared_state_keys,
)

ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"


class _Recording:
    """A ``read(path)`` over a dict that records every path asked for, in call order."""

    def __init__(self, files: dict[str, str]):
        self.files = files
        self.calls: list[str] = []

    def __call__(self, path: str) -> str | None:
        self.calls.append(path)
        return self.files.get(path)


def _writer(path: str, *added: str) -> ChunkFile:
    return ChunkFile(path=path, added=added)


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ("    run.root().data[LEDGER_KEY] = ledger", "data"),
        ("self.cache[key] = value", "cache"),
        ("self.counts[key] += 1", "counts"),
        ("this.index[a][b] = v;", "index"),
        ("registry[name] = handler", "registry"),
    ],
)
def test_subscript_store_writes_the_receiver(line, key):
    assert shared_state_keys([line]) == [key]


def test_subscript_comparison_and_annotation_are_not_writes():
    assert shared_state_keys([
        "assert self.cache[k] == v",
        "if run.data[k] != v:",
        "frames: list[Frame] = field(default_factory=list)",
    ]) == []


def test_bare_subscript_store_resolves_an_alias_to_its_attribute():
    assert shared_state_keys(["data = run.root().data", "data[LEDGER_KEY] = ledger"]) == ["data"]
    assert shared_state_keys(["rows = self.table", "rows[0] = row"]) == ["table"]


@pytest.mark.parametrize("verb", WRITE_VERBS)
def test_every_write_verb_writes_the_last_receiver_segment(verb):
    assert shared_state_keys([f"self.table.{verb}(row)"]) == ["table"]


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ("this.items.push(item);", "items"),
        ("app.state.cache.save(entry)", "cache"),
        ("repo.store(entry)", "repo"),
        ("self.queue?.put(job)", "queue"),
    ],
)
def test_write_call_forms(line, key):
    assert shared_state_keys([line]) == [key]


def test_write_call_on_self_or_with_another_method_gives_nothing():
    assert shared_state_keys([
        "self.save(run)",
        "this.add(x);",
        "super.update(x)",
        "self.table.recent(5)",
        "rows.addAll(more)",
        "self.table.appendix(row)",
        "get_table().append(row)",
    ]) == []


@pytest.mark.parametrize(
    "added",
    [
        ["items = []", "items.append(1)"],
        ["self._lines: list[str] = []", "self._lines.append(line)"],
        ["self.cache = {}", "self.cache[key] = value"],
        ["List<String> names = new ArrayList<>();", "names.add(name);"],
        ["const seen = new Set();", "seen.add(id);"],
        ["ledger = Ledger(turn)", "ledger.save()"],
        ["buffer = []  # scratch", "buffer.extend(rows)"],
    ],
)
def test_added_assignment_of_a_new_value_skips_the_key(added):
    assert shared_state_keys(added) == []


@pytest.mark.parametrize(
    ("added", "keys"),
    [
        (["self.table = table", "self.table.append(row)"], ["table"]),
        (["data = run.root().data", "data[KEY] = ledger"], ["data"]),
        (["this.store = store;", "this.store.put(k, v);"], ["store"]),
        (["announce(table=[])", "table.append(row)"], ["table"]),
    ],
)
def test_assignment_of_existing_state_keeps_the_key(added, keys):
    assert shared_state_keys(added) == keys


def test_comment_and_import_lines_write_nothing():
    assert shared_state_keys([
        "# self.cache[k] = v",
        "// this.items.push(x)",
        " * registry.add(handler)",
        "from app.data import update",
    ]) == []


def test_keys_are_in_first_appearance_order_and_deduplicated():
    assert shared_state_keys(["self.b.add(1)", "self.a[k] = 1; self.c.put(k)", "self.b.add(2)"]) == ["b", "a", "c"]


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ("    return run.data", "data"),
        ("    return run.data == {}", "data"),
        ("    return self.data()", "data"),
        ('    return raw["data"]', "data"),
        ("    return raw['data']", "data"),
        ("    return table.recent(5)", "table"),
        ("    return self.table.recent(5)", "table"),
        ("    return table?.recent(5)", "table"),
    ],
)
def test_read_forms(body, key):
    text = f"def reader(run, raw, table):\n{body}\n"
    assert reader_matches(text, ["data", "table"], language="python") == [
        (key, 1, f"def reader(run, raw, table):\n{body}")
    ]


@pytest.mark.parametrize(
    "body",
    [
        "    run.data = 1",
        "    run.data += 1",
        "    run.data[k] = v",
        "    run.data.append(v)",
        "    self.data: dict = {}",
        '    raw["data"] = v',
        '    raw["data"]["k"] = v',
        '    raw["data"].update(v)',
        "    table.append(row)",
        "    self.table.save(row)",
    ],
)
def test_a_store_is_not_a_read(body):
    text = f"def writer(run, raw, table):\n{body}\n"
    assert reader_matches(text, ["data", "table"], language="python") == []


def test_reads_are_word_bounded_and_skip_comments_and_imports():
    text = "\n".join([
        "from app.data import loader",
        "# reads self.data",
        "def f(x, raw, message_table):",
        "    a = MessageTable.recent(1)",
        "    b = message_table.recent(1)",
        "    c = x.database",
        '    d = raw["data2"]',
    ])
    assert reader_matches(text, ["data", "table"], language="python") == []


STORE = """\
class StateStore:
    def save(self, run):
        self._blobs[run.session_id] = dump(run)

    def load(self, session_id):
        blob = self._blobs.get(session_id)
        raw = parse(blob)
        return [f["data"] for f in raw["frames"]]
"""


def test_excerpt_starts_at_the_enclosing_definition_not_a_sibling():
    ((key, line, text),) = reader_matches(STORE, ["data"], language="python")
    assert (key, line) == ("data", 5)
    assert text.splitlines()[0] == "    def load(self, session_id):"
    assert text.splitlines()[-1] == '        return [f["data"] for f in raw["frames"]]'
    assert len(text.splitlines()) == 4


def test_excerpt_of_exactly_the_line_cap_is_whole():
    body = [f"    step{n} = {n}" for n in range(6)]
    text = "\n".join(["def f(run):", *body, "    return run.data"])
    ((_, line, excerpt),) = reader_matches(text, ["data"], language="python")
    assert line == 1
    assert excerpt.splitlines() == text.splitlines()
    assert len(excerpt.splitlines()) == repo_readers.MAX_READER_LINES


def test_long_excerpt_keeps_the_definition_line_and_elides_the_middle():
    body = [f"    step{n} = {n}" for n in range(20)]
    lines = ["def f(run):", *body, "    return run.data"]
    ((_, line, excerpt),) = reader_matches("\n".join(lines), ["data"], language="python")
    assert line == 1
    got = excerpt.splitlines()
    assert len(got) == repo_readers.MAX_READER_LINES
    assert got[0] == "def f(run):"
    assert got[1] == f"    {ELLIPSIS}"
    assert got[2:] == lines[-6:]


def test_line_cap_is_read_at_call_time(monkeypatch):
    monkeypatch.setattr(repo_readers, "MAX_READER_LINES", 4)
    lines = ["def f(run):", "    a = 1", "    b = 2", "    c = 3", "    return run.data"]
    ((_, _, excerpt),) = reader_matches("\n".join(lines), ["data"], language="python")
    assert excerpt.splitlines() == ["def f(run):", f"    {ELLIPSIS}", "    c = 3", "    return run.data"]


def test_module_level_read_starts_at_the_reading_line():
    text = "import os\n\nLIMIT = 3\nprint(cfg.data)\n\ndef helper():\n    pass\n\nif ready:\n    show(cfg.data)\n"
    assert reader_matches(text, ["data"], language="python") == [
        ("data", 4, "print(cfg.data)"),
        ("data", 10, "    show(cfg.data)"),
    ]


def test_one_entry_per_enclosing_definition():
    text = "def a(x):\n    one = x.data\n    two = x.data\n\ndef b(x):\n    return x.data\n"
    assert [(line, text.splitlines()[0]) for _, line, text in reader_matches(text, ["data"], language="python")] == [
        (1, "def a(x):"),
        (5, "def b(x):"),
    ]


def test_java_method_is_the_enclosing_definition():
    text = """\
package com.acme.store;

import com.acme.data.Loader;

public class Store {
    private final Map<String, Object> data = new HashMap<>();

    @Override
    public Object load(String key) {
        if (key == null) {
            return null;
        }
        return cache.data.get(key);
    }
}
"""
    ((key, line, excerpt),) = reader_matches(text, ["data"], language="java")
    assert (key, line) == ("data", 9)
    assert excerpt.splitlines()[0] == "    public Object load(String key) {"
    assert excerpt.splitlines()[-1] == "        return cache.data.get(key);"


def test_js_method_is_the_enclosing_definition_not_a_local_const():
    text = """\
export class Store {
  load(id) {
    const raw = JSON.parse(this.blobs.get(id));
    if (raw) {
      return raw["data"];
    }
  }
}
"""
    ((_, line, excerpt),) = reader_matches(text, ["data"], language="js")
    assert line == 2
    assert excerpt.splitlines()[0] == "  load(id) {"


def test_kotlin_and_go_functions_are_enclosing_definitions():
    kotlin = "class Store {\n    fun load(id: String): Any? {\n        return frames.data[id]\n    }\n}\n"
    assert [line for _, line, _ in reader_matches(kotlin, ["data"], language="kotlin")] == [2]
    go = "package store\n\nfunc (s *Store) Load(id string) string {\n\tvalue := s.data[id]\n\treturn value\n}\n"
    assert [line for _, line, _ in reader_matches(go, ["data"], language="go")] == [3]


def test_oversized_or_empty_text_or_no_key_gives_nothing():
    big = "def f(x):\n    return x.data\n" + "#" * (chunk_context.MAX_FILE_BYTES + 1)
    assert reader_matches(big, ["data"], language="python") == []
    assert reader_matches("", ["data"], language="python") == []
    assert reader_matches("def f(x):\n    return x.data\n", [], language="python") == []


def test_candidates_are_ordered_by_shared_depth_then_path():
    listing = [
        "other/c.py",
        "svc/b.py",
        "svc/app/z.py",
        "svc/app/core/sub/b.py",
        "svc/app/core/a.py",
        "svc/app/core/writer.py",
        "svc/app/core/x.js",
        "svc/app/diffed.py",
        "README.md",
        "svc/app/core/a.py",
    ]
    assert reader_candidates(["svc/app/core/writer.py"], listing, diff_paths={"svc/app/diffed.py"}) == [
        "svc/app/core/a.py",
        "svc/app/core/sub/b.py",
        "svc/app/z.py",
        "svc/b.py",
        "other/c.py",
    ]


def test_candidates_rank_against_the_nearest_changed_file_of_their_language():
    listing = ["b/c/z.py", "a/w.py", "b/c/v.ts", "a/u.ts", "a/doc.md"]
    assert reader_candidates(["a/x.py", "b/c/y.ts"], listing) == ["b/c/v.ts", "a/w.py", "a/u.ts", "b/c/z.py"]


def _many(count: int, body: str) -> dict[str, str]:
    return {f"pkg/m{n:02}.py": body for n in range(count)}


WRITER = _writer("pkg/writer.py", "self.data[key] = value")
READS = "def f(x):\n    return x.data\n"


def test_entry_cap_stops_the_walk():
    read = _Recording(_many(10, READS))
    entries = reader_entries([WRITER], listing=set(read.files), read=read)
    assert len(entries) == repo_readers.MAX_READER_ENTRIES == 6
    assert read.calls == sorted(read.files)[:6]


def test_entry_cap_is_read_at_call_time(monkeypatch):
    monkeypatch.setattr(repo_readers, "MAX_READER_ENTRIES", 2)
    read = _Recording(_many(10, READS))
    assert len(reader_entries([WRITER], listing=set(read.files), read=read)) == 2
    assert len(read.calls) == 2


def test_entry_cap_patched_to_zero_gives_nothing_and_reads_nothing(monkeypatch):
    monkeypatch.setattr(repo_readers, "MAX_READER_ENTRIES", 0)
    read = _Recording(_many(10, READS))
    assert reader_entries([WRITER], listing=set(read.files), read=read) == []
    assert read.calls == []


def test_scan_cap_bounds_read_attempts():
    read = _Recording(_many(30, "VALUE = 1\n"))
    assert reader_entries([WRITER], listing=set(read.files), read=read) == []
    assert read.calls == sorted(read.files)[: repo_readers.MAX_READER_SCAN]
    assert repo_readers.MAX_READER_SCAN == 24


def test_scan_cap_counts_reads_that_answer_none(monkeypatch):
    monkeypatch.setattr(repo_readers, "MAX_READER_SCAN", 3)
    read = _Recording({})
    listing = set(_many(30, ""))
    assert reader_entries([WRITER], listing=listing, read=read) == []
    assert read.calls == sorted(listing)[:3]


def test_excluded_candidates_are_neither_read_nor_counted(monkeypatch):
    monkeypatch.setattr(repo_readers, "MAX_READER_SCAN", 4)
    read = _Recording(_many(12, "VALUE = 1\n"))

    def exclude(path: str) -> bool:
        return int(path[-5:-3]) % 2 == 0

    reader_entries([WRITER], listing=set(read.files), read=read, exclude=exclude)
    assert read.calls == ["pkg/m01.py", "pkg/m03.py", "pkg/m05.py", "pkg/m07.py"]


def test_an_exclude_that_raises_excludes():
    def exclude(path: str) -> bool:
        raise RuntimeError(path)

    read = _Recording(_many(3, READS))
    assert reader_entries([WRITER], listing=set(read.files), read=read, exclude=exclude) == []
    assert read.calls == []


def test_diff_files_and_other_languages_are_never_read():
    files = {
        "pkg/changed.py": READS,
        "pkg/view.ts": "function f(x) {\n  return x.data;\n}\n",
        "pkg/reader.py": READS,
    }
    read = _Recording(files)
    entries = reader_entries([WRITER], listing=set(files), read=read, diff_paths={"pkg/changed.py"})
    assert read.calls == ["pkg/reader.py"]
    assert [(e.path, e.line, e.symbol, e.kind, e.reason) for e in entries] == [
        ("pkg/reader.py", 1, "data", "reader", "shared-state")
    ]


def test_nothing_is_read_without_a_reader_listing_or_key():
    read = _Recording(_many(3, READS))
    assert reader_entries([WRITER], listing=None, read=read) == []
    assert reader_entries([WRITER], listing=set(), read=read) == []
    assert reader_entries([WRITER], listing=set(read.files), read=None) == []
    assert reader_entries([_writer("pkg/writer.py", "x = 1")], listing=set(read.files), read=read) == []
    assert reader_entries([_writer("notes.md", "self.data[k] = v")], listing=set(read.files), read=read) == []
    assert read.calls == []


PROGRESS = '''\
"""Progress notes: a short user-facing line announced before each step runs."""
from assistant.features import enabled
from assistant.messages import Message, MessageTable
from assistant.session import Run

LEDGER_KEY = "progress.ledger"


class ProgressLedger:
    def __init__(self, turn_id: str) -> None:
        self._turn_id = turn_id
        self._lines: list[str] = []

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
class Engine:
    def __init__(self, table: MessageTable, store: StateStore) -> None:
        self.table = table
        self.store = store

    def run_turn(self, run: Run, steps: list[Step]) -> str | None:
        for step in steps:
            if step.kind == "handoff":
                self.store.save(run)
                return None
            self.table.append(Message(run.session_id, "assistant", step.execute(run)))
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
@dataclass
class Frame:
    agent_id: str
    # Scratch space any engine component can use for per-frame bookkeeping.
    data: dict[str, Any] = field(default_factory=dict)
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

TEST_ENGINE = '''\
def test_handoff_parks_and_resumes():
    store = StateStore()
    engine = Engine(MessageTable(), store)
    run = make_run()
    run.root().data["attempts"] = 1
    assert engine.run_turn(run, [Step("handoff", "human", lambda r: "")]) is None
    assert store.load("s1").root().data == {"attempts": 1}
'''


def test_issue_22_fixture_readers_of_the_ledger_map_and_the_message_table():
    files = {
        "README.md": "# assistant\n",
        "assistant/__init__.py": "",
        "assistant/engine.py": ENGINE,
        "assistant/history.py": HISTORY,
        "assistant/messages.py": MESSAGES,
        "assistant/progress.py": PROGRESS,
        "assistant/session.py": SESSION,
        "assistant/state_store.py": STATE_STORE,
        "tests/conftest.py": "",
        "tests/test_engine.py": TEST_ENGINE,
    }
    diff_paths = {"assistant/engine.py", "assistant/progress.py", "tests/conftest.py", "README.md"}
    assert shared_state_keys(PROGRESS.splitlines()) == ["table", "data"]
    read = _Recording(files)
    entries = reader_entries(
        [_writer("assistant/progress.py", *PROGRESS.splitlines())],
        listing=set(files),
        read=read,
        diff_paths=diff_paths,
    )
    assert read.calls == [
        "assistant/__init__.py",
        "assistant/history.py",
        "assistant/messages.py",
        "assistant/session.py",
        "assistant/state_store.py",
        "tests/test_engine.py",
    ]
    assert [(e.path, e.line, e.symbol) for e in entries] == [
        ("assistant/history.py", 9, "table"),
        ("assistant/state_store.py", 15, "data"),
        ("tests/test_engine.py", 1, "data"),
    ]
    assert {(e.kind, e.reason) for e in entries} == {("reader", "shared-state")}
    history, store, _ = entries
    assert history.text.splitlines()[0].startswith("def model_history(")
    assert history.text.splitlines()[-1] == "    for m in table.recent(session_id, HISTORY_WINDOW):"
    assert store.text.splitlines()[0] == "    def load(self, session_id: str) -> Run | None:"
    assert store.text.splitlines()[-1].endswith('f["data"]) for f in raw["frames"]]')
    assert store.rendered().startswith("assistant/state_store.py:15:     def load(")
