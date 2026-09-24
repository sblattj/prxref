"""Issue #12 T2: the ``applies_to`` front-matter key of a scoped rules file.

``parse_applies_to`` reads the fence ``split_front_matter`` reads and returns
the file's path globs in file order, or ``None`` when the file has no such key
and so applies to every unit. What is pinned here:

- the three value forms (a scalar, a comma-separated scalar, a list, as a flow
  list or as indented ``- <glob>`` lines) and the ``applyTo`` alias;
- ``None`` for an absent key, and a ConfigError, never ``()``, for an empty one;
- the exact ``<source>: <path>:<line>:`` message of every rejected value;
- the new ``ReviewRules.applies_to`` field, which no prompt or record reads;
- that the always-on ``PRXREF_REVIEW_RULES`` file parses and prompts byte for
  byte as it did before, and only warns about an ``applies_to`` it ignores.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import replace

import pytest

from prxref.llm import ConfigError
from prxref.rules import (
    APPLIES_TO_KEYS,
    ReviewRules,
    load_review_rules,
    parse_applies_to,
    split_front_matter,
)
from prxref.text_inputs import cap_text
from tests.test_issue_63_review_rules import BODY, MAP, SKILL

SOURCE = "PRXREF_SCOPED_RULES"
PATH = "rules/java.md"
JAVA = "**/*.java"
NOT_TESTS = "!**/src/test/**"
UNITS = ("worker", "sweep")

GOLDEN_SKILL_BLOCKS = {
    "worker": ("492058d625ae4dc7ccadc61403aa903866ae06b7af22de39d98fe7ba2915efdc", 765),
    "sweep": ("89e91832bc7eb5c6be983d8e0030184bfcfb4b0514ccd0970e8197f4460605c8", 968),
}
"""sha256 and length of the issue #63 SKILL file's prompt blocks, measured at
daec2d0, before any ``applies_to`` code existed."""


def _fm(*lines: str, body: str = "# Java\n- rule\n") -> str:
    return "---\n" + "".join(f"{line}\n" for line in lines) + "---\n" + body


def _parse(text: str):
    return parse_applies_to(text, source=SOURCE, path=PATH)


def _error(text: str) -> str:
    with pytest.raises(ConfigError) as exc:
        _parse(text)
    return str(exc.value)


def _empty(name: str = "applies_to") -> str:
    return f"'{name}' is empty; list at least one glob, or omit the key to apply the file to every unit"


def _not_a_string(value: str) -> str:
    return (
        f"'applies_to' entries must be glob strings, got {value!r}; "
        "quote a glob that YAML reads as another type"
    )


def _not_a_json_string(shown: str) -> str:
    return f"'applies_to' entries must be glob strings, got {shown}"


def _bad_flow(value: str) -> str:
    return (
        "'applies_to' flow list must be JSON-style, with double-quoted globs such as "
        f'["**/*.java"], got {value!r}'
    )


def _bad_quote(value: str) -> str:
    return f"'applies_to' has a malformed quoted string, got {value!r}"


def _bad_negation(glob: str) -> str:
    return f"'applies_to' entry {glob!r} must put its glob right after '!', as in '!**/test/**'"


def _leading_slash(glob: str) -> str:
    return (
        f"'applies_to' entry {glob!r} starts with '/', but diff paths are relative to the "
        "repository root; drop the leading '/'"
    )


EMPTY_ENTRY = "'applies_to' has an empty entry; every entry must be a glob"
ONLY_NEGATED = (
    "'applies_to' has only negated ('!') globs, so it matches no path; add a glob the file applies to"
)
BLOCK_SCALAR = (
    "'applies_to' cannot be a block scalar ('|' or '>'); give a glob, a comma-separated "
    "string of globs, or a list"
)
FLOW_ACROSS_LINES = (
    "'applies_to' flow list must close on the line that opens it; write a long list as "
    "indented '- <glob>' lines"
)
SCALAR_ACROSS_LINES = "'applies_to' has a value after the colon, so it cannot continue on an indented line"


def _not_an_entry(raw: str) -> str:
    return f"'applies_to' list entries must be '- <glob>' lines, got {raw!r}"


ACCEPTS = [
    ("a bare scalar", _fm("applies_to: **/*.java"), (JAVA,)),
    ("a double-quoted scalar", _fm('applies_to: "**/*.java"'), (JAVA,)),
    ("a single-quoted scalar with an escaped quote", _fm("applies_to: 'docs/it''s/*.md'"), ("docs/it's/*.md",)),
    ("a comma-separated bare scalar", _fm("applies_to: **/*.java, !**/src/test/**"), (JAVA, NOT_TESTS)),
    ("a comma-separated quoted scalar", _fm('applies_to: "**/*.ts,**/*.tsx"'), ("**/*.ts", "**/*.tsx")),
    ("the issue's flow list", _fm('applies_to: ["**/*.java", "!**/src/test/**"]'), (JAVA, NOT_TESTS)),
    ("a flow entry keeps its comma", _fm('applies_to: ["src/a,b/**", "**/*.md"]'), ("src/a,b/**", "**/*.md")),
    ("flow entries are stripped", _fm('applies_to: [ " **/*.java " ]'), (JAVA,)),
    (
        "a block list, bare and quoted",
        _fm("applies_to:", "  - **/*.java", '  - "!**/src/test/**"', "  - 'helm/**'"),
        (JAVA, NOT_TESTS, "helm/**"),
    ),
    ("a block entry keeps its comma", _fm("applies_to:", '  - "a,b/*.md"'), ("a,b/*.md",)),
    (
        "comments, blank lines and a tab in a block list",
        _fm(
            "applies_to:   # java only", "", "  # production code", "\t- **/*.java  # all of it",
            "  - '!**/src/test/**'",
        ),
        (JAVA, NOT_TESTS),
    ),
    ("a comment after a flow list", _fm('applies_to: ["**/*.java"]  # java'), (JAVA,)),
    ("a negation may come first", _fm('applies_to: ["!**/gen/**", "**/*.py"]'), ("!**/gen/**", "**/*.py")),
    ("a glob with an inner space", _fm('applies_to: "docs/team notes/*.md"'), ("docs/team notes/*.md",)),
    ("a colon not followed by a space", _fm("applies_to: src/a:b/*.md"), ("src/a:b/*.md",)),
    ("a number-like glob when quoted", _fm("applies_to:", '  - "2024"', "  - '2025'"), ("2024", "2025")),
    ("a repeated glob is kept in order", _fm("applies_to: a/**, b/**, a/**"), ("a/**", "b/**", "a/**")),
    (
        "beside skill keys, a block scalar and a severity map",
        _fm(
            "name: java", "description: |", "  applies_to: prose inside a block scalar",
            "severity:", "  blocker: error", "applies_to:", "  - **/*.java",
        ),
        (JAVA,),
    ),
    ("tabs and trailing blanks on the fences", "---  \napplies_to: **/*.java\n---\t\nbody", (JAVA,)),
]

ABSENT = [
    ("no front matter", "# Rules\n- be nice\n"),
    ("front matter without the key", _fm("severity:", "  blocker: error")),
    ("the issue #63 skill file", SKILL),
    ("the key only inside a block scalar", _fm("description: |", "  applies_to: **/*.java")),
    ("the key only in the body", _fm("name: x", body="applies_to: **/*.java\n")),
    ("an unclosed fence", "---\napplies_to: **/*.java\n# body\n"),
    ("a fence that is not the first line", "\n---\napplies_to: **/*.java\n---\n"),
    ("an empty file", ""),
    ("keys that only resemble it", _fm("applies_tox: **/*.java", "apply_to: **/*.java", "appliesTo: x/**")),
]

REJECTS = [
    ("an empty flow list", _fm("applies_to: []"), 2, _empty()),
    ("an empty flow list with a space", _fm("applies_to: [ ]"), 2, _empty()),
    ("no value and no entries", _fm("applies_to:", "severity:", "  blocker: error"), 2, _empty()),
    ("no value at the end of the front matter", _fm("applies_to:   # todo"), 2, _empty()),
    ("an empty quoted scalar", _fm('applies_to: ""'), 2, _empty()),
    ("a blank quoted scalar", _fm("applies_to: '  '"), 2, _empty()),
    ("a YAML null", _fm("applies_to: ~"), 2, _empty()),
    ("a YAML null word", _fm("applies_to: null"), 2, _empty()),
    ("the alias, empty", _fm("applyTo: []"), 2, _empty("applyTo")),
    ("line numbers count comments and blanks", _fm("# scope", "", "name: x", "applies_to: []"), 5, _empty()),
    ("an empty entry in a comma list", _fm("applies_to: a/**,,b/**"), 2, EMPTY_ENTRY),
    ("a trailing comma", _fm("applies_to: a/**,"), 2, EMPTY_ENTRY),
    ("an empty flow entry", _fm('applies_to: ["a/**", "  "]'), 2, EMPTY_ENTRY),
    ("a bare dash", _fm("applies_to:", "  - a/**", "  -"), 4, EMPTY_ENTRY),
    ("an empty quoted block entry", _fm("applies_to:", '  - ""'), 3, EMPTY_ENTRY),
    ("a number in a flow list", _fm('applies_to: [1, "a/**"]'), 2, _not_a_json_string("1")),
    ("a null in a flow list", _fm('applies_to: ["a/**", null]'), 2, _not_a_json_string("null")),
    ("a boolean in a flow list", _fm("applies_to: [true]"), 2, _not_a_json_string("true")),
    ("a list in a flow list", _fm('applies_to: [["a/**"]]'), 2, _not_a_json_string('["a/**"]')),
    ("a mapping in a flow list", _fm('applies_to: [{"a": "b"}]'), 2, _not_a_json_string('{"a": "b"}')),
    ("a number in a block list", _fm("applies_to:", "  - 42"), 3, _not_a_string("42")),
    ("a boolean in a block list", _fm("applies_to:", "  - a/**", "  - True"), 4, _not_a_string("True")),
    ("a null in a block list", _fm("applies_to:", "  - ~"), 3, _not_a_string("~")),
    ("a float in a block list", _fm("applies_to:", "  - 1.5e3"), 3, _not_a_string("1.5e3")),
    ("a mapping in a block list", _fm("applies_to:", "  - a: b"), 3, _not_a_string("a: b")),
    ("a key with no value in a block list", _fm("applies_to:", "  - src:"), 3, _not_a_string("src:")),
    ("a flow list in a block list", _fm("applies_to:", "  - [a, b]"), 3, _not_a_string("[a, b]")),
    ("a flow mapping in a block list", _fm("applies_to:", "  - {a: b}"), 3, _not_a_string("{a: b}")),
    ("a nested block list", _fm("applies_to:", "  - - a/**"), 3, _not_a_string("- a/**")),
    ("a number scalar", _fm("applies_to: 42"), 2, _not_a_string("42")),
    ("a flow mapping scalar", _fm("applies_to: {a: b}"), 2, _not_a_string("{a: b}")),
    ("a single-quoted flow list", _fm("applies_to: ['**/*.java']"), 2, _bad_flow("['**/*.java']")),
    ("a bare flow list", _fm("applies_to: [**/*.java]"), 2, _bad_flow("[**/*.java]")),
    ("text after a flow list", _fm('applies_to: ["a/**"] b/**'), 2, _bad_flow('["a/**"] b/**')),
    ("a trailing comma in a flow list", _fm('applies_to: ["a/**",]'), 2, _bad_flow('["a/**",]')),
    ("a flow list that never closes", _fm('applies_to: ["a/**"'), 2, _bad_flow('["a/**"')),
    ("a flow list across lines", _fm('applies_to: ["a/**",', '  "b/**"]'), 3, FLOW_ACROSS_LINES),
    ("a scalar across lines", _fm("applies_to: a/**", "  b/**"), 3, SCALAR_ACROSS_LINES),
    ("a scalar followed by entries", _fm("applies_to: a/**", "  - b/**"), 3, SCALAR_ACROSS_LINES),
    ("an indented line that is not an entry", _fm("applies_to:", "  a/**"), 3, _not_an_entry("a/**")),
    ("a dash glued to its glob", _fm("applies_to:", "  - a/**", "  -b/**"), 4, _not_an_entry("-b/**")),
    ("an unterminated double quote", _fm("applies_to:", '  - "a/**'), 3, _bad_quote('"a/**')),
    ("an unterminated single quote", _fm("applies_to: 'a/**"), 2, _bad_quote("'a/**")),
    ("text after a quoted scalar", _fm('applies_to: "a/**" b/**'), 2, _bad_quote('"a/**" b/**')),
    ("a literal block scalar", _fm("applies_to: |", "  a/**"), 2, BLOCK_SCALAR),
    ("a folded block scalar", _fm("applies_to: >-"), 2, BLOCK_SCALAR),
    ("a bare negation", _fm("applies_to: a/**, !"), 2, _bad_negation("!")),
    ("a space after the negation", _fm('applies_to: ["a/**", "! b/**"]'), 2, _bad_negation("! b/**")),
    ("a leading slash", _fm("applies_to: /src/**"), 2, _leading_slash("/src/**")),
    (
        "a negated leading slash", _fm("applies_to:", "  - src/**", "  - '!/src/gen/**'"), 4,
        _leading_slash("!/src/gen/**"),
    ),
    ("only negated globs", _fm('applies_to: ["!**/test/**", "!**/gen/**"]'), 2, ONLY_NEGATED),
    ("only a negated block entry", _fm("applies_to:", "", "  - '!**/test/**'"), 2, ONLY_NEGATED),
    (
        "a second key", _fm("applies_to: a/**", "applies_to: b/**"), 3,
        "duplicate 'applies_to' key ('applies_to' is already set on line 2)",
    ),
    (
        "the key and its alias", _fm("applyTo: a/**", "name: x", "applies_to: b/**"), 4,
        "duplicate 'applies_to' key ('applyTo' is already set on line 2)",
    ),
]


class TestParseAppliesTo:
    @pytest.mark.parametrize(("text", "expected"), [c[1:] for c in ACCEPTS], ids=[c[0] for c in ACCEPTS])
    def test_accepted(self, text, expected):
        assert _parse(text) == expected

    @pytest.mark.parametrize("text", [c[1] for c in ABSENT], ids=[c[0] for c in ABSENT])
    def test_absent_key_is_none(self, text):
        assert _parse(text) is None

    @pytest.mark.parametrize(("text", "lineno", "problem"), [c[1:] for c in REJECTS], ids=[c[0] for c in REJECTS])
    def test_rejected_naming_source_path_and_line(self, text, lineno, problem):
        assert _error(text) == f"{SOURCE}: {PATH}:{lineno}: {problem}"

    def test_the_result_is_a_tuple_in_file_order(self):
        globs = _parse(_fm("applies_to:", "  - z/**", "  - a/**", "  - m/**"))
        assert type(globs) is tuple
        assert globs == ("z/**", "a/**", "m/**")

    def test_absent_is_none_and_empty_is_an_error_never_an_empty_tuple(self):
        assert _parse(_fm("name: x")) is None
        for empty in ("applies_to: []", "applies_to:", 'applies_to: ""'):
            with pytest.raises(ConfigError, match="is empty; list at least one glob"):
                _parse(_fm(empty))

    def test_parsing_never_logs_not_even_for_an_unclosed_fence(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="prxref"):
            assert _parse("---\napplies_to: **/*.java\n") is None
            assert _parse(_fm("applies_to: **/*.java")) == (JAVA,)
        assert caplog.records == []

    def test_the_source_and_path_are_quoted_as_given(self):
        with pytest.raises(ConfigError) as exc:
            parse_applies_to(_fm("applies_to: []"), source="--scoped-rules", path="./team rules/api.md")
        assert str(exc.value) == f"--scoped-rules: ./team rules/api.md:2: {_empty()}"


FORMS = {
    "scalar": ("{key}: **/*.java",),
    "comma-separated": ("{key}: **/*.java, !**/src/test/**",),
    "flow list": ('{key}: ["**/*.java", "!**/src/test/**"]',),
    "block list": ("{key}:", "  - **/*.java", "  - '!**/src/test/**'"),
}


class TestAlias:
    def test_the_keys_are_the_casefolded_spelling_and_its_alias(self):
        assert APPLIES_TO_KEYS == frozenset({"applies_to", "applyto"})

    @pytest.mark.parametrize("form", list(FORMS))
    @pytest.mark.parametrize("key", ["applyTo", "APPLIES_TO", "Applies_To", "applyto"])
    def test_every_spelling_reads_every_form_like_applies_to(self, key, form):
        lines = FORMS[form]
        assert _parse(_fm(*(line.format(key=key) for line in lines))) == _parse(
            _fm(*(line.format(key="applies_to") for line in lines))
        )

    def test_rejections_name_the_key_as_written(self):
        assert _error(_fm("applyTo:", "  - 42")) == f"{SOURCE}: {PATH}:3: " + _not_a_string("42").replace(
            "'applies_to'", "'applyTo'"
        )


class TestReviewRulesField:
    def _rules(self, **kwargs) -> ReviewRules:
        return ReviewRules(path="r.md", body=cap_text(BODY, 12000, sha256="0" * 64), severity_map=MAP, **kwargs)

    def test_the_field_defaults_to_none(self):
        assert self._rules().applies_to is None

    def test_no_prompt_block_or_record_reads_the_field(self):
        plain = self._rules()
        scoped = replace(plain, applies_to=(JAVA, NOT_TESTS))
        assert scoped.applies_to == (JAVA, NOT_TESTS)
        for unit in UNITS:
            assert scoped.prompt_block(unit) == plain.prompt_block(unit)
        assert scoped.record() == plain.record()
        assert "applies_to" not in plain.record()


def _write(tmp_path, text: str, name: str = "rules.md") -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _always_on(tmp_path, text: str, caplog, name: str = "rules.md"):
    path = _write(tmp_path, text, name)
    with caplog.at_level(logging.INFO, logger="prxref"):
        rules = load_review_rules(path, max_chars=12000, source="--rules-file")
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    return path, rules, infos, warnings


def _scoping_warning(path: str, keys: str) -> str:
    return (
        f"--rules-file: rules file {path!r} sets {keys}, which only a scoped rules file "
        "(PRXREF_SCOPED_RULES / --scoped-rules) reads; this file still reaches every unit"
    )


SKILL_WITH_SCOPE = SKILL.replace("severity:\n", 'applies_to: ["**/*.java"]\nseverity:\n', 1)
LEGACY_INFO = "--rules-file: ignoring front-matter keys other than 'severity': name, description"


class TestAlwaysOnFileIsUnchanged:
    def test_the_legacy_prompt_blocks_match_the_pre_change_golden_bytes(self, tmp_path, caplog):
        _, rules, infos, warnings = _always_on(tmp_path, SKILL, caplog)
        for unit in UNITS:
            block = rules.prompt_block(unit)
            assert (hashlib.sha256(block.encode("utf-8")).hexdigest(), len(block)) == GOLDEN_SKILL_BLOCKS[unit]
        assert rules.applies_to is None
        assert rules.ignored_keys == ("name", "description")
        assert (infos, warnings) == ([LEGACY_INFO], [])

    def test_an_applies_to_key_changes_no_prompt_byte_and_is_not_applied(self, tmp_path, caplog):
        path, rules, infos, warnings = _always_on(tmp_path, SKILL_WITH_SCOPE, caplog)
        legacy = load_review_rules(_write(tmp_path, SKILL, "legacy.md"), max_chars=12000, source="--rules-file")
        for unit in UNITS:
            block = rules.prompt_block(unit)
            assert block == legacy.prompt_block(unit)
            assert (hashlib.sha256(block.encode("utf-8")).hexdigest(), len(block)) == GOLDEN_SKILL_BLOCKS[unit]
        assert rules.applies_to is None
        assert rules.ignored_keys == ("name", "description", "applies_to")
        record, legacy_record = rules.record(), legacy.record()
        assert record.pop("sha256") != legacy_record.pop("sha256")
        record.pop("path")
        legacy_record.pop("path")
        assert record == legacy_record
        assert infos == [LEGACY_INFO]
        assert warnings == [_scoping_warning(path, "'applies_to'")]

    def test_split_front_matter_still_lists_the_key_as_ignored(self):
        assert split_front_matter(SKILL_WITH_SCOPE, source="--rules-file", path="r.md") == (
            MAP, ("name", "description", "applies_to"), BODY + "\n",
        )

    def test_a_scope_only_file_warns_and_logs_no_info(self, tmp_path, caplog):
        path, rules, infos, warnings = _always_on(tmp_path, _fm("applyTo: '**/*.ts'", body="- rule\n"), caplog)
        assert rules.body.text == "- rule"
        assert (infos, warnings) == ([], [_scoping_warning(path, "'applyTo'")])

    @pytest.mark.parametrize("value", ["[]", "[1", "{a: b}", "|", "/abs/**"])
    def test_the_always_on_file_does_not_validate_the_key(self, tmp_path, caplog, value):
        path, rules, _, warnings = _always_on(tmp_path, _fm(f"applies_to: {value}", body="- rule\n"), caplog)
        assert (rules.body.text, rules.applies_to) == ("- rule", None)
        assert warnings == [_scoping_warning(path, "'applies_to'")]

    def test_both_spellings_share_one_warning(self, tmp_path, caplog):
        text = _fm("applies_to: a/**", "name: x", "applyTo: b/**", body="- rule\n")
        path, rules, infos, warnings = _always_on(tmp_path, text, caplog)
        assert rules.ignored_keys == ("applies_to", "name", "applyTo")
        assert infos == ["--rules-file: ignoring front-matter keys other than 'severity': name"]
        assert warnings == [_scoping_warning(path, "'applies_to', 'applyTo'")]
