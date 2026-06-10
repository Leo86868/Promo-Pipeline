import json

import pytest


class _FakeUploader:
    def __init__(self):
        self.uploads = []

    def ensure_batch_folder(
        self,
        *,
        parent_folder_id,
        parent_folder_name,
        paradigm,
        date,
        batch_id,
    ):
        self.folder_context = {
            "parent_folder_id": parent_folder_id,
            "parent_folder_name": parent_folder_name,
            "paradigm": paradigm,
            "date": date,
            "batch_id": batch_id,
        }
        return {"id": "drive-folder", "name": batch_id}

    def upload_video_once(self, *, local_path, folder_id, filename=None):
        self.uploads.append({
            "local_path": local_path,
            "folder_id": folder_id,
            "filename": filename,
        })
        return {
            "id": f"drive-{filename}",
            "name": filename,
            "size": "3",
            "mimeType": "video/mp4",
            "reused_existing": False,
        }


class _FakeUpscaler:
    def __init__(self):
        self.calls = []

    def upscale(self, *, input_path, output_path):
        from pathlib import Path

        self.calls.append({"input_path": input_path, "output_path": output_path})
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(Path(input_path).read_bytes() + b"-upscaled")
        return {
            "status": "applied",
            "provider": "wavespeed",
            "input_path": input_path,
            "output_path": output_path,
        }


def _write_valid_manifest_from_command(command, *, video_index=1):
    from promo.core.pipeline.run_manifest import build_run_manifest
    from pathlib import Path

    output_path = command[command.index("--output") + 1]
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_bytes(b"mp4")
    manifest_path = output_file.parent / f"run_manifest_terranea_resort_{video_index:03d}.json"
    manifest = build_run_manifest(
        poi_name="Terranea Resort",
        location="Rancho Palos Verdes",
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
            "asset_id": f"asset_{video_index:03d}",
            "clip_id": "0001",
            "source_storage_bucket": "poi-assets",
            "source_storage_path": f"poi_123/clips/asset_{video_index:03d}.mp4",
            "source_content_hash": "sha256:" + "a" * 64,
            "duration_sec": 5.0,
        }],
        rendered_outputs=[{
            "variant_index": 1,
            "render_output_path": str(output_file),
            "final_output_path": str(output_file),
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
        run_id=f"pgc_run_{video_index:03d}",
        manifest_id=f"manifest_{video_index:03d}",
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _selection_rows(*, poi_count=2, assets_per_poi=60):
    rows = []
    for poi_index in range(1, poi_count + 1):
        poi_id = f"poi_{poi_index}"
        for asset_index in range(1, assets_per_poi + 1):
            asset_id = f"asset_{poi_index}{asset_index:03d}"
            rows.append({
                "poi_id": poi_id,
                "display_name": f"Selection Resort {poi_index}",
                "canonical_key": f"selection_resort_{poi_index}",
                "location": "California",
                "asset_id": asset_id,
                "clip_id": f"{asset_index:04d}",
                "source_storage_bucket": "poi-assets",
                "source_storage_path": f"{poi_id}/clips/{asset_id}.mp4",
                "source_content_hash": "sha256:" + "a" * 64,
                "duration_sec": 5.0,
            })
    return rows


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
        # 2026-06-09: rotation seed threads through (batch seed when set).
        assert kwargs == {"target_duration_sec": 65.0, "count": 2, "shuffle_seed": 10}
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


def test_prepare_selected_batch_writes_batch_and_summary(tmp_path):
    from promo.cli.run_batch import prepare_selected_batch

    prepared = prepare_selected_batch(
        output_root=str(tmp_path / "out"),
        poi_count=1,
        videos_per_poi=2,
        target_duration_sec=65,
        cooldown_days=3,
        seed=7,
        client_factory=lambda: object(),
        valid_clip_rows_fetcher=lambda client: _selection_rows(),
        ready_embedding_asset_ids_fetcher=lambda client, asset_ids: set(asset_ids),
        recent_usage_poi_ids_fetcher=lambda client, **kwargs: set(),
    )

    batch = json.loads((tmp_path / "out" / "batch.json").read_text())
    summary = json.loads((tmp_path / "out" / "selection_summary.json").read_text())
    assert prepared.batch_path == str(tmp_path / "out" / "batch.json")
    assert prepared.selection_summary_path == str(tmp_path / "out" / "selection_summary.json")
    assert batch["videos_per_poi"] == 2
    assert batch["target_duration_sec"] == 65.0
    assert batch["selection"]["mode"] == "random_equal"
    assert batch["selection"]["selection_summary_path"] == prepared.selection_summary_path
    assert summary["request"]["filters"]["required_active_assets"] == 60
    assert len(summary["selected_pois"]) == 1
    assert summary["batch_spec"] == batch


def test_run_selected_batch_generates_batch_then_runs(tmp_path):
    from promo.cli.run_batch import run_selected_batch

    commands = []

    exit_code = run_selected_batch(
        output_root=str(tmp_path / "out"),
        poi_count=1,
        videos_per_poi=1,
        target_duration_sec=65,
        seed=9,
        command_runner=lambda command: commands.append(list(command)) or 0,
        selection_client_factory=lambda: object(),
        valid_clip_rows_fetcher=lambda client: _selection_rows(
            poi_count=1,
            assets_per_poi=50,
        ),
        ready_embedding_asset_ids_fetcher=lambda client, asset_ids: set(asset_ids),
        recent_usage_poi_ids_fetcher=lambda client, **kwargs: set(),
    )

    assert exit_code == 0
    assert len(commands) == 1
    assert "--supabase-poi-id" in commands[0]
    assert commands[0][commands[0].index("--supabase-poi-id") + 1] == "poi_1"
    receipt = json.loads((tmp_path / "out" / "RUN_RECEIPT.json").read_text())
    assert receipt["request"]["selection"] == "random_equal"
    assert receipt["request"]["selection_metadata"]["selection_summary_path"] == str(
        tmp_path / "out" / "selection_summary.json"
    )
    assert receipt["request"]["filters"]["required_active_assets"] == 50
    assert receipt["selected_pois"][0]["poi_id"] == "poi_1"


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


def test_run_batch_production_autopilot_registers_successful_video(tmp_path):
    from promo.cli.run_batch import DriveUploadTarget, run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [{
                "poi_id": "poi_123",
                "name": "Terranea Resort",
                "location": "Rancho Palos Verdes",
            }],
            "videos_per_poi": 1,
            "target_duration_sec": 65,
        }),
        encoding="utf-8",
    )
    fake_uploader = _FakeUploader()
    usage_calls = []
    release_calls = []

    def command_runner(command):
        _write_valid_manifest_from_command(command, video_index=1)
        return 0

    def usage_recorder(client, events):
        usage_calls.append(events)
        return {"inserted_count": len(events), "duplicate_count": 0}

    def usage_verifier(client, events):
        return {
            "verified": True,
            "expected_count": len(events),
            "observed_count": len(events),
            "missing_count": 0,
            "mismatch_count": 0,
            "duplicate_observed_event_id_count": 0,
            "missing_event_ids": [],
            "mismatches": [],
            "duplicate_observed_event_ids": [],
        }

    def release_registrar(client, records):
        release_calls.append(records)
        return {
            "inserted_count": len(records),
            "already_registered_count": 0,
            "verification": {
                "verified": True,
                "expected_count": len(records),
                "observed_count": len(records),
                "missing_count": 0,
                "mismatch_count": 0,
                "duplicate_observed_key_count": 0,
                "missing_source_video_keys": [],
                "mismatches": [],
                "duplicate_observed_source_video_keys": [],
            },
        }

    exit_code = run_batch(
        batch_path=str(batch_path),
        output_root=str(tmp_path / "out"),
        target_duration_sec=65,
        production_autopilot=True,
        command_runner=command_runner,
        drive_upload_target_factory=lambda: DriveUploadTarget(
            uploader=fake_uploader,
            parent_folder_id="parent-folder",
            parent_folder_name="AIGC Production Masters",
        ),
        supabase_client_factory=lambda: object(),
        usage_recorder=usage_recorder,
        usage_verifier=usage_verifier,
        release_registrar=release_registrar,
    )

    assert exit_code == 0
    assert len(usage_calls) == 1
    assert len(release_calls) == 1
    receipt = json.loads((tmp_path / "out" / "RUN_RECEIPT.json").read_text())
    video = receipt["videos"][0]
    assert receipt["request"]["mode"] == "production_autopilot"
    assert receipt["summary"]["rendered_videos"] == 1
    assert receipt["summary"]["drive_uploaded_videos"] == 1
    assert receipt["summary"]["usage_written_videos"] == 1
    assert receipt["summary"]["release_candidates_created"] == 1
    assert video["state"] == "complete"
    assert video["drive_upload"]["source_output_uri"].startswith("drive:")
    assert video["usage"]["writeback_status"] == "verified"
    assert video["release_candidate"]["status"] == "verified"
    assert (tmp_path / "out" / "handoff").exists()
    assert len(list((tmp_path / "out" / "handoff").glob("*_drive_inventory.json"))) == 1
    assert release_calls[0][0]["source_output_uri"].startswith("drive:")
    assert release_calls[0][0]["source_batch_id"] == receipt["batch_id"]


