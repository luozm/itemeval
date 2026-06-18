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
    id: problem_idx            # optional; default: row index. column | [segments] | "{template}" (see Composite item ids)
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
  provider_routing: null       # optional OpenRouter routing object, sent verbatim with every openrouter/* request (pins the upstream); inert+warns if no openrouter/* model

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
    provider_routing: null     # optional; same OpenRouter routing object as solvers.provider_routing, for this judge's calls

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
  prefer_native_batch: false   # under a batch plan, route openrouter/* sampled models to their native batch API for the ~50% discount (opt-in; can change outputs) — see Cost Savings
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
- **`solvers.sample`** (mutually exclusive with `solvers.models`): draw the
  model facet from a universe instead of listing it — useful when candidates
  come from a large roster.

  ```yaml
  solvers:
    sample:
      n: 20
      seed: 7
      stratify_by: provider          # optional; see the dimensions below
      allocation: equal              # optional; equal-per-stratum (default: proportional)
      include: [openrouter/openai/gpt-5.1]   # optional; always present, counted against n
      exclude: [openrouter/openai/gpt-5-judge]  # optional; dropped from any universe before drawing
      universe: pricing-table        # | a file path | an inline list of ids
      where:                         # pricing-table only
        provider: [anthropic, openai, google]
        max_output_usd_per_mtok: 15
        min_context_length: 131072
        released_after: "2025-01-01" # keep only models released on/after this date
        reasoning: true              # keep only reasoning models
        multimodal: false            # keep only text-only models
  ```

  `n` models are selected with `seed` (deterministic given the seed and the
  sorted universe). `universe` is one of: `pricing-table` — the `openrouter/*`
  roster from the pricing table (run `itemeval estimate … --refresh-pricing`
  first to sample today's roster); a **file** path of ids (one per line, `#`
  comments allowed), resolved relative to the config file; or an **inline list**.

  **`stratify_by`** balances the draw across one dimension: `provider` (the
  model-id org — works for any universe), or — for a `pricing-table` universe
  only — `reasoning`, `multimodal`, `price_tier`, `context_tier`, or `recency`.
  Tier edges are fixed: **price** (output $/Mtok) `free` / `low` ≤ $1 / `mid` ≤
  $10 / `high`; **context** `short` ≤ 32k / `medium` ≤ 128k / `long` ≤ 400k /
  `xlong`; **recency** buckets by the model's **release year** (UTC), a pure
  function of the roster's `created` date so a pinned table tiers identically.

  **`allocation`** (`proportional`, the default, or `equal`; requires
  `stratify_by`) decides how `n` is split across strata. `proportional`
  allocates by stratum size — large-roster vendors get more slots — while
  `equal` gives every stratum the same share (capped at the models it has, with
  the overflow redistributed). Use `equal` for balanced coverage so a big vendor
  can't dominate and a small one can't drop to zero.

  **`include`** pins must-have model ids that are **always present and counted
  against `n`**; the seeded draw fills the remaining `n − len(include)` slots.
  Pinned ids bypass `where` and need not be in the universe (they are purposive
  picks). When you also stratify, pins **count toward** their stratum's balanced
  share rather than stacking on top of it — pinning two OpenAI models inside an
  equal-by-provider draw means OpenAI's share is met by the pins, not doubled;
  if you pin more than a stratum's share, all pins are kept and the rest
  rebalances.

  **`exclude`** is the inverse of `include`: a list of exact model-ids dropped
  from the universe before the draw (e.g. the judge model-ids, so a judge can't
  be drawn as a solver). Unlike `where`, it is **not** roster-only — it works for
  `pricing-table`, file, and inline-list universes alike ("this list, minus these
  three"). Ids absent from the universe are a no-op, and an id cannot be both
  `include`d and `exclude`d (rejected at load). The blocked ids are recorded in
  `model_locks.json` and `STUDY_CARD.md`, so the card attests them.

  **`where`** (pricing-table only — list/file universes are already curated)
  narrows the roster before the draw: a `provider` allowlist, a
  `max_output_usd_per_mtok` ceiling, a `min_context_length` floor, a
  `released_after` **absolute** `YYYY-MM-DD` release cutoff (uses `created`;
  drops undated models; never wall-clock age, so a pinned table draws
  identically), and `reasoning` / `multimodal` booleans.

  The draw is **pinned** in `model_locks.json` beside the study: later runs reuse
  the same models, a roster that has since changed only prints a warning (the
  pinned draw stands), and changing `n`/`seed`/`stratify_by`/`allocation`/
  `include`/`where` fails loudly — delete `model_locks.json` to re-draw (existing
  solutions for dropped models remain). The drawn set, universe size, and seed
  are recorded in the run manifest and `STUDY_CARD.md` and printed as a
  `models: sampled N of M …` line.

  **Evaluating the current SOTA frontier?** The honest way to get one flagship
  per vendor is to **name them with `include`** — "latest by release date" is not
  a flagship proxy (vendors ship cheap `…-mini`/`…-nano`/preview variants *after*
  the flagship), and no roster field reliably marks the flagship, so itemeval
  does not guess one. Pin the models you mean:

  ```yaml
  solvers:
    sample:
      n: 5
      seed: 7
      universe: pricing-table
      include:                       # the flagships you consider SOTA
        - openrouter/anthropic/claude-opus-4.8
        - openrouter/openai/gpt-5.1
        - openrouter/google/gemini-3-pro
        - openrouter/x-ai/grok-5
        - openrouter/deepseek/deepseek-v4
  ```

  The `pricing-table` universe is restricted to OpenRouter's **runnable text
  models** — those that take text and emit text and expose generation parameters
  — so embedding and meta/router entries are never sampled. It also **excludes
  free (`$0` output) models**: they are rate-limited `:free` endpoints, not
  representative of the paid models a measurement frame samples (so a
  `pricing-table` draw never yields a `free` price tier). They stay in the
  pricing table — name one directly in `solvers.models` if you want it and its
  price still resolves — they are simply not drawn. The roster metadata
  that powers the universe filter, `where`, and the metadata `stratify_by`
  dimensions (`text_model`, `reasoning`, `multimodal`, `context_length`, and the
  `created` release date behind `released_after` / `recency`) is captured by
  `--refresh-pricing`. When the cached table predates this metadata, `prepare`
  refreshes once automatically before a `pricing-table` draw (announced on the
  pricing provenance line); offline — or for the `recency` dimension, whose
  release dates aren't auto-recovered — the empty-universe and recency errors
  point you at a manual `--refresh-pricing`.
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
- **`provider_routing`** (on `solvers:` and per grader): a verbatim OpenRouter
  provider-routing object (e.g. `{order: [anthropic], allow_fallbacks: false}`)
  attached to every `openrouter/*` request, pinning which upstream answers — so
  a cached run can't silently land on a host that ignores cache markers
  (Bedrock/Vertex). Passed through unchanged and never part of a condition id;
  setting it in a section with no `openrouter/*` model warns (inert, never
  blocks). Why it matters and how to confirm the pin held:
  [Cost Savings](Cost-Savings.md#openrouter-or-direct).

## Two-stage (materialized) rubrics

By default a rubric is a single static template rendered once per grading call.
Some published protocols (ProofBench, RefGrader) are **two-stage**: a per-item
rubric is *generated from the reference solution and frozen*, then every
candidate is graded against that frozen rubric. Declare one with a top-level
`rubrics:` mapping (parallel to `graders:`):

```yaml
# sketch
rubrics:
  checkpoint:
    materialize:
      model: openrouter/openai/gpt-5.4   # the materializer (often a strong model)
      template: checkpoint.build         # build prompt over {input,target,grading_scheme,id}
      max_tokens: 2048                   # a marking scheme is long (default 2048)
    grade_template: checkpoint.grade     # judge prompt; receives {rubric} + {solution}
facets:
  grader: [judge]
  rubric: [checkpoint, builtin:standard] # materialized + plain levels crossed as usual
```

A `facets.rubric` name found in `rubrics:` materializes; any other name stays a
plain template reference (bare or `builtin:`), **byte-identical to today**. How
it runs:

- **Stage 1 (materialize), folded into `grade`.** Before judging, the
  materializer renders the **build template** over the item's reference only —
  `{input}`, `{target}` (the reference solution), `{grading_scheme}`, `{id}`;
  it **must not** reference `{solution}` (none exists yet). The result is frozen
  in `materialized_rubrics.parquet`, content-addressed by the build template +
  materializer model, and **reused across every grader, solution, replication,
  and resumed run** (reuse is $0). Run once per item — cheap relative to grading.
- **Stage 2 (grade).** The **grade template** must contain `{rubric}` (filled
  with the frozen text) and `{solution}`. With `split_rubric`, the rubric sits in
  the cached shared head (it's solution-independent).
- **Cost & consent.** The materializer calls are in the `estimate` and ride the
  **single existing money gate** — there is no separate command or prompt. The
  `grade` summary prints `materialized: N rubrics (model) · $X · M reused`.
- **Identity.** The grade **condition id** includes the materializer model and
  build-template hash, so changing either re-derives the rubric (like editing a
  rubric). The per-item rubric text is recorded in the store (and copied into
  `export --snapshot`) as the reproducibility record. Materialization is a
  **design declaration** — always explicit, never auto-enabled.

The build/grade prompts are yours to author (study content); itemeval ships no
built-in materialize template. If a materialization returns no text, the
`empty-materialized-rubrics` hint fires and that item is graded against a blank
rubric (see [Error Handling](Error-Handling.md#empty-materialized-rubrics)).

## Composite item ids

Item ids must be **unique across all configured datasets** — they are the join
key into the solutions/gradings stores and the export table. When you pool
datasets that share a natural key (a per-split row index, a per-release problem
number repeated each year), `mapping.id` can compose a unique id instead of a
single column. Three forms:

| `mapping.id` | Result for `org/set_2026`, `problem_idx = 6` | Use when |
|---|---|---|
| `problem_idx` | `6` | a single globally-unique column (default) |
| `[colA, colB]` | `<colA>:<colB>` | a multi-column natural key |
| `["{dataset}", problem_idx]` | `set_2026:6` | namespacing a repeated key per dataset |
| `"{dataset}:{problem_idx}"` | `set_2026:6` | the same, written as one template string |

A segment containing `{` is a **template**: `{dataset}` becomes the dataset
**basename** (the part after `/`), and any other `{name}` becomes that record
column. Plain segments are column names. Segments join with `:`. A single plain
column is unchanged — existing studies' ids never move. An unknown
`{placeholder}`, a missing column, or a malformed segment (an unbalanced brace)
fails the load with a message naming the valid options.

## Python API

```python
from itemeval import load_config

cfg = load_config("configs/my_study.yaml")   # ConfigError on any problem
cfg.study_dir                                 # resolved output directory
```

The whole pipeline is also drivable programmatically — see
[Python API](Python-API.md).
