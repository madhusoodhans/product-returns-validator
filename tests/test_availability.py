"""Unit tests for model availability: pre-flight, the circuit breaker, LLM fallbacks, standard replies, the routing
preview fallback, mid-turn failures and that no raw error text reaches the client. No real models: bench/stub_models.py
runs in-process with per-model switches (down / slow / 5xx).

Run: .venv/bin/python tests/test_availability.py
"""
import json
import re
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "bench"))
import availability  # noqa: E402
import models  # noqa: E402
import reply  # noqa: E402
import server  # noqa: E402
import stub_models  # noqa: E402
import turn_checks  # noqa: E402
from availability import TRACKER, UNAVAILABLE_TEXT, Unavailable  # noqa: E402
from flow import Session  # noqa: E402
from limits import Limits  # noqa: E402

STUB = stub_models.Server(("127.0.0.1", 0), stub_models.Handler)
threading.Thread(target=STUB.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{STUB.server_port}"
models.ROUTER_URL = URL
turn_checks.PREVIEW = URL + "/api/v1/routing/preview"
TIMEOUTS = dict(availability.TIMEOUT)
NO_APPROVAL = {"says": "other"}   # the stub's D3 would otherwise read every reply as an approval
CRACKED = "Order 4411, the phone screen was cracked when I opened the box."
PHOTO = "samples/phone/phone_3_clear.jpg"
GOOD_PHOTO = {"real_photo": 0.95, "damage_visible": 0.95}


def stub(**cfg):
    req = urllib.request.Request(URL + "/stub/config", json.dumps(cfg).encode(), {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def calls_by():
    with urllib.request.urlopen(URL + "/stub/config", timeout=5) as r:
        return json.loads(r.read())["calls_by"]


def fresh(**cfg):
    """Every switch back to default (plus cfg), counts cleared, every model unknown, normal timeouts."""
    stub(reset=True, scale=0.05, **cfg)
    TRACKER.reset()
    TRACKER.break_s = availability.BREAK_S
    availability.TIMEOUT.update(TIMEOUTS)


def run(s, text=None, image=None):
    """One turn: (steps by node, message, all events)."""
    evs = list(s.turn(text, image))
    steps = {}
    for e in evs:
        if e["type"] == "step":
            steps[e["node"]] = e
    return steps, next(e for e in evs if e["type"] == "message"), evs


def mark_down(*names):
    for n in names:
        TRACKER.failure(n, "unavailable", "test", probe=True)


# ---------------------------------------------------------------------------------------------- tracker

def test_breaker_opens_after_three_and_closes_on_success():
    fresh(fail={"qwen9b": 500})
    for i in range(3):
        try:
            models.chat("qwen9b", [{"role": "user", "content": "hi"}], max_tokens=2)
            raise AssertionError("should fail")
        except Unavailable as e:
            assert e.reason == "unavailable" and str(e) == "Qwen 9B unavailable"
        assert TRACKER.available("qwen9b") == (i < 2)
    n = calls_by().get("qwen9b")
    try:
        models.chat("qwen9b", [{"role": "user", "content": "hi"}], max_tokens=2)
        raise AssertionError("should be refused")
    except Unavailable:
        pass
    assert calls_by().get("qwen9b") == n, "a down model must be skipped without a request"
    # the breaker ends, the next call goes out; one success marks it up
    TRACKER.break_s = 0.2
    mark_down("qwen9b")
    stub(fail={})
    time.sleep(0.25)
    models.chat("qwen9b", [{"role": "user", "content": "hi"}], max_tokens=2)
    assert TRACKER.snapshot()["qwen9b"]["state"] == "up" and TRACKER.state["qwen9b"]["fails"] == 0


def test_success_resets_the_count():
    fresh()
    for _ in range(2):
        TRACKER.failure("d3", "unavailable", "x")
    TRACKER.success("d3")
    for _ in range(2):
        TRACKER.failure("d3", "unavailable", "x")
    assert TRACKER.available("d3")


def test_429_is_not_a_failure():
    fresh(p429=1.0, retry_after=0)
    for _ in range(4):
        try:
            models.d3("x", {"q": {"type": "noul", "instructions": "x"}})
        except Exception as e:  # noqa: BLE001
            assert type(e).__name__ == "Busy"
    assert TRACKER.available("d3") and TRACKER.state["d3"]["fails"] == 0


def test_timeout_and_probes():
    fresh(slow={"d3": 600})
    availability.TIMEOUT["d3"] = 0.2
    try:
        models.d3("x", {"q": {"type": "noul", "instructions": "x"}})
        raise AssertionError("should time out")
    except Unavailable as e:
        assert e.reason == "timed out" and e.text == "D3 timed out"
    fresh(down=["qwen27b"], fail={"preview": 502})
    availability.probe_all()
    snap = TRACKER.snapshot()
    assert {n: s["state"] for n, s in snap.items()} == {"d3": "up", "preview": "down", "qwen9b": "up", "qwen27b": "down"}
    assert availability.status()[1] == "Using Qwen 9B for all replies: Qwen 27B unavailable"
    stub(down=[], fail={})
    availability.probe_all()
    assert availability.status() == ("ok", "All systems working")


def test_status_words():
    fresh()
    assert availability.status()[0] == "checking"
    for down, words in [((), "All systems working"),
                        (("qwen9b",), "Using Qwen 27B for all replies: Qwen 9B unavailable"),
                        (("qwen9b", "qwen27b"), "Standard replies: language models unavailable"),
                        (("d3", "qwen9b"), "Assistant unavailable")]:
        TRACKER.reset()
        for n in availability.NAMES:
            TRACKER.success(n)
        mark_down(*down)
        assert availability.status()[1] == words, (down, availability.status())


# ---------------------------------------------------------------------------------------------- flow

def test_preflight_refusal_without_model_calls():
    fresh()
    mark_down("d3")
    s = Session(str(uuid.uuid4()))
    steps, msg, evs = run(s, CRACKED)
    assert msg["verdict"] == "unavailable" and msg["text"] == UNAVAILABLE_TEXT
    assert steps["guardrail"]["status"] == "failed" and all(steps[n]["status"] == "skipped" for n in server.NODES[1:])
    assert calls_by() == {}, "pre-flight must not call any model"
    assert s.turns == 0 and len(s.history) == 1 and not s.claims
    assert not any(e["type"] == "done" for e in evs)


def test_9b_down_uses_27b():
    fresh(down=["qwen9b"], answers=NO_APPROVAL)
    s = Session(str(uuid.uuid4()))
    # 9B not known to be down yet: the failed call falls through to 27B in the same turn
    steps, msg, _ = run(s, CRACKED)
    assert msg["verdict"] == "need_photo", msg
    assert steps["claim"]["model"] == "qwen27b" and "Qwen 9B unavailable" in steps["claim"]["summary"]
    assert steps["reply"]["model"] == "qwen27b" and steps["reply"]["detail"]["fallback_from"] == "qwen9b"
    # once marked down (probe): the router step says so and 9B is not called at all
    availability.probe_all()
    n9 = calls_by().get("qwen9b")
    steps, msg, _ = run(Session(str(uuid.uuid4())), CRACKED)
    assert "Qwen 9B unavailable, Qwen 27B instead" in steps["router"]["summary"]
    assert steps["router"]["detail"]["unavailable"] == ["qwen9b"]
    assert steps["reply"]["model"] == "qwen27b" and calls_by().get("qwen9b") == n9


def test_27b_down_uses_9b():
    fresh(fail={"qwen27b": 500}, pick="qwen27b", answers=NO_APPROVAL)
    steps, msg, _ = run(Session(str(uuid.uuid4())), CRACKED)
    assert steps["router"]["detail"]["chosen"] == "qwen27b"
    assert steps["reply"]["model"] == "qwen9b" and "Qwen 27B unavailable, written by Qwen 9B" in steps["reply"]["summary"]
    assert msg["verdict"] == "need_photo"
    mark_down("qwen27b")
    steps, _m, _e = run(Session(str(uuid.uuid4())), CRACKED)
    assert "Qwen 27B unavailable, Qwen 9B instead" in steps["router"]["summary"] and steps["reply"]["model"] == "qwen9b"


def test_llm_timeout_is_not_retried_in_the_turn():
    fresh(slow={"qwen9b": 800}, answers=NO_APPROVAL)
    availability.TIMEOUT["qwen9b"] = 0.3
    t = time.perf_counter()
    steps, msg, _ = run(Session(str(uuid.uuid4())), CRACKED)
    # claim extraction timed out on 9B: no second wait for it; 27B words the reply
    assert "Qwen 9B timed out" in steps["claim"]["summary"] and steps["reply"]["model"] == "qwen27b"
    assert time.perf_counter() - t < 3


# every verdict the flow produces, with both LLMs down: (turns before, the turn, D3 answers, verdict, words in reply)
TEMPLATE_CASES = [
    ([], ("my thing is broken", None), {"order": "unclear"}, "need_order", ["Which order"]),
    ([], ("Order 4411", None), {}, "need_problem", ["What is wrong", "4411"]),
    ([], ("Order 4411, it is broken", None), {"issue": "unclear", "complaint": 0.9}, "clarify", ["what exactly"]),
    ([], ("Order 4411, it is broken", None), {"when": "not_said", "complaint": 0.9}, "clarify", ["already there"]),
    ([], (CRACKED, None), {"complaint": 0.9}, "need_photo", ["photo", "screen switched off"]),
    ([], ("Order 4411, I dropped it and the screen cracked", None), {"when": "after", "complaint": 0.9},
     "not_eligible", ["not covered", "after delivery", "does not cover"]),
    ([], ("Order 4411, it makes a buzzing sound", None), {"issue": "not_visual", "complaint": 0.9}, "human",
     ["colleague", "one working day"]),
    ([], ("Order 4411, the box was dented but the phone is fine", None), {"issue": "no_damage", "complaint": 0.9},
     "not_eligible", ["Returns page"]),
    ([], ("Order 5121, the laptop screen is cracked out of the box", None), {"complaint": 0.9}, "human",
     ["review limit"]),
    ([(CRACKED, None)], (None, PHOTO), {"complaint": 0.9} | GOOD_PHOTO, "accept", ["approved", "label"]),
    ([(CRACKED, None)], (None, PHOTO), {"complaint": 0.9, "real_photo": 0.95, "blurry": 0.9}, "retake",
     ["blurry", "hold the phone still"]),
    ([(CRACKED, None)], (None, PHOTO), {"complaint": 0.9, "real_photo": 0.95, "kind": "mug"}, "mismatch",
     ["does not show"]),
    ([(CRACKED, None)], (None, PHOTO), {"complaint": 0.9, "real_photo": 0.95, "damage_visible": 0.05}, "no_damage",
     ["closer photo"]),
    ([(CRACKED, None)], ("how long does the refund take?", None), {"complaint": 0.9, "intent": "refund_info"}, "info",
     ["5 to 7 working days", "photo of the damage"]),
    ([(CRACKED, None)], ("I want to talk to a person", None), {"complaint": 0.9, "intent": "human"}, "human",
     ["colleague"]),
]


def test_standard_replies_for_every_verdict_when_both_llms_are_down():
    for before, (text, image), answers, want, words in TEMPLATE_CASES:
        fresh(answers=NO_APPROVAL | answers)
        mark_down("qwen9b", "qwen27b")
        s = Session(str(uuid.uuid4()))
        for t, i in before:
            run(s, t, i)
        steps, msg, _ = run(s, text, image)
        assert msg["verdict"] == want, (text, answers, msg["verdict"], want)
        assert steps["reply"]["summary"] == "Standard reply: language models unavailable", steps["reply"]
        assert steps["reply"]["detail"]["standard"] and steps["reply"]["model"] is None
        assert all(w.lower() in msg["text"].lower() for w in words), (want, msg["text"])
        assert "{" not in msg["text"] and "None" not in msg["text"], msg["text"]
        assert not any(k.startswith("qwen") for k in calls_by()), calls_by()


def test_standard_templates_direct():
    """Verdicts that need longer chats to reach: closed claims, a person after an approval, orders not found."""
    base = {"product": "phone", "order": "4411", "item": "smartphone", "reasons": []}
    for what, words in [("closed_accept", ["already approved"]), ("closed_human", ["already reviewing"]),
                        ("closed_not_eligible", ["already decided", "after delivery"]),
                        ("human_after_accept", ["stays approved"])]:
        t = reply.standard("closed", base | {"what": what, "previous": "The damage happened after delivery",
                                             "photo_sent": True})
        assert all(w in t for w in words) and ("No more photos" in t) == what.startswith("closed_"), t
    t = reply.standard("need_order", {"what": "not_found", "not_found": "9050", "options": "Phone (order 4411)"})
    assert "9050" in t and "Phone (order 4411)" in t
    t = reply.standard("need_order", {"options": "A (order 1) or B (order 2)"})
    assert t.startswith("Which order do you mean: A (order 1) or B (order 2)?")
    t = reply.standard("accept", base | {"std_notes": ["Next, I will look at your other order 6230 (mugs)."]})
    assert t.endswith("order 6230 (mugs).")
    t = reply.standard("not_eligible", base | {"reason": "Delivered 40 days ago, return window 10 days",
                                               "policy_ref": "window", "policy_line": "Returns within 10 days of delivery"})
    assert "delivered 40 days ago" in t and "Returns within 10 days of delivery." in t


def test_preview_down_falls_back_to_d3():
    fresh(down=["preview"], answers=NO_APPROVAL)
    steps, msg, _ = run(Session(str(uuid.uuid4())), CRACKED)
    assert msg["verdict"] == "need_photo"
    assert steps["guardrail"]["summary"].endswith("Router unavailable") and steps["guardrail"]["model"] == "D3 mini"
    assert "D3 picked" in steps["router"]["summary"] and steps["router"]["model"] == "vllm-sr/d3-mini"
    for _ in range(2):
        run(Session(str(uuid.uuid4())), CRACKED)
    assert not TRACKER.available("preview")
    n = calls_by().get("preview")
    run(Session(str(uuid.uuid4())), CRACKED)
    assert calls_by().get("preview") == n


def test_mid_turn_d3_failure_leaves_the_chat_unchanged():
    fresh(fail_if={"Return policy covers": 500}, answers=NO_APPROVAL)
    s = Session(str(uuid.uuid4()))
    steps, msg, evs = run(s, CRACKED)
    assert steps["eligibility"]["status"] == "failed" and steps["eligibility"]["summary"] == "D3 unavailable"
    assert all(steps[n]["status"] == "skipped" for n in ("image_check", "decision", "reply"))
    assert msg["verdict"] == "unavailable" and msg["text"] == UNAVAILABLE_TEXT
    assert not s.claims and s.claim is None and len(s.history) == 1 and s.turns == 0
    stub(fail_if={})
    steps, msg, _ = run(s, CRACKED)   # sent again: works as a first message
    assert msg["verdict"] == "need_photo" and s.turns == 1
    # D3 too slow at the first step
    fresh(slow={"d3": 600})
    availability.TIMEOUT["d3"] = 0.2
    steps, msg, _ = run(Session(str(uuid.uuid4())), CRACKED)
    assert steps["guardrail"]["status"] == "failed" and steps["guardrail"]["summary"] == "D3 timed out"
    assert msg["verdict"] == "unavailable"


def test_reply_check_down_gives_standard_reply():
    fresh(fail_if={"is about to send this reply": 500}, answers=NO_APPROVAL)
    steps, msg, _ = run(Session(str(uuid.uuid4())), CRACKED)
    assert msg["verdict"] == "need_photo"
    assert steps["reply"]["summary"] == "Standard reply: the D3 reply check is unavailable"
    assert msg["text"].startswith("Please send one clear photo")


# ---------------------------------------------------------------------------------------------- HTTP: no raw errors

RAW = re.compile(r"Error|Exception|Traceback|HTTPConnectionPool|upstream|stub_failure|Max retries|requests\.|"
                 r"RemoteDisconnected|Read timed out|status code|\bsecret\b")


def http_server():
    srv = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def post(base, body):
    req = urllib.request.Request(base + "/api/chat", json.dumps(body).encode(), {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        return e.read().decode()


def test_no_raw_error_text_reaches_the_client():
    server.STATE, server.LIMITS = server.State(), Limits()
    srv, base = http_server()
    seen = []
    try:
        modes = [dict(down=["qwen9b"]), dict(fail={"qwen9b": 500, "qwen27b": 502}), dict(down=["preview"]),
                 dict(fail_if={"Return policy covers": 500}), dict(slow={"d3": 600}), dict(fail={"d3": 500}),
                 dict(down=["d3", "qwen9b", "qwen27b", "preview"]), dict(p503=1.0, retry_after=0)]
        for mode in modes:
            fresh(answers=NO_APPROVAL, **mode)
            availability.TIMEOUT["d3"] = 0.3
            sid = str(uuid.uuid4())
            for i, (text, image) in enumerate([(CRACKED, None), (None, PHOTO), ("why?", None)] * 2):
                raw = post(base, {"session": sid, "text": text, "image": image, "has_turns": i > 0})
                seen.append(raw)
            seen += [get(base, p) for p in ("/health", "/api/health", "/api/config", "/api/metrics")]
        # a bug in the flow: the client gets a plain sentence, the detail goes to the log
        real = server.Session.turn

        def broken(self, *a, **k):
            yield {"type": "step", "node": "guardrail", "status": "running", "summary": None, "model": None,
                   "ms": None, "detail": None}
            raise RuntimeError("secret detail from a bug")
        server.Session.turn = broken
        try:
            raw = post(base, {"session": str(uuid.uuid4()), "text": "hi"})
            assert '"verdict": "error"' in raw and server.ERROR_TEXT in raw
            seen.append(raw)
        finally:
            server.Session.turn = real
    finally:
        srv.shutdown()
        availability.TIMEOUT.update(TIMEOUTS)
    for raw in seen:
        for line in raw.splitlines():
            m = RAW.search(line)
            assert not m, f"raw error text {m.group(0)!r} in: {line[:300]}"
    joined = "\n".join(seen)
    assert '"verdict": "unavailable"' in joined and "Standard reply" in joined


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
