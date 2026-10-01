"""Deterministic CI-wiring check (issue #66), on by default.

A PR that adds a verification script (a ``scripts/verify.sh`` smoke test, a
new test file, a check flag) has only partly met an acceptance criterion
like "add regression checks so this cannot come back" when nothing in the
repository's CI configuration ever runs it: the check runs only when
someone remembers to run it locally. This module flags exactly that — a
check-shaped file the PR adds that no CI configuration file invokes.

Like :mod:`prxref.metadata_rules` it is a companion to
:mod:`prxref.heuristics` under the same doctrine: no model in the loop,
every finding ends its body with " (deterministic check, no model)" (the
suffix is imported from heuristics so the two literals cannot drift), so
:func:`prxref.heuristics.is_deterministic` exempts it from severity
consistency. Unlike the metadata checks it is not pure: it must READ the
repository's CI files, so every I/O it makes is a ``read`` callable the
orchestrator passes in (the forge's head-sha reads or ``--repo-dir``,
never the ``repo_context`` reader whose chunk caps a CI file could starve
on), bounded by :data:`MAX_CI_FILES` literal reads per run. The check is
on by default — ``ci_wiring = "off"`` turns it off — and never changes the verdict: a finding
is ``spec`` when the ticket mentions regression checks, CI, pipelines or
automated tests (relabeled ``warning`` by spec grounding on an ungrounded
run) and ``warning`` otherwise.

A CI step that runs the check through a runner is followed exactly one hop:
``make verify`` reads the root Makefile (``GNUmakefile``, ``makefile``,
``Makefile``, GNU make's lookup order) and counts the check wired when the
``verify`` rule's recipe, or the recipe of a prerequisite it reaches in
that same Makefile, names it; ``npm run verify``, ``npm test``,
``yarn verify`` and ``pnpm run verify`` do the same over the root
``package.json`` ``scripts`` entry. A runner file is read only when a CI
invocation line names a runner and some check is not invoked directly,
and only when the listing shows it (or the source cannot list).

A check can also be a runner target (OD10's target kind): a rule the PR
adds to the root Makefile, or a ``scripts`` entry it adds to the root
``package.json``, whose name says verify/smoke/check or is a whole
``test``/``tests``/``e2e`` token. It is wired when a CI invocation line
runs it (``make verify``, ``npm run smoke``, ``yarn smoke``) or runs a
runner entry that reaches it one runner file deep — a make goal whose
prerequisites reach it, or a recipe or script body that runs it
(``$(MAKE) verify`` included, for a target candidate only). A
target in a nested Makefile or workspace ``package.json`` is never a
candidate, since the one-hop follow reads only the root runner files.

Known false negatives (a check reported wired that CI never runs),
accepted for v1: a CI job that only copies the script
(``cp scripts/verify.sh stage.sh``) still names its path. Known false
positives (a wired check flagged anyway), the price of a bounded,
deterministic search: an invocation inside a folded YAML block scalar the
indentation scanner mis-slices, and a runner chain the one-file hop does
not follow — a recipe calling ``$(MAKE) smoke``, ``make -C sub`` /
``make -f other.mk``, a workspace ``npm --prefix web run verify``, a
script body calling another script, or a ``justfile``/``Taskfile``/
``tox.ini`` runner.
"""
from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from .heuristics import _BODY_SUFFIX
from .rules import match_globs
from .triage import FileDiff, Finding

#: The name fragments that make a file check-shaped: a script whose name
#: (or a ``--flag`` it gains) says verify, smoke or check.
CI_SUFFIX_HINT: tuple[str, ...] = ("verify", "smoke", "check")

#: The built-in CI-configuration globs, restated as ``ci_wiring_globs`` in
#: :mod:`prxref.config` (config stays a leaf module); a set value replaces
#: this set rather than adding to it, as ``context_contract_globs`` does.
DEFAULT_CI_GLOBS: tuple[str, ...] = (
    ".github/workflows/*.y*ml",
    ".gitlab-ci.yml",
    "azure-pipelines.yml",
    ".circleci/config.yml",
    "Jenkinsfile",
    "bitbucket-pipelines.yml",
    ".drone.yml",
    "cloudbuild.yaml",
    ".travis.yml",
)

#: The most CI files one run reads, bounding the check's read cost however
#: many workflow files a glob match finds.
MAX_CI_FILES = 12

# The ticket words that upgrade a finding to ``spec``: the issue's own
# criterion ("the ticket ... mentions regression checks, CI, or automated
# tests"). ``ci\b`` so "ci" matches "CI" and "ci:" but not "circle".
_SPEC_TICKET_RE = re.compile(
    r"regression check|regression test|ci\b|pipeline|automated test", re.I,
)

