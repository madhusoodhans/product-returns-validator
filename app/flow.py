"""The returns chat flow: one Session per chat, one generator per turn yielding the docs/API.md events.

The state machine decides what runs; models answer questions; reply.py words the decision.

A chat can hold several claims, one per order (Claim). Each claim moves through
    need_problem -> (eligibility) -> need_photo -> closed (accept | not_eligible | human)
                     \\-> clarify (ask; re-checked with every new customer message about the problem)
Any new message about the problem re-runs eligibility until the claim is closed, so "actually I dropped it" after an
eligible claim still counts. A photo is checked whenever it arrives: before the order is known it is kept for later,
before the claim is clear its result is kept, and it decides the claim once the claim is eligible.
"""
import copy
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import availability
import claim
import eligibility
import photo
import reply
import turn_checks
from availability import UNAVAILABLE_TEXT, Unavailable
from models import D3_MODEL, GUARD_MODEL, LLM_MODEL, BadImage, chat_stream, image_url

NODES = ["guardrail", "router", "claim", "policy", "eligibility", "image_check", "decision", "reply"]
MAX_PHOTOS = 3
MAX_CLARIFY = 2
MATCH_SWITCH = 0.9
# "let's do the phone now" was read as wanting a person: naming another item without these words is a claim step
PERSON_WORDS = re.compile(r"\b(human|person|people|agent|manager|someone|somebody|staff|representative)\b", re.I)   # D3 order match needed to move to ANOTHER order without an order number
RULES = {
    "blocked": "instructions to the assistant, a jailbreak or commands, without a real complaint -> blocked",
    "off_topic": "not about orders or returns -> fixed reply",
    "bad_file": "the upload is not a readable image -> ask for a photo",
    "need_order": "no order of this customer yet -> ask for it",
    "need_problem": "no problem described -> ask",
    "clarify": "visible damage without timing, or unclear problem -> ask",
    "need_photo": "eligible -> ask for a photo",
    "retake": "photo unusable or damage hidden -> retake",
    "mismatch": "photo is not of the ordered product -> ask for the ordered item",
    "no_damage": "damage not visible in a usable photo -> ask for a closer photo",
    "accept": "eligible and damage clearly visible -> approve",
    "not_eligible": "policy or eligibility check failed -> not covered",
    "human": "not visual, over the review limit, too many photos or the customer asked -> a person",
    "info": "a question, not a claim step -> answer from the FAQ and policy, claim unchanged",
    "closed": "claim already decided -> answer only",
}
FAQ = claim.FAQ


class Claim:
    """One claim for one order."""

    def __init__(self, order):
        self.order = order
        self.stage = "need_problem"
        self.complaint = []      # (question the bot asked or None, customer message) about the problem
        self.clarified = 0
        self.photos = 0          # photos checked for this claim
        self.photo = None        # (verdict, reasons) of a usable photo checked before the claim was eligible
        self.policy_ok = None
        self.final = None        # (verdict, reason) once closed
        self.photo_checked = False
        self.last = None         # (verdict, reasons, reason) of the last claim decision, for "why?"
        self.photo_log = []      # (verdict, reasons) of every photo checked for this claim, for "why?"


