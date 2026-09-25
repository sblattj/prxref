"""The reworded-duplicate tier of ``apply_sweep_dedup`` (issue #10).

P1-P3 are the reworded same-line pairs quoted in issue #10; N1 and N2 are
the closest different-problem negatives from the issue map, and S1/S2 the
short-title cases that share fewer than three tokens. They are copied from
``tests/test_title_similarity.py`` so this file reads on its own.
"""
from __future__ import annotations

import itertools
import random
from collections.abc import Sequence
from dataclasses import replace

import pytest

from prxref import quality
from prxref.quality import apply_sweep_dedup, normalize_title
from prxref.triage import Finding

THRESHOLD = 0.5

P1_A = "Replace manual null check with Optional in resolve()"
P1_B = "Use Optional instead of manual null check in adapter lookup"
P2_A = "Restore admin-only scope on destructive migration admin endpoints"
P2_B = "Restore admin-only scope on migration admin controller"
P3_A = "Use a logging-key constant instead of a string literal key"
P3_B = "Use a logging-key constant instead of a string-literal key"
P3_C = "Use the logging-key constant instead of the string literal key"

REWORDED = {
    "P1": (P1_A, P1_B, "0.57"),
    "P2": (P2_A, P2_B, "0.57"),
    "P3": (P3_A, P3_B, "1.00"),
}

DIFFERENT_PROBLEM = {
    "N1": (P1_A, "Missing null check on resolve() return value"),
    "N2": (P2_B, "Add rate limiting to migration admin controller"),
    "N3": (
        "Unbounded retry loop in payment webhook handler",
        "Payment webhook handler logs the raw card payload",
    ),
    "N4": (
        "Race condition on cache entry expiry in session store",
        "Session store cache key includes the user email",
    ),
    "S1": ("Missing null check", P1_B),
    "S2": ("SQL injection", "SQL injection in search query"),
}

TRIPLE_A = "Cache entry expiry race"
TRIPLE_B = "Cache entry expiry race in session store"
TRIPLE_C = "Expiry race in session store"

SEVERITIES = ("error", "warning", "spec", "outofscope")


def _base_apply_sweep_dedup(
    findings: Sequence[Finding], sweep_start: int
) -> list[Finding]:
    """The body of ``apply_sweep_dedup`` at 8d36b2e, copied verbatim."""
    start = max(0, sweep_start)
    chunk_keys = {
        (f.file, normalize_title(f.title))
        for f in findings[:start]
        if f.drop_reason is None
    }
    result: list[Finding] = []
    for i, f in enumerate(findings):
        if (
            i >= start
            and f.drop_reason is None
            and (f.file, normalize_title(f.title)) in chunk_keys
        ):
            result.append(replace(f, drop_reason="duplicate of chunk finding"))
        else:
            result.append(f)
    return result


def _f(title: str, **kwargs) -> Finding:
    defaults = {
        "file": "src/app.py",
        "line": 10,
        "severity": "warning",
        "confidence": 0.9,
        "title": title,
        "body": "Details about the finding.",
    }
    defaults.update(kwargs)
    return Finding(**defaults)


def _dedup(
    chunk: Sequence[Finding], sweep: Sequence[Finding], similarity: float | None = THRESHOLD
) -> tuple[list[Finding], list[Finding]]:
    out = apply_sweep_dedup(list(chunk) + list(sweep), len(chunk), similarity=similarity)
    assert len(out) == len(chunk) + len(sweep)
    return out[: len(chunk)], out[len(chunk):]


def _reason(side: str, score: str) -> str:
    return f"duplicate of {side} finding (reworded, similarity {score})"


_POOL = (
    P1_A, P1_B, P2_A, P2_B, P3_A, P3_B,
    "use a logging-key constant instead of a `string literal` key.",
    DIFFERENT_PROBLEM["N1"][1], DIFFERENT_PROBLEM["N2"][1],
    TRIPLE_A, TRIPLE_B, TRIPLE_C, "SQL injection", "",
)


def _random_findings(rng: random.Random) -> list[Finding]:
    return [
        Finding(
            file=rng.choice(["a.py", "b.py"]),
            line=rng.choice([0, 1, 1, 2]),
            severity=rng.choice(SEVERITIES),
            confidence=rng.choice([0.6, 0.7, 0.9, 1.0]),
            title=rng.choice(_POOL),
            body=rng.choice(["b1", "b2"]),
            drop_reason=rng.choice([None, None, None, None, 'hedged: "if"']),
        )
        for _ in range(rng.randint(0, 12))
    ]


