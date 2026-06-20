# Pipeline Concepts

## Items

The canonical unit is an `Item`: `id`, `input` (the problem), `target`
(reference answer, may be empty), `grading_scheme` (rubric/points text, may be
null), `metadata`. Adapters turn benchmark rows into items via the
`benchmark.mapping` spec; item ids must be unique across all configured
datasets — when a natural key repeats across datasets, compose a unique id with
`mapping.id` (see [Configuration](Configuration.md#composite-item-ids)).

## Facets and conditions

A **facet** is an experimental factor: solver model, prompt variant,
model-config (sampling/reasoning settings), grader, rubric, replication.
itemeval crosses them fully (`crossing: full`):

- **Generate conditions** = `models × facets.prompt × facets.model_config`
- **Grade conditions** = `facets.grader × facets.rubric`, plus one
  verifiable-scorer condition if `facets.scorer` is set
- **Replications** = inspect epochs; every (generate condition × item) runs
  `facets.replications` times

## Condition ids

Every condition gets a stable, content-derived id:
`<human-readable-slug>--<sha256-of-payload>[:12]`, e.g.
`gpt-5-mini_minimal_default--3fa9c2d1e0b4`. The hashed payload contains the
*content* that defines the condition — model id, resolved sampling params,
prompt/rubric name **and content hash**. Consequences:

- Editing a prompt file changes its conditions' ids — old rows stay in the
  store under the old id; the edited condition starts fresh. You can never
  silently mix results from two prompt versions.
- Ids are reproducible across machines and runs. Replication/epoch is never
  part of the id (it's a column).
- When a run's grid disagrees with stored rows — same facet name with a
  different content hash (edited template), or an unchanged slug mapping to
  a new id (changed sampling param) — `generate`/`grade` print a **config
  drift warning** naming the facet, the hash change, and the affected row
  count (also in the run JSON as `warnings[]`). Warnings never block: the
  run proceeds under the new condition by design.

## Two decoupled stages

**Generate** runs one inspect task per generate condition and upserts one row
per (condition × item × epoch) into `solutions.parquet`.

**Grade** fans out grade conditions over **stored** solutions — it never
re-generates and never writes the solutions store:

- *Verifiable* scorers (`exact_match`, `multiple_choice`, `numeric`) are pure
  Python over the stored text: no model, no cost.
- *Judge* grading builds a fresh inspect task whose dataset is the stored
  solutions rendered into a rubric template; the judge model runs at
  temperature 0 with its own logs, retries, caching, batch eligibility, and
  cost accounting.

Because the stages share only the solutions store, adding a new grader or
rubric later (`itemeval grade --grader new_judge`) re-uses every stored
solution at zero generation cost — that is the core reason this package
exists on top of inspect_ai rather than using inspect's in-eval scorers.

## Judge output contract

The packaged format suffix instructs judges to end with a fenced JSON block
`{"score": <number>, "reasoning": "..."}`. Parsing is strict (fenced blocks
last-to-first, then raw JSON objects) with exact failure codes
(`no_json_object`, `no_score_in_json`, `score_not_numeric`,
`score_not_finite`). **Parse failures are results, not errors**: the row is
kept with `parse_ok=false` and is *final* — it is not retried on re-runs
(use `--force` to re-grade). Sample-level *errors* (provider failures), by
contrast, leave `error` set and are re-attempted on the next run.

## Waves

`generate cfg.yaml --wave 2026-07` re-observes the **same** design scope as
a new observation, keeping both — the substrate for drift / model-downgrade
detection over time. A wave is an **epoch block**: wave *w* with
`replications: R` occupies epochs `w·R+1 … (w+1)·R`. The epoch axis is
already in every store key and both cache keys, so new waves are new keys —
no migration, no replacement — and fresh epoch numbers give fresh draws
automatically. Mechanics:

- `--wave` is a design declaration: always explicit, never auto-fired.
  Without it, behavior is exactly the single-wave default (`wave` column
  constantly 0, `wave_label` null — zero noise).
- The offset eval runs with the **local response cache off** (announced:
  `wave 2026-07: local response cache off — re-observations must be fresh
  draws`) — otherwise inspect would replay the wave-0 bytes as "new"
  observations. By design waves never replay: they cost full price.
- Resumable mid-wave: re-running with the same label fills only the block's
  missing work; an existing label resumes its block, a new label allocates
  the next free one.
- `grade --wave LABEL` grades exactly that block's solutions under the
  existing grade conditions; plain `grade` stays scoped to wave 0.
- Rows carry `wave` (int) and `wave_label` columns
  (solutions/gradings/export, additive — old stores read as wave 0);
  manifests and the ledger record the `epoch_offset`. `status` prints
  per-wave completion (generate and graded counts, e.g.
  `waves: 0 — gen 8/8 · graded 8/8, 1 (w1) — gen 8/8 · graded 0/8`) only
  when more than one wave exists; the main completion matrix stays scoped
  to wave 0, so wave progress lives entirely on this line.
- Analysis: `df.groupby("wave")` over the export, plus `served_model` from
  the manifests to attribute differences.
- Config drift warnings are load-bearing here: a changed template between
  waves means the new wave is a different condition — the warning says so.

## Resume semantics

The parquet store is the source of truth; raw `.eval` logs are evidence.

- Upserts are keyed — generate: `(condition_id, item_id, epoch)`; grade:
  `(grade_condition_id, gen_condition_id, item_id, epoch)` — re-runs replace
  rather than duplicate.
- A generate item is re-run iff any epoch is missing or errored. A solution
  is re-graded iff it has no successful grading row under that grade
  condition.
- `--force` re-runs everything selected; `--condition <id-prefix-or-slug>`
  narrows any run.
- Interrupting a run is always safe; re-invoke the same command.

## Caching (why re-runs are free)

Two layers, both from inspect_ai:

1. **Local response cache** (`cache: true`, the default): identical model
   calls (same model, prompt, sampling config, epoch) are served from disk.
   Re-running a wiped study re-pays nothing; such rows record `usd = 0.0`.
   Per-epoch caching keeps replications distinct.
2. **Provider prompt caching**: the grade stage sets `cache_prompt: auto`, so
   repeated rubric+problem prefixes across solutions are cache-eligible on
   providers that support it.

## Reproducibility

Every run writes `manifests/<experiment_id>.aN.json` (the run identity:
`experiment_id` + `attempt`, so a recovery re-run of an unchanged config records
a new attempt of the *same* experiment rather than a forked id — see
[Outputs#run-identity](Outputs-and-Schemas.md#run-identity)): config content
hash, dataset ids + resolved revisions, prompt/rubric content hashes, model ids,
requested sampling params (effective per-condition values backfilled after the
run), seeds, package versions, the full condition grid, and the estimate the run
was approved under. Dataset revisions are pinned at first run in
`dataset_locks.json`. Same manifest + cache ⇒ identical results.
