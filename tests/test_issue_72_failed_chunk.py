"""Issue #72: a failed chunk degraded the verdict not at all, and the fixed
default deadline was too short for large prompts.

Two halves:

* **Verdict / record / output.** A run with a failed chunk used to come back
  ``Approved`` — the failed unit simply contributed no findings, which proved
  nothing — with ``degraded`` left ``null`` (it only described failed POSTS)
  and no machine-readable list of the files that went unreviewed. Now the
  verdict is ``Incomplete`` (below ``Request-Changes`` with error findings,
  never shadowing the total-failure ``Error``), the run record and the JSON
  gain ``failed_chunks`` and a ``degraded`` record with one ``chunks`` row
  per failed unit, and the text ``coverage:`` line names the unreviewed
  files (``systemic sweep`` when the failed unit is the sweep). A clean run
  is byte-identical to before: the new keys appear only on a partial run,
  like ``degraded`` always has.
* **Deadline scaling.** With ``PRXREF_LLM_TIMEOUT`` left at its default the
  openai-compat client sizes each request's deadline from the prompt —
  prefill and decode both grow with the input — instead of holding a fixed
  45s that a 22k-token prompt cannot fit inside. An explicit timeout (flag,
  variable or config file) is used as-is and disables scaling entirely.
"""
from __future__ import annotations

import io
import itertools
import json

import pytest

from prxref import cli, orchestrator
from prxref.cli import _build_json_result, _print_summary
from prxref.llm_backends import (
    DEFAULT_TIMEOUT,
    DEFAULT_TIMEOUT_PER_1K,
    SCALED_DEADLINE_BASE_S,
    SCALED_DEADLINE_CAP_S,
    OpenAICompatClient,
    create_llm_client,
    scaled_deadline,
)
from prxref.orchestrator import orchestrate_review
from tests.test_llm_backends import _resp, _ScriptedSession
from tests.test_orchestrator import (
    REF,
    FakeForge,
    FakeLLM,
    _added_file_diff,
)

pytestmark = pytest.mark.usefixtures("contract_stubs")

#: The failure reason of the one flaky chunk. Not a timeout word, so the
#: deterministic timeout retry never fires and the chunk stays failed.
FLAKY_ERROR = "LLMError: all models failed: m1: HTTP 500"

TWO_FILE_DIFF = (
    _added_file_diff("src/one.py", 400) + _added_file_diff("other/two.py", 400)
)


def _flaky_review_chunk(counter):
    """Fail the first chunk review, succeed every later one (chunk and sweep)."""

    def _rc(llm, files, **kwargs):
        # NOT the timeout vocabulary: that triggers the deterministic
        # context_lines=0 retry, whose second call here would succeed.
        if next(counter) == 1:
            return [], {
                "escalations": [], "input_tokens": 0, "output_tokens": 0,
                "model": "", "elapsed_ms": 0,
                "error": FLAKY_ERROR,
            }
        return [], {
            "escalations": [], "input_tokens": 5, "output_tokens": 5,
            "model": "m", "elapsed_ms": 1, "error": "",
        }

    return _rc


