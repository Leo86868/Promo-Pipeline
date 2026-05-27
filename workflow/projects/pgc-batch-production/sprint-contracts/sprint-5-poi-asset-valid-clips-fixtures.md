# Sprint 5 - `poi_asset_valid_clips` Fixture Adapter

Status: implemented locally

## Objective

Prepare PGC to consume AIGC Main's hard-switched shared asset view:

```text
public.poi_asset_valid_clips
```

This sprint validates and projects fixture rows only. It does not add
live Supabase reads, Storage downloads, or usage writeback.

## Scope

In scope:

- Add pure row-normalization helpers for `poi_asset_valid_clips`.
- Validate required identity and media fields:
  - `poi_id`;
  - `asset_id`;
  - `clip_id`;
  - `source_storage_bucket`;
  - `source_storage_path`;
  - `source_content_hash`;
  - `duration_sec`.
- Enforce the confirmed storage convention:

```text
source_storage_bucket = poi-assets
source_storage_path = <poi_id>/clips/<asset_id>.mp4
```

- Build sorted one-POI snapshots for `run_manifest.asset_snapshot`.
- Add fixture tests proving `asset_id` propagates to assigned clips and
  `bridge_tail` clips.

Out of scope:

- Live Supabase reads.
- Storage downloads or signed URLs.
- Semantic retrieval from `scene_description`, `category`, or embeddings.
- Usage writeback RPC calls.

## Acceptance Criteria

1. The adapter exposes the external view name as
   `POI_ASSET_VALID_CLIPS_VIEW = "poi_asset_valid_clips"`.
2. Rows missing required identity/storage/hash fields fail clearly.
3. Rows that include the bucket name inside `source_storage_path` fail.
4. Duplicate `clip_id` values inside one POI snapshot fail.
5. Valid fixture rows feed `build_run_manifest()` through
   `shared_assets`.
6. Both assigned and `bridge_tail` timeline entries receive `asset_id`.
7. No live Supabase calls are added.
