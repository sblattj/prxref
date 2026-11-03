"""Issue #145: every ticket id in the PR title is loaded, and a partial read never yields ``out``.

``PRXREF_TICKET_IDS_FROM_TITLE=on`` fetches each Jira key in the PR title
through the spec-source Jira path, joins the texts as the ticket context under
``PRXREF_TICKET_CONTEXT_MAX_CHARS``, lists them in the summary, and records
them in the run record's ``ticket_context``. Whatever the key says, a finding
the model marks ``out`` of a ticket that was only partly read (a listed ticket
could not be fetched, or the context was cut at the cap) is recorded as
``unknown``.

The Jira side is faked at the session the spec fetcher builds, so the real
fetch code runs and no test touches the network. Most tests use the REAL
reviewer prompts with a recording LLM; one goes through ``orchestrate_review``
with ``FakeLLM`` and the ``contract_stubs`` fixture.
"""
from __future__ import annotations

import pytest

from prxref import cli, config, orchestrator, specs
from prxref.llm import ConfigError
from prxref.text_inputs import cap_text
from prxref.ticket import NOTE_EMPTY, NOTE_NO_AC, TicketContext
from tests.test_issue_64_ticket_context import FINDING, _RecordingLLM
from tests.test_orchestrator import REF, FakeForge, FakeLLM, _added_file_diff, make_pr
from tests.test_specs import _FakeResponse

JIRA = "https://jira.example.com"
TITLE = "[T-1][T-2][T-3] docs: spec update"
TEXTS = {
    "T-1": "Alpha ticket asks for the first widget.",
    "T-2": "Bravo ticket asks for the reply id replacement.",
    "T-3": "Charlie ticket asks for the third widget.",
}
OUT_FINDING = {**FINDING, "scope": "out"}
IN_FINDING = {**FINDING, "scope": "in"}


class JiraRouter:
    """Answers Jira REST issue GETs by key; ``fail`` keys get a 500."""

    def __init__(self, texts=None, fail=()):
        self.texts = dict(TEXTS if texts is None else texts)
        self.fail = set(fail)
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        key = url.split("/issue/")[1].split("?")[0]
        if key in self.fail or key not in self.texts:
            return _FakeResponse(status_code=500, content_type="application/json", payload={})
        payload = {"fields": {"summary": f"Summary of {key}", "description": self.texts[key]}}
        return _FakeResponse(content_type="application/json", payload=payload)


@pytest.fixture
def jira(monkeypatch):
    router = JiraRouter()
    monkeypatch.setattr(specs, "_create_default_session", lambda: router)
    return router


@pytest.fixture
def no_http(monkeypatch):
    calls: list[str] = []

    def boom():
        calls.append("session")
        raise AssertionError("no HTTP session may be built when the key is off")

    monkeypatch.setattr(specs, "_create_default_session", boom)
    return calls


def _run(title=TITLE, *, mode="on", findings=None, ticket=None, max_chars=6000, **kw):
    forge = FakeForge(pr=make_pr(title), diff=_added_file_diff("src/app.py", 20))
    llm = _RecordingLLM(findings)
    if mode is not None:
        kw["ticket_ids_from_title"] = mode
    kw.setdefault("jira_base_url", JIRA)
    res = orchestrator.orchestrate_review(
        forge, REF, llm, ticket=ticket, ticket_context_max_chars=max_chars, **kw,
    )
    return forge, llm, res


def _worker_user(llm) -> str:
    return llm.units()["worker"][0][1]


def _scopes(res) -> list[str]:
    return [f.scope for f in res["findings_active"]] if not isinstance(
        res["findings_active"][0], dict
    ) else [f["scope"] for f in res["findings_active"]]


class TestTitleIds:
    def test_ids_in_order_without_duplicates(self):
        assert specs.title_ticket_ids("[T-1][T-2][T-1] fix AB_C-30") == ("T-1", "T-2", "AB_C-30")

    def test_an_id_glued_to_a_word_is_not_one(self):
        assert specs.title_ticket_ids("iOS-14 support, fooT-1, T-1x, T-") == ()

    def test_no_title_no_ids(self):
        assert specs.title_ticket_ids("") == ()
        assert specs.title_ticket_ids("docs: update the spec") == ()


