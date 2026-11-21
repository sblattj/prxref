"""Lower a failure claim the PR's own diff already contains (issue #175).

Two shapes, both read from the parsed diff only, never from a model:

``null_return_guarded_by_caller``: an ``error`` or ``warning`` finding says a
function can return ``null`` (``None``, ``nil``, ``undefined``) and that a
caller then fails. When another file of the same diff adds a null or blank
check on a variable tied to the call (assigned from a call to that function
within :data:`GUARD_WINDOW` lines above, or, in a file whose name holds a word
the finding uses and its own file's name does not, such as ``the parser`` for
``ConfigParser.java``, a parameter of the function the check sits in), and the
check's own block (at most :data:`GUARD_WINDOW` lines) throws or raises a
named exception, the caller already turns the null into a clear error. The
claimed failure cannot happen, so the finding drops to ``outofscope`` (shown
as minor) with the guard named.

``null_return_ruled_out_by_writer``: the same null-return claim, where the
anchored line is an added read ``<getter>(...).<field>()`` whose getter is
``get``/``read``/``fetch``/``load`` plus a noun (``getValue(id).text()``).
When every call in the diff's post-image (added and context lines, all files)
that writes that noun (``put``/``set``/``write``/``store``/``save`` plus the
same noun, any case or underscores) is exactly one added call, and it passes
the field the read uses as a named argument (``Text=text``) whose value is not
a null literal, the only writer the PR shows always stores that field, so the
null case the finding claims does not come from this PR's writes. The finding
drops to ``outofscope`` with the writer named. A positional writer, a second
writer, or one that leaves the field out ties nothing. Writers outside the
diff are not visible, and the note says so.

``precondition_rules_out_preexisting``: an ``error`` or ``warning`` finding
says a delete, remove or overwrite can hit something the call never created
(``pre-existing``, ``never created``, ``didn't create``). When the same
function, above the anchor in the same hunk, adds or keeps an existence check
(``find``, ``exists``, ``isPresent``, ``contains``, ``has``, ``get``,
``lookup``) that passes when the value exists, on an identifier the anchored line also
uses, and whose own block (at most :data:`GUARD_WINDOW` lines) throws, raises
or returns, a pre-existing value
never reaches the delete. Only a race between the check and the create is
left, so the finding drops to ``outofscope`` with the check named.

The appended ``Contained:`` paragraph names the guard and says it is handled
there, so :func:`prxref.quality.apply_containment_note` adds no
``[containment boundary not stated]`` suffix afterwards. The check never drops
a finding and never moves confidence; a dropped finding, an ``outofscope`` or
``spec`` finding, and a body that already holds ``Contained:`` pass through as
the same object.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import PurePosixPath

from .triage import FileDiff, Finding

CONTAINED_REASONS: tuple[str, ...] = (
    "null_return_guarded_by_caller",
    "precondition_rules_out_preexisting",
    "null_return_ruled_out_by_writer",
)

GUARD_WINDOW: int = 3
"""Lines after a guard's condition that may hold its throw, raise or return."""

PRECONDITION_LOOKBACK: int = 40
"""Lines above the anchor searched for an existence check in the same function."""

_LOWERED = frozenset({"error", "warning"})
_MARK = "Contained:"

