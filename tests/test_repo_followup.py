"""The context follow-up's lookup half (#22): names, definitions and the prompt block."""
from __future__ import annotations

from pathlib import Path

import pytest

from prxref import chunk_context, repo_followup
from prxref.repo_context import exclude_predicate
from prxref.repo_followup import (
    FOLLOWUP_HEADER,
    FOLLOWUP_NOTE,
    FollowupExcerpt,
    definition_body,
    diff_defined_names,
    finding_names,
    lookup_excerpts,
    lookup_names,
    name_tiers,
    question_indices,
    render_followup_block,
)
from prxref.triage import Finding, build_chunks, parse_unified_diff

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issue22"
REPO = FIXTURE / "repo"

R2_BODY = (
    "`_ledger` puts a live `ProgressLedger` object into `run.root().data`. `Engine.run_turn` passes that "
    "same `run` to `self.store.save(run)` on handoff. `StateStore` is not shown here. Does it serialize "
    "frame data in a way that fails on arbitrary objects, e.g. JSON? The conftest turns the feature off for "
    "every existing test, so save/resume with the default-on setting looks untested."
)
SHOWN_017 = "assistant/state_store.py:8: class StateStore:"


class CountingReader:
    def __init__(self, texts: dict[str, str]) -> None:
        self.texts = texts
        self.calls: list[str] = []

    def __call__(self, path: str) -> str | None:
        self.calls.append(path)
        return self.texts.get(path)


@pytest.fixture(scope="module")
def pr():
    files = parse_unified_diff((FIXTURE / "pr.patch").read_text())
    return files, build_chunks(files)


@pytest.fixture(scope="module")
def texts() -> dict[str, str]:
    return {p.relative_to(REPO).as_posix(): p.read_text() for p in REPO.rglob("*") if p.is_file()}


def _lookup(pr, texts, names, *, shown=SHOWN_017, exclude=None, listing=True, reader=None):
    files, chunks = pr
    reader = reader or CountingReader(texts)
    excerpts = lookup_excerpts(
        chunks[0],
        files,
        names,
        read=reader,
        listing_paths=frozenset(texts) if listing else None,
        listing_complete=True,
        exclude=exclude or exclude_predicate([]),
        shown=shown,
    )
    return excerpts, reader


def _lookup_reads(pr, reader) -> list[str]:
    own = {f.path for f in pr[1][0]}
    return [path for path in reader.calls if path not in own]


def _finding(conf: float, title: str = "t", body: str = "b") -> Finding:
    return Finding("a.py", 1, "warning", conf, title, body)


def test_chunk0_is_the_mapped_chunk(pr):
    assert [f.path for f in pr[1][0]] == [
        "assistant/progress.py",
        "assistant/engine.py",
        ".gitignore",
        "README.md",
        "tests/conftest.py",
    ]


def test_r2_names_state_store_first_after_diff_defined_names(pr, texts):
    files, chunks = pr
    defined = diff_defined_names(files, chunks[0], CountingReader(texts))
    assert {"ProgressLedger", "Engine", "_ledger", "run_turn"} <= defined
    assert "StateStore" not in defined
    assert lookup_names([name_tiers("Serialization", R2_BODY)], defined=defined)[0] == "StateStore"
    assert lookup_names([finding_names("Serialization", R2_BODY)], defined=defined)[0] == "StateStore"


def test_engine_is_defined_only_through_the_chunk_head_text(pr):
    files, chunks = pr
    assert "Engine" not in diff_defined_names(files, chunks[0], None)
    assert "Engine" in diff_defined_names(files, chunks[0], lambda path: (REPO / path).read_text())


def test_state_store_excerpt_is_the_whole_class(pr, texts):
    excerpts, _ = _lookup(pr, texts, ["StateStore"])
    assert len(excerpts) == 1
    excerpt = excerpts[0]
    assert (excerpt.path, excerpt.line, excerpt.symbol) == ("assistant/state_store.py", 8, "StateStore")
    assert "json.dumps(asdict(run))" in excerpt.text
    assert "save" in excerpt.covers
    assert "__init__" not in excerpt.covers
    assert excerpt.source in repo_followup.SOURCES
    assert excerpt.record() == {
        "path": "assistant/state_store.py",
        "line": 8,
        "symbol": "StateStore",
        "source": excerpt.source,
        "chars": len(excerpt.rendered()),
    }
    assert excerpt.rendered().startswith("assistant/state_store.py:8: class StateStore:\n")


