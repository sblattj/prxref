from assistant.engine import Engine, Step
from assistant.messages import MessageTable
from assistant.session import Frame, Run
from assistant.state_store import StateStore


def make_run():
    return Run("s1", "t1", [Frame("root")])


def test_turn_returns_last_answer():
    engine = Engine(MessageTable(), StateStore())
    steps = [Step("tool", "lookup", lambda r: "found"), Step("model", "reply", lambda r: "done")]
    assert engine.run_turn(make_run(), steps) == "done"


def test_handoff_parks_and_resumes():
    store = StateStore()
    engine = Engine(MessageTable(), store)
    run = make_run()
    run.root().data["attempts"] = 1
    assert engine.run_turn(run, [Step("handoff", "human", lambda r: "")]) is None
    assert store.load("s1").root().data == {"attempts": 1}