class TestPartialRunVerdictAndRecord:
    def test_a_failed_chunk_downgrades_the_verdict_to_incomplete(self, monkeypatch):
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk", _flaky_review_chunk(itertools.count(1)),
        )
        res = orchestrate_review(
            FakeForge(diff=TWO_FILE_DIFF), REF, FakeLLM("{}"), post=False, max_chunks=2,
        )
        assert res["verdict"] == "Incomplete"
        assert res["chunks_failed"] == 1

    def test_error_findings_outrank_the_coverage_gap(self, monkeypatch):
        """A real defect beats incompleteness: the verdict stays actionable."""
        counter = itertools.count(1)
        error_finding = {
            "file": "other/two.py", "line": 3, "severity": "error",
            "confidence": 0.9, "title": "Null deref", "body": "data",
        }

        def _rc(llm, files, **kwargs):
            if next(counter) == 1:
                return [], {
                    "escalations": [], "input_tokens": 0, "output_tokens": 0,
                    "model": "", "elapsed_ms": 0, "error": FLAKY_ERROR,
                }
            return [error_finding], {
                "escalations": [], "input_tokens": 5, "output_tokens": 5,
                "model": "m", "elapsed_ms": 1, "error": "",
            }

        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _rc)
        res = orchestrate_review(
            FakeForge(diff=TWO_FILE_DIFF), REF, FakeLLM("{}"), post=False, max_chunks=2,
        )
        assert res["verdict"] == "Request-Changes"
        assert res["chunks_failed"] == 1

    def test_the_degraded_record_carries_one_row_per_failed_unit(self, monkeypatch):
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk", _flaky_review_chunk(itertools.count(1)),
        )
        res = orchestrate_review(
            FakeForge(diff=TWO_FILE_DIFF), REF, FakeLLM("{}"), post=False, max_chunks=2,
        )
        assert res["degraded"] == {
            "cause": "partial",
            "failed": ["chunk"],
            "fallback": [],
            "annotations": 0,
            "chunks": [{
                # The first of two chunks: worker index 1 of 2.
                "index": 1,
                "files": ["src/one.py"],
                "error": FLAKY_ERROR,
            }],
        }

    def test_the_run_record_carries_failed_chunks_only_on_a_partial_run(
        self, monkeypatch,
    ):
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk", _flaky_review_chunk(itertools.count(1)),
        )
        res = orchestrate_review(
            FakeForge(diff=TWO_FILE_DIFF), REF, FakeLLM("{}"), post=False, max_chunks=2,
        )
        assert res["failed_chunks"] == [(FLAKY_ERROR, ["src/one.py"])]

    def test_a_clean_run_keeps_the_pre_72_key_set_and_null_degraded(self):
        """The new keys ride partial runs only, like ``degraded`` always has."""
        res = orchestrate_review(
            FakeForge(diff=_added_file_diff("src/app.py", 20)), REF,
            FakeLLM("{}"), post=False,
        )
        assert res["verdict"] == "Approved"
        assert res["degraded"] is None
        assert "failed_chunks" not in res

    def test_a_clean_json_payload_has_no_failed_chunks_key(self):
        res = orchestrate_review(
            FakeForge(diff=_added_file_diff("src/app.py", 20)), REF,
            FakeLLM("{}"), post=False,
        )
        payload = _build_json_result(res)
        assert "failed_chunks" not in payload
        assert payload["degraded"] is None

    def test_a_partial_json_payload_gains_failed_chunks_after_chunks_failed(
        self, monkeypatch,
    ):
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk", _flaky_review_chunk(itertools.count(1)),
        )
        res = orchestrate_review(
            FakeForge(diff=TWO_FILE_DIFF), REF, FakeLLM("{}"), post=False, max_chunks=2,
        )
        payload = _build_json_result(res)
        keys = list(payload)
        assert keys[keys.index("failed_chunks") - 1] == "chunks_failed"
        assert payload["failed_chunks"] == [(FLAKY_ERROR, ["src/one.py"])]


class TestTextCoverageLine:
    def _lines(self, result) -> list[str]:
        out = io.StringIO()
        _print_summary(result, 0.0, verbose=False, out=out)
        return out.getvalue().splitlines()

    def test_the_line_names_the_unreviewed_files(self, monkeypatch):
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk", _flaky_review_chunk(itertools.count(1)),
        )
        res = orchestrate_review(
            FakeForge(diff=TWO_FILE_DIFF), REF, FakeLLM("{}"), post=False, max_chunks=2,
        )
        assert self._lines(res) == [
            "verdict: Incomplete",
            "coverage: 2/3 chunks reviewed; NOT reviewed: src/one.py",
        ]

    def test_a_failed_sweep_says_systemic_sweep_rather_than_nothing(
        self, monkeypatch,
    ):
        def _sweep_double(llm, files, *, pr_title="", pr_description="", **kwargs):
            return [], {
                "escalations": [], "input_tokens": 0, "output_tokens": 0,
                "model": "", "elapsed_ms": 0,
                "error": "systemic sweep: LLMError: all models failed",
            }

        monkeypatch.setattr(orchestrator.reviewer, "review_systemic", _sweep_double)
        res = orchestrate_review(
            FakeForge(diff=_added_file_diff("src/app.py", 20)), REF,
            FakeLLM('{"findings": []}'), post=False,
        )
        assert self._lines(res) == [
            "verdict: Incomplete",
            "coverage: 1/2 chunks reviewed; NOT reviewed: systemic sweep",
        ]

    def test_a_record_without_the_file_list_keeps_the_old_line(self):
        """An error-shaped or partial result must not grow the suffix."""
        lines = self._lines({"chunks_failed": 1, "chunks_reviewed": 1})
        assert lines == ["verdict: done", "coverage: 1/2 chunks reviewed"]


