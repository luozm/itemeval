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

## Where actual costs come from

Per-sample token usage from inspect logs × the pricing table, recorded on
every solutions/gradings row (`usd`) and aggregated per (run × stage ×
condition × model) in the ledger. Cache-served calls record `usd = 0.0`.
`export` verifies ledger totals equal row sums; checking the totals against
your provider dashboards is a manual step (expect the batch approximation
delta).
