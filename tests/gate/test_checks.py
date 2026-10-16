"""The sdlc-review publisher (#102)."""

import re

import pytest
import requests

from prxref.forges.base import SUMMARY_MARKER, InlineComment
from prxref.gate import checks
from prxref.gate.store import Store
from prxref.triage import Finding

REPO, HEAD, BASE = "acme/widget", "h" * 40, "b" * 40


class _Resp:
    def __init__(self, status, payload=None, request_id="RID-1"):
        self.status_code = status
        self._payload = payload
        self.headers = {"X-GitHub-Request-Id": request_id}
        self.content = b"x" if payload is not None else b""

    def json(self):
        return self._payload


class _GitHub:
    """Routes ``(method, path regex)`` to scripted replies; records every call."""

    def __init__(self, routes):
        self.routes = [(m, re.compile(p), list(r)) for m, p, r in routes]
        self.calls = []

    def request(self, method, url, headers=None, timeout=None, json=None, params=None):
        self.calls.append((method, url, headers, json, params))
        for m, pattern, replies in self.routes:
            if m == method and pattern.search(url):
                reply = replies.pop(0) if len(replies) > 1 else replies[0]
                if isinstance(reply, Exception):
                    raise reply
                return reply
        raise AssertionError(f"unrouted {method} {url}")

    def sent(self, method, pattern):
        return [c for c in self.calls if c[0] == method and re.search(pattern, c[1])]


class _Auth:
    def __init__(self):
        self.minted = 0

    def token_for(self, repo):
        self.minted += 1
        return f"ghs_TOKEN{self.minted}"

    def invalidate(self, repo):
        self.invalidated = repo


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "gate.db")
    yield s
    s.close()


@pytest.fixture
def lease(store):
    return checks.Lease("host:1", store.acquire_lease("host:1", 600), 600)


@pytest.fixture
def gen(store, lease):
    g = store.enqueue(REPO, 7, HEAD, BASE, "main", "opened")
    return store.start(g.id, lease.token)


def _publisher(gh, store):
    client = checks.GitHubClient(_Auth(), gh)
    return checks.Publisher(client, store, app_id=99, bot_login="sdlc[bot]")


def _finding(path="a.py", line=0, severity="warning"):
    return Finding(file=path, line=line, severity=severity, confidence=0.9, title="t", body="b")


@pytest.mark.parametrize(
    "result, conclusion",
    [
        ({"verdict": "Approved", "chunks_failed": 0}, "success"),
        ({"verdict": "Request-Changes"}, "failure"),
        ({"verdict": "Incomplete"}, "failure"),
        ({"verdict": "Approved", "chunks_failed": 1}, "failure"),
        ({"verdict": "Approved", "overflow_files": 2}, "failure"),
        ({"verdict": "Approved", "degraded": {"post": "failed"}}, "failure"),
        ({"verdict": "Error"}, "failure"),
        ("not a dict", "failure"),
        ({"verdict": "Maybe"}, "failure"),
    ],
)
def test_conclusion_follows_the_verdict_table(result, conclusion):
    assert checks.conclusion_for(result)[0] == conclusion


def test_a_401_refreshes_the_token_once_and_retries(store):
    gh = _GitHub([("GET", r"/pulls/7$", [_Resp(401, {"message": "Bad credentials"}), _Resp(200, {"n": 1})])])
    client = checks.GitHubClient(_Auth(), gh)
    assert client.request("GET", REPO, "/pulls/7") == {"n": 1}
    tokens = [c[2]["Authorization"] for c in gh.calls]
    assert tokens == ["token ghs_TOKEN1", "token ghs_TOKEN2"]


def test_a_second_401_is_an_error_without_the_token():
    gh = _GitHub([("GET", r"/pulls/7$", [_Resp(401, {"message": "Bad credentials"})])])
    with pytest.raises(checks.GitHubError) as err:
        checks.GitHubClient(_Auth(), gh).request("GET", REPO, "/pulls/7")
    assert err.value.status == 401 and err.value.request_id == "RID-1"
    assert "ghs_" not in str(err.value)


