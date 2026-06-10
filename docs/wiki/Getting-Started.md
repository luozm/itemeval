# Getting Started

## Install

```bash
pip install itemeval        # or: uv add itemeval (in a project) / uv tool install itemeval (as a CLI)
```

Scaffold a runnable study and drive it — runs free on the mock provider, no key:

```bash
itemeval init my_study      # writes my_study/config.yaml (templates resolve from the package)
cd my_study
itemeval status   config.yaml
itemeval estimate config.yaml
itemeval generate config.yaml --yes
itemeval grade    config.yaml --yes
itemeval export   config.yaml
```

`init` writes only `config.yaml`; its `builtin:` prompt/rubric references resolve
from templates packaged inside itemeval, so nothing else is needed to run. Pass
`--with-templates` to also copy those templates into `my_study/prompts/` and
`my_study/rubrics/` as editable starters (the config's references are rewritten
to point at the local copies). Outputs land under the current directory:
`my_study/studies/my_study/`.

API keys are read from the environment (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`OPENROUTER_API_KEY`, ...) following inspect_ai's provider conventions.

## Install from source (development)

```bash
git clone https://github.com/luozm/itemeval && cd itemeval
uv sync                       # creates ./.venv from pyproject.toml + uv.lock
./.venv/bin/python -m pytest  # one test downloads a small public HF dataset
```

The repo ships example configs under `configs/`; the 5-minute demo below uses one.
**No key is needed** — it runs on the free `mockllm/*` provider.

## The 5-minute demo (zero paid API calls)

`configs/usamo_demo.yaml`: 6 USAMO 2025 problems (public HuggingFace dataset,
revision-pinned) solved by three mock models under two `builtin:` prompts, two
replications each, graded by a mock judge against the `builtin:standard` rubric.

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

1. **Scaffold a study**: `itemeval init my_study` (installed), or copy
   `configs/usamo_demo.yaml` (from a clone). Point `benchmark.datasets` at your
   HuggingFace dataset and adjust `benchmark.mapping` to its column names
   ([Configuration](Configuration.md)).
2. **Choose prompts**: keep the packaged `builtin:minimal` / `builtin:standard`,
   or write your own — one Markdown file per variant in `prompts/solver/<name>.md`
   containing an `{input}` placeholder, referenced by its **bare name**. (Run
   `itemeval init --with-templates` to start from editable copies of the built-ins.)
3. **Pick grading**: either `facets.scorer: exact_match | multiple_choice |
   numeric` (free, no LLM) or judge grading — keep `builtin:standard` or add
   rubric files in `rubrics/<name>.md` with `{input}` and `{solution}`
   placeholders, plus a `graders:` section naming the judge models.
4. **Swap in real models**: replace `mockllm/...` with real inspect model ids
   (`openai/gpt-5-mini`, `anthropic/claude-haiku-4-5`,
   `openrouter/deepseek/deepseek-v3.2`, ...).
5. **Keep `budget.policy: dev`** until the pipeline looks right — dev runs
   only the first 2 items. Then switch to `full-interactive` or `full-batch`.

Always run `estimate` before the first paid run, and refresh pricing first:

```bash
itemeval estimate my_study/config.yaml --refresh-pricing
```
