# API contract (server :8602, draft 2026-10-26)

The UI is built against `ui/mock-server.mjs` first; the real server (Phase 2) implements the same contract.

## GET /api/samples
The 19 preloaded conversations for the left panel, in 4 groups (valid 4, invalid 7, malicious 5, escalation 3) (from data/scenarios.json), plus every product policy.
```json
{"groups": [
  {"key": "valid", "title": "Valid returns", "scenarios": [
     {"id": "v-phone", "title": "Cracked phone screen", "subtitle": "Clear photo, approved", "order": "4411",
      "product": "phone", "expected": "accept", "uses_reasoning": false,
      "turns": [{"text": "Hi, order 4411. The phone screen was cracked when I opened the box.", "expect": "need_photo",
                 "route": {"model": "qwen/qwen3.8-flash-next", "decision": "fast", "reasoning": "off", "effort": "effort_off"}},
                {"image": "samples/phone/phone_3_clear.jpg", "expect": "accept", "route": {"...": "..."}}]}]},
  {"key": "invalid", "title": "Invalid returns", "scenarios": ["..."]},
  {"key": "malicious", "title": "Malicious intent", "scenarios": ["..."]},
  {"key": "escalation", "title": "Upset or urgent customers", "scenarios": ["..."]}],
 "policies": {"phone": {"name": "Smartphone", "window_days": 10, "human_review_over": null,
                        "covered": "...", "not_covered": ["..."], "photo": "..."}}}
```
`expected` = the scenario's own `expected` if set, else the last turn's `expect` that is not `closed`. `order` and `product` may be null (jailbreak scenario).
Each turn's `route` = what the Router (`vllm-sr/auto`) picked for that turn's reply in the last bench run
(`data/scenario_routes.json`): `{model, decision, reasoning, effort}`, or null for a fixed reply (blocked, off-topic) or
when no bench run is stored. `uses_reasoning` = a reply of the scenario ran with reasoning on in that run. The
`escalation` group (angry customer who wants a manager, legal threat, two-part policy question) shows the Router raising
the reasoning effort.

## GET /api/customer
The one mock customer, logged in: `{"name": "Emily Carter", "email": "emily.c****@example.com", "greeting": "Hi Emily! ...",
"orders": [{"order": "4411", "product": "phone", "product_name": "Smartphone", "item": "...", "price": 499,
"delivered_days_ago": 2, "window_days": 10}, ...]}` (most recent first).
- Every new chat starts with `greeting` as the first assistant message (shown by the UI; the server's chat history
  starts with it too). No model call.
- Clicking an order card sends a normal visible user message `Order 4411` (so the Claim step runs as usual).
- Without an order number, the `claim` step runs D3 order matching: its detail then has `order_match`, a D3 detail
  like `eligibility` with one choice question `order` whose `probs` keys are order ids (or `unclear`) and a `labels`
  map id -> item name; `threshold` 0.75. Matched: summary "Order 6230: ... (matched by D3, 0.99)". Two close
  candidates: verdict `need_order`, summary "Which order? A (order x) or B (order y)" and the reply asks.

## GET /api/gallery
Images in the sample folder for the 📁 picker: `[{"path": "samples/phone/phone_1_clear.jpg", "product": "phone", "case": "damaged_clear"}]`.
Images are served at `/<path>`.

## POST /api/chat → `text/event-stream`
Body: `{"session": "uuid", "text": "...?", "image": "samples/... or data:image/jpeg;base64,...?", "has_turns": false}`
(text, image or both; `session` must be a UUID, see "Many visitors" below). Header `X-Visitor-Id`: the tab's visitor id;
without a key for that visitor the answer is 401 `{"code": "no_key", ...}` and nothing runs (see "API key" below).

Events (each `data:` is JSON with a `type`):

