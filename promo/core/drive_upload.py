"""OAuth Google Drive upload helpers for PGC final videos.

This module uses the same OAuth client-secret/token-pickle shape as AIGC Main.
It deliberately does not create public sharing permissions.
"""

from __future__ import annotations

import fcntl
import json
import mimetypes
import pickle
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from promo.core.drive_staging import handoff_items_from_inventory, summarize_inventory


SCOPES = ["https://www.googleapis.com/auth/drive"]
CHUNK_SIZE = 8 * 1024 * 1024
MAX_RETRIES = 3
DEFAULT_PARENT_FOLDER_NAME = "AIGC Production Masters"


class DriveUploadError(RuntimeError):
    """Raised when Drive upload or verification is unsafe."""


@dataclass(frozen=True)
class DriveUploadConfig:
    credentials_file: Path
    token_file: Path
    parent_folder_id: str | None = None
    parent_folder_name: str = DEFAULT_PARENT_FOLDER_NAME


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_staging_inventory(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise DriveUploadError("staging inventory JSON must be an object")
    if payload.get("inventory_kind") != "pgc_drive_staging_inventory":
        raise DriveUploadError("staging inventory kind must be pgc_drive_staging_inventory")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise DriveUploadError("staging inventory must contain non-empty items")
    return payload


def token_file_for_credentials(credentials_file: Path) -> Path:
    return credentials_file.resolve().parent / "token.pickle"


def build_drive_upload_config(
    *,
    credentials_file: str,
    token_file: str | None = None,
    parent_folder_id: str | None = None,
    parent_folder_name: str | None = None,
) -> DriveUploadConfig:
    credentials_path = Path(credentials_file).expanduser()
    token_path = Path(token_file).expanduser() if token_file else token_file_for_credentials(credentials_path)
    if not credentials_path.exists():
        raise DriveUploadError(f"Google credentials file does not exist: {credentials_path}")
    return DriveUploadConfig(
        credentials_file=credentials_path,
        token_file=token_path,
        parent_folder_id=(parent_folder_id or "").strip() or None,
        parent_folder_name=(parent_folder_name or DEFAULT_PARENT_FOLDER_NAME).strip()
        or DEFAULT_PARENT_FOLDER_NAME,
    )


def _utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DriveUploadError(f"{field} is required")
    return value.strip()


def _folder_segment(value: Any, field: str) -> str:
    text = _required_text(value, field)
    if "/" in text:
        raise DriveUploadError(f"{field} must not contain '/'")
    return text


def _drive_query_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def resolve_upload_context(
    inventory: dict[str, Any],
    *,
    paradigm: str | None = None,
    date: str | None = None,
    batch_id: str | None = None,
) -> dict[str, str]:
    return {
        "paradigm": _folder_segment(
            paradigm or inventory.get("paradigm") or "pgc_65s",
            "paradigm",
        ),
        "date": _folder_segment(
            date or str(inventory.get("created_at") or "")[:10] or _utc_date(),
            "date",
        ),
        "batch_id": _folder_segment(
            batch_id or inventory.get("batch_id") or "pgc_batch_upload",
            "batch_id",
        ),
    }


def file_id_from_drive_uri(value: str) -> str:
    if not value.startswith("drive:") or value == "drive:":
        raise DriveUploadError("source_output_uri must be drive:<file_id>")
    return value.split(":", 1)[1]


def _is_retryable_error(error: Exception) -> bool:
    if hasattr(error, "resp") and hasattr(error.resp, "status"):
        status = int(error.resp.status)
        if status in {401, 403, 404}:
            return False
        if status >= 500:
            return True
    error_text = str(error).lower()
    return any(
        token in error_text
        for token in (
            "timeout",
            "timed out",
            "connection",
            "reset",
            "aborted",
            "broken pipe",
            "eof",
            "network",
        )
    )


class OAuthDriveUploader:
    """Small Drive uploader using OAuth token.pickle credentials."""

    def __init__(self, config: DriveUploadConfig) -> None:
        self._config = config
        self._creds = self._load_credentials()
        self._service = None

    def _lock_token_file(self):
        lock_path = str(self._config.token_file) + ".lock"
        try:
            self._config.token_file.parent.mkdir(parents=True, exist_ok=True)
            lock_fh = open(lock_path, "w", encoding="utf-8")
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            return lock_fh
        except OSError:
            return None

    def _unlock_token_file(self, lock_fh: Any) -> None:
        if not lock_fh:
            return
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()
        except OSError:
            pass

    def _load_credentials(self) -> Any:
        try:
            from google.auth.transport.requests import Request
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:
            raise DriveUploadError(
                "google-api-python-client and google-auth-oauthlib are required"
            ) from exc

        lock_fh = self._lock_token_file()
        try:
            creds = None
            if self._config.token_file.exists():
                with self._config.token_file.open("rb") as fh:
                    creds = pickle.load(fh)
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception:
                    creds = None
            if not creds or not creds.valid:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self._config.credentials_file),
                    SCOPES,
                )
                creds = flow.run_local_server(port=0)
            self._config.token_file.parent.mkdir(parents=True, exist_ok=True)
            with self._config.token_file.open("wb") as fh:
                pickle.dump(creds, fh)
            return creds
        finally:
            self._unlock_token_file(lock_fh)

    def _get_service(self) -> Any:
        if self._service is None:
            from googleapiclient.discovery import build

            self._service = build(
                "drive",
                "v3",
                credentials=self._creds,
                cache_discovery=False,
            )
        return self._service

    def _retry(self, operation: Any, description: str) -> Any:
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return operation()
            except Exception as exc:
                last_error = exc
                if not _is_retryable_error(exc):
                    raise
                if attempt < MAX_RETRIES - 1:
                    time.sleep(5 * (3 ** attempt))
        raise DriveUploadError(f"{description} failed after retries: {last_error}") from last_error

    def find_child(
        self,
        *,
        name: str,
        parent_folder_id: str,
        mime_type: str | None = None,
    ) -> dict[str, Any] | None:
        safe_name = _drive_query_text(name)
        query = f"name = '{safe_name}' and '{parent_folder_id}' in parents and trashed = false"
        if mime_type:
            query += f" and mimeType = '{_drive_query_text(mime_type)}'"

        def _do_find() -> Any:
            return (
                self._get_service()
                .files()
                .list(
                    q=query,
                    spaces="drive",
                    fields="files(id,name,mimeType,size,webViewLink)",
                    pageSize=1,
                )
                .execute(num_retries=2)
            )

        result = self._retry(_do_find, f"find Drive child {name}")
        files = result.get("files", [])
        return files[0] if files else None

    def ensure_folder(self, name: str, parent_folder_id: str) -> dict[str, Any]:
        existing = self.find_child(
            name=name,
            parent_folder_id=parent_folder_id,
            mime_type="application/vnd.google-apps.folder",
        )
        if existing:
            return existing
        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_folder_id],
        }

        def _do_create() -> Any:
            return (
                self._get_service()
                .files()
                .create(body=metadata, fields="id,name,mimeType,webViewLink")
                .execute(num_retries=2)
            )

        return self._retry(_do_create, f"create Drive folder {name}")

    def ensure_batch_folder(
        self,
        *,
        parent_folder_id: str | None,
        parent_folder_name: str,
        paradigm: str,
        date: str,
        batch_id: str,
    ) -> dict[str, Any]:
        current_folder_id = parent_folder_id
        if not current_folder_id:
            parent = self.ensure_folder(parent_folder_name, "root")
            current_folder_id = parent["id"]
        folder = {"id": current_folder_id, "name": parent_folder_name}
        for segment in (paradigm, date, batch_id):
            folder = self.ensure_folder(segment, current_folder_id)
            current_folder_id = folder["id"]
        return folder

    def get_file_metadata(self, file_id: str) -> dict[str, Any]:
        def _do_get() -> Any:
            return (
                self._get_service()
                .files()
                .get(fileId=file_id, fields="id,name,mimeType,size,parents,webViewLink")
                .execute(num_retries=2)
            )

        return self._retry(_do_get, f"get Drive file metadata {file_id}")

    def upload_video_once(
        self,
        *,
        local_path: str,
        folder_id: str,
        filename: str | None = None,
    ) -> dict[str, Any]:
        from googleapiclient.http import MediaFileUpload

        source = Path(local_path)
        if not source.is_file():
            raise DriveUploadError(f"local video does not exist: {source}")
        upload_name = filename or source.name
        expected_size = source.stat().st_size
        existing = self.find_child(name=upload_name, parent_folder_id=folder_id)
        if existing:
            observed_size = int(existing.get("size") or -1)
            if observed_size != expected_size:
                raise DriveUploadError(
                    f"Drive file already exists with different size: {upload_name}"
                )
            return {**existing, "reused_existing": True}

        mime_type, _ = mimetypes.guess_type(str(source))
        if not mime_type or not mime_type.startswith("video/"):
            mime_type = "video/mp4"
        metadata = {"name": upload_name, "parents": [folder_id]}

        def _do_upload() -> Any:
            media = MediaFileUpload(
                str(source),
                mimetype=mime_type,
                resumable=True,
                chunksize=CHUNK_SIZE,
            )
            request = (
                self._get_service()
                .files()
                .create(
                    body=metadata,
                    media_body=media,
                    fields="id,name,mimeType,size,parents,webViewLink",
                )
            )
            response = None
            while response is None:
                _status, response = request.next_chunk(num_retries=2)
            return response

        try:
            result = self._retry(_do_upload, f"upload Drive video {upload_name}")
        except Exception as exc:
            existing_after_error = self.find_child(
                name=upload_name,
                parent_folder_id=folder_id,
            )
            if existing_after_error:
                observed_size = int(existing_after_error.get("size") or -1)
                if observed_size == expected_size:
                    return {**existing_after_error, "reused_existing": True}
            raise DriveUploadError(f"Drive upload failed for {upload_name}: {exc}") from exc

        verified = self.get_file_metadata(result["id"])
        observed_size = int(verified.get("size") or -1)
        if observed_size != expected_size:
            raise DriveUploadError(
                f"Drive upload size mismatch for {upload_name}: "
                f"expected {expected_size}, observed {observed_size}"
            )
        return {**verified, "reused_existing": False}


