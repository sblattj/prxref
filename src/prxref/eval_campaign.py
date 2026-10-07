"""``prxref eval campaign``: arms x repeats of ``eval run``, in parallel, resumable, scored.

A *campaign* answers "is this prxref version, or this rules file, better on
our past PRs?" with repeated runs, so the answer can be told apart from
run-to-run noise by ``prxref eval verdict``. :func:`run_campaign` is the
whole command; ``prxref.evals.eval_campaign`` imports it lazily.

- An *arm* is one rules configuration, read from ``arms.toml``
  (:func:`load_arms`). A *pass* is one arm at one repeat ``k`` (``1..N``).
- A pass is split into *units*: the arm's folds (one fold unless it mines
  rules) times ``--case-jobs`` shards (:func:`prxref.eval_units.shard_ids`).
  Each shard is one ``eval run`` subprocess over a subset ``cases.json``
  (:func:`prxref.eval_units.write_subset_cases`), at most ``--jobs`` passes
  and, inside a pass, at most ``--case-jobs`` subprocesses at a time.
- An arm with ``[arm.mine_rules]`` gets one rules file per fold, mined once
  per arm (not per repeat) from the human labels of the OTHER folds
  (:func:`prxref.eval_rules_mine.training_cases`), so no rules file has seen
  the labels it is scored on.
- After a pass's subprocesses finish every case is classified with
  :func:`prxref.eval_units.unit_state`. ``ok`` units are kept; every other
  unit is reset and its shard run again with ``--resume``, up to
  ``--max-attempts`` attempts in all, after a ``min(300, 30 * attempt)``
  second pause when any unit was rate limited. A unit still failing after
  the last attempt stays in the run and counts as missed.
- The shards are merged (:func:`prxref.eval_units.merge_runs`) into
  ``runs/<arm>/r<k>`` and scored in process by
  :func:`prxref.evals.eval_score`, always by THIS prxref, even when the
  reviews ran a pinned ``--prxref``, so every campaign is graded by one
  scorer.

The directory layout, ``campaign.json`` and ``progress.json`` are documented
in ``docs/evals.md``. Only a configuration problem raises
:class:`~prxref.llm.ConfigError` (exit 2), and it does so before any review
runs; a pass that fails is reported and the command still exits 0.

This module never imports :mod:`prxref.cli`.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import prxref
from prxref import eval_judge, eval_units, evals
from prxref.config import find_config_file, load_config, load_config_with_sources
from prxref.eval_cases import EvalCase, case_to_json, is_safe_id, load_cases
from prxref.eval_rules_mine import assign_folds, mine_rules, training_cases
from prxref.llm import ConfigError
from prxref.review_inputs import load_path_inputs

CAMPAIGN_VERSION = 1
PROGRESS_VERSION = 1
PASS_PENDING = "pending"
PASS_RUNNING = "running"
PASS_SCORING = "scoring"
PASS_SCORED = "scored"
PASS_FAILED = "failed"
PASS_STATES = (PASS_PENDING, PASS_RUNNING, PASS_SCORING, PASS_SCORED, PASS_FAILED)
ARM_KEYS = ("name", "rules_file", "scoped_rules", "prompts_dir", "mine_rules")
MINE_RULES_KEYS = ("judge_model",)
MAX_RATE_LIMIT_SLEEP_S = 300
RATE_LIMIT_SLEEP_STEP_S = 30
INSTALL_TIMEOUT_S = 1800
PROBE_TIMEOUT_S = 300

Runner = Callable[[Sequence[str], Path, Mapping[str, str]], int]


@dataclass(frozen=True)
class Arm:
    """One arm of ``arms.toml``, its paths resolved against the file's directory.

    ``rules_file``: ``None`` keeps the environment's rules file, ``""`` turns
    it off, anything else is an absolute path. ``scoped_rules``: ``None``
    keeps the environment's, ``()`` turns them off, else absolute paths.
    ``prompts_dir``: ``None``, ``""`` or an absolute path, the same way.
    ``mine_judge_model`` is the model that mines the arm's per-fold rules, or
    ``None`` when the arm does not mine.
    """

    name: str
    rules_file: str | None = None
    scoped_rules: tuple[str, ...] | None = None
    prompts_dir: str | None = None
    mine_judge_model: str | None = None

    @property
    def mines(self) -> bool:
        """Whether the arm mines its rules per fold."""
        return self.mine_judge_model is not None

    def to_json(self) -> dict[str, Any]:
        """The arm as ``campaign.json`` records it."""
        return {
            "name": self.name,
            "rules_file": self.rules_file,
            "scoped_rules": None if self.scoped_rules is None else list(self.scoped_rules),
            "prompts_dir": self.prompts_dir,
            "mine_rules": None if self.mine_judge_model is None else {"judge_model": self.mine_judge_model},
        }


@dataclass(frozen=True)
class PrxrefEnv:
    """The prxref the reviews run with.

    ``command`` is the prefix that ``eval run ...`` is appended to.
    ``requested`` is ``--prxref`` as given (``None`` for this prxref),
    ``version`` what that install reports, ``python`` its interpreter
    (``None`` for this prxref) and ``eval_run_help`` the text of its
    ``eval run --help`` (empty for this prxref, which has every flag).
    ``env`` holds environment variables to set (``None`` value: remove).
    """

    requested: str | None
    version: str
    python: str | None
    command: tuple[str, ...]
    eval_run_help: str = ""
    env: Mapping[str, str | None] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """The ``prxref`` block of ``campaign.json``."""
        return {"requested": self.requested, "version": self.version, "python": self.python}


EnvBuilder = Callable[[str, Path], PrxrefEnv]


def cases_sha256(cases: Sequence[EvalCase]) -> str:
    """The sha256 of a dataset: canonical JSON of :func:`~prxref.eval_cases.case_to_json` per case.

    Key order is sorted and separators fixed, so the same cases loaded from
    the same place hash the same. Paths are part of the hash as the loader
    resolved them.
    """
    payload = json.dumps([case_to_json(case) for case in cases], sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_arms(path: str) -> list[Arm]:
    """Read and validate ``arms.toml``; every problem raises ``ConfigError`` naming ``--arms``.

    The file holds only ``[[arm]]`` tables with the keys of :data:`ARM_KEYS`
    (``mine_rules`` a table with only ``judge_model``). ``name`` is required,
    a safe id (:func:`prxref.eval_cases.is_safe_id`) and unique. Relative
    paths are joined onto the directory of the file. A mining arm cannot also
    name a non-empty ``rules_file``. Whether the paths load is checked by
    :func:`run_campaign`.
    """
    source = Path(path)
    try:
        data = tomllib.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"--arms: cannot read {path!r}: {exc.strerror or exc}") from exc
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"--arms: {path!r} is not valid TOML: {exc}") from exc
    unknown = sorted(set(data) - {"arm"})
    if unknown:
        raise ConfigError(f"--arms: unknown top-level key(s) {', '.join(unknown)}; only [[arm]] tables are allowed")
    tables = data.get("arm")
    if not isinstance(tables, list) or not tables or not all(isinstance(t, dict) for t in tables):
        raise ConfigError("--arms: needs at least one [[arm]] table")
    base = source.resolve().parent
    arms: list[Arm] = []
    seen: set[str] = set()
    for number, table in enumerate(tables, start=1):
        where = f"--arms: arm {number}"
        extra = sorted(set(table) - set(ARM_KEYS))
        if extra:
            raise ConfigError(f"{where}: unknown key(s) {', '.join(extra)}; allowed: {', '.join(ARM_KEYS)}")
        name = table.get("name")
        if not is_safe_id(name):
            raise ConfigError(
                f"{where}: name must be one directory name of letters, digits, '.', '_' and '-' "
                f"that starts with a letter or digit, got {name!r}"
            )
        where = f"--arms: arm {name!r}"
        if name in seen:
            raise ConfigError(f"{where}: the name is used twice")
        seen.add(name)
        rules_file = _optional_path(table, "rules_file", base, where)
        prompts_dir = _optional_path(table, "prompts_dir", base, where)
        scoped = table.get("scoped_rules")
        scoped_rules: tuple[str, ...] | None = None
        if "scoped_rules" in table:
            if not isinstance(scoped, list) or not all(isinstance(item, str) and item for item in scoped):
                raise ConfigError(f"{where}: scoped_rules must be a list of non-empty paths, got {scoped!r}")
            scoped_rules = tuple(_join(base, item) for item in scoped)
        mine_model = None
        if "mine_rules" in table:
            mine = table["mine_rules"]
            if not isinstance(mine, dict):
                raise ConfigError(f"{where}: mine_rules must be a table, got {mine!r}")
            extra = sorted(set(mine) - set(MINE_RULES_KEYS))
            if extra:
                raise ConfigError(f"{where}: unknown mine_rules key(s) {', '.join(extra)}; allowed: judge_model")
            model = mine.get("judge_model", "")
            if not isinstance(model, str):
                raise ConfigError(f"{where}: mine_rules.judge_model must be a string, got {model!r}")
            mine_model = model.strip()
            if rules_file:
                raise ConfigError(f"{where}: mine_rules and a non-empty rules_file cannot be combined")
        arms.append(Arm(name, rules_file, scoped_rules, prompts_dir, mine_model))
    return arms


def default_runner(cmd: Sequence[str], log_path: Path, env: Mapping[str, str]) -> int:
    """Run ``cmd`` with stdout and stderr appended to ``log_path``; return its exit code."""
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(list(cmd), stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=dict(env))
        return proc.wait()


def current_prxref_env() -> PrxrefEnv:
    """This prxref: the running interpreter, with this package first on ``PYTHONPATH``."""
    root = str(Path(prxref.__file__).resolve().parents[1])
    existing = os.environ.get("PYTHONPATH")
    path = root if not existing else os.pathsep.join([root, existing])
    return PrxrefEnv(None, prxref.__version__, None, (sys.executable, "-m", "prxref.cli"), "",
                     {"PYTHONPATH": path})


def is_prxref_path(requested: str) -> bool:
    """Whether ``--prxref`` names a local checkout or wheel rather than a version."""
    return os.sep in requested or "/" in requested or requested.startswith(".") or os.path.exists(requested)


def env_slug(requested: str) -> str:
    """The ``envs/<slug>`` directory name for ``--prxref``."""
    if is_prxref_path(requested):
        digest = hashlib.sha256(os.path.abspath(requested).encode("utf-8")).hexdigest()[:12]
        return f"path-{digest}"
    return re.sub(r"[^A-Za-z0-9._-]", "_", requested)


def build_prxref_env(requested: str, env_dir: Path) -> PrxrefEnv:
    """Install ``prxref==requested`` (or the local path) into ``env_dir`` with ``uv``, once.

    ``uv venv`` then ``uv pip install --python <env>/bin/python``; an env
    that already holds ``bin/prxref`` is reused. Returns the env with the
    version its interpreter imports and its ``eval run --help``. A missing
    ``uv`` or a failed step raises ``ConfigError`` naming ``--prxref``.
    ``PYTHONPATH`` is removed for its subprocesses, so it cannot import
    another prxref.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise ConfigError("--prxref: uv is not on PATH; it is needed to build the isolated prxref env")
    env_dir = Path(env_dir)
    bindir = env_dir / ("Scripts" if os.name == "nt" else "bin")
    python = bindir / ("python.exe" if os.name == "nt" else "python")
    exe = bindir / ("prxref.exe" if os.name == "nt" else "prxref")
    child_env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    if not exe.is_file():
        target = os.path.abspath(requested) if is_prxref_path(requested) else f"prxref=={requested}"
        env_dir.parent.mkdir(parents=True, exist_ok=True)
        _probe([uv, "venv", "--allow-existing", str(env_dir)], "uv venv", child_env, INSTALL_TIMEOUT_S)
        _probe([uv, "pip", "install", "--python", str(python), target], f"installing {target}",
               child_env, INSTALL_TIMEOUT_S)
    version = _probe([str(python), "-c", "import prxref; print(prxref.__version__)"],
                     "reading the installed version", child_env, PROBE_TIMEOUT_S).strip()
    help_text = _probe([str(exe), "eval", "run", "--help"], "prxref eval run --help", child_env, PROBE_TIMEOUT_S)
    return PrxrefEnv(requested, version, str(python), (str(exe),), help_text, {"PYTHONPATH": None})


