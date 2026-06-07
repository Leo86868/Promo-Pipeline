#!/usr/bin/env python3
"""Dry-run shared asset retrieval without downloading videos or writing Supabase."""

from __future__ import annotations

import argparse
import json
import os
import sys

from dotenv import load_dotenv

from promo.core.assets.retrieval import (
    EMBEDDING_DIM,
    AssetRetrievalError,
    build_asset_visual_brief,
    fetch_ready_assets,
    ranked_asset_to_dict,
    retrieve_candidates,
)
from promo.core.assign.clip_embedder import embed_texts


DEFAULT_QUERIES = [
    "oceanfront arrival at a luxury coastal resort",
    "pool cabanas and Pacific ocean views",
    "guest room balcony and relaxing resort details",
    "beach spa dining and sunset atmosphere",
]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-run semantic retrieval against shared POI assets",
    )
    parser.add_argument("--poi-id", required=True, help="Stable shared POI id")
    parser.add_argument(
        "--query",
        action="append",
        default=[],
        help="Retrieval query text. Can be passed multiple times.",
    )
    parser.add_argument("--top-k-per-query", type=int, default=6)
    parser.add_argument("--max-candidates", type=int, default=35)
    parser.add_argument("--min-eligible-assets", type=int, default=50)
    parser.add_argument("--examples-per-category", type=int, default=3)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a human summary",
    )
    return parser


def _create_supabase_client():
    url = os.environ.get("SUPABASE_URL")
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
        or os.environ.get("SUPABASE_ANON_KEY")
    )
    if not url or not key:
        raise AssetRetrievalError("SUPABASE_URL and a Supabase key are required")
    try:
        from supabase import create_client
    except ImportError as exc:
        raise AssetRetrievalError("supabase package is required") from exc
    return create_client(url, key)


def _print_human(payload: dict) -> None:
    print(f"POI: {payload['poi_id']}")
    print(f"Ready assets: {payload['ready_asset_count']}")
    print(f"Queries: {len(payload['queries'])}")
    print(f"Candidates: {len(payload['candidates'])}")
    print()
    brief = payload["asset_visual_brief"]
    print(
        "Asset Visual Brief: "
        f"{brief['eligible_asset_count']} assets, "
        f"{brief['eligible_total_seconds']:.1f}s total"
    )
    for row in brief["categories"]:
        motifs = "; ".join(
            str(item.get("phrase") or "")
            for item in row["coverage_motifs"][:3]
        )
        print(
            f"- {row['category']}: {row['asset_count']} clips, "
            f"{row['total_seconds']:.1f}s ({motifs})"
        )
    core_visuals = "; ".join(
        str(item.get("phrase") or "") for item in brief["core_visuals"]
    )
    if core_visuals:
        print(f"Core visuals: {core_visuals}")
    print("Grounding set:")
    for item in brief["grounding_set"]:
        print(
            f"  - [{item['coverage_role']}] {item['category']}: "
            f"{item['visual_detail']}"
        )
    secondary_count = sum(
        1 for item in brief["grounding_set"]
        if item["coverage_role"] == "secondary"
    )
    supporting_count = sum(
        1 for item in brief["grounding_set"]
        if item["coverage_role"] == "supporting_detail"
    )
    if secondary_count or supporting_count:
        print(
            "Non-core detail items: "
            f"{secondary_count} secondary, {supporting_count} supporting"
        )
    print()
    print("Top candidates:")
    for idx, item in enumerate(payload["candidates"], start=1):
        desc = str(item.get("scene_description") or "").replace("\n", " ")
        if len(desc) > 120:
            desc = desc[:117] + "..."
        print(
            f"{idx:02d}. clip {item['clip_id']} {item['asset_id']} "
            f"[{item.get('category')}] score={item['score']:.4f} "
            f"query={item['query_index'] + 1} rank={item['rank_for_query']} - {desc}"
        )


def main() -> int:
    load_dotenv()
    parser = _build_parser()
    args = parser.parse_args()
    queries = [query.strip() for query in args.query if query.strip()] or DEFAULT_QUERIES

    try:
        client = _create_supabase_client()
        assets = fetch_ready_assets(client, poi_id=args.poi_id)
        query_vectors = [
            tuple(vector)
            for vector in embed_texts(queries)
        ]
        for vector in query_vectors:
            if len(vector) != EMBEDDING_DIM:
                raise AssetRetrievalError(
                    f"query embedding dim {len(vector)} does not match {EMBEDDING_DIM}"
                )
        candidates = retrieve_candidates(
            assets=assets,
            queries=queries,
            query_vectors=query_vectors,
            top_k_per_query=args.top_k_per_query,
            max_candidates=args.max_candidates,
            min_eligible_assets=args.min_eligible_assets,
        )
    except AssetRetrievalError as exc:
        parser.error(str(exc))

    payload = {
        "poi_id": args.poi_id,
        "ready_asset_count": len(assets),
        "queries": queries,
        "asset_visual_brief": build_asset_visual_brief(
            assets,
            motifs_per_category=args.examples_per_category,
        ),
        "candidates": [ranked_asset_to_dict(item) for item in candidates],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_human(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
