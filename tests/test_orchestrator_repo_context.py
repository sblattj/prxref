"""Repository context in the orchestrator (#17): ``orchestrate_review(repo_context=...)``.

The orchestrator takes the level (``off``, ``diff`` or ``repo``), the per-chunk
budget, the contract and exclude globs and an optional ``RepoDir``, and wires
them: one ``RepoReader`` per run, the listing and the contract files once per
run, each chunk's ``build_unit_context`` inside its worker before the first
attempt, the new blocks on the first attempt only, the ``repo_context`` record
on every exit, one WARNING when ``repo`` degrades, and the trace events.

These tests run the REAL reviewer end to end over ``tests/fixtures/issue17``
and capture every prompt with a recording LLM; none uses the ``contract_stubs``
fixture, which replaces the worker template. With ``max_files_per_chunk=1``
the fixture chunks into [ConnectorService], [TransportConfig], [003], in that
order. No test touches the network.
"""
from __future__ import annotations

import inspect
import json
import logging
import threading
from collections import Counter
from pathlib import Path

import pytest

from prxref import config, orchestrator, repo_unit
from prxref.chunk_context import CONTRACT_HEADER, DEFINITIONS_HEADER, render_context_blocks
from prxref.forges.base import PathListing
from prxref.forges.replay import LocalDiffForge
from prxref.forges.repo_dir import RepoDir
from prxref.llm import InvokeResult
from prxref.repo_context import exclude_predicate
from prxref.repo_contracts import literal_contract_paths, select_contract_files
from prxref.repo_reader import repo_dir_reader
from prxref.repo_unit import EMPTY_UNIT, build_unit_context
from prxref.triage import DEFAULT_TOKEN_BUDGET, build_chunks, parse_unified_diff
from tests.test_orchestrator import REF, FakeForge

FIXTURE = Path(__file__).parent / "fixtures" / "issue17"
REPO = FIXTURE / "repo"
DIFF_FILE = FIXTURE / "pr.diff"
DIFF = DIFF_FILE.read_text(encoding="utf-8")
CONNECTOR_SERVICE = "src/main/java/com/acme/connectors/ConnectorService.java"
TRANSPORT_CONFIG = "src/main/java/com/acme/connectors/TransportConfig.java"
MIGRATION = "db/changelog/003-idempotency-unique.sql"
IDEMPOTENCY_TABLE = "db/changelog/002-create-idempotency-keys.sql"
SPEC = "api/openapi/connectors.yaml"
CHUNK_ORDER = [CONNECTOR_SERVICE, TRANSPORT_CONFIG, MIGRATION]
EXCLUSIVITY = "exactly one of url or legacyUrl must be set"
GLOBS = tuple(config._DEFAULTS["context_contract_globs"])
MAX_CHARS = config._DEFAULTS["repo_context_max_chars"]
WARN_NAME = "PRXREF_REPO_CONTEXT"
NO_FINDINGS = '{"findings": []}'
INITIAL_KEYS = {
    "mode", "max_chars", "contract_globs", "exclude_globs", "reader", "listing",
    "reads", "read_cap_hit", "units",
}

PY_HELPER = (
    "diff --git a/tools/sync.py b/tools/sync.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/tools/sync.py\n"
    "@@ -0,0 +1,3 @@\n"
    "+from tools.helpers import load_config\n"
    "+\n"
    "+settings = load_config()\n"
)


class _ReadingForge(FakeForge):
    """FakeForge over a diff, with ``get_file_content`` reading a tree and counting calls per path."""

    def __init__(self, diff: str = DIFF, root: Path = REPO):
        super().__init__(diff=diff)
        self.root = Path(root)
        self.content_calls: Counter[str] = Counter()
        self.get_pr_calls = 0
        self._lock = threading.Lock()

    def get_pr(self, ref):
        self.get_pr_calls += 1
        return super().get_pr(ref)

    def get_file_content(self, ref, path, *, sha):
        with self._lock:
            self.content_calls[path] += 1
        target = self.root / path
        return target.read_text(encoding="utf-8") if target.is_file() else None


