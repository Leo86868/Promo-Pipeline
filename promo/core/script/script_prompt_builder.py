"""Gemini #1 prompt assembly — pure transformation, no I/O.

Extracted from ``script_generator.py`` (Sprint S2c, commit 1/4). All
inputs are dicts/dataclasses; the only output is a fully-rendered prompt
string. No Gemini calls, no file reads beyond the arsenal MD templates
that ``arsenal_loader`` already memoizes.

Module contents:
  - :data:`HOOK_TECHNIQUES` — hook seed strings rotated across variants.
  - :data:`_DEFAULT_PERSONA_PATH` — bundled persona fallback (kept under
    the legacy underscore name; re-exported by the facade for tests that
    import it directly).
  - :func:`build_variant_plans` — per-variant diversity plans.
  - :func:`format_clip_inventory` — clip inventory block.
  - :func:`format_examples` — persona example formatter.
  - :func:`build_prompt` — full prompt assembly with arsenal MD templates.

The facade ``script_generator.py`` re-exports these under their
pre-S2c underscore-prefixed names so existing test patches and direct
imports targeting ``promo.core.script.script_generator.X`` keep working.
"""

from __future__ import annotations

import logging
import os
from string import Template
from typing import Any

from promo.core import arsenal_loader
from promo.core.format_profiles import PromoFormatProfile
from promo.core.schema import ClipMetadata, NarratorPersona

logger = logging.getLogger(__name__)


# DESCRIPTION-block usage guardrail (poi_description bridge, 2026-06-22).
# The facts card is verified ground truth — feeding it kills fabrication
# (model stops inventing prices/years/counts). But handed raw, the model
# treats the 2700-char card as a checklist and recites amenities, which
# reads as a brochure and flattens the story (blind A/B: raw-fed scripts
# were flagged "list-y" 3/5 and scored worst on story feel). This sentence
# tells the model HOW to use the card: stay accurate, weave at most one
# fact, never list. Validated 3-way blind (same 5 POIs): with-guardrail
# kept 5/5 facts accurate AND recovered story feel to a near-tie with the
# no-card baseline, strictly beating raw-fed. Trailing blank line separates
# the instruction from the facts that follow it in the rendered block.
_DESCRIPTION_GUARDRAIL = (
    "(These are VERIFIED facts about this property, given ONLY so you stay "
    "accurate and never invent details like prices, dates, or counts. Do NOT "
    "list amenities or recite several facts — the 'one specific number or "
    "fact per script' rule still holds. Pick AT MOST ONE concrete detail that "
    "genuinely serves the story and weave it in naturally; treat everything "
    "else here as background to stay true to, not material to mention.)\n\n"
)


# V1-2: hook cards ({name, technique, must_not}); HOOK_TECHNIQUES keeps
# the historical name-list shape for rotation and consumers.
# REPRODUCIBILITY: rotation is seed % len(HOOK_CARDS) — editing the card
# list (add/remove/reorder) invalidates historical seed→card mappings,
# same rule as the music library.
HOOK_CARDS = arsenal_loader.load_script_hooks()
HOOK_TECHNIQUES = [card["name"] for card in HOOK_CARDS]
_HOOK_CARD_BY_NAME = {card["name"]: card for card in HOOK_CARDS}


def build_variant_plans(
    n_variants: int,
    clips_metadata: list[ClipMetadata],
    *,
    hook_seed: int | None = None,
) -> list[dict[str, str]]:
    """Build pre-assigned diversity plans for each variant.

    Each plan contains:
      - hook_technique: a seed string from HOOK_TECHNIQUES (rotated)
      - first_clip_id: a different clip ID for segment 1, clip 1 per variant

    ``hook_seed`` (P2 step 5) offsets the hook rotation per VIDEO.
    Production compiles each video independently with ``--n-variants 1``,
    so the variant ordinal is constant 1 and variant-only rotation deals
    the same card to every video; ``run_batch`` threads
    ``(base_seed or 0) + canonical_ordinal`` here (the music-rotation
    convention), so consecutive videos walk the deck deterministically.
    ``None`` (manual dev compile) keeps the legacy variant-only rotation
    starting at card 0.
    """
    hook_offset = int(hook_seed) % len(HOOK_TECHNIQUES) if hook_seed is not None else 0
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
            "hook_technique": HOOK_TECHNIQUES[(hook_offset + i) % len(HOOK_TECHNIQUES)],
            "first_clip_id": first_clip_pool[i % len(first_clip_pool)],
        })

    return plans


