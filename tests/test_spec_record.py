"""The spec stage's operator record: its log lines, run-record key and trace event.

A spec source that fails is posted only in the summary note, so a
``--no-post``, dry-run or inline-only run learns about it from three other
places, all tested here through the real ``orchestrate_review``:

- one WARNING per failed source, naming its ordinal, kind and a log-safe
  origin (a path verbatim; a URL without userinfo, query or fragment), with
  the reason redacted;
- one INFO line, ``spec grounding: ok/total source(s) ok, N constraint(s)
  injected``, where N is what actually reached the prompts;
- ``run_inputs["spec_grounding"]``: ``None`` when no spec stage ran, a
  path-free dict otherwise, and a fixed crash shape when the stage raised;
- one ``specs`` trace event, ``ok`` or ``fail`` (no source fetched, or a
  crash), whose ``fail`` form carries the raw reasons.

Local sources are real files under ``tmp_path``. A URL source goes through
the real ``fetch_specs`` with a session that refuses to connect, so nothing
touches the network. The reviewer is the real one.
"""
from __future__ import annotations

import hashlib
import json
import logging

import pytest
import requests

from prxref import orchestrator, specs
from prxref.llm import InvokeResult
from prxref.orchestrator import _log_safe_origin, orchestrate_review, redact_for_post
from tests.test_orchestrator import REF, FakeForge, _added_file_diff

DIFF = _added_file_diff("src/app.py", 20)

NO_SPECS = "(no specs provided for this review)"

SPEC_DOC = (
    "# Data rules\n\n"
    "The data token MUST NOT be logged.\n\n"
    "Every data line SHOULD be validated.\n"
)

SPEC_DOC_CONSTRAINTS = 2

SECRET_URL = "https://alice:s3cret-pw@specs.example.invalid:8443/team/api.md?token=q-secret#frag-secret"

SAFE_URL = "https://specs.example.invalid:8443/team/api.md"

CONNECT_ERROR = (
    "HTTPSConnectionPool(host='specs.example.invalid', port=8443): Max retries "
    "exceeded with url: /team/api.md?token=q-secret"
)

WARNING_FORMAT = "spec source %d/%d (%s, %s) failed (best-effort): %s"

RECORD_KEYS = ["sources", "ok", "failed", "constraints", "digest_sha256"]


class CleanLLM:
    """Answers every unit with no findings and records each user prompt."""

    def __init__(self):
        self.prompts: list[str] = []

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        self.prompts.append(user)
        return InvokeResult(
            text=json.dumps({"findings": [], "escalations": []}),
            input_tokens=10, output_tokens=5, model="test-model-1",
            backend="fake", elapsed_ms=1,
        )


class DeadLLM:
    """Fails every unit, so the run takes the total-failure exit."""

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        raise RuntimeError("gateway down")


class RefusingSession:
    """A spec session whose every GET fails to connect, as an unreachable host does."""

    def get(self, url, **kwargs):
        raise requests.ConnectionError(CONNECT_ERROR)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(specs, "_create_default_session", RefusingSession)


@pytest.fixture
def digests(monkeypatch):
    """Record every digest the real ``build_spec_digest`` returns."""
    seen: list[str] = []
    real = specs.build_spec_digest

    def recording(*args, **kwargs):
        digest = real(*args, **kwargs)
        seen.append(digest)
        return digest

    monkeypatch.setattr(orchestrator.specs, "build_spec_digest", recording)
    return seen


def _spec_file(tmp_path, text: str = SPEC_DOC, name: str = "spec.md") -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _missing(tmp_path) -> str:
    return str(tmp_path / "team specs" / "missing.md")


def _review(tmp_path, sources=(), *, llm=None, diff: str = DIFF, **kw):
    forge = FakeForge(diff=diff)
    trace = tmp_path / "run.jsonl"
    res = orchestrate_review(
        forge, REF, llm or CleanLLM(), spec_sources=list(sources),
        trace_file=str(trace), **kw,
    )
    events = [json.loads(x) for x in trace.read_text().splitlines() if x.strip()]
    return forge, res, events


def _stage_events(events: list[dict]) -> list[tuple[str, dict]]:
    return [
        (e["phase"], e.get("meta", {})) for e in events
        if e["node"] == "specs" and e["phase"] in ("ok", "fail")
    ]


def _source_warnings(caplog) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.msg == WARNING_FORMAT
    ]


def _grounding_infos(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.INFO
        and r.getMessage().startswith("spec grounding: ")
        and "relabelled" not in r.getMessage()
    ]


