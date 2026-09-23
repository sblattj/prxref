"""Bounded, fingerprinted reads of operator-named text files.

Team review rules, ticket context and local spec sources all put text that an
operator named by path into a prompt, and all three need the same four
guarantees, so they share this loader:

- **Bounded memory.** :func:`read_capped_file` streams the file. It keeps at
  most ``max_chars`` decoded characters and reads the rest in fixed-size
  chunks only to hash, count and validate it, so a huge file or a mistyped
  path to one costs a scan, not the file's size in memory.
- **A stable fingerprint.** ``sha256`` covers the raw file bytes, before
  decoding and capping. It equals ``shasum -a 256 FILE``, it does not change
  when the cap changes, and it changes when any byte does.
- **Strict text.** UTF-8 only, a leading BOM dropped, CRLF and lone CR folded
  to LF. Invalid UTF-8 anywhere in the file is an error, even past the cap.
- **No symlink escape out of the working directory.** A path that sits under
  the working directory may be inside a PR checkout, where the PR author
  controls every file and every symlink. Such a path must still resolve
  under the working directory once its symlinks are followed, so a committed
  ``docs/SPEC.md -> ~/.ssh/id_rsa`` is refused. An absolute path outside the
  working directory (``$RUNNER_TEMP/rules.md``, a CI file variable) is the
  operator's own choice and is read as given.

Only regular files are read: a directory, FIFO or device is refused before
anything is opened, so a named pipe can never block a review.

The module raises plain exceptions and never a ``ConfigError``:
``FileNotFoundError``, ``IsADirectoryError``, ``PermissionError`` (including
a confinement refusal), ``OSError`` (not a regular file), ``UnicodeDecodeError``
and ``ValueError`` (a cap below 1). Each ``OSError`` carries ``strerror`` and
``filename`` separately, so a caller can report the reason without the path.
The caller decides what a failure means: a spec source records it as that
source's error, while rules and ticket context turn it into a ``ConfigError``
naming the env var or flag. Truncation markers are also the caller's; the
kept text never contains one.
"""
from __future__ import annotations

import codecs
import errno
import hashlib
import io
import os
import stat
from dataclasses import dataclass

_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class CappedText:
    """Operator-supplied text, capped for a prompt and fingerprinted for the run record.

    ``text`` is exactly the first ``max_chars`` characters of the decoded,
    newline-normalised text, with no marker. ``chars`` is the length of the
    whole text the cap was applied to, so ``truncated`` is ``chars >
    max_chars`` (a text exactly ``max_chars`` long is not truncated).
    ``sha256`` is the lowercase hex digest of the source: the raw bytes for a
    file.
    """

    text: str
    sha256: str
    chars: int
    truncated: bool
    max_chars: int

    def record(self) -> dict[str, object]:
        """Return the run-record fingerprint: ``sha256``, ``chars``,
        ``max_chars`` and ``truncated``, JSON-native values only and never the
        text itself.
        """
        return {
            "sha256": self.sha256,
            "chars": self.chars,
            "max_chars": self.max_chars,
            "truncated": self.truncated,
        }


def confine_to_cwd(path: str | os.PathLike[str]) -> str:
    """Return the resolved path, refusing one that symlinks out of the cwd.

    A path counts as inside the working directory when its absolute form is
    lexically under the cwd (as ``os.getcwd()`` spells it, or as its
    ``realpath``), or when one of its parent directories resolves under the
    cwd: the cwd reached through another spelling, such as ``/tmp/x`` for
    ``/private/tmp/x``, or through a symlink into it. Such a path must
    resolve, all symlinks followed, to a location under the cwd's
    ``realpath``; otherwise ``PermissionError`` is raised. A path outside the
    cwd is returned resolved and unchecked. ``..`` segments are folded
    lexically first, so a path the operator writes to climb out of the cwd
    counts as outside. Works for files and directories alike, and does not
    require the path to exist. Comparisons use ``os.path.commonpath``, never
    string prefixes.
    """
    raw = os.fspath(path)
    cwd = os.getcwd()
    real_cwd = os.path.realpath(cwd)
    resolved = os.path.realpath(raw)
    if _is_within(resolved, real_cwd):
        return resolved
    if _lexically_inside(os.path.abspath(raw), (os.path.abspath(cwd), real_cwd), real_cwd):
        raise PermissionError(errno.EACCES, "resolves outside the working directory", raw)
    return resolved


