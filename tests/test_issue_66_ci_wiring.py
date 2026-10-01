"""Issue 66: flag verification checks a PR adds but no CI job runs.

A PR that adds ``scripts/verify.sh`` while its ticket says "add regression
checks" has only partly met the criterion when no CI configuration file
ever runs the script. Three layers, each pinned on its own:

* the pure functions in ``prxref.ci_wiring`` — the default-include table,
  the candidate scan (name/flag hints, a gained shebang, a test file
  outside the default include), the CI-file globs and the invocation
  matcher — constructed by hand, no forge, no LLM, no I/O;
* the config surface — the ``ci_wiring`` choice, ``ci_wiring_globs`` as a
  list key, ``FILE_KEYS`` membership, and the built-in glob set restating
  ``ci_wiring.DEFAULT_CI_GLOBS``;
* the orchestrator wiring — the acceptance trio from the issue (an
  unwired verify script with a regression-check ticket raises one
  finding; a workflow step invoking it stays silent; a jest default
  include stays silent), the reader gate (a readerless run warns and
  records why; ``--repo-dir`` alone is enough), the off identity, and
  spec grounding degrading the ``spec`` severity on an ungrounded run.

``heuristics.is_deterministic`` is asserted like the metadata rules pin
it: the body suffix is the whole mechanism that exempts the finding from
severity consistency.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_orchestrator import (  # noqa: E402  (shared fixtures, read not guessed)
    REF,
    FakeForge,
    FakeLLM,
    make_pr,
)

from prxref import ci_wiring, config, heuristics, orchestrator  # noqa: E402
from prxref.forges.base import PathListing  # noqa: E402
from prxref.forges.repo_dir import RepoDir  # noqa: E402
from prxref.llm import ConfigError  # noqa: E402
from prxref.orchestrator import orchestrate_review  # noqa: E402
from prxref.specs import SpecSource  # noqa: E402
from prxref.text_inputs import cap_text  # noqa: E402
from prxref.ticket import TicketContext  # noqa: E402
from prxref.triage import FileDiff, parse_unified_diff  # noqa: E402

EMPTY_LLM = FakeLLM('{"findings":[],"escalations":[]}')

SPEC_TEXT = "## Rules\n\nTools MUST be named with an mcp prefix.\n"


@pytest.fixture(autouse=True)
def _contract_stubs(contract_stubs):
    """Pin the reviewer contract: the chunk and sweep stubs answer no
    findings, so every finding below comes from the CI wiring check alone."""
    return None


# --- fixtures ----------------------------------------------------------------


def _ticket(text: str) -> TicketContext:
    return TicketContext(
        path="ticket.md", capped=cap_text(text, 6000),
        text=text, has_acceptance_criteria=True,
    )


def _new_file_diff(path: str, body_lines: list[str]) -> str:
    body = "\n".join(f"+{line}" for line in body_lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(body_lines)} @@\n"
        f"{body}\n"
    )


VERIFY_SH = [
    "#!/usr/bin/env bash",
    "set -euo pipefail",
    "curl -fsS http://localhost:8080/health | grep -q 'no-store'",
]
VERIFY_DIFF = _new_file_diff("scripts/verify.sh", VERIFY_SH)

WORKFLOW_PATH = ".github/workflows/ci.yml"
WORKFLOW_TEXT = """\
name: ci
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: ./scripts/verify.sh
"""

# The literal entries of the built-in glob set: read directly even when no
# listing shows them (a miss costs one read), and named in a finding body.
LITERAL_CI_PATHS = [
    ".circleci/config.yml", ".drone.yml", ".gitlab-ci.yml", ".travis.yml",
    "Jenkinsfile", "azure-pipelines.yml", "bitbucket-pipelines.yml",
    "cloudbuild.yaml",
]


class CiForge(FakeForge):
    """FakeForge plus the optional GitHub-shaped head-sha reader.

    ``get_file_content``/``list_paths`` are exactly the methods
    ``repo_reader.forge_reader`` probes, so a CiForge run takes the forge
    reader route; every read is recorded so the off identity and the
    read bound can be asserted directly.
    """

    def __init__(self, diff: str, repo_files: dict[str, str] | None = None):
        super().__init__(pr=make_pr(), diff=diff)
        self.repo_files = dict(repo_files or {})
        self.reads: list[str] = []

    def get_file_content(self, ref, path, sha=None):
        self.reads.append(path)
        return self.repo_files.get(path)

    def list_paths(self, ref, sha=None):
        return PathListing(paths=tuple(sorted(self.repo_files)), complete=True)


def _ci_findings(res: dict) -> list:
    """The findings the CI wiring check made (vs any model echo)."""
    return [f for f in res["findings_active"] if heuristics.is_deterministic(f)]


# --- the pure functions -------------------------------------------------------


class TestDefaultInclude:
    @pytest.mark.parametrize(
        "path",
        [
            "tests/test_app.py", "test_app.py", "src/pkg/test_app.py",
            "src/app_test.py", "pkg/internal_test.py",
            "src/app.test.ts", "src/app.test.tsx", "src/app.spec.js",
            "src/app.spec.mjs", "__tests__/app.ts", "src/__tests__/app.ts",
            "pkg/app_test.go", "src/FooTest.java", "src/LoginIT.java",
            "AppTests.swift", "AppTests.m", "tests/integration.rb",
            "test/helper.rb", "spec/login_spec.rb",
        ],
    )
    def test_the_runner_defaults_include_the_file(self, path):
        assert ci_wiring.default_include(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "scripts/verify.sh", "src/app.py", "src/App.tests.tsx",
            "src/login_test.jsx", "docs/test-plan.md", "special_offer.py",
        ],
    )
    def test_everything_else_is_outside(self, path):
        assert ci_wiring.default_include(path) is False


class TestCandidateChecks:
    def _fd(self, path: str, status: str = "added") -> FileDiff:
        return FileDiff(path=path, old_path=path, new_path=path, status=status)

    def test_a_hint_in_the_basename_is_a_candidate(self):
        [candidate] = ci_wiring.candidate_checks([self._fd("scripts/verify.sh")])
        assert candidate.path == "scripts/verify.sh"
        assert "verify" in candidate.reason

    def test_a_hint_in_a_new_flag_is_a_candidate(self):
        files = parse_unified_diff(_new_file_diff(
            "scripts/build.py", ["parser.add_argument('--smoke', action='store_true')"],
        ))
        [candidate] = ci_wiring.candidate_checks(files)
        assert candidate.path == "scripts/build.py"
        assert "--smoke" in candidate.reason

    def test_a_gained_shebang_is_a_candidate(self):
        files = parse_unified_diff(_new_file_diff("tools/migrate", ["#!/usr/bin/env python3"]))
        [candidate] = ci_wiring.candidate_checks(files)
        assert candidate.path == "tools/migrate"
        assert "shebang" in candidate.reason

    def test_a_new_test_file_outside_the_default_include_is_a_candidate(self):
        [candidate] = ci_wiring.candidate_checks([self._fd("src/App.tests.tsx")])
        assert "default include" in candidate.reason

    def test_a_default_included_test_file_is_not_a_candidate(self):
        assert ci_wiring.candidate_checks([self._fd("src/app.test.ts")]) == []

    def test_a_removed_file_is_never_a_candidate(self):
        assert ci_wiring.candidate_checks([self._fd("scripts/verify.sh", "removed")]) == []

    def test_an_ordinary_source_file_is_not_a_candidate(self):
        assert ci_wiring.candidate_checks([self._fd("src/app.py")]) == []

    def test_the_result_is_sorted_by_path(self):
        found = ci_wiring.candidate_checks([self._fd("b/verify.sh"), self._fd("a/smoke.py")])
        assert [c.path for c in found] == ["a/smoke.py", "b/verify.sh"]


class TestCiConfigPaths:
    def test_the_listing_matches_the_globs(self):
        paths = ci_wiring.ci_config_paths(
            [".github/workflows/ci.yml", ".github/workflows/release.yml", "src/app.py"],
            [".github/workflows/*.y*ml"],
        )
        assert paths == [".github/workflows/ci.yml", ".github/workflows/release.yml"]

    def test_literal_globs_are_read_without_a_listing(self):
        paths = ci_wiring.ci_config_paths(None, list(ci_wiring.DEFAULT_CI_GLOBS))
        assert paths == LITERAL_CI_PATHS

    def test_a_set_value_replaces_the_built_in_set(self):
        paths = ci_wiring.ci_config_paths(
            [".gitlab-ci.yml", "Jenkinsfile"], ["Jenkinsfile"],
        )
        assert paths == ["Jenkinsfile"]


class TestInvokes:
    CANDIDATE = ci_wiring.CiCandidate("scripts/verify.sh", "test")

    def test_a_github_run_step_invoking_the_full_path(self):
        assert ci_wiring.invokes(WORKFLOW_TEXT, WORKFLOW_PATH, self.CANDIDATE) is True

    def test_the_bare_basename_at_word_boundaries(self):
        text = "jobs:\n  test:\n    steps:\n      - run: bash verify.sh\n"
        assert ci_wiring.invokes(text, WORKFLOW_PATH, self.CANDIDATE) is True

    def test_a_gitlab_script_list(self):
        text = "build:\n  script:\n    - pip install .\n    - ./scripts/verify.sh\n"
        assert ci_wiring.invokes(text, ".gitlab-ci.yml", self.CANDIDATE) is True

    def test_an_azure_bash_block(self):
        text = "steps:\n- bash: |\n    ./scripts/verify.sh --smoke\n"
        assert ci_wiring.invokes(text, "azure-pipelines.yml", self.CANDIDATE) is True

    def test_a_plain_shell_line_with_no_yaml(self):
        assert ci_wiring.invokes("./scripts/verify.sh\n", "run.sh", self.CANDIDATE) is True

    def test_a_comment_only_mention_stays_unwired(self):
        text = "jobs:\n  test:\n    steps:\n      - run: make test\n      # TODO run scripts/verify.sh\n"
        assert ci_wiring.invokes(text, WORKFLOW_PATH, self.CANDIDATE) is False

    def test_a_label_mentioning_the_script_stays_unwired(self):
        text = (
            "jobs:\n  test:\n    steps:\n      - name: run scripts/verify.sh\n"
            "        run: make test\n"
        )
        assert ci_wiring.invokes(text, WORKFLOW_PATH, self.CANDIDATE) is False

    def test_a_make_target_without_the_path_is_not_matched(self):
        text = "jobs:\n  test:\n    steps:\n      - run: make verify\n"
        assert ci_wiring.invokes(text, WORKFLOW_PATH, self.CANDIDATE) is False

    def test_a_renamed_copy_counts_as_wired_the_documented_gap(self):
        """The accepted false negative from the issue: a CI job that only
        COPIES the script still mentions its path on an invocation line,
        so v1 counts it as wired rather than risk target-name matching."""
        text = "jobs:\n  test:\n    steps:\n      - run: cp scripts/verify.sh stage.sh && ./stage.sh\n"
        assert ci_wiring.invokes(text, WORKFLOW_PATH, self.CANDIDATE) is True

    def test_a_stage_name_that_shares_no_path_stays_unwired(self):
        text = "jobs:\n  test:\n    steps:\n      - run: ./staged-verify.sh\n"
        assert ci_wiring.invokes(text, WORKFLOW_PATH, self.CANDIDATE) is False


class TestCiWiringFindings:
    def _files(self):
        return parse_unified_diff(VERIFY_DIFF)

    def test_the_read_bound_caps_at_max_ci_files(self):
        listing = [f".github/workflows/job{i:02d}.yml" for i in range(30)]
        reads: list[str] = []
        ci_wiring.ci_wiring_findings(
            self._files(), read=reads.append, listing=listing,
        )
        assert len(reads) == ci_wiring.MAX_CI_FILES

    def test_no_candidates_reads_nothing(self):
        reads: list[str] = []
        findings, record = ci_wiring.ci_wiring_findings(
            parse_unified_diff(_new_file_diff("src/app.py", ["x = 1"])),
            read=reads.append, listing=[".gitlab-ci.yml"],
        )
        assert findings == []
        assert reads == []
        assert record["triggered"] is False

    def test_the_severity_switch_reads_the_ticket_text(self):
        files = self._files()
        spec, spec_record = ci_wiring.ci_wiring_findings(
            files, read=lambda p: None, listing=None,
            ticket_text="Add regression checks so this cannot come back.",
        )
        warning, _ = ci_wiring.ci_wiring_findings(
            files, read=lambda p: None, listing=None,
            ticket_text="Rewrite the header component.",
        )
        none, _ = ci_wiring.ci_wiring_findings(
            files, read=lambda p: None, listing=None, ticket_text=None,
        )
        assert spec[0].severity == "spec" and spec_record["triggered"] is True
        assert warning[0].severity == "warning"
        assert none[0].severity == "warning"

    def test_a_read_returning_none_is_skipped_never_raised(self):
        findings, record = ci_wiring.ci_wiring_findings(
            self._files(), read=lambda p: None, listing=None,
        )
        assert len(findings) == 1
        assert record["ci_files"] == []
        assert "no CI configuration file was found" in findings[0].body

    def test_a_literal_absent_from_the_listing_is_not_read(self):
        reads: list[str] = []

        def read(path):
            reads.append(path)
            return None

        ci_wiring.ci_wiring_findings(
            self._files(), read=read, listing=["src/app.py"],
        )
        assert reads == []

    def test_a_missing_literal_never_crowds_out_a_real_workflow(self):
        workflows = {
            f".github/workflows/w{n:02d}.yml": "name: x\njobs: {}\n" for n in range(11)
        }
        workflows[".github/workflows/w10.yml"] = "run: bash scripts/verify.sh\n"
        findings, record = ci_wiring.ci_wiring_findings(
            self._files(), read=workflows.get, listing=sorted(workflows),
        )
        assert findings == []
        assert ".github/workflows/w10.yml" in record["ci_files"]


# --- the config surface -------------------------------------------------------


class TestConfigSurface:
    def test_the_defaults_and_the_choice(self):
        cfg = config.load_config()
        assert cfg["ci_wiring"] == "on"
        assert cfg["ci_wiring_globs"] == list(ci_wiring.DEFAULT_CI_GLOBS)

    def test_off_is_still_accepted(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CI_WIRING", "off")
        assert config.load_config()["ci_wiring"] == "off"

    def test_the_builtin_glob_set_is_restated_not_drifted(self):
        assert config._DEFAULTS["ci_wiring_globs"] == list(ci_wiring.DEFAULT_CI_GLOBS)

    def test_both_keys_are_file_keys(self):
        assert {"ci_wiring", "ci_wiring_globs"} <= config.FILE_KEYS

    def test_a_value_outside_the_vocabulary_is_a_config_error(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CI_WIRING", "sometimes")
        with pytest.raises(ConfigError, match="PRXREF_CI_WIRING"):
            config.load_config()

    def test_the_globs_parse_as_a_list_key(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CI_WIRING_GLOBS", "ci/a.yml,ci/b.yml")
        cfg = config.load_config()
        assert cfg["ci_wiring_globs"] == ["ci/a.yml", "ci/b.yml"]

    def test_an_empty_globs_value_keeps_the_builtin_set(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CI_WIRING_GLOBS", " ")
        assert config.load_config()["ci_wiring_globs"] == list(ci_wiring.DEFAULT_CI_GLOBS)


# --- the orchestrator wiring --------------------------------------------------


class TestTheAcceptance:
    """The issue's three acceptance criteria, end to end."""

    def _run(self, forge, **kw):
        return orchestrate_review(forge, REF, EMPTY_LLM, post=False, **kw)

    def test_an_unwired_script_with_a_regression_ticket_raises_one_spec_finding(
        self, monkeypatch,
    ):
        monkeypatch.setattr(
            orchestrator.specs, "fetch_specs",
            lambda *a, **k: [SpecSource(
                origin="docs/spec.md", kind="file", text=SPEC_TEXT, error="",
            )],
        )
        forge = CiForge(VERIFY_DIFF, repo_files={})
        res = self._run(
            forge, ci_wiring="on", ticket=_ticket("Add regression checks so this cannot come back."),
            spec_sources=["docs/spec.md"],
        )

        found = _ci_findings(res)
        assert len(found) == 1, f"expected exactly one finding, got {res['findings_active']}"
        f = found[0]
        assert f.severity == "spec"
        assert f.confidence == 1.0
        assert f.file == "scripts/verify.sh"
        assert f.line == 0
        assert f.drop_reason is None
        assert "scripts/verify.sh" in f.title
        assert heuristics.is_deterministic(f)
        assert "no CI configuration file was found" in f.body

        assert res["ci_wiring"] == {
            "candidates": ["scripts/verify.sh"],
            "ci_files": [],
            "picked_up_default": [],
            "triggered": True,
        }

    def test_a_spec_source_mentioning_regression_checks_gives_spec_severity(
        self, monkeypatch,
    ):
        monkeypatch.setattr(
            orchestrator.specs, "fetch_specs",
            lambda *a, **k: [SpecSource(
                origin="docs/spec.md", kind="file",
                text="## Rules\n\nThe PR MUST add regression checks.\n", error="",
            )],
        )
        forge = CiForge(VERIFY_DIFF, repo_files={})
        res = self._run(forge, ci_wiring="on", spec_sources=["docs/spec.md"])
        [f] = _ci_findings(res)
        assert f.severity == "spec"

    def test_a_failed_spec_source_does_not_raise_severity(self, monkeypatch):
        monkeypatch.setattr(
            orchestrator.specs, "fetch_specs",
            lambda *a, **k: [SpecSource(
                origin="docs/spec.md", kind="file",
                text="add regression checks", error="boom",
            )],
        )
        forge = CiForge(VERIFY_DIFF, repo_files={})
        res = self._run(forge, ci_wiring="on", spec_sources=["docs/spec.md"])
        [f] = _ci_findings(res)
        assert f.severity == "warning"

    def test_a_workflow_step_invoking_the_script_stays_silent(self):
        forge = CiForge(VERIFY_DIFF, repo_files={WORKFLOW_PATH: WORKFLOW_TEXT})
        res = self._run(forge, ci_wiring="on", ticket=_ticket("Add regression checks."))

        assert res["findings_active"] == []
        assert res["ci_wiring"]["triggered"] is False
        assert res["ci_wiring"]["candidates"] == ["scripts/verify.sh"]
        assert res["ci_wiring"]["ci_files"] == [WORKFLOW_PATH]
        assert WORKFLOW_PATH in forge.reads

    def test_a_jest_default_include_file_stays_silent(self):
        diff = _new_file_diff("src/app.test.ts", ["it('works', () => {});"])
        forge = CiForge(diff, repo_files={WORKFLOW_PATH: WORKFLOW_TEXT})
        res = self._run(forge, ci_wiring="on")

        assert res["findings_active"] == []
        assert res["ci_wiring"] == {
            "candidates": [],
            "ci_files": [],
            "picked_up_default": ["src/app.test.ts"],
            "triggered": False,
        }
        # No CI configuration file was read (the diff file's own read is
        # chunk context, not this check).
        assert WORKFLOW_PATH not in forge.reads
        assert not any(path in LITERAL_CI_PATHS for path in forge.reads)


