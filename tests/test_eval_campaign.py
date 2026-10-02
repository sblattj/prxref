"""``prxref eval campaign`` (#81): arms x repeats of ``eval run``, parallel, resumable, scored.

Most tests call :func:`prxref.eval_campaign.run_campaign` with an injected
runner. :class:`FakeRunner` parses the ``eval run`` argv the campaign built
and runs the REAL :func:`prxref.evals.eval_run` in process with a scripted
review (``tests.test_eval_run.FakeReview``), so every unit directory, record
and ``run.json`` is the real shape, and the real merge and ``eval score``
follow. Configuration errors go through ``cli.main`` so the exit code is the
CLI's. :class:`TestEndToEnd` runs real ``eval run`` subprocesses against a
local OpenAI-compatible stub server.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

import prxref
from prxref import cli, eval_campaign, evals
from prxref.eval_cases import load_cases
from prxref.eval_rules_mine import assign_folds
from prxref.llm import ConfigError, InvokeResult
from tests import test_eval_run as run_helpers

DIFF = run_helpers.DIFF
ISO_Z = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
RATE_LIMITED = RuntimeError("HTTP 429 Too Many Requests")
PROGRESS_PASS_KEYS = [
    "arm", "repeat", "state", "units_total", "units_ok", "units_failed", "units_rate_limited", "attempt",
    "started_at", "finished_at", "log", "error",
]
CAMPAIGN_KEYS = [
    "version", "created_at", "cases_path", "cases_sha256", "repeats", "folds", "severity", "judge_model",
    "prxref", "arms",
]


def _label(case_id: str) -> dict[str, Any]:
    return {"id": "h1", "file": "src/app.py", "line": 3, "severity": "error", "must_match": "total",
            "text": f"LABEL-TEXT-{case_id}"}


def _dataset(tmp_path: Path, ids=("a", "b", "c")) -> Path:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "change.patch").write_text(DIFF, encoding="utf-8")
    cases = [{"id": i, "diff_file": "change.patch", "expected": [_label(i)]} for i in ids]
    path = data / "cases.json"
    path.write_text(json.dumps({"version": 1, "cases": cases}), encoding="utf-8")
    return path


ARMS = """
[[arm]]
name = "no-rules"
rules_file = ""
scoped_rules = []

