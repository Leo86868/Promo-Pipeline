# PGC Batch Production Daily Log

## 2026-06-01

### Done Today

- Ran a controlled live usage writeback + revert smoke after AIGC/Supabase
  PR #106 and PR #109 were merged/applied.
- Target manifest:
  - Little Palm Island Resort & Spa;
  - `manifest_3f6b63c2c4d549869414e632a58d75a4`;
  - 17 manifest-derived usage events;
  - 16 `assigned_phrase`;
  - 1 `bridge_tail`.
- Baseline live DB was clean:
  - global usage rows = 0;
  - target manifest rows = 0;
  - affected asset usage_count min/max = 0 / 0.
- Writeback RPC result:
  - inserted = 17;
  - duplicates = 0.
- Post-write verification:
  - usage rows for manifest = 17;
  - unique occurrence IDs = 17;
  - affected assets with usage_count=1 = 17;
  - affected assets with last_used_at present = 17.
- Revert RPC result:
  - reverted events = 17;
  - affected assets = 17.
- Post-revert verification:
  - global usage rows = 0;
  - target manifest rows = 0;
  - affected assets all restored to usage_count=0;
  - affected assets all restored to last_used_at=null;
  - assets with usage_count > 0 = 0.

### Evidence

- `workflow/projects/pgc-batch-production/evidence/usage-writeback-revert-smoke-2026-06-01.md`

## 2026-05-29

### Done Today

- Ran a five-POI production-style preview batch on the VPS:
  - Little Palm Island Resort & Spa;
  - CIVANA Wellness Resort and Spa;
  - Turquoise Place;
  - Southall Farm & Inn;
  - Blue Hills Ranch.
- All five rendered successfully as 65s videos using:
  - Supabase `poi_asset_valid_clips`;
  - ready centralized embeddings;
  - semantic candidate retrieval;
  - candidate-only clip download;
  - Supabase Music Library BGM;
  - local run manifests and sidecars.
- Copied final artifacts locally:
  - `/Users/leowu/Downloads/preview5_new_pois_20260529T101139Z`
  - `/Users/leowu/Downloads/pgc_65s_review_15_videos_20260529`
- Verified the five new MP4 durations:
  - 65.045s, 65.045s, 65.045s, 65.344s, 65.344s.
- Audited all five manifests:
  - every timeline entry has `asset_id`;
  - every timeline entry has `occurrence_id`;
  - every timeline asset appears in `asset_snapshot`;
  - every `bridge_tail` entry has `asset_id`;
  - no duplicate occurrence IDs.
- Generated local usage preview:
  - 90 usage events;
  - 90 unique event IDs;
  - 90 unique assets;
  - 87 `assigned_phrase`;
  - 3 `bridge_tail`.
- Confirmed live Supabase usage ledger stayed empty:
  - `public.poi_asset_usage_events` rows = 0.

### Notes

- The SSH stream disconnected during the final Blue Hills Ranch item, but the
  VPS process continued and produced the final MP4, manifest, and sidecars.
- One Blue Hills asset download retried once and then succeeded.
- No usage writeback was performed for this batch.

### Evidence

- `workflow/projects/pgc-batch-production/evidence/preview5-new-pois-2026-05-29.md`

## 2026-05-28

### Current Status

PGC has moved past pure local-only proof work:

- 65s Supabase clip + Supabase Music Library render has been proven once.
- Music Library duration filtering is live on the AIGC/Supabase side.
- PGC has read-only support for `public.poi_asset_valid_clips`.
- Local `run_manifest` emission exists.
- `occurrence_id` exists in manifest timeline entries.
- `asset_snapshot` can freeze shared asset rows.
- Bridge clips are represented in final timeline entries.
- Terranea semantic embeddings are ready and PGC can retrieve candidate
  `asset_id` values from Supabase after Gemini #1 script generation.
- Candidate-only download is now implemented for the Supabase shared-asset
  backend: PGC reads metadata/embeddings first, retrieves candidates, and
  downloads only the selected candidate videos.

The important caveat:

