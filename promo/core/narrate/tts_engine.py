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

import base64
import json
import logging
import os
import re
import tempfile
import wave
from typing import Optional

import requests

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
# ``_SILENCE_*`` constants are re-imported because ``_gemini_pcm_to_mp3``
# (still in this module until C4) reads them directly. Drop in C4.
from promo.core.narrate.tts_assembly import (  # noqa: E402
    _SILENCE_BITRATE,
    _SILENCE_CODEC,
    _SILENCE_SAMPLE_RATE,
    _back_allocate_timestamps,
    _ffmpeg_concat_mp3s,
    _ffprobe_duration,
    _generate_silence_mp3,
    _run_ffmpeg,
    _validate_word_timestamps,
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

# R2 voice parameter baseline — empirical A/B-tested in Sprint 07 planning.
# Single source of truth; applied to all voices in Sprint 07.
VOICE_SETTINGS: dict = {
    "stability": 0.35,
    "similarity_boost": 0.75,
    "style": 0.25,
    "use_speaker_boost": True,
    "speed": 0.95,
}

MODEL_ID = "eleven_multilingual_v2"
OUTPUT_FORMAT = "mp3_44100_128"  # Starter-tier cap
ELEVENLABS_API_BASE = "https://api.elevenlabs.io"


# ---------------------------------------------------------------------------
#  ElevenLabs API
# ---------------------------------------------------------------------------

def _get_api_key() -> str:
    from promo.core.config import elevenlabs_api_key

    return elevenlabs_api_key()


def _call_elevenlabs_with_timestamps(
    text: str,
    voice_id: str,
    *,
    model_id: str = MODEL_ID,
    output_format: str = OUTPUT_FORMAT,
    timeout: int = 180,
    speed: Optional[float] = None,
) -> dict[str, object]:
    """POST to ``/v1/text-to-speech/{voice_id}/with-timestamps``.

    Returns the parsed JSON body. Raises ``RuntimeError`` on non-2xx responses
    or if the response body is not valid JSON.

    ``speed`` overrides ``VOICE_SETTINGS.speed`` when provided. Accepted
    ElevenLabs range is 0.7–1.2; default (None) keeps ``VOICE_SETTINGS.speed``.
    """
    api_key = _get_api_key()
    url = f"{ELEVENLABS_API_BASE}/v1/text-to-speech/{voice_id}/with-timestamps"
    voice_settings = dict(VOICE_SETTINGS)
    if speed is not None:
        voice_settings["speed"] = float(speed)
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": voice_settings,
    }
    params = {"output_format": output_format}
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    logger.info(
        "Calling ElevenLabs with_timestamps (voice=%s, model=%s, format=%s)...",
        voice_id[:12], model_id, output_format,
    )
    try:
        response = requests.post(
            url, params=params, json=payload, headers=headers, timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"ElevenLabs request failed: {exc}") from exc

    if response.status_code >= 400:
        body = response.text[:800]
        raise RuntimeError(
            f"ElevenLabs API error {response.status_code}: {body}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"ElevenLabs returned non-JSON response: {response.text[:300]!r}"
        ) from exc


_CAPTION_WORD_STRIP_RE = re.compile(
    r"""^[\"'“”‘’(\[{<«»—–\-]+|[\"'“”‘’)\]}>«»—–]+$""",
    re.VERBOSE,
)


def _clean_caption_word(word: str) -> str:
    """Strip surrounding quotes / brackets / dashes from a caption word.

    Keeps internal apostrophes ("can't"), sentence-ending punctuation
    (``.,!?;:``), and hyphens inside compound words ("rust-red"). Only the
    outermost wrapping characters are peeled — a word like ``"(can't)"`` →
    ``can't``; ``"rust-red,"`` → ``rust-red,``.
    """
    if not word:
        return word
    stripped = _CAPTION_WORD_STRIP_RE.sub("", word)
    return stripped or word


def _characters_to_words(
    characters: list[str],
    starts: list[float],
    ends: list[float],
) -> list[WordTimestamp]:
    """Group character-level timings into word-level timestamps.

    Whitespace characters delimit words. Non-spoken SSML markup (e.g. ``<break>``)
    should not appear in the alignment payload for properly rendered audio,
    but defensively: any character belonging to a ``<...>`` token is skipped.

    After grouping, each word is passed through ``_clean_caption_word`` to
    strip surrounding quotes / brackets — defensive against Gemini output
    like ``"can't"`` or ``(can't)`` bleeding through into captions.

    Returns a list of ``{word, start, end}`` dicts.
    """
    words: list[WordTimestamp] = []
    buf_chars: list[str] = []
    buf_start: Optional[float] = None
    buf_end: Optional[float] = None
    in_tag = 0  # depth of angle-bracket nesting (defensive)

    def flush():
        nonlocal buf_chars, buf_start, buf_end
        if buf_chars and buf_start is not None and buf_end is not None:
            word = "".join(buf_chars).strip()
            word = _clean_caption_word(word)
            if word:
                words.append(WordTimestamp(
                    word=word,
                    start=round(float(buf_start), 3),
                    end=round(float(buf_end), 3),
                ))
        buf_chars = []
        buf_start = None
        buf_end = None

    n = min(len(characters), len(starts), len(ends))
    for i in range(n):
        ch = characters[i]
        start = starts[i]
        end = ends[i]
        if ch == "<":
            flush()
            in_tag += 1
            continue
        if ch == ">" and in_tag > 0:
            in_tag -= 1
            continue
        if in_tag:
            continue
        if ch.isspace():
            flush()
            continue
        if buf_start is None:
            buf_start = start
        buf_end = end
        buf_chars.append(ch)
    flush()
    return words


