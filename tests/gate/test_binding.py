"""The live binding guard (#102)."""

import pytest

from prxref.gate import binding
from prxref.gate.checks import Stale
from prxref.gate.store import Store

REPO, HEAD, BASE = "acme/widget", "h" * 40, "b" * 40


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "gate.db")
    yield s
    s.close()


@pytest.fixture
def gen(store):
    return store.enqueue(REPO, 7, HEAD, BASE, "main", "opened")


def _live(**over):
    fields = {"head_sha": HEAD, "base_sha": BASE, "base_ref": "main", "state": "open", "merged": False}
    return binding.LivePR(**{**fields, **over})


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def request(self, method, repo, path, **kwargs):
        self.calls.append((method, repo, path))
        return self.payload


def _pull(head=HEAD, base=BASE, ref="main", state="open"):
    return {"head": {"sha": head}, "base": {"sha": base, "ref": ref}, "state": state, "merged": False}


def test_read_live_pr_parses_the_pull(store):
    client = _Client(_pull())
    assert binding.read_live_pr(client, REPO, 7) == _live()
    assert client.calls == [("GET", REPO, "/pulls/7")]


def test_a_matching_open_pr_is_bound(store, gen):
    assert binding.check_binding(gen, _live(), store) is None


@pytest.mark.parametrize(
    "over, reason",
    [
        ({"state": "closed"}, "closed"),
        ({"state": "closed", "merged": True}, "closed"),
        ({"head_sha": "c" * 40}, "head moved"),
        ({"base_ref": "release"}, "retargeted"),
        ({"base_sha": "d" * 40}, "base moved"),
    ],
)
def test_a_drifted_pr_is_not_bound(store, gen, over, reason):
    assert binding.check_binding(gen, _live(**over), store) == reason


def test_a_newer_generation_supersedes_even_on_the_same_head(store, gen):
    store.enqueue(REPO, 7, HEAD, BASE, "main", "rerequested")
    assert binding.check_binding(gen, _live(), store) == "superseded"


def test_guard_raises_stale_with_the_reason(store, gen):
    guard = binding.make_guard(_Client(_pull(head="c" * 40)), store)
    with pytest.raises(Stale) as err:
        guard(gen)
    assert err.value.reason == "head moved"


def test_guard_passes_a_bound_generation(store, gen):
    assert binding.make_guard(_Client(_pull()), store)(gen) is None
