"""翻转二 B4 — packer house-rule tests."""

import pytest

from promo.core.assign.packer import pack_clips
from promo.core.assign.usage_windows import UsedWindow
from promo.core.errors import ClipAssignmentError


def _words(n, word_sec=0.4):
    return [
        {"word": f"w{i}", "start": round(i * word_sec, 3),
         "end": round((i + 1) * word_sec, 3)}
        for i in range(n)
    ]


def _beats_one_segment(*ranges):
    return [
        {"segment": 1, "start_word_idx": lo, "end_word_idx": hi}
        for lo, hi in ranges
    ]


# 10 words @0.4s = two beats of 2.0s/2.0s display span.
WTS = _words(10)
BEATS = _beats_one_segment((0, 4), (5, 9))


def _meta(clip_id, category="pool", phase=""):
    return {"id": clip_id, "category": category,
            "scene_description": "x", "dominant_motion_phase": phase}


def test_happy_path_picks_top_candidates():
    rankings = [
        [("0001", 0.9), ("0002", 0.8)],
        [("0002", 0.85), ("0001", 0.7)],
    ]
    durations = {"0001": 5.0, "0002": 5.0}
    meta = [_meta("0001", "pool"), _meta("0002", "room")]
    assignments, prov = pack_clips(
        BEATS, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
    )
    assert [a["clip_id"] for a in assignments] == ["0001", "0002"]
    assert all(a["trim_start"] == 0.0 for a in assignments)
    assert prov["assigner"] == "packer"
    assert prov["window_exhausted_beats"] == []


def test_rule1_no_reuse_within_video():
    rankings = [
        [("0001", 0.9), ("0002", 0.8)],
        [("0001", 0.95), ("0002", 0.5)],  # top pick already used → next
    ]
    durations = {"0001": 5.0, "0002": 5.0}
    meta = [_meta("0001", "pool"), _meta("0002", "room")]
    assignments, _ = pack_clips(
        BEATS, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
    )
    assert [a["clip_id"] for a in assignments] == ["0001", "0002"]


def test_rule2_skips_clips_too_short_for_span():
    rankings = [
        [("0001", 0.9), ("0002", 0.8)],
        [("0003", 0.9), ("0002", 0.8)],
    ]
    durations = {"0001": 5.0, "0002": 5.0, "0003": 1.0}  # 0003 < 2.0s span
    meta = [_meta("0001"), _meta("0002", "room"), _meta("0003", "spa")]
    assignments, _ = pack_clips(
        BEATS, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
    )
    assert [a["clip_id"] for a in assignments] == ["0001", "0002"]


def test_rule3_window_rotation_picks_unused_window():
    # Clip 0001's seconds [0, 2.5) were shown before → trim lands at 2.5.
    rankings = [[("0001", 0.9)], [("0002", 0.8)]]
    durations = {"0001": 5.0, "0002": 5.0}
    meta = [_meta("0001"), _meta("0002", "room")]
    assignments, prov = pack_clips(
        BEATS, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
        used_windows={"asset_1": [UsedWindow(0.0, 2.5)]},
        clip_to_asset={"0001": "asset_1"},
    )
    assert assignments[0]["trim_start"] == 2.5
    assert prov["window_exhausted_beats"] == []


def test_rule3_prefers_fresh_candidate_over_exhausted_top_pick():
    # 0001 fully shown; 0002 fresh → packer steps down the ranking.
    rankings = [[("0001", 0.9), ("0002", 0.8)], [("0003", 0.9)]]
    durations = {"0001": 5.0, "0002": 5.0, "0003": 5.0}
    meta = [_meta("0001"), _meta("0002", "room"), _meta("0003", "spa")]
    assignments, prov = pack_clips(
        BEATS, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
        used_windows={"asset_1": [UsedWindow(0.0, 5.0)]},
        clip_to_asset={"0001": "asset_1"},
    )
    assert assignments[0]["clip_id"] == "0002"
    assert prov["window_exhausted_beats"] == []


