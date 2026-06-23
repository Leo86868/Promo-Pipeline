"""TTS engine for promotional hotel narration — dual backend.

Architecture
------------
Two backends coexist behind a single dispatch seam inside
:func:`generate_narration`:

- **ElevenLabs v2** (Sprint 08 baseline) — per-segment TTS calls with
  API-returned ``normalized_alignment`` providing ~10ms word timestamps.
- **Gemini 3.1 Flash TTS** (Sprint TTS-Migration) — PCM 24kHz mono
  response, re-encoded to mp3 at 44.1kHz to match the concat sample
  rate; word timestamps produced via
  :mod:`promo.core.narrate.forced_aligner` (torchaudio MMS_FA, p95 = 44.6ms on
  the 99-word test script). Primary model
  ``gemini-3.1-flash-tts-preview`` with ``gemini-2.5-flash-preview-tts``
  as fallback on HTTP 404/403.

Both backends return ``(path, duration_sec, word_timestamps)`` tuples of
identical shape, consumed by :func:`_back_allocate_timestamps` without
a branch. The dispatch site is the ONLY place a
``backend == "gemini"`` / ``"elevenlabs"`` check lives (contract N3 —
operator's 屎山代码 constraint).

Per-segment TTS + ffmpeg silence concat is preserved across both
backends. ElevenLabs' ``<break>`` compression (Sprint 08 learning —
docs at https://help.elevenlabs.io/hc/en-us/articles/13416374683665) and
Gemini's unreliable pause tags (spike-measured σ up to 719ms — N1) are
both bypassed: exact silence MP3s via ffmpeg ``anullsrc`` remain the
canonical declarative-pause mechanism.

Usage:
    from promo.core.narrate.tts_engine import generate_narration

    result = generate_narration(
        segments=[{"segment": 1, "text": "...", "pause_after_ms": 1500, ...}, ...],
        voice_key="jarnathan",  # or "kore" for Gemini
    )
    # result = {"audio_path": "...", "word_timestamps": [...], "segment_timestamps": [...], ...}
"""

import logging
import os
import tempfile
from typing import Optional

from promo.core import arsenal_loader
from promo.core.schema import Narration, ScriptSegment, WordTimestamp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Back-compat re-exports — extracted symbols (Sprint S2a)
# ---------------------------------------------------------------------------
# Tests + downstream code import these private names directly from
# ``tts_engine`` (e.g. ``from promo.core.narrate.tts_engine import
# _normalize_for_tts`` and ``mock.patch("promo.core.narrate.tts_engine.
# _normalize_for_tts", ...)``). After S2a these symbols live in sibling
# modules; ``tts_engine`` remains the single import path the test +
# mock-patch surface targets so the ``generate_narration`` dispatcher
# resolves them via its own module scope (lets ``mock.patch.object``
# work without test rewrites).
from promo.core.narrate.tts_text_normalize import (  # noqa: E402
    _int_to_words,
    normalize_digits_to_words as _normalize_digits_to_words,
    normalize_for_tts as _normalize_for_tts,
)
from promo.core.narrate.tts_assembly import (  # noqa: E402
    _back_allocate_timestamps,
    _ffmpeg_concat_mp3s,
    _ffprobe_duration,
    _generate_silence_mp3,
    _run_ffmpeg,
    _validate_word_timestamps,
)
from promo.core.narrate.tts_elevenlabs import (  # noqa: E402
    MODEL_ID,
    VOICE_SETTINGS,
    _call_elevenlabs_with_timestamps,
    _characters_to_words,
    _clean_caption_word,
    _generate_segment_audio_elevenlabs,
)
from promo.core.narrate.tts_gemini import (  # noqa: E402
    GEMINI_FALLBACK_MODEL,
    GEMINI_PRIMARY_MODEL,
    _gemini_pcm_to_mp3,
    _gemini_tts_rest,
    _gemini_tts_with_fallback,
    _generate_segment_audio_gemini,
)


