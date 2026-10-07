"""Team learnings (#33): the file, the suppression pass, the record and harvest."""
from __future__ import annotations

import json
import logging
import random
from dataclasses import replace
from datetime import date, timedelta

import pytest

from prxref import cli, orchestrator
from prxref import learnings as learnings_mod
from prxref.forges.base import ATTRIBUTION_MARKER, PRRef, Thread, says_wont_fix
from prxref.learnings import (
    DROP_PREFIX,
    Learnings,
    harvest_candidates,
    load_learnings,
    parse_inline_header,
    parse_learnings,
    render_toml,
)
from prxref.llm import ConfigError
from prxref.markers import inline_header
from prxref.orchestrator import orchestrate_review
from prxref.quality import active, apply_learning_suppression
from prxref.triage import Finding
from tests.test_orchestrator import REF, FakeForge, FakeLLM, _added_file_diff

TODAY = date(2026, 10, 7)
SOURCE = "PRXREF_LEARNINGS_FILE"

FILE = """\
[[learning]]
id = "legacy-raw-sql"
paths = ["src/legacy/**"]
claim = "Raw SQL string built by concatenation"
rule = "SEC-3"
reason = "Inputs are compile-time constants in this module"
added = 2026-09-01

[[learning]]
id = "fixtures-secrets"
paths = ["tests/fixtures/**", "!tests/fixtures/real/**"]
claim = "Hardcoded secret token committed"
"""


def _finding(file="src/legacy/db.py", title="Raw SQL string built by concatenation",
             body="The query is assembled with string concatenation.", rule="SEC-3",
             severity="error", **kw):
    return Finding(file=file, line=4, severity=severity, confidence=0.9, title=title,
                   body=body, rule=rule, **kw)


def _entries(text=FILE):
    return parse_learnings(text)


class TestSuppression:
    def test_a_match_drops_the_finding_with_the_reason(self):
        out = apply_learning_suppression([_finding()], _entries())
        assert out[0].drop_reason == "suppressed by learning: legacy-raw-sql"
        assert out[0].drop_reason == DROP_PREFIX + "legacy-raw-sql"

    def test_the_rule_is_compared_case_insensitively(self):
        out = apply_learning_suppression([_finding(rule=" sec-3 ")], _entries())
        assert out[0].drop_reason == "suppressed by learning: legacy-raw-sql"

    def test_a_path_the_globs_do_not_select_is_untouched(self):
        f = _finding(file="src/modern/db.py")
        assert apply_learning_suppression([f], _entries()) == [f]

    def test_a_negated_path_is_untouched(self):
        f = _finding(file="tests/fixtures/real/a.env", title="Hardcoded secret token committed",
                     rule=None)
        assert apply_learning_suppression([f], _entries()) == [f]

    def test_another_rule_is_untouched(self):
        f = _finding(rule="SEC-4")
        assert apply_learning_suppression([f], _entries()) == [f]

    def test_a_ruleless_finding_never_matches_a_learning_with_a_rule(self):
        f = _finding(rule=None)
        assert apply_learning_suppression([f], _entries()) == [f]

    def test_a_learning_without_a_rule_matches_any_rule(self):
        f = _finding(file="tests/fixtures/x.env", title="Hardcoded secret token committed",
                     body="", rule="ANY-1")
        out = apply_learning_suppression([f], _entries())
        assert out[0].drop_reason == "suppressed by learning: fixtures-secrets"

    def test_too_few_shared_claim_tokens_is_untouched(self):
        f = _finding(title="Raw query looks slow", body="Consider an index.")
        assert apply_learning_suppression([f], _entries()) == [f]

    def test_an_expired_entry_is_skipped_and_counted(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "l.toml").write_text(FILE.replace("added = 2026-09-01", "expires = 2026-10-06"))
        loaded = load_learnings("l.toml", source=SOURCE, today=TODAY)
        assert [e.id for e in loaded.active] == ["fixtures-secrets"]
        assert loaded.record()["expired"] == 1
        f = _finding()
        assert apply_learning_suppression([f], loaded.active) == [f]

    def test_an_entry_expiring_today_still_applies(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "l.toml").write_text(FILE.replace("added = 2026-09-01", "expires = 2026-10-07"))
        loaded = load_learnings("l.toml", source=SOURCE, today=TODAY)
        assert loaded.record()["expired"] == 0
        assert apply_learning_suppression([_finding()], loaded.active)[0].drop_reason

    def test_an_already_dropped_finding_keeps_its_reason(self):
        f = _finding(drop_reason="duplicate of an existing thread")
        assert apply_learning_suppression([f], _entries()) == [f]

    def test_no_learnings_is_the_identity(self):
        fs = [_finding(), _finding(file="x.py")]
        assert apply_learning_suppression(fs, ()) == fs


