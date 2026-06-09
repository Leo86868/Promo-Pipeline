import json

import pytest


def _write_manifest(path, *, music_label="swatting at flies", poi_id="poi_123"):
    output = {
        "variant_index": 1,
        "output_path": "/tmp/promo.mp4",
    }
    if music_label is not None:
        output["music_label"] = music_label
    manifest = {
        "manifest_id": "manifest_abc",
        "run_id": "pgc_run_abc",
        "poi": {
            "poi_id": poi_id,
            "display_name": "Test Resort",
        },
        "outputs": [output],
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_build_release_handoff_normalizes_drive_id_and_preserves_music_label(tmp_path):
    from promo.core.pipeline.release_handoff import build_release_handoff_from_items_file

    manifest_path = tmp_path / "run_manifest_test_resort_65s.json"
    _write_manifest(manifest_path, music_label="Run Away with Me")
    items_path = tmp_path / "approved_items.json"
    items_path.write_text(
        json.dumps({
            "items": [{
                "run_manifest_path": manifest_path.name,
                "drive_file_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz",
            }]
        }),
        encoding="utf-8",
    )

    handoff = build_release_handoff_from_items_file(
        items_path,
        default_source_batch_id="pgc_batch_test",
        default_approved_at="2026-06-07T00:00:00Z",
    )

    assert list(handoff) == ["release_candidates"]
    record = handoff["release_candidates"][0]
    assert record == {
        "source_pipeline": "pgc_65s",
        "source_video_key": "manifest:manifest_abc:variant:1",
        "poi_id": "poi_123",
        "poi_name": "Test Resort",
        "source_output_uri": "drive:1AbCdEfGhIjKlMnOpQrStUvWxYz",
        "source_run_id": "pgc_run_abc",
        "status": "approved",
        "approved_at": "2026-06-07T00:00:00Z",
        "music_label": "Run Away with Me",
        "source_batch_id": "pgc_batch_test",
    }


def test_build_release_handoff_rejects_missing_music_label(tmp_path):
    from promo.core.pipeline.release_handoff import (
        ReleaseHandoffError,
        build_release_handoff_from_items_file,
    )

    manifest_path = tmp_path / "run_manifest_test_resort_65s.json"
    _write_manifest(manifest_path, music_label=None)
    items_path = tmp_path / "approved_items.json"
    items_path.write_text(
        json.dumps([{
            "run_manifest_path": str(manifest_path),
            "source_output_uri": "drive:1AbCdEfGhIjKlMnOpQrStUvWxYz",
        }]),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseHandoffError, match="music_label"):
        build_release_handoff_from_items_file(items_path)


def test_build_release_handoff_rejects_url_output_uri(tmp_path):
    from promo.core.pipeline.release_handoff import (
        ReleaseHandoffError,
        build_release_handoff_from_items_file,
    )

    manifest_path = tmp_path / "run_manifest_test_resort_65s.json"
    _write_manifest(manifest_path)
    items_path = tmp_path / "approved_items.json"
    items_path.write_text(
        json.dumps([{
            "run_manifest_path": str(manifest_path),
            "source_output_uri": "https://drive.google.com/file/d/abc/view",
        }]),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseHandoffError, match="not a URL"):
        build_release_handoff_from_items_file(items_path)


def test_export_release_handoff_cli_writes_output_json(tmp_path):
    from promo.cli.export_release_handoff import main

    manifest_path = tmp_path / "run_manifest_test_resort_65s.json"
    _write_manifest(manifest_path)
    items_path = tmp_path / "approved_items.json"
    output_path = tmp_path / "handoff.json"
    items_path.write_text(
        json.dumps([{
            "run_manifest_path": str(manifest_path),
            "source_output_uri": "drive:1AbCdEfGhIjKlMnOpQrStUvWxYz",
        }]),
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "sys.argv",
            [
                "export_release_handoff",
                "--items",
                str(items_path),
                "--output",
                str(output_path),
                "--approved-at",
                "2026-06-07T00:00:00Z",
            ],
        )
        assert main() == 0

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["release_candidates"][0]["music_label"] == "swatting at flies"
