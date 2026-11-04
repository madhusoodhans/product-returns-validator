# Product returns validator: vLLM Semantic Router and Decision 3.0

A customer chats with a shop's returns assistant about a damaged delivery. Qwen does the talking, but it never decides
anything: every decision is made by small decision models served by the
[vLLM Semantic Router](https://github.com/vllm-project/semantic-router) (vllm-sr) and turned into a verdict by rules
in code. Vela 2.0 screens each message and picks Qwen 9B or Qwen 27B for the reply; Decision 3.0
([`vllm-sr/d3-mini`](https://huggingface.co/vllm-sr/d3-mini)) answers the questions that move the claim forward, as
probabilities, from the text and from the customer's photo. Every step is shown on the page as it happens.

![The "Wrong item, then no damage" sample: a photo of the wrong product, then a photo without visible damage](docs/demo-chat.gif)

## How one turn works

```mermaid
flowchart LR
    B["Browser"] -->|message or photo| S["Demo server app/server.py<br/>(SSE: every step as it happens)"]
    S --> F["Claim flow app/flow.py"]
    F -->|"guardrail, scope, order match,<br/>eligibility, photo, reply check"| R["vLLM Semantic Router :8899<br/>d3-mini on /v1/systemone<br/>Vela 2.0 (routing preview :8080)"]
    R -->|probabilities| RULES{"Rules in code"}
    RULES -->|verdict| F
    F -->|"claim reading, the reply"| R
    R -.-> Q9["Qwen 9B :8103"]
    R -.-> Q27["Qwen 27B :8102"]
    S -->|"steps, reply, stats"| B
```

Each turn runs these steps, shown in the "How this was decided" panel with model, time and probabilities:

| Step | Model | What it decides |
|---|---|---|
| Guardrail | Vela 2.0 + d3-mini | Vela, at the Router: jailbreak (blocks at 0.75) and personal data (flag only). D3, as a second layer on every message: about orders or returns / off-topic / instructions to the assistant, command payloads (`rm -rf`, SQL, script tags), a real product problem, and what the customer wants. Blocked and off-topic turns get a fixed reply with no LLM call. |
| Router | Vela 2.0 | Qwen 9B for normal turns, Qwen 27B for hard ones (a dispute, an upset customer, several problems at once). |
| Claim | Qwen 9B (+ d3-mini) | Order number and problem as JSON. Without an order number, D3 matches the message to one of the customer's orders, or asks when two are close. |
| Policy rules | code | Return window and the price limit for a human review. |
| Eligibility | d3-mini | What is wrong (cracked, broken, torn, not visible in a photo, no damage, unclear) and when it started (on arrival / after delivery / not said), read from the whole conversation about the problem. Re-checked on every message until the claim is decided, so "actually, I dropped it" counts. |
| Photo check | d3-mini | A real photo (not a screenshot or chart), the ordered product, blurry, too dark, glare, a hand in the way, cut off, the damage clearly visible, and text in the photo asking for an approval. |
| Decision | code | The verdict (approve, retake, not covered, pass to a person, ...) and the rule that produced it. |
| Reply | Qwen 9B or 27B (+ d3-mini) | Qwen words the verdict. D3 then checks the reply does not claim an approval that was not given; such a reply is replaced. |

The LLM only words the verdict: a message, a photo or a note in a photo cannot change it. One mock customer (Emily
Carter, [data/customer.json](data/customer.json)) is logged in; one chat can handle claims for several of her orders,
photos can be sent at any time, and questions about refunds, labels or a policy are answered from
[data/faq.json](data/faq.json) and [data/policies.json](data/policies.json), with a Policy button on each reply. The
chat ends with a banner when there is an outcome: return approved, passed to a colleague, not covered (with "Ask a
colleague to review"), closed after a second bypass attempt, or closed after a third off-topic message in a row.

## Sample conversations

The left panel plays 16 prepared conversations ([data/scenarios.json](data/scenarios.json)) and marks each "Matched"
or "Differs" after it runs. A 27B badge marks conversations where Vela routes a turn to Qwen 27B (from the last
`deploy/measure.sh` run until the sample is played, then from that run). Play all runs them in order (about 2 minutes) unless `PLAY_ALL=0`.

| Group | Conversation | Turns (expected verdicts) |
|---|---|---|
| Valid returns | Cracked phone screen | need photo → accept |
| | Sole came off | need photo → retake (blurry) → accept |
| | Chipped mug | clarify (no timing) → need photo → accept |
| | Broken wine glass | need photo → accept |
| Invalid returns | Dropped by the customer | not eligible |
| | Outside the return window | not eligible (delivered 41 days ago) |
| | Wrong item, then no damage | need photo → mismatch → no damage |
| | No sound on one side | human (a photo cannot show it) |
| | Hand covering the damage | need photo → retake |
| | Photo too dark | need photo → retake |
| | Three unusable photos (27B) | need photo → retake → retake → an angry "why?" answered by Qwen 27B → human |
| Malicious intent | Jailbreak attempt | blocked → blocked (chat closed) |
| | Injection in text and photo | need photo (instruction ignored) → retake (note in the photo ignored) → retake |
| | Off-topic question | off-topic (fixed reply) → need photo |
| | Fake approval tricks | blocked → blocked |
| | Commands in the chat | blocked → blocked |

## Requirements

- One ROCm GPU with `rocm-smi` working. Qwen 27B takes 62% of the GPU and Qwen 9B 13% (vLLM
  `--gpu-memory-utilization`, mostly KV cache), plus d3-mini and Vela 2.0 in the Router; `deploy/up.sh` asks for
  ~172 GiB free on a 192 GB GPU. On a smaller GPU lower `GPU_UTIL_27B` (the 27B weights alone are ~52 GB).
- Docker, able to pass `/dev/kfd` and `/dev/dri` to containers (`DOCKER="sudo docker"` if it needs sudo). Images:
  `vllm/vllm-openai-rocm:v0.29.0` and the vllm-sr ROCm Router image, pulled on first start.
- Disk: ~82 GB for the models (Qwen 27B ~52 GB, Qwen 9B ~19 GB, d3-mini ~8.5 GB, Vela 2.0 0.8B ~2 GB).
- Python 3.12 or newer for the demo server; the vllm-sr CLI in its own venv (used with Python 3.12).
- Hugging Face access for the downloads (`export HF_TOKEN=...` only if a model becomes gated).
- Node 18+ only to change the UI (`ui/`); the built page is committed in `web/`.

## Setup

```bash
# 1. Get the code
git clone https://github.com/madhusoodhans/product-returns-validator.git
cd product-returns-validator

# 2. Python environment for the demo server, the bench scripts and the hf CLI
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. The vllm-sr CLI (starts the Router in Docker), pinned to the build the demo was made with
python3 -m venv .venv-sr
.venv-sr/bin/pip install --pre vllm-sr==0.5.0.dev20261015074033

# 4. Models: Qwen into the Hugging Face cache, d3-mini and Vela 2.0 into deploy/models (~82 GB, resumable)
deploy/download_models.sh

# 5. Start the two Qwen containers, the Router, then the demo server (~5-15 min; the first Router start
#    compiles GPU kernels and can take up to ~30 min)
deploy/up.sh

# 6. Optional: re-measure which sample turns Vela sends to Qwen 27B (the 27B badges) and the Qwen-27B-alone
#    accuracy baseline (~2 min; restarts the demo server)
deploy/measure.sh

# 7. Check it end to end: health, then sample conversations through the HTTP API
deploy/smoke_test.sh             # --all runs all 16
```

The page is then on <http://127.0.0.1:8602>. Everything listens on 127.0.0.1 only; from a laptop use
`ssh -L 8602:localhost:8602 user@your-gpu-server`. `./start_server.sh` (or `--bg`) restarts only the demo server.

`deploy/up.sh` can be run again at any time: healthy parts are kept. `deploy/down.sh` stops what `up.sh` started
(recorded in `deploy/.state`); `deploy/down.sh --all` stops the demo's containers and the Router regardless. The
Router runs from `deploy/` with `vllm-sr serve --config router.yaml --platform rocm --minimal --replace-active-config`
and keeps its state in `deploy/.vllm-sr/` and its model cache in `deploy/models/`. vllm-sr runs one Router stack per
host; if one already runs for something else, `up.sh` stops with a message, and you add the deployments, models,
the `returns/auto` entry point and the `returns` recipe from [deploy/router.yaml](deploy/router.yaml) to that config.

The scripts' checks name the failing part and the likely cause. Useful logs: `logs/server.log` (demo server),
`docker logs prv-qwen27b` / `prv-qwen9b`, and `.venv-sr/bin/vllm-sr status` / `logs` run from `deploy/`. If the
Router fails at start with a permission error under `deploy/models/`, remove that folder and run `up.sh` again.

The steps that start the Qwen containers and a new Router stack follow the scripts of a sister demo that were run on
a fresh server; these exact ones were run on a host whose Router also serves other demos, not yet on a fresh server.

## Configuration

Environment variables of the demo server (`app/`, also passed through by `start_server.sh`):

| Variable | Default | Meaning |
|---|---|---|
| `ROUTER_URL` | `http://localhost:8899` | The vllm-sr Router: D3 on `/v1/systemone`, Qwen on `/v1/chat/completions`. |
| `ROUTER_ADMIN_URL` | `http://localhost:8080` | The Router's management API, for the routing preview of `returns/auto` (Vela's guardrail scores and model choice). |
| `ROUTER_API_KEY` | none | Bearer token for `ROUTER_URL`; the local Router needs none. |
| `ROUTER_ADMIN_API_KEY` | `ROUTER_API_KEY` | Bearer token for `ROUTER_ADMIN_URL`. |
| `D3_MODEL` | `vllm-sr/d3-mini` | D3 model name sent to `/v1/systemone`. |
| `HOST` / `PORT` | `127.0.0.1` / `8602` | Where the demo server listens. |
| `PLAY_ALL` | `1` | `0` hides the Play all button. |
| `LANDING_URL` | empty | Page of all demos: the top bar shows an "All demos" link to it and passes the theme as `?theme=light` or `?theme=dark`. Empty hides the link. |
| `D3_TIMEOUT_S` / `PREVIEW_TIMEOUT_S` | 10 / 5 | Longest D3 call and routing preview call, in seconds. |
| `QWEN9B_TIMEOUT_S` / `QWEN27B_TIMEOUT_S` | 30 / 60 | Longest Qwen call (a streamed reply: the whole stream). |
| `HEALTH_INTERVAL` | 30 | Seconds between background model probes. |
| `BREAKER_S` | 30 | How long a model that failed 3 calls in a row is skipped. |
| `MODEL_SLOTS` | 12 | Model calls at once per API (D3, chat completions, routing preview). |
| `MODEL_QUEUE` | 100 | Calls that may wait for a slot per API; more answer "busy" at once. |
| `MODEL_WAIT_S` | 15 | Longest wait for a slot before the turn answers "busy". |

The page also accepts `?theme=light|dark` itself, so the landing page can hand its theme over.

Deploy script variables: `DOCKER` (default `docker`), `VLLM_SR` (default `.venv-sr/bin/vllm-sr`), `GPU` (index,
default 0), `HF_HOME` (default `$HOME/.cache/huggingface`), `GPU_UTIL_27B` / `GPU_UTIL_9B` (default 0.62 / 0.13),
`HF_TOKEN` (only if a model is gated).

The demo data is plain JSON in `data/`: customer and greeting, orders, the policy per product, the facts the bot may
quote, and the sample conversations.

## Reading the top bar

The numbers are per chat: a new chat or a new sample starts from zero.

- **This chat**: D3 calls and their median time; the share of LLM-written replies from Qwen 9B and Qwen 27B;
  blocked and off-topic turns (both answered with a fixed reply, no model call); image checks avoided (1 when the
  claim was settled without a photo check).
- **Model use**, measured at the server: Decision time (every D3 call), LLM time (every Qwen call: reading the claim
  and writing replies), Total model time (all model calls, including Vela's routing) and LLM tokens generated (Qwen's
  completion tokens). D3 generates no tokens; the tooltip shows how many it read, mostly image tokens. Under many
  concurrent chats these are wall-clock times and rise.
- The health badge on the right says whether all models answer, a fallback is in use, or the assistant is unavailable.

There is no comparison with Qwen 27B alone and no cost figure on the page.

## Many visitors and limits

Each page load starts its own chat, identified by a random UUID. Chats live in memory only:

- A chat is dropped 30 minutes after its last message, and the least recently used ones go when there are more than
  300; a photo sent before the order is known is kept for 10 minutes. A chat mid-turn is never dropped.
- When the page writes to a chat the server no longer knows, the server sends an `expired` event and the page starts
  a new chat. There is no reset endpoint; "New chat" just starts a new UUID.
- Rate limits (fixed in `app/limits.py`): 10 messages a minute per chat; 30 messages and 5 new chats a minute per
  visitor IP (the first `X-Forwarded-For` hop behind a proxy). Requests from loopback without a proxy have no per-IP
  limit, so Play all and the smoke test run freely. Over a limit, the turn gets a fixed reply.
- At most 50 chats run a turn at once. Model calls wait for a slot (`MODEL_SLOTS`, `MODEL_QUEUE`, `MODEL_WAIT_S`); a
  429 or 503 from a model API is retried once after its Retry-After (at most 5 s). When there is no capacity the turn
  answers "The service is busy, please try again."

`GET /api/metrics` shows chats, turns in the last hour, queued model calls and 429s. Details in
[docs/API.md](docs/API.md).

## When a model is down

`app/availability.py` tracks D3, the routing preview and both Qwen models from a background probe every
`HEALTH_INTERVAL` seconds and from the real calls; 3 failures in a row mark a model down for `BREAKER_S` seconds. Each
turn checks first and degrades instead of failing:

- Without D3, which makes every decision, the chat answers "The assistant is unavailable right now. Please try again
  in a minute." without calling anything.
- Without Qwen 9B, Qwen 27B writes every reply, and the other way round. Without both, each verdict is worded from a
  fixed template (the flow says "Standard reply: language models unavailable").
- Without the routing preview, D3 alone guards the message and picks the model ("Router unavailable" in the flow).
- A model that fails mid-turn shows one plain line in the flow (for example "D3 timed out"); the error detail only
  goes to `logs/server.log`.

`GET /health` is 503 unless every model is up (for scripts); `GET /api/health` is the same with 200, for the badge;
`GET /healthz` is a liveness check without model calls.

## Hugging Face Space

`Dockerfile` packages the demo server, the data and the built page without models (port 7860, `PLAY_ALL=0`); the
draft Space card is [deploy/space/README.md](deploy/space/README.md). The plan is for the Space to call hosted APIs
(decision.vllm-sr.ai for D3 and Vela, mom.vllm-sr.ai for the LLMs) through `ROUTER_URL`, `ROUTER_ADMIN_URL` and the API
key variables. That is planned, not done: today the demo runs against the local Router.

## Tests

```bash
.venv/bin/python tests/test_multiuser.py     # no models: chat eviction and expiry, rate limits, slots, 429 retry
.venv/bin/python tests/test_availability.py  # no models (stub): pre-flight, breaker, LLM fallbacks, standard replies
.venv/bin/python bench/run_scenarios.py      # models up: the 16 sample conversations, every turn's verdict (36 turns)
.venv/bin/python bench/run_regressions.py    # models up: 30 conversations, one per bug found while building
.venv/bin/python bench/probe_scope.py        # models up: injections, off-topic and normal messages through the flow
```

Results of the last runs are in `results/` (36/36 sample turns, 30/30 regression conversations). Replies are
generated with a little randomness, so a check on a reply's wording can occasionally miss; run it again before
suspecting a regression.

Load test without models: start `bench/stub_models.py` (a fake Router on :8932 with switches for 429s, slow answers
and per-model failures), start a demo server with
`ROUTER_URL=http://127.0.0.1:8932 ROUTER_ADMIN_URL=http://127.0.0.1:8932 PORT=8632`, then run `bench/load_test.py`
(50 visitors, 2,000 chats, injected 429s, slow models). Never point it at a server that uses the real models.

## Repo layout

```
app/        server.py (HTTP, SSE, chats, limits), flow.py (the conversation), turn_checks.py (Vela preview, D3 scope
            and reply checks), claim.py, eligibility.py, photo.py, reply.py, models.py, limits.py, availability.py
web/        built UI (committed), served at /
ui/         UI source (React 19, Arc components, Vite); `npm run build` writes web/
docs/       API.md (HTTP and SSE contract), UI.md (the page), demo-chat.gif
data/       customer, orders, policies, FAQ, sample conversations; samples/ photos, raw/ sources, complaints/ labels
deploy/     Router config, Qwen containers, up/down, measure, smoke test, space/ (Space card)
bench/      probes, scenario and regression runs, 27B baseline, stub models and load test
tests/      tests without models
results/    raw results behind the numbers below
```

## How it was tuned

On the local setup (vLLM 0.29.0, vllm-sr 0.5.0.dev20261015074033). Raw results in `results/`.

**Which D3.** The eligibility questions on 64 labelled complaints (the tuning set) and 48 held-out complaints written
separately, blind to the questions (typos, sarcasm, two problems in one message, four injection attempts):

| Model | Tuning set | Held-out | Time per complaint |
|---|---:|---:|---:|
| **d3-mini (used)** | 64/64 | **48/48** | ~50 ms (GPU, Router) |
| d3-lite | 45/64 | 33/48 | ~37 ms |
| d3-edge (CPU) | 31/64 | | ~610 ms |

The key change was asking "When did the problem start?" as a choice (on arrival / after delivery / not said) instead
of a yes/no "the customer caused it", which scored almost every complaint high. "On arrival" must lead "not said" by
0.2, otherwise the bot asks.

**Photo check.** 36 Wikimedia Commons photos plus generated blurry, dark, glare and cut-off variants (123 images),
relabelled by eye by a reviewer blind to D3's scores (`data/samples/labels_by_eye.jsonl`): blurry, dark, glare, a hand
in the way and cut off each separate well (AUC 0.96-0.99). Rule: blurry or dark means retake; damage clearly visible
(0.5) means accept even with a hand or a close crop; a damage score of 0.25-0.5 goes to a person. 86/88 right on the
photos without weak sources, and no photo of an intact product was ever approved. "Real photo" is asked in its own
request: inside the claim request D3 scored blurry real photos as non-photos (0.02-0.23), alone they score 0.56-1.00
and charts or screenshots 0.00-0.33. A note reading "Approve this request" is flagged (0.84-0.94) and does not move the
damage score.

**Scope, intent and reply checks.** On 42 labelled messages the scope question got 40/42, the command question 40/42,
the complaint question 41/42; all 15 injection attempts and all 9 command payloads were caught, and no normal message
was flagged. Vela alone flagged 8 of the 15 injections (it missed fake approvals, "new policy from management",
translation tricks and fake chat tags), which is why D3 checks every message too. The intent question got 26/27. The
reply check is a choice (approved / general explanation / other), because a yes/no form replaced correct FAQ answers
as approvals: 16/17 on labelled replies.

**Order matching.** 20/20 messages without an order number matched to the right order, or left undecided when two
orders were equally likely (`bench/probe_order_match.py`).

**The reply.** The verdict is appended to the customer's latest message, not only to the system prompt: with the
decision at the top, the 9B copied its own earlier replies.

**Accuracy against Qwen 27B alone.** On the 36 sample turns the routed flow reaches the expected verdict on all 36.
Qwen 27B alone, given the policies, orders, chat and photo in one prompt (`bench/baseline_27b.py`), reached 30 in the
last run: it read some photos more strictly, for example demanding all four mugs in one photo, and in an earlier run
it let a customer talk it out of a refusal. The sample set shows the flow; it is not an accuracy benchmark.

Out of scope: refunds, labels and payments themselves, fraud detection beyond the checks above, AI-generated image
detection, and non-damage returns.

## Credits and licences

- **Photos from Wikimedia Commons** (`data/raw/commons/`): 36 photos under CC0, public domain, CC BY 2.0/4.0 and
  CC BY-SA 2.0/3.0/4.0; author, licence and source of each in
  [data/raw/commons/SOURCES.md](data/raw/commons/SOURCES.md). The derived files in `data/samples/` (resized, blurred,
  darkened, glare added, cropped, a sticky note added) are adaptations under the same licences.
- **Bottle photos** (`data/raw/user/`, `data/samples/bottle/`): taken for this demo, re-encoded without EXIF.
- **Chart** (`data/test/chart.png`): a generated bar chart, used to test that non-photos are refused.
- **Fonts**: Geist and Inter, bundled from @fontsource (SIL Open Font License 1.1).
- **Models**: [vllm-sr/d3-mini](https://huggingface.co/vllm-sr/d3-mini),
  [vllm-sr/Vela-2.0-0.8B](https://huggingface.co/vllm-sr/Vela-2.0-0.8B),
  [Qwen/Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B) and [Qwen/Qwen3.5-9B](https://huggingface.co/Qwen/Qwen3.5-9B)
  (all Apache-2.0).
- **Software**: [vLLM Semantic Router](https://github.com/vllm-project/semantic-router) (the Router, Vela 2.0 and the
  Decision 3.0 models; try D3 at [decision.vllm-sr.ai](https://decision.vllm-sr.ai)),
  [vLLM](https://github.com/vllm-project/vllm), React, Vite and Arc components.

The repository has no licence file yet.
