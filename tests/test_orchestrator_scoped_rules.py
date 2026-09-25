"""Path-scoped review rules in the orchestrator (#12): ``orchestrate_review(scoped_rules=...)``.

The orchestrator takes one loaded :class:`prxref.rules.ScopedRules` and the
per-unit cap, and wires them five ways: each chunk's own block replaces
``rules_worker`` in that chunk's copy of the prompt context, on the first
attempt and on the timeout retry; the sweep's block, over the union of the
chunks' paths, replaces ``rules_sweep``; the merged severity map feeds the
remapping pass, with or without an always-on file; the record gains a
``scoped_rules`` key on every exit, ``None`` when unset; and the trace gains one
``scoped_rules ok`` event plus ``rules`` rows on the chunk and sweep start
events, only when set.

These tests run the REAL reviewer and the REAL loaders end to end and capture
every prompt with a recording LLM. None of them uses the ``contract_stubs``
fixture, which replaces ``reviewer.load_prompt`` with a summary-only stub. The
loaders confine every path to the working directory, so each test runs from a
fresh directory under ``tmp_path``.
"""
from __future__ import annotations

import inspect
import json
import logging
import threading
from pathlib import Path

import pytest

from prxref import config, orchestrator, viz
from prxref.llm import InvokeResult
from prxref.reviewer import RULE_REQUEST
from prxref.rules import ScopedRules, load_review_rules, load_scoped_rules
from prxref.triage import build_chunks, parse_unified_diff
from tests.test_orchestrator import REF, FakeForge, _added_file_diff, multi_chunk_diff

JAVA_RULE = "JAVA-RULE: every service method validates its arguments."
HELM_RULE = "HELM-RULE: every manifest sets CPU and memory limits."
DOCS_RULE = "DOCS-RULE: every page links its owner."
LEGACY_RULE = "LEGACY-RULE: code moved out of legacy/ keeps its audit log."
ALWAYS_RULE = "ALWAYS-RULE: never log a secret."

JAVA_MD = f'---\napplies_to: ["**/*.java"]\n---\n{JAVA_RULE}\n'
HELM_MD = f'---\napplies_to:\n  - "charts/**"\n---\n{HELM_RULE}\n'
DOCS_MD = f"---\napplies_to: docs/**\n---\n{DOCS_RULE}\n"
LEGACY_MD = f"---\napplies_to: legacy/**\n---\n{LEGACY_RULE}\n"
BLOCKER_MD = '---\napplies_to: ["docs/**"]\nseverity:\n  blocker: error\n---\n'
ALWAYS_MD = f"---\nseverity:\n  nit: warning\n---\n{ALWAYS_RULE}\n"

JAVA_PATHS = [f"src/main/java/com/acme/Service{i}.java" for i in range(1, 6)]
HELM_PATHS = [f"charts/app/templates/manifest{i}.yaml" for i in range(1, 4)]
SERVICE = JAVA_PATHS[0]

NO_FINDINGS = '{"findings": []}'
CAP_NAME = "PRXREF_SCOPED_RULES_MAX_CHARS"
DEFAULT_CAP = config._DEFAULTS["scoped_rules_max_chars"]


def _finding(severity: str, title: str = "Unchecked input", path: str = SERVICE) -> dict:
    return {
        "file": path, "line": 3, "severity": severity, "confidence": 0.9,
        "title": title, "body": f"data 3 is used before it is checked ({title}).",
    }


def _reply(*findings: dict) -> str:
    return json.dumps({"findings": list(findings)})


class _RecordingLLM:
    """Records every ``(system, user)`` prompt; call numbers in ``fail_calls`` time out."""

    def __init__(self, text: str = NO_FINDINGS, *, fail_calls=(), error: Exception | None = None):
        self.text = text
        self.fail_calls = set(fail_calls)
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        with self._lock:
            number = len(self.calls)
            self.calls.append((system, user))
        if self.error is not None:
            raise self.error
        if number in self.fail_calls:
            raise TimeoutError("request timeout after 60s")
        return InvokeResult(
            text=self.text, input_tokens=10, output_tokens=5,
            model="fake-model", backend="fake", elapsed_ms=1,
        )


