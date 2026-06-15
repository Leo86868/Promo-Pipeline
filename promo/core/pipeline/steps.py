"""Pipeline step helpers for ``full_pipeline``.

Each helper corresponds to a discrete pipeline stage: Steps 1/2/2.5
(clip prep), Step 3 (Gemini #1 + pause budget), Step 4 (TTS
narration), Step 4.5 (deterministic clip assignment), plus the
Sprint 16 ``_build_variant_selections`` seam for
per-variant format/persona selection and the per-variant
``_build_variant_tts_metrics`` row builder.

Extracted from ``promo/cli/compile_promo.py`` (lines 86 + 352-965)
in promo-handoff-readiness Sprint 4 A-001 (narrow — ``compile_promo.py``
decomposition). Behavior byte-identical to the pre-extraction site.
"""

import logging
import os
import re
from typing import TYPE_CHECKING, Any

from promo.core import sanitize_poi_name as _safe_poi_dir
from promo.core.format_profiles import (
    PromoFormatProfile,
    get_clip_pool_messages,
    get_promo_format_profile,
)
from promo.core.script.pause_budget import (
    bootstrap_wpm_for_backend,
    compute_pause_budget,
    load_calibrated_wpm,
    measure_wpm,
)
from promo.core.pipeline.bgm_voice_resolver import _empty_retrieval_provenance

if TYPE_CHECKING:
    from promo.core.backend import PromoBackend
    from promo.core.script.script_generator import NarratorPersona


logger = logging.getLogger(__name__)


TTS_ASSEMBLY_DRIFT_MAX_ATTEMPTS = 2
TTS_ASSEMBLY_DRIFT_RETRY_MAX_SEC = 1.0
_TTS_ASSEMBLY_DRIFT_MARKER = "Narration assembly drift too large"
_TTS_ASSEMBLY_DRIFT_RE = re.compile(r"drift=([0-9]+(?:\.[0-9]+)?)s")


# ---------------------------------------------------------------------------
#  Step 2: Analyze clips with MiMo
# ---------------------------------------------------------------------------

def analyze_clips_for_script(
    clip_paths: dict[str, str],
    cache_dir: str | None = None,
) -> list[dict]:
    """Run MiMo V2 Omni analysis on clips for script generation."""
    from promo.core.analyze.clip_analyzer import analyze_clips
    return analyze_clips(clip_paths, cache_dir=cache_dir)


# Sprint 10 C4 — _assign_tail_clip retired. Pre-10 one-pass Gemini needed a
# per-variant tail-source estimate to drive compute_pause_budget's tail cap,
# but the deterministic assigner replaces that with its per-phrase
# hard constraint (source_dur − trim_start ≥ display_span), which makes the
# tail reserve redundant.


# ---------------------------------------------------------------------------
#  Sprint 10 C4 — full_pipeline step helpers
# ---------------------------------------------------------------------------
#
# Extracted from full_pipeline under the F4 decomposition goal: the
# pipeline naturally splits Step 3 (Gemini #1 script) from Step 4 (TTS)
# from the new Step 4.5 (deterministic clip assignment). Each helper
# is callable standalone — closures over full_pipeline locals are replaced
# with explicit kwargs so tests can drive them directly.


def load_replay_script(path: str, *, n_variants: int = 1) -> dict:
    """翻转二 B6 — load a recorded script for ``compile_promo --replay-script``.

    Accepts either a ``clip_assignments_*.json`` sidecar (uses
    ``variants[0]["script"]`` — the FINAL accepted script recorded at the
    success-gated commit point in ``variant_loop``) or a bare
    ``{"segments": [...]}`` script JSON. Raises ``ValueError`` with an
    operator-readable message on any other shape, and when
    ``n_variants != 1`` (a replay is one fixed script, not a variant pack).
    """
    import json

    if n_variants != 1:
        raise ValueError(f"--replay-script requires --n-variants 1 (got {n_variants})")
    payload = json.loads(open(path, encoding="utf-8").read())
    if isinstance(payload, dict) and isinstance(payload.get("variants"), list):
        variants = payload["variants"]
        if not variants or not isinstance(variants[0], dict):
            raise ValueError(f"{path}: clip_assignments file has no variants")
        script = variants[0].get("script")
        if not isinstance(script, dict):
            raise ValueError(
                f"{path}: variant row carries no 'script' field — the run "
                "predates script recording (2026-06-11); re-render the "
                "source video on current code first"
            )
        return {**script, "_source_path": path}
    if isinstance(payload, dict) and isinstance(payload.get("segments"), list):
        return {**payload, "_source_path": path}
    raise ValueError(
        f"{path}: expected a clip_assignments sidecar or a "
        "{'segments': [...]} script JSON"
    )


