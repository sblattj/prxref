"""Issue #14: the judge prompt, its response parser, the per-ref cap and the cache key."""
from __future__ import annotations

import hashlib
import json
import logging
import re

import pytest

from prxref import judge, reviewer
from prxref.judge import (
    JUDGE_PROMPT_VERSION,
    JUDGE_SPLIT_MARKER,
    MAX_CREDITS_PER_AI_FINDING,
    REASON_MISSING,
    REASON_OTHER_FILE,
    REASON_OVER_CAP,
    REASON_UNKNOWN_REF,
    AIFinding,
    Grade,
    JudgeParseError,
    ai_ref_files,
    assign_refs,
    build_judge_prompt,
    human_files,
    judge_cache_key,
    judge_prompt_sha,
    parse_judge_response,
    split_judge_prompt,
)
from prxref.triage import Finding

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")

CASE = {
    "id": "case-1",
    "expected": [
        {"id": "H1", "file": "src/a.py", "line": 10, "severity": "error", "text": "Divides by zero when size is 0."},
        {"id": "H2", "file": "src/a.py", "line": 12, "severity": "warning", "text": "Leaks the file handle."},
        {"id": "H3", "file": "src/a.py", "line": 30, "severity": "minor", "text": "Misleading name."},
        {"id": "H4", "file": "src/b.py", "line": 5, "severity": "error", "text": "Token logged in clear."},
    ],
}

RECORD_FINDINGS = [
    {"file": "src/a.py", "line": 10, "severity": "error", "confidence": 0.9, "title": "Divide by zero",
     "body": "size may be 0.", "drop_reason": None},
    {"file": "src/a.py", "line": 11, "severity": "warning", "confidence": 0.4, "title": "Low confidence",
     "body": "gated out.", "drop_reason": "confidence"},
    {"file": "src/b.py", "line": 5, "severity": "error", "confidence": 0.8, "title": "Secret in log",
     "body": "token is logged.", "drop_reason": None},
    {"file": "src/c.py", "line": 1, "severity": "warning", "confidence": 0.7, "title": "Unlabelled file",
     "body": "no human finding here.", "drop_reason": None},
]

AI = assign_refs(RECORD_FINDINGS)
HUMANS = human_files(CASE)
REFS = ai_ref_files(AI)


def _reply(*rows: dict) -> str:
    return json.dumps({"grades": list(rows)})


def _row(human_id, grade, ai_ref=None) -> dict:
    return {"human_id": human_id, "grade": grade, "ai_ref": ai_ref}


def _by_id(grades: list[Grade]) -> dict[str, Grade]:
    return {g.human_id: g for g in grades}


class TestRefsAndMaps:
    def test_refs_number_post_gate_findings_only_in_record_order(self):
        assert [(f.ref, f.file, f.title) for f in AI] == [
            ("A1", "src/a.py", "Divide by zero"),
            ("A2", "src/b.py", "Secret in log"),
            ("A3", "src/c.py", "Unlabelled file"),
        ]

    def test_finding_objects_are_accepted(self):
        found = assign_refs([
            Finding("src/a.py", 3, "error", 0.9, "T", "B"),
            Finding("src/a.py", 4, "error", 0.9, "Gone", "B", drop_reason="dedup"),
        ])
        assert found == [AIFinding(ref="A1", file="src/a.py", line=3, severity="error", title="T", body="B")]

    def test_human_files_keep_label_order(self):
        assert list(HUMANS.items()) == [
            ("H1", "src/a.py"), ("H2", "src/a.py"), ("H3", "src/a.py"), ("H4", "src/b.py"),
        ]

    def test_ai_ref_files(self):
        assert REFS == {"A1": "src/a.py", "A2": "src/b.py", "A3": "src/c.py"}

    def test_a_repeated_human_id_is_refused(self):
        case = {"expected": [{"id": "H1", "file": "a"}, {"id": "H1", "file": "b"}]}
        with pytest.raises(ValueError, match="duplicate human finding id 'H1'"):
            human_files(case)

    def test_a_label_without_an_id_is_refused(self):
        with pytest.raises(ValueError, match="without an id"):
            human_files({"expected": [{"file": "a"}]})


