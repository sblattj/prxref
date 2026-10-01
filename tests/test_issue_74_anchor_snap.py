"""Issue #74: anchor snapping, "Also at" verification, and the line-0 rework.

What is pinned here:

- ``apply_anchor_snap``: a finding whose quoted code (backticked span,
  double-quoted span, or the interior of ``catch (`` / ``if (`` / ``for (``
  / ``while (``) occurs in the head file within :data:`ANCHOR_SNAP_WINDOW`
  lines of the MODEL's reported line is anchored on it — including the
  issue's core shape, a demoted (line 0) finding whose defect sits dozens
  of lines outside every hunk. A finding whose snippet the file does not
  hold, whose multi-match nothing breaks, or that sits file-level with no
  snippet at all keeps its line and is marked ``anchor_unverified`` with
  its confidence untouched.
  No reader, an unreadable file, a dropped finding and a deterministic
  check's finding are untouched.
- ``apply_location_verification``: each ``Also at:`` site of a grouped
  representative is re-checked for the representative's own quoted
  snippets near the site; an unverifiable site is dropped and the
  paragraph rebuilt through the shared capped writer.
- The line-0 rework: a file-level sweep copy that restates a file-level
  chunk copy in the same file is now a reworded duplicate, and a
  file-level context-followup question is confirmed on same-file shared
  evidence tokens.
- The wiring: ``orchestrate_review`` feeds the pass the raw model anchor
  through ``model_lines`` and the head file through the chunk-context
  reader, so a far-anchor finding posts at its quoted evidence.
"""
from __future__ import annotations

import pytest

from prxref import quality
from prxref.followup_merge import confirms
from prxref.quality import (
    ANCHOR_SNAP_WINDOW,
    DEFAULT_LINE_TOLERANCE,
    _quoted_snippets,
    apply_anchor_snap,
    apply_location_verification,
    apply_sweep_dedup,
)
from prxref.triage import Finding

APP = "src/app.py"


def _head_file(marker_line: int, marker: str = "itemIndex", total: int = 220) -> str:
    """A head file whose only occurrence of ``marker`` is ``marker_line``."""
    lines = [f"filler line {i}" for i in range(1, total + 1)]
    lines[marker_line - 1] = f"    const idx = {marker} + 1;"
    return "\n".join(lines) + "\n"


def _reader(files: dict[str, str]):
    return lambda path: files.get(path)


def _f(**kw) -> Finding:
    fields = {
        "file": APP,
        "line": 120,
        "severity": "warning",
        "confidence": 0.8,
        "title": "`itemIndex` is read past the bounds",
        "body": "The renderer reads `itemIndex` without a bounds check.",
    }
    fields.update(kw)
    return Finding(**fields)


class TestQuotedSnippets:
    def test_backtick_double_quote_and_control_interiors_qualify_in_order(self):
        snippets = _quoted_snippets(
            "`itemIndex` is unchecked",
            'The claim "parseManifest drops entries" and if (manifest_is_truncated) bail.',
        )
        assert snippets == [
            "itemIndex",
            "parseManifest drops entries",
            "manifest_is_truncated",
        ]

    def test_a_path_citation_is_a_location_not_evidence(self):
        assert _quoted_snippets("See `src/app.py:31`", "") == []

    def test_a_span_without_a_four_char_token_is_dropped(self):
        assert _quoted_snippets("`id` is reused", '"x y" leaks') == []

    def test_trailing_punctuation_is_stripped_and_duplicates_collapse(self):
        assert _quoted_snippets("Uses `itemIndex`.", "Also `itemIndex`, again.") == [
            "itemIndex"
        ]


