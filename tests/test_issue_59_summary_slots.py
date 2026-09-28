"""Summary-template slots, the bullet separator and the relaxed summary validator (#59).

Seat B of #59: ``summary_bullet_separator`` reaches every summary render, the
summary gains per-group slots (``{error_findings}`` ... ``{outside_ticket_section}``),
``{inline_accounting}``, ``{head_sha}``/``{head_sha_short}`` and the
``{chunk_count}``/``{input_tokens}``/``{output_tokens}`` counts, a summary
override is valid with ``{findings}`` OR a per-group slot, and a finding whose
group a template leaves out is appended under ``Other findings`` instead of
being dropped.
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from prxref import markers, orchestrator, prompt_templates
from prxref.forges.base import PRData
from prxref.llm import ConfigError
from prxref.prompt_templates import (
    SUMMARY_GROUP_SLOTS,
    SUMMARY_GROUPS,
    SUMMARY_OPTIONAL_PLACEHOLDERS,
    load_prompt_templates,
    uncovered_summary_groups,
)
from prxref.triage import Finding
from tests.test_issue_59_markers import (
    BASELINE_ALL_OUTSIDE,
    BASELINE_FALLBACK_SUMMARY,
    BASELINE_SUMMARY,
)

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PATH = ROOT / "docs" / "examples" / "summary-by-severity.md"
DOC_PATH = ROOT / "docs" / "prompt-templates.md"
SHA = "3f9c2ab71d0e4c5b8a6f9e2d1c0b7a6f5e4d3c2b"


def _pr(sha: str = SHA, title: str = "Add widget") -> PRData:
    return PRData(
        title=title, description="d", author="a", source_branch="f",
        target_branch="main", source_sha=sha, target_sha="b" * 40, raw={},
    )


def _f(file, line, sev, scope="unknown", title=None) -> Finding:
    return Finding(
        file=file, line=line, severity=sev, confidence=0.9,
        title=title or f"{sev} problem", body="body text", scope=scope,
    )


FINDINGS = [
    _f("a.py", 3, "error", "in"),
    _f("b.py", 0, "warning"),
    _f("c.py", 5, "spec", "in"),
    _f("d.py", 7, "outofscope"),
    _f("e.py", 9, "warning", "out", "outside thing"),
    _f("g.py", 2, "error", "out", "outside error"),
    _f("h.py", 4, "error", "unknown", "second error"),
]


def _render(template: str, findings=FINDINGS, *, pr=None, **kw) -> str:
    return orchestrator._render_summary(
        pr if pr is not None else _pr(), ["a.py", "b.py"], "Request-Changes", findings,
        "m", 100, 50, 1234, summary_template=template, **kw,
    )


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


@pytest.fixture
def work(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


def _prompts_dir(work: Path, summary: str) -> str:
    d = work / "prompts"
    d.mkdir()
    (d / "summary.md").write_text(summary, encoding="utf-8")
    return "prompts"


ALL_GROUP_SLOTS_TEMPLATE = "|".join(
    f"{{{slot}}}" for group in SUMMARY_GROUPS for slot in (f"{group}_findings", f"{group}_section")
)


class TestGroupFindingSlots:
    def test_each_severity_lists_its_in_ticket_and_unjudged_findings_in_order(self):
        rendered = _render(
            "E[{error_findings}]W[{warning_findings}]S[{spec_findings}]"
            "O[{outofscope_findings}]X[{outside_ticket_findings}]{attribution}"
        )
        assert rendered == (
            "E[- 🟥 `a.py:3` — error problem\n- 🟥 `h.py:4` — second error]"
            "W[- 🟧 `b.py:—` — warning problem]"
            "S[- 🔍 `c.py:5` — spec problem]"
            "O[- ⬜ `d.py:7` — outofscope problem]"
            "X[- 🟦 🟧 `e.py:9` — outside thing\n- 🟦 🟥 `g.py:2` — outside error]"
            "Reviewed by prxref · model=m · 150 tok · 1.2s"
        )

    def test_sections_carry_a_bold_marker_label_heading_and_a_trailing_newline(self):
        rendered = _render(
            "{error_section}#{warning_section}#{spec_section}#{outofscope_section}#"
            "{outside_ticket_section}#{attribution}"
        )
        assert rendered == (
            "**🟥 Errors**\n\n- 🟥 `a.py:3` — error problem\n- 🟥 `h.py:4` — second error\n#"
            "**🟧 Warnings**\n\n- 🟧 `b.py:—` — warning problem\n#"
            "**🔍 Spec**\n\n- 🔍 `c.py:5` — spec problem\n#"
            "**⬜ Minor**\n\n- ⬜ `d.py:7` — outofscope problem\n#"
            "**🟦 Outside the ticket (2)**\n\n"
            "- 🟦 🟧 `e.py:9` — outside thing\n- 🟦 🟥 `g.py:2` — outside error\n#"
            "Reviewed by prxref · model=m · 150 tok · 1.2s"
        )

    def test_the_outside_ticket_section_is_the_block_findings_carries(self):
        rendered = _render("{findings}\n=====\n{outside_ticket_section}")
        findings_part, section = rendered.split("\n=====\n", 1)
        section = section.split("\n\nReviewed by prxref")[0]
        assert section.endswith("\n")
        assert findings_part.endswith("\n\n" + section[:-1])

    def test_section_headings_follow_the_effective_glyph_table(self):
        with markers.overridden("error=🔴,warning=🟡,outofscope=⚪,spec=S,out_of_ticket=T"):
            rendered = _render(
                "{error_section}{warning_section}{spec_section}{outofscope_section}"
                "{outside_ticket_section}"
            )
        assert "**🔴 Errors**\n\n- 🔴 `a.py:3`" in rendered
        assert "**🟡 Warnings**\n\n- 🟡 `b.py:—`" in rendered
        assert "**S Spec**\n\n- S `c.py:5`" in rendered
        assert "**⚪ Minor**\n\n- ⚪ `d.py:7`" in rendered
        assert "**T Outside the ticket (2)**\n\n- T 🟡 `e.py:9`" in rendered

    def test_only_the_empty_groups_render_empty(self):
        rendered = _render(ALL_GROUP_SLOTS_TEMPLATE, [_f("a.py", 1, "warning", "in")])
        parts = rendered.split("\n\nReviewed by prxref")[0].split("|")
        assert parts[:2] == ["", ""]
        assert parts[2] == "- 🟧 `a.py:1` — warning problem"
        assert parts[3] == "**🟧 Warnings**\n\n- 🟧 `a.py:1` — warning problem\n"
        assert parts[4:] == [""] * 6

    def test_an_unknown_severity_joins_outofscope(self):
        rendered = _render("{outofscope_findings}|{outofscope_section}", [_f("z.py", 1, "bogus")])
        assert rendered.startswith(
            "- ⬜ `z.py:1` — bogus problem|**⬜ Minor**\n\n- ⬜ `z.py:1` — bogus problem\n"
        )


class TestEmptyAndCountSlots:
    def test_every_group_slot_is_empty_without_findings(self):
        rendered = _render(ALL_GROUP_SLOTS_TEMPLATE + "|{attribution}", [])
        assert rendered == "|" * 10 + "Reviewed by prxref · model=m · 150 tok · 1.2s"

    def test_chunk_and_token_counts(self):
        rendered = _render(
            "{chunk_count}/{input_tokens}/{output_tokens}\n{findings}",
            chunks_reviewed=3, chunks_failed=2,
        )
        assert rendered.startswith("5/100/50\n")

    def test_chunk_count_is_zero_on_the_summary_only_path(self):
        assert _render("{chunk_count}|{findings}").startswith("0|")


class TestHeadSha:
    def test_present(self):
        assert _render("{head_sha}|{head_sha_short}|{findings}").startswith(f"{SHA}|3f9c2ab|")

    def test_empty_when_the_forge_reported_none(self):
        assert _render("[{head_sha}|{head_sha_short}]{findings}", pr=_pr(sha="")).startswith("[|]")

    def test_empty_when_the_pr_object_has_no_source_sha(self):
        pr = SimpleNamespace(title="Add widget")
        assert _render("[{head_sha}|{head_sha_short}]{findings}", pr=pr).startswith("[|]")

    def test_a_short_sha_is_kept_whole(self):
        assert _render("{head_sha_short}|{findings}", pr=_pr(sha="abc")).startswith("abc|")


class TestBulletSeparator:
    def test_default_constant(self):
        assert orchestrator.SUMMARY_BULLET_SEPARATOR == " — "

    def test_it_reaches_findings_and_every_group_slot(self):
        rendered = _render(
            "{findings}\n#{error_findings}#{outside_ticket_section}",
            summary_bullet_separator=": ",
        )
        assert "- 🟥 `a.py:3`: error problem" in rendered
        assert "#- 🟥 `a.py:3`: error problem\n- 🟥 `h.py:4`: second error#" in rendered
        assert "- 🟦 🟧 `e.py:9`: outside thing" in rendered
        assert "` — " not in rendered

    def test_a_file_level_line_keeps_its_dash(self):
        rendered = _render("{warning_findings}", summary_bullet_separator=" -> ")
        assert rendered.startswith("- 🟧 `b.py:—` -> warning problem")

    def test_the_packaged_summary_with_a_separator_changes_only_the_bullets(self):
        default = _render("")
        custom = _render("", summary_bullet_separator=": ")
        assert custom == default.replace("` — ", "`: ")
        assert custom != default


ACCOUNTING = "Inline comments: 3 of 7 findings (4 over the 3-comment cap)."


class TestInlineAccounting:
    def test_rides_findings_when_the_template_has_no_slot_for_it(self):
        rendered = _render("{findings}\n--\n{attribution}", inline_accounting=ACCOUNTING)
        assert f"- 🟦 🟥 `g.py:2` — outside error\n\n{ACCOUNTING}\n--\n" in rendered
        assert rendered.count(ACCOUNTING) == 1

    def test_goes_only_to_its_slot_when_the_template_has_one(self):
        rendered = _render(
            "{findings}\n--\n{inline_accounting}\n{attribution}", inline_accounting=ACCOUNTING,
        )
        assert rendered.count(ACCOUNTING) == 1
        assert f"outside error\n--\n{ACCOUNTING}\nReviewed by prxref" in rendered

    def test_is_appended_when_the_template_has_neither(self):
        rendered = _render(
            ALL_GROUP_SLOTS_TEMPLATE + "\n{attribution}", inline_accounting=ACCOUNTING,
        )
        assert rendered.endswith(f"{ACCOUNTING}\n\nReviewed by prxref · model=m · 150 tok · 1.2s")

    def test_the_slot_is_empty_without_accounting(self):
        assert _render("[{inline_accounting}]{findings}").startswith("[]")


class TestDropGuard:
    def test_findings_of_an_uncovered_group_are_appended_with_a_warning(self, caplog):
        template = "{error_section}{warning_section}{outofscope_section}{outside_ticket_section}--"
        with caplog.at_level(logging.WARNING, logger="prxref.orchestrator"):
            rendered = _render(template)
        assert "**Other findings (1)**\n\n- 🔍 `c.py:5` — spec problem" in rendered
        assert _warnings(caplog) == [
            "summary template has no {findings} and no slot for the spec finding group(s); "
            "appending 1 finding(s) under 'Other findings'"
        ]

    def test_every_finding_lands_somewhere(self):
        rendered = _render("{error_findings}\n{attribution}")
        for f in FINDINGS:
            assert f"`{f.file}:" in rendered

    def test_a_covered_group_is_not_repeated(self):
        rendered = _render("{error_findings}\n{attribution}")
        assert rendered.count("`a.py:3`") == 1
        other = rendered.split("**Other findings (5)**\n\n", 1)[1]
        assert other == (
            "- 🟧 `b.py:—` — warning problem\n- 🔍 `c.py:5` — spec problem\n"
            "- ⬜ `d.py:7` — outofscope problem\n- 🟦 🟧 `e.py:9` — outside thing\n"
            "- 🟦 🟥 `g.py:2` — outside error\n\n"
            "Reviewed by prxref · model=m · 150 tok · 1.2s"
        )

    def test_other_findings_go_above_a_rule_and_the_attribution(self):
        rendered = _render("{error_findings}\n\n---\n\n{attribution}\n")
        assert rendered.endswith(
            "- 🟦 🟥 `g.py:2` — outside error\n\n---\n\n"
            "Reviewed by prxref · model=m · 150 tok · 1.2s\n"
        )

    @pytest.mark.parametrize("rule", ["-----", "***", "_ _ _", "- - -"])
    def test_any_thematic_break_above_the_attribution_stays_whole(self, rule):
        rendered = _render(f"{{error_findings}}\n\n{rule}\n\n{{attribution}}\n")
        assert rendered.endswith(
            f"- 🟦 🟥 `g.py:2` — outside error\n\n{rule}\n\n"
            "Reviewed by prxref · model=m · 150 tok · 1.2s\n"
        )

    def test_a_dash_ending_body_line_is_not_taken_for_a_rule(self):
        rendered = _render("{error_findings}\nsee a---\n{attribution}")
        assert "see a---\n\n**Other findings (5)**" in rendered

    def test_other_findings_precede_an_appended_attribution(self):
        rendered = _render("{error_findings}")
        assert rendered.endswith(
            "- 🟦 🟥 `g.py:2` — outside error\n\nReviewed by prxref · model=m · 150 tok · 1.2s"
        )

    def test_no_warning_when_the_uncovered_group_is_empty(self, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref.orchestrator"):
            rendered = _render("{error_section}{attribution}", [_f("a.py", 1, "error")])
        assert "Other findings" not in rendered
        assert _warnings(caplog) == []

    def test_no_guard_when_the_template_has_findings(self, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref.orchestrator"):
            rendered = _render("{findings}")
        assert "Other findings" not in rendered
        assert _warnings(caplog) == []

    def test_append_order_other_accounting_attribution_banner(self):
        rendered = _render(
            "{error_section}", inline_accounting=ACCOUNTING,
            chunks_reviewed=2, chunks_failed=1, failed_chunks=[("timeout", ["x.py"])],
        )
        other = rendered.index("**Other findings (5)**")
        accounting = rendered.index(ACCOUNTING)
        attribution = rendered.index("Reviewed by prxref")
        banner = rendered.index("> ⚠️ Partial review")
        assert rendered.index("**🟥 Errors**") < other < accounting < attribution < banner
        assert rendered.endswith("> - chunk of 1 file (x.py): timeout")

    def test_uncovered_summary_groups(self):
        assert uncovered_summary_groups(frozenset({"findings"})) == ()
        assert uncovered_summary_groups(frozenset()) == SUMMARY_GROUPS
        assert uncovered_summary_groups(
            frozenset({"error_findings", "warning_section", "outside_ticket_findings"})
        ) == ("spec", "outofscope")


class TestOnePass:
    def test_a_title_naming_a_slot_renders_literally(self):
        hostile = "{error_section} {findings} {head_sha} {inline_accounting} {attribution}"
        findings = [_f("a.py", 1, "error", "in", hostile)]
        rendered = _render("{error_section}\n{error_findings}\n{findings}", findings)
        assert rendered.count(f"- 🟥 `a.py:1` — {hostile}") == 3
        assert SHA not in rendered

    def test_a_pr_title_naming_a_slot_renders_literally(self):
        rendered = _render("{title}\n{findings}", pr=_pr(title="{error_section}{head_sha}"))
        assert rendered.startswith("{error_section}{head_sha}\n")


class TestDefaultsAreByteIdentical:
    """Seat A's goldens (captured at aeda4fe), plus one with a ticket note,
    out-of-ticket findings, inline accounting and a partial-review banner,
    captured at 8ff109f by /tmp/prxref-059/B-capture.py before any seat-B edit.
    """

    def test_seat_a_goldens(self):
        from tests.test_issue_59_markers import FINDINGS as A_FINDINGS
        from tests.test_issue_59_markers import PR as A_PR
        from tests.test_issue_59_markers import _spec_note

        assert orchestrator._render_summary(
            A_PR, ["a.py", "b.py"], "Request-Changes", A_FINDINGS, "m", 100, 50, 1234,
            spec_note=_spec_note(),
        ) == BASELINE_SUMMARY
        assert orchestrator._render_summary(
            A_PR, ["a.py"], "Request-Changes", A_FINDINGS, "m", 100, 50, 1234,
            spec_note=_spec_note(), summary_template=orchestrator._FALLBACK_SUMMARY_TEMPLATE,
        ) == BASELINE_FALLBACK_SUMMARY
        assert orchestrator._render_summary(
            A_PR, ["a.py"], "Approved", [A_FINDINGS[4]], "m", 1, 2, 3,
        ) == BASELINE_ALL_OUTSIDE

    def test_ticket_out_of_scope_accounting_and_banner(self):
        findings = [
            _f("a.py", 3, "error", "in"),
            _f("b.py", 0, "warning", "in"),
            _f("c.py", 5, "spec", "in"),
            _f("d.py", 7, "outofscope", "in"),
            _f("e.py", 9, "warning", "out", "outside thing"),
            _f("g.py", 2, "outofscope", "out", "outside nit"),
        ]
        accounting = orchestrator._inline_accounting(6, 4, 3, failed=False, cap=4)
        assert accounting == (
            "Inline comments: 3 of 6 findings (2 over the 4-comment cap · "
            "1 anchor rejected by the forge)."
        )
        rendered = orchestrator._render_summary(
            _pr(sha="a" * 40), ["a.py", "b.py", "c.py"], "Request-Changes", findings, "m",
            1000, 250, 4321,
            chunks_reviewed=2, chunks_failed=1, failed_chunks=[("timeout", ["x.py"])],
            ticket_note="> 🎫 Ticket T-1: 4 in scope · 2 outside\n", inline_accounting=accounting,
        )
        assert rendered == (
            "## prxref automated review: Request-Changes\n\nPR: Add widget · files reviewed: 3\n\n"
            "🟥 1 error · 🟧 2 warning · 🔍 1 spec · ⬜ 2 outofscope\n"
            "> 🎫 Ticket T-1: 4 in scope · 2 outside\n\n"
            "- 🟥 `a.py:3` — error problem\n- 🟧 `b.py:—` — warning problem\n"
            "- 🔍 `c.py:5` — spec problem\n- ⬜ `d.py:7` — outofscope problem\n\n"
            "**🟦 Outside the ticket (2)**\n\n"
            "- 🟦 🟧 `e.py:9` — outside thing\n- 🟦 ⬜ `g.py:2` — outside nit\n\n"
            "Inline comments: 3 of 6 findings (2 over the 4-comment cap · "
            "1 anchor rejected by the forge).\n\n"
            "---\n\nReviewed by prxref · model=m · 1250 tok · 4.3s\n\n\n"
            "> ⚠️ Partial review: 2 of 3 chunks were reviewed; 1 failed. "
            "Findings may be incomplete.\n>\n> - chunk of 1 file (x.py): timeout"
        )

    def test_the_packaged_template_gained_no_slot(self):
        packaged = prompt_templates.packaged_text("summary")
        assert not prompt_templates.placeholders(packaged) & SUMMARY_OPTIONAL_PLACEHOLDERS


SUMMARY_SOURCE = "PRXREF_PROMPTS_DIR"


class TestValidator:
    def test_every_new_slot_is_known(self, work, caplog):
        slots = sorted(SUMMARY_OPTIONAL_PLACEHOLDERS | prompt_templates.SUMMARY_MARKER_PLACEHOLDERS)
        text = "\n".join(f"{{{s}}}" for s in slots) + "\n"
        with caplog.at_level(logging.WARNING, logger="prxref.prompt_templates"):
            loaded = load_prompt_templates(_prompts_dir(work, text), source=SUMMARY_SOURCE)
        assert loaded is not None and loaded.summary == text
        assert _warnings(caplog) == []

    def test_optional_set_is_exactly_the_documented_slots(self):
        assert SUMMARY_OPTIONAL_PLACEHOLDERS == SUMMARY_GROUP_SLOTS | {
            "inline_accounting", "head_sha", "head_sha_short",
            "chunk_count", "input_tokens", "output_tokens",
        }
        assert len(SUMMARY_GROUP_SLOTS) == 10

    @pytest.mark.parametrize("slot", sorted(SUMMARY_GROUP_SLOTS))
    def test_one_group_slot_without_findings_is_accepted(self, work, slot):
        loaded = load_prompt_templates(_prompts_dir(work, f"{{{slot}}}\n"), source=SUMMARY_SOURCE)
        assert loaded is not None

    def test_findings_alone_is_still_accepted_without_a_warning(self, work, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref.prompt_templates"):
            load_prompt_templates(_prompts_dir(work, "{findings}\n"), source=SUMMARY_SOURCE)
        assert _warnings(caplog) == []

    def test_neither_is_refused(self, work):
        d = _prompts_dir(work, "{title} {error_marker} {head_sha} {inline_accounting}\n")
        with pytest.raises(ConfigError) as exc:
            load_prompt_templates(d, source=SUMMARY_SOURCE)
        assert str(exc.value) == (
            "PRXREF_PROMPTS_DIR: prompt template 'prompts/summary.md' is missing the required "
            "{findings} placeholder; a summary template needs it or at least one per-group slot "
            "({error_findings}, {error_section}, {outofscope_findings}, {outofscope_section}, "
            "{outside_ticket_findings}, {outside_ticket_section}, {spec_findings}, {spec_section}, "
            "{warning_findings}, {warning_section})"
        )
        assert str(exc.value) in DOC_PATH.read_text(encoding="utf-8")

    def test_uncovered_groups_warn_at_load(self, work, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref.prompt_templates"):
            load_prompt_templates(
                _prompts_dir(work, "{error_section}{warning_findings}{outofscope_section}"
                             "{outside_ticket_section}\n"),
                source=SUMMARY_SOURCE,
            )
        message = (
            "PRXREF_PROMPTS_DIR: prompt template 'prompts/summary.md' has no {findings} and no "
            "slot for the spec finding group(s); those findings are appended under an "
            "'Other findings' heading"
        )
        assert _warnings(caplog) == [message]
        assert message in DOC_PATH.read_text(encoding="utf-8")

    def test_several_uncovered_groups_are_named_in_order(self, work, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref.prompt_templates"):
            load_prompt_templates(_prompts_dir(work, "{warning_section}\n"), source=SUMMARY_SOURCE)
        assert len(_warnings(caplog)) == 1
        assert "for the error, spec, outofscope, outside_ticket finding group(s)" in _warnings(caplog)[0]

    def test_packaged_template_loads_without_a_warning(self, work, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref.prompt_templates"):
            load_prompt_templates(
                _prompts_dir(work, prompt_templates.packaged_text("summary")), source=SUMMARY_SOURCE,
            )
        assert _warnings(caplog) == []


def _example_render_inputs():
    pr = PRData(
        title="Add retry budget to the webhook client", description="", author="a",
        source_branch="feat/retry", target_branch="main", source_sha=SHA,
        target_sha="0" * 40, raw={},
    )

    def f(file, line, sev, scope, title):
        return Finding(file=file, line=line, severity=sev, confidence=0.9,
                       title=title, body="b", scope=scope)

    findings = [
        f("src/client.py", 42, "error", "in", "Retry loop never gives up on 5xx"),
        f("src/client.py", 88, "warning", "in", "Backoff ignores Retry-After"),
        f("src/config.py", 0, "warning", "in", "New key missing from .env.example"),
        f("src/client.py", 57, "spec", "in", "Budget must reset per delivery (T-12 §2)"),
        f("tests/test_client.py", 12, "outofscope", "in", "Test name says 3 retries, asserts 4"),
        f("src/log.py", 7, "warning", "out", "Log line leaks the webhook URL"),
    ]
    files = ["src/client.py", "src/config.py", "src/log.py", "tests/test_client.py"]
    return pr, files, findings


def _render_example(template: str) -> str:
    pr, files, findings = _example_render_inputs()
    with markers.overridden("error=🔴,warning=🟡,outofscope=⚪"):
        return orchestrator._render_summary(
            pr, files, "Request-Changes", findings, "openai/gpt-5-mini", 18234, 1912, 41800,
            chunks_reviewed=2, summary_template=template, summary_bullet_separator=": ",
        )


class TestWorkedExample:
    def test_the_example_file_loads_through_the_override_loader_without_warnings(self, work, caplog):
        text = EXAMPLE_PATH.read_text(encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            loaded = load_prompt_templates(_prompts_dir(work, text), source=SUMMARY_SOURCE)
            assert loaded is not None
            rendered = _render_example(loaded.override("summary"))
        assert _warnings(caplog) == []
        assert "Other findings" not in rendered
        assert "**🔍 Spec**\n\n- 🔍 `src/client.py:57`: Budget must reset" in rendered

    def test_the_documented_rendering_is_what_prxref_renders(self):
        doc = DOC_PATH.read_text(encoding="utf-8")
        text = EXAMPLE_PATH.read_text(encoding="utf-8")
        assert f"```markdown\n{text}```" in doc
        rendered = _render_example(text)
        assert rendered.endswith("41.8s\n")
        assert f"```markdown\n{rendered}```" in doc

    def test_the_issue_template_without_a_spec_slot_appends_the_spec_finding(self, caplog):
        text = EXAMPLE_PATH.read_text(encoding="utf-8")
        issue = (
            text.replace(" · {spec_marker} {spec_count} spec", "")
            .replace("{spec_section}\n", "")
            .replace("{inline_accounting}\n", "")
        )
        with caplog.at_level(logging.WARNING, logger="prxref.orchestrator"):
            rendered = _render_example(issue)
        tail = rendered[rendered.index("**🟦 Outside the ticket"):]
        assert f"```markdown\n{tail}```" in DOC_PATH.read_text(encoding="utf-8")
        assert tail.endswith(
            "**Other findings (1)**\n\n- 🔍 `src/client.py:57`: Budget must reset per delivery (T-12 §2)"
            "\n\n---\n\nReviewed by prxref · model=openai/gpt-5-mini · 20146 tok · 41.8s\n"
        )
        assert len(_warnings(caplog)) == 1


@pytest.mark.usefixtures("contract_stubs")
class TestSeparatorReachesEveryRender:
    def _spy(self, monkeypatch) -> list[str]:
        seen: list[str] = []
        real = orchestrator._render_summary

        def spy(*args, **kwargs):
            seen.append(kwargs.get("summary_bullet_separator", "<missing>"))
            return real(*args, **kwargs)

        monkeypatch.setattr(orchestrator, "_render_summary", spy)
        return seen

    def test_main_post_and_inline_accounting_repost(self, monkeypatch):
        from tests.test_orchestrator import HAPPY_FINDINGS, REF, FakeForge, FakeLLM, _added_file_diff

        seen = self._spy(monkeypatch)
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        orchestrator.orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS),
            max_inline_comments=1, summary_bullet_separator=" :: ",
        )
        assert seen == [" :: ", " :: "]
        assert len(forge.summaries) == 2
        for body in forge.summaries:
            assert "`src/app.py:3` :: Null deref" in body

    def test_degraded_fallback_render(self, monkeypatch):
        from tests.test_orchestrator import HAPPY_FINDINGS, REF, FakeForge, FakeLLM, _added_file_diff

        seen = self._spy(monkeypatch)
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        forge.fail.add("post_inline")
        orchestrator.orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS),
            post_mode="inline", summary_bullet_separator=" :: ",
        )
        assert seen == [" :: "]

    def test_empty_diff_summary(self, monkeypatch):
        from tests.test_orchestrator import REF, FakeForge, FakeLLM

        seen = self._spy(monkeypatch)
        forge = FakeForge(diff="")
        orchestrator.orchestrate_review(forge, REF, FakeLLM(), summary_bullet_separator=" :: ")
        assert seen == [" :: "]
        assert len(forge.summaries) == 1
