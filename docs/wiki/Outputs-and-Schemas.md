# Outputs and Schemas

Everything a study produces lives under one directory:
`<work_dir>/<output_dir>/<study>/`, where `work_dir` is the current directory by
default (override with `-C/--base-dir`, or `work_dir=` in `load_config`).

```
studies/<study>/
  items.parquet            # canonical items snapshot (all loaded items)
  solutions.parquet        # one row per (generate condition x item x epoch)
  gradings.parquet         # one row per grading event
  materialized_rubrics.parquet  # frozen per-item rubrics (only with a materializing rubric)
  log_index.parquet        # index of raw inspect .eval logs
  ledger.parquet           # cost ledger (run x stage x condition x model)
  dataset_locks.json       # dataset revisions pinned at first run
  model_locks.json         # model sample draw pinned (only with solvers.sample)
  manifests/<experiment_id>.aN.json  # one reproducibility manifest per attempt
  manifests/experiments/<stage>.<experiment_id>.json  # attempt rollup per experiment
  logs/generate/*.eval     # raw inspect logs (full transcripts; one per condition)
  logs/grade/*.eval
  export/gradings_long.parquet  # the analysis-ready long table
  export/gradings_long.csv      # byte-equivalent CSV mirror
  export/ledger.csv
```

All parquet stores use keyed, atomic upserts — re-runs replace rows, never
duplicate them. Raw `.eval` logs are append-only evidence; open them with
`inspect view` or `inspect_ai.log.read_eval_log()` to audit any number.

`dataset_locks.json` pins each dataset's resolved revision at first run so
every later run loads identical data. Every command that loads data prints
one provenance line per dataset — revision, downloaded vs reused from the HF
cache, and (only when this run wrote the lock) a
`revision pinned in dataset_locks.json` clause; the same facts ride JSON as
`datasets[]` (`{id, split, revision, revision_source, cache, cache_dir,
download_bytes, pinned_now}`).

