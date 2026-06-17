# FAQ / Troubleshooting

## Why two stages instead of inspect's built-in model-graded scorers?

With an in-eval scorer, judge calls live inside the generating eval: one set
of logs, no separate batching/caching/cost attribution, and adding a second
judge or rubric later re-runs generation. itemeval stores solutions once and
fans grading out over them — `itemeval grade --grader new_judge` against a
finished study costs only judge tokens. For item-response analysis (where
grader and rubric are *facets*, not afterthoughts) this decoupling is the
whole point.

## Why did my condition ids change?

Condition ids hash the condition's *content*: model id, resolved sampling
params, prompt/rubric name **and file content**. If you edited a template or
changed `solvers.temperature`, the affected conditions are genuinely new
cells — old rows remain under the old id, and `status` shows the new cells
as 0/N. This is deliberate: you can never silently mix results produced
under different conditions. Cosmetic renames also change the id (the slug is
part of it), so finish naming before big runs.

## `grader 'judge_a' is not defined under graders: and is not a model id`

Every `facets.grader` name needs either an entry under `graders:` or to be a
model id containing `/`. Add:

```yaml
graders:
  judge_a: {model: openai/gpt-5-mini}
```

## Can each item have its own rubric?

Yes — that's the `grading_scheme` item field. The `rubric` facet is a single
*shared* template (the grading instructions); the per-item rubric *content* — a
different point breakdown per problem — rides on each item and renders into the
template at `{grading_scheme}`, alongside the per-item reference solution
(`{target}`). Map it from your dataset with `mapping.grading_scheme` (a
list-valued column is stored as canonical JSON). This is the MathArena
per-problem-scheme pattern — see
[Tutorial 2](Tutorial-LLM-Judge.md#per-item-rubrics-shared-template-vs-per-item-scheme).
There is no per-item *template* selection (one harness, per-item content), by
design: the per-problem texts are study data that live in your dataset, not the
package.

## `local template 'x' not found in .../prompts/solver`

A **bare** template name (`x`) resolves to a local file under `prompts_dir`/
`rubrics_dir`, anchored to the **config file's directory**. Either create
`<config dir>/prompts/solver/x.md`, point `prompts_dir` at the right directory,
or — if you meant a packaged template — reference it as `builtin:x`. The error
lists the local templates found and suggests `builtin:` when a built-in of that
name exists. (Outputs are separate: they anchor to the working directory, not
the config dir — see [Configuration](Configuration.md).)

## `duplicate item id 'x' in datasets ...`

Item ids must be unique across **all** configured datasets — they are the join
key into every store and the export table. Two datasets that share a natural key
(a per-split row index, a per-release problem number) collide, and omitting
`mapping.id` falls back to a per-dataset row index that collides too. Make the
ids unique with a **composite `mapping.id`**: a list of columns, or a template
with a `{dataset}` token (the dataset basename), joined with `:` —
`mapping.id: ["{dataset}", problem_idx]` yields `set_2026:6`. A single plain
column is unchanged, so existing studies are unaffected. See
[Configuration](Configuration.md#composite-item-ids).

## Exit code 3 in CI / scripts

The cost gate needs confirmation and stdin isn't a TTY. Pass `--yes`
(and set `budget.max_usd` as the un-overridable backstop).

## Some rows have usd = 0.0 and empty token counts

Those calls were served by inspect's local response cache — genuinely free.
Null `usd` is different: it means no price was known for the model (run
`estimate --refresh-pricing` or provide `budget.pricing_path`).

## Judge rows with `parse_ok = false`

The judge's output didn't contain a valid `{"score": ...}` JSON block;
`parse_error` says exactly how it failed, and `judge_completion` holds the
raw text. These rows are kept (never dropped) and are **final** — re-running
`grade` won't retry them. If you fix a rubric to elicit better-formatted
output, the rubric hash changes and grading starts a fresh condition; to
re-grade in place, use `grade --force`.

## A run was interrupted / a provider erred mid-run

Just re-run the same command. The store is keyed: completed work skips,
errored rows re-run, and the response cache means already-paid calls aren't
paid again. `status` shows exactly what's missing.

## How do I run only part of the grid?

`--condition <id|id-prefix|slug>` (repeatable) on generate/grade;
`--grader` / `--rubric` on grade. The dev policy (`budget.policy: dev`)
limits to the first `dev_items` items globally.

## Does `status`/`estimate` really make no API calls?

No **model** API calls, ever. The first run of any command resolves and
downloads the dataset from the HF Hub (free); after that, the revision lock
plus HF's local cache make loads effectively offline.

## Can I use a dataset that isn't on HuggingFace?

Not yet — `adapter: hf` is the only adapter in v0.1. GitHub-repo and local
JSONL adapters are on the roadmap ("Later"); the adapter protocol in
`adapters/_base.py` is the extension point.

## Where are the raw transcripts?

`studies/<study>/logs/<stage>/<condition_id>/*.eval` — full inspect logs.
`inspect view --log-dir studies/<study>/logs` gives you the inspect UI over
them; every store row carries its `log_file` and `sample_uuid`.

## Is `mockllm/...` safe to leave in a config?

Yes — any model id starting with `mockllm/` runs a deterministic free stub
(solver-style or judge-style depending on stage). It exists so pipelines can
be validated end-to-end at $0; swap in real model ids when ready.
