"""Issue #12: ``match_globs``, the path matcher behind ``applies_to``.

Pinned here, as one table of ``(path, patterns, selected)`` rows:

- stdlib ``fnmatchcase`` semantics, shared with ``PRXREF_SIZE_IGNORE_GLOBS``:
  case-sensitive, ``*`` crosses ``/``, the whole path must match;
- the zero-directory ``**/`` fold: a ``**/`` that starts the pattern or
  follows a ``/`` also matches no directory at all, so a root-level file
  matches ``**/*.java`` and ``src/Foo.java`` matches ``src/**/*.java``;
- negation: a ``!`` pattern vetoes the path wherever it sits in the list, and
  a later positive pattern cannot bring the path back;
- an empty list, and a list of negations only, select nothing;
- the issue's own ``["**/*.java", "!**/src/test/**"]`` example.

The fold is for ``applies_to`` only (decisions #12): the size advisory's
``is_size_ignored`` keeps plain ``fnmatchcase``, and the last class pins that.
"""
from __future__ import annotations

import fnmatch

import pytest

from prxref.rules import match_globs
from prxref.triage import is_size_ignored

ISSUE_EXAMPLE = ("**/*.java", "!**/src/test/**")

ROOT_LEVEL = [
    ("Foo.java", ["**/*.java"], True),
    ("pom.xml", ["**/*.java"], False),
    ("Foo.java", ["**/**/*.java"], True),
    ("Foo.java", ["**/**/**/*.java"], True),
    ("README.md", ["**"], True),
    ("README.md", ["**/*"], True),
    ("Chart.yaml", ["**/Chart.yaml"], True),
    ("xChart.yaml", ["**/Chart.yaml"], False),
]

NESTED = [
    ("src/main/java/Foo.java", ["**/*.java"], True),
    ("a/b/c/d/e/Foo.java", ["**/*.java"], True),
    ("src/Foo.java", ["src/*.java"], True),
    ("src/deep/Foo.java", ["src/*.java"], True),
    ("lib/Foo.java", ["src/*.java"], False),
    ("docs/guide.md", ["docs/**"], True),
    ("docs/a/b/guide.md", ["docs/**"], True),
    ("docs", ["docs/**"], False),
    ("helm/charts/app/Chart.yaml", ["**/Chart.yaml"], True),
    ("src/Foo.java", ["Foo.java"], False),
]

MIDDLE_DOUBLESTAR = [
    ("src/Foo.java", ["src/**/*.java"], True),
    ("src/a/b/Foo.java", ["src/**/*.java"], True),
    ("lib/src/Foo.java", ["src/**/*.java"], False),
    ("charts/templates/deploy.yaml", ["charts/**/templates/*.yaml"], True),
    ("charts/app/templates/deploy.yaml", ["charts/**/templates/*.yaml"], True),
    ("charts/app/values.yaml", ["charts/**/templates/*.yaml"], False),
    ("src/Foo.java", ["**/src/**/*.java"], True),
    ("mod/src/Foo.java", ["**/src/**/*.java"], True),
    ("mod/src/a/b/Foo.java", ["**/src/**/*.java"], True),
    ("mod/lib/Foo.java", ["**/src/**/*.java"], False),
    ("a/b", ["a/**/**/b"], True),
    ("a/x/y/b", ["a/**/**/b"], True),
    ("a/b", ["a**/b"], True),
    ("ab", ["a**/b"], False),
    ("xb", ["x**/b"], False),
    ("src/Foo.java", ["src/F**/oo.java"], False),
]

NEGATION = [
    ("src/test/A.java", ["**/*.java", "!**/src/test/**", "**/src/test/**/*.java"], False),
    ("src/test/A.java", ["**/src/test/**/*.java", "!**/src/test/**"], False),
    ("src/main/A.java", ["!**/src/test/**"], False),
    ("src/main/A.java", ["!**/src/test/**", "!**/*.md"], False),
    ("src/main/A.java", ["**/*.java", "!**/*.md"], True),
    ("README.md", ["**/*.java", "!**/*.md"], False),
    ("gen/A.java", ["**/*.java", "!gen/**", "!**/*Test.java"], False),
    ("src/ATest.java", ["**/*.java", "!gen/**", "!**/*Test.java"], False),
    ("src/A.java", ["**/*.java", "!gen/**", "!**/*Test.java"], True),
    ("a!b.txt", ["a!b.txt"], True),
    ("!notes.md", ["?notes.md"], True),
    ("!notes.md", ["!notes.md"], False),
]

