# promo/core/selection/ — per-variant format + persona seams

Two `runtime_checkable` Protocols (`FormatSelector`, `PersonaSelector`) plus default `Single*` / `Random*` implementations let `pipeline/_build_variant_selections` pick a `PromoFormatProfile` and `NarratorPersona` per variant without knowing which selector mode is active. Sprint 16 introduced this seam so a future `SmartFormatSelector` (clip-metadata + POI-aware) drops in without touching the variant loop.

## Files (inventory)

| File | Role |
|---|---|
| `__init__.py` | Public re-exports — `FormatSelector`, `PersonaSelector`, both `Single*` / `Random*` impls, `make_seeded_random`. |
| `protocols.py` | `FormatSelector` and `PersonaSelector` Protocols — both `runtime_checkable`, both expose `select(n_variants, *, poi_name, clip_metadata)`. |
| `format_selectors.py` | `SingleFormatSelector` (default; pins all variants to one profile) + `RandomFormatSelector` (samples per variant from `FORMAT_TEMPLATES`). |
| `persona_selectors.py` | `SinglePersonaSelector` (default; one persona for all variants) + `RandomPersonaSelector` (samples per variant from a YAML path list). |
| `_seed.py` | `make_seeded_random(seed)` — factory that returns a fresh `random.Random` instance per selector. |

## How they wire together

**Cross-file seams:**

- `protocols.py` references `format_profiles.PromoFormatProfile` and `script.script_generator.NarratorPersona` for the typed return shapes — Protocols are structural, so implementations don't inherit; they just need the matching `select` signature.
- `format_selectors.py` reads `format_profiles.FORMAT_TEMPLATES` (the discoverable registry rebuilt from `arsenal/script_skeletons/*.yaml` at module-import) and threads `make_seeded_random` for the `RandomFormatSelector`'s PRNG.
- `persona_selectors.py` loads YAMLs via `arsenal/personas/_loader.load_persona`, raises `config.ConfigError` on missing paths, and threads `make_seeded_random` for the `RandomPersonaSelector`'s PRNG. The bundled fallback is `arsenal/personas/third_person_promo.yaml`.
- Active selector resolved at pipeline startup by `pipeline/_build_variant_selections` from the `PROMO_FORMAT_SELECTOR` env var (`config.promo_format_selector` rejects unknown values with `ConfigError`).

**Invariants:**

- **`runtime_checkable` Protocols** — config-driven selector dispatch can `isinstance`-guard the active impl without import gymnastics.
- **Shared `select(n_variants, *, poi_name, clip_metadata)` signature** — both axes already thread `poi_name` + `clip_metadata` so a future `Smart*Selector` consumes them without changing the call site.
- **PRNG isolation** — every random-mode selector keeps its own `random.Random` via `make_seeded_random(seed)`. Never reach for the process-global `random` state.
- **Sorted iteration over discoverable sets** — `RandomFormatSelector` calls `tuple(sorted(FORMAT_TEMPLATES))` so the `_rng.choice` sequence is deterministic for any seed regardless of dict insertion order. `RandomPersonaSelector` keeps the caller-supplied `persona_paths` order verbatim (operator owns the ordering).
- **Loud failure on missing persona path** — `RandomPersonaSelector` raises `ConfigError` if a YAML listed in `persona_paths` doesn't exist; the bundled fallback is only used when `persona_paths` is empty.
- **`_DEFAULT_PERSONA_PATH` is duplicated** between `selection/persona_selectors.py` and `script/script_prompt_builder.py` — drift risk; tracked as a BACKLOG S6 item.
