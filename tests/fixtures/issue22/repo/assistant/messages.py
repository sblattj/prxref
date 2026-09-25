"""Append-only message table for a session."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    session_id: str
    role: str  # "user" | "assistant" | "tool"
    text: str


class MessageTable:
    def __init__(self) -> None:
        self._rows: list[Message] = []

    def append(self, message: Message) -> None:
        self._rows.append(message)

    def recent(self, session_id: str, limit: int) -> list[Message]:
        rows = [m for m in self._rows if m.session_id == session_id]
        return rows[-limit:]
