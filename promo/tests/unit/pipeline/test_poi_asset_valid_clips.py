import pytest


def _row(**overrides):
    row = {
        "poi_id": "poi_123",
        "asset_id": "asset_abc",
        "clip_id": "0001",
        "display_name": "Test Hotel",
        "canonical_key": "test hotel",
        "source_storage_bucket": "poi-assets",
        "source_storage_path": "poi_123/clips/asset_abc.mp4",
        "source_content_hash": "sha256:" + "a" * 64,
        "duration_sec": 5.5,
        "width": 720,
        "height": 1280,
        "fps": 30,
        "container": "mp4",
        "video_codec": "h264",
        "file_size_bytes": 1000,
        "scene_description": None,
        "category": None,
        "embedding_text": None,
        "embedding_status": "pending",
        "status": "active",
    }
    row.update(overrides)
    return row


def test_normalize_poi_asset_valid_clip_row_projects_contract_fields():
    from promo.core.pipeline.poi_asset_valid_clips import (
        POI_ASSET_VALID_CLIPS_VIEW,
        normalize_poi_asset_valid_clip_row,
    )

    normalized = normalize_poi_asset_valid_clip_row(_row())

    assert POI_ASSET_VALID_CLIPS_VIEW == "poi_asset_valid_clips"
    assert normalized["poi_id"] == "poi_123"
    assert normalized["asset_id"] == "asset_abc"
    assert normalized["clip_id"] == "0001"
    assert normalized["source_storage_bucket"] == "poi-assets"
    assert normalized["source_storage_path"] == "poi_123/clips/asset_abc.mp4"
    assert normalized["source_content_hash"] == "sha256:" + "a" * 64
    assert normalized["duration_sec"] == 5.5
    assert normalized["width"] == 720
    assert normalized["scene_description"] is None
    assert normalized["embedding_status"] == "pending"


def test_build_poi_asset_valid_clip_snapshot_sorts_and_rejects_duplicates():
    from promo.core.pipeline.poi_asset_valid_clips import (
        PoiAssetValidClipError,
        build_poi_asset_valid_clip_snapshot,
    )

    second = _row(
        asset_id="asset_def",
        clip_id="0002",
        source_storage_path="poi_123/clips/asset_def.mp4",
        source_content_hash="sha256:" + "b" * 64,
    )

    snapshot = build_poi_asset_valid_clip_snapshot([second, _row()], poi_id="poi_123")

    assert [row["clip_id"] for row in snapshot] == ["0001", "0002"]
    with pytest.raises(PoiAssetValidClipError, match="duplicate clip_id"):
        build_poi_asset_valid_clip_snapshot([_row(), _row()], poi_id="poi_123")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"asset_id": None}, "missing required fields"),
        ({"clip_id": "1"}, "4-digit"),
        ({"source_storage_bucket": "pipeline-media"}, "poi-assets"),
        (
            {"source_storage_path": "poi-assets/poi_123/clips/asset_abc.mp4"},
            "must not include bucket",
        ),
        ({"source_content_hash": "bad"}, "sha256"),
        ({"status": "retired"}, "active"),
    ],
)
def test_normalize_poi_asset_valid_clip_row_rejects_invalid_identity(
    overrides,
    message,
):
    from promo.core.pipeline.poi_asset_valid_clips import (
        PoiAssetValidClipError,
        normalize_poi_asset_valid_clip_row,
    )

    with pytest.raises(PoiAssetValidClipError, match=message):
        normalize_poi_asset_valid_clip_row(_row(**overrides))


def test_poi_asset_valid_clip_snapshot_feeds_manifest_bridge_asset_ids():
    from promo.core.pipeline.poi_asset_valid_clips import (
        build_poi_asset_valid_clip_snapshot,
    )
    from promo.core.pipeline.run_manifest import build_run_manifest

    shared_assets = build_poi_asset_valid_clip_snapshot([
        _row(),
        _row(
            asset_id="asset_def",
            clip_id="0002",
            source_storage_path="poi_123/clips/asset_def.mp4",
            source_content_hash="sha256:" + "b" * 64,
            duration_sec=6.0,
        ),
    ])
    manifest = build_run_manifest(
        poi_name="Test Hotel",
        location="Nowhere",
        target_duration_sec=60.0,
        n_variants=1,
        script_candidates=1,
        format_selector="single",
        embedding_cache_active=False,
        poi_id="poi_123",
        canonical_key="test hotel",
        clip_paths={
            "0001": "/tmp/clip_0001.mp4",
            "0002": "/tmp/clip_0002.mp4",
        },
        clips_metadata=[],
        clip_durations={},
        shared_assets=shared_assets,
        rendered_outputs=[{
            "variant_index": 1,
            "render_output_path": "/tmp/render.mp4",
            "final_output_path": "/tmp/final.mp4",
            "target_duration_sec": 60.0,
            "timeline_entries": [
                {
                    "clip_id": "0001",
                    "usage_role": "assigned_phrase",
                    "segment": 1,
                    "trim_start_sec": 0.0,
                    "trim_end_sec": 2.0,
                    "display_start_sec": 0.0,
                    "display_end_sec": 2.0,
                    "source_duration_sec": 5.5,
                },
                {
                    "clip_id": "0002",
                    "usage_role": "bridge_tail",
                    "segment": None,
                    "trim_start_sec": 1.0,
                    "trim_end_sec": 3.0,
                    "display_start_sec": 2.0,
                    "display_end_sec": 4.0,
                    "source_duration_sec": 6.0,
                },
            ],
        }],
        sidecar_paths={},
        run_id="pgc_run_test",
        manifest_id="manifest_test",
        created_at="2026-05-27T00:00:00Z",
    )

    assert manifest["asset_snapshot"][0]["asset_id"] == "asset_abc"
    assert manifest["asset_snapshot"][0]["source_storage_path"] == (
        "poi_123/clips/asset_abc.mp4"
    )
    assert manifest["timeline_entries"][0]["asset_id"] == "asset_abc"
    assert manifest["timeline_entries"][1]["asset_id"] == "asset_def"
    assert manifest["timeline_entries"][1]["usage_role"] == "bridge_tail"
    assert manifest["timeline_entries"][1]["occurrence_id"] == "occ_0001_000001"