def test_begin_creates_an_in_progress_check_with_external_id_and_details_url(store, gen, lease):
    gh = _GitHub([("POST", r"/check-runs$", [_Resp(201, {"id": 555})])])
    assert _publisher(gh, store).begin(gen, lease) == 555
    body = gh.sent("POST", r"/check-runs$")[0][3]
    assert body["name"] == "sdlc-review" and body["head_sha"] == HEAD
    assert body["external_id"] == gen.external_id and body["status"] == "in_progress"
    assert body["details_url"] == "https://github.com/acme/widget/pull/7/checks"
    assert store.get(gen.id).check_run_id == 555


def test_begin_reuses_a_stored_check_run_id(store, gen, lease):
    store.set_check_run(gen.id, lease.token, 555)
    gh = _GitHub([("PATCH", r"/check-runs/555$", [_Resp(200, {"id": 555})])])
    assert _publisher(gh, store).begin(store.get(gen.id), lease) == 555
    assert not gh.sent("POST", r"/check-runs")


def test_begin_after_a_timeout_finds_the_check_instead_of_creating_a_second(store, gen, lease):
    found = {"check_runs": [{"id": 556, "external_id": gen.external_id}]}
    gh = _GitHub(
        [
            ("POST", r"/check-runs$", [requests.Timeout("slow")]),
            ("GET", rf"/commits/{HEAD}/check-runs$", [_Resp(200, found)]),
        ]
    )
    assert _publisher(gh, store).begin(gen, lease) == 556
    assert len(gh.sent("POST", r"/check-runs$")) == 1
    assert gh.sent("GET", r"/check-runs$")[0][4] == {"check_name": "sdlc-review", "app_id": 99, "filter": "all"}


def _complete(gh, store, gen, lease, result, captured=None):
    captured = captured if captured is not None else checks.Captured()
    store.set_check_run(gen.id, lease.token, 555)
    return _publisher(gh, store).complete(
        store.get(gen.id),
        lease,
        result,
        captured,
        effort="high",
    )


def test_annotations_are_batched_by_fifty(store, gen, lease):
    findings = [_finding(f"f{i}.py") for i in range(120)]
    gh = _GitHub(
        [
            ("POST", r"/pulls/7/reviews$", [_Resp(200, {"id": 9})]),
            ("PATCH", r"/check-runs/555$", [_Resp(200, {"id": 555})]),
        ]
    )
    _complete(gh, store, gen, lease, {"verdict": "Approved", "findings_active": findings})
    patches = [c[3] for c in gh.sent("PATCH", r"/check-runs/555$")]
    assert [len(p["output"]["annotations"]) for p in patches] == [50, 50, 20]
    assert [p.get("conclusion") for p in patches] == [None, None, "success"]
    assert patches[-1]["status"] == "completed"


def _review_gh(*replies):
    return _GitHub(
        [
            ("POST", r"/pulls/7/reviews$", list(replies) or [_Resp(200, {"id": 9})]),
            ("PATCH", r"/check-runs/555$", [_Resp(200, {})]),
        ]
    )


def test_an_approved_result_posts_an_approve_review_with_the_generation_marker(store, gen, lease):
    gh = _review_gh()
    comment = InlineComment(path="a.py", line=3, body="**warning** x")
    _complete(gh, store, gen, lease, {"verdict": "Approved"}, checks.Captured(comments=(comment,)))
    review = gh.sent("POST", r"/reviews$")[0][3]
    assert review["event"] == "APPROVE" and review["commit_id"] == HEAD
    assert (
        review["body"]
        == f"<!-- sdlc-gate:generation={gen.external_id} -->\nsdlc review: approved - 1 inline finding(s)."
    )
    assert review["comments"] == [{"path": "a.py", "line": 3, "side": "RIGHT", "body": "**warning** x"}]
    assert store.publication(gen.id) == ("APPROVE", 9)


def test_a_non_success_result_posts_a_request_changes_review(store, gen, lease):
    gh = _review_gh()
    comment = InlineComment(path="a.py", line=3, body="x")
    _complete(gh, store, gen, lease, {"verdict": "Request-Changes"}, checks.Captured(comments=(comment,)))
    review = gh.sent("POST", r"/reviews$")[0][3]
    assert review["event"] == "REQUEST_CHANGES"
    assert review["body"].endswith("sdlc review: changes requested - 1 inline finding(s).\nReason: error-severity findings.")
    assert gh.sent("PATCH", r"/check-runs/555$")[-1][3]["conclusion"] == "failure"


