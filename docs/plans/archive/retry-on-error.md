# Implementation plan — retry-on-error (configurable sample-level retry / fail-fast pass)

**Status: IMPLEMENTED 2026-06-23.** Shipped on `feat/retry-on-error` (CHANGELOG
`Closes: retry-on-error`). Written against inspect_ai 0.3.239 (pinned in
`uv.lock`). This file is now the design record; the context below is what the
implementing session worked from (config field + identity pop + generate-stage
threading + a pass-through test + the digest-stability guard and validation tests
+ the CHANGELOG entry and BACKLOG removal). The reading list it carried:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract (knob buckets, no new gates,
   JSON parity). The single new knob's bucket + interaction strength are below.
3. `DEVELOPMENT.md` — inspect_ai boundary rules (**pass through, don't rename** is
   load-bearing) and the study-facing schema-evolution gate (this change is
   *additive-optional*, discharged by a digest-stability guard test — gate item 1).
4. `docs/plans/archive/request-timeout.md` — the near-exact precedent: a
   non-identity, opt-in, `default=None`, pure-pass-through execution knob. This
   plan mirrors it.
5. This file end-to-end before coding the remainder.

Scope: one small feature, generate stage only. Effort S.

**Decision (settled 2026-06-23): opt-in, `default=None`, pure pass-through, no
auto-applied value.** The value is threaded verbatim to inspect's
`eval(retry_on_error=...)` and is `None` (today's behavior — itemeval's built-in
`1`) unless a study sets it. Rationale: the honest, non-surprising default for a
measurement tool. Generate stage only; grade keeps its built-in `1`.

---

## Context: the facts that decide the design

### What's missing
itemeval always called inspect's `eval(..., retry_on_error=1)` with a **hard-coded
`1`** (`generate/_run.py:260`, and `grade/_run.py:390`) — one sample-level retry,
chosen for resilience. There was no way for a study to change it. Two real needs:
- **Fail-fast "fast pass"** — when `attempt_timeout` is set, a stalled cell is
  retried once *and times out again* before it holes, so the wall-clock cost of a
  stuck cell is ~2× the timeout. A study doing a quick first pass wants `0`: hole
  after a single attempt, fill the holes later with a longer timeout / fresh run.
- **More resilience** — a flaky provider window may warrant `2`+.

### The right inspect knob (verified in installed source, 0.3.239)
`inspect_ai.eval(retry_on_error: int | None = None)` — docstring: "Number of times
to retry samples if they encounter errors (**by default, no retries occur**)."
Internally normalized `None → 0` (`inspect_ai/.../run.py`:
`retry_on_error=config.retry_on_error or 0`). This is the **sample-level** retry —
distinct from `GenerateConfig.max_retries`, which retries *within a single model
call*. itemeval re-runs the whole sample; the two layers compose.

**itemeval's "built-in 1" is itemeval's choice, not inspect's default.** Because
inspect's own default means *no* retries, itemeval must keep passing an explicit
value to preserve current behavior. The wrapper therefore maps:
- config `None`  → pass `1`  (itemeval's resilient default — unchanged)
- config `0`     → pass `0`  (single attempt — true fail-fast)
- config `N`     → pass `N`

via `retry_on_error=1 if retry_on_error is None else retry_on_error`
(`generate/_run.py:260`). itemeval never relies on inspect's `None` default.

### Interaction with `attempt_timeout` (informs the docs)
With `attempt_timeout` set and `retry_on_error=0`, a stalled cell holes after **one**
`attempt_timeout` instead of two (no sample retry). This is the headline "fast pass"
story. No code interaction — both are independent pass-throughs.

### Identity: the field must NOT move any id (load-bearing — verified)
`retry_on_error` is a pure execution/robustness knob; it must not change condition
ids or the `experiment_id` digest, or it would re-key existing studies.
- A new `retry_on_error: int | None = None` field serializes into
  `config.model_dump(mode="json")` as `"retry_on_error": null`, which would
  otherwise change **every** study's digest (`_identity.normalized_config_digest`
  dumps the whole config, then pops non-identity keys).
