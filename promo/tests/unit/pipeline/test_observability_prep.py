from pathlib import Path
from unittest.mock import MagicMock, patch


def test_write_sidecar_result_reports_exact_and_bumped_paths(tmp_path):
    from promo.core.pipeline.sidecar_writer import _write_sidecar_result

    first = _write_sidecar_result(
        str(tmp_path), "tts_metrics_test_30s.json",
        [{"variant_index": 1}], "tts_metrics",
    )
    assert first.ok is True
    assert first.path == str(tmp_path / "tts_metrics_test_30s.json")

    second = _write_sidecar_result(
        str(tmp_path), "tts_metrics_test_30s.json",
        [{"variant_index": 2}], "tts_metrics",
    )
    assert second.ok is True
    assert second.path == str(tmp_path / "tts_metrics_test_30s-2.json")
    assert Path(first.path).exists()
    assert Path(second.path).exists()


def test_emit_run_sidecars_result_exposes_written_paths(tmp_path):
    from promo.core.pipeline.sidecar_writer import _emit_run_sidecars_result

    backend = MagicMock()
    backend.output_dir.return_value = str(tmp_path)
    result = _emit_run_sidecars_result(
        backend=backend,
        output_path=str(tmp_path / "promo.mp4"),
        poi_name="Test Hotel",
        target_duration_sec=30.0,
        tts_metrics=[{"variant_index": 1}],
        match_quality_entries=[{"variant_index": 1}],
        clip_assignments_entries=[{"variant_index": 1, "assignments": []}],
        run_retrieval_provenance={
            "retrieval_active": False,
            "embedded_pool_size": 0,
            "reduced_pool_size": 0,
            "mimo_prompt_sha1": None,
            "fallback_reason": None,
            "retrieval_contract": "soft_hint",
        },
    )

    assert result.ok is True
    assert result.paths == {
        "tts_metrics": str(tmp_path / "tts_metrics_test_hotel_30s.json"),
        "match_quality": str(tmp_path / "match_quality_test_hotel_30s.json"),
        "clip_assignments": str(tmp_path / "clip_assignments_test_hotel_30s.json"),
    }


