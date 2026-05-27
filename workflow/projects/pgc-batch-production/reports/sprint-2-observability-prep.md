# Sprint 2 Observability Prep Evidence

Date: 2026-05-26

## What Changed

- Added structured sidecar write results in
  `promo/core/pipeline/sidecar_writer.py`.
- Kept old bool sidecar wrappers for existing callers.
- Added optional rendered-output accumulation in
  `promo/core/pipeline/variant_loop.py`.
- Added manifest-friendly renderer timeline projection in
  `promo/core/render/remotion_renderer.py`.
- Added focused tests in
  `promo/tests/unit/pipeline/test_observability_prep.py`.

## What This Enables

A future `run_manifest` writer can now consume:

- exact sidecar paths, including collision-bumped `-2`, `-3`, etc.;
- per-render `render_output_path` and `final_output_path`;
- bridge-aware timeline rows with clip role, segment, trim span, display
  span, source duration, and source path.

## What Did Not Change

- No Supabase reads or writes.
- No `run_manifest_*.json` emission yet.
- No `PipelineRunRequest`.
- No `promo/arsenal` directory move.
- No render naming policy change.

## Verification

Targeted checks run:

```bash
python3 -m compileall -q promo
python3 -m pytest promo/tests/unit/pipeline/test_observability_prep.py -q
python3 -m pytest promo/tests/integration/test_compile_promo.py -k "WriteSidecar or sidecar_filenames_are_poi_scoped or full_pipeline_multi_variant_smoke" -q
python3 -m pytest promo/tests/unit/render/test_remotion_renderer.py -k "BindHappyPath or TailExtensionRegressionGuard or build_props_from_script_takes_assignments_kwarg" -q
python3 -m pytest promo/tests/unit/assign/test_clip_assigner.py -k "write_sidecar_log_reports_variants_count_for_dict_payload or writer_exists_in_compile_promo or clip_assignments_payload_carries_retrieval_contract_soft_hint" -q
```

Observed results:

- compileall passed with no output
- `4 passed`
- `9 passed, 58 deselected`
- `6 passed, 22 deselected`
- `3 passed, 54 deselected`

## Grandma Explanation

The pipeline can now tell us exactly:

1. which receipt files it wrote;
2. where the finished video was saved;
3. which clips appeared on screen, including bridge clips inserted by the
   renderer.

It still does not write the final `run_manifest` receipt. That is the
next sprint.
