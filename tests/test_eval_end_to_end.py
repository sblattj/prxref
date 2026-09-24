"""The eval harness end to end (#14 T9): ``eval run``, ``eval score``, ``eval compare``.

Every step goes through ``cli.main`` with its argv built here, and the real
case loader, CLI, orchestrator, reviewer, quality passes, judge prompt, judge
cache, metrics and compare run. Three things are doubles: the LLM client
factory (``prxref.llm_backends.create_llm_client``, which the review and the
judge both look up at call time, so one factory dispatches on
``cfg["llm_models"]``), the ``--pr-url`` forge (``prxref.cli.make_forge``
returns a :class:`tests.test_replay.RecordingForge`), and any HTTP request,
which fails the test.

One ``cases.json`` holds five cases, because ``--cases`` takes one path and
the directory form cannot express a pull-request case:

- the three ``tests/evals/case-*`` directories, loaded by the directory
  loader and written back with :func:`prxref.eval_cases.case_to_json`;
- ``case-pinned``, a ``pr_url`` pinned to two SHAs, served by the forge;
- ``case-grouped``, a local diff on which the reviewer reports two findings
  that break one ``rule`` in one file, reviewed with
  ``PRXREF_GROUP_FINDINGS=1`` so the review folds them into one.

Two labels are run and scored. ``base`` uses the defaults. ``new`` adds a
``--rules-file`` whose marker makes the reviewer stub report one more
finding in ``case-pinned``, so exactly one label changes between the arms.

The judge stub grades like a judge that reads lines as a hint: each label is
credited to the same-file AI finding on the nearest line, and graded
``none`` when its file has no AI finding.

``cli._finding_json`` gains its ``locations`` key with #13's JSON emission,
which may postdate the tree this file runs on. :func:`_bridge_locations` adds
the key exactly as decided for it, from the ``Finding.locations`` the
grouping pass sets, and only when the row lacks it, so the bridge is inert
once the CLI emits the key. :class:`TestGroupedWithoutTheBridge` runs the
grouped case without it; its per-location credit through the record is a
strict expected failure only while the CLI does not emit the key, and a
plain test once it does.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from prxref import cli, orchestrator
from prxref.eval_cases import case_to_json, load_cases
from prxref.llm import InvokeResult
from tests.test_replay import RecordingForge

EVALS_DIR = Path(__file__).parent / "evals"
REVIEWER = "reviewer-m"
JUDGE = "judge-m"
WORKER_OPENING = "You review one chunk of a pull-request diff per call."
ARM_MARKER = "E2E-ARM-NEW: name the retry limit constant in every retry module."
ELAPSED_MS = 1500

PR_URL = "https://github.com/acme/widget/pull/7"
PIN_BASE = "a" * 40
PIN_HEAD = "b" * 40
PINNED_PATH = "src/widget/retry.py"
PINNED_DIFF = (
    f"diff --git a/{PINNED_PATH} b/{PINNED_PATH}\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    f"+++ b/{PINNED_PATH}\n"
    "@@ -0,0 +1,5 @@\n"
    "+MAX_RETRIES = 3\n"
    "+\n"
    "+\n"
    "+def backoff_seconds(attempt):\n"
    "+    return 2 ** attempt\n"
)
GROUPED_PATH = "src/vault/credentials.py"
GROUPED_DIFF = (
    f"diff --git a/{GROUPED_PATH} b/{GROUPED_PATH}\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    f"+++ b/{GROUPED_PATH}\n"
    "@@ -0,0 +1,16 @@\n"
    '+"""Service credentials."""\n'
    "+import os\n"
    "+\n"
    '+DB_PASSWORD = "hunter2"\n'
    '+DB_USER = "admin"\n'
    "+\n"
    "+\n"
    "+def database_url():\n"
    '+    host = os.environ.get("DB_HOST", "localhost")\n'
    '+    return f"postgres://{DB_USER}@{host}/app"\n'
    "+\n"
    "+\n"
    "+def cache_url():\n"
    '+    return "redis://localhost:6379/0"\n'
    "+\n"
    '+API_TOKEN = "tok_live_abcdef"\n'
)
REP_LINE = 4
MEMBER_LINE = 16

PINNED_CASE = {
    "id": "case-pinned",
    "pr_url": PR_URL,
    "base_sha": PIN_BASE,
    "head_sha": PIN_HEAD,
    "expected": [
        {"id": "P1", "file": PINNED_PATH, "line": 5, "severity": "warning", "category": "reliability",
         "text": "The retry delay grows without a ceiling."},
        {"id": "P2", "file": PINNED_PATH, "line": 1, "severity": "minor", "category": "style",
         "must_match": "MAX_RETRIES"},
    ],
}
GROUPED_CASE = {
    "id": "case-grouped",
    "diff_file": "grouped.patch",
    "expected": [
        {"id": "M1", "file": GROUPED_PATH, "line": REP_LINE, "severity": "error", "category": "security",
         "accepted": True, "must_match": "DB_PASSWORD"},
        {"id": "M2", "file": GROUPED_PATH, "line": REP_LINE, "severity": "error", "category": "security",
         "must_match": "plaintext"},
        {"id": "J1", "file": GROUPED_PATH, "line": MEMBER_LINE, "severity": "error", "category": "security",
         "text": "API_TOKEN is a live secret committed to the repository."},
    ],
}


def _finding(file: str, line: int, title: str, body: str, *, severity: str = "error",
             rule: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "file": file, "line": line, "severity": severity, "confidence": 0.9, "title": title, "body": body,
    }
    if rule is not None:
        row["rule"] = rule
    return row


REVIEW_FINDINGS = {
    "src/mcp_client/session.py": [
        _finding("src/mcp_client/session.py", 17, "Initialize payload omits clientInfo",
                 "The new capabilities entry (roots listChanged) ships without the required clientInfo object.",
                 severity="spec"),
    ],
    PINNED_PATH: [
        _finding(PINNED_PATH, 5, "Retry delay has no ceiling",
                 "`return 2 ** attempt` grows without a ceiling as attempt rises.", severity="warning"),
    ],
    GROUPED_PATH: [
        _finding(GROUPED_PATH, REP_LINE, "Hardcoded database password",
                 "DB_PASSWORD is committed as the plaintext literal hunter2.", rule="no-hardcoded-secrets"),
        _finding(GROUPED_PATH, MEMBER_LINE, "Hardcoded API token",
                 "API_TOKEN is committed as the plaintext literal tok_live_abcdef.", rule="no-hardcoded-secrets"),
    ],
}
ARM_FINDINGS = {
    PINNED_PATH: [
        _finding(PINNED_PATH, 1, "Retry limit constant is unnamed in the docs",
                 "MAX_RETRIES = 3 is a bare module constant with no documented meaning.", severity="warning"),
    ],
}


class ReviewerStub:
    """The reviewer: canned findings per file of a worker chunk, plus the arm's findings under its marker."""

    def __init__(self, models: list[str], calls: list[dict[str, Any]]) -> None:
        self.models = list(models)
        self.temperature = 0.0
        self.seed = 7
        self.calls = calls

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=None):
        worker = WORKER_OPENING in system
        marked = ARM_MARKER in system
        self.calls.append({"worker": worker, "marked": marked})
        findings: list[dict[str, Any]] = []
        if worker:
            for path, rows in REVIEW_FINDINGS.items():
                if f"+++ b/{path}" in user:
                    findings += rows
                    if marked:
                        findings += ARM_FINDINGS.get(path, [])
        return InvokeResult(
            text=json.dumps({"findings": findings, "escalations": []}), model=self.models[0], backend="stub",
            input_tokens=1000, output_tokens=100, cost_usd=0.001, cost_source="stub",
        )