@pytest.mark.parametrize(
    ("result", "event"),
    [
        ({"verdict": "Approved"}, "APPROVE"),
        ({"verdict": "Request-Changes"}, "REQUEST_CHANGES"),
        ({"verdict": "Incomplete"}, "REQUEST_CHANGES"),
        ({"verdict": "Approved", "degraded": True}, "REQUEST_CHANGES"),
    ],
)
def test_a_review_is_posted_even_with_no_inline_comments(store, gen, lease, result, event):
    gh = _review_gh()
    _complete(gh, store, gen, lease, result)
    reviews = gh.sent("POST", r"/reviews$")
    assert len(reviews) == 1
    assert reviews[0][3]["event"] == event and reviews[0][3]["comments"] == []
    assert store.publication(gen.id) == (event, 9)


@pytest.mark.parametrize("result", [{"verdict": "Error"}, "garbage", {"verdict": "Maybe"}])
def test_an_error_or_malformed_result_posts_request_changes(store, gen, lease, result):
    gh = _review_gh()
    _complete(gh, store, gen, lease, result)
    reviews = gh.sent("POST", r"/reviews$")
    assert len(reviews) == 1 and reviews[0][3]["event"] == "REQUEST_CHANGES"
    assert gh.sent("PATCH", r"/check-runs/555$")[-1][3]["conclusion"] == "failure"
    assert store.publication(gen.id) == ("REQUEST_CHANGES", 9)


def test_a_published_generation_with_the_same_event_posts_no_second_review(store, gen, lease):
    store.set_check_run(gen.id, lease.token, 555)
    store.record_publication(gen.id, lease.token, "APPROVE", 4)
    gh = _review_gh()
    _complete(gh, store, gen, lease, {"verdict": "Approved"})
    assert not gh.sent("POST", r"/reviews$")
    assert store.publication(gen.id) == ("APPROVE", 4)


def test_a_rerun_with_a_different_event_posts_and_records_the_new_review(store, gen, lease):
    store.set_check_run(gen.id, lease.token, 555)
    store.record_publication(gen.id, lease.token, "REQUEST_CHANGES", 4)
    gh = _review_gh()
    _complete(gh, store, gen, lease, {"verdict": "Approved"})
    reviews = gh.sent("POST", r"/reviews$")
    assert len(reviews) == 1 and reviews[0][3]["event"] == "APPROVE"
    assert store.publication(gen.id) == ("APPROVE", 9)


def test_an_approved_generation_that_now_fails_posts_request_changes(store, gen, lease):
    store.set_check_run(gen.id, lease.token, 555)
    store.record_publication(gen.id, lease.token, "APPROVE", 4)
    gh = _review_gh()
    _complete(gh, store, gen, lease, {"verdict": "Request-Changes"})
    reviews = gh.sent("POST", r"/reviews$")
    assert len(reviews) == 1 and reviews[0][3]["event"] == "REQUEST_CHANGES"
    assert store.publication(gen.id) == ("REQUEST_CHANGES", 9)


def test_a_legacy_review_kind_means_event_unknown_and_posts(store, gen, lease):
    store.set_check_run(gen.id, lease.token, 555)
    store.record_publication(gen.id, lease.token, "review", 4)
    gh = _review_gh()
    _complete(gh, store, gen, lease, {"verdict": "Approved"})
    assert len(gh.sent("POST", r"/reviews$")) == 1
    assert store.publication(gen.id) == ("APPROVE", 9)


def test_an_uncertain_reply_on_the_comment_less_repost_recovers_by_marker(store, gen, lease):
    marker = f"<!-- sdlc-gate:generation={gen.external_id} -->"
    existing = [{"id": 13, "user": {"login": "sdlc[bot]"}, "body": f"{marker}\nsdlc review"}]
    gh = _GitHub(
        [
            (
                "POST",
                r"/pulls/7/reviews$",
                [_Resp(422, {"message": "line must be part of the diff"}), _Resp(502, {"message": "Bad Gateway"})],
            ),
            ("GET", r"/pulls/7/reviews$", [_Resp(200, existing)]),
            ("PATCH", r"/check-runs/555$", [_Resp(200, {})]),
        ]
    )
    comment = InlineComment(path="a.py", line=3, body="x")
    _complete(gh, store, gen, lease, {"verdict": "Approved"}, checks.Captured(comments=(comment,)))
    posts = gh.sent("POST", r"/reviews$")
    assert len(posts) == 2 and posts[1][3]["comments"] == []
    assert len(gh.sent("GET", r"/pulls/7/reviews$")) == 1
    assert store.publication(gen.id) == ("APPROVE", 13)