def _wpm_search_dirs(backend: "PromoBackend", output_path: str) -> list[str]:
    """Sidecar dirs to probe for calibrated-WPM files: the run's output
    dir and its parent (extracted from ``full_pipeline`` to keep its
    body under the Sprint 10 C4 400-line ceiling)."""
    dirs: list[str] = []
    try_dir = backend.output_dir() or os.path.dirname(output_path)
    if isinstance(try_dir, str) and try_dir:
        dirs.append(try_dir)
        parent = os.path.dirname(try_dir)
        if parent and parent != try_dir:
            dirs.append(parent)
    return dirs


def _step_generate_script(
    *,
    poi_name: str,
    location: str,
    clips_metadata: list[dict],
    n_variants: int,
    script_candidates: int,
    target_duration_sec: float,
    hotel_description: str,
    notable_details: str,
    wpm_search_dirs: list[str],
    resolved_voice_keys: list[str],
    variant_profiles: list[PromoFormatProfile] | None = None,
    variant_personas: "list[NarratorPersona] | None" = None,
    asset_visual_brief: dict | None = None,
    replay_script: dict | None = None,
    hook_seed: int | None = None,
) -> list[dict]:
    """Run Gemini #1 + resolve per-variant effective_wpm + apply
    compute_pause_budget on each accepted script. Returns ``scripts``
    with ``script["effective_wpm"]`` populated per variant.

    翻转二 B6 — ``replay_script`` skips Gemini #1 entirely and feeds a
    previously recorded script (the ``script`` field of a
    ``clip_assignments_*.json`` variant row) through the SAME wpm
    calibration + pause-budget loop below. Narration text is held
    verbatim; pause_after_ms is recomputed. Requires ``n_variants == 1``
    (a replay is one fixed script, not a variant pack).

    Sprint 10 C4: extraction of Step 3 from ``full_pipeline``. Scripts
    come back with ``pause_after_ms`` populated per segment but no clip
    assignments (Gemini #1 under the two-pass schema does not emit
    ``clips[]`` — assignments are the assign stage's job in ``_step_assign_clips``).

    Sprint 16 — when ``variant_profiles`` / ``variant_personas`` are
    threaded from the selector seams, each variant uses its own
    profile and persona; the pause budget is then computed against
    each variant's own ``target_duration_sec``.

    Each variant's WPM is resolved against its own voice's backend
    (round-robin mirrors ``variant_loop``'s ``(i-1) % len(keys)`` rule)
    so a mixed-backend rotation does not feed a single run-level WPM
    into pause_budget — the prior pre-S0.5 single-WPM path tripped
    "narration already fills target" for ElevenLabs variants when slot 0
    was Gemini.

    Raises ``RuntimeError`` on variant-pack under-delivery or if
    ``generate_script_variants`` itself gives up.
    """
    from promo.core import config as promo_config
    from promo.core.narrate.tts_engine import VOICE_CATALOG
    from promo.core.script.script_generator import generate_script_variants

    if replay_script is None:
        env_path = promo_config.replay_script_path()
        if env_path:
            replay_script = load_replay_script(env_path, n_variants=n_variants)
    if replay_script is not None:
        if n_variants != 1:
            raise ValueError(
                f"--replay-script requires n_variants=1 (got {n_variants})"
            )
        segments = replay_script.get("segments")
        if not isinstance(segments, list) or not segments:
            raise ValueError("replay script has no segments")
        replayed_segments = [dict(seg) for seg in segments]
        # Recount word_count from the TEXT (2026-06-11 review bug ①): the
        # pause budget estimates spoken duration from this label alone —
        # a missing/stale label reads as a zero-word script and maxes
        # every gap's silence, silently changing the B arm's pacing for
        # reasons unrelated to clip selection (A/B invalidated).
        for seg in replayed_segments:
            seg["word_count"] = len(str(seg.get("text") or "").split())
        script: dict = {
            "variant_index": 1,
            "segments": replayed_segments,
            "total_words": sum(seg["word_count"] for seg in replayed_segments),
            "format_mode": replay_script.get("format_mode"),
            "hook_technique": replay_script.get("hook_technique"),
            "assigned_hook_technique": replay_script.get("assigned_hook"),
            "target_duration_sec": target_duration_sec,
            # Review bug ② companion: every downstream record of a replayed
            # render declares itself (clip_assignments row picks this up).
            "replay_source": replay_script.get("_source_path") or "replay",
        }
        logger.info(
            "Step 3: REPLAY script (%d segments, %d words) — Gemini #1 skipped",
            len(script["segments"]), script["total_words"],
        )
        scripts = [script]
        # Fall through to the shared wpm + pause-budget loop below.
    else:
        scripts = None  # populated by generation just after the log lines

    logger.info("=" * 60)
    if variant_profiles is not None:
        mode_mix = ",".join(p.mode for p in variant_profiles)
        logger.info(
            "Step 3: Generating narration script(s) (Gemini Flash, per-variant modes=[%s], %d variant%s)...",
            mode_mix, n_variants, "" if n_variants == 1 else "s",
        )
    else:
        logger.info(
            "Step 3: Generating narration script(s) (Gemini Flash, %.0fs target, %d variant%s)...",
            target_duration_sec, n_variants, "" if n_variants == 1 else "s",
        )
    # TypedDict boundary: ``clips_metadata`` is the loosely-typed ``list[dict]``
    # the pipeline carries end-to-end (Sprint 10 C4 extraction predates the
    # Sprint 14 TypedDict landing); ``generate_script_variants`` narrows to
    # ``list[ClipMetadata]`` internally. Runtime is identical (TypedDicts ARE
    # dicts); the ignore preserves the ``list[dict]`` pipeline contract until
    # a future sprint retypes the seam.
    if scripts is None:
        scripts = generate_script_variants(
            poi_name=poi_name,
            location=location,
            clips_metadata=clips_metadata,  # type: ignore[arg-type]
            hotel_description=hotel_description,
            notable_details=notable_details,
            n_variants=n_variants,
            n_candidates=script_candidates,
            target_duration_sec=target_duration_sec,
            profiles=variant_profiles,
            personas=variant_personas,
            asset_visual_brief=asset_visual_brief,
            hook_seed=hook_seed,
        )
    if len(scripts) != n_variants:
        raise RuntimeError(
            f"Variant-pack delivery failed: requested {n_variants} variants "
            f"but received {len(scripts)}"
        )

    sidecar_slug = _safe_poi_dir(poi_name)

    for script in scripts:
        variant_index = script["variant_index"]
        variant_voice_key = resolved_voice_keys[(variant_index - 1) % len(resolved_voice_keys)]
        variant_backend = VOICE_CATALOG[variant_voice_key]["backend"]
        bootstrap_wpm = bootstrap_wpm_for_backend(variant_backend)
        calibrated_wpm = load_calibrated_wpm(
            sidecar_slug, target_duration_sec, wpm_search_dirs,
            backend=variant_backend,
        )
        if calibrated_wpm is not None:
            effective_wpm = calibrated_wpm
            logger.info(
                "WPM calibration v%d (%s/%s): %d WPM (bootstrap=%d)",
                variant_index, variant_voice_key, variant_backend,
                effective_wpm, bootstrap_wpm,
            )
        else:
            effective_wpm = bootstrap_wpm
            logger.info(
                "WPM cold-start v%d (%s/%s): bootstrap %d WPM",
                variant_index, variant_voice_key, variant_backend, effective_wpm,
            )
        script["effective_wpm"] = effective_wpm
        # Sprint 16 — pause budget honours each variant's own duration.
        # P2 step 3: the pause cap comes from the variant's format card.
        script_target = float(script.get("target_duration_sec", target_duration_sec))
        compute_pause_budget(
            script["segments"],  # type: ignore[arg-type]
            target_sec=script_target,
            wpm=effective_wpm,
            pause_cap_ms=get_promo_format_profile(script_target).pause_cap_ms,
        )
        logger.info(
            "Variant %d: %d segments, %d words (%s/%ss)",
            variant_index, len(script["segments"]),
            script["total_words"], script.get("format_mode", "?"),
            script.get("target_duration_sec", "?"),
        )
        for seg in script["segments"]:
            logger.info("  Seg %d: \"%s\"", seg["segment"], seg["text"][:80])  # type: ignore[typeddict-item]

    return scripts