_NULL_RETURN_RE = re.compile(
    r"\breturn(?:s|ing)?\b[^.;\n]{0,40}?\b(?:null|none|nil|undefined)\b",
    re.IGNORECASE,
)
_CALLER_FAILS_RE = re.compile(r"\bcallers?\b", re.IGNORECASE)
_RETURNER_RE = re.compile(
    r"`?([A-Za-z_][\w.]*)`?(?:\(\))?\s+(?:can|may|could|might|will)\s+(?:also\s+)?return\b",
)
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_NULL_CHECK_RE = re.compile(
    r"\b([A-Za-z_]\w*)\s*(?:===?|is)\s*(?:null|None|nil|undefined)\b"
    r"|\b(?:null|None|nil)\s*===?\s*([A-Za-z_]\w*)"
    r"|\b([A-Za-z_]\w*)\.(?:isBlank|isEmpty|is_none)\(\)"
    r"|\b(?:isBlank|isEmpty|requireNonNull|isNullOrEmpty|isNullOrBlank)\(\s*([A-Za-z_]\w*)"
    r"|\bif\s+not\s+([A-Za-z_]\w*)\s*(?:or\b|:)|\bif\s*\(?\s*!([A-Za-z_]\w*)\b(?!\s*[.(])",
)
_ASSIGN_RE = re.compile(
    r"^\s*(?:(?:final|const|let|var|val|auto)\s+)?(?:[\w<>\[\],.?]+\s+)?([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)",
)
_TYPED_THROW_RE = re.compile(
    r"\b(?:throw\s+(?:new\s+)?|raise\s+|panic\s*\(\s*)([A-Z][A-Za-z0-9_]*)",
)
_UNTYPED_EXCEPTIONS = frozenset({
    "NullPointerException", "TypeError", "AttributeError", "Exception",
    "RuntimeException", "Error",
})

_READ_FIELD_RE = re.compile(
    r"\b(?:get|read|fetch|load)_?([A-Za-z]\w*)\s*\([^()]*\)\s*\.\s*([A-Za-z_]\w*)\s*\(\s*\)",
)
_WRITE_CALL_RE = re.compile(r"\b(?:put|set|write|store|save)_?([A-Za-z]\w*)\s*\(([^()]*)\)")
_NAMED_ARG_RE = re.compile(r"\b([A-Za-z_]\w*)\s*[=:](?!=)\s*([^,]+)")
_NULL_LITERAL_RE = re.compile(r"^\s*(?:null|None|nil|undefined)\s*$")

_DESTRUCTIVE_RE = re.compile(
    r"\b(?:delet|remov|overwrit|destroy|drop|clobber|wip)\w*", re.IGNORECASE,
)
_NOT_OURS_RE = re.compile(
    r"\bnever\s+created\b|\bdid(?:n't|\s+not)\s+create\b|\bnot\s+created\s+by\b"
    r"|\bpre-?existing\b|\balready\s+existed\b|\bit\s+did(?:n't|\s+not)\s+(?:create|make|write)\b",
    re.IGNORECASE,
)
_EXISTENCE_CHECK_RE = re.compile(
    r"^\s*(?:\}\s*)?(?:else\s+)?(?:if|unless|elif)\b.*?"
    r"\b(?:find\w*|exists?\w*|isPresent|is_present|contains\w*|has\w*|get\w*|lookup\w*)\s*\(",
)
_NEGATED_TEST_RE = re.compile(
    r"!\s*(?!=)|\bnot\b|\bunless\b|isEmpty|isAbsent|is_none|isNull|\bnone\b"
    r"|===?\s*(?:null|None|nil|undefined|false|False|0)\b|\bis\s+None\b",
    re.IGNORECASE,
)
_CONDITIONAL_RE = re.compile(
    r"^\s*(?:if|else|elif|unless|for|foreach|while|switch|case|when|try|catch|except|with)\b",
)
_EXIT_RE = re.compile(r"\b(?:throw|raise|return|panic|abort)\b")
_FUNC_DEF_RE = re.compile(
    r"^\s*(?:def|async\s+def|func|fn|function)\s+\w+"
    r"|^\s*(?:(?:public|private|protected|internal|static|final|override|async|suspend)\s+)+"
    r"[\w<>\[\],.? ]*?\b\w+\s*\([^;]*$",
)
_KEYWORDS = frozenset({
    "if", "else", "elif", "unless", "not", "and", "or", "is", "in", "new", "throw",
    "raise", "return", "try", "catch", "except", "finally", "null", "none", "nil",
    "true", "false", "this", "self", "e", "err", "ex",
})


