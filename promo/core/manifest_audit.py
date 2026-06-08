"""Production manifest audit helpers for PGC run manifests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from promo.core.pipeline.run_manifest import build_usage_events_from_manifest


class ManifestAuditError(ValueError):
    """Raised when a manifest cannot be loaded for audit."""


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _variant_index(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _add_error(errors: list[dict[str, str]], field: str, message: str) -> None:
    errors.append({"field": field, "message": message})


def audit_manifest(manifest: dict[str, Any], *, manifest_path: str | None = None) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    manifest_id = _text(manifest.get("manifest_id"))
    run_id = _text(manifest.get("run_id"))
    poi = manifest.get("poi") if isinstance(manifest.get("poi"), dict) else {}
    poi_id = _text(poi.get("poi_id"))
    poi_name = _text(poi.get("display_name"))

    if manifest.get("schema_version") != 1:
        _add_error(errors, "schema_version", "schema_version must be 1")
    if not manifest_id:
        _add_error(errors, "manifest_id", "manifest_id is required")
    if not run_id:
        _add_error(errors, "run_id", "run_id is required")
    if not poi_id:
        _add_error(errors, "poi.poi_id", "poi.poi_id is required")
    if not poi_name:
        _add_error(errors, "poi.display_name", "poi.display_name is required")

    outputs = manifest.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        _add_error(errors, "outputs", "outputs must be a non-empty list")
        outputs = []
    output_variants: set[int] = set()
    for index, output in enumerate(outputs):
        if not isinstance(output, dict):
            _add_error(errors, f"outputs[{index}]", "output must be an object")
            continue
        variant_index = _variant_index(output.get("variant_index"))
        if variant_index is None:
            _add_error(
                errors,
                f"outputs[{index}].variant_index",
                "variant_index must be a positive integer",
            )
        elif variant_index in output_variants:
            _add_error(
                errors,
                f"outputs[{index}].variant_index",
                "variant_index must be unique",
            )
        else:
            output_variants.add(variant_index)
        if not _text(
            output.get("final_output_path")
            or output.get("output_path")
            or output.get("render_output_path")
        ):
            _add_error(
                errors,
                f"outputs[{index}].output_path",
                "output path is required",
            )
        music_label = output.get("music_label")
        if not music_label and isinstance(output.get("music"), dict):
            music_label = output["music"].get("music_label")
        if not _text(music_label):
            _add_error(
                errors,
                f"outputs[{index}].music_label",
                "music_label is required",
            )

    asset_snapshot = manifest.get("asset_snapshot")
    if not isinstance(asset_snapshot, list) or not asset_snapshot:
        _add_error(
            errors,
            "asset_snapshot",
            "asset_snapshot must be a non-empty list",
        )
        asset_snapshot = []
    asset_ids: set[str] = set()
    for index, row in enumerate(asset_snapshot):
        if not isinstance(row, dict):
            _add_error(errors, f"asset_snapshot[{index}]", "asset row must be an object")
            continue
        asset_id = _text(row.get("asset_id"))
        if not asset_id:
            _add_error(
                errors,
                f"asset_snapshot[{index}].asset_id",
                "asset_id is required for production manifests",
            )
            continue
        if asset_id in asset_ids:
            _add_error(
                errors,
                f"asset_snapshot[{index}].asset_id",
                "asset_id must be unique",
            )
        asset_ids.add(asset_id)

    timeline_entries = manifest.get("timeline_entries")
    if not isinstance(timeline_entries, list) or not timeline_entries:
        _add_error(
            errors,
            "timeline_entries",
            "timeline_entries must be a non-empty list",
        )
        timeline_entries = []
    occurrence_ids: set[str] = set()
    for index, entry in enumerate(timeline_entries):
        if not isinstance(entry, dict):
            _add_error(errors, f"timeline_entries[{index}]", "entry must be an object")
            continue
        variant_index = _variant_index(entry.get("variant_index"))
        if variant_index is None:
            _add_error(
                errors,
                f"timeline_entries[{index}].variant_index",
                "variant_index must be a positive integer",
            )
        elif output_variants and variant_index not in output_variants:
            _add_error(
                errors,
                f"timeline_entries[{index}].variant_index",
                "variant_index must reference an output",
            )
        if not _text(entry.get("clip_id")):
            _add_error(errors, f"timeline_entries[{index}].clip_id", "clip_id is required")
        occurrence_id = _text(entry.get("occurrence_id"))
        if not occurrence_id:
            _add_error(
                errors,
                f"timeline_entries[{index}].occurrence_id",
                "occurrence_id is required",
            )
        elif occurrence_id in occurrence_ids:
            _add_error(
                errors,
                f"timeline_entries[{index}].occurrence_id",
                "occurrence_id must be unique",
            )
        else:
            occurrence_ids.add(occurrence_id)
        usage_role = _text(entry.get("usage_role"))
        if not usage_role:
            _add_error(
                errors,
                f"timeline_entries[{index}].usage_role",
                "usage_role is required",
            )
        if usage_role == "bridge_tail" and entry.get("segment") is not None:
            _add_error(
                errors,
                f"timeline_entries[{index}].segment",
                "bridge_tail segment must be null",
            )
        asset_id = _text(entry.get("asset_id"))
        if not asset_id:
            _add_error(
                errors,
                f"timeline_entries[{index}].asset_id",
                "asset_id is required for production usage writeback",
            )
        elif asset_ids and asset_id not in asset_ids:
            _add_error(
                errors,
                f"timeline_entries[{index}].asset_id",
                "asset_id must exist in asset_snapshot",
            )

    usage_event_count = 0
    unique_usage_event_id_count = 0
    if not errors:
        try:
            usage_events = build_usage_events_from_manifest(manifest)
        except (KeyError, TypeError, ValueError) as exc:
            _add_error(errors, "usage_events", str(exc))
        else:
            usage_event_ids = [event["event_id"] for event in usage_events]
            usage_event_count = len(usage_events)
            unique_usage_event_id_count = len(set(usage_event_ids))
            if usage_event_count != unique_usage_event_id_count:
                _add_error(
                    errors,
                    "usage_events.event_id",
                    "usage event IDs must be unique",
                )

    return {
        "manifest_path": manifest_path,
        "manifest_id": manifest_id,
        "run_id": run_id,
        "poi_id": poi_id,
        "poi_name": poi_name,
        "passed": not errors,
        "error_count": len(errors),
        "errors": errors,
        "summary": {
            "output_count": len(outputs),
            "timeline_entry_count": len(timeline_entries),
            "asset_snapshot_count": len(asset_snapshot),
            "usage_event_count": usage_event_count,
            "unique_usage_event_id_count": unique_usage_event_id_count,
        },
    }


def audit_manifest_path(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ManifestAuditError("manifest JSON must be an object")
    return audit_manifest(payload, manifest_path=str(path))


def audit_manifest_paths(paths: list[Path]) -> dict[str, Any]:
    audits = [audit_manifest_path(path) for path in paths]
    failed_count = sum(1 for audit in audits if not audit["passed"])
    return {
        "schema_version": 1,
        "audit_kind": "pgc_run_manifest_production_audit",
        "summary": {
            "manifest_count": len(audits),
            "passed_count": len(audits) - failed_count,
            "failed_count": failed_count,
        },
        "manifests": audits,
    }
