"""Operator prompt-template overrides: load, validate and fingerprint a prompts directory.

``PRXREF_PROMPTS_DIR`` (or ``--prompts-dir DIR``) names a directory that may
hold any of ``worker.md``, ``systemic.md`` and ``summary.md``. Each file found
there replaces the packaged template of the same name for the whole run; a
file that is absent falls back to the packaged copy, read from
``prxref/prompts`` exactly as :func:`prxref.reviewer.load_prompt` reads it.
The judge prompt is not overridable, and no other name is read.

The directory is loaded once, before any network call, and every failure is a
:class:`~prxref.llm.ConfigError` whose message starts with the ``source`` that
supplied the path, so the CLI exits 2 naming the env var or flag. Validation
is mandatory, not advisory: a review template without the
:data:`CONTEXT_MARKER` line raises inside every review unit's render, which
the orchestrator records as a crashed chunk, so an unvalidated template would
silently zero the review and still exit 0.

What an override must contain is derived from the packaged templates at load,
never from a hand-kept list, so a slot added to a packaged template becomes
required of every override automatically:

- ``worker.md`` and ``systemic.md`` must contain :data:`CONTEXT_MARKER` and,
  after it, every placeholder the packaged template has after it, except the
  feature slots in :data:`OPTIONAL_PLACEHOLDERS`. Only the text after the
  marker is filled, so a placeholder above it does not count.
- ``summary.md`` must contain ``{findings}``; the other summary slots may be
  dropped.

Refused outright: a URL instead of a local path, a path that does not exist
or is not a directory, a directory that symlinks out of the working
directory, a template that symlinks out of the prompts directory, a template
that is not a regular file, one over :data:`MAX_TEMPLATE_BYTES` (never
truncated, because a cut template loses its placeholders), invalid UTF-8 and
NUL bytes. Logged as a WARNING and loaded anyway: a placeholder that is not a
slot of that template (a likely typo, which renders literally), a known
placeholder above the marker, a second marker, an unrecognised file in the
directory (dotfiles are skipped), and a directory holding none of the three
templates.

Whoever controls the directory controls the reviewer's whole policy, so the
path must come from the operator's configuration and never from the pull
request under review; a PR that commits a prompts directory into its own
checkout rewrites its own review. This loader reads only a configured local
path, never through a forge.
"""
from __future__ import annotations

import codecs
import errno
import hashlib
import logging
import os
import re
import stat
from dataclasses import dataclass
from importlib import resources

from .llm import ConfigError
from .reviewer import _CONTEXT_MARKER
from .text_inputs import check_readable_path, confine_to_cwd, decode_text

logger = logging.getLogger(__name__)

CONTEXT_MARKER = _CONTEXT_MARKER
TEMPLATE_NAMES: tuple[str, ...] = ("worker", "systemic", "summary")
REVIEW_TEMPLATES: frozenset[str] = frozenset({"worker", "systemic"})
OPTIONAL_PLACEHOLDERS: frozenset[str] = frozenset({"scope_example", "rule_example"})
SUMMARY_REQUIRED: frozenset[str] = frozenset({"findings"})
MAX_TEMPLATE_BYTES = 256 * 1024
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_]\w*)\}")

_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


@dataclass(frozen=True)
class TemplateFile:
    """One override read from the prompts directory.

    ``name`` is the template (``"worker"``, ``"systemic"`` or ``"summary"``);
    ``path`` is the file's path as configured (the directory joined with the
    file name, not resolved); ``text`` is the decoded text (a BOM dropped,
    CRLF and CR folded to LF); ``sha256`` is the lowercase hex digest of the
    raw file bytes, so it equals ``shasum -a 256 FILE``; ``chars`` is
    ``len(text)``.
    """

    name: str
    path: str
    text: str
    sha256: str
    chars: int

    def record(self) -> dict[str, object]:
        """Return the run-record fingerprint ``{path, sha256, chars}``, never the text."""
        return {"path": self.path, "sha256": self.sha256, "chars": self.chars}


