"""Live state of one run."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Frame:
    """One agent working on the request. The bottom frame is the root agent."""

    agent_id: str
    # Scratch space any engine component can use for per-frame bookkeeping.
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Run:
    session_id: str
    turn_id: str
    frames: list[Frame] = field(default_factory=list)

    def root(self) -> Frame:
        return self.frames[0]

    def current(self) -> Frame:
        return self.frames[-1]
