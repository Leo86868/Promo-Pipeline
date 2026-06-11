"""翻转二 B1 — deterministic visual-beat planner (no LLM).

Replaces Gemini #2's phrase segmentation with pure timestamp math: split
each script segment's narration into "beats" targeting at most
``max_beat_sec`` of display time. With clip sources ≥5s and beats ≤4s the
duration hard-constraint that drives ``ClipAssignmentError`` (and the F3
script-regen / split-repair machinery) loses its normal failure mode.
NOT absolutely, though (2026-06-10 review blocking #3): a beat's span
includes the authored pause that follows it, and production allows
pauses up to ~7s — a 6.5s beat is legal output. Such beats are flagged
loudly (WARNING here, ``overlong_beats`` in packer provenance) so the
packer's need for a longer clip is visible instead of silent; there is
no F3 fallback on this path.

Splitting strategy (SEMANTIC-FIRST, 2026-06-10 review revision): cuts
follow the breath of the narration, not a metronome.

1. **Clauses first** — every punctuation boundary (word ending with
   ,.;:!?…—) starts a candidate beat, so slide changes land where the
   speaker pauses. Beat lengths vary naturally (1.8s, 3.2s, 2.6s…).
2. **Soft floor** — a fragment shorter than ``min_beat_sec`` merges with
   its neighbour, but ONLY when the merged span stays within the hard
   ceiling; when merging would breach it, the short beat survives (the
   floor is soft, the ceiling is not).
3. **Hard ceiling** — any beat still longer than ``max_beat_sec`` is
   force-split on an equal-time grid at word boundaries (interior
   punctuation, if any survived merging, attracts the cut within
   ``PUNCT_SNAP_SEC``).

Trade-off named by the review: semantic cuts yield MORE beats per video
than time-grid cuts, which burns clip inventory faster (each beat
consumes a unique clip; assets carry a 3-use platform cap). The packer's
provenance therefore records per-video beat/clip counts so the burn
rate is measurable in production data.

Span semantics are IDENTICAL to ``clip_assignment_validator``'s
peek-ahead formula (the single arbiter of the renderer-facing contract):
a beat's display span runs from its first word's start to the NEXT
beat's first word's start in emission order; the very last beat runs to
``word_timestamps[-1].end`` (narration end — the buffer up to
``target_duration_sec`` is renderer bridge territory, Sprint 10.5 C1.2).
Inter-segment authored silence is therefore naturally charged to the
last beat of each segment, exactly as the validator charges phrases.

Seatbelt (设计契约 #3, 2026-06-10; rewritten after review blocking #1):
the POI selection gate (50 + 10×extra active assets) already guarantees
pool ≫ beat count for selected batches; ``max_beats`` exists only for
hand-built batches that bypass the gate. A uniform length stretch is
NOT a guarantee — per-segment ceil rounding defeats it (counterexample:
segment windows [7,1,1]s, max_beats=3 → stretched ceiling 3s still
gives the 7s segment 2+ beats = 4 total). The seatbelt therefore
allocates the budget across segments explicitly (largest-remainder by
span, floor 1 each) and grid-splits each segment into exactly its
allocation, then fail-louds on a post-check that should be unreachable.

Word indices are GLOBAL into ``word_timestamps`` and segment word ranges
are derived from ``len(segment.text.split())`` — byte-for-byte the same
cumulative math as ``_enforce_hard_constraint_and_enrich``, so planned
beats always satisfy the validator's tiling invariant by construction.
"""

from __future__ import annotations

import logging
import math

from promo.core.schema import Script, WordTimestamp

logger = logging.getLogger(__name__)

# P2 step 3: the beat ceiling/floor are the format card's
# ``pacing.beat_max_sec`` / ``pacing.beat_min_sec`` (skeleton YAML),
# threaded in by the caller — no module defaults. Tuning rationale
# (clip-physics ceiling, clip-burn floor) lives on the card.

# How far (seconds) a punctuation boundary may sit from the ideal grid
# point and still win, when force-splitting an over-long beat.
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


