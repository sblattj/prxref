"""The run-scoped repository reader behind repository context (#17).

One ``RepoReader`` serves every file read and the one path listing a review
run makes when repository context is on. It sits in front of a forge's
optional ``get_file_content`` and ``list_paths`` (``forge_reader``) or a
local ``--repo-dir`` tree (``repo_dir_reader``) and gives the chunk workers,
which run in parallel, one shared view of the repository:

- every path is fetched at most once per run, single-flight across threads,
  and a miss is cached like a hit;
- the listing is fetched at most once per run, single-flight as well;
- each chunk's reader is capped at ``MAX_CHUNK_READS`` uncached reads and all
  chunk readers together at ``MAX_RUN_READS``, so repository context bounds
  both its network cost and its prompt growth;
- an excluded path is never read and never listed, at the one boundary every
  read crosses;
- ``stats`` reports the counters the run record carries.

The module does no prompt work and knows nothing about which paths are
worth reading; the caller decides that and passes plain callables in.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from prxref.forges.base import Forge, PathListing, PRRef
from prxref.forges.repo_dir import RepoDir

logger = logging.getLogger(__name__)

MAX_RUN_READS = 200
MAX_CHUNK_READS = 16
READER_KINDS = ("forge", "repo-dir")


class _Slot:
    """One single-flight result: ``done`` is set once ``value`` is final."""

    __slots__ = ("done", "value")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: str | PathListing | None = None


class RepoReader:
    """A cached, single-flight, capped view of one repository for one review run.

    ``fetch(path)`` returns a file's text or None; ``lister()`` returns a
    ``PathListing`` or None, and ``lister=None`` means the source cannot list.
    Either callable may raise or return the wrong type: an exception is
    logged at DEBUG and read as None, and a ``fetch`` result that is not a
    ``str`` (or a ``lister`` result that is not a ``PathListing``) is read as
    None. ``kind`` is ``"forge"`` or ``"repo-dir"``, the run record's
    ``reader`` field. ``exclude(path)`` returning true (or raising) makes a
    path unreadable and unlisted.

    The object is thread-safe and meant to be shared by every chunk worker
    of a run. ``read`` is cached and single-flight but not capped: its
    fetches count in ``stats()["reads"]`` and never spend either cap.
    ``chunk_reader`` hands each chunk a reader that also enforces
    ``chunk_cap`` for that chunk and ``run_cap`` across all chunk readers.
    A path already cached or in flight is served without counting against
    either cap, whichever reader fetched it.

    Determinism: below the caps, what every read returns is independent of
    the order in which parallel workers ask, because each path is fetched
    once and every caller sees that one result. Once the run cap is reached,
    WHICH chunk meets it first depends on thread scheduling; that is
    accepted only because ``stats()["read_cap_hit"]`` records that it
    happened.
    """

    def __init__(
        self,
        fetch: Callable[[str], str | None],
        lister: Callable[[], PathListing | None] | None,
        *,
        kind: str,
        exclude: Callable[[str], bool] | None = None,
        run_cap: int = MAX_RUN_READS,
        chunk_cap: int = MAX_CHUNK_READS,
    ) -> None:
        """Wrap ``fetch`` and ``lister``; raises ``ValueError`` for a ``kind`` outside ``READER_KINDS``."""
        if kind not in READER_KINDS:
            raise ValueError(f"RepoReader kind must be one of {READER_KINDS}, got {kind!r}")
        self.kind = kind
        self._fetch = fetch
        self._lister = lister
        self._exclude = exclude
        self._run_cap = run_cap
        self._chunk_cap = chunk_cap
        self._lock = threading.Lock()
        self._slots: dict[str, _Slot] = {}
        self._reads = 0
        self._run_used = 0
        self._read_cap_hit = False
        self._listing_slot: _Slot | None = None

    def _excluded(self, path: str) -> bool:
        if self._exclude is None:
            return False
        try:
            return bool(self._exclude(path))
        except Exception as e:  # noqa: BLE001 - an exclude that cannot answer fails closed
            logger.debug("repository context exclude(%s) failed: %s", path, e)
            return True

    def _fetch_value(self, path: str) -> str | None:
        try:
            value = self._fetch(path)
        except Exception as e:  # noqa: BLE001 - context is never worth a failed review
            logger.debug("%s read of %s failed: %s", self.kind, path, e)
            return None
        return value if isinstance(value, str) else None

    def _get(self, path: str, admit: Callable[[], bool] | None) -> str | None:
        if self._excluded(path):
            return None
        with self._lock:
            slot = self._slots.get(path)
            owner = slot is None
            if owner:
                if admit is not None and not admit():
                    self._read_cap_hit = True
                    return None
                slot = _Slot()
                self._slots[path] = slot
                self._reads += 1
        if owner:
            try:
                slot.value = self._fetch_value(path)
            finally:
                slot.done.set()
        else:
            slot.done.wait()
        value = slot.value
        return value if isinstance(value, str) else None

    def read(self, path: str) -> str | None:
        """Return the text of ``path``, or None; cached and single-flight, never capped.

        The first caller for an uncached path fetches it; a concurrent caller
        for the same path waits for that one fetch and gets its result. A
        None result is cached too, so a miss costs one fetch per run. An
        excluded path returns None without fetching or counting.
        """
        return self._get(path, None)

    def chunk_reader(self) -> Callable[[str], str | None]:
        """Return a new ``read(path) -> str | None`` for one chunk, enforcing both caps.

        A path already cached or in flight is served as ``read`` serves it,
        without counting. An uncached path counts one against this chunk's
        ``chunk_cap`` and one against the run's ``run_cap``, which every
        chunk reader of this ``RepoReader`` shares. When either cap is
        already reached, it returns None without fetching, leaves the path
        uncached, and marks ``read_cap_hit``. An excluded path returns None
        without fetching or counting.
        """
        used = 0

        def admit() -> bool:
            nonlocal used
            if used >= self._chunk_cap or self._run_used >= self._run_cap:
                return False
            used += 1
            self._run_used += 1
            return True

        def read(path: str) -> str | None:
            return self._get(path, admit)

        return read

    def _list_value(self) -> PathListing | None:
        try:
            listing = self._lister()
        except Exception as e:  # noqa: BLE001 - context is never worth a failed review
            logger.debug("%s path listing failed: %s", self.kind, e)
            return None
        if not isinstance(listing, PathListing):
            return None
        if self._exclude is None:
            return listing
        kept = tuple(path for path in listing.paths if not self._excluded(path))
        return PathListing(paths=kept, complete=listing.complete)

    def listing(self) -> PathListing | None:
        """Return the repository's path listing, or None; the lister runs at most once per run.

        Concurrent callers share one call to ``lister``, and its result is
        cached, including None, a raise read as None, and a result that is
        not a ``PathListing`` read as None. Excluded paths are filtered out,
        and ``complete`` stays as the source reported it. With
        ``lister=None`` this returns None and calls nothing.
        """
        if self._lister is None:
            return None
        with self._lock:
            slot = self._listing_slot
            owner = slot is None
            if owner:
                slot = self._listing_slot = _Slot()
        if owner:
            try:
                slot.value = self._list_value()
            finally:
                slot.done.set()
        else:
            slot.done.wait()
        value = slot.value
        return value if isinstance(value, PathListing) else None

    def stats(self) -> dict:
        """Return a snapshot of the run's counters for the run record.

        ``{"reads": int, "read_cap_hit": bool, "listing": {"paths": int,
        "complete": bool} | None}``. ``reads`` counts every call that reached
        ``fetch``, from ``read`` and chunk readers alike. ``read_cap_hit`` is
        true once any chunk reader refused a path at a cap. ``listing`` is
        None until ``listing()`` has finished, and when it gave None; its
        ``paths`` counts the listing after exclusion. Taking a snapshot never
        calls ``fetch`` or ``lister``.
        """
        with self._lock:
            reads = self._reads
            cap_hit = self._read_cap_hit
            slot = self._listing_slot
        listing = None
        if slot is not None and slot.done.is_set() and isinstance(slot.value, PathListing):
            listing = {"paths": len(slot.value.paths), "complete": slot.value.complete}
        return {"reads": reads, "read_cap_hit": cap_hit, "listing": listing}


def forge_reader(
    forge: Forge,
    ref: PRRef,
    sha: str | None,
    *,
    exclude: Callable[[str], bool] | None = None,
) -> RepoReader | None:
    """Return a ``RepoReader`` over a forge at commit ``sha``, or None.

    None when the forge has no ``get_file_content`` or ``sha`` is empty,
    exactly as the orchestrator's same-file reader decides. Reads call
    ``get_file_content(ref, path, sha=sha)``. The listing calls
    ``list_paths(ref, sha=sha)`` when the forge has that method; otherwise
    the reader has no lister and ``listing()`` is None. ``kind`` is
    ``"forge"``.
    """
    getter = getattr(forge, "get_file_content", None)
    if getter is None or not sha:
        return None
    list_paths = getattr(forge, "list_paths", None)

    def fetch(path: str) -> str | None:
        return getter(ref, path, sha=sha)

    def list_at_sha() -> PathListing | None:
        return list_paths(ref, sha=sha)

    lister = None if list_paths is None else list_at_sha
    return RepoReader(fetch, lister, kind="forge", exclude=exclude)


def repo_dir_reader(
    repo_dir: RepoDir,
    *,
    exclude: Callable[[str], bool] | None = None,
) -> RepoReader:
    """Return a ``RepoReader`` over a local ``RepoDir`` tree.

    Reads call ``repo_dir.read``; the listing wraps ``repo_dir.list_files()``
    into a ``PathListing``. Both caps apply as they do to a forge, because
    they bound prompt growth as well as network cost. ``kind`` is
    ``"repo-dir"``.
    """

    def lister() -> PathListing:
        paths, complete = repo_dir.list_files()
        return PathListing(paths=tuple(paths), complete=complete)

    return RepoReader(repo_dir.read, lister, kind="repo-dir", exclude=exclude)
