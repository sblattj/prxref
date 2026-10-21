"""The gate's AWS Lambda handlers over moto: receiver, worker, watchdog (#102)."""

import base64
import hashlib
import hmac
import json
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from prxref.gate import aws, worker
from prxref.gate.dynamo_store import DynamoStore, create_table
from prxref.gate.settings import WEBHOOK_SECRET_ENV, WEBHOOK_SECRET_PREVIOUS_ENV
from prxref.gate.watchdog import Watchdog
from tests.gate.test_worker import BASE, HEAD, REPO, _Client, _outcome, _Publisher

WEBHOOK_SECRET = "test-webhook-secret-" + "x" * 12
APP_ID = 12345


@pytest.fixture
def cloud(monkeypatch):
    """moto DynamoDB + SQS FIFO + Secrets Manager, and the Lambda environment pointing at them."""
    for name, value in (
        ("AWS_ACCESS_KEY_ID", "testing"),
        ("AWS_SECRET_ACCESS_KEY", "testing"),
        ("AWS_SESSION_TOKEN", "testing"),
        ("AWS_DEFAULT_REGION", "us-east-1"),
        ("PRXREF_GATE_APP_ID", str(APP_ID)),
        ("PRXREF_GATE_INSTALLATION_IDS", "777"),
        ("PRXREF_GATE_REPOS", REPO),
    ):
        monkeypatch.setenv(name, value)
    for name in (WEBHOOK_SECRET_ENV, WEBHOOK_SECRET_PREVIOUS_ENV, "PRXREF_RULES_SOURCE", "AWS_BEARER_TOKEN_BEDROCK"):
        monkeypatch.delenv(name, raising=False)
    aws._reset()
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        create_table(ddb, "gate")
        sqs = boto3.client("sqs", region_name="us-east-1")
        queue_url = sqs.create_queue(
            QueueName="gate.fifo", Attributes={"FifoQueue": "true", "VisibilityTimeout": "5400"}
        )["QueueUrl"]
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        hook_arn = sm.create_secret(Name="gate-webhook", SecretString=WEBHOOK_SECRET)["ARN"]
        monkeypatch.setenv(aws.TABLE_ENV, "gate")
        monkeypatch.setenv(aws.QUEUE_ENV, queue_url)
        monkeypatch.setenv(aws.WEBHOOK_SECRET_ARN_ENV, hook_arn)
        yield SimpleNamespace(ddb=ddb, sqs=sqs, sm=sm, queue_url=queue_url, store=DynamoStore("gate", ddb))
    aws._reset()


def _messages(cloud):
    got = cloud.sqs.receive_message(
        QueueUrl=cloud.queue_url, MaxNumberOfMessages=10, AttributeNames=["All"], VisibilityTimeout=0
    )
    return [
        (json.loads(m["Body"]), m["Attributes"]["MessageGroupId"], m["Attributes"]["MessageDeduplicationId"])
        for m in got.get("Messages", [])
    ]


