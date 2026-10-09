"""GitHub App authentication for the gate: the app JWT and installation tokens (#102).

Tokens are minted per repository, scoped at mint to that one repository and
to the three permissions in :data:`TOKEN_PERMISSIONS` (GitHub grants
``metadata: read`` to every installation token implicitly), and cached until
five minutes before their one-hour expiry. No token or key text is ever logged
or put in an exception message; errors carry GitHub's request id and status.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime

import requests

from prxref.gate import require_jwt

TOKEN_PERMISSIONS = {"checks": "write", "pull_requests": "write", "contents": "read"}
_REFRESH_MARGIN_S = 300
_TIMEOUT_S = 10
_MALFORMED = (KeyError, ValueError, IndexError, TypeError, AttributeError)


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
                method,
                f"{self._api}{path}",
                headers=headers,
                timeout=_TIMEOUT_S,
                **kwargs,
            )
        except requests.RequestException as e:
            raise GateAuthError(f"{method} {path} failed: {e.__class__.__name__}") from None
        if resp.status_code >= 400:
            request_id = resp.headers.get("x-github-request-id", "?")
            raise GateAuthError(f"{method} {path} returned {resp.status_code} (request id {request_id})")
        try:
            data = resp.json()
        except ValueError as e:
            raise GateAuthError(f"{method} {path} returned a malformed reply: {e.__class__.__name__}") from None
        if not isinstance(data, dict):
            raise GateAuthError(f"{method} {path} returned a malformed reply: {type(data).__name__}")
        return data

    def installation_for(self, repo: str) -> int:
        """The installation id for ``owner/name``; refused unless it is allowlisted."""
        key = repo.lower()
        with self._lock:
            if key in self._installations:
                return self._installations[key]
        try:
            installation = int(self._call("GET", f"/repos/{repo}/installation")["id"])
        except _MALFORMED as e:
            raise GateAuthError(f"GET /repos/{repo}/installation: malformed reply ({e.__class__.__name__})") from None
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
        try:
            name = repo.split("/", 1)[1]
        except IndexError:
            raise GateAuthError(f"{repo!r} is not an owner/name repository") from None
        installation = self.installation_for(repo)
        path = f"/app/installations/{installation}/access_tokens"
        data = self._call("POST", path, json={"repositories": [name], "permissions": TOKEN_PERMISSIONS})
        try:
            token = data["token"]
            if not isinstance(token, str) or not token:
                raise TypeError("token")
            expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        except _MALFORMED as e:
            raise GateAuthError(f"POST {path}: malformed reply ({e.__class__.__name__})") from None
        from prxref.gate.worker import register_secret  # worker imports the gate stack; import late

        register_secret(token)
        with self._lock:
            self._tokens[key] = (token, expires)
        return token

    def invalidate(self, repo: str) -> None:
        """Drop the cached token for ``repo`` (after a 401)."""
        with self._lock:
            self._tokens.pop(repo.lower(), None)

    def bot_login(self) -> str:
        """The app's bot login, ``<slug>[bot]``, read once from ``GET /app``."""
        if self._bot_login is None:
            try:
                slug = self._call("GET", "/app")["slug"]
            except _MALFORMED as e:
                raise GateAuthError(f"GET /app: malformed reply ({e.__class__.__name__})") from None
            self._bot_login = f"{slug}[bot]"
        return self._bot_login
