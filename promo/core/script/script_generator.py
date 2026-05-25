"""Narration script generator for promotional hotel videos.

Generates a 4-segment narration script using Gemini Flash, driven by the
available clip inventory ("script follows clips" approach). Produces
best-of-N candidates, validates each, and returns the highest-scoring one.

Usage:
    from promo.core.script.script_generator import generate_script_variants

    variants = generate_script_variants(
        poi_name="Amangiri",
        location="Canyon Point, Utah",
        clips_metadata=[
            {"id": "0001", "category": "scenic", "scene_description": "..."},
            ...
        ],
        n_variants=1,
    )
    script = variants[0]

Module layout (Sprint S2c — script_generator.py split from 779 → ~480 LOC)
--------------------------------------------------------------------------

This module is the public facade for the script-generation surface. The
three sibling modules under ``promo/core/script/`` own the implementation:

- :mod:`promo.core.script.script_prompt_builder` — pure transformation:
  ``HOOK_TECHNIQUES``, ``_DEFAULT_PERSONA_PATH``, ``build_variant_plans``,
  ``format_clip_inventory`` (also called from
  ``promo.core.assign.clip_assignment_gemini``), ``format_examples``,
  ``build_prompt``.
- :mod:`promo.core.script.script_gemini_caller` — single Gemini #1 call
  wrapper (``generate_one``) with retry/backoff + JSON parse.
- :mod:`promo.core.script.script_validation_gates` — pre/post-generation
  raise-or-pass gates (``enforce_clip_pool_contract``,
  ``enforce_pacing_gate``).

:func:`generate_script_variants`, :func:`regenerate_single_variant_with_hint`,
the ``NarratorPersona`` re-export, and the legacy underscore-prefixed
aliases for every extracted helper physically live here because they
form the orchestration + monkeypatch surface that tests and
``pipeline/steps.py`` target. Moving the orchestrators across files
would break the in-module bare-name resolution that makes
``unittest.mock.patch("promo.core.script.script_generator._generate_one",
...)`` and ``..._build_prompt``/``..._enforce_pacing_gate``/
``...resolve_gemini_model`` take effect without per-call indirection.
The script-validator boundary (``script_validator.py``) stays distinct
and is not part of this split.
"""

import json
import logging
import re

from promo.core.model_adapters.gemini import resolve_gemini_model
from promo.core.format_profiles import (
    PromoFormatProfile,
    get_promo_format_profile,
)
from promo.core.schema import ClipMetadata, NarratorPersona, Script
from promo.arsenal.personas._loader import load_persona

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Back-compat re-exports — extracted helpers (Sprint S2c)
# ---------------------------------------------------------------------------
# Tests + cross-module callers import these names directly from
# ``script_generator`` (e.g. ``from promo.core.script.script_generator
# import _build_prompt``) and patch them via
# ``unittest.mock.patch("promo.core.script.script_generator._generate_one",
# ...)``. The implementations live in sibling modules under
# ``promo.core.script.``; the legacy underscore-prefixed names are
# re-bound here so:
#   1. existing test patches keep targeting the facade's globals;
#   2. the entry-point orchestrators (``generate_script_variants`` +
#      ``regenerate_single_variant_with_hint``, both still in this file)
#      resolve the symbols through THIS module's globals via bare-name
#      lookup, so a monkeypatch on ``script_generator._build_prompt``
#      reaches them.
# Both the public (``build_prompt``) and legacy underscore
# (``_build_prompt``) names are re-exported so existing imports under
# either spelling continue to work.
from promo.core.script.script_prompt_builder import (  # noqa: E402
    HOOK_TECHNIQUES,
    _DEFAULT_PERSONA_PATH,
    build_prompt,
    build_prompt as _build_prompt,
    build_variant_plans,
    build_variant_plans as _build_variant_plans,
    format_clip_inventory,
    format_clip_inventory as _format_clip_inventory,
    format_examples,
    format_examples as _format_examples,
)
from promo.core.script.script_gemini_caller import (  # noqa: E402
    generate_one,
    generate_one as _generate_one,
)
from promo.core.script.script_validation_gates import (  # noqa: E402
    enforce_clip_pool_contract,
    enforce_clip_pool_contract as _enforce_clip_pool_contract,
    enforce_pacing_gate,
    enforce_pacing_gate as _enforce_pacing_gate,
)