# A ``--flag`` an added line gains: ``--verify``, ``--smoke-test``,
# ``--check-headers``. Long-form only; a single-dash ``-v`` style flag is
# too short to be check-shaped.
_FLAG_RE = re.compile(r"--([A-Za-z0-9][A-Za-z0-9_-]*)")

# A YAML mapping key line, with GitHub's list dash: ``run: x``,
# ``- run: x``, ``    script:``. The key must be followed by ``:`` but not
# ``:=``-style text; Azure's ``- bash: |`` is the same shape.
_YAML_KEY_RE = re.compile(r"^(?P<indent> *)(?P<dash>- +)?(?P<key>[A-Za-z_][\w.-]*) *:(?!=)")

#: YAML keys whose lines (and indented blocks) are invocations: GitHub's
#: ``run``/``uses``, GitLab's and Bitbucket's ``script``, Azure's ``bash``/
#: ``pwsh``/``script``, plus the generic shell spellings.
_INVOCATION_KEYS = frozenset({
    "run", "uses", "script", "bash", "pwsh", "powershell", "shell", "cmd",
})

#: A line that is a command outright, no YAML key above it: a plain shell
#: line, a Makefile recipe body, a Jenkins ``sh '...'`` step.
_PLAIN_COMMAND_PREFIXES = ("sh ", "bash ", "./", "make ", "pytest ", "npm run ")

#: The root Makefile names, in GNU make's lookup order: the first one the
#: source returns is the one ``make`` would run.
MAKEFILE_NAMES: tuple[str, ...] = ("GNUmakefile", "makefile", "Makefile")

#: The root npm manifest whose ``scripts`` entries npm, yarn and pnpm run.
PACKAGE_JSON = "package.json"

#: Every runner file the one-hop follow may read, at most once each per run.
RUNNER_FILES: tuple[str, ...] = MAKEFILE_NAMES + (PACKAGE_JSON,)

_RUNNER_RE = re.compile(r"(?<![\w./$-])(make|npm|pnpm|yarn)(?![\w.-])([^;&|()\n]*)")

_MAKE_REDIRECT_OPTIONS = ("-C", "-f", "--directory", "--file", "--makefile")

_MAKE_ARG_OPTIONS = frozenset({"-o", "-W", "-I", "--old-file", "--what-if", "--include-dir"})

_MAKE_COUNT_OPTIONS = frozenset({"-j", "-l", "--jobs", "--load-average"})

_NPM_REDIRECT_OPTIONS = (
    "--prefix", "-C", "--dir", "--cwd", "--filter", "-F", "-w", "--workspace",
)

_NPM_RUN_VERBS = frozenset({"run", "run-script", "rum", "urn"})

_NPM_SCRIPT_ALIASES = {
    "t": "test", "tst": "test", "test": "test",
    "start": "start", "stop": "stop", "restart": "restart",
}

_YARN_BUILTINS = frozenset({
    "add", "audit", "bin", "cache", "ci", "config", "create", "dedupe", "dlx",
    "exec", "i", "import", "info", "init", "install", "link", "list", "ls",
    "outdated", "pack", "publish", "remove", "rm", "store", "unlink", "up",
    "update", "upgrade", "why", "workspace", "workspaces", "x",
})

_MAKE_RULE_RE = re.compile(r"^(?P<targets>[^\s:=#][^:=#]*?)\s*::?(?!=)(?P<rest>.*)$")


_TEST_TARGET_TOKENS = frozenset({"test", "tests", "e2e"})

_MAKE_VAR_RE = re.compile(r"\$[({]MAKE[)}]")

_JSON_STRING_KEY_RE =re.compile(r'^\s*"(?P<key>[^"\\]+)"\s*:\s*"')


@dataclass(frozen=True)
class CiCandidate:
    """One check-shaped file, or runner target, the PR adds or changes.

    ``path`` is the diff path; ``reason`` is the human phrase naming why
    the file counts (which hint matched, the shebang, or a test file
    outside the runner's default include) — it rides the finding body and
    the run record unchanged. ``target`` is None for a file candidate and
    the runner entry (``("make", name)`` or ``("npm", name)``, the shape
    :func:`runner_targets` returns) for a target candidate, whose ``path``
    is then the root Makefile or ``package.json`` that defines it.
    """

    path: str
    reason: str
    new: bool = True
    target: tuple[str, str] | None = None

    @property
    def label(self) -> str:
        """The finding's subject: the diff path, or the target's command spelling."""
        return _hop_label(self.target) if self.target is not None else self.path


def _added_lines(file: FileDiff):
    """``(new_line, text)`` for every added (``+``) line, in diff order.

    Same shape as :func:`prxref.heuristics._added_lines`, restated because
    that helper is private to heuristics.
    """
    for hunk in file.hunks:
        for ln in hunk.lines:
            if ln.kind == "+" and ln.new_line is not None:
                yield ln.new_line, ln.text


