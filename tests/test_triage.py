"""Tests for prxref.triage: unified-diff parsing, risk scoring, and chunking."""
from __future__ import annotations

from prxref.triage import (
    DiffLine,
    FileDiff,
    Finding,
    Hunk,
    added_lines_by_file,
    build_chunks,
    parse_unified_diff,
    score_file,
    trim_hunk_context,
)


class TestBitbucketServerDiffPrefixes:
    """Bitbucket Server / Data Center emits ``src://``/``dst://`` where git
    emits ``a/``/``b/``.

    The header and operand shapes below are copied verbatim from a real
    Bitbucket Data Center 10.4.2 pull-request diff. The hand-written Server
    fixtures elsewhere in the suite all use git-style prefixes, which is
    exactly why an unstripped ``dst://`` survived into every reported location
    without a test noticing.
    """

    SERVER_DIFF = (
        "diff --git src://cache.py dst://cache.py\n"
        "index 3cc2d49..4a62838 100644\n"
        "--- src://cache.py\n"
        "+++ dst://cache.py\n"
        "@@ -17,3 +17,4 @@ class TTLCache:\n"
        " context1\n"
        " context2\n"
        " context3\n"
        "+added_line\n"
    )

    GIT_DIFF = (
        SERVER_DIFF
        .replace("src://", "a/")
        .replace("dst://", "b/")
    )

    def test_server_prefixes_are_stripped(self):
        files = parse_unified_diff(self.SERVER_DIFF)
        assert len(files) == 1
        assert files[0].path == "cache.py"
        assert files[0].old_path == "cache.py"
        assert files[0].new_path == "cache.py"

    def test_server_form_parses_identically_to_the_git_form(self):
        """The control: the same diff in git spelling must come out the same."""
        server = parse_unified_diff(self.SERVER_DIFF)
        git = parse_unified_diff(self.GIT_DIFF)
        assert [f.path for f in server] == [f.path for f in git] == ["cache.py"]
        assert server[0].added_lines == git[0].added_lines

    def test_server_header_is_matched_not_just_the_operands(self):
        """Without the header pattern the file is still recovered from
        ``---``/``+++``, so assert the header itself parses -- otherwise a
        header-only regression stays invisible."""
        header_only = (
            "diff --git src://pkg/mod.py dst://pkg/mod.py\n"
            "@@ -1,2 +1,3 @@\n"
            " context\n"
            "+added\n"
        )
        files = parse_unified_diff(header_only)
        assert len(files) == 1
        assert files[0].path == "pkg/mod.py"

    def test_git_prefixes_still_work(self):
        assert parse_unified_diff(self.GIT_DIFF)[0].path == "cache.py"


