"""Local spec sources: symlink confinement, bounded reads, per-file failures, path-free reasons.

Covers SEC-3: ``_dispatch`` confines a file or directory source to the working
directory before anything stats or reads it (a), ``_fetch_dir`` skips every
symlinked entry (b), and ``_read_capped`` reads through
``text_inputs.read_capped_file`` in bounded memory (c). Also covers COR
backlog (2), the file truncation marker exactly past ``max_chars``; (4), one
bad file never failing its directory; (5), a dropped BOM; and SEC-5's
path-free reasons.

The symlinks are real, and every escape target sits in a sibling tmp dir that
is never under the working directory, so a refusal is a refusal of a real
escape. The end-to-end cases run the real ``fetch_specs`` and the real
reviewer through ``orchestrate_review``, so they assert what the LLM and the
PR actually see, each against a control that must come out different.
"""
from __future__ import annotations

import errno
import logging
import os
import tracemalloc
from pathlib import Path

import pytest
import requests

from prxref import specs
from prxref.orchestrator import orchestrate_review
from prxref.specs import SOURCE_TRUNCATION_MARKER, SPEC_DIR_MAX_FILES, fetch_specs
from tests.test_orchestrator import REF, FakeForge, _added_file_diff
from tests.test_specs import _FakeSession
from tests.test_specs_digest import _RecordingLLM

