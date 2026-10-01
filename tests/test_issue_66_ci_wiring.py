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

    def test_a_make_target_alone_is_not_matched_without_its_makefile(self):
        text = "jobs:\n  test:\n    steps:\n      - run: make verify\n"
        assert ci_wiring.invokes(text, WORKFLOW_PATH, self.CANDIDATE) is False

    def test_a_make_target_whose_recipe_names_the_script_is_wired(self):
        text = "jobs:\n  test:\n    steps:\n      - run: make verify\n"
        runners = {"Makefile": "verify:\n\t./scripts/verify.sh\n"}
        assert ci_wiring.invokes(
            text, WORKFLOW_PATH, self.CANDIDATE, runners=runners,
        ) is True

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


# --- one hop through runner files (OD10) ---------------------------------------


MAKE_WORKFLOW = "jobs:\n  test:\n    steps:\n      - run: make verify\n"


class TestRunnerTargets:
    """The make/npm/yarn/pnpm invocations an invocation line names."""

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("make verify", {("make", "verify")}),
            ("make -j4 lint verify V=1", {("make", "lint"), ("make", "verify")}),
            ("make", {("make", "")}),
            ("cd app && make verify && echo ok", {("make", "verify")}),
            ("make -C sub verify", set()),
            ("make -f ci.mk verify", set()),
            ("cmake --build .", set()),
            ("npm run verify", {("npm", "verify")}),
            ("npm run-script --silent verify", {("npm", "verify")}),
            ("npm test", {("npm", "test")}),
            ("npm t", {("npm", "test")}),
            ("npm ci", set()),
            ("npm --prefix web run verify", set()),
            ("yarn verify", {("npm", "verify")}),
            ("pnpm run verify", {("npm", "verify")}),
        ],
    )
    def test_the_targets(self, line, expected):
        assert ci_wiring.runner_targets(line) == expected


class TestRunnerHop:
    CANDIDATE = ci_wiring.CiCandidate("scripts/verify.sh", "test")

    def _wired(self, ci_text: str, runners: dict[str, str]) -> bool:
        return ci_wiring.invokes(ci_text, WORKFLOW_PATH, self.CANDIDATE, runners=runners)

    def test_a_makefile_target_naming_the_script_is_wired(self):
        assert self._wired(MAKE_WORKFLOW, {"Makefile": "verify:\n\t./scripts/verify.sh\n"})

    def test_a_makefile_target_not_naming_the_script_stays_unwired(self):
        makefile = "verify:\n\tpytest -q\n\nsmoke:\n\t./scripts/verify.sh\n"
        assert not self._wired(MAKE_WORKFLOW, {"Makefile": makefile})

    def test_a_prerequisite_inside_the_same_makefile_is_followed(self):
        makefile = "verify: lint smoke | out\n\t@echo done\n\nsmoke:\n\t./scripts/verify.sh\n"
        assert self._wired(MAKE_WORKFLOW, {"Makefile": makefile})

    def test_a_prerequisite_cycle_terminates_unwired(self):
        makefile = "verify: a\n\ttrue\na: verify\n\techo a\n"
        assert not self._wired(MAKE_WORKFLOW, {"Makefile": makefile})

    def test_a_nested_make_call_is_not_followed(self):
        makefile = "verify:\n\t$(MAKE) smoke\n\nsmoke:\n\t./scripts/verify.sh\n"
        assert not self._wired(MAKE_WORKFLOW, {"Makefile": makefile})

    def test_a_commented_recipe_line_stays_unwired(self):
        makefile = "verify:\n\t# ./scripts/verify.sh\n\tpytest\n"
        assert not self._wired(MAKE_WORKFLOW, {"Makefile": makefile})

    def test_a_recipe_prefix_and_a_multi_target_rule(self):
        makefile = "CHECK := x\n\nlint verify: deps\n\t@bash scripts/verify.sh --all\n"
        assert self._wired(MAKE_WORKFLOW, {"Makefile": makefile})

    def test_an_inline_recipe_after_a_semicolon(self):
        assert self._wired(MAKE_WORKFLOW, {"Makefile": "verify: ; ./scripts/verify.sh\n"})

    def test_bare_make_follows_the_default_goal(self):
        ci_text = "jobs:\n  t:\n    steps:\n      - run: make\n"
        makefile = ".PHONY: all\nall:\n\t./scripts/verify.sh\n\nother:\n\ttrue\n"
        assert self._wired(ci_text, {"Makefile": makefile})

    def test_an_npm_script_naming_the_script_is_wired(self):
        ci_text = "jobs:\n  t:\n    steps:\n      - run: npm run verify\n"
        package = '{"scripts": {"verify": "bash scripts/verify.sh", "test": "jest"}}'
        assert self._wired(ci_text, {"package.json": package})

    def test_npm_test_follows_the_test_script(self):
        ci_text = "jobs:\n  t:\n    steps:\n      - run: npm test\n"
        package = '{"scripts": {"test": "jest && ./scripts/verify.sh"}}'
        assert self._wired(ci_text, {"package.json": package})

    def test_an_npm_script_not_naming_the_script_stays_unwired(self):
        ci_text = "jobs:\n  t:\n    steps:\n      - run: npm run verify\n"
        package = '{"scripts": {"verify": "tsc --noEmit", "smoke": "./scripts/verify.sh"}}'
        assert not self._wired(ci_text, {"package.json": package})

    def test_a_malformed_package_json_stays_unwired_and_never_raises(self):
        ci_text = "jobs:\n  t:\n    steps:\n      - run: npm run verify\n"
        assert not self._wired(ci_text, {"package.json": "{not json"})
        assert not self._wired(ci_text, {"package.json": '["a list"]'})
        assert not self._wired(ci_text, {"package.json": '{"scripts": {"verify": 3}}'})

    def test_a_make_invocation_in_a_comment_is_not_followed(self):
        ci_text = "jobs:\n  t:\n    steps:\n      - run: pytest\n      # make verify\n"
        assert not self._wired(ci_text, {"Makefile": "verify:\n\t./scripts/verify.sh\n"})