class TestUnifiedDiffParsing:
    def test_parses_single_modified_file(self):
        diff = (
            "diff --git a/src/app.py b/src/app.py\n"
            "index 111..222 100644\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -10,3 +10,4 @@ def main():\n"
            " context1\n"
            "-removed\n"
            "+added1\n"
            "+added2\n"
            " context2\n"
        )
        files = parse_unified_diff(diff)
        assert len(files) == 1
        f = files[0]
        assert f.path == "src/app.py"
        assert f.status == "modified"
        assert not f.is_binary
        assert f.lines_added == 2
        assert f.lines_removed == 1
        assert f.added_lines == {11, 12}
        assert len(f.hunks) == 1
        assert f.hunks[0].old_start == 10
        assert f.hunks[0].new_start == 10

    def test_parses_multiple_files(self):
        diff = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,1 +1,2 @@\n"
            " x\n"
            "+y\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -5,2 +5,1 @@\n"
            "-old\n"
            " keep\n"
        )
        files = parse_unified_diff(diff)
        assert len(files) == 2
        assert files[0].path == "a.py"
        assert files[0].lines_added == 1
        assert files[1].path == "b.py"
        assert files[1].lines_removed == 1

    def test_parses_new_file_addition(self):
        diff = (
            "diff --git a/new.py b/new.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+line1\n"
            "+line2\n"
        )
        files = parse_unified_diff(diff)
        assert len(files) == 1
        f = files[0]
        assert f.path == "new.py"
        assert f.status == "added"
        assert f.old_path is None
        assert f.new_path == "new.py"
        assert f.added_lines == {1, 2}

    def test_parses_file_deletion(self):
        diff = (
            "diff --git a/gone.py b/gone.py\n"
            "deleted file mode 100644\n"
            "--- a/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-old1\n"
            "-old2\n"
        )
        files = parse_unified_diff(diff)
        assert len(files) == 1
        f = files[0]
        assert f.path == "gone.py"
        assert f.status == "removed"
        assert f.old_path == "gone.py"
        assert f.new_path is None
        assert f.lines_removed == 2
        assert f.lines_added == 0
        assert f.added_lines == set()

    def test_parses_pure_rename(self):
        diff = (
            "diff --git a/old.py b/new.py\n"
            "similarity index 100%\n"
            "rename from old.py\n"
            "rename to new.py\n"
        )
        files = parse_unified_diff(diff)
        assert len(files) == 1
        f = files[0]
        assert f.path == "new.py"
        assert f.old_path == "old.py"
        assert f.new_path == "new.py"
        assert f.status == "renamed"
        assert f.hunks == []

    def test_parses_binary_file(self):
        diff = (
            "diff --git a/logo.png b/logo.png\n"
            "index 111..222 100644\n"
            "Binary files a/logo.png and b/logo.png differ\n"
        )
        files = parse_unified_diff(diff)
        assert len(files) == 1
        assert files[0].is_binary
        assert files[0].hunks == []
        assert files[0].path == "logo.png"

    def test_parses_git_binary_patch(self):
        diff = (
            "diff --git a/blob.bin b/blob.bin\n"
            "GIT binary patch\n"
            "literal 123\n"
        )
        files = parse_unified_diff(diff)
        assert len(files) == 1
        assert files[0].is_binary

    def test_handles_no_newline_marker(self):
        diff = (
            "diff --git a/a.txt b/a.txt\n"
            "--- a/a.txt\n"
            "+++ b/a.txt\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "\\ No newline at end of file\n"
            "+new\n"
            "\\ No newline at end of file\n"
        )
        files = parse_unified_diff(diff)
        assert len(files) == 1
        assert files[0].lines_added == 1
        assert files[0].lines_removed == 1

    def test_disambiguates_body_lines_starting_with_plus_or_minus(self):
        diff = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,1 +1,2 @@\n"
            " ctx\n"
            "++increment\n"
        )
        files = parse_unified_diff(diff)
        assert len(files) == 1
        assert files[0].hunks[0].lines[1].text == "+increment"
        assert files[0].hunks[0].lines[1].kind == "+"

    def test_added_lines_by_file_helper(self):
        diff = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,1 +1,2 @@\n"
            " x\n"
            "+y\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1,2 +1,1 @@\n"
            "-z\n"
            " keep\n"
        )
        mapping = added_lines_by_file(parse_unified_diff(diff))
        assert mapping == {"a.py": {2}}


class TestScoreFile:
    def test_large_diff_scores_higher(self):
        big = FileDiff(path="src/core.py", old_path="src/core.py", new_path="src/core.py",
                       hunks=[Hunk(1, 0, 1, 200, [DiffLine("+", "x", new_line=i) for i in range(1, 201)])])
        small = FileDiff(path="src/util.py", old_path="src/util.py", new_path="src/util.py",
                         hunks=[Hunk(1, 0, 1, 5, [DiffLine("+", "x", new_line=i) for i in range(1, 6)])])
        assert score_file(big) > score_file(small)

    def test_test_files_penalized(self):
        src = FileDiff(path="src/core.py", old_path="src/core.py", new_path="src/core.py",
                       hunks=[Hunk(1, 0, 1, 20, [DiffLine("+", "x", new_line=i) for i in range(1, 21)])])
        test = FileDiff(path="tests/test_core.py", old_path="tests/test_core.py", new_path="tests/test_core.py",
                        hunks=[Hunk(1, 0, 1, 20, [DiffLine("+", "x", new_line=i) for i in range(1, 21)])])
        assert score_file(src) > score_file(test)

    def test_lock_files_heavily_penalized(self):
        lock = FileDiff(path="package-lock.json", old_path="package-lock.json", new_path="package-lock.json",
                        hunks=[Hunk(1, 0, 1, 500, [DiffLine("+", "x", new_line=i) for i in range(1, 501)])])
        assert score_file(lock) < 10

    def test_churn_boosts_score(self):
        f = FileDiff(path="src/core.py", old_path="src/core.py", new_path="src/core.py",
                     hunks=[Hunk(1, 0, 1, 10, [DiffLine("+", "x", new_line=i) for i in range(1, 11)])])
        assert score_file(f, churn=20) > score_file(f, churn=0)


