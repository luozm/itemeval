# CLI Reference

```
itemeval {estimate,generate,grade,export,status} CONFIG [options]
```

Every command takes the config YAML path as its argument. `itemeval` is
installed as a console script (`./.venv/bin/itemeval`); `python -m
itemeval.cli` is equivalent.

## Exit codes (all commands)

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | unexpected error, or at least one condition failed during a run |
| 2 | config / template / adapter error (and argparse usage errors) |
| 3 | cost gate declined, or confirmation required in a non-interactive shell |
| 4 | projected cost exceeds `budget.max_usd` (hard cap; `--yes` does not override) |

## `estimate` — projected cost, no model API calls

```
itemeval estimate CONFIG [--stage {generate,grade,all}] [--refresh-pricing] [--json]
```

Prints per-stage and per-condition projections (calls, tokens, USD), flags
unpriced models, and warns when generation is uncapped (no `max_tokens`).
`--refresh-pricing` pulls current per-token prices from the OpenRouter API
into a local cache first. The estimate always projects the **full**
policy-effective grid; completed work is not subtracted (conservative).

## `generate` — stage 1 (resumable)

```
itemeval generate CONFIG [-y/--yes] [--force] [--condition F]...
                  [--display {none,plain,rich,full}]
```

Flow: estimate → print projection → **gate** → run each generate condition
serially → upsert solutions, log index, ledger → write manifest. Conditions
already complete print `skipped: complete`. A condition whose eval fails is
reported and the rest continue (final exit 1).

- `-y/--yes` confirms the gate non-interactively (never overrides `max_usd`).
- `--force` re-runs completed work (rows are replaced, not duplicated).
- `--condition F` (repeatable) selects conditions by exact id, id prefix, or
  slug — e.g. `--condition gpt-5-mini_minimal_default`.
- `--display` passes through to inspect (default `none`; try `rich`
  interactively).

## `grade` — stage 2 (resumable, re-runnable)

```
itemeval grade CONFIG [-y/--yes] [--force] [--condition F]...
               [--grader N]... [--rubric N]... [--display ...]
```

Same flow over grade conditions. Verifiable conditions cost $0 and need no
model. `--grader`/`--rubric` (repeatable) narrow to specific judges/rubrics —
useful for adding a new grader over existing solutions. The summary line
reports `parse_failures` (rows kept with `parse_ok=false`).

## `export` — analysis-ready tables

```
itemeval export CONFIG [--json]
```

Joins gradings × solutions into `export/gradings_long.parquet` (one row per
grading event, 45 columns) plus a byte-equivalent CSV and `ledger.csv`.
Prints per-stage spend and the internal reconciliation verdict (ledger totals
vs row sums; reconciliation against provider dashboards is a manual step).

## `status` — completion matrix, no model API calls

```
itemeval status CONFIG [--json]
```

Prints datasets (id @ revision, item counts), the policy-effective scope,
both condition tables with `done/expected`, error and parse-failure counts,
spend per stage, and manifest count. `--json` emits the full structured
report (also available for `estimate` and `export`).

## Typical session

```bash
itemeval estimate cfg.yaml --refresh-pricing   # sanity-check projected $
itemeval generate cfg.yaml                     # prompts if above confirm_above_usd
# ... interrupted? just re-run; completed conditions skip ...
itemeval generate cfg.yaml
itemeval grade    cfg.yaml
itemeval grade    cfg.yaml --grader second_judge   # later: new judge, $0 generation
itemeval export   cfg.yaml
itemeval status   cfg.yaml
```
