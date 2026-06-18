# Implementation plan — sample-exclude (id blocklist + non-free roster by default)

**Status: IMPLEMENTED 2026-06-18.** Written 2026-06-18 and shipped the same day
(branch `feat/sample-exclude`, 0.3.0.dev); this file is now the design record,
past tense. It described the two workstreams as built: `solvers.sample.exclude`
and the non-free `pricing-table` roster. The feature **extends the unreleased
`model-sampling`** (in `[Unreleased]`, ships with 0.3; design records in
[docs/plans/archive/model-sampling.md](archive/model-sampling.md) and
[docs/plans/archive/model-sample-composition.md](archive/model-sample-composition.md)).
It is **engine-free** — config, the draw module, the manifest, the study card;
**no** `inspect_ai` import, so the inspect boundary (DEVELOPMENT.md) is satisfied
trivially and is not re-discussed below. Read first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style,
   "don't over-engineer").
2. [docs/UX-PATTERNS.md](../UX-PATTERNS.md) — **binding** UX contract.
   Load-bearing here: Law 1 (no silent side effects — `include ∩ exclude` must
   fail loudly, never silently let one win), Law 5 (knob buckets — `exclude` is
   a **design declaration**, never auto-flipped), Law 6 (three renderings —
   excluded ids enter provenance so the study card can attest them), Law 7
   (append-only machine surface).
3. [docs/plans/archive/model-sample-composition.md](archive/model-sample-composition.md)
   — the most recent extension of the same draw/lock machinery; its `include`
   workstream (W3) is the direct sibling of this plan's `exclude`.
4. This file end-to-end before coding — the two workstreams share the
   universe-build path.

Scope: **2 workstreams.** **W1** top-level `exclude` (id blocklist, sibling of
`include`, works on every universe type) · **W2** non-free roster by default
(the `pricing-table` universe drops `$0` models at build time — no key, folds
into the unreleased `model-sampling` story).

This plan **supersedes** the original two-feature request
(`local/itemeval_request_where_exclude_nonfree.md`): `where.exclude` becomes the
top-level `exclude` in W1, and `where.free` is **dropped entirely** — W2 makes
the roster non-free by construction, so a `free` filter is unnecessary, and
"draw only free models" is something we design *against* (see Out of scope).

---

## Context: the facts that decide the design

### What already exists (model-sampling + model-sample-composition, unreleased)

The whole draw is engine-free in
[_modelsample.py](../../src/itemeval/_modelsample.py); config lives in
[_config.py](../../src/itemeval/_config.py). The load-bearing facts:

