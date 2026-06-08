---
name: pgc-production-batch
description: Use when running, auditing, approving, upscaling, staging, writing usage for, reverting usage for, enforcing freshness in, or handing off PGC production-style 65s+ batch videos backed by Supabase POI assets, Supabase Music Library, run_manifest JSON, and usage events. Triggers on requests like "run 3 POIs", "batch PGC videos", "write usage", "approve videos", "revert manifest usage", "freshness", "production PGC run", "upscale PGC masters", "Drive staging", or "release handoff".
---

# PGC Production Batch

Repo skill version: 2026-06-08. This repo copy is the source of truth for the
PGC production workflow. The installed copy under `~/.codex/skills` should be a
symlink or installed copy of this folder; refresh it with
`scripts/install_repo_skills.sh` after workflow changes.

This skill is an operator checklist for PGC batch video production. Keep
repeatable business logic in repo CLIs; use this skill to avoid skipping review,
manifest, final-master, and usage-writeback gates.

For human-facing guidance, read `docs/operations/pgc_daily_runbook.md`.

## Defaults

- Treat every batch as preview until Leo explicitly approves usage writeback.
- Do not write usage for test renders by default.
- Skip weak POIs instead of blocking the whole batch.
- `eligible ready assets <= 50` means skip that POI for production.
- Use VPS for real Gemini/TTS/Remotion runs when local Gemini access is blocked.
- Use `--jobs 1` until Remotion staging is run-scoped and parallel-safe.
- Never auto-revert usage. Reverts require explicit approval and a preview.

## STOP Checkpoints

- STOP before render execution: report eligible, skipped, and near-exhausted POIs.
- STOP after artifact copy and manifest audit: ask Leo to review the Downloads
  folder before any usage writeback.
- STOP after ad hoc upscale/audit when final masters differ from rendered MP4s:
  ask Leo to approve final masters before Drive staging or usage writeback.
- STOP before live usage writeback: state manifest IDs, event count, unique
  assets, and that Supabase usage counts will change.
- STOP before revert execution: show the revert preview and wait for explicit
  approval.

## Boundaries

- Preview render output is disposable until approved.
- Usage writeback changes Supabase state. Do not do it from implication.
- Drive staging changes what downstream AIGC/distribution can import. Use final
  master IDs, not preview MP4 IDs, when upscaling was approved.
- PGC exports handoff JSON only. It must not write `release_candidates` or
  `distribution_status` directly.
- Local Downloads and VPS run folders are review/staging surfaces, not durable
  storage after final masters are staged.

## Freshness Protocol

- Production clip selection must read from `public.poi_asset_valid_clips`.
- Do not query `public.poi_asset_assets` directly for production selection unless
  Leo explicitly asks to bypass freshness.
- The asset library hides clips once `usage_count >= 3`.
- Treat `usage_freshness_status = "warning"` or `usage_remaining = 1` as
  near-exhausted, but still selectable.
- Report near-exhausted counts during preflight when those fields are available.
- Avoid large preview batches without usage writeback. Supabase cannot count
  preview-only usage until approved manifests are written.
- Default production rhythm: run a small batch, copy/audit, ask for approval,
  write usage for approved manifests, then continue.
- For large batches before writeback, either split into smaller writeback cycles
  or require a run-local reserved-usage guard.
- If usage writeback fails, stop the production loop and report the affected
  manifests. Do not keep producing as if counts were updated.

## Useful Commands

Semantic retrieval preflight for one POI:

```bash
python3 -m promo.cli.retrieval_dry_run \
  --poi-id "$poi_id" \
  --json
```

Usage preview before any writeback:

```bash
python3 -m promo.cli.usage_events_preview \
  "$manifest_path" \
  --output "$preview_json"
```

Production batch render:

```bash
PROMO_RENDER_CONCURRENCY=4 python3 -m promo.cli.run_batch \
  --batch "$batch_file" \
  --output-dir "$run_dir" \
  --supabase-music-library \
  --jobs 1
```

