# Implementation plan — model-sample-composition (recency, equal allocation, pinned include)

**Status: IMPLEMENTED 2026-06-17.** Written 2026-06-17 against the current
`main` (0.3.0.dev) and shipped the same day; this file is now the design record,
past tense. The feature **extends the just-shipped `model-sampling`** (in
`[Unreleased]`, ships with 0.3; design record in
[docs/plans/archive/model-sampling.md](model-sampling.md)). It is
**engine-free** — config, pricing, the draw module, the manifest, the study
card; **no** `inspect_ai` import, so the inspect boundary (DEVELOPMENT.md) is
satisfied trivially and is not re-discussed below. The context that the
implementing session needed, in reading order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style,
   "don't over-engineer").
2. `docs/UX-PATTERNS.md` — **binding** UX contract. Load-bearing here: Law 1
   (no silent side effects — the lock write is already announced), Law 5 (knob
   buckets — every new knob is a **design declaration**, never auto-flipped),
   Law 6 (three renderings), Law 7 (append-only machine surface).
3. `docs/plans/archive/model-sampling.md` — the feature this builds on; its
   universe/lock/draw design is the substrate every workstream here mutates.
4. `docs/BACKLOG.md` → the `model-sample-composition` section (the spec this
   discharges).
5. This file end-to-end before coding any part — the workstreams share the
   draw + lock design.

Scope: 4 workstreams, staged **recency → equal allocation → include** per the
BACKLOG note. **W1** recency (`ModelPrice.created` + `where.released_after` +
`stratify_by: recency`) · **W2** equal allocation (`allocation: equal`) ·
**W3** pinned include (`include:`) · **W4** provenance surfacing + same-change
docs.

---

## Context: the facts that decide the design

### What already exists (model-sampling, unreleased)

