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


# --- near-dup soft gate (VISUAL modality, insertion point A) -------------
# The gate keys off the whitened ``visual_embedding`` (NOT text). Vectors
# here are tiny hand-built: 0001 and 0002 are IDENTICAL (twins → cosine 1.0
# even after pool mean-centering), 0003 is orthogonal (diverse). Distinct
# categories so rule-4 adjacency never fires and the gate is isolated as the
# only thing that can change a pick.
_EMB_A = [1.0, 0.0, 0.0]
_EMB_DIVERSE = [0.0, 1.0, 0.0]


def _meta_emb(clip_id, emb, category, visual=None):
    """Clip metadata carrying a text ``embedding`` AND a ``visual_embedding``
    (defaults to the same vector). The gate reads the visual one; pass a
    distinct ``visual`` to prove text and visual diverge."""
    return {"id": clip_id, "category": category, "scene_description": "x",
            "dominant_motion_phase": "", "embedding": emb,
            "visual_embedding": emb if visual is None else visual}


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
    """When a beat's only coverable candidate is a visual twin, the gate
    relaxes (allows the near-dup) rather than failing the video."""
    rankings = [[("0001", 0.9)], [("0002", 0.9)]]  # beat 2's only choice ≈ 0001
    durations = {"0001": 5.0, "0002": 5.0, "0003": 5.0}
    # A diverse 0003 sits in the pool so whitening has signal (an all-identical
    # pool mean-centers to zero → uncomparable), but it is never offered to
    # beat 2 — whose sole candidate is the twin 0002.
    meta = [
        _meta_emb("0001", _EMB_A, "c1"),
        _meta_emb("0002", _EMB_A, "c2"),
        _meta_emb("0003", _EMB_DIVERSE, "c3"),
    ]
    assignments, prov = pack_clips(
        BEATS, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
        near_dup_threshold=0.9,
    )
    # Never fails the video — relaxes and allows the near-dup.
    assert [a["clip_id"] for a in assignments] == ["0001", "0002"]
    assert prov["diversity_relaxed_beats"] == [1]
    assert prov["unique_clip_count"] == 2


def test_gate_keys_off_visual_not_text_embedding():
    """A clip whose TEXT vector looks diverse but whose VISUAL vector twins an
    already-chosen clip is skipped — proving the gate judges visual, not text."""
    rankings = [
        [("0001", 0.90), ("0002", 0.85), ("0003", 0.70)],
        [("0001", 0.95), ("0002", 0.90), ("0003", 0.60)],
    ]
    durations = {"0001": 5.0, "0002": 5.0, "0003": 5.0}
    meta = [
        _meta_emb("0001", _EMB_A, "c1", visual=_EMB_A),
        # text says "diverse" but the visual vector is identical to 0001
        _meta_emb("0002", _EMB_DIVERSE, "c2", visual=_EMB_A),
        _meta_emb("0003", _EMB_A, "c3", visual=_EMB_DIVERSE),
    ]
    assignments, prov = pack_clips(
        BEATS, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
        near_dup_threshold=0.9,
    )
    # On text the gate would keep 0002 (cos 0 to 0001); on visual 0002 twins
    # 0001 → skipped for the diverse 0003.
    assert [a["clip_id"] for a in assignments] == ["0001", "0003"]
    assert 1 in prov["diversity_skipped_beats"]


def test_gate_fail_open_when_clip_lacks_visual_embedding():
    """A clip with no visual_embedding (pending/failed asset) is never blocked
    — the gate cannot compare it, so it fails open."""
    rankings = [[("0001", 0.9)], [("0002", 0.9), ("0003", 0.5)]]
    durations = {"0001": 5.0, "0002": 5.0, "0003": 5.0}
    meta = [
        _meta_emb("0001", _EMB_A, "c1", visual=_EMB_A),
        # 0002 has NO visual vector (pending) → uncomparable → fail-open,
        # even though its text vector twins the chosen 0001.
        {"id": "0002", "category": "c2", "scene_description": "x",
         "dominant_motion_phase": "", "embedding": _EMB_A},
        _meta_emb("0003", _EMB_DIVERSE, "c3", visual=_EMB_DIVERSE),
    ]
    assignments, prov = pack_clips(
        BEATS, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
        near_dup_threshold=0.9,
    )
    # 0002 taken despite ranking first and twinning 0001 — uncomparable.
    assert [a["clip_id"] for a in assignments] == ["0001", "0002"]
    assert prov["diversity_skipped_beats"] == []


