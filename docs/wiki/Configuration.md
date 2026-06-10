# Configuration Reference

One YAML file fully describes a study. Validation is strict
(`extra="forbid"`): unknown or misspelled keys are rejected at load time with
a pydantic error naming the field. Relative paths (`output_dir`,
`prompts_dir`, `rubrics_dir`, `budget.pricing_path`) resolve **relative to
the config file's directory**.

## Complete annotated example

```yaml
study: my_study                # required; ^[a-z0-9][a-z0-9_-]{0,63}$
output_dir: studies            # study dir = <config dir>/<output_dir>/<study>
prompts_dir: prompts           # solver templates at <prompts_dir>/solver/<name>.md
rubrics_dir: rubrics           # rubric templates at <rubrics_dir>/<name>.md
cache: true                    # inspect local response cache, both stages

benchmark:
  adapter: hf                  # only "hf" in v0.1
  datasets:                    # one or more; item ids must be unique across all
    - id: MathArena/usamo_2025
      revision: 0a2c60f2...    # optional commit SHA/tag; omit to pin at first run
      split: train             # default: train
      name: null               # optional HF config name
      limit: null              # optional: first N rows only
  mapping:                     # dataset columns -> Item fields
    input: problem             # required
    id: problem_idx            # optional; default: row index
    target: sample_solution    # optional; default ""
    grading_scheme: grading_scheme   # optional; non-strings stored as canonical JSON
    metadata: [points]         # optional columns copied into Item.metadata

solvers:
  models: [openai/gpt-5-mini, anthropic/claude-haiku-4-5]  # inspect model ids, unique
  temperature: 0.7             # optional, 0..2
  max_tokens: 1024             # optional; unset => uncapped (estimate warns)
  top_p: null                  # optional, (0, 1]
  seed: null                   # optional; recorded; only some providers honor it

facets:
  prompt: [minimal, standard]  # default: [default]
  grader: [judge_a]            # judge grading; [] if using scorer only
  rubric: [standard]           # default: [default]; used only with graders
  scorer: null                 # or: exact_match | multiple_choice | numeric
  replications: 4              # default 1; = inspect epochs
  model_config:                # sampling/reasoning variants as a facet; default one "default" cell
    - name: plain
    - name: thinking
      reasoning_effort: high   # none|minimal|low|medium|high|xhigh|max (OpenAI-style)
      reasoning_tokens: 8192   # Anthropic extended thinking budget
      temperature: 1.0         # per-cell overrides of solvers.* fields

graders:                       # resolves facets.grader names
  judge_a:
    model: openai/gpt-5-mini   # required; judge temperature is pinned to 0.0 in v0.1
    max_tokens: 2048           # default 2048
    reasoning_effort: null

crossing: full                 # only "full" in v0.1

budget:
  policy: dev                  # dev | full-interactive | full-batch (default: dev)
  confirm_above_usd: 5         # gate threshold (default 5.0)
  batch: auto                  # auto | true | false | <max batch size int>
  max_usd: null                # hard cap; exceeding estimate aborts (exit 4), never overridable
  dev_items: 2                 # dev policy: first N items (default 2)
  dev_replications: null       # dev policy: cap replications (default: keep)
  pricing_path: null           # explicit pricing JSON (else user cache, else packaged seed)
```

## Field notes

- **`facets` requires at least one of `grader` / `scorer`.** Both may be set;
  then the grid contains the verifiable condition plus every grader × rubric.
- **Grader resolution**: a `facets.grader` name is looked up in `graders:`;
  a name containing `/` is treated directly as a judge model id with default
  settings. Anything else fails at grid-expansion time with a `ConfigError`.
- **`model_config` name**: stored under the YAML key `model_config` (the
  pydantic field is internally aliased). Each cell's non-null fields override
  the matching `solvers.*` value for that condition; `reasoning_effort` /
  `reasoning_tokens` exist only on cells.
- **Templates** are content-hashed; required placeholders are validated at
  grid expansion. Solver prompts must contain `{input}` (optional `{id}`).
  Rubrics must contain `{input}` and `{solution}` (optional `{target}`,
  `{grading_scheme}`, `{id}`). Rendering replaces only known placeholders —
  LaTeX/JSON braces in templates and item text are safe.
- **`policy: dev`** trims the run to the first `dev_items` items and forces
  batch off — the recommended default until your pipeline looks right.
- **`batch: auto`** enables batch-API mode only under `policy: full-batch`
  (an integer sets the batch size). Batch-capable providers: openai,
  anthropic, google, grok, together.

## Python API

```python
from itemeval import load_config

cfg = load_config("configs/my_study.yaml")   # ConfigError on any problem
cfg.study_dir                                 # resolved output directory
```

The whole pipeline is also drivable programmatically — see
[Python API](Python-API.md).
