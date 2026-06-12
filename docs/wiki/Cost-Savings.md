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
  from your disk for $0 instead of re-billed.

In practice this matters because iterating *is* the workflow — you will
re-run things, and none of it re-bills.
**Trade-off:** none. **Limit:** the memo lives on *your* machine — a
different computer starts fresh.

#### Never pay twice

The consequence for growing a study: pilot first, scale later, and the pilot
is never wasted money. `itemeval generate cfg.yaml --policy dev` runs a few
items without touching the config; re-running at full scope only pays for the
delta, because completed rows resume-skip and identical calls replay from the
local memo at $0. The `pilot-available` hint points here when a first paid
run hits the money gate.

### 3. Provider "seen this before" discounts — on by default, two opt-ins

Providers charge ~75–90% less for input text they processed moments ago. Two
things decide whether you actually get that discount:

**a) Call order (`budget.cache_schedule` — already on).**
The discount works like a toll transponder: the first call must register at
full price before the ones behind it get the fast lane. itemeval sends one
warm-up call per group, then the rest together.

**b) Prompt packaging (`solvers.split_prompt` / `graders.<name>.split_rubric`
— off by default, recommended for repeat-heavy studies).**
Your prompt has a reusable part (instructions, rubric, problem) and a changing
part (the specific answer). These options send them as two pieces so the
provider can recognize the reusable part. Required for Anthropic models called
through OpenRouter; helpful everywhere. The model sees exactly the same text.

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
answers are always full price — judge-style work benefits most); and Anthropic
only discounts reusable parts longer than ~1,000–4,000 tokens, silently doing
nothing below that. Check the `cache_read=… hit_rows=…` numbers itemeval now
prints on every run — zeros on a big run mean the discount isn't engaging
(that situation also triggers the `cache-zero-reads` hint).

### 4. Batch mode — ~50% off, but slow

`budget.policy: full-batch` sends calls through the provider's batch queue at
about half price. **Trade-off:** results take minutes to hours, with no live
progress — use it for large runs you'll collect later, never for iterating.
**Limit:** works with OpenAI/Anthropic/Google/Grok/Together directly; **not
through OpenRouter**.

### 5. Guardrails — savings by prevention (on by default)

New configs run on the first 2 items (`dev` policy) until you scale up;
projected costs above `confirm_above_usd` ask first; `max_usd` is a hard stop
nothing can override; and `estimate` projects the bill with zero API calls.

## Your own API key vs OpenRouter — which to call?

Both work in the same config; pick per model:

**Use the provider's own API (e.g. `openai/…`, `anthropic/…`) when…**
- you want the discounts above to work reliably (through OpenRouter, requests
  sometimes land on backends that ignore them);
- you want batch mode (OpenRouter has none);
- you want one clean bill per provider with no marketplace fee.

**Use OpenRouter (`openrouter/…`) when…**
- you're comparing many models and want one key and one bill for all of them;
- the model has no direct account you own;
- the run is small enough that discounts don't matter anyway (dev/pilot).

Rule of thumb: **pilot wide on OpenRouter, run big and cached on direct
keys.**

## What's on by default

| Setting | Default | Change it when… |
|---|---|---|
| Free re-runs (`cache`) | on | almost never |
| Call scheduling (`budget.cache_schedule`) | on (`auto`) | a tiny latency-critical run (`off`) |
| Prompt packaging (`split_prompt` / `split_rubric`) | off | your study repeats long text — turn on for ~half price (note: starts fresh conditions, so decide before big runs) |
| Generation prompt caching (`solvers.cache_prompt`) | `auto` (on when replications > 1) | rarely |
| Batch (`budget.policy`) | off (`dev`) | large unattended runs → `full-batch` |
| Budget gate / hard cap | $5 ask / no cap | set `max_usd` before every big run |
