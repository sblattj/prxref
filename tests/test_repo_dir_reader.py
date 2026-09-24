"""``RepoDir`` reads a local working tree in place of a forge, for ``--repo-dir``.

``read`` mirrors the forge adapters' ``get_file_content`` contract (never
raises, None for missing/binary/oversized/unsafe paths) and ``list_files``
gives a bounded, confined listing. The confinement check must use
``os.path.realpath`` and not a string prefix: a sibling directory whose name
extends the root's name (``.../r2`` next to ``.../r``) must not be mistaken
for being inside it.
"""
from __future__ import annotations

import os
import re

import pytest

from prxref.forges import repo_dir as repo_dir_module
from prxref.forges.repo_dir import RepoDir


def test_read_normal_file(tmp_path):
    (tmp_path / "a.py").write_text("print('hi')\n")
    rd = RepoDir(tmp_path)
    assert rd.read("a.py") == "print('hi')\n"


def test_read_missing_file(tmp_path):
    rd = RepoDir(tmp_path)
    assert rd.read("nope.py") is None


def test_read_directory(tmp_path):
    (tmp_path / "sub").mkdir()
    rd = RepoDir(tmp_path)
    assert rd.read("sub") is None


@pytest.mark.parametrize(
    "unsafe",
    ["../x", "/etc/hosts", "a/../../x", "a\\b", ""],
)
def test_unsafe_paths_read_none(tmp_path, unsafe):
    rd = RepoDir(tmp_path)
    assert rd.read(unsafe) is None


def test_symlink_to_file_outside_root_reads_none(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("nope\n")
    link = root / "link.txt"
    link.symlink_to(secret)
    rd = RepoDir(root)
    assert rd.read("link.txt") is None


def test_symlink_to_file_inside_root_reads_fine(tmp_path):
    (tmp_path / "real.txt").write_text("hello\n")
    link = tmp_path / "link.txt"
    link.symlink_to(tmp_path / "real.txt")
    rd = RepoDir(tmp_path)
    assert rd.read("link.txt") == "hello\n"


def test_symlinked_directory_outside_root_not_walked(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "kept.txt").write_text("keep\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope\n")
    (root / "escape").symlink_to(outside)
    rd = RepoDir(root)
    paths, complete = rd.list_files()
    assert paths == ("kept.txt",)
    assert complete is True


def test_git_config_read_none_and_never_listed(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n")
    (tmp_path / "kept.txt").write_text("keep\n")
    rd = RepoDir(tmp_path)
    assert rd.read(".git/config") is None
    paths, complete = rd.list_files()
    assert paths == ("kept.txt",)
    assert complete is True


def test_file_exactly_512kib_reads(tmp_path):
    size = 512 * 1024
    (tmp_path / "big.txt").write_bytes(b"a" * size)
    rd = RepoDir(tmp_path)
    content = rd.read("big.txt")
    assert content is not None
    assert len(content) == size


def test_file_512kib_plus_one_byte_reads_none(tmp_path):
    size = 512 * 1024 + 1
    (tmp_path / "toobig.txt").write_bytes(b"a" * size)
    rd = RepoDir(tmp_path)
    assert rd.read("toobig.txt") is None


def test_nul_byte_reads_none(tmp_path):
    (tmp_path / "bin.dat").write_bytes(b"abc\x00def")
    rd = RepoDir(tmp_path)
    assert rd.read("bin.dat") is None


def test_invalid_utf8_is_replaced_not_raised(tmp_path):
    (tmp_path / "bad.txt").write_bytes(b"caf\xe9 au lait")
    rd = RepoDir(tmp_path)
    content = rd.read("bad.txt")
    assert content is not None
    assert "\N{REPLACEMENT CHARACTER}" in content


def test_listing_is_sorted_and_posix(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "a.txt").write_text("1\n")
    (tmp_path / "b" / "z.txt").write_text("2\n")
    (tmp_path / "b" / "a.txt").write_text("3\n")
    rd = RepoDir(tmp_path)
    paths, complete = rd.list_files()
    assert paths == ("a.txt", "b/a.txt", "b/z.txt")
    assert complete is True
    assert all("\\" not in p for p in paths)


def test_listing_cap_stops_and_reports_incomplete(tmp_path, monkeypatch):
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x\n")
    monkeypatch.setattr(repo_dir_module, "_MAX_LISTED_FILES", 3)
    rd = RepoDir(tmp_path)
    paths, complete = rd.list_files()
    assert len(paths) == 3
    assert complete is False


def test_listing_exactly_at_cap_is_complete(tmp_path, monkeypatch):
    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text("x\n")
    monkeypatch.setattr(repo_dir_module, "_MAX_LISTED_FILES", 3)
    rd = RepoDir(tmp_path)
    paths, complete = rd.list_files()
    assert len(paths) == 3
    assert complete is True


def test_root_that_does_not_exist_raises_value_error_naming_it(tmp_path):
    missing = tmp_path / "nope"
    with pytest.raises(ValueError, match=re.escape(os.fspath(missing))):
        RepoDir(missing)


def test_confinement_uses_realpath_not_string_prefix(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    sibling = tmp_path / "r2"
    sibling.mkdir()
    secret = sibling / "secret"
    secret.write_text("nope\n")
    link = root / "link.txt"
    link.symlink_to(secret)
    rd = RepoDir(root)
    assert rd.read("link.txt") is None
    paths, complete = rd.list_files()
    assert paths == ()
    assert complete is True
