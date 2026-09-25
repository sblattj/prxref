"""Capture the issue #17 D1 golden: the ordered worker and sweep prompts of a review with the feature off.

Run it against the RELEASED prxref 0.15.0, from a directory OUTSIDE the
repository, so the tree's ``src/`` is not on ``sys.path``::

    cd /tmp && uv run --no-project --with prxref==0.15.0 \\
        python /abs/path/to/tests/fixtures/issue17/make_golden.py OUT.json

It drives ``harness.py`` beside it, which uses only API that exists in both
0.15.0 and 0.16.0, once per design point with ``max_workers=1``, and writes
the prompts plus the forge's read and listing calls as ASCII JSON. It prints
``prxref.__version__`` and ``prxref.__file__``: an installed release lives in
site-packages, never under ``src/prxref/``, so ``__file__`` says which prxref
ran. The file records the version and whether ``__file__`` was an installed
distribution, never the path itself.

``tests/test_issue_17_acceptance.py`` replays the same harness at the tip
with ``repo_context="off"`` and compares byte for byte. Never regenerate the
golden from the tree: a difference is a D1 finding, not a stale file.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path


def _harness():
    path = Path(__file__).resolve().parent / "harness.py"
    spec = importlib.util.spec_from_file_location("issue17_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str]) -> int:
    """Write the golden to ``argv[1]``; returns the exit status."""
    if len(argv) != 2:
        print("usage: make_golden.py OUT.json", file=sys.stderr)
        return 2
    for name in [name for name in os.environ if name.startswith("PRXREF_")]:
        del os.environ[name]
    import prxref

    print(f"prxref.__version__ = {prxref.__version__}")
    print(f"prxref.__file__ = {prxref.__file__}")
    harness = _harness()
    installed = f"{os.sep}src{os.sep}prxref{os.sep}" not in prxref.__file__
    golden = {
        "prxref_version": prxref.__version__,
        "generated_from": "installed distribution" if installed else "source tree",
        "runs": {point: harness.golden_run(point) for point in harness.DESIGN_POINTS},
    }
    Path(argv[1]).write_text(
        json.dumps(golden, indent=1, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8",
    )
    for point, run in golden["runs"].items():
        prompts, reads, lists = len(run["prompts"]), run["content_calls"], run["list_calls"]
        print(f"{point}: {prompts} prompts, content_calls={reads}, list_calls={lists}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
