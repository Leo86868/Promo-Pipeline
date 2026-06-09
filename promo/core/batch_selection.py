"""Read-only POI selection helpers for PGC batches."""

from __future__ import annotations

import json
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from promo.core.pipeline.poi_asset_valid_clips import (
    POI_ASSET_VALID_CLIPS_VIEW,
    normalize_poi_asset_valid_clip_row,
)
from promo.core.assets.retrieval import (
    EMBEDDING_COMPOSITION_VERSION,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
)
from promo.core.run_receipt import required_active_assets


USAGE_EVENTS_TABLE = "poi_asset_usage_events"
DEFAULT_PGC_TARGET_DURATION_SEC = 65.0


class BatchSelectionError(ValueError):
    """Raised when read-only batch selection cannot satisfy the request."""


def _response_data(response: Any) -> Any:
    return getattr(response, "data", response)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def cooldown_cutoff_iso(cooldown_days: int, *, now: datetime | None = None) -> str:
    if cooldown_days < 0:
        raise BatchSelectionError("cooldown_days must be >= 0")
    anchor = now or utc_now()
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    cutoff = anchor.astimezone(timezone.utc) - timedelta(days=int(cooldown_days))
    return cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fetch_all_rows(query: Any, *, table_name: str, page_size: int = 1000) -> list[dict[str, Any]]:
    if page_size <= 0:
        raise BatchSelectionError("page_size must be positive")
    rows: list[dict[str, Any]] = []
    start = 0
    while True:
        page = _response_data(
            query.range(start, start + page_size - 1).execute()
        ) or []
        if not isinstance(page, list):
            raise BatchSelectionError(f"{table_name} query returned non-list data")
        rows.extend(page)
        if len(page) < page_size:
            break
        start += page_size
    return rows


def fetch_valid_clip_rows(client: Any, *, page_size: int = 1000) -> list[dict[str, Any]]:
    query = client.table(POI_ASSET_VALID_CLIPS_VIEW).select("*")
    return _fetch_all_rows(
        query,
        table_name=POI_ASSET_VALID_CLIPS_VIEW,
        page_size=page_size,
    )


