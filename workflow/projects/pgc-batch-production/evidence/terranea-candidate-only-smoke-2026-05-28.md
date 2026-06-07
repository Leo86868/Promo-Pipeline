# Terranea Candidate-Only Smoke - 2026-05-28

## Scope

Run a real 65s Terranea proof using:

- Supabase `poi_asset_valid_clips`;
- Supabase `poi-assets` storage;
- Supabase Music Library BGM;
- centralized Terranea embeddings;
- candidate-only video download;
- local `run_manifest` emission.

No Supabase usage writeback was attempted.

## VPS Run

```text
run_dir: /home/deploy/pgc_candidate_only_runs/terranea_elevenlabs_20260528T075937Z
poi: Terranea Resort
poi_id: poi_1c7e529f7329
voice: jarnathan
target_duration_sec: 65
variants: 2
render concurrency: 4
```

The default voice-rotation attempt hit missing `torchaudio` on the VPS for the
Gemini TTS `kore` path. The successful smoke pinned ElevenLabs `jarnathan`.

## Result

```text
ready assets: 78
downloaded candidate clips: 35
manifest asset_snapshot: 35
timeline entries: 38
timeline entries missing asset_id: 0
bridge_tail entries: 2
```

Outputs copied locally:

```text
/Users/leowu/Downloads/terranea_elevenlabs_20260528T075937Z/promo_terranea_resort_v1_65s.mp4
/Users/leowu/Downloads/terranea_elevenlabs_20260528T075937Z/promo_terranea_resort_v2_65s.mp4
```

Local `ffprobe`:

```text
promo_terranea_resort_v1_65s.mp4 duration 65.045s size 81667541
promo_terranea_resort_v2_65s.mp4 duration 65.045s size 80853646
```

Variant clip assignment overlap:

```text
variant 1: 18 unique assigned clips
variant 2: 18 unique assigned clips
overlap: 13 clips
v1 only: 0010, 0015, 0038, 0053, 0056
v2 only: 0006, 0019, 0030, 0054, 0070
```

## Notes

- Candidate-only download worked: PGC did not download all 78 Terranea videos.
- Manifest identity worked: every final timeline entry had `asset_id`.
- Bridge clips were included in the manifest with `asset_id`.
- Usage count writeback remains future work.
- The generated sidecar contains correct nested `shared_asset_retrieval`
  provenance. A small local follow-up also synced the older top-level sidecar
  retrieval fields for future runs.