class TestNoneIsTheExactTier:
    def test_none_equals_the_pre_change_function_on_randomized_input(self):
        rng = random.Random(20260924)
        cases = 3000
        same = 0
        control_differs = 0
        for _ in range(cases):
            findings = _random_findings(rng)
            start = rng.randint(-2, len(findings) + 2)
            expected = repr(_base_apply_sweep_dedup(list(findings), start))
            default = repr(apply_sweep_dedup(list(findings), start))
            explicit = repr(apply_sweep_dedup(list(findings), start, similarity=None))
            if expected == default == explicit:
                same += 1
            if repr(apply_sweep_dedup(list(findings), start, similarity=THRESHOLD)) != expected:
                control_differs += 1
        assert same == cases
        assert control_differs > 0

    def test_none_never_scores_a_title(self, monkeypatch):
        def refuse(*_args, **_kwargs):
            raise AssertionError("the similarity tier ran")

        monkeypatch.setattr(quality, "titles_similar", refuse)
        monkeypatch.setattr(quality, "title_similarity", refuse)
        chunk, sweep = [_f(P1_A)], [_f(P1_B)]
        _dedup(chunk, sweep, similarity=None)
        with pytest.raises(AssertionError, match="the similarity tier ran"):
            _dedup(chunk, sweep, similarity=THRESHOLD)

    def test_similarity_is_keyword_only(self):
        with pytest.raises(TypeError):
            apply_sweep_dedup([_f(P1_A), _f(P1_B)], 1, THRESHOLD)


class TestRewordedPairs:
    @pytest.mark.parametrize("name", sorted(REWORDED))
    def test_reworded_sweep_copy_is_dropped(self, name):
        chunk_title, sweep_title, score = REWORDED[name]
        chunk, sweep = _dedup([_f(chunk_title)], [_f(sweep_title)])
        assert chunk[0].drop_reason is None
        assert sweep[0].drop_reason == _reason("chunk", score)

    @pytest.mark.parametrize("name", sorted(REWORDED))
    def test_either_wording_on_the_chunk_side(self, name):
        sweep_title, chunk_title, score = REWORDED[name]
        chunk, sweep = _dedup([_f(chunk_title)], [_f(sweep_title)])
        assert chunk[0].drop_reason is None
        assert sweep[0].drop_reason == _reason("chunk", score)

    @pytest.mark.parametrize("name", sorted(DIFFERENT_PROBLEM))
    def test_different_problem_pair_is_kept(self, name):
        a, b = DIFFERENT_PROBLEM[name]
        chunk, sweep = _dedup([_f(a)], [_f(b)])
        assert [f.drop_reason for f in chunk + sweep] == [None, None]
        chunk, sweep = _dedup([_f(a), _f(b)], [])
        assert [f.drop_reason for f in chunk] == [None, None]

    def test_without_a_threshold_the_reworded_pair_is_kept(self):
        chunk, sweep = _dedup([_f(P1_A)], [_f(P1_B)], similarity=None)
        assert [f.drop_reason for f in chunk + sweep] == [None, None]

    def test_threshold_is_inclusive_and_read(self):
        _, sweep = _dedup([_f(P1_A)], [_f(P1_B)], similarity=4 / 7)
        assert sweep[0].drop_reason == _reason("chunk", "0.57")
        _, sweep = _dedup([_f(P1_A)], [_f(P1_B)], similarity=0.6)
        assert sweep[0].drop_reason is None

    def test_dropped_finding_is_marked_not_removed(self):
        original = _f(P1_B, severity="warning", confidence=0.7, body="sweep body")
        _, sweep = _dedup([_f(P1_A)], [original])
        assert sweep[0] == replace(original, drop_reason=_reason("chunk", "0.57"))

    def test_exact_title_keeps_the_exact_tier_reason(self):
        chunk, sweep = _dedup(
            [_f(P3_A, line=10)],
            [_f(P3_A.upper(), line=10), _f(P3_A, line=40)],
        )
        assert chunk[0].drop_reason is None
        assert [f.drop_reason for f in sweep] == ["duplicate of chunk finding"] * 2

    def test_a_finding_dropped_earlier_keeps_its_reason_and_suppresses_nothing(self):
        gated = _f(P1_A, drop_reason="confidence 0.40 below floor 0.60")
        chunk, sweep = _dedup([gated], [_f(P1_B)])
        assert chunk[0].drop_reason == "confidence 0.40 below floor 0.60"
        assert sweep[0].drop_reason is None
        hedged = _f(P1_B, drop_reason='hedged: "if"')
        _, sweep = _dedup([_f(P1_A)], [hedged])
        assert sweep[0].drop_reason == 'hedged: "if"'