def test_run_batch_final_upscale_required_fails_before_drive_usage_or_release(tmp_path):
    from promo.cli.run_batch import DriveUploadTarget, run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [{"poi_id": "poi_123", "name": "Terranea Resort"}],
            "videos_per_poi": 1,
            "target_duration_sec": 65,
            "source_resolution_policy": {
                "mode": "transition_low_res_only",
                "target_width": 720,
                "tolerance_px": 40,
            },
        }),
        encoding="utf-8",
    )
    fake_uploader = _FakeUploader()
    commands = []

    def command_runner(command):
        commands.append(list(command))
        _write_valid_manifest_from_command(command, video_index=1)
        return 0

    exit_code = run_batch(
        batch_path=str(batch_path),
        output_root=str(tmp_path / "out"),
        target_duration_sec=65,
        production_autopilot=True,
        command_runner=command_runner,
        drive_upload_target_factory=lambda: DriveUploadTarget(
            uploader=fake_uploader,
            parent_folder_id=None,
            parent_folder_name="AIGC Production Masters",
        ),
        supabase_client_factory=lambda: object(),
        usage_recorder=lambda client, events: pytest.fail("usage should not run"),
        usage_verifier=lambda client, events: pytest.fail("usage verify should not run"),
        release_registrar=lambda client, records: pytest.fail("release should not run"),
        final_upscaler_factory=lambda policy: None,
    )

    # 2026-06-09 preflight fix: a required-but-unconfigured upscaler now
    # fails the batch BEFORE the first render (previously each video
    # rendered, then failed at the upscale gate).
    assert exit_code == 1
    assert commands == []
    assert fake_uploader.uploads == []
    receipt = json.loads((tmp_path / "out" / "RUN_RECEIPT.json").read_text())
    assert receipt["request"]["filters"]["final_upscale_policy"]["required"] is True
    assert receipt["preflight"]["status"] == "failed"
    assert "PGC_WAVESPEED_UPSCALE_COMMAND" in receipt["preflight"]["errors"][0]
    assert receipt["summary"]["rendered_videos"] == 0
    assert receipt["summary"]["drive_uploaded_videos"] == 0
    assert receipt["summary"]["usage_written_videos"] == 0
    assert receipt["summary"]["release_candidates_created"] == 0