# ---------------------------------------------------------------------------
#  Voice catalog — dual backend (Gemini + ElevenLabs)
# ---------------------------------------------------------------------------
# Entry shape (backend-agnostic callers read only ``id`` and ``backend``):
#
#   {"id": str,           # ElevenLabs voice_id UUID OR Gemini voice name (e.g. "Kore")
#    "name": str,         # human-readable display name
#    "backend": "gemini" | "elevenlabs",
#    "style_prompt": str, # Gemini only — directorial prompt prepended to each
#                         # batch. Not spoken aloud (Gemini TTS interprets as a
#                         # delivery directive). Empty string = no prompt.
#    ...demographics for operator reference...}
#
# Order matters: ``promo.core.pipeline.bgm_voice_resolver._resolve_voice_keys``
# reads dict order for variant rotation when ``--voice`` is unset. Gemini
# entries are declared FIRST so the default rotation picks Gemini.
#
# Gemini entry (Kore + directorial prompt) locked by Sprint TTS-Migration
# Phase 1 voice A/B — decision memo at
# ``workflow/projects/promo-foundation/decisions/2026-04-21-gemini-voice-lock.md``.

# Sprint Arsenal Externalization (Commit 5): the literal catalog body
# moved to ``promo/arsenal/voices/catalog.yaml``. The Python symbol stays
# here as a re-export populated at module-import time, so callers that
# do ``from promo.core.narrate.tts_engine import VOICE_CATALOG`` keep
# working byte-identically. PyYAML preserves insertion order, so the
# Gemini-first dispatch rotation contract is preserved.
VOICE_CATALOG: dict[str, dict] = arsenal_loader.load_voice_catalog()


# ---------------------------------------------------------------------------
#  Main entry point
# ---------------------------------------------------------------------------

