"""Issue #17 acceptance: repository context through the real ``orchestrate_review``, end to end.

Every test drives ``orchestrate_review`` over ``tests/fixtures/issue17`` through
the version-neutral harness in ``tests/fixtures/issue17/harness.py``: a
read-and-list fake forge that records every ``get_file_content`` and
``list_paths`` call, and an LLM that captures every worker and sweep prompt and
answers with no findings. The reviewer is the REAL one (no ``contract_stubs``),
``post`` is false and ``max_files_per_chunk`` is 1, which chunks the fixture into
[ConnectorService], [TransportConfig], [003] in that order. Each test asserts on
what a user sees: the prompts, the run record and the trace.

The D1 golden, ``golden_off_prompts.json``, was captured from the RELEASED
prxref 0.15.0 by ``make_golden.py`` (its docstring has the command); it is
never regenerated from the tree. No test touches the network.

The exclusivity message is in every ConnectorService prompt, the feature off
included, because the "Other files changed in this PR" digest quotes
TransportConfig's hunk. So the definitions evidence is keyed on the
``TransportConfig.java:11: `` row inside the definitions block, never on the
bare string.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest

from prxref import config, orchestrator
from prxref.chunk_context import CONTRACT_HEADER, DEFINITIONS_HEADER, DEPENDENCY_HEADER
from prxref.forges.replay import LocalDiffForge
from prxref.forges.repo_dir import RepoDir
from prxref.repo_reader import MAX_CHUNK_READS
from prxref.triage import build_chunks, parse_unified_diff
from tests.fixtures.issue17.harness import (
    DESIGN_POINTS,
    DIFF,
    FIXTURE,
    HEAD_SHA,
    REPO,
    FixtureForge,
    golden_run,
    review,
)

DIFF_FILE = FIXTURE / "pr.diff"
GOLDEN = json.loads((FIXTURE / "golden_off_prompts.json").read_text(encoding="utf-8"))
CONNECTOR_SERVICE = "src/main/java/com/acme/connectors/ConnectorService.java"
TRANSPORT_CONFIG = "src/main/java/com/acme/connectors/TransportConfig.java"
MIGRATION = "db/changelog/003-idempotency-unique.sql"
FIRST_MIGRATION = "db/changelog/001-create-connectors.sql"
IDEMPOTENCY_TABLE = "db/changelog/002-create-idempotency-keys.sql"
SPEC = "api/openapi/connectors.yaml"
CHUNK_ORDER = [CONNECTOR_SERVICE, TRANSPORT_CONFIG, MIGRATION]
GLOBS = tuple(config._DEFAULTS["context_contract_globs"])
EXCLUSIVITY = "exactly one of url or legacyUrl must be set"
OUTPUT_HEADER = "\n\n## Output Format"
WARN_NAME = "PRXREF_REPO_CONTEXT"
ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"

TC_9 = (TRANSPORT_CONFIG, 9, "cross-chunk", "definition", "TransportConfig", 275)
TC_11 = (TRANSPORT_CONFIG, 11, "cross-chunk", "definition", "TransportConfig", 342)
SPEC_6 = (SPEC, 6, "contract", "contract", "/connectors/{connectorId}/transports", 664)
SPEC_31 = (SPEC, 31, "contract", "contract", "TransportConfig", 250)
SPEC_41 = (SPEC, 41, "contract", "contract", "CreateTransportRequest", 185)
SPEC_50 = (SPEC, 50, "contract", "contract", "IdempotencyKey", 369)
TABLE_4 = (IDEMPOTENCY_TABLE, 4, "contract", "contract", "idempotency_keys", 248)
REPO_ROWS = [[TC_9, TC_11, SPEC_6, SPEC_31, SPEC_41], [SPEC_31], [SPEC_50, TABLE_4]]
HUNK_ROWS = [[TC_9, TC_11], [], []]
REPO_READS = [TRANSPORT_CONFIG, CONNECTOR_SERVICE, SPEC, FIRST_MIGRATION, IDEMPOTENCY_TABLE]


def _rows(result: dict) -> list[list[tuple]]:
    """Each chunk's entries as ``(path, line, reason, kind, symbol, chars)``, in chunk order."""
    return [
        [(e["path"], e["line"], e["reason"], e["kind"], e["symbol"], e["chars"]) for e in row["entries"]]
        for row in result["repo_context"]["units"]["chunks"]
    ]


