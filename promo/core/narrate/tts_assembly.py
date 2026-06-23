"""Backend-agnostic audio assembly utilities — ffmpeg + timestamp stitching.

This is a "library" module (not a 1-IO API service): it exposes a small
set of cohesive primitives the dispatcher and both TTS backends compose.
Operator-blessed exception to the API-service split principle (S2a Q3).

Public surface
--------------
ffmpeg primitives (used by both backends + the dispatcher):
    - :func:`_run_ffmpeg`              — shell-out + error surfacing
    - :func:`_generate_silence_mp3`    — exact-duration silence mp3
    - :func:`_ffmpeg_concat_mp3s`      — concat demuxer with re-encode
    - :func:`_ffprobe_duration`        — measured audio duration

Timestamp utilities (consumed by ``generate_narration``):
    - :func:`_validate_word_timestamps` — shape sanity check
    - :func:`_back_allocate_timestamps` — batch→source-segment mapping

The audio-format constants (44.1 kHz mono mp3) are load-bearing across
both silence + Gemini PCM→MP3 paths so the concat demuxer can stream
mixed-source streams without re-encoding artifacts.
"""

import logging
import os
import subprocess
import tempfile

from promo.core.schema import SegmentTimestamp, WordTimestamp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Audio-format constants (shared across silence + concat + Gemini PCM→MP3)
# ---------------------------------------------------------------------------
# ffmpeg writes mp3 at 44.1kHz mono silence to match the ElevenLabs output
# format (mp3_44100_128) so the concat demuxer accepts the streams.
_SILENCE_SAMPLE_RATE = 44100
_SILENCE_CODEC = "libmp3lame"
_SILENCE_BITRATE = "128k"

# EBU R128 loudness normalization for the assembled narration. Brings every
# TTS engine's voice to a consistent integrated loudness so it sits ABOVE the
# music bed (Remotion ducks music under the narration); the true-peak ceiling
# prevents clipping structurally regardless of engine. Tune NARRATION_LUFS_TARGET
# after listening (less negative = louder voice relative to music).
NARRATION_LUFS_TARGET = -16.0     # integrated loudness, ffmpeg loudnorm I=
NARRATION_TRUE_PEAK_DBTP = -1.0   # true-peak ceiling, loudnorm TP=
NARRATION_LRA = 11.0              # loudness range, loudnorm LRA=


# ---------------------------------------------------------------------------
#  ffmpeg helpers
# ---------------------------------------------------------------------------

