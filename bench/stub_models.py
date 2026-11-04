"""Fake Router for load tests: /v1/systemone (D3), /v1/chat/completions (plain, json_schema, streaming), the routing
preview (/api/v1/routing/preview) and /v1/models, with realistic delays. No model runs.

Run:  python bench/stub_models.py [--port 8932]
Point the demo at it:  ROUTER_URL=http://127.0.0.1:8932 ROUTER_ADMIN_URL=http://127.0.0.1:8932 python app/server.py
Switches (POST /stub/config, JSON, any subset; GET /stub/config shows them and the call counts):
  p429      share of model calls answered 429 (with Retry-After: retry_after)
  p503      share answered 503
  slow_ms   extra delay on every model call
  scale     multiplier on the normal delays
Per model ("d3", "preview", "qwen9b", "qwen27b"; probes count too):
  down      list of models whose calls get the connection dropped (no answer), like a stopped container
  slow      {model: extra ms}
  fail      {model: HTTP status}, e.g. {"qwen27b": 500}
  fail_if   {text: status}: D3 calls whose state contains text get that status (a failure later in the turn)
  answers   {D3 question key: choice or yes/no value}, e.g. {"when": "after", "real_photo": 0.9}
  pick      model the routing preview selects (default: qwen27b for long messages, else qwen9b)
  calls_by  (read only) calls per model
POST /stub/config {"reset": true} puts every switch back to its default.
"""
import argparse
import json
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULTS = {"p429": 0.0, "p503": 0.0, "slow_ms": 0, "scale": 1.0, "retry_after": 1, "down": [], "slow": {}, "fail": {},
            "fail_if": {}, "answers": {}, "pick": None}
CONFIG = json.loads(json.dumps(DEFAULTS))
COUNTS = {"calls": 0, "429": 0, "503": 0, "busy_now": {}, "busy_max": {}, "calls_by": {}}
_lock = threading.Lock()
WORDS = ("Thanks for the details. I have checked your order and the next step is below. Please send a clear photo of "
         "the damage in good light so we can finish your return.").split()


def d3_answers(questions, answers=None):
    """First option of every choice question at 0.9 (or the `answers` override); every yes/no at 0.02-0.2 (seeded by
    its key, so stable) or the override."""
    out, answers = {}, answers or {}
    for key, q in questions.items():
        if q.get("type") == "choice":
            opts = list(q.get("criteria") or {})
            pick = answers.get(key) if answers.get(key) in opts else opts[0]
            rest = 0.1 / max(1, len(opts) - 1)
            out[key] = {"choice": pick, "probabilities": {o: 0.9 if o == pick else rest for o in opts}}
        else:
            v = answers.get(key)
            out[key] = {"noul": float(v) if isinstance(v, (int, float)) else round(random.Random(key).uniform(0.02, 0.2), 4)}
    return out


