"""Asset-library-native retrieval helpers.

This module is read-only. It ranks ready shared POI assets using centralized
embedding rows and returns candidate asset ids before any video download.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Any

import numpy as np

from promo.core.pipeline.poi_asset_valid_clips import build_poi_asset_valid_clip_snapshot
from promo.core.source_resolution_policy import (
    SourceResolutionPolicy,
    normalize_source_resolution_policy,
    source_resolution_matches,
)


EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
EMBEDDING_COMPOSITION_VERSION = 1
DEFAULT_TOP_K_PER_QUERY = 6
DEFAULT_MAX_CANDIDATES = 35
DEFAULT_MIN_ELIGIBLE_ASSETS = 50
DEFAULT_MIN_DOWNLOAD_CANDIDATES = 30


class AssetRetrievalError(RuntimeError):
    """Raised when shared asset retrieval cannot build a usable pool."""


@dataclass(frozen=True)
class BriefAsset:
    clip_id: str
    category: str | None
    scene_description: str | None
    shot_size: str | None
    main_subject: str | None
    camera_motion: str | None
    duration_sec: float
    usage_count: int


@dataclass(frozen=True)
class ReadyAsset:
    poi_id: str
    asset_id: str
    clip_id: str
    category: str | None
    scene_description: str | None
    shot_size: str | None
    main_subject: str | None
    camera_motion: str | None
    embedding_text: str
    duration_sec: float
    usage_count: int
    source_storage_bucket: str
    source_storage_path: str
    source_content_hash: str
    embedding_vector: tuple[float, ...]
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class RankedAsset:
    asset: ReadyAsset
    score: float
    query_index: int
    rank_for_query: int


def _response_data(response: Any) -> Any:
    return getattr(response, "data", response)


def brief_assets_from_rows(rows: list[dict[str, Any]]) -> list[BriefAsset]:
    """Normalize shared rows or clip metadata into brief-only visual assets."""
    assets: list[BriefAsset] = []
    for index, row in enumerate(rows, start=1):
        raw_duration = row.get("duration_sec", row.get("source_duration_sec", 0.0))
        try:
            duration_sec = float(raw_duration or 0.0)
        except (TypeError, ValueError):
            duration_sec = 0.0
        raw_usage = row.get("usage_count", 0)
        try:
            usage_count = int(raw_usage or 0)
        except (TypeError, ValueError):
            usage_count = 0
        clip_id = str(row.get("clip_id") or row.get("id") or f"{index:04d}").zfill(4)
        assets.append(
            BriefAsset(
                clip_id=clip_id,
                category=row.get("category"),
                scene_description=row.get("scene_description"),
                shot_size=row.get("shot_size"),
                main_subject=row.get("main_subject"),
                camera_motion=row.get("camera_motion"),
                duration_sec=duration_sec,
                usage_count=usage_count,
            )
        )
    return assets


def parse_embedding_vector(value: Any, *, expected_dim: int = EMBEDDING_DIM) -> tuple[float, ...]:
    """Parse a Supabase pgvector value into a float tuple."""
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("[") and raw.endswith("]"):
            raw = raw[1:-1]
        parts = [part.strip() for part in raw.split(",") if part.strip()]
        vector = tuple(float(part) for part in parts)
    elif isinstance(value, (list, tuple)):
        vector = tuple(float(part) for part in value)
    else:
        raise AssetRetrievalError(f"unsupported embedding_vector type: {type(value).__name__}")
    if len(vector) != expected_dim:
        raise AssetRetrievalError(
            f"embedding_vector dim {len(vector)} does not match expected {expected_dim}"
        )
    return vector


def fetch_ready_assets(
    client: Any,
    *,
    poi_id: str,
    model: str = EMBEDDING_MODEL,
    dim: int = EMBEDDING_DIM,
    composition_version: int = EMBEDDING_COMPOSITION_VERSION,
    chunk_size: int = 100,
    source_resolution_policy: SourceResolutionPolicy | dict[str, Any] | None = None,
) -> list[ReadyAsset]:
    """Fetch ready shared assets and vectors for one POI from Supabase."""
    resolved_source_policy = normalize_source_resolution_policy(source_resolution_policy)
    valid_rows = _response_data(
        client.table("poi_asset_valid_clips")
        .select("*")
        .eq("poi_id", poi_id)
        .order("clip_id")
        .execute()
    ) or []
    if not isinstance(valid_rows, list):
        raise AssetRetrievalError("poi_asset_valid_clips query returned non-list data")
    snapshot = build_poi_asset_valid_clip_snapshot(valid_rows, poi_id=poi_id)
    if not snapshot:
        raise AssetRetrievalError(f"no valid shared assets found for poi_id={poi_id}")
    snapshot = [
        row for row in snapshot
        if source_resolution_matches(row, resolved_source_policy)
    ]
    if not snapshot:
        raise AssetRetrievalError(
            "no valid shared assets matched source_resolution_policy="
            f"{resolved_source_policy.to_dict()} for poi_id={poi_id}"
        )

    asset_ids = [row["asset_id"] for row in snapshot]
    embedding_rows: list[dict[str, Any]] = []
    for start in range(0, len(asset_ids), chunk_size):
        chunk = asset_ids[start:start + chunk_size]
        rows = _response_data(
            client.table("poi_asset_embeddings")
            .select(
                "asset_id,embedding_text,embedding_vector,status,generated_at,"
                "embedding_model,embedding_dim,embedding_composition_version"
            )
            .in_("asset_id", chunk)
            .eq("embedding_model", model)
            .eq("embedding_dim", dim)
            .eq("embedding_composition_version", composition_version)
            .eq("status", "ready")
            .execute()
        ) or []
        if not isinstance(rows, list):
            raise AssetRetrievalError("poi_asset_embeddings query returned non-list data")
        embedding_rows.extend(rows)

    embeddings_by_asset_id = {
        str(row["asset_id"]): row
        for row in embedding_rows
        if row.get("embedding_vector") is not None and row.get("generated_at") is not None
    }
    ready_assets: list[ReadyAsset] = []
    for row in snapshot:
        embedding = embeddings_by_asset_id.get(row["asset_id"])
        if embedding is None:
            continue
        embedding_text = str(embedding.get("embedding_text") or row.get("embedding_text") or "")
        if not embedding_text:
            continue
        ready_assets.append(
            ReadyAsset(
                poi_id=row["poi_id"],
                asset_id=row["asset_id"],
                clip_id=row["clip_id"],
                category=row.get("category"),
                scene_description=row.get("scene_description"),
                shot_size=row.get("shot_size"),
                main_subject=row.get("main_subject"),
                camera_motion=row.get("camera_motion"),
                embedding_text=embedding_text,
                duration_sec=float(row["duration_sec"]),
                usage_count=int(row.get("usage_count") or 0),
                source_storage_bucket=row["source_storage_bucket"],
                source_storage_path=row["source_storage_path"],
                source_content_hash=row["source_content_hash"],
                embedding_vector=parse_embedding_vector(embedding["embedding_vector"], expected_dim=dim),
                width=row.get("width"),
                height=row.get("height"),
            )
        )
    return sorted(ready_assets, key=lambda asset: asset.clip_id)


def clip_metadata_from_ready_assets(assets: list[ReadyAsset]) -> list[dict[str, Any]]:
    """Project ready shared assets into the clip metadata shape used downstream."""
    return [
        {
            "id": asset.clip_id,
            "asset_id": asset.asset_id,
            "category": asset.category,
            "scene_description": asset.scene_description,
            "shot_size": asset.shot_size,
            "main_subject": asset.main_subject,
            "camera_motion": asset.camera_motion,
            "source_duration_sec": asset.duration_sec,
            "width": asset.width,
            "height": asset.height,
        }
        for asset in sorted(assets, key=lambda item: item.clip_id)
    ]


def candidate_asset_ids_for_download(
    *,
    candidates: list[RankedAsset],
    assets: list[ReadyAsset],
    min_candidates: int = DEFAULT_MIN_DOWNLOAD_CANDIDATES,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> list[str]:
    """Return the candidate download pool, padded for Gemini #2/bridge reserve."""
    if min_candidates <= 0 or max_candidates <= 0:
        raise AssetRetrievalError("min_candidates and max_candidates must be positive")
    if min_candidates > max_candidates:
        raise AssetRetrievalError("min_candidates must be <= max_candidates")

    seen: set[str] = set()
    asset_ids: list[str] = []
    for ranked in candidates:
        asset_id = ranked.asset.asset_id
        if asset_id in seen:
            continue
        seen.add(asset_id)
        asset_ids.append(asset_id)
        if len(asset_ids) >= max_candidates:
            return asset_ids

    reserve = sorted(
        assets,
        key=lambda asset: (
            asset.usage_count,
            -(asset.duration_sec or 0.0),
            asset.clip_id,
        ),
    )
    for asset in reserve:
        if len(asset_ids) >= min_candidates:
            break
        if asset.asset_id in seen:
            continue
        seen.add(asset.asset_id)
        asset_ids.append(asset.asset_id)
    return asset_ids


