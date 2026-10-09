"""Gate worker: capture, rules at base, deadline, binding, loop (#102)."""

import json
import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest

from prxref import llm_backends
from prxref.forges.base import InlineComment
from prxref.gate import checks, worker
from prxref.gate.store import LeaseLost, Store
from tests.gate.test_rules_source import _ShaForge
from tests.test_issue_68_standards_context import TWO_CHUNK_DIFF, _RecordingLLM, _write
from tests.test_orchestrator import REF, FakeForge


class _PinnedForge(_ShaForge):
    """_ShaForge whose pinned diff is the real one and whose unpinned ``get_diff`` is a trap."""

    def __init__(self, diff, roots):
        super().__init__(diff, roots)
        self.compare_calls = []

    def get_diff(self, ref):
        return "UNPINNED"

    def get_compare_diff(self, ref, *, base_sha, head_sha):
        self.compare_calls.append((base_sha, head_sha))
        return self.diff


REPO, HEAD, BASE = "acme/widget", "a" * 40, "b" * 40
SECRET = "ghs_SECRETVALUE123"


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "gate.db")
    yield opened
    opened.close()


@pytest.fixture
def lease(store):
    return checks.Lease("host:1", store.acquire_lease("host:1", 600), 600)


@pytest.fixture
def gen(store):
    return store.enqueue(REPO, 7, HEAD, BASE, "main", "opened")


def _roots(tmp_path, base_rules):
    base, head = tmp_path / "base", tmp_path / "head"
    _write(base, ".prxref.toml", 'review_rules = "rules/team.md"\n')
    _write(base, "rules/team.md", base_rules)
    _write(head, ".prxref.toml", 'review_rules = ""\n')
    return {BASE: base, HEAD: head}


def test_installation_forge_ignores_the_env_token(monkeypatch):
    monkeypatch.setenv("PRXREF_GITHUB_TOKEN", "env-token")
    headers = worker.InstallationForge(lambda: "ghs_inst")._headers("github.com", {"X": "1"})
    assert headers["Authorization"] == "Bearer ghs_inst"
    assert headers["X"] == "1"


def test_capturing_forge_pins_shas_and_captures_posts(gen):
    inner = FakeForge(diff=TWO_CHUNK_DIFF)
    inner.get_diff = lambda ref: pytest.fail("get_diff is unpinned")
    compare = []

    def get_compare_diff(ref, *, base_sha, head_sha):
        compare.append((base_sha, head_sha))
        return TWO_CHUNK_DIFF

    inner.get_compare_diff = get_compare_diff
    cap = worker.CapturingForge(inner, gen)
    pr = cap.get_pr(REF)
    assert (pr.source_sha, pr.target_sha) == (gen.head_sha, gen.base_sha)
    assert cap.get_diff(REF) == TWO_CHUNK_DIFF and cap.get_summary(REF) is None
    assert compare == [(gen.base_sha, gen.head_sha)]
    comment = InlineComment(path="a.py", line=1, body="x")
    cap.post_summary(REF, "summary")
    assert cap.post_inline_comments(REF, [comment]) == 1
    cap.seal()
    cap.post_summary(REF, "late")
    cap.post_inline_comments(REF, [comment])
    assert cap.captured() == checks.Captured(summary="summary", comments=(comment,))
    assert inner.summaries == [] and inner.inline_batches == []


@pytest.mark.parametrize("source, present", [("base", True), ("head", False)])
def test_review_reads_the_repo_config_at_the_rules_sha(tmp_path, monkeypatch, gen, source, present):
    llm = _RecordingLLM()
    monkeypatch.setattr(llm_backends, "create_llm_client", lambda cfg: llm)
    monkeypatch.setenv("PRXREF_RULES_SOURCE", source)
    inner = _PinnedForge(TWO_CHUNK_DIFF, _roots(tmp_path, "- RULE-SENTINEL: never log tokens.\n"))
    outcome = worker.review_with_prxref(gen, inner, rules_source=source, runs_dir=tmp_path / "runs")
    prompts = "\n".join(f"{system}\n{user}" for system, user in llm.calls)
    assert ("RULE-SENTINEL" in prompts) is present
    record = json.loads((outcome.run_dir / "result.json").read_text())
    assert record["verdict"] == outcome.result["verdict"]
    run_record = json.loads((outcome.run_dir / "run_record.json").read_text())
    assert "failed_chunks" in run_record
    for name in ("result.json", "run_record.json"):
        assert SECRET not in (outcome.run_dir / name).read_text()
    assert outcome.run_dir == tmp_path / "runs" / str(gen.id)


