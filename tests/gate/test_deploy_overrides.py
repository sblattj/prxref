"""deploy.sh merges PRXREF_GATE_PARAM_OVERRIDES over samconfig's parameter overrides.

The merge is the Python heredoc inside deploy.sh; it is run here as written, so the
test sees the shipped code rather than a copy of it.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "aws" / "deploy.sh"
SAMCONFIG = """version = 0.1
[default.deploy.parameters]
image_repository = "1.dkr.ecr.us-west-2.amazonaws.com/gate"
parameter_overrides = {AppId = "1", ModelId = "bedrock/a", ImageUri = "x@sha256:0"}
"""


def _merge(tmp_path, overrides):
    code = re.search(r"<<'PY'\n(.*?)\nPY\n", DEPLOY.read_text(), re.S).group(1)
    cfg = tmp_path / "samconfig.toml"
    cfg.write_text(SAMCONFIG)
    env = {k: v for k, v in os.environ.items() if k != "PRXREF_GATE_PARAM_OVERRIDES"}
    if overrides is not None:
        env["PRXREF_GATE_PARAM_OVERRIDES"] = overrides
    return subprocess.run([sys.executable, "-c", code, str(cfg)], capture_output=True, text=True, env=env)


def test_without_overrides_the_saved_parameters_pass_through(tmp_path):
    r = _merge(tmp_path, None)
    assert r.returncode == 0
    assert r.stdout.splitlines() == ["1.dkr.ecr.us-west-2.amazonaws.com/gate", "AppId=1", "ModelId=bedrock/a"]


def test_an_override_replaces_one_key_and_adds_another(tmp_path):
    r = _merge(
        tmp_path, "ModelId=openrouter/x,bedrock/a LlmApiKeySecretArn=arn:aws:secretsmanager:k ReasoningEffort=off"
    )
    assert r.returncode == 0
    assert r.stdout.splitlines()[1:] == [
        "AppId=1",
        "ModelId=openrouter/x,bedrock/a",
        "LlmApiKeySecretArn=arn:aws:secretsmanager:k",
        "ReasoningEffort=off",
    ]


@pytest.mark.parametrize("bad", ["ModelId", "=x"])
def test_a_pair_without_key_and_value_stops_the_deploy(tmp_path, bad):
    r = _merge(tmp_path, bad)
    assert r.returncode != 0 and "not Key=Value" in r.stderr
