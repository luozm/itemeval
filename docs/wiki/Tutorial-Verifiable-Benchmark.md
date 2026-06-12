# Tutorial 1 — Score a verifiable benchmark for ~2 cents

**Use case:** "How does model X actually do on benchmark Y — per problem, not
just the headline number?"

You will run the complete itemeval pipeline on
[AIME 2025](https://huggingface.co/datasets/MathArena/aime_2025) (competition
math, integer answers) with `openai/gpt-5-mini`. Because the answers are
integers, grading uses the built-in `numeric` scorer — pure Python, no judge
model — so the only cost is generation. This exact run has been validated live:
5 problems, 5/5 correct, about $0.014 of generation and $0.00 of grading.

**You need:** `pip install itemeval[openai]` and an `OPENAI_API_KEY` in your
environment. Time: ~10 minutes.

## Step 1 — Write the config

One YAML file describes the whole study. Save this as `aime.yaml`:

```yaml
study: aime_quickstart
benchmark:
  adapter: hf                    # load from the HuggingFace Hub
  datasets:
    - id: MathArena/aime_2025    # dataset revision auto-pins at first run
      split: train
  mapping:                       # dataset columns -> itemeval's Item fields
    id: problem_idx
    input: problem
    target: answer               # integer answers -> the numeric scorer
solvers:
  models: [openai/gpt-5-mini]
  max_tokens: 8192               # room for hidden reasoning + the "ANSWER:" line
facets:
  prompt: [builtin:minimal]      # packaged template; ends with 'state your final
                                 # answer on a line starting with "ANSWER:"'
  scorer: numeric                # verifiable scorer: extracts the last number, $0
  model_config: [{name: low, reasoning_effort: low}]   # keep it fast and cheap
budget:
  policy: dev                    # dev = only the first dev_items items
  dev_items: 5
```

Three choices worth noticing:

- **`mapping`** is the only thing you change to point at a different dataset:
  name the columns that hold the problem id, the problem text, and the
  reference answer.
- **`scorer: numeric`** means grading is free. The packaged `builtin:minimal`
  prompt instructs the model to end with an `ANSWER:` line, which the scorer
  parses.
- **`policy: dev`** caps the run at the first 5 items. This is the default
  posture for any new config — prove the pipeline first, scale later
  ([Tutorial 5](Tutorial-Budget-and-Scale.md)).

## Step 2 — Estimate before you spend

```bash
itemeval estimate aime.yaml
```

This makes **no model API calls**. It renders the actual prompts, applies a
token heuristic and the pricing table, and prints projected calls, tokens, and
dollars per stage. Expect a projection of well under $0.10 for this config
(estimates are deliberately conservative — actuals usually come in lower).
It also prints which pricing table the numbers came from; before bigger runs,
add `--refresh-pricing` to pull current prices.

## Step 3 — Generate solutions

```bash
itemeval generate aime.yaml
```

This expands the design grid — here a single condition,
(gpt-5-mini × builtin:minimal × low) — and runs one inspect_ai task over the 5
items, with live progress. Every solution is upserted into
`studies/aime_quickstart/solutions.parquet` with full provenance: prompt hash,
requested vs effective sampling params, tokens, dollars, and a pointer to the
raw `.eval` transcript.

If it's interrupted or a provider call fails, just run the same command again —
completed items skip, failed ones retry, and nothing is double-paid.

## Step 4 — Grade

```bash
itemeval grade aime.yaml
```

The `numeric` scorer parses each stored solution's `ANSWER:` line and compares
it to the target. No LLM, no cost, instant. Results go to
`studies/aime_quickstart/gradings.parquet`; any solution whose answer could not
be parsed is kept and flagged (`parse_ok=false`), never silently dropped.

## Step 5 — Export and look at your data

```bash
itemeval export aime.yaml
```

This writes the analysis-ready table and prints the spend summary:

- `studies/aime_quickstart/export/gradings_long.parquet` (+ a CSV mirror) —
  **one row per grading event**, 47 columns.
- `studies/aime_quickstart/export/ledger.csv` — the cost ledger.

Open it:

```python
import pandas as pd

df = pd.read_parquet("studies/aime_quickstart/export/gradings_long.parquet")
df[["item_id", "model", "score", "score_raw", "solution", "gen_usd"]]
```

Each row tells you, for one problem: the score (`1.0`/`0.0`), the raw value the
scorer extracted (`score_raw`), the full solution text, token counts, and what
that call cost. The aggregate accuracy is `df.score.mean()` — but the point of
itemeval is that you have the rows, not just the mean.

## Step 6 — Check the books

```bash
itemeval status aime.yaml
```

`status` shows the completion matrix (5/5 done), error/parse-failure counts,
and spend per stage. Run it any time; like `estimate`, it never calls a model.

## What just happened

- The dataset revision was **pinned** at first run (`dataset_locks.json`), and a
  **manifest** was written per run with template hashes, model ids, effective
  sampling params, and package versions — re-running the same config
  reproduces the same study ([Pipeline Concepts](Pipeline-Concepts.md)).
- Everything is **resumable**: re-run any command and completed work skips.
- Want more items? Raise `dev_items`, or switch `policy` —
  see [Tutorial 5](Tutorial-Budget-and-Scale.md) before scaling.

## Variations

- **Letter answers** (A/B/C/D): `scorer: multiple_choice` — your dataset's
  `input` column must already contain the choices.
- **String answers**: `scorer: exact_match`.
- **Free-form answers that need judgment**: you need an LLM judge —
  that's [Tutorial 2](Tutorial-LLM-Judge.md).
