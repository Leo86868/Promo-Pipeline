"""Read-only backend for shared POI assets in Supabase.

This backend reads ``public.poi_asset_valid_clips`` rows, downloads the
referenced objects from Supabase Storage into the pipeline temp directory,
and exposes the same rows for ``run_manifest.asset_snapshot``. It does not
write to Supabase.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Mapping
from typing import Any

from promo.core.backend import LocalBackend
from promo.core.pipeline.poi_asset_valid_clips import (
    POI_ASSET_VALID_CLIPS_VIEW,
    build_poi_asset_valid_clip_snapshot,
)

logger = logging.getLogger(__name__)


class PoiAssetBackendError(RuntimeError):
    """Raised when the read-only POI asset backend cannot load clips."""


def _response_data(response: Any) -> Any:
    return getattr(response, "data", response)


def _downloaded_bytes(payload: Any) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, bytearray):
        return bytes(payload)
    content = getattr(payload, "content", None)
    if isinstance(content, bytes):
        return content
    raise PoiAssetBackendError("Supabase Storage download did not return bytes")


class PoiAssetSupabaseBackend:
    """Read clips from ``poi_asset_valid_clips`` and Supabase Storage.

    Args:
        client: Supabase client instance. The backend only calls
            ``table(...).select(...).eq(...).order(...).execute()`` and
            ``storage.from_(bucket).download(path)``.
        poi_id: Stable POI id to load. Preferred lookup key.
        canonical_key: Fallback lookup key while operators bridge old inputs.
        output_dir: Optional final-output directory, matching ``LocalBackend``.
        bgm_path: Optional local BGM path.
        max_clips: Optional read limit for smoke tests.
        verify_hash: Compare downloaded bytes to ``source_content_hash``.
    """

    def __init__(
        self,
        client: Any,
        *,
        poi_id: str | None = None,
        canonical_key: str | None = None,
        output_dir: str | None = None,
        bgm_path: str | None = None,
        max_clips: int | None = None,
        verify_hash: bool = True,
    ) -> None:
        if not poi_id and not canonical_key:
            raise PoiAssetBackendError("poi_id or canonical_key is required")
        self._client = client
        self._poi_id = poi_id
        self._canonical_key = canonical_key
        self._output_dir = output_dir
        self._bgm_path = bgm_path
        self._max_clips = max_clips
        self._verify_hash = verify_hash
        self._shared_assets: list[dict[str, Any]] = []
        self._local_output = LocalBackend(
            clips_dir="",
            bgm_path=bgm_path,
            output_dir=output_dir,
        )

    @classmethod
    def from_env(cls, **kwargs: Any) -> "PoiAssetSupabaseBackend":
        """Create the backend from ``SUPABASE_URL`` and a server-side key.

        This helper intentionally does not call ``load_dotenv``; callers can
        load env files at their CLI boundary.
        """
        url = os.environ.get("SUPABASE_URL")
        key = (
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
            or os.environ.get("SUPABASE_KEY")
            or os.environ.get("SUPABASE_ANON_KEY")
        )
        if not url or not key:
            raise PoiAssetBackendError("SUPABASE_URL and a Supabase key are required")
        try:
            from supabase import create_client
        except ImportError as exc:
            raise PoiAssetBackendError(
                "supabase package is required for PoiAssetSupabaseBackend.from_env",
            ) from exc
        return cls(create_client(url, key), **kwargs)

    def _fetch_rows(self) -> list[Mapping[str, Any]]:
        query = self._client.table(POI_ASSET_VALID_CLIPS_VIEW).select("*")
        if self._poi_id:
            query = query.eq("poi_id", self._poi_id)
        else:
            query = query.eq("canonical_key", self._canonical_key)
        query = query.order("clip_id")
        if self._max_clips is not None:
            query = query.limit(int(self._max_clips))
        rows = _response_data(query.execute()) or []
        if not isinstance(rows, list):
            raise PoiAssetBackendError("poi_asset_valid_clips query returned non-list data")
        return rows

    def fetch_clips(self, poi_name: str, tmp_dir: str) -> dict[str, str]:
        rows = self._fetch_rows()
        if not rows:
            logger.error("poi_asset_valid_clips returned no rows")
            return {}

        snapshot = build_poi_asset_valid_clip_snapshot(rows, poi_id=self._poi_id)
        poi_ids = {row["poi_id"] for row in snapshot}
        if len(poi_ids) != 1:
            raise PoiAssetBackendError("poi_asset_valid_clips rows span multiple poi_id values")
        canonical_keys = {
            row.get("canonical_key")
            for row in snapshot
            if row.get("canonical_key")
        }
        if len(canonical_keys) > 1:
            raise PoiAssetBackendError(
                "poi_asset_valid_clips rows span multiple canonical_key values",
            )
        self._poi_id = next(iter(poi_ids))
        self._canonical_key = next(iter(canonical_keys), self._canonical_key)

        clip_paths: dict[str, str] = {}
        for row in snapshot:
            blob = self._client.storage.from_(row["source_storage_bucket"]).download(
                row["source_storage_path"],
            )
            data = _downloaded_bytes(blob)
            if self._verify_hash:
                digest = "sha256:" + hashlib.sha256(data).hexdigest()
                if digest != row["source_content_hash"]:
                    raise PoiAssetBackendError(
                        f"content hash mismatch for asset_id={row['asset_id']}",
                    )
            filename = f"clip_{row['clip_id']}_{row['asset_id']}.mp4"
            dest = os.path.join(tmp_dir, filename)
            with open(dest, "wb") as fh:
                fh.write(data)
            clip_paths[row["clip_id"]] = dest

        self._shared_assets = snapshot
        logger.info(
            "Loaded %d shared POI asset clips for %s",
            len(clip_paths), poi_name,
        )
        return clip_paths

    def shared_assets(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._shared_assets]

    def shared_poi_id(self) -> str | None:
        return self._poi_id

    def shared_canonical_key(self) -> str | None:
        return self._canonical_key

    def fetch_bgm(self, poi_name: str, tmp_dir: str) -> str | None:
        return self._local_output.fetch_bgm(poi_name, tmp_dir)

    def save_output(self, poi_name: str, video_path: str) -> str:
        return self._local_output.save_output(poi_name, video_path)

    def clips_dir(self) -> str | None:
        return None

    def output_dir(self) -> str | None:
        return self._output_dir
