"""Phase 0: outcome accuracy on the held-out complaints (written blind to the questions)."""
import json, sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_eligibility import ROOT, ask, outcome
rows = [json.loads(l) for l in (ROOT / "data/complaints/heldout.jsonl").read_text().splitlines() if l.strip()]
for model in sys.argv[1:] or ["d3-mini"]:
    res, conf = [], Counter()
    for c in rows:
        a, ms = ask(model, c)
        o = outcome(a); conf[(c["outcome"], o)] += 1
        res.append({"id": c["id"], "gold": c["outcome"], "got": o, "ms": ms, "answers": a})
    (ROOT / f"results/p0_eligibility/heldout_{model}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in res))
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