class TestSeverity:
    def test_no_ticket_gives_warning_severity(self):
        forge = CiForge(VERIFY_DIFF, repo_files={})
        res = orchestrate_review(forge, REF, EMPTY_LLM, post=False, ci_wiring="on")
        [f] = _ci_findings(res)
        assert f.severity == "warning"
    def test_a_ticket_without_ci_words_gives_warning_severity(self):
        forge = CiForge(VERIFY_DIFF, repo_files={})
        res = orchestrate_review(
            forge, REF, EMPTY_LLM, post=False, ci_wiring="on",
            ticket=_ticket("Rewrite the header component for the marketing site."),
        )
        [f] = _ci_findings(res)
        assert f.severity == "warning"

    def test_an_ungrounded_run_degrades_spec_to_warning(self):
        """The safe default from the issue: without a spec digest the
        ``spec`` severity is relabelled, so the finding still lands."""
        forge = CiForge(VERIFY_DIFF, repo_files={})
        res = orchestrate_review(
            forge, REF, EMPTY_LLM, post=False, ci_wiring="on",
            ticket=_ticket("Add regression checks so this cannot come back."),
        )
        [f] = _ci_findings(res)
        assert f.severity == "warning"


class TestTheReaderGate:
    def test_off_reads_nothing_and_records_null(self):
        forge = CiForge(VERIFY_DIFF, repo_files={WORKFLOW_PATH: WORKFLOW_TEXT})
        res = orchestrate_review(forge, REF, EMPTY_LLM, post=False, ci_wiring="off")

        assert res["ci_wiring"] is None
        assert forge.reads == []
        assert res["findings_active"] == []

    def test_the_default_runs_the_check(self):
        forge = CiForge(VERIFY_DIFF, repo_files={})
        res = orchestrate_review(forge, REF, EMPTY_LLM, post=False)

        [f] = _ci_findings(res)
        assert f.file == "scripts/verify.sh"
        assert res["ci_wiring"]["triggered"] is True

    def test_a_readerless_run_warns_and_records_the_reason(self, caplog):
        forge = FakeForge(diff=VERIFY_DIFF)  # no get_file_content, no repo_dir
        with caplog.at_level("WARNING"):
            res = orchestrate_review(forge, REF, EMPTY_LLM, post=False, ci_wiring="on")

        assert res["ci_wiring"] == {"triggered": False, "reason": "no reader"}
        assert res["findings_active"] == []
        assert any(
            "PRXREF_CI_WIRING" in record.message and record.levelname == "WARNING"
            for record in caplog.records
        )

    def test_a_readerless_run_on_the_default_notes_it_at_info_only(self, caplog):
        forge = FakeForge(diff=VERIFY_DIFF)
        with caplog.at_level("INFO"):
            res = orchestrate_review(forge, REF, EMPTY_LLM, post=False)

        assert res["ci_wiring"] == {"triggered": False, "reason": "no reader"}
        notices = [r for r in caplog.records if "PRXREF_CI_WIRING" in r.message]
        assert [r.levelname for r in notices] == ["INFO"]

    def test_the_cli_passes_a_defaulted_value_as_the_default(self, monkeypatch, tmp_path):
        from prxref import cli, llm_backends

        seen: list = []

        def fake_orchestrate(*args, **kwargs):
            seen.append(kwargs["ci_wiring"])
            return {}

        diff = tmp_path / "pr.diff"
        diff.write_text(VERIFY_DIFF, encoding="utf-8")
        monkeypatch.setattr(orchestrator, "orchestrate_review", fake_orchestrate)
        monkeypatch.setattr(llm_backends, "create_llm_client", lambda cfg: EMPTY_LLM)
        monkeypatch.delenv("PRXREF_CI_WIRING", raising=False)
        cli._run_review(None, post=False, diff_file=str(diff))
        monkeypatch.setenv("PRXREF_CI_WIRING", "on")
        cli._run_review(None, post=False, diff_file=str(diff))
        cli._run_review(None, post=False, diff_file=str(diff), ci_wiring="off")

        assert seen == [None, "on", "off"]

    def test_repo_dir_alone_is_enough(self, tmp_path):
        """The acceptance route: no forge file reader at all, just a local
        checkout, and the check still runs off its own reader."""
        forge = FakeForge(diff=VERIFY_DIFF)

        unwired = orchestrate_review(
            forge, REF, EMPTY_LLM, post=False, ci_wiring="on", repo_dir=RepoDir(tmp_path),
        )
        [f] = _ci_findings(unwired)
        assert f.file == "scripts/verify.sh"

        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        (tmp_path / WORKFLOW_PATH).write_text(WORKFLOW_TEXT, encoding="utf-8")
        wired = orchestrate_review(
            forge, REF, EMPTY_LLM, post=False, ci_wiring="on", repo_dir=RepoDir(tmp_path),
        )
        assert wired["findings_active"] == []
        assert wired["ci_wiring"]["ci_files"] == [WORKFLOW_PATH]


