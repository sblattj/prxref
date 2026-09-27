"""Issue #34: incremental re-review on push.

With ``PRXREF_INCREMENTAL=on`` every summary is stamped with the PR head it
reviewed, and a later run reviews only the PR files touched since that head,
while the systemic sweep still sees the whole PR. What is pinned:

- a multi-push fixture: push 1 reviews all three files and stamps the head;
  push 2 chunks only the file the compare diff touches, costs less, and its
  sweep still digests all three files; the summary note and the record are
  exact;
- the regression the issue asks for: push 2 prunes only the re-reviewed
  file's comments, so push 1's ERROR comment on another file stands;
- a failed chunk keeps the previous marker; a failed compare diff reviews
  every file with a WARNING; an empty delta runs the sweep alone and prunes
  nothing;
- each full-review fallback carries its reason;
- off is identity: no summary read, no marker, ``prune(ref)``, a null record;
- the CLI turns it off for ``--full-review`` and a ``PRXREF_FAIL_ON`` gate,
  a ``--diff-file`` replay runs full, and a bad value exits 2.
"""
from __future__ import annotations

import dataclasses
import json
import logging

import pytest

from prxref import config, orchestrator
from prxref.cli import main
from prxref.config import load_config
from prxref.costs import ModelPrice
from prxref.forges.base import FeedReadError, PRRef
from prxref.llm import ConfigError, InvokeResult
from prxref.orchestrator import (
    REVIEWED_HEAD_RE,
    orchestrate_review,
    reviewed_head_line,
)
from tests.test_cli import _install_fake_module
from tests.test_orchestrator import REF, FakeForge, _added_file_diff, make_pr, multi_chunk_diff

pytestmark = pytest.mark.usefixtures("contract_stubs")

HEAD_1 = "a" * 40
HEAD_2 = "c" * 40
FILE_A, FILE_B, FILE_C = "src/big1.py", "src/big2.py", "src/big3.py"
PRICES = {"test-model-1": ModelPrice(1.0, 1.0)}
ERROR_ON_A = {
    FILE_A: [
        {"file": FILE_A, "line": 3, "severity": "error", "confidence": 0.9,
         "title": "Null deref", "body": "the data value may be None here; a crash follows."},
    ],
}
_CLI_REF = PRRef(
    forge="github", host="github.com", owner="acme", repo="widget", number=7,
    url="https://github.com/acme/widget/pull/7",
)


class CountingLLM:
    """Answers the chunk stub from ``findings_by_path`` and the sweep double with no findings.

    Counts chunk and sweep calls apart, and raises for a chunk that holds a
    path in ``fail_paths``.
    """

    def __init__(self, findings_by_path=None, fail_paths=()):
        self.findings_by_path = findings_by_path or {}
        self.fail_paths = set(fail_paths)
        self.chunk_calls = 0
        self.sweep_calls = 0

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        if system == "sweep":
            self.sweep_calls += 1
            text = json.dumps({"findings": []})
        else:
            self.chunk_calls += 1
            paths = json.loads(user)
            if self.fail_paths & set(paths):
                raise RuntimeError("chunk boom")
            text = json.dumps({"findings": [f for p in paths for f in self.findings_by_path.get(p, [])]})
        return InvokeResult(
            text=text, input_tokens=100, output_tokens=50, model="test-model-1",
            backend="fake", elapsed_ms=1,
        )


@pytest.fixture
def digests(monkeypatch):
    """Replace the sweep with a double that calls the LLM once and records its digest."""
    seen: list[str] = []

    def review_systemic(llm, digest, **kwargs):
        seen.append(digest)
        result = llm.invoke(system="sweep", user="[]")
        return [], {
            "escalations": [], "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens, "model": result.model,
            "elapsed_ms": 1, "error": "",
        }

    monkeypatch.setattr(orchestrator.reviewer, "review_systemic", review_systemic)
    return seen


