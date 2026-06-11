"""翻转二 B1 — beat planner unit tests.

The load-bearing property: planned beats ALWAYS satisfy the existing
validator (`_enforce_hard_constraint_and_enrich`) by construction —
tiling, word-index bounds, and (with ≥5s clips vs ≤4s beats) the
duration hard-constraint.
"""

import pytest

from promo.core.assign.beat_planner import beat_text, plan_beats

# P2 step 3: plan_beats has no defaults — bounds always come from the
# format card. Tests pin the production long-card values.
MAX_BEAT_SEC = 4.0
MIN_BEAT_SEC = 2.0


def _plan(script, word_timestamps, **kwargs):
    kwargs.setdefault("max_beat_sec", MAX_BEAT_SEC)
    kwargs.setdefault("min_beat_sec", MIN_BEAT_SEC)
    return plan_beats(script, word_timestamps, **kwargs)


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
    beats = _plan(script, wts)
    assert beats == [{"segment": 1, "start_word_idx": 0, "end_word_idx": 4}]


def test_long_segment_splits_into_bounded_beats():
    script = _script(" ".join(f"w{i}" for i in range(25)))
    wts = _uniform_words(25)  # 10.0s → expect ceil(10/4) = 3 beats
    beats = _plan(script, wts)
    assert len(beats) == 3
    for span in _beat_spans(beats, wts):
        assert span <= MAX_BEAT_SEC + 0.4  # word granularity slack


def test_beats_tile_every_segment_contiguously():
    script = _script(
        " ".join(f"a{i}" for i in range(12)),
        " ".join(f"b{i}" for i in range(20)),
    )
    wts = _uniform_words(32)
    beats = _plan(script, wts)
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
    beats = _plan(script, wts)
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
    beats = _plan(script, wts)
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
    beats = _plan(script, wts)
    assert beats == [{"segment": 1, "start_word_idx": 0, "end_word_idx": 7}]


def test_soft_floor_yields_to_hard_ceiling():
    # Trailing fragment is 1.2s but merging would make 4.8s > 4s ceiling
    # → the short beat survives (floor is soft, ceiling is not).
    tokens = [f"a{i}" for i in range(8)] + ["a8,"] + ["b0", "b1", "b2"]
    script = _script(" ".join(tokens))
    wts = _uniform_words(12, tokens=tokens)
    beats = _plan(script, wts)
    assert len(beats) == 2
    assert beats[1] == {"segment": 1, "start_word_idx": 9, "end_word_idx": 11}
    assert _beat_spans(beats, wts)[1] == pytest.approx(1.2)


def test_min_beat_validation():
    script = _script("a b c")
    wts = _uniform_words(3)
    with pytest.raises(ValueError, match="min_beat_sec"):
        _plan(script, wts, min_beat_sec=5.0)


def test_intersegment_silence_charged_to_last_beat_of_segment():
    # Segment 1: 5 words ending at 2.0s; segment 2 starts at 3.5s (1.5s
    # authored pause). Peek-ahead must charge the pause to segment 1's
    # last beat, exactly like the validator does.
    wts = _uniform_words(5) + _uniform_words(5, start=3.5)
    script = _script("a b c d e", "f g h i j")
    beats = _plan(script, wts)
    spans = _beat_spans(beats, wts)
    seg1_last_span = spans[len([b for b in beats if b["segment"] == 1]) - 1]
    assert seg1_last_span == pytest.approx(3.5 - 0.0, abs=1e-6)


def test_seatbelt_stretches_beats_to_fit_pool():
    script = _script(" ".join(f"w{i}" for i in range(40)))
    wts = _uniform_words(40)  # 16s → nominal 4 beats
    assert len(_plan(script, wts)) == 4
    beats = _plan(script, wts, max_beats=2)
    assert len(beats) == 2


def test_seatbelt_below_segment_count_raises():
    script = _script("a b", "c d", "e f")
    wts = _uniform_words(6)
    with pytest.raises(ValueError, match="segment count"):
        _plan(script, wts, max_beats=2)


def test_token_timestamp_mismatch_raises():
    script = _script("a b c")
    wts = _uniform_words(5)
    with pytest.raises(ValueError, match="alignment drift"):
        _plan(script, wts)


def test_empty_segment_raises():
    script = _script("a b", "   ")
    wts = _uniform_words(2)
    with pytest.raises(ValueError, match="no words"):
        _plan(script, wts)


def test_beat_text_joins_covered_words():
    tokens = ["Sunset", "views,", "private", "balconies"]
    script = _script(" ".join(tokens))
    wts = _uniform_words(4, tokens=tokens)
    beats = _plan(script, wts)
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
    beats = _plan(script, wts)
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


# --- 2026-06-10 review blockings -------------------------------------------


