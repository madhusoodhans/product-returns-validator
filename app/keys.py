"""The visitor's own vllm-sr API key: format check, live validation, error codes, per-visitor storage, log redaction.

Each visitor (one browser tab: the X-Visitor-Id header, a UUID the page keeps in sessionStorage) enters a key in the
page. The server checks it (format, then one tiny System One call and one tiny System Two call), keeps it in process
memory for that visitor only, and every model call of that visitor's turns uses it (CURRENT, a ContextVar: the flow's
worker threads copy it). Keys are never written to disk, logs, metrics, events or error messages.

An env key (VLLM_SR_API_KEY) is optional and off by default: when set, visitors without a key of their own use it.

Error codes (docs/API.md "API key"): format, wrong_kind, invalid, disabled, quota, rate_limited, unreachable, error;
no_key when a model call is asked for without a key.
"""
import contextvars
import hashlib
import io
import os
import re
import sys
import threading
import time

import requests

API_BASE = os.environ.get("VLLM_SR_API_BASE", "https://app.vllm-sr.ai").rstrip("/")
CHECK_D3_MODEL = "vllm-sr/d3-flash"
CHECK_LLM_MODEL = "vllm-sr/auto"
CHECK_TIMEOUT = (5.0, 30.0)
KEY_IDLE_S = float(os.environ.get("KEY_IDLE_S", str(4 * 3600)))   # a visitor's key is dropped after this idle time
MAX_VISITORS = 2000
CHECKS_PER_MIN = int(os.environ.get("KEY_CHECKS_PER_MIN", "10"))   # validation attempts per visitor / IP
OK_CACHE_S = 600.0   # a key that passed the live check is not checked again for this long (only its hash is kept)

SECRET = re.compile(r"sk-(?:vsr|pt)-[A-Za-z0-9_\-]+")
SHAPE = re.compile(r"^sk-vsr-[A-Za-z0-9_\-]{8,200}$")
GET_KEY = "https://app.vllm-sr.ai/#key"

MESSAGES = {
    "format": "That does not look like a vllm-sr API key. Keys from app.vllm-sr.ai start with sk-vsr-.",
    "wrong_kind": "This demo needs a key from app.vllm-sr.ai: sk-pt keys only work for chat.",
    "invalid": "This API key was not accepted. Check that you copied all of it, or get a new one.",
    "disabled": "This API key is disabled or cannot use these models.",
    "quota": "This API key has used its daily quota. It resets at 00:00 UTC.",
    "rate_limited": "Too many requests for this key right now. Try again in {s} seconds.",
    "unreachable": "The vllm-sr API cannot be reached right now. Try again in a minute.",
    "error": "The key could not be checked. Try again in a minute.",
    "no_key": "Enter your vllm-sr API key to use the demo.",
    "too_many_checks": "Too many key checks from this browser. Wait a minute and try again.",
}
HTTP_STATUS = {"format": 400, "wrong_kind": 400, "invalid": 401, "no_key": 401, "disabled": 403, "quota": 429,
               "rate_limited": 429, "too_many_checks": 429, "unreachable": 502, "error": 502}
# codes that mean the key itself failed (the turn stops and the page asks for a key again); other 429s are "busy"
KEY_FAILURES = ("invalid", "disabled", "quota", "wrong_kind")


def redact(s):
    return SECRET.sub("sk-***", s)


def message(code, retry_s=None):
    return MESSAGES.get(code, MESSAGES["error"]).format(s=retry_s if retry_s is not None else 30)


class KeyFailed(Exception):
    """A model call refused because of the key (401, 403 key_disabled/access_denied/model_not_allowed, 429 quota).
    Not a model failure: it passes the circuit breaker and the template replies and stops the turn."""

    def __init__(self, code, retry_s=None):
        super().__init__(code)
        self.code, self.retry_s = code, retry_s

    @property
    def text(self):
        return message(self.code, self.retry_s)


class NoKey(KeyFailed):
    def __init__(self):
        super().__init__("no_key")


# ---------------------------------------------------------------------------------------------- the key of this turn

CURRENT = contextvars.ContextVar("vllm_sr_key", default=None)


def env_key():
    return os.environ.get("VLLM_SR_API_KEY", "").strip() or None


def current():
    """The key for a model call now: the visitor's (CURRENT), else the optional env key. Raises NoKey."""
    k = CURRENT.get() or env_key()
    if not k:
        raise NoKey()
    return k


