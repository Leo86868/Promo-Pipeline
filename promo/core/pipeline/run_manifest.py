"""Local run manifest builder/writer.

The manifest is one local receipt for one PGC pipeline invocation. It
references rendered outputs, sidecars, and final renderer timeline rows.
It does not perform Supabase reads or writes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from uuid import uuid4

from promo.core import sanitize_poi_name as _safe_poi_dir
from promo.core.pipeline.sidecar_writer import SidecarWriteResult, _write_sidecar_result


SCHEMA_VERSION = 1
USAGE_EVENT_CONTRACT_VERSION = "pgc_usage_v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z",
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _clip_id(value: Any) -> str:
    return str(value).zfill(4)


def _occurrence_id(variant_index: int, occurrence_index: int) -> str:
    return f"occ_{variant_index:04d}_{occurrence_index:06d}"


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _voice_backend(voice_key: str | None) -> str | None:
    if not voice_key:
        return None
    try:
        from promo.core.narrate.tts_engine import VOICE_CATALOG
    except Exception:  # noqa: BLE001 - manifest should not fail on catalog load.
        return None
    voice = VOICE_CATALOG.get(voice_key)
    if not isinstance(voice, dict):
        return None
    backend = voice.get("backend")
    return str(backend) if backend else None


def build_usage_event_id(
    *,
    manifest_id: str,
    variant_index: int,
    occurrence_id: str,
    asset_id: str,
) -> str:
    """Return the retry-safe usage event id without float seconds in the hash."""
    payload = "\n".join([
        USAGE_EVENT_CONTRACT_VERSION,
        manifest_id,
        str(int(variant_index)),
        occurrence_id,
        asset_id,
    ])
    return "sha256:" + sha256(payload.encode("utf-8")).hexdigest()


def validate_shared_asset_id_coverage(
    *,
    clip_paths: dict[str, str],
    shared_assets: list[dict[str, Any]] | None,
) -> None:
    """Fail fast when a shared-asset run cannot map every clip to asset_id."""
    if not shared_assets:
        return
    shared_by_id = {
        _clip_id(item.get("clip_id")): item
        for item in shared_assets
        if item.get("clip_id") is not None
    }
    missing = [
        _clip_id(raw_clip_id)
        for raw_clip_id in sorted(clip_paths.keys(), key=lambda value: _clip_id(value))
        if not shared_by_id.get(_clip_id(raw_clip_id), {}).get("asset_id")
    ]
    if missing:
        raise ValueError(
            "shared asset manifest requires asset_id for clip_id="
            + ", ".join(missing)
        )


def build_asset_snapshot(
    *,
    clip_paths: dict[str, str],
    clips_metadata: list[dict],
    clip_durations: dict[str, float],
    shared_assets: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    metadata_by_id = {
        _clip_id(item.get("id")): item
        for item in clips_metadata
        if item.get("id") is not None
    }
    shared_by_id = {
        _clip_id(item.get("clip_id")): item
        for item in (shared_assets or [])
        if item.get("clip_id") is not None
    }
    require_shared_asset_ids = bool(shared_assets)
    if require_shared_asset_ids:
        validate_shared_asset_id_coverage(
            clip_paths=clip_paths,
            shared_assets=shared_assets,
        )
    rows: list[dict[str, Any]] = []
    for raw_clip_id in sorted(clip_paths.keys(), key=lambda value: _clip_id(value)):
        clip_id = _clip_id(raw_clip_id)
        metadata = metadata_by_id.get(clip_id, {})
        shared = shared_by_id.get(clip_id, {})
        duration = (
            shared.get("duration_sec")
            or shared.get("source_duration_sec")
            or clip_durations.get(raw_clip_id)
            or clip_durations.get(clip_id)
            or metadata.get("source_duration_sec")
        )
        rows.append({
            "clip_id": clip_id,
            "asset_id": shared.get("asset_id"),
            "local_clip_path": clip_paths[raw_clip_id],
            "source_storage_bucket": shared.get("source_storage_bucket"),
            "source_storage_path": shared.get("source_storage_path"),
            "source_content_hash": shared.get("source_content_hash"),
            "source_duration_sec": _maybe_float(duration),
            "width": _maybe_int(shared.get("width")),
            "height": _maybe_int(shared.get("height")),
            "fps": _maybe_float(shared.get("fps")),
            "container": shared.get("container"),
            "video_codec": shared.get("video_codec"),
            "file_size_bytes": _maybe_int(shared.get("file_size_bytes")),
            "scene_description": _first_present(
                shared.get("scene_description"),
                metadata.get("scene_description"),
            ),
            "category": _first_present(shared.get("category"), metadata.get("category")),
            "camera_motion": _first_present(
                shared.get("camera_motion"),
                metadata.get("camera_motion"),
            ),
            "dominant_motion_phase": _first_present(
                shared.get("dominant_motion_phase"),
                metadata.get("dominant_motion_phase"),
            ),
            "shot_size": _first_present(shared.get("shot_size"), metadata.get("shot_size")),
            "main_subject": _first_present(
                shared.get("main_subject"),
                metadata.get("main_subject"),
            ),
            "analysis_model": _first_present(
                shared.get("analysis_model"),
                metadata.get("analysis_model"),
            ),
            "analysis_prompt_sha1": _first_present(
                shared.get("analysis_prompt_sha1"),
                metadata.get("analysis_prompt_sha1"),
            ),
            "analysis_generated_at": shared.get("analysis_generated_at"),
            "embedding_text": shared.get("embedding_text"),
            "embedding_model": shared.get("embedding_model"),
            "embedding_dim": _maybe_int(shared.get("embedding_dim")),
            "embedding_composition_version": _maybe_int(
                shared.get("embedding_composition_version"),
            ),
            "embedding_source_analysis_sha1": shared.get("embedding_source_analysis_sha1"),
            "embedding_status": shared.get("embedding_status"),
            "embedding_key": metadata.get("embedding_key"),
        })
    return rows


def _build_outputs(*, rendered_outputs: list[dict]) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for rendered in rendered_outputs:
        voice_key = rendered.get("voice_key")
        output = {
            "variant_index": int(rendered["variant_index"]),
            "variant_status": rendered.get("variant_status", "rendered"),
            "output_path": rendered.get("final_output_path")
            or rendered.get("render_output_path"),
            "render_output_path": rendered.get("render_output_path"),
            "final_output_path": rendered.get("final_output_path"),
            "target_duration_sec": _maybe_float(rendered.get("target_duration_sec")),
            "format_mode": rendered.get("format_mode"),
            "voice_key": voice_key,
            "voice_backend": _voice_backend(str(voice_key)) if voice_key else None,
            "bgm_path": rendered.get("bgm_path"),
            "file_size_bytes": rendered.get("file_size_bytes"),
        }
        music = rendered.get("music")
        if isinstance(music, dict):
            output["music"] = dict(music)
        for key in (
            "music_label",
            "music_id",
            "music_name",
            "music_duration_sec",
            "music_drive_file_id",
        ):
            value = rendered.get(key)
            if value is None and isinstance(music, dict):
                value = music.get(key)
            if value is not None:
                output[key] = value
        outputs.append(output)
    return outputs


def _build_sidecars(sidecar_paths: dict[str, str]) -> dict[str, str | None]:
    return {
        "clip_assignments": sidecar_paths.get("clip_assignments"),
        "tts_metrics": sidecar_paths.get("tts_metrics"),
        "match_quality": sidecar_paths.get("match_quality"),
    }


def _build_timeline_entries(
    rendered_outputs: list[dict],
    *,
    asset_id_by_clip_id: dict[str, str | None],
    require_asset_ids: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rendered in sorted(rendered_outputs, key=lambda item: int(item["variant_index"])):
        variant_index = int(rendered["variant_index"])
        for occurrence_index, entry in enumerate(rendered.get("timeline_entries", [])):
            usage_role = str(entry.get("usage_role") or "assigned_phrase")
            clip_id = _clip_id(entry.get("clip_id"))
            asset_id = entry.get("asset_id") or asset_id_by_clip_id.get(clip_id)
            if require_asset_ids and not asset_id:
                raise ValueError(
                    "shared asset timeline entries require asset_id "
                    f"for clip_id={clip_id}"
                )
            rows.append({
                "variant_index": variant_index,
                "occurrence_index": occurrence_index,
                "occurrence_id": _occurrence_id(variant_index, occurrence_index),
                "usage_role": usage_role,
                "clip_id": clip_id,
                "asset_id": asset_id,
                "segment": None
                if usage_role == "bridge_tail"
                else _maybe_int(entry.get("segment")),
                "trim_start_sec": _maybe_float(entry.get("trim_start_sec")),
                "trim_end_sec": _maybe_float(entry.get("trim_end_sec")),
                "display_start_sec": _maybe_float(entry.get("display_start_sec")),
                "display_end_sec": _maybe_float(entry.get("display_end_sec")),
                "source_duration_sec": _maybe_float(entry.get("source_duration_sec")),
            })
    return rows


def build_usage_events_from_manifest(
    manifest: dict[str, Any],
    *,
    retrieval_contract: str | None = None,
    retrieval_fallback_reason: str | None = None,
) -> list[dict[str, Any]]:
    """Build future RPC payload rows from a fully identified manifest.

    This is a pure helper for tests/adapters. It does not write to Supabase.
    """
    manifest_id = manifest.get("manifest_id")
    run_id = manifest.get("run_id")
    poi_id = (manifest.get("poi") or {}).get("poi_id")
    if not manifest_id or not run_id or not poi_id:
        raise ValueError("manifest_id, run_id, and poi.poi_id are required")

    outputs_by_variant = {
        int(output["variant_index"]): output
        for output in manifest.get("outputs", [])
    }
    sidecars = manifest.get("sidecars", {})
    events: list[dict[str, Any]] = []
    for entry in manifest.get("timeline_entries", []):
        variant_index = int(entry["variant_index"])
        output = outputs_by_variant.get(variant_index)
        asset_id = entry.get("asset_id")
        occurrence_id = entry.get("occurrence_id")
        if output is None:
            raise ValueError(f"missing output for variant_index={variant_index}")
        if not asset_id or not occurrence_id:
            raise ValueError("timeline entries require asset_id and occurrence_id")
        events.append({
            "event_id": build_usage_event_id(
                manifest_id=str(manifest_id),
                variant_index=variant_index,
                occurrence_id=str(occurrence_id),
                asset_id=str(asset_id),
            ),
            "run_id": run_id,
            "manifest_id": manifest_id,
            "poi_id": poi_id,
            "asset_id": asset_id,
            "clip_id": entry["clip_id"],
            "variant_index": variant_index,
            "occurrence_index": int(entry["occurrence_index"]),
            "occurrence_id": occurrence_id,
            "usage_role": entry["usage_role"],
            "segment": entry.get("segment"),
            "trim_start_sec": entry["trim_start_sec"],
            "display_start_sec": entry["display_start_sec"],
            "display_end_sec": entry["display_end_sec"],
            "source_duration_sec": entry["source_duration_sec"],
            "output_path": output["output_path"],
            "target_duration_sec": output.get("target_duration_sec"),
            "format_mode": output.get("format_mode"),
            "voice_key": output.get("voice_key"),
            "voice_backend": output.get("voice_backend"),
            "retrieval_contract": retrieval_contract,
            "retrieval_fallback_reason": retrieval_fallback_reason,
            "clip_assignments_sidecar_path": sidecars.get("clip_assignments"),
            "tts_metrics_sidecar_path": sidecars.get("tts_metrics"),
            "match_quality_sidecar_path": sidecars.get("match_quality"),
            "created_at": manifest.get("created_at"),
        })
    return events


def build_run_manifest(
    *,
    poi_name: str,
    location: str,
    target_duration_sec: float,
    n_variants: int,
    script_candidates: int,
    format_selector: str,
    embedding_cache_active: bool,
    clip_paths: dict[str, str],
    clips_metadata: list[dict],
    clip_durations: dict[str, float],
    rendered_outputs: list[dict],
    sidecar_paths: dict[str, str],
    poi_id: str | None = None,
    canonical_key: str | None = None,
    shared_assets: list[dict[str, Any]] | None = None,
    skip_analysis: bool | None = None,
    tts_speed: float | None = None,
    seed: int | None = None,
    run_id: str | None = None,
    manifest_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    asset_snapshot = build_asset_snapshot(
        clip_paths=clip_paths,
        clips_metadata=clips_metadata,
        clip_durations=clip_durations,
        shared_assets=shared_assets,
    )
    asset_id_by_clip_id = {
        row["clip_id"]: row.get("asset_id")
        for row in asset_snapshot
    }
    timeline_entries = _build_timeline_entries(
        rendered_outputs,
        asset_id_by_clip_id=asset_id_by_clip_id,
        require_asset_ids=bool(shared_assets),
    )
    poi = {
        "poi_id": poi_id,
        "display_name": poi_name,
        "pgc_slug": _safe_poi_dir(poi_name),
        "location": location,
    }
    if canonical_key is not None:
        poi["canonical_key"] = canonical_key
    run_config: dict[str, Any] = {
        "target_duration_sec": float(target_duration_sec),
        "n_variants": int(n_variants),
        "script_candidates": int(script_candidates),
        "format_selector": format_selector,
        "embedding_cache_active": bool(embedding_cache_active),
    }
    if skip_analysis is not None:
        run_config["skip_analysis"] = bool(skip_analysis)
    if tts_speed is not None:
        run_config["tts_speed"] = float(tts_speed)
    if seed is not None:
        run_config["seed"] = int(seed)
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_id": manifest_id or _new_id("manifest"),
        "run_id": run_id or _new_id("pgc_run"),
        "created_at": created_at or _utc_now(),
        "pipeline": {
            "repo": "pgc-pipeline",
            "entrypoint": "promo.cli.compile_promo",
        },
        "poi": poi,
        "run_config": run_config,
        "asset_snapshot": asset_snapshot,
        "sidecars": _build_sidecars(sidecar_paths),
        "outputs": _build_outputs(rendered_outputs=rendered_outputs),
        "timeline_entries": timeline_entries,
    }


def emit_run_manifest(
    *,
    sidecar_dir: str | None,
    poi_name: str,
    target_duration_sec: float,
    manifest: dict[str, Any],
) -> SidecarWriteResult:
    sidecar_tag = f"{_safe_poi_dir(poi_name)}_{int(round(target_duration_sec))}s"
    return _write_sidecar_result(
        sidecar_dir,
        f"run_manifest_{sidecar_tag}.json",
        manifest,
        "run_manifest",
    )
