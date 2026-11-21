"""Issue 177: an interactive setup helper is not an unwired check.

A PR adding ``scripts/setup-dev-profile.sh`` (browser login, a ``select``
prompt, a write to the user's home config) and its shell test suite got two
"added but no CI job runs it" warnings. The test-suite one is right; the
helper one is a false positive, since no CI job could run an interactive
script. Only the "it gains a shebang line" path is narrowed: a name, flag
or test-file signal still counts, and a test-looking script is never exempt.
"""
from __future__ import annotations

import pytest

from prxref import ci_wiring
from prxref.triage import parse_unified_diff


def _added(path: str, lines: list[str]) -> str:
    body = "\n".join(f"+{line}" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100755\n--- /dev/null\n"
        f"+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}\n"
    )


def _paths(*diffs: str) -> list[str]:
    return [c.path for c in ci_wiring.candidate_checks(parse_unified_diff("".join(diffs)))]


HELPER = [
    "#!/usr/bin/env bash",
    "# scripts/setup-dev-profile.sh",
    "set -euo pipefail",
    "cloud sso login --session dev            # opens a browser",
    "select role in admin readonly; do break; done",
    "printf '[profile dev]\\nrole = %s\\n' \"$role\" >> \"$HOME/.cloud/config\"",
]
SUITE = [
    "#!/usr/bin/env bash",
    "set -euo pipefail",
    "HOME=$(mktemp -d)",
    "out=$(bash scripts/setup-dev-profile.sh < /dev/null || true)",
    "[ -f \"$HOME/.cloud/config\" ] || { echo FAIL; exit 1; }",
]
REPRO = (
    _added("scripts/setup-dev-profile.sh", HELPER)
    + _added("scripts/tests/test-setup-dev-profile.sh", SUITE)
)


def test_the_issue_repro_flags_only_the_test_suite():
    assert _paths(REPRO) == ["scripts/tests/test-setup-dev-profile.sh"]


def test_the_issue_repro_end_to_end():
    files = parse_unified_diff(REPRO)
    ci = ".github/workflows/ci.yml"
    findings, record = ci_wiring.ci_wiring_findings(
        files, read={ci: "jobs:\n  t:\n    steps:\n      - run: pytest\n"}.get,
        listing=[ci],
    )
    assert [f.file for f in findings] == ["scripts/tests/test-setup-dev-profile.sh"]
    assert record["candidates"] == ["scripts/tests/test-setup-dev-profile.sh"]


@pytest.mark.parametrize("line", [
    "select env in dev prod; do break; done",
    "read -rp 'Profile name: ' name",
    "read -p \"Region? \" region",
    "read -r answer < /dev/tty",
    "exec < /dev/tty",
    "exec 3</dev/tty",
    "aws sso login --profile dev",
    "gcloud auth login",
    "az login",
    "gh auth login --web",
    "echo '[profile x]' >> ~/.aws/config",
    "cat > \"${HOME}/.config/tool/settings.toml\" <<EOT",
    "tee -a $HOME/.netrc",
    "    read -rp 'Profile name: ' name",
    "  gcloud auth login",
    "if ! aws sso login --profile dev; then",
    "then read -p \"Region? \" r",
])
def test_each_interactive_sign_exempts_a_shebang_only_script(line):
    assert _paths(_added("scripts/bootstrap.sh", ["#!/usr/bin/env bash", line])) == []


@pytest.mark.parametrize("line", [
    "# select role in admin readonly",
    "echo 'run gcloud auth login first'",
    "read -r line",
    "while read -r f; do echo \"$f\"; done < files.txt",
    "cp out.txt \"$TMPDIR/home-report\"",
    "echo ok > build/home.txt",
    "echo \"Use /dev/tty for terminal input\"",
    "echo done > /dev/tty",
])
def test_non_interactive_lines_keep_the_shebang_candidate(line):
    assert _paths(_added("scripts/bootstrap.sh", ["#!/usr/bin/env bash", line])) == [
        "scripts/bootstrap.sh"
    ]


def test_a_check_named_script_stays_flagged_even_when_interactive():
    lines = ["#!/usr/bin/env bash", "read -rp 'continue? ' x"]
    assert _paths(_added("scripts/verify-setup.sh", lines)) == ["scripts/verify-setup.sh"]


def test_a_test_looking_script_is_never_exempt():
    lines = ["#!/usr/bin/env bash", "echo '[p]' >> \"$HOME/.cloud/config\""]
    assert _paths(_added("tools/setup_test.sh", lines)) == ["tools/setup_test.sh"]
