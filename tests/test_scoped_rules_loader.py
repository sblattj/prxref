"""Issue #12 T3: ``load_scoped_rules``, from the configured entries to a ``ScopedRules``.

``PRXREF_SCOPED_RULES`` / ``--scoped-rules`` name rules files and directories
whose ``applies_to:`` front matter scopes each file to paths. What is pinned
here, with the real loader and real files under ``tmp_path``:

- ``[]`` and blank entries mean off;
- which files a directory contributes, and the load order across entries;
- confinement: a symlink that stays inside the working directory is read, one
  that escapes it is a ``ConfigError``, never a silent skip;
- the 50-file cap, at exactly 50 and at 51;
- strict UTF-8, NUL bytes and URLs;
- the front matter: ``applies_to`` errors propagate with ``<path>:<line>``, a
  file without the key is logged at INFO, and the key never reaches the
  ignored-keys INFO line;
- the severity merge, and the conflict that names both files and lines;
- the per-file ``PRXREF_REVIEW_RULES_MAX_CHARS`` cap, which truncates and
  warns but is never an error.
"""
from __future__ import annotations

import hashlib
import logging
import os

import pytest

from prxref.llm import ConfigError
from prxref.rules import (
    SCOPED_RULES_MAX_FILES,
    ReviewRules,
    ScopedRules,
    load_review_rules,
    load_scoped_rules,
)
from prxref.text_inputs import cap_text

SOURCES = ("--scoped-rules", "PRXREF_SCOPED_RULES")

JAVA = (
    "---\n"
    "name: java-standards\n"
    'applies_to: ["**/*.java", "!**/src/test/**"]\n'
    "severity:\n"
    "  blocker: error\n"
    "---\n"
    "# Java\n"
    "- every public method has a Javadoc.\n"
)
JAVA_BODY = "# Java\n- every public method has a Javadoc."
JAVA_GLOBS = ("**/*.java", "!**/src/test/**")
HELM = (
    "---\n"
    "applyTo: charts/**\n"
    "severity:\n"
    "  nit: outofscope\n"
    "---\n"
    "# Helm\n"
    "- pin every image tag.\n"
)
EVERYWHERE = "# General\n- no secrets in code.\n"


def _write(directory, name: str, content: str | bytes = EVERYWHERE) -> str:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return str(path)


def _load(entries, *, max_chars: int = 12000, source: str = "--scoped-rules", always_on=None):
    return load_scoped_rules(entries, max_chars=max_chars, source=source, always_on=always_on)


def _config_error(entries, **kwargs) -> str:
    with pytest.raises(ConfigError) as exc:
        _load(entries, **kwargs)
    return str(exc.value)


def _sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _paths(scoped: ScopedRules) -> list[str]:
    return [f.path for f in scoped.files]


class TestOff:
    @pytest.mark.parametrize("entries", [None, [], (), [""], ["   ", "\t"], ""])
    def test_no_entry_means_off(self, entries):
        assert _load(entries) is None

    def test_off_does_not_validate_the_cap(self):
        assert _load([""], max_chars=0) is None