def _name_tokens(name: str) -> tuple[str, ...]:
    """``name`` lowercased and split on every non-alphanumeric run."""
    return tuple(part for part in re.split(r"[^a-z0-9]+", name.lower()) if part)


def _hint_in_tokens(tokens: tuple[str, ...]) -> str | None:
    """The first CI hint any token contains, or None."""
    for hint in CI_SUFFIX_HINT:
        if any(hint in token for token in tokens):
            return hint
    return None


def default_include(path: str) -> bool:
    """True when ``path`` sits inside a test runner's default include.

    Conservative table, matched case-sensitively on the basename like
    heuristics' frozen basename sets (real ecosystem tooling always emits
    these exact spellings): pytest ``test_*.py`` / ``*_test.py``, jest and
    vitest ``*.test.*`` / ``*.spec.*`` over ts/tsx/js/jsx/mjs/cjs and the
    ``__tests__/`` directory, Go ``*_test.go``, JUnit ``*Test.java`` /
    ``*IT.java``, XCTest ``*Tests.swift`` / ``*Tests.m``, and any file
    under ``tests/``, ``test/`` or ``spec/`` for the ecosystems with no
    name convention (Rust's ``tests/`` included). A file in here runs when
    the suite runs, so it is never a CI-wiring candidate.
    """
    parts = PurePosixPath(path).parts
    if any(part in ("__tests__", "tests", "test", "spec") for part in parts[:-1]):
        return True
    name = parts[-1] if parts else path
    if any(
        fnmatch.fnmatchcase(name, pattern)
        for pattern in (
            "test_*.py", "*_test.py", "*_test.go",
            "*Test.java", "*IT.java", "*Tests.swift", "*Tests.m",
        )
    ):
        return True
    return any(
        name.endswith(suffix)
        for suffix in (
            ".test.ts", ".test.tsx", ".test.js", ".test.jsx", ".test.mjs", ".test.cjs",
            ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx", ".spec.mjs", ".spec.cjs",
        )
    )


def _looks_like_test(path: str) -> bool:
    """True when a whole basename token says test or spec.

    ``src/App.tests.tsx`` (jest's default pattern is ``.test.``, singular)
    and ``src/login_test.jsx`` look like tests; ``special_offer.py`` does
    not ("spec" as a prefix is not "spec" as a word), and neither does
    ``testutils.py`` (a helper, not a check).
    """
    return any(
        token in ("test", "tests", "spec", "specs")
        for token in _name_tokens(PurePosixPath(path).name)
    )


def _target_hint(name: str) -> str | None:
    """The word that makes a runner target check-shaped, or None.

    A :data:`CI_SUFFIX_HINT` word inside any name token, or a whole
    ``test``/``tests``/``e2e`` token: ``verify``, ``smoke-api``,
    ``test-integration`` and ``e2e`` count, ``latest`` and ``build`` do not.
    """
    tokens = _name_tokens(name)
    return _hint_in_tokens(tokens) or next(
        (token for token in tokens if token in _TEST_TARGET_TOKENS), None,
    )


def _rule_line(text: str) -> tuple[list[str], bool]:
    """The targets a Makefile line defines, and whether it is a target-specific assignment.

    A recipe (tab-indented) line or any non-rule line defines nothing. A
    special (``.PHONY``), pattern (``%``) or variable-built (``$``) target
    is never returned.
    """
    if text.startswith("\t"):
        return [], False
    match = _MAKE_RULE_RE.match(text)
    if match is None:
        return [], False
    targets = [
        target for target in match.group("targets").split()
        if not target.startswith(".") and "%" not in target and "$" not in target
    ]
    return targets, "=" in match.group("rest").partition(";")[0]


def _make_target_candidates(file: FileDiff) -> list[CiCandidate]:
    """The check-shaped make targets an added rule line of a root Makefile defines.

    A target already on a removed or context line of the diff existed
    before the PR and never counts, and neither does a target-specific
    variable line (``verify: GOFLAGS=-count=1``), which defines no rule.
    """
    existing: set[str] = set()
    added: dict[str, None] = {}
    for hunk in file.hunks:
        for ln in hunk.lines:
            targets, assignment = _rule_line(ln.text)
            if ln.kind != "+":
                existing.update(targets)
            elif not assignment:
                added.update(dict.fromkeys(targets))
    out: list[CiCandidate] = []
    for name in added:
        hint = _target_hint(name)
        if name in existing or hint is None:
            continue
        out.append(CiCandidate(
            file.path, f"it is a new make target whose name mentions {hint!r}",
            True, ("make", name),
        ))
    return out