def _judge_rows(user: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    humans, _, rest = user.partition("### Human findings")[2].partition("### AI findings")
    return json.loads(humans), json.loads(rest.partition("## Output Format")[0])


class JudgeStub:
    """The judge: each label goes to the same-file AI finding on the nearest line, else ``none``."""

    def __init__(self, models: list[str], calls: list[str]) -> None:
        self.models = list(models)
        self.temperature = 0.0
        self.seed = 11
        self.calls = calls

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=None):
        self.calls.append(user)
        humans, ai = _judge_rows(user)
        grades = []
        for human in humans:
            same = [row for row in ai if row["file"] == human["file"] and isinstance(row["line"], int)]
            best = min(same, key=lambda row: (abs(row["line"] - human["line"]), int(row["ref"][1:])), default=None)
            grades.append({
                "human_id": human["id"],
                "grade": "none" if best is None else "full",
                "ai_ref": None if best is None else best["ref"],
            })
        return InvokeResult(
            text=json.dumps({"grades": grades}), model=self.models[0], backend="stub",
            input_tokens=900, output_tokens=60, cost_usd=0.002, cost_source="stub",
        )


def _no_network(*args, **kwargs):
    raise AssertionError("the eval end-to-end test must not touch the network")


@pytest.fixture
def rig(monkeypatch):
    """The doubles and the environment every arm shares; ``rig`` records what they saw."""
    state = SimpleNamespace(reviewer_calls=[], judge_calls=[], forges=[])

    def create_llm_client(cfg, session=None):
        models = list(cfg["llm_models"])
        if models == [JUDGE]:
            return JudgeStub(models, state.judge_calls)
        return ReviewerStub(models, state.reviewer_calls)

    def make_forge(ref):
        forge = RecordingForge(compare=PINNED_DIFF)
        state.forges.append((ref, forge))
        return forge

    monkeypatch.setenv("PRXREF_LLM_MODELS", REVIEWER)
    monkeypatch.setenv("PRXREF_MAX_WORKERS", "1")
    monkeypatch.setenv("PRXREF_GROUP_FINDINGS", "1")
    monkeypatch.setattr("prxref.llm_backends.create_llm_client", create_llm_client)
    monkeypatch.setattr("prxref.cli.make_forge", make_forge)
    monkeypatch.setattr("requests.Session.request", _no_network)
    monkeypatch.setattr(orchestrator, "_elapsed_ms", lambda t0: ELAPSED_MS)
    return state