def check_readable_path(path: str | os.PathLike[str], *, confine: bool = True) -> str:
    """Return the resolved path of a regular file that is safe to open.

    With ``confine`` (the default) the path first goes through
    :func:`confine_to_cwd`. Then ``os.stat`` must report a regular file, so a
    directory raises ``IsADirectoryError`` and a FIFO or device raises
    ``OSError`` before anything is opened. A missing file raises
    ``FileNotFoundError`` naming the path as given.
    """
    raw = os.fspath(path)
    resolved = confine_to_cwd(raw) if confine else os.path.realpath(raw)
    _require_regular_file(os.stat(raw), raw)
    return resolved


def decode_text(raw: bytes) -> str:
    """Decode strict UTF-8, drop a leading BOM, and fold CRLF and lone CR to LF.

    Raises ``UnicodeDecodeError`` (a ``ValueError``) on invalid UTF-8. The
    result is identical to what :func:`read_capped_file` decodes from the same
    bytes.
    """
    return raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")


def cap_text(text: str, max_chars: int, *, sha256: str | None = None) -> CappedText:
    """Cap ``text`` at ``max_chars`` characters.

    ``sha256`` is the fingerprint to record; it defaults to the digest of the
    whole ``text`` encoded as UTF-8, taken before the cap. A caller that read
    the text from a file passes the digest of the raw file bytes instead.
    ``max_chars`` below 1 raises ``ValueError``.
    """
    _require_cap(max_chars)
    if sha256 is None:
        sha256 = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
    chars = len(text)
    return CappedText(text[:max_chars], sha256, chars, chars > max_chars, max_chars)


def read_capped_file(
    path: str | os.PathLike[str], max_chars: int, *, confine: bool = True
) -> CappedText:
    """Stream a text file into a :class:`CappedText` in bounded memory.

    The path is checked with :func:`check_readable_path` first (``confine``
    is passed through). The file is read once in 64 KiB chunks: every byte is
    hashed and decoded as in :func:`decode_text`, the first ``max_chars``
    characters are kept, and the rest are only counted. So the whole file is
    validated, ``sha256`` matches ``shasum -a 256``, and ``chars`` counts the
    full text even when it was truncated. ``max_chars`` below 1 raises
    ``ValueError`` before the filesystem is touched.
    """
    _require_cap(max_chars)
    raw_path = os.fspath(path)
    resolved = check_readable_path(raw_path, confine=confine)
    digest = hashlib.sha256()
    decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8-sig")("strict"), True)
    kept: list[str] = []
    kept_chars = 0
    total_chars = 0
    with open(resolved, "rb") as fh:
        _require_regular_file(os.fstat(fh.fileno()), raw_path)
        while True:
            chunk = fh.read(_READ_CHUNK_BYTES)
            final = not chunk
            digest.update(chunk)
            piece = decoder.decode(chunk, final=final)
            total_chars += len(piece)
            if piece and kept_chars < max_chars:
                take = piece[: max_chars - kept_chars]
                kept.append(take)
                kept_chars += len(take)
            if final:
                break
    return CappedText("".join(kept), digest.hexdigest(), total_chars, total_chars > max_chars, max_chars)


def _require_cap(max_chars: int) -> None:
    if max_chars < 1:
        raise ValueError(f"max_chars must be at least 1, got {max_chars!r}")


def _require_regular_file(st: os.stat_result, raw: str) -> None:
    if stat.S_ISREG(st.st_mode):
        return
    if stat.S_ISDIR(st.st_mode):
        raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), raw)
    raise OSError(errno.EINVAL, "not a regular file", raw)


def _is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _lexically_inside(lexical: str, roots: tuple[str, ...], real_cwd: str) -> bool:
    if any(_is_within(lexical, root) for root in roots):
        return True
    current = os.path.dirname(lexical)
    while True:
        if _is_within(os.path.realpath(current), real_cwd):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent
