"""Read-only POI selection helpers for PGC batches."""

from __future__ import annotations

import json
import logging
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
from promo.core.format_profiles import get_promo_format_profile
from promo.core.run_receipt import required_active_assets
from promo.core.source_resolution_policy import (
    SourceResolutionPolicy,
    normalize_source_resolution_policy,
    source_resolution_matches,
    source_resolution_summary,
)


logger = logging.getLogger(__name__)

USAGE_EVENTS_TABLE = "poi_asset_usage_events"
DEFAULT_PGC_TARGET_DURATION_SEC = 65.0

# Paradigm scope for cooldown (Leo 2026-06-17): PGC's selection cooldown must
# count only PGC's OWN recent usage, not the music_remix (AIGC) paradigm that
# shares poi_asset_usage_events — otherwise a busy music_remix run cools PGC's
# pool (measured 2026-06-16: 145 POIs cooled by music_remix alone).
#
# CONVENTION-DEPENDENT: the shared table has NO paradigm column, so we partition
# by run_id prefix. PGC run_ids are emitted as ``pgc_run_<uuid>`` by
# run_manifest._new_id("pgc_run"). Verified airtight against live data
# (2026-06-17, 14749 events): every row whose output is PGC content (65s /
# pgc_batch_runs) carries a ``pgc_run_`` run_id (0 missed), and every
# ``pgc_run_`` row is PGC content (0 false-include); all non-pgc_run_ rows are
# music_remix (incl. descriptively-named batches like ``eu_expl_720drain_*``,
# which live under video_paradigms/music_remix/). If PGC ever runs a batch with
# a custom (non-default) run_id, this filter would silently drop it — pinned by
# test_fetch_recent_usage_scopes_to_pgc_run_prefix.
#
# NB: in SQL LIKE the underscore is a single-char WILDCARD, so the deployed
# pattern ``pgc_run_%`` technically means "p g c <any> r u n <any> ...". Live
# fidelity check (2026-06-17) confirmed it returns EXACTLY the literal-prefix
# set (53 POIs, 0 false-match) because no other run_id is shaped like that —
# safe in practice, but that's why this is the prefix and not an arbitrary one.
PGC_RUN_ID_PREFIX = "pgc_run_"


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


def _fetch_all_rows(
    query: Any,
    *,
    table_name: str,
    page_size: int = 1000,
    order_by: str,
) -> list[dict[str, Any]]:
    if page_size <= 0:
        raise BatchSelectionError("page_size must be positive")
    # 2026-06-09 fix: PostgREST gives no stable row order without an
    # explicit ORDER BY, so unordered .range() pages can skip/duplicate
    # rows when concurrent writers shift rows between page reads — a
    # skipped usage row silently breaks cooldown enforcement.
    query = query.order(order_by)
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
        order_by="asset_id",
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
        .gte("created_at", cutoff)
        # Paradigm scope: count only PGC's own usage (see PGC_RUN_ID_PREFIX).
        .like("run_id", f"{PGC_RUN_ID_PREFIX}%"),
        table_name=USAGE_EVENTS_TABLE,
        page_size=page_size,
        order_by="event_id",
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
    source_resolution_policy: SourceResolutionPolicy | dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    if min_active_assets <= 0:
        raise BatchSelectionError("min_active_assets must be positive")
    if bool(classification_field) != bool(classification_value):
        raise BatchSelectionError(
            "classification_field and classification_value must be provided together"
        )
    _require_classification_field(rows, classification_field=classification_field)

    cooldown_poi_ids = cooldown_poi_ids or set()
    resolved_source_policy = normalize_source_resolution_policy(source_resolution_policy)
    grouped: dict[str, dict[str, Any]] = {}
    asset_ids_by_poi: dict[str, set[str]] = defaultdict(set)
    source_policy_asset_ids_by_poi: dict[str, set[str]] = defaultdict(set)
    source_rows_by_poi: dict[str, list[dict[str, Any]]] = defaultdict(list)
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
        if source_resolution_matches(row, resolved_source_policy):
            source_policy_asset_ids_by_poi[poi_id].add(row["asset_id"])
            source_rows_by_poi[poi_id].append(row)
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
        source_policy_asset_ids = source_policy_asset_ids_by_poi[poi_id]
        effective_asset_ids = (
            asset_ids_by_poi[poi_id]
            if resolved_source_policy.mode == "best_available"
            else source_policy_asset_ids
        )
        source_policy_asset_count = len(source_policy_asset_ids)
        candidate_ready_asset_count = (
            len(effective_asset_ids & candidate_ready_ids)
            if candidate_ready_ids is not None
            else None
        )
        record = {
            **row,
            "active_asset_count": active_asset_count,
            "required_active_assets": int(min_active_assets),
            "source_resolution_policy": resolved_source_policy.to_dict(),
            "source_resolution_asset_count": source_policy_asset_count,
            "source_resolution_summary": source_resolution_summary(
                source_rows_by_poi[poi_id]
            ),
        }
        if candidate_ready_asset_count is not None:
            record["candidate_ready_asset_count"] = candidate_ready_asset_count
            record["required_candidate_ready_assets"] = int(min_active_assets)
        # Soft cooldown: the cooldown ledger is cross-paradigm (music_remix
        # writes to it too) so a hard skip starved PGC's eligible pool. A
        # cooled POI is no longer excluded — it stays eligible but flagged so
        # selection prefers fresh POIs and only falls back to cooled ones when
        # the fresh pool is exhausted. Asset-floor checks below still hard-skip
        # (cooldown does NOT rescue an asset-poor POI).
        if active_asset_count < min_active_assets:
            skipped.append({**record, "reason": "insufficient_active_assets"})
        elif len(effective_asset_ids) < min_active_assets:
            skipped.append({**record, "reason": "insufficient_source_resolution_assets"})
        elif (
            candidate_ready_asset_count is not None
            and candidate_ready_asset_count < min_active_assets
        ):
            reason = (
                "insufficient_candidate_ready_assets"
                if resolved_source_policy.mode == "best_available"
                else "insufficient_source_resolution_candidate_ready_assets"
            )
            skipped.append({**record, "reason": reason})
        else:
            record["cooldown"] = poi_id in cooldown_poi_ids
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
    """Select up to ``poi_count`` POIs, preferring fresh over cooled.

    Soft cooldown: ``eligible_pois`` may carry a ``cooldown`` flag (set by
    ``summarize_pois``). Selection samples from the fresh pool first; only when
    fresh is exhausted does it fall back to the cooled pool. Fresh is always
    preferred. Each returned record is tagged ``selected_as`` ("fresh" or
    "cooled_fallback") so callers can see which cooled POIs were used as
    fallback. Determinism: the same seed + same inputs yield the same picks
    (fresh and cooled are sampled with independent, seed-derived RNGs).
    """
    if poi_count <= 0:
        raise BatchSelectionError("poi_count must be positive")
    fresh = [poi for poi in eligible_pois if not poi.get("cooldown")]
    cooled = [poi for poi in eligible_pois if poi.get("cooldown")]
    available = len(fresh) + len(cooled)
    if available < poi_count and not allow_shortage:
        raise BatchSelectionError(
            f"not enough eligible POIs: requested {poi_count}, available {available}"
        )

    fresh_count = min(poi_count, len(fresh))
    selected = [
        {**poi, "selected_as": "fresh"}
        for poi in random.Random(seed).sample(list(fresh), fresh_count)
    ]
    remainder = min(poi_count - fresh_count, len(cooled))
    if remainder > 0:
        # Independent, seed-derived RNG keeps the cooled draw deterministic
        # without coupling it to how many fresh POIs happened to exist.
        cooled_seed = None if seed is None else seed + 1
        selected.extend(
            {**poi, "selected_as": "cooled_fallback"}
            for poi in random.Random(cooled_seed).sample(list(cooled), remainder)
        )
    return selected


