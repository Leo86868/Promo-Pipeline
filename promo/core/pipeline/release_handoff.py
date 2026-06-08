"""Build AIGC release-candidate handoff JSON from approved PGC manifests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ReleaseHandoffError(ValueError):
    """Raised when approved outputs cannot form a safe release handoff."""


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseHandoffError(f"{field} is required")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_drive_uri(item: dict[str, Any]) -> str:
    source_output_uri = item.get("source_output_uri")
    drive_file_id = item.get("drive_file_id")
    if source_output_uri:
        raw = _required_text(source_output_uri, "source_output_uri")
        if raw.startswith(("http://", "https://")):
            raise ReleaseHandoffError(
                "source_output_uri must be drive:<file_id>, not a URL"
            )
        if not raw.startswith("drive:") or raw == "drive:":
            raise ReleaseHandoffError("source_output_uri must be drive:<file_id>")
        return raw
    if drive_file_id:
        raw = _required_text(drive_file_id, "drive_file_id")
        if raw.startswith(("http://", "https://", "drive:")):
            raise ReleaseHandoffError("drive_file_id must be the raw Drive file id")
        return f"drive:{raw}"
    raise ReleaseHandoffError("source_output_uri or drive_file_id is required")


def _variant_index(item: dict[str, Any]) -> int:
    raw = item.get("variant_index", 1)
    try:
        variant_index = int(raw)
    except (TypeError, ValueError) as exc:
        raise ReleaseHandoffError("variant_index must be a positive integer") from exc
    if variant_index <= 0:
        raise ReleaseHandoffError("variant_index must be a positive integer")
    return variant_index


def _select_output(manifest: dict[str, Any], variant_index: int) -> dict[str, Any]:
    outputs = manifest.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise ReleaseHandoffError("manifest outputs are required")
    for output in outputs:
        if not isinstance(output, dict):
            continue
        try:
            output_variant_index = int(output.get("variant_index") or 0)
        except (TypeError, ValueError):
            output_variant_index = 0
        if output_variant_index == variant_index:
            return output
    raise ReleaseHandoffError(f"manifest output variant_index={variant_index} not found")


def _music_label(output: dict[str, Any]) -> str:
    music_label = output.get("music_label")
    if not music_label and isinstance(output.get("music"), dict):
        music_label = output["music"].get("music_label")
    return _required_text(music_label, "outputs[].music_label")


def load_handoff_items(path: Path) -> list[dict[str, Any]]:
    payload = _load_json(path)
    if isinstance(payload, dict):
        payload = payload.get("items")
    if not isinstance(payload, list) or not payload:
        raise ReleaseHandoffError("items JSON must contain a non-empty list")
    items: list[dict[str, Any]] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ReleaseHandoffError(f"items[{index}] must be an object")
        items.append(item)
    return items


def build_release_candidate_record(
    item: dict[str, Any],
    *,
    items_base_dir: Path,
    default_source_batch_id: str | None = None,
    default_approved_at: str | None = None,
    default_source_pipeline: str = "pgc_65s",
) -> dict[str, Any]:
    manifest_value = item.get("run_manifest_path") or item.get("manifest_path")
    manifest_path = Path(_required_text(manifest_value, "run_manifest_path"))
    if not manifest_path.is_absolute():
        manifest_path = items_base_dir / manifest_path
    manifest = _load_json(manifest_path)

    variant_index = _variant_index(item)
    output = _select_output(manifest, variant_index)
    poi = manifest.get("poi") or {}
    poi_id = _required_text(poi.get("poi_id"), "manifest.poi.poi_id")
    poi_name = _required_text(
        poi.get("display_name") or manifest.get("poi_name"),
        "manifest.poi.display_name",
    )
    manifest_id = _required_text(manifest.get("manifest_id"), "manifest_id")
    run_id = _required_text(manifest.get("run_id"), "run_id")

    record = {
        "source_pipeline": (
            _optional_text(item.get("source_pipeline")) or default_source_pipeline
        ),
        "source_video_key": f"manifest:{manifest_id}:variant:{variant_index}",
        "poi_id": poi_id,
        "poi_name": poi_name,
        "source_output_uri": _normalize_drive_uri(item),
        "source_run_id": run_id,
        "status": _optional_text(item.get("status")) or "approved",
        "approved_at": _required_text(
            item.get("approved_at") or default_approved_at or _utc_now_iso(),
            "approved_at",
        ),
        "music_label": _music_label(output),
    }

    source_batch_id = (
        _optional_text(item.get("source_batch_id")) or default_source_batch_id
    )
    if source_batch_id:
        record["source_batch_id"] = source_batch_id
    return record


def build_release_handoff(
    items: list[dict[str, Any]],
    *,
    items_base_dir: Path,
    default_source_batch_id: str | None = None,
    default_approved_at: str | None = None,
    default_source_pipeline: str = "pgc_65s",
) -> dict[str, list[dict[str, Any]]]:
    records = [
        build_release_candidate_record(
            item,
            items_base_dir=items_base_dir,
            default_source_batch_id=default_source_batch_id,
            default_approved_at=default_approved_at,
            default_source_pipeline=default_source_pipeline,
        )
        for item in items
    ]
    source_keys = [record["source_video_key"] for record in records]
    if len(source_keys) != len(set(source_keys)):
        raise ReleaseHandoffError("duplicate source_video_key in handoff")
    drive_uris = [record["source_output_uri"] for record in records]
    if len(drive_uris) != len(set(drive_uris)):
        raise ReleaseHandoffError("duplicate source_output_uri in handoff")
    return {"release_candidates": records}


def build_release_handoff_from_items_file(
    path: Path,
    *,
    default_source_batch_id: str | None = None,
    default_approved_at: str | None = None,
    default_source_pipeline: str = "pgc_65s",
) -> dict[str, list[dict[str, Any]]]:
    return build_release_handoff(
        load_handoff_items(path),
        items_base_dir=path.parent,
        default_source_batch_id=default_source_batch_id,
        default_approved_at=default_approved_at,
        default_source_pipeline=default_source_pipeline,
    )
