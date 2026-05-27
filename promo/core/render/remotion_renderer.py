"""Remotion renderer for promotional hotel videos.

Self-contained bridge from Python pipeline to Remotion rendering:
  1. Binds clips to narration via backend-agnostic word timestamps
     (ElevenLabs native alignment OR MMS_FA via forced_aligner on the
     Gemini TTS path — see tts_engine.py for the dispatch seam)
  2. Builds props.json for Remotion
  3. Validates before render
  4. Invokes `npx remotion render`

``ffmpeg`` is a required system dependency (used by tts_engine for the
Gemini PCM→MP3 conversion and silence stitching; renderer itself shells
out only to Remotion).

Usage:
    from promo.core.render.remotion_renderer import build_props_from_script, render_promo

    props = build_props_from_script(
        poi_name="Ventana Big Sur", location="Big Sur, California",
        script_segments=script["segments"], clip_paths=clip_paths,
        narration_result=narration, bgm_path="bgm.mp3",
    )
    render_promo(props, "output.mp4")
"""

import json
import logging
import os
import shutil
import subprocess
import uuid
from typing import Any

from promo.core.assign.clip_assigner import HARD_CONSTRAINT_TOL_SEC
from promo.core.schema import (
    ClipAssignment,
    Narration,
    ScriptSegment,
    SegmentTimestamp,
    WordTimestamp,
)
from promo.core.errors import FreezeWouldOccurError

logger = logging.getLogger(__name__)

REMOTION_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "remotion")
PUBLIC_DIR = os.path.join(REMOTION_DIR, "public")

# Rendering defaults
DEFAULT_FPS = 30
DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
DEFAULT_BGM_VOLUME = 0.35
DEFAULT_BGM_DUCKED_VOLUME = 0.08
DEFAULT_DUCK_RAMP_SEC = 0.3
DEFAULT_CAPTION_FONT_SIZE = 48
DEFAULT_HIGHLIGHT_COLOR = "#D4AF37"
DEFAULT_TEXT_COLOR = "#FFFFFF"


# ---------------------------------------------------------------------------
#  Props builder
# ---------------------------------------------------------------------------