class TestFileEntry:
    def test_a_file_loads_as_one_review_rules_with_its_globs(self, tmp_path):
        path = _write(tmp_path, "java.md", JAVA)
        scoped = _load([path])
        assert scoped == ScopedRules(
            entries=(path,),
            files=(
                ReviewRules(
                    path=path,
                    body=cap_text(JAVA_BODY, 12000, sha256=_sha(path)),
                    severity_map={"blocker": "error"},
                    ignored_keys=("name",),
                    applies_to=JAVA_GLOBS,
                ),
            ),
            severity_map={"blocker": "error"},
        )

    def test_a_bare_string_is_one_entry(self, tmp_path):
        path = _write(tmp_path, "java.md", JAVA)
        assert _paths(_load(path)) == [path]

    def test_the_applyto_alias_scopes_the_file(self, tmp_path):
        scoped = _load([_write(tmp_path, "helm.md", HELM)])
        assert scoped.files[0].applies_to == ("charts/**",)
        assert scoped.files[0].ignored_keys == ()

    def test_paths_are_recorded_as_configured_not_resolved(self, tmp_path, monkeypatch):
        _write(tmp_path / "rules", "java.md", JAVA)
        monkeypatch.chdir(tmp_path)
        scoped = _load(["./rules/java.md", "rules"])
        assert scoped.entries == ("./rules/java.md", "rules")
        assert _paths(scoped) == ["./rules/java.md"]

    def test_record_sha256_equals_shasum_of_the_whole_file(self, tmp_path):
        path = _write(tmp_path, "java.md", JAVA)
        record = _load([path]).files[0].record()
        assert record == {
            "path": path, "sha256": _sha(path), "chars": len(JAVA_BODY), "max_chars": 12000,
            "truncated": False, "severity_map": {"blocker": "error"},
        }
        assert record["sha256"] != hashlib.sha256(JAVA_BODY.encode("utf-8")).hexdigest()

    @pytest.mark.parametrize("source", SOURCES)
    def test_a_missing_file_is_a_config_error_naming_its_source(self, tmp_path, source):
        missing = str(tmp_path / "absent.md")
        assert _config_error([missing], source=source) == (
            f"{source}: cannot read rules file {missing!r}: No such file or directory"
        )


class TestDirectoryEntry:
    def test_a_directory_contributes_its_md_files_in_name_order(self, tmp_path):
        rules = tmp_path / "rules"
        for name in ("b.md", "a.md", "C.md", "notes.txt", "java.MD", ".hidden.md", "._a.md"):
            _write(rules, name)
        _write(rules / "nested", "deep.md")
        scoped = _load([str(rules)])
        assert _paths(scoped) == [os.path.join(str(rules), name) for name in ("C.md", "a.md", "b.md")]
        assert scoped.entries == (str(rules),)

    def test_a_directory_file_path_is_the_directory_joined_with_the_name(self, tmp_path, monkeypatch):
        _write(tmp_path / "rules", "java.md", JAVA)
        monkeypatch.chdir(tmp_path)
        assert _paths(_load(["rules/"])) == ["rules/java.md"]

    def test_an_empty_directory_warns_and_loads_no_files(self, tmp_path, caplog):
        empty = tmp_path / "rules"
        empty.mkdir()
        _write(empty, "README.txt")
        with caplog.at_level(logging.INFO, logger="prxref"):
            scoped = _load([str(empty)])
        assert scoped == ScopedRules(entries=(str(empty),), files=(), severity_map={})
        assert [r.getMessage() for r in caplog.records] == [
            f"--scoped-rules: rules directory {str(empty)!r} holds no *.md files; no rules loaded from it",
        ]

    def test_a_subdirectory_named_like_a_rules_file_is_a_config_error(self, tmp_path):
        rules = tmp_path / "rules"
        (rules / "old.md").mkdir(parents=True)
        path = os.path.join(str(rules), "old.md")
        assert _config_error([str(rules)]) == f"--scoped-rules: cannot read rules file {path!r}: Is a directory"

    def test_a_fifo_named_like_a_rules_file_is_refused_without_being_opened(self, tmp_path):
        rules = tmp_path / "rules"
        rules.mkdir()
        os.mkfifo(rules / "pipe.md")
        path = os.path.join(str(rules), "pipe.md")
        assert _config_error([str(rules)]) == f"--scoped-rules: cannot read rules file {path!r}: not a regular file"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root lists a mode-000 directory")
    def test_an_unlistable_directory_is_a_config_error(self, tmp_path):
        rules = tmp_path / "rules"
        _write(rules, "java.md", JAVA)
        os.chmod(rules, 0)
        try:
            message = _config_error([str(rules)])
        finally:
            os.chmod(rules, 0o755)
        assert message == f"--scoped-rules: cannot read rules directory {str(rules)!r}: Permission denied"


