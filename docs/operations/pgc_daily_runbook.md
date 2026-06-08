# PGC Daily Production Runbook

This is the human version of the PGC production workflow. The matching agent
skill lives at `.codex/skills/pgc-production-batch/SKILL.md`.

## What This Repo Is

Think of this repo as a preview-video assembly line with receipts.

It can render 65s PGC videos, produce manifests that explain which Supabase
assets were used, preview usage events, and export a local release handoff JSON.
It is not yet a fully automated final distribution system.

The skill is the checklist. The repo CLIs are the machinery. Supabase, Drive,
VPS, and AIGC distribution are outside systems.

## Boundaries Grandma Should Know

Preview render output is not live. You can delete it, reject it, or rerun it.

Usage writeback is live. Once usage is written, Supabase asset counts change and
future selection sees those clips as more used. Do not write usage until the
exact manifests are approved.

Final-master approval is separate from preview approval. If a rendered MP4 is
upscaled, the upscaled file becomes the master. Drive handoff must use the
upscaled master file ID, not the original preview MP4 file ID.

PGC does not publish to accounts. PGC exports handoff JSON; AIGC owns
`release_candidates` and distribution.

Downloads and VPS folders are inspection areas. They are not durable storage
after final masters are staged.

## Daily Flow

1. Pick POIs and target counts.
2. Run read-only preflight:
   - ready assets from `public.poi_asset_valid_clips`;
   - near-exhausted assets;
   - ready embeddings;
   - Music Library tracks long enough for the target duration;
   - current usage state when needed.
3. Stop and review the plan before rendering.
4. Render previews on VPS with `promo.cli.run_batch --jobs 1`.
5. Copy review artifacts to a local `Downloads` package:
   - MP4s;
   - `run_manifest_*.json`;
   - `clip_assignments_*.json`;
   - `tts_metrics_*.json`;
   - `match_quality_*.json`;
   - `batch_run.log`;
   - usage preview JSON;
   - review audit JSON.
6. Audit the review package before asking for human review.
7. Review videos manually.
8. If the videos need final upscaling, upscale approved masters and audit the
   upscaled files.
9. Stage approved final masters to Drive and record raw Drive file IDs.
10. Export release handoff JSON with `drive:<file_id>` URIs.
11. Write usage only after explicit approval.
12. Verify Supabase usage rows and affected asset usage counts.

## Approval Language

These are enough to write usage when the manifest list is clear:

```text
approve all
write usage
write usage for video_001 and video_003
```

These are not enough:

```text
looks good
continue
ship it
```

When language is ambiguous, stop and ask which manifests are approved.

## Quality Notes

A final MP4 can be `1080x1920` even when many source clips are `720x1280` or
`704x1248`. That is a 1080p container, not necessarily native 1080p detail.
For distribution quality, either use higher-resolution source assets or run and
audit a final-master upscale pass.

Music labels must come from the manifest, not filenames or memory. Downstream
distribution uses the formal handoff field.

## Do Not Do

- Do not write usage for preview-only or test renders.
- Do not bypass `public.poi_asset_valid_clips` for production selection.
- Do not keep rendering after usage writeback fails.
- Do not use original preview MP4 Drive IDs after approving upscaled masters.
- Do not manually subtract usage counts when reverting; recompute from the usage
  ledger.
- Do not assume the VPS worktree matches local code. Check it before production
  runs.

## Where Things Live

- Repo skill: `.codex/skills/pgc-production-batch/SKILL.md`
- Skill installer: `scripts/install_repo_skills.sh`
- Main batch CLI: `python3 -m promo.cli.run_batch`
- Usage preview: `python3 -m promo.cli.usage_events_preview`
- Usage writeback: `python3 -m promo.cli.usage_events_writeback --execute`
- Release handoff export: `python3 -m promo.cli.export_release_handoff`
- Review packages: usually under `/Users/leowu/Downloads/`

## Current Gaps

These are still operator or ad hoc steps unless/until the repo grows dedicated
CLIs:

- production preflight over many candidate POIs;
- review package audit command;
- final-master upscale runner;
- final-master audit command;
- Drive staging uploader;
- post-write Supabase verification command.
