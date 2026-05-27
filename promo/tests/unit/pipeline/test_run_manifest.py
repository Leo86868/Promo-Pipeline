from copy import deepcopy
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch


def test_build_run_manifest_local_mode_keeps_shared_ids_null():
    from promo.core.pipeline.run_manifest import build_run_manifest

    manifest = build_run_manifest(
        poi_name="Test Hotel",
        location="Nowhere",
        target_duration_sec=65.0,
        n_variants=1,
        script_candidates=2,
        format_selector="single",
        embedding_cache_active=False,
        clip_paths={"0001": "/tmp/clip_0001.mp4"},
        clips_metadata=[{
            "id": "0001",
            "scene_description": "pool at sunset",
            "category": "pool",
            "source_duration_sec": 5.12,
        }],
        clip_durations={"0001": 5.12},
        rendered_outputs=[{
            "variant_index": 1,
            "variant_status": "rendered",
            "render_output_path": "/tmp/render.mp4",
            "final_output_path": "/tmp/final.mp4",
            "target_duration_sec": 65.0,
            "format_mode": "long",
            "voice_key": "kore",
            "bgm_path": "/tmp/bgm.mp3",
            "file_size_bytes": 123,
            "timeline_entries": [{
                "clip_id": "0001",
                "usage_role": "assigned_phrase",
                "segment": 1,
                "source_path": "/tmp/clip_0001.mp4",
                "trim_start_sec": 0.0,
                "trim_end_sec": 2.0,
                "display_start_sec": 0.0,
                "display_end_sec": 2.0,
                "source_duration_sec": 5.12,
            }],
        }],
        sidecar_paths={
            "clip_assignments": "/tmp/clip_assignments_test_hotel_65s.json",
            "tts_metrics": "/tmp/tts_metrics_test_hotel_65s.json",
            "match_quality": "/tmp/match_quality_test_hotel_65s.json",
        },
        skip_analysis=True,
        tts_speed=0.9,
        seed=123,
        run_id="pgc_run_test",
        manifest_id="manifest_test",
        created_at="2026-05-26T00:00:00Z",
    )

    assert manifest["schema_version"] == 1
    assert manifest["manifest_id"] == "manifest_test"
    assert manifest["run_id"] == "pgc_run_test"
    assert manifest["poi"]["poi_id"] is None
    assert manifest["poi"]["pgc_slug"] == "test_hotel"
    assert "canonical_key" not in manifest["poi"]
    assert manifest["run_config"]["skip_analysis"] is True
    assert manifest["run_config"]["tts_speed"] == 0.9
    assert manifest["run_config"]["seed"] == 123
    assert manifest["asset_snapshot"][0]["asset_id"] is None
    assert manifest["asset_snapshot"][0]["source_storage_bucket"] is None
    assert manifest["asset_snapshot"][0]["source_storage_path"] is None
    assert manifest["asset_snapshot"][0]["source_content_hash"] is None
    assert manifest["asset_snapshot"][0]["scene_description"] == "pool at sunset"
    assert manifest["outputs"][0]["output_path"] == "/tmp/final.mp4"
    assert manifest["sidecars"]["clip_assignments"].endswith(
        "clip_assignments_test_hotel_65s.json"
    )
    assert manifest["timeline_entries"][0]["occurrence_index"] == 0
    assert manifest["timeline_entries"][0]["occurrence_id"] == "occ_0001_000000"
    assert manifest["timeline_entries"][0]["asset_id"] is None
    assert "source_path" not in manifest["timeline_entries"][0]
    assert "usage_event_drafts" not in manifest


