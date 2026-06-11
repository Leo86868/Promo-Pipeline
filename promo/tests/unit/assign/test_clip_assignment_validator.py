"""Validator contract tests — input/output only (2026-06-11 rewrite).

The validator is the single arbiter of the renderer-facing assignment
contract, and since the Gemini #2 chain retired it guards the packer's
output. These tests replace the 1,900-line legacy suite that exercised
the same invariants THROUGH the retired assigner facade with
monkeypatched internals; here every test feeds raw assignments straight
in and asserts on what comes out (enriched rows or a structured raise).
"""

import pytest

from promo.core.assign.clip_assignment_validator import (
    HARD_CONSTRAINT_TOL_SEC,
    _enforce_hard_constraint_and_enrich,
)
from promo.core.errors import ClipAssignmentError


def _words(n, word_sec=0.5):
    return [
        {"word": f"w{i}", "start": round(i * word_sec, 3),
         "end": round((i + 1) * word_sec, 3)}
        for i in range(n)
    ]


def _script(*word_counts):
    return {"segments": [
        {"text": " ".join(f"s{si}w{i}" for i in range(n)), "pause_weight": 1}
        for si, n in enumerate(word_counts, start=1)
    ]}


def _entry(segment, clip_id, lo, hi, trim=0.0):
    return {"segment": segment, "clip_id": clip_id,
            "start_word_idx": lo, "end_word_idx": hi, "trim_start": trim}


WTS = _words(8)               # 4.0s narration
SCRIPT = _script(4, 4)        # two segments of 4 words
POOL = {"0001": 5.0, "0002": 5.0, "0003": 5.0}


def test_valid_assignment_is_enriched_with_span_and_source():
    out = _enforce_hard_constraint_and_enrich(
        [_entry(1, "0001", 0, 3), _entry(2, "0002", 4, 7)],
        SCRIPT, WTS, POOL,
    )
    assert [(e["clip_id"], e["segment"]) for e in out] == [("0001", 1), ("0002", 2)]
    # Peek-ahead span: phrase 1 runs to phrase 2's first word (2.0s);
    # last phrase runs to narration_end (wts[-1].end = 4.0 → 2.0s).
    assert out[0]["display_span_sec"] == pytest.approx(2.0)
    assert out[1]["display_span_sec"] == pytest.approx(2.0)
    assert out[0]["source_duration_sec"] == 5.0


def test_intersegment_pause_charged_to_preceding_phrase():
    wts = _words(4) + [
        {"word": f"x{i}", "start": 5.0 + i * 0.5, "end": 5.5 + i * 0.5}
        for i in range(4)
    ]  # 3s authored pause between segments
    out = _enforce_hard_constraint_and_enrich(
        [_entry(1, "0001", 0, 3), _entry(2, "0002", 4, 7)],
        SCRIPT, wts, {"0001": 6.0, "0002": 5.0},
    )
    assert out[0]["display_span_sec"] == pytest.approx(5.0)  # 2s words + 3s pause


def test_duration_violation_raises_with_actionable_numbers():
    with pytest.raises(ClipAssignmentError) as exc:
        _enforce_hard_constraint_and_enrich(
            [_entry(1, "0001", 0, 3, trim=4.0), _entry(2, "0002", 4, 7)],
            SCRIPT, WTS, POOL,
        )  # usable = 5.0 − 4.0 = 1.0 < 2.0 span
    assert exc.value.segment_index == 1
    assert exc.value.required_span == pytest.approx(2.0)
    assert exc.value.actual_max_usable == pytest.approx(1.0)


def test_duplicate_clip_rejected_even_across_id_spellings():
    # "7" and "0007" are the same physical clip — zfill normalization
    # must catch the duplicate (Sprint 10b audit fix, preserved).
    with pytest.raises(ClipAssignmentError) as exc:
        _enforce_hard_constraint_and_enrich(
            [_entry(1, "0007", 0, 3), _entry(2, "7", 4, 7)],
            SCRIPT, WTS, {"0007": 5.0},
        )
    assert "duplicate" in str(exc.value.clip_id)


def test_segment_tiling_gap_rejected():
    with pytest.raises(ClipAssignmentError):
        _enforce_hard_constraint_and_enrich(
            [_entry(1, "0001", 0, 2), _entry(2, "0002", 4, 7)],  # word 3 orphaned
            SCRIPT, WTS, POOL,
        )


def test_missing_segment_rejected():
    with pytest.raises(ClipAssignmentError) as exc:
        _enforce_hard_constraint_and_enrich(
            [_entry(1, "0001", 0, 3)],  # segment 2 absent
            SCRIPT, WTS, POOL,
        )
    assert "no assignment for segments" in str(exc.value.clip_id)


def test_unknown_clip_rejected():
    with pytest.raises(ClipAssignmentError) as exc:
        _enforce_hard_constraint_and_enrich(
            [_entry(1, "9999", 0, 3), _entry(2, "0002", 4, 7)],
            SCRIPT, WTS, POOL,
        )
    assert "missing from inventory" in str(exc.value.clip_id)


def test_out_of_range_word_indices_rejected():
    with pytest.raises(ClipAssignmentError):
        _enforce_hard_constraint_and_enrich(
            [_entry(1, "0001", 0, 3), _entry(2, "0002", 4, 99)],
            SCRIPT, WTS, POOL,
        )


def test_tolerance_absorbs_measurement_noise():
    # usable is short of the span by LESS than the 50ms tolerance → passes.
    pool = {"0001": 2.0 - HARD_CONSTRAINT_TOL_SEC / 2, "0002": 5.0}
    out = _enforce_hard_constraint_and_enrich(
        [_entry(1, "0001", 0, 3), _entry(2, "0002", 4, 7)],
        SCRIPT, WTS, pool,
    )
    assert len(out) == 2
