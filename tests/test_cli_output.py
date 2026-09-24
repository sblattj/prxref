"""The ``review`` outputs added in 0.14, and the documents that describe them.

Covers the ``--format json`` payload (key order, the always-present run-record
keys, ``null`` when a feature is off, ``replay`` only on replay runs), the
per-finding ``scope`` field, the text summary lines (``size advisory:``,
``replay:``, and the ``-v`` cost, rules, ticket and spec lines), the
``Forge.get_compare_diff`` declaration, and the README surfaces that promise
all of it: the ``## CLI Flags`` list and its ``--format json`` key list, and
the taglines.

The fake-orchestrator stubbing mirrors ``tests/test_issue_08_cli_format.py``,
so this file stays self-contained. The end-to-end tests go through
``cli.main``, the same entry point the console script calls.
"""
from __future__ import annotations

import argparse
import inspect
import io
import json
import re
import sys
import tomllib
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import prxref
from prxref import cli
from prxref.forges.base import Forge, PRRef
from prxref.triage import Finding

URL = "https://github.com/org/repo/pull/7"
REF = PRRef(forge="github", host="github.com", owner="org", repo="repo", number=7, url=URL)

BASE_SHA = "0123456789abcdef0123456789abcdef01234567"
HEAD_SHA = "fedcba9876543210fedcba9876543210fedcba98"

JSON_KEYS = [
    "verdict", "findings", "chunk_count", "chunks_reviewed", "chunks_failed", "elapsed_ms",
    "input_tokens", "output_tokens", "cost_usd", "cost_estimated", "posted",
    "review_rules", "ticket_context", "spec_grounding", "size_advisory", "prompt_templates", "scoped_rules",
    "rule_counts",
]
NEW_RECORD_KEYS = [
    "cost_usd", "cost_estimated", "review_rules", "ticket_context", "spec_grounding", "size_advisory",
    "prompt_templates", "scoped_rules", "rule_counts",
]
FINDING_KEYS = [
    "file", "line", "severity", "confidence", "scope", "rule", "locations", "title", "body", "drop_reason",
]

RULES = {
    "path": ".prxref/rules.md", "sha256": "a1b2c3d4e5f6" + "0" * 52, "chars": 420,
    "max_chars": 12000, "truncated": False, "severity_map": {"warning": "error"},
}
TICKET = {
    "path": "ticket.md", "sha256": "feedfacecafe" + "1" * 52, "chars": 812, "max_chars": 6000,
    "truncated": False, "has_acceptance_criteria": True, "empty": False,
}
SPEC = {"sources": 3, "ok": 2, "failed": ["source 3 (url): HTTP 404"], "constraints": 7, "digest_sha256": "ab" * 32}
SIZE = {
    "changed_lines": 1200, "changed_files": 9, "lines_limit": 400, "files_limit": None,
    "triggered": True, "message": "This PR changes 1200 lines (limit 400); consider splitting it.",
}
REPLAY = {
    "base_sha": BASE_SHA, "head_sha": HEAD_SHA, "threads": "hidden", "diff_file": None,
    "description": "pinned", "as_of": "2026-05-01T09:30:00Z", "as_of_source": "first-review",
}
SAMPLING = {"temperature": 0.0, "seed": 7, "models": ["m"]}


def _finding(scope: str = "unknown", *, title: str = "t", drop_reason: str | None = None) -> Finding:
    return Finding(
        file="src/a.py", line=3, severity="warning", confidence=0.8, title=title, body="b",
        drop_reason=drop_reason, scope=scope,
    )


def _full_result(**overrides) -> dict:
    result = {
        "verdict": "Commented",
        "findings_active": [_finding("in", title="a"), _finding("out", title="b")],
        "findings_dropped": [_finding(title="c", drop_reason="duplicate of existing thread")],
        "chunk_count": 2,
        "chunks_reviewed": 2,
        "chunks_failed": 0,
        "elapsed_ms": 1500,
        "input_tokens": 1000,
        "output_tokens": 200,
        "cost_usd": 0.0007,
        "cost_estimated": False,
        "posted": False,
        "review_rules": RULES,
        "ticket_context": TICKET,
        "spec_grounding": SPEC,
        "size_advisory": SIZE,
    }
    result.update(overrides)
    return result


def _summary(result, *, verbose: bool, elapsed_s: float = 2.0) -> list[str]:
    buf = io.StringIO()
    cli._print_summary(result, elapsed_s, verbose=verbose, out=buf)
    return buf.getvalue().splitlines()