def _worker_prompts(llm) -> dict[str, str]:
    """Each chunk's one worker prompt, keyed by the chunk's file; the sweep, which runs last, is left out."""
    *workers, _sweep = llm.calls
    prompts: dict[str, str] = {}
    for _system, user in workers:
        (owner,) = [path for path in CHUNK_ORDER if f"diff --git a/{path} " in user]
        assert owner not in prompts, f"{owner} was prompted twice"
        prompts[owner] = user
    assert set(prompts) == set(CHUNK_ORDER)
    return prompts


def _block(prompt: str, header: str) -> str:
    """The prompt's ``header`` block, header included, up to the next block or the output section; "" if absent."""
    if header not in prompt:
        return ""
    start = prompt.index(header)
    ends = [
        end for end in (prompt.find("\n\n### ", start + len(header)), prompt.find(OUTPUT_HEADER, start))
        if end >= 0
    ]
    return prompt[start:min(ends)]


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _named_warnings(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING and WARN_NAME in r.getMessage()
    ]


def _assert_connector_evidence(prompt: str) -> None:
    """L2's evidence: TransportConfig's exclusivity check and the spec's path item and mutually exclusive schema."""
    definitions = _block(prompt, DEFINITIONS_HEADER)
    assert f"{TRANSPORT_CONFIG}:9: public record TransportConfig(String url, String legacyUrl) {{" in definitions
    assert EXCLUSIVITY in definitions[definitions.index(f"{TRANSPORT_CONFIG}:11: "):]
    contracts = _block(prompt, CONTRACT_HEADER)
    assert f"{SPEC}:6: /connectors/{{connectorId}}/transports:" in contracts
    assert "operationId: createTransport" in contracts
    assert f"{SPEC}:31: TransportConfig:" in contracts
    assert "url and legacyUrl are mutually exclusive" in contracts
    assert f"{SPEC}:41: CreateTransportRequest:" in contracts


def _assert_migration_evidence(prompt: str) -> None:
    """L1's evidence: 002's table with connector_id and the spec's IdempotencyKey uniqueness rule."""
    assert DEFINITIONS_HEADER not in prompt
    contracts = _block(prompt, CONTRACT_HEADER)
    assert f"{IDEMPOTENCY_TABLE}:4: CREATE TABLE idempotency_keys (" in contracts
    assert "connector_id VARCHAR(36) NOT NULL" in contracts
    assert f"{SPEC}:50: IdempotencyKey:" in contracts
    assert "unique per (tenant, connector, key)" in contracts


EVIDENCE = (
    DEFINITIONS_HEADER,
    CONTRACT_HEADER,
    f"{TRANSPORT_CONFIG}:11: ",
    f"{SPEC}:",
    "mutually exclusive",
    f"{IDEMPOTENCY_TABLE}:4: ",
    "connector_id VARCHAR(36)",
    "unique per (tenant, connector, key)",
)


class TestTheEvidenceReachesTheModel:
    """Check 1: at ``repo``, the worker prompts carry the evidence for both labels in cases.json (L1, L2).

    A deterministic test can prove only that the evidence reaches the model,
    not that the model uses it.
    """

    def test_both_labeled_chunks_carry_their_evidence(self):
        _, _, llm = review(repo_context="repo", context_contract_globs=GLOBS, max_workers=1)
        prompts = _worker_prompts(llm)
        _assert_connector_evidence(prompts[CONNECTOR_SERVICE])
        _assert_migration_evidence(prompts[MIGRATION])

    def test_control_an_off_run_carries_none_of_it(self):
        """The evidence is the level's doing: none of it is in any prompt of an ``off`` run."""
        _, _, llm = review(max_workers=1)
        assert len(llm.calls) == 4
        for system, user in llm.calls:
            for needle in EVIDENCE:
                assert needle not in system and needle not in user, needle