def _npm_target_candidates(
    file: FileDiff, read: Callable[[str], str | None] | None,
) -> list[CiCandidate]:
    """The check-shaped ``scripts`` entries an added line of the root package.json defines.

    An added string-valued ``"name": "..."`` line whose key is on no
    removed or context line is a candidate only when the head
    ``package.json`` (through ``read``) lists it under ``scripts`` — a
    hunk rarely shows the enclosing object, and a dependency named
    ``check-types`` is not a script. No ``read``, a miss, a raising read
    or a malformed manifest yields nothing.
    """
    existing: set[str] = set()
    added: dict[str, None] = {}
    for hunk in file.hunks:
        for ln in hunk.lines:
            match = _JSON_STRING_KEY_RE.match(ln.text)
            if match is None:
                continue
            if ln.kind == "+":
                added.setdefault(match.group("key"))
            else:
                existing.add(match.group("key"))
    wanted = {
        name: hint for name in added
        if name not in existing and (hint := _target_hint(name)) is not None
    }
    if not wanted or read is None:
        return []
    try:
        text = read(file.path)
    except Exception:  # noqa: BLE001
        return []
    scripts = _npm_scripts(text) if isinstance(text, str) else {}
    return [
        CiCandidate(
            file.path, f"it is a new package.json script whose name mentions {hint!r}",
            True, ("npm", name),
        )
        for name, hint in wanted.items()
        if name in scripts
    ]


def target_candidates(
    files: Sequence[FileDiff],
    read: Callable[[str], str | None] | None = None,
) -> list[CiCandidate]:
    """The check-shaped runner targets ``files`` add (OD10's target kind).

    Only the root runner files the one-hop follow reads count: a rule
    added to a root ``GNUmakefile``/``makefile``/``Makefile``, matched by
    ``make <name>``, and a ``scripts`` entry added to the root
    ``package.json``, matched by ``npm run``/``yarn``/``pnpm`` (see
    :func:`runner_targets`). A target is check-shaped when its name
    carries a :data:`CI_SUFFIX_HINT` word or a whole ``test``/``tests``/
    ``e2e`` token. The Makefile scan reads only the diff; a package.json
    entry is confirmed against the head manifest through ``read`` (one
    read, and only when the diff adds a check-shaped key). Never raises.
    """
    out: list[CiCandidate] = []
    for file in files:
        if file.status == "removed" or file.is_binary:
            continue
        if file.path in MAKEFILE_NAMES:
            out.extend(_make_target_candidates(file))
        elif file.path == PACKAGE_JSON:
            out.extend(_npm_target_candidates(file, read))
    return out


def candidate_checks(
    files: Sequence[FileDiff],
    *,
    read: Callable[[str], str | None] | None = None,
) -> list[CiCandidate]:
    """The check-shaped files and runner targets among ``files``.

    A file that is not ``removed`` counts when it is new (any status but
    ``modified``) and its basename contains a :data:`CI_SUFFIX_HINT` word
    or it gains a shebang as its first line, or when any ``--flag`` it
    gains (on an added line, absent from the removed lines) contains such
    a word — so a modified script counts only for a new flag, and a
    body-only edit never does. A NEW file whose name says test or spec but
    sits outside every default include counts too. The
    :func:`target_candidates` follow, each on the runner file that
    defines it (``read`` is handed through for the package.json check).
    The result is sorted by path, then target, so both the findings and
    the run record are deterministic. Reads nothing but the parsed diff
    and, for a package.json target, the head manifest.
    """
    candidates: dict[str, tuple[str, bool]] = {}
    for file in files:
        if file.status == "removed":
            continue
        path = file.path
        fresh = file.status != "modified"
        if fresh:
            hint = _hint_in_tokens(_name_tokens(PurePosixPath(path).name))
            if hint is not None:
                candidates[path] = (f"its name mentions {hint!r}", True)
                continue
        dropped = {
            flag
            for hunk in file.hunks
            for ln in hunk.lines
            if ln.kind == "-"
            for flag in _FLAG_RE.findall(ln.text)
        }
        flag_hint: str | None = None
        for _, text in _added_lines(file):
            for flag in _FLAG_RE.findall(text):
                if flag in dropped:
                    continue
                flag_hint = flag_hint or _hint_in_tokens(_name_tokens(flag))
        if flag_hint is not None:
            candidates[path] = (f"it gains a --{flag_hint} flag", fresh)
            continue
        if fresh and any(
            new_line == 1 and text.startswith("#!") for new_line, text in _added_lines(file)
        ):
            candidates[path] = ("it gains a shebang line", True)
            continue
        if file.status == "added" and _looks_like_test(path) and not default_include(path):
            candidates[path] = (
                "it is a new test file outside the runner's default include",
                True,
            )
    out = [
        CiCandidate(path, reason, fresh)
        for path, (reason, fresh) in candidates.items()
    ]
    out.extend(target_candidates(files, read))
    return sorted(out, key=lambda candidate: (candidate.path, candidate.target or ("", "")))