class TestTheTimeoutReasonIsRewritten:
    TIMEOUT = "LLMError: all models failed: m1: timeout (ReadTimeout)"

    def test_a_persistent_timeout_names_the_chunk_and_the_lever(self, monkeypatch):
        counter = itertools.count(1)

        def _rc(llm, files, **kwargs):
            # Calls 1 and 2 are chunk 1's two attempts (the original and the
            # timeout retry); both time out, so the chunk stays failed and
            # the FINAL error is the rewritten one. Call 3 is chunk 2, which
            # succeeds, keeping this a partial run rather than a total one.
            failed = next(counter) <= 2
            return [], {
                "escalations": [], "input_tokens": 0, "output_tokens": 0,
                "model": "", "elapsed_ms": 47000,
                "error": self.TIMEOUT if failed else "",
            }

        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _rc)
        res = orchestrate_review(
            FakeForge(diff=TWO_FILE_DIFF), REF, FakeLLM("{}"), post=False, max_chunks=2,
        )
        assert res["failed_chunks"][0][0] == (
            "[chunk 1/2] timed out after 47s; increase --timeout"
        )
        # The row in degraded carries the same rewritten reason.
        assert res["degraded"]["chunks"][0]["error"].startswith("[chunk 1/2]")

    def test_a_non_timeout_error_keeps_its_reason(self, monkeypatch):
        counter = itertools.count(1)

        def _rc(llm, files, **kwargs):
            error = "LLMError: malformed response" if next(counter) == 1 else ""
            return [], {
                "escalations": [], "input_tokens": 0, "output_tokens": 0,
                "model": "", "elapsed_ms": 120, "error": error,
            }

        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _rc)
        res = orchestrate_review(
            FakeForge(diff=TWO_FILE_DIFF), REF, FakeLLM("{}"), post=False, max_chunks=2,
        )
        assert res["failed_chunks"][0][0] == "LLMError: malformed response"


class TestTheGate:
    @pytest.mark.parametrize("policy", ["error", "any"])
    def test_incomplete_exits_1_like_error_under_a_gate(self, policy):
        code, note = cli._fail_on_exit(
            {"verdict": "Incomplete", "findings_active": []}, policy,
        )
        assert code == 1
        assert note == (
            f"PRXREF_FAIL_ON={policy}: review did not complete "
            "(verdict Incomplete); exiting 1"
        )

    def test_never_stays_0(self):
        assert cli._fail_on_exit({"verdict": "Incomplete"}, "never") == (0, None)

    def test_error_keeps_its_own_wording(self):
        code, note = cli._fail_on_exit({"verdict": "Error"}, "error")
        assert (code, note) == (
            1,
            "PRXREF_FAIL_ON=error: review did not complete (verdict Error); exiting 1",
        )


class TestDegradedCausePrecedence:
    def test_a_blocked_write_outranks_the_partial_cause(self, monkeypatch):
        import requests

        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk", _flaky_review_chunk(itertools.count(1)),
        )
        forge = FakeForge(diff=TWO_FILE_DIFF)
        response = requests.Response()
        response.status_code = 403
        forge.fail.add("post_summary")

        def _refused(ref, body):
            raise requests.HTTPError(response=response)

        forge.post_summary = _refused  # type: ignore[method-assign]
        res = orchestrate_review(forge, REF, FakeLLM("{}"), post=True, max_chunks=2)
        assert res["degraded"]["cause"] == "permission"
        assert res["degraded"]["failed"] == ["chunk", "summary"]
        assert "chunks" in res["degraded"]


