"""Deterministic split-repair for duration-violating Gemini #2 assignments.

F3 stopgap (2026-06-09): when the hard-constraint enforcer rejects a
phrase because its ``display_span_sec`` exceeds the assigned clip's
usable footage, the pool often cannot offer ANY single clip that covers
the span (clip sources are ~5s/8s; an 8.4s phrase has no single-clip
solution). The pre-existing F3 policy answers that by regenerating the
ENTIRE script via Gemini #1 and re-running TTS — expensive and indirect
for what is purely a visual-timeline problem.

This module repairs the assignment instead: split the offending phrase
at the word boundary nearest its span midpoint and give the second half
an unused clip from the inventory. The narration audio is untouched —
phrase boundaries are word-index ranges, so the split only changes
where the VISUAL cuts, not what is spoken. The repaired raw list is
re-validated by ``_enforce_hard_constraint_and_enrich``; all of its
invariants (segment tiling, clip-id uniqueness, duration constraint)
are preserved by construction:

- tiling: ``[a..b]`` becomes ``[a..m-1] + [m..b]`` — still contiguous.
- uniqueness: the second half's clip is drawn from ids absent from the
  current assignment list.
- duration: both sub-spans are checked against their clips (same
  ``HARD_CONSTRAINT_TOL_SEC`` slack the enforcer applies) before the
  split is returned.

Replacement clips come from ``clip_durations`` (the FULL ffprobed pool,
not the retrieval-narrowed prompt subset) — consistent with the Sprint
18 F soft-hint contract, which already accepts assignments outside the
retrieved subset. Among fitting unused clips the tightest fit wins
(smallest sufficient duration, clip-id tie-break) so long clips stay in
reserve for renderer bridges and any further repairs.

Only genuine duration shortfalls are repairable: structural enforcer
raises (tiling, duplicates, missing inventory, malformed fields) carry
``required_span == 0.0`` and must propagate — :func:`attempt_split_repair`
returns ``None`` for anything it cannot fix and the caller re-raises.
"""

from __future__ import annotations

import logging

from promo.core.assign.clip_assignment_validator import (
    HARD_CONSTRAINT_TOL_SEC,
    _segment_phrase_layout,
)
from promo.core.errors import ClipAssignmentError
from promo.core.schema import ClipAssignment, WordTimestamp

logger = logging.getLogger(__name__)

# Ceiling on split-repairs per Gemini #2 response. Each repair fixes one
# violating phrase and re-validation surfaces the next, so this bounds a
# pathological response (every phrase over-long) without letting the
# repair loop spin; anything beyond falls through to the F3 script-regen
# retry exactly as before.
MAX_SPLIT_REPAIRS = 3


def attempt_split_repair(
    raw_assignments: list[ClipAssignment],
    exc: ClipAssignmentError,
    word_timestamps: list[WordTimestamp],
    clip_durations: dict[str, float],
) -> list[ClipAssignment] | None:
    """Try to repair the duration violation named by ``exc`` by splitting
    the offending phrase in two. Returns the repaired raw-assignment list
    (input order preserved, offending entry replaced by its two halves),
    or ``None`` when no repair exists — single-word phrase, negative
    trim, or no unused clip fits the second half at any split point.
    """
    if exc.required_span <= 0.0:
        return None  # structural raise, not a duration shortfall

    layout = _segment_phrase_layout(raw_assignments)
    phrases = layout.get(exc.segment_index)
    if not phrases or not (1 <= exc.phrase_index <= len(phrases)):
        return None
    entry = phrases[exc.phrase_index - 1]

    try:
        start_idx = int(entry["start_word_idx"])
        end_idx = int(entry["end_word_idx"])
        trim_start = float(entry.get("trim_start", 0.0))
    except (KeyError, TypeError, ValueError):
        return None
    if trim_start < 0 or end_idx <= start_idx:
        return None  # negative trim isn't fixed by splitting; 1 word can't split

    orig_norm = str(entry.get("clip_id", "")).zfill(4)
    if orig_norm not in clip_durations:
        return None
    usable_orig = float(clip_durations[orig_norm]) - trim_start

    used_ids = {str(e.get("clip_id", "")).zfill(4) for e in raw_assignments}

    # Mirror the enforcer's emission-order peek-ahead to find where this
    # phrase's span ends: the next flat entry's first word's start, or
    # narration_end for the very last phrase (Sprint 10.5 C1.2).
    flat: list[ClipAssignment] = []
    for seg_i in sorted(layout.keys()):
        flat.extend(layout[seg_i])
    pos = next(i for i, e in enumerate(flat) if e is entry)
    if pos + 1 < len(flat):
        try:
            next_start = int(flat[pos + 1]["start_word_idx"])
            span_end = float(word_timestamps[next_start]["start"])
        except (KeyError, TypeError, ValueError, IndexError):
            return None
    else:
        span_end = (
            float(word_timestamps[-1].get("end", 0.0)) if word_timestamps else 0.0
        )

    this_start_t = float(word_timestamps[start_idx]["start"])
    midpoint_t = this_start_t + (span_end - this_start_t) / 2.0

    # Candidate split words (second half starts at word m), nearest the
    # span midpoint first — both halves stay short and visually balanced.
    candidates = sorted(
        range(start_idx + 1, end_idx + 1),
        key=lambda m: abs(float(word_timestamps[m]["start"]) - midpoint_t),
    )
    for m in candidates:
        m_start_t = float(word_timestamps[m]["start"])
        half1 = m_start_t - this_start_t
        half2 = span_end - m_start_t
        if half1 > usable_orig + HARD_CONSTRAINT_TOL_SEC:
            continue
        fitting = sorted(
            (float(dur), cid)
            for cid, dur in clip_durations.items()
            if cid not in used_ids
            and float(dur) + HARD_CONSTRAINT_TOL_SEC >= half2
        )
        if not fitting:
            continue
        new_clip_id = fitting[0][1]

        first_half = dict(entry)
        first_half["end_word_idx"] = m - 1
        second_half = {
            "segment": exc.segment_index,
            "clip_id": new_clip_id,
            "start_word_idx": m,
            "end_word_idx": end_idx,
            "trim_start": 0.0,
        }
        repaired: list[ClipAssignment] = []
        for e in raw_assignments:
            if e is entry:
                repaired.append(first_half)  # type: ignore[arg-type]
                repaired.append(second_half)  # type: ignore[arg-type]
            else:
                repaired.append(e)
        logger.warning(
            "F3 split-repair: segment %d phrase %d (clip %s, span %.2fs > "
            "usable %.2fs) split at word %d — clip %s keeps %.2fs, clip %s "
            "covers %.2fs",
            exc.segment_index, exc.phrase_index, exc.clip_id,
            exc.required_span, exc.actual_max_usable, m,
            orig_norm, half1, new_clip_id, half2,
        )
        return repaired

    return None