def test_run_batch_final_upscale_uploads_verified_upscaled_master(tmp_path):
    from promo.cli.run_batch import DriveUploadTarget, run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [{"poi_id": "poi_123", "name": "Terranea Resort"}],
            "videos_per_poi": 1,
            "target_duration_sec": 65,
            "source_resolution_policy": {
                "mode": "transition_low_res_only",
                "target_width": 720,
                "tolerance_px": 40,
            },
        }),
        encoding="utf-8",
    )
    fake_uploader = _FakeUploader()
    fake_upscaler = _FakeUpscaler()
    usage_calls = []
    release_calls = []

    def command_runner(command):
        _write_valid_manifest_from_command(command, video_index=1)
        return 0

    def final_upscale_verifier(*, output_path, policy, **kwargs):
        from pathlib import Path

        path = Path(output_path)
        return {
            "verified": True,
            "path": output_path,
            "file_size_bytes": path.stat().st_size,
            "width": policy.target_width,
            "height": policy.target_height,
            "target_width": policy.target_width,
            "target_height": policy.target_height,
        }

    exit_code = run_batch(
        batch_path=str(batch_path),
        output_root=str(tmp_path / "out"),
        target_duration_sec=65,
        production_autopilot=True,
        command_runner=command_runner,
        drive_upload_target_factory=lambda: DriveUploadTarget(
            uploader=fake_uploader,
            parent_folder_id="parent-folder",
            parent_folder_name="AIGC Production Masters",
        ),
        supabase_client_factory=lambda: object(),
        usage_recorder=lambda client, events: usage_calls.append(events) or {
            "inserted_count": len(events),
            "duplicate_count": 0,
        },
        usage_verifier=lambda client, events: {
            "verified": True,
            "expected_count": len(events),
            "observed_count": len(events),
            "missing_count": 0,
            "mismatch_count": 0,
            "duplicate_observed_event_id_count": 0,
            "missing_event_ids": [],
            "mismatches": [],
            "duplicate_observed_event_ids": [],
        },
        release_registrar=lambda client, records: release_calls.append(records) or {
            "inserted_count": len(records),
            "already_registered_count": 0,
            "verification": {
                "verified": True,
                "expected_count": len(records),
                "observed_count": len(records),
                "missing_count": 0,
                "mismatch_count": 0,
                "duplicate_observed_key_count": 0,
                "missing_source_video_keys": [],
                "mismatches": [],
                "duplicate_observed_source_video_keys": [],
            },
        },
        final_upscaler_factory=lambda policy: fake_upscaler,
        final_upscale_verifier=final_upscale_verifier,
    )

    assert exit_code == 0
    assert len(fake_upscaler.calls) == 1
    assert len(fake_uploader.uploads) == 1
    assert fake_uploader.uploads[0]["local_path"].endswith("_wavespeed_1080p.mp4")
    assert len(usage_calls) == 1
    assert len(release_calls) == 1
    receipt = json.loads((tmp_path / "out" / "RUN_RECEIPT.json").read_text())
    video = receipt["videos"][0]
    assert video["state"] == "complete"
    assert video["final_upscale"]["status"] == "verified"
    assert video["drive_upload"]["source_output_uri"].startswith("drive:")
    assert release_calls[0][0]["source_output_uri"].startswith("drive:")


def test_run_batch_production_autopilot_quarantines_poi_on_usage_failure(tmp_path):
    from promo.cli.run_batch import DriveUploadTarget, run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [{"poi_id": "poi_123", "name": "Terranea Resort"}],
            "videos_per_poi": 2,
            "target_duration_sec": 65,
        }),
        encoding="utf-8",
    )
    commands = []

    def command_runner(command):
        commands.append(command)
        _write_valid_manifest_from_command(command, video_index=len(commands))
        return 0

    def usage_verifier(client, events):
        return {
            "verified": False,
            "expected_count": len(events),
            "observed_count": 0,
            "missing_count": len(events),
            "mismatch_count": 0,
            "duplicate_observed_event_id_count": 0,
            "missing_event_ids": [event["event_id"] for event in events],
            "mismatches": [],
            "duplicate_observed_event_ids": [],
        }

    exit_code = run_batch(
        batch_path=str(batch_path),
        output_root=str(tmp_path / "out"),
        target_duration_sec=65,
        production_autopilot=True,
        command_runner=command_runner,
        drive_upload_target_factory=lambda: DriveUploadTarget(
            uploader=_FakeUploader(),
            parent_folder_id=None,
            parent_folder_name="AIGC Production Masters",
        ),
        supabase_client_factory=lambda: object(),
        usage_recorder=lambda client, events: {
            "inserted_count": len(events),
            "duplicate_count": 0,
        },
        usage_verifier=usage_verifier,
        release_registrar=lambda client, records: pytest.fail(
            "release registration should not run after usage failure"
        ),
    )

    assert exit_code == 1
    assert len(commands) == 1
    receipt = json.loads((tmp_path / "out" / "RUN_RECEIPT.json").read_text())
    assert receipt["quarantined_pois"] == [{
        "poi_key": "poi_123",
        "poi_id": "poi_123",
        "canonical_key": None,
        "poi_name": "Terranea Resort",
        "reason": "usage writeback verification failed",
    }]
    assert [video["state"] for video in receipt["videos"]] == [
        "usage_writeback_failed",
        "skipped_quarantined_poi",
    ]
    assert receipt["summary"]["drive_uploaded_videos"] == 1
    assert receipt["summary"]["usage_failed_videos"] == 1
    assert receipt["summary"]["quarantined_skipped_videos"] == 1


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


