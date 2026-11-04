"""Which models are up: a tracker per model, fed by a background probe and by every real call (a circuit breaker).

Models: "d3" (/v1/systemone), "preview" (the Router's routing preview), "qwen9b" and "qwen27b" (chat completions).
- Every real call goes through `call(name)`: a connection error, a timeout, a 5xx or an unreadable answer is a
  failure; FAILS failures in a row mark the model down for BREAK_S seconds, and calls to it are then refused at once
  (Unavailable, no request sent). One success marks it up again. 429s and full slots (limits.Busy) are capacity, not
  failures.
- `start_probes()` checks every model every HEALTH_INTERVAL seconds with a tiny request that works against any
  OpenAI-compatible / System One endpoint (a 1-token chat completion, a one-question D3 call, a routing preview). A
  model that is down is probed again when its breaker ends. Probes do not take model-call slots.
- The flow asks `available(name)` before it relies on a model; the reasons the customer can see are plain words
  ("unavailable", "timed out"); exception text only goes to the server log.
"""
import os
import threading
import time
from contextlib import contextmanager

import requests

from limits import Busy

NAMES = ("d3", "preview", "qwen9b", "qwen27b")
LABEL = {"d3": "D3", "preview": "Routing preview", "qwen9b": "Qwen 9B", "qwen27b": "Qwen 27B"}
FAILS = 3
BREAK_S = float(os.environ.get("BREAKER_S", "30"))
HEALTH_INTERVAL = float(os.environ.get("HEALTH_INTERVAL", "30"))
# per call, seconds (a streamed reply: the whole stream)
TIMEOUT = {"d3": float(os.environ.get("D3_TIMEOUT_S", "10")),
           "preview": float(os.environ.get("PREVIEW_TIMEOUT_S", "5")),
           "qwen9b": float(os.environ.get("QWEN9B_TIMEOUT_S", "30")),
           "qwen27b": float(os.environ.get("QWEN27B_TIMEOUT_S", "60"))}
UNAVAILABLE_TEXT = "The assistant is unavailable right now. Please try again in a minute."
OTHER_LLM = {"qwen9b": "qwen27b", "qwen27b": "qwen9b"}


def timeout(name):
    """(connect, read) for requests."""
    t = TIMEOUT[name]
    return min(3.0, t), t


class Unavailable(Exception):
    """A model call that failed or was not sent because the model is down. `reason`: "unavailable" or "timed out"."""

    def __init__(self, name, reason="unavailable"):
        super().__init__(f"{LABEL.get(name, name)} {reason}")
        self.name, self.reason = name, reason

    @property
    def text(self):
        """One plain line for a flow step."""
        return f"{LABEL.get(self.name, self.name)} {self.reason}"


def log(msg):
    print(f"models: {msg}", flush=True)


class Tracker:
    def __init__(self, fails=FAILS, break_s=BREAK_S):
        self.lock = threading.Lock()
        self.fails, self.break_s = fails, break_s
        # up: None until the first probe or call; down_until: breaker open until then
        self.state = {n: {"up": None, "fails": 0, "down_until": 0.0, "reason": None, "checked": None,
                          "next_probe": 0.0} for n in NAMES}

    def available(self, name, now=None):
        """False while the model's breaker is open. Unknown (not checked yet) counts as available."""
        now = time.time() if now is None else now
        with self.lock:
            return self.state[name]["down_until"] <= now

    def success(self, name, now=None):
        now = time.time() if now is None else now
        with self.lock:
            s = self.state[name]
            was = s["up"]
            s.update(up=True, fails=0, down_until=0.0, reason=None, checked=now)
        if was is False:
            log(f"{name} up")

    def failure(self, name, reason, detail="", now=None, probe=False):
        """A failed call (or probe: down at once). Returns True when this opened the breaker."""
        now = time.time() if now is None else now
        with self.lock:
            s = self.state[name]
            s["fails"] = self.fails if probe else s["fails"] + 1
            s["checked"] = now
            opened = s["fails"] >= self.fails
            if opened:
                was_down = s["down_until"] > now
                s.update(up=False, down_until=now + self.break_s, reason=reason, next_probe=now + self.break_s)
        log(f"{name} {'probe' if probe else 'call'} failed ({reason}; {s['fails']} in a row): {detail[:300]}")
        if opened and not was_down:
            log(f"{name} down for {self.break_s:.0f} s")
        return opened

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            out = {}
            for n, s in self.state.items():
                down = s["down_until"] > now
                out[n] = {"state": "down" if down else "unknown" if s["up"] is None else "up",
                          "reason": s["reason"] if down else None,
                          "retry_in_s": round(s["down_until"] - now) if down else None,
                          "checked_s_ago": None if s["checked"] is None else round(now - s["checked"])}
            return out

    def reset(self):
        with self.lock:
            for s in self.state.values():
                s.update(up=None, fails=0, down_until=0.0, reason=None, checked=None, next_probe=0.0)


TRACKER = Tracker()
available = TRACKER.available


def classify(e):
    """(reason, counts as a failure) for an exception from a model call."""
    if isinstance(e, requests.Timeout):
        return "timed out", True
    if isinstance(e, requests.ConnectionError):
        # a read timeout in the middle of a stream surfaces as a ConnectionError
        return ("timed out" if "timed out" in str(e).lower() else "unavailable"), True
    if isinstance(e, requests.HTTPError):
        code = e.response.status_code if e.response is not None else 500
        return "unavailable", code >= 500
    if isinstance(e, (ValueError, KeyError, IndexError, TypeError, AttributeError)):   # not the answer we expect
        return "unavailable", True
    return "unavailable", False