def test_build_run_manifest_snapshots_poi_asset_valid_clips_and_bridge_asset_ids():
    from promo.core.pipeline.run_manifest import build_run_manifest

    manifest = build_run_manifest(
        poi_name="Shared Hotel",
        location="Somewhere",
        target_duration_sec=60.0,
        n_variants=1,
        script_candidates=1,
        format_selector="single",
        embedding_cache_active=False,
        poi_id="poi_123",
        canonical_key="shared hotel",
        clip_paths={
            "0001": "/tmp/clip_0001.mp4",
            "0002": "/tmp/clip_0002.mp4",
        },
        clips_metadata=[],
        clip_durations={},
        shared_assets=[
            {
                "poi_id": "poi_123",
                "asset_id": "asset_abc",
                "clip_id": "0001",
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
                "scene_description": "pool at sunset",
                "category": "pool",
                "embedding_status": "pending",
            },
            {
                "poi_id": "poi_123",
                "asset_id": "asset_def",
                "clip_id": "0002",
                "source_storage_bucket": "poi-assets",
                "source_storage_path": "poi_123/clips/asset_def.mp4",
                "source_content_hash": "sha256:" + "b" * 64,
                "duration_sec": 6.0,
                "scene_description": "lobby detail",
                "category": "lobby",
            },
        ],
        rendered_outputs=[{
            "variant_index": 1,
            "render_output_path": "/tmp/render.mp4",
            "final_output_path": "/tmp/final.mp4",
            "target_duration_sec": 60.0,
            "format_mode": "long",
            "voice_key": "kore",
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
        created_at="2026-05-26T00:00:00Z",
    )

    assert manifest["poi"]["poi_id"] == "poi_123"
    assert manifest["poi"]["canonical_key"] == "shared hotel"
    assert manifest["asset_snapshot"][0]["asset_id"] == "asset_abc"
    assert manifest["asset_snapshot"][0]["source_storage_bucket"] == "poi-assets"
    assert manifest["asset_snapshot"][0]["source_storage_path"] == (
        "poi_123/clips/asset_abc.mp4"
    )
    assert manifest["asset_snapshot"][0]["source_content_hash"] == "sha256:" + "a" * 64
    assert manifest["asset_snapshot"][0]["width"] == 720
    assert manifest["asset_snapshot"][0]["scene_description"] == "pool at sunset"
    assert manifest["timeline_entries"][0]["asset_id"] == "asset_abc"
    assert manifest["timeline_entries"][0]["occurrence_id"] == "occ_0001_000000"
    assert manifest["timeline_entries"][1]["asset_id"] == "asset_def"
    assert manifest["timeline_entries"][1]["usage_role"] == "bridge_tail"
    assert manifest["timeline_entries"][1]["segment"] is None
    assert manifest["timeline_entries"][1]["occurrence_id"] == "occ_0001_000001"


def test_usage_event_ids_use_occurrence_id_not_float_seconds():
    from promo.core.pipeline.run_manifest import (
        build_run_manifest,
        build_usage_events_from_manifest,
    )

    manifest = build_run_manifest(
        poi_name="Shared Hotel",
        location="Somewhere",
        target_duration_sec=60.0,
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
            "target_duration_sec": 60.0,
            "format_mode": "long",
            "voice_key": "kore",
            "timeline_entries": [{
                "clip_id": "0001",
                "usage_role": "assigned_phrase",
                "segment": 1,
                "trim_start_sec": 0.1,
                "trim_end_sec": 2.0,
                "display_start_sec": 0.2,
                "display_end_sec": 2.1,
                "source_duration_sec": 5.0,
            }],
        }],
        sidecar_paths={
            "clip_assignments": "/tmp/clip_assignments.json",
            "tts_metrics": "/tmp/tts_metrics.json",
            "match_quality": "/tmp/match_quality.json",
        },
        run_id="pgc_run_test",
        manifest_id="manifest_test",
        created_at="2026-05-26T00:00:00Z",
    )

    events = build_usage_events_from_manifest(manifest)
    changed_seconds = deepcopy(manifest)
    changed_seconds["timeline_entries"][0]["trim_start_sec"] = 0.123456
    changed_seconds["timeline_entries"][0]["display_start_sec"] = 0.234567
    changed_seconds["timeline_entries"][0]["display_end_sec"] = 2.345678
    events_with_changed_seconds = build_usage_events_from_manifest(changed_seconds)

    assert events[0]["event_id"] == events_with_changed_seconds[0]["event_id"]
    assert events[0]["event_id"].startswith("sha256:")
    assert events[0]["occurrence_id"] == "occ_0001_000000"
    assert events[0]["trim_start_sec"] == 0.1
    assert events[0]["display_start_sec"] == 0.2
    assert events[0]["asset_id"] == "asset_abc"
    assert events[0]["clip_assignments_sidecar_path"] == "/tmp/clip_assignments.json"


def test_emit_run_manifest_uses_sidecar_collision_bump(tmp_path):
    from promo.core.pipeline.run_manifest import emit_run_manifest

    base = tmp_path / "run_manifest_test_hotel_65s.json"
    base.write_text("{}")
    result = emit_run_manifest(
        sidecar_dir=str(tmp_path),
        poi_name="Test Hotel",
        target_duration_sec=65.0,
        manifest={"schema_version": 1},
    )

    assert result.ok is True
    assert result.path == str(tmp_path / "run_manifest_test_hotel_65s-2.json")
    assert json.loads(Path(result.path).read_text()) == {"schema_version": 1}


