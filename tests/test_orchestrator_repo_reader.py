"""The run-scoped repository reader (#17 T12): ``prxref.repo_reader``.

Plain fakes only: no forge adapter, no network. The single-flight tests hold
the first fetch (or listing) open on a ``threading.Event`` while seven more
threads ask for the same thing, and assert the call count BEFORE releasing
it, so a reader that lets a second caller fetch while the first is in
flight fails here rather than passing on timing luck.
"""
from __future__ import annotations

import logging
import threading
import time

import pytest

from prxref.forges.base import PathListing
from prxref.forges.replay import ReplayForge
from prxref.forges.repo_dir import RepoDir
from prxref.repo_reader import (
    MAX_CHUNK_READS,
    MAX_RUN_READS,
    READER_KINDS,
    RepoReader,
    forge_reader,
    repo_dir_reader,
)

WAIT = 5.0
SHA = "c" * 40
REF = object()
THREADS = 8


class Fetch:
    """A recording ``fetch`` over a dict; paths it does not hold read as None."""

    def __init__(self, files: dict[str, object] | None = None):
        self.files = files if files is not None else {}
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, path: str):
        with self.lock:
            self.calls.append(path)
        return self.files.get(path)


class BlockingFetch(Fetch):
    """A ``Fetch`` whose FIRST call signals ``entered`` and then blocks on ``release``."""

    def __init__(self, files: dict[str, object]):
        super().__init__(files)
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, path: str):
        with self.lock:
            self.calls.append(path)
            first = len(self.calls) == 1
        if first:
            self.entered.set()
            self.release.wait(WAIT)
        return self.files.get(path)


class Lister:
    """A recording ``lister`` returning a fixed result, or raising it when it is an exception."""

    def __init__(self, result: object):
        self.result = result
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _reader(fetch=None, lister=None, **kw) -> RepoReader:
    return RepoReader(fetch if fetch is not None else Fetch(), lister, kind=kw.pop("kind", "forge"), **kw)


def _run_threads(target, count: int = THREADS) -> tuple[list[threading.Thread], list[object]]:
    """Start ``count`` threads that pass a barrier together, then call ``target``."""
    barrier = threading.Barrier(count)
    results: list[object] = []
    results_lock = threading.Lock()

    def work():
        barrier.wait(WAIT)
        value = target()
        with results_lock:
            results.append(value)

    threads = [threading.Thread(target=work, daemon=True) for _ in range(count)]
    for thread in threads:
        thread.start()
    return threads, results


def _join(threads: list[threading.Thread]) -> None:
    for thread in threads:
        thread.join(WAIT)
    assert not any(thread.is_alive() for thread in threads)


class TestModuleConstants:
    def test_the_caps_match_the_design_budget(self):
        assert MAX_RUN_READS == 200
        assert MAX_CHUNK_READS == 16

    def test_the_reader_kinds_are_the_record_vocabulary(self):
        assert READER_KINDS == ("forge", "repo-dir")

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValueError, match="kind"):
            RepoReader(Fetch(), None, kind="local")

    def test_the_default_caps_are_the_module_constants(self):
        fetch = Fetch({f"f{i}": "x" for i in range(MAX_CHUNK_READS + 1)})
        chunk = _reader(fetch).chunk_reader()
        for i in range(MAX_CHUNK_READS):
            assert chunk(f"f{i}") == "x"
        assert chunk(f"f{MAX_CHUNK_READS}") is None
        assert len(fetch.calls) == MAX_CHUNK_READS