class TestScaledDeadlineMath:
    """The formula the orchestrator ruled on, pinned at three points."""

    def test_zero_tokens_gets_the_base(self):
        assert scaled_deadline(0) == SCALED_DEADLINE_BASE_S == 20.0

    def test_the_calibration_point(self):
        # 22k tokens -> 20 + 1.6 * 22 = 55.2s, matching the reported run.
        assert scaled_deadline(22_000) == pytest.approx(55.2)

    def test_the_cap_bounds_a_pathological_prompt(self):
        assert scaled_deadline(1_000_000) == SCALED_DEADLINE_CAP_S == 900.0
        assert scaled_deadline(10_000_000) == 900.0

    def test_the_coefficient_is_tunable(self):
        assert scaled_deadline(22_000, per_1k=2.5) == pytest.approx(75.0)

    def test_the_default_coefficient_is_the_documented_one(self):
        assert DEFAULT_TIMEOUT_PER_1K == 1.6


class _DeadlineCapturingSession(_ScriptedSession):
    """Records the read timeout each POST was given (= the request deadline)."""

    def post(self, url, json=None, headers=None, timeout=None, stream=None):
        self.calls.append({
            "url": url, "json": json, "headers": headers,
            "timeout": timeout, "stream": stream,
        })
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _resp()


def _client(session, **kwargs) -> OpenAICompatClient:
    return OpenAICompatClient(
        base_url="https://llm.test/v1/",
        api_key="local",
        models=["m1"],
        session=session,
        default_timeout=DEFAULT_TIMEOUT,
        **kwargs,
    )


class TestDeadlineScalingInTheClient:
    BIG = "x" * (22_000 * 4)  # ~22k tokens at the 4-chars-per-token estimate

    def test_the_default_timeout_scales_with_the_prompt(self):
        session = _DeadlineCapturingSession(_resp())
        client = _client(session, timeout_per_1k=DEFAULT_TIMEOUT_PER_1K)
        client.invoke("system", self.BIG)
        # max(45, 20 + 1.6 * ~22) == ~55.2 (the system half adds a token or two)
        assert session.calls[0]["timeout"][1] == pytest.approx(55.2, abs=0.01)

    def test_a_small_prompt_never_loses_the_default_deadline(self):
        session = _DeadlineCapturingSession(_resp())
        client = _client(session, timeout_per_1k=DEFAULT_TIMEOUT_PER_1K)
        client.invoke("sys", "usr")
        assert session.calls[0]["timeout"][1] == DEFAULT_TIMEOUT

    def test_an_explicit_per_call_timeout_scales_nothing(self):
        session = _DeadlineCapturingSession(_resp())
        client = _client(session, timeout_per_1k=DEFAULT_TIMEOUT_PER_1K)
        client.invoke("system", self.BIG, timeout_s=30.0)
        assert session.calls[0]["timeout"][1] == 30.0

    def test_scaling_off_uses_the_default_for_every_prompt(self):
        session = _DeadlineCapturingSession(_resp())
        client = _client(session, timeout_per_1k=None)
        client.invoke("system", self.BIG)
        assert session.calls[0]["timeout"][1] == DEFAULT_TIMEOUT

    def test_an_explicit_environment_timeout_disables_scaling_via_the_factory(
        self, monkeypatch,
    ):
        monkeypatch.setenv("PRXREF_LLM_TIMEOUT", "45")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.test/v1")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "m1")
        session = _DeadlineCapturingSession(_resp())
        client = create_llm_client(session=session)
        assert client.timeout_per_1k is None
        client.invoke("system", self.BIG)
        assert session.calls[0]["timeout"][1] == 45.0

    def test_the_default_timeout_scales_via_the_factory(self):
        session = _DeadlineCapturingSession(_resp())
        client = create_llm_client({
            "LLM_BASE_URL": "https://llm.test/v1",
            "LLM_MODELS": "m1",
            "LLM_TIMEOUT_IS_DEFAULT": True,
        }, session=session)
        assert client.timeout_per_1k == DEFAULT_TIMEOUT_PER_1K
        client.invoke("system", self.BIG)
        assert session.calls[0]["timeout"][1] == pytest.approx(55.2, abs=0.01)

    def test_a_false_layer_hint_disables_scaling_via_the_factory(self):
        session = _DeadlineCapturingSession(_resp())
        client = create_llm_client({
            "LLM_BASE_URL": "https://llm.test/v1",
            "LLM_MODELS": "m1",
            "LLM_TIMEOUT_IS_DEFAULT": False,
        }, session=session)
        assert client.timeout_per_1k is None

    def test_a_tuned_coefficient_reaches_the_wire(self):
        session = _DeadlineCapturingSession(_resp())
        client = create_llm_client({
            "LLM_BASE_URL": "https://llm.test/v1",
            "LLM_MODELS": "m1",
            "LLM_TIMEOUT_PER_1K": "2.5",
            "LLM_TIMEOUT_IS_DEFAULT": True,
        }, session=session)
        assert client.timeout_per_1k == 2.5
        client.invoke("system", self.BIG)
        assert session.calls[0]["timeout"][1] == pytest.approx(75.0, abs=0.01)

    def test_a_malformed_coefficient_is_a_config_error(self):
        with pytest.raises(Exception, match="PRXREF_LLM_TIMEOUT_PER_1K"):
            create_llm_client({
                "LLM_BASE_URL": "https://llm.test/v1",
                "LLM_MODELS": "m1",
                "LLM_TIMEOUT_PER_1K": "zero",
            })


