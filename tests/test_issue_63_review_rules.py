"""Issue #63: team review rules, from the file on disk to the posted verdict.

``PRXREF_REVIEW_RULES`` / ``--rules-file`` name a Markdown file whose body
reaches every chunk worker and the systemic sweep in the SYSTEM half of the
prompt, and whose optional ``severity:`` front matter maps team severity
words onto prxref's tiers. What is pinned here, with the real loader, the
real renderers and the real orchestrator (only the forge and the model are
doubles):

- the loader: what it returns, what it hashes and caps, and that every
  failure is a ``ConfigError`` naming the input that supplied the path;
- the front-matter grammar, including the exact ``<path>:<line>`` of each
  rejected line;
- the prompt block for each unit, and that it never touches the user half;
- ``quality.apply_severity_map``, and that a mapped word survives the gate
  while an unmapped one is still dropped;
- the run record on every exit, the ``--trace-dir`` system files, and the
  CLI and daemon paths end to end.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import types
from dataclasses import replace

import pytest

from prxref import cli, orchestrator
from prxref.cli import main
from prxref.llm import ConfigError, InvokeResult
from prxref.quality import SEVERITIES, apply_severity_map
from prxref.reviewer import (
    _CONTEXT_MARKER,
    NO_PROMPT_CONTEXT,
    PromptContext,
    _render_prompt,
    _render_systemic_prompt,
    load_prompt,
)
from prxref.rules import (
    MAPPABLE_SEVERITIES,
    RESERVED_SEVERITIES,
    RULES_HEADING,
    ReviewRules,
    load_review_rules,
    split_front_matter,
)
from prxref.text_inputs import cap_text
from prxref.triage import SCOPE_OUT, Finding, parse_unified_diff
from tests.test_orchestrator import REF, FakeForge, FakeLLM, _added_file_diff, multi_chunk_diff
from tests.test_run_record import PATHS, _run

SOURCES = ("--rules-file", "PRXREF_REVIEW_RULES")
CANARY = "RULE-CANARY-6363"

SKILL = (
    "---\n"
    "name: team-review\n"
    "description: |\n"
    "  The checklist every reviewer on this team uses.\n"
    "  severity: prose inside a block scalar, not the map\n"
    "severity:\n"
    "  blocker: error   # a merge blocker\n"
    '  "Must  Fix": warning\n'
    "  nit: outofscope\n"
    "---\n"
    "# Team rules\n"
    "\n"
    f"- blocker: any network call without an explicit timeout ({CANARY}).\n"
    "- nit: a TODO without a ticket id.\n"
)
BODY = (
    "# Team rules\n"
    "\n"
    f"- blocker: any network call without an explicit timeout ({CANARY}).\n"
    "- nit: a TODO without a ticket id."
)
MAP = {"blocker": "error", "must fix": "warning", "nit": "outofscope"}
MAP_PARAGRAPH = (
    "Team severity words map onto that vocabulary: `blocker` → `error`; "
    "`must fix` → `warning`; `nit` → `outofscope`. Classify a problem by the "
    "team's definition, then write the mapped word in `severity`."
)
WORKER_PHRASE = "Check this chunk against them as well."
SWEEP_PHRASE = "In this sweep, apply only the rules that concern a whole-PR or cross-file property"

FINDING = {
    "file": "src/app.py", "line": 3, "severity": "warning", "confidence": 0.9,
    "title": "Unchecked data write", "body": "The data line is written without validation.",
}

WORKER_HEAD = load_prompt("worker").partition(_CONTEXT_MARKER)[0].strip()
SWEEP_HEAD = load_prompt("systemic").partition(_CONTEXT_MARKER)[0].strip()


def _write(tmp_path, content: str | bytes, name: str = "rules.md") -> str:
    path = tmp_path / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return str(path)


def _load(tmp_path, content: str | bytes = SKILL, *, max_chars: int = 12000, name: str = "rules.md"):
    return load_review_rules(_write(tmp_path, content, name), max_chars=max_chars, source="--rules-file")


def _sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _config_error(path, *, max_chars: int = 100, source: str = "--rules-file") -> str:
    with pytest.raises(ConfigError) as exc:
        load_review_rules(path, max_chars=max_chars, source=source)
    return str(exc.value)


class TestLoader:
    @pytest.mark.parametrize("path", [None, "", "   ", "\t\n"])
    def test_unset_path_returns_none(self, path):
        assert load_review_rules(path, max_chars=100, source="PRXREF_REVIEW_RULES") is None

    def test_a_skill_file_yields_its_map_body_and_ignored_keys(self, tmp_path):
        path = _write(tmp_path, SKILL)
        rules = load_review_rules(path, max_chars=12000, source="PRXREF_REVIEW_RULES")
        assert rules.path == path
        assert dict(rules.severity_map) == MAP
        assert list(rules.severity_map) == ["blocker", "must fix", "nit"]
        assert rules.body.text == BODY
        assert rules.ignored_keys == ("name", "description")

    def test_the_path_is_recorded_as_configured_not_resolved(self, tmp_path, monkeypatch):
        _write(tmp_path, SKILL)
        monkeypatch.chdir(tmp_path)
        rules = load_review_rules("./rules.md", max_chars=100, source="--rules-file")
        assert rules.record()["path"] == "./rules.md"

    def test_record_sha256_equals_shasum_of_the_file_including_front_matter(self, tmp_path):
        path = _write(tmp_path, SKILL)
        record = load_review_rules(path, max_chars=12000, source="--rules-file").record()
        assert record["sha256"] == _sha(path)
        assert record["sha256"] != hashlib.sha256(BODY.encode("utf-8")).hexdigest()

    def test_the_hash_does_not_move_with_the_cap_and_one_byte_moves_it(self, tmp_path):
        small = _load(tmp_path, max_chars=5).record()
        large = _load(tmp_path, max_chars=5000).record()
        assert small["sha256"] == large["sha256"]
        assert (small["truncated"], large["truncated"]) == (True, False)
        edited = _load(tmp_path, SKILL.replace("timeout", "timeouT")).record()
        assert edited["sha256"] != large["sha256"]

    def test_chars_and_truncated_describe_the_body_after_front_matter(self, tmp_path):
        path = _write(tmp_path, SKILL)
        record = load_review_rules(path, max_chars=12000, source="--rules-file").record()
        assert record == {
            "path": path, "sha256": _sha(path), "chars": len(BODY), "max_chars": 12000,
            "truncated": False, "severity_map": MAP,
        }
        assert json.loads(json.dumps(record)) == record
        assert CANARY not in json.dumps(record)

    def test_a_body_exactly_at_the_cap_is_whole_and_one_over_is_cut(self, tmp_path):
        exact = _load(tmp_path, max_chars=len(BODY))
        assert (exact.body.text, exact.body.truncated) == (BODY, False)
        cut = _load(tmp_path, max_chars=len(BODY) - 1)
        assert (cut.body.text, cut.body.truncated, cut.body.chars) == (BODY[:-1], True, len(BODY))

    def test_bom_crlf_and_cr_are_normalised_before_the_split(self, tmp_path):
        crlf = b"\xef\xbb\xbf" + SKILL.replace("\n", "\r\n").encode("utf-8")
        rules = _load(tmp_path, crlf)
        assert dict(rules.severity_map) == MAP
        assert rules.body.text == BODY
        assert rules.record()["sha256"] == hashlib.sha256(crlf).hexdigest()
        lone_cr = _load(tmp_path, SKILL.replace("\n", "\r"), name="cr.md")
        assert lone_cr.body.text == BODY

    @pytest.mark.parametrize("source", SOURCES)
    def test_missing_file_is_a_config_error_naming_its_source(self, tmp_path, source):
        missing = str(tmp_path / "absent.md")
        assert _config_error(missing, source=source) == (
            f"{source}: cannot read rules file {missing!r}: No such file or directory"
        )

    def test_a_directory_is_a_config_error(self, tmp_path):
        assert _config_error(str(tmp_path)) == (
            f"--rules-file: cannot read rules file {str(tmp_path)!r}: Is a directory"
        )

    def test_a_fifo_is_refused_without_being_opened(self, tmp_path):
        fifo = tmp_path / "rules.fifo"
        os.mkfifo(fifo)
        assert _config_error(str(fifo)) == (
            f"--rules-file: cannot read rules file {str(fifo)!r}: not a regular file"
        )

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
    def test_an_unreadable_file_is_a_config_error(self, tmp_path):
        path = _write(tmp_path, SKILL)
        os.chmod(path, 0)
        assert _config_error(path) == f"--rules-file: cannot read rules file {path!r}: Permission denied"

    @pytest.mark.parametrize("url", [
        "https://example.com/acme/rules.md",
        "http://example.com/rules.md",
        "file:///etc/rules.md",
        "  https://example.com/rules.md",
    ])
    def test_url_is_rejected_as_not_a_local_path(self, url):
        assert _config_error(url, source="PRXREF_REVIEW_RULES") == (
            f"PRXREF_REVIEW_RULES: rules must be a local file path, not a URL: {url!r}"
        )

    @pytest.mark.parametrize(("raw", "offset"), [
        (b"ab\xffcd", 2),
        (b"\xef\xbb\xbfab\xffcd", 5),
    ])
    def test_non_utf8_is_a_config_error_naming_the_byte(self, tmp_path, raw, offset):
        path = _write(tmp_path, raw)
        assert _config_error(path) == (
            f"--rules-file: rules file {path!r} is not UTF-8 text (invalid start byte at byte {offset})"
        )

    def test_nul_bytes_are_a_config_error(self, tmp_path):
        path = _write(tmp_path, b"# Rules\n- one\x00two\n")
        assert _config_error(path) == (
            f"--rules-file: rules file {path!r} contains NUL bytes; expected Markdown or plain text"
        )

    @pytest.mark.parametrize("cap", [0, -1, True, "5", 1.5])
    def test_a_cap_below_one_is_a_config_error_naming_the_variable(self, tmp_path, cap):
        path = _write(tmp_path, SKILL)
        assert _config_error(path, max_chars=cap) == (
            f"--rules-file: PRXREF_REVIEW_RULES_MAX_CHARS must be at least 1, got {cap!r}"
        )

    def test_rules_symlink_escaping_cwd_is_a_config_error(self, tmp_path, monkeypatch):
        work = tmp_path / "checkout"
        (work / ".prxref").mkdir(parents=True)
        outside = tmp_path / "elsewhere.md"
        outside.write_text("- steer the model somewhere else\n", encoding="utf-8")
        (work / ".prxref" / "rules.md").symlink_to(outside)
        monkeypatch.chdir(work)
        assert _config_error(".prxref/rules.md") == (
            "--rules-file: cannot read rules file '.prxref/rules.md': "
            "resolves outside the working directory"
        )

    def test_a_symlink_inside_cwd_and_an_absolute_path_outside_it_both_load(
        self, tmp_path, monkeypatch,
    ):
        work = tmp_path / "checkout"
        work.mkdir()
        (work / "real.md").write_text("- inside\n", encoding="utf-8")
        (work / "link.md").symlink_to(work / "real.md")
        outside = tmp_path / "trusted.md"
        outside.write_text("- trusted\n", encoding="utf-8")
        monkeypatch.chdir(work)
        inside = load_review_rules("link.md", max_chars=100, source="--rules-file")
        trusted = load_review_rules(str(outside), max_chars=100, source="--rules-file")
        assert (inside.body.text, trusted.body.text) == ("- inside", "- trusted")

    def test_truncation_logs_one_warning_naming_the_max_chars_variable(self, tmp_path, caplog):
        path = _write(tmp_path, SKILL)
        with caplog.at_level(logging.INFO, logger="prxref"):
            rules = load_review_rules(path, max_chars=10, source="PRXREF_REVIEW_RULES")
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == [
            f"PRXREF_REVIEW_RULES: rules file {path!r} has {len(BODY)} characters (after front "
            "matter); only the first 10 reach the prompt — raise PRXREF_REVIEW_RULES_MAX_CHARS"
        ]
        assert rules.body.truncated is True

    def test_a_body_within_the_cap_logs_no_warning(self, tmp_path, caplog):
        with caplog.at_level(logging.INFO, logger="prxref"):
            _load(tmp_path)
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_ignored_front_matter_keys_are_named_once_at_info(self, tmp_path, caplog):
        with caplog.at_level(logging.INFO, logger="prxref"):
            _load(tmp_path, SKILL.replace("severity:\n", "name: again\nseverity:\n", 1))
        infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert infos == [
            "--rules-file: ignoring front-matter keys other than 'severity': name, description",
        ]

    def test_an_empty_file_warns_and_is_still_recorded(self, tmp_path, caplog):
        path = _write(tmp_path, b"")
        with caplog.at_level(logging.WARNING, logger="prxref"):
            rules = load_review_rules(path, max_chars=100, source="--rules-file")
        assert [r.getMessage() for r in caplog.records] == [
            f"--rules-file: rules file {path!r} is empty; no rules injected",
        ]
        assert rules.record() == {
            "path": path, "sha256": hashlib.sha256(b"").hexdigest(), "chars": 0,
            "max_chars": 100, "truncated": False, "severity_map": {},
        }

    def test_a_map_only_file_does_not_warn_about_being_empty(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref"):
            rules = _load(tmp_path, "---\nseverity:\n  blocker: error\n---\n")
        assert caplog.records == []
        assert (rules.body.text, dict(rules.severity_map)) == ("", {"blocker": "error"})


ACCEPTS = [
    ("no front matter", "# Rules\n- be nice\n", {}, (), "# Rules\n- be nice\n", False),
    (
        "flat severity block",
        "---\nseverity:\n  blocker: error\n  major: warning\n  nit: outofscope\n---\n# Body\n",
        {"blocker": "error", "major": "warning", "nit": "outofscope"}, (), "# Body\n", False,
    ),
    (
        "skill front matter with a block scalar",
        "---\nname: code-review\ndescription: |\n  Review: carefully\n  blocker: stuff\n"
        "severity:\n  Must Fix: error   # comment\n  'nit': \"outofscope\"\n---\nbody",
        {"must fix": "error", "nit": "outofscope"}, ("name", "description"), "body", False,
    ),
    (
        "an unclosed fence is all body",
        "---\nseverity:\n  blocker: error\n",
        {}, (), "---\nseverity:\n  blocker: error\n", True,
    ),
    (
        "the identity mapping is ignored",
        "---\nseverity:\n  error: error\n  blocker: error\n---\nx",
        {"blocker": "error"}, (), "x", False,
    ),
    ("a later rule is body", "# T\n\n---\n\nmore", {}, (), "# T\n\n---\n\nmore", False),
    (
        "tabs and trailing blanks on the fences",
        "---  \nseverity:\n\tblocker: error\n---\t\nb",
        {"blocker": "error"}, (), "b", False,
    ),
    ("an empty severity block", "---\nseverity:\ntitle: x\n---\nb", {}, ("title",), "b", False),
    (
        "a quoted word with a run of spaces",
        '---\nseverity:\n  "Must   Fix":  warning\n---\n',
        {"must fix": "warning"}, (), "", False,
    ),
    (
        "comments, blank lines and an upper-case tier",
        "---\n# the team map\n\nseverity:   # below\n  blocker: ERROR  # hard stop\n\n---\nb",
        {"blocker": "error"}, (), "b", False,
    ),
    (
        "one word twice with the same tier",
        "---\nseverity:\n  blocker: error\n  Blocker: error\n---\nb",
        {"blocker": "error"}, (), "b", False,
    ),
    ("a fence closed at once", "---\n---\nbody", {}, (), "body", False),
    (
        "a rule after the front matter stays in the body",
        "---\nseverity:\n  nit: outofscope\n---\n# T\n---\nmore",
        {"nit": "outofscope"}, (), "# T\n---\nmore", False,
    ),
    (
        "the key is case-insensitive",
        "---\nSeverity:\n  nit: outofscope\n---\nb",
        {"nit": "outofscope"}, (), "b", False,
    ),
]

REJECTS = [
    ("an unknown tier", "---\nseverity:\n  blocker: critical\n---\n", 3,
     "unknown severity 'critical' for 'blocker'; expected one of error, outofscope, warning"),
    ("the reserved tier", "---\nseverity:\n  contract: spec\n---\n", 3,
     "'spec' is reserved for spec-grounded findings (PRXREF_SPEC_SOURCES / --spec); "
     "map team words to error, warning, or outofscope"),
    ("a remap of prxref's own word", "---\nseverity:\n  warning: error\n---\n", 3,
     "cannot remap prxref's own severity 'warning'"),
    ("an inline flow value", "---\nseverity: {blocker: error}\n---\n", 2,
     "'severity' must be a block of indented '<word>: <tier>' lines"),
    ("an inline scalar", "---\nseverity: error\n---\n", 2,
     "'severity' must be a block of indented '<word>: <tier>' lines"),
    ("a list item", "---\nseverity:\n  - blocker\n---\n", 3,
     "severity map entry must be '<word>: <tier>', got '- blocker'"),
    ("a list item with a colon", "---\nseverity:\n  - blocker: error\n---\n", 3,
     "severity map entry must be '<word>: <tier>', got '- blocker: error'"),
    ("a nested block", "---\nseverity:\n  blocker:\n    tier: error\n---\n", 3,
     "severity map entry must be '<word>: <tier>', got 'blocker:'"),
    ("one word mapped to two tiers", "---\nseverity:\n  blocker: error\n  Blocker: warning\n---\n", 4,
     "'blocker' is mapped twice (error and warning)"),
    ("a second severity key", "---\nseverity:\n  blocker: error\nseverity:\n---\n", 4,
     "duplicate 'severity' key"),
]


class TestFrontMatter:
    @pytest.mark.parametrize(
        ("text", "severity_map", "ignored", "body", "warns"),
        [case[1:] for case in ACCEPTS], ids=[case[0] for case in ACCEPTS],
    )
    def test_accepted(self, caplog, text, severity_map, ignored, body, warns):
        with caplog.at_level(logging.WARNING, logger="prxref"):
            got = split_front_matter(text, source="PRXREF_REVIEW_RULES", path="r.md")
        assert got == (severity_map, ignored, body)
        warnings = [r.getMessage() for r in caplog.records]
        assert warnings == ([
            "PRXREF_REVIEW_RULES: rules file 'r.md' starts with '---' but never closes it; "
            "treating the whole file as rules text"
        ] if warns else [])

    @pytest.mark.parametrize(
        ("text", "lineno", "problem"), [case[1:] for case in REJECTS], ids=[case[0] for case in REJECTS],
    )
    def test_rejected_naming_source_path_and_line(self, text, lineno, problem):
        with pytest.raises(ConfigError) as exc:
            split_front_matter(text, source="--rules-file", path="team/rules.md")
        assert str(exc.value) == f"--rules-file: team/rules.md:{lineno}: {problem}"

    def test_error_message_carries_path_and_line_number_through_the_loader(self, tmp_path):
        path = _write(tmp_path, SKILL.replace("  nit: outofscope\n", "  nit: outofscop\n"))
        assert _config_error(path, source="PRXREF_REVIEW_RULES") == (
            f"PRXREF_REVIEW_RULES: {path}:9: unknown severity 'outofscop' for 'nit'; "
            "expected one of error, outofscope, warning"
        )

    def test_the_mappable_tiers_are_prxrefs_own_minus_spec(self):
        assert RESERVED_SEVERITIES == frozenset({"spec"})
        assert MAPPABLE_SEVERITIES == SEVERITIES - {"spec"} == {"error", "warning", "outofscope"}

    def test_front_matter_is_removed_from_the_injected_text(self, tmp_path):
        rules = _load(tmp_path)
        for unit in ("worker", "sweep"):
            block = rules.prompt_block(unit)
            assert f"<team_rules>\n{BODY}\n</team_rules>" in block
            for leaked in ("name: team-review", "description:", "severity:", "---"):
                assert leaked not in block, (unit, leaked)


class TestPromptBlock:
    def test_the_worker_block_in_order(self, tmp_path):
        block = _load(tmp_path).prompt_block("worker")
        parts = block.split("\n\n")
        assert parts[0] == RULES_HEADING == "## Team review rules"
        assert WORKER_PHRASE in parts[1] and SWEEP_PHRASE not in block
        assert parts[2] == MAP_PARAGRAPH
        assert block.endswith(f"\n\n{MAP_PARAGRAPH}\n\n<team_rules>\n{BODY}\n</team_rules>")

    def test_the_sweep_block_uses_the_sweep_framing(self, tmp_path):
        rules = _load(tmp_path)
        sweep, worker = rules.prompt_block("sweep"), rules.prompt_block("worker")
        assert sweep.split("\n\n")[0] == RULES_HEADING
        assert SWEEP_PHRASE in sweep.split("\n\n")[1] and WORKER_PHRASE not in sweep
        assert sweep != worker
        assert sweep.split("\n\n")[2:] == worker.split("\n\n")[2:]

    def test_severity_paragraph_lists_entries_in_file_order(self, tmp_path):
        rules = _load(tmp_path, "---\nseverity:\n  nit: outofscope\n  blocker: error\n---\nbody")
        assert "`nit` → `outofscope`; `blocker` → `error`." in rules.prompt_block("worker")

    def test_severity_paragraph_absent_without_a_map(self, tmp_path):
        block = _load(tmp_path, BODY).prompt_block("worker")
        assert "Team severity words" not in block
        assert block.split("\n\n")[2] == "<team_rules>\n# Team rules"

    def test_truncation_line_present_only_when_truncated(self, tmp_path):
        cut = _load(tmp_path, max_chars=10).prompt_block("worker")
        assert cut.endswith(
            f"<team_rules>\n{BODY[:10]}\n</team_rules>\n\n"
            f"[team rules truncated: only the first 10 of {len(BODY)} characters are shown]"
        )
        whole = _load(tmp_path, max_chars=len(BODY)).prompt_block("worker")
        assert "truncated" not in whole

    def test_map_only_file_renders_heading_and_map_without_tags(self, tmp_path):
        block = _load(tmp_path, "---\nseverity:\n  blocker: error\n---\n").prompt_block("sweep")
        assert block.startswith(f"{RULES_HEADING}\n\n")
        assert block.endswith("`blocker` → `error`. Classify a problem by the team's definition, "
                              "then write the mapped word in `severity`.")
        assert "<team_rules>" not in block

    @pytest.mark.parametrize("content", [b"", b"\n\n  \n", b"---\nname: x\n---\n\n"])
    def test_empty_file_renders_no_block(self, tmp_path, content):
        rules = _load(tmp_path, content)
        assert rules.prompt_block("worker") == rules.prompt_block("sweep") == ""

    def test_an_unknown_unit_raises(self, tmp_path):
        with pytest.raises(ValueError, match="unit must be one of worker, sweep, got 'chunk'"):
            _load(tmp_path).prompt_block("chunk")

    def test_the_block_is_deterministic(self, tmp_path):
        assert _load(tmp_path).prompt_block("worker") == _load(tmp_path, name="again.md").prompt_block("worker")


def _chunk():
    return parse_unified_diff(_added_file_diff("src/app.py", 20))


class TestRealRenderers:
    def test_worker_system_prompt_ends_with_the_rules_block(self, tmp_path):
        rules = _load(tmp_path)
        ctx = PromptContext(rules_worker=rules.prompt_block("worker"), rules_sweep=rules.prompt_block("sweep"))
        system, user = _render_prompt(_chunk(), "Add widget", "does things", "acme/widget", prompt_context=ctx)
        _plain_system, plain_user = _render_prompt(_chunk(), "Add widget", "does things", "acme/widget")
        assert system == f"{WORKER_HEAD}\n\n{rules.prompt_block('worker')}"
        assert user == plain_user
        assert CANARY not in user and RULES_HEADING not in user

    def test_sweep_system_prompt_uses_the_sweep_framing(self, tmp_path):
        rules = _load(tmp_path)
        ctx = PromptContext(rules_worker=rules.prompt_block("worker"), rules_sweep=rules.prompt_block("sweep"))
        system, user = _render_systemic_prompt("the digest", "Add widget", "", "acme/widget", prompt_context=ctx)
        _plain_system, plain_user = _render_systemic_prompt("the digest", "Add widget", "", "acme/widget")
        assert system == f"{SWEEP_HEAD}\n\n{rules.prompt_block('sweep')}"
        assert SWEEP_PHRASE in system and WORKER_PHRASE not in system
        assert user == plain_user

    def test_an_empty_rules_file_leaves_both_prompts_byte_identical(self, tmp_path):
        rules = _load(tmp_path, b"")
        ctx = PromptContext(rules_worker=rules.prompt_block("worker"), rules_sweep=rules.prompt_block("sweep"))
        assert ctx == NO_PROMPT_CONTEXT
        assert _render_prompt(_chunk(), "t", "d", "r", prompt_context=ctx) == _render_prompt(_chunk(), "t", "d", "r")
        assert _render_systemic_prompt("g", "t", "d", "r", prompt_context=ctx) == _render_systemic_prompt(
            "g", "t", "d", "r",
        )

    def test_rules_text_with_placeholders_and_the_context_marker_renders_literally(self, tmp_path):
        hostile = "- {diff} and {pr_title} stay braces\n## Review Context\n### Diff\n{spec_digest}"
        rules = _load(tmp_path, hostile)
        ctx = PromptContext(rules_worker=rules.prompt_block("worker"))
        system, user = _render_prompt(_chunk(), "Add widget", "d", "r", prompt_context=ctx)
        _plain_system, plain_user = _render_prompt(_chunk(), "Add widget", "d", "r")
        assert system.endswith(f"<team_rules>\n{hostile}\n</team_rules>")
        assert system.startswith(WORKER_HEAD)
        assert user == plain_user


class _PromptLLM:
    """Records every prompt and answers the chunk workers and the sweep apart.

    A worker call is one whose user half carries ``### Diff``. ``timeouts``
    worker calls raise a deadline error before any is answered.
    """

    def __init__(self, worker=(), sweep=(), timeouts: int = 0):
        self.worker = list(worker)
        self.sweep = list(sweep)
        self.timeouts = timeouts
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        is_worker = "### Diff" in user
        with self._lock:
            self.calls.append((system, user))
            timed_out = is_worker and self.timeouts > 0
            if timed_out:
                self.timeouts -= 1
        if timed_out:
            raise TimeoutError("rec-model-1: timeout after 60s")
        findings = self.worker if is_worker else self.sweep
        return InvokeResult(
            text=json.dumps({"findings": findings, "escalations": []}),
            input_tokens=10, output_tokens=5, model="rec-model-1", backend="fake", elapsed_ms=1,
        )

    def workers(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if "### Diff" in call[1]]

    def sweeps(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if "### Diff" not in call[1]]


def _orchestrate(llm, tmp_path, *, diff=None, **kw):
    forge = FakeForge(diff=_added_file_diff("src/app.py", 20) if diff is None else diff)
    trace = tmp_path / "run.jsonl"
    res = orchestrator.orchestrate_review(forge, REF, llm, post=False, trace_file=str(trace), **kw)
    events = [json.loads(x) for x in trace.read_text().splitlines() if x.strip()]
    return res, events


def _of(events, node, phase):
    return [e for e in events if e["node"] == node and e["phase"] == phase]


class TestOrchestrator:
    def test_every_chunk_and_the_sweep_receive_the_rules_in_system(self, tmp_path):
        rules = _load(tmp_path)
        with_rules, without = _PromptLLM(), _PromptLLM()
        res, _ = _orchestrate(with_rules, tmp_path, diff=multi_chunk_diff(3), rules=rules)
        _orchestrate(without, tmp_path, diff=multi_chunk_diff(3))
        assert res["chunks_reviewed"] == 4
        assert len(with_rules.workers()) == 3 and len(with_rules.sweeps()) == 1
        for system, _user in with_rules.workers():
            assert system == f"{WORKER_HEAD}\n\n{rules.prompt_block('worker')}"
        [(sweep_system, _user)] = with_rules.sweeps()
        assert sweep_system == f"{SWEEP_HEAD}\n\n{rules.prompt_block('sweep')}"
        assert sorted(u for _s, u in with_rules.calls) == sorted(u for _s, u in without.calls)
        for _system, user in with_rules.calls:
            assert CANARY not in user and RULES_HEADING not in user

    def test_timeout_retry_keeps_the_rules(self, tmp_path):
        rules = _load(tmp_path)
        llm = _PromptLLM(worker=[FINDING], timeouts=1)
        res, events = _orchestrate(llm, tmp_path, rules=rules)
        assert len(_of(events, "chunk", "retry")) == 1
        first, retry = llm.workers()
        assert first[0] == retry[0] == f"{WORKER_HEAD}\n\n{rules.prompt_block('worker')}"
        assert res["chunks_failed"] == 0

    def test_mapped_team_word_survives_the_gate_as_its_tier(self, tmp_path):
        rules = _load(tmp_path)
        llm = _PromptLLM(worker=[{**FINDING, "severity": "Blocker"}])
        res, events = _orchestrate(llm, tmp_path, rules=rules)
        assert [(f.title, f.severity) for f in res["findings_active"]] == [("Unchecked data write", "error")]
        assert res["verdict"] == "Request-Changes"
        assert [e["meta"] for e in _of(events, "rules", "remap")] == [{"findings": 1}]

    def test_unmapped_word_is_still_dropped_as_invalid_severity(self, tmp_path):
        rules = _load(tmp_path, "---\nseverity:\n  major: warning\n---\nbody")
        llm = _PromptLLM(worker=[{**FINDING, "severity": "Blocker"}])
        res, events = _orchestrate(llm, tmp_path, rules=rules)
        assert res["findings_active"] == []
        assert [f.drop_reason for f in res["findings_dropped"]] == ["invalid severity: 'Blocker'"]
        assert res["verdict"] == "Approved"
        assert _of(events, "rules", "remap") == []

    def test_mapping_preserves_the_sweep_boundary(self, tmp_path):
        rules = _load(tmp_path)
        llm = _PromptLLM(worker=[{**FINDING, "severity": "error"}], sweep=[{**FINDING, "severity": "blocker"}])
        res, _ = _orchestrate(llm, tmp_path, rules=rules)
        assert [f.severity for f in res["findings_active"]] == ["error"]
        assert [(f.severity, f.drop_reason) for f in res["findings_dropped"]] == [
            ("error", "duplicate of chunk finding"),
        ]

    def test_the_rules_ok_event_carries_the_real_record(self, tmp_path):
        rules = _load(tmp_path)
        _res, events = _orchestrate(_PromptLLM(), tmp_path, rules=rules)
        assert [(e["node"], e["phase"]) for e in events[:2]] == [("run", "start"), ("rules", "ok")]
        assert [e["meta"] for e in _of(events, "rules", "ok")] == [rules.record()]

    def test_the_trace_dir_system_files_carry_the_rules_and_the_user_files_do_not(self, tmp_path):
        rules = _load(tmp_path)
        trace_dir = tmp_path / "trace"
        orchestrator.orchestrate_review(
            FakeForge(diff=_added_file_diff("src/app.py", 20)), REF, FakeLLM('{"findings": []}'),
            post=False, trace_dir=str(trace_dir), rules=rules,
        )
        chunk0 = (trace_dir / "chunk0.system.md").read_text(encoding="utf-8")
        sweep = (trace_dir / "sweep.system.md").read_text(encoding="utf-8")
        assert rules.prompt_block("worker") in chunk0
        assert rules.prompt_block("sweep") in sweep
        for user_file in sorted(trace_dir.glob("*.user.md")):
            assert CANARY not in user_file.read_text(encoding="utf-8"), user_file.name
        assert len(list(trace_dir.glob("*.user.md"))) == 2


@pytest.mark.usefixtures("contract_stubs")
class TestRecordOnEveryExit:
    @pytest.mark.parametrize("path", PATHS)
    def test_the_real_record_rides_every_exit(self, monkeypatch, tmp_path, path):
        rules = _load(tmp_path)
        res, _forge, events = _run(monkeypatch, path, tmp_path / "run", rules=rules)
        assert res["review_rules"] == rules.record()
        assert json.loads(json.dumps(res["review_rules"])) == rules.record()
        assert [e["meta"] for e in _of(events, "rules", "ok")] == [rules.record()]

    @pytest.mark.parametrize("path", PATHS)
    def test_it_is_none_on_every_exit_when_unset(self, monkeypatch, tmp_path, path):
        res, _forge, events = _run(monkeypatch, path, tmp_path)
        assert res["review_rules"] is None
        assert [e for e in events if e["node"] == "rules"] == []


class TestApplySeverityMap:
    def _finding(self, severity, **kw) -> Finding:
        return Finding(
            file="src/app.py", line=3, severity=severity, confidence=0.9,
            title=f"Problem {severity!r}", body="data 3 is wrong", **kw,
        )

    def test_it_matches_case_and_whitespace_insensitively(self):
        findings = [self._finding(s) for s in ("Blocker", "  must   FIX ", "nit", "NIT\t")]
        out = apply_severity_map(findings, MAP)
        assert [f.severity for f in out] == ["error", "warning", "outofscope", "outofscope"]

    def test_a_rewrite_is_a_copy_that_keeps_every_other_field(self):
        original = self._finding("blocker", scope=SCOPE_OUT)
        (out,) = apply_severity_map([original], MAP)
        assert out == replace(original, severity="error")
        assert out.scope == SCOPE_OUT and original.severity == "blocker"

    def test_dropped_unmapped_and_own_severities_pass_through_as_the_same_object(self):
        findings = [
            self._finding("blocker", drop_reason='hedged: "if"'),
            self._finding("major"),
            self._finding("warning"),
            self._finding("Error"),
            self._finding(None),
        ]
        out = apply_severity_map(findings, {**MAP, "warning": "error", "error": "outofscope"})
        assert all(a is b for a, b in zip(out, findings, strict=True))

    def test_length_and_order_are_kept_and_nothing_is_dropped(self):
        findings = [self._finding(s) for s in ("nit", "warning", "blocker", "major", "error")]
        out = apply_severity_map(findings, MAP)
        assert [f.severity for f in out] == ["outofscope", "warning", "error", "major", "error"]
        assert [f.title for f in out] == [f.title for f in findings]
        assert all(f.drop_reason is None for f in out)

    @pytest.mark.parametrize("severity_map", [{}, None])
    def test_an_empty_map_returns_a_new_list_of_the_same_objects(self, severity_map):
        findings = [self._finding("blocker"), self._finding("warning")]
        out = apply_severity_map(findings, severity_map)
        assert out is not findings
        assert all(a is b for a, b in zip(out, findings, strict=True))


def _install_fake_module(monkeypatch, fullname: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(fullname)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, fullname, mod)
    return mod


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """``main`` and the daemon over the real orchestrator; only the forge and the model are doubles."""
    assert sys.modules["prxref.orchestrator"] is orchestrator
    forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
    llm = _PromptLLM(worker=[{**FINDING, "severity": "blocker"}])
    monkeypatch.setattr("prxref.cli.detect_forge", lambda url: REF)
    monkeypatch.setattr("prxref.cli.make_forge", lambda ref: forge)
    _install_fake_module(monkeypatch, "prxref.llm_backends", create_llm_client=lambda cfg: llm)
    return types.SimpleNamespace(forge=forge, llm=llm, rules=_write(tmp_path, SKILL))


def _review(*extra: str) -> int:
    return main(["review", "--pr-url", REF.url, "--no-post", *extra])


def _json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _rules_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("rules:")]


class TestCli:
    def _expected(self, path: str, **kw) -> dict:
        return {
            "path": path, "sha256": _sha(path), "chars": len(BODY), "max_chars": 12000,
            "truncated": False, "severity_map": MAP, **kw,
        }

    def _assert_in_every_system_prompt(self, llm) -> None:
        assert len(llm.workers()) == 1 and len(llm.sweeps()) == 1
        for system, user in llm.calls:
            assert CANARY in system
            assert CANARY not in user

    def test_the_flag_reaches_every_prompt_the_record_and_the_gate(self, rig, capsys):
        assert _review("--format", "json", "--rules-file", rig.rules) == 0
        payload = _json(capsys)
        assert payload["review_rules"] == self._expected(rig.rules)
        assert [(f["severity"], f["drop_reason"]) for f in payload["findings"]] == [("error", None)]
        assert payload["verdict"] == "Request-Changes"
        self._assert_in_every_system_prompt(rig.llm)

    def test_the_variable_reaches_the_pipeline_with_its_cap(self, rig, monkeypatch, capsys, caplog):
        monkeypatch.setenv("PRXREF_REVIEW_RULES", rig.rules)
        monkeypatch.setenv("PRXREF_REVIEW_RULES_MAX_CHARS", "10")
        with caplog.at_level(logging.WARNING, logger="prxref"):
            assert _review("--format", "json") == 0
        assert _json(capsys)["review_rules"] == self._expected(rig.rules, max_chars=10, truncated=True)
        warnings = [r.getMessage() for r in caplog.records if r.name == "prxref.rules"]
        assert len(warnings) == 1
        assert warnings[0].startswith("PRXREF_REVIEW_RULES: rules file ")
        assert warnings[0].endswith("raise PRXREF_REVIEW_RULES_MAX_CHARS")

    def test_the_flag_wins_over_the_variable(self, rig, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("PRXREF_REVIEW_RULES", str(tmp_path / "absent.md"))
        assert _review("--format", "json", "--rules-file", rig.rules) == 0
        assert _json(capsys)["review_rules"]["path"] == rig.rules

    def test_an_empty_flag_disables_the_variable(self, rig, monkeypatch, capsys):
        monkeypatch.setenv("PRXREF_REVIEW_RULES", rig.rules)
        assert _review("--format", "json", "--rules-file", "") == 0
        payload = _json(capsys)
        assert payload["review_rules"] is None
        assert [(f["severity"], f["drop_reason"]) for f in payload["findings"]] == [
            ("blocker", "invalid severity: 'blocker'"),
        ]
        assert rig.llm.calls and all(CANARY not in system for system, _user in rig.llm.calls)

    @pytest.mark.parametrize("via", ["flag", "variable"])
    def test_a_missing_file_exits_2_naming_its_source(self, rig, monkeypatch, capsys, tmp_path, via):
        missing = str(tmp_path / "absent.md")
        if via == "flag":
            code, source = _review("--rules-file", missing), "--rules-file"
        else:
            monkeypatch.setenv("PRXREF_REVIEW_RULES", missing)
            code, source = _review(), "PRXREF_REVIEW_RULES"
        assert code == 2
        assert capsys.readouterr().err == (
            f"configuration error: {source}: cannot read rules file {missing!r}: No such file or directory\n"
        )
        assert rig.llm.calls == []

    def test_a_bad_severity_map_exits_2_naming_the_line(self, rig, capsys, tmp_path):
        path = _write(tmp_path, "---\nseverity:\n  blocker: eror\n---\nbody\n", "bad.md")
        assert _review("--rules-file", path) == 2
        assert capsys.readouterr().err == (
            f"configuration error: --rules-file: {path}:3: unknown severity 'eror' for 'blocker'; "
            "expected one of error, outofscope, warning\n"
        )
        assert rig.llm.calls == []

    def test_max_chars_zero_exits_2_naming_the_variable(self, rig, monkeypatch, capsys):
        monkeypatch.setenv("PRXREF_REVIEW_RULES_MAX_CHARS", "0")
        assert _review("--rules-file", rig.rules) == 2
        assert capsys.readouterr().err.startswith("configuration error: PRXREF_REVIEW_RULES_MAX_CHARS")
        assert rig.llm.calls == []

    @pytest.mark.parametrize("fail_on", ["error", "any"])
    def test_the_fail_on_gate_does_not_turn_a_bad_rules_file_into_exit_1(
        self, rig, monkeypatch, capsys, tmp_path, fail_on,
    ):
        monkeypatch.setenv("PRXREF_FAIL_ON", fail_on)
        assert _review("--rules-file", str(tmp_path / "absent.md")) == 2
        assert capsys.readouterr().err.startswith("configuration error: --rules-file: ")

    def test_verbose_text_prints_the_rules_line_only_when_set(self, rig, monkeypatch, capsys):
        assert _review("-v") == 0
        assert _rules_lines(capsys.readouterr().out) == []
        assert _review("-v", "--rules-file", rig.rules) == 0
        assert _rules_lines(capsys.readouterr().out) == [
            f"rules: {rig.rules} sha256={_sha(rig.rules)[:12]} chars={len(BODY)}",
        ]
        monkeypatch.setenv("PRXREF_REVIEW_RULES_MAX_CHARS", "10")
        assert _review("-v", "--rules-file", rig.rules) == 0
        assert _rules_lines(capsys.readouterr().out) == [
            f"rules: {rig.rules} sha256={_sha(rig.rules)[:12]} chars={len(BODY)} (truncated at 10)",
        ]

    def test_the_daemon_rereads_the_variable_for_every_webhook(self, rig, monkeypatch):
        monkeypatch.setenv("PRXREF_REVIEW_RULES", rig.rules)
        cli._webhook_handler(REF.url)
        self._assert_in_every_system_prompt(rig.llm)
        with open(rig.rules, "w", encoding="utf-8") as fh:
            fh.write(SKILL.replace(CANARY, "RULE-CANARY-EDITED"))
        rig.llm.calls.clear()
        cli._webhook_handler(REF.url)
        assert len(rig.llm.calls) == 2
        for system, _user in rig.llm.calls:
            assert "RULE-CANARY-EDITED" in system and CANARY not in system
        assert len(rig.forge.summaries) == 2

    def test_the_daemon_logs_a_bad_rules_file_and_reviews_nothing(self, rig, monkeypatch, caplog, tmp_path):
        missing = str(tmp_path / "absent.md")
        monkeypatch.setenv("PRXREF_REVIEW_RULES", missing)
        with caplog.at_level(logging.ERROR, logger="prxref"):
            cli._webhook_handler(REF.url)
        errors = [str(r.exc_info[1]) for r in caplog.records if r.exc_info]
        assert errors == [
            f"PRXREF_REVIEW_RULES: cannot read rules file {missing!r}: No such file or directory",
        ]
        assert rig.llm.calls == [] and rig.forge.summaries == []


def test_a_hand_built_rules_object_matches_the_loaded_one(tmp_path):
    loaded = _load(tmp_path)
    body = cap_text(BODY, 12000, sha256=loaded.body.sha256)
    built = ReviewRules(path=loaded.path, body=body, severity_map=MAP)
    assert built.prompt_block("worker") == loaded.prompt_block("worker")
    assert built.record() == loaded.record()