class TestLogSafeOrigin:
    """Logs name the source; they never carry its credentials."""

    @pytest.mark.parametrize(
        ("origin", "expected"),
        [
            (SECRET_URL, SAFE_URL),
            ("https://bot@specs.example.invalid/api.md?sig=abc", "https://specs.example.invalid/api.md"),
            ("http://specs.example.invalid/doc;jsessionid=abc123", "http://specs.example.invalid/doc"),
            ("https://specs.example.invalid?token=abc#x", "https://specs.example.invalid"),
            ("https://[2001:db8::1]:8080/spec.md?k=v", "https://[2001:db8::1]:8080/spec.md"),
            ("  https://u:p@specs.example.invalid/a.md  ", "https://specs.example.invalid/a.md"),
        ],
    )
    def test_a_url_keeps_scheme_host_port_and_path_only(self, origin, expected):
        assert _log_safe_origin(origin) == expected

    @pytest.mark.parametrize(
        "origin",
        [
            "docs/spec.md",
            "/abs/team specs/api.md",
            "docs/what?-is-this#1.md",
            "C:\\specs\\api.md",
            "",
        ],
    )
    def test_a_path_is_verbatim(self, origin):
        assert _log_safe_origin(origin) == origin

    def test_a_malformed_url_is_not_echoed(self):
        assert _log_safe_origin("https://u:s3cret@[::1/x") == "[unparseable origin]"