def asset_ids_from_valid_clip_rows(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({str(row["asset_id"]) for row in rows if row.get("asset_id")})


def fetch_ready_embedding_asset_ids(
    client: Any,
    asset_ids: list[str],
    *,
    model: str = EMBEDDING_MODEL,
    dim: int = EMBEDDING_DIM,
    composition_version: int = EMBEDDING_COMPOSITION_VERSION,
    chunk_size: int = 100,
) -> set[str]:
    if chunk_size <= 0:
        raise BatchSelectionError("chunk_size must be positive")
    ready_asset_ids: set[str] = set()
    normalized_asset_ids = sorted({str(asset_id) for asset_id in asset_ids if asset_id})
    for start in range(0, len(normalized_asset_ids), chunk_size):
        chunk = normalized_asset_ids[start:start + chunk_size]
        rows = _response_data(
            client.table("poi_asset_embeddings")
            .select(
                "asset_id,status,generated_at,embedding_model,embedding_dim,"
                "embedding_composition_version"
            )
            .in_("asset_id", chunk)
            .eq("embedding_model", model)
            .eq("embedding_dim", dim)
            .eq("embedding_composition_version", composition_version)
            .eq("status", "ready")
            .not_.is_("embedding_vector", "null")
            .not_.is_("generated_at", "null")
            .execute()
        ) or []
        if not isinstance(rows, list):
            raise BatchSelectionError("poi_asset_embeddings query returned non-list data")
        ready_asset_ids.update(str(row["asset_id"]) for row in rows if row.get("asset_id"))
    return ready_asset_ids


def fetch_recent_usage_poi_ids(
    client: Any,
    *,
    cooldown_days: int,
    now: datetime | None = None,
    page_size: int = 1000,
) -> set[str]:
    if cooldown_days <= 0:
        return set()
    cutoff = cooldown_cutoff_iso(cooldown_days, now=now)
    rows = _fetch_all_rows(
        client.table(USAGE_EVENTS_TABLE)
        .select("poi_id,created_at")
        .gte("created_at", cutoff),
        table_name=USAGE_EVENTS_TABLE,
        page_size=page_size,
    )
    return {
        str(row["poi_id"])
        for row in rows
        if row.get("poi_id")
    }


def _classification_matches(
    row: dict[str, Any],
    *,
    classification_field: str | None,
    classification_value: str | None,
) -> bool:
    if not classification_field:
        return True
    raw = row.get(classification_field)
    if isinstance(raw, list):
        return classification_value in {str(item) for item in raw}
    return str(raw) == classification_value


def _require_classification_field(
    rows: list[dict[str, Any]],
    *,
    classification_field: str | None,
) -> None:
    if not classification_field:
        return
    if not rows or not any(classification_field in row for row in rows):
        raise BatchSelectionError(
            f"classification field is not available: {classification_field}"
        )


def summarize_pois(
    rows: list[dict[str, Any]],
    *,
    min_active_assets: int,
    candidate_ready_asset_ids: set[str] | None = None,
    cooldown_poi_ids: set[str] | None = None,
    classification_field: str | None = None,
    classification_value: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    if min_active_assets <= 0:
        raise BatchSelectionError("min_active_assets must be positive")
    if bool(classification_field) != bool(classification_value):
        raise BatchSelectionError(
            "classification_field and classification_value must be provided together"
        )
    _require_classification_field(rows, classification_field=classification_field)

    cooldown_poi_ids = cooldown_poi_ids or set()
    grouped: dict[str, dict[str, Any]] = {}
    asset_ids_by_poi: dict[str, set[str]] = defaultdict(set)
    candidate_ready_ids = (
        {str(asset_id) for asset_id in candidate_ready_asset_ids}
        if candidate_ready_asset_ids is not None
        else None
    )
    for raw_row in rows:
        if not _classification_matches(
            raw_row,
            classification_field=classification_field,
            classification_value=classification_value,
        ):
            continue
        row = normalize_poi_asset_valid_clip_row(raw_row)
        poi_id = row["poi_id"]
        asset_ids_by_poi[poi_id].add(row["asset_id"])
        grouped.setdefault(
            poi_id,
            {
                "poi_id": poi_id,
                "poi_name": row.get("display_name") or row.get("canonical_key") or poi_id,
                "canonical_key": row.get("canonical_key"),
                "location": raw_row.get("location") or "",
            },
        )

    eligible: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for poi_id, row in grouped.items():
        active_asset_count = len(asset_ids_by_poi[poi_id])
        candidate_ready_asset_count = (
            len(asset_ids_by_poi[poi_id] & candidate_ready_ids)
            if candidate_ready_ids is not None
            else None
        )
        record = {
            **row,
            "active_asset_count": active_asset_count,
            "required_active_assets": int(min_active_assets),
        }
        if candidate_ready_asset_count is not None:
            record["candidate_ready_asset_count"] = candidate_ready_asset_count
            record["required_candidate_ready_assets"] = int(min_active_assets)
        if poi_id in cooldown_poi_ids:
            skipped.append({**record, "reason": "cooldown"})
        elif active_asset_count < min_active_assets:
            skipped.append({**record, "reason": "insufficient_active_assets"})
        elif (
            candidate_ready_asset_count is not None
            and candidate_ready_asset_count < min_active_assets
        ):
            skipped.append({**record, "reason": "insufficient_candidate_ready_assets"})
        else:
            eligible.append(record)

    sort_key = lambda item: (str(item["poi_name"]), str(item["poi_id"]))
    return {
        "eligible_pois": sorted(eligible, key=sort_key),
        "skipped_pois": sorted(skipped, key=sort_key),
    }


def select_random_pois(
    eligible_pois: list[dict[str, Any]],
    *,
    poi_count: int,
    seed: int | None = None,
    allow_shortage: bool = False,
) -> list[dict[str, Any]]:
    if poi_count <= 0:
        raise BatchSelectionError("poi_count must be positive")
    if len(eligible_pois) < poi_count and not allow_shortage:
        raise BatchSelectionError(
            f"not enough eligible POIs: requested {poi_count}, available {len(eligible_pois)}"
        )
    count = min(poi_count, len(eligible_pois))
    rng = random.Random(seed)
    return rng.sample(list(eligible_pois), count)


def build_batch_spec(
    selected_pois: list[dict[str, Any]],
    *,
    videos_per_poi: int,
    target_duration_sec: float = DEFAULT_PGC_TARGET_DURATION_SEC,
) -> dict[str, Any]:
    return {
        "pois": [
            {
                "poi_id": poi["poi_id"],
                "name": poi["poi_name"],
                "location": poi.get("location") or "",
                "canonical_key": poi.get("canonical_key"),
            }
            for poi in selected_pois
        ],
        "videos_per_poi": int(videos_per_poi),
        "target_duration_sec": float(target_duration_sec),
    }


def build_selection_payload(
    *,
    rows: list[dict[str, Any]],
    poi_count: int,
    videos_per_poi: int,
    candidate_ready_asset_ids: set[str] | None = None,
    cooldown_poi_ids: set[str] | None = None,
    cooldown_days: int = 3,
    target_duration_sec: float = DEFAULT_PGC_TARGET_DURATION_SEC,
    seed: int | None = None,
    classification_field: str | None = None,
    classification_value: str | None = None,
    allow_shortage: bool = False,
) -> dict[str, Any]:
    min_active_assets = required_active_assets(videos_per_poi)
    summary = summarize_pois(
        rows,
        min_active_assets=min_active_assets,
        candidate_ready_asset_ids=candidate_ready_asset_ids,
        cooldown_poi_ids=cooldown_poi_ids or set(),
        classification_field=classification_field,
        classification_value=classification_value,
    )
    selected = select_random_pois(
        summary["eligible_pois"],
        poi_count=poi_count,
        seed=seed,
        allow_shortage=allow_shortage,
    )
    shortage = max(poi_count - len(summary["eligible_pois"]), 0)
    return {
        "schema_version": 1,
        "status": "shortage" if shortage else "ok",
        "request": {
            "poi_count": int(poi_count),
            "videos_per_poi": int(videos_per_poi),
            "target_duration_sec": float(target_duration_sec),
            "selection": "random_equal",
            "seed": seed,
            "filters": {
                "classification_field": classification_field,
                "classification_value": classification_value,
                "cooldown_days": int(cooldown_days),
                "required_active_assets": int(min_active_assets),
                **(
                    {
                        "required_candidate_ready_assets": int(min_active_assets),
                    }
                    if candidate_ready_asset_ids is not None
                    else {}
                ),
            },
        },
        "selected_pois": selected,
        "eligible_pois": summary["eligible_pois"],
        "skipped_pois": summary["skipped_pois"],
        "shortage_count": shortage,
        "batch_spec": build_batch_spec(
            selected,
            videos_per_poi=videos_per_poi,
            target_duration_sec=target_duration_sec,
        ),
    }


def write_json(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
