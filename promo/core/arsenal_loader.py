"""Single thin reader for the ``promo/arsenal/`` library tree.

Per contract §4.9, the loader is the only module that does file I/O for
arsenal data. Every consumer (``clip_analyzer``, ``script_generator``,
``clip_assigner``, ``tts_engine``, ``format_profiles``,
``arsenal/personas/_loader.py``) imports through here.

Six public entry points cover the arsenal sub-libraries:

  - ``load_system_prompt(name)`` → ``arsenal/system_prompts/<name>_v1.md``
    (4 known names: ``mimo_clip_analysis``, ``gemini1_script``,
    e.g. ``gemini1_script``).
  - ``load_voice_catalog()`` → ``arsenal/voices/catalog.yaml``.
  - ``load_persona(name_or_path)`` → ``arsenal/personas/<name>.yaml``
    (or, in legacy path mode, an absolute / relative-to-cwd path).
  - ``load_format_templates()`` → every ``arsenal/script_skeletons/*.yaml``
    keyed by ``mode``.
  - ``load_format_template(key)`` → one profile by ``mode`` key.
  - ``load_script_hooks()`` → ``arsenal/script_hooks.yaml``.

Type imports come from :mod:`promo.core.schema` to break the circular
import that would otherwise form between ``arsenal_loader`` and the
modules that previously hosted ``NarratorPersona`` / ``PromoFormatProfile``
(see Sprint Arsenal Externalization Commit 0).

Loaders are LRU-cached so module-import I/O happens once per process.
:func:`reset_for_tests` clears every cache + re-primes the ``VOICE_CATALOG``
and ``FORMAT_TEMPLATES`` re-exports when a test rotates an arsenal file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from promo.core.schema import NarratorPersona, PromoFormatProfile

# Resolved lazily inside each loader so import-time has no I/O cost.
_ARSENAL_ROOT = Path(__file__).resolve().parent.parent / "arsenal"

# Names that ``load_system_prompt`` knows how to resolve. The set grows
# commit-by-commit as each Sprint Arsenal Externalization migration lands
# its corresponding ``_v1.md`` file under ``arsenal/system_prompts/``.
_KNOWN_SYSTEM_PROMPTS: frozenset[str] = frozenset({
    "mimo_clip_analysis",        # Commit 2
    "gemini1_script",            # Commit 3
    "gemini2_assign",            # Commit 4
})


def _arsenal_path(*parts: str) -> Path:
    """Resolve a path under ``promo/arsenal/``.

    Kept private so the directory layout is opaque to callers — they
    address libraries by name, not by file path.
    """
    return _ARSENAL_ROOT.joinpath(*parts)


@lru_cache(maxsize=None)
def load_system_prompt(name: str) -> str:
    """Return the v1 system prompt for ``name``.

    ``name`` is the prompt's stem (e.g. ``"mimo_clip_analysis"``); the
    loader appends ``_v1.md`` and reads from
    ``promo/arsenal/system_prompts/``. Trailing whitespace is stripped
    via ``.rstrip()`` to protect cache invariants — the MiMo
    ``_cache_version_suffix`` is a hash of the prompt string, so a stray
    editor-added trailing newline would invalidate every per-POI
    ``.mimo_cache/<hash>-<suffix>.json`` file across operator's POI set.

    Raises ``ValueError`` for an unknown prompt name and ``FileNotFoundError``
    when the MD file is missing — both fail-fast so a typo or a missing
    arsenal file does not silently default to an empty prompt.
    """
    if name not in _KNOWN_SYSTEM_PROMPTS:
        raise ValueError(
            f"unknown system prompt {name!r}; "
            f"known: {sorted(_KNOWN_SYSTEM_PROMPTS)}"
        )
    path = _arsenal_path("system_prompts", f"{name}_v1.md")
    if not path.exists():
        raise FileNotFoundError(f"missing arsenal prompt file: {path}")
    return path.read_text(encoding="utf-8").rstrip()


_VOICE_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "id", "name", "gender", "age", "accent", "description", "backend",
})


@lru_cache(maxsize=1)
def load_voice_catalog() -> dict[str, dict[str, Any]]:
    """Return the voice catalog keyed by voice slug (``kore``, etc).

    Reads ``promo/arsenal/voices/catalog.yaml``. PyYAML preserves
    insertion order so the dispatch-rotation contract that
    ``promo.core.pipeline.bgm_voice_resolver._resolve_voice_keys``
    depends on (Gemini-first when ``--voice`` is unset) is preserved.

    Each entry must carry the 7 required fields; ``style_prompt`` is
    optional (Gemini-backend only). ``backend`` value-domain is left
    open — adding a new TTS vendor (Resemble, OpenAI TTS, etc.) is
    "drop a new value here + add the dispatch arm in ``tts_engine``";
    no enum-narrowing in this loader.

    A malformed catalog (missing required field) raises ``ValueError``
    at module-import time, not at first compile.
    """
    path = _arsenal_path("voices", "catalog.yaml")
    if not path.exists():
        raise FileNotFoundError(f"missing arsenal voice catalog: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        catalog = yaml.safe_load(fh)
    if not isinstance(catalog, dict):
        raise ValueError(f"{path}: top-level must be a mapping; got {type(catalog).__name__}")
    for voice_key, entry in catalog.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{path}::{voice_key}: entry must be a mapping; got {type(entry).__name__}")
        missing = _VOICE_REQUIRED_FIELDS - entry.keys()
        if missing:
            raise ValueError(
                f"{path}::{voice_key}: missing required fields: {sorted(missing)}"
            )
    return catalog


def load_persona(name_or_path: str) -> NarratorPersona:
    """Return a :class:`NarratorPersona` from a persona YAML.

    Two call shapes supported:

      - **Stem**: ``load_persona("third_person_promo")`` resolves to
        ``promo/arsenal/personas/third_person_promo.yaml``.
      - **Path**: ``load_persona("path/to/persona.yaml")`` resolves
        the literal path. Supports absolute, cwd-relative, and any
        ``.yaml``-suffixed string (used by
        ``script_generator._DEFAULT_PERSONA_PATH`` and by tests that
        load fixture personas from disk).

    Unknown top-level keys are silently dropped so future optional
    persona fields do not break the loader on historical YAMLs.
    """
    if name_or_path.endswith(".yaml") or "/" in name_or_path or "\\" in name_or_path:
        path = Path(name_or_path)
    else:
        path = _arsenal_path("personas", f"{name_or_path}.yaml")
    if not path.exists():
        raise FileNotFoundError(f"missing persona YAML: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level must be a mapping; got {type(data).__name__}")
    known = set(NarratorPersona.__dataclass_fields__)
    filtered = {k: v for k, v in data.items() if k in known}
    return NarratorPersona(**filtered)


_FORMAT_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "mode", "target_duration_sec", "duration_label", "segment_count",
    "total_words_min", "total_words_max", "per_segment_min", "per_segment_max",
    "min_clip_pool_size", "recommended_clip_pool_size",
    "min_effective_wpm", "max_effective_wpm", "max_narration_ratio",
    "segment_plans",
    # P2 personality blocks — REQUIRED, no silent defaults: a card that
    # forgets to declare its pacing or asset floor must fail at load,
    # not inherit another type's behavior.
    "description", "pacing", "assets",
})

_PACING_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "beat_min_sec", "beat_max_sec", "pause_cap_ms",
})

_ASSETS_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "base_min", "per_extra",
})

_SEGMENT_PLAN_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "label", "approx_words", "min_clips", "max_clips", "guidance",
})


def _build_segment_plan(raw: dict[str, Any], *, ctx: str):
    """Inner ctor — returns a :class:`promo.core.schema.SegmentPlan`."""
    from promo.core.schema import SegmentPlan

    missing = _SEGMENT_PLAN_REQUIRED_FIELDS - raw.keys()
    if missing:
        raise ValueError(f"{ctx}: segment_plan missing required fields: {sorted(missing)}")
    preferred = raw.get("preferred_categories") or ()
    if not isinstance(preferred, (list, tuple)):
        raise ValueError(
            f"{ctx}: preferred_categories must be a list; got {type(preferred).__name__}"
        )
    return SegmentPlan(
        label=raw["label"],
        approx_words=raw["approx_words"],
        min_clips=raw["min_clips"],
        max_clips=raw["max_clips"],
        guidance=raw["guidance"],
        preferred_categories=tuple(preferred),
    )


def _build_format_profile(raw: dict[str, Any], *, ctx: str) -> PromoFormatProfile:
    """Inner ctor — returns a :class:`promo.core.schema.PromoFormatProfile`."""
    missing = _FORMAT_REQUIRED_FIELDS - raw.keys()
    if missing:
        raise ValueError(f"{ctx}: format_template missing required fields: {sorted(missing)}")
    plans_raw = raw["segment_plans"]
    if not isinstance(plans_raw, list) or not plans_raw:
        raise ValueError(f"{ctx}: segment_plans must be a non-empty list")
    plans = tuple(
        _build_segment_plan(p, ctx=f"{ctx}::segment_plans[{i}]")
        for i, p in enumerate(plans_raw)
    )
    extra_rules = raw.get("extra_rules") or ()
    if not isinstance(extra_rules, (list, tuple)):
        raise ValueError(
            f"{ctx}: extra_rules must be a list; got {type(extra_rules).__name__}"
        )
    description = raw["description"]
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"{ctx}: description must be a non-empty string")
    pacing = raw["pacing"]
    if not isinstance(pacing, dict):
        raise ValueError(f"{ctx}: pacing must be a mapping; got {type(pacing).__name__}")
    missing_pacing = _PACING_REQUIRED_FIELDS - pacing.keys()
    if missing_pacing:
        raise ValueError(f"{ctx}: pacing missing required fields: {sorted(missing_pacing)}")
    beat_min_sec = float(pacing["beat_min_sec"])
    beat_max_sec = float(pacing["beat_max_sec"])
    if not 0 < beat_min_sec < beat_max_sec:
        raise ValueError(
            f"{ctx}: pacing requires 0 < beat_min_sec < beat_max_sec "
            f"(got {beat_min_sec}, {beat_max_sec})"
        )
    pause_cap_ms = int(pacing["pause_cap_ms"])
    if pause_cap_ms <= 0:
        raise ValueError(f"{ctx}: pacing.pause_cap_ms must be positive (got {pause_cap_ms})")
    assets = raw["assets"]
    if not isinstance(assets, dict):
        raise ValueError(f"{ctx}: assets must be a mapping; got {type(assets).__name__}")
    missing_assets = _ASSETS_REQUIRED_FIELDS - assets.keys()
    if missing_assets:
        raise ValueError(f"{ctx}: assets missing required fields: {sorted(missing_assets)}")
    assets_base_min = int(assets["base_min"])
    assets_per_extra = int(assets["per_extra"])
    if assets_base_min <= 0 or assets_per_extra < 0:
        raise ValueError(
            f"{ctx}: assets requires base_min > 0 and per_extra >= 0 "
            f"(got {assets_base_min}, {assets_per_extra})"
        )
    return PromoFormatProfile(
        mode=raw["mode"],
        target_duration_sec=int(raw["target_duration_sec"]),
        duration_label=raw["duration_label"],
        segment_count=int(raw["segment_count"]),
        total_words_min=int(raw["total_words_min"]),
        total_words_max=int(raw["total_words_max"]),
        per_segment_min=int(raw["per_segment_min"]),
        per_segment_max=int(raw["per_segment_max"]),
        min_clip_pool_size=int(raw["min_clip_pool_size"]),
        recommended_clip_pool_size=int(raw["recommended_clip_pool_size"]),
        min_effective_wpm=int(raw["min_effective_wpm"]),
        max_effective_wpm=int(raw["max_effective_wpm"]),
        max_narration_ratio=float(raw["max_narration_ratio"]),
        segment_plans=plans,
        description=description.strip(),
        beat_min_sec=beat_min_sec,
        beat_max_sec=beat_max_sec,
        pause_cap_ms=pause_cap_ms,
        assets_base_min=assets_base_min,
        assets_per_extra=assets_per_extra,
        sentence_rule=raw.get("sentence_rule", ""),
        extra_rules=tuple(extra_rules),
    )


@lru_cache(maxsize=1)
def load_format_templates() -> dict[str, PromoFormatProfile]:
    """Return every :class:`PromoFormatProfile` in the skeleton library,
    keyed by ``profile.mode``.

    Iterates every ``*.yaml`` under ``promo/arsenal/script_skeletons/``;
    each YAML must declare a ``mode`` field that becomes the dict key.
    Two yaml files declaring the same ``mode`` raise ``ValueError``."""
    skel_dir = _arsenal_path("script_skeletons")
    if not skel_dir.exists():
        raise FileNotFoundError(f"missing arsenal script_skeletons dir: {skel_dir}")
    profiles: dict[str, PromoFormatProfile] = {}
    for yaml_path in sorted(skel_dir.glob("*.yaml")):
        with open(yaml_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        if not isinstance(raw, dict):
            raise ValueError(f"{yaml_path}: top-level must be a mapping")
        profile = _build_format_profile(raw, ctx=str(yaml_path))
        if profile.mode in profiles:
            raise ValueError(f"duplicate format mode {profile.mode!r} in {yaml_path}")
        profiles[profile.mode] = profile
    return profiles


def load_format_template(key: str) -> PromoFormatProfile:
    """Return one :class:`PromoFormatProfile` by ``key`` (``short`` / ``long``)."""
    profiles = load_format_templates()
    if key not in profiles:
        raise ValueError(
            f"unknown format template {key!r}; known: {sorted(profiles)}"
        )
    return profiles[key]


@lru_cache(maxsize=1)
def load_script_hooks() -> list[str]:
    """Return ordered hook-technique seeds for script variant diversity."""
    path = _arsenal_path("script_hooks.yaml")
    if not path.exists():
        raise FileNotFoundError(f"missing arsenal script hooks: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level must be a mapping; got {type(raw).__name__}")
    hooks = raw.get("hook_techniques")
    if not isinstance(hooks, list) or not hooks:
        raise ValueError(f"{path}: hook_techniques must be a non-empty list")
    for idx, hook in enumerate(hooks):
        if not isinstance(hook, str) or not hook.strip():
            raise ValueError(
                f"{path}: hook_techniques[{idx}] must be a non-empty string"
            )
    return [hook.strip() for hook in hooks]


def reset_for_tests() -> None:
    """Clear every loader cache so a test that rotates an arsenal file
    sees the rotated content on the next read.

    Per the project convention (``feedback_module_global_cache_reset``
    in ``MEMORY.md``): module-level ``@lru_cache`` loaders MUST ship a
    reset helper so test isolation does not depend on import order.
    Mirrors :func:`promo.core.llm.gemini_client.reset_for_tests`.

    Also re-primes the two import-time consumers (``tts_engine.VOICE_CATALOG``
    and ``format_profiles.FORMAT_TEMPLATES`` / ``SHORT_PROFILE`` /
    ``LONG_PROFILE``) so a test that mutates ``catalog.yaml`` or a
    skeleton YAML and calls ``reset_for_tests()`` afterwards observes
    the rotation through the symbol re-exports as well as through the
    direct loader calls.
    """
    load_system_prompt.cache_clear()
    load_voice_catalog.cache_clear()
    load_format_templates.cache_clear()
    load_script_hooks.cache_clear()

    # Re-prime the import-time consumers. Imports are local so this
    # module stays free of optional-consumer dependencies — a test that
    # only exercises arsenal_loader directly need not load tts_engine
    # or format_profiles.
    try:
        from promo.core.narrate import tts_engine
        tts_engine.VOICE_CATALOG = load_voice_catalog()
    except ImportError:
        pass
    try:
        from promo.core import format_profiles
        format_profiles.FORMAT_TEMPLATES = load_format_templates()
        format_profiles.SHORT_PROFILE = format_profiles.FORMAT_TEMPLATES["short"]
        format_profiles.LONG_PROFILE = format_profiles.FORMAT_TEMPLATES["long"]
    except (ImportError, KeyError):
        pass
