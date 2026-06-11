"""End-to-end promo pipeline orchestration.

``full_pipeline`` is the orchestrator — the only module that knows
the ordering of all pipeline steps. It runs the pre-loop orchestration
(clip prep, retrieval/embedding sidecar resolution, voice-rotation +
selector-seam dispatch, Gemini #1 + pause budget, BGM resolution) and
then hands off to :func:`_run_variant_loop` for the per-variant body,
finally emitting the run-level sidecars via :func:`_emit_run_sidecars`.

Extracted from ``promo/cli/compile_promo.py`` lines 1016-1193 in
promo-handoff-readiness Sprint 4 A-001 (narrow — ``compile_promo.py``
decomposition). Behavior byte-identical to the pre-extraction site.
"""

import logging
import os
import shutil
import tempfile
from typing import Any

from promo.core import sanitize_poi_name as _safe_poi_dir
from promo.core.backend import PromoBackend
from promo.core.config import ConfigError, promo_format_selector
from promo.core.errors import NoSuitableBGMError
from promo.core.pipeline.bgm_voice_resolver import (
    _resolve_bgm_paths,
    _resolve_voice_keys,
)
from promo.core.pipeline.sidecar_writer import _emit_run_sidecars_result
from promo.core.pipeline.run_manifest import (
    build_run_manifest,
    emit_run_manifest,
    validate_shared_asset_id_coverage,
)
from promo.core.pipeline.steps import (
    _build_variant_selections,
    _step_generate_script,
    _step_prepare_clips,
    _wpm_search_dirs,
)
from promo.core.pipeline.variant_loop import _run_variant_loop
from promo.core.render.remotion_renderer import REMOTION_DIR
from promo.core.source_resolution_policy import source_resolution_summary

logger = logging.getLogger(__name__)


def _optional_backend_value(backend: PromoBackend, name: str):
    value = getattr(backend, name, None)
    if callable(value):
        return value()
    return None


def _optional_backend_text(backend: PromoBackend, name: str) -> str | None:
    value = _optional_backend_value(backend, name)
    return value if isinstance(value, str) and value else None


def _backend_declares_callable(backend: PromoBackend, name: str) -> bool:
    return callable(getattr(type(backend), name, None))


def _compact_shared_candidate(ranked) -> dict[str, Any]:
    asset = ranked.asset
    return {
        "asset_id": asset.asset_id,
        "clip_id": asset.clip_id,
        "category": asset.category,
        "score": round(float(ranked.score), 6),
        "query_index": ranked.query_index,
        "rank_for_query": ranked.rank_for_query,
        "duration_sec": asset.duration_sec,
        "usage_count": asset.usage_count,
        "scene_description": asset.scene_description,
    }


def _filter_candidate_metadata(
    *,
    clips_metadata: list[dict],
    ready_assets: list[Any],
    asset_ids: list[str],
) -> list[dict]:
    clip_id_by_asset_id = {
        asset.asset_id: asset.clip_id
        for asset in ready_assets
    }
    metadata_by_clip_id = {
        str(item.get("id")).zfill(4): item
        for item in clips_metadata
        if item.get("id") is not None
    }
    filtered: list[dict] = []
    for asset_id in asset_ids:
        clip_id = clip_id_by_asset_id.get(asset_id)
        if clip_id is None:
            continue
        metadata = metadata_by_clip_id.get(str(clip_id).zfill(4))
        if metadata is not None:
            filtered.append(metadata)
    return filtered


def _clip_durations_from_metadata(clips_metadata: list[dict]) -> dict[str, float]:
    durations: dict[str, float] = {}
    for item in clips_metadata:
        clip_id = item.get("id")
        duration = item.get("source_duration_sec")
        if clip_id is None or duration is None:
            continue
        try:
            durations[str(clip_id).zfill(4)] = float(duration)
        except (TypeError, ValueError):
            continue
    return durations