# ---------------------------------------------------------------------------
#  Persona default path
#
#  ``NarratorPersona`` lives in :mod:`promo.core.schema` (Sprint Arsenal
#  Externalization Commit 0). The facade re-exports it for consumers
#  that still import it from ``script_generator``.
# ---------------------------------------------------------------------------


_DEFAULT_PERSONA_PATH = os.path.join(
    os.path.dirname(__file__), '..', '..', 'arsenal', 'personas', 'third_person_promo.yaml'
)


# ---------------------------------------------------------------------------
#  Prompt builder
# ---------------------------------------------------------------------------

def format_clip_inventory(
    clips_metadata: list[ClipMetadata],
    *,
    duration_precision: int = 1,
) -> str:
    """Format clip metadata into a readable inventory for the prompt.

    Each line includes the clip's ``source_duration_sec`` so Gemini knows how
    much visual time a single clip can cover. If a narration phrase needs
    more visual time than a single clip's source, the script should split
    the phrase across multiple clips — the assign-stage hard-constraint enforcer
    (``clip_assignment_validator._enforce_hard_constraint_and_enrich``) is the actual
    gate that prevents a single phrase from outrunning its clip.

    Shared between the script prompt (script_generator, default precision=1) and
    the assign validator (clip_assignment_validator, passes ``duration_precision=2``
    for the hard-constraint math). Merged from a previously-duplicated pair in
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


def format_asset_visual_brief(asset_visual_brief: dict[str, Any]) -> str:
    """Format an Asset Visual Brief into Gemini #1 grounding text."""
    lines = [
        "ASSET VISUAL BRIEF",
        (
            f"Eligible visual pool: {asset_visual_brief.get('eligible_asset_count', 0)} clips, "
            f"{float(asset_visual_brief.get('eligible_total_seconds') or 0.0):.1f}s total."
        ),
        "",
        "Category coverage:",
    ]
    for row in asset_visual_brief.get("categories") or []:
        motifs = "; ".join(
            str(item.get("phrase") or "")
            for item in (row.get("coverage_motifs") or [])[:3]
            if item.get("phrase")
        )
        suffix = f" — {motifs}" if motifs else ""
        lines.append(
            f"- {row.get('category', 'unknown')}: "
            f"{row.get('asset_count', 0)} clips, "
            f"{float(row.get('total_seconds') or 0.0):.1f}s{suffix}"
        )

    core_visuals = asset_visual_brief.get("core_visuals") or []
    if core_visuals:
        lines.extend(["", "Core visual anchors:"])
        for item in core_visuals:
            phrase = item.get("phrase")
            if phrase:
                lines.append(f"- {phrase}")

    grounding_set = asset_visual_brief.get("grounding_set") or []
    if grounding_set:
        lines.extend(["", "Concrete visual grounding set:"])
        for item in grounding_set:
            detail = item.get("visual_detail")
            if not detail:
                continue
            role = item.get("coverage_role", "detail")
            category = item.get("category", "unknown")
            lines.append(f"- [{role}] {category}: {detail}")

    note = asset_visual_brief.get("summary_note")
    if note:
        lines.extend(["", f"Note: {note}"])
    return "\n".join(lines)


# P2 step 4 (范文法规): every format mode the persona serves must carry
# at least this many format-tagged examples. The model imitates examples
# over instruction numbers (2026-06-11 word-friction root cause: the
# only long example was 143 words — below the 150 floor — and the model
# wrote 143-word scripts no matter what the prompt said), so a missing
# or cross-format example set silently rewrites the card's personality.
MIN_EXAMPLES_PER_FORMAT = 2