def test_fail_and_cancel_post_no_review(store, gen, lease):
    store.set_check_run(gen.id, lease.token, 555)
    gh = _GitHub([("PATCH", r"/check-runs/555$", [_Resp(200, {})])])
    publisher = _publisher(gh, store)
    publisher.fail(store.get(gen.id), lease, "boom")
    publisher.cancel(store.get(gen.id), lease, "closed")
    assert gh.sent("PATCH", r"/check-runs/555$") and not gh.sent("POST", r"/reviews")


def test_review_after_a_5xx_is_found_by_marker_not_reposted(store, gen, lease):
    marker = f"<!-- sdlc-gate:generation={gen.external_id} -->"
    existing = [{"id": 10, "user": {"login": "sdlc[bot]"}, "body": f"{marker}\nsdlc review"}]
    gh = _GitHub(
        [
            ("POST", r"/pulls/7/reviews$", [_Resp(502, {"message": "Bad Gateway"})]),
            ("GET", r"/pulls/7/reviews$", [_Resp(200, existing)]),
            ("PATCH", r"/check-runs/555$", [_Resp(200, {})]),
        ]
    )
    comment = InlineComment(path="a.py", line=3, body="x")
    _complete(gh, store, gen, lease, {"verdict": "Approved"}, checks.Captured(comments=(comment,)))
    assert len(gh.sent("POST", r"/reviews$")) == 1
    assert store.publication(gen.id) == ("APPROVE", 10)


@pytest.mark.parametrize(("verdict", "event"), [("Approved", "APPROVE"), ("Request-Changes", "REQUEST_CHANGES")])
def test_a_422_reposts_the_same_event_without_comments_and_demotes_them(store, gen, lease, verdict, event):
    gh = _review_gh(_Resp(422, {"message": "line must be part of the diff"}), _Resp(200, {"id": 11}))
    comment = InlineComment(path="a.py", line=3, body="x")
    _complete(gh, store, gen, lease, {"verdict": verdict}, checks.Captured(comments=(comment,)))
    first, second = (c[3] for c in gh.sent("POST", r"/reviews$"))
    assert first["comments"] and second["comments"] == []
    assert first["event"] == second["event"] == event and first["body"].split("\n")[0] == second["body"].split("\n")[0]
    annotation = gh.sent("PATCH", r"/check-runs/555$")[-1][3]["output"]["annotations"][0]
    assert (annotation["path"], annotation["start_line"], annotation["message"]) == ("a.py", 3, "x")
    assert store.publication(gen.id) == (event, 11)


def test_a_422_then_a_422_raises(store, gen, lease):
    gh = _review_gh(_Resp(422, {"message": "no"}), _Resp(422, {"message": "still no"}))
    comment = InlineComment(path="a.py", line=3, body="x")
    with pytest.raises(checks.GitHubError):
        _complete(gh, store, gen, lease, {"verdict": "Approved"}, checks.Captured(comments=(comment,)))
    assert len(gh.sent("POST", r"/reviews$")) == 2
    assert store.publication(gen.id) is None


def test_a_422_on_a_comment_less_review_raises(store, gen, lease):
    gh = _review_gh(_Resp(422, {"message": "no"}))
    with pytest.raises(checks.GitHubError):
        _complete(gh, store, gen, lease, {"verdict": "Approved"})
    assert len(gh.sent("POST", r"/reviews$")) == 1


