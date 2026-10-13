"""Write one live-proof capture: ``gh api`` output plus its index entry (#102).

Usage: uv run python docs/live-instance-verification/sdlc-gate/capture.py PHASE NAME
       --repo R --pr N --head SHA --base SHA --ruleset-id I --app-id I [--check-run-id I]
       [--json FILE | -- GH_API_ARGS...]
``gh`` runs with the caller's environment (set ``GH_TOKEN`` there, never here).
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent


def main(argv: list[str]) -> int:
    split = argv.index("--") if "--" in argv else len(argv)
    p = argparse.ArgumentParser()
    p.add_argument("phase", choices=["1", "2", "3", "4"])
    p.add_argument("name")
    for flag in ("--repo", "--head", "--base"):
        p.add_argument(flag, required=True)
    for flag in ("--pr", "--ruleset-id", "--app-id"):
        p.add_argument(flag, type=int, required=True)
    p.add_argument("--check-run-id", type=int)
    p.add_argument("--json", help="a JSON file already captured (phase 4 harness output)")
    p.add_argument("--gate-version", default=subprocess.run(
        ["prxref", "--version"], capture_output=True, text=True).stdout.strip())
    args = p.parse_args(argv[:split])
    if args.json:
        payload = json.loads(Path(args.json).read_text(encoding="utf-8"))
    else:
        out = subprocess.run(["gh", "api", *argv[split + 1:]], capture_output=True, text=True, check=True)
        payload = json.loads(out.stdout)
    name = f"{args.phase}-{args.name}.json"
    (DIR / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    index_path = DIR / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    index = [e for e in index if (e["phase"], e["name"]) != (args.phase, args.name)]
    index.append({"phase": args.phase, "name": args.name, "file": name, "repo": args.repo, "pr": args.pr,
                  "head_sha": args.head, "base_sha": args.base, "ruleset_id": args.ruleset_id,
                  "app_id": args.app_id, "check_run_id": args.check_run_id, "gate_version": args.gate_version})
    index.sort(key=lambda e: (e["phase"], e["name"]))
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(f"captured {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
