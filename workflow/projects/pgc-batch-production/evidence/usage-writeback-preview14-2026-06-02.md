# Usage Writeback - Preview14 Review Set

Date: 2026-06-02

Scope: Write approved usage for the 14 completed videos from the partial
5-POI production preview batch.

Review folder:

```text
/Users/leowu/Downloads/pgc_review_20260601T105019Z
```

Batch status:

```text
completed videos: 14 / 15
failed video: sandpearl_resort/video_001
batch exit code: 1
failure: selected clip had 8.04s usable footage for an 8.86s visual assignment
```

Pre-write audit:

```text
MP4 files: 14
run manifests: 14
clip assignment sidecars: 14
tts metrics sidecars: 14
match quality sidecars: 14
duration range: 65.045s to 65.344s
missing asset_id in timeline: 0
missing occurrence_id in timeline: 0
timeline asset_id not in asset_snapshot: 0
bridge_tail missing asset_id: 0
```

Pre-write usage preview:

```text
event_count: 249
unique_event_id_count: 249
poi_count: 5
asset_count: 161
assigned_phrase: 236
bridge_tail: 13
run-local max events per asset: 3
run-local assets over 3 events: 0
```

Writeback:

```text
RPC: rpc_record_poi_asset_usage_events
manifests written: 14
inserted events: 249
duplicate events: 0
```

Live Supabase verification:

```text
target usage rows: 249
target distinct manifest_id: 14
target distinct event_id: 249
target distinct asset_id: 161
target bad occurrence_id rows: 0
target assigned_phrase rows: 236
target bridge_tail rows: 13
total usage rows after writeback: 511
total distinct manifest_id after writeback: 28
total distinct asset_id after writeback: 361
affected assets missing last_used_at: 0
current poi_asset_valid_clips rows: 8328
```

Affected asset usage distribution after writeback:

```text
usage_count = 1: 100 assets
usage_count = 2: 34 assets
usage_count = 3: 27 assets
```

Interpretation:

- These 14 approved preview videos are now reflected in the shared usage ledger.
- The previous approved 14-manifest review set remains written; total retained
  manifest count is now 28.
- Assets reaching `usage_count >= 3` are hidden by `poi_asset_valid_clips`.
- No usage was written for the failed `sandpearl_resort/video_001`.
