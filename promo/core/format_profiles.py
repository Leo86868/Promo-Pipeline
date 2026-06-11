"""Promo format profiles for short- and long-form outputs.

Single source of truth for target-duration-driven script structure.
"""

from __future__ import annotations

# Sprint Arsenal Externalization (Commit 0 + Commit 6a): the dataclass
# definitions moved to :mod:`promo.core.schema`; the literal
# ``SHORT_PROFILE`` / ``LONG_PROFILE`` / ``FORMAT_TEMPLATES`` bodies
# moved to YAML files under ``promo/arsenal/script_skeletons/``. The
# Python symbols stay here as re-exports populated at module-import
# time via ``arsenal_loader.load_format_templates()``.
from promo.core import arsenal_loader
from promo.core.schema import PromoFormatProfile, SegmentPlan

__all__ = [
    "SegmentPlan",
    "PromoFormatProfile",
    "SHORT_PROFILE",
    "LONG_PROFILE",
    "FORMAT_TEMPLATES",
    "get_promo_format_profile",
    "get_clip_pool_messages",
]


FORMAT_TEMPLATES: dict[str, PromoFormatProfile] = arsenal_loader.load_format_templates()
"""Discoverable registry of every promo format template (Sprint 16).

`RandomFormatSelector` samples from this dict, and `architecture.md`
"Selector seams" names it as the cold-reader entry point for
"what format templates exist?". To add a third template (e.g. a 90s
long-form), drop a new ``*.yaml`` file under
``promo/arsenal/script_skeletons/`` and the loader picks it up
automatically. Keys are the YAML's ``mode`` field; selectors that need
a deterministic iteration order should ``sorted(FORMAT_TEMPLATES)``
rather than rely on dict insertion order.
"""

SHORT_PROFILE = FORMAT_TEMPLATES["short"]
LONG_PROFILE = FORMAT_TEMPLATES["long"]


def get_promo_format_profile(target_duration_sec: float | int | None) -> PromoFormatProfile:
    """Route a requested duration to its format card (P2 step 2).

    Pure duration → card lookup over the skeleton library: every card
    declares ``target_duration_sec``, one card per duration (collisions
    fail loudly at load in ``arsenal_loader``). An unknown duration is a
    loud error listing the deck — supporting a new duration means
    dropping a new skeleton YAML, not widening a threshold here.

    ``None`` preserves the legacy "no duration requested" contract and
    returns the short card.
    """
    if target_duration_sec is None:
        return FORMAT_TEMPLATES["short"]
    requested = float(target_duration_sec)
    for profile in FORMAT_TEMPLATES.values():
        if float(profile.target_duration_sec) == requested:
            return profile
    known = sorted(p.target_duration_sec for p in FORMAT_TEMPLATES.values())
    raise ValueError(
        f"no format card declares target_duration_sec={target_duration_sec!r}; "
        f"known durations: {known} — add a skeleton YAML under "
        "promo/arsenal/script_skeletons/ to support a new duration"
    )


def get_clip_pool_messages(
    available_unique_clips: int,
    profile: PromoFormatProfile,
) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a clip pool against a format profile."""
    errors: list[str] = []
    warnings: list[str] = []

    if available_unique_clips < profile.min_clip_pool_size:
        errors.append(
            f"{profile.mode} format requires at least {profile.min_clip_pool_size} unique clips; "
            f"found {available_unique_clips}"
        )
        return errors, warnings

    if available_unique_clips < profile.recommended_clip_pool_size:
        warnings.append(
            f"{profile.mode} format works best with {profile.recommended_clip_pool_size}+ unique clips; "
            f"found {available_unique_clips}"
        )

    return errors, warnings
