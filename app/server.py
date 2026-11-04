"""Returns photo check demo server (127.0.0.1:8602): the docs/API.md contract over app/flow.py, and the built page.

Many visitors at once: chats are kept in memory with limits (State, limits.py); see docs/API.md "Many visitors".

Run: ./start_server.sh [--bg]   (or: python app/server.py)
"""
import json
import mimetypes
import os
import re
import statistics
import sys
import threading
import time
import traceback
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import availability  # noqa: E402
import claim  # noqa: E402
from flow import NODES, Session, failed_steps  # noqa: E402
from limits import BUSY_TEXT, COUNTS, GATES, Busy, Limits, start_turn  # noqa: E402

try:  # iPhone photos
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
SAMPLES = ROOT / "data/samples"
PORT = int(os.environ.get("PORT", "8602"))
MAX_BODY = 25_000_000   # a base64 phone photo is ~4-8 MB
HOST = os.environ.get("HOST", "127.0.0.1")
PLAY_ALL = os.environ.get("PLAY_ALL", "1") != "0"
LANDING_URL = os.environ.get("LANDING_URL", "")   # "All demos" link in the top bar; empty hides it
MAX_CHATS, IDLE_S, PHOTO_S = 300, 30 * 60, 10 * 60
MAX_ACTIVE = 50          # chats mid-turn at once
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
RATE_TEXT = "You are sending messages faster than this demo allows. Please wait a minute and try again."
EXPIRED_TEXT = "This chat expired, starting a new one."
ERROR_TEXT = "Sorry, something went wrong on our side. Please send that again."
# Price of one GPU hour for the $ figures: DigitalOcean's on-demand MI300X GPU Droplet price per GPU (checked
# 2026-10-26). Set your own cloud's price.
GPU_USD_PER_HOUR = float(os.environ.get("GPU_USD_PER_HOUR", "2.59"))
BASELINE = (json.loads((ROOT / "data/baseline_27b.json").read_text())
            if (ROOT / "data/baseline_27b.json").exists() else None)
# which LLM answered each sample turn in the last bench/run_scenarios.py run (Vela's choice), for the "27B" marker
MODELS = (json.loads((ROOT / "data/scenario_models.json").read_text())
          if (ROOT / "data/scenario_models.json").exists() else {})
CASES = ["note_screen", "cut_off", "clear", "blurry", "dark", "glare", "intact", "hand", "finger", "note"]


class SessionStats:
    """Stats of one chat (the top bar is per chat: a new chat or a new sample starts from zero)."""
    def __init__(self):
        self.calls = []            # (kind, ms, tokens) of every model call
        self.reply_models = []     # model per turn
        self.baseline = []         # (seconds, tokens) of the all-27B run for each turn
        self.blocked = 0
        self.off_topic = 0
        self.avoided = 0           # claims closed before any photo check

    def as_dict(self):
        d3 = [ms for k, ms, _ in self.calls if k == "d3"]
        n = len(self.reply_models)
        llm = [m for m in self.reply_models if m]
        seconds = sum(ms for _k, ms, _t in self.calls) / 1000
        llm_tokens = sum(t for k, _ms, t in self.calls if k.startswith("qwen"))
        d3_tokens = sum(t for k, _ms, t in self.calls if k == "d3")
        b_seconds = sum(x for x, _ in self.baseline) if n else None
        b_tokens = sum(t for _, t in self.baseline) if n else None
        usd = lambda secs: round(secs / 3600 * GPU_USD_PER_HOUR * 1e6) if secs else None
        return {"turns": n, "d3_calls": len(d3), "d3_ms_median": round(statistics.median(d3)) if d3 else None,
                # share of the turns where an LLM actually wrote the reply
                "share": {m: round(llm.count(m) / len(llm), 3) for m in ("qwen9b", "qwen27b")} if llm else None,
                "llm_turns": len(llm),
                "blocked": self.blocked, "off_topic": self.off_topic, "image_checks_avoided": self.avoided,
                "model_seconds": {"routed": round(seconds, 2), "baseline_27b": round(b_seconds, 2) if n else None},
                "tokens": {"routed": llm_tokens, "baseline_27b": b_tokens, "d3": d3_tokens,
                           "note": "LLM tokens only; D3 reads images as tokens too (d3)"},
                "usd_per_million_chats": {"routed": usd(seconds), "baseline_27b": usd(b_seconds)},
                "gpu_usd_per_hour": GPU_USD_PER_HOUR,
                "baseline": BASELINE and {"correct": BASELINE["correct"], "turns": BASELINE["turns"],
                                          "measured": BASELINE["measured"]}}


