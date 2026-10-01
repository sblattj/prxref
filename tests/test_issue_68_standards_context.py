"""Issue #68: in-repo standards documents as chunk context.

``prxref.repo_standards`` slices the repository's own standards documents
(``docs/standards/**``, ``docs/adr/**``, ``SECURITY.md``, ``CONTRIBUTING.md``,
``docs/CONTRIBUTING.md`` by default) into Markdown sections and offers the
ones a chunk's own changes point at to the chunk worker at the ``repo``
level, as entries of kind ``standards`` under one prompt block. What is
pinned here:

- the pure slicing: headings of level ``N`` close sections of level ``N``
  or deeper, so a ``##`` section owns its ``###`` children and ends at the
  next ``##`` or ``#``; fenced code, pre-heading text and front matter
  open nothing;
- the pure triggers: the chunk's path atoms and the quoted string literals
  of its added lines, whole and — for a ``key=value`` literal — the key
  half too, because the value is exactly what the diff gets to disagree
  with; a literal shorter than four characters matches nothing;
- the pure selection: a section qualifies when a trigger appears in it, a
  heading hit outranks body hits, ties break on ``(path, line)``, the
  per-chunk budget admits in rank order and closes the last admitted entry
  with an omitted marker, a Superseded or Rejected ADR is annotated, at
  most six files are read priority-first, and a read that returns None is
  skipped silently;
- the disagreement note: when two admitted sections state different values
  for the same setting, the first entry carries a ``[note]`` line — unless
  the budget left the disagreeing section out;
- the flow through the REAL reviewer: the middleware chunk that writes
  ``max-age=15552000`` is shown the repo's ``max-age=63072000`` standard
  with its ``path:line:`` citation, a Dockerfile chunk is shown the
  deployment section that names Dockerfiles, an unrelated chunk sees no
  block at all, and a document the globs miss is never read;
- the off switch: the exact env value ``off`` empties the glob set — so no
  standards file is read and no block renders — while an empty or
  whitespace-only value reads as unset and keeps the built-in set;
- the record: the initial ``repo_context`` row carries the glob set and
  the per-chunk budget that actually ran.
"""
from __future__ import annotations

import json
import threading
from collections import Counter
from pathlib import Path

import pytest

from prxref import cli, config, orchestrator
from prxref.chunk_context import STANDARDS_HEADER
from prxref.config import load_config
from prxref.forges.base import PathListing
from prxref.forges.repo_dir import RepoDir
from prxref.llm import InvokeResult
from prxref.repo_standards import (
    MAX_STANDARDS_FILES,
    split_sections,
    standards_entries,
    standards_triggers,
)
from prxref.triage import parse_unified_diff
from tests.test_orchestrator import REF, FakeForge

# The repo's standard pins a two-year lifetime; the diff under review sets
# a six-month one. Line 12 is the section heading the entry must cite.
WEB_SECURITY = "\n".join([
    "# Web security standards",
    "",
    "Standards every ACME service must meet before it ships.",
    "",
    "## Cookies",
    "",
    "Cookies are Secure, HttpOnly and SameSite=Strict.",
    "",
    "## CORS",
    "Origins are allow-listed, never wildcarded.",
    "",
    "## HSTS",  # line 12
    "",
    "Every public endpoint sends Strict-Transport-Security for two full",
    "years; anything shorter is a downgrade. The exact value on the wire:",
    "max-age=63072000; includeSubDomains; preload.",
])

DEPLOYMENT = "\n".join([
    "# Deployment standards",
    "",
    "## Building",
    "",
    "Images are built by CI, never on a developer laptop.",
    "",
    "## Runtime",  # line 7
    "",
    "The Dockerfile must pin its base image by digest and run as a",
    "non-root user; a floating tag is a release blocker.",
])

