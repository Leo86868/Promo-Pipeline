import json


def _valid_manifest():
    from promo.core.pipeline.run_manifest import build_run_manifest

    return build_run_manifest(
        poi_name="Test Resort",
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
        run_id="pgc_run_test",
        manifest_id="manifest_test",
        created_at="2026-06-08T00:00:00Z",
    )


def test_audit_manifest_passes_valid_production_manifest():
    from promo.core.manifest_audit import audit_manifest

    result = audit_manifest(_valid_manifest())

    assert result["passed"] is True
    assert result["error_count"] == 0
    assert result["summary"]["usage_event_count"] == 1
    assert result["summary"]["unique_usage_event_id_count"] == 1


def test_audit_manifest_rejects_missing_music_label():
    from promo.core.manifest_audit import audit_manifest

    manifest = _valid_manifest()
    manifest["outputs"][0].pop("music_label")

    result = audit_manifest(manifest)

    assert result["passed"] is False
    assert {
        "field": "outputs[0].music_label",
        "message": "music_label is required",
    } in result["errors"]


def test_audit_manifest_rejects_timeline_asset_missing_from_snapshot():
    from promo.core.manifest_audit import audit_manifest

    manifest = _valid_manifest()
    manifest["timeline_entries"][0]["asset_id"] = "asset_missing"

    result = audit_manifest(manifest)

    assert result["passed"] is False
    assert {
        "field": "timeline_entries[0].asset_id",
        "message": "asset_id must exist in asset_snapshot",
    } in result["errors"]


def test_audit_run_manifest_cli_writes_output_and_returns_failure(tmp_path):
    from promo.cli.audit_run_manifest import main

    manifest = _valid_manifest()
    manifest["poi"]["poi_id"] = None
    manifest_path = tmp_path / "run_manifest_test.json"
    output_path = tmp_path / "audit.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    import pytest

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "sys.argv",
            [
                "audit_run_manifest",
                str(manifest_path),
                "--output",
                str(output_path),
            ],
        )
        assert main() == 1

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["summary"]["failed_count"] == 1
    assert payload["manifests"][0]["errors"][0]["field"] == "poi.poi_id"
