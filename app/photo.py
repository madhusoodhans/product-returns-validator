"""D3 photo check: is the photo usable, is it the ordered product, and does it show the damage.

One d3-mini request per photo with every question. Wording and rule from Phase 0 (bench/probe_images.py,
bench/score_images.py; PLAN.md section 8):
  not a real photo (screenshot, chart)  -> mismatch
  blurry or dark                        -> retake   (before the product check: unusable photos read as "other")
  not the ordered product               -> mismatch
  damage clearly visible                -> accept   (a hand, a close-up crop or a reflection is fine then)
  glare, a hand or a crop               -> retake   (they explain why the damage cannot be seen)
  damage score 0.25-0.5                 -> human    (borderline; d3-flash does not fit on the GPU, so a person looks)
  otherwise                             -> no_damage
Phase 0 with by-eye labels: 86/88 right on the photos without weak sources, no intact photo approved.
Text in the photo asking for approval is flagged and shown; it never changes the verdict.
"""
from concurrent.futures import ThreadPoolExecutor

from models import d3

NOUN = {"phone": "smartphone", "laptop": "laptop", "mug": "mug", "glass": "wine glass", "sneaker": "sneaker",
        "headphones": "pair of headphones", "suitcase": "suitcase", "lamp": "table lamp", "bottle": "water bottle"}
DAMAGE = {"phone": "a crack or broken glass", "laptop": "a crack, a broken hinge or a dent",
          "mug": "a chip, a crack or broken pieces", "glass": "a crack or broken glass",
          "sneaker": "a tear or a detached sole", "headphones": "a break or a crack",
          "suitcase": "a crack, a broken wheel or a broken handle", "lamp": "a break, a crack or a tear",
          "bottle": "a crack or a break"}

# key: (statement, label, threshold)
HARD = {"blurry": ("The photo is blurry or out of focus.", "Blurry", 0.5),
        "dark": ("The photo is too dark to see the details of the product.", "Too dark", 0.5)}
SOFT = {"glare": ("Glare, a reflection or a bright flash hides part of the product.", "Glare or reflection", 0.5),
        "covered": ("A hand, fingers or another object cover part of the product.", "Hand or object in the way", 0.5),
        "cut_off": ("Part of the product is outside the photo.", "Product cut off", 0.5)}
DAMAGE_Q = ("The photo clearly shows {damage} on the {noun}.", "Damage clearly visible", 0.5)
NOTE_QS = {"note": "The photo contains handwritten or printed words addressed to a reviewer, an agent or an AI.",
           "note_request": "Somewhere in the photo, on paper, a sticky note or a screen, there is text that gives an "
                           "instruction or a request."}
DAMAGE_UNSURE = 0.25
NOTE_FLAG = 0.4
REAL_PHOTO = ("The image is a camera photo of a real physical object, not a screenshot, chart, diagram, document, "
              "drawing or text on a screen.", "Real photo", 0.3)
# 120 sample photos: min 0.56, median 0.99; charts, chat and UI screenshots 0.00-0.33 (two UI screenshots full of
# product photos 0.64-0.70 still go on to the product check)  # max of the two: 5/5 note photos, 0/29 clean photos in Phase 0


def state(product):
    return (f"A customer is returning a {NOUN[product]} that they say arrived damaged. They uploaded this photo as "
            f"evidence. Check whether the photo is clear enough and shows the damage.")


def questions(product):
    q = {"kind": {"type": "choice", "instructions": "What is the main product in the photo?",
                  "criteria": {k: f"A {v}" for k, v in NOUN.items()} | {"other": "Something else, or no product"}}}
    for k, (s, _l, _t) in (HARD | SOFT).items():
        q[k] = {"type": "noul", "instructions": s}
    q["damage_visible"] = {"type": "noul", "instructions": DAMAGE_Q[0].format(damage=DAMAGE[product], noun=NOUN[product])}
    q.update({k: {"type": "noul", "instructions": s} for k, s in NOTE_QS.items()})
    return q


