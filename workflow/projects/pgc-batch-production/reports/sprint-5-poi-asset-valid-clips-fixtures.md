# Sprint 5 `poi_asset_valid_clips` Fixture Adapter Evidence

Date: 2026-05-27

## What Changed

- Added `promo/core/pipeline/poi_asset_valid_clips.py`.
- Added fixture tests in
  `promo/tests/unit/pipeline/test_poi_asset_valid_clips.py`.
- Updated PGC docs/roadmap to use AIGC Main's hard-switch names:
  - `public.poi_asset_pois`;
  - `public.poi_asset_assets`;
  - `public.poi_asset_valid_clips`.

## Runtime Meaning

PGC now has a pure adapter layer for rows shaped like
`public.poi_asset_valid_clips`. It validates the shared-library identity
contract and returns manifest-ready rows for `asset_snapshot`.

The adapter does not query Supabase. It is safe fixture infrastructure for
the later live adapter.

## Verified Behaviors

- Required identity/storage/hash fields are enforced.
- `source_storage_bucket` must be `poi-assets`.
- `source_storage_path` must be `<poi_id>/clips/<asset_id>.mp4`.
- `source_storage_path` must not include the bucket name.
- Duplicate `clip_id` values inside one POI snapshot fail.
- Shared rows feed `build_run_manifest(shared_assets=...)`.
- Assigned and `bridge_tail` timeline rows both receive `asset_id`.

## Not Changed

- No live Supabase reads.
- No Storage downloads.
- No semantic retrieval from imported asset metadata.
- No usage writeback.

## Verification

```bash
python3 -m compileall -q promo/core/pipeline/poi_asset_valid_clips.py promo/core/pipeline/run_manifest.py promo/tests/unit/pipeline/test_poi_asset_valid_clips.py promo/tests/unit/pipeline/test_run_manifest.py
python3 -m pytest promo/tests/unit/pipeline/test_poi_asset_valid_clips.py promo/tests/unit/pipeline/test_run_manifest.py -q
```

Observed result:

```text
14 passed
```
