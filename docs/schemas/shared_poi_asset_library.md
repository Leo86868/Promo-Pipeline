# Shared POI Asset Library Integration Notes

Working notes for integrating PGC Pipeline with the shared POI asset
library owned outside this repo. This is not the source of truth for the
AIGC Main schema while that design is still moving; it records PGC's
consumer requirements and adapter assumptions so the two repos can
converge without wiring runtime Supabase reads or writes yet.

## Scope

PGC needs a stable asset inventory for batch production:

- read a POI's approved video assets and analysis metadata;
- use stable identifiers through script, assignment, render, and sidecars;
- reuse or validate embedding metadata for retrieval;
- emit local run manifests that can retry usage writeback;
- write one usage event per rendered visual occurrence when writeback is
  implemented.

Out of scope for this document: table migrations, storage bucket creation,
auth policy design, and runtime implementation in PGC.

## Identity Contract

The shared library must provide identifiers that do not change across
batch runs.

| Field | Required | Stability | Meaning |
|---|---:|---|---|
| `poi_id` | yes | Global, never reassigned | Canonical POI identifier from the shared library. PGC display names and slugs are derived labels, not identity. |
| `asset_id` | yes | Global, never reassigned | Canonical identifier for one immutable source media asset. If the source bytes change, create a new `asset_id`. |
| `clip_id` | yes | Stable within one `poi_id` | PGC-facing clip identifier used by Gemini #2, sidecars, and renderer bindings. Current PGC expects a zero-padded 4-digit string such as `"0042"`. |
| `asset_version` | recommended | Monotonic per `asset_id` or immutable at `1` | Optional version marker if the library supports metadata re-analysis without changing media bytes. |

Required mapping invariant:

```text
(poi_id, clip_id) -> exactly one active asset_id
asset_id -> exactly one immutable source media object
```

`clip_id` should not be reused for a different active asset under the same
`poi_id`. If an asset is retired, keep historical mappings so old run
manifests and usage events remain explainable.

## Asset Read Contract

For each active asset in a POI pool, the shared library should expose the
following shape to PGC. Field names can differ in storage, but the PGC
adapter should project to this contract.

```json
{
  "poi_id": "poi_123",
  "asset_id": "asset_abc",
  "clip_id": "0042",
  "source_storage_path": "poi_123/clips/asset_abc.mp4",
  "source_storage_bucket": "poi-assets",
  "source_content_hash": "sha256:...",
  "media": {
    "duration_sec": 5.12,
    "width": 1080,
    "height": 1920,
    "fps": 29.97,
    "container": "mp4",
    "video_codec": "h264",
    "file_size_bytes": 43811820
  },
  "material_analysis": {
    "scene_description": "infinity pool at sunset with palm trees",
    "category": "pool",
    "camera_motion": "slow_push_in",
    "dominant_motion_phase": "middle",
    "shot_size": "wide",
    "main_subject": "pool terrace",
    "analysis_model": "mimo-v2-omni",
    "analysis_prompt_sha1": "abcdef12",
    "analysis_generated_at": "2026-05-26T00:00:00Z"
  },
  "embedding": {
    "embedding_text": "infinity pool at sunset with palm trees | pool",
    "embedding_model": "text-embedding-3-small",
    "embedding_dim": 1536,
    "embedding_vector": [0.024, -0.011, 0.093],
    "composition_version": 1,
    "source_analysis_sha1": "abcdef12"
  }
}
```

## Source Storage Path

`source_storage_path` is the durable pointer to the original media object.
PGC may download or copy it to a local temp path before rendering, but the
manifest and usage writeback must preserve the source storage path.

Requirements:

- path must identify the exact source object used for the run;
- path must be paired with a content hash such as `sha256:<hex>`;
- paths should not be overwritten in place with different bytes;
- signed URLs are runtime transport details and should not be stored as
  durable identity.

## Media Metadata

PGC currently probes `source_duration_sec` locally before assignment. The
shared library should still provide media metadata so batch production can
preflight pools before downloading assets.

Minimum required media fields:

| Field | Type | Notes |
|---|---|---|
| `duration_sec` | float | Required by assignment and renderer capacity checks. |
| `width` | int | Should preserve source dimensions. |
| `height` | int | Should preserve source dimensions. |
| `fps` | float | Use measured stream fps where available. |
| `container` | string | Example: `mp4`. |
| `video_codec` | string | Example: `h264`, `hevc`. |
| `file_size_bytes` | int | Useful for transfer planning and diagnostics. |
| `source_content_hash` | string | Prefer `sha256:<hex>`. |

PGC adapter mapping:

```text
media.duration_sec -> ClipMetadata.source_duration_sec
clip_id -> ClipMetadata.id
```

## Material Analysis Fields

PGC's current analysis path produces `ClipMetadata` for Gemini #1,
Gemini #2, retrieval, and match-quality review. A shared library can
provide these fields directly, or PGC can generate them and later sync
them upstream.

Minimum required fields:

| Field | Required | Notes |
|---|---:|---|
| `scene_description` | yes | Must be non-empty. Used in prompts, match quality, and embedding text. |
| `category` | yes | Must be non-empty. Used in prompts and embedding text. |
| `camera_motion` | recommended | Current MiMo output field. |
| `dominant_motion_phase` | recommended | Current default is `"middle"` when absent. |
| `shot_size` | optional | Already represented in `ClipMetadata`. |
| `main_subject` | optional | Already represented in `ClipMetadata`. |
| `analysis_model` | yes | Model identifier used for the analysis. |
| `analysis_prompt_sha1` | yes | Must match the analysis prompt/model version used to produce the fields. |
| `analysis_generated_at` | recommended | ISO-8601 timestamp for audit and stale-analysis review. |

The current PGC MiMo cache version key is an 8-hex SHA1 over the MiMo
prompt and clip model. The shared library should expose an equivalent
`analysis_prompt_sha1` so PGC can detect stale analysis and embedding
vectors without reading local `.mimo_cache/` files.

## Embedding Requirements

Retrieval is a soft hint in PGC: the retrieved subset is what Gemini #2
sees, but a returned `clip_id` is not rejected solely because it was
outside the retrieved subset. See `docs/schemas/clip_assignments.md`.

The shared library should either provide embeddings with enough metadata
to validate them, or let PGC build local `.embedding_cache/` sidecars from
the shared analysis fields.

Required embedding metadata:

| Field | Required | Current PGC value |
|---|---:|---|
| `embedding_text` | yes | `"<scene_description> | <category>"` |
| `embedding_model` | yes | `text-embedding-3-small` |
| `embedding_dim` | yes | `1536` |
| `embedding_vector` | yes | Float array of length `embedding_dim`. |
| `composition_version` | yes | `1` |
| `source_analysis_sha1` | yes | Must match `material_analysis.analysis_prompt_sha1`. |
| `embedding_generated_at` | recommended | ISO-8601 timestamp. |

Invalidation rule:

```text
embedding_model + embedding_dim + source_analysis_sha1 + composition_version
```

Any change in one of those four values means the vector is stale for PGC
retrieval and must be regenerated or ignored.

## Usage Event Writeback

PGC should eventually write usage events to the shared library after a
successful render. This is not implemented yet.

Writeback should be idempotent. The recommended idempotency key is:

```text
event_id = "sha256:" + sha256(
  "pgc_usage_v1\n" +
  manifest_id + "\n" +
  variant_index + "\n" +
  occurrence_id + "\n" +
  asset_id
)
```

One event should represent one rendered visual occurrence. If the same
asset appears twice in one video, write two usage events.

Seconds such as `trim_start_sec`, `display_start_sec`, and
`display_end_sec` remain event fields, but they should not be part of the
event identity hash. This avoids cross-language float-format drift.

Minimum usage event fields:

| Field | Required | Notes |
|---|---:|---|
| `event_id` | yes | Idempotency key for retry-safe upsert. |
| `run_id` | yes | Local PGC run identifier from `run_manifest`. |
| `manifest_id` | yes | Stable identifier for the local run manifest payload. |
| `poi_id` | yes | Shared-library POI identity. |
| `asset_id` | yes | Shared-library asset identity. |
| `clip_id` | yes | PGC-facing clip id used in prompts and sidecars. |
| `variant_index` | yes | 1-based variant index. |
| `occurrence_index` | yes | 0-based visual occurrence index in final video order. |
| `occurrence_id` | recommended | Stable manifest occurrence id used for `event_id` generation. |
| `usage_role` | yes | `assigned_phrase`, `bridge_tail`, or future role. |
| `segment` | conditional | Required for `assigned_phrase`; null for bridge-only usage. |
| `trim_start_sec` | yes | Seconds into the source asset. |
| `display_start_sec` | yes | Seconds in final video timeline. |
| `display_end_sec` | yes | Seconds in final video timeline. |
| `source_duration_sec` | yes | Source media duration observed by PGC. |
| `output_path` | yes | Local or final rendered output path returned by backend. |
| `created_at` | yes | Writeback event creation timestamp. |

Recommended provenance fields:

- `target_duration_sec`;
- `format_mode`;
- `voice_key`;
- `voice_backend`;
- `retrieval_contract`;
- `retrieval_fallback_reason`;
- `clip_assignments_sidecar_path`;
- `tts_metrics_sidecar_path`;
- `match_quality_sidecar_path`.

Writeback should happen after local artifacts are durable. If the shared
library is unavailable, PGC should keep the local manifest and retry later
rather than failing an already-rendered deliverable.

## Local Run Manifest Relationship

PGC should use a local `run_manifest` as the bridge between current
file-based sidecars and future shared-library writeback. The manifest is
the local source of truth for retrying usage writes and explaining which
shared assets were used in a render.

Recommended local manifest responsibilities:

- assign `run_id` and `manifest_id`;
- snapshot the POI identity and asset mapping used by the run;
- record local temp paths and durable source storage paths;
- record sidecar and output paths;
- record every rendered visual occurrence in final timeline order;
- provide stable `occurrence_id` values so usage events can be derived
  later.

Recommended shape:

```json
{
  "manifest_id": "manifest_20260526_001",
  "run_id": "pgc_run_20260526_001",
  "poi": {
    "poi_id": "poi_123",
    "display_name": "Hotel Name",
    "pgc_slug": "hotel_name"
  },
  "asset_snapshot": [
    {
      "clip_id": "0042",
      "asset_id": "asset_abc",
      "source_storage_bucket": "poi-assets",
      "source_storage_path": "poi_123/clips/asset_abc.mp4",
      "source_content_hash": "sha256:...",
      "source_duration_sec": 5.12,
      "analysis_prompt_sha1": "abcdef12",
      "embedding_key": "text-embedding-3-small-1536-abcdef12-v1"
    }
  ],
  "outputs": [
    {
      "variant_index": 1,
      "output_path": "output/run-1/promo_hotel_name_30s.mp4",
      "target_duration_sec": 30.0,
      "format_mode": "short_30s"
    }
  ],
  "sidecars": {
    "clip_assignments": "output/run-1/clip_assignments_hotel_name_30s.json",
    "tts_metrics": "output/run-1/tts_metrics_hotel_name_30s.json",
    "match_quality": "output/run-1/match_quality_hotel_name_30s.json"
  },
  "timeline_entries": [
    {
      "asset_id": "asset_abc",
      "clip_id": "0042",
      "variant_index": 1,
      "occurrence_index": 0,
      "occurrence_id": "occ_0001_000000",
      "usage_role": "assigned_phrase",
      "segment": 1,
      "trim_start_sec": 0.0,
      "trim_end_sec": 2.35,
      "display_start_sec": 0.0,
      "display_end_sec": 2.35
    }
  ]
}
```

Current sidecars remain useful:

- `clip_assignments_*.json` records Gemini #2 phrase assignments and
  retrieval provenance.
- `tts_metrics_*.json` records successful variant TTS metrics for future
  WPM calibration.
- `match_quality_*.json` records human-review overlap diagnostics.

The future `run_manifest` should reference these sidecars instead of
duplicating every field. It should duplicate only the asset identity,
storage, output, and final timeline data needed to derive writeback
payloads.