def _bridge_locations(monkeypatch) -> None:
    """Emit each JSON finding row's ``locations`` as decided for #13, when the CLI does not yet."""
    emit = cli._finding_json

    def with_locations(f, *, drop_reason):
        row = emit(f, drop_reason=drop_reason)
        if "locations" not in row:
            pairs = getattr(f, "locations", ())
            row["locations"] = [{"file": file, "line": line} for file, line in pairs] or None
        return row

    monkeypatch.setattr(cli, "_finding_json", with_locations)


def _cli_emits_locations() -> bool:
    """Whether ``cli._finding_json`` writes a ``locations`` key at this tree (#13's JSON emission)."""
    probe = SimpleNamespace(file="a.py", line=1, severity="error", confidence=0.9, title="t", body="b")
    return "locations" in cli._finding_json(probe, drop_reason=None)


CLI_EMITS_LOCATIONS = _cli_emits_locations()


def _write_dataset(root: Path, cases: list[dict[str, Any]]) -> Path:
    root.mkdir(parents=True)
    (root / "grouped.patch").write_text(GROUPED_DIFF, encoding="utf-8")
    path = root / "cases.json"
    path.write_text(json.dumps({"version": 1, "cases": cases}, indent=2) + "\n", encoding="utf-8")
    return path


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _warnings(caplog, start: int) -> list[str]:
    return [r.getMessage() for r in caplog.records[start:] if r.levelno >= logging.WARNING]


