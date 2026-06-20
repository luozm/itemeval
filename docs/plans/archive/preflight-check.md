# Implementation plan — preflight-check (pre-flight model probe + terminal/transient classifier)

**Status: IMPLEMENTED 2026-06-20.** Shipped on `feat/preflight-check`
(CHANGELOG `Closes: preflight-check`). This file is the design record; the
"working brief" framing below is past tense. Written 2026-06-20 against
inspect_ai 0.3.x (pinned in `uv.lock`) — re-verify the `[verify]` facts below if
that moved. This file is the working brief for a fresh implementation session: it
carries all context that session needs. Read these first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract. The load-bearing tension this
   feature must resolve: **the money gate is the only thing that spends, and it is
   the only blocking interaction** (Laws 2, 3). A probe fires real (tiny) model
   calls, so *where it sits relative to the gate* is the central design question.
3. `DEVELOPMENT.md` — inspect_ai boundary rules (wrap don't fork; pass through
   don't rename; inspect imports confined to task-builder/orchestrator/extension
   modules). The probe + classifier are an orchestrator-tier concern.
4. This file end-to-end before coding.

Scope: **W1** terminal-vs-transient error classifier (the reusable primitive) ·
**W2** `itemeval preflight` command (probe + roster report) · **W3** apply the
classifier to in-run condition error reporting (concise, classified).

Cluster context: this is item **D** of the run-UX cluster
(`local/run-ux-reorder-plan.md`). It **builds the terminal-vs-transient
classifier** that the shipped `request-timeout` (H) feature will later consume
("don't retry a terminal timeout") — see `docs/plans/archive/request-timeout.md`
Out-of-scope §. H needs only that the classifier *exists* and is importable; the
in-run retry-suppression refinement is **not** in this plan (W3 reports concisely
but does not yet suppress inspect's per-sample retry — see Out of scope).

---

## Context: the facts that decide the design

### Where a run spends, and the gate that guards it

`cli._run_stage` ([cli.py:498-626](../../src/itemeval/cli.py)) is the shared
`estimate → gate → run → report` skeleton for both `generate` and `grade`:

- estimate (`estimate_study`, no spend) → print the pre-gate block →
  `_check_gate(st.remaining_usd, …)` at [cli.py:544](../../src/itemeval/cli.py)
  (the **single** money gate, UX Law 2) → if `gate.proceed`, call the runner
  (`run_generate` / `run_grade`) which fires the paid `inspect_ai.eval`.
- **Nothing in itemeval spends model money before that gate.** Pricing refresh and
  dataset download hit the network but cost no model dollars. A probe that calls
  models is the *first* would-be model spend, so it cannot simply be dropped in
  ahead of the gate without breaking "never be surprised by a bill."

### How a model is resolved and could be probed

`resolve_model(model, stage, model_args)` ([_mockmodels.py:63](../../src/itemeval/_mockmodels.py))
always returns an inspect `Model`. A `mockllm/*` id resolves to a deterministic
in-process callable — **a probe of a mock model makes no network call and costs
nothing** (so tests stay hermetic/free, and a mock roster reports all-ok
instantly). A real id resolves via `get_model(model, **model_args)`.

Probe mechanism `[verify against .venv]`: `await model.generate("ping",
config=GenerateConfig(max_tokens=1, max_retries=0))`
([_model.py:673](../../.venv/lib/python3.12/site-packages/inspect_ai/model/_model.py)).
`max_retries=0` makes a terminal failure raise immediately (no retry storm during
a *probe*); the raised provider exception carries the status code / message the
classifier reads. Run N distinct-model probes concurrently under `asyncio.run` +
a bounded `gather` (or inspect's own concurrency helper — `[verify]` whether
`inspect_ai.util` exposes one; a plain `asyncio.Semaphore` is the no-dep
fallback). **This calls a published inspect API directly (`Model.generate`) — it
is wrapping, not forking** (DEVELOPMENT.md boundary), and writes **no `.eval`
log**, so it cannot pollute the harvest path (harvest only scans `logs/generate`
& `logs/grade`, [_harvest.py:95](../../src/itemeval/_harvest.py)). Rejected
alternative: a one-sample `inspect_ai.eval` per model — heavier, litters log
dirs, and needs a harvest-exclusion carve-out.

### What "terminal vs transient" means (the classifier's contract)

- **Terminal** — the model/endpoint is dead for this config and a retry cannot
  help: `404` (model not found / EOL), `401`/`403` (auth/permission),
  `400` model-does-not-exist, and inspect's `PrerequisiteError` (missing
  provider SDK / API key). These are the roster-health failures the user must fix
  by editing the config, not by retrying.
