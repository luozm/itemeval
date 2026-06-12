# CLI Reference

```
itemeval init DIR [options]
itemeval {estimate,generate,grade,export,status} CONFIG [options]
```

`init` scaffolds a new study into `DIR`; every other command takes the config
YAML path as its argument. `itemeval` is installed as a console script;
`python -m itemeval.cli` is equivalent.

The run/report commands (`estimate|generate|grade|export|status`) accept
`-C/--base-dir DIR` to set the **work directory** that anchors outputs (the
`studies/` tree). It defaults to the current directory; inputs (prompts/rubrics)
always resolve relative to the config file, independent of `-C`.

`estimate`, `generate`, `grade`, and `status` also accept
`--policy {dev,full-interactive,full-batch}`, overriding `budget.policy` for
that invocation only (see
[Budget-and-Costs#policies](Budget-and-Costs.md#policies)).

## Exit codes (all commands)

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | unexpected error, or at least one condition failed during a run |
| 2 | config / template / adapter error (and argparse usage errors) |
| 3 | cost gate declined, or confirmation required in a non-interactive shell |
| 4 | projected cost exceeds `budget.max_usd` (hard cap; `--yes` does not override) |

## `init` — scaffold a new study

```
itemeval init DIR [--with-templates] [--force]
```

Writes `DIR/config.yaml` — a runnable starter study (mock provider, the USAMO
demo dataset, `builtin:` template references) named after `DIR`. Refuses to
overwrite an existing `config.yaml` unless `--force`. With `--with-templates`,
also copies the referenced built-in prompts/rubrics into `DIR/prompts/` and
`DIR/rubrics/` and rewrites the config to reference those local copies (bare
names). Then `cd DIR && itemeval status config.yaml`.

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
                  [--display {none,plain,rich,full}] [--json]
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
- `--json` emits the run result as a single JSON document on stdout (the
  `GenerateResult` fields plus `pricing`, `estimate_usd`, and the `gate`
  outcome) and silences the live display unless `--display` is passed. A
  gate stop still emits a JSON document (projected cost, gate reason, the
  `--yes` rerun command) before exiting 3/4.

## `grade` — stage 2 (resumable, re-runnable)

```
itemeval grade CONFIG [-y/--yes] [--force] [--condition F]...
               [--grader N]... [--rubric N]... [--display ...] [--json]
```

Same flow over grade conditions (including `--json`, as on `generate`). Verifiable conditions cost $0 and need no
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

## Hints

Commands may end with up to two dim `hint:` lines on **stderr** — one
observed fact from this run plus a doc pointer
(`hint: <fact> — learn more: <wiki-page#anchor>`). Hints never change
behavior and never block. Set `ITEMEVAL_HINTS=off` to silence them in the
text rendering; under `--json` they always ride as structured data in the
`hints` array (`{code, message, learn_more}`), uncapped.

Hint codes are stable and append-only:

| Code | Fires when |
|---|---|
| `cache-zero-reads` | same-prefix calls were scheduled for a provider cache discount but none engaged |
| `empty-solutions` | completions finished without an API error but produced no gradable text |
| `unpriced-models` | a model has no pricing-table entry (dollar columns missing; run unaffected) |

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