@dataclass(frozen=True)
class PromptTemplates:
    """The effective templates of one run: overrides where present, packaged text elsewhere.

    ``worker``, ``systemic`` and ``summary`` hold the text every render of
    that template uses. ``dir`` is the directory as configured. ``overrides``
    holds one :class:`TemplateFile` per overridden template, in
    :data:`TEMPLATE_NAMES` order, and is empty when the directory held none.
    """

    dir: str
    worker: str
    systemic: str
    summary: str
    overrides: tuple[TemplateFile, ...] = ()

    @property
    def overridden(self) -> tuple[str, ...]:
        """The names of the overridden templates, in :data:`TEMPLATE_NAMES` order."""
        return tuple(f.name for f in self.overrides)

    def text(self, name: str) -> str:
        """The effective text of template ``name``; any other name raises ``ValueError``."""
        _require_name(name)
        return getattr(self, name)

    def override(self, name: str) -> str:
        """The override text of template ``name``, or ``""`` when it is packaged.

        ``""`` is the renderers' "use the packaged template" value, so a caller
        can pass this straight through and leave a non-overridden template on
        the packaged path. Any other name raises ``ValueError``.
        """
        _require_name(name)
        for f in self.overrides:
            if f.name == name:
                return f.text
        return ""

    def record(self) -> dict[str, object]:
        """Return the run-record view, JSON-native values only and never template text.

        ``{"dir": <dir as configured>, "templates": {<name>: {"path",
        "sha256", "chars"}}}``, with one entry per overridden template in
        :data:`TEMPLATE_NAMES` order; a template left packaged has no entry.
        """
        return {"dir": self.dir, "templates": {f.name: f.record() for f in self.overrides}}


def placeholders(text: str) -> frozenset[str]:
    """Every ``{identifier}`` placeholder name in ``text``.

    Uses the brace-identifier shape :func:`prxref.reviewer.fill_template`
    fills, so JSON braces and ``{ spaced }`` text are not placeholders.
    """
    return frozenset(PLACEHOLDER_RE.findall(text))


def required_placeholders(name: str) -> frozenset[str]:
    """The placeholders an override of template ``name`` must contain.

    For ``worker`` and ``systemic``: every placeholder after
    :data:`CONTEXT_MARKER` in the packaged template, minus
    :data:`OPTIONAL_PLACEHOLDERS`, computed from the packaged file on every
    call. For ``summary``: :data:`SUMMARY_REQUIRED`. Any other name raises
    ``ValueError``.
    """
    _require_name(name)
    if name not in REVIEW_TEMPLATES:
        return SUMMARY_REQUIRED
    _, _, tail = _packaged_text(name).partition(CONTEXT_MARKER)
    return placeholders(tail) - OPTIONAL_PLACEHOLDERS


def load_prompt_templates(path: str | os.PathLike[str] | None, *, source: str) -> PromptTemplates | None:
    """Load and validate the prompt-template overrides in directory ``path``.

    ``None``, an empty or a whitespace-only path means "no overrides" and
    returns ``None``. ``source`` names the input that supplied the path
    (``PRXREF_PROMPTS_DIR`` or ``--prompts-dir``); every failure listed in
    the module docstring is a :class:`~prxref.llm.ConfigError` whose message
    starts with it, and every warning names it too. The directory is read
    once: only the exact file names ``worker.md``, ``systemic.md`` and
    ``summary.md`` are loaded, and a template not present falls back to its
    packaged text. Makes no network call.
    """
    if path is None:
        return None
    raw = os.fspath(path)
    if not raw.strip():
        return None
    if _URL_RE.match(raw.strip()):
        raise ConfigError(f"{source}: prompts directory must be a local path, not a URL: {raw!r}")
    root = _open_root(raw, source)
    try:
        entries = set(os.listdir(raw))
    except OSError as exc:
        raise ConfigError(f"{source}: cannot list prompts directory {raw!r}: {_reason(exc)}") from exc
    files = [f"{name}.md" for name in TEMPLATE_NAMES]
    unknown = sorted(e for e in entries if not e.startswith(".") and e not in files)
    if unknown:
        logger.warning(
            "%s: prompts directory %r: ignoring unrecognised %s; only %s are read",
            source, raw, ", ".join(unknown), ", ".join(files),
        )
    overrides = tuple(
        _load_one(name, os.path.join(raw, f"{name}.md"), root, source)
        for name in TEMPLATE_NAMES
        if f"{name}.md" in entries
    )
    if not overrides:
        logger.warning(
            "%s: prompts directory %r holds none of %s; the packaged templates are used",
            source, raw, ", ".join(files),
        )
    effective = {name: _packaged_text(name) for name in TEMPLATE_NAMES}
    effective.update({f.name: f.text for f in overrides})
    return PromptTemplates(dir=raw, overrides=overrides, **effective)