class _RepoForge(_ReadingForge):
    """``_ReadingForge`` plus ``list_paths`` over the same tree, counting calls."""

    def __init__(self, diff: str = DIFF, root: Path = REPO):
        super().__init__(diff, root)
        self.list_calls = 0

    def list_paths(self, ref, *, sha):
        with self._lock:
            self.list_calls += 1
        paths, complete = RepoDir(self.root).list_files()
        return PathListing(paths=paths, complete=complete)


class _RecordingLLM:
    """Records every ``(system, user)`` prompt; the first worker prompt of ``timeout_path`` times out."""

    def __init__(self, *, timeout_path: str | None = None):
        self.calls: list[tuple[str, str]] = []
        self.timeout_path = timeout_path
        self._timed_out = False
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        with self._lock:
            self.calls.append((system, user))
            fire = (
                self.timeout_path is not None
                and not self._timed_out
                and f"diff --git a/{self.timeout_path} " in user
            )
            if fire:
                self._timed_out = True
        if fire:
            raise TimeoutError("request timeout after 60s")
        return InvokeResult(
            text=NO_FINDINGS, input_tokens=10, output_tokens=5,
            model="fake-model", backend="fake", elapsed_ms=1,
        )


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    monkeypatch.setattr(orchestrator, "_elapsed_ms", lambda t0: 0)


def _review(forge, llm: _RecordingLLM | None = None, *, ref=REF, **kwargs):
    llm = llm if llm is not None else _RecordingLLM()
    kwargs.setdefault("max_files_per_chunk", 1)
    res = orchestrator.orchestrate_review(forge, ref, llm, post=False, **kwargs)
    return res, llm


def _worker_prompts(llm: _RecordingLLM) -> dict[str, list[str]]:
    """Every worker prompt (the sweep, which runs last, excluded), grouped by the chunk's file, in call order."""
    *workers, _sweep = llm.calls
    grouped: dict[str, list[str]] = {path: [] for path in CHUNK_ORDER}
    for _system, user in workers:
        owners = [path for path in CHUNK_ORDER if f"diff --git a/{path} " in user]
        assert len(owners) == 1, owners
        grouped[owners[0]].append(user)
    return grouped


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _stable(events: list[dict]) -> list[tuple]:
    """Every event but its wall-clock fields."""
    return [
        (e["node"], e["phase"], {k: v for k, v in (e.get("meta") or {}).items() if k != "elapsed_ms"})
        for e in events
    ]


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING and WARN_NAME in r.getMessage()]


def _keys(row: dict) -> list[tuple[str, int, str]]:
    return [(e["path"], e["line"], e["reason"]) for e in row["entries"]]


