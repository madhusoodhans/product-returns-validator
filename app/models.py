"""Model calls on the hosted vllm-sr APIs: System One (D3 decisions, the Vela guardrail) and chat completions
(`vllm-sr/auto`: the Router picks the model and the reasoning effort per request).

Every call goes through availability.call (circuit breaker, plain-word failures) with its per-model timeout and through
a limits.py slot. The API key is the visitor's own (keys.current(): set per turn by the server; the optional env key
VLLM_SR_API_KEY otherwise); it is sent as a bearer token and never logged. No key: keys.NoKey before any request.
Model names and the API base are set by env (defaults: the hosted ones).
"""
import base64
import os
import io
import json
import time
from pathlib import Path

from PIL import Image, ImageOps

import keys
from availability import Unavailable, call, timeout as model_timeout
from limits import Busy, Gate, post

API_BASE = os.environ.get("VLLM_SR_API_BASE", "https://app.vllm-sr.ai").rstrip("/")
D3_MODEL = os.environ.get("D3_MODEL", "vllm-sr/d3-flash")          # policy, eligibility, photo and scope checks
GUARD_MODEL = os.environ.get("GUARD_MODEL", "vllm-sr/vela3-flash")  # prompt attack and harm screen
LLM_MODEL = os.environ.get("LLM_MODEL", "vllm-sr/auto")             # claim extraction and reply wording
MAX_IMAGES = 4               # per System One request (hosted d3-flash limit)
MAX_IMAGE_BYTES = 8_000_000  # per image, decoded
ROOT = Path(__file__).resolve().parent.parent


MissingKey = keys.NoKey


def api_key():
    """The key for a call now (the visitor's, else the optional env key); raises keys.NoKey."""
    return keys.current()


def headers(debug=False):
    h = {"authorization": f"Bearer {api_key()}"}
    if debug:   # the Router's choice in the response headers (x-vsr-selected-*)
        h["x-vsr-debug"] = "true"
    return h


def route_info(r):
    """What vllm-sr/auto picked for this request, from the response headers."""
    h = r.headers
    ms = h.get("x-vsr-routing-latency-ms")
    try:
        ms = round(float(ms), 1)
    except (TypeError, ValueError):
        ms = None
    return {"model": h.get("x-vsr-selected-model"), "decision": h.get("x-vsr-selected-decision"),
            "reasoning": h.get("x-vsr-selected-reasoning"), "effort": h.get("x-vsr-matched-projections"),
            "signals": h.get("x-vsr-matched-decision-model"), "ranking": h.get("x-vsr-decision-ranking"),
            "routing_ms": ms}


def d3(state, questions, images=(), model=D3_MODEL, timeout=None, kind="d3"):
    """One System One request. Returns (answers, ms, tokens). Raises availability.Unavailable when the model is down,
    fails or times out. kind: "d3" or "guard" (its own breaker and slots)."""
    images = list(images)
    if len(images) > MAX_IMAGES:   # the hosted API refuses more; never split silently
        raise ValueError(f"{len(images)} images in one System One request (max {MAX_IMAGES})")
    body = {"model": model, "state": state, "questions": questions}
    if images:
        body["images"] = images
    t = time.perf_counter()
    with call(kind):
        with post(kind, f"{API_BASE}/v1/systemone", json=body, headers=headers(),
                  timeout=timeout or model_timeout(kind)) as r:
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
            if buf.tell() > MAX_IMAGE_BYTES:   # cannot happen at 1600 px, but the API would refuse it
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=75)
    except (BadImage, Busy):
        raise
    except Exception as e:  # PIL: not an image, truncated, too many pixels; base64 errors
        raise BadImage(f"{type(e).__name__}") from e
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _body(messages, max_tokens, temperature, schema=None, stream=False):
    body = {"model": LLM_MODEL, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}
    if stream:
        body.update(stream=True, stream_options={"include_usage": True})
    return body


# The Router may switch reasoning on (it cannot be switched off from here): reasoning tokens count against
# max_tokens, so the budget leaves room for them.
MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "2048"))


def chat(messages, max_tokens=MAX_TOKENS, schema=None, timeout=None):
    """Non-streaming call to vllm-sr/auto. Returns (text, ms, tokens, route). Raises availability.Unavailable."""
    t = time.perf_counter()
    with call("llm"):
        with post("chat", f"{API_BASE}/v1/chat/completions", json=_body(messages, max_tokens, 0, schema),
                  headers=headers(debug=True), timeout=timeout or model_timeout("llm")) as r:
            r.raise_for_status()
            route = route_info(r)
            out = r.json()
        text = (out["choices"][0]["message"].get("content") or "").strip()
    # tokens = what the model wrote (completion tokens, reasoning included), the top bar's "LLM tokens"
    return text, round((time.perf_counter() - t) * 1000, 1), (out.get("usage") or {}).get("completion_tokens") or 0, route


def chat_stream(messages, max_tokens=MAX_TOKENS, timeout=None):
    """Streaming call to vllm-sr/auto. Yields ("route", info) once the headers arrive, ("token", text) for the answer
    (reasoning is not shown) and finally ("done", {"ms", "tokens", "text", "route"}). The whole stream must finish
    within the timeout. Raises availability.Unavailable."""
    t = time.perf_counter()
    limit = timeout or model_timeout("llm")
    text, tokens = [], 0
    with call("llm"):
        with post("chat", f"{API_BASE}/v1/chat/completions", json=_body(messages, max_tokens, 0.3, stream=True),
                  headers=headers(debug=True), timeout=limit, stream=True) as r:
            r.raise_for_status()
            route = route_info(r)
            yield "route", route
            for line in r.iter_lines(decode_unicode=True):
                if time.perf_counter() - t > limit[1]:
                    raise Unavailable("llm", "timed out")
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("error"):
                    raise ValueError(f"stream error {(chunk['error'] or {}).get('code')}")
                if chunk.get("usage"):
                    tokens = chunk["usage"].get("completion_tokens") or tokens
                for c in chunk.get("choices") or []:
                    piece = (c.get("delta") or {}).get("content")
                    if piece:
                        text.append(piece)
                        yield "token", piece
        if not "".join(text).strip():
            raise ValueError("empty reply")   # e.g. reasoning used the whole token budget
    yield "done", {"ms": round((time.perf_counter() - t) * 1000, 1), "tokens": tokens, "text": "".join(text).strip(),
                   "route": route}
