"""Issue #48: a review still reaches the author when the token cannot post.

Covers the pure renderers in :mod:`prxref.ci_fallback`, the orchestrator's
``degraded`` record on every posting exit, and the CLI's emission through
each CI (GitHub Actions, Azure Pipelines, GitLab CI, Bitbucket Pipelines, no
CI) in both output formats, with ``PRXREF_FALLBACK=off`` and with posts that
succeed. The identity goldens were measured on the 0.22.0 CLI with the same
fake review.
"""
from __future__ import annotations

import hashlib
import json
import logging
from types import SimpleNamespace

import pytest
import requests

from prxref import ci_fallback, cli
from prxref.ci_fallback import (
    DEGRADED_SUMMARY_KEY,
    GITLAB_REPORT_FILE,
    azure_log_issues,
    detect_ci,
    github_annotations,
    gitlab_codequality,
    step_summary_markdown,
)
from prxref.orchestrator import orchestrate_review, post_failure_cause
from tests.test_orchestrator import HAPPY_FINDINGS, REF, FakeForge, FakeLLM, _added_file_diff

pytestmark = pytest.mark.usefixtures("contract_stubs")

APP_DIFF = _added_file_diff("src/app.py", 20)
ERROR_LINE = (
    "::error file=src/app.py,line=3,title=Null deref::"
    "x may be None when config is missing; data loss follows."
)
NOTICE_LINE = "::notice file=src/app.py,line=7,title=Typo::recieve -> receive in data text."
AZURE_ERROR = (
    "##vso[task.logissue type=error;sourcepath=src/app.py;linenumber=3;]"
    "Null deref: x may be None when config is missing%3B data loss follows."
)
AZURE_OTHER = (
    "##vso[task.logissue type=warning;sourcepath=src/app.py;linenumber=7;]"
    "Typo: recieve -> receive in data text."
)
COULD_NOT_POST = "prxref could not post (permission); the review is in this log"

# sha256 of stdout for a successful posting run of the fake review, measured
# on the 0.22.0 CLI (the JSON one after dropping the new "degraded" key), and
# of the summaries it posted.
BASE_TEXT_SHA = "8f4125946c3177378a3f9dbc929f1f909dbd3fa83f3698e422bd3c69b655c8d0"
BASE_JSON_SHA = "d3a4b72f0b3251087fdb4c3621b76a2f3081474c78195c83ab0ec413bb46453c"
BASE_VERBOSE_SHA = "f145977233fae8f533f3c693983527d25e5758af9465a74842c867e5b64487c6"
BASE_SUMMARIES_SHA = "b8e2f3de1189c9b0f800d5da70fa7c75c19ac2071eac3d4ebd3a696ee265e5cb"


def http_error(status: int) -> requests.HTTPError:
    """A ``requests.HTTPError`` as ``raise_for_status`` builds it."""
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status} Client Error", response=resp)


class RefusingForge(FakeForge):
    """A fake forge whose posts raise the given exceptions."""

    def __init__(self, *, summary=None, inline=None, summary_repost=None, **kw):
        super().__init__(**kw)
        self.summary_error = summary
        self.inline_error = inline
        self.repost_error = summary_repost
        self.summary_calls = 0

    def post_summary(self, ref, body):
        self.summary_calls += 1
        if self.summary_calls == 1 and self.summary_error is not None:
            raise self.summary_error
        if self.summary_calls > 1 and self.repost_error is not None:
            raise self.repost_error
        self.summaries.append(body)

    def post_inline_comments(self, ref, comments):
        if self.inline_error is not None:
            raise self.inline_error
        return super().post_inline_comments(ref, comments)


def finding(file="src/x.py", line=1, severity="warning", title="T", body="B", rule=None):
    return SimpleNamespace(
        file=file, line=line, severity=severity, title=title, body=body, rule=rule,
        drop_reason=None,
    )


def _drive(monkeypatch, capsys, forge, *argv):
    import prxref.llm_backends as backends

    monkeypatch.setattr(cli, "detect_forge", lambda url: REF)
    monkeypatch.setattr(cli, "make_forge", lambda ref: forge)
    monkeypatch.setattr(
        backends, "create_llm_client", lambda cfg: FakeLLM(findings_by_path=HAPPY_FINDINGS),
    )
    monkeypatch.setattr(cli.time, "perf_counter", lambda: 0.0)
    rc = cli.main(["review", "--pr-url", REF.url, *argv])
    out, err = capsys.readouterr()
    return rc, out, err


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