@pytest.fixture
def work(tmp_path, monkeypatch) -> Path:
    """A fresh working directory, with the attribution's elapsed time pinned."""
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(orchestrator, "_elapsed_ms", lambda t0: 0)
    return root


def _scoped(work: Path, files: dict[str, str], *, always_on=None) -> ScopedRules:
    """Write ``files`` into ``rules/`` and load that directory as the scoped rules."""
    d = work / "rules"
    d.mkdir(exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text, encoding="utf-8")
    scoped = load_scoped_rules(["rules"], max_chars=12000, source="--scoped-rules", always_on=always_on)
    assert scoped is not None
    return scoped


def _always_on(work: Path, text: str = ALWAYS_MD):
    (work / "team-rules.md").write_text(text, encoding="utf-8")
    rules = load_review_rules("team-rules.md", max_chars=12000, source="PRXREF_REVIEW_RULES")
    assert rules is not None
    return rules


def stack_diff() -> str:
    """Five Java files and three Helm manifests, which chunk into exactly one chunk per stack.

    The Java files carry more changed lines, so they score higher and fill the
    first chunk up to the default five-file cap; the manifests open the second.
    The partition is asserted, so a chunking change fails here loudly.
    """
    diff = "".join(_added_file_diff(p, 20) for p in JAVA_PATHS)
    diff += "".join(_added_file_diff(p, 8) for p in HELM_PATHS)
    chunks = build_chunks(parse_unified_diff(diff))
    assert [[f.path for f in chunk] for chunk in chunks] == [JAVA_PATHS, HELM_PATHS]
    return diff


RENAME_DIFF = (
    "diff --git a/legacy/Old.java b/src/main/java/com/acme/New.java\n"
    "similarity index 90%\n"
    "rename from legacy/Old.java\n"
    "rename to src/main/java/com/acme/New.java\n"
    "--- a/legacy/Old.java\n"
    "+++ b/src/main/java/com/acme/New.java\n"
    "@@ -1,2 +1,2 @@\n"
    " keep\n"
    "-data 1\n"
    "+data 2\n"
)


def _review(diff: str, llm: _RecordingLLM | None = None, **kwargs):
    forge = FakeForge(diff=diff)
    llm = llm if llm is not None else _RecordingLLM()
    res = orchestrator.orchestrate_review(forge, REF, llm, **kwargs)
    return res, forge, llm


def _stack(user: str) -> str:
    """Which chunk a worker prompt belongs to, read from its rendered diff headers."""
    java = f"diff --git a/{JAVA_PATHS[0]}" in user
    helm = f"diff --git a/{HELM_PATHS[0]}" in user
    assert java != helm, "a worker prompt must render exactly one stack"
    return "java" if java else "helm"


def _units(llm: _RecordingLLM) -> tuple[dict[str, list[tuple[str, str]]], tuple[str, str]]:
    """The chunk prompts grouped by stack (in call order), and the sweep prompt, which runs last."""
    *chunks, sweep = llm.calls
    grouped: dict[str, list[tuple[str, str]]] = {"java": [], "helm": []}
    for system, user in chunks:
        grouped[_stack(user)].append((system, user))
    return grouped, sweep


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _stable(events: list[dict]) -> list[tuple]:
    """Every event but its wall-clock fields."""
    return [
        (e["node"], e["phase"], {k: v for k, v in (e.get("meta") or {}).items() if k != "elapsed_ms"})
        for e in events
    ]


def _with_block(system: str, block: str) -> str:
    return f"{system}\n\n{block.strip()}" if block.strip() else system


