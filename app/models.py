"""Model calls: D3 (System One API) and Qwen (chat completions), both through the vllm-sr Router on :8899.

Every call goes through availability.call (circuit breaker, plain-word failures) with its per-model timeout. Set
ROUTER_API_KEY / ROUTER_ADMIN_API_KEY to send a bearer token (hosted APIs); the local Router needs none.
"""
import base64
import os
import io
import json
import time
from pathlib import Path

from PIL import Image, ImageOps

from availability import Unavailable, call, timeout as model_timeout
from limits import Busy, Gate, post

ROUTER_URL = os.environ.get("ROUTER_URL", "http://localhost:8899")
D3_MODEL = os.environ.get("D3_MODEL", "vllm-sr/d3-mini")
ROOT = Path(__file__).resolve().parent.parent


def headers(admin=False):
    key = os.environ.get("ROUTER_ADMIN_API_KEY" if admin else "ROUTER_API_KEY") or (
        os.environ.get("ROUTER_API_KEY") if admin else None)
    return {"authorization": f"Bearer {key}"} if key else {}


def d3(state, questions, images=(), model=D3_MODEL, timeout=None):
    """Returns (answers, ms, tokens). Raises availability.Unavailable when D3 is down, fails or times out."""
    body = {"model": model, "state": state, "questions": questions}
    if images:
        body["images"] = list(images)
    t = time.perf_counter()
    with call("d3"):
        with post("d3", f"{ROUTER_URL}/v1/systemone", json=body, headers=headers(),
                  timeout=timeout or model_timeout("d3")) as r:
            r.raise_for_status()
            out = r.json()
        answers = out["answers"]
        for k in questions:   # an answer without our questions is not an answer
            answers[k]
    u = out.get("usage") or {}
    return answers, round((time.perf_counter() - t) * 1000, 1), (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)


try:  # iPhone photos (HEIC) wherever images are read, not only in the server
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

Image.MAX_IMAGE_PIXELS = 60_000_000  # a 48 MP phone photo fits; decompression bombs raise instead of filling memory
DECODE = Gate("image decode", slots=4, queue=100)  # a full-size 48 MP decode is ~150 MB
SIDE = 1600


class BadImage(ValueError):
    """The upload is not an image we can read; the flow answers with a "send a photo" reply and changes no state."""


def image_url(image):
    """A sample path (relative to data/) or a data URL -> an upright JPEG data URL, at most 1600 px on the long side.
    Raises BadImage for anything that is not a readable image."""
    try:
        if image.startswith("data:"):
            head, sep, b64 = image.partition(",")
            if not sep or ";base64" not in head:
                raise BadImage("not a base64 data URL")
            raw = base64.b64decode(b64, validate=False)
        else:
            p = (ROOT / "data" / image).resolve()
            if not p.is_relative_to(ROOT / "data") or not p.is_file():
                raise BadImage("unknown image path")
            raw = p.read_bytes()
        with DECODE:
            im = Image.open(io.BytesIO(raw))
            if im.format == "JPEG":  # decode at 1/2, 1/4 or 1/8 scale, still at least the target size
                s = SIDE / max(im.size)
                if s < 1:
                    im.draft("RGB", (round(im.size[0] * s), round(im.size[1] * s)))
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((SIDE, SIDE), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90)
    except (BadImage, Busy):
        raise
    except Exception as e:  # PIL: not an image, truncated, too many pixels; base64 errors
        raise BadImage(f"{type(e).__name__}") from e
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def chat(model, messages, max_tokens=300, schema=None, timeout=None):
    """Non-streaming call. Returns (text, ms, tokens). Raises availability.Unavailable."""
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False}}
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}
    t = time.perf_counter()
    with call(model):
        with post("chat", f"{ROUTER_URL}/v1/chat/completions", json=body, headers=headers(),
                  timeout=timeout or model_timeout(model)) as r:
            r.raise_for_status()
            out = r.json()
        text = out["choices"][0]["message"]["content"].strip()
    # tokens = what the model wrote (completion tokens), the figure the top bar sums as "LLM tokens generated"
    return text, round((time.perf_counter() - t) * 1000, 1), (out.get("usage") or {}).get("completion_tokens") or 0


def chat_stream(model, messages, max_tokens=300, timeout=None):
    """Streaming call. Yields ("token", text) and finally ("done", {"ms", "tokens", "text"}), tokens being the completion
    tokens. The whole stream must
    finish within the model's timeout. Raises availability.Unavailable."""
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.3, "stream": True,
            "stream_options": {"include_usage": True}, "chat_template_kwargs": {"enable_thinking": False}}
    t = time.perf_counter()
    limit = timeout or model_timeout(model)
    text, tokens = [], 0
    with call(model):
        with post("chat", f"{ROUTER_URL}/v1/chat/completions", json=body, headers=headers(), timeout=limit,
                  stream=True) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if time.perf_counter() - t > limit[1]:
                    raise Unavailable(model, "timed out")
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("usage"):
                    tokens = chunk["usage"].get("completion_tokens") or tokens
                for c in chunk.get("choices") or []:
                    piece = (c.get("delta") or {}).get("content")
                    if piece:
                        text.append(piece)
                        yield "token", piece
    yield "done", {"ms": round((time.perf_counter() - t) * 1000, 1), "tokens": tokens, "text": "".join(text).strip()}
