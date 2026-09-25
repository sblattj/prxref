import pytest


@pytest.fixture(autouse=True)
def _progress_notes_off(monkeypatch):
    # Keep exact message-table assertions deterministic; progress tests opt in.
    monkeypatch.setenv("ASSISTANT_PROGRESS_NOTES", "false")
