# Cost optimization — developer reference

Maintainer-facing reference for every cost-saving mechanism in itemeval: how
each works, what it measurably saves (price **and** time), the trade-offs, and
the routing guidance (direct API vs OpenRouter). All numbers are from the live
validation pilot of 2026-06-11 (~$2.6 total: haiku-4.5 + gpt-5-mini, via
OpenRouter and via the direct APIs; artifacts were under
`/tmp/itemeval-cachepilot`). The user-facing version of this page is
[docs/wiki/Cost-Savings.md](wiki/Cost-Savings.md).

## The mechanism stack

Ordered from "always on, free" to "opt-in, situational":

| # | Mechanism | Saves | Costs (trade-off) | Default | Where in code |
|---|---|---|---|---|---|
| 1 | Two-stage generate/grade | 100% of generation when adding graders/rubrics | none — architectural | always | `grade/` reads the solutions store |
| 2 | Local response cache | 100% of any identical re-run/resume/retry call | disk; per-machine only | on (`cache: true`) | `CachePolicy(expiry=None, per_epoch=True)` in both task builders |
| 3 | Keyed resume | re-pays nothing after interruption | none | always | `store._solutions.items_to_run`, `store._gradings.pending_solutions` |
| 4 | Provider prompt caching (this work) | 75–90% of *repeated input tokens*; can also cut wall time at scale | see matrix below | scheduling on; layouts opt-in | `_cachegate.py`, `split_prompt`/`split_rubric`, `cache_prompt` |
| 5 | Batch APIs | ~50% of everything | latency minutes–hours; no live progress; **not available via OpenRouter** (`BATCH_PROVIDERS` = openai/anthropic/google/grok/together) | `full-batch` policy | `budget/_policies.py`, `GenerateConfig.batch` |
| 6 | Budget guardrails | unbounded (prevention) | none | `dev` policy, gate, `max_usd` | `budget/_gate.py`, `_policies.py` |
| 7 | Output caps + `on_empty` | bounds output spend; avoids re-paying reasoning-burned calls | truncation risk | user-set | `solvers.max_tokens`, `solvers.on_empty` |

## Provider prompt caching: measured price/time matrix

### Replications (10 calls = 2 items × 5 epochs, ~9k-token shared instruction
head, direct APIs)

| Arm | Price | Δprice | Wall time | Δtime |
|---|---|---|---|---|
| OpenAI, burst (all calls at once) | $0.038 | — | 26 s | — |
| OpenAI, gated (warm-up call first) | **$0.017** | **−55%** | 39 s | +50% |
| Anthropic, monolithic, burst | $0.119 | — | 40 s | — |
| Anthropic, monolithic, gated | $0.067 | −43% | 45 s | +12% |
| Anthropic, split, burst | $0.093 | −22% | 36 s | −10% |
| Anthropic, split, gated | **$0.055** | **−54%** | 50 s | +25% |

### Judge fan-out (116 stored solutions, one Anthropic judge via OpenRouter
pinned to the Anthropic upstream; both arms gated)

| Arm | Price | Δprice | Wall time | Δtime |
|---|---|---|---|---|
| Monolithic rubric (0 cache hits) | $0.840 | — | 72 s | — |
| `split_rubric` (106/116 hits) | **$0.426** | **−49%** | **35 s** | **−51%** |

### Reading the matrix

- **The gate trades one extra call-latency for the discount.** On a small
  fan-out (everything fits one concurrent wave) that is +10–50% wall time for
  −40–55% price. On a large fan-out (calls ≫ `max_connections`) the wait
  amortizes to noise **and flips sign**: cache reads skip re-processing the
  long prefix, so the cached arm was 2× *faster* on the 116-call judge run.
  Rule of thumb: below ~20 calls per condition you are trading time for
  money; above that you get both.
