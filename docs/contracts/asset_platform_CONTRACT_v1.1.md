> **[PGC repo 本地存档]** 快照存档于 2026-06-11,源 = `AIGC Main/asset_platform/CONTRACT.md`(normative 原本在彼)。契约通知要求双方 repo 留档;改动走 contract PR,本文件随通知刷新。

# Asset Platform Contract

**Status:** v1.1, 2026-06-11 (v1 + production-side chapter §6/§7). Normative.
**Scope:** the surface the shared asset platform promises to BOTH sides:
its video-paradigm consumers (§1–§5: music remix in this repo; PGC 65s in
`Leo86868/Promo-Pipeline`) and its material producers (§6–§7: how a
generation chain legally writes into the library). Compiled from a
cross-repo code audit (2026-06-10) plus the ingest code surface
(2026-06-11); every item below is load-bearing today.

**Verification:** `python3 scripts/check_asset_platform_contract.py`
(read-only) validates every promise below against the live database; run it
after any platform migration and require "Contract holds."

**Change protocol:** any change to an object listed here starts with a PR
editing THIS file, and both repos get notified before it merges. Additive
changes (new columns, new optional fields) are safe; renames, type changes,
path-format changes, and RPC signature changes are breaking and need an
explicit migration plan for both consumers.

## 1. Read contract

### 1.1 View `public.poi_asset_valid_clips`

The only sanctioned clip read surface. Must expose at least:

```text
poi_id, asset_id, clip_id, canonical_key, display_name,
source_storage_bucket, source_storage_path, source_content_hash,
duration_sec, width, height, fps, container, video_codec, file_size_bytes,
scene_description, category, camera_motion, dominant_motion_phase,
shot_size, main_subject, embedding_text, embedding_status,
usage_count, usage_remaining, last_used_at, status, overlay_name, location,
poi_type, region        (operating labels; music remix batch filters)
```

(Live view 2026-06-11 exposes 42 columns; the list above is the load-bearing
minimum — extra columns are free to exist, removing/renaming any listed one
is breaking.)

Row invariants consumers validate against (PGC fails closed on violation):

```text
poi_id   ~ ^poi_[a-z0-9]+$        (permanent identity)
asset_id ~ ^asset_[a-z0-9]+$      (permanent identity)
clip_id  = 4-digit string, unique per POI
source_storage_bucket = "poi-assets"
source_storage_path   = "{poi_id}/clips/{asset_id}.mp4"
source_content_hash   ~ ^sha256:[a-f0-9]{64}$
status ∈ {active, NULL}
```

### 1.2 Table `poi_asset_embeddings`

Columns: `asset_id, embedding_text, embedding_vector (pgvector), status,
generated_at, embedding_model, embedding_dim, embedding_composition_version`.
Consumers filter on the frozen v1 tuple:

```text
embedding_model = 'text-embedding-3-small'
embedding_dim = 1536
embedding_composition_version = 1
status = 'ready'
```

A new embedding generation must be introduced as a new version, never by
mutating rows that match the v1 tuple.

### 1.3 Storage bucket `poi-assets`

Private bucket; objects downloadable at `{poi_id}/clips/{asset_id}.mp4`.
Consumers verify bytes against `source_content_hash` after download. Existing
objects are immutable — replacements get a new `asset_id`.

### 1.4 Table `music_library`

Columns consumers rely on: `id (uuid), music_name, drive_file_id,
duration_sec, genre, bpm, tags, embedding_text`. Audio bytes are fetched from
Google Drive by `drive_file_id` (not Supabase Storage).

### 1.5 Table `poi_asset_usage_events` (read-back)

- Cooldown / freshness: `poi_id, created_at`.
- Source-window rotation (FAMILY STANDARD, first reader: PGC packer):
  `asset_id, trim_start_sec, display_start_sec, display_end_sec,
  source_duration_sec`. These four window fields must stay populated by every
  writer so any paradigm can rotate windows by lookup.

### 1.6 Views/tables read defensively

- `release_unassigned_candidates` (view): distribution-facing inventory.
- `distribution_status`: paradigms READ it only as a veto (block reverts of
  already-claimed candidates). Paradigms never write it; its schema is owned
  by the distribution lane in this repo.

## 2. Write contract

### 2.1 RPC `rpc_record_poi_asset_usage_events(p_payload)`

