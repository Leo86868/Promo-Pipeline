import pytest


def _asset(**overrides):
    from promo.core.assets.retrieval import ReadyAsset

    base = {
        "poi_id": "poi_123",
        "asset_id": "asset_001",
        "clip_id": "0001",
        "category": "pool",
        "scene_description": "pool with ocean view",
        "shot_size": "wide",
        "main_subject": "ocean-view pool",
        "camera_motion": "push_in",
        "embedding_text": "pool with ocean view | pool",
        "duration_sec": 5.0,
        "usage_count": 0,
        "source_storage_bucket": "poi-assets",
        "source_storage_path": "poi_123/clips/asset_001.mp4",
        "source_content_hash": "sha256:" + "a" * 64,
        "embedding_vector": (1.0, 0.0, 0.0),
    }
    base.update(overrides)
    return ReadyAsset(**base)


def test_parse_embedding_vector_accepts_pgvector_string():
    from promo.core.assets.retrieval import parse_embedding_vector

    assert parse_embedding_vector("[1, 2.5, -3]", expected_dim=3) == (1.0, 2.5, -3.0)


def test_parse_embedding_vector_rejects_wrong_dim():
    from promo.core.assets.retrieval import AssetRetrievalError, parse_embedding_vector

    with pytest.raises(AssetRetrievalError, match="does not match"):
        parse_embedding_vector([1.0, 2.0], expected_dim=3)


def test_build_asset_visual_brief_summarizes_coverage_without_ids():
    from promo.core.assets.retrieval import build_asset_visual_brief

    assets = [
        _asset(
            asset_id="asset_002",
            clip_id="0002",
            category="room",
            duration_sec=8.5,
            usage_count=3,
            main_subject="ocean-view room",
            scene_description="room with ocean-view balcony",
        ),
        _asset(
            asset_id="asset_001",
            clip_id="0001",
            category="pool",
            duration_sec=5.0,
            usage_count=1,
            main_subject="pool cabanas",
            scene_description="pool cabanas near the ocean",
        ),
        _asset(
            asset_id="asset_003",
            clip_id="0003",
            category="pool",
            duration_sec=7.0,
            usage_count=0,
            main_subject="sunset pool terrace",
            scene_description="sunset pool terrace with lounge chairs",
        ),
    ]

    brief = build_asset_visual_brief(assets, motifs_per_category=1)

    assert brief["eligible_asset_count"] == 3
    assert brief["eligible_total_seconds"] == 20.5
    assert brief["categories"][0]["category"] == "pool"
    assert brief["categories"][0]["asset_count"] == 2
    assert brief["categories"][0]["total_seconds"] == 12.0
    assert brief["categories"][0]["coverage_motifs"] == [
        {
            "phrase": "sunset pool terrace",
            "duration_sec": 7.0,
            "usage_count": 0,
        }
    ]
    assert "asset_id" not in brief["categories"][0]["coverage_motifs"][0]
    assert brief["categories"][1]["category"] == "room"
    assert brief["core_visuals"][0]["category"] == "pool"
    assert [
        (item["coverage_role"], item["category"], item["visual_detail"])
        for item in brief["grounding_set"]
    ] == [
        ("core", "pool", "sunset pool terrace with lounge chairs"),
        ("core", "pool", "pool cabanas near the ocean"),
        ("core", "room", "room with ocean-view balcony"),
    ]
    assert "asset_id" not in brief["grounding_set"][0]
    assert "core promises" in brief["summary_note"]


def test_retrieve_candidates_requires_pool_at_least_threshold():
    from promo.core.assets.retrieval import AssetRetrievalError, retrieve_candidates

    with pytest.raises(AssetRetrievalError, match="eligible asset pool"):
        retrieve_candidates(
            assets=[_asset()],
            queries=["pool"],
            query_vectors=[(1.0, 0.0, 0.0)],
            min_eligible_assets=2,
        )


def test_retrieve_candidates_dedupes_ranked_assets():
    from promo.core.assets.retrieval import retrieve_candidates

    assets = [
        _asset(asset_id="asset_001", clip_id="0001", embedding_vector=(1.0, 0.0, 0.0)),
        _asset(asset_id="asset_002", clip_id="0002", embedding_vector=(0.0, 1.0, 0.0)),
        _asset(asset_id="asset_003", clip_id="0003", embedding_vector=(0.0, 0.0, 1.0)),
    ]

    candidates = retrieve_candidates(
        assets=assets,
        queries=["pool", "pool again"],
        query_vectors=[(1.0, 0.0, 0.0), (1.0, 0.0, 0.0)],
        top_k_per_query=2,
        max_candidates=5,
        min_eligible_assets=2,
    )

    assert [item.asset.asset_id for item in candidates] == ["asset_001", "asset_002"]
    assert candidates[0].query_index == 0
    assert candidates[0].rank_for_query == 1


def test_candidate_asset_ids_for_download_pads_bridge_reserve():
    from promo.core.assets.retrieval import candidate_asset_ids_for_download, retrieve_candidates

    assets = [
        _asset(
            asset_id=f"asset_{index:03d}",
            clip_id=f"{index:04d}",
            usage_count=index,
            duration_sec=5.0 + index,
            embedding_vector=(1.0, 0.0, 0.0) if index <= 2 else (0.0, 1.0, 0.0),
        )
        for index in range(1, 8)
    ]
    candidates = retrieve_candidates(
        assets=assets,
        queries=["pool"],
        query_vectors=[(1.0, 0.0, 0.0)],
        top_k_per_query=2,
        max_candidates=5,
        min_eligible_assets=2,
    )

    asset_ids = candidate_asset_ids_for_download(
        candidates=candidates,
        assets=assets,
        min_candidates=5,
        max_candidates=5,
    )

    assert asset_ids[:2] == ["asset_001", "asset_002"]
    assert len(asset_ids) == 5
    assert len(set(asset_ids)) == 5