def format_examples(persona: NarratorPersona, mode: str = "short") -> str:
    """Format example scripts from persona into prompt text.

    Filters examples by format tag matching the requested mode. A
    persona that carries examples but fewer than
    ``MIN_EXAMPLES_PER_FORMAT`` for the requested mode FAILS LOUDLY —
    the pre-P2 silent fallback (mix in other formats' examples) is
    exactly the trap that caused the word-friction accident: short
    examples pull long scripts under the floor.

    A persona with NO examples at all returns an empty block (a
    deliberate minimal persona, no conflicting signal to leak).

    Future home (预留之路): when per-type cards grow their own example
    sets, examples move OUT of the persona into the skeleton YAML and
    this filter becomes a plain card read — keep the ≥2 rule there.
    """
    if not persona.example_scripts:
        return ""
    matching = [ex for ex in persona.example_scripts if ex.get("format", "short") == mode]
    if len(matching) < MIN_EXAMPLES_PER_FORMAT:
        raise ValueError(
            f"persona {persona.id!r} has {len(matching)} example_scripts tagged "
            f"format={mode!r} but the {mode!r} card requires at least "
            f"{MIN_EXAMPLES_PER_FORMAT} (范文法规: a new type ships with 2-3 "
            "format-tagged examples; never silently borrow another format's)"
        )

    blocks = []
    for ex in matching:
        header = f"EXAMPLE ({ex['hotel']}, {ex['location']}):"
        segs = []
        for i, seg in enumerate(ex['segments'], 1):
            segs.append(f'{i}. "{seg}"')
        blocks.append(header + "\n" + "\n".join(segs))
    return "\n\n".join(blocks)


def build_prompt(
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
    asset_visual_brief: dict[str, Any] | None = None,
) -> str:
    """Build the full generation prompt.

    ``tighten_hint`` (vestigial): once injected a structured feedback
    block so Gemini #1 tightened a failing segment on retry. The F3-retry
    chain retired 2026-06-11 and no caller passes it now (see the
    ``feedback_block`` note in the body). Kept for signature stability.
    """

    clip_inventory = (
        format_asset_visual_brief(asset_visual_brief)
        if asset_visual_brief
        else format_clip_inventory(clips_metadata)
    )
    examples = format_examples(persona, mode=profile.mode)
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
        if variant_plan and variant_plan.get("first_clip_id"):
            # First-clip steering is a multi-variant diversity device only;
            # single-variant production ignores script clip suggestions
            # (the packer owns clip choice).
            variant_note += (
                f"- REQUIRED FIRST CLIP: Segment 1, clip 1 MUST be clip "
                f"{variant_plan['first_clip_id']}. "
                f"Write narration that matches what this clip shows.\n"
            )
    # 2026-06-12 fix: the dealt hook card must reach the prompt in EVERY
    # compile shape. Production runs --n-variants 1 (one video per compile,
    # per-video rotation via --hook-seed), and the previous placement inside
    # the n_variants > 1 block silently dropped the card from every
    # production prompt — the dealer rotated cards nobody ever saw.
    if variant_plan and variant_plan.get("hook_technique"):
        hook = variant_plan["hook_technique"]
        variant_note += (
            f"\nHOOK ASSIGNMENT:\n"
            f"- REQUIRED HOOK TECHNIQUE: Use a \"{hook}\" hook for segment 1. "
            f"This is your assigned technique — do not use a different one.\n"
        )
        card = _HOOK_CARD_BY_NAME.get(hook)
        if card:
            variant_note += (
                f"- TECHNIQUE: {card['technique']}\n"
                f"- MUST NOT: {card['must_not']}\n"
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

    # F3 script-regen retired 2026-06-11 with the LLM-assigner chain; no
    # caller passes tighten_hint any more (param kept for signature
    # stability until the next contract bump).
    feedback_block = ""

    # Sprint Arsenal Externalization (Commit 3): caller pre-renders the
    # 2 conditional description blocks so the MD template can stay flat
    # `$identifier` substitution (no f-string-style ternaries in the
    # template body).
    hotel_description_block = (
        f"DESCRIPTION: {_DESCRIPTION_GUARDRAIL}{hotel_description}"
        if hotel_description else ""
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