- **Transient** — a retry (or reroute) could succeed: timeouts, `429` rate
  limits, `5xx`, connection resets. A probe seeing these reports "unverified",
  never "dead" (we must not tell a user to delete a model that was merely rate-
  limited).
- **Unknown** — anything unclassifiable → treat as transient/unverified (never
  block, never accuse).

Inputs the classifier reads, in order of reliability: an HTTP status code if the
exception exposes one (`getattr(e, "status_code", None)`, or `.response.status_code`
for httpx/SDK errors `[verify]` the attribute shape per provider), inspect's
own exception types (`inspect_ai._util.error.PrerequisiteError` `[verify]` path),
then a lowercased-message substring fallback (`"not found"`, `"does not exist"`,
`"eol"`, `"deprecated"`). Pure function, no I/O — fully unit-testable without
inspect or a network.

### Config surface that exists today (for knob-bucket placement)

`solvers.attempt_timeout` / `graders.<name>.attempt_timeout` (the shipped H knob,
[_config.py:221](../../src/itemeval/_config.py),
[_config.py:330](../../src/itemeval/_config.py)) are pure pass-through execution
knobs. Preflight is a *robustness/visibility* feature; the decision below is
whether it needs a config knob at all (recommended: no config knob — a command +
a `--no-*`/`--json` flag).

---

## The central design decision (resolve at checkpoint before building)

**Where does the probe live, given it spends un-gated model money?**

**Recommended — a separate staged command `itemeval preflight CONFIG`.** Law 4
makes the staged verbs the real contract; `estimate` is the precedent — an
explicitly-invoked planning command. Running `preflight` *is* the consent to its
(sub-cent) spend, exactly as running `estimate` is consent to its network calls.
It composes (`itemeval preflight cfg && itemeval generate cfg`), carries no
gate-ordering puzzle, adds no per-run latency tax to every `generate`/`grade`,
and never spends money the user did not directly ask for. This keeps "the money
gate is the only *surprising* spend" intact: a probe command is not a surprise.

- Probes the **distinct execution models of the current grid** (generate solver
  models + grade judge models for the configured graders/rubrics). One probe per
  distinct id, concurrent.
- Output (Law 1 announce + Law 8 quotable): one summary line
  `preflight: 39 ok · 1 dead · 2 unverified — dead: openrouter/x/y (404 model not
  found); unverified: … (timeout)`, plus a per-model breakdown. `--json` parity:
  a `models[]` array of `{id, status: ok|dead|unverified, detail, http_status}`.
- **Exit code:** `0` when no model is `dead`; `1` (the existing generic-error
  code — *not* a new value, Law 7) when any model is terminal-dead, so
  `preflight && generate` short-circuits for agents/CI. `unverified` alone does
  not fail (it is not a roster error).
- **No new gate, no blocking** (Law 2): the command reports and exits; the user
  edits the roster and re-runs.

Considered and rejected:
- **Inline in `generate`/`grade`, default-on (recoverable-harvest's
  auto-on-with-`--no-*` shape).** Cleanest "before the paid loop" reading of the
  BACKLOG, but it spends model money *before* the gate is crossed — the first
  un-gated model bill in the package — and taxes every run (incl. resumes) with
  probe latency. The principle break (gate is the only model spend) outweighs the
  convenience.
- **Inline but *after* the gate proceeds, before the big eval.** Spend is covered
  by the consent already given, and a dead-model abort is "a run the user started
  failing early" (not a new gate). But it weakens "fix the roster *before* you
  commit" and pushes a second early-exit path into the hot loop. Possible later;
  not now.

→ **Checkpoint question for the maintainer:** ship the separate `preflight`
command (recommended), or wire it inline into `generate`/`grade`? The rest of
this plan assumes the separate command; W2 is the only part that changes if the
answer is "inline".

---

## W1 — terminal-vs-transient classifier (the reusable primitive)

**Goal.** A pure, importable classifier that labels a model-call failure
terminal / transient / unknown. It is the load-bearing primitive D owns: W2 uses
it to decide "dead" vs "unverified", and the shipped `request-timeout` (H) will
later import it for "don't retry a terminal timeout."

**Config / public surface.** No config knob. New internal module
`src/itemeval/_classify.py` exporting a small enum/Literal
(`"terminal" | "transient" | "unknown"`) and `classify_error(exc: BaseException)
-> str` (plus a message-only overload `classify_message(status, message)` for the
probe and for harvest/log paths that only have a string). Not added to the public
`itemeval/__init__.py` API surface (internal; H imports it cross-module by path)
— so `test_public_api_snapshot.py` is untouched. If a future feature needs it
public, that is its call.

