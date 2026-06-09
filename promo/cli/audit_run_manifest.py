#!/usr/bin/env python3
"""Audit PGC run manifests before usage writeback or release handoff."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from promo.core.manifest_audit import ManifestAuditError, audit_manifest_paths


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit PGC run manifests for production usage safety",
    )
    parser.add_argument(
        "manifest",
        nargs="+",
        help="Path to one or more run_manifest_*.json files.",
    )
    parser.add_argument(
        "--output",
        help="Optional audit JSON path. Defaults to stdout.",
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
        payload = audit_manifest_paths(manifest_paths)
    except (OSError, json.JSONDecodeError, ManifestAuditError) as exc:
        parser.error(str(exc))

    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 1 if payload["summary"]["failed_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