def test_litellm_receives_the_configured_timeout_unscaled(tmp_path, monkeypatch, gen):
    captured = []

    def fake_completion(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"findings": []}'))],
            usage=None,
            model="m",
        )

    monkeypatch.setenv("PRXREF_LLM_BACKEND", "litellm")
    monkeypatch.setenv("PRXREF_LLM_MODELS", "bedrock/test-model")
    monkeypatch.setenv("PRXREF_LLM_TIMEOUT", "900")
    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(completion=fake_completion))
    inner = _PinnedForge(TWO_CHUNK_DIFF, _roots(tmp_path, "- a rule\n"))
    worker.review_with_prxref(gen, inner, rules_source="base", runs_dir=tmp_path / "runs")
    assert captured
    assert {call["timeout"] for call in captured} == {900.0}


class _Client:
    """Answers GET /pulls/7 with a scripted live PR."""

    def __init__(self, head=HEAD, base=BASE, state="open"):
        self.pull = {"head": {"sha": head}, "base": {"sha": base, "ref": "main"}, "state": state, "merged": False}

    def request(self, method, repo, path, **kwargs):
        assert (method, path) == ("GET", "/pulls/7")
        return self.pull


class _Publisher:
    def __init__(self, store, complete_raises=None):
        self.store = store
        self.calls = []
        self.complete_raises = complete_raises
        self.reasons = []
        self.capture = None
        self.sealed_at_fail = None

    def begin(self, gen, lease):
        self.calls.append("begin")
        self.store.set_check_run(gen.id, lease.token, 1)
        return 1

    def complete(self, gen, lease, result, captured, *, effort):
        self.calls.append("complete")
        if self.complete_raises:
            raise self.complete_raises
        return checks.conclusion_for(result)[0]

    def fail(self, gen, lease, reason):
        self.calls.append(f"fail:{reason}")
        self.reasons.append(reason)
        if self.capture is not None:
            self.capture.post_summary(REF, "after fail")
            self.sealed_at_fail = self.capture.captured().summary is None

    def cancel(self, gen, lease, reason):
        self.calls.append(f"cancel:{reason}")


def _outcome(result, tmp_path):
    return worker.ReviewOutcome(result, checks.Captured(), tmp_path)


def _run(store, gen, lease, review, *, client=None, publisher=None, deadline_s=5.0):
    publisher = publisher or _Publisher(store)
    state = worker.run_generation(
        gen,
        store=store,
        publisher=publisher,
        client=client or _Client(),
        lease=lease,
        review=review,
        deadline_s=deadline_s,
        effort="high",
    )
    return state, publisher.calls, store.get(gen.id)


def test_an_approved_review_completes_as_success(store, gen, lease, tmp_path):
    state, calls, row = _run(store, gen, lease, lambda g, register=None: _outcome({"verdict": "Approved"}, tmp_path))
    assert (state, calls, row.conclusion) == ("completed", ["begin", "complete"], "success")


def test_a_deadline_hit_is_a_failure(store, gen, lease):
    release = threading.Event()

    publisher = _Publisher(store)

    def slow(g, register=None):
        capture = worker.CapturingForge(FakeForge(diff=TWO_CHUNK_DIFF), g)
        register(capture)
        publisher.capture = capture
        release.wait(5)

    state, calls, row = _run(store, gen, lease, slow, deadline_s=0.05, publisher=publisher)
    release.set()
    assert (state, row.state, row.conclusion) == ("failed", "failed", "failure")
    assert calls[-1].startswith("fail:deadline")
    assert publisher.sealed_at_fail is True


def test_a_crash_mid_review_is_a_failure(store, gen, lease):
    def crash(g, register=None):
        raise RuntimeError("model went away")

    state, calls, row = _run(store, gen, lease, crash)
    assert (state, row.conclusion) == ("failed", "failure")
    assert calls[-1] == "fail:review crashed: RuntimeError"