`model_locks.json` appears only when `solvers.sample` draws the models (see
[Configuration](Configuration#field-notes)). It pins the drawn set — the sample
spec, the full universe and its content hash, and the resulting model ids — so
later runs reuse the same models (a roster that drifts only warns). Adding an
itemeval version with a *new* sample field — e.g. `where.output_text_only`, or
`allocation` / `include` — never invalidates an older lock: specs are compared
normalized through the current schema, so an absent additive field defaults in.
A *genuinely* changed spec (`n` / `seed` / `stratify_by` / `where` / …) hard-fails
`generate` / `grade` with a **change briefing** — a field-level diff and the two
safe actions — because running a different panel than the one pinned would mix
results; the read-only commands (`estimate` / `status` / `export --snapshot`)
instead **warn and proceed on the pinned panel** (`spec_drift`), so a pinned study
is always inspectable. To move forward on the write path without re-drawing, run
**`itemeval rebless CONFIG`**: it records the new spec while keeping the pinned
panel, so the lock then holds **both** the spec the panel was *drawn under*
(`sample`) and the spec it was *re-blessed to* (`reblessed_spec` / `reblessed_at`),
and later runs compare against the re-blessed spec. (Deleting the lock to re-draw
is the *other* choice — it draws a different panel, a different scientific frame.)
A command prints `models: sampled N of M … — pinned in model_locks.json` on the
first draw and a reuse line afterward (or `… — pinned panel` + a warning under spec
drift, or `… — re-blessed` after a re-bless); the same facts ride JSON as
`model_sample`
(`{source, universe_size, universe_hash, n, seed, stratify_by, allocation,
include, models, pinned_now, universe_drift, spec_drift, reblessed}`) on
estimate/generate/grade/status, and are
recorded in each run manifest and `STUDY_CARD.md`.

## `solutions.parquet` (key: condition_id, item_id, epoch)

Provenance: `study, experiment_id, attempt, condition_id, condition_slug, item_id,
dataset_id, dataset_revision, epoch, model, prompt_name, prompt_hash,
model_config_name` (`experiment_id`/`attempt` are the run identity — see
[Run identity](#run-identity) below; they are *not* part of the content key, so a
recovery attempt overwrites the failed row at the same `condition_id, item_id,
epoch`).
Sampling params, requested **and** effective: `temperature_*, top_p_*,
max_tokens_*, seed_requested, reasoning_effort[_effective],
reasoning_tokens_requested` — provider-forced values show up as a
requested/effective mismatch.
Result: `solution, stop_reason, error` (errored rows are kept and re-run next
time).
Raw call provenance: `served_provider` (the OpenRouter backend that actually
answered, e.g. `GMICloud`/`Fireworks`) and `native_finish_reason` (the provider
`finish_reason` *before* inspect flattens it into `stop_reason` — `error` and
unmapped reasons collapse to `unknown`). Null for mock models, cache replays, and
providers that don't return the fields. See
[Error-Handling#serving-provider-and-native-finish-reason](Error-Handling.md#serving-provider-and-native-finish-reason).
Cost: `input_tokens, output_tokens, total_tokens, cache_read_tokens,
cache_write_tokens, reasoning_tokens, usd, latency_s`.
Audit: `log_file, sample_uuid, created_at`.
Waves: `wave` (int, default 0), `wave_label` (null unless `--wave` was used);
old stores read as wave 0 — see
[Pipeline-Concepts#waves](Pipeline-Concepts.md#waves). Gradings and the
export carry the same two columns, inherited from the graded solution row.

## `gradings.parquet` (key: grade_condition_id, gen_condition_id, item_id, epoch)

Provenance: `study, experiment_id, attempt, grade_condition_id/slug, gen_condition_id,
item_id, epoch, grade_kind` (judge|verifiable), `grader_name, grader_model,
rubric_name, rubric_hash, scorer_name, solution_hash`.
`solution_hash` is the sha256 of the solution text this grade scored: a cell counts
as graded only while it still matches the current solution, so a solution overwritten
at a fixed key auto-re-grades on the next `grade` run instead of leaving a stale
score (and `status` reports it as `stale`). Null on stores written before the column
existed (read as "unknown → matches", so an old study never force-re-grades).
Result: `score, score_raw, parse_ok, parse_error, reasoning,
judge_completion, error`.
Raw judge-call provenance: `served_provider, native_finish_reason` (as in
solutions; null for verifiable/skip rows, which make no model call).
Cost/audit: token columns, `usd` (0.0 for verifiable), `latency_s`,
`log_file`, `created_at`.

Written only when a [materializing rubric](Configuration.md#two-stage-materialized-rubrics)
runs: `materialized_rubrics.parquet` (key: `materialize_id, item_id`) holds the
frozen per-item rubrics — `materialize_id` (build-template hash + materializer
model), `rubric_name, materializer_model, build_template_hash, rubric_text,
rubric_hash`, `usd`, token columns, `error, experiment_id, attempt, created_at`. Reused across
graders/solutions/replications/resumes; an `error` row is retried next run.

Invariant: `parse_ok=false` ⟺ `parse_error` set ⟺ `score` null (for rows
without a sample-level `error`). Parse failures are final; errors re-run.

## `export/gradings_long.parquet` — one row per grading event

The left-join of gradings onto solutions: 54 columns, grouped as

- **Design cell**: `study, item_id, dataset_id, dataset_revision, model,
  prompt_name, prompt_hash, model_config_name, replication,
  gen_condition_id/slug, grade_condition_id/slug, grade_kind, grader_name,
  grader_model, rubric_name, rubric_hash, scorer_name`
- **Outcome**: `score, score_raw, parse_ok, parse_error, reasoning,
  solution, truncated, judge_completion`
  — `truncated` is `True` when the solution was cut at a length cap
  (`max_tokens`/`model_length`) with non-empty text: graded as finished but a
  budget cut, not content. Filter it out of a content-validity analysis
  (`df[~df.truncated]`). See [Error-Handling#truncation](Error-Handling.md#truncation).
- **Params**: `temperature_requested, temperature_effective, reasoning_effort`
- **Cost**: `gen_*` and `grade_*` token counts, `gen_usd, grade_usd,
  gen_latency_s, grade_latency_s`
- **Audit**: `gen_experiment_id, gen_attempt, grade_experiment_id,
  grade_attempt, gen_log_file, grade_log_file, created_at`
- **Raw call provenance**: `gen_served_provider, gen_native_finish_reason`
  (solver call), `grade_served_provider, grade_native_finish_reason` (judge call)
  — the OpenRouter backend that served the call and its raw `finish_reason` before
  inspect's `stop_reason` flatten. Diagnose a provider soft failure (HTTP 200 +
  `finish_reason=error` + empty content) without opening the `.eval`. Null when
  the provider/cache/mock did not return them. See
  [Error-Handling#serving-provider-and-native-finish-reason](Error-Handling.md#serving-provider-and-native-finish-reason).

Never aggregated; parse failures present with `parse_ok=false`. This is the
input to IRT / mixed-model analysis.

## `ledger.parquet` (key: experiment_id, attempt, stage, condition_id, model)

`provider` (the inspect prefix of `model` — which provider's billing the row
belongs to), `calls`, token totals, `usd`, `priced` (was a price known),
`batch` (was batch mode on), `created_at`. `export` checks ledger totals equal
row sums per stage (`internally reconciled`). Null `usd` on a row means "model
unpriced"; `0.0` with empty tokens means the call was served by the response
cache (free).

## `manifests/<experiment_id>.aN.json`

Per attempt: `experiment_id, attempt, stage, created_at`; `itemeval_version, python_version,
packages` (inspect-ai, pandas, pyarrow, pydantic, pyyaml, datasets);
`config_path, config_sha256` + the full parsed config; per-dataset resolved
revisions and an items content hash; solver/rubric templates each as
`{name, source, path, sha256}` (`source` is `local` or `builtin`; built-in
templates record a package-relative, machine-independent `path`);
resolved graders; requested sampling params (+ `sampling_effective` per
condition, backfilled after the run); `endpoints_effective` per condition
(`{provider, base_url, served_model, execution_model, routed}`, backfilled after
the run — the endpoint and provider-returned model snapshot that actually
answered; `base_url` is null on the provider's default endpoint; under
`budget.prefer_native_batch`, `execution_model` is the native id the calls were
sent to and `routed` flags when it differs from the sampled `model`); policy,
effective replications/items
limit/batch; the complete condition grid with payloads; the condition ids
this run selected; and the estimate the run was approved under
(`estimate_usd` = the remaining figure, `estimate_full_usd` alongside).

Because manifests are immutable, they double as an **endpoint drift**
record: when past manifests show inconsistent `served_model` snapshots for a
model id this run uses (or the last run is >30 days old), `generate`/`grade`
print a best-effort warning — rows remain distinguishable by `experiment_id`/`attempt`.

## Run identity

Every row and manifest carries `experiment_id` + `attempt` instead of a
per-invocation run id. The `experiment_id` is `sha256(config_digest : study :
stage)[:12]`, where `config_digest` is the **semantic** config — re-parsed
through the schema, identity-bearing fields only — so comments, whitespace, key
order, and pure execution/cost knobs (`output_dir`, `cache`, the `budget` block,
`provider_routing`, `cache_prompt`) never change it. The consequences:

- A re-run of an **unchanged config recovers the same experiment** (next
  `attempt`) and **converges** into the existing store — completed cells are
  never re-paid; `generate`/`grade` announce `recovery: attempt N of experiment
  <id> — converging into existing results`.
- A genuine **design edit forks** a new experiment (the digest changes);
  `--new-run` forces a fresh one even from an identical config. Grown items or a
  drifting roster under an unchanged config are a soft warning, not a fork.
- The append-only `experiments/<stage>.<experiment_id>.json` index rolls up an
  experiment's attempts and names the current one; `status` shows
  `experiments: <id> (<stage>) — N attempts, current aN` whenever a run recovered.

`experiment_id`, `attempt`, and `run_kind` (`recovery`/`new`) ride the
`generate`/`grade` `--json` results; `status --json` carries an `experiments`
array. Because the identity columns are **not** part of any content key, the
rename never re-keyed conditions or rows.

## Snapshots

`itemeval export CONFIG --snapshot NAME` (Python:
`export_study(cfg, snapshot="NAME")` → `ExportResult.snapshot_path`) runs a
normal export, then freezes an immutable copy:

```
export/snapshots/<name>/
  gradings_long.parquet    # frozen copy of the just-written export
  gradings_long.csv
  ledger.csv
  dataset_locks.json       # pins as of snapshot time
  model_locks.json         # the model sample pin (when solvers.sample was used)
  materialized_rubrics.parquet  # frozen rubrics (when a materializing rubric was used)
  manifests/               # every manifest covering included rows
  snapshot.json            # name, created_at, itemeval_version, config_sha256,
                           # run_ids, row/condition counts, spend totals
  STUDY_CARD.md            # self-describing record (see below)
```

Why copy, not reference: the current-state layer is mutable (upserts
replace), so "the table as of pub-1" cannot be reconstructed later — history
is materialized at freeze time. Rules: names match
`^[a-z0-9][a-z0-9_-]{0,63}$`; an existing name is **refused** (exit 2,
`snapshot 'pub1' exists — choose a new name`) — refusing overwrite removes
the only destructive path; snapshots are never read by any compute path (not
resume, not merge). Consume them like any export (read the parquet; zip the
folder to share). `status` lists existing snapshots
(`snapshots: pub1 (2026-06-11, 1,920 rows)`; `snapshots[]` in JSON).

**STUDY_CARD.md** is the HF-dataset-card analog written into every snapshot:
YAML front-matter (schema-versioned, `itemeval_study_card: 1` — datasets and
pins, models, replications, graders, rows, spend) followed by sections every
number of which is derived from existing stores: Design (the facet grid with
content hashes and template sources), Execution (one row per run from
manifests + ledger, including `served_model` per condition — exactly which
provider snapshot answered), Results (completion matrix and per-condition
mean scores, labeled *descriptive, not analysis*), Costs (per-stage and
per-provider spend, savings decomposition), and Reproduce (the config
verbatim plus dataset pins). Configs must never contain secrets — keys live
in the environment.
