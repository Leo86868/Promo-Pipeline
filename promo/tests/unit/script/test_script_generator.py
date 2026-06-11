"""Unit tests for promo.core.script.script_generator and promo.core.script.script_validator."""

import json
import os
import re
import shutil
import sys
import tempfile
from unittest.mock import patch, MagicMock

from pathlib import Path

import pytest

class TestP1ScriptGeneratorCLI:
    """P1 fix: CLI main() must not reference result['score']['composite']."""

    def test_generate_script_return_has_no_score(self):
        """generate_script() no longer returns a 'score' key — CLI must not expect it."""
        # The fix is to remove the score reference from CLI.
        # We verify by inspecting the source for the removed reference.
        from promo.core.script import script_generator
        import inspect
        source = inspect.getsource(script_generator.main)
        assert "score" not in source, (
            "CLI main() still references 'score' — this field was removed from generate_script()"
        )

class TestScriptValidator:
    """Ensure structural validation catches bad scripts."""

    def _make_valid_script(self):
        """Helper: create a minimally valid script dict with 50-80 words, 8-25 per segment."""
        return {
            "segments": [
                {"segment": 1, "text": "Rooms here start at twelve hundred square feet and the views go on forever. Your own slice of Big Sur coastline.",
                 "clips": [
                     {"clip_id": "0001", "cut_after": "forever."},
                     {"clip_id": "0002", "cut_after": ""},
                 ]},
                {"segment": 2, "text": "You breathe differently here. You feel the sun on your face and the wind on your skin. Your worries just melt away slowly.",
                 "clips": [
                     {"clip_id": "0003", "cut_after": "here."},
                     {"clip_id": "0004", "cut_after": "skin."},
                     {"clip_id": "0005", "cut_after": ""},
                 ]},
                {"segment": 3, "text": "Forest bathing every morning. Redwood weddings in the grove. Airstream glamping under the stars. Dinner with an ocean view.",
                 "clips": [
                     {"clip_id": "0006", "cut_after": "weddings."},
                     {"clip_id": "0007", "cut_after": ""},
                 ]},
                {"segment": 4, "text": "This is Ventana Big Sur. It stays with you long after.",
                 "clips": [
                     {"clip_id": "0008", "cut_after": ""},
                 ]},
            ],
        }

    def test_valid_script_passes(self):
        """A well-formed script should pass structural validation."""
        from promo.core.script.script_validator import validate_structural
        script = self._make_valid_script()
        # Should not raise
        validate_structural(script)

    def test_banned_word_caught(self):
        """Scripts with banned words should fail validation."""
        from promo.core.script.script_validator import validate_structural, ValidationError
        script = self._make_valid_script()
        script["segments"][0]["text"] = "This nestled resort sits by the ocean with rooms that start at very good prices honestly."
        with pytest.raises(ValidationError, match="nestled"):
            validate_structural(script)

    def test_duplicate_clip_caught(self):
        """Reusing a clip_id across segments should fail."""
        from promo.core.script.script_validator import validate_structural, ValidationError
        script = self._make_valid_script()
        # Reuse clip 0001 in segment 2
        script["segments"][1]["clips"][0]["clip_id"] = "0001"
        with pytest.raises(ValidationError, match="duplicate"):
            validate_structural(script)

    def test_long_profile_five_segment_script_passes(self):
        """65s long-form profile should accept a 5-segment script."""
        from promo.core.script.script_validator import validate_structural
        from promo.core.format_profiles import get_promo_format_profile

        script = {
            "segments": [
                {"segment": 1, "text": "Rooms here start at three thousand a night, and the canyon makes that number feel strangely calm. By the time you arrive, the outside world already feels distant, and the silence starts doing its quiet work on you.",
                 "clips": [
                     {"clip_id": "0001", "cut_after": "night,"},
                     {"clip_id": "0002", "cut_after": "calm."},
                     {"clip_id": "0003", "cut_after": ""},
                 ]},
                {"segment": 2, "text": "You arrive quiet, then everything slows down. Stone, shadow, warm concrete, and open sky take over. Nothing here asks you to perform.",
                 "clips": [
                     {"clip_id": "0004", "cut_after": "down."},
                     {"clip_id": "0005", "cut_after": "over."},
                     {"clip_id": "0006", "cut_after": ""},
                 ]},
                {"segment": 3, "text": "Morning light hits the walls first. Coffee on the terrace, a pool cut into stone, and long silences that actually feel luxurious.",
                 "clips": [
                     {"clip_id": "0007", "cut_after": "first."},
                     {"clip_id": "0008", "cut_after": "stone,"},
                     {"clip_id": "0009", "cut_after": ""},
                 ]},
                {"segment": 4, "text": "Private dinners, canyon walks, spa hours, and sunsets that keep changing color. Every detail feels edited down to only what matters.",
                 "clips": [
                     {"clip_id": "0010", "cut_after": "walks,"},
                     {"clip_id": "0011", "cut_after": "color."},
                     {"clip_id": "0012", "cut_after": ""},
                 ]},
                {"segment": 5, "text": "This is Amangiri. It never fights the desert. It lets you stay inside it a little longer.",
                 "clips": [
                     {"clip_id": "0013", "cut_after": "desert."},
                     {"clip_id": "0014", "cut_after": ""},
                 ]},
            ],
        }

        validate_structural(script, profile=get_promo_format_profile(65))