class _Step(SimpleNamespace):
    """One ``cli.main`` call: its exit code, stdout, stderr and the WARNINGs it logged."""


def _main(argv: list[str], capsys, caplog) -> _Step:
    start = len(caplog.records)
    code = cli.main(argv)
    captured = capsys.readouterr()
    return _Step(code=code, out=captured.out, err=captured.err, warnings=_warnings(caplog, start))


def _grade(score: dict, case_id: str, human_id: str) -> dict[str, Any]:
    case = next(row for row in score["cases"] if row["case_id"] == case_id)
    return next(entry for entry in case["findings"] if entry["human_id"] == human_id)


def _row_at(record: dict, file: str, line: int) -> dict[str, Any]:
    (row,) = [row for row in record["findings"] if row["file"] == file and row["line"] == line]
    return row


@pytest.fixture
def pipeline(rig, tmp_path, monkeypatch, capsys, caplog):
    """Run both arms, score both, compare twice, then rescore ``base``; every step through ``cli.main``."""
    _bridge_locations(monkeypatch)
    directory_cases = [case_to_json(case) for case in load_cases(EVALS_DIR)]
    cases = _write_dataset(tmp_path / "dataset", [*directory_cases, PINNED_CASE, GROUPED_CASE])
    rules = tmp_path / "rules.md"
    rules.write_text(f"# Team rules\n\n{ARM_MARKER}\n", encoding="utf-8")
    out = tmp_path / "runs"
    run = ["eval", "run", "--cases", str(cases), "--out", str(out)]
    compare = ["eval", "compare", "base", "new", "--out", str(out)]

    p = SimpleNamespace(cases=cases, out=out, rules=rules, rig=rig)
    p.run_base = _main([*run, "--label", "base"], capsys, caplog)
    p.base_reviews = len(rig.reviewer_calls)
    p.run_new = _main([*run, "--label", "new", "--rules-file", str(rules)], capsys, caplog)
    p.score_base = _main(["eval", "score", "--label", "base", "--out", str(out), "--judge-model", JUDGE],
                         capsys, caplog)
    p.score_new = _main(["eval", "score", "--label", "new", "--out", str(out), "--judge-model", JUDGE],
                        capsys, caplog)
    p.scored_base = _read(out / "base" / "score.json")
    p.scored_new = _read(out / "new" / "score.json")
    p.compare_1 = _main(compare, capsys, caplog)
    p.compare_2 = _main(compare, capsys, caplog)
    p.judge_calls_before_rescore = len(rig.judge_calls)
    p.rescore_base = _main(["eval", "score", "--label", "base", "--out", str(out), "--judge-model", JUDGE],
                           capsys, caplog)
    p.judge_calls_after_rescore = len(rig.judge_calls)
    p.rescored_base = _read(out / "base" / "score.json")
    p.steps = [p.run_base, p.run_new, p.score_base, p.score_new, p.compare_1, p.compare_2, p.rescore_base]
    return p


RUN_KEYS = ["version", "label", "cases_path", "created_at", "case_ids", "prompts", "sampling", "review_rules",
            "scoped_rules", "config"]
SCORE_KEYS = ["version", "label", "run", "judge", "failed", "metrics", "cases"]
JUDGE_KEYS = ["model", "sampling", "prompt_version", "prompt_sha256", "self_judged", "cost_usd", "cost_estimated",
              "llm_calls", "cached", "errors"]
METRIC_KEYS = ["case_count", "recall", "recall_by_severity", "recall_by_category", "recall_accepted",
               "unmatched_ai", "severity_agreement", "chunks_failed", "elapsed_ms", "review_cost", "judge_cost"]
CASE_IDS = ["case-001-mcp-protocol-upgrade", "case-002-session-token-logging", "case-003-config-schema-pin",
            "case-pinned", "case-grouped"]
