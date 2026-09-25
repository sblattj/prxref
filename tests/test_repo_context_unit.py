"""Tests for the per-chunk repository-context unit (issue #17).

Covers :func:`prxref.repo_unit.build_unit_context` (sources, order, budget,
the omitted line, exclusion and the guard), :func:`prxref.repo_context.exclude_predicate`
with :data:`prxref.repo_context.EXCLUDE_FLOOR`, and the two optional arguments
of :func:`prxref.chunk_context.render_context_blocks`.

The fixture half runs the real parser, chunker, sources and capped reader over
``tests/fixtures/issue17``. The budget and order half replaces the three
sources bound in ``prxref.repo_unit`` with canned entries. The rest builds
duck-typed ``triage.FileDiff`` stand-ins so each rule is pinned on its own.
No test touches the network.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from prxref import config, repo_unit
from prxref.chunk_context import (
    CONTRACT_HEADER,
    DEFINITIONS_HEADER,
    DEPENDENCY_HEADER,
    render_context_blocks,
)
from prxref.forges.repo_dir import RepoDir
from prxref.repo_context import EXCLUDE_FLOOR, REASONS, ContextEntry, exclude_predicate
from prxref.repo_contracts import literal_contract_paths, select_contract_files
from prxref.repo_reader import RepoReader, repo_dir_reader
from prxref.repo_resolve import Candidate
from prxref.repo_unit import EMPTY_UNIT, MODES, UnitContext, build_unit_context
from prxref.rules import match_globs
from prxref.triage import build_chunks, parse_unified_diff

FIXTURE = Path(__file__).parent / "fixtures" / "issue17"
REPO = FIXTURE / "repo"
TRANSPORT_CONFIG = "src/main/java/com/acme/connectors/TransportConfig.java"
CONNECTOR_SERVICE = "src/main/java/com/acme/connectors/ConnectorService.java"
MIGRATION = "db/changelog/003-idempotency-unique.sql"
IDEMPOTENCY_TABLE = "db/changelog/002-create-idempotency-keys.sql"
SPEC = "api/openapi/connectors.yaml"
EXCLUSIVITY = "exactly one of url or legacyUrl must be set"
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

    def distinct(self) -> list[str]:
        return list(dict.fromkeys(self.calls))


def _texts(texts: dict[str, str]) -> _Recording:
    return _Recording(texts.get)


def _repo_text(path: str) -> str | None:
    target = REPO / path
    return target.read_text(encoding="utf-8") if target.is_file() else None


def _keys(entries) -> list[tuple[str, int, str, str, str]]:
    return [(e.path, e.line, e.symbol, e.kind, e.reason) for e in entries]


def _file(path: str, *hunks: tuple[int, list[str]], status: str = "modified") -> SimpleNamespace:
    """A FileDiff stand-in; each hunk is ``(new_start, lines)`` with a ``+``/``-``/`` `` prefix per line."""
    built = []
    for new_start, body in hunks:
        number = new_start
        lines = []
        for raw in body:
            kind, text = raw[0], raw[1:]
            if kind == "-":
                lines.append(SimpleNamespace(kind="-", text=text, new_line=None))
            else:
                lines.append(SimpleNamespace(kind=kind, text=text, new_line=number))
                number += 1
        built.append(SimpleNamespace(lines=lines))
    return SimpleNamespace(path=path, status=status, hunks=built)


def _run() -> SimpleNamespace:
    """The once-per-run inputs, built the way the orchestrator builds them."""
    files = parse_unified_diff((FIXTURE / "pr.diff").read_text(encoding="utf-8"))
    chunks = build_chunks(files, max_files_per_chunk=1)
    exclude = exclude_predicate()
    reader = repo_dir_reader(RepoDir(REPO), exclude=exclude)
    listing = reader.listing()
    globs = config._DEFAULTS["context_contract_globs"]
    return SimpleNamespace(
        files=files,
        chunks=chunks,
        reader=reader,
        exclude=exclude,
        listing=listing,
        listing_paths=frozenset(listing.paths),
        listing_complete=listing.complete,
        contract_paths=select_contract_files(
            globs, listing=listing.paths, diff_paths=[f.path for f in files if f.status != "removed"]
        ),
        contract_priority=literal_contract_paths(globs),
    )


def _chunk_holding(chunks, path: str):
    return next(chunk for chunk in chunks if any(f.path == path for f in chunk))


def _unit(run, path: str, *, mode: str, read, **overrides) -> UnitContext:
    kwargs = dict(
        mode=mode,
        read=read,
        max_chars=MAX_CHARS,
        listing_paths=run.listing_paths,
        listing_complete=run.listing_complete,
        contract_paths=run.contract_paths,
        contract_priority=run.contract_priority,
        exclude=run.exclude,
    )
    kwargs.update(overrides)
    return build_unit_context(_chunk_holding(run.chunks, path), run.files, **kwargs)


def _rendered(unit: UnitContext) -> str:
    return render_context_blocks(
        [], [], extra_def_lines=unit.definition_lines, contract_lines=unit.contract_lines
    )


class TestFixtureRepoMode:
    def test_run_inputs_match_what_t6_observed(self):
        run = _run()
        assert run.listing_complete is True
        assert run.contract_paths == [
            SPEC,
            "db/changelog/001-create-connectors.sql",
            IDEMPOTENCY_TABLE,
            MIGRATION,
        ]
        assert run.contract_priority == []
        assert [[f.path for f in chunk] for chunk in run.chunks] == [
            [CONNECTOR_SERVICE],
            [TRANSPORT_CONFIG],
            [MIGRATION],
        ]

    def test_connector_service_chunk_gets_cross_chunk_then_contract_entries(self):
        run = _run()
        unit = _unit(run, CONNECTOR_SERVICE, mode="repo", read=run.reader.chunk_reader())

        assert _keys(unit.entries) == [
            (TRANSPORT_CONFIG, 9, "TransportConfig", "definition", "cross-chunk"),
            (TRANSPORT_CONFIG, 11, "TransportConfig", "definition", "cross-chunk"),
            (SPEC, 6, "/connectors/{connectorId}/transports", "contract", "contract"),
            (SPEC, 31, "TransportConfig", "contract", "contract"),
            (SPEC, 41, "CreateTransportRequest", "contract", "contract"),
        ]
        assert unit.omitted == 0
        assert unit.definition_lines == tuple(e.rendered() for e in unit.entries[:2])
        assert unit.contract_lines == tuple(e.rendered() for e in unit.entries[2:])
        assert "mutually exclusive" in unit.entries[3].text

    def test_connector_service_prompt_holds_both_blocks(self):
        run = _run()
        unit = _unit(run, CONNECTOR_SERVICE, mode="repo", read=run.reader.chunk_reader())
        text = _rendered(unit)

        assert text.startswith(DEFINITIONS_HEADER + "\n\n")
        assert text.count(CONTRACT_HEADER) == 1
        definitions, contracts = text.split(CONTRACT_HEADER)
        assert EXCLUSIVITY in definitions
        assert "if (hasUrl == hasLegacyUrl) {" in definitions
        assert f"{SPEC}:31: TransportConfig:" in contracts
        assert "mutually exclusive" in contracts

    def test_migration_chunk_gets_the_table_and_the_schema(self):
        run = _run()
        unit = _unit(run, MIGRATION, mode="repo", read=run.reader.chunk_reader())

        assert _keys(unit.entries) == [
            (SPEC, 50, "IdempotencyKey", "contract", "contract"),
            (IDEMPOTENCY_TABLE, 4, "idempotency_keys", "contract", "contract"),
        ]
        assert unit.entries[1].text.startswith("CREATE TABLE idempotency_keys (")
        assert unit.definition_lines == ()
        assert unit.contract_lines == tuple(e.rendered() for e in unit.entries)

    def test_transport_config_chunk_gets_its_schema_and_nothing_from_the_diff(self):
        run = _run()
        repo = _unit(run, TRANSPORT_CONFIG, mode="repo", read=run.reader.chunk_reader())
        diff = _unit(run, TRANSPORT_CONFIG, mode="diff", read=run.reader.chunk_reader())

        assert diff.entries == ()
        assert _keys(repo.entries) == [(SPEC, 31, "TransportConfig", "contract", "contract")]

    def test_connector_service_reads_and_zero_resolver_candidate_reads(self):
        run = _run()
        read = _Recording(run.reader.chunk_reader())
        _unit(run, CONNECTOR_SERVICE, mode="repo", read=read)

        assert read.distinct() == [TRANSPORT_CONFIG, CONNECTOR_SERVICE, SPEC]
        assert read.calls == [
            TRANSPORT_CONFIG,
            CONNECTOR_SERVICE,
            SPEC,
            CONNECTOR_SERVICE,
        ]
        assert run.reader.stats()["reads"] == 3

    def test_the_complete_listing_is_what_keeps_the_resolver_from_reading(self):
        run = _run()
        read = _Recording(run.reader.chunk_reader())
        unit = _unit(run, CONNECTOR_SERVICE, mode="repo", read=read, listing_complete=False)

        convention = "src/main/java/com/acme/connectors/"
        assert read.distinct() == [
            TRANSPORT_CONFIG,
            CONNECTOR_SERVICE,
            SPEC,
            convention + "Tenant.java",
            convention + "Id.java",
            convention + "CreateTransportRequest.java",
        ]
        assert [e.reason for e in unit.entries] == ["cross-chunk"] * 2 + ["contract"] * 3


class TestFixtureDiffModeAndNoReader:
    def test_diff_mode_gives_only_the_cross_chunk_entries_and_reads_no_contract(self):
        run = _run()
        read = _Recording(run.reader.chunk_reader())
        unit = _unit(run, CONNECTOR_SERVICE, mode="diff", read=read)

        assert _keys(unit.entries) == [
            (TRANSPORT_CONFIG, 9, "TransportConfig", "definition", "cross-chunk"),
            (TRANSPORT_CONFIG, 11, "TransportConfig", "definition", "cross-chunk"),
        ]
        assert unit.contract_lines == ()
        assert read.calls == [TRANSPORT_CONFIG]
        assert SPEC not in read.calls

    def test_diff_without_a_reader_gives_the_hunk_only_entries(self):
        run = _run()
        unit = _unit(run, CONNECTOR_SERVICE, mode="diff", read=None)

        assert [(e.path, e.line, e.reason) for e in unit.entries] == [
            (TRANSPORT_CONFIG, 9, "cross-chunk"),
            (TRANSPORT_CONFIG, 11, "cross-chunk"),
        ]
        assert EXCLUSIVITY in unit.entries[1].text
        assert run.reader.stats()["reads"] == 0

    @pytest.mark.parametrize("path", [CONNECTOR_SERVICE, TRANSPORT_CONFIG, MIGRATION])
    def test_repo_without_a_reader_equals_diff_without_a_reader(self, path):
        run = _run()
        repo = _unit(run, path, mode="repo", read=None)
        diff = _unit(run, path, mode="diff", read=None)
        assert repo == diff

    def test_off_is_the_empty_unit_with_zero_reads(self):
        run = _run()
        read = _Recording(run.reader.chunk_reader())
        unit = _unit(run, CONNECTOR_SERVICE, mode="off", read=read)

        assert unit is EMPTY_UNIT
        assert read.calls == []
        assert run.reader.stats()["reads"] == 0

    def test_the_same_inputs_give_the_same_unit_and_the_same_reads(self):
        first_run, second_run = _run(), _run()
        first = _Recording(_repo_text)
        second = _Recording(_repo_text)
        a = _unit(first_run, CONNECTOR_SERVICE, mode="repo", read=first)
        b = _unit(second_run, CONNECTOR_SERVICE, mode="repo", read=second)
        assert a == b
        assert first.calls == second.calls


class TestReadCap:
    def test_the_real_chunk_cap_degrades_to_fewer_entries(self):
        run = _run()
        capped = RepoReader(RepoDir(REPO).read, None, kind="repo-dir", exclude=run.exclude, chunk_cap=2)
        unit = _unit(run, CONNECTOR_SERVICE, mode="repo", read=capped.chunk_reader())

        assert [e.reason for e in unit.entries] == ["cross-chunk", "cross-chunk"]
        assert capped.stats()["read_cap_hit"] is True
        assert capped.stats()["reads"] == 2

    @pytest.mark.parametrize(
        ("allowed", "count"),
        [(0, 2), (1, 2), (2, 2), (3, 5), (4, 5), (5, 5)],
    )
    def test_a_reader_that_goes_dry_raises_nothing(self, allowed, count):
        run = _run()
        calls: list[str] = []

        def read(path: str) -> str | None:
            calls.append(path)
            return _repo_text(path) if len(calls) <= allowed else None

        unit = _unit(run, CONNECTOR_SERVICE, mode="repo", read=read)
        assert len(unit.entries) == count
        assert unit.omitted == 0


@pytest.fixture
def canned(monkeypatch):
    """Replace the three sources ``repo_unit`` binds with canned entry lists."""
    sources: dict[str, list[ContextEntry]] = {"diff": [], "contract": [], "resolver": []}
    called: list[str] = []

    def diff(chunk, all_files, read):
        called.append("diff")
        return list(sources["diff"])

    def contracts(chunk, *, contract_paths, read, priority=()):
        called.append("contract")
        return list(sources["contract"])

    def resolver(chunk, all_files, read, **kwargs):
        called.append("resolver")
        return list(sources["resolver"])

    monkeypatch.setattr(repo_unit, "diff_definitions", diff)
    monkeypatch.setattr(repo_unit, "contract_entries", contracts)
    monkeypatch.setattr(repo_unit, "_resolver_entries", resolver)
    return SimpleNamespace(sources=sources, called=called)


def _entry(path: str, line: int, reason: str, *, kind: str = "definition", text: str = "t") -> ContextEntry:
    return ContextEntry(path, line, "S", kind, reason, text)


def _build(max_chars: int = MAX_CHARS, **kwargs) -> UnitContext:
    kwargs.setdefault("mode", "repo")
    kwargs.setdefault("read", lambda path: None)
    return build_unit_context([], [], max_chars=max_chars, **kwargs)


class TestBudget:
    def test_admits_exactly_the_prefix_that_fits_and_appends_the_omitted_line(self, canned):
        a = _entry("a.java", 1, "cross-chunk", text="a" * 20)
        b = _entry("b.java", 1, "cross-chunk", text="b" * 20)
        c = _entry("c.java", 1, "cross-chunk", text="c" * 20)
        canned.sources["diff"] = [a, b, c]
        size = len(a.rendered())

        unit = _build(max_chars=2 * size)

        assert unit.entries == (a, b)
        assert unit.omitted == 1
        assert unit.definition_lines == (a.rendered(), b.rendered(), _omitted(1))
        assert unit.contract_lines == ()

    def test_one_char_short_admits_one_fewer(self, canned):
        a = _entry("a.java", 1, "cross-chunk", text="a" * 20)
        b = _entry("b.java", 1, "cross-chunk", text="b" * 20)
        canned.sources["diff"] = [a, b]

        unit = _build(max_chars=len(a.rendered()) + len(b.rendered()) - 1)

        assert unit.entries == (a,)
        assert unit.definition_lines == (a.rendered(), _omitted(1))

    def test_a_later_smaller_entry_is_not_admitted_after_a_non_fit(self, canned):
        small = _entry("a.java", 1, "cross-chunk", text="s")
        big = _entry("b.java", 1, "cross-chunk", text="b" * 200)
        later = _entry("c.java", 1, "cross-chunk", text="s")
        canned.sources["diff"] = [small, big, later]

        unit = _build(max_chars=len(small.rendered()) + len(later.rendered()) + 10)

        assert unit.entries == (small,)
        assert unit.omitted == 2
        assert later.rendered() not in unit.definition_lines
        assert unit.definition_lines == (small.rendered(), _omitted(2))

    def test_the_omitted_line_goes_to_the_contract_block_when_a_contract_is_first_left_out(self, canned):
        definition = _entry("a.java", 1, "cross-chunk", text="d")
        contract = _entry("spec.yaml", 5, "contract", kind="contract", text="c" * 200)
        tail = _entry("z.java", 1, "diff-file", text="d")
        canned.sources["diff"] = [definition, tail]
        canned.sources["contract"] = [contract]

        unit = _build(max_chars=len(definition.rendered()) + 5)

        assert unit.entries == (definition,)
        assert unit.definition_lines == (definition.rendered(),)
        assert unit.contract_lines == (_omitted(2),)

    def test_the_omitted_line_goes_to_the_definition_block_when_a_definition_is_first_left_out(self, canned):
        contract = _entry("spec.yaml", 5, "contract", kind="contract", text="c")
        definition = _entry("z.java", 1, "diff-file", text="d" * 200)
        canned.sources["contract"] = [contract]
        canned.sources["diff"] = [definition]

        unit = _build(max_chars=len(contract.rendered()))

        assert unit.contract_lines == (contract.rendered(),)
        assert unit.definition_lines == (_omitted(1),)

    def test_zero_budget_admits_nothing(self, canned):
        canned.sources["diff"] = [_entry("a.java", 1, "cross-chunk")]
        unit = _build(max_chars=0)
        assert unit.entries == ()
        assert unit.definition_lines == (_omitted(1),)

    def test_nothing_to_admit_is_an_empty_unit(self, canned):
        assert _build() == EMPTY_UNIT

    def test_record_lists_the_admitted_entries_and_the_omitted_count(self, canned):
        a = _entry("a.java", 1, "cross-chunk", text="a")
        b = _entry("b.java", 1, "cross-chunk", text="b" * 50)
        canned.sources["diff"] = [a, b]

        unit = _build(max_chars=len(a.rendered()))

        assert unit.record() == {"entries": [a.record()], "omitted": 1}
        assert EMPTY_UNIT.record() == {"entries": [], "omitted": 0}


class TestOrder:
    def test_reason_rank_beats_path(self, canned):
        canned.sources["diff"] = [_entry("a/x.java", 1, "diff-file"), _entry("z/y.java", 1, "cross-chunk")]
        canned.sources["contract"] = [_entry("y/spec.yaml", 1, "contract", kind="contract")]
        canned.sources["resolver"] = [
            _entry("d/r.java", 1, "shared-state", kind="reader"),
            _entry("a/n.java", 1, "name-search"),
            _entry("b/p.java", 1, "path-convention"),
            _entry("c/i.java", 1, "import"),
        ]

        unit = _build()

        assert [e.reason for e in unit.entries] == list(REASONS)
        assert [e.path for e in unit.entries] == [
            "z/y.java", "y/spec.yaml", "a/x.java", "c/i.java", "b/p.java", "a/n.java", "d/r.java",
        ]

    def test_within_a_reason_path_then_line(self, canned):
        canned.sources["resolver"] = [
            _entry("c/i.java", 9, "import"),
            _entry("c/i.java", 2, "import"),
            _entry("b/i.java", 5, "import"),
        ]
        unit = _build()
        assert [(e.path, e.line) for e in unit.entries] == [("b/i.java", 5), ("c/i.java", 2), ("c/i.java", 9)]

    def test_path_line_dedup_keeps_the_higher_rank(self, canned):
        lower = _entry("x/A.java", 5, "diff-file", text="from the diff")
        higher = _entry("x/A.java", 5, "contract", kind="contract", text="from the contract")
        canned.sources["diff"] = [lower]
        canned.sources["contract"] = [higher]

        unit = _build()

        assert unit.entries == (higher,)
        assert unit.definition_lines == ()

    def test_sources_are_called_in_rank_order(self, canned):
        _build()
        assert canned.called == ["diff", "contract", "resolver"]

    def test_diff_mode_calls_only_the_diff_source(self, canned):
        canned.sources["contract"] = [_entry("spec.yaml", 1, "contract", kind="contract")]
        canned.sources["resolver"] = [_entry("a.java", 1, "import")]
        unit = _build(mode="diff")
        assert canned.called == ["diff"]
        assert unit == EMPTY_UNIT

    def test_repo_without_a_reader_calls_only_the_diff_source(self, canned):
        _build(read=None)
        assert canned.called == ["diff"]

    def test_off_calls_no_source(self, canned):
        canned.sources["diff"] = [_entry("a.java", 1, "cross-chunk")]
        assert _build(mode="off") is EMPTY_UNIT
        assert canned.called == []

    @pytest.mark.parametrize("mode", ["", "on", "Repo", "full", None])
    def test_an_unknown_mode_raises(self, canned, mode):
        with pytest.raises(ValueError, match="mode"):
            _build(mode=mode)

    def test_modes(self):
        assert MODES == ("off", "diff", "repo")
        assert EMPTY_UNIT == UnitContext((), (), (), 0)


SERVICE = "src/main/java/com/acme/app/Service.java"
SERVICE_TEXT = "package com.acme.app;\n\npublic class Service {\n    Widget w = new Widget();\n}\n"
WIDGET = "src/main/java/com/acme/app/Widget.java"
WIDGET_TEXT = "package com.acme.app;\n\npublic class Widget {\n    int size;\n}\n"


def _service_chunk(status: str = "modified") -> SimpleNamespace:
    return _file(SERVICE, (3, [" public class Service {", "+    Widget w = new Widget();", " }"]), status=status)


class TestResolver:
    def test_a_convention_candidate_is_read_and_admitted(self):
        chunk = [_service_chunk()]
        read = _texts({SERVICE: SERVICE_TEXT, WIDGET: WIDGET_TEXT})

        unit = build_unit_context(
            chunk, chunk, mode="repo", read=read, max_chars=MAX_CHARS,
            listing_paths=frozenset({SERVICE, WIDGET}), listing_complete=True,
        )

        assert _keys(unit.entries) == [(WIDGET, 3, "Widget", "definition", "path-convention")]
        assert unit.entries[0].text.startswith("public class Widget {")
        assert read.calls == [SERVICE, WIDGET]

    def test_a_name_the_diff_already_resolved_causes_no_resolver_read(self):
        legacy = "src/main/java/com/acme/legacy/Widget.java"
        other = _file(legacy, (7, [" public class Widget {", "+    int legacySize;", " }"]))
        chunk = [_service_chunk()]
        read = _texts({SERVICE: SERVICE_TEXT, WIDGET: WIDGET_TEXT})

        unit = build_unit_context(
            chunk, [*chunk, other], mode="repo", read=read, max_chars=MAX_CHARS,
            listing_paths=frozenset({SERVICE, WIDGET, legacy}), listing_complete=True,
        )

        assert WIDGET not in read.calls
        assert read.calls == [legacy]
        assert [(e.path, e.reason) for e in unit.entries] == [(legacy, "cross-chunk"), (legacy, "cross-chunk")]

    def test_a_name_the_resolver_found_is_not_searched_again_for_the_next_file(self):
        second = "src/main/java/com/acme/app/Other.java"
        chunk = [
            _service_chunk(),
            _file(second, (3, [" public class Other {", "+    Widget w;", " }"])),
        ]
        read = _texts({SERVICE: SERVICE_TEXT, WIDGET: WIDGET_TEXT})

        unit = build_unit_context(
            chunk, chunk, mode="repo", read=read, max_chars=MAX_CHARS,
            listing_paths=frozenset({SERVICE, WIDGET, second}), listing_complete=True,
        )

        assert read.calls.count(WIDGET) == 1
        assert read.calls == [SERVICE, WIDGET]
        assert [e.path for e in unit.entries] == [WIDGET]

    def test_a_removed_chunk_file_is_not_resolved(self):
        read = _texts({SERVICE: SERVICE_TEXT, WIDGET: WIDGET_TEXT})
        removed = [_service_chunk(status="removed")]
        kept = [_service_chunk()]
        kwargs = dict(
            mode="repo", max_chars=MAX_CHARS, listing_paths=frozenset({SERVICE, WIDGET}), listing_complete=True
        )

        assert build_unit_context(removed, removed, read=read, **kwargs).entries == ()
        assert WIDGET not in read.calls
        assert len(build_unit_context(kept, kept, read=read, **kwargs).entries) == 1

    def test_a_candidate_that_is_a_diff_file_is_left_to_the_diff_source(self):
        widget_diff = _file(WIDGET, (4, ["+    int size;"]))
        chunk = [_service_chunk()]
        read = _texts({SERVICE: SERVICE_TEXT})

        build_unit_context(
            chunk, [*chunk, widget_diff], mode="repo", read=read, max_chars=MAX_CHARS,
            listing_paths=frozenset({SERVICE, WIDGET}), listing_complete=True,
        )

        assert read.calls.count(WIDGET) == 1

    def test_a_path_whose_names_are_all_found_is_not_read(self):
        service = "app/service.py"
        chunk = [_file(service, (1, ["+w = Widget()"]))]
        first, second = "lib/Widget.py", "pkg/widget.py"
        listing = frozenset({service, first, second})
        widget = "class Widget:\n    pass\n"
        hit = _texts({service: "w = Widget()\n", first: widget, second: widget})
        miss = _texts({service: "w = Widget()\n", first: "x = 1\n", second: widget})

        found = build_unit_context(chunk, chunk, mode="repo", read=hit, max_chars=MAX_CHARS, listing_paths=listing)
        control = build_unit_context(chunk, chunk, mode="repo", read=miss, max_chars=MAX_CHARS, listing_paths=listing)

        assert hit.calls == [service, first]
        assert [(e.path, e.reason) for e in found.entries] == [(first, "name-search")]
        assert miss.calls == [service, first, second]
        assert [(e.path, e.reason) for e in control.entries] == [(second, "name-search")]

    def test_an_import_candidate_carries_the_import_reason(self):
        service = "app/service.py"
        chunk = [_file(service, (1, ["+from pkg.models import Widget", "+w = Widget()"]))]
        read = _texts({"pkg/models.py": "class Widget:\n    size = 0\n"})

        unit = build_unit_context(chunk, chunk, mode="repo", read=read, max_chars=MAX_CHARS)

        assert _keys(unit.entries) == [("pkg/models.py", 1, "Widget", "definition", "import")]
        assert read.calls == [service, "pkg/models.py"]


class TestExclusion:
    def test_a_resolver_candidate_at_a_label_file_is_never_read(self, monkeypatch):
        label = "evals/cases.json"
        library = "lib/Widget.java"
        monkeypatch.setattr(
            repo_unit,
            "resolve_candidates",
            lambda path, text, names, **kwargs: [
                Candidate("Widget", label, "import"),
                Candidate("Widget", library, "import"),
            ],
        )
        chunk = [_service_chunk()]
        guarded = _texts({SERVICE: SERVICE_TEXT, library: "public class Widget {\n}\n"})
        unguarded = _texts({SERVICE: SERVICE_TEXT, library: "public class Widget {\n}\n"})

        unit = build_unit_context(
            chunk, chunk, mode="repo", read=guarded, max_chars=MAX_CHARS, exclude=exclude_predicate()
        )
        build_unit_context(chunk, chunk, mode="repo", read=unguarded, max_chars=MAX_CHARS, exclude=None)

        assert label not in guarded.calls
        assert library in guarded.calls
        assert [(e.path, e.reason) for e in unit.entries] == [(library, "import")]
        assert label in unguarded.calls

    def test_a_real_resolver_candidate_under_an_extra_glob_is_never_read(self):
        service = "app/service.py"
        chunk = [_file(service, (1, ["+from vault.config import Settings", "+s = Settings()"]))]
        texts = {
            service: "from vault.config import Settings\ns = Settings()\n",
            "vault/config.py": "class Settings:\n    pass\n",
        }
        guarded, control = _texts(texts), _texts(texts)

        excluded = build_unit_context(
            chunk, chunk, mode="repo", read=guarded, max_chars=MAX_CHARS, exclude=exclude_predicate(["**/vault/**"])
        )
        admitted = build_unit_context(
            chunk, chunk, mode="repo", read=control, max_chars=MAX_CHARS, exclude=exclude_predicate()
        )

        assert not any("vault/" in path for path in guarded.calls)
        assert excluded.entries == ()
        assert "vault/config.py" in control.calls
        assert [e.path for e in admitted.entries] == ["vault/config.py"]

    @pytest.mark.parametrize("with_reader", [True, False], ids=["reader", "no-reader"])
    @pytest.mark.parametrize("mode", ["diff", "repo"])
    def test_an_excluded_diff_files_hunk_entries_are_dropped(self, mode, with_reader):
        generated = "generated/Widget.java"
        other = _file(generated, (3, [" public class Widget {", "+    int size;", " }"]))
        chunk = [_service_chunk()]
        read = _texts({SERVICE: SERVICE_TEXT}) if with_reader else None

        dropped = build_unit_context(
            chunk, [*chunk, other], mode=mode, read=read, max_chars=MAX_CHARS,
            exclude=exclude_predicate(["generated/**"]),
        )
        kept = build_unit_context(
            chunk, [*chunk, other], mode=mode, read=None, max_chars=MAX_CHARS, exclude=exclude_predicate()
        )

        assert dropped.entries == ()
        assert read is None or generated not in read.calls
        assert [e.path for e in kept.entries] == [generated, generated]

    def test_the_guard_refuses_an_excluded_path_without_calling_read(self, monkeypatch):
        seen: dict[str, str | None] = {}

        def diff(chunk, all_files, read):
            for path in (".env", "keys/deploy.pem", "src/A.java"):
                seen[path] = read(path)
            return []

        monkeypatch.setattr(repo_unit, "diff_definitions", diff)
        read = _texts({".env": "SECRET=1", "keys/deploy.pem": "-----", "src/A.java": "class A {}"})

        build_unit_context([], [], mode="diff", read=read, max_chars=MAX_CHARS, exclude=exclude_predicate())

        assert read.calls == ["src/A.java"]
        assert seen == {".env": None, "keys/deploy.pem": None, "src/A.java": "class A {}"}

    def test_an_exclude_that_raises_fails_closed(self):
        run = _run()

        def broken(path: str) -> bool:
            raise RuntimeError("glob engine down")

        read = _Recording(_repo_text)
        unit = _unit(run, CONNECTOR_SERVICE, mode="repo", read=read, exclude=broken)

        assert read.calls == []
        assert unit.entries == ()
        assert unit.omitted == 0


class TestExcludePredicate:
    def test_the_floor_is_exactly_the_decided_set(self):
        assert EXCLUDE_FLOOR == (
            "**/expected.json",
            "**/cases.json",
            "**/case.json",
            "**/prxref-eval/**",
            "**/.env*",
            "**/*.pem",
            "**/*.key",
        )

    FLOOR_SAMPLES = {
        "**/expected.json": "tests/evals/cases/rate-limit/expected.json",
        "**/cases.json": "cases.json",
        "**/case.json": "evals/one/case.json",
        "**/prxref-eval/**": "out/prxref-eval/run-1/summary.json",
        "**/.env*": "deploy/.env.production",
        "**/*.pem": "certs/server.pem",
        "**/*.key": "keys/private.key",
    }

    def test_every_floor_glob_has_a_sample(self):
        assert tuple(self.FLOOR_SAMPLES) == EXCLUDE_FLOOR

    @pytest.mark.parametrize(("glob", "sample"), list(FLOOR_SAMPLES.items()))
    def test_every_floor_glob_excludes_its_sample(self, glob, sample):
        assert match_globs(sample, [glob])
        assert exclude_predicate()(sample) is True
        assert exclude_predicate(["docs/**"])(sample) is True

    @pytest.mark.parametrize("path", ["src/main/java/A.java", "api/openapi/connectors.yaml", "docs/cases.md"])
    def test_ordinary_paths_are_not_excluded(self, path):
        assert exclude_predicate()(path) is False

    def test_a_negation_in_the_extra_globs_cannot_re_admit_a_floor_path(self):
        assert exclude_predicate(["!**/cases.json"])("a/cases.json") is True

    def test_extra_globs_add_to_the_floor(self):
        assert exclude_predicate(["docs/**"])("docs/x.md") is True
        assert exclude_predicate()("docs/x.md") is False

    def test_a_negation_in_the_extra_globs_still_applies_to_the_extra_positives(self):
        predicate = exclude_predicate(["docs/**", "!docs/keep.md"])
        assert predicate("docs/x.md") is True
        assert predicate("docs/keep.md") is False

    def test_an_iterator_of_extra_globs_is_read_once(self):
        predicate = exclude_predicate(iter(["docs/**"]))
        assert predicate("docs/a.md") is True
        assert predicate("docs/b.md") is True


def _oracle(dep_lines, def_lines):
    """``render_context_blocks`` exactly as it was before repository context (the D1 oracle)."""
    blocks: list[str] = []
    if dep_lines:
        blocks.append(DEPENDENCY_HEADER + "\n\n" + "\n".join(dep_lines))
    if def_lines:
        blocks.append(DEFINITIONS_HEADER + "\n\n" + "\n".join(def_lines))
    return "\n\n".join(blocks)


_DEPS = {"none": [], "one": ["effect@4.0.0"], "two": ["a@1.0.0", "b@2.0.0"], "tuple": ("c@3.0.0",)}
_DEFS = {
    "none": [],
    "one": ["a.ts:1: const X = 1;"],
    "capped": ["a.ts:1: x", "b.ts:2: y", "\N{HORIZONTAL ELLIPSIS} 1 more definitions omitted"],
    "tuple": ("m.py:3: def f():",),
}


class TestRenderContextBlocks:
    @pytest.mark.parametrize("deps", list(_DEPS), ids=list(_DEPS))
    @pytest.mark.parametrize("defs", list(_DEFS), ids=list(_DEFS))
    def test_the_defaults_are_byte_identical_to_the_old_rendering(self, deps, defs):
        dep_lines, def_lines = _DEPS[deps], _DEFS[defs]
        expected = _oracle(dep_lines, def_lines)
        assert render_context_blocks(dep_lines, def_lines) == expected
        assert render_context_blocks(dep_lines, def_lines, (), ()) == expected
        assert render_context_blocks(dep_lines, def_lines, extra_def_lines=[], contract_lines=[]) == expected

    def test_the_contract_header(self):
        assert CONTRACT_HEADER == "### Contract excerpts"

    def test_extra_definitions_follow_under_one_header(self):
        out = render_context_blocks(["effect@4.0.0"], ["a.ts:1: x"], extra_def_lines=["B.java:9: record B() {"])
        assert out == (
            DEPENDENCY_HEADER + "\n\neffect@4.0.0\n\n"
            + DEFINITIONS_HEADER + "\n\na.ts:1: x\nB.java:9: record B() {"
        )
        assert out.count(DEFINITIONS_HEADER) == 1

    def test_extra_definitions_alone_bring_the_header(self):
        out = render_context_blocks([], [], extra_def_lines=["B.java:9: record B() {"])
        assert out == DEFINITIONS_HEADER + "\n\nB.java:9: record B() {"

    def test_contracts_come_last(self):
        out = render_context_blocks(
            ["effect@4.0.0"],
            ["a.ts:1: x"],
            extra_def_lines=["B.java:9: y"],
            contract_lines=["s.yaml:3: B:", "s.yaml:9: C:"],
        )
        assert out == (
            DEPENDENCY_HEADER + "\n\neffect@4.0.0\n\n"
            + DEFINITIONS_HEADER + "\n\na.ts:1: x\nB.java:9: y\n\n"
            + CONTRACT_HEADER + "\n\ns.yaml:3: B:\ns.yaml:9: C:"
        )

    def test_contracts_alone(self):
        assert render_context_blocks([], [], contract_lines=["s.yaml:3: B:"]) == CONTRACT_HEADER + "\n\ns.yaml:3: B:"
