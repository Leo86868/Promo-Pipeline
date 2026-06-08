# PGC Production Contract

This contract captures the target behavior for the repo-owned
`pgc-production-batch` skill. It is intentionally ahead of the current
implementation. Use it to guide future CLIs and audits.

## Modes

Normal requests are production autopilot.

```text
Use PGC skill. Make 15 POIs, 3 videos each.
```

means:

- choose eligible POIs;
- run without routine mid-batch stops;
- write usage per successful video;
- register finished videos in `release_candidates` after durable upload;
- write `RUN_RECEIPT.json`.

Review mode is explicit.

```text
Make 15 POIs, 3 each, review first.
```

means:

- produce review artifacts;
- optionally copy videos to Downloads;
- do not write usage;
- do not create release candidates.

## Data Ownership

PGC owns production metadata through finished-video registration. It does not
own account assignment or publishing.

| Surface | Owner | Purpose |
| --- | --- | --- |
| `public.poi_asset_usage_events` | PGC and other video paradigms | Asset usage ledger. |
| `public.release_candidates` | PGC writes approved finished-video candidates; zhongtai consumes them | Finished video handoff before distribution. |
| `public.distribution_status` | AIGC/zhongtai | Account assignment, publishing, metrics. |
| `RUN_RECEIPT.json` | PGC | Batch-level order and recovery record. |

No Supabase batch registry is required for now. Add one later only if dashboards,
cross-run retry control, or central run monitoring justify it.

## POI Eligibility

POI selection is equal random among eligible POIs. Do not weight by active asset
count.

Default cooldown:

```text
cooldown_days = 3
```

Cooldown is global per POI across PGC runs.

Classification filters such as `EU` or `gold` must be honored only when the
asset platform exposes the needed field. If not, fail clearly before production.

If fewer POIs pass filters than requested, stop before production and ask Leo to
choose whether to run fewer or wait for zhongtai to add assets.

## Active Asset Formula

The asset platform determines active/eligible assets and usage caps. The PGC
paradigm determines how many active assets are enough for its format.

For `pgc_65s`:

```text
base_min_assets_for_format = 50
required_active_assets = base_min_assets_for_format + 10 * max(videos_per_poi - 1, 0)
```

Examples:

| Videos per POI | Required active assets |
| --- | --- |
| 1 | 50 |
| 2 | 60 |
| 3 | 70 |
| 4 | 80 |

Future paradigms can define their own base value and buffer formula.

## Per-Video State Machine

Target state progression:

```text
planned
-> rendering
-> rendered
-> manifest_audited
-> drive_uploaded
-> usage_written
-> usage_verified
-> release_candidate_inserted
-> release_candidate_verified
-> complete
```

Safe operation order:

```text
render MP4
-> audit manifest
-> upload final MP4 to Drive
-> write usage from manifest
-> verify usage writeback
-> insert release_candidates row
-> verify release_candidates row
-> update RUN_RECEIPT.json
```

`release_candidates.source_output_uri` must be durable. Current accepted form:

```text
drive:<file_id>
```

Reject local and temporary paths as candidate output URIs.

## Manifest Audit Gate

Usage writeback is derived from the manifest. The manifest must pass audit
before usage writeback.

Minimum manifest checks:

- manifest exists;
- `manifest_id` and `run_id` exist;
- POI id/name exist;
- exactly one rendered output exists for the video slot;
- rendered output has `music_label`;
- every timeline entry has `asset_id`;
- every timeline entry has `occurrence_id`;
- every used asset exists in `asset_snapshot`;
- bridge/tail filler entries carry `asset_id`;
- usage events can be derived;
- usage event IDs are unique.

If the audit fails, do not write usage and do not create a release candidate.

## Retry And Failure Policy

Retries must be bounded and implemented by repo/runtime code. The skill defines
policy; agents should not improvise unbounded retry loops.

| Step | Retry? | After retry exhaustion |
| --- | --- | --- |
| Render/transient vendor call | Yes, when safe | Mark video failed; continue. |
| Manifest audit | No by default | Mark video unsafe; no usage or candidate; continue. |
| Drive upload | Yes | Mark failed handoff; no usage or candidate; continue. |
| Usage writeback | Yes | Quarantine that POI. |
| Usage verification | Yes | Quarantine that POI. |
| Release candidate insert | Yes | Mark handoff failed/retryable; continue. |
| Release candidate verification | Yes | Mark handoff unknown/retryable; continue. |

When a POI is quarantined, do not produce more videos for that POI. Continue
other POIs only when asset usage is POI-isolated. If isolation cannot be proven,
stop the batch.

## No Auto Top-Up

If a batch requests 45 videos and 43 succeed, finish and report 43/45. Do not
automatically spend more compute to fill the gap.

Run a top-up batch only when Leo asks.

## Receipt Contract

Every production run writes `RUN_RECEIPT.json`. Future sessions resume from it.

Recommended structure:

```json
{
  "batch_id": "pgc_65s_20260608T210000Z",
  "paradigm": "pgc_65s",
  "request": {
    "poi_count": 15,
    "videos_per_poi": 3,
    "selection": "random_equal",
    "filters": {
      "classification": null,
      "cooldown_days": 3,
      "base_min_assets_for_format": 50,
      "extra_variation_asset_buffer": 10,
      "required_active_assets": 70
    }
  },
  "selected_pois": [],
  "skipped_pois": [],
  "quarantined_pois": [],
  "videos": [
    {
      "poi_id": "poi_example",
      "poi_name": "Example Hotel",
      "video_index": 1,
      "state": "complete",
      "manifest_id": "manifest_example",
      "manifest_path": "...",
      "source_video_key": "manifest:manifest_example:variant:1",
      "source_output_uri": "drive:file_id",
      "usage": {
        "event_count": 18,
        "writeback_status": "verified"
      },
      "release_candidate": {
        "id": "release_candidate_id",
        "status": "verified"
      },
      "error": null
    }
  ],
  "summary": {
    "requested_videos": 45,
    "succeeded_videos": 43,
    "failed_videos": 2,
    "usage_written_videos": 43,
    "release_candidates_created": 43
  }
}
```

The receipt is the batch-level order record. Do not overload the asset usage
ledger with batch registry responsibilities.

## Implementation Gaps

As of this contract, the repo still needs implementation for:

- Drive upload and verification;
- per-video production orchestration;
- release candidate insertion and verification;
- POI quarantine;
- receipt-based resume/top-up.

`promo.cli.select_batch_pois` already implements read-only random POI selection,
cooldown enforcement, dynamic active asset thresholds, and batch JSON emission.

`promo.cli.run_batch` already emits a render-only `RUN_RECEIPT.json`; future
work should extend it through the full production state machine.