@pytest.mark.parametrize("result", ["garbage", {"verdict": "Approved", "degraded": {"x": 1}}])
def test_a_malformed_or_degraded_result_completes_as_failure(store, gen, lease, tmp_path, result):
    state, _calls, row = _run(store, gen, lease, lambda g, register=None: _outcome(result, tmp_path))
    assert (state, row.conclusion) == ("completed", "failure")


def test_a_moved_head_goes_stale_and_queues_the_live_head(store, gen, lease, tmp_path):
    client = _Client(head="c" * 40)
    state, calls, row = _run(store, gen, lease, lambda g, register=None: pytest.fail("must not review"), client=client)
    assert (state, row.state, row.error, calls) == ("stale", "stale", "head moved", [])
    newer = store.latest_generation(REPO, 7)
    assert (newer.head_sha, newer.reason) == ("c" * 40, "repair")


def test_a_closed_pr_cancels(store, gen, lease):
    state, calls, row = _run(
        store, gen, lease, lambda g, register=None: pytest.fail("no"), client=_Client(state="closed")
    )
    assert (state, row.state, calls) == ("cancelled", "cancelled", ["cancel:PR closed"])


def test_stale_at_publish_publishes_nothing_more(store, gen, lease, tmp_path):
    publisher = _Publisher(store, complete_raises=checks.Stale("superseded"))
    state, calls, row = _run(
        store, gen, lease, lambda g, register=None: _outcome({"verdict": "Approved"}, tmp_path), publisher=publisher
    )
    assert (state, row.state, calls) == ("stale", "stale", ["begin", "complete"])


def test_a_publish_error_finishes_failed_and_says_so(store, gen, lease, tmp_path):
    publisher = _Publisher(store, complete_raises=checks.GitHubError("PATCH: HTTP 422", status=422))
    state, calls, row = _run(
        store, gen, lease, lambda g, register=None: _outcome({"verdict": "Approved"}, tmp_path), publisher=publisher
    )
    assert (state, row.state, row.conclusion) == ("failed", "failed", "failure")
    assert calls == ["begin", "complete", "fail:publish failed: GitHubError"]


def _worker(store, tmp_path, review=None, holder="host:1"):
    return worker.Worker(
        store,
        _Publisher(store),
        _Client(),
        review or (lambda g, register=None: _outcome({"verdict": "Approved"}, tmp_path)),
        deadline_s=5.0,
        effort="high",
        holder=holder,
    )


def test_a_paused_worker_dequeues_nothing(store, gen, tmp_path):
    w = _worker(store, tmp_path)
    lease = w.acquire()
    store.set_paused(True)
    assert w.run_once(lease) is None
    assert store.get(gen.id).state == "queued"


def test_a_fresh_worker_adopts_an_orphan_of_a_dead_holder(store, gen, tmp_path):
    old = store.acquire_lease("host:999999", 600)
    store.start(gen.id, old)
    w = _worker(store, tmp_path, holder="host:2")
    assert w.acquire() is None
    store.acquire_lease("host:999999", -1)
    lease = w.acquire()
    assert lease is not None and store.get(gen.id).state == "queued"
    assert w.run_once(lease) == "completed"


def test_a_lost_lease_raises_before_dequeueing(store, gen, tmp_path):
    w = _worker(store, tmp_path)
    lease = w.acquire()
    store.acquire_lease("other:1", 600, holder_alive=lambda h: False)
    with pytest.raises(LeaseLost):
        w.run_once(lease)
    assert store.get(gen.id).state == "queued"


def test_a_crash_never_leaks_the_installation_token(store, gen, lease, caplog):
    publisher = _Publisher(store)
    token_source = lambda: SECRET  # noqa: E731

    def crash(g, register=None):
        raise RuntimeError(f"401 for Bearer {token_source()} and token {token_source()}")

    with caplog.at_level("DEBUG"):
        state, _calls, row = _run(store, gen, lease, crash, publisher=publisher)
    assert state == "failed"
    assert SECRET not in (row.error or "")
    assert all(SECRET not in reason for reason in publisher.reasons)
    assert SECRET not in caplog.text


