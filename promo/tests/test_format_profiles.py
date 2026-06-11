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
        assert long_profile.total_words_min == 150
        assert long_profile.total_words_max == 170
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


class TestP2PersonalityBackfillPin:
    """P2 step 1 byte-identical pin: the cards carry backfilled copies of
    the pacing / asset-floor code constants. While consumers still read
    the constants, card and constant MUST agree — any drift is a silent
    two-sources-of-truth bug. P2 step 3 deletes the constants and
    rewrites this pin to assert consumers read the card."""

    def test_card_pacing_matches_code_constants(self):
        from promo.core.assign import beat_planner
        from promo.core.format_profiles import SHORT_PROFILE, LONG_PROFILE
        from promo.core.script import pause_budget

        for p in (SHORT_PROFILE, LONG_PROFILE):
            assert p.beat_min_sec == beat_planner.DEFAULT_MIN_BEAT_SEC
            assert p.beat_max_sec == beat_planner.DEFAULT_MAX_BEAT_SEC
            assert p.pause_cap_ms == pause_budget.PER_GAP_CAP_MS

    def test_card_asset_floor_matches_code_constants(self):
        from promo.core import run_receipt
        from promo.core.format_profiles import SHORT_PROFILE, LONG_PROFILE

        for p in (SHORT_PROFILE, LONG_PROFILE):
            assert p.assets_base_min == run_receipt.DEFAULT_BASE_MIN_ASSETS_FOR_FORMAT
            assert p.assets_per_extra == run_receipt.DEFAULT_EXTRA_VARIATION_ASSET_BUFFER

    def test_card_descriptions_are_distinct_nonempty(self):
        from promo.core.format_profiles import SHORT_PROFILE, LONG_PROFILE

        assert SHORT_PROFILE.description
        assert LONG_PROFILE.description
        assert SHORT_PROFILE.description != LONG_PROFILE.description

