import json

import pytest


def test_plan_batch_items_isolates_each_video_run(tmp_path):
    from promo.cli.run_batch import BatchPoi, plan_batch_items

    items = plan_batch_items(
        pois=[
            BatchPoi(
                name="Terranea Resort",
                location="Rancho Palos Verdes",
                poi_id="poi_123",
                canonical_key=None,
            )
        ],
        videos_per_poi=3,
        target_duration_sec=65,
        output_root=str(tmp_path),
        voices=["jarnathan", "hope"],
        music_ids=["music_a", "music_b"],
        base_seed=100,
    )

    assert [item.video_index for item in items] == [1, 2, 3]
    assert [item.voice_key for item in items] == ["jarnathan", "hope", "jarnathan"]
    assert [item.music_id for item in items] == ["music_a", "music_b", "music_a"]
    assert [item.seed for item in items] == [100, 101, 102]
    assert all(
        item.output_dir.endswith(f"video_{i:03d}")
        for i, item in enumerate(items, 1)
    )
    assert all("terranea_resort" in item.output_path for item in items)


def test_run_batch_executes_independent_one_video_runs(tmp_path):
    from promo.cli.run_batch import run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [{
                "poi_id": "poi_123",
                "name": "Terranea Resort",
                "location": "Rancho Palos Verdes",
            }],
            "videos_per_poi": 2,
            "target_duration_sec": 65,
            "voices": ["jarnathan", "hope"],
        }),
        encoding="utf-8",
    )
    commands = []

    def command_runner(command):
        commands.append(list(command))
        return 0

    def music_id_resolver(**kwargs):
        assert kwargs == {"target_duration_sec": 65.0, "count": 2}
        return ["music_a", "music_b"]

    exit_code = run_batch(
        batch_path=str(batch_path),
        output_root=str(tmp_path / "out"),
        use_music_library=True,
        seed=10,
        command_runner=command_runner,
        music_id_resolver=music_id_resolver,
    )

    assert exit_code == 0
    assert len(commands) == 2
    assert all("--n-variants" in command for command in commands)
    assert [command[command.index("--n-variants") + 1] for command in commands] == [
        "1",
        "1",
    ]
    assert [command[command.index("--voice") + 1] for command in commands] == [
        "jarnathan",
        "hope",
    ]
    assert [command[command.index("--seed") + 1] for command in commands] == [
        "10",
        "11",
    ]
    assert [command[command.index("--supabase-music-id") + 1] for command in commands] == [
        "music_a",
        "music_b",
    ]
    assert all("--supabase-poi-id" in command for command in commands)
    assert all(command[command.index("--supabase-poi-id") + 1] == "poi_123" for command in commands)
    assert commands[0][commands[0].index("--output") + 1] != commands[1][
        commands[1].index("--output") + 1
    ]
    receipt = json.loads((tmp_path / "out" / "RUN_RECEIPT.json").read_text())
    assert receipt["receipt_kind"] == "pgc_batch_run_receipt"
    assert receipt["paradigm"] == "pgc_65s"
    assert receipt["request"]["mode"] == "render_only_current_implementation"
    assert receipt["request"]["filters"]["required_active_assets"] == 60
    assert receipt["summary"]["requested_videos"] == 2
    assert receipt["summary"]["rendered_videos"] == 2
    assert receipt["summary"]["usage_written_videos"] == 0
    assert receipt["summary"]["release_candidates_created"] == 0
    assert [video["state"] for video in receipt["videos"]] == [
        "rendered_manifest_missing",
        "rendered_manifest_missing",
    ]
    assert {
        video["drive_upload"]["status"] for video in receipt["videos"]
    } == {"not_implemented"}


def test_run_batch_returns_failure_when_any_item_fails(tmp_path):
    from promo.cli.run_batch import run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [{"poi_id": "poi_123", "name": "Terranea Resort"}],
            "videos_per_poi": 2,
        }),
        encoding="utf-8",
    )
    results = iter([True, False])

    exit_code = run_batch(
        batch_path=str(batch_path),
        output_root=str(tmp_path / "out"),
        target_duration_sec=65,
        command_runner=lambda command: 0 if next(results) else 1,
    )

    assert exit_code == 1
    receipt = json.loads((tmp_path / "out" / "RUN_RECEIPT.json").read_text())
    assert receipt["summary"]["requested_videos"] == 2
    assert receipt["summary"]["rendered_videos"] == 1
    assert receipt["summary"]["failed_videos"] == 1
    assert [video["state"] for video in receipt["videos"]] == [
        "rendered_manifest_missing",
        "render_failed",
    ]
    assert receipt["videos"][1]["error"] == "compile_promo exited with code 1"