def test_build_compile_command_threads_source_resolution_policy(tmp_path):
    from promo.cli.run_batch import BatchItem, BatchPoi, build_compile_command

    item = BatchItem(
        poi=BatchPoi(
            name="Hotel Maya",
            location="Long Beach",
            poi_id="poi_123",
            canonical_key=None,
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
        script_candidates=1,
        tts_speed=0.95,
        source_resolution_policy={
            "mode": "transition_low_res_only",
            "target_width": 720,
            "tolerance_px": 40,
        },
    )

    assert command[command.index("--source-resolution-policy-mode") + 1] == (
        "transition_low_res_only"
    )
    assert command[command.index("--source-target-width") + 1] == "720"


def test_plan_batch_items_rotates_music_by_global_ordinal(tmp_path):
    """2026-06-09 fix: music rotates by global item ordinal so video_001 of
    every POI no longer pins the same track; a pool larger than
    videos_per_poi gets fully cycled across the batch."""
    from promo.cli.run_batch import BatchPoi, plan_batch_items

    pois = [
        BatchPoi(name=f"Hotel {i}", location="X", poi_id=f"poi_{i}", canonical_key=None)
        for i in range(1, 3)
    ]
    items = plan_batch_items(
        pois=pois,
        videos_per_poi=3,
        target_duration_sec=65,
        output_root=str(tmp_path),
        voices=["jarnathan"],
        music_ids=[f"music_{n}" for n in range(1, 8)],  # 7-track pool
        base_seed=None,
    )

    assert [item.music_id for item in items] == [
        "music_1", "music_2", "music_3",  # POI 1
        "music_4", "music_5", "music_6",  # POI 2 — no longer repeats POI 1
    ]


def test_autopilot_preflight_fails_before_any_render(tmp_path):
    """2026-06-09 preflight fix: a bad Drive/Supabase credential or missing
    upscale command aborts the batch BEFORE the first render, instead of
    being discovered after hours of render + LLM/TTS spend."""
    from promo.cli.run_batch import run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [{
                "poi_id": "poi_123",
                "name": "Terranea Resort",
                "location": "Rancho Palos Verdes",
            }],
            "videos_per_poi": 1,
            "target_duration_sec": 65,
            "voices": ["jarnathan"],
        }),
        encoding="utf-8",
    )
    commands = []

    def command_runner(command):
        commands.append(list(command))
        return 0

    def broken_drive_factory():
        raise RuntimeError("token has been expired or revoked")

    exit_code = run_batch(
        batch_path=str(batch_path),
        output_root=str(tmp_path / "out"),
        production_autopilot=True,
        command_runner=command_runner,
        drive_upload_target_factory=broken_drive_factory,
        supabase_client_factory=lambda: object(),
    )

    assert exit_code == 1
    assert commands == []  # nothing rendered, nothing spent
    receipt = json.loads((tmp_path / "out" / "RUN_RECEIPT.json").read_text())
    assert receipt["preflight"]["status"] == "failed"
    assert "expired" in receipt["preflight"]["errors"][0]


def test_autopilot_preflight_requires_upscaler_when_policy_enabled(tmp_path):
    from promo.cli.run_batch import run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [{
                "poi_id": "poi_123",
                "name": "Terranea Resort",
                "location": "Rancho Palos Verdes",
            }],
            "videos_per_poi": 1,
            "target_duration_sec": 65,
            "voices": ["jarnathan"],
            "final_upscale_policy": {"required": True, "enabled": True, "provider": "wavespeed"},
        }),
        encoding="utf-8",
    )
    commands = []

    exit_code = run_batch(
        batch_path=str(batch_path),
        output_root=str(tmp_path / "out"),
        production_autopilot=True,
        command_runner=lambda command: commands.append(command) or 0,
        drive_upload_target_factory=lambda: _FakeUploader(),
        supabase_client_factory=lambda: object(),
        final_upscaler_factory=lambda policy: None,  # PGC_WAVESPEED_UPSCALE_COMMAND unset
    )

    assert exit_code == 1
    assert commands == []
    receipt = json.loads((tmp_path / "out" / "RUN_RECEIPT.json").read_text())
    assert receipt["preflight"]["status"] == "failed"
    assert "PGC_WAVESPEED_UPSCALE_COMMAND" in receipt["preflight"]["errors"][0]


def test_receipt_records_render_stage_timings(tmp_path):
    """2026-06-09 observability fix: every video record carries per-stage
    started_at/finished_at/duration_sec under `timings`."""
    from promo.cli.run_batch import run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [{
                "poi_id": "poi_123",
                "name": "Terranea Resort",
                "location": "Rancho Palos Verdes",
            }],
            "videos_per_poi": 1,
            "target_duration_sec": 65,
            "voices": ["jarnathan"],
        }),
        encoding="utf-8",
    )

    exit_code = run_batch(
        batch_path=str(batch_path),
        output_root=str(tmp_path / "out"),
        command_runner=lambda command: 1,  # render fails; timing still lands
    )

    assert exit_code == 1
    receipt = json.loads((tmp_path / "out" / "RUN_RECEIPT.json").read_text())
    timings = receipt["videos"][0]["timings"]
    assert timings["render"]["started_at"]
    assert timings["render"]["finished_at"]
    assert timings["render"]["duration_sec"] is not None