class TestFailedSourceWarning:
    """One WARNING per failed source: ordinal, kind, log-safe origin, redacted reason."""

    def test_each_failed_source_is_named_by_ordinal_kind_and_safe_origin(
        self, tmp_path, caplog,
    ):
        caplog.set_level(logging.INFO, logger="prxref")
        missing = _missing(tmp_path)
        good = _spec_file(tmp_path)
        _review(tmp_path, [missing, SECRET_URL, good], post=False)
        warnings = _source_warnings(caplog)
        url_reason = redact_for_post(f"ConnectionError: {CONNECT_ERROR}")
        assert [r.args for r in warnings] == [
            (1, 3, "unknown", missing, "not a URL or path"),
            (2, 3, "url", SAFE_URL, url_reason),
        ]
        assert [r.getMessage() for r in warnings] == [
            f"spec source 1/3 (unknown, {missing}) failed (best-effort): not a URL or path",
            f"spec source 2/3 (url, {SAFE_URL}) failed (best-effort): {url_reason}",
        ]

    def test_no_prxref_log_line_carries_the_url_credentials(self, tmp_path, caplog):
        caplog.set_level(logging.DEBUG, logger="prxref")
        _review(tmp_path, [SECRET_URL], post=False)
        assert _source_warnings(caplog)
        messages = [r.getMessage() for r in caplog.records]
        for secret in ("alice", "s3cret-pw", "q-secret", "frag-secret"):
            assert not [m for m in messages if secret in m], secret

    def test_a_path_with_a_space_is_logged_whole(self, tmp_path, caplog):
        caplog.set_level(logging.INFO, logger="prxref")
        missing = _missing(tmp_path)
        _review(tmp_path, [missing], post=False)
        (record,) = _source_warnings(caplog)
        assert record.args[3] == missing
        assert f"(unknown, {missing})" in record.getMessage()

    def test_an_ok_source_logs_no_warning(self, tmp_path, caplog):
        caplog.set_level(logging.INFO, logger="prxref")
        _review(tmp_path, [_spec_file(tmp_path)], post=False)
        assert _source_warnings(caplog) == []

    def test_the_warnings_survive_a_digest_crash(self, tmp_path, caplog, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("digest exploded")

        monkeypatch.setattr(orchestrator.specs, "build_spec_digest", boom)
        caplog.set_level(logging.INFO, logger="prxref")
        llm = CleanLLM()
        _forge, res, _events = _review(
            tmp_path, [_missing(tmp_path), _spec_file(tmp_path)], llm=llm, post=False,
        )
        assert [r.args[:3] for r in _source_warnings(caplog)] == [(1, 2, "unknown")]
        assert res["spec_grounding"]["failed"] == ["spec stage crashed: RuntimeError"]
        assert llm.prompts and all(NO_SPECS in p for p in llm.prompts)


class TestGroundingInfoLine:
    """Exactly one INFO line; its last count is what reached the prompts."""

    def test_a_grounded_run_counts_the_injected_constraints(self, tmp_path, caplog):
        caplog.set_level(logging.INFO, logger="prxref")
        _review(tmp_path, [_spec_file(tmp_path)], post=False)
        assert _grounding_infos(caplog) == [
            f"spec grounding: 1/1 source(s) ok, {SPEC_DOC_CONSTRAINTS} constraint(s) injected",
        ]

    def test_an_ungrounded_run_injects_zero(self, tmp_path, caplog, digests):
        caplog.set_level(logging.INFO, logger="prxref")
        _review(tmp_path, [_spec_file(tmp_path)], post=False, spec_digest_tokens=1)
        assert digests and digests[0]
        assert _grounding_infos(caplog) == [
            "spec grounding: 1/1 source(s) ok, 0 constraint(s) injected",
        ]

    def test_an_all_failed_run_counts_zero_of_n(self, tmp_path, caplog):
        caplog.set_level(logging.INFO, logger="prxref")
        _review(tmp_path, [_missing(tmp_path), SECRET_URL], post=False)
        assert _grounding_infos(caplog) == [
            "spec grounding: 0/2 source(s) ok, 0 constraint(s) injected",
        ]

    def test_a_partial_failure_counts_ok_of_all(self, tmp_path, caplog):
        caplog.set_level(logging.INFO, logger="prxref")
        _review(tmp_path, [_missing(tmp_path), _spec_file(tmp_path)], post=False)
        assert _grounding_infos(caplog) == [
            f"spec grounding: 1/2 source(s) ok, {SPEC_DOC_CONSTRAINTS} constraint(s) injected",
        ]


class TestSpecGroundingRecord:
    """``spec_grounding``: None, the normal dict, or the crash dict."""

    def test_none_when_no_sources_are_configured(self, tmp_path):
        _forge, res, _events = _review(tmp_path, [], post=False)
        assert res["spec_grounding"] is None

    def test_none_when_the_run_exits_before_the_spec_stage(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            orchestrator.specs, "fetch_specs",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("spec stage reached")),
        )
        _forge, res, events = _review(tmp_path, ["docs/spec.md"], diff="", post=False)
        assert res["verdict"] == "Approved"
        assert res["spec_grounding"] is None
        assert _stage_events(events) == []

    def test_the_normal_record_is_path_free_and_hashes_the_injected_digest(
        self, tmp_path, digests,
    ):
        missing = _missing(tmp_path)
        _forge, res, _events = _review(
            tmp_path, [missing, SECRET_URL, _spec_file(tmp_path)], post=False,
        )
        record = res["spec_grounding"]
        assert list(record) == RECORD_KEYS
        assert record == {
            "sources": 3,
            "ok": 1,
            "failed": [
                "source 1: not a URL or path",
                f"source 2 (url): {redact_for_post(f'ConnectionError: {CONNECT_ERROR}')}",
            ],
            "constraints": SPEC_DOC_CONSTRAINTS,
            "digest_sha256": hashlib.sha256(digests[0].encode("utf-8")).hexdigest(),
        }
        text = json.dumps(record)
        for leak in (str(tmp_path), "team specs", "specs.example.invalid", "s3cret-pw", "q-secret"):
            assert leak not in text, leak

    def test_an_ungrounded_record_hashes_nothing(self, tmp_path, digests):
        _forge, res, _events = _review(
            tmp_path, [_spec_file(tmp_path)], post=False, spec_digest_tokens=1,
        )
        assert digests and digests[0]
        assert res["spec_grounding"] == {
            "sources": 1, "ok": 1, "failed": [], "constraints": 0, "digest_sha256": None,
        }

    def test_an_all_failed_record(self, tmp_path):
        _forge, res, _events = _review(tmp_path, [_missing(tmp_path)], post=False)
        assert res["spec_grounding"] == {
            "sources": 1, "ok": 0, "failed": ["source 1: not a URL or path"],
            "constraints": 0, "digest_sha256": None,
        }

    def test_the_crash_record(self, tmp_path, monkeypatch, caplog):
        def boom(*a, **k):
            raise RuntimeError("boom at /private/team/specs")

        monkeypatch.setattr(orchestrator.specs, "fetch_specs", boom)
        caplog.set_level(logging.INFO, logger="prxref")
        _forge, res, _events = _review(tmp_path, ["a.md", "b.md"], post=False)
        assert res["spec_grounding"] == {
            "sources": 2, "ok": 0, "failed": ["spec stage crashed: RuntimeError"],
            "constraints": 0, "digest_sha256": None,
        }
        assert any(
            r.levelno == logging.ERROR
            and r.getMessage() == "spec grounding failed (best-effort): boom at /private/team/specs"
            for r in caplog.records
        )
        assert _grounding_infos(caplog) == []
        assert res["verdict"] == "Approved"

    def test_a_lone_surrogate_in_the_digest_is_hashed_not_raised(
        self, tmp_path, monkeypatch, digests,
    ):
        statement = json.loads('"The data token MUST NOT be logged \\ud800 ever."')
        ticket = specs.SpecSource(
            origin="https://jira.example.invalid/browse/PROJ-1", kind="jira",
            text=f"Summary: Keep data private\n\n{statement}\n", error="",
        )
        monkeypatch.setattr(orchestrator.specs, "fetch_specs", lambda *a, **k: [ticket])
        _forge, res, _events = _review(tmp_path, [ticket.origin], post=False)
        assert "\ud800" in digests[0]
        assert res["spec_grounding"]["digest_sha256"] == hashlib.sha256(
            digests[0].encode("utf-8", "surrogatepass"),
        ).hexdigest()

    def test_the_total_failure_exit_still_carries_the_record(self, tmp_path):
        _forge, res, _events = _review(
            tmp_path, [_spec_file(tmp_path)], llm=DeadLLM(), post=False,
        )
        assert res["verdict"] == "Error"
        assert res["spec_grounding"]["ok"] == 1
        assert res["spec_grounding"]["constraints"] == SPEC_DOC_CONSTRAINTS

    @pytest.mark.parametrize("shape", ["normal", "crash"])
    def test_the_record_is_json_native_and_trace_safe(self, tmp_path, monkeypatch, shape):
        if shape == "crash":
            monkeypatch.setattr(
                orchestrator.specs, "fetch_specs",
                lambda *a, **k: (_ for _ in ()).throw(ValueError("x")),
            )
        _forge, res, _events = _review(
            tmp_path, [_missing(tmp_path), _spec_file(tmp_path)], post=False,
        )
        record = res["spec_grounding"]
        assert json.loads(json.dumps(record)) == record
        assert not {"node", "phase"} & set(record)


