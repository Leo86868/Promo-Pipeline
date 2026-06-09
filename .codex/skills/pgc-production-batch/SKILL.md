---
name: pgc-production-batch
description: Use for PGC production autopilot, review batches, top-ups, usage writeback/reverts, Drive handoff, release_candidates registration, freshness checks, and RUN_RECEIPT-backed recovery for Supabase POI asset videos. Triggers on "make 15 POIs", "batch PGC videos", "review first", "write usage", "top up", "revert usage", "production PGC run", "Drive staging", or "release candidate".
---

# PGC Production Batch

Repo skill version: 2026-06-08 production-autopilot contract. This repo copy is
the source of truth for the PGC production workflow. The installed copy under
`~/.codex/skills` should be a symlink or installed copy of this folder; refresh
it with `scripts/install_repo_skills.sh` after workflow changes.

Read `docs/operations/pgc_production_contract.md` when implementing or auditing
behavior. Read `docs/operations/pgc_daily_runbook.md` when explaining the flow
to Leo in non-code terms.

## Target Default

- A normal request such as "make 15 POIs, 3 videos each" means production
  autopilot, not preview-first mode.
- Actual live production and live smoke runs should execute on the VPS
  production worktree, not the local Mac worktree. The VPS has the intended
  compute and production env. Local runs are for code work, dry/read-only
  preflight, and human review artifact inspection.
- The VPS is shared with AIGC/asset-platform jobs. For large batches, inspect
  `uptime` and the top CPU processes first. If heavy AIGC backfill or ffmpeg
  compression is already running, report that the batch may be slow; do not
  kill or pause those jobs unless Leo explicitly asks.
- If a session starts locally and Leo asks for live production, run it on the
  VPS or stop and report that the active shell is not the production runtime.
- Do not stop mid-run for routine production. Render, audit, write usage, and
  register handoff per successful video when the required repo/runtime support
  exists.
- Manual review is an explicit override, e.g. "review first" or "put videos in
  Downloads for review". In review mode, do not write usage or create
  `release_candidates` until Leo explicitly approves the next step.
- Do not add arbitrary large-batch confirmation thresholds. Trust the requested
  batch size unless required inputs are missing or eligibility is insufficient.
- Do not auto top-up failed videos. Report the shortfall; run a top-up batch
  only when Leo asks.

## Current Implementation Boundary

This repo currently has local manifests, usage preview/writeback helpers with
manifest audit and post-write verification, a local release handoff exporter,
`release_candidates` registration with post-insert verification, read-only random
POI selection via `promo.cli.select_batch_pois`, manifest-backed Drive staging
inventory via `promo.cli.prepare_drive_staging`, and render plus manifest-audit
`RUN_RECEIPT.json` emission from `promo.cli.run_batch`. It also has explicit
OAuth Drive upload via `promo.cli.upload_drive_staging`; uploads are private by
default. `promo.cli.run_batch --select-random-pois --production-autopilot` can
select eligible POIs, write `selection_summary.json` and `batch.json`, then
process each audit-passed video through private Drive upload, usage
writeback/verification, `release_candidates` registration/verification, POI
quarantine on usage failure, source-width transition filtering, fail-closed
final-upscale gating before Drive handoff, and receipt updates. The remaining
future autopilot work is receipt-based resume/top-up and live smoke hardening.

When a target behavior is not implemented yet, say so and do not fake it with
unsafe ad hoc live writes. Use the safest current workflow and report the gap.

## Ownership

PGC owns:

- video generation;
- manifest receipt and manifest audit;
- usage writeback to `public.poi_asset_usage_events`;
- durable finished-video registration in `public.release_candidates` once a
  durable `drive:<file_id>` URI exists;
- `RUN_RECEIPT.json` as the batch-level order record.

AIGC/zhongtai owns:

- account assignment;
- distribution and publishing;
- `public.distribution_status`;
- platform metrics.

PGC must not write account distribution state.

## Selection Defaults

- Select POIs randomly with equal weight among eligible POIs. Do not weight by
  active asset count; coverage is the goal.
- Default cooldown is global 3 days by POI, configurable when Leo asks.
- If Leo requests a classification such as EU or gold POIs and the asset
  platform does not expose that field, fail clearly. Do not silently fall back
  to all POIs.
- If not enough POIs pass filters, stop before production and ask Leo whether to
  run the smaller count or wait for zhongtai to add assets.
- Use `python3 -m promo.cli.run_batch --select-random-pois` for normal
  selection plus production runs. Use `promo.cli.select_batch_pois` only for
  manual preflight/debug.

## Active Asset Rule

The asset platform owns which assets are active/eligible and the usage cap.
The PGC paradigm owns how many active assets are enough for the requested video
format.
Selection must apply this threshold to candidate-ready assets, not just raw
active clips. For the current shared-asset path, candidate-ready means the asset
has a ready `text-embedding-3-small` embedding for the active composition
version and can enter semantic retrieval.