class TestLineRule:
    def test_line_zero_pair_is_never_compared(self):
        chunk, sweep = _dedup([_f(P3_A, line=0)], [_f(P3_B, line=0)])
        assert [f.drop_reason for f in chunk + sweep] == [None, None]
        chunk, _ = _dedup([_f(P1_A, line=0), _f(P1_B, line=0)], [])
        assert [f.drop_reason for f in chunk] == [None, None]

    def test_line_zero_against_an_anchored_copy_is_not_compared(self):
        chunk, sweep = _dedup([_f(P1_A, line=10)], [_f(P1_B, line=0)])
        assert [f.drop_reason for f in chunk + sweep] == [None, None]

    def test_same_line_control_is_dropped(self):
        _, sweep = _dedup([_f(P3_A, line=10)], [_f(P3_B, line=10)])
        assert sweep[0].drop_reason == _reason("chunk", "1.00")

    @pytest.mark.parametrize("sweep_line", [9, 11, 15])
    def test_a_nearby_line_is_not_the_same_line(self, sweep_line):
        chunk, sweep = _dedup([_f(P3_A, line=10)], [_f(P3_B, line=sweep_line)])
        assert [f.drop_reason for f in chunk + sweep] == [None, None]

    def test_another_file_is_never_compared(self):
        chunk, sweep = _dedup([_f(P3_A, file="src/a.py")], [_f(P3_B, file="src/b.py")])
        assert [f.drop_reason for f in chunk + sweep] == [None, None]


class TestKeepRuleAcrossTheBoundary:
    @pytest.mark.parametrize(
        ("chunk_severity", "sweep_severity"),
        list(itertools.product(SEVERITIES, repeat=2)),
    )
    def test_the_chunk_copy_always_survives(self, chunk_severity, sweep_severity):
        chunk, sweep = _dedup(
            [_f(P1_A, severity=chunk_severity, confidence=0.6)],
            [_f(P1_B, severity=sweep_severity, confidence=1.0)],
        )
        assert chunk[0].drop_reason is None
        sweep_more_severe = SEVERITIES.index(sweep_severity) < SEVERITIES.index(chunk_severity)
        expected = None if sweep_more_severe else _reason("chunk", "0.57")
        assert sweep[0].drop_reason == expected

    def test_a_more_severe_sweep_copy_survives_beside_the_chunk_copy(self):
        chunk, sweep = _dedup(
            [_f(P2_A, severity="warning")], [_f(P2_B, severity="error")]
        )
        assert [(f.severity, f.drop_reason) for f in chunk + sweep] == [
            ("warning", None),
            ("error", None),
        ]

    def test_confidence_does_not_decide_across_the_boundary(self):
        chunk, sweep = _dedup(
            [_f(P1_A, confidence=0.6)], [_f(P1_B, confidence=1.0)]
        )
        assert chunk[0].drop_reason is None
        assert sweep[0].drop_reason == _reason("chunk", "0.57")

    def test_randomized_invariants_of_the_keep_rule(self):
        rng = random.Random(3)
        tier_drops = 0
        for _ in range(2000):
            findings = _random_findings(rng)
            start = rng.randint(0, len(findings))
            exact = apply_sweep_dedup(findings, start)
            tiered = apply_sweep_dedup(findings, start, similarity=THRESHOLD)
            worst_before: dict[tuple[str, int], int] = {}
            worst_after: dict[tuple[str, int], int] = {}
            for i, (before, after) in enumerate(zip(exact, tiered, strict=True)):
                if before.drop_reason is not None:
                    assert after == before
                    continue
                rank = SEVERITIES.index(before.severity)
                key = (before.file, before.line)
                worst_before[key] = min(worst_before.get(key, rank), rank)
                if after.drop_reason is None:
                    worst_after[key] = min(worst_after.get(key, rank), rank)
                    continue
                tier_drops += 1
                assert before.line > 0
                assert after.drop_reason.startswith("duplicate of ")
                if i < start:
                    assert after.drop_reason.startswith("duplicate of chunk finding (reworded")
            assert worst_after == worst_before
        assert tier_drops > 0

    def test_the_sweep_copy_is_compared_with_every_kept_chunk_copy(self):
        chunk, sweep = _dedup(
            [_f(P2_A, severity="warning"), _f(P1_A, severity="error")],
            [_f(P2_B, severity="warning")],
        )
        assert [f.drop_reason for f in chunk] == [None, None]
        assert sweep[0].drop_reason == _reason("chunk", "0.57")

    def test_a_chunk_match_outranks_a_sweep_match_in_the_reason(self):
        _, sweep = _dedup(
            [_f(P3_A, severity="warning")],
            [_f(P3_B, severity="error"), _f(P3_C, severity="warning")],
        )
        assert sweep[0].drop_reason is None
        assert sweep[1].drop_reason == _reason("chunk", "1.00")