class TestDetectCi:
    @pytest.mark.parametrize(("env", "expected"), [
        ({"GITHUB_ACTIONS": "true"}, "github"),
        ({"TF_BUILD": "True"}, "azure"),
        ({"GITLAB_CI": "true"}, "gitlab"),
        ({"BITBUCKET_BUILD_NUMBER": "42"}, "bitbucket"),
        ({}, None),
        ({"GITHUB_ACTIONS": "false"}, None),
        ({"BITBUCKET_BUILD_NUMBER": ""}, None),
        ({"GITLAB_CI": "true", "GITHUB_ACTIONS": "true"}, "github"),
    ])
    def test_each_ci_is_named_from_its_variable(self, env, expected):
        assert detect_ci(env) == expected


class TestGithubAnnotations:
    def test_properties_and_message_are_escaped(self):
        f = finding(file="src/a,b:c.py", line=3, severity="error",
                    title="50% off: now, really", body="line1\nline2\r%")
        assert github_annotations([f]) == [
            "::error file=src/a%2Cb%3Ac.py,line=3,title=50%25 off%3A now%2C really::"
            "line1%0Aline2%0D%25",
        ]

    def test_the_message_keeps_colons_and_commas(self):
        f = finding(severity="warning", title="t", body="a: b, c")
        assert github_annotations([f]) == ["::warning file=src/x.py,line=1,title=t::a: b, c"]

    def test_severity_maps_to_the_command_and_errors_come_first(self):
        found = [finding(severity="outofscope", title="o"), finding(severity="warning", title="w"),
                 finding(severity="error", title="e")]
        assert [line.split(" ")[0] for line in github_annotations(found)] == [
            "::error", "::warning", "::notice",
        ]

    def test_a_finding_without_a_path_or_line_omits_them(self):
        assert github_annotations([finding(file="", line=0, title="t", body="b")]) == [
            "::warning title=t::b",
        ]

    def test_the_output_is_capped_at_fifty_keeping_the_errors(self):
        found = [finding(severity="warning", title=f"w{i}") for i in range(55)]
        found += [finding(severity="error", title=f"e{i}") for i in range(5)]
        lines = github_annotations(found)
        assert len(lines) == ci_fallback.GITHUB_ANNOTATION_CAP == 50
        assert sum(line.startswith("::error ") for line in lines) == 5
        assert sum(line.startswith("::warning ") for line in lines) == 45


class TestAzureLogIssues:
    def test_properties_and_message_are_escaped(self):
        f = finding(file="src/a;b].py", line=4, severity="error", title="a;b]", body="x\ny\rz")
        assert azure_log_issues([f]) == [
            "##vso[task.logissue type=error;sourcepath=src/a%3Bb%5D.py;linenumber=4;]"
            "a%3Bb%5D: x%0Ay%0Dz",
        ]

    def test_anything_but_error_is_a_warning(self):
        lines = azure_log_issues([finding(severity="outofscope"), finding(severity="error")])
        assert [line.split(";")[0] for line in lines] == [
            "##vso[task.logissue type=error", "##vso[task.logissue type=warning",
        ]


class TestGitlabCodequality:
    def test_the_entry_shape_and_severity_mapping(self):
        found = [finding(severity="error", title="E", body="b", rule="R1"),
                 finding(severity="warning", line=0), finding(severity="outofscope")]
        entries = gitlab_codequality(found)
        assert [e["severity"] for e in entries] == ["major", "minor", "info"]
        first = entries[0]
        assert set(first) == {"description", "check_name", "fingerprint", "severity", "location"}
        assert first["description"] == "E: b"
        assert first["check_name"] == "R1"
        assert entries[1]["check_name"] == "prxref"
        assert first["location"] == {"path": "src/x.py", "lines": {"begin": 1}}
        assert entries[1]["location"]["lines"]["begin"] == 1
        assert first["fingerprint"] == hashlib.sha256(
            json.dumps(["src/x.py", 1, "E"]).encode(),
        ).hexdigest()

    def test_the_fingerprint_is_stable_and_ignores_the_body(self):
        a = gitlab_codequality([finding(body="one")])[0]["fingerprint"]
        b = gitlab_codequality([finding(body="two")])[0]["fingerprint"]
        c = gitlab_codequality([finding(line=2)])[0]["fingerprint"]
        assert a == b != c


def test_the_step_summary_quotes_one_line_then_the_summary():
    assert step_summary_markdown("## Review") == (
        "> prxref could not post this review to the pull request, "
        "so it is shown here instead.\n\n## Review\n"
    )