def _autopilot_fakes():
    """Shared happy-path fakes for usage/release (mirrors the registers test)."""
    usage_calls = []
    release_calls = []

    def usage_recorder(client, events):
        usage_calls.append(events)
        return {"inserted_count": len(events), "duplicate_count": 0}

    def usage_verifier(client, events):
        return {
            "verified": True,
            "expected_count": len(events),
            "observed_count": len(events),
            "missing_count": 0,
            "mismatch_count": 0,
            "duplicate_observed_event_id_count": 0,
            "missing_event_ids": [],
            "mismatches": [],
            "duplicate_observed_event_ids": [],
        }

    def release_registrar(client, records):
        release_calls.append(records)
        return {
            "inserted_count": len(records),
            "already_registered_count": 0,
            "verification": {
                "verified": True,
                "expected_count": len(records),
                "observed_count": len(records),
                "missing_count": 0,
                "mismatch_count": 0,
                "duplicate_observed_key_count": 0,
                "missing_source_video_keys": [],
                "mismatches": [],
                "duplicate_observed_source_video_keys": [],
            },
        }

    return usage_recorder, usage_verifier, release_registrar, usage_calls, release_calls


def test_resume_skips_complete_videos_and_rerenders_failures(tmp_path):
    """2026-06-10 resume fix: --resume replays only what needs work — done
    videos are untouched, failed renders replay their recorded command."""
    from promo.cli.run_batch import resume_batch, run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [{"poi_id": "poi_123", "name": "Terranea Resort"}],
            "videos_per_poi": 2,
            "target_duration_sec": 65,
        }),
        encoding="utf-8",
    )

    def first_runner(command):
        index = 1 if "video_001" in command[command.index("--output") + 1] else 2
        if index == 1:
            _write_valid_manifest_from_command(command, video_index=1)
            return 0
        return 1  # video 2 dies

    assert run_batch(
        batch_path=str(batch_path),
        output_root=str(tmp_path / "out"),
        target_duration_sec=65,
        command_runner=first_runner,
    ) == 1
    receipt_path = tmp_path / "out" / "RUN_RECEIPT.json"
    before = json.loads(receipt_path.read_text())
    assert before["videos"][0]["state"] == "rendered_manifest_audited"
    assert before["videos"][1]["state"] == "render_failed"
    original_command = before["videos"][1]["render"]["command"]

    resumed_commands = []

    def resume_runner(command):
        resumed_commands.append(list(command))
        _write_valid_manifest_from_command(command, video_index=2)
        return 0

    assert resume_batch(
        receipt_path=str(receipt_path),
        command_runner=resume_runner,
    ) == 0
    after = json.loads(receipt_path.read_text())
    # Only the failed video re-rendered, with its exact recorded command.
    assert resumed_commands == [original_command]
    assert after["videos"][0]["state"] == "rendered_manifest_audited"
    assert after["videos"][1]["state"] == "rendered_manifest_audited"
    assert after["resume_history"][0]["plan"] == {"skip": 1, "tail": 0, "render": 1}


def test_resume_tail_only_keeps_original_manifest_no_rerender(tmp_path):
    """The double-spend guard: a drive_upload_failed video resumes from the
    tail with its ORIGINAL manifest (same usage event ids), and the render
    command is never replayed."""
    from promo.cli.run_batch import DriveUploadTarget, resume_batch, run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [{"poi_id": "poi_123", "name": "Terranea Resort"}],
            "videos_per_poi": 1,
            "target_duration_sec": 65,
        }),
        encoding="utf-8",
    )
    usage_recorder, usage_verifier, release_registrar, usage_calls, _ = _autopilot_fakes()

    class _ExplodingUploader(_FakeUploader):
        def upload_video_once(self, **kwargs):
            raise RuntimeError("Drive 503 backend error")

    def command_runner(command):
        _write_valid_manifest_from_command(command, video_index=1)
        return 0

    assert run_batch(
        batch_path=str(batch_path),
        output_root=str(tmp_path / "out"),
        target_duration_sec=65,
        production_autopilot=True,
        command_runner=command_runner,
        drive_upload_target_factory=lambda: DriveUploadTarget(
            uploader=_ExplodingUploader(),
            parent_folder_id="parent-folder",
            parent_folder_name="AIGC Production Masters",
        ),
        supabase_client_factory=lambda: object(),
        usage_recorder=usage_recorder,
        usage_verifier=usage_verifier,
        release_registrar=release_registrar,
    ) == 1
    receipt_path = tmp_path / "out" / "RUN_RECEIPT.json"
    before = json.loads(receipt_path.read_text())
    assert before["videos"][0]["state"] == "drive_upload_failed"
    original_manifest_id = before["videos"][0]["manifest"]["manifest_id"]
    assert original_manifest_id
    assert usage_calls == []  # usage never ran in the failed attempt

    assert resume_batch(
        receipt_path=str(receipt_path),
        command_runner=lambda command: pytest.fail("must not re-render"),
        drive_upload_target_factory=lambda: DriveUploadTarget(
            uploader=_FakeUploader(),
            parent_folder_id="parent-folder",
            parent_folder_name="AIGC Production Masters",
        ),
        supabase_client_factory=lambda: object(),
        usage_recorder=usage_recorder,
        usage_verifier=usage_verifier,
        release_registrar=release_registrar,
    ) == 0
    after = json.loads(receipt_path.read_text())
    assert after["videos"][0]["state"] == "complete"
    assert after["resume_history"][0]["plan"] == {"skip": 0, "tail": 1, "render": 0}
    # Usage events were written against the ORIGINAL manifest — no new
    # manifest_id was minted, so the usage ledger cannot double-spend.
    assert len(usage_calls) == 1
    assert all(e["manifest_id"] == original_manifest_id for e in usage_calls[0])