class TestKeepRuleOnOneSide:
    @pytest.mark.parametrize("side", ["chunk", "sweep"])
    def test_higher_confidence_wins_at_equal_severity(self, side):
        pair = [_f(P1_A, confidence=0.7), _f(P1_B, confidence=0.9)]
        chunk, sweep = _dedup(pair, []) if side == "chunk" else _dedup([], pair)
        out = chunk + sweep
        assert out[0].drop_reason == _reason(side, "0.57")
        assert out[1].drop_reason is None

    @pytest.mark.parametrize("side", ["chunk", "sweep"])
    def test_higher_severity_beats_higher_confidence(self, side):
        pair = [_f(P1_A, severity="error", confidence=0.6), _f(P1_B, severity="warning", confidence=1.0)]
        chunk, sweep = _dedup(pair, []) if side == "chunk" else _dedup([], pair)
        out = chunk + sweep
        assert out[0].drop_reason is None
        assert out[1].drop_reason == _reason(side, "0.57")

    def test_full_ties_break_on_content_not_arrival(self):
        pair = [_f(P1_A), _f(P1_B)]
        forward, _ = _dedup(pair, [])
        backward, _ = _dedup(list(reversed(pair)), [])
        kept_forward = [f.title for f in forward if f.drop_reason is None]
        kept_backward = [f.title for f in backward if f.drop_reason is None]
        assert kept_forward == kept_backward == [P1_A]

    def test_a_dropped_copy_never_drops_a_third(self):
        chunk, _ = _dedup(
            [
                _f(TRIPLE_C, confidence=0.8),
                _f(TRIPLE_B, confidence=0.9),
                _f(TRIPLE_A, confidence=0.95),
            ],
            [],
        )
        assert [f.drop_reason for f in chunk] == [None, _reason("chunk", "0.67"), None]

    def test_a_sweep_copy_kept_for_severity_still_absorbs_a_weaker_sweep_copy(self):
        chunk, sweep = _dedup(
            [_f(P3_A, severity="spec")],
            [_f(P3_B, severity="error"), _f(P3_C, severity="warning")],
        )
        assert chunk[0].drop_reason is None
        assert sweep[0].drop_reason is None
        assert sweep[1].drop_reason == _reason("sweep", "1.00")


class TestOrderIndependence:
    def _cluster(self) -> tuple[list[Finding], list[Finding]]:
        chunk = [
            _f(P1_A, severity="warning", confidence=0.7),
            _f(P1_B, severity="warning", confidence=0.9),
            _f(TRIPLE_A, severity="warning", confidence=0.95),
            _f(TRIPLE_B, severity="warning", confidence=0.9),
            _f(TRIPLE_C, severity="warning", confidence=0.8),
            _f(P3_A, line=0),
        ]
        sweep = [
            _f("Replace the manual null check with Optional in resolve() call", confidence=1.0),
            _f(P1_B + " path", severity="error", confidence=0.6),
            _f("Session store cache entry expiry race", severity="error", confidence=0.9),
            _f("Expiry race in the session store", severity="error", confidence=0.7),
            _f(P3_B, line=0),
        ]
        return chunk, sweep

    def test_the_cluster_resolves_to_the_expected_survivors(self):
        chunk, sweep = self._cluster()
        out_c, out_s = _dedup(chunk, sweep)
        assert [f.drop_reason for f in out_c] == [
            _reason("chunk", "0.57"), None, None, _reason("chunk", "0.67"), None, None,
        ]
        assert [f.drop_reason for f in out_s] == [
            _reason("chunk", "0.50"), None, None, _reason("sweep", "0.67"), None,
        ]

    def test_every_shuffle_of_each_side_gives_one_result(self):
        chunk, sweep = self._cluster()
        out_c, out_s = _dedup(chunk, sweep)
        reference = sorted(map(repr, out_c + out_s))
        rng = random.Random(7)
        for _ in range(200):
            c, s = list(chunk), list(sweep)
            rng.shuffle(c)
            rng.shuffle(s)
            out_c, out_s = _dedup(c, s)
            assert sorted(map(repr, out_c + out_s)) == reference
            for before, after in zip(c + s, out_c + out_s, strict=True):
                assert replace(after, drop_reason=None) == before

    def test_randomized_inputs_are_order_independent(self):
        rng = random.Random(10)
        for _ in range(1000):
            findings = _random_findings(rng)
            start = rng.randint(0, len(findings))
            chunk, sweep = findings[:start], findings[start:]
            reference = sorted(map(repr, apply_sweep_dedup(findings, start, similarity=THRESHOLD)))
            rng.shuffle(chunk)
            rng.shuffle(sweep)
            shuffled = apply_sweep_dedup(chunk + sweep, start, similarity=THRESHOLD)
            assert sorted(map(repr, shuffled)) == reference
