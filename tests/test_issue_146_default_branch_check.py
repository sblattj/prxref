"""Issue #146, second half: lower versioning findings on stacked PRs.

The failure case: a PR stacked on another PR gets "moved URIs need a new API
version" about a route that never existed on the default branch. With
``PRXREF_DEFAULT_BRANCH_CHECK=on`` the finding's own file is read at the default
branch and the finding is lowered to the minor tier (``outofscope``). Every
orchestrator test runs the real pipeline with a scripted model and a duck-typed
forge that records its reads.
"""
from __future__ import annotations

import pytest

from prxref import config
from prxref.cli import _build_json_result, _fail_on_exit
from prxref.config import load_config
from prxref.default_branch import COMPAT_RE, is_compat_finding
from prxref.llm import ConfigError
from prxref.orchestrator import orchestrate_review
from prxref.triage import Finding
from tests.test_orchestrator import REF, FakeForge, FakeLLM, make_pr

ENV = "PRXREF_DEFAULT_BRANCH_CHECK"
API = "src/api.py"
NOTE_A = (
    "Checked against main: this file is not there, so no released client should "
    "depend on it. If it moved earlier in this stack, this check cannot tell. "
    "Docs that mention it may still need updating."
)
NOTE_B = (
    "Checked against main: what this finding names is not in this file there, so no "
    "released client should depend on it. If it moved earlier in this stack, this "
    "check cannot tell. Docs that mention it may still need updating."
)
PRESENT = "Checked against main: still present there."


def route_diff(path: str, route: str = "/v1/widgets", new: str = "/v2/widgets") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,5 +1,5 @@\n"
        " import web\n"
        " \n"
        f'-@route("{route}")\n'
        f'+@route("{new}")\n'
        " def widgets():\n"
        "     return data\n"
    )


def raw(file=API, severity="error", title="Moved URIs need a new API version",
        body="`/v1/widgets` was taken away here and clients still call it; `data` shows no versioned route.",
        line=4):
    return {
        "file": file, "line": line, "severity": severity, "confidence": 0.9,
        "title": title, "body": body,
    }


class BranchForge(FakeForge):
    """A forge with the two optional #146 methods, recording every call."""

    def __init__(self, *, diff, target="stack/base", default="main", files=None,
                 raises=False, default_raises=False):
        pr = make_pr()
        pr.target_branch = target
        super().__init__(pr=pr, diff=diff)
        self.default = default
        self.files = files or {}
        self.raises = raises
        self.default_raises = default_raises
        self.default_calls = 0
        self.reads: list[tuple[str, str]] = []

    def get_default_branch(self, ref):
        self.default_calls += 1
        if self.default_raises:
            raise RuntimeError("boom default")
        return self.default

    def get_file_at_branch(self, ref, path, branch):
        self.reads.append((path, branch))
        if self.raises:
            raise RuntimeError("boom read")
        return self.files.get(path, (False, None))


class NoMethodsForge(FakeForge):
    """A forge that predates the optional methods."""


def run(forge, findings=None, *, mode="on", **kw):
    llm = FakeLLM({API: findings if findings is not None else [raw()]})
    res = orchestrate_review(
        forge, REF, llm, post=True, max_workers=1, default_branch_check=mode, **kw,
    )
    return res


def only(res):
    (f,) = res["findings_active"]
    return f


@pytest.mark.usefixtures("contract_stubs")
class TestTheIssueCases:
    def test_surface_absent_on_default_is_lowered_with_variant_b(self):
        forge = BranchForge(diff=route_diff(API), files={API: (True, 'import web\n@route("/v2/widgets")\n')})
        res = run(forge)
        f = only(res)
        assert f.severity == "outofscope"
        assert f.body.endswith(NOTE_B)
        assert f.drop_reason is None
        rec = res["default_branch_check"]
        assert rec["lowered"] == 1 and rec["stacked"] is True and rec["branch"] == "main"
        assert rec["lowered_findings"] == [
            {"file": API, "reason": "named surface absent from file on default branch"},
        ]
        assert forge.reads == [(API, "main")]

    def test_file_absent_on_default_is_lowered_with_variant_a(self):
        forge = BranchForge(diff=route_diff(API), files={})
        res = run(forge)
        f = only(res)
        assert f.severity == "outofscope"
        assert f.body.endswith(NOTE_A)
        assert res["default_branch_check"]["lowered_findings"][0]["reason"] == "file absent on default branch"

    def test_the_issue_control_route_on_default_keeps_the_error(self):
        forge = BranchForge(diff=route_diff(API), files={API: (True, '@route("/v1/widgets")\n')})
        res = run(forge)
        f = only(res)
        assert f.severity == "error"
        assert f.body.endswith(PRESENT)
        assert res["verdict"] == "Request-Changes"
        rec = res["default_branch_check"]
        assert (rec["present"], rec["lowered"], rec["checked"]) == (1, 0, 1)

    def test_a_lowered_finding_no_longer_makes_the_verdict_request_changes(self):
        res = run(BranchForge(diff=route_diff(API), files={}))
        assert res["verdict"] == "Approved"

    def test_a_lowered_finding_is_posted_inline_with_the_note(self):
        forge = BranchForge(diff=route_diff(API), files={})
        run(forge)
        comments = [c for batch in forge.inline_batches for c in batch]
        assert len(comments) == 1 and NOTE_A in comments[0].body