def _probe(cmd: list[str], what: str, env: Mapping[str, str], timeout: float) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=dict(env), timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"--prxref: {what} failed: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-600:]
        raise ConfigError(f"--prxref: {what} failed (exit {proc.returncode}): {tail}")
    return proc.stdout


def run_campaign(
    args: argparse.Namespace,
    *,
    runner: Runner = default_runner,
    env_builder: EnvBuilder = build_prxref_env,
    sleep: Callable[[float], None] = time.sleep,
    stdout: TextIO | None = None,
) -> int:
    """Run (or ``--resume``) the campaign of ``args`` and return 0.

    Reads ``args.cases``, ``args.arms``, ``args.repeats``, ``args.out``,
    ``args.prxref``, ``args.folds``, ``args.jobs``, ``args.case_jobs``,
    ``args.judge_model``, ``args.severity``, ``args.resume`` and
    ``args.max_attempts``. ``runner`` runs one ``eval run`` subprocess,
    ``env_builder`` builds the ``--prxref`` env and ``sleep`` waits before
    a rate-limit retry; tests replace them. Every check runs before any
    review and raises ``ConfigError`` naming its flag (exit 2). Failed
    passes are reported, never raised.
    """
    out_stream = stdout if stdout is not None else sys.stdout
    plan = _validate(args, env_builder)
    campaign = _Campaign(plan, runner=runner, sleep=sleep, stdout=out_stream)
    campaign.run()
    return 0


