# Python API

Everything the CLI does is available programmatically — `import itemeval`
exposes one function per pipeline step, plus the config models. Use whichever
fits: the CLI for terminal/script workflows, the Python API for notebooks and
orchestration code. Both drive the exact same internals and produce the same
on-disk outputs.

## The surface

```python
import itemeval

itemeval.load_config(path)        # YAML -> ExperimentConfig (raises ConfigError)
itemeval.prepare_study(cfg)       # config -> PreparedStudy (datasets, templates, grid, plan, pricing)
itemeval.estimate_study(prep)     # -> Estimate (projected calls/tokens/$ per stage)
itemeval.run_generate(prep)       # -> GenerateResult (stage 1; writes solutions store)
itemeval.run_grade(prep)          # -> GradeResult (stage 2; writes gradings store)
itemeval.export_study(cfg)        # -> ExportResult (writes export/ tables)
itemeval.build_status(cfg, prep)  # -> StatusReport (completion matrix)

itemeval.ExperimentConfig         # the validated config model
itemeval.Item                     # the canonical item model
itemeval.__version__
```

These names (`itemeval.__all__`) are the entire supported Python surface.
`_`-prefixed modules remain importable but carry **no stability promise** —
pre-1.0 they may change in any minor release.

## A complete run

```python
import itemeval

cfg = itemeval.load_config("configs/my_study.yaml")
prep = itemeval.prepare_study(cfg)          # loads datasets (pins revisions), expands the grid

est = itemeval.estimate_study(prep)
print(f"projected: generate ${est.generate.usd:.2f}, grade ${est.grade.usd:.2f}")
for w in est.warnings:
    print("warning:", w)
assert est.total_usd < 20, "over budget — not running"   # the gate is YOUR job here

gen = itemeval.run_generate(prep)           # resumable: completed conditions skip
assert not any(c.status == "error" for c in gen.conditions), gen.conditions

graded = itemeval.run_grade(prep)
print(f"{graded.rows_written} gradings, {graded.parse_failures} parse failures")

exported = itemeval.export_study(cfg)
report = itemeval.build_status(cfg, prep)
```

Then analyze:

```python
import pandas as pd
df = pd.read_parquet(cfg.study_dir / "export" / "gradings_long.parquet")
```

## Result objects

All returns are pydantic models — use `.model_dump()` / `.model_dump_json()`
freely:

| Call | Returns | Key fields |
|---|---|---|
| `estimate_study` | `Estimate` | `generate`/`grade` (`.calls`, `.input_tokens`, `.output_tokens`, `.usd`, `.unpriced_models`, per-condition list), `total_usd`, `warnings` |
| `run_generate` | `GenerateResult` | `run_id`, `conditions` (per-condition `status` run/skipped/error, `rows_written`, `errors`, `usd`), `rows_written`, `total_usd`, `manifest_path` |
| `run_grade` | `GradeResult` | as above plus `parse_failures` |
| `export_study` | `ExportResult` | `rows`, output paths, `generation_usd`, `grading_usd`, `internally_reconciled` |
| `build_status` | `StatusReport` | datasets, item counts, per-condition `expected/completed/errors/parse_failures`, spend, manifests |

## Useful keyword arguments

```python
itemeval.prepare_study(cfg, refresh_pricing_table=True)   # pull OpenRouter prices first

itemeval.run_generate(prep,
    force=True,                          # re-run completed work (rows replaced)
    condition_filter=["gpt-5-mini_minimal"],  # id / id-prefix / slug, like --condition
    display="rich",                      # inspect progress UI (default "none")
)

itemeval.run_grade(prep,
    graders=["judge_b"],                 # like --grader
    rubrics=["strict"],                  # like --rubric
    force=False, condition_filter=None, display="none",
)

itemeval.estimate_study(prep)            # reads the solutions store automatically so
                                         # judge estimates use real stored solutions
```

## Differences from the CLI

1. **No confirmation gate.** `generate`/`grade` in the CLI refuse to run past
   `confirm_above_usd` without confirmation and abort on `max_usd`. The
   Python functions run when called — compare `estimate_study` totals against
   your own threshold first (the CLI's exit-code-3/4 behavior is yours to
   implement if you need it).
2. **No printing.** Information arrives as return values; condition-level
   eval failures are reported in `result.conditions` (status `"error"`), not
   raised — check them.
3. **Exceptions instead of exit codes.** Config/template/dataset problems
   raise `itemeval._errors.ItemevalError` subclasses (`ConfigError`,
   `TemplateError`, `AdapterError`, `StoreError`, `BudgetError`). The
   hierarchy lives in an internal module; catching the broad `Exception` or
   re-importing those names is at your own risk pre-1.0.

## Notes

- `import itemeval` is lightweight — heavy dependencies (inspect_ai, pandas)
  load lazily on first use of a pipeline function.
- `prepare_study` touches the HF Hub on first run (revision resolution +
  download); afterwards the lock file + local cache make it effectively
  offline.
- Stages call inspect's `eval()` serially, one condition at a time — don't
  run two stages concurrently in one process or share a study directory
  across processes.
- Everything remains resumable and idempotent exactly as documented in
  [Pipeline Concepts](Pipeline-Concepts.md): calling `run_generate` twice is
  safe, the second call skips completed conditions.