class TestSuppressionProperty:
    """Seeded random inputs: active never grows, earlier drop reasons survive."""

    WORDS = ["concatenation", "string", "built", "secret", "token", "hardcoded",
             "committed", "query", "index", "missing", "null", "handler", "raw"]
    FILES = ["src/legacy/a.py", "src/legacy/b/c.py", "tests/fixtures/k.env",
             "tests/fixtures/real/k.env", "src/app.py", "./src/legacy/d.py"]
    RULES = [None, "SEC-3", "sec-3", "SEC-4", "ANY"]

    def _random_finding(self, rng):
        title = " ".join(rng.choice(self.WORDS) for _ in range(rng.randint(1, 6)))
        body = " ".join(rng.choice(self.WORDS) for _ in range(rng.randint(0, 6)))
        drop = rng.choice([None, None, "duplicate", "suppressed by learning: old"])
        return _finding(file=rng.choice(self.FILES), title=title, body=body,
                        rule=rng.choice(self.RULES), drop_reason=drop)

    def test_active_never_grows_and_earlier_reasons_are_preserved(self):
        rng = random.Random(33)
        entries = _entries()
        for _ in range(500):
            fs = [self._random_finding(rng) for _ in range(rng.randint(0, 12))]
            out = apply_learning_suppression(fs, entries)
            assert len(out) == len(fs)
            assert len(active(out)) <= len(active(fs))
            for before, after in zip(fs, out, strict=True):
                assert replace(after, drop_reason=None) == replace(before, drop_reason=None)
                if before.drop_reason is not None:
                    assert after.drop_reason == before.drop_reason
                elif after.drop_reason is not None:
                    assert after.drop_reason.startswith(DROP_PREFIX)


class TestTheFile:
    def test_a_valid_file_loads_with_its_digest(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "l.toml").write_text(FILE)
        loaded = load_learnings("l.toml", source=SOURCE, today=TODAY)
        assert isinstance(loaded, Learnings)
        rec = loaded.record()
        assert rec["file"] == "l.toml" and rec["loaded"] == 2 and rec["expired"] == 0
        assert rec["suppressed"] == [] and len(rec["sha256"]) == 64

    @pytest.mark.parametrize("path", [None, "", "   "])
    def test_unset_is_off(self, path):
        assert load_learnings(path, source=SOURCE) is None

    @pytest.mark.parametrize("text, problem", [
        ("[[learning]\n", "invalid TOML"),
        ("title = 1\n", "unknown top-level key"),
        ('[[learning]]\nid = "a"\nclaim = "Raw string built"\n', "missing 'paths'"),
        ('[[learning]]\nid = "a"\npaths = []\nclaim = "Raw string built"\n', "non-empty array"),
        ('[[learning]]\nid = "a"\npaths = ["!x/**"]\nclaim = "Raw string built"\n', "only negations"),
        ('[[learning]]\nid = "a"\npaths = ["x"]\nclaim = "a b c"\n', "no content word"),
        ('[[learning]]\nid = "a"\npaths = ["x"]\nclaim = "Raw string built"\nwhy = "x"\n', "unknown key"),
        ('[[learning]]\nid = "a"\npaths = ["x"]\nclaim = "Raw string built"\nexpires = "soon"\n', "'expires'"),
        ('[[learning]]\nid = "a"\npaths = ["x"]\nclaim = "Raw string built"\n'
         '[[learning]]\nid = "a"\npaths = ["y"]\nclaim = "Raw string built"\n', "duplicate id"),
    ])
    def test_a_malformed_file_is_a_config_error_naming_the_variable(
        self, tmp_path, monkeypatch, text, problem,
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "l.toml").write_text(text)
        with pytest.raises(ConfigError) as exc:
            load_learnings("l.toml", source=SOURCE, today=TODAY)
        assert str(exc.value).startswith("PRXREF_LEARNINGS_FILE: ")
        assert problem in str(exc.value)

    def test_a_missing_file_is_a_config_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ConfigError, match="^PRXREF_LEARNINGS_FILE: cannot read"):
            load_learnings("nope.toml", source=SOURCE)

    def test_an_old_entry_is_warned_about(self, tmp_path, monkeypatch, caplog):
        monkeypatch.chdir(tmp_path)
        old = (TODAY - timedelta(days=181)).isoformat()
        (tmp_path / "l.toml").write_text(FILE.replace("added = 2026-09-01", f"added = {old}"))
        with caplog.at_level(logging.WARNING, logger="prxref.learnings"):
            loaded = load_learnings("l.toml", source=SOURCE, today=TODAY)
        assert loaded.stale == ("legacy-raw-sql",)
        assert "more than 180 days" in caplog.text and "legacy-raw-sql" in caplog.text

    def test_a_recent_entry_is_not_warned_about(self, tmp_path, monkeypatch, caplog):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "l.toml").write_text(FILE)
        with caplog.at_level(logging.WARNING, logger="prxref.learnings"):
            assert load_learnings("l.toml", source=SOURCE, today=TODAY).stale == ()
        assert caplog.text == ""


