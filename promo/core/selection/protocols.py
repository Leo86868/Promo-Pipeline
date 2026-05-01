"""Sprint 16 selector seams: ``FormatSelector`` + ``PersonaSelector``.

Both protocols are runtime-checkable so callers can ``isinstance``-guard
config-driven selector instances without import gymnastics. The shared
``select(n_variants, *, poi_name, clip_metadata)`` keyword shape leaves
room for a future ``SmartFormatSelector`` / ``SmartPersonaSelector``
that reads POI / inventory information without changing the call site
in ``compile_promo.py``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from promo.core.format_profiles import PromoFormatProfile
from promo.core.script.script_generator import NarratorPersona


@runtime_checkable
class FormatSelector(Protocol):
    """Pick one ``PromoFormatProfile`` per variant.

    Implementations decide how to spread variants across the
    ``FORMAT_TEMPLATES`` registry: random sampling, explicit operator
    list, or a smart picker that consults clip / POI metadata. The
    default Sprint 16 implementation is :class:`RandomFormatSelector`.
    """

    def select(
        self,
        n_variants: int,
        *,
        poi_name: str,
        clip_metadata: list[dict],
    ) -> list[PromoFormatProfile]:
        """Return one ``PromoFormatProfile`` per requested variant."""
        ...


@runtime_checkable
class PersonaSelector(Protocol):
    """Pick one ``NarratorPersona`` per variant.

    Mirrors :class:`FormatSelector` so the variant loop instantiates
    both seams from config and threads their outputs in lockstep.
    Sprint 16 ships :class:`SinglePersonaSelector` (one persona for all
    variants) and :class:`RandomPersonaSelector` (chooses between
    discovered persona YAMLs); both produce ``NarratorPersona`` objects
    via the extracted ``promo.personas._loader.load_persona``.
    """

    def select(
        self,
        n_variants: int,
        *,
        poi_name: str,
        clip_metadata: list[dict],
    ) -> list[NarratorPersona]:
        """Return one ``NarratorPersona`` per requested variant."""
        ...