class TestPostFailureCause:
    @pytest.mark.parametrize(("exc", "cause"), [
        (http_error(403), "permission"),
        (http_error(401), "permission"),
        (http_error(404), "error"),
        (http_error(500), "error"),
        (requests.ConnectionError("down"), "error"),
        (RuntimeError("boom"), "error"),
    ])
    def test_only_401_and_403_are_permission(self, exc, cause):
        assert post_failure_cause(exc) == cause


class TestTheOrchestratorRecord:
    def test_a_refused_summary_is_recorded_with_its_markdown(self):
        forge = RefusingForge(summary=http_error(403), diff=APP_DIFF)
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS))
        assert res["posted"] is False
        assert res["degraded"] == {
            "cause": "permission", "failed": ["summary"], "fallback": [], "annotations": 0,
        }
        assert "Null deref" in res[DEGRADED_SUMMARY_KEY]
        assert forge.inline_batches == []

    def test_a_refused_inline_batch_keeps_the_posted_summary(self):
        forge = RefusingForge(inline=http_error(401), diff=APP_DIFF)
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS))
        assert res["degraded"]["failed"] == ["inline"]
        assert res["degraded"]["cause"] == "permission"
        assert len(forge.summaries) == 2
        assert res[DEGRADED_SUMMARY_KEY] == forge.summaries[-1]

    def test_a_transport_failure_is_an_error_and_permission_wins_a_mix(self):
        forge = RefusingForge(inline=requests.ConnectionError("down"), diff=APP_DIFF)
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS))
        assert res["degraded"]["cause"] == "error"
        forge = RefusingForge(
            inline=requests.ConnectionError("down"), summary_repost=http_error(403), diff=APP_DIFF,
        )
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS))
        assert res["degraded"]["failed"] == ["inline", "summary"]
        assert res["degraded"]["cause"] == "permission"

    def test_an_inline_only_run_still_gets_a_summary_for_the_fallback(self):
        forge = RefusingForge(inline=http_error(403), diff=APP_DIFF)
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS), post_mode="inline",
        )
        assert forge.summaries == []
        assert res["degraded"]["failed"] == ["inline"]
        assert "Null deref" in res[DEGRADED_SUMMARY_KEY]

    def test_the_empty_diff_exit_records_a_refused_summary(self):
        forge = RefusingForge(summary=http_error(403), diff="")
        res = orchestrate_review(forge, REF, FakeLLM())
        assert res["degraded"]["failed"] == ["summary"]
        assert res[DEGRADED_SUMMARY_KEY]

    def test_the_error_exit_records_a_refused_notice(self):
        forge = RefusingForge(summary=http_error(403), diff=APP_DIFF)
        forge.fail.add("get_diff")
        res = orchestrate_review(forge, REF, FakeLLM())
        assert res["verdict"] == "Error"
        assert res["degraded"]["failed"] == ["summary"]
        assert "could not complete" in res[DEGRADED_SUMMARY_KEY]

    @pytest.mark.parametrize("post", [True, False])
    def test_no_failed_post_means_null_and_no_markdown_key(self, post):
        forge = FakeForge(diff=APP_DIFF)
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS), post=post)
        assert res["degraded"] is None
        assert DEGRADED_SUMMARY_KEY not in res
        assert DEGRADED_SUMMARY_KEY not in cli._build_json_result(res)