def _step_tts_narration(
    script: dict,
    voice_key: str,
    tmp_dir: str,
    speed: float,
) -> dict:
    """Run TTS narration for a single variant's script (backend chosen
    from ``VOICE_CATALOG[voice_key]['backend']`` inside ``generate_narration``).

    Sprint 10 C4: extraction of Step 4 from ``full_pipeline``. Thin wrapper
    around :func:`tts_engine.generate_narration` so the variant loop reads
    as a single call at full_pipeline level.
    """
    from promo.core.narrate.tts_engine import generate_narration

    variant_index = script.get("variant_index", "?")
    logger.info("Step 4: Generating TTS narration (voice=%s)...", voice_key)
    logger.info(
        "Pre-TTS pause_after_ms per segment (variant %s): %s",
        variant_index,
        [s.get("pause_after_ms") for s in script["segments"]],
    )
    for attempt in range(1, TTS_ASSEMBLY_DRIFT_MAX_ATTEMPTS + 1):
        try:
            return generate_narration(  # type: ignore[return-value]
                segments=script["segments"],
                voice_key=voice_key,
                output_dir=tmp_dir,
                speed=speed,
            )
        except RuntimeError as exc:
            drift_sec = _retryable_tts_assembly_drift_sec(exc)
            if drift_sec is None or attempt >= TTS_ASSEMBLY_DRIFT_MAX_ATTEMPTS:
                raise
            logger.warning(
                "TTS assembly drift %.3fs on attempt %d/%d; retrying narration once",
                drift_sec,
                attempt,
                TTS_ASSEMBLY_DRIFT_MAX_ATTEMPTS,
            )
    raise RuntimeError("TTS narration retry loop exhausted unexpectedly")


