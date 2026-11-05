"""D3 photo check on data/samples/manifest.jsonl (123 images), with the app's own questions (app/photo.py).

Usage: python bench/probe_images.py [vllm-sr/d3-flash]
Two hosted System One requests per image, as the app sends them: the main request (product, quality, damage, note
questions) and the separate "real photo" request. Each request carries one image (the hosted limit is 4). Images go
through the app's image_url (upright JPEG, at most 1600 px). Writes results/d3_flash/images.jsonl (raw answers + ms);
bench/score_images.py turns them into verdicts. Key: bench/hosted.py.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "bench"))
import hosted  # noqa: E402
import photo  # noqa: E402
from models import image_url  # noqa: E402


def ask(model, row):
    img = image_url(row["image"].removeprefix("data/"))
    main, ms = hosted.systemone({"model": model, "state": photo.state(row["product"]), "images": [img],
                                 "questions": photo.questions(row["product"])})
    real, rms = hosted.systemone({"model": model, "state": photo.REAL_PHOTO_STATE, "images": [img],
                                  "questions": {"real_photo": {"type": "noul", "instructions": photo.REAL_PHOTO[0]}}})
    return {**row, "answers": {**main, **real}, "ms": ms, "real_ms": rms}


def main(model):
    rows = [json.loads(l) for l in (ROOT / "data/samples/manifest.jsonl").read_text().splitlines() if l.strip()]
    out = hosted.pmap(lambda r: ask(model, r), rows)
    d = ROOT / "results/d3_flash"
    d.mkdir(parents=True, exist_ok=True)
    (d / "images.jsonl").write_text("".join(json.dumps(r) + "\n" for r in out))
    ms = sorted(max(r["ms"], r["real_ms"]) for r in out)
    print(f"{model}: {len(out)} images, {hosted.CALLS['n']} calls, median {ms[len(ms) // 2]} ms per image "
          f"(both requests at once in the app), p90 {ms[int(len(ms) * 0.9)]} ms")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "vllm-sr/d3-flash")
