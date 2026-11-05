"""Which models are up: a tracker per model, fed by a background probe and by every real call (a circuit breaker).

Models: "d3" (D3 on /v1/systemone), "guard" (the Vela guardrail on /v1/systemone) and "llm" (vllm-sr/auto on
/v1/chat/completions), all on the hosted vllm-sr API.
- Every real call goes through `call(name)`: a connection error, a timeout, a 5xx or an unreadable answer is a
  failure; FAILS failures in a row mark the model down for BREAK_S seconds, and calls to it are then refused at once
  (Unavailable, no request sent). One success marks it up again. 429s and full slots (limits.Busy) are capacity, not
  failures.
- `start_probes()` checks the API every HEALTH_INTERVAL seconds with one GET /v1/models (no model call, no quota per
  visitor): the API answers and lists the configured models. A model that is down is probed again when its breaker
  ends. Probes do not take model-call slots; real calls keep the tracker current in between.
- The flow asks `available(name)` before it relies on a model; the reasons the customer can see are plain words
  ("unavailable", "timed out"); exception text only goes to the server log.
"""
import os
import threading
import time
from contextlib import contextmanager

import requests

from limits import Busy

NAMES = ("d3", "guard", "llm")
LABEL = {"d3": "D3", "guard": "Vela guardrail", "llm": "Language model"}
FAILS = 3
BREAK_S = float(os.environ.get("BREAKER_S", "30"))
HEALTH_INTERVAL = max(10.0, float(os.environ.get("HEALTH_INTERVAL", "60")))
# per call, seconds (a streamed reply: the whole stream); the hosted API stops a request at 35 s
TIMEOUT = {"d3": min(30.0, float(os.environ.get("D3_TIMEOUT_S", "15"))),
           "guard": min(30.0, float(os.environ.get("GUARD_TIMEOUT_S", "5"))),
           "llm": min(34.0, float(os.environ.get("LLM_TIMEOUT_S", "30"))),
           "probe": 5.0}
UNAVAILABLE_TEXT = "The assistant is unavailable right now. Please try again in a minute."


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


# ---------------------------------------------------------------------------------------------- probes

def model_ids():
    import models
    return {"d3": models.D3_MODEL, "guard": models.GUARD_MODEL, "llm": models.LLM_MODEL}


def probe_models():
    """One GET /v1/models (no model call, no quota): {name: (ok, reason, detail)} for every model. A model the API
    does not list is "not offered" (a wrong model name in the environment, or a key without access)."""
    import models
    try:
        r = requests.get(f"{models.API_BASE}/v1/models", headers=models.headers(), timeout=timeout("probe"))
        if r.status_code == 429:
            return {n: (True, None, "429: up but throttled") for n in NAMES}
        if r.status_code in (401, 403):
            return {n: (False, "unavailable", f"API key refused ({r.status_code})") for n in NAMES}
        r.raise_for_status()
        listed = {m.get("id") for m in r.json().get("data") or []}
    except Exception as e:  # noqa: BLE001
        reason, _counts = classify(e)
        return {n: (False, reason, f"{type(e).__name__}") for n in NAMES}
    return {n: (True, None, "") if mid in listed else (False, "not offered", f"{mid} is not listed by /v1/models")
            for n, mid in model_ids().items()}


def probe(name):
    """Returns (ok, reason, detail) for one model (one GET /v1/models)."""
    return probe_models()[name]


def probe_all(names=NAMES):
    """Probe now: one GET /v1/models for all models."""
    res = probe_models()
    for n in names:
        ok, reason, detail = res[n]
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
    """What the top bar says: (key, plain words). Keys: ok, checking, guard_down, standard, unavailable."""
    snap = snap or TRACKER.snapshot()
    down = {n for n, s in snap.items() if s["state"] == "down"}
    if "d3" in down:
        return "unavailable", "Assistant unavailable"
    if "llm" in down:
        return "standard", "Standard replies: language model unavailable"
    if "guard" in down:
        return "guard_down", "Vela guardrail unavailable: D3 checks messages alone"
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