def apply_contained_claim_check(
    findings: Sequence[Finding], files: Sequence[FileDiff],
) -> tuple[list[Finding], list[str | None]]:
    """Lower a failure claim the diff's own guard rules out (#175).

    Returns the findings in input order and, per finding, the name from
    :data:`CONTAINED_REASONS` that fired, ``None`` for a finding passed
    through as the same object. A lowered finding keeps its title, file,
    line and confidence; its severity becomes ``outofscope`` and its body
    gains a ``Contained:`` paragraph naming the guard's ``file:line``.
    """
    by_path = {f.path: f for f in files}
    out: list[Finding] = []
    reasons: list[str | None] = []
    for f in findings:
        if f.drop_reason is not None or f.severity not in _LOWERED or _MARK in (f.body or ""):
            out.append(f)
            reasons.append(None)
            continue
        claim = f"{f.title}. {f.body}"
        note = _null_return_guard(f, claim, files)
        reason = "null_return_guarded_by_caller" if note else None
        if note is None:
            note = _precondition_guard(f, claim, by_path.get(f.file))
            reason = "precondition_rules_out_preexisting" if note else None
        if note is None:
            note = _null_return_writer(f, claim, files)
            reason = "null_return_ruled_out_by_writer" if note else None
        if note is None:
            out.append(f)
            reasons.append(None)
            continue
        body = f"{f.body}\n\n{note}" if f.body else note
        out.append(replace(f, severity="outofscope", body=body))
        reasons.append(reason)
    return out, reasons


def _post_image(fd: FileDiff) -> list[tuple[int, str, bool]]:
    """The file's added and context lines as ``(new_line, text, is_added)``."""
    return [
        (ln.new_line, ln.text, ln.kind == "+")
        for h in fd.hunks for ln in h.lines
        if ln.kind != "-" and ln.new_line
    ]


def _returner_names(claim: str) -> set[str]:
    """Function names the claim says can return null, last dotted piece."""
    names = {m.group(1).rsplit(".", 1)[-1] for m in _RETURNER_RE.finditer(claim)}
    for span in _BACKTICK_RE.findall(claim):
        m = re.fullmatch(r"\s*(?:[\w.]+\.)?([A-Za-z_]\w*)\s*\(.*\)?\s*", span)
        if m:
            names.add(m.group(1))
    return {n for n in names if len(n) >= 3 and n.lower() not in _KEYWORDS}


def _stem_words(path: str) -> set[str]:
    """Lowercase words of a file's stem: ``ConfigParser.java`` -> config, parser."""
    stem = PurePosixPath(path).stem
    parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", stem)
    return {p.lower() for p in parts if len(p) >= 4}


def _null_return_guard(f: Finding, claim: str, files: Sequence[FileDiff]) -> str | None:
    """The ``Contained:`` note for a guarded null-return claim, else ``None``."""
    if not (_NULL_RETURN_RE.search(claim) and _CALLER_FAILS_RE.search(claim)):
        return None
    names = _returner_names(claim)
    claim_words = {w.lower() for w in re.findall(r"[A-Za-z]+", claim)}
    own_words = _stem_words(f.file)
    for fd in files:
        if fd.path == f.file:
            continue
        rows = _post_image(fd)
        guard = None
        if names:
            guard = _null_guard_in(rows, _call_results(rows, names))
        if guard is None and (_stem_words(fd.path) - own_words) & claim_words:
            guard = _null_guard_in(rows, None)
        if guard is not None:
            line, exc = guard
            return (
                f"{_MARK} the null case is handled by the caller this PR adds: "
                f"`{fd.path}:{line}` rejects a null or blank value with `{exc}`, so "
                "the claimed downstream failure cannot happen. What is left is how "
                "clear that error is."
            )
    return None


def _code_only(text: str) -> str:
    """``text`` with string literals emptied and any comment cut off.

    A line that starts a comment (``//``, ``#``, ``/*``, ``*``) is no code at
    all, so a write call quoted in a comment or a string is never a writer.
    """
    code = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', text)
    if re.match(r"^\s*(?://|#|/\*|\*)", code):
        return ""
    return re.split(r"(?://|#|/\*)", code, maxsplit=1)[0]


def _norm(word: str) -> str:
    """``put_value`` / ``putValue`` / ``Value`` -> ``value``."""
    return word.replace("_", "").lower()


