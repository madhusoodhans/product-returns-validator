"""Outcome accuracy on the held-out complaints (written blind to the questions). Run once per model; never tune on it.

Usage: python bench/probe_heldout.py [d3-flash | d3-mini]   (d3-flash: hosted, results/d3_flash/eligibility_heldout.jsonl)
"""
import json, sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hosted
from probe_eligibility import ENDPOINTS, ROOT, ask, out_path, outcome
rows = [json.loads(l) for l in (ROOT / "data/complaints/heldout.jsonl").read_text().splitlines() if l.strip()]
for model in sys.argv[1:] or ["d3-flash"]:
    conf = Counter()
    def one(c):
        a, ms = ask(model, c)
        return {"id": c["id"], "gold": c["outcome"], "got": outcome(a), "ms": ms, "answers": a}
    res = hosted.pmap(one, rows) if ENDPOINTS[model][0] == "hosted" else [one(c) for c in rows]
    for r in res:
        conf[(r["gold"], r["got"])] += 1
    out_path(model, "heldout").write_text("".join(json.dumps(r) + "\n" for r in res))
    ok = sum(r["gold"] == r["got"] for r in res)
    print(f"\n== {model} held-out: {ok}/{len(res)}")
    labs = ["eligible", "not_eligible", "human", "clarify"]
    print("gold \\ got   " + " ".join(f"{l[:8]:>8s}" for l in labs))
    for g in labs:
        print(f"{g:12s} " + " ".join(f"{conf[(g, l)]:8d}" for l in labs))
    inj = [c for c in rows if "injection" in c.get("note", "").lower()]
    print("injection items:", [(c["id"], round(next(r for r in res if r["id"] == c["id"])["answers"]["injection"]["noul"], 2)) for c in inj])
    for r, c in zip(res, rows):
        if r["gold"] != r["got"]:
            a = r["answers"]
            print(f"  {c['id']:16s} gold {r['gold']:12s} got {r['got']:12s} issue={a['issue']['choice']} when={a['when']['choice']} | {c['text'][:90]} | {c.get('note','')[:60]}")
