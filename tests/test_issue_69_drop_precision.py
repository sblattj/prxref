"""Issue #69, 0.30.1 review: the evidence drop only fires on a contradiction.

Pinned here, beside ``tests/test_issue_69_evidence.py``:

- a claim about a missing DIRECTIVE or VALUE of a header the probe shows
  present is not contradicted by that probe, so it is kept;
- an item whose exit status is unknown (JSON without ``exit_code``, a text
  block without an ``exit:`` line) settles nothing: it neither drops a
  header claim nor raises a failure finding;
- a finding naming no resource is dropped only by an item that probed no
  specific resource; a probe of one resource does not settle a claim
  about others;
- the 0.30.0 config-file key ``evidence_max_chunk_chars`` still loads as a
  deprecated alias of ``evidence_max_chars``;
- the summary's evidence note lists the findings dropped as restating a
  failing check, beside the ones the evidence contradicts.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from prxref.config import load_config, load_config_with_sources, read_config_file
from prxref.evidence import (
    EvidenceBundle,
    EvidenceItem,
    _parse_text_items,
    failure_findings,
    load_evidence,
)
from prxref.llm import ConfigError
from prxref.orchestrator import _evidence_note
from prxref.quality import apply_evidence_drops
from prxref.triage import Finding
from tests.test_issue_69_failure_findings import LINT as FAILING_LINT
from tests.test_issue_69_failure_findings import _evidence, _run

PR_PATHS = ("nginx.conf",)
ROOT_PROBE = "curl -sI https://cdn.example.com/"


def _finding(title: str, body: str = "", file: str = "nginx.conf", line: int = 3) -> Finding:
    return Finding(
        file=file, line=line, severity="error", confidence=0.9, title=title, body=body,
    )


def _drop(title: str, output: str, *, body: str = "", command: str = ROOT_PROBE,
          exit_code: int | None = 0) -> str | None:
    bundle = EvidenceBundle(
        items=(EvidenceItem(command, exit_code, output),), files=("run.json",),
    )
    (out,) = apply_evidence_drops([_finding(title, body)], bundle, pr_paths=PR_PATHS)
    return out.drop_reason


class TestDirectiveClaimsAreKept:
    """A missing directive or value of a present header is not contradicted."""

    @pytest.mark.parametrize("title,body,output", [
        ("Strict-Transport-Security lacks includeSubDomains", "",
         "Strict-Transport-Security: max-age=300"),
        ("Cache-Control lacks max-age directive", "", "Cache-Control: no-store"),
        ("Cache-Control header without no-store on login page", "",
         "Cache-Control: max-age=60"),
        ("Set Cache-Control: immutable", "Cache-Control has no immutable directive",
         "Cache-Control: max-age=60"),
        ("Cache-Control is missing max-age", "", "Cache-Control: no-store"),
        ("Cache-Control header is missing the no-store directive", "",
         "Cache-Control: max-age=60"),
        ("Missing includeSubDomains in Strict-Transport-Security", "",
         "Strict-Transport-Security: max-age=300"),
        ("Missing immutable directive on Cache-Control", "",
         "Cache-Control: max-age=60"),
        ("No preload value for the Strict-Transport-Security header", "",
         "Strict-Transport-Security: max-age=300"),
        ("Missing Cache-Control max-age directive", "", "Cache-Control: no-store"),
        ("Strict-Transport-Security includeSubDomains is missing", "",
         "Strict-Transport-Security: max-age=300"),
        ("Cache-Control not set to no-store", "", "Cache-Control: max-age=60"),
        ("Strict-Transport-Security is not set to include subdomains", "",
         "Strict-Transport-Security: max-age=300"),
    ])
    def test_a_directive_claim_is_kept(self, title, body, output):
        assert _drop(title, output, body=body) is None

    @pytest.mark.parametrize("title,body", [
        ("Missing Cache-Control header", ""),
        ("Caching", "Responses are served without a Cache-Control header."),
        ("Caching", "Responses set no Cache-Control header"),
        ("Caching", "Cache-Control is not set"),
        ("Caching", "Cache-Control header missing"),
        ("Caching", "The Cache-Control header is missing from responses."),
        ("Caching", "Cache-Control is absent"),
        ("Caching", "The static location block lacks a Cache-Control header."),
        ("Caching", "nginx does not send Cache-Control"),
        ("Missing HTTP Cache-Control header", ""),
    ])
    def test_a_header_claim_still_drops(self, title, body):
        assert _drop(title, "HTTP/2 200\ncache-control: max-age=60", body=body) == (
            f"contradicted by execution evidence: {ROOT_PROBE}"
        )


BOTH_HEADERS = (
    "HTTP/1.1 200 OK\nStrict-Transport-Security: max-age=300\n"
    "Cache-Control: max-age=60\nContent-Security-Policy: default-src 'self'"
)


class TestBareDirectiveAfterTheName:
    """A bare directive after the header name is an object, in either order."""

    @pytest.mark.parametrize("title", [
        "Missing Strict-Transport-Security includeSubDomains",
        "Missing Strict-Transport-Security preload",
        "Missing Cache-Control immutable",
        "No Cache-Control private for authenticated responses",
        "No Cache-Control no-store",
        "Missing Cache-Control no-store",
        "No Content-Security-Policy frame-ancestors",
        "Missing Strict-Transport-Security: includeSubDomains",
        "Lacks Cache-Control private and no-store",
        "Missing Strict-Transport-Security (HSTS) includeSubDomains",
    ])
    def test_keyword_first_directive_claim_is_kept(self, title):
        assert _drop(title, BOTH_HEADERS) is None

    @pytest.mark.parametrize("title", [
        "Strict-Transport-Security lacks includeSubDomains",
        "Strict-Transport-Security has no preload",
        "Cache-Control lacks immutable",
        "Cache-Control is missing private",
        "Cache-Control without no-store",
        "Content-Security-Policy lacks frame-ancestors",
        "Strict-Transport-Security does not include includeSubDomains",
    ])
    def test_name_first_directive_claim_is_kept(self, title):
        assert _drop(title, BOTH_HEADERS) is None

    @pytest.mark.parametrize("title", [
        "Missing Cache-Control header",
        "No Cache-Control header",
        "Missing Strict-Transport-Security",
        "Missing Strict-Transport-Security (HSTS) header",
        "Missing Strict-Transport-Security (HSTS)",
        "Missing Strict-Transport-Security to enforce HTTPS",
        "No Cache-Control for authenticated responses",
        "Missing Cache-Control header allows proxies to cache",
        "Missing Strict-Transport-Security which allows downgrade",
        "Response lacks a Cache-Control header",
        "The server does not send Strict-Transport-Security",
        "No Cache-Control is sent",
    ])
    def test_keyword_first_header_claim_still_drops(self, title):
        assert _drop(title, BOTH_HEADERS) == (
            f"contradicted by execution evidence: {ROOT_PROBE}"
        )

    @pytest.mark.parametrize("title", [
        "Cache-Control header is not set",
        "Strict-Transport-Security missing",
        "Strict-Transport-Security header missing which allows downgrade",
        "Cache-Control is never set by the server",
        "Cache-Control is missing from responses",
    ])
    def test_name_first_header_claim_still_drops(self, title):
        assert _drop(title, BOTH_HEADERS) == (
            f"contradicted by execution evidence: {ROOT_PROBE}"
        )


class TestUnknownExitSettlesNothing:
    """An item with no exit status carries no drop power and raises nothing."""

    def test_a_json_item_without_exit_code_has_an_unknown_exit(self, tmp_path):
        path = tmp_path / "run.json"
        path.write_text(json.dumps([
            {"command": ROOT_PROBE, "output": "cache-control: max-age=60"},
        ]), encoding="utf-8")
        bundle = load_evidence([str(path)], max_chars=120_000, source="PRXREF_EVIDENCE_FILES")
        assert bundle.items[0].exit_code is None
        (out,) = apply_evidence_drops(
            [_finding("Missing Cache-Control header")], bundle, pr_paths=PR_PATHS,
        )
        assert out.drop_reason is None

    def test_a_json_null_exit_code_is_unknown(self, tmp_path):
        path = tmp_path / "run.json"
        path.write_text(json.dumps([{"command": "make", "exit_code": None}]), encoding="utf-8")
        bundle = load_evidence([str(path)], max_chars=120_000, source="--evidence-file")
        assert bundle.items[0].exit_code is None

    def test_a_text_block_without_an_exit_line_has_an_unknown_exit(self):
        items = _parse_text_items(f"$ {ROOT_PROBE}\ncache-control: max-age=60\n")
        assert items[0].exit_code is None
        bundle = EvidenceBundle(items=tuple(items), files=("run.txt",))
        (out,) = apply_evidence_drops(
            [_finding("Missing Cache-Control header")], bundle, pr_paths=PR_PATHS,
        )
        assert out.drop_reason is None

    def test_an_explicit_exit_line_still_parses(self):
        items = _parse_text_items(f"$ {ROOT_PROBE}\nexit: 0\ncache-control: max-age=60\n")
        assert items[0].exit_code == 0

    def test_an_unknown_exit_raises_no_failure_finding(self):
        bundle = EvidenceBundle(
            items=(EvidenceItem("ruff check nginx.conf", None, "nginx.conf:3:1: E999 bad"),),
            files=("run.json",),
        )
        assert failure_findings(bundle, PR_PATHS) == []

    def test_an_unknown_exit_renders_as_unknown(self):
        rendered = EvidenceItem("make", None, "ok").render()
        assert "exit: unknown" in rendered
        assert "exit: None" not in rendered


class TestUnscopedClaimsNeedAnUnscopedProbe:
    """A finding naming no resource is settled only by a probe of no specific one."""

    @pytest.mark.parametrize("command", [
        "curl -sI https://site/index.html",
        "curl -sI /x",
        "curl -sI https://cdn.example.com/fonts/a.otf",
    ])
    def test_a_probe_of_one_resource_keeps_an_unscoped_claim(self, command):
        assert _drop(
            "Missing Cache-Control header for font files", "cache-control: max-age=60",
            body="The location block for fonts sets no Cache-Control header.",
            command=command,
        ) is None

    @pytest.mark.parametrize("command", [
        "curl -sI https://cdn.example.com/",
        "curl -sI https://cdn.example.com",
        "nginx -T",
    ])
    def test_a_probe_of_no_specific_resource_drops_it(self, command):
        assert _drop(
            "Missing Cache-Control header", "Cache-Control: max-age=60", command=command,
        ) == f"contradicted by execution evidence: {command}"

    def test_a_named_resource_still_matches_the_probed_one(self):
        assert _drop(
            "Missing Cache-Control header on /x", "Cache-Control: max-age=60",
            command="curl -sI /x",
        ) == "contradicted by execution evidence: curl -sI /x"


class TestLegacyFileKey:
    """``evidence_max_chunk_chars`` in ``.prxref.toml`` is a deprecated alias."""

    def _file(self, tmp_path: Path, text: str) -> Path:
        path = tmp_path / ".prxref.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_0_30_0_key_still_loads(self, tmp_path):
        path = self._file(tmp_path, "evidence_max_chunk_chars = 3000\n")
        cfg = load_config(config_file=path)
        assert cfg["evidence_max_chars"] == 3000
        assert "evidence_max_chunk_chars" not in cfg
        assert read_config_file(path) == {"evidence_max_chars": 3000}

    def test_the_alias_is_attributed_to_the_file_layer(self, tmp_path):
        path = self._file(tmp_path, "evidence_max_chunk_chars = 3000\n")
        _cfg, layers = load_config_with_sources(config_file=path)
        assert layers["evidence_max_chars"] == "file"

    def test_an_out_of_range_alias_names_the_alias(self, tmp_path):
        path = self._file(tmp_path, "evidence_max_chunk_chars = 0\n")
        with pytest.raises(ConfigError, match=r"evidence_max_chunk_chars: must be .* greater than 0"):
            load_config(config_file=path)

    def test_a_wrong_type_names_the_alias(self, tmp_path):
        path = self._file(tmp_path, 'evidence_max_chunk_chars = "big"\n')
        with pytest.raises(ConfigError, match="'evidence_max_chunk_chars' must be an integer"):
            read_config_file(path)

    def test_both_names_in_one_file_is_an_error(self, tmp_path):
        path = self._file(
            tmp_path, "evidence_max_chars = 5000\nevidence_max_chunk_chars = 3000\n",
        )
        with pytest.raises(ConfigError, match="both"):
            read_config_file(path)

    def test_the_environment_still_beats_the_alias(self, tmp_path, monkeypatch):
        path = self._file(tmp_path, "evidence_max_chunk_chars = 3000\n")
        monkeypatch.setenv("PRXREF_EVIDENCE_MAX_CHARS", "5000")
        assert load_config(config_file=path)["evidence_max_chars"] == 5000


class TestNoteListsRestatedDrops:
    """The note names every claim the evidence settled, restated failures included."""

    RECORD = {"files": ["run.json"], "items": 1, "matched_chunks": 1}
    LINT = EvidenceItem("ruff check nginx.conf", 1, "nginx.conf:3:1: F401 unused")

    def test_a_restated_failure_is_counted_and_listed(self):
        restated = replace(
            _finding("Unused import (F401)"),
            drop_reason="restates execution evidence: ruff check nginx.conf",
        )
        bundle = EvidenceBundle(items=(self.LINT,), files=("run.json",))
        note = _evidence_note(self.RECORD, 0, bundle, (), restated=[restated])
        assert "1 finding(s) restating a failing check dropped" in note
        assert (
            "> Dropped: Unused import (F401) (nginx.conf), restates "
            "`ruff check nginx.conf`" in note
        )

    def test_no_restated_drop_leaves_the_note_unchanged(self):
        bundle = EvidenceBundle(items=(self.LINT,), files=("run.json",))
        assert _evidence_note(self.RECORD, 0, bundle, ()) == _evidence_note(
            self.RECORD, 0, bundle, (), restated=[],
        )
        assert "restat" not in _evidence_note(self.RECORD, 0, bundle, ())

    def test_the_posted_summary_lists_the_restated_drop(self, tmp_path):
        model = {
            "file": "src/app.py", "line": 3, "severity": "error", "confidence": 0.9,
            "title": "Unused import data", "body": "ruff reports F401 on data 3.",
        }
        forge, _res = _run(_evidence(tmp_path, FAILING_LINT), findings=[model], post=True)
        summary = forge.summaries[0]
        assert "1 finding(s) restating a failing check dropped" in summary
        assert (
            "> Dropped: Unused import data (src/app.py), restates `ruff check src`"
            in summary
        )
