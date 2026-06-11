# Budget and Costs

itemeval refuses to be surprised by a bill: every paid stage is preceded by a
projection and a gate, every call's cost is attributed afterward, and a hard
cap can never be talked past.

## Estimation

`itemeval estimate` projects each stage **without any model API calls**:

- input tokens: a chars/4 heuristic over the actual rendered prompts (judge
  inputs use real stored solutions when present, otherwise a placeholder
  sized from the generation `max_tokens`);
- output tokens: the configured `max_tokens` cap, or pessimistic defaults
  when uncapped (4096/call for generation — with a printed
  `uncapped-generation` warning — and 512/call for judges without a cap);
- dollars: tokens × the pricing table, with a 0.5 multiplier for
  batch-eligible conditions.

The estimate covers the full policy-effective grid (resume state is not
subtracted) and is a planning number, not an invoice — target accuracy is
"within ~2× of actuals".

## The gate

`generate` and `grade` compare their stage's projection against the config:

1. projection > `budget.max_usd` → **abort, exit 4** — never overridable;
2. projection ≤ `confirm_above_usd` → proceed;
3. `--yes` → proceed;
4. interactive terminal → ask `Proceed? [y/N]`;
5. otherwise → **exit 3** with "re-run with --yes to confirm".

CI/scripting pattern: set `confirm_above_usd` to your comfort level, pass
`--yes`, and set `max_usd` as the backstop.

## Policies

| policy | items | batch | use for |
|--------|-------|-------|---------|
| `dev` (default) | first `dev_items` (2) | forced off | pipeline validation |
| `full-interactive` | all | off unless `batch: true` | runs you watch |
| `full-batch` | all | on (`batch: auto`) | large unattended runs, ~50% cheaper |

Batch mode flows through inspect_ai to the provider batch APIs (openai,
anthropic, google, grok, together). The recorded per-row cost applies a flat
0.5 multiplier for batch runs — a documented approximation; **provider
invoices are authoritative**, and the ledger records the `batch` flag so rows
can be re-priced.

## Pricing table

Lookup precedence:

1. `budget.pricing_path` (explicit JSON, relative to the config dir);
2. the user cache — `$ITEMEVAL_PRICING_PATH` or
   `~/.cache/itemeval/pricing.json`, written by `--refresh-pricing`;
3. the packaged seed (a handful of common models, point-in-time estimates —
   refresh before real runs).

`estimate --refresh-pricing` merges live per-token prices for every model on
the OpenRouter API over the seed. Models with no price are flagged
`unpriced` in estimates and carry null `usd` in stores — the run still works;
only cost attribution is missing.

`mockllm/*` models are deliberately priced (at claude-sonnet-class rates) so
demos and tests exercise the full dollar pipeline at $0 actual spend.

### Why OpenRouter as the refresh source

OpenRouter's `/models` endpoint is the only practical way to keep the long
tail fresh from a single call: it lists hundreds of models across providers in
one **public, keyless** response with a **uniform** `prompt`/`completion`
per-token schema. Native providers (OpenAI, Anthropic, Google) publish prices
on docs pages, not a stable machine-readable API, and would each need their own
auth and parser. Crucially, a refresh does **not** clobber curated native
prices: it writes `openrouter/<id>` keys and only fills a bare native id when
the seed lacks it (`seed wins for native ids`). The refresh is an estimate for
planning — the **provider invoice is authoritative**.

### Auto-refresh

Set `budget.pricing_max_age_days` to refresh the cached table automatically
once it ages past the threshold — no manual `--refresh-pricing`. It is:

- **opt-in** (default `None` = off), so no command makes a surprise network
  call and offline/CI runs are unaffected;
- **best-effort** — a failed fetch (offline, API change) keeps the existing
  table and never breaks a run;
- **ignored** when `budget.pricing_path` pins an explicit table (you chose it
  deliberately).

### Provenance (knowing which prices you got)

Because prices can come from a pinned file, a months-old seed, or a fresh
refresh, every cost-bearing command states where its numbers came from:

```
pricing: merged (updated 2026-06-08T00:00:00Z, 2d old) — just refreshed from OpenRouter
```

`estimate`, `generate`, `grade`, `export`, and `status` all print this line.
Programmatically the same provenance is on `Estimate.pricing` and
`ExportResult.pricing` (a `PricingProvenance`: `source`, `updated_at`,
`age_days`, `refreshed`), and `PreparedStudy.pricing_refreshed` flags whether a
live refresh ran during preparation.

