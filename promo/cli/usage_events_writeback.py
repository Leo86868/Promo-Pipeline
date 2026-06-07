#!/usr/bin/env python3
"""Record manifest-derived usage events through the Supabase RPC."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from promo.cli.usage_events_preview import build_preview


RPC_NAME = "rpc_record_poi_asset_usage_events"


def _response_data(response: Any) -> Any:
    return getattr(response, "data", response)


def _create_supabase_client_from_env() -> Any:
    url = os.environ.get("SUPABASE_URL")
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
        or os.environ.get("SUPABASE_ANON_KEY")
    )
    if not url or not key:
        raise ValueError("SUPABASE_URL and a Supabase key are required")
    try:
        from supabase import create_client
    except ImportError as exc:
        raise ValueError("supabase package is required") from exc
    return create_client(url, key)


def _normalize_rpc_result(data: Any) -> dict[str, int]:
    if isinstance(data, list):
        if not data:
            return {"inserted_count": 0, "duplicate_count": 0}
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError("usage RPC returned an unexpected shape")
    return {
        "inserted_count": int(data.get("out_inserted_count") or 0),
        "duplicate_count": int(data.get("out_duplicate_count") or 0),
    }


def record_usage_events(client: Any, events: list[dict[str, Any]]) -> dict[str, int]:
    response = client.rpc(RPC_NAME, {"p_payload": events}).execute()
    return _normalize_rpc_result(_response_data(response))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write manifest-derived POI asset usage events",
    )
    parser.add_argument(
        "manifest",
        nargs="+",
        help="Path to one or more run_manifest_*.json files",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually call the Supabase usage RPC. Omit for dry-run summary.",
    )
    return parser


def main() -> int:
    load_dotenv()
    parser = _parser()
    args = parser.parse_args()
    manifest_paths = [Path(path) for path in args.manifest]
    missing = [str(path) for path in manifest_paths if not path.exists()]
    if missing:
        parser.error("manifest path does not exist: " + ", ".join(missing))

    try:
        preview = build_preview(manifest_paths)
    except ValueError as exc:
        parser.error(str(exc))

    result: dict[str, Any] = {
        "execute": bool(args.execute),
        "rpc_name": RPC_NAME,
        "summary": preview["summary"],
        "manifests": preview["manifests"],
    }
    if args.execute:
        try:
            client = _create_supabase_client_from_env()
            calls = []
            for manifest in preview["manifests"]:
                events = [
                    event for event in preview["events"]
                    if event["manifest_id"] == manifest["manifest_id"]
                ]
                rpc_result = record_usage_events(client, events)
                calls.append({
                    "manifest_id": manifest["manifest_id"],
                    "event_count": len(events),
                    **rpc_result,
                })
            result["rpc_calls"] = calls
        except ValueError as exc:
            parser.error(str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
