# Stability 2-POI x 2 Smoke - 2026-05-28

## Scope

Run two fully embedded Supabase-backed POIs with two 65s videos each.

This was a stability repeat after the first two-POI smoke. It did not add a
new helper and did not write Supabase usage events.

## Inputs

```text
run_dir: /home/deploy/pgc_batch_runs/stability_2poi_2x_20260528T113109Z
local_copy: /Users/leowu/Downloads/stability_2poi_2x_20260528T113109Z
target_duration_sec: 65
videos_per_poi: 2
render_concurrency: 4
jobs: 1
```

```text
Alila Ventana Big Sur - Inclusive Resort
poi_id = poi_16062c7710d6
ready assets = 119

1 Hotel Brooklyn Bridge
poi_id = poi_31ee13d1bf71
ready assets = 105
```

Voice and music rotation:

```text
video_001: voice=jarnathan, music=music_swatting_at_flies_06542a3f-cb48-4dfa-b6a6-bf02ded90caf.mp3
video_002: voice=hope, music=music_Davis_Brothers_Band_c4256a53-7b52-4aaa-be48-da37bd28411f.mp3
```

## Result

```text
rendered videos: 4 / 4
errors: 0
tracebacks: 0
pool-exhaustion hard-fails: 0
Supabase usage writes: 0
```

Per-video audit:

```text
1 Hotel Brooklyn Bridge / video_001
  duration: 65.045s
  size: 68.6 MB
  eligible_pool: 105
  semantic_candidates: 26
  downloaded_candidate_clips: 30
  asset_snapshot: 30
  timeline_entries: 18
  bridge_tail: 1
  missing_asset_id: 0
  missing_occurrence_id: 0

1 Hotel Brooklyn Bridge / video_002
  duration: 65.045s
  size: 66.7 MB
  eligible_pool: 105
  semantic_candidates: 25
  downloaded_candidate_clips: 30
  asset_snapshot: 30
  timeline_entries: 16
  bridge_tail: 1
  missing_asset_id: 0
  missing_occurrence_id: 0

Alila Ventana Big Sur - Inclusive Resort / video_001
  duration: 65.045s
  size: 75.5 MB
  eligible_pool: 119
  semantic_candidates: 24
  downloaded_candidate_clips: 30
  asset_snapshot: 30
  timeline_entries: 17
  bridge_tail: 0
  missing_asset_id: 0
  missing_occurrence_id: 0

Alila Ventana Big Sur - Inclusive Resort / video_002
  duration: 65.045s
  size: 74.4 MB
  eligible_pool: 119
  semantic_candidates: 26
  downloaded_candidate_clips: 30
  asset_snapshot: 30
  timeline_entries: 18
  bridge_tail: 1
  missing_asset_id: 0
  missing_occurrence_id: 0
```

Manifest and usage-preview audit:

```text
manifests: 4
timeline_entries: 69
missing asset_id: 0
missing occurrence_id: 0
timeline assets not in asset_snapshot: 0
bridge_tail entries: 3
bridge_tail missing asset_id: 0
usage preview events: 69
unique event IDs: 69
unique assets: 53
assigned_phrase events: 66
bridge_tail events: 3
retrieval_contract: shared_asset_semantic_candidates_v1
retrieval_fallback_reason: null
```

Same-POI final assigned asset overlap:

```text
1 Hotel Brooklyn Bridge
  overlap: 8 / 26 unique assets
  Jaccard: 30.8%

Alila Ventana Big Sur - Inclusive Resort
  overlap: 8 / 27 unique assets
  Jaccard: 29.6%
```

## Notes

- Candidate-only download stayed active: each video downloaded 30 clips, not
  the full 105/119 ready-asset pool.
- Brooklyn video 001 hit one F3 retry because a segment needed more visual
  time than the candidate pool could provide. The retry regenerated script/TTS
  and rendered successfully.
- Pause-budget warnings appeared, but every MP4 duration landed at 65.045s.
- The log line `Sprint 12b retrieval disabled` refers to the old local
  `clips_dir` retrieval seam. The shared-asset semantic candidate path was
  active and recorded in the sidecars.
- VPS temporary overlay restored cleanly after the run.