# --------------------------------------------------------------------------- JSON


class TestJsonPayload:
    def test_a_full_result_emits_every_key_in_the_contract_order(self):
        payload = cli._build_json_result(_full_result())
        assert list(payload) == JSON_KEYS

    def test_sampling_then_replay_come_last_when_the_result_carries_them(self):
        payload = cli._build_json_result(_full_result(sampling=SAMPLING, replay=REPLAY))
        assert list(payload) == [*JSON_KEYS, "sampling", "replay"]
        assert payload["sampling"] == SAMPLING
        assert payload["replay"] == REPLAY

    def test_replay_is_absent_on_a_normal_run(self):
        payload = cli._build_json_result(_full_result(sampling=SAMPLING))
        assert "replay" not in payload
        assert list(payload)[-1] == "sampling"

    def test_replay_without_sampling_still_follows_the_fixed_keys(self):
        payload = cli._build_json_result(_full_result(replay=REPLAY))
        assert list(payload) == [*JSON_KEYS, "replay"]

    def test_the_new_record_keys_are_forwarded_verbatim(self):
        payload = cli._build_json_result(_full_result())
        assert payload["cost_usd"] == 0.0007
        assert payload["cost_estimated"] is False
        assert payload["review_rules"] == RULES
        assert payload["ticket_context"] == TICKET
        assert payload["spec_grounding"] == SPEC
        assert payload["size_advisory"] == SIZE

    def test_a_feature_that_is_off_is_null_not_missing(self):
        """Release-wide rule: a JSON key new in 0.14 is always present and
        ``null`` when its feature is off. A consumer can then tell "off" from
        "this prxref is too old to know the key"."""
        result = _full_result(
            cost_usd=None, review_rules=None, ticket_context=None, spec_grounding=None, size_advisory=None,
        )
        payload = cli._build_json_result(result)
        for key in ("cost_usd", "review_rules", "ticket_context", "spec_grounding", "size_advisory"):
            assert key in payload
            assert payload[key] is None

    @pytest.mark.parametrize("result", [{}, None, "Approved", {"verdict": "Approved", "chunks_reviewed": 1}])
    def test_a_partial_or_malformed_result_gives_null_for_every_missing_key(self, result):
        payload = cli._build_json_result(result)
        assert list(payload) == JSON_KEYS
        assert payload["findings"] == []
        for key in NEW_RECORD_KEYS:
            assert payload[key] is None

    def test_the_payload_serializes(self):
        payload = cli._build_json_result(_full_result(sampling=SAMPLING, replay=REPLAY))
        assert json.loads(json.dumps(payload)) == payload


class TestFindingJson:
    def test_scope_sits_between_confidence_and_title(self):
        row = cli._finding_json(_finding("out"), drop_reason=None)
        assert list(row) == FINDING_KEYS
        assert row["scope"] == "out"

    def test_a_finding_without_a_ticket_reports_unknown(self):
        row = cli._finding_json(Finding("f.py", 1, "error", 0.9, "t", "b"), drop_reason=None)
        assert row["scope"] == "unknown"

    def test_a_finding_object_without_the_attribute_reports_unknown(self):
        legacy = SimpleNamespace(file="f.py", line=1, severity="error", confidence=0.9, title="t", body="b")
        row = cli._finding_json(legacy, drop_reason="x")
        assert row["scope"] == "unknown"
        assert row["drop_reason"] == "x"

    def test_active_and_dropped_rows_both_carry_scope(self):
        payload = cli._build_json_result(_full_result())
        assert [row["scope"] for row in payload["findings"]] == ["in", "out", "unknown"]
        assert [row["drop_reason"] for row in payload["findings"]] == [None, None, "duplicate of existing thread"]


# --------------------------------------------------------------------------- cost label


class TestFmtCost:
    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            ({}, "-"),
            ("Approved", "-"),
            (None, "-"),
            ({"cost_usd": None, "cost_estimated": False}, "cost unknown"),
            ({"cost_usd": 0.0007, "cost_estimated": False}, "$0.0007"),
            ({"cost_usd": 0.0007, "cost_estimated": True}, "~$0.0007 (est.)"),
            ({"cost_usd": 0.0007}, "$0.0007"),
            ({"cost_usd": 0.0, "cost_estimated": False}, "$0.00"),
            ({"cost_usd": 1.234, "cost_estimated": False}, "$1.23"),
        ],
    )
    def test_labels(self, result, expected):
        assert cli._fmt_cost(result) == expected

    def test_an_absent_key_and_an_unknown_cost_print_differently(self):
        """``-`` says nothing measured the cost; ``cost unknown`` says it was
        measured and no source priced it. Collapsing them hides a gap."""
        assert cli._fmt_cost({}) != cli._fmt_cost({"cost_usd": None})