- This is still not the final production path because usage writeback is not
  implemented and repeated production batches have not been proven yet.

### Done Today

- Confirmed AIGC embedding contract:
  - `public.poi_asset_embeddings` exists;
  - `embedding_vector extensions.vector(1536)` exists;
  - model is fixed to `text-embedding-3-small` for v1;
  - Terranea has 78 valid clips and 78 ready embedding rows.
- Audited PGC embedding/retrieval code and classified ownership:
  - PGC keeps runtime script query text, timing assignment, render timeline,
    and manifest generation;
  - asset library should own durable scene analysis, embedding text/vector
    generation, embedding backfill, and future retrieval helpers/RPC.
- Hardened shared-asset manifest identity:
  - shared runs fail if final timeline entries cannot resolve `asset_id`;
  - bridge clips are covered by `clip_id -> asset_id`;
  - local/dev runs still allow nullable `asset_id`.
- Added shared-asset preflight:
  - if staged shared clips cannot map to `asset_id`, PGC stops before
    script/TTS/Remotion work.
- Added asset-library-native retrieval dry-run:
  - reads `poi_asset_valid_clips` and `poi_asset_embeddings`;
  - ranks ready assets with text-embedding query vectors;
  - builds a coverage-first Asset Visual Brief with category coverage and a
    category-aware grounding set of concrete visual details.
- Wired Asset Visual Brief into Gemini #1:
  - shared-asset runs send coverage/category/concrete visual grounding instead
    of a raw full clip inventory;
  - asset IDs stay hidden from Gemini #1.
- Wired shared semantic retrieval into `full_pipeline` after Gemini #1:
  - uses script segment text as retrieval queries;
  - requires the ready eligible pool to be greater than 50;
  - records candidate `asset_id` values in clip-assignment sidecar provenance;
  - does not change Gemini #2 assignment yet.
- Added candidate-only download for shared-asset runs:
  - skips full-pool media download for backends that support ready-asset
    metadata plus candidate fetch;
  - downloads only retrieved/padded candidate asset IDs;
  - snapshots only the downloaded candidate rows into `run_manifest`.
- Ran a real VPS Terranea 65s x 2 smoke with Supabase clips, Supabase Music
  Library BGM, centralized embeddings, candidate-only video download, and
  manifest emission.
- Raised the Remotion narration-period BGM ducked volume from `0.08` to `0.18`
  for the next listening test.
- Added multi-track Music Library selection so multi-video runs can rotate
  exact `--supabase-music-id` values instead of repeatedly picking the first
  eligible track.
- Added `promo.cli.run_batch` as a thin batch shell:
  - each requested video becomes one isolated `compile_promo --n-variants 1`
    subprocess;
  - each video gets its own output folder and seed;
  - voices and Music Library track IDs rotate per video;
  - `--jobs` is intentionally limited to `1` for now.
- Ran a real VPS two-POI batch smoke:
  - Terranea Resort x 2;
  - Marriott Marquis Houston x 2;
  - all four outputs rendered from Supabase clips, Supabase Music Library BGM,
    centralized embeddings, candidate-only video download, and manifest
    emission.
- Added read-only usage event preview:
  - derives one event per rendered visual occurrence from `run_manifest`;
  - reads retrieval contract/fallback from the local clip-assignment sidecar
    when available;
  - does not write Supabase.
- Added explicit usage writeback CLI:
  - calls `rpc_record_poi_asset_usage_events` only with `--execute`;
  - sends one RPC array per manifest;
  - remains separate from automatic batch production.
- Ran a controlled live writeback on one Terranea manifest:
  - first RPC call inserted 16 events;
  - duplicate retry inserted 0 and returned 16 duplicates;
  - all 16 matching assets now have `usage_count=1`.
- Ran a second stability repeat on two fully embedded POIs:
  - Alila Ventana Big Sur x 2;
  - 1 Hotel Brooklyn Bridge x 2;
  - all four outputs rendered from Supabase clips, Supabase Music Library BGM,
    centralized embeddings, candidate-only video download, and manifest
    emission;
  - no Supabase usage writeback was attempted.