def _retrieve_shared_asset_candidates_from_ready_assets(
    *,
    ready_assets: list[Any],
    scripts: list[dict],
) -> dict[str, Any]:
    """Run asset-library semantic retrieval after Gemini #1 script generation."""
    from promo.core.assets.retrieval import (
        DEFAULT_MAX_CANDIDATES,
        DEFAULT_MIN_DOWNLOAD_CANDIDATES,
        DEFAULT_MIN_ELIGIBLE_ASSETS,
        DEFAULT_TOP_K_PER_QUERY,
        build_script_retrieval_queries,
        candidate_asset_ids_for_download,
        retrieve_candidates,
    )
    from promo.core.assign.clip_embedder import embed_texts

    queries = build_script_retrieval_queries(scripts)
    query_vectors = [tuple(vector) for vector in embed_texts(queries)]
    candidates = retrieve_candidates(
        assets=ready_assets,
        queries=queries,
        query_vectors=query_vectors,
        top_k_per_query=DEFAULT_TOP_K_PER_QUERY,
        max_candidates=DEFAULT_MAX_CANDIDATES,
        min_eligible_assets=DEFAULT_MIN_ELIGIBLE_ASSETS,
    )
    download_asset_ids = candidate_asset_ids_for_download(
        candidates=candidates,
        assets=ready_assets,
        min_candidates=DEFAULT_MIN_DOWNLOAD_CANDIDATES,
        max_candidates=DEFAULT_MAX_CANDIDATES,
    )
    candidate_asset_ids = [ranked.asset.asset_id for ranked in candidates]
    return {
        "retrieval_active": True,
        "retrieval_contract": "shared_asset_semantic_candidates_v1",
        "fallback_reason": None,
        "eligible_asset_pool_size": len(ready_assets),
        "source_resolution_summary": source_resolution_summary([
            {
                "width": getattr(asset, "width", None),
                "height": getattr(asset, "height", None),
            }
            for asset in ready_assets
        ]),
        "query_count": len(queries),
        "top_k_per_query": DEFAULT_TOP_K_PER_QUERY,
        "max_candidates": DEFAULT_MAX_CANDIDATES,
        "min_eligible_assets": DEFAULT_MIN_ELIGIBLE_ASSETS,
        "min_download_candidates": DEFAULT_MIN_DOWNLOAD_CANDIDATES,
        "candidate_count": len(candidates),
        "download_pool_count": len(download_asset_ids),
        "download_pool_padding_count": max(
            0,
            len(download_asset_ids) - len(set(candidate_asset_ids)),
        ),
        "queries": [
            {"query_index": index, "text": text}
            for index, text in enumerate(queries)
        ],
        "candidate_asset_ids": candidate_asset_ids,
        "download_asset_ids": download_asset_ids,
        "candidates": [
            _compact_shared_candidate(ranked)
            for ranked in candidates
        ],
    }


def _retrieve_shared_asset_candidates(
    *,
    backend: PromoBackend,
    scripts: list[dict],
) -> dict[str, Any]:
    """Run asset-library semantic retrieval after Gemini #1 script generation."""
    ready_assets_fn = getattr(backend, "ready_assets_for_retrieval", None)
    if not callable(ready_assets_fn):
        return {
            "retrieval_active": False,
            "retrieval_contract": "shared_asset_semantic_candidates_v1",
            "fallback_reason": "backend_missing_ready_asset_reader",
            "eligible_asset_pool_size": 0,
            "query_count": 0,
            "candidate_count": 0,
            "candidate_asset_ids": [],
            "download_asset_ids": [],
            "candidates": [],
        }

    return _retrieve_shared_asset_candidates_from_ready_assets(
        ready_assets=ready_assets_fn(),
        scripts=scripts,
    )