class Session:
    def __init__(self, sid):
        self.sid = sid
        self.history = [{"role": "assistant", "content": claim.CUSTOMER["greeting"]}]  # chat for the LLM
        self.unmatched = []      # customer messages before an order is known (for D3 order matching)
        self.claims = {}         # order id -> Claim
        self.claim = None        # the claim this chat is on
        self.pending_image = None  # a photo sent before any order was known (data URL), checked once one is
        self.pending_at = 0.0      # when it came (the server drops it after a while)
        self.queued = []         # other order ids named in one message, handled after the current claim
        self.turns = 0
        self.ended = None          # set when the chat is over (see END): no further turns except a review request
        self.blocked_n = 0         # blocked turns in this chat (2nd ends it)
        self.off_topic_run = 0     # off-topic turns in a row (3rd ends it)

    # ------------------------------------------------------------------ one turn
    def turn(self, text=None, image=None, action=None):
        """One turn's events. A model that fails mid-turn ends the turn with UNAVAILABLE_TEXT and leaves the chat as it
        was before the turn (the customer can simply send the message again)."""
        if self.ended and not (action == "review" and self.ended["reason"] == "not_covered"):
            yield from self._ended_turn()
            return
        if not availability.available("d3"):
            # pre-flight: D3 makes every decision, so nothing can be decided without it; no model call
            yield from self._unavailable_turn()
            return
        snap = self._snapshot()
        nodes = {}
        try:
            for ev in self._turn(text, image, action):
                if ev["type"] == "step":
                    nodes[ev["node"]] = ev["status"]
                yield ev
        except Unavailable as e:
            self.__dict__.update(snap)
            for ev in failed_steps(nodes, e.text):
                yield ev
            yield from self._fixed_message(UNAVAILABLE_TEXT, "unavailable")
        except Exception:
            self.__dict__.update(snap)   # busy or a bug: the server answers; the chat stays as it was
            raise

    def _snapshot(self):
        keep = {id(o): o for o in claim.ORDERS.values()}   # orders are shared data, not chat state
        return copy.deepcopy(self.__dict__, keep)

    def _fixed_message(self, text, verdict):
        """A fixed reply without a stored turn (no `done`: no stats, nothing to replay), as for busy."""
        yield {"type": "token", "text": text}
        yield {"type": "message", "role": "assistant", "text": text, "verdict": verdict, "order": None,
               "policy_ref": None, "ended": self.ended}

    def _unavailable_turn(self):
        for n in NODES:
            yield {"type": "step", "node": n, "status": "failed" if n == "guardrail" else "skipped",
                   "summary": "D3 unavailable: nothing can be decided without it, no model call" if n == "guardrail"
                   else "Not run", "model": None, "ms": None, "detail": None}
        yield from self._fixed_message(UNAVAILABLE_TEXT, "unavailable")

    def _turn(self, text=None, image=None, action=None):
        force_person = False
        if action == "review":
            # the only thing allowed after "not covered": ask a colleague to review the decision
            text, image, force_person = "Please have a colleague review this decision.", None, True
            self.ended = None
        self.turns += 1
        tid = f"{self.sid}-t{self.turns}"
        calls = []                 # (model, ms, tokens) for the stats
        steps = {}

        def step(node, status, summary=None, model=None, ms=None, detail=None):
            ev = {"type": "step", "node": node, "status": status, "summary": summary, "model": model, "ms": ms,
                  "detail": detail}
            steps[node] = ev
            return ev

        def skip_rest(after, why):
            for n in NODES[NODES.index(after) + 1:NODES.index("decision")]:
                if n not in steps or steps[n]["status"] in ("waiting", "running"):
                    yield step(n, "skipped", why)

        text = (text or "").strip() or None
        verdict, ctx = None, {"reasons": []}
        # the upload is decoded first: a file that is not an image changes nothing in the chat
        img = None
        if image:
            try:
                img = image_url(image)
            except BadImage:
                verdict, ctx["fixed"] = "bad_file", "bad_file"
        user_msg = (reply.scrub(text) if text else "") + ("\n[The customer sent a photo]" if image else "")
        self.history.append({"role": "user", "content": user_msg.strip()})
        for n in NODES:
            yield step(n, "waiting")

        # 1. guardrail: Vela (prompt attack, harm) and D3's scope check, both at once on the hosted System One API.
        # 2. router: vllm-sr/auto picks the model and reasoning effort when the reply is written (step filled in then).
        intent = "claim"
        yield step("guardrail", "running" if text else "skipped", None if text else "No text in this turn")
        if text:
            prev = next((m["content"] for m in reversed(self.history[:-1]) if m["role"] == "assistant"), None)
            with ThreadPoolExecutor(2) as pool:   # each thread copies this context: its 429s count for this turn
                fv = pool.submit(copy_context().run, turn_checks.vela_guard, text)
                fs = pool.submit(copy_context().run, turn_checks.scope_check, text, prev)
                vg, vms, vtok = fv.result()
                sc, sdetail, sms, stok = fs.result()
            if vg:
                calls.append(("vela", vms, vtok))
            calls.append(("d3", sms, stok))
            # Vela's attack score flags any message; its harm score only one D3 does not already call off-topic (a coding
            # request is off-topic, not an attack; off-topic gets its own fixed reply)
            vela_attack = bool(vg) and vg["attack"] >= turn_checks.ATTACK
            vela_block = vela_attack or (bool(vg) and vg["harm"] >= turn_checks.HARM and sc["scope"] != "off_topic")
            intent = sc["intent"]
            ctx["complaint"] = sc["complaint"]
            command = sc["command"] >= 0.5
            names_own = any(n in claim.CUSTOMER["orders"] for n in re.findall(r"\b\d{4,6}\b", text))
            sure_normal = sc["scope"] == "returns" and sc["command"] < 0.2 and (
                sc["probs"].get("returns", 0) >= 0.9 or names_own)
            vela_false_alarm = vela_block and sure_normal
            injection = (vela_block and not sure_normal) or sc["scope"] == "instructions" or command
            if intent == "human" and not command:
                injection = False      # "I want a person, not a bot" reads as instructions; it is a fair request
            sdetail["ms"] = sms
            gdetail = {"attack": round(vg["attack"], 4) if vg else None, "harm": round(vg["harm"], 4) if vg else None,
                       "vela_flagged": vela_block, "vela_ms": vms if vg else None,
                       "thresholds": {"attack": turn_checks.ATTACK, "harm": turn_checks.HARM},
                       "scope_check": sdetail, "complaint": round(sc["complaint"], 4),
                       "model": f"{GUARD_MODEL} + {D3_MODEL}" if vg else D3_MODEL}
            jb = f"Vela attack {vg['attack']:.2f}, harm {vg['harm']:.2f}" if vg else "D3 only"
            coded = command and re.search(r"[;|&$<>{}`]|rm -|drop table|curl |powershell", text, re.I)
            what = ("commands or code" if coded else
                    "a prompt attack (Vela)" if vela_attack and sc["scope"] != "instructions" else
                    "a harmful request (Vela)" if vela_block and sc["scope"] != "instructions" else
                    "instructions to the assistant")
            gstatus, summary = "done", f"Passed ({jb}; D3: on topic)"
            if vela_false_alarm:
                summary = f"Passed: Vela flagged it ({jb}), D3 sure it is a normal returns message"
            if injection and sc["complaint"] >= 0.5:
                summary = f"Ignored {what}; real complaint handled"
            elif injection:
                gstatus, summary, verdict = "blocked", f"Blocked: {what}", "blocked"
                ctx["fixed"] = "blocked_command" if coded else "blocked"
            elif sc["scope"] == "off_topic" and intent not in ("human", "refund_info", "policy_info"):
                gstatus, summary, verdict = "blocked", "Off-topic: fixed reply, no model call", "off_topic"
                ctx["fixed"] = "off_topic"
            gdetail["flagged"] = gstatus == "blocked"
            yield step("guardrail", gstatus, summary + ("" if vg else "; Vela unavailable, D3 checked alone"),
                       gdetail["model"], max(vms, sms) if vg else sms, gdetail)

        fixed = verdict in ("blocked", "off_topic", "bad_file")
        if fixed:
            yield step("router", "skipped", "Not called: fixed reply, no language model")
        elif not availability.available("llm"):
            yield step("router", "skipped", "Language model unavailable: standard reply")

        # 3-6. the claim
        if verdict in ("blocked", "off_topic"):
            yield from skip_rest("router", "Off-topic" if verdict == "off_topic" else "Blocked by the guardrail")
        elif verdict == "bad_file":
            yield from skip_rest("router", "The upload was not a readable image")
        elif intent in ("refund_info", "policy_info", "change_of_mind", "why", "chitchat") and not image \
                and (intent == "change_of_mind" or not (text and re.search(r"\b\d{4,6}\b", text))) \
                and not (intent != "policy_info" and self._other_claim_named(text)):
            verdict = yield from self._answer(intent, text, step, skip_rest, calls, ctx)
        elif force_person:
            verdict = yield from self._to_person(step, skip_rest, ctx)
        elif intent == "human" and not (self._other_claim_named(text) and not PERSON_WORDS.search(text or "")):
            verdict = yield from self._to_person(step, skip_rest, ctx)
        else:
            verdict = yield from self._claim_flow(text, img, step, skip_rest, calls, ctx)

        # 7. decision
        c = self.claim
        if verdict in ("accept", "not_eligible", "human") and c and not c.final:
            c.stage, c.final = "closed", (verdict, ctx.get("reason") or ", ".join(ctx["reasons"]))
        if c and verdict not in ("info", "closed", "blocked", "off_topic", "bad_file"):
            c.last = (verdict, list(ctx["reasons"]), ctx.get("reason"))
        if verdict == "closed" and c and c.final:
            ctx["what"] = f"closed_{c.final[0]}"
            ctx["previous"] = c.final[1]
            ctx["facts"] = ctx.get("facts") or [FAQ["label"], FAQ["refund"], FAQ["replacement"]] if c.final[0] == "accept" else ctx.get("facts")
        if self.queued and c and c.final and verdict in ("accept", "not_eligible", "human"):
            nxt = self.queued[0][0]
            ctx["extra_note"] = (f"Then say that next you will look at their other order {nxt} "
                                 f"({claim.ORDERS[nxt]['item']}).")
            std_note(ctx, f"Next, I will look at your other order {nxt} ({claim.ORDERS[nxt]['item']}).")
        # the chat ends when the customer has an outcome, or after repeated abuse
        self.blocked_n += verdict == "blocked"
        self.off_topic_run = self.off_topic_run + 1 if verdict == "off_topic" else 0
        end = None
        if self.blocked_n >= 2 and verdict == "blocked":
            end = "blocked"
        elif self.off_topic_run >= 3:
            end = "off_topic"
        elif verdict in ("accept", "not_eligible", "human") and not self.queued:
            end = {"accept": "approved", "not_eligible": "not_covered", "human": "handed_over"}[verdict]
            if verdict == "human" and c and c.final and c.final[0] == "accept":
                end = "approved_and_handed_over"
        yield step("decision", "done", verdict.replace("_", " ").capitalize(), None, 0,
                   {"verdict": verdict, "reasons": ctx["reasons"] or ([ctx["reason"]] if ctx.get("reason") else []),
                    "rule": RULES.get(verdict, "")})

        # 8. reply
        ctx.update(product=c and c.order["product"], order=c and c.order["order"], item=c and c.order["item"])
        if end and not ctx.get("fixed"):
            # the last message of the chat: no question the customer can no longer answer
            ctx["extra_note"] = (ctx.get("extra_note", "") + " " + reply.LAST_MESSAGE).strip()
        if ctx.get("fixed"):
            # blocked, off-topic and unreadable uploads get a fixed reply: no model call, nothing to steer
            key = ctx["fixed"] + ("_end" if end and ctx["fixed"] + "_end" in reply.FIXED else "")
            out = {"text": reply.FIXED[key], "ms": 0, "tokens": 0}
            yield step("reply", "skipped", "Fixed reply, no model call")
        else:
            out = None
            failed = ctx.setdefault("llm_failed", {})
            # one call to vllm-sr/auto; not again when it already failed in this turn (claim extraction)
            if ctx.get("router_guard"):   # the Router's guard already declined this turn's claim reading: no second call
                out = {"route": ctx["router_guard"], "ms": 0, "tokens": 0, "text": ""}
            elif availability.available("llm") and "llm" not in failed:
                yield step("router", "running", "vllm-sr/auto picks the model and reasoning for the reply", LLM_MODEL)
                yield step("reply", "running", None, LLM_MODEL)
                try:
                    for kind, val in chat_stream(reply.messages(self.history, verdict, ctx)):
                        if kind == "done":
                            out = val   # buffered: the reply is checked before the customer sees it
                except Unavailable as e:
                    failed["llm"] = e.text
            if out is not None and router_guarded(out.get("route")):
                # the hosted Router's own safety guard answered instead of a model (it read the customer's words in
                # the reply call): its refusal is not a reply to the customer, the decision is worded from a template
                if out["ms"]:
                    calls.append(("llm", out["ms"], out["tokens"]))
                ctx["route"] = out["route"]
                yield step("router", "blocked", "Guard decision: the Router declined the call (the customer's words "
                           "were flagged); no model ran", None, out["route"].get("routing_ms"),
                           {**out["route"], "call": "reply", "extraction": ctx.get("extract_route")})
                yield from self._standard_reply(verdict, ctx, "the Router's safety guard declined the reply call", step,
                                                "Standard reply: the Router's guard declined the call")
                out = {"text": reply.standard(verdict, ctx)}
            elif out is None:
                if steps["router"]["status"] in ("waiting", "running"):
                    yield step("router", "skipped" if steps["router"]["status"] == "waiting" else "failed",
                               "No answer from vllm-sr/auto: standard reply", LLM_MODEL)
                yield from self._standard_reply(verdict, ctx, "; ".join(failed.values()), step)
                out = {"text": reply.standard(verdict, ctx)}
            else:
                used = LLM_MODEL
                route = out.get("route") or {}
                ctx["route"] = route
                calls.append(("llm", out["ms"], out["tokens"]))
                rdetail = {"model": route.get("model"), "tokens": out["tokens"], "route": route}
                summary, ms = f"{out['tokens']} tokens by {route.get('model') or 'the routed model'}", out["ms"]
                yield step("router", "done", route_summary(route), route.get("model") or LLM_MODEL,
                           route.get("routing_ms"), {**route, "call": "reply", "extraction": ctx.get("extract_route")})
                approved = verdict == "accept" or (c and c.final and c.final[0] == "accept"
                                                   and verdict in ("closed", "info", "human"))
                if not approved:
                    # D3 reply check: the words must not claim an approval that was not given
                    try:
                        p, cms, ctok = turn_checks.reply_claims_approval(out["text"])
                    except Unavailable as e:   # unchecked words are not sent: the standard reply instead
                        yield from self._standard_reply(verdict, ctx, f"reply check: {e.text}", step,
                                                        "Standard reply: the D3 reply check is unavailable")
                        out, used = {"text": reply.standard(verdict, ctx)}, None
                    else:
                        calls.append(("d3", cms, ctok))
                        ms += cms
                        rdetail["reply_check"] = {"model": D3_MODEL, "claims_approval": round(p, 4),
                                                  "threshold": 0.5, "replaced": p >= 0.5, "ms": cms}
                        summary += ", checked by D3"
                        if p >= 0.5:
                            rdetail["reply_check"]["original"] = out["text"]
                            key = ctx.get("what") or verdict
                            out["text"] = reply.FALLBACK.get(key, reply.FALLBACK["need_order"])
                            if end:
                                out["text"] = reply.FALLBACK.get(key + "_end", out["text"])
                            summary = f"Replaced: the reply claimed an approval ({p:.2f})"
                if used:
                    yield step("reply", "done", summary, used, ms, rdetail)
        for piece in out["text"].split(" "):
            yield {"type": "token", "text": piece + " "}
        self.history.append({"role": "assistant", "content": out["text"]})
        if end:
            self.ended = ending(end, c)
        yield {"type": "message", "role": "assistant", "text": out["text"], "verdict": verdict,
               "order": c and {k: c.order[k] for k in ("order", "product", "item", "price", "delivered_days_ago")},
               "policy_ref": policy_ref(verdict, ctx, c), "ended": self.ended}
        yield {"type": "done", "turn": tid, "calls": calls, "route": ctx.get("route"),
               "steps": [steps[n] for n in NODES if n in steps],
               "avoided": bool(c and c.final and verdict in ("not_eligible", "human") and not c.photo_checked)}

    def _standard_reply(self, verdict, ctx, why, step, summary="Standard reply: language model unavailable"):
        """The reply step of a standard (template) reply; the text is reply.standard(verdict, ctx)."""
        yield step("reply", "done", summary, None, 0,
                   {"standard": True, "reason": why or "Language model unavailable", "model": None})

    def _ended_turn(self):
        """A message to a chat that has ended: nothing runs, nothing changes."""
        tid = f"{self.sid}-x{self.turns}"
        steps = []
        for n in NODES:
            ev = {"type": "step", "node": n, "status": "skipped", "summary": "The chat has ended", "model": None,
                  "ms": None, "detail": None}
            steps.append(ev)
            yield ev
        text = "This chat has ended. Please start a new chat if you need help with another order."
        yield {"type": "token", "text": text}
        yield {"type": "message", "role": "assistant", "text": text, "verdict": "ended", "order": None,
               "policy_ref": None, "ended": self.ended}
        yield {"type": "done", "turn": tid, "calls": [], "steps": steps, "avoided": False, "no_turn": True}

    # ------------------------------------------------------------------ questions that are not claim steps
    def _next_step(self, std=False):
        """The claim's next step: an instruction for the LLM, or (std) a sentence for the standard reply."""
        c, nxt = self.claim, reply.NEXT_STD if std else reply.NEXT
        if not c:
            return nxt["need_order"]
        item = photo.NOUN.get(c.order["product"], c.order["item"]) if std else c.order["item"]
        if c.final:
            return nxt[f"closed_{c.final[0]}"].format(item=item)
        if c.stage == "clarify":
            q = next((m["content"] for m in reversed(self.history[:-1]) if m["role"] == "assistant"), "")
            return nxt["clarify"].format(question=q)
        return nxt[c.stage].format(item=item)

    def _answer(self, intent, text, step, skip_rest, calls, ctx):
        """FAQ, policy questions, change of mind, "why?" and small talk: answered from facts, the claim is unchanged."""
        c = self.claim
        yield step("claim", "skipped", f"Not a claim step ({intent.replace('_', ' ')}); claim unchanged")
        yield from skip_rest("claim", "Not needed: a question, the claim is unchanged")
        ctx["next"], ctx["std_next"] = self._next_step(), self._next_step(std=True)
        facts, std = [], None   # std: the facts for a standard reply when they differ
        status = c and c.final and (f"Your {photo.NOUN.get(c.order['product'], 'item')} claim is "
                                    f"{reply.STATUS_WORDS[c.final[0]]}.")
        if intent == "refund_info":
            facts = [FAQ["label"], FAQ["refund"], FAQ["replacement"]]
            std = [reply.REFUND_STD]
            if c and c.final:
                facts.append(f"This claim's status: {c.final[0].replace('_', ' ')}.")
                std.append(status)
            elif c:
                facts.append("This claim is not decided yet, so no label has been sent.")
                std.append("Your claim is not decided yet, so no label has been sent.")
        elif intent == "policy_info":
            named = claim.products_in(text)
            prod = named[0] if named else c and c.order["product"]
            if not prod:
                chosen, _c, mdet, mms, mtok = claim.match_order(text)
                calls.append(("d3", mms, mtok))
                prod = chosen and claim.ORDERS[chosen]["product"]
            if prod:
                p = claim.POLICIES[prod]
                facts = [f"{p['name']} policy: returns within {p['window_days']} days of delivery. Covered: {p['covered']} "
                         f"Not covered: {'; '.join(p['not_covered'])}. Photo needed: {p['photo']}"]
            else:
                facts = ["The return policy depends on the item. Ask which item they mean."]
                std = ["The return policy depends on the item. Which item do you mean?"]
                ctx["std_next"] = ""
        elif intent == "change_of_mind":
            facts = [FAQ["other_returns"]]
            std = [reply.OTHER_RETURNS_STD]
            ctx["extra_note"] = "Name the 'Returns page' in their account, in those words."
            ctx["next"] = reply.NEXT["none"] if not c or c.final else ctx["next"]
            ctx["std_next"] = reply.NEXT_STD["none"] if not c or c.final else ctx["std_next"]
        elif intent == "why":
            if c and c.photo_log and any(v != "accept" for v, _r in c.photo_log):
                facts = [f"Photo {i} was not accepted: {', '.join(r).lower() or v.replace('_', ' ')}."
                         for i, (v, r) in enumerate(c.photo_log, 1) if v != "accept"]
                tips = {t for _v, r in c.photo_log for t in r if t in reply.TIPS}
                if tips:
                    facts.append("Tips: " + "; ".join(reply.TIPS[t] for t in tips) + ".")
                if c.final:
                    facts.append(f"The claim's status: {c.final[0].replace('_', ' ')}.")
                    std = facts[:-1] + [status]
            elif c and c.last:
                v, reasons, reason = c.last
                facts = [f"The last decision was '{v.replace('_', ' ')}' because: {reason or ', '.join(reasons) or 'no reason recorded'}."]
                std = [f"My last answer was based on this: {(reason or ', '.join(reasons) or 'no reason recorded').rstrip('.')}."]
                if reasons and v in ("retake", "no_damage", "mismatch"):
                    facts.append("Tips: " + "; ".join(reply.TIPS.get(r, "") for r in reasons if r in reply.TIPS))
                    std.append(facts[-1] + ".")
            else:
                facts = ["Nothing has been decided on a claim in this chat yet."]
        else:  # chitchat
            facts = ["The customer is greeting or thanking."]
            std = ["Happy to help."]
        ctx["facts"] = facts
        if std is not None:
            ctx["std_facts"] = std
        ctx["what"] = "info"
        return "info"

    def _to_person(self, step, skip_rest, ctx):
        """The customer asked for a person: hand over straight away."""
        c = self.claim
        yield step("claim", "skipped", "Customer asked for a person")
        yield from skip_rest("claim", "Not needed: handed to a person")
        ctx["reason"] = "the customer asked for a person"
        if c and c.final and c.final[0] == "accept":
            ctx["what"] = "human_after_accept"
        elif c and c.final and c.final[0] == "not_eligible":
            ctx["extra_note"] = "A colleague will look at the earlier 'not covered' decision too."
            std_note(ctx, "They will look at the earlier 'not covered' decision too.")
        return "human" if not (c and c.final and c.final[0] == "human") else "closed"

    # ------------------------------------------------------------------ claim, policy, eligibility, photo
    def _extract(self, text, step, calls, ctx):
        """Order number and problem from the message: vllm-sr/auto (JSON schema); without it, the order numbers in the
        text (D3 order matching then runs as usual) and D3's scope-check answer on whether it describes a problem.
        Returns (extracted, ms, model or None, note or None)."""
        failed = ctx.setdefault("llm_failed", {})   # so the reply does not try it again in this turn
        if availability.available("llm"):
            yield step("claim", "running", None, LLM_MODEL)
            try:
                d, ms, tok, route = claim.extract(text)
            except Unavailable as e:
                failed["llm"] = e.text
            else:
                calls.append(("llm", ms, tok))
                ctx["extract_route"] = route
                if not router_guarded(route):
                    return d, ms, LLM_MODEL, None
                ctx["router_guard"] = route
                note = "the Router's guard declined the call"   # no extraction: the fallback below, as when it is down
                yield step("claim", "running", None, None)
                known = [n for n in re.findall(r"\b\d{4,6}\b", text) if n in claim.CUSTOMER["orders"]]
                return ({"order": known[0] if known else None,
                         "problem": text if ctx.get("complaint", 0) >= 0.5 else None}, ms, None, note)
        yield step("claim", "running", None, None)
        known = [n for n in re.findall(r"\b\d{4,6}\b", text) if n in claim.CUSTOMER["orders"]]
        problem = text if ctx.get("complaint", 0) >= 0.5 else None
        why = "; ".join(failed.values())
        return ({"order": known[0] if known else None, "problem": problem}, 0, None,
                "no language model" + (f": {why}" if why else ""))

    def _open(self, order_id):
        """Make order_id the current claim (an existing one, or a new one)."""
        if order_id not in self.claims:
            self.claims[order_id] = Claim(claim.ORDERS[order_id])
        self.claim = self.claims[order_id]
        self.queued = [q for q in self.queued if q[0] != order_id]
        return self.claim

    def _other_claim_named(self, text):
        """An order of this customer, other than the current claim, named by product word (one match only)."""
        prods = claim.products_in(text)
        cur = self.claim and self.claim.order["order"]
        hits = [o for o in claim.CUSTOMER["orders"] if claim.ORDERS[o]["product"] in prods and o != cur]
        opened = [o for o in hits if o in self.claims]
        if len(opened) == 1:
            return opened[0]
        return hits[0] if len(hits) == 1 else None

    def _claim_flow(self, text, img, step, skip_rest, calls, ctx):
        c = self.claim
        mine = claim.CUSTOMER["orders"]
        new_problem = False
        # 3. claim: which order, and is there problem text
        if text:
            d, ms, xm, xnote = yield from self._extract(text, step, calls, ctx)
            named = [n for n in re.findall(r"\b\d{4,6}\b", text)]
            if d.get("order") and d["order"] not in named:
                named.insert(0, d["order"])
            own = [n for n in dict.fromkeys(named) if n in mine]
            foreign = [n for n in dict.fromkeys(named) if n not in mine]
            detail = {"problem": d.get("problem"), "extracted_order": d.get("order")}
            if xnote:
                detail["extraction"] = xnote
            how = ""
            other = None if own else self._other_claim_named(text)
            if own:
                if not c or c.order["order"] != own[0]:
                    new = own[0] not in self.claims
                    c = self._open(own[0])
                    how = (" (new claim)" if new else " (back to this claim)") if len(self.claims) > 1 else ""
                for extra in own[1:]:
                    if extra not in [q[0] for q in self.queued] and extra not in self.claims:
                        self.queued.append((extra, text))
                if own[1:]:
                    ctx["extra_note"] = (f"The customer also mentioned order {', '.join(own[1:])}: say you will look at "
                                         f"that one right after this one.")
                    std_note(ctx, f"I will look at order {', '.join(own[1:])} right after this one.")
            elif foreign:
                ctx["not_found"] = foreign[0]
                ctx["what"] = "not_found"
            elif other and (not c or other != c.order["order"]):
                new = other not in self.claims
                c = self._open(other)
                how = " (new claim, named by product)" if new else " (back to this claim)"
            elif c and c.final and self.queued:
                # the earlier message named another order: it is next, with that message as its first description
                nxt, seed = self.queued[0]
                c = self._open(nxt)
                how = " (next order from the earlier message)"
                if seed:
                    c.complaint.append((None, seed))
                    new_problem = True
            # no own order number: D3 matches the text against the customer's orders
            if not own and not how and (not c or c.final or (d.get("problem") and not foreign)):
                self.unmatched.append(text)
                chosen, cands, mdetail, mms, mtok = claim.match_order(" ".join(self.unmatched[-3:]) if not c else text)
                calls.append(("d3", mms, mtok))
                mdetail["ms"] = mms
                detail["order_match"] = mdetail
                ms += mms
                p = dict(cands).get(chosen, 0) if chosen else 0
                if chosen and foreign and not c:
                    # they typed a number that is not theirs: offer a match from their description, never assume it
                    if d.get("problem"):
                        ctx["options"] = f"{claim.ORDERS[chosen]['item']} (order {chosen})"
                elif chosen and (not c or (chosen != c.order["order"] and p >= MATCH_SWITCH)):
                    c = self._open(chosen)
                    how = f" (matched by D3, {p:.2f})"
                elif not c and len(cands) == 2 and cands[0][1] + cands[1][1] >= 0.6:
                    ctx["options"] = " or ".join(f"{claim.ORDERS[k]['item']} (order {k})" for k, _ in cands)
            if c and not c.final and (d.get("problem") or c.stage in ("need_problem", "clarify")) \
                    and not (own and not d.get("problem") and len(text.split()) <= 3):
                asked = self.history[-2]["content"] if c.stage == "clarify" and len(self.history) > 1 else None
                c.complaint.append((asked, text))
                new_problem = True
            if c:
                self.unmatched = []
            detail.update(order=c and c.order["order"], product=c and c.order["product"])
            summary = (f"Order {c.order['order']}: {c.order['item']}{how}" if c else
                       f"Order {ctx['not_found']} is not on this account" if ctx.get("not_found") else
                       "Which order? " + ctx["options"] if ctx.get("options") else "No order yet")
            if xnote:
                summary += f" ({xnote})"
            if ctx.get("extract_route"):
                detail["route"] = ctx["extract_route"]
            models = [m for m in (xm, D3_MODEL if "order_match" in detail else None) if m]
            yield step("claim", "done", summary, " + ".join(models) or None, ms, detail)
        else:
            if c and c.final and self.queued:
                nxt, seed = self.queued[0]
                c = self._open(nxt)
                if seed:
                    c.complaint.append((None, seed))
                    new_problem = True
            yield step("claim", "skipped", f"From an earlier turn: order {c.order['order']}" if c else "No text")

        if not c:
            if img:
                self.pending_image, self.pending_at = img, time.time()
                ctx["photo_note"] = "Say their photo is kept and will be checked once we know the order; do not describe it."
                std_note(ctx, "I have kept your photo and will check it once I know the order.")
            yield from skip_rest("claim", "Waiting for the order")
            return "need_order"
        if ctx.get("what") == "not_found":
            ctx.pop("what")   # a foreign number while a claim is open: mention it, continue with the open claim
            std_note(ctx, f"Order {ctx['not_found']} is not on your account.")
            ctx["extra_note"] = (ctx.get("extra_note", "") + f" Also say order {ctx.pop('not_found')} is not on their "
                                 "account.").strip()
        if img is None and self.pending_image:
            img, self.pending_image = self.pending_image, None   # the photo sent before the order was known
        if c.final:
            yield from skip_rest("claim", f"Decided in an earlier turn: {c.final[0].replace('_', ' ')}")
            ctx["photo_sent"] = img is not None
            return "closed"

        # 4. policy (hard rules, once per claim)
        if c.policy_ok is None:
            ok, outcome, rules = claim.hard_rules(c.order)
            c.policy_ok = ok
            yield step("policy", "done" if ok else "blocked",
                       "All rules passed" if ok else next(r["label"] for r in rules if not r["ok"]), None, 0,
                       {"rules": rules})
            if not ok:
                pol = claim.POLICIES[c.order["product"]]
                ctx["reason"] = next(r["label"] for r in rules if not r["ok"])
                ctx["policy_line"] = (f"Returns within {pol['window_days']} days of delivery" if outcome == "not_eligible"
                                      else "Orders over the review limit are checked by a colleague")
                ctx["policy_ref"] = "window" if outcome == "not_eligible" else "review_limit"
                yield from skip_rest("policy", "Not needed: policy rule failed")
                return outcome
        else:
            yield step("policy", "skipped", "Passed in an earlier turn")

        # 5. eligibility (D3, text): re-run whenever the customer adds to the problem, until the claim is closed
        if new_problem:
            yield step("eligibility", "running", None, D3_MODEL)
            a, ms, tok = eligibility.ask(c.order["product"], c.complaint)
            calls.append(("d3", ms, tok))
            outcome, reason = eligibility.decide(a)
            if outcome == "clarify":
                c.clarified += 1
                if c.clarified > MAX_CLARIFY:
                    outcome, reason = "human", f"{MAX_CLARIFY} clarifying questions did not settle the claim"
            was = c.stage
            yield step("eligibility", "blocked" if outcome == "not_eligible" else "done",
                       f"{outcome.replace('_', ' ').capitalize()}: {reason}"
                       + (" (re-checked after the customer's new message)" if was == "need_photo" else ""),
                       D3_MODEL, ms, eligibility.detail(a))
            ctx["reason"] = reason
            if outcome == "not_eligible":
                pol = claim.POLICIES[c.order["product"]]
                if a["issue"]["choice"] == "no_damage":
                    ctx["policy_line"] = FAQ["other_returns"]
                else:
                    ctx["policy_line"] = "; ".join(pol["not_covered"])
                    ctx["policy_ref"] = "not_covered"
                yield from skip_rest("eligibility", "Not needed: claim not eligible")
                return "not_eligible"
            if outcome == "human":
                yield from skip_rest("eligibility", "Not needed: goes to a person")
                return "human"
            c.stage = "clarify" if outcome == "clarify" else "need_photo"
        elif c.stage in ("need_problem", "clarify"):
            yield step("eligibility", "skipped", "Waiting for " + ("an answer" if c.stage == "clarify" else
                                                                   "a description of the problem"))
        else:
            yield step("eligibility", "skipped", "Eligible in an earlier turn")

        # 6. photo (D3, image)
        if img is not None:
            v, reasons = yield from self._photo(img, step, calls, ctx, early=c.stage != "need_photo")
            kind = ctx.get("photo_kind")
            target = next((o for o, cl in self.claims.items() if cl is not c and not cl.final
                           and cl.order["product"] == kind), None)
            if v == "mismatch" and target:
                # the photo shows the product of another open claim in this chat: judge it for that claim
                c = self._open(target)
                ctx["extra_note"] = f"The photo was for their {c.order['item']} claim (order {target})."
                std_note(ctx, f"I used your photo for the {c.order['item']} claim (order {target}).")
                if c.policy_ok is None or c.stage in ("need_problem", "clarify"):
                    c.photo = None
                    yield from self._photo(img, step, calls, ctx, early=True)
                    return c.stage if c.stage != "need_photo" else "need_photo"
                v, reasons = yield from self._photo(img, step, calls, ctx)
            if c.stage != "need_photo":
                # the claim is not clear yet: keep a usable result, point out a wrong or unusable photo now
                c.photo = (v, reasons) if v in ("accept", "no_damage", "human") else None
                ctx["photo_note"] = (
                    f"The photo they sent does not show the {c.order['item']} ({', '.join(reasons)}); mention it briefly."
                    if v == "mismatch" else
                    f"The photo they sent cannot be used ({', '.join(reasons)}); mention that a new one will be needed."
                    if v == "retake" else
                    "Their photo was received and will be used once the problem is clear; do not describe it.")
                std_note(ctx, f"The photo you sent does not show the {c.order['item']} ({', '.join(reasons).lower()})."
                         if v == "mismatch" else
                         f"The photo you sent cannot be used ({', '.join(reasons).lower()}), so a new one will be needed."
                         if v == "retake" else
                         "I have your photo and will use it once the problem is clear.")
                ctx["reasons"] = []
                return c.stage
            c.photo = None
        elif c.stage == "need_photo" and c.photo:
            v, reasons = c.photo
            c.photo = None
            ctx["reasons"] = reasons
            yield step("image_check", "skipped", "Checked in an earlier turn: " +
                       ("damage clearly visible" if v == "accept" else ", ".join(reasons).lower()))
        else:
            yield step("image_check", "skipped", "Waiting for a photo" if c.stage == "need_photo" else
                       "Not needed yet: the problem is not clear")
            return c.stage
        if v != "accept" and c.photos >= MAX_PHOTOS:
            ctx["reason"] = f"{c.photos} photos could not confirm the damage"
            return "human"
        return v

    def _photo(self, img, step, calls, ctx, early=False):
        """D3 photo check of one decoded upload; yields the image_check step, returns (verdict, reasons)."""
        c = self.claim
        yield step("image_check", "running", None, D3_MODEL)
        v, reasons, det, ms, tok = photo.check(c.order["product"], img)
        c.photos += 1
        c.photo_checked = True
        c.photo_log.append((v, list(reasons)))
        calls.append(("d3", ms, tok))
        det["image"] = "upload"
        ctx["photo_kind"] = next((q["choice"] for q in det["questions"] if q["key"] == "kind"), None)
        note = " (text asking for approval ignored)" if det["note_flagged"] else ""
        when = "Checked before the claim was clear: " if early else ""
        yield step("image_check", "done" if v == "accept" else "blocked",
                   when + ("Damage clearly visible" if v == "accept" else ", ".join(reasons)) + note,
                   D3_MODEL, ms, det)
        ctx["reasons"] = reasons
        return v, reasons