# Two same-level sections stating different HSTS lifetimes: the oldest
# context-relevance trap there is, and the one the [note] exists for.
TLS_POLICY = "\n".join([
    "## Old policy",
    "",
    "The HSTS lifetime max-age=15552000 is kept for history.",
    "",
    "## New policy",
    "",
    "The HSTS lifetime max-age=63072000 is the current standard.",
])

SUPERSEDED_ADR = "\n".join([
    "---",
    "status: superseded",
    "---",
    "",
    "# 0007: one migration per PR",
    "",
    "Deploy exactly one migration per pull request.",
])

REJECTED_ADR = "\n".join([
    "# 0009: shared database",
    "",
    "Status: Rejected — every service keeps its own schema.",
    "",
    "The shared-database pattern was rejected in favor of schemas per service.",
])

HSTS_DIFF = (
    "diff --git a/app/middleware.py b/app/middleware.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/app/middleware.py\n"
    "@@ -0,0 +1,3 @@\n"
    "+def add_security_headers(response):\n"
    '+    response.headers["Strict-Transport-Security"] = "max-age=15552000"\n'
    "+    return response\n"
)

HELPER_DIFF = (
    "diff --git a/tools/helper.py b/tools/helper.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/tools/helper.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def helper():\n"
    "+    return 7\n"
)

TWO_CHUNK_DIFF = HSTS_DIFF + HELPER_DIFF

DOCKERFILE_DIFF = (
    "diff --git a/Dockerfile b/Dockerfile\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/Dockerfile\n"
    "@@ -0,0 +1,2 @@\n"
    "+FROM python:3.12-slim\n"
    "+USER app\n"
)

STANDARDS_DOC = "docs/standards/web-security.md"
DEPLOYMENT_DOC = "docs/standards/deployment.md"
MISSED_DOC = "docs/notes/old-notes.md"
STANDARD_GLOBS = ("docs/standards/**",)
BUILTIN_GLOBS = list(config._DEFAULTS["context_standards_globs"])
NO_FINDINGS = '{"findings": []}'


def _thing_diff(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1,2 @@\n"
        "+thing = 1\n"
        "+other = 2\n"
    )


class TestSectionSlicing:
    NESTED = "\n".join([
        "# Top",
        "",
        "intro",
        "",
        "## Alpha",
        "",
        "alpha body",
        "",
        "### Alpha child",
        "",
        "child body",
        "",
        "## Beta",
        "",
        "beta body",
    ])

    def test_a_section_owns_its_deeper_children_and_ends_at_its_own_level(self):
        sections = split_sections("doc.md", self.NESTED)

        assert [(s.heading, s.line) for s in sections] == [
            ("Top", 1), ("Alpha", 5), ("Alpha child", 9), ("Beta", 13),
        ]
        alpha = sections[1]
        assert "child body" in alpha.text  # the ### child rides inside ## Alpha
        assert alpha.text.startswith("## Alpha\n")
        child = sections[2].text
        assert child.startswith("### Alpha child") and "child body" in child
        assert "## Beta" not in child  # the child ends where Beta opens it
        assert sections[3].text == "## Beta\n\nbeta body"

    def test_a_fenced_hash_opens_nothing(self):
        doc = "\n".join(["## Real", "", "```sh", "# not a heading", "```", "", "tail"])
        sections = split_sections("doc.md", doc)

        assert [s.heading for s in sections] == ["Real"]
        assert "# not a heading" in sections[0].text

    def test_front_matter_and_pre_heading_text_open_nothing(self):
        doc = "\n".join(["---", "status: draft", "---", "", "lead text", "", "## First"])
        sections = split_sections("doc.md", doc)

        assert [s.heading for s in sections] == ["First"]
        assert "lead text" not in sections[0].text