# --------------------------------------------------------------------------- text summary


class TestTextSummaryAlwaysLines:
    def test_size_advisory_and_replay_follow_verdict_and_coverage_in_that_order(self):
        result = _full_result(chunks_reviewed=1, chunks_failed=1, replay=REPLAY)
        assert _summary(result, verbose=False) == [
            "verdict: Commented",
            "coverage: 1/2 chunks reviewed",
            f"size advisory: {SIZE['message']}",
            f"replay: base={BASE_SHA[:12]} head={HEAD_SHA[:12]} threads=hidden diff_file=- "
            "description=pinned as_of=2026-05-01T09:30:00Z (first-review)",
        ]

    def test_the_lines_print_under_verbose_too(self):
        lines = _summary(_full_result(replay=REPLAY), verbose=True)
        assert lines[0] == "verdict: Commented"
        assert lines[1] == f"size advisory: {SIZE['message']}"
        assert lines[2].startswith("replay: ")

    @pytest.mark.parametrize("size", [None, {**SIZE, "triggered": False, "message": None}, {**SIZE, "message": ""}])
    def test_no_size_advisory_line_without_a_message(self, size):
        lines = _summary(_full_result(size_advisory=size), verbose=False)
        assert not any(line.startswith("size advisory") for line in lines)

    def test_a_normal_run_prints_no_replay_line(self):
        lines = _summary(_full_result(), verbose=True)
        assert not any(line.startswith("replay:") for line in lines)

    def test_a_diff_file_replay_prints_dashes_for_the_missing_shas(self):
        stamp = {
            "base_sha": None, "head_sha": None, "threads": "hidden", "diff_file": "cases/x.patch",
            "description": "file", "as_of": None, "as_of_source": None,
        }
        lines = _summary(_full_result(replay=stamp, size_advisory=None), verbose=False)
        assert lines == [
            "verdict: Commented",
            "replay: base=- head=- threads=hidden diff_file=cases/x.patch description=file",
        ]

    def test_a_pinned_replay_with_threads_shown(self):
        stamp = {**REPLAY, "threads": "shown"}
        lines = _summary(_full_result(replay=stamp, size_advisory=None), verbose=False)
        assert lines[-1] == (
            f"replay: base={BASE_SHA[:12]} head={HEAD_SHA[:12]} threads=shown diff_file=- "
            "description=pinned as_of=2026-05-01T09:30:00Z (first-review)"
        )

    def test_non_verbose_prints_none_of_the_verbose_lines(self):
        lines = _summary(_full_result(), verbose=False)
        for prefix in ("counts:", "elapsed:", "rules:", "ticket:", "spec:"):
            assert not any(line.startswith(prefix) for line in lines), prefix