def test_build_script_retrieval_queries_uses_segment_text():
    from promo.core.assets.retrieval import build_script_retrieval_queries

    queries = build_script_retrieval_queries([
        {
            "variant_index": 1,
            "segments": [
                {"segment": 1, "text": "Oceanfront arrival with coastal views."},
                {"segment": 2, "text": "  "},
                {"segment": 3, "text": "Pool, spa, and rooms near the bluff."},
            ],
        },
        {"variant_index": 2, "segments": [{"text": "Sunset dining on the terrace."}]},
    ])

    assert queries == [
        "Oceanfront arrival with coastal views.",
        "Pool, spa, and rooms near the bluff.",
        "Sunset dining on the terrace.",
    ]


def test_build_script_retrieval_queries_rejects_empty_scripts():
    from promo.core.assets.retrieval import (
        AssetRetrievalError,
        build_script_retrieval_queries,
    )

    with pytest.raises(AssetRetrievalError, match="no script segment text"):
        build_script_retrieval_queries([{"segments": [{"text": ""}]}])


# --- V2 brief sampler (A 层抽样 + B 层 motif 轮换) -------------------------


def _pool(n_per_cat: dict[str, int]):
    assets = []
    i = 0
    for cat, n in n_per_cat.items():
        for _ in range(n):
            i += 1
            assets.append(_asset(
                clip_id=f"{i:04d}",
                asset_id=f"asset_{i:03d}",
                category=cat,
                main_subject=f"{cat} subject {i}",
                scene_description=f"{cat} scene {i}",
            ))
    return assets


class TestBriefSampler:
    def test_small_pool_returned_whole(self):
        from promo.core.assets.retrieval import sample_brief_display_assets

        pool = _pool({"pool": 20, "room": 20})  # 40 <= target_min 50
        assert sample_brief_display_assets(pool, seed=3) == pool

    def test_deterministic_per_seed_and_varies_across_seeds(self):
        from promo.core.assets.retrieval import sample_brief_display_assets

        pool = _pool({"pool": 40, "room": 30, "food": 29})
        a1 = {x.clip_id for x in sample_brief_display_assets(pool, seed=1)}
        a1_again = {x.clip_id for x in sample_brief_display_assets(pool, seed=1)}
        a2 = {x.clip_id for x in sample_brief_display_assets(pool, seed=2)}
        assert a1 == a1_again
        assert a1 != a2  # different videos see different members

    def test_stratified_floor_and_target(self):
        from promo.core.assets.retrieval import sample_brief_display_assets

        pool = _pool({"pool": 60, "room": 30, "scenic": 8, "spa": 1})
        sampled = sample_brief_display_assets(pool, seed=5)
        assert 50 <= len(sampled) <= 60
        by_cat = {}
        for x in sampled:
            by_cat[x.category] = by_cat.get(x.category, 0) + 1
        # Every non-empty category keeps representation (floor 1)…
        assert set(by_cat) == {"pool", "room", "scenic", "spa"}
        # …and allocation roughly follows pool proportions.
        assert by_cat["pool"] > by_cat["room"] > by_cat["scenic"] >= by_cat["spa"] == 1

    def test_category_weights_c_slot_shifts_allocation(self):
        from promo.core.assets.retrieval import sample_brief_display_assets

        pool = _pool({"pool": 50, "room": 50})
        plain = sample_brief_display_assets(pool, seed=7)
        weighted = sample_brief_display_assets(
            pool, seed=7, category_weights={"room": 3.0},
        )
        count = lambda s, c: sum(1 for x in s if x.category == c)  # noqa: E731
        assert count(weighted, "room") > count(plain, "room")

    def test_brief_stats_full_pool_scenes_from_display(self):
        from promo.core.assets.retrieval import build_asset_visual_brief

        pool = _pool({"pool": 6})
        display = pool[:2]
        brief = build_asset_visual_brief(pool, display_assets=display)
        row = brief["categories"][0]
        # Stats tell the truth about store size…
        assert row["asset_count"] == 6
        assert brief["eligible_asset_count"] == 6
        # …but concrete scenes only come from the display subset.
        shown = {m["phrase"] for m in row["coverage_motifs"]}
        assert shown <= {"pool subject 1", "pool subject 2"}
        grounding = {g["visual_detail"] for g in brief["grounding_set"]}
        assert grounding <= {"pool scene 1", "pool scene 2"}

    def test_motif_seed_rotates_leading_motifs(self):
        from promo.core.assets.retrieval import build_asset_visual_brief

        pool = _pool({"pool": 9})  # 9 distinct subjects, max 4 shown
        b0 = build_asset_visual_brief(pool, motif_seed=0)
        b3 = build_asset_visual_brief(pool, motif_seed=3)
        m0 = [m["phrase"] for m in b0["categories"][0]["coverage_motifs"]]
        m3 = [m["phrase"] for m in b3["categories"][0]["coverage_motifs"]]
        assert len(m0) == len(m3) == 4
        assert m0 != m3
