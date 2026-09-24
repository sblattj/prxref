"""Every built-in forge's ``get_compare_diff`` held to one shared contract.

Replay (#65) reviews a pinned ``base_sha``/``head_sha`` range instead of a live
pull request, so each forge has to turn that range into what its ``get_diff``
already returns: one unified diff that ``parse_unified_diff`` reads, holding the
changes on the head side of the merge-base. The per-forge test files pin each
adapter's request shape. This file pins what all of them share, against a fake
session that answers only the request the forge's compare API documents. A
request it does not recognise gets a 404, and a write fails the test.

Fixture provenance, stated plainly:

- ``THREE_DOT`` is real ``git diff base...feature`` output from a throwaway local
  repository built for this file. The feature branch renames a doc, edits one
  module, deletes one and adds one. After the fork, the base branch edits
  README.md, and ``README_TWO_DOT`` is the extra file that ``git diff
  base..feature`` shows because of that edit. A server that skips the merge-base
  hands that file back, so the parse check fails.
- The Bitbucket Data Center spelling of the same diff is ``THREE_DOT`` rewritten
  by ``_dc_spelling`` to the ``src://``/``dst://`` prefixes Data Center writes.
- ``DC_10_4_2_CAPTURE`` is byte-for-byte the raw diff a Bitbucket Data Center
  10.4.2 instance served for a pull request on 2026-08-30. It is recorded in
  ``docs/live-instance-verification/followup-tasks-real-forge-fixtures.md``.
  Here it plays the ``/diff?since=&until=`` response. That endpoint writes the
  same raw format, but it was not itself probed against a live instance.
- The GitHub and Bitbucket Cloud responses are ``THREE_DOT`` served as text. The
  GitLab response is GitLab's documented ``diffs`` entries built from it. None of
  them is a capture.

Space-only context lines are spelled ``\\x20`` in the fixtures, so an editor or
linter that trims trailing whitespace cannot rewrite the recorded bytes.

Adding a forge takes one ``serve`` function and one ``CompareCase`` in
``CASES``. ``test_every_builtin_forge_implements_get_compare_diff`` finds a new
``prxref.forges`` module on its own. ``test_every_builtin_forge_has_a_compare_case``
skips, naming the missing entry, until that case exists.
"""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

import pytest
import requests

import prxref.forges
from prxref.triage import parse_unified_diff

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
MERGE_BASE_SHA = "c" * 40

THREE_DOT = '''diff --git a/docs/usage.md b/docs/guide.md
similarity index 95%
rename from docs/usage.md
rename to docs/guide.md
index cccb70a..9341bfd 100644
--- a/docs/usage.md
+++ b/docs/guide.md
@@ -1,4 +1,4 @@
-# Usage
+# Guide
\x20
 Call `total()` with the invoice lines and the tax rate.
 Amounts are integers in cents.
diff --git a/src/acme/billing.py b/src/acme/billing.py
index 4cbb1a5..2827ee4 100644
--- a/src/acme/billing.py
+++ b/src/acme/billing.py
@@ -7,5 +7,10 @@ def subtotal(lines: list[tuple[int, int]]) -> int:
\x20
\x20
 def total(lines: list[tuple[int, int]], tax_rate: float) -> int:
-    """Return the subtotal plus tax, in whole cents."""
-    return int(subtotal(lines) * (1 + tax_rate))
+    """Return the subtotal plus tax, rounded to whole cents."""
+    return round(subtotal(lines) * (1 + tax_rate))
+
+
+def discounted(lines: list[tuple[int, int]], percent: int) -> int:
+    """Return the subtotal less ``percent`` per cent."""
+    return subtotal(lines) * (100 - percent) // 100
diff --git a/src/acme/legacy.py b/src/acme/legacy.py
deleted file mode 100644
index a738496..0000000
--- a/src/acme/legacy.py
+++ /dev/null
@@ -1,5 +0,0 @@
-"""Deprecated helpers kept for one release."""
-
-
-def old_total(lines):
-    return sum(q * c for q, c in lines)
diff --git a/src/acme/refunds.py b/src/acme/refunds.py
new file mode 100644
index 0000000..c8f9dba
--- /dev/null
+++ b/src/acme/refunds.py
@@ -0,0 +1,8 @@
+"""Refunds against a settled invoice."""
+
+
+def refund(paid_cents: int, amount_cents: int) -> int:
+    """Return the balance left after refunding ``amount_cents``."""
+    if amount_cents > paid_cents:
+        raise ValueError("refund exceeds the amount paid")
+    return paid_cents - amount_cents
'''