For `pgc_65s`, use:

```text
required_active_assets = base_min_assets_for_format + 10 * extra_variations
base_min_assets_for_format = 50
extra_variations = max(videos_per_poi - 1, 0)
```

Examples:

```text
1 video per POI -> 50 active assets
2 videos per POI -> 60 active assets
3 videos per POI -> 70 active assets
4 videos per POI -> 80 active assets
```

Future paradigms such as `pgc_120s` may define different base requirements.

## Source Width Policy

For the controlled low-res transition, PGC uses the shared asset `width` field
as the main source-resolution selector for vertical assets. Do not key this
policy off `height`.

Current transition config:

```text
source_resolution_policy.mode = transition_low_res_only
target_width = 720
tolerance_px = 40
aspect_ratio_min = 1.70
aspect_ratio_max = 1.86
```

This policy must be applied both at POI eligibility time and at
retrieval/download time. Retrieval fallback or reserve padding must not widen
back to mixed 720/1080 assets.

For `pgc_65s`, the normal candidate-ready threshold still applies after the
width policy. Example: `3 videos per POI` needs 70 active, embedding-ready,
policy-matching assets.

When this transition policy is active, final-video upscale is required for
production handoff. The policy is detachable: when zhongtai has enough
1080-width assets or the 720-width pool is drained, switch back to
`best_available` or a future 1080 policy and disable final-upscale requirement.

PGC must not mutate asset-platform quality fields.

## Per-Video Production Order

Target order for each production video:

```text
render MP4
-> audit manifest
-> if final_upscale_required: apply and verify final-video WaveSpeed upscale
-> upload final MP4 to durable storage, currently Drive
-> write usage from manifest
-> verify usage writeback
-> insert release_candidates row
-> verify release_candidates row
-> update RUN_RECEIPT.json
```

Do not create `release_candidates` for local paths, VPS temp paths, or Downloads
paths. `source_output_uri` must be durable, currently `drive:<file_id>`.

Do not write usage before durable upload succeeds. If upload fails, no usage was
spent and no release candidate exists.

If `final_upscale_policy.required = true`, fail closed before Drive upload,
usage writeback, and `release_candidates` unless the final-video upscale was
actually applied and verified. `release_candidates.source_output_uri` must point
to the upscaled Drive URI. Usage still comes from the original manifest because
the asset timeline did not change.

Verified means ffprobe reports the actual final MP4 dimensions match the
configured target, currently 1080x1920 by default.

Current repo support can run selection plus the per-video production order:

```bash
ssh vps
cd /home/deploy/pgc_batch_worktrees/main_20260608T000000Z
set -a
. ./.env
set +a
python3 -m promo.cli.run_batch \
  --select-random-pois \
  --poi-count "$poi_count" \
  --videos-per-poi "$videos_per_poi" \
  --output-dir "$output_dir" \
  --supabase-music-library \
  --production-autopilot
```

For the temporary 720-width source transition, add:

```bash
  --source-resolution-policy-mode transition_low_res_only \
  --source-target-width 720 \
  --source-width-tolerance-px 40
```

This derives `final_upscale_policy.required=true` by default. Configure the
runtime runner before live transition production:

```text
PGC_WAVESPEED_UPSCALE_COMMAND='python3 -m promo.cli.wavespeed_upscale_once --input {input_path} --output {output_path} --env /path/to/wavespeed.env'
```

The env file must provide `WAVESPEED_API_KEY`. If this command is missing,
production autopilot must fail before Drive/usage/release rather than hand off a
raw low-res-source render.

This is the current structured production path. The skill translates Leo's
natural-language request into these flags; the repo does not parse English.
The explicit `--production-autopilot` flag avoids accidental live Drive/Supabase
writes from old render-only commands.

Render speed knobs are repo config, not natural-language policy:

```text
PROMO_RENDER_CONCURRENCY=2
PROMO_RENDER_X264_PRESET=veryfast
PROMO_RENDER_CRF=23
PROMO_RENDER_TIMEOUT_SEC=900
```

Use these only to tune Remotion's current renderer. A future ffmpeg-only
renderer would be a separate implementation path because it must preserve
captions, CTA, audio mix, timeline entries, and manifest/usage semantics.

For repair/debug against a known POI list, `run_batch --batch "$batch_json"` is
still supported.

## Approved Existing Masters Handoff

When Leo says something like "continue the approved batch handoff" for existing
PGC masters, do not require him to recite the workflow. Ask only for the missing
artifact path if it was not provided. The normal artifact is an inventory JSON.

Supported inventory shapes:

- `pgc_drive_staging_inventory`: already normalized for Drive upload.
- `final_masters_inventory`: approved/upscaled masters with
  `upscaled_mp4_path`, `run_manifest_path`, `manifest_id`, `run_id`,
  `source_video_key`, POI fields, and music fields. Normalize this into a
  `pgc_drive_staging_inventory` before upload; use `upscaled_mp4_path` as the
  final `local_output_path`, not the pre-upscale render.

Safe order for approved existing masters:

```text
preflight inventory and MP4 paths
-> audit every run_manifest_path
-> check Supabase for existing usage/release candidate duplicates
-> upload approved masters to private neutral Drive staging
-> generate handoff items / release handoff JSON with drive:<file_id> URIs
-> write usage from manifests and verify
-> register release_candidates and verify
-> stop before distribution_status
```

Do not write usage or `release_candidates` for local Downloads paths or VPS temp
paths. Durable media must be `drive:<file_id>`. Do not write
`distribution_status`; AIGC/zhongtai owns distribution after
`release_candidates`.

The manual repair/debug path can prepare/audit the Drive staging inventory:

```bash
python3 -m promo.cli.prepare_drive_staging \
  --receipt "$run_receipt_json" \
  --output "$inventory_json" \
```

Then upload staged MP4s to Drive with the same OAuth credential shape as AIGC
Main:

```bash
python3 -m promo.cli.upload_drive_staging \
  --inventory "$inventory_json" \
  --output "$uploaded_inventory_json" \
  --handoff-items-output "$handoff_items_json"
```

This uses only manifest-audit-passed videos from the receipt. It uploads into
`AIGC Production Masters/<paradigm>/<date>/<batch_id>/` unless a parent folder
id/name override is provided. Do not use AIGC's public-share upload helper for
PGC final masters.

Once usage has been written and verified, current repo support can explicitly
register approved handoff rows:

```bash
python3 -m promo.cli.register_release_candidates \
  --handoff "$release_handoff_json" \
  --execute
```

Without `--execute`, this command is a dry run. Do not run `--execute` in review
mode unless Leo explicitly approved release registration.

## Manifest Audit

Usage writeback is derived from the manifest. No valid manifest means no usage
writeback and no release candidate.

Before trusting a rendered video, verify:

- manifest exists and has `manifest_id`, `run_id`, POI id/name;
- exactly one rendered output exists for the video slot;
- output has `music_label`;
- every timeline entry has `asset_id` and `occurrence_id`;
- every used `asset_id` exists in `asset_snapshot`;
- `bridge_tail` entries also carry `asset_id`;
- usage events can be derived and event IDs are unique.

Current repo support:

```bash
python3 -m promo.cli.audit_run_manifest "$manifest_path"
```

`promo.cli.usage_events_writeback --execute` runs this production audit before
calling Supabase. If audit fails, it prints the audit JSON and does not write
usage.

## Failure Policy

| Failure | Required behavior |
| --- | --- |
| Render/vendor/transient failure | Retry in code when safe; after retry budget, mark video failed and continue. |
| Manifest audit failure | Do not write usage; do not create release candidate; mark video unsafe and continue. |
| Drive upload failure | Retry in code; after retry budget, do not write usage or release candidate; mark failed handoff and continue. |
| Usage writeback/verification failure | Retry in code; if still failed or unclear, quarantine that POI and do not produce more videos for it. Continue other POIs only if asset usage is POI-isolated; otherwise stop the batch. |
| `release_candidates` insert/verification failure | Retry in code; if still failed, mark handoff failed/retryable and continue. Usage is already correct. |
| Not enough eligible POIs | Stop before production and ask Leo whether to run fewer or wait. |

Retries belong in repo/runtime code, not improvised agent loops. The skill sets
policy; CLIs should implement bounded retries and verification.

## RUN_RECEIPT.json

Every production run must write a batch receipt. Future sessions resume from
the receipt, not chat history.

Minimum fields:

- `batch_id`;
- `paradigm`, e.g. `pgc_65s`;
- request config: requested POIs, videos per POI, filters, cooldown,
  active-asset formula;
- selected/skipped POIs;
- per-video state: render, manifest, Drive upload, usage, release candidate;
- manifest IDs and output URIs;
- usage event counts and verification status;
- release candidate IDs/status when created;
- quarantined POIs;
- summary counts and top-up recommendation.

Do not add a Supabase batch registry now. Use JSON receipts for the batch-level
"order"; keep asset truth in `poi_asset_usage_events`, finished-video truth in
`release_candidates`, and distribution truth in `distribution_status`.

## Review Mode

Review mode is explicit. In review mode:

- produce MP4s/manifests/sidecars;
- copy review artifacts to Downloads if requested;
- generate usage preview and audit summary;
- do not write usage;
- do not upload/register release candidates unless Leo later approves that step.

Clear approval examples:

