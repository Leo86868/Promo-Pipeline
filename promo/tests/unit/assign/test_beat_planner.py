"""翻转二 B1 — beat planner unit tests.

The load-bearing property: planned beats ALWAYS satisfy the existing
validator (`_enforce_hard_constraint_and_enrich`) by construction —
tiling, word-index bounds, and (with ≥5s clips vs ≤4s beats) the
duration hard-constraint.
"""

import pytest

from promo.core.assign.beat_planner import (
    DEFAULT_MAX_BEAT_SEC,
    beat_text,
    plan_beats,
)


def _uniform_words(n, *, word_sec=0.4, start=0.0, tokens=None):
    """n words back to back, word_sec each."""
    out = []
    t = start
    for i in range(n):
        out.append({
            "word": (tokens[i] if tokens else f"w{i}"),
            "start": round(t, 3),
            "end": round(t + word_sec, 3),
        })
        t += word_sec
    return out


def _script(*texts):
    return {"segments": [{"text": t, "pause_weight": 1} for t in texts]}


def _beat_spans(beats, word_timestamps):
    """Peek-ahead spans, validator semantics (last beat → narration end)."""
    spans = []
    for i, b in enumerate(beats):
        t0 = word_timestamps[b["start_word_idx"]]["start"]
        if i + 1 < len(beats):
            t1 = word_timestamps[beats[i + 1]["start_word_idx"]]["start"]
        else:
            t1 = word_timestamps[-1]["end"]
        spans.append(t1 - t0)
    return spans


def test_short_segment_is_a_single_beat():
    script = _script("a b c d e")
    wts = _uniform_words(5)  # 2.0s narration
    beats = plan_beats(script, wts)
    assert beats == [{"segment": 1, "start_word_idx": 0, "end_word_idx": 4}]


def test_long_segment_splits_into_bounded_beats():
    script = _script(" ".join(f"w{i}" for i in range(25)))
    wts = _uniform_words(25)  # 10.0s → expect ceil(10/4) = 3 beats
    beats = plan_beats(script, wts)
    assert len(beats) == 3
    for span in _beat_spans(beats, wts):
        assert span <= DEFAULT_MAX_BEAT_SEC + 0.4  # word granularity slack


def test_beats_tile_every_segment_contiguously():
    script = _script(
        " ".join(f"a{i}" for i in range(12)),
        " ".join(f"b{i}" for i in range(20)),
    )
    wts = _uniform_words(32)
    beats = plan_beats(script, wts)
    by_seg = {}
    for b in beats:
        by_seg.setdefault(b["segment"], []).append(b)
    assert by_seg[1][0]["start_word_idx"] == 0
    assert by_seg[1][-1]["end_word_idx"] == 11
    assert by_seg[2][0]["start_word_idx"] == 12
    assert by_seg[2][-1]["end_word_idx"] == 31
    for seg_beats in by_seg.values():
        for prev, nxt in zip(seg_beats, seg_beats[1:]):
            assert nxt["start_word_idx"] == prev["end_word_idx"] + 1


def test_semantic_cut_lands_exactly_on_punctuation():
    # Comma after word 8 → clause 1 = [0..8] (3.6s, a real beat), clause 2
    # = [9..19] (4.4s, over the ceiling) force-splits into two.
    tokens = [f"w{i}," if i == 8 else f"w{i}" for i in range(20)]
    script = _script(" ".join(tokens))
    wts = _uniform_words(20, tokens=tokens)
    beats = plan_beats(script, wts)
    assert beats[0]["end_word_idx"] == 8   # cut AT the comma, not near a grid
    assert beats[1]["start_word_idx"] == 9
    assert len(beats) == 3


def test_clause_rhythm_produces_varied_beat_lengths():
    # Three clauses of 2.4s / 3.2s / 2.4s — each becomes its own beat;
    # lengths follow the breath, not a uniform grid.
    tokens = (
        [f"a{i}" for i in range(5)] + ["a5,"]
        + [f"b{i}" for i in range(7)] + ["b7,"]
        + [f"c{i}" for i in range(6)]
    )
    script = _script(" ".join(tokens))
    wts = _uniform_words(20, tokens=tokens)
    beats = plan_beats(script, wts)
    assert [(b["start_word_idx"], b["end_word_idx"]) for b in beats] == [
        (0, 5), (6, 13), (14, 19),
    ]
    spans = _beat_spans(beats, wts)
    assert spans == pytest.approx([2.4, 3.2, 2.4])


