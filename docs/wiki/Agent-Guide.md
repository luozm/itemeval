# Agent Guide — driving itemeval from an AI agent

This page is written for an **LLM agent** (Claude Code, Codex, a custom
orchestrator) operating itemeval on a user's behalf — and for the humans
wiring that up. It is a compact operating contract: what to run, in what
order, what every outcome means, and which guardrails must never be bypassed.
If you maintain a study repo, copy the
[drop-in instructions block](#drop-in-block-for-a-study-repos-claudemd--agentsmd)
into your repo's `CLAUDE.md` / `AGENTS.md`.

(This is about *using* the installed package. For developing itemeval itself,
see `CLAUDE.md` in the repo.)

## Mental model in five lines

1. One YAML config fully describes a **study**: a benchmark (HuggingFace
   dataset + column mapping) and a facet grid (models × prompts ×
   model-configs × graders × rubrics × replications).
2. `generate` produces solutions; `grade` scores stored solutions (verifiable
   scorer at $0, or LLM judge); the stages are decoupled — new graders/rubrics
   never re-pay generation.
3. All state lives under `<cwd>/studies/<study>/` in keyed parquet stores;
   every command is **idempotent and resumable** — re-running is always safe
   and never duplicates or re-pays completed work.
4. Money is governed by config: `budget.policy` (scope), `confirm_above_usd`
   (confirmation gate), `max_usd` (hard abort, not overridable).
5. `export` writes the analysis artifact: `export/gradings_long.parquet`, one
   row per grading event.

## The command contract

```
itemeval init DIR [--with-templates]            # scaffold config.yaml (no API calls)
itemeval estimate CONFIG [--refresh-pricing] [--json]   # projected $; NO model calls
itemeval generate CONFIG [--yes] [--json] [--force] [--condition F]...  # stage 1 (paid)
itemeval grade    CONFIG [--yes] [--json] [--force] [--grader N] [--rubric N] [--condition F]...  # stage 2 (paid if judge)
itemeval export   CONFIG [--json]               # tables + ledger (no API calls)
itemeval status   CONFIG [--json]               # completion matrix (no API calls)
```

- `estimate`, `status`, `export` never call a model API and are always safe.
  First-ever run downloads the dataset from the HF Hub (free).
- `--json` (on every command) emits the full structured report — **prefer it
  over parsing human-readable stdout.** On `generate`/`grade` it carries the
  run result plus `pricing`, `estimate_usd`, and the `gate` outcome, and a
  gate stop still emits a JSON document (projected cost, gate reason, the
  `--yes` rerun command) before exit 3/4.
- `-C/--base-dir DIR` anchors the output tree (`studies/`); default is the
  current directory. Inputs (prompts/rubrics) always resolve relative to the
  config file.
- For paid runs in non-interactive shells, `--yes` is required whenever the
  projection exceeds `confirm_above_usd` (there is no TTY to confirm on).

### Exit codes (deterministic — branch on them)

| Code | Meaning | Correct agent reaction |
|------|---------|------------------------|
| 0 | success | proceed |
| 1 | unexpected error, or ≥1 condition failed during the run | run `status --json`; re-run the same command (errored samples retry); escalate if the same rows fail repeatedly |
| 2 | config / template / adapter error | fix the YAML or template file; do not retry unchanged |
| 3 | cost gate needs confirmation (non-interactive) | report the projected cost to the user; re-run with `--yes` **only** within an authorized budget |
| 4 | projection exceeds `budget.max_usd` | **stop**. Never raise `max_usd` yourself — that number is the user's, not yours |

## Hard rules

1. **Never raise or remove `budget.max_usd`, and never inflate
   `confirm_above_usd`,** without an explicit user instruction quoting the new
   number. Exit 4 is the user's hard cap working as designed.
2. **Always run `estimate` (ideally `--refresh-pricing --json`) before the
   first `generate`/`grade` of a session** and compare `total_usd` against the
   budget you were given.
3. **Start every new config at `policy: dev`** (a few items). Scale to
   `full-interactive`/`full-batch` only after the dev run's export looks right.
4. **Re-run, don't repair.** On interruption or partial failure, re-invoke the
   identical command — the stores are keyed and the response cache prevents
   double payment. Never delete or hand-edit `studies/<study>/*.parquet`.
5. **One command at a time per study directory.** Never run two
   `generate`/`grade` processes concurrently on the same study.
6. **Don't loop on parse failures.** Judge rows with `parse_ok=false` are
   final results, not retryable errors; re-running `grade` will not change
   them (that needs `--force` or a rubric change — a user decision).
7. **API keys come from the environment** (`OPENAI_API_KEY`, etc.). Never
   write keys into configs or commit them.

## Standard operating procedure

```bash
# 1. Scaffold (or receive) a config
itemeval init my_study && cd my_study

# 2. Edit config.yaml: point benchmark.datasets/mapping at the target dataset,
#    set solvers.models, choose facets.scorer (verifiable) or grader+rubric (judge),
#    keep policy: dev.

# 3. Validate without spending
itemeval status   config.yaml --json   # config parses; grid is what you expect
itemeval estimate config.yaml --refresh-pricing --json   # projected $; check warnings

# 4. Dev run (cheap), then inspect
itemeval generate config.yaml --yes
itemeval grade    config.yaml --yes
itemeval export   config.yaml --json
#    -> read studies/<study>/export/gradings_long.parquet; check scores, parse_ok,
#       empty-solution counts; sanity-check a solution and a judge reasoning by eye

# 5. Report findings + full-scope estimate to the user; on approval flip
#    budget.policy to full-batch, set max_usd, then repeat 3–4 at full scope.
```

Key config rules that bite agents (full reference:
[Configuration](Configuration.md)):

- Validation is strict — unknown keys are rejected (exit 2 with the field
  named). Fix the config; don't retry.
- `facets` needs at least one of `scorer` (verifiable: `exact_match` /
  `multiple_choice` / `numeric`) or `grader` (+ entries under `graders:`).
- Template namespaces: `builtin:NAME` = packaged template; bare `NAME` = local
  file under `prompts_dir`/`rubrics_dir` (relative to the config file). Solver
  prompts must contain `{input}`; rubrics must contain `{input}` and
  `{solution}`.
- Pooling datasets that share a natural key (a row index, a per-year problem
  number) — item ids must be unique across datasets, so give them a composite
  `mapping.id`: a list of columns, or a template with a `{dataset}` token
  (the dataset basename), joined with `:` —
  `mapping.id: ["{dataset}", problem_idx]` → `set_2026:6`. A single column is
  unchanged. Without it, two datasets reusing a key abort with `duplicate item
  id` (exit 2). See [Configuration](Configuration.md#composite-item-ids).
- Reasoning models need `max_tokens` headroom for hidden reasoning **plus**
  the visible answer; if `grade`/`status` report `empty` solutions, raise
  `max_tokens` or lower `reasoning_effort` and set `solvers.on_empty: rerun`.

## Reading results programmatically

Prefer the parquet stores over stdout:

| Artifact | Path under `studies/<study>/` | Use |
|---|---|---|
| Analysis table | `export/gradings_long.parquet` | one row per grading event; key cols: `item_id, model, prompt_name, replication, grader_name, rubric_name, score, parse_ok, solution, reasoning, gen_usd, grade_usd` |
| Solutions | `solutions.parquet` | per (condition × item × epoch): `solution, stop_reason, error`, tokens, `usd` |
| Gradings | `gradings.parquet` | per grading event incl. `parse_error`, `judge_completion` |
| Cost ledger | `ledger.parquet` / `export/ledger.csv` | spend by run × stage × condition × model |
| Manifests | `manifests/<run_id>.json` | full reproducibility record per run |
| Raw transcripts | `logs/<stage>/<condition_id>/*.eval` | inspect_ai logs (open with `inspect view`) |

Or stay in Python — one public function per command, same semantics, pydantic
results (`.model_dump()` for JSON). Consent is a parameter: pass `max_usd=`
and the run raises `itemeval.BudgetExceededError` before any API call when
the remaining projection exceeds it
([Python API](Python-API.md)).

```python
import itemeval
cfg  = itemeval.load_config("config.yaml")
prep = itemeval.prepare_study(cfg)
est  = itemeval.estimate_study(prep)        # remaining figures: est.generate.remaining_usd
gen  = itemeval.run_generate(prep, display="none", max_usd=BUDGET_USD)
assert not any(c.status == "error" for c in gen.conditions)
itemeval.run_grade(prep, display="none", max_usd=BUDGET_USD)
itemeval.export_study(cfg)
```

## Failure triage table

| Observation | Meaning | Action |
|---|---|---|
| exit 2, pydantic message naming a field | invalid config | fix that field |
| exit 2, `local template 'x' not found` | bare name with no local file | create the file, or use `builtin:x` |
| exit 2, `duplicate item id ... in datasets` | pooled datasets share a natural key | give items a unique id via composite `mapping.id` (e.g. `["{dataset}", <col>]`); see [Configuration](Configuration.md#composite-item-ids) |
| exit 3 | gate wants confirmation | surface cost to user; `--yes` if authorized |
| exit 4 | projection > `max_usd` | stop; report; user decides |
| exit 1 + `ERROR:` on a condition line | whole condition failed (auth, provider down) | check the named exception; fix env/keys; re-run |
| `errors=N` in summary / `err` column in status | per-sample provider failures | re-run the same command (they retry) |
| `parse_failures=N` / `parse_ok=false` rows | judge output didn't parse | final, not retryable; inspect `judge_completion`; consider raising grader `max_tokens` or fixing the rubric, then `grade --force` |
| `empty=N` / `empty` column | completions with no text (reasoning-token exhaustion) | raise `max_tokens` / lower `reasoning_effort`; `on_empty: rerun`; re-generate |
| rows with `usd = 0.0`, zero tokens | served by local response cache | normal — genuinely free |
| rows with `usd = null` | model not in pricing table | `estimate --refresh-pricing`; run is otherwise fine |

## Drop-in block for a study repo's CLAUDE.md / AGENTS.md

```markdown
## Running evaluations (itemeval)

This repo's eval studies run on itemeval (https://github.com/luozm/itemeval —
agent contract: https://github.com/luozm/itemeval/wiki/Agent-Guide).

- Pipeline per study config: `itemeval estimate <cfg> --json` →
  `itemeval generate <cfg> --yes` → `itemeval grade <cfg> --yes` →
  `itemeval export <cfg> --json`. All commands are idempotent; on any
  interruption or partial failure, re-run the same command.
- ALWAYS `estimate` before the first paid command; report projected USD.
- NEVER change `budget.max_usd` or `confirm_above_usd` without an explicit
  instruction. Exit code 4 = over hard cap: stop and report.
- New/changed configs start at `budget.policy: dev`; full runs only after a
  green dev export and explicit approval.
- Results: read `studies/<study>/export/gradings_long.parquet` (one row per
  grading event). Never hand-edit anything under `studies/`.
```
