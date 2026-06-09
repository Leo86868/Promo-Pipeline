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