class TestRunnerHopFindings:
    def _files(self):
        return parse_unified_diff(VERIFY_DIFF)

    def test_ci_make_verify_with_a_makefile_naming_the_script_gives_no_finding(self):
        repo = {WORKFLOW_PATH: MAKE_WORKFLOW, "Makefile": "verify:\n\t./scripts/verify.sh\n"}
        findings, record = ci_wiring.ci_wiring_findings(
            self._files(), read=repo.get, listing=sorted(repo),
        )
        assert findings == []
        assert record["triggered"] is False
        assert record["ci_files"] == [WORKFLOW_PATH]

    def test_a_makefile_target_not_naming_the_script_still_gives_a_finding(self):
        repo = {WORKFLOW_PATH: MAKE_WORKFLOW, "Makefile": "verify:\n\tpytest -q\n"}
        findings, record = ci_wiring.ci_wiring_findings(
            self._files(), read=repo.get, listing=sorted(repo),
        )
        [finding] = findings
        assert finding.file == "scripts/verify.sh"
        assert "`Makefile` (followed from `make verify`)" in finding.body
        assert record["triggered"] is True
        assert record["ci_files"] == [WORKFLOW_PATH]

    def test_runner_files_are_read_only_when_ci_invokes_a_runner(self):
        reads: list[str] = []
        repo = {
            WORKFLOW_PATH: "jobs:\n  t:\n    steps:\n      - run: pytest\n",
            "Makefile": "verify:\n\t./scripts/verify.sh\n",
            "package.json": '{"scripts": {}}',
        }

        def read(path):
            reads.append(path)
            return repo.get(path)

        findings, _ = ci_wiring.ci_wiring_findings(
            self._files(), read=read, listing=sorted(repo),
        )
        assert len(findings) == 1
        assert reads == [WORKFLOW_PATH]

    def test_a_runner_absent_from_the_listing_is_not_read(self):
        reads: list[str] = []
        repo = {WORKFLOW_PATH: MAKE_WORKFLOW}

        def read(path):
            reads.append(path)
            return repo.get(path)

        ci_wiring.ci_wiring_findings(self._files(), read=read, listing=sorted(repo))
        assert reads == [WORKFLOW_PATH]

    def test_without_a_listing_the_runner_literals_are_read(self):
        repo = {"Jenkinsfile": "make verify\n", "Makefile": "verify:\n\t./scripts/verify.sh\n"}
        findings, _ = ci_wiring.ci_wiring_findings(
            self._files(), read=repo.get, listing=None,
        )
        assert findings == []

    def test_gnu_make_prefers_gnumakefile_over_makefile(self):
        repo = {
            WORKFLOW_PATH: MAKE_WORKFLOW,
            "GNUmakefile": "verify:\n\tpytest\n",
            "Makefile": "verify:\n\t./scripts/verify.sh\n",
        }
        findings, _ = ci_wiring.ci_wiring_findings(
            self._files(), read=repo.get, listing=sorted(repo),
        )
        assert len(findings) == 1

    def test_a_raising_runner_read_never_escapes(self):
        def read(path):
            if path == "Makefile":
                raise OSError("boom")
            return MAKE_WORKFLOW if path == WORKFLOW_PATH else None

        findings, _ = ci_wiring.ci_wiring_findings(
            self._files(), read=read, listing=[WORKFLOW_PATH, "Makefile"],
        )
        assert len(findings) == 1

    def test_the_acceptance_end_to_end_through_the_forge_reader(self):
        forge = CiForge(VERIFY_DIFF, repo_files={
            WORKFLOW_PATH: MAKE_WORKFLOW, "Makefile": "verify:\n\t./scripts/verify.sh\n",
        })
        res = orchestrate_review(
            forge, REF, EMPTY_LLM, post=False, ci_wiring="on",
            ticket=_ticket("Add regression checks."),
        )
        assert _ci_findings(res) == []
        assert "Makefile" in forge.reads

        unwired = CiForge(VERIFY_DIFF, repo_files={
            WORKFLOW_PATH: MAKE_WORKFLOW, "Makefile": "verify:\n\tpytest\n",
        })
        res = orchestrate_review(
            unwired, REF, EMPTY_LLM, post=False, ci_wiring="on",
            ticket=_ticket("Add regression checks."),
        )
        [f] = _ci_findings(res)
        assert f.file == "scripts/verify.sh"