def test_seatbelt_reviewer_counterexample_7_1_1():
    """Blocking #1: segment windows [7,1,1]s with max_beats=3. A uniform
    stretch (total 9s / 3 = 3s ceiling) still hands the 7s segment
    ceil(7/3)=3 beats → 5 total; the original ×1 stretch gave 4. The
    allocation seatbelt must produce EXACTLY ≤ 3."""
    wts = (
        _uniform_words(14, word_sec=0.5)                 # seg1: 7.0s
        + _uniform_words(2, word_sec=0.5, start=7.0)     # seg2: 1.0s
        + _uniform_words(2, word_sec=0.5, start=8.0)     # seg3: 1.0s
    )
    script = _script(
        " ".join(f"a{i}" for i in range(14)),
        "b0 b1",
        "c0 c1",
    )
    assert len(_plan(script, wts)) > 3  # unconstrained plan is bigger
    beats = _plan(script, wts, max_beats=3)
    assert len(beats) <= 3
    # Tiling survives the seatbelt path.
    by_seg = {}
    for b in beats:
        by_seg.setdefault(b["segment"], []).append(b)
    assert by_seg[1][0]["start_word_idx"] == 0
    assert by_seg[1][-1]["end_word_idx"] == 13
    assert by_seg[2][0]["start_word_idx"] == 14
    assert by_seg[3][-1]["end_word_idx"] == 17


def test_overlong_beat_warns_loudly(caplog):
    """Blocking #3: a long authored pause (production allows ~7s) makes
    the segment's last beat exceed the ceiling with no word boundary to
    split at — legal output, but it must WARN, never pass silently."""
    import logging

    wts = _uniform_words(5) + _uniform_words(5, start=8.5)  # 6.5s pause gap
    script = _script("a b c d e", "f g h i j")
    with caplog.at_level(logging.WARNING, logger="promo.core.assign.beat_planner"):
        beats = _plan(script, wts)
    spans = _beat_spans(beats, wts)
    assert max(spans) > MAX_BEAT_SEC  # the over-ceiling beat exists
    assert any("exceed" in rec.message for rec in caplog.records)


def test_empty_inputs_raise():
    with pytest.raises(ValueError, match="no segments"):
        _plan({"segments": []}, _uniform_words(1))
    with pytest.raises(ValueError, match="word_timestamps is empty"):
        _plan(_script("a b"), [])


def test_fuzz_realistic_shapes_hold_invariants():
    """Both review rounds asked for real-shape bombardment: seeded-random
    narrations (varied word lengths, punctuation, pauses up to 7s,
    occasional max_beats) must always tile, respect max_beats, stay
    deterministic, and sail through the production validator given a
    big-enough pool of long clips."""
    import random

    from promo.core.assign.clip_assignment_validator import (
        _enforce_hard_constraint_and_enrich,
    )

    rng = random.Random(20260610)
    for _ in range(150):
        n_segments = rng.randint(1, 6)
        tokens_per_seg, texts, wts = [], [], []
        t = 0.0
        for s in range(n_segments):
            n_words = rng.randint(3, 40)
            toks = []
            for w in range(n_words):
                tok = f"s{s}w{w}"
                if rng.random() < 0.15:
                    tok += rng.choice([",", ".", "!", ";"])
                dur = rng.uniform(0.15, 0.6)
                wts.append({"word": tok, "start": round(t, 3),
                            "end": round(t + dur, 3)})
                t += dur
                toks.append(tok)
            texts.append(" ".join(toks))
            tokens_per_seg.append(n_words)
            t += rng.uniform(0.0, 7.0)  # authored pause, production-real
        script = _script(*texts)

        max_beats = None
        if rng.random() < 0.4:
            max_beats = rng.randint(n_segments, n_segments + 10)
        beats = _plan(script, wts, max_beats=max_beats)

        assert beats == _plan(script, wts, max_beats=max_beats)  # deterministic
        if max_beats is not None:
            assert len(beats) <= max_beats
        # Tiling: contiguous per segment, exact coverage.
        cursor = 0
        by_seg = {}
        for b in beats:
            by_seg.setdefault(b["segment"], []).append(b)
        for seg_pos, n_words in enumerate(tokens_per_seg, start=1):
            seg_beats = by_seg[seg_pos]
            assert seg_beats[0]["start_word_idx"] == cursor
            for prev, nxt in zip(seg_beats, seg_beats[1:]):
                assert nxt["start_word_idx"] == prev["end_word_idx"] + 1
            cursor += n_words
            assert seg_beats[-1]["end_word_idx"] == cursor - 1
        # Production validator accepts the plan with unique long clips.
        durations = {f"{i:04d}": 60.0 for i in range(1, len(beats) + 1)}
        raw = [
            {"segment": b["segment"], "clip_id": f"{i:04d}",
             "start_word_idx": b["start_word_idx"],
             "end_word_idx": b["end_word_idx"], "trim_start": 0.0}
            for i, b in enumerate(beats, start=1)
        ]
        enriched = _enforce_hard_constraint_and_enrich(raw, script, wts, durations)
        assert len(enriched) == len(beats)