def _grid_starts(
    lo: int,
    hi: int,
    t_lo: float,
    t_next: float,
    n: int,
    word_timestamps: list[WordTimestamp],
    punct_after: set[int],
) -> list[int]:
    """Split ``[lo, hi]`` into (at most) ``n`` beats on the equal-time
    grid; word granularity may yield fewer, never more."""
    starts = [lo]
    span = t_next - t_lo
    for k in range(1, n):
        grid_t = t_lo + span * k / n
        cand_lo = starts[-1] + 1
        cand_hi = hi - (n - k) + 1
        if cand_lo > cand_hi:
            break  # word granularity exhausted
        starts.append(
            _pick_boundary(grid_t, cand_lo, cand_hi, word_timestamps, punct_after)
        )
    return starts


def _plan_segment(
    lo: int,
    hi: int,
    t_lo: float,
    t_next: float,
    word_timestamps: list[WordTimestamp],
    punct_after: set[int],
    *,
    max_beat_sec: float,
    min_beat_sec: float,
) -> list[int]:
    """Return beat START word indices for one segment (semantic-first)."""
    def start_t(m: int) -> float:
        return float(word_timestamps[m]["start"])

    # 1. Clauses: a new candidate beat after every punctuation word.
    clause_starts = [lo] + [
        m for m in range(lo + 1, hi + 1) if (m - 1) in punct_after
    ]

    # 2. Soft-floor merge, left to right: a clause joins the open beat
    #    while the open beat is still under the floor AND the merge
    #    respects the ceiling.
    starts: list[int] = []
    for ci, cstart in enumerate(clause_starts):
        if starts:
            open_start_t = start_t(starts[-1])
            open_span = start_t(cstart) - open_start_t
            clause_end_t = (
                start_t(clause_starts[ci + 1])
                if ci + 1 < len(clause_starts)
                else t_next
            )
            if (
                open_span < min_beat_sec
                and clause_end_t - open_start_t <= max_beat_sec
            ):
                continue  # merge into the open beat
        starts.append(cstart)
    # Trailing fragment merges backward when the ceiling allows.
    if len(starts) >= 2:
        if (
            t_next - start_t(starts[-1]) < min_beat_sec
            and t_next - start_t(starts[-2]) <= max_beat_sec
        ):
            starts.pop()

    # 3. Hard ceiling: force-split any beat still over max_beat_sec on
    #    an equal-time grid (interior punctuation attracts the cut).
    final: list[int] = []
    for bi, bstart in enumerate(starts):
        b_next_idx = starts[bi + 1] if bi + 1 < len(starts) else None
        b_end_t = start_t(b_next_idx) if b_next_idx is not None else t_next
        b_hi = (b_next_idx - 1) if b_next_idx is not None else hi
        span = b_end_t - start_t(bstart)
        if span <= max_beat_sec:
            final.append(bstart)
            continue
        n = min(math.ceil(span / max_beat_sec), b_hi - bstart + 1)
        final.extend(
            _grid_starts(
                bstart, b_hi, start_t(bstart), b_end_t, n,
                word_timestamps, punct_after,
            )
        )
    return final