# ---------------------------------------------------------------------------
#  Variant orchestration
# ---------------------------------------------------------------------------

def generate_script_variants(
    poi_name: str,
    location: str,
    clips_metadata: list[ClipMetadata],
    persona_path: str = None,
    hotel_description: str = "",
    notable_details: str = "",
    n_variants: int = 1,
    n_candidates: int = 1,
    max_retries: int = 4,
    target_duration_sec: float | int | None = None,
    *,
    profile: PromoFormatProfile | None = None,
    profiles: list[PromoFormatProfile] | None = None,
    persona: NarratorPersona | None = None,
    personas: list[NarratorPersona] | None = None,
) -> list[Script]:
    """Generate one or more script variants over a shared analyzed clip pool.

    Sprint 16 — ``profiles`` / ``personas`` (plural, keyword-only) let
    callers thread per-variant format profile and persona from the new
    :mod:`promo.core.selection` seams. ``profile`` / ``persona``
    (singular) pin a shared choice across every variant. When both are
    omitted the function falls back to the pre-Sprint-16 behaviour:
    ``get_promo_format_profile(target_duration_sec)`` for the profile
    and ``load_persona(persona_path)`` for the persona.
    """
    from promo.core.script.script_validator import normalize_script, validate_script_only, ValidationError

    # Per-variant profile resolution. ``profiles`` wins; otherwise the
    # singular ``profile`` fans out across variants; otherwise resolve
    # from the caller's ``target_duration_sec`` scalar (legacy path).
    if profiles is not None:
        if len(profiles) != n_variants:
            raise ValueError(
                f"profiles list length {len(profiles)} must equal n_variants {n_variants}"
            )
        profile_list: list[PromoFormatProfile] = list(profiles)
        per_variant_profile_mode = True
    elif profile is not None:
        profile_list = [profile] * n_variants
        per_variant_profile_mode = False
    else:
        profile_list = [get_promo_format_profile(target_duration_sec)] * n_variants
        per_variant_profile_mode = False

    # Per-variant persona resolution. Mirrors the profile trio; when all
    # three are omitted, the bundled ``third_person_promo.yaml`` loads.
    if personas is not None:
        if len(personas) != n_variants:
            raise ValueError(
                f"personas list length {len(personas)} must equal n_variants {n_variants}"
            )
        persona_list: list[NarratorPersona] = list(personas)
    elif persona is not None:
        persona_list = [persona] * n_variants
    else:
        if persona_path is None:
            persona_path = _DEFAULT_PERSONA_PATH
        loaded_persona = load_persona(persona_path)
        persona_list = [loaded_persona] * n_variants

    # Pool contract — check against every distinct profile, so a mixed
    # short/long variant pack fails fast if either mode's floor is unmet.
    clip_ids = {c.get("id", f"{i:04d}") for i, c in enumerate(clips_metadata, 1)}
    distinct_profiles: dict[str, PromoFormatProfile] = {}
    for p in profile_list:
        distinct_profiles.setdefault(p.mode, p)
    for p in distinct_profiles.values():
        _enforce_clip_pool_contract(
            len(clip_ids),
            p,
            context_label=f"Variant generation for '{poi_name}' ({p.mode} profile)",
        )

    model = resolve_gemini_model(log_context="Gemini #1")
    accepted: list[Script] = []
    # seen_texts stays shared across variants: cross-variant de-duplication
    # (no two variants may emit identical narration) is part of the
    # variant-pack contract and orthogonal to per-variant retry budgeting.
    seen_texts: set[str] = set()
    per_variant_budget = max(n_candidates, 1) * (1 + max_retries)

    # Build diversity plans for each variant
    variant_plans = _build_variant_plans(n_variants, clips_metadata)
    for i, plan in enumerate(variant_plans):
        logger.info(
            "VariantPlan %d/%d: hook=%s, first_clip=%s",
            i + 1, n_variants, plan["hook_technique"], plan["first_clip_id"],
        )

    # Sprint 09a H-002: each variant gets its own independent retry budget.
    # Pre-09a's shared `total_attempts` let early-variant failures starve
    # later variants — observed under transient model flakiness.
    for variant_index in range(1, n_variants + 1):
        plan = variant_plans[variant_index - 1] if variant_index <= len(variant_plans) else None
        current_profile = profile_list[variant_index - 1]
        current_persona = persona_list[variant_index - 1]
        variant_accepted = False
        for attempt_in_variant in range(1, per_variant_budget + 1):
            prompt = _build_prompt(
                poi_name,
                location,
                clips_metadata,
                current_persona,
                current_profile,
                hotel_description,
                notable_details,
                variant_index=variant_index,
                n_variants=n_variants,
                variant_plan=plan,
            )
            raw = _generate_one(prompt, model)
            if raw is None:
                logger.warning(
                    "Variant %d/%d attempt %d/%d: generation failed",
                    variant_index, n_variants, attempt_in_variant, per_variant_budget,
                )
                continue

            try:
                normalize_script(raw, profile=current_profile, persona=current_persona)
                validate_script_only(raw, current_persona, profile=current_profile)
                _enforce_pacing_gate(raw, current_persona, current_profile)
            except ValidationError as exc:
                logger.warning(
                    "Variant %d/%d attempt %d/%d: validation failed: %s",
                    variant_index, n_variants, attempt_in_variant, per_variant_budget, exc,
                )
                continue

            full_text = " ".join(seg.get("text", "").strip() for seg in raw.get("segments", []))
            normalized = re.sub(r"\s+", " ", full_text).strip().lower()
            if normalized in seen_texts:
                logger.warning(
                    "Variant %d/%d attempt %d/%d: duplicate script text, retrying",
                    variant_index, n_variants, attempt_in_variant, per_variant_budget,
                )
                continue
            seen_texts.add(normalized)

            # Sprint 16 BUG-1 — sidecar target_duration_sec reflects the
            # caller-provided value, not the profile's canonical 30/65.
            # When the caller threads per-variant ``profiles``, each
            # profile's own target drives its variant; otherwise the
            # scalar ``target_duration_sec`` wins if given; otherwise
            # fall back to the (single) profile's canonical target.
            if per_variant_profile_mode:
                variant_sidecar_duration: float | int = current_profile.target_duration_sec
            elif target_duration_sec is not None:
                variant_sidecar_duration = target_duration_sec
            else:
                variant_sidecar_duration = current_profile.target_duration_sec

            result = {
                **raw,
                "persona_id": current_persona.id,
                "poi_name": poi_name,
                "location": location,
                "target_duration_sec": variant_sidecar_duration,
                "format_mode": current_profile.mode,
                "variant_index": variant_index,
            }
            accepted.append(result)
            variant_accepted = True
            logger.info(
                "Variant %d/%d accepted for '%s': %d words, %d clips (attempt %d/%d)",
                variant_index,
                n_variants,
                poi_name,
                raw.get("total_words", 0),
                raw.get("total_clips", 0),
                attempt_in_variant, per_variant_budget,
            )
            break

        if not variant_accepted:
            raise RuntimeError(
                f"Variant {variant_index}/{n_variants} for '{poi_name}' "
                f"exhausted its per-variant budget of {per_variant_budget} "
                f"attempts without producing a valid script"
            )

    if len(accepted) != n_variants:
        # Defensive: the per-variant raise above should make this unreachable.
        raise RuntimeError(
            f"Requested {n_variants} variants for '{poi_name}', "
            f"but only {len(accepted)} were accepted"
        )
    return accepted


