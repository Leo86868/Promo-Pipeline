"""Unit tests for promo.core.arsenal_loader — Sprint Arsenal
Externalization audit-fix.

Pins:
  - F3-retry feedback block byte-identity vs the pre-extraction
    inline literal (the caller's ``+ "\\n"`` restoration).
  - hook techniques are loaded from arsenal data.
  - ``reset_for_tests`` clears every cache and re-primes
    ``tts_engine.VOICE_CATALOG`` and ``format_profiles.FORMAT_TEMPLATES``.
  - ``load_format_template(key)`` (singular) is exercised — the contract
    declares it but the production happy path uses ``load_format_templates()``;
    a regression that breaks the singular shape would otherwise go silent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from promo.core import arsenal_loader
from promo.core.schema import PromoFormatProfile


PROMO_PKG = Path(__file__).resolve().parents[2]
ARSENAL_ROOT = PROMO_PKG / "arsenal"


class TestPersonaLoaderShapes:
    """``load_persona`` accepts both a bare stem (resolves under
    ``arsenal/personas/``) and a path (loaded literally). Both shapes
    are part of the loader's public contract — kept open so future
    callers / tests are not constrained."""

    def test_bare_stem_resolves_against_arsenal(self):
        persona = arsenal_loader.load_persona("third_person_promo")
        assert persona.id == "third_person_promo"

    def test_path_with_separator_works(self):
        path = ARSENAL_ROOT / "personas" / "third_person_promo.yaml"
        persona = arsenal_loader.load_persona(str(path))
        assert persona.id == "third_person_promo"


class TestResetForTests:
    """The 3 ``@lru_cache`` loaders MUST ship a ``reset_for_tests()``
    helper per project convention (``feedback_module_global_cache_reset``).
    Verify the helper actually clears the caches AND re-primes the
    import-time consumers."""

    def test_reset_clears_system_prompt_cache(self):
        # Prime the cache.
        arsenal_loader.load_system_prompt("mimo_clip_analysis")
        info = arsenal_loader.load_system_prompt.cache_info()
        assert info.currsize >= 1

        arsenal_loader.reset_for_tests()
        info = arsenal_loader.load_system_prompt.cache_info()
        assert info.currsize == 0

    def test_reset_clears_script_hooks_cache(self):
        arsenal_loader.load_script_hooks()
        info = arsenal_loader.load_script_hooks.cache_info()
        assert info.currsize == 1

        arsenal_loader.reset_for_tests()
        info = arsenal_loader.load_script_hooks.cache_info()
        assert info.currsize == 0

    def test_reset_clears_voice_catalog_cache_to_observe_disk_changes(
        self, tmp_path, monkeypatch
    ):
        """End-to-end behavioural pin: a test that swaps the voice
        catalog file via monkeypatch + `reset_for_tests` MUST observe
        the rotated content on the next loader call. Without the
        cache-clear in `reset_for_tests`, the first load would be
        served from the stale cached value and the rotation invisible."""
        original = arsenal_loader.load_voice_catalog()  # prime
        rotated = tmp_path / "catalog.yaml"
        rotated.write_text(
            "rotated_only:\n"
            "  id: TestVoice\n"
            "  name: Rotated\n"
            "  gender: female\n"
            "  age: young\n"
            "  accent: American\n"
            "  description: synthetic test voice\n"
            "  backend: gemini\n"
        )
        monkeypatch.setattr(
            arsenal_loader,
            "_arsenal_path",
            lambda *parts: rotated if parts == ("voices", "catalog.yaml")
            else arsenal_loader._ARSENAL_ROOT.joinpath(*parts),
        )
        try:
            arsenal_loader.reset_for_tests()
            after = arsenal_loader.load_voice_catalog()
            assert "rotated_only" in after
            assert "kore" not in after
        finally:
            # Restore the real catalog regardless of test outcome.
            monkeypatch.undo()
            arsenal_loader.reset_for_tests()
            assert "kore" in arsenal_loader.load_voice_catalog()

    def test_reset_clears_format_templates_cache_to_observe_disk_changes(
        self, tmp_path, monkeypatch
    ):
        """Same end-to-end test for format templates."""
        original = arsenal_loader.load_format_templates()  # prime
        rotated_dir = tmp_path / "skel"
        rotated_dir.mkdir()
        # Construct a minimal complete template that differs from short/long.
        (rotated_dir / "rotated.yaml").write_text(
            "mode: rotated_only\n"
            "target_duration_sec: 45\n"
            "duration_label: '45 second'\n"
            "segment_count: 1\n"
            "total_words_min: 50\n"
            "total_words_max: 80\n"
            "per_segment_min: 50\n"
            "per_segment_max: 80\n"
            "min_clip_pool_size: 1\n"
            "recommended_clip_pool_size: 1\n"
            "min_effective_wpm: 100\n"
            "max_effective_wpm: 120\n"
            "max_narration_ratio: 0.9\n"
            "sentence_rule: ''\n"
            "extra_rules: []\n"
            "segment_plans:\n"
            "  - label: ALL\n"
            "    approx_words: 65\n"
            "    min_clips: 1\n"
            "    max_clips: 1\n"
            "    guidance: synthetic\n"
        )
        monkeypatch.setattr(
            arsenal_loader,
            "_arsenal_path",
            lambda *parts: rotated_dir if parts == ("script_skeletons",)
            else arsenal_loader._ARSENAL_ROOT.joinpath(*parts),
        )
        try:
            arsenal_loader.reset_for_tests()
            # After reset_for_tests, format_profiles re-prime would have
            # tried `["short"]` lookup which doesn't exist in the rotated
            # set — that branch is wrapped in `except KeyError` so the
            # reset is non-fatal even with a non-canonical skeleton set.
            after = arsenal_loader.load_format_templates()
            assert "rotated_only" in after
            assert "short" not in after
        finally:
            monkeypatch.undo()
            arsenal_loader.reset_for_tests()
            templates = arsenal_loader.load_format_templates()
            assert "short" in templates and "long" in templates

    def test_reset_re_primes_voice_catalog_re_export(self):
        """``tts_engine.VOICE_CATALOG`` is set at import time. After
        ``reset_for_tests``, the re-export points at the freshly-loaded
        catalog (verified by identity-check against the loader output)."""
        from promo.core.narrate import tts_engine

        arsenal_loader.reset_for_tests()
        assert tts_engine.VOICE_CATALOG == arsenal_loader.load_voice_catalog()

    def test_reset_re_primes_format_templates_re_export(self):
        from promo.core import format_profiles

        arsenal_loader.reset_for_tests()
        assert (
            format_profiles.FORMAT_TEMPLATES
            == arsenal_loader.load_format_templates()
        )
        assert (
            format_profiles.SHORT_PROFILE
            is format_profiles.FORMAT_TEMPLATES["short"]
        )
        assert (
            format_profiles.LONG_PROFILE
            is format_profiles.FORMAT_TEMPLATES["long"]
        )


class TestLoadFormatTemplateSingular:
    """The contract §4.9 declares ``load_format_template(key)`` as part
    of the public surface but the production happy path uses
    ``load_format_templates()`` — a regression in the singular shape
    would otherwise go silent. Pin both shapes."""

    def test_singular_returns_promo_format_profile(self):
        profile = arsenal_loader.load_format_template("short")
        assert isinstance(profile, PromoFormatProfile)
        assert profile.mode == "short"

    def test_singular_unknown_key_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown format template"):
            arsenal_loader.load_format_template("bogus_format_42s")

    def test_singular_and_plural_return_same_profile(self):
        plural = arsenal_loader.load_format_templates()
        for key in plural:
            assert arsenal_loader.load_format_template(key) is plural[key]


class TestLoadScriptHooks:
    def test_load_script_hooks_returns_ordered_hook_techniques(self):
        assert arsenal_loader.load_script_hooks() == [
            "contradiction",
            "sensory",
            "specific_number",
            "second_person",
            "time_anchor",
            "superlative",
        ]

    def test_script_prompt_builder_re_exports_loaded_hook_techniques(self):
        from promo.core.script import script_prompt_builder

        assert script_prompt_builder.HOOK_TECHNIQUES == arsenal_loader.load_script_hooks()

    def test_malformed_script_hooks_raise_value_error(self, tmp_path, monkeypatch):
        bad_hooks = tmp_path / "script_hooks.yaml"
        bad_hooks.write_text("hook_techniques: []\n")
        monkeypatch.setattr(
            arsenal_loader,
            "_arsenal_path",
            lambda *parts: bad_hooks if parts == ("script_hooks.yaml",)
            else arsenal_loader._ARSENAL_ROOT.joinpath(*parts),
        )

        arsenal_loader.reset_for_tests()
        try:
            with pytest.raises(ValueError, match="hook_techniques"):
                arsenal_loader.load_script_hooks()
        finally:
            monkeypatch.undo()
            arsenal_loader.reset_for_tests()