LIVE_DESCRIPTION = (
    "replay shows the PR's CURRENT title and description: the github forge cannot read a pull "
    "request's description history"
)
COMPARE_GOLDEN = """\
# prxref eval compare

- A: base
- B: new

## Metrics

| Metric | A | B | Change |
|---|---:|---:|---:|
| Cases | 5 | 5 | 0 |
| Recall (micro) | 38.5% (5 of 13) | 46.2% (6 of 13) | +7.7 pp |
| Judge errors | 0 | 0 | 0 |
| Recall, severity `error` | 50.0% (3 of 6) | 50.0% (3 of 6) | 0.0 pp |
| Recall, severity `minor` | 0.0% (0 of 1) | 100.0% (1 of 1) | +100.0 pp |
| Recall, severity `spec` | 20.0% (1 of 5) | 20.0% (1 of 5) | 0.0 pp |
| Recall, severity `warning` | 100.0% (1 of 1) | 100.0% (1 of 1) | 0.0 pp |
| Recall, category `generic` | 0.0% (0 of 3) | 0.0% (0 of 3) | 0.0 pp |
| Recall, category `reliability` | 100.0% (1 of 1) | 100.0% (1 of 1) | 0.0 pp |
| Recall, category `security` | 100.0% (3 of 3) | 100.0% (3 of 3) | 0.0 pp |
| Recall, category `spec` | 20.0% (1 of 5) | 20.0% (1 of 5) | 0.0 pp |
| Recall, category `style` | 0.0% (0 of 1) | 100.0% (1 of 1) | +100.0 pp |
| Recall, accepted labels | 100.0% (1 of 1) | 100.0% (1 of 1) | 0.0 pp |
| Unmatched AI per PR | 0.00 (0 of 3) | 0.00 (0 of 4) | 0.00 |
| Severity agreement | 100.0% (5 of 5) | 100.0% (6 of 6) | 0.0 pp |
| Chunks failed | 0 | 0 | 0 |
| Elapsed | 7.5 s | 7.5 s | 0.0 s |
| Review cost | $0.0100 | $0.0100 | $0.0000 |
| Judge cost | $0.0040 | $0.0040 | $0.0000 |

## Changed labels

| Case | Label | Location | A | B |
|---|---|---|---|---|
| case-pinned | P2 | src/widget/retry.py:1 | none (0) | full (1) |

## Only in one run

None.
"""


class TestTheDataset:
    def test_the_directory_cases_reach_the_run_exactly_as_the_directory_loader_reads_them(self, tmp_path):
        directory = load_cases(EVALS_DIR)
        cases = _write_dataset(tmp_path / "dataset", [*map(case_to_json, directory), PINNED_CASE, GROUPED_CASE])
        loaded = load_cases(cases)
        assert [case.id for case in loaded] == CASE_IDS
        assert loaded[:3] == directory
        assert all(case.diff_file and case.context_file and case.spec for case in directory)