class TestAnchorSnap:
    def test_a_demoted_finding_lands_on_quoted_evidence_outside_every_hunk(self):
        # The issue #74 shape: the model's line 120 sits past the diff's 40
        # added lines, line align demotes it to file-level, and the quoted
        # `itemIndex` lives at 185 — 65 lines past the last hunk line.
        head = _head_file(185)
        out = apply_anchor_snap(
            [_f(line=0)], read=_reader({APP: head}), model_lines=[120],
        )
        assert out[0].line == 185
        assert out[0].anchor_unverified is False
        assert out[0].confidence == 0.8

    def test_the_window_is_centred_on_the_raw_model_line(self):
        head = _head_file(190)
        # Aligned line 0, model line 120: |190 - 120| <= 100, so it moves.
        out = apply_anchor_snap(
            [_f(line=0)], read=_reader({APP: head}), model_lines=[120],
        )
        assert out[0].line == 190
        out = apply_anchor_snap(
            [_f(line=0)], read=_reader({APP: head}), model_lines=[90],
        )
        assert out[0].line == 190
        # Model line 89: |190 - 89| = 101 > 100, so the window misses it.
        out = apply_anchor_snap(
            [_f(line=0)], read=_reader({APP: head}), model_lines=[89],
        )
        assert out[0].line == 0
        assert out[0].anchor_unverified is False

    def test_a_snippet_absent_from_the_head_file_marks_without_lowering(self):
        out = apply_anchor_snap(
            [_f()], read=_reader({APP: "nothing here\n"}), model_lines=[120],
        )
        assert out[0].line == 120
        assert out[0].anchor_unverified is True
        assert out[0].confidence == 0.8

    def test_a_file_level_finding_with_no_snippet_is_marked(self):
        out = apply_anchor_snap(
            [_f(line=0, title="Manifest handling is fragile", body="Plain prose body.")],
            read=_reader({APP: "any content\n"}),
            model_lines=[0],
        )
        assert out[0].line == 0
        assert out[0].anchor_unverified is True
        assert out[0].confidence == 0.8

    def test_an_unbreakable_tie_keeps_the_line_and_marks_the_finding(self):
        head = "\n".join(
            f"line {i}" + ("  uses itemIndex here" if i in (100, 140) else "")
            for i in range(1, 241)
        )
        out = apply_anchor_snap(
            [_f()], read=_reader({APP: head}), model_lines=[120],
        )
        assert out[0].line == 120
        assert out[0].anchor_unverified is True

    def test_a_tie_inside_a_corroborating_hunk_moves_without_a_mark(self):
        from prxref.triage import parse_unified_diff
        from tests.test_quality import MULTI_HUNK_DIFF

        files = parse_unified_diff(MULTI_HUNK_DIFF)
        # `user_id` occurs on lines 1261 and 1263, equidistant from the
        # model's 1262, but both sit inside hunk 1, whose diff lines share
        # the claim's evidence — so the tie is corroborated, the demoted
        # (line 0) finding moves to the earliest match, and nothing is
        # marked.
        head = "\n".join(
            f"$line {i}" + ("  $input['user_id']" if i in (1261, 1263) else "")
            for i in range(1, 1400)
        )
        finding = Finding(
            file="src/functions.php", line=0, severity="warning", confidence=0.8,
            title="user_id used without a guard",
            body="The added code reads `user_id` from $input unguarded.",
        )
        out = apply_anchor_snap(
            [finding], files=files, read=_reader({"src/functions.php": head}),
            model_lines=[1262],
        )
        assert out[0].line == 1261
        assert out[0].anchor_unverified is False

    def test_a_match_within_tolerance_of_a_vetted_anchor_never_churns(self):
        head = _head_file(122)
        out = apply_anchor_snap(
            [_f(line=120)], read=_reader({APP: head}), model_lines=[120],
        )
        assert out[0].line == 120
        assert out[0].anchor_unverified is False

    def test_a_snippet_present_only_far_outside_the_window_keeps_aligns_verdict(self):
        head = _head_file(235, total=240)
        out = apply_anchor_snap(
            [_f()], read=_reader({APP: head}), model_lines=[120],
        )
        assert out[0].line == 120
        assert out[0].anchor_unverified is False

    def test_no_reader_is_a_no_op(self):
        finding = _f()
        out = apply_anchor_snap([finding], read=None, model_lines=[120])
        assert out == [finding]
        assert out[0] is finding

    def test_an_unreadable_file_leaves_the_finding_untouched(self):
        finding = _f(line=0)
        out = apply_anchor_snap([finding], read=_reader({}), model_lines=[120])
        assert out[0] is finding

    def test_a_deterministic_finding_is_never_moved_or_marked(self):
        head = _head_file(185)
        finding = _f(
            line=0,
            title="Release-shaped PR",
            body="`src/logo.png` is not release machinery (deterministic check, no model)",
        )
        out = apply_anchor_snap(
            [finding], read=_reader({APP: head}), model_lines=[0],
        )
        assert out[0] is finding

    def test_a_dropped_finding_keeps_its_audit_anchor(self):
        head = _head_file(185)
        finding = _f(line=0, drop_reason="duplicate of existing thread")
        out = apply_anchor_snap([finding], read=_reader({APP: head}), model_lines=[120])
        assert out[0] is finding

    def test_a_wrong_sized_model_lines_raises(self):
        with pytest.raises(ValueError, match="2 entries for 1 findings"):
            apply_anchor_snap([_f()], read=_reader({APP: "x\n"}), model_lines=[1, 2])

    def test_the_window_constant_is_the_documented_hundred(self):
        assert ANCHOR_SNAP_WINDOW == 100
        assert not hasattr(quality, "ANCHOR_UNVERIFIED_CONFIDENCE_HIT")
        assert DEFAULT_LINE_TOLERANCE == 5


