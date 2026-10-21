"""The gate's resolved settings and its mode-0600 environment file (#102)."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prxref.gate import EXTRA_HINT, GateDependencyError, require_jwt
from prxref.llm import ConfigError

WEBHOOK_SECRET_ENV = "PRXREF_GATE_WEBHOOK_SECRET"
WEBHOOK_SECRET_PREVIOUS_ENV = "PRXREF_GATE_WEBHOOK_SECRET_PREVIOUS"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_DIGITS_RE = re.compile(r"[0-9]+")
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
        raise ConfigError(f"{name}: {path} must be mode 0600 (it is {stat.S_IMODE(mode):04o}); run chmod 600 {path}")


def _require_pem(path: Path, name: str) -> None:
    """Parse the key with the backend PyJWT signs with, so a bad file fails at startup (exit 2)."""
    require_jwt()
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        load_pem_private_key(path.read_bytes(), password=None)
    except ImportError as e:
        raise GateDependencyError(f"prxref gate needs PyJWT with RS256 support; {EXTRA_HINT}") from e
    except (OSError, ValueError, TypeError) as e:
        raise ConfigError(f"{name}: {path} is not a usable PEM private key ({type(e).__name__})") from e


def _app_id(cfg: Mapping[str, Any]) -> int:
    app_id = str(cfg["gate_app_id"]).strip()
    if not _DIGITS_RE.fullmatch(app_id):
        raise ConfigError("PRXREF_GATE_APP_ID must be the app's numeric id")
    return int(app_id)


def _installation_ids(cfg: Mapping[str, Any]) -> frozenset[int]:
    raw_ids = list(cfg["gate_installation_ids"])
    if not raw_ids or not all(_DIGITS_RE.fullmatch(v.strip()) for v in raw_ids):
        raise ConfigError("PRXREF_GATE_INSTALLATION_IDS must list one or more numeric installation ids")
    return frozenset(int(v.strip()) for v in raw_ids)


def _repos(cfg: Mapping[str, Any]) -> frozenset[str]:
    repos = [v.strip() for v in cfg["gate_repos"]]
    if not repos or not all(_REPO_RE.match(r) for r in repos):
        raise ConfigError("PRXREF_GATE_REPOS must list one or more owner/name repositories")
    return frozenset(r.lower() for r in repos)


def settings_from_config(cfg: Mapping[str, Any]) -> GateSettings:
    """Validate the gate keys of a loaded ``cfg``; raise ``ConfigError`` naming the variable."""
    app_id = _app_id(cfg)
    key_file = str(cfg["gate_private_key_file"]).strip()
    if not key_file:
        raise ConfigError("PRXREF_GATE_PRIVATE_KEY_FILE is required")
    key_path = Path(key_file).expanduser()
    _require_private(key_path, "PRXREF_GATE_PRIVATE_KEY_FILE")
    _require_pem(key_path, "PRXREF_GATE_PRIVATE_KEY_FILE")
    installation_ids = _installation_ids(cfg)
    repos = _repos(cfg)
    if not str(cfg["gate_webhook_secret"]).strip():
        raise ConfigError("PRXREF_GATE_WEBHOOK_SECRET is required")
    for key, name in (("gate_state_file", "PRXREF_GATE_STATE_FILE"), ("gate_runs_dir", "PRXREF_GATE_RUNS_DIR")):
        if not str(cfg[key]).strip():
            raise ConfigError(f"{name} is required")
    return GateSettings(
        app_id=app_id,
        private_key_file=key_path,
        installation_ids=installation_ids,
        repos=repos,
        secret_grace_minutes=int(cfg["gate_secret_grace_minutes"]),
        state_file=Path(str(cfg["gate_state_file"])).expanduser(),
        runs_dir=Path(str(cfg["gate_runs_dir"])).expanduser(),
        review_deadline=int(cfg["gate_review_deadline"]),
        watchdog_seconds=int(cfg["gate_watchdog_seconds"]),
        reconcile_minutes=int(cfg["gate_reconcile_minutes"]),
    )


def lambda_settings(cfg: Mapping[str, Any], runs_dir: Path) -> GateSettings:
    """Settings for the AWS Lambda gate: no key file, no state file, no secret grace window.

    The private key and the webhook secret come from Secrets Manager, the
    state from DynamoDB, so the file fields are unused placeholders. The
    previous-secret grace is measured from process start, which on Lambda is
    each cold start, so it is off (0 minutes).
    """
    return GateSettings(
        app_id=_app_id(cfg),
        private_key_file=Path(os.devnull),
        installation_ids=_installation_ids(cfg),
        repos=_repos(cfg),
        secret_grace_minutes=0,
        state_file=Path(os.devnull),
        runs_dir=runs_dir,
        review_deadline=int(cfg["gate_review_deadline"]),
        watchdog_seconds=int(cfg["gate_watchdog_seconds"]),
        reconcile_minutes=int(cfg["gate_reconcile_minutes"]),
    )


def load_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines of a mode-0600 file; ``#`` comments, blanks and ``export`` allowed."""
    _require_private(path, "--env-file")
    pairs: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"--env-file {path}: cannot read it as UTF-8 text: {e}") from e
    for number, line in enumerate(lines, start=1):
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
