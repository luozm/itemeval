# Cost optimization — developer reference

Maintainer-facing reference for every cost-saving mechanism in itemeval: how
each works, what it measurably saves (price **and** time), the trade-offs, and
the routing guidance (direct API vs OpenRouter). All numbers are from the live
validation pilot of 2026-06-11 (~$2.6 total: haiku-4.5 + gpt-5-mini, via
OpenRouter and via the direct APIs; artifacts were under
`/tmp/itemeval-cachepilot`). A follow-up tail pilot of 2026-06-12 (~$0.12,
artifacts under `/tmp/itemeval-tailpilot`) validated the cache-tail features
live: the `provider_routing` pin held (all calls answered by the Anthropic
first-party upstream — verified then from per-call `provider` fields in the
raw logs; recorded directly as `endpoints_effective.upstream` since),
OpenAI keyed caching hit across separate runs of the same
condition (a later wave read 1152 cached tokens per call from the
pilot-warmed head), and the discounted projection stayed within 2× of actuals
even when the projected discount did not engage (the `cache-zero-reads` hint
fired as designed). The user-facing version of this page is
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
| 5b | Native batch routing | makes #5 reachable for `openrouter/*`-sampled models (so the ~50% applies to the dominant grade stage) | endpoint switch can change outputs / confounds an endpoint comparison → opt-in; native key required | off (`budget.prefer_native_batch`) | `budget/_routing.py`; threaded through `_estimator`/`generate/_run`/`grade/_run` |
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
  Anthropic-family models: required through OpenRouter (re-checked live
  2026-06-12 on inspect 0.3.239 — its openrouter provider now inserts
  message-level `cache_control` markers, but a monolithic prompt is a single
  string-content user message, which the placement scheme never marks:
  `cache_write=0` on every call), optional on the direct API (it auto-caches
  the last block), but still strictly better when the template head is shared
  across items (one cache write instead of one per item).
- **Output tokens are never discounted.** End-to-end savings scale with the
  input:output ratio — judge stages (read a lot, write a score) benefit most;
  long-form generation benefits least.
- **Direct OpenAI runs are keyed automatically** (`_endpoints.model_args_for`):
  scheduled runs attach `prompt_cache_key = itemeval/<study>/<condition_id>`
  (stable across estimate→pilot→full, so a pilot warms the full run) and
  `prompt_cache_retention: "24h"` (surcharge-free, verified 2026-06-12 —
  same-day phases keep hitting the cache instead of the default 5–10 min
  window). Per-condition granularity: per-sample keys aren't reachable
  through inspect's GenerateConfig.

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

**If using OpenRouter for a cache-heavy Anthropic run:** pin the upstream with
the `provider_routing` knob (on `solvers:`, and per grader spec — judges route
too):

```yaml
solvers:
  provider_routing: { order: [anthropic], allow_fallbacks: false }
graders:
  judge:
    provider_routing: { order: [anthropic], allow_fallbacks: false }
```

