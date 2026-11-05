"""Returns photo check demo server (127.0.0.1:8602): the docs/API.md contract over app/flow.py, and the built page.

Models: the hosted vllm-sr API (VLLM_SR_API_BASE, default https://app.vllm-sr.ai) with each visitor's own key, entered
in the page (keys.py, docs/API.md "API key"); VLLM_SR_API_KEY is an optional fallback, off by default.

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
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import availability  # noqa: E402
import claim  # noqa: E402
import keys  # noqa: E402
import models  # noqa: E402
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


def _origin(url):
    u = urlsplit(url or "")
    return f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else None


# CORS for POST /api/key/check only: the landing page checks a key through this backend (the hosted API sends no CORS)
KEY_CHECK_ORIGINS = {o for o in (_origin(LANDING_URL), "http://localhost:8630", "http://127.0.0.1:8630") if o}
MAX_CHATS, IDLE_S, PHOTO_S = 300, 30 * 60, 10 * 60
MAX_ACTIVE = 50          # chats mid-turn at once
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
RATE_TEXT = "You are sending messages faster than this demo allows. Please wait a minute and try again."
EXPIRED_TEXT = "This chat expired, starting a new one."
ERROR_TEXT = "Sorry, something went wrong on our side. Please send that again."
# TRUST_PROXY=N: the app runs behind N proxies (a Hugging Face Space: 1) that append the client address to
# X-Forwarded-For; the per-IP limits then use the address the nearest trusted proxy saw. Unset: X-Forwarded-For is
# ignored (a client could write anything there) and the peer address is used.
TRUST_PROXY = int(os.environ.get("TRUST_PROXY", "0") or 0)
# what vllm-sr/auto picked for each sample turn in the last bench/run_scenarios.py run (the Router decides live)
ROUTES = (json.loads((ROOT / "data/scenario_routes.json").read_text())
          if (ROOT / "data/scenario_routes.json").exists() else {})
CASES = ["note_screen", "cut_off", "clear", "blurry", "dark", "glare", "intact", "hand", "finger", "note"]


class SessionStats:
    """Stats of one chat (the top bar is per chat: a new chat or a new sample starts from zero)."""
    def __init__(self):
        self.calls = []            # (kind, ms, tokens) of every model call: kind d3, vela or llm; llm tokens = completion
        self.reply_routes = []     # per turn: what vllm-sr/auto picked for the reply ({model, decision, reasoning}), or None
        self.blocked = 0
        self.off_topic = 0
        self.avoided = 0           # claims closed before any photo check

    def as_dict(self):
        d3 = [ms for k, ms, _ in self.calls if k == "d3"]
        decision = [ms for k, ms, _ in self.calls if k in ("d3", "vela")]
        n = len(self.reply_routes)
        llm = [r for r in self.reply_routes if r]
        llm_ms = sum(ms for k, ms, _t in self.calls if k == "llm")
        decisions = {}
        for r in llm:
            decisions[r.get("decision") or "unknown"] = decisions.get(r.get("decision") or "unknown", 0) + 1
        return {"turns": n, "d3_calls": len(d3), "d3_ms_median": round(statistics.median(d3)) if d3 else None,
                # the turns where the language model wrote the reply, by the Router's decision and reasoning
                "llm_turns": len(llm), "routes": decisions,
                "reasoning_turns": sum(r.get("reasoning") == "on" for r in llm),
                "blocked": self.blocked, "off_topic": self.off_topic, "image_checks_avoided": self.avoided,
                # this chat's model time in ms: decision models (D3 and Vela), the language model, and all model calls
                "model_ms": {"decision": round(sum(decision), 1), "llm": round(llm_ms, 1),
                             "total": round(sum(ms for _k, ms, _t in self.calls), 1)} if n else None,
                "llm_tokens": sum(t for k, _ms, t in self.calls if k == "llm") if n else None,
                "d3_tokens": sum(t for k, _ms, t in self.calls if k in ("d3", "vela"))}


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
            # what the Router picked for the reply; None when no LLM wrote it (fixed or standard reply, the Router's guard)
            r = done.get("route")
            st.reply_routes.append({k: r.get(k) for k in ("model", "decision", "reasoning")} if r and r.get("model") else None)
            st.blocked += verdict == "blocked"
            st.off_topic += verdict == "off_topic"
            st.avoided += bool(done.get("avoided"))
            self.turns[done["turn"]] = {"turn": done["turn"], "steps": done["steps"]}
            chat.turns.append(done["turn"])


STATE = State()
LIMITS = Limits()
KEYS = keys.Store()           # visitor id -> the visitor's API key, in memory only
KEY_ATTEMPTS = keys.Attempts()
ACTIVE = threading.BoundedSemaphore(MAX_ACTIVE)   # chats mid-turn
METRICS = {"active": 0, "busy": 0, "rate_limited": 0, "expired": 0, "unavailable": 0, "errors": 0, "key_failed": 0,
           "no_key": 0,
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
        KEYS.sweep()
        KEY_ATTEMPTS.sweep()


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
            used = ROUTES.get(sc["id"], [])
            for i, t in enumerate(sc["turns"]):
                t["route"] = used[i] if i < len(used) else None
            sc["uses_reasoning"] = any(r and r.get("reasoning") == "on" for r in used)
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

    def _json(self, code, body, close=False, cors=False):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-store")
        if cors:
            self._cors()
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

    def _cors(self):
        """Allow the landing page's origin (POST /api/key/check only)."""
        origin = self.headers.get("origin")
        self.send_header("vary", "Origin")
        if origin in KEY_CHECK_ORIGINS:
            self.send_header("access-control-allow-origin", origin)
            self.send_header("access-control-allow-methods", "POST, OPTIONS")
            self.send_header("access-control-allow-headers", "content-type")
            self.send_header("access-control-max-age", "600")

    def visitor(self):
        """This tab's visitor id (X-Visitor-Id, a UUID the page keeps in sessionStorage), or None."""
        v = (self.headers.get("x-visitor-id") or "").strip().lower()
        return v if UUID.match(v) else None

    def key_state(self):
        v = self.visitor()
        return {"set": KEYS.has(v), "server_key": keys.env_key() is not None, "failed": KEYS.failure(v)}

    def do_OPTIONS(self):
        path = urlparse(self.path).path
        if path != "/api/key/check":
            return self._json(404, {"error": "not found"})
        self.send_response(204)
        self._cors()
        self.send_header("content-length", "0")
        self.end_headers()

    def client_ip(self):
        """The visitor's address for the per-IP limits. TRUST_PROXY=N (behind N proxies, a Space: 1): the N-th
        X-Forwarded-For entry from the right, the address the nearest trusted proxy saw (entries further left are
        written by the client and can be faked). Otherwise the peer; None on loopback without a proxy (the presenter's
        machine or an SSH tunnel): no per-IP limits there, so Play all and the smoke test run freely."""
        peer = self.client_address[0]
        if TRUST_PROXY > 0:
            hops = [h.strip() for h in (self.headers.get("x-forwarded-for") or "").split(",") if h.strip()]
            if hops:
                return hops[-min(TRUST_PROXY, len(hops))]
            return peer
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
                                    "health": availability.health(), "key": self.key_state()})
        if path == "/api/key":   # never the key or any part of it
            return self._json(200, self.key_state())
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

    def _read_json(self, max_body=MAX_BODY, cors=False):
        """The request body as a dict, or None after answering the error."""
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            return self._json(400, {"error": "bad content-length"}, close=True, cors=cors)
        if length > max_body:   # checked before reading anything
            return self._json(413, {"error": f"upload too large (max {max(1, max_body // 1_000_000)} MB)"}, close=True,
                              cors=cors)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            req = None
        if not isinstance(req, dict):
            return self._json(400, {"error": "bad json"}, cors=cors)
        return req

    def do_DELETE(self):
        if urlparse(self.path).path != "/api/key":
            return self._json(404, {"error": "not found"})
        KEYS.delete(self.visitor())
        return self._json(200, {"ok": True})

    def do_POST(self):
        path = urlparse(self.path).path
        if path in ("/api/key/check", "/api/key"):
            return self._key(path)
        if path != "/api/chat":
            return self._json(404, {"error": "not found"}, close=True)
        req = self._read_json()
        if req is None:
            return
        sid, text, image = str(req.get("session") or "").lower(), req.get("text"), req.get("image")
        if not UUID.match(sid):
            return self._json(400, {"error": "session must be a UUID"})
        action = req.get("action") if req.get("action") in ("review",) else None
        text = text.strip() if isinstance(text, str) else None
        image = image if isinstance(image, str) and image else None
        if not (text or image or action):
            return self._json(400, {"error": "text, image or action required"})
        visitor = self.visitor()
        key = KEYS.get(visitor) or keys.env_key()
        if not key:   # nothing runs without a key
            note("no_key")
            failed = KEYS.failure(visitor)
            return self._json(401, {"code": "no_key", "message": keys.message("no_key"), "failed": failed,
                                    "error": keys.message("no_key")})
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-store")
        self.send_header("x-accel-buffering", "no")
        self.send_header("connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            keys.CURRENT.set(key)   # this request's thread (and the flow's worker threads, which copy the context)
            self._chat(sid, text, image, action, bool(req.get("has_turns")), visitor)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            keys.CURRENT.set(None)

    def _key(self, path):
        """POST /api/key/check (validate only, CORS for the landing page) and POST /api/key (validate, then keep it for
        this visitor). Answers {"ok": true} or {"ok": false, "code", "message"}; the key is never echoed or logged."""
        check = path == "/api/key/check"
        req = self._read_json(max_body=4096, cors=check)
        if req is None:
            return
        visitor = self.visitor()
        if not check and not visitor:
            return self._json(400, {"ok": False, "code": "error", "message": "Missing visitor id."})
        if not KEY_ATTEMPTS.allow(visitor, self.client_ip() or ("local" if not visitor else None)):
            code = "too_many_checks"
            return self._json(429, {"ok": False, "code": "rate_limited", "message": keys.message(code)}, cors=check)
        ok, code, msg = keys.validate(req.get("key"))
        print(f"key {'check' if check else 'set'} {'ok' if ok else code}", flush=True)
        if not ok:
            return self._json(keys.HTTP_STATUS.get(code, 400), {"ok": False, "code": code, "message": msg}, cors=check)
        if not check:
            KEYS.set(visitor, req["key"])
        return self._json(200, {"ok": True}, cors=check)

    def _send(self, ev):
        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
        self.wfile.flush()

    def _chat(self, sid, text, image, action, has_turns, visitor=None):
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
                self._turn(chat, text, image, action, visitor)
        finally:
            note("active", -1)
            ACTIVE.release()

    def _turn(self, chat, text, image, action, visitor=None):
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
        except keys.KeyFailed as e:   # the visitor's key was refused: stop the turn and say so (no template reply)
            verdict = f"key_failed ({e.code})"
            note("key_failed")
            KEYS.fail(visitor, e.code)
            try:
                for ev in failed_steps(nodes, "Stopped: the API key was refused"):
                    self._send(ev)
                self._send({"type": "key_error", "code": e.code, "message": e.text})
            except OSError:
                pass
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
    keys.guard_logs()   # anything shaped like a key is printed as sk-***
    print(f"models: {models.D3_MODEL}, {models.GUARD_MODEL}, {models.LLM_MODEL} on {models.API_BASE}", flush=True)
    print(f"returns validator demo on http://{HOST}:{PORT} (play all: {PLAY_ALL}; API key: "
          f"{'env key as fallback' if keys.env_key() else 'entered in the browser'})", flush=True)
    threading.Thread(target=sweeper, daemon=True).start()
    availability.start_probes()
    Server((HOST, PORT), Handler).serve_forever()