def _expected_rows(
    mode: str, *, read: bool = True, listing: bool = True, globs=GLOBS, diff: str = DIFF,
    root: Path = REPO, max_files_per_chunk: int = 1, token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> list[dict]:
    """The units rows ``build_unit_context`` gives each chunk when called directly with a fresh reader."""
    files = parse_unified_diff(diff)
    chunks = build_chunks(files, token_budget=token_budget, max_files_per_chunk=max_files_per_chunk)
    exclude = exclude_predicate(())
    reader = repo_dir_reader(RepoDir(root), exclude=exclude) if read else None
    got = reader.listing() if reader is not None and listing and mode == "repo" else None
    contract_paths: list[str] = []
    priority: list[str] = []
    if mode == "repo" and reader is not None:
        contract_paths = select_contract_files(
            list(globs), listing=got.paths if got is not None else None,
            diff_paths=[f.path for f in files if f.status != "removed"],
        )
        priority = literal_contract_paths(list(globs))
    rows = []
    for chunk in chunks:
        unit = build_unit_context(
            chunk, files, mode=mode, read=reader.chunk_reader() if reader is not None else None,
            max_chars=MAX_CHARS,
            listing_paths=frozenset(got.paths) if got is not None else None,
            listing_complete=got.complete if got is not None else False,
            contract_paths=contract_paths, contract_priority=priority, exclude=exclude,
        )
        rows.append({**unit.record(), "retry_dropped": False})
    return rows


REPO_CONNECTOR_KEYS = [
    (TRANSPORT_CONFIG, 9, "cross-chunk"),
    (TRANSPORT_CONFIG, 11, "cross-chunk"),
    (SPEC, 6, "contract"),
    (SPEC, 31, "contract"),
    (SPEC, 41, "contract"),
]


class TestTheParameters:
    NEW = ["repo_context", "repo_context_max_chars", "context_contract_globs", "context_exclude_globs", "repo_dir"]

    def test_the_five_kwargs_follow_max_findings_per_rule_keyword_only(self):
        params = inspect.signature(orchestrator.orchestrate_review).parameters
        names = list(params)
        start = names.index("max_findings_per_rule") + 1
        assert names[start:start + 5] == self.NEW
        for name in self.NEW:
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name

    def test_the_defaults_are_off_and_restate_config(self):
        params = inspect.signature(orchestrator.orchestrate_review).parameters
        assert params["repo_context"].default == "off" == config._DEFAULTS["repo_context"]
        assert params["repo_context_max_chars"].default == 12000 == MAX_CHARS
        assert params["context_contract_globs"].default == ()
        assert params["context_exclude_globs"].default == ()
        assert params["repo_dir"].default is None

    @pytest.mark.parametrize("mode", ["bogus", "", "OFF", None])
    def test_an_unknown_level_raises_before_any_forge_call(self, mode):
        forge = _RepoForge()
        with pytest.raises(ValueError, match="repo_context"):
            _review(forge, repo_context=mode)
        assert forge.get_pr_calls == 0
        assert forge.list_calls == 0
        assert not forge.content_calls


class TestOffIsByteIdentical:
    """D1: ``off`` with every other new kwarg set is exactly a run without them."""

    def _run(self, tmp_path, caplog, name, **kwargs):
        forge = _RepoForge(DIFF + PY_HELPER)
        trace = tmp_path / f"{name}.jsonl"
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="prxref"):
            res, llm = _review(
                forge, _RecordingLLM(timeout_path=CONNECTOR_SERVICE),
                max_files_per_chunk=4, max_workers=1, trace_file=str(trace), **kwargs,
            )
        logs = [
            (r.name, r.levelname, r.getMessage()) for r in caplog.records
            if r.levelno >= logging.INFO
        ]
        return res, llm.calls, _stable(_events(trace)), logs, forge

    def test_off_with_every_kwarg_matches_a_run_without_them(self, tmp_path, caplog):
        base = self._run(tmp_path, caplog, "base")
        off = self._run(
            tmp_path, caplog, "off",
            repo_context="off", repo_context_max_chars=5, context_contract_globs=GLOBS,
            context_exclude_globs=("**/*.yaml",), repo_dir=RepoDir(REPO),
        )
        assert off[:4] == base[:4]
        assert off[4].content_calls == base[4].content_calls
        assert base[4].content_calls, "the old reader read nothing, so the call-count check is vacuous"
        assert off[4].list_calls == base[4].list_calls == 0
        assert base[0]["repo_context"] is None and off[0]["repo_context"] is None
        assert ("chunk", "retry") in [(n, p) for n, p, _ in base[2]]
        for node, phase, _meta in off[2]:
            assert (node, phase) != ("chunk", "context")
            assert node != "repo_context"

    def test_the_oracle_sees_a_repo_run_differ(self, tmp_path, caplog):
        """The control: the same oracle over ``repo`` must come out different on every axis it checks."""
        base = self._run(tmp_path, caplog, "base")
        on = self._run(tmp_path, caplog, "on", repo_context="repo", context_contract_globs=GLOBS)
        assert on[1] != base[1]
        assert on[2] != base[2]
        assert on[4].list_calls == 1
        assert on[4].content_calls != base[4].content_calls
        assert on[0]["repo_context"] is not None