class TestTheCliEmits:
    def test_github_text_prints_annotations_and_appends_the_job_summary(
        self, monkeypatch, capsys, caplog, tmp_path,
    ):
        summary_file = tmp_path / "step-summary.md"
        summary_file.write_text("earlier step\n", encoding="utf-8")
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
        forge = RefusingForge(summary=http_error(403), diff=APP_DIFF)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            rc, out, _ = _drive(monkeypatch, capsys, forge)
        assert rc == 0
        lines = out.splitlines()
        assert lines[:2] == [ERROR_LINE, NOTICE_LINE]
        written = summary_file.read_text(encoding="utf-8")
        assert written.startswith("earlier step\n> prxref could not post this review")
        assert "Null deref" in written
        assert COULD_NOT_POST in _warnings(caplog)

    def test_github_json_keeps_stdout_one_document(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "s.md"))
        forge = RefusingForge(summary=http_error(403), diff=APP_DIFF)
        rc, out, _ = _drive(monkeypatch, capsys, forge, "--format", "json")
        assert rc == 0
        payload = json.loads(out)
        assert not any(line.startswith("::") for line in out.splitlines())
        assert payload["degraded"] == {
            "cause": "permission", "failed": ["summary"],
            "fallback": ["github-step-summary"], "annotations": 0,
        }
        assert list(payload)[list(payload).index("incremental") + 1] == "ci_wiring"
        assert list(payload)[list(payload).index("incremental") + 2] == "evidence"
        assert list(payload)[list(payload).index("incremental") + 3] == "stable_ids"
        assert list(payload)[list(payload).index("incremental") + 4] == "degraded"
        assert DEGRADED_SUMMARY_KEY not in payload
        assert (tmp_path / "s.md").is_file()

    def test_github_text_record_counts_the_annotations(self, monkeypatch, capsys):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        forge = RefusingForge(summary=http_error(403), diff=APP_DIFF)
        result = orchestrate_review(forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS))
        cli._emit_fallback(result, json_format=False)
        assert capsys.readouterr().out.splitlines() == [ERROR_LINE, NOTICE_LINE]
        assert result["degraded"]["fallback"] == ["github-annotations"]
        assert result["degraded"]["annotations"] == 2

    def test_an_unwritable_job_summary_is_logged_and_dropped(
        self, monkeypatch, capsys, caplog, tmp_path,
    ):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path))
        forge = RefusingForge(summary=http_error(403), diff=APP_DIFF)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            rc, out, _ = _drive(monkeypatch, capsys, forge, "--format", "json")
        assert rc == 0
        assert json.loads(out)["degraded"]["fallback"] == []
        assert any("GITHUB_STEP_SUMMARY" in m for m in _warnings(caplog))

    def test_azure_text_prints_logging_commands(self, monkeypatch, capsys):
        monkeypatch.setenv("TF_BUILD", "True")
        forge = RefusingForge(summary=http_error(401), diff=APP_DIFF)
        rc, out, _ = _drive(monkeypatch, capsys, forge)
        assert rc == 0
        assert out.splitlines()[:2] == [AZURE_ERROR, AZURE_OTHER]

    def test_azure_json_skips_them(self, monkeypatch, capsys):
        monkeypatch.setenv("TF_BUILD", "True")
        forge = RefusingForge(summary=http_error(401), diff=APP_DIFF)
        rc, out, _ = _drive(monkeypatch, capsys, forge, "--format", "json")
        assert rc == 0
        assert "##vso" not in out
        assert json.loads(out)["degraded"]["fallback"] == []

    def test_gitlab_writes_a_stable_code_quality_report(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("GITLAB_CI", "true")
        monkeypatch.chdir(tmp_path)
        (tmp_path / GITLAB_REPORT_FILE).write_text("stale", encoding="utf-8")
        reports = []
        for _ in range(2):
            forge = RefusingForge(summary=http_error(403), diff=APP_DIFF)
            rc, out, _ = _drive(monkeypatch, capsys, forge, "--format", "json")
            assert rc == 0
            assert json.loads(out)["degraded"]["fallback"] == ["gitlab-codequality"]
            assert json.loads(out)["degraded"]["annotations"] == 2
            reports.append(json.loads((tmp_path / GITLAB_REPORT_FILE).read_text(encoding="utf-8")))
        assert reports[0] == reports[1]
        assert [e["severity"] for e in reports[0]] == ["major", "info"]
        assert {e["location"]["path"] for e in reports[0]} == {"src/app.py"}
        assert all(len(e["fingerprint"]) == 64 for e in reports[0])

    @pytest.mark.parametrize("env", [{"BITBUCKET_BUILD_NUMBER": "7"}, {}])
    def test_bitbucket_and_no_ci_log_the_review(self, monkeypatch, capsys, caplog, env):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        forge = RefusingForge(summary=http_error(403), diff=APP_DIFF)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            rc, out, _ = _drive(monkeypatch, capsys, forge, "--format", "json")
        assert rc == 0
        assert json.loads(out)["degraded"]["fallback"] == ["log"]
        warnings = _warnings(caplog)
        assert warnings[-1] == COULD_NOT_POST
        assert warnings[-2].startswith("> prxref could not post this review")
        assert "Null deref" in warnings[-2]

    def test_fallback_off_emits_nothing_but_still_records(
        self, monkeypatch, capsys, caplog, tmp_path,
    ):
        monkeypatch.setenv("PRXREF_FALLBACK", "off")
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "s.md"))
        forge = RefusingForge(summary=http_error(403), diff=APP_DIFF)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            rc, out, _ = _drive(monkeypatch, capsys, forge)
        assert rc == 0
        assert not any(line.startswith("::") for line in out.splitlines())
        assert not (tmp_path / "s.md").exists()
        assert COULD_NOT_POST not in _warnings(caplog)
        forge = RefusingForge(summary=http_error(403), diff=APP_DIFF)
        _, out, _ = _drive(monkeypatch, capsys, forge, "--format", "json")
        assert json.loads(out)["degraded"] == {
            "cause": "permission", "failed": ["summary"], "fallback": [], "annotations": 0,
        }

    def test_an_unknown_fallback_value_is_a_configuration_error(self, monkeypatch, capsys):
        monkeypatch.setenv("PRXREF_FALLBACK", "on")
        rc, _, err = _drive(monkeypatch, capsys, FakeForge(diff=APP_DIFF))
        assert rc == 2
        assert "PRXREF_FALLBACK" in err

    def test_an_emission_failure_never_changes_the_exit(self, monkeypatch, capsys, caplog):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")

        def boom(findings):
            raise RuntimeError("renderer broke")

        monkeypatch.setattr(ci_fallback, "github_annotations", boom)
        forge = RefusingForge(summary=http_error(403), diff=APP_DIFF)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            rc, out, _ = _drive(monkeypatch, capsys, forge)
        assert rc == 0
        assert out.splitlines()[0].startswith("verdict: ")
        assert any("renderer broke" in m for m in _warnings(caplog))

    @pytest.mark.parametrize(("fail_on", "code"), [("never", 0), ("error", 1)])
    @pytest.mark.parametrize("fallback", ["auto", "off"])
    def test_the_exit_follows_fail_on_unchanged(self, monkeypatch, capsys, fail_on, code, fallback):
        monkeypatch.setenv("PRXREF_FAIL_ON", fail_on)
        monkeypatch.setenv("PRXREF_FALLBACK", fallback)
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        forge = RefusingForge(summary=http_error(403), diff=APP_DIFF)
        rc, _, _ = _drive(monkeypatch, capsys, forge)
        assert rc == code