def test_covered_name_needs_no_further_read(pr, texts):
    excerpts, reader = _lookup(pr, texts, ["StateStore", "save"])
    assert [e.symbol for e in excerpts] == ["StateStore"]
    assert _lookup_reads(pr, reader)[-1] == "assistant/state_store.py"


def test_excerpt_already_shown_is_dropped(pr, texts):
    first, _ = _lookup(pr, texts, ["StateStore"])
    shown = "prelude\n" + first[0].text + "\ncoda"
    excerpts, _ = _lookup(pr, texts, ["StateStore"], shown=shown)
    assert excerpts == []


def test_char_cap_stops_at_the_first_misfit(pr, texts, monkeypatch):
    both, _ = _lookup(pr, texts, ["StateStore", "root"])
    assert [e.symbol for e in both] == ["StateStore", "root"]
    monkeypatch.setattr(repo_followup, "MAX_FOLLOWUP_CHARS", len(both[0].rendered()) - 1)
    excerpts, _ = _lookup(pr, texts, ["StateStore", "root"])
    assert excerpts == []


def test_excerpt_cap(pr, texts, monkeypatch):
    monkeypatch.setattr(repo_followup, "MAX_FOLLOWUP_EXCERPTS", 1)
    excerpts, _ = _lookup(pr, texts, ["StateStore", "root"])
    assert [e.symbol for e in excerpts] == ["StateStore"]


def test_read_cap_counts_every_candidate_read(pr, texts, monkeypatch):
    _, reader = _lookup(pr, texts, ["NoSuchSymbol"])
    assert len(_lookup_reads(pr, reader)) == len(set(_lookup_reads(pr, reader))) > 2
    monkeypatch.setattr(repo_followup, "MAX_FOLLOWUP_READS", 2)
    _, reader = _lookup(pr, texts, ["NoSuchSymbol"])
    assert len(_lookup_reads(pr, reader)) == 2


def test_read_cap_default_is_eight(pr, texts):
    many = dict(texts)
    many.update({f"assistant/extra_{i:02d}.py": "x = 1\n" for i in range(20)})
    _, reader = _lookup(pr, many, ["NoSuchSymbol"])
    assert len(_lookup_reads(pr, reader)) == repo_followup.MAX_FOLLOWUP_READS == 8


def test_excluded_and_diff_paths_are_never_read(pr, texts):
    files, chunks = pr
    excluded = exclude_predicate(["assistant/state_store.py"])
    excerpts, reader = _lookup(pr, texts, ["StateStore", "NoSuchSymbol"], exclude=excluded)
    assert "assistant/state_store.py" not in reader.calls
    assert all(e.path != "assistant/state_store.py" for e in excerpts)
    diff_paths = {f.path for f in files}
    own = {f.path for f in chunks[0]}
    assert not [path for path in reader.calls if path in diff_paths - own]
    assert "tests/test_progress.py" not in reader.calls


def test_exclude_that_raises_counts_as_excluded(pr, texts):
    def exclude(path: str) -> bool:
        raise RuntimeError(path)

    excerpts, reader = _lookup(pr, texts, ["StateStore"], exclude=exclude)
    assert excerpts == []
    assert _lookup_reads(pr, reader) == []


def test_no_listing_means_resolver_only(pr, texts):
    excerpts, reader = _lookup(pr, texts, ["root"], listing=False)
    assert excerpts == []
    assert _lookup_reads(pr, reader) == []


def test_lookup_never_raises(pr, texts):
    def boom(path: str) -> str:
        raise OSError(path)

    excerpts, _ = _lookup(pr, texts, ["StateStore"], reader=boom)
    assert excerpts == []


def test_no_names_or_no_reader_reads_nothing(pr, texts):
    _, reader = _lookup(pr, texts, [])
    assert reader.calls == []
    files, chunks = pr
    assert lookup_excerpts(
        chunks[0], files, ["StateStore"], read=None, listing_paths=frozenset(texts),
        listing_complete=True, exclude=None, shown="",
    ) == []