@pytest.mark.usefixtures("contract_stubs")
class TestNothingHappens:
    def test_not_stacked_makes_no_reads_and_changes_nothing(self):
        forge = BranchForge(diff=route_diff(API), target="main", files={})
        res = run(forge)
        f = only(res)
        assert f.severity == "error" and "Checked against" not in f.body
        assert forge.reads == []
        assert res["default_branch_check"]["stacked"] is False

    def test_a_read_that_returns_unknown_leaves_the_finding_alone(self):
        forge = BranchForge(diff=route_diff(API), files={API: (True, None)})
        res = run(forge)
        f = only(res)
        assert f.severity == "error" and "Checked against" not in f.body
        assert res["default_branch_check"]["unknown"] == 1

    def test_a_read_that_raises_leaves_the_finding_alone_and_the_review_completes(self):
        forge = BranchForge(diff=route_diff(API), raises=True)
        res = run(forge)
        f = only(res)
        assert f.severity == "error" and "Checked against" not in f.body
        assert res["verdict"] == "Request-Changes"
        assert res["default_branch_check"]["unknown"] == 1

    def test_an_unknown_default_branch_reads_nothing(self):
        forge = BranchForge(diff=route_diff(API), default=None)
        res = run(forge)
        assert only(res).severity == "error"
        assert forge.reads == []

    def test_a_default_branch_lookup_that_raises_is_ignored(self):
        forge = BranchForge(diff=route_diff(API), default_raises=True)
        res = run(forge)
        assert only(res).severity == "error"
        assert forge.reads == []

    def test_a_forge_without_the_methods_is_unchanged(self):
        forge = NoMethodsForge(diff=route_diff(API))
        res = run(forge)
        f = only(res)
        assert f.severity == "error" and "Checked against" not in f.body
        assert res["default_branch_check"]["stacked"] is False

    def test_a_sql_finding_is_untouched_and_costs_no_read(self):
        sql = raw(
            title="SQL injection through `route` argument",
            body="The query concatenates `/v1/widgets` input into the statement; `data` is unsanitized.",
        )
        forge = BranchForge(diff=route_diff(API), files={})
        res = run(forge, [sql])
        f = only(res)
        assert f.severity == "error" and "Checked against" not in f.body
        assert forge.reads == []

    def test_a_finding_that_names_no_removed_surface_is_unchanged(self):
        bare = raw(body="This is a breaking change for callers of the widget service; `data` is affected.")
        forge = BranchForge(diff=route_diff(API), files={API: (True, "other text\n")})
        res = run(forge, [bare])
        f = only(res)
        assert f.severity == "error" and "Checked against" not in f.body
        assert res["default_branch_check"]["unknown"] == 1

    def test_a_spec_finding_is_not_a_candidate(self):
        spec = raw(severity="outofscope")
        forge = BranchForge(diff=route_diff(API), files={})
        run(forge, [spec])
        assert forge.reads == []


@pytest.mark.usefixtures("contract_stubs")
class TestOff:
    def test_off_makes_zero_calls_and_adds_no_record_key(self):
        forge = BranchForge(diff=route_diff(API), files={})
        res = run(forge, mode="off")
        assert forge.default_calls == 0 and forge.reads == []
        assert only(res).severity == "error"
        assert "default_branch_check" not in res
        assert "default_branch_check" not in _build_json_result(res)

    def test_off_output_is_identical_to_the_argument_left_out(self):
        findings = [raw()]
        a = orchestrate_review(
            BranchForge(diff=route_diff(API), files={}), REF, FakeLLM({API: findings}),
            post=True, max_workers=1, default_branch_check="off",
        )
        b = orchestrate_review(
            BranchForge(diff=route_diff(API), files={}), REF, FakeLLM({API: findings}),
            post=True, max_workers=1,
        )
        assert a["findings_active"] == b["findings_active"]
        assert {k for k in a if k != "elapsed_ms"} == {k for k in b if k != "elapsed_ms"}

    def test_on_adds_the_key_to_the_json_result(self):
        res = run(BranchForge(diff=route_diff(API), files={}))
        assert _build_json_result(res)["default_branch_check"]["lowered"] == 1

    def test_a_bad_mode_is_refused(self):
        with pytest.raises(ValueError, match="default_branch_check"):
            run(BranchForge(diff=route_diff(API)), mode="maybe")


