# Preview 5 New POIs - 2026-05-29

## Scope

Production-style preview batch for five new POIs using:

- Supabase `poi_asset_valid_clips`;
- centralized ready embeddings;
- candidate-only shared-asset download;
- Supabase Music Library BGM;
- local `run_manifest` plus sidecars;
- no usage writeback.

## Target

```text
VPS run dir:
  /home/deploy/pgc_batch_runs/preview5_new_pois_20260529T101139Z

Local artifact copy:
  /Users/leowu/Downloads/preview5_new_pois_20260529T101139Z

Combined review folder:
  /Users/leowu/Downloads/pgc_65s_review_15_videos_20260529
```

## POIs

```text
Little Palm Island Resort & Spa
  poi_id: poi_344b66371fce
  ready assets: 149

CIVANA Wellness Resort and Spa
  poi_id: poi_abaecf14178d
  ready assets: 124

Turquoise Place
  poi_id: poi_f921f5ba7960
  ready assets: 123

Southall Farm & Inn
  poi_id: poi_f0d7b1d50f3d
  ready assets: 115

Blue Hills Ranch
  poi_id: poi_7ab94f48df36
  ready assets: 113
```

All five POIs passed the current production preflight threshold of more than
50 ready assets.

## Render Result

```text
videos requested: 5
videos rendered: 5
videos failed: 0
videos skipped: 0
```

Durations:

```text
Little Palm Island Resort & Spa: 65.045s
CIVANA Wellness Resort and Spa: 65.045s
Turquoise Place: 65.045s
Southall Farm & Inn: 65.344s
Blue Hills Ranch: 65.344s
```

## Retrieval

```text
Little Palm Island Resort & Spa:
  eligible assets: 149
  semantic candidates: 25
  downloaded clips: 30
  fallback: null

CIVANA Wellness Resort and Spa:
  eligible assets: 124
  semantic candidates: 27
  downloaded clips: 30
  fallback: null

Turquoise Place:
  eligible assets: 123
  semantic candidates: 25
  downloaded clips: 30
  fallback: null

Southall Farm & Inn:
  eligible assets: 115
  semantic candidates: 23
  downloaded clips: 30
  fallback: null

Blue Hills Ranch:
  eligible assets: 113
  semantic candidates: 27
  downloaded clips: 30
  fallback: null
```

Every video used `shared_asset_semantic_candidates_v1`.

## Manifest Audit

```text
Little Palm Island Resort & Spa:
  asset_snapshot: 30
  timeline entries: 17
  unique used assets: 17
  bridge_tail: 1
  missing asset_id: 0
  missing occurrence_id: 0
  timeline assets outside snapshot: 0

CIVANA Wellness Resort and Spa:
  asset_snapshot: 30
  timeline entries: 20
  unique used assets: 20
  bridge_tail: 1
  missing asset_id: 0
  missing occurrence_id: 0
  timeline assets outside snapshot: 0

Turquoise Place:
  asset_snapshot: 30
  timeline entries: 17
  unique used assets: 17
  bridge_tail: 1
  missing asset_id: 0
  missing occurrence_id: 0
  timeline assets outside snapshot: 0

Southall Farm & Inn:
  asset_snapshot: 30
  timeline entries: 17
  unique used assets: 17
  bridge_tail: 0
  missing asset_id: 0
  missing occurrence_id: 0
  timeline assets outside snapshot: 0

Blue Hills Ranch:
  asset_snapshot: 30
  timeline entries: 19
  unique used assets: 19
  bridge_tail: 0
  missing asset_id: 0
  missing occurrence_id: 0
  timeline assets outside snapshot: 0
```

## Usage Preview

Local preview only:

```text
usage event count: 90
unique event IDs: 90
unique assets: 90
POIs: 5
assigned_phrase: 87
bridge_tail: 3
```

Live Supabase verification:

```text
public.poi_asset_usage_events rows: 0
```

No usage writeback was performed.

## Notes

The local SSH stream disconnected during the final Blue Hills Ranch item, but
the VPS process continued and produced the final MP4, manifest, and sidecars.
`ffprobe` verified the Blue Hills MP4 duration as 65.344s.

One Blue Hills asset download retried once and then succeeded. The run did not
fall back away from semantic retrieval.

## Result

PASS.

This proves the current PGC path can render five additional 65s videos across
five new POIs using Supabase assets, Supabase embeddings, candidate-only media
download, Music Library BGM, and manifest-ready usage event previews.