def test_whiten_visual_pool_centers_and_normalizes():
    """Pool whitening = mean-center over the POI pool, then L2. Twins stay
    cosine 1.0; an all-identical pool has no signal → omitted (fail-open)."""
    import math

    from promo.core.assign.packer import (
        max_cosine_to_chosen,
        whiten_visual_pool,
    )

    units = whiten_visual_pool({
        "0001": {"visual_embedding": [1.0, 0.0, 0.0]},
        "0002": {"visual_embedding": [1.0, 0.0, 0.0]},
        "0003": {"visual_embedding": [0.0, 1.0, 0.0]},
    })
    # Twins remain cosine 1.0 after centering; each vector is unit-length.
    assert max_cosine_to_chosen(units["0001"], [units["0002"]]) == pytest.approx(1.0)
    for u in units.values():
        assert math.isclose(math.sqrt(sum(x * x for x in u)), 1.0)
    # Constant pool → every vector centers to zero → uncomparable → empty.
    assert whiten_visual_pool({
        "a": {"visual_embedding": [2.0, 2.0]},
        "b": {"visual_embedding": [2.0, 2.0]},
    }) == {}
    # No visual vectors at all → empty.
    assert whiten_visual_pool({"a": {"embedding": [1.0, 0.0]}}) == {}


# --- global assignment consolidation (global_assignment=True) -------------
# These pin the armed path: ① default-off byte-identical, ② the allocation
# fix (greedy strands a clip a later beat needed; global resolves it),
# ③ adjacency soft penalty, ④ near-dup folded as penalty, ⑤ relocated window
# resolution, ⑥ fail-loud on thin pool, ⑦ displaced-beat observability.


def test_global_off_is_byte_identical_to_greedy():
    """global_assignment=False must reproduce greedy picks AND add no armed-only
    provenance keys (default-off byte-identical pin)."""
    rankings = [
        [("0001", 0.9), ("0002", 0.8)],
        [("0002", 0.85), ("0001", 0.7)],
    ]
    durations = {"0001": 5.0, "0002": 5.0}
    meta = [_meta("0001", "pool"), _meta("0002", "room")]
    base_a, base_p = pack_clips(
        BEATS, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
    )
    off_a, off_p = pack_clips(
        BEATS, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
        global_assignment=False,
    )
    assert off_a == base_a
    assert off_p == base_p
    assert "global_assignment" not in off_p
    assert "displaced_beats" not in off_p


def test_global_resolves_greedy_allocation_miss():
    """The core fix. Beat 1 greedily grabs the shared best clip 0001; beat 2's
    ONLY good clip is also 0001, so greedy strands beat 2 on a poor clip. The
    global solver gives 0001 to the beat that needs it most and lifts total
    cosine. Mirrors the run3x3 'surf simulator' stranding."""
    beats = _beats_one_segment((0, 4), (5, 9))
    durations = {"0001": 5.0, "0002": 5.0}
    meta = [_meta("0001", "a"), _meta("0002", "b")]
    # Beat 1 slightly prefers 0001 (0.50 vs 0.45). Beat 2 STRONGLY needs 0001
    # (0.80) and 0002 is a poor match (0.10). Greedy beat 1 takes 0001 → beat 2
    # stranded on 0002 (0.10). Global: beat1←0002 (0.45), beat2←0001 (0.80).
    rankings = [
        [("0001", 0.50), ("0002", 0.45)],
        [("0001", 0.80), ("0002", 0.10)],
    ]
    greedy_a, _ = pack_clips(
        beats, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
    )
    assert [a["clip_id"] for a in greedy_a] == ["0001", "0002"]  # greedy strands

    glob_a, glob_p = pack_clips(
        beats, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
        global_assignment=True,
    )
    # Global trades beat 1 down 0.05 to lift beat 2 up 0.70 — maximizes total.
    assert [a["clip_id"] for a in glob_a] == ["0002", "0001"]
    assert glob_p["global_assignment"] is True
    # Beat 2 was the silent-displacement victim under greedy; global flags
    # nothing displaced because every beat now gets its best-feasible clip.
    assert glob_p["displaced_beats"] == []