def baseline_turn(text, image):
    """All-27B time and tokens for this turn: the measured value when it is a sample turn, else the measured average
    for that kind of turn (text only, or with a photo)."""
    if not BASELINE:
        return 0.0, 0
    hit = BASELINE.get("by_input", {}).get(image if image and not image.startswith("data:") else text or "")
    if hit and not (image and text and image.startswith("data:")):
        return hit["ms"] / 1000, hit["tokens"]
    a = BASELINE.get("avg", {}).get("image" if image else "text")
    if a:
        return a["ms"] / 1000, a["tokens"]
    return BASELINE["seconds"] / BASELINE["turns"], round(BASELINE["tokens"] / BASELINE["turns"])


class Chat:
    """One chat in memory: the flow session, its lock (one turn at a time), its stats and its turn ids."""

    def __init__(self, sid):
        self.session = Session(sid)
        self.lock = threading.Lock()
        self.stats = SessionStats()
        self.turns = []
        self.used = time.time()


class State:
    """Chats in memory, LRU: idle ones are dropped after IDLE_S, the least recently used over MAX_CHATS, and a photo
    kept for a chat without an order after PHOTO_S. A chat mid-turn is never dropped."""

    def __init__(self, max_chats=MAX_CHATS, idle_s=IDLE_S, photo_s=PHOTO_S):
        self.lock = threading.Lock()
        self.max_chats, self.idle_s, self.photo_s = max_chats, idle_s, photo_s
        self.chats = OrderedDict()   # sid -> Chat, least recently used first
        self.turns = {}              # turn id -> stored steps

    def get(self, sid, create=False):
        """The chat, or None when unknown (and not created)."""
        with self.lock:
            c = self.chats.get(sid)
            if c is None and create:
                c = self.chats[sid] = Chat(sid)
            if c is not None:
                c.used = time.time()
                self.chats.move_to_end(sid)
                over = len(self.chats) - self.max_chats
                for old in [k for k, v in self.chats.items() if not v.lock.locked()][:max(0, over)]:
                    self._drop(old)
            return c

    def _drop(self, sid):
        c = self.chats.pop(sid)
        for t in c.turns:
            self.turns.pop(t, None)
        c.session.pending_image = None

    def sweep(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            for sid, c in list(self.chats.items()):
                if not c.lock.acquire(blocking=False):
                    continue    # mid-turn
                try:
                    if now - c.used > self.idle_s:
                        self._drop(sid)
                    elif c.session.pending_image and now - c.session.pending_at > self.photo_s:
                        c.session.pending_image = None
                finally:
                    c.lock.release()

    def stats(self, sid):
        with self.lock:
            c = self.chats.get(sid)
            return {"session": sid, **(c.stats if c else SessionStats()).as_dict()}

    def record(self, done, verdict, chat, text, image):
        with self.lock:
            if self.chats.get(chat.session.sid) is not chat:   # dropped while this turn ran
                return
            st = chat.stats
            st.calls += done["calls"]
            # the model that wrote the reply; None when the turn got a fixed reply (blocked, off-topic): no LLM ran
            st.reply_models.append(next((k for k, _ms, _t in reversed(done["calls"]) if k.startswith("qwen")), None))
            st.baseline.append(baseline_turn(text, image))
            st.blocked += verdict == "blocked"
            st.off_topic += verdict == "off_topic"
            st.avoided += bool(done.get("avoided"))
            self.turns[done["turn"]] = {"turn": done["turn"], "steps": done["steps"]}
            chat.turns.append(done["turn"])


STATE = State()
LIMITS = Limits()
ACTIVE = threading.BoundedSemaphore(MAX_ACTIVE)   # chats mid-turn
METRICS = {"active": 0, "busy": 0, "rate_limited": 0, "expired": 0, "unavailable": 0, "errors": 0,
           "turns": deque()}   # turns: end times
_metrics_lock = threading.Lock()


def note(key, n=1):
    with _metrics_lock:
        METRICS[key] += n


def metrics():
    now = time.time()
    with _metrics_lock:
        while METRICS["turns"] and now - METRICS["turns"][0] > 3600:
            METRICS["turns"].popleft()
        m = {k: v for k, v in METRICS.items() if k != "turns"}
        m["turns_last_hour"] = len(METRICS["turns"])
    try:
        rss = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except (OSError, ValueError):
        rss = None
    return {"active_chats": m.pop("active"), "chats_in_memory": len(STATE.chats), **m,
            "queued_calls": {k: g.waiting for k, g in GATES.items()}, "throttled_429_503": COUNTS["throttled"],
            "memory_mb": rss and round(rss, 1)}


def sweeper():
    while True:
        time.sleep(30)
        STATE.sweep()
        LIMITS.sweep()


def log_turn(sid, verdict, t0, calls, throttled):
    models = ",".join(sorted({k for k, _ms, _t in calls})) or "-"
    print(f"turn {sid[:8]} verdict={verdict} ms={round((time.perf_counter() - t0) * 1000)} models={models} "
          f"429s={throttled}", flush=True)


def fixed_events(verdict, text, node_status, node_summary):
    """A reply that runs no model: the guardrail node carries why, the rest is skipped. No turn is recorded."""
    for n in NODES:
        first = n == "guardrail"
        yield {"type": "step", "node": n, "status": node_status if first else "skipped",
               "summary": node_summary if first else "Not run", "model": None, "ms": None, "detail": None}
    yield {"type": "token", "text": text}
    yield {"type": "message", "role": "assistant", "text": text, "verdict": verdict, "order": None, "policy_ref": None,
           "ended": None}


def samples():
    data = json.loads((ROOT / "data/scenarios.json").read_text())
    for g in data["groups"]:
        for sc in g["scenarios"]:
            sc["product"] = claim.ORDERS[sc["order"]]["product"] if sc.get("order") in claim.ORDERS else None
            # the outcome that matters: the last verdict before the claim was closed
            used = MODELS.get(sc["id"], [])
            for i, t in enumerate(sc["turns"]):
                t["model"] = used[i] if i < len(used) else None
            sc["uses_27b"] = "qwen27b" in used
            sc["expected"] = sc.get("expected") or next(t["expect"] for t in reversed(sc["turns"]) if t["expect"] != "closed")
    data["policies"] = claim.POLICIES
    return data


def gallery():
    out = []
    for p in sorted(SAMPLES.glob("*/*.jpg")):
        stem = p.stem
        case = next((c for c in CASES if stem.endswith("_" + c)), "other")
        out.append({"path": f"samples/{p.parent.name}/{p.name}", "product": p.parent.name, "case": case})
    return out


def busy_steps(nodes, e):
    """Steps for a turn that ran out of model capacity: the running nodes (else the first one) fail with the
    reason, the nodes not reached are skipped."""
    status = {n: nodes.get(n, "waiting") for n in NODES}
    failed = {n for n in NODES if status[n] == "running"} or {next((n for n in NODES if status[n] == "waiting"), None)}
    return [{"type": "step", "node": n, "status": "failed" if n in failed else "skipped",
             "summary": f"Service busy: {e}" if n in failed else "Not run: the service is busy",
             "model": None, "ms": None, "detail": {"capacity": str(e)} if n in failed else None}
            for n in NODES if status[n] in ("waiting", "running")]


class Server(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60   # per socket read or write: a stalled client frees its thread

    def log_message(self, *args):
        pass

    def _json(self, code, body, close=False):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-store")
        if close:   # the request body was not read: the connection cannot carry another request
            self.send_header("connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path):
        if not path.is_file():
            return self._json(404, {"error": "not found"})
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("content-type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def client_ip(self):
        """The visitor's address for the per-IP limits: the first X-Forwarded-For hop behind a proxy (Spaces), else
        the peer. None on loopback without a proxy (the presenter's machine or an SSH tunnel): no per-IP limits there,
        so Play all and the smoke test run freely."""
        fwd = self.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        peer = self.client_address[0]
        return None if peer in ("127.0.0.1", "::1") else peer

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/healthz":
            return self._json(200, {"ok": True})
        if path == "/health":   # for scripts: 503 unless every model is up
            h = availability.health()
            return self._json(200 if h["ok"] else 503, h)
        if path == "/api/health":   # for the page's health badge: always 200
            return self._json(200, availability.health())
        if path == "/api/metrics":
            return self._json(200, metrics())
        if path == "/api/config":
            return self._json(200, {"play_all": PLAY_ALL, "landing_url": LANDING_URL,
                                    "health": availability.health()})
        if path == "/api/samples":
            return self._json(200, samples())
        if path == "/api/customer":
            c = claim.CUSTOMER
            orders = sorted((claim.ORDERS[o] for o in c["orders"]), key=lambda o: o["delivered_days_ago"])
            return self._json(200, {"name": c["name"], "email": c["email"], "greeting": c["greeting"],
                                    "orders": [{**o, "product_name": claim.POLICIES[o["product"]]["name"],
                                                "window_days": claim.POLICIES[o["product"]]["window_days"]}
                                               for o in orders]})
        if path == "/api/gallery":
            return self._json(200, gallery())
        if path == "/api/stats":
            sid = (parse_qs(urlparse(self.path).query).get("session") or [""])[0].lower()
            return self._json(200, STATE.stats(sid)) if UUID.match(sid) else self._json(400, {"error": "bad session id"})
        if path.startswith("/api/turn/"):
            t = STATE.turns.get(path.rsplit("/", 1)[1])
            return self._json(200, t) if t else self._json(404, {"error": "unknown turn"})
        if path.startswith("/samples/"):
            p = (SAMPLES / path[len("/samples/"):]).resolve()
            return self._file(p) if p.is_relative_to(SAMPLES) else self._json(404, {"error": "not found"})
        p = (WEB / path.lstrip("/")).resolve()
        if path == "/" or not p.is_file() or not p.is_relative_to(WEB):
            p = WEB / "index.html"
        return self._file(p)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/chat":
            return self._json(404, {"error": "not found"}, close=True)
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            return self._json(400, {"error": "bad content-length"}, close=True)
        if length > MAX_BODY:   # checked before reading anything
            return self._json(413, {"error": f"upload too large (max {MAX_BODY // 1_000_000} MB)"}, close=True)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            req = None
        if not isinstance(req, dict):
            return self._json(400, {"error": "bad json"})
        sid, text, image = str(req.get("session") or "").lower(), req.get("text"), req.get("image")
        if not UUID.match(sid):
            return self._json(400, {"error": "session must be a UUID"})
        action = req.get("action") if req.get("action") in ("review",) else None
        text = text.strip() if isinstance(text, str) else None
        image = image if isinstance(image, str) and image else None
        if not (text or image or action):
            return self._json(400, {"error": "text, image or action required"})
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-store")
        self.send_header("x-accel-buffering", "no")
        self.send_header("connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self._chat(sid, text, image, action, bool(req.get("has_turns")))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _send(self, ev):
        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
        self.wfile.flush()

    def _chat(self, sid, text, image, action, has_turns):
        chat = STATE.get(sid)
        if chat is None and has_turns:   # the server restarted or dropped this chat
            note("expired")
            return self._send({"type": "expired", "message": EXPIRED_TEXT})
        if LIMITS.admit(self.client_ip(), sid, chat is None):
            note("rate_limited")
            print(f"turn {sid[:8]} verdict=rate_limited", flush=True)
            for ev in fixed_events("rate_limited", RATE_TEXT, "blocked", "Too many messages: fixed reply, no model call"):
                self._send(ev)
            return
        if not ACTIVE.acquire(blocking=False):
            note("busy")
            print(f"turn {sid[:8]} verdict=busy (chats mid-turn)", flush=True)
            for ev in fixed_events("busy", BUSY_TEXT, "failed", f"Service busy: {MAX_ACTIVE} chats mid-turn"):
                self._send(ev)
            return
        note("active")
        try:
            chat = chat or STATE.get(sid, create=True)
            with chat.lock:
                self._turn(chat, text, image, action)
        finally:
            note("active", -1)
            ACTIVE.release()

    def _turn(self, chat, text, image, action):
        session, sid = chat.session, chat.session.sid
        t0, counts = time.perf_counter(), start_turn()
        verdict, calls, nodes = None, [], {}
        try:
            for ev in session.turn(text, image, action):
                if ev["type"] == "step":
                    nodes[ev["node"]] = ev["status"]
                if ev["type"] == "message":
                    verdict = ev["verdict"]
                    if verdict == "unavailable":
                        note("unavailable")
                if ev["type"] == "done" and ev.get("no_turn"):
                    ev = {"type": "done", "turn": ev["turn"]}   # a message to an ended chat: no stats
                elif ev["type"] == "done":
                    calls = ev["calls"]
                    STATE.record(ev, verdict, chat, text, image)
                    self._send({"type": "stats", **STATE.stats(sid)})
                    ev = {"type": "done", "turn": ev["turn"]}
                self._send(ev)
        except Busy as e:   # no model capacity: a capacity step in the flow and a polite reply, not a crash
            verdict = f"busy ({e})"
            note("busy")
            self._end_turn(session, BUSY_TEXT, "busy", busy_steps(nodes, e))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            raise
        except Exception:  # a bug: end the turn cleanly, keep serving; the detail goes to the log only
            verdict = "error"
            note("errors")
            print(f"turn {sid[:8]} failed:\n{traceback.format_exc()}", flush=True)
            self._end_turn(session, ERROR_TEXT, "error",
                           failed_steps(nodes, "Something went wrong on our side"))
        finally:
            with _metrics_lock:
                METRICS["turns"].append(time.time())
            chat.used = time.time()
            log_turn(sid, verdict, t0, calls, counts["throttled"])

    def _end_turn(self, session, text, verdict, events):
        if session.history and session.history[-1]["role"] == "user":
            session.history.append({"role": "assistant", "content": text})
        try:
            for ev in events:
                self._send(ev)
            self._send({"type": "message", "role": "assistant", "text": text, "verdict": verdict, "order": None,
                        "policy_ref": None})
        except OSError:
            pass


if __name__ == "__main__":
    print(f"returns validator demo on http://{HOST}:{PORT} (play all: {PLAY_ALL})", flush=True)
    threading.Thread(target=sweeper, daemon=True).start()
    availability.start_probes()
    Server((HOST, PORT), Handler).serve_forever()