def _retryable_tts_assembly_drift_sec(exc: RuntimeError) -> float | None:
    message = str(exc)
    if _TTS_ASSEMBLY_DRIFT_MARKER not in message:
        return None
    match = _TTS_ASSEMBLY_DRIFT_RE.search(message)
    if not match:
        return None
    drift_sec = float(match.group(1))
    if drift_sec > TTS_ASSEMBLY_DRIFT_RETRY_MAX_SEC:
        return None
    return drift_sec


def _step_prepare_clips(
    *,
    backend: "PromoBackend",
    poi_name: str,
    tmp_dir: str,
    target_duration_sec: float,
    skip_analysis: bool,
) -> tuple[dict[str, str], list[dict], dict[str, float]] | None:
    """Sprint 10 C4 — Steps 1, 2, and 2.5 extracted.

    1. Fetch clip paths via backend.
    2. Run MiMo clip analysis (or stub if ``skip_analysis``).
    3. ffprobe each clip's source duration and attach to its metadata.

    Returns ``(clip_paths, clips_metadata, clip_durations)`` on success,
    or ``None`` when clip pool fails preflight (full_pipeline surfaces
    that as a False return).
    """
    logger.info("=" * 60)
    logger.info("Step 1: Fetching clips...")
    clip_paths = backend.fetch_clips(poi_name, tmp_dir)
    if not clip_paths:
        logger.error("No clips found for '%s'", poi_name)
        return None

    # Preflight against the profile derived from the operator's
    # `--target-duration-sec`. Sprint 16 audit-fix (post-Codex round 4):
    # an earlier draft of this preflight checked against the worst-case
    # profile (long, min 14 clips) regardless of the operator's request
    # — that broke `--target-duration-sec 30` against pools sized for
    # short (e.g. hotel-y at 11 clips, short needs 8 but long needs 14).
    # The strict per-distinct-profile gate that handles the random-mode
    # worst case still fires inside ``generate_script_variants``; this
    # stage is just early-exit safety against MiMo cycles, so matching
    # it to the operator's actual request is the right scope.
    preflight_profile = get_promo_format_profile(target_duration_sec)
    clip_pool_errors, clip_pool_warnings = get_clip_pool_messages(
        len(clip_paths), preflight_profile
    )
    for warning in clip_pool_warnings:
        logger.warning("Clip-pool preflight: %s", warning)
    if clip_pool_errors:
        logger.error("Clip-pool preflight failed: %s", clip_pool_errors[0])
        return None

    clips_metadata: list[dict]
    if skip_analysis:
        logger.info("Step 2: Skipping MiMo analysis (--skip-analysis)")
        clips_metadata = [
            {"id": cid, "scene_description": "", "category": "unknown"}
            for cid in sorted(clip_paths.keys())
        ]
    else:
        source_clips_dir = backend.clips_dir()
        mimo_cache_dir: str | None = (
            os.path.join(source_clips_dir, "..", ".mimo_cache")
            if source_clips_dir else None
        )
        if mimo_cache_dir:
            mimo_cache_dir = os.path.normpath(mimo_cache_dir)
        logger.info("=" * 60)
        logger.info(
            "Step 2: Analyzing clips with MiMo V2 Omni (%d clips, cache=%s)...",
            len(clip_paths), mimo_cache_dir or "disabled",
        )
        clips_metadata = analyze_clips_for_script(clip_paths, cache_dir=mimo_cache_dir)

    # Step 2.5 (Sprint 09a H-001): probe each clip's source duration and
    # attach to metadata BEFORE script generation so Gemini #1's inventory
    # carries per-clip visual capacity (still load-bearing for the
    # GROUNDING reference even under Sprint 10's two-pass schema).
    logger.info("=" * 60)
    logger.info("Step 2.5: Probing clip source durations via ffprobe...")
    from promo.core.render.remotion_renderer import get_clip_duration
    clip_durations: dict[str, float] = {}
    for cid, cpath in clip_paths.items():
        try:
            clip_durations[cid] = float(get_clip_duration(cpath))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ffprobe failed for clip %s (%s): %s", cid, cpath, exc,
            )
    for cm in clips_metadata:
        cm_id = cm.get("id")
        if cm_id and cm_id in clip_durations:
            cm["source_duration_sec"] = clip_durations[cm_id]
    logger.info(
        "Step 2.5: probed %d/%d clip durations",
        len(clip_durations), len(clip_paths),
    )
    return clip_paths, clips_metadata, clip_durations


