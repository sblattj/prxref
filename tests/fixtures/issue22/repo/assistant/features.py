"""Feature toggles, read from the environment at startup."""
import os


def enabled(name: str, default: bool) -> bool:
    raw = os.environ.get(f"ASSISTANT_{name.upper()}")
    return default if raw is None else raw.lower() == "true"
