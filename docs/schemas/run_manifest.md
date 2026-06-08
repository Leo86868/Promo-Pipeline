# `run_manifest_<slug>_<dur>s.json`

Working draft for a local PGC run manifest. The manifest is a local
sidecar that records what a successful `compile_promo` run actually used
and produced. It is the bridge between today's file-based PGC pipeline
and future shared-library usage writeback.

This document defines the local manifest contract. Local manifest
emission is implemented in `promo/core/pipeline/run_manifest.py`.
Supabase reads and writes are still out of scope.

## Scope

The manifest should answer one question:

```text
For this rendered output, which source clips/assets appeared where?
```

In scope:

- local PGC run identity;
- POI labels and optional future `poi_id`;
- asset snapshot used by the run;
- output video paths and existing sidecar paths;
- final visual timeline entries, including renderer bridge clips;
- stable `occurrence_id` values for future usage writeback.

Out of scope:

- live Supabase writeback;
- shared-library asset fetching;
- media copying;
- vector storage;
- dashboard display.

## Production Relationship

`run_manifest` should be a separate PGC module/artifact, but production
execution must provide the facts. The manifest writer should not guess by
scraping a finished MP4 after the run.

Current implementation:

```text
promo/core/pipeline/run_manifest.py
```

The pipeline gathers facts during the real render path, then calls a
small builder/writer after successful renders and durable sidecar writes.
This keeps manifest formatting separate while avoiding scattered manifest
logic across the pipeline.

Implementation note: the sidecar writer now has structured-result
helpers that expose exact collision-bumped paths while preserving the
older bool-compatible wrappers.

## Filename Template

```text
run_manifest_<sanitized_poi_slug>_<round(duration_sec)>s.json
```

Use the same collision-bump convention as the existing sidecars:

```text
run_manifest_hotel_name_60s.json
run_manifest_hotel_name_60s-2.json
run_manifest_hotel_name_60s-3.json
```

`<sanitized_poi_slug>` should match `promo.core.sanitize_poi_name`.

## Payload Shape

```json
{
  "schema_version": 1,
  "manifest_id": "manifest_20260526_001",
  "run_id": "pgc_run_20260526_001",
  "created_at": "2026-05-26T00:00:00Z",
  "pipeline": {
    "repo": "pgc-pipeline",
    "entrypoint": "promo.cli.compile_promo"
  },
  "poi": {
    "poi_id": null,
    "display_name": "Hotel Name",
    "pgc_slug": "hotel_name",
    "location": "Palm Springs"
  },
  "run_config": {
    "target_duration_sec": 60.0,
    "n_variants": 1,
    "format_selector": "single",
    "embedding_cache_active": true,
    "skip_analysis": false,
    "tts_speed": 0.95,
    "seed": 123
  },
  "asset_snapshot": [
    {
      "clip_id": "0042",
      "asset_id": null,
      "local_clip_path": "/tmp/pgc/clip_0042.mp4",
      "source_storage_bucket": null,
      "source_storage_path": null,
      "source_content_hash": null,
      "source_duration_sec": 5.12,
      "width": null,
      "height": null,
      "fps": null,
      "container": null,
      "video_codec": null,
      "file_size_bytes": null,
      "scene_description": "infinity pool at sunset with palm trees",
      "category": "pool",
      "analysis_prompt_sha1": "abcdef12",
      "embedding_text": null,
      "embedding_model": null,
      "embedding_dim": null,
      "embedding_composition_version": null,
      "embedding_source_analysis_sha1": null,
      "embedding_status": null,
      "embedding_key": "text-embedding-3-small-1536-abcdef12-v1"
    }
  ],
  "sidecars": {
    "clip_assignments": "output/run-1/clip_assignments_hotel_name_60s.json",
    "tts_metrics": "output/run-1/tts_metrics_hotel_name_60s.json",
    "match_quality": "output/run-1/match_quality_hotel_name_60s.json"
  },
  "outputs": [
    {
      "variant_index": 1,
      "variant_status": "rendered",
      "output_path": "output/run-1/promo_hotel_name_60s.mp4",
      "render_output_path": "output/run-1/promo_hotel_name_60s.mp4",
      "final_output_path": "output/run-1/promo_hotel_name_60s.mp4",
      "target_duration_sec": 60.0,
      "format_mode": "long_65s",
      "voice_key": "kore",
      "voice_backend": "gemini",
      "bgm_path": "output/run-1/bgm.mp3",
      "music_label": "Run Away with Me",
      "music_id": "22222222-2222-2222-2222-222222222222",
      "music_duration_sec": 70.0,
      "music_drive_file_id": "drive_file_123",
      "file_size_bytes": 43811820
    }
  ],
  "timeline_entries": [
    {
      "variant_index": 1,
      "occurrence_index": 0,
      "occurrence_id": "occ_0001_000000",
      "usage_role": "assigned_phrase",
      "clip_id": "0042",
      "asset_id": null,
      "segment": 1,
      "trim_start_sec": 0.0,
      "trim_end_sec": 2.35,
      "display_start_sec": 0.0,
      "display_end_sec": 2.35,
      "source_duration_sec": 5.12
    }
  ]
}
```

