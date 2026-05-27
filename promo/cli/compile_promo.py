#!/usr/bin/env python3
"""CLI entry point for promo video compilation via Remotion.

Full pipeline: clips → MiMo analysis → Gemini script →
               ElevenLabs v2 TTS (native word timestamps) → BGM →
               props.json → Remotion render

The pipeline is backend-agnostic: external I/O (clip fetching, BGM,
output saving) is abstracted behind a PromoBackend Protocol.

Usage:
    # Standalone mode (local clips directory, no Supabase needed)
    python3 -m promo.cli.compile_promo --poi "Hotel" --local-clips ./my_clips/

    # Render from existing props.json
    python3 -m promo.cli.compile_promo --render-props path/to/props.json

This module is the CLI shell. Pipeline orchestration lives in
``promo.core.pipeline`` (promo-handoff-readiness Sprint 4 A-001 narrow
decomposition). Private helpers that the test suite imports directly
(``_variant_output_path``, ``_discover_bgm_files``, ``_write_sidecar``,
``_step_tts_narration``, ``_step_assign_clips``, ``_filter_clips_by_ids``)
are re-exported from their new subpackage locations to keep the
``from promo.cli.compile_promo import ...`` import surface stable.
"""
# user-facing CLI

import argparse
import json
import logging
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from promo.core import config
from promo.core import sanitize_poi_name as _safe_poi_dir
from promo.core.backend import PromoBackend, LocalBackend
from promo.core.errors import MimoAnalysisError, NoSuitableBGMError
from promo.core.poi_asset_backend import PoiAssetSupabaseBackend
from promo.core.render.remotion_renderer import REMOTION_DIR, render_promo, validate_props

