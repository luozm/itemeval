# Implementation plan — model-sampling (sample candidate LLMs from a roster, with provenance)

**Status: IMPLEMENTED 2026-06-16.** Written 2026-06-16 against the
current `main` (0.3.0.dev); shipped the same day. This file is the design
record, past tense. The feature is **engine-free** — it touches config,
prepare, pricing, the manifest, and the study card; it adds **no** `inspect_ai`
import, so the inspect boundary (DEVELOPMENT.md) is satisfied trivially and is
not re-discussed below. This file is the working brief for a fresh session:
it carries all context that session needs. Read these first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract. Load-bearing here: Law 1
   (no silent side effects — the lock write is announced), Law 5 (knob buckets —
   `sample` is a **design declaration**, never auto-flipped), Law 6 (three
   renderings), Law 7 (append-only machine surface).
3. `docs/BACKLOG.md` → the `model-sampling` section (the spec this discharges)
   and its sibling `item-sampling` (NOT yet built — see Context).
4. This file end-to-end before coding any part — the workstreams share the
   universe/lock/draw design.

Scope: 3 workstreams. **W1** config schema (`solvers.sample`) · **W2** resolve
+ pin (`model_locks.json`, the draw, prepare wiring) · **W3** provenance
surfacing (announcement line, JSON parity, manifest, study card) + same-change
docs.

---

## Context: the facts that decide the design

### Where models live (the BACKLOG sketch was wrong)