def _before_request(system: str, block: str) -> str:
    """``system`` with ``block`` put in ahead of its trailing :data:`RULE_REQUEST`."""
    head = system.removesuffix(f"\n\n{RULE_REQUEST}")
    assert head != system, "the base run must carry the rule request"
    return f"{_with_block(head, block)}\n\n{RULE_REQUEST}"


def _rows(scoped: ScopedRules, *paths: str) -> list[dict]:
    by_path = {f.path: f for f in scoped.files}
    return [{"path": p, "chars": by_path[p].body.chars} for p in paths]


class TestTheParameters:
    def test_both_kwargs_are_keyword_only_and_follow_prompts(self):
        params = inspect.signature(orchestrator.orchestrate_review).parameters
        names = list(params)
        assert names[names.index("prompts") + 1:names.index("prompts") + 3] == [
            "scoped_rules", "scoped_rules_max_chars",
        ]
        for name in ("scoped_rules", "scoped_rules_max_chars"):
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY

    def test_scoped_rules_is_off_by_default(self):
        params = inspect.signature(orchestrator.orchestrate_review).parameters
        assert params["scoped_rules"].default is None

    def test_the_cap_default_restates_the_config_default(self):
        params = inspect.signature(orchestrator.orchestrate_review).parameters
        assert params["scoped_rules_max_chars"].default == config._DEFAULTS["scoped_rules_max_chars"]
        assert DEFAULT_CAP == 24000


class TestUnsetRun:
    def test_the_record_key_is_present_and_null(self, work):
        for kwargs in ({}, {"scoped_rules": None}):
            res, _, _ = _review(multi_chunk_diff(2), **kwargs)
            assert "scoped_rules" in res
            assert res["scoped_rules"] is None

    def test_none_is_byte_identical_to_a_run_without_the_kwarg(self, work, tmp_path, caplog):
        """Prompts, retries, remap, posts, record, trace and logs, with an always-on file set."""
        rules = _always_on(work)
        reply = _reply(_finding("nit", "Style drift", "src/big1.py"))
        runs = []
        for name, kwargs in (("base", {}), ("none", {"scoped_rules": None})):
            trace = tmp_path / f"{name}.jsonl"
            caplog.clear()
            with caplog.at_level(logging.INFO, logger="prxref"):
                res, forge, llm = _review(
                    multi_chunk_diff(2), _RecordingLLM(reply, fail_calls={0, 2}),
                    rules=rules, max_workers=1, trace_file=str(trace), **kwargs,
                )
            logs = [(r.name, r.levelname, r.getMessage()) for r in caplog.records]
            runs.append((res, forge.summaries, forge.inline_batches, llm.calls, _stable(_events(trace)), logs))
        base, none = runs
        assert len(base[3]) == 5
        assert ("chunk", "retry") in [(n, p) for n, p, _ in base[4]]
        assert ("rules", "remap") in [(n, p) for n, p, _ in base[4]]
        assert none == base

    def test_an_unset_run_emits_no_scoped_event_and_no_rules_meta(self, work, tmp_path):
        trace = tmp_path / "trace.jsonl"
        _review(stack_diff(), trace_file=str(trace))
        events = _events(trace)
        assert [e for e in events if e["node"] == "scoped_rules"] == []
        starts = [e for e in events if e["phase"] == "start" and e["node"] in ("chunk", "sweep")]
        assert len(starts) == 3
        assert all("rules" not in e["meta"] for e in starts)

    @pytest.mark.parametrize("with_always_on", [False, True])
    def test_a_scoped_set_that_reaches_no_unit_renders_the_unset_prompts(self, work, with_always_on):
        """No file selected and no new severity word: every unit keeps the prompt it had without them.

        A loaded set still turns the per-rule cap on, so the base run turns grouping on for the same rule request.
        """
        rules = _always_on(work) if with_always_on else None
        scoped = _scoped(work, {"docs.md": DOCS_MD}, always_on=rules)
        base, base_forge, base_llm = _review(stack_diff(), rules=rules, group_findings=True)
        res, forge, llm = _review(stack_diff(), rules=rules, scoped_rules=scoped)
        assert _units(llm) == _units(base_llm)
        assert DOCS_RULE not in "".join(system for system, _ in llm.calls)
        assert forge.summaries == base_forge.summaries
        assert {k: v for k, v in res.items() if k not in ("scoped_rules", "rule_counts")} == {
            k: v for k, v in base.items() if k not in ("scoped_rules", "rule_counts")
        }
        assert res["scoped_rules"]["units"] == {"chunks": [[], []], "sweep": []}
        assert res["rule_counts"] == []