def build_props(
    poi_name: str,
    location: str,
    clip_assignments: list[dict[str, Any]],
    word_timestamps: list[WordTimestamp],
    segment_timestamps: list[SegmentTimestamp],
    narration_path: str,
    bgm_path: str,
    fps: int = DEFAULT_FPS,
    bgm_volume: float = DEFAULT_BGM_VOLUME,
    bgm_ducked_volume: float = DEFAULT_BGM_DUCKED_VOLUME,
    script_segments: list[ScriptSegment] | None = None,
) -> dict[str, object]:
    """Build a props.json dict from Python pipeline data.

    Args:
        poi_name: Hotel name.
        location: City, State/Country.
        clip_assignments: From _bind_clips_to_narration(). Each is a
            renderer-ready entry:
            {path, clip_id, trim_start, trim_end, video_start, narration,
             source_duration}.
        word_timestamps: From tts_engine [{word, start, end}].
        segment_timestamps: From tts_engine [{segment, start, end, duration}].
        narration_path: Absolute path to narration WAV.
        bgm_path: Absolute path to BGM MP3.
        fps: Video FPS.
        bgm_volume: BGM volume during gaps.
        bgm_ducked_volume: BGM volume during narration.
        script_segments: Optional source-of-truth list from
            ``script["segments"]`` used to populate ``props["segments"][i].text``
            by segment-number lookup. When ``None`` the text field is left
            empty — the Sprint-10 path always provides this.

    Returns:
        Props dict matching the Remotion HotelPromo schema.
    """
    # AC8: derive POI-specific subdirectory prefix for file paths
    file_prefix = f"{_safe_poi_dir(poi_name)}/" if poi_name else ""

    clips = []
    for i, ca in enumerate(clip_assignments):
        video_start = ca["video_start"]
        # Every renderer entry produced by _bind_clips_to_narration carries
        # trim_start + trim_end; asserting here catches any future caller
        # that synthesizes an assignment dict by hand and forgets the
        # fields, instead of silently rendering a bogus Magic-5.04 default.
        trim_start = float(ca["trim_start"])
        trim_end = float(ca["trim_end"])
        if i + 1 < len(clip_assignments):
            video_end = clip_assignments[i + 1]["video_start"]
        else:
            video_end = round(video_start + (trim_end - trim_start), 3)

        clips.append({
            "clipId": str(ca["clip_id"]).zfill(4),
            "file": f"{file_prefix}{os.path.basename(ca['path'])}",
            "narration": ca.get("narration", ""),
            "videoStart": round(video_start, 3),
            "videoEnd": round(video_end, 3),
            "trimStart": round(trim_start, 3),
            "trimEnd": round(trim_end, 3),
        })

    # Sprint 10 C5: segment text is sourced directly from script_segments
    # (the Gemini #1 output), not reconstructed from word_timestamps —
    # the pre-Sprint-10 text-reconstruction helper retired with the rewire.
    text_by_seg: dict[int, str] = {}
    if script_segments:
        for s in script_segments:
            try:
                text_by_seg[int(s.get("segment"))] = str(s.get("text", ""))
            except (TypeError, ValueError):
                continue

    segments = []
    for st in segment_timestamps:
        seg_num = st["segment"]
        segments.append({
            "segment": seg_num,
            "text": text_by_seg.get(int(seg_num), ""),
            "startSec": round(st["start"], 3),
            "endSec": round(st["end"], 3),
        })

    # Anchor appears only in the last 5 seconds — driven by the actual final
    # video duration (last clip's videoEnd), not the CLOSE segment's startSec.
    # Sprint 08.5: the old "segments[-1].startSec" placement fired at ~52 s on
    # a 65 s video; operator wants BOOK HERE as a closing stinger only.
    ANCHOR_WINDOW_SEC = 5.0
    if clips:
        video_duration_sec = float(clips[-1]["videoEnd"])
    elif segments:
        video_duration_sec = float(segments[-1]["endSec"])
    else:
        video_duration_sec = 0.0
    anchor_start = max(0.0, video_duration_sec - ANCHOR_WINDOW_SEC)

    return {
        "meta": {
            "poiName": poi_name,
            "location": location,
            "fps": fps,
            "width": DEFAULT_WIDTH,
            "height": DEFAULT_HEIGHT,
        },
        "clips": clips,
        "audio": {
            "narration": f"{file_prefix}{os.path.basename(narration_path)}",
            "bgm": f"{file_prefix}{os.path.basename(bgm_path)}",
            "bgmVolume": bgm_volume,
            "bgmDuckedVolume": bgm_ducked_volume,
            "duckRampSec": DEFAULT_DUCK_RAMP_SEC,
        },
        "captions": {
            "wordTimestamps": word_timestamps,
            "highlightColor": DEFAULT_HIGHLIGHT_COLOR,
            "defaultColor": DEFAULT_TEXT_COLOR,
            "fontFamily": "Montserrat",
            "fontSize": DEFAULT_CAPTION_FONT_SIZE,
        },
        "segments": segments,
        "anchor": {
            "enabled": True,
            "text": "BOOK HERE",
            "startSec": round(anchor_start, 3),
            "durationSec": round(ANCHOR_WINDOW_SEC, 3),
        },
    }


# ---------------------------------------------------------------------------
#  Clip-to-narration binding (self-contained, no FFmpeg dependency)
# ---------------------------------------------------------------------------
#
# Sprint 10 C5 retired four pre-Sprint-10 helpers whose jobs moved to the
# two-pass Gemini architecture. Phrase boundaries are now carried as
# global word indices on each Gemini #2 assignment entry, per-phrase
# trim_start is chosen by Gemini #2 under the hard-constraint enforcement
# in :func:`promo.core.assign.clip_assigner._enforce_hard_constraint_and_enrich`,
# and segment text now comes from ``script_segments`` passed into
# :func:`build_props`. See ``workflow/projects/promo-foundation/checkpoints/
# 2026-04-17-sprint-10b-pre-flight-sweep.md`` for the retirement catalogue.

