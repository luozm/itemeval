# Implementation plan — cache-projection (pre-flight response-cache hit projection)

**Status: IN PROGRESS (started 2026-06-20).** Written 2026-06-20 against
inspect_ai 0.3.x (pinned in `uv.lock`). **The load-bearing facts below are tied
to inspect's private response-cache internals — re-verify every `[verify]` on an
inspect bump; a guard test (W-test) pins them so a drift goes red, not silent.**
Working brief for a fresh session; read these first, in order:

1. `CLAUDE.md` — conventions; **"don't over-engineer"** matters here (the probe is
   niche — see Motivation/scope).
2. `docs/UX-PATTERNS.md` — Law 2 (**the money gate keeps comparing the ceiling**;
   this projection is *informational*, never the gate input), Law 6 (three
   renderings), Law 1 (a read-only local probe is not a side effect — no ledger
   row).
3. `DEVELOPMENT.md` — inspect boundary: **inspect imports are confined to task
   builders / orchestrators / extensions**, and **`import itemeval` / no-API
   commands must stay light** (no eager inspect import on the estimate path). Both
   shape where this code lives (a new lazy-importing extension module).
4. This file end-to-end.

Cluster context: item **G** of the run-UX cluster (`local/run-ux-reorder-plan.md`).
Pairs with the shipped `preflight-check` (**D**) as one "before you spend, here's
what will happen" pre-flight report. Covers report §2.H (cache invisible).
**Distinct from `reuse-savings`** (BACKLOG), which attributes cache reuse
*post-hoc*; this is a *pre-flight projection*.

Scope: **W1** the response-cache probe (the mechanism + its guard test) ·
**W2** wire the cached/fresh split + "real" cost into the estimate + renderings.

---

## Motivation & honest scope (read before building)

A re-run's true cost is invisible up front: of the calls a run *will* make, some
are already in inspect's **local response cache** ($0), the rest are paid fresh —
but the pre-flight estimate prices them all as fresh, over-stating a cheap
recovery re-run.

**Where this actually bites (keep scope honest).** itemeval's **store-based
resume already skips completed cells** ($0, not even a call). So the response
cache only changes the picture for calls that are *not* store-skipped but *are*
cached — i.e. **`--force`** (re-run completed cells: the store says "replace", the
response cache replays at $0) and a **`replications` bump** (epochs 1..R₁ replay
from cache while R₁..R₂ are fresh). A wave run turns the cache **off** (fresh
draws), so it is out of scope by construction. For an ordinary first run or a
plain resume the projection reports "0 cached" — correct, and cheap to compute.
This narrowness is why it is item G, not a headline.

---

## Context: the facts that decide the design

### inspect's response cache (the replication target) `[verify on bump]`

`.venv/.../inspect_ai/model/_cache.py`:

- A call is cached under `cache_path(model) / key`, where `cache_path`
  (`_cache.py:265`) = `$INSPECT_CACHE_DIR/generate/<model>` (or
  `inspect_cache_dir("generate")/<model>`), and `key` = `_cache_key(entry)`
  (`_cache.py:139`) — an **md5** over: `config.model_dump(exclude={max_connections,
  adaptive_connections, max_retries, timeout, cache, batch})`, the input messages
  (`message.model_dump(exclude={"id"})` joined), `base_url`, `tool_choice`,
  `tools`, the parsed `expiry` (or `None`), `policy.scopes`, and — when
  `policy.per_epoch` — the epoch number.
- The entry is built inside `Model.generate` (`_model.py:1076`) as
  `CacheEntry(base_url=self.api.base_url, config=deepcopy(config), input=input,
  model=str(self), policy=policy, tool_choice=tool_choice, tools=event_tools)`,
  then `cache_fetch(entry)` returns the stored `ModelOutput` (hit) or `None`.
- itemeval always caches with `CachePolicy(expiry=None, per_epoch=True)`
  (`generate/_task.py:74`), and **`expiry=None` ⇒ the entry never expires**
  (`_cache_expiry` returns None). So for itemeval's own writes a **hit is exactly
  `(cache_path(model)/key).exists()`** — no unpickle, no expiry check needed.

**Every input to the key is something itemeval already constructs** for the real
run: the rendered messages (`generate/_task.render_input`, incl. the `split_prompt`
system/user split), the `GenerateConfig` (`generate/_task.build_generate_task`),
the resolved model (`resolve_model` → `.api.base_url`, `str(model)`), `tools=[]`,
`tool_choice=None`, and the epoch range it will run. So the probe **reconstructs
the identical `CacheEntry` and reuses inspect's own `_cache_key`** (wrap, don't
fork) rather than re-deriving the md5 — the BACKLOG's "replicating the cache key"
risk is handled by *reusing inspect's class*, not re-implementing it.

### The boundary constraint (decides where the code lives)

`budget/_estimator.py` is **engine-free today** (no inspect import), and the
`estimate` command must **stay light** (no eager inspect import — `import
itemeval` laziness, DEVELOPMENT.md). The probe *needs* inspect (`CacheEntry`/
`cache_path`). Resolution: a **new extension module `src/itemeval/_cacheprobe.py`**
(sibling of `_cachegate.py`, an allowed inspect-importing "extension") that does
its `from inspect_ai.model._cache import CacheEntry, cache_path` **lazily inside
the function body**. It is invoked **only when `config.cache` is on and there are
fresh (non-store-skipped) calls to probe** — so an ordinary estimate never imports
inspect and stays light. Resolving models for `base_url` likewise happens only on
that path.

### Estimator surface to extend

