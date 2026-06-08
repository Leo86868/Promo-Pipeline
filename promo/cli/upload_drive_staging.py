#!/usr/bin/env python3
"""Upload PGC Drive staging inventory into Google Drive."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from promo.core import config
from promo.core.drive_staging import DriveStagingError, write_json
from promo.core.drive_upload import (
    DriveUploadError,
    OAuthDriveUploader,
    build_drive_upload_config,
    build_handoff_items_payload,
    load_staging_inventory,
    resolve_upload_context,
    upload_staging_inventory,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload PGC final MP4s from a Drive staging inventory",
    )
    parser.add_argument(
        "--inventory",
        required=True,
        help="Path to pgc_drive_staging_inventory JSON.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path to write the updated inventory JSON with drive:<file_id> URIs.",
    )
    parser.add_argument(
        "--handoff-items-output",
        help="Optional path to write export_release_handoff-compatible items JSON.",
    )
    parser.add_argument("--credentials-file", help="Google OAuth client_secret.json path.")
    parser.add_argument("--token-file", help="Google OAuth token.pickle path.")
    parser.add_argument(
        "--parent-folder-id",
        help=(
            "Existing Drive folder id for AIGC Production Masters. If omitted, "
            "the folder name is created/resolved under Drive root."
        ),
    )
    parser.add_argument(
        "--parent-folder-name",
        help="Top-level Drive folder name. Defaults to AIGC Production Masters.",
    )
    parser.add_argument("--paradigm", help="Folder segment. Defaults from inventory or pgc_65s.")
    parser.add_argument("--date", help="Folder segment. Defaults from inventory created_at or today.")
    parser.add_argument("--batch-id", help="Folder segment. Defaults from inventory or pgc_batch_upload.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inventory and print/write the target folder plan without uploading.",
    )
    return parser


def _credentials_file(args: argparse.Namespace) -> str:
    return args.credentials_file or config.google_credentials_file()


def _token_file(args: argparse.Namespace) -> str | None:
    return args.token_file or config.pgc_google_token_file() or None


def _parent_folder_id(args: argparse.Namespace) -> str | None:
    return args.parent_folder_id or config.pgc_drive_parent_folder_id() or None


def _parent_folder_name(args: argparse.Namespace) -> str:
    return args.parent_folder_name or config.pgc_drive_parent_folder_name()


def main() -> int:
    load_dotenv()
    parser = _parser()
    args = parser.parse_args()
    try:
        inventory = load_staging_inventory(Path(args.inventory))
        context = resolve_upload_context(
            inventory,
            paradigm=args.paradigm,
            date=args.date,
            batch_id=args.batch_id,
        )
        parent_folder_name = _parent_folder_name(args)
        if args.dry_run:
            inventory["drive_upload"] = {
                "status": "dry_run",
                "parent_folder_id": _parent_folder_id(args),
                "parent_folder_name": parent_folder_name,
                **context,
            }
        else:
            upload_config = build_drive_upload_config(
                credentials_file=_credentials_file(args),
                token_file=_token_file(args),
                parent_folder_id=_parent_folder_id(args),
                parent_folder_name=parent_folder_name,
            )
            uploader = OAuthDriveUploader(upload_config)
            inventory = upload_staging_inventory(
                inventory,
                uploader,
                parent_folder_id=upload_config.parent_folder_id,
                parent_folder_name=upload_config.parent_folder_name,
                paradigm=context["paradigm"],
                date=context["date"],
                batch_id=context["batch_id"],
            )
        write_json(Path(args.output), inventory)
        if args.handoff_items_output and not args.dry_run:
            write_json(
                Path(args.handoff_items_output),
                build_handoff_items_payload(inventory),
            )
    except (OSError, DriveStagingError, DriveUploadError) as exc:
        parser.error(str(exc))
    return 1 if inventory.get("drive_upload", {}).get("status") == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
