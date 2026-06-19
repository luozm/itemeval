# Implementation plan — endpoint-context-clamp (clamp max_tokens to the smallest routed endpoint window)

**Status: IMPLEMENTED 2026-06-19.** This file is the design record for the
shipped feature (`Closes: endpoint-context-clamp`); the module names landed as
`src/itemeval/budget/_endpoint_windows.py` + `tests/test_endpoint_windows.py`
(the plan's `_endpoints.py` name was taken by the unrelated provider-routing
module). Written 2026-06-19 against inspect_ai 0.3.239 (pinned
in `uv.lock`) and the OpenRouter `/api/v1/models/:author/:slug/endpoints` API
(**[verify]** the response shape on first implementation — see Context). This
file is the working brief for a fresh implementation session; it carries all
context that session needs. Read first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract (knob buckets, hint
   framework, the money gate is the only gate, JSON parity, Law 1 side-effect
   announcement, append-only machine surface).
3. `DEVELOPMENT.md` — network/inspect boundary rules (network confined to the
   budget layer, like `_pricing.py`).
4. This file end-to-end before coding.

Scope: 2 workstreams. **W1** endpoint-window fetch + cache · **W2** feed the
min window into the generate clamp + announce.

---

## Context: the facts that decide the design

**The bug (reproduced live, g-theory-screen 2026-06-19).** The context-fit
clamp shipped in the `max_tokens`-context fix (`84964bd`, CHANGELOG
`[Unreleased] › Fixed`, "A small-context model in a mixed roster no longer
400s") clamps `max_tokens` against the model's `context_length` from the
pricing table. That number is OpenRouter's **model-level** `context_length` —
the *maximum across all providers serving the model*. OpenRouter routes a
request to whichever provider; a floor provider's window can be smaller. So:

- `openrouter/qwen/qwen-2.5-7b-instruct` → pricing table `context_length =
  131072` (verified in the user's `~/.cache/itemeval/pricing.json`,
  2026-06-18). Clamp computes `budget = 131072 − 157 − 256 = 130659 ≥ 32768` →
  **no clamp** → request goes out with `max_tokens = 32768`.
- Served endpoint window = 32768. `157 + 32768 = 32925 > 32768` → HTTP 400,
  `stop_reason=NaN`, error set, solution empty, 2/2 samples errored,
  `retry_on_error=1` wastes one extra attempt.

The original handoff misdiagnosed this as an *unknown* `context_length`; it is
**present**. The fix is to use a *truer* (smaller) window, not to handle a
missing one.

**Current clamp code (the thing we extend, do not replace).**
`src/itemeval/generate/_params.py`:

```python
def fit_max_tokens(requested, context_length, input_tokens) -> tuple[int|None, bool]:
    if requested is None or context_length is None:
        return requested, False
    budget = context_length - input_tokens - CONTEXT_FIT_MARGIN   # 256
    if budget >= requested:
        return requested, False
    return max(MIN_FIT_MAX_TOKENS, budget), True                  # MIN = 256
```

Call site — `src/itemeval/generate/_run.py` ~L585–L612 (inside `run_generate`,
the per-condition loop that builds tasks):

```python
price = lookup_price(prep.pricing, cond.model)
ctx_len = price.context_length if price else None
max_input = max((estimate_tokens(template.text) + estimate_tokens(it.input)
                 for it in items), default=0)
eff_max_tokens, clamped = fit_max_tokens(cond.gen_params.max_tokens, ctx_len, max_input)
if clamped:
    clamped_models[cond.model] = (cond.gen_params.max_tokens, eff_max_tokens, ctx_len)
```

The warning is aggregated at `_run.py` ~L785–L803 into `GenerateResult.warnings`
(`max_tokens clamped to fit context window for N model(s) … model req→eff (ctx C)`)
and printed by the CLI. `GenerateResult` is defined at `_run.py:103`; it already
carries `warnings: list[str]` (L113) and a local-cache summary pattern
(`local_cache_rows`/`local_cache_dir`) that the CLI prints via
`_print_local_cache` — **copy that pattern** for the endpoint-fetch announce.

**The estimator does NOT clamp.** `grep context_length src/itemeval/budget/_estimator.py`
→ no hits; it projects output cost from `cond.gen_params.max_tokens` directly
(`_estimator.py:840`, `:622`). The shipped clamp left this as a conservative
ceiling (a clamped model's *real* output ≤ requested, so the estimate
over-projects safely). **This feature keeps that precedent — the estimator is
untouched.** Rationale: clamping only fires for small-endpoint models, the
over-estimate is conservative (Agent-Guide "gate on the ceiling"), and adding
the network fetch to the estimate path would make `estimate` hit the network
for a figure that is already a safe upper bound. (Out of scope below.)

**Pricing layer (the model to copy for fetch + cache).**
`src/itemeval/budget/_pricing.py`: `refresh_pricing()` does the single
`urllib.request.urlopen(OPENROUTER_MODELS_URL)` call (L82–L89) and
`atomic_write_bytes(user_pricing_path(), …)` (L132). `user_pricing_path()`
(L62) → `~/.cache/itemeval/pricing.json` (overridable via
`ITEMEVAL_PRICING_PATH`). `lookup_price(table, model) -> ModelPrice | None`
(L204). `ModelPrice.context_length` (L35). Mirror these in `_endpoints.py`.

**OpenRouter endpoints API — [verify] on first implementation.** Expected:
`GET https://openrouter.ai/api/v1/models/<author>/<slug>/endpoints` returns
`{"data": {"id": …, "name": …, "endpoints": [{"name": …, "context_length":
<int>, "max_completion_tokens": <int|null>, "provider_name": …}, …]}}`. The
clamp ceiling is `min(e["context_length"] for e in endpoints if e.get("context_length"))`.
The model id on a condition is `openrouter/<author>/<slug>` (e.g.
`openrouter/qwen/qwen-2.5-7b-instruct`); strip the `openrouter/` prefix to form
the URL path. **Re-verify** the JSON keys against a live response (or the
OpenRouter API docs) and stamp the checked date in a code comment; only
`openrouter/*` models have an endpoints API — others → `None` (no clamp change).

---

## W1 — endpoint-window fetch + cache (`budget/_endpoints.py`)

**Goal.** Given a roster's distinct OpenRouter model ids, return each one's
minimum endpoint `context_length`, fetched once from OpenRouter and cached on
disk so warm runs cost zero calls.

**Config / public surface.** **No new knob** (optimization bucket — invisible
default). New module `src/itemeval/budget/_endpoints.py`. Cache file
`~/.cache/itemeval/endpoints.json` next to `pricing.json`, overridable via
`ITEMEVAL_ENDPOINTS_PATH` (mirror `ITEMEVAL_PRICING_PATH` for test isolation).

**Mechanism.**
- `EndpointWindows(BaseModel)`: `{updated_at: str, windows: dict[str, int|None]}`
  (`model_id → min context_length`; `None` recorded for a model whose endpoints
  API returned nothing, so we don't refetch it every run).
- `endpoint_min_context(model_id, *, timeout=30.0) -> int | None`: pure-ish
  single fetch for one model; returns `min` window or `None`. Non-`openrouter/*`
  id → `None` without a call.
- `load_endpoint_windows(model_ids, *, max_age_days, fetch=...) -> tuple[dict, FetchStats]`:
  read the cache, fetch only ids that are missing or staler than `max_age_days`,
  persist with `atomic_write_bytes`, return `({model_id: min_ctx}, stats)` where
  `stats` = counts of `fetched` / `reused` for the announce line. The `fetch`
  param is injectable so tests never hit the network.
- Staleness: a **fixed internal default** (e.g. `ENDPOINT_MAX_AGE_DAYS = 30` —
  endpoint windows change rarely), NOT a new config knob (keep it invisible).
  State the constant and reasoning in a comment.
- Network failure is non-fatal: on a fetch error, log nothing fatal, return the
  model as `None` (unknown window → clamp falls back to model-level ctx, i.e.
  today's behavior). A clamp feature must never *block* a run.

**UX contract.** The fetch is a Law-1 side effect (network + global-cache
write). Announce via `FetchStats` surfaced on `GenerateResult` (W2) — no line
printed from this module. **No hint, no gate.**

**Tests.** `tests/test_endpoints.py` (new): inject a fake `fetch` returning
per-provider windows; assert (a) min is selected across endpoints, (b) a
non-`openrouter/*` id is `None` with zero fetch calls, (c) warm cache (fresh
`updated_at`) → zero fetch calls, (d) a stale/missing id triggers exactly one
fetch and persists, (e) a fetch raising → recorded `None`, no exception
propagates. Use a `tmp_path` cache via `ITEMEVAL_ENDPOINTS_PATH`. No network.

**Docs/CHANGELOG.** Covered by W2's same-change entry (W1 ships with W2).

---

## W2 — feed the min window into the generate clamp + announce

**Goal.** The generate clamp uses `min(model_ctx, endpoint_min)` so a
small-endpoint model (qwen-2.5-7b) clamps and succeeds instead of 400ing.

**Config / public surface.** New append-only `GenerateResult` fields:
`endpoint_windows_fetched: int = 0`, `endpoint_windows_reused: int = 0`,
`endpoint_cache_dir: str | None = None` (JSON parity for the announce). No CLI
flag.

**Mechanism.**
- In `run_generate`, **before** the condition loop, collect the distinct
  `openrouter/*` models among `selected` conditions and call
  `load_endpoint_windows(...)`; keep the returned `{model_id: min_ctx}` dict and
  stats.
- At the clamp site, replace `ctx_len = price.context_length if price else None`
  with the tighter of the two:
  ```python
  model_ctx = price.context_length if price else None
  endpoint_ctx = endpoint_windows.get(cond.model)
  effective_ctx = min([c for c in (model_ctx, endpoint_ctx) if c], default=None)
  ```
  Feed `effective_ctx` into `fit_max_tokens`. The existing `clamped_models`
  record and warning then naturally report the effective (endpoint-min) window
  as `ctx C`.
- Set the new `GenerateResult` fields from the stats.

**UX contract.** **Law 1 announce line** (text rendering, printed by the CLI in
the generate summary block next to `_print_local_cache`):
`endpoints: fetched N model windows from OpenRouter → <cache_dir> (or M reused)`.
**JSON parity:** the three new fields. **Side-effect ledger:** add a row to the
`docs/UX-PATTERNS.md` ledger table (network → OpenRouter endpoints API; writes
`~/.cache/itemeval/endpoints.json`; line as above) **in the same commit**. The
clamp warning is unchanged in shape (already lists `ctx C`). **No new hint, no
gate** (the clamp already has no hint; a future `endpoint-clamp-truncation`
hint is an open question, not shipped here).

**Tests.** Extend `tests/` for the clamp: drive `run_generate` (or the pure
clamp-selection helper, factored out if cleaner) with a model whose
`price.context_length = 131072` but injected `endpoint_windows = {model: 32768}`
and assert `max_tokens` is clamped to fit 32768 (qwen repro, the regression
test the bug demands). Assert a model with no endpoint window falls back to
model-level ctx (today's behavior, unchanged). Assert the announce fields are
populated. Mock the endpoints fetch; **no paid API, no network** (CLAUDE.md:
unit tests never call paid/real APIs).

**Docs/CHANGELOG.** `CHANGELOG.md [Unreleased] › Fixed` (this closes a defect in
unreleased clamp behavior, so `Fixed` fits) with `Closes: endpoint-context-clamp`;
**remove** the `endpoint-context-clamp` section from `docs/BACKLOG.md`; add the
UX-PATTERNS ledger row; wiki: the clamp's owning page (Budget-and-Costs or
Cost-Savings — wherever the `max_tokens`-context note lives) gains a sentence on
the endpoint-min window + the truncation trade. Per the same-change rule, all in
the commit that lands W2.

---

## Sequencing (canonical)

1. **W1** — `_endpoints.py` + `tests/test_endpoints.py` (pure, network
   injectable). One `feat:` commit (or fold into W2's commit if small).
2. **W2** — wire into `run_generate`, `GenerateResult` fields, CLI announce
   line, clamp regression test, same-change docs (CHANGELOG `Closes:`, BACKLOG
   removal, UX-PATTERNS ledger row, wiki). One `feat:` commit carrying the
   user-visible change + its paperwork.

After each step: `make check` (lint + fast tests). On IMPLEMENTED: stamp this
file's status, `git mv` it to `docs/plans/archive/`, fix inbound links.

## Out of scope (explicitly, to prevent creep)

- **Estimator parity.** The estimator keeps using requested `max_tokens` (a
  conservative ceiling) — same as the shipped clamp. Revisit only if an
  estimate/run mismatch is reported (cf. the grade-scope Issue 3 lesson).
- **Provider-routing injection (mechanism B).** Auto-pinning OpenRouter
  `provider` routing to a large-enough window changes *which provider answers*
  — a study-author/reproducibility decision, not the package's. Rejected for
  this feature.
- **Fetching all ~685 models on pricing refresh.** Roster-scoped + cached only.
- **Don't-retry-the-400.** Making a deterministic context-overflow a terminal
  (non-retried) per-sample error is a separate, smaller robustness change
  (touches the inspect `retry_on_error` boundary); track in KNOWN-ISSUES if it
  recurs after this clamp, not here.
- **An `endpoint-clamp-truncation` hint** for when the min-window clamp shortens
  output on a big-window route — a candidate, not shipped; note in BACKLOG if
  demanded.
