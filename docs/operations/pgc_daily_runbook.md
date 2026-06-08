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
3. upload the final MP4 to Drive
4. write asset usage
5. verify asset usage
6. register the finished video in release_candidates
7. verify the registration
8. update RUN_RECEIPT.json
```

Grandma version:

```text
Make the video.
Check the receipt.
Put the final MP4 in the Drive safe.
Write the asset ledger.
Register that zhongtai can use this finished video.
Update the batch order JSON.
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

- random eligible POI selection;
- 3-day cooldown;
- dynamic active asset thresholds;
- Drive upload;
- per-video usage writeback orchestration;
- release candidate insertion and verification;
- POI quarantine;
- resume/top-up from receipt.

Current `promo.cli.run_batch` writes a render-only `RUN_RECEIPT.json`. Future
work needs to extend that receipt through Drive, usage, release-candidate, and
resume/top-up states.
