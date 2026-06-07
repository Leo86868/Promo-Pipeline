# PGC Batch Production Project

This folder tracks PGC Pipeline work for robust one-minute-plus video
production that consumes the shared asset library cleanly.

This is PGC-only project state. AIGC Main / Supabase / storage work is
coordinated separately, but PGC depends on that asset library becoming
complete.

## Current Waterline

As of 2026-05-28:

- PGC can render a real 65s video from Supabase POI clips and Supabase
  Music Library BGM.
- `run_manifest` exists and records `asset_snapshot`, outputs, sidecars,
  and final timeline entries.
- Shared-asset runs now have local guardrails:
  - preflight stops before expensive script/TTS/render work if staged
    shared clips cannot map to `asset_id`;
  - final manifest still fails closed if a rendered timeline entry,
    including `bridge_tail`, cannot resolve `asset_id`;
  - local/dev runs can still use nullable `asset_id`.
- Terranea and Marriott Marquis Houston have ready centralized embeddings, and
  PGC now records post-script semantic candidate `asset_id` values from
  Supabase.
- Shared-asset Supabase runs can now use candidate-only download: metadata and
  embeddings are read first, semantic retrieval chooses the candidate assets,
  and only those videos are downloaded.
- `promo.cli.run_batch` exists as the first thin batch shell. It expands a POI
  list into one isolated `compile_promo --n-variants 1` subprocess per
  requested video, so each output can get its own script, retrieval pass,
  candidate pool, manifest, voice, and BGM.
- `promo.cli.usage_events_preview` can derive local usage-event preview JSON
  from one or more `run_manifest_*.json` files. It is read-only and does not
  write Supabase.
- `promo.cli.usage_events_writeback` can call the Supabase usage RPC when
  explicitly run with `--execute`.
- A two-POI batch smoke rendered 4 / 4 videos from Supabase assets and
  Supabase Music Library BGM: Terranea Resort x 2 and Marriott Marquis Houston
  x 2.
- A controlled live writeback has been tested for one manifest: 16 events
  inserted, duplicate retry returned 16 duplicates, and 16 asset usage counts
  incremented once.
- This is still a proof path, not the final production shape.

## Not Final Yet

- Candidate-only download has been smoke-tested across Terranea and Marriott
  Marquis Houston, but still needs repeated production-style batches before it
  should be treated as final.
- Batch runner parallelism is intentionally not enabled yet; `--jobs` must be
  `1` until same-POI staging and no-repeat policies are safer.
- Usage writeback exists as an explicit CLI path and has one controlled live
  proof. It is not yet wired automatically into batch production.
- Freshness policy is not implemented in PGC. PGC should emit accurate usage
  events; the shared asset library should update `usage_count` and remove
  overused assets from the active read surface.
- Old local/short-video paths still exist. Do not delete them blindly until
  the shared-asset long-form path is stable under repeated real runs.

## Target Production Shape

The intended PGC flow is:

```text
read asset metadata / embeddings only
-> hard filter eligible assets
-> require a healthy eligible pool, likely > 50 for production POIs
-> build a coverage-first Asset Visual Brief for Gemini #1
-> Gemini #1 writes script
-> embedding retrieval selects roughly 30-35 candidate asset_id values
-> download only those candidate videos
-> TTS produces word timestamps
-> Gemini #2 performs timing assignment for v1
-> render
-> manifest is the final receipt
-> derive usage events from manifest and write back later
```

Expected scale for a 65s video:

- final visual occurrences: roughly 20-25;
- downloaded candidate clips: roughly 30-35;
- eligible asset pool after hard filters: should be comfortably above 50.

The Asset Visual Brief should summarize category coverage, available seconds,
duration mix, shot/motion mix when available, and a category-aware grounding set
of concrete visual details. It should not give Gemini #1 clip IDs or imply that
the sampled details are final clip assignments.

POI facts are a separate lane from visual coverage. Visual claims should come
from asset metadata; non-visual facts should come from verified facts or an
operator brief.

## Key Local Files

- `roadmap.md` - long-running sprint roadmap and current decisions.
- `daily-log.md` - dated project log for quick human review.
- `reports/core-input-map.md` - factual map of current pipeline inputs and
  stage flow.
- `sprint-contracts/` - scoped sprint plans.
- `reports/` - completed sprint reports.

Key code areas:

- `promo/core/poi_asset_backend.py` - read-only Supabase clip backend.
- `promo/core/pipeline/run_manifest.py` - manifest builder and future usage
  event helper surface.
- `promo/core/pipeline/pipeline.py` - main orchestration and shared asset
  preflight.
- `promo/core/assign/clip_retriever.py` - old local embedding retrieval
  prototype.
- `promo/core/assign/clip_assigner.py` - Gemini #2 timing assignment.

Open design point:

- Gemini #2 currently does phrase-level timing assignment after TTS word
  timestamps. If asset retrieval and future "memory" become strong enough, its
  role may shrink later, but it should not be removed until deterministic or
  lighter-weight assignment can cover phrase tiling, trim starts, duration
  constraints, and bridge behavior.

## Current External Dependency

AIGC / asset-library side has backfilled ready embeddings for at least:

```text
Terranea Resort
poi_id = poi_1c7e529f7329

Marriott Marquis Houston
poi_id = poi_00bf1e49b204
```

PGC can now test semantic retrieval against these POIs. Broader production
still depends on all target POIs receiving scene analysis and ready embeddings.

Latest PGC smoke against two POIs:

```text
batch: Terranea Resort x 2, Marriott Marquis Houston x 2
rendered videos: 4 / 4
downloaded candidate clips: 30 per video
timeline entries missing asset_id: 0
bridge_tail entries missing asset_id: 0
usage event preview: 65 events, 65 unique event IDs, 48 unique assets
controlled writeback: one Terranea manifest, 16 inserted, retry 16 duplicates
local copy: /Users/leowu/Downloads/two_poi_2x_20260528T093517Z
```

Minimal batch file shape:

```json
{
  "pois": [
    {
      "poi_id": "poi_1c7e529f7329",
      "name": "Terranea Resort",
      "location": "Rancho Palos Verdes, California"
    }
  ],
  "videos_per_poi": 3,
  "target_duration_sec": 65,
  "voices": ["jarnathan", "hope", "heather"]
}
```

First smoke command shape:

```bash
python3 -m promo.cli.run_batch \
  --batch path/to/batch.json \
  --output-dir path/to/output-root \
  --supabase-music-library \
  --jobs 1
```

## Guardrails

- Do not touch repo-root `PLANNING.md`.
- Do not touch `pgc-pipeline-clean-source-2026-05-19.zip`.
- Keep Supabase writes out of PGC until usage-event RPC payload and operator
  approval are explicit.
- Prefer small, test-backed changes.