- **Layout (split) is about whether the discount can engage at all** on
  Anthropic-family models: required through OpenRouter (single-block text
  prompts get no cache marker), optional on the direct API (it auto-caches
  the last block), but still strictly better when the template head is shared
  across items (one cache write instead of one per item).
- **Output tokens are never discounted.** End-to-end savings scale with the
  input:output ratio — judge stages (read a lot, write a score) benefit most;
  long-form generation benefits least.

## Direct API vs OpenRouter — when to use which

Both can be mixed in one config (`solvers.models` accepts both id forms).

**Call the provider's own API when:**

- You want the cache discount *reliably*. The gate's full effect only shows
  direct (OpenAI burst: 0% hits direct; gated: 90%). Via OpenRouter, routing
  can also land Anthropic models on Bedrock, which ignores cache markers
  entirely — silent full price.
- You want **batch mode**: inspect's batch support covers
  openai/anthropic/google/grok/together natively; OpenRouter is not a batch
  provider, so `full-batch` buys nothing there.
- You care about clean cost reconciliation: one provider dashboard, no
  marketplace fee (OpenRouter takes ~5% on credits), stable endpoint
  provenance in the manifest.
- Anthropic + monolithic prompts: direct auto-caches them; via OpenRouter
  you must restructure (`split_*`) to get anything.

**Call OpenRouter when:**

- You need the long tail: models with no direct account, or many providers
  under one key/bill — the typical multi-model comparison study.
- You want rate-limit resilience/failover across upstreams (the same routing
  spread that hurts caching helps throughput).
- The study is small enough that discounts don't matter (dev/pilot scale).

**If using OpenRouter for a cache-heavy Anthropic run:** pin the upstream —
`provider={"order": ["Anthropic"], "allow_fallbacks": False}` — currently only
possible via the Python API's `model_factory` hook (a `solvers.provider_routing`
config knob is future work, FUTURE.md §1.6).

## Failure modes (all observed live)

| Symptom | Cause | Fix |
|---|---|---|
| `cache_read=0` everywhere, Anthropic via OpenRouter, monolithic prompts | no marker on single-block text messages | `split_prompt` / `split_rubric: true` |
| `cache_read=0`, split layout, Anthropic | shared head below the per-model minimum (4096 tok for Haiku 4.5/Opus-class; 1024–2048 others) — silent no-op | lengthen the head or accept no caching |
| `cache_read=0`, OpenRouter, markers correct | routed to Bedrock/other upstream | pin provider order |
| `cache_read=0`, direct OpenAI, burst | simultaneous arrivals; no entry registered yet | `cache_schedule: auto` (default) |
| writes ≫ reads (Anthropic) | low reuse — write surcharge (1.25×) exceeded read savings | expected on tiny groups; gate + split reduce duplicate writes |

## Interaction notes

- **Gate × local cache**: byte-identical duplicate judge calls (same item +
  same solution text, common with empty/short/identical answers) resolve to
  the *local* cache when gated — $0, no API call. This beat provider caching
  in the pilot (19/36 calls free).
- **Gate × batch**: gate disabled under batch (the batch queue reorders
  anyway; cache hits there are best-effort bonus on top of the 50%).
- **Split × condition ids**: both `split_*` flags add `layout: split` to the
  condition payload — enabling them starts fresh conditions by design (never
  silently mixes layouts).
- **Pricing**: cache reads/writes are priced from the table
  (`--refresh-pricing` pulls per-model cache rates from OpenRouter); write
  surcharge defaults to 1.25× input for Anthropic-style models and $0
  elsewhere (`budget/_pricing.py:cache_write_default`).

## Open follow-ups (FUTURE.md §1.6 tail)

`solvers.provider_routing` config knob; OpenAI `prompt_cache_key` + 24h
retention passthrough; cache-aware estimator (project the discounted cost);
estimate-time warning when a split head is below the provider minimum;
store-level judge dedup (`dedup_identical`); cheap-then-escalate judging.
