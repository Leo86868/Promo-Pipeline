# Sprint 2 - Observability Prep For Run Manifest

Status: implemented locally

## Objective

Expose the facts a future local `run_manifest` needs without changing
render behavior or adding Supabase integration.

Sprint 2 is a narrow observability sprint:

- sidecar writers should be able to report the exact collision-bumped
  paths they wrote;
- successful rendered variants should expose the final backend output
  location;
- bridge-aware renderer timeline entries should be available in a
  manifest-friendly shape.

## Scope

In scope:

- Keep existing bool-compatible sidecar APIs working.
- Add structured result helpers for sidecar writes.
- Add an optional rendered-output accumulator to the variant loop.
- Add a renderer timeline helper that includes `clip_id`, role, segment,
  trim/display spans, and source duration.
- Add focused unit tests.
- Update the PGC batch-production roadmap status.

Out of scope:

- Writing `run_manifest_*.json`.
- Adding Supabase reads or writes.
- Adding `PipelineRunRequest`.
- Refactoring `steps.py` or threading a context object through the
  pipeline.
- Moving `promo/arsenal` files.
- Changing render output naming semantics.

## Acceptance Criteria

1. Sidecar write paths are observable.
   Verify: a focused test can call a structured sidecar write helper and
   assert the exact base path and collision-bumped path.

2. Existing sidecar bool APIs remain compatible.
   Verify: existing `_write_sidecar(...) is True/False` tests still pass.

3. Successful rendered outputs are observable.
   Verify: a focused variant-loop test passes an accumulator and asserts
   `variant_index`, render path, final backend output path, duration, and
   format mode.

4. Final bridge-aware timeline facts are exposed.
   Verify: a focused renderer test asserts assigned clips and bridge clips
   include `usage_role`, `display_start_sec`, `display_end_sec`,
   `trim_start_sec`, `trim_end_sec`, and `source_duration_sec`.

5. No behavior change to production render flow.
   Verify: existing targeted pipeline and renderer tests pass.

6. Protected untracked files are untouched.
   Verify: `PLANNING.md` and `pgc-pipeline-clean-source-2026-05-19.zip`
   remain untracked and unchanged.

## Grandma Explanation

Before writing the final video receipt, the pipeline needs to tell us
three things clearly:

1. which receipt files it wrote;
2. where the final video was saved;
3. which clip appeared at which time.

This sprint only makes those facts visible. It does not send anything to
Supabase and it does not change how videos are made.
