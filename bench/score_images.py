"""Phase 0: verdict accuracy of the photo rule on results/p0_images/<model>.jsonl (no new D3 calls)."""
import json, sys, itertools
from collections import Counter
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
rows = [json.loads(l) for l in (ROOT / f"results/p0_images/{sys.argv[1] if len(sys.argv) > 1 else 'd3-mini'}.jsonl").read_text().splitlines()]
GOLD = {"damaged_clear": "accept", "blurry": "retake", "dark": "retake", "glare": "retake", "cut_off": "retake",
        "hand": "retake", "no_damage": "no_damage", "wrong_product": "mismatch"}
Q = ["blurry", "dark", "glare", "covered", "cut_off"]

HARD = ["blurry", "dark", "glare"]  # the photo is unusable whatever it shows
SOFT = ["covered", "cut_off"]        # only matter when the damage cannot be seen


def verdict(r, t):
    a = r["answers"]
    if any(a[k]["noul"] >= t[k] for k in HARD):
        return "retake"          # before the product check: an unusable photo often reads as "not a phone"
    if a["kind"]["choice"] != r["product"]:
        return "mismatch"
    if a["damage_visible"]["noul"] >= t["damage_visible"]:
        return "accept"          # a close-up or a hand in the photo is fine when the damage is clearly visible
    if any(a[k]["noul"] >= t[k] for k in SOFT):
        return "retake"
    return "no_damage"

def score(t):
    return sum(verdict(r, t) == GOLD[r["case"]] for r in rows)

base = {k: 0.5 for k in Q + ["damage_visible"]}
print("all thresholds 0.5:", score(base), "/", len(rows))
best = dict(base)
for _ in range(3):  # coordinate search; small set, so this is a first look, not a final tuning
    for k in best:
        best[k] = max([x / 20 for x in range(2, 19)], key=lambda v: (score({**best, k: v}), -abs(v - 0.5)))
print("tuned:", score(best), "/", len(rows), best)
conf = Counter((GOLD[r["case"]], verdict(r, best)) for r in rows)
labs = ["accept", "retake", "no_damage", "mismatch"]
print("gold \\ got   " + " ".join(f"{l:>9s}" for l in labs))
for g in labs:
    print(f"{g:12s} " + " ".join(f"{conf[(g, l)]:9d}" for l in labs))
for r in rows:
    v = verdict(r, best)
    if v != GOLD[r["case"]]:
        a = r["answers"]
        print(f"  {r['image'].split('/')[-1]:28s} {r['case']:13s} got {v:9s} kind={a['kind']['choice']:10s} " +
              " ".join(f"{k[:4]}={a[k]['noul']:.2f}" for k in Q + ['damage_visible']))
