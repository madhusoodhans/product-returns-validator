"""Run the 36 sample-scenario turns through the REAL flow, for variant A or B.

  A  = shipped flow: D3 (System One) extracts, code rules decide, Qwen words.      (System 1 extract + System 1 decide + LLM)
  B  = same flow, extractor swapped to a Qwen LLM (d3_llm shim); code rules decide. (System 1 decide + LLM, no System 1 extract)

Variant C (LLM only, Qwen 27B decides everything) is bench/baseline_27b.py.

Only the EXTRACTOR backend changes between A and B; every decision rule, threshold
and prompt in app/ is identical, so the delta is purely System-1-extract vs LLM-extract.

Model time + tokens per turn come from the flow's own `calls` ledger (each model
request, server-measured, one at a time), the same accounting the live app shows.

Usage: python run_variant.py {A|B}  [scenario-id ...]
Writes results_<variant>.json and prints a per-turn + summary table.
"""
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(os.environ.get("PRV_REPO", Path.home() / "product-returns-validator"))
sys.path.insert(0, str(REPO / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

VARIANT = (sys.argv[1] if len(sys.argv) > 1 else "A").upper()
WANT = set(sys.argv[2:])
assert VARIANT in ("A", "B"), "variant must be A or B"

import flow, claim, eligibility, photo, turn_checks  # noqa: E402

if VARIANT == "B":
    import d3_llm
    # swap the extractor in every module that bound `d3` by name
    for mod in (claim, eligibility, photo, turn_checks):
        if hasattr(mod, "d3"):
            mod.d3 = d3_llm.d3
    import models
    models.d3 = d3_llm.d3
    # Disable the Vela router preview: Vela is itself a System-1 decision model
    # (guardrail + 9B/27B routing). Leaving it on would mean B still uses a
    # System-1 extractor. With the preview off, the flow's documented fallback
    # runs guardrail via scope_check and model choice via turn_checks.route,
    # both now LLM-backed, so ALL extraction in B is done by the LLM.
    turn_checks.router_preview = lambda messages: (None, 0.0)
    flow.turn_checks = turn_checks

groups = json.loads((REPO / "data/scenarios.json").read_text())["groups"]
rows = []
for g in groups:
    for sc in g["scenarios"]:
        if WANT and sc["id"] not in WANT:
            continue
        s = flow.Session(sc["id"])
        for i, t in enumerate(sc["turns"]):
            t0 = time.perf_counter()
            msg, calls = None, []
            for ev in s.turn(t.get("text"), t.get("image")):
                if ev["type"] == "message":
                    msg = ev
                if ev["type"] == "done":
                    calls = ev.get("calls") or []
            wall = round((time.perf_counter() - t0) * 1000, 1)
            model_ms = round(sum(c[1] for c in calls), 1)
            tokens = sum(c[2] for c in calls)
            ncalls = len(calls)
            ok = msg["verdict"] == t["expect"]
            rows.append({"scenario": sc["id"], "turn": i, "expect": t["expect"],
                         "verdict": msg["verdict"], "ok": ok, "wall_ms": wall,
                         "model_ms": model_ms, "tokens": tokens, "calls": ncalls,
                         "image": bool(t.get("image"))})
            print(f"{'ok ' if ok else 'BAD'} {sc['id']:14s} t{i} want {t['expect']:11s} got {str(msg['verdict']):11s} "
                  f"{model_ms/1000:5.2f}s model {tokens:6d}tok {ncalls}calls", flush=True)

ok = sum(r["ok"] for r in rows)
summary = {"variant": VARIANT, "turns": len(rows), "correct": ok,
           "model_seconds": round(sum(r["model_ms"] for r in rows) / 1000, 2),
           "wall_seconds": round(sum(r["wall_ms"] for r in rows) / 1000, 2),
           "tokens": sum(r["tokens"] for r in rows),
           "total_model_calls": sum(r["calls"] for r in rows),
           "chats": len({r["scenario"] for r in rows}),
           "measured": time.strftime("%Y-%m-%d %H:%M"),
           "rows": rows}
out = Path(__file__).resolve().parent / f"results_{VARIANT}.json"
out.write_text(json.dumps(summary, indent=1))
print(f"\nVariant {VARIANT}: {ok}/{len(rows)} correct | model {summary['model_seconds']}s "
      f"wall {summary['wall_seconds']}s | {summary['tokens']} tok | {summary['total_model_calls']} calls | -> {out.name}")
