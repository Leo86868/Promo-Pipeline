# PGC Daily Production Runbook

This is the human version of the PGC production workflow. The matching agent
skill lives at `.codex/skills/pgc-production-batch/SKILL.md`. The technical
contract lives at `docs/operations/pgc_production_contract.md`.

## Grandma Version

The future default is simple:

```text
Use PGC skill. Make 15 POIs, 3 videos each.
```

That means real production. The system should pick eligible POIs, make videos,
put finished videos somewhere durable, write the asset usage ledger, register
finished videos for zhongtai, and give you a final receipt.

If you want to watch the videos first, say so explicitly:

```text
Make 15 POIs, 3 each, review first.
```

Review mode is the exception, not the default.

## The Three Cabinets

PGC does not need one big database table for the whole batch right now. It uses
three existing places plus one JSON receipt:

- `poi_asset_usage_events`: which assets were used where;
- `release_candidates`: which finished videos are ready for zhongtai;
- `distribution_status`: which account/distribution work happened later;
- `RUN_RECEIPT.json`: the batch order summary.

Grandma translation:

```text
Asset usage goes in one cabinet.
Finished videos go in one cabinet.
Distribution goes in one cabinet.
This batch order lives in a JSON receipt for now.
```

## What PGC Owns

PGC owns making the video and registering that a finished video exists.

PGC should:

- render the MP4;
- make and check the manifest;
- upload the final MP4 to durable storage, currently Drive;
- write asset usage;
- verify usage;
- create a `release_candidates` row;
- write `RUN_RECEIPT.json`.

PGC should not:

- assign videos to accounts;
- publish videos;
- write distribution state.

Zhongtai/AIGC owns distribution after `release_candidates`.

## Normal Production Flow

For each video, the safe order is:

```text
1. make the video
2. check the manifest
3. if required, upscale the final MP4 and verify it
4. upload the final MP4 to Drive
5. write asset usage
6. verify asset usage
7. register the finished video in release_candidates
8. verify the registration
9. update RUN_RECEIPT.json
```

Grandma version:

```text
Make the video.
Check the receipt.
Upscale the finished video first if this run consumed low-res source assets.
Put the final MP4 in the Drive safe.
Write the asset ledger.
Register that zhongtai can use this finished video.
Update the batch order JSON.
```

Current one-command production path:

```bash
ssh vps
cd /home/deploy/pgc_batch_worktrees/main_20260608T000000Z
set -a
. ./.env
set +a
python3 -m promo.cli.run_batch \
  --select-random-pois \
  --poi-count 15 \
  --videos-per-poi 3 \
  --output-dir out/pgc_batch_xxx \
  --supabase-music-library \
  --production-autopilot
```

The skill translates "15 POIs, 3 each" into flags like `--poi-count 15` and
`--videos-per-poi 3`. The repo itself does not parse natural language.

Actual live production and live smoke runs should run on the VPS production
worktree. The local Mac worktree is for code edits, dry/read-only checks, and
inspecting review packages. If an operator is in a local shell, they should SSH
to the VPS before running live production.

The explicit `--production-autopilot` flag is a code-level safety latch. The
skill can add it for normal production requests; old render-only commands stay
render-only.

## VPS Load And Render Speed

PGC shares the VPS with AIGC/asset-platform work. If another job is compressing
or enriching assets, PGC can be slow even when PGC itself is healthy.

Before a large batch, inspect the VPS:

```bash
uptime
ps -eo pid,ppid,stat,pcpu,pmem,comm,args --sort=-pcpu | head -25
```

If AIGC backfill or ffmpeg compression is already using the machine heavily,
either run only a small smoke/top-up or wait for the shared job to finish.

PGC Remotion encoding defaults are tuned for production speed:

```text
PROMO_RENDER_CONCURRENCY=2
PROMO_RENDER_X264_PRESET=veryfast
PROMO_RENDER_CRF=23
PROMO_RENDER_TIMEOUT_SEC=900
```