class TestLocationVerification:
    HEAD = "\n".join(
        f"line {i}" + ("  calls dataLoader here" if i == 12 else "") for i in range(1, 60)
    )

    def _rep(self, locations, body_extra="Also at: `src/app.py:300`, `src/app.py:12`"):
        return Finding(
            file=APP, line=5, severity="warning", confidence=0.9,
            title="`dataLoader` swallows parse errors",
            body=f"The loader calls `dataLoader` here.\n\n{body_extra}",
            locations=tuple((APP, line) for line in locations),
        )

    def test_an_unverifiable_site_is_dropped_and_the_paragraph_rebuilt(self):
        out = apply_location_verification(
            [self._rep([300, 12])], read=_reader({APP: self.HEAD}),
        )
        assert out[0].locations == ((APP, 12),)
        assert out[0].body == (
            "The loader calls `dataLoader` here.\n\nAlso at: `src/app.py:12`"
        )

    def test_a_site_with_the_snippet_stays(self):
        assert self.HEAD.splitlines()[11] == "line 12  calls dataLoader here"

    def test_every_site_dropped_leaves_no_paragraph_and_empty_locations(self):
        out = apply_location_verification(
            [self._rep([300])], read=_reader({APP: "unrelated content\n"}),
        )
        assert out[0].locations == ()
        assert out[0].body == "The loader calls `dataLoader` here."

    def test_a_shrunk_list_longer_than_five_gains_the_more_suffix(self):
        head = "\n".join(
            f"line {i}" + ("  calls dataLoader here" if i % 10 == 2 else "")
            for i in range(1, 90)
        )
        locations = [12, 22, 32, 42, 52, 62, 72, 300]
        paragraph = "Also at: " + ", ".join(f"`{APP}:{n}`" for n in locations)
        out = apply_location_verification(
            [self._rep(locations, paragraph)], read=_reader({APP: head}),
        )
        assert out[0].locations == tuple((APP, n) for n in locations[:-1])
        assert out[0].body.endswith(
            "Also at: `src/app.py:12`, `src/app.py:22`, `src/app.py:32`, "
            "`src/app.py:42`, `src/app.py:52` (+2 more)"
        )

    def test_an_unreadable_site_file_keeps_every_site(self):
        rep = self._rep([300, 12])
        out = apply_location_verification([rep], read=_reader({}))
        assert out[0] is rep

    def test_a_representative_with_no_snippet_keeps_every_site(self):
        rep = Finding(
            file=APP, line=5, severity="warning", confidence=0.9,
            title="Fragile manifest handling",
            body="Plain prose.\n\nAlso at: `src/app.py:300`",
            locations=((APP, 300),),
        )
        out = apply_location_verification([rep], read=_reader({APP: "x\n"}))
        assert out[0] is rep

    def test_no_reader_returns_the_same_content(self):
        rep = self._rep([300, 12])
        out = apply_location_verification([rep], read=None)
        assert out == [rep]

    def test_a_file_level_site_survives_on_any_occurrence_in_its_file(self):
        head = "\n".join(
            f"line {i}" + ("  calls dataLoader here" if i == 155 else "")
            for i in range(1, 200)
        )
        rep = Finding(
            file=APP, line=5, severity="warning", confidence=0.9,
            title="`dataLoader` swallows parse errors",
            body="The loader calls `dataLoader` here.\n\nAlso at: `src/app.py:0`",
            locations=((APP, 0),),
        )
        out = apply_location_verification([rep], read=_reader({APP: head}))
        assert out[0].locations == ((APP, 0),)


class TestLine0DedupJoins:
    """The reworded tier compares line-0 findings per file (#74)."""

    def test_a_file_level_sweep_copy_restating_a_file_level_chunk_copy_drops(self):
        chunk = Finding(
            file=APP, line=0, severity="warning", confidence=0.9,
            title="Use a logging-key constant instead of a string literal key",
            body="Plain body.",
        )
        sweep = Finding(
            file=APP, line=0, severity="warning", confidence=0.8,
            title="Use the logging-key constant instead of the string-literal key",
            body="Plain body.",
        )
        out = apply_sweep_dedup([chunk, sweep], 1, similarity=0.5)
        assert out[0].drop_reason is None
        assert out[1].drop_reason == (
            "duplicate of chunk finding (reworded, similarity 1.00)"
        )

    def test_a_file_level_copy_in_another_file_still_never_compares(self):
        chunk = Finding(
            file="src/a.py", line=0, severity="warning", confidence=0.9,
            title="Use a logging-key constant instead of a string literal key",
            body="Plain body.",
        )
        sweep = Finding(
            file="src/b.py", line=0, severity="warning", confidence=0.8,
            title="Use the logging-key constant instead of the string-literal key",
            body="Plain body.",
        )
        out = apply_sweep_dedup([chunk, sweep], 1, similarity=0.5)
        assert [f.drop_reason for f in out] == [None, None]


