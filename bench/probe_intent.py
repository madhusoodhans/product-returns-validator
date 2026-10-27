"""Probe: an `intent` choice added to the D3 scope request (no extra call)."""
import sys, statistics
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from models import d3
import turn_checks

INTENT = {"type": "choice", "instructions": "What does the customer want with their latest message?",
          "criteria": {
              "claim": "To report or describe a problem with an item, give or pick an order, send evidence, or answer the assistant's question",
              "refund_info": "To know how or when the refund, replacement or return label will happen, or to choose a replacement instead of a refund",
              "policy_info": "To know the return policy or rules, such as what is covered or how long they have",
              "human": "To talk to a person, a manager or a human agent",
              "change_of_mind": "To return an item that is not damaged or faulty, for example they changed their mind, it does not fit or they do not like it",
              "why": "To know why their photo or claim was not accepted, or what was wrong with it",
              "chitchat": "To greet, thank or make small talk"}}
C = [
 ("Order 4411, the screen was cracked when I opened the box.", None, "claim"),
 ("it was like that when it arrived", "Was the chip already there when the mugs arrived?", "claim"),
 ("yes", "Was the damage already there when the package arrived?", "claim"),
 ("the 14 inch one", "Is it the 16-inch Pro (order 5121) or the 14-inch laptop (order 5120)?", "claim"),
 ("Order 6230", None, "claim"),
 ("actually I dropped it", "Please send a photo of the cracked screen.", "claim"),
 ("also my headphones 8018 arrived with a cracked ear cup", "Your return is approved.", "claim"),
 ("How long does the refund take?", None, "refund_info"),
 ("can I get a replacement instead of a refund?", None, "refund_info"),
 ("when will the label arrive?", "Your return is approved. A free return label will be emailed within 10 minutes.", "refund_info"),
 ("what's your return policy for sneakers?", None, "policy_info"),
 ("how many days do I have to return the lamp?", None, "policy_info"),
 ("what is covered for phones?", None, "policy_info"),
 ("can I talk to a human please", None, "human"),
 ("I want a person, not a bot", "Please send a photo of the damage.", "human"),
 ("get me your manager", None, "human"),
 ("I want to speak to a manager, nobody is helping me with my broken laptop.", None, "human"),
 ("nothing is wrong I just changed my mind", None, "change_of_mind"),
 ("the sneakers are too small, I want to return them", None, "change_of_mind"),
 ("I don't like the colour of the mugs", None, "change_of_mind"),
 ("why? what's wrong with it?", "The photo is too dark. Please send a new photo.", "why"),
 ("why was my photo rejected?", "We could not use that photo.", "why"),
 ("why was my claim refused?", "Sorry, damage after delivery is not covered.", "why"),
 ("thanks!", "Your return is approved.", "chitchat"),
 ("hi", None, "chitchat"),
 ("ok", "Please send a photo of the damage.", "chitchat"),
 ("great, thank you so much", None, "chitchat"),
]
def state(msg, prev):
    s = "A customer is chatting with a shop's returns assistant.\n"
    if prev: s += f"Assistant's previous message: \"{prev}\"\n"
    return s + f"Customer's latest message (written by the customer): \"{msg}\""
q = dict(turn_checks.SCOPE_Q); q["intent"] = INTENT
ok = 0; ms_ = []
for msg, prev, want in C:
    a, ms, _ = d3(state(msg, prev), q); ms_.append(ms)
    got = a["intent"]["choice"]; p = a["intent"]["probabilities"][got]; ok += got == want
    print(f"{'ok ' if got == want else 'BAD'} {got:14s} {p:.2f} (want {want:14s}) scope={a['scope']['choice']:9s} cmp={a['complaint']['noul']:.2f} | {msg[:60]}")
print(ok, "/", len(C), "median ms", statistics.median(ms_))
