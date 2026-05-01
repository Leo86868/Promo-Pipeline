"""Forced aligner wrapping ``torchaudio.pipelines.MMS_FA``.

Sprint TTS-Migration Phase 3 — produces ``[{word, start, end}]`` word
timestamps for the Gemini TTS backend, matching the shape of ElevenLabs'
native ``normalized_alignment`` so downstream consumers (V2 caption
renderer, ``clip_assigner``) don't need a branch.

Only invoked on the Gemini backend path. ElevenLabs continues to use the
API's returned alignment (~10ms precision).

Precision (Sprint TTS-Spike supplemental #2): p95 = 44.6ms on the 99-word
test script vs ElevenLabs API ground truth, under the pipeline's 50ms
``HARD_CONSTRAINT_TOL_SEC`` budget.

Architecture
------------
MMS_FA is a wav2vec2-CTC forced aligner (Meta's MMS, arXiv:2305.13516).
Audio is resampled to 16kHz mono PCM via ffmpeg (contract AC5 — no
``torchcodec`` in requirements; torchaudio's load path now requires it,
so we bypass it). The WAV is then loaded via ``wave``/``numpy`` into a
``torch.Tensor`` and passed through the MMS model; per-character frame
indices are converted to seconds using the waveform-to-emission ratio.

The MMS_FA vocab is 26 lowercase letters + apostrophe + blank. Script
tokens are lowercased and stripped to ASCII before tokenization.

L-003 guard (AC7): each word's average CTC score is checked against
``_MIN_AVG_SCORE``. Real speech tokens score ≥ 0.8 on in-audio words;
OOV / garbage tokens score ≤ 0.5 even when forced to align against real
speech. Below-threshold tokens raise ``ForcedAlignmentError`` with the
offending token + script position — no silent contraction of the output
list.

L-002 guard (AC8): every ``subprocess.run`` call uses ``check=True``.

Runtime cost: first call caches the MMS_FA model at
``~/.cache/torch/hub/checkpoints/`` (~1.2GB); subsequent calls are
~3s for a 40s clip on CPU.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
import torch
import torchaudio

from promo.core.schema import WordTimestamp

from promo.core.errors import ForcedAlignmentError

logger = logging.getLogger(__name__)

_MMS_FA_SAMPLE_RATE = 16000

# CTC-score guard policy — warning-only for score-based issues.
#
# L-003 guard intent (from Sprint TTS-Spike audit): "unaligned tokens
# raise, no silent contraction of the output list." MMS_FA in practice
# never refuses to emit spans — even for truly unknown tokens it returns
# a (low-score, possibly zero-width) best-fit. Treating low scores as
# hard errors proved too fragile for real Gemini TTS:
#
#   - Short fixture OOV "xqzvvvt"   → score 0.14 (wrong match)
#   - Real Gemini TTS "of"           → score 0.000 (token was slurred into
#                                       neighbor; span has zero width)
#   - Real Gemini TTS "nine hundred" → scores 0.40-0.55 (fluent-speech
#                                       blend; tokens ARE in audio, just
#                                       with soft boundaries)
#
# Policy:
#   - ALWAYS return exactly len(script_tokens) timestamps. No silent
#     drop (L-003 intent preserved).
#   - RAISE on structural failures only: empty span list from MMS_FA
#     (never observed but defensive), or token normalizes to an empty
#     string before tokenization.
#   - WARN on low confidence and log the offending tokens so the
#     operator knows which caption spans to spot-check.
_LOW_QUALITY_WARN = 0.60   # warn when a token is below this (diagnostic only)
_MEDIAN_WARN = 0.70        # warn when the batch median is below this

_MMS_NORMALIZE_RE = re.compile(r"[^a-z']")

# Symbols Gemini/ElevenLabs TTS pronounce as words — map to spoken form
# BEFORE the a-z/apostrophe strip so forced alignment has something to
# match against. Without this step, a POI name like "Ocean Key Resort &
# Spa" surfaces "&" as a standalone script token that MMS_FA refuses
# (discovered during Sprint 12b's first Ocean Key end-to-end render).
_SYMBOL_SPOKEN_FORM = {
    "&": "and",
}


def _preprocess_token(token: str) -> str:
    """Lowercase and strip to MMS_FA's 27-symbol vocab (a-z + apostrophe).

    Returns empty string if nothing survives — caller treats empty as OOV.
    Known-spoken symbols (``&`` → ``and``) are expanded before the strip.
    """
    lowered = token.lower()
    for symbol, word in _SYMBOL_SPOKEN_FORM.items():
        if symbol in lowered:
            lowered = lowered.replace(symbol, word)
    return _MMS_NORMALIZE_RE.sub("", lowered)


def _resample_to_16k_mono_wav(audio_path: str, tmpdir: Path) -> Path:
    """Resample ``audio_path`` to 16kHz mono 16-bit PCM WAV via ffmpeg.

    Bypasses ``torchaudio.load`` (which in torchaudio ≥ 2.8 requires
    ``torchcodec``; contract AC5 forbids that dep).
    """
    wav_16k = tmpdir / "aligner_16k.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(audio_path),
         "-ar", str(_MMS_FA_SAMPLE_RATE),
         "-ac", "1",
         "-acodec", "pcm_s16le",
         str(wav_16k)],
        check=True,
        capture_output=True,
    )
    return wav_16k


def _load_wav_as_tensor(wav_path: Path) -> tuple[torch.Tensor, int]:
    """Load a 16-bit PCM WAV into a ``[1, samples]`` float32 tensor in [-1, 1]."""
    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        nchannels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        nframes = wf.getnframes()
        raw = wf.readframes(nframes)
    if sampwidth != 2:
        raise RuntimeError(
            f"forced_aligner expects 16-bit PCM WAV (sampwidth=2); "
            f"got sampwidth={sampwidth}"
        )
    arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if nchannels > 1:
        arr = arr.reshape(-1, nchannels).mean(axis=1)
    return torch.from_numpy(arr).unsqueeze(0), sr


def align_words(audio_path: str, script_tokens: list[str]) -> list[WordTimestamp]:
    """Align ``script_tokens`` to ``audio_path`` using torchaudio MMS_FA.

    Args:
        audio_path: Path to a mono or stereo audio file (any format ffmpeg
            reads). Internally resampled to 16kHz mono 16-bit PCM WAV.
        script_tokens: Ordered list of word tokens. Punctuation is allowed
            and stripped internally; the output preserves caller's original
            token form in the ``word`` field.

    Returns:
        A list of ``{word, start, end}`` dicts, same length and order as
        ``script_tokens``. ``start`` / ``end`` are in seconds.

    Raises:
        ValueError: if ``script_tokens`` is empty.
        ForcedAlignmentError: if any token normalizes to empty, if MMS_FA's
            aligner raises internally, or if MMS_FA returns an empty span
            list for any token (structural failure — never observed in
            practice but defensive). Score-based low confidence does NOT
            raise; it logs a warning and returns best-effort timestamps
            (see ``2026-04-20-l003-guard-warn-only.md`` decision memo).
        RuntimeError: if the WAV loader encounters a non-16-bit sample width.
    """
    if not script_tokens:
        raise ValueError("script_tokens is empty")

    normalized: list[str] = []
    for i, tok in enumerate(script_tokens):
        norm = _preprocess_token(tok)
        if not norm:
            raise ForcedAlignmentError(
                token=tok,
                position=i,
                reason="token normalizes to empty string (non-alphabetic)",
            )
        normalized.append(norm)

    tmpdir = Path(tempfile.mkdtemp(prefix="promo_align_"))
    try:
        wav_16k = _resample_to_16k_mono_wav(audio_path, tmpdir)
        waveform, sr = _load_wav_as_tensor(wav_16k)
        if sr != _MMS_FA_SAMPLE_RATE:
            raise RuntimeError(
                f"ffmpeg resample produced sr={sr}; expected {_MMS_FA_SAMPLE_RATE}"
            )

        bundle = torchaudio.pipelines.MMS_FA
        device = torch.device("cpu")
        model = bundle.get_model(with_star=False).to(device)
        tokenizer = bundle.get_tokenizer()
        aligner = bundle.get_aligner()

        with torch.inference_mode():
            emission, _ = model(waveform.to(device))
            try:
                token_spans = aligner(emission[0], tokenizer(normalized))
            except (RuntimeError, ValueError) as exc:
                raise ForcedAlignmentError(
                    token="<aligner-internal>",
                    position=-1,
                    reason=f"MMS_FA aligner raised: {exc}",
                ) from exc

        if len(token_spans) != len(normalized):
            raise ForcedAlignmentError(
                token="<count-mismatch>",
                position=-1,
                reason=(
                    f"MMS_FA returned {len(token_spans)} span-lists for "
                    f"{len(normalized)} tokens"
                ),
            )

        ratio = waveform.size(1) / emission.size(1)

        avg_scores: list[float] = []
        for spans in token_spans:
            if not spans:
                avg_scores.append(0.0)
                continue
            avg_scores.append(sum(s.score for s in spans) / len(spans))
        median_score = sorted(avg_scores)[len(avg_scores) // 2]

        out: list[WordTimestamp] = []
        low_tokens: list[tuple[int, str, float]] = []
        for i, (orig_token, spans, avg_score) in enumerate(
            zip(script_tokens, token_spans, avg_scores)
        ):
            if not spans:
                # Structural failure — MMS_FA declined to place this
                # token at all. Never observed in practice; raise so
                # any future occurrence surfaces rather than returning
                # an incomplete output list.
                raise ForcedAlignmentError(
                    token=orig_token,
                    position=i,
                    reason="MMS_FA returned empty span list",
                )
            if avg_score < _LOW_QUALITY_WARN:
                low_tokens.append((i, orig_token, avg_score))
            start_sec = spans[0].start * ratio / _MMS_FA_SAMPLE_RATE
            end_sec = spans[-1].end * ratio / _MMS_FA_SAMPLE_RATE
            # Inflate zero-width spans to 1ms (H-001, audit finding).
            # MMS_FA returns ``start == end`` for tokens Gemini slurred
            # into a neighbor; downstream ``_validate_word_timestamps``
            # rejects ``end <= start`` as malformed, which would crash
            # an otherwise-fine render. 1ms keeps captions from flashing
            # at zero duration while staying invisible to the viewer.
            if end_sec <= start_sec:
                end_sec = start_sec + 0.001
            out.append(WordTimestamp(
                word=orig_token,
                start=round(float(start_sec), 3),
                end=round(float(end_sec), 3),
            ))

        if low_tokens:
            sample = ", ".join(
                f"{t!r}@{i}:{s:.2f}" for i, t, s in low_tokens[:5]
            )
            logger.warning(
                "MMS_FA: %d/%d token(s) scored below %.2f (rough but "
                "present in audio) — first 5: %s. Timestamps still "
                "returned; operator should spot-check caption sync.",
                len(low_tokens), len(avg_scores), _LOW_QUALITY_WARN,
                sample,
            )
        if median_score < _MEDIAN_WARN:
            logger.warning(
                "MMS_FA median CTC score %.3f is below %.2f — overall "
                "alignment quality is low. Consider shorter batches or "
                "a different voice.",
                median_score, _MEDIAN_WARN,
            )
        logger.info(
            "MMS_FA aligned %d tokens against %.2fs audio "
            "(median=%.3f min=%.3f)",
            len(out), waveform.size(1) / sr,
            median_score, min(avg_scores),
        )
        return out
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