The object is OpenRouter's own provider-routing schema, passed through
verbatim — no renamed keys (slugs from `openrouter.ai/api/v1/providers`; the
Anthropic first-party upstream is lowercase `anthropic`, checked 2026-06-12).
Optimization knob: it never enters condition ids; the manifest's config echo
records what routing was requested and `endpoints_effective` what actually
answered — including, for openrouter models, the `upstream` host the calls
landed on (the response's `provider` field: `"Anthropic"`, `"Amazon
Bedrock"`, ...; distinct values within one run are comma-joined). A change
of upstream across runs of the same model raises an endpoint-drift warning
(`_driftcheck.py`) naming `provider_routing` as the fix. Setting it in a section with no `openrouter/*` model warns (inert
knob, never blocks). An `openrouter/anthropic/*` model running cached
*without* it fires the `openrouter-unpinned-cache` hint.

### OpenRouter or direct? (three buckets)

Where caching decides the routing question (facts checked 2026-06-12 against
OpenRouter's live endpoints API and provider docs):

**A. OpenRouter direct is fine — no routing object needed.** Single upstream,
or automatic token-prefix caching on every upstream:

- `openrouter/openai/*` for *unkeyed* caching: both listed upstreams (OpenAI
  and Azure) report `supports_implicit_caching` with cache-read pricing
  accounted by OpenRouter.
- `openrouter/x-ai/*`: single upstream (xAI), cache-read priced.
- `openrouter/google/*`: Vertex and AI Studio upstreams carry identical
  implicit-caching flags and cache pricing per model — no upstream to pin
  (note: the flag is per-model; some Gemini endpoints don't support implicit
  caching at all).

**B. OpenRouter works but MUST pin the upstream (`provider_routing`):**

- `openrouter/anthropic/*` with caching on — routing can land on
  Bedrock/Vertex, which ignore the markers (our live finding):
  `{order: [anthropic], allow_fallbacks: false}`.
- `openrouter/deepseek/*` and other multi-host open models — only the
  first-party upstream caches (64-token blocks); pin it or accept
  `cache_read=0`.

**C. Must NOT use OpenRouter — direct API required:**

- OpenAI **keyed** caching (`prompt_cache_key` / `prompt_cache_retention`):
  OpenRouter does not document forwarding these request fields (they appear
  in no endpoint's `supported_parameters`); itemeval only attaches them on
  direct `openai/*` models.
- Any provider batch API (OpenRouter is not in `BATCH_PROVIDERS` — already
  structurally true).
- Anthropic if you ever need the 1h cache TTL (not reachable through inspect
  at all today, direct or not — the marker has no `ttl` field; documented
  limitation).

## Failure modes (all observed live)

| Symptom | Cause | Fix |
|---|---|---|
| `cache_read=0` everywhere, Anthropic via OpenRouter, monolithic prompts | no marker on single-block text messages | `split_prompt` / `split_rubric: true` (the `anthropic-openrouter-no-split` hint flags this at estimate time, and the estimator projects no discount for this layout — full price, honestly) |
| `cache_read=0`, split layout, Anthropic | shared head below the per-model minimum (4096 Haiku 4.5/Opus 4.5–4.6; 2048 Opus 4.7/Haiku 3.5; 1024 Opus 4.8/Sonnet 4.x; 512 Fable/Mythos 5 — checked 2026-06-12) — silent no-op | lengthen the head or accept no caching; the `split-head-below-min` hint flags this at estimate time |
| `cache_read=0`, OpenRouter, markers correct | routed to Bedrock/other upstream | `provider_routing: {order: [anthropic], allow_fallbacks: false}` (the `openrouter-unpinned-cache` hint flags this) |
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

## Estimator pairing

The estimator projects the cache discount (best case: it assumes the
scheduled hits land), so the money gate compares the price the run *should*
cost. The corrective feedback loop is the post-run `cache-zero-reads` hint:
if the projected discount didn't materialize, the run says so. There is
deliberately no haircut/confidence knob — the pair of "optimistic projection
+ loud zero-reads signal" replaces it. One exception to "best case": layouts
where the discount is structurally unreachable — Anthropic-style monolithic
prompts via OpenRouter (no marker ever lands) — are projected at full price
and flagged by the `anthropic-openrouter-no-split` hint instead.

## Open follow-ups (cache-scheduling tail; plan: `docs/plans/archive/cache-tail.md`)

Store-level judge dedup (`dedup_identical`); cheap-then-escalate judging;
per-cache-group OpenAI `prompt_cache_key` (needs upstream GenerateConfig
support — today's key is per-condition, attached automatically).

## Batch vs cache (settled by `native-batch-routing`)

`native-batch-routing` (`budget.prefer_native_batch`, plan
`docs/plans/archive/native-batch-routing.md`) routes `openrouter/*`-sampled
models to their native API under batch. It settles the standing "which is
cheaper, batch or cache?" question: **batch wins for itemeval's cost profile** —
it is ~50% off *everything including output*, whereas caching discounts only the
repeated input prefix and never output (matrix above). `estimate` now shows the
per-model native-batch-vs-OpenRouter-cache **expected** comparison
(`Estimate.routes`, each a `NativeRoute`). The two are kept mutually exclusive
(batch reorders calls, so it disables cache scheduling); **stacking** native
cache *on top of* batch is deferred — provider support exists in principle but
inspect's batch path + marker placement is unverified and the marginal gain is
small (cache would only re-discount input the batch already halved). Routing
prices off the **sampled** id (the table keys models under OpenRouter's spelling,
so native ids are not reliably priceable) — only batch eligibility and the served
endpoint follow the native id.