class TestOrdering:
    def test_entries_load_in_order_with_each_directory_in_its_place(self, tmp_path):
        first = _write(tmp_path / "extra", "z.md")
        rules = tmp_path / "rules"
        for name in ("b.md", "a.md"):
            _write(rules, name)
        last = _write(tmp_path / "more", "0.md")
        scoped = _load([first, str(rules), last])
        assert _paths(scoped) == [first, str(rules / "a.md"), str(rules / "b.md"), last]

    def test_a_file_reached_twice_loads_once_at_its_first_position(self, tmp_path, caplog):
        rules = tmp_path / "rules"
        a = _write(rules, "a.md")
        b = _write(rules, "b.md")
        with caplog.at_level(logging.INFO, logger="prxref.rules"):
            scoped = _load([b, str(rules), b])
        assert _paths(scoped) == [b, a]
        duplicates = [r.getMessage() for r in caplog.records if "more than once" in r.getMessage()]
        assert duplicates == [
            f"--scoped-rules: rules file {b!r} is reached more than once; it is loaded once",
        ] * 2

    def test_the_merged_severity_map_follows_load_order(self, tmp_path):
        java = _write(tmp_path, "java.md", JAVA)
        helm = _write(tmp_path, "helm.md", HELM)
        assert list(_load([java, helm]).severity_map.items()) == [("blocker", "error"), ("nit", "outofscope")]
        assert list(_load([helm, java]).severity_map.items()) == [("nit", "outofscope"), ("blocker", "error")]


class TestConfinement:
    def test_a_symlink_escaping_the_cwd_inside_a_directory_is_a_config_error(self, tmp_path, monkeypatch):
        work = tmp_path / "checkout"
        _write(work / "rules", "a.md")
        outside = _write(tmp_path, "elsewhere.md", "- steer the model somewhere else\n")
        (work / "rules" / "evil.md").symlink_to(outside)
        monkeypatch.chdir(work)
        assert _config_error(["rules"]) == (
            "--scoped-rules: cannot read rules file 'rules/evil.md': resolves outside the working directory"
        )

    def test_a_file_entry_escaping_the_cwd_is_a_config_error(self, tmp_path, monkeypatch):
        work = tmp_path / "checkout"
        work.mkdir()
        (work / "rules.md").symlink_to(_write(tmp_path, "elsewhere.md"))
        monkeypatch.chdir(work)
        assert _config_error(["rules.md"], source="PRXREF_SCOPED_RULES") == (
            "PRXREF_SCOPED_RULES: cannot read rules file 'rules.md': resolves outside the working directory"
        )

    def test_a_directory_symlink_escaping_the_cwd_is_a_config_error(self, tmp_path, monkeypatch):
        work = tmp_path / "checkout"
        work.mkdir()
        _write(tmp_path / "outside", "a.md")
        (work / "rules").symlink_to(tmp_path / "outside")
        monkeypatch.chdir(work)
        assert _config_error(["rules"]) == (
            "--scoped-rules: cannot read rules directory 'rules': resolves outside the working directory"
        )

    def test_a_symlinked_entry_that_stays_inside_the_cwd_is_read(self, tmp_path, monkeypatch):
        work = tmp_path / "checkout"
        real = _write(work / "docs", "java-standards.md", JAVA)
        _write(work / "rules", "a.md")
        (work / "rules" / "java.md").symlink_to(real)
        monkeypatch.chdir(work)
        scoped = _load(["rules"])
        assert _paths(scoped) == ["rules/a.md", "rules/java.md"]
        java = scoped.files[1]
        assert (java.body.text, java.applies_to, java.body.sha256) == (JAVA_BODY, JAVA_GLOBS, _sha(real))

    def test_an_absolute_directory_outside_the_cwd_is_the_operators_choice(self, tmp_path, monkeypatch):
        work = tmp_path / "checkout"
        work.mkdir()
        trusted = tmp_path / "trusted"
        _write(trusted, "java.md", JAVA)
        monkeypatch.chdir(work)
        assert _paths(_load([str(trusted)])) == [str(trusted / "java.md")]