| type | fields | meaning |
|---|---|---|
| `step` | `node`, `status`, `model?`, `ms?`, `summary?`, `detail?` | a flow node changed state |
| `token` | `text` | streamed reply text |
| `message` | `role: "assistant"`, `text`, `verdict`, `order`, `policy_ref` | final reply of this turn |
| `stats` | stats object (below) | top bar update |
| `done` | `turn` | end of turn; `turn` = turn id, for replaying its flow |
| `error` | `message` | |
| `key_error` | `code`, `message` | the visitor's API key was refused during the turn; no `message` follows (see "API key") |

`node` (in flow order): `guardrail`, `router`, `claim`, `policy`, `eligibility`, `image_check`, `decision`, `reply`.
`status`: `waiting | running | done | skipped | blocked | failed`. Nodes not used by this turn are sent as `skipped`
(e.g. a text-only turn skips `image_check`; a not-eligible claim skips `image_check` for the rest of the session).

`detail` by node:
- `guardrail` (model `vllm-sr/vela3-flash + vllm-sr/d3-flash`, or `vllm-sr/d3-flash` when Vela was unavailable; Vela and
  the D3 scope check run in parallel, `ms` = the slower of the two):
  ```json
  {"attack": 0.02, "harm": 0.01, "vela_flagged": false, "vela_ms": 48, "thresholds": {"attack": 0.5, "harm": 0.5},
   "scope_check": {"...": "D3 detail, see Scope and reply checks"}, "complaint": 0.96,
   "model": "vllm-sr/vela3-flash + vllm-sr/d3-flash", "flagged": false}
  ```
  `attack` and `harm` are null (and `vela_ms` null) when Vela was unavailable; the summary then ends "; Vela
  unavailable, D3 checked alone".
- `router`: see "Router step (vllm-sr/auto)" below.
- `claim`: `{"order": "4411", "product": "phone", "issue": "crack", "part": "screen", "route": {...}}`; step `model`
  `vllm-sr/auto` (`vllm-sr/auto + vllm-sr/d3-flash` when order matching ran); `route` = the Router's choice for the
  extraction call (same shape as the router detail, below).
- `policy`: `{"rules": [{"label": "Delivered 2 days ago, window 10 days", "ok": true}, {"label": "Price under review limit", "ok": true}]}`
- `eligibility` and `image_check`: D3 questions
  ```json
  {"model": "vllm-sr/d3-flash", "questions": [
     {"key": "when", "label": "When did it start", "type": "choice", "choice": "arrival",
      "probs": {"arrival": 0.97, "after": 0.02, "not_said": 0.01}},
     {"key": "blurry", "label": "Blurry", "type": "yesno", "prob": 0.86, "threshold": 0.6, "borderline_from": 0.45,
      "status": "bad"}],
   "escalated": []}
  ```
  `status` per yes/no: `ok | borderline | bad`. Every D3 check runs on `vllm-sr/d3-flash`; `escalated` is empty.
- `decision`: `{"verdict": "retake", "reasons": ["Blurry"], "rule": "any quality check bad -> retake"}`
- `reply`: step `model` `vllm-sr/auto`; detail `{"model": "qwen/qwen3.8-flash-next", "tokens": 112, "route": {...},
  "reply_check": {"model": "vllm-sr/d3-flash", ...}}` (`model` = the model the Router picked; `tokens` = completion
  tokens, reasoning included). A standard reply (language model unavailable): `model` null, summary "Standard reply:
  language model unavailable", detail `{"standard": true, "reason": "...", "model": null}`.

### Router step (vllm-sr/auto, 2026-11-05)
Every language-model call goes to `vllm-sr/auto`; the vllm-sr Router picks the model and the reasoning effort per
request. The Router raises the reasoning effort for upset, urgent or complex turns; normal turns run with reasoning off.
It is not a switch to a larger model: the routed model can differ per call (for example a 27B or a flash model), and
the UI shows which one answered.
- The `router` step stays `waiting` while the claim, policy, eligibility and photo steps run. It is sent `running`
  (model `vllm-sr/auto`, summary "vllm-sr/auto picks the model and reasoning for the reply") right before the reply
  call, and `done` after it, so it arrives after later nodes: the UI updates a node whenever a later event for it comes.