def test_global_adjacency_soft_penalty_breaks_back_to_back():
    """Adjacency is a SOFT penalty in the cost matrix: when a same-category
    alternative costs only marginally more, the penalty flips the pick to a
    different category. With near-equal scores the penalty decides."""
    beats = _beats_one_segment((0, 4), (5, 9))
    durations = {"0001": 5.0, "0002": 5.0, "0003": 5.0}
    # Beat1←0001(cat a). Beat2: 0002(cat a, 0.50) vs 0003(cat b, 0.49). Without
    # the penalty beat2 takes 0002 (same cat a). adjacency_penalty 0.05 > 0.01
    # gap → beat2 flips to the diverse 0003.
    meta = [_meta("0001", "a"), _meta("0002", "a"), _meta("0003", "b")]
    rankings = [
        [("0001", 0.90), ("0002", 0.10), ("0003", 0.05)],
        [("0002", 0.50), ("0003", 0.49), ("0001", 0.40)],
    ]
    no_pen, _ = pack_clips(
        beats, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
        global_assignment=True, adjacency_penalty=0.0,
    )
    assert [a["clip_id"] for a in no_pen] == ["0001", "0002"]  # same cat a,a

    with_pen, _ = pack_clips(
        beats, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
        global_assignment=True, adjacency_penalty=0.05,
    )
    assert [a["clip_id"] for a in with_pen] == ["0001", "0003"]  # a,b diverse


def test_global_near_dup_folded_as_penalty():
    """Near-dup gate folded into the SAME matrix: a visual twin of a neighbour's
    clip gets penalized and is displaced for a diverse alternative."""
    beats = _beats_one_segment((0, 4), (5, 9))
    durations = {"0001": 5.0, "0002": 5.0, "0003": 5.0}
    # 0002 visually twins 0001; 0003 diverse. Beat2 prefers 0002 (0.50) over
    # 0003 (0.45). near_dup penalty pushes beat2 off the twin to 0003.
    meta = [
        _meta_emb("0001", _EMB_A, "c1", visual=_EMB_A),
        _meta_emb("0002", _EMB_A, "c2", visual=_EMB_A),       # twin of 0001
        _meta_emb("0003", _EMB_DIVERSE, "c3", visual=_EMB_DIVERSE),
    ]
    rankings = [
        [("0001", 0.90), ("0002", 0.20), ("0003", 0.10)],
        [("0002", 0.50), ("0003", 0.45), ("0001", 0.40)],
    ]
    no_gate, _ = pack_clips(
        beats, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
        global_assignment=True, near_dup_threshold=None,
    )
    assert [a["clip_id"] for a in no_gate] == ["0001", "0002"]  # twin kept

    gated, prov = pack_clips(
        beats, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
        global_assignment=True, near_dup_threshold=0.9, near_dup_penalty=0.50,
    )
    assert [a["clip_id"] for a in gated] == ["0001", "0003"]  # twin displaced
    assert prov["near_dup_threshold"] == 0.9


def test_global_relocates_window_rotation():
    """Window resolution (rule 3/5) runs AFTER the solve, unchanged: an assigned
    clip whose early seconds were shown lands its trim in the free window."""
    rankings = [[("0001", 0.9), ("0002", 0.1)], [("0002", 0.9), ("0001", 0.1)]]
    durations = {"0001": 5.0, "0002": 5.0}
    meta = [_meta("0001", "a"), _meta("0002", "b")]
    assignments, prov = pack_clips(
        BEATS, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
        global_assignment=True,
        used_windows={"asset_1": [UsedWindow(0.0, 2.5)]},
        clip_to_asset={"0001": "asset_1"},
    )
    assert assignments[0]["clip_id"] == "0001"
    assert assignments[0]["trim_start"] == 2.5      # relocated trim landed
    assert prov["window_exhausted_beats"] == []