class TestPerUnitSelection:
    def test_each_chunk_carries_only_its_own_rules_and_the_sweep_carries_both(self, work):
        """The base run turns grouping on for the rule request the set's per-rule cap adds."""
        scoped = _scoped(work, {"java.md": JAVA_MD, "helm.md": HELM_MD})
        _, _, base_llm = _review(stack_diff(), group_findings=True)
        _, _, llm = _review(stack_diff(), scoped_rules=scoped)
        assert len(llm.calls) == 3
        (grouped, sweep), (base_grouped, base_sweep) = _units(llm), _units(base_llm)
        [(java_system, java_user)] = grouped["java"]
        [(helm_system, helm_user)] = grouped["helm"]
        assert JAVA_RULE in java_system and HELM_RULE not in java_system
        assert HELM_RULE in helm_system and JAVA_RULE not in helm_system
        assert JAVA_RULE in sweep[0] and HELM_RULE in sweep[0]
        java_block = scoped.unit_block("worker", JAVA_PATHS, None, max_chars=DEFAULT_CAP)
        helm_block = scoped.unit_block("worker", HELM_PATHS, None, max_chars=DEFAULT_CAP)
        sweep_block = scoped.unit_block("sweep", JAVA_PATHS + HELM_PATHS, None, max_chars=DEFAULT_CAP)
        assert java_system == _before_request(base_grouped["java"][0][0], java_block.text)
        assert helm_system == _before_request(base_grouped["helm"][0][0], helm_block.text)
        assert sweep[0] == _before_request(base_sweep[0], sweep_block.text)
        assert (java_user, helm_user, sweep[1]) == (
            base_grouped["java"][0][1], base_grouped["helm"][0][1], base_sweep[1],
        )

    def test_the_always_on_file_rides_every_unit_beside_the_scoped_ones(self, work):
        rules = _always_on(work)
        scoped = _scoped(work, {"java.md": JAVA_MD, "helm.md": HELM_MD}, always_on=rules)
        _, _, llm = _review(stack_diff(), rules=rules, scoped_rules=scoped)
        grouped, sweep = _units(llm)
        [(java_system, _)] = grouped["java"]
        [(helm_system, _)] = grouped["helm"]
        for system in (java_system, helm_system, sweep[0]):
            assert system.count(ALWAYS_RULE) == 1
        for system, paths in ((java_system, JAVA_PATHS), (helm_system, HELM_PATHS)):
            block = scoped.unit_block("worker", paths, rules, max_chars=DEFAULT_CAP).text
            assert system.endswith(f"{block}\n\n{RULE_REQUEST}")
        assert JAVA_RULE not in helm_system and HELM_RULE not in java_system

    def test_a_rename_is_selected_by_its_old_path(self, work):
        scoped = _scoped(work, {"legacy.md": LEGACY_MD, "helm.md": HELM_MD})
        res, _, llm = _review(RENAME_DIFF, scoped_rules=scoped)
        [(chunk_system, _), (sweep_system, _)] = llm.calls
        assert LEGACY_RULE in chunk_system and HELM_RULE not in chunk_system
        assert LEGACY_RULE in sweep_system
        rows = _rows(scoped, "rules/legacy.md")
        assert res["scoped_rules"]["units"] == {"chunks": [rows], "sweep": rows}