def test_resume_reuses_verified_upscale_output_without_repaying(tmp_path):
    """A video that failed AFTER a successful paid upscale resumes without
    a second upscale call — the verified output on disk is reused."""
    from promo.cli.run_batch import DriveUploadTarget, resume_batch, run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [{"poi_id": "poi_123", "name": "Terranea Resort"}],
            "videos_per_poi": 1,
            "target_duration_sec": 65,
            "final_upscale_policy": {
                "required": True, "enabled": True, "provider": "wavespeed",
            },
        }),
        encoding="utf-8",
    )
    usage_recorder, usage_verifier, _, _, _ = _autopilot_fakes()

    def command_runner(command):
        _write_valid_manifest_from_command(command, video_index=1)
        return 0

    def failing_registrar(client, records):
        raise RuntimeError("400 Client Error: bad release payload")

    upscaler = _FakeUpscaler()
    common = dict(
        drive_upload_target_factory=lambda: DriveUploadTarget(
            uploader=_FakeUploader(),
            parent_folder_id="parent-folder",
            parent_folder_name="AIGC Production Masters",
        ),
        supabase_client_factory=lambda: object(),
        usage_recorder=usage_recorder,
        usage_verifier=usage_verifier,
        final_upscaler_factory=lambda policy: upscaler,
        final_upscale_verifier=lambda *, output_path, policy, **kwargs: {
            "verified": True, "path": output_path,
            "file_size_bytes": 3, "width": 1080, "height": 1920,
            "target_width": 1080, "target_height": 1920,
        },
    )

    assert run_batch(
        batch_path=str(batch_path),
        output_root=str(tmp_path / "out"),
        target_duration_sec=65,
        production_autopilot=True,
        command_runner=command_runner,
        release_registrar=failing_registrar,
        **common,
    ) == 1
    receipt_path = tmp_path / "out" / "RUN_RECEIPT.json"
    before = json.loads(receipt_path.read_text())
    assert before["videos"][0]["state"] == "release_candidate_failed_retryable"
    assert len(upscaler.calls) == 1  # paid once

    _, _, working_registrar, _, release_calls = (
        lambda r=_autopilot_fakes(): (r[0], r[1], r[2], r[3], r[4])
    )()

    assert resume_batch(
        receipt_path=str(receipt_path),
        command_runner=lambda command: pytest.fail("must not re-render"),
        release_registrar=working_registrar,
        **common,
    ) == 0
    after = json.loads(receipt_path.read_text())
    assert after["videos"][0]["state"] == "complete"
    assert len(upscaler.calls) == 1  # NOT paid a second time
    assert after["videos"][0]["final_upscale"]["result"]["status"] == "reused_existing"
    assert len(release_calls) == 1


def test_autopilot_preflight_runs_upscaler_deep_check(tmp_path):
    """2026-06-10 review fix: when the upscaler exposes preflight(), the
    batch preflight runs it — a bad runtime config (e.g. missing
    WAVESPEED_API_KEY inside the command's --env file) fails the batch
    before any render."""
    from promo.cli.run_batch import DriveUploadTarget, run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [{"poi_id": "poi_123", "name": "Terranea Resort"}],
            "videos_per_poi": 1,
            "target_duration_sec": 65,
            "final_upscale_policy": {
                "required": True, "enabled": True, "provider": "wavespeed",
            },
        }),
        encoding="utf-8",
    )

    class _BadConfigUpscaler(_FakeUpscaler):
        def preflight(self):
            raise RuntimeError("WAVESPEED_API_KEY missing")

    commands = []
    exit_code = run_batch(
        batch_path=str(batch_path),
        output_root=str(tmp_path / "out"),
        production_autopilot=True,
        command_runner=lambda command: commands.append(command) or 0,
        drive_upload_target_factory=lambda: DriveUploadTarget(
            uploader=_FakeUploader(),
            parent_folder_id="parent-folder",
            parent_folder_name="AIGC Production Masters",
        ),
        supabase_client_factory=lambda: object(),
        final_upscaler_factory=lambda policy: _BadConfigUpscaler(),
    )

    assert exit_code == 1
    assert commands == []
    receipt = json.loads((tmp_path / "out" / "RUN_RECEIPT.json").read_text())
    assert receipt["preflight"]["status"] == "failed"
    assert "WAVESPEED_API_KEY" in receipt["preflight"]["errors"][0]