class IncForge(FakeForge):
    """A forge that stores the posted summary and serves it back from ``get_summary``."""

    def __init__(self, diff: str, head: str = HEAD_1):
        super().__init__(pr=dataclasses.replace(make_pr(), source_sha=head), diff=diff)
        self.stored: str | None = None
        self.summary_reads = 0
        self.summary_error: Exception | None = None
        self.compare_diff = ""
        self.compare_error: Exception | None = None
        self.compare_calls: list[tuple[str, str]] = []
        self.prune_calls: list[dict] = []

    def push(self, head: str, compare_diff: str) -> None:
        self.pr = dataclasses.replace(self.pr, source_sha=head)
        self.compare_diff = compare_diff

    def post_summary(self, ref, body):
        super().post_summary(ref, body)
        self.stored = body

    def get_summary(self, ref):
        self.summary_reads += 1
        if self.summary_error is not None:
            raise self.summary_error
        return self.stored

    def get_compare_diff(self, ref, *, base_sha, head_sha):
        self.compare_calls.append((base_sha, head_sha))
        if self.compare_error is not None:
            raise self.compare_error
        return self.compare_diff

    def prune_inline_comments(self, ref, **kwargs):
        self.prune_calls.append(kwargs)
        return 0


def _review(forge, llm, **kw):
    kw.setdefault("incremental", "on")
    kw.setdefault("max_workers", 1)
    kw.setdefault("price_table", PRICES)
    return orchestrate_review(forge, REF, llm, **kw)


def _markers(forge) -> list[str]:
    return [m for body in forge.summaries for m in REVIEWED_HEAD_RE.findall(body)]


def _two_pushes(digests, *, compare_paths=(FILE_B,), findings=None, fail_paths=()):
    forge = IncForge(multi_chunk_diff(3))
    llm1 = CountingLLM(findings)
    res1 = _review(forge, llm1)
    forge.push(HEAD_2, "".join(_added_file_diff(p, 5) for p in compare_paths))
    forge.summaries.clear()
    llm2 = CountingLLM(findings, fail_paths=fail_paths)
    res2 = _review(forge, llm2)
    return forge, (res1, llm1), (res2, llm2)


class TestMultiPush:
    def test_push_two_reviews_only_the_delta_and_costs_less(self, digests):
        forge, (res1, llm1), (res2, llm2) = _two_pushes(digests)
        assert (llm1.chunk_calls, llm1.sweep_calls) == (3, 1)
        assert (llm2.chunk_calls, llm2.sweep_calls) == (1, 1)
        assert res2["chunk_count"] == 2
        assert res1["cost_usd"] is not None and res2["cost_usd"] is not None
        assert res2["cost_usd"] < res1["cost_usd"]
        assert forge.compare_calls == [(HEAD_1, HEAD_2)]

    def test_the_sweep_still_sees_every_file(self, digests):
        _two_pushes(digests)
        push_two = digests[-1]
        for path in (FILE_A, FILE_B, FILE_C):
            assert f"## {path}" in push_two
        assert "PR changes 3 file(s)" in push_two

    def test_the_records_are_exact(self, digests):
        _, (res1, _), (res2, _) = _two_pushes(digests)
        assert res1["incremental"] == {
            "mode": "full", "reason": "first review", "since_sha": None,
            "files_total": 3, "files_reviewed": 3, "marker_sha": HEAD_1,
        }
        assert res2["incremental"] == {
            "mode": "incremental", "reason": None, "since_sha": HEAD_1,
            "files_total": 3, "files_reviewed": 1, "marker_sha": HEAD_2,
        }

    def test_the_summary_carries_the_note_and_the_new_marker(self, digests):
        forge, _, _ = _two_pushes(digests)
        (body,) = forge.summaries
        assert (
            "> Incremental review: 1 of 3 changed files re-reviewed since `aaaaaaa`; "
            "the systemic sweep saw the whole PR, and earlier inline comments on the "
            "other files still stand."
        ) in body
        assert body.endswith(reviewed_head_line(HEAD_2))
        assert _markers(forge) == [HEAD_2]

    def test_a_full_run_has_no_note(self, digests):
        forge = IncForge(multi_chunk_diff(3))
        _review(forge, CountingLLM())
        (body,) = forge.summaries
        assert "Incremental review" not in body
        assert body.endswith(reviewed_head_line(HEAD_1))