class TestAcceptance:
    def test_all_three_tickets_reach_the_prompt_and_the_footer(self, jira):
        forge, llm, res = _run(findings=[IN_FINDING])
        user = _worker_user(llm)
        for key, text in TEXTS.items():
            assert text in user
            assert key in user
        (summary,) = forge.summaries
        assert "T-1, T-2, T-3" in summary
        assert "could not be read" not in summary
        assert len(jira.calls) == 3
        assert all(url.startswith(f"{JIRA}/rest/api/2/issue/T-") for url in jira.calls)
        assert res["ticket_context"]["ids_loaded"] == ["T-1", "T-2", "T-3"]
        assert res["ticket_context"]["ids_failed"] == []

    def test_a_failed_ticket_is_named_and_out_becomes_unknown(self, jira):
        jira.fail = {"T-2"}
        forge, llm, res = _run(findings=[OUT_FINDING])
        assert res["verdict"] != "Error"
        (summary,) = forge.summaries
        assert "T-1, T-3" in summary
        assert "T-2 could not be read" in summary
        assert _scopes(res) == ["unknown"]
        assert TEXTS["T-2"] not in _worker_user(llm)
        assert res["ticket_context"]["ids_failed"] == ["T-2"]
        assert res["ticket_context"]["ids_loaded"] == ["T-1", "T-3"]

    def test_a_failed_ticket_keeps_an_in_finding_in(self, jira):
        jira.fail = {"T-2"}
        _, _, res = _run(findings=[IN_FINDING])
        assert _scopes(res) == ["in"]

    def test_every_ticket_read_keeps_out_as_out(self, jira):
        _, _, res = _run(findings=[OUT_FINDING])
        assert _scopes(res) == ["out"]

    def test_a_truncated_context_turns_out_into_unknown_and_shows_the_marker(self, jira):
        jira.texts = {k: f"{k} " + "long body " * 40 for k in TEXTS}
        forge, llm, res = _run(findings=[OUT_FINDING], max_chars=300)
        assert "[ticket context truncated" in _worker_user(llm)
        assert _scopes(res) == ["unknown"]
        assert res["ticket_context"]["truncated"] is True
        (summary,) = forge.summaries
        assert "cut at 300 of" in summary

    def test_every_fetch_failing_still_completes_and_says_so(self, jira):
        jira.fail = set(TEXTS)
        forge, llm, res = _run(findings=[OUT_FINDING])
        assert res["verdict"] != "Error"
        (summary,) = forge.summaries
        assert "T-1, T-2, T-3 could not be read" in summary
        assert _scopes(res) == ["unknown"]
        assert "### Ticket context" not in _worker_user(llm)

    def test_a_fetch_that_raises_is_a_failed_ticket_not_a_failed_review(self, monkeypatch):
        class Down:
            def get(self, url, **kwargs):
                raise OSError("connection refused")

        monkeypatch.setattr(specs, "_create_default_session", lambda: Down())
        forge, _, res = _run(findings=[OUT_FINDING])
        assert res["verdict"] != "Error"
        assert "could not be read" in forge.summaries[0]
        assert _scopes(res) == ["unknown"]

    def test_the_ticket_file_comes_first_and_is_named_without_its_path(self, jira, tmp_path):
        path = tmp_path / "ticket.md"
        path.write_text("FILE-TICKET-BODY\n", encoding="utf-8")
        from prxref.ticket import load_ticket_context

        base = load_ticket_context(str(path), max_chars=6000, source="--context-file")
        forge, llm, res = _run(findings=[IN_FINDING], ticket=base)
        user = _worker_user(llm)
        assert user.index("FILE-TICKET-BODY") < user.index(TEXTS["T-1"])
        (summary,) = forge.summaries
        assert "the ticket file" in summary
        assert str(tmp_path) not in summary
        assert res["ticket_context"]["ids_loaded"] == ["T-1", "T-2", "T-3"]

    def test_an_id_less_title_with_the_key_on_changes_nothing(self, no_http):
        forge, llm, res = _run("docs: update the spec", findings=[IN_FINDING])
        assert no_http == []
        assert res["ticket_context"] is None
        assert "### Ticket context" not in _worker_user(llm)
        assert "Ticket context:" not in forge.summaries[0]

    def test_a_one_id_title(self, jira):
        forge, llm, res = _run("[T-2] docs: spec update", findings=[OUT_FINDING])
        assert jira.calls and len(jira.calls) == 1
        assert TEXTS["T-2"] in _worker_user(llm)
        assert "T-2" in forge.summaries[0]
        assert _scopes(res) == ["out"]


