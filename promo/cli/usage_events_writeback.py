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
from promo.core.manifest_audit import audit_manifest_paths


RPC_NAME = "rpc_record_poi_asset_usage_events"
USAGE_EVENTS_TABLE = "poi_asset_usage_events"
VERIFY_SELECT_COLUMNS = (
    "event_id,manifest_id,run_id,poi_id,asset_id,clip_id,"
    "variant_index,occurrence_index,occurrence_id,usage_role"
)
VERIFY_COMPARE_FIELDS = (
    "manifest_id",
    "run_id",
    "poi_id",
    "asset_id",
    "clip_id",
    "variant_index",
    "occurrence_index",
    "occurrence_id",
    "usage_role",
)


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


def _chunked(items: list[str], chunk_size: int) -> list[list[str]]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    return [
        items[index:index + chunk_size]
        for index in range(0, len(items), chunk_size)
    ]


def _coerce_compare_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def fetch_usage_event_rows(
    client: Any,
    event_ids: list[str],
    *,
    chunk_size: int = 100,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chunk in _chunked(event_ids, chunk_size):
        response = (
            client.table(USAGE_EVENTS_TABLE)
            .select(VERIFY_SELECT_COLUMNS)
            .in_("event_id", chunk)
            .execute()
        )
        data = _response_data(response) or []
        if not isinstance(data, list):
            raise ValueError("usage verification query returned non-list data")
        rows.extend(data)
    return rows


def verify_usage_events(
    client: Any,
    events: list[dict[str, Any]],
    *,
    chunk_size: int = 100,
) -> dict[str, Any]:
    expected_by_id: dict[str, dict[str, Any]] = {}
    duplicate_expected_event_ids: list[str] = []
    for event in events:
        event_id = str(event.get("event_id") or "")
        if not event_id:
            raise ValueError("usage events require event_id")
        if event_id in expected_by_id:
            duplicate_expected_event_ids.append(event_id)
        expected_by_id[event_id] = event

    if duplicate_expected_event_ids:
        raise ValueError("usage events contain duplicate event_id values")

    if not expected_by_id:
        return {
            "verified": True,
            "expected_count": 0,
            "observed_count": 0,
            "missing_count": 0,
            "mismatch_count": 0,
            "duplicate_observed_event_id_count": 0,
            "missing_event_ids": [],
            "mismatches": [],
            "duplicate_observed_event_ids": [],
        }

    observed_rows = fetch_usage_event_rows(
        client,
        list(expected_by_id),
        chunk_size=chunk_size,
    )
    observed_by_id: dict[str, dict[str, Any]] = {}
    duplicate_observed_event_ids: list[str] = []
    for row in observed_rows:
        event_id = str(row.get("event_id") or "")
        if event_id in observed_by_id:
            duplicate_observed_event_ids.append(event_id)
        observed_by_id[event_id] = row

    missing_event_ids = sorted(set(expected_by_id) - set(observed_by_id))
    mismatches: list[dict[str, Any]] = []
    for event_id, expected in expected_by_id.items():
        observed = observed_by_id.get(event_id)
        if observed is None:
            continue
        fields: list[dict[str, Any]] = []
        for field in VERIFY_COMPARE_FIELDS:
            expected_value = _coerce_compare_value(expected.get(field))
            observed_value = _coerce_compare_value(observed.get(field))
            if expected_value != observed_value:
                fields.append({
                    "field": field,
                    "expected": expected.get(field),
                    "observed": observed.get(field),
                })
        if fields:
            mismatches.append({
                "event_id": event_id,
                "fields": fields,
            })

    verified = not (
        missing_event_ids
        or mismatches
        or duplicate_observed_event_ids
    )
    return {
        "verified": verified,
        "expected_count": len(expected_by_id),
        "observed_count": len(observed_rows),
        "missing_count": len(missing_event_ids),
        "mismatch_count": len(mismatches),
        "duplicate_observed_event_id_count": len(duplicate_observed_event_ids),
        "missing_event_ids": missing_event_ids,
        "mismatches": mismatches,
        "duplicate_observed_event_ids": sorted(set(duplicate_observed_event_ids)),
    }


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
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip post-write verification query after --execute.",
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

    manifest_audit: dict[str, Any] | None = None
    if args.execute:
        manifest_audit = audit_manifest_paths(manifest_paths)
        if manifest_audit["summary"]["failed_count"]:
            result = {
                "execute": True,
                "rpc_name": RPC_NAME,
                "manifest_audit": manifest_audit,
            }
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 1

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
    if manifest_audit is not None:
        result["manifest_audit"] = manifest_audit
    exit_code = 0
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
                call: dict[str, Any] = {
                    "manifest_id": manifest["manifest_id"],
                    "event_count": len(events),
                    **rpc_result,
                }
                if not args.no_verify:
                    verification = verify_usage_events(client, events)
                    call["verification"] = verification
                    if not verification["verified"]:
                        exit_code = 1
                calls.append(call)
            result["rpc_calls"] = calls
        except ValueError as exc:
            parser.error(str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
