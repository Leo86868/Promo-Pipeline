"""Per-variant loop body for ``full_pipeline``.

Each iteration runs Step 4 (TTS), Step 4.5 (Gemini #2 clip assignment
with F3 retry), Step 7 (props build + freeze-prevention), Step 8
(Remotion render), and success-gates the per-variant observability
row appends into the run-level accumulators passed in by the caller.

Extracted from ``promo/cli/compile_promo.py`` lines 1194-1397 in
promo-handoff-readiness Sprint 4 A-001 (narrow — ``compile_promo.py``
decomposition). Behavior byte-identical to the pre-extraction site;
the three observability accumulator lists are mutated in place so the
caller's references see the success-gated rows without an extra
return-tuple payload.
"""

import json
import logging
import os
from typing import TYPE_CHECKING, Any

from promo.core.backend import PromoBackend
from promo.core.errors import FreezeWouldOccurError
from promo.core.format_profiles import PromoFormatProfile
from promo.core.assign.match_quality import build_match_quality_entries
from promo.core.pipeline.bgm_voice_resolver import (
    _empty_retrieval_provenance,
    _variant_output_path,
)
from promo.core.pipeline.steps import (
    _build_variant_tts_metrics,
    _step_assign_clips,
    _step_tts_narration,
)
from promo.core.render.remotion_renderer import (
    build_props_from_script,
    render_promo,
    stage_media,
    validate_props,
)

if TYPE_CHECKING:
    from promo.core.script.script_generator import NarratorPersona


logger = logging.getLogger(__name__)


def _music_metadata_for_path(backend: PromoBackend, bgm_path: str) -> dict[str, Any] | None:
    resolver = getattr(backend, "music_metadata_for_path", None)
    if not callable(resolver):
        return None
    metadata = resolver(bgm_path)
    return metadata if isinstance(metadata, dict) else None