class TestTheRecordAndTheTraceCarryTheReasons:
    """Check 2: the run record's units rows and the trace say what was added and why."""

    def test_rows_reads_and_trace_events(self, tmp_path):
        trace = tmp_path / "trace.jsonl"
        result, forge, _ = review(
            repo_context="repo", context_contract_globs=GLOBS, max_workers=1, trace_file=str(trace),
        )
        record = result["repo_context"]
        assert {key: value for key, value in record.items() if key != "units"} == {
            "mode": "repo", "max_chars": 12000, "contract_globs": list(GLOBS), "exclude_globs": [],
            "reader": "forge", "listing": {"paths": 6, "complete": True}, "reads": 5, "read_cap_hit": False,
        }
        assert _rows(result) == REPO_ROWS
        chunks = record["units"]["chunks"]
        assert [(row["omitted"], row["retry_dropped"]) for row in chunks] == [(0, False)] * 3
        assert {e["reason"] for row in chunks for e in row["entries"]} == {"cross-chunk", "contract"}
        assert forge.content_calls == REPO_READS
        assert forge.content_shas == {HEAD_SHA}
        assert forge.list_calls == 1

        events = _events(trace)
        steps = [(e["node"], e["phase"]) for e in events]
        assert [e["meta"] for e in events if (e["node"], e["phase"]) == ("chunk", "context")] == [
            {"index": 1, "total": 3, "entries": 5, "omitted": 0, "chars": 1716},
            {"index": 2, "total": 3, "entries": 1, "omitted": 0, "chars": 250},
            {"index": 3, "total": 3, "entries": 2, "omitted": 0, "chars": 617},
        ]
        assert steps.count(("repo_context", "ok")) == 1
        assert [e for e in events if e["node"] == "repo_context"][0]["meta"] == {
            "mode": "repo", "reader": "forge", "listing": {"paths": 6, "complete": True},
            "reads": 5, "read_cap_hit": False,
        }
        last_context = max(i for i, step in enumerate(steps) if step == ("chunk", "context"))
        assert last_context < steps.index(("repo_context", "ok")) < steps.index(("sweep", "start"))


def _off_variant(name: str) -> dict:
    return {
        "no-new-kwargs": {},
        "off": {"repo_context": "off"},
        "off-with-every-other-kwarg-set": {
            "repo_context": "off", "repo_context_max_chars": 5, "context_contract_globs": GLOBS,
            "context_exclude_globs": ("**/*.yaml",), "repo_dir": RepoDir(REPO),
        },
    }[name]


def _first_difference(want: dict, got: dict) -> str:
    for half in ("system", "user"):
        if want[half] == got[half]:
            continue
        old, new = want[half].splitlines(), got[half].splitlines()
        for number, (a, b) in enumerate(zip(old, new, strict=False), start=1):
            if a != b:
                return f"{half} line {number}: 0.15.0 {a!r} vs tip {b!r}"
        return f"{half}: 0.15.0 has {len(old)} lines, tip {len(new)}"
    return "equal"


