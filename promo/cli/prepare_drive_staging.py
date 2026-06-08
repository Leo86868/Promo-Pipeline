#!/usr/bin/env python3
"""Prepare manifest-backed Drive staging inventory for PGC final videos."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from promo.core.drive_staging import (
    DriveStagingError,
    apply_drive_file_map,
    build_staging_inventory,
    handoff_items_from_inventory,
    load_drive_file_map,
    manifest_paths_from_receipt,
    write_json,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a manifest-backed Drive staging inventory",
    )
    parser.add_argument(
        "manifest",
        nargs="*",
        help="Path to one or more run_manifest_*.json files",
    )
    parser.add_argument(
        "--receipt",
        help=(
            "Optional RUN_RECEIPT.json. Uses only manifest-audit-passed videos. "
            "Do not combine with positional manifest paths."
        ),
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path to write the staging inventory JSON",
    )
    parser.add_argument(
        "--drive-file-map",
        help=(
            "Optional JSON mapping source_video_key -> raw Drive file id, "
            "or list of objects with source_video_key and drive_file_id."
        ),
    )
    parser.add_argument(
        "--handoff-items-output",
        help="Optional path to write export_release_handoff-compatible items JSON.",
    )
    parser.add_argument(
        "--allow-missing-source",
        action="store_true",
        help="Allow inventory creation when local MP4 source files are not present.",
    )
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if bool(args.manifest) == bool(args.receipt):
        parser.error("provide either manifest paths or --receipt, but not both")

    try:
        manifest_paths = (
            manifest_paths_from_receipt(Path(args.receipt))
            if args.receipt
            else [Path(path) for path in args.manifest]
        )
        missing = [str(path) for path in manifest_paths if not path.exists()]
        if missing:
            parser.error("manifest path does not exist: " + ", ".join(missing))
        inventory = build_staging_inventory(
            manifest_paths,
            require_source_exists=not args.allow_missing_source,
        )
        if args.drive_file_map:
            inventory = apply_drive_file_map(
                inventory,
                load_drive_file_map(Path(args.drive_file_map)),
            )
        write_json(Path(args.output), inventory)
        if args.handoff_items_output:
            write_json(
                Path(args.handoff_items_output),
                {"items": handoff_items_from_inventory(inventory)},
            )
    except (OSError, DriveStagingError) as exc:
        parser.error(str(exc))

    return 0


if __name__ == "__main__":
    sys.exit(main())