def test_summary_comment_is_updated_only_when_authored_by_the_bot(store, gen, lease):
    feed = [
        {"id": 1, "user": {"login": "prxref-advisor"}, "body": f"{SUMMARY_MARKER}\nadvisory"},
        {"id": 2, "user": {"login": "sdlc[bot]"}, "body": f"{SUMMARY_MARKER}\nold"},
    ]
    gh = _GitHub(
        [
            ("POST", r"/pulls/7/reviews$", [_Resp(200, {"id": 9})]),
            ("GET", r"/issues/7/comments$", [_Resp(200, feed)]),
            ("PATCH", r"/issues/comments/2$", [_Resp(200, {})]),
            ("PATCH", r"/check-runs/555$", [_Resp(200, {})]),
        ]
    )
    _complete(gh, store, gen, lease, {"verdict": "Approved"}, checks.Captured(summary="new summary"))
    assert SUMMARY_MARKER in gh.sent("PATCH", r"/issues/comments/2$")[0][3]["body"]
    assert not gh.sent("PATCH", r"/issues/comments/1$")
    assert not gh.sent("POST", r"/issues/7/comments$")


def test_publish_after_lease_loss_writes_nothing(store, gen, lease):
    store.acquire_lease("other:2", 600, holder_alive=lambda h: False)
    gh = _GitHub([])
    with pytest.raises(checks.LeaseLost):
        _publisher(gh, store).begin(gen, lease)
    assert gh.calls == []


def test_a_guard_refusal_stops_the_publish_before_any_write(store, gen, lease):
    def guard(g):
        raise checks.Stale("head moved")

    gh = _GitHub([])
    publisher = checks.Publisher(checks.GitHubClient(_Auth(), gh), store, app_id=99, bot_login="sdlc[bot]", guard=guard)
    with pytest.raises(checks.Stale):
        publisher.begin(gen, lease)
    assert gh.calls == []


def test_summary_names_model_effort_tokens_and_shas(gen):
    result = {
        "verdict": "Approved",
        "input_tokens": 1200,
        "output_tokens": 300,
        "chunk_count": 3,
        "chunks_reviewed": 3,
        "chunks_failed": 0,
        "sampling": {"models": ["bedrock/us.anthropic.claude-opus"]},
    }
    text = checks.summary_text(gen, result, "success", "approved", effort="high")
    for needle in ("bedrock/us.anthropic.claude-opus", "high", "1200", "300", "Approved", "3/3", HEAD, BASE, "`runs/"):
        assert needle in text


def test_check_output_leads_with_the_executive_summary(store, gen, lease):
    gh = _GitHub(
        [
            ("POST", r"/pulls/7/reviews$", [_Resp(200, {"id": 9})]),
            ("GET", r"/issues/7/comments$", [_Resp(200, [])]),
            ("POST", r"/issues/7/comments$", [_Resp(201, {})]),
            ("PATCH", r"/check-runs/555$", [_Resp(200, {})]),
        ]
    )
    _complete(gh, store, gen, lease, {"verdict": "Approved"}, checks.Captured(summary="EXEC SUMMARY"))
    summary = gh.sent("PATCH", r"/check-runs/555$")[-1][3]["output"]["summary"]
    assert summary.startswith("EXEC SUMMARY") and HEAD in summary


def test_check_output_falls_back_to_the_generated_summary(store, gen, lease):
    gh = _GitHub(
        [("POST", r"/pulls/7/reviews$", [_Resp(200, {"id": 9})]), ("PATCH", r"/check-runs/555$", [_Resp(200, {})])]
    )
    _complete(gh, store, gen, lease, {"verdict": "Approved"})
    summary = gh.sent("PATCH", r"/check-runs/555$")[-1][3]["output"]["summary"]
    assert "**success**" in summary and BASE in summary


def test_fail_with_no_check_creates_one_without_running_the_guard(store, gen, lease):
    seen = []
    gh = _GitHub(
        [
            ("POST", r"/check-runs$", [_Resp(201, {"id": 777})]),
            ("PATCH", r"/check-runs/777$", [_Resp(200, {})]),
        ]
    )
    publisher = checks.Publisher(
        checks.GitHubClient(_Auth(), gh), store, app_id=99, bot_login="sdlc[bot]", guard=lambda g: seen.append(g)
    )
    publisher.fail(gen, lease, "deadline")
    assert seen == []
    last = gh.sent("PATCH", r"/check-runs/777$")[-1][3]
    assert last["conclusion"] == "failure" and last["status"] == "completed"


