# Tutorial 3 — Compare models and prompts with replications

**Use case:** "Is model A really better than model B on my benchmark — or is
the gap within run-to-run noise? Does the prompt matter more than the model?"

A single accuracy number can't answer that. You will run a small **crossed
design** — 2 models × 2 prompts × 3 replications over AIME 2025 problems —
and analyze the resulting item-response table, where every (item × model ×
prompt × replication) cell is a row.

**You need:** `pip install itemeval[all]` (or `[openai]` + `[anthropic]`),
`OPENAI_API_KEY` and `ANTHROPIC_API_KEY`. Any inspect_ai model ids work — to
stay on one key, route both models through OpenRouter
(`openrouter/<org>/<model>` with `OPENROUTER_API_KEY`). Time: ~20 minutes;
cost: tens of cents at `dev` scope.

## Step 1 — Declare the design, not the runs

Save as `compare.yaml`:

```yaml
study: model_prompt_compare
benchmark:
  adapter: hf
  datasets:
    - id: MathArena/aime_2025
      split: train
  mapping: {id: problem_idx, input: problem, target: answer}
solvers:
  models: [openai/gpt-5-mini, anthropic/claude-haiku-4-5]
  max_tokens: 8192
facets:
  prompt: [builtin:minimal, builtin:standard]  # a terse and a "careful expert" prompt
  scorer: numeric                              # free grading -> replications cost only generation
  replications: 3                              # every cell runs 3 times
  model_config: [{name: low, reasoning_effort: low}]
budget:
  policy: dev
  dev_items: 10
  confirm_above_usd: 2
```

itemeval expands this into **4 generate conditions**
(2 models × 2 prompts × 1 model_config), each run over 10 items × 3
replications = 120 solutions, each graded for free. You declare the facets;
the grid, the run order, and the bookkeeping are itemeval's job.

## Step 2 — Inspect the grid before running anything

```bash
itemeval status compare.yaml
```

`status` prints every condition with a stable, content-derived id — e.g.
`gpt-5-mini_minimal_low--<hash>` — and `0/30 done` for each. Condition ids
hash the *content* of the cell (model, resolved sampling params, prompt text),
so two conditions can never silently mix results from different prompt
versions ([Pipeline Concepts](Pipeline-Concepts.md)).

```bash
itemeval estimate compare.yaml --refresh-pricing
```

Multi-condition estimates are itemized per condition — check that no model is
flagged `unpriced`.

## Step 3 — Run it

```bash
itemeval generate compare.yaml    # 4 conditions, run serially, each resumable
itemeval grade    compare.yaml    # free numeric scoring of all 120 solutions
itemeval export   compare.yaml
```

Replications are inspect_ai *epochs*: each item is asked 3 times per
condition, with per-epoch response caching keeping the replications distinct.
If a provider hiccups mid-run, re-run `generate` — completed work skips,
errored samples retry.

You can also run a slice first: `itemeval generate compare.yaml --condition
gpt-5-mini_minimal` runs just that condition (id, id-prefix, or slug).

## Step 4 — Analyze the long-format table

```python
import pandas as pd

df = pd.read_parquet("studies/model_prompt_compare/export/gradings_long.parquet")

# Condition means — the leaderboard view (the least interesting view)
df.groupby(["model", "prompt_name"]).score.mean().unstack()

# Per-item difficulty: which problems separate the models?
df.groupby("item_id").score.mean().sort_values()

# Item x condition accuracy matrix
pivot = df.pivot_table(index="item_id", columns=["model", "prompt_name"],
                       values="score", aggfunc="mean")

# Replication instability: cells where the same model+prompt+item disagrees
# with itself across the 3 replications
flaky = df.groupby(["model", "prompt_name", "item_id"]).score.std()
flaky[flaky > 0]

# What did the comparison cost, per condition?
df.groupby(["model", "prompt_name"]).gen_usd.sum()
```

Because the table is one row per grading event with full design columns
(`model`, `prompt_name`, `replication`, `item_id`, ...), it drops directly
into mixed-effects or IRT tooling — e.g. with
[statsmodels](https://www.statsmodels.org):

```python
import statsmodels.formula.api as smf

# Random intercept per item; fixed effects for model and prompt
m = smf.mixedlm("score ~ model * prompt_name", df,
                groups=df["item_id"]).fit()
print(m.summary())
```

That model answers the actual question: the model effect *with item difficulty
controlled and replication noise in the denominator* — which a leaderboard
number never can.

## Step 5 — Extend the design later

The grid is open-ended in every direction, and resume semantics mean
extensions never re-pay for what's done:

- **Add a model**: append to `solvers.models`; re-run `generate` — only the
  new conditions run.
- **Add a prompt**: write `prompts/solver/cot.md` (must contain `{input}`),
  append its bare name to `facets.prompt`.
- **Vary sampling/reasoning instead**: add cells to `facets.model_config`
  (e.g. `{name: high, reasoning_effort: high}`) — model-config is a facet too.
- **Add a judge dimension**: [Tutorial 4](Tutorial-Second-Judge.md).

## Next

Happy with the design at `dev` scope? Scale it to the full item set safely —
[Tutorial 5 — Scale up without surprises](Tutorial-Budget-and-Scale.md).
