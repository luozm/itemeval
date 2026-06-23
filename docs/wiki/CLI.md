# CLI Reference

```
itemeval init DIR [options]
itemeval {estimate,preflight,generate,grade,export,status,rebless,harvest} CONFIG [options]
```

`init` scaffolds a new study into `DIR`; every other command takes the config
YAML path as its argument. `itemeval` is installed as a console script;
`python -m itemeval.cli` is equivalent.

The config-taking commands (`estimate|preflight|generate|grade|export|status|rebless|harvest`)
accept `-C/--base-dir DIR` to set the **work directory** that anchors outputs (the
`studies/` tree). It defaults to the current directory; inputs (prompts/rubrics)
always resolve relative to the config file, independent of `-C`.

`estimate`, `preflight`, `generate`, `grade`, and `status` also accept
`--policy {dev,full-interactive,full-batch}`, overriding `budget.policy` for
that invocation only (see
[Budget-and-Costs#policies](Budget-and-Costs.md#policies)).

## Exit codes (all commands)

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | unexpected error, or at least one condition failed during a run |
| 2 | config / template / adapter error (and argparse usage errors) |
| 3 | cost gate declined, or confirmation required in a non-interactive shell |
| 4 | projected cost exceeds `budget.max_usd` (hard cap; `--yes` does not override) |

## `init` — scaffold a new study

```
itemeval init DIR [--with-templates] [--force]
```

Writes `DIR/config.yaml` — a runnable starter study (mock provider, the USAMO
demo dataset, `builtin:` template references) named after `DIR`. Refuses to
overwrite an existing `config.yaml` unless `--force`. With `--with-templates`,
also copies the referenced built-in prompts/rubrics into `DIR/prompts/` and
`DIR/rubrics/` and rewrites the config to reference those local copies (bare
names). Then `cd DIR && itemeval status config.yaml`.

## `estimate` — projected cost, no model API calls

```
itemeval estimate CONFIG [--stage {generate,grade,all}] [--refresh-pricing] [--json]
```

Prints per-stage and per-condition projections (calls, tokens, USD), flags
unpriced models, and warns when generation is uncapped (no `max_tokens`).
`--refresh-pricing` pulls current per-token prices from the OpenRouter API
into a local cache first. Projections show the **full** policy-effective
grid plus the **remaining** figure (completed cells subtracted — what the
next run can actually spend; the money gate operates on it). See
[Budget-and-Costs#estimation](Budget-and-Costs.md#estimation).

## `preflight` — probe roster health before a paid run

```
itemeval preflight CONFIG [--policy POLICY] [--json]
```

Fires **one ~1-token call per distinct model** in the grid (solver models +
judge models) and reports roster health, so a dead model — a `404` EOL, a model
your key can't reach — is caught at sub-cent cost instead of failing mid-paid-run
and flooding the log:

```
preflight: probed 40 distinct model(s) over the provider network (~1 token each) — 39 ok · 1 dead · 0 unverified
  dead: openrouter/some/eol-model — BadRequestError: 404 model not found
```

Each model is labelled **ok**, **dead** (a *terminal* failure — fix the roster),
or **unverified** (a *transient* failure like a timeout or rate limit — the probe
can't confirm it, and deliberately never reports it dead). **Exit `1`** when any
model is dead, else `0`, so `itemeval preflight cfg && itemeval generate cfg`
short-circuits before spend. `--json` carries the `ok`/`dead`/`unverified` counts
and a `models[]` array of `{id, status, detail, http_status}`. See
[Error-Handling#preflight](Error-Handling.md#preflight).

This is a **deliberately-invoked** command, like `estimate`: it is *not* run
automatically inside `generate`/`grade` (invoking it is your consent to its tiny
spend, and the money gate stays the only thing that spends without asking).
`mockllm/*` models probe ok with no network call.

## `generate` — stage 1 (resumable)

```
itemeval generate CONFIG [-y/--yes] [--force] [--new-run] [--condition F]...
                  [--display {none,plain,rich,full}] [--json]
                  [--policy P] [--wave LABEL]
```

Flow: estimate → print projection → **gate** → run each generate condition
serially → upsert solutions, log index, ledger → write manifest. Conditions
already complete print `skipped: complete`. A condition whose eval fails is
reported and the rest continue (final exit 1). The summary names the run
identity — `recovery: attempt N of experiment <id> …` when a re-run recovered
an existing experiment, or `experiment: <id> · attempt 1 (new)` for a fresh one
(see [Outputs#run-identity](Outputs-and-Schemas.md#run-identity)).

- `-y/--yes` confirms the gate non-interactively (never overrides `max_usd`).
- `--force` re-runs completed work (rows are replaced, not duplicated).
- `--new-run` starts a **fresh experiment** instead of recovering the existing
  one. An unchanged config otherwise recovers (same `experiment_id`, next
  `attempt`, converging into the store); use this to fork a deliberately separate
  run of an identical config.
- `--condition F` (repeatable) selects conditions by exact id, id prefix, or
  slug — e.g. `--condition gpt-5-mini_minimal_default`.
- `--display` passes through to inspect (default `none`; try `rich`
  interactively).
- `--json` emits the run result as a single JSON document on stdout (the
  `GenerateResult` fields plus `pricing`, `estimate_usd`, and the `gate`
  outcome) and silences the live display unless `--display` is passed. A
  gate stop still emits a JSON document (projected cost, gate reason, the
  `--yes` rerun command) before exiting 3/4.
- `--wave LABEL` re-observes the current scope as a new epoch block, keeping
  both observations (see
  [Pipeline-Concepts#waves](Pipeline-Concepts.md#waves)).

## `grade` — stage 2 (resumable, re-runnable)

```
itemeval grade CONFIG [-y/--yes] [--force] [--new-run] [--condition F]...
               [--grader N]... [--rubric N]... [--display ...] [--json]
               [--policy P] [--wave LABEL]
```

Same flow over grade conditions (including `--json`, `--wave`, `--new-run`, and
the recovery/new identity line, as on `generate`). Verifiable conditions cost $0 and need no
model. `--grader`/`--rubric` (repeatable) narrow to specific judges/rubrics —
useful for adding a new grader over existing solutions. The summary line
reports `parse_failures` (rows kept with `parse_ok=false`).

## `export` — analysis-ready tables

```
itemeval export CONFIG [--snapshot NAME] [--no-harvest] [--json]
```

Joins gradings × solutions into `export/gradings_long.parquet` (one row per
grading event, 47 columns) plus a byte-equivalent CSV and `ledger.csv`.
Prints per-stage spend and the internal reconciliation verdict (ledger totals
vs row sums; reconciliation against provider dashboards is a manual step).

`--snapshot NAME` additionally freezes an immutable copy under
`export/snapshots/NAME/` (tables, locks, covering manifests, `snapshot.json`,
`STUDY_CARD.md`); an existing name is refused with exit 2. See
[Outputs-and-Schemas#snapshots](Outputs-and-Schemas.md#snapshots).

## `status` — completion matrix, no model API calls

```
itemeval status CONFIG [--no-harvest] [--json]
```

Prints datasets (id @ revision, item counts), the policy-effective scope,
both condition tables with `done/expected`, error and parse-failure counts,
spend per stage, and manifest count. The GENERATE table also shows an `empty`
column (no-error blank completions) and a `trunc` column (non-empty completions
cut at a length cap — `max_tokens`/`model_length`; see
[Error-Handling#truncation](Error-Handling.md#truncation)). The GRADE table shows a
`stale` column — grades whose solution was overwritten since they were scored
(their `solution_hash` no longer matches); these are excluded from `done` and
auto-re-grade on the next `grade` run. Both tables are scoped to the current
grid at the current scope (wave 0); studies with more than one wave get an
extra `waves:` line with per-wave gen/graded counts
([Pipeline-Concepts#waves](Pipeline-Concepts.md#waves)). `--json` emits the
full structured report (also available for `estimate` and `export`).

## `rebless` — re-bless a drifted `solvers.sample` pin

```
itemeval rebless CONFIG [--json]
```

For a sampled roster (`solvers.sample`) only. When you've **genuinely changed**
the sample spec (`n`/`seed`/`stratify_by`/`where`/`universe`), `generate`/`grade`
stop with a *change briefing* rather than running a different panel than the one
pinned in `model_locks.json`. `rebless` is the safe way forward: it **records the
new spec while keeping the pinned panel** (no re-draw), so the work you already ran
stays the scientific object. The lock then keeps both the spec the panel was *drawn
under* and the spec it was *re-blessed to*; later runs of the edited config match
and reuse. Prints the field-level diff and `N models kept`; `--json` emits the
`ReblessResult` (`diff`, `models`, `reblessed_at`, `lock_path`). The *other* choice
— deleting `model_locks.json` — re-draws a **different** panel (a different frame),
so prefer `rebless` whenever you want to keep your results. Errors (exit 2) if
there is no lock or the spec already matches.

## `harvest` — recover a crashed run's logs into the store

```
itemeval harvest CONFIG [--json]
```

Durable parquet (`solutions`/`gradings`) is written **after** a stage's
`inspect_ai.eval()` returns cleanly. A hard mid-run death — SIGKILL, OOM, or a
force-killed stuck request — leaves the progress only in inspect's on-disk `.eval`
(its incremental write-ahead log), so `status`/`export` go blind to the killed
run. `harvest` reads those `.eval` files back and projects them into the stores
through the same row builders a live run uses, so a crashed run's completed cells
become readable — and resumable — without re-running. It is **idempotent** (it
skips logs already in the store and the upserts dedup), so running it repeatedly is
safe; with nothing to recover it prints `harvest: nothing to recover`. `--json`
emits the `HarvestReport` (`generate_rows`, `grade_rows`, `logs`).

You rarely need to call it directly: **`status`, `export`, `generate`, and
`grade` auto-harvest first**, so the store reflects a crashed run *whenever you
look*, and a re-run resumes the recovered cells instead of re-paying them. When a
harvest recovers rows, those commands print a `recovered N solutions + M gradings
from K interrupted run log(s) into the store …` line (and carry a `harvested`
object under `--json`). Pass **`--no-harvest`** to any of them to skip the
automatic recovery. See [Error-Handling#crash-recovery](Error-Handling.md#crash-recovery).

## Hints

Commands may end with up to two dim `hint:` lines on **stderr** — one
observed fact from this run plus a doc pointer
(`hint: <fact> — learn more: <wiki-page#anchor>`). Hints never change
behavior and never block. Set `ITEMEVAL_HINTS=off` to silence them in the
text rendering; under `--json` they always ride as structured data in the
`hints` array (`{code, message, learn_more}`), uncapped.

Hint codes are stable and append-only:

| Code | Fires when |
|---|---|
| `cache-zero-reads` | same-prefix calls were scheduled for a provider cache discount but none engaged |
| `empty-solutions` | completions finished without an API error but produced no gradable text |
| `unpriced-models` | a model has no pricing-table entry (dollar columns missing; run unaffected) |
| `pilot-available` | the cost gate engaged with no completed rows yet for the selected conditions — a cheap `--policy dev` pilot can come first |
| `anthropic-openrouter-no-split` | an Anthropic-style model runs monolithic prompts through OpenRouter (no `split_prompt`/`split_rubric`), which earns no cache discount (estimate-time) |
| `split-head-below-min` | `split_prompt`/`split_rubric` is on but the shared head estimates below the provider's minimum cacheable prefix, so the cache silently won't engage (estimate-time) |
| `openrouter-unpinned-cache` | a cached `openrouter/anthropic/*` run has no `provider_routing` pin, so routing may land on an upstream that ignores cache markers (not raised when `prefer_native_batch` routes the run to the native batch API) |
| `estimate-is-ceiling` | a money-spending stage has no observed rows yet, so its projection is a pure ceiling (output assumed at `max_tokens`) — a `--policy dev` pilot calibrates an expected cost (estimate-time) |
| `native-batch-available` | a batch run has `openrouter/*` models with an eligible native batch endpoint (key present) but `budget.prefer_native_batch` is off, leaving the ~50% batch discount unclaimed |

## Typical session

```bash
itemeval estimate cfg.yaml --refresh-pricing   # sanity-check projected $
itemeval generate cfg.yaml                     # prompts if above confirm_above_usd
# ... interrupted? just re-run; completed conditions skip ...
itemeval generate cfg.yaml
itemeval grade    cfg.yaml
itemeval grade    cfg.yaml --grader second_judge   # later: new judge, $0 generation
itemeval export   cfg.yaml
itemeval status   cfg.yaml
```