class TestPruneScope:
    def test_only_the_re_reviewed_files_are_pruned(self, digests):
        forge, _, _ = _two_pushes(digests, findings=ERROR_ON_A)
        first, second = forge.prune_calls
        assert first == {}
        assert FILE_B in second["paths"]
        assert FILE_A not in second["paths"]

    def test_push_one_posted_the_error_on_file_a(self, digests):
        forge = IncForge(multi_chunk_diff(3))
        _review(forge, CountingLLM(ERROR_ON_A))
        posted = [c for batch in forge.inline_batches for c in batch]
        assert [(c.path, c.line) for c in posted] == [(FILE_A, 3)]

    def test_a_renamed_file_prunes_its_old_path_too(self, digests):
        diff = (
            "diff --git a/src/old.py b/src/new.py\n"
            "similarity index 90%\n"
            "rename from src/old.py\n"
            "rename to src/new.py\n"
            "--- a/src/old.py\n"
            "+++ b/src/new.py\n"
            "@@ -1,1 +1,2 @@\n"
            " keep\n"
            "+data 1\n"
        )
        forge = IncForge(diff + _added_file_diff("src/other.py", 3))
        _review(forge, CountingLLM())
        forge.push(HEAD_2, diff)
        _review(forge, CountingLLM())
        assert forge.prune_calls[-1]["paths"] == frozenset({"src/old.py", "src/new.py"})


class TestMarkerCarry:
    def test_a_failed_chunk_keeps_the_previous_marker(self, digests):
        forge, _, (res2, llm2) = _two_pushes(
            digests, compare_paths=(FILE_A, FILE_B), fail_paths={FILE_B},
        )
        assert llm2.chunk_calls == 2
        assert res2["chunks_failed"] == 1
        assert res2["incremental"]["marker_sha"] == HEAD_1
        assert _markers(forge) == [HEAD_1]

    def test_a_totally_failed_push_keeps_the_previous_marker(self, digests):
        forge, _, (res2, _) = _two_pushes(digests, fail_paths={FILE_B})
        assert res2["verdict"] == "Error"
        assert res2["incremental"]["marker_sha"] == HEAD_1
        assert _markers(forge) == [HEAD_1]

    def test_a_failed_first_review_writes_no_marker(self, digests):
        forge = IncForge(multi_chunk_diff(3))
        res = _review(forge, CountingLLM(fail_paths={FILE_A}))
        assert res["chunks_failed"] == 1
        assert res["incremental"]["marker_sha"] is None
        assert _markers(forge) == []


class TestCompareFailure:
    def test_a_failed_compare_reviews_every_file_with_a_warning(self, digests, caplog):
        forge = IncForge(multi_chunk_diff(3))
        _review(forge, CountingLLM())
        forge.push(HEAD_2, "")
        forge.compare_error = RuntimeError("unknown commit")
        llm = CountingLLM()
        with caplog.at_level(logging.WARNING, logger="prxref"):
            res = _review(forge, llm)
        assert llm.chunk_calls == 3
        assert res["incremental"] == {
            "mode": "full", "reason": "compare diff failed", "since_sha": None,
            "files_total": 3, "files_reviewed": 3, "marker_sha": HEAD_2,
        }
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("compare diff" in w and "RuntimeError" in w for w in warnings)
        assert forge.prune_calls[-1] == {}


class TestEmptyDelta:
    def _assert_sweep_alone(self, forge, llm, res):
        assert (llm.chunk_calls, llm.sweep_calls) == (0, 1)
        assert res["chunk_count"] == 1
        assert res["verdict"] == "Approved"
        assert len(forge.summaries) == 1
        assert forge.prune_calls == []
        assert res["incremental"]["files_reviewed"] == 0

    def test_marker_equal_to_head_runs_the_sweep_alone(self, digests):
        forge = IncForge(multi_chunk_diff(3))
        _review(forge, CountingLLM())
        forge.summaries.clear()
        forge.prune_calls.clear()
        llm = CountingLLM()
        res = _review(forge, llm)
        self._assert_sweep_alone(forge, llm, res)
        assert forge.compare_calls == []
        assert res["incremental"]["mode"] == "incremental"
        assert _markers(forge) == [HEAD_1]

    def test_an_empty_compare_diff_runs_the_sweep_alone(self, digests):
        forge = IncForge(multi_chunk_diff(3))
        _review(forge, CountingLLM())
        forge.push(HEAD_2, "")
        forge.summaries.clear()
        forge.prune_calls.clear()
        llm = CountingLLM()
        res = _review(forge, llm)
        self._assert_sweep_alone(forge, llm, res)
        assert _markers(forge) == [HEAD_2]

    def test_a_compare_outside_the_pr_is_an_empty_delta(self, digests):
        forge = IncForge(multi_chunk_diff(3))
        _review(forge, CountingLLM())
        forge.push(HEAD_2, _added_file_diff("merged/from_base.py", 4))
        forge.summaries.clear()
        forge.prune_calls.clear()
        llm = CountingLLM()
        res = _review(forge, llm)
        self._assert_sweep_alone(forge, llm, res)


