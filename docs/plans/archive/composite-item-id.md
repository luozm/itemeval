# Implementation plan — composite-item-id (templated/composite `mapping.id` for multi-dataset pooling)

**Status: IMPLEMENTED 2026-06-17.** Written 2026-06-17 against the current tree
(no inspect_ai surface was touched — a config + HF-adapter change only). This
file is the design record; it was the working brief for the implementation
session. The original reading order, kept for provenance:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract. This feature's checklist
   pass is recorded under "UX contract" below; it is almost all "no" answers
   (no new side effect, no new gate, no new command surface) — the one live
   item is the **knob bucket** (`mapping.id` is a *design declaration*) and the
   guard-message change.
3. This file end-to-end before coding — the parts share one id-synthesis
   contract.

Scope: **one workstream** (~50 lines + tests), one `feat:` commit. The inspect
boundary (DEVELOPMENT.md) is not engaged: adapters import `datasets` /
`huggingface_hub`, never `inspect_ai`, so wrap-don't-fork / flatten-at-boundary
do not bite here.

---

## Context: the facts that decide the design

**The problem (BACKLOG `composite-item-id`).** `mapping.id` takes a single
column and `load_items` requires globally-unique ids across datasets. Datasets
that share a natural key (a per-split row index; a per-release problem number
repeated each year) can't be pooled — the load aborts on a duplicate id, and
omitting `id` falls back to a per-dataset row index that collides too.

**Current code — the three exact touch points.**

- `src/itemeval/_config.py:57` — `MappingSpec.id: str | None = None`
  ("record column -> Item.id (else row index)"). `MappingSpec` has
  `ConfigDict(extra="forbid")` and no `id` validator today.
- `src/itemeval/adapters/_hf.py:26-64` — `_record_to_item(record, index,
  mapping, dataset_id)`. The id line is **`_hf.py:37`**:
  ```python
  item_id = str(require(mapping.id)) if mapping.id else str(index)
  ```
  `require(column)` (defined at `_hf.py:29-35`) raises `AdapterError` naming the
  column and the available columns when a mapped column is absent. The function
  already receives `dataset_id` (the full `spec.id`, e.g. `org/set_2026`) — so
  the `{dataset}` token needs no new plumbing.
- `src/itemeval/adapters/_base.py:131-139` — the uniqueness guard in
  `load_items`:
  ```python
  raise AdapterError(
      f"duplicate item id {item.id!r} in datasets "
      f"{seen[item.id]!r} and {ds.dataset_id!r}"
  )
  ```

**The id is the join key everywhere.** `Item.id` flows into the solutions and
gradings parquet stores, the export long table, and the manifest's `items_hash`
(`_manifest.py:104-106`, hashes `(id, input-hash)` pairs). So the hard
constraint: **single-column configs must produce byte-for-byte identical ids**,
or every existing study's stored rows orphan and `items_hash` drifts. Verified
below that the chosen design preserves this exactly.

**Manifest / serialization.** The manifest echoes the whole config via
`config_to_jsonable` → `model_dump(mode="json", by_alias=True)`
(`_manifest.py:152`). A list-valued `mapping.id` serializes to a JSON list
natively — **no manifest code change**. Confirmed `MappingSpec` is not in
`itemeval.__all__` and we add no public name or CLI command, so
`tests/test_public_api_snapshot.py` (tracks `__all__` + CLI subcommands, not
field types) stays green untouched.

### The id-synthesis contract (the one shared design decision)

`mapping.id` becomes `str | list[str] | None`. Normalize to a **list of
segments** (a bare string is a one-element list; `None` keeps today's row-index
fallback). Each segment renders independently, then segments join with `:`:

- A segment **containing `{`** is a **template**: every `{name}` token is
  substituted, where `name == "dataset"` → the dataset **basename**
  (`dataset_id.split("/")[-1]`, e.g. `set_2026` from `org/set_2026`), and any
  other `name` → that record column (`str(record[name])`). A `{name}` whose
  column is absent raises `AdapterError` listing the valid placeholders
  (`dataset` + the record's columns) — the same teaching shape as `require`.
- A segment **without `{`** is a **literal column name** → `str(require(seg))`,
  identical to today's accessor.
- After substitution, a segment that still contains `{` or `}` (an unbalanced /
  malformed placeholder like `"{dataset"`) raises `AdapterError` naming the
  segment — so a typo fails loud instead of leaking a literal brace into ids.

**Backward-compat proof.** `id: problem_idx` (str, no brace) → segments
`["problem_idx"]` → literal column → `str(require("problem_idx"))` → single
segment, nothing to join → exactly today's `str(require(mapping.id))`. None →
`str(index)`. Byte-for-byte. ✓ The BACKLOG example
`id: ["{dataset}", problem_idx]` → `["set_2026", "6"]` → `set_2026:6`. ✓ The
template-string form `id: "{dataset}:{problem_idx}"` → one segment, both tokens
substituted, the literal `:` kept → `set_2026:6` (same result, second spelling).

**Decisions made (were BACKLOG open questions):**
- **Separator `:`** (matches the BACKLOG example; avoids the path-like `/`). No
  escaping: ids are opaque keys never split back into parts, and the existing
  uniqueness guard catches any real collision — so ambiguity has no failure
  mode to defend against. Simplest correct thing.
- **`{dataset}` = basename** of `spec.id`, not the full `org/name` (matches the
  example; keeps ids short/readable; keeps `/` out of ids, which double as store
  keys). Basename collision across orgs (`a/aime`, `b/aime`) is rare and caught
  loudly by the uniqueness guard, whose message now names the composite knob.
- `str(value)` for every rendered token (None → `"None"`), matching today's
  single-column `str(require(...))` exactly — no special-casing.

---

## W1 — composite/templated `mapping.id`

**Goal.** Let a study pool datasets that share a natural key by giving
`mapping.id` a composite/template form that namespaces ids per dataset, so the
existing global-uniqueness guard becomes *satisfiable* instead of a dead end —
without moving any existing single-column study's ids.

**Config / public surface.** `MappingSpec.id: str | list[str] | None`
(`_config.py`). UX-PATTERNS **knob bucket: design declaration** — it defines
item identity / the cross-store join key, so it stays explicit forever and is
never auto-flipped. Widening an existing field to accept a superset of values is
append-only (no removal, no rename). New `field_validator` on `id`: when a list,
reject empty list and any empty/blank or non-string element (`extra="forbid"`
already guards unknown keys). Structural only — placeholder/column validity is a
load-time fact (the config layer can't see record columns), caught in the
adapter, mirroring the existing config-validates-shape / adapter-validates-
references split.

**Mechanism.**
- `adapters/_hf.py`: add a module-level pure helper
  `_synthesize_id(record, index, mapping, dataset_id) -> str` implementing the
  id-synthesis contract above; replace the `_hf.py:37` one-liner with a call to
  it. A small module-level `re.compile(r"\{([^{}]*)\}")` drives token
  substitution; the missing-placeholder and stray-brace branches raise
  `AdapterError`. Keep `require` for the literal-column path so absent columns
  raise the existing message. This stays a pure function over a dict — no
  network, no inspect, fully unit-testable.
- `adapters/_base.py`: extend the duplicate-id guard message (keep the
  `duplicate item id` prefix that `test_adapter_mapping.py` matches) with one
  clause pointing at the fix — e.g. `… — if the same natural key repeats across
  datasets, make ids unique with a composite mapping.id (e.g. ["{dataset}",
  <col>]); see Configuration#composite-item-ids`. A teaching error, not a hint
  (errors may teach; hints are the dim post-run channel).

Rejected as over-engineering: a separate one-flag `dataset_id`-prefix knob (the
explicit `["{dataset}", <col>]` template *is* that, readably); brace-escaping
(`{{`); splitting ids back into components; per-`name`/`split` dataset tokens.

**UX contract (the 9-question checklist).**
1. Side effects — none new (id computed at adapter load, inside the study
   pipeline; no network/global-cache/lock/provider-side state). No ledger row. ✓
2. Quotable summary — no new command/summary line; the `dataset:` provenance
   line is unchanged. Item ids are internal join keys, not an announced fact. ✓
3. JSON parity — the only new fact is the config value, already echoed in the
   manifest `config` blob (list serializes natively). No new field needed. ✓
4. Doc anchor — `docs/wiki/Configuration.md`, the `benchmark.mapping` section
   (new `#composite-item-ids` note); `Pipeline-Concepts.md:8` ("ids unique
   across datasets") gains the one-line "compose them when keys repeat".
5. Hint candidate — no *new silent* failure: the prior silent-ish dead end
   (uniqueness abort) is what this fixes; a degenerate template (all ids equal)
   still trips the loud uniqueness guard, and a brace typo now errors loudly. No
   coded hint added.
6. Knob bucket — **design declaration** (above); stays explicit. It does **not**
   enter condition ids (conditions hash model×prompt×… cell content, not items),
   so enabling it never silently re-keys conditions; it *does* set the item join
   key, hence the byte-for-byte guarantee for existing single-column studies.
7. Consent class — no spend, no row replacement (a composite config is for new
   pooled studies; existing rows are untouched). Not part of the money gate. ✓
8. Surface parity — config-only; identical on CLI and Python (both read the same
   `mapping.id`). No Python-only/CLI-only behavior, no prompt. ✓
9. Stability — no new exit code / JSON key / hint code. Schema widening is
   append-only. ✓

No UX-PATTERNS ledger row and no hint-catalog row to flip (no side effect, no
hint). The development checklist is satisfied by the answers above.

**Tests.** `tests/test_adapter_mapping.py` (unit, hermetic — no network, no
paid API; the file already exercises `_record_to_item` over plain dicts):
- backward-compat: existing single-column assertions stay green unchanged (the
  byte-for-byte guarantee);
- list-of-columns join: `id: ["a", "b"]` → `"<a>:<b>"`;
- `{dataset}` token: basename of `dataset_id` (`org/set_2026` → `set_2026`);
- BACKLOG example: `["{dataset}", problem_idx]` → `set_2026:6`;
- template-string spelling: `"{dataset}:{problem_idx}"` → same result;
- missing placeholder column → `AdapterError` listing valid placeholders;
- stray/unbalanced brace (`"{dataset"`) → `AdapterError` naming the segment;
- the cross-dataset duplicate now *resolves* when ids are composed (extend
  `test_load_items_duplicate_ids_across_datasets` or add a sibling asserting a
  composite mapping loads without raising), and the guard message still matches
  `duplicate item id` for the genuinely-colliding case.
`tests/test_config.py`: `MappingSpec(id=[...])` validates; empty list / empty
element rejected (`ConfigError` via `ValidationError`).

**Docs/CHANGELOG (same commit as the behavior).**
- `CHANGELOG.md` `[Unreleased] → ### Added`: one entry describing the composite/
  templated `mapping.id` (list + `{dataset}`/`{col}` template, `:` separator,
  single-column unchanged), with a `Closes: composite-item-id` trailer.
- `docs/BACKLOG.md`: **remove** the `composite-item-id` section (its design
  record lives on in this plan once archived).
- `docs/wiki/Configuration.md`: document the three `mapping.id` forms + the
  `{dataset}` token under the mapping section, with the `composite-item-ids`
  anchor the guard message points at.
- `docs/wiki/Pipeline-Concepts.md:8`: one clause — when a natural key repeats
  across datasets, compose ids to keep them unique.
- No `README.md` `**Status:**` change (that tracks the latest *released*
  version; this is `[Unreleased]`).

---

## Sequencing (canonical)

One workstream, one `feat:` commit carrying code + tests + the same-change
paperwork together. Order within the commit:
1. `_config.py` — widen `MappingSpec.id` + validator.
2. `adapters/_hf.py` — `_synthesize_id` + swap the id line.
3. `adapters/_base.py` — guard message.
4. Tests (`test_adapter_mapping.py`, `test_config.py`).
5. Same-change paperwork: CHANGELOG entry (`Closes: composite-item-id`), drop
   the BACKLOG section, wiki touches.

After the step: `make check` (lint + fast tests). The public-API snapshot is
expected to stay green (no `__all__` / CLI change); if it goes red, something
unintended changed — investigate before updating the golden set.

## Out of scope (explicitly, to prevent creep)

- A dedicated one-flag auto-namespacing knob — the explicit `["{dataset}",
  <col>]` template covers it; not worth a second spelling.
- Brace-escaping for a literal `{`/`}` in an id — no real id needs it; reserved
  braces keep the model simple, and the stray-brace check makes the rule loud.
- Splitting composite ids back into parts anywhere downstream — ids stay opaque
  keys.
- `{split}` / `{name}` (HF config-name) dataset tokens beyond `{dataset}` — add
  only on demonstrated need; record here if it ever returns to BACKLOG.
- Any non-HF adapter — only `adapter: hf` exists (`adapters/_base.py:75-80`).
