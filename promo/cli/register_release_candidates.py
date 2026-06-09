#!/usr/bin/env python3
"""Register approved PGC release candidates in Supabase."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from promo.core.release_candidates import (
    RELEASE_CANDIDATES_TABLE,
    ReleaseCandidateRegistrationError,
    load_release_candidate_records,
    register_release_candidates,
    summarize_release_candidates,
)


def _create_supabase_client_from_env() -> Any:
    url = os.environ.get("SUPABASE_URL")
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
        or os.environ.get("SUPABASE_ANON_KEY")
    )
    if not url or not key:
        raise ReleaseCandidateRegistrationError(
            "SUPABASE_URL and a Supabase key are required"
        )
    try:
        from supabase import create_client
    except ImportError as exc:
        raise ReleaseCandidateRegistrationError(
            "supabase package is required"
        ) from exc
    return create_client(url, key)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Register approved PGC release candidates",
    )
    parser.add_argument(
        "--handoff",
        required=True,
        help="JSON object with release_candidates from export_release_handoff.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually insert into Supabase. Omit for dry-run summary.",
    )
    return parser


def main() -> int:
    load_dotenv()
    parser = _parser()
    args = parser.parse_args()
    try:
        records = load_release_candidate_records(Path(args.handoff))
    except (
        OSError,
        json.JSONDecodeError,
        ReleaseCandidateRegistrationError,
    ) as exc:
        parser.error(str(exc))

    result: dict[str, Any] = {
        "execute": bool(args.execute),
        "table": RELEASE_CANDIDATES_TABLE,
        "summary": summarize_release_candidates(records),
        "release_candidates": records,
    }
    exit_code = 0
    if args.execute:
        try:
            client = _create_supabase_client_from_env()
            result["registration"] = register_release_candidates(client, records)
            if not result["registration"]["verification"]["verified"]:
                exit_code = 1
        except ReleaseCandidateRegistrationError as exc:
            parser.error(str(exc))

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
