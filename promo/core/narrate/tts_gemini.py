"""Gemini 3.1 Flash TTS backend — single-segment generator.

Public API
----------
- :func:`_generate_segment_audio_gemini` — TTS one segment, return
  ``(duration_sec, word_timestamps)``. Same tuple shape returned by
  the ElevenLabs backend; the dispatcher consumes both without a branch.

Architecture notes
------------------
Primary / fallback model selection: spike-measured behavior (Sprint
TTS-Spike verdict memo §a). 3.1 Flash TTS is the top-ranked option on
Artificial Analysis TTS Elo (1211), but the preview channel can flip
404/403 when the preview quota exhausts. Fall back to 2.5 Flash TTS —
same API surface, same voice catalog, ~half the cost.

Gemini returns PCM 24kHz mono 16-bit raw bytes; we wrap into a WAV
container via stdlib ``wave``, then ffmpeg-encode to mp3 at 44.1kHz
mono to match the ElevenLabs output format so
``_ffmpeg_concat_mp3s`` streams the batches cleanly.

Word timestamps come from ``forced_aligner.align_words``
(torchaudio MMS_FA). Gemini's API does not return alignment — the
forced-align pass is load-bearing on this backend.

Gemini pause tags ([short/medium/long pause]) are NEVER sent (N1):
spike-measured σ of up to 719ms makes them unusable for declarative
timing. Declarative pauses stay the responsibility of
``_generate_silence_mp3`` stitched between batches.
"""

import logging
import os
import wave

import requests

from promo.core.narrate.tts_assembly import (
    _SILENCE_BITRATE,
    _SILENCE_CODEC,
    _ffprobe_duration,
    _run_ffmpeg,
)
from promo.core.narrate.tts_text_normalize import normalize_digits_to_words
from promo.core.model_adapters.registry import (
    GEMINI_TTS_API_BASE,
    GEMINI_TTS_FALLBACK_MODEL,
    GEMINI_TTS_PRIMARY_MODEL,
)
from promo.core.model_adapters.tts import generate_gemini_tts_pcm
from promo.core.schema import WordTimestamp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

GEMINI_PRIMARY_MODEL = GEMINI_TTS_PRIMARY_MODEL
GEMINI_FALLBACK_MODEL = GEMINI_TTS_FALLBACK_MODEL
GEMINI_API_BASE = GEMINI_TTS_API_BASE
_GEMINI_PCM_SAMPLE_RATE = 24000
_GEMINI_MP3_SAMPLE_RATE = 44100


# ---------------------------------------------------------------------------
#  HTTP client + fallback
# ---------------------------------------------------------------------------

def _gemini_tts_rest(text: str, model: str, voice: str) -> bytes:
    """POST to Gemini ``:generateContent`` with AUDIO response modality.

    Returns PCM 24kHz mono 16-bit raw bytes. Raises
    ``requests.HTTPError`` on non-2xx; the caller
    (``_gemini_tts_with_fallback``) discriminates on status code.
    """
    return generate_gemini_tts_pcm(text=text, model=model, voice=voice)


def _gemini_tts_with_fallback(text: str, voice: str) -> tuple[bytes, str]:
    """Try primary model, fall back to 2.5 Flash on HTTP 404/403.

    Other HTTP errors propagate so operators see the real cause instead
    of a silent degrade. Returns ``(pcm_bytes, model_id_used)``.
    """
    try:
        return _gemini_tts_rest(text, GEMINI_PRIMARY_MODEL, voice), GEMINI_PRIMARY_MODEL
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 403):
            logger.warning(
                "Gemini TTS primary %s returned %d; falling back to %s",
                GEMINI_PRIMARY_MODEL, status, GEMINI_FALLBACK_MODEL,
            )
            return (
                _gemini_tts_rest(text, GEMINI_FALLBACK_MODEL, voice),
                GEMINI_FALLBACK_MODEL,
            )
        raise


# ---------------------------------------------------------------------------
#  PCM → MP3
# ---------------------------------------------------------------------------

def _gemini_pcm_to_mp3(pcm_bytes: bytes, output_path: str) -> None:
    """Convert Gemini PCM (24kHz mono 16-bit) to mp3 at 44.1kHz mono.

    WAV-wraps via stdlib ``wave`` then re-encodes via ffmpeg to match
    the ElevenLabs output format. Matching the concat sample rate is
    load-bearing — mixed-rate streams cause the concat demuxer to
    re-encode with audible artifacts.
    """
    tmp_wav = output_path + ".pcm.wav"
    try:
        with wave.open(tmp_wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(_GEMINI_PCM_SAMPLE_RATE)
            wf.writeframes(pcm_bytes)
        _run_ffmpeg([
            "-i", tmp_wav,
            "-ar", str(_GEMINI_MP3_SAMPLE_RATE),
            "-ac", "1",
            "-acodec", _SILENCE_CODEC,
            "-b:a", _SILENCE_BITRATE,
            output_path,
        ])
    finally:
        try:
            os.unlink(tmp_wav)
        except OSError:
            pass


# ---------------------------------------------------------------------------
#  Segment generator (public)
# ---------------------------------------------------------------------------

def _generate_segment_audio_gemini(
    segment_text: str,
    voice_name: str,
    output_path: str,
    *,
    style_prompt: str = "",
) -> tuple[float, list[WordTimestamp]]:
    """TTS a single segment via Gemini. Returns (duration_sec, word_timestamps).

    The ``style_prompt`` (if non-empty) is prepended to the segment text
    as a directorial prefix (e.g. "Read at a confident pace:"). Gemini
    interprets this as a delivery directive and does NOT read it aloud,
    so we align only ``segment_text`` tokens — the audio content matches.

    Numeric tokens are spelled out via ``normalize_digits_to_words``
    before both the TTS call and the aligner so MMS_FA (letter-only
    vocab) can score them. The spelled-out form is what Gemini actually
    reads aloud, keeping audio-token parity.

    Word timestamps come from ``forced_aligner.align_words`` (MMS_FA).
    Runtime ~3s per batch on CPU after the first call populates the
    model cache.
    """
    from promo.core.narrate.forced_aligner import align_words

    spelled = normalize_digits_to_words(segment_text)
    api_text = (
        f"{style_prompt.strip()} {spelled}".strip()
        if style_prompt
        else spelled
    )
    pcm_bytes, model_used = _gemini_tts_with_fallback(api_text, voice_name)
    if len(pcm_bytes) < 512:
        raise RuntimeError(
            f"Gemini TTS audio payload suspiciously small ({len(pcm_bytes)} bytes)"
        )
    _gemini_pcm_to_mp3(pcm_bytes, output_path)
    duration = _ffprobe_duration(output_path)
    if duration <= 0:
        raise RuntimeError(f"Invalid Gemini segment duration {duration!r}")

    script_tokens = spelled.split()
    word_timestamps = align_words(output_path, script_tokens)
    logger.info(
        "Gemini TTS segment: %.2fs, %d words aligned via MMS_FA (model=%s)",
        duration, len(word_timestamps), model_used,
    )
    return duration, word_timestamps
