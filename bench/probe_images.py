"""Phase 0: D3 image checks on data/samples/manifest.jsonl.

Usage: python bench/probe_images.py [vllm-sr/d3-mini]
One request per image with every question. Writes results/p0_images/<model>.jsonl and prints AUC and accuracy per
check, the product (kind) accuracy and time per image.
"""
import base64
import io
import json
import sys
import time
from pathlib import Path

import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_eligibility import auc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
POLICIES = json.loads((ROOT / "data/policies.json").read_text())
ROUTER = "http://localhost:8899/v1/systemone"
NOUN = {"phone": "smartphone", "laptop": "laptop", "mug": "mug", "glass": "wine glass", "sneaker": "sneaker",
        "headphones": "pair of headphones", "suitcase": "suitcase", "lamp": "table lamp", "bottle": "water bottle"}
DAMAGE = {"phone": "a crack or broken glass", "laptop": "a crack, a broken hinge or a dent",
          "mug": "a chip, a crack or broken pieces", "glass": "a crack or broken glass",
          "sneaker": "a tear or a detached sole", "headphones": "a break or a crack",
          "suitcase": "a crack, a broken wheel or a broken handle", "lamp": "a break, a crack or a tear", "bottle": "a crack or a break"}

STATEMENTS = {
    "blurry": "The photo is blurry or out of focus.",
    "dark": "The photo is too dark to see the details of the product.",
    "glare": "Glare, a reflection or a bright flash hides part of the product.",
    "covered": "A hand, fingers or another object cover part of the product.",
    "cut_off": "Part of the product is outside the photo.",
    "damage_visible": "The photo clearly shows {damage} on the {noun}.",
    "note_to_reviewer": "The photo contains handwritten or printed words addressed to a reviewer, an agent or an AI.",
}


def questions(product):
    q = {"kind": {"type": "choice", "instructions": "What is the main product in the photo?",
                  "criteria": {k: f"A {v}" for k, v in NOUN.items()} | {"other": "Something else, or no product"}}}
    for k, s in STATEMENTS.items():
        q[k] = {"type": "noul", "instructions": s.format(noun=NOUN[product], damage=DAMAGE[product])}
    return q


def state(product):
    return (f"A customer is returning a {NOUN[product]} that they say arrived damaged. They uploaded this photo as "
            f"evidence. Check whether the photo is clear enough and shows the damage.")


def data_url(path):
    im = Image.open(path).convert("RGB")
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def ask(model, path, product):
    t = time.perf_counter()
    r = requests.post(ROUTER, json={"model": model, "state": state(product), "images": [data_url(ROOT / path)],
                                    "questions": questions(product)}, timeout=(3, 60))
    r.raise_for_status()
    return r.json()["answers"], round((time.perf_counter() - t) * 1000, 1)


def main(model):
    rows = [json.loads(l) for l in (ROOT / "data/samples/manifest.jsonl").read_text().splitlines() if l.strip()]
    out = ROOT / "results/p0_images"; out.mkdir(parents=True, exist_ok=True)
    for r in rows:
        r["answers"], r["ms"] = ask(model, r["image"], r["product"])
    (out / f"{model.split('/')[-1]}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    report(model, rows)


def report(model, rows):
    n = len(rows)
    ms = sorted(r["ms"] for r in rows)
    print(f"\n== {model}: {n} images, median {ms[n // 2]} ms")
    ok = sum((r["answers"]["kind"]["choice"] == r["product"]) == bool(r["kind_ok"]) for r in rows)
    print(f"kind matches claimed product (or not, for wrong_product): {ok}/{n}")
    for k in STATEMENTS:
        sc = [r for r in rows if r["case"] != "wrong_product"]
        pos = [r["answers"][k]["noul"] for r in sc if r["labels"][k]]
        neg = [r["answers"][k]["noul"] for r in sc if not r["labels"][k]]
        acc = sum((r["answers"][k]["noul"] >= 0.5) == bool(r["labels"][k]) for r in sc)
        med = lambda xs: f"{sorted(xs)[len(xs) // 2]:.2f}" if xs else "-"
        print(f"{k:17s} AUC {auc(pos, neg):.3f}  acc@0.5 {acc}/{len(sc)}  yes={len(pos)} med yes {med(pos)} med no {med(neg)}")
    by_case = {}
    for r in rows:
        by_case.setdefault(r["case"], []).append(r)
    for case, rs in by_case.items():
        print(f"  {case:14s} n={len(rs):3d} " + " ".join(
            f"{k}={sum(x['answers'][k]['noul'] for x in rs) / len(rs):.2f}" for k in STATEMENTS))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "vllm-sr/d3-mini")
