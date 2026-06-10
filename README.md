# itemeval

**Item-level LLM evaluation over any API, with built-in budget control.**

Most eval harnesses give you a leaderboard number. itemeval gives you the
**data behind the number**: you declare a benchmark and an experiment design
(models × prompts × graders × rubrics × replications) in one YAML file, and it
returns a long-format table with one row per grading event — every score,
judge rationale, token count, and dollar — plus full raw logs. Built as a thin,
design-driven layer on [inspect_ai](https://inspect.aisi.org.uk), it is made
for item-response-level analysis (psychometrics, IRT, mixed-model, judge
reliability), never just aggregate scores.

**Status: v0.1.0 — first public release.**
See [ROADMAP.md](https://github.com/luozm/itemeval/blob/main/ROADMAP.md).

## Who is this for

- **Measurement / evaluation researchers** — you treat an LLM benchmark as a
  measurement instrument and need every individual grading event as a row:
  item × model × prompt × replication × grader × rubric, ready for IRT,
  mixed-effects, or reliability analysis.
- **LLM-as-judge methodologists** — you study how much the judge model and the
  rubric matter. Solutions are generated once and stored; adding a new judge or
  rubric re-grades them at **zero generation cost**.
- **Benchmark builders and auditors** — you need per-item difficulty,
  prompt sensitivity, and parse-failure visibility to find broken items, not a
  single accuracy number.
- **Practitioners comparing models for a task** — you want a model × prompt
  comparison on your benchmark with per-item failure analysis, a dry-run cost
  estimate before anything runs, and a hard dollar cap while it runs.
- **Agent / CI pipelines** — declarative config, idempotent resumable commands,
  deterministic exit codes, `--json` output, and a budget gate that can never be
  talked past make itemeval safe for an AI agent to drive — see the
  [Agent Guide](https://github.com/luozm/itemeval/wiki/Agent-Guide).

## Quickstart

Run a real evaluation end-to-end on a public, verifiable benchmark — no judge
model, so grading is free and the only cost is a few cents of generation. This
scores [AIME 2025](https://huggingface.co/datasets/MathArena/aime_2025)
(integer answers → the built-in `numeric` scorer) with `openai/gpt-5-mini`.

```bash
pip install itemeval[openai]
export OPENAI_API_KEY=sk-...
```

Save this as `aime.yaml`:

```yaml
study: aime_quickstart
benchmark:
  adapter: hf
  datasets:
    - id: MathArena/aime_2025    # dataset revision auto-pins at first run
      split: train
  mapping: {id: problem_idx, input: problem, target: answer}
solvers:
  models: [openai/gpt-5-mini]
  max_tokens: 8192               # cover hidden reasoning + the visible "ANSWER:" line
facets:
  prompt: [builtin:minimal]      # packaged template: ends on a line starting "ANSWER:"
  scorer: numeric                # verifiable, $0 — no grader or rubric needed
  model_config: [{name: low, reasoning_effort: low}]
budget:
  policy: dev                    # first few problems only; raise dev_items or change policy to scale up
  dev_items: 5
```

Then walk the pipeline — estimate first, generate, grade, export:

```bash
itemeval estimate aime.yaml   # projected $ per stage, no model calls
itemeval generate aime.yaml   # stage 1 → solutions store (resumable)
itemeval grade    aime.yaml   # stage 2 → numeric scores (free, no LLM)
itemeval export   aime.yaml   # long-format parquet + CSV + cost ledger
```

`export` writes `studies/aime_quickstart/export/gradings_long.{parquet,csv}` —
one row per problem with its score (`1.0`/`0.0`), the answer the scorer
extracted, the full solution text, token counts, and dollar cost. Stages are
cached and resumable, so re-runs never re-pay for completed work. Swap in
`scorer: multiple_choice` (letter answers) or `exact_match`, or declare a
`grader` + `rubric` for LLM-judged benchmarks — see the tutorials below.

## Documentation

The **[wiki](https://github.com/luozm/itemeval/wiki)** is the user guide.
Start with the step-by-step tutorials — each is a complete, runnable use case:

| Tutorial | What you learn |
|---|---|
| [1 — Score a verifiable benchmark](https://github.com/luozm/itemeval/wiki/Tutorial-Verifiable-Benchmark) | The full pipeline on AIME 2025 for ~2¢: estimate → generate → grade → export |
| [2 — Grade with an LLM judge](https://github.com/luozm/itemeval/wiki/Tutorial-LLM-Judge) | Rubric-based judging of open-ended answers, the judge output contract, parse failures |
| [3 — Compare models and prompts](https://github.com/luozm/itemeval/wiki/Tutorial-Experiment-Design) | A crossed design with replications, and analyzing the long-format export in pandas |
| [4 — Add a second judge at $0 generation](https://github.com/luozm/itemeval/wiki/Tutorial-Second-Judge) | Fan new graders/rubrics out over stored solutions; judge-agreement analysis |
| [5 — Scale up without surprises](https://github.com/luozm/itemeval/wiki/Tutorial-Budget-and-Scale) | dev → full-batch, the cost gate, hard caps, batch APIs, resume, the savings report |

Reference pages: [Getting Started](https://github.com/luozm/itemeval/wiki/Getting-Started) ·
[Pipeline Concepts](https://github.com/luozm/itemeval/wiki/Pipeline-Concepts) ·
[Configuration](https://github.com/luozm/itemeval/wiki/Configuration) ·
[CLI](https://github.com/luozm/itemeval/wiki/CLI) ·
[Python API](https://github.com/luozm/itemeval/wiki/Python-API) ·
[Outputs and Schemas](https://github.com/luozm/itemeval/wiki/Outputs-and-Schemas) ·
[Budget and Costs](https://github.com/luozm/itemeval/wiki/Budget-and-Costs) ·
[Error Handling](https://github.com/luozm/itemeval/wiki/Error-Handling) ·
[FAQ](https://github.com/luozm/itemeval/wiki/FAQ).

**Driving itemeval from an AI agent** (Claude Code, Codex, custom agents):
point the agent at the [Agent Guide](https://github.com/luozm/itemeval/wiki/Agent-Guide) —
a compact, contract-style page covering commands, exit codes, machine-readable
outputs, and budget guardrails.

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
5. **Budget layer** — dry-run cost estimation before launch, per-sample cost
   attribution plus a per-run cost ledger after, hard token caps, and run
   policies (`dev` / `full-batch` / `full-interactive`).

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
  on_empty: skip                # empty (no-error) completions: skip | rerun | grade
facets:
  prompt: [builtin:minimal, builtin:standard]   # packaged templates; bare name -> prompts/solver/*.md
  grader: [judge_a, judge_b]    # or scorer: exact_match for verifiable benchmarks
  rubric: [builtin:standard]    # packaged; bare name -> rubrics/*.md (judge only; default: [builtin:standard])
  replications: 4
graders:                        # resolves facet names; bare model ids also work
  judge_a: {model: openai/gpt-5-mini}
  judge_b: {model: anthropic/claude-haiku-4-5}
crossing: full
budget:
  policy: dev                   # small subset preset for pipeline validation
  confirm_above_usd: 5
  batch: auto                   # batch API when policy is full-batch
```

## CLI

```
itemeval init     my_study                   # scaffold config.yaml (--with-templates also copies prompts/rubrics)
itemeval estimate configs/my_study.yaml      # projected $ per stage, no model API calls
itemeval generate configs/my_study.yaml      # stage 1 (resumable)
itemeval grade    configs/my_study.yaml      # stage 2 (resumable, re-runnable per rubric/grader)
itemeval export   configs/my_study.yaml      # long-format parquet + CSV + cost ledger
itemeval status   configs/my_study.yaml      # grid completion matrix
```

`generate`/`grade` show inspect's `rich` live progress by default; pass
`--display none` (or set `INSPECT_DISPLAY=none`) to silence it, or
`--display plain|rich|full` to pick a mode. The display degrades automatically
off a TTY (CI, pipes).

## Python API

The same pipeline, programmatically (one public function per CLI command):

```python
import itemeval

cfg  = itemeval.load_config("configs/my_study.yaml")
prep = itemeval.prepare_study(cfg)           # datasets + templates + grid + plan + pricing

est = itemeval.estimate_study(prep)          # projected $ per stage, no model API calls
itemeval.run_generate(prep)                  # stage 1 -> solutions store
itemeval.run_grade(prep)                     # stage 2 -> gradings store
itemeval.export_study(cfg)                   # long-format parquet + CSV + ledger
itemeval.build_status(cfg, prep)             # grid completion report
```

Every call returns a pydantic result object. One difference from the CLI:
the budget **confirmation gate is a CLI feature** — programmatic callers
should check `estimate_study(...)` totals against their own threshold before
paid runs. Like the CLI, `run_generate`/`run_grade` show inspect's `rich` live
progress by default; pass `display="none"` (or set `INSPECT_DISPLAY=none`) to
silence it. Anything not exported from `itemeval` (the `_`-prefixed modules) is
internal with no stability promise.

## Cost controls

- `estimate` before every run; runs projected above `confirm_above_usd` require
  explicit confirmation.
- inspect local response cache: re-runs never re-pay for completed samples.
- Batch APIs (OpenAI/Anthropic/Google/xAI/Together) at ~50% for non-interactive runs.
- Prompt caching exploited in the grading stage (rubric + problem prefix repeats
  across solutions).
- `max_tokens` caps on both stages; `dev` policy as the default for new configs.
- Cost ledger appended per run: tokens and USD per sample (and aggregated per
  condition), attributed to generation vs grading. Exports check that ledger
  totals match row sums; reconciliation against provider dashboards is a
  documented manual step.
- `export` reports per-provider spend and the savings the package delivered
  versus a plain-API list price (no caching, no batch discount), split into
  prompt-cache and batch-discount components. Resume / response-cache reuse is
  not counted — cache hits carry no token usage — so it reflects the prompt-cache
  and batch discounts only.
- Pricing stays current automatically: set `budget.pricing_max_age_days` to
  refresh the cached OpenRouter pricing table once it ages past the threshold
  (best-effort; a failed refresh keeps the existing table).
- Every cost-bearing command prints its pricing provenance (`source`, age, and
  whether a refresh just ran), so the dollar figures are never anonymous; the
  same is on `Estimate.pricing` / `ExportResult.pricing` for the Python API.

## Reproducibility

Every run writes a manifest: dataset ids + revision hashes, prompt/rubric
template content hashes, model ids with provider + version, temperature and all
sampling params (effective values, including provider-forced ones), seeds where
supported, package versions, and condition grid. Same manifest + cache ⇒
identical results; raw logs allow full re-derivation of every number.

## Install

```bash
pip install itemeval[openai]   # provider extra; also: [anthropic], [google], [all]
                               # bare `pip install itemeval` omits provider SDKs
itemeval init my_study      # scaffold a runnable study (config.yaml only; templates resolve from the package)
cd my_study && itemeval status config.yaml
```

Provider SDKs are optional extras (mirroring inspect_ai's lazy imports): install
the extra for the provider(s) you call. The `openai` extra also covers
OpenRouter and other OpenAI-compatible providers. A bare `pip install itemeval`
runs the free `mockllm/*` path and all no-API commands (`status`, `estimate`);
calling a real provider without its extra raises a clear install hint.

`init` writes just `config.yaml`; its `builtin:` prompt/rubric references resolve
from templates packaged inside itemeval, so the study runs with no local files.
Add `--with-templates` to also copy those templates locally as editable starters.
Outputs land under the current working directory (`./studies/<study>/`).

API keys are read from the environment (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`OPENROUTER_API_KEY`, ...) following inspect_ai's provider conventions.

### From source (development)

```bash
git clone https://github.com/luozm/itemeval && cd itemeval
uv sync                              # creates ./.venv from pyproject.toml + uv.lock
./.venv/bin/python -m pytest
```

## License

MIT