class TestTriggers:
    def test_quoted_literals_count_whole_and_the_key_half_of_a_pair(self):
        line = '    headers["Strict-Transport-Security"] = "max-age=15552000"'
        triggers = standards_triggers(_chunk("app/middleware.py", [line]))

        assert "stricttransportsecurity" in triggers
        assert "maxage=15552000" in triggers
        assert "maxage" in triggers

    def test_short_literals_and_path_atoms_match_nothing(self):
        triggers = standards_triggers(_chunk("src/go/a/mod.py", ["    flag = 'on'"]))

        assert "on" not in triggers
        assert "go" not in triggers and "py" not in triggers and "src" not in triggers

    def test_path_atoms_fold_the_changed_files_own_names(self):
        triggers = standards_triggers(parse_unified_diff(DOCKERFILE_DIFF))

        assert "dockerfile" in triggers


def _chunk(path: str, added: list[str]) -> list[object]:
    """A one-file chunk through the real parser, carrying exactly ``added`` as its ``+`` lines."""
    body = "".join(f"+{line}\n" for line in added)
    diff = (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(added)} @@\n"
        f"{body}"
    )
    return parse_unified_diff(diff)


class TestStandardsEntries:
    def test_the_standard_a_change_contradicts_is_cited_by_path_and_line(self):
        entries = standards_entries(
            parse_unified_diff(HSTS_DIFF),
            standards_paths=[STANDARDS_DOC],
            read=lambda path: {STANDARDS_DOC: WEB_SECURITY}.get(path),
        )

        hsts = [e for e in entries if e.line == 12]
        assert len(hsts) == 1
        entry = hsts[0]
        assert (entry.path, entry.symbol, entry.kind, entry.reason) == (
            STANDARDS_DOC, "HSTS", "standards", "standard",
        )
        assert "max-age=63072000" in entry.text
        assert entry.rendered().startswith(f"{STANDARDS_DOC}:12: ## HSTS")

    def test_two_sections_disagreeing_on_one_setting_earn_a_note_on_the_first(self):
        chunk = _chunk("ops/tls.py", ['    header = "max-age=15552000"'])
        entries = standards_entries(
            chunk,
            standards_paths=["docs/standards/tls-policy.md"],
            read=lambda path: {"docs/standards/tls-policy.md": TLS_POLICY}.get(path),
        )

        assert [e.line for e in entries] == [1, 5]
        assert entries[0].text.startswith(
            "[note] two standards in this repo disagree on max-age: "
            "15552000 vs 63072000\n## Old policy"
        )
        assert "[note]" not in entries[1].text

    def test_the_budget_closes_the_last_admitted_entry_with_an_omitted_marker(self):
        read = {"docs/standards/tls-policy.md": TLS_POLICY}
        chunk = _chunk("ops/tls.py", ['    header = "max-age=15552000"'])
        full = standards_entries(
            chunk, standards_paths=list(read), read=read.get,
        )
        first_size = len(full[0].rendered())

        entries = standards_entries(
            chunk, standards_paths=list(read), read=read.get, max_chars=first_size + 5,
        )

        assert len(entries) == 1
        assert entries[0].text.endswith("\n… 1 more standards sections omitted")
        # The section the budget left out cannot disagree with anything.
        assert "[note]" not in entries[0].text

    def test_a_budget_that_fits_nothing_gives_no_entries_and_no_marker(self):
        entries = standards_entries(
            parse_unified_diff(HSTS_DIFF),
            standards_paths=[STANDARDS_DOC],
            read=lambda path: {STANDARDS_DOC: WEB_SECURITY}.get(path),
            max_chars=10,
        )

        assert entries == []

    def test_no_reader_no_paths_a_dead_budget_or_no_triggers_read_nothing(self):
        calls: list[str] = []

        def read(path):
            calls.append(path)
            return WEB_SECURITY

        chunk = parse_unified_diff(HSTS_DIFF)
        quiet = _chunk("a/b.c", ["    x = 1"])
        assert standards_entries(chunk, standards_paths=[STANDARDS_DOC], read=None) == []
        assert standards_entries(chunk, standards_paths=[], read=read) == []
        assert standards_entries(chunk, standards_paths=[STANDARDS_DOC], read=read, max_chars=0) == []
        assert standards_entries(quiet, standards_paths=[STANDARDS_DOC], read=read) == []
        assert calls == []

    def test_a_read_that_fails_is_skipped_silently(self):
        entries = standards_entries(
            parse_unified_diff(HSTS_DIFF),
            standards_paths=["gone.md", STANDARDS_DOC],
            read=lambda path: None if path == "gone.md" else {STANDARDS_DOC: WEB_SECURITY}.get(path),
        )

        assert {e.path for e in entries} == {STANDARDS_DOC}
        assert 12 in [e.line for e in entries]  # the HSTS section survived the failed read

    def test_at_most_six_files_are_read_priority_paths_first(self):
        paths = [f"a{i}.md" for i in range(1, 9)]
        calls: list[str] = []

        def read(path):
            calls.append(path)
            return "# Doc\n\n## Thing cap\n\nbody thing\n"

        standards_entries(
            _chunk("tools/thing.py", ["thing = 1", "other = 2"]),
            standards_paths=paths, read=read, priority=["a8.md"],
        )

        assert calls == ["a8.md", "a1.md", "a2.md", "a3.md", "a4.md", "a5.md"]
        assert len(calls) == MAX_STANDARDS_FILES

    def test_a_superseded_adr_is_annotated_in_heading_and_symbol(self):
        sections = split_sections("docs/adr/0007-one-migration.md", SUPERSEDED_ADR)

        assert [s.heading for s in sections] == [
            "0007: one migration per PR (status: Superseded)",
        ]
        assert sections[0].text.startswith("# 0007: one migration per PR (status: Superseded)")

        entries = standards_entries(
            _chunk("db/changelog/004.sql", ['    run("migration", "up")']),
            standards_paths=["docs/adr/0007-one-migration.md"],
            read=lambda path: SUPERSEDED_ADR,
        )

        assert entries[0].symbol.endswith("(status: Superseded)")
        assert "one migration per pull request" in entries[0].text

    def test_a_rejected_adr_stated_in_the_body_is_annotated_too(self):
        sections = split_sections("docs/adr/0009-shared-db.md", REJECTED_ADR)

        assert sections[0].heading == "0009: shared database (status: Rejected)"


