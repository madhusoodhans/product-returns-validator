"""Capacity shared by every chat: model-call slots per API kind, the 429/503 retry, and per-visitor rate limits.

A turn that cannot get model capacity raises Busy; the server answers it with BUSY_TEXT instead of an error.
"""
import contextvars
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from email.utils import parsedate_to_datetime

import requests

SLOTS = min(12, int(os.environ.get("MODEL_SLOTS", "12")))  # calls at once per API kind (a hosted key allows 16 chats)
QUEUE = int(os.environ.get("MODEL_QUEUE", "100"))     # calls waiting for a slot per kind; more -> Busy at once
WAIT_S = float(os.environ.get("MODEL_WAIT_S", "15"))  # longest wait for a slot
RETRY_CAP_S = 5.0     # a longer Retry-After is not waited out inside a turn: the second 429 makes the turn Busy
THROTTLED = (429, 503)
BUSY_TEXT = "The service is busy, please try again."


class Busy(Exception):
    """No model capacity for this turn: a slot wait over WAIT_S, a full queue, or 429/503 twice in a row (`code`: the
    last status then)."""

    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


class Gate:
    """At most `slots` calls at once; callers wait up to `wait` seconds, at most `queue` of them."""

    def __init__(self, name, slots=SLOTS, queue=QUEUE, wait=WAIT_S):
        self.name, self.queue, self.wait = name, queue, wait
        self.sem = threading.BoundedSemaphore(slots)
        self.lock = threading.Lock()
        self.waiting = 0

    def __enter__(self):
        with self.lock:
            if self.waiting >= self.queue:
                raise Busy(f"{self.name}: {self.waiting} calls already waiting")
            self.waiting += 1
        try:
            ok = self.sem.acquire(timeout=self.wait)
        finally:
            with self.lock:
                self.waiting -= 1
        if not ok:
            raise Busy(f"{self.name}: waited over {self.wait:.0f} s for a free slot")
        return self

    def __exit__(self, *exc):
        self.sem.release()


GATES = {"d3": Gate("D3 (/v1/systemone)"), "guard": Gate("Vela guardrail (/v1/systemone)"),
         "chat": Gate("chat completions")}

# counters since start, and the 429/503s of the running turn (a ContextVar: photo.py's threads copy it)
COUNTS = {"throttled": 0}
_count_lock = threading.Lock()
_turn = contextvars.ContextVar("turn_counts", default=None)


def start_turn():
    """Count this thread's (and its copied contexts') throttled answers from now on; returns the counter dict."""
    d = {"throttled": 0}
    _turn.set(d)
    return d


def _note_throttled():
    with _count_lock:
        COUNTS["throttled"] += 1
        d = _turn.get()
        if d is not None:
            d["throttled"] += 1


def retry_after(r, cap=RETRY_CAP_S):
    """Seconds to wait from a Retry-After header (seconds or HTTP date), default 1, at most `cap`."""
    v = r.headers.get("retry-after")
    try:
        s = float(v)
    except (TypeError, ValueError):
        try:
            s = parsedate_to_datetime(v).timestamp() - time.time()
        except (TypeError, ValueError):
            s = 1.0
    return min(cap, max(0.0, s))


COOLDOWN_CAP_S = 60.0
_cool = {}   # kind -> time until which the API asked us to wait (Retry-After of a 429)


@contextmanager
def post(kind, url, **kw):
    """requests.post through the gate of `kind`, holding the slot until the response is consumed. A 429/503 is retried
    once after Retry-After when that is at most RETRY_CAP_S; a second one, or a longer wait, raises Busy. While a 429's
    Retry-After runs, other calls of the same kind raise Busy at once without a request."""
    if _cool.get(kind, 0) > time.time():
        raise Busy(f"{GATES[kind].name}: rate limited, retry after {_cool[kind] - time.time():.0f} s", 429)
    for attempt in (0, 1):
        with GATES[kind], requests.post(url, **kw) as r:
            if r.status_code not in THROTTLED:
                yield r
                return
            code, wait = r.status_code, retry_after(r, COOLDOWN_CAP_S)
        _note_throttled()
        if code == 429 and r.headers.get("retry-after"):
            _cool[kind] = max(_cool.get(kind, 0), time.time() + wait)
        if attempt == 0 and wait <= RETRY_CAP_S:
            time.sleep(wait)
            continue
        break
    raise Busy(f"{GATES[kind].name} answered {code}" + (" twice" if attempt else f", retry after {wait:.0f} s"), code)


class Window:
    """At most `limit` hits per `per` seconds for each key (sliding window). Not thread-safe: see Limits."""

    def __init__(self, limit, per=60.0):
        self.limit, self.per, self.hits = limit, per, {}

    def full(self, key, now):
        q = self.hits.get(key)
        while q and now - q[0] >= self.per:
            q.popleft()
        return bool(q) and len(q) >= self.limit

    def add(self, key, now):
        self.hits.setdefault(key, deque()).append(now)

    def sweep(self, now):
        for k in [k for k, q in self.hits.items() if not q or now - q[-1] >= self.per]:
            del self.hits[k]


class Limits:
    """Per chat and per visitor IP message limits; new chats per IP."""

    def __init__(self, chat_msgs=10, ip_msgs=30, ip_chats=5, per=60.0):
        self.lock = threading.Lock()
        self.chat, self.ip, self.new = Window(chat_msgs, per), Window(ip_msgs, per), Window(ip_chats, per)

    def admit(self, ip, sid, new_chat, now=None):
        """Records the message and returns None, or returns which limit it hit (and records nothing). ip None: no
        per-IP limits (the presenter's own machine)."""
        now = time.time() if now is None else now
        with self.lock:
            if ip is not None and self.ip.full(ip, now):
                return "ip_messages"
            if self.chat.full(sid, now):
                return "chat_messages"
            if ip is not None and new_chat and self.new.full(ip, now):
                return "ip_chats"
            self.chat.add(sid, now)
            if ip is not None:
                self.ip.add(ip, now)
                if new_chat:
                    self.new.add(ip, now)
            return None

    def sweep(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            for w in (self.chat, self.ip, self.new):
                w.sweep(now)