def std_note(ctx, text):
    """A plain sentence for the standard reply (no LLM), next to the instruction the LLM gets."""
    ctx.setdefault("std_notes", []).append(text)


def failed_steps(nodes, reason):
    """Steps after a model failed mid-turn: the running node (else the first not reached) fails with one plain line,
    the nodes not reached are skipped."""
    status = {n: nodes.get(n, "waiting") for n in NODES}
    failed = {n for n in NODES if status[n] == "running"} or {next((n for n in NODES if status[n] == "waiting"), None)}
    return [{"type": "step", "node": n, "status": "failed" if n in failed else "skipped",
             "summary": reason if n in failed else "Not run", "model": None, "ms": None, "detail": None}
            for n in NODES if status[n] in ("waiting", "running")]


def policy_ref(verdict, ctx, c=None):
    """Which part of the product policy the reply rests on, for the UI's policy popover."""
    if ctx.get("policy_ref"):
        return ctx["policy_ref"]
    if verdict == "closed" and c and c.final:
        return {"accept": "covered", "not_eligible": "not_covered"}.get(c.final[0])
    return {"accept": "covered", "need_photo": "photo", "retake": "photo", "mismatch": "photo", "no_damage": "photo",
            "clarify": "covered"}.get(verdict)


def router_guarded(route):
    """The hosted Router answered with its own guard decision (no model ran)."""
    return bool(route) and (route.get("decision") == "guard" or not route.get("model"))


