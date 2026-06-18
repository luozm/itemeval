# Cost Savings — every way itemeval lowers your bill

itemeval has several built-in ways to spend less on the same study. This page
lists each one in plain terms: what it saves, what it costs you in return,
when to use it, and what's already on by default. (Deeper detail:
[Budget and Costs](Budget-and-Costs.md).)

## The short version

If you change nothing, you already get: never paying twice for completed work,
free re-runs, and smart call scheduling. If your study **repeats long text** —
the same question asked several times, or one rubric grading many answers —
turn on the two `split` options below and expect roughly **half the bill** on
those parts. If your run is large and you don't need results today, switch to
batch for another ~50% off.

## The options

### 1. Add judges without re-generating — free, automatic

Solutions are stored once. Adding another judge model or another rubric later
re-grades the *stored* answers — you never pay for generation again. This is
how itemeval works; nothing to configure.

### 2. Never pay for the same call twice — automatic

Strictly speaking this is insurance, not a discount: it makes *repeated* work
free rather than making necessary work cheaper. Your results themselves are
safe in the study's data files regardless — this is about the API calls.
Two layers:

- **Resume**: re-running a command skips work that's already done — completed
  calls aren't even attempted after an interruption or crash.
- **Call memo**: if an identical call *is* issued again (extending
  replications, `--force` after a fix, duplicate judge inputs), it's answered
  from your disk for $0 instead of re-billed. When this happens the run says
  so — `12 calls answered from local cache ($0) — cache dir: …` — and the
  run JSON carries `local_cache_rows`/`local_cache_dir`.

In practice this matters because iterating *is* the workflow — you will
re-run things, and none of it re-bills.
**Trade-off:** none. **Limit:** the memo lives on *your* machine — a
different computer starts fresh. **Exception (by design):** wave runs
(`--wave`) turn the memo off — re-observations must be fresh draws, so waves
never replay and always cost full price.

#### Never pay twice

The consequence for growing a study: pilot first, scale later, and the pilot
is never wasted money. `itemeval generate cfg.yaml --policy dev` runs a few
items without touching the config; re-running at full scope only pays for the
delta, because completed rows resume-skip and identical calls replay from the
local memo at $0. The `pilot-available` hint points here when the money gate
engages with no completed rows behind it.

### 3. Provider "seen this before" discounts — on by default, two opt-ins

Providers charge ~75–90% less for input text they processed moments ago. Two
things decide whether you actually get that discount:

**a) Call order (`budget.cache_schedule` — already on).**
The discount works like a toll transponder: the first call must register at
full price before the ones behind it get the fast lane. itemeval sends one
warm-up call per group, then the rest together. On OpenAI's own API it also
tags every scheduled request with a stable cache key per study and condition
and asks for 24-hour retention (free) — so your pilot in the morning still
discounts the full run in the afternoon. Nothing to configure.

This keyed caching applies to **direct** `openai/*` models only: the key is
`itemeval/<study>/<condition_id>` (stable across runs and phases, so it holds
routing affinity), and OpenAI's `24h` retention carries no surcharge. Names
pass through verbatim. `openrouter/openai/*` is excluded — OpenRouter doesn't
document forwarding these fields — so there it falls back to ordinary
same-prefix scheduling; either way the effect stays visible in the
`cache_read=…` numbers and the `cache-zero-reads` hint.

**b) Prompt packaging (`solvers.split_prompt` / `graders.<name>.split_rubric`
— off by default, recommended for repeat-heavy studies).**
Your prompt has a reusable part (instructions, rubric, problem) and a changing
part (the specific answer). These options send them as two pieces so the
provider can recognize the reusable part. Required for Anthropic models called
through OpenRouter; helpful everywhere. The model sees exactly the same text.
If you run an Anthropic model through OpenRouter *without* the split option,
itemeval says so up front (the `anthropic-openrouter-no-split` hint, at
estimate time) and projects full price — that combination verifiably gets no
discount at all.

**What we measured (real runs, June 2026):**

| Situation | Without | With | Money | Time |
|---|---|---|---|---|
| Ask 2 questions × 5 times each (OpenAI) | $0.038 / 26 s | $0.017 / 39 s | **−55%** | +13 s |
| Same, Anthropic, with both options on | $0.119 / 40 s | $0.055 / 50 s | **−54%** | +10 s |
| One judge grading 116 answers (Anthropic) | $0.840 / 72 s | $0.426 / 35 s | **−49%** | **−37 s** |

**The trade-off in one sentence:** on *small* runs the warm-up call adds a few
seconds in exchange for ~half price; on *big* runs you get both — cheaper
**and** faster, because discounted calls also skip re-reading the long text.

#### Two gotchas

The discount never applies to the model's *output* (its
answers are always full price — judge-style work benefits most); and providers
only discount reusable parts longer than a minimum (OpenAI ~1,000 tokens;
Anthropic ~500–4,000 depending on the model), silently doing nothing below
that. itemeval now checks this *before* you spend: if a split layout's shared
part estimates below the minimum, the `split-head-below-min` hint fires at
estimate time and on the run itself. After the run, check the
`cache_read=… hit_rows=…` numbers it prints — zeros on a big run mean the
discount isn't engaging (that situation also triggers the `cache-zero-reads`
hint).

### 4. Batch mode — ~50% off, but slow

`budget.policy: full-batch` sends calls through the provider's batch queue at
about half price. **Trade-off:** results take minutes to hours, with no live
progress — use it for large runs you'll collect later, never for iterating.
**Limit:** works with OpenAI/Anthropic/Google/Grok/Together directly; **not
through OpenRouter** — but if you sampled your models as `openrouter/…`,
`prefer_native_batch` can route them to their native API to get the discount
anyway (next section).