def get_clip_duration(clip_path: str) -> float:
    """Get clip duration via ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", clip_path],
            capture_output=True, text=True, timeout=10,
        )
        return float(result.stdout.strip())
    except Exception as exc:
        logger.warning("ffprobe failed for %s, using 5.0s default: %s", clip_path, exc)
        return 5.0


def _bind_clips_to_narration(
    assignments: list[ClipAssignment],
    clip_paths: dict[str, str],
    word_timestamps: list[WordTimestamp],
    target_duration_sec: float | None = None,
) -> list[dict[str, Any]]:
    """Bind Gemini #2 clip assignments to narration word timestamps.

    Sprint 10 C5 rewrite — consumes the word-idx ``assignments`` list
    produced by :func:`promo.core.assign.clip_assigner.assign_clips`. Each
    assignment entry carries ``start_word_idx`` / ``end_word_idx``
    (indices into the global ``word_timestamps`` list), plus
    ``trim_start`` and ``source_duration_sec`` already chosen by
    Gemini #2 under the hard-constraint enforcement in ``clip_assigner``.
    The pre-Sprint-10 string-matching path that walked per-clip phrase
    markers inside ``script_segments`` retired with this rewire.

    When ``target_duration_sec`` is provided and exceeds the narration
    duration, the last clip is extended to create visual-only breathing
    room (BGM only, no narration) so the composition hits target.

    Returns renderer-ready entries:
        {path, clip_id, trim_start, trim_end, video_start, narration,
         source_duration}

    The freeze-prevention bridge block at the bottom of this function
    is canonical infrastructure — it pulls from the unused-clip pool
    to cover inter-phrase silences longer than the primary clip's
    remaining footage. Sprint 10b's proposed C7 deletion was
    retracted (revert ``ab3938c``) after post-C7 smoke proved that
    inter-segment pauses can exceed ``max_clip_dur`` and no single
    assignment from Gemini #2 can fit such spans; bridges are how
    the physical pool covers them without a silent freeze.
    :class:`FreezeWouldOccurError` is raised when the bridge pool
    itself exhausts.
    """
    if not word_timestamps or not assignments:
        return []

    narration_end = word_timestamps[-1]["end"]
    final_display_end = (
        float(target_duration_sec)
        if target_duration_sec and target_duration_sec > narration_end
        else narration_end
    )

    def _is_valid_assignment(entry: ClipAssignment) -> bool:
        try:
            s_idx = int(entry["start_word_idx"])
            e_idx = int(entry["end_word_idx"])
        except (KeyError, TypeError, ValueError):
            return False
        if s_idx < 0 or e_idx < s_idx or e_idx >= len(word_timestamps):
            return False
        raw_id = str(entry.get("clip_id", ""))
        return bool(clip_paths.get(raw_id) or clip_paths.get(raw_id.zfill(4)))

    # Step 1: lower each word-idx assignment to a renderer entry in-order.
    renderer_entries: list[dict] = []
    n = len(assignments)
    for i, a in enumerate(assignments):
        raw_clip_id = str(a.get("clip_id", ""))
        clip_id = raw_clip_id.zfill(4)

        start_idx = int(a["start_word_idx"])
        end_idx = int(a["end_word_idx"])
        if (
            start_idx < 0
            or end_idx < start_idx
            or end_idx >= len(word_timestamps)
        ):
            logger.warning(
                "Assignment %d (segment %s, clip %s) has invalid word indices "
                "[%d, %d] against word_timestamps len=%d, skipping.",
                i, a.get("segment"), clip_id, start_idx, end_idx,
                len(word_timestamps),
            )
            continue

        clip_path = clip_paths.get(raw_clip_id) or clip_paths.get(clip_id)
        if not clip_path:
            logger.warning(
                "Clip %s missing from clip_paths, skipping assignment %d.",
                clip_id, i,
            )
            continue

        video_start = float(word_timestamps[start_idx]["start"])

        # Display end runs to the NEXT *valid* assignment's start (fills
        # the gap and doesn't chase a timestamp tied to an assignment that
        # will itself be skipped). Falls through to the final display end
        # if no later assignment is valid. Addresses logic-auditor L-005.
        video_end_t: float = final_display_end
        for j in range(i + 1, n):
            if _is_valid_assignment(assignments[j]):
                try:
                    nj = int(assignments[j]["start_word_idx"])
                    video_end_t = float(word_timestamps[nj]["start"])
                except (KeyError, TypeError, ValueError, IndexError):
                    video_end_t = narration_end
                break

        target_dur = max(round(video_end_t - video_start, 3), 0.5)

        trim_start = float(a.get("trim_start", 0.0))
        source_dur = float(a.get("source_duration_sec") or 0.0)
        if source_dur <= 0.0:
            source_dur = get_clip_duration(clip_path)

        trim_end = trim_start + target_dur

        narration_text = " ".join(
            str(wt.get("word", "")) for wt in word_timestamps[start_idx:end_idx + 1]
        ).strip()

        renderer_entries.append({
            "path": clip_path,
            "clip_id": clip_id,
            "usage_role": "assigned_phrase",
            "segment": a.get("segment"),
            "trim_start": round(trim_start, 3),
            "trim_end": round(trim_end, 3),
            "video_start": round(video_start, 3),
            "narration": narration_text,
            "source_duration": source_dur,
            "source_duration_sec": source_dur,
        })
        logger.info(
            "  clip %s: %.1fs @ %.1f-%.1fs | \"%s\"",
            clip_id, target_dur, video_start, video_end_t, narration_text[:50],
        )

    # Tail extension applies to the ACTUAL last renderer entry — not the
    # last input assignment, which may have been skipped. Addresses
    # logic-auditor L-001. Capped by remaining source footage so we never
    # cause a freeze by over-extending.
    if renderer_entries:
        last = renderer_entries[-1]
        TAIL_SEC = 0.3
        src = float(last.get("source_duration") or 0.0)
        tail = min(TAIL_SEC, max(src - last["trim_end"], 0.0))
        if tail > 0:
            last["trim_end"] = round(last["trim_end"] + tail, 3)

    # Enforce strictly increasing video_start (prevent zero-duration clips in Remotion).
    for i in range(1, len(renderer_entries)):
        if renderer_entries[i]["video_start"] <= renderer_entries[i - 1]["video_start"]:
            logger.warning(
                "Non-increasing video_start: clip %s (%.3f) <= clip %s (%.3f), bumping by 1ms",
                renderer_entries[i]["clip_id"], renderer_entries[i]["video_start"],
                renderer_entries[i - 1]["clip_id"], renderer_entries[i - 1]["video_start"],
            )
            renderer_entries[i]["video_start"] = round(
                renderer_entries[i - 1]["video_start"] + 0.001, 3,
            )

    logger.info("Bound %d clips to narration (%.1fs)", len(renderer_entries), narration_end)

    # Sprint 08.5 freeze-prevention (overall rule — a clip must never play past
    # its source duration). Scan each entry's displayed span (= next clip's
    # video_start − this clip's video_start, or target_duration_sec − this
    # clip's video_start for the last clip). If that span exceeds the clip's
    # source_duration, insert bridge clips from the unused pool to cover the
    # overflow instead of letting OffthreadVideo hold its last frame. This
    # block is canonical infrastructure (see the function docstring for why
    # Sprint 10b's proposed deletion was retracted).
    used_ids = {e["clip_id"] for e in renderer_entries}
    unused_with_dur: list[tuple[str, str, float]] = []
    for cid, cpath in clip_paths.items():
        padded = str(cid).zfill(4)
        if padded in used_ids or cid in used_ids:
            continue
        unused_with_dur.append(
            (padded, cpath, get_clip_duration(cpath))
        )
    # Longest first — best coverage per bridge.
    unused_with_dur.sort(key=lambda t: -t[2])

    fixed: list[dict] = []
    bridge_count = 0
    for i, entry in enumerate(renderer_entries):
        fixed.append(entry)
        displayed_end = (
            renderer_entries[i + 1]["video_start"]
            if i + 1 < len(renderer_entries)
            else final_display_end
        )
        source_dur = float(entry.get("source_duration", 0.0))
        if source_dur <= 0:
            continue
        # Usable footage runs from trim_start to end-of-source. OffthreadVideo
        # uses trimBefore only — no trimAfter — so the clip plays from
        # trim_start until source runs out, then freezes. Cursor must track
        # where the clip actually runs out of footage, not where source_dur
        # would run out from video_start=0.
        trim_start = float(entry.get("trim_start", 0.0))
        usable_dur = max(source_dur - trim_start, 0.0)
        cursor = entry["video_start"] + usable_dur
        remaining = displayed_end - cursor
        while remaining > HARD_CONSTRAINT_TOL_SEC and unused_with_dur:
            new_cid, new_path, new_src = unused_with_dur.pop(0)
            if new_src <= 0:
                logger.warning(
                    "Freeze-prevention: skipping bridge candidate %s with "
                    "invalid source duration %.2fs.", new_cid, new_src,
                )
                continue
            bridge_dur = min(remaining, new_src)
            bridge = {
                "path": new_path,
                "clip_id": new_cid,
                "usage_role": "bridge_tail",
                "segment": None,
                "trim_start": 0.0,
                "trim_end": round(bridge_dur, 3),
                "video_start": round(cursor, 3),
                "narration": "",  # visual-only bridge, no caption tie-in
                "source_duration": new_src,
                "source_duration_sec": new_src,
            }
            fixed.append(bridge)
            bridge_count += 1
            logger.info(
                "Freeze-prevention: bridged %.2fs after clip %s with clip %s "
                "(src=%.1fs).",
                bridge_dur, entry["clip_id"], new_cid, new_src,
            )
            cursor += bridge_dur
            remaining = displayed_end - cursor
        if remaining > HARD_CONSTRAINT_TOL_SEC:
            # Sprint 09a M-006 (operator directive 2026-04-17): the pipeline
            # must never render a freeze-prone video. Pre-09a logged this and
            # continued; now we raise so full_pipeline can return False
            # without writing an mp4.
            logger.error(
                "Freeze-prevention: clip %s has %.2fs overflow past usable "
                "footage (%.1fs = source %.1fs − trim_start %.1fs) "
                "but unused clip pool is exhausted — aborting render.",
                entry["clip_id"], remaining, usable_dur, source_dur, trim_start,
            )
            raise FreezeWouldOccurError(
                f"Clip {entry['clip_id']} has {remaining:.2f}s overflow past "
                f"usable footage and the unused clip pool is exhausted; "
                f"rendering would freeze. Increase the clip pool size or "
                f"adjust target_duration_sec."
            )
    renderer_entries = fixed
    if bridge_count > 0:
        logger.info(
            "Freeze-prevention summary: inserted %d bridge clip(s); "
            "total clips now %d.",
            bridge_count, len(renderer_entries),
        )

    if target_duration_sec and len(renderer_entries) > 0:
        last = renderer_entries[-1]
        actual_video_end = round(
            last["video_start"] + (last["trim_end"] - last["trim_start"]), 3,
        )
        shortfall = float(target_duration_sec) - actual_video_end
        if shortfall > 0.1:
            logger.warning(
                "Final video end %.2fs falls %.2fs short of target %.1fs — "
                "unused clip pool insufficient to bridge the gap.",
                actual_video_end, shortfall, target_duration_sec,
            )
        logger.info(
            "Dynamic duration: narration=%.1fs, target=%.1fs, "
            "actual video end=%.1fs (includes %d bridge clip(s)).",
            narration_end, target_duration_sec, actual_video_end, bridge_count,
        )

    return renderer_entries


def build_renderer_timeline_entries(
    renderer_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project renderer entries into a manifest-friendly timeline shape.

    ``_bind_clips_to_narration`` owns bridge insertion and tail extension.
    This helper runs after that binding so the returned rows match the
    final visual timeline that Remotion receives.
    """
    timeline: list[dict[str, Any]] = []
    for i, entry in enumerate(renderer_entries):
        display_start = float(entry["video_start"])
        if i + 1 < len(renderer_entries):
            display_end = float(renderer_entries[i + 1]["video_start"])
        else:
            display_end = display_start + (
                float(entry["trim_end"]) - float(entry["trim_start"])
            )
        timeline.append({
            "clip_id": str(entry["clip_id"]).zfill(4),
            "usage_role": str(entry.get("usage_role") or "assigned_phrase"),
            "segment": entry.get("segment"),
            "source_path": entry.get("path"),
            "trim_start_sec": round(float(entry["trim_start"]), 3),
            "trim_end_sec": round(float(entry["trim_end"]), 3),
            "display_start_sec": round(display_start, 3),
            "display_end_sec": round(display_end, 3),
            "source_duration_sec": round(
                float(entry.get("source_duration_sec", entry.get("source_duration", 0.0))),
                3,
            ),
            "narration": entry.get("narration", ""),
        })
    return timeline