def test_guard_runs_before_each_complete_write(store, gen, lease):
    def guard(g):
        raise checks.Stale("head moved")

    store.set_check_run(gen.id, lease.token, 555)
    gh = _GitHub([])
    publisher = checks.Publisher(checks.GitHubClient(_Auth(), gh), store, app_id=99, bot_login="sdlc[bot]", guard=guard)
    with pytest.raises(checks.Stale):
        publisher.complete(store.get(gen.id), lease, {"verdict": "Approved"}, checks.Captured(), effort="high")
    assert gh.calls == []


def test_cancel_without_a_check_posts_nothing(store, gen, lease):
    gh = _GitHub([])
    _publisher(gh, store).cancel(gen, lease, "closed")
    assert gh.calls == []


class _Ordered(_GitHub):
    """A fake GitHub that logs writes into a shared event list."""

    def __init__(self, routes, events):
        super().__init__(routes)
        self.events = events

    def request(self, method, url, **kw):
        if method != "GET":
            self.events.append("W")
        return super().request(method, url, **kw)


class _CountingStore:
    def __init__(self, store, events):
        self._store, self._events = store, events

    def renew_lease(self, *args):
        self._events.append("R")
        return self._store.renew_lease(*args)

    def __getattr__(self, name):
        return getattr(self._store, name)


def test_the_lease_is_renewed_and_guarded_before_every_write(store, gen, lease):
    events = []
    gh = _Ordered(
        [
            ("POST", r"/check-runs$", [_Resp(201, {"id": 555})]),
            ("POST", r"/pulls/7/reviews$", [_Resp(200, {"id": 9})]),
            ("GET", r"/issues/7/comments$", [_Resp(200, [])]),
            ("POST", r"/issues/7/comments$", [_Resp(201, {})]),
            ("PATCH", r"/check-runs/555$", [_Resp(200, {})]),
        ],
        events,
    )
    publisher = checks.Publisher(
        checks.GitHubClient(_Auth(), gh),
        _CountingStore(store, events),
        app_id=99,
        bot_login="sdlc[bot]",
        guard=lambda g: events.append("G"),
    )
    findings = [_finding(f"f{i}.py") for i in range(120)]
    publisher.begin(gen, lease)
    publisher.complete(
        gen,
        lease,
        {"verdict": "Approved", "findings_active": findings},
        checks.Captured(summary="s", comments=(InlineComment(path="a.py", line=3, body="x"),)),
        effort="high",
    )
    assert events.count("W") == 6
    assert events.count("R") == 6
    assert events.count("G") == 6
    for i, e in enumerate(events):
        if e == "W":
            assert events[i - 2 : i] == ["R", "G"], events


def test_details_survive_a_summary_over_the_limit(store, gen, lease):
    gh = _GitHub(
        [
            ("POST", r"/pulls/7/reviews$", [_Resp(200, {"id": 9})]),
            ("GET", r"/issues/7/comments$", [_Resp(200, [])]),
            ("POST", r"/issues/7/comments$", [_Resp(201, {})]),
            ("PATCH", r"/check-runs/555$", [_Resp(200, {})]),
        ]
    )
    _complete(gh, store, gen, lease, {"verdict": "Approved"}, checks.Captured(summary="x" * 70000))
    summary = gh.sent("PATCH", r"/check-runs/555$")[-1][3]["output"]["summary"]
    assert len(summary) <= checks.SUMMARY_LIMIT and HEAD in summary and BASE in summary


def test_a_422_on_the_retry_review_post_demotes_the_comments(store, gen, lease):
    gh = _GitHub(
        [
            (
                "POST",
                r"/pulls/7/reviews$",
                [
                    _Resp(502, {"message": "Bad Gateway"}),
                    _Resp(422, {"message": "line must be part of the diff"}),
                    _Resp(200, {"id": 12}),
                ],
            ),
            ("GET", r"/pulls/7/reviews$", [_Resp(200, [])]),
            ("PATCH", r"/check-runs/555$", [_Resp(200, {})]),
        ]
    )
    comment = InlineComment(path="a.py", line=3, body="x")
    _complete(gh, store, gen, lease, {"verdict": "Approved"}, checks.Captured(comments=(comment,)))
    posts = gh.sent("POST", r"/reviews$")
    assert len(posts) == 3 and posts[2][3]["comments"] == []
    annotation = gh.sent("PATCH", r"/check-runs/555$")[-1][3]["output"]["annotations"][0]
    assert annotation["path"] == "a.py"
    assert store.publication(gen.id) == ("APPROVE", 12)