class TestTheCliExitsTwo:
    @pytest.mark.parametrize("argv", [
        ["config", "check", "--no-config"],
        ["review", "--no-config", "--pr-url", "https://github.com/o/r/pull/1"],
    ])
    def test_malformed_toml_exits_2_naming_the_variable(self, tmp_path, monkeypatch, capsys, argv):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "l.toml").write_text("[[learning]\n")
        monkeypatch.setenv("PRXREF_LEARNINGS_FILE", "l.toml")
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: pytest.fail("forge contacted"))
        assert cli.main(argv) == 2
        err = capsys.readouterr().err
        assert "configuration error: PRXREF_LEARNINGS_FILE: " in err
        assert "invalid TOML" in err


def _thread(snippet, wont_fix_text, *, path="src/legacy/db.py", url="https://h/t/1", author="bob"):
    return Thread(path=path, line=4, resolved=True, author=author, body_snippet=snippet[:120],
                  url=url, wont_fix=says_wont_fix(wont_fix_text))


class TestHarvest:
    HEADER = inline_header(_finding())

    def test_the_header_round_trips(self):
        assert parse_inline_header(self.HEADER) == (
            "Raw SQL string built by concatenation", "src/legacy/db.py",
        )

    @pytest.mark.parametrize("severity, scope", [
        ("error", "in"), ("warning", "out"), ("spec", "unknown"), ("outofscope", "in"),
    ])
    def test_the_posted_comment_body_parses_through_a_forge_snippet(self, severity, scope):
        f = _finding(severity=severity, scope=scope)
        body = orchestrator._format_finding(f, "model-x")
        for cut in (120, 200):
            title, _ = parse_inline_header(body[:cut])
            assert title == f.title

    def test_an_out_of_ticket_header_parses(self):
        f = _finding(severity="warning", scope="out")
        assert parse_inline_header(inline_header(f))[0] == f.title

    def test_a_truncated_header_keeps_the_title_prefix(self):
        long = _finding(title="Raw SQL string built by concatenation " + "x" * 200)
        title, loc = parse_inline_header(inline_header(long)[:120])
        assert title.startswith("Raw SQL string built") and loc is None

    def test_one_wont_fix_thread_on_a_prxref_comment_gives_one_candidate(self):
        body = f"{self.HEADER}\n\nThe query...\n\n_{ATTRIBUTION_MARKER} (model x)_"
        threads = [
            _thread(body, "won't fix, constants only"),
            _thread("Some human comment", "won't fix this", url="https://h/t/2"),
            _thread(body, body, url="https://h/t/3", path="src/other.py"),
        ]
        cands = harvest_candidates(threads, "https://h/pr/1", today=TODAY)
        assert len(cands) == 1
        cand = cands[0]
        assert cand["paths"] == ["src/legacy/db.py"]
        assert cand["claim"] == "Raw SQL string built by concatenation"
        assert cand["source"] == "https://h/t/1" and cand["added"] == TODAY
        assert "rule" not in cand

    def test_a_prxref_authored_wont_fix_gives_none(self):
        body = f"{self.HEADER}\n\nThis is a won't fix candidate.\n\n_{ATTRIBUTION_MARKER} (model x)_"
        threads = [_thread(body, body, author="prxref-bot")]
        assert threads[0].wont_fix is False
        assert harvest_candidates(threads, "https://h/pr/1", today=TODAY) == []

    def test_the_rendered_toml_loads_back(self):
        body = f"{self.HEADER}\n\n_{ATTRIBUTION_MARKER}_"
        cands = harvest_candidates(
            [_thread(body, "won't fix")], 'https://h/pr/1\n"x', today=TODAY,
        )
        entries = parse_learnings(render_toml(cands, 'https://h/pr/1\n"x'))
        assert [(e.id, e.paths, e.claim, e.added) for e in entries] == [
            (cands[0]["id"], ("src/legacy/db.py",), cands[0]["claim"], TODAY),
        ]
        assert apply_learning_suppression([_finding()], entries)[0].drop_reason

    def test_no_candidates_still_renders_valid_toml(self):
        text = render_toml([], "https://h/pr/1")
        assert parse_learnings(text) == ()
        assert "No prxref finding" in text