def test_run_variant_loop_accumulates_rendered_output_facts(tmp_path):
    from promo.core.pipeline.bgm_voice_resolver import _empty_retrieval_provenance
    from promo.core.pipeline.variant_loop import _run_variant_loop

    output_path = str(tmp_path / "promo_test.mp4")
    variant_output_path = str(tmp_path / "promo_test_65s.mp4")
    final_path = str(tmp_path / "final" / "promo_test.mp4")
    backend = MagicMock()
    backend.save_output.return_value = final_path
    rendered_outputs: list[dict] = []
    props = {
        "meta": {"poiName": "Test Hotel", "location": "Nowhere", "fps": 30},
        "clips": [
            {
                "clipId": "0001", "file": "clip.mp4", "narration": "hello",
                "videoStart": 0.0, "videoEnd": 1.0,
                "trimStart": 0.0, "trimEnd": 1.0,
            }
        ],
        "audio": {"narration": "narration.wav", "bgm": "bgm.mp3"},
        "captions": {"wordTimestamps": [{"word": "hello", "start": 0.0, "end": 0.5}]},
        "segments": [{"segment": 1, "text": "hello", "startSec": 0.0, "endSec": 0.5}],
    }

    def fake_build_props(*args, **kwargs):
        kwargs["timeline_entries"].append({
            "clip_id": "0001",
            "usage_role": "assigned_phrase",
            "display_start_sec": 0.0,
            "display_end_sec": 1.0,
            "trim_start_sec": 0.0,
            "trim_end_sec": 1.0,
            "source_duration_sec": 5.0,
        })
        return props

    def fake_render(_props, out):
        Path(out).write_bytes(b"video")
        return True

    with patch(
        "promo.core.pipeline.variant_loop._step_tts_narration",
        return_value={
            "duration": 0.5,
            "word_timestamps": [{"word": "hello", "start": 0.0, "end": 0.5}],
            "segment_timestamps": [{"segment": 1, "start": 0.0, "end": 0.5}],
            "audio_path": str(tmp_path / "narration.wav"),
        },
    ), patch(
        "promo.core.pipeline.variant_loop._step_assign_clips",
        return_value=(
            {
                "variant_index": 1,
                "effective_wpm": 120,
                "segments": [{"segment": 1, "text": "hello"}],
                "format_mode": "long",
                "target_duration_sec": 65,
            },
            {
                "duration": 0.5,
                "word_timestamps": [{"word": "hello", "start": 0.0, "end": 0.5}],
                "segment_timestamps": [{"segment": 1, "start": 0.0, "end": 0.5}],
                "audio_path": str(tmp_path / "narration.wav"),
            },
            [{"segment": 1, "clip_id": "0001"}],
            _empty_retrieval_provenance(),
        ),
    ), patch(
        "promo.core.pipeline.variant_loop.build_props_from_script",
        side_effect=fake_build_props,
    ), patch(
        "promo.core.pipeline.variant_loop.validate_props",
        return_value=[],
    ), patch(
        "promo.core.pipeline.variant_loop.stage_media",
    ), patch(
        "promo.core.pipeline.variant_loop.build_match_quality_entries",
        return_value=[],
    ), patch(
        "promo.core.pipeline.variant_loop.render_promo",
        side_effect=fake_render,
    ):
        all_ok, hard_fails, _provenance = _run_variant_loop(
            scripts=[{
                "variant_index": 1,
                "effective_wpm": 120,
                "segments": [{"segment": 1, "text": "hello"}],
                "format_mode": "long",
                "target_duration_sec": 65,
            }],
            clip_paths={"0001": str(tmp_path / "clip.mp4")},
            clips_metadata=[{"id": "0001"}],
            clip_durations={"0001": 5.0},
            resolved_voice_keys=["kore"],
            resolved_bgm_paths=[str(tmp_path / "bgm.mp3")],
            variant_profiles=[MagicMock()],
            variant_personas=[MagicMock()],
            output_path=output_path,
            backend=backend,
            poi_name="Test Hotel",
            location="Nowhere",
            hotel_description="",
            notable_details="",
            tmp_dir=str(tmp_path),
            tts_speed=0.95,
            target_duration_sec=65,
            script_candidates=1,
            embedding_cache_dir=None,
            tts_metrics=[],
            match_quality_entries=[],
            clip_assignments_entries=[],
            rendered_outputs=rendered_outputs,
        )

    assert all_ok is True
    assert hard_fails == 0
    assert rendered_outputs == [{
        "variant_index": 1,
        "variant_status": "rendered",
        "render_output_path": variant_output_path,
        "final_output_path": final_path,
        "target_duration_sec": 65.0,
        "format_mode": "long",
        "voice_key": "kore",
        "bgm_path": str(tmp_path / "bgm.mp3"),
        "file_size_bytes": 5,
        "timeline_entries": [{
            "clip_id": "0001",
            "usage_role": "assigned_phrase",
            "display_start_sec": 0.0,
            "display_end_sec": 1.0,
            "trim_start_sec": 0.0,
            "trim_end_sec": 1.0,
            "source_duration_sec": 5.0,
        }],
    }]


def test_renderer_timeline_marks_assigned_and_bridge_roles():
    from promo.core.render import remotion_renderer

    word_timestamps = [
        {"word": "first", "start": 0.0, "end": 0.5},
        {"word": "second", "start": 3.0, "end": 3.5},
    ]
    assignments = [
        {
            "segment": 1, "clip_id": "0001",
            "start_word_idx": 0, "end_word_idx": 0,
            "trim_start": 0.0, "source_duration_sec": 1.0,
        },
        {
            "segment": 2, "clip_id": "0002",
            "start_word_idx": 1, "end_word_idx": 1,
            "trim_start": 0.0, "source_duration_sec": 5.0,
        },
    ]
    clip_paths = {
        "0001": "/tmp/clip_0001.mp4",
        "0002": "/tmp/clip_0002.mp4",
        "0003": "/tmp/clip_0003.mp4",
    }

    with patch.object(remotion_renderer, "get_clip_duration", return_value=2.0):
        renderer_entries = remotion_renderer._bind_clips_to_narration(
            assignments, clip_paths, word_timestamps,
        )
    timeline = remotion_renderer.build_renderer_timeline_entries(renderer_entries)

    assert [row["usage_role"] for row in timeline] == [
        "assigned_phrase", "bridge_tail", "assigned_phrase",
    ]
    assert timeline[0]["clip_id"] == "0001"
    assert timeline[0]["segment"] == 1
    assert timeline[0]["display_start_sec"] == 0.0
    assert timeline[0]["display_end_sec"] == 1.0
    assert timeline[1]["clip_id"] == "0003"
    assert timeline[1]["segment"] is None
    assert timeline[1]["display_start_sec"] == 1.0
    assert timeline[1]["display_end_sec"] == 3.0
    assert timeline[1]["source_duration_sec"] == 2.0
    assert timeline[2]["clip_id"] == "0002"
    assert timeline[2]["segment"] == 2
