"""Opt-in deterministic PR metadata rules (#70).

Three layers, each pinned on its own:

* the pure checks in ``prxref.metadata_rules`` — branch patterns (the PR's
  type from labels or title prefix against its source branch), commit
  references (every non-merge commit subject) and area globs (the diff's
  paths against a cap) — constructed by hand, no forge, no LLM, no I/O;
* the config surface — five flat keys, ``FILE_KEYS`` membership, the
  choice on ``metadata_rules``, and the validation that makes a malformed
  pattern or pair exit 2 naming ``<file>: <key>``;
* the orchestrator wiring — the run-record stamp that exists only when the
  feature is on, the commit-list skip when the forge has none, and the
  empty-diff exit that still reports a branch violation;
* the notes contract (OD8) — a violation is a ``MetadataNote`` rendered in
  the summary's ``PR metadata`` section, never a finding: no inline post
  even with stable ids, no severity cap, no ``PRXREF_FAIL_ON`` exit.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from prxref import config, metadata_rules, orchestrator
from prxref.cli import _build_json_result, _fail_on_exit, _print_summary
from prxref.forges import bitbucket, gitlab
from prxref.forges.base import CommitData, FeedReadError, PRData
from prxref.llm import ConfigError
from prxref.triage import FileDiff
from tests.test_orchestrator import REF, FakeForge, FakeLLM, _added_file_diff, make_pr


@pytest.fixture(autouse=True)
def _contract_stubs(contract_stubs):
    """Pin the reviewer contract for every orchestrator test in this module.

    The same opt-in wrapper ``tests/test_orchestrator.py`` uses: the chunk
    and sweep stubs answer no findings, so every finding below comes from
    the metadata checks alone.
    """
    return None

# --- fixtures ----------------------------------------------------------------


def _pr(
    title: str = "Add widget",
    branch: str = "feature/widget",
    labels: object = None,
) -> PRData:
    raw: dict = {}
    if labels is not None:
        raw["labels"] = labels
    return PRData(
        title=title, description="", author="alice",
        source_branch=branch, target_branch="main",
        source_sha="a" * 40, target_sha="b" * 40, raw=raw,
    )


def _files(*paths: str) -> list[FileDiff]:
    return [FileDiff(path=p, old_path=p, new_path=p) for p in paths]


def _commit(sha: str, subject: str, parents: int = 1) -> CommitData:
    return CommitData(sha=sha, subject=subject, parent_count=parents)


# --- the branch-pattern check ------------------------------------------------


class TestBranchPatternCheck:
    def test_a_matching_branch_passes(self):
        findings, status = metadata_rules.branch_pattern_check(
            _pr(title="fix: handle empty input", branch="fix/42-empty"),
            {"fix": r"fix/.*"},
        )
        assert findings == []
        assert status == "pass"

    def test_a_mismatched_branch_is_one_note(self):
        notes, status = metadata_rules.branch_pattern_check(
            _pr(title="fix: handle empty input", branch="bugfix/42-empty"),
            {"fix": r"fix/\d+.*"},
        )
        assert status == "fail"
        [note] = notes
        assert note.check == "branch_pattern"
        assert "bugfix/42-empty" in note.title
        assert "the title prefix" in note.detail

    def test_matching_is_fullmatch_so_team_anchors_are_harmless(self):
        ok, _ = metadata_rules.branch_pattern_check(
            _pr(title="fix: x", branch="fix/1"), {"fix": r"^fix/\d+$"},
        )
        assert ok == []
        bad, status = metadata_rules.branch_pattern_check(
            _pr(title="fix: x", branch="prefix-fix/1"), {"fix": r"^fix/\d+$"},
        )
        assert status == "fail"
        assert len(bad) == 1

    def test_the_type_comes_from_a_github_shaped_label_first(self):
        findings, _ = metadata_rules.branch_pattern_check(
            _pr(title="Add widget", branch="fix/widget",
                labels=[{"name": "Fix"}, {"name": "size/L"}]),
            {"fix": r"fix/.*"},
        )
        assert findings == []

    def test_the_type_comes_from_a_gitlab_shaped_label(self):
        findings, status = metadata_rules.branch_pattern_check(
            _pr(title="irrelevant", branch="fix/1", labels=["fix"]),
            {"fix": r"fix/.*"},
        )
        assert (findings, status) == ([], "pass")

    def test_a_label_naming_no_configured_type_falls_to_the_title(self):
        findings, status = metadata_rules.branch_pattern_check(
            _pr(title="feature: new thing", branch="feature/thing",
                labels=["priority: high"]),
            {"feature": r"feature/.*"},
        )
        assert (findings, status) == ([], "pass")

    def test_the_title_prefix_strips_a_scope(self):
        findings, status = metadata_rules.branch_pattern_check(
            _pr(title="feature(api): new endpoint", branch="feature/api"),
            {"feature": r"feature/.*"},
        )
        assert (findings, status) == ([], "pass")

    def test_a_title_without_a_conventional_prefix_resolves_no_type(self):
        findings, status = metadata_rules.branch_pattern_check(
            _pr(title="Update README", branch="whatever"),
            {"fix": r"fix/.*"},
        )
        assert (findings, status) == ([], "skipped: no PR type")

    def test_a_type_no_entry_covers_is_skipped_not_a_violation(self):
        findings, status = metadata_rules.branch_pattern_check(
            _pr(title="chore: tidy", branch="random"),
            {"fix": r"fix/.*", "feature": r"feature/.*"},
        )
        assert (findings, status) == ([], "skipped: no pattern for type 'chore'")

    def test_no_patterns_configured_skips(self):
        findings, status = metadata_rules.branch_pattern_check(_pr(), {})
        assert (findings, status) == ([], "skipped: no branch patterns")

    def test_no_source_branch_skips(self):
        findings, status = metadata_rules.branch_pattern_check(
            _pr(branch=""), {"fix": r"fix/.*"},
        )
        assert (findings, status) == ([], "skipped: no source branch")

    def test_the_type_lookup_is_case_insensitive(self):
        findings, status = metadata_rules.branch_pattern_check(
            _pr(title="FIX: uppercase type", branch="fix/1"),
            {"Fix": r"fix/.*"},
        )
        assert (findings, status) == ([], "pass")


# --- the commit-reference check -----------------------------------------------


class TestCommitReferenceCheck:
    def test_subjects_containing_the_reference_pass(self):
        commits = [_commit("a" * 40, "ACME-12 fix the loop"),
                   _commit("b" * 40, "ACME-13: tidy")]
        findings, status = metadata_rules.commit_reference_check(
            commits, r"ACME-\d+",
        )
        assert (findings, status) == ([], "pass")

    def test_a_missing_reference_is_one_note_per_commit(self):
        commits = [
            _commit("a" * 40, "ACME-12 fix the loop"),
            _commit("1234567890", "fix the loop"),
            _commit("c" * 40, "tidy"),
        ]
        findings, status = metadata_rules.commit_reference_check(
            commits, r"ACME-\d+",
        )
        assert status == "fail"
        assert [f.title for f in findings] == [
            "Commit 1234567890 subject has no 'ACME-\\d+' reference",
            "Commit cccccccccc subject has no 'ACME-\\d+' reference",
        ]
        assert all(f.check == "commit_reference" for f in findings)
        assert '"fix the loop"' in findings[0].detail

    def test_the_match_is_a_search_anywhere_in_the_subject(self):
        findings, status = metadata_rules.commit_reference_check(
            [_commit("a" * 40, "fix PROJ-7 the loop")], r"PROJ-\d+",
        )
        assert (findings, status) == ([], "pass")

    def test_merge_commits_are_exempt(self):
        findings, status = metadata_rules.commit_reference_check(
            [_commit("a" * 40, "Merge branch 'x'", parents=2)], r"ACME-\d+",
        )
        assert (findings, status) == ([], "pass")

    def test_no_commit_list_skips_with_the_reason(self):
        findings, status = metadata_rules.commit_reference_check(
            None, r"ACME-\d+", skip_reason="no commit source",
        )
        assert (findings, status) == ([], "skipped: no commit source")

    def test_no_pattern_configured_skips(self):
        findings, status = metadata_rules.commit_reference_check(
            [_commit("a" * 40, "anything")], "",
        )
        assert (findings, status) == ([], "skipped: no commit reference pattern")

    def test_only_the_first_line_of_the_message_is_the_subject(self):
        # The caller hands over CommitData with the subject already cut to
        # the first line; a multi-line body never reaches this check.
        commits = [_commit("a" * 40, "oneline ACME-1")]
        findings, status = metadata_rules.commit_reference_check(commits, r"ACME-\d+")
        assert (findings, status) == ([], "pass")


# --- the area check -----------------------------------------------------------


class TestAreaCheck:
    def test_within_the_cap_passes(self):
        findings, status = metadata_rules.area_check(
            _files("src/a.py", "web/b.ts"), ["backend=src/**", "frontend=web/**"], 2,
        )
        assert (findings, status) == ([], "pass")

    def test_over_the_cap_is_one_note_listing_areas(self):
        findings, status = metadata_rules.area_check(
            _files("src/a.py", "web/b.ts", "infra/c.tf"),
            ["backend=src/**", "frontend=web/**", "infra=infra/**"], 2,
        )
        assert status == "fail"
        [note] = findings
        assert note.check == "area_globs"
        assert "3 areas (max 2)" in note.title
        assert "backend (1 file(s))" in note.detail
        assert "infra (1 file(s))" in note.detail

    def test_a_path_matching_no_area_is_ignored(self):
        findings, status = metadata_rules.area_check(
            _files("docs/guide.md", "README.md"), ["backend=src/**"], 0,
        )
        assert (findings, status) == ([], "pass")

    def test_entries_sharing_a_name_form_one_area(self):
        findings, status = metadata_rules.area_check(
            _files("src/a.py", "lib/b.py", "web/c.ts"),
            ["backend=src/**", "backend=lib/**", "frontend=web/**"], 1,
        )
        assert status == "fail"
        [finding] = findings
        assert "backend (2 file(s))" in finding.detail

    def test_a_path_counts_in_every_matching_area_order_independently(self):
        for globs in (
            ["a=x/**", "b=x/**"],
            ["b=x/**", "a=x/**"],
        ):
            findings, status = metadata_rules.area_check(
                _files("x/f"), globs, 1,
            )
            assert status == "fail"
            assert len(findings) == 1

    def test_no_globs_configured_skips(self):
        findings, status = metadata_rules.area_check(_files("src/a.py"), [], 1)
        assert (findings, status) == ([], "skipped: no area globs")

    def test_zero_is_a_legal_cap(self):
        findings, status = metadata_rules.area_check(
            _files("src/a.py"), ["backend=src/**"], 0,
        )
        assert status == "fail"


# --- run_metadata_checks: the one orchestrator-facing entry point -------------


class TestRunMetadataChecks:
    def test_the_stamp_covers_the_three_checks_in_a_fixed_order(self):
        _, stamp = metadata_rules.run_metadata_checks(
            _pr(), _files("src/a.py"), None,
        )
        assert list(stamp) == ["branch_pattern", "commit_reference", "area_globs"]

    def test_notes_come_back_in_check_order(self):
        notes, _ = metadata_rules.run_metadata_checks(
            _pr(title="fix: x", branch="wrong/name"),
            _files("src/a.py", "web/b.ts"),
            [_commit("c" * 40, "no ref")],
            branch_patterns=["fix=^fix/"], commit_reference="ACME-\\d+",
            area_globs=["backend=src/**", "frontend=web/**"], max_areas_per_pr=1,
        )
        assert [n.check for n in notes] == [
            "branch_pattern", "commit_reference", "area_globs",
        ]

    def test_an_empty_diff_still_checks_the_branch(self):
        notes, _ = metadata_rules.run_metadata_checks(
            _pr(title="fix: x", branch="wrong/name"), [],
            branch_patterns=["fix=^fix/"],
        )
        [note] = notes
        assert note.as_dict() == {
            "check": note.check, "title": note.title, "detail": note.detail,
        }

    def test_type_regex_strings_are_parsed_not_trusted(self):
        findings, stamp = metadata_rules.run_metadata_checks(
            _pr(title="fix: x", branch="fix/1"), [],
            branch_patterns=["fix=fix/.*"],
        )
        assert (findings, stamp["branch_pattern"]) == ([], "pass")


# --- the config surface -------------------------------------------------------


class TestConfigKeys:
    def test_the_five_keys_default_off_and_empty(self):
        assert config._DEFAULTS["metadata_rules"] == "off"
        assert config._DEFAULTS["branch_patterns"] == []
        assert config._DEFAULTS["commit_reference"] == ""
        assert config._DEFAULTS["area_globs"] == []
        assert config._DEFAULTS["max_areas_per_pr"] == 2

    def test_the_keys_are_classified_as_file_keys(self):
        assert {
            "metadata_rules", "branch_patterns", "commit_reference",
            "area_globs", "max_areas_per_pr",
        } <= config.FILE_KEYS

    def test_the_types_are_declared(self):
        assert "max_areas_per_pr" in config._INT_KEYS
        assert {"branch_patterns", "area_globs"} <= config._LIST_KEYS
        assert set(config._CHOICE_KEYS["metadata_rules"]) == {"off", "on"}

    def test_the_cap_range_allows_zero(self):
        assert config._RANGES["max_areas_per_pr"] == config._Range(0, low_inclusive=True)
        assert config._RANGES["max_areas_per_pr"].accepts(0) is True
        assert config._RANGES["max_areas_per_pr"].accepts(-1) is False

    def test_an_unknown_value_for_the_switch_is_a_config_error(self, monkeypatch):
        monkeypatch.setenv("PRXREF_METADATA_RULES", "maybe")
        with pytest.raises(ConfigError, match=r"^PRXREF_METADATA_RULES: must be one of"):
            config.load_config()


class TestConfigFileValidation:
    @pytest.fixture
    def repo(self, tmp_path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.chdir(root)
        monkeypatch.delenv(config.CONFIG_FILE_ENV, raising=False)
        return root

    def _write(self, repo, text):
        path = repo / ".prxref.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_metadata_section_is_flat_not_a_table(self, repo):
        path = self._write(repo, "[metadata]\nrules = \"on\"\n")
        with pytest.raises(ConfigError, match=r"is a table, but the config file is flat"):
            config.read_config_file(path)

    def test_the_file_sets_all_five_keys(self, repo):
        path = self._write(repo, "\n".join([
            'metadata_rules = "on"',
            'branch_patterns = ["fix=^fix/"]',
            'commit_reference = "ACME-[0-9]+"',
            'area_globs = ["backend=src/**"]',
            "max_areas_per_pr = 3",
        ]) + "\n")
        cfg = config.load_config(config_file=path)
        assert cfg["metadata_rules"] == "on"
        assert cfg["branch_patterns"] == ["fix=^fix/"]
        assert cfg["commit_reference"] == "ACME-[0-9]+"
        assert cfg["area_globs"] == ["backend=src/**"]
        assert cfg["max_areas_per_pr"] == 3

    def test_a_bad_branch_regex_names_the_file_and_key(self, repo):
        path = self._write(repo, 'branch_patterns = ["fix=**("]\n')
        with pytest.raises(ConfigError) as info:
            config.load_config(config_file=path)
        message = str(info.value)
        assert message.startswith(".prxref.toml: branch_patterns:")
        assert "regex does not compile" in message

    def test_a_branch_entry_without_an_equals_is_rejected(self, repo):
        path = self._write(repo, 'branch_patterns = ["^fix/"]\n')
        with pytest.raises(ConfigError, match=r"^\.prxref\.toml: branch_patterns:"):
            config.load_config(config_file=path)

    def test_an_empty_side_of_a_pair_is_rejected(self, repo):
        path = self._write(repo, 'area_globs = ["=src/**"]\n')
        with pytest.raises(ConfigError) as info:
            config.load_config(config_file=path)
        assert "both sides non-empty" in str(info.value)

    def test_a_bad_commit_reference_is_rejected(self, repo):
        path = self._write(repo, 'commit_reference = "ACME-["\n')
        with pytest.raises(ConfigError, match=r"^\.prxref\.toml: commit_reference:"):
            config.load_config(config_file=path)

    def test_the_cap_keeps_its_range_check(self, repo):
        path = self._write(repo, "max_areas_per_pr = -1\n")
        with pytest.raises(ConfigError, match=r"^\.prxref\.toml: max_areas_per_pr:"):
            config.load_config(config_file=path)

    def test_the_environment_splits_the_list_keys(self, monkeypatch):
        monkeypatch.setenv("PRXREF_BRANCH_PATTERNS", "fix=^fix/, feature=^feature/")
        cfg = config.load_config()
        assert cfg["branch_patterns"] == ["fix=^fix/", "feature=^feature/"]


# --- the orchestrator wiring --------------------------------------------------


class _CommitsForge(FakeForge):
    """FakeForge plus a counting ``get_commits``."""

    def __init__(self, commits, **kwargs):
        super().__init__(**kwargs)
        self._commits = commits
        self.commit_calls = 0

    def get_commits(self, ref, *, base_sha="", head_sha=""):
        self.commit_calls += 1
        if isinstance(self._commits, Exception):
            raise self._commits
        return list(self._commits)


NO_FINDINGS = FakeLLM(findings_by_path={})


class TestOrchestratorWiring:
    def test_off_stamps_no_metadata_rules_key_and_posts_as_before(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS,
            branch_patterns=["fix=^fix/"], commit_reference="ACME-\\d+",
        )
        assert "metadata_rules" not in res
        assert res["verdict"] == "Approved"
        assert forge.inline_batches or forge.summaries

    def test_on_records_the_stamp_and_the_note(self):
        forge = FakeForge(
            pr=make_pr(title="fix: handle empty input"),
            diff=_added_file_diff("src/app.py", 20),
        )
        res = orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS,
            metadata_rules="on", branch_patterns=["fix=^fix/"],
        )
        # make_pr's branch is feature/widget, which matches no pattern the
        # resolved type 'fix' owns -> one violation.
        assert res["metadata_rules"]["branch_pattern"] == "fail"
        assert res["metadata_rules"]["commit_reference"] == "skipped: no commit reference pattern"
        assert res["metadata_rules"]["area_globs"] == "skipped: no area globs"
        [note] = res["metadata_rules"]["violations"]
        assert note["check"] == "branch_pattern"
        assert res["findings_active"] == []
        assert res["verdict"] == "Approved"

    def test_a_passing_configuration_stamps_pass(self):
        forge = FakeForge(
            pr=PRData(
                title="fix: handle empty input", description="", author="alice",
                source_branch="fix/42", target_branch="main",
                source_sha="a" * 40, target_sha="b" * 40, raw={},
            ),
            diff=_added_file_diff("src/app.py", 20),
        )
        res = orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS,
            metadata_rules="on", branch_patterns=["fix=fix/.*"],
        )
        assert res["metadata_rules"]["branch_pattern"] == "pass"
        assert res["findings_active"] == []

    def test_the_summary_lists_the_note_the_inline_batch_never_carries_it(self):
        forge = FakeForge(
            pr=make_pr(title="feature: widget"),
            diff=_added_file_diff("src/app.py", 20),
        )
        res = orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS,
            metadata_rules="on", branch_patterns=["feature=nomatch/.*"],
        )
        assert any(
            "Branch 'feature/widget' does not match" in n["title"]
            for n in res["metadata_rules"]["violations"]
        )
        assert forge.summaries and "does not match the 'feature' pattern" in forge.summaries[0]
        for batch in forge.inline_batches:
            for comment in batch:
                assert "does not match the 'feature' pattern" not in comment.body

    def test_only_metadata_notes_never_post_an_empty_inline_batch(self):
        forge = FakeForge(
            pr=make_pr(title="feature: widget"),
            diff=_added_file_diff("src/app.py", 20),
        )
        res = orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS,
            metadata_rules="on", branch_patterns=["feature=nomatch/.*"],
        )
        assert res["metadata_rules"]["violations"]
        assert forge.inline_batches == []
        assert forge.summaries

    def test_the_commit_check_reads_the_forge_commit_list(self):
        forge = _CommitsForge(
            [_commit("a" * 40, "ACME-1 ok"), _commit("1234567890", "no ref")],
            diff=_added_file_diff("src/app.py", 20),
        )
        res = orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS,
            metadata_rules="on", commit_reference="ACME-\\d+",
        )
        assert forge.commit_calls == 1
        assert res["metadata_rules"]["commit_reference"] == "fail"
        assert any(
            "Commit 1234567890 subject has no" in n["title"]
            for n in res["metadata_rules"]["violations"]
        )

    def test_a_failing_commit_list_skips_the_check_not_the_review(self):
        forge = _CommitsForge(
            RuntimeError("boom"),
            diff=_added_file_diff("src/app.py", 20),
        )
        res = orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS,
            metadata_rules="on", commit_reference="ACME-\\d+",
        )
        assert res["metadata_rules"]["commit_reference"] == (
            "skipped: commit source failed: RuntimeError"
        )
        assert res["verdict"] == "Approved"

    def test_a_forge_without_get_commits_skips_with_the_reason(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        assert getattr(forge, "get_commits", None) is None
        res = orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS,
            metadata_rules="on", commit_reference="ACME-\\d+",
        )
        assert res["metadata_rules"]["commit_reference"] == "skipped: no commit source"

    def test_no_commit_list_is_fetched_when_the_check_is_not_configured(self):
        forge = _CommitsForge(
            [_commit("a" * 40, "no ref")],
            diff=_added_file_diff("src/app.py", 20),
        )
        orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS, metadata_rules="on",
        )
        assert forge.commit_calls == 0

    def test_an_empty_diff_still_reports_the_branch_violation(self):
        forge = FakeForge(
            pr=make_pr(title="fix: handle empty input"), diff="",
        )
        res = orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS,
            metadata_rules="on", branch_patterns=["fix=^fix/"],
        )
        assert res["chunk_count"] == 0
        assert res["metadata_rules"]["branch_pattern"] == "fail"
        assert res["findings_active"] == []
        assert forge.summaries and "does not match the 'fix' pattern" in forge.summaries[0]

    def test_the_area_check_fires_over_the_cap(self):
        forge = FakeForge(diff="".join(
            _added_file_diff(p, 5) for p in ("src/a.py", "web/b.ts")
        ))
        res = orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS,
            metadata_rules="on", area_globs=["backend=src/**", "frontend=web/**"],
            max_areas_per_pr=1,
        )
        assert res["metadata_rules"]["area_globs"] == "fail"
        assert any(
            "2 areas (max 1)" in n["title"] for n in res["metadata_rules"]["violations"]
        )


# --- --format json ------------------------------------------------------------


class TestJsonPayload:
    def test_off_still_emits_the_key_as_null(self):
        """The release-wide rule (#38 and every key since): the JSON key is
        always present, even though the off run-record omits it."""
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrator.orchestrate_review(forge, REF, NO_FINDINGS)
        assert "metadata_rules" not in res
        payload = _build_json_result(res)
        assert payload["metadata_rules"] is None
        assert list(payload)[list(payload).index("degraded") + 1] == "metadata_rules"

    def test_on_carries_the_stamp_verbatim(self):
        forge = FakeForge(
            pr=make_pr(title="fix: handle empty input"),
            diff=_added_file_diff("src/app.py", 20),
        )
        res = orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS,
            metadata_rules="on", branch_patterns=["fix=^fix/"],
        )
        payload = _build_json_result(res)
        assert payload["metadata_rules"] == res["metadata_rules"]
        assert payload["metadata_rules"]["branch_pattern"] == "fail"


# --- violations are summary notes, never findings (OD8) ----------------------


def _branch_violation_run(**kwargs):
    forge = FakeForge(
        pr=make_pr(title="fix: handle empty input"),
        diff=_added_file_diff("src/app.py", 20),
    )
    res = orchestrator.orchestrate_review(
        forge, REF, NO_FINDINGS,
        metadata_rules="on", branch_patterns=["fix=^fix/"], **kwargs,
    )
    return forge, res


class TestViolationsAreSummaryNotes:
    def test_stable_ids_on_never_posts_a_metadata_violation_inline(self):
        forge, res = _branch_violation_run(stable_ids=True)
        assert res["findings_active"] == []
        assert forge.inline_batches == []
        assert forge.summaries
        assert "does not match the 'fix' pattern" in forge.summaries[-1]

    def test_severity_cap_zero_keeps_metadata_violation_in_summary(self):
        forge, res = _branch_violation_run(max_warning_findings=0)
        assert res["findings_dropped"] == []
        assert "does not match the 'fix' pattern" in forge.summaries[-1]

    def test_fail_on_any_ignores_metadata_violations(self):
        _, res = _branch_violation_run()
        assert res["metadata_rules"]["branch_pattern"] == "fail"
        assert _fail_on_exit(res, "any") == (0, None)
        assert _fail_on_exit(res, "error") == (0, None)

    def test_the_summary_carries_a_pr_metadata_section(self):
        forge, _ = _branch_violation_run()
        summary = forge.summaries[-1]
        assert "**PR metadata**" in summary
        assert "No findings" in summary
        section = summary[summary.index("**PR metadata**"):]
        assert "Branch 'feature/widget' does not match the 'fix' pattern" in section

    def test_the_section_sits_above_the_attribution_footer(self):
        forge, _ = _branch_violation_run()
        summary = forge.summaries[-1]
        assert summary.index("**PR metadata**") < summary.rindex("Reviewed by prxref")

    def test_the_section_sits_above_a_ruled_footer(self):
        section = orchestrator._metadata_section([metadata_rules.MetadataNote(
            check="area_globs", title="PR touches 3 areas", detail="Split it.",
        )])
        rendered = orchestrator._render_summary(
            _pr(), [], "Approved", [], "m", 0, 0, 0,
            summary_template="{verdict}\n\n{findings}\n\n---\n\n{attribution}",
            metadata_section=section,
        )
        assert rendered.index("**PR metadata**") < rendered.index("\n---\n")
        assert "- PR touches 3 areas — Split it." in rendered

    def test_no_notes_render_no_section(self):
        assert orchestrator._metadata_section([]) == ""

    def test_the_record_lists_each_violation(self):
        _, res = _branch_violation_run()
        [note] = res["metadata_rules"]["violations"]
        assert note["check"] == "branch_pattern"
        assert "does not match the 'fix' pattern" in note["title"]
        assert "feature/widget" in note["detail"]
        assert _build_json_result(res)["metadata_rules"]["violations"] == [note]

    def test_a_passing_run_renders_no_section(self):
        forge = FakeForge(
            pr=PRData(
                title="fix: handle empty input", description="", author="alice",
                source_branch="fix/42", target_branch="main",
                source_sha="a" * 40, target_sha="b" * 40, raw={},
            ),
            diff=_added_file_diff("src/app.py", 20),
        )
        res = orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS,
            metadata_rules="on", branch_patterns=["fix=fix/.*"],
        )
        assert res["metadata_rules"]["violations"] == []
        assert "PR metadata" not in forge.summaries[-1]

    def test_off_summary_is_byte_identical(self):
        on = FakeForge(diff=_added_file_diff("src/app.py", 20))
        off = FakeForge(diff=_added_file_diff("src/app.py", 20))
        orchestrator.orchestrate_review(on, REF, NO_FINDINGS, metadata_rules="on")
        orchestrator.orchestrate_review(off, REF, NO_FINDINGS)
        strip = [s.split("Reviewed by prxref")[0] for s in (on.summaries[-1], off.summaries[-1])]
        assert strip[0] == strip[1]

    def test_an_empty_diff_summary_carries_the_section(self):
        forge = FakeForge(pr=make_pr(title="fix: handle empty input"), diff="")
        res = orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS,
            metadata_rules="on", branch_patterns=["fix=^fix/"],
        )
        assert res["findings_active"] == []
        assert "**PR metadata**" in forge.summaries[-1]

    def test_notes_never_reach_the_verdict_or_the_counts(self):
        _, res = _branch_violation_run()
        assert res["verdict"] == "Approved"
        assert res["findings_active"] == []

    def test_the_text_printer_names_each_violation(self, capsys):
        _, res = _branch_violation_run()
        _print_summary(res, 1.0, verbose=False)
        out = capsys.readouterr().out
        assert "pr metadata: Branch 'feature/widget' does not match the 'fix' pattern" in out

    def test_the_checks_return_notes_not_findings(self):
        notes, _ = metadata_rules.run_metadata_checks(
            _pr(title="fix: x", branch="wrong/name"), _files("src/a.py"),
            branch_patterns=["fix=^fix/"],
        )
        [note] = notes
        assert isinstance(note, metadata_rules.MetadataNote)
        assert note.check == "branch_pattern"


class TestSkippedChecksAreNamed:
    def _skipped_run(self):
        forge = FakeForge(
            pr=PRData(
                title="update things", description="", author="alice",
                source_branch="work/42", target_branch="main",
                source_sha="a" * 40, target_sha="b" * 40, raw={},
            ),
            diff=_added_file_diff("src/app.py", 20),
        )
        res = orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS,
            metadata_rules="on", branch_patterns=["fix=fix/.*"],
            commit_reference="ACME-\\d+",
        )
        return forge, res

    def test_a_skipped_configured_check_is_named_in_the_summary(self):
        forge, _ = self._skipped_run()
        summary = forge.summaries[-1]
        assert "**PR metadata**" in summary
        assert "no commit source" in summary
        assert "no PR type" in summary
        assert "area" not in summary.split("**PR metadata**")[1].lower()

    def test_a_missing_commit_source_logs_a_warning(self, caplog):
        with caplog.at_level("WARNING"):
            self._skipped_run()
        assert any("no commit source" in r.getMessage() for r in caplog.records)

    def test_a_passing_run_prints_no_metadata_section(self):
        forge = FakeForge(
            pr=PRData(
                title="fix: handle empty input", description="", author="alice",
                source_branch="fix/42", target_branch="main",
                source_sha="a" * 40, target_sha="b" * 40, raw={},
            ),
            diff=_added_file_diff("src/app.py", 20),
        )
        orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS,
            metadata_rules="on", branch_patterns=["fix=fix/.*"],
        )
        assert "PR metadata" not in forge.summaries[-1]


# --- forge commit listings: GitLab and Bitbucket Cloud ------------------------


def _json_response(body, status: int = 200):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.headers = {}
    resp.json.return_value = body
    resp.raise_for_status.side_effect = (
        None if resp.ok else requests.HTTPError(response=resp)
    )
    return resp


def _session(get):
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = get
    return session


def _recording_session(pages, calls):
    def get(url, **kwargs):
        calls.append((url, kwargs.get("params")))
        return _json_response(pages[len(calls) - 1])

    return _session(get)


GL_URL = "https://gitlab.com/acme/sub/api/-/merge_requests/7"
BB_URL = "https://bitbucket.org/acme/api/pull-requests/42"


def _gl_commit(sha: str, message: str, parents: int = 1) -> dict:
    return {
        "id": sha, "short_id": sha[:8], "title": message.splitlines()[0],
        "message": message, "parent_ids": [f"p{i}" for i in range(parents)],
    }


def _bb_commit(sha: str, message: str, parents: int = 1) -> dict:
    return {
        "hash": sha, "message": message,
        "parents": [{"hash": f"p{i}", "type": "commit"} for i in range(parents)],
    }


class TestGitLabGetCommits:
    def test_subjects_and_parent_counts_come_back_oldest_first(self):
        calls: list = []
        newest_first = [
            _gl_commit("c" * 40, "Merge branch 'main' into feature\n", parents=2),
            _gl_commit("b" * 40, "ACME-2 second\n\nbody text"),
            _gl_commit("a" * 40, "no ref here\nACME-9 only in the body"),
        ]
        forge = gitlab.ForgeImpl(session=_recording_session([newest_first], calls))
        ref = gitlab.ForgeImpl.parse_pr_url(GL_URL)
        commits = forge.get_commits(ref, base_sha="b" * 40, head_sha="a" * 40)
        assert commits == [
            CommitData(sha="a" * 40, subject="no ref here", parent_count=1),
            CommitData(sha="b" * 40, subject="ACME-2 second", parent_count=1),
            CommitData(
                sha="c" * 40, subject="Merge branch 'main' into feature",
                parent_count=2,
            ),
        ]
        url, params = calls[0]
        assert url == (
            "https://gitlab.com/api/v4/projects/acme%2Fsub%2Fapi"
            "/merge_requests/7/commits"
        )
        assert params["per_page"] == 100 and params["page"] == 1

    def test_the_listing_is_paged_to_its_end(self):
        calls: list = []
        first = [_gl_commit(f"{i:040d}", f"ACME-{i} c") for i in range(100, 0, -1)]
        second = [_gl_commit("0" * 40, "root commit")]
        forge = gitlab.ForgeImpl(session=_recording_session([first, second], calls))
        commits = forge.get_commits(gitlab.ForgeImpl.parse_pr_url(GL_URL))
        assert [p["page"] for _, p in calls] == [1, 2]
        assert len(commits) == 101
        assert commits[0].subject == "root commit"
        assert commits[-1].subject == "ACME-100 c"

    def test_a_failed_page_raises_rather_than_returning_short(self):
        forge = gitlab.ForgeImpl(
            session=_session(lambda url, **kw: _json_response({}, 500)),
        )
        with pytest.raises(FeedReadError):
            forge.get_commits(gitlab.ForgeImpl.parse_pr_url(GL_URL))


class TestBitbucketCloudGetCommits:
    def test_subjects_and_parent_counts_come_back_oldest_first(self):
        calls: list = []
        page = {"values": [
            _bb_commit("c" * 40, "Merged main into feature\n", parents=2),
            _bb_commit("b" * 40, "ACME-2 second\n\nbody"),
            _bb_commit("a" * 40, "no ref here"),
        ]}
        forge = bitbucket.ForgeImpl(session=_recording_session([page], calls))
        ref = bitbucket.ForgeImpl.parse_pr_url(BB_URL)
        commits = forge.get_commits(ref, base_sha="b" * 40, head_sha="a" * 40)
        assert [(c.sha[0], c.subject, c.parent_count) for c in commits] == [
            ("a", "no ref here", 1),
            ("b", "ACME-2 second", 1),
            ("c", "Merged main into feature", 2),
        ]
        url, params = calls[0]
        assert url == (
            "https://api.bitbucket.org/2.0/repositories/acme/api"
            "/pullrequests/42/commits"
        )
        assert params == {"pagelen": 100}

    def test_the_listing_follows_next_to_its_end(self):
        calls: list = []
        next_url = "https://api.bitbucket.org/2.0/next-page-token"
        pages = [
            {"values": [_bb_commit("2" * 40, "ACME-2 newer")], "next": next_url},
            {"values": [_bb_commit("1" * 40, "ACME-1 older")]},
        ]
        forge = bitbucket.ForgeImpl(session=_recording_session(pages, calls))
        commits = forge.get_commits(bitbucket.ForgeImpl.parse_pr_url(BB_URL))
        assert calls[1] == (next_url, None)
        assert [c.subject for c in commits] == ["ACME-1 older", "ACME-2 newer"]

    def test_a_failed_page_raises_rather_than_returning_short(self):
        forge = bitbucket.ForgeImpl(
            session=_session(lambda url, **kw: _json_response({}, 403)),
        )
        with pytest.raises(FeedReadError):
            forge.get_commits(bitbucket.ForgeImpl.parse_pr_url(BB_URL))


def _gitlab_adapter():
    page = [
        _gl_commit("2" * 40, "Merge branch 'main' into feature", parents=2),
        _gl_commit("1" * 40, "no ref here"),
        _gl_commit("0" * 40, "ACME-1 fine"),
    ]
    forge = gitlab.ForgeImpl(session=_session(lambda url, **kw: _json_response(page)))
    return forge, gitlab.ForgeImpl.parse_pr_url(GL_URL)


def _bitbucket_adapter():
    page = {"values": [
        _bb_commit("2" * 40, "Merged main into feature", parents=2),
        _bb_commit("1" * 40, "no ref here"),
        _bb_commit("0" * 40, "ACME-1 fine"),
    ]}
    forge = bitbucket.ForgeImpl(
        session=_session(lambda url, **kw: _json_response(page)),
    )
    return forge, bitbucket.ForgeImpl.parse_pr_url(BB_URL)


class _AdapterCommitsForge(FakeForge):
    """FakeForge whose ``get_commits`` is a real adapter's over a mocked session."""

    def __init__(self, adapter, adapter_ref, **kwargs):
        super().__init__(**kwargs)
        self._adapter = adapter
        self._adapter_ref = adapter_ref

    def get_commits(self, ref, *, base_sha="", head_sha=""):
        return self._adapter.get_commits(
            self._adapter_ref, base_sha=base_sha, head_sha=head_sha,
        )


class TestEveryCommitSourceFeedsTheCheck:
    @pytest.mark.parametrize(
        "build", [_gitlab_adapter, _bitbucket_adapter], ids=["gitlab", "bitbucket"],
    )
    def test_the_commit_check_fails_not_skips(self, build):
        adapter, adapter_ref = build()
        forge = _AdapterCommitsForge(
            adapter, adapter_ref, diff=_added_file_diff("src/app.py", 20),
        )
        res = orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS,
            metadata_rules="on", commit_reference="ACME-\\d+",
        )
        assert res["metadata_rules"]["commit_reference"] == "fail"
        titles = [n["title"] for n in res["metadata_rules"]["violations"]]
        assert len(titles) == 1
        assert "1111111111" in titles[0]
