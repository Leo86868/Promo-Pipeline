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
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
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
from promo.core.final_upscale import (
    FinalUpscaleError,
    FinalUpscalePolicy,
    create_final_video_upscaler_from_env,
    normalize_final_upscale_policy,
    probe_video_properties,
    verify_final_upscale_output,
)
from promo.core.music_library import SupabaseMusicLibrary
from promo.core.pipeline.release_handoff import build_release_handoff
from promo.core.release_candidates import register_release_candidates
from promo.core.run_receipt import (
    build_run_receipt,
    build_video_record,
    mark_render_result,
    mark_rendering,
    mark_stage_finished,
    mark_stage_started,
    plan_resume_action,
    reset_video_record_for_rerender,
    utc_now_iso,
    write_run_receipt,
)
from promo.core.source_resolution_policy import normalize_source_resolution_policy

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
    # P2 step 5 — always set: (base_seed or 0) + canonical_ordinal.
    # Unlike ``seed`` it does NOT require --seed, so the hook deck
    # rotates in unseeded production batches too (music convention).
    hook_seed: int = 0


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
    source_resolution_policy: dict[str, Any] | None = None,
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
        source_resolution_policy=source_resolution_policy,
    )

    selection_summary_path = os.path.join(output_root, "selection_summary.json")
    batch_path = os.path.join(output_root, "batch.json")
    batch_spec = dict(payload["batch_spec"])
    source_policy = normalize_source_resolution_policy(
        batch_spec.get("source_resolution_policy")
    )
    final_policy = normalize_final_upscale_policy(
        None,
        source_policy_mode=source_policy.mode,
    )
    if final_policy.required or final_policy.enabled:
        batch_spec["final_upscale_policy"] = final_policy.to_dict()
    batch_spec["selection"] = {
        "mode": "random_equal",
        "selection_summary_path": selection_summary_path,
        "status": payload["status"],
        "shortage_count": payload["shortage_count"],
        "cooldown_days": int(cooldown_days),
    }
    if batch_spec.get("source_resolution_policy"):
        batch_spec["selection"]["source_resolution_policy"] = batch_spec[
            "source_resolution_policy"
        ]
    if batch_spec.get("final_upscale_policy"):
        batch_spec["selection"]["final_upscale_policy"] = batch_spec[
            "final_upscale_policy"
        ]
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
    shuffle_seed: int | None = None,
    client_factory: Callable[[], Any] = _create_supabase_client_from_env,
) -> list[str]:
    """Return ALL eligible track ids (shuffled per batch) for rotation.

    2026-06-09 fix: previously returned exactly ``count`` ids — and the
    library query ordered by duration ascending, so every batch reused
    the same shortest tracks. Returning the full eligible pool lets
    ``plan_batch_items`` cycle every track; ``shuffle_seed`` varies the
    rotation order across batches.
    """
    client = client_factory()
    tracks = SupabaseMusicLibrary(
        client,
        min_duration_sec=target_duration_sec,
    ).select_tracks(count=count, shuffle_seed=shuffle_seed)
    if len(tracks) < count:
        logger.warning(
            "music_library has only %d track(s) >= %.1fs (requested %d) — "
            "tracks will repeat within POIs; add longer tracks to the library.",
            len(tracks), target_duration_sec, count,
        )
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
    # POI round-robin (2026-06-10, tail pipelining): adjacent items must hit
    # DIFFERENT POIs so the renderer never has to wait on the previous tail —
    # same-POI videos cannot overlap because their usage events must stay
    # ordered. Output paths are (poi, video_index)-keyed, so reordering only
    # changes execution order, not where anything lands.
    for video_index in range(1, videos_per_poi + 1):
        for poi_ordinal, poi in enumerate(pois):
            poi_slug = _safe_poi_dir(poi.name)
            run_dir = os.path.join(output_root, poi_slug, f"video_{video_index:03d}")
            output_path = os.path.join(
                run_dir,
                f"promo_{poi_slug}_video_{video_index:03d}_{duration_label}.mp4",
            )
            # Music + seed key off the CANONICAL (POI-major) ordinal, not the
            # round-robin execution ordinal. Two reasons (2026-06-10 review):
            # (1) a POI's videos sit poi_count apart in execution order, so
            # whenever poi_count is a multiple of the track-pool size the
            # execution ordinal pins ONE track to all of a POI's videos —
            # the same per-POI monotony the 2026-06-09 rotation fix removed
            # (which rotated by global ordinal to stop video_001 of every
            # POI sharing a track); the canonical ordinal keeps a POI's
            # videos on consecutive tracks in every batch shape.
            # (2) it keeps seed→(music, seed) per video identical to the
            # pre-pipelining serial code, so seeded batches stay comparable
            # across versions.
            canonical_ordinal = poi_ordinal * videos_per_poi + (video_index - 1)
            music_id = None
            if music_ids:
                music_id = music_ids[canonical_ordinal % len(music_ids)]
            seed = base_seed + canonical_ordinal if base_seed is not None else None
            items.append(
                BatchItem(
                    poi=poi,
                    video_index=video_index,
                    output_dir=run_dir,
                    output_path=output_path,
                    voice_key=voices[(video_index - 1) % len(voices)],
                    music_id=music_id,
                    seed=seed,
                    hook_seed=(base_seed or 0) + canonical_ordinal,
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
    source_resolution_policy: dict[str, Any] | None = None,
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
    command.extend(["--hook-seed", str(item.hook_seed)])
    resolved_source_policy = normalize_source_resolution_policy(source_resolution_policy)
    if resolved_source_policy.mode != "best_available":
        command.extend([
            "--source-resolution-policy-mode",
            resolved_source_policy.mode,
            "--source-target-width",
            str(resolved_source_policy.target_width),
            "--source-width-tolerance-px",
            str(resolved_source_policy.tolerance_px),
            "--source-aspect-ratio-min",
            str(resolved_source_policy.aspect_ratio_min),
            "--source-aspect-ratio-max",
            str(resolved_source_policy.aspect_ratio_max),
        ])
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


def _reject_sticky_replay_env() -> None:
    """Refuse batch work while PROMO_REPLAY_SCRIPT is set (2026-06-11
    review bug ②): the env var is inherited by every compile subprocess,
    so a forgotten A/B switch would render ONE script for every POI in
    the batch — whole batch wasted, no guard would catch it. Replay is a
    single-video compile_promo affair only."""
    if os.environ.get("PROMO_REPLAY_SCRIPT", "").strip():
        raise ValueError(
            "PROMO_REPLAY_SCRIPT is set — a batch would replay one recorded "
            "script for EVERY video. Unset it; replay is for single "
            "compile_promo A/B renders only."
        )


def _item_poi_key(item: BatchItem) -> str:
    return item.poi.poi_id or item.poi.canonical_key or item.poi.name


def _is_poi_quarantined(receipt: dict[str, Any], item: BatchItem) -> bool:
    poi_key = _item_poi_key(item)
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


def _upscaled_output_path(*, handoff_dir: str, video: dict[str, Any]) -> str:
    source_output = Path(str(video["render"]["output_path"]))
    token = _artifact_token(video)
    return str(
        Path(handoff_dir)
        / "final_upscaled"
        / f"{token}_{source_output.stem}_wavespeed_1080p.mp4"
    )


def _apply_final_upscale_to_inventory(
    *,
    video: dict[str, Any],
    handoff_dir: str,
    inventory: dict[str, Any],
    policy: FinalUpscalePolicy,
    upscaler_factory: Callable[[FinalUpscalePolicy], Any | None],
    verifier: Callable[..., dict[str, Any]],
) -> bool:
    video["final_upscale"] = {
        "required": policy.required,
        "enabled": policy.enabled,
        "provider": policy.provider,
        "reason": policy.reason,
        "status": "not_required" if not policy.required and not policy.enabled else "pending",
    }
    if not policy.required and not policy.enabled:
        return True
    if not policy.enabled:
        # Whole-value reassignment (not key insertion) keeps concurrent
        # receipt serialization safe under tail pipelining.
        video["final_upscale"] = {
            **video["final_upscale"],
            "status": "failed",
            "error": "final upscale is required but disabled",
        }
        video["state"] = "final_upscale_failed"
        video["error"] = "final upscale is required but disabled"
        return False

    items = inventory.get("items") or []
    if len(items) != 1:
        video["final_upscale"] = {
            **video["final_upscale"],
            "status": "failed",
            "error": "expected exactly one staging item",
        }
        video["state"] = "final_upscale_failed"
        video["error"] = "final upscale expected exactly one staging item"
        return False
    item = items[0]
    input_path = str(item["local_output_path"])
    output_path = _upscaled_output_path(handoff_dir=handoff_dir, video=video)

    try:
        # The pre-upscale master's duration anchors output verification —
        # an upscale must not change content length (review hardening
        # 2026-06-10: dimensions alone can pass on a truncated file).
        try:
            expected_duration = float(
                probe_video_properties(Path(input_path))["duration_sec"]
            ) or None
        except (FinalUpscaleError, OSError):
            expected_duration = None
        # Idempotency (2026-06-10, resume support): a previous attempt may
        # have produced a verified upscale before a later sub-step failed.
        # Reuse it instead of paying for a fresh WaveSpeed prediction.
        result: dict[str, Any]
        existing = (
            verifier(
                output_path=output_path,
                policy=policy,
                expected_duration_sec=expected_duration,
            )
            if Path(output_path).exists()
            else {"verified": False}
        )
        if existing.get("verified"):
            logger.info(
                "Final upscale output already verified — reusing %s", output_path,
            )
            result = {"status": "reused_existing", "output_path": output_path}
            verified = existing
        else:
            upscaler = upscaler_factory(policy)
            if upscaler is None:
                raise RuntimeError("final upscale provider is not configured")
            result = _retry_autopilot_step(
                lambda: upscaler.upscale(input_path=input_path, output_path=output_path),
                "final video upscale",
            )
            verified = verifier(
                output_path=output_path,
                policy=policy,
                expected_duration_sec=expected_duration,
            )
        if not verified.get("verified"):
            raise RuntimeError(
                "final upscale verification failed: "
                + str(verified.get("reason") or verified)
            )
    except Exception as exc:
        video["final_upscale"] = {
            **video["final_upscale"],
            "status": "failed",
            "input_path": input_path,
            "output_path": output_path,
            "error": str(exc),
        }
        video["state"] = "final_upscale_failed"
        video["error"] = f"final upscale failed: {exc}"
        return False

    item["pre_upscale_local_output_path"] = input_path
    item["local_output_path"] = output_path
    item["final_upscale"] = {
        "status": "applied",
        "policy": policy.to_dict(),
        "result": result,
        "verification": verified,
    }
    video["final_upscale"] = {
        **video["final_upscale"],
        "status": "verified",
        "input_path": input_path,
        "output_path": output_path,
        "result": result,
        "verification": verified,
    }
    return True


def _autopilot_preflight(
    receipt: dict[str, Any],
    receipt_path: str,
    *,
    final_upscale_policy: FinalUpscalePolicy,
    drive_upload_target_factory: Callable[[], DriveUploadTarget],
    supabase_client_factory: Callable[[], Any],
    final_upscaler_factory: Callable[[FinalUpscalePolicy], Any | None],
) -> tuple[DriveUploadTarget | None, Any | None, bool]:
    """Validate every autopilot dependency BEFORE rendering anything.

    2026-06-09 preflight fix: these used to be constructed lazily after
    the first successful render — a bad credential or missing
    PGC_WAVESPEED_UPSCALE_COMMAND was discovered only once hours of
    render + LLM/TTS spend were already sunk (or crashed the batch
    process mid-run). Returns ``(drive_target, supabase_client, ok)``;
    the result is recorded under ``receipt["preflight"]``.
    """
    preflight_errors: list[str] = []
    drive_target: DriveUploadTarget | None = None
    supabase_client: Any | None = None
    try:
        drive_target = drive_upload_target_factory()
    except Exception as exc:
        preflight_errors.append(f"drive upload target: {exc}")
    try:
        supabase_client = supabase_client_factory()
    except Exception as exc:
        preflight_errors.append(f"supabase client: {exc}")
    if final_upscale_policy.enabled:
        try:
            upscaler = final_upscaler_factory(final_upscale_policy)
            if upscaler is None:
                preflight_errors.append(
                    "final upscale is enabled but no upscaler is configured "
                    "(set PGC_WAVESPEED_UPSCALE_COMMAND)"
                )
            else:
                # Deep config check (2026-06-10 review fix): constructing the
                # wrapper only proves the command string exists. When the
                # upscaler supports --preflight, run it so a missing
                # WAVESPEED_API_KEY or Supabase storage credential (even one
                # inside the command's own --env file) fails the batch here.
                preflight_fn = getattr(upscaler, "preflight", None)
                if callable(preflight_fn):
                    preflight_fn()
        except Exception as exc:
            preflight_errors.append(f"final upscaler preflight: {exc}")
    receipt["preflight"] = {
        "status": "failed" if preflight_errors else "passed",
        "checked_at": utc_now_iso(),
        "errors": preflight_errors,
    }
    write_run_receipt(receipt_path, receipt)
    if preflight_errors:
        for error in preflight_errors:
            logger.error("Autopilot preflight failed: %s", error)
        return drive_target, supabase_client, False
    return drive_target, supabase_client, True


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
    final_upscale_policy: FinalUpscalePolicy | None = None,
    final_upscaler_factory: Callable[[FinalUpscalePolicy], Any | None] = create_final_video_upscaler_from_env,
    final_upscale_verifier: Callable[..., dict[str, Any]] = verify_final_upscale_output,
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

    # 2026-06-09 fix: flush the receipt after EVERY sub-step (not only when
    # the whole chain returns) so a mid-chain crash leaves on-disk state
    # that matches what actually happened — the receipt is the recovery
    # source of truth. Stage timings land in video["timings"].
    try:
        inventory = build_staging_inventory([manifest_path], require_source_exists=True)
        policy = final_upscale_policy or normalize_final_upscale_policy(None)
        mark_stage_started(video, "final_upscale")
        upscale_ok = _apply_final_upscale_to_inventory(
            video=video,
            handoff_dir=handoff_dir,
            inventory=inventory,
            policy=policy,
            upscaler_factory=final_upscaler_factory,
            verifier=final_upscale_verifier,
        )
        mark_stage_finished(video, "final_upscale")
        write_run_receipt(receipt_path, receipt)
        if not upscale_ok:
            return False
        mark_stage_started(video, "drive_upload")
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
            mark_stage_finished(video, "drive_upload")
            write_run_receipt(receipt_path, receipt)
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
        mark_stage_finished(video, "drive_upload")
        write_run_receipt(receipt_path, receipt)
        return False
    mark_stage_finished(video, "drive_upload")
    write_run_receipt(receipt_path, receipt)

    mark_stage_started(video, "usage_writeback")
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
            mark_stage_finished(video, "usage_writeback")
            write_run_receipt(receipt_path, receipt)
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
        mark_stage_finished(video, "usage_writeback")
        write_run_receipt(receipt_path, receipt)
        return False
    mark_stage_finished(video, "usage_writeback")
    write_run_receipt(receipt_path, receipt)

    mark_stage_started(video, "release_candidate")
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
            mark_stage_finished(video, "release_candidate")
            write_run_receipt(receipt_path, receipt)
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
        mark_stage_finished(video, "release_candidate")
        write_run_receipt(receipt_path, receipt)
        return False
    mark_stage_finished(video, "release_candidate")

    video["state"] = "complete"
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
    source_resolution_policy: dict[str, Any] | None = None,
    final_upscale_policy: dict[str, Any] | None = None,
    final_upscaler_factory: Callable[[FinalUpscalePolicy], Any | None] = create_final_video_upscaler_from_env,
    final_upscale_verifier: Callable[..., dict[str, Any]] = verify_final_upscale_output,
    tail_workers: int = 1,
) -> int:
    if jobs != 1:
        raise ValueError("run_batch currently supports --jobs 1 only")
    if tail_workers < 0:
        raise ValueError("tail_workers must be >= 0 (0 = serial tail)")
    _reject_sticky_replay_env()

    spec = load_batch_spec(batch_path)
    selection_metadata = spec.get("selection") if isinstance(spec.get("selection"), dict) else None
    resolved_source_policy = normalize_source_resolution_policy(
        source_resolution_policy or spec.get("source_resolution_policy")
    )
    resolved_final_upscale_policy = normalize_final_upscale_policy(
        final_upscale_policy or spec.get("final_upscale_policy"),
        source_policy_mode=resolved_source_policy.mode,
    )
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
        # Batch seed doubles as the rotation seed so a seeded batch is
        # reproducible; unseeded batches still vary track order per run.
        music_shuffle_seed = seed if seed is not None else int.from_bytes(os.urandom(4), "big")
        music_ids = music_id_resolver(
            target_duration_sec=resolved_target_duration,
            count=resolved_videos_per_poi,
            shuffle_seed=music_shuffle_seed,
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
            source_resolution_policy=resolved_source_policy.to_dict(),
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
        source_resolution_policy=resolved_source_policy.to_dict(),
        final_upscale_policy=resolved_final_upscale_policy.to_dict(),
    )
    write_run_receipt(resolved_receipt_path, receipt)

    failures = 0
    drive_target: DriveUploadTarget | None = None
    supabase_client: Any | None = None
    resolved_handoff_dir = handoff_dir or os.path.join(output_root, "handoff")

    if production_autopilot:
        drive_target, supabase_client, preflight_ok = _autopilot_preflight(
            receipt,
            resolved_receipt_path,
            final_upscale_policy=resolved_final_upscale_policy,
            drive_upload_target_factory=drive_upload_target_factory,
            supabase_client_factory=supabase_client_factory,
            final_upscaler_factory=final_upscaler_factory,
        )
        if not preflight_ok:
            return 1

    # Tail pipelining (2026-06-10): the autopilot tail (upscale ~700s, mostly
    # waiting on WaveSpeed's servers) is LONGER than a render (~450s of local
    # CPU). Overlapping render N+1 with tail N drops the per-video cadence
    # from render+tail to max(render, tail); a second tail worker drops it
    # to ~render. tail_workers=0 restores the strictly serial behavior.
    pipelined = production_autopilot and tail_workers > 0
    executor: ThreadPoolExecutor | None = None
    in_flight: dict[Future, str] = {}
    tail_locals = threading.local()

    def _tail_clients() -> tuple[DriveUploadTarget, Any]:
        # One client pair per worker thread: googleapiclient/httplib2 is not
        # thread-safe, so workers never share the preflight-validated pair
        # (which only proves the credentials work).
        if not hasattr(tail_locals, "drive_target"):
            tail_locals.drive_target = drive_upload_target_factory()
            tail_locals.supabase_client = supabase_client_factory()
        return tail_locals.drive_target, tail_locals.supabase_client

    def _run_tail(video: dict[str, Any]) -> bool:
        tail_drive_target, tail_supabase_client = _tail_clients()
        ok = _run_video_production_autopilot(
            video=video,
            receipt=receipt,
            receipt_path=resolved_receipt_path,
            handoff_dir=resolved_handoff_dir,
            drive_target=tail_drive_target,
            supabase_client=tail_supabase_client,
            usage_recorder=usage_recorder,
            usage_verifier=usage_verifier,
            release_registrar=release_registrar,
            final_upscale_policy=resolved_final_upscale_policy,
            final_upscaler_factory=final_upscaler_factory,
            final_upscale_verifier=final_upscale_verifier,
        )
        write_run_receipt(resolved_receipt_path, receipt)
        return ok

    def _harvest(futures: Sequence[Future]) -> None:
        nonlocal failures
        for future in futures:
            poi_key = in_flight.pop(future)
            try:
                ok = future.result()
            except Exception:
                logger.exception("Autopilot tail crashed: poi_key=%s", poi_key)
                ok = False
            if not ok:
                failures += 1

    def _harvest_done() -> None:
        _harvest([future for future in list(in_flight) if future.done()])

    def _wait_for_poi_tail(poi_key: str) -> None:
        # Same-POI usage events must stay ordered: never start rendering a
        # POI's next video while that POI's tail is still in flight, and
        # only check quarantine after the tail has settled.
        pending = [f for f, key in in_flight.items() if key == poi_key]
        if pending:
            wait(pending)
        _harvest_done()

    def _wait_for_tail_slot() -> None:
        while len(in_flight) >= tail_workers:
            wait(list(in_flight), return_when=FIRST_COMPLETED)
            _harvest_done()

    if pipelined:
        executor = ThreadPoolExecutor(
            max_workers=tail_workers, thread_name_prefix="autopilot-tail",
        )
    try:
        for item, command, video in zip(items, item_commands, videos, strict=True):
            if pipelined:
                _wait_for_poi_tail(_item_poi_key(item))
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
                if pipelined:
                    assert executor is not None
                    _wait_for_tail_slot()
                    future = executor.submit(_run_tail, video)
                    in_flight[future] = _item_poi_key(item)
                else:
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
                        final_upscale_policy=resolved_final_upscale_policy,
                        final_upscaler_factory=final_upscaler_factory,
                        final_upscale_verifier=final_upscale_verifier,
                    )
                    write_run_receipt(resolved_receipt_path, receipt)
                    if not ok:
                        failures += 1
    finally:
        if executor is not None:
            # Drain so a crash/interrupt never abandons a paid-for upscale
            # mid-flight; the receipt stays the recovery source of truth.
            if in_flight:
                logger.info("Draining %d in-flight autopilot tail(s)", len(in_flight))
            _harvest(list(in_flight))
            executor.shutdown(wait=True)

    return 1 if failures else 0