README_TWO_DOT = '''diff --git a/README.md b/README.md
index 3ddb646..848e72a 100644
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-acme storefront, now with docs
+acme storefront
'''

DC_10_4_2_CAPTURE = '''diff --git src://cache.py dst://cache.py
index 3cc2d49..4a62838 100644
--- src://cache.py
+++ dst://cache.py
@@ -17,16 +17,38 @@ class TTLCache:
         with self._lock:
             entry = self._data.get(key)
             if entry is None:
                 return None
             stored_at, value = entry
             if time.monotonic() - stored_at > self._ttl:
                 del self._data[key]
                 return None
             return value
\x20
+    def get_many(self, keys: list[str]) -> dict[str, object]:
+        """Return every live entry among ``keys``."""
+        out = {}
+        for key in keys:
+            entry = self._data.get(key)
+            if entry is None:
+                continue
+            stored_at, value = entry
+            if time.monotonic() - stored_at <= self._ttl:
+                out[key] = value
+        return out
+
+    def purge_expired(self) -> int:
+        """Drop every expired entry, returning how many were removed."""
+        removed = 0
+        with self._lock:
+            for key, (stored_at, _value) in self._data.items():
+                if time.monotonic() - stored_at > self._ttl:
+                    del self._data[key]
+                    removed += 1
+        return removed
+
     def put(self, key: str, value: object) -> None:
         with self._lock:
             if len(self._data) >= self._max_entries:
                 oldest = min(self._data, key=lambda k: self._data[k][0])
                 del self._data[oldest]
             self._data[key] = (time.monotonic(), value)
diff --git src://rates.py dst://rates.py
index f0578dd..1eaa9aa 100644
--- src://rates.py
+++ dst://rates.py
@@ -1,6 +1,13 @@
 """Currency conversion helpers."""
\x20
\x20
 def convert(amount_cents: int, rate: float) -> int:
     """Convert ``amount_cents`` using ``rate``, returning whole cents."""
     return int(amount_cents * rate)
+
+
+def convert_all(amounts: list[int], rate: float, results: list[int] = []) -> list[int]:
+    """Convert every amount in ``amounts``, accumulating into ``results``."""
+    for amount in amounts:
+        results.append(convert(amount, rate))
+    return results
'''

THREE_DOT_FILES = (
    ("docs/guide.md", "docs/usage.md", "renamed", 1, 1),
    ("src/acme/billing.py", "src/acme/billing.py", "modified", 7, 2),
    ("src/acme/legacy.py", "src/acme/legacy.py", "removed", 0, 5),
    ("src/acme/refunds.py", None, "added", 8, 0),
)
DC_CAPTURE_FILES = (
    ("cache.py", "cache.py", "modified", 22, 0),
    ("rates.py", "rates.py", "modified", 7, 0),
)


def _dc_spelling(diff: str) -> str:
    """Rewrite git's ``a/``/``b/`` path prefixes as Data Center's ``src://``/``dst://``."""
    out = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git a/"):
            old, new = line[len("diff --git a/") :].split(" b/", 1)
            line = f"diff --git src://{old} dst://{new}"
        elif line.startswith("--- a/"):
            line = "--- src://" + line[len("--- a/") :]
        elif line.startswith("+++ b/"):
            line = "+++ dst://" + line[len("+++ b/") :]
        out.append(line)
    return "".join(out)


def _hunks(diff: str, new_path: str) -> str:
    """Return the hunks of ``new_path``'s section of ``diff``, headers dropped, as GitLab stores them."""
    for section in diff.split("diff --git ")[1:]:
        if section.splitlines()[0].endswith(f" b/{new_path}"):
            return section[section.index("@@") :]
    raise KeyError(new_path)


def _gitlab_entry(old_path: str, new_path: str, diff: str, **flags: bool) -> dict:
    new_file = flags.get("new_file", False)
    deleted_file = flags.get("deleted_file", False)
    return {
        "old_path": old_path,
        "new_path": new_path,
        "a_mode": "0" if new_file else "100644",
        "b_mode": "0" if deleted_file else "100644",
        "new_file": new_file,
        "renamed_file": flags.get("renamed_file", False),
        "deleted_file": deleted_file,
        "diff": _hunks(diff, new_path),
    }