class TestBuildChunks:
    def _make_file(self, path: str, lines: int = 10) -> FileDiff:
        return FileDiff(
            path=path,
            old_path=path,
            new_path=path,
            hunks=[Hunk(1, 0, 1, lines, [DiffLine("+", "x", new_line=i) for i in range(1, lines + 1)])],
        )

    def test_empty_files_returns_empty(self):
        assert build_chunks([]) == []

    def test_single_file_one_chunk(self):
        files = [self._make_file("src/a.py")]
        chunks = build_chunks(files)
        assert len(chunks) == 1
        assert chunks[0] == files

    def test_skips_binary_files(self):
        binary = FileDiff(path="logo.png", old_path="logo.png", new_path="logo.png", is_binary=True)
        text = self._make_file("src/a.py")
        chunks = build_chunks([binary, text])
        assert len(chunks) == 1
        assert chunks[0] == [text]

    def test_respects_max_chunks(self):
        files = [self._make_file(f"src/f{i}.py", lines=50) for i in range(20)]
        chunks = build_chunks(files, max_chunks=4, token_budget=1000)
        assert len(chunks) <= 4
        # All non-binary files must still be partitioned
        assert sum(len(c) for c in chunks) == 20

    def test_respects_max_files_per_chunk(self):
        files = [self._make_file(f"src/f{i}.py", lines=2) for i in range(6)]
        chunks = build_chunks(files, token_budget=1_000_000, max_files_per_chunk=2)
        assert [len(c) for c in chunks] == [2, 2, 2]
        assert sum(len(c) for c in chunks) == 6

    def test_default_max_files_per_chunk_splits_a_wide_flat_set(self):
        """Six same-directory files fit one chunk on budget alone; the default
        cap of 5 is what opens the second chunk."""
        files = [self._make_file(f"src/f{i}.py", lines=2) for i in range(6)]
        chunks = build_chunks(files, token_budget=1_000_000)
        assert len(chunks) == 2
        assert max(len(c) for c in chunks) == 5

    def test_file_cap_yields_in_overflow_like_the_budget(self):
        """max_chunks reached with every chunk full: an overflow file joins the
        smallest chunk past the cap — coverage is the invariant, the cap is a
        preference, exactly like the token budget in the same branch."""
        files = [self._make_file(f"src/f{i}.py", lines=2) for i in range(6)]
        chunks = build_chunks(
            files, max_chunks=2, token_budget=1_000_000, max_files_per_chunk=2,
        )
        assert len(chunks) == 2
        assert sum(len(c) for c in chunks) == 6

    def test_groups_files_by_directory_proximity(self):
        # 4 files, 2 in pkg/auth and 2 in pkg/billing.
        # Budget fits exactly 2 files (each is 2 lines = 80 tokens, budget 170).
        f_auth1 = self._make_file("pkg/auth/login.py", lines=2)
        f_auth2 = self._make_file("pkg/auth/tokens.py", lines=2)
        f_bill1 = self._make_file("pkg/billing/invoice.py", lines=2)
        f_bill2 = self._make_file("pkg/billing/stripe.py", lines=2)

        files = [f_auth1, f_auth2, f_bill1, f_bill2]
        chunks = build_chunks(files, max_chunks=8, token_budget=170)
        assert len(chunks) == 2

        for chunk in chunks:
            paths = [f.path for f in chunk]
            if "pkg/auth/login.py" in paths:
                assert "pkg/auth/tokens.py" in paths
            if "pkg/billing/invoice.py" in paths:
                assert "pkg/billing/stripe.py" in paths


