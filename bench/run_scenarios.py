"""Plays data/scenarios.json through the real flow and checks every turn's verdict.

Usage: python bench/run_scenarios.py [scenario-id ...]
Hosted vllm-sr API (key: bench/hosted.py). A full run also records what vllm-sr/auto picked for every reply
(data/scenario_routes.json; the sample list shows it) and prints the routing per turn and the time per step.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "bench"))
import hosted  # noqa: E402
hosted.use_key()
from flow import NODES, Session  # noqa: E402

groups = json.loads((ROOT / "data/scenarios.json").read_text())["groups"]
want = set(sys.argv[1:])
bad = total = 0
routes = {}   # scenario id -> per turn {model, decision, reasoning} of the reply (None: no LLM wrote it)
step_ms = {n: [] for n in NODES}
turn_s = []
for g in groups:
    print(f"\n## {g['title']}")
    for sc in g["scenarios"]:
        if want and sc["id"] not in want:
            continue
        s = Session(sc["id"])
        print(f"- {sc['id']}: {sc['title']}")
        for t in sc["turns"]:
            t0 = time.perf_counter()
            msg, summ, route = None, {}, None
            for ev in s.turn(t.get("text"), t.get("image")):
                if ev["type"] == "step" and ev["status"] not in ("waiting", "running", "skipped"):
                    summ[ev["node"]] = ev["summary"]
                    if ev.get("ms") and ev["status"] != "failed":
                        step_ms[ev["node"]].append(ev["ms"])
                if ev["type"] == "message":
                    msg = ev
                if ev["type"] == "done":
                    route = ev.get("route")
            routes.setdefault(sc["id"], []).append(
                {k: route.get(k) for k in ("model", "decision", "reasoning", "effort")} if route else None)
            turn_s.append(time.perf_counter() - t0)
            total += 1
            ok = msg["verdict"] == t["expect"]
            bad += not ok
            what = t.get("text") or f"[{t['image'].split('/')[-1]}]"
            print(f"    {'ok ' if ok else 'BAD'} {msg['verdict']:12s} (want {t['expect']:12s}) {time.perf_counter() - t0:4.1f} s | {what[:60]}")
            if route:
                print(f"        router: {route.get('decision')} / reasoning {route.get('reasoning')} "
                      f"({route.get('effort')}) / {route.get('model')}")
            for n in ("guardrail", "eligibility", "image_check"):
                if n in summ:
                    print(f"        {n}: {summ[n]}")
            print(f"        reply: {msg['text']}")
print(f"\n{total - bad}/{total} turns as expected; {hosted.CALLS['n']} bench calls")
med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None
print("median ms per step:", {n: med(v) for n, v in step_ms.items() if v}, "| median turn", round(med(turn_s), 2), "s")
flat = [r for v in routes.values() for r in v if r]
print("router decisions:", {d: sum(r["decision"] == d for r in flat) for d in sorted({r["decision"] for r in flat if r["decision"]})},
      "reasoning on:", sum(r["reasoning"] == "on" for r in flat), "of", len(flat))
if not want:
    (ROOT / "data/scenario_routes.json").write_text(json.dumps(routes, indent=1) + "\n")
sys.exit(1 if bad else 0)