class TestFollowupLine0Confirm:
    FLOOR = 0.6

    def test_a_file_level_question_confirms_on_shared_evidence_tokens(self):
        question = Finding(
            file="acme/progress.py", line=0, severity="warning", confidence=0.5,
            title="`parseManifest` drops malformed entries",
            body="`parseManifest` swallows the entry when the manifest is truncated.",
        )
        rerun = Finding(
            file="acme/progress.py", line=0, severity="error", confidence=0.9,
            title="Truncated manifests lose entries",
            body="The reader relies on parseManifest for every entry.",
        )
        assert confirms(question, rerun, [], floor=self.FLOOR)

    def test_one_shared_token_alone_does_not_confirm(self):
        question = Finding(
            file="acme/progress.py", line=0, severity="warning", confidence=0.5,
            title="`parseManifest` drops malformed entries",
            body="`parseManifest` swallows the entry.",
        )
        rerun = Finding(
            file="acme/progress.py", line=0, severity="error", confidence=0.9,
            title="Window scroll misses rows",
            body="An unrelated defect about stale entries is not enough.",
        )
        assert not confirms(question, rerun, [], floor=self.FLOOR)


class TestOrchestratorWiring:
    def test_a_far_anchor_finding_posts_at_its_quoted_evidence(self):
        import json

        from prxref.llm import InvokeResult
        from prxref.orchestrator import orchestrate_review
        from tests.test_orchestrator import REF, FakeForge, _added_file_diff, make_pr

        class _ReadingForge(FakeForge):
            def __init__(self, diff, files):
                super().__init__(diff=diff)
                self._files = files

            def get_file_content(self, ref, path, *, sha):
                return self._files.get(path)

        class _ScriptedLLM:
            def __init__(self, findings):
                self._text = json.dumps({"findings": findings})

            def invoke(self, system, user, *, max_tokens=4096, json_mode=False,
                       timeout_s=60.0):
                sweep = json.dumps({"findings": []})
                return InvokeResult(
                    text=sweep if "systemic sweep" in system else self._text,
                    input_tokens=10, output_tokens=5, model="scripted",
                    backend="fake", elapsed_ms=1,
                )

        raw = [{
            "file": APP, "line": 120, "severity": "warning", "confidence": 0.8,
            "title": "`itemIndex` is read past the bounds",
            "body": "The renderer reads `itemIndex` without a bounds check.",
        }]
        forge = _ReadingForge(
            _added_file_diff(APP, 40), {APP: _head_file(185)},
        )
        forge.pr = make_pr()
        res = orchestrate_review(forge, REF, _ScriptedLLM(raw), post=False, max_workers=1)
        (active,) = res["findings_active"]
        assert active.line == 185
        assert active.anchor_unverified is False
        assert res["findings_dropped"] == []

    def test_an_absent_snippet_marks_the_finding_through_the_pipeline(self):
        import json

        from prxref.llm import InvokeResult
        from prxref.orchestrator import orchestrate_review
        from tests.test_orchestrator import REF, FakeForge, _added_file_diff, make_pr

        class _ReadingForge(FakeForge):
            def __init__(self, diff, files):
                super().__init__(diff=diff)
                self._files = files

            def get_file_content(self, ref, path, *, sha):
                return self._files.get(path)

        class _ScriptedLLM:
            def __init__(self, findings):
                self._text = json.dumps({"findings": findings})

            def invoke(self, system, user, *, max_tokens=4096, json_mode=False,
                       timeout_s=60.0):
                sweep = json.dumps({"findings": []})
                return InvokeResult(
                    text=sweep if "systemic sweep" in system else self._text,
                    input_tokens=10, output_tokens=5, model="scripted",
                    backend="fake", elapsed_ms=1,
                )

        raw = [{
            "file": APP, "line": 120, "severity": "warning", "confidence": 0.8,
            "title": "`itemIndex` is read past the bounds",
            "body": "The renderer reads `itemIndex` without a bounds check.",
        }]
        forge = _ReadingForge(_added_file_diff(APP, 40), {APP: "no marker at all\n"})
        forge.pr = make_pr()
        res = orchestrate_review(forge, REF, _ScriptedLLM(raw), post=False, max_workers=1)
        (active,) = res["findings_active"]
        assert active.line == 0
        assert active.anchor_unverified is True
        assert active.confidence == 0.8
