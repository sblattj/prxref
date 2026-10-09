"""App JWT, installation lookup and scoped installation tokens (#102)."""

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from prxref.gate import app_auth


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
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


def test_token_is_minted_scoped_to_one_repo_and_three_permissions(keypair):
    session = _Session(
        [
            _Resp(200, {"id": 777}),
            _Resp(201, {"token": "ghs_one", "expires_at": "1970-01-12T14:46:40Z"}),
        ]
    )
    auth = _auth(keypair, session, clock=lambda: 1_000_000.0)
    assert auth.token_for("acme/widget") == "ghs_one"
    method, url, kwargs = session.calls[1]
    assert (method, url) == ("POST", "https://api.github.com/app/installations/777/access_tokens")
    assert kwargs["json"] == {"repositories": ["widget"], "permissions": app_auth.TOKEN_PERMISSIONS}
    assert kwargs["headers"]["Authorization"].startswith("Bearer ey")


def test_token_is_cached_until_five_minutes_before_expiry(keypair):
    now = [1_000_000.0]
    expires = "1970-01-12T14:46:40Z"
    session = _Session(
        [
            _Resp(200, {"id": 777}),
            _Resp(201, {"token": "ghs_one", "expires_at": expires}),
            _Resp(201, {"token": "ghs_two", "expires_at": "1970-01-12T15:46:40Z"}),
        ]
    )
    auth = _auth(keypair, session, clock=lambda: now[0])
    assert auth.token_for("acme/widget") == "ghs_one"
    now[0] = 1_000_000.0 + 3600 - 301
    assert auth.token_for("acme/widget") == "ghs_one"
    now[0] = 1_000_000.0 + 3600 - 299
    assert auth.token_for("acme/widget") == "ghs_two"
    assert len(session.calls) == 3


def test_invalidate_forces_a_new_token(keypair):
    session = _Session(
        [
            _Resp(200, {"id": 777}),
            _Resp(201, {"token": "ghs_one", "expires_at": "1970-01-12T14:46:40Z"}),
            _Resp(201, {"token": "ghs_two", "expires_at": "1970-01-12T14:46:40Z"}),
        ]
    )
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
    assert "returned 500" in str(info.value)
    assert session.calls[1][2]["headers"]["Authorization"].split()[1] not in str(info.value)