def test_the_retry_posts_are_fenced_again(store, gen, lease):
    renewals = []

    class _Once(_CountingStore):
        def renew_lease(self, *args):
            renewals.append(1)
            return len(renewals) < 2  # the lease is lost during the first POST's hang

    gh = _GitHub(
        [
            ("POST", r"/pulls/7/reviews$", [requests.Timeout("slow")]),
            ("GET", r"/pulls/7/reviews$", [_Resp(200, [])]),
        ]
    )
    store.set_check_run(gen.id, lease.token, 555)
    publisher = checks.Publisher(checks.GitHubClient(_Auth(), gh), _Once(store, []), app_id=99, bot_login="b")
    comment = InlineComment(path="a.py", line=3, body="x")
    with pytest.raises(checks.LeaseLost):
        publisher.complete(
            store.get(gen.id),
            lease,
            {"verdict": "Approved"},
            checks.Captured(comments=(comment,)),
            effort="",
        )
    assert len(gh.sent("POST", r"/reviews$")) == 1


def test_the_check_create_retry_is_fenced_again(store, gen, lease):
    renewals = []

    class _Once(_CountingStore):
        def renew_lease(self, *args):
            renewals.append(1)
            return len(renewals) < 2

    gh = _GitHub(
        [
            ("POST", r"/check-runs$", [requests.Timeout("slow")]),
            ("GET", rf"/commits/{HEAD}/check-runs$", [_Resp(200, {"check_runs": []})]),
        ]
    )
    publisher = checks.Publisher(checks.GitHubClient(_Auth(), gh), _Once(store, []), app_id=99, bot_login="b")
    with pytest.raises(checks.LeaseLost):
        publisher.begin(gen, lease)
    assert len(gh.sent("POST", r"/check-runs$")) == 1


def test_complete_with_no_check_creates_one_first_and_runs_the_guard(store, gen, lease):
    seen = []
    gh = _GitHub(
        [
            ("POST", r"/check-runs$", [_Resp(201, {"id": 888})]),
            ("POST", r"/pulls/7/reviews$", [_Resp(200, {"id": 9})]),
            ("PATCH", r"/check-runs/888$", [_Resp(200, {})]),
        ]
    )
    publisher = checks.Publisher(
        checks.GitHubClient(_Auth(), gh), store, app_id=99, bot_login="b", guard=lambda g: seen.append(1)
    )
    publisher.complete(gen, lease, {"verdict": "Approved"}, checks.Captured(), effort="")
    assert not gh.sent("PATCH", r"/check-runs/None")
    assert gh.sent("PATCH", r"/check-runs/888$")[-1][3]["conclusion"] == "success"
    assert len(seen) >= 2


def test_the_summary_names_the_run_record_relative_to_the_runs_dir(gen):
    text = checks.summary_text(gen, {"verdict": "Approved"}, "success", "approved", effort="high")
    assert f"run record: `runs/{gen.external_id}`" in text
    assert "/Users/" not in text and "/home/" not in text


class _FailingAuth(_Auth):
    def token_for(self, repo):
        from prxref.gate.app_auth import GateAuthError

        raise GateAuthError("POST /app/installations/1/access_tokens returned 401 (request id R)")


@pytest.mark.parametrize("auth, reply", [(_FailingAuth(), None), (_Auth(), requests.exceptions.InvalidURL("bad"))])
def test_an_auth_or_transport_failure_is_an_uncertain_reply(auth, reply):
    gh = _GitHub([("GET", r"/pulls/7$", [reply or _Resp(200, {})])])
    client = checks.GitHubClient(auth, gh)
    with pytest.raises(checks.UncertainReply):
        client.request("GET", REPO, "/pulls/7")


def test_every_minted_token_is_registered_for_redaction():
    from prxref.gate import worker

    gh = _GitHub([("GET", r"/pulls/7$", [_Resp(200, {})])])

    class _LongAuth(_Auth):
        def token_for(self, repo):
            return "opaque-installation-token-QWERTY"

    checks.GitHubClient(_LongAuth(), gh).request("GET", REPO, "/pulls/7")
    assert worker._redact("x opaque-installation-token-QWERTY y") == "x [redacted] y"
