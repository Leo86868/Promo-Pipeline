#!/usr/bin/env python3
"""Batch runner for production-style one-video PGC runs.

This CLI intentionally keeps the core pipeline simple: each requested video is
one independent ``full_pipeline(..., n_variants=1)`` execution. The batch layer
owns POI/video iteration, voice rotation, optional Music Library rotation, and
per-video output directories.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from promo.cli.usage_events_preview import build_preview
from promo.cli.usage_events_writeback import record_usage_events, verify_usage_events
from promo.core.batch_selection import (
    DEFAULT_PGC_TARGET_DURATION_SEC,
    BatchSelectionError,
    asset_ids_from_valid_clip_rows,
    build_selection_payload,
    fetch_recent_usage_poi_ids,
    fetch_ready_embedding_asset_ids,
    fetch_valid_clip_rows,
)
from promo.core import config
from promo.core import sanitize_poi_name as _safe_poi_dir
from promo.core.drive_staging import (
    build_staging_inventory,
    handoff_items_from_inventory,
    write_json,
)
from promo.core.drive_upload import (
    OAuthDriveUploader,
    build_drive_upload_config,
    upload_staging_inventory,
)
from promo.core.music_library import SupabaseMusicLibrary
from promo.core.pipeline.release_handoff import build_release_handoff
from promo.core.release_candidates import register_release_candidates
from promo.core.run_receipt import (
    build_run_receipt,
    build_video_record,
    mark_render_result,
    mark_rendering,
    write_run_receipt,
)

load_dotenv()

logger = logging.getLogger(__name__)
MAX_AUTOPILOT_RETRIES = 3


@dataclass(frozen=True)
class BatchPoi:
    name: str
    location: str
    poi_id: str | None
    canonical_key: str | None


@dataclass(frozen=True)
class BatchItem:
    poi: BatchPoi
    video_index: int
    output_dir: str
    output_path: str
    voice_key: str
    music_id: str | None
    seed: int | None


@dataclass(frozen=True)
class DriveUploadTarget:
    uploader: Any
    parent_folder_id: str | None
    parent_folder_name: str


@dataclass(frozen=True)
class PreparedSelectionBatch:
    batch_path: str
    selection_summary_path: str
    payload: dict[str, Any]


def _positive_int(value: Any, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return parsed


def _positive_float(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive number") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive number")
    return parsed


def load_batch_spec(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("batch file must contain a JSON object")
    return data


def parse_batch_pois(spec: dict[str, Any]) -> list[BatchPoi]:
    raw_pois = spec.get("pois")
    if not isinstance(raw_pois, list) or not raw_pois:
        raise ValueError("batch file requires a non-empty pois array")

    pois: list[BatchPoi] = []
    for index, raw in enumerate(raw_pois, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"pois[{index}] must be an object")
        name = raw.get("name") or raw.get("poi_name") or raw.get("display_name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"pois[{index}] requires name")
        poi_id = raw.get("poi_id")
        canonical_key = raw.get("canonical_key")
        if not poi_id and not canonical_key:
            raise ValueError(f"pois[{index}] requires poi_id or canonical_key")
        pois.append(
            BatchPoi(
                name=name.strip(),
                location=str(raw.get("location") or "").strip(),
                poi_id=str(poi_id).strip() if poi_id else None,
                canonical_key=str(canonical_key).strip() if canonical_key else None,
            )
        )
    return pois


def parse_voice_keys(raw: str | Sequence[str] | None) -> list[str]:
    if raw is None:
        return ["jarnathan", "hope", "heather"]
    if isinstance(raw, str):
        values = [item.strip() for item in raw.split(",")]
    else:
        values = [str(item).strip() for item in raw]
    voices = [item for item in values if item]
    if not voices:
        raise ValueError("at least one voice is required")
    return voices


def _create_supabase_client_from_env() -> Any:
    url = os.environ.get("SUPABASE_URL")
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
        or os.environ.get("SUPABASE_ANON_KEY")
    )
    if not url or not key:
        raise ValueError("SUPABASE_URL and a Supabase key are required")
    try:
        from supabase import create_client
    except ImportError as exc:
        raise ValueError("supabase package is required for run_batch") from exc
    return create_client(url, key)


def _create_drive_upload_target_from_env() -> DriveUploadTarget:
    upload_config = build_drive_upload_config(
        credentials_file=config.google_credentials_file(),
        token_file=config.pgc_google_token_file() or None,
        parent_folder_id=config.pgc_drive_parent_folder_id() or None,
        parent_folder_name=config.pgc_drive_parent_folder_name(),
    )
    return DriveUploadTarget(
        uploader=OAuthDriveUploader(upload_config),
        parent_folder_id=upload_config.parent_folder_id,
        parent_folder_name=upload_config.parent_folder_name,
    )


def prepare_selected_batch(
    *,
    output_root: str,
    poi_count: int,
    videos_per_poi: int,
    target_duration_sec: float = DEFAULT_PGC_TARGET_DURATION_SEC,
    cooldown_days: int = 3,
    seed: int | None = None,
    classification_field: str | None = None,
    classification_value: str | None = None,
    allow_shortage: bool = False,
    client_factory: Callable[[], Any] = _create_supabase_client_from_env,
    valid_clip_rows_fetcher: Callable[
        [Any], list[dict[str, Any]]
    ] = fetch_valid_clip_rows,
    ready_embedding_asset_ids_fetcher: Callable[
        [Any, list[str]], set[str]
    ] = fetch_ready_embedding_asset_ids,
    recent_usage_poi_ids_fetcher: Callable[..., set[str]] = fetch_recent_usage_poi_ids,
) -> PreparedSelectionBatch:
    if bool(classification_field) != bool(classification_value):
        raise BatchSelectionError(
            "classification_field and classification_value must be provided together"
        )
    resolved_poi_count = _positive_int(poi_count, "poi_count")
    resolved_videos_per_poi = _positive_int(videos_per_poi, "videos_per_poi")
    resolved_target_duration = _positive_float(target_duration_sec, "target_duration_sec")
    if cooldown_days < 0:
        raise BatchSelectionError("cooldown_days must be >= 0")

    client = client_factory()
    rows = valid_clip_rows_fetcher(client)
    ready_embedding_asset_ids = ready_embedding_asset_ids_fetcher(
        client,
        asset_ids_from_valid_clip_rows(rows),
    )
    cooldown_poi_ids = recent_usage_poi_ids_fetcher(
        client,
        cooldown_days=int(cooldown_days),
    )
    payload = build_selection_payload(
        rows=rows,
        poi_count=resolved_poi_count,
        videos_per_poi=resolved_videos_per_poi,
        candidate_ready_asset_ids=ready_embedding_asset_ids,
        cooldown_poi_ids=cooldown_poi_ids,
        cooldown_days=int(cooldown_days),
        target_duration_sec=resolved_target_duration,
        seed=seed,
        classification_field=classification_field,
        classification_value=classification_value,
        allow_shortage=allow_shortage,
    )

    selection_summary_path = os.path.join(output_root, "selection_summary.json")
    batch_path = os.path.join(output_root, "batch.json")
    batch_spec = dict(payload["batch_spec"])
    batch_spec["selection"] = {
        "mode": "random_equal",
        "selection_summary_path": selection_summary_path,
        "status": payload["status"],
        "shortage_count": payload["shortage_count"],
        "cooldown_days": int(cooldown_days),
    }
    payload["batch_spec"] = batch_spec
    write_json(Path(selection_summary_path), payload)
    write_json(Path(batch_path), batch_spec)
    return PreparedSelectionBatch(
        batch_path=batch_path,
        selection_summary_path=selection_summary_path,
        payload=payload,
    )


def resolve_music_ids(
    *,
    target_duration_sec: float,
    count: int,
    client_factory: Callable[[], Any] = _create_supabase_client_from_env,
) -> list[str]:
    client = client_factory()
    tracks = SupabaseMusicLibrary(
        client,
        min_duration_sec=target_duration_sec,
    ).select_tracks(count=count)
    return [track["id"] for track in tracks]


def plan_batch_items(
    *,
    pois: Sequence[BatchPoi],
    videos_per_poi: int,
    target_duration_sec: float,
    output_root: str,
    voices: Sequence[str],
    music_ids: Sequence[str] | None,
    base_seed: int | None,
) -> list[BatchItem]:
    items: list[BatchItem] = []
    duration_label = f"{int(round(target_duration_sec))}s"
    for poi in pois:
        poi_slug = _safe_poi_dir(poi.name)
        for video_index in range(1, videos_per_poi + 1):
            run_dir = os.path.join(output_root, poi_slug, f"video_{video_index:03d}")
            output_path = os.path.join(
                run_dir,
                f"promo_{poi_slug}_video_{video_index:03d}_{duration_label}.mp4",
            )
            music_id = None
            if music_ids:
                music_id = music_ids[(video_index - 1) % len(music_ids)]
            seed = base_seed + len(items) if base_seed is not None else None
            items.append(
                BatchItem(
                    poi=poi,
                    video_index=video_index,
                    output_dir=run_dir,
                    output_path=output_path,
                    voice_key=voices[(video_index - 1) % len(voices)],
                    music_id=music_id,
                    seed=seed,
                )
            )
    return items


def build_compile_command(
    *,
    item: BatchItem,
    target_duration_sec: float,
    use_music_library: bool,
    script_candidates: int,
    tts_speed: float,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "promo.cli.compile_promo",
        "--poi",
        item.poi.name,
        "--target-duration-sec",
        str(float(target_duration_sec)),
        "--n-variants",
        "1",
        "--script-candidates",
        str(int(script_candidates)),
        "--tts-speed",
        str(float(tts_speed)),
        "--voice",
        item.voice_key,
        "--output",
        item.output_path,
    ]
    if item.poi.location:
        command.extend(["--location", item.poi.location])
    if item.poi.poi_id:
        command.extend(["--supabase-poi-id", item.poi.poi_id])
    elif item.poi.canonical_key:
        command.extend(["--supabase-canonical-key", item.poi.canonical_key])
    if use_music_library:
        if item.music_id:
            command.extend(["--supabase-music-id", item.music_id])
        else:
            command.append("--supabase-music-library")
    if item.seed is not None:
        command.extend(["--seed", str(item.seed)])
    return command


def run_compile_command(command: Sequence[str]) -> int:
    return subprocess.run(list(command), check=False).returncode


def _artifact_token(video: dict[str, Any]) -> str:
    poi_token = _safe_poi_dir(str(video.get("poi_name") or "poi"))
    return f"{poi_token}_video_{int(video.get('video_index') or 0):03d}"


def _add_quarantined_poi(
    receipt: dict[str, Any],
    video: dict[str, Any],
    *,
    reason: str,
) -> None:
    poi_key = video.get("poi_id") or video.get("canonical_key") or video.get("poi_name")
    quarantined = receipt.setdefault("quarantined_pois", [])
    if any(row.get("poi_key") == poi_key for row in quarantined):
        return
    quarantined.append({
        "poi_key": poi_key,
        "poi_id": video.get("poi_id"),
        "canonical_key": video.get("canonical_key"),
        "poi_name": video.get("poi_name"),
        "reason": reason,
    })


def _is_poi_quarantined(receipt: dict[str, Any], item: BatchItem) -> bool:
    poi_key = item.poi.poi_id or item.poi.canonical_key or item.poi.name
    return any(
        row.get("poi_key") == poi_key
        for row in receipt.get("quarantined_pois", [])
    )


def _mark_skipped_quarantined(video: dict[str, Any]) -> None:
    video["state"] = "skipped_quarantined_poi"
    video["error"] = "POI quarantined by earlier usage writeback failure"
    video["render"]["return_code"] = None


def _is_retryable_autopilot_error(error: Exception) -> bool:
    status = getattr(getattr(error, "resp", None), "status", None)
    status = status or getattr(error, "status_code", None)
    if status is not None:
        try:
            code = int(status)
        except (TypeError, ValueError):
            code = 0
        if code == 429 or code >= 500:
            return True
        if 400 <= code < 500:
            return False
    text = str(error).lower()
    return any(
        token in text
        for token in (
            "timeout",
            "timed out",
            "connection",
            "reset",
            "broken pipe",
            "eof",
            "temporarily unavailable",
            "rate limit",
            "too many requests",
        )
    )


def _retry_autopilot_step(operation: Callable[[], Any], description: str) -> Any:
    last_error: Exception | None = None
    for attempt in range(MAX_AUTOPILOT_RETRIES):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if not _is_retryable_autopilot_error(exc):
                raise
            if attempt < MAX_AUTOPILOT_RETRIES - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{description} failed after retries: {last_error}") from last_error


def _run_video_production_autopilot(
    *,
    video: dict[str, Any],
    receipt: dict[str, Any],
    receipt_path: str,
    handoff_dir: str,
    drive_target: DriveUploadTarget,
    supabase_client: Any,
    usage_recorder: Callable[[Any, list[dict[str, Any]]], dict[str, int]] = record_usage_events,
    usage_verifier: Callable[[Any, list[dict[str, Any]]], dict[str, Any]] = verify_usage_events,
    release_registrar: Callable[[Any, list[dict[str, Any]]], dict[str, Any]] = register_release_candidates,
) -> bool:
    manifest = video.get("manifest") or {}
    manifest_audit = video.get("manifest_audit") or {}
    if manifest.get("status") != "found" or manifest_audit.get("status") != "passed":
        return False

    Path(handoff_dir).mkdir(parents=True, exist_ok=True)
    token = _artifact_token(video)
    manifest_path = Path(str(manifest["path"]))
    inventory_path = Path(handoff_dir) / f"{token}_drive_inventory.json"
    handoff_items_path = Path(handoff_dir) / f"{token}_handoff_items.json"
    release_handoff_path = Path(handoff_dir) / f"{token}_release_handoff.json"

    try:
        inventory = build_staging_inventory([manifest_path], require_source_exists=True)
        inventory.update({
            "source_receipt_path": receipt_path,
            "batch_id": receipt["batch_id"],
            "paradigm": receipt["paradigm"],
            "created_at": receipt["created_at"],
        })
        inventory = upload_staging_inventory(
            inventory,
            drive_target.uploader,
            parent_folder_id=drive_target.parent_folder_id,
            parent_folder_name=drive_target.parent_folder_name,
            paradigm=receipt["paradigm"],
            date=str(receipt.get("created_at") or "")[:10],
            batch_id=receipt["batch_id"],
        )
        write_json(inventory_path, inventory)
        item = inventory["items"][0]
        video["drive_upload"] = {
            "status": item.get("drive_upload", {}).get("status"),
            "source_output_uri": item.get("source_output_uri"),
            "drive_file_id": item.get("drive_file_id"),
            "inventory_path": str(inventory_path),
            "folder_id": item.get("drive_upload", {}).get("folder_id"),
        }
        if item.get("staging_status") != "drive_uri_ready":
            video["state"] = "drive_upload_failed"
            video["error"] = item.get("drive_upload", {}).get("error") or "Drive upload failed"
            return False
    except Exception as exc:
        video["drive_upload"] = {
            "status": "failed",
            "source_output_uri": None,
            "inventory_path": str(inventory_path),
            "error": str(exc),
        }
        video["state"] = "drive_upload_failed"
        video["error"] = f"Drive upload failed: {exc}"
        return False

    try:
        preview = build_preview([manifest_path])
        events = preview["events"]
        rpc_result = _retry_autopilot_step(
            lambda: usage_recorder(supabase_client, events),
            "usage writeback",
        )
        verification = _retry_autopilot_step(
            lambda: usage_verifier(supabase_client, events),
            "usage verification",
        )
        video["usage"] = {
            "writeback_status": "verified" if verification["verified"] else "verification_failed",
            "event_count": len(events),
            "rpc_result": rpc_result,
            "verification": verification,
        }
        if not verification["verified"]:
            video["state"] = "usage_writeback_failed"
            video["error"] = "usage writeback verification failed"
            _add_quarantined_poi(
                receipt,
                video,
                reason="usage writeback verification failed",
            )
            return False
    except Exception as exc:
        video["usage"] = {
            "writeback_status": "failed",
            "event_count": 0,
            "error": str(exc),
        }
        video["state"] = "usage_writeback_failed"
        video["error"] = f"usage writeback failed: {exc}"
        _add_quarantined_poi(receipt, video, reason="usage writeback failed")
        return False

    try:
        handoff_items = handoff_items_from_inventory(inventory)
        write_json(handoff_items_path, {"items": handoff_items})
        release_handoff = build_release_handoff(
            handoff_items,
            items_base_dir=Path.cwd(),
            default_source_batch_id=receipt["batch_id"],
            default_source_pipeline=receipt["paradigm"],
        )
        write_json(release_handoff_path, release_handoff)
        records = release_handoff["release_candidates"]
        registration = _retry_autopilot_step(
            lambda: release_registrar(supabase_client, records),
            "release candidate registration",
        )
        verification = registration.get("verification") or {}
        verified = bool(verification.get("verified"))
        video["release_candidate"] = {
            "status": "verified" if verified else "verification_failed",
            "id": None,
            "handoff_path": str(release_handoff_path),
            "registration": registration,
        }
        if not verified:
            video["state"] = "release_candidate_failed_retryable"
            video["error"] = "release candidate verification failed after usage writeback"
            return False
    except Exception as exc:
        video["release_candidate"] = {
            "status": "failed_retryable",
            "id": None,
            "handoff_path": str(release_handoff_path),
            "error": str(exc),
        }
        video["state"] = "release_candidate_failed_retryable"
        video["error"] = f"release candidate registration failed after usage writeback: {exc}"
        return False

    video["state"] = "release_candidate_verified"
    video["error"] = None
    return True


def run_batch(
    *,
    batch_path: str,
    output_root: str,
    videos_per_poi: int | None = None,
    target_duration_sec: float | None = None,
    voices: Sequence[str] | None = None,
    use_music_library: bool = False,
    script_candidates: int = 1,
    tts_speed: float = 0.95,
    seed: int | None = None,
    jobs: int = 1,
    receipt_path: str | None = None,
    command_runner: Callable[[Sequence[str]], int] = run_compile_command,
    music_id_resolver: Callable[..., list[str]] = resolve_music_ids,
    production_autopilot: bool = False,
    handoff_dir: str | None = None,
    drive_upload_target_factory: Callable[[], DriveUploadTarget] = _create_drive_upload_target_from_env,
    supabase_client_factory: Callable[[], Any] = _create_supabase_client_from_env,
    usage_recorder: Callable[[Any, list[dict[str, Any]]], dict[str, int]] = record_usage_events,
    usage_verifier: Callable[[Any, list[dict[str, Any]]], dict[str, Any]] = verify_usage_events,
    release_registrar: Callable[[Any, list[dict[str, Any]]], dict[str, Any]] = register_release_candidates,
) -> int:
    if jobs != 1:
        raise ValueError("run_batch currently supports --jobs 1 only")

    spec = load_batch_spec(batch_path)
    selection_metadata = spec.get("selection") if isinstance(spec.get("selection"), dict) else None
    pois = parse_batch_pois(spec)
    resolved_videos_per_poi = _positive_int(
        videos_per_poi if videos_per_poi is not None else spec.get("videos_per_poi", 1),
        "videos_per_poi",
    )
    resolved_target_duration = _positive_float(
        target_duration_sec
        if target_duration_sec is not None
        else spec.get("target_duration_sec", config.default_duration_sec()),
        "target_duration_sec",
    )
    resolved_voices = parse_voice_keys(
        voices if voices is not None else spec.get("voices"),
    )
    os.makedirs(output_root, exist_ok=True)

    music_ids: list[str] | None = None
    if use_music_library:
        music_ids = music_id_resolver(
            target_duration_sec=resolved_target_duration,
            count=resolved_videos_per_poi,
        )

    items = plan_batch_items(
        pois=pois,
        videos_per_poi=resolved_videos_per_poi,
        target_duration_sec=resolved_target_duration,
        output_root=output_root,
        voices=resolved_voices,
        music_ids=music_ids,
        base_seed=seed,
    )
    item_commands = [
        build_compile_command(
            item=item,
            target_duration_sec=resolved_target_duration,
            use_music_library=use_music_library,
            script_candidates=script_candidates,
            tts_speed=tts_speed,
        )
        for item in items
    ]
    videos = [
        build_video_record(item=item, command=list(command))
        for item, command in zip(items, item_commands, strict=True)
    ]
    resolved_receipt_path = receipt_path or os.path.join(output_root, "RUN_RECEIPT.json")
    receipt = build_run_receipt(
        batch_path=batch_path,
        output_root=output_root,
        pois=list(pois),
        videos=videos,
        videos_per_poi=resolved_videos_per_poi,
        target_duration_sec=resolved_target_duration,
        voices=list(resolved_voices),
        use_music_library=use_music_library,
        script_candidates=script_candidates,
        tts_speed=tts_speed,
        seed=seed,
        production_autopilot=production_autopilot,
        selection_metadata=selection_metadata,
    )
    write_run_receipt(resolved_receipt_path, receipt)

    failures = 0
    drive_target: DriveUploadTarget | None = None
    supabase_client: Any | None = None
    resolved_handoff_dir = handoff_dir or os.path.join(output_root, "handoff")
    for item, command, video in zip(items, item_commands, videos, strict=True):
        if production_autopilot and _is_poi_quarantined(receipt, item):
            _mark_skipped_quarantined(video)
            failures += 1
            write_run_receipt(resolved_receipt_path, receipt)
            continue
        os.makedirs(item.output_dir, exist_ok=True)
        logger.info(
            "Batch item: poi=%s video=%d voice=%s music_id=%s output=%s",
            item.poi.name,
            item.video_index,
            item.voice_key,
            item.music_id,
            item.output_path,
        )
        mark_rendering(video)
        write_run_receipt(resolved_receipt_path, receipt)
        return_code = command_runner(command)
        mark_render_result(video, return_code=return_code)
        write_run_receipt(resolved_receipt_path, receipt)
        if return_code != 0:
            failures += 1
            logger.error(
                "Batch item failed: poi=%s video=%d exit_code=%d",
                item.poi.name,
                item.video_index,
                return_code,
            )
        elif (video.get("manifest_audit") or {}).get("status") in {"failed", "error"}:
            failures += 1
            logger.error(
                "Batch item manifest audit failed: poi=%s video=%d",
                item.poi.name,
                item.video_index,
            )
        elif production_autopilot:
            if drive_target is None:
                drive_target = drive_upload_target_factory()
            if supabase_client is None:
                supabase_client = supabase_client_factory()
            ok = _run_video_production_autopilot(
                video=video,
                receipt=receipt,
                receipt_path=resolved_receipt_path,
                handoff_dir=resolved_handoff_dir,
                drive_target=drive_target,
                supabase_client=supabase_client,
                usage_recorder=usage_recorder,
                usage_verifier=usage_verifier,
                release_registrar=release_registrar,
            )
            write_run_receipt(resolved_receipt_path, receipt)
            if not ok:
                failures += 1

    return 1 if failures else 0


def run_selected_batch(
    *,
    output_root: str,
    poi_count: int,
    videos_per_poi: int = 3,
    target_duration_sec: float = DEFAULT_PGC_TARGET_DURATION_SEC,
    cooldown_days: int = 3,
    classification_field: str | None = None,
    classification_value: str | None = None,
    allow_shortage: bool = False,
    voices: Sequence[str] | None = None,
    use_music_library: bool = False,
    script_candidates: int = 1,
    tts_speed: float = 0.95,
    seed: int | None = None,
    jobs: int = 1,
    receipt_path: str | None = None,
    command_runner: Callable[[Sequence[str]], int] = run_compile_command,
    music_id_resolver: Callable[..., list[str]] = resolve_music_ids,
    production_autopilot: bool = False,
    handoff_dir: str | None = None,
    drive_upload_target_factory: Callable[[], DriveUploadTarget] = _create_drive_upload_target_from_env,
    supabase_client_factory: Callable[[], Any] = _create_supabase_client_from_env,
    usage_recorder: Callable[[Any, list[dict[str, Any]]], dict[str, int]] = record_usage_events,
    usage_verifier: Callable[[Any, list[dict[str, Any]]], dict[str, Any]] = verify_usage_events,
    release_registrar: Callable[[Any, list[dict[str, Any]]], dict[str, Any]] = register_release_candidates,
    selection_client_factory: Callable[[], Any] = _create_supabase_client_from_env,
    valid_clip_rows_fetcher: Callable[
        [Any], list[dict[str, Any]]
    ] = fetch_valid_clip_rows,
    ready_embedding_asset_ids_fetcher: Callable[
        [Any, list[str]], set[str]
    ] = fetch_ready_embedding_asset_ids,
    recent_usage_poi_ids_fetcher: Callable[..., set[str]] = fetch_recent_usage_poi_ids,
) -> int:
    prepared = prepare_selected_batch(
        output_root=output_root,
        poi_count=poi_count,
        videos_per_poi=videos_per_poi,
        target_duration_sec=target_duration_sec,
        cooldown_days=cooldown_days,
        seed=seed,
        classification_field=classification_field,
        classification_value=classification_value,
        allow_shortage=allow_shortage,
        client_factory=selection_client_factory,
        valid_clip_rows_fetcher=valid_clip_rows_fetcher,
        ready_embedding_asset_ids_fetcher=ready_embedding_asset_ids_fetcher,
        recent_usage_poi_ids_fetcher=recent_usage_poi_ids_fetcher,
    )
    return run_batch(
        batch_path=prepared.batch_path,
        output_root=output_root,
        videos_per_poi=None,
        target_duration_sec=None,
        voices=voices,
        use_music_library=use_music_library,
        script_candidates=script_candidates,
        tts_speed=tts_speed,
        seed=seed,
        jobs=jobs,
        receipt_path=receipt_path,
        command_runner=command_runner,
        music_id_resolver=music_id_resolver,
        production_autopilot=production_autopilot,
        handoff_dir=handoff_dir,
        drive_upload_target_factory=drive_upload_target_factory,
        supabase_client_factory=supabase_client_factory,
        usage_recorder=usage_recorder,
        usage_verifier=usage_verifier,
        release_registrar=release_registrar,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one-video PGC jobs from a batch JSON")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--batch", help="Path to batch JSON")
    source.add_argument(
        "--select-random-pois",
        action="store_true",
        help="Read Supabase assets, select eligible POIs, and write output-dir/batch.json.",
    )
    parser.add_argument("--output-dir", required=True, help="Root directory for batch outputs")
    parser.add_argument(
        "--poi-count",
        type=int,
        default=None,
        help="Required with --select-random-pois.",
    )
    parser.add_argument("--videos-per-poi", type=int, default=None)
    parser.add_argument("--target-duration-sec", type=float, default=None)
    parser.add_argument("--cooldown-days", type=int, default=3)
    parser.add_argument("--classification-field", default=None)
    parser.add_argument("--classification-value", default=None)
    parser.add_argument(
        "--allow-shortage",
        action="store_true",
        help="With --select-random-pois, run all eligible POIs if fewer than requested pass.",
    )
    parser.add_argument(
        "--voices",
        default=None,
        help="Comma-separated voice keys. Default: jarnathan,hope,heather",
    )
    parser.add_argument("--supabase-music-library", action="store_true")
    parser.add_argument(
        "--script-candidates",
        type=int,
        default=config.default_script_candidates(),
    )
    parser.add_argument("--tts-speed", type=float, default=0.95)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Parallel jobs. Currently only 1 is supported.",
    )
    parser.add_argument(
        "--receipt-path",
        default=None,
        help="Optional RUN_RECEIPT.json path. Defaults to output-dir/RUN_RECEIPT.json.",
    )
    parser.add_argument(
        "--production-autopilot",
        action="store_true",
        help=(
            "After each audit-passed render, upload to private Drive, write/verify "
            "usage, and register/verify release_candidates."
        ),
    )
    parser.add_argument(
        "--handoff-dir",
        default=None,
        help="Directory for Drive inventory and release handoff JSON. Defaults to output-dir/handoff.",
    )
    return parser


def main() -> None:
    from promo.core.logging_config import configure_logging

    configure_logging()
    parser = _build_parser()
    args = parser.parse_args()
    try:
        voices = parse_voice_keys(args.voices) if args.voices is not None else None
        if args.select_random_pois:
            if args.poi_count is None:
                parser.error("--poi-count is required with --select-random-pois")
            if bool(args.classification_field) != bool(args.classification_value):
                parser.error(
                    "--classification-field and --classification-value must be passed together"
                )
            exit_code = run_selected_batch(
                output_root=args.output_dir,
                poi_count=args.poi_count,
                videos_per_poi=args.videos_per_poi or 3,
                target_duration_sec=(
                    args.target_duration_sec
                    if args.target_duration_sec is not None
                    else DEFAULT_PGC_TARGET_DURATION_SEC
                ),
                cooldown_days=args.cooldown_days,
                classification_field=args.classification_field,
                classification_value=args.classification_value,
                allow_shortage=args.allow_shortage,
                voices=voices,
                use_music_library=args.supabase_music_library,
                script_candidates=args.script_candidates,
                tts_speed=args.tts_speed,
                seed=args.seed,
                jobs=args.jobs,
                receipt_path=args.receipt_path,
                production_autopilot=args.production_autopilot,
                handoff_dir=args.handoff_dir,
            )
        else:
            exit_code = run_batch(
                batch_path=args.batch,
                output_root=args.output_dir,
                videos_per_poi=args.videos_per_poi,
                target_duration_sec=args.target_duration_sec,
                voices=voices,
                use_music_library=args.supabase_music_library,
                script_candidates=args.script_candidates,
                tts_speed=args.tts_speed,
                seed=args.seed,
                jobs=args.jobs,
                receipt_path=args.receipt_path,
                production_autopilot=args.production_autopilot,
                handoff_dir=args.handoff_dir,
            )
    except (BatchSelectionError, ValueError) as exc:
        parser.error(str(exc))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
