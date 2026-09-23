"""Tests for prxref.text_inputs: the bounded, fingerprinted loader shared by
review rules, ticket context and local spec sources.

The confinement cases build real symlinks under pytest's tmp dirs, with the
escape target in a sibling directory that is never under the working
directory, so a pass means the loader refused a real escape.
"""
from __future__ import annotations

import ast
import dataclasses
import errno
import hashlib
import json
import os
import sys
import threading
import tracemalloc
from pathlib import Path

import pytest

from prxref import text_inputs
from prxref.text_inputs import (
    CappedText,
    cap_text,
    check_readable_path,
    confine_to_cwd,
    decode_text,
    read_capped_file,
)

SECRET = "SECRET-TOKEN-CONTENT"


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """A fresh working directory, made the process cwd for the test."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def outside(tmp_path_factory):
    """A directory that is never under the working directory, holding a secret."""
    out = tmp_path_factory.mktemp("outside")
    (out / "secret.txt").write_text(SECRET, encoding="utf-8")
    return out


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class TestFingerprint:
    def test_sha256_matches_the_raw_file_bytes(self, workdir):
        data = b"\xef\xbb\xbfrule one\r\nrule two\rrule three\n"
        path = _write(workdir / "rules.md", data)
        capped = read_capped_file(path, 5)
        assert capped.sha256 == hashlib.sha256(data).hexdigest()

    def test_hash_does_not_change_with_the_cap(self, workdir):
        path = _write(workdir / "rules.md", b"abcdefghij" * 100)
        assert read_capped_file(path, 5).sha256 == read_capped_file(path, 5000).sha256

    def test_one_changed_byte_changes_the_hash(self, workdir):
        first = read_capped_file(_write(workdir / "a.md", b"abcdef"), 3)
        second = read_capped_file(_write(workdir / "b.md", b"abcdeF"), 3)
        assert first.text == second.text
        assert first.sha256 != second.sha256

    def test_empty_file_hashes_the_empty_string(self, workdir):
        capped = read_capped_file(_write(workdir / "empty.md", b""), 10)
        assert capped == CappedText("", hashlib.sha256(b"").hexdigest(), 0, False, 10)


class TestCap:
    def test_len_equal_to_cap_is_not_truncated(self, workdir):
        capped = read_capped_file(_write(workdir / "f.md", b"abcde"), 5)
        assert (capped.text, capped.chars, capped.truncated) == ("abcde", 5, False)

    def test_one_over_the_cap_is_truncated(self, workdir):
        capped = read_capped_file(_write(workdir / "f.md", b"abcdef"), 5)
        assert (capped.text, capped.chars, capped.truncated) == ("abcde", 6, True)

    def test_kept_text_is_exactly_the_prefix_and_carries_no_marker(self, workdir):
        body = "line one\nline two\nline three\n"
        capped = read_capped_file(_write(workdir / "f.md", body.encode()), 12)
        assert capped.text == body[:12]
        assert "truncated" not in capped.text

    def test_chars_counts_the_full_text_not_the_kept_text(self, workdir):
        capped = read_capped_file(_write(workdir / "f.md", "é".encode() * 1000), 10)
        assert capped.chars == 1000
        assert len(capped.text) == 10

    @pytest.mark.parametrize("cap", [0, -1])
    def test_cap_below_one_raises_value_error(self, workdir, cap):
        with pytest.raises(ValueError, match="max_chars"):
            cap_text("text", cap)
        with pytest.raises(ValueError, match="max_chars"):
            read_capped_file(_write(workdir / "f.md", b"text"), cap)

    def test_cap_is_checked_before_the_filesystem_is_touched(self, workdir):
        with pytest.raises(ValueError, match="max_chars"):
            read_capped_file(workdir / "missing.md", 0)


class TestDecoding:
    def test_bom_stripped_and_crlf_and_cr_normalised(self, workdir):
        capped = read_capped_file(_write(workdir / "f.md", b"\xef\xbb\xbfa\r\nb\rc\n"), 100)
        assert capped.text == "a\nb\nc\n"
        assert capped.chars == 6

    def test_only_a_leading_bom_is_dropped(self):
        assert decode_text(b"\xef\xbb\xbfa\xef\xbb\xbfb") == "a﻿b"

    def test_cr_before_crlf_is_two_newlines(self):
        assert decode_text(b"a\r\r\nb") == "a\n\nb"

    def test_invalid_utf8_raises_unicode_decode_error(self, workdir):
        with pytest.raises(UnicodeDecodeError):
            read_capped_file(_write(workdir / "f.md", b"ok\xff\xfe"), 100)
        with pytest.raises(UnicodeDecodeError):
            decode_text(b"ok\xff\xfe")

    def test_invalid_utf8_after_the_cap_still_raises(self, workdir):
        with pytest.raises(UnicodeDecodeError):
            read_capped_file(_write(workdir / "f.md", b"a" * 200_000 + b"\xff"), 10)

    def test_a_truncated_multibyte_sequence_at_eof_raises(self, workdir):
        with pytest.raises(UnicodeDecodeError):
            read_capped_file(_write(workdir / "f.md", b"abc" + "é".encode()[:1]), 100)

    @pytest.mark.parametrize("split", [65535, 65536, 65537])
    def test_crlf_split_across_a_read_chunk_counts_once(self, workdir, split):
        data = b"x" * (split - 1) + b"\r\n" + b"y" * 10
        capped = read_capped_file(_write(workdir / "f.md", data), 10**7)
        assert capped.chars == (split - 1) + 1 + 10
        assert capped.text.count("\n") == 1

    @pytest.mark.parametrize("split", [65535, 65536])
    def test_multibyte_char_split_across_a_read_chunk(self, workdir, split):
        data = b"x" * (split - 1) + "é".encode() + "🟦".encode() + b"z"
        capped = read_capped_file(_write(workdir / "f.md", data), 10**7)
        assert capped.chars == (split - 1) + 3
        assert capped.text.endswith("é🟦z")


_PARITY_SAMPLES = [
    b"",
    b"plain ascii text",
    b"\xef\xbb\xbfbom then text",
    b"\xef\xbb\xbf",
    b"a\r\nb\r\nc",
    b"a\rb\rc\r",
    b"\r\n\r\n\r",
    b"a\r\r\nb\n\rc",
    "café — \U0001f7e6 done\r\n".encode(),
    "\U0001f7e5\r\U0001f7e7\r\né".encode(),
]


class TestStreamingMatchesWholeFileDecoding:
    """The streaming reader and ``cap_text(decode_text(...))`` must agree on
    every cut point, so rules (whole read) and ticket/spec (streamed) record
    identical fingerprints for identical files.
    """

    @pytest.mark.parametrize("data", _PARITY_SAMPLES, ids=range(len(_PARITY_SAMPLES)))
    def test_every_cap_agrees(self, workdir, data):
        path = _write(workdir / "f.md", data)
        digest = hashlib.sha256(data).hexdigest()
        full = decode_text(data)
        for cap in range(1, len(full) + 3):
            assert read_capped_file(path, cap) == cap_text(full, cap, sha256=digest), cap

    @pytest.mark.parametrize("cap", [65534, 65535, 65536, 65537])
    def test_caps_at_the_chunk_boundary_agree(self, workdir, cap):
        data = b"x" * 65535 + b"\r\n" + "é\r".encode() * 20
        path = _write(workdir / "f.md", data)
        expected = cap_text(decode_text(data), cap, sha256=hashlib.sha256(data).hexdigest())
        assert read_capped_file(path, cap) == expected


class TestCapText:
    def test_default_sha256_covers_the_whole_text_before_the_cap(self):
        capped = cap_text("abcdef", 3)
        assert capped.sha256 == hashlib.sha256(b"abcdef").hexdigest()
        assert (capped.text, capped.chars, capped.truncated, capped.max_chars) == ("abc", 6, True, 3)

    def test_an_explicit_sha256_is_recorded_as_given(self):
        assert cap_text("abc", 10, sha256="f" * 64).sha256 == "f" * 64

    def test_a_lone_surrogate_does_not_crash_the_default_hash(self):
        assert len(cap_text("a\ud800b", 10).sha256) == 64


class TestRecord:
    def test_record_is_json_serialisable_with_fixed_keys(self, workdir):
        capped = read_capped_file(_write(workdir / "f.md", b"abcdef"), 3)
        record = capped.record()
        assert list(record) == ["sha256", "chars", "max_chars", "truncated"]
        assert json.loads(json.dumps(record)) == {
            "sha256": hashlib.sha256(b"abcdef").hexdigest(),
            "chars": 6,
            "max_chars": 3,
            "truncated": True,
        }

    def test_the_record_never_carries_the_text(self):
        assert "SECRET" not in json.dumps(cap_text(SECRET, 100).record())

    def test_capped_text_is_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            cap_text("abc", 10).text = "changed"


class TestNonRegularFiles:
    def test_missing_file_raises_file_not_found_naming_the_path_as_given(self, workdir):
        with pytest.raises(FileNotFoundError) as info:
            read_capped_file("missing.md", 10)
        assert info.value.filename == "missing.md"

    def test_directory_is_refused(self, workdir):
        (workdir / "sub").mkdir()
        with pytest.raises(IsADirectoryError) as info:
            read_capped_file("sub", 10)
        assert info.value.errno == errno.EISDIR
        assert info.value.filename == "sub"
        with pytest.raises(IsADirectoryError):
            check_readable_path("sub")

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
    def test_fifo_is_refused_without_blocking(self, workdir):
        os.mkfifo("pipe.md")
        outcome: list[BaseException | None] = []

        def attempt() -> None:
            try:
                read_capped_file("pipe.md", 10)
                outcome.append(None)
            except BaseException as e:
                outcome.append(e)

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(5)
        assert not worker.is_alive(), "reading a FIFO blocked"
        assert type(outcome[0]) is OSError
        assert outcome[0].strerror == "not a regular file"
        assert outcome[0].filename == "pipe.md"

    @pytest.mark.skipif(not os.path.exists("/dev/zero"), reason="needs /dev/zero")
    def test_device_is_refused(self):
        with pytest.raises(OSError) as info:
            read_capped_file("/dev/zero", 10, confine=False)
        assert info.value.strerror == "not a regular file"


class TestBoundedMemory:
    def test_a_large_file_is_scanned_not_held(self, workdir):
        line = b"line of text\n"
        data = line * (4 * 1024 * 1024 // len(line))
        path = _write(workdir / "big.md", data)
        tracemalloc.start()
        try:
            capped = read_capped_file(path, 1000)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert capped.truncated
        assert capped.chars == len(data)
        assert capped.sha256 == hashlib.sha256(data).hexdigest()
        assert peak < 1024 * 1024, f"peak {peak} bytes for a {len(data)} byte file"


class TestConfinement:
    """SEC-3 rule (a): a path under the working directory must not resolve out of it."""

    def test_relative_file_symlink_escaping_the_cwd_is_refused(self, workdir, outside):
        (workdir / "docs").mkdir()
        os.symlink(outside / "secret.txt", workdir / "docs" / "SPEC.md")
        with pytest.raises(PermissionError) as info:
            read_capped_file("docs/SPEC.md", 100)
        assert info.value.errno == errno.EACCES
        assert info.value.strerror == "resolves outside the working directory"
        assert info.value.filename == "docs/SPEC.md"

    def test_the_refusal_never_names_the_symlink_target(self, workdir, outside):
        os.symlink(outside / "secret.txt", workdir / "SPEC.md")
        with pytest.raises(PermissionError) as info:
            read_capped_file("SPEC.md", 100)
        assert str(outside) not in str(info.value)
        assert "secret" not in str(info.value)

    def test_symlinked_directory_root_is_refused(self, workdir, outside):
        (workdir / "docs").mkdir()
        os.symlink(outside, workdir / "docs" / "specs")
        with pytest.raises(PermissionError):
            confine_to_cwd("docs/specs")
        with pytest.raises(PermissionError):
            read_capped_file("docs/specs/secret.txt", 100)

    def test_symlinked_parent_component_is_refused(self, workdir, outside):
        (workdir / "p").mkdir()
        os.symlink(outside, workdir / "p" / "docs")
        with pytest.raises(PermissionError):
            read_capped_file(os.path.join("p", "docs", "secret.txt"), 100)

    def test_dotdot_after_a_symlinked_directory_is_refused(self, workdir, outside):
        deep = outside / "deep" / "dir"
        deep.mkdir(parents=True)
        _write(outside / "deep" / "x.md", b"escaped")
        os.symlink(deep, workdir / "sub")
        with pytest.raises(PermissionError):
            read_capped_file("sub/../x.md", 100)

    def test_absolute_spelling_of_the_cwd_is_confined_too(self, workdir, outside):
        os.symlink(outside / "secret.txt", workdir / "SPEC.md")
        with pytest.raises(PermissionError):
            read_capped_file(str(workdir / "SPEC.md"), 100)

    def test_the_cwd_reached_through_a_symlinked_alias_is_confined(self, workdir, outside, tmp_path):
        (workdir / "docs").mkdir()
        os.symlink(outside / "secret.txt", workdir / "docs" / "SPEC.md")
        _write(workdir / "docs" / "ok.md", b"fine")
        alias = tmp_path / "alias"
        os.symlink(workdir, alias)
        with pytest.raises(PermissionError):
            read_capped_file(str(alias / "docs" / "SPEC.md"), 100)
        assert read_capped_file(str(alias / "docs" / "ok.md"), 100).text == "fine"

    def test_a_symlinked_alias_into_a_subdirectory_is_confined(self, workdir, outside, tmp_path):
        (workdir / "docs").mkdir()
        os.symlink(outside / "secret.txt", workdir / "docs" / "SPEC.md")
        alias = tmp_path / "docs-alias"
        os.symlink(workdir / "docs", alias)
        with pytest.raises(PermissionError):
            read_capped_file(str(alias / "SPEC.md"), 100)

    def test_dangling_escape_is_refused_before_existence_is_checked(self, workdir, outside):
        os.symlink(outside / "no-such-file", workdir / "SPEC.md")
        with pytest.raises(PermissionError):
            read_capped_file("SPEC.md", 100)

    def test_dangling_symlink_inside_the_cwd_is_just_missing(self, workdir):
        os.symlink(workdir / "no-such-file", workdir / "SPEC.md")
        with pytest.raises(FileNotFoundError):
            read_capped_file("SPEC.md", 100)

    def test_symlink_that_stays_inside_the_cwd_is_allowed(self, workdir):
        _write(workdir / "real.md", b"inside")
        os.symlink("real.md", workdir / "link.md")
        assert read_capped_file("link.md", 100).text == "inside"
        assert confine_to_cwd("link.md") == os.path.realpath(workdir / "real.md")

    def test_absolute_path_outside_cwd_allowed(self, workdir, outside):
        capped = read_capped_file(str(outside / "secret.txt"), 100)
        assert capped.text == SECRET

    def test_relative_dotdot_path_outside_cwd_is_the_operators_choice(self, workdir, outside, monkeypatch):
        monkeypatch.chdir(outside)
        (outside / "inner").mkdir()
        monkeypatch.chdir(outside / "inner")
        assert read_capped_file("../secret.txt", 100).text == SECRET

    def test_confine_false_follows_an_escaping_link(self, workdir, outside):
        os.symlink(outside / "secret.txt", workdir / "link.md")
        assert read_capped_file("link.md", 100, confine=False).text == SECRET

    def test_confine_to_cwd_returns_the_realpath_and_needs_no_file(self, workdir):
        assert confine_to_cwd("not-yet/there.md") == os.path.realpath(workdir / "not-yet" / "there.md")
        assert confine_to_cwd(".") == os.path.realpath(workdir)

    def test_a_directory_inside_the_cwd_is_allowed(self, workdir):
        (workdir / "docs" / "specs").mkdir(parents=True)
        assert confine_to_cwd(Path("docs/specs")) == os.path.realpath(workdir / "docs" / "specs")

    def test_check_readable_path_returns_the_resolved_regular_file(self, workdir):
        _write(workdir / "real.md", b"x")
        os.symlink("real.md", workdir / "link.md")
        assert check_readable_path("link.md") == os.path.realpath(workdir / "real.md")


class TestModuleIsALeaf:
    def test_imports_only_the_standard_library(self):
        tree = ast.parse(Path(text_inputs.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, "text_inputs must not import from the package"
                imported.add((node.module or "").split(".")[0])
        assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported
