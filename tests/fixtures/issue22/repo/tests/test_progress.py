from assistant.engine import Engine, Step
from assistant.messages import MessageTable
from assistant.session import Frame, Run
from assistant.state_store import StateStore


def test_each_step_is_announced_once(monkeypatch):
    monkeypatch.setenv("ASSISTANT_PROGRESS_NOTES", "true")
    table = MessageTable()
    engine = Engine(table, StateStore())
    run = Run("s1", "t1", [Frame("root")])
    steps = [
        Step("tool", "find_order", lambda r: "order 7"),
        Step("model", "reply", lambda r: "thinking"),
        Step("model", "reply", lambda r: "your order shipped"),
    ]
    assert engine.run_turn(run, steps) == "your order shipped"
    notes = [m.text for m in table.recent("s1", 50) if m.role == "progress"]
    assert notes == ["Running find order...", "Thinking about your request..."]
