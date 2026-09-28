"""Configurable finding glyphs and the summary bullet separator key (#59).

Three groups:

* defaults are byte-identical: the expected strings below were captured by
  rendering the same inputs at aeda4fe, before the glyph table became
  configurable, so any drift in default output fails here;
* ``severity_markers`` / ``PRXREF_SEVERITY_MARKERS``: parsing, every
  configuration error, partial overrides, and the override reaching every
  rendering surface;
* ``summary_bullet_separator`` / ``PRXREF_SUMMARY_BULLET_SEPARATOR``: the key
  and its validation only (the renderer wiring is a later change).
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from prxref import cli, config, formatter, markers, orchestrator
from prxref.config import load_config, load_config_with_sources, read_config_file
from prxref.forges.base import PRData
from prxref.llm import ConfigError
from prxref.markers import (
    FALLBACK_MARKER,
    OUT_OF_TICKET_MARKER,
    SEVERITY_MARKERS,
    inline_header,
    marker_for,
    severity_marker,
)
from prxref.prompt_templates import load_prompt_templates
from prxref.triage import Finding

PR = PRData(
    title="Add widget", description="d", author="a", source_branch="f",
    target_branch="main", source_sha="a" * 40, target_sha="b" * 40, raw={},
)


def _f(file, line, sev, scope="unknown", title=None):
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
]
SOURCES = [SimpleNamespace(error=None, kind="url"), SimpleNamespace(error="boom", kind="file")]
DIGEST = "[spec:abc#x] MUST do things\n[ticket:T-1] statement"


def _spec_note() -> str:
    return orchestrator._spec_note(SOURCES, DIGEST)


def _summary(template: str = "", files=("a.py", "b.py")) -> str:
    return orchestrator._render_summary(
        PR, list(files), "Request-Changes", FINDINGS, "m", 100, 50, 1234,
        spec_note=_spec_note(), summary_template=template,
    )


def _fmt_summary() -> str:
    return formatter.format_summary(
        "Request-Changes", [*FINDINGS, _f("h.py", 1, "bogus")], [_f("z.py", 1, "error")],
        chunk_count=2, elapsed_ms=3500, input_tokens=10, output_tokens=20, model="m",
    )


# Captured at aeda4fe with the inputs above (the pre-#59 renderer).
BASELINE_SPEC_NOTE = (
    "> 🔍 Spec-grounded: 2 source(s) · 1 constraint(s) injected\n"
    "> ⚠️ Spec fetch failed for 1 source(s): source 2 (file): boom\n"
)
BASELINE_SUMMARY = (
    "## prxref automated review: Request-Changes\n\nPR: Add widget · files reviewed: 2\n\n"
    "🟥 2 error · 🟧 2 warning · 🔍 1 spec · ⬜ 1 outofscope\n"
    "> 🔍 Spec-grounded: 2 source(s) · 1 constraint(s) injected\n"
    "> ⚠️ Spec fetch failed for 1 source(s): source 2 (file): boom\n\n"
    "- 🟥 `a.py:3` — error problem\n- 🟧 `b.py:—` — warning problem\n"
    "- 🔍 `c.py:5` — spec problem\n- ⬜ `d.py:7` — outofscope problem\n\n"
    "**🟦 Outside the ticket (2)**\n\n"
    "- 🟦 🟧 `e.py:9` — outside thing\n- 🟦 🟥 `g.py:2` — outside error\n\n"
    "---\n\nReviewed by prxref · model=m · 150 tok · 1.2s\n"
)
BASELINE_FALLBACK_SUMMARY = (
    "🤖 **prxref review — Request-Changes**\n\nPR: Add widget\n\n"
    "Files reviewed: 1 · 🟥 2 error · 🟧 2 warning · 🔍 1 spec · ⬜ 1 outofscope\n"
    "> 🔍 Spec-grounded: 2 source(s) · 1 constraint(s) injected\n"
    "> ⚠️ Spec fetch failed for 1 source(s): source 2 (file): boom\n\n"
    "- 🟥 `a.py:3` — error problem\n- 🟧 `b.py:—` — warning problem\n"
    "- 🔍 `c.py:5` — spec problem\n- ⬜ `d.py:7` — outofscope problem\n\n"
    "**🟦 Outside the ticket (2)**\n\n"
    "- 🟦 🟧 `e.py:9` — outside thing\n- 🟦 🟥 `g.py:2` — outside error\n\n"
    "Reviewed by prxref · model=m · 150 tok · 1.2s"
)
BASELINE_ALL_OUTSIDE = (
    "## prxref automated review: Approved\n\nPR: Add widget · files reviewed: 1\n\n"
    "🟥 0 error · 🟧 1 warning · 🔍 0 spec · ⬜ 0 outofscope\n\n"
    "No in-ticket findings.\n\n**🟦 Outside the ticket (1)**\n\n"
    "- 🟦 🟧 `e.py:9` — outside thing\n\n---\n\nReviewed by prxref · model=m · 3 tok · 0.0s\n"
)
BASELINE_INLINE = {
    "in": "🤖 🟥 **[ERROR] error problem** (`a.py:3`)",
    "out": "🤖 🟦 🟧 **[WARNING · OUTSIDE TICKET] outside thing** (`e.py:9`)",
    "file_level": "🤖 🟧 **[WARNING] warning problem** (`b.py`)",
    "unknown_severity": "🤖 ⬜ **[BOGUS] bogus problem** (`h.py:1`)",
}
BASELINE_FORMAT_FINDING = (
    "🤖 🟦 🟧 **[WARNING · OUTSIDE TICKET] outside thing** (`e.py:9`)\n\n"
    "body text\n\n---\n*Reviewed by prxref · model=m*"
)
BASELINE_FMT_SUMMARY = (
    "## 🛑 Request-Changes\n\n"
    "**Findings:** 🟥 2 error · 🟧 2 warning · 🔍 1 spec · ⬜ 2 outofscope\n\n"
    "7 active of 8 raw\n\n"
    "| Severity | Location | Title |\n| --- | --- | --- |\n"
    "| 🟥 | a.py:3 | error problem |\n| 🟦 🟥 | g.py:2 | outside error |\n"
    "| 🟧 | b.py | warning problem |\n| 🟦 🟧 | e.py:9 | outside thing |\n"
    "| 🔍 | c.py:5 | spec problem |\n| ⬜ | d.py:7 | outofscope problem |\n"
    "| ⬜ | h.py:1 | bogus problem |\n"
    "<details>\n<summary>Dropped findings: 1 (retained for audit)</summary>\n\n"
    "- 1 × unspecified\n\n"
    "| Severity | Location | Title |\n| --- | --- | --- |\n| 🟥 | z.py:1 | error problem |\n"
    "</details>\n\n---\n\n*chunks 2 · 10 in / 20 out tokens · 3.5s · model m*\n\n"
    "*Reviewed by prxref · model=m · 30 tok · 3.5s*\n"
)
BASELINE_FMT_INLINE = {
    "out": "🟦 🟧 **[OUTSIDE TICKET] outside thing**\n\nbody text\n\n*attr*",
    "in": "🟥 **error problem**\n\nbody text\n\n*attr*",
}


class TestDefaultsAreByteIdentical:
    def test_spec_note(self):
        assert _spec_note() == BASELINE_SPEC_NOTE

    def test_packaged_summary(self):
        assert _summary() == BASELINE_SUMMARY

    def test_fallback_summary(self):
        rendered = _summary(orchestrator._FALLBACK_SUMMARY_TEMPLATE, files=["a.py"])
        assert rendered == BASELINE_FALLBACK_SUMMARY

    def test_summary_with_every_finding_outside_the_ticket(self):
        rendered = orchestrator._render_summary(
            PR, ["a.py"], "Approved", [FINDINGS[4]], "m", 1, 2, 3,
        )
        assert rendered == BASELINE_ALL_OUTSIDE

    def test_inline_headers(self):
        assert inline_header(FINDINGS[0]) == BASELINE_INLINE["in"]
        assert inline_header(FINDINGS[4]) == BASELINE_INLINE["out"]
        assert inline_header(FINDINGS[1]) == BASELINE_INLINE["file_level"]
        assert inline_header(_f("h.py", 1, "bogus")) == BASELINE_INLINE["unknown_severity"]
        assert orchestrator._format_finding(FINDINGS[4], "m") == BASELINE_FORMAT_FINDING

    def test_formatter(self):
        assert _fmt_summary() == BASELINE_FMT_SUMMARY
        assert formatter.format_inline_comment(FINDINGS[4], "attr") == BASELINE_FMT_INLINE["out"]
        assert formatter.format_inline_comment(FINDINGS[0], "attr") == BASELINE_FMT_INLINE["in"]

    def test_an_empty_override_is_the_default(self):
        with markers.overridden(""):
            assert _summary() == BASELINE_SUMMARY
        with markers.overridden({}):
            assert _fmt_summary() == BASELINE_FMT_SUMMARY

    def test_the_default_config_values(self):
        cfg = load_config()
        assert cfg["severity_markers"] == ""
        assert cfg["summary_bullet_separator"] == " — "
        assert config._DEFAULTS["severity_markers"] == ""
        assert config._DEFAULTS["summary_bullet_separator"] == " — "


class TestParseOverrides:
    def test_full_table(self):
        raw = "error=🔴,warning=🟡,spec=🔎,outofscope=⚪,out_of_ticket=🔷"
        assert markers.parse_overrides(raw) == {
            "error": "🔴", "warning": "🟡", "spec": "🔎", "outofscope": "⚪",
            "out_of_ticket": "🔷",
        }

    def test_partial_and_whitespace(self):
        assert markers.parse_overrides("  error = 🔴 ,\twarning=W  ,") == {
            "error": "🔴", "warning": "W",
        }

    def test_empty_and_none(self):
        assert markers.parse_overrides("") == {}
        assert markers.parse_overrides(" , ") == {}
        assert markers.parse_overrides(None) == {}

    def test_mapping(self):
        assert markers.parse_overrides({"spec": "S"}) == {"spec": "S"}

    def test_a_glyph_may_contain_an_equals_sign(self):
        assert markers.parse_overrides("error=a=b") == {"error": "a=b"}

    def test_configure_reaches_every_accessor(self):
        with markers.overridden("error=E,outofscope=O,out_of_ticket=T"):
            assert markers.active_severity_markers() == {
                "error": "E", "warning": "🟧", "spec": "🔍", "outofscope": "O",
            }
            assert markers.out_of_ticket_marker() == "T"
            assert markers.fallback_marker() == "O"
            assert severity_marker("nonsense") == "O"
            assert marker_for("warning", "out") == "T 🟧"
            assert markers.marker_slots() == {
                "error_marker": "E", "warning_marker": "🟧", "spec_marker": "🔍",
                "outofscope_marker": "O", "out_of_ticket_marker": "T",
            }
        assert markers.fallback_marker() == FALLBACK_MARKER
        assert markers.out_of_ticket_marker() == OUT_OF_TICKET_MARKER

    def test_the_defaults_stay_immutable(self):
        with markers.overridden("error=E"):
            assert SEVERITY_MARKERS["error"] == "🟥"
            with pytest.raises(TypeError):
                markers.active_severity_markers()["error"] = "x"  # type: ignore[index]

    def test_overridden_restores_the_previous_table(self):
        markers.configure("error=A")
        with markers.overridden("error=B"):
            assert severity_marker("error") == "B"
        assert severity_marker("error") == "A"

    def test_overridden_restores_on_error(self):
        with pytest.raises(RuntimeError), markers.overridden("error=B"):
            raise RuntimeError
        assert severity_marker("error") == "🟥"

    def test_an_invalid_configure_leaves_the_table(self):
        markers.configure("error=A")
        with pytest.raises(ValueError):
            markers.configure("warning=🟥")
        assert severity_marker("error") == "A"

    def test_configure_replaces_rather_than_stacks(self):
        markers.configure("error=A")
        markers.configure("warning=W")
        assert severity_marker("error") == "🟥"
        assert severity_marker("warning") == "W"


ENV = "PRXREF_SEVERITY_MARKERS"

ERROR_CASES = [
    (
        "eror=X",
        "unknown marker name 'eror'; did you mean 'error'? "
        "(known: error, warning, spec, outofscope, out_of_ticket)",
    ),
    (
        "zzz=X",
        "unknown marker name 'zzz' (known: error, warning, spec, outofscope, out_of_ticket)",
    ),
    ("error", "pair 'error' has no '='; expected name=glyph"),
    ("=X", "pair '=X' has an empty name; expected name=glyph"),
    ("error=", "marker 'error' has an empty glyph"),
    ("error= ,warning=W", "marker 'error' has an empty glyph"),
    ("error=A,error=B", "marker 'error' is given twice"),
    ("error=a b", "marker 'error' glyph 'a b' contains whitespace or a comma"),
    ("error=A,warning=A", "markers 'error' and 'warning' would both render 'A'; the five glyphs must be distinct"),
    ("warning=🟥", "markers 'error' and 'warning' would both render '🟥'; the five glyphs must be distinct"),
    (
        "out_of_ticket=🟧",
        "markers 'warning' and 'out_of_ticket' would both render '🟧'; the five glyphs must be distinct",
    ),
]


class TestSeverityMarkerErrors:
    @pytest.mark.parametrize(("raw", "message"), ERROR_CASES)
    def test_env_error_names_the_variable(self, monkeypatch, raw, message):
        monkeypatch.setenv(ENV, raw)
        with pytest.raises(ConfigError) as exc:
            load_config()
        assert str(exc.value) == f"{ENV}: {message}"

    @pytest.mark.parametrize(("raw", "message"), ERROR_CASES)
    def test_file_error_names_the_file_key(self, tmp_path, monkeypatch, raw, message):
        monkeypatch.chdir(tmp_path)
        path = tmp_path / ".prxref.toml"
        path.write_text(f"severity_markers = {json.dumps(raw, ensure_ascii=False)}\n", encoding="utf-8")
        with pytest.raises(ConfigError) as exc:
            load_config(config_file=path)
        assert str(exc.value) == f".prxref.toml: severity_markers: {message}"

    def test_a_comma_in_a_mapping_glyph(self):
        with pytest.raises(ConfigError, match=r"^severity_markers: marker 'spec' glyph 'a,b' contains"):
            load_config(severity_markers={"spec": "a,b"})

    def test_a_non_string_override(self):
        with pytest.raises(ConfigError, match=r"^severity_markers: must be name=glyph pairs, got 3$"):
            load_config(severity_markers=3)

    def test_a_toml_non_string(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = tmp_path / ".prxref.toml"
        path.write_text("severity_markers = 3\n", encoding="utf-8")
        with pytest.raises(ConfigError, match=r"'severity_markers' must be a string, got an integer"):
            load_config(config_file=path)

    def test_the_cli_exits_2(self, monkeypatch, capsys):
        monkeypatch.setenv(ENV, "eror=X")
        assert cli.main(["config", "check"]) == 2
        assert f"configuration error: {ENV}: unknown marker name 'eror'" in capsys.readouterr().err


class TestSeverityMarkerLoading:
    def test_env_parses_into_the_override_dict(self, monkeypatch):
        monkeypatch.setenv(ENV, " error = 🔴 , out_of_ticket=🔷 ")
        cfg, layers = load_config_with_sources()
        assert cfg["severity_markers"] == " error = 🔴 , out_of_ticket=🔷 "
        assert markers.parse_overrides(cfg["severity_markers"]) == {
            "error": "🔴", "out_of_ticket": "🔷",
        }
        assert layers["severity_markers"] == f"env {ENV}"

    def test_file_sets_it_and_env_wins(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = tmp_path / ".prxref.toml"
        path.write_text('severity_markers = "spec=S, warning=W"\n', encoding="utf-8")
        assert read_config_file(path) == {"severity_markers": "spec=S, warning=W"}
        cfg, layers = load_config_with_sources(config_file=path)
        assert cfg["severity_markers"] == "spec=S, warning=W"
        assert layers["severity_markers"] == "file"
        monkeypatch.setenv(ENV, "error=E")
        assert load_config(config_file=path)["severity_markers"] == "error=E"

    def test_an_empty_file_value_reads_as_unset(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = tmp_path / ".prxref.toml"
        path.write_text('severity_markers = ""\n', encoding="utf-8")
        assert read_config_file(path) == {}

    def test_it_is_a_file_key(self):
        assert "severity_markers" in config.FILE_KEYS
        assert "summary_bullet_separator" in config.FILE_KEYS

    def test_config_check_prints_the_value(self, monkeypatch, capsys):
        monkeypatch.setenv(ENV, "error=E")
        assert cli.main(["config", "check", "--format", "json"]) == 0
        values = json.loads(capsys.readouterr().out)["values"]
        assert values["severity_markers"] == {"value": "error=E", "source": f"env {ENV}"}
        assert values["summary_bullet_separator"] == {"value": " — ", "source": "default"}

    def test_run_review_installs_the_table(self, monkeypatch):
        """_run_review is the one site review, serve and eval run share."""
        class Stop(Exception):
            pass

        seen = []

        def stamp(_path):
            seen.append(markers.marker_slots())
            raise Stop

        monkeypatch.setenv(ENV, "error=E,out_of_ticket=T")
        monkeypatch.setattr(cli, "_config_file_stamp", stamp)
        with pytest.raises(Stop):
            cli._run_review("https://github.com/o/r/pull/1", post=False)
        assert seen[0]["error_marker"] == "E"
        assert seen[0]["out_of_ticket_marker"] == "T"
        monkeypatch.delenv(ENV)
        with pytest.raises(Stop):
            cli._run_review("https://github.com/o/r/pull/1", post=False)
        assert seen[1]["error_marker"] == "🟥"


OVERRIDE = "error=E,warning=W,spec=S,outofscope=O,out_of_ticket=T"


class TestTheOverrideReachesEverySurface:
    def test_summary_counts_bullets_heading_and_spec_note(self):
        with markers.overridden(OVERRIDE):
            rendered = _summary()
        assert "E 2 error · W 2 warning · S 1 spec · O 1 outofscope\n" in rendered
        assert "> S Spec-grounded: 2 source(s)" in rendered
        assert "- E `a.py:3` — error problem\n" in rendered
        assert "- O `d.py:7` — outofscope problem\n" in rendered
        assert "**T Outside the ticket (2)**" in rendered
        assert "- T W `e.py:9` — outside thing\n" in rendered
        assert not any(g in rendered for g in (*SEVERITY_MARKERS.values(), OUT_OF_TICKET_MARKER))

    def test_fallback_summary(self):
        with markers.overridden(OVERRIDE):
            rendered = _summary(orchestrator._FALLBACK_SUMMARY_TEMPLATE)
        assert "Files reviewed: 2 · E 2 error · W 2 warning · S 1 spec · O 1 outofscope\n" in rendered
        assert "**T Outside the ticket (2)**" in rendered

    def test_a_partial_override_keeps_the_rest(self):
        with markers.overridden("warning=W"):
            rendered = _summary()
        assert "🟥 2 error · W 2 warning · 🔍 1 spec · ⬜ 1 outofscope\n" in rendered
        assert "- 🟦 W `e.py:9` — outside thing" in rendered

    def test_inline_headers(self):
        with markers.overridden(OVERRIDE):
            assert inline_header(FINDINGS[0]) == "🤖 E **[ERROR] error problem** (`a.py:3`)"
            assert inline_header(FINDINGS[4]) == (
                "🤖 T W **[WARNING · OUTSIDE TICKET] outside thing** (`e.py:9`)"
            )
            assert inline_header(_f("h.py", 1, "bogus")) == "🤖 O **[BOGUS] bogus problem** (`h.py:1`)"
            assert orchestrator._format_finding(FINDINGS[4], "m").startswith("🤖 T W **[")

    def test_formatter(self):
        with markers.overridden(OVERRIDE):
            rendered = _fmt_summary()
            inline = formatter.format_inline_comment(FINDINGS[4], "attr")
        assert "**Findings:** E 2 error · W 2 warning · S 1 spec · O 2 outofscope\n" in rendered
        assert "| T E | g.py:2 | outside error |" in rendered
        assert "| O | h.py:1 | bogus problem |" in rendered
        assert inline == "T W **[OUTSIDE TICKET] outside thing**\n\nbody text\n\n*attr*"

    def test_an_operator_template_can_place_every_slot(self):
        template = (
            "{error_marker}{warning_marker}{spec_marker}{outofscope_marker}"
            "{out_of_ticket_marker}\n{findings}"
        )
        with markers.overridden(OVERRIDE):
            rendered = _summary(template)
        assert rendered.startswith("EWSOT\n")
        assert rendered.startswith("🟥🟧🔍⬜🟦\n") is False
        assert _summary(template).startswith("🟥🟧🔍⬜🟦\n")


class TestPromptTemplateSlots:
    def test_marker_slots_are_known_summary_placeholders(self, tmp_path, monkeypatch, caplog):
        monkeypatch.chdir(tmp_path)
        d = tmp_path / "prompts"
        d.mkdir()
        (d / "summary.md").write_text(
            "{error_marker} {warning_marker} {spec_marker} {outofscope_marker} "
            "{out_of_ticket_marker}\n{findings}\n", encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING):
            loaded = load_prompt_templates("prompts", source="PRXREF_PROMPTS_DIR")
        assert loaded is not None
        assert "unknown placeholder" not in caplog.text

    def test_a_typo_still_warns(self, tmp_path, monkeypatch, caplog):
        monkeypatch.chdir(tmp_path)
        d = tmp_path / "prompts"
        d.mkdir()
        (d / "summary.md").write_text("{eror_marker}\n{findings}\n", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            load_prompt_templates("prompts", source="PRXREF_PROMPTS_DIR")
        assert "unknown placeholder(s) {eror_marker}" in caplog.text


SEP_ENV = "PRXREF_SUMMARY_BULLET_SEPARATOR"


class TestSummaryBulletSeparator:
    def test_env_keeps_its_spaces(self, monkeypatch):
        monkeypatch.setenv(SEP_ENV, ": ")
        assert load_config()["summary_bullet_separator"] == ": "
        monkeypatch.setenv(SEP_ENV, "  ->  ")
        assert load_config()["summary_bullet_separator"] == "  ->  "

    def test_whitespace_only_env_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv(SEP_ENV, "   ")
        assert load_config()["summary_bullet_separator"] == " — "

    def test_file_keeps_its_spaces(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = tmp_path / ".prxref.toml"
        path.write_text('summary_bullet_separator = " | "\n', encoding="utf-8")
        cfg, layers = load_config_with_sources(config_file=path)
        assert cfg["summary_bullet_separator"] == " | "
        assert layers["summary_bullet_separator"] == "file"
        path.write_text('summary_bullet_separator = " "\n', encoding="utf-8")
        assert load_config(config_file=path)["summary_bullet_separator"] == " "

    def test_empty_means_the_default(self, tmp_path, monkeypatch):
        assert load_config(summary_bullet_separator="")["summary_bullet_separator"] == " — "
        monkeypatch.chdir(tmp_path)
        path = tmp_path / ".prxref.toml"
        path.write_text('summary_bullet_separator = ""\n', encoding="utf-8")
        assert load_config(config_file=path)["summary_bullet_separator"] == " — "

    def test_sixteen_characters_is_the_limit(self, monkeypatch):
        monkeypatch.setenv(SEP_ENV, "x" * 16)
        assert load_config()["summary_bullet_separator"] == "x" * 16

    @pytest.mark.parametrize(
        ("raw", "message"),
        [
            ("a\nb", "must not contain a newline, got 'a\\nb'"),
            ("a\rb", "must not contain a newline, got 'a\\rb'"),
            ("x" * 17, f"must be at most 16 characters, got 17 ({'x' * 17!r})"),
        ],
    )
    def test_env_errors(self, monkeypatch, raw, message):
        monkeypatch.setenv(SEP_ENV, raw)
        with pytest.raises(ConfigError) as exc:
            load_config()
        assert str(exc.value) == f"{SEP_ENV}: {message}"

    def test_file_errors(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = tmp_path / ".prxref.toml"
        path.write_text('summary_bullet_separator = "a\\nb"\n', encoding="utf-8")
        with pytest.raises(ConfigError) as exc:
            load_config(config_file=path)
        assert str(exc.value) == (
            ".prxref.toml: summary_bullet_separator: must not contain a newline, got 'a\\nb'"
        )
        path.write_text("summary_bullet_separator = 1\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="'summary_bullet_separator' must be a string, got an integer"):
            load_config(config_file=path)

    def test_a_non_string_override(self):
        with pytest.raises(ConfigError) as exc:
            load_config(summary_bullet_separator=3)
        assert str(exc.value) == "summary_bullet_separator: must be a string, got 3"