GITLAB_THREE_DOT = [
    _gitlab_entry("docs/usage.md", "docs/guide.md", THREE_DOT, renamed_file=True),
    _gitlab_entry("src/acme/billing.py", "src/acme/billing.py", THREE_DOT),
    _gitlab_entry("src/acme/legacy.py", "src/acme/legacy.py", THREE_DOT, deleted_file=True),
    _gitlab_entry("src/acme/refunds.py", "src/acme/refunds.py", THREE_DOT, new_file=True),
]
GITLAB_README_TWO_DOT = _gitlab_entry("README.md", "README.md", README_TWO_DOT)


# --- a session that only answers the documented request -------------------------


@dataclass
class Call:
    """One request a forge issued through ``RoutedSession``."""

    method: str
    url: str
    params: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)


def _response(status: int, body: str, content_type: str, url: str) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = body.encode("utf-8")
    response.headers["Content-Type"] = content_type
    response.encoding = "utf-8"
    response.url = url
    response.reason = HTTPStatus(status).phrase
    return response


def _text(body: str, url: str, content_type: str = "text/plain; charset=utf-8") -> requests.Response:
    return _response(200, body, content_type, url)


def _json(payload: Any, url: str) -> requests.Response:
    return _response(200, json.dumps(payload), "application/json", url)


class RoutedSession:
    """A ``requests.Session`` stand-in that answers GETs through one ``serve`` function.

    ``serve`` returns a response, or ``None`` for a request the emulated API does
    not know, which becomes a 404. Every request is recorded in ``calls``. Any
    write raises, because a compare must only read.
    """

    def __init__(self, serve: Callable[[Call], requests.Response | None]) -> None:
        self._serve = serve
        self.calls: list[Call] = []

    def get(self, url: str, params: dict | None = None, headers: dict | None = None, **kwargs: Any):
        call = Call("GET", url, dict(params or {}), dict(headers or {}))
        self.calls.append(call)
        response = self._serve(call)
        if response is None:
            return _response(404, '{"message": "Not Found"}', "application/json", url)
        return response

    def _refuse(self, method: str, url: str) -> None:
        self.calls.append(Call(method, url))
        raise AssertionError(f"get_compare_diff issued {method} {url}; it must only read")

    def post(self, url: str, *args: Any, **kwargs: Any):
        self._refuse("POST", url)

    def put(self, url: str, *args: Any, **kwargs: Any):
        self._refuse("PUT", url)

    def patch(self, url: str, *args: Any, **kwargs: Any):
        self._refuse("PATCH", url)

    def delete(self, url: str, *args: Any, **kwargs: Any):
        self._refuse("DELETE", url)

    def request(self, method: str, url: str, *args: Any, **kwargs: Any):
        if method.upper() == "GET":
            return self.get(url, params=kwargs.get("params"), headers=kwargs.get("headers"))
        self._refuse(method.upper(), url)


# --- one serve function per forge -----------------------------------------------


GITHUB_COMPARE = f"https://api.github.com/repos/acme/api/compare/{BASE_SHA}...{HEAD_SHA}"


def _serve_github(call: Call, payload: str) -> requests.Response | None:
    if call.url != GITHUB_COMPARE:
        return None
    if call.headers.get("Accept") != "application/vnd.github.diff":
        # The default media type is the JSON comparison object, not a diff.
        return _json({"status": "ahead", "ahead_by": 3, "files": []}, call.url)
    return _text(payload, call.url, "application/vnd.github.diff; charset=utf-8")


GITLAB_COMPARE = "https://gitlab.com/api/v4/projects/acme%2Fapi/repository/compare"


def _serve_gitlab(call: Call, payload: list[dict]) -> requests.Response | None:
    if call.url != GITLAB_COMPARE:
        return None
    if call.params.get("from") != BASE_SHA or call.params.get("to") != HEAD_SHA:
        return None
    diffs = [GITLAB_README_TWO_DOT, *payload] if call.params.get("straight") == "true" else payload
    body = {
        "commit": None,
        "commits": [],
        "diffs": diffs,
        "compare_timeout": False,
        "compare_same_ref": not payload,
    }
    return _json(body, call.url)


BITBUCKET_DIFF = "https://api.bitbucket.org/2.0/repositories/acme/api/diff"


