# PGC Batch Production Roadmap

**Last updated:** 2026-05-27
**Status:** In progress - Sprint 5 fixture adapter implemented locally; Sprint 4 planned
**Owner repo:** PGC Pipeline

## Goal

Prepare PGC Pipeline for repeatable batch production of roughly
one-minute promo videos while keeping the codebase understandable and
local-first.

The work has two tracks:

1. add local `run_manifest` support so every rendered output can explain
   which clips/assets appeared where;
2.梳理 the core pipeline code so inputs, outputs, sidecars, and stage
   boundaries are clear before adding shared-library adapters.

## Why This Is A Long Task

This is not just one doc or one code edit. It touches:

- CLI/runtime inputs;
- `full_pipeline` orchestration;
- per-variant execution;
- renderer timeline binding and bridge insertion;
- sidecar writing;
- future shared-library usage writeback.

The safe path is to map the current flow first, then make narrow changes
behind tests.

## Related Artifacts

- `docs/schemas/run_manifest.md`
  - Working draft for local manifest payload.
- `docs/schemas/shared_poi_asset_library.md`
  - PGC integration notes for the external shared asset library.
- `workflow/projects/shared-poi-asset-library/handoffs/2026-05-26-pgc-integration-checkpoint.md`
  - Cross-project checkpoint for AIGC Main/Supabase/storage coordination.

## Current State

- PGC is local-first and currently reads clips through `PromoBackend`.
- Current run observability is split across:
  - `clip_assignments_*.json`;
  - `tts_metrics_*.json`;
  - `match_quality_*.json`.
- Local `run_manifest_*.json` emission is implemented for successful
  rendered outputs with durable sidecars.
- Manifest shape is aligned with the shared-library direction:
  `asset_snapshot` can freeze `poi_asset_valid_clips` rows, timeline entries carry
  stable `occurrence_id`, and placeholder usage-event drafts have been
  removed.
- A pure fixture adapter for `public.poi_asset_valid_clips` validates
  identity/storage/hash rows and projects them into manifest-ready asset
  snapshots. It performs no live Supabase reads or writes.
- Renderer bridge clips are created inside
  `promo/core/render/remotion_renderer.py::_bind_clips_to_narration`.
- Existing bool sidecar APIs remain compatible, and structured sidecar
  helpers now expose exact collision-bumped paths.
- Successful rendered variants can now accumulate final backend output
  locations and bridge-aware timeline facts for manifest emission.
- Shared-library Supabase schema and storage are being handled outside
  this repo. PGC should not add live Supabase reads/writes yet.

## Decisions

- Use a long-task workflow for this broader PGC batch-production cleanup.
- Keep Supabase/storage coordination in the separate
  `shared-poi-asset-library` workflow project.
- Start with a read-only core map/input inventory sprint before changing
  production code.
- Keep manifest implementation local-only until the external usage RPC
  contract is final. `poi_asset_valid_clips` is now the confirmed read surface for
  future adapter fixtures.
- Treat `run_manifest` as a separate module/artifact, but gather its
  facts from production execution.

## Sprint Roadmap

| Sprint | Status | Purpose |
|---|---|---|
| 0 - Checkpoint and contracts | completed | Create shared-library integration notes, local `run_manifest` draft, and cross-project handoff. |
| 1 - Core map and input inventory | completed |梳理 current core flow: entrypoints, inputs, stage outputs, sidecars, render timeline facts, and messy boundaries. No production code changes. |
| 2 - Manifest implementation prep | implemented locally | Make exact sidecar paths, final output locations, and bridge-aware renderer timeline entries observable. Add focused tests. |
| 3 - Local manifest emission | implemented locally | Add `promo/core/pipeline/run_manifest.py` and emit `run_manifest_*.json` for successful local renders with nullable `poi_id`/`asset_id`. |
| 3b - Manifest contract cleanup | implemented locally | Align manifest with `poi_asset_valid_clips` snapshot semantics, add stable `occurrence_id`, remove placeholder usage-event drafts, and add pure event derivation helpers. |
| 4 - Interface cleanup | planned | Reduce messy stage inputs/outputs based on Sprint 1 findings. Keep changes surgical and behavior-preserving. |
| 5 - Shared-library adapter fixtures | implemented locally | Add fixture-based mapping tests for `poi_asset_valid_clips` ingestion and storage-path/hash propagation. No live network. |
| 6 - Live shared-library adapter | deferred | Add live reads/writeback only after fixture contract and external schema stabilize. |

## Sprint 1 Detail — Core Map And Input Inventory