class TestOffMatchesTheReleased015:
    """Check 3 (decision D1): ``off`` at the tip is byte for byte what the RELEASED 0.15.0 sends.

    The golden came from ``make_golden.py`` run under ``uv run --no-project
    --with prxref==0.15.0`` from outside the repository, so the prompts are
    0.15.0's own, not the tree's. It holds two design points: the fixture, and
    the fixture plus a Python file whose chunk makes the pre-0.16 reader render
    its dependency and definitions blocks, so the rendering
    ``render_context_blocks`` extended is compared too.
    """

    def test_the_golden_is_a_0_15_0_oracle_that_exercises_the_old_blocks(self):
        assert GOLDEN["prxref_version"] == "0.15.0"
        assert GOLDEN["generated_from"] == "installed distribution"
        assert set(GOLDEN["runs"]) == set(DESIGN_POINTS)
        python = GOLDEN["runs"]["fixture+python"]
        assert python["content_calls"] == ["tools/pyproject.toml", "pyproject.toml", "tools/sync.py"]
        blocks = [p["user"] for p in python["prompts"] if DEPENDENCY_HEADER in p["user"]]
        assert len(blocks) == 1
        assert "requests@==2.32.3" in blocks[0]
        assert "tools/sync.py:5: def load_config(path):" in _block(blocks[0], DEFINITIONS_HEADER)

    @pytest.mark.parametrize("point", sorted(DESIGN_POINTS))
    @pytest.mark.parametrize("variant", ["no-new-kwargs", "off", "off-with-every-other-kwarg-set"])
    def test_off_prompts_and_reads_equal_the_golden(self, point, variant):
        got = golden_run(point, **_off_variant(variant))
        want = GOLDEN["runs"][point]
        assert got["list_calls"] == want["list_calls"] == 0
        assert got["content_calls"] == want["content_calls"]
        assert len(got["prompts"]) == len(want["prompts"])
        for index, (old, new) in enumerate(zip(want["prompts"], got["prompts"], strict=True)):
            assert new == old, f"prompt {index}: {_first_difference(old, new)}"

    def test_control_the_same_comparison_fails_for_repo(self):
        """Every worker prompt differs; the sweep, the last prompt, carries no repository context and does not."""
        got = golden_run("fixture", repo_context="repo", context_contract_globs=GLOBS)
        want = GOLDEN["runs"]["fixture"]
        assert len(got["prompts"]) == len(want["prompts"])
        changed = [i for i, (old, new) in enumerate(zip(want["prompts"], got["prompts"], strict=True)) if old != new]
        assert changed == [0, 1, 2]
        assert got["list_calls"] == 1
        assert got["content_calls"] == REPO_READS != want["content_calls"]


class TestTheBudget:
    """Check 4: a tiny ``repo_context_max_chars`` admits one entry on the ConnectorService chunk and says so."""

    CAP = 300

    def test_a_tiny_cap_holds_and_names_what_it_left_out(self):
        result, _, llm = review(
            repo_context="repo", context_contract_globs=GLOBS, repo_context_max_chars=self.CAP, max_workers=1,
        )
        record = result["repo_context"]
        assert record["max_chars"] == self.CAP
        chunks = record["units"]["chunks"]
        assert [sum(e["chars"] for e in row["entries"]) for row in chunks] == [275, 250, 0]
        assert all(sum(e["chars"] for e in row["entries"]) <= self.CAP for row in chunks)
        assert _rows(result) == [[TC_9], [SPEC_31], []]
        assert [row["omitted"] for row in chunks] == [4, 0, 2]

        prompts = _worker_prompts(llm)
        connector = prompts[CONNECTOR_SERVICE]
        body = _block(connector, DEFINITIONS_HEADER).removeprefix(DEFINITIONS_HEADER + "\n\n")
        admitted, marker = body.rsplit("\n", 1)
        assert marker == f"{ELLIPSIS} 4 more context entries omitted"
        assert admitted.startswith(f"{TRANSPORT_CONFIG}:9: ")
        assert len(admitted) == 275 <= self.CAP
        assert f"{TRANSPORT_CONFIG}:11: " not in connector
        assert CONTRACT_HEADER not in connector

        transport = _block(prompts[TRANSPORT_CONFIG], CONTRACT_HEADER)
        assert transport.startswith(f"{CONTRACT_HEADER}\n\n{SPEC}:31: TransportConfig:")
        assert ELLIPSIS not in prompts[TRANSPORT_CONFIG]
        assert _block(prompts[MIGRATION], CONTRACT_HEADER) == (
            f"{CONTRACT_HEADER}\n\n{ELLIPSIS} 2 more context entries omitted"
        )


