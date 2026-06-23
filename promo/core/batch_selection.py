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
from promo.core.atomic_io import atomic_write_text
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
# SQL LIKE pattern for the prefix. A bare '_' in LIKE is a single-char wildcard,
# so "pgc_run_%" would also match imposters like "pgcXrunY..."; escape each
# underscore (Postgres default backslash escape -> '\_' = literal '_') so only
# the literal prefix matches.
PGC_RUN_ID_LIKE_PATTERN = PGC_RUN_ID_PREFIX.replace("_", r"\_") + "%"


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
        .like("run_id", PGC_RUN_ID_LIKE_PATTERN),
        table_name=USAGE_EVENTS_TABLE,
        page_size=page_size,
        order_by="event_id",
    )
    return {
        str(row["poi_id"])
        for row in rows
        if row.get("poi_id")
    }


# In-progress POI lock (2026-06-18): skip POIs already claimed by a not-yet-
# finished sibling batch under the same runs-root, so concurrent/overlapping
# batches stop colliding on the same POI. Ported from music_remix's receipt
# soft-lock (AIGC commit 0021ff2), ADAPTED because PGC splits the two facts that
# music_remix keeps in one RUN_RECEIPT into two files:
#   - CLAIMED POIs  -> selection_summary.json:selected_pois[].poi_id  (written
#     at selection time, BEFORE rendering -> the lock fires early, narrowing the
#     race vs music_remix which only learns the claim once the receipt lands)
#   - COMPLETION    -> RUN_RECEIPT.json per-video ``state``
# Release rule (fail-closed): a sibling releases its POIs ONLY when its
# RUN_RECEIPT.json exists AND every video state == "complete". A missing receipt
# (selected but not yet rendering), any non-complete video, or an unreadable
# receipt all keep the claim. A crashed/abandoned batch therefore holds its POIs
# until it is --resume'd or its dir is cleaned (deliberate, per Leo 2026-06-18 —
# no TTL/staleness release; keep it simple + fail-loud). It is a SOFT lock: two
# batches that both finish selection before either writes selection_summary.json
# can still pick the same POI. Safe for staggered/sequential launches; NOT a
# hard mutex for truly simultaneous launches.
SELECTION_SUMMARY_FILENAME = "selection_summary.json"
RUN_RECEIPT_FILENAME = "RUN_RECEIPT.json"
RELEASED_VIDEO_STATE = "complete"


def _sibling_batch_released(run_dir: Path) -> bool:
    """True only when this sibling batch is fully done (releases its POI claims).

    Fail-closed: returns False (claim held) when RUN_RECEIPT.json is missing,
    has no videos, has any non-``complete`` video, or is unreadable/corrupt.
    """
    receipt_path = run_dir / RUN_RECEIPT_FILENAME
    if not receipt_path.is_file():
        return False
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "in-progress lock: unreadable receipt %s (%s) — treating as in-progress",
            receipt_path,
            exc,
        )
        return False
    videos = receipt.get("videos")
    if not isinstance(videos, list) or not videos:
        return False
    return all(
        isinstance(video, dict) and video.get("state") == RELEASED_VIDEO_STATE
        for video in videos
    )


