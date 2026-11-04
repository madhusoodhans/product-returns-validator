"""Load test against the stub models (bench/stub_models.py), never against real models.

Start the stub and a demo server pointed at it, then:
  python bench/load_test.py [--url http://127.0.0.1:8632] [--stub http://127.0.0.1:8932]
Phases:
  1. 50 visitors play data/scenarios.json chats at once (some photos sent as uploads); checks that no event of one chat
     shows up in another, reports turn latency (p50 / p95) and the most model calls the stub saw at once per kind.
  2. 2,000 short chats (half leave a photo waiting for an order); reports chats kept and server memory.
  3. Injected 429s: every model call 429 -> every turn answers "busy"; then 30% 429s.
  4. Slow models (8 s per call) with 60 chats at once: the 50-chats-mid-turn cap and the 15 s slot wait answer "busy".
Exit code 1 when a check fails.
"""
import argparse
import base64
import json
import random
import statistics
import sys
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GROUPS = json.loads((ROOT / "data/scenarios.json").read_text())["groups"]
SCENARIOS = [sc for g in GROUPS for sc in g["scenarios"]]
UPLOAD = "data:image/jpeg;base64," + base64.b64encode((ROOT / "data/samples/phone/phone_3_clear.jpg").read_bytes()).decode()
ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://127.0.0.1:8632")
ap.add_argument("--stub", default="http://127.0.0.1:8932")
ap.add_argument("--visitors", type=int, default=50)
ap.add_argument("--rounds", type=int, default=3, help="chats per visitor in phase 1")
ap.add_argument("--chats", type=int, default=2000)
ap.add_argument("--skip-slow", action="store_true")
A = ap.parse_args()
problems = []
_ip = iter(range(1, 10**9))
_ip_lock = threading.Lock()


def new_ip():   # each chat comes from its own visitor address (X-Forwarded-For), like many people
    with _ip_lock:
        n = next(_ip)
    return f"10.{n >> 16 & 255}.{n >> 8 & 255}.{n & 255}"


def get(path, base=None):
    with urllib.request.urlopen((base or A.url) + path, timeout=30) as r:
        return json.loads(r.read())


