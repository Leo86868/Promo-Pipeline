"""Pure helpers for the shared POI asset read surface.

PGC reads shared-library media through ``public.poi_asset_valid_clips``.
This module validates fixture/live rows and projects them into the
``run_manifest.asset_snapshot`` shape. It does not perform Supabase I/O.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


POI_ASSET_VALID_CLIPS_VIEW = "poi_asset_valid_clips"
POI_ASSET_STORAGE_BUCKET = "poi-assets"

_POI_ID_RE = re.compile(r"^poi_[a-z0-9]+$")
_ASSET_ID_RE = re.compile(r"^asset_[a-z0-9]+$")
_CLIP_ID_RE = re.compile(r"^[0-9]{4}$")
_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")

_REQUIRED_FIELDS = (
    "poi_id",
    "asset_id",
    "clip_id",
    "source_storage_bucket",
    "source_storage_path",
    "source_content_hash",
    "duration_sec",
)

_SNAPSHOT_FIELDS = (
    "poi_id",
    "asset_id",
    "clip_id",
    "display_name",
    "canonical_key",
    "source_storage_bucket",
    "source_storage_path",
    "source_content_hash",
    "duration_sec",
    "width",
    "height",
    "fps",
    "container",
    "video_codec",
    "file_size_bytes",
    "scene_description",
    "category",
    "camera_motion",
    "dominant_motion_phase",
    "shot_size",
    "main_subject",
    "analysis_model",
    "analysis_prompt_sha1",
    "analysis_generated_at",
    "embedding_text",
    "embedding_model",
    "embedding_dim",
    "embedding_composition_version",
    "embedding_source_analysis_sha1",
    "embedding_status",
    "usage_count",
    "last_used_at",
    "status",
    "created_at",
    "updated_at",
)


class PoiAssetValidClipError(ValueError):
    """Raised when a poi_asset_valid_clips row violates PGC's contract."""


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise PoiAssetValidClipError(f"{field} is required")
    return value


def _optional_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PoiAssetValidClipError(f"{field} must be an integer") from exc
    if parsed <= 0:
        raise PoiAssetValidClipError(f"{field} must be positive")
    return parsed


def _optional_float(value: Any, field: str) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise PoiAssetValidClipError(f"{field} must be a number") from exc
    if parsed <= 0:
        raise PoiAssetValidClipError(f"{field} must be positive")
    return parsed


def normalize_poi_asset_valid_clip_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize one ``public.poi_asset_valid_clips`` row."""
    missing = [field for field in _REQUIRED_FIELDS if row.get(field) in (None, "")]
    if missing:
        raise PoiAssetValidClipError(f"missing required fields: {', '.join(missing)}")

    poi_id = _required_text(row, "poi_id")
    asset_id = _required_text(row, "asset_id")
    clip_id = _required_text(row, "clip_id")
    bucket = _required_text(row, "source_storage_bucket")
    path = _required_text(row, "source_storage_path")
    content_hash = _required_text(row, "source_content_hash")

    if not _POI_ID_RE.fullmatch(poi_id):
        raise PoiAssetValidClipError("poi_id must match ^poi_[a-z0-9]+$")
    if not _ASSET_ID_RE.fullmatch(asset_id):
        raise PoiAssetValidClipError("asset_id must match ^asset_[a-z0-9]+$")
    if not _CLIP_ID_RE.fullmatch(clip_id):
        raise PoiAssetValidClipError("clip_id must be a 4-digit string")
    if bucket != POI_ASSET_STORAGE_BUCKET:
        raise PoiAssetValidClipError("source_storage_bucket must be poi-assets")
    if path.startswith(f"{POI_ASSET_STORAGE_BUCKET}/"):
        raise PoiAssetValidClipError("source_storage_path must not include bucket name")
    expected_path = f"{poi_id}/clips/{asset_id}.mp4"
    if path != expected_path:
        raise PoiAssetValidClipError(
            f"source_storage_path must be {expected_path}",
        )
    if not _HASH_RE.fullmatch(content_hash):
        raise PoiAssetValidClipError("source_content_hash must be sha256:<64 hex>")
    if row.get("status") not in (None, "active"):
        raise PoiAssetValidClipError("poi_asset_valid_clips rows must be active")

    normalized = {field: row.get(field) for field in _SNAPSHOT_FIELDS}
    normalized["duration_sec"] = _optional_float(row.get("duration_sec"), "duration_sec")
    for field in ("width", "height", "file_size_bytes", "embedding_dim"):
        normalized[field] = _optional_int(row.get(field), field)
    for field in ("fps",):
        normalized[field] = _optional_float(row.get(field), field)
    if row.get("embedding_composition_version") is not None:
        normalized["embedding_composition_version"] = _optional_int(
            row.get("embedding_composition_version"),
            "embedding_composition_version",
        )
    return normalized


def build_poi_asset_valid_clip_snapshot(
    rows: Iterable[Mapping[str, Any]],
    *,
    poi_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return sorted manifest-ready rows from ``poi_asset_valid_clips``."""
    snapshot: list[dict[str, Any]] = []
    seen_clip_ids: set[str] = set()
    for row in rows:
        normalized = normalize_poi_asset_valid_clip_row(row)
        if poi_id is not None and normalized["poi_id"] != poi_id:
            raise PoiAssetValidClipError("all rows must belong to the requested poi_id")
        clip_id = normalized["clip_id"]
        if clip_id in seen_clip_ids:
            raise PoiAssetValidClipError(f"duplicate clip_id in POI snapshot: {clip_id}")
        seen_clip_ids.add(clip_id)
        snapshot.append(normalized)
    return sorted(snapshot, key=lambda item: item["clip_id"])
