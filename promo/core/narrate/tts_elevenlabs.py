"""ElevenLabs TTS backend — single-segment generator.

Public API
----------
- :func:`_generate_segment_audio_elevenlabs` — TTS one segment, return
  ``(duration_sec, word_timestamps)``. Same tuple shape returned by
  the Gemini backend; the dispatcher consumes both without a branch.

Private helpers (HTTP, alignment, caption cleaning) stay underscored
and are imported as private names from ``tts_engine`` for the test
mock-patch surface (``mock.patch("promo.core.narrate.tts_engine.
_call_elevenlabs_with_timestamps", ...)``).

Module-level constants are immutable (model id, output format, API
base URL, voice-settings dict). ``VOICE_SETTINGS`` is the empirical
A/B-tested R2 baseline from Sprint 07; mutating it at runtime is not
supported.
"""

import base64
import logging
import re
from typing import Optional

import requests

from promo.core.model_adapters.registry import (
    ELEVENLABS_API_BASE as _ELEVENLABS_API_BASE,
    ELEVENLABS_MODEL_ID,
    ELEVENLABS_OUTPUT_FORMAT,
)
from promo.core.model_adapters.tts import call_elevenlabs_with_timestamps
from promo.core.schema import WordTimestamp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

# R2 voice parameter baseline — empirical A/B-tested in Sprint 07 planning.
# Single source of truth; applied to all ElevenLabs voices in Sprint 07.
VOICE_SETTINGS: dict = {
    "stability": 0.35,
    "similarity_boost": 0.75,
    "style": 0.25,
    "use_speaker_boost": True,
    "speed": 0.95,
}

MODEL_ID = ELEVENLABS_MODEL_ID
OUTPUT_FORMAT = ELEVENLABS_OUTPUT_FORMAT  # Starter-tier cap
ELEVENLABS_API_BASE = _ELEVENLABS_API_BASE


# ---------------------------------------------------------------------------
#  HTTP client
# ---------------------------------------------------------------------------

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
    voice_settings = dict(VOICE_SETTINGS)
    if speed is not None:
        voice_settings["speed"] = float(speed)
    logger.info(
        "Calling ElevenLabs with_timestamps (voice=%s, model=%s, format=%s)...",
        voice_id[:12], model_id, output_format,
    )
    return call_elevenlabs_with_timestamps(
        text=text,
        voice_id=voice_id,
        voice_settings=voice_settings,
        model_id=model_id,
        output_format=output_format,
        timeout=timeout,
        requests_post=requests.post,
    )


# ---------------------------------------------------------------------------
#  Caption / character → word grouping
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
#  Segment generator (public)
# ---------------------------------------------------------------------------

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