- **`ModelSample`** ([_config.py:126-185](../../src/itemeval/_config.py#L126-L185))
  carries `n`, `seed`, `stratify_by`, `allocation`, `include`
  ([:147](../../src/itemeval/_config.py#L147), `list[str]`, top-level),
  `universe`, `where`. `_check`
  ([:151-185](../../src/itemeval/_config.py#L151-L185)) validates: `include`
  uniqueness ([:178](../../src/itemeval/_config.py#L178)), `include ≤ n`
  ([:180](../../src/itemeval/_config.py#L180)), and the roster-only gate on
  `where` ([:163-167](../../src/itemeval/_config.py#L163-L167)). `model_config =
  ConfigDict(extra="forbid")` — a new field MUST be declared on the model or the
  config won't load.
- **`ModelUniverseFilter`** (`where`,
  [_config.py:93-124](../../src/itemeval/_config.py#L93-L124)) is roster-only and
  **rejected for list/file universes**
  ([:163-167](../../src/itemeval/_config.py#L163-L167)). This is the decisive
  reason `exclude` does NOT belong in `where`: an id blocklist is universe-type
  agnostic (you want "this curated 50-id list, minus 3" too), and an exact-id
  list is not a roster-metadata filter. `exclude` goes top-level beside
  `include`, which already works on every universe type.
- **`_build_universe(sample, pricing, input_base)`**
  ([_modelsample.py:140-184](../../src/itemeval/_modelsample.py#L140-L184)) →
  `(source, sorted unique ids)`, three branches:
  - inline list → `("explicit", sorted(set(universe)))`
    ([:145-146](../../src/itemeval/_modelsample.py#L145-L146));
  - `pricing-table` → `openrouter/*` keys with `p.text_model` set
    ([:151](../../src/itemeval/_modelsample.py#L151)), then `_apply_where` if
    `where` is set ([:158-159](../../src/itemeval/_modelsample.py#L158-L159)),
    with a dedicated empty-frame error
    ([:160-171](../../src/itemeval/_modelsample.py#L160-L171));
  - file → one id per line ([:173-184](../../src/itemeval/_modelsample.py#L173-L184)).
- **`_apply_where`**
  ([_modelsample.py:113-137](../../src/itemeval/_modelsample.py#L113-L137)) —
  keep-iff per `where` field; **no exclude, no price floor** today.
- **`_price_tier`**
  ([_modelsample.py:56-66](../../src/itemeval/_modelsample.py#L56-L66)) — the
  documented free edge is `out_usd <= 0 → "free"`. W2 reuses exactly this edge.
- **`_draw`** ([_modelsample.py:246-291](../../src/itemeval/_modelsample.py#L246-L291))
  — `include = sorted(set(sample.include))`
  ([:258](../../src/itemeval/_modelsample.py#L258)), `fill_pool =
  sorted(set(universe) - set(include))`
  ([:259](../../src/itemeval/_modelsample.py#L259)), and the draw begins
  `drawn = list(include)` ([:286](../../src/itemeval/_modelsample.py#L286)) —
  i.e. **`include` is added unconditionally after the draw and bypasses the
  universe.** Consequence: an id in both `include` and `exclude` would have
  `include` silently win (the excluded id reappears). W1's overlap validation
  exists to make that contradiction fail loudly (Law 1), not be resolved
  silently.
- **The lock spec** that must match for reuse is built at
  [_modelsample.py:349-357](../../src/itemeval/_modelsample.py#L349-L357):
  `{source, n, seed, stratify_by, allocation, include, where}` (with `include`
  sorted, `where` as `where.model_dump()`). A changed spec **fails loudly**
  ([:361-366](../../src/itemeval/_modelsample.py#L361-L366)); a changed
  `universe_hash` only **warns** (`universe_drift`, the frozen draw stands).
- **`ModelSampleResult`**
  ([_modelsample.py:26-41](../../src/itemeval/_modelsample.py#L26-L41)) records
  `include` ([:38](../../src/itemeval/_modelsample.py#L38)) and is surfaced in
  every rendering (estimate/generate/grade/status JSON, manifest, study card) by
  model-sampling. `exclude` mirrors `include` here for provenance parity.

### Why save-time filtering (the original ask) is the wrong layer

The original request proposed dropping free models (and deduping) inside
[`refresh_pricing`](../../src/itemeval/budget/_pricing.py#L82-L136), i.e. at
write time. **Rejected.** The pricing table has a *second* role beyond the
sampling roster: it is the price-lookup store for **any** model a user runs.
[`lookup_price`](../../src/itemeval/budget/_pricing.py#L192-L201) returning
`None` makes a model **unpriced** — it contributes `0` to the estimate but is
flagged in `unpriced_models`
([_estimator.py:250-251](../../src/itemeval/budget/_estimator.py#L250-L251),
[:930](../../src/itemeval/budget/_estimator.py#L930)). Deleting free entries at
save time would therefore break the very escape hatch the request wants to keep
("a user who wants a free model names its endpoint directly"): that explicit
`openrouter/foo:free` would show up as *unpriced/unknown* instead of correctly
`$0`. The table must stay a faithful catalog mirror; "which models are
**drawable**" is a consumer decision that belongs in `_build_universe`, next to
the existing `text_model` filter. W2 follows that rule.

### No migration cost

`model-sampling` is **unreleased** (in `[Unreleased]`, ships with 0.3), so there
is no released roster contract or lock format to preserve. W2's roster change
and W1's new `exclude` spec field can land freely before 0.3; a pre-existing
local `model_locks.json` whose spec/universe now differs fails loudly (or warns
on drift) telling the user to clear it — the already-shipped guard behavior.

---

## W1 — top-level `exclude` (id blocklist)

**Goal.** Let a draw remove specific model ids from any universe before
sampling — e.g. drop the judge model-ids from a solver panel for rater–object
independence. `include` adds purposive pins; `exclude` is its inverse, and like
`include` it works for `pricing-table`, file, and inline-list universes.

**Config / public surface.** One new field on `ModelSample`
([_config.py:126-185](../../src/itemeval/_config.py#L126-L185)), beside
`include`:

```python
# Remove these exact model-ids from every universe before drawing (e.g. judge
# ids, for rater-object independence). Exact match; ids absent from the
# universe are a no-op. The inverse of `include`, which *adds* purposive pins;
# unlike `where`, it is not roster-only.
exclude: list[str] = Field(default_factory=list)
```

**UX-PATTERNS bucket:** **design declaration** (it changes the drawn set, hence
the grid) — always explicit, never auto-flipped. Same bucket as `include`.

**Validation** — add to `ModelSample._check`
([_config.py:151-185](../../src/itemeval/_config.py#L151-L185)), mirroring the
`include` checks:
- reject duplicate `exclude` entries (parallel to
  [:178](../../src/itemeval/_config.py#L178));
- reject empty-string entries;
- reject `set(include) & set(exclude)` overlap (`ValueError`/`ConfigError`): an
  id cannot be both pinned and blocked — without this, `include` silently wins
  (see Context, `_draw`), violating Law 1.
- **No** roster gating — `exclude` is valid for list/file universes too (the
  whole point). Do not copy the `where` gate at
  [:163-167](../../src/itemeval/_config.py#L163-L167).

**Mechanism.** Apply `exclude` as a single shared tail in `_build_universe`
([_modelsample.py:140-184](../../src/itemeval/_modelsample.py#L140-L184)) so all
three branches get it uniformly: have each branch compute `(source, ids)`
(keeping its existing pre-checks and per-branch empty errors), then a shared
trailing step subtracts the blocklist and does one final empty check:

```python
exclude = set(sample.exclude)
ids = [k for k in ids if k not in exclude]
if not ids:
    raise ConfigError(
        "solvers.sample.exclude removed every model from the universe — "
        "loosen exclude or widen the universe"
    )
return source, sorted(set(ids))
```

Place the subtraction *after* `_apply_where` for the roster branch (so the
existing where-empty message still fires for the where case). The simplest
correct shape is one tail; do not duplicate the subtraction into three returns.

**Lock spec + provenance.** Add `"exclude": sorted(sample.exclude)` to the spec
dict ([_modelsample.py:349-357](../../src/itemeval/_modelsample.py#L349-L357))
so editing `exclude` trips the spec-change re-draw guard
([:361-366](../../src/itemeval/_modelsample.py#L361-L366)). The post-exclude
universe also flows into `universe_hash` automatically. Add an `exclude:
list[str] = Field(default_factory=list)` field to `ModelSampleResult`
([:26-41](../../src/itemeval/_modelsample.py#L26-L41)) and populate it in both
return paths, mirroring `include` exactly — this is append-only (Law 7) and lets
the study card / manifest attest which ids were blocked (the rater–object
independence claim). Surface it wherever `include` is surfaced; keep it to a
one-line addition per renderer (no new rendering shape).

**UX contract.** No new gate (money is the only gate). The lock write is already
announced. `exclude` joins the existing `model_sample` JSON object / manifest
block / study-card front-matter as an append-only field beside `include`. Flip
the relevant `model-sampling` rows in
[docs/UX-PATTERNS.md](../UX-PATTERNS.md) only if the ledger enumerates
`include`/`where` explicitly (check; if it lists the sample knobs, add
`exclude`).

**Tests** (`tests/`, alongside the existing model-sample tests):
1. `exclude` removes listed ids from a `pricing-table` draw; unlisted ids
   unaffected; a non-roster id is a no-op.
2. `exclude` works on an **inline-list** universe and on a **file** universe
   (proves it is not roster-gated — the key difference from putting it in
   `where`).
3. `include ∩ exclude` raises at load (`ConfigError`/`ValueError`).
4. Duplicate `exclude` entries, and empty-string entries, raise at load.
5. `exclude` enters the lock `spec`; editing `exclude` trips the spec-change
   re-draw guard; `ModelSampleResult.exclude` is populated.
6. Combined: `stratify_by: recency` + `released_after` + `max_output_usd_per_mtok`
   + `exclude: [<judge ids>]` yields a year-stratified frame containing none of
   the excluded ids (and, with W2, no `$0` models). All pure/engine-free — no
   paid API.

**Docs/CHANGELOG (same commit as the behavior).**
- `CHANGELOG.md` `[Unreleased]` → new `Added` entry for `exclude` with a
  `Closes: sample-exclude` trailer. (`exclude` is a feature → it leaves BACKLOG
  in this same commit; remove its section from
  [docs/BACKLOG.md](../BACKLOG.md), and move the key to ROADMAP's
  `Already landed / in flight` line.)
- [docs/wiki/Configuration.md](../wiki/Configuration.md): in the `solvers.sample`
  block (~line 119, beside the `include:` example line) add an `exclude:`
  example, and after the **`include`** paragraph (~line 151-158) add a short
  **`exclude`** paragraph: inverse of `include`, exact-id match, works on every
  universe type (unlike `where`), no-op for absent ids, cannot overlap
  `include`.

---

## W2 — non-free roster by default (no key; folds into model-sampling)

**Goal.** The `pricing-table` universe should never offer `$0` models to the
draw. Free OpenRouter entries are real but rate-limited `:free` endpoints — not
representative of the paid production models a measurement frame is about. A user
who genuinely wants one names it directly in `solvers.models` (where its price
still resolves — see Context). This removes the only reason the original request
needed a `where.free` filter.

**Config / public surface.** **No new knob.** This is a refinement of the roster
definition, exactly like the existing `text_model` restriction.

**Mechanism.** In `_build_universe`'s `pricing-table` branch
([_modelsample.py:151](../../src/itemeval/_modelsample.py#L151)), extend the
roster comprehension to also require a positive output price, reusing the
documented free edge from `_price_tier`
([:56-66](../../src/itemeval/_modelsample.py#L56-L66)):

```python
ids = [
    k
    for k, p in pricing.models.items()
    if k.startswith("openrouter/") and p.text_model and p.output_usd_per_mtok > 0
]
```

Do **not** touch [`refresh_pricing`](../../src/itemeval/budget/_pricing.py#L82-L136)
— the saved table stays a faithful mirror so `lookup_price` still prices
explicitly-named free models (see Context, "wrong layer"). Mention free in the
no-runnable-models empty error ([:153-157](../../src/itemeval/_modelsample.py#L153-L157))
only if it reads naturally; the main empty-frame error already covers `where`.

**Interactions to verify (no code, but assert in tests/docs):**
- `stratify_by: price_tier` over a `pricing-table` universe can no longer
  produce a `free` stratum (the roster has none). That is correct — free models
  aren't part of the measured population. The `free` tier edge stays documented
  (it still applies to file/list universes and as the definition W2 reuses).
- File/inline-list universes are **unaffected** — W2 only narrows the
  `pricing-table` roster. A user can still put a free id in an explicit list.

**UX contract.** No knob, no gate. This is a definitional narrowing of a roster
the wiki already describes as "runnable text models only"; extend that sentence.

**Tests:**
1. A `pricing-table` universe built from a fixture containing `$0` and paid
   `openrouter/*` text models excludes the `$0` ones from the drawable universe.
2. The same `$0` ids remain in `pricing.models` and still resolve via
   `lookup_price` (the escape hatch is intact).
3. A free id placed in an **inline-list** universe is still drawable (W2 does not
   touch list/file).

**Docs/CHANGELOG (same commit).** **No new CHANGELOG entry.** Because
`model-sampling` is unreleased, amend its existing `[Unreleased]` `Added`
paragraph — the sentence "The `pricing-table` universe is restricted to
OpenRouter's **runnable text models** …" gains "… and excludes free (`$0`
output) models; name a free model directly in `solvers.models` if you want one."
Mirror the same one-clause edit in
[docs/wiki/Configuration.md](../wiki/Configuration.md) at the "restricted to
runnable text models" sentence (~line 195) and, if it states the price `free`
tier edge (~line 139), note that a `pricing-table` draw never yields a free
stratum.

---

## Sequencing (canonical)

1. **W2** first (non-free roster) — one comprehension line + tests + the
   model-sampling CHANGELOG/wiki amendment. Smallest, and it removes `where.free`
   from consideration so W1 is designed against the final roster. `fix:` commit
   (or fold into W1's commit — no separate key either way).
2. **W1** (top-level `exclude`) — schema + validation + `_build_universe` tail +
   lock spec + `ModelSampleResult` + tests, then the same-change docs (CHANGELOG
   `Closes: sample-exclude`, BACKLOG removal, ROADMAP move, wiki). `feat:` commit
   on branch `feat/sample-exclude`.

After each step: `make check` (lint + fast tests); CHANGELOG and normative doc
tables updated in the same commit.

## Out of scope (explicitly, to prevent creep)

- **`where.free` / a `min_output_usd_per_mtok` floor** — **dropped.** W2 makes
  the roster non-free by construction, so a non-free *filter* is redundant, and
  "draw only free models" is a workflow we design against (free models reached
  via explicit `solvers.models`, not sampling). The `min_output_usd_per_mtok:
  0.0001` spelling from the original request is a magic-number proxy and is
  rejected.
- **Semantic dedup of routing variants** (`:nitro`, `:thinking`, `:extended`,
  and the `:free` duplicates) — deferred to BACKLOG **`roster-dedup`**.
  Free-exclusion (W2) already removes most `:free` duplicates; the original
  survey found exactly **1** suffixed id left in the non-free ≥2023 roster, so
  exact-id `sorted(set(...))` dedup is sufficient now. Real dedup needs a
  canonical-key design and, like W2, must live at the roster layer (never delete
  variants from the saved table — someone may run `:nitro` explicitly).
- **Glob/regex in `exclude`** — exact-id match only (mirrors `include`).