# --- the target kind: a new make/npm check target (OD10, F38) ------------------


VERIFY_TARGET_DIFF = _modified_diff(
    "Makefile", [], ["", "verify:", "\t./scripts/check-health.sh"],
)
MAKEFILE_AFTER = "build:\n\tgo build ./...\n\nverify:\n\t./scripts/check-health.sh\n"
PYTEST_WORKFLOW = "jobs:\n  t:\n    steps:\n      - run: pytest\n"

PACKAGE_DIFF = _modified_diff(
    "package.json",
    ['    "build": "tsc"'],
    ['    "build": "tsc",', '    "smoke": "node scripts/smoke-run.js"'],
)
PACKAGE_AFTER = (
    '{"name": "app", "scripts": {"build": "tsc", "smoke": "node scripts/smoke-run.js"},'
    ' "devDependencies": {"check-types": "^1.0.0"}}'
)


class TestTargetCandidates:
    def test_an_added_make_rule_with_a_check_name_is_a_target_candidate(self):
        [candidate] = ci_wiring.candidate_checks(parse_unified_diff(VERIFY_TARGET_DIFF))
        assert candidate.path == "Makefile"
        assert candidate.target == ("make", "verify")
        assert candidate.label == "make verify"

    def test_a_test_named_make_target_counts(self):
        files = parse_unified_diff(_modified_diff(
            "Makefile", [], ["test-integration: build", "\tgo test -tags it ./..."],
        ))
        [candidate] = ci_wiring.candidate_checks(files)
        assert candidate.target == ("make", "test-integration")

    @pytest.mark.parametrize(
        "added",
        [
            ["release:", "\tgoreleaser"],
            ["latest:", "\tdocker pull app:latest"],
            ["\t./scripts/check-health.sh"],
            ["verify: GOFLAGS=-count=1"],
            [".PHONY: verify"],
            ["CHECK := ./scripts/x.sh"],
            ["%.check: %.in"],
        ],
    )
    def test_no_target_candidate(self, added):
        files = parse_unified_diff(_modified_diff("Makefile", [], added))
        assert ci_wiring.candidate_checks(files) == []

    def test_a_rule_already_on_a_removed_line_is_not_new(self):
        files = parse_unified_diff(_modified_diff(
            "Makefile", ["verify: lint"], ["verify: lint vet"],
        ))
        assert ci_wiring.candidate_checks(files) == []

    def test_a_rule_already_on_a_context_line_is_not_new(self):
        files = parse_unified_diff(
            "diff --git a/Makefile b/Makefile\n--- a/Makefile\n+++ b/Makefile\n"
            "@@ -1,2 +1,4 @@\n verify:\n+\t./scripts/check-health.sh\n+verify: vet\n \tpytest\n"
        )
        assert ci_wiring.candidate_checks(files) == []

    def test_a_nested_makefile_target_is_no_candidate(self):
        files = parse_unified_diff(_modified_diff("web/Makefile", [], ["verify:", "\ttrue"]))
        assert ci_wiring.candidate_checks(files) == []

    def test_a_new_package_json_script_is_a_target_candidate_when_head_confirms_it(self):
        files = parse_unified_diff(PACKAGE_DIFF)
        [candidate] = ci_wiring.candidate_checks(
            files, read={"package.json": PACKAGE_AFTER}.get,
        )
        assert candidate.path == "package.json"
        assert candidate.target == ("npm", "smoke")
        assert candidate.label == "npm run smoke"

    def test_a_package_json_key_outside_scripts_is_no_candidate(self):
        files = parse_unified_diff(_modified_diff(
            "package.json", [], ['    "check-types": "^1.0.0",'],
        ))
        assert ci_wiring.candidate_checks(files, read={"package.json": PACKAGE_AFTER}.get) == []

    def test_without_a_head_read_a_package_json_key_is_no_candidate(self):
        files = parse_unified_diff(PACKAGE_DIFF)
        assert ci_wiring.candidate_checks(files) == []
        assert ci_wiring.candidate_checks(files, read=lambda p: None) == []
        assert ci_wiring.candidate_checks(files, read=lambda p: "{not json") == []

    def test_a_raising_head_read_is_no_candidate_and_never_escapes(self):
        def read(path):
            raise OSError("boom")

        assert ci_wiring.candidate_checks(parse_unified_diff(PACKAGE_DIFF), read=read) == []