# ----------------------------------------------------------------- the reviewer


class _RepoForge(FakeForge):
    """FakeForge over a diff, reading and listing a temp tree, counting reads per path."""

    def __init__(self, diff: str, root: Path):
        super().__init__(diff=diff)
        self.root = Path(root)
        self.content_calls: Counter[str] = Counter()
        self._lock = threading.Lock()

    def get_file_content(self, ref, path, *, sha):
        with self._lock:
            self.content_calls[path] += 1
        target = self.root / path
        return target.read_text(encoding="utf-8") if target.is_file() else None

    def list_paths(self, ref, *, sha):
        paths, complete = RepoDir(self.root).list_files()
        return PathListing(paths=paths, complete=complete)


class _RecordingLLM:
    """Records every ``(system, user)`` prompt."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        with self._lock:
            self.calls.append((system, user))
        return InvokeResult(
            text=NO_FINDINGS, input_tokens=10, output_tokens=5,
            model="fake-model", backend="fake", elapsed_ms=1,
        )


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    monkeypatch.setattr(orchestrator, "_elapsed_ms", lambda t0: 0)


def _write(root: Path, path: str, text: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _review(forge: _RepoForge, **kwargs):
    llm = _RecordingLLM()
    kwargs.setdefault("max_files_per_chunk", 1)
    kwargs.setdefault("context_standards_globs", STANDARD_GLOBS)
    res = orchestrator.orchestrate_review(
        forge, REF, llm, post=False, repo_context="repo", **kwargs,
    )
    return res, llm


def _worker_prompts(llm: _RecordingLLM, paths: list[str]) -> dict[str, list[str]]:
    """Every worker prompt (the sweep, which runs last, excluded), grouped by the chunk's file."""
    *workers, _sweep = llm.calls
    grouped: dict[str, list[str]] = {path: [] for path in paths}
    for _system, user in workers:
        owners = [path for path in paths if f"diff --git a/{path} " in user]
        assert len(owners) == 1, owners
        grouped[owners[0]].append(user)
    return grouped