def _null_return_writer(f: Finding, claim: str, files: Sequence[FileDiff]) -> str | None:
    """The ``Contained:`` note when the PR's only writer always sets the field read."""
    if not _NULL_RETURN_RE.search(claim):
        return None
    anchor = next(
        (text for fd in files if fd.path == f.file
         for line, text, added in _post_image(fd) if added and line == f.line),
        None,
    )
    m = _READ_FIELD_RE.search(anchor or "")
    if m is None:
        return None
    noun, field = _norm(m.group(1)), _norm(m.group(2))
    writers: list[tuple[str, int, str, bool]] = []
    for fd in files:
        for line, text, added in _post_image(fd):
            code = _code_only(text)
            for w in _WRITE_CALL_RE.finditer(code):
                if _norm(w.group(1)) == noun:
                    writers.append((fd.path, line, w.group(2), added))
    if len(writers) != 1:
        return None
    path, line, args, added = writers[0]
    if not added:
        return None
    named = {_norm(a.group(1)): a.group(2) for a in _NAMED_ARG_RE.finditer(args)}
    if field not in named or _NULL_LITERAL_RE.match(named[field]):
        return None
    arg = next(a.group(1) for a in _NAMED_ARG_RE.finditer(args) if _norm(a.group(1)) == field)
    return (
        f"{_MARK} the only writer this PR adds, `{path}:{line}`, always stores "
        f"`{arg}`, the field `{m.group(2)}()` reads, so the null case is handled by "
        "that writer and does not come from this PR's writes. Writers outside this "
        "PR are not visible to this check."
    )


def _call_results(rows: list[tuple[int, str, bool]], names: set[str]) -> dict[int, str]:
    """Per new-file line, the identifier a call to one of ``names`` assigns."""
    out: dict[int, str] = {}
    for line, text, _ in rows:
        if any(re.search(rf"\b{re.escape(n)}\s*\(", text) for n in names):
            m = _ASSIGN_RE.match(text)
            if m:
                out[line] = m.group(1)
    return out


def _null_guard_in(
    rows: list[tuple[int, str, bool]], call_results: dict[int, str] | None,
) -> tuple[int, str] | None:
    """``(line, exception)`` of the first tied, added null check that throws a typed error.

    ``rows`` are the file's post-image lines (added and context) in diff
    order, so a tie can see an unchanged line; the check itself must be
    added. With ``call_results`` the checked variable must be one a call
    assigned at most :data:`GUARD_WINDOW` lines above, with no line in between
    assigning it again; without it (the file is tied only by its name) the
    checked variable must be a parameter of the function the check sits in,
    so the guard covers a value handed in from outside.
    """
    plain = [(line, text) for line, text, _ in rows]
    for i, (line, text, is_added) in enumerate(rows):
        checked = _checked_names(text) if is_added else set()
        if not checked:
            continue
        if call_results is not None:
            if not checked & _fed_names(rows, i, call_results):
                continue
        elif not checked & _enclosing_params(plain, i):
            continue
        for after in _guard_block(plain, i):
            m = _TYPED_THROW_RE.search(after)
            if m and m.group(1) not in _UNTYPED_EXCEPTIONS:
                return line, m.group(1)
    return None


def _fed_names(rows: list[tuple[int, str, bool]], i: int, call_results: dict[int, str]) -> set[str]:
    """Variables a call assigned within the window above row ``i``, not reassigned since."""
    line = rows[i][0]
    fed: set[str] = set()
    for at, name in call_results.items():
        if not 0 <= line - at <= GUARD_WINDOW:
            continue
        between = [t for n, t, _ in rows if at < n < line]
        if len(between) != line - at - 1:
            continue
        if any((m := _ASSIGN_RE.match(t)) and m.group(1) == name for t in between):
            continue
        fed.add(name)
    return fed


def _checked_names(text: str) -> set[str]:
    """Identifiers a line tests for null or blank."""
    return {next(g for g in m.groups() if g) for m in _NULL_CHECK_RE.finditer(text)}