def test_global_window_exhausted_fallback_flagged():
    """When the assigned clip's source is fully shown, the relocated
    least-overlap fallback fires and flags window_exhausted (relocated rule 3
    fallback, not reinvented)."""
    rankings = [[("0001", 0.9), ("0002", 0.1)], [("0002", 0.9), ("0001", 0.1)]]
    durations = {"0001": 5.0, "0002": 5.0}
    meta = [_meta("0001", "a"), _meta("0002", "b")]
    assignments, prov = pack_clips(
        BEATS, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
        global_assignment=True,
        used_windows={"asset_1": [UsedWindow(0.0, 5.0)]},
        clip_to_asset={"0001": "asset_1"},
    )
    assert prov["window_exhausted_beats"] == [0]
    assert prov["picks"][0]["window_exhausted"] is True


def test_global_coverage_gap_falls_back_without_reuse():
    """A beat whose only high-cosine clip is too short gets the relocated
    coverage fallback (a coverable unused clip), and the assignment stays
    strictly 1-to-1 (no reuse) — exercising the infeasible-row path in the
    solver without a spurious fail-loud."""
    beats = _beats_one_segment((0, 4), (5, 9))  # both spans 2.0s
    # 0001 long+best for beat1; 0009 is best for beat2 but only 0.5s (too short
    # to cover the 2.0s span) → beat2's feasible set is {0002}. Solver must put
    # 0001→beat1, 0002→beat2 (0009 uncoverable everywhere).
    durations = {"0001": 5.0, "0002": 5.0, "0009": 0.5}
    meta = [_meta("0001", "a"), _meta("0002", "b"), _meta("0009", "c")]
    rankings = [
        [("0001", 0.80), ("0009", 0.40), ("0002", 0.20)],
        [("0009", 0.90), ("0002", 0.30), ("0001", 0.10)],
    ]
    assignments, _ = pack_clips(
        beats, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
        global_assignment=True,
    )
    ids = [a["clip_id"] for a in assignments]
    assert ids == ["0001", "0002"]      # 0009 never picked (uncoverable)
    assert len(set(ids)) == len(ids)    # strictly 1-to-1, no reuse


def test_global_fail_loud_on_thin_pool():
    """Fewer coverable clips than beats → fail loud (same contract as greedy's
    no-coverable-candidate raise), never reuse a clip."""
    rankings = [[("0001", 0.9)], [("0001", 0.9)]]
    durations = {"0001": 5.0}
    with pytest.raises(ClipAssignmentError):
        pack_clips(
            BEATS, rankings, word_timestamps=WTS,
            clip_durations=durations, clip_metadata=[_meta("0001")],
            global_assignment=True,
        )


def test_global_flags_displaced_beat():
    """Observability: when 1-to-1 contention forces a beat onto a clip >0.15
    below its OWN best-feasible cosine, the beat is flagged in displaced_beats.

    Both beats want 0001 strongly. The solver gives it to beat 2 (0.90, the
    larger total gain) and pushes beat 1 onto 0002 (0.20) — 0.65 below beat 1's
    best-feasible 0001 (0.85). Beat 1 is the flagged silent displacement."""
    beats = _beats_one_segment((0, 4), (5, 9))
    durations = {"0001": 5.0, "0002": 5.0}
    meta = [_meta("0001", "a"), _meta("0002", "b")]
    rankings = [
        [("0001", 0.85), ("0002", 0.20)],
        [("0001", 0.90), ("0002", 0.05)],
    ]
    assignments, prov = pack_clips(
        beats, rankings, word_timestamps=WTS,
        clip_durations=durations, clip_metadata=meta,
        global_assignment=True,
    )
    assert [a["clip_id"] for a in assignments] == ["0002", "0001"]
    assert len(prov["displaced_beats"]) == 1
    d = prov["displaced_beats"][0]
    assert d["beat"] == 0 and d["clip_id"] == "0002"
    assert d["best_feasible"] == 0.85 and d["score"] == 0.20
    assert d["gap"] == pytest.approx(0.65)
    assert set(d) == {"beat", "clip_id", "score", "best_feasible", "gap"}