- `done`: `model` = the model the Router picked (e.g. `qwen/qwen3.8-27b`, `qwen/qwen3.8-flash-next`), `ms` = routing
  latency (may be null), summary "Fast decision, reasoning off" / "Standard decision, reasoning on (medium)" / "Hard
  decision, reasoning on (high)", detail:
  ```json
  {"model": "qwen/qwen3.8-27b", "decision": "hard", "reasoning": "on", "effort": "effort_high", "signals": "task:chat",
   "ranking": null, "routing_ms": 21.4, "call": "reply",
   "extraction": {"model": "qwen/qwen3.8-flash-next", "decision": "fast", "reasoning": "off", "effort": "effort_off", "...": "..."}}
  ```
  `decision`: `fast | standard | hard | agentic | ...`; `reasoning`: `on | off | null`; `effort`: `effort_off |
  effort_medium | effort_high | null` (null when the Router did not report it). `extraction` is the same shape for
  the claim-extraction call, or null.
- `skipped` with a summary: "Not called: fixed reply, no language model" (blocked, off-topic, unreadable upload) or
  "Language model unavailable: standard reply". `failed` / `skipped` "No answer from vllm-sr/auto: standard reply"
  when the language model did not answer.

`message.order` (null until an order number is known): `{"order", "product", "item", "price", "delivered_days_ago"}`.
`message.policy_ref`: which part of that product's policy the reply rests on, for the policy popover:
`covered | not_covered | window | review_limit | photo | null`.

Verdicts: `accept | retake | mismatch | no_damage | not_eligible | human | clarify | blocked | need_photo | need_order |
need_problem | closed` (closed = the claim was decided in an earlier turn).

## GET /api/stats?session=<id>
Stats of ONE chat session (the top bar is per chat; a new chat or a newly played sample starts from zero). The
`stats` SSE event carries the same object for the session of that request.
```json
{"session": "s-123", "turns": 3, "d3_calls": 2, "d3_ms_median": 290,
 "llm_turns": 2, "routes": {"fast": 1, "hard": 1}, "reasoning_turns": 1,
 "blocked": 0, "off_topic": 0, "image_checks_avoided": 0,
 "model_ms": {"decision": 640, "llm": 2210, "total": 2850}, "llm_tokens": 556, "d3_tokens": 9100}
```
`llm_turns` = turns whose reply the language model wrote; `routes` = those turns by Router decision;
`reasoning_turns` = those of them with reasoning on. `model_ms` = this chat's model time: `decision` (every D3 flash
and Vela 3 flash call), `llm` (every `vllm-sr/auto` call) and `total` (all model calls); null before the first turn.
`llm_tokens` = completion tokens the language model wrote, reasoning tokens included (null before the first turn);
`d3_tokens` = what D3 and Vela read, mostly image tokens. `image_checks_avoided` is 0 or 1 per chat. There is no
`chats` field any more. (2026-11-05: `share` and `model_ms.d3` were removed.)
(2026-11-04: `model_seconds`, `tokens`, `usd_per_million_chats`, `gpu_usd_per_hour` and `baseline`, the comparison
with a single large model and the cost figure, were removed. Step `tokens` for the language model are completion tokens.)

## GET /api/turn/{turn}
The stored `step` events of a past turn, so clicking an earlier message replays its flow on the right:
`{"turn": "t3", "steps": [<step events>]}`.

## Scope and reply checks (2026-10-26)
- `guardrail` detail now also has `scope_check`: a D3 detail (like `eligibility`) with a choice `scope`
  (`returns | off_topic | instructions`, `labels` map for display names) and yes/no `command` (commands or code) and
  `complaint` (real product problem). Blocked when Vela flags a prompt attack (and D3 is not sure it is a normal returns
  message), scope = instructions or command >= 0.5,
  unless complaint >= 0.5 (then "Ignored ...; real complaint handled"). Off-topic -> verdict `off_topic`.
- New verdict `off_topic`. Blocked and off-topic turns get a FIXED reply: `reply` step is `skipped` with summary
  "Fixed reply, no model call", and `claim` .. `image_check` are skipped.
