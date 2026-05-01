"""Backend Protocol for promo pipeline I/O abstraction.

The promo module needs exactly 3 external I/O operations:
  1. Fetch video clips for a POI → local files
  2. Fetch background music → local file
  3. Save the rendered output → destination

This module defines a Protocol for those operations and provides one
implementation:
  - LocalBackend: standalone default (reads from local directories)

Usage:
    # Standalone default
    backend = LocalBackend(clips_dir="./my_clips", output_dir="./output")
    clips = backend.fetch_clips("Hotel Name", tmp_dir="/tmp/work")
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Regex for extracting 4-digit clip IDs from filenames.
# Prefers "clip_XXXX" pattern (pipeline convention), falls back to
# any isolated 4-digit group not adjacent to other digits.
_CLIP_ID_PATTERN = re.compile(r"clip[_\-]?(\d{4})")
_CLIP_ID_FALLBACK = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def _extract_clip_id(filename: str) -> str | None:
    """Extract a 4-digit clip ID from a filename.

    Tries the pipeline convention (clip_XXXX) first, then falls back
    to the LAST isolated 4-digit group in the filename. The last match
    is preferred because clip IDs typically appear after prefixes like
    resolutions (1920x1080) or dates.
    """
    m = _CLIP_ID_PATTERN.search(filename)
    if m:
        return m.group(1)
    matches = _CLIP_ID_FALLBACK.findall(filename)
    if matches:
        return matches[-1]
    return None


from promo.core import sanitize_poi_name as _sanitize_name


@runtime_checkable
class PromoBackend(Protocol):
    """Interface for promo pipeline external I/O.

    Any object implementing these methods can drive the promo pipeline.
    """

    def fetch_clips(self, poi_name: str, tmp_dir: str) -> dict[str, str]:
        """Download/copy clips for a POI into tmp_dir.

        Returns:
            dict mapping clip_id (4-digit string like "0001") to local file path.
        """
        ...

    def fetch_bgm(self, poi_name: str, tmp_dir: str) -> str | None:
        """Fetch a background music track into tmp_dir.

        Returns:
            Path to the local BGM file, or None to use the default fallback.
        """
        ...

    def save_output(self, poi_name: str, video_path: str) -> str:
        """Save the rendered promo video to its final destination.

        Args:
            poi_name: Hotel/POI name.
            video_path: Path to the rendered MP4 on local disk.

        Returns:
            Final location string (URL, file path, or Drive ID).
        """
        ...

    def clips_dir(self) -> str | None:
        """Directory containing the backend's clip inventory.

        Returned path is the source-of-truth location for the POI's
        `clips/`; callers use it to derive sibling paths like
        `.mimo_cache/` and `.embedding_cache/`. ``None`` means "no
        backing directory" (remote-only backends) — downstream code
        should no-op any sibling-path derivations.
        """
        ...

    def output_dir(self) -> str | None:
        """Directory where sidecars + the rendered MP4 are staged.

        ``None`` means "no configured output root" — callers should fall
        back to `os.path.dirname(output_path)` of whatever file they are
        about to write.
        """
        ...


class LocalBackend:
    """Standalone mode: read clips from a local directory, no external services.

    This backend is designed for independent development and testing.
    Someone who receives the promo module can use this without Supabase or Drive.

    Expected clips directory structure:
        clips_dir/
            clip_0001.mp4
            clip_0002.mp4
            ...
        OR any .mp4 files with 4-digit numbers in the filename.
    """

    def __init__(
        self,
        clips_dir: str,
        bgm_path: str = None,
        output_dir: str = None,
    ):
        """Initialize the local backend.

        Args:
            clips_dir: Directory containing .mp4 clip files.
            bgm_path: Path to a BGM .mp3 file, or None for default fallback.
            output_dir: Directory to copy the final video into. If None, the
                        video stays at its render location.
        """
        self._clips_dir = clips_dir
        self._bgm_path = bgm_path
        self._output_dir = output_dir

    def fetch_clips(self, poi_name: str, tmp_dir: str) -> dict[str, str]:
        """Copy clips from the local clips directory into tmp_dir.

        Scans for .mp4 files and extracts 4-digit clip IDs from filenames.
        Skips unreadable files gracefully (logs warning, continues).
        """
        if not os.path.isdir(self._clips_dir):
            logger.error("Clips directory does not exist: %s", self._clips_dir)
            return {}

        clip_paths: dict[str, str] = {}
        patterns = [
            os.path.join(self._clips_dir, "*.mp4"),
            os.path.join(self._clips_dir, "*.MP4"),
        ]
        files = []
        for pattern in patterns:
            files.extend(glob.glob(pattern))

        for filepath in sorted(files):
            filename = os.path.basename(filepath)
            clip_id = _extract_clip_id(filename)
            if not clip_id:
                continue

            if clip_id in clip_paths:
                logger.warning(
                    "Clip ID collision: '%s' already mapped to %s, skipping %s",
                    clip_id, os.path.basename(clip_paths[clip_id]), filename,
                )
                continue

            dest = os.path.join(tmp_dir, filename)
            try:
                shutil.copy2(filepath, dest)
            except OSError as exc:
                logger.warning("Failed to copy clip %s: %s", filepath, exc)
                continue
            clip_paths[clip_id] = dest

        logger.info("Loaded %d clips from local dir: %s", len(clip_paths), self._clips_dir)
        return clip_paths

    def fetch_bgm(self, poi_name: str, tmp_dir: str) -> str | None:
        """Return the configured local BGM path, or None for default fallback."""
        if self._bgm_path and os.path.isfile(self._bgm_path):
            logger.info("Using local BGM: %s", self._bgm_path)
            return self._bgm_path
        if self._bgm_path:
            logger.warning("BGM file not found: %s, will use fallback", self._bgm_path)
        return None

    def save_output(self, poi_name: str, video_path: str) -> str:
        """Copy the rendered video to the output directory.

        Sprint 18 C: on collision (same destination basename already on
        disk), bump the basename via the same ``-N`` algorithm
        ``_write_sidecar`` uses so a back-to-back same-POI rerun keeps the
        prior MP4 on disk and the new MP4 lands at ``<stem>-2.mp4``,
        ``<stem>-3.mp4``, ... — matching the bumped-sidecar pairing the
        sidecar writer already produces. The 999-attempt cap raises
        ``OSError`` (this method's return contract is ``str``, so a typed
        raise is the closest mirror to the sidecar writer's ``False``
        terminator).
        """
        if not self._output_dir:
            return video_path

        os.makedirs(self._output_dir, exist_ok=True)
        filename = os.path.basename(video_path)
        if not filename:
            safe_name = _sanitize_name(poi_name)
            filename = f"promo_{safe_name}.mp4"
        dest = os.path.join(self._output_dir, filename)
        # Skip copy if already in the output directory
        if os.path.abspath(video_path) == os.path.abspath(dest):
            logger.info("Output already at destination: %s", dest)
            return dest

        stem, ext = os.path.splitext(filename)
        candidate = dest
        bump = 2
        while os.path.exists(candidate):
            candidate = os.path.join(self._output_dir, f"{stem}-{bump}{ext}")
            bump += 1
            if bump > 999:
                logger.warning(
                    "Giving up on save_output collision bump after 999 attempts "
                    "in %s for base %s; raising OSError.",
                    self._output_dir, filename,
                )
                raise OSError(
                    f"collision-bump exhausted for {filename!r} in {self._output_dir!r} "
                    "after 999 attempts"
                )
        if candidate != dest:
            logger.info(
                "Output %s already exists; bumping to %s to preserve prior deliverable.",
                filename, os.path.basename(candidate),
            )
        # Sprint 18 audit-fix D-001: atomic write — copy to a `.tmp`
        # sibling then `os.replace` onto the final candidate. A SIGKILL
        # mid-copy leaves the `.tmp` orphan (never the target), so a
        # crash cannot occupy the next collision-bump slot with a partial
        # MP4. Mirrors the sidecar writer's tempfile + os.replace pattern.
        tmp_path = candidate + ".tmp"
        try:
            shutil.copy2(video_path, tmp_path)
            os.replace(tmp_path, candidate)
        except OSError:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            raise
        logger.info("Output saved: %s", candidate)
        return candidate

    def clips_dir(self) -> str | None:
        return self._clips_dir

    def output_dir(self) -> str | None:
        return self._output_dir
