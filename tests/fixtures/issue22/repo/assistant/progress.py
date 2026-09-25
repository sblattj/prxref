"""Progress notes: a short user-facing line announced before each step runs.

Each note is saved as a message (role "progress") so the transcript holds exactly what the
user saw. A per-turn ledger on the root frame drops a note that repeats one already shown.
"""
from assistant.features import enabled
from assistant.messages import Message, MessageTable
from assistant.session import Run

LEDGER_KEY = "progress.ledger"


class ProgressLedger:
    """Notes shown to the user during one turn."""

    def __init__(self, turn_id: str) -> None:
        self._turn_id = turn_id
        self._lines: list[str] = []

    def turn_id(self) -> str:
        return self._turn_id

    def record(self, line: str) -> bool:
        key = line.strip().lower()
        if any(shown.strip().lower() == key for shown in self._lines):
            return False
        self._lines.append(line)
        return True


class ProgressAnnouncer:
    def __init__(self, table: MessageTable) -> None:
        self.table = table
        self.active = enabled("progress_notes", default=True)

    def announce(self, run: Run, text: str) -> None:
        if not self.active:
            return
        if self._ledger(run).record(text):
            self.table.append(Message(run.session_id, "progress", text))

    def _ledger(self, run: Run) -> ProgressLedger:
        data = run.root().data
        ledger = data.get(LEDGER_KEY)
        if not isinstance(ledger, ProgressLedger) or ledger.turn_id() != run.turn_id:
            ledger = ProgressLedger(run.turn_id)
            data[LEDGER_KEY] = ledger
        return ledger
