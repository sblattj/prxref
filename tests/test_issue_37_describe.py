"""PR auto-description and auto-labels, the describe side (#37).

Covers the pure helpers in :mod:`prxref.describe` (the generated-block
marker, compose/split round-trip, the write decision, the review view and the
label picker), the LLM call's defensive parse, and the two settings
``PRXREF_DESCRIBE`` and ``PRXREF_LABELS``.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta, timezone

import pytest

from prxref import config, describe
from prxref.describe import (
    CONVENTIONAL_LABELS,
    DESCRIBE_MODES,
    LABEL_MODES,
    DescribeResult,
    compose_description,
    decide_write,
    generate_description,
    pick_labels,
    review_description,
    split_description,
)
from prxref.llm import ConfigError, InvokeResult

AT = datetime(2026, 10, 14, 12, 30, 5, tzinfo=UTC)
HEAD = "0123456789abcdef0123456789abcdef01234567"


def compose(generated: str = "## Summary\nAdds a thing.", original: str = "") -> str:
    return compose_description(generated, original, head_sha=HEAD, at=AT)


# --------------------------------------------------------------------------
# marker + compose/split
# --------------------------------------------------------------------------


class TestMarker:
    def test_the_block_opens_with_a_versioned_marker_carrying_head_and_time(self):
        body = compose()
        assert body.startswith(
            f"<!-- prxref:generated v1 head={HEAD} at=2026-10-14T12:30:05Z -->\n"
        )
        assert describe.GENERATED_CLOSE in body

    def test_split_reads_the_pinned_head_and_time_back(self):
        parts = split_description(compose())
        assert parts.head == HEAD
        assert parts.at == AT
        assert parts.generated == "## Summary\nAdds a thing."

    def test_a_non_utc_time_is_pinned_in_utc(self):
        est = timezone(timedelta(hours=-5))
        body = compose_description("x", "", head_sha=HEAD, at=AT.astimezone(est))
        assert "at=2026-10-14T12:30:05Z" in body
        assert split_description(body).at == AT

    def test_a_naive_time_is_read_as_utc(self):
        body = compose_description("x", "", head_sha=HEAD, at=AT.replace(tzinfo=None))
        assert split_description(body).at == AT

    def test_a_head_with_whitespace_is_refused(self):
        with pytest.raises(ValueError):
            compose_description("x", "", head_sha="abc def", at=AT)


class TestRoundTrip:
    @pytest.mark.parametrize("original", [
        "",
        "Fixes the login bug.",
        "  leading and trailing whitespace kept  \n\n",
        "Multi\nline\n\n- list\n- items\n",
        "contains </details> and <details><summary>x</summary> html",
        f"contains the close marker {describe.ORIGINAL_CLOSE} inline",
        f"line one\n{describe.ORIGINAL_CLOSE}\n\n</details>\nline four",
        f"contains the generated close {describe.GENERATED_CLOSE} too",
        "contains <!-- prxref:generated v1 head=abc at=2020-01-01T00:00:00Z --> an open marker",
        "{diff} braces and \\backslashes\\",
    ])
    def test_split_of_compose_returns_the_original_verbatim(self, original):
        parts = split_description(compose(original=original))
        assert parts.original == original
        assert parts.generated == "## Summary\nAdds a thing."
        assert parts.head == HEAD

    def test_the_original_sits_in_a_collapsed_details_section(self):
        body = compose(original="Human text.")
        assert "<details><summary>Original description</summary>" in body
        assert body.index(describe.GENERATED_CLOSE) < body.index("Human text.")

    def test_an_empty_original_omits_the_details_section(self):
        assert "<details>" not in compose(original="")
        assert "<details>" not in compose(original="   \n")

    def test_a_whitespace_only_original_reads_back_as_empty(self):
        assert split_description(compose(original="  \n")).original == ""

    def test_regeneration_does_not_nest(self):
        first = compose(generated="v1 text", original="Human text.")
        second = compose_description(
            "v2 text", split_description(first).original, head_sha="f" * 40, at=AT,
        )
        assert second.count(describe.GENERATED_OPEN_PREFIX) == 1
        assert second.count("<details><summary>Original description</summary>") == 1
        parts = split_description(second)
        assert parts.original == "Human text."
        assert parts.generated == "v2 text"
        assert parts.head == "f" * 40

    def test_generated_text_cannot_smuggle_markers(self):
        evil = f"before {describe.GENERATED_CLOSE} after <!-- prxref:original v1 --> x"
        parts = split_description(compose(generated=evil, original="Human."))
        assert parts.original == "Human."
        assert "prxref:" not in parts.generated


class TestSplitRobustness:
    def test_an_empty_body(self):
        parts = split_description("")
        assert (parts.original, parts.generated, parts.head, parts.at) == ("", None, None, None)

    def test_none_reads_as_empty(self):
        assert split_description(None).original == ""  # type: ignore[arg-type]

    def test_a_marker_free_body_is_all_original(self):
        text = "Just a human description.\n"
        parts = split_description(text)
        assert parts.original == text
        assert parts.generated is None and parts.head is None and parts.at is None

    def test_an_unclosed_open_marker_is_treated_as_human_text(self):
        text = "<!-- prxref:generated v1 head=abc at=2026-01-01T00:00:00Z -->\nno close"
        parts = split_description(text)
        assert parts.generated is None
        assert parts.original == text

    def test_human_text_added_above_the_block_is_kept_as_original(self):
        body = "Reviewer note: see ticket.\n\n" + compose(original="Orig.")
        parts = split_description(body)
        assert parts.generated == "## Summary\nAdds a thing."
        assert "Reviewer note: see ticket." in parts.original
        assert "Orig." in parts.original
        assert parts.edited

    def test_human_text_added_below_the_block_is_kept_as_original(self):
        body = compose() + "\n\nAdded later by a human."
        parts = split_description(body)
        assert parts.original == "Added later by a human."
        assert parts.edited

    def test_a_composed_body_is_not_edited(self):
        assert not split_description(compose(original="Orig.")).edited

    def test_a_human_who_deleted_the_generated_part_keeps_their_original(self):
        body = compose(original="Orig.")
        cut = body[body.index(describe.GENERATED_CLOSE) + len(describe.GENERATED_CLOSE):]
        parts = split_description(cut)
        assert parts.generated is None
        assert parts.original == "Orig."

    def test_a_crlf_body_from_a_forge_still_splits(self):
        body = compose(original="Orig.").replace("\n", "\r\n")
        parts = split_description(body)
        assert parts.generated == "## Summary\r\nAdds a thing."
        assert parts.original == "Orig."

    def test_a_malformed_time_reads_as_unknown_not_a_crash(self):
        body = compose().replace("at=2026-10-14T12:30:05Z", "at=yesterday")
        parts = split_description(body)
        assert parts.generated is not None
        assert parts.at is None


# --------------------------------------------------------------------------
# decide_write
# --------------------------------------------------------------------------


class TestDecideWrite:
    def test_off_never_writes(self):
        assert decide_write("", "off")[0] is False
        assert decide_write("Human.", "off")[0] is False

    @pytest.mark.parametrize("body", ["", "   \n\t"])
    def test_empty_writes_an_empty_body(self, body):
        write, reason = decide_write(body, "empty")
        assert write is True
        assert reason

    def test_empty_regenerates_a_body_that_is_only_a_previous_block(self):
        write, reason = decide_write(compose(), "empty")
        assert write is True
        assert "previous" in reason

    def test_empty_never_overwrites_a_human_written_body(self):
        write, reason = decide_write("Fixes the login bug.", "empty")
        assert write is False
        assert "force" in reason

    def test_empty_refuses_a_block_that_preserves_a_human_original(self):
        assert decide_write(compose(original="Human."), "empty")[0] is False

    def test_empty_refuses_a_block_a_human_has_since_edited_around(self):
        assert decide_write(compose() + "\n\nHuman addition.", "empty")[0] is False

    def test_force_writes_a_human_written_body(self):
        write, reason = decide_write("Fixes the login bug.", "force")
        assert write is True
        assert "preserved" in reason

    def test_force_preserves_the_original_through_compose(self):
        current = "Fixes the login bug."
        assert decide_write(current, "force")[0]
        new = compose_description(
            "generated", split_description(current).original, head_sha=HEAD, at=AT,
        )
        assert split_description(new).original == current
        assert current in new

    def test_an_unknown_mode_raises(self):
        with pytest.raises(ValueError):
            decide_write("", "always")

    def test_the_mode_vocabulary(self):
        assert DESCRIBE_MODES == ("off", "empty", "force")


# --------------------------------------------------------------------------
# review_description
# --------------------------------------------------------------------------


class TestReviewDescription:
    def test_a_marker_free_body_is_returned_unchanged(self):
        assert review_description("Human text.\n") == "Human text.\n"

    def test_the_generated_block_is_stripped(self):
        out = review_description(compose(generated="GENERATED CLAIM", original="Human intent."))
        assert out == "Human intent."
        assert "GENERATED CLAIM" not in out
        assert "prxref:" not in out

    def test_a_generated_only_body_reviews_as_no_description(self):
        assert review_description(compose()) == ""

    def test_empty_and_none(self):
        assert review_description("") == ""
        assert review_description(None) == ""  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# labels
# --------------------------------------------------------------------------


class TestPickLabels:
    def test_the_conventional_set(self):
        assert CONVENTIONAL_LABELS == (
            "feat", "fix", "docs", "refactor", "test", "chore", "ci", "perf", "breaking",
        )
        assert LABEL_MODES == ("off", "existing", "create")

    def test_off_applies_nothing(self):
        assert pick_labels(["feat"], ["feat"], "off") == ([], False)

    def test_existing_returns_the_repos_spelling_and_never_creates(self):
        assert pick_labels(["fix"], ["Fix", "docs"], "existing") == (["Fix"], False)

    def test_existing_skips_a_label_the_repo_lacks(self):
        assert pick_labels(["perf", "fix"], ["fix"], "existing") == (["fix"], False)

    @pytest.mark.parametrize("proposed,existing,expected", [
        ("feat", ["feature"], ["feature"]),
        ("feat", ["enhancement"], ["enhancement"]),
        ("fix", ["bug"], ["bug"]),
        ("fix", ["BugFix"], ["BugFix"]),
        ("docs", ["documentation"], ["documentation"]),
    ])
    def test_existing_maps_common_aliases(self, proposed, existing, expected):
        assert pick_labels([proposed], existing, "existing") == (expected, False)

    def test_existing_picks_one_label_per_proposal_preferring_the_exact_name(self):
        assert pick_labels(["feat"], ["Feature", "enhancement"], "existing") == (["Feature"], False)
        assert pick_labels(["feat"], ["enhancement", "FEAT"], "existing") == (["FEAT"], False)

    def test_existing_without_a_label_list_applies_nothing(self):
        assert pick_labels(["feat"], None, "existing") == ([], False)

    def test_create_returns_conventional_names_and_create_missing(self):
        assert pick_labels(["Feature", "bug", "fix"], None, "create") == (["feat", "fix"], True)

    def test_unknown_proposals_are_dropped(self):
        assert pick_labels(["urgent", "wontfix", "", 7], ["urgent"], "existing") == ([], False)  # type: ignore[list-item]
        assert pick_labels(["urgent"], None, "create") == ([], True)

    def test_an_unknown_mode_raises(self):
        with pytest.raises(ValueError):
            pick_labels(["feat"], [], "all")


# --------------------------------------------------------------------------
# generate_description
# --------------------------------------------------------------------------


class FakeLLM:
    def __init__(self, text: str = "", *, raises: Exception | None = None, finish_reason: str = "stop"):
        self.text = text
        self.raises = raises
        self.finish_reason = finish_reason
        self.calls: list[dict] = []

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=None):
        self.calls.append({
            "system": system, "user": user, "max_tokens": max_tokens,
            "json_mode": json_mode, "timeout_s": timeout_s,
        })
        if self.raises is not None:
            raise self.raises
        return InvokeResult(
            text=self.text, input_tokens=120, output_tokens=40, model="m1",
            backend="fake", finish_reason=self.finish_reason, cost_usd=0.002,
            cost_source="usage.cost",
        )


def reply(**obj) -> str:
    return json.dumps(obj)


class TestGenerateDescription:
    def test_a_good_reply(self):
        llm = FakeLLM(reply(description="## Summary\nDoes X.", labels=["feat", "Bug", "urgent"]))
        res = generate_description(
            llm, title="Add X", diff="+x\n", original="Author text {diff}", max_tokens=2048,
        )
        assert isinstance(res, DescribeResult)
        assert res.body == "## Summary\nDoes X."
        assert res.labels == ["feat", "fix"]
        assert res.error == ""
        assert (res.input_tokens, res.output_tokens, res.model) == (120, 40, "m1")
        assert (res.cost_usd, res.cost_source) == (0.002, "usage.cost")
        call = llm.calls[0]
        assert call["max_tokens"] == 2048 and call["json_mode"] is True
        assert "Add X" in call["user"] and "+x" in call["user"]
        assert "Author text {diff}" in call["user"]
        assert "Add X" not in call["system"]

    def test_the_unit_feeds_the_run_cost(self):
        from prxref import costs

        res = generate_description(FakeLLM(reply(description="d")), title="t", diff="", original="", max_tokens=10)
        assert costs.run_cost([res.unit()], None) == (0.002, False, [])

    def test_the_timeout_is_left_to_the_backend_unless_given(self):
        llm = FakeLLM(reply(description="d"))
        generate_description(llm, title="t", diff="", original="", max_tokens=10)
        generate_description(llm, title="t", diff="", original="", max_tokens=10, timeout_s=30.0)
        assert [c["timeout_s"] for c in llm.calls] == [None, 30.0]

    def test_fenced_json_is_read(self):
        llm = FakeLLM("```json\n" + reply(description="d", labels=["docs"]) + "\n```")
        res = generate_description(llm, title="t", diff="", original="", max_tokens=10)
        assert (res.body, res.labels) == ("d", ["docs"])

    @pytest.mark.parametrize("text", [
        "not json at all",
        "",
        "[1, 2, 3]",
        '"just a string"',
        reply(labels=["feat"]),
        reply(description=17),
        reply(description="   "),
    ])
    def test_bad_output_yields_no_description_and_never_raises(self, text, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref"):
            res = generate_description(FakeLLM(text), title="t", diff="", original="", max_tokens=10)
        assert res.body == ""
        assert res.error
        assert any("describe" in r.getMessage() for r in caplog.records)

    def test_bad_labels_are_ignored(self):
        res = generate_description(
            FakeLLM(reply(description="d", labels="feat")), title="t", diff="", original="", max_tokens=10,
        )
        assert res.body == "d" and res.labels == []

    def test_an_invoke_that_raises_is_an_error_not_a_crash(self):
        res = generate_description(
            FakeLLM(raises=TimeoutError("deadline")), title="t", diff="", original="", max_tokens=10,
        )
        assert res.body == ""
        assert "TimeoutError" in res.error
        assert res.model == "" and res.input_tokens == 0

    def test_a_truncated_unparseable_reply_names_the_budget(self):
        res = generate_description(
            FakeLLM('{"description": "cut', finish_reason="length"),
            title="t", diff="", original="", max_tokens=64,
        )
        assert res.body == ""
        assert "max_tokens=64" in res.error and "PRXREF_LLM_MAX_TOKENS" in res.error

    def test_markers_in_the_reply_are_removed(self):
        res = generate_description(
            FakeLLM(reply(description=f"x {describe.GENERATED_CLOSE} y")),
            title="t", diff="", original="", max_tokens=10,
        )
        assert "prxref:" not in res.body

    def test_a_long_diff_is_capped(self):
        llm = FakeLLM(reply(description="d"))
        generate_description(llm, title="t", diff="+" * 500, original="", max_tokens=10, max_diff_chars=100)
        user = llm.calls[0]["user"]
        assert "+" * 101 not in user
        assert "truncated" in user

    def test_the_original_passed_to_the_model_excludes_a_previous_generated_block(self):
        llm = FakeLLM(reply(description="d"))
        generate_description(
            llm, title="t", diff="", original=compose(generated="OLD GENERATED", original="Human."),
            max_tokens=10,
        )
        assert "OLD GENERATED" not in llm.calls[0]["user"]
        assert "Human." in llm.calls[0]["user"]

    def test_the_prompt_is_packaged(self):
        from prxref.reviewer import load_prompt

        text = load_prompt("describe")
        assert describe.DESCRIBE_SPLIT_MARKER in text
        for slot in ("{title}", "{original}", "{diff}"):
            assert slot in text.partition(describe.DESCRIBE_SPLIT_MARKER)[2]


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------


class TestSettings:
    def test_both_default_off(self):
        cfg = config.load_config()
        assert cfg["describe"] == "off"
        assert cfg["labels"] == "off"

    @pytest.mark.parametrize("value", DESCRIBE_MODES)
    def test_describe_accepts_its_vocabulary(self, value, monkeypatch):
        monkeypatch.setenv("PRXREF_DESCRIBE", value)
        assert config.load_config()["describe"] == value

    @pytest.mark.parametrize("value", LABEL_MODES)
    def test_labels_accepts_its_vocabulary(self, value, monkeypatch):
        monkeypatch.setenv("PRXREF_LABELS", value)
        assert config.load_config()["labels"] == value

    @pytest.mark.parametrize("key,value", [
        ("PRXREF_DESCRIBE", "always"), ("PRXREF_DESCRIBE", "on"), ("PRXREF_DESCRIBE", "Force"),
        ("PRXREF_LABELS", "on"), ("PRXREF_LABELS", "all"),
    ])
    def test_an_unknown_value_is_a_configuration_error(self, key, value, monkeypatch):
        monkeypatch.setenv(key, value)
        with pytest.raises(ConfigError, match=key):
            config.load_config()

    def test_the_vocabularies_match_the_module(self):
        assert config._CHOICE_KEYS["describe"] == frozenset(DESCRIBE_MODES)
        assert config._CHOICE_KEYS["labels"] == frozenset(LABEL_MODES)

    def test_both_are_config_file_keys(self, tmp_path):
        assert {"describe", "labels"} <= config.FILE_KEYS
        path = tmp_path / ".prxref.toml"
        path.write_text('describe = "empty"\nlabels = "existing"\n', encoding="utf-8")
        cfg = config.load_config(config_file=path)
        assert (cfg["describe"], cfg["labels"]) == ("empty", "existing")

    def test_the_environment_beats_the_file(self, tmp_path, monkeypatch):
        path = tmp_path / ".prxref.toml"
        path.write_text('describe = "empty"\n', encoding="utf-8")
        monkeypatch.setenv("PRXREF_DESCRIBE", "force")
        assert config.load_config(config_file=path)["describe"] == "force"

    def test_a_bad_file_value_names_the_file_key(self, tmp_path):
        path = tmp_path / ".prxref.toml"
        path.write_text('labels = "everything"\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="labels"):
            config.load_config(config_file=path)

    def test_the_help_text_documents_both(self):
        doc = config.__doc__ or ""
        assert "PRXREF_DESCRIBE" in doc and "PRXREF_LABELS" in doc
