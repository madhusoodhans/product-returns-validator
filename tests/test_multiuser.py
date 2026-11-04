"""Unit tests for many visitors at once: chat eviction and expiry, rate limits, model-call slots, the 429 retry.
No models are called (a tiny local HTTP server plays the model API where needed).

Run: .venv/bin/python tests/test_multiuser.py
"""
import base64
import io
import json
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import limits  # noqa: E402
import server  # noqa: E402
from limits import Busy, Gate, Limits, Window  # noqa: E402


def sid():
    return str(uuid.uuid4())


def test_idle_eviction_drops_everything():
    st = server.State(max_chats=10, idle_s=1800, photo_s=600)
    s = sid()
    c = st.get(s, create=True)
    c.session.pending_image, c.session.pending_at = "data:image/jpeg;base64,xx", time.time()
    st.record({"turn": s + "-t1", "steps": [], "calls": []}, "need_order", c, "hi", None)
    assert s + "-t1" in st.turns
    st.sweep(now=time.time() + 1700)
    assert st.get(s) is c
    st.sweep(now=time.time() + 1801)
    assert st.get(s) is None and s + "-t1" not in st.turns and c.session.pending_image is None
    assert st.stats(s)["turns"] == 0


def test_mid_turn_chat_is_kept():
    st = server.State(max_chats=10, idle_s=1)
    s = sid()
    c = st.get(s, create=True)
    with c.lock:
        st.sweep(now=time.time() + 100)
        assert st.get(s) is c
    st.sweep(now=time.time() + 100)
    assert st.get(s) is None


def test_lru_cap():
    st = server.State(max_chats=3)
    ids = [sid() for _ in range(4)]
    for s in ids[:3]:
        st.get(s, create=True)
    st.get(ids[0])               # used again: now the most recent
    st.get(ids[3], create=True)  # over the cap: ids[1] is the least recently used
    assert list(st.chats) == [ids[2], ids[0], ids[3]]


def test_pending_photo_dropped():
    st = server.State(photo_s=600)
    c = st.get(sid(), create=True)
    c.session.pending_image, c.session.pending_at = "data:x", time.time()
    st.sweep(now=time.time() + 300)
    assert c.session.pending_image == "data:x"
    st.sweep(now=time.time() + 601)
    assert c.session.pending_image is None and c.session.sid in st.chats


def test_window_and_limits():
    w = Window(2, per=60)
    assert not w.full("a", 0)
    w.add("a", 0), w.add("a", 1)
    assert w.full("a", 59) and not w.full("a", 60.5)
    w.sweep(200)
    assert not w.hits
    lim = Limits(chat_msgs=10, ip_msgs=30, ip_chats=5)
    t = 1000.0
    chats = [sid() for _ in range(6)]
    assert [lim.admit("1.1.1.1", c, True, t) for c in chats] == [None] * 5 + ["ip_chats"]
    one = chats[0]
    got = [lim.admit("1.1.1.1", one, False, t + 1) for _ in range(10)]
    assert got == [None] * 9 + ["chat_messages"]   # 1 + 9 messages in this chat
    others = [lim.admit("1.1.1.1", c, False, t + 2) for c in chats[1:5] for _ in range(5)]
    assert others.count(None) == 16 and others[-1] == "ip_messages"   # 30 per IP: 5 + 9 + 16
    assert lim.admit("2.2.2.2", sid(), True, t + 2) is None           # another visitor is not affected
    assert lim.admit("1.1.1.1", one, False, t + 62) is None           # a minute later
    assert all(lim.admit(None, sid(), True, t + 63) is None for _ in range(20))   # loopback: no per-IP limits


