import json

import pytest


def _write_manifest(tmp_path, *, output_exists=True, music_label="Run Away"):
    output_path = tmp_path / "promo_test_resort_65s.mp4"
    if output_exists:
        output_path.write_bytes(b"mp4")
    manifest_path = tmp_path / "run_manifest_test_resort_65s.json"
    output = {
        "variant_index": 1,
        "output_path": str(output_path),
    }
    if music_label is not None:
        output["music_label"] = music_label
    manifest_path.write_text(
        json.dumps({
            "manifest_id": "manifest_abc",
            "run_id": "pgc_run_abc",
            "poi": {
                "poi_id": "poi_123",
                "display_name": "Test Resort",
            },
            "outputs": [output],
        }),
        encoding="utf-8",
    )
    return manifest_path


def test_build_staging_inventory_reads_manifest_and_local_output(tmp_path):
    from promo.core.drive_staging import build_staging_inventory

    manifest_path = _write_manifest(tmp_path)

    inventory = build_staging_inventory([manifest_path])

    assert inventory["summary"] == {
        "item_count": 1,
        "pending_drive_upload": 1,
        "drive_uri_ready": 0,
        "missing_source_outputs": 0,
    }
    item = inventory["items"][0]
    assert item["source_video_key"] == "manifest:manifest_abc:variant:1"
    assert item["manifest_id"] == "manifest_abc"
    assert item["run_id"] == "pgc_run_abc"
    assert item["poi_id"] == "poi_123"
    assert item["poi_name"] == "Test Resort"
    assert item["music_label"] == "Run Away"
    assert item["staging_status"] == "pending_drive_upload"


def test_build_staging_inventory_rejects_missing_source_by_default(tmp_path):
    from promo.core.drive_staging import DriveStagingError, build_staging_inventory

    manifest_path = _write_manifest(tmp_path, output_exists=False)

    with pytest.raises(DriveStagingError, match="does not exist"):
        build_staging_inventory([manifest_path])

    inventory = build_staging_inventory([manifest_path], require_source_exists=False)
    assert inventory["summary"]["missing_source_outputs"] == 1


def test_apply_drive_file_map_and_build_handoff_items(tmp_path):
    from promo.core.drive_staging import (
        apply_drive_file_map,
        build_staging_inventory,
        handoff_items_from_inventory,
    )

    manifest_path = _write_manifest(tmp_path)
    inventory = build_staging_inventory([manifest_path])

    apply_drive_file_map(
        inventory,
        {"manifest:manifest_abc:variant:1": "1AbCdEfGhIjKlMnOpQrStUvWxYz"},
    )

    assert inventory["summary"]["drive_uri_ready"] == 1
    assert inventory["items"][0]["source_output_uri"] == (
        "drive:1AbCdEfGhIjKlMnOpQrStUvWxYz"
    )
    assert handoff_items_from_inventory(inventory) == [{
        "run_manifest_path": str(manifest_path),
        "variant_index": 1,
        "source_output_uri": "drive:1AbCdEfGhIjKlMnOpQrStUvWxYz",
    }]


def test_drive_file_id_must_be_raw_id(tmp_path):
    from promo.core.drive_staging import (
        DriveStagingError,
        apply_drive_file_map,
        build_staging_inventory,
    )

    manifest_path = _write_manifest(tmp_path)
    inventory = build_staging_inventory([manifest_path])

    with pytest.raises(DriveStagingError, match="raw Drive file id"):
        apply_drive_file_map(
            inventory,
            {"manifest:manifest_abc:variant:1": "drive:already-prefixed"},
        )


def test_prepare_drive_staging_cli_writes_inventory_and_handoff_items(tmp_path):
    from promo.cli.prepare_drive_staging import main

    manifest_path = _write_manifest(tmp_path)
    map_path = tmp_path / "drive_map.json"
    inventory_path = tmp_path / "inventory.json"
    handoff_path = tmp_path / "handoff_items.json"
    map_path.write_text(
        json.dumps({
            "manifest:manifest_abc:variant:1": "1AbCdEfGhIjKlMnOpQrStUvWxYz",
        }),
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "sys.argv",
            [
                "prepare_drive_staging",
                str(manifest_path),
                "--output",
                str(inventory_path),
                "--drive-file-map",
                str(map_path),
                "--handoff-items-output",
                str(handoff_path),
            ],
        )
        assert main() == 0

    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    assert inventory["summary"]["drive_uri_ready"] == 1
    assert handoff["items"][0]["source_output_uri"].startswith("drive:")
