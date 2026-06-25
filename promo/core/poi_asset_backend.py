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
import time
from collections.abc import Mapping
from typing import Any

import requests

from promo.core.backend import LocalBackend
from promo.core.music_library import MusicLibraryError, SupabaseMusicLibrary
from promo.core.pipeline.poi_asset_valid_clips import (
    POI_ASSET_VALID_CLIPS_VIEW,
    build_poi_asset_valid_clip_snapshot,
)
from promo.core.source_resolution_policy import (
    SourceResolutionPolicy,
    normalize_source_resolution_policy,
    source_resolution_matches,
)

logger = logging.getLogger(__name__)
_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_RETRY_DELAY_SEC = 1.0
_DOWNLOAD_TIMEOUT_SEC = 120.0


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
            ``storage.from_(bucket).get_public_url(path)`` (clip bytes are then
            fetched over the public CDN path with an unauthenticated HTTP GET).
        poi_id: Stable POI id to load. Preferred lookup key.
        canonical_key: Fallback lookup key while operators bridge old inputs.
        output_dir: Optional final-output directory, matching ``LocalBackend``.
        bgm_path: Optional local BGM path.
        use_music_library: When true, fetch BGM from ``public.music_library``.
        music_id: Optional exact Music Library row to use.
        music_min_duration_sec: Minimum audited track length for Music Library.
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
        use_music_library: bool = False,
        music_id: str | None = None,
        music_min_duration_sec: float | None = None,
        max_clips: int | None = None,
        verify_hash: bool = True,
        source_resolution_policy: SourceResolutionPolicy | dict[str, Any] | None = None,
    ) -> None:
        if not poi_id and not canonical_key:
            raise PoiAssetBackendError("poi_id or canonical_key is required")
        self._client = client
        self._poi_id = poi_id
        self._canonical_key = canonical_key
        self._output_dir = output_dir
        self._bgm_path = bgm_path
        self._music_library = (
            SupabaseMusicLibrary(
                client,
                min_duration_sec=music_min_duration_sec or 0,
                music_id=music_id,
            )
            if use_music_library or music_id
            else None
        )
        self._max_clips = max_clips
        self._verify_hash = verify_hash
        self._source_resolution_policy = normalize_source_resolution_policy(
            source_resolution_policy
        )
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
        snapshot = [
            row for row in snapshot
            if source_resolution_matches(row, self._source_resolution_policy)
        ]
        if not snapshot:
            raise PoiAssetBackendError(
                "poi_asset_valid_clips rows did not match source_resolution_policy"
            )
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
        self._download_snapshot(snapshot, clip_paths, tmp_dir)

        self._shared_assets = snapshot
        logger.info(
            "Loaded %d shared POI asset clips for %s",
            len(clip_paths), poi_name,
        )
        return clip_paths

    def _download_snapshot(
        self,
        snapshot: list[dict[str, Any]],
        clip_paths: dict[str, str],
        tmp_dir: str,
    ) -> None:
        for row in snapshot:
            blob = self._download_clip_blob(row)
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

    def fetch_candidate_clips(
        self,
        poi_name: str,
        tmp_dir: str,
        asset_ids: list[str],
    ) -> dict[str, str]:
        requested_asset_ids = list(dict.fromkeys(str(asset_id) for asset_id in asset_ids))
        if not requested_asset_ids:
            raise PoiAssetBackendError("candidate asset_ids are required")
        rows = self._fetch_rows()
        rows_by_asset_id = {
            str(row.get("asset_id")): row
            for row in rows
            if row.get("asset_id")
        }
        missing = [
            asset_id
            for asset_id in requested_asset_ids
            if asset_id not in rows_by_asset_id
        ]
        if missing:
            raise PoiAssetBackendError(
                "candidate asset_ids not found in poi_asset_valid_clips: "
                + ", ".join(missing[:5])
            )
        ordered_rows = [rows_by_asset_id[asset_id] for asset_id in requested_asset_ids]
        snapshot = build_poi_asset_valid_clip_snapshot(ordered_rows, poi_id=self._poi_id)
        rejected = [
            row["asset_id"] for row in snapshot
            if not source_resolution_matches(row, self._source_resolution_policy)
        ]
        if rejected:
            raise PoiAssetBackendError(
                "candidate asset_ids violate source_resolution_policy: "
                + ", ".join(rejected[:5])
            )
        poi_ids = {row["poi_id"] for row in snapshot}
        if len(poi_ids) != 1:
            raise PoiAssetBackendError("candidate asset rows span multiple poi_id values")
        self._poi_id = next(iter(poi_ids))
        canonical_keys = {
            row.get("canonical_key")
            for row in snapshot
            if row.get("canonical_key")
        }
        if len(canonical_keys) > 1:
            raise PoiAssetBackendError(
                "candidate asset rows span multiple canonical_key values",
            )
        self._canonical_key = next(iter(canonical_keys), self._canonical_key)

        clip_paths: dict[str, str] = {}
        self._download_snapshot(snapshot, clip_paths, tmp_dir)
        self._shared_assets = snapshot
        logger.info(
            "Loaded %d candidate shared POI asset clips for %s",
            len(clip_paths),
            poi_name,
        )
        return clip_paths

    def _download_clip_blob(self, row: Mapping[str, Any]) -> Any:
        # Fetch via the bucket's PUBLIC CDN path ($0.03/GB) instead of the
        # authenticated storage API ($0.09/GB). get_public_url only builds the
        # URL string (no egress, no auth); the GET below is unauthenticated
        # because poi-assets is a public bucket. The bytes-in/bytes-out contract
        # is unchanged, and integrity is still enforced by the source_content_hash
        # check in _download_snapshot (verify_hash). Fail-loud on purpose: a
        # private bucket / missing object returns a non-200 with a JSON error
        # body, which must NEVER be written as clip bytes.
        url = self._client.storage.from_(row["source_storage_bucket"]).get_public_url(
            row["source_storage_path"],
        )
        last_error: Exception | None = None
        for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
            try:
                response = requests.get(url, timeout=_DOWNLOAD_TIMEOUT_SEC)
                if response.status_code != 200:
                    raise PoiAssetBackendError(
                        f"public CDN fetch returned HTTP {response.status_code} "
                        f"for asset_id={row.get('asset_id')}",
                    )
                content = response.content
                if not content:
                    raise PoiAssetBackendError(
                        f"public CDN fetch returned an empty body "
                        f"for asset_id={row.get('asset_id')}",
                    )
                return content
            except Exception as exc:
                last_error = exc
                if attempt == _DOWNLOAD_ATTEMPTS:
                    break
                logger.warning(
                    "Retrying shared asset download after error "
                    "asset_id=%s attempt=%d/%d",
                    row.get("asset_id"),
                    attempt,
                    _DOWNLOAD_ATTEMPTS,
                )
                time.sleep(_DOWNLOAD_RETRY_DELAY_SEC)
        raise PoiAssetBackendError(
            f"failed to download asset_id={row.get('asset_id')}",
        ) from last_error

    def shared_assets(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._shared_assets]

    def ready_assets_for_retrieval(self):
        """Load ready embedding rows for this POI without downloading media."""
        if not self._poi_id:
            raise PoiAssetBackendError("poi_id is required for semantic retrieval")
        from promo.core.assets.retrieval import fetch_ready_assets

        return fetch_ready_assets(
            self._client,
            poi_id=self._poi_id,
            source_resolution_policy=self._source_resolution_policy,
        )

    def visual_vectors_for_assets(self, asset_ids: list[str]) -> dict[str, list[float]]:
        """工单② — read ready DINOv2 visual vectors for the given assets from
        ``poi_asset_visual_embeddings`` (``status='ready'`` only). Returns
        ``{asset_id: vector}``; assets without a ready vector are simply
        absent (the diversity selector fails open for them). Read-only; used
        only when the download-diversity flag is armed."""
        from promo.core.assets.retrieval import (
            VISUAL_EMBEDDING_DIM,
            parse_embedding_vector,
        )

        ids = sorted(set(asset_ids))
        if not ids:
            return {}
        vectors: dict[str, list[float]] = {}
        for start in range(0, len(ids), 200):
            chunk = ids[start:start + 200]
            rows = (
                self._client.table("poi_asset_visual_embeddings")
                .select("asset_id,embedding_vector,status")
                .in_("asset_id", chunk)
                .eq("status", "ready")
                .execute()
                .data
            ) or []
            for row in rows:
                vectors[str(row["asset_id"])] = list(
                    parse_embedding_vector(
                        row["embedding_vector"], expected_dim=VISUAL_EMBEDDING_DIM,
                    )
                )
        return vectors

    def shared_poi_id(self) -> str | None:
        return self._poi_id

    def shared_canonical_key(self) -> str | None:
        return self._canonical_key

    def fetch_bgm(self, poi_name: str, tmp_dir: str) -> str | None:
        if self._music_library is not None:
            try:
                return self._music_library.fetch_bgm(tmp_dir)
            except MusicLibraryError as exc:
                raise PoiAssetBackendError(str(exc)) from exc
        return self._local_output.fetch_bgm(poi_name, tmp_dir)

    def fetch_bgms(self, poi_name: str, tmp_dir: str, *, count: int) -> list[str]:
        if self._music_library is None:
            fetched = self._local_output.fetch_bgm(poi_name, tmp_dir)
            return [fetched] if fetched else []
        try:
            return self._music_library.fetch_bgms(tmp_dir, count=count)
        except MusicLibraryError as exc:
            raise PoiAssetBackendError(str(exc)) from exc

    def music_metadata_for_path(self, path: str) -> dict[str, Any] | None:
        if self._music_library is None:
            return None
        return self._music_library.music_metadata_for_path(path)

    def save_output(self, poi_name: str, video_path: str) -> str:
        return self._local_output.save_output(poi_name, video_path)

    def clips_dir(self) -> str | None:
        return None

    def output_dir(self) -> str | None:
        return self._output_dir
