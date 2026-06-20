# Implementation plan — lock-rebless (safe re-bless + change briefing for a drifted sample lock)

**Status: NOT STARTED.** Written 2026-06-20 against the shipped `lock-spec-brick`
fix (L1+L2 on `main`: `_normalized_spec`/`_LockSpec` in `src/itemeval/_modelsample.py`,
`allow_spec_drift` threaded through `prepare_study`/`_load`/`build_status`). This is
the working brief for a fresh session — read first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — binding UX contract (Law 1 announce side effects; Law 2
   nothing blocks but money; Law 6 three renderings; Law 7 append-only machine
   surface; the side-effect ledger).
3. `DEVELOPMENT.md` §"Study-facing schema evolution" — this feature *is* the
   `model_locks` surface's safe-reconcile path that section requires.
4. This file end-to-end.

Scope: **W1** re-bless mechanism (lock schema + resolve comparison) · **W2** change
briefing on the write-path mismatch · **W3** `itemeval rebless` CLI surface.
No inspect_ai involvement anywhere (pure config/lock code).

---

## Context: the facts that decide the design

Current genuine-mismatch path, `src/itemeval/_modelsample.py` `resolve_model_sample`
(post-L1/L2):

```python
if lock is not None:
    if _normalized_spec(lock.get("sample")) != spec:
        if allow_spec_drift:
            return _reuse_pinned_panel(config, lock)        # read paths (L2)
        raise ConfigError("solvers.sample spec changed since … clear … to re-draw")
    models = list(lock["models"]); config.solvers.models = models
    return ModelSampleResult(… pinned_now=False, universe_drift=…)
```

Lock file shape (`_write_model_lock`): `{version, resolved_at, sample, universe_hash,
universe, models}`. `sample` is the normalized spec (`_LockSpec(...).model_dump()`).
`_normalized_spec(raw)` re-parses a stored spec through `_LockSpec` (None if
malformed). `ModelSampleResult` already carries `pinned_now`/`universe_drift`/
`spec_drift`.

**Key design decisions (from the brainstorm, locked):**
- **Keep both specs.** `sample` stays the spec the panel was *drawn under*
  (immutable). Re-bless adds `reblessed_spec` (+ `reblessed_at`). The match check
  uses the **effective** spec = `reblessed_spec` if present, else `sample`.
- **Separate command** `itemeval rebless CONFIG` — not a `--rebless` run flag.
  Running it is the consent (no money → no gate, Law 2); it announces the pin
  write (Law 1).
- **Sample lock only.** `dataset_locks.json` has the same footgun but is out of
  scope (open question in BACKLOG; do not build a shared abstraction now).

---

## W1 — re-bless mechanism (lock schema + comparison)

**Goal.** A pinned study whose spec genuinely changed can record the new spec
without re-drawing, keeping the panel it already ran; later runs of the new config
then match and reuse.

**Public surface.** Lock gains optional `reblessed_spec` (normalized spec dict) +
`reblessed_at` (iso). `ModelSampleResult` gains `reblessed: bool = False`
(append-only). No new config knob (re-bless is a command, not config).

**Mechanism (`_modelsample.py`).**
- `_effective_spec(lock) -> dict | None` = `_normalized_spec(lock.get("reblessed_spec")
  or lock.get("sample"))`. Use it in the match check (replaces the bare
  `_normalized_spec(lock.get("sample"))`).
- On a matching reuse, set `reblessed=lock.get("reblessed_spec") is not None` on the
  result so provenance can say "re-blessed".
- `rebless_model_sample(config, pricing, locks_path) -> ModelSampleResult` (or a
  small `ReblessOutcome`): read lock (no lock → `ConfigError` "nothing to
  re-bless"); compute current normalized `spec` (reuse the build path; if the
  universe no longer builds, fall back to a cheap `source` so re-bless still works —
  the panel is kept regardless); if `_effective_spec(lock) == spec` → no-op
  (`ConfigError`/friendly "already current"); else rewrite the lock keeping
  `sample`/`models`/`universe`/`universe_hash`, set `reblessed_spec=spec`,
  `reblessed_at=utc_now_iso()`; mutate `config.solvers.models` to the kept panel;
  return a result with `reblessed=True`.