def generate_narration(
    segments: list[ScriptSegment],
    *,
    voice_id: Optional[str] = None,
    voice_key: str = "jarnathan",
    output_dir: Optional[str] = None,
    speed: float = 0.95,
) -> Narration:
    """Generate narration via batched ElevenLabs calls + ffmpeg silence concat.

    Sprint 08.5: segments are grouped into batches by ``plan_tts_batches``.
    Consecutive segments whose joining gap has ``pause_weight == 1`` share a
    single ElevenLabs call (natural prosody carries the beat); gaps with
    ``pause_weight >= 2`` trigger an explicit ffmpeg silence block between
    batches using the segment's ``pause_after_ms`` (populated by
    ``compute_pause_budget``).

    ``speed`` (default ``0.95``) overrides ``VOICE_SETTINGS.speed`` on every
    ElevenLabs call — exposed as the Sprint 08.5 ``--tts-speed`` CLI flag.

    On any exception during TTS / concat, partial ``seg_*.mp3`` /
    ``silence_*.mp3`` files written by this call are cleaned up. When
    ``output_dir`` was auto-created via ``tempfile.mkdtemp``, the tmpdir
    itself is removed.

    Returns a dict with keys:
        audio_path, word_timestamps, segment_timestamps, duration,
        voice_id, voice_key, tagged_text.
    """
    if not segments:
        raise ValueError("segments is empty")

    # Resolve backend + style_prompt from the catalog. Operators may still
    # override with a raw ``voice_id``; that path assumes ElevenLabs
    # (Gemini has no UUIDs — its "id" is the voice name).
    voice = VOICE_CATALOG.get(voice_key)
    if voice_id is None:
        if not voice:
            raise ValueError(
                f"Unknown voice_key: {voice_key}. Options: {list(VOICE_CATALOG.keys())}"
            )
        voice_id = voice["id"]
        backend = voice["backend"]
        style_prompt = voice.get("style_prompt", "") if backend == "gemini" else ""
    else:
        # Raw voice_id override — retain Sprint 07 behavior (ElevenLabs path).
        backend = (voice or {}).get("backend", "elevenlabs")
        style_prompt = (voice or {}).get("style_prompt", "") if backend == "gemini" else ""

    auto_tmpdir = output_dir is None
    if auto_tmpdir:
        output_dir = tempfile.mkdtemp(prefix="promo_tts_")
    os.makedirs(output_dir, exist_ok=True)

    created_files: list[str] = []
    success = False
    try:
        from promo.core.narrate.tts_batch_planner import plan_tts_batches

        batches = plan_tts_batches(segments)
        if not batches:
            raise ValueError("batch planner produced no batches")

        audio_path = os.path.join(output_dir, "narration.mp3")
        plain_text = " ".join(s.get("text", "").strip() for s in segments).strip()
        if not plain_text:
            raise ValueError("segments contain no narration text")
        logger.info(
            "Narration plain text (%d words, %d segments → %d batches, speed=%.2f): %s...",
            len(plain_text.split()), len(segments), len(batches), speed, plain_text[:100],
        )

        # Per-batch TTS. Each batch's text = space-joined source segment texts.
        batch_audios: list[tuple[str, float, list[WordTimestamp]]] = []
        for b_idx, batch in enumerate(batches):
            b_segs = batch["segments"]
            if not b_segs:
                continue
            b_text = " ".join(
                (s.get("text") or "").strip() for s in b_segs
            ).strip()
            if not b_text:
                raise ValueError(f"batch {b_idx + 1} has empty text")
            normalized_text = _normalize_for_tts(b_text)
            if normalized_text != b_text:
                logger.info(
                    "Batch %d: TTS text normalized: %r → %r",
                    b_idx + 1, b_text[:80], normalized_text[:80],
                )
            b_path = os.path.join(output_dir, f"seg_{b_idx + 1:02d}.mp3")
            created_files.append(b_path)
            # N3 dispatch seam — ONLY place ``backend`` equality is tested
            # inside this module. Downstream consumers see a unified tuple.
            if backend == "gemini":
                duration, words = _generate_segment_audio_gemini(
                    normalized_text, voice_id, b_path,
                    style_prompt=style_prompt,
                )
            else:
                duration, words = _generate_segment_audio_elevenlabs(
                    normalized_text, voice_id, b_path, speed=speed,
                )
            logger.info(
                "Batch %d/%d TTS (%d source segment(s)): %.2fs, %d words",
                b_idx + 1, len(batches), len(b_segs), duration, len(words),
            )
            batch_audios.append((b_path, duration, words))

        if not batch_audios:
            raise RuntimeError("generate_narration produced zero batch audios")

        # Concat order: batch1, silence1, batch2, silence2, ..., batchN.
        concat_inputs: list[str] = []
        silence_durations_sec: list[float] = []
        for i, (b_path, _dur, _words) in enumerate(batch_audios):
            concat_inputs.append(b_path)
            if i < len(batch_audios) - 1:
                silence_ms = batches[i].get("post_batch_silence_ms") or 0
                silence_ms = int(silence_ms)
                if silence_ms > 0:
                    silence_path = os.path.join(
                        output_dir, f"silence_{i + 1:02d}.mp3"
                    )
                    created_files.append(silence_path)
                    silence_sec = silence_ms / 1000.0
                    _generate_silence_mp3(silence_sec, silence_path)
                    concat_inputs.append(silence_path)
                    silence_durations_sec.append(silence_sec)
                else:
                    silence_durations_sec.append(0.0)

        # Final assembly: concat + EBU R128 loudnorm in one re-encode, so the
        # voice sits above the ducked music bed (+TP cap). Single-pass loudnorm
        # preserves duration → no word_timestamp drift below.
        _ffmpeg_concat_mp3s(concat_inputs, audio_path, normalize_loudness=True)
        created_files.append(audio_path)

        # Stitch word_timestamps with per-batch offsets. Sprint 09b C6:
        # logic extracted to _back_allocate_timestamps. For each batch,
        # words are offset by cumulative audio time and split back into
        # per-source-segment spans (exact word_count match preferred;
        # proportional char-count fallback on tokenization divergence).
        word_timestamps, segment_timestamps, cumulative_offset = (
            _back_allocate_timestamps(batch_audios, batches, silence_durations_sec)
        )

        # Sanity: measured concat duration vs cumulative offset.
        measured_duration = _ffprobe_duration(audio_path)
        tolerance_sec = 0.5
        drift = abs(measured_duration - cumulative_offset)
        if drift > tolerance_sec:
            raise RuntimeError(
                f"Narration assembly drift too large: concat ffprobe={measured_duration:.3f}s "
                f"vs stitched offsets={cumulative_offset:.3f}s (drift={drift:.3f}s, "
                f"tolerance={tolerance_sec}s). {len(concat_inputs)} concat inputs, "
                f"{sum(silence_durations_sec):.3f}s of inter-batch silence."
            )
        if drift > 0.1:
            logger.warning(
                "Narration assembly drift %.3fs (within tolerance %.2fs)",
                drift, tolerance_sec,
            )

        _validate_word_timestamps(word_timestamps, measured_duration)

        pure_spoken_sec = sum(dur for _path, dur, _words in batch_audios)
        measured_wpm_spoken = (
            len(word_timestamps) / pure_spoken_sec * 60.0
            if pure_spoken_sec > 0 else None
        )

        result: Narration = {
            "audio_path": audio_path,
            "word_timestamps": word_timestamps,
            "segment_timestamps": segment_timestamps,
            "duration": round(measured_duration, 3),
            "pure_spoken_sec": round(pure_spoken_sec, 3),
            "inter_segment_silence_sec": round(sum(silence_durations_sec), 3),
            "measured_wpm_spoken": round(measured_wpm_spoken, 2) if measured_wpm_spoken else None,
            "voice_id": voice_id,
            "voice_key": voice_key if voice_id == VOICE_CATALOG.get(voice_key, {}).get("id") else None,
            "backend": backend,
            "tagged_text": plain_text,
            "tts_speed": float(speed),
            "batch_count": len(batch_audios),
        }

        logger.info(
            "Narration assembled: %.1fs total = %.1fs spoken + %.1fs silence "
            "(%d batch(es) from %d segment(s), %d words, WPM=%s, voice=%s, speed=%.2f)",
            measured_duration, pure_spoken_sec, sum(silence_durations_sec),
            len(batch_audios), len(segments), len(word_timestamps),
            f"{measured_wpm_spoken:.1f}" if measured_wpm_spoken else "n/a",
            voice_id[:12], float(speed),
        )

        success = True
        return result
    finally:
        if not success:
            if auto_tmpdir:
                import shutil as _shutil
                _shutil.rmtree(output_dir, ignore_errors=True)
            else:
                for p in created_files:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass


# ---------------------------------------------------------------------------
#  CLI shim — preserves AC-16 invocation
# ---------------------------------------------------------------------------
# Real CLI bodies live at ``promo.cli.list_voices`` / ``promo.cli.generate_narration``.
# This shim keeps ``python3 -m promo.core.narrate.tts_engine list`` working byte-
# identically (arsenal-externalization-contract AC-16 verify gate).

if __name__ == "__main__":
    import sys

    _USAGE = (
        "usage: python3 -m promo.core.narrate.tts_engine {list|generate} [args...]\n"
        "  list     — list available voices (no args)\n"
        "  generate — generate narration from a script JSON (--script-json …)\n"
    )

    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        sys.stderr.write(_USAGE)
        sys.exit(0 if len(sys.argv) >= 2 and sys.argv[1] in {"-h", "--help"} else 2)

    _sub = sys.argv[1]
    if _sub == "list":
        from promo.cli.list_voices import main as _list_main
        _list_main()
    elif _sub == "generate":
        from promo.cli.generate_narration import main as _generate_main
        _generate_main(sys.argv[2:])
    else:
        sys.stderr.write(f"unknown subcommand: {_sub!r}\n{_USAGE}")
        sys.exit(2)
