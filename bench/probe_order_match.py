"""Probe: can d3-mini pick which of the customer's orders a complaint is about, without an order number?"""
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
from models import d3
ORDERS = {o["order"]: o for o in json.loads((ROOT / "data/orders.json").read_text())["orders"]}
CUST = json.loads((ROOT / "data/customer.json").read_text())

def question():
    crit = {o: f"{ORDERS[o]['item']}, delivered {ORDERS[o]['delivered_days_ago']} days ago" for o in CUST["orders"]}
    crit["unclear"] = "The message does not say which item it is about"
    return {"order": {"type": "choice", "instructions": "Which of the customer's orders is the message about?", "criteria": crit}}

def state(text):
    return f"A customer wrote to a shop's returns chat:\n\"{text}\""

CASES = [("My phone arrived with a cracked screen", "4411"), ("the mugs came chipped", "6230"),
 ("one of the wine glasses was broken in the box", "6345"), ("the headphones have no sound on the right", "8018"),
 ("my water bottle has a crack", "3301"), ("the lamp shade is torn", "9311"),
 ("the running shoes sole is coming off", "7102"), ("the black canvas sneakers are falling apart", "7103"),
 ("the 16 inch laptop screen is cracked", "5121"), ("the laptop hinge is broken", None),
 ("my sneakers are damaged", None), ("something I ordered is broken", "unclear"), ("hi", "unclear"),
 ("bhai mera phone ka screen toota hua aaya", "4411"), ("the pink bottle leaks", "3301"),
 ("the 14 inch laptop has a dent", "5120"), ("ceramic cup broken", "6230"), ("lampe kaputt", "9311"),
 ("the item is broken", "unclear"), ("the glass thing shattered", "6345")]
for text, want in CASES:
    a, ms, _ = d3(state(text), question())
    pr = a["order"]["probabilities"]; top = sorted(pr.items(), key=lambda kv: -kv[1])[:2]
    ok = (want is None and top[0][1] < 0.7) or (want is not None and top[0][0] == want)
    print(f"{'ok ' if ok else 'BAD'} want {str(want):8s} got {top[0][0]:8s} {top[0][1]:.2f} (2nd {top[1][0]} {top[1][1]:.2f}) {ms:.0f} ms | {text}")
