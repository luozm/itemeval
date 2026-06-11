# Configuration Reference

One YAML file fully describes a study. Validation is strict
(`extra="forbid"`): unknown or misspelled keys are rejected at load time with
a pydantic error naming the field.

Paths resolve by **intent**: **input** paths (`prompts_dir`, `rubrics_dir`,
`budget.pricing_path`) resolve relative to the **config file's directory**, so a
config always finds its own templates; the **output** path (`output_dir`)
resolves relative to the **working directory** — the current directory by
default, override with `-C/--base-dir` (CLI) or `work_dir=` (`load_config`).
Outputs never land next to the config or inside the installed package. Absolute
paths are used as-is.

## Complete annotated example

```yaml
study: my_study                # required; ^[a-z0-9][a-z0-9_-]{0,63}$
output_dir: studies            # output study dir = <work_dir (CWD)>/<output_dir>/<study>
prompts_dir: prompts           # local solver templates at <config dir>/<prompts_dir>/solver/<name>.md
rubrics_dir: rubrics           # local rubric templates at <config dir>/<rubrics_dir>/<name>.md
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
  on_empty: skip               # empty (no-error) solutions: skip | rerun | grade (default skip)
  cache_prompt: auto           # provider prompt caching for generation: auto|on|off (auto = on when replications > 1)
  split_prompt: false          # render prompt as system(template head)+user(item) for provider cache breakpoints; changes condition ids

facets:
  prompt: [builtin:standard]   # builtin:NAME = packaged; bare NAME = local file. default: [builtin:standard]
  grader: [judge_a]            # judge grading; [] if using scorer only
  rubric: [builtin:standard]   # same rule; used only with graders. default: [builtin:standard]
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
    split_rubric: false        # rubric head as system msg + solution as user msg (provider cache breakpoint); changes condition ids

crossing: full                 # only "full" in v0.1

budget:
  policy: dev                  # dev | full-interactive | full-batch (default: dev)
  confirm_above_usd: 5         # gate threshold (default 5.0)
  batch: auto                  # auto | true | false | <max batch size int>
  max_usd: null                # hard cap; exceeding estimate aborts (exit 4), never overridable
  dev_items: 2                 # dev policy: first N items (default 2)
  dev_replications: null       # dev policy: cap replications (default: keep)
  pricing_path: null           # explicit pricing JSON (else user cache, else packaged seed)
  cache_schedule: auto         # warm-then-fan-out scheduling of same-prefix calls: auto | off
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
- **`solvers.on_empty`**: what to do with a *completed* generation that produced
  no gradable text (empty/blank `solution`, no API error — typically a reasoning
  model whose `max_tokens` was spent entirely on hidden reasoning). This is a
  distinct channel from API errors (always re-attempted) and parse failures
  (always final). `skip` (default) excludes them from grading; `rerun` also
  treats them as not-done so a later `generate` re-attempts them (raise
  `max_tokens` / lower `reasoning_effort` first — an identical request hits the
  response cache and stays empty); `grade` sends the empty answer to the judge
  as-is (usually scored low). Either way they are surfaced, never silently
  counted as complete: `grade` prints the count + stop-reason breakdown and
  `status` shows an `empty` column. The usual cause is too small a `max_tokens`
  for a reasoning model — give the cap room for reasoning **plus** the answer.
- **Templates: built-in vs local.** A `prompt`/`rubric` entry references a
  template in one of two namespaces, never mixed or silently shadowed:
  `builtin:NAME` resolves to a template packaged inside itemeval (run
  `itemeval init --with-templates` to see/copy them); a **bare** `NAME` resolves
  to a local file under `prompts_dir`/`rubrics_dir`. A local `standard` and
  `builtin:standard` are distinct references — each is content-hashed and
  recorded with its `source` (`local`/`builtin`) in the run manifest, so two
  same-named templates with different content never collide. A bare name with no
  local file errors at load (before any output is written) and, if a built-in of
  that name exists, suggests the `builtin:` form. Built-in templates ship today:
  prompts `minimal`, `standard`; rubric `standard`.
- **Templates** are content-hashed; required placeholders are validated before
  any run. Solver prompts must contain `{input}` (optional `{id}`). Rubrics must
  contain `{input}` and `{solution}` (optional `{target}`, `{grading_scheme}`,
  `{id}`). Rendering replaces only known placeholders — LaTeX/JSON braces in
  templates and item text are safe.
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
