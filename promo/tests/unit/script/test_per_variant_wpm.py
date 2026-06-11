"""S0.5 regression: ``_step_generate_script`` must resolve WPM per-variant
from each variant's own voice's backend, not from a single run-level
``primary_backend`` derived from voice slot 0.

Pre-S0.5 bug: when voice rotation crosses backends (default catalog ships
Kore/Gemini at slot 0 plus three ElevenLabs voices), every variant got
the Gemini bootstrap (148 WPM). On ElevenLabs variants the predicted
spoken time at 148 WPM exceeded ``target_sec * 0.90`` for short scripts,
tripping ``compute_pause_budget``'s "narration already fills target"
branch and zeroing every gap. Real ElevenLabs render at ~180-195 WPM
finished 5-7s early, leaving dead air at the tail.
"""

from unittest.mock import patch


# Voice catalog ships Kore/Gemini + three ElevenLabs voices. The rotation
# below crosses backends on slots 1 and 2, which is the default 3-variant
# voice rotation that triggered the original bug.
_ROTATION = ["kore", "jarnathan", "hope"]
_GEMINI_BOOTSTRAP = 148
_ELEVENLABS_BOOTSTRAP = 195


def _fake_scripts(n_variants):
    return [
        {
            "variant_index": i,
            "total_words": 70,
            "target_duration_sec": 30.0,
            "format_mode": "short",
            "segments": [
                {"segment": 1, "text": "hook", "word_count": 18, "pause_weight": 2},
                {"segment": 2, "text": "feel", "word_count": 18, "pause_weight": 2},
                {"segment": 3, "text": "high", "word_count": 18, "pause_weight": 2},
                {"segment": 4, "text": "close", "word_count": 16, "pause_weight": 1},
            ],
        }
        for i in range(1, n_variants + 1)
    ]


class TestPerVariantWPMResolution:
    """Each variant resolves WPM against ITS OWN voice's backend."""

    def test_mixed_backend_rotation_uses_per_variant_bootstrap(self, monkeypatch):
        """Cold-start (no prior calibration sidecar): each variant gets
        the bootstrap of its own voice's backend, not slot-0's."""
        from promo.core.pipeline import steps

        from promo.core.script import script_generator
        monkeypatch.setattr(
            script_generator,
            "generate_script_variants",
            lambda **kw: _fake_scripts(kw["n_variants"]),
        )
        monkeypatch.setattr(steps, "load_calibrated_wpm", lambda *a, **kw: None)
        compute_pause_budget_calls = []
        monkeypatch.setattr(
            steps,
            "compute_pause_budget",
            lambda segments, *, target_sec, wpm, pause_cap_ms: compute_pause_budget_calls.append(
                {"target_sec": target_sec, "wpm": wpm, "pause_cap_ms": pause_cap_ms}
            ),
        )

        scripts = steps._step_generate_script(
            poi_name="Ocean Key Resort & Spa",
            location="Key West, FL",
            clips_metadata=[],
            n_variants=3,
            script_candidates=1,
            target_duration_sec=30.0,
            hotel_description="",
            notable_details="",
            wpm_search_dirs=[],
            resolved_voice_keys=_ROTATION,
            variant_profiles=None,
            variant_personas=None,
        )

        # Slot 0 = kore (Gemini), slots 1&2 = ElevenLabs.
        assert [s["effective_wpm"] for s in scripts] == [
            _GEMINI_BOOTSTRAP,
            _ELEVENLABS_BOOTSTRAP,
            _ELEVENLABS_BOOTSTRAP,
        ]
        assert [c["wpm"] for c in compute_pause_budget_calls] == [
            _GEMINI_BOOTSTRAP,
            _ELEVENLABS_BOOTSTRAP,
            _ELEVENLABS_BOOTSTRAP,
        ]

    def test_calibration_lookup_scoped_per_variant_backend(self, monkeypatch):
        """``load_calibrated_wpm`` is invoked once per variant with that
        variant's backend — not once per run with slot-0's backend."""
        from promo.core.pipeline import steps

        from promo.core.script import script_generator
        monkeypatch.setattr(
            script_generator,
            "generate_script_variants",
            lambda **kw: _fake_scripts(kw["n_variants"]),
        )
        monkeypatch.setattr(steps, "compute_pause_budget", lambda *a, **kw: None)

        seen_backends = []

        def _capture_calibration(slug, dur, dirs, *, backend):
            seen_backends.append(backend)
            return None

        monkeypatch.setattr(steps, "load_calibrated_wpm", _capture_calibration)

        steps._step_generate_script(
            poi_name="Test POI",
            location="",
            clips_metadata=[],
            n_variants=3,
            script_candidates=1,
            target_duration_sec=30.0,
            hotel_description="",
            notable_details="",
            wpm_search_dirs=["/tmp/none"],
            resolved_voice_keys=_ROTATION,
            variant_profiles=None,
            variant_personas=None,
        )

        assert seen_backends == ["gemini", "elevenlabs", "elevenlabs"]

    def test_voice_rotation_wraps_for_n_variants_greater_than_catalog(self, monkeypatch):
        """``(variant_index - 1) % len(resolved_voice_keys)`` mirrors
        ``variant_loop``'s rotation rule. Variant 4 wraps to slot 0."""
        from promo.core.pipeline import steps

        from promo.core.script import script_generator
        monkeypatch.setattr(
            script_generator,
            "generate_script_variants",
            lambda **kw: _fake_scripts(kw["n_variants"]),
        )
        monkeypatch.setattr(steps, "load_calibrated_wpm", lambda *a, **kw: None)
        monkeypatch.setattr(steps, "compute_pause_budget", lambda *a, **kw: None)

        scripts = steps._step_generate_script(
            poi_name="Test POI",
            location="",
            clips_metadata=[],
            n_variants=4,
            script_candidates=1,
            target_duration_sec=30.0,
            hotel_description="",
            notable_details="",
            wpm_search_dirs=[],
            resolved_voice_keys=_ROTATION,
            variant_profiles=None,
            variant_personas=None,
        )

        # Variant 4 wraps to slot 0 (kore/Gemini).
        assert scripts[3]["effective_wpm"] == _GEMINI_BOOTSTRAP

    def test_calibrated_wpm_overrides_bootstrap_per_variant(self, monkeypatch):
        """When a same-backend prior-run sidecar exists, the calibrated
        value wins — but only for matching-backend variants."""
        from promo.core.pipeline import steps

        from promo.core.script import script_generator
        monkeypatch.setattr(
            script_generator,
            "generate_script_variants",
            lambda **kw: _fake_scripts(kw["n_variants"]),
        )
        monkeypatch.setattr(steps, "compute_pause_budget", lambda *a, **kw: None)

        def _calibration_for(slug, dur, dirs, *, backend):
            return 170 if backend == "elevenlabs" else None

        monkeypatch.setattr(steps, "load_calibrated_wpm", _calibration_for)

        scripts = steps._step_generate_script(
            poi_name="Test POI",
            location="",
            clips_metadata=[],
            n_variants=3,
            script_candidates=1,
            target_duration_sec=30.0,
            hotel_description="",
            notable_details="",
            wpm_search_dirs=["/tmp/none"],
            resolved_voice_keys=_ROTATION,
            variant_profiles=None,
            variant_personas=None,
        )

        # Gemini variant has no calibration → bootstrap. ElevenLabs
        # variants pick up the 170 calibrated value.
        assert [s["effective_wpm"] for s in scripts] == [_GEMINI_BOOTSTRAP, 170, 170]
