"""Batch-level PGC run receipt helpers.

The receipt is the recoverable "order record" for a batch. It is intentionally
local JSON for now; live asset truth still belongs in Supabase usage events.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from promo.core.manifest_audit import audit_manifest_path


SCHEMA_VERSION = 1
DEFAULT_COOLDOWN_DAYS = 3
DEFAULT_BASE_MIN_ASSETS_FOR_FORMAT = 50
DEFAULT_EXTRA_VARIATION_ASSET_BUFFER = 10


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )


def required_active_assets(
    videos_per_poi: int,
    *,
    base_min_assets_for_format: int = DEFAULT_BASE_MIN_ASSETS_FOR_FORMAT,
    extra_variation_asset_buffer: int = DEFAULT_EXTRA_VARIATION_ASSET_BUFFER,
) -> int:
    extra_variations = max(int(videos_per_poi) - 1, 0)
    return int(base_min_assets_for_format) + int(extra_variation_asset_buffer) * extra_variations


def paradigm_for_duration(target_duration_sec: float) -> str:
    return f"pgc_{int(round(float(target_duration_sec)))}s"


def default_batch_id(output_root: str, *, created_at: str | None = None) -> str:
    name = Path(output_root).name.strip()
    if name:
        return name
    stamp = (created_at or utc_now_iso()).replace("-", "").replace(":", "")
    return f"pgc_batch_{stamp}"


def _poi_record(poi: Any) -> dict[str, Any]:
    return {
        "poi_id": poi.poi_id,
        "poi_name": poi.name,
        "location": poi.location,
        "canonical_key": poi.canonical_key,
    }


def build_video_record(
    *,
    item: Any,
    command: list[str],
) -> dict[str, Any]:
    return {
        "poi_id": item.poi.poi_id,
        "poi_name": item.poi.name,
        "canonical_key": item.poi.canonical_key,
        "video_index": int(item.video_index),
        "state": "planned",
        "voice_key": item.voice_key,
        "music_id": item.music_id,
        "seed": item.seed,
        "render": {
            "command": command,
            "output_path": item.output_path,
            "output_dir": item.output_dir,
            "return_code": None,
        },
        "manifest": {
            "status": "pending",
            "path": None,
            "manifest_id": None,
            "run_id": None,
        },
        "manifest_audit": {
            "status": "pending",
            "passed": None,
            "error_count": None,
        },
        "drive_upload": {
            "status": "not_implemented",
            "source_output_uri": None,
        },
        "usage": {
            "writeback_status": "not_written",
            "event_count": 0,
        },
        "release_candidate": {
            "status": "not_created",
            "id": None,
        },
        "error": None,
    }


def build_run_receipt(
    *,
    batch_path: str,
    output_root: str,
    pois: list[Any],
    videos: list[dict[str, Any]],
    videos_per_poi: int,
    target_duration_sec: float,
    voices: list[str],
    use_music_library: bool,
    script_candidates: int,
    tts_speed: float,
    seed: int | None,
    batch_id: str | None = None,
    created_at: str | None = None,
    production_autopilot: bool = False,
    selection_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    created = created_at or utc_now_iso()
    selection_mode = "provided_list"
    if isinstance(selection_metadata, dict):
        selection_mode = str(selection_metadata.get("mode") or selection_mode)
    implementation_gaps = [
        "receipt_based_resume_top_up",
    ]
    if not production_autopilot:
        implementation_gaps.extend([
            "drive_upload",
            "per_video_usage_writeback_orchestration",
            "release_candidate_registration",
            "poi_quarantine",
        ])
    return {
        "schema_version": SCHEMA_VERSION,
        "receipt_kind": "pgc_batch_run_receipt",
        "batch_id": batch_id or default_batch_id(output_root, created_at=created),
        "paradigm": paradigm_for_duration(target_duration_sec),
        "created_at": created,
        "updated_at": created,
        "request": {
            "batch_path": batch_path,
            "output_root": output_root,
            "mode": (
                "production_autopilot"
                if production_autopilot
                else "render_only_current_implementation"
            ),
            "selection": selection_mode,
            "selection_metadata": selection_metadata or None,
            "poi_count": len(pois),
            "videos_per_poi": int(videos_per_poi),
            "requested_videos": len(videos),
            "target_duration_sec": float(target_duration_sec),
            "voices": list(voices),
            "use_music_library": bool(use_music_library),
            "script_candidates": int(script_candidates),
            "tts_speed": float(tts_speed),
            "seed": seed,
            "filters": {
                "classification": None,
                "cooldown_days": DEFAULT_COOLDOWN_DAYS,
                "base_min_assets_for_format": DEFAULT_BASE_MIN_ASSETS_FOR_FORMAT,
                "extra_variation_asset_buffer": DEFAULT_EXTRA_VARIATION_ASSET_BUFFER,
                "required_active_assets": required_active_assets(videos_per_poi),
            },
            "implementation_gaps": implementation_gaps,
        },
        "selected_pois": [_poi_record(poi) for poi in pois],
        "skipped_pois": [],
        "quarantined_pois": [],
        "videos": videos,
        "summary": summarize_videos(videos),
    }


def discover_manifest(output_dir: str) -> dict[str, Any]:
    paths = sorted(Path(output_dir).glob("run_manifest_*.json"))
    if not paths:
        return {
            "status": "missing",
            "path": None,
            "manifest_id": None,
            "run_id": None,
        }
    path = max(paths, key=lambda item: item.stat().st_mtime)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "unreadable",
            "path": str(path),
            "manifest_id": None,
            "run_id": None,
        }
    return {
        "status": "found",
        "path": str(path),
        "manifest_id": payload.get("manifest_id"),
        "run_id": payload.get("run_id"),
    }


def audit_discovered_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("status") != "found" or not manifest.get("path"):
        return {
            "status": "not_run",
            "passed": None,
            "error_count": None,
        }
    audit = audit_manifest_path(Path(str(manifest["path"])))
    return {
        "status": "passed" if audit["passed"] else "failed",
        "passed": bool(audit["passed"]),
        "error_count": int(audit["error_count"]),
        "summary": audit["summary"],
        "errors": audit["errors"],
    }


def mark_rendering(video: dict[str, Any]) -> None:
    video["state"] = "rendering"
    video["error"] = None


def mark_render_result(video: dict[str, Any], *, return_code: int) -> None:
    video["render"]["return_code"] = int(return_code)
    if return_code == 0:
        video["state"] = "rendered"
        video["manifest"] = discover_manifest(video["render"]["output_dir"])
        if video["manifest"]["status"] == "missing":
            video["state"] = "rendered_manifest_missing"
            video["manifest_audit"] = {
                "status": "not_run",
                "passed": None,
                "error_count": None,
            }
        elif video["manifest"]["status"] == "found":
            try:
                video["manifest_audit"] = audit_discovered_manifest(video["manifest"])
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                video["state"] = "rendered_manifest_audit_error"
                video["manifest_audit"] = {
                    "status": "error",
                    "passed": False,
                    "error_count": None,
                    "error": str(exc),
                }
                video["error"] = f"manifest audit error: {exc}"
            else:
                if video["manifest_audit"]["passed"]:
                    video["state"] = "rendered_manifest_audited"
                else:
                    video["state"] = "rendered_manifest_audit_failed"
                    video["error"] = "manifest audit failed"
        else:
            video["state"] = f"rendered_manifest_{video['manifest']['status']}"
            video["manifest_audit"] = {
                "status": "not_run",
                "passed": None,
                "error_count": None,
            }
    else:
        video["state"] = "render_failed"
        video["manifest"] = {
            "status": "not_checked",
            "path": None,
            "manifest_id": None,
            "run_id": None,
        }
        video["manifest_audit"] = {
            "status": "not_run",
            "passed": None,
            "error_count": None,
        }
        video["error"] = f"compile_promo exited with code {return_code}"


def summarize_videos(videos: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "requested_videos": len(videos),
        "rendered_videos": sum(
            1 for video in videos
            if str(video.get("state", "")).startswith("rendered")
            or (video.get("render") or {}).get("return_code") == 0
        ),
        "failed_videos": sum(1 for video in videos if video.get("state") == "render_failed"),
        "manifest_found_videos": sum(
            1 for video in videos
            if (video.get("manifest") or {}).get("status") == "found"
        ),
        "manifest_audited_videos": sum(
            1 for video in videos
            if (video.get("manifest_audit") or {}).get("status") == "passed"
        ),
        "manifest_audit_failed_videos": sum(
            1 for video in videos
            if (video.get("manifest_audit") or {}).get("status") in {"failed", "error"}
        ),
        "usage_written_videos": sum(
            1 for video in videos
            if (video.get("usage") or {}).get("writeback_status") == "verified"
        ),
        "release_candidates_created": sum(
            1 for video in videos
            if (video.get("release_candidate") or {}).get("status") == "verified"
        ),
        "drive_uploaded_videos": sum(
            1 for video in videos
            if (video.get("drive_upload") or {}).get("status")
            in {"verified", "verified_existing"}
        ),
        "drive_upload_failed_videos": sum(
            1 for video in videos
            if (video.get("drive_upload") or {}).get("status") == "failed"
        ),
        "usage_failed_videos": sum(
            1 for video in videos
            if (video.get("usage") or {}).get("writeback_status")
            in {"failed", "verification_failed"}
        ),
        "release_candidate_failed_videos": sum(
            1 for video in videos
            if (video.get("release_candidate") or {}).get("status")
            in {"failed_retryable", "verification_failed"}
        ),
        "quarantined_skipped_videos": sum(
            1 for video in videos
            if video.get("state") == "skipped_quarantined_poi"
        ),
    }


def write_run_receipt(path: str, receipt: dict[str, Any]) -> None:
    receipt["updated_at"] = utc_now_iso()
    receipt["summary"] = summarize_videos(receipt.get("videos", []))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(receipt, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
