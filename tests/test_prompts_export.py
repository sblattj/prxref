"""``prxref prompts export DIR [--force]`` (#11): the packaged templates as an override starting point."""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

from prxref import reviewer
from prxref.cli import _build_parser, main
from prxref.llm import ConfigError
from prxref.prompt_templates import TEMPLATE_NAMES, export_prompt_templates, load_prompt_templates

FILES = tuple(f"{name}.md" for name in TEMPLATE_NAMES)
MINE = b"our team's own template\n"


def _packaged(name: str) -> bytes:
    return reviewer.load_prompt(name).encode("utf-8")


def _resource(fname: str) -> bytes:
    return resources.files("prxref").joinpath("prompts").joinpath(fname).read_bytes()


def _state(d: Path) -> dict[str, tuple[bool, bytes, int]]:
    return {
        p.name: (p.is_symlink(), os.readlink(p).encode() if p.is_symlink() else p.read_bytes(), p.lstat().st_mtime_ns)
        for p in sorted(d.iterdir())
    }


def _seed(d: Path, names: tuple[str, ...]) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for fname in names:
        (d / fname).write_bytes(MINE)


@pytest.fixture
def work(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


class TestExportedFiles:
    @pytest.mark.parametrize("name", TEMPLATE_NAMES)
    def test_reviewer_load_prompt_text_encodes_back_to_the_raw_packaged_bytes(self, name):
        assert _packaged(name) == _resource(f"{name}.md")

    @pytest.mark.parametrize("name", TEMPLATE_NAMES)
    def test_each_exported_file_is_byte_equal_to_the_packaged_template(self, work, name):
        export_prompt_templates("prompts")
        assert (work / "prompts" / f"{name}.md").read_bytes() == _packaged(name)

    def test_exactly_the_three_overridable_templates_are_written_and_never_judge(self, work):
        packaged = {p.name for p in resources.files("prxref").joinpath("prompts").iterdir() if p.name.endswith(".md")}
        assert "judge.md" in packaged
        assert packaged > set(FILES)
        export_prompt_templates("prompts")
        assert sorted(os.listdir(work / "prompts")) == sorted(FILES)
        assert not (work / "prompts" / "judge.md").exists()

    def test_it_returns_the_written_paths_as_given_in_template_order(self, work):
        assert export_prompt_templates("prompts") == [os.path.join("prompts", f) for f in FILES]

    def test_a_path_object_is_accepted_and_returned_as_strings(self, work):
        written = export_prompt_templates(work / "prompts")
        assert written == [str(work / "prompts" / f) for f in FILES]

    def test_a_missing_directory_is_created_with_its_parents(self, work):
        dest = work / "a" / "b" / "prompts"
        export_prompt_templates(dest)
        assert sorted(os.listdir(dest)) == sorted(FILES)

    def test_an_existing_directory_with_other_files_keeps_them(self, work):
        _seed(work / "prompts", ("README.md",))
        export_prompt_templates("prompts")
        assert sorted(os.listdir(work / "prompts")) == sorted((*FILES, "README.md"))
        assert (work / "prompts" / "README.md").read_bytes() == MINE


class TestRefusal:
    def test_every_existing_target_is_refused_naming_each_and_force_and_nothing_is_modified(self, work):
        d = work / "prompts"
        _seed(d, FILES)
        before = _state(d)
        with pytest.raises(ConfigError, match="--force") as exc:
            export_prompt_templates("prompts")
        for fname in FILES:
            assert repr(os.path.join("prompts", fname)) in str(exc.value)
        assert _state(d) == before

    @pytest.mark.parametrize("fname", FILES)
    def test_one_existing_target_refuses_the_whole_export(self, work, fname):
        d = work / "prompts"
        _seed(d, (fname,))
        before = _state(d)
        with pytest.raises(ConfigError, match="--force") as exc:
            export_prompt_templates("prompts")
        assert repr(os.path.join("prompts", fname)) in str(exc.value)
        for other in set(FILES) - {fname}:
            assert other not in str(exc.value)
        assert _state(d) == before
        assert os.listdir(d) == [fname]

    def test_a_dangling_symlink_counts_as_an_existing_target(self, work):
        d = work / "prompts"
        d.mkdir()
        (d / "worker.md").symlink_to(work / "nowhere.md")
        before = _state(d)
        with pytest.raises(ConfigError, match="worker.md"):
            export_prompt_templates("prompts")
        assert _state(d) == before
        assert not (work / "nowhere.md").exists()

    def test_a_second_export_into_the_same_directory_is_refused(self, work):
        export_prompt_templates("prompts")
        before = _state(work / "prompts")
        with pytest.raises(ConfigError, match="--force"):
            export_prompt_templates("prompts")
        assert _state(work / "prompts") == before

    def test_force_overwrites_every_existing_target(self, work):
        d = work / "prompts"
        _seed(d, FILES)
        export_prompt_templates("prompts", force=True)
        for name in TEMPLATE_NAMES:
            assert (d / f"{name}.md").read_bytes() == _packaged(name)

    def test_force_fills_a_partly_seeded_directory(self, work):
        d = work / "prompts"
        _seed(d, ("summary.md",))
        assert export_prompt_templates("prompts", force=True) == [os.path.join("prompts", f) for f in FILES]
        for name in TEMPLATE_NAMES:
            assert (d / f"{name}.md").read_bytes() == _packaged(name)

    def test_force_replaces_a_symlink_rather_than_writing_through_it(self, work):
        shared = work / "shared.md"
        shared.write_bytes(MINE)
        d = work / "prompts"
        d.mkdir()
        (d / "worker.md").symlink_to(shared)
        export_prompt_templates("prompts", force=True)
        assert not (d / "worker.md").is_symlink()
        assert (d / "worker.md").read_bytes() == _packaged("worker")
        assert shared.read_bytes() == MINE

    @pytest.mark.parametrize("dest", ["blocker", os.path.join("blocker", "sub")])
    def test_a_destination_that_cannot_be_a_directory_is_a_config_error(self, work, dest):
        (work / "blocker").write_bytes(MINE)
        with pytest.raises(ConfigError, match="prompts export: cannot write"):
            export_prompt_templates(dest, force=True)
        assert (work / "blocker").read_bytes() == MINE

    def test_a_directory_standing_in_a_targets_place_is_a_config_error_under_force(self, work):
        (work / "prompts" / "worker.md").mkdir(parents=True)
        with pytest.raises(ConfigError, match="prompts export: cannot write"):
            export_prompt_templates("prompts", force=True)

    @pytest.mark.parametrize("dest", ["", "   "])
    def test_an_empty_destination_is_a_config_error_and_writes_nothing(self, work, dest):
        with pytest.raises(ConfigError, match="DIR"):
            export_prompt_templates(dest)
        assert os.listdir(work) == []


class TestRoundTrip:
    @pytest.mark.parametrize("source", ["--prompts-dir", "PRXREF_PROMPTS_DIR"])
    def test_the_exported_directory_loads_silently_and_overrides_nothing(self, work, caplog, source):
        export_prompt_templates("prompts")
        with caplog.at_level(logging.DEBUG, logger="prxref"):
            loaded = load_prompt_templates("prompts", source=source)
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert loaded is not None
        assert loaded.overridden == TEMPLATE_NAMES
        for name in TEMPLATE_NAMES:
            assert loaded.override(name) == reviewer.load_prompt(name)
            assert loaded.text(name) == reviewer.load_prompt(name)
            recorded = loaded.record()["templates"][name]
            assert recorded["sha256"] == hashlib.sha256(_resource(f"{name}.md")).hexdigest()


class TestCli:
    def test_export_exits_0_and_prints_one_line_per_written_file(self, work, capsys):
        assert main(["prompts", "export", "prompts"]) == 0
        out, err = capsys.readouterr()
        assert out.splitlines() == [os.path.join("prompts", f) for f in FILES]
        assert err == ""
        for name in TEMPLATE_NAMES:
            assert (work / "prompts" / f"{name}.md").read_bytes() == _packaged(name)

    @pytest.mark.parametrize("seeded", [FILES, ("systemic.md",)], ids=["all", "one"])
    def test_an_existing_template_exits_2_naming_force_and_modifies_nothing(self, work, capsys, seeded):
        d = work / "prompts"
        _seed(d, seeded)
        before = _state(d)
        assert main(["prompts", "export", "prompts"]) == 2
        out, err = capsys.readouterr()
        assert out == ""
        assert err.startswith("configuration error: prompts export: refusing to overwrite")
        assert "--force" in err
        for fname in seeded:
            assert fname in err
        assert _state(d) == before

    def test_force_overwrites_and_exits_0(self, work, capsys):
        d = work / "prompts"
        _seed(d, FILES)
        assert main(["prompts", "export", "prompts", "--force"]) == 0
        assert len(capsys.readouterr().out.splitlines()) == 3
        for name in TEMPLATE_NAMES:
            assert (d / f"{name}.md").read_bytes() == _packaged(name)

    def test_an_unwritable_destination_exits_2(self, work, capsys):
        (work / "blocker").write_bytes(MINE)
        assert main(["prompts", "export", os.path.join("blocker", "sub")]) == 2
        assert capsys.readouterr().err.startswith("configuration error: prompts export: cannot write")

    def test_prompts_without_an_action_exits_2_with_usage(self, capsys):
        assert main(["prompts"]) == 2
        assert "usage:" in capsys.readouterr().err.lower()

    def test_export_without_dir_is_an_argparse_usage_error(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["prompts", "export"])
        assert exc.value.code == 2
        assert "DIR" in capsys.readouterr().err

    def test_force_defaults_off(self):
        assert _build_parser().parse_args(["prompts", "export", "d"]).force is False
        assert _build_parser().parse_args(["prompts", "export", "d", "--force"]).force is True

    def test_the_module_entry_point_exports_then_refuses(self, work):
        cmd = [sys.executable, "-m", "prxref.cli", "prompts", "export", "prompts"]
        first = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ))
        assert first.returncode == 0, first.stderr
        assert first.stdout.splitlines() == [os.path.join("prompts", f) for f in FILES]
        second = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ))
        assert second.returncode == 2
        assert "--force" in second.stderr