### 5. Guardrails — savings by prevention (on by default)

New configs run on the first 2 items (`dev` policy) until you scale up;
projected costs above `confirm_above_usd` ask first; `max_usd` is a hard stop
nothing can override; and `estimate` projects the bill with zero API calls.
One thing to know about projections: output is priced at your `max_tokens`
cap, since nothing else bounds it before the run. A generous cap (say, a
reasoning model with a fat budget producing short answers) over-states the
estimate — never under — so a real bill far below the projection usually
just means your cap is roomy.

### Native batch routing

Batch mode (above) needs a provider with a batch API, which **OpenRouter doesn't
have** — so a model sampled as `openrouter/anthropic/…` normally can't batch at
all, and on a judge-heavy study that is the single biggest discount left on the
table. Turn on `budget.prefer_native_batch` and, under a batch run, itemeval
routes each such model to its **native** API (`anthropic/…`, `openai/…`,
`x-ai`→`grok/…`, …) when you have that provider's key set — so the calls
actually receive the ~50% batch discount.

It is **opt-in on purpose.** Switching the serving endpoint can change a model's
outputs and mixes two endpoints in one study, so itemeval never does it silently
(the same reasoning as `provider_routing`). The model you sampled stays the model
of record: the `openrouter/…` id is what is pinned in `model_locks.json`, what
appears in the `model` column, and what condition ids hash — the native id is
recorded only as the *execution* id (`execution_model` in the run manifest), and
every run that routes prints `native batch routing: N model(s) → native API …`.
Routing is all-or-nothing per model and decided before the run starts, so
resuming is safe.

```yaml
budget:
  policy: full-batch
  prefer_native_batch: true
```

A model routes only when **all** of these hold: you are on a batch run, the knob
is on, the model is `openrouter/<provider>/…` for a provider whose native API
batches (Anthropic, OpenAI, Google, xAI→grok, Together), and that provider's API
key is in your environment. Anything else stays on OpenRouter untouched.

**Batch or cache — which is cheaper?** Both cut the same bill and you can only
pick one per run (batch reorders calls, which turns prompt caching off).
`estimate` shows the comparison per model:

```
native routing comparison (1 model(s) eligible; expected, remaining):
  openrouter/anthropic/claude-opus-4.8  native batch $4.20  ·  openrouter cache $7.90  → batch cheaper
```

Batch almost always wins, because it is ~50% off **everything, including output
tokens**, while caching only discounts the *repeated* part of the input and
never the output. Caching pulls ahead only in the narrow case of a huge shared
prefix with tiny output that you also cannot wait on (batch takes minutes to
hours). If you leave the knob off on a batch run where routing would help,
itemeval nudges you with the `native-batch-available` hint.

## Your own API key vs OpenRouter — which to call?

Both work in the same config; pick per model:

**Use the provider's own API (e.g. `openai/…`, `anthropic/…`) when…**
- you want the discounts above to work reliably (through OpenRouter, requests
  sometimes land on backends that ignore them);
- you want batch mode (OpenRouter has none — or set `prefer_native_batch` to
  route `openrouter/…` models to their native API automatically);
- you want one clean bill per provider with no marketplace fee.

**Use OpenRouter (`openrouter/…`) when…**
- you're comparing many models and want one key and one bill for all of them;
- the model has no direct account you own;
- the run is small enough that discounts don't matter anyway (dev/pilot).

Rule of thumb: **pilot wide on OpenRouter, run big and cached on direct
keys.**

### OpenRouter or direct?

If you do run cached Anthropic models through OpenRouter, pin the upstream —
OpenRouter is free to route your calls to hosts (Amazon Bedrock, Google
Vertex) that ignore the caching markers, and the only symptom is a silently
full-price bill. One config line fixes it (also available per grader):

```yaml
solvers:
  provider_routing: { order: [anthropic], allow_fallbacks: false }
```

The object is passed to OpenRouter verbatim, so anything from
[OpenRouter's provider-routing docs](https://openrouter.ai/docs/guides/routing/provider-selection)
works. itemeval reminds you when this matters: a cached
`openrouter/anthropic/*` run without it gets the `openrouter-unpinned-cache`
hint. And you can verify the pin held after any run: the run's manifest
records which host actually answered (`endpoints_effective` → `upstream`,
e.g. `"Anthropic"` vs `"Amazon Bedrock"`), and if the upstream changes
between runs of the same model, the next run warns you. In short: OpenAI, Grok, and Gemini models cache fine through OpenRouter
as-is; Anthropic and DeepSeek-style open models need the pin; OpenAI's keyed
caching and all batch APIs need a direct key.

## What's on by default

| Setting | Default | Change it when… |
|---|---|---|
| Free re-runs (`cache`) | on | almost never |
| Call scheduling (`budget.cache_schedule`) | on (`auto`) | a tiny latency-critical run (`off`) |
| Prompt packaging (`split_prompt` / `split_rubric`) | off | your study repeats long text — turn on for ~half price (note: starts fresh conditions, so decide before big runs) |
| Generation prompt caching (`solvers.cache_prompt`) | `auto` (on when replications > 1) | rarely |
| Batch (`budget.policy`) | off (`dev`) | large unattended runs → `full-batch` |
| Native batch routing (`budget.prefer_native_batch`) | off | batching `openrouter/…` models → `true` to route them to their native batch API |
| Budget gate / hard cap | $5 ask / no cap | set `max_usd` before every big run |
