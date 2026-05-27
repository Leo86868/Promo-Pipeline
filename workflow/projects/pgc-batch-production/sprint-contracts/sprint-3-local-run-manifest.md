# Sprint 3 - Local Run Manifest Emission

Status: implemented locally; payload refined by Sprint 3b

## Objective

Emit one local `run_manifest_*.json` per `full_pipeline` invocation that
has rendered outputs and durable sidecars.

For this sprint, a "run" means one POI pipeline invocation. A run may
produce multiple variants. A future "batch" manifest would sit above
many POI runs and is out of scope.

## Scope

In scope:

- Add `promo/core/pipeline/run_manifest.py`.
- Build a manifest from local PGC facts:
  - POI labels;
  - run config;
  - clip asset snapshot;
  - rendered output rows;
  - exact sidecar paths;
  - bridge-aware timeline rows.
- Write `run_manifest_<slug>_<dur>s.json` next to the output video and
  existing sidecars, using the same collision-bump behavior.
- Keep shared IDs nullable in local-folder mode.
- Add focused tests.
- Update roadmap/evidence.

Out of scope:

- Supabase reads or writes.
- Shared-library `poi_asset_valid_clips` fetching.
- Batch-level manifest files.
- `PipelineRunRequest`.
- Moving `promo/arsenal`.

## Acceptance Criteria

1. Local manifest file is emitted beside sidecars after rendered outputs
   and sidecars exist.
2. Manifest includes `manifest_id`, `run_id`, `created_at`, `pipeline`,
   `poi`, `run_config`, `asset_snapshot`, `sidecars`, `outputs`, and
   `timeline_entries`.
3. Local mode uses `null` for `poi_id`, `asset_id`,
   `source_storage_bucket`, `source_storage_path`, and
   `source_content_hash`.
4. Each rendered visual occurrence appears once in `timeline_entries`
   with `variant_index`, `occurrence_index`, and `occurrence_id`.
5. Usage events are derived later from the manifest instead of stored as
   placeholder drafts.
6. Existing sidecars remain referenced, not duplicated wholesale.
7. No Supabase integration is added.

## Grandma Explanation

Sprint 2 made the kitchen remember the important facts. Sprint 3 prints
the master receipt for one POI run:

- where the final videos are;
- which sidecar receipts were written;
- which clips appeared at which times;
- which future shared-library IDs are still missing.