def _merge_shared_asset_retrieval_provenance(
    run_retrieval_provenance: dict[str, Any],
    shared_asset_retrieval_provenance: dict[str, Any],
) -> dict[str, Any]:
    return {
        **run_retrieval_provenance,
        "retrieval_active": shared_asset_retrieval_provenance["retrieval_active"],
        "embedded_pool_size": shared_asset_retrieval_provenance.get(
            "eligible_asset_pool_size", 0
        ),
        "reduced_pool_size": shared_asset_retrieval_provenance.get(
            "download_pool_count",
            shared_asset_retrieval_provenance.get("candidate_count", 0),
        ),
        "fallback_reason": shared_asset_retrieval_provenance.get("fallback_reason"),
        "retrieval_contract": shared_asset_retrieval_provenance.get(
            "retrieval_contract"
        ),
        "shared_asset_retrieval": shared_asset_retrieval_provenance,
    }


def full_pipeline(
    poi_name: str,
    location: str = "",
    output_path: str | None = None,
    voice_key: str | None = None,
    bgm_path: str | None = None,
    bgm_paths: list[str] | None = None,
    skip_analysis: bool = False,
    backend: PromoBackend | None = None,
    target_duration_sec: float = 30.0,
    n_variants: int = 1,
    script_candidates: int = 1,
    tts_speed: float = 0.95,
    hotel_description: str = "",
    notable_details: str = "",
    seed: int | None = None,
    replay_script: dict | None = None,
) -> bool:
    """Run the full promo pipeline end-to-end.

    Args:
        poi_name: Hotel/POI name.
        location: Location string for script generation.
        output_path: Where to write the rendered MP4.
        voice_key: VOICE_CATALOG key (``kore`` Gemini-default or ``jarnathan``/
            ``hope``/``heather`` ElevenLabs). When set, ALL variants use this
            voice; when ``None`` (default), rotate round-robin through the catalog.
        bgm_path: Explicit single BGM path (legacy, used if bgm_paths not set).
        bgm_paths: List of BGM paths for per-variant rotation (round-robin).
        skip_analysis: Skip MiMo clip analysis.
        backend: I/O backend. In the standalone repo this should usually be a LocalBackend instance.
        target_duration_sec: Runtime target duration for the promo.
        n_variants: Number of promo variants to render from one clip pool.
        script_candidates: Number of candidates per accepted script variant.

    Requires env vars for the promo's own AI services:
        OPENROUTER_API_KEY, GEMINI_API_KEY, ELEVENLABS_API_KEY.
    Backend-specific env vars depend on the chosen backend. In this repo,
    the default path is LocalBackend with --local-clips.
    """
    if backend is None:
        raise ValueError("A backend instance is required in the standalone promo repo")

    if output_path is None:
        safe_name = _safe_poi_dir(poi_name)
        output_path = os.path.join(REMOTION_DIR, "out", f"promo_{safe_name}.mp4")

    tmp_dir = tempfile.mkdtemp(prefix=f"promo_{_safe_poi_dir(poi_name)}_")
    logger.info("Temp dir: %s", tmp_dir)

    try:
        asset_visual_brief: dict | None = None
        shared_assets_for_manifest: list[dict[str, Any]] | None = None
        shared_asset_ready_pool: list[Any] | None = None
        candidate_only_mode = (
            _backend_declares_callable(backend, "ready_assets_for_retrieval")
            and _backend_declares_callable(backend, "fetch_candidate_clips")
        )
        if candidate_only_mode:
            try:
                from promo.core.assets.retrieval import (
                    build_asset_visual_brief,
                    clip_metadata_from_ready_assets,
                )

                shared_asset_ready_pool = backend.ready_assets_for_retrieval()  # type: ignore[attr-defined]
                clips_metadata = clip_metadata_from_ready_assets(shared_asset_ready_pool)
                clip_durations = _clip_durations_from_metadata(clips_metadata)
                clip_paths: dict[str, str] = {}
                asset_visual_brief = build_asset_visual_brief(shared_asset_ready_pool)
                logger.info(
                    "Candidate-only shared asset mode: %d ready assets, %.1fs total",
                    asset_visual_brief["eligible_asset_count"],
                    asset_visual_brief["eligible_total_seconds"],
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Candidate-only shared asset setup failed: %s", exc)
                return False
        else:
            # Steps 1-2.5 (Sprint 10 C4): fetch + analyze + ffprobe extracted.
            prep = _step_prepare_clips(
                backend=backend,
                poi_name=poi_name,
                tmp_dir=tmp_dir,
                target_duration_sec=target_duration_sec,
                skip_analysis=skip_analysis,
            )
            if prep is None:
                return False
            clip_paths, clips_metadata, clip_durations = prep
            shared_assets = _optional_backend_value(backend, "shared_assets")
            shared_assets_for_manifest = (
                shared_assets if isinstance(shared_assets, list) else None
            )
            try:
                validate_shared_asset_id_coverage(
                    clip_paths=clip_paths,
                    shared_assets=shared_assets_for_manifest,
                )
            except ValueError as exc:
                logger.error("Shared asset preflight failed: %s", exc)
                return False
            if shared_assets_for_manifest:
                try:
                    from promo.core.assets.retrieval import (
                        brief_assets_from_rows,
                        build_asset_visual_brief,
                    )

                    brief_assets = brief_assets_from_rows(shared_assets_for_manifest)
                    if brief_assets:
                        asset_visual_brief = build_asset_visual_brief(brief_assets)
                        logger.info(
                            "Asset Visual Brief enabled for Gemini #1: %d assets, %.1fs total",
                            asset_visual_brief["eligible_asset_count"],
                            asset_visual_brief["eligible_total_seconds"],
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Asset Visual Brief unavailable; falling back to clip inventory: %s",
                        exc,
                    )

        # Sprint 12b — resolve the embedding sidecar directory for retrieval.
        # Same derivation pattern ``_step_prepare_clips`` uses for
        # ``mimo_cache_dir`` (sibling ``.embedding_cache/`` of ``.mimo_cache/``).
        # ``None`` when the backend has no clips_dir() or the directory
        # does not exist → ``_step_assign_clips`` runs Sprint 11 no-op path.
        _source_clips_dir = backend.clips_dir()
        embedding_cache_dir: str | None
        if _source_clips_dir:
            embedding_cache_dir = os.path.normpath(
                os.path.join(_source_clips_dir, "..", ".embedding_cache")
            )
            if not os.path.isdir(embedding_cache_dir):
                logger.info(
                    "Sprint 12b retrieval disabled: embedding_cache_dir %s "
                    "does not exist — using full-pool Gemini #2.",
                    embedding_cache_dir,
                )
                embedding_cache_dir = None
        else:
            logger.info(
                "Sprint 12b retrieval disabled: backend.clips_dir() returned "
                "None — using full-pool Gemini #2.",
            )
            embedding_cache_dir = None

        # Pre-resolve voice rotation so Step 3 can resolve per-variant
        # WPM against each variant's own voice's backend (S0.5 fix).
        # The full rotation list is also reused for Step 6's variant loop.
        try:
            resolved_voice_keys = _resolve_voice_keys(voice_key)
        except ValueError as exc:
            logger.error(str(exc))
            return False

        # Sprint 16 — selector seams pick per-variant format + persona.
        # Default `single` pins to target_duration_sec; `random` opt-in
        # via PROMO_FORMAT_SELECTOR env var ignores the scalar.
        try:
            variant_profiles, variant_personas = _build_variant_selections(
                n_variants=n_variants, poi_name=poi_name,
                clips_metadata=clips_metadata, seed=seed,
                target_duration_sec=target_duration_sec,
            )
        except ConfigError as exc:
            logger.error("Selector configuration invalid: %s", exc)
            return False

        # Step 3 (Sprint 10 C4): Gemini #1 + WPM calibration + pause budget,
        # extracted into _step_generate_script.
        wpm_search_dirs = _wpm_search_dirs(backend, output_path)
        try:
            scripts = _step_generate_script(
                poi_name=poi_name,
                location=location,
                clips_metadata=clips_metadata,
                n_variants=n_variants,
                script_candidates=script_candidates,
                target_duration_sec=target_duration_sec,
                hotel_description=hotel_description,
                notable_details=notable_details,
                wpm_search_dirs=wpm_search_dirs,
                resolved_voice_keys=resolved_voice_keys,
                variant_profiles=variant_profiles,
                variant_personas=variant_personas,
                asset_visual_brief=asset_visual_brief,
                replay_script=replay_script,
            )
        except RuntimeError as exc:
            logger.error("Variant-pack generation failed: %s", exc)
            return False

        shared_asset_retrieval_provenance: dict[str, Any] | None = None
        if candidate_only_mode and shared_asset_ready_pool is not None:
            try:
                shared_asset_retrieval_provenance = (
                    _retrieve_shared_asset_candidates_from_ready_assets(
                        ready_assets=shared_asset_ready_pool,
                        scripts=scripts,
                    )
                )
                download_asset_ids = shared_asset_retrieval_provenance[
                    "download_asset_ids"
                ]
                clip_paths = backend.fetch_candidate_clips(  # type: ignore[attr-defined]
                    poi_name,
                    tmp_dir,
                    download_asset_ids,
                )
                shared_assets = _optional_backend_value(backend, "shared_assets")
                shared_assets_for_manifest = (
                    shared_assets if isinstance(shared_assets, list) else None
                )
                clips_metadata = _filter_candidate_metadata(
                    clips_metadata=clips_metadata,
                    ready_assets=shared_asset_ready_pool,
                    asset_ids=download_asset_ids,
                )
                clip_durations = _clip_durations_from_metadata(clips_metadata)
                validate_shared_asset_id_coverage(
                    clip_paths=clip_paths,
                    shared_assets=shared_assets_for_manifest,
                )
                from promo.core.assets.retrieval import (
                    brief_assets_from_rows,
                    build_asset_visual_brief,
                )

                asset_visual_brief = build_asset_visual_brief(
                    brief_assets_from_rows(shared_assets_for_manifest or [])
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Candidate-only shared asset retrieval/download failed: %s", exc)
                return False
            logger.info(
                "Candidate-only download selected %d semantic candidates and "
                "downloaded %d clips from %d ready assets",
                shared_asset_retrieval_provenance["candidate_count"],
                len(clip_paths),
                shared_asset_retrieval_provenance["eligible_asset_pool_size"],
            )
        elif shared_assets_for_manifest:
            try:
                shared_asset_retrieval_provenance = _retrieve_shared_asset_candidates(
                    backend=backend,
                    scripts=scripts,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Shared asset semantic retrieval failed: %s", exc)
                return False
            if shared_asset_retrieval_provenance["retrieval_active"]:
                logger.info(
                    "Shared asset semantic retrieval selected %d candidates "
                    "from %d ready assets over %d script queries",
                    shared_asset_retrieval_provenance["candidate_count"],
                    shared_asset_retrieval_provenance["eligible_asset_pool_size"],
                    shared_asset_retrieval_provenance["query_count"],
                )
            else:
                logger.info(
                    "Shared asset semantic retrieval inactive: %s",
                    shared_asset_retrieval_provenance["fallback_reason"],
                )

        # Step 6: Select BGM(s). Voice rotation pre-resolved above
        # (Sprint TTS-Migration Phase 4 moved it earlier so Step 3
        # could pick the per-backend WPM bootstrap).
        logger.info("=" * 60)
        logger.info("Step 6: Selecting BGM...")
        try:
            resolved_bgm_paths = _resolve_bgm_paths(
                bgm_paths=bgm_paths,
                bgm_path=bgm_path,
                poi_name=poi_name,
                backend=backend,
                tmp_dir=tmp_dir,
                target_duration_sec=target_duration_sec,
                count=n_variants,
            )
        except NoSuitableBGMError as exc:
            logger.error("No BGM available: %s", exc)
            return False

        for i, bp in enumerate(resolved_bgm_paths):
            logger.info("  BGM %d: %s", i + 1, os.path.basename(bp))

        # Sprint 09b C7 pool-exhaustion metric + Sprint 09a M-004 / Sprint
        # 10 C3 (F1) success-gated accumulators. Constructed before the
        # loop; the variant loop mutates them in place so only rendered-
        # variant rows land here.
        tts_metrics: list[dict] = []
        match_quality_entries: list[dict] = []
        clip_assignments_entries: list[dict] = []  # Sprint 10 C3 — F1 sidecar accumulator
        rendered_outputs: list[dict] = []
        # Sprint 13 AC19: last-variant-wins run-level provenance is owned by
        # ``_run_variant_loop`` and surfaced through its return tuple.
        # Sprint 4 post-audit L-001/D-001 removed the caller-side init + kwarg
        # plumbing after audit found the passed-in object was never read back
        # (only the return value) — a dead-weight parameter that looked like
        # an accumulator but was not.

        all_ok, pool_exhaustion_hard_fails, run_retrieval_provenance = _run_variant_loop(
            scripts=scripts,
            clip_paths=clip_paths,
            clips_metadata=clips_metadata,
            clip_durations=clip_durations,
            resolved_voice_keys=resolved_voice_keys,
            resolved_bgm_paths=resolved_bgm_paths,
            variant_profiles=variant_profiles,
            variant_personas=variant_personas,
            output_path=output_path,
            backend=backend,
            poi_name=poi_name,
            location=location,
            hotel_description=hotel_description,
            notable_details=notable_details,
            tmp_dir=tmp_dir,
            tts_speed=tts_speed,
            target_duration_sec=target_duration_sec,
            script_candidates=script_candidates,
            embedding_cache_dir=embedding_cache_dir,
            asset_visual_brief=asset_visual_brief,
            tts_metrics=tts_metrics,
            match_quality_entries=match_quality_entries,
            clip_assignments_entries=clip_assignments_entries,
            rendered_outputs=rendered_outputs,
        )
        if shared_asset_retrieval_provenance is not None:
            run_retrieval_provenance = _merge_shared_asset_retrieval_provenance(
                run_retrieval_provenance,
                shared_asset_retrieval_provenance,
            )

        sidecar_result = _emit_run_sidecars_result(
            backend=backend,
            output_path=output_path,
            poi_name=poi_name,
            target_duration_sec=target_duration_sec,
            tts_metrics=tts_metrics,
            match_quality_entries=match_quality_entries,
            clip_assignments_entries=clip_assignments_entries,
            run_retrieval_provenance=run_retrieval_provenance,
        )
        if not sidecar_result.ok:
            all_ok = False
        elif rendered_outputs:
            try:
                manifest = build_run_manifest(
                    poi_name=poi_name,
                    location=location,
                    target_duration_sec=target_duration_sec,
                    n_variants=n_variants,
                    script_candidates=script_candidates,
                    format_selector=promo_format_selector(),
                    embedding_cache_active=embedding_cache_dir is not None,
                    clip_paths=clip_paths,
                    clips_metadata=clips_metadata,
                    clip_durations=clip_durations,
                    rendered_outputs=rendered_outputs,
                    sidecar_paths=sidecar_result.paths,
                    poi_id=_optional_backend_text(backend, "shared_poi_id"),
                    canonical_key=_optional_backend_text(backend, "shared_canonical_key"),
                    shared_assets=shared_assets_for_manifest,
                    skip_analysis=skip_analysis,
                    tts_speed=tts_speed,
                    seed=seed,
                )
            except ValueError as exc:
                logger.error("Run manifest validation failed: %s", exc)
                all_ok = False
            else:
                manifest_result = emit_run_manifest(
                    sidecar_dir=sidecar_result.sidecar_dir,
                    poi_name=poi_name,
                    target_duration_sec=target_duration_sec,
                    manifest=manifest,
                )
                if not manifest_result.ok:
                    all_ok = False

        # Sprint 09b C7: pool-exhaustion metric. Emit once per run as a
        # grepable log line. Clean runs report 0; a non-zero value means
        # at least one variant aborted because FreezeWouldOccurError
        # fired in remotion_renderer._bind_clips_to_narration.
        logger.info(
            "Pool-exhaustion hard-fails this run: %d", pool_exhaustion_hard_fails,
        )

        return all_ok

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