def upload_staging_inventory(
    inventory: dict[str, Any],
    uploader: Any,
    *,
    parent_folder_id: str | None = None,
    parent_folder_name: str = DEFAULT_PARENT_FOLDER_NAME,
    paradigm: str | None = None,
    date: str | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    context = resolve_upload_context(
        inventory,
        paradigm=paradigm,
        date=date,
        batch_id=batch_id,
    )
    folder = uploader.ensure_batch_folder(
        parent_folder_id=parent_folder_id,
        parent_folder_name=parent_folder_name,
        paradigm=context["paradigm"],
        date=context["date"],
        batch_id=context["batch_id"],
    )
    failed = 0
    for item in inventory.get("items", []):
        if item.get("staging_status") == "drive_uri_ready" and item.get("source_output_uri"):
            file_id = file_id_from_drive_uri(str(item["source_output_uri"]))
            metadata = uploader.get_file_metadata(file_id)
            item["drive_file_id"] = file_id
            item["drive_upload"] = {
                "status": "verified_existing",
                "folder_id": folder["id"],
                "file_name": metadata.get("name"),
                "size_bytes": int(metadata.get("size") or 0),
                "mime_type": metadata.get("mimeType"),
            }
            continue
        try:
            local_output_path = _required_text(
                item.get("local_output_path"),
                "items[].local_output_path",
            )
            metadata = uploader.upload_video_once(
                local_path=local_output_path,
                folder_id=folder["id"],
                filename=Path(local_output_path).name,
            )
            item["drive_file_id"] = metadata["id"]
            item["source_output_uri"] = f"drive:{metadata['id']}"
            item["staging_status"] = "drive_uri_ready"
            item["drive_upload"] = {
                "status": "verified",
                "folder_id": folder["id"],
                "file_name": metadata.get("name"),
                "size_bytes": int(metadata.get("size") or 0),
                "mime_type": metadata.get("mimeType"),
                "reused_existing": bool(metadata.get("reused_existing")),
            }
        except Exception as exc:
            failed += 1
            item["staging_status"] = "drive_upload_failed"
            item["drive_upload"] = {
                "status": "failed",
                "folder_id": folder["id"],
                "error": str(exc),
            }

    inventory["drive_upload"] = {
        "status": "failed" if failed else "complete",
        "folder_id": folder["id"],
        "folder_name": folder.get("name"),
        "parent_folder_name": parent_folder_name,
        **context,
    }
    inventory["summary"] = summarize_inventory(inventory.get("items", []))
    return inventory


def build_handoff_items_payload(inventory: dict[str, Any]) -> dict[str, Any]:
    return {"items": handoff_items_from_inventory(inventory)}