def test_full_pipeline_emits_local_run_manifest(tmp_path):
    from promo.core.pipeline.pipeline import full_pipeline

    backend = MagicMock()
    backend.fetch_clips.return_value = {
        f"{i:04d}": str(tmp_path / f"clip_{i:04d}.mp4")
        for i in range(1, 15)
    }
    backend.fetch_bgm.return_value = str(tmp_path / "bgm.mp3")
    backend.output_dir.return_value = str(tmp_path)
    backend.clips_dir.return_value = None
    backend.save_output.side_effect = lambda _poi_name, path: path

    scripts = [{
        "variant_index": 1,
        "segments": [{"segment": 1, "text": "hello world"}],
        "total_words": 2,
        "format_mode": "long",
        "target_duration_sec": 65,
        "effective_wpm": 120,
    }]
    narration = {
        "duration": 0.8,
        "word_timestamps": [
            {"word": "hello", "start": 0.0, "end": 0.4},
            {"word": "world", "start": 0.4, "end": 0.8},
        ],
        "segment_timestamps": [{"segment": 1, "start": 0.0, "end": 0.8}],
        "audio_path": str(tmp_path / "narration.wav"),
    }
    props = {
        "meta": {"poiName": "Test Hotel", "location": "Nowhere", "fps": 30},
        "clips": [{
            "clipId": "0001", "file": "clip.mp4", "narration": "hello world",
            "videoStart": 0.0, "videoEnd": 1.0,
            "trimStart": 0.0, "trimEnd": 1.0,
        }],
        "audio": {"narration": "narration.wav", "bgm": "bgm.mp3"},
        "captions": {"wordTimestamps": narration["word_timestamps"]},
        "segments": [{"segment": 1, "text": "hello world", "startSec": 0.0, "endSec": 0.8}],
    }

    def fake_assign(script, narration_in, _metadata, _durations, **_kwargs):
        return script, narration_in, [{
            "segment": 1,
            "clip_id": "0001",
            "start_word_idx": 0,
            "end_word_idx": 1,
            "trim_start": 0.0,
            "display_span_sec": 0.8,
            "source_duration_sec": 5.0,
        }]

    def fake_build_props(*_args, **kwargs):
        kwargs["timeline_entries"].append({
            "clip_id": "0001",
            "usage_role": "assigned_phrase",
            "segment": 1,
            "source_path": str(tmp_path / "clip_0001.mp4"),
            "trim_start_sec": 0.0,
            "trim_end_sec": 1.0,
            "display_start_sec": 0.0,
            "display_end_sec": 1.0,
            "source_duration_sec": 5.0,
        })
        return props

    def fake_render(_props, output_path):
        Path(output_path).write_bytes(b"video")
        return True

    with patch(
        "promo.core.pipeline.steps.analyze_clips_for_script",
        return_value=[
            {
                "id": f"{i:04d}",
                "scene_description": f"clip {i}",
                "category": "scenic",
            }
            for i in range(1, 15)
        ],
    ), patch(
        "promo.core.render.remotion_renderer.get_clip_duration",
        return_value=5.0,
    ), patch(
        "promo.core.script.script_generator.generate_script_variants",
        return_value=scripts,
    ), patch(
        "promo.core.narrate.tts_engine.generate_narration",
        return_value=narration,
    ), patch(
        "promo.core.assign.clip_assigner.assign_clips_with_f3_retry",
        side_effect=fake_assign,
    ), patch(
        "promo.core.pipeline.variant_loop.build_props_from_script",
        side_effect=fake_build_props,
    ), patch(
        "promo.core.pipeline.variant_loop.validate_props",
        return_value=[],
    ), patch(
        "promo.core.pipeline.variant_loop.stage_media",
    ), patch(
        "promo.core.pipeline.variant_loop.render_promo",
        side_effect=fake_render,
    ):
        ok = full_pipeline(
            poi_name="Test Hotel",
            location="Nowhere",
            output_path=str(tmp_path / "promo_test.mp4"),
            backend=backend,
            target_duration_sec=65,
            n_variants=1,
            script_candidates=1,
        )

    assert ok is True
    manifest_path = tmp_path / "run_manifest_test_hotel_65s.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["poi"]["display_name"] == "Test Hotel"
    assert manifest["outputs"][0]["output_path"].endswith("promo_test_65s.mp4")
    assert manifest["sidecars"]["clip_assignments"].endswith(
        "clip_assignments_test_hotel_65s.json"
    )
    assert manifest["timeline_entries"][0]["clip_id"] == "0001"
    assert manifest["timeline_entries"][0]["occurrence_id"] == "occ_0001_000000"
    assert manifest["timeline_entries"][0]["asset_id"] is None
    assert "usage_event_drafts" not in manifest
    assert not any(
        path.name.startswith("batch_manifest_")
        for path in tmp_path.iterdir()
    )