def _literal_globs(globs: Sequence[str]) -> list[str]:
    """The globs that are plain paths, in order, deduplicated.

    Same rule as :func:`prxref.repo_contracts.literal_contract_paths`
    (restated, not imported: this module shares nothing else with the
    contract excerptor): a glob without ``*``, ``?`` or ``[`` that does
    not start with ``!`` names one repository-relative path, read directly
    even when no listing shows it — a miss costs one read.
    """
    out: dict[str, None] = {}
    for glob in globs:
        if not glob.strip() or glob.startswith("!") or any(c in glob for c in "*?["):
            continue
        if match_globs(glob, globs):
            out.setdefault(glob)
    return list(out)


def ci_config_paths(
    listing: Collection[str] | None, globs: Sequence[str],
) -> list[str]:
    """The repository's CI configuration paths: sorted, deduplicated.

    Every path of ``listing`` (the head-sha file listing, or None when the
    source cannot list) :func:`~prxref.rules.match_globs` selects with
    ``globs`` counts. A literal glob counts only when the listing shows it,
    so a literal that does not exist never spends the read budget; with no
    listing at all the glob entries — ``.github/workflows/*.y*ml`` among
    the built-ins — cannot match, and the literal entries are the fallback
    the check reads. Pure: reads neither the repository nor the diff.
    """
    selected = {path for path in (listing or ()) if path and match_globs(path, globs)}
    if listing is None:
        selected.update(_literal_globs(globs))
    return sorted(selected)


def _invocation_lines(text: str) -> list[str]:
    """Every line of ``text`` that can carry a command.

    A mini indentation scanner in the spirit of
    :class:`prxref.repo_contracts._Yaml` (restated here because that class
    is private to the contract excerptor; a shared scanner would be the
    third copy's excuse to exist): a line with a key in
    :data:`_INVOCATION_KEYS` opens a block, every deeper-indented
    non-comment line inside it is shell too, and a line that starts with a
    plain command prefix counts without any YAML around it. Blank lines
    and ``#`` comments never count, so a mention in a comment alone — the
    only line naming the script — leaves the check unwired.
    """
    out: list[str] = []
    floor: int | None = None
    for raw in text.split("\n"):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue  # a comment or blank never counts, nor closes a block
        indent = len(raw) - len(raw.lstrip(" "))
        match = _YAML_KEY_RE.match(raw)
        if match is not None:
            # The key's column sits after a list dash when there is one, so
            # GitHub's ``- run:`` and its aligned siblings compare columns.
            column = match.end("dash") if match.group("dash") else len(match.group("indent"))
            if match.group("key") in _INVOCATION_KEYS:
                floor = column
                out.append(stripped)
                continue
            floor = None  # a sibling or parent key closes the open block
        elif floor is not None and indent > floor:
            out.append(stripped)  # a line inside the open invocation block
            continue
        else:
            floor = None
        command = stripped.lstrip("- ")
        if stripped.startswith(_PLAIN_COMMAND_PREFIXES) or command.startswith(
            _PLAIN_COMMAND_PREFIXES,
        ):
            out.append(stripped)
    return out


def _mentions(line: str, path: str, basename: str) -> bool:
    """True when ``line`` names the candidate by full path or bare basename."""
    if path and path in line:
        return True
    return bool(
        re.search(rf"(?<![\w/.-]){re.escape(basename)}(?![\w.-])", line)
    )


def _make_targets(tokens: list[str]) -> set[tuple[str, str]]:
    """The ``("make", target)`` pairs one ``make`` argument list runs.

    Options are skipped (with their argument where one is required), a
    ``NAME=value`` assignment is not a target, and no target at all is
    the default goal, spelled ``""``. An option that points make at
    another directory or makefile yields nothing: the root Makefile is
    not the one it runs.
    """
    targets: set[str] = set()
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token.startswith(_MAKE_REDIRECT_OPTIONS):
            return set()
        if token in _MAKE_ARG_OPTIONS:
            skip_next = True
            continue
        if token in _MAKE_COUNT_OPTIONS:
            continue
        if token.startswith("-"):
            continue
        if token.isdigit() or "=" in token:
            continue
        targets.add(token.strip("'\""))
    return {("make", target) for target in targets} or {("make", "")}


def _npm_script(tool: str, tokens: list[str]) -> str | None:
    """The package.json script one npm, yarn or pnpm argument list runs, or None."""
    positional: list[str] = []
    for token in tokens:
        if token.startswith(_NPM_REDIRECT_OPTIONS):
            return None
        if token.startswith("-"):
            continue
        positional.append(token.strip("'\""))
    if not positional:
        return None
    verb = positional[0]
    if verb in _NPM_RUN_VERBS:
        return positional[1] if len(positional) > 1 else None
    if tool == "npm":
        return _NPM_SCRIPT_ALIASES.get(verb)
    if verb in _YARN_BUILTINS:
        return None
    return _NPM_SCRIPT_ALIASES.get(verb, verb)


