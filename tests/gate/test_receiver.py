"""Gate webhook intake: verification, allowlist, dedup, event mapping (#102)."""

import hashlib
import hmac
import json
import socket
import threading

import pytest
import requests

from prxref.gate import receiver
from prxref.gate.settings import GateSettings
from prxref.gate.store import Store

SECRET = "current-secret"
OLD = "previous-secret"
APP_ID = 12345


def _settings(tmp_path):
    return GateSettings(
        app_id=APP_ID, private_key_file=tmp_path / "k.pem", installation_ids=frozenset({777}),
        repos=frozenset({"acme/widget"}), secret_grace_minutes=30, state_file=tmp_path / "g.sqlite3",
        runs_dir=tmp_path / "runs", review_deadline=900, watchdog_seconds=60, reconcile_minutes=10,
    )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("PRXREF_GATE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("PRXREF_GATE_WEBHOOK_SECRET_PREVIOUS", OLD)


@pytest.fixture
def clock():
    return [10_000.0]


@pytest.fixture
def rx(tmp_path, env, clock):
    db = Store(tmp_path / "g.sqlite3", clock=lambda: clock[0])
    yield receiver.Receiver(db, _settings(tmp_path), started_at=10_000.0, clock=lambda: clock[0])
    db.close()


def _pr_payload(action="opened", head="h1", base="b1", number=7, repo="acme/widget", installation=777, **extra):
    payload = {
        "action": action, "number": number,
        "pull_request": {"number": number, "head": {"sha": head}, "base": {"sha": base, "ref": "main"}},
        "repository": {"full_name": repo}, "installation": {"id": installation},
    }
    payload.update(extra)
    return payload


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(rx, payload, *, event="pull_request", delivery="d1", secret=SECRET, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery}
    if secret is not None:
        headers["X-Hub-Signature-256"] = _sign(body, secret)
    return rx.handle("POST", "/webhook", headers, body)


def test_opened_enqueues_a_generation(rx):
    status, reply = _post(rx, _pr_payload())
    assert (status, reply["status"], reply["delivery"]) == (202, "accepted", "d1")
    gen = rx.store.next_queued()
    assert (gen.pr_number, gen.head_sha, gen.base_sha, gen.base_ref, gen.reason) == (7, "h1", "b1", "main", "opened")


@pytest.mark.parametrize("secret", ["wrong", None])
def test_bad_or_missing_signature_is_401_without_a_row(rx, secret):
    status, _ = _post(rx, _pr_payload(), secret=secret)
    assert status == 401
    assert rx.store.queue_depth() == 0
    assert rx.store.record_delivery("d1", "x", None, None, None) is True


def test_allow_unsigned_does_not_open_the_gate(rx, monkeypatch):
    monkeypatch.setenv("PRXREF_ALLOW_UNSIGNED", "1")
    assert _post(rx, _pr_payload(), secret=None)[0] == 401
    assert _post(rx, _pr_payload(), secret="wrong", delivery="d2")[0] == 401
    assert rx.store.queue_depth() == 0


def test_previous_secret_is_accepted_inside_the_grace_window(rx, clock):
    clock[0] = 10_000.0 + 29 * 60
    assert _post(rx, _pr_payload(), secret=OLD)[0] == 202


def test_previous_secret_is_refused_after_the_grace_window(rx, clock):
    clock[0] = 10_000.0 + 31 * 60
    assert _post(rx, _pr_payload(), secret=OLD)[0] == 401


@pytest.mark.parametrize("raw", [
    b"\xef\xbb\xbf" + json.dumps({"action": "opened"}).encode(),
    b"\xff\xfe\x00not-utf8",
    b"[1, 2, 3]",
    b'{"action": "opened"',
    b"",
])
def test_signed_garbage_bodies_are_400_without_a_row(rx, raw):
    status, _ = _post(rx, None, raw=raw)
    assert status == 400
    assert rx.store.queue_depth() == 0
    assert rx.store.record_delivery("d1", "x", None, None, None) is True


def test_unknown_installation_or_repo_is_403(rx):
    assert _post(rx, _pr_payload(installation=999))[0] == 403
    assert _post(rx, _pr_payload(repo="acme/other"), delivery="d2")[0] == 403
    assert rx.store.queue_depth() == 0


def test_repo_allowlist_is_case_insensitive(rx):
    assert _post(rx, _pr_payload(repo="ACME/Widget"))[0] == 202


def test_duplicate_delivery_is_202_and_enqueues_once(rx):
    assert _post(rx, _pr_payload())[0] == 202
    status, reply = _post(rx, _pr_payload())
    assert (status, reply["status"]) == (202, "duplicate")
    assert rx.store.queue_depth() == 1


def test_second_push_supersedes_the_first(rx):
    _post(rx, _pr_payload(head="h1"))
    _post(rx, _pr_payload(action="synchronize", head="h2"), delivery="d2")
    assert rx.store.queue_depth() == 1
    assert rx.store.next_queued().head_sha == "h2"


def test_out_of_order_delivery_still_queues_a_generation(rx):
    _post(rx, _pr_payload(action="synchronize", head="h2"), delivery="d2")
    _post(rx, _pr_payload(action="synchronize", head="h1"), delivery="d1")
    assert rx.store.next_queued().head_sha == "h1"


def test_retarget_queues_a_new_generation_for_the_same_head(rx):
    _post(rx, _pr_payload())
    payload = _pr_payload(action="edited", base="b2", changes={"base": {"ref": {"from": "dev"}}})
    assert _post(rx, payload, delivery="d2")[0] == 202
    assert rx.store.next_queued().base_sha == "b2"


def test_edit_of_the_title_only_is_ignored(rx):
    payload = _pr_payload(action="edited", changes={"title": {"from": "old"}})
    status, reply = _post(rx, payload)
    assert (status, reply["status"]) == (202, "ignored")
    assert rx.store.queue_depth() == 0


def test_closed_cancels_and_reopened_creates(rx):
    _post(rx, _pr_payload())
    _post(rx, _pr_payload(action="closed"), delivery="d2")
    assert rx.store.queue_depth() == 0
    _post(rx, _pr_payload(action="reopened"), delivery="d3")
    assert rx.store.next_queued().reason == "reopened"


def _check_run(head="h1", prs=None, app_id=APP_ID, name="sdlc-review"):
    return {
        "action": "rerequested", "repository": {"full_name": "acme/widget"}, "installation": {"id": 777},
        "check_run": {"name": name, "head_sha": head, "app": {"id": app_id}, "pull_requests": prs or []},
    }


def test_rerequest_creates_a_generation_with_the_same_head(rx):
    prs = [{"number": 7, "head": {"sha": "h1"}, "base": {"sha": "b1", "ref": "main"}}]
    assert _post(rx, _check_run(prs=prs), event="check_run")[0] == 202
    gen = rx.store.next_queued()
    assert (gen.head_sha, gen.reason) == ("h1", "rerequest")


def test_rerequest_on_fork_pr_uses_the_store_by_head_sha(rx):
    _post(rx, _pr_payload(head="forkhead", number=12))
    assert _post(rx, _check_run(head="forkhead"), event="check_run", delivery="d2")[0] == 202
    gen = rx.store.next_queued()
    assert (gen.pr_number, gen.head_sha, gen.reason) == (12, "forkhead", "rerequest")


def test_rerequest_for_unknown_head_is_ignored(rx):
    status, reply = _post(rx, _check_run(head="never-seen"), event="check_run")
    assert (status, reply["status"]) == (202, "ignored")


def test_rerequest_of_another_apps_check_is_ignored(rx):
    prs = [{"number": 7, "head": {"sha": "h1"}, "base": {"sha": "b1", "ref": "main"}}]
    assert _post(rx, _check_run(prs=prs, app_id=1), event="check_run")[1]["status"] == "ignored"


def test_check_suite_rerequest_creates_a_generation(rx):
    payload = {
        "action": "rerequested", "repository": {"full_name": "acme/widget"}, "installation": {"id": 777},
        "check_suite": {"head_sha": "h1", "app": {"id": APP_ID},
                        "pull_requests": [{"number": 7, "base": {"sha": "b1", "ref": "main"}}]},
    }
    assert _post(rx, payload, event="check_suite")[0] == 202
    assert rx.store.next_queued().reason == "rerequest"


@pytest.mark.parametrize("event", ["ping", "installation", "issues"])
def test_other_events_are_202_ignored(rx, event):
    status, reply = _post(rx, {"zen": "x", "repository": {"full_name": "acme/widget"},
                               "installation": {"id": 777}}, event=event)
    assert (status, reply["status"]) == (202, "ignored")


def test_missing_delivery_header_is_400(rx):
    body = json.dumps(_pr_payload()).encode()
    headers = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sign(body)}
    assert rx.handle("POST", "/webhook", headers, body)[0] == 400


