"""``prxref eval mine`` (issue #81): a dataset from a repo's merged PRs, over a fake HTTP session."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from prxref import eval_mine, evals
from prxref.eval_cases import load_cases
from prxref.eval_mine import (
    _accepted,
    _changed_positions,
    mine,
    parse_severities,
    rehash,
)
from prxref.llm import ConfigError

C1, C2, C3, C9 = "1" * 40, "2" * 40, "3" * 40, "9" * 40
MERGE_BASE = "b" * 40
FINAL10 = C2
A_PATCH = "@@ -8,4 +8,5 @@\n ctx\n ctx\n-old\n+new\n+new2\n ctx\n"
API = "https://api.github.com/repos/o/r"


class FakeResponse:
    def __init__(self, status: int, body, headers=None) -> None:
        self.headers = headers or {}
        self.status_code = status
        self.ok = status < 400
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class Replies:
    """A route that answers each request with the next ``(status, body[, headers])``; the last repeats."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)

    def next(self):
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


class FakeSession:
    """Answers GETs from a ``{url: body}`` table (a ``(status, body)`` tuple sets the status); records every URL."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.urls: list[str] = []
        self.params: list[dict] = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.urls.append(url)
        self.params.append(dict(params or {}))
        page = (params or {}).get("page", 1)
        route = self.routes.get(url)
        if route is None:
            return FakeResponse(404, {"message": "Not Found"})
        if isinstance(route, Replies):
            route = route.next()
        status, body, *rest = route if isinstance(route, tuple) else (200, route)
        if isinstance(body, list) and page > 1:
            body = []
        return FakeResponse(status, body, rest[0] if rest else None)


def _pr(number, merged_at, author="alice", head=FINAL10, updated=None, merged=True):
    return {
        "number": number, "merged_at": merged_at if merged else None,
        "updated_at": updated or merged_at or "2026-01-01T00:00:00Z",
        "user": {"login": author, "type": "User"}, "base": {"ref": "main"}, "head": {"sha": head},
        "html_url": f"https://github.com/o/r/pull/{number}",
    }


def _comment(cid, user, path, line, commit, body="fix this", parent=None, utype="User", assoc=None):
    return {
        "author_association": assoc,
        "id": cid, "user": {"login": user, "type": utype}, "path": path, "original_line": line,
        "original_commit_id": commit, "body": body, "in_reply_to_id": parent,
        "html_url": f"https://github.com/o/r/pull/x#discussion_r{cid}",
        "created_at": f"2026-09-0{cid % 9 + 1}T00:00:00Z",
    }


def _compare(left, right, merge_base, files=None):
    return f"{API}/compare/{left}...{right}", {"merge_base_commit": {"sha": merge_base}, "files": files or []}


def _routes() -> dict:
    routes = {
        f"{API}/pulls": [
            _pr(12, None, merged=False, updated="2026-09-25T00:00:00Z"),
            _pr(10, "2026-09-20T00:00:00Z", updated="2026-09-24T00:00:00Z"),
            _pr(11, "2026-09-10T00:00:00Z", updated="2026-09-11T00:00:00Z"),
            _pr(9, "2026-08-01T00:00:00Z", head=C9),
            _pr(8, "2026-07-01T00:00:00Z"),
        ],
        f"{API}/pulls/10/comments": [
            _comment(101, "bob", "a.py", 10, C1, "Handle the empty case."),
            _comment(102, "alice", "a.py", 10, C1, "Done.", parent=101),
            _comment(103, "dependabot[bot]", "a.py", 11, C1, utype="Bot"),
            _comment(104, "ci-helper[bot]", "a.py", 12, C1, utype="User"),
            _comment(105, "alice", "a.py", 13, C1, "my own note"),
            _comment(106, "carol", "b.py", 5, C1, "Rename this."),
            _comment(107, "carol", "a.py", 30, C1, "Far from the later edit."),
            _comment(108, "carol", "a.py", 20, C2, "On the final head."),
            _comment(109, "carol", "a.py", None, C1, "file-level"),
        ],
        f"{API}/pulls/11/comments": [_comment(111, "alice", "a.py", 1, C1)],
        f"{API}/pulls/9/comments": [_comment(91, "bob", "z.py", 3, C3, "Why?")],
        f"{API}/pulls/8/comments": [_comment(81, "bob", "y.py", 3, C1, "Old.")],
    }
    for commit in (C1, C2, C3):
        url, body = _compare("main", commit, MERGE_BASE)
        routes[url] = body
    url, body = _compare(C1, FINAL10, C1, [{"filename": "a.py", "status": "modified", "patch": A_PATCH}])
    routes[url] = body
    routes[f"{API}/compare/{C3}...{C9}"] = (404, {"message": "Not Found"})
    return routes


def _args(tmp_path, **over):
    base = dict(repo="o/r", out=str(tmp_path / "out"), host="github.com", since=None, until=None, pr=None,
                reviewers="any", prs=50, judge_model=None, min_comments=1, rehash=None, allow_unconfirmed=False)
    base.update(over)
    return SimpleNamespace(eval_command="mine", **base)


class StubJudge:
    def __init__(self, replies=None, error=None):
        self.replies = list(replies or [])
        self.error = error
        self.calls: list[dict] = []

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=None):
        self.calls.append({"system": system, "user": user, "json_mode": json_mode, "max_tokens": max_tokens})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.replies.pop(0) if self.replies else "{}")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """No real sleeping: the miner's sleeper records each wait; the clock is frozen at 1_000_000."""
    waits: list[float] = []
    monkeypatch.setattr(eval_mine, "_sleep", waits.append, raising=False)
    monkeypatch.setattr(eval_mine, "_now", lambda: 1_000_000.0, raising=False)
    return waits