class TestASuccessfulPostIsUnchanged:
    @pytest.mark.parametrize(("argv", "golden"), [
        ((), BASE_TEXT_SHA), (("-v",), BASE_VERBOSE_SHA),
    ])
    @pytest.mark.parametrize("ci", [{}, {"GITHUB_ACTIONS": "true"}, {"TF_BUILD": "True"}])
    def test_text_stdout_is_byte_identical_to_0_22_0(self, monkeypatch, capsys, argv, golden, ci):
        for name, value in ci.items():
            monkeypatch.setenv(name, value)
        forge = FakeForge(diff=APP_DIFF)
        rc, out, _ = _drive(monkeypatch, capsys, forge, *argv)
        assert rc == 0
        assert _sha(out) == golden
        assert _sha("\x00".join(forge.summaries)) == BASE_SUMMARIES_SHA

    def test_json_differs_from_0_22_0_only_by_a_null_degraded(
        self, monkeypatch, capsys, tmp_path,
    ):
        monkeypatch.setenv("GITLAB_CI", "true")
        monkeypatch.chdir(tmp_path)
        forge = FakeForge(diff=APP_DIFF)
        rc, out, _ = _drive(monkeypatch, capsys, forge, "--format", "json")
        assert rc == 0
        payload = json.loads(out)
        assert payload.pop("degraded") is None
        assert payload.pop("config_file") is None
        assert payload.pop("rule_scope_cleared") is None  # 0.29 (#75): null when no scope declared
        # #66: on by default (OD2); this forge has no reader, so the check records why.
        assert payload.pop("ci_wiring") == {"triggered": False, "reason": "no reader"}
        assert payload.pop("evidence") is None  # #69: null when no evidence file is configured
        assert payload.pop("stable_ids") is not None  # #71: ids are on by default
        assert payload.pop("metadata_rules") is None  # #70: null when PRXREF_METADATA_RULES is off
        assert payload.pop("failed_chunks") == []
        assert [payload.pop(key) for key in (
            "chunks_over_budget", "largest_chunk_tokens", "overflow_files", "chunk_token_budget",
        )] == [0, 800, 0, 25000]
        for row in payload["findings"]:
            assert row.pop("anchor_unverified") is False  # 0.3x (#74): null-free, never stamped here
            assert row.pop("id").startswith("src/app.py#")  # #71: ids are on by default
            assert (row.pop("anchor_block"), row.pop("id_reused_from")) == (None, None)
        assert _sha(json.dumps(payload)) == BASE_JSON_SHA
        assert not (tmp_path / GITLAB_REPORT_FILE).exists()