```text
write usage
approve these for production
register these release candidates
top up the missing videos
```

Ambiguous phrases like "looks good" or "continue" are not enough for live state
changes in review mode.

## Reverts And Smoke Cleanup

First classify Leo's intent. There are two different revert scopes:

- Usage-only revert: remove asset usage ledger rows and recompute asset
  counters, while leaving any finished-video handoff state untouched.
- Full smoke/test cleanup: remove asset usage ledger rows, recompute asset
  counters, and withdraw the matching `release_candidates` row so the test
  video no longer appears as a distributable PGC product.

If Leo says "revert usage", treat it as usage-only unless he also says "clean
up the smoke/test", "remove it from PGC products", "make today only the real
batch", or otherwise indicates full cleanup.

If `source_batch_id`, receipt path, output directory, Drive folder name, or
other run identifier contains `smoke` or `live_smoke`, default to preparing a
full smoke/test cleanup preview instead of silently treating the request as
usage-only. Still stop for explicit approval before updating usage rows or
marking any release candidate `rejected`.

Always inspect usage ledger state and release candidate state separately. Usage
rows being zero does not prove a smoke/test cleanup is complete; an approved
linked `release_candidates` row can still appear in
`release_unassigned_candidates` and remain distributable.

Usage-only revert:

1. Query `public.poi_asset_usage_events` by manifest ID.
2. Query linked `public.release_candidates` by
   `source_video_key` containing the manifest ID.
3. If zero usage rows exist, say no usage revert is needed, but still report
   any linked candidate status and whether it remains in
   `release_unassigned_candidates`.
4. If rows exist, show a revert preview: manifest IDs, event count, affected
   asset count, current usage_count min/max, and any linked release candidate
   status.
5. 🔴 CHECKPOINT · STOP and wait for explicit approval.
6. Revert usage rows and recompute usage counts from the ledger; do not blindly
   subtract counts.
7. Verify manifest usage rows are zero and affected asset counters match the
   remaining ledger.
8. If a linked `release_candidates.status = 'approved'` row remains, explicitly
   report that the video may still appear in `release_unassigned_candidates`.

Full smoke/test cleanup:

1. Query usage rows by manifest ID and linked `release_candidates` by
   `source_video_key`.
2. Query `public.distribution_status` for the linked
   `release_candidate_id`/`candidate_id`.
3. If any distribution row exists, STOP. Report that distribution has already
   claimed the candidate and ask Leo before touching release state.
4. Show a cleanup preview: usage event count, affected asset count, candidate
   ID, current candidate status, Drive URI, and distribution row count.
5. 🔴 CHECKPOINT · STOP and wait for explicit approval.
6. Revert usage rows and recompute usage counts from the ledger. If usage rows
   are already zero, skip the usage revert but continue the release-candidate
   withdrawal after approval.
7. Mark the linked release candidate as withdrawn by updating only:

```sql
update public.release_candidates
set status = 'rejected', updated_at = now()
where candidate_id = '<candidate_id>';
```

8. Verify all of the following:
   - manifest usage rows are zero;
   - affected asset counters match the remaining ledger;
   - the candidate status is `rejected`;
   - the candidate no longer appears in `release_unassigned_candidates`;
   - no `distribution_status` row was created or modified.

Do not delete `release_candidates` rows during cleanup. Use `status =
'rejected'` so the audit trail remains visible while zhongtai no longer sees
the smoke/test video as an approved candidate. Do not delete the Drive file
unless Leo separately asks for media cleanup.

## Danger List

- Do not write usage from an invalid manifest.
- Do not register `release_candidates` without a durable `drive:<file_id>`.
- Do not write `distribution_status` from PGC.
- Do not silently relax POI filters or classification requirements.
- Do not auto top-up unless Leo asks.
- Do not use local Downloads or VPS temp paths as final media URIs.
- Do not continue producing a quarantined POI.
- Do not revert by manual row deletion or count subtraction.
- Do not leave a smoke/test `release_candidates.status = 'approved'` row after
  Leo asks for full cleanup.
- Do not delete release candidate rows for smoke cleanup; mark them `rejected`.
- Do not modify `distribution_status` from PGC cleanup.

## Final Report

Always finish with:

- requested/succeeded/failed video counts;
- selected/skipped POIs and shortage if any;
- quarantined POIs;
- receipt path;
- usage writeback summary;
- release candidate summary;
- review package path only when review/download was requested;
- exact next action if top-up, revert, or handoff repair is needed.

For revert or smoke cleanup close-outs, always include:

- manifest IDs checked;
- usage rows found and usage rows reverted;
- linked release candidate ID, status before, and status after;
- whether `release_unassigned_candidates` was checked;
- whether any `distribution_status` rows caused cleanup to stop;
- whether the remaining state is usage-only reverted or fully removed from PGC
  product candidates.
