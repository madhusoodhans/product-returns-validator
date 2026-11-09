"""D3-compatible shim backed by a Qwen LLM (the 'System 1 extract' ablation).

Drop-in for app.models.d3(state, questions, images=(), ...). It turns the same
question dict D3 answers into a JSON-schema Qwen request, so the EXACT same code
decision rules in app/ consume the output. One LLM call per d3() call (call-count
parity with D3's one-forward-pass). Returns (answers, ms, tokens) in D3's shape:
  choice question -> {"choice": k, "probabilities": {k: p, ...}}
  noul question   -> {"noul": p}

Extractor model is configurable: PRV_EXTRACTOR env (default qwen27b = the 'big LLM').
Vision questions (images present) always go to qwen27b (9B is text-only).
"""
import json
import os
import time

import requests

ROUTER_URL = os.environ.get("ROUTER_URL", "http://localhost:8899")
EXTRACTOR = os.environ.get("PRV_EXTRACTOR", "qwen27b")


def _schema(questions):
    props, req = {}, []
    for key, q in questions.items():
        if q["type"] == "choice":
            opts = list(q["criteria"].keys())
            props[key] = {"type": "object", "properties": {
                "choice": {"type": "string", "enum": opts},
                "probabilities": {"type": "object",
                                  "properties": {o: {"type": "number"} for o in opts},
                                  "required": opts}},
                "required": ["choice", "probabilities"]}
        else:  # noul: a single probability 0..1
            props[key] = {"type": "object",
                          "properties": {"noul": {"type": "number"}},
                          "required": ["noul"]}
        req.append(key)
    return {"type": "object", "properties": props, "required": req}


def _instructions(questions):
    lines = ["You are a precise information-extraction engine. For EACH field below, read the context and answer.",
             "A 'choice' field: pick exactly one option key and give a probability (0..1) for every option; they should sum to about 1.",
             "A 'yes/no' field ('noul'): give one probability (0..1) that the statement is TRUE.",
             "Answer ONLY with the JSON schema. Do not judge, approve or decide anything; just report what the text/photo shows.",
             "", "FIELDS:"]
    for key, q in questions.items():
        if q["type"] == "choice":
            lines.append(f"- {key} (choice): {q['instructions']}")
            for o, d in q["criteria"].items():
                lines.append(f"    * {o}: {d}")
        else:
            lines.append(f"- {key} (yes/no probability): {q['instructions']}")
    return "\n".join(lines)


def _empty(questions):
    out = {}
    for key, q in questions.items():
        if q["type"] == "choice":
            opts = list(q["criteria"].keys())
            out[key] = {"choice": opts[0], "probabilities": {o: (1.0 if o == opts[0] else 0.0) for o in opts}}
        else:
            out[key] = {"noul": 0.0}
    return out


def d3(state, questions, images=(), model=None, timeout=(3, 180)):
    """D3-compatible. Returns (answers, ms, tokens)."""
    use_vision = bool(images)
    mdl = "qwen27b" if use_vision else EXTRACTOR
    content = [{"type": "text", "text": state}]
    for im in images:
        content.append({"type": "image_url", "image_url": {"url": im}})
    body = {"model": mdl, "temperature": 0, "max_tokens": 700,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "extract", "schema": _schema(questions)}},
            "messages": [{"role": "system", "content": _instructions(questions)},
                         {"role": "user", "content": content}]}
    t = time.perf_counter()
    r = requests.post(f"{ROUTER_URL}/v1/chat/completions", json=body, timeout=timeout)
    r.raise_for_status()
    out = r.json()
    ms = round((time.perf_counter() - t) * 1000, 1)
    tokens = (out.get("usage") or {}).get("total_tokens") or 0
    try:
        raw = json.loads(out["choices"][0]["message"]["content"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return _empty(questions), ms, tokens
    answers = {}
    for key, q in questions.items():
        v = raw.get(key)
        if q["type"] == "choice":
            opts = list(q["criteria"].keys())
            if not isinstance(v, dict):
                answers[key] = _empty({key: q})[key]
                continue
            probs = {o: float(v.get("probabilities", {}).get(o, 0.0) or 0.0) for o in opts}
            choice = v.get("choice")
            if choice not in opts:
                choice = max(probs, key=probs.get)
            answers[key] = {"choice": choice, "probabilities": probs}
        else:
            p = 0.0
            if isinstance(v, dict):
                try:
                    p = float(v.get("noul", 0.0) or 0.0)
                except (TypeError, ValueError):
                    p = 0.0
            answers[key] = {"noul": max(0.0, min(1.0, p))}
    return answers, ms, tokens
