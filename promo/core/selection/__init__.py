"""Per-variant format + persona selector seams (Sprint 16).

The package is a `Shape B` layout under `promo/core/`:

* :mod:`promo.core.selection.protocols` — :class:`FormatSelector` and
  :class:`PersonaSelector` runtime-checkable Protocols.
* :mod:`promo.core.selection.format_selectors` —
  :class:`SingleFormatSelector` (the
  ``PROMO_FORMAT_SELECTOR=single`` default; pins all variants to the
  profile derived from ``--target-duration-sec``) and
  :class:`RandomFormatSelector` (opt-in
  ``PROMO_FORMAT_SELECTOR=random``; samples per variant from
  :data:`promo.core.format_profiles.FORMAT_TEMPLATES`).
* :mod:`promo.core.selection.persona_selectors` —
  :class:`SinglePersonaSelector` (default; only one persona YAML
  ships) and :class:`RandomPersonaSelector` (works once a second
  persona YAML lands in ``promo/personas/``).
* :mod:`promo.core.selection._seed` — shared :func:`make_seeded_random`
  helper so every selector samples reproducibly under a fixed seed.

See ``architecture.md`` "Selector seams (Sprint 16)" for the four
cold-reader entry points, the opt-in random caveats, and the steps
to add a new selector or format template.
"""

from .format_selectors import RandomFormatSelector, SingleFormatSelector
from .persona_selectors import RandomPersonaSelector, SinglePersonaSelector
from .protocols import FormatSelector, PersonaSelector
from ._seed import make_seeded_random

__all__ = [
    "FormatSelector",
    "PersonaSelector",
    "RandomFormatSelector",
    "RandomPersonaSelector",
    "SingleFormatSelector",
    "SinglePersonaSelector",
    "make_seeded_random",
]