Approved usage writeback:

```bash
python3 -m promo.cli.usage_events_writeback \
  "$manifest_path" \
  --execute
```

Approved release-candidate handoff export:

```bash
python3 -m promo.cli.export_release_handoff \
  --items "$approved_items_json" \
  --output "$handoff_json" \
  --source-batch-id "$source_batch_id" \
  --approved-at "$approved_at"
```

## Batch Flow

1. Clarify requested POIs and videos per POI. Accept `poi_id` when available.
2. Preflight each POI with read-only checks:
   - ready active assets from `public.poi_asset_valid_clips` only;
   - near-exhausted assets using `usage_freshness_status` / `usage_remaining`
     when available;
   - ready embeddings for those assets;
   - Music Library tracks with `duration_sec >= target_duration_sec`;
   - existing usage state only if needed for the decision.
3. Report skipped POIs before running:
   - POI name / `poi_id`;
   - ready asset count;
   - reason, usually `eligible_assets <= 50`.
4. STOP before render execution and report the production plan.
5. Run only eligible POIs with the batch CLI.
6. After completion, copy only final artifacts to local Downloads:
   - `promo_*_65s.mp4`;
   - `run_manifest_*_65s.json`;
   - `clip_assignments_*_65s.json`;
   - `tts_metrics_*_65s.json`;
   - `match_quality_*_65s.json`;
   - `batch_run.log`.
7. Audit manifests before asking Leo to review:
   - MP4 duration is near target;
   - every `timeline_entry` has `asset_id`;
   - every `timeline_entry` has `occurrence_id`;
   - every timeline `asset_id` exists in `asset_snapshot`;
   - every `bridge_tail` has `asset_id`;
   - every `outputs[]` entry has `music_label` when Supabase Music Library is
     used;
   - usage preview can be generated;
   - report candidate-only download count and fallback reason.
8. STOP and ask Leo to review the local Downloads folder. Do not write usage yet.

## Approved Master Staging

After Leo approves a batch, keep the manifest as the receipt. Do not rely on MP4
filenames alone.

If the batch needs ad hoc final-video upscale:

1. Build or read a manifest-backed inventory that maps each approved MP4 to:
   - `manifest_id`;
   - `source_video_key = manifest:<manifest_id>:variant:<variant_index>`;
   - POI name / `poi_id`;
   - `music_label`;
   - local MP4 path and run manifest path;
   - eventual upscaled local path and Drive file id.
2. Run upscale as an ad hoc post-process only. This is not normal PGC pipeline
   behavior; future production should consume already-upscaled source assets
   from the asset library.
3. Audit final masters before staging:
   - expected count;
   - `1080x1920`;
   - duration near target;
   - audio stream present;
   - no missing manifest mapping.
4. Update the local inventory with final master paths.
5. STOP and ask Leo to approve final masters before staging.

Drive staging is currently not productized in this repo. Until a real uploader
exists, use a controlled manual/ad hoc upload to the neutral PGC staging folder,
then record each resulting raw Drive file id in the inventory/items JSON.

Recommended order after final-master approval:

```text
final masters audited
-> upload to neutral PGC Drive staging
-> export release handoff JSON with drive:<file_id>
-> write approved usage from manifests
-> AIGC imports release_candidates
-> AIGC distribution assigns/deploys to accounts
```

## Release Candidate Handoff

PGC owns final-video production metadata. AIGC owns `release_candidates` storage
and distribution. When exporting approved masters for AIGC import, each handoff
row must include:

- `source_pipeline = "pgc_65s"`;
- `source_video_key = "manifest:<manifest_id>:variant:<variant_index>"`;
- `poi_id` and `poi_name`;
- `source_output_uri = "drive:<file_id>"`;
- `source_run_id` and optional `source_batch_id`;
- `approved_at`;
- `music_label` copied from `run_manifest.outputs[].music_label`.