class TestSingleFlight:
    def test_eight_threads_reading_one_path_fetch_it_once(self):
        fetch = BlockingFetch({"src/A.java": "class A {}"})
        reader = _reader(fetch)
        threads, results = _run_threads(lambda: reader.read("src/A.java"))
        try:
            assert fetch.entered.wait(WAIT)
            time.sleep(0.2)
            assert fetch.calls == ["src/A.java"]
        finally:
            fetch.release.set()
        _join(threads)
        assert fetch.calls == ["src/A.java"]
        assert results == ["class A {}"] * THREADS
        assert reader.stats()["reads"] == 1

    def test_eight_chunk_readers_on_one_in_flight_path_fetch_once_and_count_once(self):
        fetch = BlockingFetch({"src/A.java": "class A {}"})
        reader = _reader(fetch, run_cap=1, chunk_cap=1)
        threads, results = _run_threads(lambda: reader.chunk_reader()("src/A.java"))
        try:
            assert fetch.entered.wait(WAIT)
            time.sleep(0.2)
            assert fetch.calls == ["src/A.java"]
        finally:
            fetch.release.set()
        _join(threads)
        assert results == ["class A {}"] * THREADS
        assert reader.stats() == {"reads": 1, "read_cap_hit": False, "listing": None}

    def test_eight_threads_listing_call_the_lister_once(self):
        entered = threading.Event()
        release = threading.Event()
        listing = PathListing(paths=("a.py", "b.py"), complete=True)
        calls: list[int] = []

        def lister():
            calls.append(1)
            entered.set()
            release.wait(WAIT)
            return listing

        reader = _reader(lister=lister)
        threads, results = _run_threads(reader.listing)
        try:
            assert entered.wait(WAIT)
            time.sleep(0.2)
            assert len(calls) == 1
            assert reader.stats()["listing"] is None
        finally:
            release.set()
        _join(threads)
        assert len(calls) == 1
        assert results == [listing] * THREADS
        assert reader.stats()["listing"] == {"paths": 2, "complete": True}

    def test_a_raising_fetch_releases_its_waiters_with_none(self):
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def fetch(path):
            calls.append(path)
            entered.set()
            release.wait(WAIT)
            raise OSError("boom")

        reader = _reader(fetch)
        threads, results = _run_threads(lambda: reader.read("x.py"), count=3)
        assert entered.wait(WAIT)
        release.set()
        _join(threads)
        assert results == [None, None, None]
        assert calls == ["x.py"]


class TestCache:
    def test_a_missing_path_is_fetched_once(self):
        fetch = Fetch()
        reader = _reader(fetch)
        assert reader.read("missing.py") is None
        assert reader.read("missing.py") is None
        assert fetch.calls == ["missing.py"]
        assert reader.stats()["reads"] == 1

    def test_a_hit_is_fetched_once(self):
        fetch = Fetch({"a.py": "A"})
        reader = _reader(fetch)
        assert reader.read("a.py") == "A"
        assert reader.chunk_reader()("a.py") == "A"
        assert fetch.calls == ["a.py"]

    def test_a_raising_fetch_reads_as_none_logs_at_debug_and_is_cached(self, caplog):
        def fetch(path):
            raise RuntimeError("transport down")

        reader = _reader(fetch)
        with caplog.at_level(logging.DEBUG, logger="prxref"):
            assert reader.read("a.py") is None
            assert reader.read("a.py") is None
        debug = [r for r in caplog.records if "a.py" in r.getMessage()]
        assert len(debug) == 1
        assert debug[0].levelno == logging.DEBUG
        assert "transport down" in debug[0].getMessage()
        assert reader.stats()["reads"] == 1

    def test_a_non_str_result_reads_as_none(self):
        fetch = Fetch({"a.py": b"bytes", "b.py": 42})
        reader = _reader(fetch)
        assert reader.read("a.py") is None
        assert reader.chunk_reader()("b.py") is None


class TestChunkCap:
    def test_the_fourth_uncached_path_is_refused_without_a_fetch(self):
        fetch = Fetch({p: p.upper() for p in "abcdefg"})
        reader = _reader(fetch, chunk_cap=3)
        chunk = reader.chunk_reader()
        assert [chunk(p) for p in "abc"] == ["A", "B", "C"]
        assert reader.stats()["read_cap_hit"] is False
        assert chunk("d") is None
        assert fetch.calls == ["a", "b", "c"]
        assert reader.stats()["read_cap_hit"] is True

    def test_a_cached_path_still_reads_after_the_cap(self):
        fetch = Fetch({p: p.upper() for p in "abcd"})
        reader = _reader(fetch, chunk_cap=3)
        chunk = reader.chunk_reader()
        for p in "abc":
            chunk(p)
        assert chunk("d") is None
        assert chunk("a") == "A"
        assert fetch.calls == ["a", "b", "c"]

    def test_a_refused_path_is_not_cached(self):
        fetch = Fetch({p: p.upper() for p in "abcd"})
        reader = _reader(fetch, chunk_cap=3)
        chunk = reader.chunk_reader()
        for p in "abc":
            chunk(p)
        assert chunk("d") is None
        assert reader.chunk_reader()("d") == "D"
        assert fetch.calls == ["a", "b", "c", "d"]

    def test_a_second_chunk_reader_gets_its_own_budget(self):
        fetch = Fetch({p: p.upper() for p in "abcdefg"})
        reader = _reader(fetch, chunk_cap=3)
        first = reader.chunk_reader()
        for p in "abcd":
            first(p)
        second = reader.chunk_reader()
        assert [second(p) for p in "def"] == ["D", "E", "F"]
        assert second("g") is None
        assert fetch.calls == ["a", "b", "c", "d", "e", "f"]
        assert reader.stats()["reads"] == 6

    def test_each_call_returns_a_new_reader(self):
        reader = _reader()
        assert reader.chunk_reader() is not reader.chunk_reader()