`budget/_estimator.py`: `Estimate` (`:170`), `StageEstimate` (`:117`),
`ConditionEstimate` (`:77`) already carry cache-*discount* fields (provider prompt
cache — a different thing). Add **append-only** response-cache-projection fields
(see W2). `estimate_study` (`:380`) is the entry point; the per-stage projection
counts `remaining_calls` already — the probe refines that count into
cached-vs-fresh.

---

## W1 — the response-cache probe (`_cacheprobe.py`) + guard test

**Goal.** Given the planned fresh calls of a stage, return `(cached, fresh)`
counts (and the per-call hit bits, so W2 can price the fresh remainder).

**Mechanism.** `count_cache_hits(prep, stage, planned_calls) -> CacheProbe`:
- Lazy `import` of inspect's `CacheEntry` + `cache_path` inside the body.
- Resolve each distinct model once (reuse `_mockmodels.resolve_model` with the
  same `model_args_for(...)` the run uses) to get `.api.base_url` + `str(model)`.
  `[verify]` `resolved.api.base_url` is reachable and equals what `Model.generate`
  uses; for `mockllm/*` it is `None` (fine — keys still compute).
- For each planned `(condition, item, epoch)`: rebuild the identical messages via
  the **same** `render_input` path the task builder uses (factor it so the probe
  and `build_generate_task` share one renderer — never a second copy that can
  drift), build the identical `GenerateConfig`, construct `CacheEntry(...,
  policy=CachePolicy(expiry=None, per_epoch=True), tool_choice=None, tools=[])`,
  and test `(cache_path(str(model)) / entry.key).exists()`.
- Grade path is symmetric over `grade/_judge` message construction. **Both stages
  are in scope (maintainer decision 2026-06-20):** a crash during *either* stage
  must get the projection. Factor the judge-message renderer the same way as
  generate's so the probe reuses it (never a drifting second copy).

**Simplicity guard.** Pure existence check (no unpickle) — valid because
itemeval's `expiry=None` entries never expire. No new knob. Skip entirely when
`not config.cache`.

**Guard test (the safety net for the brittle replication).** `tests/test_cacheprobe.py`:
hermetic, `mockllm/*`, real inspect cache dir (the conftest already isolates
`INSPECT_CACHE_DIR` to tmp). Run a `generate` with `cache` on so inspect writes
real cache files, then call the probe over the *same* planned calls and assert it
reports **all cached**; mutate one message/epoch and assert that one flips to
fresh. This **pins itemeval's reconstruction to the installed inspect** — an
inspect change to `_cache_key`/`CacheEntry` turns this red (the documented bump
checklist catches it), instead of silently mis-projecting.

**UX contract.** None of its own (internal). No side effect (read-only local stat)
→ no ledger row.

**Docs/CHANGELOG.** Internal; ships under the W2 changelog entry.

---

## W2 — cached/fresh split + "real" cost in the estimate

**Goal.** Show, before the gate, `N cached / M fresh → ~$X real` so a cheap
recovery re-run isn't over-stated.

**Config / public surface.** **No new knob.** Append-only fields:
`StageEstimate.cache_hits` / `cache_misses` / `real_remaining_usd` (the remaining
projection counting only the fresh calls as paid; cached calls = $0), summed onto
`Estimate`. The existing `remaining_usd`/`usd` are **unchanged** — the gate keeps
comparing the ceiling (UX Law 2); `real_remaining_usd` is informational, beside
`expected_remaining_usd`.

**Mechanism.** In `estimate_study`, after the per-stage call plan is known and
**only when `config.cache` is on**, call `count_cache_hits`; subtract the cached
calls' projected cost from the fresh-cost figure. Lives behind the lazy boundary
(W1), so a no-cache estimate is untouched and inspect-free.

**UX contract.** A pre-gate line on `estimate`/`generate`/`grade` (only when there
*are* cached hits, else silent — never noise): e.g.
`cache: 38 of 40 calls already in the local response cache ($0) → ~$0.04 real of
$2.10 projected`. JSON parity: the new `StageEstimate`/`Estimate` fields. Doc
anchor: `Cost-Savings.md` (the response-cache section) + `Budget-and-Costs.md`
(estimation). Pairs with `preflight-check` as the second pre-flight line.
**Money gate unchanged** (Law 2). Consent: none (read-only).

**Tests.** `tests/test_estimator.py` — with `--force` over a warm mock cache, the
estimate reports `cache_hits == N` and `real_remaining_usd < remaining_usd`;
without cache, the fields are 0/equal and no inspect import happens
(`[verify]` via a no-inspect-in-sys.modules assertion on the plain estimate path,
mirroring any existing laziness test).

**Docs/CHANGELOG.** `[Unreleased]` `Added`, `Closes: cache-projection`; **remove**
the `Pre-flight cache projection` BACKLOG section; ROADMAP move; wiki
(Cost-Savings, Budget-and-Costs); no UX-PATTERNS ledger row (read-only), but note
the new estimate line in the relevant doc.

---

## Sequencing (canonical)

1. **W1** probe + guard test (proves the replication holds against installed
   inspect before anything depends on it).
2. **W2** estimate fields + renderings + the pre-gate line.

One `feat:` commit. `make check` after. Public-API snapshot: **untouched** (no
`__all__`/CLI-subcommand change — only additive estimate fields + an internal
module). Then archive this plan.

## Out of scope (explicitly)

- **Post-hoc reuse attribution** — that's `reuse-savings` (separate BACKLOG key).
- **Making the gate consider the cache** — never; the gate compares the ceiling
  (Law 2). This is informational only.
- **Provider prompt-cache** projection — already shipped (the `cache_discount_*`
  fields); this is the *local response* cache, a different mechanism.
- **Any new knob / exit code / side effect.**