**Mechanism.** Pure function; no inspect import required for the core logic
(it inspects duck-typed attributes + message text), keeping the module engine-
free and trivially testable. `[verify]` the one inspect coupling: the
`PrerequisiteError` import path under `.venv` (treated terminal), guarded so its
absence degrades to the message fallback. Status-code map is a small fixed dict;
message substrings a fixed tuple — no knob, no generality beyond the cases named
in Context.

**UX contract.** No user-facing surface of its own (a primitive). No ledger/hint
rows.

**Tests.** `tests/test_classify.py` — table-driven: synthetic exceptions carrying
`status_code` 404/401/400/429/500/timeout; bare messages ("model not found",
"deprecated", "rate limit", "connection reset"); `PrerequisiteError`-shaped;
and an unrecognized exception → `unknown`. Pure, hermetic, no network.

**Docs/CHANGELOG.** No standalone CHANGELOG line (it ships as part of the
preflight entry). Internal.

---

## W2 — `itemeval preflight CONFIG` (probe + roster report)

**Goal.** Before committing to a paid run, the user runs one command that fires a
~1-token call per distinct model and sees roster health
(`39 ok · 1 dead: <model> 404 EOL`), so a dead model is caught at $-nothing
instead of mid-paid-run, and the per-condition failure flood (§2.F) is pre-empted.

