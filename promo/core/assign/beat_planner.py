"""翻转二 B1 — deterministic visual-beat planner (no LLM).

Replaces Gemini #2's phrase segmentation with pure timestamp math: split
each script segment's narration into "beats" of at most ``max_beat_sec``
of display time. Because clip sources are ≥5s and beats are ≤4s, the
duration hard-constraint that drives ``ClipAssignmentError`` (and the F3
script-regen / split-repair machinery) becomes structurally unsatisfiable
— any clip covers any beat.

Span semantics are IDENTICAL to ``clip_assignment_validator``'s
peek-ahead formula (the single arbiter of the renderer-facing contract):
a beat's display span runs from its first word's start to the NEXT
beat's first word's start in emission order; the very last beat runs to
``word_timestamps[-1].end`` (narration end — the buffer up to
``target_duration_sec`` is renderer bridge territory, Sprint 10.5 C1.2).
Inter-segment authored silence is therefore naturally charged to the
last beat of each segment, exactly as the validator charges phrases.

Splitting strategy per segment (deterministic):

1. ``n = ceil(segment_span / max_beat_sec)`` beats, capped at the
   segment's word count (cannot split below word granularity).
2. Ideal boundaries sit on the equal-time grid ``k * span / n``. For
   each grid point the planner picks the word boundary whose start time
   is nearest — preferring a PUNCTUATION boundary (word ending with
   ,.;:!?…—) within ``PUNCT_SNAP_SEC`` of the grid so cuts land on
   natural speech pauses when one is close enough.
3. Boundaries are strictly monotonic, so every beat keeps ≥1 word.

Seatbelt (设计契约 #3, 2026-06-10): the POI selection gate
(50 + 10×extra active assets) already guarantees pool ≫ beat count for
selected batches; ``max_beats`` exists only for hand-built batches that
bypass the gate. When the nominal plan would exceed it, the effective
beat length is stretched to ``total_span / max_beats`` so the count
always fits the pool.

Word indices are GLOBAL into ``word_timestamps`` and segment word ranges
are derived from ``len(segment.text.split())`` — byte-for-byte the same
cumulative math as ``_enforce_hard_constraint_and_enrich``, so planned
beats always satisfy the validator's tiling invariant by construction.
"""

from __future__ import annotations

import math

from promo.core.schema import Script, WordTimestamp

# Nominal beat ceiling. Clips are ≥5s; 4s keeps a ≥1s trim window free
# for the packer's source-window rotation even on the shortest clips.
DEFAULT_MAX_BEAT_SEC = 4.0

# How far (seconds) a punctuation boundary may sit from the ideal grid
# point and still win over the nearest plain word boundary.
PUNCT_SNAP_SEC = 0.6

_PUNCT_CHARS = (",", ".", ";", ":", "!", "?", "…", "—")
_CLOSERS = "\"'”’)»]"


class Beat(dict):
    """``{"segment", "start_word_idx", "end_word_idx"}`` — same word-index
    contract as ``ClipAssignment`` minus the clip fields the packer adds."""


def _segment_word_ranges(
    script: Script, word_timestamps: list[WordTimestamp],
) -> list[tuple[int, int]]:
    """Global ``(lo, hi)`` word range per segment — validator's cumulative
    math. Raises ``ValueError`` when the script's token count disagrees
    with the timestamp count (upstream alignment bug; fail loud here
    rather than emit beats the validator would reject)."""
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for seg_pos, seg in enumerate(script.get("segments", []), start=1):
        n = len((seg.get("text") or "").split())
        if n == 0:
            raise ValueError(f"segment {seg_pos} has no words — cannot plan beats")
        ranges.append((cursor, cursor + n - 1))
        cursor += n
    if cursor != len(word_timestamps):
        raise ValueError(
            f"script tokens ({cursor}) != word_timestamps ({len(word_timestamps)}) "
            "— alignment drift between script and TTS output"
        )
    return ranges


def _is_punct_boundary(token: str) -> bool:
    return token.rstrip(_CLOSERS).endswith(_PUNCT_CHARS)


