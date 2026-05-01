"""TTS batch planner — merges consecutive weight=1 segments into one call.

Reads ``segments[i].pause_weight`` (narrative intent, Gemini-authored) and
produces a batch plan: a list of ``{segments, post_batch_silence_ms}`` dicts.

Consecutive segments whose joining gap has ``pause_weight == 1`` (STANDARD)
are placed in the SAME batch — ElevenLabs delivers the pause naturally
through its own prosody. Any gap with ``pause_weight >= 2`` terminates the
current batch; between batches, explicit ffmpeg silence of
``post_batch_silence_ms`` is inserted (the value is ``pause_after_ms`` on the
segment immediately before the boundary, populated by
``compute_pause_budget`` with weight≥2-only distribution).

The last segment's ``pause_weight`` is ignored (no gap after it). The final
batch's ``post_batch_silence_ms`` is always ``None``.

Sprint 08.5 context: Sprint 08 placed one ffmpeg silence between every
adjacent segment, which operator review found mechanical (~20 s of hard
silence per 65 s LONG variant). This planner concentrates explicit silence
on narratively meaningful breaks only.
"""

from __future__ import annotations

from typing import Any

from promo.core.schema import ScriptSegment


def plan_tts_batches(segments: list[ScriptSegment]) -> list[dict[str, Any]]:
    """Group ``segments`` into TTS batches driven by ``pause_weight``.

    Returns a list of ``{segments: [...], post_batch_silence_ms: int | None}``.
    The final batch's ``post_batch_silence_ms`` is ``None``. Non-final
    batches' ``post_batch_silence_ms`` equals the ``pause_after_ms`` value on
    the segment immediately before the batch boundary.

    Rules:
    - ``segments[i].pause_weight`` describes the gap AFTER segment ``i``.
    - For N segments, there are N-1 gaps (indices 0..N-2).
    - Consecutive segments joined by a ``pause_weight == 1`` gap share a batch.
    - A gap with ``pause_weight >= 2`` terminates the current batch.
    - Missing / invalid ``pause_weight`` is treated as ``1`` (most forgiving —
      the ElevenLabs merge covers standard beats without explicit silence).
    - The last segment's ``pause_weight`` is ignored.
    """
    if not segments:
        return []
    batches: list[dict] = []
    current: list[ScriptSegment] = []
    for i, seg in enumerate(segments):
        current.append(seg)
        is_last = (i == len(segments) - 1)
        if is_last:
            continue
        raw = seg.get("pause_weight")
        try:
            w = int(raw) if raw is not None else 1
        except (TypeError, ValueError):
            w = 1
        if w >= 2:
            batches.append({
                "segments": current,
                "post_batch_silence_ms": int(seg.get("pause_after_ms") or 0),
            })
            current = []
    batches.append({"segments": current, "post_batch_silence_ms": None})
    return batches
