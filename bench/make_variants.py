"""Phase 0: builds the image probe set from data/raw/commons and writes data/samples/manifest.jsonl.

Every damaged photo is copied as-is (damage visible) and as four generated variants: blurry, dark, glare, cut_off.
Intact photos give the "no visible damage" case; catalog photos are kept for the 2-image match test; hand photos are
the "covered" case. Labels are per image: which checks should fire.
"""
import json
import random
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data/raw/commons"
OUT = ROOT / "data/samples"
CHECKS = ["blurry", "dark", "glare", "covered", "cut_off", "damage_visible", "note_to_reviewer"]


def load(p):
    im = Image.open(p).convert("RGB")
    im.thumbnail((1600, 1600))
    return im


def blurry(im):
    return im.filter(ImageFilter.GaussianBlur(radius=max(im.size) / 110))


def dark(im):
    return ImageEnhance.Brightness(im).enhance(0.12)


def glare(im):
    """A bright flash spot over the centre, where the damage usually is."""
    w, h = im.size
    layer = Image.new("L", im.size, 0)
    r = int(min(w, h) * 0.32)
    ImageDraw.Draw(layer).ellipse([w // 2 - r, h // 2 - r, w // 2 + r, h // 2 + r], fill=255)
    layer = layer.filter(ImageFilter.GaussianBlur(r / 2.2))
    return Image.composite(Image.new("RGB", im.size, (255, 255, 250)), im, layer)


def cut_off(im):
    """Keeps one corner (40 % of each side): most of the product is outside the photo."""
    w, h = im.size
    return im.crop((0, 0, int(w * 0.4), int(h * 0.4))).resize((int(w * 0.8), int(h * 0.8)))


VARIANTS = {"blurry": blurry, "dark": dark, "glare": glare, "cut_off": cut_off}


def labels(**fire):
    return {k: int(fire.get(k, 0)) for k in CHECKS}


def main():
    random.seed(7)
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []

    def add(im, name, product, folder, kind_ok, lab, source):
        path = OUT / product / name
        path.parent.mkdir(parents=True, exist_ok=True)
        im.save(path, "JPEG", quality=90)
        rows.append({"image": str(path.relative_to(ROOT)), "product": product, "case": folder, "kind_ok": kind_ok,
                     "labels": lab, "source": source})

    for p in sorted((RAW / "damaged").glob("*.jpg")):
        product = re.match(r"([a-z]+)_", p.name).group(1)
        im = load(p)
        add(im, f"{p.stem}_clear.jpg", product, "damaged_clear", 1, labels(damage_visible=1), p.name)
        for v, fn in VARIANTS.items():
            # a blurry/dark/glare/cut-off photo should not count as clear evidence of the damage
            add(fn(im), f"{p.stem}_{v}.jpg", product, v, 1, labels(**{v: 1}), p.name)
    for p in sorted((RAW / "intact").glob("*.jpg")):
        product = re.match(r"([a-z]+)_", p.name).group(1)
        add(load(p), f"{p.stem}_intact.jpg", product, "no_damage", 1, labels(), p.name)
    for p in sorted((RAW / "hand").glob("*.jpg")):
        product = re.match(r"([a-z]+)_", p.name).group(1)
        add(load(p), f"{p.stem}_hand.jpg", product, "hand", 1, labels(covered=1), p.name)
    # wrong product: a clear damaged photo of another product, claimed as this one
    damaged = [r for r in rows if r["case"] == "damaged_clear"]
    products = sorted({r["product"] for r in damaged})
    for r in damaged[::3]:
        other = random.choice([x for x in products if x != r["product"]])
        rows.append({**r, "product": other, "case": "wrong_product", "kind_ok": 0, "claimed_as": other,
                     "true_product": r["product"]})
    (OUT / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"{len(rows)} images -> {OUT / 'manifest.jsonl'}")


if __name__ == "__main__":
    main()
