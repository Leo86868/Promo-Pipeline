"""Manifest-backed final-video Drive staging helpers.

This module does not upload to Drive. It prepares and validates the durable
URI inventory that a manual or future API uploader can fill in.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class DriveStagingError(ValueError):
    """Raised when final-video staging data cannot form a safe handoff."""


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DriveStagingError(f"{field} is required")
    return value.strip()


def normalize_drive_file_id(value: Any) -> str:
    raw = _required_text(value, "drive_file_id")
    if raw.startswith(("http://", "https://", "drive:")):
        raise DriveStagingError("drive_file_id must be the raw Drive file id")
    return raw


def drive_uri(value: Any) -> str:
    return f"drive:{normalize_drive_file_id(value)}"


def source_video_key(*, manifest_id: str, variant_index: int) -> str:
    return f"manifest:{manifest_id}:variant:{int(variant_index)}"


def _resolve_output_path(manifest_path: Path, output: dict[str, Any]) -> str:
    raw = (
        output.get("final_output_path")
        or output.get("output_path")
        or output.get("render_output_path")
    )
    path = Path(_required_text(raw, "manifest.outputs[].output_path"))
    if not path.is_absolute():
        path = manifest_path.parent / path
    return str(path)


def _music_label(output: dict[str, Any]) -> str:
    music_label = output.get("music_label")
    if not music_label and isinstance(output.get("music"), dict):
        music_label = output["music"].get("music_label")
    return _required_text(music_label, "manifest.outputs[].music_label")


def build_staging_items_from_manifest(
    manifest_path: Path,
    *,
    require_source_exists: bool = True,
) -> list[dict[str, Any]]:
    manifest = _load_json(manifest_path)
    manifest_id = _required_text(manifest.get("manifest_id"), "manifest_id")
    run_id = _required_text(manifest.get("run_id"), "run_id")
    poi = manifest.get("poi") or {}
    poi_id = _required_text(poi.get("poi_id"), "manifest.poi.poi_id")
    poi_name = _required_text(
        poi.get("display_name") or manifest.get("poi_name"),
        "manifest.poi.display_name",
    )
    outputs = manifest.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise DriveStagingError("manifest outputs are required")

    items: list[dict[str, Any]] = []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        try:
            variant_index = int(output.get("variant_index") or 0)
        except (TypeError, ValueError) as exc:
            raise DriveStagingError("outputs[].variant_index is required") from exc
        if variant_index <= 0:
            raise DriveStagingError("outputs[].variant_index must be positive")
        local_output_path = _resolve_output_path(manifest_path, output)
        source_exists = Path(local_output_path).exists()
        if require_source_exists and not source_exists:
            raise DriveStagingError(f"output MP4 does not exist: {local_output_path}")
        key = source_video_key(manifest_id=manifest_id, variant_index=variant_index)
        items.append({
            "source_video_key": key,
            "manifest_id": manifest_id,
            "run_id": run_id,
            "run_manifest_path": str(manifest_path),
            "variant_index": variant_index,
            "poi_id": poi_id,
            "poi_name": poi_name,
            "music_label": _music_label(output),
            "local_output_path": local_output_path,
            "local_output_exists": source_exists,
            "drive_file_id": None,
            "source_output_uri": None,
            "staging_status": "pending_drive_upload",
        })
    if not items:
        raise DriveStagingError("manifest has no object outputs")
    return items


def build_staging_inventory(
    manifest_paths: list[Path],
    *,
    require_source_exists: bool = True,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for manifest_path in manifest_paths:
        items.extend(
            build_staging_items_from_manifest(
                manifest_path,
                require_source_exists=require_source_exists,
            )
        )
    keys = [item["source_video_key"] for item in items]
    if len(keys) != len(set(keys)):
        raise DriveStagingError("duplicate source_video_key in staging inventory")
    return {
        "schema_version": 1,
        "inventory_kind": "pgc_drive_staging_inventory",
        "items": items,
        "summary": summarize_inventory(items),
    }


def manifest_paths_from_receipt(receipt_path: Path) -> list[Path]:
    payload = _load_json(receipt_path)
    if not isinstance(payload, dict):
        raise DriveStagingError("receipt JSON must be an object")
    videos = payload.get("videos")
    if not isinstance(videos, list):
        raise DriveStagingError("receipt videos must be a list")

    paths: list[Path] = []
    seen: set[str] = set()
    for index, video in enumerate(videos, start=1):
        if not isinstance(video, dict):
            raise DriveStagingError(f"receipt videos[{index}] must be an object")
        manifest = video.get("manifest") or {}
        manifest_audit = video.get("manifest_audit") or {}
        if manifest.get("status") != "found":
            continue
        if manifest_audit.get("status") != "passed":
            continue
        manifest_path = Path(_required_text(manifest.get("path"), "manifest.path"))
        key = str(manifest_path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(manifest_path)

    if not paths:
        raise DriveStagingError("receipt contains no audit-passed manifest paths")
    return paths


def load_drive_file_map(path: Path) -> dict[str, str]:
    payload = _load_json(path)
    if isinstance(payload, dict) and "items" in payload:
        payload = payload["items"]
    if isinstance(payload, dict):
        return {
            str(key): normalize_drive_file_id(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        result: dict[str, str] = {}
        for index, row in enumerate(payload, start=1):
            if not isinstance(row, dict):
                raise DriveStagingError(f"drive map item {index} must be an object")
            key = _required_text(row.get("source_video_key"), "source_video_key")
            result[key] = normalize_drive_file_id(row.get("drive_file_id"))
        return result
    raise DriveStagingError("drive file map must be an object or list")


def apply_drive_file_map(
    inventory: dict[str, Any],
    drive_file_ids: dict[str, str],
) -> dict[str, Any]:
    mapped = {str(key): normalize_drive_file_id(value) for key, value in drive_file_ids.items()}
    seen: set[str] = set()
    for item in inventory.get("items", []):
        key = item["source_video_key"]
        drive_file_id = mapped.get(key)
        if not drive_file_id:
            continue
        if drive_file_id in seen:
            raise DriveStagingError("duplicate drive_file_id in staging inventory")
        seen.add(drive_file_id)
        item["drive_file_id"] = drive_file_id
        item["source_output_uri"] = f"drive:{drive_file_id}"
        item["staging_status"] = "drive_uri_ready"
    inventory["summary"] = summarize_inventory(inventory.get("items", []))
    return inventory


def summarize_inventory(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "item_count": len(items),
        "pending_drive_upload": sum(
            1 for item in items if item.get("staging_status") == "pending_drive_upload"
        ),
        "drive_uri_ready": sum(
            1 for item in items if item.get("staging_status") == "drive_uri_ready"
        ),
        "missing_source_outputs": sum(
            1 for item in items if not item.get("local_output_exists")
        ),
    }


def handoff_items_from_inventory(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in inventory.get("items", []):
        source_output_uri = item.get("source_output_uri")
        if item.get("staging_status") != "drive_uri_ready" or not source_output_uri:
            raise DriveStagingError(
                f"item is not Drive-ready: {item.get('source_video_key')}"
            )
        rows.append({
            "run_manifest_path": item["run_manifest_path"],
            "variant_index": item["variant_index"],
            "source_output_uri": source_output_uri,
        })
    return rows


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