class TestDiffLevel:
    def test_the_connector_chunk_gets_the_cross_chunk_definitions_and_no_contracts(self):
        forge = _RepoForge()
        res, llm = _review(forge, repo_context="diff", context_contract_globs=GLOBS, max_workers=1)
        prompts = _worker_prompts(llm)
        (connector,) = prompts[CONNECTOR_SERVICE]
        block = connector[connector.index(DEFINITIONS_HEADER):]
        assert f"{TRANSPORT_CONFIG}:9: " in block
        assert f"{TRANSPORT_CONFIG}:11: " in block
        assert EXCLUSIVITY in block
        assert all(CONTRACT_HEADER not in p for group in prompts.values() for p in group)
        assert forge.list_calls == 0
        record = res["repo_context"]
        assert (record["mode"], record["reader"], record["listing"]) == ("diff", "forge", None)
        assert record["reads"] == 2
        assert record["read_cap_hit"] is False
        assert record["units"]["chunks"] == _expected_rows("diff")
        assert _keys(record["units"]["chunks"][0]) == REPO_CONNECTOR_KEYS[:2]

    def test_an_off_run_has_no_definitions_block_on_that_chunk(self):
        """The control for the test above: the block is the level's doing, not the fixture's.

        The exclusivity text itself is in every run's "Other files changed" digest, so the
        control looks for the definition's ``path:line:`` rows instead.
        """
        _, llm = _review(_RepoForge(), max_workers=1)
        (connector,) = _worker_prompts(llm)[CONNECTOR_SERVICE]
        assert DEFINITIONS_HEADER not in connector
        assert f"{TRANSPORT_CONFIG}:9: " not in connector
        assert f"{TRANSPORT_CONFIG}:11: " not in connector

    def test_diff_with_no_reader_builds_hunk_entries_and_logs_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref"):
            res, llm = _review(FakeForge(diff=DIFF), repo_context="diff", max_workers=1)
        assert _warnings(caplog) == []
        (connector,) = _worker_prompts(llm)[CONNECTOR_SERVICE]
        assert EXCLUSIVITY in connector[connector.index(DEFINITIONS_HEADER):]
        record = res["repo_context"]
        assert (record["reader"], record["listing"], record["reads"]) == (None, None, 0)
        assert record["units"]["chunks"] == _expected_rows("diff", read=False)


