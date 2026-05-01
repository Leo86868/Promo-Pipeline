"""Unit tests for promo.core.format_profiles."""

import json
import os
import re
import shutil
import sys
import tempfile
from unittest.mock import patch, MagicMock

from pathlib import Path

import pytest

class TestPromoFormatProfiles:
    """Duration profiles should map short and long promo modes consistently."""

    def test_short_and_long_profiles(self):
        from promo.core.format_profiles import get_promo_format_profile

        short_profile = get_promo_format_profile(30)
        long_profile = get_promo_format_profile(65)

        assert short_profile.segment_count == 4
        assert short_profile.mode == "short"
        assert long_profile.segment_count == 5
        assert long_profile.mode == "long"
        assert long_profile.total_words_min == 130
        assert long_profile.total_words_max == 140
        assert long_profile.min_clip_pool_size == 14
        assert long_profile.recommended_clip_pool_size == 18
        # Sprint 08: total_clips_* are derived from segment_plans.
        assert long_profile.total_clips_min == sum(
            sp.min_clips for sp in long_profile.segment_plans
        )
        assert long_profile.total_clips_max == sum(
            sp.max_clips for sp in long_profile.segment_plans
        )
        # SHORT profile parity check.
        assert short_profile.total_clips_min == sum(
            sp.min_clips for sp in short_profile.segment_plans
        )
        assert short_profile.total_clips_max == sum(
            sp.max_clips for sp in short_profile.segment_plans
        )

    def test_long_clip_pool_messages(self):
        from promo.core.format_profiles import get_clip_pool_messages, get_promo_format_profile

        long_profile = get_promo_format_profile(65)

        errors, warnings = get_clip_pool_messages(12, long_profile)
        assert errors == ["long format requires at least 14 unique clips; found 12"]
        assert warnings == []

        errors, warnings = get_clip_pool_messages(16, long_profile)
        assert errors == []
        assert warnings == ["long format works best with 18+ unique clips; found 16"]

class TestSprint08FormatProfilesDerived:
    """PromoFormatProfile.total_clips_* derive from segment_plans (Item 4)."""

    def test_total_clips_match_sum_of_segment_plans(self):
        from promo.core.format_profiles import SHORT_PROFILE, LONG_PROFILE
        for p in (SHORT_PROFILE, LONG_PROFILE):
            assert p.total_clips_min == sum(sp.min_clips for sp in p.segment_plans)
            assert p.total_clips_max == sum(sp.max_clips for sp in p.segment_plans)

    def test_segmentplan_clip_range_display(self):
        from promo.core.format_profiles import SegmentPlan
        sp = SegmentPlan("HOOK", 20, 2, 3, "x")
        assert sp.clip_range_display == "2-3 clips"
        sp_one = SegmentPlan("CLOSE", 10, 1, 1, "x")
        assert sp_one.clip_range_display == "1 clip"

class TestSegmentPlanClipRangeContract:
    """Advisor 2026-04-18: ``SegmentPlan`` had no ``clip_range`` attribute,
    so the two call sites in ``clip_assigner.py`` were silently
    AttributeErroring into a hardcoded ``(1, 4)`` fallback. Result:
    Gemini #2 was rendered the string "1-4 phrases" regardless of the
    format profile's actual per-segment bounds. These tests lock in
    the fixed contract + prevent a silent-fallback regression.
    """

    def test_clip_range_property_returns_min_max_tuple(self):
        """Property exposes ``(min_clips, max_clips)`` in that order."""
        from promo.core.format_profiles import SegmentPlan

        sp = SegmentPlan(
            label="hook", approx_words=20, min_clips=2, max_clips=5,
            guidance="open with a punchline",
        )
        assert sp.clip_range == (2, 5)
        # Display string unchanged by the property addition.
        assert sp.clip_range_display == "2-5 clips"

    def test_gemini2_prompt_reflects_profile_clip_range_not_default(self):
        """Regression (advisor step 3): build a profile whose per-segment
        ``(min_clips, max_clips)`` differs from the default (1, 4) and
        assert the Gemini #2 prompt contains the actual profile range
        for EVERY segment.
        """
        from promo.core.assign.clip_assigner import _build_gemini2_prompt
        from promo.core.format_profiles import PromoFormatProfile, SegmentPlan

        # Deliberately unusual shape — 3 segments, ranges 2-3 / 3-3 / 4-6.
        profile = PromoFormatProfile(
            mode="long", target_duration_sec=65, duration_label="long",
            segment_count=3,
            total_words_min=100, total_words_max=140,
            per_segment_min=25, per_segment_max=50,
            min_clip_pool_size=12, recommended_clip_pool_size=15,
            min_effective_wpm=150, max_effective_wpm=210,
            max_narration_ratio=0.95,
            segment_plans=(
                SegmentPlan("open", 30, 2, 3, "hook"),
                SegmentPlan("body", 50, 3, 3, "build"),
                SegmentPlan("close", 30, 4, 6, "close"),
            ),
        )
        script = {
            "poi_name": "Test", "location": "Nowhere",
            "target_duration_sec": 65,
            "segments": [
                {"segment": 1, "text": "one two three four five",
                 "word_count": 5, "pause_after_ms": 500},
                {"segment": 2, "text": "six seven eight nine ten",
                 "word_count": 5, "pause_after_ms": 500},
                {"segment": 3, "text": "eleven twelve thirteen fourteen fifteen",
                 "word_count": 5, "pause_after_ms": 0},
            ],
        }
        word_ts = [
            {"word": "w", "start": round(i * 0.2, 3), "end": round(i * 0.2 + 0.15, 3)}
            for i in range(15)
        ]
        prompt = _build_gemini2_prompt(
            script, word_ts, [500, 500, 0], [], 1, profile,
        )
        # The PHRASE COUNT PER SEGMENT block renders profile ranges per segment.
        assert "Segment 1: 2-3 phrases" in prompt
        assert "Segment 2: 3-3 phrases" in prompt
        assert "Segment 3: 4-6 phrases" in prompt
        # And the NARRATION block renders the display string for segment 1.
        assert "Segment 1 (2-3 clips" in prompt
        assert "Segment 2 (3 clips" in prompt  # single-value "3 clips" display
        assert "Segment 3 (4-6 clips" in prompt
        # Must NOT appear anywhere — the silent-fallback vocabulary is gone.
        assert "1-4 phrases (and thus 1-4 clips)" not in prompt

    def test_no_silent_attribute_error_fallback_on_clip_range_access(self):
        """Guardrail (advisor step 4): ``clip_assigner.py`` must NOT
        catch ``AttributeError`` around ``clip_range`` access. The
        silent-fallback pattern is what hid the SegmentPlan contract
        drift from Sprint 10a through Sprint 10b close.
        """
        import inspect
        from promo.core.assign import clip_assigner

        src = inspect.getsource(clip_assigner)
        # Two call sites previously caught (IndexError, AttributeError);
        # after the fix, neither catches AttributeError.
        assert "except (IndexError, AttributeError)" not in src
        assert "except (AttributeError, IndexError)" not in src
        assert "except AttributeError" not in src

    def test_no_silent_index_error_fallback_on_clip_range_access(self):
        """Guardrail (Sprint 10b audit-fix): the IndexError silent-
        fallback axis must also be closed. A truncated profile (fewer
        segment_plans than script segments) is a real data mismatch
        that must raise, not silently render a hardcoded 1-4 range
        into the prompt while the enforcement layer assumes a
        different range. Behavioral check — feeds a profile with a
        1-element segment_plans tuple against a 2-segment script
        and asserts the access raises IndexError up to the caller.
        """
        import pytest
        from promo.core.assign.clip_assigner import _format_phrase_timing_block
        from promo.core.format_profiles import PromoFormatProfile, SegmentPlan

        truncated_profile = PromoFormatProfile(
            mode="long", target_duration_sec=65, duration_label="long",
            segment_count=1,
            total_words_min=100, total_words_max=140,
            per_segment_min=25, per_segment_max=50,
            min_clip_pool_size=8, recommended_clip_pool_size=10,
            min_effective_wpm=150, max_effective_wpm=210,
            max_narration_ratio=0.95,
            segment_plans=(SegmentPlan("only", 50, 2, 3, "just one"),),
        )
        script = {
            "segments": [
                {"segment": 1, "text": "one two three four five", "word_count": 5,
                 "pause_after_ms": 500},
                {"segment": 2, "text": "six seven eight nine ten", "word_count": 5,
                 "pause_after_ms": 0},
            ],
        }
        word_ts = [
            {"word": "w", "start": round(i * 0.2, 3), "end": round(i * 0.2 + 0.15, 3)}
            for i in range(10)
        ]
        with pytest.raises(IndexError):
            _format_phrase_timing_block(script, word_ts, [500, 0], truncated_profile)