class TestTheCliTellsTheFactoryWhichLayerSuppliedTheTimeout:
    """The resolved cfg always carries a value, so the CLI passes the layer."""

    @pytest.fixture
    def captured(self, monkeypatch, tmp_path):
        seen: list[dict] = []

        class _Stub:
            timeout_per_1k = None

        def _factory(cfg, session=None):
            seen.append(cfg)
            return _Stub()

        monkeypatch.setattr("prxref.llm_backends.create_llm_client", _factory)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PRXREF_LLM_MODELS", "fake")
        return seen

    def _diff_file(self, tmp_path):
        diff = tmp_path / "pr.diff"
        diff.write_text(_added_file_diff("src/app.py", 3), encoding="utf-8")
        return diff

    def test_no_timeout_given_means_default_and_scaling(self, captured, tmp_path):
        assert cli.main([
            "review", "--diff-file", str(self._diff_file(tmp_path)), "--no-post",
        ]) == 0
        assert captured[0]["LLM_TIMEOUT_IS_DEFAULT"] is True

    def test_the_flag_counts_as_explicit_even_at_the_default_value(
        self, captured, tmp_path,
    ):
        assert cli.main([
            "review", "--diff-file", str(self._diff_file(tmp_path)),
            "--no-post", "--timeout", "45",
        ]) == 0
        assert captured[0]["LLM_TIMEOUT_IS_DEFAULT"] is False

    def test_the_environment_counts_as_explicit(self, captured, tmp_path, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_TIMEOUT", "45")
        assert cli.main([
            "review", "--diff-file", str(self._diff_file(tmp_path)), "--no-post",
        ]) == 0
        assert captured[0]["LLM_TIMEOUT_IS_DEFAULT"] is False


class TestPartialRunByteIdentityOfCleanOutput:
    """A clean run's stdout and JSON are unchanged by the feature (golden)."""

    def test_text(self):
        from tests.test_issue_38_cli_config import BASE_TEXT

        out = io.StringIO()
        _print_summary({"verdict": "Approved"}, 0.0, verbose=False, out=out)
        assert out.getvalue() == BASE_TEXT

    def test_json(self):
        from tests.test_issue_38_cli_config import BASE_JSON

        expected = BASE_JSON.replace(
            '"degraded": null}', '"degraded": null, "config_file": null}',
        )
        assert json.dumps(_build_json_result({
            "verdict": "Approved", "findings_active": [], "findings_dropped": [],
            "chunk_count": 1, "chunks_reviewed": 1, "chunks_failed": 0,
            "elapsed_ms": 5, "input_tokens": 10, "output_tokens": 2,
            "posted": False,
        })) + "\n" == expected