# ---------------------------------------------------------------------------
#  Authored pause windows (Sprint 07 — Spike 6 feed-forward)
# ---------------------------------------------------------------------------

# Pauses shorter than this are considered filler/none and are NOT surfaced.
PAUSE_WINDOWS_MIN_MS = 300


def _compute_pause_windows(
    script_segments: list[ScriptSegment],
    segment_timestamps: list[SegmentTimestamp],
) -> list[dict[str, Any]]:
    """Compute ``audio.pauseWindows`` from authored ``pause_after_ms`` values.

    One entry per inter-segment gap whose authored duration is
    ``>= PAUSE_WINDOWS_MIN_MS``. The last segment's ``pause_after_ms`` is
    ignored (nothing follows it). Intra-segment pauses are NOT represented.

    Shape per entry: exactly ``{startSec, durationSec, afterSegmentIdx, strategy}``
    — no extra keys. ``strategy`` is ``None`` in Sprint 07; see strategy enum in
    :func:`build_props_from_script`.
    """
    if not script_segments or not segment_timestamps:
        return []
    end_by_seg: dict[int, float] = {}
    for st in segment_timestamps:
        try:
            end_by_seg[int(st["segment"])] = float(st["end"])
        except (KeyError, TypeError, ValueError):
            continue
    windows: list[dict] = []
    # Skip the last segment — nothing follows it.
    for idx, seg in enumerate(script_segments[:-1]):
        pause_ms = int(seg.get("pause_after_ms") or 0)
        if pause_ms < PAUSE_WINDOWS_MIN_MS:
            continue
        try:
            seg_num = int(seg.get("segment") or (idx + 1))
        except (TypeError, ValueError):
            seg_num = idx + 1
        end_sec = end_by_seg.get(seg_num)
        if end_sec is None:
            # Segment had no timestamps (e.g. timestamp exhaustion) — skip cleanly.
            continue
        windows.append({
            "startSec": round(end_sec, 3),
            "durationSec": round(pause_ms / 1000.0, 3),
            "afterSegmentIdx": idx,
            "strategy": None,
        })
    return windows


