#!/usr/bin/env python3
"""Read-only random eligible POI selector for PGC batch specs."""

from __future__ import annotations

import argparse
import json
import os
import sys

from dotenv import load_dotenv

from promo.core.batch_selection import (
    DEFAULT_PGC_TARGET_DURATION_SEC,
    BatchSelectionError,
    asset_ids_from_valid_clip_rows,
    build_selection_payload,
    fetch_recent_usage_poi_ids,
    fetch_ready_embedding_asset_ids,
    fetch_valid_clip_rows,
    write_json,
)


def _create_supabase_client():
    url = os.environ.get("SUPABASE_URL")
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
        or os.environ.get("SUPABASE_ANON_KEY")
    )
    if not url or not key:
        raise BatchSelectionError("SUPABASE_URL and a Supabase key are required")
    try:
        from supabase import create_client
    except ImportError as exc:
        raise BatchSelectionError("supabase package is required") from exc
    return create_client(url, key)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Select random eligible POIs and emit a run_batch-compatible JSON",
    )
    parser.add_argument("--poi-count", type=int, required=True)
    parser.add_argument("--videos-per-poi", type=int, default=3)
    parser.add_argument("--target-duration-sec", type=float, default=DEFAULT_PGC_TARGET_DURATION_SEC)
    parser.add_argument("--cooldown-days", type=int, default=3)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--classification-field", default=None)
    parser.add_argument("--classification-value", default=None)
    parser.add_argument(
        "--allow-shortage",
        action="store_true",
        help="Write a batch for all eligible POIs when fewer than requested pass filters.",
    )
    parser.add_argument(
        "--batch-output",
        help="Path to write the run_batch-compatible batch JSON.",
    )
    parser.add_argument(
        "--summary-output",
        help="Optional path to write the full selection/preflight summary JSON.",
    )
    return parser


def main() -> int:
    load_dotenv()
    parser = _parser()
    args = parser.parse_args()

    if bool(args.classification_field) != bool(args.classification_value):
        parser.error("--classification-field and --classification-value must be passed together")

    try:
        client = _create_supabase_client()
        rows = fetch_valid_clip_rows(client)
        ready_embedding_asset_ids = fetch_ready_embedding_asset_ids(
            client,
            asset_ids_from_valid_clip_rows(rows),
        )
        cooldown_poi_ids = fetch_recent_usage_poi_ids(
            client,
            cooldown_days=args.cooldown_days,
        )
        payload = build_selection_payload(
            rows=rows,
            poi_count=args.poi_count,
            videos_per_poi=args.videos_per_poi,
            candidate_ready_asset_ids=ready_embedding_asset_ids,
            cooldown_poi_ids=cooldown_poi_ids,
            cooldown_days=args.cooldown_days,
            target_duration_sec=args.target_duration_sec,
            seed=args.seed,
            classification_field=args.classification_field,
            classification_value=args.classification_value,
            allow_shortage=args.allow_shortage,
        )
    except BatchSelectionError as exc:
        parser.error(str(exc))

    if args.summary_output:
        write_json(args.summary_output, payload)
    if args.batch_output:
        write_json(args.batch_output, payload["batch_spec"])

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
