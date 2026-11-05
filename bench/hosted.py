"""Bench helper for the hosted vllm-sr APIs: the key, one System One call with a 429 retry, and a small pool.

The key comes from env VLLM_SR_API_KEY, else from ~/.vllm-sr-key (bench scripts only; the app reads the env var).
It is never printed. At most 4 requests are in flight (POOL), to be gentle with a shared key.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

BASE = os.environ.get("VLLM_SR_API_BASE", "https://app.vllm-sr.ai").rstrip("/")
POOL = 4
CALLS = {"n": 0}


def key():
    k = os.environ.get("VLLM_SR_API_KEY")
    if not k:
        p = Path.home() / ".vllm-sr-key"
        k = p.read_text().strip() if p.is_file() else ""
    if not k:
        raise SystemExit("No API key: set VLLM_SR_API_KEY (or put it in ~/.vllm-sr-key)")
    return k


def use_key():
    """Bench scripts that drive the app: hand the key to it through the environment (never printed)."""
    os.environ.setdefault("VLLM_SR_API_KEY", key())


def systemone(body, timeout=(5, 35)):
    """POST /v1/systemone; returns (answers, ms). Retries a 429/503 (after Retry-After) up to 3 times."""
    h = {"authorization": f"Bearer {key()}"}
    for attempt in range(4):
        t = time.perf_counter()
        CALLS["n"] += 1
        r = requests.post(f"{BASE}/v1/systemone", json=body, headers=h, timeout=timeout)
        ms = round((time.perf_counter() - t) * 1000, 1)
        if r.status_code in (429, 503) and attempt < 3:
            time.sleep(min(10.0, float(r.headers.get("retry-after") or 2)))
            continue
        if not r.ok:
            err = (r.json().get("error") or {}) if r.headers.get("content-type", "").startswith("application/json") else {}
            raise RuntimeError(f"systemone {r.status_code} {err.get('type')} {err.get('code')}: {err.get('message')}")
        return r.json()["answers"], ms


def pmap(fn, items):
    """fn over items, at most POOL at once, results in order."""
    with ThreadPoolExecutor(POOL) as ex:
        return list(ex.map(fn, items))
