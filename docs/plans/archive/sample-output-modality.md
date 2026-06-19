# Implementation plan — sample-output-modality (output-modality filter for `solvers.sample`)

**Status: IMPLEMENTED 2026-06-19.** This file is the design record for the
shipped feature (`Closes: sample-output-modality`). Written and implemented in
one session against inspect_ai 0.3.239 (pinned in `uv.lock`) and the OpenRouter
`/api/v1/models` roster, whose `architecture.output_modalities` was confirmed by
a live pull on 2026-06-19.

## Problem

The `pricing-table` universe restricts to "runnable text models" via
`ModelPrice.text_model`, computed at `--refresh-pricing` as

    "text" in input_modalities and "text" in output_modalities and bool(params)

The output clause only checks that text is **present** in the emitted
modalities. A model that emits text **and** image/audio (e.g.
`google/gemini-3-pro-image`, `openai/gpt-audio`) therefore passes the gate and
is drawable — but it can't be the object of a closed-text eval. The live pull
found **10 of 309 drawable ids** in this state (8 `image+text`, 2 `audio+text`),
~3% and growing as multimodal-output models ship. Until this feature they had to
be hand-listed in `solvers.sample.exclude` (the recipe a consuming study used).

## What shipped

Three small touches; `output_modalities` was already read at refresh, just
discarded.

1. **Persist the metadata** (`src/itemeval/budget/_pricing.py`). New additive
   optional field `ModelPrice.output_modalities: list[str] | None` (the raw list,
   not a derived bool — so a later "drop audio-output only" filter needs no
   re-refresh; `None` for the seed / pinned tables, like `created`, so no schema
   version bump and `is_schema_stale` is unaffected). `refresh_pricing` now keeps
   `out_mods` (already computed for the `text_model` gate) and stores
   `output_modalities=out_mods or None`.

2. **The filter** (`src/itemeval/_config.py`, `src/itemeval/_modelsample.py`).
   New `ModelUniverseFilter.output_text_only: bool | None = None`. In
   `_apply_where`, drop any model whose output set isn't exactly `{"text"}` when
   `True` (and the symmetric inverse for `False`), parallel to the input-side
   `where.multimodal` clause:

       if where.output_text_only is not None:
           text_only = p is not None and set(p.output_modalities or []) == {"text"}
           if text_only != where.output_text_only:
               continue

   A drawable model always has `output_modalities` (the gate requires `"text"`
   in it), so the `p is None` guard only matters for the degenerate `False` case.

3. **No new provenance code.** `where` is dumped wholesale into the
   `model_locks.json` spec (`sample.where.model_dump()`) and the manifest config
   echo, so `output_text_only` flows through exactly like `multimodal`/`reasoning`
   — `ModelSampleResult` carries no individual `where` field. The `where`
   rejection for list/file universes (`_config.py` model-validator) already
   covers the new field with no per-field handling.

## Decisions

- **Raw list, not a derived bool.** Asked for explicitly: keep the metadata, not
  a one-off reduction. Mild asymmetry with the input-side `multimodal: bool` is
  acceptable — output modalities are few and meaningful.
- **Opt-in `where` filter, not a default universe gate.** Unlike the
  non-reproducible `-latest`/`~` alias ids (a separate default-drop fix), a
  multimodal-*output* model is a legitimate, reproducible, paid model — a valid
  target for a study that *means* to sample it. The package can't presume it
  unwanted, so the default frame is unchanged.

## Open questions (not shipped)

- A hint when a non-text-output model lands in a draw without the filter set (the
  alias-guard safety-net pattern) — deferred; purely opt-in for now.
- A more general `where.output: [text]` allowlist instead of the boolean —
  deferred, no second use yet (one boolean per "don't over-engineer").

## Tests

- `tests/test_pricing.py::test_refresh_pricing_merges_openrouter` — extended with
  a text+image generator entry; asserts `output_modalities` is persisted and the
  generator still passes the `text_model` gate.
- `tests/test_model_sample.py::test_where_output_text_only` — generators drawable
  without the filter; `output_text_only: true` keeps only text-only-output models
  and pins the value in the lock spec; `false` is the symmetric inverse.