def test_soft_floor_merges_short_fragment():
    # Clause 1 is 1.2s (< 2s floor); merging with clause 2 stays ≤4s →
    # one beat covers both.
    tokens = ["a0", "a1", "a2,"] + [f"b{i}" for i in range(5)]
    script = _script(" ".join(tokens))
    wts = _uniform_words(8, tokens=tokens)
    beats = plan_beats(script, wts)
    assert beats == [{"segment": 1, "start_word_idx": 0, "end_word_idx": 7}]


def test_soft_floor_yields_to_hard_ceiling():
    # Trailing fragment is 1.2s but merging would make 4.8s > 4s ceiling
    # → the short beat survives (floor is soft, ceiling is not).
    tokens = [f"a{i}" for i in range(8)] + ["a8,"] + ["b0", "b1", "b2"]
    script = _script(" ".join(tokens))
    wts = _uniform_words(12, tokens=tokens)
    beats = plan_beats(script, wts)
    assert len(beats) == 2
    assert beats[1] == {"segment": 1, "start_word_idx": 9, "end_word_idx": 11}
    assert _beat_spans(beats, wts)[1] == pytest.approx(1.2)


def test_min_beat_validation():
    script = _script("a b c")
    wts = _uniform_words(3)
    with pytest.raises(ValueError, match="min_beat_sec"):
        plan_beats(script, wts, min_beat_sec=5.0)


def test_intersegment_silence_charged_to_last_beat_of_segment():
    # Segment 1: 5 words ending at 2.0s; segment 2 starts at 3.5s (1.5s
    # authored pause). Peek-ahead must charge the pause to segment 1's
    # last beat, exactly like the validator does.
    wts = _uniform_words(5) + _uniform_words(5, start=3.5)
    script = _script("a b c d e", "f g h i j")
    beats = plan_beats(script, wts)
    spans = _beat_spans(beats, wts)
    seg1_last_span = spans[len([b for b in beats if b["segment"] == 1]) - 1]
    assert seg1_last_span == pytest.approx(3.5 - 0.0, abs=1e-6)


def test_seatbelt_stretches_beats_to_fit_pool():
    script = _script(" ".join(f"w{i}" for i in range(40)))
    wts = _uniform_words(40)  # 16s → nominal 4 beats
    assert len(plan_beats(script, wts)) == 4
    beats = plan_beats(script, wts, max_beats=2)
    assert len(beats) == 2


def test_seatbelt_below_segment_count_raises():
    script = _script("a b", "c d", "e f")
    wts = _uniform_words(6)
    with pytest.raises(ValueError, match="segment count"):
        plan_beats(script, wts, max_beats=2)


def test_token_timestamp_mismatch_raises():
    script = _script("a b c")
    wts = _uniform_words(5)
    with pytest.raises(ValueError, match="alignment drift"):
        plan_beats(script, wts)


def test_empty_segment_raises():
    script = _script("a b", "   ")
    wts = _uniform_words(2)
    with pytest.raises(ValueError, match="no words"):
        plan_beats(script, wts)


def test_beat_text_joins_covered_words():
    tokens = ["Sunset", "views,", "private", "balconies"]
    script = _script(" ".join(tokens))
    wts = _uniform_words(4, tokens=tokens)
    beats = plan_beats(script, wts)
    assert beat_text(beats[0], wts) == "Sunset views, private balconies"


def test_planned_beats_pass_the_production_validator():
    """The contract test: beats + unique ≥5s clips sail through
    `_enforce_hard_constraint_and_enrich` with zero violations."""
    from promo.core.assign.clip_assignment_validator import (
        _enforce_hard_constraint_and_enrich,
    )

    script = _script(
        " ".join(f"a{i}" for i in range(18)),
        " ".join(f"b{i}" for i in range(22)),
        " ".join(f"c{i}" for i in range(14)),
    )
    wts = _uniform_words(54)  # 21.6s narration across 3 segments
    beats = plan_beats(script, wts)
    assert len(beats) >= 6

    clip_durations = {f"{i:04d}": 5.0 for i in range(1, len(beats) + 1)}
    raw = [
        {
            "segment": b["segment"],
            "clip_id": f"{i:04d}",
            "start_word_idx": b["start_word_idx"],
            "end_word_idx": b["end_word_idx"],
            "trim_start": 0.0,
        }
        for i, b in enumerate(beats, start=1)
    ]
    enriched = _enforce_hard_constraint_and_enrich(raw, script, wts, clip_durations)
    assert len(enriched) == len(beats)
    assert all(e["display_span_sec"] <= 5.0 for e in enriched)
