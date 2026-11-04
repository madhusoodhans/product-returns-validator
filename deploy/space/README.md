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

A customer chats with a shop's returns assistant about a damaged delivery. Small decision models served by the
[vLLM Semantic Router](https://github.com/vllm-project/semantic-router) make every decision (is the message on topic
or an attack, which order, is the claim eligible, does the photo show the damage); Qwen only words the reply. Each
step is shown on the right as it happens. Pick a sample conversation on the left, or type your own.

> **Please do not enter real personal data.** This is a demo with one made-up customer. Messages and photos you send
> are passed to model APIs, and those requests may be logged by the API providers. Do not type real names, addresses,
> order numbers or card details, and do not upload photos of people or documents.

Chats are kept in memory only, for 30 minutes after the last message; a photo sent before an order is known is kept
for 10 minutes. There are limits per visitor (messages per minute, new chats per minute); when the models are busy
the assistant says so and you can try again.

<!-- Space variables: ROUTER_URL, ROUTER_ADMIN_URL (and ROUTER_API_KEY for a hosted API) reach the models; the Dockerfile
sets PLAY_ALL=0. Optional: LANDING_URL, the page of all demos, adds an "All demos" link to the top bar (empty hides it).
All variables: README.md, Configuration. -->

Source code, setup and how it was tuned: <https://github.com/madhusoodhans/product-returns-validator>

## Credits

- Decision models: [vllm-sr/d3-mini](https://huggingface.co/vllm-sr/d3-mini) and
  [vllm-sr/Vela-2.0-0.8B](https://huggingface.co/vllm-sr/Vela-2.0-0.8B), served by the
  [vLLM Semantic Router](https://github.com/vllm-project/semantic-router) (Apache-2.0).
- Language models: [Qwen/Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B) and
  [Qwen/Qwen3.5-9B](https://huggingface.co/Qwen/Qwen3.5-9B) (Apache-2.0), served with [vLLM](https://github.com/vllm-project/vllm).
- Sample photos: 36 photos from Wikimedia Commons (CC0, public domain, CC BY 2.0/4.0, CC BY-SA 2.0/3.0/4.0) and
  adaptations of them (resized, blurred, darkened, glare, cropped, a sticky note added) under the same licences;
  author, licence and source of each are listed in
  [data/raw/commons/SOURCES.md](https://github.com/madhusoodhans/product-returns-validator/blob/main/data/raw/commons/SOURCES.md).
  Bottle photos taken for this demo.
- UI: React, Vite and Arc components.
