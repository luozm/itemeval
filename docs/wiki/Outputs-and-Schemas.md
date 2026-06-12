# Outputs and Schemas

Everything a study produces lives under one directory:
`<work_dir>/<output_dir>/<study>/`, where `work_dir` is the current directory by
default (override with `-C/--base-dir`, or `work_dir=` in `load_config`).

```
studies/<study>/
  items.parquet            # canonical items snapshot (all loaded items)
  solutions.parquet        # one row per (generate condition x item x epoch)
  gradings.parquet         # one row per grading event
  log_index.parquet        # index of raw inspect .eval logs
  ledger.parquet           # cost ledger (run x stage x condition x model)
  dataset_locks.json       # dataset revisions pinned at first run
  manifests/<run_id>.json  # one reproducibility manifest per run
  logs/generate/<condition_id>/*.eval   # raw inspect logs (full transcripts)
  logs/grade/<condition_id>/*.eval
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

## `solutions.parquet` (key: condition_id, item_id, epoch)

Provenance: `study, run_id, condition_id, condition_slug, item_id,
dataset_id, dataset_revision, epoch, model, prompt_name, prompt_hash,
model_config_name`.
Sampling params, requested **and** effective: `temperature_*, top_p_*,
max_tokens_*, seed_requested, reasoning_effort[_effective],
reasoning_tokens_requested` — provider-forced values show up as a
requested/effective mismatch.
Result: `solution, stop_reason, error` (errored rows are kept and re-run next
time).
Cost: `input_tokens, output_tokens, total_tokens, cache_read_tokens,
cache_write_tokens, reasoning_tokens, usd, latency_s`.
Audit: `log_file, sample_uuid, created_at`.

## `gradings.parquet` (key: grade_condition_id, gen_condition_id, item_id, epoch)

Provenance: `study, run_id, grade_condition_id/slug, gen_condition_id,
item_id, epoch, grade_kind` (judge|verifiable), `grader_name, grader_model,
rubric_name, rubric_hash, scorer_name`.
Result: `score, score_raw, parse_ok, parse_error, reasoning,
judge_completion, error`.
Cost/audit: token columns, `usd` (0.0 for verifiable), `latency_s`,
`log_file`, `created_at`.

Invariant: `parse_ok=false` ⟺ `parse_error` set ⟺ `score` null (for rows
without a sample-level `error`). Parse failures are final; errors re-run.

## `export/gradings_long.parquet` — one row per grading event

The left-join of gradings onto solutions: 45 columns, grouped as

- **Design cell**: `study, item_id, dataset_id, dataset_revision, model,
  prompt_name, prompt_hash, model_config_name, replication,
  gen_condition_id/slug, grade_condition_id/slug, grade_kind, grader_name,
  grader_model, rubric_name, rubric_hash, scorer_name`
- **Outcome**: `score, score_raw, parse_ok, parse_error, reasoning,
  solution, judge_completion`
- **Params**: `temperature_requested, temperature_effective, reasoning_effort`
- **Cost**: `gen_*` and `grade_*` token counts, `gen_usd, grade_usd,
  gen_latency_s, grade_latency_s`
- **Audit**: `gen_run_id, grade_run_id, gen_log_file, grade_log_file,
  created_at`

Never aggregated; parse failures present with `parse_ok=false`. This is the
input to IRT / mixed-model analysis.

## `ledger.parquet` (key: run_id, stage, condition_id, model)

`provider` (the inspect prefix of `model` — which provider's billing the row
belongs to), `calls`, token totals, `usd`, `priced` (was a price known),
`batch` (was batch mode on), `created_at`. `export` checks ledger totals equal
row sums per stage (`internally reconciled`). Null `usd` on a row means "model
unpriced"; `0.0` with empty tokens means the call was served by the response
cache (free).

## `manifests/<run_id>.json`

Per run: `run_id, stage, created_at`; `itemeval_version, python_version,
packages` (inspect-ai, pandas, pyarrow, pydantic, pyyaml, datasets);
`config_path, config_sha256` + the full parsed config; per-dataset resolved
revisions and an items content hash; solver/rubric templates each as
`{name, source, path, sha256}` (`source` is `local` or `builtin`; built-in
templates record a package-relative, machine-independent `path`);
resolved graders; requested sampling params (+ `sampling_effective` per
condition, backfilled after the run); `endpoints_effective` per condition
(`{provider, base_url, served_model}`, backfilled after the run — the endpoint
and provider-returned model snapshot that actually answered; `base_url` is null
on the provider's default endpoint); policy, effective replications/items
limit/batch; the complete condition grid with payloads; the condition ids
this run selected; and the estimate the run was approved under
(`estimate_usd` = the remaining figure, `estimate_full_usd` alongside).

Because manifests are immutable, they double as an **endpoint drift**
record: when past manifests show inconsistent `served_model` snapshots for a
model id this run uses (or the last run is >30 days old), `generate`/`grade`
print a best-effort warning — rows remain distinguishable by `run_id`.
