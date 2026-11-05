"""Fake hosted vllm-sr API for tests and load tests: /v1/systemone (D3 and the Vela guardrail), /v1/chat/completions
(vllm-sr/auto: plain, json_schema, streaming, with the x-vsr-selected-* headers) and /v1/models, with realistic delays.
No model runs, no quota used.

Run:  python bench/stub_models.py [--port 8932]
Point the demo at it:  VLLM_SR_API_BASE=http://127.0.0.1:8932 VLLM_SR_API_KEY=stub python app/server.py
Switches (POST /stub/config, JSON, any subset; GET /stub/config shows them and the call counts):
  p429      share of model calls answered 429 (with Retry-After: retry_after)
  p503      share answered 503
  slow_ms   extra delay on every model call
  scale     multiplier on the normal delays
Per model ("d3", "guard", "llm"; /v1/models probes count as "models"):
  down      list of models whose calls get the connection dropped (no answer), like a stopped container
  slow      {model: extra ms}
  fail      {model: HTTP status}, e.g. {"llm": 500}
  fail_if   {text: status}: D3 calls whose state contains text get that status (a failure later in the turn)
  answers   {D3 question key: choice or yes/no value}, e.g. {"when": "after", "real_photo": 0.9}
  pick      Router decision for chat calls: fast | standard | hard (default: standard when the customer is upset or
            mentions a manager or lawyer, else fast)
  calls_by  (read only) calls per model
  key_error [status, code]: every model call answers this error (a key revoked or out of quota mid-chat)
API keys: the bearer token picks the answer, as the hosted API would for such a key (fake keys only):
  contains "invalid" -> 401 invalid_api_key, "disabled" -> 403 key_disabled, "quota" -> 429 quota_exceeded,
  "ratelim" -> 429 rate_limit_exceeded (Retry-After 7), "slowkey" -> no answer for 40 s (a timeout), sk-pt- -> 403
  model_not_allowed on /v1/systemone; anything else works.
POST /stub/config {"reset": true} puts every switch back to its default.
"""
import argparse
import json
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = ("vllm-sr/d3-flash", "vllm-sr/vela3-flash", "vllm-sr/auto")
UPSET = re.compile(r"manager|lawyer|legal|unacceptable|urgent|escalat", re.I)
DEFAULTS = {"p429": 0.0, "p503": 0.0, "slow_ms": 0, "scale": 1.0, "retry_after": 1, "down": [], "slow": {}, "fail": {},
            "fail_if": {}, "answers": {}, "pick": None, "key_error": None}
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


def key_refusal(token, kind):
    """(status, code) for a fake test key that the hosted API would refuse, else None."""
    for part, out in (("invalid", (401, "invalid_api_key")), ("disabled", (403, "key_disabled")),
                      ("quota", (429, "quota_exceeded")), ("ratelim", (429, "rate_limit_exceeded")),
                      ("slowkey", (0, "slow"))):
        if part in token:
            return out
    if token.startswith("sk-pt-") and kind == "d3":
        return 403, "model_not_allowed"
    return None


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
            with _lock:
                COUNTS["calls_by"]["models"] = COUNTS["calls_by"].get("models", 0) + 1
                if "models" in CONFIG["down"]:
                    self.close_connection = True
                    return
            return self._json(200, {"data": [{"id": m} for m in MODELS]})
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
        kind = {"/v1/systemone": "d3", "/v1/chat/completions": "chat"}.get(self.path)
        if not kind:
            return self._json(404, {"error": "not found"})
        auth = self.headers.get("authorization") or ""
        if not auth.startswith("Bearer "):
            return self._json(401, {"error": {"type": "auth", "code": "missing_api_key", "message": "no key"}})
        refused = key_refusal(auth[7:], kind) or CONFIG["key_error"]
        if refused:
            status, code = refused
            if code == "slow":
                time.sleep(40)
                self.close_connection = True
                return
            return self._json(status, {"error": {"type": "auth", "code": code, "message": "refused"}},
                              [("retry-after", "7")] if status == 429 else ())
        model = "llm" if kind == "chat" else "guard" if "vela" in str(body.get("model")) else "d3"
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
            slot = "chat" if kind == "chat" else model   # the app's slot kinds: d3, guard, chat
            COUNTS["busy_now"][slot] = COUNTS["busy_now"].get(slot, 0) + 1
            COUNTS["busy_max"][slot] = max(COUNTS["busy_max"].get(slot, 0), COUNTS["busy_now"][slot])
            scale, slow = CONFIG["scale"], (CONFIG["slow_ms"] + CONFIG["slow"].get(model, 0)) / 1000
        try:
            if down:   # a stopped backend: the connection closes without an answer
                self.close_connection = True
                return
            if code != 200:
                time.sleep(0.01)
                return self._json(code, {"error": {"type": "rate_limit", "code": "rate_limited", "message": "slow down"}},
                                  [("retry-after", str(CONFIG["retry_after"]))])
            time.sleep(slow)
            if fail:
                return self._json(int(fail), {"error": {"message": "upstream connect error or disconnect/reset before "
                                                                   "headers", "type": "stub_failure"}})
            getattr(self, kind)(body, scale)
        finally:
            with _lock:
                COUNTS["busy_now"][slot] -= 1

    def d3(self, body, scale):
        time.sleep(scale * (0.3 if body.get("images") else 0.05) * random.uniform(0.8, 1.3))
        self._json(200, {"answers": d3_answers(body.get("questions") or {}, CONFIG["answers"]),
                         "usage": {"input_tokens": 900 if body.get("images") else 200, "output_tokens": 5}})

    def chat(self, body, scale):
        last = next((m["content"] for m in reversed(body.get("messages") or []) if m.get("role") == "user"), "")
        rf = body.get("response_format") or {}
        if rf.get("type") == "json_schema":
            time.sleep(scale * 0.25 * random.uniform(0.8, 1.3))
            text = json.dumps(fill(rf["json_schema"]["schema"], last if isinstance(last, str) else ""))
        else:
            text = " ".join(WORDS[:random.randint(12, len(WORDS))])
        usage = {"prompt_tokens": 600, "completion_tokens": len(text.split()), "total_tokens": 600 + len(text.split())}
        decision = CONFIG["pick"] or ("standard" if UPSET.search(str(last)) else "fast")
        if "GUARDME" in str(last):   # the hosted Router's own guard: a refusal text, no model
            text = "This request was declined by the router's safety guard. Please rephrase it."
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            route = [("x-vsr-selected-decision", "guard")]
            if not body.get("stream"):
                return self._json(200, {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": usage}, route)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            for k, v in route:
                self.send_header(k, v)
            self.end_headers()
            self.close_connection = True
            self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n\ndata: [DONE]\n\n".encode())
            return
        route = [("x-vsr-selected-model", "qwen/qwen3.8-27b"), ("x-vsr-selected-decision", decision),
                 ("x-vsr-selected-reasoning", "off" if decision == "fast" else "on"),
                 ("x-vsr-matched-projections", {"fast": "effort_off", "standard": "effort_medium"}.get(decision, "effort_high")),
                 ("x-vsr-routing-latency-ms", "12.5")]
        if not body.get("stream"):
            time.sleep(scale * 0.4 * random.uniform(0.8, 1.3))
            return self._json(200, {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": usage}, route)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        for k, v in route:
            self.send_header(k, v)
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