Objective: build a factual map of the current code before refactoring.

Questions to answer:

- What are all CLI inputs to `compile_promo`?
- Which inputs are passed into `full_pipeline`?
- Which stage owns each input?
- What does each stage return?
- Which sidecars are written, and by whom?
- Where is the final renderer timeline assembled?
- Which facts needed by `run_manifest` are currently unavailable?
- Which function signatures are carrying too many unrelated inputs?

Deliverables:

- `workflow/projects/pgc-batch-production/reports/core-input-map.md`
- optional short Sprint 2 contract if the map makes the next change clear.

Verification:

- map references real files/functions;
- no production code changes;
- protected untracked files remain untouched.

## Sprint 2 Detail — Manifest Implementation Prep

Scope:

- update sidecar writing so callers can know exact written paths;
- make successful rendered variant output locations observable;
- expose renderer-ready entries after bridge insertion without changing
  render behavior;
- add tests for collision-bumped sidecar paths and bridge timeline entry
  shape.

Deliverables:

- `workflow/projects/pgc-batch-production/sprint-contracts/sprint-2-observability-prep.md`
- `promo/core/pipeline/sidecar_writer.py`
- `promo/core/pipeline/variant_loop.py`
- `promo/core/render/remotion_renderer.py`
- `promo/tests/unit/pipeline/test_observability_prep.py`

Out of scope:

- writing `run_manifest_*.json`;
- Supabase reads/writes;
- shared-library asset fetching.
- `PipelineRunRequest`.

## Sprint 3 Detail — Local Run Manifest Emission

Scope:

- add local manifest builders/writer in `promo/core/pipeline/run_manifest.py`;
- emit one `run_manifest_<slug>_<dur>s.json` beside output videos and
  sidecars after rendered outputs and durable sidecars exist;
- keep `poi_id`, `asset_id`, storage path, and source hash nullable in
  local-folder mode;
- include output rows, exact sidecar paths, final timeline entries, and
  inert usage-event drafts.

Deliverables:

- `workflow/projects/pgc-batch-production/sprint-contracts/sprint-3-local-run-manifest.md`
- `promo/core/pipeline/run_manifest.py`
- `promo/tests/unit/pipeline/test_run_manifest.py`

## Sprint 3b Detail — Manifest Contract Cleanup

Scope:

- allow `run_manifest.asset_snapshot` to freeze `poi_asset_valid_clips`-style rows;
- add stable `occurrence_id` to every timeline entry;
- propagate `asset_id` into timeline entries by `clip_id`, including
  `bridge_tail` entries;
- remove placeholder `usage_event_drafts`;
- add pure helpers for deriving future usage events without Supabase
  writes.

Deliverables:

- `promo/core/pipeline/run_manifest.py`
- `promo/tests/unit/pipeline/test_run_manifest.py`
- `docs/schemas/run_manifest.md`
- `docs/schemas/shared_poi_asset_library.md`

Out of scope:

- Supabase reads/writes;
- shared-library `poi_asset_valid_clips` fetching;
- batch-level manifest;
- `PipelineRunRequest`.

## Sprint 5 Detail — Shared-Library Adapter Fixtures

Scope:

- add pure helpers for rows from `public.poi_asset_valid_clips`;
- validate required identity fields: `poi_id`, `asset_id`, `clip_id`,
  storage bucket/path, content hash, and duration;
- enforce the confirmed storage convention:
  `source_storage_bucket = "poi-assets"` and
  `source_storage_path = "<poi_id>/clips/<asset_id>.mp4"`;
- sort and validate one-POI snapshots before passing them into
  `run_manifest.asset_snapshot`;
- prove assigned clips and `bridge_tail` clips both receive `asset_id`.

Deliverables:

- `promo/core/pipeline/poi_asset_valid_clips.py`
- `promo/tests/unit/pipeline/test_poi_asset_valid_clips.py`

Out of scope:

- live Supabase reads;
- Storage downloads;
- semantic retrieval from analysis/embedding metadata;
- usage writeback RPC calls.

## Open Items

- Decide whether manifest emission should remain always-on for successful
  local renders or become configurable before broader batch use.
- Wait for an explicit live-read task before adding Supabase access in
  PGC production code.
- Keep semantic retrieval gated until analysis/embedding fields are
  populated in `poi_asset_valid_clips`.

## Guardrails

- Do not touch `PLANNING.md`.
- Do not touch `pgc-pipeline-clean-source-2026-05-19.zip`.
- Do not add live Supabase reads or writes in this repo until explicitly
  requested.
- Prefer small, test-backed changes over broad refactors.