@contextmanager
def call(name):
    """Wrap one real model call: refused at once while the model is down; failures and successes feed the breaker;
    every failure leaves as Unavailable (Busy passes through: capacity is not a failure, except a 503 twice)."""
    if not TRACKER.available(name):
        raise Unavailable(name, "unavailable")
    try:
        yield
    except Unavailable as e:   # raised by the call itself (a stream over its time limit)
        TRACKER.failure(name, e.reason, str(e))
        raise
    except Busy as e:
        if getattr(e, "code", None) == 503:   # 503 twice: the Router has no healthy backend, or it is overloaded
            TRACKER.failure(name, "unavailable", str(e))
        raise
    except Exception as e:  # noqa: BLE001
        reason, counts = classify(e)
        if counts:
            TRACKER.failure(name, reason, f"{type(e).__name__}: {e}")
        else:
            log(f"{name} call failed (not counted): {type(e).__name__}: {str(e)[:300]}")
        raise Unavailable(name, reason) from e
    TRACKER.success(name)


def llm_order(prefer):
    """The LLMs to try, preferred first, only those not marked down."""
    return [m for m in (prefer, OTHER_LLM[prefer]) if TRACKER.available(m)]


# ---------------------------------------------------------------------------------------------- probes

def _headers(admin=False):
    import models
    return models.headers(admin)


def probe(name):
    """One cheap request, outside the model-call slots. Returns (ok, reason, detail)."""
    import models
    import turn_checks
    try:
        if name == "d3":
            body = {"model": models.D3_MODEL, "state": "Health check.",
                    "questions": {"q": {"type": "noul", "instructions": "This is a test."}}}
            r = requests.post(f"{models.ROUTER_URL}/v1/systemone", json=body, headers=_headers(), timeout=timeout(name))
        elif name == "preview":
            r = requests.post(turn_checks.PREVIEW, json={"model": "returns/auto", "messages": [
                {"role": "user", "content": "hello"}]}, headers=_headers(admin=True), timeout=timeout(name))
        else:
            r = requests.post(f"{models.ROUTER_URL}/v1/chat/completions", headers=_headers(), timeout=timeout(name),
                              json={"model": name, "messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 1,
                                    "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}})
        if r.status_code == 429:
            return True, None, "429: up but throttled"
        r.raise_for_status()
        d = r.json()
        if name == "d3":
            d["answers"]["q"]["noul"]
        elif name == "preview":
            dec = (d.get("decision_result") or {}).get("decision_name")
            if not dec or not str(dec).startswith("returns"):
                return False, "not configured", f"returns/auto is not configured (decision {dec!r}); see deploy/router.yaml"
        else:
            d["choices"][0]
        return True, None, ""
    except Exception as e:  # noqa: BLE001
        reason, _counts = classify(e)
        return False, reason, f"{type(e).__name__}: {e}"


def probe_all(names=NAMES):
    """Probe now, one model at a time (keeps the load on a shared Router low)."""
    for n in names:
        ok, reason, detail = probe(n)
        if ok:
            TRACKER.success(n)
        else:
            TRACKER.failure(n, reason, detail, probe=True)
        with TRACKER.lock:
            s = TRACKER.state[n]
            if ok:
                s["next_probe"] = time.time() + HEALTH_INTERVAL


def _loop():
    while True:
        now = time.time()
        with TRACKER.lock:
            due = [n for n, s in TRACKER.state.items() if s["next_probe"] <= now]
        if due:
            probe_all(due)
        time.sleep(1)


_started = threading.Lock()
_running = []


def start_probes():
    """Start the background probe thread once (first round at once)."""
    with _started:
        if not _running:
            t = threading.Thread(target=_loop, daemon=True, name="model-probes")
            t.start()
            _running.append(t)


def status(snap=None):
    """What the top bar says: (key, plain words). Keys: ok, checking, preview_down, qwen9b_down, qwen27b_down,
    standard, unavailable."""
    snap = snap or TRACKER.snapshot()
    down = {n for n, s in snap.items() if s["state"] == "down"}
    if "d3" in down:
        return "unavailable", "Assistant unavailable"
    if {"qwen9b", "qwen27b"} <= down:
        return "standard", "Standard replies: language models unavailable"
    if "qwen9b" in down:
        return "qwen9b_down", "Using Qwen 27B for all replies: Qwen 9B unavailable"
    if "qwen27b" in down:
        return "qwen27b_down", "Using Qwen 9B for all replies: Qwen 27B unavailable"
    if "preview" in down:
        return "preview_down", "Router preview unavailable: D3 picks the model"
    if any(s["state"] == "unknown" for s in snap.values()):
        return "checking", "Checking systems"
    return "ok", "All systems working"


def health():
    """GET /health and /api/health: the tracker's view (no probe of its own). ok: every model up."""
    snap = TRACKER.snapshot()
    key, words = status(snap)
    return {"ok": all(s["state"] == "up" for s in snap.values()), "status": key, "message": words,
            **{n: s["state"] == "up" for n, s in snap.items()}, "models": snap,
            "probe_interval_s": HEALTH_INTERVAL}