class TestKeyOff:
    def test_off_fetches_nothing_and_prompts_are_byte_identical(self, no_http):
        f1, l1, r1 = _run(mode=None, findings=[IN_FINDING])
        f2, l2, r2 = _run(mode="off", findings=[IN_FINDING])
        assert no_http == []
        assert l1.prompts == l2.prompts
        assert r1["ticket_context"] is None and r2["ticket_context"] is None
        assert f1.summaries[0].split("\n\n")[:3] == f2.summaries[0].split("\n\n")[:3]

    def test_a_truncated_file_ticket_loses_out_even_with_the_key_off(self, tmp_path):
        from prxref.ticket import load_ticket_context

        path = tmp_path / "ticket.md"
        path.write_text("word " * 200, encoding="utf-8")
        base = load_ticket_context(str(path), max_chars=100, source="--context-file")
        assert base.record()["truncated"] is True
        _, _, res = _run(mode=None, findings=[OUT_FINDING], ticket=base)
        assert _scopes(res) == ["unknown"]
        assert "ids_loaded" not in res["ticket_context"]

    def test_an_untruncated_file_ticket_keeps_out(self, tmp_path):
        from prxref.ticket import load_ticket_context

        path = tmp_path / "ticket.md"
        path.write_text("short ticket\n", encoding="utf-8")
        base = load_ticket_context(str(path), max_chars=100, source="--context-file")
        _, _, res = _run(mode=None, findings=[OUT_FINDING], ticket=base)
        assert _scopes(res) == ["out"]


class TestTicketContextObject:
    def _ctx(self, **kw):
        text = "body"
        return TicketContext(
            path="", capped=cap_text(text, 100), text=text, has_acceptance_criteria=True, **kw,
        )

    def test_complete_is_false_for_a_failed_id_or_a_cut(self):
        assert self._ctx(ids_loaded=("T-1",)).complete is True
        assert self._ctx(ids_loaded=("T-1",), ids_failed=("T-2",)).complete is False
        cut = TicketContext(
            path="", capped=cap_text("x" * 50, 10), text="x" * 10, has_acceptance_criteria=False,
        )
        assert cut.complete is False

    def test_the_default_record_has_no_title_keys(self):
        assert "ids_loaded" not in self._ctx().record()

    def test_a_title_record_lists_both_id_sets(self):
        rec = self._ctx(ids_loaded=("T-1",), ids_failed=("T-2",), from_title=True).record()
        assert rec["ids_loaded"] == ["T-1"] and rec["ids_failed"] == ["T-2"]


@pytest.mark.usefixtures("contract_stubs")
class TestThroughOrchestratorWithFakeLLM:
    def test_wiring_record_and_footer(self, jira):
        jira.fail = {"T-3"}
        forge = FakeForge(pr=make_pr(TITLE), diff=_added_file_diff("src/app.py", 20))
        res = orchestrator.orchestrate_review(
            forge, REF, FakeLLM(), ticket_ids_from_title="on", jira_base_url=JIRA,
        )
        assert res["ticket_context"]["ids_loaded"] == ["T-1", "T-2"]
        assert res["ticket_context"]["ids_failed"] == ["T-3"]
        assert "T-3 could not be read" in forge.summaries[0]
        assert len(jira.calls) == 3

    def test_off_makes_no_http_call(self, no_http):
        forge = FakeForge(pr=make_pr(TITLE), diff=_added_file_diff("src/app.py", 20))
        res = orchestrator.orchestrate_review(forge, REF, FakeLLM(), ticket_ids_from_title="off")
        assert no_http == []
        assert res["ticket_context"] is None