class TestTheTimeoutRetry:
    def test_the_retry_keeps_the_chunks_own_block(self, work, tmp_path):
        """With one worker the calls run in order: each chunk's first attempt times out."""
        scoped = _scoped(work, {"java.md": JAVA_MD, "helm.md": HELM_MD})
        trace = tmp_path / "trace.jsonl"
        _, _, llm = _review(
            stack_diff(), _RecordingLLM(fail_calls={0, 2}),
            scoped_rules=scoped, max_workers=1, trace_file=str(trace),
        )
        assert len(llm.calls) == 5
        assert [e["phase"] for e in _events(trace) if e["node"] == "chunk"].count("retry") == 2
        (java_first, java_retry), (helm_first, helm_retry) = llm.calls[0:2], llm.calls[2:4]
        assert _stack(java_first[1]) == _stack(java_retry[1]) == "java"
        assert _stack(helm_first[1]) == _stack(helm_retry[1]) == "helm"
        assert java_retry[0] == java_first[0]
        assert helm_retry[0] == helm_first[0]
        assert JAVA_RULE in java_retry[0] and HELM_RULE not in java_retry[0]
        assert HELM_RULE in helm_retry[0] and JAVA_RULE not in helm_retry[0]


class TestTheSeverityMap:
    def test_a_scoped_only_map_remaps_with_no_always_on_file(self, work, tmp_path):
        """The file reaches no unit (docs/**), yet its map is run-wide."""
        scoped = _scoped(work, {"blocker.md": BLOCKER_MD})
        diff = _added_file_diff(SERVICE, 20)
        reply = _reply(_finding("blocker"))
        trace = tmp_path / "trace.jsonl"
        res, _, _ = _review(diff, _RecordingLLM(reply), scoped_rules=scoped, trace_file=str(trace))
        assert [f.severity for f in res["findings_active"]] == ["error"]
        assert res["verdict"] == "Request-Changes"
        remaps = [e for e in _events(trace) if (e["node"], e["phase"]) == ("rules", "remap")]
        assert len(remaps) == 1 and remaps[0]["meta"]["findings"] >= 1
        off, _, _ = _review(diff, _RecordingLLM(reply))
        assert off["findings_active"] == []
        assert {f.drop_reason for f in off["findings_dropped"]} == {"invalid severity: 'blocker'"}

    def test_the_always_on_and_scoped_maps_both_apply(self, work):
        rules = _always_on(work)
        scoped = _scoped(work, {"blocker.md": BLOCKER_MD}, always_on=rules)
        reply = _reply(_finding("blocker", "Crash"), _finding("nit", "Style drift", JAVA_PATHS[1]))
        diff = _added_file_diff(SERVICE, 20) + _added_file_diff(JAVA_PATHS[1], 20)
        res, _, _ = _review(diff, _RecordingLLM(reply), rules=rules, scoped_rules=scoped)
        assert sorted((f.title, f.severity) for f in res["findings_active"]) == [
            ("Crash", "error"), ("Style drift", "warning"),
        ]


