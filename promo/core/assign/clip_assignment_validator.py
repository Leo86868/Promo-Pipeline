"""Hard-constraint validator for raw clip-assignment output.

The validator is the single arbiter of the packer's output against the
renderer-facing display-span contract: each
phrase's clip must cover the peek-ahead span (next-phrase first-word
start minus this-phrase first-word start, or narration_end for the last
phrase) up to ``HARD_CONSTRAINT_TOL_SEC`` of measurement slack.

Sprint 10.5 C1.2 + Sprint 10b amendment together fix the boundary
conditions; the per-phrase enforcement here is the runtime guardian for
both. Six audit fixes (L-001 / L-003 / L-009 / L-010 + duplicate-clip-id
normalisation + segment-tiling tilewise check) live inside the
enforcement loop — every branch in this file is a known-bug-class fix
documented in the project's audit log.
"""

from __future__ import annotations

from promo.core.errors import ClipAssignmentError
from promo.core.schema import (
    ClipAssignment,
    Script,
    WordTimestamp,
)


# Hard-constraint slack. The display-span ≤ usable-footage check allows up
# to 50 ms of tolerance because word_timestamps are measured to ~10 ms
# precision and clip source durations are ffprobed to ~1 ms; a tighter
# tolerance would make legitimate assignments fail on measurement noise.
HARD_CONSTRAINT_TOL_SEC = 0.05


# ---------------------------------------------------------------------------
#  Display-span math helpers
# ---------------------------------------------------------------------------

def _phrase_display_span_sec(
    this_start_word_idx: int,
    next_start_word_idx: int | None,
    word_timestamps: list[WordTimestamp],
) -> float:
    """Compute the visual duration a clip must cover for a phrase, using
    the peek-ahead formula that matches the renderer's bind-time span math
    (``_bind_clips_to_narration`` in ``remotion_renderer.py``) for
    middle phrases.

    The span is the gap from this phrase's first word's start to the NEXT
    phrase's first word's start in the delivered TTS timeline. Because
    ``word_timestamps`` is produced by TTS and back-allocated onto the
    concatenated audio, this gap naturally includes both inter-phrase TTS
    drift AND inter-segment authored silence — no separate pause term is
    needed (the pre-Sprint-10.5 formula's authored-silence kwarg was
    retired with this rewrite).

    For the VERY LAST phrase in emission order (``next_start_word_idx is
    None``), the span runs to ``word_timestamps[-1].end`` (narration end),
    NOT to ``final_display_end = max(target, narration_end)``. The
    target-extension buffer (``target_duration_sec − narration_end``) is
    explicitly renderer territory: the renderer extends the last clip
    past narration_end and — when clip source runs out — inserts bridge
    clips from the unused-clip pool to cover the remaining tail. Bridges
    are canonical silence-fill infrastructure per the Sprint 10b
    amendment. If the assigner tied the last-phrase hard-constraint to
    ``final_display_end`` instead, it would force the last clip to
    cover what the bridge mechanism is explicitly designed to handle,
    producing unrecoverable ``ClipAssignmentError`` raises for any
    narration whose final phrase starts more than ``pool_max`` seconds
    before ``target_duration_sec`` (Sprint 10.5 C1.2, 2026-04-18).
    """
    this_start = float(word_timestamps[this_start_word_idx]["start"])
    if next_start_word_idx is None:
        narration_end = (
            float(word_timestamps[-1].get("end", 0.0)) if word_timestamps else 0.0
        )
        span = narration_end - this_start
    else:
        span = float(word_timestamps[next_start_word_idx]["start"]) - this_start
    return max(0.0, span)


def _segment_phrase_layout(
    raw_assignments: list[ClipAssignment],
) -> dict[int, list[ClipAssignment]]:
    """Group raw assignments by segment index, preserving input order."""
    layout: dict[int, list[ClipAssignment]] = {}
    for entry in raw_assignments:
        layout.setdefault(int(entry["segment"]), []).append(entry)
    return layout


# ---------------------------------------------------------------------------
#  Hard-constraint enforcement
# ---------------------------------------------------------------------------