class TestConfig:
    def test_default_is_off_and_the_vocabulary_is_closed(self, monkeypatch):
        assert config.load_config()["ticket_ids_from_title"] == "off"
        monkeypatch.setenv("PRXREF_TICKET_IDS_FROM_TITLE", "maybe")
        with pytest.raises(ConfigError, match="PRXREF_TICKET_IDS_FROM_TITLE"):
            config.load_config()

    def test_on_needs_a_jira_base_url(self, monkeypatch):
        monkeypatch.setenv("PRXREF_TICKET_IDS_FROM_TITLE", "on")
        with pytest.raises(ConfigError) as exc:
            config.load_config()
        assert "PRXREF_TICKET_IDS_FROM_TITLE" in str(exc.value)
        assert "PRXREF_JIRA_BASE_URL" in str(exc.value)

    @pytest.mark.parametrize(
        "env", [{"PRXREF_JIRA_EMAIL": "a@example.com"}, {"PRXREF_JIRA_API_TOKEN": "tok"}],
    )
    def test_on_needs_the_credentials_as_a_pair(self, monkeypatch, env):
        monkeypatch.setenv("PRXREF_TICKET_IDS_FROM_TITLE", "on")
        monkeypatch.setenv("PRXREF_JIRA_BASE_URL", JIRA)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        with pytest.raises(ConfigError, match="PRXREF_TICKET_IDS_FROM_TITLE"):
            config.load_config()

    def test_a_non_http_base_url_is_refused(self, monkeypatch):
        monkeypatch.setenv("PRXREF_TICKET_IDS_FROM_TITLE", "on")
        monkeypatch.setenv("PRXREF_JIRA_BASE_URL", "jira.example.com")
        with pytest.raises(ConfigError, match="PRXREF_JIRA_BASE_URL"):
            config.load_config()

    def test_on_with_a_base_url_loads(self, monkeypatch):
        monkeypatch.setenv("PRXREF_TICKET_IDS_FROM_TITLE", "on")
        monkeypatch.setenv("PRXREF_JIRA_BASE_URL", JIRA)
        assert config.load_config()["ticket_ids_from_title"] == "on"

    def test_it_is_env_only(self):
        assert "ticket_ids_from_title" in config.ENV_ONLY_KEYS
        assert "ticket_ids_from_title" not in config.FILE_KEYS

    def test_the_cli_exits_2_naming_the_variable(self, monkeypatch, capsys):
        monkeypatch.setenv("PRXREF_TICKET_IDS_FROM_TITLE", "on")
        rc = cli.main(
            ["review", "--pr-url", "https://github.com/acme/widget/pull/7", "--no-post"]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert err.startswith("configuration error: ")
        assert "PRXREF_TICKET_IDS_FROM_TITLE" in err


class TestLongTicketAndEmptyFile:
    def test_a_ticket_far_over_the_cap_is_truncated_not_unreadable(self, jira):
        jira.texts = {"T-1": "x" * 30_000}
        forge, llm, res = _run("[T-1] docs", findings=[OUT_FINDING], max_chars=6000)
        rec = res["ticket_context"]
        assert rec["ids_loaded"] == ["T-1"] and rec["ids_failed"] == []
        assert rec["truncated"] is True
        assert "[ticket context truncated" in _worker_user(llm)
        assert "could not be read" not in forge.summaries[0]
        assert _scopes(res) == ["unknown"]

    def test_an_empty_file_with_no_ids_adds_no_contradicting_line(self, tmp_path):
        from prxref.ticket import load_ticket_context

        path = tmp_path / "ticket.md"
        path.write_text("  \n", encoding="utf-8")
        base = load_ticket_context(str(path), max_chars=100, source="--context-file")
        forge, _, _ = _run("docs: no ids", findings=[IN_FINDING], ticket=base)
        assert "No ticket context for this PR" in forge.summaries[0]
        assert "Tickets read" not in forge.summaries[0]


class TestCli:
    """``prxref review`` and the webhook handler with the real loader, orchestrator and reviewer."""

    @pytest.fixture
    def rig(self, monkeypatch, jira):
        import sys
        import types

        forge = FakeForge(pr=make_pr(TITLE), diff=_added_file_diff("src/app.py", 20))
        llm = _RecordingLLM([OUT_FINDING])
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: REF)
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: forge)
        mod = types.ModuleType("prxref.llm_backends")
        mod.create_llm_client = lambda cfg: llm
        monkeypatch.setitem(sys.modules, "prxref.llm_backends", mod)
        monkeypatch.setenv("PRXREF_TICKET_IDS_FROM_TITLE", "on")
        monkeypatch.setenv("PRXREF_JIRA_BASE_URL", JIRA)
        return types.SimpleNamespace(forge=forge, llm=llm, jira=jira)

    def test_review_reads_the_title_tickets(self, rig, capsys):
        import json

        assert cli.main(["review", "--pr-url", REF.url, "--no-post", "--format", "json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ticket_context"]["ids_loaded"] == ["T-1", "T-2", "T-3"]
        assert len(rig.jira.calls) == 3
        for text in TEXTS.values():
            assert text in _worker_user(rig.llm)

    def test_the_webhook_daemon_honours_it_per_pr(self, rig):
        cli._webhook_handler(REF.url)
        assert len(rig.jira.calls) == 3
        assert "T-1, T-2, T-3" in rig.forge.summaries[0]


class TestEvalPins:
    """The eval pins hold ``ticket_ids_from_title`` off before the Jira check runs (#145)."""

    DATASET = "tests/fixtures/issue17/cases.json"
    DIFF = "tests/fixtures/issue17/pr.diff"

    @pytest.fixture
    def keyed(self, monkeypatch, tmp_path, no_http):
        """Key on, no Jira settings anywhere, orchestrator and LLM stubbed; yields the recorded kwargs."""
        calls: list[dict] = []

        def _orchestrate(**kwargs):
            calls.append(kwargs)
            return {
                "verdict": "Approved", "findings_active": [], "findings_dropped": [],
                "chunk_count": 1, "chunks_reviewed": 1, "chunks_failed": 0, "elapsed_ms": 5,
                "input_tokens": 10, "output_tokens": 2, "posted": False,
            }

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("prxref.orchestrator.orchestrate_review", _orchestrate)
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: object())
        monkeypatch.setattr(cli, "make_forge", lambda ref: object())
        monkeypatch.setenv("PRXREF_TICKET_IDS_FROM_TITLE", "on")
        for name in ("PRXREF_JIRA_BASE_URL", "PRXREF_JIRA_EMAIL", "PRXREF_JIRA_API_TOKEN",
                     "PRXREF_EVAL_PINS", config.CONFIG_FILE_ENV):
            monkeypatch.delenv(name, raising=False)
        return calls

    def _root(self):
        from pathlib import Path

        return Path(__file__).resolve().parent.parent

    def test_the_pin_is_in_the_mapping(self):
        from prxref import evals

        assert evals.EVAL_PINS["ticket_ids_from_title"] == "off"

    def test_eval_run_does_not_exit_2_and_fetches_nothing(self, keyed, no_http, tmp_path):
        argv = ["eval", "run", "--cases", str(self._root() / self.DATASET),
                "--label", "L", "--out", str(tmp_path / "out")]
        assert cli.main(argv) == 0
        assert keyed and all(c["ticket_ids_from_title"] == "off" for c in keyed)
        assert no_http == []

    def test_review_with_eval_pins_does_not_exit_2_and_fetches_nothing(self, keyed, no_http):
        argv = ["review", "--diff-file", str(self._root() / self.DIFF), "--no-post", "--eval-pins"]
        assert cli.main(argv) == 0
        assert keyed and keyed[0]["ticket_ids_from_title"] == "off"
        assert no_http == []

    def test_the_variable_pins_it_too(self, keyed, monkeypatch):
        monkeypatch.setenv("PRXREF_EVAL_PINS", "on")
        argv = ["review", "--diff-file", str(self._root() / self.DIFF), "--no-post"]
        assert cli.main(argv) == 0
        assert keyed[0]["ticket_ids_from_title"] == "off"

    def test_control_without_pins_it_still_exits_2_naming_the_variable(self, keyed, capsys):
        argv = ["review", "--diff-file", str(self._root() / self.DIFF), "--no-post"]
        assert cli.main(argv) == 2
        assert keyed == []
        assert "PRXREF_TICKET_IDS_FROM_TITLE" in capsys.readouterr().err


