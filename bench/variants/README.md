# System-1 architecture evaluation

Does putting a System-1 model (Decision 3.0 / d3) in front of the LLM actually
help, or is it just faster? This eval answers that by running three
architectures of the Product-Returns Validator end to end on one AMD Instinct
MI300X and measuring **accuracy, latency and tokens** on every set, not just
speed.

![System-1 evaluator results](../docs/system1-eval.png)

## The three architectures

| | Extract | Decide | Word the reply |
|---|---|---|---|
| **A** (shipped) | System-1 (d3) | code rules | LLM |
| **B** (ablation) | LLM (Qwen) | code rules | LLM |
| **C** (baseline) | LLM | LLM | LLM |

Only the **extractor** changes between A and B. Every decision rule, threshold
and prompt in `app/` is byte-identical, so the A-vs-B delta is purely
System-1-extract vs LLM-extract. C is the single-model baseline
(`bench/baseline_27b.py`): one Qwen 27B prompt does extraction, decision and
reply together.

## Headline result

Faster is not enough; A is also the only variant that gets every turn right.

| Variant | Accuracy (36 turns) | Model time | Per turn | Tokens |
|---|---:|---:|---:|---:|
| **A** System-1 extract + decide + LLM | **36/36** | 24.8 s | **690 ms** | 280k |
| B  LLM extract + System-1 decide | 35/36 | 289.5 s | 8043 ms | 120k |
| C  LLM only | 30/36 | 38.6 s (wall) | 1073 ms | 85k |

- **The decide stage** is what lifts C's 30/36 to 36/36: the six turns the LLM
  gets wrong on its own are all caught by the code rules (see the table in the
  figure, and `results/system1_eval/baseline_27b.json`). Five of the six are
  photo-judgement turns where the free LLM invents policy or misreads the image.
- **The extract stage** is what makes it cheap: identical outcome accuracy to
  the LLM extractor on the labelled sets, at roughly 1/56th of the latency.

## Labelled sets (extractor accuracy and latency)

Run through the app's own `eligibility` / `photo` / `turn_checks` modules, so
the rules are identical; only the extractor backend changes.

| Set | System-1 (d3) | LLM extractor |
|---|---|---|
| eligibility (64) | 64/64 @ 59 ms | 64/64 @ 3328 ms |
| held-out (48) | 48/48 @ 60 ms | 48/48 @ 3355 ms |
| scope (42) | 40 / 41 / 40 @ 59 ms | 42 / 41 / 41 @ 3265 ms |
| photo (119) | 88 @ 443 ms | 83 @ 6110 ms |

Suite wall-clock: 60.8 s (d3) vs 1312 s (LLM), a 21.6x gap. Vision is a genuine
tradeoff: d3 scores 88 vs the LLM's 83, with different failure cases (d3 is
weaker on glare and cut-off crops; the LLM is weaker on dark and
clearly-damaged shots).

## How to reproduce

With the stack up (see the repo README, `deploy/up.sh`):

```bash
# 36 sample turns, variant A (shipped) and B (LLM-extract ablation)
python bench/variants/run_variant.py A
python bench/variants/run_variant.py B

# variant C (LLM-only baseline)
python bench/baseline_27b.py

# labelled sets, each extractor backend
PRV_BACKEND=d3  python bench/variants/run_labelled.py
PRV_BACKEND=llm python bench/variants/run_labelled.py
```

Each writes a JSON next to the raw runs in `results/system1_eval/`.

## Environment

All on one MI300X: vLLM 0.29.0 (ROCm), vllm-sr 0.5.0.dev20261015074033, Router
(d3-mini + Vela 2.0) in front of Qwen3.8-27B and Qwen3.5-9B. The raw JSON behind
every number in the figure is in `results/system1_eval/`.

## Files

```
bench/variants/
  d3_llm.py          d3-compatible shim backed by a Qwen LLM (the B extractor)
  run_variant.py     36 sample turns through the real flow, variant A or B
  run_labelled.py    labelled eligibility / photo / scope sets, d3 or llm backend
results/system1_eval/
  results_A.json     variant A, 36 turns
  results_B.json     variant B, 36 turns
  baseline_27b.json  variant C, 36 turns (+ the six misses)
  labelled_d3.json   labelled sets, System-1 extractor
  labelled_llm.json  labelled sets, LLM extractor
docs/system1-eval.png  the figure above
```