def _build_variant_tts_metrics(
    narration: dict,
    variant_index: int,
    variant_voice_key: str,
    target_duration_sec: float,
) -> dict:
    """Build the per-variant ``tts_metrics.json`` entry and emit the
    associated measured-WPM / coverage log lines.

    Sprint 10 C4: pulled out of ``full_pipeline`` so the variant loop
    reads linearly. Caller still success-gates the accumulator append
    (per 09a M-004) by waiting until ``render_promo`` returns True.
    """
    word_timestamps = narration["word_timestamps"]
    first_word_start = float(word_timestamps[0]["start"]) if word_timestamps else 0.0
    last_word_end = float(word_timestamps[-1]["end"]) if word_timestamps else 0.0
    measured_wpm = (
        narration.get("measured_wpm_spoken")
        or measure_wpm(word_timestamps)
    )
    narration_coverage = (
        narration["duration"] / target_duration_sec if target_duration_sec > 0 else 1.0
    )
    logger.info(
        "Post-TTS tagged_text (variant %d):\n%s",
        variant_index, narration.get("tagged_text") or "(empty)",
    )
    logger.info(
        "Post-TTS segment_timestamps (variant %d): %s",
        variant_index, narration.get("segment_timestamps") or [],
    )
    # Sprint TTS-Migration Phase 4: per-backend bootstrap for the delta log.
    # ``narration["backend"]`` is set by tts_engine.generate_narration for
    # both backends (unified tuple shape).
    variant_backend = narration.get("backend", "elevenlabs")
    variant_bootstrap = bootstrap_wpm_for_backend(variant_backend)
    if measured_wpm is not None:
        wpm_delta_pct = (measured_wpm - variant_bootstrap) / variant_bootstrap * 100
        logger.info(
            "Measured WPM v%d (pure spoken, backend=%s): %.1f (bootstrap=%d, delta=%+.1f%%)",
            variant_index, variant_backend, measured_wpm, variant_bootstrap, wpm_delta_pct,
        )
    logger.info(
        "Narration v%d: %.1fs, %d word timestamps, coverage %.0f%%",
        variant_index, narration["duration"],
        len(word_timestamps), narration_coverage * 100,
    )
    if narration_coverage < 0.85:
        logger.warning(
            "Narration coverage %.0f%% is below 85%% target "
            "(narration=%.1fs, target=%.1fs). Prosody pauses may be "
            "shorter than expected.",
            narration_coverage * 100, narration["duration"], target_duration_sec,
        )
    return {
        "variant_index": variant_index,
        "voice_key": variant_voice_key,
        "backend": variant_backend,
        "word_count": len(word_timestamps),
        "duration_sec": narration["duration"],
        "pure_spoken_sec": narration.get("pure_spoken_sec"),
        "inter_segment_silence_sec": narration.get("inter_segment_silence_sec"),
        "first_word_start": round(first_word_start, 3),
        "last_word_end": round(last_word_end, 3),
        "measured_wpm": round(measured_wpm, 2) if measured_wpm is not None else None,
        "bootstrap_wpm": variant_bootstrap,
        "target_duration_sec": target_duration_sec,
        "narration_coverage": round(narration_coverage, 3),
    }