- `_write_model_lock` extended to accept/preserve `reblessed_spec`/`reblessed_at`
  (default None → omitted, so a never-reblessed lock is byte-identical to today).

**UX contract.** Re-bless is a **pin write** (decides future runs) → announce
(Law 1). No gate (Law 2 — no spend). `reblessed` rides `--json` via
`model_sample`.

**Tests (`tests/test_model_sample.py`).** draw → edit spec → `rebless` → lock keeps
old `sample` + new `reblessed_spec`, `models` unchanged; a later `resolve` with the
edited config matches (no drift, `reblessed=True`); editing *again* drifts again;
re-bless with no lock / already-current errors cleanly.

## W2 — change briefing on the write-path mismatch

**Goal.** The hard-fail that remains on `generate`/`grade` explains what changed
and points at the safe action, instead of the dangerous "clear the lock".

**Mechanism.** `_spec_diff(old, new) -> list[str]` — flatten both normalized specs
(top-level + `where.*`), emit `field: old → new` lines for added/removed/changed
keys. Replace the `raise ConfigError(...)` text with the briefing: the diff lines,
"the pinned panel (N models) was drawn under the old spec", and the two actions
(`itemeval rebless CONFIG` to keep the panel; delete `model_locks.json` to re-draw a
NEW panel). Keep it quotable (relay rule, Law 8).

**UX contract.** Still a hard stop on the write path (correct — running a different
panel than the pin mixes results); only the *message* improves. No JSON change
(the error path under `--json` already emits structure elsewhere; the message is
the human channel).

**Tests.** assert the raised `ConfigError` names the changed field and both safe
actions; assert it does NOT say the bare "clear … to re-draw" without the re-bless
alternative.

## W3 — `itemeval rebless` CLI surface

**Goal.** The consent surface for W1.

**Mechanism (`cli.py`).** New `rebless` subparser (CONFIG + the standard
`-C/--base-dir`; no `--json` needed first cut, or add for parity). Loads config,
calls `rebless_model_sample`, prints the briefing of what's being re-blessed + a
confirmation line (`re-blessed: model_locks.json now records the new spec; panel of
N models unchanged`). Maps `ConfigError` to exit 2 like other commands
(`_USAGE_ERRORS`).

**UX contract.** Announce the pin write (Law 1). Running the command is the consent;
no interactive prompt (Law 2).

**Tests.** CLI smoke: `rebless` on a drifted study returns 0 and rewrites the lock;
on a clean/no-lock study returns the usage error.

---

## Sequencing (canonical)

W1 → W2 → W3 (W2's briefing references the `rebless` command W3 adds; W1's effective
spec is what W2 diffs against). After each: `make check`; CHANGELOG + normative doc
tables in the same commit.

Same-change rule on the shipping commit: CHANGELOG `[Unreleased]` `Added` (or
`Fixed`/`Changed` as fits) with `Closes: lock-rebless`; remove the BACKLOG section;
`git mv` this plan to `docs/plans/archive/`; update the UX-PATTERNS "Model sample
pin write" ledger row (re-bless is a new pin-write path) and the wiki
(Configuration / Outputs-and-Schemas: the `reblessed_spec` field + the `rebless`
command + the briefing replacing "clear the lock"; Agent-Guide: the safe action).

## Out of scope (explicitly)

- **`dataset_locks.json` re-bless** — same footgun, tracked as the BACKLOG open
  question; do not build a shared lock-reconcile abstraction now.
- **A full audit log of every re-bless** — keep two fields (drawn + latest
  re-blessed), not an append list; honesty needs only "what it was drawn under" and
  "what it's blessed as now".
- **Growing the panel** (draw N more under a bumped `n`) — a different operation
  (`random.sample` is not prefix-stable); not re-bless.