class TestNoReaderMeansNoReads:
    """Check 5: ``LocalDiffForge`` with no ``repo_dir`` reads nothing and keeps the hunk-based entries."""

    @pytest.mark.parametrize(("mode", "warnings"), [("repo", 1), ("diff", 0)])
    def test_a_local_diff_forge_gives_hunk_entries_only(self, mode, warnings, caplog):
        forge = LocalDiffForge(DIFF, path=str(DIFF_FILE))
        assert not hasattr(forge, "get_file_content")
        with caplog.at_level(logging.WARNING, logger="prxref"):
            result, _, llm = review(
                forge, ref=LocalDiffForge.ref_for(str(DIFF_FILE)), repo_context=mode,
                context_contract_globs=GLOBS, max_workers=1,
            )
        named = _named_warnings(caplog)
        assert len(named) == warnings
        if warnings:
            assert "no repository reader" in named[0] and "hunk lines" in named[0]
        record = result["repo_context"]
        assert (record["mode"], record["reader"], record["listing"], record["reads"], record["read_cap_hit"]) == (
            mode, None, None, 0, False,
        )
        assert _rows(result) == HUNK_ROWS
        prompts = _worker_prompts(llm)
        definitions = _block(prompts[CONNECTOR_SERVICE], DEFINITIONS_HEADER)
        assert EXCLUSIVITY in definitions[definitions.index(f"{TRANSPORT_CONFIG}:11: "):]
        assert all(CONTRACT_HEADER not in prompt for prompt in prompts.values())


class TestRepoDirOnALocalDiffForge:
    """Check 6: ``repo_dir`` gives a forge-less replay the contract excerpts of check 1."""

    def test_repo_dir_carries_both_labels_evidence(self, caplog):
        forge = LocalDiffForge(DIFF, path=str(DIFF_FILE))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            result, _, llm = review(
                forge, ref=LocalDiffForge.ref_for(str(DIFF_FILE)), repo_context="repo",
                context_contract_globs=GLOBS, repo_dir=RepoDir(REPO), max_workers=1,
            )
        assert _named_warnings(caplog) == []
        record = result["repo_context"]
        assert (record["reader"], record["listing"], record["reads"], record["read_cap_hit"]) == (
            "repo-dir", {"paths": 6, "complete": True}, 5, False,
        )
        assert _rows(result) == REPO_ROWS
        prompts = _worker_prompts(llm)
        _assert_connector_evidence(prompts[CONNECTOR_SERVICE])
        _assert_migration_evidence(prompts[MIGRATION])


CANARY = "LABELCANARY7f3a"
SCHEMA = "api/schemas/TransportConfig.schema.json"
SCHEMA_MARKER = "SCHEMACONTROL9c1d"
BAIT_JAVA = (
    "package com.acme.connectors;\n"
    "\n"
    "\n"
    "public record CreateTransportRequest(\n"
    f"        String idempotencyKey, String url, String legacyUrl, String {CANARY}) {{\n"
    "}\n"
)
LABEL_FILES = {
    "eval/cases.json": json.dumps({"title": "TransportConfig", "description": CANARY}),
    "eval/expected.json": json.dumps({"title": "TransportConfig", "description": CANARY}),
    "prxref-eval/notes.md": f"# TransportConfig\n\n{CANARY}\n",
    "prxref-eval/CreateTransportRequest.java": BAIT_JAVA,
    ".env": f"TRANSPORT_CONFIG_NOTE=TransportConfig {CANARY}\n",
    "keys/dev.pem": f"-----BEGIN TransportConfig {CANARY}-----\n",
}
SCHEMA_TEXT = json.dumps({"title": "TransportConfig", "description": SCHEMA_MARKER, "type": "object"}, indent=2)
LABEL_GLOBS = ["**/*.json", "**/*.md", "**/.env", "**/*.pem"]