class TestPacingValidation:
    """Test pacing validation in script_validator."""

    def test_segment4_longer_than_segment1_warns(self):
        """Segment 4 being much longer than segment 1 should produce a warning."""
        from promo.core.script.script_validator import validate_pacing

        script = {
            "segments": [
                {"segment": 1, "word_count": 10, "text": "x " * 10},
                {"segment": 2, "word_count": 15, "text": "x " * 15},
                {"segment": 3, "word_count": 12, "text": "x " * 12},
                {"segment": 4, "word_count": 20, "text": "x " * 20},
            ]
        }

        warnings = validate_pacing(script, target_duration=30.0, wpm=130)
        assert any("segment 4" in w.lower() for w in warnings)

    def test_good_pacing_no_warnings(self):
        """Well-paced script should produce no warnings."""
        from promo.core.script.script_validator import validate_pacing

        # 62 words at 130 WPM needs ~28.6s of a 35s video = 81% — well under 90%
        script = {
            "segments": [
                {"segment": 1, "word_count": 18, "text": "x " * 18},
                {"segment": 2, "word_count": 18, "text": "x " * 18},
                {"segment": 3, "word_count": 16, "text": "x " * 16},
                {"segment": 4, "word_count": 10, "text": "x " * 10},
            ]
        }

        # 62 words / 130wpm * 60 = ~28.6s narration. Use 40s video for comfortable ratio.
        # effective_wpm = (62 / 40) * 60 = 93 WPM — too slow.
        # Use 30s video: effective_wpm = (62 / 30) * 60 = 124 WPM — perfect.
        # narration_ratio = (62/130*60) / 30 = 28.6/30 = 0.95 — too high.
        # Use 34s: effective_wpm = (62/34)*60 = 109 — too slow.
        # The constraint is: WPM 110-150 AND narration < 90%.
        # WPM >= 110 requires duration <= (62/110)*60 = 33.8s
        # narration < 90% requires duration >= (62/130*60)/0.9 = 31.8s
        # Sweet spot: 32s. WPM = (62/32)*60 = 116. ratio = 28.6/32 = 0.89
        warnings = validate_pacing(script, target_duration=32.0, wpm=130)
        assert len(warnings) == 0, f"Unexpected warnings: {warnings}"

    def test_long_profile_warns_when_narration_too_dense(self):
        """A structurally valid long-form script can still fail premium pacing."""
        from promo.core.format_profiles import get_promo_format_profile
        from promo.core.script.script_validator import validate_pacing

        long_profile = get_promo_format_profile(65)
        script = {
            "segments": [
                {"segment": 1, "word_count": 24, "text": "x " * 24},
                {"segment": 2, "word_count": 24, "text": "x " * 24},
                {"segment": 3, "word_count": 24, "text": "x " * 24},
                {"segment": 4, "word_count": 24, "text": "x " * 24},
                {"segment": 5, "word_count": 24, "text": "x " * 24},
            ]
        }

        warnings = validate_pacing(
            script,
            target_duration=65.0,
            wpm=120,
            profile=long_profile,
        )
        assert any("narration fills" in w.lower() for w in warnings)

