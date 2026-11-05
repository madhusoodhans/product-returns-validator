#!/bin/bash
# Checks the running demo end to end: GET /health, then sample conversations through POST /api/chat (the same HTTP
# and SSE path the page uses), every turn's verdict compared with data/scenarios.json. Exit code 0 = all passed.
#   deploy/smoke_test.sh          three samples: an approved return, an order outside the window, a jailbreak (~15 s)
#   deploy/smoke_test.sh --all    all 19 samples (~2 min; about 90 hosted API calls)
# HOST / PORT: the demo server (default 127.0.0.1:8602); PYTHON (default .venv/bin/python).
# The key: like a visitor, it is sent to POST /api/key (from VLLM_SR_API_KEY, else ~/.vllm-sr-key; never printed).
# A server with its own env key needs none.
set -uo pipefail
cd "$(dirname "$0")/.."
exec "${PYTHON:-.venv/bin/python}" -I - "http://${HOST:-127.0.0.1}:${PORT:-8602}" "$@" <<'PY'
import json
import os
import sys
import uuid
from pathlib import Path

import requests

base, args = sys.argv[1], sys.argv[2:]
fails = 0


def result(ok, what):
    global fails
    fails += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {what}", flush=True)


print(f"Smoke test against {base}")
try:
    h = requests.get(f"{base}/health", timeout=120).json()
except Exception as e:  # noqa: BLE001
    print(f"  FAIL  GET /health: {e}\nIs the demo server running? (./start_server.sh --bg)")
    sys.exit(1)
parts = {k: v for k, v in h.items() if isinstance(v, bool) and k != "ok"}
detail = h.get("detail") or {}
result(h.get("ok") is True, "GET /health " + ", ".join(f"{k} {'up' if v else 'DOWN'}" for k, v in parts.items())
       + "".join(f"\n        {k}: {detail[k]}" for k, v in parts.items() if not v and k in detail))
if h.get("ok") is not True:
    sys.exit(1)

visitor = {"x-visitor-id": str(uuid.uuid4())}
key = os.environ.get("VLLM_SR_API_KEY", "").strip()
if not key and Path("~/.vllm-sr-key").expanduser().is_file():
    key = Path("~/.vllm-sr-key").expanduser().read_text().strip()
state = requests.get(f"{base}/api/key", headers=visitor, timeout=10).json()
if key:
    r = requests.post(f"{base}/api/key", json={"key": key}, headers=visitor, timeout=70)
    result(r.ok, "POST /api/key " + ("ok" if r.ok else f"refused: {r.json().get('code')}"))
    if not r.ok:
        sys.exit(1)
elif not state.get("server_key"):
    result(False, "no API key: set VLLM_SR_API_KEY or put it in ~/.vllm-sr-key")
    sys.exit(1)

groups = json.load(open("data/scenarios.json"))["groups"]
scenarios = [sc for g in groups for sc in g["scenarios"]]
if "--all" not in args:
    scenarios = [sc for sc in scenarios if sc["id"] in ("v-phone", "i-window", "m-jailbreak")]

for sc in scenarios:
    session = str(uuid.uuid4())
    got, ended, err = [], None, None
    for i, t in enumerate(sc["turns"]):
        verdict = None
        try:
            with requests.post(f"{base}/api/chat", json={"session": session, "text": t.get("text"), "image": t.get("image"),
                                                      "has_turns": i > 0}, headers=visitor, stream=True,
                               timeout=(5, 180)) as r:
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if not line.startswith("data:"):
                        continue
                    ev = json.loads(line[5:])
                    if ev["type"] == "message":
                        verdict, ended = ev["verdict"], ev.get("ended")
                    elif ev["type"] in ("error", "key_error"):
                        err = ev["message"]
        except Exception as e:  # noqa: BLE001
            err = f"request failed: {e}"
        got.append(verdict)
        if err:
            break
    want = [t["expect"] for t in sc["turns"]]
    end = f", chat ended: {ended['title']}" if ended else ""
    result(got == want and not err, f"{sc['title']}: {' -> '.join(map(str, got))}{end}"
           + (f"  (expected {' -> '.join(want)})" if got != want else "") + (f"  ERROR {err}" if err else ""))

print(f"\n{'ALL PASSED' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
PY