def route_summary(route):
    """One line for the router step: what vllm-sr/auto picked for the reply."""
    effort = (route.get("effort") or "").removeprefix("effort_")
    reasoning = route.get("reasoning")
    words = ("reasoning off" if reasoning == "off" else
             f"reasoning on ({effort})" if reasoning == "on" and effort and effort != "off" else
             "reasoning on" if reasoning == "on" else "reasoning not reported")
    return f"{(route.get('decision') or 'unknown').capitalize()} decision, {words}"


END = {
    "approved": ("Return approved", "Your free return label will be emailed within 10 minutes. The replacement or refund "
                 "follows once the courier collects the item."),
    "approved_and_handed_over": ("Return approved", "Your return stays approved, and a colleague will also email you "
                                 "within one working day."),
    "handed_over": ("Passed to a colleague", "A colleague will review your case and reply by email within one working "
                    "day."),
    "not_covered": ("Not covered", "This return is not covered by the policy for this item. If you think this is wrong, "
                    "a colleague can review the decision."),
    "blocked": ("Closed after repeated bypass attempts", "This chat was closed after repeated attempts to bypass our "
                "review. Start a new chat if you need help with an order."),
    "off_topic": ("Closed: not about orders", "This chat is only for orders, returns and refunds, so it has been "
                  "closed. Start a new chat if you need help with an order."),
}


def ending(reason, c):
    title, text = END[reason]
    return {"reason": reason, "title": title, "text": text,
            "order": c and c.order["order"] if reason not in ("blocked", "off_topic") else None,
            "actions": ["review", "new_chat"] if reason == "not_covered" else ["new_chat"]}
