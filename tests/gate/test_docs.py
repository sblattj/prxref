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
    for heading in (
        "## Prerequisites",
        "## Install",
        "## Rotation",
        "## Local limits",
        "## Troubleshooting",
        "## Rollback",
    ):
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
