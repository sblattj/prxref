# sdlc review gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `prxref gate …`, a local GitHub App (`sdlc`) that reviews every pull request on an allowlisted repository with prxref and publishes an app-owned `sdlc-review` check run that a ruleset pins as a required status check (issue #102).

**Architecture:** One process (`prxref gate serve`) runs three threads over one SQLite file: an HTTP receiver that verifies, dedups and enqueues webhook deliveries; a single worker that holds a fenced lease, runs `orchestrate_review` against a capturing forge built on an installation token, and publishes a check run plus a `COMMENT` review; and a watchdog that re-queues orphaned work and reconciles open PRs against the store. Repository rules (`.prxref.toml`, rule files, standards documents, learnings) are read at the PR's **base** SHA; the head SHA supplies inert file context only.

**Tech Stack:** Python 3.12+, stdlib (`sqlite3`, `http.server`, `threading`, `concurrent.futures`, `tomllib`, `plistlib`), `requests`, optional extra `github-app` = `pyjwt[crypto]`, litellm → Amazon Bedrock (`bedrock/us.anthropic.claude-opus-4-6-v1`), pytest, ruff, uv.

**Spec:** `docs/design/2026-10-08-sdlc-review-gate.md` (read it beside this plan; section names below refer to it).

**Baseline:** at `aa01299` the full suite is green: `11680 passed`. Re-derive with `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest -q -p no:randomly 2>&1 | tail -1` (takes about 1 minute; give the Bash tool `timeout: 300000`).

**Commands used by every task:**

- Run one test file: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_x.py -q`
- Lint: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run ruff check src tests`
- Full suite (end of every task that touches `src/prxref/config.py`, `cli.py` or `orchestrator.py`): `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest -q` with `timeout: 300000`.
- Commit: stage by explicit pathspec only (never `git add -A`). Every commit message ends with a blank line and `Claude-Session-Id: <the executing session's full UUID>`.

## Global Constraints