class TestPrompt:
    def test_version_is_one(self):
        assert JUDGE_PROMPT_VERSION == 1

    def test_the_template_has_exactly_the_two_data_slots(self):
        template = reviewer.load_prompt("judge")
        assert sorted(set(_PLACEHOLDER.findall(template))) == ["ai_findings", "human_findings"]

    def test_the_prompt_fills_every_placeholder(self):
        prompt = build_judge_prompt(CASE, AI)
        assert _PLACEHOLDER.findall(prompt) == []

    def test_the_prompt_shows_labels_and_same_file_ai_findings(self):
        prompt = build_judge_prompt(CASE, AI)
        for text in ("H1", "H4", "Divides by zero when size is 0.", "Misleading name.", '"minor"'):
            assert text in prompt
        assert '"ref": "A1"' in prompt
        assert '"ref": "A2"' in prompt
        assert "A3" not in prompt
        assert "Unlabelled file" not in prompt
        assert "gated out." not in prompt

    def test_the_rendered_rows_carry_the_documented_fields(self):
        _system, user = split_judge_prompt(build_judge_prompt(CASE, AI))
        humans_block = user.split("### Human findings", 1)[1].split("### AI findings", 1)[0]
        ai_block = user.split("### AI findings", 1)[1].split("## Output Format", 1)[0]
        humans = json.loads(humans_block)
        ais = json.loads(ai_block)
        assert humans[0] == {
            "id": "H1", "file": "src/a.py", "line": 10, "severity": "error",
            "text": "Divides by zero when size is 0.",
        }
        assert [row["ref"] for row in ais] == ["A1", "A2"]
        assert set(ais[0]) == {"ref", "file", "line", "severity", "title", "body"}

    def test_a_label_without_text_renders_an_empty_text(self):
        case = {"expected": [{"id": "S1", "file": "src/a.py", "line": 2, "severity": "spec"}]}
        prompt = build_judge_prompt(case, [])
        assert '"text": ""' in prompt
        assert _PLACEHOLDER.findall(prompt) == []

    def test_a_value_quoting_a_placeholder_renders_literally(self):
        case = {"expected": [{"id": "H1", "file": "f", "line": 1, "severity": "error", "text": "see {ai_findings}"}]}
        prompt = build_judge_prompt(case, [])
        assert "see {ai_findings}" in prompt

    def test_the_split_puts_instructions_in_system_and_the_case_in_user(self):
        system, user = split_judge_prompt(build_judge_prompt(CASE, AI))
        assert JUDGE_SPLIT_MARKER not in system
        assert "## Grades" in system
        assert "Divides by zero" not in system
        assert user.startswith(JUDGE_SPLIT_MARKER)
        assert "Divides by zero when size is 0." in user
        assert '"grades"' in user

    def test_the_split_refuses_a_prompt_without_the_marker(self):
        with pytest.raises(ValueError, match="split marker"):
            split_judge_prompt("no marker here")

    def test_the_template_is_read_through_reviewer_load_prompt(self, monkeypatch):
        seen = []

        def fake_load(name):
            seen.append(name)
            return "H={human_findings}\n## Case\nA={ai_findings}"

        monkeypatch.setattr(reviewer, "load_prompt", fake_load)
        prompt = build_judge_prompt(CASE, AI)
        assert seen == ["judge"]
        assert prompt.startswith('H=[\n  {\n    "id": "H1"')

    def test_the_template_sha_is_sha256_of_the_packaged_text(self):
        text = reviewer.load_prompt("judge")
        assert judge_prompt_sha() == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert judge_prompt_sha("x") == hashlib.sha256(b"x").hexdigest()

    def test_the_prompt_name_is_not_an_overridable_review_prompt(self):
        assert judge.JUDGE_PROMPT_NAME not in {"worker", "systemic", "summary"}


class TestMalformed:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "Sorry, I cannot grade these findings.",
            "[1, 2, 3]",
            '"grades"',
            '{"verdicts": []}',
            '{"grades": {"human_id": "H1"}}',
            '{"grades": ["H1 full A1"]}',
            '{"grades": [{"grade": "full", "ai_ref": "A1"}]}',
            '{"grades": [{"human_id": null, "grade": "full", "ai_ref": "A1"}]}',
            '{"grades": [{"human_id": "H1", "ai_ref": "A1"}]}',
            '{"grades": [{"human_id": "H1", "grade": "match", "ai_ref": "A1"}]}',
            '{"grades": [{"human_id": "H1", "grade": 1, "ai_ref": "A1"}]}',
        ],
    )
    def test_malformed_output_raises(self, text):
        with pytest.raises(JudgeParseError):
            parse_judge_response(text, HUMANS, REFS)

    def test_the_error_is_a_value_error(self):
        assert issubclass(JudgeParseError, ValueError)

    def test_a_malformed_row_raises_even_for_an_unknown_id(self):
        with pytest.raises(JudgeParseError, match="grade"):
            parse_judge_response(_reply(_row("H9", "maybe")), HUMANS, REFS)

    def test_fenced_json_with_prose_still_parses(self):
        text = "Here you go:\n```json\n" + _reply(_row("H1", "full", "A1")) + "\n```\n"
        grades = parse_judge_response(text, HUMANS, REFS)
        assert _by_id(grades)["H1"] == Grade("H1", "full", "A1")

    def test_grade_case_and_whitespace_are_tolerated(self):
        grades = parse_judge_response(_reply(_row("H1", " Full ", "A1")), HUMANS, REFS)
        assert _by_id(grades)["H1"] == Grade("H1", "full", "A1")


