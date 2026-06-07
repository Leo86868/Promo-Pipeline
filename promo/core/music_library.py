"""Supabase Music Library helpers.

The production music source is ``public.music_library``. This module keeps
the runtime contract small: a row needs a Drive file id and an audited
``duration_sec`` so PGC can select tracks long enough for the target video.
It does not write to Supabase.
"""

from __future__ import annotations

import os
import re
import urllib.request
from collections.abc import Iterable, Mapping
from typing import Any


MUSIC_LIBRARY_TABLE = "music_library"
MUSIC_LIBRARY_SELECT_FIELDS = (
    "id,music_name,drive_file_id,duration_sec,genre,bpm,tags,embedding_text"
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
)
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


class MusicLibraryError(RuntimeError):
    """Raised when a Music Library row cannot be used as BGM."""


def _response_data(response: Any) -> Any:
    return getattr(response, "data", response)


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MusicLibraryError(f"{field} is required")
    return value.strip()


def _positive_float(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise MusicLibraryError(f"{field} must be a positive number") from exc
    if parsed <= 0:
        raise MusicLibraryError(f"{field} must be positive")
    return parsed


def normalize_music_library_track(
    row: Mapping[str, Any],
    *,
    min_duration_sec: float | None = None,
) -> dict[str, Any]:
    """Validate and normalize one ``public.music_library`` row."""
    music_id = _required_text(row, "id")
    if not _UUID_RE.fullmatch(music_id):
        raise MusicLibraryError("id must be a UUID string")
    music_name = _required_text(row, "music_name")
    drive_file_id = _required_text(row, "drive_file_id")
    duration_sec = _positive_float(row.get("duration_sec"), "duration_sec")
    if min_duration_sec is not None and duration_sec < float(min_duration_sec):
        raise MusicLibraryError(
            f"duration_sec {duration_sec:.3f} is below target "
            f"{float(min_duration_sec):.3f}",
        )
    return {
        "id": music_id,
        "music_name": music_name,
        "drive_file_id": drive_file_id,
        "duration_sec": duration_sec,
        "genre": row.get("genre"),
        "bpm": row.get("bpm"),
        "tags": row.get("tags"),
        "embedding_text": row.get("embedding_text"),
    }


def eligible_music_tracks(
    rows: Iterable[Mapping[str, Any]],
    *,
    min_duration_sec: float,
) -> list[dict[str, Any]]:
    """Return rows that satisfy the runtime duration requirement."""
    tracks: list[dict[str, Any]] = []
    for row in rows:
        try:
            tracks.append(
                normalize_music_library_track(
                    row,
                    min_duration_sec=min_duration_sec,
                ),
            )
        except MusicLibraryError:
            continue
    return sorted(tracks, key=lambda item: (item["duration_sec"], item["music_name"]))


def drive_download_url(drive_file_id: str) -> str:
    """Return the direct Google Drive download URL for a file id."""
    file_id = drive_file_id.strip()
    if not file_id:
        raise MusicLibraryError("drive_file_id is required")
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def music_filename(track: Mapping[str, Any]) -> str:
    """Build a stable local filename for a downloaded music row."""
    music_id = _required_text(track, "id")
    name = _required_text(track, "music_name")
    safe_name = _SAFE_FILENAME_RE.sub("_", name).strip("._") or "track"
    return f"music_{safe_name}_{music_id}.mp3"


def download_drive_file(drive_file_id: str, dest: str) -> None:
    """Download a Drive file with a regular user-agent header."""
    request = urllib.request.Request(
        drive_download_url(drive_file_id),
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        with open(dest, "wb") as fh:
            fh.write(response.read())


class SupabaseMusicLibrary:
    """Read-only Music Library downloader for runtime BGM selection."""

    def __init__(
        self,
        client: Any,
        *,
        min_duration_sec: float,
        music_id: str | None = None,
    ) -> None:
        self._client = client
        self._min_duration_sec = float(min_duration_sec)
        self._music_id = music_id
        self._selected_track: dict[str, Any] | None = None
        self._selected_tracks_by_path: dict[str, dict[str, Any]] = {}

    def _fetch_rows(self, *, limit: int = 1) -> list[Mapping[str, Any]]:
        query = self._client.table(MUSIC_LIBRARY_TABLE).select(
            MUSIC_LIBRARY_SELECT_FIELDS,
        )
        if self._music_id:
            query = query.eq("id", self._music_id)
        else:
            query = query.gte("duration_sec", self._min_duration_sec)
            query = query.order("duration_sec")
            query = query.limit(max(1, int(limit)))
        rows = _response_data(query.execute()) or []
        if not isinstance(rows, list):
            raise MusicLibraryError("music_library query returned non-list data")
        return rows

    def select_tracks(self, *, count: int = 1) -> list[dict[str, Any]]:
        rows = self._fetch_rows(limit=count)
        if not rows:
            raise MusicLibraryError(
                "music_library returned no tracks with duration_sec >= "
                f"{self._min_duration_sec:.1f}",
            )
        tracks = [
            normalize_music_library_track(
                row,
                min_duration_sec=self._min_duration_sec,
            )
            for row in rows
        ]
        self._selected_track = tracks[0]
        return tracks

    def select_track(self) -> dict[str, Any]:
        return self.select_tracks(count=1)[0]

    def selected_track(self) -> dict[str, Any] | None:
        return dict(self._selected_track) if self._selected_track else None

    def music_metadata_for_path(self, path: str) -> dict[str, Any] | None:
        track = (
            self._selected_tracks_by_path.get(path)
            or self._selected_tracks_by_path.get(os.path.abspath(path))
        )
        if not track:
            return None
        return {
            "music_id": track["id"],
            "music_label": track["music_name"],
            "music_name": track["music_name"],
            "music_duration_sec": track["duration_sec"],
            "music_drive_file_id": track["drive_file_id"],
        }

    def fetch_bgm(self, tmp_dir: str) -> str:
        return self.fetch_bgms(tmp_dir, count=1)[0]

    def fetch_bgms(self, tmp_dir: str, *, count: int) -> list[str]:
        tracks = self.select_tracks(count=count)
        os.makedirs(tmp_dir, exist_ok=True)
        paths: list[str] = []
        for track in tracks:
            dest = os.path.join(tmp_dir, music_filename(track))
            download_drive_file(track["drive_file_id"], dest)
            self._selected_tracks_by_path[dest] = track
            self._selected_tracks_by_path[os.path.abspath(dest)] = track
            paths.append(dest)
        return paths