def _modified_diff(path: str, removed: list[str], added: list[str], ctx: bool = True) -> str:
    body = "\n".join([f"-{x}" for x in removed] + [f"+{x}" for x in added])
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,{len(removed) + ctx} +1,{len(added) + ctx} @@\n"
        f"{' context' + chr(10) if ctx else ''}"
        f"{body}\n"
    )


class TestModifiedCandidates:
    def test_a_body_only_edit_to_a_check_named_script_is_no_candidate(self):
        files = parse_unified_diff(_modified_diff(
            "scripts/check_env.sh", ["echo old"], ["echo new"],
        ))
        assert ci_wiring.candidate_checks(files) == []

    def test_a_modified_file_gaining_a_shebang_is_no_candidate(self):
        files = parse_unified_diff(_modified_diff(
            "tools/migrate", ["x = 1"], ["#!/usr/bin/env python3"], ctx=False,
        ))
        assert ci_wiring.candidate_checks(files) == []

    def test_a_modified_script_gaining_a_flag_is_a_changed_candidate(self):
        files = parse_unified_diff(_modified_diff(
            "scripts/build.py", ["p.add_argument('--fast')"],
            ["p.add_argument('--fast')", "p.add_argument('--verify')"],
        ))
        [candidate] = ci_wiring.candidate_checks(files)
        assert candidate.path == "scripts/build.py"
        assert "--verify" in candidate.reason
        findings, _ = ci_wiring.ci_wiring_findings(
            files, listing=None, read=lambda p: "run: make build\n" if p == "Jenkinsfile" else None,
            ticket_text=None, globs=[],
        )
        [finding] = findings
        assert "changed" in finding.title
        assert "added" not in finding.title
        assert "This PR changes" in finding.body

    def test_a_flag_already_present_in_removed_lines_is_not_gained(self):
        files = parse_unified_diff(_modified_diff(
            "scripts/build.py", ["p.add_argument('--verify')"],
            ["p.add_argument('--verify', help='x')"],
        ))
        assert ci_wiring.candidate_checks(files) == []

    def test_an_added_candidate_keeps_the_added_wording(self):
        files = parse_unified_diff(VERIFY_DIFF)
        findings, _ = ci_wiring.ci_wiring_findings(
            files, listing=None, read=lambda p: "run: make\n" if p == "Jenkinsfile" else None,
            ticket_text=None, globs=[],
        )
        [finding] = findings
        assert "is added but" in finding.title
        assert "This PR adds" in finding.body