class TestRepoLevel:
    def test_the_connector_chunk_gets_its_contract_excerpts(self, caplog):
        forge = _RepoForge()
        with caplog.at_level(logging.WARNING, logger="prxref"):
            res, llm = _review(forge, repo_context="repo", context_contract_globs=GLOBS, max_workers=1)
        assert _warnings(caplog) == []
        (connector,) = _worker_prompts(llm)[CONNECTOR_SERVICE]
        contracts = connector[connector.index(CONTRACT_HEADER):]
        for line in (6, 31, 41):
            assert f"{SPEC}:{line}: " in contracts
        assert "mutually exclusive" in contracts
        assert forge.list_calls == 1
        record = res["repo_context"]
        assert set(record) == INITIAL_KEYS
        assert record["mode"] == "repo" and record["reader"] == "forge"
        assert record["max_chars"] == MAX_CHARS
        assert record["contract_globs"] == list(GLOBS) and record["exclude_globs"] == []
        assert record["listing"] == {"paths": 6, "complete": True}
        assert record["reads"] > 0
        assert record["read_cap_hit"] is False
        rows = record["units"]["chunks"]
        assert rows == _expected_rows("repo")
        assert [_keys(row) for row in rows] == [
            REPO_CONNECTOR_KEYS,
            [(SPEC, 31, "contract")],
            [(SPEC, 50, "contract"), (IDEMPOTENCY_TABLE, 4, "contract")],
        ]

    def test_empty_contract_globs_mean_no_contract_files(self):
        res, llm = _review(_RepoForge(), repo_context="repo", max_workers=1)
        assert all(CONTRACT_HEADER not in p for group in _worker_prompts(llm).values() for p in group)
        assert _keys(res["repo_context"]["units"]["chunks"][0]) == REPO_CONNECTOR_KEYS[:2]

    def test_an_excluded_spec_is_never_read(self):
        forge = _RepoForge()
        res, llm = _review(
            forge, repo_context="repo", context_contract_globs=GLOBS,
            context_exclude_globs=["api/**"], max_workers=1,
        )
        assert forge.content_calls[SPEC] == 0
        assert res["repo_context"]["exclude_globs"] == ["api/**"]
        assert res["repo_context"]["listing"] == {"paths": 5, "complete": True}
        assert all(SPEC not in p for group in _worker_prompts(llm).values() for p in group)

    def test_repo_dir_on_a_local_diff_forge(self, caplog):
        """No ``get_file_content`` and no head sha: the old blocks have no reader, and the new ones still render."""
        forge = LocalDiffForge(DIFF, path=str(DIFF_FILE))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            res, llm = _review(
                forge, ref=LocalDiffForge.ref_for(str(DIFF_FILE)), repo_context="repo",
                context_contract_globs=GLOBS, repo_dir=RepoDir(REPO), max_workers=1,
            )
        assert _warnings(caplog) == []
        record = res["repo_context"]
        assert record["reader"] == "repo-dir"
        assert record["listing"] == {"paths": 6, "complete": True}
        assert record["units"]["chunks"] == _expected_rows("repo")
        (connector,) = _worker_prompts(llm)[CONNECTOR_SERVICE]
        assert f"{SPEC}:6: " in connector[connector.index(CONTRACT_HEADER):]
        assert EXCLUSIVITY in connector[connector.index(DEFINITIONS_HEADER):]

    def test_repo_with_no_reader_warns_once_and_keeps_the_hunk_entries(self, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref"):
            res, llm = _review(
                FakeForge(diff=DIFF), repo_context="repo", context_contract_globs=GLOBS, max_workers=4,
            )
        (warning,) = _warnings(caplog)
        assert "hunk lines" in warning.getMessage()
        record = res["repo_context"]
        assert (record["reader"], record["listing"], record["reads"], record["read_cap_hit"]) == (
            None, None, 0, False,
        )
        rows = record["units"]["chunks"]
        assert rows == _expected_rows("repo", read=False)
        assert _keys(rows[0]) == REPO_CONNECTOR_KEYS[:2]
        (connector,) = _worker_prompts(llm)[CONNECTOR_SERVICE]
        assert f"{TRANSPORT_CONFIG}:9: " in connector[connector.index(DEFINITIONS_HEADER):]
        assert CONTRACT_HEADER not in connector

    def test_repo_with_a_reader_but_no_listing_warns_once(self, caplog):
        forge = _ReadingForge()
        assert not hasattr(forge, "list_paths")
        with caplog.at_level(logging.WARNING, logger="prxref"):
            res, _ = _review(forge, repo_context="repo", context_contract_globs=GLOBS, max_workers=4)
        (warning,) = _warnings(caplog)
        assert "listing" in warning.getMessage()
        record = res["repo_context"]
        assert (record["reader"], record["listing"]) == ("forge", None)
        assert record["units"]["chunks"] == _expected_rows("repo", listing=False)


class TestTheTimeoutRetry:
    def test_the_retry_carries_neither_new_block(self, tmp_path):
        trace = tmp_path / "trace.jsonl"
        res, llm = _review(
            _RepoForge(), _RecordingLLM(timeout_path=CONNECTOR_SERVICE), repo_context="repo",
            context_contract_globs=GLOBS, max_workers=1, trace_file=str(trace),
        )
        first, retry = _worker_prompts(llm)[CONNECTOR_SERVICE]
        assert CONTRACT_HEADER in first and DEFINITIONS_HEADER in first
        assert CONTRACT_HEADER not in retry
        assert DEFINITIONS_HEADER not in retry
        assert f"{SPEC}:" not in retry and f"{TRANSPORT_CONFIG}:9: " not in retry
        assert res["chunks_failed"] == 0
        assert [(n, p) for n, p, _ in _stable(_events(trace))].count(("chunk", "retry")) == 1
        rows = res["repo_context"]["units"]["chunks"]
        assert [row["retry_dropped"] for row in rows] == [True, False, False]
        expected = _expected_rows("repo")
        expected[0]["retry_dropped"] = True
        assert rows == expected


class TestABuildThatRaises:
    def test_the_chunk_gets_the_empty_unit_and_one_warning(self, monkeypatch, caplog):
        real = repo_unit.build_unit_context

        def flaky(chunk, all_files, **kwargs):
            if any(f.path == CONNECTOR_SERVICE for f in chunk):
                raise RuntimeError("resolver exploded")
            return real(chunk, all_files, **kwargs)

        monkeypatch.setattr(orchestrator.repo_unit, "build_unit_context", flaky)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            res, llm = _review(_RepoForge(), repo_context="repo", context_contract_globs=GLOBS, max_workers=1)
        failures = [r for r in caplog.records if "repository context failed" in r.getMessage()]
        assert len(failures) == 1
        assert "[chunk 1/3]" in failures[0].getMessage()
        assert failures[0].levelno == logging.WARNING
        assert res["chunks_failed"] == 0
        assert res["verdict"] == "Approved"
        rows = res["repo_context"]["units"]["chunks"]
        assert rows[0] == {**EMPTY_UNIT.record(), "retry_dropped": False}
        assert rows[1:] == _expected_rows("repo")[1:]
        (connector,) = _worker_prompts(llm)[CONNECTOR_SERVICE]
        assert CONTRACT_HEADER not in connector


class TestEarlyExits:
    def _initial(self, mode: str) -> dict:
        return {
            "mode": mode, "max_chars": MAX_CHARS, "contract_globs": list(GLOBS), "exclude_globs": [],
            "reader": None, "listing": None, "reads": 0, "read_cap_hit": False, "units": None,
        }

    @pytest.mark.parametrize("mode", ["diff", "repo"])
    def test_an_empty_diff_carries_the_initial_record(self, mode):
        forge = _RepoForge("")
        res, _ = _review(forge, repo_context=mode, context_contract_globs=GLOBS)
        assert res["repo_context"] == self._initial(mode)
        assert forge.list_calls == 0 and not forge.content_calls

    def test_an_empty_diff_off_is_none(self):
        res, _ = _review(_RepoForge(""))
        assert res["repo_context"] is None

    def test_a_get_pr_failure_carries_the_initial_record(self):
        forge = _RepoForge()
        forge.fail.add("get_pr")
        res, _ = _review(forge, repo_context="repo", context_contract_globs=GLOBS)
        assert res["verdict"] == "Error"
        assert res["repo_context"] == self._initial("repo")

    def test_a_total_failure_carries_the_filled_record(self):
        class _DeadLLM(_RecordingLLM):
            def invoke(self, system, user, **kwargs):
                raise RuntimeError("no model")

        res, _ = _review(_RepoForge(), _DeadLLM(), repo_context="repo", context_contract_globs=GLOBS)
        assert res["verdict"] == "Error"
        assert res["repo_context"]["reader"] == "forge"
        assert res["repo_context"]["units"]["chunks"] == _expected_rows("repo")


class TestDeterminism:
    def test_one_and_four_workers_give_equal_records(self):
        forge = _RepoForge()
        records = [
            _review(forge, repo_context="repo", context_contract_globs=GLOBS, max_workers=workers)[0]["repo_context"]
            for workers in (1, 4, 4)
        ]
        assert records[0]["units"]["chunks"] == _expected_rows("repo")
        assert records[1] == records[0]
        assert records[2] == records[0]


class TestTheTrace:
    def test_one_context_event_per_chunk_and_one_run_event(self, tmp_path):
        trace = tmp_path / "trace.jsonl"
        res, _ = _review(
            _RepoForge(), repo_context="repo", context_contract_globs=GLOBS, max_workers=4,
            trace_file=str(trace),
        )
        events = _events(trace)
        record = res["repo_context"]
        rows = record["units"]["chunks"]
        context = sorted(
            (e["meta"] for e in events if (e["node"], e["phase"]) == ("chunk", "context")),
            key=lambda meta: meta["index"],
        )
        assert context == [
            {
                "index": i, "total": 3, "entries": len(row["entries"]), "omitted": row["omitted"],
                "chars": sum(entry["chars"] for entry in row["entries"]),
            }
            for i, row in enumerate(rows, start=1)
        ]
        (run,) = [e for e in events if e["node"] == "repo_context"]
        assert run["phase"] == "ok"
        assert run["meta"] == {key: record[key] for key in ("mode", "reader", "listing", "reads", "read_cap_hit")}
        sweep_start = next(
            i for i, e in enumerate(events) if (e["node"], e["phase"]) == ("sweep", "start")
        )
        assert events.index(run) < sweep_start


def _java_type(name: str) -> str:
    return f"package com.acme.widgets;\n\npublic class {name} {{\n    private final int size = 0;\n}}\n"


def _new_file_diff(path: str, text: str) -> str:
    lines = text.splitlines()
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


WIDGET_SPEC = (
    "openapi: 3.0.3\n"
    "info:\n"
    "  title: Widgets\n"
    '  version: "1"\n'
    "paths:\n"
    "  /widgets:\n"
    "    post:\n"
    "      operationId: createWidget\n"
    "      responses:\n"
    "        '201':\n"
    "          description: The created widget.\n"
)


class TestDiffFileReadsAreUncapped:
    """D-L: a chunk's diff-file reads go to the shared read, so its capped reads are left for everything else."""

    PARTS = [f"Part{i:02d}" for i in range(1, 21)]
    CONTROLLER = "src/main/java/com/acme/widgets/WidgetController.java"

    def _tree(self, root: Path) -> str:
        texts = {f"src/main/java/com/acme/widgets/{name}.java": _java_type(name) for name in self.PARTS}
        texts[self.CONTROLLER] = (
            "package com.acme.widgets;\n\n"
            "public class WidgetController {\n\n"
            '    @PostMapping("/widgets")\n'
            f"    public Widget create({', '.join(f'{n} p{i}' for i, n in enumerate(self.PARTS))}) {{\n"
            "        return null;\n"
            "    }\n"
            "}\n"
        )
        for path, text in {**texts, "api/openapi.yaml": WIDGET_SPEC}.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return "".join(_new_file_diff(path, text) for path, text in texts.items())

    def test_a_chunk_of_many_diff_files_still_gets_its_contract(self, tmp_path):
        root = tmp_path / "repo"
        diff = self._tree(root)
        shape = {"max_files_per_chunk": 30, "token_budget": 200_000}
        assert len(build_chunks(parse_unified_diff(diff), **shape)) == 1
        capped = _expected_rows("repo", diff=diff, root=root, **shape)
        assert all(e["kind"] != "contract" for e in capped[0]["entries"]), (
            "the capped reader was not starved, so this test cannot see the routing"
        )
        res, llm = _review(
            FakeForge(diff=diff), repo_context="repo", context_contract_globs=GLOBS,
            repo_dir=RepoDir(root), **shape,
        )
        record = res["repo_context"]
        (row,) = record["units"]["chunks"]
        assert [(e["path"], e["line"], e["kind"]) for e in row["entries"]] == [("api/openapi.yaml", 6, "contract")]
        assert record["read_cap_hit"] is False
        worker = llm.calls[0][1]
        assert "api/openapi.yaml:6: " in worker[worker.index(CONTRACT_HEADER):]


class TestContextBlocks:
    """``_context_blocks`` with a unit and no old reader, and with no unit, called directly."""

    def _chunk(self):
        return parse_unified_diff(DIFF)[:1]

    def test_no_reader_and_no_unit_is_empty(self):
        assert orchestrator._context_blocks(self._chunk(), None, include_definitions=True) == ""
        assert orchestrator._context_blocks(self._chunk(), None, include_definitions=True, unit=EMPTY_UNIT) == ""

    def test_a_unit_renders_without_the_old_reader(self):
        unit = repo_unit.UnitContext(("a.java:1: class A {}",), ("spec.yaml:2: /a:",), (), 0)
        out = orchestrator._context_blocks(self._chunk(), None, include_definitions=True, unit=unit)
        assert out == render_context_blocks([], [], extra_def_lines=unit.definition_lines,
                                            contract_lines=unit.contract_lines)
        assert out.startswith(DEFINITIONS_HEADER) and CONTRACT_HEADER in out