def _generate_segment_audio_elevenlabs(
    segment_text: str,
    voice_id: str,
    output_path: str,
    *,
    speed: Optional[float] = None,
) -> tuple[float, list[WordTimestamp]]:
    """TTS a single segment via ElevenLabs. Returns (duration_sec, word_timestamps).

    No SSML breaks are inserted — this is a single-sentence-block call so
    ElevenLabs renders without the long-form break-compression behavior.
    Pauses between segments are applied as exact silence MP3s downstream.

    ``speed`` threads through to ``voice_settings.speed`` (Sprint 08.5 Item 5).

    Sprint TTS-Migration renamed ``_generate_segment_audio`` →
    ``_generate_segment_audio_elevenlabs`` to make the backend symmetric
    with ``_generate_segment_audio_gemini``.
    """
    response = _call_elevenlabs_with_timestamps(segment_text, voice_id, speed=speed)

    audio_b64 = response.get("audio_base64")
    if not audio_b64 or not isinstance(audio_b64, str):
        raise RuntimeError(
            "ElevenLabs response missing 'audio_base64' field (shape: "
            f"{list(response.keys())})"
        )
    try:
        audio_bytes = base64.b64decode(audio_b64)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"Failed to decode audio_base64: {exc}") from exc
    if len(audio_bytes) < 512:
        raise RuntimeError(
            f"ElevenLabs audio payload suspiciously small ({len(audio_bytes)} bytes)"
        )
    with open(output_path, "wb") as f:
        f.write(audio_bytes)

    alignment = response.get("normalized_alignment") or response.get("alignment")
    if not isinstance(alignment, dict):
        raise RuntimeError(
            "ElevenLabs alignment missing "
            f"(keys: {list(response.keys())})"
        )
    characters = alignment.get("characters")
    starts = alignment.get("character_start_times_seconds")
    ends = alignment.get("character_end_times_seconds")
    if not (
        isinstance(characters, list)
        and isinstance(starts, list)
        and isinstance(ends, list)
    ):
        raise RuntimeError(
            "ElevenLabs alignment missing character arrays "
            f"(keys: {list(alignment.keys())})"
        )
    if not (len(characters) == len(starts) == len(ends)):
        raise RuntimeError(
            "ElevenLabs alignment arrays length mismatch: "
            f"chars={len(characters)}, starts={len(starts)}, ends={len(ends)}"
        )

    word_timestamps = _characters_to_words(characters, starts, ends)
    duration = float(ends[-1]) if ends else 0.0
    if duration <= 0:
        raise RuntimeError(f"Invalid segment duration {duration!r}")
    return duration, word_timestamps


# ---------------------------------------------------------------------------
#  Gemini 3.1 Flash TTS
# ---------------------------------------------------------------------------
# Primary / fallback model selection: spike-measured behavior (Sprint
# TTS-Spike verdict memo §a). 3.1 Flash TTS is the top-ranked option on
# Artificial Analysis TTS Elo (1211), but the preview channel can flip
# 404/403 when the preview quota exhausts. Fall back to 2.5 Flash TTS —
# same API surface, same voice catalog, ~half the cost.
#
# Gemini returns PCM 24kHz mono 16-bit raw bytes; we wrap into a WAV
# container via stdlib ``wave``, then ffmpeg-encode to mp3 at 44.1kHz
# mono to match the ElevenLabs output format so
# ``_ffmpeg_concat_mp3s`` streams the batches cleanly.
#
# Word timestamps come from ``forced_aligner.align_words``
# (torchaudio MMS_FA). Gemini's API does not return alignment — the
# forced-align pass is load-bearing on this backend.
#
# Gemini pause tags ([short/medium/long pause]) are NEVER sent (N1):
# spike-measured σ of up to 719ms makes them unusable for declarative
# timing. Declarative pauses stay the responsibility of
# ``_generate_silence_mp3`` stitched between batches.

GEMINI_PRIMARY_MODEL = "gemini-3.1-flash-tts-preview"
GEMINI_FALLBACK_MODEL = "gemini-2.5-flash-preview-tts"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
_GEMINI_PCM_SAMPLE_RATE = 24000
_GEMINI_MP3_SAMPLE_RATE = 44100


