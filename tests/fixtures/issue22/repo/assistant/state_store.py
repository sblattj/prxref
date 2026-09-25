"""Saves a run so it can resume after a human handoff."""
import json
from dataclasses import asdict

from assistant.session import Frame, Run


class StateStore:
    def __init__(self) -> None:
        self._blobs: dict[str, str] = {}

    def save(self, run: Run) -> None:
        self._blobs[run.session_id] = json.dumps(asdict(run))

    def load(self, session_id: str) -> Run | None:
        blob = self._blobs.get(session_id)
        if blob is None:
            return None
        raw = json.loads(blob)
        frames = [Frame(f["agent_id"], f["data"]) for f in raw["frames"]]
        return Run(raw["session_id"], raw["turn_id"], frames)