class TestLongFormGenerationContract:
    """Long-form foundation rules should be enforced before productization layers."""

    def _write_persona(self, tmp_path, wpm: int = 175) -> str:
        persona_path = tmp_path / "persona.yaml"
        persona_path.write_text(
            "\n".join(
                [
                    "id: test_persona",
                    "display_name: Test Persona",
                    "perspective: third_person",
                    f"wpm: {wpm}",
                    'voice_id: ""',
                    "system_prompt: |",
                    "  You are writing a {duration_label} voiceover.",
                    "tone_keywords: []",
                    "forbidden_phrases: []",
                    "forbidden_openers: []",
                    "example_scripts: []",
                ]
            ),
            encoding="utf-8",
        )
        return str(persona_path)

    def _valid_long_raw_script(self) -> dict:
        # 2026-06-11 word-budget raise: extended to 168 words (LONG range
        # [150, 170]).
        # Sprint 10 C1: pause_weight on non-last segments (required by
        # validate_script_only under the two-pass Gemini schema).
        return {
            "segments": [
                {"segment": 1, "text": "Rooms here start at three thousand a night, and the canyon makes that number feel strangely calm. By the time you arrive, the outside world already feels distant, and the silence starts doing its quiet work on you.",
                 "pause_weight": 2,
                 "clips": [
                     {"clip_id": "0001", "cut_after": "night,"},
                     {"clip_id": "0002", "cut_after": "calm."},
                     {"clip_id": "0003", "cut_after": ""},
                 ]},
                {"segment": 2, "text": "You arrive quiet, then everything slows down in the heat. Stone, shadow, warm concrete, and open sky take over the senses. Nothing here asks you to perform, and nobody is keeping score of your day.",
                 "pause_weight": 2,
                 "clips": [
                     {"clip_id": "0004", "cut_after": "heat."},
                     {"clip_id": "0005", "cut_after": "senses."},
                     {"clip_id": "0006", "cut_after": ""},
                 ]},
                {"segment": 3, "text": "Morning light hits the rust-red walls first, slow and certain. Coffee on the terrace, a pool cut into stone, and long silences that actually feel luxurious instead of awkward, the kind you stop counting.",
                 "pause_weight": 2,
                 "clips": [
                     {"clip_id": "0007", "cut_after": "certain."},
                     {"clip_id": "0008", "cut_after": "stone,"},
                     {"clip_id": "0009", "cut_after": ""},
                 ]},
                {"segment": 4, "text": "Private canyon dinners, slot-canyon walks at dawn, spa hours by the boulder, and sunsets that keep changing color. Every detail feels edited down to only what truly matters, with everything else stripped quietly away.",
                 "pause_weight": 3,
                 "clips": [
                     {"clip_id": "0010", "cut_after": "dawn,"},
                     {"clip_id": "0011", "cut_after": "color."},
                     {"clip_id": "0012", "cut_after": ""},
                 ]},
                {"segment": 5, "text": "This is Amangiri. It never fights the desert. It just lets you stay inside it, a little longer than you expected, and somehow that feels exactly right.",
                 "clips": [
                     {"clip_id": "0013", "cut_after": "desert."},
                     {"clip_id": "0014", "cut_after": ""},
                 ]},
            ],
            "hook_technique": "specific_number",
            "unique_detail": "canyon calm",
        }

    def test_generate_script_variants_long_form_clip_pool_fails_before_api_requirements(self, tmp_path, monkeypatch):
        from promo.core.script.script_generator import generate_script_variants

        persona_path = self._write_persona(tmp_path)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        clips_metadata = [{"id": f"{i:04d}", "category": "scenic", "scene_description": "clip"} for i in range(1, 11)]

        with pytest.raises(RuntimeError, match="requires at least 14 unique clips"):
            generate_script_variants(
                poi_name="Test Hotel",
                location="Nowhere",
                clips_metadata=clips_metadata,
                persona_path=persona_path,
                n_variants=1,
                n_candidates=1,
                max_retries=0,
                target_duration_sec=65,
            )

    def _valid_long_raw_script_under_range(self) -> dict:
        """Under-range LONG fixture (~110 words, below the 130 floor).

        Used to test normalize-not-reject behavior on short scripts; the main
        ``_valid_long_raw_script`` fixture is now in-range per Sprint 08.5.
        Sprint 10 C1: pause_weight added for schema parity with the in-range
        fixture (normalize_script still raises on LONG under-range before
        validate_script_only would inspect pause_weight).
        """
        return {
            "segments": [
                {"segment": 1, "text": "Rooms here start at three thousand a night, and the canyon makes that number feel strangely calm. By the time you arrive, the outside world already feels distant, and the silence starts doing its quiet work on you.",
                 "pause_weight": 2,
                 "clips": [
                     {"clip_id": "0001", "cut_after": "night,"},
                     {"clip_id": "0002", "cut_after": "calm."},
                     {"clip_id": "0003", "cut_after": ""},
                 ]},
                {"segment": 2, "text": "You arrive quiet, then everything slows down. Stone, shadow, warm concrete, and open sky take over. Nothing here asks you to perform.",
                 "pause_weight": 2,
                 "clips": [
                     {"clip_id": "0004", "cut_after": "down."},
                     {"clip_id": "0005", "cut_after": "over."},
                     {"clip_id": "0006", "cut_after": ""},
                 ]},
                {"segment": 3, "text": "Morning light hits the walls first. Coffee on the terrace, a pool cut into stone, and long silences that actually feel luxurious.",
                 "pause_weight": 2,
                 "clips": [
                     {"clip_id": "0007", "cut_after": "first."},
                     {"clip_id": "0008", "cut_after": "stone,"},
                     {"clip_id": "0009", "cut_after": ""},
                 ]},
                {"segment": 4, "text": "Private dinners, canyon walks, spa hours, and sunsets that keep changing color. Every detail feels edited down to only what matters.",
                 "pause_weight": 3,
                 "clips": [
                     {"clip_id": "0010", "cut_after": "walks,"},
                     {"clip_id": "0011", "cut_after": "color."},
                     {"clip_id": "0012", "cut_after": ""},
                 ]},
                {"segment": 5, "text": "This is Amangiri. It never fights the desert. It lets you stay inside it a little longer.",
                 "clips": [
                     {"clip_id": "0013", "cut_after": "desert."},
                     {"clip_id": "0014", "cut_after": ""},
                 ]},
            ],
            "hook_technique": "specific_number",
            "unique_detail": "canyon calm",
        }

    def test_generate_script_retries_under_range_long_form(self, tmp_path, monkeypatch, caplog):
        """Sprint 08.5 (fix B2): under-130 LONG scripts are hard-rejected at
        the normalizer so the retry loop gets another Gemini draw. With
        n_variants=1, n_candidates=1, max_retries=0 and a single under-range
        candidate, the function exhausts its per-variant budget of 1 attempt
        and raises RuntimeError — proving the hard-gate is in force.

        Previously (Sprint 08): under-range LONG was accepted with a warning
        under normalize-not-reject. Sprint 08.5 flipped LONG to hard-reject
        because the AC1 floor of 130 must actually hold, not drift.

        Sprint 13 AC10/D-003: rewritten on top of ``generate_script_variants``
        after the duplicated singular ``generate_script`` was deleted. The
        per-variant-budget raise shape is the new error surface.
        """
        import logging
        import pytest
        from promo.core.script.script_generator import generate_script_variants

        persona_path = self._write_persona(tmp_path, wpm=120)
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        clips_metadata = [{"id": f"{i:04d}", "category": "scenic", "scene_description": "clip"} for i in range(1, 15)]

        with patch("promo.core.script.script_generator.resolve_gemini_model", return_value=object()), \
             patch("promo.core.script.script_generator._generate_one", return_value=self._valid_long_raw_script_under_range()):
            with caplog.at_level(logging.WARNING, logger="promo.core.script.script_generator"):
                with pytest.raises(RuntimeError, match=r"Variant 1/1 for 'Test Hotel' exhausted its per-variant budget of 1 attempts"):
                    generate_script_variants(
                        poi_name="Test Hotel",
                        location="Nowhere",
                        clips_metadata=clips_metadata,
                        persona_path=persona_path,
                        n_variants=1,
                        n_candidates=1,
                        max_retries=0,
                        target_duration_sec=65,
                    )

        # The retry-attempt warning must name the LONG floor breach.
        assert any(
            "below LONG floor" in rec.getMessage() for rec in caplog.records
        ), "expected LONG floor hard-rejection in retry attempt warnings"

    def test_generate_script_variants_rejects_partial_delivery(self, tmp_path, monkeypatch):
        from promo.core.script.script_generator import generate_script_variants

        # 2026-06-11: wpm=175 tracks measured TTS speech rate; fixture is 168 words
        # stays under profile.max_narration_ratio=0.92 at target 65s.
        persona_path = self._write_persona(tmp_path, wpm=175)
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        clips_metadata = [{"id": f"{i:04d}", "category": "scenic", "scene_description": "clip"} for i in range(1, 15)]
        valid_script = self._valid_long_raw_script()

        # Sprint 09a H-002: per-variant retry budget. Variant 1 accepts the
        # first valid script. Variant 2 receives ONLY the same script, which
        # the cross-variant de-dup rejects, and then variant 2 exhausts its
        # own budget (1 attempt with max_retries=0, n_candidates=1) — raising
        # a Variant-scoped RuntimeError instead of the old aggregate message.
        with patch("promo.core.script.script_generator.resolve_gemini_model", return_value=object()), \
             patch(
                 "promo.core.script.script_generator._generate_one",
                 side_effect=[valid_script, valid_script],
             ):
            with pytest.raises(RuntimeError, match="Variant 2/2 for 'Test Hotel' exhausted its per-variant budget"):
                generate_script_variants(
                    poi_name="Test Hotel",
                    location="Nowhere",
                    clips_metadata=clips_metadata,
                    persona_path=persona_path,
                    n_variants=2,
                    n_candidates=1,
                    max_retries=0,
                    target_duration_sec=65,
                )