[[arm]]
name = "rules"
rules_file = "rules.md"
"""


def _arms(tmp_path: Path, text: str = ARMS) -> Path:
    conf = tmp_path / "conf"
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "rules.md").write_text("# Team rules\n\n- Name every constant.\n", encoding="utf-8")
    path = conf / "arms.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _argv(tmp_path: Path, *extra: str, cases=None, arms=None, out=None) -> list[str]:
    return [
        "eval", "campaign",
        "--cases", str(cases or _dataset(tmp_path)),
        "--arms", str(arms or _arms(tmp_path)),
        "--out", str(out or tmp_path / "camp"),
        *extra,
    ]


def _args(tmp_path: Path, *extra: str, **kwargs):
    return cli._build_parser().parse_args(_argv(tmp_path, *extra, **kwargs))


def _ok() -> dict:
    return run_helpers._result("Request-Changes", active=[run_helpers._finding("total is printed")])


class FakeRunner:
    """Runs the real ``eval run`` in process for each argv; reviews answer per case id and call number.

    ``script[(case_id, n)]`` is the outcome of that case's ``n``-th review
    (1-based) across the whole campaign of one arm; anything unscripted is a
    matched finding. ``rc`` overrides the return code per unit label;
    ``version`` rewrites ``run.json``'s ``prxref_version``. Concurrency is
    measured over the whole runner and per pass directory.
    """

    def __init__(self, script=None, *, delay: float = 0.0, rc=None, version: str | None = None,
                 before=None) -> None:
        self.script = script or {}
        self.delay = delay
        self.rc = rc or {}
        self.version = version
        self.before = before
        self.calls: list[dict[str, Any]] = []
        self.reviews: dict[tuple[str, str], int] = {}
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.pass_active: dict[str, int] = {}
        self.max_pass_active: dict[str, int] = {}

    def __call__(self, cmd, log_path, env) -> int:
        cmd = list(cmd)
        parsed = cli._build_parser().parse_args(cmd[cmd.index("eval"):])
        pass_key = parsed.out
        with self.lock:
            self.calls.append({"cmd": cmd, "args": parsed, "env": dict(env), "log": Path(log_path),
                               "case_ids": [c.id for c in load_cases(parsed.cases)]})
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.pass_active[pass_key] = self.pass_active.get(pass_key, 0) + 1
            self.max_pass_active[pass_key] = max(self.max_pass_active.get(pass_key, 0),
                                                 self.pass_active[pass_key])
        try:
            if self.before is not None:
                self.before(parsed)
            if self.delay:
                time.sleep(self.delay)
            arm = Path(parsed.out).parent.name
            evals.eval_run(parsed, run_review=self._review(arm), build_record=cli._build_json_result)
            if self.version is not None:
                run_path = Path(parsed.out) / parsed.label / "run.json"
                run = json.loads(run_path.read_text(encoding="utf-8"))
                run["prxref_version"] = self.version
                run_path.write_text(json.dumps(run), encoding="utf-8")
            return self.rc.get(parsed.label, 0)
        finally:
            with self.lock:
                self.active -= 1
                self.pass_active[pass_key] -= 1

    def _review(self, arm: str):
        def review(url, **kwargs):
            case_id = Path(kwargs["trace_dir"]).parent.name
            with self.lock:
                n = self.reviews[(arm, case_id)] = self.reviews.get((arm, case_id), 0) + 1
            outcome = self.script.get((case_id, n), _ok())
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return review


class Sleeps(list):
    def __call__(self, seconds: float) -> None:
        self.append(seconds)


def _run(args, runner, **kwargs) -> tuple[int, str, Sleeps]:
    out = io.StringIO()
    sleeps = Sleeps()
    code = eval_campaign.run_campaign(args, runner=runner, sleep=sleeps, stdout=out, **kwargs)
    return code, out.getvalue(), sleeps


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _passes(out: Path) -> dict[tuple[str, int], dict]:
    return {(p["arm"], p["repeat"]): p for p in _read(out / "progress.json")["passes"]}


class TestFanOut:
    def test_arms_times_repeats_times_shards_and_every_pass_scored(self, tmp_path):
        runner = FakeRunner()
        args = _args(tmp_path, "--repeats", "2", "--case-jobs", "2")
        code, stdout, _ = _run(args, runner)
        out = tmp_path / "camp"
        assert code == 0
        assert len(runner.calls) == 2 * 2 * 2
        labels = sorted((Path(c["args"].out).relative_to(out).as_posix(), c["args"].label) for c in runner.calls)
        assert labels == sorted(
            (f"units/{arm}/r{k}", f"f0-s{i}") for arm in ("no-rules", "rules") for k in (1, 2) for i in (0, 1)
        )
        shards = {c["args"].label: c["case_ids"] for c in runner.calls}
        assert shards == {"f0-s0": ["a", "c"], "f0-s1": ["b"]}
        for arm in ("no-rules", "rules"):
            for k in (1, 2):
                run_dir = out / "runs" / arm / f"r{k}"
                score = _read(run_dir / "score.json")
                assert score["label"] == f"r{k}"
                assert score["metrics"]["recall"]["recall"] == 1.0
                assert _read(run_dir / "run.json")["case_ids"] == ["a", "b", "c"]
        assert {p["state"] for p in _passes(out).values()} == {"scored"}
        assert "no-rules r1: running (0/3 units)" in stdout
        assert "rules r2: scored (3/3 units)" in stdout
        assert "| rules | 2/2 | 100.0%; 100.0%, 100.0% |" in stdout
        assert f"campaign: {out}" in stdout
        assert f"prxref eval verdict --baseline {out} --candidate <other campaign>" in stdout

    def test_each_arms_rules_flags_reach_its_units(self, tmp_path):
        runner = FakeRunner()
        _run(_args(tmp_path, "--repeats", "1"), runner)
        by_arm = {Path(c["args"].out).parent.name: c["args"] for c in runner.calls}
        assert by_arm["no-rules"].rules_file == ""
        assert by_arm["no-rules"].scoped_rules == [""]
        assert by_arm["rules"].rules_file == str(tmp_path / "conf" / "rules.md")
        assert by_arm["rules"].scoped_rules is None
        assert all(a.prompts_dir is None and a.resume is False for a in by_arm.values())

    def test_without_prxref_the_unit_runs_this_package_with_this_interpreter(self, tmp_path):
        runner = FakeRunner()
        _run(_args(tmp_path, "--repeats", "1"), runner)
        call = runner.calls[0]
        assert call["cmd"][:5] == [sys.executable, "-m", "prxref.cli", "eval", "run"]
        src = str(Path(prxref.__file__).resolve().parents[1])
        assert call["env"]["PYTHONPATH"].split(os.pathsep)[0] == src

    def test_scoring_output_goes_to_the_pass_log_and_every_attempt_has_a_header(self, tmp_path):
        _, stdout, _ = _run(_args(tmp_path, "--repeats", "1"), FakeRunner())
        log = (tmp_path / "camp" / "logs" / "rules-r1.log").read_text(encoding="utf-8")
        assert re.search(r"^=== \S+Z rules r1 attempt 1 unit f0-s0: .* eval run ", log, re.M)
        assert "Recall (micro)" in log and "Recall (micro)" not in stdout

    def test_concurrency_is_bounded_by_jobs_and_case_jobs(self, tmp_path):
        arms = "\n".join(f'[[arm]]\nname = "arm{i}"\nrules_file = ""\n' for i in range(4))
        runner = FakeRunner(delay=0.15)
        args = _args(tmp_path, "--repeats", "1", "--jobs", "2", "--case-jobs", "2",
                     cases=_dataset(tmp_path, ids=("a", "b", "c", "d")), arms=_arms(tmp_path, arms))
        _run(args, runner)
        assert len(runner.calls) == 8
        assert runner.max_active <= 4
        assert max(runner.max_pass_active.values()) <= 2
        assert runner.max_active >= 2

    def test_jobs_1_and_case_jobs_1_never_overlap(self, tmp_path):
        runner = FakeRunner(delay=0.02)
        _run(_args(tmp_path, "--repeats", "2"), runner)
        assert len(runner.calls) == 4
        assert runner.max_active == 1


class TestRetries:
    def test_only_rate_limited_and_error_units_are_rerun_after_a_rate_limit_pause(self, tmp_path):
        runner = FakeRunner({("b", 1): RATE_LIMITED, ("c", 1): RuntimeError("boom")})
        args = _args(tmp_path, "--repeats", "1", "--case-jobs", "3",
                     arms=_arms(tmp_path, '[[arm]]\nname = "only"\nrules_file = ""\n'))
        _, stdout, sleeps = _run(args, runner)
        assert runner.reviews == {("only", "a"): 1, ("only", "b"): 2, ("only", "c"): 2}
        assert sleeps == [30]
        assert sorted(c["args"].label for c in runner.calls[3:]) == ["f0-s1", "f0-s2"]
        assert all(c["args"].resume for c in runner.calls[3:])
        entry = _passes(tmp_path / "camp")[("only", 1)]
        assert (entry["state"], entry["attempt"], entry["units_ok"]) == ("scored", 2, 3)
        assert _read(tmp_path / "camp" / "runs" / "only" / "r1" / "score.json")["failed"] == []

    def test_an_error_alone_is_retried_without_a_pause(self, tmp_path):
        runner = FakeRunner({("a", 1): RuntimeError("boom")})
        args = _args(tmp_path, "--repeats", "1", arms=_arms(tmp_path, '[[arm]]\nname = "only"\n'))
        _, _, sleeps = _run(args, runner)
        assert sleeps == []
        assert runner.reviews[("only", "a")] == 2 and runner.reviews[("only", "b")] == 1

    def test_attempts_are_capped_and_the_unit_counts_as_missed(self, tmp_path):
        script = {("b", n): RATE_LIMITED for n in range(1, 10)}
        runner = FakeRunner(script)
        args = _args(tmp_path, "--repeats", "1", "--max-attempts", "3",
                     arms=_arms(tmp_path, '[[arm]]\nname = "only"\n'))
        _, stdout, sleeps = _run(args, runner)
        assert runner.reviews[("only", "b")] == 3
        assert sleeps == [30, 60]
        entry = _passes(tmp_path / "camp")[("only", 1)]
        assert (entry["state"], entry["attempt"], entry["units_ok"], entry["units_rate_limited"]) == (
            "scored", 3, 2, 1)
        score = _read(tmp_path / "camp" / "runs" / "only" / "r1" / "score.json")
        assert [f["case_id"] for f in score["failed"]] == ["b"]
        assert score["metrics"]["recall"]["scored"] == 3

    def test_the_pause_is_capped_at_300_seconds(self, tmp_path):
        script = {("a", n): RATE_LIMITED for n in range(1, 20)}
        args = _args(tmp_path, "--repeats", "1", "--max-attempts", "13",
                     cases=_dataset(tmp_path, ids=("a",)), arms=_arms(tmp_path, '[[arm]]\nname = "only"\n'))
        _, _, sleeps = _run(args, FakeRunner(script))
        assert sleeps == [min(300, 30 * n) for n in range(1, 13)]

    def test_a_rate_limited_record_left_after_the_last_attempt_is_not_counted(self, tmp_path):
        runner = FakeRunner()
        original = runner.__call__

        def call(cmd, log_path, env):
            code = original(cmd, log_path, env)
            parsed = cli._build_parser().parse_args(list(cmd)[list(cmd).index("eval"):])
            trace = Path(parsed.out) / parsed.label / "cases" / "a" / "trace"
            if trace.parent.is_dir():
                trace.mkdir(exist_ok=True)
                (trace / "worker.meta.json").write_text(json.dumps({"fallback": "429 quota"}), encoding="utf-8")
            return code

        args = _args(tmp_path, "--repeats", "1", "--max-attempts", "2",
                     arms=_arms(tmp_path, '[[arm]]\nname = "only"\n'))
        _, _, sleeps = _run(args, call)
        case_dir = tmp_path / "camp" / "runs" / "only" / "r1" / "cases" / "a"
        assert (case_dir / "record.rejected.json").is_file() and not (case_dir / "record.json").exists()
        assert "rate limited after 2 attempt(s)" in _read(case_dir / "error.json")["error"]
        assert sleeps == [30]

    def test_exit_2_from_a_unit_fails_the_pass_at_once_and_the_campaign_goes_on(self, tmp_path):
        runner = FakeRunner(rc={"f0-s0": 2})
        args = _args(tmp_path, "--repeats", "1")
        code, stdout, _ = _run(args, runner)
        assert code == 0
        assert len(runner.calls) == 2
        passes = _passes(tmp_path / "camp")
        assert {p["state"] for p in passes.values()} == {"failed"}
        assert "eval run exited 2" in passes[("rules", 1)]["error"]
        assert re.search(r"^rules r1: failed \(\d/3 units\): eval run exited 2", stdout, re.M)


class TestResume:
    def test_scored_passes_are_skipped(self, tmp_path):
        _run(_args(tmp_path, "--repeats", "2"), FakeRunner())
        again = FakeRunner()
        code, stdout, _ = _run(_args(tmp_path, "--repeats", "2", "--resume"), again)
        assert code == 0 and again.calls == []
        assert {p["state"] for p in _passes(tmp_path / "camp").values()} == {"scored"}
        assert "| rules | 2/2 |" in stdout

    def test_a_failed_pass_restarts_at_the_unit_check_and_keeps_ok_units(self, tmp_path):
        first = FakeRunner({("b", 1): RuntimeError("boom")}, rc={"f0-s0": 2})
        args = ["--repeats", "1", "--case-jobs", "1"]
        arms = _arms(tmp_path, '[[arm]]\nname = "only"\n')
        _run(_args(tmp_path, *args, arms=arms), first)
        assert _passes(tmp_path / "camp")[("only", 1)]["state"] == "failed"
        again = FakeRunner()
        _run(_args(tmp_path, *args, "--resume", arms=arms), again)
        assert [c["args"].resume for c in again.calls] == [True]
        assert again.reviews == {("only", "b"): 1}
        assert _passes(tmp_path / "camp")[("only", 1)]["state"] == "scored"

    def test_a_pass_whose_units_all_finished_is_merged_and_scored_without_a_run(self, tmp_path):
        arms = _arms(tmp_path, '[[arm]]\nname = "only"\n')
        _run(_args(tmp_path, "--repeats", "1", arms=arms), FakeRunner(rc={"f0-s0": 2}))
        again = FakeRunner()
        _run(_args(tmp_path, "--repeats", "1", "--resume", arms=arms), again)
        assert again.calls == []
        assert _passes(tmp_path / "camp")[("only", 1)]["state"] == "scored"

    def test_resume_on_an_empty_out_starts_fresh(self, tmp_path):
        code, _, _ = _run(_args(tmp_path, "--repeats", "1", "--resume"), FakeRunner())
        assert code == 0 and (tmp_path / "camp" / "campaign.json").is_file()


class TestProgressAndCampaignJson:
    def test_progress_json_shape(self, tmp_path):
        _run(_args(tmp_path, "--repeats", "2"), FakeRunner())
        progress = _read(tmp_path / "camp" / "progress.json")
        assert list(progress) == ["version", "started_at", "updated_at", "passes"]
        assert progress["version"] == 1
        assert ISO_Z.match(progress["started_at"]) and ISO_Z.match(progress["updated_at"])
        assert [(p["arm"], p["repeat"]) for p in progress["passes"]] == [
            ("no-rules", 1), ("no-rules", 2), ("rules", 1), ("rules", 2)]
        for entry in progress["passes"]:
            assert list(entry) == PROGRESS_PASS_KEYS
            assert entry["state"] in eval_campaign.PASS_STATES
            assert entry["log"] == f"logs/{entry['arm']}-r{entry['repeat']}.log"
            assert (tmp_path / "camp" / entry["log"]).is_file()
            assert ISO_Z.match(entry["started_at"]) and ISO_Z.match(entry["finished_at"])
            assert (entry["units_total"], entry["units_ok"], entry["units_failed"], entry["error"]) == (3, 3, 0, None)

    def test_progress_json_is_written_atomically_on_every_change(self, tmp_path, monkeypatch):
        seen: list[dict] = []
        real = os.replace

        def spy(src, dst):
            real(src, dst)
            if Path(dst).name == "progress.json":
                seen.append(json.loads(Path(dst).read_text(encoding="utf-8")))

        monkeypatch.setattr(eval_campaign.os, "replace", spy)
        _run(_args(tmp_path, "--repeats", "1"), FakeRunner())
        states = [tuple(p["state"] for p in snap["passes"]) for snap in seen]
        assert states[0] == ("pending", "pending")
        assert ("running", "pending") in states or ("pending", "running") in states
        assert any("scoring" in s for s in states)
        assert states[-1] == ("scored", "scored")
        assert not list((tmp_path / "camp").glob("*.tmp")) and not list((tmp_path / "camp").glob(".*.tmp"))

    def test_campaign_json_records_the_inputs(self, tmp_path):
        cases = _dataset(tmp_path)
        _run(_args(tmp_path, "--repeats", "2", "--severity", "error", cases=cases), FakeRunner())
        campaign = _read(tmp_path / "camp" / "campaign.json")
        assert list(campaign) == CAMPAIGN_KEYS
        assert campaign["version"] == 1 and ISO_Z.match(campaign["created_at"])
        assert campaign["cases_path"] == str(cases)
        assert campaign["cases_sha256"] == eval_campaign.cases_sha256(load_cases(cases))
        assert (campaign["repeats"], campaign["folds"], campaign["severity"], campaign["judge_model"]) == (
            2, 1, "error", None)
        assert campaign["prxref"] == {"requested": None, "version": prxref.__version__, "python": None}
        assert campaign["arms"] == [
            {"name": "no-rules", "rules_file": "", "scoped_rules": [], "prompts_dir": None, "mine_rules": None},
            {"name": "rules", "rules_file": str(tmp_path / "conf" / "rules.md"), "scoped_rules": None,
             "prompts_dir": None, "mine_rules": None},
        ]

    def test_severity_adds_a_column_to_the_summary(self, tmp_path):
        _, stdout, _ = _run(_args(tmp_path, "--repeats", "1", "--severity", "error"), FakeRunner())
        assert "| Arm | Scored | Recall (mean; per pass) | error recall (mean; per pass) |" in stdout


class StubMiner:
    def __init__(self, fail: bool = False) -> None:
        self.models = ["miner-m"]
        self.temperature = 0.0
        self.seed = 3
        self.fail = fail
        self.prompts: list[str] = []

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=None):
        self.prompts.append(user)
        if self.fail:
            raise RuntimeError("miner down")
        return InvokeResult(text=f"# Mined rules\n\n- Rule {len(self.prompts)}.\n", model="miner-m",
                            backend="stub", input_tokens=10, output_tokens=5, cost_usd=0.0, cost_source="stub")


MINE_ARMS = """
[[arm]]
name = "base"
rules_file = ""

