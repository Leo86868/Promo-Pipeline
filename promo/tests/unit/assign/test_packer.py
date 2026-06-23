"""翻转二 B4 — packer house-rule tests."""

import pytest

from promo.core.assign.packer import pack_clips as _real_pack_clips
from promo.core.assign.usage_windows import UsedWindow
from promo.core.errors import ClipAssignmentError

# P2 step 3: pack_clips has no default beat ceiling — it comes from the
# format card. Tests pin the production long-card value.
MAX_BEAT_SEC = 4.0


def pack_clips(*args, **kwargs):
    kwargs.setdefault("max_beat_sec", MAX_BEAT_SEC)
    return _real_pack_clips(*args, **kwargs)


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
    # Clip-burn observability (2026-06-10 review).
    assert prov["beat_count"] == 2
    assert prov["unique_clip_count"] == 2


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
    beats = plan_beats(script, wts, max_beat_sec=MAX_BEAT_SEC, min_beat_sec=2.0)
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


# --- near-dup soft gate (phase 1, insertion point A) ---------------------
# Embeddings here are tiny hand-built vectors: 0001 and 0002 are IDENTICAL
# (cosine 1.0 = near-dup), 0003 is orthogonal (cosine 0 = diverse).
# Distinct categories so rule-4 adjacency never fires and the gate is
# isolated as the only thing that can change a pick.
_EMB_A = [1.0, 0.0, 0.0]
_EMB_DIVERSE = [0.0, 1.0, 0.0]


def _meta_emb(clip_id, emb, category):
    return {"id": clip_id, "category": category, "scene_description": "x",
            "dominant_motion_phase": "", "embedding": emb}


_GATE_RANKINGS = [
    [("0001", 0.90), ("0002", 0.85), ("0003", 0.70)],
    [("0001", 0.95), ("0002", 0.90), ("0003", 0.60)],  # 0001 used; 0002≈0001
]
_GATE_DURATIONS = {"0001": 5.0, "0002": 5.0, "0003": 5.0}
_GATE_META = [
    _meta_emb("0001", _EMB_A, "c1"),
    _meta_emb("0002", _EMB_A, "c2"),        # identical vector → near-dup of 0001
    _meta_emb("0003", _EMB_DIVERSE, "c3"),  # orthogonal → diverse
]


def test_gate_off_is_byte_identical_to_today():
    """threshold=None must reproduce today's picks AND add no provenance keys."""
    assignments, prov = pack_clips(
        BEATS, _GATE_RANKINGS, word_timestamps=WTS,
        clip_durations=_GATE_DURATIONS, clip_metadata=_GATE_META,
        near_dup_threshold=None,
    )
    # Without the gate, beat 2 takes the higher-ranked near-dup 0002.
    assert [a["clip_id"] for a in assignments] == ["0001", "0002"]
    # No gate-only provenance leaks into the serialized sidecar.
    assert "diversity_skipped_beats" not in prov
    assert "diversity_relaxed_beats" not in prov
    assert "near_dup_threshold" not in prov


def test_gate_on_skips_near_dup_for_diverse_clip():
    """Armed gate skips the near-dup 0002 and takes the diverse 0003."""
    assignments, prov = pack_clips(
        BEATS, _GATE_RANKINGS, word_timestamps=WTS,
        clip_durations=_GATE_DURATIONS, clip_metadata=_GATE_META,
        near_dup_threshold=0.9,
    )
    assert [a["clip_id"] for a in assignments] == ["0001", "0003"]
    assert prov["near_dup_threshold"] == 0.9
    assert 1 in prov["diversity_skipped_beats"]   # beat 2 skipped a near-dup
    assert prov["diversity_relaxed_beats"] == []  # a diverse clip existed


def test_gate_fail_soft_all_near_dup_still_full_video():
    """A POI of only mutually near-identical clips still yields a full video."""
    rankings = [[("0001", 0.9)], [("0002", 0.9)]]  # only choice for beat 2 ≈ 0001
    durations = {"0001": 5.0, "0002": 5.0}
    meta = [_meta_emb("0001", _EMB_A, "c1"), _meta_emb("0002", _EMB_A, "c2")]
    assignments, prov = pack_clips(
        BEATS, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
        near_dup_threshold=0.9,
    )
    # Never fails the video — relaxes and allows the near-dup.
    assert [a["clip_id"] for a in assignments] == ["0001", "0002"]
    assert prov["diversity_relaxed_beats"] == [1]
    assert prov["unique_clip_count"] == 2