EMPTY = [
    ("Foo.java", [], False),
    ("Foo.java", (), False),
    ("README.md", [], False),
    ("src/deep/path/x.py", [], False),
]

LETTER_CASE = [
    ("Foo.JAVA", ["**/*.java"], False),
    ("Foo.java", ["**/*.JAVA"], False),
    ("SRC/Foo.java", ["src/**/*.java"], False),
    ("src/Foo.java", ["SRC/**"], False),
    ("README.md", ["readme.md"], False),
    ("readme.md", ["readme.md"], True),
    ("Src/Test/A.java", list(ISSUE_EXAMPLE), True),
    ("src/Test/A.java", ["**/*.java", "!**/src/test/**"], True),
]

ISSUE_PATHS = [
    ("Foo.java", True),
    ("src/main/java/com/acme/Foo.java", True),
    ("service/src/main/java/Foo.java", True),
    ("src/test/java/FooTest.java", False),
    ("service/src/test/java/FooTest.java", False),
    ("src/test/Foo.java", False),
    ("src/main/resources/application.yaml", False),
    ("helm/values.yaml", False),
    ("src/testing/Foo.java", True),
    ("mysrc/test/Foo.java", True),
]


def _rows(label, rows):
    return [pytest.param(path, patterns, selected, id=f"{label}:{path}:{','.join(patterns)}")
            for path, patterns, selected in rows]


TABLE = (
    _rows("root", ROOT_LEVEL)
    + _rows("nested", NESTED)
    + _rows("middle", MIDDLE_DOUBLESTAR)
    + _rows("negation", NEGATION)
    + _rows("empty", EMPTY)
    + _rows("case", LETTER_CASE)
)


@pytest.mark.parametrize(("path", "patterns", "selected"), TABLE)
def test_match_globs_table(path, patterns, selected):
    assert match_globs(path, patterns) is selected


@pytest.mark.parametrize(("path", "selected"), ISSUE_PATHS, ids=[p for p, _ in ISSUE_PATHS])
class TestIssueExample:
    """The issue's ``applies_to: ["**/*.java", "!**/src/test/**"]``, in both orders."""

    def test_as_written(self, path, selected):
        assert match_globs(path, list(ISSUE_EXAMPLE)) is selected

    def test_reversed(self, path, selected):
        assert match_globs(path, list(reversed(ISSUE_EXAMPLE))) is selected

    def test_as_tuple(self, path, selected):
        assert match_globs(path, ISSUE_EXAMPLE) is selected


@pytest.mark.parametrize(("path", "patterns", "_selected"), _rows("order", NEGATION + LETTER_CASE))
def test_order_never_changes_the_result(path, patterns, _selected):
    forward = match_globs(path, list(patterns))
    assert match_globs(path, list(reversed(patterns))) is forward


@pytest.mark.parametrize(("path", "pattern"), [
    ("Foo.java", "**/*.java"),
    ("src/test/A.java", "**/src/test/**"),
    ("src/Foo.java", "src/**/*.java"),
    ("charts/templates/deploy.yaml", "charts/**/templates/*.yaml"),
])
class TestZeroDirectoryFold:
    """Each fold row is a path stdlib ``fnmatchcase`` misses and ``match_globs`` selects."""

    def test_stdlib_fnmatch_misses_it(self, path, pattern):
        assert fnmatch.fnmatchcase(path, pattern) is False

    def test_match_globs_selects_it(self, path, pattern):
        assert match_globs(path, [pattern]) is True

    def test_a_negation_vetoes_it(self, path, pattern):
        assert match_globs(path, ["**", f"!{pattern}"]) is False

    def test_size_advisory_keeps_plain_fnmatch(self, path, pattern):
        assert is_size_ignored(path, ignore_globs=[pattern]) is False


def test_patterns_are_not_consumed():
    patterns = ["**/*.java", "!**/src/test/**"]
    assert match_globs("Foo.java", patterns) is True
    assert match_globs("src/test/A.java", patterns) is False
    assert patterns == ["**/*.java", "!**/src/test/**"]