[[arm]]
name = "mined"
[arm.mine_rules]
"""


class TestMining:
    @pytest.fixture
    def miner(self, monkeypatch):
        stub = StubMiner()
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg, session=None: stub)
        return stub

    def test_rules_are_mined_once_per_arm_and_each_fold_gets_its_own(self, tmp_path, miner):
        ids = ("a", "b", "c", "d")
        cases = _dataset(tmp_path, ids=ids)
        runner = FakeRunner()
        args = _args(tmp_path, "--repeats", "2", "--folds", "2", "--judge-model", "judge-m",
                     cases=cases, arms=_arms(tmp_path, MINE_ARMS))
        _run(args, runner)
        out = tmp_path / "camp"
        assert len(miner.prompts) == 2
        folds = assign_folds(load_cases(cases), 2)
        for j in (0, 1):
            assert (out / "rules" / "mined" / f"f{j}.md").read_text(encoding="utf-8").startswith("# Mined rules")
        mined_calls = [c for c in runner.calls if Path(c["args"].out).parent.name == "mined"]
        assert len(mined_calls) == 2 * 2
        for call in mined_calls:
            fold = int(call["args"].label[1])
            assert call["args"].rules_file == str(out / "rules" / "mined" / f"f{fold}.md")
            assert {folds[cid] for cid in call["case_ids"]} == {fold}
        for j, prompt in enumerate(miner.prompts):
            for cid in ids:
                assert (f"LABEL-TEXT-{cid}" in prompt) == (folds[cid] != j)
        assert {p["state"] for p in _passes(out).values()} == {"scored"}
        merged = _read(out / "runs" / "mined" / "r1" / "run.json")
        assert merged["case_ids"] == list(ids)
        assert _read(out / "campaign.json")["arms"][1]["mine_rules"] == {"judge_model": "judge-m"}

    def test_mined_files_are_reused_on_resume(self, tmp_path, miner):
        argv = ["--repeats", "1", "--folds", "2", "--judge-model", "judge-m"]
        cases, arms = _dataset(tmp_path, ids=("a", "b")), _arms(tmp_path, MINE_ARMS)
        _run(_args(tmp_path, *argv, cases=cases, arms=arms), FakeRunner(rc={"f1-s0": 2}))
        assert len(miner.prompts) == 2
        _run(_args(tmp_path, *argv, "--resume", cases=cases, arms=arms), FakeRunner())
        assert len(miner.prompts) == 2
        assert _passes(tmp_path / "camp")[("mined", 1)]["state"] == "scored"

    def test_a_mining_failure_fails_that_arm_only(self, tmp_path, miner):
        miner.fail = True
        runner = FakeRunner()
        args = _args(tmp_path, "--repeats", "2", "--folds", "2", "--judge-model", "judge-m",
                     arms=_arms(tmp_path, MINE_ARMS))
        code, stdout, _ = _run(args, runner)
        assert code == 0
        passes = _passes(tmp_path / "camp")
        assert passes[("base", 1)]["state"] == "scored"
        assert passes[("mined", 2)]["state"] == "failed"
        assert passes[("mined", 2)]["error"].startswith("rules mining failed: RuntimeError: miner down")
        assert all(Path(c["args"].out).parent.name == "base" for c in runner.calls)
        assert "mined r1: failed (0/3 units): rules mining failed" in stdout


class FakeEnv:
    def __init__(self, version: str,
                 help_text: str = "--cases --label --out --resume --rules-file --scoped-rules") -> None:
        self.version = version
        self.help_text = help_text
        self.built: list[tuple[str, Path]] = []

    def __call__(self, requested: str, env_dir: Path) -> eval_campaign.PrxrefEnv:
        self.built.append((requested, env_dir))
        return eval_campaign.PrxrefEnv(requested, self.version, "/envs/py/bin/python", ("/envs/py/bin/prxref",),
                                       self.help_text, {"PYTHONPATH": None})


class TestPinnedPrxref:
    def test_the_pinned_env_runs_every_unit_and_is_recorded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
        env = FakeEnv("0.29.0")
        runner = FakeRunner(version="0.29.0")
        _run(_args(tmp_path, "--repeats", "1", "--prxref", "0.29.0"), runner, env_builder=env)
        out = tmp_path / "camp"
        assert env.built == [("0.29.0", out / "envs" / "0.29.0")]
        assert all(c["cmd"][:3] == ["/envs/py/bin/prxref", "eval", "run"] for c in runner.calls)
        assert all("PYTHONPATH" not in c["env"] for c in runner.calls)
        assert _read(out / "campaign.json")["prxref"] == {
            "requested": "0.29.0", "version": "0.29.0", "python": "/envs/py/bin/python"}
        assert {p["state"] for p in _passes(out).values()} == {"scored"}

    def test_a_unit_written_by_another_version_fails_its_pass(self, tmp_path):
        runner = FakeRunner()
        _run(_args(tmp_path, "--repeats", "1", "--prxref", "0.29.0"), runner, env_builder=FakeEnv("0.29.0"))
        entry = _passes(tmp_path / "camp")[("rules", 1)]
        assert entry["state"] == "failed"
        assert f"written by prxref {prxref.__version__}, expected 0.29.0" in entry["error"]

    def test_a_version_mismatch_is_a_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match=r"^--prxref: asked for '0.29.0'.*'0.29.1'"):
            _run(_args(tmp_path, "--prxref", "0.29.0"), FakeRunner(), env_builder=FakeEnv("0.29.1"))

    def test_a_flag_the_arms_need_but_the_env_lacks_is_a_config_error(self, tmp_path):
        runner = FakeRunner()
        with pytest.raises(ConfigError, match=r"^--prxref: prxref 0.20.0 eval run has no --scoped-rules"):
            _run(_args(tmp_path, "--prxref", "0.20.0"), runner,
                 env_builder=FakeEnv("0.20.0", "--cases --label --out --resume --rules-file"))
        assert runner.calls == []

    def test_a_path_records_whatever_version_it_reports(self, tmp_path):
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        env = FakeEnv("0.31.0.dev0", "--cases --label --out --resume --rules-file --scoped-rules")
        _run(_args(tmp_path, "--repeats", "1", "--prxref", str(checkout)), FakeRunner(version="0.31.0.dev0"),
             env_builder=env)
        assert env.built[0][1].name.startswith("path-")
        assert _read(tmp_path / "camp" / "campaign.json")["prxref"]["version"] == "0.31.0.dev0"

    def test_without_uv_the_default_builder_refuses(self, tmp_path, monkeypatch):
        monkeypatch.setattr(eval_campaign.shutil, "which", lambda name: None)
        with pytest.raises(ConfigError, match="^--prxref: uv is not on PATH"):
            eval_campaign.build_prxref_env("0.29.0", tmp_path / "env")


def _main(argv: list[str], capsys) -> tuple[int, str]:
    code = cli.main(argv)
    return code, capsys.readouterr().err


class TestConfigErrors:
    @pytest.mark.parametrize("text, needle", [
        ("[[arm]\n", "--arms: "),
        ("[[arm]]\nname = \"x\"\nrule_file = \"\"\n", "--arms: arm 1: unknown key(s) rule_file"),
        ("[defaults]\n[[arm]]\nname = \"x\"\n", "--arms: unknown top-level key(s) defaults"),
        ("title = 'x'\n", "--arms: unknown top-level key(s) title"),
        ("[[arm]]\nname = \"x\"\n[[arm]]\nname = \"x\"\n", "--arms: arm 'x': the name is used twice"),
        ("[[arm]]\nname = \"../x\"\n", "--arms: arm 1: name must be"),
        ("[[arm]]\nname = \"x\"\nrules_file = \"missing.md\"\n", "--arms: arm 'x': "),
        ("[[arm]]\nname = \"x\"\nscoped_rules = \"one.md\"\n", "--arms: arm 'x': scoped_rules must be a list"),
        ("[[arm]]\nname = \"x\"\nprompts_dir = \"nope/\"\n", "--arms: arm 'x': "),
        ("[[arm]]\nname = \"x\"\nrules_file = \"rules.md\"\n[arm.mine_rules]\n",
         "--arms: arm 'x': mine_rules and a non-empty rules_file"),
        ("[[arm]]\nname = \"x\"\n[arm.mine_rules]\nmodel = \"m\"\n", "--arms: arm 'x': unknown mine_rules key(s)"),
    ])
    def test_a_bad_arms_file_exits_2(self, tmp_path, capsys, text, needle):
        code, err = _main(_argv(tmp_path, arms=_arms(tmp_path, text)), capsys)
        assert code == 2
        assert f"configuration error: {needle}" in err
        assert not (tmp_path / "camp").exists()

    def test_a_missing_arms_file_exits_2(self, tmp_path, capsys):
        code, err = _main(_argv(tmp_path, arms=tmp_path / "nope.toml"), capsys)
        assert code == 2 and "configuration error: --arms: cannot read" in err

    @pytest.mark.parametrize("flag", ["--repeats", "--folds", "--jobs", "--case-jobs", "--max-attempts"])
    def test_a_count_below_1_exits_2_naming_it(self, tmp_path, capsys, flag):
        code, err = _main(_argv(tmp_path, flag, "0"), capsys)
        assert code == 2 and f"configuration error: {flag}: must be at least 1, got 0" in err

    def test_a_bad_dataset_exits_2_naming_cases(self, tmp_path, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text("{}", encoding="utf-8")
        code, err = _main(_argv(tmp_path, cases=bad), capsys)
        assert code == 2 and "configuration error: --cases" in err

    def test_mining_without_a_judge_model_exits_2(self, tmp_path, capsys):
        code, err = _main(_argv(tmp_path, "--folds", "2", arms=_arms(tmp_path, MINE_ARMS)), capsys)
        assert code == 2 and "configuration error: --judge-model: arm 'mined' mines rules" in err

    def test_mining_with_one_fold_exits_2(self, tmp_path, capsys):
        code, err = _main(_argv(tmp_path, "--judge-model", "j", arms=_arms(tmp_path, MINE_ARMS)), capsys)
        assert code == 2 and "configuration error: --folds: arm 'mined' mines rules, which needs at least 2" in err

    def test_more_folds_than_pr_groups_exits_2(self, tmp_path, capsys):
        code, err = _main(_argv(tmp_path, "--folds", "4"), capsys)
        assert code == 2
        assert "configuration error: --folds: folds must be between 2 and the number of PR groups (3)" in err

    def test_labels_without_must_match_need_a_judge_model(self, tmp_path, capsys):
        data = tmp_path / "data"
        data.mkdir()
        (data / "change.patch").write_text(DIFF, encoding="utf-8")
        label = {"id": "h1", "file": "src/app.py", "line": 3, "severity": "error", "text": "judge me"}
        cases = data / "cases.json"
        cases.write_text(json.dumps({"version": 1, "cases": [
            {"id": "a", "diff_file": "change.patch", "expected": [label]}]}), encoding="utf-8")
        code, err = _main(_argv(tmp_path, cases=cases), capsys)
        assert code == 2 and "configuration error: --judge-model: required because 1 label(s) have no must_match" in err

    def test_a_severity_no_label_has_exits_2(self, tmp_path, capsys):
        code, err = _main(_argv(tmp_path, "--severity", "nit"), capsys)
        assert code == 2 and "configuration error: --severity: no label of the dataset has severity 'nit'" in err

    def test_a_non_empty_out_without_resume_exits_2(self, tmp_path, capsys):
        (tmp_path / "camp").mkdir()
        (tmp_path / "camp" / "stray.txt").write_text("x", encoding="utf-8")
        code, err = _main(_argv(tmp_path), capsys)
        assert code == 2 and "configuration error: --out: " in err and "pass --resume" in err

    def test_resume_into_a_non_campaign_directory_exits_2(self, tmp_path, capsys):
        (tmp_path / "camp").mkdir()
        (tmp_path / "camp" / "stray.txt").write_text("x", encoding="utf-8")
        code, err = _main(_argv(tmp_path, "--resume"), capsys)
        assert code == 2 and "holds no campaign.json" in err

    @pytest.mark.parametrize("change, flag", [
        (["--repeats", "3"], "--repeats"),
        (["--folds", "2"], "--folds"),
    ])
    def test_resume_with_different_inputs_exits_2(self, tmp_path, capsys, change, flag):
        cases, arms = _dataset(tmp_path), _arms(tmp_path)
        _run(_args(tmp_path, "--repeats", "1", cases=cases, arms=arms), FakeRunner())
        code, err = _main(_argv(tmp_path, "--repeats", "1", "--resume", *change, cases=cases, arms=arms), capsys)
        assert code == 2 and f"configuration error: --resume: {flag} differs from the campaign being resumed" in err

    def test_resume_with_another_dataset_or_arms_exits_2(self, tmp_path, capsys):
        cases, arms = _dataset(tmp_path), _arms(tmp_path)
        _run(_args(tmp_path, "--repeats", "1", cases=cases, arms=arms), FakeRunner())
        other = _dataset(tmp_path / "other", ids=("a", "b"))
        code, err = _main(_argv(tmp_path, "--repeats", "1", "--resume", cases=other, arms=arms), capsys)
        assert code == 2 and "--resume: --cases differs" in err
        fewer = _arms(tmp_path / "x", '[[arm]]\nname = "no-rules"\nrules_file = ""\n')
        code, err = _main(_argv(tmp_path, "--repeats", "1", "--resume", cases=cases, arms=fewer), capsys)
        assert code == 2 and "--resume: --arms differs" in err


class _StubLLM(BaseHTTPRequestHandler):
    """An OpenAI-compatible ``/chat/completions`` that reports one ``total`` finding to every chunk worker."""

    def do_POST(self):  # noqa: N802 - the http.server hook name
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        system = " ".join(m.get("content") or "" for m in body.get("messages", []) if m.get("role") == "system")
        findings = []
        if "You review one chunk of a pull-request diff per call." in system:
            findings = [{"file": "src/app.py", "line": 3, "severity": "error", "confidence": 0.9,
                         "title": "total is printed", "body": "The new total is printed to stdout."}]
        reply = {
            "model": body.get("model") or "stub-m",
            "choices": [{"message": {"role": "assistant",
                                     "content": json.dumps({"findings": findings, "escalations": []})},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        }
        data = json.dumps(reply).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        return None


class TestEndToEnd:
    def test_a_tiny_campaign_runs_real_eval_run_subprocesses_and_scores_them(self, tmp_path, monkeypatch):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _StubLLM)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            monkeypatch.chdir(tmp_path)
            monkeypatch.setenv("PRXREF_LLM_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}/v1")
            monkeypatch.setenv("PRXREF_LLM_MODELS", "stub-m")
            monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
            out = tmp_path / "camp"
            argv = _argv(tmp_path, "--repeats", "2", "--jobs", "2", "--case-jobs", "2", "--severity", "error",
                         cases=_dataset(tmp_path, ids=("a", "b")))
            out_buf = io.StringIO()
            code = eval_campaign.run_campaign(cli._build_parser().parse_args(argv), stdout=out_buf)
        finally:
            server.shutdown()
            server.server_close()
        assert code == 0
        passes = _passes(out)
        assert {p["state"] for p in passes.values()} == {"scored"}, passes
        for arm in ("no-rules", "rules"):
            for k in (1, 2):
                score = _read(out / "runs" / arm / f"r{k}" / "score.json")
                assert score["failed"] == []
                assert score["metrics"]["recall"]["recall"] == 1.0
                for label in ("f0-s0", "f0-s1"):
                    unit = _read(out / "units" / arm / f"r{k}" / label / "run.json")
                    assert unit["prxref_version"] == prxref.__version__
        rules = _read(out / "runs" / "rules" / "r1" / "run.json")["review_rules"]
        assert rules is not None
        assert _read(out / "runs" / "no-rules" / "r1" / "run.json")["review_rules"] is None
        log = (out / "logs" / "rules-r2.log").read_text(encoding="utf-8")
        assert "run directory: " in log
        assert "| rules | 2/2 | 100.0%; 100.0%, 100.0% | 100.0%; 100.0%, 100.0% |" in out_buf.getvalue()