def test_resume_derives_required_upscale_from_source_policy(tmp_path):
    """2026-06-10 review fix: a transition_low_res_only receipt that lacks
    an explicit final_upscale_policy must still REQUIRE upscale on resume
    (preflight demands an upscaler instead of silently skipping the gate)."""
    from promo.cli.run_batch import DriveUploadTarget, resume_batch

    receipt = {
        "schema_version": 1,
        "receipt_kind": "pgc_batch_run_receipt",
        "batch_id": "b1",
        "paradigm": "pgc_65s",
        "created_at": "2026-06-10T00:00:00Z",
        "updated_at": "2026-06-10T00:00:00Z",
        "request": {
            "output_root": str(tmp_path / "out"),
            "mode": "production_autopilot",
            "filters": {
                "source_resolution_policy": {
                    "mode": "transition_low_res_only",
                    "target_width": 720,
                    "tolerance_px": 40,
                },
                # final_upscale_policy intentionally ABSENT (older receipt)
            },
        },
        "videos": [{
            "poi_id": "poi_123", "poi_name": "Terranea Resort",
            "canonical_key": None, "video_index": 1,
            "state": "drive_upload_failed",
            "voice_key": "jarnathan", "music_id": None, "seed": None,
            "render": {"command": ["echo"], "output_path": "x.mp4",
                       "output_dir": str(tmp_path), "return_code": 0},
            "manifest": {"status": "found", "path": str(tmp_path / "m.json"),
                         "manifest_id": "m1", "run_id": "r1"},
            "manifest_audit": {"status": "passed", "passed": True, "error_count": 0},
            "drive_upload": {"status": "failed", "source_output_uri": None},
            "usage": {"writeback_status": "not_written", "event_count": 0},
            "release_candidate": {"status": "not_created", "id": None},
            "error": "Drive upload failed",
        }],
        "summary": {},
    }
    receipt_path = tmp_path / "RUN_RECEIPT.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    exit_code = resume_batch(
        receipt_path=str(receipt_path),
        command_runner=lambda command: pytest.fail("must not render"),
        drive_upload_target_factory=lambda: DriveUploadTarget(
            uploader=_FakeUploader(),
            parent_folder_id="parent-folder",
            parent_folder_name="AIGC Production Masters",
        ),
        supabase_client_factory=lambda: object(),
        final_upscaler_factory=lambda policy: None,  # not configured
    )

    # Required upscale was derived from the source policy → preflight fails
    # closed instead of resuming without the mandatory upscale gate.
    assert exit_code == 1
    after = json.loads(receipt_path.read_text())
    assert after["preflight"]["status"] == "failed"
    assert "PGC_WAVESPEED_UPSCALE_COMMAND" in after["preflight"]["errors"][0]


# --- Tail pipelining (2026-06-10) -------------------------------------------


def _ok_usage_verifier(client, events):
    return {
        "verified": True,
        "expected_count": len(events),
        "observed_count": len(events),
        "missing_count": 0,
        "mismatch_count": 0,
        "duplicate_observed_event_id_count": 0,
        "missing_event_ids": [],
        "mismatches": [],
        "duplicate_observed_event_ids": [],
    }


def _ok_release_registrar(client, records):
    return {
        "inserted_count": len(records),
        "already_registered_count": 0,
        "verification": {
            "verified": True,
            "expected_count": len(records),
            "observed_count": len(records),
            "missing_count": 0,
            "mismatch_count": 0,
            "duplicate_observed_key_count": 0,
            "missing_source_video_keys": [],
            "mismatches": [],
            "duplicate_observed_source_video_keys": [],
        },
    }


def _two_poi_batch(tmp_path, *, videos_per_poi=1):
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [
                {"poi_id": "poi_1", "name": "Resort One"},
                {"poi_id": "poi_2", "name": "Resort Two"},
            ],
            "videos_per_poi": videos_per_poi,
            "target_duration_sec": 65,
        }),
        encoding="utf-8",
    )
    return batch_path


def test_plan_batch_items_round_robins_pois_for_tail_pipelining(tmp_path):
    from promo.cli.run_batch import BatchPoi, plan_batch_items

    items = plan_batch_items(
        pois=[
            BatchPoi(name="Resort One", location="", poi_id="poi_1", canonical_key=None),
            BatchPoi(name="Resort Two", location="", poi_id="poi_2", canonical_key=None),
        ],
        videos_per_poi=2,
        target_duration_sec=65,
        output_root=str(tmp_path),
        voices=["jarnathan", "hope"],
        music_ids=["music_a", "music_b", "music_c"],
        base_seed=100,
    )

    # Adjacent items hit different POIs so render N+1 never waits on tail N.
    assert [(item.poi.poi_id, item.video_index) for item in items] == [
        ("poi_1", 1), ("poi_2", 1), ("poi_1", 2), ("poi_2", 2),
    ]
    # Music still rotates by global ordinal; seed by global ordinal.
    assert [item.music_id for item in items] == [
        "music_a", "music_b", "music_c", "music_a",
    ]
    assert [item.seed for item in items] == [100, 101, 102, 103]
    # Voice still keys off video_index (unchanged by reordering).
    assert [item.voice_key for item in items] == [
        "jarnathan", "jarnathan", "hope", "hope",
    ]


def test_pipelined_tail_overlaps_next_render(tmp_path):
    import threading

    from promo.cli.run_batch import DriveUploadTarget, run_batch

    tail_gate = threading.Event()
    tail_one_done = threading.Event()
    overlap = {}

    class _BlockingUploader(_FakeUploader):
        def upload_video_once(self, **kwargs):
            if not tail_one_done.is_set():
                # First tail parks here until render #2 has started.
                assert tail_gate.wait(timeout=10), "render #2 never released tail #1"
                tail_one_done.set()
            return super().upload_video_once(**kwargs)

    commands = []

    def command_runner(command):
        commands.append(command)
        if len(commands) == 2:
            # Render #2 is running while tail #1 is still blocked: overlap.
            overlap["render2_started_before_tail1_done"] = not tail_one_done.is_set()
            tail_gate.set()
        _write_valid_manifest_from_command(command, video_index=len(commands))
        return 0

    exit_code = run_batch(
        batch_path=str(_two_poi_batch(tmp_path)),
        output_root=str(tmp_path / "out"),
        target_duration_sec=65,
        production_autopilot=True,
        tail_workers=1,
        command_runner=command_runner,
        drive_upload_target_factory=lambda: DriveUploadTarget(
            uploader=_BlockingUploader(),
            parent_folder_id="parent-folder",
            parent_folder_name="AIGC Production Masters",
        ),
        supabase_client_factory=lambda: object(),
        usage_recorder=lambda client, events: {
            "inserted_count": len(events), "duplicate_count": 0,
        },
        usage_verifier=_ok_usage_verifier,
        release_registrar=_ok_release_registrar,
    )

    assert exit_code == 0
    assert overlap == {"render2_started_before_tail1_done": True}
    receipt = json.loads((tmp_path / "out" / "RUN_RECEIPT.json").read_text())
    assert [video["state"] for video in receipt["videos"]] == ["complete", "complete"]