class TestTextSummaryVerboseLines:
    def test_the_elapsed_line_carries_the_cost_label(self):
        lines = _summary(_full_result(), verbose=True, elapsed_s=3.14)
        assert "elapsed: 3.1s tokens: 1000+200 cost: $0.0007" in lines

    @pytest.mark.parametrize(
        ("overrides", "label"),
        [
            ({"cost_usd": None}, "cost unknown"),
            ({"cost_usd": 0.0007, "cost_estimated": True}, "~$0.0007 (est.)"),
        ],
    )
    def test_the_cost_label_variants(self, overrides, label):
        lines = _summary(_full_result(**overrides), verbose=True, elapsed_s=1.0)
        assert f"elapsed: 1.0s tokens: 1000+200 cost: {label}" in lines

    def test_a_result_without_a_cost_key_prints_a_dash(self):
        result = _full_result()
        del result["cost_usd"]
        del result["cost_estimated"]
        lines = _summary(result, verbose=True, elapsed_s=1.0)
        assert "elapsed: 1.0s tokens: 1000+200 cost: -" in lines

    def test_the_rules_line(self):
        lines = _summary(_full_result(), verbose=True)
        assert "rules: .prxref/rules.md sha256=a1b2c3d4e5f6 chars=420" in lines

    def test_the_rules_line_names_the_cap_when_truncated(self):
        lines = _summary(_full_result(review_rules={**RULES, "truncated": True}), verbose=True)
        assert "rules: .prxref/rules.md sha256=a1b2c3d4e5f6 chars=420 (truncated at 12000)" in lines

    def test_the_ticket_line_counts_the_active_findings_by_scope(self):
        """Only ACTIVE findings count, and anything that is not ``in`` or
        ``out`` (including a malformed value) counts as unknown."""
        active = [
            _finding("in"), _finding("out"), _finding("out"), _finding("unknown"),
            SimpleNamespace(severity="warning", scope="sideways"),
        ]
        lines = _summary(_full_result(findings_active=active), verbose=True)
        assert "ticket: ticket.md sha256=feedfacecafe chars=812 in=1 out=2 unknown=2" in lines

    def test_the_ticket_line_marks_truncation(self):
        lines = _summary(_full_result(ticket_context={**TICKET, "truncated": True}), verbose=True)
        assert "ticket: ticket.md sha256=feedfacecafe chars=812 truncated in=1 out=1 unknown=0" in lines

    def test_the_spec_line(self):
        lines = _summary(_full_result(), verbose=True)
        assert "spec: 2/3 source(s) ok, 7 constraint(s)" in lines

    def test_the_verbose_lines_come_in_a_fixed_order(self):
        lines = _summary(_full_result(), verbose=True)
        prefixes = [line.split(":", 1)[0] for line in lines]
        assert prefixes == ["verdict", "size advisory", "counts", "elapsed", "rules", "ticket", "spec"]

    def test_an_unset_input_prints_no_line(self):
        result = _full_result(review_rules=None, ticket_context=None, spec_grounding=None)
        lines = _summary(result, verbose=True)
        for prefix in ("rules:", "ticket:", "spec:"):
            assert not any(line.startswith(prefix) for line in lines), prefix

    @pytest.mark.parametrize("result", ["Approved", None, {}])
    def test_a_bare_or_empty_result_still_prints(self, result):
        lines = _summary(result, verbose=True, elapsed_s=0.5)
        assert lines[0].startswith("verdict: ")
        assert lines[-1] == "elapsed: 0.5s tokens: 0+0 cost: -"


# --------------------------------------------------------------------------- entry point


def _install_fake_module(monkeypatch, fullname: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(fullname)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, fullname, mod)
    return mod


@pytest.fixture
def stub_review(monkeypatch):
    """Route ``cli.main`` to a fake orchestrator returning the given result."""

    def install(result: dict) -> None:
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: REF)
        _install_fake_module(monkeypatch, "prxref.llm_backends", create_llm_client=lambda cfg: object())
        _install_fake_module(monkeypatch, "prxref.orchestrator", orchestrate_review=lambda **kwargs: result)

    return install