class TestTrimHunkContext:
    """trim_hunk_context is what PRXREF_CHUNK_CONTEXT_LINES controls."""

    def _hunk(self, spec, old_start=10, new_start=10):
        """Build a Hunk the way _parse_hunk_body would, numbering included."""
        hunk = Hunk(old_start=old_start, old_count=0, new_start=new_start, new_count=0)
        old_no, new_no = old_start, new_start
        for kind, text in spec:
            if kind == "+":
                hunk.lines.append(DiffLine("+", text, new_line=new_no))
                new_no += 1
            elif kind == "-":
                hunk.lines.append(DiffLine("-", text, old_line=old_no))
                old_no += 1
            else:
                hunk.lines.append(DiffLine(" ", text, old_line=old_no, new_line=new_no))
                old_no += 1
                new_no += 1
        hunk.old_count = old_no - old_start
        hunk.new_count = new_no - new_start
        return hunk

    def test_keeps_changes_and_trims_a_long_context_run(self):
        hunk = self._hunk(
            [(" ", f"lead{i}") for i in range(1, 6)]
            + [("-", "old"), ("+", "new")]
            + [(" ", f"tail{i}") for i in range(1, 6)]
        )
        trimmed = trim_hunk_context(hunk, 2)
        assert [(ln.kind, ln.text) for ln in trimmed.lines] == [
            (" ", "lead4"), (" ", "lead5"),
            ("-", "old"), ("+", "new"),
            (" ", "tail1"), (" ", "tail2"),
        ]
        assert trimmed.old_start == 13
        assert trimmed.new_start == 13
        assert trimmed.old_count == 5
        assert trimmed.new_count == 5

    def test_short_context_between_changes_is_kept_whole(self):
        """A run of 2 context lines with radius 2 sits within reach of both
        changes, so nothing is cut and the numbering does not move."""
        spec = [(" ", "a"), ("-", "b"), (" ", "c"), (" ", "d"), ("+", "e"), (" ", "f")]
        hunk = self._hunk(spec)
        trimmed = trim_hunk_context(hunk, 2)
        assert [ln.text for ln in trimmed.lines] == ["a", "b", "c", "d", "e", "f"]
        assert trimmed.old_start == 10
        assert trimmed.new_start == 10

    def test_zero_keeps_the_changed_lines_only(self):
        hunk = self._hunk([(" ", "x"), ("-", "y"), ("+", "z"), (" ", "w")])
        trimmed = trim_hunk_context(hunk, 0)
        assert [(ln.kind, ln.text) for ln in trimmed.lines] == [("-", "y"), ("+", "z")]
        assert trimmed.old_start == 11
        assert trimmed.new_start == 11
        assert trimmed.old_count == 1
        assert trimmed.new_count == 1

    def test_negative_radius_behaves_as_zero(self):
        """The config range check rejects negatives; a library caller passing
        one gets the -U0 rendering instead of a crash."""
        hunk = self._hunk([(" ", "x"), ("-", "y"), ("+", "z"), (" ", "w")])
        assert trim_hunk_context(hunk, -3).lines == trim_hunk_context(hunk, 0).lines

    def test_a_wide_radius_keeps_everything(self):
        spec = [(" ", "a"), ("-", "b"), (" ", "c")]
        hunk = self._hunk(spec)
        trimmed = trim_hunk_context(hunk, 100)
        assert [ln.text for ln in trimmed.lines] == ["a", "b", "c"]
        assert trimmed.old_start == 10
        assert trimmed.old_count == 3

    def test_a_hunk_without_change_lines_is_returned_unchanged(self):
        hunk = self._hunk([(" ", "a"), (" ", "b")])
        assert trim_hunk_context(hunk, 0) is hunk

    def test_the_input_hunk_is_never_mutated(self):
        """Chunking, quality, and line alignment all keep working from the
        full parse — the trim result is a new Hunk."""
        hunk = self._hunk([(" ", "a"), ("-", "b"), ("+", "c"), (" ", "d")])
        before = list(hunk.lines)
        trim_hunk_context(hunk, 0)
        assert hunk.lines == before
        assert hunk.old_start == 10
        assert hunk.new_start == 10


def test_finding_dataclass_shape():
    f = Finding(
        file="a.py",
        line=10,
        severity="error",
        confidence=0.95,
        title="Bug",
        body="Details",
    )
    assert f.file == "a.py"
    assert f.drop_reason is None


class TestCopyHeaders:
    """``copy from``/``copy to`` extended headers mark a copy, not a rename."""

    def test_parses_git_copy_headers(self):
        diff = (
            "diff --git a/pkg/a.json b/pkg/b.json\n"
            "similarity index 53%\n"
            "copy from a/pkg/a.json\n"
            "copy to b/pkg/b.json\n"
        )
        files = parse_unified_diff(diff)
        assert len(files) == 1
        f = files[0]
        assert f.status == "copied"
        assert f.old_path == "pkg/a.json"
        assert f.new_path == "pkg/b.json"
        assert f.path == "pkg/b.json"

    def test_parses_bitbucket_server_copy_headers(self):
        diff = (
            "diff --git src://pkg/a.json dst://pkg/b.json\n"
            "dissimilarity index 12%\n"
            "copy from src://pkg/a.json\n"
            "copy to dst://pkg/b.json\n"
        )
        files = parse_unified_diff(diff)
        assert len(files) == 1
        f = files[0]
        assert f.status == "copied"
        assert f.old_path == "pkg/a.json"
        assert f.new_path == "pkg/b.json"

    def test_similarity_index_line_is_consumed(self):
        diff = (
            "diff --git a/pkg/a.json b/pkg/b.json\n"
            "similarity index 53%\n"
            "copy from a/pkg/a.json\n"
            "copy to b/pkg/b.json\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
        )
        files = parse_unified_diff(diff)
        assert len(files) == 1
        assert len(files[0].hunks) == 1
        assert [ln.text for ln in files[0].hunks[0].lines] == ["old", "new"]

    def test_copied_status_renders_copy_headers(self):
        from prxref.reviewer import _render_file

        diff = (
            "diff --git a/pkg/a.json b/pkg/b.json\n"
            "copy from a/pkg/a.json\n"
            "copy to b/pkg/b.json\n"
        )
        rendered = _render_file(parse_unified_diff(diff)[0])
        assert "copy from pkg/a.json" in rendered
        assert "copy to pkg/b.json" in rendered
        assert "rename from" not in rendered