def plan_beats(
    script: Script,
    word_timestamps: list[WordTimestamp],
    *,
    max_beat_sec: float,
    min_beat_sec: float,
    max_beats: int | None = None,
) -> list[Beat]:
    """Split every segment into semantic-first beats of ≤ ``max_beat_sec``
    display time (soft floor ``min_beat_sec``). Both bounds come from
    the format card (``pacing.beat_max_sec`` / ``pacing.beat_min_sec``).

    Returns beats in emission order, tiling each segment contiguously
    (validator invariant by construction). ``max_beats`` is the adaptive
    seatbelt: when the semantic plan exceeds it, beat length stretches
    and the floor rises until the count fits.
    """
    if max_beat_sec <= 0:
        raise ValueError(f"max_beat_sec must be positive (got {max_beat_sec})")
    if not 0 < min_beat_sec < max_beat_sec:
        raise ValueError(
            f"min_beat_sec must be in (0, max_beat_sec) "
            f"(got {min_beat_sec} vs {max_beat_sec})"
        )
    if max_beats is not None and max_beats <= 0:
        raise ValueError(f"max_beats must be positive (got {max_beats})")
    if not script.get("segments"):
        raise ValueError("script has no segments — cannot plan beats")
    if not word_timestamps:
        raise ValueError("word_timestamps is empty — cannot plan beats")
    ranges = _segment_word_ranges(script, word_timestamps)
    if max_beats is not None and max_beats < len(ranges):
        raise ValueError(
            f"max_beats={max_beats} < segment count {len(ranges)} — "
            "cannot plan fewer than one beat per segment"
        )

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

    punct_after = {
        i for i, wt in enumerate(word_timestamps)
        if _is_punct_boundary(str(wt.get("word", "")))
    }

    def _plan_all(eff_max: float, eff_min: float) -> list[Beat]:
        beats: list[Beat] = []
        for seg_pos, ((lo, hi), (t_lo, t_next)) in enumerate(
            zip(ranges, seg_windows), start=1,
        ):
            starts = _plan_segment(
                lo, hi, t_lo, t_next, word_timestamps, punct_after,
                max_beat_sec=eff_max, min_beat_sec=eff_min,
            )
            for j, start_idx in enumerate(starts):
                end_idx = (starts[j + 1] - 1) if j + 1 < len(starts) else hi
                beats.append(Beat(
                    segment=seg_pos,
                    start_word_idx=start_idx,
                    end_word_idx=end_idx,
                ))
        return beats

    beats = _plan_all(max_beat_sec, min_beat_sec)
    if max_beats is not None and len(beats) > max_beats:
        # Seatbelt (review blocking #1 rewrite): explicit per-segment
        # budget allocation — largest remainder by span, floor 1 each —
        # then exact grid splits. A uniform length stretch cannot
        # guarantee the count (per-segment ceil rounding; see module
        # docstring counterexample [7,1,1]×3).
        spans_w = [t1 - t0 for t0, t1 in seg_windows]
        total = sum(spans_w)
        spare = max_beats - len(ranges)
        shares = [
            (spare * s / total) if total > 0 else 0.0 for s in spans_w
        ]
        alloc = [1 + int(share) for share in shares]
        leftover = max_beats - sum(alloc)
        if leftover > 0:
            by_remainder = sorted(
                range(len(shares)),
                key=lambda i: shares[i] - int(shares[i]),
                reverse=True,
            )
            for idx in by_remainder[:leftover]:
                alloc[idx] += 1
        beats = []
        for seg_pos, ((lo, hi), (t_lo, t_next), n) in enumerate(
            zip(ranges, seg_windows, alloc), start=1,
        ):
            starts = _grid_starts(
                lo, hi, t_lo, t_next, min(n, hi - lo + 1),
                word_timestamps, punct_after,
            )
            for j, start_idx in enumerate(starts):
                end_idx = (starts[j + 1] - 1) if j + 1 < len(starts) else hi
                beats.append(Beat(
                    segment=seg_pos,
                    start_word_idx=start_idx,
                    end_word_idx=end_idx,
                ))
        if len(beats) > max_beats:
            # Post-check is load-bearing: a silent overshoot starves the
            # packer's unique-clip pool downstream. Should be unreachable.
            raise RuntimeError(
                f"beat seatbelt failed: planned {len(beats)} beats for "
                f"max_beats={max_beats} — planner bug, refusing to continue"
            )

    # Review blocking #3: over-long beats are legal (a beat's span eats
    # the authored pause after it; production pauses reach ~7s) but must
    # be LOUD — there is no F3 fallback on this path, and the packer
    # needs a longer-than-usual clip to cover them.
    overlong: list[float] = []
    for i, beat in enumerate(beats):
        t0 = float(word_timestamps[int(beat["start_word_idx"])]["start"])
        t1 = (
            float(word_timestamps[int(beats[i + 1]["start_word_idx"])]["start"])
            if i + 1 < len(beats)
            else narration_end
        )
        if t1 - t0 > max_beat_sec + 0.05:
            overlong.append(t1 - t0)
    if overlong:
        logger.warning(
            "beat planner: %d beat(s) exceed the %.1fs ceiling (longest "
            "%.2fs) — long authored pauses or seatbelt stretch; the packer "
            "needs clips long enough to cover them",
            len(overlong), max_beat_sec, max(overlong),
        )
    return beats


def beat_text(beat: Beat, word_timestamps: list[WordTimestamp]) -> str:
    """The narration text a beat covers — the retrieval query for that
    beat (B2 embeds these in one batched call)."""
    return " ".join(
        str(word_timestamps[i].get("word", ""))
        for i in range(int(beat["start_word_idx"]), int(beat["end_word_idx"]) + 1)
    ).strip()
