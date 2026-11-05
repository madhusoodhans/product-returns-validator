# UI notes

React 19 + TypeScript + Vite 5, CSS modules, Arc components (uiarc.dev) in `ui/src/components/arc`, lucide-react, motion.
React 19, Vite 5 and Arc components. The contract is `docs/API.md`; the UI is built against `ui/mock-server.mjs`.

## Run

```bash
cd ui
npm install                 # node_modules was copied from the receipt UI; install only adds what is missing
npm run mock                # mock API on 127.0.0.1:8612 (MOCK_FAST=1 = 10x faster, MOCK_SLOW=3 = 3x slower)
npm run dev                 # Vite on 127.0.0.1:5182, proxies /api, /samples, /catalog to :8612
API=live npm run dev        # proxy to the real server on :8602 instead (API_TARGET=<url> for any other)
npm run build               # tsc --noEmit + vite build into ../web (the real server serves it on :8602)
```

The mock also serves the built page from `../web`, so `npm run build && npm run mock` and opening
http://127.0.0.1:8612/ checks the production bundle without Vite.

Add Arc components with `node scripts/arc-add.mjs <id> ...` (the shadcn CLI needs Node 20; this machine has 18). It
prints the npm deps to install.

## Mock server (`ui/mock-server.mjs`, plain Node, no deps)

- `GET /api/customer` serves `data/customer.json` (name, email, greeting) with its order ids expanded from `data/orders.json`
  (`order, product, item, price, delivered_days_ago`) plus `product_name` and `window_days` from `data/policies.json`, most recent first.
- Order matching: a free-typed text turn with no four-digit number while the session has no order runs a D3 order match on the
  claim step (`detail.order_match`, choice question `order` with `labels`, `threshold` 0.75, model `vllm-sr/auto + vllm-sr/d3-flash`).
  Product words (laptop, hinge, mug, wine, sole, headphones, lamp, bottle, ...) pick the candidate orders; one candidate
  matches at ~0.97; two or more give ~0.52 / ~0.42, verdict `need_order` and a "Which order is it: A (order x) or B (order y)?"
  reply; the next turn narrows the pending candidates by item words ("the 14 inch one" -> 5120).
- `GET /api/samples` reads `data/scenarios.json` (4 groups, 19 conversations, 5 of them malicious, 3 upset or urgent, every turn with its `expect`) and returns
  `groups` plus all of `data/policies.json` as `policies`; `product` comes from `data/orders.json`, `expected` = last turn's `expect`.
- Each scenario text turn has a hidden script in `HINTS` (intent, issue, part, effort / hard, injection) so the flow lands on the
  expected verdicts; checked with a runner: all 10 scenarios match on every turn. Free typing is classified by keywords
  (order number, "dropped", "knocked", "no sound", "ignore all previous", "system override", ...); a follow-up such as
  "it was like that when it arrived" completes the earlier claim.
- Image case comes from the file name suffix (`_clear`, `_blurry`, `_dark`, `_glare`, `_cut_off`, `_intact`, `_hand`, `_note`,
  `_finger`, `_note_screen`); a photo from another product's folder than the order's product counts as the wrong product.
  Uploads (data URLs) are treated as clear photos.
- `message` carries `order` (`{order, product, item, price, delivered_days_ago}` or null) and `policy_ref`:
  accept, no_damage, clarify -> `covered`; need_photo, retake, mismatch -> `photo`; not_eligible -> `window` or `not_covered`;
  human -> `review_limit` (price) or `not_covered` (not visible, e.g. no sound); blocked and need_order -> null.
- Guardrail (docs/API.md "Scope and reply checks"): model `vllm-sr/vela3-flash + vllm-sr/d3-flash`, detail `attack`, `harm`,
  `vela_flagged`, `vela_ms`, `thresholds` (0.5 each), `flagged`, `complaint` and `scope_check` (D3: choice `scope` returns | off_topic | instructions with `labels`, yes/no `command`, `complaint`).
  A prompt attack, scope instructions or command >= 0.5 without a real complaint -> `blocked`; scope off_topic -> verdict `off_topic`.
  `MOCK_HEALTH=guard_down` sends `attack` / `harm` null and the summary "; Vela unavailable, D3 checked alone".
  Both: guardrail `blocked`, router skipped "Not called: fixed reply, no language model", claim .. image_check skipped ("Blocked by the guardrail" / "Off-topic"), reply
  `skipped` "Fixed reply, no model call", then a fixed reply streamed and `message`. Keywords: "rm -rf", "drop table", "&&"
  (command); "already approved", "translate", "tell me my refund is approved" (instructions); "world cup", "weather" (off-topic).