Grandma version:

```text
The video machine has eight cooking burners. If zhongtai is already using most
of them, PGC will cook slowly. The render knobs above control how fast and how
hard PGC cooks.
```

## Why Manifest Audit Matters

The manifest is the receipt for one video. Usage writeback is created from this
receipt.

If the manifest is wrong, usage writeback would also be wrong. So the rule is:

```text
No valid manifest = no usage writeback.
```

Manifest audit checks things like:

- which POI this video belongs to;
- which source assets appeared in the timeline;
- whether every timeline usage has an asset id;
- whether music label is present;
- whether usage events can be created safely.

Current repo support:

```bash
python3 -m promo.cli.audit_run_manifest run_manifest_*.json
```

`promo.cli.usage_events_writeback --execute` runs this audit before it calls
Supabase. If the audit fails, usage is not written.

## POI Selection

Default selection is random among eligible POIs. Random means equal chance, not
weighted by asset count. This gives broader coverage.

Default cooldown is global 3 days. If a POI had successful PGC production in
the last 3 days, do not pick it again unless Leo changes the cooldown.

Future filters can include things like:

```text
EU POIs
gold POIs
US POIs
hotel POIs
```

If the asset platform does not yet provide the requested classification, the
system should fail clearly instead of quietly using all POIs.

## Active Asset Rule

Zhongtai owns which assets are active and eligible. PGC owns how many active
assets are enough for this video format.
In the current shared-asset path, the production selector checks candidate-ready
assets: active clips that already have ready embeddings and can enter semantic
retrieval.

For current 65s PGC:

```text
required_active_assets = 50 + 10 * extra_variations
```

Examples:

- 1 video per POI needs 50 active assets;
- 2 videos per POI needs 60 active assets;
- 3 videos per POI needs 70 active assets;
- 4 videos per POI needs 80 active assets.

If not enough POIs pass the filters, the system should stop before production
and ask whether to run fewer or wait for zhongtai to add more assets.

## Source Width Transition

For the temporary low-res transition, PGC uses source asset `width` as the main
resolution field. The current policy is:

```text
mode = transition_low_res_only
target_width = 720
tolerance_px = 40
aspect ratio sanity = near 9:16
```

Grandma version:

```text
This is how PGC intentionally eats the old 720-width vertical clips.
It is not a forever rule.
```

The rule applies twice:

- when choosing POIs, the POI must have enough 720-width, active,
  embedding-ready clips;
- when retrieval downloads clips, fallback/reserve clips must still come from
  that same 720-width pool.

If this transition policy is active, final-video upscale is required before
Drive handoff. In production autopilot, the system must stop before Drive,
usage, and `release_candidates` if the WaveSpeed final upscale was not applied
and verified.

Verified means ffprobe reads the finished MP4 and confirms the configured target
dimensions, currently 1080x1920 by default.

Current repo support expects the WaveSpeed runner to be configured as a runtime
command:

```text
PGC_WAVESPEED_UPSCALE_COMMAND='python3 -m promo.cli.wavespeed_upscale_once --input {input_path} --output {output_path} --env /path/to/wavespeed.env'
```

The env file must provide `WAVESPEED_API_KEY`. The command receives
`{input_path}` and `{output_path}` placeholders and must write the verified
upscaled MP4 to `{output_path}`. If this command is missing while final upscale
is required, production fails closed before handoff.

When the old 720-width pool is drained or zhongtai provides only 1080-width
assets, switch the source policy back to `best_available` or a future 1080
policy and disable the final-upscale requirement. PGC should not update
zhongtai asset quality fields directly.

## Failure Rules

If one video render fails, continue and report it later.

If the manifest is broken, keep the MP4 if it exists, but do not write usage and
do not register it as a finished candidate.

If Drive upload fails, retry. If it still fails, do not write usage. The asset
ledger has not been touched yet.

