# Pipeline Concepts

## Items

The canonical unit is an `Item`: `id`, `input` (the problem), `target`
(reference answer, may be empty), `grading_scheme` (rubric/points text, may be
null), `metadata`. Adapters turn benchmark rows into items via the
`benchmark.mapping` spec; item ids must be unique across all configured
datasets.

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

Every run writes `manifests/<run_id>.json`: config content hash, dataset ids
+ resolved revisions, prompt/rubric content hashes, model ids, requested
sampling params (effective per-condition values backfilled after the run),
seeds, package versions, the full condition grid, and the estimate the run
was approved under. Dataset revisions are pinned at first run in
`dataset_locks.json`. Same manifest + cache ⇒ identical results.