- `reply` detail may have `reply_check`: `{"model", "claims_approval", "threshold": 0.5, "replaced", "ms",
  "original"?}`; when the reply claimed an approval that was not given, it is replaced by a fixed fallback and the
  summary says "Replaced: the reply claimed an approval (0.83)". Replies are now checked before they are streamed.
- Stats: new `off_topic` count per chat (next to `blocked`).
- On blocked / off-topic turns the `router` step is `skipped` with summary "Not called: fixed reply, no language
  model". Stats `llm_turns` counts only turns where the language model wrote the reply.

## Review fixes (2026-10-26, after the code review and exploratory test)
- Claims are per order: a chat can hold several. Switching orders or naming another of the customer's orders after a
  decision starts a new claim; `message.order` follows the current claim. Only the customer's own orders are accepted;
  any other number gets "not on your account" (no details of other orders are revealed).
- New verdicts: `info` (a question answered from the FAQ/policy: refund timing, replacement, policy, change of mind,
  "why?", greetings; the claim is unchanged), `bad_file` (upload not a readable image; fixed reply, nothing changes),
  `error` (a model or Router failure; an `error` event is followed by a `message` with verdict `error` so the turn
  ends cleanly; the UI should mark running/waiting nodes as `failed`). `closed` replies now depend on how the claim
  ended (approved / with a person / not covered).
- `guardrail` scope check has a 4th question `intent` (choice: claim, refund_info, policy_info, human,
  change_of_mind, why, chitchat; `labels`). "Wants a person" hands over to a person straight away.
- `reply` summary says "checked by D3" only when the reply check ran (it does not run on approvals). The reply step's
  `ms` includes the reply check.
- `image_check` "Checked in an earlier turn" only when the photo really came in an earlier turn; a photo in the same
  turn as the text is checked after eligibility and decides the claim directly. A photo sent before the order was
  known is kept and checked once the order is known.
- `order_match` and `scope_check` details carry their own `ms` (D3 time of that part).
- Stats: `image_checks_avoided` is now a count of claims closed before any photo check.

## Chat ending (2026-10-26)
- `message.ended` is null while the chat is open, else `{"reason", "title", "text", "order", "actions"}`:
  `approved` (accepted, no other order waiting), `approved_and_handed_over`, `handed_over` (any hand-over to a person),
  `not_covered` (actions `["review", "new_chat"]`), `blocked` (2nd blocked message in the chat), `off_topic` (3rd
  off-topic message in a row). Other reasons have actions `["new_chat"]`.
- After the end, POST /api/chat answers with verdict `ended` (all steps `skipped` "The chat has ended", no stats, no
  state change). The only exception: `{"session", "action": "review"}` after `not_covered` hands the case to a person
  (verdict `human`, then `ended.reason` = `handed_over`).
- /api/samples: each turn has `route` (what the Router picked for that reply in the last bench run, or null for a
  fixed reply) and each scenario `uses_reasoning` (see GET /api/samples).
- Samples changed: "Dropped by the customer" is one turn (the chat ends on not covered); "Three unusable photos" has
  an angry customer turn (verdict `info`).

## Agreed with the UI build (2026-10-26)
- Image-only turns send `guardrail`, `claim`, `policy`, `eligibility` as `skipped` with a summary such as
  "Eligible in an earlier turn".