If usage writeback fails or cannot be verified, quarantine that POI. Do not make
more videos for that POI. Other POIs may continue if asset usage is POI-local.

If release candidate registration fails after Drive upload and usage writeback,
report it as retryable. The video exists and the asset ledger is correct.

If the batch asked for 45 videos and 43 succeeded, stop and report 43/45. Do not
auto-spend more compute to top up. Leo can ask for a top-up batch.

## Review Mode

Review mode is explicit. Use it when Leo asks to inspect videos first.

In review mode:

- videos can be copied to Downloads;
- usage preview can be generated;
- no usage is written automatically;
- no release candidate is created automatically.

Ambiguous phrases like "looks good" are not enough for live state changes in
review mode. Ask for a clear command such as `write usage` or `register release
candidates`.

## Durable Paths

VPS run folders are scratch/output areas. Downloads is a human review area.

The durable media URI for release candidates should be:

```text
drive:<file_id>
```

Do not register a release candidate pointing to:

```text
/home/deploy/...
/Users/leowu/...
Downloads
temporary local paths
```

Current repo support can prepare and upload the Drive staging paperwork:

```bash
python3 -m promo.cli.prepare_drive_staging \
  --receipt RUN_RECEIPT.json \
  --output drive_staging_inventory.json

python3 -m promo.cli.upload_drive_staging \
  --inventory drive_staging_inventory.json \
  --output drive_staging_uploaded.json \
  --handoff-items-output handoff_items.json
```

The first command does not upload files to Drive. From a receipt, it uses only
videos whose manifest audit passed. The second command uploads those MP4s to
Drive with OAuth, keeps files private, verifies Drive metadata, and writes the
handoff items needed by the next step.

After that, build and optionally register the release handoff:

```bash
python3 -m promo.cli.export_release_handoff \
  --items handoff_items.json \
  --output release_handoff.json

python3 -m promo.cli.register_release_candidates \
  --handoff release_handoff.json \
  --execute
```

Without `--execute`, `register_release_candidates` is a dry run.

## What To Expect In The Final Report

The final report should say:

- how many videos were requested;
- how many succeeded;
- how many failed;
- which POIs were selected;
- which POIs were skipped or quarantined;
- where `RUN_RECEIPT.json` is;
- whether usage was written and verified;
- whether release candidates were created;
- whether a review package was downloaded.

## Current Gaps

The target contract is ahead of the current repo implementation. The repo still
needs dedicated support for:

- resume/top-up from receipt.

Current `promo.cli.select_batch_pois` already does read-only random POI
selection, 3-day cooldown, and dynamic active asset thresholds.

Current `promo.cli.prepare_drive_staging` already builds manifest-backed Drive
staging inventory from manifests or audit-passed receipt entries.

Current `promo.cli.upload_drive_staging` already uploads staged final MP4s to
Drive using OAuth credentials and keeps files private.

Current `promo.cli.run_batch --select-random-pois --production-autopilot`
already selects eligible POIs, writes `selection_summary.json` and `batch.json`,
then runs each audit-passed video through Drive upload, usage
writeback/verification, release-candidate registration/verification, and POI
quarantine on usage writeback failure.

Current `promo.cli.audit_run_manifest` already checks production manifest
requirements before usage writeback or handoff.

Current `promo.cli.usage_events_writeback --execute` writes manifest-derived
usage events after manifest audit and verifies the rows by `event_id` after the
RPC.

Current `promo.cli.register_release_candidates --execute` inserts approved
handoff rows into `release_candidates` and verifies the rows by
`source_video_key` after insert.

Current `promo.cli.run_batch` writes `RUN_RECEIPT.json` through render,
manifest-audit, Drive, usage, release-candidate, and quarantine states when
`--production-autopilot` is enabled, and records selection metadata when
`--select-random-pois` is used. Future work needs to add receipt-based
resume/top-up states.
