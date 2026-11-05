"""Verdict accuracy of the app's photo rule (app/photo.py) on saved answers; no new D3 calls.

Usage: python bench/score_images.py [results/d3_flash/images.jsonl] [--search]
Gold verdict per image: the manifest case, with the by-eye labels (data/samples/labels_by_eye.jsonl) for glare and
cut-off variants: damage still clearly visible -> accept, else retake. Intact -> no_damage, wrong product -> mismatch,
note photos -> accept (the note never changes the verdict). "human" (damage score in the unsure band) is a safe miss.
--search: a coordinate search over the thresholds on this (tuning) data, to see whether any change clearly helps.
Saved Phase 0 d3-mini answers (results/p0_images/d3-mini.jsonl) lack "real_photo" and "note_request": they count as
a real photo and as the old note question.
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import photo  # noqa: E402

EYE = {json.loads(l)["image"]: json.loads(l) for l in (ROOT / "data/samples/labels_by_eye.jsonl").read_text().splitlines() if l.strip()}
BASE = {"real_photo": photo.REAL_PHOTO[2], **{k: v[2] for k, v in (photo.HARD | photo.SOFT).items()},
        "damage_visible": photo.DAMAGE_Q[2], "damage_unsure": photo.DAMAGE_UNSURE, "note": photo.NOTE_FLAG}


def gold(r):
    case = r["case"]
    if case in ("glare", "cut_off") and r["image"] in EYE:
        return "accept" if EYE[r["image"]]["damage_visible"] else "retake"
    return {"damaged_clear": "accept", "blurry": "retake", "dark": "retake", "glare": "retake", "cut_off": "retake",
            "hand": "retake", "no_damage": "no_damage", "wrong_product": "mismatch", "note": "accept"}[case]


def probs(r):
    a = r["answers"]
    p = {k: float(v["noul"]) for k, v in a.items() if isinstance(v, dict) and "noul" in v}
    p.setdefault("real_photo", 1.0)
    p.setdefault("note", p.get("note_to_reviewer", 0.0))
    p.setdefault("note_request", p["note"])
    return p, a["kind"]["choice"]


def verdict(r, t):
    """app/photo.py check(), with thresholds t."""
    p, kind = probs(r)
    if p["real_photo"] < t["real_photo"]:
        return "mismatch"
    if any(p[k] >= t[k] for k in photo.HARD):
        return "retake"
    if kind != r["product"]:
        return "mismatch"
    if p["damage_visible"] >= t["damage_visible"]:
        return "accept"
    if any(p[k] >= t[k] for k in photo.SOFT):
        return "retake"
    return "human" if p["damage_visible"] >= t["damage_unsure"] else "no_damage"


def score(rows, t):
    ok = sum(verdict(r, t) == gold(r) for r in rows)
    unsafe = sum(verdict(r, t) == "accept" and gold(r) != "accept" for r in rows)
    return ok, unsafe


def report(rows, t, name):
    ok, unsafe = score(rows, t)
    human = sum(verdict(r, t) == "human" for r in rows)
    notes = [r for r in rows if r["case"] == "note"]
    clean = [r for r in rows if r["case"] != "note"]
    nf = lambda r: max(probs(r)[0]["note"], probs(r)[0]["note_request"])
    print(f"\n== {name}: {ok}/{len(rows)} right, {unsafe} wrong approvals, {human} to a person; "
          f"note flag {sum(nf(r) >= t['note'] for r in notes)}/{len(notes)} notes, "
          f"{sum(nf(r) >= t['note'] for r in clean)}/{len(clean)} clean photos flagged")
    conf = Counter((gold(r), verdict(r, t)) for r in rows)
    labs = ["accept", "retake", "no_damage", "mismatch", "human"]
    print("gold \\ got   " + " ".join(f"{l:>9s}" for l in labs))
    for g in labs[:4]:
        print(f"{g:12s} " + " ".join(f"{conf[(g, l)]:9d}" for l in labs))
    for r in rows:
        v = verdict(r, t)
        if v != gold(r):
            p, kind = probs(r)
            print(f"  {r['image'].split('/')[-1]:26s} {r['case']:13s} gold {gold(r):9s} got {v:9s} kind={kind:10s} "
                  + " ".join(f"{k[:4]}={p[k]:.2f}" for k in ["real_photo", *photo.HARD, *photo.SOFT, "damage_visible"]))
    return ok, unsafe


def search(rows):
    best = dict(BASE)
    grid = [x / 20 for x in range(1, 20)]
    for _ in range(3):
        for k in [k for k in best if k != "note"]:
            best[k] = max(grid, key=lambda v: (score(rows, {**best, k: v})[0] - 5 * score(rows, {**best, k: v})[1],
                                               -abs(v - BASE[k])))
    return best


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = ROOT / (args[0] if args else "results/d3_flash/images.jsonl")
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    report(rows, BASE, f"{path.name}, app thresholds {BASE}")
    if "--search" in sys.argv:
        best = search(rows)
        report(rows, best, f"{path.name}, searched thresholds {best}")