def _run_ffmpeg(args: list[str]) -> None:
    """Run ffmpeg with consistent error surfacing."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffmpeg is not installed or not on PATH. Remotion requires ffmpeg; "
            "install it via Homebrew: brew install ffmpeg"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed ({result.returncode}): {result.stderr[:500].strip()}"
        )


def _generate_silence_mp3(duration_sec: float, output_path: str) -> None:
    """Write a mono 44.1kHz mp3 of exactly ``duration_sec`` seconds of silence.

    ElevenLabs returns mp3_44100_128 which is stereo-capable but their
    voice audio is mono-compatible. We match the sample rate so the
    ffmpeg concat demuxer can stream-copy without re-encoding.
    """
    _run_ffmpeg([
        "-f", "lavfi",
        "-i", f"anullsrc=channel_layout=mono:sample_rate={_SILENCE_SAMPLE_RATE}",
        "-t", f"{duration_sec:.3f}",
        "-acodec", _SILENCE_CODEC,
        "-b:a", _SILENCE_BITRATE,
        output_path,
    ])


def _ffmpeg_concat_mp3s(
    inputs: list[str], output_path: str, *, normalize_loudness: bool = False,
) -> None:
    """Concatenate ``inputs`` (ordered mp3 paths) into one mp3 at ``output_path``.

    Uses the concat demuxer, which re-encodes via libmp3lame to guarantee
    compatibility across mixed-source streams (TTS vs. silence).

    ``normalize_loudness`` (final assembly only) applies a single-pass EBU R128
    ``loudnorm`` in the SAME re-encode — raising the voice to
    NARRATION_LUFS_TARGET and capping true peak at NARRATION_TRUE_PEAK_DBTP so
    it sits above the ducked music bed and never clips. Single-pass loudnorm
    preserves duration → no word_timestamp drift. Off for intermediate
    (per-batch) concats. Engine-agnostic: this is the one final-assembly call.
    """
    if not inputs:
        raise ValueError("concat inputs list is empty")
    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".txt", prefix="concat_",
        dir=os.path.dirname(output_path) or None,
    ) as f:
        for p in inputs:
            abs_p = os.path.abspath(p).replace("'", r"'\''")
            f.write(f"file '{abs_p}'\n")
        concat_list = f.name
    loudnorm_args = (
        ["-af", (
            f"loudnorm=I={NARRATION_LUFS_TARGET}:"
            f"TP={NARRATION_TRUE_PEAK_DBTP}:LRA={NARRATION_LRA}"
        )]
        if normalize_loudness else []
    )
    try:
        _run_ffmpeg([
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list,
            *loudnorm_args,
            "-acodec", _SILENCE_CODEC,
            "-b:a", _SILENCE_BITRATE,
            "-ar", str(_SILENCE_SAMPLE_RATE),
            "-ac", "1",
            output_path,
        ])
    finally:
        try:
            os.remove(concat_list)
        except OSError:
            pass


def _ffprobe_duration(path: str) -> float:
    """Return the audio file's duration in seconds. Raises on failure."""
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("ffprobe not on PATH") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"ffprobe failed on {path}: {result.stderr[:300]}")
    return float(result.stdout.strip())


# ---------------------------------------------------------------------------
#  Word-timestamp utilities
# ---------------------------------------------------------------------------

def _validate_word_timestamps(
    word_timestamps: list[WordTimestamp],
    narration_duration: float,
    *,
    tolerance_sec: float = 0.5,
) -> None:
    """Sanity-check shape. Raises ``RuntimeError`` with a clear message on failure."""
    if not isinstance(word_timestamps, list) or not word_timestamps:
        raise RuntimeError(
            "word_timestamps is empty — TTS backend returned no alignment"
        )
    max_end = narration_duration + tolerance_sec
    for idx, wt in enumerate(word_timestamps):
        if not isinstance(wt, dict):
            raise RuntimeError(
                f"word_timestamps[{idx}] is not a dict (got {type(wt).__name__})"
            )
        for key in ("word", "start", "end"):
            if key not in wt:
                raise RuntimeError(
                    f"word_timestamps[{idx}] missing key '{key}': {wt!r}"
                )
        word = wt["word"]
        if not isinstance(word, str) or not word.strip():
            raise RuntimeError(
                f"word_timestamps[{idx}] has empty/invalid word: {wt!r}"
            )
        start = wt["start"]
        end = wt["end"]
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            raise RuntimeError(
                f"word_timestamps[{idx}] has non-numeric start/end: {wt!r}"
            )
        if start < 0:
            raise RuntimeError(
                f"word_timestamps[{idx}] has negative start={start}: {wt!r}"
            )
        if end <= start:
            raise RuntimeError(
                f"word_timestamps[{idx}] has end<=start ({end}<={start}): {wt!r}"
            )
        if end > max_end:
            raise RuntimeError(
                f"word_timestamps[{idx}] end={end} exceeds narration_duration+tol={max_end}: {wt!r}"
            )