def _serve_bitbucket(call: Call, payload: str) -> requests.Response | None:
    if call.url == f"{BITBUCKET_DIFF}/{BASE_SHA}..{HEAD_SHA}":
        # Bitbucket reads SOURCE..DEST, so git's order names the reverse range:
        # a different diff that still parses.
        return _text(README_TWO_DOT, call.url)
    if call.url != f"{BITBUCKET_DIFF}/{HEAD_SHA}..{BASE_SHA}":
        return None
    if call.params.get("topic") == "false":
        return _text(README_TWO_DOT + payload, call.url)
    return _text(payload, call.url)


BBS_REPO = "https://bitbucket.example.com/rest/api/1.0/projects/PLAT/repos/api"


def _serve_bitbucket_server(call: Call, payload: str) -> requests.Response | None:
    if call.url == f"{BBS_REPO}/commits/{HEAD_SHA}/merge-base":
        if call.params.get("otherCommitId") != BASE_SHA:
            return None
        return _json({"id": MERGE_BASE_SHA, "displayId": MERGE_BASE_SHA[:11]}, call.url)
    if call.url != f"{BBS_REPO}/diff" or call.params.get("until") != HEAD_SHA:
        return None
    if "text/plain" not in call.headers.get("Accept", ""):
        # The spec lists the raw diff only as text/plain;qs=0.1. What a live
        # server does for a request that does not ask for it is unverified, so
        # this emulation refuses it, pinning the explicit Accept.
        return _response(406, '{"errors": []}', "application/json", call.url)
    if call.params.get("since") == MERGE_BASE_SHA:
        return _text(payload, call.url)
    if call.params.get("since") == BASE_SHA:
        return _text(_dc_spelling(README_TWO_DOT) + payload, call.url)
    return None


# --- the cases ------------------------------------------------------------------


@dataclass(frozen=True)
class CompareCase:
    """One forge's compare API, emulated, and what its diff must parse into.

    ``payload`` is the range as that API renders it and ``empty_payload`` is an
    empty range in the same rendering. ``verbatim`` says whether the forge must
    return the API's text unmodified, which is true of every raw-text API.
    """

    id: str
    forge: str
    pr_url: str
    serve: Callable[[Call, Any], requests.Response | None]
    payload: Any
    empty_payload: Any
    expected: tuple[tuple[str, str | None, str, int, int], ...]
    verbatim: bool


CASES = [
    CompareCase(
        id="github",
        forge="github",
        pr_url="https://github.com/acme/api/pull/42",
        serve=_serve_github,
        payload=THREE_DOT,
        empty_payload="",
        expected=THREE_DOT_FILES,
        verbatim=True,
    ),
    CompareCase(
        id="gitlab",
        forge="gitlab",
        pr_url="https://gitlab.com/acme/api/-/merge_requests/7",
        serve=_serve_gitlab,
        payload=GITLAB_THREE_DOT,
        empty_payload=[],
        expected=THREE_DOT_FILES,
        verbatim=False,
    ),
    CompareCase(
        id="bitbucket",
        forge="bitbucket",
        pr_url="https://bitbucket.org/acme/api/pull-requests/42",
        serve=_serve_bitbucket,
        payload=THREE_DOT,
        empty_payload="",
        expected=THREE_DOT_FILES,
        verbatim=True,
    ),
    CompareCase(
        id="bitbucket_server",
        forge="bitbucket_server",
        pr_url="https://bitbucket.example.com/projects/PLAT/repos/api/pull-requests/42",
        serve=_serve_bitbucket_server,
        payload=_dc_spelling(THREE_DOT),
        empty_payload="",
        expected=THREE_DOT_FILES,
        verbatim=True,
    ),
    CompareCase(
        id="bitbucket_server-dc-10.4.2-capture",
        forge="bitbucket_server",
        pr_url="https://bitbucket.example.com/projects/PLAT/repos/api/pull-requests/42",
        serve=_serve_bitbucket_server,
        payload=DC_10_4_2_CAPTURE,
        empty_payload="",
        expected=DC_CAPTURE_FILES,
        verbatim=True,
    ),
]
CASE_IDS = [case.id for case in CASES]
VERBATIM_CASES = [case for case in CASES if case.verbatim]