# Re-exports preserve the ``from promo.cli.compile_promo import <symbol>``
# surface plus ``inspect.getsource(compile_promo.<symbol>)`` so the test
# suite does not have to chase extracted symbols through the subpackage
# tree. Tests that patch a symbol at its CALL site (where a consumer does
# ``from promo.core.pipeline.<module> import X``) target the consumer
# module directly — not this file — because re-exports do not rewire
# already-imported ``from`` bindings.
from promo.core.pipeline import full_pipeline as full_pipeline
from promo.core.pipeline.bgm_voice_resolver import (
    _discover_bgm_files as _discover_bgm_files,
    _resolve_bgm_paths as _resolve_bgm_paths,
    _resolve_voice_keys as _resolve_voice_keys,
    _variant_output_path as _variant_output_path,
)
from promo.core.pipeline.sidecar_writer import (
    _emit_run_sidecars as _emit_run_sidecars,
    _write_sidecar as _write_sidecar,
)
from promo.core.pipeline.steps import (
    _build_variant_selections as _build_variant_selections,
    _filter_clips_by_ids as _filter_clips_by_ids,
    _step_assign_clips as _step_assign_clips,
    _step_generate_script as _step_generate_script,
    _step_prepare_clips as _step_prepare_clips,
    _step_tts_narration as _step_tts_narration,
    analyze_clips_for_script as analyze_clips_for_script,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Render-only shortcut
# ---------------------------------------------------------------------------

def render_from_props_file(props_path: str, output_path: str) -> bool:
    """Load an existing props.json and render it."""
    with open(props_path, "r") as f:
        props = json.load(f)

    errors = validate_props(props)
    if errors:
        logger.error("Validation failed:")
        for err in errors:
            logger.error("  - %s", err)
        return False

    return render_promo(props, output_path)


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def _build_backend(args) -> PromoBackend:
    """Construct the appropriate backend from CLI arguments.

    Note: --bgm is handled separately by full_pipeline (it takes precedence
    over backend.fetch_bgm). LocalBackend.bgm_path is only for programmatic
    use when calling the backend directly, not via CLI.
    """
    if args.local_clips:
        if args.supabase_poi_id or args.supabase_canonical_key:
            raise ValueError("choose either --local-clips or a Supabase POI lookup")
        if args.supabase_music_library or args.supabase_music_id:
            raise ValueError("Supabase Music Library requires a Supabase POI lookup")
        return LocalBackend(
            clips_dir=args.local_clips,
            output_dir=args.output_dir,
        )
    if args.supabase_poi_id or args.supabase_canonical_key:
        return PoiAssetSupabaseBackend.from_env(
            poi_id=args.supabase_poi_id,
            canonical_key=args.supabase_canonical_key,
            output_dir=args.output_dir,
            use_music_library=args.supabase_music_library or bool(args.supabase_music_id),
            music_id=args.supabase_music_id,
            music_min_duration_sec=args.target_duration_sec,
        )
    if args.supabase_music_library or args.supabase_music_id:
        raise ValueError("Supabase Music Library requires a Supabase POI lookup")
    raise ValueError(
        "--local-clips or --supabase-poi-id/--supabase-canonical-key is required",
    )


def _build_parser() -> argparse.ArgumentParser:
    """Build the compile_promo argparse parser.

    Extracted from `main()` in promo-handoff-readiness Sprint 1 AC-B3 so
    `TestArgparsePrecedence` in `promo/tests/test_compile_promo.py` can
    exercise CLI-override semantics (flag > env > hardcoded default)
    without subprocess invocation.

    `default=config.default_X()` semantics: argparse evaluates the
    `default=` expression once per parser construction. `_build_parser()`
    is called inside `main()` (not at module import), so the env read
    happens once per process invocation right before `parse_args()` runs.
    CLI flag provided → flag value wins. CLI flag omitted → env var
    wins (resolver reads the env). Env unset → hardcoded resolver
    default wins.
    """
    parser = argparse.ArgumentParser(description="Compile promo narration video via Remotion")
    parser.add_argument("--poi", type=str, help="Hotel/POI name")
    parser.add_argument("--location", type=str, default="", help="Location string")
    parser.add_argument("--output", "-o", type=str, default=None, help="Output MP4 path")
    parser.add_argument(
        "--voice", type=str, default=None,
        help="VOICE_CATALOG key: kore (Gemini 3.1 Flash TTS) or "
             "jarnathan/hope/heather (ElevenLabs v2). "
             "Default: rotate round-robin through the catalog by variant index.",
    )
    parser.add_argument("--bgm", type=str, default=None, help="Path to a single BGM file")
    parser.add_argument("--bgm-dir", type=str, default=None,
                        help="Directory of .mp3 files for per-variant BGM rotation")
    parser.add_argument(
        "--supabase-music-library",
        action="store_true",
        help=(
            "Fetch BGM from public.music_library using duration_sec >= "
            "--target-duration-sec"
        ),
    )
    parser.add_argument(
        "--supabase-music-id",
        type=str,
        default=None,
        help="Fetch one exact public.music_library row by id and validate duration_sec",
    )
    parser.add_argument("--skip-analysis", action="store_true",
                        help="Skip MiMo clip analysis (use blank descriptions)")
    parser.add_argument("--render-props", type=str, default=None,
                        help="Render from existing props.json (skips all pipeline stages)")
    parser.add_argument(
        "--target-duration-sec",
        type=float,
        default=config.default_duration_sec(),
        help="Target promo duration in seconds (default from PROMO_DEFAULT_DURATION_SEC or 30)",
    )
    parser.add_argument(
        "--n-variants",
        type=int,
        default=config.default_variants(),
        help="Number of promo variants to render (default from PROMO_DEFAULT_VARIANTS or 1)",
    )
    parser.add_argument(
        "--script-candidates",
        type=int,
        default=config.default_script_candidates(),
        help="Accepted script attempts per variant (default from PROMO_DEFAULT_SCRIPT_CANDIDATES or 1)",
    )
    parser.add_argument(
        "--tts-speed",
        type=float,
        default=0.95,
        help=(
            "ElevenLabs voice_settings.speed override (default 0.95). "
            "Drop to 0.90-0.92 when the pause budget is tail-constrained "
            "and the pipeline suggests it in the warning log."
        ),
    )

    # Standalone local-first flags
    parser.add_argument("--local-clips", type=str, default=None,
                        help="Path to local clips directory (standalone mode, no Supabase)")
    parser.add_argument(
        "--supabase-poi-id",
        type=str,
        default=None,
        help="Read clips from public.poi_asset_valid_clips by stable poi_id",
    )
    parser.add_argument(
        "--supabase-canonical-key",
        type=str,
        default=None,
        help="Read clips from public.poi_asset_valid_clips by canonical_key",
    )
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Directory to save output")

    # Sprint 16 — selector seam reproducibility flag. Landed here (inside
    # `_build_parser()` after the promo-handoff-readiness Sprint 1 parser
    # refactor) so that the parallel N3 zone (the lines 1290-1306 group)
    # stays semantically untouched; this arg is orthogonal to those.
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help=(
            "Integer seed for the Sprint 16 per-variant FormatSelector / "
            "PersonaSelector. None (default) lets the selectors draw from "
            "OS entropy; pin a seed for reproducible variant mixes."
        ),
    )

    return parser