- Yes/no questions where yes is good ("Shows a phone"): `borderline_from` above `threshold`; colour always comes from `status`.
- Each `escalated` entry carries its own `status` (ok | borderline | bad).
- `image_check` detail also carries `image` (the path or turn image id D3 judged).
- ~~POST /api/reset clears sessions and stats~~ (removed 2026-11-04: it wiped every visitor's chat; the UI never used it).
- ~~Stats `usd_per_million_chats` uses env GPU_USD_PER_HOUR~~ (removed 2026-11-04 with the cost figure).

## Many visitors (2026-11-04)
- `session` must be a UUID (`crypto.randomUUID()`), else 400; there is no shared "default" chat any more.
  `GET /api/stats?session=` also wants a UUID.
- `has_turns` (bool): the page already had a turn in this chat. A session the server does not know (restarted, or the
  chat was dropped) is a new chat when `has_turns` is false; when true the stream is one event
  `{"type": "expired", "message": "This chat expired, starting a new one."}` and the page starts a new chat.
- Chats live in memory: dropped 30 min after their last message, at most 300 (least recently used dropped first; a
  chat mid-turn is never dropped). Dropping removes its stats and stored turns (`/api/turn/{id}` then 404s). A photo
  kept for a chat without an order is dropped after 10 min.
- Limits, answered without any model call by a `message` with verdict `rate_limited` ("You are sending messages
  faster than this demo allows. ..."; the `guardrail` step is `blocked`, the rest `skipped`; no `done`, no stats):
  10 messages per minute per chat; per visitor IP (first `X-Forwarded-For` hop, else the peer) 30 messages and 5 new
  chats per minute. Requests from loopback without `X-Forwarded-For` (the presenter's machine, an SSH tunnel) have no
  per-IP limits.
- Capacity: at most 12 model calls at once per API (D3 and Vela on `/v1/systemone`, chat completions; env
  `MODEL_SLOTS`), others wait. A wait over 15 s (`MODEL_WAIT_S`), a 429/503 that repeats after one retry (Retry-After
  honoured, at most 5 s), or more than 50 chats mid-turn ends the turn with verdict `busy`, text "The service is busy,
  please try again.": the node that was running is `failed` with summary "Service busy: <why>" and
  `detail.capacity`, the nodes not reached are `skipped`; no `done`, no stats. At most 4 uploads are decoded at once
  (JPEGs are decoded at reduced scale). The 25 MB body limit is checked before the body is read (413).
- New verdicts: `busy`, `rate_limited`.
- `GET /api/config`: `{"play_all": true, "landing_url": "", "health": {...}}` (env `PLAY_ALL`, default 1; 0 hides Play all,
  as on the hosted Space; env `LANDING_URL`, default empty, the "All demos" link, hidden when empty).
- `GET /healthz`: `{"ok": true}`, no model calls (liveness). `GET /health`: see "Model availability" below.
- `GET /api/metrics`: `{"active_chats", "chats_in_memory", "busy", "rate_limited", "expired", "turns_last_hour",
  "queued_calls": {...}, "throttled_429_503", "memory_mb"}` (counts since start).
- The server log has one line per turn: `turn <first 8 of chat id> verdict=... ms=... models=... 429s=...` (no
  message text).

## Model availability (2026-11-05, hosted vllm-sr APIs)
- The server tracks three models (`app/availability.py`): `d3` (`vllm-sr/d3-flash` on `/v1/systemone`), `guard`
  (`vllm-sr/vela3-flash`, the Vela guardrail) and `llm` (`vllm-sr/auto`). A background probe sends one
  `GET /v1/models` every `HEALTH_INTERVAL` s (no model call): a model the API does not list is down. Every real call
  feeds a circuit breaker: repeated failures (connection error, timeout, 5xx, an unreadable answer) mark the model down
  for `BREAKER_S` s, calls to it are then skipped at once; one success marks it up. 429s and full slots are capacity
  (`busy`), not failures. Per-call timeouts: env `D3_TIMEOUT_S`, `GUARD_TIMEOUT_S`, `LLM_TIMEOUT_S`.
- Pre-flight: when D3 is down, a turn answers at once with verdict `unavailable` and the text "The assistant is
  unavailable right now. Please try again in a minute." (`guardrail` `failed` "D3 unavailable: nothing can be decided
  without it, no model call", the rest `skipped`; no `done`, no stats, the chat is unchanged).
- Fallbacks (the language model only words a decision D3 already made):
  - Vela down: D3's scope check screens messages alone; guardrail `model` `vllm-sr/d3-flash`, `attack` / `harm` null,
    summary ends "; Vela unavailable, D3 checked alone".
  - language model down: the `router` step is `skipped` "Language model unavailable: standard reply"; the claim is
    taken from the order numbers in the text and D3 order matching, and the reply is a fixed template per verdict:
    `reply` step `done`, `model` null, summary "Standard reply: language model unavailable", detail
    `{"standard": true, "reason": "...", "model": null}`. The same standard reply is used when the D3 reply check is
    down ("Standard reply: the D3 reply check is unavailable"): unchecked words are not sent.
- Mid-turn failure of D3 (or anything with no fallback): the running step is `failed` with one plain line ("D3
  unavailable", "D3 timed out"), the steps not reached are `skipped` ("Not run"), then the `message` with verdict
  `unavailable` and the text above; no `done`, no stats, and the chat is put back as it was before the turn (send the
  message again). A bug in the flow: verdict `error`, the failed step says "Something went wrong on our side". No event
  or API response carries exception text or a model API's answer.
- `GET /api/health` (always 200, for the page) and `GET /health` (200 when every model is up, else 503, for scripts):
  ```json
  {"ok": false, "status": "guard_down", "message": "Vela guardrail unavailable: D3 checks messages alone",
   "d3": true, "guard": false, "llm": true,
   "models": {"guard": {"state": "down", "reason": "unavailable", "retry_in_s": 21, "checked_s_ago": 9}, "...": {}},
   "probe_interval_s": 60}
  ```
  `status` / `message`: `ok` "All systems working", `checking` "Checking systems" (before the first probe),
  `guard_down` "Vela guardrail unavailable: D3 checks messages alone", `standard` "Standard replies: language model
  unavailable", `unavailable` "Assistant unavailable". `state`: `up | down | unknown`. `GET /api/config` also carries
  this object as `health`.
- `/api/metrics` adds `unavailable` and `errors` counts. New verdict: `unavailable`. The `error` SSE event is no longer
  sent.
- Hosted APIs: `VLLM_SR_API_BASE` (default https://app.vllm-sr.ai) with the visitor's own key as a bearer token
  (see "API key"; `VLLM_SR_API_KEY` is only an optional fallback). The health probe is the public `GET /v1/models`:
  no key needed, so the badge works before any key is entered; `D3_MODEL`, `GUARD_MODEL` and `LLM_MODEL` name the models (defaults `vllm-sr/d3-flash`, `vllm-sr/vela3-flash`,
  `vllm-sr/auto`).

## API key (2026-11-05, entered in the browser)

Every visitor brings their own vllm-sr key; the server needs none. Shared with the landing page and the receipt demo.

- Visitor: each request carries `X-Visitor-Id`, a UUID the page keeps in `sessionStorage` (one per tab). The key is
  kept per visitor in process memory only (`app/keys.py`): never on disk, in logs, metrics, events or error messages.
  It is dropped after 4 hours without use (`KEY_IDLE_S`), on `DELETE /api/key`, and when it fails during a turn.
- Validation (both endpoints below): the key is trimmed; it must start with `sk-vsr-` (an `sk-pt-` key works for chat
  only, so it is refused as `wrong_kind`). Then one tiny System One call (`vllm-sr/d3-flash`, state "ok", one `noul`
  question) and one tiny System Two call (`vllm-sr/auto`, "Say OK", `max_tokens` 1); both must succeed. A key that
  passed is not checked again for 10 minutes (only its hash is kept for that). At most 10 checks a minute per visitor
  and per IP (`KEY_CHECKS_PER_MIN`): more answer 429 `rate_limited`.
- Codes (`message` is a plain sentence for the page):

  | code | HTTP | when |
  |---|---|---|
  | `format` | 400 | not shaped like an `sk-vsr-` key |
  | `wrong_kind` | 400 | an `sk-pt-` key, or 403 `model_not_allowed` |
  | `invalid` | 401 | 401 from the API (`missing_api_key`, `invalid_api_key`) |
  | `disabled` | 403 | 403 `key_disabled` or `access_denied` |
  | `quota` | 429 | 429 `quota_exceeded` ("It resets at 00:00 UTC.") |
  | `rate_limited` | 429 | any other 429 (with the Retry-After seconds), or too many checks here |
  | `unreachable` | 502 | network error, timeout or 5xx |
  | `error` | 502 | anything else |
  | `no_key` | 401 | `POST /api/chat` from a visitor without a key |

Endpoints:

- `POST /api/key/check` `{"key"}` -> 200 `{"ok": true}` or `{"ok": false, "code", "message"}`. Validates only, stores
  nothing. CORS for this endpoint only: the origin of `LANDING_URL`, `http://localhost:8630` and
  `http://127.0.0.1:8630` (OPTIONS preflight answered with 204).
- `POST /api/key` `{"key"}` + `X-Visitor-Id` -> validates as above, then keeps the key for this visitor. Same answers.
- `GET /api/key` + `X-Visitor-Id` -> `{"set": bool, "server_key": bool, "failed": code | null}`. Never the key or any
  part of it. `server_key`: the server has the optional env key, so visitors without a key can still chat. `failed`:
  why this visitor's key was dropped during a turn. `GET /api/config` carries the same object as `key`.
- `DELETE /api/key` + `X-Visitor-Id` -> forgets it.

During a turn, a call refused because of the key (401, 403 `key_disabled` / `access_denied` / `model_not_allowed`, 429
`quota_exceeded`) stops the turn: the steps not finished are sent `failed` ("Stopped: the API key was refused") or
`skipped`, then `{"type": "key_error", "code", "message"}`; no reply, no template, the chat stays as it was before
the turn. The key is dropped (the next action asks for one; `GET /api/key` shows `failed`). It does not count against
the model's circuit breaker. Other 429s keep the busy reply; 429 cooldowns are per key.

The page's side: the key lives in `sessionStorage` (never `localStorage`, cookies or URLs). The landing page hands it
over as a URL fragment, `#key=<key>` (never sent to a server); the page removes it from the address bar before the
first render, saves it and sends `POST /api/key`. On a 401 `no_key` (server restarted, key expired there) the page
sends the stored key again once and retries the turn.

`VLLM_SR_API_KEY` is optional and off by default: when set, visitors without a key of their own use it. Anything shaped
like a key in the server's output is printed as `sk-***` (a redaction guard on stdout and stderr).

## Fixes v3 display fields (2026-11-06)
The flow from the eval branch `fixes-2` (wear check, mug / wine-glass identity check, vague requests, "none of these
orders", upset customers handed to a person). Extra fields for the UI, display only:
- `image_check` detail: `rule`, the photo rule that decided: `not_photo | other_product | quality | not_product |
  lookalike | accept | worn | unsure_item | damage_band | hidden | damage_unclear | no_damage`; `worn_threshold` (0.5)
  and `damage_band` (0.6). New questions: `condition` (choice `new_damaged | worn | repaired | intact`, with `labels`,
  `threshold` and `worn_or_repaired` = P(worn) + P(repaired); a person looks from the threshold) and, for a mug or a
  wine glass, `strict_kind` (yes/no, "Really a mug, not a look-alike": below 0.15 the wrong item, 0.15-0.3 a person).
  `blurry` is now "Blurry or too dark" (threshold 0.35); `note` is one merged text-in-photo question.
- `guardrail` detail: `review: true` when Vela flagged harm (no attack) on a message D3 reads as about an order: the
  turn is handed to a person with a fixed reply (`reply` skipped), verdict `human`. It is not a block.
- `claim` detail: `order_match.no_match` (D3 picked "None of these orders"); `order_fit` `{"order", "fits": false,
  "said"}` when a product word named an order but the colour, storage, size or volume given does not fit it (code).
- `decision` detail: `case` `review | vague | no_match` with a matching `rule` text.
- `message.ended.kind`: `"review"` on an upset-customer hand-over (`reason` stays `handed_over`, own title and text).
- Stats: `to_person` (turns with verdict `human`) and `reviews` (of those, upset-customer hand-overs).