def _builtin_forges() -> list[str]:
    """Name every ``prxref.forges`` module that defines a ``ForgeImpl``."""
    names = []
    for info in pkgutil.iter_modules(prxref.forges.__path__):
        module = importlib.import_module(f"prxref.forges.{info.name}")
        if hasattr(module, "ForgeImpl"):
            names.append(info.name)
    return sorted(names)


BUILTIN_FORGES = _builtin_forges()


def _shape(diff: str) -> tuple[tuple[str, str | None, str, int, int], ...]:
    return tuple(
        (f.path, f.old_path, f.status, f.lines_added, f.lines_removed) for f in parse_unified_diff(diff)
    )


def _compare(case: CompareCase, session: RoutedSession) -> str:
    impl = importlib.import_module(f"prxref.forges.{case.forge}").ForgeImpl
    ref = impl.parse_pr_url(case.pr_url)
    assert ref is not None, f"{case.forge} does not parse its own case URL {case.pr_url}"
    return impl(session=session).get_compare_diff(ref, base_sha=BASE_SHA, head_sha=HEAD_SHA)


def _serving(case: CompareCase, payload: Any) -> RoutedSession:
    return RoutedSession(lambda call: case.serve(call, payload))


# --- discovery ------------------------------------------------------------------


def test_forge_discovery_finds_the_four_known_adapters_and_skips_base():
    assert {"bitbucket", "bitbucket_server", "github", "gitlab"} <= set(BUILTIN_FORGES)
    assert "base" not in BUILTIN_FORGES


@pytest.mark.parametrize("name", BUILTIN_FORGES)
def test_every_builtin_forge_implements_get_compare_diff(name):
    impl = importlib.import_module(f"prxref.forges.{name}").ForgeImpl
    method = getattr(impl, "get_compare_diff", None)
    assert callable(method), f"prxref.forges.{name}.ForgeImpl has no get_compare_diff, so replay cannot pin a range"
    params = inspect.signature(method).parameters
    assert list(params) == ["self", "ref", "base_sha", "head_sha"]
    assert params["base_sha"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["head_sha"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize("name", BUILTIN_FORGES)
def test_every_builtin_forge_has_a_compare_case(name):
    if name not in {case.forge for case in CASES}:
        pytest.skip(
            f"prxref.forges.{name} has no CompareCase: add one entry to CASES in "
            "tests/test_forge_compare_contract.py, with a serve function for its compare API"
        )


def test_the_cases_are_the_fixtures_they_claim_to_be():
    assert _shape(THREE_DOT) == THREE_DOT_FILES
    assert _shape(_dc_spelling(THREE_DOT)) == THREE_DOT_FILES
    assert _shape(DC_10_4_2_CAPTURE) == DC_CAPTURE_FILES
    assert len(DC_10_4_2_CAPTURE) == 2163
    assert _shape(README_TWO_DOT) == (("README.md", "README.md", "modified", 1, 1),)


# --- the contract ---------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_compare_diff_parses_into_the_merge_base_changes(case):
    diff = _compare(case, _serving(case, case.payload))

    assert _shape(diff) == case.expected


@pytest.mark.parametrize("case", VERBATIM_CASES, ids=[case.id for case in VERBATIM_CASES])
def test_compare_diff_returns_the_raw_text_unmodified(case):
    diff = _compare(case, _serving(case, case.payload))

    assert diff == case.payload


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_compare_diff_names_both_ends_of_the_range(case):
    session = _serving(case, case.payload)
    _compare(case, session)

    sent = " ".join(f"{call.url} {' '.join(map(str, call.params.values()))}" for call in session.calls)
    assert BASE_SHA in sent
    assert HEAD_SHA in sent


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_compare_diff_only_reads(case):
    session = _serving(case, case.payload)
    _compare(case, session)

    assert session.calls
    assert {call.method for call in session.calls} == {"GET"}


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_compare_diff_returns_an_empty_range_as_empty_text(case):
    diff = _compare(case, _serving(case, case.empty_payload))

    assert diff == ""


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_compare_diff_raises_on_http_error(case):
    with pytest.raises(requests.HTTPError):
        _compare(case, RoutedSession(lambda call: None))


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_compare_diff_raises_on_transport_error(case):
    def down(call: Call) -> requests.Response | None:
        raise requests.ConnectionError(f"connection refused: {call.url}")

    with pytest.raises(requests.ConnectionError):
        _compare(case, RoutedSession(down))