- Reply: every worded reply carries `reply_check` (`claims_approval`, `threshold` 0.5, `replaced`, `ms`). The injection sample's
  first turn ("Injection in text and photo") is scripted to be replaced: `replaced: true`, `original` text, summary
  "Replaced: the reply claimed an approval (0.83)". `image_check` starts with `real_photo` (yes is good, threshold 0.3, band to 0.5).
- Router (docs/API.md "Router step"): `router` stays waiting until the reply, then `running` (model `vllm-sr/auto`) and,
  after the reply call, `done` with the picked model, routing ms and detail `{model, decision, reasoning, effort, signals,
  ranking, routing_ms, call, extraction}`. The mock routes by the hidden script: `effort: "high"` (or `hard`) -> hard,
  reasoning on (high), `qwen/qwen3.8-27b`; `effort: "medium"` -> standard, reasoning on (medium); else fast, reasoning off,
  `qwen/qwen3.8-flash-next`. `MOCK_HEALTH=standard` gives standard replies (router skipped).
- Flow timings: guardrail ~60 ms (Vela and the D3 scope check in parallel), claim ~600 ms, policy ~3 ms, eligibility D3 ~50 ms,
  photo D3 ~300 ms, router ~20 ms, reply streamed (longer with reasoning on).
- Session rules: a not-eligible, human or accepted claim closes that claim; later turns get verdict `closed`
  (with the closing claim's `policy_ref`, reply worded by how it ended) and skip the checks. Naming another of the customer's
  orders starts a new claim on it, so `message.order` changes (a number not on the account is ignored).
- Review fixes (docs/API.md "Review fixes"): the scope check has a 4th choice `intent` (claim, refund_info, policy_info, human,
  change_of_mind, why, chitchat, with `labels`). Questions without damage words ("how long does a refund take?", policy, "why ...",
  changed my mind, thanks) get verdict `info` (claim .. image_check skipped, claim unchanged); "a real person" / "human" / "an agent"
  hands over (`human`). An uploaded data URL that is not image/* with JPEG, PNG, WebP or HEIC bytes gets `bad_file` (fixed reply,
  nothing changes). Typing "simulate an error" sends an `error` event during the claim step, then a `message` with verdict `error`.
  Blocked and off-topic turns send the router `skipped` ("Not called: fixed reply, no language model"). The third
  unusable photo goes to `human`.
- `/samples/...` serves the real file under `data/` (placeholder SVG if missing). `GET /api/gallery` lists `data/samples/<product>/`.
- Stats are per chat session, as in the contract: `stats` events and `GET /api/stats?session=<id>` carry `session`, no `chats`;
  `model_ms` (decision, llm, total) and `llm_tokens` (completion tokens, reasoning included) feed "Model use"; `llm_turns`
  and `reasoning_turns` feed "Reasoning on"; `routes` counts replies by Router decision; `d3_tokens` is in a tooltip;
  `blocked` and `off_topic` are counted apart; `image_checks_avoided` is 0 or 1.
- (`POST /api/reset` was removed from the real server on 2026-11-04; the mock still has it.)
- Chat ending (docs/API.md "Chat ending"): every `message` carries `ended` (null while open). Set after
  `accept` (`approved`, "Return approved"), `human` (`handed_over`, "Passed to a colleague"), `not_eligible` (`not_covered`,
  "Not covered", actions `review` + `new_chat`), the 2nd `blocked` (`blocked`, "Closed after repeated bypass attempts") and the
  3rd `off_topic` in a row (`off_topic`, "Closed: not about orders"); the turn that ends a blocked / off-topic chat gets a
  closing reply with no question. Any later POST gets verdict `ended` (all steps skipped "The chat has ended", no stats, no
  state change, `ended` repeated), except `{"session", "action": "review"}` after `not_covered`: a hand-over to a person
  (guardrail skipped, verdict `human`, then `handed_over`).
- `/api/samples`: each turn has `route` (`{model, decision, reasoning, effort}` from the hidden script, null for blocked /
  off-topic fixed replies) and each scenario `uses_reasoning`.

## Component map

| File | What |
|---|---|
| `src/App.tsx` | Page shell: top bar, three columns, gallery dialog, and a small muted footer notice that is always visible ("Messages and photos are processed by the hosted vllm-sr API (research and evaluation use)."; sticky at the bottom on narrow screens) |
| `src/useDemo.ts` | `GET /api/customer` once; every chat (page load, New chat, each sample and Play all item) opens with the customer `greeting` as a client-side assistant message (`greeting: true`, no verdict, no turn); `sampleChat` marks chats started by a sample. Per-session stats (cleared on New chat and on every sample start, `GET /api/stats?session=` for the new session, a `stats` object whose `session` is not the current one is dropped), chat state, SSE handling, live flow, past-turn replay, scenario player and Play all; per-turn outcomes (`matches()`), latest `message.order` (the header chip follows every non-null `message.order`, so it switches when a chat moves to another order; order cards hide once any order is known). On an `error` event every `waiting` or `running` node turns `failed` (the running one shows the error text, the others "Not run: the turn failed"), the "Checking" shimmer stops, and the `message` with verdict `error` that follows renders as a normal reply; if no `message` follows, the error text shows in the bubble. Each POST carries `has_turns` (a `done` came back in this chat); an `expired` event starts a new chat with the notice "This chat expired, starting a new one" under the greeting |
| Chat ending | `useDemo` keeps `ended` from the latest `message` (cleared on New chat and each sample start). While set, the composer row (text field, paperclip, gallery, send) is gone and the thread ends with a quiet "Chat ended" divider and the end banner right under it (Arc `alert`, extended locally with `icon`, `iconKey`, `actions` and a `neutral` tone): icon per reason (approved: check, success; approved_and_handed_over: person with check; handed_over: person, info; not_covered: info, neutral; blocked: shield, danger; off_topic: stop sign, warning; unknown: neutral), `ended.title`, `ended.text`, then "Ask a colleague to review" (secondary, only with action `review`) and "Start a new chat" (primary, same as New chat). Review posts `{"session", "action": "review"}` through the normal send path and shows a small dashed system-style user bubble "Asked a colleague to review the decision"; the banner then morphs to the new `ended`. Buttons are disabled while a turn streams or a sample plays. Order cards stay hidden. Samples keep playing after a chat ends (every remaining turn is still sent). Verdict `ended`: neutral chip "Chat ended" |
| Reasoning marker | Samples rows show a quiet "Reasoning" outline badge when `uses_reasoning` (tooltip "The Router switches reasoning on for a reply of this chat"); once a sample has been played the badge follows that run (`usedReasoning`: any router step `done` with `reasoning: "on"`). Each assistant reply worded by the language model carries a small tag next to its verdict chip, e.g. "fast · reasoning off" or "hard · reasoning on", from that turn's router step (tooltip: decision, reasoning and effort, routed model). In the flow, the router's model pill is tinted when reasoning was on |
| `src/api.ts` | Contract types, JSON calls, SSE parser for `POST /api/chat`; every request carries `X-Visitor-Id`; a non-OK chat answer throws `ApiError` with its `code` (`no_key`) |
| `src/key.ts` | The visitor's key (docs/API.md "API key"): visitor id and key in `sessionStorage` only; on module load (imported first in `main.tsx`) takes `#key=` from the landing page, removes it with `history.replaceState` (keeping the query and any other fragment part) and saves it; `sendKey` (`POST /api/key`), `getKeyState`, `deleteKey`; the message for each error code |
| API key | `App.tsx` `useApiKey`: on load sends a stored or handed-over key (`POST /api/key`), else asks `GET /api/key` (`set` or `server_key` count as ready); status `checking`, `ready` or `none`. Until `ready`, the composer (text, photo upload, gallery), order cards, sample rows and Play all are disabled; the chat shows "Enter your API key to start: messages, photos and samples run on your own vllm-sr key." with an "Enter API key" button, the samples foot the same hint, the composer it as placeholder. `src/panels/KeyDialog.tsx` (Arc `dialog`, `button`): password field with show/hide, "Check key" with a spinner, the server's message per code in red under the field, "Key works for decisions and chat" on success (then it closes), "Get an API key" -> https://app.vllm-sr.ai/#key. It opens by itself when there is no key or a handed-over key fails; it can be closed to look around. Top bar: "Enter API key" (accent pill) or "Change key". A `key_error` during a turn (or a `no_key` the retry could not fix) shows "API key problem: <message>" and a "Change key" button in the reply bubble, marks the running nodes failed, stops a running sample or Play all, and drops the stored key. `npm run mock` answers the key endpoints (any `sk-vsr-` key works, one containing "invalid" is refused; `MOCK_KEY=none` starts without a key) |
| `src/format.tsx` | Verdict labels, tones and icons (`off_topic`: "Off-topic", info tone, circle-slash; `info`: "Answered", info tone; `bad_file`: "Not a photo file", warning tone; `error`: "Error", danger tone); `verdictMeta` gives an unknown verdict a neutral chip with its own name, so a new server verdict never breaks the page; node names; number formatting |
| Health badge | `App.tsx` polls `GET /api/health` every 15 s and after every turn; `TopBar` shows the text for `status` (`guard_down`: "Vela guardrail unavailable: D3 checks messages alone"; `standard`: "Standard replies: language model unavailable"; else the server's `message`) as a small pill left of the theme switch (dot green for `ok`, amber for a fallback, red for `unavailable`, grey while checking or when the server does not answer); the tooltip lists the models that are down (D3 flash, Vela 3 flash, vllm-sr/auto). `npm run mock` with `MOCK_HEALTH=checking|guard_down|standard|unavailable` shows each state |
| Model fallbacks in the flow | Guardrail: without Vela scores the line reads "Vela unavailable, D3 checked alone". Router: skipped with "Language model unavailable: standard reply". Reply: a standard reply (`detail.standard`) gets the warning border, its summary and the reason. Verdict `unavailable`: danger chip "Assistant unavailable" (cloud-off icon) |
| `src/panels/TopBar.tsx` | Title; one row of stat blocks spread over the bar in two captioned groups, "This chat" (D3 calls + median ms, "Reasoning on" as "1 / 4 replies" from `reasoning_turns` / `llm_turns`, "Blocked / off-topic" as "2 / 1", Image checks avoided) and "Model use" (Decision time from `model_ms.decision`, D3 flash and Vela 3 flash; LLM time; Total model time; LLM tokens generated, reasoning tokens included; each a number and unit); labels wrap rather than overlap, the "Model use" group goes below 1150 px and the stats row on phones; grows above 1900 px; theme switch (Arc `theme-switch`) |
| `src/panels/Samples.tsx` | The 4 groups from `GET /api/samples` (collapsible, start open); per row title, expected verdict (icon + word) and subtitle, Turn n/N while playing, then Matched (every turn's verdict equals its `expect`) or Differs, with a turn-by-turn tooltip; Play all / Stop (Arc `button`, `scroll-area`); Play all is hidden when `GET /api/config` says `play_all: false` |
| `src/panels/Policy.tsx` | Policy view (window with "delivered N days ago", covered, not covered, photo, review limit) with the `policy_ref` row highlighted; `OrderChip` ("Order 4411 · Smartphone" + info button) in the chat header and `PolicyButton` under each reply with a `policy_ref` (Arc `popover`, keyboard: Enter opens, Escape closes) |
| `src/panels/OrderCards.tsx` | "Your orders" under the greeting while the chat has no `message.order` and is not a sample: one button card per order (lucide icon per product, item, "Order 4411 · Delivered 2 days ago", "Outside the 30-day window" when `delivered_days_ago > window_days`), two columns; all 10 before the first user message, then the first 6 with "Show all 10". A click sends the visible user message `Order 4411` through the normal send path |
| `src/panels/Chat.tsx` | Greeting bubble + order cards as the empty state (Arc `empty-state` only if `/api/customer` fails), placeholder "Describe the problem, or pick an order above" while the cards show; order chip in the header, Policy button next to the verdict chip, bubbles, streaming reply, verdict chip (Arc `badge`), click a user message to replay its flow, composer (Arc `chat-thread`'s `ChatComposer`), gallery button (Arc `tooltip`), empty state (Arc `empty-state`), "Checking" (Arc `text-shimmer`) |
| `src/panels/Gallery.tsx` | `GET /api/gallery` grouped by product (names from `policies`), click to send (Arc `dialog`) |
| `src/panels/Flow.tsx` | The eight nodes with status dot, model, ms, summary; model names mapped to "Vela 3 flash", "D3 flash", "vllm-sr/auto", routed ids prettified ("qwen/qwen3.8-27b" -> "Qwen3.8 27B"). The router node updates whenever a later event for it arrives (it is filled in after the reply call): when `done` it shows the decision and reasoning ("Hard decision, reasoning on (high)"), the routed model id, the signals, the claim-reading route when present, and a muted note that the Router raises the reasoning effort for upset, urgent or complex turns, normal turns run with reasoning off, and it is not a switch to a larger model (also the node name's tooltip); the summary is then not repeated; claim fields, policy rules, decision. Claim with `detail.order_match` gets the D3 badge and an "Order match" sub-row: "Matched at 0.99, needs 0.75" or "Best 0.52, not matched, needs 0.75", then the distribution. Guardrail (Vela 3 flash + D3 flash): the Vela line "Vela prompt attack 0.02, needs < 0.50 · harm 0.01, needs < 0.50" (a score at or over its threshold in red, "· flagged" when Vela flagged it) or "Vela unavailable, D3 checked alone", then with `detail.scope_check` the D3 badge and a "Scope check" sub-row ("Off-topic at 0.99", red for instructions) with the scope options on one line each and the `command` / `complaint` bars, then the `intent` choice ("What the customer wants", chosen option and value on the right) with its top 3 options on one line each. Reply: blocked and off-topic turns show it skipped ("Fixed reply, no model call"); with `detail.reply_check` a line "Reply check (D3): claims approval 0.02, needs < 0.50", and when `replaced` a warning block "Replaced: the reply claimed an approval" with a collapsible "Original reply" (struck through); the node border turns warning. Earlier-turn ids show only their `t2` part |
| `src/panels/D3Questions.tsx` | Choice distributions, yes/no bars with threshold tick and borderline band, escalation sub-rows; a choice with `labels` (order match, scope check) is drawn as one row per option (`compact`: name, bar and value on one line, for the scope check) (label, "Order 5120", prob, bar with the threshold tick), the chosen row in accent when it clears the threshold, in warning when not |

## D3 time in the flow header

"Turn total X, D3 Y" (no D3 part when Y is 0; "No model time this turn" when nothing was timed, e.g. a bad file). X is the sum
of `ms` of every step that is not skipped. Y adds:
- the `ms` of every non-skipped step other than claim and reply whose `model` contains "d3" (eligibility, photo check);
- for the guardrail only `detail.scope_check.ms` (the D3 scope check; Vela's time is not D3 time);
- `detail.order_match.ms` on the claim step, only if the server sends it (it does not today, so the order match is not counted;
  the claim step's own `ms` is language model time and stays out);
- `detail.reply_check.ms` on the reply step (the reply step's `ms` includes it).

## Contract assumptions

- `GET /api/turn/{turn}` returns `{"turn": "...", "steps": [step events]}`; the UI also accepts a bare array or `events`.
- SSE frames are `data:` lines only (no `event:` names), as the contract describes.
- Yes/no questions: the borderline band is drawn between `borderline_from` and `threshold` whichever way round they are,
  so questions where "yes" is good (borderline_from above threshold) and "yes" is bad both work. Colour comes from `status`.
- `stats.d3_ms_median`, `llm_turns`, `model_ms`, `llm_tokens` may be missing or null; the top bar shows a dash.
- Fonts: Geist (headings) and Inter (text), bundled from @fontsource as on the demos landing page.

README GIF: `docs/demo-chat.gif` ("Wrong item, then no damage" on the live server, light theme). Play all on the earlier local server: 16/16 matched in
about 125 s (before the escalation group was added); the banner shows at the end of every sample that ends the chat, and playback goes on to the next sample.