class TestTheReviewerSeesTheStandards:
    def test_a_chunk_that_contradicts_the_standard_is_shown_it_with_a_citation(
        self, tmp_path,
    ):
        _write(tmp_path, STANDARDS_DOC, WEB_SECURITY)
        res, llm = _review(_RepoForge(TWO_CHUNK_DIFF, tmp_path))

        prompts = _worker_prompts(llm, ["app/middleware.py", "tools/helper.py"])
        prompt = prompts["app/middleware.py"][0]
        assert STANDARDS_HEADER in prompt
        assert f"{STANDARDS_DOC}:12: ## HSTS" in prompt
        assert "max-age=63072000" in prompt
        assert "max-age=15552000" in prompt  # the diff's own value, beside the standard
        assert res["repo_context"]["standards_globs"] == list(STANDARD_GLOBS)
        assert res["repo_context"]["standards_max_chars"] == 6000
        rows = res["repo_context"]["units"]["chunks"]
        cited = [
            (e["path"], e["line"])
            for row in rows for e in row["entries"] if e["kind"] == "standards"
        ]
        assert cited  # something was admitted
        assert (STANDARDS_DOC, 12) in cited
        assert {path for path, _line in cited} == {STANDARDS_DOC}

    def test_the_built_in_globs_reach_a_dot_github_security_doc(self, tmp_path):
        _write(tmp_path, ".github/SECURITY.md", WEB_SECURITY)
        res, llm = _review(
            _RepoForge(TWO_CHUNK_DIFF, tmp_path), context_standards_globs=BUILTIN_GLOBS,
        )

        prompts = _worker_prompts(llm, ["app/middleware.py", "tools/helper.py"])
        prompt = prompts["app/middleware.py"][0]
        assert STANDARDS_HEADER in prompt
        assert ".github/SECURITY.md:12: ## HSTS" in prompt
        assert res["repo_context"]["standards_max_chars"] == 6000

    def test_an_unrelated_chunk_in_the_same_run_sees_no_standards_block(self, tmp_path):
        _write(tmp_path, STANDARDS_DOC, WEB_SECURITY)
        _, llm = _review(_RepoForge(TWO_CHUNK_DIFF, tmp_path))

        prompts = _worker_prompts(llm, ["app/middleware.py", "tools/helper.py"])
        assert STANDARDS_HEADER in prompts["app/middleware.py"][0]
        for prompt in prompts["tools/helper.py"]:
            assert STANDARDS_HEADER not in prompt

    def test_a_dockerfile_chunk_is_shown_the_deployment_section_that_names_it(
        self, tmp_path,
    ):
        _write(tmp_path, DEPLOYMENT_DOC, DEPLOYMENT)
        _, llm = _review(_RepoForge(DOCKERFILE_DIFF, tmp_path))

        (prompt,) = _worker_prompts(llm, ["Dockerfile"])["Dockerfile"]
        assert STANDARDS_HEADER in prompt
        assert f"{DEPLOYMENT_DOC}:7: ## Runtime" in prompt

    def test_a_document_the_globs_miss_is_never_read(self, tmp_path):
        _write(tmp_path, STANDARDS_DOC, WEB_SECURITY)
        # Would match the HSTS triggers if it were ever read.
        _write(tmp_path, MISSED_DOC, "## HSTS\n\nmax-age=1\n")
        forge = _RepoForge(HSTS_DIFF, tmp_path)
        _review(forge)

        assert forge.content_calls[MISSED_DOC] == 0
        assert forge.content_calls[STANDARDS_DOC] >= 1

    def test_an_empty_glob_set_reads_nothing_and_renders_no_block(self, tmp_path):
        _write(tmp_path, STANDARDS_DOC, WEB_SECURITY)
        forge = _RepoForge(HSTS_DIFF, tmp_path)
        res, llm = _review(forge, context_standards_globs=[])

        assert forge.content_calls[STANDARDS_DOC] == 0
        (prompt,) = _worker_prompts(llm, ["app/middleware.py"])["app/middleware.py"]
        assert STANDARDS_HEADER not in prompt
        assert res["repo_context"]["standards_globs"] == []