class TestFileCap:
    @staticmethod
    def _fill(directory, count: int) -> list[str]:
        return [_write(directory, f"r{index:02d}.md") for index in range(count)]

    def test_the_cap_is_fifty(self):
        assert SCOPED_RULES_MAX_FILES == 50

    def test_exactly_fifty_files_load(self, tmp_path):
        written = self._fill(tmp_path / "rules", 50)
        assert _paths(_load([str(tmp_path / "rules")])) == written

    @pytest.mark.parametrize("source", SOURCES)
    def test_fifty_one_files_are_a_config_error_naming_the_first_past_the_cap(self, tmp_path, source):
        written = self._fill(tmp_path / "rules", 51)
        assert _config_error([str(tmp_path / "rules")], source=source) == (
            f"{source}: 51 rules files are configured, over the limit of 50; "
            f"the first one past it is {written[50]!r}"
        )

    def test_the_cap_counts_across_entries(self, tmp_path):
        self._fill(tmp_path / "one", 30)
        second = self._fill(tmp_path / "two", 21)
        assert _config_error([str(tmp_path / "one"), str(tmp_path / "two")]) == (
            "--scoped-rules: 51 rules files are configured, over the limit of 50; "
            f"the first one past it is {second[20]!r}"
        )

    def test_the_cap_is_checked_before_any_file_is_read(self, tmp_path):
        self._fill(tmp_path / "rules", 50)
        _write(tmp_path / "rules", "r50.md", b"\xff not utf-8")
        assert "51 rules files are configured" in _config_error([str(tmp_path / "rules")])

    def test_a_file_reached_twice_counts_once(self, tmp_path):
        written = self._fill(tmp_path / "rules", 50)
        assert len(_load([str(tmp_path / "rules"), written[0]]).files) == 50


class TestEncodingAndUrls:
    @pytest.mark.parametrize(("raw", "offset"), [(b"ab\xffcd", 2), (b"\xef\xbb\xbfab\xffcd", 5)])
    def test_non_utf8_is_a_config_error_naming_the_byte(self, tmp_path, raw, offset):
        path = _write(tmp_path / "rules", "bad.md", raw)
        assert _config_error([str(tmp_path / "rules")]) == (
            f"--scoped-rules: rules file {path!r} is not UTF-8 text (invalid start byte at byte {offset})"
        )

    @pytest.mark.parametrize("source", SOURCES)
    def test_nul_bytes_are_a_config_error(self, tmp_path, source):
        path = _write(tmp_path, "nul.md", b"# Rules\n- one\x00two\n")
        assert _config_error([path], source=source) == (
            f"{source}: rules file {path!r} contains NUL bytes; expected Markdown or plain text"
        )

    @pytest.mark.parametrize("source", SOURCES)
    @pytest.mark.parametrize("url", [
        "https://example.com/acme/rules/",
        "http://example.com/java.md",
        "file:///etc/rules",
        "  https://example.com/java.md",
    ])
    def test_a_url_is_refused_as_not_a_local_path(self, tmp_path, source, url):
        local = _write(tmp_path, "java.md", JAVA)
        assert _config_error([local, url], source=source) == (
            f"{source}: scoped rules must be local file or directory paths, not a URL: {url!r}"
        )