SECRET = "SECRET-TOKEN-CONTENT"
SECRET_LINE = f"Deployments MUST use the key {SECRET}."
API_LINE = "The API MUST return 200."
REFUSED = "PermissionError: resolves outside the working directory"
SUFFIXES = ".md/.markdown/.txt/.adoc"


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """A fresh working directory, made the process cwd for the test."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def outside(tmp_path_factory):
    """A directory never under the working directory, holding a spec-shaped secret."""
    out = tmp_path_factory.mktemp("outside")
    (out / "secret.md").write_text(SECRET_LINE + "\n", encoding="utf-8")
    return out


def _spec_dir(root: Path, *parts: str) -> Path:
    path = root.joinpath(*parts)
    path.mkdir(parents=True)
    (path / "api.md").write_text(API_LINE + "\n", encoding="utf-8")
    return path


def _fetch_one(source: str, max_chars: int = 1000) -> specs.SpecSource:
    (src,) = fetch_specs([source], max_chars=max_chars)
    return src


class TestConfinement:
    def test_a_symlinked_directory_entry_is_skipped_and_logged(self, workdir, outside, caplog):
        spec_dir = _spec_dir(workdir, "docs", "specs")
        (spec_dir / "zz.md").symlink_to(outside / "secret.md")
        with caplog.at_level(logging.WARNING, logger="prxref.specs"):
            src = _fetch_one("docs/specs")
        assert (src.kind, src.error) == ("dir", "")
        assert API_LINE in src.text
        assert SECRET not in src.text
        assert "## zz.md" not in src.text
        assert "zz.md (symlink)" in caplog.text

    def test_a_symlinked_entry_is_skipped_even_when_it_points_inside(self, workdir):
        spec_dir = _spec_dir(workdir, "docs", "specs")
        (spec_dir / "alias.md").symlink_to(spec_dir / "api.md")
        src = _fetch_one("docs/specs")
        assert src.error == ""
        assert "## api.md" in src.text
        assert "## alias.md" not in src.text
        assert src.text.count(API_LINE) == 1

    def test_a_symlinked_directory_root_is_refused(self, workdir, outside):
        (workdir / "docs").mkdir()
        (workdir / "docs" / "specs").symlink_to(outside, target_is_directory=True)
        src = _fetch_one("docs/specs")
        assert (src.kind, src.text, src.error) == ("", "", REFUSED)

    def test_a_symlinked_parent_component_is_refused(self, workdir, outside):
        (workdir / "p").mkdir()
        (workdir / "p" / "docs").symlink_to(outside, target_is_directory=True)
        src = _fetch_one("p/docs/secret.md")
        assert (src.kind, src.text, src.error) == ("", "", REFUSED)

    def test_a_relative_file_source_symlink_is_refused(self, workdir, outside):
        (workdir / "docs").mkdir()
        target = os.path.relpath(outside / "secret.md", workdir / "docs")
        (workdir / "docs" / "SPEC.md").symlink_to(target)
        src = _fetch_one("docs/SPEC.md")
        assert (src.kind, src.text, src.error) == ("", "", REFUSED)

    def test_a_dangling_escape_fails_exactly_like_a_live_one(self, workdir, outside):
        (workdir / "live.md").symlink_to(outside / "secret.md")
        (workdir / "dead.md").symlink_to(outside / "no-such-file.md")
        live, dead = fetch_specs(["live.md", "dead.md"], max_chars=1000)
        assert (live.kind, live.error) == (dead.kind, dead.error) == ("", REFUSED)

    def test_an_absolute_path_outside_the_cwd_is_read(self, workdir, outside):
        file_src, dir_src = fetch_specs(
            [str(outside / "secret.md"), str(outside)], max_chars=1000
        )
        assert (file_src.kind, file_src.error) == ("file", "")
        assert (dir_src.kind, dir_src.error) == ("dir", "")
        assert SECRET_LINE in file_src.text
        assert SECRET_LINE in dir_src.text

    def test_an_in_cwd_symlinked_file_source_is_read(self, workdir):
        spec_dir = _spec_dir(workdir, "docs")
        (workdir / "SPEC.md").symlink_to(spec_dir / "api.md")
        src = _fetch_one("SPEC.md")
        assert (src.kind, src.error) == ("file", "")
        assert src.text == API_LINE + "\n"

    def test_a_directory_of_only_symlinks_fails_naming_them(self, workdir, outside):
        (workdir / "specs").mkdir()
        (workdir / "specs" / "zz.md").symlink_to(outside / "secret.md")
        src = _fetch_one("specs")
        assert src.kind == "dir"
        assert src.text == ""
        assert src.error == f"no readable {SUFFIXES} files in directory; skipped zz.md (symlink)"

    def test_symlinked_entries_do_not_use_up_the_file_cap(self, workdir, outside):
        spec_dir = workdir / "specs"
        spec_dir.mkdir()
        (spec_dir / "a0.md").symlink_to(outside / "secret.md")
        for i in range(1, SPEC_DIR_MAX_FILES + 2):
            (spec_dir / f"f{i:02d}.md").write_text(f"Rule {i} MUST hold.\n", encoding="utf-8")
        src = _fetch_one("specs")
        assert sum(line.startswith("## ") for line in src.text.splitlines()) == SPEC_DIR_MAX_FILES
        assert src.text.startswith("## f01.md")
        assert f"## f{SPEC_DIR_MAX_FILES:02d}.md" in src.text
        assert f"## f{SPEC_DIR_MAX_FILES + 1:02d}.md" not in src.text


class TestBoundedRead:
    def test_a_large_file_is_scanned_not_held(self, workdir):
        line = b"The service MUST answer within one second.\n"
        data = line * (16 * 1024 * 1024 // len(line))
        (workdir / "big.md").write_bytes(data)
        session = requests.Session()
        tracemalloc.start()
        try:
            (src,) = fetch_specs(["big.md"], max_chars=1000, session=session)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert src.error == ""
        assert src.text == data.decode()[:1000] + SOURCE_TRUNCATION_MARKER.format(n=1000)
        assert peak < 1024 * 1024, f"peak {peak} bytes for a {len(data)} byte file"

    def test_a_file_exactly_at_the_cap_has_no_marker(self, workdir):
        (workdir / "spec.md").write_text("x" * 50, encoding="utf-8")
        assert _fetch_one("spec.md", max_chars=50).text == "x" * 50

    def test_one_char_past_the_cap_gets_the_marker_at_the_cap(self, workdir):
        (workdir / "spec.md").write_text("x" * 50 + "y", encoding="utf-8")
        text = _fetch_one("spec.md", max_chars=50).text
        assert text == "x" * 50 + SOURCE_TRUNCATION_MARKER.format(n=50)

    def test_the_cap_counts_characters_not_bytes(self, workdir):
        (workdir / "spec.md").write_text("é" * 50, encoding="utf-8")
        assert _fetch_one("spec.md", max_chars=50).text == "é" * 50

    def test_each_directory_file_is_capped_on_its_own(self, workdir):
        (workdir / "specs").mkdir()
        (workdir / "specs" / "a.md").write_text("a" * 30, encoding="utf-8")
        (workdir / "specs" / "b.md").write_text("b" * 10, encoding="utf-8")
        text = _fetch_one("specs", max_chars=20).text
        assert text == f"## a.md\n\n{'a' * 20}{SOURCE_TRUNCATION_MARKER.format(n=20)}\n\n## b.md\n\n{'b' * 10}"

    def test_a_bom_is_dropped_so_the_first_heading_survives(self, workdir):
        (workdir / "spec.md").write_bytes(b"\xef\xbb\xbf# Rules\r\nClients MUST retry.\r\n")
        assert _fetch_one("spec.md").text == "# Rules\nClients MUST retry.\n"


class TestPerFileFailures:
    def test_one_undecodable_file_does_not_fail_its_directory(self, workdir, caplog):
        spec_dir = _spec_dir(workdir, "specs")
        (spec_dir / "b.md").write_bytes(b"caf\xe9 MUST be served\n")
        (spec_dir / "c.md").write_text("Clients SHOULD retry.\n", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="prxref.specs"):
            src = _fetch_one("specs")
        assert (src.kind, src.error) == ("dir", "")
        assert "## api.md" in src.text
        assert "## c.md" in src.text
        assert "## b.md" not in src.text
        assert "b.md (UnicodeDecodeError: " in caplog.text

    def test_invalid_utf8_past_the_cap_still_fails_the_file(self, workdir):
        (workdir / "spec.md").write_bytes(b"x" * 5000 + b"\xff")
        src = _fetch_one("spec.md", max_chars=100)
        assert (src.kind, src.text) == ("file", "")
        assert src.error.startswith("UnicodeDecodeError: ")

    def test_a_directory_of_only_unreadable_files_fails_naming_them(self, workdir):
        (workdir / "specs").mkdir()
        (workdir / "specs" / "b.md").write_bytes(b"\xff\xfe")
        src = _fetch_one("specs")
        assert src.text == ""
        assert src.error.startswith(
            f"no readable {SUFFIXES} files in directory; skipped b.md (UnicodeDecodeError: "
        )
        assert str(workdir) not in src.error

    def test_a_cap_below_one_fails_the_source_not_each_file(self, workdir):
        _spec_dir(workdir, "specs")
        src = _fetch_one("specs", max_chars=0)
        assert src.error == "ValueError: max_chars must be at least 1, got 0"


class TestPathFreeReasons:
    def test_a_missing_path(self, workdir):
        absolute = _fetch_one(str(workdir / "Google Drive" / "spec.md"))
        relative = _fetch_one("private notes/spec.md")
        assert absolute.error == relative.error == "not a URL or path"

    def test_an_empty_directory(self, workdir):
        (workdir / "empty specs").mkdir()
        src = _fetch_one(str(workdir / "empty specs"))
        assert (src.kind, src.error) == ("dir", f"no {SUFFIXES} files in directory")

    def test_empty_and_blank_sources_never_read_the_cwd(self, workdir):
        _spec_dir(workdir, "specs")
        (workdir / "top.md").write_text(API_LINE, encoding="utf-8")
        for src in fetch_specs(["", "   "], max_chars=1000):
            assert (src.kind, src.text, src.error) == ("", "", "not a URL or path")

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
    def test_an_unreadable_file_reports_strerror_only(self, workdir):
        path = workdir / "locked spec.md"
        path.write_text(API_LINE, encoding="utf-8")
        path.chmod(0)
        try:
            src = _fetch_one(str(path))
        finally:
            path.chmod(0o600)
        assert (src.kind, src.error) == ("file", "PermissionError: Permission denied")

    def test_the_fence_drops_an_oserror_filename(self, workdir, monkeypatch):
        (workdir / "spec.md").write_text(API_LINE, encoding="utf-8")

        def vanished(src, path, max_chars):
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", "/srv/private/spec.md")

        monkeypatch.setattr(specs, "_fetch_file", vanished)
        src = _fetch_one("spec.md")
        assert src.error == "FileNotFoundError: No such file or directory"

    def test_an_oserror_without_strerror_keeps_its_message(self):
        for exc, expected in (
            (requests.ConnectionError("refused"), "ConnectionError: refused"),
            (ConnectionError("reset"), "ConnectionError: reset"),
            (RuntimeError("no network"), "RuntimeError: no network"),
        ):
            (src,) = fetch_specs(["https://example.com/a.md"], max_chars=10, session=_FakeSession(exc=exc))
            assert src.error == expected


def _review(spec_sources: list[str], *, post: bool = False) -> tuple[FakeForge, _RecordingLLM]:
    forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
    llm = _RecordingLLM()
    orchestrate_review(forge, REF, llm, post=post, spec_sources=spec_sources)
    assert len(llm.calls) == 2, "one chunk plus the sweep"
    return forge, llm


class TestEndToEnd:
    def test_a_symlinked_secret_never_reaches_a_prompt(self, workdir, outside):
        spec_dir = _spec_dir(workdir, "docs", "specs")
        (spec_dir / "zz.md").symlink_to(outside / "secret.md")
        _forge, llm = _review(["docs/specs"])
        for system, user in llm.calls:
            assert API_LINE in user
            assert SECRET not in system + user

    def test_control_the_same_file_committed_as_a_regular_file_reaches_every_prompt(self, workdir):
        spec_dir = _spec_dir(workdir, "docs", "specs")
        (spec_dir / "zz.md").write_text(SECRET_LINE + "\n", encoding="utf-8")
        _forge, llm = _review(["docs/specs"])
        for _system, user in llm.calls:
            assert SECRET in user

    def test_the_posted_note_names_no_local_path(self, workdir):
        (workdir / "empty specs").mkdir()
        sources = [
            str(workdir / "Google Drive" / "spec.md"),
            str(workdir / "empty specs"),
            "private notes/spec.md",
        ]
        forge, _llm = _review(sources, post=True)
        (summary,) = forge.summaries
        assert "Spec fetch failed for 3 source(s)" in summary
        assert f"source 2 (dir): no {SUFFIXES} files in directory" in summary
        for segment in ("Google Drive", "empty specs", "private notes", str(workdir)):
            assert segment not in summary
