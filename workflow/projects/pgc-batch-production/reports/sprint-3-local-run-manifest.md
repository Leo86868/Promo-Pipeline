# Sprint 3 Local Run Manifest Evidence

Date: 2026-05-26

Update: Sprint 3b refined the manifest payload after the shared-library
`poi_asset_valid_clips` contract was confirmed. Placeholder usage-event drafts were
removed, `occurrence_id` was added to timeline rows, and shared
`poi_asset_valid_clips` rows can now be snapshotted into `asset_snapshot`.

## What Changed

- Added `promo/core/pipeline/run_manifest.py`.
- Wired `full_pipeline` to emit `run_manifest_<slug>_<dur>s.json`
  after rendered outputs and durable sidecars exist.
- Kept the manifest local-only with nullable shared-library fields.
- Added focused tests in `promo/tests/unit/pipeline/test_run_manifest.py`.
- Updated `docs/schemas/run_manifest.md` and the batch-production roadmap.

## Runtime Meaning

One `run_manifest` represents one POI pipeline invocation. That run may
contain multiple rendered variants. A whole-batch manifest is still a
future artifact and was not added in this sprint.

## Output Location

The manifest is written beside the output video and existing sidecars:

```text
<output-dir>/
  promo_<slug>_v1_<dur>s.mp4
  tts_metrics_<slug>_<dur>s.json
  match_quality_<slug>_<dur>s.json
  clip_assignments_<slug>_<dur>s.json
  run_manifest_<slug>_<dur>s.json
```

The writer uses the same collision-bump convention:

```text
run_manifest_<slug>_<dur>s.json
run_manifest_<slug>_<dur>s-2.json
run_manifest_<slug>_<dur>s-3.json
```

## What The Manifest Contains

- POI display labels and nullable future `poi_id`.
- Run config.
- Local asset snapshot with nullable `asset_id`, storage path, and hash.
- Rendered output rows.
- Exact sidecar paths.
- Final bridge-aware timeline rows.
- Stable `occurrence_id` values for later manifest-derived usage events.

## What Did Not Change

- No Supabase reads or writes.
- No live `poi_asset_valid_clips` adapter. Sprint 5 later added a pure
  fixture adapter only.
- No batch-level manifest.
- No `PipelineRunRequest`.
- No `promo/arsenal` reorganization.

## Verification

Commands run:

```bash
python3 -m compileall -q promo
python3 -m pytest promo/tests/unit/pipeline/test_run_manifest.py promo/tests/unit/pipeline/test_observability_prep.py -q
python3 -m pytest promo/tests/integration/test_compile_promo.py -q
python3 -m pytest promo/tests/unit/render/test_remotion_renderer.py -q
python3 -m pytest promo/tests/unit/assign/test_clip_assigner.py -q
git diff --check
```

Observed results:

- compileall passed with no output
- `7 passed`
- `67 passed`
- `28 passed`
- `57 passed`
- `git diff --check` passed with no output

## Grandma Explanation

The pipeline now prints the master receipt after a successful local run.
That receipt says:

- where the videos are;
- which smaller receipt files were written;
- which source clips appeared at what times;
- which future shared-library IDs are still missing.