def test_a_publish_error_never_leaks_the_installation_token(store, gen, lease, tmp_path, caplog):
    boom = checks.GitHubError(f"PATCH failed with {SECRET}", status=422)
    publisher = _Publisher(store, complete_raises=boom)
    with caplog.at_level("DEBUG"):
        state, _calls, row = _run(
            store, gen, lease, lambda g, register=None: _outcome({"verdict": "Approved"}, tmp_path), publisher=publisher
        )
    assert state == "failed"
    assert SECRET not in (row.error or "")
    assert all(SECRET not in reason for reason in publisher.reasons)
    assert SECRET not in caplog.text


def test_redact_masks_tokens():
    text = worker._redact(f"x {SECRET} Bearer abc123 token xyz github_pat ghp_AAA")
    assert "SECRETVALUE" not in text and "abc123" not in text and "xyz" not in text
    assert "ghp_AAA" not in text
    assert "github_pat_11AAAA_bbb" not in worker._redact("x github_pat_11AAAA_bbb y") and "[redacted]" in text


def test_an_unreadable_live_pr_fails_the_generation_instead_of_stranding_it(store, gen, lease):
    class _Down:
        def request(self, method, repo, path, **kwargs):
            raise checks.UncertainReply("GET: timeout")

    state, calls, row = _run(store, gen, lease, lambda g, register=None: pytest.fail("no"), client=_Down())
    assert (state, row.state, row.conclusion) == ("failed", "failed", "failure")
    assert calls == ["fail:publish failed: UncertainReply"]


def test_a_rules_file_missing_at_base_fails_closed(tmp_path, monkeypatch, store, gen, lease):
    llm = _RecordingLLM()
    monkeypatch.setattr(llm_backends, "create_llm_client", lambda cfg: llm)
    base, head = tmp_path / "base", tmp_path / "head"
    _write(base, ".prxref.toml", 'review_rules = "rules/team.md"\n')
    _write(head, ".prxref.toml", 'review_rules = "rules/team.md"\n')
    _write(head, "rules/team.md", "- HEAD-SENTINEL: only at head.\n")
    inner = _PinnedForge(TWO_CHUNK_DIFF, {BASE: base, HEAD: head})
    publisher = _Publisher(store)

    def review(g, register=None):
        return worker.review_with_prxref(g, inner, rules_source="base", runs_dir=tmp_path / "runs", register=register)

    state, calls, row = _run(store, gen, lease, review, publisher=publisher)
    assert (state, row.state, row.conclusion) == ("failed", "failed", "failure")
    assert calls[-1] == "fail:review crashed: ConfigError"
    assert "HEAD-SENTINEL" not in "\n".join(f"{system}\n{user}" for system, user in llm.calls)


def test_a_failed_fail_publish_still_finishes_the_row_failed(store, gen, lease):
    class _BrokenFail(_Publisher):
        def fail(self, gen, lease, reason):
            self.calls.append("fail-attempt")
            raise checks.GitHubError("PATCH: HTTP 500", status=500)

    release = threading.Event()

    def slow(g, register=None):
        release.wait(5)

    publisher = _BrokenFail(store)
    state, calls, row = _run(store, gen, lease, slow, deadline_s=0.05, publisher=publisher)
    release.set()
    assert (state, row.state, row.conclusion) == ("failed", "failed", "failure")
    assert calls[-1] == "fail-attempt"


def test_a_failed_fail_publish_after_a_crash_still_finishes_the_row(store, gen, lease):
    class _BrokenFail(_Publisher):
        def fail(self, gen, lease, reason):
            raise checks.GitHubError("PATCH: HTTP 500", status=500)

    def crash(g, register=None):
        raise RuntimeError("boom")

    state, _calls, row = _run(store, gen, lease, crash, publisher=_BrokenFail(store))
    assert (state, row.state, row.conclusion) == ("failed", "failed", "failure")


def test_the_worker_loop_logs_iteration_failures_redacted(store, caplog):
    w = _worker(store, None)
    stop = threading.Event()

    def boom(lease):
        stop.set()
        raise RuntimeError(f"401 Bearer {SECRET}")

    w.run_once = boom
    with caplog.at_level("DEBUG"):
        w.run(stop)
    assert "worker iteration failed" in caplog.text
    assert SECRET not in caplog.text