def main():
    from promo.core.logging_config import configure_logging
    configure_logging()

    parser = _build_parser()
    args = parser.parse_args()

    if args.render_props:
        output = args.output or os.path.join(REMOTION_DIR, "out", "promo_output.mp4")
        ok = render_from_props_file(args.render_props, output)
        sys.exit(0 if ok else 1)

    if not args.poi:
        parser.error("--poi is required (or use --render-props)")

    has_supabase_lookup = bool(args.supabase_poi_id or args.supabase_canonical_key)
    if args.local_clips and has_supabase_lookup:
        parser.error("choose either --local-clips or a Supabase POI lookup")
    if not args.local_clips and not has_supabase_lookup:
        parser.error(
            "--local-clips or --supabase-poi-id/--supabase-canonical-key is required",
        )
    if (args.supabase_music_library or args.supabase_music_id) and not has_supabase_lookup:
        parser.error("Supabase Music Library requires a Supabase POI lookup")
    if (args.supabase_music_library or args.supabase_music_id) and (
        args.bgm or args.bgm_dir
    ):
        parser.error("choose either Supabase Music Library or --bgm/--bgm-dir")

    backend = _build_backend(args)

    # Encode target duration in filename when using output-dir (avoids collision
    # when running 30s + 65s in the same directory)
    if args.output:
        output = args.output
    elif args.output_dir:
        dur_label = f"{int(args.target_duration_sec)}s"
        output = os.path.join(
            args.output_dir,
            f"promo_{_safe_poi_dir(args.poi)}_{dur_label}.mp4",
        )
    else:
        output = os.path.join(
            REMOTION_DIR, "out",
            f"promo_{_safe_poi_dir(args.poi)}.mp4",
        )

    # Sprint 18 audit-fix C: pre-render collision-bump. For the
    # --output-dir flow (and any flow where the renderer writes
    # directly to ``output``), the renderer would clobber a prior
    # same-named MP4 BEFORE ``LocalBackend.save_output`` ever runs.
    # Bump the output filename here, before render_promo is invoked,
    # so the prior deliverable stays on disk under its unbumped name.
    # Mirrors ``_write_sidecar``'s ``-N`` algorithm; on
    # 999-attempt cap exhaustion, raises so the operator surfaces it.
    if os.path.exists(output):
        out_dir = os.path.dirname(output) or "."
        base = os.path.basename(output)
        stem, ext = os.path.splitext(base)
        bumped = output
        bump = 2
        while os.path.exists(bumped):
            bumped = os.path.join(out_dir, f"{stem}-{bump}{ext}")
            bump += 1
            if bump > 999:
                parser.error(
                    f"compile_promo: collision-bump exhausted for {base!r} "
                    f"in {out_dir!r} after 999 attempts; clear old runs "
                    "or pass --output explicitly."
                )
        if bumped != output:
            print(
                f"compile_promo: {base} already exists; bumping to "
                f"{os.path.basename(bumped)} to preserve prior deliverable.",
                flush=True,
            )
            output = bumped

    # BGM discovery: --bgm-dir takes precedence, then --bgm, then auto-discover.
    # Sprint 08.5: filter by target_duration_sec so we never pick BGM that ends
    # before the video. Sprint 09b C2 (H-1): _discover_bgm_files raises
    # NoSuitableBGMError (Sprint 09a M-005) when no track meets the minimum
    # duration; catch here and surface as a user-facing error rather than a
    # raw traceback.
    bgm_paths = None
    if args.bgm_dir:
        try:
            bgm_paths = _discover_bgm_files(
                args.bgm_dir,
                min_duration_sec=float(args.target_duration_sec),
            )
        except NoSuitableBGMError as exc:
            parser.error(
                f"No BGM in --bgm-dir '{args.bgm_dir}' meets the minimum "
                f"duration of {args.target_duration_sec:.1f}s: {exc}"
            )
        if not bgm_paths:
            parser.error(f"No .mp3 files found in --bgm-dir: {args.bgm_dir}")

    # Sprint 09b C4 (Codex #6): surface MimoAnalysisError as a user-facing
    # exit rather than a raw traceback. The error message names the
    # failing clip_id so the operator can diagnose which source file broke.
    try:
        ok = full_pipeline(
            poi_name=args.poi,
            location=args.location,
            output_path=output,
            voice_key=args.voice,
            bgm_path=args.bgm,
            bgm_paths=bgm_paths,
            skip_analysis=args.skip_analysis,
            backend=backend,
            target_duration_sec=args.target_duration_sec,
            n_variants=args.n_variants,
            script_candidates=args.script_candidates,
            tts_speed=args.tts_speed,
            seed=args.seed,
        )
    except MimoAnalysisError as exc:
        logger.error(
            "MiMo analysis failed for clip %s (%s). "
            "Retry the run or remove the offending clip from the pool. "
            "Cause: %s",
            exc.clip_id, exc.clip_path, exc.cause,
        )
        sys.exit(2)

    if ok:
        logger.info("Done. Open: %s", output)
    else:
        logger.error("Pipeline failed.")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