def _enclosing_params(rows: list[tuple[int, str]], i: int) -> set[str]:
    """Parameter identifiers of the function header above row ``i``.

    The walk stops at a gap in the line numbers: past it the enclosing
    function was not read, so nothing is claimed.
    """
    for j in range(i, -1, -1):
        if j < i and rows[j][0] != rows[j + 1][0] - 1:
            return set()
        text = rows[j][1]
        if _FUNC_DEF_RE.search(text):
            inner = text[text.find("(") + 1:text.rfind(")") if ")" in text else len(text)]
            return _idents(inner)
    return set()


def _precondition_guard(f: Finding, claim: str, fd: FileDiff | None) -> str | None:
    """The ``Contained:`` note for a ruled-out pre-existing claim, else ``None``."""
    if fd is None or not (f.line or 0) > 0:
        return None
    if not (_DESTRUCTIVE_RE.search(claim) and _NOT_OURS_RE.search(claim)):
        return None
    for h in fd.hunks:
        rows = [(ln.new_line, ln.text) for ln in h.lines if ln.kind != "-" and ln.new_line]
        idx = next((i for i, (n, _) in enumerate(rows) if n == f.line), None)
        if idx is None:
            continue
        anchor_ids = set()
        for _, text in rows[max(0, idx - 1):idx + 2]:
            anchor_ids |= _idents(text)
        for j in range(idx - 1, max(-1, idx - 1 - PRECONDITION_LOOKBACK), -1):
            line, text = rows[j]
            if _FUNC_DEF_RE.search(text):
                break
            if not _EXISTENCE_CHECK_RE.search(text) or _negated(text):
                continue
            shared = _idents(text) & anchor_ids
            exits = any(_EXIT_RE.search(t) for t in _guard_block(rows, j))
            if shared and exits:
                return (
                    f"{_MARK} the existence check at `{f.file}:{line}` exits before "
                    f"this path when `{sorted(shared)[0]}` already exists, so a "
                    "pre-existing value is handled there and never reaches it. What is "
                    "left is a race between that check and the create."
                )
        return None
    return None


def _guard_block(rows: Sequence[tuple[int, str]], i: int) -> list[str]:
    """The guard line and the lines of its own block, at most :data:`GUARD_WINDOW`.

    A guard line that does not open a block (``if x is None: raise E``, or a
    braceless one-liner) is its own block. Otherwise the block ends at a line
    that closes a brace or sits no deeper than the guard, so an exit after the
    block (``if (exists) { log(); } return;``) is not read as the guard's own.
    A gap in the line numbers (an unchanged line between two added ones) ends
    it too, since the lines in between were not read. Only the block's top
    level is kept: a line nested deeper, or one that is itself a condition or
    loop (``if (flag) throw ...``), exits only sometimes, so it is left out.
    """
    head = rows[i][1]
    out = [head]
    if not re.search(r"[{:]\s*$", head.split("//", 1)[0].rstrip()):
        return out
    depth = len(head) - len(head.lstrip())
    prev = rows[i][0]
    body_depth: int | None = None
    for at, text in rows[i + 1:i + 1 + GUARD_WINDOW]:
        if at != prev + 1:
            break
        prev = at
        if not text.strip():
            continue
        indent = len(text) - len(text.lstrip())
        if text.lstrip().startswith("}") or indent <= depth:
            break
        if body_depth is None:
            body_depth = indent
        if indent > body_depth or _CONDITIONAL_RE.match(text):
            continue
        out.append(text)
    return out


def _negated(text: str) -> bool:
    """True when an ``if`` line's test passes on absence, not on existence."""
    return bool(re.search(r"\bunless\b", text) or _NEGATED_TEST_RE.search(_condition(text)))


def _condition(text: str) -> str:
    """The condition of an ``if`` line, without its trailing brace or colon."""
    code = text.split("//", 1)[0].split("#", 1)[0]
    m = re.search(r"\b(?:if|elif|unless)\b(.*)", code)
    return (m.group(1) if m else code).rstrip(" {:")


def _idents(text: str) -> set[str]:
    """Plain identifiers on a line, minus keywords, case kept."""
    code = text.split("//", 1)[0].split("#", 1)[0]
    return {t for t in _IDENT_RE.findall(code) if t.lower() not in _KEYWORDS and len(t) >= 2}