The only sanctioned way to write usage. Idempotent on deterministic
`event_id`; returns `{out_inserted_count, out_duplicate_count}`. Payload rows
carry at minimum: `event_id, run_id, manifest_id, poi_id, asset_id, clip_id,
variant_index, occurrence_index, usage_role, trim_start_sec,
display_start_sec, display_end_sec, source_duration_sec, created_at`.
Direct inserts into `poi_asset_usage_events` are forbidden.

### 2.2 RPC `rpc_revert_poi_asset_usage_manifests(p_manifest_ids)`

The only sanctioned way to revert usage (recomputes usage counters
atomically). Blind decrement/delete is forbidden.

### 2.3 Table `release_candidates`

One row per finished video. Upsert key: `(source_pipeline, source_video_key)`.

```text
source_pipeline ∈ {"aigc_music_remix", "pgc_65s"}   (new paradigms register here)
source_video_key = "manifest:{manifest_id}:variant:{N}"  (N >= 1; manifest_id has no ":")
source_output_uri = "drive:{file_id}"  (durable Drive master, upscaled when required)
columns: source_pipeline, source_video_key, source_run_id, source_batch_id,
         poi_id, poi_name, source_output_uri, status, music_label, approved_at
```

Rejection sets `status='rejected'`; rows are never deleted by paradigms.

## 3. Consumer compliance (2026-06-11)