def _pick_boundary(
    grid_t: float,
    lo: int,
    hi: int,
    word_timestamps: list[WordTimestamp],
    punct_after: set[int],
) -> int:
    """Pick the next-beat start word ``m`` in ``[lo, hi]`` for an ideal
    boundary time ``grid_t``: nearest punctuation boundary within
    ``PUNCT_SNAP_SEC``, else the nearest word boundary outright."""
    def dist(m: int) -> float:
        return abs(float(word_timestamps[m]["start"]) - grid_t)

    nearest = min(range(lo, hi + 1), key=dist)
    punct_candidates = [m for m in range(lo, hi + 1) if (m - 1) in punct_after]
    if punct_candidates:
        best_punct = min(punct_candidates, key=dist)
        if dist(best_punct) <= dist(nearest) + PUNCT_SNAP_SEC:
            return best_punct
    return nearest


def plan_beats(
    script: Script,
    word_timestamps: list[WordTimestamp],
    *,
    max_beat_sec: float = DEFAULT_MAX_BEAT_SEC,
    max_beats: int | None = None,
) -> list[Beat]:
    """Split every segment into beats of ≤ ``max_beat_sec`` display time.

    Returns beats in emission order, tiling each segment contiguously
    (validator invariant by construction). ``max_beats`` is the adaptive
    seatbelt: when the nominal plan would exceed it, beats are stretched
    uniformly so the total count fits.
    """
    if max_beat_sec <= 0:
        raise ValueError(f"max_beat_sec must be positive (got {max_beat_sec})")
    if max_beats is not None and max_beats <= 0:
        raise ValueError(f"max_beats must be positive (got {max_beats})")
    ranges = _segment_word_ranges(script, word_timestamps)

    narration_end = float(word_timestamps[-1].get("end", 0.0))
    # Segment time window: first word's start → next segment's first
    # word's start (last segment → narration_end). Same peek-ahead the
    # validator applies across segment boundaries.
    seg_windows: list[tuple[float, float]] = []
    for i, (lo, _hi) in enumerate(ranges):
        t_lo = float(word_timestamps[lo]["start"])
        if i + 1 < len(ranges):
            t_next = float(word_timestamps[ranges[i + 1][0]]["start"])
        else:
            t_next = narration_end
        seg_windows.append((t_lo, max(t_lo, t_next)))

    effective_max = float(max_beat_sec)
    if max_beats is not None:
        total_span = sum(t1 - t0 for t0, t1 in seg_windows)
        # Each segment needs ≥1 beat regardless of length, so the
        # stretchable budget is what remains after that floor.
        spare = max_beats - len(ranges)
        if spare < 0:
            raise ValueError(
                f"max_beats={max_beats} < segment count {len(ranges)} — "
                "cannot plan fewer than one beat per segment"
            )
        nominal = sum(
            max(1, math.ceil((t1 - t0) / effective_max)) for t0, t1 in seg_windows
        )
        if nominal > max_beats:
            effective_max = max(effective_max, total_span / max(1, max_beats))

    punct_after = {
        i for i, wt in enumerate(word_timestamps)
        if _is_punct_boundary(str(wt.get("word", "")))
    }

    beats: list[Beat] = []
    for seg_pos, ((lo, hi), (t_lo, t_next)) in enumerate(
        zip(ranges, seg_windows), start=1,
    ):
        span = t_next - t_lo
        word_count = hi - lo + 1
        n = max(1, math.ceil(span / effective_max)) if span > 0 else 1
        n = min(n, word_count)

        starts = [lo]
        for k in range(1, n):
            grid_t = t_lo + span * k / n
            # Next beat must start after the previous start and leave at
            # least (n - k) words for the remaining beats.
            cand_lo = starts[-1] + 1
            cand_hi = hi - (n - k) + 1
            if cand_lo > cand_hi:
                break  # word granularity exhausted; emit fewer beats
            starts.append(
                _pick_boundary(grid_t, cand_lo, cand_hi, word_timestamps, punct_after)
            )

        for j, start_idx in enumerate(starts):
            end_idx = (starts[j + 1] - 1) if j + 1 < len(starts) else hi
            beats.append(Beat(
                segment=seg_pos,
                start_word_idx=start_idx,
                end_word_idx=end_idx,
            ))
    return beats


def beat_text(beat: Beat, word_timestamps: list[WordTimestamp]) -> str:
    """The narration text a beat covers — the retrieval query for that
    beat (B2 embeds these in one batched call)."""
    return " ".join(
        str(word_timestamps[i].get("word", ""))
        for i in range(int(beat["start_word_idx"]), int(beat["end_word_idx"]) + 1)
    ).strip()