def test_rule3_fallback_least_overlap_when_all_exhausted():
    # Sole candidate fully shown → least-overlap fallback, flagged.
    rankings = [[("0001", 0.9)], [("0002", 0.8)]]
    durations = {"0001": 5.0, "0002": 5.0}
    meta = [_meta("0001"), _meta("0002", "room")]
    assignments, prov = pack_clips(
        BEATS, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
        used_windows={"asset_1": [UsedWindow(0.0, 5.0)]},
        clip_to_asset={"0001": "asset_1"},
    )
    assert assignments[0]["clip_id"] == "0001"
    assert prov["window_exhausted_beats"] == [0]


def test_rule4_adjacency_avoids_back_to_back_category():
    # Beat 2's top pick shares beat 1's category; runner-up differs.
    rankings = [
        [("0001", 0.9)],
        [("0002", 0.9), ("0003", 0.85)],
    ]
    durations = {"0001": 5.0, "0002": 5.0, "0003": 5.0}
    meta = [_meta("0001", "pool"), _meta("0002", "pool"), _meta("0003", "room")]
    assignments, prov = pack_clips(
        BEATS, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
    )
    assert [a["clip_id"] for a in assignments] == ["0001", "0003"]
    assert prov["adjacency_relaxed_beats"] == []


def test_rule4_relaxes_when_only_same_category_remains():
    rankings = [
        [("0001", 0.9)],
        [("0002", 0.9)],  # same category, no alternative
    ]
    durations = {"0001": 5.0, "0002": 5.0}
    meta = [_meta("0001", "pool"), _meta("0002", "pool")]
    assignments, prov = pack_clips(
        BEATS, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
    )
    assert [a["clip_id"] for a in assignments] == ["0001", "0002"]
    assert prov["adjacency_relaxed_beats"] == [1]


def test_rule5_motion_phase_places_trim_in_window():
    # "late" phase pushes trim toward the free window's end (5 − 2 = 3.0).
    rankings = [[("0001", 0.9)], [("0002", 0.8)]]
    durations = {"0001": 5.0, "0002": 5.0}
    meta = [_meta("0001", phase="late_action"), _meta("0002", "room")]
    assignments, _ = pack_clips(
        BEATS, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
    )
    assert assignments[0]["trim_start"] == 3.0


def test_no_coverable_candidate_raises():
    rankings = [[("0001", 0.9)], [("0001", 0.9)]]  # only one clip exists
    durations = {"0001": 5.0}
    with pytest.raises(ClipAssignmentError):
        pack_clips(
            BEATS, rankings, word_timestamps=WTS,
            clip_durations=durations, clip_metadata=[_meta("0001")],
        )


def test_packed_output_passes_production_validator():
    """Contract test: planner beats + packer picks sail through
    `_enforce_hard_constraint_and_enrich` untouched."""
    from promo.core.assign.beat_planner import plan_beats
    from promo.core.assign.clip_assignment_validator import (
        _enforce_hard_constraint_and_enrich,
    )

    script = {"segments": [
        {"text": " ".join(f"a{i}" for i in range(18)), "pause_weight": 1},
        {"text": " ".join(f"b{i}" for i in range(22)), "pause_weight": 1},
    ]}
    wts = _words(40)
    beats = plan_beats(script, wts)
    pool = [f"{i:04d}" for i in range(1, len(beats) + 3)]
    durations = {cid: 6.0 for cid in pool}
    meta = [_meta(cid, category=f"cat{i % 3}") for i, cid in enumerate(pool)]
    rankings = [
        [(cid, 1.0 - 0.01 * j) for j, cid in enumerate(pool)] for _ in beats
    ]
    raw, prov = pack_clips(
        beats, rankings, word_timestamps=wts,
        clip_durations=durations, clip_metadata=meta,
        used_windows={"asset_7": [UsedWindow(0.0, 3.0)]},
        clip_to_asset={pool[0]: "asset_7"},
    )
    enriched = _enforce_hard_constraint_and_enrich(raw, script, wts, durations)
    assert len(enriched) == len(beats)
    assert len(prov["picks"]) == len(beats)
