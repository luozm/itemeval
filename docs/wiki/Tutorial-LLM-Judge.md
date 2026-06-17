# Tutorial 2 — Grade open-ended answers with an LLM judge

**Use case:** "My benchmark's answers are proofs / essays / explanations — no
string match can grade them. I want an LLM judge to score each answer against
a rubric, and I want the judge's reasoning on the record."

You will solve a small set of olympiad proof problems
([MathArena/usamo_2025](https://huggingface.co/datasets/MathArena/usamo_2025) —
the same public dataset the repo's free demo uses) and grade the proofs with a
judge model against a rubric. Judge calls are real model calls: they get their
own logs, caching, retries, and cost accounting, and they must return a
structured score.

**You need:** `pip install itemeval[openai]`, an `OPENAI_API_KEY`. Time: ~15
minutes; cost: a few cents at `dev` scope.

## Step 1 — A config with a grader instead of a scorer

Save as `proofs.yaml`:

```yaml
study: proof_judging
benchmark:
  adapter: hf
  datasets:
    - id: MathArena/usamo_2025
      split: train
  mapping:
    id: problem_idx
    input: problem
    target: sample_solution        # reference solution -> {target} in the rubric
    grading_scheme: grading_scheme # per-item rubric text -> {grading_scheme}
    metadata: [points]
solvers:
  models: [openai/gpt-5-mini]
  max_tokens: 16384                # proofs are long; reasoning models need headroom
facets:
  prompt: [builtin:standard]       # asks for a complete, rigorous argument
  grader: [judge_a]                # judge grading instead of scorer
  rubric: [builtin:standard]       # packaged rubric template
graders:
  judge_a:
    model: openai/gpt-5-mini
    max_tokens: 4096               # the judge also needs reasoning headroom
    reasoning_effort: minimal
budget:
  policy: dev                      # first 2 items while we validate the pipeline
  confirm_above_usd: 1
```

What changed versus [Tutorial 1](Tutorial-Verifiable-Benchmark.md):

- **`facets.grader` + `graders:`** replace `facets.scorer`. A grader is a
  judge model with its own settings; judge temperature is pinned to 0 in v0.1
  for grading stability.
- **`facets.rubric`** names the rubric template. The packaged
  `builtin:standard` rubric shows the judge the problem (`{input}`), the
  grading scheme (`{grading_scheme}`), the reference solution (`{target}`),
  and the candidate solution (`{solution}`), and asks for a score according to
  the scheme.
- **`mapping.grading_scheme`** wires a per-item rubric column from the dataset
  into the template. If your dataset has no such column, omit it — write the
  scoring criteria into your rubric file instead (Step 5).

### Per-item rubrics: shared template vs per-item scheme

Two things both feel like "the rubric"; keeping them apart is the key idea:

- **The rubric template** (`facets.rubric`) is the *shared grading harness* —
  one instruction layout used for every item in the condition: how to present
  the problem, scheme, and reference, and how to ask for a score. It's a facet,
  so swapping it starts a fresh grade condition.
- **The per-item scheme** (`Item.grading_scheme`, from `mapping.grading_scheme`)
  is the *problem-specific* content the template renders at `{grading_scheme}` —
  a different point breakdown for each item. The reference solution (`{target}`)
  varies per item the same way.

So per-item rubrics need no special mechanism: one template, per-item content.
This is exactly the
[MathArena "Proof or Bluff?"](https://huggingface.co/datasets/MathArena/usamo_2025)
pattern — one grading instruction plus a hand-written per-problem scheme (each
USAMO problem scored out of 7 with its own milestones). Map that column to
`grading_scheme` and every problem is graded against its own rubric. A
list-valued scheme column (MathArena's is a list of `{points, description}`
items) is stored as canonical JSON, so structured rubrics render cleanly. The
per-problem texts stay in your dataset, where study data belongs — not in the
config.

## Step 2 — Estimate, generate, grade

```bash
itemeval estimate proofs.yaml          # now shows TWO paid stages
itemeval generate proofs.yaml          # solve the problems (stage 1)
itemeval grade    proofs.yaml          # judge the stored solutions (stage 2)
```

Note that `estimate` now projects costs for grading too — the judge reads the
whole problem + rubric + solution, so judge input tokens often rival
generation. Grading runs as its **own inspect task** whose dataset is your
stored solutions; it never re-generates anything.

## Step 3 — The judge output contract

itemeval appends a format instruction to every rubric: the judge must end with
a fenced JSON block

```json
{"score": 4, "reasoning": "..."}
```

Parsing is strict. If the judge replies without a valid numeric `score`, the
row is **kept** with `parse_ok=false` and an exact failure code
(`no_json_object`, `no_score_in_json`, `score_not_numeric`,
`score_not_finite`) plus the raw judge text — never silently dropped, and
never retried on re-runs (a parse failure is a *result*; use `grade --force`
to redo). The `grade` summary line reports `parse_failures` so you see them
immediately.

## Step 4 — Read the judged data

```bash
itemeval export proofs.yaml
```

```python
import pandas as pd

df = pd.read_parquet("studies/proof_judging/export/gradings_long.parquet")
df[["item_id", "score", "reasoning", "parse_ok", "grade_usd"]]
```

Every row now carries the judge's numeric `score` **and** its `reasoning` —
auditable, per item. The full judge completion is in `judge_completion`, and
the raw transcript of every judge call is an `.eval` log under
`studies/proof_judging/logs/grade/`.

## Step 5 — Write your own rubric

The packaged rubric is a generic starting point. To customize it:

```bash
itemeval init proof_study --with-templates   # copies builtin templates locally
```

or create `rubrics/strict.md` next to your config:

```markdown
You are grading a candidate proof. Award an integer score from 0 to 7.

Problem:
{input}

Reference solution:
{target}

Candidate solution:
{solution}

Award 7 only for a complete and rigorous proof. Deduct points for gaps,
unjustified steps, or missing cases. A correct final answer with no valid
argument scores at most 1.
```

then reference it by **bare name** (bare = local file; `builtin:` = packaged):

```yaml
facets:
  rubric: [strict]
```

Rubrics must contain `{input}` and `{solution}`; `{target}`,
`{grading_scheme}`, and `{id}` are optional. Placeholders are validated before
any run, and the rubric's content hash goes into the condition id — editing a
rubric starts a fresh, clearly-separated grade condition
([Pipeline Concepts](Pipeline-Concepts.md)).

## Troubleshooting

- **Empty solutions reported by `grade`** — a reasoning model spent the whole
  `max_tokens` budget on hidden reasoning. Raise `solvers.max_tokens` or lower
  `reasoning_effort`; see `solvers.on_empty` in
  [Configuration](Configuration.md).
- **Many parse failures** — your judge model may be wrapping the JSON in extra
  prose or hitting its own `max_tokens` mid-reply. Raise the grader's
  `max_tokens` first; it fixes most cases.

## Next

The judge model and the rubric are *facets* — you can add more of either over
the same stored solutions, paying only judge tokens:
[Tutorial 4 — Add a second judge at $0 generation](Tutorial-Second-Judge.md).
