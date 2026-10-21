"""The gate's durable state on one DynamoDB table: :class:`DynamoStore`, the AWS twin of :class:`Store`.

Same public methods and semantics as the SQLite store, so the receiver, the
worker, the publisher and the watchdog run on it unchanged. Single-table
layout (``pk``/``sk`` strings; numbers zero-padded so they sort):

========================  ====================  ==========================================
pk                        sk                    holds
========================  ====================  ==========================================
``GEN``                   ``<id:012>``          one generation row (the only mutable copy)
``OPEN``                  ``<id:012>``          marker while queued/in progress, with state
``PR#<repo>#<n>``         ``GEN#<gen:08>``      immutable pointer; enforces unique gen per PR
``HEAD#<repo>#<sha>``     ``<id:012>``          immutable pointer for rerequest lookups
``DELIVERY#<id>``         ``DELIVERY``          webhook dedupe, ``ttl`` 7 days
``PUB#<id>``              ``PUB``               the review posted for a generation
``LEASE``                 ``worker``            the single lease and its fencing token
``META``                  ``COUNTER``/...       id counter, ``PAUSED``, ``PUBLISH``
========================  ====================  ==========================================

Every fenced write is one ``TransactWriteItems`` whose first item is a
``ConditionCheck`` on the lease (``token = :t AND expires_at > :now``); a
cancel on that item is :class:`LeaseLost`, on any other item a
:class:`StateError`. Lease liveness is always ``expires_at`` against the
injected clock, never DynamoDB's TTL feature: the lease item carries no
``ttl`` attribute, since deleting it would restart the fencing token at 1.
Every read is strongly consistent. A Lambda holder is its request id, not a
``host:pid``, so :func:`~prxref.gate.store.holder_alive` treats it as alive
and a dead holder's lease simply runs out.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

from botocore.exceptions import ClientError

from prxref.gate.store import AUTH_MARKERS, FINAL_STATES, Generation, LeaseLost, StateError

DELIVERY_TTL_S = 7 * 24 * 3600
_ENQUEUE_RETRIES = 10
_GEN_FIELDS = Generation.__dataclass_fields__
_INT_FIELDS = ("id", "pr_number", "gen", "attempts")
_FLOAT_FIELDS = ("created_at", "started_at", "finished_at")


def _s(value: str) -> dict:
    return {"S": value}


def _n(value: float) -> dict:
    return {"N": repr(value) if isinstance(value, float) else str(value)}


def _key(pk: str, sk: str) -> dict:
    return {"pk": _s(pk), "sk": _s(sk)}


def _gen_key(gen_id: int) -> dict:
    return _key("GEN", f"{gen_id:012d}")


def _open_key(gen_id: int) -> dict:
    return _key("OPEN", f"{gen_id:012d}")


def _pr_pk(repo: str, pr_number: int) -> str:
    return f"PR#{repo}#{pr_number}"


def _num(item: dict, name: str) -> float | None:
    raw = item.get(name)
    return float(raw["N"]) if raw is not None else None


def _to_generation(item: dict) -> Generation:
    values: dict[str, Any] = {}
    for name in _GEN_FIELDS:
        raw = item.get(name)
        if raw is None:
            values[name] = None
        elif name in _INT_FIELDS or name == "check_run_id":
            values[name] = int(raw["N"])
        elif name in _FLOAT_FIELDS:
            values[name] = float(raw["N"])
        else:
            values[name] = raw["S"]
    return Generation(**values)


def _code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


class _Cancelled(Exception):
    """A transaction was cancelled by a condition on an item other than the fence."""

    def __init__(self, codes: list[str]) -> None:
        super().__init__(", ".join(codes))
        self.codes = codes


def create_table(client: Any, name: str) -> None:
    """Create the gate table (on-demand, ``ttl`` attribute); for tests and local experiments.

    ``deploy/aws/template.yaml`` declares the same table for a deployment.
    """
    client.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    client.update_time_to_live(TableName=name, TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"})


class DynamoStore:
    """All reads and transitions over the gate's DynamoDB table."""

    def __init__(self, table: str, client: Any = None, *, clock: Callable[[], float] = time.time) -> None:
        if client is None:
            import boto3

            client = boto3.client("dynamodb")
        self._db = client
        self._table = table
        self._clock = clock

    def close(self) -> None:
        """Nothing to close; present so callers treat both stores alike."""

    # -- primitives -------------------------------------------------------

    def _get(self, key: dict) -> dict | None:
        return self._db.get_item(TableName=self._table, Key=key, ConsistentRead=True).get("Item")

    def _query(self, pk: str, *, forward: bool = True, prefix: str | None = None, limit: int | None = None) -> Iterator:
        values: dict[str, dict] = {":pk": _s(pk)}
        cond = "pk = :pk"
        if prefix is not None:
            cond += " AND begins_with(sk, :pre)"
            values[":pre"] = _s(prefix)
        kwargs: dict[str, Any] = {
            "TableName": self._table,
            "KeyConditionExpression": cond,
            "ExpressionAttributeValues": values,
            "ScanIndexForward": forward,
            "ConsistentRead": True,
        }
        if limit is not None:
            kwargs["Limit"] = limit
        while True:
            page = self._db.query(**kwargs)
            yield from page.get("Items", [])
            last = page.get("LastEvaluatedKey")
            if not last or limit is not None:
                return
            kwargs["ExclusiveStartKey"] = last

    def _transact(self, items: list[dict], *, fence: bool = False) -> None:
        """Run ``items`` as one transaction; ``fence`` marks item 0 as the lease check."""
        for item in items:
            next(iter(item.values()))["TableName"] = self._table
        try:
            self._db.transact_write_items(TransactItems=items)
        except ClientError as exc:
            if _code(exc) != "TransactionCanceledException":
                raise
            codes = [r.get("Code", "None") for r in exc.response.get("CancellationReasons", [])]
            if fence and codes and codes[0] == "ConditionalCheckFailed":
                raise LeaseLost("fencing token is not the live worker lease") from None
            raise _Cancelled(codes) from None

    def _fence_item(self, token: int) -> dict:
        return {
            "ConditionCheck": {
                "Key": _key("LEASE", "worker"),
                "ConditionExpression": "#token = :tok AND #exp > :now",
                "ExpressionAttributeNames": {"#token": "token", "#exp": "expires_at"},
                "ExpressionAttributeValues": {":tok": _n(token), ":now": _n(self._clock())},
            }
        }

    def _expired_lease_item(self) -> dict:
        return {
            "ConditionCheck": {
                "Key": _key("LEASE", "worker"),
                "ConditionExpression": "attribute_not_exists(pk) OR #exp <= :now",
                "ExpressionAttributeNames": {"#exp": "expires_at"},
                "ExpressionAttributeValues": {":now": _n(self._clock())},
            }
        }

    @staticmethod
    def _set_state(gen_id: int, state: str, *, when: str, extra: dict | None = None, cond: str = "") -> dict:
        """An Update of the generation row to ``state``, conditioned on its current state being ``when``."""
        sets = ["#state = :state"]
        values = {":state": _s(state), ":when": _s(when)}
        for name, value in (extra or {}).items():
            sets.append(f"{name} = :{name}")
            values[f":{name}"] = value
        return {
            "Update": {
                "Key": _gen_key(gen_id),
                "UpdateExpression": "SET " + ", ".join(sets),
                "ConditionExpression": "#state = :when" + cond,
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": values,
            }
        }

    @staticmethod
    def _open_state(gen_id: int, state: str) -> dict:
        return {
            "Update": {
                "Key": _open_key(gen_id),
                "UpdateExpression": "SET #state = :state",
                "ConditionExpression": "attribute_exists(pk)",
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": {":state": _s(state)},
            }
        }

    @staticmethod
    def _close_open(gen_id: int) -> dict:
        return {"Delete": {"Key": _open_key(gen_id)}}

    def _open_rows(self) -> list[dict]:
        """Every queued or in-progress generation's marker, oldest id first."""
        return [
            {
                "id": int(i["id"]["N"]),
                "repo": i["repo"]["S"],
                "pr_number": int(i["pr_number"]["N"]),
                "gen": int(i["gen"]["N"]),
                "state": i["state"]["S"],
            }
            for i in self._query("OPEN")
        ]

    def _latest_gen_no(self, repo: str, pr_number: int) -> int:
        for item in self._query(_pr_pk(repo, pr_number), forward=False, prefix="GEN#", limit=1):
            return int(item["gen"]["N"])
        return 0

    # -- deliveries ---------------------------------------------------------

    def record_delivery(self, delivery_id, event, action, repo, pr_number) -> bool:
        """Insert one delivery row; False when the id was already recorded (within its 7-day TTL)."""
        now = self._clock()
        item = {
            **_key(f"DELIVERY#{delivery_id}", "DELIVERY"),
            "received_at": _n(now),
            "event": _s(event),
            "handled": _n(0),
            "ttl": _n(int(now) + DELIVERY_TTL_S),
        }
        if action is not None:
            item["action"] = _s(str(action))
        if repo is not None:
            item["repo"] = _s(repo)
        if pr_number is not None:
            item["pr_number"] = _n(int(pr_number))
        try:
            self._db.put_item(TableName=self._table, Item=item, ConditionExpression="attribute_not_exists(pk)")
        except ClientError as exc:
            if _code(exc) == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def mark_delivery_handled(self, delivery_id: str) -> None:
        try:
            self._db.update_item(
                TableName=self._table,
                Key=_key(f"DELIVERY#{delivery_id}", "DELIVERY"),
                UpdateExpression="SET handled = :one",
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeValues={":one": _n(1)},
            )
        except ClientError as exc:
            if _code(exc) != "ConditionalCheckFailedException":
                raise

    # -- generations --------------------------------------------------------

    def enqueue(self, repo, pr_number, head_sha, base_sha, base_ref, reason) -> Generation:
        """Insert the PR's next generation; supersede its older queued ones in the same transaction.

        The ``PR#`` pointer is written with ``attribute_not_exists``, so two
        receivers racing on one PR cannot both take a gen number; the loser
        re-reads and retries.
        """
        new_id = int(
            self._db.update_item(
                TableName=self._table,
                Key=_key("META", "COUNTER"),
                UpdateExpression="ADD #v :one",
                ExpressionAttributeNames={"#v": "value"},
                ExpressionAttributeValues={":one": _n(1)},
                ReturnValues="UPDATED_NEW",
            )["Attributes"]["value"]["N"]
        )
        for _ in range(_ENQUEUE_RETRIES):
            now = self._clock()
            gen = self._latest_gen_no(repo, pr_number) + 1
            row = {
                **_gen_key(new_id),
                "id": _n(new_id),
                "repo": _s(repo),
                "pr_number": _n(int(pr_number)),
                "head_sha": _s(head_sha),
                "base_sha": _s(base_sha),
                "base_ref": _s(base_ref),
                "gen": _n(gen),
                "reason": _s(reason),
                "state": _s("queued"),
                "created_at": _n(now),
                "external_id": _s(str(new_id)),
                "attempts": _n(0),
            }
            items: list[dict] = [
                {
                    "Put": {
                        "Item": {**_key(_pr_pk(repo, pr_number), f"GEN#{gen:08d}"), "gen": _n(gen), "id": _n(new_id)},
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                {"Put": {"Item": row}},
                {
                    "Put": {
                        "Item": {
                            **_open_key(new_id),
                            "id": _n(new_id),
                            "repo": _s(repo),
                            "pr_number": _n(int(pr_number)),
                            "gen": _n(gen),
                            "state": _s("queued"),
                        }
                    }
                },
                {"Put": {"Item": {**_key(f"HEAD#{repo}#{head_sha}", f"{new_id:012d}"), "id": _n(new_id)}}},
            ]
            for old in self._open_rows():
                if (old["repo"], old["pr_number"], old["state"]) == (repo, int(pr_number), "queued"):
                    items.append(
                        self._set_state(old["id"], "superseded", when="queued", extra={"finished_at": _n(now)})
                    )
                    items.append(self._close_open(old["id"]))
            try:
                self._transact(items)
            except _Cancelled:
                continue
            return self.get(new_id)
        raise StateError(f"could not enqueue {repo}#{pr_number}: {_ENQUEUE_RETRIES} conflicting writes")

    def cancel_pr(self, repo: str, pr_number: int) -> int:
        """Cancel queued generations of a closed PR that have no check run yet."""
        count = 0
        for row in self._open_rows():
            if (row["repo"], row["pr_number"], row["state"]) != (repo, int(pr_number), "queued"):
                continue
            try:
                self._transact(
                    [
                        self._set_state(
                            row["id"],
                            "cancelled",
                            when="queued",
                            extra={"finished_at": _n(self._clock())},
                            cond=" AND attribute_not_exists(check_run_id)",
                        ),
                        self._close_open(row["id"]),
                    ]
                )
            except _Cancelled:
                continue
            count += 1
        return count

    def next_queued(self) -> Generation | None:
        for row in self._open_rows():
            if row["state"] == "queued" and row["gen"] == self._latest_gen_no(row["repo"], row["pr_number"]):
                return self.get(row["id"])
        return None

    def queued(self) -> list[Generation]:
        """Every queued generation, oldest id first."""
        return [self.get(row["id"]) for row in self._open_rows() if row["state"] == "queued"]

    def get(self, gen_id: int) -> Generation:
        item = self._get(_gen_key(gen_id))
        if item is None:
            raise StateError(f"no generation {gen_id}")
        return _to_generation(item)

    def latest_generation(self, repo: str, pr_number: int) -> Generation | None:
        for item in self._query(_pr_pk(repo, pr_number), forward=False, prefix="GEN#", limit=1):
            return self.get(int(item["id"]["N"]))
        return None

    def latest_generation_for_head(self, repo: str, head_sha: str) -> Generation | None:
        for item in self._query(f"HEAD#{repo}#{head_sha}", forward=False, limit=1):
            return self.get(int(item["id"]["N"]))
        return None

    def has_live_generation(self, repo, pr_number, head_sha, base_sha) -> bool:
        for item in self._query(f"HEAD#{repo}#{head_sha}"):
            g = self.get(int(item["id"]["N"]))
            if (
                g.pr_number == int(pr_number)
                and g.base_sha == base_sha
                and g.state in ("queued", "in_progress", "completed", "failed")
            ):
                return True
        return False

    # -- the lease ------------------------------------------------------------

    def acquire_lease(self, holder, ttl_s, *, holder_alive=lambda h: True) -> int | None:
        """Take the worker lease and return its new fencing token, or None while another holds it."""
        now = self._clock()
        row = self._get(_key("LEASE", "worker"))
        if row is not None:
            current = row["holder"]["S"]
            if current != holder and float(row["expires_at"]["N"]) > now and holder_alive(current):
                return None
            old = int(row["token"]["N"])
            cond, values = "#token = :old", {":old": _n(old)}
        else:
            old = 0
            cond, values = "attribute_not_exists(pk)", {}
        token = old + 1
        kwargs: dict[str, Any] = {
            "TableName": self._table,
            "Item": {
                **_key("LEASE", "worker"),
                "holder": _s(holder),
                "token": _n(token),
                "expires_at": _n(now + ttl_s),
            },
            "ConditionExpression": cond,
        }
        if values:
            kwargs["ExpressionAttributeNames"] = {"#token": "token"}
            kwargs["ExpressionAttributeValues"] = values
        try:
            self._db.put_item(**kwargs)
        except ClientError as exc:
            if _code(exc) == "ConditionalCheckFailedException":
                return None
            raise
        return token

    def _lease_update(self, holder: str, token: int, expires_at: float, *, live_only: bool) -> bool:
        cond = "#holder = :h AND #token = :tok" + (" AND #exp > :now" if live_only else "")
        values = {":h": _s(holder), ":tok": _n(token), ":new": _n(expires_at)}
        if live_only:
            values[":now"] = _n(self._clock())
        try:
            self._db.update_item(
                TableName=self._table,
                Key=_key("LEASE", "worker"),
                UpdateExpression="SET #exp = :new",
                ConditionExpression=cond,
                ExpressionAttributeNames={"#holder": "holder", "#token": "token", "#exp": "expires_at"},
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if _code(exc) == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def renew_lease(self, holder: str, token: int, ttl_s: float) -> bool:
        return self._lease_update(holder, token, self._clock() + ttl_s, live_only=True)

    def release_lease(self, holder: str, token: int) -> None:
        """Expire the worker lease if ``holder`` still holds it under ``token``; the token keeps counting."""
        self._lease_update(holder, token, 0, live_only=False)

    def lease_info(self) -> dict | None:
        row = self._get(_key("LEASE", "worker"))
        if row is None:
            return None
        expires = float(row["expires_at"]["N"])
        return {
            "holder": row["holder"]["S"],
            "token": int(row["token"]["N"]),
            "expires_at": expires,
            "live": expires > self._clock(),
        }

    # -- fenced transitions -------------------------------------------------

    def start(self, gen_id: int, token: int) -> Generation:
        update = self._set_state(gen_id, "in_progress", when="queued", extra={"started_at": _n(self._clock())})
        update["Update"]["UpdateExpression"] += " ADD attempts :one"
        update["Update"]["ExpressionAttributeValues"][":one"] = _n(1)
        try:
            self._transact([self._fence_item(token), update, self._open_state(gen_id, "in_progress")], fence=True)
        except _Cancelled:
            raise StateError(f"generation {gen_id} is not queued") from None
        return self.get(gen_id)

    def set_check_run(self, gen_id: int, token: int, check_run_id: int) -> None:
        update = {
            "Update": {
                "Key": _gen_key(gen_id),
                "UpdateExpression": "SET check_run_id = :c",
                "ConditionExpression": "attribute_exists(pk)",
                "ExpressionAttributeValues": {":c": _n(int(check_run_id))},
            }
        }
        try:
            self._transact([self._fence_item(token), update], fence=True)
        except _Cancelled:
            return  # no such generation: a no-op, as the SQLite UPDATE is

    def finish(self, gen_id, token, state, conclusion=None, error=None) -> None:
        if state not in FINAL_STATES:
            raise StateError(f"{state!r} is not a final state")
        now = self._clock()
        sets = ["#state = :state", "finished_at = :now"]
        removes = []
        values = {":state": _s(state), ":now": _n(now), ":q": _s("queued"), ":p": _s("in_progress")}
        for name, value in (("conclusion", conclusion), ("error", error)):
            if value is None:
                removes.append(f"#{name}")
            else:
                sets.append(f"#{name} = :{name}")
                values[f":{name}"] = _s(value)
        expr = "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else "")
        items = [
            self._fence_item(token),
            {
                "Update": {
                    "Key": _gen_key(gen_id),
                    "UpdateExpression": expr,
                    "ConditionExpression": "#state IN (:q, :p)",
                    "ExpressionAttributeNames": {"#state": "state", "#conclusion": "conclusion", "#error": "error"},
                    "ExpressionAttributeValues": values,
                }
            },
            self._close_open(gen_id),
        ]
        if conclusion is not None:
            items.append({"Put": {"Item": {**_key("META", "PUBLISH"), "at": _n(now)}}})
        try:
            self._transact(items, fence=True)
        except _Cancelled:
            raise StateError(f"generation {gen_id} is already final") from None

    def _requeue_in_progress(self, guard: Callable[[], dict], *, fenced: bool) -> list[int]:
        """Recover every in-progress generation after its worker died (see ``Store._requeue_in_progress``).

        Each row is one transaction guarded by ``guard()`` (the lease check).
        A guard that fails means the lease changed hands mid-sweep: a fenced
        caller gets :class:`LeaseLost`, the expiry sweep stops quietly.
        """
        ids: list[int] = []
        for row in self._open_rows():
            if row["state"] != "in_progress":
                continue
            gen_id = row["id"]
            requeue = row["gen"] >= self._latest_gen_no(row["repo"], row["pr_number"])
            if requeue:
                items = [
                    guard(),
                    self._set_state(gen_id, "queued", when="in_progress"),
                    self._open_state(gen_id, "queued"),
                ]
            else:
                has_check = self.get(gen_id).check_run_id is not None
                final = "stale" if has_check else "superseded"
                cond = " AND attribute_exists(check_run_id)" if has_check else " AND attribute_not_exists(check_run_id)"
                finished = {"finished_at": _n(self._clock())}
                items = [
                    guard(),
                    self._set_state(gen_id, final, when="in_progress", extra=finished, cond=cond),
                    self._close_open(gen_id),
                ]
            try:
                self._transact(items, fence=True)
            except LeaseLost:
                if fenced:
                    raise
                break
            except _Cancelled:
                continue
            if requeue:
                ids.append(gen_id)
        return sorted(ids)

    def requeue_expired(self) -> list[int]:
        """Return in-progress generations to the queue when no live worker lease exists."""
        lease = self.lease_info()
        if lease is not None and lease["live"]:
            return []
        return self._requeue_in_progress(self._expired_lease_item, fenced=False)

    def adopt_orphans(self, token: int) -> list[int]:
        """Re-queue every in-progress generation; called once by a fresh lease holder."""
        lease = self.lease_info()
        if lease is None or lease["token"] != token or not lease["live"]:
            raise LeaseLost(f"fencing token {token} is not the live worker lease")
        return self._requeue_in_progress(lambda: self._fence_item(token), fenced=True)

    def record_publication(self, gen_id: int, token: int, kind: str, github_id: int) -> None:
        """Record the review posted for a generation; a later one for the same generation replaces it."""
        item = {
            **_key(f"PUB#{gen_id}", "PUB"),
            "kind": _s(kind),
            "github_id": _n(int(github_id)),
            "posted_at": _n(self._clock()),
        }
        self._transact([self._fence_item(token), {"Put": {"Item": item}}], fence=True)

    def publication(self, gen_id: int) -> tuple[str, int] | None:
        row = self._get(_key(f"PUB#{gen_id}", "PUB"))
        return (row["kind"]["S"], int(row["github_id"]["N"])) if row else None

    # -- operator views ---------------------------------------------------------

    def set_paused(self, paused: bool) -> None:
        self._db.put_item(TableName=self._table, Item={**_key("META", "PAUSED"), "paused": _n(1 if paused else 0)})

    def is_paused(self) -> bool:
        row = self._get(_key("META", "PAUSED"))
        return bool(row and int(row["paused"]["N"]))

    def queue_depth(self) -> int:
        return sum(1 for row in self._open_rows() if row["state"] == "queued")

    def recent(self, limit: int = 10) -> list[Generation]:
        return [_to_generation(i) for i in self._query("GEN", forward=False, limit=limit)]

    def last_publish_time(self) -> float | None:
        row = self._get(_key("META", "PUBLISH"))
        return _num(row, "at") if row else None

    def last_auth_failure(self) -> str | None:
        seen = 0
        for item in self._query("GEN", forward=False):
            error = (item.get("error") or {}).get("S")
            if error is None:
                continue
            if any(marker in error for marker in AUTH_MARKERS):
                return error
            seen += 1
            if seen >= 50:
                break
        return None
