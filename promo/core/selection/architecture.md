# promo/core/selection/ — per-variant format + persona seams

Two `runtime_checkable` Protocols (`FormatSelector`, `PersonaSelector`) plus default `Single*` / `Random*` implementations let `pipeline/_build_variant_selections` pick a `PromoFormatProfile` and `NarratorPersona` per variant without knowing which selector mode is active. The seam exists so a future `SmartFormatSelector` (clip-metadata + POI-aware) drops in without touching the variant loop.

> **Read upstream first:** [`README.md`](../../../README.md) → [`promo/core/architecture.md`](../architecture.md) (defines POI, Pluggability Charter). This doc covers the cross-cutting selection seam.

## Vocabulary (new terms in this doc)

- **structural typing** — Python's `Protocol` typing pattern. Any class with the matching method signatures qualifies as a `FormatSelector` / `PersonaSelector`; no explicit `class Foo(FormatSelector)` inheritance required. Means a smart selector can drop in by matching the `select(...)` signature without importing the Protocol class.
- **PRNG isolation** — every random-mode selector holds its own `random.Random` instance via `make_seeded_random(seed)`, instead of touching the process-global `random` module state. Reproducibility under a fixed seed depends on this — any code path reaching for `random.choice` on globals would silently make selection non-deterministic.

## Files (inventory)

| File | I/O surface |
|---|---|
| `__init__.py` | Public re-exports — `FormatSelector`, `PersonaSelector`, both `Single*` / `Random*` impls, `make_seeded_random`. |
| `protocols.py` | **Provides:** `FormatSelector` + `PersonaSelector` Protocols, both `@runtime_checkable`. **Surface:** `select(n_variants, *, poi_name, clip_metadata) → list[PromoFormatProfile]` (or `list[NarratorPersona]`). **Side:** none (type-only). **Consumers:** `format_selectors`, `persona_selectors`, `pipeline/_build_variant_selections`. |
| `format_selectors.py` | **Provides:** `SingleFormatSelector` (default; pins every variant to one profile) + `RandomFormatSelector` (samples per variant from `FORMAT_TEMPLATES`). **In:** `n_variants` + threading kwargs (`poi_name`, `clip_metadata`). **Out:** `list[PromoFormatProfile]`. **Side:** `RandomFormatSelector` calls `tuple(sorted(FORMAT_TEMPLATES))` for deterministic iteration order. **Raises:** nothing. **Consumers:** `pipeline/_build_variant_selections`. |
| `persona_selectors.py` | **Provides:** `SinglePersonaSelector` (default; one persona for all variants) + `RandomPersonaSelector` (samples per variant from a YAML path list). **In:** `n_variants` + threading kwargs. **Out:** `list[NarratorPersona]`. **Side:** loads YAMLs via the `arsenal/personas/_loader.load_persona` shim (which routes to `arsenal_loader.load_persona`); the bundled fallback is `arsenal/personas/third_person_promo.yaml` when no `persona_paths` are supplied. **Raises:** `ConfigError` (from `config`) when a YAML listed in `persona_paths` doesn't exist. **Consumers:** `pipeline/_build_variant_selections`. |
| `_seed.py` | **Provides:** `make_seeded_random(seed) → random.Random`. **In:** `seed: int | None`. **Out:** a fresh `random.Random` instance (never the process-global `random` module). **Side:** none (pure factory). **Consumers:** `format_selectors`, `persona_selectors`. |

## How they wire together

**Cross-file seams:**

- `protocols.py` references `format_profiles.PromoFormatProfile` and `script.script_generator.NarratorPersona` for the typed return shapes.
- `format_selectors.py` reads `format_profiles.FORMAT_TEMPLATES` (the discoverable registry rebuilt from `arsenal/script_skeletons/*.yaml` at module-import).
- Active selector resolved at pipeline startup by `pipeline/_build_variant_selections` from the `PROMO_FORMAT_SELECTOR` env var (`config.promo_format_selector` rejects unknown values with `ConfigError`).

**Invariants:**

- **`runtime_checkable` Protocols** — config-driven selector dispatch can `isinstance`-guard the active impl without import gymnastics.
- **Shared `select(n_variants, *, poi_name, clip_metadata)` signature** — both axes already thread `poi_name` + `clip_metadata` so a future `Smart*Selector` consumes them without changing the call site.
- **PRNG isolation** — every random-mode selector keeps its own `random.Random` via `make_seeded_random(seed)`. Never reach for the process-global `random` state.
- **Sorted iteration over discoverable sets** — `RandomFormatSelector` calls `tuple(sorted(FORMAT_TEMPLATES))` so the `_rng.choice` sequence is deterministic for any seed regardless of dict insertion order. `RandomPersonaSelector` keeps the caller-supplied `persona_paths` order verbatim (operator owns the ordering).
- **Loud failure on missing persona path** — `RandomPersonaSelector` raises `ConfigError` if a YAML listed in `persona_paths` doesn't exist; the bundled fallback is only used when `persona_paths` is empty.
- **`_DEFAULT_PERSONA_PATH` is duplicated** between `selection/persona_selectors.py` and `script/script_prompt_builder.py` — drift risk; tracked in [`BACKLOG.md`](../../../BACKLOG.md).
