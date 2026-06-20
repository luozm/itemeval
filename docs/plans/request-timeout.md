# Implementation plan — request-timeout (bound a stalled model attempt)

**Status: IN PROGRESS (started 2026-06-20).** Written against inspect_ai 0.3.239
(pinned in `uv.lock`) — re-verify the pinned facts below if that moved. This file
is the working brief for a fresh implementation session: it carries all the
context that session needs. Read these first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract (knob buckets, no new gates,
   JSON parity). The single new knob's bucket + interaction strength are below.
3. `DEVELOPMENT.md` — inspect_ai boundary rules (**pass through, don't rename** is
   load-bearing here) and the study-facing schema-evolution gate (this change is
   *additive-optional*, discharged by a digest-stability guard test).
4. `local/run-ux-reorder-plan.md` — the cross-cutting run-UX tracker. This is item
   **H** in build order `P0 ✅ → S ✅ → R ✅ → H → A → D → C → G`. **H ships before
   D (`preflight-check`)**, so the terminal-vs-transient classifier the BACKLOG
   says this feature will "consume" does **not exist yet** — see Out of scope.
5. This file end-to-end before coding.

Scope: one small feature, two stages (generate + grade). Effort S.

**Decision (settled 2026-06-20): Option A — opt-in, `default=None`, pure
pass-through.** No auto-applied default, no batch gate, no helper — the value is
threaded verbatim to inspect's `GenerateConfig.attempt_timeout` and is `None`
(today's behavior) unless a study sets it. Discoverability (scaffold comment +
Error-Handling + FAQ) replaces a default. Rationale: the honest, non-surprising
default for a measurement tool — a value picked without data would risk silently
cutting a slow-but-valid reasoning stream, and parallel-conditions (shipped)
already softens the one-stalled-condition hostage problem.

---

## Context: the facts that decide the design

### What's broken
Neither itemeval nor inspect sets any request timeout. In the per-condition
`GenerateConfig` itemeval builds (`generate/_task.py:87`, `grade/_judge.py:156`,
`grade/_materialize.py:59`), `attempt_timeout` and `timeout` are unset → inspect's
defaults (`None`, `_generate_config.py:198,201`) → a degraded/stalled stream runs
**unbounded**. parallel-conditions (shipped) means other conditions keep going,
but each *sample* of the stuck condition can hang forever with no upper bound.

### The right inspect knob (verified in installed source)
`GenerateConfig` has two timeout fields (`_generate_config.py`):
- `timeout` (`:198`) — "Timeout for an entire request (including retries)." Bounds
  total wall-clock but does **not** retry.
- `attempt_timeout` (`:201`) — "Timeout for any given attempt (if exceeded, will
  abandon attempt and **retry** according to max_retries)."

`attempt_timeout` is the one the design wants: a stalled attempt is abandoned and
**retried**, and through OpenRouter a retry can land on a healthier upstream route
— exactly the flaky-routing recovery the run-UX cluster is about. We expose
`attempt_timeout` and **pass inspect's name through unchanged** (DEVELOPMENT.md
boundary rule). *(This corrects the BACKLOG/tracker's `solvers.timeout` spelling,
which would be a rename — boundary-rule violation. The BACKLOG design sketch
itself says "pass inspect's `attempt_timeout` through unchanged"; the knob name is
brought in line with that. The BACKLOG fix is committed on `main` in step 3.)*