@dataclass
class _Plan:
    args: argparse.Namespace
    out: Path
    cases: list[EvalCase]
    case_ids: list[str]
    arms: list[Arm]
    folds: dict[str, int] | None
    env: PrxrefEnv
    campaign_json: dict[str, Any]
    judge_clients: dict[str, Any]


def _validate(args: argparse.Namespace, env_builder: EnvBuilder) -> _Plan:
    for flag, value, low in (("--repeats", args.repeats, 1), ("--folds", args.folds, 1), ("--jobs", args.jobs, 1),
                             ("--case-jobs", args.case_jobs, 1), ("--max-attempts", args.max_attempts, 1)):
        if value < low:
            raise ConfigError(f"{flag}: must be at least {low}, got {value}")
    cases = load_cases(args.cases, source="--cases")
    if not cases:
        raise ConfigError(f"--cases: {args.cases!r} holds no case")
    arms = load_arms(args.arms)
    severity = args.severity
    unjudged = sum(1 for case in cases for label in case.expected if not label.must_match)
    if unjudged and not args.judge_model:
        raise ConfigError(
            f"--judge-model: required because {unjudged} label(s) have no must_match, and every pass is scored"
        )
    if severity is not None and not any(label.severity == severity for case in cases for label in case.expected):
        raise ConfigError(f"--severity: no label of the dataset has severity {severity!r}")
    mining = [arm for arm in arms if arm.mines]
    resolved: list[Arm] = []
    for arm in arms:
        if arm.mines and not arm.mine_judge_model:
            if not args.judge_model:
                raise ConfigError(
                    f"--judge-model: arm {arm.name!r} mines rules and sets no mine_rules.judge_model"
                )
            arm = Arm(arm.name, arm.rules_file, arm.scoped_rules, arm.prompts_dir, args.judge_model.strip())
        resolved.append(arm)
    arms = resolved
    if mining and args.folds < 2:
        raise ConfigError(
            f"--folds: arm {mining[0].name!r} mines rules, which needs at least 2 folds, got {args.folds}"
        )
    folds = None
    if args.folds >= 2:
        try:
            folds = assign_folds(cases, args.folds)
        except ValueError as exc:
            raise ConfigError(f"--folds: {exc}") from exc
    config_file = find_config_file(explicit=None)
    for arm in arms:
        _check_arm_paths(arm, config_file)
    judge_clients: dict[str, Any] = {}
    if mining:
        cfg = load_config()
        for arm in arms:
            if arm.mines:
                judge_clients[arm.name] = eval_judge.build_judge_client(cfg, arm.mine_judge_model)
    out = Path(args.out)
    existing = out / "campaign.json"
    if out.exists() and not out.is_dir():
        raise ConfigError(f"--out: {str(out)!r} is not a directory")
    previous = None
    if args.resume:
        if existing.is_file():
            try:
                previous = json.loads(existing.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ConfigError(f"--resume: cannot read {str(existing)!r}: {exc}") from exc
        elif out.is_dir() and any(out.iterdir()):
            raise ConfigError(f"--resume: {str(out)!r} is not empty and holds no campaign.json")
    elif out.is_dir() and any(out.iterdir()):
        raise ConfigError(f"--out: {str(out)!r} is not empty; pass --resume to continue the campaign in it")
    digest = cases_sha256(cases)
    arms_json = [arm.to_json() for arm in arms]
    if previous is not None:
        _check_same(previous, "cases_sha256", digest, "--cases")
        _check_same(previous, "arms", arms_json, "--arms")
        _check_same(previous, "repeats", args.repeats, "--repeats")
        _check_same(previous, "folds", args.folds, "--folds")
        _check_same(previous.get("prxref") or {}, "requested", args.prxref, "--prxref")
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"--out: cannot create {str(out)!r}: {exc.strerror or exc}") from exc
    if args.prxref:
        env = env_builder(args.prxref, out / "envs" / env_slug(args.prxref))
        if not is_prxref_path(args.prxref) and env.version != args.prxref.strip():
            raise ConfigError(f"--prxref: asked for {args.prxref!r}, but the env reports version {env.version!r}")
        missing = [flag for flag in _needed_flags(arms) if not re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])",
                                                                          env.eval_run_help)]
        if missing:
            raise ConfigError(
                f"--prxref: prxref {env.version} eval run has no {', '.join(missing)}, which the arms need"
            )
    else:
        env = current_prxref_env()
    if previous is not None:
        _check_same(previous.get("prxref") or {}, "version", env.version, "--prxref")
        campaign_json = previous
    else:
        campaign_json = {
            "version": CAMPAIGN_VERSION,
            "created_at": _now(),
            "cases_path": str(args.cases),
            "cases_sha256": digest,
            "repeats": args.repeats,
            "folds": args.folds,
            "severity": severity,
            "judge_model": args.judge_model,
            "prxref": env.to_json(),
            "arms": arms_json,
        }
        _write_json(existing, campaign_json)
    return _Plan(args, out, cases, [case.id for case in cases], arms, folds, env, campaign_json, judge_clients)