class TestRunScoreCompare:
    def test_every_step_exits_0(self, pipeline):
        assert [step.code for step in pipeline.steps] == [0] * 7
        assert [step.err for step in pipeline.steps] == [""] * 7

    def test_run_writes_the_documented_run_json_and_one_record_per_case(self, pipeline):
        for label in ("base", "new"):
            run_dir = pipeline.out / label
            run = _read(run_dir / "run.json")
            assert list(run) == RUN_KEYS
            assert run["label"] == label and run["case_ids"] == CASE_IDS
            assert run["cases_path"] == str(pipeline.cases)
            assert run["sampling"] == {"temperature": 0.0, "seed": 7, "models": [REVIEWER]}
            assert run["config"]["group_findings"] is True
            for case_id in CASE_IDS:
                assert (run_dir / "cases" / case_id / "record.json").is_file()
                assert not (run_dir / "cases" / case_id / "error.json").exists()
        for step in (pipeline.run_base, pipeline.run_new):
            assert [line.split(":")[0] for line in step.out.splitlines()[:-1]] == CASE_IDS
            assert step.out.splitlines()[-1].startswith("run directory: ")
        assert _read(pipeline.out / "base" / "run.json")["review_rules"] is None
        assert _read(pipeline.out / "new" / "run.json")["review_rules"]["path"] == str(pipeline.rules)

    def test_the_arms_differ_only_in_the_rules_file(self, pipeline):
        calls = pipeline.rig.reviewer_calls
        base, new = calls[:pipeline.base_reviews], calls[pipeline.base_reviews:]
        assert len(base) == len(new) and any(call["worker"] for call in base)
        assert not any(call["marked"] for call in base)
        assert all(call["marked"] for call in new)

    def test_score_writes_the_documented_score_json_for_each_arm(self, pipeline):
        for label, score in (("base", pipeline.scored_base), ("new", pipeline.scored_new)):
            assert list(score) == SCORE_KEYS
            assert score["label"] == label
            assert list(score["judge"]) == JUDGE_KEYS
            assert score["judge"]["model"] == JUDGE and score["judge"]["self_judged"] is False
            assert score["judge"]["llm_calls"] == 2 and score["judge"]["cached"] == 0
            assert score["judge"]["errors"] == [] and score["failed"] == []
            assert list(score["metrics"]) == METRIC_KEYS
            assert [row["case_id"] for row in score["cases"]] == sorted(CASE_IDS)
            assert (pipeline.out / label / "score.md").is_file()

    def test_the_pinned_pr_url_case_is_a_live_blind_replay_of_its_range(self, pipeline):
        for label in ("base", "new"):
            record = _read(pipeline.out / label / "cases" / "case-pinned" / "record.json")
            assert record["replay"] == {
                "base_sha": PIN_BASE, "head_sha": PIN_HEAD, "threads": "hidden", "diff_file": None,
                "description": "live", "as_of": None, "as_of_source": None,
            }
            assert record["posted"] is False
        assert len(pipeline.rig.forges) == 2
        for ref, forge in pipeline.rig.forges:
            assert ref.url == PR_URL
            assert [(b, h) for _ref, b, h in forge.compare_args] == [(PIN_BASE, PIN_HEAD)]
            assert "get_diff" not in forge.calls and "list_threads" not in forge.calls
            assert forge.summaries == [] and forge.inline_batches == [] and forge.pruned == 0

    def test_the_only_warning_of_each_run_is_the_pinned_cases_live_description(self, pipeline):
        assert pipeline.run_base.warnings == [LIVE_DESCRIPTION]
        assert pipeline.run_new.warnings == [LIVE_DESCRIPTION]
        assert pipeline.score_base.warnings == pipeline.score_new.warnings == []

    def test_compare_twice_is_byte_identical_names_the_changed_label_and_warns_nothing(self, pipeline):
        assert pipeline.compare_1.out == pipeline.compare_2.out
        assert pipeline.compare_1.out == COMPARE_GOLDEN
        assert "| case-pinned | P2 | src/widget/retry.py:1 | none (0) | full (1) |" in pipeline.compare_1.out
        assert pipeline.compare_1.warnings == pipeline.compare_2.warnings == []

    def test_a_rescore_with_the_same_judge_model_is_served_from_the_cache(self, pipeline):
        assert (pipeline.judge_calls_before_rescore, pipeline.judge_calls_after_rescore) == (4, 4)
        judge = pipeline.rescored_base["judge"]
        assert (judge["llm_calls"], judge["cached"], judge["cost_usd"]) == (0, 2, 0.0)
        assert pipeline.scored_base["judge"]["cost_usd"] == pytest.approx(0.004)

        def unpriced(score):
            return [{k: v for k, v in row.items() if k != "judge_cost_usd"} for row in score["cases"]]

        assert unpriced(pipeline.rescored_base) == unpriced(pipeline.scored_base)
        assert pipeline.rescored_base["metrics"]["recall"] == pipeline.scored_base["metrics"]["recall"]
        assert pipeline.rescore_base.out == pipeline.score_base.out

    def test_a_grouped_finding_is_credited_at_each_location_through_both_tiers(self, pipeline):
        for label, score in (("base", pipeline.scored_base), ("new", pipeline.scored_new)):
            record = _read(pipeline.out / label / "cases" / "case-grouped" / "record.json")
            rep = _row_at(record, GROUPED_PATH, REP_LINE)
            member = _row_at(record, GROUPED_PATH, MEMBER_LINE)
            assert rep["drop_reason"] is None
            assert rep["locations"] == [{"file": GROUPED_PATH, "line": MEMBER_LINE}]
            assert member["drop_reason"] == f"grouped into {GROUPED_PATH}:{REP_LINE}"
            index = record["findings"].index(rep)
            got = {human_id: _grade(score, "case-grouped", human_id) for human_id in ("M1", "M2", "J1")}
            assert {h: (g["grade"], g["ai_ref"], g["ai_line"], g["method"]) for h, g in got.items()} == {
                "M1": ("full", index, REP_LINE, "must_match"),
                "M2": ("full", index, REP_LINE, "must_match"),
                "J1": ("full", index, MEMBER_LINE, "judge"),
            }