- Ran a controlled live writeback + revert smoke on one Brooklyn manifest:
  - writeback inserted 18 usage events;
  - affected 18 assets had `usage_count=1` and `last_used_at` populated;
  - revert deleted the 18 events by `manifest_id`;
  - affected assets recomputed back to `usage_count=0` and `last_used_at=null`;
  - global usage ledger returned to 0 rows.
- Recorded Gemini #2 as an open design point:
  - retrieval chooses relevant asset candidates;
  - Gemini #2 still handles phrase tiling, trim starts, duration constraints,
    and bridge-sensitive timing after TTS.

### Verification

Latest local verification after manifest/preflight, visual brief, and semantic
candidate retrieval work:

```bash
python3 -m pytest -q promo/tests/unit/assets/test_retrieval.py \
  promo/tests/unit/script/test_script_generator.py::TestSprint10C1PromptSchema \
  promo/tests/unit/pipeline/test_run_manifest.py \
  promo/tests/unit/pipeline/test_poi_asset_backend.py \
  promo/tests/integration/test_compile_promo.py

# Result: 99 passed

python3 -m pytest -q promo/tests/unit/pipeline/test_run_manifest.py \
  promo/tests/unit/assets/test_retrieval.py \
  promo/tests/unit/script/test_script_generator.py::TestSprint10C1PromptSchema \
  promo/tests/unit/script/test_per_variant_wpm.py \
  promo/tests/unit/pipeline/test_observability_prep.py \
  promo/tests/unit/pipeline/test_poi_asset_backend.py \
  promo/tests/unit/pipeline/test_poi_asset_valid_clips.py \
  promo/tests/integration/test_compile_promo.py

# Result: 119 passed

python3 -m compileall -q promo
# Result: passed

git diff --check
# Result: passed
```

Live VPS smoke, read-only retrieval probe:

```text
POI: Terranea, poi_id=poi_1c7e529f7329
ready assets: 78
script queries: 3
semantic candidates: 17
first candidates: exterior/coastal resort cliff/ocean clips
```

Live VPS candidate-only full run:

```text
POI: Terranea, poi_id=poi_1c7e529f7329
variants: 2
ready assets: 78
downloaded candidate clips: 35
manifest asset_snapshot: 35
timeline entries: 38
timeline entries missing asset_id: 0
bridge_tail entries: 2
outputs:
  - promo_terranea_resort_v1_65s.mp4, 65.045s
  - promo_terranea_resort_v2_65s.mp4, 65.045s
local copy:
  /Users/leowu/Downloads/terranea_elevenlabs_20260528T075937Z
```

Live VPS two-POI batch smoke:

```text
POIs:
  - Terranea Resort, poi_id=poi_1c7e529f7329, ready assets=78
  - Marriott Marquis Houston, poi_id=poi_00bf1e49b204, ready assets=105
videos: 4 / 4 rendered
durations:
  - Marriott video_001: 65.344s
  - Marriott video_002: 65.045s
  - Terranea video_001: 65.045s
  - Terranea video_002: 65.045s
downloaded candidate clips: 30 per video
timeline entries missing asset_id: 0
bridge_tail entries missing asset_id: 0
local copy:
  /Users/leowu/Downloads/two_poi_2x_20260528T093517Z
usage event preview:
  /Users/leowu/Downloads/two_poi_2x_20260528T093517Z/usage_events_preview.json
  65 events, 65 unique event IDs, 48 unique assets
controlled live writeback:
  Terranea video_001 manifest
  inserted=16, duplicate_retry=16, usage_count increments verified
evidence:
  workflow/projects/pgc-batch-production/evidence/two-poi-batch-smoke-2026-05-28.md
```

Live VPS stability repeat:

```text
POIs:
  - Alila Ventana Big Sur - Inclusive Resort, poi_id=poi_16062c7710d6, ready assets=119
  - 1 Hotel Brooklyn Bridge, poi_id=poi_31ee13d1bf71, ready assets=105
videos: 4 / 4 rendered
durations: all 65.045s
downloaded candidate clips: 30 per video
timeline entries: 69
timeline entries missing asset_id: 0
timeline entries missing occurrence_id: 0
bridge_tail entries: 3
bridge_tail entries missing asset_id: 0
usage event preview:
  /Users/leowu/Downloads/stability_2poi_2x_20260528T113109Z/usage_events_preview.json
  69 events, 69 unique event IDs, 53 unique assets
Supabase writes: 0
local copy:
  /Users/leowu/Downloads/stability_2poi_2x_20260528T113109Z
evidence:
  workflow/projects/pgc-batch-production/evidence/stability-2poi-2x-2026-05-28.md
```

Live Supabase writeback + revert smoke:

```text
manifest:
  manifest_74456addffcd4ac397d72dd543ed5c51
POI:
  1 Hotel Brooklyn Bridge, poi_id=poi_31ee13d1bf71
writeback:
  inserted=18, duplicate=0
post-write:
  usage rows=18, affected assets usage_count=1
revert:
  reverted_event_count=18, affected_asset_count=18
post-revert:
  global usage rows=0, affected assets usage_count=0, last_used_at=null
evidence:
  workflow/projects/pgc-batch-production/evidence/usage-writeback-revert-smoke-2026-05-29.md
```

Approved review-set usage writeback:

```text
review folder:
  /Users/leowu/Downloads/pgc_65s_review_15_videos_20260529
manifests written: 14
review videos represented: 15
events inserted: 262
duplicate events: 0
unique assets: 200
post-write usage rows: 262
assets with usage_count:
  1 -> 154
  2 -> 35
  3 -> 6
  4 -> 5
assets hidden from poi_asset_valid_clips by usage_count >= 3: 11
evidence:
  workflow/projects/pgc-batch-production/evidence/usage-writeback-14-manifests-2026-06-01.md
```

Approved preview14 usage writeback:

```text
review folder:
  /Users/leowu/Downloads/pgc_review_20260601T105019Z
batch status:
  14 / 15 videos rendered
  failed: sandpearl_resort/video_001
manifests written: 14
events inserted: 249
duplicate events: 0
unique assets: 161
post-write usage rows: 511
post-write distinct manifests: 28
post-write distinct assets: 361
affected assets with usage_count:
  1 -> 100
  2 -> 34
  3 -> 27
current poi_asset_valid_clips rows: 8328
evidence:
  workflow/projects/pgc-batch-production/evidence/usage-writeback-preview14-2026-06-02.md
```

### Open Risks

- Semantic matching quality is only smoke-tested on Terranea and Marriott
  Marquis Houston. It still needs more POIs after embedding backfill.
- Candidate-only download now works across two POIs, but same-POI multi-video
  batches still need a no-repeat or anti-overlap policy.
- The first default voice-rotation VPS attempt hit missing `torchaudio` for
  the Gemini TTS `kore` path. The successful smoke pinned ElevenLabs
  `jarnathan`.
- Gemini #2 currently owns timing assignment after TTS word timestamps.
  Retrieval and assignment overlap conceptually, but they are not the same
  thing in current code.
- Usage writeback is proven for controlled single-manifest write/revert and for
  the retained 14-manifest review set. It is still not automatic during batch
  production; approval/writeback remains an explicit operator step.
- Freshness is owned by the shared asset library, not by manifest generation.
  PGC should write accurate usage events; the asset library should use
  `usage_count`/status to keep overused assets out of `poi_asset_valid_clips`.

### Next Work

1. Review the four local Downloads videos on phone for creative quality.
2. Add or design same-POI no-repeat / anti-overlap policy for candidate pools
   and final assigned assets.
3. Decide whether the next production loop should remain small-batch approval
   writeback or add a run-local reserved-usage guard for larger batches.
4. Later cleanup:
   - downgrade or remove old short/local assumptions only after the shared
     long-form path is stable;
   - investigate Remotion speed and VPS throughput;
   - decide how batch jobs are activated, e.g. CLI command, batch runner,
     scheduler, or API.