def fill(schema, text):
    """A value of the json_schema's shape; `order` gets the first 4-6 digit number of the message."""
    t = schema.get("type")
    t = [x for x in t if x != "null"][0] if isinstance(t, list) else t
    if t == "object":
        return {k: fill(v, text) if k != "order" else (re.findall(r"\b\d{4,6}\b", text) or [None])[0]
                for k, v in (schema.get("properties") or {}).items()}
    if "enum" in schema:
        return schema["enum"][0]
    return {"string": text[:200], "number": 0, "integer": 0, "boolean": False, "array": []}.get(t)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, code, body, headers=()):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/v1/models":
            return self._json(200, {"data": [{"id": m} for m in ("qwen9b", "qwen27b", "vllm-sr/d3-mini")]})
        if self.path == "/stub/config":
            with _lock:
                return self._json(200, {**CONFIG, **COUNTS})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        if self.path == "/stub/config":
            with _lock:
                if body.get("reset"):
                    CONFIG.clear()
                    CONFIG.update(json.loads(json.dumps(DEFAULTS)))
                CONFIG.update({k: v for k, v in body.items() if k in CONFIG})
                if body.get("reset_counts") or body.get("reset"):
                    COUNTS.update({"calls": 0, "429": 0, "503": 0, "busy_max": {}, "calls_by": {}})
            return self._json(200, CONFIG)
        kind = {"/v1/systemone": "d3", "/v1/chat/completions": "chat", "/api/v1/routing/preview": "preview"}.get(self.path)
        if not kind:
            return self._json(404, {"error": "not found"})
        model = body.get("model") if kind == "chat" else kind
        with _lock:
            COUNTS["calls"] += 1
            COUNTS["calls_by"][model] = COUNTS["calls_by"].get(model, 0) + 1
            r = random.random()
            code = 429 if r < CONFIG["p429"] else 503 if r < CONFIG["p429"] + CONFIG["p503"] else 200
            if code != 200:
                COUNTS[str(code)] += 1
            fail = CONFIG["fail"].get(model)
            if kind == "d3":
                fail = fail or next((c for t, c in CONFIG["fail_if"].items() if t in str(body.get("state"))), None)
            down = model in CONFIG["down"]
            COUNTS["busy_now"][kind] = COUNTS["busy_now"].get(kind, 0) + 1
            COUNTS["busy_max"][kind] = max(COUNTS["busy_max"].get(kind, 0), COUNTS["busy_now"][kind])
            scale, slow = CONFIG["scale"], (CONFIG["slow_ms"] + CONFIG["slow"].get(model, 0)) / 1000
        try:
            if down:   # a stopped backend: the connection closes without an answer
                self.close_connection = True
                return
            if code != 200:
                time.sleep(0.01)
                return self._json(code, {"error": "rate limited"}, [("retry-after", str(CONFIG["retry_after"]))])
            time.sleep(slow)
            if fail:
                return self._json(int(fail), {"error": {"message": "upstream connect error or disconnect/reset before "
                                                                   "headers", "type": "stub_failure"}})
            getattr(self, kind)(body, scale)
        finally:
            with _lock:
                COUNTS["busy_now"][kind] -= 1

    def d3(self, body, scale):
        time.sleep(scale * (0.3 if body.get("images") else 0.05) * random.uniform(0.8, 1.3))
        self._json(200, {"answers": d3_answers(body.get("questions") or {}, CONFIG["answers"]),
                         "usage": {"input_tokens": 900 if body.get("images") else 200, "output_tokens": 5}})

    def preview(self, body, scale):
        time.sleep(scale * 0.1 * random.uniform(0.8, 1.3))
        text = " ".join(m.get("content", "") for m in body.get("messages") or [])
        hard = len(text) > 160
        pick = CONFIG["pick"] or ("qwen27b" if hard else "qwen9b")
        self._json(200, {"decision_result": {"decision_name": "returns-hard" if pick == "qwen27b" else "returns-normal"},
                         "metrics": {"jailbreak": {"confidence": 0.02}, "pii": {"confidence": 0.0}},
                         "selected_model": pick, "selection_reason": "stub"})

    def chat(self, body, scale):
        last = next((m["content"] for m in reversed(body.get("messages") or []) if m.get("role") == "user"), "")
        rf = body.get("response_format") or {}
        if rf.get("type") == "json_schema":
            time.sleep(scale * 0.25 * random.uniform(0.8, 1.3))
            text = json.dumps(fill(rf["json_schema"]["schema"], last if isinstance(last, str) else ""))
        else:
            text = " ".join(WORDS[:random.randint(12, len(WORDS))])
        usage = {"prompt_tokens": 600, "completion_tokens": len(text.split()), "total_tokens": 600 + len(text.split())}
        if not body.get("stream"):
            time.sleep(scale * 0.4 * random.uniform(0.8, 1.3))
            return self._json(200, {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": usage})
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("connection", "close")
        self.end_headers()
        self.close_connection = True
        time.sleep(scale * 0.08)   # time to first token
        for w in text.split():
            time.sleep(scale * 0.015)
            self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': w + ' '}}]})}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(f"data: {json.dumps({'choices': [], 'usage': usage})}\n\ndata: [DONE]\n\n".encode())
        self.wfile.flush()


class Server(ThreadingHTTPServer):
    request_queue_size = 256
    daemon_threads = True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8932)
    a = ap.parse_args()
    print(f"stub models on http://127.0.0.1:{a.port}", flush=True)
    Server(("127.0.0.1", a.port), Handler).serve_forever()
