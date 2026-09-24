"""Local filesystem reader that stands in for a PR head via ``--repo-dir``.

``RepoDir`` gives a review or an eval case repository context read from a
local working tree, with no network access. ``read`` matches the shape of
the forge adapters' ``get_file_content`` contract (see
``forges/github.py``); ``list_files`` gives a bounded, confined directory
listing. The CLI flag and its wiring into the review pipeline belong to a
later seat; this module only reads a confined filesystem tree.
"""

from __future__ import annotations

import os
import stat
from pathlib import PurePosixPath

_MAX_FILE_CONTENT_BYTES = 512 * 1024
_MAX_LISTED_FILES = 100_000


class RepoDir:
    """Reads a confined local directory tree in place of a forge working copy."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        """Resolve ``root`` with ``os.path.realpath``.

        Raises ``ValueError`` naming ``root`` when it is not an existing
        directory.
        """
        resolved = os.path.realpath(root)
        if not os.path.isdir(resolved):
            raise ValueError(f"repo-dir root is not an existing directory: {root}")
        self.root = resolved

    def _confined_real_path(self, path: str) -> str | None:
        """Return the confined, symlink-resolved filesystem path for ``path``.

        Returns None for an unsafe ``path`` (absolute, a ``..`` segment, a
        backslash, a NUL byte, or empty), for one whose resolved real path
        leaves the root, and for one that lands inside ``.git`` at any
        depth. The confinement check compares resolved real paths, never
        string prefixes.
        """
        if not path or "\x00" in path or "\\" in path or os.path.isabs(path):
            return None
        if ".." in PurePosixPath(path).parts:
            return None
        candidate = os.path.join(self.root, path)
        real = os.path.realpath(candidate)
        root_prefix = self.root + os.sep
        if real != self.root and not real.startswith(root_prefix):
            return None
        rel_parts = PurePosixPath(os.path.relpath(real, self.root)).parts
        if ".git" in rel_parts:
            return None
        return real

    def read(self, path: str) -> str | None:
        """Return the utf-8 text of ``path`` under the root, or None.

        Matches the forge adapters' ``get_file_content`` contract: never
        raises, and returns None when the path is unsafe, missing, a
        directory, escapes the root (directly or via a symlink), falls
        inside ``.git``, is over 512 KiB, or holds a NUL byte. The size is
        checked from a stat before the file is read. Otherwise the content
        is decoded as utf-8 with ``errors="replace"``.
        """
        real = self._confined_real_path(path)
        if real is None:
            return None
        try:
            st = os.stat(real)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        if st.st_size > _MAX_FILE_CONTENT_BYTES:
            return None
        try:
            with open(real, "rb") as fh:
                content = fh.read()
        except OSError:
            return None
        if b"\x00" in content:
            return None
        return content.decode("utf-8", errors="replace")

    def list_files(self) -> tuple[tuple[str, ...], bool]:
        """Return sorted repo-relative POSIX file paths, and whether the walk was complete.

        The walk never follows a symlinked directory, skips any ``.git``
        directory at any depth, and omits symlinks whose target escapes the
        root. It stops past ``_MAX_LISTED_FILES`` files and reports
        ``complete=False`` only when a file beyond the cap was actually
        dropped; a tree with exactly the cap's worth of files is complete.
        """
        found: list[str] = []
        root_prefix = self.root + os.sep
        complete = True
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d != ".git")
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    real = os.path.realpath(full)
                    if real != self.root and not real.startswith(root_prefix):
                        continue
                if not os.path.isfile(full):
                    continue
                rel = PurePosixPath(os.path.relpath(full, self.root))
                if ".git" in rel.parts:
                    continue
                if len(found) < _MAX_LISTED_FILES:
                    found.append(rel.as_posix())
                else:
                    complete = False
                    break
            if not complete:
                break
        found.sort()
        return tuple(found), complete