class TestHarvestCommand:
    URL = "https://github.com/o/r/pull/1"

    def _forge(self, monkeypatch, threads):
        class Forge:
            def list_threads(self, ref):
                assert isinstance(ref, PRRef)
                return threads
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: Forge())

    def test_prints_the_candidates(self, monkeypatch, capsys):
        header = inline_header(_finding())
        self._forge(monkeypatch, [_thread(header, header + "\nwon't fix")])
        assert cli.main(["learnings", "harvest", "--pr-url", self.URL]) == 0
        out = capsys.readouterr().out
        assert len(parse_learnings(out)) == 1

    def test_writes_to_out(self, tmp_path, monkeypatch, capsys):
        self._forge(monkeypatch, [])
        out = tmp_path / "cand.toml"
        assert cli.main(["learnings", "harvest", "--pr-url", self.URL, "--out", str(out)]) == 0
        assert capsys.readouterr().out == ""
        assert parse_learnings(out.read_text()) == ()

    def test_an_unrecognised_url_exits_2(self, capsys):
        assert cli.main(["learnings", "harvest", "--pr-url", "https://example.com/x"]) == 2
        assert "--pr-url" in capsys.readouterr().err

    def test_a_forge_failure_exits_1(self, monkeypatch, capsys):
        def boom(ref):
            raise RuntimeError("401")
        monkeypatch.setattr("prxref.cli.make_forge", boom)
        assert cli.main(["learnings", "harvest", "--pr-url", self.URL]) == 1
        assert "401" in capsys.readouterr().err


@pytest.mark.usefixtures("contract_stubs")
class TestTheOrchestrator:
    FINDINGS = {
        "src/legacy/db.py": [
            {"file": "src/legacy/db.py", "line": 3, "severity": "error", "confidence": 0.9,
             "title": "Raw SQL string built by concatenation",
             "body": "data string concatenation builds the query."},
            {"file": "src/legacy/db.py", "line": 7, "severity": "warning", "confidence": 0.9,
             "title": "Missing null check", "body": "data may be None here."},
        ],
    }

    def _run(self, learnings=None):
        forge = FakeForge(diff=_added_file_diff("src/legacy/db.py", 20))
        return orchestrate_review(forge, REF, FakeLLM(findings_by_path=self.FINDINGS),
                                  learnings=learnings)

    def _loaded(self):
        entries = parse_learnings(FILE.replace('rule = "SEC-3"\n', ""))
        return Learnings(path="l.toml", sha256="0" * 64, entries=entries, today=TODAY)

    def _titles(self, rows):
        return [(r["title"] if isinstance(r, dict) else r.title) for r in rows]

    def test_off_records_null(self):
        assert self._run()["learnings"] is None

    def test_on_drops_and_records(self):
        res = self._run(self._loaded())
        assert self._titles(res["findings_active"]) == ["Missing null check"]
        dropped = res["findings_dropped"]
        reasons = [(d["drop_reason"] if isinstance(d, dict) else d.drop_reason) for d in dropped]
        assert reasons == ["suppressed by learning: legacy-raw-sql"]
        rec = res["learnings"]
        assert rec["file"] == "l.toml" and rec["loaded"] == 2 and rec["expired"] == 0
        assert len(rec["suppressed"]) == 1
        row = rec["suppressed"][0]
        assert row["learning_id"] == "legacy-raw-sql"
        assert row["title"] == "Raw SQL string built by concatenation"
        assert row["finding_id"].startswith("src/legacy/db.py#")
        assert json.loads(json.dumps(cli._build_json_result(res)))["learnings"] == rec

    def test_a_non_matching_file_changes_nothing_but_the_record(self):
        off = self._run()
        entries = parse_learnings(FILE.replace("src/legacy/**", "elsewhere/**"))
        on = self._run(Learnings(path="l.toml", sha256="0" * 64, entries=entries, today=TODAY))
        assert on["learnings"]["suppressed"] == []
        for key in ("findings_active", "findings_dropped", "verdict"):
            assert on[key] == off[key]


def test_the_module_never_writes_files():
    source = open(learnings_mod.__file__).read()
    assert "open(" not in source.replace("with open(resolved, \"rb\")", "")
    assert ".write_text(" not in source
