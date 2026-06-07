# Usage Writeback + Revert Smoke - 2026-05-29

## Scope

Controlled live Supabase test for one already-rendered PGC manifest.

This tested:

- manifest-derived usage event payload;
- `rpc_record_poi_asset_usage_events(p_payload jsonb)`;
- `poi_asset_assets.usage_count` / `last_used_at` increment;
- `rpc_revert_poi_asset_usage_manifests(p_manifest_ids text[])`;
- recompute back to zero after revert.

## Target

```text
POI: 1 Hotel Brooklyn Bridge
poi_id: poi_31ee13d1bf71
manifest_id: manifest_74456addffcd4ac397d72dd543ed5c51
manifest:
  /Users/leowu/Downloads/stability_2poi_2x_20260528T113109Z/1_hotel_brooklyn_bridge/video_001/run_manifest_1_hotel_brooklyn_bridge_65s.json
```

The video was already generated with shared-asset semantic retrieval:

```text
retrieval_active: true
retrieval_contract: shared_asset_semantic_candidates_v1
fallback_reason: null
eligible assets: 105
semantic candidates: 26
downloaded candidate clips: 30
```

## Preview

```text
event_count: 18
unique_event_id_count: 18
poi_count: 1
asset_count: 18
assigned_phrase: 17
bridge_tail: 1
```

Baseline live DB:

```text
usage rows for manifest: 0
global usage rows: 0
```

## Writeback

Local PGC CLI could build the preview, but local environment did not have
Supabase credentials:

```text
python3 -m promo.cli.usage_events_writeback ... --execute
error: SUPABASE_URL and a Supabase key are required
```

The same manifest-derived payload was then sent through the live Supabase RPC.

RPC result:

```text
out_inserted_count: 18
out_duplicate_count: 0
```

Post-write verification:

```text
usage rows for manifest: 18
distinct assets: 18
assigned_phrase: 17
bridge_tail: 1
rows_with_occurrence_id: 18
rows_with_retrieval_contract: 18
affected assets with usage_count=1: 18
affected assets with last_used_at present: 18
global usage rows: 18
```

## Revert

RPC:

```text
rpc_revert_poi_asset_usage_manifests(
  ARRAY['manifest_74456addffcd4ac397d72dd543ed5c51']
)
```

RPC result:

```text
out_reverted_event_count: 18
out_affected_asset_count: 18
```

Post-revert verification:

```text
usage rows for manifest: 0
global usage rows: 0
affected assets with usage_count=0: 18
affected assets with last_used_at null: 18
assets_with_usage_count_gt_0: 0
```

## Result

PASS.

The writeback and revert RPCs work as a controlled pair. PGC can write one
approved manifest, verify usage counts, and revert by `manifest_id` with asset
counts recomputed from the remaining ledger.
