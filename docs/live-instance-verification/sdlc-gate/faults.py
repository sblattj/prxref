"""Phase 4 fault injection against the live gate (#102). Prints no secret.

  send   --env-file F --kind {bad-signature,duplicate,rerequest} --repo R --pr N --head SHA --base SHA
         --installation I --app-id I --out FILE
  serve-502 --env-file F   run the gate in-process; the first POST /check-runs gets a 502
                           after GitHub accepted it (the reply is lost, the write is not)
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import uuid
from pathlib import Path

import requests

from prxref.gate.settings import WEBHOOK_SECRET_ENV, apply_env_file, load_env_file

URL = "http://127.0.0.1:8787/webhook"


def _post(event: str, payload: dict, secret: str, delivery: str) -> dict:
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    r = requests.post(URL, data=body, timeout=10, headers={
        "X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": sig,
        "Content-Type": "application/json"})
    return {"event": event, "delivery": delivery, "status": r.status_code, "reply": r.json()}


def send(a: argparse.Namespace) -> dict:
    secret = load_env_file(Path(a.env_file))[WEBHOOK_SECRET_ENV]
    pr = {"action": "synchronize", "number": a.pr, "repository": {"full_name": a.repo},
          "installation": {"id": a.installation},
          "pull_request": {"number": a.pr, "head": {"sha": a.head}, "base": {"sha": a.base, "ref": "main"}}}
    if a.kind == "bad-signature":
        return {"cases": [_post("pull_request", pr, secret + "-wrong", str(uuid.uuid4()))]}
    if a.kind == "duplicate":
        d = str(uuid.uuid4())
        return {"cases": [_post("pull_request", pr, secret, d), _post("pull_request", pr, secret, d)]}
    run = {"action": "rerequested", "repository": {"full_name": a.repo}, "installation": {"id": a.installation},
           "check_run": {"name": "sdlc-review", "head_sha": a.head, "app": {"id": a.app_id},
                         "pull_requests": [{"number": a.pr, "base": {"sha": a.base, "ref": "main"}}]}}
    return {"cases": [_post("check_run", run, secret, str(uuid.uuid4()))]}


class _LoseFirstCheckReply(requests.Session):
    lost = False

    def request(self, method, url, *args, **kwargs):
        resp = super().request(method, url, *args, **kwargs)
        if method == "POST" and url.endswith("/check-runs") and not self.lost:
            type(self).lost = True
            fake = requests.Response()
            fake.status_code, fake._content, fake.url = 502, b'{"message": "injected"}', url
            print(f"injected 502 after GitHub answered {resp.status_code}", file=sys.stderr)
            return fake
        return resp


def serve_502(a: argparse.Namespace) -> int:
    from prxref.config import load_config
    from prxref.gate import serve
    from prxref.gate.settings import settings_from_config

    apply_env_file(Path(a.env_file))
    os.environ.setdefault("PRXREF_RULES_SOURCE", "base")
    cfg = load_config()
    return serve.run_gate(serve.build(settings_from_config(cfg), cfg, session=_LoseFirstCheckReply()),
                          "127.0.0.1", 8787)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["send", "serve-502"])
    p.add_argument("--env-file", required=True)
    p.add_argument("--kind", choices=["bad-signature", "duplicate", "rerequest"])
    p.add_argument("--repo")
    p.add_argument("--pr", type=int)
    p.add_argument("--head")
    p.add_argument("--base")
    p.add_argument("--installation", type=int)
    p.add_argument("--app-id", type=int)
    p.add_argument("--out")
    a = p.parse_args(argv)
    if a.command == "serve-502":
        return serve_502(a)
    Path(a.out).write_text(json.dumps(send(a), indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