class TestIds:
    def test_every_human_finding_gets_one_grade_in_label_order(self):
        reply = _reply(_row("H4", "partial", "A2"), _row("H2", "none"), _row("H1", "full", "A1"), _row("H3", "none"))
        grades = parse_judge_response(reply, HUMANS, REFS)
        assert grades == [
            Grade("H1", "full", "A1"),
            Grade("H2", "none", None),
            Grade("H3", "none", None),
            Grade("H4", "partial", "A2"),
        ]

    def test_an_unknown_human_id_is_dropped_with_a_warning(self, caplog):
        reply = _reply(_row("H1", "full", "A1"), _row("H99", "full", "A1"))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            grades = parse_judge_response(reply, HUMANS, REFS)
        assert "H99" not in _by_id(grades)
        assert [g.human_id for g in grades] == ["H1", "H2", "H3", "H4"]
        assert "unknown human finding 'H99'" in caplog.text

    def test_a_missing_human_id_is_graded_none(self, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref"):
            grades = parse_judge_response(_reply(_row("H1", "full", "A1")), HUMANS, REFS)
        by_id = _by_id(grades)
        assert by_id["H1"] == Grade("H1", "full", "A1")
        for human_id in ("H2", "H3", "H4"):
            assert by_id[human_id] == Grade(human_id, "none", None, REASON_MISSING)
        assert "no grade for human finding 'H2'" in caplog.text

    def test_an_empty_grades_list_grades_everything_none(self):
        grades = parse_judge_response('{"grades": []}', HUMANS, REFS)
        assert {g.grade for g in grades} == {"none"}
        assert {g.reason for g in grades} == {REASON_MISSING}

    def test_a_repeated_human_id_keeps_the_first_row(self, caplog):
        reply = _reply(_row("H1", "none"), _row("H1", "full", "A1"))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            grades = parse_judge_response(reply, HUMANS, REFS)
        assert _by_id(grades)["H1"] == Grade("H1", "none", None)
        assert "more than once" in caplog.text

    def test_integer_ids_read_as_their_decimal_string(self):
        humans = {"7": "src/a.py"}
        grades = parse_judge_response('{"grades": [{"human_id": 7, "grade": "full", "ai_ref": "A1"}]}', humans, REFS)
        assert grades == [Grade("7", "full", "A1")]


class TestRefValidation:
    def test_an_ai_ref_in_another_file_is_rejected(self, caplog):
        reply = _reply(_row("H1", "full", "A2"), _row("H4", "full", "A2"))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            grades = parse_judge_response(reply, HUMANS, REFS)
        by_id = _by_id(grades)
        assert by_id["H1"] == Grade("H1", "none", None, REASON_OTHER_FILE)
        assert by_id["H4"] == Grade("H4", "full", "A2")
        assert "in another file" in caplog.text

    def test_an_unknown_ai_ref_is_rejected(self, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref"):
            grades = parse_judge_response(_reply(_row("H1", "partial", "A9")), HUMANS, REFS)
        assert _by_id(grades)["H1"] == Grade("H1", "none", None, REASON_UNKNOWN_REF)
        assert "unknown AI finding 'A9'" in caplog.text

    @pytest.mark.parametrize("ref", [None, "", "  ", ["A1"]])
    def test_credit_without_a_usable_ref_is_rejected(self, ref):
        grades = parse_judge_response(_reply(_row("H1", "full", ref)), HUMANS, REFS)
        assert _by_id(grades)["H1"] == Grade("H1", "none", None, REASON_UNKNOWN_REF)

    def test_a_none_grade_never_keeps_a_ref(self):
        grades = parse_judge_response(_reply(_row("H1", "none", "A1")), HUMANS, REFS)
        assert _by_id(grades)["H1"] == Grade("H1", "none", None)


class TestCap:
    def test_the_cap_is_two(self):
        assert MAX_CREDITS_PER_AI_FINDING == 2

    def test_two_credits_on_one_ref_both_stand(self):
        reply = _reply(_row("H1", "full", "A1"), _row("H2", "partial", "A1"))
        by_id = _by_id(parse_judge_response(reply, HUMANS, REFS))
        assert by_id["H1"] == Grade("H1", "full", "A1")
        assert by_id["H2"] == Grade("H2", "partial", "A1")

    def test_over_cap_drops_the_excess_and_logs_it(self, caplog):
        reply = _reply(_row("H1", "full", "A1"), _row("H2", "full", "A1"), _row("H3", "full", "A1"))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            grades = parse_judge_response(reply, HUMANS, REFS)
        assert grades[:3] == [
            Grade("H1", "full", "A1"),
            Grade("H2", "full", "A1"),
            Grade("H3", "none", None, REASON_OVER_CAP),
        ]
        assert "AI finding 'A1' to 3 human findings; kept H1, H2, graded none H3 (cap 2)" in caplog.text

    def test_full_outranks_partial_under_the_cap(self):
        reply = _reply(_row("H1", "partial", "A1"), _row("H2", "full", "A1"), _row("H3", "full", "A1"))
        grades = parse_judge_response(reply, HUMANS, REFS)
        assert grades[:3] == [
            Grade("H1", "none", None, REASON_OVER_CAP),
            Grade("H2", "full", "A1"),
            Grade("H3", "full", "A1"),
        ]

    def test_the_drop_does_not_depend_on_reply_order(self):
        rows = [_row("H1", "full", "A1"), _row("H2", "partial", "A1"), _row("H3", "partial", "A1")]
        forward = parse_judge_response(_reply(*rows), HUMANS, REFS)
        backward = parse_judge_response(_reply(*reversed(rows)), HUMANS, REFS)
        assert forward == backward
        assert _by_id(forward)["H3"] == Grade("H3", "none", None, REASON_OVER_CAP)
        assert _by_id(forward)["H2"] == Grade("H2", "partial", "A1")

    def test_rejected_refs_do_not_count_toward_the_cap(self):
        humans = {"H1": "src/a.py", "H2": "src/a.py", "H3": "src/b.py"}
        reply = _reply(_row("H1", "full", "A1"), _row("H2", "full", "A1"), _row("H3", "full", "A1"))
        grades = parse_judge_response(reply, humans, REFS)
        assert grades == [
            Grade("H1", "full", "A1"),
            Grade("H2", "full", "A1"),
            Grade("H3", "none", None, REASON_OTHER_FILE),
        ]


class TestCacheKey:
    SHA = "0" * 64

    def test_the_key_is_a_sha256_hex_digest(self):
        key = judge_cache_key(self.SHA, "judge-model", CASE, AI)
        assert re.fullmatch(r"[0-9a-f]{64}", key)

    def test_the_key_is_stable_across_mapping_key_order(self):
        shuffled_case = {"expected": [dict(reversed(list(label.items()))) for label in CASE["expected"]]}
        as_rows = [dict(reversed(list(vars(f).items()))) for f in AI]
        assert judge_cache_key(self.SHA, "m", CASE, AI) == judge_cache_key(self.SHA, "m", shuffled_case, as_rows)

    def test_the_key_is_repeatable(self):
        assert judge_cache_key(self.SHA, "m", CASE, AI) == judge_cache_key(self.SHA, "m", CASE, list(AI))

    @pytest.mark.parametrize(
        "change",
        ["prompt_sha", "model", "label_text", "ai_body", "ai_order"],
    )
    def test_anything_the_judge_sees_changes_the_key(self, change):
        sha, model, case, ai = self.SHA, "m", CASE, list(AI)
        if change == "prompt_sha":
            sha = "1" * 64
        elif change == "model":
            model = "other"
        elif change == "label_text":
            case = {"expected": [{**CASE["expected"][0], "text": "reworded"}, *CASE["expected"][1:]]}
        elif change == "ai_body":
            ai = [AIFinding(**{**vars(ai[0]), "body": "changed"}), *ai[1:]]
        else:
            ai = assign_refs([RECORD_FINDINGS[2], RECORD_FINDINGS[0]])
        assert judge_cache_key(sha, model, case, ai) != judge_cache_key(self.SHA, "m", CASE, AI)

    def test_fields_the_prompt_omits_do_not_change_the_key(self):
        case = {"expected": [{**label, "category": "security", "accepted": True} for label in CASE["expected"]]}
        rows = [{**vars(f), "confidence": 0.1} for f in AI]
        assert judge_cache_key(self.SHA, "m", case, rows) == judge_cache_key(self.SHA, "m", CASE, AI)

    def test_ai_findings_in_unlabelled_files_do_not_change_the_key(self):
        assert judge_cache_key(self.SHA, "m", CASE, AI[:2]) == judge_cache_key(self.SHA, "m", CASE, AI)


class TestRoundTrip:
    def test_a_stub_judge_reply_grades_the_case(self):
        prompt = build_judge_prompt(CASE, AI)
        assert '"ref": "A1"' in prompt
        reply = "```json\n" + _reply(_row("H1", "full", "A1"), _row("H4", "partial", "A2")) + "\n```"
        grades = parse_judge_response(reply, human_files(CASE), ai_ref_files(AI))
        assert [(g.human_id, g.grade, g.ai_ref) for g in grades] == [
            ("H1", "full", "A1"),
            ("H2", "none", None),
            ("H3", "none", None),
            ("H4", "partial", "A2"),
        ]
