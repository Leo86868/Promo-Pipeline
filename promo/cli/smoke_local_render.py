#!/usr/bin/env python3
"""Minimal standalone smoke path for local clips -> promo render output."""
# user-facing CLI

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from promo.core import sanitize_poi_name
from promo.core.backend import LocalBackend
from promo.core.render.remotion_renderer import (
    REMOTION_DIR,
    get_clip_duration,
    build_props,
    render_promo,
    stage_media,
    validate_props,
)

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "local_render_smoke.json"


def load_smoke_fixture(fixture_path: Path = FIXTURE_PATH) -> dict:
    with fixture_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _bundled_media_path(filename: str) -> str:
    return str(Path(REMOTION_DIR) / "public" / filename)


def _select_clip_items(clip_paths: dict[str, str], required_count: int) -> list[tuple[str, str]]:
    items = sorted(clip_paths.items())
    if len(items) < required_count:
        raise ValueError(
            f"Need at least {required_count} local clips with 4-digit IDs; found {len(items)}"
        )
    return items[:required_count]


def prepare_local_smoke_run(
    clips_dir: str,
    poi_name: str,
    fixture_path: Path = FIXTURE_PATH,
) -> tuple[str, dict, list[str], str, str, list[str]]:
    fixture = load_smoke_fixture(fixture_path)
    tmp_dir = tempfile.mkdtemp(prefix="promo_smoke_")
    backend = LocalBackend(clips_dir=clips_dir)
    clip_paths = backend.fetch_clips(poi_name, tmp_dir)
    selected = _select_clip_items(clip_paths, len(fixture["clip_cues"]))

    clip_assignments = []
    selected_ids: list[str] = []
    selected_paths: list[str] = []
    for cue, (clip_id, clip_path) in zip(fixture["clip_cues"], selected):
        duration = get_clip_duration(clip_path)
        if duration <= 0.1:
            raise ValueError(f"Clip {clip_id} looks unreadable: {clip_path}")

        clip_assignments.append(
            {
                "path": clip_path,
                "clip_id": clip_id,
                "trim_start": 0.0,
                "trim_end": round(min(duration, float(cue["target_duration"])), 3),
                "video_start": float(cue["video_start"]),
                "narration": cue["narration"],
            }
        )
        selected_ids.append(clip_id)
        selected_paths.append(clip_path)

    narration_path = _bundled_media_path(fixture["audio"]["narration"])
    bgm_path = _bundled_media_path(fixture["audio"]["bgm"])

    # Sprint 10 C5: build_props now reads segment text from script_segments
    # (the retired _reconstruct_segment_text helper used to derive it from
    # word_timestamps). The smoke fixture does not carry segment.text
    # directly — cue narrations are grouped by their video_start falling
    # inside each segment_timestamp window. AC2 (Sprint 13) tightened the
    # upper bound to `< seg_end` so a cue whose video_start matches the
    # next segment's start is not double-counted across segments. Sprint 13
    # post-audit L-002: emit a WARNING if any cue falls outside ALL
    # segments — guards against silent cue-narration drop when a future
    # fixture has an inter-segment gap > 0.5s.
    script_segments = []
    placed_cue_ids: set[int] = set()
    for st in fixture["segment_timestamps"]:
        seg_num = int(st["segment"])
        seg_start = float(st["start"])
        seg_end = float(st["end"])
        seg_cues: list[str] = []
        for idx, cue in enumerate(fixture["clip_cues"]):
            if seg_start - 0.5 <= float(cue["video_start"]) < seg_end:
                seg_cues.append(cue["narration"])
                placed_cue_ids.add(idx)
        script_segments.append({"segment": seg_num, "text": " ".join(seg_cues).strip()})
    unplaced = [
        (i, c) for i, c in enumerate(fixture["clip_cues"]) if i not in placed_cue_ids
    ]
    if unplaced:
        logger.warning(
            "Smoke fixture cue(s) outside every segment window — dropped from "
            "script_segments text: %s. Check fixture's inter-segment gaps "
            "against the ±0.5s membership tolerance.",
            [(i, c["video_start"], c["narration"][:40]) for i, c in unplaced],
        )

    props = build_props(
        poi_name=poi_name,
        location=fixture["location"],
        clip_assignments=clip_assignments,
        word_timestamps=fixture["word_timestamps"],
        segment_timestamps=fixture["segment_timestamps"],
        narration_path=narration_path,
        bgm_path=bgm_path,
        script_segments=script_segments,
    )

    return tmp_dir, props, selected_paths, narration_path, bgm_path, selected_ids


def _default_output_path(poi_name: str) -> str:
    return os.path.abspath(
        os.path.join(REMOTION_DIR, "out", f"{sanitize_poi_name(poi_name)}_smoke.mp4")
    )


def _default_props_preview_path(output_path: str) -> str:
    root, _ = os.path.splitext(output_path)
    return f"{root}_props.json"


def main() -> None:
    from promo.core.logging_config import configure_logging
    configure_logging()

    parser = argparse.ArgumentParser(description="Standalone local-clips smoke render")
    parser.add_argument("--local-clips", required=True, help="Directory containing local .mp4 clips")
    parser.add_argument("--poi", default="Local Smoke", help="POI label for staging and output naming")
    parser.add_argument("--output", default=None, help="Output MP4 path")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Stage media and validate props without invoking Remotion",
    )
    args = parser.parse_args()

    output_path = os.path.abspath(args.output) if args.output else _default_output_path(args.poi)

    tmp_dir = None
    staged_dir = None
    try:
        tmp_dir, props, selected_paths, narration_path, bgm_path, selected_ids = prepare_local_smoke_run(
            clips_dir=args.local_clips,
            poi_name=args.poi,
        )

        stage_media(
            clip_paths=selected_paths,
            narration_path=narration_path,
            bgm_path=bgm_path,
            poi_name=args.poi,
        )
        staged_dir = os.path.join(Path(REMOTION_DIR).resolve(), "public", sanitize_poi_name(args.poi))

        errors = validate_props(props)
        if errors:
            raise RuntimeError("Smoke props failed validation: " + "; ".join(errors))

        preview_path = _default_props_preview_path(output_path)
        os.makedirs(os.path.dirname(os.path.abspath(preview_path)), exist_ok=True)
        with open(preview_path, "w", encoding="utf-8") as f:
            json.dump(props, f, indent=2, ensure_ascii=False)

        logger.info("Smoke clip IDs: %s", ", ".join(selected_ids))
        logger.info("Smoke props preview: %s", preview_path)

        if args.dry_run:
            logger.info("Dry run complete. Re-run without --dry-run to render %s", output_path)
            return

        ok = render_promo(props, output_path)
        if not ok:
            raise SystemExit(1)

        logger.info("Smoke render complete: %s", output_path)

    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if staged_dir and os.path.isdir(staged_dir):
            shutil.rmtree(staged_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