def runner_targets(line: str) -> set[tuple[str, str]]:
    """The runner entries one CI invocation line runs.

    ``("make", target)`` for each ``make`` target (``""`` is the default
    goal), ``("npm", script)`` for an ``npm run``/``npm test``/``yarn``/
    ``pnpm`` script; a command chained with ``&&``, ``;`` or ``|`` is read
    segment by segment. ``cmake`` and ``$(MAKE)`` are not ``make``, and a
    runner pointed elsewhere (``make -C sub``, ``npm --prefix web``) yields
    nothing. Pure: reads only ``line``.
    """
    out: set[tuple[str, str]] = set()
    for match in _RUNNER_RE.finditer(line):
        tool, rest = match.group(1), match.group(2)
        tokens = rest.split()
        if tool == "make":
            out |= _make_targets(tokens)
            continue
        script = _npm_script(tool, tokens)
        if script:
            out.add(("npm", script))
    return out


def _make_recipes(
    text: str,
) -> tuple[dict[str, list[str]], dict[str, list[str]], str | None]:
    """The recipe lines and prerequisites per target of a Makefile, plus its default goal.

    A rule line (``a b: prereqs`` or ``a:: prereqs``, ``a: ; cmd`` with an
    inline recipe) opens the recipe its tab-indented lines fill; any other
    non-blank, non-comment line closes it. Recipe prefixes ``@``, ``-`` and
    ``+`` are dropped and a ``#`` comment recipe line never counts. A
    prerequisite is a plain name before any ``;`` (the order-only ``|``
    and any ``$`` variable reference are skipped). The default goal is
    the first target that does not start with ``.`` and has no ``%``
    pattern.
    """
    recipes: dict[str, list[str]] = {}
    prereqs: dict[str, list[str]] = {}
    default_goal: str | None = None
    current: list[str] | None = None

    def add(targets: list[str], command: str) -> None:
        command = command.strip().lstrip("@-+").strip()
        if not command or command.startswith("#"):
            return
        for target in targets:
            recipes.setdefault(target, []).append(command)

    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith("\t"):
            if current is not None:
                add(current, raw[1:])
            continue
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _MAKE_RULE_RE.match(raw)
        if match is None:
            current = None
            continue
        current = match.group("targets").split()
        for target in current:
            recipes.setdefault(target, [])
            if default_goal is None and not target.startswith(".") and "%" not in target:
                default_goal = target
        needs, semicolon, inline = match.group("rest").partition(";")
        names = [name for name in needs.split() if name != "|" and "$" not in name]
        for target in current:
            prereqs.setdefault(target, []).extend(names)
        if semicolon:
            add(current, inline)
    return recipes, prereqs, default_goal


def _npm_scripts(text: str) -> dict[str, str]:
    """The string-valued ``scripts`` entries of a package.json text; ``{}`` when malformed."""
    try:
        manifest = json.loads(text)
    except ValueError:
        return {}
    scripts = manifest.get("scripts") if isinstance(manifest, dict) else None
    if not isinstance(scripts, dict):
        return {}
    return {name: body for name, body in scripts.items() if isinstance(body, str)}


def _makefile(runners: Mapping[str, str]) -> str | None:
    """The root Makefile name ``make`` would run among ``runners``, or None."""
    return next((name for name in MAKEFILE_NAMES if name in runners), None)


def _runner_commands(
    runners: Mapping[str, str], hop: tuple[str, str],
) -> list[str]:
    """The command lines the runner entry ``hop`` runs, one runner file deep.

    For make that is the goal's recipe plus the recipes of every
    prerequisite it reaches inside the same Makefile (each target visited
    once, so a cycle terminates); a nested ``$(MAKE)`` or ``make -C`` call
    is never followed into.
    """
    tool, target = hop
    if tool == "make":
        return _make_walk(runners, target)[1]
    body = _npm_scripts(runners[PACKAGE_JSON]).get(target) if PACKAGE_JSON in runners else None
    return [body] if body else []


def _make_walk(runners: Mapping[str, str], target: str) -> tuple[set[str], list[str]]:
    """The targets ``make target`` reaches in the root Makefile, and their recipe lines.

    ``""`` is the default goal; every prerequisite inside the same
    Makefile is visited once, so a cycle terminates. No Makefile, or no
    goal, reaches nothing.
    """
    name = _makefile(runners)
    if name is None:
        return set(), []
    recipes, prereqs, default_goal = _make_recipes(runners[name])
    goal = target or default_goal
    if not goal:
        return set(), []
    commands: list[str] = []
    seen: set[str] = set()
    pending = [goal]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        commands.extend(recipes.get(current, []))
        pending.extend(prereqs.get(current, []))
    return seen, commands


