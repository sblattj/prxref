"""Capture the issue #20 golden: the ordered worker and sweep prompts of the fixture review under 0.16.0.

Run it against the RELEASED prxref 0.16.0 from PyPI, in an isolated
environment and from a directory OUTSIDE the repository, so the tree's
``src/`` is never on ``sys.path``::

    cd /tmp && uv run --isolated --no-project --with prxref==0.16.0 \\
        python /abs/path/to/tests/fixtures/issue20/make_golden.py /abs/path/to/golden_0_16_0.json

It refuses to write anything unless ``prxref.__version__`` is ``0.16.0`` AND
``prxref.__file__`` lies under ``site-packages``: the tree reports the same
version until the release bumps it, so the version alone cannot tell an
installed release from the source tree.

It drives ``harness.py`` beside it, which uses only API that exists in both
0.16.0 and the tip, once per design point with ``max_workers=1``, and writes
the prompts plus the forge's read and listing calls as ASCII JSON. The file
records the version and the fact that an installed distribution ran, never
the path itself.

``tests/test_issue_20_acceptance.py`` replays the same harness at the tip and
compares: the Python control chunk and the sweep byte for byte, and every JVM
chunk after removing the dependency and definition blocks 0.16.0 never
rendered. Never regenerate the golden from the tree: a difference is a
regression finding, not a stale file.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

RELEASE = "0.16.0"
COMMAND = (
    "cd /tmp && uv run --isolated --no-project --with prxref==0.16.0 "
    "python tests/fixtures/issue20/make_golden.py golden_0_16_0.json (both paths absolute)"
)


def _harness():
    path = Path(__file__).resolve().parent / "harness.py"
    spec = importlib.util.spec_from_file_location("issue20_harness", path)
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
    if prxref.__version__ != RELEASE:
        raise SystemExit(f"refusing: prxref {prxref.__version__} is not the {RELEASE} release")
    if "/site-packages/" not in prxref.__file__:
        raise SystemExit("refusing: prxref was not imported from an installed distribution")
    harness = _harness()
    golden = {
        "prxref_version": prxref.__version__,
        "generated_from": "installed distribution",
        "command": COMMAND,
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
