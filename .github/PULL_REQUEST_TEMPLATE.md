<!--
The same-change rule (CLAUDE.md) as a checklist. See CONTRIBUTING.md for the
full lifecycle. Delete rows that don't apply; don't tick a box that isn't true.
-->

## What & why

<!-- One or two sentences. Link the issue if there is one. -->

**Feature key** (if this ships a backlog feature): `<slug>` — else: n/a

## Checklist

- [ ] Conventional-commit title (`feat:`/`fix:`/`docs:`/`test:`/`refactor:`/`chore:`…)
- [ ] `make check` is green locally (lint + fast tests)
- [ ] **Same-change rule** — for any user-visible change, in this PR:
  - [ ] `CHANGELOG.md` `[Unreleased]` updated (with `Closes: <key>` if it ships a backlog feature)
  - [ ] the shipped feature's section is **removed** from `docs/BACKLOG.md`
  - [ ] if it fixes a bug tracked in `docs/KNOWN-ISSUES.md`, that entry is **removed**
  - [ ] wiki (`docs/wiki/`) updated if the change is user-facing
- [ ] **UX contract** — if a user-facing surface changed, the
      [UX-PATTERNS.md](../docs/UX-PATTERNS.md) development checklist passes (no
      silent side effects, consent, hints, knob buckets)
- [ ] Public API / CLI change is intentional and
      `tests/test_public_api_snapshot.py` was updated to match
- [ ] inspect_ai boundary respected if touched (wrap don't fork; pass through
      don't rename; flatten at the public API — see DEVELOPMENT.md)

## Notes for the reviewer

<!-- Anything non-obvious: trade-offs, follow-ups, things deliberately skipped. -->