def _run_variant_loop(
    *,
    scripts: list[dict],
    clip_paths: dict[str, str],
    clips_metadata: list[dict],
    clip_durations: dict[str, float],
    resolved_voice_keys: list[str],
    resolved_bgm_paths: list[str],
    variant_profiles: list[PromoFormatProfile],
    variant_personas: "list[NarratorPersona]",
    output_path: str,
    backend: PromoBackend,
    poi_name: str,
    location: str,
    hotel_description: str,
    notable_details: str,
    tmp_dir: str,
    tts_speed: float,
    target_duration_sec: float,
    script_candidates: int,
    embedding_cache_dir: str | None,
    tts_metrics: list[dict],
    match_quality_entries: list[dict],
    clip_assignments_entries: list[dict],
    rendered_outputs: list[dict] | None = None,
    asset_visual_brief: dict | None = None,
) -> tuple[bool, int, dict]:
    """Run the per-variant loop body end-to-end.

    ``tts_metrics``, ``match_quality_entries``, and
    ``clip_assignments_entries`` are mutated in place — only the rows
    for variants that completed ``render_promo`` successfully are
    appended (Sprint 09a M-004 / Sprint 10 C3 F1 success-gating
    preserved).

    Returns ``(all_ok, pool_exhaustion_hard_fails, run_retrieval_provenance)``
    so the caller can fold them into its run-level summary:

    - ``all_ok``: False when any variant aborted (F3 second-fail, freeze-
      prevention, props validation, render failure) or if any downstream
      write in ``_emit_run_sidecars`` would subsequently fail.
    - ``pool_exhaustion_hard_fails``: counter of aborted variants whose
      abort root cause traces back to pool exhaustion — F3 second-fail,
      the F3 regen RuntimeError path, or ``FreezeWouldOccurError``. Emitted
      as a grepable log line at run-end so post-hoc pool quality
      analysis has a single number to watch.
    - ``run_retrieval_provenance``: last-variant-wins snapshot of the
      retrieval-provenance dict so the ``clip_assignments`` sidecar can
      describe the run's retrieval state without per-variant noise.
      Owned entirely by this function (initialized at entry via
      ``_empty_retrieval_provenance()``; rebound on each successful
      ``_step_assign_clips`` return; surfaced through the return tuple).
      Not a kwarg — Sprint 4 post-audit L-001/D-001 removed the
      dead-weight kwarg after audit found the caller never read the
      mutated-in-place object (only the return value), creating a
      latent "this looks like an accumulator but isn't" trap.
    """
    all_ok = True
    run_retrieval_provenance = _empty_retrieval_provenance()
    # 翻转二 B5 — the packer assigner needs the clip→asset mapping for
    # usage-window rotation; platform backends expose it via
    # shared_assets(), local backends don't (rotation inactive).
    backend_shared_assets = (
        backend.shared_assets()  # type: ignore[attr-defined]
        if callable(getattr(type(backend), "shared_assets", None))
        else None
    )
    # Sprint 09b C7 introduced this counter for FreezeWouldOccurError
    # raises. Sprint 10 C2 extended it: the same counter now also
    # bumps on Sprint 10 F3 variant aborts (ClipAssignmentError on the
    # retry attempt, or the Gemini #1 regen RuntimeError when its
    # attempt budget is exhausted). All three abort causes mean "a
    # variant did not render," which is the metric's real semantic.
    pool_exhaustion_hard_fails = 0

    for script in scripts:
        variant_index = script["variant_index"]
        variant_target_duration = float(script.get("target_duration_sec", target_duration_sec))
        variant_output_path = _variant_output_path(
            output_path, variant_index, len(scripts), variant_target_duration,
        )
        variant_tmp_dir = os.path.join(tmp_dir, f"variant_{variant_index}")
        os.makedirs(variant_tmp_dir, exist_ok=True)

        # Round-robin BGM + voice assignments (deterministic from variant index)
        variant_bgm = resolved_bgm_paths[(variant_index - 1) % len(resolved_bgm_paths)]
        variant_voice_key = resolved_voice_keys[(variant_index - 1) % len(resolved_voice_keys)]
        variant_profile = variant_profiles[variant_index - 1]  # Sprint 16
        variant_persona = variant_personas[variant_index - 1]  # Sprint 16
        variant_effective_wpm = int(script["effective_wpm"])
        logger.info("=" * 60)
        logger.info(
            "Step 4-8: Rendering variant %d/%d (voice: %s, BGM: %s)...",
            variant_index, len(scripts), variant_voice_key,
            os.path.basename(variant_bgm),
        )

        # Step 4 (Sprint 10 C4): TTS narration extracted to helper.
        narration = _step_tts_narration(
            script, variant_voice_key, variant_tmp_dir, tts_speed,
        )

        # Step 4.5 (Sprint 10 C4): Gemini #2 clip assignment with F3
        # single-retry. ClipAssignmentError / RuntimeError on second
        # attempt abort this variant (other variants still render).
        from promo.core.assign.clip_assigner import ClipAssignmentError
        try:
            script, narration, variant_assignments, variant_retrieval = _step_assign_clips(
                script,
                narration,
                clips_metadata,
                clip_durations,
                variant_index,
                poi_name=poi_name,
                location=location,
                hotel_description=hotel_description,
                notable_details=notable_details,
                variant_voice_key=variant_voice_key,
                variant_tmp_dir=variant_tmp_dir,
                tts_speed=tts_speed,
                target_duration_sec=variant_target_duration,
                effective_wpm=variant_effective_wpm,
                n_variants_total=len(scripts),
                script_candidates=script_candidates,
                embedding_cache_dir=embedding_cache_dir,
                variant_profile=variant_profile,
                variant_persona=variant_persona,
                asset_visual_brief=asset_visual_brief,
                shared_assets=backend_shared_assets,
            )
            run_retrieval_provenance = variant_retrieval
            logger.info(
                "%s assigned %d phrases for variant %d",
                variant_retrieval.get("assigner") or "Gemini #2",
                len(variant_assignments), variant_index,
            )
        except ClipAssignmentError as exc:
            logger.error(
                "Variant %d aborted after Sprint 10 F3 retry: %s",
                variant_index, exc,
            )
            pool_exhaustion_hard_fails += 1
            all_ok = False
            continue
        except RuntimeError as exc:
            # F3 regen exhausted its Gemini #1 attempt budget.
            logger.error(
                "Variant %d F3 regen exhausted: %s", variant_index, exc,
            )
            pool_exhaustion_hard_fails += 1
            all_ok = False
            continue

        # Sprint 10 C4: variant-level TTS metrics building extracted.
        # Sprint 09a M-004 success-gating still applies — the entry is
        # built here but committed to the accumulator only after
        # render_promo() succeeds.
        variant_tts_entry = _build_variant_tts_metrics(
            narration, variant_index, variant_voice_key, variant_target_duration,
        )

        logger.info("Step 7: Building props.json for variant %d...", variant_index)
        variant_timeline_entries: list[dict] = []
        try:
            # TypedDict boundary — remotion_renderer narrows ``narration_result``
            # / ``assignments`` to ``Narration`` / ``list[ClipAssignment]``
            # internally; variant loop carries the pipeline's loose ``dict`` /
            # ``list[dict]`` shapes. Same pattern as ``_step_generate_script``.
            props = build_props_from_script(
                poi_name=poi_name,
                location=location,
                script_segments=script["segments"],
                clip_paths=clip_paths,
                narration_result=narration,  # type: ignore[arg-type]
                bgm_path=variant_bgm,
                assignments=variant_assignments,  # type: ignore[arg-type]
                target_duration_sec=variant_target_duration,
                timeline_entries=variant_timeline_entries,
            )
        except FreezeWouldOccurError as exc:
            # Sprint 09a M-006: freeze-prevention raised mid-binding.
            # Do not render this variant. Log the root cause and
            # continue to the next variant (happy-path variants are
            # still salvageable if the pool is asymmetric).
            # Sprint 09b C7: count the raise for the end-of-run
            # pool-exhaustion metric.
            logger.error(
                "Variant %d aborted to prevent freeze: %s",
                variant_index, exc,
            )
            pool_exhaustion_hard_fails += 1
            all_ok = False
            continue

        errors = validate_props(props, check_files=False)
        if errors:
            logger.error("Props structural validation failed for variant %d:", variant_index)
            for err in errors:
                logger.error("  - %s", err)
            logger.error("Props dump:\n%s", json.dumps(props, indent=2))
            all_ok = False
            continue

        stage_media(
            clip_paths=list(clip_paths.values()),
            narration_path=narration["audio_path"],
            bgm_path=variant_bgm,
            poi_name=poi_name,
        )

        # Sprint 09a M-004: build the match-quality rows locally, same
        # success-gating rule as the TTS metrics entry above. Sprint 10
        # C5: rows are now derived from the Gemini #2 assignments list
        # plus the TTS word_timestamps (phrase text reconstructed by
        # slicing on word-idx), not from ``script["segments"]``.
        variant_match_quality = build_match_quality_entries(
            assignments=variant_assignments,
            clips_metadata=clips_metadata,
            word_timestamps=narration["word_timestamps"],
            variant_index=variant_index,
        )

        # ``build_props_from_script`` returns ``dict[str, object]`` — mypy
        # won't narrow to the concrete list/dict shapes without explicit
        # casts. Ignore the Sized / index-access errors here since the JSON
        # schema is enforced by ``validate_props`` above.
        logger.info(
            "Props v%d ready: %d clips, %d words, %d segments",
            variant_index,
            len(props["clips"]),  # type: ignore[arg-type]
            len(props["captions"]["wordTimestamps"]),  # type: ignore[index]
            len(props["segments"]),  # type: ignore[arg-type]
        )

        logger.info("Step 8: Rendering via Remotion...")
        ok = render_promo(props, variant_output_path)
        if not ok:
            all_ok = False
            continue

        # Sprint 09a M-004: render succeeded — only now commit this
        # variant's observability rows to the sidecar accumulators.
        tts_metrics.append(variant_tts_entry)
        match_quality_entries.extend(variant_match_quality)
        # Sprint 10 C3 (F1): clip_assignments sidecar — success-gated
        # mirror of tts_metrics / match_quality. Aborted variants
        # (F3 second-fail, render fail, pool exhaustion) do NOT
        # appear here, matching the 09a M-004 semantic.
        clip_assignments_entries.append({
            "variant_index": variant_index,
            "variant_status": "rendered",
            "assignments": variant_assignments,
            # Sprint 16 D-003 — discriminator fields so a mixed-mode
            # sidecar (short + long variants in one file) is readable
            # without a side-channel for the per-variant duration.
            "target_duration_sec": variant_target_duration,
            "format_mode": script.get("format_mode"),
            # Review bug ② (2026-06-11): a replayed render must declare
            # itself in its own records — None for normal generation.
            "replayed_from": script.get("replay_source"),
            # 翻转二 B6 — the FINAL accepted script rides with its
            # assignments so a later run can replay it verbatim
            # (`compile_promo --replay-script`): same-script A/B holds the
            # narration constant while swapping the clip assigner.
            # pause_after_ms is intentionally stripped — replay recomputes
            # the pause budget against its own calibrated wpm.
            "script": {
                "segments": [
                    {
                        "segment": seg.get("segment", i + 1),
                        "text": seg["text"],
                        "pause_weight": seg.get("pause_weight", 1),
                    }
                    for i, seg in enumerate(script["segments"])
                ],
                "format_mode": script.get("format_mode"),
                "hook_technique": script.get("hook_technique"),
            },
        })

        size_mb = os.path.getsize(variant_output_path) / (1024 * 1024)
        logger.info("=" * 60)
        logger.info(
            "PROMO VIDEO COMPLETE (v%d): %s (%.1f MB)",
            variant_index,
            variant_output_path,
            size_mb,
        )
        logger.info("=" * 60)

        final_loc = backend.save_output(poi_name, variant_output_path)
        if final_loc != variant_output_path:
            logger.info("Output saved to: %s", final_loc)
        if rendered_outputs is not None:
            music_metadata = _music_metadata_for_path(backend, variant_bgm)
            rendered_output = {
                "variant_index": variant_index,
                "variant_status": "rendered",
                "render_output_path": variant_output_path,
                "final_output_path": final_loc,
                "target_duration_sec": variant_target_duration,
                "format_mode": script.get("format_mode"),
                "voice_key": variant_voice_key,
                "bgm_path": variant_bgm,
                "file_size_bytes": os.path.getsize(variant_output_path),
                "timeline_entries": variant_timeline_entries,
            }
            if music_metadata:
                rendered_output["music"] = music_metadata
                rendered_output["music_label"] = music_metadata.get("music_label")
            rendered_outputs.append(rendered_output)

    return all_ok, pool_exhaustion_hard_fails, run_retrieval_provenance