class TestRunCap:
    def test_two_chunk_readers_share_the_run_cap(self):
        fetch = Fetch({p: p.upper() for p in "abcdefgh"})
        reader = _reader(fetch, run_cap=4, chunk_cap=3)
        first = reader.chunk_reader()
        second = reader.chunk_reader()
        assert [first(p) for p in "abc"] == ["A", "B", "C"]
        assert second("d") == "D"
        assert reader.stats()["read_cap_hit"] is False
        assert second("e") is None
        assert first("f") is None
        assert reader.chunk_reader()("g") is None
        assert fetch.calls == ["a", "b", "c", "d"]
        assert reader.stats() == {"reads": 4, "read_cap_hit": True, "listing": None}

    def test_a_cached_path_reads_after_the_run_cap(self):
        fetch = Fetch({p: p.upper() for p in "abcde"})
        reader = _reader(fetch, run_cap=2, chunk_cap=3)
        first = reader.chunk_reader()
        first("a")
        first("b")
        second = reader.chunk_reader()
        assert second("c") is None
        assert second("a") == "A"
        assert fetch.calls == ["a", "b"]


class TestReadIgnoresCaps:
    def test_read_fetches_past_both_caps_and_counts_in_reads(self):
        fetch = Fetch({p: p.upper() for p in "abcde"})
        reader = _reader(fetch, run_cap=1, chunk_cap=1)
        assert [reader.read(p) for p in "abcde"] == ["A", "B", "C", "D", "E"]
        assert fetch.calls == list("abcde")
        assert reader.stats() == {"reads": 5, "read_cap_hit": False, "listing": None}

    def test_read_does_not_spend_the_chunk_readers_budget(self):
        fetch = Fetch({p: p.upper() for p in "abcdef"})
        reader = _reader(fetch, run_cap=1, chunk_cap=1)
        for p in "abc":
            reader.read(p)
        chunk = reader.chunk_reader()
        assert chunk("a") == "A"
        assert chunk("d") == "D"
        assert chunk("e") is None
        assert fetch.calls == ["a", "b", "c", "d"]
        assert reader.stats() == {"reads": 4, "read_cap_hit": True, "listing": None}

    def test_read_serves_a_path_a_chunk_reader_fetched(self):
        fetch = Fetch({"a": "A"})
        reader = _reader(fetch)
        reader.chunk_reader()("a")
        assert reader.read("a") == "A"
        assert fetch.calls == ["a"]


class TestExclude:
    @staticmethod
    def _exclude(path: str) -> bool:
        return path.endswith("expected.json") or path.startswith(".env")

    def test_an_excluded_path_is_never_fetched_nor_counted(self):
        fetch = Fetch({"cases/expected.json": "{}", ".env": "SECRET=x", "a.py": "A"})
        reader = _reader(fetch, exclude=self._exclude, chunk_cap=1, run_cap=1)
        assert reader.read("cases/expected.json") is None
        chunk = reader.chunk_reader()
        assert chunk(".env") is None
        assert chunk("cases/expected.json") is None
        assert chunk("a.py") == "A"
        assert fetch.calls == ["a.py"]
        assert reader.stats() == {"reads": 1, "read_cap_hit": False, "listing": None}

    def test_excluded_paths_are_dropped_from_the_listing_and_complete_is_kept(self):
        raw = PathListing(paths=(".env", "a.py", "cases/expected.json", "src/B.java"), complete=False)
        reader = _reader(lister=Lister(raw), exclude=self._exclude)
        assert reader.listing() == PathListing(paths=("a.py", "src/B.java"), complete=False)
        assert reader.stats()["listing"] == {"paths": 2, "complete": False}

    def test_a_raising_exclude_fails_closed(self):
        def exclude(path):
            raise ValueError("bad glob")

        fetch = Fetch({"a.py": "A"})
        reader = _reader(fetch, lister=Lister(PathListing(paths=("a.py",), complete=True)), exclude=exclude)
        assert reader.read("a.py") is None
        assert reader.chunk_reader()("a.py") is None
        assert fetch.calls == []
        assert reader.listing() == PathListing(paths=(), complete=True)

    def test_without_exclude_the_listing_is_returned_as_given(self):
        raw = PathListing(paths=("a.py",), complete=True)
        assert _reader(lister=Lister(raw)).listing() is raw


