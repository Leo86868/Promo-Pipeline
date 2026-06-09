import json


class _FakeUploader:
    def __init__(self, *, fail_keys=None):
        self.fail_keys = set(fail_keys or [])
        self.uploads = []
        self.metadata = {}

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
        return {"id": "folder-batch", "name": batch_id}

    def upload_video_once(self, *, local_path, folder_id, filename=None):
        if filename in self.fail_keys:
            raise RuntimeError("upload failed")
        file_id = f"drive-{filename}"
        result = {
            "id": file_id,
            "name": filename,
            "size": "3",
            "mimeType": "video/mp4",
            "reused_existing": False,
        }
        self.uploads.append((local_path, folder_id, filename))
        self.metadata[file_id] = result
        return result

    def get_file_metadata(self, file_id):
        return self.metadata.get(
            file_id,
            {
                "id": file_id,
                "name": "existing.mp4",
                "size": "3",
                "mimeType": "video/mp4",
            },
        )


def _inventory(tmp_path, *, batch_id="pgc_batch_test"):
    mp4_path = tmp_path / "promo.mp4"
    mp4_path.write_bytes(b"mp4")
    return {
        "schema_version": 1,
        "inventory_kind": "pgc_drive_staging_inventory",
        "batch_id": batch_id,
        "paradigm": "pgc_65s",
        "created_at": "2026-06-08T00:00:00Z",
        "items": [{
            "source_video_key": "manifest:manifest_abc:variant:1",
            "manifest_id": "manifest_abc",
            "run_id": "pgc_run_abc",
            "run_manifest_path": str(tmp_path / "run_manifest.json"),
            "variant_index": 1,
            "poi_id": "poi_123",
            "poi_name": "Test Resort",
            "music_label": "Run Away",
            "local_output_path": str(mp4_path),
            "local_output_exists": True,
            "drive_file_id": None,
            "source_output_uri": None,
            "staging_status": "pending_drive_upload",
        }],
        "summary": {
            "item_count": 1,
            "pending_drive_upload": 1,
            "drive_uri_ready": 0,
            "drive_upload_failed": 0,
            "missing_source_outputs": 0,
        },
    }


def test_upload_staging_inventory_updates_items_and_summary(tmp_path):
    from promo.core.drive_upload import upload_staging_inventory

    inventory = _inventory(tmp_path)
    uploader = _FakeUploader()

    result = upload_staging_inventory(
        inventory,
        uploader,
        parent_folder_id="parent-folder",
        parent_folder_name="AIGC Production Masters",
    )

    item = result["items"][0]
    assert item["staging_status"] == "drive_uri_ready"
    assert item["source_output_uri"] == "drive:drive-promo.mp4"
    assert item["drive_upload"]["status"] == "verified"
    assert result["drive_upload"]["status"] == "complete"
    assert result["drive_upload"]["batch_id"] == "pgc_batch_test"
    assert result["drive_upload"]["date"] == "2026-06-08"
    assert result["summary"]["drive_uri_ready"] == 1
    assert result["summary"]["drive_upload_failed"] == 0
    assert uploader.folder_context["parent_folder_id"] == "parent-folder"


def test_upload_staging_inventory_records_failed_items(tmp_path):
    from promo.core.drive_upload import upload_staging_inventory

    inventory = _inventory(tmp_path)
    uploader = _FakeUploader(fail_keys={"promo.mp4"})

    result = upload_staging_inventory(
        inventory,
        uploader,
        parent_folder_name="AIGC Production Masters",
    )

    item = result["items"][0]
    assert result["drive_upload"]["status"] == "failed"
    assert item["staging_status"] == "drive_upload_failed"
    assert item["drive_upload"]["status"] == "failed"
    assert result["summary"]["drive_upload_failed"] == 1


def test_build_drive_upload_config_defaults_token_next_to_credentials(tmp_path):
    from promo.core.drive_upload import build_drive_upload_config

    credentials = tmp_path / "client_secret.json"
    credentials.write_text("{}", encoding="utf-8")

    config = build_drive_upload_config(credentials_file=str(credentials))

    assert config.credentials_file == credentials
    assert config.token_file == tmp_path / "token.pickle"
    assert config.parent_folder_name == "AIGC Production Masters"


def test_upload_drive_staging_cli_dry_run_writes_target_context(tmp_path):
    from promo.cli.upload_drive_staging import main

    inventory_path = tmp_path / "inventory.json"
    output_path = tmp_path / "uploaded.json"
    handoff_path = tmp_path / "handoff_items.json"
    inventory_path.write_text(json.dumps(_inventory(tmp_path)), encoding="utf-8")

    import pytest

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "sys.argv",
            [
                "upload_drive_staging",
                "--inventory",
                str(inventory_path),
                "--output",
                str(output_path),
                "--handoff-items-output",
                str(handoff_path),
                "--dry-run",
            ],
        )
        assert main() == 0

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["drive_upload"]["status"] == "dry_run"
    assert payload["drive_upload"]["parent_folder_name"] == "AIGC Production Masters"
    assert payload["drive_upload"]["batch_id"] == "pgc_batch_test"
    assert not handoff_path.exists()
