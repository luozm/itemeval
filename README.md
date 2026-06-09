# itemeval

**Item-level LLM evaluation over any API, with built-in budget control.**

A thin, design-driven evaluation package built on [inspect_ai](https://inspect.aisi.org.uk).
Define a benchmark source and a facet grid in YAML; itemeval expands the grid, runs
generation and grading as two decoupled stages, and exports a long-format
item-response table plus full raw logs — built for item-response-level analysis
(psychometrics, G-theory, IRT), never just aggregate scores.

**Status: pre-alpha skeleton — interfaces under construction.** See [ROADMAP.md](ROADMAP.md).

## Why this exists

inspect_ai already provides the hard parts: async execution with per-provider
rate limiting and retry, ~20 model providers (official OpenAI / Anthropic /
Google APIs plus OpenRouter for the long tail), batch-API support (50% cost),
prompt caching, local response caching, and complete `.eval` logs with dataframe
extraction. itemeval adds the layer it doesn't have:

1. **Benchmark adapters** — load any HuggingFace dataset (pinned revision) or
   GitHub repo (pinned commit) into canonical items via a small field-mapping spec.
2. **Experiment design grids** — declare facets (prompt variants, graders,
   rubrics, replications, model configs) and crossing structure; itemeval expands
   the grid into runs with stable condition ids.
3. **Two-stage generate/grade pipeline** — solutions are generated once and
   stored; grading facets (grader × rubric) fan out over stored solutions without
   multiplying generation cost. Both stages are resumable and cached.
4. **Item-response export** — long-format parquet/CSV with one row per grading
   event (item × model × prompt × replication × grader × rubric × ...), including
   scores, judge reasoning, token usage, and dollar costs. Never aggregated.
5. **Budget layer** — dry-run cost estimation before launch, per-call cost
   ledger after, hard token caps, and run policies (`dev` / `full-batch` /
   `full-interactive`).

## Pipeline

```
benchmark source ─▶ adapter ─▶ items ─┐
                                      ├─▶ GENERATE ─▶ solutions store ─▶ GRADE ─▶ gradings table
design.yaml ─▶ facet grid expansion ──┘   (inspect)    (parquet+logs)   (inspect)  (long-format)
```

- **Generate**: one inspect task per (solver model × prompt × model-config)
  cell; `epochs` = replications. Every solution stored with full provenance.
- **Grade**: two scorer families behind one interface:
  - *verifiable* — exact match / multiple choice / numeric; no LLM cost.
  - *judge* — grading runs as its own inspect task (dataset = stored solutions,
    solver = grader model + rubric template). Judge calls get their own logs,
    retries, caching, batch eligibility, and cost accounting, and emit a
    structured numeric score + reasoning. Parse failures are flagged, never
    silently dropped.

## Package layout

```
src/itemeval/
  adapters/      # hf, github, local  → canonical Item
  design/        # facet declaration, grid expansion, condition ids
  generate/      # inspect task builders for the generation stage
  grade/         # verifiable scorers + judge-as-task builders
  store/         # solutions/gradings parquet stores; raw .eval logs index
  budget/        # estimator, pricing table, ledger, policies
  cli.py         # estimate | generate | grade | export | status
```

## Experiment config (sketch)

```yaml
study: my_study
benchmark:
  adapter: hf
  datasets:
    - id: SomeOrg/some_benchmark   # revision pinned at first run
  mapping: {input: question, target: answer}
solvers:
  models: [openai/gpt-5-mini, anthropic/claude-haiku-4-5, openrouter/deepseek/deepseek-v3.2]
  temperature: 0.7              # recorded; provider-forced values recorded as-is
facets:
  prompt: [minimal, standard]   # prompts/solver/*.md
  grader: [judge_a, judge_b]    # or scorer: exact_match for verifiable benchmarks
  replications: 4
crossing: full
budget:
  policy: dev                   # small subset preset for pipeline validation
  confirm_above_usd: 5
  batch: auto                   # batch API when policy is full-batch
```

## CLI

```
itemeval estimate configs/my_study.yaml      # projected $ per stage, no API calls
itemeval generate configs/my_study.yaml      # stage 1 (resumable)
itemeval grade    configs/my_study.yaml      # stage 2 (resumable, re-runnable per rubric/grader)
itemeval export   configs/my_study.yaml      # long-format parquet + CSV + cost ledger
itemeval status   configs/my_study.yaml      # grid completion matrix
```

## Cost controls

- `estimate` before every run; runs projected above `confirm_above_usd` require
  explicit confirmation.
- inspect local response cache: re-runs never re-pay for completed samples.
- Batch APIs (OpenAI/Anthropic/Google/xAI/Together) at ~50% for non-interactive runs.
- Prompt caching exploited in the grading stage (rubric + problem prefix repeats
  across solutions).
- `max_tokens` caps on both stages; `dev` policy as the default for new configs.
- Cost ledger appended per run: tokens and USD per call, attributed to
  generation vs grading.

## Reproducibility

Every run writes a manifest: dataset ids + revision hashes, prompt/rubric
template content hashes, model ids with provider + version, temperature and all
sampling params (effective values, including provider-forced ones), seeds where
supported, package versions, and condition grid. Same manifest + cache ⇒
identical results; raw logs allow full re-derivation of every number.

## Install (development)

```bash
git clone https://github.com/luozm/itemeval && cd itemeval
uv sync                              # creates ./.venv from pyproject.toml + uv.lock
./.venv/bin/python -m pytest
```

API keys are read from the environment (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`OPENROUTER_API_KEY`, ...) following inspect_ai's provider conventions.

## License

MIT
