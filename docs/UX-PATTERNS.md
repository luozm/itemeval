# UX Patterns — how itemeval behaves toward humans and agents

> **Status: BINDING.** Every feature — new or touched — must pass the
> development checklist at the end of this document.

## The promise

itemeval already promises "never be surprised by a bill." These patterns
extend it to the general form: **never be surprised, period.** The feeling of
losing control of a tool comes from exactly two failures — the tool doing
things **silently**, or **interrupting** when it shouldn't. Every rule below
guards against one of the two.

## The two operators

Every itemeval surface is operated by one of two kinds of user — and often
**both at once** (an agent runs the commands; a human supervises the agent's
summary).

| | Human user | Agent user |
|---|---|---|
| Learns from | tutorials once, reference pages when stuck, scaffold comments | Agent-Guide + whatever the tool prints **at the moment of action** |
| Acts via | terminal commands, or Python in a notebook | CLI with `--json` / Python API, inside a loop |
| Consents via | interactive prompt, or an explicit code parameter | a flag, bounded by limits the human set in config |
| Trust breaks when | something happened that nothing announced; a wall of noise; an unexpected stop | an exit code or JSON key changes meaning; a prompt blocks forever |

**The relay rule.** Because the common case is *agent operates, human reads
the agent's summary*, anything that matters must survive **relay**: an agent
will quote a plain factual line in its summary; it cannot quote a progress bar
that never rendered off-TTY. Designing output for the agent **is** designing
it for the human, one hop removed.

**No operator detection.** The package never knows — and never guesses — *who*
is driving; any "is this an agent?" heuristic misclassifies both ways. It
observes three facts, each with a standard mechanism, and every behavior
difference must trace to one of them:

| Observable | Mechanism | What it may decide |
|---|---|---|
| Is stdin an interactive terminal? | `isatty()` | whether the money gate may *ask* (otherwise exit 3); whether ephemeral progress renders |
| Did the operator ask for structure? | `--json` — explicit, never inferred | JSON document vs text rendering |
| Did the operator consent in advance? | `--yes` flag / `max_usd=` parameter | gate behavior |

There is no "human mode": the default output is a **text rendering** read by
humans *and* agents (the relay rule makes one rendering serve both), and
`--json` is a *declared* machine rendering, not a detected one. Detect
channels; let operators declare intent; never guess identity.

---

## The laws

### 1. No silent side effects

Anything that touches the world **outside the study directory** — network
calls, global cache reads **and** writes, revision/lock pinning,
provider-side job creation — must announce itself in one durable line: *what
happened, where, fresh or reused.*

- **Reuse is announced as loudly as fetching.** Silent cache reuse is how a
  user loses track of which data they are actually running on.
- Writes *inside* the study directory are normal operation and exempt —
  except replacing existing result rows, which must be stated (Law 2).
- Announcement lines are never noise: in the text rendering they print
  unconditionally; no switch hides them.
- The model to copy is the pricing provenance line
  (`pricing: merged (updated …, 2d old)`).

### 2. Advice never acts; nothing blocks but money

Three interaction strengths; every output picks exactly one:

| Strength | Changes behavior? | Stops the run? | Form |
|---|---|---|---|
| **Hint** | never | never | one line + pointer (hint framework below) |
| **Warning** | never | never | one line in the summary block |
| **Gate** | yes (waits/aborts) | yes | **money only**: `confirm_above_usd` / `max_usd` |