class TestSprint08NormalizeScript:
    """normalize_script strips openers, trims overflow, keeps safety clause."""

    def test_strip_forbidden_opener(self):
        from promo.core.script.script_validator import normalize_script
        from promo.core.format_profiles import SHORT_PROFILE
        s = {"segments": [
            {"segment": 1, "text": "Imagine walking in. A calm, warm lobby. The scent of cedar hits first.",
             "word_count": 14},
            {"segment": 2, "text": "The pool sits quiet under stars. No phones out here at all.",
             "word_count": 12},
            {"segment": 3, "text": "Breakfast on the terrace. Private guide. Two bikes waiting outside.",
             "word_count": 11},
            {"segment": 4, "text": "This is the hotel. It knows exactly what it wants to be.",
             "word_count": 12},
        ]}
        normalize_script(s, profile=SHORT_PROFILE)
        first = s["segments"][0]["text"]
        assert not first.lower().startswith("imagine")
        # Capitalization fixed on remainder.
        assert first[0].isupper()

    def test_trim_overflow_at_sentence_boundary(self):
        from promo.core.script.script_validator import normalize_script
        from promo.core.format_profiles import LONG_PROFILE
        long_tail = " ".join(["Sentence {i} with enough words here to count.".format(i=i) for i in range(20)])
        s = {"segments": [
            {"segment": 1, "text": " ".join(["a"] * 30) + ".", "word_count": 31},
            {"segment": 2, "text": " ".join(["b"] * 30) + ".", "word_count": 31},
            {"segment": 3, "text": " ".join(["c"] * 30) + ".", "word_count": 31},
            {"segment": 4, "text": " ".join(["d"] * 20) + ".", "word_count": 21},
            {"segment": 5, "text": long_tail, "word_count": len(long_tail.split())},
        ]}
        normalize_script(s, profile=LONG_PROFILE)
        new_total = sum(len(seg["text"].split()) for seg in s["segments"])
        assert new_total <= LONG_PROFILE.total_words_max
        # Trimmed on a sentence boundary.
        assert s["segments"][-1]["text"].endswith(".")

    def test_long_under_floor_raises_for_retry(self):
        """LONG hard-gates the 130-word floor so the generation loop retries."""
        from promo.core.script.script_validator import normalize_script, ValidationError
        from promo.core.format_profiles import LONG_PROFILE
        s = {"segments": [
            {"segment": i, "text": "Short.", "word_count": 1} for i in range(1, 6)
        ]}
        import pytest
        with pytest.raises(ValidationError) as exc_info:
            normalize_script(s, profile=LONG_PROFILE)
        assert "below LONG floor" in str(exc_info.value)
        assert "150" in str(exc_info.value)

    def test_short_under_range_still_warns(self, caplog):
        """SHORT keeps normalize-not-reject — pause budget absorbs the shortfall."""
        import logging
        from promo.core.script.script_validator import normalize_script
        from promo.core.format_profiles import SHORT_PROFILE
        s = {"segments": [
            {"segment": i, "text": "Short.", "word_count": 1} for i in range(1, 5)
        ]}
        with caplog.at_level(logging.WARNING, logger="promo.core.script.script_validator"):
            normalize_script(s, profile=SHORT_PROFILE)
        assert sum(len(seg["text"].split()) for seg in s["segments"]) == 4
        assert any("below profile min" in rec.getMessage() for rec in caplog.records)

    def test_safety_clause_keeps_original_if_trim_would_undershoot(self):
        """Trim would drop the total below profile min → keep the original overshoot."""
        from promo.core.script.script_validator import normalize_script
        from promo.core.format_profiles import LONG_PROFILE
        # Build 145 words with an 8-sentence last segment — trimming any
        # sentence from last drops total to <118 (min).
        head = [{"segment": i, "text": " ".join(["w"] * 22) + ".", "word_count": 23} for i in range(1, 5)]
        # head total = 92
        last_text = " ".join(["Close sentence word."] * 20)  # 60 words, 20 sentences
        total = 92 + 60  # = 152
        s = {"segments": head + [{"segment": 5, "text": last_text, "word_count": 60}]}
        # Trimming sentences from last is fine because 20 sentences × 3 words
        # leaves plenty of headroom. Make it tight: need scenario where
        # ANY trim drops below 118.
        # Simpler: head = 115 words, last = 35 words, total = 150.
        # Trim any sentence from last → total drops below 118 (min). Keep.
        s = {"segments": [
            {"segment": 1, "text": " ".join(["x"] * 30) + ".", "word_count": 31},
            {"segment": 2, "text": " ".join(["y"] * 30) + ".", "word_count": 31},
            {"segment": 3, "text": " ".join(["z"] * 25) + ".", "word_count": 26},
            {"segment": 4, "text": " ".join(["q"] * 27) + ".", "word_count": 28},
            {"segment": 5, "text": "Alpha beta gamma. Delta epsilon zeta. Eta theta iota.", "word_count": 9},
        ]}
        # head = 31+31+26+28 = 116. last = 9. total = 125. Over max 140? no, 125 ≤ 140.
        # Try another shape to actually test safety clause.
        s = {"segments": [
            {"segment": 1, "text": " ".join(["x"] * 40) + ".", "word_count": 41},
            {"segment": 2, "text": " ".join(["y"] * 40) + ".", "word_count": 41},
            {"segment": 3, "text": " ".join(["z"] * 30) + ".", "word_count": 31},
            {"segment": 4, "text": "A.", "word_count": 1},
            {"segment": 5, "text": "Alpha beta gamma delta epsilon zeta eta theta. Iota kappa.", "word_count": 10},
        ]}
        # Sprint 08.5 (fix B2): LONG hard-rejects under-130, so the old
        # "LONG under-range survives" path is removed. We now test the safety
        # clause on SHORT (which keeps normalize-not-reject) — trimming must
        # refuse to drop the total below SHORT's 50-word floor.
        from promo.core.format_profiles import SHORT_PROFILE
        # SHORT profile: total_words range [50, 80]. Build total=85 (overshoot
        # by 5) with a last segment that only has 1 sentence → safety clause
        # MUST keep the overshoot because there's nothing sentence-level to
        # trim without destroying segment integrity.
        s_short = {"segments": [
            {"segment": 1, "text": " ".join(["w1"] * 30) + ".", "word_count": 31},
            {"segment": 2, "text": " ".join(["w2"] * 30) + ".", "word_count": 31},
            {"segment": 3, "text": " ".join(["w3"] * 15) + ".", "word_count": 16},
            {"segment": 4, "text": " ".join(["w4"] * 10) + ".", "word_count": 11},  # single sentence
        ]}
        # total = 31+31+16+11 = 89 (over SHORT max 80). Last segment has only
        # 1 sentence — the safety clause in _trim_overflow should keep the
        # overshoot and log a warning.
        out = normalize_script(s_short, profile=SHORT_PROFILE)
        assert "segments" in out
        # Overshoot is preserved (last segment can't be trimmed at sentence boundary).
        assert sum(len(seg["text"].split()) for seg in out["segments"]) > SHORT_PROFILE.total_words_max

class TestSprint085ClipInventorySourceDuration:
    """_format_clip_inventory includes source_duration_sec so Gemini sees capacity."""

    def test_source_duration_in_inventory(self):
        from promo.core.script.script_generator import _format_clip_inventory
        meta = [
            {"id": "0001", "category": "pool",
             "scene_description": "blue water", "source_duration_sec": 8.0},
        ]
        out = _format_clip_inventory(meta)
        assert "8.0s" in out

    def test_inventory_without_source_duration_still_works(self):
        from promo.core.script.script_generator import _format_clip_inventory
        meta = [
            {"id": "0001", "category": "pool",
             "scene_description": "blue water"},
        ]
        out = _format_clip_inventory(meta)
        assert "Clip 0001" in out
        # No duration token shown when source_duration_sec is missing.
        assert "s — " in out or "s" not in out.split("Clip 0001:")[1].split("—")[0]