# ---------------------------------------------------------------------------
#  Props builder from script segments (main entry point)
# ---------------------------------------------------------------------------

def build_props_from_script(
    poi_name: str,
    location: str,
    script_segments: list[ScriptSegment],
    clip_paths: dict[str, str],
    narration_result: Narration,
    bgm_path: str,
    assignments: list[ClipAssignment],
    fps: int = DEFAULT_FPS,
    target_duration_sec: float | None = None,
    timeline_entries: list[dict[str, Any]] | None = None,
) -> dict[str, object]:
    """Build props.json from script + TTS + Gemini #2 clip assignments.

    Sprint 10 C5: ``assignments`` is the word-idx per-phrase list produced
    by :func:`promo.core.assign.clip_assigner.assign_clips` (post-F3-retry in the
    full_pipeline). ``script_segments`` is still passed through so
    :func:`build_props` can populate ``segments[i].text`` by segment-number
    lookup without reconstructing from word_timestamps — the retired
    ``_reconstruct_segment_text`` helper's job.

    Emits ``audio.pauseWindows`` authored from Gemini's ``pause_after_ms``.
    // AUTHORED from Gemini pause_after_ms — NOT a silence ground truth
    // strategy enum: "extend-current" | "pull-next" | "broll" | "data-card"
    //                | "slow-motion" | "map-reveal" | null
    """
    word_timestamps = narration_result["word_timestamps"]
    segment_timestamps = narration_result["segment_timestamps"]

    clip_assignments = _bind_clips_to_narration(
        assignments, clip_paths, word_timestamps,
        target_duration_sec=target_duration_sec,
    )
    if timeline_entries is not None:
        timeline_entries.extend(build_renderer_timeline_entries(clip_assignments))

    props = build_props(
        poi_name=poi_name,
        location=location,
        clip_assignments=clip_assignments,
        word_timestamps=word_timestamps,
        segment_timestamps=segment_timestamps,
        narration_path=narration_result["audio_path"],
        bgm_path=bgm_path,
        fps=fps,
        script_segments=script_segments,
    )

    # Attach authored pauseWindows under the existing audio object (per Spike 6
    # schema coordination with the Remotion-side session). The Remotion
    # component does not consume this field in Sprint 07 — future sprints pick
    # strategies per window for dynamic arrangement editing.
    props["audio"]["pauseWindows"] = _compute_pause_windows(
        script_segments, segment_timestamps,
    )
    return props