class TestThePerUnitCap:
    LONG = " Validate every input before it reaches the store." * 6

    def _setup(self, work) -> ScopedRules:
        return _scoped(work, {
            "java.md": JAVA_MD.replace(JAVA_RULE, JAVA_RULE + self.LONG),
            "helm.md": HELM_MD.replace(HELM_RULE, HELM_RULE + self.LONG),
        })

    def _cap_warnings(self, caplog) -> list[logging.LogRecord]:
        return [r for r in caplog.records if CAP_NAME in r.getMessage()]

    def test_a_cap_that_cuts_logs_exactly_one_warning(self, work, caplog):
        scoped = self._setup(work)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            res, _, llm = _review(stack_diff(), scoped_rules=scoped, scoped_rules_max_chars=50)
        [warning] = self._cap_warnings(caplog)
        assert warning.levelno == logging.WARNING
        assert warning.getMessage() == (
            f"scoped rules exceed {CAP_NAME} (50 characters per review unit) in 3 of 3 "
            "review unit(s); truncated: rules/helm.md, rules/java.md; omitted: rules/java.md"
        )
        grouped, sweep = _units(llm)
        assert "[team rules truncated: only the first 50 of" in grouped["java"][0][0]
        assert "[team rules omitted: rules/java.md" in sweep[0]
        assert res["scoped_rules"]["max_chars"] == 50
        assert res["scoped_rules"]["units"]["sweep"] == _rows(scoped, "rules/helm.md")

    def test_no_warning_when_every_unit_fits(self, work, caplog):
        scoped = self._setup(work)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            _review(stack_diff(), scoped_rules=scoped)
        assert self._cap_warnings(caplog) == []

    def test_a_cap_below_one_is_an_error_run_not_a_raise(self, work, tmp_path):
        scoped = self._setup(work)
        trace = tmp_path / "trace.jsonl"
        res, forge, llm = _review(
            stack_diff(), scoped_rules=scoped, scoped_rules_max_chars=0, trace_file=str(trace),
        )
        assert res["verdict"] == "Error"
        assert llm.calls == []
        assert len(forge.summaries) == 1 and forge.inline_batches == []
        assert res["scoped_rules"]["units"] is None
        pairs = [(e["node"], e["phase"]) for e in _events(trace)]
        assert pairs.index(("run", "fail")) < pairs.index(("post", "start"))
        assert [n for n, _ in pairs if n in ("chunk", "sweep")] == []


class TestTheRunRecord:
    def test_the_shape_of_a_normal_run(self, work):
        scoped = _scoped(work, {"java.md": JAVA_MD, "helm.md": HELM_MD})
        res, _, _ = _review(stack_diff(), scoped_rules=scoped)
        record = res["scoped_rules"]
        assert list(record) == ["entries", "files", "max_chars", "units"]
        assert record == {
            **scoped.record(),
            "max_chars": DEFAULT_CAP,
            "units": {
                "chunks": [_rows(scoped, "rules/java.md"), _rows(scoped, "rules/helm.md")],
                "sweep": _rows(scoped, "rules/helm.md", "rules/java.md"),
            },
        }
        assert [f["path"] for f in record["files"]] == ["rules/helm.md", "rules/java.md"]
        assert json.loads(json.dumps(record)) == record

    @pytest.mark.parametrize(
        "exit_path", ["get_pr", "build_chunks", "empty_diff", "cap_error", "llm_total_failure", "normal"],
    )
    def test_every_exit_carries_the_key(self, work, exit_path):
        scoped = _scoped(work, {"java.md": JAVA_MD, "helm.md": HELM_MD})
        planned = {
            "chunks": [_rows(scoped, "rules/java.md"), _rows(scoped, "rules/helm.md")],
            "sweep": _rows(scoped, "rules/helm.md", "rules/java.md"),
        }
        cap = 0 if exit_path == "cap_error" else DEFAULT_CAP
        units = planned if exit_path in ("llm_total_failure", "normal") else None
        for given, expected in (
            (None, None),
            (scoped, {**scoped.record(), "max_chars": cap, "units": units}),
        ):
            forge = FakeForge(diff="" if exit_path == "empty_diff" else stack_diff())
            if exit_path == "get_pr":
                forge.fail.add("get_pr")
            error = RuntimeError("model offline") if exit_path == "llm_total_failure" else None
            kwargs = {"max_chunks": 0} if exit_path == "build_chunks" else {}
            if given is not None:
                kwargs["scoped_rules_max_chars"] = cap
            res = orchestrator.orchestrate_review(
                forge, REF, _RecordingLLM(error=error), scoped_rules=given, **kwargs,
            )
            assert "scoped_rules" in res
            assert res["scoped_rules"] == expected
            assert json.loads(json.dumps(res["scoped_rules"])) == expected
            normal = exit_path in ("normal", "empty_diff") or (exit_path == "cap_error" and given is None)
            assert res["verdict"] == ("Approved" if normal else "Error")