How inspect applies it (`_model.py:1124-1160`): `anyio.move_on_after(attempt_timeout)`
wraps `self.api.generate(...)`; on expiry it raises `AttemptTimeoutError`, which
flows into inspect's existing retry. We set the value; inspect owns the mechanism
(wrap, don't fork).

### Batch interaction (verified) — informs the docs, not the code
The `attempt_timeout` context wraps `self.api.generate()` **directly**
(`_model.py:1149-1155`). Under a **batch** plan that single call *is* the batch
submit-and-poll, which legitimately runs minutes→hours, so a non-None
`attempt_timeout` would abandon a healthy batch job. Under the settled design
(opt-in; see Decision) the value is `None` unless a study sets it, so this is **not
a code concern** — there is no auto-applied default to suppress. It is a
documented caveat: a study that *explicitly* sets `attempt_timeout` and also runs
batch is told (Error-Handling wiki) the value bounds the batch poll too, so it
should leave it unset for batch runs. The value is honored verbatim everywhere
(the user's explicit choice).

### Identity: the new field must NOT move any id (load-bearing)
`attempt_timeout` is a pure execution/robustness knob — it must not change
condition ids or the `experiment_id` digest, or it would re-key existing studies.
Two facts make this clean:
- **Condition ids** are built from *explicit* fields, never the whole
  solvers/grader dict: generate uses `gen_params` (`design/_grid.py:34-42,123`) —
  which lists only sampling params — plus the `payload` (`:108-125`); grade builds
  the grader sub-dict field-by-field via `drop_none` (`design/_grid.py:167-174`).
  So **as long as `attempt_timeout` is not added to `GenParams`/`resolve_gen_params`
  and not added to the grade payload, ids are byte-identical.** It is threaded to
  the builders as a *separate argument* (like `batch`/`cache_prompt`), never via
  `gen_params`.
- **The `experiment_id` digest** (`_identity.normalized_config_digest`,
  `_identity.py:44-61`) dumps the whole validated config to JSON, then pops
  non-identity keys. A new `attempt_timeout: int | None = None` field would
  otherwise serialize as `"attempt_timeout": null` and **change every study's
  digest** (even ones not using it). The fix is mandatory **and** sufficient: add
  `"attempt_timeout"` to `_NON_IDENTITY_SOLVERS` (`:40`) and `_NON_IDENTITY_GRADER`
  (`:41`) so it is popped before hashing → digest unchanged for everyone.

### Other surfaces — unaffected (verified)
- `model_locks.json` pins only the `solvers.sample` (`ModelSample`) spec, not its
  siblings; `attempt_timeout` is a sibling of `sample`, so the lock's normalized
  compare (`_modelsample.py`) never sees it. No lock work.
- The run manifest echoes the validated config, so `attempt_timeout` is captured
  for provenance automatically — no manifest-schema work.
- Public-API snapshot (`tests/test_public_api_snapshot.py`) covers only
  `itemeval.__all__` + CLI subcommands; config-model fields aren't snapshotted and
  no CLI command is added, so it stays green.

### Schema-evolution gate (DEVELOPMENT.md)
Additive optional field, default `None`, compatible by construction. Discharged by
the **digest-stability guard test** (gate item 1: freeze that a config with vs
without the field produces the same `experiment_id`/condition ids). No `Study
migration` note needed.

---

## W1 — `attempt_timeout` pass-through (generate + grade)

**Goal.** A study can bound how long one model attempt may stall before it is
abandoned and retried (likely onto a healthier OpenRouter route). One knob per
stage, pass-through to inspect, no behavior change for studies that don't set it.

**Config / public surface.**
- `SolversConfig.attempt_timeout: int | None = Field(default=None, ge=1)`
  (`_config.py:206`) — seconds; generate stage.
- `GraderSpec.attempt_timeout: int | None = Field(default=None, ge=1)`
  (`_config.py:303`) — seconds; per-judge (siblings `provider_routing`/`max_tokens`
  are already per-grader, and `grade/_run.py:591` already reads the grader spec
  per condition).
- **Knob bucket: optimization / robustness** (UX-PATTERNS Law 5) — a sensible
  default that trends invisible, with an explicit override. It does *not* spend,
  does *not* change condition ids, is *not* a gate.
- **No new CLI flag, no new exit code, no new JSON top-level key.** The value rides
  the config echo already present in the manifest and in `--json` config dumps.

**Mechanism (file:line).**
1. `_config.py` — add the two fields above (validated `ge=1`).
2. `_identity.py` — `_NON_IDENTITY_SOLVERS += ("attempt_timeout",)` (`:40`);
   `_NON_IDENTITY_GRADER += ("attempt_timeout",)` (`:41`). *(Mandatory: keeps the
   digest stable; see Context.)*
3. `generate/_task.py` — `build_generate_task(...)` gains
   `attempt_timeout: int | None = None`; pass it into `GenerateConfig(...)`
   (`:87`) as `attempt_timeout=attempt_timeout`. **Not** added to `gen_params`.
4. `generate/_run.py` — at the call site (`:709`) pass it straight through:
   `attempt_timeout=prep.config.solvers.attempt_timeout`.
5. `grade/_judge.py` — `build_judge_task(...)` gains `attempt_timeout`; into
   `GenerateConfig(...)` (`:156`).
6. `grade/_run.py` — call site (`:593`) passes
   `attempt_timeout=prep.config.grader_spec(cond.grader_name).attempt_timeout`.

Pure pass-through (Decision: Option A) — no helper, no batch gate. `None` unless a
study sets it, so a run is byte-identical to today's by default.

**UX contract.**
- **Side effects (Q1):** none new — no network/cache/lock/provider-side state.
  No ledger row.
- **Quotable summary (Q2/Q8):** none required (a robustness knob, not an action;
  and `None` by default → nothing to announce). The value is in the manifest
  config echo.
- **JSON parity (Q3):** config echo only; no new fact invented in prose.
- **Doc anchor (Q4):** `Configuration.md` owns the knob; `Error-Handling.md` owns
  "what a timed-out attempt does" (abandon → retry → reroute; batch caveat).
- **Hint candidate (Q5):** a too-low value silently turns slow-but-valid
  generations into errors/repeated retries. A future `timeout-heavy` coded hint
  could fire when a run shows many `AttemptTimeoutError`-shaped errors — **deferred**
  (don't over-engineer; the existing error surfacing already shows the symptom).
  Recorded here, not built.
- **Consent (Q7):** none — no spend, no row replacement.
- **Stability (Q9):** additive optional config field; no new exit code/JSON/hint
  code.

**Tests (hermetic, no API).**
- `tests/test_config.py` — `solvers.attempt_timeout` and `graders.<n>.attempt_timeout`
  accept a positive int; reject `0`/negative (`ge=1`).
- `tests/test_identity.py` — **guard test**: two configs identical but for
  `attempt_timeout` (set vs unset, on solvers and on a grader) produce the **same**
  `normalized_config_digest` and the same `experiment_id`. (Discharges the schema
  gate.)
- `tests/test_grid.py` — same pair produces identical generate **and** grade
  condition ids.
- `tests/test_generate_run.py` — `build_generate_task(..., attempt_timeout=42)`
  yields `task.config.attempt_timeout == 42`; default `None`. (Builder is pure;
  construct with a tiny item list, no eval.)
- `tests/test_grade_run.py` — same for `build_judge_task`.

**Docs/CHANGELOG (same commit as the behavior).**
- `CHANGELOG.md` `[Unreleased] → Added`: the knob, pass-through, non-identity,
  batch caveat; `Closes: request-timeout`.
- `docs/BACKLOG.md`: **remove** the "Response / attempt timeout" section
  (its design record lives on in this archived plan).
- `ROADMAP.md`: `request-timeout` is **not** named as a future candidate there, so
  no move is required by `test_docs_consistency.py`; optionally append it to the
  0.3 `**Already landed**` line for completeness (it's part of the run-UX cluster
  already listed there).
- Wiki: `Configuration.md` (both knobs), `Error-Handling.md` (timeout→retry→reroute
  behavior + the batch caveat), and a `FAQ.md` "my run hung on one model" pointer.
- `docs/UX-PATTERNS.md`: no ledger row (no side effect) and no hint row — no
  documented surface changes under Option A.

---

## Sequencing (canonical)

Single workstream, one `feat:` commit (config + identity + both stages + tests +
same-change paperwork together — it's one atomic surface). After it:
`make check` (lint + fast tests). Then archive this plan (`IMPLEMENTED <date>`,
`git mv` to `docs/plans/archive/`, fix inbound links).

## Out of scope (explicitly, to prevent creep)

- **`timeout` (whole-request, incl. retries).** Not exposed — the design intent is
  retry-onto-a-healthier-route, which is `attempt_timeout`. Add only on demand.
- **Bounding `max_retries` / terminal-vs-transient retry suppression.** That's
  `preflight-check`'s (D's) terminal-vs-transient classifier, which **does not
  exist yet** (H precedes D). Until D, a timed-out attempt retries per inspect's
  default; a generous/explicit value is what keeps it from firing on healthy
  streams. Tracked in BACKLOG `preflight-check`.
- **`MaterializeSpec.attempt_timeout`** (`grade/_materialize.py:59`). The
  materializer is one solution-independent call per item, a niche surface; left
  `None` (inspect default). Add later if a materialize hang is observed.
- **A `timeout-heavy` hint.** Recorded under W1 Q5; not built.