def test_python_body_stops_at_the_dedent():
    lines = [
        "class A:",
        "    def f(self):",
        "        return 1",
        "",
        "",
        "def after():",
        "    pass",
    ]
    assert definition_body(lines, 0, "python") == "class A:\n    def f(self):\n        return 1"
    assert definition_body(lines, 1, "python") == "    def f(self):\n        return 1"


def test_python_multiline_signature_continues_to_the_body():
    lines = ["def f(", "    a,", "    b,", ") -> int:", "    return a + b", "x = 1"]
    assert definition_body(lines, 0, "python") == "def f(\n    a,\n    b,\n) -> int:\n    return a + b"


def test_brace_body_with_the_brace_on_the_next_line():
    lines = [
        "public class Store",
        "{",
        "    void save(Run run) {",
        "        blobs.put(run.id, json(run));",
        "    }",
        "}",
        "class Other {}",
    ]
    assert definition_body(lines, 0, "java") == "\n".join(lines[:6])


def test_brace_body_on_one_line_and_balanced():
    lines = ["export function f(a) {", "  return a;", "}", "const g = 1;"]
    assert definition_body(lines, 0, "js") == "export function f(a) {\n  return a;\n}"
    assert definition_body(lines, 3, "js") == "const g = 1;"


def test_truncated_body_ends_with_more_lines_marker():
    lines = ["def big():", *[f"    x{i} = {i}" for i in range(40)]]
    body = definition_body(lines, 0, "python", max_lines=5).splitlines()
    assert len(body) == 5
    assert body[:4] == lines[:4]
    assert body[-1] == "    \N{HORIZONTAL ELLIPSIS} 37 more lines"
    default = definition_body(lines, 0, "python").splitlines()
    assert len(default) == repo_followup.MAX_FOLLOWUP_EXCERPT_LINES
    assert default[-1] == f"    \N{HORIZONTAL ELLIPSIS} {41 - 29} more lines"


def test_excerpt_line_cap_is_read_at_call_time(monkeypatch):
    lines = ["def big():", *[f"    x{i} = {i}" for i in range(10)]]
    monkeypatch.setattr(repo_followup, "MAX_FOLLOWUP_EXCERPT_LINES", 3)
    assert definition_body(lines, 0, "python").splitlines()[-1] == "    \N{HORIZONTAL ELLIPSIS} 9 more lines"


def test_name_tiers_rank_and_drop():
    tiers = name_tiers("`self.cache.get(key)`", "Uses `None`, `if`, `StoreX`, `ab`, and `this.loader`.")
    assert tiers == (("StoreX",), ("cache", "loader"), ("key",))


def test_plain_text_fallback_keeps_type_like_names_only():
    names = finding_names("Serialization", "StateStore is not shown. The Engine may break. Does it?")
    assert names == ["StateStore", "Engine"]


def test_lookup_names_merges_tiers_across_questions_and_caps(monkeypatch):
    first = name_tiers("q1", "`a_helper` and `Alpha` and `x.beta`")
    second = name_tiers("q2", "`Gamma` and `y.delta`")
    assert lookup_names([first, second], defined=(), max_names=10) == [
        "Alpha", "Gamma", "beta", "delta", "a_helper",
    ]
    assert lookup_names([first, second], defined={"Gamma"}) == ["Alpha", "beta", "delta"]
    monkeypatch.setattr(repo_followup, "MAX_FOLLOWUP_NAMES", 0)
    assert lookup_names([first, second], defined=()) == []


def test_question_indices_below_floor_best_first():
    findings = [_finding(0.5), _finding(0.9), _finding(0.3), _finding(0.5), _finding(0.6)]
    assert question_indices(findings, 0.6) == [0, 3, 2]
    assert question_indices(findings, 0.0) == []


def test_render_followup_block_layout():
    assert render_followup_block([]) == ""
    excerpt = FollowupExcerpt("a.py", 3, "A", (), "import", "class A:\n    pass")
    assert render_followup_block([excerpt]) == (
        f"{FOLLOWUP_HEADER}\n\n{FOLLOWUP_NOTE}\n\na.py:3: class A:\n    pass"
    )
    assert FOLLOWUP_HEADER.startswith(chunk_context.DEFINITIONS_HEADER)