The BACKLOG sketch wrote `facets.model.sample`. There is **no** `facets.model`.
The model list is `solvers.models: list[str]`
([_config.py:73](../../src/itemeval/_config.py#L73), `min_length=1`, uniqueness
validator at [_config.py:106-111](../../src/itemeval/_config.py#L106-L111)).
Everything downstream reads `config.solvers.models`:

- grid expansion: `for model in config.solvers.models`
  ([design/_grid.py:88](../../src/itemeval/design/_grid.py#L88));
- manifest: `models=list(cfg.solvers.models)`
  ([_manifest.py:166](../../src/itemeval/_manifest.py#L166));
- study card front matter: `"models": list(config.solvers.models)`
  ([report/_card.py:72](../../src/itemeval/report/_card.py#L72)).

So the knob is **`solvers.sample`**, sitting next to `solvers.models` — the
exact mirror of `item-sampling`'s `benchmark.sample` next to
`benchmark.datasets`. **Decision:** when `sample` is present, prepare resolves
the draw and **sets `config.solvers.models` to the drawn list in-place**, before
grid expansion. Every reader above then works unchanged and records the drawn
set automatically. The `sample` spec stays on the config object for provenance.
Rationale: threading a separate "effective models" through grid/manifest/card
would touch far more code for no behavioural gain (simplest-thing rule). The
mutation is per-process (each command re-loads the YAML fresh) and re-derived
from the lock every run, so it is idempotent.

### `item-sampling` is not built yet

`benchmark.sample` does not exist in the code (grep: no `sample` field on
`BenchmarkConfig`). model-sampling therefore builds its own draw + lock
infrastructure; it does **not** depend on item-sampling and must not implement
it. The draw helper and lock pattern here are written so item-sampling can later
mirror them (same `random.Random(seed)` + sorted-universe discipline).

### The pricing table is the roster source

`PricingTable.models: dict[str, ModelPrice]`
([budget/_pricing.py:31-37](../../src/itemeval/budget/_pricing.py#L31-L37)).
After `refresh_pricing()`
([budget/_pricing.py:71-110](../../src/itemeval/budget/_pricing.py#L71-L110))
the table holds an `openrouter/<id>` key for **every** OpenRouter model (~400)
plus a bare `<id>` for each (seed wins for native ids), plus seed natives and
`mockllm/*`.

**Decision — `universe: pricing-table` means the `openrouter/*` keys of the
active pricing table.** That set *is* the OpenRouter roster the user means, each
id runnable as-is (`openrouter/<org>/<model>`), with no bare/native duplicates
to bias the draw. `mockllm/*` and seed natives are excluded by construction.
**[verify]** at implementation: confirm `pricing_seed.json` + a live refresh
produce `openrouter/<org>/<model>`-shaped keys, and that the seed alone has
enough `openrouter/*` entries to test against (else the test refreshes a stubbed
table — never the live network). If the active table has zero `openrouter/*`
keys, raise `ConfigError` telling the user to `--refresh-pricing` or pass an
explicit-list universe. The raw `openrouter/*` set is then narrowed by `where`
(W1/W2): a `provider` allowlist + `max_output_usd_per_mtok` ceiling. itemeval
keeps only prices from the roster (modality/architecture fields are dropped by
`refresh_pricing`), so v1 cannot filter out non-chat models — `pricing-table`
is best-effort, the explicit list is the fully-curated path (see Out of scope).

### Provider, for stratification

`provider_of(model) = model.split("/")[0]`
([budget/_pricing.py:218](../../src/itemeval/budget/_pricing.py#L218)) returns
`"openrouter"` for every `openrouter/*` id — useless for stratifying an
all-OpenRouter universe. Stratification needs the **org** segment. New helper
(in the model-sample module, not a change to `provider_of`):

```
stratum(model): parts = model.split("/")
    return parts[1] if parts[0] == "openrouter" and len(parts) > 2 else parts[0]
```

`openrouter/anthropic/claude-3.5` → `anthropic`; native `anthropic/claude-3.5`
→ `anthropic`. **[verify]** the `openrouter/<org>/<model>` three-segment shape
against a live/stubbed refresh. v1 supports `stratify_by: provider` only
(richer strata are deferred — see Out of scope).

### The lock pattern to parallel

`dataset_locks.json` is the model: `read_locks`/`write_locks`/`LOCKS_VERSION`
([adapters/_base.py:14,83-102](../../src/itemeval/adapters/_base.py#L83-L102)),
path from `StudyPaths.dataset_locks`
([store/_layout.py:31-32](../../src/itemeval/store/_layout.py#L31-L32)), written
with `atomic_write_bytes`, the pin announced only on change
([cli.py:139-140](../../src/itemeval/cli.py#L139-L140)). model-sampling adds
`StudyPaths.model_locks` → `study_dir / "model_locks.json"` and its own
read/write in a new engine-free module `src/itemeval/_modelsample.py`.

### prepare is the single chokepoint, but pricing currently loads last

`prepare_study` ([_prepare.py:51-109](../../src/itemeval/_prepare.py#L51-L109))
is called by every command, so resolving the sample there makes all surfaces
agree. **But** it expands the grid at
[_prepare.py:82](../../src/itemeval/_prepare.py#L82) *before* loading pricing at
[_prepare.py:83-93](../../src/itemeval/_prepare.py#L83-L93). The pricing-table
universe needs pricing first. **Decision:** move the pricing block to just
before `expand_grid`, insert sample resolution between them:

```
… load_items → items/origins → plan → items_effective
pricing = (refresh | pinned | maybe_refresh)          # moved up
model_sample = resolve_model_sample(config, pricing, paths.model_locks)  # mutates config.solvers.models
grid = expand_grid(config, …)                          # now sees drawn models
```

Datasets/items don't depend on pricing, so the reorder is safe. When `sample`
is absent, `resolve_model_sample` is a no-op returning `None` and the reorder is
invisible to every existing config.

---

## W1 — config schema: `solvers.sample`

**Goal.** Let a config declare a model sample instead of an explicit list, with
two universe sources (explicit list, pricing-table roster) and seeded, optionally
provider-stratified selection.

**Config / public surface.** New pydantic models in `_config.py`:

```python
class ModelUniverseFilter(BaseModel):              # `where:` — pricing-table only
    model_config = ConfigDict(extra="forbid")
    provider: list[str] | None = None              # org allowlist (stratum match)
    max_output_usd_per_mtok: float | None = Field(default=None, gt=0.0)

class ModelSample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = Field(ge=1)
    seed: int
    stratify_by: Literal["provider"] | None = None
    universe: str | list[str]      # "pricing-table" | a file path | an inline list
    where: ModelUniverseFilter | None = None
```

**Three universe sources** (the `str | list[str]` shape distinguishes them):
`list[str]` → inline explicit list; the string `"pricing-table"` (reserved
keyword) → roster; any **other** string → a **file path** (one model id per
line, `#` comments and blank lines skipped), resolved relative to `config_dir`
via the input-path rule (mirror `load_pricing(explicit_path, config._input_base)`
at [budget/_pricing.py:58-64](../../src/itemeval/budget/_pricing.py#L58-L64)).
Inline list + file are both "user-curated, already on disk"; the roster is the
only source `where` filters.

- `SolversConfig.models` becomes optional:
  `models: list[str] = Field(default_factory=list)` (drop `min_length=1`); add
  `sample: ModelSample | None = None`.
- `model_validator(after)` on `SolversConfig`: **exactly one** of `models` /
  `sample` is provided (XOR) — `ValueError` otherwise. Keep the existing
  uniqueness check, applied to `models` when it is the one provided.
- Config-time validation of `universe`: an inline `list[str]` is validated
  non-empty, unique, `len >= n`. A file path can't be read at load time
  (resolution is a prepare-stage concern), so its existence / `len >= n` /
  uniqueness are checked in W2 and raised as `ConfigError`. `"pricing-table"`
  size is likewise checked at draw time (universe unknown until pricing + filter).
- **Reject `where`** unless `universe == "pricing-table"` — inline lists and
  files are already user-curated, so `where` is meaningful only against the
  roster (`ValueError` at config time when the universe is a list; for a file
  path the universe is still a string, so this check also fires at load time
  whenever `universe != "pricing-table"`).
- `where.provider`/`where.max_output_usd_per_mtok` are both optional; an empty
  `where: {}` is allowed but inert (validator may warn, not required).

**Knob bucket (Law 5).** **Design declaration** — `sample` determines which
models exist, hence the grid and condition ids. Always explicit; never
auto-flipped; not an optimization knob. No default that silently samples.

**Mechanism.** Pure schema + validators; no behaviour. `config_to_jsonable`
already dumps by alias, so the manifest config echo carries `sample` verbatim.

**Tests.** `tests/test_config.py`: XOR accepted/rejected both ways; explicit
universe `len < n` rejected; `where` + explicit-list universe rejected;
`stratify_by` literal enforced; `extra="forbid"` holds on `sample` and `where`;
an existing `models:` config still validates unchanged (back-compat).

**Docs/CHANGELOG.** Deferred to W3 (single same-change commit point).

---

## W2 — resolve + pin: the draw and `model_locks.json`

**Goal.** Resolve `solvers.sample` to a concrete, reproducible model list, pin
it so resume/status see a stable set, and reuse the pin on later runs.

**Config / public surface.** New module `src/itemeval/_modelsample.py`:

- `ModelSampleResult(BaseModel)` (provenance, append-only): `source`
  (`"pricing-table" | "explicit" | "file"`), `universe_size: int`,
  `universe_hash: str` (12 hex over canonical-json of sorted universe ids),
  `n`, `seed`, `stratify_by: str | None`, `models: list[str]` (drawn, sorted),
  `pinned_now: bool`, `universe_drift: bool`.
- `resolve_model_sample(config, pricing, locks_path) -> ModelSampleResult | None`
  — `None` when `config.solvers.sample is None`. Otherwise resolves the
  universe, reads the lock, draws or reuses, writes the lock on first draw,
  **mutates `config.solvers.models`** to the result, returns the provenance.
- `StudyPaths.model_locks` property → `study_dir / "model_locks.json"`.
- `PreparedStudy.model_sample: ModelSampleResult | None = None`.

**Mechanism.**

*Universe — by source.*
- **`"pricing-table"`** (source `pricing-table`) → start from `[k for k in
  pricing.models if k.startswith("openrouter/")]`, then apply `where` (when
  present): keep `k` iff (`where.provider is None` or `stratum(k) in
  where.provider`) **and** (`where.max_output_usd_per_mtok is None` or
  `pricing.models[k].output_usd_per_mtok <= where.max_output_usd_per_mtok`).
- **inline `list[str]`** (source `explicit`) → the list as given.
- **other string = file path** (source `file`) → resolve relative to
  `config._input_base`, read, strip, drop blank / `#`-comment lines. Missing
  file or unreadable → `ConfigError`.

Then `universe = sorted(set(ids))` for every source. Empty universe (a
pricing-table that filters to empty, or an empty file) → `ConfigError`; when a
non-empty roster filters to empty, the message names `where`
("filter excluded all N priced openrouter models — loosen `where`"). `n >
len(universe)` → `ConfigError`, and when `where` is set the message hints the
filter may be too tight. The file/list `len >= n` and uniqueness checks happen
here, not at config-load (the file isn't read until now).

*Draw (deterministic given `(seed, sorted universe)`).*
```
rng = random.Random(seed)
ids = sorted(universe)
if stratify_by == "provider":
    groups = {stratum: [ids in that stratum]} iterated in sorted-key order
    counts = largest_remainder(n, [len(g) for g in groups])   # sums to n
    drawn = concat(rng.sample(sorted(g), k) for g, k in zip(groups, counts))
else:
    drawn = rng.sample(ids, n)
drawn = sorted(drawn)
```
`random.Random.sample` is stable across CPython versions; the lock pins the
result regardless, so cross-version drift can only matter before the first lock.
Note this in a code comment.

*Lock semantics (`model_locks.json`, `version: 1`).* Records the full
`ModelSampleResult` (spec + universe list + universe_hash + drawn models +
`resolved_at`). On a run:
- **No lock** → draw, write lock, `pinned_now=True`.
- **Lock present, spec matches** (`n`, `seed`, `stratify_by`, `source`, **and
  `where`** equal) → **reuse** the locked `models` unchanged; do **not**
  re-draw. (A `where` change is a spec change → fail loud below.) If the current
  universe_hash differs from the locked one, set `universe_drift=True` (the
  roster moved; the frozen draw still stands — Law 2: warn, never block).
  `pinned_now=False`.
- **Lock present, spec differs** (e.g. `n` 20→30, seed changed) → **fail
  loudly** with `ConfigError`: name the changed field and say
  `clear model_locks.json to re-draw (existing solutions for previously-sampled
  models remain)`. This keeps v1 small and safe: re-sampling strands/adds rows,
  which is the grow-in-place drift story we do not silently trigger. (A future
  re-draw-with-drift-warning is Out of scope.)

The locked universe list is what makes even the first draw reproducible by a
third party; the universe_hash is the cheap drift check.

**UX contract.** Writing `model_locks.json` decides future runs → Law 1 side
effect, announced (W3 line + ledger row). No new gate (Law 2): universe drift is
a warning; the resolved model set flows into the estimator, so spend is covered
by the *existing* money gate.

**Tests.** New `tests/test_model_sample.py` (hermetic — stubbed `PricingTable`,
tmp lock path, no network):
- determinism: same `(seed, universe)` → identical draw; different seed →
  different draw; result independent of pricing dict insertion order.
- stratified draw: per-provider counts match largest-remainder; sums to `n`.
- pricing-table universe = `openrouter/*` keys only (excludes bare/native/mock).
- `where`: provider allowlist keeps only matching `stratum()`;
  `max_output_usd_per_mtok` drops pricier models; filter→empty raises
  `ConfigError` naming `where`; `where` rejected for list/file universes.
- file source: reads ids (skips blanks/`#`), resolves relative to config dir,
  `source="file"`; missing file → `ConfigError`; a changed file → universe drift
  (frozen draw stands), same as roster drift.
- lock lifecycle: first run pins (`pinned_now`), second reuses identical set,
  universe change → `universe_drift=True` but same `models`, spec change →
  `ConfigError`.
- `n > universe` → `ConfigError`; empty pricing-table universe → `ConfigError`.
- `resolve_model_sample` mutates `config.solvers.models`; `sample=None` is a
  no-op.

**Docs/CHANGELOG.** In W3.

---

## W3 — provenance surfacing + same-change docs

**Goal.** Make the sample auditable in all three renderings (Law 6) and ship the
same-change paperwork.

**Config / public surface (all append-only — Law 7).**
- **Text line** (Law 1), via a new `_print_model_sample(prep)` in `cli.py`
  printed right after `_print_datasets(prep)`
  ([cli.py:129-141](../../src/itemeval/cli.py#L129-L141)) on
  estimate/generate/grade/status. First draw (source phrase varies — the
  OpenRouter roster / a 30-id list / `models.txt`):
  `models: sampled 20 of 412 (seed 7, stratified by provider) from the OpenRouter roster — pinned in model_locks.json`
  Reuse:
  `models: 20 sampled models reused from model_locks.json (seed 7)`
  Drift adds: `; universe changed since the pin (412→415) — draw unchanged`.
  Printed only when `prep.model_sample is not None`.
- **JSON parity.** `ModelSampleResult` rides as `model_sample` on the result
  models that already carry `datasets[]` — `Estimate`, `GenerateResult`,
  `GradeResult`, and the status report. (Mirror the `datasets[]` plumbing;
  grep `dataset_provenance(` / `datasets=` to find every site.) Field is `null`
  when no sampling.
- **Manifest.** Add `model_sample: dict | None = None` to `Manifest`
  ([_manifest.py:52-92](../../src/itemeval/_manifest.py#L52-L92)) and populate
  from `prep.model_sample` in `build_manifest`. `models=` already records the
  drawn set; `sampling_requested` already carries the `sample` spec (solvers
  dump minus `models`, [_manifest.py:138-139](../../src/itemeval/_manifest.py#L138-L139)).
- **Study card.** In `build_study_card`
  ([report/_card.py:46-112](../../src/itemeval/report/_card.py#L46-L112)) the
  front-matter `models` already shows the drawn set. Add one provenance line in
  the **Design** section when `prep.model_sample`:
  `Models: sampled 20 of 412 from the OpenRouter roster (seed 7, stratified by provider); pinned in model_locks.json.`
  Also copy `model_locks.json` into snapshots alongside `dataset_locks.json`
  (grep `dataset_locks` in `store/_export.py` for the snapshot copy set).

**UX contract.**
- **Side-effect ledger (UX-PATTERNS.md):** add a row —
  *Model sample pin write · `model_locks.json` (study dir, **decides future
  runs**) · required line:* `models: … — pinned in model_locks.json` (printed on
  change only), mirroring the dataset revision-pin row.
- **Hint candidate (checklist Q5):** none added in v1. The announcement line is
  the visibility; `unpriced-models` already covers an unpriced drawn model.
  Documented caveat: a pricing-table draw may include models the user has no key
  for — surfaced at run time as the usual per-model provider error, not at
  sample time. Note a possible future `model-sample-unrunnable` hint in the
  BACKLOG follow-on, do not build it.
- **Consent (Q7):** no new gate; resolved models feed the estimator → existing
  money gate. **Surface parity (Q8):** config-driven, so CLI and Python both get
  it; provenance on `PreparedStudy.model_sample` (Python) and `--json` (CLI);
  no prompting.

**Tests.**
- `tests/test_manifest.py`: `model_sample` recorded; `models` = drawn set.
- `tests/test_public_api_snapshot.py`: **stayed green** — it snapshots only
  `itemeval.__all__` and the CLI subcommands, neither of which changed (no new
  top-level export, no new command). `ModelSampleResult` rides on the result
  objects but is *not* a top-level export, mirroring `DatasetProvenance`. (The
  plan originally predicted a red here; the snapshot's scope is narrower.)
- `tests/test_snapshot.py`: `model_locks.json` copied into a snapshot.
- A `tests/test_docs_consistency.py` run stays green (the BACKLOG `sample:`
  example is a `facets:`/`solvers:` fragment, not a top-level `study:` block, so
  it is not schema-validated — confirm the YAML I write in the wiki/CHANGELOG
  either validates or is a non-`study:` fragment).

**Docs/CHANGELOG (same commit as behaviour — same-change rule).**
- `CHANGELOG.md` `[Unreleased]` → `### Added` entry describing `solvers.sample`,
  the two universe sources, the lock, and the provenance surfaces; trailer
  `Closes: model-sampling`. Minor bump (new feature; additive manifest field +
  new lock file, no store-schema change).
- **Remove** the `model-sampling` section from `docs/BACKLOG.md` (design record
  lives on in this plan once archived).
- Wiki: `Configuration.md` gains a `solvers.sample` subsection (the two sources,
  seed/stratify, the lock + "fixed after first draw" + how to re-draw);
  `Outputs-and-Schemas.md` documents `model_locks.json`. The deferred
  `where:`/richer-`stratify_by` design already lives in the BACKLOG follow-on —
  link, don't restate.

---

## Sequencing (canonical)

1. **W1** schema + validators (no behaviour) — `tests/test_config.py` green.
2. **W2** `_modelsample.py` + `StudyPaths.model_locks` + prepare reorder/wiring
   — consumes W1; `tests/test_model_sample.py` green.
3. **W3** provenance surfaces + manifest/card + same-change docs — consumes
   W2's `PreparedStudy.model_sample`; update the public-API snapshot golden.

One conventional `feat:` commit per workstream is fine, but **W3 must be the
same commit as the CHANGELOG/BACKLOG/wiki changes** (same-change rule). After
each step: `make check` (lint + fast tests), docs tables updated in the same
commit.

## In v1 vs deferred (resolved 2026-06-16)

**In v1** (confirmed with the maintainer): **three** universe sources (inline
explicit list, a file of ids, `pricing-table`); `stratify_by: provider`;
**`where:` filtering** on the roster by `provider` allowlist +
`max_output_usd_per_mtok`. Filtering matters because
the raw OpenRouter roster (~400) includes free/toy/non-chat models; a provider
allowlist also acts as a junk filter (frontier-lab entries are chat models).

**Live roster fetch — not built, already covered.** The pricing table is
refreshed from OpenRouter by the *existing* mechanism (`--refresh-pricing` /
`budget.pricing_max_age_days`), so sampling from the current roster already
works — refresh, then run. W3's wiki text must say this explicitly; there is no
new network path.

## Out of scope (explicitly, to prevent creep)

- **Richer `stratify_by`** (family, price tier) — no clean metadata; fragile.
  Tracked in the `model-sampling` BACKLOG **Follow-on**; do not build.
- **Modality-aware filtering** (exclude non-chat/embedding/vision models) — the
  *real* fix for roster junk, but it needs `refresh_pricing` to keep OpenRouter's
  `architecture`/modality fields (currently dropped — only prices are stored),
  i.e. a pricing-cache schema change. Out of scope; note the limitation in the
  wiki ("`pricing-table` is best-effort; use an explicit list for a fully
  curated universe"). Add to the BACKLOG follow-on.
- **Re-draw-on-spec-change with a drift warning** — v1 fails loudly and tells
  the user to clear the lock; the grow-in-place re-sample story is deferred.
- **`item-sampling`** — a separate key; not implemented here.
- **Any `inspect_ai` change** — the feature is engine-free.
