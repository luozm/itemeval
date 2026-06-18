# Implementation plan — rubric-materialization (two-stage generate-then-grade rubrics)

**Status: IMPLEMENTED 2026-06-18.** Written 2026-06-18 against inspect_ai 0.3.x
(pinned in `uv.lock`). This file was the working brief for the implementation
and remains as the design record (shipped folded into `grade` per the W5
same-change rule; `Closes: rubric-materialization`). Read order, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract (knob buckets, hint
   framework, the money gate is the only gate, JSON parity, append-only machine
   surface). Every workstream below states its bucket and interaction strength.
3. `DEVELOPMENT.md` — inspect_ai boundary rules (mandatory: this feature adds a
   model-calling stage). Wrap don't fork; pass through don't rename; flatten at
   the public API; inspect imports confined to task-builder / orchestrator /
   extension modules.
4. This file end-to-end before coding any part — the workstreams share design
   decisions (the condition-id payload in W2 is consumed by W3/W4).

Scope: 5 workstreams. **W1** config (`rubrics:` mapping) · **W2** grid (condition
id + resolved templates) · **W3** materialization stage + artifact store · **W4**
estimator term · **W5** UX surface (provenance line, hint, JSON, docs).

---

## Context: the facts that decide the design

### What a rubric is today (single-pass)