- **Fix (already applied, verified sufficient):** `"retry_on_error"` is added to
  `_NON_IDENTITY_SOLVERS` (`_identity.py:46`) so it is popped before hashing.
  Empirically confirmed 2026-06-23: a `SolversConfig` dump with vs without
  `retry_on_error` set differs *only* by that key, and is byte-identical after the
  non-identity pop → digest unchanged for everyone.
- Condition ids are built from explicit `gen_params`/payload fields, never the whole
  solvers dict, and `retry_on_error` is threaded as a separate argument (never via
  `gen_params`), so condition ids are byte-identical too.

### Other surfaces — unaffected (verified)
- `model_locks.json` pins only the `solvers.sample` spec; `retry_on_error` is a
  sibling, never seen by the lock compare. No lock work.
- The run manifest echoes the validated config, so the value is captured for
  provenance automatically — no manifest-schema work.
- Public-API snapshot (`tests/test_public_api_snapshot.py`) covers only
  `itemeval.__all__` + CLI subcommands; config-model fields aren't snapshotted and
  no CLI command is added → stays green (confirmed 2026-06-23).
- `tests/test_docs_consistency.py` doesn't enumerate config knobs against the wiki,
  so the wiki line is good practice, not a gated requirement (confirmed green).

### Schema-evolution gate (DEVELOPMENT.md)
Additive optional field, default `None`, compatible by construction. **Discharged by
a digest-stability guard test** (gate item 1) — frozen below. No `Study migration`
note needed (additive).

---

## W1 — `solvers.retry_on_error` pass-through (generate stage)

**Goal.** A study can choose the sample-level retry count for `generate`: unset =
itemeval's resilient built-in `1`; `0` = a fail-fast single-attempt pass (hole a
slow cell after one `attempt_timeout`, fill later); `N` = more resilience. No
behavior change for studies that don't set it.

**Config / public surface.**
- `SolversConfig.retry_on_error: int | None = Field(default=None, ge=0)`
  (`_config.py:245`) — generate stage. *(Note `ge=0`, not `ge=1` like
  `max_retries`: `0` is the meaningful fail-fast value.)*
- **Knob bucket: optimization / robustness** (UX-PATTERNS Law 5) — sensible default
  that trends invisible, explicit override. It does *not* spend extra, does *not*
  change condition ids, is *not* a gate.
- **No new CLI flag, no new exit code, no new JSON top-level key.** The value rides
  the config echo already in the manifest and `--json` config dumps.

**Mechanism (file:line — already implemented).**
1. `_config.py:245` — the field (validated `ge=0`) with the doc comment.
2. `_identity.py:46` — `"retry_on_error"` added to `_NON_IDENTITY_SOLVERS` (keeps
   the digest stable; see Context — mandatory **and** sufficient).
3. `generate/_run.py:225` — `run_condition_evals(...)` gains
   `retry_on_error: int | None = None`; at the eval call (`:260`) maps
   `1 if retry_on_error is None else retry_on_error`.
4. `generate/_run.py` — the three generate call sites pass
   `retry_on_error=prep.config.solvers.retry_on_error`: main eval (`:1198`),
   reroute (`:785`), fill-holes (`:903`).

**Grade is intentionally excluded.** `grade/_run.py:749`'s `run_condition_evals`
call omits the arg (→ `None` → `1`), and grade's other direct eval
(`grade/_run.py:390`) hard-codes `retry_on_error=1`. Grade keeps the built-in `1`
(documented in the `run_condition_evals` docstring). Add a grade knob only on demand.

**UX contract.**
- **Side effects (Q1):** none new — no network/cache/lock/provider-side state. No
  ledger row.
- **Quotable summary (Q2/Q8):** none required (a robustness knob, not an action;
  `None` by default → nothing to announce). The value is in the manifest config echo.