class TestCutTicketWithTheKeyOff:
    """A cut ticket file says why nothing is marked out of scope, key off or on (#145)."""

    def _cut(self):
        text = "word " * 200
        return TicketContext(
            path="t.md", capped=cap_text(text, 100), text=text[:100], has_acceptance_criteria=True,
        )

    def test_the_key_off_note_is_only_the_cut_sentence(self):
        note = self._cut().note()
        assert note == (
            "> ℹ️ The ticket was cut at 100 of 1000 characters; "
            "findings are not marked outside a ticket that was not fully read.\n"
        )
        assert "Tickets read" not in note

    def test_the_cut_sentence_follows_the_no_ac_note(self):
        text = "word " * 200
        ctx = TicketContext(
            path="t.md", capped=cap_text(text, 100), text=text[:100], has_acceptance_criteria=False,
        )
        assert ctx.note().startswith(NOTE_NO_AC) and "cut at 100 of 1000" in ctx.note()

    def test_under_the_cap_and_empty_notes_are_byte_identical(self):
        full = TicketContext(path="t.md", capped=cap_text("ok", 100), text="ok", has_acceptance_criteria=True)
        no_ac = TicketContext(path="t.md", capped=cap_text("ok", 100), text="ok", has_acceptance_criteria=False)
        empty = TicketContext(path="t.md", capped=cap_text("", 100), text="", has_acceptance_criteria=False)
        assert full.note() == ""
        assert no_ac.note() == NOTE_NO_AC
        assert empty.note() == NOTE_EMPTY

    def test_the_summary_carries_it_with_the_key_off(self, tmp_path):
        from prxref.ticket import load_ticket_context

        path = tmp_path / "ticket.md"
        path.write_text("word " * 200, encoding="utf-8")
        base = load_ticket_context(str(path), max_chars=100, source="--context-file")
        forge, _, res = _run(mode=None, findings=[OUT_FINDING], ticket=base)
        assert "cut at 100 of 1000 characters" in forge.summaries[0]
        assert "Tickets read" not in forge.summaries[0]
        assert _scopes(res) == ["unknown"]

    def test_an_untruncated_file_adds_no_line(self, tmp_path):
        from prxref.ticket import load_ticket_context

        path = tmp_path / "ticket.md"
        path.write_text("short ticket\n", encoding="utf-8")
        base = load_ticket_context(str(path), max_chars=100, source="--context-file")
        forge, _, _ = _run(mode=None, findings=[IN_FINDING], ticket=base)
        assert "not fully read" not in forge.summaries[0]