A rubric is a **template referenced by name** from `facets.rubric` (a
`list[str]`), resolved against the user's `rubrics_dir` or the packaged
`builtin:` set. There is **no `rubrics:` config mapping** today — only
`graders: dict[str, GraderSpec]` ([_config.py:371](../../src/itemeval/_config.py#L371)).

- [_config.py:270-297](../../src/itemeval/_config.py#L270) — `FacetsConfig.rubric:
  list[str]` (default `["builtin:standard"]`), uniqueness validated.
- [_templates.py:143-145](../../src/itemeval/_templates.py#L143) —
  `rubric_registry(config)` resolves a bare name → `<rubrics_dir>/<name>.md`,
  `builtin:<name>` → packaged. Every `Template` carries `text` + `sha256`
  ([_templates.py:25-37](../../src/itemeval/_templates.py#L25)).
- [design/_grid.py:126-176](../../src/itemeval/design/_grid.py#L126) —
  `expand_grade_grid` crosses `facets.grader × facets.rubric`. Per (grader,
  rubric) it `validate_template(template, {"input", "solution"})`, builds the
  judge condition payload `{kind, grader{...}, rubric:{name, hash}, format,
  layout?}`, hashes it into the condition id, and records `rubric_name` /
  `rubric_hash` on the `GradeCondition`.
- [grade/_judge.py:47-94](../../src/itemeval/grade/_judge.py#L47) — at judge
  time `_render_values(item, solution)` provides `{input, solution, target,
  grading_scheme, id}`; `build_judge_input` renders the rubric + appends
  `JUDGE_FORMAT_SUFFIX`; `build_judge_messages` splits at `{solution}` for the
  split-rubric cache layout; `judge_head_text` is the solution-independent
  shared head (rubric + problem + scheme + reference) used by the estimator.
- [grade/_run.py:333-346](../../src/itemeval/grade/_run.py#L333) — the
  orchestrator pulls `prep.rubric_templates[cond.rubric_name]` and calls
  `build_judge_task`, then `inspect_ai.eval`. **inspect imports already live
  here** (orchestrator) and in `_judge.py` (task builder) — the boundary rule
  permits adding the materializer task builder + eval call in these same modules.
- [_prepare.py:80-83,123-124](../../src/itemeval/_prepare.py#L80) — prepare
  resolves `rubric_templates = {name: rubrics.get(name) for name in
  config.facets.rubric}` (only when `config.facets.grader` is set) and stores it
  on `PreparedStudy.rubric_templates` ([_prepare.py:46](../../src/itemeval/_prepare.py#L46)).

### What two-stage materialization changes

A **materializing rubric** runs the judge in two stages:
1. **Materialize** (once per item, before grading): a *materializer* LLM renders
   a **build template** over the item's reference only (`{input, target,
   grading_scheme, id}` — **no `{solution}`**), producing a frozen per-item
   rubric text.
2. **Grade**: the existing judge call renders a **grade template** that has a
   new `{rubric}` placeholder, filled with the materialized text, plus the usual
   `{input, solution, target, grading_scheme, id}`.

Without `materialize`, grading is byte-for-byte unchanged (existing condition
ids must not move — same discipline as `split_prompt`/`composite-item-id`).

### The protocols this targets — **[verified 2026-06-18]**

The design was checked against the published two-stage protocols (re-verify if
revisiting; these are external facts):

- **ProofBench / ProofGrader** — Ma et al., ICLR 2026
  ([arXiv:2510.13888](https://arxiv.org/abs/2510.13888), §2.1 + Appendix J
  "Marking Scheme Generation Prompt"). "Annotation proceeds in two stages:
  marking scheme generation and model-generated proof grading." The scheme is
  produced **once per problem** by a generator LLM `M_MS` from *"the problem x
  and reference solutions S"*, output as exactly three sections (checkpoints
  with point values / zero-credit items / deductions), then **frozen and reused
  to grade every candidate proof**. Confirms: materialize from `target`
  (reference solution), grade-template gets the frozen rubric, one materialization
  reused across all solutions.
- **RefGrader** — [arXiv:2510.09021](https://arxiv.org/abs/2510.09021). A
  problem-specific rubric is *"derived once per problem from reference solutions
  and reused to grade candidate proofs"*; rubric induction is a **separate stage**
  from grading; inputs are problem + reference solution (+ optional marking
  scheme). Confirms the frozen-then-grade shape and the optional `grading_scheme`.
- **MathArena `usamo_2025` dataset**
  ([HF](https://huggingface.co/datasets/MathArena/usamo_2025)) — columns
  `problem` (→ `input`), `sample_solution` (→ `target`, the reference),
  `grading_scheme` (a **human-authored** structured rubric), `points`,
  `problem_idx`. Confirms the field mapping and that a dataset's `grading_scheme`
  is a *static, human* rubric — **distinct** from a materialized one. A study can
  contrast the two (grade against the dataset `grading_scheme` with no
  `materialize:` vs. a materializing rubric), and they compose as separate
  `facets.rubric` levels.

Two facts that shape the surface below: (a) the build/grade prompts (e.g. the
Appendix-J marking-scheme prompt) are **study-authored content** — itemeval ships
the *mechanism*, never a built-in materialize template (CLAUDE.md: no
study-specific rubric text in the package); (b) the generated rubric is a full
multi-section scheme, **not** a short answer, so the materializer needs its own
generous `max_tokens` (W1) — the judge's 512-token default would truncate it.

### Key design decisions (made here; alternatives in Out of scope)

- **Config shape: a top-level `rubrics: dict[str, RubricSpec]` mapping**,
  parallel to `graders:`. A `facets.rubric` name resolves via a new
  `config.rubric_spec(name)`: named in `rubrics:` → the spec; otherwise a bare
  template ref (today's path, unchanged). Mirrors `grader_spec()`
  ([_config.py:417-423](../../src/itemeval/_config.py#L417)).
- **Folded into `grade`, NOT a new `materialize` command.** Materialization is a
  *pre-pass inside `run_grade`*, costed by the grade estimator and covered by the
  **single existing grade money gate** (UX Law 2/4 — no new gate, no new verb).
  "Freeze then grade" is achieved by a content-addressed **artifact store** that
  caches each per-item rubric, so resume / re-grade reuse it at $0. This keeps
  the public-API + CLI snapshot ([tests/test_public_api_snapshot.py](../../tests/test_public_api_snapshot.py))
  green (no new command, no new top-level export).
- **Condition id carries the materialize *spec*, not per-item outputs.** The
  per-item materialized text varies by item; the condition spans all items. So
  the payload's `rubric.hash` becomes the **grade-template** hash and a new
  `materialize: {model, build_hash}` clause is added — a changed build template
  or materializer model changes the condition id (like changing the rubric does
  today). Per-item materialized content + its hash live in the artifact store
  (recorded provenance), never in the id.
- **Caching key for the artifact store:** `(materialize_id, item_id)` where
  `materialize_id = hash(build_template.sha256 + materializer_model)`. Item
  identity is the pinned `item_id` (dataset revision already frozen). Resumable
  via the standard keyed-upsert helper
  ([store/_base.py:36](../../src/itemeval/store/_base.py#L36)).
- **The materialized rubric is solution-independent → it lives in the
  split-rubric shared head** (caches like the rest of the head).

### Mechanism: content hashing + stores to reuse

- `Template.sha256` / `.hash12` ([_templates.py:34-37](../../src/itemeval/_templates.py#L34))
  — reuse for build + grade template hashes.
- Keyed parquet upsert ([store/_base.py:36-60](../../src/itemeval/store/_base.py#L36),
  `ITEMS_SCHEMA` pattern at [store/_items.py:13-43](../../src/itemeval/store/_items.py#L13))
  — the template to copy for the new `materialized_rubrics.parquet` store.
- `StudyPaths` ([store/_layout.py:6-50](../../src/itemeval/store/_layout.py#L6))
  — add a `materialized_rubrics` path property.
- Mock models ([_mockmodels.py:48-74](../../src/itemeval/_mockmodels.py#L48)) —
  `resolve_model(model, stage, ...)` dispatches `mock_judge_callable` for
  `stage="grade"`, else `mock_generate_callable` (free-form text). A `materialize`
  stage should map to free-form text (a mock rubric), so tests stay hermetic.
- Hints ([_hints.py:174-199](../../src/itemeval/_hints.py#L174)) —
  `detect_empty_solutions` is the exact pattern for a new
  `empty-materialized-rubrics` detector.

---

## W1 — Config: `rubrics:` mapping + `RubricSpec` / `MaterializeSpec`

**Goal.** Let a study declare a materializing rubric without disturbing plain
template refs. A new top-level `rubrics:` mapping keys named rubric specs;
`facets.rubric` continues to accept bare/`builtin:` names.

**Config / public surface (knob bucket: design declaration — it changes
condition ids; always explicit, never auto-flipped).**

```yaml
# sketch
rubrics:
  checkpoint:
    materialize:
      model: openrouter/openai/gpt-5.4
      template: checkpoint.build       # per-item rubric from {input,target,grading_scheme,id}
    grade_template: checkpoint.grade   # grade template; receives {rubric} + {solution} + ...
facets:
  rubric: [checkpoint, builtin:standard]   # named spec + a plain ref, crossed as usual
```

This is the BACKLOG sketch's exact shape (`grade_template` sibling of
`materialize`, `materialize.template` for the build template) — no BACKLOG
correction needed, and the two distinct templates never share a `template:` key.

- New `MaterializeSpec(model: str, template: str, max_tokens: int = 2048,
  reasoning_effort: ReasoningEffort | None = None)` and
  `RubricSpec(grade_template: str, materialize: MaterializeSpec)` — both required;
  `extra="forbid"`. `max_tokens`/`reasoning_effort` mirror `GraderSpec`
  ([_config.py:300-315](../../src/itemeval/_config.py#L300)): a generated marking
  scheme is a multi-section document, so the default is `2048` (the judge's 512
  fill-in would truncate it), not a new bucket. A `rubrics:` entry exists **only**
  to materialize (a plain rubric stays a bare `facets.rubric` ref, unchanged).
  `grade_template` / `materialize.template` are **template references**
  (bare / `builtin:` / local), resolved later via the rubric registry. itemeval
  ships **no** built-in materialize template — the build prompt is study content.
- `ExperimentConfig.rubrics: dict[str, RubricSpec] = {}` (after `graders`,
  [_config.py:371](../../src/itemeval/_config.py#L371)).
- `ExperimentConfig.rubric_spec(name) -> RubricSpec | None` — returns the spec if
  named, else `None` (bare ref). Mirrors `grader_spec`.
- Validation (at grid expansion, where templates resolve — not load, so the
  README sketch still validates): a `materialize.template` must **not** contain
  `{solution}` (no solution exists yet) and must contain `{input}`; a
  materializing rubric's `grade_template` must contain `{rubric}` **and**
  `{solution}`. A `facets.rubric` name that is neither in `rubrics:` nor a
  resolvable template errors with both option sets named (capability-legibility
  spirit — teach the valid set).

**Mechanism.** Pure pydantic + a resolver method; no inspect, no I/O.

**UX contract.** No new output here (config only). Knob bucket = design
declaration. No consent (no spend at load). Surface parity: same YAML drives CLI
and Python.

**Tests.** `tests/test_config.py` — `rubrics:` parses; `extra="forbid"` rejects
typos; `rubric_spec` returns spec vs None. Template-placeholder validation tested
in W2 (grid) since it needs resolved templates.

---

## W2 — Grid: condition id + resolved templates on `PreparedStudy`

**Goal.** A materializing rubric produces a distinct, content-stable condition
id; non-materializing conditions keep byte-identical ids. Prepare resolves both
the grade and build templates so the runner and estimator never re-read disk.

**Config / public surface.** No new knob. New **append-only** fields on
`GradeCondition` ([design/_grid.py:59-74](../../src/itemeval/design/_grid.py#L59)):
`materialize_model: str | None`, `build_template_hash: str | None` (12 hex),
`grade_template_hash` reuses the existing `rubric_hash` slot (now = grade-template
hash for materializing conditions; unchanged for plain ones). New
`PreparedStudy.build_templates: dict[str, Template]` (rubric_name → build
template; only materializing entries) alongside `rubric_templates`
([_prepare.py:46](../../src/itemeval/_prepare.py#L46)); for a materializing rubric,
`rubric_templates[name]` is the **grade** template.

**Mechanism.**
- [design/_grid.py:126-176](../../src/itemeval/design/_grid.py#L126)
  `expand_grade_grid` takes a way to look up the spec per rubric name (pass
  `config` + the resolved template dicts). For each rubric:
  - plain ref → today's payload + `validate_template(t, {"input","solution"})`.
  - materializing spec → `validate_template(grade_t, {"input","solution",
    "rubric"})`; `validate_template(build_t, {"input"})` + assert no `{solution}`;
    payload gains `"materialize": {"model": spec.materialize.model, "build_hash":
    build_t.hash12}` and `rubric.hash` = grade-template hash. Add it **only when
    materializing** so existing ids are unchanged (same pattern as the
    `layout: split` clause at [design/_grid.py:156-159](../../src/itemeval/design/_grid.py#L156)).
- [_prepare.py:80-83](../../src/itemeval/_prepare.py#L80) — resolve build+grade
  templates through the existing `rubric_registry`; populate `build_templates`
  and the grade-template entries in `rubric_templates`.

**UX contract.** No new output. The new `GradeCondition` fields are internal
(not in the public API surface). Stability: condition-id change is *intended* and
gated by the materialize spec; the `test_condition_ids.py` golden values for
non-materializing configs must stay unchanged (regression guard).

**Tests.** `tests/test_grid.py` / `tests/test_condition_ids.py` — a plain rubric
keeps its exact id (golden); a materializing rubric yields a new id; changing the
build template or materializer model changes the id; missing `{rubric}` /
`{solution}` / stray `{solution}` in the build template each raise.

---

## W3 — Materialization stage (task builder + orchestrator pre-pass) + artifact store

**Goal.** Before judging, produce + freeze each item's rubric with the
materializer model, cache it content-addressed, and feed it into the judge call.

**Config / public surface.** New store file `materialized_rubrics.parquet`
(inside the study dir — disposable/rebuildable, like `items.parquet`). New
**append-only** `GradeResult` fields ([grade/_run.py:61-97](../../src/itemeval/grade/_run.py#L61)):
`materialized_rubrics: int` (newly materialized this run), `materialized_reused:
int`, `materialize_usd: float`, `materialize_empty: int`.

**Mechanism.**
- **`store/_materialized.py`** (new) — `MATERIALIZED_SCHEMA` keyed by
  `["materialize_id", "item_id"]`: columns `materialize_id, rubric_name,
  item_id, materializer_model, build_template_hash, rubric_text, rubric_hash,
  usd, input_tokens, output_tokens, error, run_id, created_at`. `read_materialized`
  / `upsert_materialized` / `pending_materializations(items, existing,
  materialize_id)` — the `_solutions.epochs_to_run` / `_gradings.pending_solutions`
  analogue (reuse [store/_base.py](../../src/itemeval/store/_base.py)).
- **`grade/_materialize.py`** (new, task builder — inspect import allowed here):
  - `materialize_id(build_template, model) -> str` = `sha256(build.sha256 +
    model)[:12]`.
  - `build_materialize_input(item, build_template) -> str` — render
    `{input,target,grading_scheme,id}` (no suffix; free-form rubric text out).
  - `build_materialize_task(pending_items, build_template, spec, study, cache,
    batch)` — `MemoryDataset` of one `Sample` per item (id = item_id),
    `scorer=None`, `temperature=0.0` (deterministic — the artifact is *frozen*,
    so the draw is pinned like the judge's temp-0), `max_tokens =
    spec.max_tokens`, `reasoning_effort = spec.reasoning_effort`. Mirror
    [build_judge_task](../../src/itemeval/grade/_judge.py#L101); cache scheduling
    not needed (one call per item, no shared-prefix fan-out) — keep it simple.
- **`grade/_run.py`** — a `_materialize_rubrics(prep, selected, run_id, ...)`
  pre-pass that runs **once before the condition loop**
  ([grade/_run.py:304](../../src/itemeval/grade/_run.py#L304)): collect the unique
  `(materialize_id, build_template, model, item)` set across selected
  materializing conditions; subtract already-stored rows; for the remainder run
  `inspect_ai.eval` per (rubric, model), parse the completion as the rubric text,
  `upsert_materialized`. Build a `{(rubric_name, item_id) -> rubric_text}` map
  (stored + fresh) handed to the judge-task builder. Empty completions (no error,
  blank text) are counted (`materialize_empty`) and the item is still graded with
  an empty `{rubric}` — surfaced, never silently dropped (mirrors the empty-
  solution channel).
- **`grade/_judge.py`** — `build_judge_input` / `build_judge_messages` /
  `judge_head_text` / `_render_values` gain an optional `rubric_text: str | None`
  that fills `{rubric}`. For split-rubric, `{rubric}` sits before `{solution}`,
  so it renders into the shared head automatically (no signature change to the
  split point). `build_judge_task` receives the per-item rubric map and passes
  the right text per sample.
- **`_mockmodels.py`** — `resolve_model(..., stage="materialize")` →
  `mock_generate_callable` (free-form mock rubric text), so the pipeline runs
  free + deterministic end-to-end.

**Inspect boundary.** New eval call lives in `grade/_run.py` (orchestrator) using
a task from `grade/_materialize.py` (task builder) — both already-sanctioned
inspect-importing modules. Results flatten to the parquet store + `GradeResult`
(pydantic); no inspect types cross the public API.

**UX contract.**
- **Side effects (Law 1):** the materializer is a *paid model call to a different
  model* and writes a store **inside the study dir** — store writes inside the
  study dir are normal operation (no ledger row needed), but the spend is real,
  so it must appear in the estimate (W4) and ride the **single grade money gate**
  (Law 2 — no second prompt). Resume reuse is inside the study dir (like
  solutions resume), not a global-cache read → no Law-1 announcement, but the
  summary states it (W5).
- **Consent (Law 3/7):** spends → part of the existing grade gate; Python
  `run_grade(max_usd=)` already raises before any call, and the materialize
  pre-pass is inside `run_grade`, so the cap covers it.

**Tests.** `tests/test_grade_run.py` (or a new `tests/test_materialize.py`),
all on `mockllm/*` (zero network):
- materialize → grade end-to-end writes `materialized_rubrics.parquet`, the judge
  prompt contains the materialized text, gradings written.
- **Freeze/reuse:** a second `grade` run materializes 0 (all reused), `materialize_usd == 0`.
- changing the build template re-materializes (new `materialize_id`).
- empty mock rubric → `materialize_empty` counted, grading still proceeds.
- a non-materializing study is byte-identical (no new store file written).

---

## W4 — Estimator: materialization cost term

**Goal.** The grade estimate (and the pre-gate projection line) includes the
materializer calls, so the gate the user confirms covers the true grade spend.

**Config / public surface.** No new knob. The materialization sub-cost folds
into the existing grade `StageEstimate`
([budget/_estimator.py:89-133](../../src/itemeval/budget/_estimator.py#L89)); the
materializer model is surfaced for `unpriced_models` detection. Represent each
materializing rubric's materialize calls as their own `ConditionEstimate` row
(stage `"grade"`, `model = materializer`, slug `materialize:<rubric>`) so the
cost is legible and unpriced-model detection works unchanged.

**Mechanism.** In the grade loop ([budget/_estimator.py:727](../../src/itemeval/budget/_estimator.py#L727)),
for a materializing condition add, **once per (rubric, materializer)** (not per
grade condition — materialization is shared):
- ceiling: `n_items` calls; input = `estimate_tokens(build template rendered per
  item)`; output = `spec.materialize.max_tokens` (default 2048 — a marking scheme
  is a multi-section document; the judge's 512 default would under-cost it).
- expected: calibrate the materializer output mean from
  `materialized_rubrics.parquet` via the existing `_mean_resolver`
  ([budget/_estimator.py:183](../../src/itemeval/budget/_estimator.py#L183)) where
  rows exist, else ceiling (cold start).
- delta/resume: subtract items already in the store (only un-materialized items
  cost), the `pending_solutions`-style predicate from W3.
Guard against double-counting when several grade conditions share one
materializing rubric (dedup by `materialize_id`).

**UX contract.** No new gate. The grade projection line already prints
`projected grade cost: $X` ([cli.py:380-384](../../src/itemeval/cli.py#L380)); the
materialize cost is inside it. `estimate-is-ceiling` / `unpriced-models` hints
extend to the materializer model for free via the ConditionEstimate rows.

**Tests.** `tests/test_estimator.py` — a materializing config's grade estimate >
the same config without materialize by the materializer term; an unpriced
materializer lands in `grade.unpriced_models`; after the store is populated the
remaining materialize cost drops to 0 (resume-aware).

---

## W5 — UX surface: provenance line, hint, JSON parity, manifest, docs

**Goal.** Discharge the UX-PATTERNS checklist: one quotable summary line, JSON
parity, a doc anchor, a coded hint for the silent failure mode, and the
same-change paperwork.

**Config / public surface.** `GradeResult` fields from W3 are the JSON parity.

**Mechanism / UX contract.**
- **Quotable summary line (Law 8)** in `_run_stage` grade branch
  ([cli.py:440-460](../../src/itemeval/cli.py#L440)), printed when any
  materialization ran or was reused:
  `materialized: 40 rubrics (openrouter/openai/gpt-5.4) · $0.12 · 0 reused`
  (resume: `· 40 reused $0.00`).
- **Hint (Law 2, framework in `_hints.py`):** new coded
  `empty-materialized-rubrics` (append-only) — fires when N materialized rubrics
  came back empty (no error, no text), so grading ran against a blank `{rubric}`.
  Owning doc: `Error-Handling#empty-materialized-rubrics`. Add the catalog row to
  UX-PATTERNS.md. Mirror `detect_empty_solutions`
  ([_hints.py:174](../../src/itemeval/_hints.py#L174)); budget rule (≤2/command)
  unchanged.
- **JSON parity (Law 6):** the new `GradeResult` fields are emitted by
  `--json` already (it dumps the model); no extra wiring.
- **Manifest:** the materialize spec flows into the run manifest automatically —
  the config echo carries `rubrics:` and `grid_grade[].payload` carries the
  `materialize` clause ([_manifest.py:179-184](../../src/itemeval/_manifest.py#L179)).
  No manifest schema bump needed.
- **Snapshot reproducibility:** `_write_snapshot` already copies
  `dataset_locks.json` / `model_locks.json`
  ([store/_export.py:187-190](../../src/itemeval/store/_export.py#L187)); add
  `materialized_rubrics.parquet` (when present) so a snapshot freezes the exact
  per-item rubrics the gradings were produced against — the frozen artifact is
  the reproducibility record (the condition id only pins the *spec*).
- **Knob bucket (Law 5):** `materialize` = design declaration; documented as
  such. No optimization knob, no auto-flip.

**Docs / CHANGELOG (same commit as the behavior).**
- `CHANGELOG.md` `[Unreleased]` → `### Added` entry with a `Closes:
  rubric-materialization` trailer.
- **Remove** the `rubric-materialization` section from `docs/BACKLOG.md`
  ([docs/BACKLOG.md:479-518](../BACKLOG.md#L479)) — design record stays in this
  plan (archived).
- `ROADMAP.md`: `rubric-materialization` is named in the 0.4 "being shaped" prose
  ([ROADMAP.md:64-68](../../ROADMAP.md#L64)). It ships early (in `[Unreleased]`),
  so move it to the **Already landed** line in the 0.3 block (a shipped key may
  only appear there — `tests/test_docs_consistency.py` enforces it).
- Wiki: `docs/wiki/Configuration.md` gains a "Two-stage (materialized) rubrics"
  section (the owning anchor); `docs/wiki/Error-Handling.md` gains the
  `#empty-materialized-rubrics` anchor; `docs/wiki/Outputs-and-Schemas.md` notes
  the `materialized_rubrics.parquet` store.
- UX-PATTERNS.md: add the hint-catalog row; no new side-effect ledger row (store
  writes are inside the study dir).

**Tests.** `tests/test_hints.py` — `empty-materialized-rubrics` fires/suppresses
correctly. `tests/test_docs_consistency.py` stays green after the BACKLOG/ROADMAP
move. `tests/test_public_api_snapshot.py` stays green (no new export/command) —
if W3 adds a public export, bump the golden set in the same commit.

---

## Sequencing (canonical)

1. **W1** config models + `rubric_spec` (no dependencies).
2. **W2** grid id + prepare template resolution (consumes W1's spec).
3. **W3** materialization stage + store + judge `{rubric}` plumbing + mock stage
   (consumes W2's resolved templates + condition fields).
4. **W4** estimator term (consumes W2's condition fields + W3's store for
   calibration/resume).
5. **W5** UX surface + docs + CHANGELOG + BACKLOG removal (consumes W3's
   `GradeResult` fields).

One conventional commit per workstream (`feat:` for W1–W5; W5 carries the
same-change paperwork). After each step: `make check` (lint + fast tests),
CHANGELOG and normative doc tables updated in the same commit.

## Out of scope (explicitly, to prevent creep)

- **A standalone `itemeval materialize` command / `run_materialize` export.**
  Folded into `grade` (above); a separate verb adds CLI/API surface and a second
  gate for no protocol gain — the artifact store already gives "freeze then
  grade." Revisit only on demonstrated demand.
- **Cache-scheduling the materializer calls.** One call per item (no
  same-prefix fan-out), so the warm-then-fan-out machinery buys nothing here;
  keep `build_materialize_task` plain.
- **Exporting per-item materialized rubrics into the long table.** The artifact
  store holds them as provenance; widening `gradings_long` with per-item rubric
  columns overlaps `item-covariates-export` (BACKLOG) — do it there.
- **Judge replications / materializer replications (rubric-generation
  variance).** `judge-replication` (BACKLOG) owns grade-side epochs;
  materialization is a single frozen draw (temperature 0). Studying how much the
  *generated rubric itself* varies is a deliberate non-goal of v1.
- **A built-in materialize/build template.** itemeval ships the mechanism only;
  the marking-scheme prompt (ProofBench Appendix-J-style, RefGrader's) is
  study-authored local template content (CLAUDE.md: no study-specific rubric text
  in the package).
- **Reconciling a stale `materialized_rubrics.parquet` when item content changes
  under a fixed id.** Dataset revisions are pinned at first run; a forced item
  change is the same stale-store class as the existing `items.parquet` issue
  (`docs/KNOWN-ISSUES.md`) — not solved here.