class TestFrontMatter:
    @pytest.mark.parametrize("source", SOURCES)
    def test_an_empty_applies_to_is_a_config_error_with_its_line(self, tmp_path, source):
        path = _write(tmp_path / "rules", "empty.md", "---\nname: x\napplies_to: []\n---\nbody\n")
        assert _config_error([str(tmp_path / "rules")], source=source) == (
            f"{source}: {path}:3: 'applies_to' is empty; list at least one glob, or omit the key to "
            "apply the file to every unit"
        )

    def test_a_leading_slash_is_a_config_error_with_its_line(self, tmp_path):
        path = _write(tmp_path, "abs.md", '---\napplyTo:\n  - "src/**"\n  - "/charts/**"\n---\nbody\n')
        assert _config_error([path]) == (
            f"--scoped-rules: {path}:4: 'applyTo' entry '/charts/**' starts with '/', but diff paths are "
            "relative to the repository root; drop the leading '/'"
        )

    def test_a_malformed_severity_map_is_a_config_error_with_its_line(self, tmp_path):
        path = _write(tmp_path, "sev.md", "---\napplies_to: src/**\nseverity:\n  blocker: critical\n---\n")
        assert _config_error([path]) == (
            f"--scoped-rules: {path}:4: unknown severity 'critical' for 'blocker'; expected one of "
            "error, outofscope, warning"
        )

    def test_a_file_without_applies_to_reaches_every_unit_and_says_so_at_info(self, tmp_path, caplog):
        path = _write(tmp_path, "general.md", EVERYWHERE)
        with caplog.at_level(logging.INFO, logger="prxref"):
            scoped = _load([path], source="PRXREF_SCOPED_RULES")
        assert scoped.files[0].applies_to is None
        assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
            (
                logging.INFO,
                f"PRXREF_SCOPED_RULES: rules file {path!r} has no 'applies_to' key, so it reaches every unit",
            ),
        ]

    def test_an_unclosed_fence_is_all_body_and_reaches_every_unit(self, tmp_path, caplog):
        path = _write(tmp_path, "open.md", "---\napplies_to: src/**\n")
        with caplog.at_level(logging.INFO, logger="prxref"):
            scoped = _load([path])
        assert (scoped.files[0].applies_to, scoped.files[0].body.text) == (None, "---\napplies_to: src/**")
        assert [r.levelname for r in caplog.records] == ["WARNING", "INFO"]

    def test_the_ignored_keys_info_line_never_names_applies_to(self, tmp_path, caplog):
        java = _write(tmp_path, "java.md", JAVA)
        helm = _write(tmp_path, "helm.md", HELM)
        with caplog.at_level(logging.INFO, logger="prxref"):
            scoped = _load([java, helm])
        assert [r.getMessage() for r in caplog.records] == [
            f"--scoped-rules: rules file {java!r}: ignoring front-matter keys other than 'severity' "
            "and 'applies_to': name",
        ]
        assert [f.ignored_keys for f in scoped.files] == [("name",), ()]

    def test_an_empty_file_warns_and_is_still_loaded(self, tmp_path, caplog):
        path = _write(tmp_path, "empty.md", "---\napplies_to: src/**\n---\n")
        with caplog.at_level(logging.WARNING, logger="prxref"):
            scoped = _load([path])
        assert [r.getMessage() for r in caplog.records] == [
            f"--scoped-rules: rules file {path!r} is empty; no rules injected",
        ]
        assert scoped.files[0].record()["chars"] == 0