def build_batch_spec(
    selected_pois: list[dict[str, Any]],
    *,
    videos_per_poi: int,
    target_duration_sec: float = DEFAULT_PGC_TARGET_DURATION_SEC,
    source_resolution_policy: SourceResolutionPolicy | dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = {
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
    resolved_source_policy = normalize_source_resolution_policy(source_resolution_policy)
    if resolved_source_policy.mode != "best_available":
        spec["source_resolution_policy"] = resolved_source_policy.to_dict()
    return spec


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
    source_resolution_policy: SourceResolutionPolicy | dict[str, Any] | None = None,
) -> dict[str, Any]:
    # P2 step 3: the asset floor comes from the format card routed by
    # the batch's target duration.
    gate_profile = get_promo_format_profile(target_duration_sec)
    min_active_assets = required_active_assets(
        videos_per_poi,
        base_min_assets_for_format=gate_profile.assets_base_min,
        extra_variation_asset_buffer=gate_profile.assets_per_extra,
    )
    resolved_source_policy = normalize_source_resolution_policy(source_resolution_policy)
    summary = summarize_pois(
        rows,
        min_active_assets=min_active_assets,
        candidate_ready_asset_ids=candidate_ready_asset_ids,
        cooldown_poi_ids=cooldown_poi_ids or set(),
        classification_field=classification_field,
        classification_value=classification_value,
        source_resolution_policy=resolved_source_policy,
    )
    selected = select_random_pois(
        summary["eligible_pois"],
        poi_count=poi_count,
        seed=seed,
        allow_shortage=allow_shortage,
    )
    fresh_eligible = sum(
        1 for poi in summary["eligible_pois"] if not poi.get("cooldown")
    )
    cooled_eligible = sum(
        1 for poi in summary["eligible_pois"] if poi.get("cooldown")
    )
    cooled_fallback_used = sum(
        1 for poi in selected if poi.get("selected_as") == "cooled_fallback"
    )
    if cooled_fallback_used:
        # Fail-loud-ish observability: never silently use recently-used POIs.
        logger.warning(
            "selection used %d recently-used (cooled) POIs as fallback — "
            "fresh pool had only %d for requested %d",
            cooled_fallback_used,
            fresh_eligible,
            poi_count,
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
                "source_resolution_policy": resolved_source_policy.to_dict(),
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
        "fresh_eligible": fresh_eligible,
        "cooled_eligible": cooled_eligible,
        "cooled_fallback_used": cooled_fallback_used,
        "batch_spec": build_batch_spec(
            selected,
            videos_per_poi=videos_per_poi,
            target_duration_sec=target_duration_sec,
            source_resolution_policy=resolved_source_policy,
        ),
    }


def write_json(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
