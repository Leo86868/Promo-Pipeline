#!/usr/bin/env python3
"""Export approved PGC outputs as AIGC release-candidate handoff JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from promo.core.pipeline.release_handoff import (
    ReleaseHandoffError,
    build_release_handoff_from_items_file,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build AIGC release-candidate handoff JSON from approved manifests",
    )
    parser.add_argument(
        "--items",
        required=True,
        help=(
            "JSON list, or object with items list. Each item needs "
            "run_manifest_path plus drive_file_id or source_output_uri."
        ),
    )
    parser.add_argument(
        "--output",
        help="Optional output JSON path. Defaults to stdout.",
    )
    parser.add_argument(
        "--source-batch-id",
        help="Optional source_batch_id applied to rows missing one.",
    )
    parser.add_argument(
        "--approved-at",
        help="Optional ISO approved_at applied to rows missing one. Defaults to now.",
    )
    parser.add_argument(
        "--source-pipeline",
        default="pgc_65s",
        help="source_pipeline value. Defaults to pgc_65s.",
    )
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        payload = build_release_handoff_from_items_file(
            Path(args.items),
            default_source_batch_id=args.source_batch_id,
            default_approved_at=args.approved_at,
            default_source_pipeline=args.source_pipeline,
        )
    except (OSError, ReleaseHandoffError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