def test_gate_limits_concurrency():
    g = Gate("test", slots=3, queue=100, wait=5)
    now, peak, lock = [0], [0], threading.Lock()

    def work():
        with g:
            with lock:
                now[0] += 1
                peak[0] = max(peak[0], now[0])
            time.sleep(0.02)
            with lock:
                now[0] -= 1
    ts = [threading.Thread(target=work) for _ in range(30)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 3, peak


def test_gate_wait_and_queue():
    g = Gate("test", slots=1, queue=1, wait=0.2)
    g.__enter__()
    t = time.perf_counter()
    try:
        with g:
            raise AssertionError("should not get a slot")
    except Busy as e:
        assert "waited" in str(e) and time.perf_counter() - t >= 0.2
    g.waiting = 1   # one caller already waiting: the next one is turned away at once
    try:
        with g:
            raise AssertionError("queue should be full")
    except Busy as e:
        assert "waiting" in str(e)
    g.waiting = 0
    g.__exit__()


class Fake(BaseHTTPRequestHandler):
    """Answers with the next status in `codes` (429s carry Retry-After: 0.3)."""
    codes, seen = [], []

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        code = Fake.codes.pop(0) if Fake.codes else 200
        Fake.seen.append((code, time.perf_counter()))
        body = json.dumps({"ok": code == 200}).encode()
        self.send_response(code)
        if code != 200:
            self.send_header("retry-after", "0.3")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def fake_url():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/x"


def test_429_retry_once_then_busy():
    srv, url = fake_url()
    try:
        Fake.codes, Fake.seen = [429], []
        counts = limits.start_turn()
        with limits.post("d3", url, json={}, timeout=5) as r:
            assert r.json() == {"ok": True}
        assert [c for c, _ in Fake.seen] == [429, 200] and Fake.seen[1][1] - Fake.seen[0][1] >= 0.29
        assert counts["throttled"] == 1
        Fake.codes, Fake.seen = [429, 503], []
        try:
            with limits.post("chat", url, json={}, timeout=5):
                raise AssertionError("should be busy")
        except Busy as e:
            assert "503" in str(e)
        assert counts["throttled"] == 3 and len(Fake.seen) == 2
    finally:
        srv.shutdown()


def test_retry_after_cap():
    class R:
        def __init__(self, v):
            self.headers = {"retry-after": v} if v is not None else {}
    assert limits.retry_after(R("120")) == limits.RETRY_CAP_S
    assert limits.retry_after(R("2")) == 2 and limits.retry_after(R(None)) == 1.0
    assert limits.retry_after(R("Wed, 21 Oct 2015 07:28:00 GMT")) == 0.0


def test_busy_steps():
    nodes = {n: "waiting" for n in server.NODES} | {"guardrail": "done", "router": "done", "claim": "running"}
    st = {e["node"]: e["status"] for e in server.busy_steps(nodes, Busy("x"))}
    assert st["claim"] == "failed" and "guardrail" not in st and st["reply"] == "skipped"
    st = {e["node"]: e["status"] for e in server.busy_steps({}, Busy("x"))}
    assert st["guardrail"] == "failed" and st["reply"] == "skipped"


def test_large_jpeg_decodes_reduced():
    from PIL import Image
    import models
    buf = io.BytesIO()
    Image.new("RGB", (8000, 6000), (120, 80, 40)).save(buf, "JPEG", quality=70)
    url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    out = Image.open(io.BytesIO(base64.b64decode(models.image_url(url).split(",", 1)[1])))
    assert out.size == (1600, 1200), out.size


def http_server():
    srv = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def chat(base, body, ip="9.9.9.9"):
    req = urllib.request.Request(base + "/api/chat", json.dumps(body).encode(),
                                 {"content-type": "application/json", "x-forwarded-for": f"{ip}, 10.0.0.1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, [json.loads(l[5:]) for l in r.read().decode().splitlines() if l.startswith("data:")]
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_session_rules():
    """No models: these replies are decided before the flow runs."""
    server.STATE, server.LIMITS = server.State(), Limits()
    srv, base = http_server()
    try:
        assert chat(base, {"session": "default", "text": "hi"})[0] == 400
        assert chat(base, {"text": "hi"})[0] == 400
        code, evs = chat(base, {"session": sid(), "text": "hi", "has_turns": True})
        assert code == 200 and evs == [{"type": "expired", "message": server.EXPIRED_TEXT}]
        req = urllib.request.Request(base + "/api/reset", b"{}")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("reset should be gone")
        except urllib.error.HTTPError as e:
            assert e.code == 404
        # an ended chat answers without models: use it to hit the per-chat limit
        s = sid()
        c = server.STATE.get(s, create=True)
        c.session.ended = {"reason": "approved"}
        server.LIMITS.admit("8.8.8.8", s, True)
        verdicts = [next(e["verdict"] for e in chat(base, {"session": s, "text": "hi", "has_turns": True},
                                                       "8.8.8.8")[1] if e["type"] == "message") for _ in range(10)]
        assert verdicts[:9] == ["ended"] * 9 and verdicts[9] == "rate_limited", verdicts
        _, evs = chat(base, {"session": s, "text": "hi", "has_turns": True}, "8.8.8.8")
        assert evs[-1]["text"] == server.RATE_TEXT and evs[0]["status"] == "blocked"
        with urllib.request.urlopen(base + "/healthz", timeout=5) as r:
            assert json.loads(r.read()) == {"ok": True}
        with urllib.request.urlopen(base + "/api/metrics", timeout=5) as r:
            m = json.loads(r.read())
            assert m["rate_limited"] >= 2 and m["expired"] >= 1 and "queued_calls" in m
    finally:
        srv.shutdown()


def test_body_limit_before_read():
    import socket
    srv, base = http_server()
    try:
        s = socket.create_connection(("127.0.0.1", srv.server_port), timeout=5)
        s.sendall(b"POST /api/chat HTTP/1.1\r\nHost: x\r\nContent-Length: 999999999\r\n\r\n")   # no body sent
        head = s.recv(4096).decode()
        assert head.startswith("HTTP/1.1 413") and "connection: close" in head.lower(), head
        s.close()
        s = socket.create_connection(("127.0.0.1", srv.server_port), timeout=5)
        s.sendall(b"POST /api/chat HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n")
        assert s.recv(4096).decode().startswith("HTTP/1.1 400")
        s.close()
    finally:
        srv.shutdown()


if __name__ == "__main__":
    tests = [(k, v) for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in tests:
        t = time.perf_counter()
        try:
            fn()
            print(f"ok   {name} ({time.perf_counter() - t:.2f} s)")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
