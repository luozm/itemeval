# Implementation plan — provider-cache tail (FUTURE.md §1.6 follow-ups)

**Status: NOT STARTED.** Written 2026-06-12 against inspect_ai **0.3.239**
(pinned in `uv.lock`) — re-verify the "inspect facts" below if the pin moved.
This file is the working brief for a fresh implementation session: it carries
all context that session needs. Read these first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `docs/UX-PATTERNS.md` — **binding** UX contract. Every workstream below
   states its knob bucket and interaction strength; implementations must
   honor them (hints on stderr with stable codes, warnings never block, the
   money gate is the only gate, JSON parity, append-only machine surface).
3. `docs/COST-OPTIMIZATION.md` — the cache mechanism stack these workstreams
   complete; its "Open follow-ups" list is exactly this plan.
4. `DEVELOPMENT.md` — inspect_ai boundary rules. Everything here that touches
   inspect goes through **wrap don't fork; pass through don't rename**.
5. This file end-to-end before coding any part — the workstreams share one
   passthrough mechanism and one provider-facts table.

Scope: four workstreams.
**W1** provider pinning (`provider_routing`) ·
**W2** cache-key / retention passthrough (all providers, not just OpenAI) ·
**W3** cache-aware estimator ·
**W4** min-cacheable-prefix hint (all providers, not just Anthropic).

---

## Context: the one mechanism everything hangs on

itemeval currently hands inspect a **bare model string**: both runners call
`factory(cond.model, stage)` (default `resolve_model`,
`src/itemeval/_mockmodels.py:63-68`), which returns the string unchanged for
non-mock models. inspect 0.3.239 accepts per-model request extras as
**`model_args` on `get_model()`** (`inspect_ai/model/_model.py:1605-1616`,
kwargs flow into the provider constructor; memoization key includes
`model_args`, so distinct args → distinct cached `Model` instances — safe):

- `openai` provider pops `prompt_cache_key` / `prompt_cache_retention` from
  model_args and sends them on every request
  (`_providers/openai.py:118-125`, `openai_completions.py:84-89`). **No
  inspect change needed.**
- `openrouter` provider pops `provider` (a dict, passed verbatim as
  OpenRouter's provider-routing object) plus `models` / `transforms`
  (`_providers/openrouter.py:103-119`, injected into `extra_body` at
  373-385). **No inspect change needed.**
- `anthropic` provider inserts `cache_control: {"type": "ephemeral"}`
  markers itself when `cache_prompt` is on (`anthropic.py:1344-1381`); the
  marker has **no TTL field** — Anthropic's 1h-TTL variant is not reachable
  without forking. Out of scope; see W2.

**Design: one pure function, one impure chokepoint.**

- New pure helper `model_args_for(model: str, cfg) -> dict[str, Any]` in a
  non-inspect module (suggest `src/itemeval/_endpoints.py`, no inspect
  imports — fully unit-testable). It maps (model string, config) → the
  model_args dict for W1+W2 (empty dict for the common case).
- `resolve_model(model, stage)` grows a `model_args: dict` parameter (or
  reads config via the existing `PreparedStudy` plumbing — pick whichever
  keeps the `ModelFactory` signature change smallest; both runners construct
  the factory, `generate/_run.py:403` / `grade/_run.py`). Non-mock ids with
  non-empty args return `get_model(model, **args)` instead of the bare
  string. `_mockmodels.py` already imports inspect_ai, so the boundary rule
  holds; if the mock/real split gets awkward, a sibling `_models.py`
  extension module is acceptable — inspect imports stay confined either way.
- **Pass through, don't rename**: config field names below mirror the
  provider/OpenRouter names exactly (`provider_routing` carries OpenRouter's
  own `order` / `only` / `allow_fallbacks` / `ignore` keys verbatim;
  `prompt_cache_retention` keeps OpenAI's name).

---

## Provider facts table (single source for W1/W2/W4)

Established from the installed inspect source + `docs/COST-OPTIMIZATION.md`'s
live-pilot findings. Items marked **[verify]** are provider-doc facts that
move over time — the implementing session should re-check each against the
provider's current docs before hardcoding, and record the checked date in
code comments.

| Provider (direct API) | Cache activation | Key/retention params | Min cacheable prefix | Notes |
|---|---|---|---|---|
| `openai/*` | automatic, token prefix | `prompt_cache_key`, `prompt_cache_retention: "24h"` — supported by inspect as model_args | 1024 tok **[verify]** | retention default ~5–60 min; confirm 24h retention has no surcharge **[verify]** |
| `anthropic/*` | opt-in `cache_control` markers (inspect inserts) | none (key n/a; 1h TTL unreachable via inspect — no `ttl` on the marker) | model-dependent: 4096 (Haiku 4.5 / Opus-class), 1024–2048 others **[verify]** (numbers from our pilot + COST-OPTIMIZATION.md:102) | write surcharge 1.25×; read 0.1× |
| `google/*` | implicit (Gemini 2.5+), token prefix | none passable via inspect (explicit `CachedContent` API not wrapped — out of scope) | 1024 (Flash) / 2048–4096 (Pro) **[verify]** | implicit caching free writes |
| `grok/*` | automatic, token prefix | none | unknown **[verify]** | |
| `deepseek/*` (via OpenRouter or compatible) | automatic, 64-tok blocks | none | 64 **[verify]** | |
| `together/*` | none documented **[verify]** | none | — | treat as no-cache until verified |
| `mistral/*` | none in inspect provider | none | — **[verify]** | |
| `bedrock/*` | **none via inspect** — provider strips cache fields (`bedrock.py:316-328`); Anthropic-on-Bedrock markers rejected (`anthropic.py:430-431` skips `cache_control` on bedrock/vertex) | none | — | this is why OpenRouter→Bedrock routing silently kills caching |

### OpenRouter vs direct — the three buckets (W1's deliverable, drafted)

`provider_of("openrouter/...") == "openrouter"`, which is **not** in
`BATCH_PROVIDERS` — so batch runs already never go through OpenRouter; this
classification only concerns interactive runs.

**A. OpenRouter direct is fine (no routing object needed)** — single
upstream, or caching is automatic-token-prefix on every upstream:
- `openrouter/openai/*` for *unkeyed* caching (automatic prefix caching
  works; OpenRouter accounts the discount). **[verify]** that OpenRouter
  still lists only OpenAI/Azure upstreams for these and both cache.
- `openrouter/x-ai/*` (single upstream) **[verify]**.

**B. OpenRouter works but MUST pin the upstream (`provider_routing`)**:
- `openrouter/anthropic/*` with caching on — OpenRouter may route to
  Bedrock/Vertex, which **ignore the markers** (our live finding,
  FUTURE.md:169-170). Pin `{"order": ["anthropic"], "allow_fallbacks":
  false}` **[verify** OpenRouter's current slug for the Anthropic upstream**]**.
- `openrouter/deepseek/*` and other multi-host open models — only the
  first-party upstream caches; pin it or accept `cache_read=0`.
- `openrouter/google/*` if Vertex vs AI-Studio upstreams differ on implicit
  caching **[verify]**.

**C. Must NOT use OpenRouter (direct API required)**:
- OpenAI **keyed** caching (W2): `prompt_cache_key`/`prompt_cache_retention`
  are OpenAI request fields; assume OpenRouter does not forward them
  **[verify — single targeted test or OpenRouter docs check; if it does
  forward, move to bucket B]**.
- Any provider batch API (already structurally true, see above).
- Anthropic if you ever need >5min TTL (not reachable through inspect at
  all today — direct or not; documented limitation).

The implementing session should land this three-bucket table (verified) in
`docs/COST-OPTIMIZATION.md` as a new "OpenRouter or direct?" subsection — the
classification is user-facing guidance, not just plan input.

---

## W1 — provider pinning: `provider_routing`

**Goal.** A config knob that pins OpenRouter's upstream so cached runs don't
silently land on a marker-ignoring host. Near-required for Anthropic-cached
runs via OpenRouter.

**Config** (`src/itemeval/_config.py`). One optional field on
`SolversConfig` **and** `GraderSpec` (judges route too):

```yaml
solvers:
  provider_routing: { order: [anthropic], allow_fallbacks: false }
graders:
  - name: judge
    provider_routing: { order: [anthropic], allow_fallbacks: false }
```

- Type: `dict[str, Any] | None = None` — **verbatim OpenRouter provider
  object**, no renamed keys, no schema invention (pass through don't
  rename; OpenRouter evolves this object). Light validation only: must be a
  dict; warn (not error) at load if no `openrouter/*` model is configured in
  that section (knob would be inert — Law: no silent no-ops).
- **Knob bucket: Optimization** (UX-PATTERNS.md:114-123) with a provenance
  caveat: it does **not** enter condition ids (endpoint identity never has —
  `served_model` drift is handled by the existing endpoint-drift warnings,
  `_driftcheck.py:137-164`). Record it in the manifest's config echo so
  provenance shows what routing was requested; `endpoints_effective`
  already records what actually answered.

**Mechanism.** `model_args_for()` returns `{"provider": cfg_routing}` for
`openrouter/*` models when set; `resolve_model` attaches it. Nothing else
changes — inspect's openrouter provider injects it into `extra_body`
verbatim (`openrouter.py:373-385`).

**Announcement (Law 1).** Not a side effect (request shaping, like
temperature) — no new announcement line. But add the catalogued hint
`anthropic-openrouter-no-split` 's sibling: fire the existing-style hint
**`openrouter-unpinned-cache`** (new stable code, add to UX-PATTERNS hint
catalog + Cost-Savings owning doc) when an `openrouter/anthropic/*` model
runs with caching active (`cache_prompt` resolved on) and no
`provider_routing` — data-derived, one line, never blocks. This is the
productized form of the Bedrock finding.

**Tests** (`tests/test_endpoints.py`, new): pure `model_args_for` cases
(openrouter+routing → dict; direct model + routing → {} + the load-time
warning asserted via config tests); config validation round-trip; hint
detector unit test. No API calls.

**Docs/CHANGELOG.** CHANGELOG `[Unreleased]` Added; COST-OPTIMIZATION.md
three-bucket subsection (above) + failure-modes row gains "fix:
`provider_routing`"; UX-PATTERNS hint catalog row; FUTURE.md tail item
checked off.

---

## W2 — cache key / retention passthrough (all providers)

**Goal.** Same-day cache survival across estimate→pilot→full and stable
routing affinity, wherever the provider supports it. The all-provider answer
is the table above; the implementable surface today is **OpenAI direct
only** — everything else is either automatic (no key exists), unreachable
through inspect (Anthropic TTL, Gemini CachedContent), or unverified via
OpenRouter (bucket C).

**Behavior (no new knob — Optimization bucket says invisible-correct
default):** when the run has cache scheduling active (`budget.cache_schedule
!= "off"`, not batch) and a condition's model is `openai/*` (direct, not via
openrouter):

- attach `prompt_cache_key = f"itemeval/{study}/{condition_id}"` — stable
  across runs and phases of the same study+condition (that's the point:
  pilot warms the full run), and deliberately **not** including `run_id` or
  wave. Granularity is per-condition, not per-cache-group: model_args are
  per-`Model`, and per-sample keys aren't reachable without forking
  (GenerateConfig has no such field). Per-condition is sufficient for
  routing affinity; note this in the code comment.
- attach `prompt_cache_retention = "24h"` **iff** the [verify] step confirms
  it is surcharge-free on current OpenAI pricing. If it has a cost, do NOT
  default it on; park it (a paid default would need gate integration —
  out of scope, note in FUTURE.md instead).

**Mechanism.** Same chokepoint: `model_args_for()` adds the two keys.
The condition_id is per-condition, so `resolve_model`/factory needs the
condition in hand — the runners already call `factory(cond.model, stage)`;
extend the factory signature to take the condition id (or pre-bind via
closure per condition). Keep the change minimal; both runners + the
`ModelFactory` type alias (`generate/_run.py`) move together.

**Announcement.** None (request shaping). JSON/result surface: nothing new —
cache effectiveness is already observable via the existing cache-read
columns and the `cache-zero-reads` hint.

**Tests**: `model_args_for` matrix (openai+scheduling → both keys;
openai+batch → {}; openrouter/openai → {} (bucket C); anthropic → {});
key stability across calls (same study/condition → same string).

**Docs/CHANGELOG**: CHANGELOG Added; COST-OPTIMIZATION mechanism-stack row
for OpenAI gains "keyed + 24h retention (automatic)"; the W2 row of the
provider table lands in the new subsection; FUTURE.md:221-223 item closed
(it's currently phrased "investigate … if supported" — the investigation is
done: supported, model_args).

---

## W3 — cache-aware estimator (no investigation: implement)

**Goal.** When the run will be scheduled into provider caches, the
projection — and therefore the **money gate** — should reflect the
discounted cost instead of overstating it.

**Where.** `src/itemeval/budget/_estimator.py` only (plus pricing helpers
already in place). `cost_usd` already prices cache tokens
(`_pricing.py:198-215`: `cache_read`, `cache_write` params; read defaults
0.1× input, write via `cache_write_default` — 1.25× for Anthropic-style,
0 otherwise). The estimator simply never models a token split. Head text is
reconstructible without inspect: generate head = rendered
`template.text[:idx_of_{input}]` (`generate/_task.py:47-54`), judge head =
rendered `rubric.text[:idx_of_{solution}]` per item
(`grade/_judge.py:61-81`).

**Model (mirror the runtime gating exactly — same predicates, no more):**
caching is projected for a condition iff `budget.cache_schedule != "off"`
**and** the plan is not batch **and** the provider caches (table above)
**and** the relevant head/prefix meets the provider minimum (W4's table —
build W4 first). Then, per cache group (same keys the task builders use,
`generate/_task.py:40-43` group = condition when split head is static, else
item; judge groups by item per `grade/_judge.py:119`):

- group of size *g*: 1 leader call pays `cache_write` on the shared-prefix
  tokens (for Anthropic-style; 0-cost write elsewhere), *g−1* follower calls
  pay `cache_read` on those tokens instead of full input price.
- shared-prefix tokens: with split layouts, `estimate_tokens(head_text)`;
  without split but replications > 1, the **entire rendered prompt** is the
  shared prefix within an item's replication group (token-prefix providers
  cache it; Anthropic monolithic gets markers from inspect's `cache_prompt`
  too). Model both — that matches what the gate actually schedules.
- non-shared (tail) tokens price as today. Output tokens unchanged. Batch
  0.5× and cache discounts never combine (batch ⇒ no scheduling).

**Surfaces (append-only, JSON parity):**
- `ConditionEstimate` gains `cache_read_tokens: int = 0`,
  `cache_write_tokens: int = 0`, `cache_discount_usd: float = 0.0`
  (undiscounted minus discounted, for display); `usd` **becomes the
  discounted figure** (it's the projection users pay).
- `StageEstimate` sums the three; `remaining_usd` (the gate input,
  `cli.py:_run_stage` → `_check_gate`) is therefore discounted — that is
  the feature. Delta-aware paths (`epochs_to_run` subset math,
  `_estimator.py:211-231`) must apply the same per-group split to the
  *remaining* groups only (a partially-complete group's leader may already
  exist; treat any group with ≥1 completed row as warm — followers-only).
- Text line: extend the existing projection line with the discount when
  nonzero, e.g. `projected generate cost: $4.10 (includes −$1.30 provider
  prompt-cache discount; confirm_above_usd: $5.00)`. One line, no new
  interaction strength.
- Honesty note: this is a best-case projection (assumes scheduled hits).
  The existing post-run `cache-zero-reads` hint is the corrective feedback
  loop; mention the pairing in COST-OPTIMIZATION.md. Do **not** add a
  haircut factor (speculative knob).

**Tests** (`tests/test_estimator.py` extensions + maybe
`tests/test_cache_estimate.py`): arithmetic fixtures with mock pricing —
anthropic-style (write surcharge can make tiny groups *more* expensive:
assert the estimator shows that honestly, it's the documented
writes≫reads failure mode), token-prefix free-write case, batch excludes
discount, `cache_schedule: off` excludes, head-below-min excludes (W4),
delta path with a half-complete group. All mock, no APIs.

**CHANGELOG**: user-visible (gate threshold changes meaning) — clear Added
entry; note `usd`-now-discounted explicitly.

---

## W4 — min-cacheable-prefix hint (all providers)

**Goal.** The silent no-op we hit live: split layout on, head below the
provider's minimum, `cache_read=0` forever with no signal. Fire a coded
**hint at estimate time** (it's catalogued already as ☐
`split-head-below-min`, UX-PATTERNS.md:234-245 — hint, not warning, per the
catalog; owning doc Cost-Savings#two-gotchas).

**Minimums table.** New constant in a pricing-adjacent module (suggest
`budget/_pricing.py` or the W1 `_endpoints.py`):
`MIN_CACHEABLE_PREFIX_TOKENS: dict[str, int | Callable[[str], int]]` keyed
by provider, with the Anthropic entry model-aware (4096 Haiku-4.5/Opus-class,
else 1024–2048 — encode what COST-OPTIMIZATION.md:102 records, then
**[verify]** every number against current provider docs and stamp the
checked date in a comment). Providers with no known minimum: omit (no hint
fired — never guess). This table is also W3's eligibility input; **build W4
before W3**.

**Detection** (estimate-time, pure):
- generate + `split_prompt`: head = rendered template head (static per
  condition unless `{id}` precedes `{input}` — reuse the
  `head_is_static` logic, `generate/_task.py:40-43`; when per-item, check
  each item's head). `estimate_tokens(head)` (chars/4 heuristic — fire only
  when **clearly** below: suggested threshold `est < min` with the heuristic
  noted in the hint text, no fudge factor invented).
- grade + `split_rubric`: head varies per item (rubric+problem). Fire when
  any item's head falls below, reporting the count:
  `split-head-below-min: 7/40 judge heads under ~4096 tok (anthropic/...)
  — those groups will not engage the provider cache`.
- Resolve provider via the same direct-vs-openrouter mapping as the facts
  table (an `openrouter/anthropic/*` head obeys Anthropic minimums).
- Emit through the existing hint framework (`_hints.py` detector +
  `est.hints`); hints already flow from `Estimate.hints` and obey the
  ≤2-per-command and stderr rules. **Note:** today `estimate` prints
  hints/warnings but `generate`/`grade` print `est.warnings` only on
  generate (cli `_run_stage` stage branch) — estimate-time *hints* must
  surface on all three commands' runs; wire `est.hints` into `_run_stage`'s
  pre-gate output for both stages (hints, unlike est.warnings, are
  stage-relevant by construction here).

**Tests** (`tests/test_hints.py` + estimator tests): detector unit tests per
provider; static vs per-item generate heads; judge per-item counting;
threshold boundary; no-known-minimum provider → no hint; openrouter/anthropic
mapping.

**Docs/CHANGELOG**: tick the ☐ rows in UX-PATTERNS catalog +
COST-OPTIMIZATION failure-modes "planned" row → live; CHANGELOG Added.

---

## Sequencing (canonical)

1. **W1** — smallest, independent, fixes the live Bedrock footgun; lands the
   `_endpoints.py` chokepoint + `model_args_for` that W2 reuses.
2. **W4** — minimums table + hint; no dependencies; W3 needs the table.
3. **W3** — estimator, consuming W4's table and the facts table's
   provider-caches predicate.
4. **W2** — needs the external [verify] pass (OpenRouter forwarding, 24h
   retention pricing); mechanically trivial once verified.

One commit per workstream (conventional `feat:`), CHANGELOG in the same
commit as the behavior. Run `./.venv/bin/python -m pytest` and ruff
check+format per repo rules; no test may call a paid API — everything above
is designed pure/mock-testable.

## Out of scope (explicitly, to prevent creep)

- Anthropic 1h-TTL cache markers (inspect hardcodes the marker shape — file
  an upstream inspect issue instead; wrap don't fork).
- Gemini explicit `CachedContent` lifecycle management.
- Per-sample / per-cache-group OpenAI keys (needs upstream GenerateConfig
  support).
- Estimator haircut/confidence knobs; any new gate or prompt (UX-PATTERNS:
  money gate stays the only gate).
- Store-level judge dedup and cheap-then-escalate judging (separate tail
  items, not part of this plan).