class TestSprint09bC5BanListPruning:
    """Sprint 09b C5 (ACs 19-23): content-only pruning of the three
    ban-list structures. Keep them separate (operator directive — no
    structural consolidation); just shrink the entries so Gemini has
    room to write.
    """

    def test_banned_words_pruned(self):
        from promo.core.script.script_validator import BANNED_WORDS
        assert len(BANNED_WORDS) <= 15, (
            f"BANNED_WORDS has {len(BANNED_WORDS)} entries; contract caps at 15"
        )
        # Spot-check the survivors: high-signal AI tells must stay.
        assert "nestled" in BANNED_WORDS
        assert "delve" in BANNED_WORDS

    def test_forbidden_openers_pruned(self):
        from promo.core.script.script_validator import FORBIDDEN_OPENERS
        assert len(FORBIDDEN_OPENERS) <= 5, (
            f"FORBIDDEN_OPENERS has {len(FORBIDDEN_OPENERS)} entries; cap 5"
        )
        # `imagine` is the canonical opener AI-tell; must survive pruning.
        assert "imagine" in FORBIDDEN_OPENERS

    def test_persona_yaml_forbidden_phrases_pruned(self):
        import yaml
        import pathlib
        # Sprint Arsenal Externalization (Commit 7) relocated personas to
        # promo/arsenal/personas/. Path-pinned tests update accordingly.
        path = pathlib.Path(__file__).resolve().parents[3] / "arsenal" / "personas" / "third_person_promo.yaml"
        with open(path, "r") as f:
            persona = yaml.safe_load(f)
        entries = persona.get("forbidden_phrases", [])
        assert len(entries) <= 8, (
            f"forbidden_phrases has {len(entries)} entries; contract caps at 8"
        )

    def test_no_structural_consolidation_script_validator_no_yaml_import(self):
        """AC22: script_validator.py must NOT have a new yaml import
        introduced by this sprint. Ban-lists stay in native formats."""
        import inspect
        from promo.core.script import script_validator
        src = inspect.getsource(script_validator)
        assert "import yaml" not in src
        assert "from yaml " not in src

    def test_no_structural_consolidation_persona_yaml_stays_native(self):
        """AC22 inverse: persona stays a pure YAML data file — no tags or
        directives that would pull ban-list values from Python at load
        time. Comments referencing constant names are fine (and expected
        for cross-reference); what's forbidden is YAML import directives
        or environment-variable interpolation that would couple the
        YAML to validator code."""
        import pathlib
        # Sprint Arsenal Externalization (Commit 7) relocated personas
        # to promo/arsenal/personas/.
        yaml_path = pathlib.Path(__file__).resolve().parents[3] / "arsenal" / "personas" / "third_person_promo.yaml"
        yaml_text = yaml_path.read_text()
        # PyYAML tag-based object instantiation (e.g. `!!python/object`)
        # would be the only way a YAML could import Python code.
        assert "!!python/" not in yaml_text
        # No `<<: *script_validator_*` anchor merge from an external file
        # either (fine if absent; would indicate runtime coupling).
        assert "!import " not in yaml_text
        assert "!include " not in yaml_text

