"""Issue #12 T4: selecting scoped rules per review unit, and the block each unit gets.

``ScopedRules.select`` picks the rules files a unit's diff paths reach,
``ScopedRules.unit_block`` builds that unit's ``## Team review rules`` block
under the per-unit ``PRXREF_SCOPED_RULES_MAX_CHARS`` cap,
``merged_severity_map`` is the run-wide map, and ``record`` is the run-record
view. What is pinned here, with real files loaded through
``load_scoped_rules`` / ``load_review_rules`` from a temporary working
directory (both loaders confine paths to the cwd):

- selection: a file without ``applies_to`` reaches every unit; ``!``
  negation; a rename's ``old_path``; load order;
- byte identity: a unit no scoped file reaches, when no scoped file adds a
  severity word, gets exactly ``ReviewRules.prompt_block(unit)``, or ``""``
  with no always-on file;
- the block's structure, byte for byte, with and without an always-on file;
- the per-unit cap: whole files, the first file truncated, the omission
  marker, and what ``ScopedBlock`` reports;
- the merged severity map and the record shape.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from prxref.rules import (
    RULES_HEADING,
    ReviewRules,
    ScopedBlock,
    ScopedRules,
    load_review_rules,
    load_scoped_rules,
)
from prxref.text_inputs import cap_text

UNITS = ("worker", "sweep")

JAVA_GLOBS = ("**/*.java", "!**/src/test/**")
JAVA_BODY = "# Java\n- every public method has a Javadoc."
HELM_BODY = "# Helm\n- pin every image tag."
GENERAL_BODY = "# General\n- no secrets in code."
ALWAYS_BODY = "# Team\n- keep pull requests small."


def _front(applies_to=None, severity=None) -> str:
    lines = []
    if applies_to is not None:
        lines.append(f"applies_to: {json.dumps(list(applies_to))}")
    if severity:
        lines.append("severity:")
        lines.extend(f"  {word}: {tier}" for word, tier in severity.items())
    return "---\n" + "\n".join(lines) + "\n---\n" if lines else ""


def _write(path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _rules_file(cwd, name: str, body: str, *, applies_to=None, severity=None) -> str:
    _write(cwd / "rules" / name, _front(applies_to, severity) + body + "\n")
    return f"rules/{name}"


def _scoped(entries, *, max_chars: int = 12000, always_on=None) -> ScopedRules:
    scoped = load_scoped_rules(entries, max_chars=max_chars, source="--scoped-rules", always_on=always_on)
    assert scoped is not None
    return scoped


def _always_on(cwd, body: str = ALWAYS_BODY, *, severity=None, max_chars: int = 12000) -> ReviewRules:
    _write(cwd / "team.md", _front(severity=severity) + body + "\n")
    rules = load_review_rules("team.md", max_chars=max_chars, source="--rules-file")
    assert rules is not None
    return rules


def _java(cwd, **kwargs) -> str:
    return _rules_file(cwd, "10-java.md", JAVA_BODY, applies_to=JAVA_GLOBS, **kwargs)


def _helm(cwd, **kwargs) -> str:
    return _rules_file(cwd, "20-helm.md", HELM_BODY, applies_to=["charts/**"], **kwargs)


def _general(cwd, **kwargs) -> str:
    return _rules_file(cwd, "30-general.md", GENERAL_BODY, **kwargs)


def _paths(files) -> list[str]:
    return [f.path for f in files]


def _framing(unit: str) -> str:
    return ReviewRules("x", cap_text("x", 10), {}).prompt_block(unit).split("\n\n")[1]


def _severity(entries: str) -> str:
    return (
        f"Team severity words map onto that vocabulary: {entries}. Classify a problem by the "
        "team's definition, then write the mapped word in `severity`."
    )


def _block(unit: str, scoped: ScopedRules, paths, always_on=None, *, max_chars: int = 24000) -> ScopedBlock:
    return scoped.unit_block(unit, paths, always_on, max_chars=max_chars)


class TestSelect:
    def test_a_file_without_applies_to_reaches_every_unit_even_one_with_no_paths(self, cwd):
        general = _general(cwd)
        scoped = _scoped([general])
        assert _paths(scoped.select([])) == [general]
        assert _paths(scoped.select(["README.md"])) == [general]

    def test_a_scoped_file_is_selected_when_one_path_matches(self, cwd):
        java = _java(cwd)
        scoped = _scoped([java])
        assert _paths(scoped.select(["charts/app/values.yaml", "src/main/A.java"])) == [java]
        assert scoped.select(["charts/app/values.yaml"]) == ()
        assert scoped.select([]) == ()

    def test_a_root_level_file_matches_a_leading_any_dirs_glob(self, cwd):
        java = _java(cwd)
        assert _paths(_scoped([java]).select(["Main.java"])) == [java]

    def test_a_negation_vetoes_the_paths_it_matches(self, cwd):
        java = _java(cwd)
        scoped = _scoped([java])
        assert scoped.select(["src/test/java/ATest.java"]) == ()
        assert scoped.select(["module/src/test/ATest.java"]) == ()

    def test_a_negation_vetoes_only_its_own_paths_so_another_path_still_selects(self, cwd):
        java = _java(cwd)
        scoped = _scoped([java])
        assert _paths(scoped.select(["src/test/ATest.java", "src/main/A.java"])) == [java]

    def test_the_old_path_of_a_rename_selects_the_file(self, cwd):
        legacy = _rules_file(cwd, "legacy.md", "# Legacy\n- keep the old API.", applies_to=["legacy/**"])
        scoped = _scoped([legacy])
        assert _paths(scoped.select(["modern/api.py", "legacy/api.py"])) == [legacy]
        assert scoped.select(["modern/api.py"]) == ()

    def test_none_and_empty_paths_are_skipped(self, cwd):
        java = _java(cwd)
        scoped = _scoped([java])
        assert scoped.select(["src/A.java", None, ""]) == scoped.select(["src/A.java"])
        assert scoped.select([None, ""]) == ()

    def test_a_bare_string_is_one_path(self, cwd):
        java = _java(cwd)
        assert _paths(_scoped([java]).select("src/A.java")) == [java]

    def test_a_one_shot_iterable_of_paths_reaches_every_file(self, cwd):
        java = _java(cwd)
        also = _rules_file(cwd, "11-java-too.md", "# More Java", applies_to=["**/*.java"])
        scoped = _scoped(["rules"])
        assert _paths(scoped.select(p for p in ["src/A.java"])) == [java, also]

    def test_selection_keeps_load_order_whatever_the_path_order(self, cwd):
        java, helm, general = _java(cwd), _helm(cwd), _general(cwd)
        scoped = _scoped([general, helm, java])
        assert _paths(scoped.select(["src/A.java", "charts/x.yaml"])) == [general, helm, java]
        assert _paths(scoped.select(["charts/x.yaml", "src/A.java"])) == [general, helm, java]

    def test_the_union_of_chunk_paths_selects_the_union_of_chunk_selections(self, cwd):
        java, helm, general = _java(cwd), _helm(cwd), _general(cwd)
        scoped = _scoped(["rules"])
        chunks = [["src/A.java", "src/B.java"], ["charts/app/values.yaml"], ["docs/x.md"]]
        union = [path for chunk in chunks for path in chunk]
        selected = {path for chunk in chunks for path in _paths(scoped.select(chunk))}
        assert _paths(scoped.select(union)) == [java, helm, general]
        assert selected == {java, helm, general}


class TestByteIdentity:
    @pytest.mark.parametrize("unit", UNITS)
    def test_no_match_and_no_scoped_severity_equals_the_always_on_block(self, cwd, unit):
        always_on = _always_on(cwd, severity={"blocker": "error"})
        scoped = _scoped([_java(cwd), _helm(cwd)])
        block = _block(unit, scoped, ["docs/guide.md"], always_on)
        assert block.text == always_on.prompt_block(unit)
        assert block == ScopedBlock(always_on.prompt_block(unit), (), None, ())

    @pytest.mark.parametrize("unit", UNITS)
    def test_scoped_words_the_always_on_map_already_has_keep_the_block_identical(self, cwd, unit):
        always_on = _always_on(cwd, severity={"blocker": "error", "nit": "outofscope"})
        scoped = _scoped([_java(cwd, severity={"nit": "outofscope"})], always_on=always_on)
        assert _block(unit, scoped, ["docs/guide.md"], always_on).text == always_on.prompt_block(unit)

    @pytest.mark.parametrize("unit", UNITS)
    def test_with_no_always_on_file_an_unreached_unit_gets_nothing(self, cwd, unit):
        scoped = _scoped([_java(cwd), _helm(cwd)])
        assert _block(unit, scoped, ["docs/guide.md"]) == ScopedBlock("", (), None, ())

    def test_an_always_on_truncation_line_survives_byte_for_byte(self, cwd):
        always_on = _always_on(cwd, "x" * 40, max_chars=25)
        scoped = _scoped([_java(cwd)])
        expected = always_on.prompt_block("worker")
        assert "[team rules truncated: only the first 25 of 40 characters are shown]" in expected
        assert _block("worker", scoped, ["docs/guide.md"], always_on).text == expected

    def test_an_empty_always_on_file_and_no_match_gives_nothing(self, cwd):
        always_on = _always_on(cwd, "")
        assert always_on.prompt_block("worker") == ""
        assert _block("worker", _scoped([_java(cwd)]), ["docs/guide.md"], always_on).text == ""

    @pytest.mark.parametrize("unit", UNITS)
    def test_a_selected_file_with_no_body_and_no_new_word_keeps_the_block_identical(self, cwd, unit):
        always_on = _always_on(cwd, severity={"blocker": "error"})
        only_map = _rules_file(cwd, "map.md", "", applies_to=["**/*.java"], severity={"blocker": "error"})
        scoped = _scoped([only_map], always_on=always_on)
        block = _block(unit, scoped, ["src/A.java"], always_on)
        assert block.text == always_on.prompt_block(unit)
        assert _paths(block.files) == [only_map]

    @pytest.mark.parametrize("unit", UNITS)
    def test_a_new_scoped_word_reaches_an_unmatched_unit_through_the_severity_paragraph(self, cwd, unit):
        always_on = _always_on(cwd, severity={"blocker": "error"})
        scoped = _scoped([_helm(cwd, severity={"nit": "outofscope"})], always_on=always_on)
        block = _block(unit, scoped, ["docs/guide.md"], always_on)
        assert block.text != always_on.prompt_block(unit)
        assert block.text == "\n\n".join([
            RULES_HEADING,
            _framing(unit),
            _severity("`blocker` → `error`; `nit` → `outofscope`"),
            f"<team_rules>\n{ALWAYS_BODY}\n</team_rules>",
        ])
        assert block == ScopedBlock(block.text, (), None, ())

    def test_a_new_scoped_word_with_no_always_on_file_gives_a_map_only_block(self, cwd):
        scoped = _scoped([_helm(cwd, severity={"nit": "outofscope"})])
        assert _block("worker", scoped, ["docs/guide.md"]).text == "\n\n".join([
            RULES_HEADING, _framing("worker"), _severity("`nit` → `outofscope`"),
        ])


class TestBlock:
    def test_scoped_only_block_byte_for_byte(self, cwd):
        java = _java(cwd, severity={"blocker": "error"})
        _helm(cwd, severity={"nit": "outofscope"})
        general = _general(cwd)
        scoped = _scoped(["rules"])
        block = _block("worker", scoped, ["src/main/A.java"])
        assert block.text == (
            f"{RULES_HEADING}\n\n"
            f"{_framing('worker')}\n\n"
            f"{_severity('`blocker` → `error`; `nit` → `outofscope`')}\n\n"
            '<team_rules source="rules/10-java.md" applies_to="**/*.java, !**/src/test/**">\n'
            f"{JAVA_BODY}\n"
            "</team_rules>\n\n"
            '<team_rules source="rules/30-general.md">\n'
            f"{GENERAL_BODY}\n"
            "</team_rules>"
        )
        assert block == ScopedBlock(block.text, tuple(scoped.select(["src/main/A.java"])), None, ())
        assert _paths(block.files) == [java, general]

    def test_scoped_only_block_with_no_severity_map_has_no_severity_paragraph(self, cwd):
        scoped = _scoped([_helm(cwd)])
        assert _block("sweep", scoped, ["charts/x.yaml"]).text == (
            f"{RULES_HEADING}\n\n{_framing('sweep')}\n\n"
            f'<team_rules source="rules/20-helm.md" applies_to="charts/**">\n{HELM_BODY}\n</team_rules>'
        )

    @pytest.mark.parametrize("unit", UNITS)
    def test_always_on_and_scoped_share_one_heading_framing_and_merged_map(self, cwd, unit):
        always_on = _always_on(cwd, severity={"blocker": "error", "nit": "outofscope"})
        scoped = _scoped(
            [_java(cwd, severity={"nit": "outofscope", "must": "error"}), _helm(cwd)], always_on=always_on,
        )
        text = _block(unit, scoped, ["src/A.java", "charts/x.yaml"], always_on).text
        assert text == "\n\n".join([
            RULES_HEADING,
            _framing(unit),
            _severity("`blocker` → `error`; `nit` → `outofscope`; `must` → `error`"),
            f"<team_rules>\n{ALWAYS_BODY}\n</team_rules>",
            '<team_rules source="rules/10-java.md" applies_to="**/*.java, !**/src/test/**">\n'
            f"{JAVA_BODY}\n</team_rules>",
            f'<team_rules source="rules/20-helm.md" applies_to="charts/**">\n{HELM_BODY}\n</team_rules>',
        ])
        assert text.count(RULES_HEADING) == 1
        assert text.count("Team severity words") == 1

    def test_the_always_on_truncation_line_follows_its_body_before_the_scoped_files(self, cwd):
        always_on = _always_on(cwd, "y" * 30, max_chars=20)
        scoped = _scoped([_general(cwd)])
        assert _block("worker", scoped, ["a.py"], always_on).text == "\n\n".join([
            RULES_HEADING,
            _framing("worker"),
            f"<team_rules>\n{'y' * 20}\n</team_rules>",
            "[team rules truncated: only the first 20 of 30 characters are shown]",
            f'<team_rules source="rules/30-general.md">\n{GENERAL_BODY}\n</team_rules>',
        ])

    def test_a_file_cut_by_its_own_cap_gets_the_prompt_block_truncation_line(self, cwd):
        _rules_file(cwd, "long.md", "z" * 50)
        scoped = _scoped(["rules"], max_chars=30)
        (rules,) = scoped.files
        own_line = rules.prompt_block("worker").rsplit("\n\n", 1)[1]
        assert own_line == "[team rules truncated: only the first 30 of 50 characters are shown]"
        block = _block("worker", scoped, ["a.py"])
        assert block.text.endswith(f'<team_rules source="rules/long.md">\n{"z" * 30}\n</team_rules>\n\n{own_line}')
        assert block.truncated is None
        assert block.omitted == ()

    def test_a_selected_file_with_no_body_adds_no_element_but_is_carried(self, cwd):
        only_map = _rules_file(cwd, "map.md", "", applies_to=["**/*.java"], severity={"must": "error"})
        java = _java(cwd)
        scoped = _scoped(["rules"])
        block = _block("worker", scoped, ["src/A.java"])
        assert "rules/map.md" not in block.text
        assert _paths(block.files) == [java, only_map]

    def test_selected_files_with_nothing_to_say_give_nothing(self, cwd):
        _rules_file(cwd, "empty.md", "")
        scoped = _scoped(["rules"])
        block = _block("worker", scoped, ["a.py"])
        assert block.text == ""
        assert _paths(block.files) == ["rules/empty.md"]

    def test_quotes_and_ampersands_in_attributes_are_escaped(self, cwd):
        _rules_file(cwd, 'r&d "core".md', "# R&D", applies_to=['src/"quoted"/**'])
        scoped = _scoped(["rules"])
        text = _block("worker", scoped, ['src/"quoted"/a.py']).text
        assert '<team_rules source="rules/r&amp;d &quot;core&quot;.md" applies_to="src/&quot;quoted&quot;/**">' in text
        assert "\n# R&D\n" in text

    def test_an_unknown_unit_is_a_value_error(self, cwd):
        with pytest.raises(ValueError, match="unit must be one of worker, sweep, got 'chunk'"):
            _block("chunk", _scoped([_general(cwd)]), ["a.py"])

    @pytest.mark.parametrize("cap", [0, -1])
    def test_a_cap_below_one_is_a_value_error(self, cwd, cap):
        with pytest.raises(ValueError, match="max_chars must be at least 1"):
            _block("worker", _scoped([_general(cwd)]), ["a.py"], max_chars=cap)


class TestTotalCap:
    @pytest.fixture
    def three(self, cwd):
        return [
            _rules_file(cwd, "a.md", "a" * 10),
            _rules_file(cwd, "b.md", "b" * 10),
            _rules_file(cwd, "c.md", "c" * 10),
        ]

    @staticmethod
    def _element(name: str, text: str) -> str:
        return f'<team_rules source="rules/{name}">\n{text}\n</team_rules>'

    def test_whole_files_that_fit_exactly_are_not_cut(self, three):
        block = _block("worker", _scoped(["rules"]), ["x.py"], max_chars=30)
        assert block.text.endswith(self._element("c.md", "c" * 10))
        assert "truncated" not in block.text
        assert "omitted" not in block.text
        assert (_paths(block.files), block.truncated, block.omitted) == (three, None, ())

    def test_the_last_file_that_does_not_fit_is_truncated_with_no_omission_marker(self, three):
        block = _block("worker", _scoped(["rules"]), ["x.py"], max_chars=25)
        assert block.text.endswith(
            self._element("c.md", "c" * 5)
            + "\n\n[team rules truncated: only the first 5 of 10 characters are shown]"
        )
        assert "omitted" not in block.text
        assert (_paths(block.files), block.truncated, block.omitted) == (three, "rules/c.md", ())

    def test_the_first_file_that_does_not_fit_is_truncated_and_the_rest_omitted(self, three):
        block = _block("worker", _scoped(["rules"]), ["x.py"], max_chars=15)
        assert block.text == "\n\n".join([
            RULES_HEADING,
            _framing("worker"),
            self._element("a.md", "a" * 10),
            self._element("b.md", "b" * 5),
            "[team rules truncated: only the first 5 of 10 characters are shown]",
            "[team rules omitted: rules/c.md (over the 15-character limit on scoped rules for one review unit)]",
        ])
        assert (_paths(block.files), block.truncated, block.omitted) == (three[:2], "rules/b.md", ("rules/c.md",))

    def test_with_no_room_left_the_next_file_is_omitted_not_truncated_to_nothing(self, three):
        block = _block("worker", _scoped(["rules"]), ["x.py"], max_chars=20)
        assert "<team_rules source=\"rules/c.md\">" not in block.text
        assert block.text.endswith(
            self._element("b.md", "b" * 10)
            + "\n\n[team rules omitted: rules/c.md (over the 20-character limit on scoped rules for one review unit)]"
        )
        assert (_paths(block.files), block.truncated, block.omitted) == (three[:2], None, ("rules/c.md",))

    def test_every_later_file_is_omitted_even_one_that_would_fit(self, cwd):
        _rules_file(cwd, "a.md", "a" * 10)
        _rules_file(cwd, "b.md", "b" * 30)
        _rules_file(cwd, "c.md", "c")
        block = _block("worker", _scoped(["rules"]), ["x.py"], max_chars=20)
        assert block.truncated == "rules/b.md"
        assert block.omitted == ("rules/c.md",)
        assert "[team rules omitted: rules/c.md (over the 20-character limit" in block.text

    def test_the_marker_names_every_omitted_file_in_load_order(self, cwd):
        for name in ("a.md", "b.md", "c.md", "d.md"):
            _rules_file(cwd, name, name[0] * 10)
        block = _block("worker", _scoped(["rules"]), ["x.py"], max_chars=12)
        assert block.omitted == ("rules/c.md", "rules/d.md")
        assert block.text.endswith(
            "[team rules omitted: rules/c.md, rules/d.md (over the 12-character limit on scoped rules for one "
            "review unit)]"
        )

    def test_only_selected_files_count_against_the_cap(self, cwd):
        _rules_file(cwd, "a-helm.md", "h" * 50, applies_to=["charts/**"])
        _rules_file(cwd, "b.md", "b" * 10)
        block = _block("worker", _scoped(["rules"]), ["src/A.java"], max_chars=10)
        assert _paths(block.files) == ["rules/b.md"]
        assert (block.truncated, block.omitted) == (None, ())

    def test_the_always_on_body_does_not_count_against_the_cap(self, cwd):
        always_on = _always_on(cwd, "t" * 100)
        _rules_file(cwd, "a.md", "a" * 10)
        block = _block("worker", _scoped(["rules"]), ["x.py"], always_on, max_chars=10)
        assert block.text.endswith(f"<team_rules>\n{'t' * 100}\n</team_rules>\n\n" + self._element("a.md", "a" * 10))
        assert (block.truncated, block.omitted) == (None, ())

    def test_a_file_with_no_body_after_the_cut_is_carried_not_omitted(self, cwd):
        _rules_file(cwd, "a.md", "a" * 30)
        _rules_file(cwd, "b.md", "", severity={"must": "error"})
        block = _block("worker", _scoped(["rules"]), ["x.py"], max_chars=10)
        assert _paths(block.files) == ["rules/a.md", "rules/b.md"]
        assert (block.truncated, block.omitted) == ("rules/a.md", ())

    def test_the_sweep_and_a_chunk_are_capped_separately(self, cwd):
        _rules_file(cwd, "a-java.md", "j" * 10, applies_to=["**/*.java"])
        _rules_file(cwd, "b-helm.md", "h" * 10, applies_to=["charts/**"])
        scoped = _scoped(["rules"])
        java_chunk = _block("worker", scoped, ["src/A.java"], max_chars=10)
        helm_chunk = _block("worker", scoped, ["charts/x.yaml"], max_chars=10)
        sweep = _block("sweep", scoped, ["src/A.java", "charts/x.yaml"], max_chars=10)
        assert (java_chunk.truncated, java_chunk.omitted) == (None, ())
        assert (helm_chunk.truncated, helm_chunk.omitted) == (None, ())
        assert (sweep.truncated, sweep.omitted) == (None, ("rules/b-helm.md",))


class TestMergedSeverityMap:
    def test_always_on_words_come_first_then_words_only_scoped_files_map(self, cwd):
        always_on = _always_on(cwd, severity={"blocker": "error", "nit": "outofscope"})
        scoped = _scoped(
            [_java(cwd, severity={"must": "error", "nit": "outofscope"}), _helm(cwd, severity={"meh": "warning"})],
            always_on=always_on,
        )
        merged = scoped.merged_severity_map(always_on)
        assert list(merged.items()) == [
            ("blocker", "error"), ("nit", "outofscope"), ("must", "error"), ("meh", "warning"),
        ]

    def test_with_no_always_on_file_it_is_the_scoped_map(self, cwd):
        scoped = _scoped([_java(cwd, severity={"must": "error"}), _helm(cwd, severity={"meh": "warning"})])
        assert scoped.merged_severity_map(None) == {"must": "error", "meh": "warning"}

    def test_an_always_on_file_with_no_map_adds_nothing(self, cwd):
        always_on = _always_on(cwd)
        scoped = _scoped([_java(cwd, severity={"must": "error"})], always_on=always_on)
        assert scoped.merged_severity_map(always_on) == {"must": "error"}

    def test_no_map_anywhere_is_empty(self, cwd):
        assert _scoped([_java(cwd)]).merged_severity_map(None) == {}

    def test_the_result_is_a_fresh_dict(self, cwd):
        always_on = _always_on(cwd, severity={"blocker": "error"})
        scoped = _scoped([_java(cwd, severity={"must": "error"})], always_on=always_on)
        merged = scoped.merged_severity_map(always_on)
        merged["other"] = "warning"
        assert scoped.severity_map == {"must": "error"}
        assert always_on.severity_map == {"blocker": "error"}


class TestRecord:
    def test_the_record_shape_byte_for_byte(self, cwd):
        java = _java(cwd, severity={"blocker": "error"})
        general = _general(cwd)
        scoped = _scoped(["rules", general])
        record = scoped.record()
        assert list(record) == ["entries", "files"]
        assert record == {
            "entries": ["rules", general],
            "files": [
                {
                    "path": java,
                    "sha256": hashlib.sha256((cwd / java).read_bytes()).hexdigest(),
                    "chars": len(JAVA_BODY),
                    "max_chars": 12000,
                    "truncated": False,
                    "severity_map": {"blocker": "error"},
                    "applies_to": list(JAVA_GLOBS),
                },
                {
                    "path": general,
                    "sha256": hashlib.sha256((cwd / general).read_bytes()).hexdigest(),
                    "chars": len(GENERAL_BODY),
                    "max_chars": 12000,
                    "truncated": False,
                    "severity_map": {},
                    "applies_to": None,
                },
            ],
        }
        for row in record["files"]:
            assert list(row) == ["path", "sha256", "chars", "max_chars", "truncated", "severity_map", "applies_to"]

    def test_each_file_row_is_its_review_rules_record_plus_applies_to(self, cwd):
        scoped = _scoped([_java(cwd), _general(cwd)])
        for row, rules in zip(scoped.record()["files"], scoped.files, strict=True):
            assert row == {**rules.record(), "applies_to": list(rules.applies_to) if rules.applies_to else None}

    def test_a_truncated_file_is_recorded_as_truncated(self, cwd):
        _rules_file(cwd, "long.md", "z" * 50)
        (row,) = _scoped(["rules"], max_chars=30).record()["files"]
        assert (row["chars"], row["max_chars"], row["truncated"]) == (50, 30, True)

    def test_directories_with_no_rules_file_record_no_files(self, cwd):
        (cwd / "empty").mkdir()
        assert _scoped(["empty"]).record() == {"entries": ["empty"], "files": []}

    def test_the_record_is_json_native_and_never_holds_rules_text(self, cwd):
        record = _scoped([_java(cwd), _general(cwd)]).record()
        dumped = json.dumps(record)
        assert json.loads(dumped) == record
        assert "Javadoc" not in dumped
        assert "secrets" not in dumped
