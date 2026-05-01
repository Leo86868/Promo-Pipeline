"""Sprint 16 default :class:`FormatSelector` implementations.

Two shapes ship today:

* :class:`SingleFormatSelector` — every variant gets the same profile
  (the pre-Sprint-16 behaviour, lifted out so the variant loop reads
  through the seam regardless of selector mode). This is the
  ``PROMO_FORMAT_SELECTOR=single`` default — `--target-duration-sec X`
  pins all variants to the profile derived from X.
* :class:`RandomFormatSelector` — picks per variant uniformly at random
  from :data:`FORMAT_TEMPLATES`. Operator opt-in via
  ``PROMO_FORMAT_SELECTOR=random``; randomness changes per-variant
  duration so operators who opt in accept the per-variant
  filename / BGM / sidecar caveats called out in
  ``architecture.md`` "Selector seams (Sprint 16)".
"""

from __future__ import annotations

from promo.core.format_profiles import (
    FORMAT_TEMPLATES,
    PromoFormatProfile,
    get_promo_format_profile,
)

from ._seed import make_seeded_random


class SingleFormatSelector:
    """Pin every variant to one profile.

    ``profile`` wins if explicitly provided; otherwise the profile is
    derived from ``target_duration_sec`` via
    :func:`get_promo_format_profile` (the pre-Sprint-16 path). This is
    the ``PROMO_FORMAT_SELECTOR=single`` default and preserves the
    operator contract that ``--target-duration-sec X`` produces an X-
    second video for every variant.
    """

    def __init__(
        self,
        target_duration_sec: float | int | None = None,
        profile: PromoFormatProfile | None = None,
    ) -> None:
        if profile is not None:
            self._profile = profile
        else:
            self._profile = get_promo_format_profile(target_duration_sec)

    def select(
        self,
        n_variants: int,
        *,
        poi_name: str,
        clip_metadata: list[dict],
    ) -> list[PromoFormatProfile]:
        if n_variants <= 0:
            return []
        return [self._profile] * n_variants


class RandomFormatSelector:
    """Pick a profile per variant uniformly at random from
    ``FORMAT_TEMPLATES``.

    The selector keeps its own ``random.Random`` so callers passing a
    ``seed`` get reproducible variant mixes without touching the
    process-global ``random`` state. Each ``select()`` call samples
    independently — no de-duplication; with ``n_variants >= 2`` and
    two templates the operator should expect both modes to appear over
    a small number of seeds.
    """

    def __init__(self, seed: int | None = None) -> None:
        self._rng = make_seeded_random(seed)
        # Sort the keys so iteration order across Python runs is
        # stable; ``random.choice`` then sees a deterministic sequence
        # for any given seed regardless of dict insertion order.
        self._template_keys: tuple[str, ...] = tuple(sorted(FORMAT_TEMPLATES))

    def select(
        self,
        n_variants: int,
        *,
        poi_name: str,
        clip_metadata: list[dict],
    ) -> list[PromoFormatProfile]:
        if n_variants <= 0:
            return []
        return [
            FORMAT_TEMPLATES[self._rng.choice(self._template_keys)]
            for _ in range(n_variants)
        ]
