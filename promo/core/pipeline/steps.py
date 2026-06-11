"""Pipeline step helpers for ``full_pipeline``.

Each helper corresponds to a discrete stage in the two-pass Gemini
flow: Steps 1/2/2.5 (clip prep), Step 3 (Gemini #1 + pause budget),
Step 4 (TTS narration), Step 4.5 (Gemini #2 clip assignment with F3
retry), plus the Sprint 16 ``_build_variant_selections`` seam for
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


# Sprint 12b / Sprint 13 post-audit DC-6: retrieval top-k for the Gemini #2
# inventory-narrowing union. Tuning this is a calibration choice; previously
# hardcoded in 3 call sites inside _step_assign_clips. A single constant keeps
# the closure's log line, the union_of_top_k call, and any future analytical
# consumer in lockstep.
RETRIEVAL_TOP_K = 6
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
# but the two-pass architecture replaces that with Gemini #2's per-phrase
# hard constraint (source_dur − trim_start ≥ display_span), which makes the
# tail reserve redundant.


# ---------------------------------------------------------------------------
#  Sprint 10 C4 — full_pipeline step helpers
# ---------------------------------------------------------------------------
#
# Extracted from full_pipeline under the F4 decomposition goal: the two-pass
# Gemini flow naturally splits Step 3 (Gemini #1 script) from Step 4 (TTS)
# from the new Step 4.5 (Gemini #2 clip assignment + F3 retry). Each helper
# is callable standalone — closures over full_pipeline locals are replaced
# with explicit kwargs so tests can drive them directly.


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
    ``clips[]`` — assignments are Gemini #2's job in ``_step_assign_clips``).

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
    from promo.core.narrate.tts_engine import VOICE_CATALOG
    from promo.core.script.script_generator import generate_script_variants

    if replay_script is not None:
        if n_variants != 1:
            raise ValueError(
                f"--replay-script requires n_variants=1 (got {n_variants})"
            )
        segments = replay_script.get("segments")
        if not isinstance(segments, list) or not segments:
            raise ValueError("replay script has no segments")
        script: dict = {
            "variant_index": 1,
            "segments": [dict(seg) for seg in segments],
            "total_words": sum(
                len(str(seg.get("text") or "").split()) for seg in segments
            ),
            "format_mode": replay_script.get("format_mode"),
            "hook_technique": replay_script.get("hook_technique"),
            "target_duration_sec": target_duration_sec,
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
        script_target = float(script.get("target_duration_sec", target_duration_sec))
        compute_pause_budget(
            script["segments"],  # type: ignore[arg-type]
            target_sec=script_target,
            wpm=effective_wpm,
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


def _filter_clips_by_ids(
    clips_metadata: list[dict], reduced_ids: list[str],
) -> list[dict]:
    """Filter ``clips_metadata`` to entries whose ``id`` is in ``reduced_ids``,
    preserving ``reduced_ids`` insertion order (not ``clips_metadata`` order).

    Sprint 12b — ``union_of_top_k`` returns ranked-then-deduped clip_ids;
    preserving that order when feeding Gemini #2 carries the retrieval
    signal through to the prompt (primacy in the inventory block).
    """
    by_id = {str(clip["id"]): clip for clip in clips_metadata}
    return [by_id[cid] for cid in reduced_ids if cid in by_id]


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
    """Gemini #2 clip assignment with F3 single-retry policy.

    Sprint 10 C4: extraction of the F3 retry block from ``full_pipeline``.
    Builds ``regenerate_script_fn`` + ``regenerate_narration_fn`` closures
    over the variant's context and delegates to
    :func:`clip_assigner.assign_clips_with_f3_retry`.

    Returns ``(final_script, final_narration, assignments, retrieval_provenance)``
    on success. Raises :class:`ClipAssignmentError` when Gemini #2 violates
    the hard constraint on its second attempt, or :class:`RuntimeError`
    when the Gemini #1 regeneration call itself exhausts its attempt
    budget — both are variant-abort conditions at the caller.

    Sprint 12b — when ``embedding_cache_dir`` is supplied, builds a
    retrieval closure (``union_of_top_k`` over segment-text queries at
    k=6) that narrows ``clips_metadata`` before each Gemini #2 attempt.
    Falls back to the full pool with a WARNING log when either the
    sidecar's embedded pool shrank below the MiMo pool, or the
    per-segment union came out shorter than the segment count.

    Sprint 13 AC19 (D-004) — the retrieval closure updates the
    ``retrieval_provenance`` dict returned to the caller so the
    ``clip_assignments`` sidecar can record retrieval_active,
    embedded_pool_size, reduced_pool_size, mimo_prompt_sha1, and
    fallback_reason at run-end. Post-hoc quality regression attribution
    no longer has to mine the log files for these signals.

    Sprint 18 F — retrieval is a **soft hint** (advisory). The four
    fallback codes (``no_sidecar``, ``m4_attach_shrinkage``,
    ``h2_union_shortfall``, ``retrieval_exception``) encode cases where
    retrieval did not even reach Gemini #2 with a narrowed pool and the
    full ``clips_metadata`` was used instead; even when retrieval *did*
    produce a narrowed pool, ``assign_clips_with_f3_retry`` does not
    reject a Gemini #2 reply whose ``clip_id`` falls outside the
    retrieved subset. The provenance dict carries
    ``retrieval_contract: "soft_hint"`` (seeded by
    ``_empty_retrieval_provenance``) so the ``clip_assignments_*.json``
    sidecar declares the contract explicitly. See
    ``docs/schemas/clip_assignments.md`` "Soft hint contract" + the
    matching block in ``clip_assigner.assign_clips_with_f3_retry``'s
    docstring for the design rationale.
    """
    from promo.core.assign.clip_assigner import assign_clips_with_f3_retry
    from promo.core.script.script_generator import regenerate_single_variant_with_hint
    from promo.core.narrate.tts_engine import generate_narration

    from promo.core import config as promo_config

    # 翻转二 B5 — PROMO_CLIP_ASSIGNER=packer routes to the deterministic
    # chain (beat planner → per-beat retrieval → packer → same validator).
    # No F3 machinery: with beats targeting ≤4s vs clips ≥5s the duration
    # hard-constraint loses its normal failure mode — though long authored
    # pauses can still produce over-ceiling beats, which surface loudly
    # (planner WARNING + provenance["packer"]["overlong_beats"]) instead
    # of being retried.
    if promo_config.clip_assigner() == "packer":
        return _assign_clips_packer(
            script,
            narration,
            clips_metadata,
            clip_durations,
            embedding_cache_dir=embedding_cache_dir,
            shared_assets=shared_assets,
        )

    def _regen_script(hint: str) -> dict:
        # Sprint 16 — F3 regen uses the SAME profile + persona the original
        # variant was built with, threaded from the variant-loop selectors.
        # TypedDict boundary (same reasoning as ``_step_generate_script``):
        # pipeline carries ``list[dict]``; script_generator narrows internally.
        return regenerate_single_variant_with_hint(  # type: ignore[return-value]
            poi_name=poi_name,
            location=location,
            clips_metadata=clips_metadata,  # type: ignore[arg-type]
            hotel_description=hotel_description,
            notable_details=notable_details,
            variant_index=variant_index,
            n_variants=n_variants_total,
            variant_plan=None,
            tighten_hint=hint,
            n_candidates=script_candidates,
            target_duration_sec=target_duration_sec,
            profile=variant_profile,
            persona=variant_persona,
            asset_visual_brief=asset_visual_brief,
        )

    def _regen_narration(new_script: dict) -> dict:
        compute_pause_budget(
            new_script["segments"],
            target_sec=target_duration_sec,
            wpm=effective_wpm,
        )
        retry_dir = os.path.join(variant_tmp_dir, "f3_retry")
        os.makedirs(retry_dir, exist_ok=True)
        return generate_narration(  # type: ignore[return-value]
            segments=new_script["segments"],
            voice_key=variant_voice_key,
            output_dir=retry_dir,
            speed=tts_speed,
        )

    # Sprint 13 AC19 — retrieval provenance. Default = "inactive"; the
    # reduced_pool_size default of full-pool is meaningful for the
    # "retrieval never ran" path (no embedding_cache_dir threaded).
    retrieval_provenance = _empty_retrieval_provenance()
    retrieval_provenance["reduced_pool_size"] = len(clips_metadata)
    retrieve_clips_fn = None
    if embedding_cache_dir is not None:
        from promo.core.assign import clip_embedder, clip_retriever

        sidecar = clip_embedder.load_embeddings_for_poi(embedding_cache_dir)
        if sidecar is None:
            retrieval_provenance["fallback_reason"] = "no_sidecar"
            logger.warning(
                "Sprint 12b retrieval disabled: no embedding sidecar at %s "
                "(run build_embedding_index for this POI). Falling back to "
                "full-pool Gemini #2 (Sprint 11 behavior).",
                embedding_cache_dir,
            )
        else:
            embedded_metadata, dropped_clip_ids = (
                clip_embedder.attach_embeddings_to_metadata(
                    clips_metadata, sidecar,  # type: ignore[arg-type]
                )
            )
            retrieval_provenance["retrieval_active"] = True
            retrieval_provenance["embedded_pool_size"] = len(embedded_metadata)
            # Sprint 13 post-audit D-004: an embedding sidecar missing
            # `mimo_prompt_sha1` (externally-modified or hand-crafted) would
            # otherwise silently record sha1=null alongside retrieval_active=
            # true — inconsistent for the cross-reference to the sidecar
            # filename. Warn + record None so the provenance is at least
            # diagnosable.
            sha1 = sidecar.get("mimo_prompt_sha1")
            if sha1 is None:
                logger.warning(
                    "Sprint 13 provenance gap: embedding sidecar at %s is "
                    "missing 'mimo_prompt_sha1'; clip_assignments record "
                    "will store mimo_prompt_sha1=null. Rebuild the index to "
                    "restore the cross-reference to .embedding_cache/.",
                    embedding_cache_dir,
                )
            retrieval_provenance["mimo_prompt_sha1"] = sha1
            # Sprint 13 post-audit D-006: suppress duplicate `m4_attach_shrinkage`
            # WARNINGs on F3 retry. The shrinkage condition is a static property
            # of the loaded sidecar — if it held on the first attempt, it will
            # hold on the F3 retry too, and the unified WARNING should fire ONCE
            # per variant.
            _retrieve_state = {"m4_warned": False}

            def _retrieve(current_script: dict) -> list[dict]:
                try:
                    if len(embedded_metadata) < len(clips_metadata):
                        # Sprint 13 AC18 (D-002): unified fallback WARNING folds
                        # counts + dropped_ids + fallback reason into one record.
                        if not _retrieve_state["m4_warned"]:
                            logger.warning(
                                "Sprint 12b retrieval fallback [m4_attach_shrinkage]: "
                                "embedded pool shrunk from %d to %d clips (dropped: %s); "
                                "using full clips_metadata for Gemini #2 attempt.",
                                len(clips_metadata), len(embedded_metadata),
                                dropped_clip_ids,
                            )
                            _retrieve_state["m4_warned"] = True
                        retrieval_provenance["fallback_reason"] = "m4_attach_shrinkage"
                        retrieval_provenance["reduced_pool_size"] = len(clips_metadata)
                        return clips_metadata
                    narration_queries = [
                        seg["text"] for seg in current_script.get("segments", [])
                    ]
                    reduced_ids, union_size = clip_retriever.union_of_top_k(
                        narration_queries, embedded_metadata, k=RETRIEVAL_TOP_K,  # type: ignore[arg-type]
                    )
                    if union_size < len(narration_queries):
                        logger.warning(
                            "Sprint 12b retrieval fallback [h2_union_shortfall]: "
                            "union size %d < %d narration queries (heavy overlap "
                            "at k=%d); using full clips_metadata for Gemini #2 attempt.",
                            union_size, len(narration_queries), RETRIEVAL_TOP_K,
                        )
                        retrieval_provenance["fallback_reason"] = "h2_union_shortfall"
                        retrieval_provenance["reduced_pool_size"] = len(clips_metadata)
                        return clips_metadata
                    reduced = _filter_clips_by_ids(clips_metadata, reduced_ids)
                    logger.info(
                        "Sprint 12b retrieval narrowed Gemini #2 inventory: "
                        "%d clips → %d clips (union of top-%d over %d segments)",
                        len(clips_metadata), len(reduced), RETRIEVAL_TOP_K,
                        len(narration_queries),
                    )
                    retrieval_provenance["fallback_reason"] = None
                    retrieval_provenance["reduced_pool_size"] = len(reduced)
                    return reduced
                except Exception:
                    # Sprint 13 post-audit L-001: `assign_clips_with_f3_retry`
                    # swallows any exception from this closure and falls back
                    # to the full pool (design intent preserved). Record the
                    # exception in retrieval_provenance so the sidecar does not
                    # falsely advertise a clean retrieval when a fallback
                    # actually fired.
                    retrieval_provenance["fallback_reason"] = "retrieval_exception"
                    retrieval_provenance["reduced_pool_size"] = len(clips_metadata)
                    raise

            retrieve_clips_fn = _retrieve

    # TypedDict boundary — ``assign_clips_with_f3_retry`` narrows
    # ``script``/``narration``/``clips_metadata`` to their TypedDict forms
    # internally; the pipeline carries the loose ``dict``/``list[dict]``
    # shapes that matched the pre-Sprint-14 public surface. Same pattern as
    # ``_step_generate_script``: runtime identical; ignore preserves the
    # ``list[dict]`` pipeline contract until a future sprint retypes it.
    final_script, final_narration, assignments = assign_clips_with_f3_retry(
        script,  # type: ignore[arg-type]
        narration,  # type: ignore[arg-type]
        clips_metadata,  # type: ignore[arg-type]
        clip_durations,
        variant_index=variant_index,
        regenerate_script_fn=_regen_script,  # type: ignore[arg-type]
        regenerate_narration_fn=_regen_narration,  # type: ignore[arg-type]
        retrieve_clips_fn=retrieve_clips_fn,  # type: ignore[arg-type]
    )
    return final_script, final_narration, assignments, retrieval_provenance  # type: ignore[return-value]


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
            "(fail-closed per 设计契约 ②; set PROMO_CLIP_ASSIGNER=gemini2 "
            "to fall back to the legacy assigner)"
        )
    from supabase import create_client

    return create_client(url, key)


def _assign_clips_packer(
    script: dict,
    narration: dict,
    clips_metadata: list[dict],
    clip_durations: dict[str, float],
    *,
    embedding_cache_dir: str | None,
    shared_assets: list[dict] | None,
    rank_fn: Any | None = None,
    windows_fetcher: Any | None = None,
    usage_client_factory: Any | None = None,
) -> tuple[dict, dict, list[dict], dict]:
    """翻转二 B5 — deterministic assignment: beats → retrieval → packer.

    Same return contract as the Gemini #2 path, but script/narration pass
    through untouched (no regen) and the output still runs through
    ``_enforce_hard_constraint_and_enrich`` — the validator stays the
    single renderer-contract arbiter regardless of assigner.

    Embeddings are REQUIRED (the packer ranks by cosine; there is no
    full-pool prompt to fall back to). Usage-window reads are fail-closed
    per 设计契约 ②: ledger errors abort the variant (``--resume``
    recovers); dev runs without an asset mapping skip rotation with a
    provenance flag instead.

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
    if embedding_cache_dir is None:
        raise RuntimeError(
            "PROMO_CLIP_ASSIGNER=packer requires the embedding sidecar "
            "(embedding_cache_dir is unset — run build_embedding_index)"
        )
    sidecar = clip_embedder.load_embeddings_for_poi(embedding_cache_dir)
    if sidecar is None:
        raise RuntimeError(
            f"PROMO_CLIP_ASSIGNER=packer: no embedding sidecar at "
            f"{embedding_cache_dir} — run build_embedding_index for this POI"
        )
    embedded_metadata, dropped_clip_ids = clip_embedder.attach_embeddings_to_metadata(
        clips_metadata, sidecar,  # type: ignore[arg-type]
    )
    if not embedded_metadata:
        raise RuntimeError(
            "PROMO_CLIP_ASSIGNER=packer: embedding sidecar matched zero clips"
        )
    if dropped_clip_ids:
        logger.warning(
            "packer: %d clip(s) lack embeddings and are outside the ranking "
            "pool: %s (rebuild the index to include them)",
            len(dropped_clip_ids), dropped_clip_ids,
        )

    beats = plan_beats(
        script,  # type: ignore[arg-type]
        word_timestamps,
        max_beats=len(embedded_metadata),
    )
    queries = [beat_text(beat, word_timestamps) for beat in beats]
    rankings = (rank_fn or clip_retriever.rank_per_query)(queries, embedded_metadata)

    clip_to_asset = {
        str(row.get("clip_id")).zfill(4): str(row.get("asset_id"))
        for row in (shared_assets or [])
        if row.get("clip_id") is not None and row.get("asset_id")
    }
    used_windows: dict = {}
    ledger_state = "no_asset_mapping"
    if clip_to_asset:
        client = (usage_client_factory or _default_usage_client)()
        # 设计契约 ② — UsageWindowError propagates: the variant aborts and
        # --resume recovers. Never silently degrade to trim-0 reruns.
        used_windows = (windows_fetcher or fetch_used_windows)(
            client, sorted(set(clip_to_asset.values())),
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
        "mimo_prompt_sha1": sidecar.get("mimo_prompt_sha1"),
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
