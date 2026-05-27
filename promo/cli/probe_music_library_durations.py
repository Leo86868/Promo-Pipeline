#!/usr/bin/env python3
"""Dry-run duration probe for ``public.music_library`` rows.

This command downloads Drive-backed tracks to a temporary directory, measures
them with ffprobe, and prints JSON or SQL update statements. It does not write
to Supabase.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from typing import Any

from dotenv import load_dotenv

from promo.core.music_library import (
    MUSIC_LIBRARY_TABLE,
    download_drive_file,
)


def _response_data(response: Any) -> Any:
    return getattr(response, "data", response)


def probe_duration_sec(path: str) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return float(proc.stdout.strip())


def duration_update_sql(music_id: str, duration_sec: float) -> str:
    escaped_id = music_id.replace("'", "''")
    return (
        "update public.music_library "
        f"set duration_sec = {duration_sec:.6f} "
        f"where id = '{escaped_id}';"
    )


def _fetch_rows(client: Any, *, music_id: str | None, limit: int | None) -> list[dict]:
    query = client.table(MUSIC_LIBRARY_TABLE).select(
        "id,music_name,drive_file_id",
    )
    if music_id:
        query = query.eq("id", music_id)
    query = query.order("music_name")
    if limit is not None:
        query = query.limit(limit)
    rows = _response_data(query.execute()) or []
    if not isinstance(rows, list):
        raise RuntimeError("music_library query returned non-list data")
    return [dict(row) for row in rows]


def probe_row(row: Mapping[str, Any], tmp_dir: str) -> dict[str, Any]:
    music_id = str(row.get("id") or "")
    music_name = str(row.get("music_name") or "")
    drive_file_id = str(row.get("drive_file_id") or "")
    if not music_id or not music_name or not drive_file_id:
        raise RuntimeError(f"music row is missing id/music_name/drive_file_id: {row!r}")

    dest = os.path.join(tmp_dir, f"{music_id}.mp3")
    download_drive_file(drive_file_id, dest)
    return {
        "id": music_id,
        "music_name": music_name,
        "drive_file_id": drive_file_id,
        "duration_sec": probe_duration_sec(dest),
        "file_size_bytes": os.path.getsize(dest),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-run ffprobe duration backfill for public.music_library",
    )
    parser.add_argument("--music-id", default=None, help="Probe one music_library id")
    parser.add_argument("--limit", type=int, default=None, help="Maximum rows to probe")
    parser.add_argument(
        "--sql",
        action="store_true",
        help="Print UPDATE statements instead of JSON lines",
    )
    return parser


def main() -> None:
    load_dotenv()
    parser = _build_parser()
    args = parser.parse_args()

    url = os.environ.get("SUPABASE_URL")
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
        or os.environ.get("SUPABASE_ANON_KEY")
    )
    if not url or not key:
        parser.error("SUPABASE_URL and a Supabase key are required")
    try:
        from supabase import create_client
    except ImportError as exc:
        raise SystemExit("supabase package is required") from exc

    client = create_client(url, key)
    rows = _fetch_rows(client, music_id=args.music_id, limit=args.limit)
    with tempfile.TemporaryDirectory(prefix="pgc_music_probe_") as tmp_dir:
        for row in rows:
            result = probe_row(row, tmp_dir)
            if args.sql:
                print(duration_update_sql(result["id"], result["duration_sec"]))
            else:
                print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
