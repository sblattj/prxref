"""Deterministic, opt-in CI-wiring check (issue #66).

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
opt-in — ``ci_wiring = "on"`` — and never changes the verdict: a finding
is ``spec`` when the ticket mentions regression checks, CI, pipelines or
automated tests (relabeled ``warning`` by spec grounding on an ungrounded
run) and ``warning`` otherwise.

A CI step that runs the check through a runner is followed exactly one hop:
``make verify`` reads the root Makefile (``GNUmakefile``, ``makefile``,
``Makefile``, GNU make's lookup order) and counts the check wired when the
``verify`` rule's own recipe names it; ``npm run verify``, ``npm test``,
``yarn verify`` and ``pnpm run verify`` do the same over the root
``package.json`` ``scripts`` entry. A runner file is read only when a CI
invocation line names a runner and some check is not invoked directly,
and only when the listing shows it (or the source cannot list).

Known false negatives, accepted for v1 and documented here rather than
hidden: an invocation embedded inside a folded YAML block scalar the
indentation scanner mis-slices, a CI job that renames the script before
running it (``cp scripts/verify.sh stage.sh``), and a runner chain deeper
than one hop — a make prerequisite (``verify: smoke``), a recipe calling
``$(MAKE) smoke``, ``make -C sub`` / ``make -f other.mk``, a workspace
``npm --prefix web run verify``, or a ``justfile``/``Taskfile``/``tox.ini``
runner. The conservative direction is under-flagging.
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


@dataclass(frozen=True)
class CiCandidate:
    """One check-shaped file the PR adds or changes.

    ``path`` is the diff path; ``reason`` is the human phrase naming why
    the file counts (which hint matched, the shebang, or a test file
    outside the runner's default include) — it rides the finding body and
    the run record unchanged.
    """

    path: str
    reason: str
    new: bool = True


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


def candidate_checks(files: Sequence[FileDiff]) -> list[CiCandidate]:
    """The check-shaped files among ``files``.

    A file that is not ``removed`` counts when it is new (any status but
    ``modified``) and its basename contains a :data:`CI_SUFFIX_HINT` word
    or it gains a shebang as its first line, or when any ``--flag`` it
    gains (on an added line, absent from the removed lines) contains such
    a word — so a modified script counts only for a new flag, and a
    body-only edit never does. A NEW file whose name says test or spec but
    sits outside every default include counts too. The result is sorted by path, so both the findings and
    the run record are deterministic. Pure: reads only the parsed diff.
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
    return [
        CiCandidate(path, reason, fresh)
        for path, (reason, fresh) in sorted(candidates.items())
    ]


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


def _make_recipes(text: str) -> tuple[dict[str, list[str]], str | None]:
    """The recipe lines per target of a Makefile, plus its default goal.

    A rule line (``a b: prereqs`` or ``a:: prereqs``, ``a: ; cmd`` with an
    inline recipe) opens the recipe its tab-indented lines fill; any other
    non-blank, non-comment line closes it. Recipe prefixes ``@``, ``-`` and
    ``+`` are dropped and a ``#`` comment recipe line never counts. The
    default goal is the first target that does not start with ``.`` and
    has no ``%`` pattern. Prerequisites are not recorded: the follow is
    one hop.
    """
    recipes: dict[str, list[str]] = {}
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
        _, semicolon, inline = match.group("rest").partition(";")
        if semicolon:
            add(current, inline)
    return recipes, default_goal


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
    """The command lines the runner entry ``hop`` runs, one hop deep."""
    tool, target = hop
    if tool == "make":
        name = _makefile(runners)
        if name is None:
            return []
        recipes, default_goal = _make_recipes(runners[name])
        goal = target or default_goal
        return recipes.get(goal, []) if goal else []
    body = _npm_scripts(runners[PACKAGE_JSON]).get(target) if PACKAGE_JSON in runners else None
    return [body] if body else []


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
    """
    basename = PurePosixPath(candidate.path).name
    lines = _invocation_lines(text)
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
    from, while ``ci_files`` stays the CI files alone. Deterministic: no
    model, no randomness, the only I/O the ``read`` callable.
    """
    candidates = candidate_checks(files)
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
        findings.append(Finding(
            file=candidate.path,
            line=0,
            severity=severity,
            confidence=1.0,
            title=f"`{candidate.path}` is {verb} but no CI job runs it",
            body=(
                f"This PR {'adds' if candidate.new else 'changes'} `{candidate.path}`, and {candidate.reason}, but no "
                f"CI configuration file the run could read invokes it, so the "
                f"check runs only when someone remembers to run it locally. CI "
                f"files searched:\n{searched}\nWire the check into CI (a workflow "
                f"`run:` step, a `script:` entry, a make or npm target) or drop "
                f"it from the ticket's acceptance.{_BODY_SUFFIX}"
            ),
        ))
    record = {
        "candidates": [candidate.path for candidate in candidates],
        "ci_files": read_files,
        "picked_up_default": picked_up_default,
        "triggered": bool(findings),
    }
    return findings, record