def _check_arm_paths(arm: Arm, config_file: Path | None) -> None:
    """Load the arm's rules file, scoped rules and prompts directory as ``eval run`` will."""
    scoped = None if arm.scoped_rules is None else (list(arm.scoped_rules) or [""])
    try:
        cfg, layers = load_config_with_sources(
            config_file=config_file,
            review_rules="" if arm.mines else arm.rules_file,
            scoped_rules=scoped,
            prompts_dir=arm.prompts_dir,
            source_labels={"review_rules": "rules_file", "scoped_rules": "scoped_rules",
                           "prompts_dir": "prompts_dir"},
        )
        load_path_inputs(cfg, layers, config_file=config_file, ticket=False, evidence=False, learnings=False)
    except ConfigError as exc:
        raise ConfigError(f"--arms: arm {arm.name!r}: {exc}") from exc


def _needed_flags(arms: Sequence[Arm]) -> list[str]:
    flags = ["--cases", "--label", "--out", "--resume"]
    if any(arm.rules_file is not None or arm.mines for arm in arms):
        flags.append("--rules-file")
    if any(arm.scoped_rules is not None for arm in arms):
        flags.append("--scoped-rules")
    if any(arm.prompts_dir is not None for arm in arms):
        flags.append("--prompts-dir")
    return flags


