"""Pre/post-generation validation gates for Gemini #1 scripts.

Extracted from ``script_generator.py`` (Sprint S2c, commit 3/4).

Two raise-or-pass gates:

- :func:`enforce_clip_pool_contract` — pre-generation pool-size check
  using ``format_profiles.get_clip_pool_messages``. Raises
  ``RuntimeError`` on hard errors; warnings are logged through.
- :func:`enforce_pacing_gate` — post-generation pacing check that wraps
  ``script_validator.validate_pacing``. LONG-mode pacing warnings are
  promoted to ``ValidationError`` (the per-attempt loop in the entry
  points catches and re-rolls); SHORT-mode warnings are observational
  only.

The pacing gate keeps a lazy import of ``script_validator`` to avoid
the circular dependency the entry points already manage at the
``script_generator``/``script_validator`` boundary.
"""

from __future__ import annotations

import logging

from promo.core.format_profiles import PromoFormatProfile, get_clip_pool_messages
from promo.core.schema import NarratorPersona

logger = logging.getLogger(__name__)


def enforce_clip_pool_contract(
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


def enforce_pacing_gate(
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
