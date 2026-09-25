"""Builds the model's conversation history from the message table."""
from assistant.messages import MessageTable

HISTORY_WINDOW = 20

_MODEL_ROLES = {"user": "user", "assistant": "assistant", "tool": "tool"}


def model_history(table: MessageTable, session_id: str) -> list[dict]:
    history = []
    for m in table.recent(session_id, HISTORY_WINDOW):
        role = _MODEL_ROLES.get(m.role)
        if role is None:
            continue
        history.append({"role": role, "content": m.text})
    return history