class TestSpecsTraceEvent:
    """``specs ok`` / ``specs fail`` with ``sources, ok, constraints``; raw reasons on fail."""

    def test_ok_when_a_source_was_fetched(self, tmp_path):
        _forge, _res, events = _review(tmp_path, [_spec_file(tmp_path)], post=False)
        assert _stage_events(events) == [
            ("ok", {"sources": 1, "ok": 1, "constraints": SPEC_DOC_CONSTRAINTS}),
        ]

    def test_ok_with_a_partial_failure_carries_no_reasons(self, tmp_path):
        _forge, _res, events = _review(
            tmp_path, [_missing(tmp_path), _spec_file(tmp_path)], post=False,
        )
        assert _stage_events(events) == [
            ("ok", {"sources": 2, "ok": 1, "constraints": SPEC_DOC_CONSTRAINTS}),
        ]

    def test_ok_even_when_nothing_was_injected(self, tmp_path):
        _forge, _res, events = _review(
            tmp_path, [_spec_file(tmp_path)], post=False, spec_digest_tokens=1,
        )
        assert _stage_events(events) == [("ok", {"sources": 1, "ok": 1, "constraints": 0})]

    def test_fail_when_no_source_was_fetched_with_raw_reasons(self, tmp_path):
        _forge, _res, events = _review(
            tmp_path, [_missing(tmp_path), SECRET_URL], post=False,
        )
        assert _stage_events(events) == [
            ("fail", {
                "sources": 2, "ok": 0, "constraints": 0,
                "reasons": [
                    "source 1: not a URL or path",
                    f"source 2 (url): ConnectionError: {CONNECT_ERROR}",
                ],
            }),
        ]

    def test_fail_when_the_stage_crashed(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("boom specs")

        monkeypatch.setattr(orchestrator.specs, "fetch_specs", boom)
        _forge, _res, events = _review(tmp_path, ["docs/spec.md"], post=False)
        assert _stage_events(events) == [
            ("fail", {
                "sources": 1, "ok": 0, "constraints": 0,
                "reasons": ["spec stage crashed: RuntimeError: boom specs"],
            }),
        ]


class TestFeatureOff:
    """No sources: no stage runs, and nothing about the run mentions one."""

    def test_no_sources_leaves_posts_record_trace_and_logs_untouched(
        self, tmp_path, monkeypatch, caplog,
    ):
        def poisoned(*a, **k):
            raise AssertionError("spec stage ran")

        monkeypatch.setattr(orchestrator.specs, "fetch_specs", poisoned)
        monkeypatch.setattr(orchestrator.specs, "build_spec_digest", poisoned)
        caplog.set_level(logging.DEBUG, logger="prxref")
        llm = CleanLLM()
        forge, res, events = _review(tmp_path, [], llm=llm)
        assert res["spec_grounding"] is None
        assert [e for e in events if e["node"] == "specs"] == []
        assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("spec ")] == []
        assert "Spec-grounded" not in forge.summaries[0]
        assert "Spec fetch failed" not in forge.summaries[0]
        assert llm.prompts and all(NO_SPECS in p for p in llm.prompts)
