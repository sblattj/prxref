"""Issue #146, default on: the default-branch check runs unless it is turned off.

``PRXREF_DEFAULT_BRANCH_CHECK`` defaults to ``on``. These tests drive the CLI's
``_run_review`` with the variable unset and prove the check ran; ``off`` (env or
``.prxref.toml``) still opts out with zero forge calls. A run that cannot
answer (a diff file, a forge without the optional methods, a failed or empty
default-branch lookup, no ``--repo-dir``) must leave the findings exactly as an
``off`` run does: no crash, no extra findings, a run-record entry at most.
"""
from __future__ import annotations

import logging

import pytest

from prxref import cli, config, llm_backends
from prxref.config import load_config
from prxref.orchestrator import orchestrate_review
from tests.test_issue_146_default_branch_check import (
    API,
    ENV,
    BranchForge,
    NoMethodsForge,
    raw,
    route_diff,
)
from tests.test_orchestrator import REF, FakeLLM

URL = "https://github.com/acme/widget/pull/7"


def cli_run(monkeypatch, forge, *, url=URL, findings=None, **kw):
    llm = FakeLLM({API: findings if findings is not None else [raw()]})
    monkeypatch.setattr(llm_backends, "create_llm_client", lambda cfg: llm)
    monkeypatch.setattr(cli, "make_forge", lambda ref: pytest.fail("make_forge must not run"))
    return cli._run_review(url, forge=forge, post=False, **kw)


def view(res):
    return [(f.file, f.line, f.severity, f.title) for f in res["findings_active"]]


@pytest.fixture(autouse=True)
def _unset(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


class TestDefaultIsOn:
    def test_config_default_is_on(self):
        assert config._DEFAULTS["default_branch_check"] == "on"
        assert load_config()["default_branch_check"] == "on"

    @pytest.mark.usefixtures("contract_stubs")
    def test_cli_run_with_the_variable_unset_runs_the_check(self, monkeypatch):
        forge = BranchForge(diff=route_diff(API), files={})
        res = cli_run(monkeypatch, forge)
        assert forge.default_calls == 1
        assert forge.reads == [(API, "main")]
        assert res["default_branch_check"]["lowered"] == 1
        (f,) = res["findings_active"]
        assert f.severity == "outofscope"

    @pytest.mark.usefixtures("contract_stubs")
    def test_orchestrate_review_runs_the_check_when_the_argument_is_left_out(self):
        forge = BranchForge(diff=route_diff(API), files={})
        res = orchestrate_review(forge, REF, FakeLLM({API: [raw()]}), post=True, max_workers=1)
        assert forge.default_calls == 1
        assert res["default_branch_check"]["lowered"] == 1


@pytest.mark.usefixtures("contract_stubs")
class TestOffStillOptsOut:
    def test_env_off_makes_zero_forge_calls(self, monkeypatch):
        monkeypatch.setenv(ENV, "off")
        forge = BranchForge(diff=route_diff(API), files={})
        res = cli_run(monkeypatch, forge)
        assert forge.default_calls == 0 and forge.reads == []
        assert "default_branch_check" not in res
        (f,) = res["findings_active"]
        assert f.severity == "error"

    def test_prxref_toml_off_makes_zero_forge_calls(self, monkeypatch, tmp_path):
        path = tmp_path / ".prxref.toml"
        path.write_text('default_branch_check = "off"\n')
        forge = BranchForge(diff=route_diff(API), files={})
        res = cli_run(monkeypatch, forge, config_file=path)
        assert forge.default_calls == 0 and forge.reads == []
        assert "default_branch_check" not in res


def off_view(monkeypatch, forge_factory, **kw):
    monkeypatch.setenv(ENV, "off")
    res = cli_run(monkeypatch, forge_factory(), **kw)
    monkeypatch.delenv(ENV)
    return view(res)


@pytest.mark.usefixtures("contract_stubs")
class TestDegradesQuietly:
    """Each case: default (on) gives the same findings as an explicit off run."""

    def _same_as_off(self, monkeypatch, caplog, forge_factory, **kw):
        off = off_view(monkeypatch, forge_factory, **kw)
        forge = forge_factory()
        with caplog.at_level(logging.WARNING):
            res = cli_run(monkeypatch, forge, **kw)
        assert view(res) == off
        assert all(f.severity == "error" for f in res["findings_active"])
        return res, forge

    def test_forge_without_the_optional_methods(self, monkeypatch, caplog):
        res, _ = self._same_as_off(
            monkeypatch, caplog, lambda: NoMethodsForge(diff=route_diff(API)),
        )
        assert res["default_branch_check"]["branch"] is None
        assert res["default_branch_check"]["checked"] == 0

    def test_default_branch_unknown_as_on_a_401(self, monkeypatch, caplog):
        res, forge = self._same_as_off(
            monkeypatch, caplog, lambda: BranchForge(diff=route_diff(API), default=None),
        )
        assert forge.default_calls == 1 and forge.reads == []
        assert res["default_branch_check"]["branch"] is None

    def test_default_branch_lookup_raises(self, monkeypatch, caplog):
        res, forge = self._same_as_off(
            monkeypatch, caplog,
            lambda: BranchForge(diff=route_diff(API), default_raises=True),
        )
        assert forge.reads == []
        assert res["default_branch_check"]["branch"] is None

    def test_pr_targets_the_default_branch(self, monkeypatch, caplog):
        res, forge = self._same_as_off(
            monkeypatch, caplog, lambda: BranchForge(diff=route_diff(API), target="main"),
        )
        assert forge.reads == []
        assert res["default_branch_check"]["stacked"] is False

    def test_every_read_fails(self, monkeypatch, caplog):
        res, _ = self._same_as_off(
            monkeypatch, caplog, lambda: BranchForge(diff=route_diff(API), raises=True),
        )
        assert res["default_branch_check"]["lowered"] == 0

    def test_a_repo_dir_that_is_not_a_git_checkout(self, monkeypatch, caplog, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "api.py").write_text('import web\n@route("/v2/widgets")\n')
        res, forge = self._same_as_off(
            monkeypatch, caplog,
            lambda: BranchForge(diff=route_diff(API), files={API: (True, None)}),
            repo_dir=str(tmp_path),
        )
        assert res["default_branch_check"]["refs"] == []

    def test_a_diff_file_run_has_no_repository_to_ask(self, monkeypatch, caplog, tmp_path):
        diff = tmp_path / "pr.diff"
        diff.write_text(route_diff(API))
        llm = FakeLLM({API: [raw()]})
        monkeypatch.setattr(llm_backends, "create_llm_client", lambda cfg: llm)
        monkeypatch.setenv(ENV, "off")
        off = view(cli._run_review(None, diff_file=str(diff)))
        monkeypatch.delenv(ENV)
        llm = FakeLLM({API: [raw()]})
        with caplog.at_level(logging.WARNING):
            res = cli._run_review(None, diff_file=str(diff))
        assert view(res) == off
        assert res["default_branch_check"]["branch"] is None
        assert not [r for r in caplog.records if "default branch" in r.getMessage()]