# ---------------------------------------------------------------------------
#  Validation (Step 7.5 from PRD)
# ---------------------------------------------------------------------------

def validate_props(props: dict, check_files: bool = True) -> list[str]:
    """Validate props.json before Remotion render. Returns list of errors.

    Args:
        props: Props dict to validate.
        check_files: If True, also check that media files exist in public/.
            Set to False for structural-only validation before staging.
    """
    errors = []

    # Required top-level fields
    for field in ("meta", "clips", "audio", "captions", "segments"):
        if field not in props:
            errors.append(f"Missing required field: {field}")
    if errors:
        return errors  # Can't validate further without structure

    # Clips and segments must exist and be non-empty
    clips = props.get("clips", [])
    if not clips:
        errors.append("No clips defined")
    segments = props.get("segments", [])
    if not segments:
        errors.append("No segments defined")

    if check_files:
        # Clip files must exist in public/
        for clip in clips:
            clip_file = os.path.join(PUBLIC_DIR, clip["file"])
            if not clip["file"].startswith("http") and not os.path.exists(clip_file):
                errors.append(f"Clip file not found in public/: {clip['file']}")

        # Audio files must exist in public/
        audio = props.get("audio", {})
        for key in ("narration", "bgm"):
            filename = audio.get(key, "")
            if filename and not filename.startswith("http"):
                if not os.path.exists(os.path.join(PUBLIC_DIR, filename)):
                    errors.append(f"Audio file not found in public/: {filename}")

    # videoStart must be strictly increasing (no overlaps, no duplicates).
    # We don't compare against videoEnd because natural clip transitions
    # have videoEnd == next videoStart (Remotion Sequence handles handoffs).
    for i in range(1, len(clips)):
        if clips[i]["videoStart"] <= clips[i - 1]["videoStart"]:
            errors.append(
                f"Clip {clips[i]['clipId']} videoStart ({clips[i]['videoStart']}) "
                f"not after previous clip ({clips[i-1]['videoStart']})"
            )

    # Word timestamps count vs segment word count. Tolerance accommodates
    # tokenization differences between Python split() and the TTS engine's
    # alignment (contractions, numerals expanded to words, punctuation).
    total_words = sum(len(s["text"].split()) for s in props.get("segments", []))
    ts_count = len(props.get("captions", {}).get("wordTimestamps", []))
    tolerance = max(5, int(total_words * 0.1))
    if abs(total_words - ts_count) > tolerance:
        errors.append(
            f"Word count mismatch: {total_words} in segments vs {ts_count} timestamps "
            f"(tolerance={tolerance})"
        )

    return errors