class TestSprint10C1ValidateScriptOnly:
    """C1 criterion 2: validate_script_only raises on each pass-1 failure
    condition and accepts a well-formed happy-path script.
    """

    def _long_happy_path(self) -> dict:
        """In-range LONG script with pause_weight on non-last segments.
        Mirrors the shape Gemini #1 will emit under Sprint 10 (no clips field).
        Word counts per segment: 28 + 27 + 29 + 28 + 21 = 133 (inside the
        [130, 140] LONG band, 130/140 profile bounds).
        """
        return {
            "segments": [
                {"segment": 1, "pause_weight": 2, "text":
                 "Rooms here start at three thousand a night, and the canyon makes that number feel strangely calm. By the time you arrive, the outside world already feels distant, and the silence starts doing its quiet work on you."},
                {"segment": 2, "pause_weight": 2, "text":
                 "You arrive quiet, then everything slows down in the heat. Stone, shadow, warm concrete, and open sky take over the senses. Nothing here asks you to perform, and nobody is keeping score of your day."},
                {"segment": 3, "pause_weight": 2, "text":
                 "Morning light hits the rust-red walls first, slow and certain. Coffee on the terrace, a pool cut into stone, and long silences that actually feel luxurious instead of awkward, the kind you stop counting."},
                {"segment": 4, "pause_weight": 3, "text":
                 "Private canyon dinners, slot-canyon walks at dawn, spa hours by the boulder, and sunsets that keep changing color. Every detail feels edited down to only what truly matters, with everything else stripped quietly away."},
                {"segment": 5, "text":
                 "This is Amangiri. It never fights the desert. It just lets you stay inside it, a little longer than you expected, and somehow that feels exactly right."},
            ],
            "hook_technique": "specific_number",
            "unique_detail": "canyon calm",
        }

    def test_happy_path_accepts_script_without_clips(self):
        """A well-formed LONG script with pause_weight but no clips array
        passes validate_script_only silently and refreshes word_count +
        total_words on the script dict in place.
        """
        from promo.core.script.script_validator import validate_script_only
        from promo.core.format_profiles import get_promo_format_profile

        script = self._long_happy_path()
        profile = get_promo_format_profile(65)
        validate_script_only(script, profile=profile)  # must not raise
        assert script["total_words"] == sum(
            s["word_count"] for s in script["segments"]
        )
        assert all("word_count" in s for s in script["segments"])
        # No clip validation — the happy path has no 'clips' field at all.
        assert all("clips" not in s for s in script["segments"])

    def test_missing_segments_raises(self):
        from promo.core.script.script_validator import validate_script_only, ValidationError
        from promo.core.format_profiles import get_promo_format_profile

        with pytest.raises(ValidationError, match="Missing or invalid 'segments'"):
            validate_script_only({}, profile=get_promo_format_profile(65))

    def test_wrong_segment_count_raises(self):
        """LONG profile expects 5 segments; feeding 4 raises."""
        from promo.core.script.script_validator import validate_script_only, ValidationError
        from promo.core.format_profiles import get_promo_format_profile

        script = self._long_happy_path()
        script["segments"].pop()  # 5 → 4
        with pytest.raises(ValidationError, match="expected 5 segments, got 4"):
            validate_script_only(script, profile=get_promo_format_profile(65))

    def test_per_segment_word_count_below_band_raises(self):
        from promo.core.script.script_validator import validate_script_only, ValidationError
        from promo.core.format_profiles import get_promo_format_profile

        script = self._long_happy_path()
        # Replace segment 2 text with something well under per_segment_min.
        script["segments"][1]["text"] = "Two words."
        with pytest.raises(ValidationError, match=r"segment 2 has 2 words"):
            validate_script_only(script, profile=get_promo_format_profile(65))

    def test_per_segment_word_count_above_band_raises(self):
        from promo.core.script.script_validator import validate_script_only, ValidationError
        from promo.core.format_profiles import get_promo_format_profile

        script = self._long_happy_path()
        # Blow past per_segment_max with a long run of filler words.
        script["segments"][0]["text"] = " ".join(["word"] * 80)
        with pytest.raises(ValidationError, match=r"segment 1 has \d+ words"):
            validate_script_only(script, profile=get_promo_format_profile(65))

    def test_banned_word_raises(self):
        from promo.core.script.script_validator import validate_script_only, ValidationError
        from promo.core.format_profiles import get_promo_format_profile

        script = self._long_happy_path()
        # Keep segment 1 within per-segment band by substituting one token —
        # "canyon" (6 letters) → "nestled" (7 letters) leaves word count intact.
        script["segments"][0]["text"] = script["segments"][0]["text"].replace(
            "canyon", "nestled", 1,
        )
        with pytest.raises(ValidationError, match="nestled"):
            validate_script_only(script, profile=get_promo_format_profile(65))

    def test_missing_pause_weight_on_non_last_raises(self):
        """Contract criterion 2: pause_weight missing on any non-last
        segment raises.
        """
        from promo.core.script.script_validator import validate_script_only, ValidationError
        from promo.core.format_profiles import get_promo_format_profile

        script = self._long_happy_path()
        del script["segments"][1]["pause_weight"]
        with pytest.raises(ValidationError, match=r"segment 2 pause_weight .*invalid"):
            validate_script_only(script, profile=get_promo_format_profile(65))

    def test_invalid_pause_weight_value_raises(self):
        """pause_weight not in {1,2,3} raises (e.g., 0, 4, or string)."""
        from promo.core.script.script_validator import validate_script_only, ValidationError
        from promo.core.format_profiles import get_promo_format_profile

        for bad_weight in (0, 4, "2", None):
            script = self._long_happy_path()
            script["segments"][0]["pause_weight"] = bad_weight
            with pytest.raises(ValidationError, match=r"segment 1 pause_weight .*invalid"):
                validate_script_only(script, profile=get_promo_format_profile(65))

    def test_last_segment_pause_weight_not_required(self):
        """Contract criterion 2 + Gemini #1 prompt: last segment's
        pause_weight is ignored. Missing or invalid on the last segment
        must NOT raise.
        """
        from promo.core.script.script_validator import validate_script_only
        from promo.core.format_profiles import get_promo_format_profile

        profile = get_promo_format_profile(65)

        script_missing = self._long_happy_path()  # last segment already has no pause_weight
        validate_script_only(script_missing, profile=profile)  # no raise

        script_invalid = self._long_happy_path()
        script_invalid["segments"][-1]["pause_weight"] = 99
        validate_script_only(script_invalid, profile=profile)  # no raise

class TestSprint10C1PromptSchema:
    """C1 criterion 1: the Gemini #1 prompt drops the clips[] output template
    but keeps VIDEO CLIP INVENTORY as a grounding reference, and explicitly
    tells Gemini #1 not to emit clip IDs.
    """

    def _clips_metadata(self) -> list[dict]:
        return [
            {"id": f"{i:04d}", "category": "scenic",
             "scene_description": "clip", "source_duration_sec": 6.0}
            for i in range(1, 15)
        ]

    def _render_prompt(self, duration_sec: float = 65.0) -> str:
        from promo.core.script.script_generator import _build_prompt, load_persona, _DEFAULT_PERSONA_PATH
        from promo.core.format_profiles import get_promo_format_profile

        persona = load_persona(_DEFAULT_PERSONA_PATH)
        profile = get_promo_format_profile(duration_sec)
        return _build_prompt(
            poi_name="Test Hotel",
            location="Nowhere",
            clips_metadata=self._clips_metadata(),
            persona=persona,
            profile=profile,
        )

    def test_inventory_header_present_as_grounding_reference(self):
        prompt = self._render_prompt()
        assert "VIDEO CLIP INVENTORY" in prompt
        assert "do NOT reference clip IDs in your output" in prompt

    def test_asset_visual_brief_replaces_full_inventory_when_supplied(self):
        from promo.core.script.script_generator import _build_prompt, load_persona, _DEFAULT_PERSONA_PATH
        from promo.core.format_profiles import get_promo_format_profile

        persona = load_persona(_DEFAULT_PERSONA_PATH)
        profile = get_promo_format_profile(65)
        prompt = _build_prompt(
            poi_name="Test Hotel",
            location="Nowhere",
            clips_metadata=self._clips_metadata(),
            persona=persona,
            profile=profile,
            asset_visual_brief={
                "eligible_asset_count": 78,
                "eligible_total_seconds": 555.2,
                "categories": [
                    {
                        "category": "pool",
                        "asset_count": 12,
                        "total_seconds": 87.5,
                        "coverage_motifs": [{"phrase": "ocean-view pool"}],
                    }
                ],
                "core_visuals": [{"phrase": "cliffside resort exterior"}],
                "grounding_set": [
                    {
                        "coverage_role": "secondary",
                        "category": "beach",
                        "visual_detail": "paddleboarders near a rocky coastline",
                    }
                ],
                "summary_note": "No item is a specific clip assignment.",
            },
        )

        assert "ASSET VISUAL BRIEF" in prompt
        assert "pool: 12 clips, 87.5s" in prompt
        assert "[secondary] beach: paddleboarders near a rocky coastline" in prompt
        assert "Clip 0001:" not in prompt

    def test_output_template_has_no_clips_array(self):
        """The JSON output block shown to Gemini #1 must NOT contain
        literal 'clips' / 'clip_id' / 'cut_after' fields. VIDEO CLIP
        INVENTORY text elsewhere in the prompt is allowed — we're only
        gating the output schema block.
        """
        prompt = self._render_prompt()
        # Locate the output-template section.
        anchor = prompt.index("Output ONLY valid JSON:")
        template = prompt[anchor:]
        assert '"clips":' not in template, \
            "Gemini #1 output template must not declare a clips array"
        assert '"clip_id"' not in template, \
            "Gemini #1 output template must not declare clip_id"
        assert "cut_after" not in template, \
            "Gemini #1 output template must not declare cut_after"
        assert '"total_clips"' not in template, \
            "Gemini #1 output template must not declare total_clips"

    def test_prompt_has_no_clips_rules_section(self):
        prompt = self._render_prompt()
        assert "RULES for clips array" not in prompt
        assert "NO FREEZE" not in prompt, (
            "NO FREEZE rule is a Sprint 09 artifact of one-pass Gemini; "
            "Sprint 10 Gemini #2 enforces source-duration via hard constraint"
        )

    def test_prompt_tells_gemini_not_to_assign_clips(self):
        """Explicit guidance to Gemini #1 that clip assignment is downstream.
        L-004 fix: single unambiguous substring check — the prior disjunction
        collapsed to one branch after .lower() and left prompt-regression
        risk uncovered.
        """
        prompt_lower = self._render_prompt().lower()
        assert "do not choose clips" in prompt_lower, (
            "Gemini #1 prompt must tell the model NOT to choose clips — the "
            "downstream clip-assignment stage handles that. Regression guard: "
            "if you rephrase the directive, update this assertion too."
        )

    def test_stubbed_gemini_response_parses_without_keyerror(self):
        """Criterion 1 verify (b): feeding a Gemini response matching the
        new schema (no clips field) through normalize_script +
        validate_script_only raises no KeyError on the missing clip fields.
        """
        from promo.core.script.script_validator import normalize_script, validate_script_only
        from promo.core.format_profiles import get_promo_format_profile

        profile = get_promo_format_profile(65)
        stub = {
            "segments": [
                {"segment": 1, "pause_weight": 2, "text":
                 "Rooms here start at three thousand a night, and the canyon makes that number feel strangely calm. By the time you arrive, the outside world already feels distant, and the silence starts doing its quiet work on you."},
                {"segment": 2, "pause_weight": 2, "text":
                 "You arrive quiet, then everything slows down in the heat. Stone, shadow, warm concrete, and open sky take over the senses. Nothing here asks you to perform, and nobody is keeping score of your day."},
                {"segment": 3, "pause_weight": 2, "text":
                 "Morning light hits the rust-red walls first, slow and certain. Coffee on the terrace, a pool cut into stone, and long silences that actually feel luxurious instead of awkward, the kind you stop counting."},
                {"segment": 4, "pause_weight": 3, "text":
                 "Private canyon dinners, slot-canyon walks at dawn, spa hours by the boulder, and sunsets that keep changing color. Every detail feels edited down to only what truly matters, with everything else stripped quietly away."},
                {"segment": 5, "text":
                 "This is Amangiri. It never fights the desert. It just lets you stay inside it, a little longer than you expected, and somehow that feels exactly right."},
            ],
            "hook_technique": "specific_number",
            "unique_detail": "canyon calm",
        }
        normalize_script(stub, profile=profile)
        validate_script_only(stub, profile=profile)  # must not raise