**Config / public surface.**
- New CLI subcommand `preflight CONFIG [--json] [--no-color?]`, parallel to
  `estimate`/`status` (the no-heavy-spend commands). Flags: `--json` (Law 6
  machine rendering), standard `-C/--base-dir`, `--policy` (so a `dev` probe
  matches a `dev` run's roster — same draw), `--condition`/`--grader` filters to
  scope the probe.
- New public Python entry point `preflight_study(prep: PreparedStudy) ->
  PreflightReport` exported from `itemeval/__init__.py` (lazy, like the other
  `*_study` functions) — **this changes the public API snapshot; update
  `tests/test_public_api_snapshot.py` golden in the same change** (deliberate).
- New pydantic result models in a new `src/itemeval/_preflight.py`:
  `PreflightModel{id, status, detail, http_status}` and `PreflightReport{study,
  models: list, ok, dead, unverified, hints}` (append-only fields).

**Mechanism.** `src/itemeval/_preflight.py` (orchestrator-tier — may import
inspect):
1. Collect distinct execution models from `prep.grid` (generate solver models +
   grade judge models per configured grader/rubric), applying native-route
   substitution `prep.native_routes` so the probe hits the id that will actually
   run. Mock ids are probed too (return ok instantly, no network).
2. `asyncio.run` a bounded `gather` of `resolve_model(id, stage, model_args_for(
   …)).generate("ping", GenerateConfig(max_tokens=1, max_retries=0))`; on success
   → `ok`; on exception → `classify_error` → `dead` (terminal) / `unverified`
   (transient|unknown) with the exception's status/message as `detail`.
   Concurrency bound = a small constant (e.g. `max_tasks_for`-style distinct-model
   count) `[verify]` no event-loop clash with inspect (a fresh `asyncio.run` at
   the CLI top level is clean).
3. CLI `cmd_preflight`: `_load(args)` → `preflight_study(prep)` → print the
   provenance line + summary + per-model breakdown (text) or the JSON document.
   Exit `1` iff any `dead`, else `0`.

Simplicity guard: no caching of probe results across runs (a model can die
between runs; a probe is cheap and the user invoked it deliberately). No retry of
the probe itself. No new knob.

**UX contract.**
- **Side effects → new ledger row** in UX-PATTERNS (network → provider, a ~1-token
  call per distinct model; tiny spend): announced by the summary line
  `preflight: N ok · M dead · K unverified — …`. Add the row in the same commit.
- **Quotable summary** (Law 8): the one-line `preflight: …` with counts + the
  dead ids and reasons.
- **JSON parity** (Law 6): every count + per-model status has a field; `--json`
  stdout is pure JSON. Hints (if any) ride the `hints` array.
- **Doc anchor** (Law 6): wiki `Error-Handling.md#preflight` owns the
  terminal-vs-transient explanation + the command; `CLI.md` lists the subcommand;
  `Agent-Guide.md` adds the `preflight && generate` short-circuit pattern.
- **Consent class** (Law 2/3): the spend is the sub-cent probe; invoking the
  command is the consent (like `estimate`). **No new gate, no prompt.** Python
  `preflight_study` never prompts.
- **Surface parity** (Law 8): CLI + `preflight_study` Python entry point.
- **Stability** (Law 7): no new exit code (reuses `0`/`1`); new JSON keys +
  Python fields append-only.
- **Hint candidate:** the silent failure mode is "a dead model discovered only
  mid-run." Preflight's whole report *is* the visibility, so no new coded hint is
  required; revisit only if a post-run "you skipped preflight and hit a dead
  model" hint proves wanted (out of scope here).

**Tests.** `tests/test_preflight.py` — drive `preflight_study` with a `prep` over
`mockllm/*` (all ok, hermetic) and with a `model_factory`/monkeypatched probe
that raises 404 / 429 / unknown to assert `dead`/`unverified` mapping, the
summary counts, exit-code logic, and `--json` shape. No network (the probe is the
injected boundary). A CLI smoke via the existing CLI test harness.

**Docs/CHANGELOG.** `[Unreleased]` `Added` entry with `Closes: preflight-check`;
**remove the `Pre-flight model check` section from `docs/BACKLOG.md`** in the same
commit; ROADMAP `0.3` names `preflight-check` as a future candidate under
"Already landed / in flight" — move it to that line (it is in `[Unreleased]`).
Wiki: `Error-Handling.md`, `CLI.md`, `Agent-Guide.md`. UX-PATTERNS: new ledger
row.

---

## W3 — classified, concise in-run condition error reporting

**Goal.** When a condition errors during the actual run, label *why* in the one
concise line `ConditionRunReport.message` already carries
([generate/_run.py:743,783](../../src/itemeval/generate/_run.py);
[grade/_run.py:627,657](../../src/itemeval/grade/_run.py)) — e.g. `terminal:
404 model not found` vs `transient: timeout` — so the operator reading the
summary knows whether to fix the roster or just re-run. This is the lightweight
half of the §2.F log-flood mitigation that does **not** require touching inspect's
retry loop.

**Config / public surface.** No new knob, no new field — reuse the existing
`ConditionRunReport.message` string, prefixed with the classification from W1.

**Mechanism.** In `eval_error_message`
([generate/_run.py:239](../../src/itemeval/generate/_run.py)) and the grade
equivalent, run the eval-error string / fatal through `classify_message` and
prefix the label. Purely a string enrichment; no behavior change.

**UX contract.** Summary-line wording only; JSON `message` field unchanged in
shape (richer content). No ledger/hint change.

**Tests.** Extend the existing generate/grade run tests that assert
`ConditionRunReport.message` to check the classification prefix on a synthesized
terminal vs transient eval error.

**Docs/CHANGELOG.** Folded into the same `Closes: preflight-check` entry (one
clause: "errored conditions now report terminal/transient in their summary line").

---

## Sequencing (canonical)

1. **W1** classifier (`_classify.py` + tests) — pure, no deps; everything else
   imports it.
2. **W2** `preflight` command (`_preflight.py`, CLI subcommand, public export,
   ledger row, wiki, BACKLOG removal, snapshot golden) — the headline surface.
3. **W3** classified in-run message — small enrichment, depends on W1.

One `feat:` commit (W1+W2+W3 are one atomic user-facing surface + its same-change
paperwork). After it: `make check` (lint + fast tests). Expect
`test_public_api_snapshot.py` red from the new `preflight_study` export — update
the golden deliberately in the same commit. Then archive this plan
(`IMPLEMENTED <date>`, `git mv` to `docs/plans/archive/`, fix inbound links —
grep the filename + the `local/run-ux-reorder-plan.md` D row).

After each step: `make check`; CHANGELOG + normative doc tables updated in the
same commit.

## Out of scope (explicitly, to prevent creep)

- **In-run terminal-retry *suppression*** (stop inspect retrying a terminal
  sample / a terminal `attempt_timeout`). That is the part the H handoff names,
  but it needs an inspect retry hook (`GenerateFilter`/`RetryDecision`, exported
  from `inspect_ai.model` — `[verify]` whether they let us veto a retry from a
  task). W1 delivers the classifier H needs to *exist*; wiring it into the live
  retry loop is a follow-up (note it in `docs/KNOWN-ISSUES.md` or a new BACKLOG
  key if it needs real design). W3 only *labels* the failure, it does not change
  retry behavior.
- **Probe-result caching / a roster-health store.** A probe is cheap and
  deliberately invoked; persisting "model X was alive at T" invites staleness.
- **Pre-flight cache projection** (`cache-projection`) — the sibling "before you
  spend" facet (cached vs fresh cost). Separate BACKLOG key; pairs with this as
  one report later, not now.
- **Probing the materializer model** (rubric materialization's per-item builder).
  It is a real model call too, but keep W2 to the solver/judge roster; add on
  demand.
- **A new config knob or a new gate/exit code.** None needed.