# ---------------------------------------------------------------------------
#  F3 regen — single-variant retry with tighten hint
# ---------------------------------------------------------------------------

def regenerate_single_variant_with_hint(
    *,
    poi_name: str,
    location: str,
    clips_metadata: list[ClipMetadata],
    persona_path: str | None = None,
    hotel_description: str = "",
    notable_details: str = "",
    variant_index: int,
    n_variants: int,
    variant_plan: dict | None = None,
    tighten_hint: str,
    n_candidates: int = 1,
    max_retries: int = 4,
    target_duration_sec: float | int | None = None,
    profile: PromoFormatProfile | None = None,
    persona: NarratorPersona | None = None,
) -> Script:
    """Regenerate one variant's script with a Sprint 10 F3 "tighten segment X"
    hint injected into the Gemini #1 prompt.

    Runs the same gate chain as :func:`generate_script_variants`
    (``normalize_script`` → ``validate_script_only`` → pacing gate) with a
    per-call attempt budget of ``n_candidates * (1 + max_retries)``.
    Raises :class:`RuntimeError` if every attempt fails validation.

    Unlike :func:`generate_script_variants` this is scoped to a single
    variant's regeneration path inside the F3 retry at
    ``compile_promo.full_pipeline``.
    The variant_plan (hook technique / first clip) stays the same — only
    the tighten hint is new.
    """
    from promo.core.script.script_validator import (
        ValidationError,
        normalize_script,
        validate_script_only,
    )

    # Sprint 16: per-variant profile / persona come in from the F3 caller
    # (full_pipeline) so the regen uses the SAME format + persona the
    # original variant used. Legacy callers that don't thread them fall
    # back to the pre-Sprint-16 path.
    caller_supplied_profile = profile is not None
    if persona is None:
        if persona_path is None:
            persona_path = _DEFAULT_PERSONA_PATH
        persona = load_persona(persona_path)
    if profile is None:
        profile = get_promo_format_profile(target_duration_sec)

    clip_ids = {c.get("id", f"{i:04d}") for i, c in enumerate(clips_metadata, 1)}
    _enforce_clip_pool_contract(
        len(clip_ids),
        profile,
        context_label=f"F3 regen for '{poi_name}' variant {variant_index}",
    )

    model = resolve_gemini_model(
        log_context=f"Gemini #1 F3 regen (variant {variant_index}, hint={tighten_hint[:100]!r})",
    )
    prompt = _build_prompt(
        poi_name=poi_name,
        location=location,
        clips_metadata=clips_metadata,
        persona=persona,
        profile=profile,
        hotel_description=hotel_description,
        notable_details=notable_details,
        variant_index=variant_index,
        n_variants=n_variants,
        variant_plan=variant_plan,
        tighten_hint=tighten_hint,
    )

    attempts = max(n_candidates, 1) * (1 + max(max_retries, 0))
    for attempt in range(1, attempts + 1):
        raw = _generate_one(prompt, model)
        if raw is None:
            logger.warning(
                "F3 regen variant %d attempt %d/%d: generation failed",
                variant_index, attempt, attempts,
            )
            continue
        try:
            normalize_script(raw, profile=profile, persona=persona)
            validate_script_only(raw, persona, profile=profile)
            _enforce_pacing_gate(raw, persona, profile)
        except ValidationError as exc:
            logger.warning(
                "F3 regen variant %d attempt %d/%d: validation failed: %s",
                variant_index, attempt, attempts, exc,
            )
            continue
        # Sprint 16 BUG-1 — sidecar reflects caller-provided duration.
        # Priority is "scalar wins when given" so the F3 path (which
        # always passes the per-variant scalar `target_duration_sec`
        # alongside `profile`) records the operator's intended duration
        # for that variant, matching the original variant's sidecar.
        # The first branch is reserved for callers that pass `profile`
        # without any scalar (no current call site does this — kept as
        # the explicit no-scalar fallback so future selectors that emit
        # only profiles route correctly).
        if target_duration_sec is not None:
            regen_sidecar_duration: float | int = target_duration_sec
        elif caller_supplied_profile:
            regen_sidecar_duration = profile.target_duration_sec
        else:
            regen_sidecar_duration = profile.target_duration_sec
        result: Script = {
            **raw,
            "persona_id": persona.id,
            "poi_name": poi_name,
            "location": location,
            "target_duration_sec": regen_sidecar_duration,
            "format_mode": profile.mode,
            "variant_index": variant_index,
        }
        return result

    raise RuntimeError(
        f"F3 regen exhausted {attempts} attempts without producing a valid "
        f"script for variant {variant_index} of '{poi_name}'"
    )