- **JSON parity (Q3):** config echo only; no new fact invented in prose.
- **Doc anchor (Q4):** `Configuration.md` owns the knob (the `solvers.retry_on_error`
  comment — already added). Optional cross-ref from `Error-Handling.md`
  (attempt_timeout × fast pass) — not required; the Configuration comment is
  self-contained.
- **Hint candidate (Q5):** `retry_on_error: 0` deliberately produces more holes; a
  too-aggressive fail-fass pass could surprise. The existing hole/soft-failure
  surfacing already shows the symptom (`cells_filled`/reroute residue) — no new
  coded hint. Recorded, not built (don't over-engineer).
- **Knob bucket (Q6):** optimization/robustness; path to retiring = none needed (it
  exposes a deliberate trade-off, like `attempt_timeout`).
- **Consent (Q7):** none — no spend, no row replacement.
- **Surface parity (Q8):** config field, read identically by CLI and Python.
- **Stability (Q9):** additive optional config field; no new exit code / JSON / hint
  code.

**Tests (hermetic, no API).**
- `tests/test_generate_run.py::test_retry_on_error_threads_to_eval` — **DONE** in
  the working tree: monkeypatches `inspect_ai.eval`, asserts the `retry_on_error`
  kwarg is `1` for `None`, `0` for `0`, `2` for `2`.
- `tests/test_identity.py` — **TODO (guard test, discharges the schema gate):** a
  config with `solvers.retry_on_error` set vs unset produces the **same**
  `normalized_config_digest` and the same `experiment_id`. (Mirrors
  `test_normalized_digest_drops_provider_routing`.)
- `tests/test_config.py` — **TODO:** `solvers.retry_on_error` accepts `0` and a
  positive int; rejects a negative (`ge=0`).

**Docs/CHANGELOG (same commit as the behavior).**
- `CHANGELOG.md` `[Unreleased] → Added`: the knob, generate-stage pass-through,
  None→built-in-1 / 0→fail-fast semantics, non-identity, grade-excluded; trailer
  `Closes: retry-on-error`. **TODO.**
- `docs/BACKLOG.md`: **remove** the `retry-on-error` section in the shipping commit
  (its design record lives on in this plan once archived). **TODO.**
- `ROADMAP.md`: `retry-on-error` is **not** named there as a future candidate, so
  no move is required by `test_docs_consistency.py`; leave it untouched (the 0.3
  `**Already landed**` line is already exhaustive — appending is optional, skipped).
- Wiki: `Configuration.md` `solvers.retry_on_error` comment — **DONE** in the
  working tree.
- `docs/UX-PATTERNS.md`: no ledger row (no side effect) and no hint row — no
  documented surface changes.

---

## Sequencing (canonical)

The implementation (config + identity + threading + the pass-through test) is
already in the working tree. Remaining, in one `feat:` commit on `feat/retry-on-error`:
add the two TODO tests (digest guard + validation), the CHANGELOG entry, and remove
the BACKLOG section — one atomic surface. Then `make check` (lint + fast tests).
Then archive this plan (`IMPLEMENTED <date>`, `git mv` to `docs/plans/archive/`, fix
inbound links).

After each step: `make check`; CHANGELOG and normative doc tables updated in the
same commit.

## Out of scope (explicitly, to prevent creep)

- **A grade-stage `retry_on_error` knob.** Grade keeps the built-in `1`. Add only on
  demand; the symmetry isn't needed yet (grade is cheaper to re-run).
- **`MaterializeSpec` / materializer retry.** One call per item, niche surface; left
  at the built-in behavior.
- **A coded fail-fast/holes hint.** Recorded under W1 Q5; the existing
  hole/soft-failure surfacing covers the symptom. Not built.
- **Bounding within-call retries** (`max_retries`) — a separate, already-shipped
  knob (`request-timeout`'s sibling). This feature is the *sample*-level layer only.