def _check_same(previous: Mapping[str, Any], key: str, value: Any, flag: str) -> None:
    if previous.get(key) != value:
        raise ConfigError(
            f"--resume: {flag} differs from the campaign being resumed ({key} was {previous.get(key)!r}, "
            f"now {value!r}); start a new campaign under another --out"
        )


@dataclass
class _Shard:
    fold: int
    index: int
    case_ids: list[str]

    @property
    def label(self) -> str:
        return f"f{self.fold}-s{self.index}"


class _PassFailed(Exception):
    """A pass cannot finish; the message is its ``error``."""


class _Campaign:
    def __init__(self, plan: _Plan, *, runner: Runner, sleep: Callable[[float], None], stdout: TextIO) -> None:
        self.plan = plan
        self.args = plan.args
        self.out = plan.out
        self.runner = runner
        self.sleep = sleep
        self.stdout = stdout
        self.lock = threading.Lock()
        self.score_lock = threading.Lock()
        self.progress = self._initial_progress()
        self.mined_errors: dict[str, str] = {}
        self.by_id = {case.id: case for case in plan.cases}

    def _initial_progress(self) -> dict[str, Any]:
        path = self.out / "progress.json"
        previous: dict[str, Any] = {}
        if self.args.resume and path.is_file():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = {}
        old = {(p.get("arm"), p.get("repeat")): p for p in previous.get("passes") or [] if isinstance(p, dict)}
        passes = []
        for arm in self.plan.arms:
            for k in range(1, self.args.repeats + 1):
                kept = old.get((arm.name, k))
                if kept and kept.get("state") == PASS_SCORED and (self._run_dir(arm.name, k) / "score.json").is_file():
                    passes.append(kept)
                    continue
                passes.append({
                    "arm": arm.name, "repeat": k, "state": PASS_PENDING,
                    "units_total": len(self.plan.case_ids), "units_ok": 0, "units_failed": 0,
                    "units_rate_limited": 0, "attempt": 0, "started_at": None, "finished_at": None,
                    "log": f"logs/{arm.name}-r{k}.log", "error": None,
                })
        now = _now()
        return {"version": PROGRESS_VERSION, "started_at": previous.get("started_at") or now,
                "updated_at": now, "passes": passes}

    def _run_dir(self, arm: str, k: int) -> Path:
        return self.out / "runs" / arm / f"r{k}"

    def _pass(self, arm: str, k: int) -> dict[str, Any]:
        return next(p for p in self.progress["passes"] if p["arm"] == arm and p["repeat"] == k)

    def _update(self, arm: str, k: int, **fields: Any) -> None:
        with self.lock:
            entry = self._pass(arm, k)
            changed = "state" in fields and fields["state"] != entry["state"]
            entry.update(fields)
            self.progress["updated_at"] = _now()
            _write_json(self.out / "progress.json", self.progress)
            if changed:
                line = f"{arm} r{k}: {entry['state']} ({entry['units_ok']}/{entry['units_total']} units)"
                if entry["state"] == PASS_FAILED and entry.get("error"):
                    line += f": {entry['error']}"
                print(line, file=self.stdout, flush=True)

    def run(self) -> None:
        (self.out / "logs").mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.progress["updated_at"] = _now()
            _write_json(self.out / "progress.json", self.progress)
        todo = [(arm, p["repeat"]) for arm in self.plan.arms for p in self.progress["passes"]
                if p["arm"] == arm.name and p["state"] != PASS_SCORED]
        for arm in self.plan.arms:
            if arm.mines and any(a is arm for a, _ in todo):
                self._mine(arm)
        with ThreadPoolExecutor(max_workers=self.args.jobs) as pool:
            list(pool.map(lambda item: self._run_pass(*item), todo))
        self._summary()

    def _mine(self, arm: Arm) -> None:
        folds = self.plan.folds or {}
        rules_dir = self.out / "rules" / arm.name
        try:
            cfg = load_config()
            for j in range(self.args.folds):
                path = rules_dir / f"f{j}.md"
                if path.is_file() and path.read_text(encoding="utf-8").strip():
                    continue
                mined = mine_rules(
                    training_cases(self.plan.cases, folds, j), self.plan.judge_clients[arm.name],
                    arm.mine_judge_model or "", cache_dir=rules_dir / "cache",
                    max_tokens=cfg["llm_max_tokens"], parse_retries=cfg["llm_parse_retries"],
                )
                rules_dir.mkdir(parents=True, exist_ok=True)
                _write_text(path, mined.text.rstrip("\n") + "\n")
        except Exception as exc:  # noqa: BLE001 - a mining failure fails the arm, not the campaign
            self.mined_errors[arm.name] = f"rules mining failed: {type(exc).__name__}: {exc}"

    def _shards(self, arm: Arm) -> list[_Shard]:
        if arm.mines:
            folds = self.plan.folds or {}
            groups = [[cid for cid in self.plan.case_ids if folds[cid] == j] for j in range(self.args.folds)]
        else:
            groups = [list(self.plan.case_ids)]
        return [_Shard(j, i, ids) for j, ids in enumerate(groups)
                for i, ids in enumerate(eval_units.shard_ids(ids, self.args.case_jobs))]

    def _run_pass(self, arm: Arm, k: int) -> None:
        log = self.out / "logs" / f"{arm.name}-r{k}.log"
        self._update(arm.name, k, started_at=_now(), finished_at=None, error=None, attempt=0)
        if arm.name in self.mined_errors:
            self._update(arm.name, k, state=PASS_FAILED, error=self.mined_errors[arm.name], finished_at=_now())
            return
        try:
            self._update(arm.name, k, state=PASS_RUNNING)
            shard_dirs = self._run_units(arm, k, log)
            self._update(arm.name, k, state=PASS_SCORING)
            self._merge_and_score(arm, k, shard_dirs, log)
        except _PassFailed as exc:
            self._update(arm.name, k, state=PASS_FAILED, error=str(exc), finished_at=_now())
            return
        except Exception as exc:  # noqa: BLE001 - one failed pass never stops the campaign
            self._update(arm.name, k, state=PASS_FAILED, error=f"{type(exc).__name__}: {exc}", finished_at=_now())
            return
        self._update(arm.name, k, state=PASS_SCORED, finished_at=_now())

    def _run_units(self, arm: Arm, k: int, log: Path) -> list[Path]:
        units_dir = self.out / "units" / arm.name / f"r{k}"
        inputs = units_dir / "inputs"
        shards = self._shards(arm)
        for shard in shards:
            eval_units.write_subset_cases(self.args.cases, shard.case_ids, inputs / f"{shard.label}.json")
        states = self._check(arm, k, units_dir, shards)
        attempt = 0
        while attempt < self.args.max_attempts:
            todo = [s for s in shards if not self._shard_done(units_dir, s, states)]
            if not todo:
                break
            if attempt > 0 and any(states[cid] == eval_units.UNIT_RATE_LIMITED for s in todo for cid in s.case_ids):
                self.sleep(min(MAX_RATE_LIMIT_SLEEP_S, RATE_LIMIT_SLEEP_STEP_S * attempt))
            attempt += 1
            self._update(arm.name, k, attempt=attempt)
            for shard in todo:
                for cid in shard.case_ids:
                    if states[cid] != eval_units.UNIT_OK:
                        eval_units.reset_unit(units_dir / shard.label / "cases" / cid)
            number = attempt
            with ThreadPoolExecutor(max_workers=self.args.case_jobs) as pool:
                codes = list(pool.map(
                    lambda s, n=number: self._run_shard(arm, k, s, units_dir, inputs, log, n), todo,
                ))
            bad = [s.label for s, rc in zip(todo, codes, strict=True) if rc == 2]
            if bad:
                raise _PassFailed(
                    f"eval run exited 2 (configuration error) for {', '.join(bad)}; see {self._rel(log)}"
                )
            states = self._check(arm, k, units_dir, shards)
        for shard in shards:
            for cid in shard.case_ids:
                if states[cid] != eval_units.UNIT_OK:
                    self._finalize_unit(units_dir / shard.label / "cases" / cid, cid, states[cid], attempt)
        dirs = [units_dir / s.label for s in shards]
        missing_runs = [s.label for s in shards if not (units_dir / s.label / "run.json").is_file()]
        if missing_runs:
            raise _PassFailed(f"no run.json for unit(s) {', '.join(missing_runs)} after {attempt} attempt(s); "
                              f"see {self._rel(log)}")
        for path in dirs:
            run = json.loads((path / "run.json").read_text(encoding="utf-8"))
            version = run.get("prxref_version", self.plan.env.version)
            if version != self.plan.env.version:
                raise _PassFailed(
                    f"unit {self._rel(path)} was written by prxref {version}, expected {self.plan.env.version}"
                )
        return dirs

    def _shard_done(self, units_dir: Path, shard: _Shard, states: Mapping[str, str]) -> bool:
        return (units_dir / shard.label / "run.json").is_file() and all(
            states[cid] == eval_units.UNIT_OK for cid in shard.case_ids)

    def _check(self, arm: Arm, k: int, units_dir: Path, shards: Sequence[_Shard]) -> dict[str, str]:
        states = {cid: eval_units.unit_state(units_dir / s.label / "cases" / cid)
                  for s in shards for cid in s.case_ids}
        values = list(states.values())
        rate = values.count(eval_units.UNIT_RATE_LIMITED)
        ok = values.count(eval_units.UNIT_OK)
        self._update(arm.name, k, units_ok=ok, units_rate_limited=rate, units_failed=len(values) - ok - rate)
        return states

    def _run_shard(self, arm: Arm, k: int, shard: _Shard, units_dir: Path, inputs: Path, log: Path,
                   attempt: int) -> int:
        cmd = [*self.plan.env.command, "eval", "run", "--cases", str(inputs / f"{shard.label}.json"),
               "--label", shard.label, "--out", str(units_dir)]
        if arm.mines:
            cmd += ["--rules-file", str(self.out / "rules" / arm.name / f"f{shard.fold}.md")]
        elif arm.rules_file is not None:
            cmd += ["--rules-file", arm.rules_file]
        if arm.scoped_rules is not None:
            for item in arm.scoped_rules or ("",):
                cmd += ["--scoped-rules", item]
        if arm.prompts_dir is not None:
            cmd += ["--prompts-dir", arm.prompts_dir]
        if (units_dir / shard.label).exists():
            cmd.append("--resume")
        env = dict(os.environ)
        for key, value in self.plan.env.env.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        with self.lock:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(f"=== {_now()} {arm.name} r{k} attempt {attempt} unit {shard.label}: "
                             f"{' '.join(cmd)}\n")
        return self.runner(cmd, log, env)

    def _finalize_unit(self, case_dir: Path, case_id: str, state: str, attempts: int) -> None:
        """Make a unit still failing after the last attempt score as missed.

        An ``error`` unit already scores as missed. A rate-limited unit that
        produced a record anyway (a fallback) and a ``missing`` unit get an
        ``error.json``, any ``record.json`` moved aside to
        ``record.rejected.json``, so ``eval score`` counts none of it.
        """
        if state == eval_units.UNIT_ERROR:
            return
        record = case_dir / "record.json"
        if state == eval_units.UNIT_RATE_LIMITED and not record.is_file():
            return
        if state == eval_units.UNIT_RATE_LIMITED and _record_verdict(record) == "Error":
            return
        case_dir.mkdir(parents=True, exist_ok=True)
        if record.is_file():
            os.replace(record, case_dir / "record.rejected.json")
        if not (case_dir / "case.json").is_file():
            _write_json(case_dir / "case.json", case_to_json(self.by_id[case_id]))
        why = "rate limited" if state == eval_units.UNIT_RATE_LIMITED else "no result"
        _write_json(case_dir / "error.json", {
            "case_id": case_id, "error": f"CampaignError: {why} after {attempts} attempt(s)",
        })

    def _merge_and_score(self, arm: Arm, k: int, shard_dirs: Sequence[Path], log: Path) -> None:
        dest = self._run_dir(arm.name, k)
        dest.parent.mkdir(parents=True, exist_ok=True)
        eval_units.merge_runs(shard_dirs, dest, label=f"r{k}", case_ids=self.plan.case_ids)
        namespace = argparse.Namespace(label=f"r{k}", out=str(dest.parent), judge_model=self.args.judge_model,
                                       precision=bool(self.args.judge_model))
        with self.score_lock, open(log, "a", encoding="utf-8") as handle, contextlib.redirect_stdout(handle):
            handle.write(f"=== {_now()} {arm.name} r{k} scoring {self._rel(dest)}\n")
            handle.flush()
            evals.eval_score(namespace)

    def _rel(self, path: Path) -> str:
        try:
            return str(Path(path).relative_to(self.out))
        except ValueError:
            return str(path)

    def _summary(self) -> None:
        severity = self.args.severity
        heads = ["Arm", "Scored", "Recall (mean; per pass)"]
        if severity:
            heads.append(f"{severity} recall (mean; per pass)")
        lines = ["", "| " + " | ".join(heads) + " |", "|" + "---|" * len(heads)]
        for arm in self.plan.arms:
            passes = [p for p in self.progress["passes"] if p["arm"] == arm.name]
            recalls: list[float | None] = []
            gates: list[float | None] = []
            for entry in passes:
                if entry["state"] != PASS_SCORED:
                    continue
                metrics = _read_metrics(self._run_dir(arm.name, entry["repeat"]) / "score.json")
                recalls.append((metrics.get("recall") or {}).get("recall"))
                if severity:
                    gates.append(((metrics.get("recall_by_severity") or {}).get(severity) or {}).get("recall"))
            row = [arm.name, f"{len(recalls)}/{len(passes)}", _series(recalls)]
            if severity:
                row.append(_series(gates))
            lines.append("| " + " | ".join(row) + " |")
        lines += ["", f"campaign: {self.out}",
                  f"next: prxref eval verdict --baseline {self.out} --candidate <other campaign>"]
        print("\n".join(lines), file=self.stdout, flush=True)


def _record_verdict(path: Path) -> Any:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record.get("verdict") if isinstance(record, dict) else None


def _read_metrics(path: Path) -> dict[str, Any]:
    try:
        score = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    metrics = score.get("metrics") if isinstance(score, dict) else None
    return metrics if isinstance(metrics, dict) else {}


def _series(values: Sequence[float | None]) -> str:
    known = [v for v in values if isinstance(v, (int, float))]
    if not known:
        return "n/a"
    mean = sum(known) / len(known)
    return f"{mean * 100:.1f}%; " + ", ".join("n/a" if v is None else f"{v * 100:.1f}%" for v in values)


def _optional_path(table: Mapping[str, Any], key: str, base: Path, where: str) -> str | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, str):
        raise ConfigError(f"{where}: {key} must be a string path (\"\" turns it off), got {value!r}")
    return "" if value == "" else _join(base, value)


def _join(base: Path, value: str) -> str:
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else (base / path))


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, obj: Any) -> None:
    _write_text(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
