"""The composite action's environment names must be real config keys.

``action.yml`` forwards its inputs to prxref as ``PRXREF_*`` environment
variables. A name that is not a key in ``config._DEFAULTS`` is silently ignored
at runtime, so the input would do nothing. The check is line-based because the
test environment has no YAML parser.
"""
from __future__ import annotations

import re
from pathlib import Path

from prxref import config

ACTION = Path(__file__).resolve().parents[1] / "action.yml"
# Names the action sets for its own shell steps, not for prxref's config.
ACTION_ONLY = {"PRXREF_ACTION_VERSION", "PRXREF_ACTION_PR_URL"}


def _env_names(text: str) -> set[str]:
    return set(re.findall(r"^\s+(PRXREF_[A-Z0-9_]+):", text, flags=re.MULTILINE))


def test_every_env_name_is_a_config_key() -> None:
    names = _env_names(ACTION.read_text(encoding="utf-8")) - ACTION_ONLY
    assert names, "action.yml sets no PRXREF_ variables"
    for name in sorted(names):
        key = name.removeprefix("PRXREF_").lower()
        assert key in config._DEFAULTS, f"{name} in action.yml is not a config key"


def test_every_input_is_used_and_the_pr_is_never_checked_out() -> None:
    text = ACTION.read_text(encoding="utf-8")
    head = text.split("\nruns:")[0].split("\ninputs:")[1]
    declared = set(re.findall(r"^  ([a-z][a-z-]*):\s*$", head, re.MULTILINE))
    used = set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", text))
    assert declared == used
    assert "uses: actions/checkout" not in text
