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
"""

import json
import logging
import os
import re
from string import Template
from typing import Optional

from promo.core import arsenal_loader
from promo.core.llm.gemini_client import GeminiModel, resolve_gemini_model
from promo.core.llm.retry import retry_with_backoff
from promo.core.llm.json_response import parse_json_response
from promo.core.format_profiles import (
    PromoFormatProfile,
    get_clip_pool_messages,
    get_promo_format_profile,
)
from promo.core.schema import ClipMetadata, NarratorPersona, Script
from promo.arsenal.personas._loader import load_persona

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Hook technique seeds for variant diversity
# ---------------------------------------------------------------------------

HOOK_TECHNIQUES = [
    "contradiction",
    "sensory",
    "specific_number",
    "second_person",
    "time_anchor",
    "superlative",
]


def _build_variant_plans(
    n_variants: int,
    clips_metadata: list[ClipMetadata],
) -> list[dict[str, str]]:
    """Build pre-assigned diversity plans for each variant.

    Each plan contains:
      - hook_technique: a seed string from HOOK_TECHNIQUES (rotated)
      - first_clip_id: a different clip ID for segment 1, clip 1 per variant
    """
    plans = []
    # Pick distinct first clips from different categories for maximum diversity
    clip_ids_by_category: dict[str, list[str]] = {}
    for clip in clips_metadata:
        cat = clip.get("category", "unknown")
        clip_ids_by_category.setdefault(cat, []).append(clip.get("id", "0000"))

    # Build a diverse first-clip pool: one from each category, then extras
    first_clip_pool = []
    for cat in ("exterior", "scenic", "aerial", "pool", "room", "lobby",
                "restaurant", "food", "spa", "activity", "unknown"):
        ids = clip_ids_by_category.get(cat, [])
        for cid in ids:
            if cid not in first_clip_pool:
                first_clip_pool.append(cid)
            if len(first_clip_pool) >= n_variants:
                break
        if len(first_clip_pool) >= n_variants:
            break

    # If not enough categories, pad from all clips
    if len(first_clip_pool) < n_variants:
        all_ids = sorted({c.get("id", "0000") for c in clips_metadata})
        for cid in all_ids:
            if cid not in first_clip_pool:
                first_clip_pool.append(cid)
            if len(first_clip_pool) >= n_variants:
                break

    for i in range(n_variants):
        plans.append({
            "hook_technique": HOOK_TECHNIQUES[i % len(HOOK_TECHNIQUES)],
            "first_clip_id": first_clip_pool[i % len(first_clip_pool)],
        })

    return plans


# ---------------------------------------------------------------------------
#  Persona
#
#  ``NarratorPersona`` moved to :mod:`promo.core.schema` in Sprint Arsenal
#  Externalization (Commit 0); re-exported above so consumers that import
#  it from this module keep working.
# ---------------------------------------------------------------------------


_DEFAULT_PERSONA_PATH = os.path.join(
    os.path.dirname(__file__), '..', '..', 'arsenal', 'personas', 'third_person_promo.yaml'
)


# ---------------------------------------------------------------------------
#  Prompt builder
# ---------------------------------------------------------------------------

def _format_clip_inventory(
    clips_metadata: list[ClipMetadata],
    *,
    duration_precision: int = 1,
) -> str:
    """Format clip metadata into a readable inventory for the prompt.

    Each line includes the clip's ``source_duration_sec`` so Gemini knows how
    much visual time a single clip can cover. If a narration phrase needs
    more visual time than a single clip's source, the script should split
    the phrase across multiple clips — the Gemini #2 hard-constraint enforcer
    (``clip_assigner._enforce_hard_constraint_and_enrich``) is the actual
    gate that prevents a single phrase from outrunning its clip.

    Shared between Gemini #1 (script_generator, default precision=1) and
    Gemini #2 (clip_assigner, passes ``duration_precision=2`` for the
    hard-constraint math). Merged from a previously-duplicated pair in
    Sprint 13 AC11.
    """
    lines = []
    for i, clip in enumerate(clips_metadata, 1):
        clip_id = clip.get("id", f"{i:04d}")
        cat = clip.get("category", "unknown")
        desc = clip.get("scene_description", "no description")
        shot = clip.get("shot_size", "")
        subject = clip.get("main_subject", "")
        src_dur = clip.get("source_duration_sec")
        parts = [f"Clip {clip_id}: [{cat}]"]
        if src_dur is not None:
            parts.append(f"{float(src_dur):.{duration_precision}f}s")
        if desc:
            parts.append(desc)
        if subject:
            parts.append(f"(subject: {subject})")
        if shot:
            parts.append(f"[{shot}]")
        lines.append(" — ".join(parts))
    return "\n".join(lines)


def _format_examples(persona: NarratorPersona, mode: str = "short") -> str:
    """Format example scripts from persona into prompt text.

    Filters examples by format tag matching the requested mode.
    Falls back to all examples if no matching examples exist.
    """
    matching = [ex for ex in persona.example_scripts if ex.get("format", "short") == mode]
    if not matching:
        logger.warning("No examples with format=%s found, using all examples as fallback", mode)
        matching = persona.example_scripts

    blocks = []
    for ex in matching:
        header = f"EXAMPLE ({ex['hotel']}, {ex['location']}):"
        segs = []
        for i, seg in enumerate(ex['segments'], 1):
            segs.append(f'{i}. "{seg}"')
        blocks.append(header + "\n" + "\n".join(segs))
    return "\n\n".join(blocks)


def _build_prompt(
    poi_name: str,
    location: str,
    clips_metadata: list[ClipMetadata],
    persona: NarratorPersona,
    profile: PromoFormatProfile,
    hotel_description: str = "",
    notable_details: str = "",
    variant_index: int = 1,
    n_variants: int = 1,
    variant_plan: dict | None = None,
    tighten_hint: str = "",
) -> str:
    """Build the full generation prompt.

    ``tighten_hint`` (Sprint 10 C2 F3 policy): when non-empty, injects a
    structured feedback block at the top of the prompt so Gemini #1
    tightens the failing segment on its single retry. See
    ``promo/core/clip_assigner.py`` for how the hint is constructed.
    """

    clip_inventory = _format_clip_inventory(clips_metadata)
    examples = _format_examples(persona, mode=profile.mode)
    banned = ", ".join(persona.forbidden_phrases)
    system_prompt = persona.system_prompt.format(
        duration_label=profile.duration_label,
    )
    # Sprint Arsenal Externalization (Commit 6c): the LONG-mode
    # `system_prompt.replace("5-12 words max per sentence", ...)` call
    # that lived here is gone. Persona is now mode-agnostic — the
    # `5-12 words max per sentence` line was the only one that ever
    # required surgery on persona text, and Commit 6c retired it
    # alongside 3 other format/segment-specific bullets. Per-mode
    # cadence guidance now flows in through `profile.sentence_rule`
    # (skeleton-owned, see `arsenal/script_skeletons/{short,long}*.yaml`).
    segment_lines = []
    for idx, seg in enumerate(profile.segment_plans, 1):
        category_hint = ""
        if seg.preferred_categories:
            category_hint = f" Prefer clips tagged {', '.join(seg.preferred_categories)}."
        segment_lines.append(
            f"- Segment {idx} ({seg.label}, ~{seg.approx_words} words, {seg.clip_range_display}): {seg.guidance}{category_hint}"
        )
    segment_structure = "\n".join(segment_lines)
    variant_note = ""
    if n_variants > 1:
        variant_note = (
            f"\nVARIANT MODE:\n"
            f"- You are writing variant {variant_index} of {n_variants}.\n"
            f"- Keep the same hotel and clip inventory, but change the hook, sentence choices, "
            f"and clip usage enough that this feels like a genuinely different edit.\n"
        )
        if variant_plan:
            hook = variant_plan.get("hook_technique", "")
            first_clip = variant_plan.get("first_clip_id", "")
            if hook:
                variant_note += (
                    f"- REQUIRED HOOK TECHNIQUE: Use a \"{hook}\" hook for segment 1. "
                    f"This is your assigned technique — do not use a different one.\n"
                )
            if first_clip:
                variant_note += (
                    f"- REQUIRED FIRST CLIP: Segment 1, clip 1 MUST be clip {first_clip}. "
                    f"Write narration that matches what this clip shows.\n"
                )

    # Sprint Arsenal Externalization (Commit 6b): per-mode RULES content
    # comes from the skeleton YAML, not from inline `if profile.mode ==
    # "long":` conditionals. `profile.sentence_rule` carries the cadence
    # bullet (LONG widens to 5-18 words; SHORT keeps 5-12); `profile.extra_rules`
    # carries the additional bullets joined into `$extra_rules_block`
    # (LONG ships the "Minimum 130 words" floor; SHORT ships an empty
    # tuple). Adding a third format = add a YAML, no Python edit.
    sentence_rule = profile.sentence_rule
    extra_rules_block = (
        "\n- " + "\n- ".join(profile.extra_rules)
        if profile.extra_rules else ""
    )

    # Pause-authoring guidance — drives pause budget math downstream in code.
    pause_block = ""
    if persona.pause_guidelines:
        pause_block = (
            "\n\nPAUSE AUTHORING (pause_weight per segment):\n"
            f"{persona.pause_guidelines.strip()}\n"
        )

    target_word_midpoint = (profile.total_words_min + profile.total_words_max) // 2

    # Sprint Arsenal Externalization (Commit 3): the F3 retry feedback
    # block is loaded from `arsenal/system_prompts/gemini1_f3_retry_v1.md`.
    # The MD template's literal body is everything between the leading
    # `\n\n` and the trailing `\n` that the caller (this code) puts back —
    # `arsenal_loader.load_system_prompt(...)` returns the rstripped form,
    # so we restore the trailing newline here to preserve the byte-level
    # output of the previous inline implementation.
    feedback_block = ""
    if tighten_hint:
        feedback_template = Template(arsenal_loader.load_system_prompt("gemini1_f3_retry"))
        feedback_block = feedback_template.substitute(
            tighten_hint=tighten_hint.strip(),
        ) + "\n"

    # Sprint Arsenal Externalization (Commit 3): caller pre-renders the
    # 2 conditional description blocks so the MD template can stay flat
    # `$identifier` substitution (no f-string-style ternaries in the
    # template body).
    hotel_description_block = (
        f"DESCRIPTION: {hotel_description}" if hotel_description else ""
    )
    notable_details_block = (
        f"NOTABLE DETAILS: {notable_details}" if notable_details else ""
    )

    template = Template(arsenal_loader.load_system_prompt("gemini1_script"))
    prompt = template.substitute(
        system_prompt=system_prompt,
        feedback_block=feedback_block,
        poi_name=poi_name,
        location=location,
        hotel_description_block=hotel_description_block,
        notable_details_block=notable_details_block,
        segment_count=profile.segment_count,
        target_word_midpoint=target_word_midpoint,
        segment_structure=segment_structure,
        sentence_rule=sentence_rule,
        extra_rules_block=extra_rules_block,
        banned_phrases=banned,
        variant_note=variant_note,
        pause_block=pause_block,
        examples=examples,
        clip_inventory=clip_inventory,
    )
    return prompt


# ---------------------------------------------------------------------------
#  Core generation
# ---------------------------------------------------------------------------

def _generate_one(
    prompt: str, model: GeminiModel
) -> Optional[dict]:
    """Generate a single script candidate. Returns parsed dict or None."""
    def _call():
        response = model.generate_content(
            prompt,
            generation_config={
                "temperature": 0.85,
                "top_p": 0.9,
                # Sprint 08.5: bumped 1500→10000 per operator directive. Token
                # cost is not a constraint on this repo (memory:
                # feedback_gemini_token_budget). Headroom covers 130-140 word
                # scripts + the fuller clips[] arrays without truncation risk.
                "max_output_tokens": 10000,
            },
        )
        return parse_json_response(response.text)

    try:
        return retry_with_backoff(_call, max_retries=2, base_delay=2.0)
    except Exception as exc:
        logger.warning("Script generation failed: %s", exc)
        return None


def _enforce_clip_pool_contract(
    available_unique_clips: int,
    profile: PromoFormatProfile,
    *,
    context_label: str,
) -> None:
    errors, warnings = get_clip_pool_messages(available_unique_clips, profile)
    for warning in warnings:
        logger.warning("%s: %s", context_label, warning)
    if errors:
        raise RuntimeError(f"{context_label}: {errors[0]}")


def _enforce_pacing_gate(
    script: dict,
    persona: NarratorPersona,
    profile: PromoFormatProfile,
) -> None:
    from promo.core.script.script_validator import ValidationError, validate_pacing

    warnings = validate_pacing(
        script,
        target_duration=profile.target_duration_sec,
        wpm=persona.wpm,
        profile=profile,
    )
    if profile.mode == "long" and warnings:
        raise ValidationError("pacing validation failed: " + "; ".join(warnings))


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