- Python 3.12+; `uv` for env and lock; hatchling packaging (unchanged).
- Core stays stdlib + `requests`. `pyjwt[crypto]` arrives only through the new optional extra `github-app`; it is imported inside a function, never at module top level, and a missing extra raises an error naming `github-app`.
- One model, no fallback: `PRXREF_LLM_BACKEND=litellm`, `PRXREF_LLM_MODELS=bedrock/us.anthropic.claude-opus-4-6-v1`, `PRXREF_LLM_REASONING_EFFORT=medium`, `PRXREF_LLM_TEMPERATURE=1`, `PRXREF_LLM_SEED=off`, `PRXREF_LLM_TIMEOUT=900`, `PRXREF_LLM_MAX_TOKENS=32000`, `AWS_REGION_NAME=us-west-2`.
- No head code execution: prxref never runs repository content. Rules come from the base SHA; head content is data only.
- Credentials (`AWS_BEARER_TOKEN_BEDROCK`, the app private key, the webhook secrets, installation tokens) stay outside source, the SQLite file, run records and logs. The publisher logs request ids and status codes only.
- The advisory lane (`.github/workflows/prxref-review.yml`, `prxref serve`, `action.yml`) is unchanged. `prxref review` and `prxref serve` keep `PRXREF_RULES_SOURCE=head` (today's behavior) by default.
- The app never submits `APPROVE` or `REQUEST_CHANGES`; its review event is always `COMMENT`.
- GitHub only. Check name `sdlc-review`. App name `sdlc`. Annotations at most 50 per check-run update.
- Exit codes: `review` keeps exit 0 doctrine; `gate serve` exits 2 on a configuration error (missing/malformed key, missing extra, key file not 0600, `PRXREF_DRY_RUN` on).
- Every new config key follows `CLAUDE.md` "config lives in one place": `_DEFAULTS`, its type table, `_RANGES`/`_CHOICE_KEYS`, the `config.py` docstring, `.env.example`, `docs/env-vars.md` (row, category bullet, both totals), and `ENV_ONLY_KEYS` with its `docs/config-file.md` row.

## Review Focus

1. **`PRXREF_ALLOW_UNSIGNED=1` in the gate's environment with an unsigned or wrongly signed delivery** must still be 401 and write no row: `webhooks._check_hmac` returns `"unsigned"` under that switch (`src/prxref/webhooks.py:202 (_check_hmac)`), and the gate accepts only `"ok"`. Pinned by `test_allow_unsigned_does_not_open_the_gate` in Task 5.
2. **A correctly signed body that is not JSON** (UTF-8 BOM, invalid UTF-8, a JSON array, truncated JSON) must answer 400 and write no row, never a 500 or a half-written delivery. Pinned by `test_signed_garbage_bodies_are_400_without_a_row` in Task 5.
3. **A fork PR**: its `check_run.rerequested` payload has an empty `pull_requests` list, so the PR must be found through the store by head SHA, and a fork head that the gate never saw is ignored with 202, not crashed on. Pinned by `test_rerequest_on_fork_pr_uses_the_store_by_head_sha` and `test_rerequest_for_unknown_head_is_ignored` in Task 5.
4. **An installation token that expires (or is revoked) between check creation and completion** must cause one token refresh and one retry, not a failed publish and a check stuck `in_progress`. Pinned by `test_a_401_refreshes_the_token_once_and_retries` in Task 7.
5. **More than 50 file-level annotations** must be sent in batches of at most 50 (120 → 50, 50, 20), with the conclusion only on the last update. Pinned by `test_annotations_are_batched_by_fifty` in Task 7.

---

## File Structure

| Path | Responsibility | Task |
|---|---|---|
| `src/prxref/gate/__init__.py` | Package marker; `GateDependencyError`; `require_jwt()` | 1 |
| `src/prxref/gate/settings.py` | `GateSettings` from `cfg`; `load_env_file()` | 2 |
| `src/prxref/gate/app_auth.py` | App JWT, installation lookup, scoped installation tokens, bot login | 3 |
| `src/prxref/gate/store.py` | SQLite schema, every state transition, lease fencing | 4 |
| `src/prxref/gate/receiver.py` | `POST /webhook`, `GET /health`; verification, allowlist, dedup, event → intent | 5 |
| `src/prxref/gate/rules_source.py` | Materialize the base-pinned `.prxref.toml` and its path inputs | 6 |
| `src/prxref/orchestrator.py` | `rules_source` parameter: standards documents read at `pr.target_sha` under `base` | 6 |
| `src/prxref/gate/checks.py` | `conclusion_for`; `GitHubClient` (token refresh on 401); `Publisher` | 7 |
| `src/prxref/gate/binding.py` | Live binding guard before every write | 8 |
| `src/prxref/gate/worker.py` | `InstallationForge`, `CapturingForge`, `run_generation`, `Worker` loop with deadline | 9 |
| `src/prxref/cli.py` | `_run_review(forge=…)`; `rules_source` pass-through; `prxref gate …` subcommands | 6, 9, 12 |
| `src/prxref/gate/watchdog.py` | Re-queue orphaned generations; reconcile open PRs | 10 |
| `src/prxref/gate/rulesets.py` | Manifest and ruleset renderers; apply and diff via `gh` | 11 |
| `src/prxref/gate/serve.py` | Compose receiver + worker + watchdog threads | 12 |
| `docs/gate.md`, `README.md`, `CHANGELOG.md` | Runbook, section, changelog | 13 |
| `examples/launchd/*.plist`, `examples/launchd/prxref-gate.env.example` | launchd services and env template | 14 |
| `docs/live-instance-verification/sdlc-gate/` | Sandbox capture files | 15 |
| version files | 0.35.0 release | 16 |

Tests: one file per component under `tests/gate/`. The repo's `tests/` is a package (`tests/__init__.py` exists), so `tests/gate/__init__.py` is required (Task 1). The autouse fixture `tests/conftest.py:60 (_clear_prxref_env)` clears every `_DEFAULTS`-derived `PRXREF_*` name before each test, so every new `PRXREF_GATE_*` key is cleared automatically once Task 2 adds it to `_DEFAULTS`; tests set what they need with `monkeypatch.setenv`.

Fixture style copied throughout: fakes are small hand-written classes in the test module (as `tests/test_orchestrator.py:199 (FakeForge)` and `tests/test_llm_backends.py:63 (_ScriptedSession)` do: queued responses, recorded calls), never `unittest.mock` call chains.

---

### Task 1: Package scaffolding and the `github-app` extra

**Files:**
- Create: `src/prxref/gate/__init__.py`
- Create: `tests/gate/__init__.py` (empty)
- Create: `tests/gate/test_extra.py`
- Modify: `pyproject.toml` (`[project.optional-dependencies]`, `[dependency-groups] dev`)
- Modify: `uv.lock` (regenerated by `uv lock`)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class GateDependencyError(ImportError)` in `prxref.gate`
  - `def require_jwt() -> types.ModuleType` in `prxref.gate`: returns the `jwt` module or raises `GateDependencyError` whose message contains `github-app`.
  - `EXTRA_HINT: str` in `prxref.gate` = `"install the github-app extra: pip install 'prxref[github-app]'"`

- [ ] **Step 1: Write the failing test**

`tests/gate/__init__.py` is an empty file. `tests/gate/test_extra.py`:

```python
"""The github-app extra: PyJWT is optional and its absence is named (#102)."""

import builtins
import sys

import pytest

from prxref import gate


def test_require_jwt_returns_the_module_when_installed():
    jwt = gate.require_jwt()
    assert hasattr(jwt, "encode")


def test_require_jwt_names_the_extra_when_missing(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "jwt" or name.startswith("jwt."):
            raise ImportError("No module named 'jwt'")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "jwt", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(gate.GateDependencyError) as info:
        gate.require_jwt()
    assert "github-app" in str(info.value)
    assert isinstance(info.value, ImportError)


def test_importing_the_package_does_not_import_jwt(monkeypatch):
    monkeypatch.delitem(sys.modules, "prxref.gate", raising=False)
    monkeypatch.delitem(sys.modules, "jwt", raising=False)
    __import__("prxref.gate")
    assert "jwt" not in sys.modules
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_extra.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'prxref.gate'`.

- [ ] **Step 3: Add the extra and the dev dependency, then lock**

In `pyproject.toml`, under `[project.optional-dependencies]` (beside the existing `litellm = ["litellm>=1.40"]`), add:

```toml
github-app = ["pyjwt[crypto]>=2.8"]
```

In the `dev` dependency group (it holds `pytest`, `pytest-cov`, `ruff`), add `"pyjwt[crypto]>=2.8"` so the gate tests run in the default `uv run` environment.

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv lock && uv sync`
Expected: `uv.lock` gains `pyjwt` and `cryptography` entries; `uv run python -c "import jwt, cryptography; print('ok')"` prints `ok`.

- [ ] **Step 4: Write the package**

`src/prxref/gate/__init__.py`:

```python
"""The sdlc review gate (#102): a local GitHub App whose check run gates merges.

``prxref gate serve`` runs the receiver, the worker and the watchdog over one
SQLite file. Nothing in this package imports PyJWT at module level; the app's
JWT is built through :func:`require_jwt`, so ``prxref`` without the
``github-app`` extra still imports, and the gate names the extra it lacks.
"""

from __future__ import annotations

import types

EXTRA_HINT = "install the github-app extra: pip install 'prxref[github-app]'"


class GateDependencyError(ImportError):
    """PyJWT (with its crypto backend) is not installed."""


def require_jwt() -> types.ModuleType:
    """Return the ``jwt`` module, or raise :class:`GateDependencyError` naming the extra."""
    try:
        import jwt
    except ImportError as e:
        raise GateDependencyError(f"prxref gate needs PyJWT with RS256 support; {EXTRA_HINT}") from e
    return jwt
```

- [ ] **Step 5: Run the tests and lint**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_extra.py -q && uv run ruff check src tests`
Expected: `3 passed`; ruff `All checks passed!`.

- [ ] **Step 6: Commit**

```bash
cd ~/code/prxref/.claude/worktrees/sdlc-gate
git add src/prxref/gate/__init__.py tests/gate/__init__.py tests/gate/test_extra.py pyproject.toml uv.lock
git commit -m "feat(gate): package scaffold and the github-app extra (#102)" -m "Claude-Session-Id: <session UUID>"
```

---

### Task 2: Gate config keys, `PRXREF_RULES_SOURCE`, and `GateSettings`

All thirteen new keys land in one task, because `tests/test_docs_consistency.py` requires every `PRXREF_` name documented anywhere to be a `_DEFAULTS` key, and the totals in `docs/env-vars.md` are tested; adding them piecemeal leaves the suite red between tasks.

| Key (`_DEFAULTS` name) | Env var | Default | Type table | Range / choice | Env-only reason |
|---|---|---|---|---|---|
| `rules_source` | `PRXREF_RULES_SOURCE` | `"head"` | — | `_CHOICE_KEYS {"head","base"}` | gate |
| `gate_app_id` | `PRXREF_GATE_APP_ID` | `""` | — | digits, checked in `GateSettings` | credential |
| `gate_private_key_file` | `PRXREF_GATE_PRIVATE_KEY_FILE` | `""` | — | mode 0600, checked in `GateSettings` | credential |
| `gate_installation_ids` | `PRXREF_GATE_INSTALLATION_IDS` | `[]` | `_LIST_KEYS` | integers, checked in `GateSettings` | gate |
| `gate_repos` | `PRXREF_GATE_REPOS` | `[]` | `_LIST_KEYS` | `owner/name`, checked in `GateSettings` | gate |
| `gate_webhook_secret` | `PRXREF_GATE_WEBHOOK_SECRET` | `""` | — | non-empty in `GateSettings` | credential |
| `gate_webhook_secret_previous` | `PRXREF_GATE_WEBHOOK_SECRET_PREVIOUS` | `""` | — | — | credential |
| `gate_secret_grace_minutes` | `PRXREF_GATE_SECRET_GRACE_MINUTES` | `30` | `_INT_KEYS` | `_Range(0, low_inclusive=True)` | gate |
| `gate_state_file` | `PRXREF_GATE_STATE_FILE` | `""` | — | non-empty in `GateSettings` | local write |
| `gate_runs_dir` | `PRXREF_GATE_RUNS_DIR` | `""` | — | non-empty in `GateSettings` | local write |
| `gate_review_deadline` | `PRXREF_GATE_REVIEW_DEADLINE` | `900` (seconds) | `_INT_KEYS` | `_Range(0)` | gate |
| `gate_watchdog_seconds` | `PRXREF_GATE_WATCHDOG_SECONDS` | `60` | `_INT_KEYS` | `_Range(0)` | gate |
| `gate_reconcile_minutes` | `PRXREF_GATE_RECONCILE_MINUTES` | `10` | `_INT_KEYS` | `_Range(0)` | gate |

Counts after this task: 96 + 13 = **109** configuration keys; 98 + 13 = **111** accepted variable names. Re-derive before editing the docs: `uv run python -c "from prxref import config; print(len(config._DEFAULTS))"` must print `109`.

**Files:**
- Modify: `src/prxref/config.py`: `_DEFAULTS` (append after `"allow_unsigned": False,` near `:975`), `_INT_KEYS` (`:978`), `_LIST_KEYS` (`:998`), `_CHOICE_KEYS` (`:1010`), `_RANGES` (`:1103`), `_ENV_ONLY_REASONS` (`:1188`), and the module docstring (new `Gate:` block after the `Webhooks:` block at `:708`; add `PRXREF_RULES_SOURCE` to the same block).
- Modify: `.env.example` (new `# Gate Configuration` section after the Webhooks section ending at `PRXREF_ALLOW_UNSIGNED=0`, `:691`).
- Modify: `docs/env-vars.md` (a new `## Gate` table after the webhook rows `:126-127`; the totals at `:204` and `:211`; a new category bullet after `- **Webhooks (6):**` at `:209`).
- Modify: `docs/config-file.md` (thirteen rows in the env-only table, which ends with `| \`allow_unsigned\` | gate | … |` at `:301`).
- Create: `src/prxref/gate/settings.py`
- Test: `tests/gate/test_settings.py`

**Interfaces:**
- Consumes: `prxref.config.load_config(*, config_file=None, **overrides) -> dict` (`src/prxref/config.py:1722 (load_config)`), `prxref.llm.ConfigError` (`src/prxref/llm.py:17`).
- Produces:
  - `cfg["rules_source"]: str` in `{"head","base"}` and every `cfg["gate_*"]` key above.
  - `@dataclass(frozen=True) class GateSettings` with fields `app_id: int`, `private_key_file: Path`, `installation_ids: frozenset[int]`, `repos: frozenset[str]` (lower-cased `owner/name`), `secret_grace_minutes: int`, `state_file: Path`, `runs_dir: Path`, `review_deadline: int`, `watchdog_seconds: int`, `reconcile_minutes: int`.
  - `def settings_from_config(cfg: Mapping[str, Any]) -> GateSettings` (raises `ConfigError` naming the env var).
  - `def load_env_file(path: Path) -> dict[str, str]` (raises `ConfigError` if the file is not mode 0600 or a line is malformed; returns the parsed pairs; does not touch `os.environ`).
  - `def apply_env_file(path: Path) -> list[str]` (calls `load_env_file`, sets each pair in `os.environ`, returns the key names set, never the values).
  - `WEBHOOK_SECRET_ENV = "PRXREF_GATE_WEBHOOK_SECRET"`, `WEBHOOK_SECRET_PREVIOUS_ENV = "PRXREF_GATE_WEBHOOK_SECRET_PREVIOUS"`.

- [ ] **Step 1: Write the failing test**

`tests/gate/test_settings.py`:

```python
"""Gate config keys and GateSettings (#102)."""

import os
from pathlib import Path

import pytest

from prxref import config
from prxref.gate import settings
from prxref.llm import ConfigError

GATE_KEYS = {
    "rules_source", "gate_app_id", "gate_private_key_file", "gate_installation_ids",
    "gate_repos", "gate_webhook_secret", "gate_webhook_secret_previous",
    "gate_secret_grace_minutes", "gate_state_file", "gate_runs_dir",
    "gate_review_deadline", "gate_watchdog_seconds", "gate_reconcile_minutes",
}


def _key_file(tmp_path: Path, mode: int = 0o600) -> Path:
    path = tmp_path / "app.pem"
    path.write_text("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
    path.chmod(mode)
    return path


def _env(monkeypatch, tmp_path, **extra):
    values = {
        "PRXREF_GATE_APP_ID": "12345",
        "PRXREF_GATE_PRIVATE_KEY_FILE": str(_key_file(tmp_path)),
        "PRXREF_GATE_INSTALLATION_IDS": "777,778",
        "PRXREF_GATE_REPOS": "SBlattJ/sdlc-sandbox",
        "PRXREF_GATE_WEBHOOK_SECRET": "s3cret",
        "PRXREF_GATE_STATE_FILE": str(tmp_path / "gate.sqlite3"),
        "PRXREF_GATE_RUNS_DIR": str(tmp_path / "runs"),
    }
    values.update(extra)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_every_gate_key_is_a_default_and_env_only():
    assert GATE_KEYS <= set(config._DEFAULTS)
    assert GATE_KEYS <= config.ENV_ONLY_KEYS
    assert not GATE_KEYS & set(config.FILE_KEYS)


def test_defaults():
    cfg = config.load_config()
    assert cfg["rules_source"] == "head"
    assert cfg["gate_secret_grace_minutes"] == 30
    assert cfg["gate_review_deadline"] == 900
    assert cfg["gate_watchdog_seconds"] == 60
    assert cfg["gate_reconcile_minutes"] == 10
    assert cfg["gate_installation_ids"] == []


def test_rules_source_rejects_a_value_outside_its_vocabulary(monkeypatch):
    monkeypatch.setenv("PRXREF_RULES_SOURCE", "merge")
    with pytest.raises(ConfigError, match="PRXREF_RULES_SOURCE"):
        config.load_config()


def test_review_deadline_must_be_positive(monkeypatch):
    monkeypatch.setenv("PRXREF_GATE_REVIEW_DEADLINE", "0")
    with pytest.raises(ConfigError, match="PRXREF_GATE_REVIEW_DEADLINE"):
        config.load_config()


def test_settings_from_a_complete_environment(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    s = settings.settings_from_config(config.load_config())
    assert s.app_id == 12345
    assert s.installation_ids == frozenset({777, 778})
    assert s.repos == frozenset({"sblattj/sdlc-sandbox"})
    assert s.review_deadline == 900
    assert s.state_file == tmp_path / "gate.sqlite3"


@pytest.mark.parametrize("name, value", [
    ("PRXREF_GATE_APP_ID", ""),
    ("PRXREF_GATE_APP_ID", "sdlc"),
    ("PRXREF_GATE_INSTALLATION_IDS", ""),
    ("PRXREF_GATE_INSTALLATION_IDS", "77x"),
    ("PRXREF_GATE_REPOS", ""),
    ("PRXREF_GATE_REPOS", "no-slash"),
    ("PRXREF_GATE_WEBHOOK_SECRET", ""),
    ("PRXREF_GATE_STATE_FILE", ""),
    ("PRXREF_GATE_RUNS_DIR", ""),
    ("PRXREF_GATE_PRIVATE_KEY_FILE", "/nonexistent/app.pem"),
])
def test_a_missing_or_malformed_value_names_its_variable(monkeypatch, tmp_path, name, value):
    _env(monkeypatch, tmp_path, **{name: value})
    with pytest.raises(ConfigError, match=name):
        settings.settings_from_config(config.load_config())


def test_a_group_or_world_readable_key_file_is_refused(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, PRXREF_GATE_PRIVATE_KEY_FILE=str(_key_file(tmp_path, 0o644)))
    with pytest.raises(ConfigError, match="0600"):
        settings.settings_from_config(config.load_config())


def test_env_file_is_parsed_and_mode_checked(tmp_path):
    env = tmp_path / "env"
    env.write_text(
        "# comment\n\nexport PRXREF_GATE_APP_ID=12345\nAWS_REGION_NAME='us-west-2'\n"
        'PRXREF_LLM_MODELS="bedrock/us.anthropic.claude-opus-4-6-v1"\n'
    )
    env.chmod(0o600)
    assert settings.load_env_file(env) == {
        "PRXREF_GATE_APP_ID": "12345",
        "AWS_REGION_NAME": "us-west-2",
        "PRXREF_LLM_MODELS": "bedrock/us.anthropic.claude-opus-4-6-v1",
    }
    env.chmod(0o640)
    with pytest.raises(ConfigError, match="0600"):
        settings.load_env_file(env)


def test_env_file_rejects_a_line_without_equals(tmp_path):
    env = tmp_path / "env"
    env.write_text("JUSTANAME\n")
    env.chmod(0o600)
    with pytest.raises(ConfigError, match="line 1"):
        settings.load_env_file(env)


def test_apply_env_file_returns_names_not_values(monkeypatch, tmp_path):
    env = tmp_path / "env"
    env.write_text("PRXREF_GATE_WEBHOOK_SECRET=topsecret\n")
    env.chmod(0o600)
    monkeypatch.delenv("PRXREF_GATE_WEBHOOK_SECRET", raising=False)
    names = settings.apply_env_file(env)
    assert names == ["PRXREF_GATE_WEBHOOK_SECRET"]
    assert os.environ["PRXREF_GATE_WEBHOOK_SECRET"] == "topsecret"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_settings.py -q`
Expected: FAIL (`ImportError: cannot import name 'settings'`, and `GATE_KEYS <= set(config._DEFAULTS)` false).

- [ ] **Step 3: Add the keys to `src/prxref/config.py`**

Append to `_DEFAULTS` after `"allow_unsigned": False,`:

```python
    "rules_source": "head",
    "gate_app_id": "",
    "gate_private_key_file": "",
    "gate_installation_ids": [],
    "gate_repos": [],
    "gate_webhook_secret": "",
    "gate_webhook_secret_previous": "",
    "gate_secret_grace_minutes": 30,
    "gate_state_file": "",
    "gate_runs_dir": "",
    "gate_review_deadline": 900,
    "gate_watchdog_seconds": 60,
    "gate_reconcile_minutes": 10,
```

Add `"gate_secret_grace_minutes", "gate_review_deadline", "gate_watchdog_seconds", "gate_reconcile_minutes",` to `_INT_KEYS`; `"gate_installation_ids", "gate_repos",` to `_LIST_KEYS`; `"rules_source": frozenset({"head", "base"}),` to `_CHOICE_KEYS`; to `_RANGES`:

```python
    "gate_secret_grace_minutes": _Range(0, low_inclusive=True),
    "gate_review_deadline": _Range(0),
    "gate_watchdog_seconds": _Range(0),
    "gate_reconcile_minutes": _Range(0),
```

and to `_ENV_ONLY_REASONS`:

```python
    "rules_source": "gate",
    "gate_app_id": "credential",
    "gate_private_key_file": "credential",
    "gate_installation_ids": "gate",
    "gate_repos": "gate",
    "gate_webhook_secret": "credential",
    "gate_webhook_secret_previous": "credential",
    "gate_secret_grace_minutes": "gate",
    "gate_state_file": "local write",
    "gate_runs_dir": "local write",
    "gate_review_deadline": "gate",
    "gate_watchdog_seconds": "gate",
    "gate_reconcile_minutes": "gate",
```

Append to the module docstring after the `Webhooks:` block (same two-column layout, names padded to column 34):

```text
Gate (prxref gate serve, #102):
  PRXREF_RULES_SOURCE             where repository rules are read: head
                                  (default for review and serve) or base
                                  (the gate's default)
  PRXREF_GATE_APP_ID              the sdlc GitHub App's numeric id
  PRXREF_GATE_PRIVATE_KEY_FILE    the app's PEM private key (mode 0600)
  PRXREF_GATE_INSTALLATION_IDS    comma-separated installation id allowlist
  PRXREF_GATE_REPOS               comma-separated owner/name allowlist
  PRXREF_GATE_WEBHOOK_SECRET      the app's webhook secret
  PRXREF_GATE_WEBHOOK_SECRET_PREVIOUS
                                  the old secret, accepted during rotation
  PRXREF_GATE_SECRET_GRACE_MINUTES
                                  minutes the old secret stays valid (30)
  PRXREF_GATE_STATE_FILE          the gate's SQLite file
  PRXREF_GATE_RUNS_DIR            directory for per-generation run records
  PRXREF_GATE_REVIEW_DEADLINE     seconds one review may take (900)
  PRXREF_GATE_WATCHDOG_SECONDS    watchdog interval in seconds (60)
  PRXREF_GATE_RECONCILE_MINUTES   open-PR reconcile interval (10)
```

- [ ] **Step 4: Write `src/prxref/gate/settings.py`**

```python
"""The gate's resolved settings and its mode-0600 environment file (#102)."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prxref.llm import ConfigError

WEBHOOK_SECRET_ENV = "PRXREF_GATE_WEBHOOK_SECRET"
WEBHOOK_SECRET_PREVIOUS_ENV = "PRXREF_GATE_WEBHOOK_SECRET_PREVIOUS"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ENV_LINE_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


@dataclass(frozen=True)
class GateSettings:
    """Everything ``prxref gate serve`` needs beyond the review config."""

    app_id: int
    private_key_file: Path
    installation_ids: frozenset[int]
    repos: frozenset[str]
    secret_grace_minutes: int
    state_file: Path
    runs_dir: Path
    review_deadline: int
    watchdog_seconds: int
    reconcile_minutes: int


def _require_private(path: Path, name: str) -> None:
    try:
        mode = path.stat().st_mode
    except OSError as e:
        raise ConfigError(f"{name}: cannot read {path}: {e.strerror}") from e
    if stat.S_IMODE(mode) & 0o077:
        raise ConfigError(
            f"{name}: {path} must be mode 0600 (it is {stat.S_IMODE(mode):04o}); run chmod 600 {path}"
        )


def settings_from_config(cfg: Mapping[str, Any]) -> GateSettings:
    """Validate the gate keys of a loaded ``cfg``; raise ``ConfigError`` naming the variable."""
    app_id = str(cfg["gate_app_id"]).strip()
    if not app_id.isdigit():
        raise ConfigError("PRXREF_GATE_APP_ID must be the app's numeric id")
    key_file = str(cfg["gate_private_key_file"]).strip()
    if not key_file:
        raise ConfigError("PRXREF_GATE_PRIVATE_KEY_FILE is required")
    key_path = Path(key_file).expanduser()
    _require_private(key_path, "PRXREF_GATE_PRIVATE_KEY_FILE")
    raw_ids = list(cfg["gate_installation_ids"])
    if not raw_ids or not all(v.strip().isdigit() for v in raw_ids):
        raise ConfigError("PRXREF_GATE_INSTALLATION_IDS must list one or more numeric installation ids")
    repos = [v.strip() for v in cfg["gate_repos"]]
    if not repos or not all(_REPO_RE.match(r) for r in repos):
        raise ConfigError("PRXREF_GATE_REPOS must list one or more owner/name repositories")
    if not str(cfg["gate_webhook_secret"]):
        raise ConfigError("PRXREF_GATE_WEBHOOK_SECRET is required")
    for key, name in (("gate_state_file", "PRXREF_GATE_STATE_FILE"), ("gate_runs_dir", "PRXREF_GATE_RUNS_DIR")):
        if not str(cfg[key]).strip():
            raise ConfigError(f"{name} is required")
    return GateSettings(
        app_id=int(app_id),
        private_key_file=key_path,
        installation_ids=frozenset(int(v) for v in raw_ids),
        repos=frozenset(r.lower() for r in repos),
        secret_grace_minutes=int(cfg["gate_secret_grace_minutes"]),
        state_file=Path(str(cfg["gate_state_file"])).expanduser(),
        runs_dir=Path(str(cfg["gate_runs_dir"])).expanduser(),
        review_deadline=int(cfg["gate_review_deadline"]),
        watchdog_seconds=int(cfg["gate_watchdog_seconds"]),
        reconcile_minutes=int(cfg["gate_reconcile_minutes"]),
    )


def load_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines of a mode-0600 file; ``#`` comments, blanks and ``export`` allowed."""
    _require_private(path, "--env-file")
    pairs: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        m = _ENV_LINE_RE.match(text)
        if not m:
            raise ConfigError(f"--env-file {path}: line {number} is not KEY=VALUE")
        value = m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        pairs[m.group(1)] = value
    return pairs


def apply_env_file(path: Path) -> list[str]:
    """Load ``path`` into ``os.environ``; return the names set (never the values)."""
    pairs = load_env_file(path)
    for name, value in pairs.items():
        os.environ[name] = value
    return list(pairs)
```

- [ ] **Step 5: Update `.env.example`, `docs/env-vars.md`, `docs/config-file.md`**

`.env.example`, after `PRXREF_ALLOW_UNSIGNED=0`:

```bash

# ------------------------------------------------------------------------------
# Gate Configuration (prxref gate serve, #102; env-only)
# ------------------------------------------------------------------------------

# Where repository rules are read: head (default for review and serve) or
# base (the gate's default; prxref gate serve sets it when unset)
# PRXREF_RULES_SOURCE=head

# The sdlc GitHub App's numeric id and its PEM private key (mode 0600)
# PRXREF_GATE_APP_ID=
# PRXREF_GATE_PRIVATE_KEY_FILE=

# Comma-separated allowlists: installation ids and owner/name repositories
# PRXREF_GATE_INSTALLATION_IDS=
# PRXREF_GATE_REPOS=

# The app's webhook secret, and the previous one during a rotation
# PRXREF_GATE_WEBHOOK_SECRET=
# PRXREF_GATE_WEBHOOK_SECRET_PREVIOUS=
PRXREF_GATE_SECRET_GRACE_MINUTES=30

# The gate's SQLite file and per-generation run-record directory
# PRXREF_GATE_STATE_FILE=
# PRXREF_GATE_RUNS_DIR=

# Seconds one review may take; watchdog seconds; reconcile minutes
PRXREF_GATE_REVIEW_DEADLINE=900
PRXREF_GATE_WATCHDOG_SECONDS=60
PRXREF_GATE_RECONCILE_MINUTES=10
```

`docs/env-vars.md`: after the webhook table (the row for `PRXREF_ALLOW_UNSIGNED` at `:127`), add a `## Gate` section with a three-column table (`| Variable | Default | Description |`, the same columns as the rows above) holding one row per key in the table at the top of this task; each description states the type, the default, the validation from Step 4, and "Env-only." Change `**96** configuration keys` (`:204`) to `**109**`; add after the `- **Webhooks (6):**` bullet:

```markdown
- **Gate (13):** `PRXREF_RULES_SOURCE`, `PRXREF_GATE_APP_ID`, `PRXREF_GATE_PRIVATE_KEY_FILE`, `PRXREF_GATE_INSTALLATION_IDS`, `PRXREF_GATE_REPOS`, `PRXREF_GATE_WEBHOOK_SECRET`, `PRXREF_GATE_WEBHOOK_SECRET_PREVIOUS`, `PRXREF_GATE_SECRET_GRACE_MINUTES`, `PRXREF_GATE_STATE_FILE`, `PRXREF_GATE_RUNS_DIR`, `PRXREF_GATE_REVIEW_DEADLINE`, `PRXREF_GATE_WATCHDOG_SECONDS`, `PRXREF_GATE_RECONCILE_MINUTES`
```

and in the italic line at `:211` change `96 configuration keys` to `109 configuration keys` and `98 accepted variable names` to `111 accepted variable names`.

`docs/config-file.md`: append after the `allow_unsigned` row (`:301`):

```markdown
| `rules_source` | gate | Chooses whether repository rules come from the PR's base or its head. |
| `gate_app_id` | credential | The gate app's identity. |
| `gate_private_key_file` | credential | The gate app's private key. |
| `gate_installation_ids` | gate | Which app installations the gate serves. |
| `gate_repos` | gate | Which repositories the gate serves. |
| `gate_webhook_secret` | credential | The gate app's webhook signing secret. |
| `gate_webhook_secret_previous` | credential | The previous webhook secret during a rotation. |
| `gate_secret_grace_minutes` | gate | How long the previous webhook secret stays valid. |
| `gate_state_file` | local write | The gate's SQLite state file. |
| `gate_runs_dir` | local write | Where the gate writes run records. |
| `gate_review_deadline` | gate | How long a gate review may run before it fails. |
| `gate_watchdog_seconds` | gate | The gate's repair interval. |
| `gate_reconcile_minutes` | gate | The gate's open-PR reconcile interval. |
```

- [ ] **Step 6: Run the tests, the doc-consistency tests and the full suite**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_settings.py tests/test_docs_consistency.py tests/test_issue_38_config_docs.py tests/test_config.py -q`
Expected: all pass, `tests/gate/test_settings.py` contributing `19 passed` (10 functions, parametrized). If a doc test pins a wording or count this step did not anticipate, take the expected value from the failing assertion and fix the doc, never the test.
Then the full suite with `timeout: 300000`: `uv run pytest -q` → `11680 + N passed` where N is this task's new tests (plus any parametrized doc tests that grew by the 13 names), `0 failed`. Then `uv run ruff check src tests`.

- [ ] **Step 7: Commit**

```bash
cd ~/code/prxref/.claude/worktrees/sdlc-gate
git add src/prxref/config.py src/prxref/gate/settings.py tests/gate/test_settings.py .env.example docs/env-vars.md docs/config-file.md
git commit -m "feat(gate): PRXREF_GATE_* keys, PRXREF_RULES_SOURCE and GateSettings (#102)" -m "Claude-Session-Id: <session UUID>"
```

---

### Task 3: `app_auth.py`: app JWT and scoped installation tokens

**Files:**
- Create: `src/prxref/gate/app_auth.py`
- Test: `tests/gate/test_app_auth.py`

**Interfaces:**
- Consumes: `prxref.gate.require_jwt()` (Task 1); `GateSettings.app_id`, `.private_key_file`, `.installation_ids` (Task 2).
- Produces:
  - `class GateAuthError(RuntimeError)`
  - `TOKEN_PERMISSIONS: dict[str, str]` = `{"checks": "write", "pull_requests": "write", "contents": "read", "metadata": "read"}`
  - `class AppAuth` with `__init__(self, app_id: int, private_key_pem: str, installation_ids: frozenset[int], *, session: requests.Session | None = None, clock: Callable[[], float] = time.time, api: str = "https://api.github.com")`
  - `AppAuth.app_jwt(self) -> str`
  - `AppAuth.installation_for(self, repo: str) -> int` (`repo` is `owner/name`; raises `GateAuthError` when GitHub's installation is not in the allowlist)
  - `AppAuth.token_for(self, repo: str) -> str` (cached until 5 minutes before expiry)
  - `AppAuth.invalidate(self, repo: str) -> None`
  - `AppAuth.bot_login(self) -> str` (`"<slug>[bot]"`, cached)

- [ ] **Step 1: Write the failing test**

`tests/gate/test_app_auth.py`:

```python
"""App JWT, installation lookup and scoped installation tokens (#102)."""

import json

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from prxref.gate import app_auth


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.headers = {"x-github-request-id": "REQ1"}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}", response=self)


class _Session:
    """Queued responses, recorded calls (the tests/test_llm_backends.py:63 _ScriptedSession style)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _auth(keypair, session, clock):
    return app_auth.AppAuth(12345, keypair[0], frozenset({777}), session=session, clock=clock)


def test_jwt_is_rs256_with_backdated_iat_and_ten_minute_exp(keypair):
    auth = _auth(keypair, _Session([]), clock=lambda: 1_000_000.0)
    claims = jwt.decode(auth.app_jwt(), keypair[1], algorithms=["RS256"], options={"verify_exp": False})
    assert claims == {"iss": "12345", "iat": 1_000_000 - 60, "exp": 1_000_000 + 600}


def test_token_is_minted_scoped_to_one_repo_and_four_permissions(keypair):
    session = _Session([
        _Resp(200, {"id": 777}),
        _Resp(201, {"token": "ghs_one", "expires_at": "1970-01-12T14:46:40Z"}),
    ])
    auth = _auth(keypair, session, clock=lambda: 1_000_000.0)
    assert auth.token_for("acme/widget") == "ghs_one"
    method, url, kwargs = session.calls[1]
    assert (method, url) == ("POST", "https://api.github.com/app/installations/777/access_tokens")
    assert kwargs["json"] == {"repositories": ["widget"], "permissions": app_auth.TOKEN_PERMISSIONS}
    assert kwargs["headers"]["Authorization"].startswith("Bearer ey")


def test_token_is_cached_until_five_minutes_before_expiry(keypair):
    now = [1_000_000.0]
    expires = "1970-01-12T14:46:40Z"
    session = _Session([
        _Resp(200, {"id": 777}),
        _Resp(201, {"token": "ghs_one", "expires_at": expires}),
        _Resp(201, {"token": "ghs_two", "expires_at": "1970-01-12T15:46:40Z"}),
    ])
    auth = _auth(keypair, session, clock=lambda: now[0])
    assert auth.token_for("acme/widget") == "ghs_one"
    now[0] = 1_000_000.0 + 3600 - 301
    assert auth.token_for("acme/widget") == "ghs_one"
    now[0] = 1_000_000.0 + 3600 - 299
    assert auth.token_for("acme/widget") == "ghs_two"
    assert len(session.calls) == 3


def test_invalidate_forces_a_new_token(keypair):
    session = _Session([
        _Resp(200, {"id": 777}),
        _Resp(201, {"token": "ghs_one", "expires_at": "1970-01-12T14:46:40Z"}),
        _Resp(201, {"token": "ghs_two", "expires_at": "1970-01-12T14:46:40Z"}),
    ])
    auth = _auth(keypair, session, clock=lambda: 1_000_000.0)
    auth.token_for("acme/widget")
    auth.invalidate("acme/widget")
    assert auth.token_for("acme/widget") == "ghs_two"


def test_an_installation_outside_the_allowlist_is_refused(keypair):
    auth = _auth(keypair, _Session([_Resp(200, {"id": 999})]), clock=lambda: 1_000_000.0)
    with pytest.raises(app_auth.GateAuthError, match="999"):
        auth.token_for("acme/widget")


def test_bot_login_is_the_slug_plus_bot(keypair):
    session = _Session([_Resp(200, {"slug": "sdlc", "id": 12345})])
    auth = _auth(keypair, session, clock=lambda: 1_000_000.0)
    assert auth.bot_login() == "sdlc[bot]"
    assert auth.bot_login() == "sdlc[bot]"
    assert len(session.calls) == 1


def test_no_token_text_reaches_an_error_message(keypair):
    session = _Session([_Resp(200, {"id": 777}), _Resp(500, {"message": "boom"})])
    auth = _auth(keypair, session, clock=lambda: 1_000_000.0)
    with pytest.raises(app_auth.GateAuthError) as info:
        auth.token_for("acme/widget")
    assert "Bearer" not in str(info.value)
    assert "REQ1" in str(info.value)
    assert json.dumps(str(info.value))
```

`expires_at` arithmetic: `1970-01-12T14:46:40Z` is epoch `1_000_000 + 3600` = `1_003_600` (11 days = 950 400 s; 14 h 46 min 40 s = 53 200 s; total 1 003 600). Re-derive with `uv run python -c "import datetime;print(datetime.datetime.fromtimestamp(1003600, datetime.UTC).isoformat())"`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_app_auth.py -q`
Expected: FAIL with `ImportError: cannot import name 'app_auth'`.

- [ ] **Step 3: Write `src/prxref/gate/app_auth.py`**

```python
"""GitHub App authentication for the gate: the app JWT and installation tokens (#102).

Tokens are minted per repository, scoped at mint to that one repository and
to the four permissions in :data:`TOKEN_PERMISSIONS`, and cached until five
minutes before their one-hour expiry. No token or key text is ever logged or
put in an exception message; errors carry GitHub's request id and status.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime

import requests

from prxref.gate import require_jwt

TOKEN_PERMISSIONS = {"checks": "write", "pull_requests": "write", "contents": "read", "metadata": "read"}
_REFRESH_MARGIN_S = 300
_TIMEOUT_S = 10


class GateAuthError(RuntimeError):
    """An installation lookup or token mint failed, or was refused by the allowlist."""


class AppAuth:
    """Mint and cache installation tokens for the ``sdlc`` app."""

    def __init__(
        self,
        app_id: int,
        private_key_pem: str,
        installation_ids: frozenset[int],
        *,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
        api: str = "https://api.github.com",
    ) -> None:
        self._app_id = app_id
        self._pem = private_key_pem
        self._allowed = installation_ids
        self._session = session or requests.Session()
        self._clock = clock
        self._api = api.rstrip("/")
        self._lock = threading.Lock()
        self._installations: dict[str, int] = {}
        self._tokens: dict[str, tuple[str, float]] = {}
        self._bot_login: str | None = None

    def app_jwt(self) -> str:
        """An RS256 app JWT: ``iat`` 60 s in the past, ``exp`` 10 minutes ahead."""
        jwt = require_jwt()
        now = int(self._clock())
        payload = {"iat": now - 60, "exp": now + 600, "iss": str(self._app_id)}
        return jwt.encode(payload, self._pem, algorithm="RS256")

    def _call(self, method: str, path: str, **kwargs) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.app_jwt()}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            resp = self._session.request(
                method, f"{self._api}{path}", headers=headers, timeout=_TIMEOUT_S, **kwargs,
            )
        except requests.RequestException as e:
            raise GateAuthError(f"{method} {path} failed: {e.__class__.__name__}") from None
        if resp.status_code >= 400:
            request_id = resp.headers.get("x-github-request-id", "?")
            raise GateAuthError(f"{method} {path} returned {resp.status_code} (request id {request_id})")
        return resp.json()

    def installation_for(self, repo: str) -> int:
        """The installation id for ``owner/name``; refused unless it is allowlisted."""
        key = repo.lower()
        with self._lock:
            if key in self._installations:
                return self._installations[key]
        installation = int(self._call("GET", f"/repos/{repo}/installation")["id"])
        if installation not in self._allowed:
            raise GateAuthError(
                f"{repo} is installed as {installation}, which PRXREF_GATE_INSTALLATION_IDS does not list"
            )
        with self._lock:
            self._installations[key] = installation
        return installation

    def token_for(self, repo: str) -> str:
        """A token for ``owner/name`` scoped to that repository and :data:`TOKEN_PERMISSIONS`."""
        key = repo.lower()
        now = self._clock()
        with self._lock:
            cached = self._tokens.get(key)
            if cached is not None and cached[1] - _REFRESH_MARGIN_S > now:
                return cached[0]
        installation = self.installation_for(repo)
        data = self._call(
            "POST", f"/app/installations/{installation}/access_tokens",
            json={"repositories": [repo.split("/", 1)[1]], "permissions": TOKEN_PERMISSIONS},
        )
        expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        with self._lock:
            self._tokens[key] = (data["token"], expires)
        return data["token"]

    def invalidate(self, repo: str) -> None:
        """Drop the cached token for ``repo`` (after a 401)."""
        with self._lock:
            self._tokens.pop(repo.lower(), None)

    def bot_login(self) -> str:
        """The app's bot login, ``<slug>[bot]``, read once from ``GET /app``."""
        if self._bot_login is None:
            self._bot_login = f"{self._call('GET', '/app')['slug']}[bot]"
        return self._bot_login
```

- [ ] **Step 4: Run the tests and lint**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_app_auth.py -q && uv run ruff check src tests`
Expected: `7 passed`; ruff clean.

- [ ] **Step 5: Commit**

```bash
cd ~/code/prxref/.claude/worktrees/sdlc-gate
git add src/prxref/gate/app_auth.py tests/gate/test_app_auth.py
git commit -m "feat(gate): app JWT and repo-scoped installation tokens (#102)" -m "Claude-Session-Id: <session UUID>"
```

---

### Task 4: `store.py`: durable state with a fenced lease

**Files:**
- Create: `src/prxref/gate/store.py`
- Test: `tests/gate/test_store.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces (all in `prxref.gate.store`):
  - `STATES = ("queued", "in_progress", "completed", "failed", "superseded", "stale", "cancelled")`; `FINAL_STATES = frozenset({"completed", "failed", "stale", "cancelled"})`
  - `class LeaseLost(RuntimeError)`; `class StateError(RuntimeError)`
  - `@dataclass(frozen=True) class Generation` with fields in schema order: `id: int, repo: str, pr_number: int, head_sha: str, base_sha: str, base_ref: str, gen: int, reason: str, state: str, created_at: float, started_at: float | None, finished_at: float | None, check_run_id: int | None, external_id: str, conclusion: str | None, error: str | None, attempts: int`
  - `class Store(path: str | Path, *, clock: Callable[[], float] = time.time)` with:
    - `record_delivery(delivery_id: str, event: str, action: str | None, repo: str | None, pr_number: int | None) -> bool` (False = duplicate)
    - `mark_delivery_handled(delivery_id: str) -> None`
    - `enqueue(repo: str, pr_number: int, head_sha: str, base_sha: str, base_ref: str, reason: str) -> Generation`
    - `cancel_pr(repo: str, pr_number: int) -> int` (rows cancelled)
    - `next_queued() -> Generation | None`
    - `get(gen_id: int) -> Generation`
    - `latest_generation(repo: str, pr_number: int) -> Generation | None`
    - `latest_generation_for_head(repo: str, head_sha: str) -> Generation | None`
    - `has_live_generation(repo: str, pr_number: int, head_sha: str, base_sha: str) -> bool`
    - `acquire_lease(holder: str, ttl_s: float, *, holder_alive: Callable[[str], bool] = lambda h: True) -> int | None`
    - `renew_lease(holder: str, token: int, ttl_s: float) -> bool`
    - `lease_info() -> dict | None` (`{"holder", "token", "expires_at", "live"}`)
    - `start(gen_id: int, token: int) -> Generation`
    - `set_check_run(gen_id: int, token: int, check_run_id: int) -> None`
    - `finish(gen_id: int, token: int, state: str, conclusion: str | None = None, error: str | None = None) -> None`
    - `requeue_expired() -> list[int]`
    - `adopt_orphans(token: int) -> list[int]` (fenced: a fresh lease holder re-queues every `in_progress` row, since with one worker any such row belongs to a dead predecessor; covers a predecessor whose lease was broken by `holder_alive` before it expired)
    - `record_publication(gen_id: int, token: int, kind: str, github_id: int) -> None`
    - `publication(gen_id: int) -> tuple[str, int] | None`
    - `set_paused(paused: bool) -> None`; `is_paused() -> bool`
    - `queue_depth() -> int`; `recent(limit: int = 10) -> list[Generation]`; `last_publish_time() -> float | None`; `last_auth_failure() -> str | None`
  - `def holder_alive(holder: str) -> bool` (holder format `"<hostname>:<pid>"`; a holder on this host whose pid is gone is dead; any other host counts as alive)
  - `AUTH_MARKERS = ("AuthenticationError", "UnrecognizedClientException", "ExpiredToken", "security token")`

Schema (exact spec columns; constraints added):

```sql
CREATE TABLE IF NOT EXISTS deliveries(
  delivery_id TEXT PRIMARY KEY, received_at REAL NOT NULL, event TEXT NOT NULL,
  action TEXT, repo TEXT, pr_number INTEGER, handled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS generations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, pr_number INTEGER NOT NULL,
  head_sha TEXT NOT NULL, base_sha TEXT NOT NULL, base_ref TEXT NOT NULL, gen INTEGER NOT NULL,
  reason TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, started_at REAL,
  finished_at REAL, check_run_id INTEGER, external_id TEXT NOT NULL, conclusion TEXT,
  error TEXT, attempts INTEGER NOT NULL DEFAULT 0, UNIQUE(repo, pr_number, gen));
CREATE TABLE IF NOT EXISTS leases(
  name TEXT PRIMARY KEY, holder TEXT, token INTEGER NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS publications(
  generation_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, github_id INTEGER NOT NULL,
  posted_at REAL NOT NULL);
```

Lease rows: `worker` (the fencing lease) and `paused` (the kill switch: `token` 1 = paused, 0 = running; `holder` `"operator"`; `expires_at` 0). `external_id` is `str(id)`, written in the same transaction as the insert.

- [ ] **Step 1: Write the failing test**

`tests/gate/test_store.py`:

```python
"""Gate durable state: generations, supersede, lease fencing, restart recovery (#102)."""

import os
import socket
import sqlite3

import pytest

from prxref.gate import store as store_mod
from prxref.gate.store import LeaseLost, Store


@pytest.fixture
def clock():
    now = [1000.0]
    return now


@pytest.fixture
def db(tmp_path, clock):
    return Store(tmp_path / "gate.sqlite3", clock=lambda: clock[0])


def _enqueue(db, head="h1", pr=7, base="b1", reason="opened"):
    return db.enqueue("acme/widget", pr, head, base, "main", reason)


def test_wal_and_synchronous_full(tmp_path, db):
    conn = sqlite3.connect(tmp_path / "gate.sqlite3")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db._conn.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_duplicate_delivery_is_refused(db):
    assert db.record_delivery("d1", "pull_request", "opened", "acme/widget", 7) is True
    assert db.record_delivery("d1", "pull_request", "opened", "acme/widget", 7) is False


def test_gen_is_monotonic_and_a_new_push_supersedes_queued(db):
    g1 = _enqueue(db, "h1")
    g2 = _enqueue(db, "h2", reason="synchronize")
    assert (g1.gen, g2.gen) == (1, 2)
    assert g2.external_id == str(g2.id)
    assert db.get(g1.id).state == "superseded"
    assert db.next_queued().id == g2.id


def test_next_queued_is_oldest_across_prs(db):
    a = _enqueue(db, "h1", pr=7)
    _enqueue(db, "h9", pr=8)
    assert db.next_queued().id == a.id


def test_lease_fencing_refuses_a_stale_token(db, clock):
    gen = _enqueue(db)
    token = db.acquire_lease("hostA:1", 60)
    db.start(gen.id, token)
    clock[0] += 61
    other = db.acquire_lease("hostB:2", 60)
    assert other == token + 1
    with pytest.raises(LeaseLost):
        db.finish(gen.id, token, "completed", "success")
    with pytest.raises(LeaseLost):
        db.record_publication(gen.id, token, "review", 55)


def test_second_holder_cannot_take_a_live_lease(db):
    assert db.acquire_lease("hostA:1", 60) == 1
    assert db.acquire_lease("hostB:2", 60) is None


def test_a_dead_local_holder_is_broken_immediately(db):
    db.acquire_lease("hostA:1", 600)
    assert db.acquire_lease("hostB:2", 60, holder_alive=lambda h: False) == 2


def test_holder_alive_checks_pids_on_this_host():
    host = socket.gethostname()
    assert store_mod.holder_alive(f"{host}:{os.getpid()}") is True
    assert store_mod.holder_alive(f"{host}:999999999") is False
    assert store_mod.holder_alive("some-other-host:1") is True


def test_restart_requeues_in_progress_only_when_the_lease_is_dead(db, clock):
    gen = _enqueue(db)
    token = db.acquire_lease("hostA:1", 60)
    db.start(gen.id, token)
    db.set_check_run(gen.id, token, 4242)
    assert db.requeue_expired() == []
    clock[0] += 61
    assert db.requeue_expired() == [gen.id]
    again = db.get(gen.id)
    assert (again.state, again.check_run_id, again.attempts) == ("queued", 4242, 1)


def test_a_fresh_lease_adopts_in_progress_orphans(db):
    gen = _enqueue(db)
    old = db.acquire_lease("hostA:1", 600)
    db.start(gen.id, old)
    new = db.acquire_lease("hostB:2", 600, holder_alive=lambda h: False)
    with pytest.raises(LeaseLost):
        db.adopt_orphans(old)
    assert db.adopt_orphans(new) == [gen.id]
    assert db.get(gen.id).state == "queued"


def test_finish_refuses_a_non_final_state(db):
    gen = _enqueue(db)
    token = db.acquire_lease("hostA:1", 60)
    db.start(gen.id, token)
    with pytest.raises(store_mod.StateError):
        db.finish(gen.id, token, "queued")


def test_cancel_pr_cancels_queued_rows_without_a_check(db, clock):
    g1 = _enqueue(db, "h1")
    assert db.cancel_pr("acme/widget", 7) == 1
    assert db.get(g1.id).state == "cancelled"


def test_has_live_generation_ignores_superseded_and_stale(db):
    _enqueue(db, "h1")
    _enqueue(db, "h2")
    assert db.has_live_generation("acme/widget", 7, "h2", "b1") is True
    assert db.has_live_generation("acme/widget", 7, "h1", "b1") is False


def test_latest_generation_for_head_finds_the_pr(db):
    _enqueue(db, "fork-head", pr=12)
    assert db.latest_generation_for_head("acme/widget", "fork-head").pr_number == 12
    assert db.latest_generation_for_head("acme/widget", "nope") is None


def test_pause_flag_round_trips(db):
    assert db.is_paused() is False
    db.set_paused(True)
    assert db.is_paused() is True
    db.set_paused(False)
    assert db.is_paused() is False


def test_last_auth_failure_reports_the_newest_auth_error(db):
    gen = _enqueue(db)
    token = db.acquire_lease("hostA:1", 60)
    db.start(gen.id, token)
    db.finish(gen.id, token, "failed", "failure", "review error: litellm.AuthenticationError: expired")
    assert "AuthenticationError" in db.last_auth_failure()


def test_state_survives_reopen(tmp_path, clock):
    first = Store(tmp_path / "gate.sqlite3", clock=lambda: clock[0])
    g = first.enqueue("acme/widget", 7, "h1", "b1", "main", "opened")
    second = Store(tmp_path / "gate.sqlite3", clock=lambda: clock[0])
    assert second.get(g.id).head_sha == "h1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_store.py -q`
Expected: FAIL with `ImportError: cannot import name 'store'`.

- [ ] **Step 3: Write `src/prxref/gate/store.py`**

```python
"""The gate's durable state: one SQLite file in WAL mode with ``synchronous=FULL`` (#102).

Every state transition is a method here. The worker lease is the ``worker``
row of ``leases``; its ``token`` is a fencing token, and every transition a
worker makes passes the token it holds. A transition from a holder whose
token is no longer current, or whose lease has expired, raises
:class:`LeaseLost` before anything is written, so a second ``prxref gate
serve`` on the same file can never publish.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

STATES = ("queued", "in_progress", "completed", "failed", "superseded", "stale", "cancelled")
FINAL_STATES = frozenset({"completed", "failed", "stale", "cancelled"})
AUTH_MARKERS = ("AuthenticationError", "UnrecognizedClientException", "ExpiredToken", "security token")
_COLUMNS = (
    "id, repo, pr_number, head_sha, base_sha, base_ref, gen, reason, state, created_at, "
    "started_at, finished_at, check_run_id, external_id, conclusion, error, attempts"
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries(
  delivery_id TEXT PRIMARY KEY, received_at REAL NOT NULL, event TEXT NOT NULL,
  action TEXT, repo TEXT, pr_number INTEGER, handled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS generations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, pr_number INTEGER NOT NULL,
  head_sha TEXT NOT NULL, base_sha TEXT NOT NULL, base_ref TEXT NOT NULL, gen INTEGER NOT NULL,
  reason TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, started_at REAL,
  finished_at REAL, check_run_id INTEGER, external_id TEXT NOT NULL, conclusion TEXT,
  error TEXT, attempts INTEGER NOT NULL DEFAULT 0, UNIQUE(repo, pr_number, gen));
CREATE TABLE IF NOT EXISTS leases(
  name TEXT PRIMARY KEY, holder TEXT, token INTEGER NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS publications(
  generation_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, github_id INTEGER NOT NULL,
  posted_at REAL NOT NULL);
"""


class LeaseLost(RuntimeError):
    """The caller's fencing token is not the current, live worker lease."""


class StateError(RuntimeError):
    """A transition from a state that does not allow it."""


@dataclass(frozen=True)
class Generation:
    """One review attempt for one PR at one (head, base) pair."""

    id: int
    repo: str
    pr_number: int
    head_sha: str
    base_sha: str
    base_ref: str
    gen: int
    reason: str
    state: str
    created_at: float
    started_at: float | None
    finished_at: float | None
    check_run_id: int | None
    external_id: str
    conclusion: str | None
    error: str | None
    attempts: int


def holder_alive(holder: str) -> bool:
    """False only for a ``host:pid`` holder on this host whose process is gone."""
    host, _, pid = holder.rpartition(":")
    if host != socket.gethostname() or not pid.isdigit():
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Store:
    """All reads and transitions over the gate's SQLite file."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _fence(self, c: sqlite3.Connection, token: int) -> None:
        row = c.execute("SELECT token, expires_at FROM leases WHERE name='worker'").fetchone()
        if row is None or row[0] != token or row[1] <= self._clock():
            raise LeaseLost(f"fencing token {token} is not the live worker lease")

    def _one(self, sql: str, args: tuple) -> Generation | None:
        with self._lock:
            row = self._conn.execute(f"SELECT {_COLUMNS} FROM generations {sql}", args).fetchone()
        return Generation(*row) if row else None

    def record_delivery(self, delivery_id, event, action, repo, pr_number) -> bool:
        """Insert one delivery row; False when the id was already recorded."""
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO deliveries(delivery_id, received_at, event, action, repo, pr_number) "
                "VALUES (?, ?, ?, ?, ?, ?)", (delivery_id, self._clock(), event, action, repo, pr_number),
            )
            return cur.rowcount == 1

    def mark_delivery_handled(self, delivery_id: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE deliveries SET handled=1 WHERE delivery_id=?", (delivery_id,))

    def enqueue(self, repo, pr_number, head_sha, base_sha, base_ref, reason) -> Generation:
        """Insert the PR's next generation; supersede its older queued ones in the same transaction."""
        now = self._clock()
        with self._tx() as c:
            gen = c.execute(
                "SELECT COALESCE(MAX(gen), 0) + 1 FROM generations WHERE repo=? AND pr_number=?",
                (repo, pr_number),
            ).fetchone()[0]
            c.execute(
                "UPDATE generations SET state='superseded', finished_at=? "
                "WHERE repo=? AND pr_number=? AND state='queued'", (now, repo, pr_number),
            )
            cur = c.execute(
                "INSERT INTO generations(repo, pr_number, head_sha, base_sha, base_ref, gen, reason, "
                "state, created_at, external_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, '')",
                (repo, pr_number, head_sha, base_sha, base_ref, gen, reason, now),
            )
            c.execute("UPDATE generations SET external_id=CAST(id AS TEXT) WHERE id=?", (cur.lastrowid,))
            new_id = cur.lastrowid
        return self.get(new_id)

    def cancel_pr(self, repo: str, pr_number: int) -> int:
        """Cancel queued generations of a closed PR that have no check run yet."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE generations SET state='cancelled', finished_at=? WHERE repo=? AND pr_number=? "
                "AND state='queued' AND check_run_id IS NULL", (self._clock(), repo, pr_number),
            )
            return cur.rowcount

    def next_queued(self) -> Generation | None:
        return self._one(
            "AS g WHERE state='queued' AND gen=(SELECT MAX(gen) FROM generations "
            "WHERE repo=g.repo AND pr_number=g.pr_number) ORDER BY id LIMIT 1", (),
        )

    def get(self, gen_id: int) -> Generation:
        found = self._one("WHERE id=?", (gen_id,))
        if found is None:
            raise StateError(f"no generation {gen_id}")
        return found

    def latest_generation(self, repo: str, pr_number: int) -> Generation | None:
        return self._one("WHERE repo=? AND pr_number=? ORDER BY gen DESC LIMIT 1", (repo, pr_number))

    def latest_generation_for_head(self, repo: str, head_sha: str) -> Generation | None:
        return self._one("WHERE repo=? AND head_sha=? ORDER BY id DESC LIMIT 1", (repo, head_sha))

    def has_live_generation(self, repo, pr_number, head_sha, base_sha) -> bool:
        return self._one(
            "WHERE repo=? AND pr_number=? AND head_sha=? AND base_sha=? "
            "AND state IN ('queued','in_progress','completed','failed') LIMIT 1",
            (repo, pr_number, head_sha, base_sha),
        ) is not None

    def acquire_lease(self, holder, ttl_s, *, holder_alive=lambda h: True) -> int | None:
        """Take the worker lease and return its new fencing token, or None while another holds it."""
        now = self._clock()
        with self._tx() as c:
            row = c.execute("SELECT holder, token, expires_at FROM leases WHERE name='worker'").fetchone()
            if row is not None and row[0] != holder and row[2] > now and holder_alive(row[0]):
                return None
            token = (row[1] if row else 0) + 1
            c.execute(
                "INSERT INTO leases(name, holder, token, expires_at) VALUES ('worker', ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET holder=excluded.holder, token=excluded.token, "
                "expires_at=excluded.expires_at", (holder, token, now + ttl_s),
            )
            return token

    def renew_lease(self, holder: str, token: int, ttl_s: float) -> bool:
        now = self._clock()
        with self._tx() as c:
            cur = c.execute(
                "UPDATE leases SET expires_at=? WHERE name='worker' AND holder=? AND token=? AND expires_at>?",
                (now + ttl_s, holder, token, now),
            )
            return cur.rowcount == 1

    def lease_info(self) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT holder, token, expires_at FROM leases WHERE name='worker'").fetchone()
        if row is None:
            return None
        return {"holder": row[0], "token": row[1], "expires_at": row[2], "live": row[2] > self._clock()}

    def start(self, gen_id: int, token: int) -> Generation:
        with self._tx() as c:
            self._fence(c, token)
            cur = c.execute(
                "UPDATE generations SET state='in_progress', started_at=?, attempts=attempts+1 "
                "WHERE id=? AND state='queued'", (self._clock(), gen_id),
            )
            if cur.rowcount != 1:
                raise StateError(f"generation {gen_id} is not queued")
        return self.get(gen_id)

    def set_check_run(self, gen_id: int, token: int, check_run_id: int) -> None:
        with self._tx() as c:
            self._fence(c, token)
            c.execute("UPDATE generations SET check_run_id=? WHERE id=?", (check_run_id, gen_id))

    def finish(self, gen_id, token, state, conclusion=None, error=None) -> None:
        if state not in FINAL_STATES:
            raise StateError(f"{state!r} is not a final state")
        with self._tx() as c:
            self._fence(c, token)
            cur = c.execute(
                "UPDATE generations SET state=?, conclusion=?, error=?, finished_at=? "
                "WHERE id=? AND state IN ('queued','in_progress')",
                (state, conclusion, error, self._clock(), gen_id),
            )
            if cur.rowcount != 1:
                raise StateError(f"generation {gen_id} is already final")

    def requeue_expired(self) -> list[int]:
        """Return in-progress generations to the queue when no live worker lease exists."""
        with self._tx() as c:
            row = c.execute("SELECT expires_at FROM leases WHERE name='worker'").fetchone()
            if row is not None and row[0] > self._clock():
                return []
            ids = [r[0] for r in c.execute("SELECT id FROM generations WHERE state='in_progress' ORDER BY id")]
            c.execute("UPDATE generations SET state='queued' WHERE state='in_progress'")
            return ids

    def adopt_orphans(self, token: int) -> list[int]:
        """Re-queue every in-progress generation; called once by a fresh lease holder."""
        with self._tx() as c:
            self._fence(c, token)
            ids = [r[0] for r in c.execute("SELECT id FROM generations WHERE state='in_progress' ORDER BY id")]
            c.execute("UPDATE generations SET state='queued' WHERE state='in_progress'")
            return ids

    def record_publication(self, gen_id: int, token: int, kind: str, github_id: int) -> None:
        with self._tx() as c:
            self._fence(c, token)
            c.execute(
                "INSERT OR IGNORE INTO publications(generation_id, kind, github_id, posted_at) "
                "VALUES (?, ?, ?, ?)", (gen_id, kind, github_id, self._clock()),
            )

    def publication(self, gen_id: int) -> tuple[str, int] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT kind, github_id FROM publications WHERE generation_id=?", (gen_id,),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def set_paused(self, paused: bool) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO leases(name, holder, token, expires_at) VALUES ('paused', 'operator', ?, 0) "
                "ON CONFLICT(name) DO UPDATE SET token=excluded.token", (1 if paused else 0,),
            )

    def is_paused(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT token FROM leases WHERE name='paused'").fetchone()
        return bool(row and row[0])

    def queue_depth(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM generations WHERE state='queued'").fetchone()[0]

    def recent(self, limit: int = 10) -> list[Generation]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM generations ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [Generation(*r) for r in rows]

    def last_publish_time(self) -> float | None:
        with self._lock:
            return self._conn.execute(
                "SELECT MAX(finished_at) FROM generations WHERE conclusion IS NOT NULL",
            ).fetchone()[0]

    def last_auth_failure(self) -> str | None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT error FROM generations WHERE error IS NOT NULL ORDER BY id DESC LIMIT 50",
            ).fetchall()
        for (error,) in rows:
            if any(marker in error for marker in AUTH_MARKERS):
                return error
        return None
```

- [ ] **Step 4: Run the tests and lint**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_store.py -q && uv run ruff check src tests`
Expected: `17 passed`; ruff clean.

- [ ] **Step 5: Commit**

```bash
cd ~/code/prxref/.claude/worktrees/sdlc-gate
git add src/prxref/gate/store.py tests/gate/test_store.py
git commit -m "feat(gate): SQLite state with a fenced worker lease (#102)" -m "Claude-Session-Id: <session UUID>"
```

---

### Task 5: `receiver.py`: webhook intake

**Files:**
- Create: `src/prxref/gate/receiver.py`
- Test: `tests/gate/test_receiver.py`

**Interfaces:**
- Consumes: `prxref.webhooks._check_hmac(body: bytes, headers: dict, header: str, secret_env: str, forge: str) -> tuple[str, str | None]` (`src/prxref/webhooks.py:192 (_check_hmac)`; headers must be lower-cased; returns `"ok"`, `"unsigned"` or `"reject"`); `GateSettings`, `WEBHOOK_SECRET_ENV`, `WEBHOOK_SECRET_PREVIOUS_ENV` (Task 2); `Store` (Task 4).
- Produces:
  - `CHECK_NAME = "sdlc-review"`
  - `@dataclass(frozen=True) class Intent` with `kind: str` (`"enqueue"` or `"cancel"`), `pr_number: int`, `head_sha: str = ""`, `base_sha: str = ""`, `base_ref: str = ""`, `reason: str = ""`
  - `def map_event(event: str, payload: dict, *, app_id: int, repo: str, store: Store) -> Intent | None`
  - `class Receiver(store: Store, settings: GateSettings, *, started_at: float, clock: Callable[[], float] = time.time)` with `handle(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict]` and `health(self) -> dict`
  - `def serve_http(receiver: Receiver, host: str, port: int) -> ThreadingHTTPServer` (bound, not yet serving; caller runs `serve_forever` in a thread)
  - `MAX_BODY_BYTES = 25 * 1024 * 1024`

Status codes: 401 bad/missing/unsigned signature (no row); 400 non-JSON or non-object body, or a missing `X-GitHub-Delivery`/`X-GitHub-Event` (no row); 403 installation or repository outside the allowlist (no row); 200 `{"status": "duplicate"}` for a repeated delivery id; 202 for accepted and for ignored events; 404 any other path; 413 a body over `MAX_BODY_BYTES`.

- [ ] **Step 1: Write the failing test**

`tests/gate/test_receiver.py`:

```python
"""Gate webhook intake: verification, allowlist, dedup, event mapping (#102)."""

import hashlib
import hmac
import json
import threading

import pytest
import requests

from prxref.gate import receiver
from prxref.gate.settings import GateSettings
from prxref.gate.store import Store

SECRET = "current-secret"
OLD = "previous-secret"
APP_ID = 12345


def _settings(tmp_path):
    return GateSettings(
        app_id=APP_ID, private_key_file=tmp_path / "k.pem", installation_ids=frozenset({777}),
        repos=frozenset({"acme/widget"}), secret_grace_minutes=30, state_file=tmp_path / "g.sqlite3",
        runs_dir=tmp_path / "runs", review_deadline=900, watchdog_seconds=60, reconcile_minutes=10,
    )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("PRXREF_GATE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("PRXREF_GATE_WEBHOOK_SECRET_PREVIOUS", OLD)


@pytest.fixture
def clock():
    return [10_000.0]


@pytest.fixture
def rx(tmp_path, env, clock):
    db = Store(tmp_path / "g.sqlite3", clock=lambda: clock[0])
    return receiver.Receiver(db, _settings(tmp_path), started_at=10_000.0, clock=lambda: clock[0])


def _pr_payload(action="opened", head="h1", base="b1", number=7, repo="acme/widget", installation=777, **extra):
    payload = {
        "action": action, "number": number,
        "pull_request": {"number": number, "head": {"sha": head}, "base": {"sha": base, "ref": "main"}},
        "repository": {"full_name": repo}, "installation": {"id": installation},
    }
    payload.update(extra)
    return payload


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(rx, payload, *, event="pull_request", delivery="d1", secret=SECRET, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery}
    if secret is not None:
        headers["X-Hub-Signature-256"] = _sign(body, secret)
    return rx.handle("POST", "/webhook", headers, body)


def test_opened_enqueues_a_generation(rx):
    status, reply = _post(rx, _pr_payload())
    assert (status, reply["status"], reply["delivery"]) == (202, "accepted", "d1")
    gen = rx.store.next_queued()
    assert (gen.pr_number, gen.head_sha, gen.base_sha, gen.base_ref, gen.reason) == (7, "h1", "b1", "main", "opened")


@pytest.mark.parametrize("secret", ["wrong", None])
def test_bad_or_missing_signature_is_401_without_a_row(rx, secret):
    status, _ = _post(rx, _pr_payload(), secret=secret)
    assert status == 401
    assert rx.store.queue_depth() == 0
    assert rx.store.record_delivery("d1", "x", None, None, None) is True


def test_allow_unsigned_does_not_open_the_gate(rx, monkeypatch):
    monkeypatch.setenv("PRXREF_ALLOW_UNSIGNED", "1")
    assert _post(rx, _pr_payload(), secret=None)[0] == 401
    assert _post(rx, _pr_payload(), secret="wrong", delivery="d2")[0] == 401
    assert rx.store.queue_depth() == 0


def test_previous_secret_is_accepted_inside_the_grace_window(rx, clock):
    clock[0] = 10_000.0 + 29 * 60
    assert _post(rx, _pr_payload(), secret=OLD)[0] == 202


def test_previous_secret_is_refused_after_the_grace_window(rx, clock):
    clock[0] = 10_000.0 + 31 * 60
    assert _post(rx, _pr_payload(), secret=OLD)[0] == 401


@pytest.mark.parametrize("raw", [
    b"\xef\xbb\xbf" + json.dumps({"action": "opened"}).encode(),
    b"\xff\xfe\x00not-utf8",
    b"[1, 2, 3]",
    b'{"action": "opened"',
    b"",
])
def test_signed_garbage_bodies_are_400_without_a_row(rx, raw):
    status, _ = _post(rx, None, raw=raw)
    assert status == 400
    assert rx.store.queue_depth() == 0
    assert rx.store.record_delivery("d1", "x", None, None, None) is True


def test_unknown_installation_or_repo_is_403(rx):
    assert _post(rx, _pr_payload(installation=999))[0] == 403
    assert _post(rx, _pr_payload(repo="acme/other"), delivery="d2")[0] == 403
    assert rx.store.queue_depth() == 0


def test_repo_allowlist_is_case_insensitive(rx):
    assert _post(rx, _pr_payload(repo="ACME/Widget"))[0] == 202


def test_duplicate_delivery_is_200_and_enqueues_once(rx):
    assert _post(rx, _pr_payload())[0] == 202
    status, reply = _post(rx, _pr_payload())
    assert (status, reply["status"]) == (200, "duplicate")
    assert rx.store.queue_depth() == 1


def test_second_push_supersedes_the_first(rx):
    _post(rx, _pr_payload(head="h1"))
    _post(rx, _pr_payload(action="synchronize", head="h2"), delivery="d2")
    assert rx.store.queue_depth() == 1
    assert rx.store.next_queued().head_sha == "h2"


def test_out_of_order_delivery_still_queues_a_generation(rx):
    _post(rx, _pr_payload(action="synchronize", head="h2"), delivery="d2")
    _post(rx, _pr_payload(action="synchronize", head="h1"), delivery="d1")
    assert rx.store.next_queued().head_sha == "h1"


def test_retarget_queues_a_new_generation_for_the_same_head(rx):
    _post(rx, _pr_payload())
    payload = _pr_payload(action="edited", base="b2", changes={"base": {"ref": {"from": "dev"}}})
    assert _post(rx, payload, delivery="d2")[0] == 202
    assert rx.store.next_queued().base_sha == "b2"


def test_edit_of_the_title_only_is_ignored(rx):
    payload = _pr_payload(action="edited", changes={"title": {"from": "old"}})
    status, reply = _post(rx, payload)
    assert (status, reply["status"]) == (202, "ignored")
    assert rx.store.queue_depth() == 0


def test_closed_cancels_and_reopened_creates(rx):
    _post(rx, _pr_payload())
    _post(rx, _pr_payload(action="closed"), delivery="d2")
    assert rx.store.queue_depth() == 0
    _post(rx, _pr_payload(action="reopened"), delivery="d3")
    assert rx.store.next_queued().reason == "reopened"


def _check_run(head="h1", prs=None, app_id=APP_ID, name="sdlc-review"):
    return {
        "action": "rerequested", "repository": {"full_name": "acme/widget"}, "installation": {"id": 777},
        "check_run": {"name": name, "head_sha": head, "app": {"id": app_id}, "pull_requests": prs or []},
    }


def test_rerequest_creates_a_generation_with_the_same_head(rx):
    prs = [{"number": 7, "head": {"sha": "h1"}, "base": {"sha": "b1", "ref": "main"}}]
    assert _post(rx, _check_run(prs=prs), event="check_run")[0] == 202
    gen = rx.store.next_queued()
    assert (gen.head_sha, gen.reason) == ("h1", "rerequest")


def test_rerequest_on_fork_pr_uses_the_store_by_head_sha(rx):
    _post(rx, _pr_payload(head="forkhead", number=12))
    assert _post(rx, _check_run(head="forkhead"), event="check_run", delivery="d2")[0] == 202
    gen = rx.store.next_queued()
    assert (gen.pr_number, gen.head_sha, gen.reason) == (12, "forkhead", "rerequest")


def test_rerequest_for_unknown_head_is_ignored(rx):
    status, reply = _post(rx, _check_run(head="never-seen"), event="check_run")
    assert (status, reply["status"]) == (202, "ignored")


def test_rerequest_of_another_apps_check_is_ignored(rx):
    prs = [{"number": 7, "head": {"sha": "h1"}, "base": {"sha": "b1", "ref": "main"}}]
    assert _post(rx, _check_run(prs=prs, app_id=1), event="check_run")[1]["status"] == "ignored"


def test_check_suite_rerequest_creates_a_generation(rx):
    payload = {
        "action": "rerequested", "repository": {"full_name": "acme/widget"}, "installation": {"id": 777},
        "check_suite": {"head_sha": "h1", "app": {"id": APP_ID},
                        "pull_requests": [{"number": 7, "base": {"sha": "b1", "ref": "main"}}]},
    }
    assert _post(rx, payload, event="check_suite")[0] == 202
    assert rx.store.next_queued().reason == "rerequest"


@pytest.mark.parametrize("event", ["ping", "installation", "issues"])
def test_other_events_are_202_ignored(rx, event):
    status, reply = _post(rx, {"zen": "x", "repository": {"full_name": "acme/widget"},
                               "installation": {"id": 777}}, event=event)
    assert (status, reply["status"]) == (202, "ignored")


def test_missing_delivery_header_is_400(rx):
    body = json.dumps(_pr_payload()).encode()
    headers = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sign(body)}
    assert rx.handle("POST", "/webhook", headers, body)[0] == 400


def test_health_reports_queue_lease_publish_and_pause(rx):
    _post(rx, _pr_payload())
    rx.store.set_paused(True)
    status, body = rx.handle("GET", "/health", {}, b"")
    assert status == 200
    assert body == {"queue_depth": 1, "lease": None, "last_publish": None, "paused": True,
                    "last_auth_failure": None}


def test_real_socket_round_trip(rx):
    server = receiver.serve_http(rx, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        body = json.dumps(_pr_payload()).encode()
        resp = requests.post(
            f"http://127.0.0.1:{port}/webhook", data=body, timeout=5,
            headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "sock1",
                     "X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"},
        )
        assert (resp.status_code, resp.json()["delivery"]) == (202, "sock1")
        assert requests.get(f"http://127.0.0.1:{port}/health", timeout=5).json()["queue_depth"] == 1
    finally:
        server.shutdown()
        server.server_close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_receiver.py -q`
Expected: FAIL with `ImportError: cannot import name 'receiver'`.

- [ ] **Step 3: Write `src/prxref/gate/receiver.py`**

```python
"""The gate's webhook intake: verify, allowlist, dedup, enqueue, answer 202 (#102).

No GitHub call and no inference happens here. A delivery is accepted only
when ``webhooks._check_hmac`` returns ``"ok"``: its ``"unsigned"`` state,
which ``PRXREF_ALLOW_UNSIGNED=1`` produces for the advisory daemon, is a 401
here. The previous secret is tried only inside the rotation grace window,
measured from this process's start.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prxref.gate.settings import WEBHOOK_SECRET_ENV, WEBHOOK_SECRET_PREVIOUS_ENV, GateSettings
from prxref.gate.store import Store
from prxref.webhooks import _check_hmac

logger = logging.getLogger(__name__)

CHECK_NAME = "sdlc-review"
MAX_BODY_BYTES = 25 * 1024 * 1024
_PR_ENQUEUE_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})
_HANDLED_EVENTS = frozenset({"pull_request", "check_run", "check_suite"})


@dataclass(frozen=True)
class Intent:
    """What one delivery asks the gate to do."""

    kind: str
    pr_number: int
    head_sha: str = ""
    base_sha: str = ""
    base_ref: str = ""
    reason: str = ""


def _rerequest(head_sha: str, prs: list, *, repo: str, store: Store) -> Intent | None:
    if prs:
        pr = prs[0]
        base = pr.get("base") or {}
        return Intent("enqueue", int(pr["number"]), head_sha, str(base.get("sha", "")),
                      str(base.get("ref", "")), "rerequest")
    known = store.latest_generation_for_head(repo, head_sha)
    if known is None:
        return None
    return Intent("enqueue", known.pr_number, head_sha, known.base_sha, known.base_ref, "rerequest")


def map_event(event: str, payload: dict, *, app_id: int, repo: str, store: Store) -> Intent | None:
    """Map one verified, allowlisted delivery to an :class:`Intent`, or None to ignore it."""
    action = payload.get("action")
    if event == "pull_request":
        pr = payload.get("pull_request") or {}
        head, base = pr.get("head") or {}, pr.get("base") or {}
        number = int(pr.get("number") or payload.get("number"))
        if action == "closed":
            return Intent("cancel", number)
        reason = action if action in _PR_ENQUEUE_ACTIONS else None
        if action == "edited" and "base" in (payload.get("changes") or {}):
            reason = "retarget"
        if reason is None:
            return None
        return Intent("enqueue", number, str(head["sha"]), str(base["sha"]), str(base["ref"]), reason)
    if event in ("check_run", "check_suite") and action == "rerequested":
        body = payload.get(event) or {}
        if int((body.get("app") or {}).get("id", -1)) != app_id:
            return None
        if event == "check_run" and body.get("name") != CHECK_NAME:
            return None
        return _rerequest(str(body["head_sha"]), body.get("pull_requests") or [], repo=repo, store=store)
    return None


class Receiver:
    """Pure request handling; :func:`serve_http` puts it on a socket."""

    def __init__(self, store: Store, settings: GateSettings, *, started_at: float,
                 clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.settings = settings
        self._started_at = started_at
        self._clock = clock

    def _verify(self, body: bytes, headers: dict) -> str:
        state, _ = _check_hmac(body, headers, "x-hub-signature-256", WEBHOOK_SECRET_ENV, "gate")
        if state == "ok":
            return "current"
        grace_open = self._clock() - self._started_at < self.settings.secret_grace_minutes * 60
        if grace_open:
            state, _ = _check_hmac(body, headers, "x-hub-signature-256", WEBHOOK_SECRET_PREVIOUS_ENV, "gate")
            if state == "ok":
                logger.warning("delivery %s matched only the previous webhook secret",
                               headers.get("x-github-delivery", "?"))
                return "previous"
        return "reject"

    def health(self) -> dict:
        return {
            "queue_depth": self.store.queue_depth(),
            "lease": self.store.lease_info(),
            "last_publish": self.store.last_publish_time(),
            "paused": self.store.is_paused(),
            "last_auth_failure": self.store.last_auth_failure(),
        }

    def handle(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict]:
        """Answer one HTTP request with ``(status, json_body)``."""
        h = {k.lower(): v for k, v in headers.items()}
        if method == "GET" and path == "/health":
            return 200, self.health()
        if method != "POST" or path != "/webhook":
            return 404, {"error": "not found"}
        if self._verify(body, h) == "reject":
            return 401, {"error": "signature rejected"}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return 400, {"error": "body is not JSON"}
        if not isinstance(payload, dict):
            return 400, {"error": "body is not a JSON object"}
        delivery = h.get("x-github-delivery", "").strip()
        event = h.get("x-github-event", "").strip()
        if not delivery or not event:
            return 400, {"error": "missing X-GitHub-Delivery or X-GitHub-Event"}
        if event not in _HANDLED_EVENTS:
            logger.info("delivery %s: event %s ignored", delivery, event)
            return 202, {"status": "ignored", "delivery": delivery}
        installation = (payload.get("installation") or {}).get("id")
        repo = str((payload.get("repository") or {}).get("full_name") or "").lower()
        if installation not in self.settings.installation_ids or repo not in self.settings.repos:
            return 403, {"error": "installation or repository not allowlisted"}
        try:
            intent = map_event(event, payload, app_id=self.settings.app_id, repo=repo, store=self.store)
        except (KeyError, TypeError, ValueError):
            return 400, {"error": "payload is missing a required field"}
        if not self.store.record_delivery(delivery, event, payload.get("action"), repo,
                                          intent.pr_number if intent else None):
            return 200, {"status": "duplicate", "delivery": delivery}
        if intent is None:
            self.store.mark_delivery_handled(delivery)
            logger.info("delivery %s: %s %s ignored", delivery, event, payload.get("action"))
            return 202, {"status": "ignored", "delivery": delivery}
        if intent.kind == "cancel":
            self.store.cancel_pr(repo, intent.pr_number)
        else:
            self.store.enqueue(repo, intent.pr_number, intent.head_sha, intent.base_sha,
                               intent.base_ref, intent.reason)
        self.store.mark_delivery_handled(delivery)
        return 202, {"status": "accepted", "delivery": delivery}


def serve_http(receiver: Receiver, host: str, port: int) -> ThreadingHTTPServer:
    """Bind a threading HTTP server that hands every request to ``receiver.handle``."""

    class _Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            self._reply(*receiver.handle("GET", self.path, dict(self.headers), b""))

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._reply(413, {"error": "body too large"})
                return
            body = self.rfile.read(length)
            self._reply(*receiver.handle("POST", self.path, dict(self.headers), body))

        def log_message(self, fmt: str, *args) -> None:
            logger.info("%s %s", self.address_string(), fmt % args)

    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    return server
```

The `_check_hmac` `forge` argument is only used in its reason text; `"gate"` keeps the gate's rejections distinguishable in logs.

- [ ] **Step 4: Run the tests and lint**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_receiver.py -q && uv run ruff check src tests`
Expected: `30 passed`; ruff clean.

- [ ] **Step 5: Commit**

```bash
cd ~/code/prxref/.claude/worktrees/sdlc-gate
git add src/prxref/gate/receiver.py tests/gate/test_receiver.py
git commit -m "feat(gate): webhook receiver with strict signatures, allowlist and dedup (#102)" -m "Claude-Session-Id: <session UUID>"
```

---

### Task 6: `rules_source.py`: base-pinned rules, the orchestrator switch, and `_run_review(forge=…)`

Today `.prxref.toml` and every path it names are read from the local filesystem (`src/prxref/cli.py:1772 (_run_review)` → `load_config_with_sources(config_file=…)` → `load_path_inputs(…)`, `src/prxref/review_inputs.py:144 (load_path_inputs)`), and standards documents are read through a forge reader at the PR head (`src/prxref/orchestrator.py:3534 (_plan_repo_context)`, `repo_reader.forge_reader(forge, ref, pr.source_sha, …)`). The gate has no checkout, so this task (a) materializes the repository's `.prxref.toml` and the files its path keys name, fetched at one SHA, into a private temp directory, which then becomes `config_file`; and (b) adds an orchestrator `rules_source` parameter that reads standards documents at `pr.target_sha` under `"base"`. Head content stays the source of chunk context (`src/prxref/orchestrator.py:3362 (_make_file_reader)` keeps `source_sha`).

**Files:**
- Create: `src/prxref/gate/rules_source.py`
- Modify: `src/prxref/orchestrator.py`: `orchestrate_review` signature (`:570`, add `rules_source: str = "head"` after `learnings: Any = None` at `:641`); both `_plan_repo_context(…)` calls (`:1802`, `:1812`); `_RepoPlan` (`:3482`, add a field); `_plan_repo_context` (`:3506`); `_chunk_unit` (`:3607`).
- Modify: `src/prxref/cli.py`: `_run_review` (`:1772`): new keyword `forge: Any = None`; `rules_source=cfg["rules_source"]` in the `orchestrate(...)` call.
- Test: `tests/gate/test_rules_source.py`

**Interfaces:**
- Consumes: `cfg["rules_source"]` (Task 2); `prxref.config._FILE_PATH_KEYS`, `prxref.config.METADATA_RULES_SWITCHES`, `prxref.config.CONFIG_FILE_NAME` (`src/prxref/config.py:1150`).
- Produces:
  - `prxref.gate.rules_source.DIRECTORY_KEYS = frozenset({"prompts_dir"})` (never materialized; the worker blanks `prompts_dir`)
  - `@dataclass(frozen=True) class PinnedRules` with `directory: Path`, `config_file: Path | None`, `fetched: tuple[str, ...]`, `missing: tuple[str, ...]`
  - `def materialize_rules(read: Callable[[str], str | None], directory: Path) -> PinnedRules`
  - `def rules_sha(head_sha: str, base_sha: str, rules_source: str) -> str`
  - `orchestrate_review(..., rules_source: str = "head")`
  - `cli._run_review(url, *, ..., forge: Any = None)`: when given, used instead of `make_forge(ref)`.

- [ ] **Step 1: Write the failing tests**

`tests/gate/test_rules_source.py`:

```python
"""Base-pinned rules (#102): the repo config and standards come from the base SHA."""

from pathlib import Path

import pytest

from prxref import cli, config, llm_backends, orchestrator
from prxref.gate import rules_source
from tests.test_issue_68_standards_context import (
    STANDARD_GLOBS,
    STANDARDS_DOC,
    TWO_CHUNK_DIFF,
    WEB_SECURITY,
    _pinned_clock,  # noqa: F401  (autouse fixture: importing it registers it here)
    _RecordingLLM,
    _RepoForge,
    _worker_prompts,
    _write,
)
from tests.test_orchestrator import REF, FakeForge

HEAD_SHA, BASE_SHA = "a" * 40, "b" * 40


def _reader(files: dict[str, str]):
    return lambda path: files.get(path)


def test_materializes_the_config_and_its_path_inputs(tmp_path):
    files = {
        ".prxref.toml": 'review_rules = "rules/team.md"\nscoped_rules = ["rules/api.md", "rules/gone.md"]\n',
        "rules/team.md": "- Never log secrets.\n",
        "rules/api.md": "---\npaths: ['api/**']\n---\n- Validate input.\n",
    }
    pinned = rules_source.materialize_rules(_reader(files), tmp_path)
    assert pinned.config_file == tmp_path / ".prxref.toml"
    assert (tmp_path / "rules/team.md").read_text() == "- Never log secrets.\n"
    assert pinned.fetched == ("rules/api.md", "rules/team.md")
    assert pinned.missing == ("rules/gone.md",)
    assert not any(p.is_symlink() for p in tmp_path.rglob("*"))


def test_no_config_at_the_sha_gives_no_config_file(tmp_path):
    assert rules_source.materialize_rules(_reader({}), tmp_path).config_file is None


@pytest.mark.parametrize("raw", ["../outside.md", "/etc/passwd", "~/x.md", "https://x/y.md"])
def test_paths_leaving_the_repo_are_never_fetched(tmp_path, raw):
    asked = []

    def read(path):
        asked.append(path)
        return f'review_rules = "{raw}"\n' if path == ".prxref.toml" else "x"

    rules_source.materialize_rules(read, tmp_path)
    assert asked == [".prxref.toml"]


def test_prompts_dir_and_metadata_switches_are_not_fetched(tmp_path):
    asked = []

    def read(path):
        asked.append(path)
        return 'prompts_dir = "prompts"\nmetadata_rules = "off"\n' if path == ".prxref.toml" else "x"

    rules_source.materialize_rules(read, tmp_path)
    assert asked == [".prxref.toml"]


def test_base_config_wins_over_a_head_edit_that_disables_the_rule(tmp_path):
    base = {".prxref.toml": 'review_rules = "rules/team.md"\n', "rules/team.md": "- RULE-SENTINEL\n"}
    head = {".prxref.toml": 'review_rules = ""\n'}
    by_sha = {BASE_SHA: base, HEAD_SHA: head}
    for source, expected in (("base", "RULE-SENTINEL"), ("head", None)):
        sha = rules_source.rules_sha(HEAD_SHA, BASE_SHA, source)
        directory = tmp_path / source
        directory.mkdir()
        pinned = rules_source.materialize_rules(_reader(by_sha[sha]), directory)
        cfg = config.load_config(config_file=pinned.config_file)
        rules_path = cfg["review_rules"]
        text = Path(rules_path).read_text() if rules_path else None
        assert (expected in text) if expected else text is None


class _ShaForge(_RepoForge):
    """_RepoForge (tests/test_issue_68_standards_context.py:388) with one tree per commit."""

    def __init__(self, diff, roots: dict[str, Path]):
        super().__init__(diff, roots[HEAD_SHA])
        self.roots = roots

    def get_file_content(self, ref, path, *, sha):
        target = self.roots[sha] / path
        return target.read_text(encoding="utf-8") if target.is_file() else None

    def list_paths(self, ref, *, sha):
        self.root = self.roots[sha]
        return super().list_paths(ref, sha=sha)


@pytest.mark.parametrize("source, shown, hidden", [
    ("base", "max-age=63072000", "max-age=31536000"),
    ("head", "max-age=31536000", "max-age=63072000"),
])
def test_standards_document_is_read_at_the_rules_sha(tmp_path, source, shown, hidden):
    base_root, head_root = tmp_path / "base", tmp_path / "head"
    _write(base_root, STANDARDS_DOC, WEB_SECURITY)
    _write(head_root, STANDARDS_DOC, WEB_SECURITY.replace("63072000", "31536000"))
    forge = _ShaForge(TWO_CHUNK_DIFF, {HEAD_SHA: head_root, BASE_SHA: base_root})
    llm = _RecordingLLM()
    orchestrator.orchestrate_review(
        forge, REF, llm, post=False, repo_context="repo", max_files_per_chunk=1,
        context_standards_globs=STANDARD_GLOBS, rules_source=source,
    )
    prompt = _worker_prompts(llm, ["app/middleware.py", "tools/helper.py"])["app/middleware.py"][0]
    assert shown in prompt
    assert hidden not in prompt


def test_run_review_uses_the_given_forge_and_passes_rules_source(monkeypatch):
    seen = {}

    def fake_orchestrate(**kwargs):
        seen.update(kwargs)
        return {"verdict": "Approved"}

    monkeypatch.setattr(orchestrator, "orchestrate_review", fake_orchestrate)
    monkeypatch.setattr(llm_backends, "create_llm_client", lambda cfg: object())
    monkeypatch.setattr(cli, "make_forge", lambda ref: pytest.fail("make_forge must not run"))
    monkeypatch.setenv("PRXREF_RULES_SOURCE", "base")
    given = FakeForge(diff="")
    cli._run_review("https://github.com/acme/widget/pull/7", forge=given)
    assert seen["forge"] is given
    assert seen["rules_source"] == "base"
```

`FakeForge`'s PR has `source_sha` `"a" * 40` and `target_sha` `"b" * 40` (`tests/test_orchestrator.py:113 (make_pr)`); re-check with `grep -n 'source_sha\|target_sha' tests/test_orchestrator.py | head` before relying on `HEAD_SHA`/`BASE_SHA`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_rules_source.py -q`
Expected: FAIL (`ImportError: cannot import name 'rules_source'`).

- [ ] **Step 3: Write `src/prxref/gate/rules_source.py`**

```python
"""Materialize a repository's prxref config at one commit (#102).

The gate has no checkout. It fetches ``.prxref.toml`` at the rules SHA (the
PR's base under ``PRXREF_RULES_SOURCE=base``) and every file its path keys
name at the same SHA, and writes them as regular files under a private
directory, which then serves as the review's ``config_file``. A path that is
absolute, starts with ``~``, climbs out with ``..`` or is a URL is never
fetched: the config loader refuses it with a ConfigError, so the review
fails closed. A named file missing at the SHA is left missing, with the
same result.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from prxref import config

DIRECTORY_KEYS = frozenset({"prompts_dir"})


@dataclass(frozen=True)
class PinnedRules:
    """What :func:`materialize_rules` wrote."""

    directory: Path
    config_file: Path | None
    fetched: tuple[str, ...]
    missing: tuple[str, ...]


def rules_sha(head_sha: str, base_sha: str, rules_source: str) -> str:
    """The commit rules are read at: ``base_sha`` under ``"base"``, else ``head_sha``."""
    return base_sha if rules_source == "base" else head_sha


def _safe(raw: str) -> PurePosixPath | None:
    if not raw.strip() or raw.startswith("~") or "://" in raw:
        return None
    rel = PurePosixPath(raw)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    return rel


def materialize_rules(read: Callable[[str], str | None], directory: Path) -> PinnedRules:
    """Write the config at ``read``'s commit, and every file its path keys name, under ``directory``."""
    text = read(config.CONFIG_FILE_NAME)
    if text is None:
        return PinnedRules(directory, None, (), ())
    config_file = directory / config.CONFIG_FILE_NAME
    config_file.write_text(text, encoding="utf-8")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return PinnedRules(directory, config_file, (), ())
    wanted: set[str] = set()
    for key in config._FILE_PATH_KEYS - DIRECTORY_KEYS:
        value = data.get(key)
        values = value if isinstance(value, list) else [value]
        for raw in values:
            if not isinstance(raw, str):
                continue
            if key == "metadata_rules" and raw.strip() in config.METADATA_RULES_SWITCHES:
                continue
            rel = _safe(raw)
            if rel is not None:
                wanted.add(rel.as_posix())
    fetched, missing = [], []
    for rel in sorted(wanted):
        content = read(rel)
        if content is None:
            missing.append(rel)
            continue
        dest = directory / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        fetched.append(rel)
    return PinnedRules(directory, config_file, tuple(fetched), tuple(missing))
```

Before writing, confirm the two names exist: `grep -n '^CONFIG_FILE_NAME\|^METADATA_RULES_SWITCHES' src/prxref/config.py` must print two lines.

- [ ] **Step 4: Add `rules_source` to the orchestrator**

In `_RepoPlan` (`src/prxref/orchestrator.py:3482`), add the last field:

```python
    standards_reader: repo_reader.RepoReader | None = None
```

and one docstring sentence: "``standards_reader`` reads the standards documents when they come from another commit than ``reader`` (``rules_source="base"``); ``None`` means ``reader``."

Change `_plan_repo_context`'s signature to `(mode, forge, ref, pr, files, initial, *, repo_dir, rules_source="head")`. After the existing `reader = …` block, add:

```python
    standards_reader = None
    if rules_source == "base" and repo_dir is None:
        standards_reader = repo_reader.forge_reader(
            forge, ref, getattr(pr, "target_sha", "") or "", exclude=exclude, **caps,
        )
    listing_source = standards_reader if standards_reader is not None else reader
```

In the `mode != "repo"` branch, replace `listing = reader.listing()` with `listing = listing_source.listing()` and guard `if listing_source is not None and initial["standards_globs"] and budget > 0:`, and pass `standards_reader=standards_reader` to the `_RepoPlan(...)` it returns. In the `"repo"` branch, compute `standards_listing = standards_reader.listing() if standards_reader is not None else listing` and use `standards_listing` (not `listing`) for `standards_paths=…select_contract_files(standards_globs, listing=…)`; pass `standards_reader=standards_reader`. Add to the docstring: "With ``rules_source="base"`` and no ``repo_dir``, standards documents are listed and read at the PR's base sha (``pr.target_sha``); everything else still reads the head."

Add beside `_routed_read`:

```python
def _standards_routed(
    read: Callable[[str], str | None] | None, standards: repo_reader.RepoReader, paths: frozenset[str],
) -> Callable[[str], str | None]:
    """One chunk's ``read`` with the standards documents served by ``standards`` (#102)."""

    def routed(path: str) -> str | None:
        if path in paths:
            return standards.read(path)
        return read(path) if read is not None else None

    return routed
```

In `_chunk_unit`, after `read = _routed_read(...) if plan.reader is not None else None`:

```python
    if plan.standards_reader is not None and plan.standards_paths:
        read = _standards_routed(read, plan.standards_reader, frozenset(plan.standards_paths))
```

Add `rules_source: str = "head",` after `learnings: Any = None,` in `orchestrate_review`'s signature, document it in the docstring ("``rules_source`` (#102): ``"base"`` reads standards documents at the PR's base sha; ``"head"``, the default, is the behavior before 0.35.0"), and pass `rules_source=rules_source` to both `_plan_repo_context(...)` calls.

- [ ] **Step 5: Wire `_run_review`**

In `src/prxref/cli.py:1772 (_run_review)`, add `forge: Any = None,` after `metadata_rules: str | None = None,`. Replace

```python
    else:
        forge = make_forge(ref)
        if replay is not None:
```

with

```python
    else:
        forge = forge if forge is not None else make_forge(ref)
        if replay is not None:
```

(the `url is None` branch assigns `forge = LocalDiffForge(...)` and is unchanged), and add `rules_source=cfg["rules_source"],` after `learnings=inputs.learnings,` in the `orchestrate(...)` call. Add one docstring line to `_run_review` if it has one, else none: "``forge`` (#102) replaces the forge ``make_forge`` would build; the gate passes its capturing forge."

- [ ] **Step 6: Run the tests, the standards suite, and the full suite**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_rules_source.py tests/test_issue_68_standards_context.py tests/test_orchestrator.py -q`
Expected: all pass (`tests/gate/test_rules_source.py`: `11 passed`).
Then `uv run pytest -q` with `timeout: 300000`: 0 failed. If a run-config key-set test fails, the change leaked a new key into a record; this task adds none, so fix the code, not the pin. Then `uv run ruff check src tests`.

- [ ] **Step 7: Commit**

```bash
cd ~/code/prxref/.claude/worktrees/sdlc-gate
git add src/prxref/gate/rules_source.py src/prxref/orchestrator.py src/prxref/cli.py tests/gate/test_rules_source.py
git commit -m "feat(gate): base-pinned repository rules and standards (#102)" -m "Claude-Session-Id: <session UUID>"
```

---
### Task 7: `checks.py`: the check-run and review publisher

Maps a review result to a check conclusion (spec "Verdict mapping"), owns every write to GitHub (check run, `COMMENT` review, the bot's summary comment, annotations), and makes each write idempotent against an uncertain reply. Every write is preceded by a lease renewal, so a worker that lost its fencing token publishes nothing.

The summary comment is upserted by the publisher, not by `ForgeImpl.post_summary`: that method takes the first comment carrying `SUMMARY_MARKER` whoever wrote it (`src/prxref/forges/github.py:549 (_find_summary)`), so on a PR the advisory lane already reviewed the bot would try to PATCH another user's comment and get a 403. The publisher matches the marker and `user.login == bot_login`.

**Files:**
- Create: `src/prxref/gate/checks.py`
- Test: `tests/gate/test_checks.py`

**Interfaces:**
- Consumes: `AppAuth.token_for(repo) -> str`, `AppAuth.invalidate(repo) -> None` (Task 3); `Store`, `Generation`, `LeaseLost` (Task 4); `CHECK_NAME` (Task 5); `prxref.forges.base.InlineComment`, `SUMMARY_MARKER`, `with_summary_marker` (`src/prxref/forges/base.py:35`, `:267`, `:301`); `prxref.triage.Finding`.
- Produces (all in `prxref.gate.checks`):
  - `REVIEW_MARKER = "<!-- sdlc-gate:generation={external_id} -->"`; `ANNOTATION_BATCH = 50`; `SUMMARY_LIMIT = 60000`; `VERDICTS = frozenset({"Approved", "Request-Changes", "Incomplete", "Error"})`
  - `class GitHubError(RuntimeError)` with `.status: int | None`, `.request_id: str | None`; `class UncertainReply(GitHubError)` (timeout, connection error or 5xx)
  - `@dataclass(frozen=True) class Lease(holder: str, token: int, ttl_s: float)`
  - `@dataclass(frozen=True) class Captured(summary: str | None = None, comments: tuple[InlineComment, ...] = ())`
  - `def conclusion_for(result: object) -> tuple[str, str]` (`(conclusion, reason)`)
  - `def annotations_for(findings: Sequence[Finding], demoted: Sequence[InlineComment]) -> list[dict]`
  - `def summary_text(gen: Generation, result: object, conclusion: str, reason: str, *, effort: str, run_dir: str) -> str`
  - `class GitHubClient(auth, session, *, api: str = "https://api.github.com")` with `request(method: str, repo: str, path: str, *, json: dict | None = None, params: dict | None = None) -> Any`
  - `class Stale(RuntimeError)` with `.reason: str` (raised by a guard; the generation must not publish)
  - `class Publisher(client: GitHubClient, store: Store, *, app_id: int, bot_login: str, guard: Callable[[Generation], None] | None = None)` with (`guard` runs after the lease renewal before every write of `begin` and `complete`, never of `fail` or `cancel`)
    - `begin(gen: Generation, lease: Lease) -> int` (check run id)
    - `complete(gen: Generation, lease: Lease, result: object, captured: Captured, *, effort: str, run_dir: str) -> str` (the conclusion)
    - `fail(gen: Generation, lease: Lease, reason: str) -> None`
    - `cancel(gen: Generation, lease: Lease, reason: str) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/gate/test_checks.py`:

```python
"""The sdlc-review publisher (#102)."""

import re

import pytest
import requests

from prxref.forges.base import SUMMARY_MARKER, InlineComment
from prxref.gate import checks
from prxref.gate.store import Store
from prxref.triage import Finding

REPO, HEAD, BASE = "acme/widget", "h" * 40, "b" * 40


class _Resp:
    def __init__(self, status, payload=None, request_id="RID-1"):
        self.status_code = status
        self._payload = payload
        self.headers = {"X-GitHub-Request-Id": request_id}
        self.content = b"x" if payload is not None else b""

    def json(self):
        return self._payload


class _GitHub:
    """Routes ``(method, path regex)`` to scripted replies; records every call."""

    def __init__(self, routes):
        self.routes = [(m, re.compile(p), list(r)) for m, p, r in routes]
        self.calls = []

    def request(self, method, url, headers=None, timeout=None, json=None, params=None):
        self.calls.append((method, url, headers, json, params))
        for m, pattern, replies in self.routes:
            if m == method and pattern.search(url):
                reply = replies.pop(0) if len(replies) > 1 else replies[0]
                if isinstance(reply, Exception):
                    raise reply
                return reply
        raise AssertionError(f"unrouted {method} {url}")

    def sent(self, method, pattern):
        return [c for c in self.calls if c[0] == method and re.search(pattern, c[1])]


class _Auth:
    def __init__(self):
        self.minted = 0

    def token_for(self, repo):
        self.minted += 1
        return f"ghs_TOKEN{self.minted}"

    def invalidate(self, repo):
        self.invalidated = repo


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "gate.db")


@pytest.fixture
def lease(store):
    return checks.Lease("host:1", store.acquire_lease("host:1", 600), 600)


@pytest.fixture
def gen(store, lease):
    g = store.enqueue(REPO, 7, HEAD, BASE, "main", "opened")
    return store.start(g.id, lease.token)


def _publisher(gh, store):
    client = checks.GitHubClient(_Auth(), gh)
    return checks.Publisher(client, store, app_id=99, bot_login="sdlc[bot]")


def _finding(path="a.py", line=0, severity="warning"):
    return Finding(file=path, line=line, severity=severity, confidence=0.9, title="t", body="b")


@pytest.mark.parametrize("result, conclusion", [
    ({"verdict": "Approved", "chunks_failed": 0}, "success"),
    ({"verdict": "Request-Changes"}, "failure"),
    ({"verdict": "Incomplete"}, "failure"),
    ({"verdict": "Approved", "chunks_failed": 1}, "failure"),
    ({"verdict": "Approved", "overflow_files": 2}, "failure"),
    ({"verdict": "Approved", "degraded": {"post": "failed"}}, "failure"),
    ({"verdict": "Error"}, "failure"),
    ("not a dict", "failure"),
    ({"verdict": "Maybe"}, "failure"),
])
def test_conclusion_follows_the_verdict_table(result, conclusion):
    assert checks.conclusion_for(result)[0] == conclusion


def test_a_401_refreshes_the_token_once_and_retries(store):
    gh = _GitHub([("GET", r"/pulls/7$", [_Resp(401, {"message": "Bad credentials"}), _Resp(200, {"n": 1})])])
    client = checks.GitHubClient(_Auth(), gh)
    assert client.request("GET", REPO, "/pulls/7") == {"n": 1}
    tokens = [c[2]["Authorization"] for c in gh.calls]
    assert tokens == ["token ghs_TOKEN1", "token ghs_TOKEN2"]


def test_a_second_401_is_an_error_without_the_token():
    gh = _GitHub([("GET", r"/pulls/7$", [_Resp(401, {"message": "Bad credentials"})])])
    with pytest.raises(checks.GitHubError) as err:
        checks.GitHubClient(_Auth(), gh).request("GET", REPO, "/pulls/7")
    assert err.value.status == 401 and err.value.request_id == "RID-1"
    assert "ghs_" not in str(err.value)


def test_begin_creates_an_in_progress_check_with_external_id_and_details_url(store, gen, lease):
    gh = _GitHub([("POST", r"/check-runs$", [_Resp(201, {"id": 555})])])
    assert _publisher(gh, store).begin(gen, lease) == 555
    body = gh.sent("POST", r"/check-runs$")[0][3]
    assert body["name"] == "sdlc-review" and body["head_sha"] == HEAD
    assert body["external_id"] == gen.external_id and body["status"] == "in_progress"
    assert body["details_url"] == "https://github.com/acme/widget/pull/7/checks"
    assert store.get(gen.id).check_run_id == 555


def test_begin_reuses_a_stored_check_run_id(store, gen, lease):
    store.set_check_run(gen.id, lease.token, 555)
    gh = _GitHub([("PATCH", r"/check-runs/555$", [_Resp(200, {"id": 555})])])
    assert _publisher(gh, store).begin(store.get(gen.id), lease) == 555
    assert not gh.sent("POST", r"/check-runs")


def test_begin_after_a_timeout_finds_the_check_instead_of_creating_a_second(store, gen, lease):
    found = {"check_runs": [{"id": 556, "external_id": gen.external_id}]}
    gh = _GitHub([
        ("POST", r"/check-runs$", [requests.Timeout("slow")]),
        ("GET", rf"/commits/{HEAD}/check-runs$", [_Resp(200, found)]),
    ])
    assert _publisher(gh, store).begin(gen, lease) == 556
    assert len(gh.sent("POST", r"/check-runs$")) == 1
    assert gh.sent("GET", r"/check-runs$")[0][4] == {"check_name": "sdlc-review", "app_id": 99, "filter": "all"}


def _complete(gh, store, gen, lease, result, captured=None):
    captured = captured if captured is not None else checks.Captured()
    store.set_check_run(gen.id, lease.token, 555)
    return _publisher(gh, store).complete(
        store.get(gen.id), lease, result, captured, effort="high", run_dir="/runs/1",
    )


def test_annotations_are_batched_by_fifty(store, gen, lease):
    findings = [_finding(f"f{i}.py") for i in range(120)]
    gh = _GitHub([("PATCH", r"/check-runs/555$", [_Resp(200, {"id": 555})])])
    _complete(gh, store, gen, lease, {"verdict": "Approved", "findings_active": findings})
    patches = [c[3] for c in gh.sent("PATCH", r"/check-runs/555$")]
    assert [len(p["output"]["annotations"]) for p in patches] == [50, 50, 20]
    assert [p.get("conclusion") for p in patches] == [None, None, "success"]
    assert patches[-1]["status"] == "completed"


def test_review_is_posted_as_comment_with_the_generation_marker(store, gen, lease):
    gh = _GitHub([
        ("POST", r"/pulls/7/reviews$", [_Resp(200, {"id": 9})]),
        ("PATCH", r"/check-runs/555$", [_Resp(200, {})]),
    ])
    comment = InlineComment(path="a.py", line=3, body="**warning** x")
    _complete(gh, store, gen, lease, {"verdict": "Approved"}, checks.Captured(comments=(comment,)))
    review = gh.sent("POST", r"/reviews$")[0][3]
    assert review["event"] == "COMMENT" and review["commit_id"] == HEAD
    assert f"<!-- sdlc-gate:generation={gen.external_id} -->" in review["body"]
    assert review["comments"] == [{"path": "a.py", "line": 3, "side": "RIGHT", "body": "**warning** x"}]
    assert store.publication(gen.id) == ("review", 9)


def test_review_after_a_5xx_is_found_by_marker_not_reposted(store, gen, lease):
    marker = f"<!-- sdlc-gate:generation={gen.external_id} -->"
    existing = [{"id": 10, "user": {"login": "sdlc[bot]"}, "body": f"{marker}\nsdlc review"}]
    gh = _GitHub([
        ("POST", r"/pulls/7/reviews$", [_Resp(502, {"message": "Bad Gateway"})]),
        ("GET", r"/pulls/7/reviews$", [_Resp(200, existing)]),
        ("PATCH", r"/check-runs/555$", [_Resp(200, {})]),
    ])
    comment = InlineComment(path="a.py", line=3, body="x")
    _complete(gh, store, gen, lease, {"verdict": "Approved"}, checks.Captured(comments=(comment,)))
    assert len(gh.sent("POST", r"/reviews$")) == 1
    assert store.publication(gen.id) == ("review", 10)


def test_a_422_review_demotes_comments_to_annotations(store, gen, lease):
    gh = _GitHub([
        ("POST", r"/pulls/7/reviews$", [_Resp(422, {"message": "line must be part of the diff"})]),
        ("PATCH", r"/check-runs/555$", [_Resp(200, {})]),
    ])
    comment = InlineComment(path="a.py", line=3, body="x")
    _complete(gh, store, gen, lease, {"verdict": "Approved"}, checks.Captured(comments=(comment,)))
    annotation = gh.sent("PATCH", r"/check-runs/555$")[-1][3]["output"]["annotations"][0]
    assert (annotation["path"], annotation["start_line"], annotation["message"]) == ("a.py", 3, "x")
    assert store.publication(gen.id) is None


def test_summary_comment_is_updated_only_when_authored_by_the_bot(store, gen, lease):
    feed = [
        {"id": 1, "user": {"login": "prxref-advisor"}, "body": f"{SUMMARY_MARKER}\nadvisory"},
        {"id": 2, "user": {"login": "sdlc[bot]"}, "body": f"{SUMMARY_MARKER}\nold"},
    ]
    gh = _GitHub([
        ("GET", r"/issues/7/comments$", [_Resp(200, feed)]),
        ("PATCH", r"/issues/comments/2$", [_Resp(200, {})]),
        ("PATCH", r"/check-runs/555$", [_Resp(200, {})]),
    ])
    _complete(gh, store, gen, lease, {"verdict": "Approved"}, checks.Captured(summary="new summary"))
    assert SUMMARY_MARKER in gh.sent("PATCH", r"/issues/comments/2$")[0][3]["body"]
    assert not gh.sent("PATCH", r"/issues/comments/1$")
    assert not gh.sent("POST", r"/issues/7/comments$")


def test_publish_after_lease_loss_writes_nothing(store, gen, lease):
    store.acquire_lease("other:2", 600, holder_alive=lambda h: False)
    gh = _GitHub([])
    with pytest.raises(checks.LeaseLost):
        _publisher(gh, store).begin(gen, lease)
    assert gh.calls == []


def test_a_guard_refusal_stops_the_publish_before_any_write(store, gen, lease):
    def guard(g):
        raise checks.Stale("head moved")

    gh = _GitHub([])
    publisher = checks.Publisher(checks.GitHubClient(_Auth(), gh), store, app_id=99,
                                 bot_login="sdlc[bot]", guard=guard)
    with pytest.raises(checks.Stale):
        publisher.begin(gen, lease)
    assert gh.calls == []


def test_summary_names_model_effort_tokens_and_shas(gen):
    result = {"verdict": "Approved", "input_tokens": 1200, "output_tokens": 300,
              "chunk_count": 3, "chunks_reviewed": 3, "chunks_failed": 0,
              "sampling": {"models": ["bedrock/us.anthropic.claude-opus"]}}
    text = checks.summary_text(gen, result, "success", "approved", effort="high", run_dir="/runs/1")
    for needle in ("bedrock/us.anthropic.claude-opus", "high", "1200", "300", "Approved",
                   "3/3", HEAD, BASE, "/runs/1"):
        assert needle in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_checks.py -q`
Expected: FAIL (`ImportError: cannot import name 'checks'`).

- [ ] **Step 3: Write `src/prxref/gate/checks.py`**

```python
"""Publish a gate generation to GitHub as the ``sdlc-review`` check (#102).

Every write renews the worker lease first (:class:`Lease`), so a worker
whose fencing token is no longer current raises :class:`LeaseLost` before
it touches GitHub. A timeout or 5xx on a create is answered by looking the
object up (the check by ``external_id``, the review by the bot login and
:data:`REVIEW_MARKER`) before one retry, so a duplicate is never created.
Errors carry the HTTP status and ``X-GitHub-Request-Id``, never a token.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import requests

from prxref.forges.base import SUMMARY_MARKER, InlineComment, with_summary_marker
from prxref.gate.receiver import CHECK_NAME
from prxref.gate.store import Generation, LeaseLost, Store
from prxref.triage import Finding

__all__ = [
    "ANNOTATION_BATCH", "Captured", "GitHubClient", "GitHubError", "Lease", "LeaseLost",
    "Publisher", "REVIEW_MARKER", "Stale", "UncertainReply", "annotations_for", "conclusion_for",
    "summary_text",
]

REVIEW_MARKER = "<!-- sdlc-gate:generation={external_id} -->"
ANNOTATION_BATCH = 50
SUMMARY_LIMIT = 60000
VERDICTS = frozenset({"Approved", "Request-Changes", "Incomplete", "Error"})
_LEVELS = {"error": "failure", "warning": "warning"}
_TIMEOUT_S = 30


class GitHubError(RuntimeError):
    """A GitHub API call failed; the message names the status and request id only."""

    def __init__(self, message: str, *, status: int | None = None, request_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.request_id = request_id


class UncertainReply(GitHubError):
    """A timeout, a dropped connection or a 5xx: the write may or may not have landed."""


class Stale(RuntimeError):
    """The live PR no longer matches the generation; it must not publish."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Lease:
    """The worker's lease, renewed before every write."""

    holder: str
    token: int
    ttl_s: float


@dataclass(frozen=True)
class Captured:
    """What the review asked to post: the summary body and the inline comments."""

    summary: str | None = None
    comments: tuple[InlineComment, ...] = ()


def conclusion_for(result: object) -> tuple[str, str]:
    """Map a review result to ``(conclusion, reason)``; only a clean ``Approved`` succeeds."""
    if not isinstance(result, dict) or result.get("verdict") not in VERDICTS:
        return "failure", "malformed review result"
    verdict = result["verdict"]
    if verdict == "Error":
        return "failure", "review error: the error class is in the summary comment and the run record"
    if verdict == "Incomplete" or (result.get("chunks_failed") or 0) > 0:
        return "failure", "incomplete review: a partial review is not a review"
    if (result.get("overflow_files") or 0) > 0:
        return "failure", f"{result['overflow_files']} file(s) did not fit the review budget"
    if result.get("degraded"):
        return "failure", "degraded review"
    if verdict == "Request-Changes":
        return "failure", "error-severity findings"
    return "success", "approved"


def annotations_for(findings: Sequence[Finding], demoted: Sequence[InlineComment]) -> list[dict]:
    """File-level findings, then inline comments GitHub refused, as check annotations."""
    out = [
        {"path": f.file, "start_line": 1, "end_line": 1,
         "annotation_level": _LEVELS.get(f.severity, "notice"),
         "title": f.title[:255], "message": f.body or f.title}
        for f in findings if isinstance(f, Finding) and f.line == 0
    ]
    out += [
        {"path": c.path, "start_line": c.start_line or c.line, "end_line": c.line,
         "annotation_level": "warning", "message": c.body}
        for c in demoted
    ]
    return out


def summary_text(
    gen: Generation, result: object, conclusion: str, reason: str, *, effort: str, run_dir: str,
) -> str:
    """The check output summary: model, effort, tokens, verdict, chunks, SHAs, run record."""
    r = result if isinstance(result, dict) else {}
    models = ", ".join((r.get("sampling") or {}).get("models") or []) or "unknown"
    lines = [
        f"**{conclusion}**: {reason}",
        "",
        f"- Verdict: {r.get('verdict', 'none')}",
        f"- Model: {models} (effort: {effort or 'default'})",
        f"- Tokens: {r.get('input_tokens', 0)} in, {r.get('output_tokens', 0)} out",
        f"- Chunks reviewed: {r.get('chunks_reviewed', 0)}/{r.get('chunk_count', 0)}"
        f" ({r.get('chunks_failed', 0)} failed)",
        f"- Base: `{gen.base_sha}` ({gen.base_ref}); head: `{gen.head_sha}`",
        f"- Generation {gen.external_id}; run record: `{run_dir}`",
    ]
    return "\n".join(lines)[:SUMMARY_LIMIT]


class GitHubClient:
    """Installation-token REST calls for one repository at a time."""

    def __init__(self, auth: Any, session: Any, *, api: str = "https://api.github.com"):
        self.auth = auth
        self.session = session
        self.api = api.rstrip("/")

    def _send(self, method: str, repo: str, path: str, json: dict | None, params: dict | None):
        headers = {
            "Authorization": f"token {self.auth.token_for(repo)}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            return self.session.request(
                method, f"{self.api}/repos/{repo}{path}", headers=headers,
                timeout=_TIMEOUT_S, json=json, params=params,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise UncertainReply(f"{method} {path}: {type(exc).__name__}") from None

    def request(self, method: str, repo: str, path: str, *, json: dict | None = None,
                params: dict | None = None) -> Any:
        """Send one call; a 401 mints a fresh token and retries once."""
        resp = self._send(method, repo, path, json, params)
        if resp.status_code == 401:
            self.auth.invalidate(repo)
            resp = self._send(method, repo, path, json, params)
        rid = resp.headers.get("X-GitHub-Request-Id")
        if resp.status_code >= 500:
            raise UncertainReply(f"{method} {path}: HTTP {resp.status_code} (request {rid})",
                                 status=resp.status_code, request_id=rid)
        if resp.status_code >= 400:
            raise GitHubError(f"{method} {path}: HTTP {resp.status_code} (request {rid})",
                              status=resp.status_code, request_id=rid)
        return resp.json() if resp.content else None


class Publisher:
    """Every GitHub write for one generation; fenced by the worker lease."""

    def __init__(self, client: GitHubClient, store: Store, *, app_id: int, bot_login: str,
                 guard: Callable[[Generation], None] | None = None):
        self.client = client
        self.store = store
        self.app_id = app_id
        self.bot_login = bot_login
        self.guard = guard

    def _fence(self, lease: Lease, gen: Generation | None = None) -> None:
        if not self.store.renew_lease(lease.holder, lease.token, lease.ttl_s):
            raise LeaseLost(f"fencing token {lease.token} is not the live worker lease")
        if gen is not None and self.guard is not None:
            self.guard(gen)

    def _find_check(self, gen: Generation) -> int | None:
        data = self.client.request(
            "GET", gen.repo, f"/commits/{gen.head_sha}/check-runs",
            params={"check_name": CHECK_NAME, "app_id": self.app_id, "filter": "all"},
        )
        for run in (data or {}).get("check_runs", []):
            if run.get("external_id") == gen.external_id:
                return run["id"]
        return None

    def begin(self, gen: Generation, lease: Lease) -> int:
        """Create the in-progress check, or move the stored one back to in progress."""
        self._fence(lease, gen)
        if gen.check_run_id is not None:
            self.client.request("PATCH", gen.repo, f"/check-runs/{gen.check_run_id}",
                                json={"status": "in_progress"})
            return gen.check_run_id
        body = {
            "name": CHECK_NAME, "head_sha": gen.head_sha, "external_id": gen.external_id,
            "status": "in_progress",
            "details_url": f"https://github.com/{gen.repo}/pull/{gen.pr_number}/checks",
            "output": {"title": "sdlc review running",
                       "summary": f"Reviewing `{gen.base_sha}`..`{gen.head_sha}`."},
        }
        try:
            check_id = self.client.request("POST", gen.repo, "/check-runs", json=body)["id"]
        except UncertainReply:
            check_id = self._find_check(gen)
            if check_id is None:
                check_id = self.client.request("POST", gen.repo, "/check-runs", json=body)["id"]
        self.store.set_check_run(gen.id, lease.token, check_id)
        return check_id

    def _find_review(self, gen: Generation, marker: str) -> int | None:
        reviews = self.client.request("GET", gen.repo, f"/pulls/{gen.pr_number}/reviews",
                                      params={"per_page": 100}) or []
        for review in reviews:
            if (review.get("user") or {}).get("login") == self.bot_login and marker in (review.get("body") or ""):
                return review["id"]
        return None

    def _post_review(self, gen: Generation, comments: Sequence[InlineComment]) -> int | None:
        """Post the ``COMMENT`` review; ``None`` when GitHub refuses its anchors (422)."""
        marker = REVIEW_MARKER.format(external_id=gen.external_id)
        payload = {
            "commit_id": gen.head_sha, "event": "COMMENT",
            "body": f"{marker}\nsdlc review: {len(comments)} inline finding(s).",
            "comments": [
                {"path": c.path, "line": c.line, "side": c.side, "body": c.body,
                 **({"start_line": c.start_line, "start_side": c.side} if c.start_line else {})}
                for c in comments
            ],
        }
        path = f"/pulls/{gen.pr_number}/reviews"
        try:
            return self.client.request("POST", gen.repo, path, json=payload)["id"]
        except UncertainReply:
            found = self._find_review(gen, marker)
            if found is not None:
                return found
            return self.client.request("POST", gen.repo, path, json=payload)["id"]
        except GitHubError as exc:
            if exc.status == 422:
                return None
            raise

    def _upsert_summary(self, gen: Generation, body: str) -> None:
        page, mine = 1, None
        while mine is None:
            feed = self.client.request("GET", gen.repo, f"/issues/{gen.pr_number}/comments",
                                       params={"per_page": 100, "page": page}) or []
            mine = next((c["id"] for c in feed
                         if (c.get("user") or {}).get("login") == self.bot_login
                         and SUMMARY_MARKER in (c.get("body") or "")), None)
            if len(feed) < 100:
                break
            page += 1
        body = with_summary_marker(body)
        if mine is not None:
            self.client.request("PATCH", gen.repo, f"/issues/comments/{mine}", json={"body": body})
        else:
            self.client.request("POST", gen.repo, f"/issues/{gen.pr_number}/comments", json={"body": body})

    def _close_check(self, gen: Generation, lease: Lease, conclusion: str, title: str,
                     summary: str, annotations: Sequence[dict] = (), *, guarded: bool = False) -> None:
        batches = [list(annotations[i:i + ANNOTATION_BATCH])
                   for i in range(0, len(annotations), ANNOTATION_BATCH)] or [[]]
        path = f"/check-runs/{gen.check_run_id}"
        for index, batch in enumerate(batches):
            self._fence(lease, gen if guarded else None)
            output = {"title": title, "summary": summary, "annotations": batch}
            if index < len(batches) - 1:
                self.client.request("PATCH", gen.repo, path, json={"output": output})
            else:
                self.client.request("PATCH", gen.repo, path, json={
                    "status": "completed", "conclusion": conclusion, "output": output,
                })

    def complete(self, gen: Generation, lease: Lease, result: object, captured: Captured,
                 *, effort: str, run_dir: str) -> str:
        """Post the review and summary comment, then close the check; returns the conclusion."""
        conclusion, reason = conclusion_for(result)
        findings = result.get("findings_active") or [] if isinstance(result, dict) else []
        demoted: list[InlineComment] = []
        if captured.comments and self.store.publication(gen.id) is None:
            self._fence(lease, gen)
            review_id = self._post_review(gen, captured.comments)
            if review_id is None:
                demoted = list(captured.comments)
            else:
                self.store.record_publication(gen.id, lease.token, "review", review_id)
        if captured.summary:
            self._fence(lease, gen)
            self._upsert_summary(gen, captured.summary)
        text = summary_text(gen, result, conclusion, reason, effort=effort, run_dir=run_dir)
        self._close_check(gen, lease, conclusion, f"sdlc review: {conclusion}", text,
                          annotations_for(findings, demoted), guarded=True)
        return conclusion

    def fail(self, gen: Generation, lease: Lease, reason: str) -> None:
        """Close the check as ``failure`` with ``reason`` (deadline, crash, publish error)."""
        gen = self.store.get(gen.id)
        if gen.check_run_id is None:
            self.begin(gen, lease)
            gen = self.store.get(gen.id)
        self._close_check(gen, lease, "failure", "sdlc review: failure", f"**failure**: {reason}")

    def cancel(self, gen: Generation, lease: Lease, reason: str) -> None:
        """Close an existing check as ``cancelled``; a generation with no check posts nothing."""
        gen = self.store.get(gen.id)
        if gen.check_run_id is not None:
            self._close_check(gen, lease, "cancelled", "sdlc review: cancelled", reason)
```

`requests` is already a core dependency (`pyproject.toml` `dependencies`), so the import costs nothing. The fake session in the tests raises `requests.Timeout`, the exception the real `requests.Session.request` raises.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_checks.py -q`
Expected: `22 passed` (9 parametrized conclusions + 13 tests).
Then: `uv run ruff check src tests`

- [ ] **Step 5: Commit**

```bash
cd ~/code/prxref/.claude/worktrees/sdlc-gate
git add src/prxref/gate/checks.py tests/gate/test_checks.py
git commit -m "feat(gate): sdlc-review check and COMMENT review publisher (#102)" -m "Claude-Session-Id: <session UUID>"
```

---
### Task 8: `binding.py`: the live binding guard

Immediately before every write of `Publisher.begin` and `Publisher.complete` (Task 7's `guard`), re-reads the PR. A generation publishes only while the live head SHA, base SHA and base ref equal its own, the PR is open, and no newer generation exists for the PR (spec "binding.py").

**Files:**
- Create: `src/prxref/gate/binding.py`
- Test: `tests/gate/test_binding.py`

**Interfaces:**
- Consumes: `GitHubClient.request` and `Stale` (Task 7); `Store.latest_generation(repo, pr_number) -> Generation | None`, `Generation` (Task 4).
- Produces (all in `prxref.gate.binding`):
  - `@dataclass(frozen=True) class LivePR(head_sha: str, base_sha: str, base_ref: str, state: str, merged: bool)`
  - `def read_live_pr(client, repo: str, number: int) -> LivePR`
  - `def check_binding(gen: Generation, live: LivePR, store: Store) -> str | None` (`None` = bound; else one of `"closed"`, `"head moved"`, `"retargeted"`, `"base moved"`, `"superseded"`)
  - `def make_guard(client, store: Store) -> Callable[[Generation], None]` (raises `Stale(reason)`)

- [ ] **Step 1: Write the failing tests**

`tests/gate/test_binding.py`:

```python
"""The live binding guard (#102)."""

import pytest

from prxref.gate import binding
from prxref.gate.checks import Stale
from prxref.gate.store import Store

REPO, HEAD, BASE = "acme/widget", "h" * 40, "b" * 40


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "gate.db")


@pytest.fixture
def gen(store):
    return store.enqueue(REPO, 7, HEAD, BASE, "main", "opened")


def _live(**over):
    fields = {"head_sha": HEAD, "base_sha": BASE, "base_ref": "main", "state": "open", "merged": False}
    return binding.LivePR(**{**fields, **over})


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def request(self, method, repo, path, **kwargs):
        self.calls.append((method, repo, path))
        return self.payload


def _pull(head=HEAD, base=BASE, ref="main", state="open"):
    return {"head": {"sha": head}, "base": {"sha": base, "ref": ref}, "state": state, "merged": False}


def test_read_live_pr_parses_the_pull(store):
    client = _Client(_pull())
    assert binding.read_live_pr(client, REPO, 7) == _live()
    assert client.calls == [("GET", REPO, "/pulls/7")]


def test_a_matching_open_pr_is_bound(store, gen):
    assert binding.check_binding(gen, _live(), store) is None


@pytest.mark.parametrize("over, reason", [
    ({"state": "closed"}, "closed"),
    ({"state": "closed", "merged": True}, "closed"),
    ({"head_sha": "c" * 40}, "head moved"),
    ({"base_ref": "release"}, "retargeted"),
    ({"base_sha": "d" * 40}, "base moved"),
])
def test_a_drifted_pr_is_not_bound(store, gen, over, reason):
    assert binding.check_binding(gen, _live(**over), store) == reason


def test_a_newer_generation_supersedes_even_on_the_same_head(store, gen):
    store.enqueue(REPO, 7, HEAD, BASE, "main", "rerequested")
    assert binding.check_binding(gen, _live(), store) == "superseded"


def test_guard_raises_stale_with_the_reason(store, gen):
    guard = binding.make_guard(_Client(_pull(head="c" * 40)), store)
    with pytest.raises(Stale) as err:
        guard(gen)
    assert err.value.reason == "head moved"


def test_guard_passes_a_bound_generation(store, gen):
    assert binding.make_guard(_Client(_pull()), store)(gen) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_binding.py -q`
Expected: FAIL (`ImportError: cannot import name 'binding'`).

- [ ] **Step 3: Write `src/prxref/gate/binding.py`**

```python
"""The live binding guard (#102).

A generation is bound to the PR state it was queued for: head SHA, base
SHA, base ref, open. Before every publishing write the guard re-reads the
PR; any drift, or a newer generation for the PR, means the generation is
stale and publishes nothing, because a newer generation owns the result.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from prxref.gate.checks import Stale
from prxref.gate.store import Generation, Store


@dataclass(frozen=True)
class LivePR:
    """The fields of a live pull request the binding compares."""

    head_sha: str
    base_sha: str
    base_ref: str
    state: str
    merged: bool


def read_live_pr(client: Any, repo: str, number: int) -> LivePR:
    """``GET /repos/{repo}/pulls/{number}`` reduced to a :class:`LivePR`."""
    pull = client.request("GET", repo, f"/pulls/{number}")
    return LivePR(
        head_sha=pull["head"]["sha"], base_sha=pull["base"]["sha"], base_ref=pull["base"]["ref"],
        state=pull["state"], merged=bool(pull.get("merged")),
    )


def check_binding(gen: Generation, live: LivePR, store: Store) -> str | None:
    """``None`` while ``gen`` may publish; else why it may not."""
    if live.state != "open":
        return "closed"
    if live.head_sha != gen.head_sha:
        return "head moved"
    if live.base_ref != gen.base_ref:
        return "retargeted"
    if live.base_sha != gen.base_sha:
        return "base moved"
    latest = store.latest_generation(gen.repo, gen.pr_number)
    if latest is not None and latest.gen > gen.gen:
        return "superseded"
    return None


def make_guard(client: Any, store: Store) -> Callable[[Generation], None]:
    """A :class:`~prxref.gate.checks.Publisher` guard that raises :class:`Stale`."""

    def guard(gen: Generation) -> None:
        reason = check_binding(gen, read_live_pr(client, gen.repo, gen.pr_number), store)
        if reason is not None:
            raise Stale(reason)

    return guard
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_binding.py -q`
Expected: `10 passed` (5 parametrized + 5 tests).
Then: `uv run ruff check src tests`

- [ ] **Step 5: Commit**

```bash
cd ~/code/prxref/.claude/worktrees/sdlc-gate
git add src/prxref/gate/binding.py tests/gate/test_binding.py
git commit -m "feat(gate): live binding guard before every publish (#102)" -m "Claude-Session-Id: <session UUID>"
```

---

### Task 9: `worker.py`: review execution, deadline, and the worker loop

Runs one generation end to end: start (fenced), binding check, `begin`, the review inside a deadline, `complete`, finish. The review is prxref's own `_run_review` over a **capturing forge**: reads go to the installation-token forge with the PR's SHAs pinned to the generation, and the two post calls are captured, so the orchestrator renders every body with its own `formatter` and `markers` (spec "checks.py": "Comment bodies come from prxref's own formatter") while all GitHub writes stay in the publisher. `post=False` would render nothing (see "Spec questions").

**Files:**
- Create: `src/prxref/gate/worker.py`
- Test: `tests/gate/test_worker.py`

**Interfaces:**
- Consumes: `cli._run_review(url, *, forge=..., config_file=..., context_file="", evidence_files=[""], prompts_dir="", full_review=True)` (Task 6), `cli._build_json_result(result) -> dict` (`src/prxref/cli.py:1196`); `materialize_rules`, `rules_sha` (Task 6); `Lease`, `Captured`, `Stale`, `Publisher`, `conclusion_for` (Task 7); `read_live_pr`, `check_binding` (Task 8); `Store`, `Generation`, `LeaseLost`, `holder_alive`, `Store.adopt_orphans` (Task 4); `prxref.forges.github.ForgeImpl` (`src/prxref/forges/github.py:228`, `_headers` at `:257`).
- Produces (all in `prxref.gate.worker`):
  - `class InstallationForge(ForgeImpl)`: `__init__(self, token_source: Callable[[], str], session=None)`; `_headers(host, extra=None)` sends `Bearer <token_source()>` and never reads `PRXREF_GITHUB_TOKEN`
  - `class CapturingForge(inner, gen: Generation)`: `get_pr`, `get_diff`, `get_summary` (always `None`), `post_summary`, `post_inline_comments`, `prune_inline_comments`, `seal() -> None`, `captured() -> Captured`; every other attribute forwards to `inner`
  - `@dataclass(frozen=True) class ReviewOutcome(result: object, captured: Captured, run_dir: Path)`
  - `def review_with_prxref(gen: Generation, inner: Any, *, rules_source: str, runs_dir: Path) -> ReviewOutcome`
  - `def run_generation(gen: Generation, *, store: Store, publisher: Publisher, client: Any, lease: Lease, review: Callable[[Generation], ReviewOutcome], deadline_s: float, effort: str) -> str` (the final state)
  - `class Worker(store, publisher, client, review, *, deadline_s: float, effort: str, holder: str | None = None, poll_s: float = 2.0)` with `acquire() -> Lease | None`, `run_once(lease) -> str | None`, `run(stop: threading.Event) -> None`; `ttl_s = deadline_s + 120`

- [ ] **Step 1: Write the failing tests**

`tests/gate/test_worker.py`:

```python
"""Gate worker: capture, rules at base, deadline, binding, loop (#102)."""

import json
import sys
import threading
import types
from types import SimpleNamespace

import pytest

from prxref import llm_backends
from prxref.forges.base import InlineComment
from prxref.gate import checks, worker
from prxref.gate.store import LeaseLost, Store
from tests.gate.test_rules_source import _ShaForge
from tests.test_issue_68_standards_context import TWO_CHUNK_DIFF, _RecordingLLM, _write
from tests.test_orchestrator import REF, FakeForge

REPO, HEAD, BASE = "acme/widget", "a" * 40, "b" * 40


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "gate.db")


@pytest.fixture
def lease(store):
    return checks.Lease("host:1", store.acquire_lease("host:1", 600), 600)


@pytest.fixture
def gen(store):
    return store.enqueue(REPO, 7, HEAD, BASE, "main", "opened")


def _roots(tmp_path, base_rules):
    base, head = tmp_path / "base", tmp_path / "head"
    _write(base, ".prxref.toml", 'review_rules = "rules/team.md"\n')
    _write(base, "rules/team.md", base_rules)
    _write(head, ".prxref.toml", 'review_rules = ""\n')
    return {BASE: base, HEAD: head}


def test_installation_forge_ignores_the_env_token(monkeypatch):
    monkeypatch.setenv("PRXREF_GITHUB_TOKEN", "env-token")
    headers = worker.InstallationForge(lambda: "ghs_inst")._headers("github.com", {"X": "1"})
    assert headers["Authorization"] == "Bearer ghs_inst"
    assert headers["X"] == "1"


def test_capturing_forge_pins_shas_and_captures_posts(gen):
    inner = FakeForge(diff=TWO_CHUNK_DIFF)
    cap = worker.CapturingForge(inner, gen)
    pr = cap.get_pr(REF)
    assert (pr.source_sha, pr.target_sha) == (gen.head_sha, gen.base_sha)
    assert cap.get_diff(REF) == TWO_CHUNK_DIFF and cap.get_summary(REF) is None
    comment = InlineComment(path="a.py", line=1, body="x")
    cap.post_summary(REF, "summary")
    assert cap.post_inline_comments(REF, [comment]) == 1
    cap.seal()
    cap.post_summary(REF, "late")
    cap.post_inline_comments(REF, [comment])
    assert cap.captured() == checks.Captured(summary="summary", comments=(comment,))
    assert inner.summaries == [] and inner.inline_batches == []


@pytest.mark.parametrize("source, present", [("base", True), ("head", False)])
def test_review_reads_the_repo_config_at_the_rules_sha(tmp_path, monkeypatch, gen, source, present):
    llm = _RecordingLLM()
    monkeypatch.setattr(llm_backends, "create_llm_client", lambda cfg: llm)
    monkeypatch.setenv("PRXREF_RULES_SOURCE", source)
    inner = _ShaForge(TWO_CHUNK_DIFF, _roots(tmp_path, "- RULE-SENTINEL: never log tokens.\n"))
    outcome = worker.review_with_prxref(gen, inner, rules_source=source, runs_dir=tmp_path / "runs")
    prompts = "\n".join(user for _system, user in llm.calls)
    assert ("RULE-SENTINEL" in prompts) is present
    record = json.loads((outcome.run_dir / "result.json").read_text())
    assert record["verdict"] == outcome.result["verdict"]
    assert outcome.run_dir == tmp_path / "runs" / str(gen.id)


def test_litellm_receives_the_configured_timeout_unscaled(tmp_path, monkeypatch, gen):
    captured = []

    def fake_completion(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"findings": []}'))],
            usage=None, model="m",
        )

    monkeypatch.setenv("PRXREF_LLM_BACKEND", "litellm")
    monkeypatch.setenv("PRXREF_LLM_MODELS", "bedrock/test-model")
    monkeypatch.setenv("PRXREF_LLM_TIMEOUT", "900")
    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(completion=fake_completion))
    inner = _ShaForge(TWO_CHUNK_DIFF, _roots(tmp_path, "- a rule\n"))
    worker.review_with_prxref(gen, inner, rules_source="base", runs_dir=tmp_path / "runs")
    assert captured
    assert {call["timeout"] for call in captured} == {900.0}


class _Client:
    """Answers GET /pulls/7 with a scripted live PR."""

    def __init__(self, head=HEAD, base=BASE, state="open"):
        self.pull = {"head": {"sha": head}, "base": {"sha": base, "ref": "main"},
                     "state": state, "merged": False}

    def request(self, method, repo, path, **kwargs):
        assert (method, path) == ("GET", "/pulls/7")
        return self.pull


class _Publisher:
    def __init__(self, store, complete_raises=None):
        self.store = store
        self.calls = []
        self.complete_raises = complete_raises

    def begin(self, gen, lease):
        self.calls.append("begin")
        self.store.set_check_run(gen.id, lease.token, 1)
        return 1

    def complete(self, gen, lease, result, captured, *, effort, run_dir):
        self.calls.append("complete")
        if self.complete_raises:
            raise self.complete_raises
        return checks.conclusion_for(result)[0]

    def fail(self, gen, lease, reason):
        self.calls.append(f"fail:{reason}")

    def cancel(self, gen, lease, reason):
        self.calls.append(f"cancel:{reason}")


def _outcome(result, tmp_path):
    return worker.ReviewOutcome(result, checks.Captured(), tmp_path)


def _run(store, gen, lease, review, *, client=None, publisher=None, deadline_s=5.0):
    publisher = publisher or _Publisher(store)
    state = worker.run_generation(
        gen, store=store, publisher=publisher, client=client or _Client(), lease=lease,
        review=review, deadline_s=deadline_s, effort="high",
    )
    return state, publisher.calls, store.get(gen.id)


def test_an_approved_review_completes_as_success(store, gen, lease, tmp_path):
    state, calls, row = _run(store, gen, lease, lambda g: _outcome({"verdict": "Approved"}, tmp_path))
    assert (state, calls, row.conclusion) == ("completed", ["begin", "complete"], "success")


def test_a_deadline_hit_is_a_failure(store, gen, lease):
    release = threading.Event()

    def slow(g):
        release.wait(5)

    state, calls, row = _run(store, gen, lease, slow, deadline_s=0.05)
    release.set()
    assert (state, row.state, row.conclusion) == ("failed", "failed", "failure")
    assert calls[-1].startswith("fail:deadline")


def test_a_crash_mid_review_is_a_failure(store, gen, lease):
    def crash(g):
        raise RuntimeError("model went away")

    state, calls, row = _run(store, gen, lease, crash)
    assert (state, row.conclusion) == ("failed", "failure")
    assert calls[-1] == "fail:review crashed: RuntimeError"


@pytest.mark.parametrize("result", ["garbage", {"verdict": "Approved", "degraded": {"x": 1}}])
def test_a_malformed_or_degraded_result_completes_as_failure(store, gen, lease, tmp_path, result):
    state, _calls, row = _run(store, gen, lease, lambda g: _outcome(result, tmp_path))
    assert (state, row.conclusion) == ("completed", "failure")


def test_a_moved_head_goes_stale_and_queues_the_live_head(store, gen, lease, tmp_path):
    client = _Client(head="c" * 40)
    state, calls, row = _run(store, gen, lease, lambda g: pytest.fail("must not review"), client=client)
    assert (state, row.state, row.error, calls) == ("stale", "stale", "head moved", [])
    newer = store.latest_generation(REPO, 7)
    assert (newer.head_sha, newer.reason) == ("c" * 40, "repair")


def test_a_closed_pr_cancels(store, gen, lease):
    state, calls, row = _run(store, gen, lease, lambda g: pytest.fail("no"), client=_Client(state="closed"))
    assert (state, row.state, calls) == ("cancelled", "cancelled", ["cancel:PR closed"])


def test_stale_at_publish_publishes_nothing_more(store, gen, lease, tmp_path):
    publisher = _Publisher(store, complete_raises=checks.Stale("superseded"))
    state, calls, row = _run(store, gen, lease, lambda g: _outcome({"verdict": "Approved"}, tmp_path),
                             publisher=publisher)
    assert (state, row.state, calls) == ("stale", "stale", ["begin", "complete"])


def test_a_publish_error_finishes_failed_and_says_so(store, gen, lease, tmp_path):
    publisher = _Publisher(store, complete_raises=checks.GitHubError("PATCH: HTTP 422", status=422))
    state, calls, row = _run(store, gen, lease, lambda g: _outcome({"verdict": "Approved"}, tmp_path),
                             publisher=publisher)
    assert (state, row.state, row.conclusion) == ("failed", "failed", "failure")
    assert calls == ["begin", "complete", "fail:publish failed: GitHubError"]


def _worker(store, tmp_path, review=None, holder="host:1"):
    return worker.Worker(store, _Publisher(store), _Client(),
                         review or (lambda g: _outcome({"verdict": "Approved"}, tmp_path)),
                         deadline_s=5.0, effort="high", holder=holder)


def test_a_paused_worker_dequeues_nothing(store, gen, tmp_path):
    w = _worker(store, tmp_path)
    lease = w.acquire()
    store.set_paused(True)
    assert w.run_once(lease) is None
    assert store.get(gen.id).state == "queued"


def test_a_fresh_worker_adopts_an_orphan_of_a_dead_holder(store, gen, tmp_path):
    old = store.acquire_lease("host:999999", 600)
    store.start(gen.id, old)
    w = _worker(store, tmp_path, holder="host:2")
    assert w.acquire() is None
    store.acquire_lease("host:999999", -1)
    lease = w.acquire()
    assert lease is not None and store.get(gen.id).state == "queued"
    assert w.run_once(lease) == "completed"


def test_a_lost_lease_raises_before_dequeueing(store, gen, tmp_path):
    w = _worker(store, tmp_path)
    lease = w.acquire()
    store.acquire_lease("other:1", 600, holder_alive=lambda h: False)
    with pytest.raises(LeaseLost):
        w.run_once(lease)
    assert store.get(gen.id).state == "queued"
```

`FakeForge.summaries` and `FakeForge.inline_batches` are the lists its two post methods append to (`tests/test_orchestrator.py`, `class FakeForge.__init__`). In `test_a_fresh_worker_adopts_an_orphan_of_a_dead_holder` the holder `host:999999` is not this host's name, so `holder_alive` counts it alive and the first `acquire` returns `None`; the test then expires that lease (`ttl -1`), and the second `acquire` takes over and adopts the orphan.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_worker.py -q`
Expected: FAIL (`ImportError: cannot import name 'worker'`).

- [ ] **Step 3: Write `src/prxref/gate/worker.py`**

```python
"""Gate worker: one generation at a time, under a deadline (#102).

:func:`run_generation` starts a generation (fenced by the lease), checks
its binding to the live PR, opens the check, runs the review in a
one-thread pool bounded by ``deadline_s``, and publishes. A deadline hit
or a crash is a ``failure``. A generation whose head, base or base ref
moved goes ``stale`` and, when nothing live covers the PR's current head
and base, queues a ``repair`` generation for them, which repairs a
delivery that arrived out of order. A closed PR is ``cancelled``.

A deadline cannot stop the review thread; it runs on in the background,
and :meth:`CapturingForge.seal` makes its late posts no-ops, so it can
never publish.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from prxref import cli
from prxref.forges.github import ForgeImpl
from prxref.gate.binding import check_binding, read_live_pr
from prxref.gate.checks import Captured, Lease, Publisher, Stale
from prxref.gate.rules_source import materialize_rules, rules_sha
from prxref.gate.store import Generation, LeaseLost, Store, holder_alive

logger = logging.getLogger(__name__)

TTL_PAD_S = 120
_DRIFT = frozenset({"head moved", "base moved", "retargeted"})


class InstallationForge(ForgeImpl):
    """The GitHub adapter authenticated by an installation token, never ``PRXREF_GITHUB_TOKEN``."""

    def __init__(self, token_source: Callable[[], str], session: Any = None) -> None:
        super().__init__(session=session)
        self._token_source = token_source

    def _headers(self, host: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json",
                   "Authorization": f"Bearer {self._token_source()}"}
        if extra:
            headers.update(extra)
        return headers


class CapturingForge:
    """Reads through ``inner`` at the generation's SHAs; captures both posts."""

    def __init__(self, inner: Any, gen: Generation) -> None:
        self._inner = inner
        self._gen = gen
        self._summary: str | None = None
        self._comments: list = []
        self._sealed = False
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def get_pr(self, ref):
        return replace(self._inner.get_pr(ref), source_sha=self._gen.head_sha,
                       target_sha=self._gen.base_sha)

    def get_diff(self, ref) -> str:
        return self._inner.get_diff(ref)

    def get_summary(self, ref) -> None:
        return None

    def post_summary(self, ref, body: str) -> None:
        with self._lock:
            if not self._sealed:
                self._summary = body

    def post_inline_comments(self, ref, comments) -> int:
        with self._lock:
            if self._sealed:
                return 0
            self._comments.extend(comments)
            return len(comments)

    def prune_inline_comments(self, ref, paths=None) -> int:
        return 0

    def seal(self) -> None:
        with self._lock:
            self._sealed = True

    def captured(self) -> Captured:
        with self._lock:
            return Captured(summary=self._summary, comments=tuple(self._comments))


@dataclass(frozen=True)
class ReviewOutcome:
    """A finished review: the result dict, what it asked to post, and its run directory."""

    result: object
    captured: Captured
    run_dir: Path


def review_with_prxref(gen: Generation, inner: Any, *, rules_source: str, runs_dir: Path) -> ReviewOutcome:
    """Run prxref on ``gen`` with the repo config pinned to the rules SHA; persist the record."""
    url = f"https://github.com/{gen.repo}/pull/{gen.pr_number}"
    ref = ForgeImpl.parse_pr_url(url)
    run_dir = Path(runs_dir) / str(gen.id)
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    capture = CapturingForge(inner, gen)
    sha = rules_sha(gen.head_sha, gen.base_sha, rules_source)
    try:
        with tempfile.TemporaryDirectory(prefix="prxref-gate-rules-") as tmp:
            pinned = materialize_rules(lambda p: inner.get_file_content(ref, p, sha=sha), Path(tmp))
            result = cli._run_review(
                url, forge=capture, config_file=pinned.config_file, context_file="",
                evidence_files=[""], prompts_dir="", full_review=True,
            )
    finally:
        capture.seal()
    record = cli._build_json_result(result) if isinstance(result, dict) else {"verdict": None}
    (run_dir / "result.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return ReviewOutcome(result, capture.captured(), run_dir)


def _not_bound(gen: Generation, reason: str, *, store: Store, publisher: Publisher,
               client: Any, lease: Lease) -> str:
    if reason == "closed":
        publisher.cancel(gen, lease, "PR closed")
        store.finish(gen.id, lease.token, "cancelled", "cancelled", reason)
        return "cancelled"
    store.finish(gen.id, lease.token, "stale", None, reason)
    if reason in _DRIFT:
        live = read_live_pr(client, gen.repo, gen.pr_number)
        if live.state == "open" and not store.has_live_generation(
            gen.repo, gen.pr_number, live.head_sha, live.base_sha,
        ):
            store.enqueue(gen.repo, gen.pr_number, live.head_sha, live.base_sha, live.base_ref, "repair")
    return "stale"


def _publish_failed(gen: Generation, exc: Exception, *, store: Store, publisher: Publisher,
                    lease: Lease) -> str:
    why = f"publish failed: {type(exc).__name__}"
    logger.error("generation %s: %s", gen.id, why)
    try:
        publisher.fail(store.get(gen.id), lease, why)
    except LeaseLost:
        raise
    except Exception:  # noqa: BLE001
        logger.error("generation %s: the failure could not be published either", gen.id)
    store.finish(gen.id, lease.token, "failed", "failure", f"{why}: {exc}"[:500])
    return "failed"


def run_generation(gen: Generation, *, store: Store, publisher: Publisher, client: Any, lease: Lease,
                   review: Callable[[Generation], ReviewOutcome], deadline_s: float, effort: str) -> str:
    """Review and publish one queued generation; returns its final state."""
    gen = store.start(gen.id, lease.token)
    reason = check_binding(gen, read_live_pr(client, gen.repo, gen.pr_number), store)
    if reason is not None:
        return _not_bound(gen, reason, store=store, publisher=publisher, client=client, lease=lease)
    try:
        publisher.begin(gen, lease)
    except Stale as stale:
        return _not_bound(gen, stale.reason, store=store, publisher=publisher, client=client, lease=lease)
    except LeaseLost:
        raise
    except Exception as exc:  # noqa: BLE001
        return _publish_failed(gen, exc, store=store, publisher=publisher, lease=lease)
    gen = store.get(gen.id)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"gate-review-{gen.id}")
    try:
        outcome = pool.submit(review, gen).result(timeout=deadline_s)
    except FuturesTimeout:
        why = f"deadline: review exceeded {deadline_s:g}s"
        publisher.fail(gen, lease, why)
        store.finish(gen.id, lease.token, "failed", "failure", why)
        return "failed"
    except (LeaseLost, Stale):
        raise
    except Exception as exc:  # noqa: BLE001
        why = f"review crashed: {type(exc).__name__}"
        logger.error("generation %s: %s", gen.id, why)
        publisher.fail(gen, lease, why)
        store.finish(gen.id, lease.token, "failed", "failure", f"{why}: {exc}"[:500])
        return "failed"
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    try:
        conclusion = publisher.complete(gen, lease, outcome.result, outcome.captured,
                                        effort=effort, run_dir=str(outcome.run_dir))
    except Stale as stale:
        return _not_bound(gen, stale.reason, store=store, publisher=publisher, client=client, lease=lease)
    except LeaseLost:
        raise
    except Exception as exc:  # noqa: BLE001
        return _publish_failed(gen, exc, store=store, publisher=publisher, lease=lease)
    store.finish(gen.id, lease.token, "completed", conclusion)
    return "completed"


class Worker:
    """The single review loop: lease, adopt orphans, honor pause, dequeue."""

    def __init__(self, store: Store, publisher: Publisher, client: Any,
                 review: Callable[[Generation], ReviewOutcome], *, deadline_s: float, effort: str,
                 holder: str | None = None, poll_s: float = 2.0) -> None:
        self.store = store
        self.publisher = publisher
        self.client = client
        self.review = review
        self.deadline_s = deadline_s
        self.effort = effort
        self.holder = holder or f"{socket.gethostname()}:{os.getpid()}"
        self.poll_s = poll_s
        self.ttl_s = deadline_s + TTL_PAD_S

    def acquire(self) -> Lease | None:
        """Take the lease (breaking a dead local holder's) and re-queue its orphans."""
        token = self.store.acquire_lease(self.holder, self.ttl_s, holder_alive=holder_alive)
        if token is None:
            return None
        adopted = self.store.adopt_orphans(token)
        if adopted:
            logger.warning("re-queued in-progress generations of a previous worker: %s", adopted)
        return Lease(self.holder, token, self.ttl_s)

    def run_once(self, lease: Lease) -> str | None:
        """Run the next generation; ``None`` when paused or idle."""
        if not self.store.renew_lease(lease.holder, lease.token, lease.ttl_s):
            raise LeaseLost(f"fencing token {lease.token} is not the live worker lease")
        if self.store.is_paused():
            return None
        gen = self.store.next_queued()
        if gen is None:
            return None
        return run_generation(gen, store=self.store, publisher=self.publisher, client=self.client,
                              lease=lease, review=self.review, deadline_s=self.deadline_s,
                              effort=self.effort)

    def run(self, stop: threading.Event) -> None:
        """Loop until ``stop`` is set; a lost lease is re-acquired, never published through."""
        lease: Lease | None = None
        while not stop.is_set():
            if lease is None:
                lease = self.acquire()
                if lease is None:
                    stop.wait(self.poll_s)
                    continue
            try:
                state = self.run_once(lease)
            except LeaseLost:
                logger.error("worker lease lost; re-acquiring")
                lease = None
                continue
            except Exception:  # noqa: BLE001
                logger.exception("worker iteration failed")
                state = None
            if state is None:
                stop.wait(self.poll_s)
```

`run_once` renews before `is_paused`, so `test_a_lost_lease_raises_before_dequeueing` sees `LeaseLost` while paused or not. A publish error (a GitHub 4xx, or a 5xx the retry did not cure) finishes the generation `failed` and tries to close the check as `failure`; if that also fails, the check stays `in_progress`, which blocks the merge (fail closed) until a re-run. An exception escaping `run_generation` some other way leaves the row `in_progress`; a restarted worker's `adopt_orphans` re-queues it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_worker.py -q`
Expected: `17 passed`.
If `test_litellm_receives_the_configured_timeout_unscaled` sees a timeout other than 900.0, do not change the test: the gate's deadline contract (spec: `PRXREF_GATE_REVIEW_DEADLINE` wraps the review) depends on the per-call timeout being what the operator set. The path today is `src/prxref/llm_backends.py:941 (create_llm_client)` → `LiteLLMClient(default_timeout=timeout)` → `:585 (LiteLLMClient.invoke)` `request_timeout = self.default_timeout if timeout_s is None` → `"timeout": request_timeout`; prompt-size scaling (#72) applies only to the openai-compat client. Find which hop changed.
Then the full suite (`uv run pytest -q`, Bash `timeout: 300000`): 0 failed. Then `uv run ruff check src tests`.

- [ ] **Step 5: Commit**

```bash
cd ~/code/prxref/.claude/worktrees/sdlc-gate
git add src/prxref/gate/worker.py tests/gate/test_worker.py
git commit -m "feat(gate): worker with deadline, capture forge and base-pinned review (#102)" -m "Claude-Session-Id: <session UUID>"
```

---

### Task 10: `watchdog.py`: expiry repair and reconciliation

Every `PRXREF_GATE_WATCHDOG_SECONDS` re-queues `in_progress` generations whose lease lapsed; on start and every `PRXREF_GATE_RECONCILE_MINUTES`, lists open PRs on every allowlisted repository (one call per repo) and enqueues a `reconcile` generation for any PR whose live head and base have no live generation. This is the recovery for a push while the Mac slept or the tunnel was down (spec "watchdog.py").

**Files:**
- Create: `src/prxref/gate/watchdog.py`
- Test: `tests/gate/test_watchdog.py`

**Interfaces:**
- Consumes: `GitHubClient.request`, `GitHubError` (Task 7); `Store.requeue_expired`, `Store.has_live_generation`, `Store.enqueue` (Task 4).
- Produces (all in `prxref.gate.watchdog`):
  - `def reconcile(client, store: Store, repos: Sequence[str]) -> list[Generation]`
  - `class Watchdog(store: Store, client, repos: Sequence[str], *, watchdog_s: float, reconcile_s: float)` with `tick(now: float) -> dict` (`{"requeued": [...], "reconciled": [...] | None}`) and `run(stop: threading.Event, *, clock: Callable[[], float] = time.monotonic) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/gate/test_watchdog.py`:

```python
"""Watchdog: expiry repair and reconciliation (#102)."""

import pytest

from prxref.gate import watchdog
from prxref.gate.checks import GitHubError
from prxref.gate.store import Store

H1, H2, BASE = "1" * 40, "2" * 40, "b" * 40


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "gate.db")


def _pull(n, head):
    return {"number": n, "head": {"sha": head}, "base": {"sha": BASE, "ref": "main"}}


class _Client:
    def __init__(self, by_repo):
        self.by_repo = by_repo
        self.calls = []

    def request(self, method, repo, path, **kwargs):
        self.calls.append((method, repo, path, kwargs.get("params")))
        answer = self.by_repo[repo]
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_reconcile_enqueues_only_uncovered_heads(store):
    store.enqueue("acme/a", 1, H1, BASE, "main", "opened")
    client = _Client({"acme/a": [_pull(1, H1), _pull(2, H2)]})
    made = watchdog.reconcile(client, store, ["acme/a"])
    assert [(g.pr_number, g.head_sha, g.reason) for g in made] == [(2, H2, "reconcile")]
    assert client.calls == [("GET", "acme/a", "/pulls", {"state": "open", "per_page": 100})]


def test_reconcile_survives_one_failing_repo(store):
    client = _Client({"acme/a": GitHubError("GET /pulls: HTTP 404", status=404),
                      "acme/b": [_pull(3, H1)]})
    made = watchdog.reconcile(client, store, ["acme/a", "acme/b"])
    assert [(g.repo, g.pr_number) for g in made] == [("acme/b", 3)]


def test_tick_reconciles_at_start_then_on_the_interval(store):
    client = _Client({"acme/a": []})
    dog = watchdog.Watchdog(store, client, ["acme/a"], watchdog_s=60, reconcile_s=600)
    assert dog.tick(1000.0)["reconciled"] == []
    assert dog.tick(1060.0)["reconciled"] is None
    assert dog.tick(1600.0)["reconciled"] == []
    assert len(client.calls) == 2


def test_tick_requeues_expired_in_progress(store, monkeypatch):
    calls = []
    monkeypatch.setattr(store, "requeue_expired", lambda: calls.append(1) or [42])
    dog = watchdog.Watchdog(store, _Client({}), [], watchdog_s=60, reconcile_s=600)
    assert dog.tick(0.0)["requeued"] == [42]
    assert calls == [1]


def test_run_returns_when_stopped(store):
    import threading

    stop = threading.Event()
    stop.set()
    watchdog.Watchdog(store, _Client({}), [], watchdog_s=60, reconcile_s=600).run(stop)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_watchdog.py -q`
Expected: FAIL (`ImportError: cannot import name 'watchdog'`).

- [ ] **Step 3: Write `src/prxref/gate/watchdog.py`**

```python
"""Gate watchdog: expiry repair and reconciliation (#102).

GitHub never redelivers a webhook on its own, so a push made while the
Mac slept is found here: one ``GET /pulls?state=open`` per allowlisted
repository per reconcile interval, and a ``reconcile`` generation for any
open PR whose live head and base have no live generation. At most the
first 100 open PRs of a repository are seen.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from prxref.gate.checks import GitHubError
from prxref.gate.store import Generation, Store

logger = logging.getLogger(__name__)


def reconcile(client: Any, store: Store, repos: Sequence[str]) -> list[Generation]:
    """Enqueue every open PR whose live head and base no live generation covers."""
    made: list[Generation] = []
    for repo in repos:
        try:
            pulls = client.request("GET", repo, "/pulls", params={"state": "open", "per_page": 100}) or []
        except GitHubError as exc:
            logger.error("reconcile %s: %s", repo, exc)
            continue
        for pull in pulls:
            number, head, base = pull["number"], pull["head"]["sha"], pull["base"]["sha"]
            if not store.has_live_generation(repo, number, head, base):
                made.append(store.enqueue(repo, number, head, base, pull["base"]["ref"], "reconcile"))
    return made


class Watchdog:
    """Periodic expiry repair plus reconciliation on start and on its own interval."""

    def __init__(self, store: Store, client: Any, repos: Sequence[str], *, watchdog_s: float,
                 reconcile_s: float) -> None:
        self.store = store
        self.client = client
        self.repos = list(repos)
        self.watchdog_s = watchdog_s
        self.reconcile_s = reconcile_s
        self._last_reconcile: float | None = None

    def tick(self, now: float) -> dict:
        """One pass: re-queue expired work; reconcile if due."""
        requeued = self.store.requeue_expired()
        if requeued:
            logger.warning("watchdog re-queued expired generations: %s", requeued)
        reconciled = None
        if self._last_reconcile is None or now - self._last_reconcile >= self.reconcile_s:
            self._last_reconcile = now
            reconciled = reconcile(self.client, self.store, self.repos)
        return {"requeued": requeued, "reconciled": reconciled}

    def run(self, stop: threading.Event, *, clock: Callable[[], float] = time.monotonic) -> None:
        """Tick every ``watchdog_s`` until ``stop`` is set."""
        while not stop.is_set():
            try:
                self.tick(clock())
            except Exception:  # noqa: BLE001
                logger.exception("watchdog tick failed")
            stop.wait(self.watchdog_s)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_watchdog.py -q`
Expected: `5 passed`.
Then: `uv run ruff check src tests`

- [ ] **Step 5: Commit**

```bash
cd ~/code/prxref/.claude/worktrees/sdlc-gate
git add src/prxref/gate/watchdog.py tests/gate/test_watchdog.py
git commit -m "feat(gate): watchdog expiry repair and open-PR reconciliation (#102)" -m "Claude-Session-Id: <session UUID>"
```

---

### Task 11: `rulesets.py`: manifest and ruleset renderer, apply and diff

Renders the GitHub App manifest and the merge ruleset exactly as the spec lists them ("rulesets.py"), applies the ruleset through the operator's own `gh` login (the app never administers repositories), and diffs the live ruleset against the rendered one for the bypass audit.

**Files:**
- Create: `src/prxref/gate/rulesets.py`
- Create: `tests/gate/golden/manifest.json`, `tests/gate/golden/ruleset.json`
- Test: `tests/gate/test_rulesets.py`

**Interfaces:**
- Consumes: `CHECK_NAME` (Task 5); `TOKEN_PERMISSIONS` (Task 3).
- Produces (all in `prxref.gate.rulesets`):
  - `RULESET_NAME = "sdlc-review"`; `class RulesetError(RuntimeError)`
  - `def render_manifest(webhook_url: str, *, name: str = "sdlc") -> dict`
  - `def render_ruleset(integration_id: int) -> dict`
  - `def apply_ruleset(repo: str, integration_id: int, *, run: Callable = subprocess.run) -> int` (ruleset id; creates, or updates the one named `sdlc-review`)
  - `def diff_ruleset(repo: str, integration_id: int, *, run: Callable = subprocess.run) -> str` (`""` when the live ruleset matches; else a unified diff)

- [ ] **Step 1: Write the golden files and the failing tests**

`tests/gate/golden/manifest.json`:

```json
{
  "name": "sdlc",
  "url": "https://github.com/sblattj/prxref",
  "hook_attributes": {"url": "https://sdlc.example.dev/webhook", "active": true},
  "public": false,
  "default_permissions": {
    "checks": "write",
    "pull_requests": "write",
    "contents": "read",
    "metadata": "read"
  },
  "default_events": ["pull_request", "check_run", "check_suite"]
}
```

`tests/gate/golden/ruleset.json`:

```json
{
  "name": "sdlc-review",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
  "rules": [
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": true,
        "required_status_checks": [{"context": "sdlc-review", "integration_id": 123456}]
      }
    },
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": true,
        "require_last_push_approval": true,
        "require_code_owner_review": false,
        "required_review_thread_resolution": false
      }
    }
  ]
}
```

`tests/gate/test_rulesets.py`:

```python
"""App manifest and ruleset rendering, apply and diff (#102)."""

import copy
import json
import subprocess
from pathlib import Path

import pytest

from prxref.gate import rulesets

GOLDEN = Path(__file__).parent / "golden"
REPO = "sblattj/sdlc-sandbox"


def _golden(name):
    return json.loads((GOLDEN / name).read_text())


def test_manifest_matches_golden():
    assert rulesets.render_manifest("https://sdlc.example.dev/webhook") == _golden("manifest.json")


def test_ruleset_matches_golden():
    assert rulesets.render_ruleset(123456) == _golden("ruleset.json")


class _Gh:
    """A scripted ``subprocess.run`` for ``gh api``."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, args, input=None, capture_output=True, text=True, check=False):
        self.calls.append((args, input))
        rc, out = self.replies.pop(0)
        return subprocess.CompletedProcess(args, rc, stdout=out, stderr="" if rc == 0 else "HTTP 403: nope")


def test_apply_creates_when_absent():
    gh = _Gh([(0, "[]"), (0, json.dumps({"id": 77}))])
    assert rulesets.apply_ruleset(REPO, 123456, run=gh) == 77
    args, body = gh.calls[1]
    assert args == ["gh", "api", "--method", "POST", f"repos/{REPO}/rulesets", "--input", "-"]
    assert json.loads(body) == _golden("ruleset.json")


def test_apply_updates_the_existing_ruleset_by_name():
    gh = _Gh([(0, json.dumps([{"id": 5, "name": "other"}, {"id": 77, "name": "sdlc-review"}])),
              (0, json.dumps({"id": 77}))])
    assert rulesets.apply_ruleset(REPO, 123456, run=gh) == 77
    assert gh.calls[1][0][:5] == ["gh", "api", "--method", "PUT", f"repos/{REPO}/rulesets/77"]


def _live(**changes):
    live = copy.deepcopy(_golden("ruleset.json"))
    live.update({"id": 77, "source": REPO, "source_type": "Repository"})
    live["rules"][1]["parameters"]["allowed_merge_methods"] = ["merge", "squash", "rebase"]
    live.update(changes)
    return live


def test_diff_ignores_fields_github_adds():
    gh = _Gh([(0, json.dumps([{"id": 77, "name": "sdlc-review"}])), (0, json.dumps(_live()))])
    assert rulesets.diff_ruleset(REPO, 123456, run=gh) == ""


def test_diff_shows_an_added_bypass_actor():
    actor = {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
    gh = _Gh([(0, json.dumps([{"id": 77, "name": "sdlc-review"}])),
              (0, json.dumps(_live(bypass_actors=[actor])))])
    diff = rulesets.diff_ruleset(REPO, 123456, run=gh)
    assert "RepositoryRole" in diff and diff.startswith("--- rendered")


def test_diff_without_a_live_ruleset_says_so():
    gh = _Gh([(0, "[]")])
    with pytest.raises(rulesets.RulesetError, match="no ruleset named sdlc-review"):
        rulesets.diff_ruleset(REPO, 123456, run=gh)


def test_a_gh_failure_raises_with_its_stderr():
    gh = _Gh([(1, "")])
    with pytest.raises(rulesets.RulesetError, match="HTTP 403"):
        rulesets.apply_ruleset(REPO, 123456, run=gh)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_rulesets.py -q`
Expected: FAIL (`ImportError: cannot import name 'rulesets'`).

- [ ] **Step 3: Write `src/prxref/gate/rulesets.py`**

```python
"""GitHub App manifest and merge ruleset for the sdlc gate (#102).

The ruleset requires the ``sdlc-review`` check from one app
(``integration_id``), a branch up to date with its base, one approval
after the last push, and no bypass actors. It is applied with the
operator's own ``gh`` login: the app's token has no administration
permission and never edits rulesets.
"""

from __future__ import annotations

import difflib
import json
import subprocess
from collections.abc import Callable
from typing import Any

from prxref.gate.app_auth import TOKEN_PERMISSIONS
from prxref.gate.receiver import CHECK_NAME

RULESET_NAME = "sdlc-review"
EVENTS = ["pull_request", "check_run", "check_suite"]


class RulesetError(RuntimeError):
    """A ``gh api`` call failed, or the live ruleset is missing."""


def render_manifest(webhook_url: str, *, name: str = "sdlc") -> dict:
    """The App manifest for GitHub's manifest flow."""
    return {
        "name": name,
        "url": "https://github.com/sblattj/prxref",
        "hook_attributes": {"url": webhook_url, "active": True},
        "public": False,
        "default_permissions": dict(TOKEN_PERMISSIONS),
        "default_events": list(EVENTS),
    }


def render_ruleset(integration_id: int) -> dict:
    """The default-branch ruleset that makes the app's check and one fresh approval required."""
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": [{"context": CHECK_NAME, "integration_id": integration_id}],
            }},
            {"type": "pull_request", "parameters": {
                "required_approving_review_count": 1,
                "dismiss_stale_reviews_on_push": True,
                "require_last_push_approval": True,
                "require_code_owner_review": False,
                "required_review_thread_resolution": False,
            }},
        ],
    }


def _gh(run: Callable, method: str, path: str, body: dict | None = None) -> Any:
    args = ["gh", "api", "--method", method, path]
    if body is not None:
        args += ["--input", "-"]
    proc = run(args, input=json.dumps(body) if body is not None else None,
               capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RulesetError(f"gh api {method} {path} exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def _existing_id(run: Callable, repo: str) -> int | None:
    for ruleset in _gh(run, "GET", f"repos/{repo}/rulesets") or []:
        if ruleset.get("name") == RULESET_NAME:
            return ruleset["id"]
    return None


def apply_ruleset(repo: str, integration_id: int, *, run: Callable = subprocess.run) -> int:
    """Create the ruleset, or replace the one named ``sdlc-review``; returns its id."""
    body = render_ruleset(integration_id)
    existing = _existing_id(run, repo)
    if existing is None:
        return _gh(run, "POST", f"repos/{repo}/rulesets", body)["id"]
    return _gh(run, "PUT", f"repos/{repo}/rulesets/{existing}", body)["id"]


def _project(live: Any, shape: Any) -> Any:
    """``live`` cut down to the keys ``shape`` has, so fields GitHub adds are not a diff."""
    if isinstance(shape, dict) and isinstance(live, dict):
        return {k: _project(live[k], v) if k in live else None for k, v in shape.items()}
    if isinstance(shape, list) and isinstance(live, list) and shape and isinstance(shape[0], dict):
        return [_project(item, shape[min(i, len(shape) - 1)]) for i, item in enumerate(live)]
    return live


def diff_ruleset(repo: str, integration_id: int, *, run: Callable = subprocess.run) -> str:
    """A unified diff of the rendered ruleset against the live one; ``""`` when they match."""
    existing = _existing_id(run, repo)
    if existing is None:
        raise RulesetError(f"{repo} has no ruleset named {RULESET_NAME}")
    rendered = render_ruleset(integration_id)
    live = _project(_gh(run, "GET", f"repos/{repo}/rulesets/{existing}"), rendered)
    a = json.dumps(rendered, indent=2, sort_keys=True).splitlines()
    b = json.dumps(live, indent=2, sort_keys=True).splitlines()
    return "\n".join(difflib.unified_diff(a, b, "rendered", "live", lineterm=""))
```

`_project` keeps a live list whole when the rendered list is empty, so every bypass actor GitHub reports is a diff line, and a key missing from the live ruleset projects to `null`, also a diff line. `difflib.unified_diff` output starts with `--- rendered`, which the test pins.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_rulesets.py -q`
Expected: `8 passed`.
Then: `uv run ruff check src tests`

- [ ] **Step 5: Commit**

```bash
cd ~/code/prxref/.claude/worktrees/sdlc-gate
git add src/prxref/gate/rulesets.py tests/gate/golden/manifest.json tests/gate/golden/ruleset.json tests/gate/test_rulesets.py
git commit -m "feat(gate): app manifest and merge ruleset renderer with apply and diff (#102)" -m "Claude-Session-Id: <session UUID>"
```

---

### Task 12: `serve.py` and the `prxref gate` commands

Wires every component into one process (`prxref gate serve`): the receiver's HTTP server, the worker thread and the watchdog thread, all on one `Store`. Adds the operator commands `pause`, `resume`, `status`, `render-manifest`, `render-ruleset` and `apply-ruleset` (spec "cli.py additions"). The parser lives in `cli.py` as plain argparse; the implementation is imported lazily, because `prxref.gate.worker` imports `prxref.cli` and an eager import would be circular.

**Files:**
- Create: `src/prxref/gate/serve.py`
- Modify: `src/prxref/cli.py`: `_build_parser` (`:203`, add the `gate` subparser after `config`'s, before `return parser`); `main` (`:2512`, dispatch `gate` before the final `parser.print_help`; add `gate <action>` to its docstring)
- Test: `tests/gate/test_serve.py`

**Interfaces:**
- Consumes: `apply_env_file`, `settings_from_config`, `GateSettings` (Task 2); `AppAuth` (Task 3); `Store` (Task 4); `Receiver`, `serve_http` (Task 5); `GitHubClient`, `Publisher` (Task 7); `make_guard` (Task 8); `InstallationForge`, `review_with_prxref`, `Worker` (Task 9); `Watchdog` (Task 10); `render_manifest`, `render_ruleset`, `apply_ruleset`, `diff_ruleset`, `RulesetError` (Task 11); `require_jwt`, `GateDependencyError` (Task 1); `prxref.config.load_config`, `prxref.llm.ConfigError`.
- Produces (all in `prxref.gate.serve`):
  - `@dataclass class Gate` with `settings, store, auth, client, publisher, receiver, worker, watchdog`
  - `def make_review(auth, settings: GateSettings, rules_source: str) -> Callable[[Generation], ReviewOutcome]`
  - `def build(settings: GateSettings, cfg: Mapping[str, Any], *, session=None) -> Gate`
  - `def run_gate(gate: Gate, host: str, port: int, *, stop: threading.Event | None = None) -> int`
  - `def serve_main(env_file: Path, host: str, port: int) -> int`
  - `def status_text(store: Store) -> str`
  - `def run_command(args: argparse.Namespace) -> int` (exit 0 ok; 1 a ruleset diff or a `gh` failure; 2 a configuration error or the missing extra)

- [ ] **Step 1: Write the failing tests**

`tests/gate/test_serve.py`:

```python
"""prxref gate: wiring and operator commands (#102)."""

import json
import os
from pathlib import Path

import pytest

from prxref import cli
from prxref.gate import rulesets, serve, worker
from prxref.gate.store import Store


@pytest.fixture(autouse=True)
def _isolated_environ(monkeypatch):
    """apply_env_file writes os.environ; keep every test's writes inside the test."""
    monkeypatch.setattr(os, "environ", os.environ.copy())


def _env_file(tmp_path, lines, mode=0o600):
    path = tmp_path / "env"
    path.write_text("\n".join(lines) + "\n")
    path.chmod(mode)
    return path


def test_gate_without_an_action_prints_help_and_exits_2(capsys):
    assert cli.main(["gate"]) == 2
    assert "usage:" in capsys.readouterr().err.lower()


def test_serve_names_the_missing_variable_and_exits_2(tmp_path, capsys):
    env = _env_file(tmp_path, ["PRXREF_GATE_STATE_FILE=" + str(tmp_path / "s.db")])
    assert cli.main(["gate", "serve", "--env-file", str(env)]) == 2
    assert "PRXREF_GATE_APP_ID" in capsys.readouterr().err


def test_serve_refuses_an_env_file_others_can_read(tmp_path, capsys):
    env = _env_file(tmp_path, ["PRXREF_GATE_APP_ID=1"], mode=0o644)
    assert cli.main(["gate", "serve", "--env-file", str(env)]) == 2
    assert "0600" in capsys.readouterr().err


def test_serve_without_the_extra_exits_2(tmp_path, monkeypatch, capsys):
    from prxref import gate

    def missing():
        raise gate.GateDependencyError(gate.EXTRA_HINT)

    monkeypatch.setattr(serve, "require_jwt", missing)
    env = _env_file(tmp_path, ["PRXREF_GATE_APP_ID=1"])
    assert cli.main(["gate", "serve", "--env-file", str(env)]) == 2
    assert "github-app" in capsys.readouterr().err


@pytest.mark.parametrize("line, expected", [(None, "base"), ("PRXREF_RULES_SOURCE=head", "head")])
def test_serve_defaults_rules_source_to_base(tmp_path, monkeypatch, line, expected):
    seen = {}
    monkeypatch.setattr(serve, "settings_from_config", lambda cfg: seen.setdefault("cfg", cfg))
    monkeypatch.setattr(serve, "build", lambda settings, cfg: "gate")
    monkeypatch.setattr(serve, "run_gate", lambda gate, host, port: 0)
    env = _env_file(tmp_path, [x for x in ["PRXREF_GATE_APP_ID=1", line] if x])
    assert serve.serve_main(env, "127.0.0.1", 8787) == 0
    assert seen["cfg"]["rules_source"] == expected


def test_pause_resume_and_status(tmp_path, capsys):
    state = tmp_path / "gate.db"
    env = _env_file(tmp_path, [f"PRXREF_GATE_STATE_FILE={state}"])
    Store(state).enqueue("acme/widget", 7, "h" * 40, "b" * 40, "main", "opened")
    assert cli.main(["gate", "pause", "--env-file", str(env)]) == 0
    assert Store(state).is_paused()
    assert cli.main(["gate", "status", "--env-file", str(env)]) == 0
    out = capsys.readouterr().out
    assert "paused: yes" in out and "queue depth: 1" in out and "acme/widget#7" in out
    assert cli.main(["gate", "resume", "--env-file", str(env)]) == 0
    assert not Store(state).is_paused()


def test_status_without_a_state_file_exits_2(tmp_path, capsys):
    env = _env_file(tmp_path, ["PRXREF_GATE_APP_ID=1"])
    assert cli.main(["gate", "status", "--env-file", str(env)]) == 2
    assert "PRXREF_GATE_STATE_FILE" in capsys.readouterr().err


def test_render_commands_print_the_golden_json(capsys):
    golden = Path(__file__).parent / "golden"
    assert cli.main(["gate", "render-manifest", "--webhook-url", "https://sdlc.example.dev/webhook"]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads((golden / "manifest.json").read_text())
    assert cli.main(["gate", "render-ruleset", "--integration-id", "123456"]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads((golden / "ruleset.json").read_text())


@pytest.mark.parametrize("diff, rc", [("", 0), ("--- rendered\n+++ live\n", 1)])
def test_apply_ruleset_diff_exits_1_on_drift(monkeypatch, capsys, diff, rc):
    monkeypatch.setattr(rulesets, "diff_ruleset", lambda repo, iid: diff)
    assert cli.main(["gate", "apply-ruleset", "--repo", "o/r", "--integration-id", "5", "--diff"]) == rc
    assert ("--- rendered" in capsys.readouterr().out) is bool(diff)


def test_apply_ruleset_prints_the_id(monkeypatch, capsys):
    monkeypatch.setattr(rulesets, "apply_ruleset", lambda repo, iid: 77)
    assert cli.main(["gate", "apply-ruleset", "--repo", "o/r", "--integration-id", "5"]) == 0
    assert "77" in capsys.readouterr().out


def test_make_review_reads_through_an_installation_token(tmp_path, monkeypatch):
    seen = {}

    def fake_review(gen, inner, *, rules_source, runs_dir):
        seen.update(inner=inner, rules_source=rules_source, runs_dir=runs_dir)
        return "outcome"

    monkeypatch.setattr(worker, "review_with_prxref", fake_review)

    class _Auth:
        def token_for(self, repo):
            return f"tok-{repo}"

    settings = type("S", (), {"runs_dir": tmp_path / "runs"})()
    gen = Store(tmp_path / "s.db").enqueue("acme/widget", 7, "h" * 40, "b" * 40, "main", "opened")
    assert serve.make_review(_Auth(), settings, "base")(gen) == "outcome"
    assert isinstance(seen["inner"], worker.InstallationForge)
    assert seen["inner"]._headers("github.com")["Authorization"] == "Bearer tok-acme/widget"
    assert (seen["rules_source"], seen["runs_dir"]) == ("base", tmp_path / "runs")
```

`test_serve_names_the_missing_variable_and_exits_2` relies on Task 2's `settings_from_config` naming the first missing required variable; Task 2's `test_settings.py` pins `PRXREF_GATE_APP_ID` as the first one checked. Confirm with `awk '/^### Task 2:/,/^### Task 3:/' docs/design/2026-10-08-sdlc-review-gate-plan.md | grep -n 'PRXREF_GATE_APP_ID' | head -3`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_serve.py -q`
Expected: FAIL (`ImportError: cannot import name 'serve'`).

- [ ] **Step 3: Write `src/prxref/gate/serve.py`**

```python
"""``prxref gate``: one process running the receiver, the worker and the watchdog (#102).

Every component shares one :class:`~prxref.gate.store.Store`. The process
reads its credentials from a mode-0600 ``--env-file`` and logs the names it
loaded, never their values. ``PRXREF_RULES_SOURCE`` defaults to ``base`` here
and only here; ``prxref review`` and ``prxref serve`` keep ``head``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from prxref.config import load_config
from prxref.gate import GateDependencyError, require_jwt, rulesets
from prxref.gate.app_auth import AppAuth
from prxref.gate.binding import make_guard
from prxref.gate.checks import GitHubClient, Publisher
from prxref.gate.receiver import Receiver, serve_http
from prxref.gate.settings import GateSettings, apply_env_file, settings_from_config
from prxref.gate.store import Generation, Store
from prxref.gate.watchdog import Watchdog
from prxref.gate.worker import InstallationForge, ReviewOutcome, Worker
from prxref.gate import worker as worker_module
from prxref.llm import ConfigError

logger = logging.getLogger(__name__)


@dataclass
class Gate:
    """The wired components of one gate process."""

    settings: GateSettings
    store: Store
    auth: AppAuth
    client: GitHubClient
    publisher: Publisher
    receiver: Receiver
    worker: Worker
    watchdog: Watchdog


def make_review(auth: Any, settings: GateSettings, rules_source: str) -> Callable[[Generation], ReviewOutcome]:
    """The worker's review callable: prxref reading the PR through an installation token."""

    def review(gen: Generation) -> ReviewOutcome:
        inner = InstallationForge(lambda: auth.token_for(gen.repo))
        return worker_module.review_with_prxref(gen, inner, rules_source=rules_source,
                                                runs_dir=settings.runs_dir)

    return review


def build(settings: GateSettings, cfg: Mapping[str, Any], *, session: Any = None) -> Gate:
    """Construct every component from validated settings; nothing listens or runs yet."""
    session = session or requests.Session()
    pem = settings.private_key_file.read_text(encoding="utf-8")
    auth = AppAuth(settings.app_id, pem, settings.installation_ids, session=session)
    store = Store(settings.state_file)
    client = GitHubClient(auth, session)
    publisher = Publisher(client, store, app_id=settings.app_id, bot_login=auth.bot_login(),
                          guard=make_guard(client, store))
    receiver = Receiver(store, settings, started_at=time.time())
    worker = Worker(store, publisher, client, make_review(auth, settings, cfg["rules_source"]),
                    deadline_s=float(settings.review_deadline),
                    effort=cfg["llm_reasoning_effort"] or "default")
    watchdog = Watchdog(store, client, sorted(settings.repos), watchdog_s=float(settings.watchdog_seconds),
                        reconcile_s=float(settings.reconcile_minutes * 60))
    return Gate(settings, store, auth, client, publisher, receiver, worker, watchdog)


def run_gate(gate: Gate, host: str, port: int, *, stop: threading.Event | None = None) -> int:
    """Serve until SIGTERM/SIGINT or ``stop``; returns 0 after a clean shutdown."""
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
    server = serve_http(gate.receiver, host, port)
    threads = [
        threading.Thread(target=server.serve_forever, name="gate-http", daemon=True),
        threading.Thread(target=gate.worker.run, args=(stop,), name="gate-worker", daemon=True),
        threading.Thread(target=gate.watchdog.run, args=(stop,), name="gate-watchdog", daemon=True),
    ]
    for t in threads:
        t.start()
    logger.info("gate listening on %s:%d", host, server.server_address[1])
    stop.wait()
    server.shutdown()
    server.server_close()
    for t in threads[1:]:
        t.join(timeout=5)
    return 0


def serve_main(env_file: Path, host: str, port: int) -> int:
    """``prxref gate serve``: load the env file, validate, build and run."""
    names = apply_env_file(env_file)
    os.environ.setdefault("PRXREF_RULES_SOURCE", "base")
    require_jwt()
    cfg = load_config()
    settings = settings_from_config(cfg)
    logger.info("gate env file %s set: %s", env_file, ", ".join(sorted(names)))
    return run_gate(build(settings, cfg), host, port)


def _fmt_time(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "never"


def status_text(store: Store) -> str:
    """The operator view: pause, queue, lease, last publish, last auth failure, last 10 generations."""
    lease = store.lease_info()
    lines = [
        f"paused: {'yes' if store.is_paused() else 'no'}",
        f"queue depth: {store.queue_depth()}",
        f"lease: {lease['holder'] + ' until ' + _fmt_time(lease['expires_at']) if lease else 'none'}",
        f"last publish: {_fmt_time(store.last_publish_time())}",
        f"last auth failure: {store.last_auth_failure() or 'none'}",
        "recent generations:",
    ]
    for g in store.recent(10):
        lines.append(f"  #{g.id} {g.repo}#{g.pr_number} gen {g.gen} {g.head_sha[:12]} {g.state}"
                     f" {g.conclusion or '-'} {g.reason}")
    return "\n".join(lines)


def _state_store(env_file: Path) -> Store:
    apply_env_file(env_file)
    state = str(load_config()["gate_state_file"]).strip()
    if not state:
        raise ConfigError("PRXREF_GATE_STATE_FILE is required")
    return Store(Path(state).expanduser())


def run_command(args: argparse.Namespace) -> int:
    """Dispatch ``prxref gate <action>``; 2 on a configuration error or the missing extra."""
    try:
        action = args.gate_command
        if action == "serve":
            return serve_main(Path(args.env_file), args.host, args.port)
        if action in ("pause", "resume"):
            _state_store(Path(args.env_file)).set_paused(action == "pause")
            print(f"gate {'paused' if action == 'pause' else 'resumed'}")
            return 0
        if action == "status":
            print(status_text(_state_store(Path(args.env_file))))
            return 0
        if action == "render-manifest":
            print(json.dumps(rulesets.render_manifest(args.webhook_url), indent=2, sort_keys=True))
            return 0
        if action == "render-ruleset":
            print(json.dumps(rulesets.render_ruleset(args.integration_id), indent=2, sort_keys=True))
            return 0
        if action == "apply-ruleset":
            if args.diff:
                diff = rulesets.diff_ruleset(args.repo, args.integration_id)
                print(diff or "ruleset matches")
                return 1 if diff else 0
            print(f"ruleset {rulesets.RULESET_NAME} id {rulesets.apply_ruleset(args.repo, args.integration_id)}")
            return 0
    except (ConfigError, GateDependencyError) as e:
        print(f"prxref gate: {e}", file=sys.stderr)
        return 2
    except rulesets.RulesetError as e:
        print(f"prxref gate: {e}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled gate action {action!r}")
```

The `gate status` line format is pinned by `test_pause_resume_and_status`; `lease_info()` returns `{"holder", "token", "expires_at"}` (Task 4).

- [ ] **Step 4: Add the parser and dispatch in `src/prxref/cli.py`**

In `_build_parser`, immediately before its final `return parser`:

```python
    gate = sub.add_parser("gate", help="run or operate the sdlc review gate (GitHub App, #102)")
    gate_sub = gate.add_subparsers(dest="gate_command")
    g_serve = gate_sub.add_parser("serve", help="receiver + worker + watchdog in one process")
    g_serve.add_argument("--env-file", required=True, help="mode-0600 KEY=VALUE file with the gate settings")
    g_serve.add_argument("--host", default="127.0.0.1")
    g_serve.add_argument("--port", type=int, default=8787)
    for action, text in (("pause", "stop dequeuing (webhooks still queue)"),
                         ("resume", "resume dequeuing"),
                         ("status", "queue depth, lease, last publish, last auth failure, recent generations")):
        p = gate_sub.add_parser(action, help=text)
        p.add_argument("--env-file", required=True)
    g_manifest = gate_sub.add_parser("render-manifest", help="print the GitHub App manifest JSON")
    g_manifest.add_argument("--webhook-url", required=True)
    g_render = gate_sub.add_parser("render-ruleset", help="print the sdlc-review ruleset JSON")
    g_render.add_argument("--integration-id", type=int, required=True)
    g_apply = gate_sub.add_parser("apply-ruleset", help="create/replace the ruleset via gh api")
    g_apply.add_argument("--repo", required=True)
    g_apply.add_argument("--integration-id", type=int, required=True)
    g_apply.add_argument("--diff", action="store_true", help="print the drift and exit 1 if any; change nothing")
```

In `main`, immediately before the final `parser.print_help(sys.stderr)` / `return 2`:

```python
    if args.command == "gate" and args.gate_command:
        from prxref.gate.serve import run_command

        return run_command(args)
```

and extend `main`'s docstring list with ``gate <action>``. The import stays inside `main` because `prxref.gate.worker` imports `prxref.cli`.

- [ ] **Step 5: Run the tests**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_serve.py tests/test_cli.py -q`
Expected: `tests/gate/test_serve.py` 13 passed (11 functions, two of them parametrized ×2); `tests/test_cli.py` unchanged and green.

Run: `uv run ruff check src tests`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
cd ~/code/prxref/.claude/worktrees/sdlc-gate
git add src/prxref/gate/serve.py src/prxref/cli.py tests/gate/test_serve.py
git commit -m "feat(gate): prxref gate serve/pause/resume/status and ruleset commands (#102)" -m "Claude-Session-Id: <session UUID>"
```

---

### Task 13: Runbook, README section, CHANGELOG

The spec's "Delivery → Docs" line asks for `docs/gate.md` (runbook) and a `README.md` section; the `docs/env-vars.md` rows and the `docs/config-file.md` classification already landed in Task 2. The runbook carries every operator procedure the spec names: prerequisites, launchd, rotation (three credentials), local limits, rollback order, and the "key expired" entry. A small test keeps the runbook in step with the parser and the config keys.

**Files:**
- Create: `docs/gate.md`
- Modify: `README.md` (new `## Review Gate (GitHub App)` section between `## Webhook Server` and `## Review Against a Spec or Ticket`, `:278`)
- Modify: `CHANGELOG.md` (`## [Unreleased]`, `:11`)
- Test: `tests/gate/test_docs.py`

**Interfaces:**
- Consumes: `cli._build_parser()` with the `gate` subparser (Task 12); `config._DEFAULTS` with the `gate_*` keys (Task 2).
- Produces: nothing code-facing. Task 16 moves the `[Unreleased]` entry under `## [0.35.0]`.

- [ ] **Step 1: Write the failing test**

`tests/gate/test_docs.py`:

```python
"""The gate runbook stays in step with the CLI and the config keys (#102)."""

import argparse
from pathlib import Path

from prxref import cli, config

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs" / "gate.md"


def _gate_actions() -> list[str]:
    parser = cli._build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    gate = sub.choices["gate"]
    gsub = next(a for a in gate._actions if isinstance(a, argparse._SubParsersAction))
    return sorted(gsub.choices)


def test_every_gate_action_is_in_the_runbook():
    text = RUNBOOK.read_text(encoding="utf-8")
    missing = [a for a in _gate_actions() if f"prxref gate {a}" not in text]
    assert missing == []


def test_every_gate_key_is_in_the_runbook():
    text = RUNBOOK.read_text(encoding="utf-8")
    names = ["PRXREF_" + k.upper() for k in config._DEFAULTS if k.startswith("gate_")]
    assert len(names) == 12
    assert [n for n in names if n not in text] == []


def test_the_runbook_has_the_operator_sections():
    text = RUNBOOK.read_text(encoding="utf-8")
    for heading in ("## Prerequisites", "## Install", "## Rotation", "## Local limits",
                    "## Troubleshooting", "## Rollback"):
        assert heading in text, heading
    assert "key expired" in text.lower()


def test_rollback_deletes_the_ruleset_before_anything_else():
    rollback = RUNBOOK.read_text(encoding="utf-8").split("## Rollback", 1)[1]
    assert rollback.index("rulesets/") < rollback.index("launchctl bootout") < rollback.index("installations")


def test_readme_and_changelog_point_at_the_gate():
    assert "docs/gate.md" in (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert "https://github.com/sblattj/prxref/issues/102" in unreleased
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_docs.py -q`
Expected: FAIL (`FileNotFoundError: ... docs/gate.md`).

- [ ] **Step 3: Write `docs/gate.md`**

````markdown
# sdlc review gate (GitHub App)

The gate turns a prxref review into a **required, merge-blocking check**. A GitHub
App named `sdlc` owns a check run named `sdlc-review`; a repository ruleset requires
that check *from that app* (by `integration_id`), so a status or a check with the
same name from anyone else does not satisfy it. The advisory lane (`prxref serve`,
`prxref review`, `action.yml`) is unchanged and never blocks.

One process (`prxref gate serve`) runs three threads on one SQLite file:

- the **receiver** (`POST /webhook`, `GET /health`): verifies the signature, checks
  the installation and repository allowlists, de-duplicates deliveries and queues a
  *generation* (one review of one head against one base);
- the **worker**: one review at a time under a fenced lease, with a hard deadline;
  it reads `.prxref.toml`, rule packs and standards from the PR's **base** commit
  (`PRXREF_RULES_SOURCE=base`), so a PR cannot weaken the rules that judge it;
- the **watchdog**: re-queues work a dead worker left behind and reconciles open
  PRs against the queue every `PRXREF_GATE_RECONCILE_MINUTES`.

Before every write the worker re-reads the PR. If the head, the base branch or the
base commit moved, or the PR closed, the result is dropped and a fresh generation
reviews what is live.

## Verdicts

| Review outcome | Check conclusion |
|---|---|
| Approved | `success` |
| Request-Changes | `failure` |
| Incomplete (a chunk, the sweep or a model failed) | `failure` |
| Error, deadline hit, crash | `failure` |
| Superseded or stale (the PR moved) | nothing published; a newer generation owns the head |
| PR closed | `cancelled`, only if a check was already created |

Every outcome posts one review with event `COMMENT` (never `APPROVE` or
`REQUEST_CHANGES`) plus the summary comment. A review that cannot finish fails
closed: the check concludes `failure`, never `neutral` or `skipped`.

## Prerequisites

1. **Install the extra:** `uv tool install 'prxref[github-app,litellm]==0.35.0'`. Without
   `github-app`, `prxref gate serve` exits 2 naming the extra.
2. **Tunnel:** a Cloudflare zone, `cloudflared tunnel login`, `cloudflared tunnel create sdlc`,
   and `cloudflared tunnel route dns sdlc <webhook-host>`. The webhook URL is
   `https://<webhook-host>/webhook`. The named tunnel needs an ingress rule that sends
   that hostname to the gate; write `~/.cloudflared/config.yml` (`<tunnel-uuid>` is the
   id `cloudflared tunnel create` printed, and its credentials file sits beside it):

   ```yaml
   tunnel: sdlc
   credentials-file: /Users/<you>/.cloudflared/<tunnel-uuid>.json
   ingress:
     - hostname: <webhook-host>
       service: http://127.0.0.1:8787
     - service: http_status:404
   ```

   Check it with `cloudflared tunnel ingress validate` and
   `cloudflared tunnel ingress rule https://<webhook-host>/webhook` (prints the
   `http://127.0.0.1:8787` rule).
3. **Model key:** a *long-term* Amazon Bedrock API key (an IAM user with
   `AmazonBedrockLimitedAccess`, created in the Bedrock console). A 12-hour short-term key
   expires under the daemon and every review after that fails closed.
4. **Register the app:** `prxref gate render-manifest --webhook-url https://<webhook-host>/webhook > manifest.json`,
   then create the app from that manifest (GitHub → Settings → Developer settings →
   GitHub Apps → New, or the manifest flow). Install it on the target repositories
   only. Download its private key to `~/.config/prxref-gate/sdlc.pem` and run
   `chmod 600` on it. Note the app id and the installation id.
5. **Apply the ruleset:** `prxref gate render-ruleset --integration-id <app-id>` shows it;
   `prxref gate apply-ruleset --repo <owner/name> --integration-id <app-id>` creates or
   replaces the ruleset named `sdlc-review` through `gh api` (your `gh` login needs admin on
   the repository). `prxref gate apply-ruleset --repo <owner/name> --integration-id <app-id> --diff`
   prints any drift and exits 1 when there is some.

## Install

Copy `examples/launchd/prxref-gate.env.example` to `~/.config/prxref-gate/env`, fill it
in, and `chmod 600` it; the gate refuses an env file anyone else can read. It holds:

| Variable | Meaning |
|---|---|
| `PRXREF_GATE_APP_ID` | the app's numeric id (also the ruleset's `integration_id`) |
| `PRXREF_GATE_PRIVATE_KEY_FILE` | path to the app's `.pem`, mode 0600 |
| `PRXREF_GATE_INSTALLATION_IDS` | comma-separated installation ids the gate accepts |
| `PRXREF_GATE_REPOS` | comma-separated `owner/name` repositories the gate accepts |
| `PRXREF_GATE_WEBHOOK_SECRET` | the app's webhook secret |
| `PRXREF_GATE_WEBHOOK_SECRET_PREVIOUS` | the previous secret, during a rotation only |
| `PRXREF_GATE_SECRET_GRACE_MINUTES` | how long after start the previous secret is honored (30) |
| `PRXREF_GATE_STATE_FILE` | the SQLite state file |
| `PRXREF_GATE_RUNS_DIR` | where each review's run record is written (mode 0700) |
| `PRXREF_GATE_REVIEW_DEADLINE` | seconds a review may take before it fails closed (900) |
| `PRXREF_GATE_WATCHDOG_SECONDS` | watchdog interval (60) |
| `PRXREF_GATE_RECONCILE_MINUTES` | open-PR reconciliation interval (10) |

plus the model settings (`PRXREF_LLM_BACKEND=litellm`, `PRXREF_LLM_MODELS`,
`PRXREF_LLM_TIMEOUT=900`, `AWS_BEARER_TOKEN_BEDROCK`, `AWS_REGION_NAME`).
`PRXREF_RULES_SOURCE` defaults to `base` under `prxref gate serve`; leave it unset.

Copy `examples/launchd/dev.prxref.gate.plist` and `examples/launchd/dev.prxref.cloudflared.plist`
to `~/Library/LaunchAgents/`, replace `__HOME__` with your home directory, then:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.prxref.gate.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.prxref.cloudflared.plist
curl -s http://127.0.0.1:8787/health
```

Operate it with `prxref gate status --env-file ~/.config/prxref-gate/env` (pause state,
queue depth, lease, last publish, last authentication failure, the last ten
generations), `prxref gate pause --env-file …` (webhooks still queue; nothing is
dequeued) and `prxref gate resume --env-file …`. A paused gate leaves PRs blocked.

## Rotation

- **Webhook secret:** generate a new secret; in the env file set
  `PRXREF_GATE_WEBHOOK_SECRET_PREVIOUS` to the old value and `PRXREF_GATE_WEBHOOK_SECRET`
  to the new one; restart (`launchctl kickstart -k gui/$(id -u)/dev.prxref.gate`); change
  the secret in the app settings. Deliveries signed with the old secret are accepted for
  `PRXREF_GATE_SECRET_GRACE_MINUTES` after the restart. Then remove the previous line and
  restart again.
- **App private key:** generate a second key in the app settings, replace the `.pem`
  (keep mode 0600), restart, confirm `/health`, then delete the old key in the settings.
- **Bedrock key:** replace the `AWS_BEARER_TOKEN_BEDROCK` line and restart.

## Local limits

The gate runs on one Mac. Sleep, shutdown and tunnel loss pause intake. GitHub
retries a failed delivery for a bounded time, and the app's "Recent Deliveries" page
has a **Redeliver** button for the rest. While the gate is away, the required check
is missing, so merges stay blocked. When the gate returns, the watchdog reconciles
every open PR and queues a review for any head without one.

A verdict already published describes the head against the base it saw. If the base
branch moves afterwards, the ruleset's strict policy ("require branches to be up to
date") refuses the merge until the PR is updated, and that update is a new push, so a
new generation reviews the new head against the new base. A base retarget
(`pull_request.edited`) queues a new generation at once; if that delivery is lost, the
next reconcile (`PRXREF_GATE_RECONCILE_MINUTES`) or push repairs it.

## Troubleshooting

- **Key expired.** Every review fails with an authentication error in the check summary,
  and `prxref gate status` shows it under "last auth failure". Replace the Bedrock key
  (see Rotation).
- **401 on every delivery.** The webhook secret in the env file and the app differ.
- **403 on a delivery.** The installation id or the repository is not in
  `PRXREF_GATE_INSTALLATION_IDS` / `PRXREF_GATE_REPOS`.
- **Check stuck `in_progress`.** The worker died mid-review; the watchdog re-queues it
  after the lease expires (review deadline + 120 s). Re-run from the PR's Checks tab
  ("Re-run") to queue it immediately.

## Rollback

Order matters: remove the ruleset first, or every merge stays blocked.

1. `gh api -X DELETE repos/<owner>/<name>/rulesets/<id>` (find the id with
   `gh api repos/<owner>/<name>/rulesets`).
2. `launchctl bootout gui/$(id -u)/dev.prxref.gate` and
   `launchctl bootout gui/$(id -u)/dev.prxref.cloudflared`.
3. Uninstall the app from the repository: GitHub → Settings → Applications →
   Installed GitHub Apps → `sdlc` → Configure, or
   `gh api -X DELETE /app/installations/<installation-id>` with an app JWT.
````

- [ ] **Step 4: README section and CHANGELOG entry**

`README.md`, immediately before `## Review Against a Spec or Ticket`:

````markdown
## Review Gate (GitHub App)

`prxref review` and `prxref serve` advise; they never block a merge. To make a prxref
review a **required check**, run the gate: a GitHub App (`sdlc`) that publishes a check
run named `sdlc-review`, required by a repository ruleset bound to that app, with the
review rules read from the PR's base commit.

```bash
pip install 'prxref[github-app,litellm]'
prxref gate serve --env-file ~/.config/prxref-gate/env
```

Setup, launchd services, rotation and rollback: [docs/gate.md](docs/gate.md).

````

`CHANGELOG.md`, under `## [Unreleased]`:

```markdown
### Added

- sdlc review gate ([#102](https://github.com/sblattj/prxref/issues/102)): `prxref gate
  serve` runs a GitHub App webhook receiver, a single worker and a watchdog on one
  SQLite file, and publishes a `sdlc-review` check run (Approved → `success`; every
  other outcome, including a deadline hit or a crash, → `failure`) plus a `COMMENT`
  review. Rules, rule packs and standards are read from the PR's base commit
  (`PRXREF_RULES_SOURCE=base`, the gate's default). Every publish re-checks that the
  PR's head and base have not moved. `prxref gate pause|resume|status`,
  `render-manifest`, `render-ruleset` and `apply-ruleset [--diff]` operate it; twelve
  `PRXREF_GATE_*` variables configure it; the new `github-app` extra brings PyJWT.
  Runbook: `docs/gate.md`.
```

- [ ] **Step 5: Run the tests**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_docs.py tests/test_docs_consistency.py -q`
Expected: `tests/gate/test_docs.py` 5 passed; `tests/test_docs_consistency.py` unchanged and green.

- [ ] **Step 6: Commit**

```bash
cd ~/code/prxref/.claude/worktrees/sdlc-gate
git add docs/gate.md README.md CHANGELOG.md tests/gate/test_docs.py
git commit -m "docs(gate): runbook, README section and changelog entry (#102)" -m "Claude-Session-Id: <session UUID>"
```

---

### Task 14: launchd services and the env template

The spec's "Operations" names two `KeepAlive` launchd services: `dev.prxref.gate` (the gate) and `dev.prxref.cloudflared` (`cloudflared tunnel run sdlc`). They ship as examples with a `__HOME__` placeholder, because launchd does not expand `~` or `$HOME`. Neither plist carries an `EnvironmentVariables` block: every credential stays in the mode-0600 env file the gate reads itself, so `launchctl print` never shows a secret.

**Files:**
- Create: `examples/launchd/dev.prxref.gate.plist`
- Create: `examples/launchd/dev.prxref.cloudflared.plist`
- Create: `examples/launchd/prxref-gate.env.example`
- Test: `tests/gate/test_launchd.py`

**Interfaces:**
- Consumes: `load_env_file` and `settings_from_config` (Task 2); the `gate serve` flags `--env-file`, `--host`, `--port` (Task 12).
- Produces: the files `docs/gate.md` (Task 13) tells the operator to copy.

- [ ] **Step 1: Write the failing test**

`tests/gate/test_launchd.py`:

```python
"""The launchd examples parse, keep running, and carry no credentials (#102)."""

import plistlib
import shutil
from pathlib import Path

import pytest

from prxref import config
from prxref.gate.settings import load_env_file

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "launchd"


def _plist(name):
    with (EXAMPLES / name).open("rb") as fh:
        return plistlib.load(fh)


@pytest.mark.parametrize("name, label, argv_tail", [
    ("dev.prxref.gate.plist", "dev.prxref.gate",
     ["gate", "serve", "--env-file", "__HOME__/.config/prxref-gate/env", "--host", "127.0.0.1",
      "--port", "8787"]),
    ("dev.prxref.cloudflared.plist", "dev.prxref.cloudflared", ["tunnel", "run", "sdlc"]),
])
def test_the_plist_is_a_keepalive_service(name, label, argv_tail):
    p = _plist(name)
    assert p["Label"] == label
    assert p["KeepAlive"] is True and p["RunAtLoad"] is True
    assert p["ProgramArguments"][1:] == argv_tail
    assert p["ProgramArguments"][0].startswith("/")
    assert p["StandardErrorPath"].startswith("__HOME__/Library/Logs/")


@pytest.mark.parametrize("name", ["dev.prxref.gate.plist", "dev.prxref.cloudflared.plist"])
def test_the_plist_carries_no_environment(name):
    assert "EnvironmentVariables" not in _plist(name)


def test_the_env_template_names_every_gate_key_and_holds_no_secret(tmp_path):
    copy = tmp_path / "env"
    shutil.copy(EXAMPLES / "prxref-gate.env.example", copy)
    copy.chmod(0o600)
    pairs = load_env_file(copy)
    gate_names = {"PRXREF_" + k.upper() for k in config._DEFAULTS if k.startswith("gate_")}
    assert gate_names <= set(pairs)
    for secret in ("PRXREF_GATE_WEBHOOK_SECRET", "PRXREF_GATE_WEBHOOK_SECRET_PREVIOUS",
                   "AWS_BEARER_TOKEN_BEDROCK"):
        assert pairs[secret] == "", secret
    assert pairs["PRXREF_LLM_BACKEND"] == "litellm"
    assert pairs["PRXREF_LLM_TIMEOUT"] == "900"
    assert pairs["PRXREF_GATE_REVIEW_DEADLINE"] == "900"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_launchd.py -q`
Expected: FAIL (`FileNotFoundError: ... examples/launchd/dev.prxref.gate.plist`).

- [ ] **Step 3: Write the three files**

`examples/launchd/dev.prxref.gate.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>dev.prxref.gate</string>
	<key>ProgramArguments</key>
	<array>
		<string>__HOME__/.local/bin/prxref</string>
		<string>gate</string>
		<string>serve</string>
		<string>--env-file</string>
		<string>__HOME__/.config/prxref-gate/env</string>
		<string>--host</string>
		<string>127.0.0.1</string>
		<string>--port</string>
		<string>8787</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>ThrottleInterval</key>
	<integer>30</integer>
	<key>StandardOutPath</key>
	<string>__HOME__/Library/Logs/prxref-gate.log</string>
	<key>StandardErrorPath</key>
	<string>__HOME__/Library/Logs/prxref-gate.log</string>
</dict>
</plist>
```

`examples/launchd/dev.prxref.cloudflared.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>dev.prxref.cloudflared</string>
	<key>ProgramArguments</key>
	<array>
		<string>/opt/homebrew/bin/cloudflared</string>
		<string>tunnel</string>
		<string>run</string>
		<string>sdlc</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>ThrottleInterval</key>
	<integer>30</integer>
	<key>StandardOutPath</key>
	<string>__HOME__/Library/Logs/prxref-cloudflared.log</string>
	<key>StandardErrorPath</key>
	<string>__HOME__/Library/Logs/prxref-cloudflared.log</string>
</dict>
</plist>
```

`examples/launchd/prxref-gate.env.example`:

```bash
# prxref gate settings. Copy to ~/.config/prxref-gate/env and chmod 600 it;
# `prxref gate serve` refuses a file anyone else can read. Runbook: docs/gate.md.

# GitHub App
PRXREF_GATE_APP_ID=
PRXREF_GATE_PRIVATE_KEY_FILE=~/.config/prxref-gate/sdlc.pem
PRXREF_GATE_INSTALLATION_IDS=
PRXREF_GATE_REPOS=sblattj/sdlc-sandbox
PRXREF_GATE_WEBHOOK_SECRET=
PRXREF_GATE_WEBHOOK_SECRET_PREVIOUS=
PRXREF_GATE_SECRET_GRACE_MINUTES=30

# State
PRXREF_GATE_STATE_FILE=~/.local/state/prxref-gate/gate.db
PRXREF_GATE_RUNS_DIR=~/.local/state/prxref-gate/runs
PRXREF_GATE_REVIEW_DEADLINE=900
PRXREF_GATE_WATCHDOG_SECONDS=60
PRXREF_GATE_RECONCILE_MINUTES=10

# Model: one model, no fallback (Amazon Bedrock through litellm)
PRXREF_LLM_BACKEND=litellm
PRXREF_LLM_MODELS=bedrock/us.anthropic.claude-opus-4-6-v1
PRXREF_LLM_REASONING_EFFORT=medium
PRXREF_LLM_TEMPERATURE=1
PRXREF_LLM_SEED=off
PRXREF_LLM_TIMEOUT=900
PRXREF_LLM_MAX_TOKENS=32000
AWS_REGION_NAME=us-west-2
# A long-term Bedrock API key; a 12-hour short-term key expires under the daemon.
AWS_BEARER_TOKEN_BEDROCK=
```

- [ ] **Step 4: Run the tests**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_launchd.py -q`
Expected: `5 passed`.

Run: `plutil -lint examples/launchd/dev.prxref.gate.plist examples/launchd/dev.prxref.cloudflared.plist`
Expected: both `OK`.

- [ ] **Step 5: Commit**

```bash
cd ~/code/prxref/.claude/worktrees/sdlc-gate
git add examples/launchd/dev.prxref.gate.plist examples/launchd/dev.prxref.cloudflared.plist examples/launchd/prxref-gate.env.example tests/gate/test_launchd.py
git commit -m "feat(gate): launchd services and the gate env template (#102)" -m "Claude-Session-Id: <session UUID>"
```

---

### Task 15: Live proof on `sblattj/sdlc-sandbox`

The spec's "Live proof (sandbox)" section: four phases against a real GitHub repository, a real app and the real gate, each capture a `gh api` JSON file in `docs/live-instance-verification/sdlc-gate/` naming the commit SHAs, ruleset id, app id and check run id. Unit tests prove the parts; only this proves that GitHub's ruleset accepts the app's check and refuses the foreign status of the same name, which is the whole point of the gate.

**This task needs the operator.** Before Step 3, stop and ask the user to confirm each item of the spec's "Operator prerequisites"; do not attempt any of them yourself:

1. `cloudflared tunnel login` done, `cloudflared tunnel create sdlc` done, a DNS route to the webhook hostname, and `~/.cloudflared/config.yml` with the ingress rule from `docs/gate.md` "Prerequisites" step 2 (checks: `cloudflared tunnel list` lists `sdlc`; `cloudflared tunnel ingress validate` prints `OK`; `cloudflared tunnel ingress rule https://<webhook-host>/webhook` names `http://127.0.0.1:8787`).
2. A long-term Bedrock API key in `~/.config/prxref-gate/env` as `AWS_BEARER_TOKEN_BEDROCK` (check without printing it: `grep -c '^AWS_BEARER_TOKEN_BEDROCK=.' ~/.config/prxref-gate/env` prints `1`).
3. A machine-user GitHub account with write access to `sblattj/sdlc-sandbox`, logged into `gh` (check: `gh auth status` lists it; the steps below call it `<machine-user>`).
4. The `sdlc` app registered from `prxref gate render-manifest` output, installed on `sblattj/sdlc-sandbox` only, its key at `~/.config/prxref-gate/sdlc.pem` mode 0600, and `PRXREF_GATE_APP_ID` / `PRXREF_GATE_INSTALLATION_IDS` / `PRXREF_GATE_WEBHOOK_SECRET` filled in the env file.

Creating the public repository `sblattj/sdlc-sandbox` is in scope for the executor (Step 3) because it is public and owned by `sblattj`; per the identity policy, no other account touches it. Every `gh` call below on the sandbox runs as `sblattj` (`GH_TOKEN=$(gh auth token --user sblattj)`), except the phase 1 foreign status, which runs as `<machine-user>`. No token value is ever written to a file, an argument or a log.

**Files:**
- Create: `docs/live-instance-verification/sdlc-gate/capture.py` (writes one capture and its index entry)
- Create: `docs/live-instance-verification/sdlc-gate/faults.py` (phase 4: signed deliveries to the live receiver; the gate with an injected 502)
- Create: `docs/live-instance-verification/sdlc-gate/index.json` and the capture files
- Create: `docs/live-instance-verification/sdlc-gate/README.md` (what each phase proved, with the SHAs and ids)
- Test: `tests/gate/test_live_captures.py`

**Interfaces:**
- Consumes: `prxref.gate.settings.load_env_file`, `apply_env_file`, `settings_from_config` (Task 2); `prxref.gate.serve.build`, `run_gate` (Task 12); `prxref.config.load_config`.
- Produces: `index.json`: a list of `{"phase": "1"|"2"|"3"|"4", "name": str, "file": str, "repo": str, "pr": int, "head_sha": str, "base_sha": str, "ruleset_id": int, "app_id": int, "check_run_id": int | None, "gate_version": str}`.

- [ ] **Step 1: Write the failing test**

`tests/gate/test_live_captures.py`:

```python
"""The sandbox live proof is complete and says what it proved (#102, spec "Live proof")."""

import json
import re
from pathlib import Path

import pytest

DIR = Path(__file__).resolve().parents[2] / "docs" / "live-instance-verification" / "sdlc-gate"
REQUIRED = {
    "1": {"ruleset", "foreign-status", "pr-blocked"},
    "2": {"check-run", "pr-clean"},
    "3": {"approval-dismissed", "check-run-new-head", "pr-blocked-new-head"},
    "4": {"bad-signature", "incomplete", "deadline", "duplicate", "rerequest", "stale-head",
          "close-reopen", "publish-5xx", "worker-restart", "secret-rotation"},
}
SHA = re.compile(r"^[0-9a-f]{40}$")
SECRET_SHAPES = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY|ABSK[A-Za-z0-9+/=]{20,}")


@pytest.fixture(scope="module")
def index():
    return json.loads((DIR / "index.json").read_text(encoding="utf-8"))


def _entry(index, phase, name):
    return next(e for e in index if (e["phase"], e["name"]) == (phase, name))


def _capture(index, phase, name):
    return json.loads((DIR / _entry(index, phase, name)["file"]).read_text(encoding="utf-8"))


def test_every_required_capture_is_present(index):
    have = {}
    for e in index:
        have.setdefault(e["phase"], set()).add(e["name"])
    assert {p: names - have.get(p, set()) for p, names in REQUIRED.items()} == {p: set() for p in REQUIRED}


def test_every_entry_names_its_shas_and_ids(index):
    for e in index:
        assert (DIR / e["file"]).is_file(), e["file"]
        assert SHA.match(e["head_sha"]) and SHA.match(e["base_sha"]), e
        assert isinstance(e["ruleset_id"], int) and isinstance(e["app_id"], int), e
        assert e["check_run_id"] is None or isinstance(e["check_run_id"], int), e


def test_phases_one_and_two_hold_repo_pr_base_and_head_constant(index):
    keys = {(e["repo"], e["pr"], e["base_sha"], e["head_sha"], e["ruleset_id"])
            for e in index if e["phase"] in ("1", "2")}
    assert len(keys) == 1


def test_the_foreign_status_blocks_and_the_app_check_clears(index):
    ruleset = _capture(index, "1", "ruleset")
    required = [r for r in ruleset["rules"] if r["type"] == "required_status_checks"][0]
    integration = required["parameters"]["required_status_checks"][0]["integration_id"]
    assert _capture(index, "1", "foreign-status")["context"] == "sdlc-review"
    assert _capture(index, "1", "pr-blocked")["mergeable_state"] == "blocked"
    check = _capture(index, "2", "check-run")
    assert (check["name"], check["app"]["id"], check["conclusion"]) == ("sdlc-review", integration, "success")
    assert _capture(index, "2", "pr-clean")["mergeable_state"] == "clean"


def test_a_new_push_dismisses_the_approval_and_blocks_again(index):
    reviews = _capture(index, "3", "approval-dismissed")
    assert any(r["state"] == "DISMISSED" for r in reviews)
    assert _capture(index, "3", "pr-blocked-new-head")["mergeable_state"] == "blocked"
    assert _entry(index, "3", "check-run-new-head")["head_sha"] != _entry(index, "2", "check-run")["head_sha"]


def test_no_capture_carries_a_credential():
    for path in sorted(DIR.glob("*.json")):
        assert not SECRET_SHAPES.search(path.read_text(encoding="utf-8")), path.name
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_live_captures.py -q`
Expected: FAIL (`FileNotFoundError: ... index.json`); `test_no_capture_carries_a_credential` passes vacuously.

- [ ] **Step 3: Write `capture.py` and `faults.py`**

`docs/live-instance-verification/sdlc-gate/capture.py`:

```python
"""Write one live-proof capture: ``gh api`` output plus its index entry (#102).

Usage: uv run python docs/live-instance-verification/sdlc-gate/capture.py PHASE NAME
       --repo R --pr N --head SHA --base SHA --ruleset-id I --app-id I [--check-run-id I]
       [--json FILE | -- GH_API_ARGS...]
``gh`` runs with the caller's environment (set ``GH_TOKEN`` there, never here).
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent


def main(argv: list[str]) -> int:
    split = argv.index("--") if "--" in argv else len(argv)
    p = argparse.ArgumentParser()
    p.add_argument("phase", choices=["1", "2", "3", "4"])
    p.add_argument("name")
    for flag in ("--repo", "--head", "--base"):
        p.add_argument(flag, required=True)
    for flag in ("--pr", "--ruleset-id", "--app-id"):
        p.add_argument(flag, type=int, required=True)
    p.add_argument("--check-run-id", type=int)
    p.add_argument("--json", help="a JSON file already captured (phase 4 harness output)")
    p.add_argument("--gate-version", default=subprocess.run(
        ["prxref", "--version"], capture_output=True, text=True).stdout.strip())
    args = p.parse_args(argv[:split])
    if args.json:
        payload = json.loads(Path(args.json).read_text(encoding="utf-8"))
    else:
        out = subprocess.run(["gh", "api", *argv[split + 1:]], capture_output=True, text=True, check=True)
        payload = json.loads(out.stdout)
    name = f"{args.phase}-{args.name}.json"
    (DIR / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    index_path = DIR / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    index = [e for e in index if (e["phase"], e["name"]) != (args.phase, args.name)]
    index.append({"phase": args.phase, "name": args.name, "file": name, "repo": args.repo, "pr": args.pr,
                  "head_sha": args.head, "base_sha": args.base, "ruleset_id": args.ruleset_id,
                  "app_id": args.app_id, "check_run_id": args.check_run_id, "gate_version": args.gate_version})
    index.sort(key=lambda e: (e["phase"], e["name"]))
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(f"captured {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

`docs/live-instance-verification/sdlc-gate/faults.py`:

```python
"""Phase 4 fault injection against the live gate (#102). Prints no secret.

  send   --env-file F --kind {bad-signature,duplicate,rerequest} --repo R --pr N --head SHA --base SHA
         --installation I --app-id I --out FILE
  serve-502 --env-file F   run the gate in-process; the first POST /check-runs gets a 502
                           after GitHub accepted it (the reply is lost, the write is not)
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import uuid
from pathlib import Path

import requests

from prxref.gate.settings import WEBHOOK_SECRET_ENV, apply_env_file, load_env_file

URL = "http://127.0.0.1:8787/webhook"


def _post(event: str, payload: dict, secret: str, delivery: str) -> dict:
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    r = requests.post(URL, data=body, timeout=10, headers={
        "X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": sig,
        "Content-Type": "application/json"})
    return {"event": event, "delivery": delivery, "status": r.status_code, "reply": r.json()}


def send(a: argparse.Namespace) -> dict:
    secret = load_env_file(Path(a.env_file))[WEBHOOK_SECRET_ENV]
    pr = {"action": "synchronize", "number": a.pr, "repository": {"full_name": a.repo},
          "installation": {"id": a.installation},
          "pull_request": {"number": a.pr, "head": {"sha": a.head}, "base": {"sha": a.base, "ref": "main"}}}
    if a.kind == "bad-signature":
        return {"cases": [_post("pull_request", pr, secret + "-wrong", str(uuid.uuid4()))]}
    if a.kind == "duplicate":
        d = str(uuid.uuid4())
        return {"cases": [_post("pull_request", pr, secret, d), _post("pull_request", pr, secret, d)]}
    run = {"action": "rerequested", "repository": {"full_name": a.repo}, "installation": {"id": a.installation},
           "check_run": {"name": "sdlc-review", "head_sha": a.head, "app": {"id": a.app_id},
                         "pull_requests": [{"number": a.pr}]}}
    return {"cases": [_post("check_run", run, secret, str(uuid.uuid4()))]}


class _LoseFirstCheckReply(requests.Session):
    lost = False

    def request(self, method, url, *args, **kwargs):
        resp = super().request(method, url, *args, **kwargs)
        if method == "POST" and url.endswith("/check-runs") and not self.lost:
            type(self).lost = True
            fake = requests.Response()
            fake.status_code, fake._content, fake.url = 502, b'{"message": "injected"}', url
            print(f"injected 502 after GitHub answered {resp.status_code}", file=sys.stderr)
            return fake
        return resp


def serve_502(a: argparse.Namespace) -> int:
    from prxref.config import load_config
    from prxref.gate import serve
    from prxref.gate.settings import settings_from_config

    apply_env_file(Path(a.env_file))
    os.environ.setdefault("PRXREF_RULES_SOURCE", "base")
    cfg = load_config()
    return serve.run_gate(serve.build(settings_from_config(cfg), cfg, session=_LoseFirstCheckReply()),
                          "127.0.0.1", 8787)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["send", "serve-502"])
    p.add_argument("--env-file", required=True)
    p.add_argument("--kind", choices=["bad-signature", "duplicate", "rerequest"])
    p.add_argument("--repo")
    p.add_argument("--pr", type=int)
    p.add_argument("--head")
    p.add_argument("--base")
    p.add_argument("--installation", type=int)
    p.add_argument("--app-id", type=int)
    p.add_argument("--out")
    a = p.parse_args(argv)
    if a.command == "serve-502":
        return serve_502(a)
    Path(a.out).write_text(json.dumps(send(a), indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Set up the sandbox and start the gate from this branch**

The live proof runs the branch's code (0.35.0 does not exist yet; Task 16 re-runs phase 2 on the published release). In the commands below `SB` is `sblattj/sdlc-sandbox`, `CAP` is `uv run python docs/live-instance-verification/sdlc-gate/capture.py`, and `IDS` is the shared flag set `--repo $SB --pr $PR --head $HEAD --base $BASE --ruleset-id $RID --app-id $APP`; re-derive `HEAD`/`BASE` with `gh pr view $PR -R $SB --json headRefOid,baseRefOid` after every push.

```bash
cd ~/code/prxref/.claude/worktrees/sdlc-gate
export GH_TOKEN=$(gh auth token --user sblattj)
gh repo create sblattj/sdlc-sandbox --public --add-readme -d "prxref sdlc gate live proof (#102)"
uv tool install --force --from . 'prxref[github-app,litellm]'
prxref --version                                     # the branch version
cp examples/launchd/dev.prxref.*.plist ~/Library/LaunchAgents/
sed -i '' "s#__HOME__#$HOME#g" ~/Library/LaunchAgents/dev.prxref.gate.plist ~/Library/LaunchAgents/dev.prxref.cloudflared.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.prxref.cloudflared.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.prxref.gate.plist
curl -s http://127.0.0.1:8787/health                # {"ok": true, ...}
curl -s https://<webhook-host>/health                # the same body through the tunnel
prxref gate apply-ruleset --repo sblattj/sdlc-sandbox --integration-id $APP
RID=$(gh api repos/sblattj/sdlc-sandbox/rulesets --jq '.[] | select(.name=="sdlc-review") | .id')
prxref gate pause --env-file ~/.config/prxref-gate/env   # phase 1 needs no app check yet
```

Then, as `sblattj`, create branch `proof` with one small change (a two-line `hello.py`), open PR `$PR` against `main`, and have `<machine-user>` approve it: `GH_TOKEN=$(gh auth token --user <machine-user>) gh pr review $PR -R $SB --approve`. The ruleset's `require_last_push_approval` means the approver must not be the last pusher, so `sblattj` does every push in phases 1 to 3.

- [ ] **Step 5: Phase 1: a foreign status of the same name does not clear the gate**

```bash
$CAP 1 ruleset $IDS -- repos/$SB/rulesets/$RID
GH_TOKEN=$(gh auth token --user <machine-user>) $CAP 1 foreign-status $IDS -- \
  --method POST repos/$SB/statuses/$HEAD -f state=success -f context=sdlc-review -f description="foreign status"
sleep 10; $CAP 1 pr-blocked $IDS -- repos/$SB/pulls/$PR
```

Expected: `1-pr-blocked.json` has `"mergeable_state": "blocked"` with the approval and the foreign `success` status both present. If it says `clean`, the ruleset is not bound to the app: stop and report, because the gate proves nothing.

- [ ] **Step 6: Phase 2: the app's check clears the gate on the same head**

```bash
prxref gate resume --env-file ~/.config/prxref-gate/env   # the `opened` generation queued while paused now runs
prxref gate status --env-file ~/.config/prxref-gate/env     # wait for the generation to finish
CRID=$(gh api repos/$SB/commits/$HEAD/check-runs --jq '.check_runs[] | select(.name=="sdlc-review") | .id')
$CAP 2 check-run $IDS --check-run-id $CRID -- repos/$SB/check-runs/$CRID
sleep 10; $CAP 2 pr-clean $IDS --check-run-id $CRID -- repos/$SB/pulls/$PR
```

Expected: `2-check-run.json` has `"conclusion": "success"` and `app.id == $APP`, and `2-pr-clean.json` has `"mergeable_state": "clean"`. `hello.py` must be clean enough for an Approved verdict. If the review says Request-Changes, fix `hello.py` in a new push; that changes the head, so restart phase 1 on the new head, keeping phases 1 and 2 on one head.

- [ ] **Step 7: Phase 3: a new push dismisses the approval and re-gates**

```bash
# as sblattj: push one more commit to `proof`; re-derive HEAD
sleep 10; $CAP 3 pr-blocked-new-head $IDS -- repos/$SB/pulls/$PR
$CAP 3 approval-dismissed $IDS -- repos/$SB/pulls/$PR/reviews
# wait for the new generation; then
CRID=$(gh api repos/$SB/commits/$HEAD/check-runs --jq '.check_runs[] | select(.name=="sdlc-review") | .id')
$CAP 3 check-run-new-head $IDS --check-run-id $CRID -- repos/$SB/check-runs/$CRID
```

Expected: the earlier approval is `DISMISSED`, the PR is `blocked`, and a new `sdlc-review` check run exists on the new head.

- [ ] **Step 8: Phase 4: failure cases**

Run `mkdir -m 700 -p /tmp/prxref-proof` first. Write each case's evidence to `/tmp/prxref-proof/<case>.json`, then `$CAP 4 <case> $IDS [--check-run-id N] --json /tmp/prxref-proof/<case>.json`. For a case whose evidence is GitHub state, capture with `-- <gh api path>` instead.

| Case | How | Pass when |
|---|---|---|
| `bad-signature` | `faults.py send --kind bad-signature` | `status` 401; `prxref gate status` shows no new generation |
| `duplicate` | `faults.py send --kind duplicate` | first 202, second 200 `{"status": "duplicate"}`; one generation |
| `rerequest` | `faults.py send --kind rerequest` (and the Checks tab's "Re-run") | 202; a new generation with the same head; capture the new check run |
| `incomplete` | set `PRXREF_LLM_TIMEOUT=5` in the env file, `launchctl kickstart -k gui/$(id -u)/dev.prxref.gate`, push | check `conclusion: failure`; the summary names the verdict (Incomplete expected; if every chunk timed out it is Error, which is still `failure`: record which one in the README's Findings); restore 900 |
| `deadline` | set `PRXREF_GATE_REVIEW_DEADLINE=20`, restart, push | `conclusion: failure`, summary `deadline: review exceeded 20s`; restore 900 |
| `stale-head` | push, then push again within the first review | the first generation `stale` in `prxref gate status`; its check never concludes `success`; the second head gets the verdict |
| `close-reopen` | `gh pr close $PR`, then `gh pr reopen $PR` | the queued generation `cancelled`; a new one after reopen |
| `publish-5xx` | `launchctl bootout gui/$(id -u)/dev.prxref.gate`; `uv run python docs/live-instance-verification/sdlc-gate/faults.py serve-502 --env-file ~/.config/prxref-gate/env`; push | stderr shows `injected 502`; exactly one `sdlc-review` check run on the head (`repos/$SB/commits/$HEAD/check-runs`), concluded; then bootstrap the plist again |
| `worker-restart` | push; while `status` shows `in_progress`, `launchctl kickstart -k gui/$(id -u)/dev.prxref.gate` | the log shows `re-queued in-progress generations`; the same check run id concludes |
| `secret-rotation` | follow `docs/gate.md` "Rotation" with `PRXREF_GATE_SECRET_GRACE_MINUTES=2`; send `faults.py send --kind duplicate` signed by the old secret (keep a copy of the old env file as `/tmp/prxref-proof/old.env`, mode 0600) inside and after the window | 202 inside the window, 401 after it |

Restore every env value changed above and restart the gate before Step 9.

- [ ] **Step 9: Write the README and run the tests**

`docs/live-instance-verification/sdlc-gate/README.md`: one section per phase stating what was proven, with the repo, PR number, head and base SHAs, ruleset id, app id, each check run id and the gate version, each linked to its capture file, plus a "Findings" section listing anything that behaved differently from the spec.

Run: `cd ~/code/prxref/.claude/worktrees/sdlc-gate && uv run pytest tests/gate/test_live_captures.py -q`
Expected: `6 passed`.

Run: `uv run ruff check src tests docs/live-instance-verification/sdlc-gate`
Expected: `All checks passed!`

- [ ] **Step 10: Commit**

```bash
cd ~/code/prxref/.claude/worktrees/sdlc-gate
git add docs/live-instance-verification/sdlc-gate tests/gate/test_live_captures.py
git status --short docs/live-instance-verification/sdlc-gate   # only .py, .json and README.md; no env copy
git commit -m "test(gate): sandbox live proof, phases 1-4 (#102)" -m "Claude-Session-Id: <session UUID>"
```

---

### Task 16: Release 0.35.0, the pinned re-run, and the issue record

Spec "Delivery": release 0.35.0 after merge, the changelog links #102, the sandbox pins 0.35.0, and issue #102 gets a comment recording the model situation. The version is a set of files, and `tests/test_cli.py:93` (`installed_version("prxref") == __version__`) fails a release whose `pyproject.toml` and `__init__.py` disagree.

**Precondition:** the draft PR from `sdlc-review-gate` has been reviewed and merged to `main` by the user. Do not merge it yourself, and apply no ruleset to `sblattj/prxref` (spec: "The prxref repository gets the gate only after the sandbox proof", and that is a separate user decision). Steps 1 to 4 run in a fresh worktree of `main` after the merge, e.g. `git -C ~/code/prxref worktree add ~/code/prxref/.claude/worktrees/release-0.35.0 -b release-0.35.0 origin/main`.

**Files:**
- Modify: `pyproject.toml:3`, `src/prxref/__init__.py:3`, `uv.lock` (the `prxref` package entry, `:1156`), `README.md` (`:25`, `:34`, `:163`, `:164`), `action.yml:51`, `examples/github-actions-inline.yml:38`, `CHANGELOG.md` (`:11` heading, `:3265`/`:3266` link refs)

**Interfaces:**
- Consumes: the `[Unreleased]` entry from Task 13; the live-proof tooling from Task 15.
- Produces: tag `v0.35.0`, GitHub Release `v0.35.0`, PyPI `prxref==0.35.0`, captures `2-check-run-0.35.0.json` and `2-pr-clean-0.35.0.json`.

- [ ] **Step 1: Bump every version file**

```bash
cd ~/code/prxref/.claude/worktrees/release-0.35.0
grep -rln '0\.34\.0' pyproject.toml src/prxref/__init__.py README.md action.yml examples/github-actions-inline.yml
sed -i '' 's/^version = "0.34.0"/version = "0.35.0"/' pyproject.toml
sed -i '' 's/__version__ = "0.34.0"/__version__ = "0.35.0"/' src/prxref/__init__.py
sed -i '' 's/0\.34\.0/0.35.0/g' README.md action.yml examples/github-actions-inline.yml
uv lock
grep -rn '0\.34\.0' pyproject.toml src/prxref/__init__.py README.md action.yml examples/github-actions-inline.yml uv.lock \
  && echo "STALE VERSION LEFT" || echo "VERSIONS: OK"
```

In `CHANGELOG.md`: change `## [Unreleased]` to `## [Unreleased]\n\n## [0.35.0] — <release date>` so the Task 13 entry sits under 0.35.0, and replace the two link refs with:

```markdown
[Unreleased]: https://github.com/sblattj/prxref/compare/v0.35.0...HEAD
[0.35.0]: https://github.com/sblattj/prxref/releases/tag/v0.35.0
[0.34.0]: https://github.com/sblattj/prxref/releases/tag/v0.34.0
```

`tests/gate/test_docs.py::test_readme_and_changelog_point_at_the_gate` reads `[Unreleased]`; change its split to `"## [0.35.0]"` in the same commit.

- [ ] **Step 2: Run the full suite**

Run: `cd ~/code/prxref/.claude/worktrees/release-0.35.0 && uv run pytest -q`
Expected: every test passes; the count is the baseline 11680 plus the gate tests (re-derive: `uv run pytest --collect-only -q tests/gate | tail -1`). `test_cli.py::test_version_matches_packaging_metadata` passes only after `uv sync` has re-installed 0.35.0.

Run: `uv run ruff check src tests`
Expected: `All checks passed!`

- [ ] **Step 3: Commit, then cut the release with the `cut-release` skill**

```bash
git add pyproject.toml src/prxref/__init__.py uv.lock README.md action.yml examples/github-actions-inline.yml CHANGELOG.md tests/gate/test_docs.py
git commit -m "release: 0.35.0" -m "Claude-Session-Id: <session UUID>"
```

Steps 1 and 2 pre-stage the version bump; the `cut-release` skill owns the release and re-verifies the version set itself, so where it disagrees with Steps 1 and 2, the skill wins. Invoke the `cut-release` skill and follow it for the push, the annotated tag `v0.35.0` on the gated SHA, the GitHub Release (notes from the changelog entry, with "Fixes #102"), the PyPI publish, and the tag↔release set-diff. Do not hand-roll these steps.

- [ ] **Step 4: Re-run phase 2 pinned to 0.35.0**

```bash
uv tool install --force 'prxref[github-app,litellm]==0.35.0'
prxref --version                          # 0.35.0
launchctl kickstart -k gui/$(id -u)/dev.prxref.gate
```

As `sblattj`, push one commit to the sandbox's `proof` branch, have `<machine-user>` approve, wait for the check, then capture with `$CAP 2 check-run-0.35.0 … --check-run-id $CRID -- repos/$SB/check-runs/$CRID` and `$CAP 2 pr-clean-0.35.0 … -- repos/$SB/pulls/$PR` (each index entry records `gate_version: 0.35.0`). These two captures sit on a later head than phases 1 and 2, so `test_phases_one_and_two_hold_repo_pr_base_and_head_constant` must exclude names ending `-0.35.0`: change its filter to `if e["phase"] in ("1", "2") and not e["name"].endswith("-0.35.0")`. Commit the captures, the index and the test change on a branch and open a PR (`docs: phase 2 re-run on 0.35.0 (#102)`).

- [ ] **Step 5: Record the model situation on issue #102**

Write the comment body to `/tmp/prxref-plan/issue-102-comment.md` (heredoc in its own Bash call), then `gh issue comment 102 -R sblattj/prxref -F /tmp/prxref-plan/issue-102-comment.md`. The comment states: the spec named Opus 5.5 behind an AWS Sales access gate; the gate ships with `bedrock/us.anthropic.claude-opus-4-6-v1`; swapping is a one-variable change (`PRXREF_LLM_MODELS`) followed by a re-run of the live proof's phase 2; the sandbox is pinned to 0.35.0; links to the release and to `docs/live-instance-verification/sdlc-gate/README.md`. End it with the `session-signature` skill's PR-comment form.

---

## Spec questions

Places where the plan departs from the spec's text, or decides something the spec leaves open. Each says which choice the plan made and why; the executor follows the plan, and the reviewer of the plan rules on each.

1. **Capturing forge, not `post=False`.** The spec's worker calls `orchestrate_review(..., post=False)`. With `post=False` the orchestrator never renders comment or summary bodies, so the spec's "comment bodies come from prxref's own `formatter` and `markers`" cannot hold. The plan runs `_run_review` with `post` on, over a `CapturingForge` (Task 9) that records the two post calls and writes nothing; every GitHub write stays in the publisher.
2. **`--env-file`, not `--config`.** The spec's launchd line is `prxref gate serve --config ~/.config/prxref-gate/prxref.toml`, but its "Credentials" section says every gate variable is env-only and loaded from `~/.config/prxref-gate/env` (mode 0600). A `.prxref.toml` cannot carry env-only keys (`config-file.md`), so the plan uses `--env-file` (Tasks 2, 12, 14).
3. **`PRXREF_GATE_REPOS` is new.** The spec's receiver rejects a repository "outside the allowlist" but defines only an installation-id allowlist. The plan adds a required repository allowlist (Task 2); the watchdog also needs the list to reconcile.
4. **The `paused` flag is a `leases` row** (`name='paused'`, `token` 1/0), not a new table, so the spec's four-table schema stays as written (Task 4).
5. **`publications` keeps `generation_id` as its primary key,** so it records one row per generation (the review). The check run id lives on `generations.check_run_id`; the summary comment is found by marker and author each time.
6. **The secret grace window is measured from process start,** not from when the secret changed (the gate cannot observe that). The runbook's rotation procedure restarts the gate, which starts the window.
7. **`PRXREF_GATE_REVIEW_DEADLINE` is in seconds (default 900),** where the spec says "default 15 min". Seconds match `PRXREF_LLM_TIMEOUT` and the lease TTL arithmetic.
8. **A missing `github-app` extra fails at `prxref gate serve`** (exit 2 naming the extra), not at `import prxref.gate`. The spec says importing `prxref.gate` without the extra raises; that would break `prxref gate render-manifest`, `pause`, `status` and the CLI's own `--help`, none of which need PyJWT.
9. **`prompts_dir` is forced off in the gate.** It is a local directory path; reading it from either SHA would let a PR or the host change the prompts the gate judges with. Base-pinned prompts are a follow-up.
10. **A review thread can outlive its deadline.** Python cannot kill a thread; on a deadline hit the worker seals the capture (so the stray thread can publish nothing) and marks the generation failed. The thread finishes its LLM calls in the background.
11. **The reconciler covers at most 100 open PRs per repository** (one `GET /pulls?per_page=100` call per repo per interval, as the spec's "one list call per repo" bounds it).
12. **`PRXREF_GATE_STATE_FILE` and `PRXREF_GATE_RUNS_DIR` are required, with no default,** so the gate never writes state to a surprising place.
13. **Out-of-order and base-moved repair happens in the worker.** When the binding guard finds the live head or base differs from the generation and no live generation covers the PR, the worker enqueues a `repair` generation for what is live, rather than waiting up to one reconcile interval.
14. **`.prxref.toml` is materialized from the base commit.** The spec says the rules are read "from the repo", but `load_config` reads `.prxref.toml` only from a local path. The plan fetches it and every in-repo path it names (`config._FILE_PATH_KEYS`, which includes `learnings_file`) at the base SHA into a temporary directory (Task 6).
15. **The binding guard runs before every `begin` and `complete` write; `fail` and `cancel` are unguarded,** because a failure or cancellation must be publishable even when the PR moved (fail closed).
16. **A stale generation leaves its check as is** (spec), and a publish error finishes the generation `failed`, possibly leaving its check `in_progress`. An `in_progress` required check still blocks merge, so this fails closed; the watchdog or the next push resolves it.
17. **`get_diff` reads the live PR diff, not a compare diff of the generation's SHAs.** The compare endpoint truncates large diffs differently from the PR diff; the binding guard guarantees the live PR still matches the generation at publish time.
18. **The gate always runs a full review.** `CapturingForge.get_summary` returns `None` and pruning is a no-op, so incremental re-review and inline-comment pruning are off in the gate.
19. **The summary comment is updated only when it carries the summary marker and was authored by the app's bot login.** `github.py:549 (_find_summary)` matches the marker regardless of author; a PATCH of the advisory lane's comment would get a 403 and fail the publish.
20. **An Error verdict's error class is not in the result dict** (`orchestrator` sets no `error` key). The check summary says "review error: the error class is in the summary comment and the run record" instead of the spec's "summary carries the error class".
21. **`Store.adopt_orphans` is new.** When a fresh worker breaks a dead holder's lease (`holder_alive`), the lease is live again immediately, so the spec's "re-queue `in_progress` whose lease expired" would never fire. The new worker re-queues every `in_progress` row under its own fencing token (Task 4).
22. **`apply-ruleset --diff` ignores fields GitHub adds** (ids, timestamps, `_links`, `source`) by projecting the live ruleset onto the rendered shape (Task 11).
23. **The live proof runs on the branch's build; 0.35.0 gets a phase 2 re-run.** The spec pins the sandbox to 0.35.0, but the release follows the merge, which follows the proof. Task 15 proves the branch; Task 16 re-runs phase 2 on the published 0.35.0.
24. **The induced "incomplete" case uses `PRXREF_LLM_TIMEOUT=5`.** If every chunk times out the verdict is Error rather than Incomplete; both conclude `failure`, and the live-proof README records which one occurred.
25. **The 5xx case is injected in-process.** GitHub cannot be made to return a 5xx, so `faults.py serve-502` runs the gate with a session that discards GitHub's real reply to the first `POST /check-runs` and returns 502, exercising the find-by-`external_id` path against the real API.
26. **The post-publish base-move window is bounded by the ruleset, not the watchdog.** The spec says the runbook bounds the retarget window "to one watchdog interval", but the watchdog re-queues expired work and the reconciler compares heads only; neither notices a moved base after a check concluded. What closes the window is the ruleset's strict policy (a moved base forces an update push, which starts a new generation) plus the receiver's `edited` handling for retargets. The runbook says that instead.