@pytest.mark.usefixtures("contract_stubs")
class TestReadBudget:
    def test_over_twenty_paths_reads_exactly_twenty_and_counts_the_rest_unknown(self):
        names = [f"w{chr(97 + i)}" for i in range(22)]
        paths = [f"src/{n}.py" for n in names]
        diff = "".join(route_diff(p, route=f"/v1/{n}", new=f"/v2/{n}") for p, n in zip(paths, names, strict=True))
        by_path = {
            p: [raw(
                file=p, severity="warning", title=f"Moved URIs for {n} need a new API version",
                body=f"`/v1/{n}` was removed in `{p}`; `data` shows no versioned route for {n}.",
            )]
            for p, n in zip(paths, names, strict=True)
        }
        forge = BranchForge(diff=diff, files={p: (True, "nothing\n") for p in paths})
        res = orchestrate_review(
            forge, REF, FakeLLM(by_path), post=False, max_workers=1,
            default_branch_check="on", max_chunks=40,
        )
        rec = res["default_branch_check"]
        assert len(forge.reads) == 20 and len({p for p, _ in forge.reads}) == 20
        assert rec["lowered"] == 20 and rec["unknown"] == 2
        severities = sorted(f.severity for f in res["findings_active"])
        assert severities.count("outofscope") == 20 and severities.count("warning") == 2

    def test_the_same_path_twice_is_one_read(self):
        second = raw(
            title="Backwards compatibility of the `/v1/widgets` route is gone",
            body="Clients on `/v1/widgets` break; `data` is no longer served there.", line=3,
        )
        forge = BranchForge(diff=route_diff(API), files={API: (True, "nothing\n")})
        run(forge, [raw(), second])
        assert forge.reads == [(API, "main")]


@pytest.mark.usefixtures("contract_stubs")
class TestFailOn:
    def test_a_lowered_finding_does_not_fail_and_a_kept_one_does(self):
        lowered = run(BranchForge(diff=route_diff(API), files={}))
        kept = run(BranchForge(diff=route_diff(API), files={API: (True, '@route("/v1/widgets")')}))
        assert _fail_on_exit(lowered, "error")[0] == 0
        assert _fail_on_exit(kept, "error")[0] == 1


def finding(title, body=""):
    return Finding(file=API, line=1, severity="error", confidence=0.9, title=title, body=body)


class TestCompatPattern:
    @pytest.mark.parametrize("title, body", [
        ("Moved URIs need a new API version", ""),
        ("This is a breaking change for clients", ""),
        ("Backward compatibility is lost", ""),
        ("Backwards-compatibility of the handler", ""),
        ("Bump the API version", ""),
        ("Versioned route missing", ""),
        ("Removed the /v1/widgets endpoint", ""),
        ("Renamed route breaks callers", ""),
        ("Public API removed without notice", ""),
        ("Keep the old path or version it", ""),
        ("Handler change", "The old route was removed, a new API version is required."),
    ])
    def test_matches(self, title, body):
        assert is_compat_finding(finding(title, body))

    @pytest.mark.parametrize("title, body", [
        ("Removed null check", "The guard on `user` was removed, so a None crashes."),
        ("SQL injection in query builder", "The statement concatenates input."),
        ("Missing await", "The coroutine is never awaited."),
        ("Version string parsed twice", "The file reads the config version two times."),
        ("Variable shadows import", "The name `route` shadows the import."),
    ])
    def test_does_not_match(self, title, body):
        assert not is_compat_finding(finding(title, body))

    def test_is_case_insensitive(self):
        assert COMPAT_RE.search("BREAKING CHANGE")


class TestConfig:
    def test_default_is_off(self):
        assert load_config()["default_branch_check"] == "off"

    @pytest.mark.parametrize("value", ["off", "on"])
    def test_env_value_loads(self, monkeypatch, value):
        monkeypatch.setenv(ENV, value)
        assert load_config()["default_branch_check"] == value

    @pytest.mark.parametrize("value", ["yes", "ON", "true", "1"])
    def test_a_bad_value_exits_2_naming_the_variable(self, monkeypatch, value):
        monkeypatch.setenv(ENV, value)
        with pytest.raises(ConfigError, match=ENV):
            load_config()

    def test_a_prxref_toml_value_is_accepted(self, tmp_path):
        path = tmp_path / ".prxref.toml"
        path.write_text('default_branch_check = "on"\n')
        assert load_config(config_file=path)["default_branch_check"] == "on"

    def test_it_is_a_file_key_not_env_only(self):
        assert "default_branch_check" in config.FILE_KEYS
        assert "default_branch_check" not in config.ENV_ONLY_KEYS