class TestListing:
    def test_no_lister_gives_none(self):
        reader = _reader(lister=None)
        assert reader.listing() is None
        assert reader.stats()["listing"] is None

    def test_the_lister_runs_once_and_its_result_is_cached(self):
        lister = Lister(PathListing(paths=("a.py", "b.py", "c.py"), complete=True))
        reader = _reader(lister=lister)
        assert reader.stats()["listing"] is None
        first = reader.listing()
        assert reader.listing() is first
        assert lister.calls == 1
        assert reader.stats()["listing"] == {"paths": 3, "complete": True}

    def test_a_raising_lister_gives_none_and_is_not_retried(self, caplog):
        lister = Lister(RuntimeError("tree API down"))
        reader = _reader(lister=lister)
        with caplog.at_level(logging.DEBUG, logger="prxref"):
            assert reader.listing() is None
            assert reader.listing() is None
        assert lister.calls == 1
        assert reader.stats()["listing"] is None
        assert any("tree API down" in r.getMessage() and r.levelno == logging.DEBUG for r in caplog.records)

    def test_a_none_listing_is_cached(self):
        lister = Lister(None)
        reader = _reader(lister=lister)
        assert reader.listing() is None
        assert reader.listing() is None
        assert lister.calls == 1
        assert reader.stats()["listing"] is None

    @pytest.mark.parametrize("bad", [("a.py",), ["a.py"], {"paths": ("a.py",), "complete": True}, "a.py"])
    def test_a_non_path_listing_gives_none(self, bad):
        lister = Lister(bad)
        reader = _reader(lister=lister)
        assert reader.listing() is None
        assert reader.listing() is None
        assert lister.calls == 1
        assert reader.stats()["listing"] is None

    def test_stats_never_calls_the_lister_or_fetch(self):
        fetch = Fetch()
        lister = Lister(PathListing(paths=(), complete=True))
        reader = _reader(fetch, lister=lister)
        reader.stats()
        assert lister.calls == 0
        assert fetch.calls == []

    def test_stats_is_a_snapshot(self):
        reader = _reader(Fetch({"a": "A"}))
        before = reader.stats()
        reader.read("a")
        assert before["reads"] == 0
        assert reader.stats()["reads"] == 1


class FakeForge:
    """A forge fake with the optional reader and listing, every call recorded with its ref and sha."""

    name = "fake"

    def __init__(self, files: dict[str, str] | None = None, listing: object = None):
        self.files = files or {}
        self.listing = listing
        self.reads: list[tuple[object, str, str]] = []
        self.lists: list[tuple[object, str]] = []

    def get_file_content(self, ref, path, *, sha):
        self.reads.append((ref, path, sha))
        return self.files.get(path)

    def list_paths(self, ref, *, sha):
        self.lists.append((ref, sha))
        return self.listing


class ReadOnlyForge:
    """A forge fake with ``get_file_content`` and no ``list_paths``."""

    name = "read-only"

    def __init__(self, files: dict[str, str] | None = None):
        self.files = files or {}
        self.reads: list[tuple[object, str, str]] = []

    def get_file_content(self, ref, path, *, sha):
        self.reads.append((ref, path, sha))
        return self.files.get(path)


class NoReaderForge:
    """A forge fake with neither optional method."""

    name = "no-reader"


