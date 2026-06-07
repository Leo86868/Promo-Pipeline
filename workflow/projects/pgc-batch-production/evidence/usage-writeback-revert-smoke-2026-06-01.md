# Usage Writeback + Revert Smoke - 2026-06-01

## Scope

Controlled live Supabase test for one PGC manifest after AIGC/Supabase PR #106
and PR #109 were merged and applied live.

This tested:

- manifest-derived usage event payload from PGC;
- `rpc_record_poi_asset_usage_events(p_payload jsonb)`;
- `poi_asset_assets.usage_count` / `last_used_at` increment;
- `rpc_revert_poi_asset_usage_manifests(p_manifest_ids text[])`;
- usage-count recompute back to baseline after revert.

No bulk writeback was performed.

## Target

```text
POI: Little Palm Island Resort & Spa
poi_id: poi_344b66371fce
manifest_id: manifest_3f6b63c2c4d549869414e632a58d75a4
manifest:
  /Users/leowu/Downloads/preview5_new_pois_20260529T101139Z/little_palm_island_resort__spa/video_001/run_manifest_little_palm_island_resort__spa_65s.json
```

The video was generated with shared-asset semantic retrieval:

```text
retrieval_active: true
retrieval_contract: shared_asset_semantic_candidates_v1
fallback_reason: null
eligible assets: 149
semantic candidates: 25
downloaded candidate clips: 30
```

## Preview

```text
event_count: 17
unique_event_id_count: 17
poi_count: 1
asset_count: 17
assigned_phrase: 16
bridge_tail: 1
```

Baseline live DB:

```text
global usage rows: 0
usage rows for manifest: 0
affected asset rows: 17
affected asset usage_count min/max: 0 / 0
affected asset rows with last_used_at: 0
```

## Writeback

RPC:

```text
rpc_record_poi_asset_usage_events(p_payload jsonb)
```

RPC result:

```text
out_inserted_count: 17
out_duplicate_count: 0
```

Post-write verification:

```text
usage rows for manifest: 17
unique event IDs: 17
unique occurrence IDs: 17
unique assets: 17
assigned_phrase rows: 16
bridge_tail rows: 1
rows with retrieval_contract: 17
affected assets with usage_count=1: 17
affected assets with last_used_at present: 17
```

## Revert

RPC:

```text
rpc_revert_poi_asset_usage_manifests(
  ARRAY['manifest_3f6b63c2c4d549869414e632a58d75a4']
)
```

RPC result:

```text
out_reverted_event_count: 17
out_affected_asset_count: 17
```

Post-revert verification:

```text
usage rows for manifest: 0
global usage rows: 0
affected asset rows: 17
affected asset usage_count min/max: 0 / 0
affected assets with usage_count=0: 17
affected assets with last_used_at null: 17
assets_with_usage_count_gt_0: 0
```

## Result

PASS.

The latest AIGC/Supabase usage writeback and revert path works for one
manifest-derived PGC payload, including a `bridge_tail` occurrence.
