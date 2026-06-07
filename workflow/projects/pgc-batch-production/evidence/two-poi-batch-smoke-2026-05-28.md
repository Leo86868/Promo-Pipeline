# Two-POI Batch Smoke - 2026-05-28

## Scope

Run a production-style batch smoke with two Supabase-backed POIs and two 65s
videos per POI.

This tested:

- `promo.cli.run_batch` expanding a batch JSON into one isolated
  `compile_promo --n-variants 1` subprocess per requested video;
- Supabase `poi_asset_valid_clips` read path;
- Supabase `poi-assets` storage downloads;
- Supabase Music Library BGM duration filtering and track rotation;
- centralized semantic embeddings;
- candidate-only video download;
- local `run_manifest` emission;
- bridge clip `asset_id` preservation.

No Supabase usage writeback was attempted.

## VPS Run

```text
run_dir: /home/deploy/pgc_batch_runs/two_poi_2x_20260528T093517Z
local_copy: /Users/leowu/Downloads/two_poi_2x_20260528T093517Z
target_duration_sec: 65
videos_per_poi: 2
render concurrency: 4
jobs: 1
```

Batch inputs:

```text
Terranea Resort
poi_id = poi_1c7e529f7329
valid ready assets = 78

Marriott Marquis Houston
poi_id = poi_00bf1e49b204
valid ready assets = 105
```

Voice and music rotation:

```text
video_001: voice=jarnathan, music_id=06542a3f-cb48-4dfa-b6a6-bf02ded90caf
video_002: voice=hope, music_id=c4256a53-7b52-4aaa-be48-da37bd28411f
```

## Result

```text
rendered videos: 4 / 4
timeline entries missing asset_id: 0
bridge_tail entries missing asset_id: 0
Supabase writes: 0
```

Per-video summary:

```text
marriott_marquis_houston/video_001
  duration: 65.344s
  size: 71.5 MB
  eligible_pool: 105
  semantic_candidates: 29
  downloaded_candidate_clips: 30
  asset_snapshot: 30
  timeline_entries: 15
  bridge_tail: 0
  voice: jarnathan

marriott_marquis_houston/video_002
  duration: 65.045s
  size: 72.1 MB
  eligible_pool: 105
  semantic_candidates: 29
  downloaded_candidate_clips: 30
  asset_snapshot: 30
  timeline_entries: 18
  bridge_tail: 1
  voice: hope

terranea_resort/video_001
  duration: 65.045s
  size: 67.1 MB
  eligible_pool: 78
  semantic_candidates: 25
  downloaded_candidate_clips: 30
  asset_snapshot: 30
  timeline_entries: 16
  bridge_tail: 1
  voice: jarnathan

terranea_resort/video_002
  duration: 65.045s
  size: 70.2 MB
  eligible_pool: 78
  semantic_candidates: 25
  downloaded_candidate_clips: 30
  asset_snapshot: 30
  timeline_entries: 16
  bridge_tail: 1
  voice: hope
```

Same-POI overlap:

```text
Marriott Marquis Houston
  semantic candidate overlap: 21 / 29 each
  semantic candidate Jaccard: 56.8%
  downloaded pool overlap: 22 / 30 each
  downloaded pool Jaccard: 57.9%
  final assigned asset overlap: 9
  final assigned asset Jaccard: 37.5%

Terranea Resort
  semantic candidate overlap: 14 / 25 each
  semantic candidate Jaccard: 38.9%
  downloaded pool overlap: 19 / 30 each
  downloaded pool Jaccard: 46.3%
  final assigned asset overlap: 8
  final assigned asset Jaccard: 33.3%
```

Script diversity:

```text
Marriott Marquis Houston
  video_001 script words: 140
  video_002 script words: 134
  token overlap: 28.4%

Terranea Resort
  video_001 script words: 140
  video_002 script words: 139
  token overlap: 37.9%
```

Stability:

```text
batch start: 09:35:19 UTC
batch end: 10:01:13 UTC
total serial wall time: about 25m54s
success rate: 4 / 4
errors: 0
tracebacks: 0
render completes: 4
pool-exhaustion hard-fails: 0 per child run
recoverable warnings: 8
F3 retry recoveries: 1
```

Per-video elapsed time:

```text
Terranea video_001: about 5m03s
Terranea video_002: about 5m16s
Marriott video_001: about 7m23s
Marriott video_002: about 8m05s, included one F3 retry
```

## Verification

Remote artifact check:

```text
4 MP4 outputs present
4 run_manifest JSON files present
4 clip_assignment JSON files present
4 TTS metrics JSON files present
4 match_quality JSON files present
```

Remote and local `ffprobe` duration check:

```text
marriott_marquis_houston/video_001/promo_marriott_marquis_houston_video_001_65s.mp4 65.344000
marriott_marquis_houston/video_002/promo_marriott_marquis_houston_video_002_65s.mp4 65.045333
terranea_resort/video_001/promo_terranea_resort_video_001_65s.mp4 65.045333
terranea_resort/video_002/promo_terranea_resort_video_002_65s.mp4 65.045333
```

Manifest and sidecar checks:

```text
timeline entries missing asset_id = 0 across all four videos
bridge_tail entries missing asset_id = 0 across all four videos
asset_snapshot rows = downloaded candidate clips per video
candidate-only download used 30 clips per video, not the full eligible pool
```

Usage event preview:

```text
preview_path: /Users/leowu/Downloads/two_poi_2x_20260528T093517Z/usage_events_preview.json
event_count: 65
unique_event_id_count: 65
poi_count: 2
unique_asset_count: 48
assigned_phrase events: 62
bridge_tail events: 3
retrieval_contract: shared_asset_semantic_candidates_v1
retrieval_fallback_reason: null
Supabase writes: 0
```

Controlled live usage writeback:

```text
manifest:
  terranea_resort/video_001/run_manifest_terranea_resort_65s.json

dry-run summary:
  event_count: 16
  unique_event_id_count: 16
  assigned_phrase events: 15
  bridge_tail events: 1

baseline:
  existing matching usage events: 0
  matching asset rows: 16
  matching assets with usage_count=0: 16

first live RPC call:
  rpc: rpc_record_poi_asset_usage_events
  inserted_count: 16
  duplicate_count: 0

duplicate live RPC call:
  inserted_count: 0
  duplicate_count: 16

post-write live DB check:
  usage event rows for manifest: 16
  rows with occurrence_id: 16
  rows with retrieval_contract: 16
  assigned_phrase events: 15
  bridge_tail events: 1
  matching assets with usage_count=1: 16
  matching assets with last_used_at: 16
```

## Notes

- The batch shell proved the intended one-video-per-subprocess model.
- Music and voice rotation worked.
- Candidate-only download worked across two POIs, not only Terranea.
- Manifest-derived usage preview works from the copied local artifacts. It
  produces one event per rendered visual occurrence and does not write
  Supabase.
- Controlled usage writeback works for one manifest, including idempotent
  duplicate handling and usage_count increments.
- Marriott video 002 hit one Gemini #2 timing failure, then recovered through
  the existing F3 retry path and rendered successfully.
- Current overlap is acceptable for a smoke, but production batches need a
  no-repeat or anti-overlap policy if multiple videos are requested for the
  same POI.
- Freshness is not a manifest responsibility. PGC should write accurate usage
  events; the asset library should update `usage_count` and hide or retire
  overused assets from `poi_asset_valid_clips`.
- `--jobs` remains limited to `1` because Remotion staging still writes into a
  POI-scoped public directory. Parallel same-POI rendering should wait for
  run-scoped staging or another isolation fix.