class _NoSummaryForge(FakeForge):
    def prune_inline_comments(self, ref, **kwargs):
        return 0


class TestFullFallbacks:
    def _full(self, forge, **kw):
        llm = CountingLLM()
        res = _review(forge, llm, **kw)
        assert llm.chunk_calls == 3
        assert res["incremental"]["mode"] == "full"
        assert res["incremental"]["since_sha"] is None
        return res

    def test_inline_only_post_mode_runs_full_without_reading(self, digests, caplog):
        forge = IncForge(multi_chunk_diff(3))
        forge.stored = f"old\n\n{reviewed_head_line(HEAD_1)}"
        with caplog.at_level(logging.INFO, logger="prxref"):
            res = self._full(forge, post_mode="inline")
        assert res["incremental"]["reason"] == (
            "PRXREF_POST_MODE=inline never writes the reviewed-head marker"
        )
        assert res["incremental"]["marker_sha"] is None
        assert forge.summary_reads == 0
        assert any(
            r.levelno == logging.INFO and "PRXREF_POST_MODE=inline" in r.getMessage()
            for r in caplog.records
        )

    def test_a_forge_without_get_summary_runs_full(self, digests):
        forge = _NoSummaryForge(pr=make_pr(), diff=multi_chunk_diff(3))
        res = self._full(forge)
        assert res["incremental"]["reason"] == "forge cannot read its summary"
        assert REVIEWED_HEAD_RE.findall(forge.summaries[-1]) == [HEAD_1]

    def test_a_raising_get_summary_runs_full_with_a_warning(self, digests, caplog):
        forge = IncForge(multi_chunk_diff(3))
        forge.summary_error = FeedReadError("feed cut short")
        with caplog.at_level(logging.WARNING, logger="prxref"):
            res = self._full(forge)
        assert res["incremental"]["reason"] == "previous summary could not be read"
        assert any(
            r.levelno == logging.WARNING and "FeedReadError" in r.getMessage()
            for r in caplog.records
        )

    def test_a_summary_without_a_marker_runs_full(self, digests):
        forge = IncForge(multi_chunk_diff(3))
        forge.stored = "a summary from before the marker existed"
        res = self._full(forge)
        assert res["incremental"]["reason"] == "previous summary has no reviewed-head marker"
        assert forge.compare_calls == []

    def test_no_summary_is_a_first_review(self, digests):
        res = self._full(IncForge(multi_chunk_diff(3)))
        assert res["incremental"]["reason"] == "first review"


class TestOffIsIdentity:
    def test_off_reads_nothing_stamps_nothing_and_prunes_whole(self, digests):
        forge = IncForge(multi_chunk_diff(3))
        forge.stored = f"old\n\n{reviewed_head_line('b' * 40)}"
        llm = CountingLLM()
        res = orchestrate_review(forge, REF, llm, max_workers=1)
        assert res["incremental"] is None
        assert forge.summary_reads == 0
        assert forge.compare_calls == []
        assert forge.prune_calls == [{}]
        assert llm.chunk_calls == 3
        assert all("prxref-reviewed-head" not in body for body in forge.summaries)

    def test_an_unknown_value_raises_before_any_forge_call(self):
        forge = IncForge(multi_chunk_diff(3))
        with pytest.raises(ValueError, match="incremental"):
            orchestrate_review(forge, REF, CountingLLM(), incremental="yes")

    def test_the_writer_and_the_reader_share_one_marker(self):
        line = reviewed_head_line(HEAD_1)
        assert line == f"<!-- prxref-reviewed-head: {HEAD_1} -->"
        assert REVIEWED_HEAD_RE.fullmatch(line).group(1) == HEAD_1


class TestErrorNotice:
    def test_an_error_notice_after_resolution_carries_the_previous_marker(self, digests):
        forge = IncForge(multi_chunk_diff(3))
        _review(forge, CountingLLM())
        forge.push(HEAD_2, _added_file_diff(FILE_B, 5))
        forge.summaries.clear()
        res = _review(forge, CountingLLM(), max_chunks=0)
        assert res["verdict"] == "Error"
        assert res["incremental"]["marker_sha"] == HEAD_1
        assert _markers(forge) == [HEAD_1]

    def test_an_error_before_resolution_is_recorded(self, digests):
        forge = IncForge(multi_chunk_diff(3))
        forge.fail.add("get_diff")
        res = _review(forge, CountingLLM())
        assert res["incremental"] == {
            "mode": "full", "reason": "review ended before scope resolution",
            "since_sha": None, "files_total": 0, "files_reviewed": 0, "marker_sha": None,
        }