class TestSprint10C1ValidateStructuralCallSitesRetired:
    """C1 criterion 3: validate_structural is no longer called from
    script_generator.py. The grep target is zero matches in that module;
    the function itself still exists in script_validator.py (C5 will
    retire it if nothing calls it).
    """

    def test_script_generator_does_not_import_validate_structural(self):
        import inspect
        from promo.core.script import script_generator

        source = inspect.getsource(script_generator)
        assert "validate_structural" not in source, (
            "Sprint 10 C1: validate_structural must be replaced by "
            "validate_script_only inside script_generator.py"
        )
        assert "validate_script_only" in source, (
            "Sprint 10 C1: validate_script_only must be wired as the pass-1 "
            "validator inside script_generator.py"
        )

class TestSprintArsenalExternalizationGemini1Template:
    """AC-23: the Gemini #1 prompt template renders cleanly.

    Triple-assertion (all 3 must hold):
      (a) ``Template(load_system_prompt("gemini1_script")).substitute(<all required slots>)``
          MUST NOT raise (every ``$identifier`` placeholder has a binding).
      (b) ``re.search(r'\\$[A-Za-z_][A-Za-z0-9_]*', rendered)`` MUST return None
          (no residual ``$identifier`` placeholder pattern; mirrors
          ``string.Template.idpattern`` to avoid false positives on a
          correctly-rendered ``$1,900``).
      (c) ``"$$" not in rendered`` AND ``"$1,900" in rendered`` — actively
          confirms the template stores ``$$`` for literal-``$`` escaping
          and that ``substitute()`` collapsed it back to a single ``$``.

    Guards against future literal-``$`` regressions (the ``$1,900``
    spoken-numbers rule line at ``script_generator.py:336`` was the one
    site that needed ``$$`` escaping when the prompt migrated; if anyone
    adds another ``$<digit>`` literal they must remember to escape it).
    """

    def _required_slots(self) -> dict:
        """Every ``$identifier`` in ``gemini1_script_v1.md`` mapped to a
        dummy value that is byte-distinct from any other slot, so a
        wrong-key substitution would surface as a noticeable artifact
        in the rendered output."""
        return {
            "system_prompt": "DUMMY_SYSTEM_PROMPT",
            "feedback_block": "",
            "poi_name": "Dummy POI",
            "location": "Dummy, USA",
            "hotel_description_block": "",
            "notable_details_block": "",
            "segment_count": 4,
            "target_word_midpoint": 65,
            "segment_structure": "DUMMY_SEGMENT_STRUCTURE",
            "sentence_rule": "DUMMY_SENTENCE_RULE",
            "extra_rules_block": "",
            "banned_phrases": "DUMMY_BANNED_PHRASES",
            "variant_note": "",
            "pause_block": "",
            "examples": "DUMMY_EXAMPLES",
            "clip_inventory": "DUMMY_CLIP_INVENTORY",
        }

    def test_ac23_substitute_does_not_raise(self):
        """AC-23(a): every ``$identifier`` has a binding in the caller."""
        from string import Template
        from promo.core.arsenal_loader import load_system_prompt

        Template(load_system_prompt("gemini1_script")).substitute(
            **self._required_slots()
        )  # raises ValueError on missing key — implicit assertion is "no raise"

    def test_ac23_no_residual_placeholder(self):
        """AC-23(b): ``\\$identifier`` pattern (mirrors
        ``string.Template.idpattern``) MUST be absent from the rendered
        output.

        Critically the regex anchors on ``[A-Za-z_]`` (NOT ``\\w``) so a
        correctly-rendered ``$1,900`` literal does NOT false-positive."""
        import re
        from string import Template
        from promo.core.arsenal_loader import load_system_prompt

        rendered = Template(load_system_prompt("gemini1_script")).substitute(
            **self._required_slots()
        )
        residual = re.search(r"\$[A-Za-z_][A-Za-z0-9_]*", rendered)
        assert residual is None, (
            f"residual placeholder pattern found at offset {residual.start()}: "
            f"{rendered[max(0, residual.start()-30):residual.end()+30]!r}"
        )

    def test_ac23_dollar_escaping_intact(self):
        """AC-23(c): ``$$`` collapsed → ``$``; the literal ``$1,900``
        spoken-numbers example survived the substitution."""
        from string import Template
        from promo.core.arsenal_loader import load_system_prompt

        rendered = Template(load_system_prompt("gemini1_script")).substitute(
            **self._required_slots()
        )
        assert "$$" not in rendered, (
            "rendered prompt contains '$$' — the template should have "
            "escaped only the one $1,900 literal; '$$' surviving means "
            "string.Template.substitute() did not consume it as expected."
        )
        assert "$1,900" in rendered, (
            "the WORD FORM FOR SPOKEN NUMBERS rule example '$1,900' "
            "must survive substitution; if missing, the $$ in the "
            "template was probably stripped or the line was deleted."
        )