class TestFetchBounds:
    """A title cannot buy unbounded serial Jira fetches: 10 keys, 60 s in all."""

    def test_the_constants(self):
        assert specs.TITLE_TICKET_MAX_KEYS == 10
        assert specs.TITLE_TICKET_BUDGET_S == 60

    def test_twelve_ids_fetch_ten_and_report_two_unread(self, monkeypatch):
        keys = [f"T-{i}" for i in range(1, 13)]
        router = JiraRouter(texts={k: f"Text of {k}." for k in keys})
        monkeypatch.setattr(specs, "_create_default_session", lambda: router)
        title = "".join(f"[{k}]" for k in keys) + " docs: spec update"
        forge, _, res = _run(title, findings=[OUT_FINDING])
        assert res["verdict"] != "Error"
        assert len(router.calls) == 10
        assert res["ticket_context"]["ids_loaded"] == keys[:10]
        assert res["ticket_context"]["ids_failed"] == ["T-11", "T-12"]
        assert "T-11, T-12 could not be read" in forge.summaries[0]
        assert "not fully read" in forge.summaries[0]
        assert _scopes(res) == ["unknown"]

    def test_the_unread_sources_say_why(self, monkeypatch):
        router = JiraRouter(texts={f"T-{i}": "x" for i in range(1, 13)})
        monkeypatch.setattr(specs, "_create_default_session", lambda: router)
        out = specs.fetch_jira_tickets(
            [f"T-{i}" for i in range(1, 13)], jira_base_url=JIRA,
        )
        assert [bool(s.error) for s in out] == [False] * 10 + [True] * 2
        assert "not fetched" in out[10].error
        assert out[10].text == ""

    def test_a_spent_budget_leaves_the_rest_unread(self, monkeypatch):
        now = [1000.0]
        fetched: list[str] = []

        def fake_fetch(src, ref, *a, **kw):
            fetched.append(ref.key)
            now[0] += 25.0
            src.text = f"Text of {ref.key}."

        class Clock:
            @staticmethod
            def monotonic():
                return now[0]

        monkeypatch.setattr(specs, "time", Clock)
        monkeypatch.setattr(specs, "_fetch_jira", fake_fetch)
        monkeypatch.setattr(specs, "_create_default_session", lambda: object())
        keys = [f"T-{i}" for i in range(1, 6)]
        out = specs.fetch_jira_tickets(keys, jira_base_url=JIRA)
        assert fetched == ["T-1", "T-2", "T-3"]
        assert [bool(s.error) for s in out] == [False, False, False, True, True]
        assert "budget" in out[3].error

    def test_a_spent_budget_still_completes_the_review(self, monkeypatch):
        now = [0.0]

        def slow_fetch(src, ref, *a, **kw):
            now[0] += 100.0
            src.text = f"Text of {ref.key}."

        class Clock:
            @staticmethod
            def monotonic():
                return now[0]

        monkeypatch.setattr(specs, "time", Clock)
        monkeypatch.setattr(specs, "_fetch_jira", slow_fetch)
        monkeypatch.setattr(specs, "_create_default_session", lambda: object())
        forge, _, res = _run(findings=[OUT_FINDING])
        assert res["verdict"] != "Error"
        assert res["ticket_context"]["ids_loaded"] == ["T-1"]
        assert res["ticket_context"]["ids_failed"] == ["T-2", "T-3"]
        assert _scopes(res) == ["unknown"]