def _event(payload, *, secret=WEBHOOK_SECRET, b64=False, delivery="d1"):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {
        "version": "2.0",
        "rawPath": "/webhook",
        "requestContext": {"http": {"method": "POST", "path": "/webhook"}},
        "headers": {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": sig},
        "body": base64.b64encode(body).decode() if b64 else body.decode(),
        "isBase64Encoded": b64,
    }


def _pr(action="opened"):
    return {
        "action": action,
        "number": 7,
        "pull_request": {"number": 7, "head": {"sha": HEAD}, "base": {"sha": BASE, "ref": "main"}},
        "repository": {"full_name": REPO},
        "installation": {"id": 777},
    }


def test_a_bad_signature_is_rejected_and_nothing_is_sent(cloud):
    reply = aws.receiver_handler(_event(_pr(), secret="wrong-secret-value"), None)
    assert reply["statusCode"] == 401
    assert _messages(cloud) == []
    assert cloud.store.latest_generation(REPO, 7) is None


def test_a_valid_pr_event_enqueues_and_sends_one_fifo_message(cloud):
    reply = aws.receiver_handler(_event(_pr()), None)
    assert (reply["statusCode"], json.loads(reply["body"])["status"]) == (202, "accepted")
    gen = cloud.store.latest_generation(REPO, 7)
    assert gen.state == "queued"
    assert _messages(cloud) == [({"generation_id": gen.id}, f"{REPO}#7", str(gen.id))]
    assert worker._redact(f"x {WEBHOOK_SECRET} y") == "x [redacted] y"


def test_a_base64_body_is_decoded_before_the_hmac(cloud):
    reply = aws.receiver_handler(_event(_pr(), b64=True), None)
    assert reply["statusCode"] == 202
    assert len(_messages(cloud)) == 1


def test_only_post_webhook_is_routed(cloud):
    event = _event(_pr())
    event["requestContext"]["http"]["method"] = "GET"
    assert aws.receiver_handler(event, None)["statusCode"] == 404


class _Context:
    aws_request_id = "req-1"

    def __init__(self, remaining_s=900.0):
        self.remaining_s = remaining_s

    def get_remaining_time_in_millis(self):
        return int(self.remaining_s * 1000)


def _deps(cloud, tmp_path, sent):
    return aws.WorkerDeps(
        store=cloud.store,
        publisher=_Publisher(cloud.store),
        client=_Client(),
        review=lambda g, register=None: _outcome({"verdict": "Approved"}, tmp_path),
        settings=SimpleNamespace(review_deadline=900),
        effort="medium",
        send=sent.append,
    )


def _sqs_event(gen_id):
    return {"Records": [{"body": json.dumps({"generation_id": gen_id})}]}


def test_the_worker_runs_a_queued_generation_and_releases_the_lease(cloud, tmp_path):
    gen = cloud.store.enqueue(REPO, 7, HEAD, BASE, "main", "opened")
    deps = _deps(cloud, tmp_path, [])
    aws._cache["worker"] = deps
    assert aws.worker_handler(_sqs_event(gen.id), _Context()) == {"results": ["completed"]}
    row = cloud.store.get(gen.id)
    assert (row.state, row.conclusion, deps.publisher.calls) == ("completed", "success", ["begin", "complete"])
    lease = cloud.store.lease_info()
    assert (lease["holder"], lease["live"]) == ("req-1", False)


def test_the_review_deadline_leaves_two_minutes_of_the_invocation(cloud, tmp_path, monkeypatch):
    gen = cloud.store.enqueue(REPO, 7, HEAD, BASE, "main", "opened")
    seen = {}

    def fake_run(g, **kwargs):
        seen.update(kwargs)
        return "completed"

    monkeypatch.setattr(aws, "run_generation", fake_run)
    deps = _deps(cloud, tmp_path, [])
    aws.process_generation(gen.id, _Context(remaining_s=890.0), deps)
    assert seen["deadline_s"] == 770.0
    assert (seen["lease"].holder, seen["lease"].ttl_s) == ("req-1", aws.LEASE_TTL_S)
    aws.process_generation(gen.id, _Context(remaining_s=2000.0), deps)
    assert seen["deadline_s"] == 900.0


def test_a_held_lease_raises_so_sqs_retries(cloud, tmp_path):
    gen = cloud.store.enqueue(REPO, 7, HEAD, BASE, "main", "opened")
    cloud.store.acquire_lease("other-request", 600)
    with pytest.raises(aws.LeaseHeld):
        aws.process_generation(gen.id, _Context(), _deps(cloud, tmp_path, []))
    assert cloud.store.get(gen.id).state == "queued"
    assert cloud.store.lease_info()["holder"] == "other-request"


def test_a_redelivered_completed_generation_is_a_no_op(cloud, tmp_path):
    gen = cloud.store.enqueue(REPO, 7, HEAD, BASE, "main", "opened")
    deps = _deps(cloud, tmp_path, [])
    assert aws.process_generation(gen.id, _Context(), deps) == "completed"
    cloud.store.acquire_lease("other-request", 600)
    assert aws.process_generation(gen.id, _Context(), deps) == "skipped:completed"
    assert deps.publisher.calls == ["begin", "complete"]


def test_a_crashed_invocation_is_adopted_on_redelivery(cloud, tmp_path, monkeypatch):
    clock = [1000.0]
    store = DynamoStore("gate", cloud.ddb, clock=lambda: clock[0])
    gen = store.enqueue(REPO, 7, HEAD, BASE, "main", "opened")
    other = store.enqueue(REPO, 8, HEAD, BASE, "main", "opened")
    dead = store.acquire_lease("dead-request", 60)
    store.start(gen.id, dead)
    store.start(other.id, dead)
    clock[0] += 61
    sent = []
    deps = _deps(cloud, tmp_path, sent)
    deps.store = store
    monkeypatch.setattr(aws, "run_generation", lambda g, **kw: f"ran:{g.id}")
    assert aws.process_generation(gen.id, _Context(), deps) == f"ran:{gen.id}"
    assert [g.id for g in sent] == [other.id]


def test_a_paused_gate_leaves_the_generation_queued(cloud, tmp_path):
    gen = cloud.store.enqueue(REPO, 7, HEAD, BASE, "main", "opened")
    cloud.store.set_paused(True)
    assert aws.process_generation(gen.id, _Context(), _deps(cloud, tmp_path, [])) == "paused"
    assert cloud.store.get(gen.id).state == "queued"


class _NoPulls:
    def request(self, method, repo, path, **kwargs):
        return []


def test_the_watchdog_requeues_an_expired_generation_and_sends_it(cloud):
    clock = [1000.0]
    store = DynamoStore("gate", cloud.ddb, clock=lambda: clock[0])
    gen = store.enqueue(REPO, 7, HEAD, BASE, "main", "opened")
    token = store.acquire_lease("dead-request", 60)
    store.start(gen.id, token)
    clock[0] += 61
    sqs_send = lambda g: aws.send_generation(cloud.sqs, cloud.queue_url, g)  # noqa: E731
    deps = aws.WatchdogDeps(
        store=store, watchdog=Watchdog(store, _NoPulls(), [REPO], watchdog_s=60, reconcile_s=600), send=sqs_send
    )
    result = aws.watchdog_tick(deps, 0.0)
    assert (result["requeued"], result["reconciled"], result["sent"]) == ([gen.id], [], [gen.id])
    assert store.get(gen.id).state == "queued"
    assert _messages(cloud) == [({"generation_id": gen.id}, f"{REPO}#7", f"{gen.id}-1")]


def test_the_worker_cold_start_reads_the_key_from_secrets_manager(cloud, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from prxref.gate.app_auth import AppAuth

    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        .decode()
    )
    arn = cloud.sm.create_secret(Name="gate-app-key", SecretString=pem)["ARN"]
    monkeypatch.setenv(aws.APP_KEY_SECRET_ENV, arn)
    monkeypatch.setattr(AppAuth, "bot_login", lambda self: "sdlc[bot]")
    deps = aws._worker_deps()
    assert deps.publisher.bot_login == "sdlc[bot]"
    assert deps.settings.runs_dir == aws.RUNS_DIR
    assert worker._redact(pem) == "[redacted]"
    assert aws._worker_deps() is deps