def test_a_token_in_a_forge_error_reaches_no_stored_or_logged_text(store, gen, lease, tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(llm_backends, "create_llm_client", lambda cfg: _RecordingLLM())
    forge = worker.InstallationForge(lambda: SECRET)
    inner = _PinnedForge(TWO_CHUNK_DIFF, _roots(tmp_path, "- a rule\n"))

    def get_file_content(ref, path, *, sha):
        raise RuntimeError(f"401 for {forge._headers('github.com')['Authorization']}")

    inner.get_file_content = get_file_content
    publisher = _Publisher(store)

    def review(g, register=None):
        return worker.review_with_prxref(g, inner, rules_source="base", runs_dir=tmp_path / "runs", register=register)

    with caplog.at_level("DEBUG"):
        state, _calls, row = _run(store, gen, lease, review, publisher=publisher)
    assert state == "failed"
    assert "[redacted]" in (row.error or "")
    assert SECRET not in (row.error or "")
    assert all(SECRET not in reason for reason in publisher.reasons)
    assert SECRET not in caplog.text
    assert list((tmp_path / "runs").rglob("*.json")) == []


class _RaisingAuth:
    """An app auth whose every mint fails, as when GitHub's app endpoint refuses the JWT."""

    def token_for(self, repo):
        from prxref.gate.app_auth import GateAuthError

        raise GateAuthError("POST /app/installations/1/access_tokens returned 401 (request id R)")

    def invalidate(self, repo):
        pass


class _NoSession:
    def request(self, *args, **kwargs):
        raise AssertionError("no request may be sent without a token")


def test_an_auth_failure_reading_the_live_pr_fails_the_generation(store, gen, lease):
    client = checks.GitHubClient(_RaisingAuth(), _NoSession())
    publisher = checks.Publisher(client, store, app_id=99, bot_login="sdlc[bot]")
    state = worker.run_generation(
        gen,
        store=store,
        publisher=publisher,
        client=client,
        lease=lease,
        review=lambda g, register=None: pytest.fail("no"),
        deadline_s=5.0,
        effort="high",
    )
    row = store.get(gen.id)
    assert (state, row.state, row.conclusion) == ("failed", "failed", "failure")
    assert row.error.startswith("publish failed: UncertainReply") and "GateAuthError" in row.error


def test_a_malformed_live_pull_fails_the_generation(store, gen, lease):
    client = _Client()
    client.pull = {"state": "open"}
    state, calls, row = _run(store, gen, lease, lambda g, register=None: pytest.fail("no"), client=client)
    assert (state, row.state, row.conclusion) == ("failed", "failed", "failure")
    assert calls == ["fail:publish failed: KeyError"]


def test_a_failed_cancel_fails_the_generation_instead_of_escaping(store, gen, lease):
    class _BrokenCancel(_Publisher):
        def cancel(self, gen, lease, reason):
            self.calls.append("cancel-attempt")
            raise ValueError("bad json")

    publisher = _BrokenCancel(store)
    state, calls, row = _run(
        store,
        gen,
        lease,
        lambda g, register=None: pytest.fail("no"),
        client=_Client(state="closed"),
        publisher=publisher,
    )
    assert (state, row.state, row.conclusion) == ("failed", "failed", "failure")
    assert calls == ["cancel-attempt", "fail:publish failed: ValueError"]


def test_an_unreadable_pr_after_drift_still_ends_stale(store, gen, lease):
    class _OnceThenDown(_Client):
        def __init__(self):
            super().__init__(head="c" * 40)
            self.reads = 0

        def request(self, method, repo, path, **kwargs):
            self.reads += 1
            if self.reads > 1:
                raise RuntimeError("down")
            return super().request(method, repo, path, **kwargs)

    state, _calls, row = _run(store, gen, lease, lambda g, register=None: pytest.fail("no"), client=_OnceThenDown())
    assert (state, row.state, row.error) == ("stale", "stale", "head moved")


def test_an_error_verdict_stores_its_reason_for_status_and_health(store, gen, lease, tmp_path):
    summary = (
        "\U0001f916 **prxref review — Error**\n\n"
        "The review could not complete: litellm.AuthenticationError: BedrockException - token ghs_LEAKED123 expired\n\n"
        "No findings were produced."
    )
    outcome = worker.ReviewOutcome({"verdict": "Error"}, checks.Captured(summary=summary), tmp_path)
    state, _calls, row = _run(store, gen, lease, lambda g, register=None: outcome)
    assert (state, row.conclusion) == ("completed", "failure")
    failure = store.last_auth_failure()
    assert failure is not None and failure.startswith("The review could not complete: litellm.AuthenticationError")
    assert "ghs_LEAKED123" not in failure and len(failure) <= 500


def test_an_error_verdict_with_no_summary_still_stores_a_reason(store, gen, lease, tmp_path):
    state, _calls, row = _run(store, gen, lease, lambda g, register=None: _outcome({"verdict": "Error"}, tmp_path))
    assert (state, row.conclusion) == ("completed", "failure")
    assert row.error and row.error.startswith("review error")


def test_the_review_runs_on_a_daemon_thread_and_the_deadline_returns_promptly(store, gen, lease):
    seen = []
    release = threading.Event()

    def slow(g, register=None):
        seen.append(threading.current_thread().daemon)
        release.wait(2)

    started = time.monotonic()
    state, _calls, row = _run(store, gen, lease, slow, deadline_s=0.2)
    elapsed = time.monotonic() - started
    release.set()
    assert (state, row.state) == ("failed", "failed")
    assert seen == [True]
    assert elapsed < 0.5


def test_a_clean_stop_releases_the_worker_lease(store, tmp_path):
    w = _worker(store, tmp_path)
    stop = threading.Event()
    live_during = []

    def once(lease):
        live_during.append(store.lease_info()["live"])
        stop.set()
        return None

    w.run_once = once
    w.run(stop)
    assert live_during == [True]
    info = store.lease_info()
    assert info is None or info["live"] is False


def test_capturing_forge_refuses_unknown_write_methods_but_forwards_reads(gen):
    inner = FakeForge(diff=TWO_CHUNK_DIFF)
    inner.get_commits = lambda ref: ["c1"]
    for name in ("post_review", "prune_comments", "delete_comment", "create_check", "dismiss_review"):
        setattr(inner, name, lambda *a, **k: pytest.fail("a write reached the real forge"))
    cap = worker.CapturingForge(inner, gen)
    for name in ("post_review", "prune_comments", "delete_comment", "create_check", "dismiss_review"):
        with pytest.raises(AttributeError):
            getattr(cap, name)
        assert getattr(cap, name, None) is None
    assert cap.get_commits(REF) == ["c1"]


def test_register_secret_ignores_short_values():
    worker.register_secret("short")
    assert worker._redact("a short b") == "a short b"


def test_a_registered_secret_reaches_no_record_row_or_log(store, gen, lease, tmp_path, caplog, monkeypatch):
    value = "bedrock-key-Zq81LmP0xY"
    worker.register_secret(value)
    monkeypatch.setattr(
        worker.cli,
        "_run_review",
        lambda url, **kw: {
            "verdict": "Error",
            "chunks_failed": 1,
            "failed_chunks": [{"index": 1, "files": [], "error": f"auth with {value} refused"}],
        },
    )
    inner = _PinnedForge(TWO_CHUNK_DIFF, _roots(tmp_path, "- a rule\n"))
    publisher = _Publisher(store, complete_raises=checks.GitHubError(f"PATCH refused {value}", status=422))

    def review(g, register=None):
        return worker.review_with_prxref(g, inner, rules_source="base", runs_dir=tmp_path / "runs", register=register)

    with caplog.at_level("DEBUG"):
        state, _calls, row = _run(store, gen, lease, review, publisher=publisher)
    assert state == "failed"
    assert value not in (row.error or "") and "[redacted]" in (row.error or "")
    assert value not in caplog.text and "[redacted]" in caplog.text
    for name in ("result.json", "run_record.json"):
        text = (tmp_path / "runs" / str(gen.id) / name).read_text()
        assert value not in text, name
        assert "[redacted]" in text, name
