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
  feature is on, the findings' summary-only contract (the summary lists
  them, the inline batch never carries them), the commit-list skip when
  the forge has none, and the empty-diff exit that keeps a branch
  violation with no diff path to anchor on.

``heuristics.is_deterministic`` is asserted on every finding kind: the
body suffix is the whole mechanism that exempts them from severity
consistency, so a rewrite of the suffix text would silently change
pipeline behaviour these tests do not otherwise cover.
"""
from __future__ import annotations

import pytest

from prxref import config, heuristics, metadata_rules, orchestrator
from prxref.forges.base import CommitData, PRData
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

    def test_a_mismatched_branch_is_one_warning(self):
        findings, status = metadata_rules.branch_pattern_check(
            _pr(title="fix: handle empty input", branch="bugfix/42-empty"),
            {"fix": r"fix/\d+.*"},
        )
        assert status == "fail"
        [finding] = findings
        assert finding.severity == "warning"
        assert finding.confidence == 1.0
        assert finding.line == 0
        assert "bugfix/42-empty" in finding.title
        assert heuristics.is_deterministic(finding)

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

    def test_a_missing_reference_is_one_outofscope_per_commit(self):
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
        assert all(f.severity == "outofscope" for f in findings)
        assert all(heuristics.is_deterministic(f) for f in findings)

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

    def test_over_the_cap_is_one_warning_listing_areas(self):
        findings, status = metadata_rules.area_check(
            _files("src/a.py", "web/b.ts", "infra/c.tf"),
            ["backend=src/**", "frontend=web/**", "infra=infra/**"], 2,
        )
        assert status == "fail"
        [finding] = findings
        assert finding.severity == "warning"
        assert finding.line == 0
        assert "3 areas (max 2)" in finding.title
        assert "backend (1 file(s))" in finding.body
        assert "infra (1 file(s))" in finding.body
        assert heuristics.is_deterministic(finding)

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
        assert "backend (2 file(s))" in finding.body

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

    def test_findings_anchor_on_the_first_sorted_diff_path(self):
        findings, _ = metadata_rules.run_metadata_checks(
            _pr(title="fix: x", branch="wrong/name"),
            _files("web/b.ts", "src/a.py"),
            branch_patterns=["fix=^fix/"],
        )
        [finding] = findings
        assert finding.file == "src/a.py"
        assert finding.line == 0

    def test_an_empty_diff_anchors_on_the_empty_string(self):
        findings, _ = metadata_rules.run_metadata_checks(
            _pr(title="fix: x", branch="wrong/name"), [],
            branch_patterns=["fix=^fix/"],
        )
        [finding] = findings
        assert finding.file == ""

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

    def test_on_records_the_stamp_and_the_finding(self):
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
        [finding] = res["findings_active"]
        assert finding.severity == "warning"
        assert finding.file == "src/app.py"
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

    def test_the_summary_lists_the_finding_the_inline_batch_never_carries_it(self):
        forge = FakeForge(
            pr=make_pr(title="feature: widget"),
            diff=_added_file_diff("src/app.py", 20),
        )
        res = orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS,
            metadata_rules="on", branch_patterns=["feature=nomatch/.*"],
        )
        assert any(
            "Branch 'feature/widget' does not match" in f.title
            for f in res["findings_active"]
        )
        assert forge.summaries and "does not match the 'feature' pattern" in forge.summaries[0]
        for batch in forge.inline_batches:
            for comment in batch:
                assert "does not match the 'feature' pattern" not in comment.body

    def test_only_metadata_findings_never_post_an_empty_inline_batch(self):
        forge = FakeForge(
            pr=make_pr(title="feature: widget"),
            diff=_added_file_diff("src/app.py", 20),
        )
        res = orchestrator.orchestrate_review(
            forge, REF, NO_FINDINGS,
            metadata_rules="on", branch_patterns=["feature=nomatch/.*"],
        )
        assert res["findings_active"]
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
            "Commit 1234567890 subject has no" in f.title
            for f in res["findings_active"]
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
        [finding] = res["findings_active"]
        assert finding.file == ""
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
        assert any("2 areas (max 1)" in f.title for f in res["findings_active"])