def _rebuild_item_from_video(video: dict[str, Any]) -> BatchItem:
    """Reconstruct a BatchItem from a receipt video record (resume path).

    ``location`` is not persisted per-video; it is only used for prompt
    grounding inside compile_promo, whose full command is replayed
    verbatim from the record, so an empty value here is harmless.
    """
    render = video.get("render") or {}
    return BatchItem(
        poi=BatchPoi(
            name=str(video.get("poi_name") or ""),
            location="",
            poi_id=video.get("poi_id"),
            canonical_key=video.get("canonical_key"),
        ),
        video_index=int(video.get("video_index") or 0),
        output_dir=str(render.get("output_dir") or ""),
        output_path=str(render.get("output_path") or ""),
        voice_key=str(video.get("voice_key") or ""),
        music_id=video.get("music_id"),
        seed=video.get("seed"),
    )


def resume_batch(
    *,
    receipt_path: str,
    handoff_dir: str | None = None,
    command_runner: Callable[[Sequence[str]], int] = run_compile_command,
    drive_upload_target_factory: Callable[[], DriveUploadTarget] = _create_drive_upload_target_from_env,
    supabase_client_factory: Callable[[], Any] = _create_supabase_client_from_env,
    usage_recorder: Callable[[Any, list[dict[str, Any]]], dict[str, int]] = record_usage_events,
    usage_verifier: Callable[[Any, list[dict[str, Any]]], dict[str, Any]] = verify_usage_events,
    release_registrar: Callable[[Any, list[dict[str, Any]]], dict[str, Any]] = register_release_candidates,
    final_upscaler_factory: Callable[[FinalUpscalePolicy], Any | None] = create_final_video_upscaler_from_env,
    final_upscale_verifier: Callable[..., dict[str, Any]] = verify_final_upscale_output,
) -> int:
    """Resume an interrupted/partially-failed batch from its RUN_RECEIPT.

    Closes the ``receipt_based_resume_top_up`` gap (2026-06-10): per-video
    state decides the cheapest safe recovery — ``complete`` videos are
    skipped, tail-failure states re-run ONLY the autopilot tail against
    the ORIGINAL manifest (deterministic usage event ids + Drive
    name/size reuse + RC missing-key inserts + verified-upscale reuse
    make the tail idempotent, and keeping the manifest_id prevents
    double-spending the usage ledger), and everything else re-renders by
    replaying the recorded compile command. Quarantined POIs get one
    fresh chance per resume; the cleared list is archived under
    ``resume_history``.
    """
    _reject_sticky_replay_env()
    receipt_file = Path(receipt_path)
    try:
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read receipt {receipt_path}: {exc}") from exc
    if receipt.get("receipt_kind") != "pgc_batch_run_receipt":
        raise ValueError(f"not a PGC batch run receipt: {receipt_path}")
    request = receipt.get("request") or {}
    production_autopilot = str(request.get("mode") or "") == "production_autopilot"
    # Derive defaults exactly like the original run did: a receipt whose
    # source policy is transition_low_res_only but which lacks an explicit
    # final_upscale_policy must still REQUIRE upscale on resume (review
    # fix 2026-06-10 — older/partial receipts would otherwise resume
    # without the mandatory upscale gate).
    resolved_source_policy = normalize_source_resolution_policy(
        (request.get("filters") or {}).get("source_resolution_policy"),
    )
    resolved_final_upscale_policy = normalize_final_upscale_policy(
        (request.get("filters") or {}).get("final_upscale_policy"),
        source_policy_mode=resolved_source_policy.mode,
    )
    output_root = str(request.get("output_root") or receipt_file.parent)
    resolved_handoff_dir = handoff_dir or os.path.join(output_root, "handoff")

    videos = receipt.get("videos") or []
    plan = [
        (video, plan_resume_action(video, production_autopilot=production_autopilot))
        for video in videos
    ]
    plan_counts = {
        action: sum(1 for _, planned in plan if planned == action)
        for action in ("skip", "tail", "render")
    }
    logger.info(
        "Resume plan for %s: %d skip / %d tail-only / %d re-render",
        receipt.get("batch_id"),
        plan_counts["skip"], plan_counts["tail"], plan_counts["render"],
    )

    # Quarantined POIs get a fresh chance on resume (the underlying outage
    # may be fixed); a repeat usage failure re-quarantines within this run.
    history_entry: dict[str, Any] = {
        "resumed_at": utc_now_iso(),
        "plan": plan_counts,
    }
    if receipt.get("quarantined_pois"):
        history_entry["cleared_quarantined_pois"] = receipt["quarantined_pois"]
        receipt["quarantined_pois"] = []
    receipt.setdefault("resume_history", []).append(history_entry)
    write_run_receipt(receipt_path, receipt)

    drive_target: DriveUploadTarget | None = None
    supabase_client: Any | None = None
    if production_autopilot and (plan_counts["tail"] or plan_counts["render"]):
        drive_target, supabase_client, preflight_ok = _autopilot_preflight(
            receipt,
            receipt_path,
            final_upscale_policy=resolved_final_upscale_policy,
            drive_upload_target_factory=drive_upload_target_factory,
            supabase_client_factory=supabase_client_factory,
            final_upscaler_factory=final_upscaler_factory,
        )
        if not preflight_ok:
            return 1

    failures = 0
    for video, action in plan:
        if action == "skip":
            continue
        item = _rebuild_item_from_video(video)
        if production_autopilot and _is_poi_quarantined(receipt, item):
            _mark_skipped_quarantined(video)
            failures += 1
            write_run_receipt(receipt_path, receipt)
            continue
        if action == "render":
            reset_video_record_for_rerender(video)
            os.makedirs(item.output_dir, exist_ok=True)
            logger.info(
                "Resume re-render: poi=%s video=%d",
                item.poi.name, item.video_index,
            )
            mark_rendering(video)
            write_run_receipt(receipt_path, receipt)
            return_code = command_runner(video["render"]["command"])
            mark_render_result(video, return_code=return_code)
            write_run_receipt(receipt_path, receipt)
            if return_code != 0:
                failures += 1
                logger.error(
                    "Resume re-render failed: poi=%s video=%d exit_code=%d",
                    item.poi.name, item.video_index, return_code,
                )
                continue
            if (video.get("manifest_audit") or {}).get("status") in {"failed", "error"}:
                failures += 1
                logger.error(
                    "Resume manifest audit failed: poi=%s video=%d",
                    item.poi.name, item.video_index,
                )
                continue
        else:
            logger.info(
                "Resume tail-only: poi=%s video=%d from state=%s",
                item.poi.name, item.video_index, video.get("state"),
            )
        if not production_autopilot:
            continue
        ok = _run_video_production_autopilot(
            video=video,
            receipt=receipt,
            receipt_path=receipt_path,
            handoff_dir=resolved_handoff_dir,
            drive_target=drive_target,
            supabase_client=supabase_client,
            usage_recorder=usage_recorder,
            usage_verifier=usage_verifier,
            release_registrar=release_registrar,
            final_upscale_policy=resolved_final_upscale_policy,
            final_upscaler_factory=final_upscaler_factory,
            final_upscale_verifier=final_upscale_verifier,
        )
        write_run_receipt(receipt_path, receipt)
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
    source_resolution_policy: dict[str, Any] | None = None,
    final_upscale_policy: dict[str, Any] | None = None,
    final_upscaler_factory: Callable[[FinalUpscalePolicy], Any | None] = create_final_video_upscaler_from_env,
    final_upscale_verifier: Callable[..., dict[str, Any]] = verify_final_upscale_output,
    tail_workers: int = 1,
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
        source_resolution_policy=source_resolution_policy,
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
        source_resolution_policy=source_resolution_policy,
        final_upscale_policy=final_upscale_policy,
        final_upscaler_factory=final_upscaler_factory,
        final_upscale_verifier=final_upscale_verifier,
        tail_workers=tail_workers,
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
    source.add_argument(
        "--resume",
        metavar="RECEIPT_PATH",
        help=(
            "Resume an interrupted/partially-failed batch from its "
            "RUN_RECEIPT.json: completed videos are skipped, tail failures "
            "(upscale/Drive/usage/release) continue from the failed step "
            "without re-rendering, everything else re-renders."
        ),
    )
    parser.add_argument(
        "--output-dir",
        required=False,
        help="Root directory for batch outputs (not needed with --resume).",
    )
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
        "--source-resolution-policy-mode",
        choices=["best_available", "transition_low_res_only", "width_band"],
        default="best_available",
        help="Shared asset source-width policy. Default uses all eligible assets.",
    )
    parser.add_argument(
        "--source-target-width",
        type=int,
        default=720,
        help="Target source width for width-band policies.",
    )
    parser.add_argument(
        "--source-width-tolerance-px",
        type=int,
        default=40,
        help="Allowed +/- width tolerance for width-band policies.",
    )
    parser.add_argument(
        "--source-aspect-ratio-min",
        type=float,
        default=1.70,
        help="Minimum height/width sanity ratio for vertical source assets.",
    )
    parser.add_argument(
        "--source-aspect-ratio-max",
        type=float,
        default=1.86,
        help="Maximum height/width sanity ratio for vertical source assets.",
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
    parser.add_argument(
        "--tail-workers",
        type=int,
        default=1,
        help=(
            "Concurrent autopilot tails (upscale/Drive/usage/release) overlapped "
            "with rendering. 1 pipelines tail N under render N+1; 2 keeps up with "
            "rendering while upscale (~700s) exceeds render (~450s); 0 = serial."
        ),
    )
    parser.add_argument(
        "--serial-tail",
        action="store_true",
        help=(
            "Disable tail pipelining (same as --tail-workers 0): strictly serial "
            "render→tail per video. Per-video voice/music/seed are identical "
            "either way; batch items always execute in POI-round-robin order."
        ),
    )
    parser.add_argument(
        "--final-upscale-provider",
        choices=["disabled", "wavespeed"],
        default=None,
        help=(
            "Optional final-video upscale provider. Omit to derive from source "
            "resolution policy."
        ),
    )
    parser.add_argument(
        "--final-upscale-required",
        action="store_true",
        help="Require final-video upscale before Drive upload and release handoff.",
    )
    return parser


def main() -> None:
    from promo.core.logging_config import configure_logging

    configure_logging()
    parser = _build_parser()
    args = parser.parse_args()
    try:
        if args.resume:
            sys.exit(resume_batch(
                receipt_path=args.resume,
                handoff_dir=args.handoff_dir,
            ))
        if not args.output_dir:
            parser.error("--output-dir is required (except with --resume)")
        voices = parse_voice_keys(args.voices) if args.voices is not None else None
        source_resolution_policy = None
        if args.source_resolution_policy_mode != "best_available":
            source_resolution_policy = {
                "mode": args.source_resolution_policy_mode,
                "target_width": args.source_target_width,
                "tolerance_px": args.source_width_tolerance_px,
                "aspect_ratio_min": args.source_aspect_ratio_min,
                "aspect_ratio_max": args.source_aspect_ratio_max,
            }
        final_upscale_policy = None
        if args.final_upscale_provider is not None or args.final_upscale_required:
            provider = args.final_upscale_provider or "wavespeed"
            final_upscale_policy = {
                "required": bool(args.final_upscale_required),
                "enabled": provider != "disabled",
                "provider": provider,
            }
        tail_workers = 0 if args.serial_tail else args.tail_workers
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
                source_resolution_policy=source_resolution_policy,
                final_upscale_policy=final_upscale_policy,
                tail_workers=tail_workers,
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
                source_resolution_policy=source_resolution_policy,
                final_upscale_policy=final_upscale_policy,
                tail_workers=tail_workers,
            )
    except (BatchSelectionError, ValueError) as exc:
        parser.error(str(exc))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
