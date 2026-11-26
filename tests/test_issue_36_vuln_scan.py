"""Issue #36: an OSV.dev known-vulnerability scan of the dependency pins a PR changes.

Every network call here goes to a fake: either the ``post``/``get`` doubles
handed to :func:`prxref.vuln_scan.scan`, or a local HTTP server speaking the
two OSV endpoints the scan uses, so the orchestrator and CLI tests run the
production ``requests`` path with ``PRXREF_OSV_URL`` pointed at it. The
advisory records are trimmed copies of real OSV records.
"""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
import requests

from prxref import cli, config, heuristics, quality, vuln_scan
from prxref.contained_claims import apply_contained_claim_check
from prxref.evals import EVAL_PINS
from prxref.orchestrator import _summary_only_run, orchestrate_review
from prxref.triage import Finding, parse_unified_diff
from prxref.vuln_scan import Pin, changed_pins, scan
from tests.test_integration import MockOpenAIServer, _completion
from tests.test_issue_175_contained_claims import CASE_ONE, CASE_ONE_DIFF
from tests.test_orchestrator import REF, FakeForge, FakeLLM

# ------------------------------------------------------------------ fixtures

LODASH_HIGH = {
    "id": "GHSA-p6mc-m468-83gw",
    "aliases": ["CVE-2020-8203"],
    "summary": "Prototype Pollution in lodash",
    "database_specific": {"severity": "HIGH"},
    "affected": [{"package": {"ecosystem": "npm", "name": "lodash"},
                  "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "4.17.19"}]}]}],
}
LODASH_MODERATE = {
    "id": "GHSA-29mw-wpgm-hmr9",
    "aliases": ["CVE-2020-28500"],
    "summary": "Regular Expression Denial of Service (ReDoS) in lodash",
    "database_specific": {"severity": "MODERATE"},
    "affected": [{"package": {"ecosystem": "npm", "name": "lodash"},
                  "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "4.17.21"}]}]}],
}
REQUESTS_GHSA = {
    "id": "GHSA-x84v-xcm2-53pg",
    "aliases": ["CVE-2018-18074"],
    "summary": "Insufficiently Protected Credentials in Requests",
    "database_specific": {"severity": "HIGH"},
    "affected": [{"package": {"ecosystem": "PyPI", "name": "requests"},
                  "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "2.20.0"}]}]}],
}
REQUESTS_PYSEC = {
    "id": "PYSEC-2018-28",
    "aliases": ["CVE-2018-18074", "GHSA-x84v-xcm2-53pg"],
    "details": "The Requests package before 2.20.0 sends an HTTP Authorization header to an http URI.",
    "affected": [{"package": {"ecosystem": "PyPI", "name": "requests"},
                  "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "2.20.0"}]}]}],
}
RECORDS = {r["id"]: r for r in (LODASH_HIGH, LODASH_MODERATE, REQUESTS_GHSA, REQUESTS_PYSEC)}
AFFECTED = {
    ("npm", "lodash", "4.17.15"): ["GHSA-29mw-wpgm-hmr9", "GHSA-p6mc-m468-83gw"],
    ("PyPI", "requests", "2.19.0"): ["GHSA-x84v-xcm2-53pg", "PYSEC-2018-28"],
}

NPM_BUMP = """diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
@@ -1,6 +1,6 @@
 {
   "dependencies": {
-    "lodash": "4.17.21",
+    "lodash": "4.17.15",
     "express": "4.18.2"
   }
 }
"""
REQUIREMENTS_NEW = """diff --git a/requirements.txt b/requirements.txt
new file mode 100644
--- /dev/null
+++ b/requirements.txt
@@ -0,0 +1,2 @@
+requests==2.19.0
+flask>=2.0
"""
APP_CHANGE = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 def main():
+    print("hello")
     return 0
"""


class FakeOSV:
    """The two OSV endpoints the scan calls, over in-memory tables."""

    def __init__(self, affected=None, records=None, *, status: int = 200, body: Any = None):
        self.affected = dict(AFFECTED if affected is None else affected)
        self.records = dict(RECORDS if records is None else records)
        self.status = status
        self.body = body
        self.queries: list[dict] = []
        self.fetched: list[str] = []
        self.lock = threading.Lock()

    def querybatch(self, payload: dict) -> tuple[int, Any]:
        with self.lock:
            self.queries.extend(payload["queries"])
        if self.body is not None or self.status != 200:
            return self.status, self.body
        results = []
        for q in payload["queries"]:
            key = (q["package"]["ecosystem"], q["package"]["name"], q["version"])
            vulns = [{"id": vid} for vid in self.affected.get(key, [])]
            results.append({"vulns": vulns} if vulns else {})
        return 200, {"results": results}

    def vuln(self, vid: str) -> tuple[int, Any]:
        with self.lock:
            self.fetched.append(vid)
        return (200, self.records[vid]) if vid in self.records else (404, {"code": 5})

    # -- doubles for scan(post=..., get=...)
    def post(self, url, *, json, timeout):
        assert url.endswith("/v1/querybatch") and 0 < timeout <= vuln_scan.REQUEST_TIMEOUT
        return _Resp(*self.querybatch(json))

    def get(self, url, *, timeout):
        assert "/v1/vulns/" in url and 0 < timeout <= vuln_scan.REQUEST_TIMEOUT
        return _Resp(*self.vuln(url.rsplit("/", 1)[1]))


class _Resp:
    def __init__(self, status: int, body: Any):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body


class OSVServer:
    """A local HTTP server serving a :class:`FakeOSV`."""

    def __init__(self, osv: FakeOSV):
        self.osv = osv
        osv_ref = osv

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def _send(self, status, body):
                raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length))
                self._send(*osv_ref.querybatch(payload))

            def do_GET(self):
                self._send(*osv_ref.vuln(self.path.rsplit("/", 1)[1]))

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _files(*diffs: str):
    return parse_unified_diff("".join(diffs))


def _scan(*diffs: str, osv: FakeOSV | None = None, read=None, vuln_max: int = 10, **kw):
    osv = osv or FakeOSV()
    return scan(_files(*diffs), read=read, vuln_max=vuln_max, post=osv.post, get=osv.get, **kw), osv


def _new_file(path: str, lines: list[str]) -> str:
    body = "".join(f"+{ln}\n" for ln in lines)
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n" + body)


# ------------------------------------------- criterion 1: changed pins only

class TestOnlyChangedPinsAreQueried:
    def test_the_bumped_pin_is_queried_and_its_unchanged_neighbour_is_not(self):
        [fd] = _files(NPM_BUMP)
        assert changed_pins(fd, None) == ([Pin("npm", "lodash", "4.17.15", "package.json", 3)], 0)
        result, osv = _scan(NPM_BUMP)
        assert [q["package"]["name"] for q in osv.queries] == ["lodash"]
        assert result.record["queried"] == 1

    def test_a_trailing_comma_re_emit_is_not_a_change(self):
        diff = """diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
@@ -1,5 +1,6 @@
 {
   "dependencies": {
-    "lodash": "4.17.21"
+    "lodash": "4.17.21",
+    "left-pad": "1.3.0"
   }
 }
"""
        [fd] = _files(diff)
        assert changed_pins(fd, None) == ([Pin("npm", "left-pad", "1.3.0", "package.json", 4)], 0)

    def test_a_reorder_read_whole_from_the_head_is_not_a_change(self):
        diff = """diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
@@ -1,6 +1,6 @@
 {
   "dependencies": {
-    "lodash": "4.17.21",
-    "express": "4.18.2"
+    "express": "4.18.2",
+    "lodash":   "4.17.21"
   }
 }
"""
        head = '{\n  "dependencies": {\n    "express": "4.18.2",\n    "lodash":   "4.17.21"\n  }\n}\n'
        [fd] = _files(diff)
        assert changed_pins(fd, {"package.json": head}.get) == ([], 0)
        result, osv = _scan(diff, read={"package.json": head}.get)
        assert osv.queries == [] and result.record["status"] == "ok"

    def test_a_maven_property_bump_changes_the_dependency_that_uses_it(self):
        head = (
            "<project>\n  <properties>\n    <jackson.version>2.9.10</jackson.version>\n  </properties>\n"
            "  <dependencies>\n    <dependency>\n      <groupId>com.fasterxml.jackson.core</groupId>\n"
            "      <artifactId>jackson-databind</artifactId>\n      <version>${jackson.version}</version>\n"
            "    </dependency>\n  </dependencies>\n</project>\n"
        )
        diff = """diff --git a/pom.xml b/pom.xml
--- a/pom.xml
+++ b/pom.xml
@@ -2,3 +2,3 @@
   <properties>
-    <jackson.version>2.13.0</jackson.version>
+    <jackson.version>2.9.10</jackson.version>
   </properties>
"""
        [fd] = _files(diff)
        pin = Pin("Maven", "com.fasterxml.jackson.core:jackson-databind", "2.9.10", "pom.xml", 3)
        assert changed_pins(fd, {"pom.xml": head}.get) == ([pin], 0)
        assert changed_pins(fd, None) == ([], 0)

    @pytest.mark.parametrize(("path", "lines", "pins", "unpinned"), [
        ("requirements-dev.txt", ["requests==2.19.0", "flask>=2.0", "urllib3 == 1.24.1", "# c", "-r base.txt"],
         [("PyPI", "requests", "2.19.0", 1), ("PyPI", "urllib3", "1.24.1", 3)], 1),
        ("pyproject.toml", ["[project]", 'name = "app"', 'version = "1.0.0"', "dependencies = [",
                            '  "jinja2==2.10",', '  "click>=8",', "]"],
         [("PyPI", "jinja2", "2.10", 5)], 1),
        ("go.mod", ["module example.com/app", "", "go 1.21", "", "require (", "\tgolang.org/x/net v0.7.0", ")",
                    "require golang.org/x/text v0.3.7"],
         [("Go", "golang.org/x/net", "0.7.0", 6), ("Go", "golang.org/x/text", "0.3.7", 8)], 0),
        ("Cargo.toml", ["[package]", 'name = "app"', "", "[dependencies]", 'smallvec = "=1.6.0"',
                        'serde = "1.0"', 'time = { version = "0.1.43" }'],
         [("crates.io", "smallvec", "1.6.0", 5), ("crates.io", "time", "0.1.43", 7)], 1),
        ("build.gradle", ["dependencies {", "    implementation 'org.apache.logging.log4j:log4j-core:2.14.1'", "}"],
         [("Maven", "org.apache.logging.log4j:log4j-core", "2.14.1", 2)], 0),
        ("package.json", ["{", '  "dependencies": {', '    "lodash": "^4.17.15",', '    "react": "latest"',
                          "  },", '  "devDependencies": {', '    "minimist": "~1.2.0"', "  },",
                          '  "scripts": {', '    "build": "1.2.3"', "  }", "}"],
         [("npm", "lodash", "4.17.15", 3), ("npm", "minimist", "1.2.0", 7)], 1),
    ])
    def test_each_manifest_kind_queries_concrete_pins_and_counts_the_rest(self, path, lines, pins, unpinned):
        [fd] = _files(_new_file(path, lines))
        got, skipped = changed_pins(fd, None)
        assert [(p.ecosystem, p.name, p.version, p.line) for p in got] == pins
        assert skipped == unpinned

    def test_a_removed_manifest_and_a_non_manifest_are_never_queried(self):
        removed = ("diff --git a/package.json b/package.json\ndeleted file mode 100644\n"
                   "--- a/package.json\n+++ /dev/null\n@@ -1,3 +0,0 @@\n"
                   '-{\n-  "dependencies": {"lodash": "4.17.15"}\n-}\n')
        result, osv = _scan(removed, APP_CHANGE)
        assert osv.queries == []
        assert result.record == {**vuln_scan.off_record(), "status": "ok"}

    def test_an_unpinned_change_is_counted_not_queried(self):
        result, osv = _scan(REQUIREMENTS_NEW)
        assert [q["package"]["name"] for q in osv.queries] == ["requests"]
        assert result.record["unpinned"] == 1


# ---------------------------------------- criterion 2: findings and the cap

class TestFindings:
    def test_one_finding_per_advisory_on_the_changed_line(self):
        result, _ = _scan(NPM_BUMP, REQUIREMENTS_NEW)
        got = [(f.file, f.line, f.severity, f.title.split(":")[1].split()[0]) for f in result.findings]
        assert got == [
            ("package.json", 3, "error", "GHSA-p6mc-m468-83gw"),
            ("requirements.txt", 1, "error", "GHSA-x84v-xcm2-53pg"),
            ("package.json", 3, "warning", "GHSA-29mw-wpgm-hmr9"),
        ]
        assert result.record == {"status": "ok", "reason": None, "manifests": 2, "queried": 2,
                                 "advisories": 3, "posted": 3, "capped": 0, "unpinned": 1}
        assert result.note == ""

    def test_aliased_records_collapse_to_one_advisory_named_by_its_ghsa_id(self):
        result, osv = _scan(REQUIREMENTS_NEW)
        [finding] = result.findings
        assert sorted(osv.fetched) == ["GHSA-x84v-xcm2-53pg", "PYSEC-2018-28"]
        assert finding.title == ("[HIGH] requests 2.19.0: GHSA-x84v-xcm2-53pg "
                                 "Insufficiently Protected Credentials in Requests")
        assert "GHSA-x84v-xcm2-53pg (CVE-2018-18074, PYSEC-2018-28)" in finding.body

    def test_the_body_carries_id_aliases_summary_fix_link_and_source(self):
        result, _ = _scan(NPM_BUMP)
        body = result.findings[0].body
        for part in ("GHSA-p6mc-m468-83gw", "CVE-2020-8203", "Prototype Pollution in lodash",
                     "Fixed in 4.17.19", "https://osv.dev/vulnerability/GHSA-p6mc-m468-83gw",
                     vuln_scan.SOURCE_LINE):
            assert part in body
        assert body.endswith(heuristics._BODY_SUFFIX)
        assert heuristics.is_deterministic(result.findings[0])
        assert vuln_scan.is_vuln_finding(result.findings[0])
        assert result.findings[0].confidence == 1.0

    @pytest.mark.parametrize(("word", "severity", "shown"), [
        ("CRITICAL", "error", "CRITICAL"), ("HIGH", "error", "HIGH"), ("MODERATE", "warning", "MODERATE"),
        ("MEDIUM", "warning", "MODERATE"), ("LOW", "warning", "LOW"), (None, "warning", "UNKNOWN"),
    ])
    def test_the_severity_map(self, word, severity, shown):
        record = {"id": "GHSA-aaaa-bbbb-cccc", "summary": "x",
                  **({"database_specific": {"severity": word}} if word else {})}
        osv = FakeOSV({("npm", "lodash", "4.17.15"): ["GHSA-aaaa-bbbb-cccc"]}, {"GHSA-aaaa-bbbb-cccc": record})
        result, _ = _scan(NPM_BUMP, osv=osv)
        [finding] = result.findings
        assert finding.severity == severity
        assert finding.title.startswith(f"[{shown}] ")

    def test_over_the_cap_the_most_severe_are_kept_and_the_rest_noted(self):
        result, _ = _scan(NPM_BUMP, REQUIREMENTS_NEW, vuln_max=2)
        assert [f.severity for f in result.findings] == ["error", "error"]
        assert result.record["advisories"] == 3
        assert result.record["posted"] == 2
        assert result.record["capped"] == 1
        assert result.note == (
            "> ⚠️ Dependency scan: 1 more known-vulnerability advisory matched this PR's changed "
            "dependencies but were not posted (PRXREF_VULN_MAX=2); the run record's vuln_scan lists "
            "the counts.\n\n"
        )

    def test_a_cap_of_zero_posts_nothing_but_still_counts(self):
        result, _ = _scan(NPM_BUMP, vuln_max=0)
        assert result.findings == []
        assert result.record["capped"] == 2

    def test_summary_text_is_neutralised(self):
        record = {"id": "GHSA-aaaa-bbbb-cccc", "summary": "Pwn @team via <script>",
                  "database_specific": {"severity": "LOW"}}
        osv = FakeOSV({("npm", "lodash", "4.17.15"): ["GHSA-aaaa-bbbb-cccc"]}, {"GHSA-aaaa-bbbb-cccc": record})
        [finding] = _scan(NPM_BUMP, osv=osv)[0].findings
        assert "@team" not in finding.body and "<script>" not in finding.body


class TestDeterministicFindingsSkipTheModelClaimGates:
    """The advisory text is OSV's: a claim-checking pass must not judge it.

    Each test runs the real pass on the scan's finding and on a copy without
    the deterministic suffix; the copy is the control that must be changed.
    """

    TEXT = ("Denial of service if the parser still sees a header, unless the session is removed; "
            "and the process can crash")

    def _pair(self):
        record = {"id": "GHSA-aaaa-bbbb-cccc", "summary": self.TEXT, "database_specific": {"severity": "HIGH"}}
        osv = FakeOSV({("npm", "lodash", "4.17.15"): ["GHSA-aaaa-bbbb-cccc"]}, {"GHSA-aaaa-bbbb-cccc": record})
        [vuln] = _scan(NPM_BUMP, osv=osv)[0].findings
        model = Finding(file=vuln.file, line=vuln.line, severity=vuln.severity, confidence=0.9,
                        title=vuln.title, body=vuln.body.removesuffix(heuristics._BODY_SUFFIX))
        return vuln, model

    def test_hedge_gate(self):
        vuln, model = self._pair()
        out = quality.apply_hedge_gate([vuln, model])
        assert out[0].drop_reason is None
        assert out[1].drop_reason and out[1].drop_reason.startswith("hedged:")

    def test_containment_note(self):
        vuln, model = self._pair()
        out = quality.apply_containment_note([vuln, model])
        assert out[0].body == vuln.body
        assert out[1].body == model.body + quality.CONTAINMENT_NOTE_SUFFIX

    def test_guard_fix_check(self):
        record = {"id": "GHSA-aaaa-bbbb-cccc", "database_specific": {"severity": "HIGH"},
                  "summary": "Mitigation: remove the null check, or use REQUIRES_NEW propagation"}
        osv = FakeOSV({("npm", "lodash", "4.17.15"): ["GHSA-aaaa-bbbb-cccc"]}, {"GHSA-aaaa-bbbb-cccc": record})
        [vuln] = _scan(NPM_BUMP, osv=osv)[0].findings
        model = Finding(file=vuln.file, line=vuln.line, severity=vuln.severity, confidence=0.9,
                        title=vuln.title, body=vuln.body.removesuffix(heuristics._BODY_SUFFIX))
        out, reasons = quality.apply_guard_fix_check([vuln, model], _files(NPM_BUMP))
        assert out[0] is vuln and reasons[0] == ()
        assert "Unverified fix:" in out[1].body and reasons[1]

    def test_manifest_claim_check(self):
        [vuln] = [f for f in _scan(NPM_BUMP)[0].findings if "p6mc" in f.title]
        moved = replace(vuln, line=4)
        model = replace(moved, confidence=0.9, body=moved.body.removesuffix(heuristics._BODY_SUFFIX))
        out = quality.apply_manifest_claim_check([moved, model], _files(NPM_BUMP))
        assert out[0] is moved
        assert out[1].drop_reason == "anchor mismatch: claims lodash but line 4 is express"

    def test_contained_claim_check(self):
        model = Finding(**CASE_ONE)
        deterministic = replace(model, body=model.body + heuristics._BODY_SUFFIX)
        out, reasons = apply_contained_claim_check([deterministic, model], parse_unified_diff(CASE_ONE_DIFF))
        assert out[0] is deterministic and reasons[0] is None
        assert reasons[1] == "null_return_guarded_by_caller" and out[1].severity == "outofscope"


# ------------------------------------------------ criterion 3: offline mode

class TestOfflineSkipsTheScan:
    @pytest.mark.parametrize(("kw", "reason"), [
        ({"status": 500, "body": {"error": "x"}}, "HTTP 500"),
        ({"status": 429, "body": {"error": "x"}}, "HTTP 429"),
        ({"body": "<html>not json"}, "JSONDecodeError"),
        ({"body": {"results": []}}, "bad response"),
        ({"body": {"nope": 1}}, "bad response"),
    ])
    def test_a_bad_answer_skips_the_whole_scan(self, kw, reason):
        result, _ = _scan(NPM_BUMP, osv=FakeOSV(**kw))
        assert result.findings == []
        assert result.record["status"] == "skipped"
        assert result.record["reason"] == reason
        assert result.record["queried"] == 1

    def test_no_network_skips_naming_the_error(self):
        def post(url, **kw):
            raise requests.ConnectionError("no route")
        result = scan(_files(NPM_BUMP), post=post, get=FakeOSV().get)
        assert (result.findings, result.record["status"], result.record["reason"]) == ([], "skipped", "ConnectionError")

    def test_a_failed_detail_fetch_skips_too(self):
        osv = FakeOSV(records={})
        result, _ = _scan(NPM_BUMP, osv=osv)
        assert (result.findings, result.record["reason"]) == ([], "HTTP 404")

    def test_the_deadline_skips(self):
        ticks = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0])
        result, _ = _scan(NPM_BUMP, clock=lambda: next(ticks))
        assert result.record["status"] == "skipped"
        assert result.record["reason"] == "deadline"

    def test_no_changed_pin_means_no_network_call(self):
        def boom(*a, **kw):
            raise AssertionError("no call expected")
        result = scan(_files(APP_CHANGE), post=boom, get=boom)
        assert result.record["status"] == "ok" and result.record["queried"] == 0


# --------------------------------------------- orchestrator and CLI, end to end

def _diff_forge(diff: str) -> FakeForge:
    return FakeForge(diff=diff)


class TestOrchestrator:
    def test_on_the_findings_post_and_the_record_counts_them(self, contract_stubs):
        osv = FakeOSV()
        with OSVServer(osv) as url:
            forge = _diff_forge(NPM_BUMP + REQUIREMENTS_NEW)
            res = orchestrate_review(forge, REF, FakeLLM(), vuln_scan="on", osv_url=url)
        vulns = [f for f in res["findings_active"] if vuln_scan.is_vuln_finding(f)]
        assert sorted((f.file, f.line, f.severity) for f in vulns) == [
            ("package.json", 3, "error"), ("package.json", 3, "warning"), ("requirements.txt", 1, "error"),
        ]
        assert res["vuln_scan"] == {"status": "ok", "reason": None, "manifests": 2, "queried": 2,
                                    "advisories": 3, "posted": 3, "capped": 0, "unpinned": 1}
        posted = [c for batch in forge.inline_batches for c in batch]
        assert sum("GHSA-" in c.body for c in posted) == 3

    def test_over_the_cap_the_summary_says_so(self, contract_stubs):
        with OSVServer(FakeOSV()) as url:
            forge = _diff_forge(NPM_BUMP + REQUIREMENTS_NEW)
            res = orchestrate_review(forge, REF, FakeLLM(), vuln_scan="on", osv_url=url, vuln_max=1)
        assert res["vuln_scan"]["capped"] == 2 and res["vuln_scan"]["posted"] == 1
        [summary] = forge.summaries
        assert "Dependency scan: 2 more known-vulnerability advisories" in summary

    def test_off_makes_no_call_and_records_off(self, contract_stubs):
        osv = FakeOSV()
        with OSVServer(osv) as url:
            res = orchestrate_review(_diff_forge(NPM_BUMP), REF, FakeLLM(), osv_url=url)
        assert osv.queries == []
        assert res["vuln_scan"] == vuln_scan.off_record()
        assert not any(vuln_scan.is_vuln_finding(f) for f in res["findings_active"])

    def test_unreachable_osv_skips_and_the_review_completes(self, contract_stubs):
        forge = _diff_forge(NPM_BUMP)
        res = orchestrate_review(forge, REF, FakeLLM(), vuln_scan="on", osv_url="http://127.0.0.1:9")
        assert res["vuln_scan"]["status"] == "skipped"
        assert res["vuln_scan"]["reason"] == "ConnectionError"
        assert res["verdict"] != "failed" and forge.summaries

    def test_a_review_skip_glob_keeps_the_manifest_out_of_the_scan(self, contract_stubs):
        osv = FakeOSV()
        with OSVServer(osv) as url:
            res = orchestrate_review(_diff_forge(NPM_BUMP + REQUIREMENTS_NEW), REF, FakeLLM(), vuln_scan="on",
                                     osv_url=url, review_skip_globs=("package.json",))
        assert [q["package"]["name"] for q in osv.queries] == ["requests"]
        assert res["vuln_scan"]["manifests"] == 1

    def test_the_no_chunk_exit_posts_the_scan_findings(self, contract_stubs):
        [vuln] = _scan(REQUIREMENTS_NEW)[0].findings
        files = _files(REQUIREMENTS_NEW)
        forge = _diff_forge(REQUIREMENTS_NEW)
        res = _summary_only_run(forge, REF, forge.pr, files, False, 0.0, vuln_findings=[vuln])
        assert res["findings_active"] == [vuln]

    def test_a_totally_failed_review_posts_no_finding_and_says_so(self, contract_stubs):
        with OSVServer(FakeOSV()) as url:
            res = orchestrate_review(_diff_forge(NPM_BUMP), REF, FakeLLM(error=RuntimeError("down")),
                                     vuln_scan="on", osv_url=url)
        assert res["verdict"] == "Error" and res["findings_active"] == []
        assert res["vuln_scan"]["advisories"] == 2 and res["vuln_scan"]["posted"] == 0

    def test_a_bad_mode_is_refused(self, contract_stubs):
        with pytest.raises(ValueError):
            orchestrate_review(_diff_forge(NPM_BUMP), REF, FakeLLM(), vuln_scan="maybe")


@pytest.fixture(scope="module")
def llm_server():
    server = MockOpenAIServer(routes={"fast": _completion(json.dumps({"findings": []}), "stop")})
    yield server.start()
    server.stop()


@pytest.fixture
def cli_review(monkeypatch, llm_server, tmp_path):
    """Run the production local path, ``--diff-file``, and return its JSON payload."""

    def _run(diff: str, **env: str) -> dict:
        path = tmp_path / "pr.patch"
        path.write_text(diff)
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "openai-compat")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", llm_server)
        monkeypatch.setenv("PRXREF_LLM_MODELS", "fast")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return json.loads(json.dumps(cli._build_json_result(cli._run_review(None, diff_file=str(path)))))

    return _run


class TestCLI:
    def test_on_through_the_env(self, cli_review):
        osv = FakeOSV()
        with OSVServer(osv) as url:
            payload = cli_review(NPM_BUMP + REQUIREMENTS_NEW, PRXREF_VULN_SCAN="on", PRXREF_OSV_URL=url)
        vulns = [f for f in payload["findings"] if vuln_scan.SOURCE_LINE in f["body"]]
        assert len(vulns) == 3
        assert payload["vuln_scan"]["status"] == "ok" and payload["vuln_scan"]["posted"] == 3

    def test_the_default_is_off(self, cli_review):
        osv = FakeOSV()
        with OSVServer(osv) as url:
            payload = cli_review(NPM_BUMP, PRXREF_OSV_URL=url)
        assert payload["vuln_scan"]["status"] == "off" and osv.queries == []

    def test_the_eval_pins_turn_it_off(self, cli_review):
        assert EVAL_PINS["vuln_scan"] == "off"
        osv = FakeOSV()
        with OSVServer(osv) as url:
            payload = cli_review(NPM_BUMP, PRXREF_VULN_SCAN="on", PRXREF_OSV_URL=url, PRXREF_EVAL_PINS="on")
        assert payload["vuln_scan"]["status"] == "off" and osv.queries == []
        assert payload["eval_pins"]["vuln_scan"] == "off"

    def test_unreachable_is_skipped_not_failed(self, cli_review):
        payload = cli_review(NPM_BUMP, PRXREF_VULN_SCAN="on", PRXREF_OSV_URL="http://127.0.0.1:9")
        assert payload["vuln_scan"]["status"] == "skipped"
        assert payload["verdict"] != "failed"


# ------------------------------------------------------------------- config

class TestConfig:
    def test_defaults(self, monkeypatch):
        for key in ("PRXREF_VULN_SCAN", "PRXREF_OSV_URL", "PRXREF_VULN_MAX"):
            monkeypatch.delenv(key, raising=False)
        cfg = config.load_config()
        assert (cfg["vuln_scan"], cfg["osv_url"], cfg["vuln_max"]) == ("off", "https://api.osv.dev", 10)

    @pytest.mark.parametrize(("key", "value", "names"), [
        ("PRXREF_VULN_SCAN", "yes", "PRXREF_VULN_SCAN"),
        ("PRXREF_OSV_URL", "api.osv.dev", "PRXREF_OSV_URL"),
        ("PRXREF_OSV_URL", "file:///etc/passwd", "PRXREF_OSV_URL"),
        ("PRXREF_VULN_MAX", "-1", "PRXREF_VULN_MAX"),
        ("PRXREF_VULN_MAX", "ten", "PRXREF_VULN_MAX"),
    ])
    def test_a_bad_value_is_a_config_error_naming_the_variable(self, monkeypatch, key, value, names):
        monkeypatch.setenv(key, value)
        with pytest.raises(config.ConfigError, match=names):
            config.load_config()

    def test_the_scan_and_its_host_are_env_only(self):
        assert {"vuln_scan", "osv_url"} <= config.ENV_ONLY_KEYS
        assert "vuln_max" in config.FILE_KEYS

    def test_a_repo_file_cannot_turn_the_scan_on(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PRXREF_VULN_SCAN", raising=False)
        path = tmp_path / ".prxref.toml"
        path.write_text('vuln_scan = "on"\n')
        with pytest.raises(config.ConfigError):
            config.load_config(config_file=path)

    def test_a_repo_file_can_set_the_cap(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PRXREF_VULN_MAX", raising=False)
        path = tmp_path / ".prxref.toml"
        path.write_text("vuln_max = 3\n")
        assert config.load_config(config_file=path)["vuln_max"] == 3