def stub(**cfg):
    req = urllib.request.Request(A.stub + "/stub/config", json.dumps(cfg).encode(), {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def turn(sid, ip, text=None, image=None, has_turns=False):
    """One POST /api/chat; returns (seconds, events)."""
    body = {"session": sid, "has_turns": has_turns, **({"text": text} if text else {}), **({"image": image} if image else {})}
    req = urllib.request.Request(A.url + "/api/chat", json.dumps(body).encode(),
                                 {"content-type": "application/json", "x-forwarded-for": ip})
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=180) as r:
        evs = [json.loads(line[5:]) for line in r.read().decode().splitlines() if line.startswith("data:")]
    return time.perf_counter() - t, evs


def check_own(sid, evs):
    for e in evs:
        if e["type"] == "done" and not e["turn"].startswith(sid):
            problems.append(f"chat {sid[:8]} got turn {e['turn']}")
        if e["type"] == "stats" and e.get("session") != sid:
            problems.append(f"chat {sid[:8]} got stats of {e.get('session')}")


def verdict(evs):
    return next((e.get("verdict") for e in evs if e["type"] == "message"), None)


def play(sc, upload_photos):
    sid, ip, out = str(uuid.uuid4()), new_ip(), []
    for i, t in enumerate(sc["turns"]):
        image = t.get("image")
        if image and upload_photos:
            image = UPLOAD
        secs, evs = turn(sid, ip, t.get("text"), image, has_turns=i > 0)
        check_own(sid, evs)
        out.append((secs, verdict(evs)))
        time.sleep(random.uniform(0.2, 1.0))   # reading and typing
    return out


def visitor(n):
    res = []
    for r in range(A.rounds):
        res += play(random.choice(SCENARIOS), upload_photos=(n + r) % 4 == 0)
    return res


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def phase1():
    stub(p429=0, p503=0, slow_ms=0, reset_counts=True)
    t = time.perf_counter()
    with ThreadPoolExecutor(A.visitors) as pool:
        res = [x for r in pool.map(visitor, range(A.visitors)) for x in r]
    secs = [s for s, _ in res]
    verdicts = [v for _, v in res]
    peak = get("/stub/config", A.stub)["busy_max"]
    print(f"1. {A.visitors} visitors, {len(res)} turns in {time.perf_counter() - t:.0f} s: "
          f"p50 {statistics.median(secs):.2f} s, p95 {pct(secs, 95):.2f} s, max {max(secs):.2f} s; "
          f"busy {verdicts.count('busy')}, rate limited {verdicts.count('rate_limited')}, "
          f"errors {verdicts.count('error')}; most model calls at once {peak}")
    if verdicts.count("error") or verdicts.count("rate_limited") or None in verdicts:
        problems.append("phase 1: error, rate-limited or missing replies")
    if any(v > 12 for v in peak.values()):
        problems.append(f"phase 1: more than 12 calls at once {peak}")
    return pct(secs, 95)


def short_chat(i):
    sid, ip = str(uuid.uuid4()), new_ip()
    if i % 2:
        _, evs = turn(sid, ip, image="samples/mug/mug_2_clear.jpg")   # kept until an order is known
    else:
        _, evs = turn(sid, ip, "Order 6230, one mug arrived chipped")
    check_own(sid, evs)
    return verdict(evs)


def phase2():
    before = get("/api/metrics")["memory_mb"]
    t = time.perf_counter()
    with ThreadPoolExecutor(A.visitors) as pool:
        verdicts = list(pool.map(short_chat, range(A.chats)))
    m = get("/api/metrics")
    print(f"2. {A.chats} chats in {time.perf_counter() - t:.0f} s: kept {m['chats_in_memory']} chats, memory "
          f"{before} MB before phase 2 -> {m['memory_mb']} MB; busy {verdicts.count('busy')}, "
          f"errors {verdicts.count('error')}")
    if m["chats_in_memory"] > 300:
        problems.append(f"phase 2: {m['chats_in_memory']} chats kept (max 300)")
    return m


def phase3():
    stub(p429=1.0, retry_after=1)
    first = [s["turns"][0] for s in SCENARIOS if s["turns"][0].get("text")]

    def one(i):
        secs, evs = turn(str(uuid.uuid4()), new_ip(), first[i % len(first)]["text"])
        failed = [e for e in evs if e["type"] == "step" and e["status"] == "failed"]
        return secs, verdict(evs), bool(failed) and "busy" in (failed[0]["summary"] or "").lower()
    with ThreadPoolExecutor(20) as pool:
        res = list(pool.map(one, range(20)))
    busy = sum(v == "busy" and f for _s, v, f in res)
    print(f"3a. every call 429: {busy}/20 turns answered busy with a failed capacity step, "
          f"max {max(s for s, *_ in res):.1f} s")
    if busy != 20:
        problems.append(f"phase 3a: {busy}/20 busy")
    stub(p429=0.3, retry_after=0.5)
    with ThreadPoolExecutor(20) as pool:
        res = list(pool.map(one, range(40)))
    vs = [v for _s, v, _f in res]
    print(f"3b. 30% of calls 429: {vs.count('busy')}/40 busy, {40 - vs.count('busy') - vs.count('error')} answered, "
          f"{vs.count('error')} errors; throttled so far {get('/api/metrics')['throttled_429_503']}")
    if vs.count("error"):
        problems.append("phase 3b: errors under 429s")
    stub(p429=0)


def phase4():
    stub(slow_ms=8000)
    first = [s["turns"][0]["text"] for s in SCENARIOS if s["turns"][0].get("text")]

    def one(i):
        secs, evs = turn(str(uuid.uuid4()), new_ip(), first[i % len(first)])
        return secs, verdict(evs), next((e["summary"] for e in evs if e.get("status") == "failed"), "")
    with ThreadPoolExecutor(60) as pool:
        res = list(pool.map(one, range(60)))
    stub(slow_ms=0)
    vs = [v for _s, v, _f in res]
    cap = sum("mid-turn" in f for *_x, f in res)
    waited = sum("waited" in f for *_x, f in res)
    print(f"4. slow models, 60 chats at once: {vs.count('busy')} busy ({cap} over the 50-chat cap, {waited} waited "
          f"over 15 s for a slot), {60 - vs.count('busy')} answered, {vs.count('error')} errors")
    if not cap or vs.count("busy") < 10 or vs.count("error"):
        problems.append("phase 4: cap not applied or errors")


if __name__ == "__main__":
    p95 = phase1()
    phase2()
    phase3()
    if not A.skip_slow:
        phase4()
    print("\nproblems:", problems or "none")
    sys.exit(1 if problems else 0)