def collect_in_progress_poi_ids(
    runs_root: str | Path,
    *,
    exclude_dir: str | Path | None = None,
) -> dict[str, str]:
    """Map ``poi_id -> claiming batch dir name`` for every POI claimed by a
    not-yet-finished sibling batch under ``runs_root``.

    Scans ``runs_root/*/selection_summary.json`` for claims, and consults each
    sibling's RUN_RECEIPT.json for completion (see ``_sibling_batch_released``).
    ``exclude_dir`` (the current run's own output dir) is skipped so a batch
    never locks against itself. A missing ``runs_root`` returns ``{}`` (no-op).
    An unreadable selection_summary.json is warned-and-skipped for that one dir
    only — it never disables the lock for the rest.
    """
    locks: dict[str, str] = {}
    root = Path(runs_root)
    if not root.is_dir():
        return locks
    exclude = Path(exclude_dir).resolve() if exclude_dir is not None else None
    for summary_path in sorted(root.glob(f"*/{SELECTION_SUMMARY_FILENAME}")):
        run_dir = summary_path.parent
        if exclude is not None and run_dir.resolve() == exclude:
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Loud, unlike music_remix's silent skip: PGC's claim lives in this
            # separate file, so losing it silently would drop a real claim.
            logger.warning(
                "in-progress lock: skipping unreadable %s (%s) — its POI claims "
                "are NOT enforced this round",
                summary_path,
                exc,
            )
            continue
        if _sibling_batch_released(run_dir):
            continue
        batch_id = run_dir.name
        for poi in summary.get("selected_pois") or []:
            if not isinstance(poi, dict):
                continue
            poi_id = str(poi.get("poi_id") or "").strip()
            if poi_id:
                locks.setdefault(poi_id, batch_id)  # first claimant wins
    return locks


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
    # Internal helper: default kept for unit-test ergonomics. The PRODUCTION
    # boundary that enforces "you must decide about readiness" is
    # build_selection_payload (which has NO default); production never reaches
    # this helper except through it, so the silent-skip footgun is closed there.
    candidate_ready_asset_ids: set[str] | None = None,
    cooldown_poi_ids: set[str] | None = None,
    in_progress_poi_ids: set[str] | None = None,
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
    in_progress_poi_ids = in_progress_poi_ids or set()
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
        # Schema-drift sentinel (fail-loud): poi_description is a POI-level facts
        # column the view is expected to expose. A NULL value (POI not yet
        # described) is fine and handled downstream (DESCRIPTION段 omitted); the
        # whole COLUMN going missing is schema drift and must hard-stop, never
        # silently run every POI description-less. Check key presence, NOT value.
        if "poi_description" not in raw_row:
            raise BatchSelectionError(
                "poi_asset_valid_clips is missing the poi_description column "
                "(schema drift) — refusing to run a batch that would silently "
                "drop every POI's facts. Fix the view, then re-run."
            )
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
                # POI-level facts card (~2700 chars). NULL/missing value → "" →
                # DESCRIPTION段 omitted downstream (normal for un-described POIs).
                # Pulled from raw_row, NOT the normalized snapshot, so it never
                # touches _SNAPSHOT_FIELDS / the recipe dedup fingerprint.
                "poi_description": raw_row.get("poi_description") or "",
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
        # In-progress lock takes precedence over asset/cooldown gates (mirrors
        # music_remix: a locked POI reports ``in_progress_lock`` regardless of
        # its other state) and is a hard exclude — it never enters ``eligible``.
        if poi_id in in_progress_poi_ids:
            skipped.append({**record, "reason": "in_progress_lock"})
        elif active_asset_count < min_active_assets:
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
                "poi_description": poi.get("poi_description") or "",
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
    # REQUIRED, no default (2026-06-22): this is the production boundary, and a
    # silent default here was a latent footgun — a missing readiness set
    # degrades the gate from "width-matched AND embedding-ready >= floor" to
    # "width-matched only", so a POI with enough wide clips but too few embedded
    # assets passes selection then HARD-FAILS at retrieval (retrieve_candidates
    # demands >=50 ready assets regardless of source policy). Forcing the caller
    # to pass it makes "forgot it" an immediate TypeError, not a silent skip.
    # Pass the real set (fetch_ready_embedding_asset_ids); pass None ONLY as a
    # conscious, reviewable choice to skip the readiness gate in unit tests that
    # isolate other selection logic.
    candidate_ready_asset_ids: set[str] | None,
    cooldown_poi_ids: set[str] | None = None,
    in_progress_poi_ids: set[str] | None = None,
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
    in_progress_poi_ids = in_progress_poi_ids or set()
    summary = summarize_pois(
        rows,
        min_active_assets=min_active_assets,
        candidate_ready_asset_ids=candidate_ready_asset_ids,
        cooldown_poi_ids=cooldown_poi_ids or set(),
        in_progress_poi_ids=in_progress_poi_ids,
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
    # Make silent degradation visible: how many of the POIs we're about to
    # render actually carry a facts card. A NULL description is legitimate
    # (un-described POI), but a sudden collapse to 0 is the signal that the
    # upstream onboarding/backfill broke — surface it every batch.
    described_selected = sum(
        1 for poi in selected if (poi.get("poi_description") or "").strip()
    )
    logger.info(
        "poi_description: %d/%d selected POIs carry a non-empty facts card "
        "(%d will omit the DESCRIPTION段)",
        described_selected,
        len(selected),
        len(selected) - described_selected,
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
        "in_progress_locked_poi_count": len(in_progress_poi_ids),
        "batch_spec": build_batch_spec(
            selected,
            videos_per_poi=videos_per_poi,
            target_duration_sec=target_duration_sec,
            source_resolution_policy=resolved_source_policy,
        ),
    }


def write_json(path: str, payload: dict[str, Any]) -> None:
    # Atomic so a concurrent in-progress-lock scan never reads a half-written
    # selection_summary.json (which would silently drop this batch's claim).
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )
