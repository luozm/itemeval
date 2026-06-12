# Implementation plan — upgrade-automation (dependency-upgrade PRs + a release-timing rule)

**Status: NOT STARTED.** Written 2026-06-12 against the repo's CI as of main
`a176985` (`ci.yml` runs lint + the hermetic test matrix on every
`pull_request`; `release.yml` publishes to PyPI on a published GitHub
Release) and inspect_ai 0.3.239 (pinned in `uv.lock`) — re-verify the pinned
facts below if those moved. This file is the working brief for a fresh
implementation session: it carries all context that session needs. Read
these first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `DEVELOPMENT.md` — the dependency policy, the inspect_ai upgrade pipeline
   this plan automates, and the versioning/release process W2 extends. This
   plan **discharges the written follow-up at DEVELOPMENT.md:76-78** ("Once
   CI exists (ROADMAP M7): add a scheduled weekly GitHub Actions job (or
   Renovate/Dependabot) that opens the upgrade PR automatically; steps 3–5
   run in CI, step 4 stays manual"). M7 shipped 2026-06-10; the condition is
   met and the follow-up is outstanding.
3. `docs/UX-PATTERNS.md` — read for awareness, but note: this plan touches
   **no package surface** (no knobs, no output, no JSON). It is repo-ops
   only; the UX contract's per-workstream questions are answered "n/a — not
   a user-facing feature" throughout, stated explicitly so the checklist is
   discharged, not skipped.
4. This file end-to-end before doing any part.

Scope: 2 workstreams. **W1** automated dependency-upgrade PRs (Dependabot) ·
**W2** release-timing rule (documentation only).

---

## Context: the facts that decide the design

**What exists (verified in-repo 2026-06-12):**

- `DEVELOPMENT.md:8-17` — dependency policy: `pyproject.toml` carries
  **ranges** (the published contract, e.g. `inspect-ai>=0.3.239`); `uv.lock`
  pins **exact versions** and is committed; add/remove only via `uv`, never
  by hand. So an "upgrade" is a **lockfile move**; the pyproject lower bound
  moves only deliberately (when we start *requiring* newer behavior).
- `DEVELOPMENT.md:45-78` — the inspect_ai upgrade pipeline: deliberate,
  never incidental; cadence = each milestone start + before any large paid
  run; steps: read release notes → `uv lock --upgrade-package inspect-ai`
  on a branch → unit tests → **live smoke (manual, costs cents)** → commit
  `chore: bump inspect-ai 0.3.X -> 0.3.Y`. `DEVELOPMENT.md:80-81`: all other
  deps `uv lock --upgrade` **quarterly**, same flow, less scrutiny.
- `.github/workflows/ci.yml` — triggers on `push: branches: [main]` **and
  bare `pull_request`**, so PRs from any actor (including `dependabot[bot]`)
  get the full 3.11/3.12/3.13 lint+test matrix. It runs `uv sync --locked`
  — i.e. CI exercises exactly the artifact an upgrade PR changes (the
  lockfile) — and needs **no repository secrets** (the `network`-marked HF
  test is excluded; everything else is hermetic). This matters because
  Dependabot-triggered workflows run with a read-only/empty secrets context.
- `.github/` has **no** `dependabot.yml` and no Renovate config today.
- `.github/workflows/sync-wiki.yml` publishes `docs/wiki/` on pushes to main
  — dependency PRs never touch `docs/wiki/`, so merging them does not
  republish the wiki. (No interaction; stated to close the question.)
- `DEVELOPMENT.md:83-117` ("Versioning discipline" + release checklist)
  documents **how** to release (pre-1.0 semver, `[Unreleased]` rolls into a
  dated heading, tag → GitHub Release → `release.yml` publishes via trusted
  publishing) but contains **no rule for when** to release. Releases today
  happen when the maintainer feels like it; nothing reminds anyone.

**External facts — [verify] before writing the config (they move):**

- **[verify]** Dependabot's `uv` ecosystem support: GA status, the exact
  `package-ecosystem` value (`"uv"`), and whether it updates `uv.lock` alone
  when `pyproject.toml` carries ranges (wanted) or also rewrites pyproject
  bounds (unwanted — if it does, find the option that pins manifest edits
  off, or fall back to `pip` ecosystem behavior notes / Renovate). Check
  the GitHub Docs page "Dependabot ecosystems and repositories supported".
- **[verify]** Dependabot `groups` semantics: one `updates:` entry per
  (ecosystem, directory) is the rule; separate cadences per dependency are
  NOT supported within one entry — grouping is the supported way to split
  "inspect-ai alone" from "everything else in one PR". Confirm `groups` with
  `patterns`/`exclude-patterns` against current docs.
- **[verify]** That Dependabot opens lockfile-only PRs which trigger the
  existing `pull_request` CI without extra `permissions:` configuration on a
  public repo.

**Design decisions (shared):**

- **Dependabot over Renovate**: GitHub-native, zero app installation, config
  is one committed YAML file. Renovate is more configurable (true per-dep
  schedules) but is rejected as a heavier dependency for a one-maintainer
  repo; revisit only if the [verify] step finds Dependabot's uv support
  inadequate.
- **Cadence mapping**: DEVELOPMENT.md says weekly (inspect-ai) + quarterly
  (rest). Dependabot has no quarterly interval. Decision: one **weekly**
  schedule with two groups — `inspect-ai` always its own PR; everything
  else grouped into a single "dev-and-runtime deps" PR. The quarterly rule
  relaxes to "the grouped PR refreshes weekly; merge it when convenient, at
  least quarterly". Update DEVELOPMENT.md:80 wording to match (W1 docs).
- **The human stays in the loop where the doc says so**: auto-merge is
  explicitly NOT enabled (out of scope below). An inspect-ai PR on green CI
  still needs the manual live smoke per DEVELOPMENT.md step 4 *before the
  next large paid run* — merging on green unit tests is allowed (that is
  already the documented bar for the commit), the smoke gate attaches to
  paid runs, not to the merge.

---

## W1 — automated dependency-upgrade PRs (`.github/dependabot.yml`)

**Goal.** The "will anything hint me?" gap: a weekly bot-opened PR is the
automatic reminder that inspect-ai (the load-bearing, fast-releasing
dependency) moved, with CI's full matrix already run against the bumped
lockfile. Discharges DEVELOPMENT.md:76-78.

**Config / public surface.** No package knob, no output, no JSON — n/a per
UX-PATTERNS (repo ops). New committed file `.github/dependabot.yml` only.

**Mechanism.** One file, roughly (exact keys subject to the [verify] pass):

```yaml
version: 2
updates:
  - package-ecosystem: "uv"          # [verify] ecosystem name + lockfile-only behavior
    directory: "/"
    schedule:
      interval: "weekly"
    groups:
      inspect-ai:                     # the load-bearing dep: always its own PR
        patterns: ["inspect-ai"]
      everything-else:                # one rolling PR for the rest (dev + runtime)
        patterns: ["*"]
        exclude-patterns: ["inspect-ai"]
    commit-message:
      prefix: "chore"                 # matches the conventional-commit style
```

- GitHub Actions versions are a second, trivial entry
  (`package-ecosystem: "github-actions"`, monthly) — the repo already got
  bitten once by a deprecated action major (`checkout@v4` Node20 forced
  deprecation, fixed by hand 2026-06-10); automate that class away in the
  same file.
- No workflow changes: `ci.yml` already runs on `pull_request` with
  `uv sync --locked` and no secrets (Context).
- Rejected generality: a custom scheduled workflow running
  `uv lock --upgrade` + `peter-evans/create-pull-request` (more moving
  parts, a PAT/permissions surface, and it reimplements what Dependabot
  ships) — only fall back to this if the [verify] pass kills the uv
  ecosystem option and Renovate is also rejected.

**UX contract.** n/a (no package surface). The "announcement" is the PR
itself; CHANGELOG is NOT touched by routine lockfile bumps (they are not
user-visible changes to the package) — only an inspect-ai bump that changes
behavior gets a CHANGELOG line per the existing upgrade pipeline.

**Tests.** None executable in-repo (Dependabot config is evaluated by
GitHub). Verification is operational: after merging, confirm in the repo's
Insights → Dependency graph → Dependabot that the config parsed and the
first run is scheduled; confirm the first opened PR triggers the CI matrix.
Record the confirmation in the plan-archive stamp.

**Docs/CHANGELOG.** `DEVELOPMENT.md`: rewrite lines 76-78 from "Once CI
exists … add a scheduled weekly job" to a present-tense description of the
Dependabot setup (weekly inspect-ai PR + grouped rest + actions updates;
step 4's live smoke stays manual, attached to paid runs); adjust line 80's
"quarterly" wording per the cadence-mapping decision. No CHANGELOG entry
(not a package change). No FUTURE.md item exists for this (the follow-up
lived in DEVELOPMENT.md).

---

## W2 — release-timing rule (documentation only)

**Goal.** Make "when do we cut a release?" a written rule instead of
maintainer mood, so the judgment survives sessions and maintainers. No
automation: there is no mechanical signal that cleanly equals "release-worthy"
(a CI nag on `[Unreleased]` size was considered and rejected — it would fire
constantly during active development, training everyone to ignore it).

**Config / public surface.** None. A new short subsection in
`DEVELOPMENT.md` under "Versioning discipline", before the release
checklist.

**Mechanism.** Add the rule (draft to refine in place):

> **When to release** (pre-1.0). Cut a release when any of these holds:
> 1. `[Unreleased]` contains a **semantics change to the machine surface**
>    — gate behavior, exit-code or JSON-field meaning, store/export schema —
>    consuming studies and agents pin against these, and an unreleased
>    semantics change is a drift bomb for `pip install itemeval` users.
> 2. A consuming study needs an `[Unreleased]` feature for a paid run —
>    release rather than having the study pin a git SHA.
> 3. `[Unreleased]` has been accumulating shipped ROADMAP items for more
>    than ~a month — release to keep PyPI within sight of main.
> Fixes for regressions in the latest release go out as a patch release
> immediately, not batched. Minor vs patch per the existing pre-1.0 rules.

The rule is deliberately a judgment aid with three concrete triggers, not a
metric; trigger 1 is the load-bearing one (both 0.2-cycle gate-semantics
changes — gate-on-remaining, gate-on-discounted — would have tripped it).

**UX contract / Tests.** n/a (documentation).

**Docs/CHANGELOG.** The DEVELOPMENT.md subsection IS the deliverable. No
CHANGELOG entry. Optionally one line in `ROADMAP.md`'s Ops list noting the
rule exists (the Ops list currently tracks only the PyPI approval gate).

---

## Sequencing (canonical)

1. **W1** — independent; do the [verify] pass first (it can change the file
   shape or, worst case, the tool choice). One commit:
   `chore: dependabot — weekly inspect-ai + grouped dependency PRs`
   (includes the DEVELOPMENT.md rewrite — the doc and the config must not
   disagree, same commit).
2. **W2** — independent of W1; one commit:
   `docs: release-timing rule in DEVELOPMENT.md`.

Note for the implementing session: the Dependabot config only takes effect
on the **remote default branch** — it is inert until the current local
commit stack is pushed (a user decision: pushing also republishes the wiki
via `sync-wiki.yml`). Land the commits locally; the operational verification
in W1's Tests section happens after the user pushes.

After each step: `./.venv/bin/python -m ruff check . && ./.venv/bin/python
-m ruff format .`, `./.venv/bin/python -m pytest` (trivially unaffected, run
anyway per the standing rule), normative doc updated in the same commit.

## Out of scope (explicitly, to prevent creep)

- **Auto-merge of upgrade PRs** (even on green CI) — the upgrade pipeline is
  deliberately human-merged; revisit only after months of boring green PRs.
- **PyPI publish approval gate** (GitHub `pypi` Environment + required
  reviewer on `release.yml`) — separate ops item, tracked in FUTURE.md §4.1.
- **Renovate** — rejected for now (Context); reconsider only if Dependabot's
  uv support fails the [verify] pass.
- **Automating the release itself** (changelog rolling, version bump, tag) —
  the manual 7-step checklist stays; it is short and runs rarely.
- **CI reminder/nag for release timing** — considered and rejected in W2
  (constant false fires); the written rule is the mechanism.
- **In-package hints for maintainers** — the hint framework is run-time UX
  for study operators (UX-PATTERNS), not a repo-ops channel; never mix them.