def _open_root(raw: str, source: str) -> str:
    try:
        st = os.stat(raw)
    except FileNotFoundError as exc:
        raise ConfigError(f"{source}: prompts directory {raw!r} does not exist") from exc
    except OSError as exc:
        raise ConfigError(f"{source}: cannot read prompts directory {raw!r}: {_reason(exc)}") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise ConfigError(f"{source}: prompts directory {raw!r} is not a directory")
    try:
        return confine_to_cwd(raw)
    except PermissionError as exc:
        raise ConfigError(f"{source}: prompts directory {raw!r} {_reason(exc)}") from exc


def _load_one(name: str, path: str, root: str, source: str) -> TemplateFile:
    if not _is_within(os.path.realpath(path), root):
        raise ConfigError(f"{source}: prompt template {path!r} resolves outside the prompts directory")
    try:
        resolved = check_readable_path(path, confine=True)
        with open(resolved, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise OSError(errno.EINVAL, "not a regular file", path)
            if st.st_size > MAX_TEMPLATE_BYTES:
                raise _too_large(source, path, st.st_size)
            data = fh.read(MAX_TEMPLATE_BYTES + 1)
    except ConfigError:
        raise
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{source}: cannot read prompt template {path!r}: {_reason(exc)}") from exc
    if len(data) > MAX_TEMPLATE_BYTES:
        raise _too_large(source, path, len(data))
    try:
        text = decode_text(data)
    except UnicodeDecodeError as exc:
        offset = exc.start + (len(codecs.BOM_UTF8) if data.startswith(codecs.BOM_UTF8) else 0)
        raise ConfigError(
            f"{source}: prompt template {path!r} is not UTF-8 text ({exc.reason} at byte {offset})"
        ) from exc
    if "\x00" in text:
        raise ConfigError(f"{source}: prompt template {path!r} contains NUL bytes; expected Markdown text")
    _validate(name, text, path, source)
    return TemplateFile(name=name, path=path, text=text, sha256=hashlib.sha256(data).hexdigest(), chars=len(text))


def _validate(name: str, text: str, path: str, source: str) -> None:
    packaged = _packaged_text(name)
    known = placeholders(packaged)
    found = placeholders(text)
    if name in REVIEW_TEMPLATES:
        known |= OPTIONAL_PLACEHOLDERS
        head, marker, tail = text.partition(CONTEXT_MARKER)
        if not marker:
            raise ConfigError(
                f"{source}: prompt template {path!r} is missing the {CONTEXT_MARKER!r} marker, which "
                "splits the system prompt (above it) from the user prompt (below it)"
            )
        missing = required_placeholders(name) - placeholders(tail)
        if missing:
            raise ConfigError(
                f"{source}: prompt template {path!r} is missing required placeholder(s) "
                f"{_braced(missing)} after the {CONTEXT_MARKER!r} marker"
            )
        if CONTEXT_MARKER in tail:
            logger.warning(
                "%s: prompt template %r has more than one %r marker; only the first splits the "
                "prompt, and the rest reach the user prompt as text",
                source, path, CONTEXT_MARKER,
            )
        above = placeholders(head) & known
        if above:
            logger.warning(
                "%s: prompt template %r has %s above the %r marker; only text below it is filled, "
                "so these reach the system prompt literally",
                source, path, _braced(above), CONTEXT_MARKER,
            )
    else:
        missing = SUMMARY_REQUIRED - found
        if missing:
            raise ConfigError(
                f"{source}: prompt template {path!r} is missing the required {_braced(missing)} placeholder"
            )
    unknown = found - known
    if unknown:
        logger.warning(
            "%s: prompt template %r has unknown placeholder(s) %s, which render literally "
            "(a typo?); %s.md fills %s",
            source, path, _braced(unknown), name, _braced(known),
        )


def _packaged_text(name: str) -> str:
    return resources.files("prxref").joinpath("prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")


def _require_name(name: str) -> None:
    if name not in TEMPLATE_NAMES:
        raise ValueError(f"template name must be one of {', '.join(TEMPLATE_NAMES)}, got {name!r}")


def _too_large(source: str, path: str, size: int) -> ConfigError:
    return ConfigError(
        f"{source}: prompt template {path!r} is {size} bytes, over the {MAX_TEMPLATE_BYTES}-byte "
        "(256 KiB) limit; templates are never truncated"
    )


def _braced(names: frozenset[str]) -> str:
    return ", ".join(f"{{{n}}}" for n in sorted(names))


def _reason(exc: BaseException) -> str:
    return getattr(exc, "strerror", None) or str(exc)


def _is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False