`solvers.sample` (`ModelSample` at
[_config.py:109-148](../../src/itemeval/_config.py#L109-L148)) already draws
`n` seeded models from a universe (`pricing-table` roster / file / inline
list), optionally `stratify_by` one of
`provider | reasoning | multimodal | price_tier | context_tier`
(`StratifyBy` + `METADATA_STRATA` at
[_config.py:88-89](../../src/itemeval/_config.py#L88-L89)), optionally narrowed
by `where` (`ModelUniverseFilter` at
[_config.py:92-106](../../src/itemeval/_config.py#L92-L106)). The whole draw
lives in engine-free [_modelsample.py](../../src/itemeval/_modelsample.py):

- `_build_universe` ([:121](../../src/itemeval/_modelsample.py#L121)) → `(source, sorted unique ids)`;
  the `pricing-table` source is `openrouter/*` keys with `text_model` set,
  then `_apply_where`.
- `_apply_where` ([:98](../../src/itemeval/_modelsample.py#L98)) → keep-iff per filter field.
- `_stratum_value(model, dim, pricing)` ([:78](../../src/itemeval/_modelsample.py#L78))
  → the stratum a model falls in (`provider` is id-derived; the rest read
  `pricing.models[id]`).
- `_largest_remainder(total, sizes)` ([:161](../../src/itemeval/_modelsample.py#L161))
  → Hamilton apportionment, sums to `total`, each allotment ≤ its stratum size
  (relies on **proportional** quotas being ≤ size).
- `_draw(universe, sample, pricing)` ([:180](../../src/itemeval/_modelsample.py#L180))
  → `random.Random(seed).sample`; when stratified, `_largest_remainder(n, [sizes])`
  then draw within each stratum.
- `resolve_model_sample(config, pricing, locks_path)` ([:226](../../src/itemeval/_modelsample.py#L226))
  → builds the universe, reads/writes `model_locks.json`, draws or reuses,
  **mutates `config.solvers.models`** to the drawn list, returns
  `ModelSampleResult` (provenance, append-only).

The **lock spec** that must match for reuse is built at
[_modelsample.py:248-254](../../src/itemeval/_modelsample.py#L248-L254):
`{source, n, seed, stratify_by, where}` (where is `where.model_dump()`). A
changed spec **fails loudly**; a changed `universe_hash` only **warns**
(`universe_drift`, the frozen draw stands). This feature **adds `allocation`
and `include` to that spec dict**, and `where` automatically gains
`released_after` via `model_dump()`. Since model-sampling is **unreleased**,
there is no released lock format to preserve — a pre-existing local
`model_locks.json` whose spec now differs will fail loudly telling the user to
clear it (the existing, correct behaviour). No `MODEL_LOCKS_VERSION` bump
needed (still version 1, still the first *released* lock format).

### Provenance surfaces already wired (extend, don't add)

`ModelSampleResult` ([_modelsample.py:24-37](../../src/itemeval/_modelsample.py#L24-L37))
already rides on every carrier — grep confirms: `PreparedStudy.model_sample`
([_prepare.py:45](../../src/itemeval/_prepare.py#L45)),
`Estimate`/`GenerateResult`/`GradeResult`/status report
([budget/_estimator.py:107](../../src/itemeval/budget/_estimator.py#L107),
[generate/_run.py:113](../../src/itemeval/generate/_run.py#L113),
[grade/_run.py:77](../../src/itemeval/grade/_run.py#L77),
[_status.py:69](../../src/itemeval/_status.py#L69)), the manifest
(`model_sample=prep.model_sample.model_dump()`,
[_manifest.py:171](../../src/itemeval/_manifest.py#L171)), the study card
front-matter + a Design line
([report/_card.py:80-89,114-126](../../src/itemeval/report/_card.py#L80-L126)),
the text line `_print_model_sample`
([cli.py:144-166](../../src/itemeval/cli.py#L144-L166)), and the snapshot copy
([store/_export.py:189-190](../../src/itemeval/store/_export.py#L189-L190)).
**Adding fields to `ModelSampleResult` propagates to manifest + card
front-matter + JSON automatically** (all `model_dump()`); only the *prose*
renderings (the cli line, the card Design line) need code to mention the new
fields. New fields are append-only (Law 7).

### The recency substrate: `created` is not fetched today

`ModelPrice` ([budget/_pricing.py:19-35](../../src/itemeval/budget/_pricing.py#L19-L35))
records `text_model`/`reasoning`/`multimodal`/`context_length` from the
OpenRouter `/api/v1/models` response but **not** the release timestamp.
`refresh_pricing` ([:78-131](../../src/itemeval/budget/_pricing.py#L78-L131))
iterates `entry` dicts; the OpenRouter model object carries a top-level
**`created`** field. **[verify]** at implementation against a live (or captured
sample) response that `entry["created"]` is a **Unix timestamp in seconds**
(integer) — it is at time of writing, but the implementing session must
confirm and stamp the checked date in a code comment. Add
`ModelPrice.created: int | None = None` and populate it
(`entry.get("created")`); `None` for the packaged seed and pinned/old user
tables.

**BACKLOG correction (made on `main` in the planning commit).** The BACKLOG
implementation note said "bump a pricing-table `schema_version`". There is **no**
`schema_version` on `PricingTable` ([:38-43](../../src/itemeval/budget/_pricing.py#L38-L43)),
and model-sampling added four metadata fields without introducing one. A
version field on a *regenerable cache file* has no consumer (nothing reads it to
decide anything) — adding it is speculative generality (CLAUDE.md
"don't over-engineer"). The established pattern is: **additive optional field +
loud failure when a feature needs it and it is absent**, pointing at
`--refresh-pricing` (exactly how `text_model` and the empty-universe error
work). This plan follows that pattern; the BACKLOG note is corrected to match.

### Reproducibility constraint (decides the recency design)

The whole point of a pin is "a pinned table → identical draw." So a recency
**filter** must be an *absolute* cutoff (`released_after: "2025-01-01"`), never
wall-clock age, and a recency **stratum** must derive deterministically from
`created` with no baked-in edges that age. **Decision:** the `recency` stratum
is the **UTC calendar year of `created`** (`"2024"`, `"2025"`, …) — a pure
function of `created`, absolute, reproducible, and the stratum set grows
naturally over time with no code edits. (Quarter buckets — `"2025-Q1"` — were
considered for finer resolution and rejected as more than the spec needs; the
absolute `released_after` filter is the precise recency lever, the stratum is
just for balanced coverage.) Fixed price/context tier edges set the precedent
([_modelsample.py:52-75](../../src/itemeval/_modelsample.py#L52-L75)).

---

## W1 — Recency (`created` substrate, `where.released_after`, `stratify_by: recency`)

**Goal.** Let a draw bound and balance models by release date so a
price-bounded "random sample of current LLMs" surfaces today's models, not a
decade of stale ones — reproducibly from a pinned table.

**Config / public surface.**
- `ModelPrice.created: int | None = None` (Unix seconds; **[verify]** the unit).
- `ModelUniverseFilter.released_after: str | None = None` — an absolute
  `YYYY-MM-DD` cutoff; keep model iff `created` is present **and** `created >=`
  the cutoff (a model with no `created` is dropped — same posture as the
  `max_output_usd_per_mtok` filter dropping unpriced models). A
  `field_validator` rejects a malformed date at config-load, naming the
  expected format (validation that teaches).
- `"recency"` added to `StratifyBy` and `METADATA_STRATA`
  ([_config.py:88-89](../../src/itemeval/_config.py#L88-L89)) — it reads roster
  metadata, so it is roster-only, enforced by the existing `METADATA_STRATA`
  check in `ModelSample._check` ([:143-147](../../src/itemeval/_config.py#L143-L147)).
  `where.released_after` lives on `ModelUniverseFilter`, which is already
  roster-only (rejected for list/file universes by `ModelSample._check`
  [:138-142](../../src/itemeval/_config.py#L138-L142)).

**Knob bucket (Law 5).** **Design declaration** — both change which models
exist, hence the grid and condition ids. Always explicit; never auto-flipped.

**Mechanism.**
- `budget/_pricing.py`: add the field; set `created=entry.get("created")` in
  the `ModelPrice(...)` construction ([:113-122](../../src/itemeval/budget/_pricing.py#L113-L122)).
- `_modelsample.py`:
  - `_apply_where` ([:98](../../src/itemeval/_modelsample.py#L98)) gains a
    `released_after` branch: parse the cutoff once to a Unix ts (helper
    `_released_after_ts(date_str) -> int` via `datetime.strptime(.., "%Y-%m-%d")`
    at UTC midnight); `continue` when `p is None or p.created is None or
    p.created < cutoff`.
  - `_stratum_value` ([:78](../../src/itemeval/_modelsample.py#L78)) gains a
    `recency` branch → `str(datetime.fromtimestamp(p.created, tz=utc).year)`
    when `p and p.created`, else `"unknown"`.
  - **Loud failure when recency is requested but the table has no dates:** if
    `where.released_after` filters the roster to empty *and* no roster model
    had `created`, the empty-`where` ConfigError
    ([:142](../../src/itemeval/_modelsample.py#L142)) message gains a clause
    naming `--refresh-pricing`. If `stratify_by == "recency"` and **every**
    universe model lands in `"unknown"` (no `created` anywhere), raise a clear
    ConfigError pointing at `--refresh-pricing` (an all-`unknown` recency draw
    is useless — fail, don't silently degrade). A *mixed* table (some dated,
    some not) is fine: `"unknown"` is a legitimate stratum.

**UX contract.** No new side effect (the `created` write rides the existing
pricing-refresh ledger row; the recency params ride the existing
`model_locks.json` write row). No new gate. The provenance line already prints
`stratified by recency` via the existing `stratify_by` rendering; nothing new
needed in W1's surfacing beyond what W4 covers. JSON parity: `created` rides
the pricing table; `released_after` rides the manifest config echo +
`sampling_requested` (a solvers dump). `recency` stratum is already in
`ModelSampleResult.stratify_by`.

**Tests.** `tests/test_pricing.py`: a stubbed OpenRouter entry with `created`
populates `ModelPrice.created`; entry without it → `None`.
`tests/test_model_sample.py` (hermetic, stubbed `PricingTable`):
`released_after` keeps only models at/after the cutoff and drops undated ones;
malformed `released_after` → `ConfigError` at config-load; `stratify_by:
recency` groups by `created` year and `_largest_remainder` allocates across
years; all-undated + recency → `ConfigError` naming `--refresh-pricing`;
mixed table → `"unknown"` stratum allowed.

---

## W2 — Equal allocation (`allocation: equal`)

**Goal.** `stratify_by` allocates **proportionally only** (largest-remainder),
so large-roster vendors dominate and small ones can drop to zero. Equal-per-
stratum allocation gives balanced coverage (the BACKLOG's core motivation).

**Config / public surface.**
- `ModelSample.allocation: Literal["proportional", "equal"] = "proportional"`
  — default preserves today's behaviour exactly.
- `ModelSample._check`: `allocation == "equal"` requires `stratify_by is not
  None` (equal allocation is meaningless without strata) — else `ValueError`.
- `ModelSampleResult.allocation: str` (append-only; defaults `"proportional"`
  so old locks/manifests round-trip).

**Knob bucket.** **Design declaration** — changes the drawn set → grid/condition
ids. Explicit; never auto-flipped.

**Mechanism.** One general apportionment routine in `_modelsample.py` that W2
and W3 both call — `_allocate(n, keys, weights, floors, caps) -> dict[str,int]`:
apportion `n` across strata roughly proportional to `weights`, with
`floors[k] <= alloc[k] <= caps[k]`, by **iterative fix-and-reapportion** —

```
fixed = {}
while True:
    free = [k for k in keys if k not in fixed]
    if not free: return fixed
    budget = n - sum(fixed.values())
    q = _largest_remainder(budget, [weights[k] for k in free])  # ~ weights, sums to budget
    changed = False
    for k, qk in zip(free, q):
        if qk < floors[k]:  fixed[k] = floors[k];  changed = True   # over-floored → fix at floor
        elif qk > caps[k]:  fixed[k] = caps[k];    changed = True   # over-cap → fix at cap
    if not changed: return {**fixed, **dict(zip(free, q))}
```

Each iteration fixes ≥1 stratum, so it converges; the precondition
`sum(floors) <= n <= sum(caps)` is guaranteed by config + universe-size checks.
For W2 (no include, `floors = 0`): **equal** passes `weights = {k: 1}`,
`caps = {k: size_k}`; **proportional** passes `weights = caps = {k: size_k}` —
which yields exactly today's `_largest_remainder(n, sizes)` (proportional quota
is always ≤ size, so nothing is ever fixed → identical draw; regression-guarded).
`_draw` ([:190-199](../../src/itemeval/_modelsample.py#L190-L199)) builds the
per-stratum counts via `_allocate` and draws within each stratum as today. This
subsumes the BACKLOG open question (n not divisible by stratum count →
largest-remainder over the equal quota) **and** the small-stratum overflow case
a naïve `n // k` would mishandle (fix-at-cap redistributes it).

**UX contract.** Lock spec gains `allocation` (a changed value → fail loud,
clear the lock — correct). Provenance line shows `(equal)` after the
`stratified by …` clause (W4). JSON parity via `ModelSampleResult.allocation`.
No new gate.

**Hint candidate (checklist Q5).** A **proportional** draw can silently zero a
small stratum (quota < 0.5 → 0 models from that provider/tier). `equal` is the
in-band fix shipped here, but the silent-zero failure mode for users who keep
`proportional` remains. A coded hint (`proportional-stratum-zeroed`) is the
right long-term detector — **deferred**, not built in v1 (it needs a
`_hints.py` detector + catalog row + wiki anchor; the spec lists it as an open
question). Recorded in W4's Out-of-scope as a BACKLOG follow-on so it is not
dropped silently.

**Tests.** `tests/test_model_sample.py`: equal allocation gives each stratum
`≈ n/k` and sums to `n`; a stratum smaller than its equal quota gets all of its
models and the overflow lands elsewhere (fix-at-cap redistribution); `equal`
without `stratify_by` → `ConfigError`; `proportional` (default) is byte-for-byte
the old draw (the `_allocate` unification regression guard).

---

## W3 — Pinned include (`include:`)

**Goal.** Purposive + random hybrid: must-include models present alongside the
seeded draw (the BACKLOG's third lever).

**Config / public surface.**
- `ModelSample.include: list[str] = Field(default_factory=list)`.
- `ModelSample._check`: `include` entries unique; `len(include) <= n`
  (else the random fill would be negative).
- `ModelSampleResult.include: list[str]` (append-only; default `[]`).

**Knob bucket.** **Design declaration.**

**Mechanism.** Includes are always present and **count toward `n`**; the random
draw fills the rest from `universe \ include` (set-difference so an included id
already in the roster is not drawn twice). **`include` bypasses `where` and
universe membership** (BACKLOG-resolved: purposive picks are intentional) — an
included id need not be in the roster or pass the filter.

- **Unstratified (`stratify_by` is None):** draw `n - len(include)` from
  `universe \ include`; `result = sorted(set(include) | set(fill))`.
- **Stratified (decided 2026-06-17 — pins count toward the stratum share, *not*
  on top of it):** includes occupy slots in their own stratum, so the fill only
  tops each stratum up toward its balanced quota. Feed `_allocate` (W2) the
  pins as **floors**: `floors[k] =` number of includes whose
  `_stratum_value(m, dim, pricing)` is `k` (every include maps to exactly one
  stratum; an include outside the pricing table lands in `"unknown"` for a
  metadata stratum, or its id-derived org for `provider`); `caps[k] =
  floors[k] + drawable[k]` where `drawable[k] = |universe \ include in stratum
  k|`; `weights[k] = 1` (equal) or the **full** universe stratum size
  (proportional, reflecting roster composition). `_allocate` returns the
  balanced **final** per-stratum counts; `fill[k] = final[k] - floors[k]`,
  drawn from `drawable[k]`. **When a stratum's pins exceed its balanced share,
  fix-at-floor keeps all the pins and re-apportions the remainder across the
  other strata** ("pins win, rest rebalances"). Result = `sorted(include ∪
  drawn)`.

The universe-size guard in `resolve_model_sample`
([:242-246](../../src/itemeval/_modelsample.py#L242-L246)) changes from
`n > len(universe)` to `n - len(include) > len(universe \ include)` (the fill
must fit; this is also `_allocate`'s `n <= sum(caps)` precondition), with the
message naming `include` when it is the binding constraint;
`len(include) <= n` is checked at config-load (W3 surface above), giving
`_allocate`'s `sum(floors) <= n` precondition. `universe_size`/`universe_hash`
stay computed over the full filtered roster (include-independent) so
roster-drift detection is unaffected.

**UX contract.** Lock spec gains `include` (changed → fail loud). Provenance
line gains a `, K via include` clause (W4). JSON parity via
`ModelSampleResult.include`. No new gate; included unpriced models surface via
the existing `unpriced-models` hint at estimate time.

**Tests.** `tests/test_model_sample.py`: included ids always present; counted
against `n` (fill = `n - len(include)`); an included id outside the universe is
still present (bypasses membership) and outside `where` (bypasses the filter);
fill is drawn from `universe \ include` (no double-draw); `len(include) > n` →
`ConfigError`; `len(include) == n` → only the include set, no fill;
non-unique include → `ConfigError`. **include × stratify (Option B):** pins
count toward their stratum's share — e.g. `n`, `stratify_by: provider`,
`include = k` openai models with openai's balanced quota `≥ k` → openai's
*final* count equals the quota (pins + `quota − k` drawn), other strata
unaffected (not openai = quota + k); **over-pinned:** `include` exceeds a
stratum's quota → all pins kept, the remaining budget rebalanced across the
other strata (final still sums to `n`).

---

## W4 — Provenance surfacing + same-change docs

**Goal.** Make the three new levers auditable in all renderings (Law 6) and
ship the same-change paperwork. (This is the **same-change commit point** — the
CHANGELOG entry, BACKLOG removal, and wiki updates land here, so the
docs-consistency check stays green at every commit; W1–W3 commits are pure
`feat:` code+tests with no `Closes` trailer.)

**Config / public surface (all append-only — Law 7).**
- **Text line** — extend `_print_model_sample`
  ([cli.py:144-166](../../src/itemeval/cli.py#L144-L166)): append `(equal)` to
  the `stratified by …` clause when `ms.allocation == "equal"`, and a
  `, {len(ms.include)} via include` clause when `ms.include`. First-draw
  example:
  `models: sampled 25 of 412 (seed 42, stratified by provider (equal), 1 via include) from the OpenRouter roster — pinned in model_locks.json`.
- **Study card** — extend the Design line + front-matter
  ([report/_card.py:82-89,114-126](../../src/itemeval/report/_card.py#L82-L126))
  with `allocation` and `include` (front-matter `model_sample` block gains the
  two keys; the prose `sample_note` mirrors the cli line). Both pull from
  `ModelSampleResult`, which now carries them.
- **Manifest** — no code change: `model_sample=prep.model_sample.model_dump()`
  picks up the new fields; `sampling_requested` (solvers dump) picks up
  `allocation`/`include`/`where.released_after` automatically.
- **JSON** — no code change: `ModelSampleResult` rides every result object
  already; the new fields serialize automatically.

**UX contract.**
- **Side-effect ledger (UX-PATTERNS.md):** **no new row** — the model-sample
  pin-write row already exists; this feature only enriches the pinned spec.
  Re-confirm the row's "required line" wording still matches.
- **Consent (Q7):** no new gate; resolved models feed the estimator → the
  *existing* money gate. **Surface parity (Q8):** config-driven → CLI + Python
  both; provenance on `PreparedStudy.model_sample` and `--json`; no prompting.
- **Stability (Q9):** `ModelPrice.created` + `ModelSampleResult.allocation`/
  `include` are additive; no new exit code / hint code / CLI command / top-level
  export.

**Tests.**
- `tests/test_public_api_snapshot.py`: **stays green** — `ModelSampleResult` is
  not a top-level export and no CLI command/`__all__` entry is added (mirrors
  model-sampling). Do **not** edit the golden unless a run proves otherwise.
- `tests/test_manifest.py`: `model_sample` records `allocation`/`include`.
- `tests/test_snapshot.py`: unchanged (`model_locks.json` already copied).
- `tests/test_docs_consistency.py`: the wiki `solvers.sample` YAML stays a
  `solvers:` fragment (not a top-level `study:` block), so it is not
  schema-validated — keep it a fragment.

**Docs/CHANGELOG (same commit as behaviour — same-change rule).**
- `CHANGELOG.md` `[Unreleased]` → one `### Added` entry covering recency
  (`created` + `released_after` + `stratify_by: recency`), `allocation: equal`,
  and `include:`; trailer `Closes: model-sample-composition`. Minor bump
  material (additive `ModelPrice.created`, additive lock spec fields — no
  store-schema change).
- **Remove** the `model-sample-composition` section from `docs/BACKLOG.md`
  (design record lives on in this plan once archived).
- Wiki `Configuration.md` — extend the `solvers.sample` subsection
  ([:107-157](../wiki/Configuration.md#L107)): add `released_after` to the
  `where` list, `recency` to the `stratify_by` dimensions (year buckets,
  absolute cutoff note), an `allocation: proportional | equal` paragraph, and an
  `include:` paragraph; update the "changing `n`/`seed`/`stratify_by`/`where`
  fails loudly" line to also name `allocation`/`include`; note that recency
  needs a refreshed table (`created`); add a short **SOTA-per-vendor recipe**
  using `include:` to pin current flagships (the honest path — `latest-by-date`
  is not a flagship proxy; decided 2026-06-17, see Out of scope).
- Wiki `Outputs-and-Schemas.md` — the `model_locks.json` spec now carries
  `allocation`/`include`/`where.released_after`; update the one-line schema note
  ([:36-42](../wiki/Outputs-and-Schemas.md#L36)).

---

## Sequencing (canonical)

1. **W1** recency — `ModelPrice.created` + refresh, `where.released_after`,
   `stratify_by: recency`; `tests/test_pricing.py` + `tests/test_model_sample.py`
   green. (`feat:`, no `Closes`.)
2. **W2** equal allocation — `allocation` field + the unified `_allocate`
   routine + `_draw` branch; `ModelSampleResult.allocation`; tests green. (`feat:`.)
3. **W3** include — `include` field + draw fill + count guard;
   `ModelSampleResult.include`; tests green. (`feat:`.)
4. **W4** surfacing + same-change docs — cli/card prose, CHANGELOG `Closes:`,
   BACKLOG removal, wiki. **This commit makes the docs-consistency check pass
   with the key gone from BACKLOG** — so the `Closes` trailer and the BACKLOG
   deletion are in the *same* commit (steps 1–3 must not add the trailer).

After each step: `make check` (lint + fast tests). If `make check` is green at
W3 but you split W4's docs across commits, keep the `Closes` trailer and the
BACKLOG deletion together to avoid a red docs-consistency check mid-branch.

## Out of scope (explicitly, to prevent creep)

- **`proportional-stratum-zeroed` hint** — the silent-zero failure mode of a
  proportional draw; `allocation: equal` is the in-band fix shipped here. The
  detector hint is deferred to a BACKLOG follow-on (needs a `_hints.py`
  detector + catalog row + wiki anchor); recorded so it is not dropped.
- **Auto flagship / latest-per-vendor selection** — deferred to BACKLOG key
  `flagship-selection` (decided 2026-06-17 with the maintainer). `created`-max
  picks mini/nano/preview variants and price-max is also heuristic; no reliable
  roster signal for "flagship" exists, so the honest SOTA-per-vendor path is
  `include:` (documented in the wiki recipe). The `created` substrate this
  feature adds makes the auto-rule cheap to add later if a signal appears.
- **`stratify_by: family`** — already ruled out by model-sampling (no clean
  roster field; `instruct_type` ~86% null, `tokenizer` ⅓ "Other"/"Router").
- **Quarter/month recency buckets** — year is the simplest reproducible stratum
  the spec needs; `released_after` is the precise lever.
- **`pricing-table` `schema_version`** — not added (BACKLOG note corrected); a
  regenerable cache needs none, and the loud-failure-on-missing-`created`
  pattern covers staleness.
- **Re-draw-on-spec-change with a warning** — inherited from model-sampling:
  v1 fails loudly and tells the user to clear the lock.
- **`item-sampling`, `per-model-config`** — separate keys; not touched here.
- **Any `inspect_ai` change** — the feature is engine-free.