Use `promo.cli.export_release_handoff` to build this JSON from approved
`run_manifest` files plus Drive file IDs. The exporter is local-only: it does
not write Supabase, deploy Drive files, or change usage counts.

If final masters were upscaled after render, the Drive file id must point to the
upscaled master, not the original rendered MP4.

## Approval And Writeback

Only write usage after clear approval, such as:

```text
approve all
write usage
write usage for video_001 and video_003
```

Support partial approval. Write usage only for approved manifests.

Before writeback:

1. Generate/read usage preview.
2. Report event count, unique assets, role counts, and manifest IDs.
3. State that live Supabase will be changed.
4. STOP and execute only after explicit approval.

For multiple approved manifests, pass all approved manifest paths. The writeback
is batched by manifest/RPC payload and idempotent by `event_id`.

After writeback, verify the approved manifest rows exist in
`public.poi_asset_usage_events` and affected assets have updated `usage_count` /
`last_used_at`. For production batches, write approved usage before starting the
next batch unless Leo explicitly keeps the run preview-only.

## Reverting Usage

If Leo asks to revert past usage counts:

1. First check whether the target manifests were actually written:
   - query `public.poi_asset_usage_events` by `manifest_id`;
   - if there are zero rows, say no revert is needed.
2. If rows exist, show a revert preview:
   - manifest IDs;
   - event count;
   - affected asset count;
   - current usage_count min/max for affected assets.
3. STOP and wait for explicit approval.
4. Use the controlled Supabase/AIGC-side revert flow that, in one transaction:
   - removes or marks only the target manifest events;
   - recomputes `poi_asset_assets.usage_count` from the remaining usage ledger;
   - recomputes `last_used_at` from remaining events.
5. Do not blindly subtract counts if other events may also reference the same
   assets. Recompute from ledger when reverting.

## Failure Handling

| If this happens | Do this |
| --- | --- |
| POI has `eligible_assets <= 50` | Skip that POI and continue with eligible POIs. |
| Music Library has no track with `duration_sec >= target_duration_sec` | Skip that POI and report no eligible music. |
| Retrieval dry run fails or has too few ready embeddings | Skip the POI; do not fall back to direct asset-table selection. |
| Batch CLI exits non-zero | Stop, preserve logs, and report failed POIs/videos. |
| Expected MP4 or manifest sidecar is missing | Stop before review; do not write usage. |
| Manifest audit fails | Stop before review; report the exact failed invariant. |
| Upscale final-master audit fails | Stop before Drive staging; do not export handoff or write usage. |
| Drive staging lacks file IDs | Stop before release handoff; `export_release_handoff` needs `drive:<file_id>`. |
| VPS disconnects or local copy fails | Reconnect and verify files before review; do not infer success from logs alone. |
| Usage preview fails | Stop; do not call the writeback CLI. |
| Usage writeback verification does not match preview | Stop production and report affected manifests. |

## Danger List

- Do not write usage for preview or test renders.
- Do not bypass `public.poi_asset_valid_clips` for production selection.
- Do not keep producing after usage writeback fails.
- Do not write usage when manifest audit fails.
- Do not revert by manually deleting rows or subtracting `usage_count`.
- Do not auto-approve partial batches; ask which manifests Leo accepts.
- Do not infer release-candidate music from filenames. The handoff must carry
  `music_label`.
- Do not run large preview batches without writeback cycles or a run-local
  reserved-usage guard.
- Do not hand off original rendered MP4 Drive IDs when Leo approved upscaled
  masters.

## Final Report

Always finish with:

- rendered/failed/skipped counts;
- local Downloads path;
- manifest audit summary;
- usage preview summary;
- whether Supabase usage was written;
- any POIs skipped for asset-count or embedding readiness;
- final-master/upscale/Drive state when relevant;
- next recommended action.
