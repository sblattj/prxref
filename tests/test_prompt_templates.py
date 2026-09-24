"""Prompt-template overrides (#11 T1): the loader, validation and run record."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
from pathlib import Path

import pytest

from prxref import prompt_templates as pt
from prxref.llm import ConfigError
from prxref.prompt_templates import (
    CONTEXT_MARKER,
    MAX_TEMPLATE_BYTES,
    OPTIONAL_PLACEHOLDERS,
    TEMPLATE_NAMES,
    PromptTemplates,
    load_prompt_templates,
    placeholders,
    required_placeholders,
)
from prxref.reviewer import _CONTEXT_MARKER, load_prompt

SOURCES = ("PRXREF_PROMPTS_DIR", "--prompts-dir")
_SLOT = re.compile(r"\{([A-Za-z_]\w*)\}")


def _packaged(name: str) -> str:
    return load_prompt(f"{name}.md")


def _override(name: str) -> str:
    return "TEAM OVERRIDE: style-guide findings are welcome.\n\n" + _packaged(name)


def _prompts(work: Path, files: dict[str, str | bytes], dirname: str = "prompts") -> Path:
    d = work / dirname
    d.mkdir()
    for fname, content in files.items():
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        (d / fname).write_bytes(data)
    return d


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


@pytest.fixture
def work(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


REQUIRED_CASES = [(n, p) for n in ("worker", "systemic") for p in sorted(required_placeholders(n))]


class TestPackagedContract:
    @pytest.mark.parametrize("name", TEMPLATE_NAMES)
    def test_the_packaged_fallback_is_what_reviewer_load_prompt_reads(self, name):
        assert pt._packaged_text(name) == load_prompt(f"{name}.md")

    def test_the_marker_is_the_one_the_renderer_splits_on(self):
        assert CONTEXT_MARKER == _CONTEXT_MARKER == "## Review Context"

    @pytest.mark.parametrize("name", ["worker", "systemic"])
    def test_review_requirements_are_every_packaged_slot_below_the_marker_bar_the_optional_ones(self, name):
        _, marker, tail = _packaged(name).partition(_CONTEXT_MARKER)
        assert marker
        expected = set(_SLOT.findall(tail)) - OPTIONAL_PLACEHOLDERS
        assert required_placeholders(name) == expected
        assert {"pr_title", "ticket_context", "spec_digest"} <= expected
        assert "scope_example" not in expected

    def test_worker_requires_diff_and_systemic_requires_digest(self):
        assert "diff" in required_placeholders("worker")
        assert "digest" in required_placeholders("systemic")

    def test_summary_requires_only_findings(self):
        assert required_placeholders("summary") == frozenset({"findings"})
        assert "findings" in placeholders(_packaged("summary"))

    def test_a_slot_added_to_a_packaged_template_is_required_of_every_override(self, work, monkeypatch):
        original = pt._packaged_text

        def grown(name):
            text = original(name)
            return text + "\n{new_slot}\n" if name == "worker" else text

        d = _prompts(work, {"worker.md": _packaged("worker")})
        monkeypatch.setattr(pt, "_packaged_text", grown)
        assert "new_slot" in required_placeholders("worker")
        with pytest.raises(ConfigError, match=r"\{new_slot\}"):
            load_prompt_templates(d, source="PRXREF_PROMPTS_DIR")

    def test_the_packaged_templates_copied_verbatim_load_without_warnings(self, work, caplog):
        d = _prompts(work, {f"{n}.md": _packaged(n) for n in TEMPLATE_NAMES})
        with caplog.at_level(logging.INFO, logger="prxref"):
            loaded = load_prompt_templates(d, source="PRXREF_PROMPTS_DIR")
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert loaded.overridden == TEMPLATE_NAMES
        for name in TEMPLATE_NAMES:
            assert loaded.text(name) == _packaged(name)


class TestOverride:
    def test_a_full_override_replaces_all_three_templates(self, work, caplog):
        files = {f"{n}.md": _override(n) for n in TEMPLATE_NAMES}
        d = _prompts(work, files)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            loaded = load_prompt_templates(d, source="PRXREF_PROMPTS_DIR")
        assert _warnings(caplog) == []
        assert isinstance(loaded, PromptTemplates)
        assert (loaded.worker, loaded.systemic, loaded.summary) == tuple(_override(n) for n in TEMPLATE_NAMES)
        assert loaded.overridden == ("worker", "systemic", "summary")
        for name in TEMPLATE_NAMES:
            assert loaded.override(name) == _override(name)
        assert list(loaded.record()["templates"]) == ["worker", "systemic", "summary"]

    def test_a_partial_override_falls_back_to_the_packaged_templates(self, work):
        d = _prompts(work, {"worker.md": _override("worker")})
        loaded = load_prompt_templates(d, source="PRXREF_PROMPTS_DIR")
        assert loaded.worker == _override("worker")
        assert loaded.systemic == _packaged("systemic")
        assert loaded.summary == _packaged("summary")
        assert loaded.overridden == ("worker",)
        assert loaded.override("worker") == _override("worker")
        assert loaded.override("systemic") == ""
        assert loaded.override("summary") == ""
        assert list(loaded.record()["templates"]) == ["worker"]

    def test_a_summary_holding_only_findings_loads_silently(self, work, caplog):
        d = _prompts(work, {"summary.md": "Review notes:\n\n{findings}\n"})
        with caplog.at_level(logging.WARNING, logger="prxref"):
            loaded = load_prompt_templates(d, source="PRXREF_PROMPTS_DIR")
        assert _warnings(caplog) == []
        assert loaded.summary == "Review notes:\n\n{findings}\n"

    @pytest.mark.parametrize("path", [None, "", "   "])
    def test_no_path_means_no_overrides(self, path):
        assert load_prompt_templates(path, source="PRXREF_PROMPTS_DIR") is None

    def test_the_record_is_json_native_and_holds_the_dir_as_configured(self, work):
        raw = b"Summary:\n{findings}\n"
        _prompts(work, {"summary.md": raw})
        loaded = load_prompt_templates("prompts", source="--prompts-dir")
        record = loaded.record()
        assert json.loads(json.dumps(record)) == record == {
            "dir": "prompts",
            "templates": {
                "summary": {
                    "path": os.path.join("prompts", "summary.md"),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "chars": len("Summary:\n{findings}\n"),
                },
            },
        }

    def test_sha256_covers_the_raw_bytes_while_chars_counts_the_decoded_text(self, work):
        raw = b"\xef\xbb\xbf" + _override("worker").replace("\n", "\r\n").encode("utf-8")
        d = _prompts(work, {"worker.md": raw})
        loaded = load_prompt_templates(d, source="PRXREF_PROMPTS_DIR")
        entry = loaded.record()["templates"]["worker"]
        assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
        assert entry["sha256"] != hashlib.sha256(loaded.worker.encode("utf-8")).hexdigest()
        assert loaded.worker == _override("worker")
        assert entry["chars"] == len(_override("worker"))

    def test_a_path_object_is_recorded_as_a_string(self, work):
        d = _prompts(work, {"summary.md": "{findings}"})
        loaded = load_prompt_templates(d, source="PRXREF_PROMPTS_DIR")
        assert loaded.dir == os.fspath(d)
        assert loaded.record()["templates"]["summary"]["path"] == os.path.join(os.fspath(d), "summary.md")

    def test_a_symlink_inside_the_directory_loads_and_hashes_its_target(self, work):
        d = _prompts(work, {})
        (d / "variants").mkdir()
        target = d / "variants" / "worker-v2.md"
        target.write_bytes(_override("worker").encode("utf-8"))
        (d / "worker.md").symlink_to(Path("variants") / "worker-v2.md")
        loaded = load_prompt_templates(d, source="PRXREF_PROMPTS_DIR")
        assert loaded.worker == _override("worker")
        assert loaded.record()["templates"]["worker"]["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()

    def test_an_absolute_directory_outside_the_working_directory_is_the_operators_choice(self, work, tmp_path):
        d = _prompts(tmp_path, {"summary.md": "{findings}"}, dirname="ci-prompts")
        loaded = load_prompt_templates(str(d), source="PRXREF_PROMPTS_DIR")
        assert loaded.overridden == ("summary",)

    def test_the_result_is_immutable(self, work):
        d = _prompts(work, {"worker.md": _override("worker")})
        loaded = load_prompt_templates(d, source="PRXREF_PROMPTS_DIR")
        with pytest.raises(dataclasses.FrozenInstanceError):
            loaded.worker = "x"
        with pytest.raises(dataclasses.FrozenInstanceError):
            loaded.overrides[0].sha256 = "0"
        assert isinstance(loaded.overrides, tuple)

    @pytest.mark.parametrize("method", ["text", "override"])
    def test_an_unknown_template_name_is_a_value_error(self, work, method):
        d = _prompts(work, {"summary.md": "{findings}"})
        loaded = load_prompt_templates(d, source="PRXREF_PROMPTS_DIR")
        with pytest.raises(ValueError, match="judge"):
            getattr(loaded, method)("judge")


def _missing_dir(work: Path, tmp_path: Path) -> str:
    return "no-such-prompts"


def _not_a_directory(work: Path, tmp_path: Path) -> str:
    (work / "prompts.md").write_text("x")
    return "prompts.md"


def _url(work: Path, tmp_path: Path) -> str:
    return "https://example.com/prompts"


def _worker_without_marker(work: Path, tmp_path: Path) -> str:
    _prompts(work, {"worker.md": _packaged("worker").replace(_CONTEXT_MARKER, "## Context")})
    return "prompts"


def _systemic_without_marker(work: Path, tmp_path: Path) -> str:
    _prompts(work, {"systemic.md": _packaged("systemic").replace(_CONTEXT_MARKER, "## Context")})
    return "prompts"


def _summary_without_findings(work: Path, tmp_path: Path) -> str:
    _prompts(work, {"summary.md": _packaged("summary").replace("{findings}", "")})
    return "prompts"


def _required_slot_only_above_the_marker(work: Path, tmp_path: Path) -> str:
    head, marker, tail = _packaged("worker").partition(_CONTEXT_MARKER)
    _prompts(work, {"worker.md": head + "{diff}\n" + marker + tail.replace("{diff}", "")})
    return "prompts"


def _over_the_size_cap(work: Path, tmp_path: Path) -> str:
    data = _packaged("worker").encode("utf-8")
    _prompts(work, {"worker.md": data + b"x" * (MAX_TEMPLATE_BYTES + 1 - len(data))})
    return "prompts"


def _not_utf8(work: Path, tmp_path: Path) -> str:
    _prompts(work, {"summary.md": b"{findings}\n\xff\xfe latin-1 \xe9\n"})
    return "prompts"


def _nul_bytes(work: Path, tmp_path: Path) -> str:
    _prompts(work, {"summary.md": b"{findings}\x00"})
    return "prompts"


def _symlink_out_of_the_directory(work: Path, tmp_path: Path) -> str:
    (work / "elsewhere.md").write_text(_packaged("worker"))
    d = _prompts(work, {})
    (d / "worker.md").symlink_to(Path("..") / "elsewhere.md")
    return "prompts"


def _symlink_out_of_the_working_directory(work: Path, tmp_path: Path) -> str:
    outside = tmp_path / "secret.md"
    outside.write_text("{findings}")
    d = _prompts(work, {})
    (d / "summary.md").symlink_to(outside)
    return "prompts"


def _directory_symlink_out_of_the_working_directory(work: Path, tmp_path: Path) -> str:
    real = _prompts(tmp_path, {"summary.md": "{findings}"}, dirname="outside-prompts")
    (work / "prompts").symlink_to(real)
    return "prompts"


def _template_is_a_directory(work: Path, tmp_path: Path) -> str:
    d = _prompts(work, {})
    (d / "worker.md").mkdir()
    return "prompts"


INVALID_CASES = [
    (_missing_dir, "does not exist"),
    (_not_a_directory, "is not a directory"),
    (_url, "not a URL"),
    (_worker_without_marker, "missing the '## Review Context' marker"),
    (_systemic_without_marker, "missing the '## Review Context' marker"),
    (_summary_without_findings, "missing the required {findings} placeholder"),
    (_required_slot_only_above_the_marker, "missing required placeholder(s) {diff} after"),
    (_over_the_size_cap, f"{MAX_TEMPLATE_BYTES + 1} bytes, over the {MAX_TEMPLATE_BYTES}-byte (256 KiB) limit"),
    (_not_utf8, "is not UTF-8 text"),
    (_nul_bytes, "contains NUL bytes"),
    (_symlink_out_of_the_directory, "resolves outside the prompts directory"),
    (_symlink_out_of_the_working_directory, "resolves outside the prompts directory"),
    (_directory_symlink_out_of_the_working_directory, "resolves outside the working directory"),
    (_template_is_a_directory, "cannot read prompt template"),
]


class TestInvalid:
    @pytest.mark.parametrize("source", SOURCES)
    @pytest.mark.parametrize(("build", "expected"), INVALID_CASES, ids=[c[0].__name__[1:] for c in INVALID_CASES])
    def test_each_invalid_case_is_a_config_error_naming_the_source(self, work, tmp_path, build, expected, source):
        path = build(work, tmp_path)
        with pytest.raises(ConfigError) as excinfo:
            load_prompt_templates(path, source=source)
        message = str(excinfo.value)
        assert message.startswith(f"{source}: ")
        assert expected in message

    @pytest.mark.parametrize(("name", "slot"), REQUIRED_CASES, ids=[f"{n}-{p}" for n, p in REQUIRED_CASES])
    def test_dropping_any_required_placeholder_is_a_config_error(self, work, name, slot):
        _prompts(work, {f"{name}.md": _packaged(name).replace(f"{{{slot}}}", "")})
        with pytest.raises(ConfigError, match=re.escape(f"{{{slot}}}")) as excinfo:
            load_prompt_templates("prompts", source="--prompts-dir")
        assert str(excinfo.value).startswith("--prompts-dir: ")
        assert os.path.join("prompts", f"{name}.md") in str(excinfo.value)

    def test_every_missing_placeholder_is_named_at_once(self, work):
        text = _packaged("systemic").replace("{digest}", "").replace("{pr_title}", "")
        _prompts(work, {"systemic.md": text})
        with pytest.raises(ConfigError, match=re.escape("{digest}, {pr_title}")):
            load_prompt_templates("prompts", source="PRXREF_PROMPTS_DIR")

    def test_a_template_of_exactly_256_kib_loads_and_is_not_truncated(self, work):
        data = _packaged("worker").encode("utf-8")
        data += b"x" * (MAX_TEMPLATE_BYTES - len(data))
        _prompts(work, {"worker.md": data})
        loaded = load_prompt_templates("prompts", source="PRXREF_PROMPTS_DIR")
        assert loaded.worker.encode("utf-8") == data
        assert loaded.record()["templates"]["worker"]["chars"] == len(data.decode("utf-8"))

    def test_the_utf8_error_reports_the_byte_offset(self, work):
        _prompts(work, {"summary.md": b"{findings}\n\xff"})
        with pytest.raises(ConfigError, match="at byte 11"):
            load_prompt_templates("prompts", source="PRXREF_PROMPTS_DIR")

    def test_a_failing_template_refuses_the_whole_directory(self, work):
        _prompts(work, {"worker.md": _override("worker"), "summary.md": "no slot here"})
        with pytest.raises(ConfigError, match="summary.md"):
            load_prompt_templates("prompts", source="PRXREF_PROMPTS_DIR")


class TestWarnings:
    def test_a_typo_placeholder_warns_and_still_loads(self, work, caplog):
        text = _packaged("worker").replace("{pr_title}", "{pr_title} {pr_titel}")
        _prompts(work, {"worker.md": text})
        with caplog.at_level(logging.WARNING, logger="prxref"):
            loaded = load_prompt_templates("prompts", source="--prompts-dir")
        assert loaded.worker == text
        (message,) = _warnings(caplog)
        assert message.startswith("--prompts-dir: ")
        assert "unknown placeholder(s) {pr_titel}" in message
        assert "worker.md" in message

    def test_a_placeholder_foreign_to_the_summary_warns(self, work, caplog):
        _prompts(work, {"summary.md": "{findings}\n{scope_example}\n{diff}"})
        with caplog.at_level(logging.WARNING, logger="prxref"):
            load_prompt_templates("prompts", source="PRXREF_PROMPTS_DIR")
        (message,) = _warnings(caplog)
        assert "unknown placeholder(s) {diff}, {scope_example}" in message

    def test_an_unknown_file_warns_and_dotfiles_are_skipped(self, work, caplog):
        _prompts(work, {
            "worker.md": _override("worker"),
            "notes.md": "scratch",
            "Summary.md": "{findings}",
            ".DS_Store": b"\x00",
            ".gitkeep": b"",
        })
        with caplog.at_level(logging.WARNING, logger="prxref"):
            loaded = load_prompt_templates("prompts", source="PRXREF_PROMPTS_DIR")
        (message,) = _warnings(caplog)
        assert message.startswith("PRXREF_PROMPTS_DIR: ")
        assert "ignoring unrecognised Summary.md, notes.md" in message
        assert ".DS_Store" not in message and ".gitkeep" not in message
        assert loaded.overridden == ("worker",)
        assert loaded.summary == _packaged("summary")

    @pytest.mark.parametrize("files", [{}, {".gitkeep": b""}], ids=["empty", "dotfiles-only"])
    def test_a_directory_without_templates_warns_and_uses_the_packaged_ones(self, work, caplog, files):
        _prompts(work, files)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            loaded = load_prompt_templates("prompts", source="PRXREF_PROMPTS_DIR")
        (message,) = _warnings(caplog)
        assert message.startswith("PRXREF_PROMPTS_DIR: ")
        assert "holds none of worker.md, systemic.md, summary.md" in message
        assert loaded.record() == {"dir": "prompts", "templates": {}}
        assert loaded.overrides == ()
        for name in TEMPLATE_NAMES:
            assert loaded.text(name) == _packaged(name)
            assert loaded.override(name) == ""

    def test_a_known_placeholder_above_the_marker_warns(self, work, caplog):
        head, marker, tail = _packaged("systemic").partition(_CONTEXT_MARKER)
        _prompts(work, {"systemic.md": head + "Title: {pr_title}\n\n" + marker + tail})
        with caplog.at_level(logging.WARNING, logger="prxref"):
            load_prompt_templates("prompts", source="PRXREF_PROMPTS_DIR")
        (message,) = _warnings(caplog)
        assert "{pr_title} above the '## Review Context' marker" in message

    def test_a_second_marker_warns(self, work, caplog):
        _prompts(work, {"worker.md": _packaged("worker") + "\n" + _CONTEXT_MARKER + "\n"})
        with caplog.at_level(logging.WARNING, logger="prxref"):
            load_prompt_templates("prompts", source="PRXREF_PROMPTS_DIR")
        (message,) = _warnings(caplog)
        assert "more than one '## Review Context' marker" in message


class TestOptionalSlots:
    @pytest.mark.parametrize("name", ["worker", "systemic"])
    def test_dropping_scope_example_is_allowed_silently(self, work, caplog, name):
        assert "scope_example" in placeholders(_packaged(name))
        text = _packaged(name).replace("{scope_example}", "")
        _prompts(work, {f"{name}.md": text})
        with caplog.at_level(logging.WARNING, logger="prxref"):
            loaded = load_prompt_templates("prompts", source="PRXREF_PROMPTS_DIR")
        assert _warnings(caplog) == []
        assert loaded.text(name) == text

    @pytest.mark.parametrize("name", ["worker", "systemic"])
    def test_the_rule_example_feature_slot_is_known_but_not_required(self, work, caplog, name):
        assert "rule_example" not in required_placeholders(name)
        text = _packaged(name) + "\n{rule_example}\n"
        _prompts(work, {f"{name}.md": text})
        with caplog.at_level(logging.WARNING, logger="prxref"):
            load_prompt_templates("prompts", source="PRXREF_PROMPTS_DIR")
        assert _warnings(caplog) == []

    def test_the_optional_set_is_exactly_the_feature_slots(self):
        assert OPTIONAL_PLACEHOLDERS == frozenset({"scope_example", "rule_example"})
