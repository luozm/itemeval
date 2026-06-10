# Tutorial 5 — Scale up without surprises

**Use case:** "The pipeline works at `dev` scope. Now I want the full run —
hundreds or thousands of paid calls — without discovering the bill afterward."

itemeval's budget layer exists so that scaling up is a config change, not a
leap of faith: estimate first, gate on a threshold, cap hard, batch for ~50%
off, and resume anything that breaks. You will take a validated study (any of
Tutorials 1–4; examples below use Tutorial 3's `compare.yaml`) to full scope.

## Step 1 — Refresh pricing and re-estimate at full scope

Switch the policy from `dev` to a full run and set the guardrails:

```yaml
budget:
  policy: full-batch        # all items, batch APIs on (~50% cheaper)
  confirm_above_usd: 5      # ask before anything projected above $5
  max_usd: 25               # hard cap: abort if projection exceeds this. Never overridable.
```

Then:

```bash
itemeval estimate compare.yaml --refresh-pricing
```

`--refresh-pricing` pulls current per-token prices (from the OpenRouter
catalog) into a local cache, so projections use today's prices — do this
before any sizeable run. Read the estimate's per-condition breakdown and its
warnings; in particular, an `uncapped-generation` warning means you forgot
`max_tokens` and the estimator had to assume a pessimistic default. The
estimate always projects the **full** grid (it doesn't subtract completed
work) — a deliberate, conservative planning number targeted to be within ~2×
of actuals.

To keep prices fresh automatically instead, set
`budget.pricing_max_age_days: 7` — every cost-bearing command prints which
pricing table it used either way.

## Step 2 — Understand the three policies and the gate

| policy | items | batch APIs | use for |
|--------|-------|-----------|---------|
| `dev` (default) | first `dev_items` | forced off | pipeline validation |
| `full-interactive` | all | off unless `batch: true` | runs you watch |
| `full-batch` | all | on (`batch: auto`) | large unattended runs |

When you run `generate` or `grade`, the projection meets the gate, in order:

1. projection > `max_usd` → **abort** (exit 4). `--yes` does *not* override.
2. projection ≤ `confirm_above_usd` → proceed.
3. otherwise → interactive `Proceed? [y/N]`, or exit 3 if there's no TTY.

The scripting/CI pattern: set `confirm_above_usd` to your comfort level, pass
`--yes`, and let `max_usd` be the backstop that no flag can talk past.

## Step 3 — Run it (batched, unattended-safe)

```bash
itemeval generate compare.yaml --yes
itemeval grade    compare.yaml --yes
```

Under `full-batch`, eligible providers (OpenAI, Anthropic, Google, Grok,
Together) receive the calls through their batch APIs at roughly half price —
slower, but built for exactly this. Judge grading additionally benefits from
provider prompt caching: the rubric + problem prefix repeats across solutions,
so repeated prefixes are served from cache where the provider supports it.

**Interruptions are a non-event.** Ctrl-C, a crash, a rate-limit storm, an
expired session — re-run the same command. The stores are keyed, so completed
work skips, errored samples retry, and inspect_ai's local response cache means
already-paid calls are never paid twice (re-served rows record `usd = 0.0`).
Check progress any time with:

```bash
itemeval status compare.yaml    # done/expected per condition, errors, spend so far
```

If a few samples keep erroring, the run still completes (exit 1, failures
reported per condition) — `status` and the stores tell you exactly which rows
are missing; see [Error Handling](Error-Handling.md).

## Step 4 — Audit what you actually spent

```bash
itemeval export compare.yaml
```

Beyond the data tables, `export` settles the books:

```
spend: generate $1.20 | grade $2.92
savings vs list price: $5.68 (58%) — cache $3.10, batch $2.58 (estimated)
provider   calls  spend   list_price  saved
anthropic  640    $2.92   $6.30       $3.38
openai     320    $1.20   $3.50       $2.30
```

- Per-sample costs are recorded on every row (`gen_usd`, `grade_usd`); the
  per-run **ledger** aggregates them by stage × condition × model, and export
  verifies ledger totals equal row sums.
- The **savings report** re-prices your tokens at the plain-API list price and
  splits what the package saved into prompt-cache and batch components.
- Batch rows use a documented flat 0.5× approximation — the **provider invoice
  is authoritative**; the ledger records the `batch` flag so rows can be
  re-priced.

## Step 5 — Pilot-then-scale, the recommended lifecycle

The pattern the package is built around:

1. **dev** — `policy: dev`, mock or cheap models, 2–10 items. Validate
   mapping, prompts, rubric, parsing. Cost: ≈ $0.
2. **pilot** — real models, `dev_items: 20`–50, `full-interactive` if you want
   to watch. Sanity-check estimate-vs-actual, per-item output quality, and
   judge parse rates.
3. **full** — `full-batch`, `--yes`, `max_usd` set. Walk away; `status` when
   curious; `export` when done.

Each step reuses the previous step's completed work where conditions overlap —
nothing you validated is re-paid (note that `dev` runs the *first N* items, so
its items are a subset of the full run).

## Gotchas at scale

- **Reasoning models with tight `max_tokens`** can burn the whole budget on
  hidden reasoning and return empty text — not an error, so it won't retry by
  default. itemeval surfaces these (`status`'s `empty` column) and
  `solvers.on_empty: rerun` makes them re-attempt after you raise the cap
  ([Configuration](Configuration.md)).
- **Unpriced models** (not in the pricing table) run fine but carry null `usd`
  — `estimate` flags them up front; refresh pricing or supply
  `budget.pricing_path`.
- **Don't run two commands concurrently** against the same study directory;
  stages are designed to run serially.