def _runs_target(
    runners: Mapping[str, str] | None, hop: tuple[str, str], target: tuple[str, str],
) -> bool:
    """True when the CI runner entry ``hop`` runs the runner target ``target``.

    Directly (``make verify`` for ``("make", "verify")``), or one runner
    file deep: a make goal whose prerequisites in the same Makefile reach
    the target, or a recipe line or package.json script body ``hop`` runs
    that itself names the target (``"ci": "npm run lint && npm run
    smoke"``, or a recipe ``$(MAKE) verify``, read as ``make verify``).
    """
    if hop == target:
        return True
    if not runners:
        return False
    if hop[0] == "make" and target[0] == "make" and target[1] in _make_walk(runners, hop[1])[0]:
        return True
    return any(
        target in runner_targets(_MAKE_VAR_RE.sub("make", command))
        for command in _runner_commands(runners, hop)
    )


def invokes(
    text: str,
    path: str,
    candidate: CiCandidate,
    *,
    runners: Mapping[str, str] | None = None,
) -> bool:
    """True when the CI file ``text`` at ``path`` invokes ``candidate``.

    An invocation is an invocation line (see :func:`_invocation_lines`)
    that mentions the candidate's full diff path — ``./scripts/verify.sh``
    contains ``scripts/verify.sh`` — or its bare basename at word
    boundaries, so ``verify.sh`` alone matches too. A mention in a
    comment, a ``name:`` label or a folded-scalar mis-slice never counts.

    ``runners`` maps a root runner file name (:data:`RUNNER_FILES`) to its
    text. When given, an invocation line running a make target or an
    npm-family script (see :func:`runner_targets`) also counts when that
    target's own recipe or script body names the candidate the same way —
    one hop, never a prerequisite or a nested runner call. Pure: reads
    only the texts it is handed.

    A target candidate (``candidate.target`` set) is invoked by an
    invocation line whose :func:`runner_targets` include it, or, with
    ``runners``, by one whose runner entry reaches it one runner file deep
    (see :func:`_runs_target`); a mention of its runner file's path is
    not an invocation of the target.
    """
    lines = _invocation_lines(text)
    if candidate.target is not None:
        return any(
            _runs_target(runners, hop, candidate.target)
            for line in lines
            for hop in runner_targets(line)
        )
    basename = PurePosixPath(candidate.path).name
    if any(_mentions(line, candidate.path, basename) for line in lines):
        return True
    if not runners:
        return False
    return any(
        _mentions(command, candidate.path, basename)
        for line in lines
        for hop in runner_targets(line)
        for command in _runner_commands(runners, hop)
    )


def _hop_label(hop: tuple[str, str]) -> str:
    """The canonical command spelling of a runner entry, for the finding body."""
    tool, target = hop
    if tool == "make":
        return f"make {target}".rstrip()
    return f"npm run {target}"


def _read_runners(
    hops: set[tuple[str, str]],
    read: Callable[[str], str | None],
    listing: Collection[str] | None,
) -> dict[str, str]:
    """The runner files ``hops`` need, read through ``read``; never raises.

    Only the Makefile when a make hop exists, only package.json when an
    npm hop does; a name the listing does not show is never read, and the
    Makefile names stop at the first one the source returns. A read that
    raises or returns a non-string is a miss.
    """
    tools = {tool for tool, _ in hops}
    wanted: list[tuple[str, ...]] = []
    if "make" in tools:
        wanted.append(MAKEFILE_NAMES)
    if "npm" in tools:
        wanted.append((PACKAGE_JSON,))
    present = set(listing) if listing is not None else None
    out: dict[str, str] = {}
    for names in wanted:
        for name in names:
            if present is not None and name not in present:
                continue
            try:
                text = read(name)
            except Exception:  # noqa: BLE001
                text = None
            if isinstance(text, str):
                out[name] = text
                break
    return out


def mentions_ci_work(text: str | None) -> bool:
    """True when ``text`` mentions regression checks, CI, pipelines or automated tests."""
    return bool(text and _SPEC_TICKET_RE.search(text))