# ---------------------------------------------------------------------------
#  Media staging — copy files into Remotion's public/ directory
# ---------------------------------------------------------------------------

from promo.core import sanitize_poi_name as _safe_poi_dir


def stage_media(
    clip_paths: list[str],
    narration_path: str,
    bgm_path: str,
    poi_name: str = "",
) -> None:
    """Copy media files into Remotion's public/ directory for rendering.

    AC8 fix: when poi_name is provided, files are staged into a POI-specific
    subdirectory (public/{safe_name}/) to prevent collisions between concurrent
    POIs that share clip basenames or narration.wav.

    Skips files that already exist with the same size (idempotent).
    """
    if poi_name:
        dest_dir = os.path.join(PUBLIC_DIR, _safe_poi_dir(poi_name))
    else:
        dest_dir = PUBLIC_DIR
    os.makedirs(dest_dir, exist_ok=True)

    all_files = list(clip_paths) + [narration_path, bgm_path]
    for src in all_files:
        if not src or not os.path.exists(src):
            logger.warning("Source file not found, skipping: %s", src)
            continue

        dst = os.path.join(dest_dir, os.path.basename(src))
        # Skip if already present with same size
        if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
            continue

        shutil.copy2(src, dst)
        rel = os.path.relpath(dst, PUBLIC_DIR)
        logger.info("Staged: %s → public/%s", os.path.basename(src), rel)


