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


@pytest.mark.parametrize(
    "name, label, argv_tail",
    [
        (
            "dev.prxref.gate.plist",
            "dev.prxref.gate",
            [
                "gate",
                "serve",
                "--env-file",
                "__HOME__/.config/prxref-gate/env",
                "--host",
                "127.0.0.1",
                "--port",
                "8787",
            ],
        ),
        ("dev.prxref.cloudflared.plist", "dev.prxref.cloudflared", ["tunnel", "run", "sdlc"]),
    ],
)
def test_the_plist_is_a_keepalive_service(name, label, argv_tail):
    p = _plist(name)
    assert p["Label"] == label
    assert p["KeepAlive"] is True and p["RunAtLoad"] is True
    assert p["ProgramArguments"][1:] == argv_tail
    # launchd needs an absolute path; __HOME__ is substituted with $HOME at install time
    assert p["ProgramArguments"][0].startswith(("/", "__HOME__/"))
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
    for secret in ("PRXREF_GATE_WEBHOOK_SECRET", "PRXREF_GATE_WEBHOOK_SECRET_PREVIOUS", "AWS_BEARER_TOKEN_BEDROCK"):
        assert pairs[secret] == "", secret
    assert pairs["PRXREF_LLM_BACKEND"] == "litellm"
    assert pairs["PRXREF_LLM_TIMEOUT"] == "900"
    assert pairs["PRXREF_GATE_REVIEW_DEADLINE"] == "900"