def key_id(k=None):
    """A short one-way id of a key (for per-key cooldowns): never the key."""
    try:
        k = k or current()
    except NoKey:
        return "-"
    return hashlib.sha256(k.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------------------------- checks

def check_format(key):
    """None when the key looks right, else an error code."""
    if not isinstance(key, str):
        return "format"
    k = key.strip()
    if k.startswith("sk-pt-"):
        return "wrong_kind"
    if not SHAPE.match(k):
        return "format"
    return None


def _retry_s(r):
    try:
        return max(1, round(float(r.headers.get("retry-after"))))
    except (TypeError, ValueError):
        return None


def _api_code(r):
    try:
        return str(((r.json() or {}).get("error") or {}).get("code") or "")
    except ValueError:
        return ""


def classify(r):
    """(code, retry_s) for a hosted API response that failed, or None when the status is not an error we map."""
    s, api = r.status_code, None
    if s == 401:
        return "invalid", None
    if s == 403:
        api = _api_code(r)
        return ("wrong_kind" if api == "model_not_allowed" else "disabled"), None
    if s == 429:
        api = _api_code(r)
        return ("quota", None) if api == "quota_exceeded" else ("rate_limited", _retry_s(r))
    if s >= 500:
        return "unreachable", None
    if s >= 400:
        return "error", None
    return None


def key_failure(r):
    """KeyFailed for a response refused because of the key, else None (429 rate limits stay capacity: limits.Busy)."""
    if r.status_code not in (401, 403, 429):
        return None
    code, retry = classify(r)
    return KeyFailed(code, retry) if code in KEY_FAILURES else None


_ok_lock = threading.Lock()
_ok = {}   # sha256(key) -> time it passed the live check


def validate(key, base=None):
    """(ok, code, message). Format first, then one tiny System One call and one tiny System Two call; both must pass."""
    code = check_format(key)
    if code:
        return False, code, message(code)
    key = key.strip()
    h = hashlib.sha256(key.encode()).hexdigest()
    now = time.time()
    with _ok_lock:
        if now - _ok.get(h, 0) < OK_CACHE_S:
            return True, None, None
    base = (base or API_BASE).rstrip("/")
    auth = {"authorization": f"Bearer {key}"}
    calls = [(f"{base}/v1/systemone", {"model": CHECK_D3_MODEL, "state": "ok",
                                       "questions": {"ok": {"type": "noul", "instructions": "Is this text ok?"}}}),
             (f"{base}/v1/chat/completions", {"model": CHECK_LLM_MODEL, "max_tokens": 1,
                                              "messages": [{"role": "user", "content": "Say OK"}]})]
    for url, body in calls:
        try:
            r = requests.post(url, json=body, headers=auth, timeout=CHECK_TIMEOUT)
        except requests.RequestException:
            return False, "unreachable", message("unreachable")
        with r:
            if r.status_code >= 400:
                code, retry = classify(r)
                return False, code, message(code, retry)
    with _ok_lock:
        _ok[h] = now
        for k in [k for k, t in _ok.items() if now - t > OK_CACHE_S]:
            del _ok[k]
    return True, None, None


def forget_ok(key):
    with _ok_lock:
        _ok.pop(hashlib.sha256(key.encode()).hexdigest(), None)


# ---------------------------------------------------------------------------------------------- per-visitor storage

class Store:
    """visitor id -> key, in memory only. A failed key is dropped and its code kept so the page can say why."""

    def __init__(self, idle_s=KEY_IDLE_S, max_visitors=MAX_VISITORS):
        self.lock = threading.Lock()
        self.idle_s, self.max_visitors = idle_s, max_visitors
        self.keys = {}     # visitor -> [key, last used]
        self.failed = {}   # visitor -> (code, time)

    def set(self, visitor, key):
        with self.lock:
            self.keys[visitor] = [key.strip(), time.time()]
            self.failed.pop(visitor, None)
            if len(self.keys) > self.max_visitors:   # drop the least recently used
                for v, _ in sorted(self.keys.items(), key=lambda kv: kv[1][1])[:len(self.keys) - self.max_visitors]:
                    del self.keys[v]

    def get(self, visitor):
        with self.lock:
            e = self.keys.get(visitor) if visitor else None
            if e:
                e[1] = time.time()
                return e[0]
            return None

    def has(self, visitor):
        with self.lock:
            return bool(visitor) and visitor in self.keys

    def delete(self, visitor):
        with self.lock:
            self.keys.pop(visitor, None)
            self.failed.pop(visitor, None)

    def fail(self, visitor, code):
        """The key failed mid-turn: drop it (the next action asks for a key again)."""
        with self.lock:
            e = self.keys.pop(visitor, None)
            if visitor:
                self.failed[visitor] = (code, time.time())
        if e:
            forget_ok(e[0])

    def failure(self, visitor):
        with self.lock:
            f = self.failed.get(visitor)
            return f[0] if f else None

    def sweep(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            for v in [v for v, e in self.keys.items() if now - e[1] > self.idle_s]:
                del self.keys[v]
            for v in [v for v, f in self.failed.items() if now - f[1] > self.idle_s]:
                del self.failed[v]


class Attempts:
    """At most `limit` validation attempts per minute per id (visitor or IP)."""

    def __init__(self, limit=CHECKS_PER_MIN, per=60.0):
        self.lock = threading.Lock()
        self.limit, self.per, self.hits = limit, per, {}

    def allow(self, *ids, now=None):
        now = time.time() if now is None else now
        ids = [i for i in ids if i]
        with self.lock:
            for i in ids:
                q = self.hits.setdefault(i, [])
                q[:] = [t for t in q if now - t < self.per]
                if len(q) >= self.limit:
                    return False
            for i in ids:
                self.hits[i].append(now)
            return True

    def sweep(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            for i in [i for i, q in self.hits.items() if not q or now - q[-1] >= self.per]:
                del self.hits[i]


# ---------------------------------------------------------------------------------------------- log redaction

class Redacting(io.TextIOBase):
    """A text stream that replaces anything shaped like a key with sk-*** before writing."""

    def __init__(self, inner):
        self.inner = inner

    def write(self, s):
        return self.inner.write(redact(s))

    def flush(self):
        self.inner.flush()

    def isatty(self):
        return self.inner.isatty()

    @property
    def encoding(self):
        return getattr(self.inner, "encoding", "utf-8")


def guard_logs():
    """Wrap stdout and stderr once (every log line of the server goes through print or a traceback)."""
    if not isinstance(sys.stdout, Redacting):
        sys.stdout = Redacting(sys.stdout)
    if not isinstance(sys.stderr, Redacting):
        sys.stderr = Redacting(sys.stderr)