class TestTargetWiring:
    def _run(self, diff: str, repo: dict[str, str]):
        return ci_wiring.ci_wiring_findings(
            parse_unified_diff(diff), read=repo.get, listing=sorted(repo),
        )

    def test_a_verify_target_with_no_ci_caller_gives_one_finding(self):
        findings, record = self._run(
            VERIFY_TARGET_DIFF, {WORKFLOW_PATH: PYTEST_WORKFLOW, "Makefile": MAKEFILE_AFTER},
        )
        [finding] = findings
        assert finding.file == "Makefile"
        assert finding.line == 0
        assert finding.confidence == 1.0
        assert finding.title == "`make verify` is added but no CI job runs it"
        assert "adds the `verify` target to `Makefile`" in finding.body
        assert f"`{WORKFLOW_PATH}`" in finding.body
        assert heuristics.is_deterministic(finding)
        assert record["candidates"] == ["Makefile (make verify)"]
        assert record["triggered"] is True

    def test_ci_make_verify_wires_the_target(self):
        findings, record = self._run(
            VERIFY_TARGET_DIFF, {WORKFLOW_PATH: MAKE_WORKFLOW, "Makefile": MAKEFILE_AFTER},
        )
        assert findings == []
        assert record["triggered"] is False

    def test_a_ci_make_goal_reaching_the_target_as_a_prerequisite_wires_it(self):
        workflow = "jobs:\n  t:\n    steps:\n      - run: make ci\n"
        makefile = MAKEFILE_AFTER + "\nci: build verify\n"
        findings, _ = self._run(
            VERIFY_TARGET_DIFF, {WORKFLOW_PATH: workflow, "Makefile": makefile},
        )
        assert findings == []

    def test_a_recipe_calling_make_var_on_the_target_wires_it(self):
        workflow = "jobs:\n  t:\n    steps:\n      - run: make ci\n"
        wired = MAKEFILE_AFTER + "\nci:\n\t$(MAKE) lint\n\t${MAKE} verify\n"
        findings, _ = self._run(VERIFY_TARGET_DIFF, {WORKFLOW_PATH: workflow, "Makefile": wired})
        assert findings == []
        unwired = MAKEFILE_AFTER + "\nci:\n\t$(MAKE) build\n"
        findings, _ = self._run(VERIFY_TARGET_DIFF, {WORKFLOW_PATH: workflow, "Makefile": unwired})
        assert len(findings) == 1

    def test_a_ci_make_goal_not_reaching_the_target_stays_unwired(self):
        workflow = "jobs:\n  t:\n    steps:\n      - run: make build\n"
        findings, _ = self._run(
            VERIFY_TARGET_DIFF, {WORKFLOW_PATH: workflow, "Makefile": MAKEFILE_AFTER},
        )
        [finding] = findings
        assert "`Makefile` (followed from `make build`)" in finding.body

    def test_an_npm_target_with_no_ci_caller_gives_one_finding(self):
        findings, _ = self._run(
            PACKAGE_DIFF, {WORKFLOW_PATH: PYTEST_WORKFLOW, "package.json": PACKAGE_AFTER},
        )
        [finding] = findings
        assert finding.file == "package.json"
        assert finding.title == "`npm run smoke` is added but no CI job runs it"

    @pytest.mark.parametrize("call", ["npm run smoke", "yarn smoke", "pnpm run smoke"])
    def test_ci_running_the_npm_script_wires_it(self, call):
        workflow = f"jobs:\n  t:\n    steps:\n      - run: {call}\n"
        findings, _ = self._run(
            PACKAGE_DIFF, {WORKFLOW_PATH: workflow, "package.json": PACKAGE_AFTER},
        )
        assert findings == []

    def test_an_npm_script_calling_the_new_script_wires_it_one_hop(self):
        workflow = "jobs:\n  t:\n    steps:\n      - run: npm run ci\n"
        package = (
            '{"scripts": {"build": "tsc", "smoke": "node scripts/smoke-run.js",'
            ' "ci": "npm run build && npm run smoke"}}'
        )
        findings, _ = self._run(PACKAGE_DIFF, {WORKFLOW_PATH: workflow, "package.json": package})
        assert findings == []

    def test_the_head_package_json_is_read_once_for_candidate_and_hop(self):
        reads: list[str] = []
        repo = {
            WORKFLOW_PATH: "jobs:\n  t:\n    steps:\n      - run: npm run build\n",
            "package.json": PACKAGE_AFTER,
        }

        def read(path):
            reads.append(path)
            return repo.get(path)

        findings, _ = ci_wiring.ci_wiring_findings(
            parse_unified_diff(PACKAGE_DIFF), read=read, listing=sorted(repo),
        )
        assert len(findings) == 1
        assert reads.count("package.json") == 1
        assert "`package.json` (followed from `npm run build`)" in findings[0].body

    def test_the_spec_acceptance_end_to_end(self):
        unwired = CiForge(VERIFY_TARGET_DIFF, repo_files={
            WORKFLOW_PATH: PYTEST_WORKFLOW, "Makefile": MAKEFILE_AFTER,
        })
        res = orchestrate_review(
            unwired, REF, EMPTY_LLM, post=False, ci_wiring="on",
            ticket=_ticket("Add regression checks."),
        )
        [f] = _ci_findings(res)
        assert f.file == "Makefile"
        assert "`make verify`" in f.title

        wired = CiForge(VERIFY_TARGET_DIFF, repo_files={
            WORKFLOW_PATH: MAKE_WORKFLOW, "Makefile": MAKEFILE_AFTER,
        })
        res = orchestrate_review(
            wired, REF, EMPTY_LLM, post=False, ci_wiring="on",
            ticket=_ticket("Add regression checks."),
        )
        assert _ci_findings(res) == []