# ---------------------------------------------------------------------------
#  CLI for standalone testing
# ---------------------------------------------------------------------------

def main():
    """CLI entry point for testing script generation."""
    import argparse
    from dotenv import load_dotenv
    load_dotenv()

    from promo.core.logging_config import configure_logging
    configure_logging()

    parser = argparse.ArgumentParser(description="Generate a hotel narration script")
    parser.add_argument("--poi", required=True, help="POI name")
    parser.add_argument("--location", default="", help="City, State/Country")
    parser.add_argument("--clips-json", help="Path to clips_metadata JSON file")
    parser.add_argument("--description", default="", help="Hotel description")
    parser.add_argument("--details", default="", help="Notable details")
    parser.add_argument("--candidates", type=int, default=3, help="Number of candidates")
    parser.add_argument("--variants", type=int, default=1, help="Number of script variants")
    parser.add_argument("--target-duration-sec", type=float, default=30.0, help="Target promo duration")
    parser.add_argument("--persona", default=None, help="Path to persona YAML")
    args = parser.parse_args()

    # Load clips metadata
    if args.clips_json:
        with open(args.clips_json) as f:
            clips = json.load(f)
            # Handle both list format and dict-keyed format
            if isinstance(clips, dict):
                clips = [{"id": k, **v} for k, v in clips.items()]
    else:
        # Default test clips if no file provided
        clips = [
            {"id": "0001", "category": "exterior", "scene_description": "hotel exterior at golden hour"},
            {"id": "0002", "category": "scenic", "scene_description": "panoramic mountain view"},
            {"id": "0003", "category": "pool", "scene_description": "infinity pool at sunset"},
            {"id": "0004", "category": "room", "scene_description": "luxury suite interior"},
        ]
        logger.warning("No --clips-json provided, using default test clips")

    results = generate_script_variants(
        poi_name=args.poi,
        location=args.location,
        clips_metadata=clips,
        persona_path=args.persona,
        hotel_description=args.description,
        notable_details=args.details,
        n_variants=args.variants,
        n_candidates=args.candidates,
        target_duration_sec=args.target_duration_sec,
    )

    for result in results:
        print("\n" + "=" * 60)
        print(f"NARRATION SCRIPT V{result['variant_index']}: {result['poi_name']}")
        print(f"Location: {result['location']}")
        print(f"Persona: {result['persona_id']}")
        print(f"Format: {result['format_mode']} ({result['target_duration_sec']}s)")
        print(f"Hook: {result.get('hook_technique', '?')}")
        print(f"Unique detail: {result.get('unique_detail', '?')}")
        print("=" * 60)
        total_clips = 0
        for seg in result["segments"]:
            clips_info = seg.get("clips", [])
            clip_ids = [c["clip_id"] for c in clips_info]
            total_clips += len(clip_ids)
            print(f"\n  [{seg['segment']}] ({len(clip_ids)} clips: {', '.join(clip_ids)}) ({seg['word_count']}w)")
            print(f"  \"{seg['text']}\"")
            for c in clips_info:
                cut = c.get("cut_after", "")
                label = f"cut after \"{cut}\"" if cut else "(holds to end)"
                print(f"    -> clip {c['clip_id']} {label}")
        print(f"\n  Total: {result['total_words']} words, {total_clips} clips")
        print("=" * 60)
        print("\nFull JSON output:")
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