def _tree(root: Path, files: dict[str, str]) -> Path:
    """A copy of the fixture repository under ``root`` with ``files`` added."""
    shutil.copytree(REPO, root)
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


class TestALabelFileIsNeverRead:
    """Check 7: the exclude floor keeps eval labels, eval output and secrets out, even against a user negation.

    Each floor file holds ``TransportConfig`` and a canary. The ``.json``
    labels are what the user's contract globs select, and
    ``prxref-eval/CreateTransportRequest.java`` is what the resolver's name
    search selects (the ConnectorService chunk references that type and the
    listing has no conventional path for it).
    """

    def test_the_floor_holds_and_a_legitimate_schema_is_still_read(self, tmp_path):
        forge = FixtureForge(root=_tree(tmp_path / "repo", {**LABEL_FILES, SCHEMA: SCHEMA_TEXT}))
        assert set(LABEL_FILES) <= set(forge.all_paths())
        assert len(forge.all_paths()) == 13
        result, forge, llm = review(
            forge, repo_context="repo", context_contract_globs=LABEL_GLOBS,
            context_exclude_globs=["!**/cases.json"], max_workers=1,
        )
        assert not set(forge.content_calls) & set(LABEL_FILES)
        assert forge.content_calls == [TRANSPORT_CONFIG, CONNECTOR_SERVICE, SCHEMA]
        record = result["repo_context"]
        assert record["contract_globs"] == LABEL_GLOBS
        assert record["exclude_globs"] == ["!**/cases.json"]
        assert record["listing"] == {"paths": 7, "complete": True}
        assert record["reads"] == 3
        schema = (SCHEMA, 1, "contract", "contract", "TransportConfig", 133)
        assert _rows(result) == [[TC_9, TC_11, schema], [schema], []]
        for system, user in llm.calls:
            assert CANARY not in system and CANARY not in user
        assert SCHEMA_MARKER in _block(_worker_prompts(llm)[CONNECTOR_SERVICE], CONTRACT_HEADER)

    def test_control_the_name_search_reads_the_same_java_file_outside_the_floor(self, tmp_path):
        """One factor varied: the bait moves from ``prxref-eval/`` to ``lib/``, and it is read and reaches the model."""
        files = {**LABEL_FILES, SCHEMA: SCHEMA_TEXT, "lib/CreateTransportRequest.java": BAIT_JAVA}
        del files["prxref-eval/CreateTransportRequest.java"]
        result, forge, llm = review(
            FixtureForge(root=_tree(tmp_path / "repo", files)), repo_context="repo",
            context_contract_globs=LABEL_GLOBS, context_exclude_globs=["!**/cases.json"], max_workers=1,
        )
        assert "lib/CreateTransportRequest.java" in forge.content_calls
        assert not set(forge.content_calls) & set(LABEL_FILES)
        assert result["repo_context"]["listing"] == {"paths": 8, "complete": True}
        connector_rows = _rows(result)[0]
        assert [row[:5] for row in connector_rows if row[0].startswith("lib/")] == [
            ("lib/CreateTransportRequest.java", 4, "name-search", "definition", "CreateTransportRequest"),
        ]
        assert CANARY in _block(_worker_prompts(llm)[CONNECTOR_SERVICE], DEFINITIONS_HEADER)


PARTS = [f"Part{i:02d}" for i in range(1, 21)]
PARTS_DIR = "src/main/java/com/acme/connectors/parts"
CONTROLLER = f"{PARTS_DIR}/TransportController.java"
ONE_CHUNK = {"max_files_per_chunk": 30, "token_budget": 200_000}


def _new_file_diff(path: str, text: str) -> str:
    lines = text.splitlines()
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