def test_run_batch_receipt_records_manifest_identity_when_present(tmp_path):
    from promo.cli.run_batch import run_batch
    from promo.core.pipeline.run_manifest import build_run_manifest

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [{"poi_id": "poi_123", "name": "Terranea Resort"}],
            "videos_per_poi": 1,
        }),
        encoding="utf-8",
    )

    def command_runner(command):
        output_path = command[command.index("--output") + 1]
        output_dir = tmp_path / "out" / "terranea_resort" / "video_001"
        assert str(output_dir) in output_path
        manifest_path = output_dir / "run_manifest_terranea_resort_65s.json"
        manifest = build_run_manifest(
            poi_name="Terranea Resort",
            location="",
            target_duration_sec=65,
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
                "render_output_path": output_path,
                "final_output_path": output_path,
                "target_duration_sec": 65,
                "music_label": "Run Away with Me",
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
            sidecar_paths={},
            run_id="pgc_run_123",
            manifest_id="manifest_123",
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return 0

    exit_code = run_batch(
        batch_path=str(batch_path),
        output_root=str(tmp_path / "out"),
        target_duration_sec=65,
        command_runner=command_runner,
    )

    assert exit_code == 0
    receipt = json.loads((tmp_path / "out" / "RUN_RECEIPT.json").read_text())
    assert receipt["summary"]["manifest_found_videos"] == 1
    assert receipt["summary"]["manifest_audited_videos"] == 1
    assert receipt["summary"]["manifest_audit_failed_videos"] == 0
    video = receipt["videos"][0]
    assert video["state"] == "rendered_manifest_audited"
    assert video["manifest"]["status"] == "found"
    assert video["manifest"]["manifest_id"] == "manifest_123"
    assert video["manifest"]["run_id"] == "pgc_run_123"
    assert video["manifest_audit"]["status"] == "passed"
    assert video["manifest_audit"]["summary"]["usage_event_count"] == 1


def test_run_batch_receipt_records_manifest_audit_failure(tmp_path):
    from promo.cli.run_batch import run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [{"poi_id": "poi_123", "name": "Terranea Resort"}],
            "videos_per_poi": 1,
        }),
        encoding="utf-8",
    )

    def command_runner(command):
        output_dir = tmp_path / "out" / "terranea_resort" / "video_001"
        manifest_path = output_dir / "run_manifest_terranea_resort_65s.json"
        manifest_path.write_text(
            json.dumps({
                "schema_version": 1,
                "manifest_id": "manifest_123",
                "run_id": "pgc_run_123",
                "poi": {
                    "poi_id": "poi_123",
                    "display_name": "Terranea Resort",
                },
                "asset_snapshot": [{
                    "clip_id": "0001",
                    "asset_id": "asset_abc",
                }],
                "outputs": [{
                    "variant_index": 1,
                    "output_path": command[command.index("--output") + 1],
                }],
                "timeline_entries": [],
            }),
            encoding="utf-8",
        )
        return 0

    exit_code = run_batch(
        batch_path=str(batch_path),
        output_root=str(tmp_path / "out"),
        target_duration_sec=65,
        command_runner=command_runner,
    )

    assert exit_code == 1
    receipt = json.loads((tmp_path / "out" / "RUN_RECEIPT.json").read_text())
    video = receipt["videos"][0]
    assert video["state"] == "rendered_manifest_audit_failed"
    assert video["manifest_audit"]["status"] == "failed"
    assert video["error"] == "manifest audit failed"
    assert receipt["summary"]["manifest_audit_failed_videos"] == 1


def test_run_batch_rejects_parallel_jobs_until_safe(tmp_path):
    from promo.cli.run_batch import run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({"pois": [{"poi_id": "poi_123", "name": "Terranea Resort"}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="jobs 1"):
        run_batch(
            batch_path=str(batch_path),
            output_root=str(tmp_path / "out"),
            target_duration_sec=65,
            jobs=2,
            command_runner=lambda command: 0,
        )


def test_build_compile_command_uses_canonical_key_and_music_library(tmp_path):
    from promo.cli.run_batch import BatchItem, BatchPoi, build_compile_command

    item = BatchItem(
        poi=BatchPoi(
            name="Hotel Maya",
            location="Long Beach",
            poi_id=None,
            canonical_key="hotel_maya",
        ),
        video_index=1,
        output_dir=str(tmp_path),
        output_path=str(tmp_path / "promo.mp4"),
        voice_key="hope",
        music_id=None,
        seed=None,
    )

    command = build_compile_command(
        item=item,
        target_duration_sec=65,
        use_music_library=True,
        script_candidates=2,
        tts_speed=0.92,
    )

    assert command[1:3] == ["-m", "promo.cli.compile_promo"]
    assert command[command.index("--supabase-canonical-key") + 1] == "hotel_maya"
    assert "--supabase-music-library" in command
    assert "--supabase-music-id" not in command
    assert command[command.index("--script-candidates") + 1] == "2"
    assert command[command.index("--tts-speed") + 1] == "0.92"
