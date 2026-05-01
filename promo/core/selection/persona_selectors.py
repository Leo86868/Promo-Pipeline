"""Sprint 16 default :class:`PersonaSelector` implementations.

Two shapes ship today:

* :class:`SinglePersonaSelector` — every variant gets the same persona.
  The variant loop's previous behaviour, lifted out of
  ``script_generator.generate_script_variants`` so the call site is
  uniform across all selectors.
* :class:`RandomPersonaSelector` — picks per variant from the
  ``promo/arsenal/personas/`` YAMLs the operator points at. With only
  one persona on disk today (``third_person_promo.yaml``) it
  degenerates to single-persona behaviour, but ships with the correct
  shape so a second persona drop-in works without code edits.
"""

from __future__ import annotations

import os
from typing import Sequence

from promo.arsenal.personas._loader import load_persona
from promo.core.config import ConfigError
from promo.core.script.script_generator import NarratorPersona

from ._seed import make_seeded_random

_DEFAULT_PERSONA_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "arsenal", "personas", "third_person_promo.yaml")
)


class SinglePersonaSelector:
    """Return one persona for every variant.

    With ``persona_path=None`` the selector resolves to the bundled
    ``promo/personas/third_person_promo.yaml``. The persona is loaded
    once per selector instance — repeated ``select()`` calls reuse the
    cached :class:`NarratorPersona`.
    """

    def __init__(self, persona_path: str | None = None) -> None:
        self._persona_path = persona_path or _DEFAULT_PERSONA_PATH
        self._persona: NarratorPersona | None = None

    def _resolve(self) -> NarratorPersona:
        if self._persona is None:
            self._persona = load_persona(self._persona_path)
        return self._persona

    def select(
        self,
        n_variants: int,
        *,
        poi_name: str,
        clip_metadata: list[dict],
    ) -> list[NarratorPersona]:
        if n_variants <= 0:
            return []
        persona = self._resolve()
        return [persona for _ in range(n_variants)]


class RandomPersonaSelector:
    """Pick a persona per variant uniformly at random from a path list.

    ``persona_paths`` is the discoverable persona library. With one
    YAML the selector mirrors :class:`SinglePersonaSelector`; with
    multiple it samples independently per variant. Each path is
    resolved at most once and cached.
    """

    def __init__(
        self,
        persona_paths: Sequence[str] | None = None,
        seed: int | None = None,
    ) -> None:
        if persona_paths:
            self._persona_paths: tuple[str, ...] = tuple(persona_paths)
        else:
            self._persona_paths = (_DEFAULT_PERSONA_PATH,)
        self._rng = make_seeded_random(seed)
        self._cache: dict[str, NarratorPersona] = {}

    def _resolve(self, path: str) -> NarratorPersona:
        cached = self._cache.get(path)
        if cached is not None:
            return cached
        try:
            persona = load_persona(path)
        except FileNotFoundError as exc:
            raise ConfigError(
                f"RandomPersonaSelector persona path not found: {path!r}. "
                f"Drop the YAML in promo/arsenal/personas/ or pass a real path."
            ) from exc
        self._cache[path] = persona
        return persona

    def select(
        self,
        n_variants: int,
        *,
        poi_name: str,
        clip_metadata: list[dict],
    ) -> list[NarratorPersona]:
        if n_variants <= 0:
            return []
        return [
            self._resolve(self._rng.choice(self._persona_paths))
            for _ in range(n_variants)
        ]