class TestCli:
    def _capture(self, monkeypatch):
        calls: list[dict] = []

        def fake_orchestrate_review(**kwargs):
            calls.append(kwargs)
            return {"verdict": "Approved", "findings_active": [], "findings_dropped": []}

        _install_fake_module(monkeypatch, "prxref.llm_backends", create_llm_client=lambda cfg: object())
        _install_fake_module(monkeypatch, "prxref.orchestrator", orchestrate_review=fake_orchestrate_review)
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: _CLI_REF)
        return calls

    def test_on_reaches_the_orchestrator(self, monkeypatch):
        calls = self._capture(monkeypatch)
        monkeypatch.setenv("PRXREF_INCREMENTAL", "on")
        assert main(["review", "--pr-url", _CLI_REF.url, "--no-post"]) == 0
        assert calls[0]["incremental"] == "on"

    def test_the_default_is_off(self, monkeypatch):
        calls = self._capture(monkeypatch)
        monkeypatch.delenv("PRXREF_INCREMENTAL", raising=False)
        assert main(["review", "--pr-url", _CLI_REF.url, "--no-post"]) == 0
        assert calls[0]["incremental"] == "off"

    def test_full_review_turns_it_off(self, monkeypatch, caplog):
        calls = self._capture(monkeypatch)
        monkeypatch.setenv("PRXREF_INCREMENTAL", "on")
        with caplog.at_level(logging.INFO, logger="prxref"):
            assert main(["review", "--pr-url", _CLI_REF.url, "--no-post", "--full-review"]) == 0
        assert calls[0]["incremental"] == "off"
        assert any("--full-review" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("gate", ["error", "any"])
    def test_a_fail_on_gate_turns_it_off(self, monkeypatch, caplog, gate):
        calls = self._capture(monkeypatch)
        monkeypatch.setenv("PRXREF_INCREMENTAL", "on")
        monkeypatch.setenv("PRXREF_FAIL_ON", gate)
        with caplog.at_level(logging.INFO, logger="prxref"):
            assert main(["review", "--pr-url", _CLI_REF.url, "--no-post"]) == 0
        assert calls[0]["incremental"] == "off"
        assert any(f"PRXREF_FAIL_ON={gate}" in r.getMessage() for r in caplog.records)

    def test_a_diff_file_replay_runs_full(self, monkeypatch, tmp_path, capsys, digests):
        path = tmp_path / "change.diff"
        path.write_text(multi_chunk_diff(3), encoding="utf-8")
        _install_fake_module(
            monkeypatch, "prxref.llm_backends", create_llm_client=lambda cfg: CountingLLM(),
        )
        monkeypatch.setenv("PRXREF_INCREMENTAL", "on")
        assert main(["review", "--diff-file", str(path), "--format", "json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["incremental"]["mode"] == "full"
        assert payload["incremental"]["reason"] == "forge cannot read its summary"
        assert payload["incremental"]["files_reviewed"] == 3

    def test_a_bad_value_exits_2_naming_the_variable(self, monkeypatch, capsys):
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: _CLI_REF)
        monkeypatch.setenv("PRXREF_INCREMENTAL", "sometimes")
        assert main(["review", "--pr-url", _CLI_REF.url, "--no-post"]) == 2
        _, err = capsys.readouterr()
        assert "configuration error" in err
        assert "PRXREF_INCREMENTAL" in err


class TestConfig:
    def test_the_default_is_off(self):
        assert config._DEFAULTS["incremental"] == "off"
        assert load_config()["incremental"] == "off"

    def test_the_vocabulary_is_declared_in_the_choice_table(self):
        assert config._CHOICE_KEYS["incremental"] == frozenset({"off", "on"})
        assert orchestrator.INCREMENTAL_MODES == config._CHOICE_KEYS["incremental"]

    @pytest.mark.parametrize("raw", ["bogus", "true", "ON", "1"])
    def test_a_value_outside_the_vocabulary_is_a_config_error(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_INCREMENTAL", raw)
        with pytest.raises(ConfigError, match="PRXREF_INCREMENTAL"):
            load_config()

    def test_eval_runs_never_record_it(self):
        from prxref import evals

        assert "incremental" not in evals.RUN_CONFIG_KEYS