@pytest.fixture
def unbridged(rig, tmp_path, monkeypatch, capsys, caplog):
    """Run and score ``case-grouped`` alone, as the CLI emits it at this tree, keeping each JSON row's finding."""
    seen: list[Any] = []
    emit = cli._finding_json

    def spy(f, *, drop_reason):
        seen.append(f)
        return emit(f, drop_reason=drop_reason)

    monkeypatch.setattr(cli, "_finding_json", spy)
    cases = _write_dataset(tmp_path / "dataset", [GROUPED_CASE])
    out = tmp_path / "runs"
    run = _main(["eval", "run", "--cases", str(cases), "--label", "L", "--out", str(out)], capsys, caplog)
    score = _main(["eval", "score", "--label", "L", "--out", str(out), "--judge-model", JUDGE], capsys, caplog)
    assert (run.code, score.code) == (0, 0)
    return SimpleNamespace(
        findings=seen,
        record=_read(out / "L" / "cases" / "case-grouped" / "record.json"),
        score=_read(out / "L" / "score.json"),
    )


class TestGroupedWithoutTheBridge:
    def test_the_review_groups_the_two_findings_and_keeps_the_member_location(self, unbridged):
        rep = _row_at(unbridged.record, GROUPED_PATH, REP_LINE)
        member = _row_at(unbridged.record, GROUPED_PATH, MEMBER_LINE)
        assert rep["drop_reason"] is None
        assert rep["body"].endswith(f"Also at: `{GROUPED_PATH}:{MEMBER_LINE}`")
        assert member["drop_reason"] == f"grouped into {GROUPED_PATH}:{REP_LINE}"
        (finding,) = [f for f in unbridged.findings if f.line == REP_LINE]
        assert finding.locations == ((GROUPED_PATH, MEMBER_LINE),)
        for human_id in ("M1", "M2"):
            assert _grade(unbridged.score, "case-grouped", human_id)["grade"] == "full"

    @pytest.mark.xfail(
        not CLI_EMITS_LOCATIONS, strict=True, raises=KeyError,
        reason="#13 JSON emission (Q13-F) pending: cli._finding_json does not emit `locations`, so the "
               "judge sees the grouped finding at its representative's line only",
    )
    def test_the_record_itself_credits_the_member_location_in_the_judge_tier(self, unbridged):
        rep = _row_at(unbridged.record, GROUPED_PATH, REP_LINE)
        assert rep["locations"] == [{"file": GROUPED_PATH, "line": MEMBER_LINE}]
        grade = _grade(unbridged.score, "case-grouped", "J1")
        assert (grade["grade"], grade["ai_line"]) == ("full", MEMBER_LINE)
