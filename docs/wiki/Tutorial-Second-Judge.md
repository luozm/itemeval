# Tutorial 4 — Add a second judge (and rubric) at $0 generation cost

**Use case:** "How much do my results depend on the judge? Would a different
judge model — or a stricter rubric — change the scores?" This is the question
behind every LLM-as-judge methodology study, and it is the reason itemeval's
pipeline is two-stage.

Solutions live in a store, independent of grading. Adding a grader or a rubric
**re-uses every stored solution**: you pay judge tokens only, never generation
again. You will extend [Tutorial 2](Tutorial-LLM-Judge.md)'s study with a
second judge model and a stricter rubric, then measure judge agreement.

**You need:** the finished `proofs.yaml` study from Tutorial 2, plus
`pip install itemeval[anthropic]` and an `ANTHROPIC_API_KEY` for the second
judge (any provider works). Cost: a few cents.

## Step 1 — Add the new grading facets

Edit `proofs.yaml` — only the grading side changes; nothing under `solvers:`
or `benchmark:` moves:

```yaml
facets:
  prompt: [builtin:standard]
  grader: [judge_a, judge_b]          # was: [judge_a]
  rubric: [builtin:standard, strict]  # was: [builtin:standard]
graders:
  judge_a:
    model: openai/gpt-5-mini
    max_tokens: 4096
    reasoning_effort: minimal
  judge_b:                            # NEW: a second judge, different provider
    model: anthropic/claude-haiku-4-5
    max_tokens: 4096
```

And create the stricter rubric as `rubrics/strict.md` next to the config
(bare name = local file; it must contain `{input}` and `{solution}`):

```markdown
You are grading a candidate proof. Award an integer score from 0 to 7.

Problem:
{input}

Reference solution:
{target}

Candidate solution:
{solution}

Award 7 only for a complete and rigorous proof with all cases handled.
Any unjustified step caps the score at 3. A correct final answer with no
valid argument scores 0.
```

The grade grid is now `grader × rubric` = **4 grade conditions**. The one
already graded (judge_a × builtin:standard) is complete and will be skipped.

## Step 2 — See what's pending, then grade only the new cells

```bash
itemeval status   proofs.yaml    # 3 new grade conditions at 0/N, old one complete
itemeval estimate proofs.yaml    # generation projects too, but it won't re-run
itemeval grade    proofs.yaml    # grades only the 3 pending conditions
```

`grade` computes what's pending per (grader × rubric) over the stored
solutions and runs just that. Generation is untouched — the solutions store is
read-only to the grade stage. You can also target cells explicitly, which is
handy when iterating on one rubric:

```bash
itemeval grade proofs.yaml --grader judge_b              # one judge, all rubrics
itemeval grade proofs.yaml --grader judge_a --rubric strict
```

## Step 3 — Measure judge agreement

```bash
itemeval export proofs.yaml
```

Every grading event is a row, so agreement is a pivot away:

```python
import pandas as pd

df = pd.read_parquet("studies/proof_judging/export/gradings_long.parquet")
ok = df[df.parse_ok]   # exclude flagged parse failures from analysis

# One column per (grader, rubric), one row per solution
scores = ok.pivot_table(
    index=["item_id", "gen_condition_id", "replication"],
    columns=["grader_name", "rubric_name"],
    values="score",
)

scores.corr()                              # inter-judge / inter-rubric correlation
(scores.max(axis=1) - scores.min(axis=1))  # per-solution judge disagreement
  .sort_values(ascending=False).head(10)   # the solutions judges fight over
```

Disagreement cases are auditable: each row's `reasoning` and
`judge_completion` columns hold both judges' rationales for the *same* stored
solution, and `grade_log_file` points at the raw transcripts.

## Why this matters

With in-eval grading (the usual harness design), each of the 4 judge × rubric
cells would have re-run generation — paying the most expensive stage 4 times
to study the cheap one. Here the grading dimension scales independently:
N judges × M rubrics over the same solutions costs only judge tokens, and the
gradings table keeps grader and rubric as first-class design columns. That is
what makes judge-sensitivity and rubric-sensitivity studies routine instead of
heroic.

Two notes for serious measurement work:

- Judge temperature is pinned to 0 in v0.1, so re-judging the same solution
  under the same (grader × rubric) is not a replication design — judge
  replication is on the roadmap
  ([BACKLOG.md](https://github.com/luozm/itemeval/blob/main/docs/BACKLOG.md)).
- Editing a rubric file changes its content hash and therefore its condition
  id: old gradings stay under the old id, the edited rubric grades fresh. Two
  rubric versions can never silently mix ([Pipeline Concepts](Pipeline-Concepts.md)).

## Next

[Tutorial 5 — Scale up without surprises](Tutorial-Budget-and-Scale.md):
take a validated design from `dev` scope to the full item set, batched and
budget-capped.
