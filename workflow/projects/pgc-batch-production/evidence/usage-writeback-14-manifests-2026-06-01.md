# Usage Writeback - 14 Manifest Review Set

Date: 2026-06-01

Scope: Write approved usage for the retained local review manifests behind the
65s PGC review set.

Review folder:

```text
/Users/leowu/Downloads/pgc_65s_review_15_videos_20260529
```

Important shape:

- 15 MP4 review videos exist in the flattened review folder.
- 14 `run_manifest_*.json` files were written because one Terranea candidate
  smoke manifest contains two rendered outputs.
- Writeback was executed from the VPS worktree with service-role Supabase env:

```text
/home/deploy/pgc_batch_worktrees/preview5_20260529T100950Z
```

Pre-write read-only preview:

```text
event_count: 262
unique_event_id_count: 262
poi_count: 9
asset_count: 200
assigned_phrase: 251
bridge_tail: 11
```

Writeback result:

```text
manifests written: 14
inserted events: 262
duplicate events: 0
```

Live Supabase verification:

```text
public.poi_asset_usage_events rows: 262
distinct manifest_id: 14
distinct asset_id: 200
```

Asset usage distribution after writeback:

```text
usage_count = 1: 154 assets
usage_count = 2: 35 assets
usage_count = 3: 6 assets
usage_count = 4: 5 assets
```

Freshness effect:

```text
assets with usage_count >= 3: 11
public.poi_asset_valid_clips rows after writeback: 8355
```

Interpretation:

- The retained review videos are now reflected in the shared usage ledger.
- All 200 used assets have `last_used_at` populated.
- The shared asset library freshness rule is active: the 11 assets with
  `usage_count >= 3` are no longer visible through `poi_asset_valid_clips`.
- No revert was performed.