class _Ticket:
    active = False

    def record(self):
        return {"source": "ticket.txt"}

    def note(self):
        return ""


class _Prompts:
    def record(self):
        return {"dir": "prompts", "templates": {}}

    def override(self, name):
        return ""


class TestTraceEvents:
    def test_a_set_run_emits_one_scoped_rules_ok_event_after_rules_ok(self, work, tmp_path):
        rules = _always_on(work)
        scoped = _scoped(work, {"java.md": JAVA_MD, "helm.md": HELM_MD}, always_on=rules)
        trace = tmp_path / "trace.jsonl"
        _review(
            stack_diff(), rules=rules, scoped_rules=scoped, ticket=_Ticket(), prompts=_Prompts(),
            scoped_rules_max_chars=5000, trace_file=str(trace),
        )
        events = _events(trace)
        assert [(e["node"], e["phase"]) for e in events[:5]] == [
            ("run", "start"), ("rules", "ok"), ("scoped_rules", "ok"), ("ticket", "ok"), ("prompts", "ok"),
        ]
        found = [e for e in events if e["node"] == "scoped_rules"]
        assert len(found) == 1
        assert found[0]["meta"] == {**scoped.record(), "max_chars": 5000}

    def test_the_chunk_and_sweep_start_events_carry_the_units_rows(self, work, tmp_path):
        scoped = _scoped(work, {"java.md": JAVA_MD, "helm.md": HELM_MD})
        trace = tmp_path / "trace.jsonl"
        _review(stack_diff(), scoped_rules=scoped, trace_file=str(trace))
        events = _events(trace)
        chunk_starts = {
            tuple(e["meta"]["files"]): e["meta"]
            for e in events if (e["node"], e["phase"]) == ("chunk", "start")
        }
        assert chunk_starts[tuple(JAVA_PATHS)]["rules"] == _rows(scoped, "rules/java.md")
        assert chunk_starts[tuple(HELM_PATHS)]["rules"] == _rows(scoped, "rules/helm.md")
        [sweep_start] = [e for e in events if (e["node"], e["phase"]) == ("sweep", "start")]
        assert sweep_start["meta"]["rules"] == _rows(scoped, "rules/helm.md", "rules/java.md")
        assert list(sweep_start["meta"]) == ["files", "digest_chars", "threads", "rules"]

    def test_a_unit_no_file_reaches_carries_an_empty_rules_list(self, work, tmp_path):
        scoped = _scoped(work, {"java.md": JAVA_MD})
        trace = tmp_path / "trace.jsonl"
        _review(stack_diff(), scoped_rules=scoped, trace_file=str(trace))
        starts = {
            tuple(e["meta"]["files"]): e["meta"]["rules"]
            for e in _events(trace) if (e["node"], e["phase"]) == ("chunk", "start")
        }
        assert starts == {tuple(JAVA_PATHS): _rows(scoped, "rules/java.md"), tuple(HELM_PATHS): []}

    def test_the_viz_summary_buckets_the_event_as_a_closed_node(self, work, tmp_path):
        scoped = _scoped(work, {"java.md": JAVA_MD})
        trace = tmp_path / "trace.jsonl"
        _review(stack_diff(), scoped_rules=scoped, trace_file=str(trace))
        events = viz.load_events(trace)
        summary = viz.summarize(events)
        assert summary["nodes"]["scoped_rules"]["ok"] == 1
        assert summary["nodes"]["scoped_rules"]["starts"] == 0
        assert "scoped_rules" not in summary["failed"]
        assert summary["open_nodes"] == []
        assert "scoped_rules" not in {n["id"] for n in viz.NODES}
        assert '"scoped_rules"' in viz.render_html(events)