class TestForgeReader:
    def test_no_get_file_content_gives_none(self):
        assert forge_reader(NoReaderForge(), REF, SHA) is None

    @pytest.mark.parametrize("sha", ["", None])
    def test_an_empty_sha_gives_none(self, sha):
        forge = FakeForge({"a.py": "A"})
        assert forge_reader(forge, REF, sha) is None
        assert forge.reads == []

    def test_ref_and_sha_pass_through_to_both_calls(self):
        listing = PathListing(paths=("a.py",), complete=True)
        forge = FakeForge({"a.py": "A"}, listing)
        reader = forge_reader(forge, REF, SHA)
        assert reader is not None
        assert reader.kind == "forge"
        assert reader.read("a.py") == "A"
        assert reader.chunk_reader()("b.py") is None
        assert reader.listing() is listing
        assert forge.reads == [(REF, "a.py", SHA), (REF, "b.py", SHA)]
        assert forge.lists == [(REF, SHA)]
        assert reader.stats() == {"reads": 2, "read_cap_hit": False, "listing": {"paths": 1, "complete": True}}

    def test_no_list_paths_gives_no_listing(self):
        reader = forge_reader(ReadOnlyForge({"a.py": "A"}), REF, SHA)
        assert reader is not None
        assert reader.read("a.py") == "A"
        assert reader.listing() is None
        assert reader.stats()["listing"] is None

    def test_a_raising_get_file_content_reads_as_none_at_debug(self, caplog):
        class Raising(ReadOnlyForge):
            def get_file_content(self, ref, path, *, sha):
                raise ConnectionError("reset by peer")

        reader = forge_reader(Raising(), REF, SHA)
        with caplog.at_level(logging.DEBUG, logger="prxref"):
            assert reader.read("a.py") is None
        assert [r.levelno for r in caplog.records if "reset by peer" in r.getMessage()] == [logging.DEBUG]

    def test_exclude_is_passed_through(self):
        forge = FakeForge({"a.py": "A", "k.pem": "KEY"}, PathListing(paths=("a.py", "k.pem"), complete=True))
        reader = forge_reader(forge, REF, SHA, exclude=lambda p: p.endswith(".pem"))
        assert reader.read("k.pem") is None
        assert reader.listing() == PathListing(paths=("a.py",), complete=True)
        assert forge.reads == []

    def test_a_replay_forge_reads_and_lists_through_its_inner_forge(self):
        listing = PathListing(paths=("src/A.java", "src/B.java"), complete=False)
        inner = FakeForge({"src/A.java": "class A {}"}, listing)
        reader = forge_reader(ReplayForge(inner, hide_threads=True), REF, SHA)
        assert reader is not None
        assert reader.read("src/A.java") == "class A {}"
        assert reader.chunk_reader()("src/B.java") is None
        assert reader.listing() == listing
        assert inner.reads == [(REF, "src/A.java", SHA), (REF, "src/B.java", SHA)]
        assert inner.lists == [(REF, SHA)]
        assert reader.stats() == {"reads": 2, "read_cap_hit": False, "listing": {"paths": 2, "complete": False}}

    def test_a_replay_forge_over_an_inner_without_list_paths_lists_none_once(self):
        replay = ReplayForge(ReadOnlyForge({"a.py": "A"}))
        reader = forge_reader(replay, REF, SHA)
        assert reader is not None
        assert reader.read("a.py") == "A"
        assert reader.listing() is None
        assert reader.stats()["listing"] is None


class TestRepoDirReader:
    @staticmethod
    def _tree(tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "A.java").write_text("class A {}\n")
        (tmp_path / "src" / "B.java").write_text("class B {}\n")
        (tmp_path / "cases").mkdir()
        (tmp_path / "cases" / "expected.json").write_text("{}\n")
        return RepoDir(tmp_path)

    def test_it_reads_lists_and_is_kind_repo_dir(self, tmp_path):
        reader = repo_dir_reader(self._tree(tmp_path))
        assert reader.kind == "repo-dir"
        assert reader.read("src/A.java") == "class A {}\n"
        assert reader.read("src/missing.java") is None
        assert reader.listing() == PathListing(
            paths=("cases/expected.json", "src/A.java", "src/B.java"), complete=True
        )
        assert reader.stats() == {"reads": 2, "read_cap_hit": False, "listing": {"paths": 3, "complete": True}}

    def test_exclude_applies(self, tmp_path):
        reader = repo_dir_reader(self._tree(tmp_path), exclude=lambda p: p.endswith("expected.json"))
        assert reader.read("cases/expected.json") is None
        assert reader.listing() == PathListing(paths=("src/A.java", "src/B.java"), complete=True)
        assert reader.stats()["reads"] == 0

    def test_the_default_chunk_cap_applies(self, tmp_path):
        for i in range(MAX_CHUNK_READS + 1):
            (tmp_path / f"f{i:02d}.txt").write_text(f"{i}\n")
        reader = repo_dir_reader(RepoDir(tmp_path))
        chunk = reader.chunk_reader()
        assert [chunk(f"f{i:02d}.txt") for i in range(MAX_CHUNK_READS)] == [f"{i}\n" for i in range(MAX_CHUNK_READS)]
        assert chunk(f"f{MAX_CHUNK_READS:02d}.txt") is None
        assert reader.stats() == {"reads": MAX_CHUNK_READS, "read_cap_hit": True, "listing": None}

    def test_an_incomplete_walk_stays_incomplete(self, tmp_path, monkeypatch):
        from prxref.forges import repo_dir as repo_dir_module

        monkeypatch.setattr(repo_dir_module, "_MAX_LISTED_FILES", 1)
        listing = repo_dir_reader(self._tree(tmp_path)).listing()
        assert listing is not None
        assert listing.complete is False
        assert len(listing.paths) == 1