def _get_gemini_api_key() -> str:
    from promo.core.config import gemini_api_key

    return gemini_api_key()


def _gemini_tts_rest(text: str, model: str, voice: str) -> bytes:
    """POST to Gemini ``:generateContent`` with AUDIO response modality.

    Returns PCM 24kHz mono 16-bit raw bytes. Raises
    ``requests.HTTPError`` on non-2xx; the caller
    (``_gemini_tts_with_fallback``) discriminates on status code.
    """
    url = f"{GEMINI_API_BASE}/models/{model}:generateContent"
    headers = {
        "x-goog-api-key": _get_gemini_api_key(),
        "Content-Type": "application/json",
    }
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice}
                }
            },
        },
    }
    response = requests.post(url, headers=headers, json=body, timeout=180)
    response.raise_for_status()
    payload = response.json()
    parts = payload["candidates"][0]["content"]["parts"]
    for part in parts:
        if "inlineData" in part:
            return base64.b64decode(part["inlineData"]["data"])
    raise RuntimeError(
        f"Gemini TTS response missing inlineData audio (keys: "
        f"{list(payload.get('candidates', [{}])[0].get('content', {}).keys())})"
    )


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

    Numeric tokens are spelled out via ``_normalize_digits_to_words``
    before both the TTS call and the aligner so MMS_FA (letter-only
    vocab) can score them. The spelled-out form is what Gemini actually
    reads aloud, keeping audio-token parity.

    Word timestamps come from ``forced_aligner.align_words`` (MMS_FA).
    Runtime ~3s per batch on CPU after the first call populates the
    model cache.
    """
    from promo.core.narrate.forced_aligner import align_words

    spelled = _normalize_digits_to_words(segment_text)
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

        _ffmpeg_concat_mp3s(concat_inputs, audio_path)
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
#  CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    from dotenv import load_dotenv
    load_dotenv()

    from promo.core.logging_config import configure_logging
    configure_logging()

    parser = argparse.ArgumentParser(
        description="Dual-backend TTS engine (ElevenLabs + Gemini 3.1 Flash) "
        "for hotel narration"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sp_gen = sub.add_parser("generate", help="Generate narration from script JSON")
    sp_gen.add_argument("--script-json", required=True, help="Path to script JSON")
    sp_gen.add_argument(
        "--voice", default="jarnathan",
        help="Voice key from VOICE_CATALOG (e.g. kore for Gemini, jarnathan/hope/heather "
        "for ElevenLabs) or a raw ElevenLabs voice_id",
    )
    sp_gen.add_argument("--output-dir", default=None, help="Output directory")

    sp_list = sub.add_parser("list", help="List available voices")
    del sp_list  # no further args

    args = parser.parse_args()

    if args.command == "list":
        print("\nVoice Catalog (ElevenLabs + Gemini 3.1 Flash):")
        print("=" * 60)
        for key, voice in VOICE_CATALOG.items():
            print(f"  {key}: {voice['name']} ({voice['gender']}, {voice['age']}, {voice['accent']})")
            print(f"    {voice['description']}")
            print(f"    voice_id: {voice['id']}")
            print()
        return

    if args.command == "generate":
        with open(args.script_json) as f:
            script = json.load(f)

        segments = script.get("segments", [])
        voice_key = args.voice
        voice_id = None
        if voice_key not in VOICE_CATALOG:
            # Treat as a raw voice_id.
            voice_id = voice_key
            voice_key = "custom"

        result = generate_narration(
            segments=segments,
            voice_id=voice_id,
            voice_key=voice_key if voice_id is None else "jarnathan",
            output_dir=args.output_dir,
        )

        print("\n" + "=" * 60)
        print("NARRATION GENERATED")
        print(f"Audio: {result['audio_path']}")
        print(f"Duration: {result['duration']:.1f}s")
        print(f"Words: {len(result['word_timestamps'])}")
        print(f"Segments: {len(result['segment_timestamps'])}")
        print("=" * 60)

        print("\nSegment timestamps:")
        for st in result["segment_timestamps"]:
            print(f"  Seg {st['segment']}: {st['start']:.2f}s - {st['end']:.2f}s ({st['duration']:.1f}s)")

        print("\nWord timestamps (first 20):")
        for wt in result["word_timestamps"][:20]:
            print(f"  [{wt['start']:.2f}-{wt['end']:.2f}] {wt['word']}")
        if len(result["word_timestamps"]) > 20:
            print(f"  ... ({len(result['word_timestamps']) - 20} more)")

        ts_path = os.path.join(os.path.dirname(result["audio_path"]), "timestamps.json")
        with open(ts_path, "w") as f:
            json.dump({
                "word_timestamps": result["word_timestamps"],
                "segment_timestamps": result["segment_timestamps"],
                "duration": result["duration"],
            }, f, indent=2)
        print(f"\nTimestamps saved: {ts_path}")


if __name__ == "__main__":
    main()
