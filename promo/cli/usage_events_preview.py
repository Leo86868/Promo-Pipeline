#!/usr/bin/env python3
"""Build local usage-event preview JSON from run manifests.

This CLI is intentionally read-only. It derives the payload shape PGC would
send later, but it does not call Supabase.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from promo.core.pipeline.run_manifest import (
    USAGE_EVENT_CONTRACT_VERSION,
    build_usage_events_from_manifest,
)


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _sidecar_path(manifest_path: Path, stored_path: str | None) -> Path | None:
    if not stored_path:
        return None
    path = Path(stored_path)
    if path.exists():
        return path
    local_sibling = manifest_path.parent / path.name
    if local_sibling.exists():
        return local_sibling
    return None


def _retrieval_context(manifest_path: Path, manifest: dict[str, Any]) -> dict[str, str | None]:
    sidecars = manifest.get("sidecars") or {}
    path = _sidecar_path(manifest_path, sidecars.get("clip_assignments"))
    if path is None:
        return {
            "retrieval_contract": None,
            "retrieval_fallback_reason": None,
        }
    payload = _load_json(path)
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "retrieval_contract": payload.get("retrieval_contract"),
        "retrieval_fallback_reason": payload.get("fallback_reason"),
    }


def _summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "event_count": len(events),
        "unique_event_id_count": len({event["event_id"] for event in events}),
        "poi_count": len({event["poi_id"] for event in events}),
        "asset_count": len({event["asset_id"] for event in events}),
        "role_counts": dict(Counter(event["usage_role"] for event in events)),
    }


def build_preview(manifest_paths: list[Path]) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    for path in manifest_paths:
        manifest = _load_json(path)
        context = _retrieval_context(path, manifest)
        manifest_events = build_usage_events_from_manifest(
            manifest,
            retrieval_contract=context["retrieval_contract"],
            retrieval_fallback_reason=context["retrieval_fallback_reason"],
        )
        manifests.append({
            "path": str(path),
            "manifest_id": manifest.get("manifest_id"),
            "run_id": manifest.get("run_id"),
            "poi_id": (manifest.get("poi") or {}).get("poi_id"),
            "event_count": len(manifest_events),
            **context,
        })
        events.extend(manifest_events)
    return {
        "schema_version": 1,
        "event_contract_version": USAGE_EVENT_CONTRACT_VERSION,
        "summary": _summary(events),
        "manifests": manifests,
        "events": events,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview manifest-derived POI asset usage events",
    )
    parser.add_argument(
        "manifest",
        nargs="+",
        help="Path to one or more run_manifest_*.json files",
    )
    parser.add_argument(
        "--output",
        help="Optional output JSON path. Defaults to stdout.",
    )
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    manifest_paths = [Path(path) for path in args.manifest]
    missing = [str(path) for path in manifest_paths if not path.exists()]
    if missing:
        parser.error("manifest path does not exist: " + ", ".join(missing))
    try:
        payload = build_preview(manifest_paths)
    except ValueError as exc:
        parser.error(str(exc))
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
