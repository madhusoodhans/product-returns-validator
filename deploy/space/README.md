---
title: Product Returns Validator
emoji: 📦
colorFrom: red
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
short_description: A returns chat where small decision models decide and an LLM only talks
---

<!-- Draft Space card: copy to the Space repo's README.md together with the Dockerfile, app/, data/ and web/. -->

# Product returns validator

A customer chats with a shop's returns assistant about a damaged delivery. Small decision models on the hosted
[vLLM Semantic Router](https://github.com/vllm-project/semantic-router) API make every decision (Vela 3 screens each
message for prompt attacks and harm; D3 decides whether it is on topic, which order, whether the claim is eligible and
whether the photo shows the damage); a language model chosen by the Router (`vllm-sr/auto`) only words the reply. Each
step, and what the Router picked for the reply, is shown on the right as it happens. Pick a sample conversation on the
left, or type your own.

> **Please do not enter real personal data.** This is a demo with one made-up customer. Messages and photos you send
> are processed by the hosted vllm-sr API (research and evaluation use) and may be logged there. Do not type real
> names, addresses, order numbers or card details, and do not upload photos of people or documents.

Chats are kept in memory only, for 30 minutes after the last message; a photo sent before an order is known is kept
for 10 minutes. There are limits per visitor (messages per minute, new chats per minute); when the models are busy
the assistant says so and you can try again.

<!-- Space settings: add VLLM_SR_API_KEY as a Space *secret* (Settings -> Variables and secrets -> New secret), never as
a variable. The Dockerfile sets PLAY_ALL=0 and TRUST_PROXY=1. Optional variables: LANDING_URL (an "All demos" link),
D3_MODEL / GUARD_MODEL / LLM_MODEL, MODEL_SLOTS. All variables: README.md, Configuration. -->

Source code, setup and how it was tuned: <https://github.com/madhusoodhans/product-returns-validator>

## Credits

- Decision models: [vllm-sr/d3-flash](https://huggingface.co/vllm-sr/d3-flash) and
  [vllm-sr/vela3-flash](https://huggingface.co/vllm-sr/vela3-flash), on the hosted vllm-sr API
  ([app.vllm-sr.ai](https://app.vllm-sr.ai)).
- Language models: chosen per request by the vllm-sr Router (`vllm-sr/auto`) on the same API.
- Sample photos: 36 photos from Wikimedia Commons (CC0, public domain, CC BY 2.0/4.0, CC BY-SA 2.0/3.0/4.0) and
  adaptations of them (resized, blurred, darkened, glare, cropped, a sticky note added) under the same licences;
  author, licence and source of each are listed in
  [data/raw/commons/SOURCES.md](https://github.com/madhusoodhans/product-returns-validator/blob/main/data/raw/commons/SOURCES.md).
  Bottle photos taken for this demo.
- UI: React, Vite and Arc components.