## Top-Level Fields

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | `int` | Manifest schema version. Start at `1`. |
| `manifest_id` | `str` | Stable ID for this manifest artifact. Generate once when the manifest is emitted. |
| `run_id` | `str` | Stable ID for one `compile_promo` invocation. Reused across all variants in that invocation. |
| `created_at` | `str` | ISO-8601 timestamp when the manifest was written. |
| `pipeline` | `dict` | Lightweight provenance for the producing repo/entrypoint. |
| `poi` | `dict` | POI labels plus optional future shared `poi_id`. |
| `run_config` | `dict` | Operator/config inputs that affect output identity and duration. |
| `asset_snapshot` | `list[dict]` | Clip/asset metadata snapshot available to the run. |
| `sidecars` | `dict` | Run-level paths to existing observability sidecars. |
| `outputs` | `list[dict]` | One row per successfully rendered variant. |
| `timeline_entries` | `list[dict]` | One row per rendered visual occurrence in final timeline order. |

`poi.poi_id` and `asset_snapshot[].asset_id` are nullable until PGC reads
from the shared asset library. Local-folder runs should still emit a
manifest with `null` shared IDs.

When PGC consumes the shared library, `asset_snapshot` should be a frozen
projection of the exact `public.poi_asset_valid_clips` rows used for the run. Do
not later re-resolve old manifests by display name or `clip_id` alone.

## Asset Snapshot

Each `asset_snapshot` row records what PGC knew about one clip before or
during rendering.

| Field | Required now | Required with shared library | Meaning |
|---|---:|---:|---|
| `clip_id` | yes | yes | PGC-facing 4-digit clip id. |
| `asset_id` | no | yes | Shared-library immutable asset identity. |
| `local_clip_path` | yes | local/debug | Local temp/source path used by PGC for this run. Not durable identity. |
| `source_storage_bucket` | no | yes | Durable shared-library bucket. |
| `source_storage_path` | no | yes | Durable shared-library object key. |
| `source_content_hash` | no | yes | `sha256:<64 hex>` source clip hash. |
| `source_duration_sec` | yes | yes | Duration observed or supplied for assignment/render capacity. |
| `width` / `height` / `fps` | no | yes | Media metadata from `poi_asset_valid_clips`. |
| `container` / `video_codec` | no | yes | Media container/codec metadata from `poi_asset_valid_clips`. |
| `file_size_bytes` | no | yes | Source object size. |
| `scene_description` | recommended | yes | Material-analysis text used by prompts/retrieval. |
| `category` | recommended | yes | Material-analysis category used by prompts/retrieval. |
| `analysis_prompt_sha1` | no | yes | Analysis version key, when known. |
| `embedding_text` / `embedding_model` / `embedding_dim` | no | yes | Embedding-ready metadata from `poi_asset_valid_clips`. |
| `embedding_composition_version` | no | yes | Embedding text composition version. |
| `embedding_source_analysis_sha1` | no | yes | Analysis hash used to produce embedding text/vector. |
| `embedding_status` | no | yes | Current embedding readiness state. |
| `embedding_key` | no | local/debug | Compact local cache key when known. |

## Outputs

Each `outputs` row records one successfully rendered variant. `bgm_path` remains
the local audio file used by the renderer. When PGC selects BGM from Supabase
Music Library, the row should also include `music_label` at minimum so the
release-candidate handoff does not need to infer music from filenames.

Recommended Music Library fields:

| Field | Type | Meaning |
|---|---|---|
| `music_label` | `str` | Human-readable music label/name used by AIGC distribution filenames. |
| `music_id` | `str` | Stable `public.music_library.id` when known. |
| `music_name` | `str` | Music Library name; usually same as `music_label` for v1. |
| `music_duration_sec` | `float` | Audited Music Library duration. |
| `music_drive_file_id` | `str` | Source Drive file id from Music Library. |
| `music` | `dict` | Optional nested copy of the same music snapshot for future expansion. |

## Timeline Entries

`timeline_entries` are the manifest's most important rows. They must
represent the final renderer timeline, not only Gemini #2 assignments.

Renderer bridge clips must appear here with:

```text
usage_role = "bridge_tail"
segment = null
```

Assigned narration clips should use:

```text
usage_role = "assigned_phrase"
segment = <Gemini #1 segment number>
```

Required fields:

| Field | Type | Meaning |
|---|---|---|
| `variant_index` | `int` | 1-based variant index. |
| `occurrence_index` | `int` | 0-based order within the final visual timeline for this variant. |
| `occurrence_id` | `str` | Stable manifest occurrence id, e.g. `occ_0001_000000`. |
| `usage_role` | `str` | `assigned_phrase` or `bridge_tail` for v1. |
| `clip_id` | `str` | PGC-facing 4-digit clip id. |
| `asset_id` | `str \| null` | Shared-library asset id when known. |
| `segment` | `int \| null` | Required for assigned phrases; null for bridge clips. |
| `trim_start_sec` | `float` | Seconds into source clip where playback starts. |
| `trim_end_sec` | `float` | Seconds into source clip where playback ends. |
| `display_start_sec` | `float` | Seconds in final rendered video timeline. |
| `display_end_sec` | `float` | Seconds in final rendered video timeline. |
| `source_duration_sec` | `float` | Source clip duration observed by PGC. |

`display_end_sec` should equal the next timeline entry's
`display_start_sec`, or the variant's final display end for the last
entry.

Bridge clips must carry `asset_id` once the run uses shared-library
assets. Bridges are inserted after Gemini assignment, so PGC must
preserve the `clip_id -> asset_id` mapping through render binding.

## Derived Usage Events

The manifest does not store placeholder `usage_event_drafts`. Usage
events should be derived from the manifest after local output and sidecar
artifacts are durable. For local-folder runs without `poi_id` and
`asset_id`, no usage writeback should be attempted.

Once shared identity is known, generate deterministic event IDs without
float seconds in the hash:

```text
event_id = "sha256:" + sha256(
  "pgc_usage_v1\n" +
  manifest_id + "\n" +
  variant_index + "\n" +
  occurrence_id + "\n" +
  asset_id
)
```

`trim_start_sec`, `trim_end_sec`, `display_start_sec`,
`display_end_sec`, and `source_duration_sec` remain normal event fields
for audit and timeline reconstruction. They are intentionally not event
identity fields, avoiding Python/JS float-format drift.

Current pure helper:

```text
promo.core.pipeline.run_manifest.build_usage_events_from_manifest()
```

It builds future RPC payload rows from a fully identified manifest but
does not write to Supabase.

Current read-only preview CLI:

```bash
python3 -m promo.cli.usage_events_preview \
  path/to/run_manifest_*.json \
  --output path/to/usage_events_preview.json
```

Current production audit CLI:

```bash
python3 -m promo.cli.audit_run_manifest \
  path/to/run_manifest_*.json \
  --output path/to/manifest_audit.json
```

The preview CLI reads local manifests and, when available, the referenced
`clip_assignments` sidecar to attach retrieval provenance such as
`retrieval_contract` and `retrieval_fallback_reason`.

Current explicit writeback CLI:

```bash
python3 -m promo.cli.usage_events_writeback \
  path/to/run_manifest_*.json \
  --execute
```

Without `--execute`, the writeback CLI prints the same manifest-derived
summary without calling Supabase. With `--execute`, it sends one JSON
array per manifest to `rpc_record_poi_asset_usage_events(p_payload jsonb)`
only after production manifest audit passes, then queries
`poi_asset_usage_events` by `event_id` to verify that the expected rows exist
with matching identity fields. Use `--no-verify` only for manual debugging
after the audit has passed.

Freshness is not a manifest responsibility. The manifest records actual
asset usage; the shared asset library should use usage events to update
`usage_count` and keep overused assets out of the active read surface.

## Implementation Notes

The first implementation is local-only.

Implemented sequence:

1. Add pure builders in `promo/core/pipeline/run_manifest.py`.
2. Make sidecar writing return exact written paths, or otherwise expose
   those paths to the manifest builder.
3. Capture renderer-ready timeline entries after bridge insertion, not
   only pre-render Gemini #2 assignments.
4. Emit the manifest after successful render and sidecar writes.
5. Add fixture tests that build a manifest from local-folder inputs with
   `poi_id` / `asset_id` set to `null`.
6. Add shared-library fixture tests with real `poi_id`,
   `asset_id`, storage path, and content hash.
7. Keep live Supabase reads/writes out of manifest emission.

Do not add Supabase writeback in the manifest implementation sprint.

## Acceptance Criteria

1. Local-folder runs can emit a manifest with null `poi_id` and
   `asset_id`.
2. Future shared-library runs can emit a manifest with populated `poi_id`,
   `asset_id`, storage path, and source hash.
3. Every rendered visual occurrence appears exactly once in
   `timeline_entries`, including renderer bridge clips.
4. Every timeline entry has a stable `occurrence_id`.
5. Existing sidecars remain the detailed observability surfaces; the
   manifest references them and does not duplicate all sidecar content.
6. Event IDs are derivable from `manifest_id`, `variant_index`,
   `occurrence_id`, and `asset_id`, without float seconds in the hash.
7. No Supabase writes happen from manifest emission.
