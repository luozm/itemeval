# Implementation plan — local-adapter (local-file dataset adapter: parquet/json/jsonl)

**Status: IMPLEMENTED 2026-06-21.** Written 2026-06-21 against inspect_ai
0.3.x (pinned in `uv.lock`); reconstructed as the design record from the shipped
change (the feature was built directly from its BACKLOG entry, removed in the
same change). Read these first, in order:

1. `CLAUDE.md` — repo conventions (uv, src layout, test rules, commit style).
2. `DEVELOPMENT.md` — adapter boundary: adapters return canonical `Item`s and
   never import inspect (the inspect boundary is untouched here).
3. `docs/wiki/Configuration.md` — the `benchmark.adapter` / `mapping` surface.

Scope: 1 workstream. **W1** local-file dataset adapter.

---

## Context: the facts that decide the design

The single most common first question for an eval tool is "my benchmark is a
file on disk, not on HuggingFace." `adapters/_base.py` already defines the
`Adapter` protocol (`resolve_revision` + `load`) and the `get_adapter(name)`
registry; the HF adapter (`adapters/_hf.py`) is the reference implementation, and
its `_record_to_item(record, idx, mapping, source)` already turns a raw record
dict into a canonical `Item` honouring the full `MappingSpec`
(`id`/`input`/`target`/`grading_scheme`/`metadata`, composite ids). A local
adapter is therefore a thin reader in front of that same helper.

The everything-pinned rule: the HF adapter pins a Hub revision (commit SHA) in
`dataset_locks.json`. A local file has no revision, so the analogue is the file's
**content hash** — a changed file is detected and refused rather than silently
used.

### Decisions that won during implementation (deviating from the BACKLOG sketch)

- **`id` is reused as the path, not a new `path` field.** The BACKLOG sketch
  proposed `datasets[].path` for local. The shipped form keeps the existing
  `DatasetSpec.id` and treats it as the file path when `adapter: local` — no
  schema fork, no per-adapter field, and `mapping`/`limit`/`split`/`name` ride
  unchanged. Simpler, and keeps one dataset model.
- **Path resolves absolute-or-CWD-relative, not relative to the config file.**
  The sketch said "relative to the config file." Shipped resolution uses the CWD
  — the same base as the `studies/` output tree — so a path reads the same way it
  is typed at the command line. (Re-examine if a study is ever run from a
  directory other than the one holding its config.)
- **Formats: `.parquet` / `.json` / `.jsonl`** (the sketch also named `.csv`).
  CSV was dropped for v1 — it needs a dtype/whitespace contract the other three
  don't. JSON accepts a top-level list or a `{data|rows: [...]}` envelope;
  parquet round-trips via `to_json` so numpy scalars never leak into
  `Item.metadata` / manifests.

---

## W1 — local-file dataset adapter

**Goal.** `adapter: local` loads a benchmark from a `.parquet`/`.json`/`.jsonl`
file on disk, pinned by content hash, with `mapping`/`metadata` behaving exactly
as for `hf`.

**Config / public surface.** `BenchmarkConfig.adapter` literal `Literal["hf"]` →
`Literal["hf", "local"]` (`_config.py`). No new dataset field — `DatasetSpec.id`
doubles as the path. No new knob, hint, or gate; UX-PATTERNS unaffected (adapters
are not a tracked knob bucket).

**Mechanism.** New `adapters/_local.py` (`LocalAdapter`):

- `resolve_revision(spec)` → sha256 of the file bytes (first 40 hex), raising
  `AdapterError` on a missing file.
- `load(spec, mapping, revision)` re-hashes and refuses a mismatch (the file
  changed since lock), reads records by extension, applies `spec.limit`, and maps
  each via the HF adapter's `_record_to_item`. Returns a `LoadedDataset` with
  `adapter="local"`, `cache="local"`, and `cache_dir`/`download_bytes` from the
  file.
- `get_adapter` (`adapters/_base.py`) dispatches `"local"` and names it in the
  unknown-adapter error (`available: hf, local`).

**UX contract.** No announcement/hint/gate beyond the existing adapter-load path.
The hash-mismatch error tells the user how to re-pin (delete the dataset's
`dataset_locks.json` entry). Pure additive — existing `hf` configs unchanged.

**Tests.** `tests/test_adapter_local.py` (hermetic, tmp files, no network):
registry resolves `local`; parquet load + content-hash pin; hash mismatch
refused; changed-file-after-lock detected; json list; `limit`; missing file;
unsupported extension.

**Docs/CHANGELOG.** `[Unreleased]` `### Added` with `Closes: local-adapter`;
`docs/wiki/Configuration.md` adapter line + a "Local datasets" bullet; this
BACKLOG section removed in the same change (the `github-adapter` cross-refs are
kept as prose, since that key still builds on this reader).

---

## Out of scope (explicitly)

- **CSV support** — deferred (dtype/whitespace contract); the sketch's `.csv` is
  not shipped. Add when a consumer needs it.
- **Glob (`data/*.jsonl`)** — one file per dataset entry for now (the BACKLOG
  open question).
- **The hermetic end-to-end CLI smoke CI follow-on** that the BACKLOG entry
  bundled (mock models + a committed JSONL fixture — the offline run the HF
  adapter can't give CI) — NOT shipped here; the adapter is covered by the unit
  tests above. Still worth doing; re-file from this note if CI offline coverage
  is wanted. See the note in `.github/workflows/ci.yml`.
- **`github-adapter`** — a separate BACKLOG key that delegates parsing to this
  reader.
