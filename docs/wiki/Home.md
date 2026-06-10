# itemeval Wiki

**Item-level LLM evaluation over any API, with built-in budget control** — a
thin layer on [inspect_ai](https://inspect.aisi.org.uk) for studies that need
every individual grading event (psychometrics, G-theory, IRT), never just
aggregate scores.

```
benchmark source ─▶ adapter ─▶ items ─┐
                                      ├─▶ GENERATE ─▶ solutions store ─▶ GRADE ─▶ gradings table
design.yaml ─▶ facet grid expansion ──┘   (inspect)    (parquet+logs)   (inspect)  (long-format)
```

You describe **what** to evaluate (a benchmark + a facet grid) in one YAML
file; itemeval expands the grid into conditions, runs generation and grading
as two decoupled, resumable stages on inspect_ai, and exports one long-format
row per grading event with scores, judge reasoning, tokens, and dollars.

## Pages

| Page | What it covers |
|------|----------------|
| [Getting Started](Getting-Started.md) | Install, run the free demo pipeline in 5 minutes |
| [Pipeline Concepts](Pipeline-Concepts.md) | Items, facets, conditions, replications, two-stage design, resume & caching |
| [Configuration](Configuration.md) | Complete YAML reference for every config field |
| [CLI](CLI.md) | The five commands, options, and exit codes |
| [Outputs and Schemas](Outputs-and-Schemas.md) | Study directory layout, parquet stores, export table, manifests |
| [Budget and Costs](Budget-and-Costs.md) | Estimation, confirmation gate, policies, pricing, batch mode |
| [Architecture](Architecture.md) | Module map: what each file does and why it exists |
| [FAQ](FAQ.md) | Common errors, troubleshooting, design rationale |

## The five commands

```
itemeval estimate configs/my_study.yaml   # projected $ per stage, no model API calls
itemeval generate configs/my_study.yaml   # stage 1: solutions (resumable)
itemeval grade    configs/my_study.yaml   # stage 2: gradings (resumable, re-runnable per grader x rubric)
itemeval export   configs/my_study.yaml   # long-format parquet + CSV + cost ledger
itemeval status   configs/my_study.yaml   # grid completion matrix, spend, manifests
```

## Stability promises

Pre-1.0, exactly three surfaces are stable-ish (minor versions may still
break them, with changelog notice):

1. The **CLI** commands and exit codes.
2. The **config YAML** schema.
3. The **on-disk outputs** (parquet schemas, manifest JSON, study layout).

The Python API exports only `Item`, `ExperimentConfig`, `load_config`, and
`__version__`. Every `_`-prefixed module is internal and free to change.