class TestTheOffSentinel:
    """``PRXREF_CONTEXT_STANDARDS_GLOBS=off``: the one exact way to switch this off alone."""

    def test_the_exact_value_empties_the_glob_set(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CONTEXT_STANDARDS_GLOBS", "off")

        assert load_config()["context_standards_globs"] == []

    def test_surrounding_whitespace_still_reads_as_the_sentinel(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CONTEXT_STANDARDS_GLOBS", " off ")

        assert load_config()["context_standards_globs"] == []

    def test_anything_else_is_just_a_glob(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CONTEXT_STANDARDS_GLOBS", "docs/standards/**,off")

        assert load_config()["context_standards_globs"] == ["docs/standards/**", "off"]

    def test_an_empty_value_reads_as_unset_and_keeps_the_builtin_set(self, monkeypatch):
        for raw in ("", " ", "\t"):
            monkeypatch.setenv("PRXREF_CONTEXT_STANDARDS_GLOBS", raw)
            assert load_config()["context_standards_globs"] == BUILTIN_GLOBS

    def test_the_sentinel_only_governs_the_globs_not_the_budget(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CONTEXT_STANDARDS_GLOBS", "off")
        monkeypatch.setenv("PRXREF_CONTEXT_STANDARDS_MAX_CHARS", "2500")

        cfg = load_config()
        assert cfg["context_standards_globs"] == []
        assert cfg["context_standards_max_chars"] == 2500


class TestTheConfigFileTurnsStandardsOff:
    """A ``.prxref.toml`` can switch discovery off with ``[]`` or ``"off"``."""

    @pytest.mark.parametrize("literal", ["[]", '"off"'])
    def test_an_empty_array_or_off_empties_the_glob_set(self, tmp_path, literal):
        path = tmp_path / ".prxref.toml"
        path.write_text(f"context_standards_globs = {literal}\n")

        cfg = load_config(config_file=path)

        assert cfg["context_standards_globs"] == []

    def test_a_non_empty_array_still_replaces_the_set(self, tmp_path):
        path = tmp_path / ".prxref.toml"
        path.write_text('context_standards_globs = ["docs/x/**"]\n')

        assert load_config(config_file=path)["context_standards_globs"] == ["docs/x/**"]

    def test_any_other_string_is_still_a_config_error(self, tmp_path):
        path = tmp_path / ".prxref.toml"
        path.write_text('context_standards_globs = "docs/**"\n')

        with pytest.raises(config.ConfigError, match="array of strings"):
            load_config(config_file=path)


class TestAZeroBudgetDisablesTheStandards:
    def test_env_zero_loads_as_zero(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CONTEXT_STANDARDS_MAX_CHARS", "0")

        assert load_config()["context_standards_max_chars"] == 0

    def test_a_negative_budget_is_still_a_config_error(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CONTEXT_STANDARDS_MAX_CHARS", "-1")

        with pytest.raises(config.ConfigError):
            load_config()

    def test_a_review_with_budget_zero_reads_no_standards_document(self, tmp_path):
        _write(tmp_path, STANDARDS_DOC, WEB_SECURITY)
        forge = _RepoForge(TWO_CHUNK_DIFF, tmp_path)
        _, llm = _review(forge, context_standards_max_chars=0)

        assert forge.content_calls[STANDARDS_DOC] == 0
        for prompts in _worker_prompts(llm, ["app/middleware.py", "tools/helper.py"]).values():
            for prompt in prompts:
                assert STANDARDS_HEADER not in prompt

    def test_the_same_review_with_the_default_budget_reads_and_renders_it(self, tmp_path):
        _write(tmp_path, STANDARDS_DOC, WEB_SECURITY)
        forge = _RepoForge(TWO_CHUNK_DIFF, tmp_path)
        _, llm = _review(forge)

        assert forge.content_calls[STANDARDS_DOC] > 0
        prompts = _worker_prompts(llm, ["app/middleware.py", "tools/helper.py"])
        assert STANDARDS_HEADER in prompts["app/middleware.py"][0]


class TestStandardsRunWithoutRepoContext:
    """OD2: standards discovery is on by default, decoupled from ``PRXREF_REPO_CONTEXT``.

    Any reader and a non-empty glob set with a budget above 0 is enough; the
    repository-context level only adds the other sources.
    """

    def _review(self, forge, **kwargs):
        llm = _RecordingLLM()
        kwargs.setdefault("max_files_per_chunk", 1)
        kwargs.setdefault("context_standards_globs", STANDARD_GLOBS)
        res = orchestrator.orchestrate_review(forge, REF, llm, post=False, **kwargs)
        return res, llm

    def test_the_default_repo_context_still_shows_the_standard(self, tmp_path):
        _write(tmp_path, STANDARDS_DOC, WEB_SECURITY)
        res, llm = self._review(_RepoForge(TWO_CHUNK_DIFF, tmp_path))

        prompts = _worker_prompts(llm, ["app/middleware.py", "tools/helper.py"])
        prompt = prompts["app/middleware.py"][0]
        assert STANDARDS_HEADER in prompt
        assert f"{STANDARDS_DOC}:12: ## HSTS" in prompt
        for other in prompts["tools/helper.py"]:
            assert STANDARDS_HEADER not in other
        record = res["repo_context"]
        assert record["mode"] == "standards"
        assert record["standards_globs"] == list(STANDARD_GLOBS)
        kinds = {
            e["kind"] for row in record["units"]["chunks"] for e in row["entries"]
        }
        assert kinds == {"standards"}

    def test_the_diff_level_shows_the_standard_too(self, tmp_path):
        _write(tmp_path, STANDARDS_DOC, WEB_SECURITY)
        res, llm = self._review(_RepoForge(HSTS_DIFF, tmp_path), repo_context="diff")

        (prompt,) = _worker_prompts(llm, ["app/middleware.py"])["app/middleware.py"]
        assert f"{STANDARDS_DOC}:12: ## HSTS" in prompt
        assert res["repo_context"]["mode"] == "diff"

    def test_an_empty_glob_set_leaves_the_off_run_untouched(self, tmp_path):
        _write(tmp_path, STANDARDS_DOC, WEB_SECURITY)
        forge = _RepoForge(HSTS_DIFF, tmp_path)
        res, llm = self._review(forge, context_standards_globs=())

        assert forge.content_calls[STANDARDS_DOC] == 0
        (prompt,) = _worker_prompts(llm, ["app/middleware.py"])["app/middleware.py"]
        assert STANDARDS_HEADER not in prompt
        assert res["repo_context"] is None

    def test_a_zero_budget_leaves_the_off_run_untouched(self, tmp_path):
        _write(tmp_path, STANDARDS_DOC, WEB_SECURITY)
        forge = _RepoForge(HSTS_DIFF, tmp_path)
        res, _ = self._review(forge, context_standards_max_chars=0)

        assert forge.content_calls[STANDARDS_DOC] == 0
        assert res["repo_context"] is None

    def test_a_repo_without_standards_documents_records_nothing(self, tmp_path):
        _write(tmp_path, "README.md", "# hello\n")
        res, llm = self._review(_RepoForge(HSTS_DIFF, tmp_path))

        assert res["repo_context"] is None
        (prompt,) = _worker_prompts(llm, ["app/middleware.py"])["app/middleware.py"]
        assert STANDARDS_HEADER not in prompt

    def test_a_forge_without_a_reader_records_nothing(self):
        llm = _RecordingLLM()
        res = orchestrator.orchestrate_review(
            FakeForge(diff=HSTS_DIFF), REF, llm, post=False,
            context_standards_globs=STANDARD_GLOBS,
        )

        assert res["repo_context"] is None
        assert all(STANDARDS_HEADER not in user for _s, user in llm.calls)

    def test_the_default_config_reads_the_built_in_globs_at_repo_context_off(self):
        cfg = load_config()

        assert cfg["repo_context"] == "off"
        assert cfg["context_standards_globs"] == BUILTIN_GLOBS
        assert cfg["context_standards_max_chars"] > 0


# ------------------------------------------------- the citation survives the gates


CITING_FINDING = {
    "file": "app/middleware.py", "line": 2, "severity": "error", "confidence": 0.9,
    "title": "HSTS lifetime below the standard",
    "body": (
        f"max-age=15552000 contradicts {STANDARDS_DOC}:12 (## HSTS), which "
        "requires max-age=63072000; the shorter lifetime is a security downgrade."
    ),
}


class _FindingLLM(_RecordingLLM):
    """Answers each chunk worker with the citing finding; the first ``timeouts`` raise a deadline error."""

    def __init__(self, timeouts: int = 0):
        super().__init__()
        self.timeouts = timeouts

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        is_worker = "### Diff" in user
        with self._lock:
            self.calls.append((system, user))
            timed_out = is_worker and self.timeouts > 0
            if timed_out:
                self.timeouts -= 1
        if timed_out:
            raise TimeoutError("fake-model: timeout after 60s")
        findings = [CITING_FINDING] if is_worker else []
        return InvokeResult(
            text=json.dumps({"findings": findings, "escalations": []}),
            input_tokens=10, output_tokens=5, model="fake-model", backend="fake", elapsed_ms=1,
        )


class TestTheCitationSurvivesTheGates:
    def test_a_finding_citing_the_standard_reaches_the_comment_and_the_json_row(
        self, tmp_path,
    ):
        _write(tmp_path, STANDARDS_DOC, WEB_SECURITY)
        forge = _RepoForge(HSTS_DIFF, tmp_path)
        res = orchestrator.orchestrate_review(
            forge, REF, _FindingLLM(), post=True, repo_context="repo",
            max_files_per_chunk=1, context_standards_globs=STANDARD_GLOBS,
        )

        citation = f"{STANDARDS_DOC}:12"
        assert [f.title for f in res["findings_active"]] == [CITING_FINDING["title"]]
        assert citation in res["findings_active"][0].body
        (batch,) = forge.inline_batches
        assert [c.path for c in batch] == ["app/middleware.py"]
        assert citation in batch[0].body
        rows = cli._build_json_result(res)["findings"]
        assert [r["drop_reason"] for r in rows] == [None]
        assert citation in rows[0]["body"]

    def test_the_timeout_retry_drops_the_standards_block(self, tmp_path):
        _write(tmp_path, STANDARDS_DOC, WEB_SECURITY)
        llm = _FindingLLM(timeouts=1)
        res = orchestrator.orchestrate_review(
            _RepoForge(HSTS_DIFF, tmp_path), REF, llm, post=False,
            repo_context="repo", max_files_per_chunk=1,
            context_standards_globs=STANDARD_GLOBS,
        )

        workers = [user for _system, user in llm.calls if "### Diff" in user]
        assert len(workers) == 2
        first, retry = workers
        assert STANDARDS_HEADER in first
        assert STANDARDS_HEADER not in retry
        assert res["chunks_failed"] == 0
        (row,) = res["repo_context"]["units"]["chunks"]
        assert row["retry_dropped"] is True
