"""Unit tests for the visitor's API key (docs/API.md "API key"): format checks, error-code mapping, the key endpoints,
no_key gating, a key refused mid-turn, CORS for /api/key/check, attempt limits, and that a key never shows up in logs,
events, metrics or answers. bench/stub_models.py plays the hosted API in-process; fake keys pick its answers (a key
containing "invalid" gets 401, "quota" 429 quota_exceeded, ...). No real key, no quota.

Run: .venv/bin/python tests/test_keys.py
"""
import contextlib
import io
import json
import os
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.pop("VLLM_SR_API_KEY", None)
VSR = "sk-" + "vsr-"   # fake keys only, built so no key-shaped literal sits in the repo
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "bench"))
import availability  # noqa: E402
import keys  # noqa: E402
import models  # noqa: E402
import server  # noqa: E402
import stub_models  # noqa: E402
from limits import Limits  # noqa: E402

STUB = stub_models.Server(("127.0.0.1", 0), stub_models.Handler)
threading.Thread(target=STUB.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{STUB.server_port}"
models.API_BASE = keys.API_BASE = URL
GOOD = VSR + "test-fake-good-0001"
CRACKED = "Order 4411, the phone screen was cracked when I opened the box."


def stub(**cfg):
    req = urllib.request.Request(URL + "/stub/config", json.dumps(cfg).encode(), {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def calls():
    with urllib.request.urlopen(URL + "/stub/config", timeout=5) as r:
        return json.loads(r.read())["calls_by"]


def fresh():
    stub(reset=True, scale=0.05, answers={"says": "other"})
    availability.TRACKER.reset()
    server.STATE, server.LIMITS = server.State(), Limits()
    server.KEYS, server.KEY_ATTEMPTS = keys.Store(), keys.Attempts(limit=1000)
    with keys._ok_lock:
        keys._ok.clear()


def http_server():
    srv = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def call(base, method, path, body=None, headers=None):
    """(status, headers, text)."""
    data = json.dumps(body).encode() if body is not None else None
    h = {"content-type": "application/json", **(headers or {})}
    req = urllib.request.Request(base + path, data, h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def events(text):
    return [json.loads(l[5:]) for l in text.splitlines() if l.startswith("data:")]


# ---------------------------------------------------------------------------------------------- format and mapping

def test_format_checks():
    assert keys.check_format(GOOD) is None
    assert keys.check_format("  " + GOOD + "\n") is None          # trimmed
    assert keys.check_format("sk-pt-abcdefghijkl") == "wrong_kind"
    for bad in ("", "   ", None, 42, VSR + "", VSR + "short", VSR + "has space-inside", "abc-" + VSR + "123456789",
                VSR + "" + "a" * 300, "Bearer " + GOOD, VSR + "abc/def+ghij"):
        assert keys.check_format(bad) == "format", bad
    assert "sk-pt keys only work for chat" in keys.message("wrong_kind")
    assert "00:00 UTC" in keys.message("quota")


def test_validate_error_codes():
    fresh()
    cases = {VSR + "test-fake-invalid": "invalid", VSR + "test-fake-disabled": "disabled",
             VSR + "test-fake-quota": "quota", VSR + "test-fake-ratelim": "rate_limited",
             "sk-pt-test-fake-0001": "wrong_kind", "nonsense": "format"}
    for k, want in cases.items():
        ok, code, msg = keys.validate(k)
        assert not ok and code == want, (k, code)
        assert k not in msg, msg
    ok, code, msg = keys.validate(VSR + "test-fake-ratelim")
    assert "7 seconds" in msg, msg                      # Retry-After from the API
    assert keys.validate(GOOD) == (True, None, None)
    n = calls()
    assert n.get("d3") == 1 and n.get("llm") == 1, n   # one System One and one System Two call per check
    keys.validate(GOOD)                                  # a key that just passed is not checked again
    assert calls() == n


def test_validate_403_codes_and_5xx():
    fresh()
    class R:   # a response object as requests gives it
        def __init__(self, status, code=None, retry=None):
            self.status_code, self._code = status, code
            self.headers = {"retry-after": retry} if retry else {}

        def json(self):
            if self._code is None:
                raise ValueError("not json")
            return {"error": {"type": "x", "code": self._code, "message": "m"}}
    assert keys.classify(R(401, "missing_api_key")) == ("invalid", None)
    assert keys.classify(R(403, "access_denied")) == ("disabled", None)
    assert keys.classify(R(403, "key_disabled")) == ("disabled", None)
    assert keys.classify(R(403, "model_not_allowed")) == ("wrong_kind", None)
    assert keys.classify(R(429, "quota_exceeded")) == ("quota", None)
    assert keys.classify(R(429, "concurrency_limit", "3")) == ("rate_limited", 3)
    assert keys.classify(R(429)) == ("rate_limited", None)
    assert keys.classify(R(502)) == ("unreachable", None)
    assert keys.classify(R(400, "bad_request")) == ("error", None)
    # mid-turn: only the key's own failures stop a turn; other 429s stay "busy"
    assert keys.key_failure(R(429, "rate_limit_exceeded")) is None
    assert keys.key_failure(R(429, "quota_exceeded")).code == "quota"
    assert keys.key_failure(R(401)).code == "invalid"
    assert keys.key_failure(R(500)) is None


def test_validate_timeout_and_unreachable():
    fresh()
    keep = keys.CHECK_TIMEOUT
    keys.CHECK_TIMEOUT = (1.0, 0.5)
    try:
        t = time.perf_counter()
        ok, code, _ = keys.validate(VSR + "test-fake-slowkey")
        assert not ok and code == "unreachable" and time.perf_counter() - t < 5
        ok, code, _ = keys.validate(GOOD + "x", base="http://127.0.0.1:9")   # nothing listens there
        assert not ok and code == "unreachable"
    finally:
        keys.CHECK_TIMEOUT = keep


# ---------------------------------------------------------------------------------------------- endpoints

def test_key_endpoints_and_no_key_gating():
    fresh()
    srv, base = http_server()
    v = {"x-visitor-id": str(uuid.uuid4())}
    try:
        st, _h, body = call(base, "GET", "/api/key", headers=v)
        assert st == 200 and json.loads(body) == {"set": False, "server_key": False, "failed": None}
        # no key: nothing runs, no model call
        st, _h, body = call(base, "POST", "/api/chat", {"session": str(uuid.uuid4()), "text": CRACKED}, v)
        assert st == 401 and json.loads(body)["code"] == "no_key" and calls() == {}
        # a bad key is refused and not stored
        st, _h, body = call(base, "POST", "/api/key", {"key": VSR + "test-fake-invalid"}, v)
        assert st == 401 and json.loads(body)["code"] == "invalid"
        st, _h, body = call(base, "POST", "/api/key", {"key": "sk-pt-test-fake-0001"}, v)
        assert st == 400 and json.loads(body)["code"] == "wrong_kind"
        assert not json.loads(call(base, "GET", "/api/key", headers=v)[2])["set"]
        # no visitor id: POST /api/key cannot store anything
        assert call(base, "POST", "/api/key", {"key": GOOD})[0] == 400
        # check only: validates, stores nothing
        st, _h, body = call(base, "POST", "/api/key/check", {"key": GOOD}, v)
        assert st == 200 and json.loads(body) == {"ok": True}
        assert not json.loads(call(base, "GET", "/api/key", headers=v)[2])["set"]
        # set: the visitor's turns run with it
        st, _h, body = call(base, "POST", "/api/key", {"key": GOOD}, v)
        assert st == 200 and json.loads(body) == {"ok": True}
        assert json.loads(call(base, "GET", "/api/key", headers=v)[2])["set"] is True
        st, _h, body = call(base, "POST", "/api/chat", {"session": str(uuid.uuid4()), "text": CRACKED}, v)
        evs = events(body)
        assert st == 200 and any(e["type"] == "message" for e in evs) and any(e["type"] == "done" for e in evs)
        # another visitor does not get it
        other = {"x-visitor-id": str(uuid.uuid4())}
        assert call(base, "POST", "/api/chat", {"session": str(uuid.uuid4()), "text": "hi"}, other)[0] == 401
        # config reports the key state (never the key)
        cfg = json.loads(call(base, "GET", "/api/config", headers=v)[2])
        assert cfg["key"]["set"] is True and GOOD not in json.dumps(cfg)
        # health needs no key
        st, _h, body = call(base, "GET", "/api/health")
        assert st == 200 and "status" in json.loads(body)
        # DELETE forgets it
        assert call(base, "DELETE", "/api/key", headers=v)[0] == 200
        assert call(base, "POST", "/api/chat", {"session": str(uuid.uuid4()), "text": "hi"}, v)[0] == 401
    finally:
        srv.shutdown()


def test_key_refused_mid_turn_is_an_error_not_a_template():
    srv, base = http_server()
    try:
        for status, api_code, want in ((401, "invalid_api_key", "invalid"), (403, "key_disabled", "disabled"),
                                       (403, "access_denied", "disabled"), (429, "quota_exceeded", "quota")):
            fresh()
            v = {"x-visitor-id": str(uuid.uuid4())}
            assert call(base, "POST", "/api/key", {"key": GOOD}, v)[0] == 200
            stub(key_error=[status, api_code])   # the key is revoked or out of quota after it was set
            sid = str(uuid.uuid4())
            st, _h, body = call(base, "POST", "/api/chat", {"session": sid, "text": CRACKED}, v)
            evs = events(body)
            err = [e for e in evs if e["type"] == "key_error"]
            assert st == 200 and len(err) == 1 and err[0]["code"] == want, evs
            assert not any(e["type"] == "message" for e in evs), "no template reply for a key error"
            assert any(e["type"] == "step" and e["status"] == "failed" for e in evs)
            # the breaker does not count it: the models are not "down" for everyone else
            assert all(s["state"] != "down" for s in availability.TRACKER.snapshot().values())
            # the visitor's key is dropped and the reason kept: the next action asks for a key again
            state = json.loads(call(base, "GET", "/api/key", headers=v)[2])
            assert state == {"set": False, "server_key": False, "failed": want}, state
            st, _h, body = call(base, "POST", "/api/chat", {"session": sid, "text": "hi", "has_turns": True}, v)
            assert st == 401 and json.loads(body)["code"] == "no_key"
            c = server.STATE.get(sid)   # the chat is as it was before the turn: only the greeting
            assert c is None or (len(c.session.history) == 1 and c.session.turns == 0), c.session.history
        # a rate limit mid-turn stays the busy reply (not a key error)
        fresh()
        v = {"x-visitor-id": str(uuid.uuid4())}
        assert call(base, "POST", "/api/key", {"key": GOOD}, v)[0] == 200
        stub(key_error=[429, "rate_limit_exceeded"])
        evs = events(call(base, "POST", "/api/chat", {"session": str(uuid.uuid4()), "text": CRACKED}, v)[2])
        assert not any(e["type"] == "key_error" for e in evs)
        assert any(e["type"] == "message" and e["verdict"] == "busy" for e in evs), evs
    finally:
        srv.shutdown()


def test_cors_preflight_for_key_check_only():
    fresh()
    srv, base = http_server()
    try:
        landing = {"origin": "http://localhost:8630", "access-control-request-method": "POST",
                   "access-control-request-headers": "content-type"}
        st, h, _ = call(base, "OPTIONS", "/api/key/check", headers=landing)
        h = {k.lower(): v for k, v in h.items()}
        assert st == 204 and h["access-control-allow-origin"] == "http://localhost:8630"
        assert "POST" in h["access-control-allow-methods"] and "content-type" in h["access-control-allow-headers"]
        st, h, body = call(base, "POST", "/api/key/check", {"key": VSR + "test-fake-quota"},
                           {"origin": "http://127.0.0.1:8630"})
        h = {k.lower(): v for k, v in h.items()}
        assert st == 429 and json.loads(body)["code"] == "quota"
        assert h["access-control-allow-origin"] == "http://127.0.0.1:8630"
        # another origin, or another endpoint: no CORS
        st, h, _ = call(base, "OPTIONS", "/api/key/check", headers={**landing, "origin": "https://evil.example"})
        assert "access-control-allow-origin" not in {k.lower() for k in h}
        st, h, _ = call(base, "POST", "/api/key", {"key": GOOD},
                        {"origin": "http://localhost:8630", "x-visitor-id": str(uuid.uuid4())})
        assert "access-control-allow-origin" not in {k.lower() for k in h}
        assert call(base, "OPTIONS", "/api/chat", headers=landing)[0] == 404
    finally:
        srv.shutdown()


def test_check_attempts_are_limited():
    fresh()
    server.KEY_ATTEMPTS = keys.Attempts(limit=10)
    srv, base = http_server()
    try:
        v = {"x-visitor-id": str(uuid.uuid4())}
        codes = [call(base, "POST", "/api/key/check", {"key": "bad"}, v)[0] for _ in range(11)]
        assert codes[:10] == [400] * 10 and codes[10] == 429, codes
        assert json.loads(call(base, "POST", "/api/key", {"key": GOOD}, v)[2])["code"] == "rate_limited"
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------------------------- secrecy

def test_key_never_in_logs_events_or_metrics():
    """A whole run with a fake key (good, then refused mid-turn): the key is in no log line, answer, event or metric.
    Logs are captured raw (without the redaction guard) so the code itself must not print it."""
    fresh()
    key = VSR + "test-fake-secret-7f3a9c"
    out = io.StringIO()
    srv, base = http_server()
    seen = []
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            v = {"x-visitor-id": str(uuid.uuid4())}
            for path in ("/api/key/check", "/api/key"):
                seen.append(call(base, "POST", path, {"key": key}, v)[2])
            sid = str(uuid.uuid4())
            seen.append(call(base, "POST", "/api/chat", {"session": sid, "text": CRACKED}, v)[2])
            seen.append(call(base, "POST", "/api/chat", {"session": sid, "image": "samples/phone/phone_3_clear.jpg",
                                                         "has_turns": True}, v)[2])
            stub(key_error=[401, "invalid_api_key"])
            seen.append(call(base, "POST", "/api/chat", {"session": sid, "text": "why?", "has_turns": True}, v)[2])
            for p in ("/api/key", "/api/config", "/api/metrics", "/api/health", f"/api/stats?session={sid}"):
                seen.append(call(base, "GET", p, headers=v)[2])
            for t in server.STATE.turns:
                seen.append(call(base, "GET", f"/api/turn/{t}")[2])
            # wrong keys are not echoed either
            for bad in ("sk-pt-test-fake-secret-1", key + " x", VSR + "test-fake-invalid-secret"):
                seen.append(call(base, "POST", "/api/key/check", {"key": bad}, v)[2])
    finally:
        srv.shutdown()
    logs = out.getvalue()
    assert "key set ok" in logs and "key_failed" in logs, logs[-500:]
    blob = logs + "\n".join(seen) + json.dumps(server.metrics())
    for secret in (key, "test-fake-secret", "sk-pt-test", "fake-invalid-secret"):
        assert secret not in blob, f"{secret!r} leaked"
    assert any('"key_error"' in s for s in seen)


def test_log_redaction_guard():
    assert keys.redact("x " + VSR + "AbC_12-3 y sk-pt-zz9 z") == "x sk-*** y sk-*** z"
    buf = io.StringIO()
    r = keys.Redacting(buf)
    print("leaked " + VSR + "test-fake-should-not-show", file=r)
    assert buf.getvalue() == "leaked sk-***\n"


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
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
