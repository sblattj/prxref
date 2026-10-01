"""Issue 71: stable finding ids and a persisted verdict store.

The repro: a reviewer who refutes a finding had no way to say so that the
NEXT run remembers — a finding's identity was its ``(file, line)`` plus its
title, and both move between runs, so every re-review re-raised the same
refuted claim at a slightly different anchor.

Frozen contracts under test: the id is deterministic and content-derived
(``<file>#<rule or norule>#<12-hex claim hash>`` over the title's sorted
content words — no model, no embedding), survives a rewording that
reorders or re-punctuates the claim and an anchor that moves one YAML key,
while ``anchor_block`` (the enclosing function, YAML key path or manifest
key) travels as metadata the id excludes; a ``refuted`` verdict in the
loaded store drops its finding as ``refuted in earlier run (<id>)`` with
``id_reused_from="verdict"``; nothing runs — every ``id`` stays ``None``,
the run record's ``stable_ids`` stays ``None`` — unless the caller turns
``PRXREF_STABLE_IDS`` on; and the eval churn metric reads 1.0 for identical
ids and 0.0 for disjoint ones.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataclasses import replace

from test_orchestrator import (  # noqa: E402  (shared fixtures, read not guessed)
    REF,
    FakeForge,
    FakeLLM,
    _added_file_diff,
)

from prxref import verdicts  # noqa: E402
from prxref.llm import ConfigError  # noqa: E402
from prxref.orchestrator import orchestrate_review  # noqa: E402
from prxref.stable_ids import (  # noqa: E402
    NO_RULE,
    REUSED_FROM_RUN,
    REUSED_FROM_THREAD,
    REUSED_FROM_VERDICT,
    anchor_block,
    apply_stable_ids,
    claim_hash,
    finding_id,
    stable_id_collisions,
)
from prxref.triage import Finding, parse_unified_diff  # noqa: E402


def _unified(path: str, body_lines: list[str]) -> str:
    """An added-file diff whose post-image is exactly ``body_lines``."""
    body = "\n".join(f"+{text}" for text in body_lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(body_lines)} @@\n"
        f"{body}\n"
    )


def _files(diff: str):
    return parse_unified_diff(diff)


def _finding(
    file: str = "src/config.py",
    line: int = 3,
    title: str = "Drain duration hardcoded",
    *,
    rule: str | None = None,
    body: str = "The retry loop never reads the configured drain duration.",
    drop_reason: str | None = None,
) -> Finding:
    return Finding(
        file=file, line=line, severity="error", confidence=0.9,
        title=title, body=body, rule=rule, drop_reason=drop_reason,
    )


class TestClaimHash:
    def test_reordering_and_punctuating_the_title_keeps_the_hash(self):
        assert claim_hash("Drain duration hardcoded") == claim_hash("hardcoded drain duration")
        assert claim_hash("Drain duration Hardcoded") == claim_hash("drain  duration, hardcoded!")

    def test_a_synonym_swap_changes_the_hash(self):
        # The exact gap the in-run titles_similar reuse bridges (below):
        # a new content word is a new claim set.
        assert claim_hash("Hardcoded timeout value ignored") != claim_hash(
            "Hardcoded timeout period ignored"
        )

    def test_stopwords_and_remedy_verbs_are_not_content(self):
        assert claim_hash("The drain duration is hardcoded") == claim_hash("drain hardcoded duration")
        assert claim_hash("Fix the hardcoded drain duration") == claim_hash("hardcoded drain duration")


class TestFindingId:
    def test_shape_is_file_rule_hash(self):
        f = _finding()
        fid = finding_id(f)
        rule, _, digest = fid.split("#")
        assert rule == "src/config.py"
        assert _ == NO_RULE
        assert len(digest) == 12
        int(digest, 16)  # hex

    def test_the_rule_component_is_casefolded_and_whitespace_collapsed(self):
        assert finding_id(_finding(rule="Never  Commit Secrets")) == finding_id(
            _finding(rule="never commit secrets")
        )

    def test_reworded_title_and_moved_anchor_keep_the_id(self):
        # The #71 acceptance: a rewording plus an anchor one YAML key apart
        # is the same finding.
        a = _finding(file="config/values.yaml", line=4, title="Drain duration hardcoded")
        b = _finding(file="config/values.yaml", line=5, title="Hardcoded drain duration")
        assert finding_id(a) == finding_id(b)


class TestAnchorBlock:
    def test_function_definition_encloses_the_line(self):
        diff = _unified("src/config.py", [
            "def drain_duration():",
            "    cfg = load()",
            "    return cfg",
        ])
        assert anchor_block(_files(diff), _finding("src/config.py", line=3)) == "symbol:drain_duration"

    def test_yaml_key_path_encloses_the_line(self):
        diff = _unified("config/values.yaml", [
            "jobs:",
            "  retry:",
            "    max_attempts: 3",
            "    backoff: fixed",
        ])
        assert anchor_block(
            _files(diff), _finding("config/values.yaml", line=4, title="Backoff not exponential"),
        ) == "yaml:jobs.retry.backoff"

    def test_one_yaml_key_apart_moves_the_anchor_not_the_id(self):
        diff = _unified("config/values.yaml", [
            "jobs:",
            "  retry:",
            "    max_attempts: 3",
            "  drain:",
            "    duration: 0",
        ])
        files = _files(diff)
        at_attempts = _finding("config/values.yaml", line=3, title="Drain duration hardcoded")
        at_duration = _finding("config/values.yaml", line=5, title="Hardcoded drain duration")
        assert anchor_block(files, at_attempts) == "yaml:jobs.retry.max_attempts"
        assert anchor_block(files, at_duration) == "yaml:jobs.drain.duration"
        assert finding_id(at_attempts) == finding_id(at_duration)

    def test_manifest_dependency_key_names_the_line(self):
        diff = _unified("package.json", [
            "{",
            '  "dependencies": {',
            '    "left-pad": "^1.2.0",',
            "  },",
            "}",
        ])
        assert anchor_block(
            _files(diff), _finding("package.json", line=3, title="left-pad unused dependency"),
        ) == "manifest:left-pad"

    def test_file_level_line_zero_and_absent_files_answer_none(self):
        diff = _unified("src/config.py", ["def drain_duration():", "    return 0"])
        files = _files(diff)
        assert anchor_block(files, _finding("src/config.py", line=0)) is None
        assert anchor_block(files, _finding("src/other.py", line=1)) is None
        # A line the post-image does not carry (context or removed) has no
        # anchor to name.
        assert anchor_block(files, _finding("src/config.py", line=99)) is None

    def test_a_language_without_definition_regexes_answers_none(self):
        diff = _unified("notes/todo.txt", ["nothing defines anything here", "still nothing"])
        assert anchor_block(
            _files(diff), _finding("notes/todo.txt", line=2, title="Note contradicts code"),
        ) is None


class TestApplyStableIds:
    def test_stamps_id_anchor_and_null_reuse_on_a_fresh_finding(self):
        diff = _unified("src/config.py", ["def drain_duration():", "    return 0", "    # gap"])
        out = apply_stable_ids([_finding(line=3)], _files(diff))
        assert out[0].id == finding_id(_finding(line=3))
        assert out[0].anchor_block == "symbol:drain_duration"
        assert out[0].id_reused_from is None
        assert out[0].drop_reason is None

    def test_synonym_swap_reuses_the_earlier_finding_id_from_the_same_run(self):
        diff = _unified("src/config.py", [
            "def timeout():", "    return 0", "", "def other():", "    return 1",
        ])
        first = _finding(line=2, title="Hardcoded timeout value ignored")
        second = _finding(line=4, title="Hardcoded timeout period ignored")
        out = apply_stable_ids([first, second], _files(diff))
        assert finding_id(first) != finding_id(second)
        assert out[0].id_reused_from is None
        assert out[1].id == out[0].id
        assert out[1].id_reused_from == REUSED_FROM_RUN

    def test_a_resolved_thread_match_is_recorded_not_dropped(self):
        from prxref.forges.base import Thread

        diff = _unified("src/app.py", ["def render():", "    data = load()", "    return data"])
        f = _finding("src/app.py", line=2, title="Null deref of data", body="data may be None; data loss follows")
        thread = Thread(
            path="src/app.py", line=2, resolved=True, author="alice",
            body_snippet="Null deref: data may be None here — data loss follows",
        )
        out = apply_stable_ids([f], _files(diff), None, [thread])
        assert out[0].drop_reason is None
        assert out[0].id_reused_from == REUSED_FROM_THREAD

    def test_already_dropped_findings_get_their_id_but_no_store_drop(self):
        dropped = _finding(line=0, drop_reason="duplicate of existing thread")
        out = apply_stable_ids([dropped], [])
        assert out[0].id is not None
        assert out[0].id_reused_from is None
        assert out[0].drop_reason == "duplicate of existing thread"


class TestVerdictStore:
    def _store_with_refuted(self, tmp_path: Path, finding: Finding) -> Path:
        path = tmp_path / "verdicts.json"
        verdicts.record(
            path, [finding], {finding_id(finding): verdicts.REFUTED},
            commit="a" * 40, run="run-1",
        )
        return path

    def test_a_refuted_verdict_drops_the_reworded_finding(self, tmp_path):
        diff = _unified("config/values.yaml", [
            "jobs:", "  retry:", "    max_attempts: 3", "  drain:", "    duration: 0",
        ])
        path = self._store_with_refuted(
            tmp_path, _finding("config/values.yaml", line=3, title="Drain duration hardcoded"),
        )
        reworded = _finding("config/values.yaml", line=5, title="Hardcoded drain duration")
        out = apply_stable_ids([reworded], _files(diff), verdicts.load(path))
        fid = finding_id(reworded)
        assert out[0].drop_reason == f"refuted in earlier run ({fid})"
        assert out[0].id == fid
        assert out[0].id_reused_from == REUSED_FROM_VERDICT

    def test_record_stores_the_title_beside_the_verdict(self, tmp_path):
        f = _finding(title="Drain duration is hardcoded instead of derived from values")
        path = self._store_with_refuted(tmp_path, f)
        entry = verdicts.load(path)["verdicts"][finding_id(f)]
        assert entry["title"] == "Drain duration is hardcoded instead of derived from values"

    def test_a_refuted_verdict_drops_a_reworded_duplicate_in_the_next_run(self, tmp_path):
        original = _finding(title="Drain duration is hardcoded instead of derived from values")
        path = self._store_with_refuted(tmp_path, original)
        orig_id = finding_id(original)
        reworded = _finding(line=7, title="drain duration hardcoded; should derive from values")
        assert finding_id(reworded) == orig_id
        out = apply_stable_ids([reworded], [], verdicts.load(path))
        assert out[0].drop_reason == f"refuted in earlier run ({orig_id})"
        assert out[0].id == orig_id
        assert out[0].id_reused_from == REUSED_FROM_VERDICT

    def test_an_unrelated_title_in_the_same_file_does_not_match_the_store(self, tmp_path):
        original = _finding(title="Drain duration is hardcoded instead of derived from values")
        path = self._store_with_refuted(tmp_path, original)
        unrelated = _finding(line=7, title="Retry loop swallows connection errors silently")
        out = apply_stable_ids([unrelated], [], verdicts.load(path))
        assert out[0].drop_reason is None
        assert out[0].id == finding_id(unrelated)
        assert out[0].id_reused_from is None

    def test_store_similarity_is_scoped_to_the_same_file_and_rule(self, tmp_path):
        original = _finding(title="Drain duration is hardcoded instead of derived from values")
        path = self._store_with_refuted(tmp_path, original)
        store = verdicts.load(path)
        title = "drain duration hardcoded; should derive from values"
        other_file = _finding(file="src/other.py", title=title)
        other_rule = _finding(title=title, rule="config hygiene")
        out = apply_stable_ids([other_file, other_rule], [], store)
        assert [f.drop_reason for f in out] == [None, None]
        assert [f.id_reused_from for f in out] == [None, None]

    def test_a_store_entry_without_a_title_matches_by_exact_id_only(self):
        original = _finding(title="Drain duration is hardcoded instead of derived from values")
        store = {"version": 1, "verdicts": {finding_id(original): {"verdict": "refuted"}}}
        reworded = _finding(title="drain period hardcoded; should derive from values")
        assert apply_stable_ids([reworded], [], store)[0].drop_reason is None
        assert apply_stable_ids([original], [], store)[0].drop_reason is not None

    def test_the_closest_stored_title_wins(self):
        target = "drain duration hardcoded; should derive from values"
        close = _finding(title="Drain duration hardcoded; derive from config values today, sadly")
        closer = _finding(title="Drain duration hardcoded, should derive from values")
        store = {"version": 1, "verdicts": {
            finding_id(close): {"verdict": "accepted", "title": close.title},
            finding_id(closer): {"verdict": "refuted", "title": closer.title},
        }}
        out = apply_stable_ids([_finding(title=target + " today")], [], store)
        assert out[0].id == finding_id(closer)
        assert out[0].drop_reason == f"refuted in earlier run ({finding_id(closer)})"

    def test_an_accepted_verdict_marks_the_reuse_but_drops_nothing(self, tmp_path):
        f = _finding()
        path = tmp_path / "verdicts.json"
        verdicts.record(path, [f], {finding_id(f): verdicts.ACCEPTED})
        out = apply_stable_ids([f], [], verdicts.load(path))
        assert out[0].drop_reason is None
        assert out[0].id_reused_from == REUSED_FROM_VERDICT

    def test_an_unknown_verdict_label_warns_naming_the_id_and_drops_nothing(self, caplog):
        f = _finding()
        fid = finding_id(f)
        store = {"version": 1, "verdicts": {fid: {"verdict": "disputed"}}}
        with caplog.at_level(logging.WARNING, logger="prxref.stable_ids"):
            out = apply_stable_ids([f], [], store)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert fid in warnings[0].getMessage()
        assert out[0].drop_reason is None

    def test_a_padded_cased_refuted_label_still_drops(self):
        f = _finding()
        fid = finding_id(f)
        store = {"version": 1, "verdicts": {fid: {"verdict": "Refuted "}}}
        assert apply_stable_ids([f], [], store)[0].drop_reason == f"refuted in earlier run ({fid})"

    def test_an_accepted_label_is_a_quiet_no_op(self, caplog):
        f = _finding()
        store = {"version": 1, "verdicts": {finding_id(f): {"verdict": "accepted"}}}
        with caplog.at_level(logging.WARNING, logger="prxref.stable_ids"):
            apply_stable_ids([f], [], store)
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_no_store_and_no_entry_match_nothing(self):
        out = apply_stable_ids([_finding()], [])
        assert out[0].drop_reason is None
        assert out[0].id_reused_from is None
        assert apply_stable_ids([_finding()], [], {"version": 1, "verdicts": {}})[0].drop_reason is None

    def test_record_merges_atomically_and_validates_ids_and_labels(self, tmp_path):
        path = tmp_path / "nested" / "verdicts.json"
        f = _finding(title="First claim")
        g = _finding(title="Second claim")
        verdicts.record(
            path, [f, g], {finding_id(f): "refuted"},
            commit="c1", run="r1", created_at="2026-01-01T00:00:00+00:00",
        )
        verdicts.record(
            path, [g], {finding_id(g): "accepted"},
            commit="c2", run="r2", created_at="2026-02-02T00:00:00+00:00",
        )
        store = json.loads(path.read_text(encoding="utf-8"))
        assert store["version"] == 1
        assert store["verdicts"][finding_id(f)]["verdict"] == "refuted"
        assert store["verdicts"][finding_id(g)]["verdict"] == "accepted"
        assert not (tmp_path / "nested" / "verdicts.json.tmp").exists()
        with pytest.raises(ConfigError, match="no finding of this run carries"):
            verdicts.record(path, [f], {"src/x.py#norule#000000000000": "refuted"})
        with pytest.raises(ConfigError, match="not one of"):
            verdicts.record(path, [f], {finding_id(f): "maybe"})

    def test_load_reads_a_missing_file_as_empty_and_refuses_a_broken_one(self, tmp_path):
        assert verdicts.load(tmp_path / "absent.json") == {"version": 1, "verdicts": {}}
        bad_json = tmp_path / "bad.json"
        bad_json.write_text("{not json", encoding="utf-8")
        with pytest.raises(ConfigError, match="not valid JSON"):
            verdicts.load(bad_json)
        wrong_version = tmp_path / "old.json"
        wrong_version.write_text(json.dumps({"version": 2, "verdicts": {}}), encoding="utf-8")
        with pytest.raises(ConfigError, match="version"):
            verdicts.load(wrong_version)
        bad_shape = tmp_path / "shape.json"
        bad_shape.write_text(json.dumps({"version": 1, "verdicts": []}), encoding="utf-8")
        with pytest.raises(ConfigError, match="'verdicts' is not an object of objects"):
            verdicts.load(bad_shape)

    def test_lookup_answers_the_entry_for_a_stamped_finding(self, tmp_path):
        f = _finding()
        path = self._store_with_refuted(tmp_path, f)
        stamped = apply_stable_ids([f], [], verdicts.load(path))[0]
        fid, entry = verdicts.lookup(verdicts.load(path), stamped)
        assert fid == stamped.id
        assert entry["verdict"] == "refuted"
        assert verdicts.lookup(None, stamped) is None
        assert verdicts.lookup(verdicts.load(path), _finding(title="Unknown claim")) is None


class TestCollisions:
    def test_two_findings_sharing_an_id_at_different_sites_collide(self, caplog):
        diff = _unified("config/values.yaml", [
            "jobs:", "  retry:", "    max_attempts: 3", "  drain:", "    duration: 0",
        ])
        files = _files(diff)
        a = _finding("config/values.yaml", line=3, title="Drain duration hardcoded")
        b = _finding("config/values.yaml", line=5, title="Hardcoded drain duration")
        with caplog.at_level(logging.WARNING, logger="prxref.stable_ids"):
            out = apply_stable_ids([a, b], files)
        fid = out[0].id
        assert out[0].id == out[1].id == fid
        assert stable_id_collisions(out) == [fid]
        assert any(fid in record.getMessage() for record in caplog.records)

    def test_same_id_same_site_is_not_a_collision(self):
        a = _finding(line=3)
        b = replace(a, confidence=0.8)
        # Identical claim set, identical site: not a collision.
        out = apply_stable_ids([a, b], [])
        assert out[0].id == out[1].id
        assert stable_id_collisions(out) == []


@pytest.mark.usefixtures("contract_stubs")
class TestOrchestratorWiring:
    FINDINGS = {
        "src/app.py": [
            {"file": "src/app.py", "line": 3, "severity": "error", "confidence": 0.9,
             "title": "Null deref", "body": "x may be None when config is missing; data loss follows."},
        ],
    }

    def _run(self, tmp_path: Path, *, stable_ids: bool, store: str | None = None):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        llm = FakeLLM(findings_by_path=self.FINDINGS)
        return orchestrate_review(
            forge, REF, llm, post=True, stable_ids=stable_ids, verdict_store=store,
        ), forge

    def test_off_by_default_no_id_no_record_block_no_summary_trace(self):
        res, forge = self._run(Path("."), stable_ids=False)
        assert res["stable_ids"] is None
        for f in (*res["findings_active"], *res["findings_dropped"]):
            assert f.id is None
            assert f.anchor_block is None
            assert f.id_reused_from is None
        # Byte-identity of the pre-#71 surfaces: no id, no anchor, no
        # stable-id wording reaches the posted summary.
        assert "stable" not in forge.summaries[0].lower()
        assert "#" not in forge.summaries[0]

    def test_on_stamps_ids_and_records_the_tally(self):
        res, _ = self._run(Path("."), stable_ids=True)
        assert res["stable_ids"] == {
            "assigned": len(res["findings_active"]),
            "reused_from_verdict": 0,
            "reused_from_thread": 0,
            "collisions": 0,
        }
        assert all(f.id and f.id.startswith("src/app.py#") for f in res["findings_active"])

    def test_a_refuted_store_entry_drops_the_finding_end_to_end(self, tmp_path):
        finding = Finding(
            file="src/app.py", line=3, severity="error", confidence=0.9,
            title="Null deref", body="x may be None when config is missing; data loss follows.",
        )
        path = tmp_path / "verdicts.json"
        verdicts.record(path, [finding], {finding_id(finding): "refuted"}, run="earlier")
        res, _ = self._run(tmp_path, stable_ids=True, store=str(path))
        assert res["findings_active"] == []
        assert len(res["findings_dropped"]) == 1
        dropped = res["findings_dropped"][0]
        assert dropped.drop_reason == f"refuted in earlier run ({finding_id(finding)})"
        assert dropped.id_reused_from == REUSED_FROM_VERDICT
        assert res["stable_ids"]["reused_from_verdict"] == 1

    def test_a_broken_store_is_a_configuration_error_not_a_degraded_run(self, tmp_path):
        bad = tmp_path / "verdicts.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(ConfigError, match="not valid JSON"):
            self._run(tmp_path, stable_ids=True, store=str(bad))


class TestMorphologicalStability:
    def test_derived_and_derive_hash_alike(self):
        assert claim_hash("Drain duration hardcoded, derived from values") == claim_hash(
            "Drain duration hardcoded, should derive from values"
        )

    def test_inflections_of_one_word_collapse(self):
        assert claim_hash("hardcoded values") == claim_hash("hardcoding value")
        assert claim_hash("cache misses") == claim_hash("cache miss")

    def test_double_s_words_are_not_over_stripped(self):
        assert claim_hash("access denied") != claim_hash("acces denied")

    def test_distinct_claims_still_differ(self):
        assert claim_hash("Hardcoded timeout value ignored") != claim_hash(
            "Hardcoded timeout period ignored"
        )


@pytest.mark.usefixtures("contract_stubs")
class TestTwoRunReplay:
    def _run(self, title: str, line: int, store: str | None):
        findings = {
            "src/app.py": [
                {"file": "src/app.py", "line": line, "severity": "error", "confidence": 0.9,
                 "title": title, "body": "The drain duration never reads the configured values."},
            ],
        }
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        return orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=findings), post=True,
            stable_ids=True, verdict_store=store,
        )

    def test_reworded_title_and_anchor_one_key_off_keep_the_id_and_drop(self, tmp_path):
        first = self._run("Drain duration hardcoded, derived from values", 3, None)
        original = first["findings_active"][0]
        path = tmp_path / "verdicts.json"
        verdicts.record(path, [original], {original.id: verdicts.REFUTED}, run="run-1")
        second = self._run("Drain duration hardcoded, should derive from values", 4, str(path))
        assert second["findings_active"] == []
        dropped = second["findings_dropped"][0]
        assert dropped.id == original.id
        assert dropped.drop_reason == f"refuted in earlier run ({original.id})"

    def test_control_an_open_store_leaves_the_reworded_finding_active(self, tmp_path):
        first = self._run("Drain duration hardcoded, derived from values", 3, None)
        original = first["findings_active"][0]
        path = tmp_path / "verdicts.json"
        verdicts.record(path, [original], {original.id: "accepted"}, run="run-1")
        second = self._run("Drain duration hardcoded, should derive from values", 4, str(path))
        assert len(second["findings_active"]) == 1
        assert second["findings_active"][0].id == original.id


class TestEvalChurnMetric:
    def _record(self, ids: list[str | None], *, dropped=None) -> dict:
        dropped = dropped or [False] * len(ids)
        return {
            "findings": [
                {"id": fid, "drop_reason": "x" if gone else None}
                for fid, gone in zip(ids, dropped, strict=True)
            ],
        }

    def test_identical_ids_score_one_and_disjoint_ids_score_zero(self):
        from prxref.evals import stable_id_reuse

        assert stable_id_reuse(
            self._record(["a#norule#1", "b#norule#2"]),
            self._record(["a#norule#1", "b#norule#2"]),
        ) == 1.0
        assert stable_id_reuse(
            self._record(["a#norule#1"]),
            self._record(["a#norule#2"]),
        ) == 0.0

    def test_half_shared_ids_score_a_half(self):
        from prxref.evals import stable_id_reuse

        assert stable_id_reuse(
            self._record(["a#norule#1", "b#norule#2"]),
            self._record(["a#norule#1", "b#norule#3"]),
        ) == 0.5

    def test_a_side_without_ids_is_not_measurable_not_zero(self):
        from prxref.evals import stable_id_reuse

        assert stable_id_reuse(self._record([None]), self._record(["a#norule#1"])) is None
        assert stable_id_reuse(self._record(["a#norule#1"]), self._record([None])) is None
        assert stable_id_reuse({"findings": []}, self._record(["a#norule#1"])) is None
        assert stable_id_reuse(None, self._record(["a#norule#1"])) is None

    def test_only_active_findings_count(self):
        from prxref.evals import stable_id_reuse

        assert stable_id_reuse(
            self._record(["a#norule#1", "b#norule#2"], dropped=[False, True]),
            self._record(["a#norule#1"]),
        ) == 1.0