# ---------------------------------------------------------------------------
#  Render
# ---------------------------------------------------------------------------

def render_promo(
    props: dict,
    output_path: str,
    composition_id: str = "HotelPromo",
    timeout: int | None = None,
) -> bool:
    """Render a promo video via Remotion.

    Args:
        props: Validated props dict.
        output_path: Where to write the final MP4.
        composition_id: Remotion composition to render (default: HotelPromo).
        timeout: Max render time in seconds. When omitted, resolved from
            ``PROMO_RENDER_TIMEOUT_SEC``.

    Returns:
        True on success, False on failure.
    """
    # Validate first (with file checks — media should be staged already)
    errors = validate_props(props)
    if errors:
        for err in errors:
            logger.error("Validation error: %s", err)
        return False

    # Write props to render-unique file (concurrent safety — UUID prevents
    # collisions between concurrent renders of the same POI)
    poi_name = props.get("meta", {}).get("poiName") or "unnamed"
    safe_name = _safe_poi_dir(poi_name)
    render_id = uuid.uuid4().hex[:8]
    props_path = os.path.join(REMOTION_DIR, f"props_{safe_name}_{render_id}.json")
    staged_dir = os.path.join(PUBLIC_DIR, safe_name)

    with open(props_path, "w", encoding="utf-8") as f:
        json.dump(props, f, indent=2, ensure_ascii=False)
    logger.info("Wrote %s (%d clips, %d words)",
                os.path.basename(props_path),
                len(props["clips"]),
                len(props.get("captions", {}).get("wordTimestamps", [])))

    try:
        # Resolve output path to absolute (Remotion runs in its own cwd)
        output_path = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Render with optimization flags
        from promo.core.config import (
            render_concurrency as _render_concurrency,
            render_timeout_sec as _render_timeout_sec,
        )
        concurrency = _render_concurrency()
        render_timeout = timeout if timeout is not None else _render_timeout_sec()
        cmd = [
            "npx", "remotion", "render", composition_id, output_path,
            "--props", props_path,
            "--concurrency", str(concurrency),
            "--x264-preset", "fast",
            "--crf", "22",
        ]

        logger.info("Rendering: %s", " ".join(cmd))

        result = subprocess.run(
            cmd,
            cwd=REMOTION_DIR,
            capture_output=True,
            text=True,
            timeout=render_timeout,
        )

        if result.returncode != 0:
            logger.error("Remotion render failed (exit %d):\nSTDOUT:\n%s\nSTDERR:\n%s",
                         result.returncode,
                         result.stdout[-2000:],
                         result.stderr[-2000:])
            return False

        # Verify output exists and has content
        if not os.path.exists(output_path):
            logger.error("Render completed but output file not found: %s", output_path)
            return False

        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        if size_mb < 0.1:
            logger.error("Output file suspiciously small (%.2f MB): %s", size_mb, output_path)
            return False

        logger.info("Render complete: %s (%.1f MB)", output_path, size_mb)
        return True

    except subprocess.TimeoutExpired:
        logger.error("Remotion render timed out after %ds", render_timeout)
        return False
    except FileNotFoundError:
        logger.error("npx not found — is Node.js installed?")
        return False
    finally:
        # Clean up props JSON (D-004) and staged media directory (D-008)
        if os.path.exists(props_path):
            os.unlink(props_path)
            logger.debug("Cleaned up %s", props_path)
        if os.path.isdir(staged_dir):
            shutil.rmtree(staged_dir, ignore_errors=True)
            logger.debug("Cleaned up staged dir %s", staged_dir)