def _duration_band(duration_sec: float) -> str:
    if duration_sec < 6.0:
        return "<6s"
    if duration_sec < 8.0:
        return "6-8s"
    return ">=8s"


def _count_values(
    assets: list[ReadyAsset] | list[BriefAsset],
    attr: str,
) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for asset in assets:
        value = getattr(asset, attr) or "unknown"
        counts[value] = counts.get(value, 0) + 1
    return [
        {"value": value, "count": count}
        for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def sample_brief_display_assets(
    assets: list[ReadyAsset] | list[BriefAsset],
    *,
    seed: int,
    target_min: int = 50,
    target_max: int = 60,
    category_weights: dict[str, float] | None = None,
) -> list[Any]:
    """V2 brief sampler (A 层): per-video stratified sample of the pool.

    The brief's WHOLE-POOL stats stay truthful elsewhere; this only
    decides which CONCRETE scenes each video's brief gets to show, so a
    constant library stops seeding every script with the same salient
    details (the fire-pit / "two and a half acres" convergence).

    - Stratified by category, proportional allocation (largest
      remainder), floor 1 per non-empty category — coverage structure
      is preserved, only the members rotate.
    - ``seed`` rides the per-video canonical-ordinal channel (same as
      the hook deal): deterministic and reproducible, never global
      randomness.
    - ``category_weights`` is the reserved C-slot (题材发牌): None means
      plain proportional; a future topic-card dealer supplies per-
      category multipliers — only the weight SOURCE changes, not this
      function's shape.
    - Pools at or under ``target_min`` are returned whole (small stores
      keep their full brief).
    """
    import random

    if target_min <= 0 or target_max < target_min:
        raise AssetRetrievalError("invalid brief sampler targets")
    if len(assets) <= target_min:
        return list(assets)
    target = min(target_max, len(assets))

    grouped: dict[str, list[Any]] = {}
    for asset in assets:
        grouped.setdefault(asset.category or "unknown", []).append(asset)

    weights = {
        category: len(members) * float((category_weights or {}).get(category, 1.0))
        for category, members in grouped.items()
    }
    total_weight = sum(weights.values()) or 1.0

    # Largest-remainder allocation with floor 1 per non-empty category.
    shares = {
        category: target * weight / total_weight
        for category, weight in weights.items()
    }
    alloc = {
        category: max(1, min(len(grouped[category]), int(share)))
        for category, share in shares.items()
    }
    remainders = sorted(
        grouped,
        key=lambda c: (-(shares[c] - int(shares[c])), c),
    )
    idx = 0
    while sum(alloc.values()) < target and idx < len(remainders) * 2:
        category = remainders[idx % len(remainders)]
        if alloc[category] < len(grouped[category]):
            alloc[category] += 1
        idx += 1

    sampled: list[Any] = []
    for category in sorted(grouped):
        members = sorted(grouped[category], key=lambda a: a.clip_id)
        # str seeds hash stably (sha-based), unlike object hash().
        rng = random.Random(f"{seed}:{category}")
        sampled.extend(rng.sample(members, min(alloc[category], len(members))))
    return sampled


def _coverage_phrases(
    assets: list[ReadyAsset] | list[BriefAsset],
    *,
    max_phrases: int,
    rotation_seed: int | None = None,
) -> list[dict[str, Any]]:
    """Pick diverse visible motifs without exposing Gemini #1 to clip ids.

    ``rotation_seed`` (V2 brief sampler B 层) rotates WHICH ``max_phrases``
    of the eligible motif list are shown — without it the same top-4
    led every video's brief for a category.
    """
    candidates: list[dict[str, Any]] = []
    seen_subjects: set[str] = set()
    ordered = sorted(
        assets,
        key=lambda asset: (
            asset.usage_count,
            -(asset.duration_sec or 0.0),
            asset.clip_id,
        ),
    )
    for asset in ordered:
        phrase = (asset.main_subject or asset.scene_description or "").strip()
        if not phrase:
            continue
        subject_key = (asset.main_subject or phrase).strip().lower()
        if subject_key in seen_subjects:
            continue
        seen_subjects.add(subject_key)
        candidates.append({
            "phrase": phrase,
            "duration_sec": round(float(asset.duration_sec), 2),
            "usage_count": asset.usage_count,
        })
    if not candidates:
        return []
    start = (int(rotation_seed) % len(candidates)) if rotation_seed else 0
    rotated = candidates[start:] + candidates[:start]
    return rotated[:max_phrases]


def _asset_visual_detail(
    asset: ReadyAsset | BriefAsset,
    *,
    coverage_role: str,
) -> dict[str, Any]:
    detail = (asset.scene_description or asset.main_subject or "").strip()
    if not detail:
        detail = str(getattr(asset, "embedding_text", "") or "").strip()
    return {
        "category": asset.category or "unknown",
        "coverage_role": coverage_role,
        "visual_detail": detail,
        "duration_sec": round(float(asset.duration_sec), 2),
        "usage_count": asset.usage_count,
        "shot_size": asset.shot_size,
        "camera_motion": asset.camera_motion,
    }


def _category_grounding_items(
    assets: list[ReadyAsset] | list[BriefAsset],
    *,
    coverage_role: str,
    max_items: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    ordered = sorted(
        assets,
        key=lambda asset: (
            asset.usage_count,
            -(asset.duration_sec or 0.0),
            asset.clip_id,
        ),
    )
    for asset in ordered:
        detail = (asset.scene_description or asset.main_subject or "").strip()
        if not detail:
            continue
        key = detail.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(_asset_visual_detail(asset, coverage_role=coverage_role))
        if len(items) >= max_items:
            break
    return items


def build_asset_visual_brief(
    assets: list[ReadyAsset] | list[BriefAsset],
    *,
    motifs_per_category: int = 4,
    signature_visual_count: int = 3,
    max_grounding_items: int = 36,
    display_assets: list[Any] | None = None,
    motif_seed: int | None = None,
) -> dict[str, Any]:
    """Return coverage-first visual grounding for Gemini #1.

    The brief avoids clip ids because Gemini #1 should learn what is visible,
    not lock onto individual assets before retrieval chooses candidates.

    V2 brief sampler split (2026-06-12, Leo's call): per-category STATS
    (asset_count / seconds / bands / mixes) always come from the FULL
    ``assets`` pool — the writer should know the store's true size.
    The CONCRETE scenes shown (coverage_motifs / grounding_set /
    core_visuals) come from ``display_assets`` when provided (the
    per-video stratified sample), so each video's brief advertises
    different members of the same structure. ``motif_seed`` additionally
    rotates which motifs lead each category (B 层).
    """
    grouped: dict[str, list[ReadyAsset | BriefAsset]] = {}
    for asset in assets:
        grouped.setdefault(asset.category or "unknown", []).append(asset)

    display_grouped: dict[str, list[Any]] = grouped
    if display_assets is not None:
        display_grouped = {}
        for asset in display_assets:
            display_grouped.setdefault(asset.category or "unknown", []).append(asset)

    categories: list[dict[str, Any]] = []
    for category, category_assets in grouped.items():
        duration_bands: dict[str, int] = {}
        for asset in category_assets:
            band = _duration_band(float(asset.duration_sec))
            duration_bands[band] = duration_bands.get(band, 0) + 1
        categories.append({
            "category": category,
            "asset_count": len(category_assets),
            "total_seconds": round(sum(float(asset.duration_sec) for asset in category_assets), 2),
            "duration_bands": [
                {"band": band, "count": count}
                for band, count in sorted(duration_bands.items())
            ],
            "shot_mix": _count_values(category_assets, "shot_size"),
            "motion_mix": _count_values(category_assets, "camera_motion"),
            "coverage_motifs": _coverage_phrases(
                display_grouped.get(category, []),
                max_phrases=motifs_per_category,
                rotation_seed=motif_seed,
            ),
        })
    categories = sorted(
        categories,
        key=lambda row: (-int(row["asset_count"]), str(row["category"])),
    )
    total_assets = len(assets)
    secondary_threshold = max(4, ceil(total_assets * 0.06))
    core_categories = {
        str(row["category"])
        for row in categories[:3]
    }
    secondary_categories = {
        str(row["category"])
        for row in categories
        if int(row["asset_count"]) >= secondary_threshold
        and str(row["category"]) not in core_categories
    }

    grounding_set: list[dict[str, Any]] = []
    # Concrete grounding items come from the display subset (sampled);
    # category order still follows full-pool size ranking.
    grouped_sorted = {
        category: display_grouped.get(category, [])
        for category in [str(row["category"]) for row in categories]
    }
    for category, category_assets in grouped_sorted.items():
        if category in core_categories:
            role = "core"
            quota = 6
        elif category in secondary_categories:
            role = "secondary"
            quota = 4
        else:
            role = "supporting_detail"
            quota = 2
        grounding_set.extend(
            _category_grounding_items(
                category_assets,
                coverage_role=role,
                max_items=quota,
            )
        )
        if len(grounding_set) >= max_grounding_items:
            grounding_set = grounding_set[:max_grounding_items]
            break

    core_visuals: list[dict[str, Any]] = []
    seen_signature_phrases: set[str] = set()
    for category_row in categories:
        if str(category_row["category"]) not in core_categories:
            continue
        for motif in category_row["coverage_motifs"]:
            phrase = str(motif.get("phrase") or "")
            if not phrase or phrase.lower() in seen_signature_phrases:
                continue
            seen_signature_phrases.add(phrase.lower())
            core_visuals.append({**motif, "category": category_row["category"]})
            break
        if len(core_visuals) >= signature_visual_count:
            break
    return {
        "eligible_asset_count": len(assets),
        "eligible_total_seconds": round(sum(float(asset.duration_sec) for asset in assets), 2),
        "categories": categories,
        "core_visuals": core_visuals,
        "grounding_set": grounding_set,
        "summary_note": (
            "Core items describe dominant coverage. Secondary and supporting "
            "details are available visual specifics, not core promises. No "
            "item is a specific clip assignment, and asset ids are "
            "intentionally omitted."
        ),
    }


def build_visual_summary(
    assets: list[ReadyAsset],
    *,
    examples_per_category: int = 3,
) -> dict[str, Any]:
    """Backward-compatible wrapper for the v1 Asset Visual Brief."""
    return build_asset_visual_brief(
        assets,
        motifs_per_category=examples_per_category,
    )


def _cosine_scores(query_vector: tuple[float, ...], assets: list[ReadyAsset]) -> np.ndarray:
    query = np.asarray(query_vector, dtype=np.float32)
    matrix = np.asarray([asset.embedding_vector for asset in assets], dtype=np.float32)
    query_norm = np.linalg.norm(query)
    if query_norm == 0:
        raise AssetRetrievalError("query embedding has zero norm")
    matrix_norms = np.linalg.norm(matrix, axis=1)
    safe_norms = np.where(matrix_norms == 0, 1.0, matrix_norms)
    scores = (matrix @ query) / (safe_norms * query_norm)
    return np.where(matrix_norms == 0, -np.inf, scores)


def retrieve_candidates(
    *,
    assets: list[ReadyAsset],
    queries: list[str],
    query_vectors: list[tuple[float, ...]],
    top_k_per_query: int = DEFAULT_TOP_K_PER_QUERY,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    min_eligible_assets: int = DEFAULT_MIN_ELIGIBLE_ASSETS,
) -> list[RankedAsset]:
    """Rank assets for queries and return first-seen deduped candidates."""
    if len(assets) < min_eligible_assets:
        raise AssetRetrievalError(
            f"eligible asset pool must be >= {min_eligible_assets}; got {len(assets)}"
        )
    if not queries:
        raise AssetRetrievalError("at least one retrieval query is required")
    if len(queries) != len(query_vectors):
        raise AssetRetrievalError("queries and query_vectors length mismatch")
    if top_k_per_query <= 0 or max_candidates <= 0:
        raise AssetRetrievalError("top_k_per_query and max_candidates must be positive")

    seen: set[str] = set()
    candidates: list[RankedAsset] = []
    for query_index, query_vector in enumerate(query_vectors):
        scores = _cosine_scores(query_vector, assets)
        order = np.argsort(-scores)
        for rank_for_query, asset_index in enumerate(order[:top_k_per_query], start=1):
            asset = assets[int(asset_index)]
            if asset.asset_id in seen:
                continue
            seen.add(asset.asset_id)
            candidates.append(
                RankedAsset(
                    asset=asset,
                    score=float(scores[int(asset_index)]),
                    query_index=query_index,
                    rank_for_query=rank_for_query,
                )
            )
            if len(candidates) >= max_candidates:
                return candidates
    return candidates


def build_script_retrieval_queries(scripts: list[dict[str, Any]]) -> list[str]:
    """Extract Gemini #1 segment text for shared-asset semantic retrieval."""
    queries: list[str] = []
    for script in scripts:
        segments = script.get("segments") or []
        if not isinstance(segments, list):
            continue
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            text = str(segment.get("text") or "").strip()
            if text:
                queries.append(text)
    if not queries:
        raise AssetRetrievalError("no script segment text available for retrieval")
    return queries


def ranked_asset_to_dict(ranked: RankedAsset) -> dict[str, Any]:
    asset = ranked.asset
    return {
        "asset_id": asset.asset_id,
        "clip_id": asset.clip_id,
        "category": asset.category,
        "score": round(ranked.score, 6),
        "query_index": ranked.query_index,
        "rank_for_query": ranked.rank_for_query,
        "duration_sec": asset.duration_sec,
        "usage_count": asset.usage_count,
        "scene_description": asset.scene_description,
        "embedding_text": asset.embedding_text,
        "source_storage_bucket": asset.source_storage_bucket,
        "source_storage_path": asset.source_storage_path,
        "source_content_hash": asset.source_content_hash,
    }