class TestSeverityMerge:
    KOTLIN = (
        "---\n"
        "description: |\n"
        "  blocker: warning\n"
        'applies_to: "**/*.kt"\n'
        "severity:\n"
        "  Blocker: warning\n"
        "---\n"
        "# Kotlin\n"
    )

    def test_each_file_keeps_its_own_map_and_the_merge_holds_both(self, tmp_path):
        scoped = _load([_write(tmp_path, "java.md", JAVA), _write(tmp_path, "helm.md", HELM)])
        assert [dict(f.severity_map) for f in scoped.files] == [{"blocker": "error"}, {"nit": "outofscope"}]
        assert dict(scoped.severity_map) == {"blocker": "error", "nit": "outofscope"}

    def test_one_word_mapped_to_the_same_tier_twice_is_merged_once(self, tmp_path):
        java = _write(tmp_path, "java.md", JAVA)
        also = _write(tmp_path, "kotlin.md", self.KOTLIN.replace("  Blocker: warning", "  Blocker: error"))
        assert dict(_load([java, also]).severity_map) == {"blocker": "error"}

    @pytest.mark.parametrize("source", SOURCES)
    def test_a_conflicting_tier_is_a_config_error_naming_both_files_and_lines(self, tmp_path, source):
        java = _write(tmp_path, "java.md", JAVA)
        kotlin = _write(tmp_path, "kotlin.md", self.KOTLIN)
        assert _config_error([java, kotlin], source=source) == (
            f"{source}: {kotlin}:6: 'blocker' is mapped to warning here but to error in {java}:5; "
            "map each team word to one tier across all rules files"
        )

    def test_a_conflict_with_the_always_on_file_is_a_config_error(self, tmp_path):
        always_path = _write(tmp_path, "team.md", "---\nseverity:\n  blocker: warning\n---\n- rules\n")
        always_on = load_review_rules(always_path, max_chars=100, source="--rules-file")
        java = _write(tmp_path, "java.md", JAVA)
        assert _config_error([java], always_on=always_on) == (
            f"--scoped-rules: {java}:5: 'blocker' is mapped to error here but to warning in the always-on "
            f"rules file {always_path!r}; map each team word to one tier across all rules files"
        )

    def test_the_always_on_map_is_checked_but_not_merged(self, tmp_path):
        always_path = _write(tmp_path, "team.md", "---\nseverity:\n  blocker: error\n  major: warning\n---\n")
        always_on = load_review_rules(always_path, max_chars=100, source="--rules-file")
        scoped = _load([_write(tmp_path, "java.md", JAVA)], always_on=always_on)
        assert dict(scoped.severity_map) == {"blocker": "error"}


class TestPerFileCap:
    def test_a_long_body_is_truncated_recorded_and_warned_not_refused(self, tmp_path, caplog):
        path = _write(tmp_path, "java.md", JAVA)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            scoped = _load([path], max_chars=10, source="PRXREF_SCOPED_RULES")
        body = scoped.files[0].body
        assert (body.text, body.truncated, body.chars) == (JAVA_BODY[:10], True, len(JAVA_BODY))
        assert scoped.files[0].record() == {
            "path": path, "sha256": _sha(path), "chars": len(JAVA_BODY), "max_chars": 10,
            "truncated": True, "severity_map": {"blocker": "error"},
        }
        assert [r.getMessage() for r in caplog.records] == [
            f"PRXREF_SCOPED_RULES: rules file {path!r} has {len(JAVA_BODY)} characters (after front "
            "matter); only the first 10 reach the prompt — raise PRXREF_REVIEW_RULES_MAX_CHARS",
        ]

    def test_each_file_is_capped_on_its_own(self, tmp_path):
        java = _write(tmp_path, "java.md", JAVA)
        general = _write(tmp_path, "general.md", EVERYWHERE)
        cap = len(EVERYWHERE.strip())
        scoped = _load([java, general], max_chars=cap)
        assert [f.body.truncated for f in scoped.files] == [len(JAVA_BODY) > cap, False]
        assert scoped.files[1].body.text == EVERYWHERE.strip()

    def test_a_body_exactly_at_the_cap_is_whole_and_logs_no_warning(self, tmp_path, caplog):
        path = _write(tmp_path, "java.md", JAVA)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            body = _load([path], max_chars=len(JAVA_BODY)).files[0].body
        assert (body.text, body.truncated) == (JAVA_BODY, False)
        assert caplog.records == []

    @pytest.mark.parametrize("cap", [0, -1, True, "5", 1.5])
    def test_a_cap_below_one_is_a_config_error_naming_the_variable(self, tmp_path, cap):
        path = _write(tmp_path, "java.md", JAVA)
        assert _config_error([path], max_chars=cap) == (
            f"--scoped-rules: PRXREF_REVIEW_RULES_MAX_CHARS must be at least 1, got {cap!r}"
        )