def _back_allocate_timestamps(
    batch_audios: list[tuple[str, float, list[WordTimestamp]]],
    batches: list[dict],
    silence_durations_sec: list[float],
) -> tuple[list[WordTimestamp], list[SegmentTimestamp], float]:
    """Map batch-level word timestamps back to per-source-segment spans.

    Sprint 09b C6 extraction out of ``generate_narration`` (shaves ~95
    lines off the assembly body). Behavior is identical to the inline
    implementation.

    Each batch's word timestamps are offset by ``cumulative_offset``
    (the running concat time before this batch). Then words are split
    back into per-source-segment spans. Preferred path: exact
    word_count match. Fallback: proportional character-count split —
    used when the ElevenLabs tokenization diverges from the LLM's
    ``word_count`` (numerals, contractions); logged as a warning when
    divergence occurs.

    Returns (word_timestamps, segment_timestamps, cumulative_offset).
    The final cumulative_offset is the total audio time consumed by
    all batches + inter-batch silences — callers use it as a sanity
    check against ffprobe'd concat duration.
    """
    word_timestamps: list[WordTimestamp] = []
    segment_timestamps: list[SegmentTimestamp] = []
    cumulative_offset = 0.0
    for b_idx, ((_bp, b_dur, b_words), batch) in enumerate(
        zip(batch_audios, batches)
    ):
        offset_words: list[WordTimestamp] = [
            WordTimestamp(
                word=w["word"],
                start=round(float(w["start"]) + cumulative_offset, 3),
                end=round(float(w["end"]) + cumulative_offset, 3),
            )
            for w in b_words
        ]
        word_timestamps.extend(offset_words)

        source_segs = batch["segments"]
        expected_wcs = [int(s.get("word_count") or 0) for s in source_segs]
        total_expected = sum(expected_wcs)

        if total_expected == len(offset_words) and total_expected > 0:
            w_idx = 0
            for src, wc in zip(source_segs, expected_wcs):
                if wc <= 0:
                    segment_timestamps.append(SegmentTimestamp(
                        segment=src.get("segment", len(segment_timestamps) + 1),
                        start=round(cumulative_offset, 3),
                        end=round(cumulative_offset, 3),
                        duration=0.0,
                        word_count=0,
                    ))
                    continue
                span = offset_words[w_idx:w_idx + wc]
                s_start = span[0]["start"]
                s_end = span[-1]["end"]
                segment_timestamps.append(SegmentTimestamp(
                    segment=src.get("segment", len(segment_timestamps) + 1),
                    start=round(s_start, 3),
                    end=round(s_end, 3),
                    duration=round(s_end - s_start, 3),
                    word_count=wc,
                ))
                w_idx += wc
        else:
            if len(source_segs) > 1 or total_expected != len(offset_words):
                logger.warning(
                    "Batch %d source word_count total (%d) != returned word "
                    "count (%d); falling back to proportional character-count "
                    "split across %d source segment(s).",
                    b_idx + 1, total_expected, len(offset_words),
                    len(source_segs),
                )
            # Proportional fallback by character count of source text.
            total_chars = sum(
                max(1, len((s.get("text") or "").strip()))
                for s in source_segs
            )
            w_idx = 0
            accumulated_chars = 0
            for s_i, src in enumerate(source_segs):
                src_chars = max(1, len((src.get("text") or "").strip()))
                accumulated_chars += src_chars
                if s_i == len(source_segs) - 1:
                    split_idx = len(offset_words)
                else:
                    split_idx = int(round(
                        len(offset_words) * accumulated_chars / total_chars
                    ))
                    split_idx = min(split_idx, len(offset_words))
                span = offset_words[w_idx:split_idx]
                if span:
                    s_start = span[0]["start"]
                    s_end = span[-1]["end"]
                else:
                    s_start = cumulative_offset
                    s_end = cumulative_offset
                segment_timestamps.append(SegmentTimestamp(
                    segment=src.get("segment", len(segment_timestamps) + 1),
                    start=round(s_start, 3),
                    end=round(s_end, 3),
                    duration=round(s_end - s_start, 3),
                    word_count=len(span),
                ))
                w_idx = split_idx

        cumulative_offset += b_dur
        if b_idx < len(silence_durations_sec):
            cumulative_offset += silence_durations_sec[b_idx]

    return word_timestamps, segment_timestamps, cumulative_offset