def _starvation_forge(root: Path) -> FixtureForge:
    """One chunk of 20 new Java types in a reference ring plus a Spring controller, over the fixture's spec."""
    texts = {
        f"{PARTS_DIR}/{name}.java": (
            f"package com.acme.connectors.parts;\n\npublic class {name} {{\n"
            f"    private {PARTS[(i + 1) % len(PARTS)]} next;\n}}\n"
        )
        for i, name in enumerate(PARTS)
    }
    texts[CONTROLLER] = (
        "package com.acme.connectors.parts;\n\n"
        "public class TransportController {\n\n"
        '    @PostMapping("/connectors/{connectorId}/transports")\n'
        "    public TransportConfig createTransport(@RequestBody CreateTransportRequest request, "
        + ", ".join(f"{name} p{i}" for i, name in enumerate(PARTS)) + ") {\n"
        "        return null;\n"
        "    }\n"
        "}\n"
    )
    for path, text in {**texts, SPEC: (REPO / SPEC).read_text(encoding="utf-8")}.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return FixtureForge("".join(_new_file_diff(path, text) for path, text in texts.items()), root=root)


class TestReadCapStarvation:
    """Check 8 (decision D-L): a chunk of many diff files still reads its spec, because diff files are uncapped.

    ``test_orchestrator_repo_context.py``'s ``TestDiffFileReadsAreUncapped`` proves the routing over a
    ``repo_dir`` with a ``/widgets`` spec and controls it with a direct build.
    This adds the forge reader, the fixture's templated route and spec, and a
    control through ``orchestrate_review`` itself: ``orchestrator._routed_read``
    replaced by the capped chunk reader alone.
    """

    def test_the_controller_chunk_keeps_its_route_contract(self, tmp_path):
        forge = _starvation_forge(tmp_path / "repo")
        assert len(build_chunks(parse_unified_diff(forge.diff), **ONE_CHUNK)) == 1
        result, forge, llm = review(
            forge, repo_context="repo", context_contract_globs=GLOBS, max_workers=1, **ONE_CHUNK,
        )
        record = result["repo_context"]
        assert (record["listing"], record["reads"], record["read_cap_hit"]) == (
            {"paths": 22, "complete": True}, 22, False,
        )
        assert _rows(result) == [[SPEC_6, SPEC_31, SPEC_41]]
        assert len(forge.content_calls) == 22 > MAX_CHUNK_READS
        assert forge.content_calls[-1] == SPEC
        worker = llm.calls[0][1]
        assert f"{SPEC}:6: /connectors/{{connectorId}}/transports:" in _block(worker, CONTRACT_HEADER)

    def test_control_every_read_through_the_capped_chunk_reader_starves_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(orchestrator, "_routed_read", lambda reader, diff_paths: reader.chunk_reader())
        result, forge, llm = review(
            _starvation_forge(tmp_path / "repo"), repo_context="repo", context_contract_globs=GLOBS,
            max_workers=1, **ONE_CHUNK,
        )
        record = result["repo_context"]
        assert (record["reads"], record["read_cap_hit"]) == (MAX_CHUNK_READS, True)
        assert _rows(result) == [[]]
        assert len(forge.content_calls) == MAX_CHUNK_READS
        assert SPEC not in forge.content_calls
        assert CONTRACT_HEADER not in llm.calls[0][1]


class TestDeterminism:
    """Check 9: one worker and four workers give the same record, the same prompts and the same reads."""

    def test_one_and_four_workers_agree(self):
        runs = [
            review(repo_context="repo", context_contract_globs=GLOBS, max_workers=workers)
            for workers in (1, 4, 4)
        ]
        base, base_forge, base_llm = runs[0]
        assert _rows(base) == REPO_ROWS
        for result, forge, llm in runs[1:]:
            assert result["repo_context"] == base["repo_context"]
            assert _worker_prompts(llm) == _worker_prompts(base_llm)
            assert sorted(llm.calls[:-1]) == sorted(base_llm.calls[:-1])
            assert llm.calls[-1] == base_llm.calls[-1]
            assert sorted(forge.content_calls) == sorted(base_forge.content_calls) == sorted(REPO_READS)