def test_health_reports_queue_lease_publish_and_pause(rx):
    _post(rx, _pr_payload())
    rx.store.set_paused(True)
    status, body = rx.handle("GET", "/health", {}, b"")
    assert status == 200
    assert body == {"queue_depth": 1, "lease": None, "last_publish": None, "paused": True,
                    "last_auth_failure": None}


def test_real_socket_round_trip(rx):
    server = receiver.serve_http(rx, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        body = json.dumps(_pr_payload()).encode()
        resp = requests.post(
            f"http://127.0.0.1:{port}/webhook", data=body, timeout=5,
            headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "sock1",
                     "X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"},
        )
        assert (resp.status_code, resp.json()["delivery"]) == (202, "sock1")
        assert requests.get(f"http://127.0.0.1:{port}/health", timeout=5).json()["queue_depth"] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_oversize_body_is_413_without_a_row(rx):
    server = receiver.serve_http(rx, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        request = (
            "POST /webhook HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            "X-GitHub-Event: pull_request\r\nX-GitHub-Delivery: big1\r\n"
            f"Content-Length: {receiver.MAX_BODY_BYTES + 1}\r\nConnection: close\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
            conn.sendall(request.encode())
            reply = b""
            while chunk := conn.recv(4096):
                reply += chunk
        assert reply.startswith(b"HTTP/1.0 413") or reply.startswith(b"HTTP/1.1 413")
        assert rx.store.queue_depth() == 0
        assert rx.store.record_delivery("big1", "x", None, None, None) is True
    finally:
        server.shutdown()
        server.server_close()