class TestThroughTheEntryPoint:
    def test_format_json_emits_the_full_payload(self, stub_review, capsys):
        stub_review(_full_result(sampling=SAMPLING, replay=REPLAY))
        rc = cli.main(["review", "--pr-url", URL, "--no-post", "--format", "json"])
        assert rc == 0
        out, _ = capsys.readouterr()
        payload = json.loads(out)
        assert list(payload) == [*JSON_KEYS, "sampling", "replay"]
        assert [row["scope"] for row in payload["findings"]] == ["in", "out", "unknown"]
        assert payload["cost_usd"] == 0.0007
        assert payload["replay"] == REPLAY

    def test_format_json_on_a_pre_0_14_shaped_result_has_nulls(self, stub_review, capsys):
        """Until the run-record seats land, the orchestrator returns none of
        the new keys; the payload must still carry every one of them."""
        stub_review({"verdict": "Approved", "findings_active": [], "findings_dropped": []})
        rc = cli.main(["review", "--pr-url", URL, "--no-post", "--format", "json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert list(payload) == JSON_KEYS
        assert all(payload[key] is None for key in NEW_RECORD_KEYS)

    def test_text_verbose_prints_every_line(self, stub_review, capsys):
        stub_review(_full_result(replay=REPLAY))
        rc = cli.main(["review", "--pr-url", URL, "--no-post", "-v"])
        assert rc == 0
        out = capsys.readouterr().out
        for needle in (
            f"size advisory: {SIZE['message']}",
            f"replay: base={BASE_SHA[:12]} head={HEAD_SHA[:12]} threads=hidden diff_file=- "
            "description=pinned as_of=2026-05-01T09:30:00Z (first-review)",
            "tokens: 1000+200 cost: $0.0007",
            "rules: .prxref/rules.md sha256=a1b2c3d4e5f6 chars=420",
            "ticket: ticket.md sha256=feedfacecafe chars=812 in=1 out=1 unknown=0",
            "spec: 2/3 source(s) ok, 7 constraint(s)",
        ):
            assert needle in out, needle


# --------------------------------------------------------------------------- forge contract


class TestCompareDiffDeclaration:
    def test_the_protocol_declares_it_with_keyword_only_shas(self):
        method = Forge.get_compare_diff
        params = inspect.signature(method).parameters
        assert list(params) == ["self", "ref", "base_sha", "head_sha"]
        assert params["base_sha"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["head_sha"].kind is inspect.Parameter.KEYWORD_ONLY
        assert inspect.signature(method).return_annotation == "str"

    def test_the_docstring_says_it_is_optional_and_how_to_resolve_it(self):
        doc = inspect.getdoc(Forge.get_compare_diff)
        assert doc.startswith("Return the unified diff of ``head_sha`` against its merge-base with ``base_sha``.")
        assert 'getattr(forge, "get_compare_diff", None)' in doc
        assert 'returns ``""`` for an' in doc


# --------------------------------------------------------------------------- documents


def _repo_root() -> Path:
    from_package = Path(prxref.__file__).resolve().parents[2]
    if (from_package / "README.md").is_file():
        return from_package
    return Path(__file__).resolve().parents[1]


README = (_repo_root() / "README.md").read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    start = text.index(f"\n{heading}\n")
    end = text.find("\n## ", start + len(heading) + 2)
    return text[start : end if end != -1 else len(text)]


def _option_strings(parser: argparse.ArgumentParser) -> set[str]:
    found: set[str] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                found |= _option_strings(sub)
        elif not isinstance(action, argparse._HelpAction):
            found.update(action.option_strings)
    return found


def _mentions(text: str, flag: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", text) is not None


class TestReadmeCliFlags:
    FLAGS = _section(README, "## CLI Flags")

    def test_every_parser_option_is_documented(self):
        """A flag the parser accepts but the README never names is a flag a
        PyPI reader cannot discover (the long description IS the README)."""
        missing = sorted(o for o in _option_strings(cli._build_parser()) if not _mentions(self.FLAGS, o))
        assert missing == [], f"README.md '## CLI Flags' does not mention {missing}"

    @pytest.mark.parametrize(
        "flag",
        [
            "--pr-url", "--no-post", "--max-chunks", "--timeout", "--spec", "--rules-file", "--context-file",
            "--trace-dir", "--verbose", "--format", "--base-sha", "--head-sha", "--no-threads", "--diff-file",
        ],
    )
    def test_every_0_14_review_flag_is_documented(self, flag):
        assert _mentions(self.FLAGS, flag)

    def test_the_format_json_key_list_matches_the_payload_in_order(self):
        bullet = self.FLAGS[self.FLAGS.index("- `--format {text,json}`") :]
        bullet = bullet[: bullet.index("\n\n")]
        keys = list(cli._build_json_result(_full_result(sampling=SAMPLING, replay=REPLAY)))
        positions = [bullet.find(f"`{key}`") for key in keys]
        assert -1 not in positions, [k for k, p in zip(keys, positions, strict=True) if p == -1]
        assert positions == sorted(positions), "README lists the --format json keys out of order"

    def test_the_format_json_finding_fields_match_the_row_in_order(self):
        line = next(ln for ln in self.FLAGS.splitlines() if ln.lstrip().startswith("- `findings`"))
        fields = list(cli._finding_json(_finding(), drop_reason=None))
        positions = [line.find(f"`{field}`") for field in fields]
        assert -1 not in positions
        assert positions == sorted(positions)


class TestTaglines:
    FORGES = ("Bitbucket", "GitLab", "GitHub", "Azure DevOps")

    def _taglines(self) -> dict[str, str]:
        pyproject = tomllib.loads((_repo_root() / "pyproject.toml").read_text(encoding="utf-8"))
        return {
            "pyproject.toml description": pyproject["project"]["description"],
            "prxref/__init__.py docstring": (prxref.__doc__ or "").splitlines()[0],
            "README.md tagline": README.splitlines()[2],
        }

    @pytest.mark.parametrize("forge", FORGES)
    def test_every_tagline_names_every_forge(self, forge):
        missing = [where for where, text in self._taglines().items() if forge not in text]
        assert missing == [], f"{forge} missing from {missing}"

    def test_the_readme_intro_names_the_spec_flag(self):
        intro = README.split("\n\n")[2]
        assert "`--spec`" in intro
        assert "Azure DevOps" in intro
