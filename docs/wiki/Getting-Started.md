# Getting Started

## Install (development)

```bash
git clone https://github.com/luozm/itemeval && cd itemeval
uv sync                       # creates ./.venv from pyproject.toml + uv.lock
./.venv/bin/python -m pytest  # 158 tests; one downloads a small public HF dataset
```

API keys are read from the environment (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`OPENROUTER_API_KEY`, ...) following inspect_ai's provider conventions. **No
key is needed for the demo below** — it runs on the free `mockllm/*` provider.

## The 5-minute demo (zero paid API calls)

The repo ships `configs/usamo_demo.yaml`: 6 USAMO 2025 problems (public
HuggingFace dataset, revision-pinned) solved by three mock models under two
prompts, two replications each, graded by a mock judge against a rubric.

```bash
./.venv/bin/itemeval status   configs/usamo_demo.yaml   # see the expanded grid (nothing run yet)
./.venv/bin/itemeval estimate configs/usamo_demo.yaml   # projected cost per stage
./.venv/bin/itemeval generate configs/usamo_demo.yaml --yes
./.venv/bin/itemeval grade    configs/usamo_demo.yaml --yes
./.venv/bin/itemeval export   configs/usamo_demo.yaml
./.venv/bin/itemeval status   configs/usamo_demo.yaml   # everything 24/24 complete
```

After this, `studies/usamo_demo/` contains the full output tree — see
[Outputs and Schemas](Outputs-and-Schemas.md). The analysis-ready file is:

```python
import pandas as pd
df = pd.read_parquet("studies/usamo_demo/export/gradings_long.parquet")
# one row per grading event: item x model x prompt x replication x grader x rubric
df[["item_id", "model", "prompt_name", "replication", "score", "reasoning"]]
```

Re-run any command — completed work is skipped (`skipped: complete`), and
inspect_ai's response cache means even `--force` re-runs of identical calls
cost nothing.

## Adapting it to your study

1. **Copy a config**: start from `configs/usamo_demo.yaml`. Point
   `benchmark.datasets` at your HuggingFace dataset and adjust
   `benchmark.mapping` to its column names ([Configuration](Configuration.md)).
2. **Write prompts**: one Markdown file per prompt variant in
   `prompts/solver/<name>.md`, containing an `{input}` placeholder.
3. **Pick grading**: either `facets.scorer: exact_match | multiple_choice |
   numeric` (free, no LLM) or judge grading — rubric files in
   `rubrics/<name>.md` with `{input}` and `{solution}` placeholders, plus a
   `graders:` section naming the judge models.
4. **Swap in real models**: replace `mockllm/...` with real inspect model ids
   (`openai/gpt-5-mini`, `anthropic/claude-haiku-4-5`,
   `openrouter/deepseek/deepseek-v3.2`, ...).
5. **Keep `budget.policy: dev`** until the pipeline looks right — dev runs
   only the first 2 items. Then switch to `full-interactive` or `full-batch`.

Always run `estimate` before the first paid run, and refresh pricing first:

```bash
./.venv/bin/itemeval estimate configs/my_study.yaml --refresh-pricing
```