No feature may add a new blocking interaction. If a future feature must pause
for anything besides projected spend (e.g. "this re-run replaces 12 existing
rows"), it joins the *existing* money gate as part of the same single
confirmation — never a second prompt.

### 3. Consent is native to the surface

Same meaning, three syntaxes; a surface never borrows another's style:

| Surface | Consent looks like | Never |
|---|---|---|
| Human at a terminal | one interactive `Proceed? [y/N]` per run plan | asking twice for one plan |
| Python (notebook, script) | an explicit parameter (e.g. `max_usd=20`); raise if exceeded | **a library never prompts** — it would hang notebooks and CI |
| Agent / CI | `--yes`, valid only under the caps a human wrote in config | `--yes` overriding `max_usd` (nothing overrides it) |

### 4. One verb to start, staged verbs to control

The front door is a single verb covering the happy path (planned:
`itemeval run` = estimate → one gate → generate → grade → export). The staged
commands are not a fallback — they are the **real contract**, because real
studies routinely need exactly one stage:

- **grade-only** — add a judge or rubric over stored solutions ($0 generation).
- **estimate-only** — planning, price refresh, CI assertion before any spend.
- **generate-only** — freeze solutions first, design rubrics after reading them.
- **export-only** — rebuild the analysis view without touching results.
- **status** — watch a long or batch run from another shell; audit an old study.
- **selective re-runs** — `--condition` / `--grader` / `--rubric` after a fix.
- **split execution** — generate today / grade tomorrow; different machines;
  batch submit now, collect later.

`run` is sugar over the stages; the stages never become second-class.

### 5. Defaults absorb optimization knobs; design knobs stay explicit

Every config option belongs to exactly one bucket, decided at design time:

| Bucket | Examples | Default policy |
|---|---|---|
| **Safety interlock** | `max_usd`, `confirm_above_usd`, `dev` policy | friction is the feature; stays explicit forever |
| **Design declaration** | facets, replications, `split_prompt`/`split_rubric` (they change condition ids) | always explicit; never auto-flipped under a user |
| **Optimization** | `cache_schedule`, `cache_prompt`, call ordering | must trend toward invisible, correct defaults; a knob here is a TODO, not a feature |

The long-term measure of this package's UX is how many optimization knobs we
have **retired** into defaults.

### 6. Every fact has three renderings

A fact that exists in only one channel is a bug:

| Rendering | For | Failure if missing |
|---|---|---|
| one human-readable line | people at terminals | invisible at point of need |
| a stable JSON field | agents, scripts | invisible to automation |
| a doc anchor (one owning page) | depth, tutorials | unexplainable; hints have nowhere to point |

The text line and the JSON field carry the **same numbers** — never a fact in
prose that automation can't read, never a JSON-only fact a person can't see.

### 7. The machine surface is an API

Exit codes (`0` ok, `3` confirmation needed, `4` budget cap), JSON keys, and
hint codes are **stable and append-only**, documented in the wiki, and changed
only with a changelog entry. Agents build retry logic on these; renaming a
JSON key breaks automation as surely as renaming a public function.

### 8. Output is written to be quoted

Assume every line will be relayed — summarized by an agent or skimmed by a
tired human:

- Each command ends in a **summary block of self-contained lines with
  numbers**: `generate: 24/24 rows · $0.41 · cache_read 78%` — never `Done.`
- Progress bars and spinners are decoration for live TTYs only; **no fact may
  exist only in ephemeral output** (off-TTY it never rendered at all).
- One line per fact; no fact buried mid-paragraph.

---

## Channel spec

### Text rendering (the default)

```
<provenance lines>   dataset/pricing/cache facts (Law 1) — stdout, one each
<live progress>      TTY only, ephemeral, zero information of record
<summary block>      self-contained facts with numbers (Law 8) — stdout
<hint lines>         0–2, dim, after the summary — stderr
```

Hint lines go to **stderr**: they are commentary about the run, not output of
the run, so stdout stays pipeable even without `--json` (the same convention
as compiler diagnostics). Facts of record stay on stdout.

### `--json` mode

- stdout carries **only** the JSON document — no prose, no hints as text, ever.
- Every provenance/summary fact has a field; hints ride as structured data:

```json
"hints": [
  {
    "code": "cache-zero-reads",
    "message": "this run repeats long text but no provider cache reads occurred",
    "learn_more": "wiki/Cost-Savings#two-gotchas"
  }
]
```

- `hints` is never suppressed in JSON — structure can't annoy anyone, and the
  agent is the audience most likely to act on it.
- `--json` declares a **machine consumer**: the gate never prompts under
  `--json` — proceed under threshold or with `--yes`, otherwise exit 3 with
  the rerun line (and the JSON document still emitted before the stop).
  Implemented via `check_gate(..., machine=True)`.

### Turning hints off

Exactly **one** switch: the environment variable `ITEMEVAL_HINTS=off`. The
machine is the right scope because hint taste belongs to the person — not to
an invocation (hints never interfere with anything, so a flag has no job) and
not to the study (a config key would let one author silence hints for every
collaborator). Precedent: `NO_COLOR`. Hints keep **no memory**: a hint
re-fires whenever its trigger re-occurs, because the waste re-occurs too —
whoever made that trade-off deliberately sets the env var once. Switches are
append-only (removing one is a breaking change), so we start with this one and
add scopes only on demonstrated demand.

---

## The hint framework

**Format (fixed):**

```
hint: <observed fact, plain words> — learn more: <wiki-page#anchor>
```

**Rules:**

1. **Data-derived only.** A hint fires because of something observed in *this
   run* — a zero that should be nonzero, a count, a config/observation
   mismatch. Never a generic tip, never marketing.
2. **Budget: at most 2 per command**, chosen by priority; aggregated across
   conditions (one line for the run, not one per condition).
3. **Every hint has a stable code** (Law 7) and always appears in `--json`.
4. **A hint is an index, not a lecture**: one observed fact + one pointer.
   The explanation lives at the doc anchor and nowhere else.
5. Hints print **after** the summary, dim, on **stderr**. They never delay,
   block, or change anything (Law 2).

The framework lives in `src/itemeval/_hints.py` (the `Hint` model, the
stderr renderer with the budget of 2, `ITEMEVAL_HINTS`, and one pure
detector function per code); results carry `hints` as structured data.

**Catalog** (status: ✅ implemented / ☐ planned):

| Code | Fires when | Example line | Owning doc |
|---|---|---|---|
| ✅ `cache-zero-reads` | run repeats long text, scheduling on, but `cache_read = 0` | `hint: 116 calls repeated a shared prompt prefix but no provider cache discount engaged — learn more: Cost-Savings#two-gotchas` | Cost-Savings |
| ☐ `split-head-below-min` | split enabled, shared head below the provider's minimum (estimable pre-call) | `hint: split_rubric is on but the shared part (~3.9k tokens) is below Anthropic's 4k minimum — it will silently do nothing — learn more: Cost-Savings#two-gotchas` | Cost-Savings |
| ☐ `anthropic-openrouter-no-split` | Anthropic model via OpenRouter without split options (known zero discount) | `hint: anthropic via OpenRouter won't get cache discounts without split_rubric — learn more: Cost-Savings#prompt-packaging` | Cost-Savings |
| ✅ `empty-solutions` | N completions empty with no API error | `hint: 21 solutions are empty — completed without an API error but produced no gradable text [model_length×21] — learn more: Error-Handling#empty-completions` | Error-Handling |
| ☐ `dev-policy-at-scale` | config defines many items but `dev` policy runs 2 | `hint: ran 2 of 500 items (policy: dev) — learn more: Budget-and-Costs#policies` | Budget-and-Costs |
| ✅ `unpriced-models` | a model has no pricing entry | `hint: 1 model unpriced (x/y) — dollars missing, run unaffected — learn more: Budget-and-Costs#pricing-table` | Budget-and-Costs |
| ✅ `pilot-available` | money gate engages on a study with zero completed rows for the selected conditions | `hint: first run of this study — you can pilot cheaply first (--policy dev runs 2 items), then re-run at full scope; completed work is never re-paid — learn more: Cost-Savings#never-pay-twice` | Cost-Savings |

---

## Side-effect ledger (audit of current code, June 2026)

Every row is something the package does outside the study directory; Law 1
requires each to have an announcement line. This table is **normative**: a
feature adding a side effect must add a row here.

| Side effect | Where | Today | Required line (one each) |
|---|---|---|---|
| Resolve dataset revision | network → HF Hub (first run per dataset) | **compliant** — folded into the dataset line | folded into the dataset line below |
| Dataset download / reuse | `~/.cache/huggingface/datasets` (global) | **compliant** — one `dataset:` line per dataset on estimate/generate/grade/status; `datasets[]` in JSON | `dataset: cais/aime2025 @ 4a1b2c3 — downloaded 412 MB to HF cache (first use)` / `— reused from HF cache (pinned)` |
| Revision pin write | `dataset_locks.json` (study dir, but **decides future runs**) | **compliant** — pin clause printed on change only | `dataset: … — revision pinned in dataset_locks.json` (printed on change only) |
| Local response cache read/write | `~/Library/Caches/inspect_ai/generate` (macOS) / `~/.cache/inspect_ai` (Linux) | **compliant** — run-level summary line + `local_cache_rows`/`local_cache_dir` on results and per-condition reports | summary line: `12 calls answered from local cache ($0) — cache dir: <path>` |
| Pricing refresh | network → OpenRouter; writes `~/.cache/itemeval/pricing.json` | **compliant** — provenance line | (the model to copy) |
| Batch job creation | provider-side job | **compliant (best-effort)** — `batch: enabled (anthropic) — provider-side jobs created; resume with the same command` + `batch`/`batch_providers` on run results; inspect does not surface job ids (follow-up: per-job-id line if inspect's API ever exposes them — never fake an id) | `batch: submitted job <id> (anthropic) — collect with the same command later` |
| `export/` rewrite | study dir (disposable view) | **compliant** — `export: rewrote export/ — … (disposable view)` | keep; wording must say *rewritten* |
| Replacing existing result rows | solutions/gradings parquet | **compliant** — `this run replaces N existing rows (…)` in the pre-gate block; `rows_replaced` in estimate/run JSON | stated in the estimate and covered by the single money gate (Law 2) |

---

## Development checklist

Every feature (new or modified) must answer all nine. "No" answers are fine;
*unanswered* ones are not.

1. **Side effects** — does it touch network, global caches, locks, or
   provider-side state? → add/update a ledger row and its announcement line.
2. **Quotable summary** — what is the one self-contained line, with numbers,
   that states what it did?
3. **JSON parity** — does every fact in the text rendering have a stable
   field? (`--json` stdout still pure JSON?)
4. **Doc anchor** — which single wiki page owns the explanation?
5. **Hint candidate** — what silent failure mode does this feature introduce,
   and what coded hint would detect it from run data?
6. **Knob bucket** — interlock, design declaration, or optimization? If
   optimization: what is the path to retiring the knob into a default?
7. **Consent class** — does it spend or replace existing results? Then it is
   part of the *single* money gate — never a new prompt.
8. **Surface parity** — exposed in both CLI and Python? Python version never
   prompts and takes consent as a parameter?
9. **Stability** — any new exit code / JSON key / hint code is append-only and
   documented in the same change.

---

## Worked example: the dataset loader

**Today** — `itemeval estimate config.yaml` on a fresh machine resolves a
revision over the network, downloads 412 MB into `~/.cache/huggingface`, pins
`dataset_locks.json`, and prints **nothing**. Run by an agent, even HF's
progress bars vanish — the human learns none of it. Re-runs silently reuse the
cache; nothing ever says which revision the study is actually on.

**After applying the laws:**

```
$ itemeval estimate config.yaml          # first run
dataset: cais/aime2025 (split test) @ 4a1b2c3 — downloaded 412 MB to HF cache (first use); revision pinned in dataset_locks.json
pricing: merged (updated 2026-06-08, 2d old)
…

$ itemeval generate config.yaml          # any later run
dataset: cais/aime2025 (split test) @ 4a1b2c3 — reused from HF cache (pinned)
…
```

JSON parity:

```json
"datasets": [{
  "id": "cais/aime2025", "split": "test", "revision": "4a1b2c3…",
  "cache": "reused", "cache_dir": "~/.cache/huggingface/datasets",
  "pinned": true, "lock_path": "dataset_locks.json"
}]
```

Checklist pass: side effects → 3 ledger rows ✓; quotable line ✓ (one, with
size and revision); JSON parity ✓; doc anchor → Outputs-and-Schemas (locks) +
Configuration (revision) ✓; hint candidate → none needed (the provenance line
*is* the visibility) ✓; knob bucket → n/a (no knob added — visibility is not
optional) ✓; consent → none (no spend; the global cache write is announced,
not gated) ✓; surface parity → fields on `PreparedStudy` ✓; stability → new
JSON keys append-only ✓.
