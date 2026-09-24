"""Tests for the reworded-duplicate title scorer in prxref.quality (issue #10).

P1-P3 are the three reworded same-line pairs quoted in issue #10. N1 and N2
are the closest different-problem negatives from the issue map; N3-N6 are
further same-line different-problem pairs written for this suite. S1 and S2
are the short-title cases that an overlap coefficient over-merges.
"""
from __future__ import annotations

import pytest

from prxref.quality import (
    TITLE_MIN_SHARED_TOKENS,
    _title_tokens,
    title_similarity,
    titles_similar,
)

THRESHOLD = 0.5

P1_A = "Replace manual null check with Optional in resolve()"
P1_B = "Use Optional instead of manual null check in adapter lookup"
P2_A = "Restore admin-only scope on destructive migration admin endpoints"
P2_B = "Restore admin-only scope on migration admin controller"
P3_A = "Use a logging-key constant instead of a string literal key"
P3_B = "Use a logging-key constant instead of a string-literal key"

REWORDED = {
    "P1": (P1_A, P1_B),
    "P2": (P2_A, P2_B),
    "P3": (P3_A, P3_B),
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
    "N5": (
        "Missing await on async save in order repository",
        "Order repository save swallows persistence exceptions",
    ),
    "N6": (
        "Off-by-one in pagination offset for user list endpoint",
        "User list endpoint returns soft-deleted users",
    ),
}

SHORT_TITLE = {
    "S1-chunk": ("Missing null check", P1_A),
    "S1-sweep": ("Missing null check", P1_B),
    "S2": ("SQL injection", "SQL injection in search query"),
}

ALL_PAIRS = {**REWORDED, **DIFFERENT_PROBLEM, **SHORT_TITLE}


class TestReworded:
    @pytest.mark.parametrize("name", sorted(REWORDED))
    def test_reworded_pair_is_similar(self, name):
        a, b = REWORDED[name]
        assert titles_similar(a, b, THRESHOLD) is True

    @pytest.mark.parametrize(
        ("name", "expected"),
        [("P1", (4 / 7, 4)), ("P2", (4 / 7, 4)), ("P3", (1.0, 5))],
    )
    def test_reworded_pair_scores(self, name, expected):
        assert title_similarity(*REWORDED[name]) == expected

    def test_identical_titles_score_one(self):
        assert title_similarity(P1_A, P1_A) == (1.0, 5)


class TestNotSimilar:
    @pytest.mark.parametrize("name", sorted(DIFFERENT_PROBLEM))
    def test_different_problem_pair_is_not_similar(self, name):
        a, b = DIFFERENT_PROBLEM[name]
        assert titles_similar(a, b, THRESHOLD) is False

    @pytest.mark.parametrize("name", sorted(DIFFERENT_PROBLEM))
    def test_different_problem_pair_is_kept_by_the_threshold(self, name):
        jaccard, shared = title_similarity(*DIFFERENT_PROBLEM[name])
        assert shared >= TITLE_MIN_SHARED_TOKENS
        assert jaccard < THRESHOLD

    @pytest.mark.parametrize("name", sorted(SHORT_TITLE))
    def test_short_title_is_not_similar(self, name):
        a, b = SHORT_TITLE[name]
        assert titles_similar(a, b, THRESHOLD) is False

    def test_short_title_is_kept_by_the_token_floor_alone(self):
        a, b = SHORT_TITLE["S2"]
        assert title_similarity(a, b) == (0.5, 2)
        assert titles_similar(a, b, THRESHOLD) is False
        assert titles_similar(a, b, 0.1) is False

    def test_scorer_separates_every_reworded_pair_from_every_negative(self):
        reworded = [title_similarity(*pair)[0] for pair in REWORDED.values()]
        negatives = [title_similarity(*pair)[0] for pair in DIFFERENT_PROBLEM.values()]
        assert min(reworded) > max(negatives)

    def test_p1_outscores_n2(self):
        assert title_similarity(*REWORDED["P1"])[0] > title_similarity(*DIFFERENT_PROBLEM["N2"])[0]


class TestThreshold:
    def test_threshold_is_honoured(self):
        assert titles_similar(P1_A, P1_B, 0.5) is True
        assert titles_similar(P1_A, P1_B, 0.6) is False

    def test_lower_threshold_admits_a_pair_with_enough_shared_tokens(self):
        a, b = DIFFERENT_PROBLEM["N1"]
        assert title_similarity(a, b) == (3 / 7, 3)
        assert titles_similar(a, b, 0.4) is True

    def test_min_shared_tokens_is_three(self):
        assert TITLE_MIN_SHARED_TOKENS == 3


class TestTitleTokens:
    def test_keeps_words_the_thread_tokenizer_drops(self):
        assert _title_tokens(P3_A) == {"logging", "key", "constant", "string", "literal"}
        assert {"null", "check"} <= _title_tokens(P1_A)

    def test_drops_function_words_generic_verbs_and_short_words(self):
        assert _title_tokens("Use the id of a DB instead") == set()
        assert _title_tokens("Replace or restore the missing lock") == {"lock"}

    def test_hyphen_and_space_tokenize_alike(self):
        assert _title_tokens("string-literal key") == _title_tokens("string literal key")

    def test_case_quotes_and_backticks_are_ignored(self):
        assert _title_tokens("`Resolve()` returns \"NULL\"") == _title_tokens("resolve() returns null")

    def test_word_order_does_not_matter(self):
        assert title_similarity("manual null check", "check null manual") == (1.0, 3)


class TestEdges:
    @pytest.mark.parametrize(
        ("a", "b"),
        [("", ""), ("!!!", "..."), ("", P1_A), ("Use the DB", "Add an id")],
    )
    def test_titles_without_shared_tokens_score_zero(self, a, b):
        assert title_similarity(a, b) == (0.0, 0)
        assert titles_similar(a, b, 0.0) is False


class TestSymmetryAndDeterminism:
    @pytest.mark.parametrize("name", sorted(ALL_PAIRS))
    def test_symmetric(self, name):
        a, b = ALL_PAIRS[name]
        assert title_similarity(a, b) == title_similarity(b, a)
        assert titles_similar(a, b, THRESHOLD) == titles_similar(b, a, THRESHOLD)

    @pytest.mark.parametrize("name", sorted(ALL_PAIRS))
    def test_deterministic(self, name):
        a, b = ALL_PAIRS[name]
        first = title_similarity(a, b)
        assert all(title_similarity(a, b) == first for _ in range(5))
        verdict = titles_similar(a, b, THRESHOLD)
        assert all(titles_similar(a, b, THRESHOLD) is verdict for _ in range(5))