def _step_assign_clips(
    script: dict,
    narration: dict,
    clips_metadata: list[dict],
    clip_durations: dict[str, float],
    variant_index: int,
    *,
    poi_name: str,
    location: str,
    hotel_description: str,
    notable_details: str,
    variant_voice_key: str,
    variant_tmp_dir: str,
    tts_speed: float,
    target_duration_sec: float,
    effective_wpm: int,
    n_variants_total: int,
    script_candidates: int,
    embedding_cache_dir: str | None = None,
    variant_profile: "PromoFormatProfile | None" = None,
    variant_persona: "NarratorPersona | None" = None,
    asset_visual_brief: dict | None = None,
    shared_assets: list[dict] | None = None,
) -> tuple[dict, dict, list[dict], dict]:
    """Deterministic clip assignment (翻转二, sole engine since 2026-06-11).

    Beat planner → per-beat retrieval → packer → the production validator,
    via :func:`_assign_clips_packer`. Script and narration pass through
    untouched; ``variant_profile`` / ``target_duration_sec`` supply the
    format card's pacing knobs (P2 step 3). The remaining unused
    per-variant kwargs (voice/tmp-dir/persona/...) are kept so the
    caller contract is stable — they belonged to the retired LLM-assigner +
    F3 script-regen chain (removed 2026-06-11 after the same-script A/B
    verdict; see docs/ROADMAP.md §执行日志).

    Returns ``(script, narration, assignments, provenance)``. Raises
    ``ClipAssignmentError`` (no coverable candidate), ``UsageWindowError``
    (ledger read failed — fail-closed per 设计契约 ②), or ``RuntimeError``
    (no embedding source) — all variant-abort conditions at the caller.
    """
    del variant_index, poi_name, location, hotel_description, notable_details
    del variant_voice_key, variant_tmp_dir, tts_speed
    del effective_wpm, n_variants_total, script_candidates
    del variant_persona, asset_visual_brief
    # P2 step 3: pacing knobs come from the variant's format card; fall
    # back to duration routing when the caller did not thread a profile.
    profile = variant_profile or get_promo_format_profile(target_duration_sec)
    return _assign_clips_packer(
        script,
        narration,
        clips_metadata,
        clip_durations,
        profile=profile,
        embedding_cache_dir=embedding_cache_dir,
        shared_assets=shared_assets,
    )