def _enforce_hard_constraint_and_enrich(
    raw_assignments: list[ClipAssignment],
    script: Script,
    word_timestamps: list[WordTimestamp],
    clip_durations: dict[str, float],
) -> list[ClipAssignment]:
    """Walk raw Gemini #2 assignments in-order, compute
    ``display_span_sec`` + ``source_duration_sec`` per phrase, and raise
    :class:`ClipAssignmentError` on the first constraint violation.

    Sprint 10.5 formula (peek-ahead, C1.2-amended): each phrase's span
    runs from THIS phrase's first word's start to the NEXT phrase's
    first word's start in emission order. Inter-segment silence is
    naturally included because the next segment's first word lands after
    the authored pause in the TTS-delivered timeline. The VERY LAST
    phrase's span runs to ``word_timestamps[-1].end`` (narration end) —
    NOT to ``final_display_end``. The target-extension buffer between
    ``narration_end`` and ``target_duration_sec`` is renderer territory:
    bridges (canonical silence-fill per the 10b amendment) fill any
    tail overflow when the last clip's source runs out past
    narration_end. Tying the last-phrase hard-constraint to the
    target-extension would force the assigner to treat
    ``FreezeWouldOccurError`` territory as ``ClipAssignmentError``
    territory, which breaks the 10b amendment.

    Returns the enriched list on success.
    """
    if not raw_assignments:
        raise ClipAssignmentError(
            segment_index=0,
            phrase_index=0,
            required_span=0.0,
            actual_max_usable=0.0,
            clip_id="<empty>",
        )

    script_segments = script.get("segments", [])
    segment_count = len(script_segments)
    layout = _segment_phrase_layout(raw_assignments)
    enriched: list[ClipAssignment] = []
    seen_clip_ids: set[str] = set()

    # Emission-order peek-ahead: flatten phrases across segments so the
    # last phrase of segment S sees the first phrase of segment S+1 as its
    # successor. Entry references stay live so downstream lookups work.
    sorted_seg_indices = sorted(layout.keys())
    flat_entries: list[ClipAssignment] = []
    for _seg_idx in sorted_seg_indices:
        flat_entries.extend(layout[_seg_idx])

    # L-001 fix: Gemini #2 must emit assignments for EVERY script segment.
    # Without this guard a truncated response silently produces a partial
    # assignments list that the renderer would consume downstream.
    missing_segments = [i + 1 for i in range(segment_count) if (i + 1) not in layout]
    if missing_segments:
        raise ClipAssignmentError(
            segment_index=missing_segments[0],
            phrase_index=0,
            required_span=0.0,
            actual_max_usable=0.0,
            clip_id=f"<no assignment for segments: {missing_segments}>",
        )

    # L-003 fix: precompute per-segment global word boundaries so we can
    # verify that phrases tile their segment end-to-end (the prompt's
    # stated invariant). Word indices here are into the GLOBAL
    # word_timestamps list — same as Gemini #2's output uses.
    segment_word_ranges: list[tuple[int, int]] = []
    cursor = 0
    for seg in script_segments:
        text = (seg.get("text") or "")
        n = len(text.split())
        if n == 0:
            # Defensive: a segment with zero words cannot tile anything.
            segment_word_ranges.append((cursor, cursor - 1))
        else:
            segment_word_ranges.append((cursor, cursor + n - 1))
        cursor += n

    emission_pos = 0
    for segment_index in sorted_seg_indices:
        phrases = layout[segment_index]
        # Zero-indexed position of THIS segment in the script segments list
        # (contract uses 1-based "segment" field).
        seg_pos = segment_index - 1
        if seg_pos < 0 or seg_pos >= segment_count:
            raise ClipAssignmentError(
                segment_index=segment_index,
                phrase_index=1,
                required_span=0.0,
                actual_max_usable=0.0,
                clip_id=str(phrases[0].get("clip_id", "<missing>")),
            )

        # L-003 fix: verify phrases tile this segment end-to-end with no
        # gaps or overlaps (the Gemini #2 prompt's stated invariant). Word
        # indices must be contiguous: first phrase starts at segment_start,
        # each subsequent phrase starts at prev.end + 1, last phrase ends
        # at segment_end.
        seg_word_lo, seg_word_hi = segment_word_ranges[seg_pos]
        prev_end = seg_word_lo - 1  # so phrase 1 must start at seg_word_lo
        for phrase_i, entry in enumerate(phrases, start=1):
            try:
                pi_start = int(entry["start_word_idx"])
                pi_end = int(entry["end_word_idx"])
            except (KeyError, TypeError, ValueError):
                # Detailed per-field raise happens below; skip tiling check.
                break
            if pi_start != prev_end + 1:
                raise ClipAssignmentError(
                    segment_index=segment_index,
                    phrase_index=phrase_i,
                    required_span=0.0,
                    actual_max_usable=0.0,
                    clip_id=str(entry.get("clip_id", "<tiling>"))
                    + f" (expected start_word_idx {prev_end + 1}, got {pi_start})",
                )
            prev_end = pi_end
        if phrases and prev_end != seg_word_hi:
            raise ClipAssignmentError(
                segment_index=segment_index,
                phrase_index=len(phrases),
                required_span=0.0,
                actual_max_usable=0.0,
                clip_id=f"<tiling: last phrase ends at {prev_end}, "
                f"segment ends at {seg_word_hi}>",
            )

        for phrase_i, entry in enumerate(phrases, start=1):
            clip_id = str(entry.get("clip_id", ""))
            if not clip_id:
                raise ClipAssignmentError(
                    segment_index=segment_index,
                    phrase_index=phrase_i,
                    required_span=0.0,
                    actual_max_usable=0.0,
                    clip_id="<missing>",
                )
            # Normalize before dedup AND inventory lookup so Gemini
            # emitting "7" and "0007" for the same physical clip cannot
            # slip through the "each clip_id appears AT MOST ONCE"
            # invariant (Sprint 10b post-close audit-fix 2026-04-18,
            # logic-auditor L-002 + Codex re-review catch). zfill(4)
            # matches the canonical form the renderer + clip inventory
            # use (``compile_promo._step_prepare_clips`` keys
            # ``clip_durations`` via ``clip_paths`` which is built from
            # zero-padded filenames). Downstream ``clip_durations``
            # lookups must use ``clip_id_norm`` or a Gemini-emitted
            # unpadded ID will raise "missing from inventory" even
            # when the padded form IS present in the pool.
            clip_id_norm = clip_id.zfill(4)
            if clip_id_norm in seen_clip_ids:
                # Reuse across phrases is structurally disallowed.
                raise ClipAssignmentError(
                    segment_index=segment_index,
                    phrase_index=phrase_i,
                    required_span=0.0,
                    actual_max_usable=0.0,
                    clip_id=f"{clip_id} (duplicate)",
                )
            seen_clip_ids.add(clip_id_norm)

            if clip_id_norm not in clip_durations:
                raise ClipAssignmentError(
                    segment_index=segment_index,
                    phrase_index=phrase_i,
                    required_span=0.0,
                    actual_max_usable=0.0,
                    clip_id=f"{clip_id} (missing from inventory)",
                )

            try:
                start_idx = int(entry["start_word_idx"])
                end_idx = int(entry["end_word_idx"])
                trim_start = float(entry.get("trim_start", 0.0))
            except (KeyError, TypeError, ValueError) as exc:
                raise ClipAssignmentError(
                    segment_index=segment_index,
                    phrase_index=phrase_i,
                    required_span=0.0,
                    actual_max_usable=0.0,
                    clip_id=f"{clip_id} ({exc})",
                )

            if (
                start_idx < 0 or end_idx < start_idx
                or end_idx >= len(word_timestamps)
            ):
                raise ClipAssignmentError(
                    segment_index=segment_index,
                    phrase_index=phrase_i,
                    required_span=0.0,
                    actual_max_usable=0.0,
                    clip_id=f"{clip_id} (bad word indices [{start_idx},{end_idx}])",
                )

            # Peek-ahead across segment boundaries via the flat emission-order
            # list. The last phrase of segment S sees segment S+1's first
            # phrase's first word as its successor; the very last phrase of
            # the last segment passes None and uses ``word_timestamps[-1].end``
            # (narration_end) — C1.2 amendment; target-extension past
            # narration_end is renderer bridge territory.
            # Audit-fix L-010 (2026-04-18): assert the emission-order invariant
            # before indexing flat_entries, so a silent drift between the outer
            # loop's traversal and the flat list can't produce wrong peek-ahead.
            assert flat_entries[emission_pos] is entry, (
                f"emission_pos drift at segment {segment_index} phrase {phrase_i}"
            )
            if emission_pos + 1 < len(flat_entries):
                next_entry = flat_entries[emission_pos + 1]
                try:
                    next_start_idx: int | None = int(next_entry["start_word_idx"])
                except (KeyError, TypeError, ValueError) as exc:
                    # Audit-fix L-009 (2026-04-18): a malformed successor entry
                    # would silently promote this phrase to "last phrase" status
                    # and inflate its span via narration_end fallback. Raise
                    # instead so the structural error surfaces where it lives.
                    raise ClipAssignmentError(
                        segment_index=int(next_entry.get("segment", segment_index + 1)),
                        phrase_index=1,
                        required_span=0.0,
                        actual_max_usable=0.0,
                        clip_id=(
                            f"{next_entry.get('clip_id', '<missing>')} "
                            f"(malformed start_word_idx: {exc})"
                        ),
                    )
            else:
                next_start_idx = None
            emission_pos += 1

            display_span_sec = _phrase_display_span_sec(
                start_idx, next_start_idx, word_timestamps,
            )
            source_duration_sec = float(clip_durations[clip_id_norm])
            usable = source_duration_sec - trim_start

            if trim_start < 0 or (usable + HARD_CONSTRAINT_TOL_SEC) < display_span_sec:
                raise ClipAssignmentError(
                    segment_index=segment_index,
                    phrase_index=phrase_i,
                    required_span=display_span_sec,
                    actual_max_usable=usable,
                    clip_id=clip_id,
                )

            enriched.append(ClipAssignment(
                segment=segment_index,
                clip_id=clip_id,
                start_word_idx=start_idx,
                end_word_idx=end_idx,
                trim_start=trim_start,
                display_span_sec=display_span_sec,
                source_duration_sec=source_duration_sec,
            ))

    return enriched