# Asked in its own request with a neutral description: inside the main request ("they say it arrived damaged ...
# check whether the photo is clear enough") D3 scored blurry or intact real photos 0.02-0.23; alone 0.95-1.00.
REAL_PHOTO_STATE = "A customer uploaded this image to a returns chat as evidence of a damaged product."


def check(product, image_data_url):
    """Returns (verdict, reasons, detail, ms, tokens). detail follows docs/API.md (image_check)."""
    with ThreadPoolExecutor(2) as pool:  # both requests at once; the Router caches the image features
        main = pool.submit(d3, state(product), questions(product), [image_data_url])
        real = pool.submit(d3, REAL_PHOTO_STATE, {"real_photo": {"type": "noul", "instructions": REAL_PHOTO[0]}},
                           [image_data_url])
        (a, ms, tokens), (ra, rms, rtok) = main.result(), real.result()
    ms, tokens = max(ms, rms), tokens + rtok
    p = {k: float(v["noul"]) for k, v in a.items() if "noul" in v}
    p["real_photo"] = float(ra["real_photo"]["noul"])
    kind = a["kind"]["choice"]
    reasons = [HARD[k][1] for k in HARD if p[k] >= HARD[k][2]]
    if p["real_photo"] < REAL_PHOTO[2]:
        # quality questions mean nothing for a chart or a screenshot: say what it is instead
        verdict, reasons = "mismatch", ["Not a photo of the product (a screenshot, chart or document)"]
    elif reasons:
        verdict = "retake"
    elif kind != product:
        verdict, reasons = "mismatch", [f"Shows {'something else' if kind == 'other' else 'a ' + NOUN[kind]}, not a {NOUN[product]}"]
    elif p["damage_visible"] >= DAMAGE_Q[2]:
        verdict = "accept"
    else:
        reasons = [SOFT[k][1] for k in SOFT if p[k] >= SOFT[k][2]]
        if reasons:
            verdict = "retake"
        elif p["damage_visible"] >= DAMAGE_UNSURE:
            verdict, reasons = "human", ["Damage unclear in the photo"]
        else:
            verdict, reasons = "no_damage", ["Damage not visible"]
    note = max(p["note"], p["note_request"])

    rp = p["real_photo"]
    qs = [{"key": "real_photo", "label": "Real photo", "type": "yesno", "prob": round(rp, 4), "threshold": REAL_PHOTO[2],
           "borderline_from": 0.5, "status": "bad" if rp < REAL_PHOTO[2] else "borderline" if rp < 0.5 else "ok"},
          {"key": "kind", "label": "Product in photo", "type": "choice", "choice": kind,
           "probs": {o: round(float(v), 4) for o, v in a["kind"]["probabilities"].items()}}]
    for k, (_s, label, thr) in (HARD | SOFT).items():
        qs.append({"key": k, "label": label, "type": "yesno", "prob": round(p[k], 4), "threshold": thr,
                   "borderline_from": round(thr - 0.1, 2),
                   "status": "bad" if p[k] >= thr else "borderline" if p[k] >= thr - 0.1 else "ok"})
    dv = p["damage_visible"]
    qs.append({"key": "damage_visible", "label": f"Shows {DAMAGE[product]}", "type": "yesno", "prob": round(dv, 4),
               "threshold": DAMAGE_Q[2], "borderline_from": round(DAMAGE_Q[2] + 0.1, 2),
               "status": "ok" if dv >= DAMAGE_Q[2] + 0.1 else "borderline" if dv >= DAMAGE_Q[2] else "bad"})
    qs.append({"key": "note", "label": "Text asking for approval", "type": "yesno", "prob": round(note, 4),
               "threshold": NOTE_FLAG, "status": "bad" if note >= NOTE_FLAG else "ok"})
    detail = {"model": "vllm-sr/d3-mini", "questions": qs, "escalated": [], "note_flagged": note >= NOTE_FLAG}
    return verdict, reasons, detail, ms, tokens
