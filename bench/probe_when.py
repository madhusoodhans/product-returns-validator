"""Phase 0: wordings for 'when / who caused the damage' on d3-mini."""
import json, sys
sys.path.insert(0, __import__("os").path.dirname(__file__))
from probe_eligibility import COMPLAINTS, state, auc
import requests
URL, MODEL = "http://localhost:8899/v1/systemone", sys.argv[1] if len(sys.argv) > 1 else "vllm-sr/d3-mini"
V = {
 "c_arrived": "The customer says the product was already damaged when it was delivered or unpacked.",
 "c_after": "The customer says the damage happened after delivery.",
 "c_fault": "The customer, someone in their home, a pet or an airline damaged the product.",
 "c_use": "The product got damaged while it was being used, worn, washed, dropped, assembled or taken on a trip.",
}
WHEN = {"type": "choice", "instructions": "When did the problem start, according to the customer?",
        "criteria": {"arrival": "It was already there when the product was delivered or unpacked",
                     "after": "It started after delivery, while the product was being used, worn, washed, dropped, assembled or taken on a trip",
                     "not_said": "The customer does not say when it started"}}
rows = []
for c in COMPLAINTS:
    q = {k: {"type": "noul", "instructions": v} for k, v in V.items()}; q["when"] = WHEN
    a = requests.post(URL, json={"model": MODEL, "state": state(c), "questions": q}, timeout=60).json()["answers"]
    rows.append((c, a))
after = lambda c: bool(c["caused"] or c["wear"])
for k in V:
    pos = [a[k]["noul"] for c, a in rows if after(c)]; neg = [a[k]["noul"] for c, a in rows if not after(c)]
    if k == "c_arrived":
        pos, neg = neg, pos
        pos = [a[k]["noul"] for c, a in rows if c["outcome"] == "eligible"]; neg = [a[k]["noul"] for c, a in rows if after(c)]
    acc = sum((a[k]["noul"] >= .5) == (after(c) if k != "c_arrived" else not after(c)) for c, a in rows)
    print(f"{k:10s} AUC {auc(pos, neg):.3f} acc@0.5 {acc}/{len(rows)}  pos med {sorted(pos)[len(pos)//2]:.2f} neg med {sorted(neg)[len(neg)//2]:.2f}")
ch = [(c, a["when"]["choice"]) for c, a in rows]
print("when choice: after-gold ->", [w for c, w in ch if after(c)].count("after"), "/", sum(after(c) for c, _ in ch),
      "| eligible-gold -> arrival", [w for c, w in ch if c["outcome"] == "eligible"].count("arrival"), "/", sum(c["outcome"] == "eligible" for c, _ in ch))
for c, w in ch:
    if (after(c) and w != "after") or (c["outcome"] == "eligible" and w != "arrival"):
        print("  miss", c["id"], w, c["text"][:70])