def _default_usage_client() -> Any:
    """Supabase client for the packer's usage-window reads.

    设计契约 ② (fail-closed): missing credentials raise instead of
    silently skipping window rotation — production platform-backed runs
    always carry these env vars (the asset download already needed them).
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError(
            "packer window rotation requires SUPABASE_URL + key in env "
            "(fail-closed per 设计契约 ②)"
        )
    from supabase import create_client

    return create_client(url, key)


def _attach_platform_embeddings(
    client: Any,
    clips_metadata: list[dict],
    clip_to_asset: dict[str, str],
) -> tuple[list[dict], list[Any]]:
    """Fetch ready vectors from ``poi_asset_embeddings`` (the production
    embedding source) and attach them per clip via the asset mapping.
    Same model+dim as the local sidecar path, so the packer's cosine
    against ``clip_embedder.embed_texts`` beat queries stays valid.
    Query errors propagate (fail-closed, 设计契约 ② spirit)."""
    from promo.core.assets.retrieval import (
        EMBEDDING_MODEL,
        parse_embedding_vector,
    )

    asset_ids = sorted(set(clip_to_asset.values()))
    rows = (
        client.table("poi_asset_embeddings")
        .select("asset_id,embedding_vector,embedding_model,status")
        .in_("asset_id", asset_ids)
        .eq("status", "ready")
        .eq("embedding_model", EMBEDDING_MODEL)
        .order("asset_id")
        .execute()
        .data
    ) or []
    vector_by_asset = {
        str(row["asset_id"]): list(parse_embedding_vector(row["embedding_vector"]))
        for row in rows
    }
    embedded: list[dict] = []
    dropped: list[Any] = []
    for meta in clips_metadata:
        asset_id = clip_to_asset.get(str(meta.get("id")).zfill(4))
        vector = vector_by_asset.get(asset_id) if asset_id else None
        if vector is not None:
            embedded.append({**meta, "embedding": vector})
        else:
            dropped.append(meta.get("id"))
    return embedded, dropped


def _assign_clips_packer(
    script: dict,
    narration: dict,
    clips_metadata: list[dict],
    clip_durations: dict[str, float],
    *,
    profile: PromoFormatProfile,
    embedding_cache_dir: str | None,
    shared_assets: list[dict] | None,
    rank_fn: Any | None = None,
    windows_fetcher: Any | None = None,
    usage_client_factory: Any | None = None,
) -> tuple[dict, dict, list[dict], dict]:
    """翻转二 B5 — deterministic assignment: beats → retrieval → packer.

    Same return contract as the legacy assigner path, but script/narration pass
    through untouched (no regen) and the output still runs through
    ``_enforce_hard_constraint_and_enrich`` — the validator stays the
    single renderer-contract arbiter regardless of assigner.

    Embeddings are REQUIRED (the packer ranks by cosine; there is no
    full-pool prompt to fall back to) and resolve through a three-step
    ladder: vectors already inline on ``clips_metadata`` → the local
    ``.embedding_cache`` sidecar (dev/build_embedding_index) → the
    platform ``poi_asset_embeddings`` table via the clip→asset mapping
    (the production supabase-backed shape; same model+dim as the local
    path, ``text-embedding-3-small``@1536). Usage-window reads are
    fail-closed per 设计契约 ②: ledger errors abort the variant
    (``--resume`` recovers); dev runs without an asset mapping skip
    rotation with a provenance flag instead.

    ``rank_fn`` / ``windows_fetcher`` / ``usage_client_factory`` are test
    seams only — production always uses the real implementations.
    """
    from promo.core.assign import clip_embedder, clip_retriever
    from promo.core.assign.beat_planner import beat_text, plan_beats
    from promo.core.assign.clip_assignment_validator import (
        _enforce_hard_constraint_and_enrich,
    )
    from promo.core.assign.packer import pack_clips
    from promo.core.assign.usage_windows import fetch_used_windows

    word_timestamps = narration["word_timestamps"]
    clip_to_asset = {
        str(row.get("clip_id")).zfill(4): str(row.get("asset_id"))
        for row in (shared_assets or [])
        if row.get("clip_id") is not None and row.get("asset_id")
    }
    # Production metadata (candidate_only_mode) carries asset_id per clip;
    # use it when the backend exposes no shared_assets mapping.
    if not clip_to_asset:
        clip_to_asset = {
            str(m.get("id")).zfill(4): str(m["asset_id"])
            for m in clips_metadata
            if m.get("id") is not None and m.get("asset_id")
        }
    client: Any | None = None

    def _client() -> Any:
        nonlocal client
        if client is None:
            client = (usage_client_factory or _default_usage_client)()
        return client

    sidecar_sha1 = None
    embedded_metadata = [m for m in clips_metadata if "embedding" in m]
    if embedded_metadata:
        embedding_source = "inline"
        dropped_clip_ids = [
            m.get("id") for m in clips_metadata if "embedding" not in m
        ]
    else:
        sidecar = (
            clip_embedder.load_embeddings_for_poi(embedding_cache_dir)
            if embedding_cache_dir is not None
            else None
        )
        if sidecar is not None:
            embedding_source = "sidecar"
            sidecar_sha1 = sidecar.get("mimo_prompt_sha1")
            embedded_metadata, dropped_clip_ids = (
                clip_embedder.attach_embeddings_to_metadata(
                    clips_metadata, sidecar,  # type: ignore[arg-type]
                )
            )
        elif clip_to_asset:
            embedding_source = "platform"
            embedded_metadata, dropped_clip_ids = _attach_platform_embeddings(
                _client(), clips_metadata, clip_to_asset,
            )
        else:
            raise RuntimeError(
                "clip assignment found no embeddings: none inline, "
                f"no sidecar at {embedding_cache_dir!r}, and no clip→asset "
                "mapping for a platform lookup — run build_embedding_index "
                "for local clips, or use a platform-backed POI"
            )
    if not embedded_metadata:
        raise RuntimeError(
            f"clip assignment: embedding source '{embedding_source}' "
            "matched zero clips"
        )
    if dropped_clip_ids:
        logger.warning(
            "packer: %d clip(s) lack embeddings and are outside the ranking "
            "pool: %s",
            len(dropped_clip_ids), dropped_clip_ids,
        )

    beats = plan_beats(
        script,  # type: ignore[arg-type]
        word_timestamps,
        max_beat_sec=profile.beat_max_sec,
        min_beat_sec=profile.beat_min_sec,
        max_beats=len(embedded_metadata),
    )
    queries = [beat_text(beat, word_timestamps) for beat in beats]
    rankings = (rank_fn or clip_retriever.rank_per_query)(queries, embedded_metadata)

    used_windows: dict = {}
    ledger_state = "no_asset_mapping"
    if clip_to_asset:
        # 设计契约 ② — UsageWindowError propagates: the variant aborts and
        # --resume recovers. Never silently degrade to trim-0 reruns.
        used_windows = (windows_fetcher or fetch_used_windows)(
            _client(), sorted(set(clip_to_asset.values())),
        )
        ledger_state = "loaded"
    else:
        logger.warning(
            "packer: no clip→asset mapping (local-clips run?) — window "
            "rotation inactive for this video",
        )

    raw_assignments, pack_provenance = pack_clips(
        beats,
        rankings,
        word_timestamps=word_timestamps,
        clip_durations=clip_durations,
        clip_metadata=embedded_metadata,
        max_beat_sec=profile.beat_max_sec,
        used_windows=used_windows,
        clip_to_asset=clip_to_asset,
    )
    assignments = _enforce_hard_constraint_and_enrich(
        raw_assignments,
        script,  # type: ignore[arg-type]
        word_timestamps,
        clip_durations,
    )

    provenance = _empty_retrieval_provenance()
    provenance.update({
        "retrieval_active": True,
        "assigner": "packer",
        "embedded_pool_size": len(embedded_metadata),
        "reduced_pool_size": len(embedded_metadata),
        "mimo_prompt_sha1": sidecar_sha1,
        "embedding_source": embedding_source,
        "usage_ledger": ledger_state,
        "packer": pack_provenance,
    })
    logger.info(
        "packer assigned %d beats (%d unique clips, ledger=%s, "
        "window_exhausted=%d, adjacency_relaxed=%d)",
        pack_provenance["beat_count"], pack_provenance["unique_clip_count"],
        ledger_state, len(pack_provenance["window_exhausted_beats"]),
        len(pack_provenance["adjacency_relaxed_beats"]),
    )
    return script, narration, assignments, provenance


# ---------------------------------------------------------------------------
#  Sprint 16 — selector seam plumbing
# ---------------------------------------------------------------------------


def _build_variant_selections(
    *,
    n_variants: int,
    poi_name: str,
    clips_metadata: list[dict],
    seed: int | None,
    target_duration_sec: float | int | None = None,
) -> tuple[list[PromoFormatProfile], "list[NarratorPersona]"]:
    """Instantiate the per-variant format + persona selectors and return
    the resolved per-variant lists.

    Sprint 16 — the format selector is chosen by the
    :func:`promo.core.config.promo_format_selector` resolver
    (env var ``PROMO_FORMAT_SELECTOR``, default ``"single"``). Default
    ``single`` pins every variant to the profile derived from
    ``target_duration_sec``, preserving the pre-Sprint-16 operator
    contract that ``--target-duration-sec X`` produces an X-second video
    for every variant. ``random`` (opt-in) samples a fresh profile per
    variant from :data:`promo.core.format_profiles.FORMAT_TEMPLATES`;
    operators who opt in accept the per-variant filename / BGM / sidecar
    caveats called out in ``architecture.md`` "Selector seams (Sprint 16)".

    The persona side defaults to :class:`SinglePersonaSelector` because
    only one persona YAML ships today; a future sprint may swap in a
    ``PROMO_PERSONA_SELECTOR`` resolver mirror.
    """
    from promo.core import config as promo_config
    from promo.core.selection import (
        FormatSelector,
        RandomFormatSelector,
        SingleFormatSelector,
        SinglePersonaSelector,
    )

    selector_name = promo_config.promo_format_selector()
    format_selector: FormatSelector
    if selector_name == "single":
        format_selector = SingleFormatSelector(
            target_duration_sec=target_duration_sec,
        )
    elif selector_name == "random":
        format_selector = RandomFormatSelector(seed=seed)
    else:
        # Reachable when config._ALLOWED_FORMAT_SELECTORS is widened
        # without a matching dispatch branch here. Surfaces as an
        # implementation gap rather than a misconfiguration so the
        # operator does not chase an env-var typo.
        raise NotImplementedError(
            f"Selector {selector_name!r} is in config._ALLOWED_FORMAT_SELECTORS "
            f"but not dispatched in _build_variant_selections — add an elif branch."
        )

    persona_selector = SinglePersonaSelector()
    profiles = format_selector.select(
        n_variants, poi_name=poi_name, clip_metadata=clips_metadata,
    )
    personas = persona_selector.select(
        n_variants, poi_name=poi_name, clip_metadata=clips_metadata,
    )
    logger.info(
        "Sprint 16 selector seams: format=%s seed=%s -> %s; persona=single -> %s",
        selector_name, seed, [p.mode for p in profiles],
        [p.id for p in personas],
    )
    if selector_name == "random":
        # Random opt-in caveat (Sprint 16 post-Codex review): when format
        # mode varies per variant, the run-level `--target-duration-sec`
        # scalar still drives output filename, BGM filtering, and sidecar
        # filename — none of which were rebuilt to per-variant in Sprint
        # 16. A short variant produced inside a 65s-tagged run will land
        # at `*_65s.mp4` (not `_30s`) and BGM will be filtered against
        # 65s. Sprint 17 (explicit format selector) is the canonical
        # resolution; until then the operator who opts into random sees
        # this WARNING on every run as the visible contract.
        logger.warning(
            "PROMO_FORMAT_SELECTOR=random opt-in active: per-variant "
            "duration is randomized but output filename / BGM filter / "
            "sidecar tag still use --target-duration-sec=%s. Variants "
            "with a different duration will be labeled inconsistently. "
            "See architecture.md 'Selector seams (Sprint 16)' for the "
            "documented caveat; Sprint 17 will land per-variant naming.",
            target_duration_sec,
        )
    return profiles, personas
