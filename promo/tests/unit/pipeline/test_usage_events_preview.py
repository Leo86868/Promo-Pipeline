import json
from pathlib import Path


def test_usage_events_preview_reads_manifest_and_local_sidecar(tmp_path):
    from promo.cli.usage_events_preview import build_preview
    from promo.core.pipeline.run_manifest import build_run_manifest

    sidecar_path = tmp_path / "clip_assignments_test_hotel_65s.json"
    sidecar_path.write_text(json.dumps({
        "retrieval_contract": "shared_asset_semantic_candidates_v1",
        "fallback_reason": None,
    }))

    manifest = build_run_manifest(
        poi_name="Test Hotel",
        location="Somewhere",
        target_duration_sec=65.0,
        n_variants=1,
        script_candidates=1,
        format_selector="single",
        embedding_cache_active=False,
        poi_id="poi_123",
        clip_paths={"0001": "/tmp/clip_0001.mp4"},
        clips_metadata=[],
        clip_durations={},
        shared_assets=[{
            "asset_id": "asset_abc",
            "clip_id": "0001",
            "source_storage_bucket": "poi-assets",
            "source_storage_path": "poi_123/clips/asset_abc.mp4",
            "source_content_hash": "sha256:" + "a" * 64,
            "duration_sec": 5.0,
        }],
        rendered_outputs=[{
            "variant_index": 1,
            "render_output_path": "/tmp/render.mp4",
            "final_output_path": "/tmp/final.mp4",
            "target_duration_sec": 65.0,
            "format_mode": "long",
            "voice_key": "jarnathan",
            "timeline_entries": [{
                "clip_id": "0001",
                "usage_role": "assigned_phrase",
                "segment": 1,
                "trim_start_sec": 0.0,
                "trim_end_sec": 2.0,
                "display_start_sec": 0.0,
                "display_end_sec": 2.0,
                "source_duration_sec": 5.0,
            }],
        }],
        sidecar_paths={
            "clip_assignments": "/remote/run/clip_assignments_test_hotel_65s.json",
        },
        run_id="pgc_run_test",
        manifest_id="manifest_test",
        created_at="2026-05-28T00:00:00Z",
    )
    manifest_path = tmp_path / "run_manifest_test_hotel_65s.json"
    manifest_path.write_text(json.dumps(manifest))

    preview = build_preview([manifest_path])

    assert preview["summary"]["event_count"] == 1
    assert preview["summary"]["unique_event_id_count"] == 1
    assert preview["manifests"][0]["retrieval_contract"] == (
        "shared_asset_semantic_candidates_v1"
    )
    event = preview["events"][0]
    assert event["poi_id"] == "poi_123"
    assert event["asset_id"] == "asset_abc"
    assert event["retrieval_contract"] == "shared_asset_semantic_candidates_v1"
    assert event["retrieval_fallback_reason"] is None
    assert event["clip_assignments_sidecar_path"].endswith(
        "clip_assignments_test_hotel_65s.json"
    )


def test_usage_events_preview_reports_missing_sidecar_as_null_context(tmp_path):
    from promo.cli.usage_events_preview import build_preview
    from promo.core.pipeline.run_manifest import build_run_manifest

    manifest = build_run_manifest(
        poi_name="Test Hotel",
        location="Somewhere",
        target_duration_sec=65.0,
        n_variants=1,
        script_candidates=1,
        format_selector="single",
        embedding_cache_active=False,
        poi_id="poi_123",
        clip_paths={"0001": "/tmp/clip_0001.mp4"},
        clips_metadata=[],
        clip_durations={},
        shared_assets=[{
            "asset_id": "asset_abc",
            "clip_id": "0001",
            "source_storage_bucket": "poi-assets",
            "source_storage_path": "poi_123/clips/asset_abc.mp4",
            "source_content_hash": "sha256:" + "a" * 64,
            "duration_sec": 5.0,
        }],
        rendered_outputs=[{
            "variant_index": 1,
            "render_output_path": "/tmp/render.mp4",
            "final_output_path": "/tmp/final.mp4",
            "target_duration_sec": 65.0,
            "timeline_entries": [{
                "clip_id": "0001",
                "usage_role": "bridge_tail",
                "trim_start_sec": 0.0,
                "trim_end_sec": 2.0,
                "display_start_sec": 0.0,
                "display_end_sec": 2.0,
                "source_duration_sec": 5.0,
            }],
        }],
        sidecar_paths={
            "clip_assignments": "/remote/run/missing.json",
        },
        run_id="pgc_run_test",
        manifest_id="manifest_test",
    )
    manifest_path = tmp_path / "run_manifest_test_hotel_65s.json"
    manifest_path.write_text(json.dumps(manifest))

    preview = build_preview([manifest_path])

    assert preview["summary"]["role_counts"] == {"bridge_tail": 1}
    assert preview["events"][0]["retrieval_contract"] is None
    assert preview["events"][0]["retrieval_fallback_reason"] is None
