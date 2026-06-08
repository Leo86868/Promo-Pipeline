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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

from promo.core import config
from promo.core import sanitize_poi_name as _safe_poi_dir
from promo.core.music_library import SupabaseMusicLibrary
from promo.core.run_receipt import (
    build_run_receipt,
    build_video_record,
    mark_render_result,
    mark_rendering,
    write_run_receipt,
)

load_dotenv()

logger = logging.getLogger(__name__)


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
) -> int:
    if jobs != 1:
        raise ValueError("run_batch currently supports --jobs 1 only")

    spec = load_batch_spec(batch_path)
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
    )
    write_run_receipt(resolved_receipt_path, receipt)

    failures = 0
    for item, command, video in zip(items, item_commands, videos, strict=True):
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

    return 1 if failures else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one-video PGC jobs from a batch JSON")
    parser.add_argument("--batch", required=True, help="Path to batch JSON")
    parser.add_argument("--output-dir", required=True, help="Root directory for batch outputs")
    parser.add_argument("--videos-per-poi", type=int, default=None)
    parser.add_argument("--target-duration-sec", type=float, default=None)
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
    return parser


def main() -> None:
    from promo.core.logging_config import configure_logging

    configure_logging()
    parser = _build_parser()
    args = parser.parse_args()
    try:
        exit_code = run_batch(
            batch_path=args.batch,
            output_root=args.output_dir,
            videos_per_poi=args.videos_per_poi,
            target_duration_sec=args.target_duration_sec,
            voices=parse_voice_keys(args.voices) if args.voices is not None else None,
            use_music_library=args.supabase_music_library,
            script_candidates=args.script_candidates,
            tts_speed=args.tts_speed,
            seed=args.seed,
            jobs=args.jobs,
            receipt_path=args.receipt_path,
        )
    except ValueError as exc:
        parser.error(str(exc))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
