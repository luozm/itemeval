# Implementation plan — native-batch-routing (route OpenRouter-sampled models to their native API to capture the batch discount; settle the batch-vs-cache "which is cheaper" question)

**Status: IN PROGRESS (started 2026-06-17; refreshed 2026-06-17 against the
shipped `expected-cost` estimator, commit `1c74fd5`).** Written against
inspect_ai 0.3.x (pinned in `uv.lock`) and the pricing/estimator code at the
commits below — re-verify the `[verify]` facts before coding. **`expected-cost`
landed first** (PR #7): `estimate_study` now runs a parallel *expected*
(calibrated) pass alongside the ceiling pass, so every discount helper has
**twice the call sites** it had when this plan was first drafted (W1 must thread
the execution id through both passes) and W2's dual projection now compares the
*expected* (realistic) figures rather than ceilings. This file is the working
brief for a fresh session: it carries all context that session needs. Read
first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract. The load-bearing rules for
   this feature: Law 1 (no silent side effects — routing the serving endpoint
   is a side effect that must be announced), Law 2 (advice never acts; nothing
   blocks but money — routing is opt-in, never a new gate), Law 5 (knob
   buckets — `prefer_native_batch` is an **optimization** knob), Law 6/7 (three
   renderings; append-only machine surface).
3. `DEVELOPMENT.md` — inspect_ai boundary rules. The routing decision is pure
   (config + pricing + env), so it lives in engine-free modules
   (`budget/`, `_prepare.py`); inspect stays confined to the orchestrators
   (`generate/_run`, `grade/_run`), which already own `inspect_ai.eval`.
4. `docs/COST-OPTIMIZATION.md` — the measured cost matrix this plan's
   batch-vs-cache analysis rests on; update it in the same change.
5. This file end-to-end before coding any part.

Scope: **2 workstreams**. **W1** native batch routing (the executable
feature). **W2** estimate-time dual projection — for each routable model,
`estimate` shows the **native-batch** projection alongside the **cache** projection
on its current OpenRouter endpoint, so the "which is cheaper" choice is visible
per-run (decided 2026-06-17 with the maintainer). The comparison is
informational; the run still executes one mode (batch when batch is on +
routed; OpenRouter-cache otherwise) — see W2 for why native-direct cache is not
a separately achievable mode here, and Out of scope for what stays out.

---

## Context: the facts that decide the design

### The motivation, in one paragraph

Models are sampled from the OpenRouter roster (`solvers.sample`,
`universe: pricing-table`), so a study's pinned model ids look like
`openrouter/anthropic/claude-haiku-4.5`. OpenRouter has **no batch API**, so
the dominant (grade) stage forgoes the ~50% batch discount the native
providers do offer. The five native batch providers are
`BATCH_PROVIDERS = {"openai", "anthropic", "google", "grok", "together"}`
([budget/_pricing.py:16](../../src/itemeval/budget/_pricing.py#L16)). Routing an
eligible OpenRouter id to its native equivalent recovers the largest single
cost lever while keeping the one-key OpenRouter convenience for everything else.

### The "which is cheaper: batch or cache?" question — settled

The user's directive: *native APIs also support cache — check which is cheaper.*
The answer is already measured in `docs/COST-OPTIMIZATION.md` and is decisive:

| Lever | What it discounts | Measured (this repo's pilots) |
|---|---|---|
| **Batch** | ~50% off **everything** — input **and output** | flat −50%, no layout/minimum constraints |
| **Prompt cache** | up to ~90% off the **repeated-prefix fraction of input only**; **output never discounted**; Anthropic adds a 1.25× write surcharge | judge fan-out with `split_rubric`: **−49%** ([COST-OPTIMIZATION.md:51-54](../../docs/COST-OPTIMIZATION.md#L51)) |

Conclusions that shape scope:

1. **Batch ≥ cache for itemeval's cost profile, and strictly wins on
   generation.** Batch also halves output tokens, which cache can never touch
   ("Output tokens are never discounted" — [COST-OPTIMIZATION.md:73](../../docs/COST-OPTIMIZATION.md#L73)).
   On the judge stage batch (−50%) edges cache (−49%) *and* is robust: no
   per-model prefix-minimum, no split layout, no write-surcharge math, no
   upstream-pin footgun.
2. **Caching is NOT a native-only lever.** itemeval *already* caches Anthropic
   models through OpenRouter today (`split_rubric`/`split_prompt` +
   `provider_routing` pin, validated live 2026-06-12). The lever native
   uniquely unlocks is **batch**. So "native cache vs native batch" reduces to
   "the discount you already have vs the new one" — and the new one is bigger.
3. **Stacking batch + cache is a separate, unverified question.** Anthropic's
   Message Batches API and OpenAI's batch both *can* coexist with caching at
   the provider, but itemeval disables cache scheduling under batch by
   construction (`scheduling = cache_schedule != "off" and plan.batch is None`,
   [_estimator.py:426](../../src/itemeval/budget/_estimator.py#L426)), and
   whether inspect's batch path places cache markers / the discount stacks
   is **[verify, live pilot]**. The marginal gain (cache only adds discount on
   the input prefix that batch already halved) does not justify the complexity
   in v1. Tracked as a follow-up, not built here.

**Therefore v1 routes native to capture the batch discount (W1), and shows the
batch-vs-cache trade-off per routable model at estimate time (W2)** so the
choice is visible, not buried in the wiki. The honest comparison is
**native-batch vs OpenRouter-cache** (the two *achievable* modes), not
"native batch vs native cache" — native-direct caching is not separately
reachable through this feature (routing only fires under batch; off-batch the
model stays on OpenRouter, where caching already works via `split_*` +
`provider_routing`). Still excluded: a comparator that *runs* a mix of modes in
one study, and stacking cache on top of batch (see Out of scope).

### Where the model id flows today (the chokepoints)

The **sampled OpenRouter id is the scientific identity** and must not move:

- `expand_grid` builds each `GenCondition.model` / `GradeCondition.grader_model`
  from `solvers.models` (post-sample) and hashes the condition id from
  `model_short(model)` + payload
  ([design/_grid.py:107](../../src/itemeval/design/_grid.py#L107),
  [:160](../../src/itemeval/design/_grid.py#L160)). **Endpoint identity has never
  been in the condition id** (confirmed: payload has no endpoint field) — routing
  must preserve this, exactly like `provider_routing` (an optimization knob that
  never enters ids).
- `model_locks.json` pins the sampled id; the `model` column in
  solutions/gradings is `cond.model`; drift checks key on it. All stay on the
  sampled id.

Execution flows through one chokepoint per stage:

- generate: `factory(cond.model, "generate", model_args_for(cond.model, ...))`
  ([generate/_run.py:495-505](../../src/itemeval/generate/_run.py#L495)).
- grade: `factory(cond.grader_model, "grade", model_args_for(cond.grader_model, ...))`
  ([grade/_run.py:338-348](../../src/itemeval/grade/_run.py#L338)).
- `factory` defaults to `resolve_model` (`_mockmodels.py`); `model_args_for`
  ([_endpoints.py:81](../../src/itemeval/_endpoints.py#L81)) shapes provider
  routing / cache keys per the model id.

Pricing/discount is computed off the model id in several places, all of which
must use the **execution** id (not the sampled id) once routing is on:

- estimate, **ceiling pass**: `_batch_discount(prep, model)` →
  `provider_of(model) in BATCH_PROVIDERS`
  ([_estimator.py:144](../../src/itemeval/budget/_estimator.py#L144)); applied in
  `_priced_usd` ([:224](../../src/itemeval/budget/_estimator.py#L224)),
  `_discounted_usd` ([:233](../../src/itemeval/budget/_estimator.py#L233)),
  `_condition_estimate` ([:270](../../src/itemeval/budget/_estimator.py#L270)).
- estimate, **expected pass** (NEW since `expected-cost` — the same helpers, more
  call sites): the per-condition `gen_exp_full`
  ([_estimator.py:566-572](../../src/itemeval/budget/_estimator.py#L566)) and
  `rem_exp_usd` ([:595](../../src/itemeval/budget/_estimator.py#L595),
  [:612](../../src/itemeval/budget/_estimator.py#L612)) for generate, and
  `grade_exp_full` ([:758-770](../../src/itemeval/budget/_estimator.py#L758)) +
  `rem_exp_usd` ([:783](../../src/itemeval/budget/_estimator.py#L783),
  [:798](../../src/itemeval/budget/_estimator.py#L798)) for grade. These call the
  same `_priced_usd`/`_discounted_usd` and so must take the execution id too —
  otherwise the *expected* projection (the headline realistic number) would miss
  the batch discount the ceiling shows.
- actuals: `usd_for_usage(..., model, batch)` applies `×0.5` when
  `provider_of(model) in BATCH_PROVIDERS`
  ([generate/_run.py](../../src/itemeval/generate/_run.py), `usd_for_usage`;
  re-verify the line — `expected-cost` shifted it).
- the ledger `provider` column = `provider_of(model)` (`ledger_row` in
  generate/_run.py) — should reflect who actually billed (the native provider
  when routed).

### The native-id resolution problem (the hard part)

OpenRouter ids and native inspect ids diverge in **two** ways, so a naive
prefix strip is wrong:

- **Provider segment differs:** OpenRouter `x-ai/grok-…` ↔ native inspect
  `grok/…`; OpenRouter `google/…` ↔ inspect `google/…` (same); `deepseek`,
  `anthropic`, `openai`, `together` match. `cache_provider_of`
  ([_endpoints.py:16](../../src/itemeval/_endpoints.py#L16)) already returns the
  **inner** provider segment — reuse it, then map to the native *inspect*
  provider prefix.
- **Model name spelling differs:** OpenRouter uses dots
  (`claude-haiku-4.5`); native Anthropic/inspect uses dashes and/or dated
  snapshots (`claude-haiku-4-5`, sometimes `…-4-5-20251001`). The pricing seed
  carries the dashed native ids (`anthropic/claude-haiku-4-5`) while an
  OpenRouter refresh stores the dotted spelling under both
  `openrouter/anthropic/claude-haiku-4.5` and bare `anthropic/claude-haiku-4.5`
  ([_pricing.py:128-130](../../src/itemeval/budget/_pricing.py#L128)). So the
  native execution id must be one the **pricing table can price** *and*
  **inspect can resolve**.

**Design decision (v1): a curated provider-prefix map + name normalization,
gated by pricing-table priceability — no inspect import in the resolver.**

- A small map `OPENROUTER_TO_NATIVE_PROVIDER = {"x-ai": "grok", "anthropic":
  "anthropic", "openai": "openai", "google": "google", "together": "together"}`
  (only the `BATCH_PROVIDERS` set; providers change far more slowly than
  models, so this is not the hand-maintained-model-list anti-pattern that
  `flagship-selection` rejected).
- Name normalization: dots→dashes for the model-name segment (covers the
  Anthropic case; identity for the others). **[verify]** each provider's native
  inspect spelling against `.venv` inspect provider code + each provider's docs
  at implementation time, and stamp the checked date in a code comment.
- **Priceability gate is the resolve-check proxy:** route only if
  `lookup_price(prep.pricing, native_id)` finds an entry. This keeps the
  resolver engine-free (no `get_model`), rides the pricing refresh (no separate
  staleness surface), and guarantees the routed run is costable. If the native
  id is not priceable, **do not route** and say so (a one-line note), never
  silently fall back to a wrong price.
- **Runtime safety net:** the orchestrator (which *may* import inspect) wraps
  the routed `factory(...)` call; if inspect cannot resolve the native slug it
  is an eval-level error already handled by the existing try/except
  ([generate/_run.py:519](../../src/itemeval/generate/_run.py#L519)) →
  reported, not crash. **[verify]** with a live single-model smoke that the
  curated native ids actually resolve before any paid run.

### Eligibility predicate (all must hold, per sampled model)

Route `m` to `native(m)` iff:
1. batch mode is on for the run (`prep.plan.batch is not None`) — routing only
   buys the batch discount, which only exists under batch;
2. `prefer_native_batch` is enabled (opt-in, default off — see UX);
3. `provider_of(m) == "openrouter"` and the inner provider
   (`cache_provider_of(m)`) maps to a `BATCH_PROVIDERS` native provider;
4. the native API key env var is present **[verify env names against inspect
   `.venv`]** (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`,
   `GROK_API_KEY`, `TOGETHER_API_KEY`); a pure `os.environ` check, no inspect;
5. `native(m)` is priceable (the resolve-check proxy above).

Routing is **all-or-nothing per model** and **decided once in `prepare_study`**
(resume-safe: the same config + env + pricing yields the same routing every
run). A model failing any check stays on its OpenRouter id, unrouted.

---

## W1 — native batch routing

**Goal.** When a batch run includes OpenRouter-sampled models whose native
provider offers a batch API, route those calls to the native id so they
actually receive the ~50% batch discount — recovering the study's single
largest cost lever — while the OpenRouter id remains the model's pinned
scientific identity. Opt-in, recorded, never silent.

**Config / public surface.**

- New knob `budget.prefer_native_batch: bool = False` — an **optimization**
  knob (Law 5): off by default; when on, eligible models route native under
  batch. *Why a knob and not an invisible default:* switching the serving
  endpoint can change model outputs and confounds an endpoint comparison across
  the study, so the user must opt in (the same reasoning that makes
  `provider_routing` explicit). The path to retiring it is *not* "flip the
  default" — it's the endpoint-drift warning making the confound visible; the
  knob stays a deliberate choice. Validation: plain bool; inert (warns, never
  blocks) when no model is routable — see UX.
- New result fields (append-only): `GenerateResult.routed_models` /
  `GradeResult.routed_models` — `list[NativeRoute]` where
  `NativeRoute = {sampled: str, execution: str, provider: str}`. `Estimate`
  gains the same on a new `routes: list[NativeRoute]` plus per-stage
  `native_route_savings_usd: float` (the projected delta the routing buys).
- Manifest: `endpoints_effective[cond.id]` already records the per-condition
  endpoint; add `execution_model` (native id when routed, else the sampled id)
  and `routed: bool`. No new top-level manifest field needed beyond echoing the
  knob through the existing `config` echo.
- No change to `model_locks.json`, condition ids, or the `model` column.

**Mechanism** (file:line level, simplest correct form):

- New pure module `budget/_routing.py`:
  - `native_id(sampled: str) -> str | None` — provider map + name
    normalization; `None` when the inner provider isn't a batch provider.
  - `resolve_native_routes(config, pricing, plan) -> dict[str, str]` — applies
    the 5-part eligibility predicate over `config.solvers.models` (post-sample)
    and the grader models, returns `{sampled_id: native_id}` for the routed
    subset (empty when `prefer_native_batch` off / not batch / nothing
    eligible). Pure: config + pricing + `os.environ` only, no inspect.
  - `NativeRoute` pydantic model for the result/manifest surface.
- `PreparedStudy` gains `native_routes: dict[str, str]` (default `{}`),
  populated in `prepare_study` after the sample resolves and pricing is loaded
  ([_prepare.py:98-99](../../src/itemeval/_prepare.py#L98)). A
  `routes_unavailable: list[str]` companion records eligible-but-unpriceable
  models for the inert/why-not note.
- Orchestrators look up the execution id at the chokepoint:
  `exec_model = prep.native_routes.get(cond.model, cond.model)` then
  `factory(exec_model, …, model_args_for(exec_model, …))`
  ([generate/_run.py:495](../../src/itemeval/generate/_run.py#L495),
  [grade/_run.py:338](../../src/itemeval/grade/_run.py#L338)). The `model`
  column / condition stay `cond.model`; pricing uses `exec_model`:
  - `usd_for_usage(prep.pricing, exec_model, usage, prep.plan.batch)`
    ([generate/_run.py:373](../../src/itemeval/generate/_run.py#L373),
    [grade/_run.py:190](../../src/itemeval/grade/_run.py#L190)).
  - `ledger_row(..., exec_model, ...)` so the `provider` column is the billing
    provider; **but** keep a `sampled_model` column or record the route in the
    report — decide: add `execution_model` to `endpoint_info` (it already takes
    `model`; pass `exec_model` and also stash the sampled id). Simplest: pass
    `exec_model` to `endpoint_info`, set `routed`/`execution_model` there.
- Estimator: thread the execution id into the discount across **both passes**.
  `_batch_discount`, `_priced_usd`, `_condition_estimate`, `_discounted_usd` use
  the execution id for `provider_of(...) in BATCH_PROVIDERS` and `lookup_price`,
  while `ConditionEstimate.model` stays the sampled id (the user reads the
  sampled id; the discount reflects the route). The cleanest threading: resolve
  `exec_model = prep.native_routes.get(cond.model, cond.model)` once per
  condition at the top of each loop and pass it wherever the discount/price is
  computed — the ceiling pass *and* the expected-pass call sites listed in
  Context (`gen_exp_full`/`grade_exp_full`/`rem_exp_usd`). Compute
  `native_route_savings_usd` over the routed conditions on the **expected**
  figures (the realistic number; report the ceiling delta too): expected cost
  un-routed (sampled id, no batch discount) − expected cost routed (native id,
  ×0.5).
- Routing is announced (Law 1) — see UX. No new module touches inspect; the
  resolver is unit-testable without a provider.

**UX contract** (binding — UX-PATTERNS checklist answered in full below):

- *Side effect (Law 1):* routing changes which provider account/endpoint
  serves the calls — a side effect outside the study dir. Add a **side-effect
  ledger row** and one provenance line, printed unconditionally on
  estimate/generate/grade when any route is active:
  `native batch routing: N models → native API (anthropic, openai) — sampled ids stay the scientific identity; native id recorded as execution_model`.
  Mirror the existing batch announcement style
  ([generate/_run.py:117-119](../../src/itemeval/generate/_run.py#L117)).
- *Quotable summary (Law 8):* the line above is self-contained with the count
  and providers.
- *The savings lever, surfaced first (estimate):* when `prefer_native_batch`
  could route but is **off**, `estimate` shows a hint-strength line —
  `hint: N models could route to their native batch API to save ~$X (set budget.prefer_native_batch) — learn more: Cost-Savings#native-batch-routing`
  (new coded hint `native-batch-available`, data-derived: fires only when batch
  is on, models are eligible+priceable, and the knob is off). When the knob is
  **on**, the projection line already reflects the discount and the provenance
  line states it.
- *Inert-knob warning (Law: no silent no-op):* `prefer_native_batch: true` with
  **no** eligible/priceable model warns once
  (`prefer_native_batch is set but no sampled model has a priceable native
  batch endpoint (checked: <list>) — inert`), mirroring `routing_warnings`
  ([_endpoints.py:116](../../src/itemeval/_endpoints.py#L116)).
- *Consent (Law 2/3):* routing **never** adds a gate. It changes the discounted
  projection the *existing* money gate already compares; nothing new blocks.
- *Knob bucket (Law 5):* optimization. Documented retirement path: none by
  flip (the endpoint confound is real); the knob is the consent surface.
- *JSON parity (Law 6):* `routes` / `routed_models` / `native_route_savings_usd`
  on the relevant result models; the new hint always rides the `hints` array.
- *Doc anchor (Law 6):* `docs/wiki/Cost-Savings.md#native-batch-routing` (new
  section) owns the explanation, incl. the batch-vs-cache comparison.
- *Append-only (Law 7):* new hint code `native-batch-available`, new JSON keys —
  all additive.

**Ledger / hint-catalog rows to flip in `docs/UX-PATTERNS.md` (same commit):**

- Side-effect ledger: new row — *Serving-endpoint routing* · where:
  `budget/_routing.py` decision, applied in the orchestrators · required line:
  the provenance line above.
- Hint catalog: new row — `native-batch-available` · fires when batch on,
  eligible+priceable native routes exist, `prefer_native_batch` off · example
  line above · owning doc Cost-Savings.

**Tests** (hermetic; no paid APIs):

- `tests/test_routing.py` (new): `native_id` spelling map (anthropic dots→dashes,
  x-ai→grok, passthrough cases, non-batch provider → None);
  `resolve_native_routes` over a fixture pricing table + monkeypatched
  `os.environ` — covering each eligibility clause (knob off, batch off, key
  missing, native unpriceable, happy path); all-or-nothing per model.
- Estimator: a config with an `openrouter/anthropic/*` model under
  `policy: full-batch` + `prefer_native_batch: true` projects the ×0.5 discount
  (vs no discount when off); `native_route_savings_usd` matches the delta;
  unpriceable native → no route, no discount, inert warning.
- Orchestrator (mock factory, no network): assert the factory is called with the
  **native** id while the written `model` column / condition id stay the sampled
  id; `endpoints_effective` records `execution_model` + `routed: true`; ledger
  `provider` is the native provider. Reuse the existing mock-model harness.
- Hint: `detect_native_batch_available` pure-function unit test.
- `tests/test_public_api_snapshot.py`: update the golden set for new exported
  names (`NativeRoute` if exported; new result fields) — deliberately, same
  change.

**Docs/CHANGELOG.** CHANGELOG `[Unreleased]` → `Added` entry with
`Closes: native-batch-routing`. Remove the `native-batch-routing` section from
`docs/BACKLOG.md`. In `ROADMAP.md`, move `native-batch-routing` out of the
"Later (vision-level)" list to the `**Already landed**` line of whatever
release it ships under (it is currently listed under *Later* and in 0.3's
neighborhood; confirm the target release with the maintainer at ship time).
New wiki section `Cost-Savings.md#native-batch-routing` (user-facing: what it
does, the knob, the endpoint confound, the batch-vs-cache comparison table).
Update `docs/COST-OPTIMIZATION.md` row 5 (batch) and the
"Direct API vs OpenRouter" section to note automatic native routing under the
knob, and resolve the cache-vs-batch open follow-up note. UX-PATTERNS rows as
above.

---

## W2 — estimate-time dual projection (native-batch vs OpenRouter-cache)

**Goal.** Make "which lever is cheaper for this model" visible at decision
time. For every routable model, `estimate` shows two achievable projections
side by side — route it native for the batch discount, or keep it on OpenRouter
and lean on prompt caching — so the user (or agent) can pick the run mode with
the numbers in front of them, instead of inferring it from the wiki.

**Why these two and not "native cache".** The two modes a user can actually
*get* for an `openrouter/anthropic/*` model are: (a) **native batch** — route +
`policy: full-batch` (W1), and (b) **OpenRouter cache** — stay on the sampled
id, non-batch, `cache_schedule: auto` (already shipped; needs `split_*` +
`provider_routing` to engage on Anthropic). Native-direct caching is a third
thing that this feature does not expose (routing fires only under batch; see
W1's eligibility predicate), so projecting it would advertise a mode the user
can't select — a Law-1/Law-8 honesty violation. W2 compares the two real
options.

**Config / public surface.** No new knob — W2 is pure projection. New
append-only fields on `Estimate`: `routes` already carries the routable set
(W1); add per-route projections to `NativeRoute`:
`batch_usd: float` (native-batch **expected** remaining cost) and
`cache_usd: float | None` (OpenRouter-cache **expected** remaining cost; `None`
when the model can't cache on its current endpoint — e.g. Anthropic monolithic
via OpenRouter, which is structurally full price). `cheaper: "batch" | "cache"`
records the verdict. Both numbers are the **expected (calibrated)** figures the
`expected-cost` pass already produces — comparing realistic costs, not ceilings
(at cold start expected == ceiling, so the comparison degrades gracefully).
These ride `Estimate.routes` only (not generate/grade results — the comparison
is a planning surface, like the rest of `estimate`).

**Mechanism.** Both numbers reuse the estimator machinery already in
`estimate_study` — specifically the **expected pass** `expected-cost` added; the
work is computing each as a *counterfactual* independent of the current
`plan.batch` flag, over the **remaining** scope (matching every other figure the
gate sees):

- **batch_usd** — the routed-native *expected* figure W1 already computes for the
  routed conditions (`rem_exp_usd` with `exec_model` + `×0.5`); sum it over both
  stages per model.
- **cache_usd** — the *expected* cache-split projection on the **sampled
  (OpenRouter) id** with `scheduling=True` forced (the counterfactual "if you ran
  this non-batch with caching"), reusing `_cache_split`
  ([_estimator.py:246](../../src/itemeval/budget/_estimator.py#L246)) +
  `_discounted_usd` ([:233](../../src/itemeval/budget/_estimator.py#L233)) over
  the calibrated token figures, honoring the same eligibility the runtime would
  (`or_mono` suppression for Anthropic-monolithic-via-OpenRouter → that model's
  `cache_usd` is the undiscounted figure, correctly showing cache buys nothing
  without `split_*`). When the model already has `split_*` on, this is the real
  −49%-class number.
- Refactor (more valuable now): `expected-cost` already **duplicated** the
  cache-eligibility + group logic across the ceiling and expected passes inline
  in both loops (e.g. the `would_cache`/`or_mono`/`cache_groups` block runs once
  but feeds two cost computations). W2 adds a *third* evaluation (the
  counterfactual), so lift the per-condition cache-eligibility + split + cost
  into a small helper parameterized by `(model_id, scheduling_flag, output_tokens
  resolver)` so all three (ceiling, expected-live, expected-counterfactual) call
  one path. Keep it in `_estimator.py` (no new module). A golden-value regression
  must show the live ceiling **and** expected numbers are byte-identical
  pre/post-refactor.
- `cheaper` = `"batch"` when `batch_usd < cache_usd` (or `cache_usd is None`),
  else `"cache"`.

**UX contract.** A compact comparison block in `estimate`'s text rendering,
after the stage lines, only when routable models exist:

```
native routing comparison (2 models eligible; expected, remaining scope):
  openrouter/anthropic/claude-haiku-4.5  native batch $0.42  ·  openrouter cache $0.83  → batch cheaper
  openrouter/openai/gpt-5-mini           native batch $0.19  ·  openrouter cache $0.21  → batch cheaper
```

- Strength: **informational summary lines** (Law 8 quotable, numbers
  self-contained) — never a hint, warning, or gate; never changes behavior.
- JSON parity (Law 6): the per-route `batch_usd`/`cache_usd`/`cheaper` fields on
  `Estimate.routes`. The text block and JSON carry the same numbers.
- Doc anchor: the same `Cost-Savings.md#native-batch-routing` section; the live
  comparison is the user-facing form of the analysis table.
- Interaction with W1's `native-batch-available` hint: when the knob is off and
  batch *would* be cheaper, the hint still fires (W1); W2's block makes the
  magnitude concrete. When `cache` is cheaper for every routable model, the
  hint does **not** fire (data-derived — don't advise routing that loses).

**Tests.** Estimator unit tests over a fixture config with an
`openrouter/anthropic/*` model: (1) monolithic → `cache_usd` equals
undiscounted, `cheaper == "batch"`; (2) `split_rubric: true` with a head above
the minimum → `cache_usd` reflects the split discount, `cheaper` follows the
arithmetic; (3) an `openrouter/openai/*` model (auto-caches) → both numbers
populated. Assert the lifted helper yields byte-identical live-plan numbers —
**both the ceiling `usd` and the expected `expected_usd`** — to the pre-refactor
estimator (golden-value regression) so W2's refactor doesn't perturb existing
projections (the `expected-cost` tests are the regression baseline to keep green).

**Docs/CHANGELOG.** Folded into W1's `Closes: native-batch-routing` entry
(one feature, two workstreams). The wiki section gains the live-comparison
example; UX-PATTERNS needs no new ledger/hint row for W2 (informational lines
under the existing estimate surface).

---

## Sequencing (canonical)

1. `budget/_routing.py` (pure: `native_id`, `resolve_native_routes`,
   `NativeRoute`) + `tests/test_routing.py`. No other module depends on its
   internals yet.
2. Wire `PreparedStudy.native_routes` in `_prepare.py`.
3. Estimator W1: thread the execution id through the discount; add
   `native_route_savings_usd` + `routes`; the `native-batch-available` hint
   (`_hints.py` detector). Tests.
4. Estimator W2: lift the cache-eligibility/split helper to take
   `(model_id, scheduling)`; compute `batch_usd`/`cache_usd`/`cheaper` per
   route (consumes W1's `routes`). Golden-value regression that live-plan
   numbers are unchanged. Tests.
5. Orchestrators: execution-id lookup at the chokepoint; pricing/ledger/
   endpoint_info on the execution id; `routed_models` on the results; the
   provenance line. Tests with the mock factory.
6. CLI: render the provenance line + the savings hint + the W2 comparison block
   on the relevant commands; `--json` parity.
7. Same-change paperwork: CHANGELOG, BACKLOG removal, ROADMAP move, wiki +
   COST-OPTIMIZATION + UX-PATTERNS, public-API snapshot.

After each step: `make check` (lint + fast tests), CHANGELOG and normative doc
tables updated in the same commit.

## Out of scope (explicitly, to prevent creep)

- **Per-model execution-mode *mixing* at runtime** (running some models batch
  and others cached *within one study run*). Batch is a single plan-level flag
  and cache is globally gated off under batch; per-model plans/scheduling is a
  large architectural change. W2 *shows* the per-model comparison at estimate
  time; acting on it is the user's choice of one run mode (batch policy, or
  not), not a within-run mix. Out of scope here.
- **Native-direct caching as a selectable mode** (routing native when *not*
  batching, to chase direct-API caching). Native cache only adds discount on
  input the batch already halves, OpenRouter already caches Anthropic via
  `split_*` + `provider_routing`, and exposing it would make W2 advertise a
  third mode the feature can't deliver. Out of scope; W2 compares only the two
  achievable modes.
- **Stacking native cache on top of native batch.** Provider support exists in
  principle but inspect's batch path + marker placement is unverified, and the
  marginal gain is small (cache only re-discounts input the batch already
  halved). Tracked as a `[verify, live pilot]` follow-up; keep the current
  "batch disables cache scheduling" invariant.
- **Curated full model-id map maintained by hand.** Rejected for the same
  reason `flagship-selection` rejects hand-maintained model lists; the
  provider-prefix map + name normalization + pricing-table priceability gate is
  the drift-resistant substitute (only the ~5 provider prefixes are curated).
- **Louder cross-model endpoint-confound warning** (mixing native + OpenRouter
  endpoints in one study). The existing endpoint-drift warning covers
  served-model drift; a dedicated confound warning is a possible follow-up,
  noted in the BACKLOG open questions, not built here.