def test_pipelined_same_poi_videos_never_overlap(tmp_path):
    import threading

    from promo.cli.run_batch import DriveUploadTarget, run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [{"poi_id": "poi_123", "name": "Terranea Resort"}],
            "videos_per_poi": 2,
            "target_duration_sec": 65,
        }),
        encoding="utf-8",
    )
    events_lock = threading.Lock()
    sequence = []

    class _RecordingUploader(_FakeUploader):
        def upload_video_once(self, **kwargs):
            with events_lock:
                sequence.append("tail")
            return super().upload_video_once(**kwargs)

    def command_runner(command):
        with events_lock:
            sequence.append("render")
        _write_valid_manifest_from_command(
            command, video_index=sequence.count("render"),
        )
        return 0

    exit_code = run_batch(
        batch_path=str(batch_path),
        output_root=str(tmp_path / "out"),
        target_duration_sec=65,
        production_autopilot=True,
        tail_workers=2,
        command_runner=command_runner,
        drive_upload_target_factory=lambda: DriveUploadTarget(
            uploader=_RecordingUploader(),
            parent_folder_id="parent-folder",
            parent_folder_name="AIGC Production Masters",
        ),
        supabase_client_factory=lambda: object(),
        usage_recorder=lambda client, events: {
            "inserted_count": len(events), "duplicate_count": 0,
        },
        usage_verifier=_ok_usage_verifier,
        release_registrar=_ok_release_registrar,
    )

    assert exit_code == 0
    # Same POI: usage ordering forbids overlap, so the second render must
    # wait for the first tail even with two workers available.
    assert sequence == ["render", "tail", "render", "tail"]


def test_pipelined_two_tail_workers_run_concurrently(tmp_path):
    import threading

    from promo.cli.run_batch import DriveUploadTarget, run_batch

    both_tails_in_flight = threading.Barrier(2, timeout=10)

    class _BarrierUploader(_FakeUploader):
        def upload_video_once(self, **kwargs):
            # Trips only if BOTH tails reach the upload step concurrently.
            both_tails_in_flight.wait()
            return super().upload_video_once(**kwargs)

    commands = []

    def command_runner(command):
        commands.append(command)
        _write_valid_manifest_from_command(command, video_index=len(commands))
        return 0

    exit_code = run_batch(
        batch_path=str(_two_poi_batch(tmp_path)),
        output_root=str(tmp_path / "out"),
        target_duration_sec=65,
        production_autopilot=True,
        tail_workers=2,
        command_runner=command_runner,
        drive_upload_target_factory=lambda: DriveUploadTarget(
            uploader=_BarrierUploader(),
            parent_folder_id="parent-folder",
            parent_folder_name="AIGC Production Masters",
        ),
        supabase_client_factory=lambda: object(),
        usage_recorder=lambda client, events: {
            "inserted_count": len(events), "duplicate_count": 0,
        },
        usage_verifier=_ok_usage_verifier,
        release_registrar=_ok_release_registrar,
    )

    assert exit_code == 0
    receipt = json.loads((tmp_path / "out" / "RUN_RECEIPT.json").read_text())
    assert [video["state"] for video in receipt["videos"]] == ["complete", "complete"]


def test_pipelined_tail_crash_fails_batch_but_renders_continue(tmp_path):
    from promo.cli.run_batch import DriveUploadTarget, run_batch

    factory_calls = []

    def drive_upload_target_factory():
        factory_calls.append(1)
        if len(factory_calls) > 1:
            # Preflight call succeeds; the worker thread's own client
            # construction crashes → the tail must fail without killing
            # the render loop.
            raise RuntimeError("worker drive client exploded")
        return DriveUploadTarget(
            uploader=_FakeUploader(),
            parent_folder_id="parent-folder",
            parent_folder_name="AIGC Production Masters",
        )

    commands = []

    def command_runner(command):
        commands.append(command)
        _write_valid_manifest_from_command(command, video_index=len(commands))
        return 0

    exit_code = run_batch(
        batch_path=str(_two_poi_batch(tmp_path)),
        output_root=str(tmp_path / "out"),
        target_duration_sec=65,
        production_autopilot=True,
        tail_workers=1,
        command_runner=command_runner,
        drive_upload_target_factory=drive_upload_target_factory,
        supabase_client_factory=lambda: object(),
        usage_recorder=lambda client, events: {
            "inserted_count": len(events), "duplicate_count": 0,
        },
        usage_verifier=_ok_usage_verifier,
        release_registrar=_ok_release_registrar,
    )

    assert exit_code == 1
    # Both videos rendered despite the tail crash.
    assert len(commands) == 2
    receipt = json.loads((tmp_path / "out" / "RUN_RECEIPT.json").read_text())
    # Renders + audits succeeded; tails never completed → states stay at the
    # audited checkpoint, which --resume classifies as tail-only.
    assert [video["state"] for video in receipt["videos"]] == [
        "rendered_manifest_audited",
        "rendered_manifest_audited",
    ]
