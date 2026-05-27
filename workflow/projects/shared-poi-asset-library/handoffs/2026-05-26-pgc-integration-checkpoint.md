# Handoff — Shared POI Assets + PGC Manifest

**Date:** 2026-05-26
**Status:** Shared asset-library planning/checkpoint saved; no PGC runtime integration started.

## Topic

Coordinate the shared POI asset library owned by AIGC Main and consumed by
PGC Pipeline, with special attention to Supabase tables, immutable
storage, PGC-compatible asset reads, local PGC manifests, and future
usage writeback.

## Current PGC Artifacts

- `docs/schemas/shared_poi_asset_library.md`
  - PGC integration notes, not the authoritative AIGC Main schema.
  - Captures PGC consumer requirements: stable `poi_id`, `asset_id`,
    per-POI 4-digit `clip_id`, source storage path/hash, media metadata,
    material analysis, embedding metadata, usage-event writeback, and
    local manifest relationship.
- `docs/schemas/run_manifest.md`
  - Working draft for local `run_manifest_*.json`.
  - Defines local-only manifest shape with nullable `poi_id` and
    `asset_id` for current local-folder runs.
  - Requires final renderer timeline entries, including bridge clips,
    before future usage writeback can be correct.

## Decisions So Far

- AIGC Main owns the shared asset library schema/storage. PGC keeps
  integration notes and future adapters.
- PGC should not implement live Supabase reads or writes yet.
- `poi_id` is the permanent place identity. `canonical_key` and
  `display_name` are mutable labels.
- `asset_id` is the permanent identity for one immutable source media
  object.
- `clip_id` remains a PGC-facing 4-digit handle within one POI, not the
  primary database identity.
- `source_content_hash` is a byte-level guard for exact source clip
  identity, useful for copy verification and duplicate detection. It
  should be indexed, not globally unique, in v1.
- Usage events are authoritative; `usage_count` is a denormalized counter.
- Vectors are deferred. Store embedding-ready metadata first.
- Future representative image / first-frame support should be a later
  roadmap item, not Sprint 1 schema. Keep it 1-to-1 per `asset_id` unless
  a clear product need justifies multiple image variants.

## External AIGC Main Direction

The other repo is expected to proceed with Sprint 1 schema foundation:

- private `poi-assets` bucket decision;
- tables: `poi_asset_pois`, `poi_asset_assets`, `poi_asset_usage_events`;
- `poi_asset_valid_clips` compatibility view;
- idempotent usage-event RPC/trigger;
- no historical media copy yet;
- no vector table yet.

PGC should wait for the final `poi_asset_valid_clips` shape and usage RPC contract
before adding live adapters.

## Next PGC Step

Recommended next PGC sprint: local-only manifest emission.

Scope:

1. Make sidecar writing expose exact written paths, including
   collision-bumped filenames.
2. Expose renderer-ready timeline entries after bridge insertion.
3. Add a small `promo/core/pipeline/run_manifest.py` builder/writer.
4. Emit `run_manifest_*.json` after successful local renders.
5. Add fixture tests where `poi_id` and `asset_id` are `null`.

Explicitly out of scope:

- Supabase reads;
- Supabase writes;
- shared-library asset download;
- usage writeback;
- media copy/backfill.

## Local Worktree Notes

Do not touch these pre-existing untracked files unless the user asks:

- `PLANNING.md`
- `pgc-pipeline-clean-source-2026-05-19.zip`