def ci_wiring_findings(
    files: Sequence[FileDiff],
    *,
    read: Callable[[str], str | None],
    listing: Collection[str] | None,
    globs: Sequence[str] = (),
    ticket_text: str | None = None,
) -> tuple[list[Finding], dict]:
    """Flag every candidate no CI configuration file invokes; never raises.

    ``read`` is the orchestrator's uncapped head-sha read (a None return
    is a miss and is skipped); ``listing`` the repository path listing or
    None; ``globs`` the effective CI-file globs, where empty falls back to
    :data:`DEFAULT_CI_GLOBS` (the replace-not-append rule: an operator
    value replaces the built-in set, and an empty one reads as unset).
    ``ticket_text`` is the ``--context-file`` ticket's text (None when
    there is no ticket; the orchestrator also raises the severity for a
    ``--spec`` source that matches, once the specs are fetched): matching :data:`_SPEC_TICKET_RE` makes the
    findings ``spec``, else they are ``warning`` — spec grounding still
    relabels a ``spec`` to ``warning`` on an ungrounded run, the desired
    safe default.

    Returns the findings — one per unwired candidate, file-level
    (``line=0``) on the candidate's own path, confidence 1.0, sorted by
    path, body naming the CI files searched and ending with the
    deterministic suffix — and the run-record stamp
    ``{"candidates", "ci_files", "picked_up_default", "triggered"}``
    (``triggered`` exactly when a finding was raised). At most
    :data:`MAX_CI_FILES` CI files are read, and none when there is no
    candidate. When a CI invocation line runs a make target or an
    npm-family script and some candidate is not invoked directly, the root
    runner files it needs (:data:`RUNNER_FILES`, at most one Makefile and
    one package.json) are read too and followed one hop; the body lists
    each one read after the CI files, naming the commands it was followed
    from, while ``ci_files`` stays the CI files alone. A runner target the
    PR adds (see :func:`target_candidates`) is a candidate too: its
    finding sits on the root Makefile or package.json, titled with the
    command that would run it (``make verify``, ``npm run smoke``), and a
    package.json in the diff is read once to confirm the entry is a
    script — the same read the runner hop reuses. Each path is read at
    most once per run. Deterministic: no model, no randomness, the only
    I/O the ``read`` callable.
    """
    cache: dict[str, str | None] = {}
    source = read

    def read_once(path: str) -> str | None:
        if path not in cache:
            cache[path] = source(path)
        return cache[path]

    read = read_once
    candidates = candidate_checks(files, read=read)
    picked_up_default = sorted({
        file.path for file in files
        if file.status == "added" and _looks_like_test(file.path)
        and default_include(file.path)
    })
    if not candidates:
        return [], {
            "candidates": [],
            "ci_files": [],
            "picked_up_default": picked_up_default,
            "triggered": False,
        }

    effective = [glob for glob in globs if glob.strip()] or list(DEFAULT_CI_GLOBS)
    ci_files = ci_config_paths(listing, effective)[:MAX_CI_FILES]
    texts = [
        (ci_path, text)
        for ci_path in ci_files
        if isinstance(text := read(ci_path), str)
    ]
    read_files = [ci_path for ci_path, _ in texts]

    runners: dict[str, str] = {}
    hops = {
        hop
        for _, text in texts
        for line in _invocation_lines(text)
        for hop in runner_targets(line)
    }
    if hops and not all(
        any(invokes(text, ci_path, candidate) for ci_path, text in texts)
        for candidate in candidates
    ):
        runners = _read_runners(hops, read, listing)

    severity = "spec" if mentions_ci_work(ticket_text) else "warning"
    searched_lines = [f"- `{ci_path}`" for ci_path in read_files]
    for name in sorted(runners):
        tool = "make" if name in MAKEFILE_NAMES else "npm"
        followed = ", ".join(
            f"`{_hop_label(hop)}`" for hop in sorted(hops) if hop[0] == tool
        )
        searched_lines.append(f"- `{name}` (followed from {followed})")
    searched = (
        "\n".join(searched_lines)
        if searched_lines
        else "- (no CI configuration file was found)"
    )
    findings: list[Finding] = []
    for candidate in candidates:
        verb = "added" if candidate.new else "changed"
        if any(
            invokes(text, ci_path, candidate, runners=runners)
            for ci_path, text in texts
        ):
            continue
        if candidate.target is None:
            subject = f"{'adds' if candidate.new else 'changes'} `{candidate.path}`"
        else:
            kind = "target" if candidate.target[0] == "make" else "script"
            subject = f"adds the `{candidate.target[1]}` {kind} to `{candidate.path}`"
        findings.append(Finding(
            file=candidate.path,
            line=0,
            severity=severity,
            confidence=1.0,
            title=f"`{candidate.label}` is {verb} but no CI job runs it",
            body=(
                f"This PR {subject}, and {candidate.reason}, but no "
                f"CI configuration file the run could read invokes it, so the "
                f"check runs only when someone remembers to run it locally. CI "
                f"files searched:\n{searched}\nWire the check into CI (a workflow "
                f"`run:` step, a `script:` entry, a make or npm target) or drop "
                f"it from the ticket's acceptance.{_BODY_SUFFIX}"
            ),
        ))
    record = {
        "candidates": [
            candidate.path if candidate.target is None
            else f"{candidate.path} ({candidate.label})"
            for candidate in candidates
        ],
        "ci_files": read_files,
        "picked_up_default": picked_up_default,
        "triggered": bool(findings),
    }
    return findings, record