| Rule | music remix (this repo) | PGC (Promo-Pipeline) |
|---|---|---|
| Reads clips via `poi_asset_valid_clips` only | ✅ via `asset_platform.poi_assets` repository | ✅ own adapter, invariant-validated |
| Never mints poi/asset identity | ✅ | ✅ |
| Usage via the two RPCs only | ✅ `asset_platform.usage` wrapper | ✅ |
| `release_candidates` key contract | ✅ same `manifest:…:variant:N` scheme | ✅ (origin of the scheme) |
| Never writes `distribution_status` | ✅ | ✅ (read-only veto) |
| Window-rotation FAMILY STANDARD (read windows, don't hash) | ⚠️ gap: stateless hash trim, deterministic across batches — adopt the window query when retiring it | ✅ standard author |
| No local sidecar embeddings | ✅ | ⚠️ transitional sidecars on legacy LocalBackend path; planned to retire (analysis moves to ingest-time) |

## 4. New-consumer checklist

A new video paradigm joins the platform by:

1. Registering its `source_pipeline` value in §2.3 (contract PR).
2. Reading clips ONLY through `poi_asset_valid_clips` (never base tables) and
   downloading via the §1.3 path template with hash verification.
3. Writing usage ONLY through the §2.1/§2.2 RPCs with deterministic
   `event_id`s, populating all four window fields.
4. Registering finished videos in `release_candidates` with the §2.3 key
   format and a durable `drive:` master.
5. Never minting poi/asset identity, never writing `distribution_status`,
   never keeping local copies of platform metadata.
6. Adding itself to the §3 compliance table.

## 5. Out of scope (for consumers)

Internal platform tables (`poi_asset_backfill_*`,
`poi_asset_poi_onboarding_plan`, `poi_asset_low_res_upscale_replacement_plan`)
and ingest internals are not consumer contract surface; consumers must not
read them. The ingest ENTRY surface is producer contract surface — see §6.

## 6. Production contract (generation chain → platform)

The only legal write path into the library is
`asset_platform/ingest/validate_package.py` (dry-run) followed by
`asset_platform/ingest/live_write.py --apply`. A generation chain never
touches `poi_asset_*` tables or the `poi-assets` bucket directly.

### 6.1 `package_manifest.json` schema (version 1)

A package is a folder containing `package_manifest.json` plus the clip
files. Required by `validate_package.py`:

| Field | Rule |
|---|---|
| `package_schema_version` | must be `1` |
| `generation_line` | required, non-empty text (e.g. `scene_motion`) |
| `run_id` | required, non-empty text |
| `poi_name` or `poi_hint` | at least one required; `poi_hint` is an object |
| `poi_hint.poi_id` | if present, must match `^poi_[a-z0-9_]+$` |
| `clips` | non-empty list |
| `clips[].file` | required; relative path inside the package folder (no absolute paths, no `..`, no escaping the folder); must exist, be a regular file, NOT a symlink |
| `clips[].source_origin_bucket` / `source_origin_path` | optional, but must be provided together |

For every clip the validator computes `file_size_bytes` and a
`sha256:<hex>` content hash, and probes media metadata with ffprobe. A
failed probe is an error. `--skip-media-probe` exists for offline runs and
is NOT blocked by the code at live-write time — running the final
validation without it is operator discipline, not a platform gate.

### 6.2 POI resolver semantics (read-only)

`validate_package.py --verify-poi` resolves the package against the live
`poi_asset_pois` table (`SupabasePoiResolver`). The resolver NEVER writes —
minting new POI identity happens only through the operator-gated
`asset_platform/ingest/onboard_poi_candidates.py` route. Resolution states:

| State | Meaning |
|---|---|
| `verified` | `poi_hint.poi_id` exists, row `status='active'`, and — if the package carries a POI name — it canonical-matches the row (`shared.poi_utils.canonical_key` vs `display_name`/`canonical_key`); a name-less package verifies on `poi_id` alone |
| `name_mismatch` | poi_id exists and is active, but the package name does not canonical-match — fix the package or the hint; never force |
| `not_found` | poi_id not in `poi_asset_pois` |
| `inactive` | poi_id exists but row status is not `active` |
| `needs_manual_review` | package carries no resolved poi_id |

No fuzzy matching anywhere in this path: identity is exact-`poi_id` plus
canonical-key name confirmation. Ambiguity is a human decision.

### 6.3 QC gates and receipt statuses

Receipt `status` and the `live_write_ready` flag:

| Status | When | live_write_ready |
|---|---|---|
| `invalid` | any schema/file/probe error | false |
| `validated` | zero errors + resolution `verified` + resolved row carries a non-empty `overlay_name` | **true** |
| `needs_manual_review` | verified but missing required POI labels, or no poi_id | false |
| `provided_unverified` | poi_id present but not verified — covers both "ran without `--verify-poi`" and live resolutions of `not_found` / `inactive` / `name_mismatch` (the specific reason is in the receipt's `poi_resolution.status`) | false |

`live_write.py` enforces, in addition: the receipt must be
`receipt_type=asset_ingest_dry_run`, `package_valid`, `live_write_ready`,
`poi_resolution.status == "verified"`, zero clip errors, and the receipt's
target `supabase_url` + `storage_bucket` must match the writer's target (no
cross-environment writes). Content-hash gates: each clip is re-hashed at
write time and must match the receipt (file changed since validation →
reject); duplicate hashes inside the package → reject; a hash already on an
ACTIVE asset **of the same POI** → reject. Scope honestly stated: the check
queries `poi_asset_assets` filtered by this `poi_id` + `status='active'`,
so a cross-POI duplicate or re-ingest of a RETIRED asset's bytes is NOT
blocked by this gate.
Uploads fail if the destination object already exists (no overwrites); a
DB-insert failure triggers best-effort cleanup of the uploaded object.
Inserted assets start `status='active'`, `embedding_status='pending'`
(enrichment backfills).

Content-level QC (image QC, video QC double-pass, retry-on-fail) is the
GENERATION CHAIN's responsibility before packaging; the platform gate only
guarantees structural integrity (schema, real files, hashes, probeable
media, verified identity). Do not ship unreviewed content and expect ingest
to catch it.

## 7. New-producer checklist

A new generation chain joins the platform by:

1. Producing packages that pass §6.1 schema with its own stable
   `generation_line` value.
2. Resolving POIs to exact `poi_id`s (§6.2): known POIs via the resolver;
   genuinely new POIs ONLY via `onboard_poi_candidates.py` with operator
   approval — never by inventing `poi_id`s.
3. Running content QC before packaging (§6.3) and iterating
   `validate_package.py` dry-runs until `status: validated`.
4. Live-writing only via `live_write.py --apply` and keeping the receipts.
5. Never importing platform internals; the package folder + receipts are
   the entire interface.
6. Adding itself to this section's producer table below (contract PR).

| Producer | generation_line | Since |
|---|---|---|
| Scene Motion (this repo) | `scene_motion` (`stages/_common.py`) | 2026-06 |
| External HF VideoGen returns | `scene_motion` via `build_hf_return_packages.py`, manifest `producer=external_hf_videogen` | 2026-06 |
