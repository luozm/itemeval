# Implementation plan — metadata-in-templates (per-item metadata exposed to rubric/build templates)

**Status: IMPLEMENTED 2026-06-21.** Written 2026-06-21 against the current
template-render path; reconstructed as the design record from the shipped change
(built directly, never a BACKLOG entry). Read first:

1. `CLAUDE.md` — repo conventions.
2. `docs/UX-PATTERNS.md` — the no-silent-side-effects / render contract.
3. `docs/wiki/Configuration.md` — the template placeholder surface.

Scope: 1 workstream. **W1** expose `mapping.metadata` columns to templates.

---

## Context: the facts that decide the design

`mapping.metadata: [cols]` is already captured into `Item.metadata` (the HF and
local adapters both populate it). Two render sites build the placeholder dict
that `render_template` fills:

- `grade/_judge.py:_render_values(item, solution, rubric_text)` — the grade
  stage; fills `{input}`/`{solution}`/`{target}`/`{grading_scheme}`/`{id}` (plus
  `{rubric}` in the two-stage materialized path).
- `grade/_materialize.py:_render_values(item)` — the build/materialize stage;
  the same canonical fields minus `{solution}` (there is no candidate solution
  when a per-item rubric is frozen from the reference solution).

`render_template` replaces only known placeholders and leaves unknown braces
(LaTeX/JSON) untouched, so widening the dict cannot break an existing template.

Motivating case: a crossed study that carries one human grading scheme in
`grading_scheme` and a second frozen scheme in a `proofbench_scheme` metadata
column, each read by a different rubric — impossible while only the canonical
fields reach the template.

---

## W1 — expose mapping.metadata to templates

**Goal.** Every `mapping.metadata` column is rendered into rubric and build
templates as `{colname}`.

**Config / public surface.** No new knob — it purely widens the existing render
dict. No hint/gate; UX-PATTERNS unaffected.

**Mechanism.** Both `_render_values` functions seed the dict from `item.metadata`
first (`{k: "" if v is None else str(v)}` — `render_template` needs strings;
`None` → `""`), then `.update(...)` the canonical fields, so a **canonical name
always wins on collision** (a metadata column literally named `input` cannot
shadow the item input). A metadata-free item is a no-op (empty seed).

**UX contract.** No announcement/hint/gate. Additive: a template that never
referenced a metadata name renders identically; only a `{colname}` that
previously stayed literal now resolves (confirmed no shipped template/test
depended on that literal behaviour).

**Tests.** `tests/test_grade_metadata_render.py`: metadata exposed + stringified
(`points` → `"7"`, `None` → `""`) in the judge dict; canonical fields beat a
colliding metadata column; the materialize dict exposes metadata and still omits
`{solution}`; a metadata-free item yields exactly the canonical keys.

**Docs/CHANGELOG.** `[Unreleased]` `### Added` with `Closes:
metadata-in-templates`; `docs/wiki/Configuration.md` `mapping.metadata` line + a
"Per-item metadata in templates" bullet.

---

## Out of scope (explicitly)

- **Solver-prompt templates** — the generate-stage prompt is item-input-driven;
  metadata exposure was added only to the rubric/build (grade-side) renders where
  the second-scheme use case lives. Extend to the solver render if a study needs
  it.
- **Typed/structured placeholders** — values are stringified; JSON/number
  formatting of a metadata column is the template author's job.
- **Export of metadata columns as covariates** — tracked separately as
  `item-covariates-export` in BACKLOG.