## Provider prompt caching (input-side discounts)

Providers discount input tokens whose prefix they recently processed
(~75–90% off cache reads). itemeval schedules and shapes calls to hit these
caches, and reports the activity per condition (`cache_read=… cache_write=…
hit_rows=…` in run summaries; `cache_read_tokens`/`cache_write_tokens` on
every row).

What engages it (all validated live; numbers from the validation pilot via
OpenRouter):

- **Replications** share the full prompt across epochs. OpenAI-family models
  cache automatically (~79% input-side discount observed). For
  Anthropic-family models set `solvers.split_prompt: true` so the static
  template head becomes a system message carrying an explicit cache
  breakpoint (66–78% observed).
- **Judge fan-out**: set `graders.<name>.split_rubric: true` to render the
  shared head (rubric + problem + scheme + reference) as a system message and
  the solution as the user message. On an Anthropic judge this **halved the
  total judge bill** (78% input-side discount); the default monolithic layout
  cached nothing there. Either layout caches on OpenAI-family judges.
  Both `split_*` options change condition ids (the layout is part of the
  design cell).
- **Scheduling** (`budget.cache_schedule: auto`, the default): judge datasets
  are sorted so same-prefix calls are adjacent, and same-prefix groups run
  warm-then-fan-out (a leader writes the cache, followers read). It also
  routes byte-identical duplicate judge calls into the local response cache
  at $0. Disabled under batch mode.

Caveats: Anthropic-family models only cache prefixes above a per-model
minimum (1k–4k tokens — a too-short shared head silently caches nothing);
cache lifetimes are minutes, so the discount applies within a run, not across
days (cross-run reuse is the local response cache's job); and through
OpenRouter, routing can land on upstreams that ignore cache markers (e.g.
Bedrock) — pin the provider for serious Anthropic-cached runs.

## Where actual costs come from

Per-sample token usage from inspect logs × the pricing table, recorded on
every solutions/gradings row (`usd`) and aggregated per (run × stage ×
condition × model) in the ledger. Cache-served calls record `usd = 0.0`.
`export` verifies ledger totals equal row sums; checking the totals against
your provider dashboards is a manual step (expect the batch approximation
delta).

## Savings and per-provider spend

`export` re-prices the ledger's stored tokens at the current table to report
what the package saved versus a plain-API list price, plus a per-provider
breakdown (`ExportResult.cost`, a `CostReport`):

```
spend: generate $1.20 | grade $2.92
savings vs list price: $5.68 (58%) — cache $3.10, batch $2.58 (estimated; excludes resume / response-cache reuse)
provider   calls  spend   list_price  saved
anthropic  640    $2.92   $6.30       $3.38
openai     320    $1.20   $3.50       $2.30
```

Three cost points are computed per ledger row and summed:

| point | input pricing | batch | meaning |
|-------|---------------|-------|---------|
| `baseline` | every input token at the **full** rate | none | plain API, no caching/batch |
| `after_cache` | cached tokens at their discounted rates | none | caching on, batch off |
| `actual` | discounted cache rates | ×0.5 if batched | what you paid |

The savings decompose exactly: `cache_savings = baseline − after_cache`,
`batch_savings = after_cache − actual`, and the two sum to
`total_savings = baseline − actual`. This relies on inspect's accounting that
`input_tokens` **excludes** cached tokens (true input is
`input + cache_read + cache_write`), so the baseline re-adds the cache buckets
at the full rate.

**Scope and caveats:**

- **Cache writes are a surcharge, not a saving** (the first call pays ~1.25×
  input on the cached prefix); savings only accrue on later reads, so
  `cache_savings` can be negative on a run with little reuse.
- **Batch savings inherit the flat 0.5 approximation** — provider invoice is
  authoritative.
- **Resume / response-cache reuse is NOT counted.** A local-cache hit carries
  no usage object, so its ledger row holds null tokens and contributes zero to
  both `actual` and `baseline`. The figure therefore covers the prompt-cache
  and batch discounts only; counting reuse savings (a join back to the original
  run's tokens) is a planned follow-up.
- Reasoning tokens need no special handling — they sit inside `output_tokens`
  in both `baseline` and `actual`, so they cancel (a cost, not a saving).
- `unpriced` models are excluded from the figures and listed separately.