@pytest.fixture(autouse=True)
def _no_token(monkeypatch):
    monkeypatch.delenv("PRXREF_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("PRXREF_GITHUB_ENTERPRISE_TOKEN", raising=False)


def _run(tmp_path, capsys, session=None, client=None, **over):
    args = _args(tmp_path, **over)
    code = mine(args, session=session or FakeSession(_routes()), client=client)
    return args, code, capsys.readouterr().out


def _cases(args):
    return json.loads((Path(args.out) / "cases.json").read_text())["cases"]


class TestFiltersAndGrouping:
    def test_bots_the_author_replies_and_lineless_comments_are_not_labels(self, tmp_path, capsys):
        args, code, _ = _run(tmp_path, capsys)
        assert code == 0
        cases = {c["id"]: c for c in _cases(args)}
        labels = {lb["id"] for c in cases.values() for lb in c["expected"]}
        assert labels == {"c101", "c106", "c107", "c108", "c91", "c81"}

    def test_replies_are_appended_to_the_root_text(self, tmp_path, capsys):
        args, _, _ = _run(tmp_path, capsys)
        text = next(lb for c in _cases(args) for lb in c["expected"] if lb["id"] == "c101")["text"]
        assert text == "Handle the empty case.\n\nReply from alice: Done."

    def test_one_case_per_pr_and_reviewed_commit(self, tmp_path, capsys):
        args, _, out = _run(tmp_path, capsys)
        cases = {c["id"]: c for c in _cases(args)}
        assert set(cases) == {f"pr10-{C1[:7]}", f"pr10-{C2[:7]}", f"pr9-{C3[:7]}", f"pr8-{C1[:7]}"}
        first = cases[f"pr10-{C1[:7]}"]
        assert (first["head_sha"], first["base_sha"], first["pr_url"]) == (C1, MERGE_BASE, "https://github.com/o/r/pull/10")
        assert [lb["id"] for lb in first["expected"]] == ["c101", "c106", "c107"]
        assert out.splitlines() == [
            "#10: 2 cases, 4 labels", "#9: 1 cases, 1 labels", "#8: 1 cases, 1 labels",
            f"cases: {Path(args.out) / 'cases.json'}",
        ]

    def test_a_pr_below_min_comments_is_left_out(self, tmp_path, capsys):
        args, _, out = _run(tmp_path, capsys, min_comments=2)
        assert [c["id"] for c in _cases(args)] == [f"pr10-{C1[:7]}", f"pr10-{C2[:7]}"]
        assert out.splitlines()[0] == "#10: 2 cases, 4 labels" and "#9:" not in out and "#8:" not in out


class TestAccepted:
    def test_changed_near_the_line_unchanged_file_and_undeterminable(self, tmp_path, capsys):
        args, _, _ = _run(tmp_path, capsys)
        got = {lb["id"]: lb["accepted"] for c in _cases(args) for lb in c["expected"]}
        assert got["c101"] is True
        assert got["c106"] is False
        assert got["c107"] is False
        assert got["c108"] is False
        assert got["c91"] is None

    def test_the_window_is_three_lines(self):
        comparison = {"merge_base_commit": {"sha": C1}, "files": [{"filename": "a.py", "patch": A_PATCH}]}
        assert [_accepted(n, "a.py", comparison, C1) for n in (6, 7, 10, 13, 14)] == [False, True, True, True, False]

    def test_a_rewritten_history_and_a_withheld_patch_are_undeterminable(self):
        moved = {"merge_base_commit": {"sha": "f" * 40}, "files": []}
        assert _accepted(1, "a.py", moved, C1) is None
        withheld = {"merge_base_commit": {"sha": C1}, "files": [{"filename": "a.py", "status": "modified"}]}
        assert _accepted(1, "a.py", withheld, C1) is None
        assert _accepted(1, "a.py", None, C1) is None

    def test_changed_positions_reads_removals_and_insertions(self):
        assert _changed_positions(A_PATCH) == [10.0, 10.5, 10.5]


class TestSeverity:
    def test_without_a_judge_every_label_is_warning_by_default(self, tmp_path, capsys):
        args, _, _ = _run(tmp_path, capsys)
        meta = json.loads((Path(args.out) / "mine.json").read_text())
        assert {lb["severity"] for lb in meta["labels"]} == {"warning"}
        assert {lb["severity_source"] for lb in meta["labels"]} == {"default"}
        assert meta["judge_model"] is None

    def test_a_judge_drafts_one_call_per_case_and_failures_default(self, tmp_path, capsys, caplog):
        good = json.dumps({"severities": [
            {"id": "c101", "severity": "error"}, {"id": "c106", "severity": "minor"},
            {"id": "c107", "severity": "spec"},
        ]})
        client = StubJudge([good, "not json", "not json", "not json", "not json", "not json", "not json"])
        with caplog.at_level(logging.WARNING):
            args, _, _ = _run(tmp_path, capsys, judge_model="judge-x", client=client)
        assert len(client.calls) == 7 and all(c["json_mode"] for c in client.calls)
        assert "c101" in client.calls[0]["user"] and "c108" not in client.calls[0]["user"]
        meta = json.loads((Path(args.out) / "mine.json").read_text())
        by_id = {lb["label_id"]: lb for lb in meta["labels"]}
        assert (by_id["c101"]["severity"], by_id["c101"]["severity_source"]) == ("error", "judge")
        assert (by_id["c106"]["severity"], by_id["c107"]["severity"]) == ("minor", "spec")
        assert (by_id["c108"]["severity"], by_id["c108"]["severity_source"]) == ("warning", "judge_error")
        assert meta["judge_model"] == "judge-x"
        written = {lb["id"]: lb["severity"] for c in _cases(args) for lb in c["expected"]}
        assert written["c101"] == "error" and written["c108"] == "warning"
        assert any("severity judge" in r.getMessage() for r in caplog.records)

    def test_a_judge_that_raises_leaves_warning_judge_error(self, tmp_path, capsys):
        client = StubJudge(error=RuntimeError("boom"))
        args, code, _ = _run(tmp_path, capsys, judge_model="judge-x", client=client)
        assert code == 0
        meta = json.loads((Path(args.out) / "mine.json").read_text())
        assert {lb["severity_source"] for lb in meta["labels"]} == {"judge_error"}

    def test_parse_severities_refuses_missing_unknown_and_repeated_entries(self):
        ids = ["c1", "c2"]
        ok = json.dumps({"severities": [{"id": "c1", "severity": "error"}, {"id": "c2", "severity": "minor"}]})
        assert parse_severities(ok, ids) == {"c1": "error", "c2": "minor"}
        for bad in (
            json.dumps({"severities": [{"id": "c1", "severity": "error"}]}),
            json.dumps({"severities": [{"id": "c1", "severity": "bad"}, {"id": "c2", "severity": "error"}]}),
            json.dumps({"severities": [{"id": "c1", "severity": "error"}, {"id": "c1", "severity": "error"}]}),
            "[]",
        ):
            with pytest.raises(ValueError):
                parse_severities(bad, ids)


class TestOutputs:
    def test_cases_json_loads_and_the_hash_matches(self, tmp_path, capsys):
        args, _, _ = _run(tmp_path, capsys)
        out = Path(args.out)
        loaded = load_cases(out / "cases.json")
        assert len(loaded) == 4 and all(case.pr_url and case.base_sha and case.head_sha for case in loaded)
        meta = json.loads((out / "mine.json").read_text())
        assert meta["cases_sha256"] == hashlib.sha256((out / "cases.json").read_bytes()).hexdigest()
        assert list(meta) == [
            "version", "repo", "host", "since", "until", "reviewers", "pr_numbers", "prs_requested", "stopped",
            "created_at", "prxref_version", "judge_model", "cases_sha256", "prs", "labels",
        ]
        assert [pr["number"] for pr in meta["prs"]] == [10, 9, 8]
        assert meta["prs"][0]["cases"] == [f"pr10-{C1[:7]}", f"pr10-{C2[:7]}"]
        assert {lb["confirmed"] for lb in meta["labels"]} == {False}
        assert meta["labels"][0]["comment_url"].endswith("discussion_r101")

    def test_severity_review_lists_every_label_with_instructions(self, tmp_path, capsys):
        args, _, _ = _run(tmp_path, capsys)
        text = (Path(args.out) / "severity-review.md").read_text()
        assert f"## pr10-{C1[:7]}" in text and "| c101 | a.py:10 | warning | default | Handle the empty case." in text
        assert "confirmed" in text and "--rehash" in text

    def test_prs_and_since_limit_the_set(self, tmp_path, capsys):
        args, _, out = _run(tmp_path, capsys, prs=1)
        assert [line.split(":")[0] for line in out.splitlines()[:-1]] == ["#10"]
        args2, _, out2 = _run(tmp_path / "b", capsys, since="2026-08-01")
        assert [line.split(":")[0] for line in out2.splitlines()[:-1]] == ["#10", "#9"]
        meta = json.loads((Path(args2.out) / "mine.json").read_text())
        assert (meta["since"], meta["prs_requested"]) == ("2026-08-01", 50)

    def test_a_non_empty_out_is_refused_naming_out(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        (out / "keep.txt").write_text("x")
        with pytest.raises(ConfigError, match="--out"):
            mine(_args(tmp_path), session=FakeSession(_routes()))
        assert [p.name for p in out.iterdir()] == ["keep.txt"]

    def test_an_empty_existing_out_is_accepted(self, tmp_path, capsys):
        (tmp_path / "out").mkdir()
        _, code, _ = _run(tmp_path, capsys)
        assert code == 0

    def test_a_pr_whose_reads_fail_is_skipped_with_a_warning(self, tmp_path, capsys, caplog):
        routes = _routes()
        routes[f"{API}/pulls/10/comments"] = (500, {"message": "boom"})
        with caplog.at_level(logging.WARNING):
            args, code, out = _run(tmp_path, capsys, session=FakeSession(routes))
        assert code == 0 and "#10" not in out and "#9:" in out
        assert any("#10" in r.getMessage() for r in caplog.records)

    def test_an_unknown_repository_exits_2_naming_repo(self, tmp_path):
        with pytest.raises(ConfigError, match="--repo"):
            mine(_args(tmp_path, repo="o/missing"), session=FakeSession({}))

    def test_a_missing_token_warns_about_rate_limits(self, tmp_path, capsys, caplog):
        with caplog.at_level(logging.WARNING):
            _run(tmp_path, capsys)
        assert any("rate limited" in r.getMessage() for r in caplog.records)

    def test_an_enterprise_host_uses_its_api_base(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("PRXREF_GITHUB_ENTERPRISE_TOKEN", "t")
        session = FakeSession({})
        with pytest.raises(ConfigError):
            mine(_args(tmp_path, host="ghe.example.com"), session=session)
        assert session.urls[0] == "https://ghe.example.com/api/v3/repos/o/r/pulls"

    @pytest.mark.parametrize("over, flag", [
        ({"repo": "nope"}, "--repo"), ({"repo": None}, "--repo"), ({"out": None}, "--out"),
        ({"since": "yesterday"}, "--since"), ({"until": "yesterday"}, "--until"),
        ({"since": "2026-02-01", "until": "2026-01-01"}, "--until"), ({"reviewers": "all"}, "--reviewers"),
        ({"pr": "10,x"}, "--pr"), ({"pr": "0"}, "--pr"), ({"pr": "10", "since": "2026-01-01"}, "--pr"),
        ({"pr": "10", "until": "2026-01-01"}, "--pr"), ({"prs": 0}, "--prs"), ({"min_comments": 0}, "--min-comments"),
        ({"judge_model": "a,b"}, "--judge-model"), ({"allow_unconfirmed": True}, "--allow-unconfirmed"),
    ])
    def test_bad_flags_name_themselves(self, tmp_path, over, flag):
        with pytest.raises(ConfigError, match=flag):
            mine(_args(tmp_path, **over), session=FakeSession(_routes()))


class TestRehash:
    def _mined(self, tmp_path, capsys):
        args, _, _ = _run(tmp_path, capsys)
        return Path(args.out)

    def test_it_refuses_while_a_label_is_unconfirmed(self, tmp_path, capsys):
        out = self._mined(tmp_path, capsys)
        before = (out / "mine.json").read_text()
        with pytest.raises(ConfigError, match="--rehash.*confirmed: false"):
            rehash(SimpleNamespace(rehash=str(out), allow_unconfirmed=False))
        assert (out / "mine.json").read_text() == before

    def test_it_records_the_new_hash_once_confirmed_or_allowed(self, tmp_path, capsys):
        out = self._mined(tmp_path, capsys)
        cases = json.loads((out / "cases.json").read_text())
        cases["cases"][0]["expected"][0]["severity"] = "error"
        (out / "cases.json").write_text(json.dumps(cases, indent=2) + "\n")
        meta = json.loads((out / "mine.json").read_text())
        for label in meta["labels"]:
            label["confirmed"] = True
        (out / "mine.json").write_text(json.dumps(meta))
        assert rehash(SimpleNamespace(rehash=str(out), allow_unconfirmed=False)) == 0
        assert capsys.readouterr().out.strip().startswith("cases_sha256: ")
        after = json.loads((out / "mine.json").read_text())
        assert after["cases_sha256"] == hashlib.sha256((out / "cases.json").read_bytes()).hexdigest()
        assert after["cases_sha256"] != meta["cases_sha256"]

    def test_allow_unconfirmed_hashes_anyway_through_the_action(self, tmp_path, capsys):
        out = self._mined(tmp_path, capsys)
        code = evals.eval_mine(SimpleNamespace(rehash=str(out), allow_unconfirmed=True))
        assert code == 0
        assert "cases_sha256: " in capsys.readouterr().out

    def test_rehash_refuses_a_cases_file_that_no_longer_loads(self, tmp_path, capsys):
        out = self._mined(tmp_path, capsys)
        (out / "cases.json").write_text("{}")
        with pytest.raises(ConfigError):
            rehash(SimpleNamespace(rehash=str(out), allow_unconfirmed=True))

    def test_rehash_cannot_be_combined_with_repo(self, tmp_path):
        with pytest.raises(ConfigError, match="--rehash"):
            mine(_args(tmp_path, rehash=str(tmp_path)), session=FakeSession({}))


SEARCH = "https://api.github.com/search/issues"


def _hits(*numbers, total=None):
    return {"total_count": total if total is not None else len(numbers), "items": [{"number": n} for n in numbers]}


def _window_routes() -> dict:
    routes = _routes()
    del routes[f"{API}/pulls"]
    routes[SEARCH] = _hits(8, 9, 10, 12)
    routes[f"{API}/pulls/10"] = _pr(10, "2026-09-20T00:00:00Z")
    routes[f"{API}/pulls/9"] = _pr(9, "2026-08-01T00:00:00Z", head=C9)
    routes[f"{API}/pulls/8"] = _pr(8, "2026-07-01T00:00:00Z")
    routes[f"{API}/pulls/12"] = _pr(12, None, merged=False)
    return routes


class TestUntilSearch:
    def test_until_pages_the_search_api_and_fetches_each_pr(self, tmp_path, capsys):
        session = FakeSession(_window_routes())
        args, code, out = _run(tmp_path, capsys, session=session, since="2026-07-01", until="2026-09-30")
        assert code == 0
        assert [line.split(":")[0] for line in out.splitlines()[:-1]] == ["#10", "#9", "#8"]
        assert f"{API}/pulls" not in session.urls
        assert session.urls[0] == SEARCH
        assert session.params[0] == {
            "q": "repo:o/r is:pr is:merged merged:2026-07-01..2026-09-30", "sort": "created", "order": "desc",
            "per_page": "100", "page": "1",
        }
        assert {f"{API}/pulls/{n}" for n in (8, 9, 10, 12)} <= set(session.urls)
        meta = json.loads((Path(args.out) / "mine.json").read_text())
        assert (meta["since"], meta["until"], meta["reviewers"], meta["pr_numbers"]) == (
            "2026-07-01", "2026-09-30", "any", None)

    def test_without_since_the_window_opens_with_a_star(self, tmp_path, capsys):
        session = FakeSession(_window_routes())
        _run(tmp_path, capsys, session=session, until="2026-09-30", prs=1)
        assert session.params[0]["q"] == "repo:o/r is:pr is:merged merged:*..2026-09-30"
        assert f"{API}/pulls/9" not in session.urls

    def test_a_window_over_the_search_cap_warns(self, tmp_path, capsys, caplog):
        routes = _window_routes()
        routes[SEARCH] = _hits(10, total=1500)
        with caplog.at_level(logging.WARNING):
            _, code, out = _run(tmp_path, capsys, session=FakeSession(routes), until="2026-09-30")
        assert code == 0 and out.startswith("#10:")
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "1500" in text and "1000 newest-created" in text and "--since" in text

    def test_a_search_rate_limit_stops_with_what_was_kept(self, tmp_path, capsys, caplog):
        routes = _window_routes()
        routes[SEARCH] = (403, {"message": "rate limit"})
        with caplog.at_level(logging.WARNING):
            _, code, out = _run(tmp_path, capsys, session=FakeSession(routes), until="2026-09-30")
        assert code == 0 and out.startswith("cases: ")
        assert any("rate limit" in r.getMessage() for r in caplog.records)

    def test_a_search_that_cannot_run_exits_2_naming_repo(self, tmp_path):
        routes = _window_routes()
        routes[SEARCH] = (422, {"message": "Validation Failed"})
        with pytest.raises(ConfigError, match="--repo"):
            mine(_args(tmp_path, until="2026-09-30"), session=FakeSession(routes))


class TestReviewers:
    def _routes(self):
        routes = _routes()
        routes[f"{API}/pulls/10/comments"] = [
            _comment(101, "bob", "a.py", 10, C1, "Handle the empty case.", assoc="MEMBER"),
            _comment(102, "dan", "a.py", 10, C1, "Done.", parent=101, assoc="NONE"),
            _comment(106, "carol", "b.py", 5, C1, "Rename this.", assoc="CONTRIBUTOR"),
            _comment(107, "erin", "a.py", 30, C1, "Drive-by.", assoc="NONE"),
            _comment(108, "frank", "a.py", 20, C2, "Owner note.", assoc="OWNER"),
            _comment(109, "gina", "a.py", 21, C2, "Collab note.", assoc="COLLABORATOR"),
        ]
        return routes

    def test_maintainers_drops_contributor_and_none_roots_but_keeps_replies_on_a_kept_root(self, tmp_path, capsys):
        args, _, _ = _run(tmp_path, capsys, session=FakeSession(self._routes()), reviewers="maintainers", prs=1)
        labels = {lb["id"]: lb["text"] for c in _cases(args) for lb in c["expected"]}
        assert set(labels) == {"c101", "c108", "c109"}
        assert labels["c101"].endswith("Reply from dan: Done.")
        meta = json.loads((Path(args.out) / "mine.json").read_text())
        assert meta["reviewers"] == "maintainers"

    def test_any_keeps_every_human_root(self, tmp_path, capsys):
        args, _, _ = _run(tmp_path, capsys, session=FakeSession(self._routes()), prs=1)
        assert {lb["id"] for c in _cases(args) for lb in c["expected"]} == {
            "c101", "c106", "c107", "c108", "c109"}

    def test_min_comments_counts_only_maintainer_roots(self, tmp_path, capsys):
        _, _, out = _run(tmp_path, capsys, session=FakeSession(self._routes()), reviewers="maintainers",
                         min_comments=4, prs=1)
        assert out.startswith("cases: ")


class TestPrList:
    def test_pr_mines_the_listed_prs_and_skips_an_unmerged_one(self, tmp_path, capsys, caplog):
        session = FakeSession(_window_routes())
        with caplog.at_level(logging.WARNING):
            args, code, out = _run(tmp_path, capsys, session=session, pr="8, 12,10,10")
        assert code == 0
        assert [line.split(":")[0] for line in out.splitlines()[:-1]] == ["#10", "#8"]
        assert SEARCH not in session.urls and f"{API}/pulls" not in session.urls
        assert any("#12" in r.getMessage() and "not a merged" in r.getMessage() for r in caplog.records)
        meta = json.loads((Path(args.out) / "mine.json").read_text())
        assert (meta["pr_numbers"], meta["prs_requested"]) == ([8, 12, 10], 3)

    def test_min_comments_still_applies_to_listed_prs(self, tmp_path, capsys):
        _, _, out = _run(tmp_path, capsys, session=FakeSession(_window_routes()), pr="10,8", min_comments=2)
        assert [line.split(":")[0] for line in out.splitlines()[:-1]] == ["#10"]

    def test_pr_with_since_exits_2_naming_pr(self, tmp_path):
        with pytest.raises(ConfigError, match="--pr.*--since"):
            mine(_args(tmp_path, pr="10", since="2026-01-01"), session=FakeSession(_window_routes()))


class TestBaseFallback:
    def _routes(self, *, default_ok=True):
        routes = _routes()
        gone = _pr(10, "2026-09-20T00:00:00Z", updated="2026-09-24T00:00:00Z")
        gone["base"] = {"ref": "master", "sha": "a" * 40}
        routes[f"{API}/pulls"] = [gone]
        routes[f"{API}/compare/master...{C1}"] = (404, {"message": "Not Found"})
        routes[f"{API}/compare/master...{C2}"] = (404, {"message": "Not Found"})
        if default_ok:
            routes[API] = {"default_branch": "main"}
        return routes

    def test_a_renamed_base_falls_back_to_the_default_branch_read_once(self, tmp_path, capsys):
        session = FakeSession(self._routes())
        args, _, out = _run(tmp_path, capsys, session=session)
        assert out.startswith("#10: 2 cases, 4 labels")
        assert {c["base_sha"] for c in _cases(args)} == {MERGE_BASE}
        assert session.urls.count(API) == 1

    def test_the_base_sha_is_the_last_resort(self, tmp_path, capsys):
        routes = self._routes(default_ok=False)
        for commit in (C1, C2):
            routes[f"{API}/compare/{'a' * 40}...{commit}"] = {"merge_base_commit": {"sha": MERGE_BASE}, "files": []}
        _, _, out = _run(tmp_path, capsys, session=FakeSession(routes))
        assert out.startswith("#10: 2 cases, 4 labels")

    def test_every_base_missing_skips_the_commit_with_a_warning(self, tmp_path, capsys, caplog):
        routes = self._routes(default_ok=False)
        with caplog.at_level(logging.WARNING):
            _, code, out = _run(tmp_path, capsys, session=FakeSession(routes))
        assert code == 0 and out.startswith("cases: ")
        assert any("skipping commit" in r.getMessage() for r in caplog.records)

    def test_a_server_error_does_not_fall_back(self, tmp_path, capsys):
        routes = self._routes()
        routes[f"{API}/compare/master...{C1}"] = (500, {"message": "boom"})
        session = FakeSession(routes)
        _run(tmp_path, capsys, session=session)
        assert f"{API}/compare/main...{C1}" not in session.urls


NOW = 1_000_000
RATE_BODY = {"message": "API rate limit exceeded"}


def _limit(status=403, **headers):
    return (status, RATE_BODY, headers)


class TestRateLimit:
    def _pr10_routes(self, first):
        routes = _routes()
        routes[f"{API}/pulls/10"] = Replies(first, (200, _pr(10, "2026-09-20T00:00:00Z")))
        return routes

    def test_a_rate_limited_pr_fetch_waits_until_reset_and_is_mined(self, tmp_path, capsys, caplog, _no_sleep):
        routes = self._pr10_routes(_limit(**{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(NOW + 30)}))
        with caplog.at_level(logging.WARNING):
            args, code, out = _run(tmp_path, capsys, session=FakeSession(routes), pr="10")
        assert code == 0 and _no_sleep == [32]
        assert out.startswith("#10: 2 cases")
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "GitHub rate limit reached; waiting 32 s until it resets" in text and "skipped" not in text
        assert json.loads((Path(args.out) / "mine.json").read_text())["stopped"] is None

    def test_a_403_without_rate_limit_signals_is_an_ordinary_skip(self, tmp_path, capsys, caplog, _no_sleep):
        routes = self._pr10_routes((403, {"message": "Resource not accessible"}, {"X-RateLimit-Remaining": "4000"}))
        with caplog.at_level(logging.WARNING):
            args, code, out = _run(tmp_path, capsys, session=FakeSession(routes), pr="10")
        assert code == 0 and out.startswith("cases: ") and _no_sleep == []
        assert any("skipped: cannot read the pull request" in r.getMessage() for r in caplog.records)
        assert json.loads((Path(args.out) / "mine.json").read_text())["stopped"] is None

    def test_a_429_waits_for_retry_after(self, tmp_path, capsys, _no_sleep):
        routes = self._pr10_routes(_limit(429, **{"Retry-After": "5"}))
        _, _, out = _run(tmp_path, capsys, session=FakeSession(routes), pr="10")
        assert _no_sleep == [5] and out.startswith("#10:")

    def test_an_unknown_wait_stops_the_walk_and_keeps_what_was_mined(self, tmp_path, capsys, caplog, _no_sleep):
        routes = _routes()
        routes[f"{API}/pulls/9"] = (403, RATE_BODY)
        routes[f"{API}/pulls/10"] = _pr(10, "2026-09-20T00:00:00Z")
        with caplog.at_level(logging.WARNING):
            args, code, out = _run(tmp_path, capsys, session=FakeSession(routes), pr="10,9,8")
        assert code == 0 and _no_sleep == []
        lines = out.splitlines()
        assert lines[0].startswith("#10:") and "#8:" not in out and lines[-1].startswith("cases: ")
        meta = json.loads((Path(args.out) / "mine.json").read_text())
        assert meta["stopped"] == "rate_limit" and [pr["number"] for pr in meta["prs"]] == [10]
        assert (Path(args.out) / "severity-review.md").exists() and len(_cases(args)) == 2
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "stopped on the GitHub rate limit" in text and "1 PRs mined" in text

    def test_a_wait_over_the_cap_stops_instead_of_sleeping(self, tmp_path, capsys, _no_sleep):
        routes = self._pr10_routes(_limit(**{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(NOW + 7200)}))
        args, code, _ = _run(tmp_path, capsys, session=FakeSession(routes), pr="10")
        assert code == 0 and _no_sleep == []
        assert json.loads((Path(args.out) / "mine.json").read_text())["stopped"] == "rate_limit"

    def test_the_fourth_consecutive_wait_for_one_request_stops(self, tmp_path, capsys, _no_sleep):
        routes = _routes()
        routes[f"{API}/pulls/10"] = _limit(429, **{"Retry-After": "1"})
        args, code, _ = _run(tmp_path, capsys, session=FakeSession(routes), pr="10")
        assert code == 0 and _no_sleep == [1, 1, 1]
        assert json.loads((Path(args.out) / "mine.json").read_text())["stopped"] == "rate_limit"

    def test_a_rate_limit_inside_the_comments_page_walk_is_waited_out(self, tmp_path, capsys, _no_sleep):
        routes = _routes()
        routes[f"{API}/pulls/10/comments"] = Replies(
            _limit(**{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(NOW + 1)}),
            (200, _routes()[f"{API}/pulls/10/comments"]),
        )
        _, _, out = _run(tmp_path, capsys, session=FakeSession(routes))
        assert _no_sleep == [3] and "#10: 2 cases, 4 labels" in out

    def test_a_walk_stopped_by_the_limit_keeps_the_newest_up_to_prs(self, tmp_path, capsys):
        routes = _routes()
        routes[f"{API}/pulls/8/comments"] = _limit()
        args, code, out = _run(tmp_path, capsys, session=FakeSession(routes), prs=2)
        assert code == 0 and "#10:" in out and "#9:" in out and "#8:" not in out

    def test_review_comments_below_min_comments_never_requests_the_comments(self, tmp_path, capsys):
        routes = _routes()
        full = _pr(10, "2026-09-20T00:00:00Z")
        full["review_comments"] = 1
        routes[f"{API}/pulls/10"] = full
        session = FakeSession(routes)
        _, code, out = _run(tmp_path, capsys, session=session, pr="10", min_comments=3)
        assert code == 0 and out.startswith("cases: ")
        assert f"{API}/pulls/10/comments" not in session.urls

    def test_review_comments_at_or_above_min_comments_still_fetches(self, tmp_path, capsys):
        routes = _routes()
        full = _pr(10, "2026-09-20T00:00:00Z")
        full["review_comments"] = 3
        routes[f"{API}/pulls/10"] = full
        session = FakeSession(routes)
        _, _, out = _run(tmp_path, capsys, session=session, pr="10", min_comments=3)
        assert f"{API}/pulls/10/comments" in session.urls and out.startswith("#10:")