class TestSprintArsenalExternalizationCommit6cPersonaCleanup:
    """Commit 6c snapshot pins: rendered Gemini #1 prompt differs from
    pre-Commit-6c only by:

      - 4 persona-system_prompt bullets removed (the format/segment-
        specific rules that migrated to skeleton or template).
      - The persona section header renamed ``Rules:`` → ``Voice:``
        (signals that persona is voice/perspective only — operator
        directive 2026-04-30).
      - 1 word added to the spoken-numbers RULES line in the template
        body: ``(price, year built, measurement)`` →
        ``(price, year built, measurement, count)`` so the rule
        survives migration with full vocabulary.

    Anything beyond these pins fails CI — Commit 6c is the single
    intentional-delta commit in the 6a/6b/6c trio."""

    def _render(self, profile_key: str) -> str:
        from promo.core.script.script_generator import (
            _build_prompt, load_persona, _DEFAULT_PERSONA_PATH,
        )
        from promo.core.format_profiles import FORMAT_TEMPLATES

        persona = load_persona(_DEFAULT_PERSONA_PATH)
        clips = [
            {"id": f"{i:04d}", "category": "scenic",
             "scene_description": "clip", "source_duration_sec": 6.0}
            for i in range(1, 15)
        ]
        return _build_prompt(
            poi_name="Test Hotel",
            location="Nowhere",
            clips_metadata=clips,
            persona=persona,
            profile=FORMAT_TEMPLATES[profile_key],
        )

    def _removed_persona_bullets(self) -> list[str]:
        return [
            "- Hook HARD in the first sentence. Price, superlative, or surprising fact.",
            "5-12 words max per sentence.",
            "- One specific number or fact per script (price, year, measurement, count).",
            "- End with the hotel name, naturally.",
        ]

    def _kept_persona_bullets(self) -> list[str]:
        return [
            '- Use "you" freely. Put the viewer IN the hotel.',
            "- Every sentence earns its place — no filler.",
            "- Contractions always.",
            "- React to what's shown, don't describe it.",
        ]

    def test_short_persona_bullets_removed(self):
        prompt = self._render("short")
        for bullet in self._removed_persona_bullets():
            assert bullet not in prompt, (
                f"persona bullet must be gone post-Commit-6c: {bullet!r}"
            )

    def test_long_persona_bullets_removed(self):
        prompt = self._render("long")
        for bullet in self._removed_persona_bullets():
            assert bullet not in prompt, (
                f"persona bullet must be gone post-Commit-6c: {bullet!r}"
            )

    def test_persona_kept_bullets_present(self):
        prompt = self._render("short")
        for bullet in self._kept_persona_bullets():
            assert bullet in prompt, (
                f"persona kept bullet missing — Commit 6c removed too much: {bullet!r}"
            )

    def test_persona_header_renamed_to_voice(self):
        """Persona header is now ``Voice:`` not ``Rules:`` —
        operator's framing 2026-04-30: persona is voice/perspective
        only. The persona block is followed by `\\n\\nHOTEL:` so the
        check is anchored to that boundary."""
        prompt = self._render("short")
        # The "Rules:" string still appears LATER in the prompt (the
        # template's "RULES:" section header — different word case but
        # also different anchor). We pin only the persona-block context.
        anchor = prompt.index("\n\nHOTEL:")
        persona_block = prompt[:anchor]
        assert "Voice:" in persona_block, "persona header should be 'Voice:'"
        assert "Rules:" not in persona_block, (
            "old 'Rules:' persona header must be gone post-Commit-6c"
        )

    def test_template_spoken_numbers_rule_has_count(self):
        """Template body's WORD FORM FOR SPOKEN NUMBERS adjacent rule
        line normalized: ``year built, measurement`` → ``year built,
        measurement, count`` (the one-word addition) so the rule
        preserves the full vocabulary that previously lived in the
        deleted persona bullet."""
        prompt = self._render("short")
        assert (
            "- One specific number or fact per script (price, year built, measurement, count)."
            in prompt
        ), "template's spoken-numbers rule must include 'count'"
        assert (
            "- One specific number or fact per script (price, year built, measurement)."
            not in prompt
        ), "old (count-less) template line must be gone"

    def test_replace_call_removed_from_script_generator(self):
        """The ``system_prompt.replace(...)`` call inside
        ``_build_prompt`` is gone — persona has nothing to replace
        post-Commit-6c. We AST-walk the function body so a docstring
        or comment that mentions the historical call (kept for code
        archeology) does not false-positive against an executable
        call."""
        import ast
        import inspect
        from promo.core.script import script_generator

        src = inspect.getsource(script_generator._build_prompt)
        # textwrap-dedent so the def isn't indented relative to ast.parse
        import textwrap
        tree = ast.parse(textwrap.dedent(src))
        # Walk all attribute calls inside the function body. Any call
        # of the form ``<expr>.replace(...)`` flagged.
        replace_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "replace"
        ]
        assert not replace_calls, (
            f"_build_prompt should not contain a `.replace(...)` call "
            f"after Commit 6c (persona is mode-agnostic). Found: "
            f"{[ast.unparse(c) for c in replace_calls]}"
        )
